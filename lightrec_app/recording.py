"""Owns FFmpeg processes, fallback paths, and final MP4 assembly."""

from __future__ import annotations

from dataclasses import dataclass
import queue
import subprocess
import threading
import time
from pathlib import Path
from typing import Callable

from .audio import capture_audio
from .media import build_video_args, choose_encoder, encoder_label, video_bitrate_for


@dataclass(frozen=True)
class RecordingOptions:
    ffmpeg: str
    output_dir: Path
    codec: str
    fps: int
    total_bitrate: int
    display: int
    region: tuple[int, int, int, int] | None
    audio_enabled: bool
    audio_source: str
    audio_bitrate: int
    skip_unchanged: bool


@dataclass(frozen=True)
class RecordingResult:
    success: bool
    message: str
    path: Path | None
    elapsed_seconds: int
    audio_error: str | None
    saved_text: str


class RecordingSession:
    def __init__(self, root, *, on_started: Callable[[str], None],
                 on_status: Callable[[str], None], on_finished: Callable[[RecordingResult], None],
                 on_stop_signal: Callable[[bool], None]) -> None:
        self.root = root
        self.on_started = on_started
        self.on_status = on_status
        self.on_finished = on_finished
        self.on_stop_signal = on_stop_signal
        self.options: RecordingOptions | None = None
        self.process: subprocess.Popen | None = None
        self.audio_process: subprocess.Popen | None = None
        self.record_thread: threading.Thread | None = None
        self.audio_thread: threading.Thread | None = None
        self.audio_stop = threading.Event()
        self.audio_started = threading.Event()
        self.audio_clock: list[float | None] = [None]
        self.audio_errors: queue.Queue[str] = queue.Queue()
        self.started_at: float | None = None
        self.video_tmp: Path | None = None
        self.audio_tmp: Path | None = None
        self.audio_log_path: Path | None = None
        self.final_path: Path | None = None
        self.log_path: Path | None = None
        self.capture_idx = 0
        self.audio_active = False
        self.audio_runtime_error: str | None = None
        self.stopping = False
        self.finalizing = False
        self.direct_d3d11 = False
        self.current_encoder: str | None = None

    @property
    def is_recording(self) -> bool:
        return self.process is not None

    def start(self, options: RecordingOptions, encoder: str) -> str | None:
        if self.process is not None:
            return "录制已经在进行中。"
        self.options = options
        self.capture_idx = options.display
        self.current_encoder = encoder
        self.audio_stop.clear()
        self.audio_started.clear()
        self.audio_clock[0] = None
        self.audio_active = options.audio_enabled
        self.audio_runtime_error = None
        self.stopping = False
        self.finalizing = False
        while not self.audio_errors.empty():
            self.audio_errors.get_nowait()

        from datetime import datetime
        stamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S-%f")
        folder = options.output_dir
        self.final_path = folder / f"Screen_{stamp}.mp4"
        self.video_tmp = folder / f".Screen_{stamp}.recording.mp4"
        self.audio_tmp = folder / f".Screen_{stamp}.audio.m4a"
        self.audio_log_path = folder / f".Screen_{stamp}.audio.ffmpeg.log"
        self.log_path = folder / f".Screen_{stamp}.ffmpeg.log"

        if self.audio_active:
            audio_args = [
                options.ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "warning",
                "-f", "s16le", "-ar", "48000", "-ac", "2", "-i", "pipe:0",
                "-vn", "-c:a", "aac", "-b:a", f"{options.audio_bitrate}k",
                "-f", "ipod", str(self.audio_tmp),
            ]
            try:
                with open(self.audio_log_path, "wb") as audio_log:
                    self.audio_process = subprocess.Popen(
                        audio_args, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                        stderr=audio_log, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                    )
            except OSError as exc:
                if self.audio_log_path:
                    self.audio_log_path.unlink(missing_ok=True)
                return str(exc)
            self.audio_thread = threading.Thread(
                target=capture_audio,
                args=(self.audio_process, self.audio_stop, self.audio_started,
                      self.audio_clock, self.audio_errors, options.audio_source),
                name="lightrec-audio", daemon=True,
            )
            self.audio_thread.start()

        audio_bitrate = options.audio_bitrate if self.audio_active else 0
        video_bitrate = video_bitrate_for(options.total_bitrate, audio_bitrate)
        duplicate_frames = 0 if options.skip_unchanged else 1
        self.direct_d3d11 = encoder.endswith(("_amf", "_qsv"))
        try:
            with open(self.log_path, "wb") as log:
                self.process = subprocess.Popen(
                    self._video_args(encoder, video_bitrate, duplicate_frames, self.direct_d3d11),
                    stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=log,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
        except OSError as exc:
            self._stop_audio_encoder()
            if self.audio_tmp:
                self.audio_tmp.unlink(missing_ok=True)
            if self.audio_log_path:
                self.audio_log_path.unlink(missing_ok=True)
            return str(exc)

        self.started_at = time.perf_counter()
        self.on_started(encoder)
        self.record_thread = threading.Thread(target=self._watch_encoder,
                                              name="lightrec-video", daemon=True)
        self.record_thread.start()
        return None

    def stop(self) -> None:
        if self.process is None or self.finalizing:
            return
        self.stopping = True
        self.finalizing = True
        self.on_stop_signal(True)
        try:
            if self.process.stdin:
                self.process.stdin.write(b"q\n")
                self.process.stdin.flush()
        except (BrokenPipeError, OSError):
            pass
        threading.Thread(target=self._finish_after_encoder,
                         name="lightrec-finalize", daemon=True).start()

    def take_audio_error(self) -> str | None:
        try:
            return self.audio_errors.get_nowait()
        except queue.Empty:
            return None

    def _video_args(self, encoder: str, video_bitrate: int,
                    duplicate_frames: int, direct: bool) -> list[str]:
        assert self.options and self.video_tmp
        return build_video_args(
            ffmpeg=self.options.ffmpeg, encoder=encoder, fps=self.options.fps,
            video_bitrate=video_bitrate, duplicate_frames=duplicate_frames,
            display=self.options.display, region=self.options.region,
            output_path=self.video_tmp, direct=direct,
        )

    def _watch_encoder(self) -> None:
        proc = self.process
        if proc is None:
            return
        code = proc.wait()
        if (code != 0 and not self.stopping and self.started_at
                and time.perf_counter() - self.started_at < 5):
            if self.direct_d3d11:
                self.root.after(0, lambda: self._restart_compat_path(proc, code))
                return
            if self.current_encoder and self.current_encoder.endswith(("_amf", "_qsv", "_nvenc", "_mf")):
                self.root.after(0, lambda: self._restart_encoder_fallback(proc, code))
                return
        self.root.after(0, lambda: self._encoder_exited(code))

    def _restart_compat_path(self, failed_process: subprocess.Popen, code: int) -> None:
        if self.process is not failed_process or self.stopping or not self.direct_d3d11:
            return
        self.direct_d3d11 = False
        self.on_status("正在切换兼容录制路径…")
        try:
            if self.video_tmp:
                self.video_tmp.unlink(missing_ok=True)
        except OSError as exc:
            self._schedule_failure(f"Direct D3D11 capture failed (FFmpeg {code}); cannot discard its incomplete file: {exc}")
            return
        if not self.current_encoder or not self.options:
            self._schedule_failure("没有可用的视频编码器。")
            return
        audio_bitrate = self.options.audio_bitrate if self.audio_active else 0
        bitrate = video_bitrate_for(self.options.total_bitrate, audio_bitrate)
        duplicates = 0 if self.options.skip_unchanged else 1
        self._restart_process(self.current_encoder, bitrate, duplicates,
                              f"Direct D3D11 capture failed (FFmpeg {code}); compatibility mode could not start: ",
                              append_log=True)

    def _restart_encoder_fallback(self, failed_process: subprocess.Popen, code: int) -> None:
        if self.process is not failed_process or self.stopping or not self.options:
            return
        previous = self.current_encoder
        encoder = choose_encoder(self.options.ffmpeg, self.options.codec.startswith("HEVC"), after=previous)
        if not encoder:
            self._schedule_failure("所选格式的硬件编码器与 CPU 编码器都无法启动。")
            return
        self.current_encoder = encoder
        self.on_status(f"{encoder_label(previous)}启动失败，正在切换到 {encoder_label(encoder)}…")
        try:
            if self.video_tmp:
                self.video_tmp.unlink(missing_ok=True)
        except OSError as exc:
            self._schedule_failure(f"Hardware encoding failed (FFmpeg {code}); cannot discard its incomplete file: {exc}")
            return
        audio_bitrate = self.options.audio_bitrate if self.audio_active else 0
        bitrate = video_bitrate_for(self.options.total_bitrate, audio_bitrate)
        duplicates = 0 if self.options.skip_unchanged else 1
        self._restart_process(encoder, bitrate, duplicates,
                              f"{encoder_label(previous)}编码失败 (FFmpeg {code}); fallback could not start: ",
                              append_log=True)

    def _restart_process(self, encoder: str, bitrate: int, duplicates: int,
                         error_prefix: str, append_log: bool = False) -> None:
        assert self.log_path
        try:
            with open(self.log_path, "ab" if append_log else "wb") as log:
                self.process = subprocess.Popen(
                    self._video_args(encoder, bitrate, duplicates, direct=False),
                    stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=log,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
        except OSError as exc:
            self._schedule_failure(error_prefix + str(exc))
            return
        self.record_thread = threading.Thread(target=self._watch_encoder,
                                              name="lightrec-video", daemon=True)
        self.record_thread.start()

    def _schedule_failure(self, message: str) -> None:
        def finish_failure() -> None:
            self._stop_audio_encoder()
            result = RecordingResult(False, message, self.final_path, self._elapsed(),
                                     self.audio_runtime_error, "")
            self.root.after(0, lambda: self._complete(result))
        threading.Thread(target=finish_failure, name="lightrec-cleanup", daemon=True).start()

    def _encoder_exited(self, code: int) -> None:
        if self.process is None or self.stopping:
            return
        self.on_stop_signal(False)
        self.finalizing = True
        self.on_status("录制已中断，正在尝试保存…")
        threading.Thread(target=self._finish_after_encoder,
                         name="lightrec-finalize", daemon=True).start()

    def _stop_audio_encoder(self) -> None:
        self.audio_stop.set()
        if self.audio_thread and self.audio_thread.is_alive():
            self.audio_thread.join(timeout=5)
        proc = self.audio_process
        if proc is None:
            return
        if proc.stdin and not proc.stdin.closed:
            try:
                proc.stdin.close()
            except OSError:
                pass
        self._wait_or_kill(proc, timeout=10)

    @staticmethod
    def _wait_or_kill(proc: subprocess.Popen, timeout: float) -> None:
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()

    def _finish_after_encoder(self) -> None:
        proc = self.process
        if proc is None:
            return
        self._wait_or_kill(proc, timeout=20)
        self._stop_audio_encoder()
        if self.audio_active and not self.audio_started.is_set():
            self.audio_active = False
        audio_error = self.take_audio_error()
        if proc.returncode != 0:
            result = RecordingResult(False, f"FFmpeg stopped with exit code {proc.returncode}.",
                                     self.final_path, self._elapsed(), self.audio_runtime_error, "")
            self.root.after(0, lambda: self._complete(result))
            return
        if self.audio_active and self.audio_process and self.audio_process.returncode != 0:
            code = self.audio_process.returncode
            self.audio_runtime_error = self.audio_runtime_error or f"AAC encoding stopped (FFmpeg exit {code})."
            self.audio_active = False
        if audio_error:
            self.audio_runtime_error = self.audio_runtime_error or f"Audio capture failed: {audio_error}"
            self.audio_active = False
        if not self.audio_active and self.audio_tmp:
            self.audio_tmp.unlink(missing_ok=True)
        try:
            self._mux_or_move()
            message = f"视频已保存：\n{self.final_path}"
            if self.audio_runtime_error:
                message += f"\n\n视频已保存，但没有声音。{self.audio_runtime_error}"
            result = RecordingResult(True, message, self.final_path, self._elapsed(),
                                     self.audio_runtime_error, self._saved_text())
        except Exception as exc:
            result = RecordingResult(False, f"无法完成视频保存：{exc}", self.final_path,
                                     self._elapsed(), self.audio_runtime_error, "")
        self.root.after(0, lambda result=result: self._complete(result))

    def _mux_or_move(self) -> None:
        assert self.video_tmp and self.final_path and self.options and self.log_path
        if self.audio_active and self.audio_tmp and self.audio_tmp.exists():
            audio_start = self.audio_clock[0]
            video_start = self.started_at
            offset = (video_start or time.perf_counter()) - (audio_start or time.perf_counter())
            args = [self.options.ffmpeg, "-hide_banner", "-nostdin", "-y", "-loglevel", "error",
                    "-i", str(self.video_tmp)]
            if offset > 0:
                args.extend(("-ss", f"{offset:.6f}"))
            elif offset < 0:
                args.extend(("-itsoffset", f"{-offset:.6f}"))
            args.extend(("-i", str(self.audio_tmp), "-map", "0:v:0", "-map", "1:a:0",
                         "-c", "copy", "-shortest", "-movflags", "+faststart", str(self.final_path)))
            with open(self.log_path, "ab") as log:
                done = subprocess.run(args, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                      stderr=log, timeout=90,
                                      creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            if done.returncode:
                raise RuntimeError(f"FFmpeg mux failed (exit {done.returncode}). Logs: {self.log_path}, {self.audio_log_path}")
            self.audio_tmp.unlink(missing_ok=True)
            if self.audio_log_path:
                self.audio_log_path.unlink(missing_ok=True)
        else:
            args = [self.options.ffmpeg, "-hide_banner", "-nostdin", "-y", "-loglevel", "error",
                    "-i", str(self.video_tmp), "-map", "0:v:0", "-c", "copy",
                    "-movflags", "+faststart", str(self.final_path)]
            with open(self.log_path, "ab") as log:
                done = subprocess.run(args, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                      stderr=log, timeout=90,
                                      creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            if done.returncode:
                raise RuntimeError(f"FFmpeg finalize failed (exit {done.returncode}). Log: {self.log_path}")
        self.video_tmp.unlink(missing_ok=True)
        if self.audio_log_path and not self.audio_runtime_error:
            self.audio_log_path.unlink(missing_ok=True)
        self.log_path.unlink(missing_ok=True)

    def _elapsed(self) -> int:
        return int(time.perf_counter() - self.started_at) if self.started_at else 0

    def _saved_text(self) -> str:
        elapsed = self._elapsed()
        text = f"已保存：{self.final_path} · {elapsed // 60}:{elapsed % 60:02d}"
        if self.audio_runtime_error:
            text += f" · 声音录制失败：{self.audio_runtime_error}"
        return text

    def _complete(self, result: RecordingResult) -> None:
        if not result.success:
            if self.video_tmp and self.video_tmp.exists():
                result = RecordingResult(
                    False, result.message + f"\nIncomplete recording retained for recovery: {self.video_tmp}",
                    result.path, result.elapsed_seconds, result.audio_error, result.saved_text)
            if self.log_path and self.log_path.exists():
                result = RecordingResult(
                    False, result.message + f"\n\nDiagnostic log: {self.log_path}",
                    result.path, result.elapsed_seconds, result.audio_error, result.saved_text)
            if self.audio_log_path and self.audio_log_path.exists():
                result = RecordingResult(
                    False, result.message + f"\nAudio diagnostic log: {self.audio_log_path}",
                    result.path, result.elapsed_seconds, result.audio_error, result.saved_text)
        self.process = None
        self.audio_process = None
        self.audio_thread = None
        self.record_thread = None
        self.stopping = False
        self.finalizing = False
        self.audio_active = False
        self.on_finished(result)
