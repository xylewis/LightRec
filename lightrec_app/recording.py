"""Owns FFmpeg processes, fallback paths, and final MP4 assembly."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import queue
import shutil
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
    continue_when_locked: bool = True


@dataclass(frozen=True)
class RecordingResult:
    success: bool
    message: str
    path: Path | None
    elapsed_seconds: int
    audio_error: str | None
    saved_text: str
    interrupted: bool = False


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
        self.diagnostic_path: Path | None = None
        self.concat_path: Path | None = None
        self.unexpected_exit = False
        self.stop_reason = ""
        self.heartbeat_id = None
        self.video_segments: list[Path] = []
        self.video_segment_durations: dict[Path, float] = {}
        self.segment_started_at: float | None = None
        self.segment_index = 0
        self.lock_image_path: Path | None = None
        self.locked = False
        self.transitioning = False
        self.pending_lock_state: bool | None = None
        self.pending_stop_reason: str | None = None
        self.capture_idx = 0
        self.audio_active = False
        self.audio_runtime_error: str | None = None
        self.stopping = False
        self.finalizing = False
        self.unexpected_exit = False
        self.stop_reason = ""
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
        self.unexpected_exit = False
        self.stop_reason = ""
        self.video_segments.clear()
        self.video_segment_durations.clear()
        self.segment_started_at = None
        self.segment_index = 0
        self.locked = False
        self.transitioning = False
        self.pending_lock_state = None
        self.pending_stop_reason = None
        while not self.audio_errors.empty():
            self.audio_errors.get_nowait()

        stamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S-%f")
        folder = options.output_dir
        self.final_path = folder / f"Screen_{stamp}.mp4"
        self.video_tmp = self._new_segment_path()
        self.audio_tmp = folder / f".Screen_{stamp}.audio.m4a"
        log_folder = folder / "LightRec Logs"
        try:
            log_folder.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            return f"无法创建诊断日志文件夹：{log_folder}\n{exc}"
        self.audio_log_path = log_folder / f"Screen_{stamp}.audio.ffmpeg.log"
        self.log_path = log_folder / f"Screen_{stamp}.ffmpeg.log"
        self.diagnostic_path = log_folder / f"Screen_{stamp}.diagnostic.log"
        self.lock_image_path = log_folder / f"Screen_{stamp}.locked.png"
        self.concat_path = log_folder / f"Screen_{stamp}.concat.txt"
        self._log_event("录制请求", f"编码器={encoder}; 显示器={options.display}; FPS={options.fps}; 码率={options.total_bitrate}kbps; 编码格式={options.codec}; 跳过静止帧={options.skip_unchanged}; 音频={'开启' if options.audio_enabled else '关闭'}; 音频来源={options.audio_source}; 输出={self.final_path}")

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
                self._log_event("启动失败", f"无法创建音频 FFmpeg 进程: {exc}")
                return f"{exc}\n录制诊断日志：{self.diagnostic_path}"
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
        video_args = self._video_args(encoder, video_bitrate, duplicate_frames, self.direct_d3d11)
        try:
            with open(self.log_path, "wb") as log:
                self.process = subprocess.Popen(
                    video_args,
                    stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=log,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
        except OSError as exc:
            self._log_event("启动失败", f"无法创建视频 FFmpeg 进程: {exc}")
            self._stop_audio_encoder()
            if self.audio_tmp:
                self.audio_tmp.unlink(missing_ok=True)
            return f"{exc}\n录制诊断日志：{self.diagnostic_path}"

        self.started_at = time.perf_counter()
        self.segment_started_at = self.started_at
        self._log_event("录制开始", f"视频 PID={self.process.pid}; 编码器={encoder}; 命令={subprocess.list2cmdline(video_args)}; FFmpeg日志={self.log_path}; 音频日志={self.audio_log_path if self.audio_active else '无'}")
        self._schedule_heartbeat()
        self.on_started(encoder)
        self.record_thread = threading.Thread(target=self._watch_encoder,
                                              name="lightrec-video", daemon=True)
        self.record_thread.start()
        return None

    def stop(self, reason: str = "用户请求") -> None:
        if self.process is None or self.finalizing:
            return
        if self.transitioning:
            self.pending_stop_reason = reason
            return
        self.stopping = True
        self.finalizing = True
        self.stop_reason = reason
        self._log_event("停止请求", reason)
        self.on_stop_signal(True)
        try:
            if self.process.stdin:
                self.process.stdin.write(b"q\n")
                self.process.stdin.flush()
        except (BrokenPipeError, OSError):
            pass
        threading.Thread(target=self._finish_after_encoder,
                         name="lightrec-finalize", daemon=True).start()

    def set_locked(self, locked: bool) -> None:
        if not self.process or self.finalizing or self.locked == locked:
            return
        if self.transitioning:
            self.pending_lock_state = locked
            return
        self._log_event("检测到锁屏" if locked else "检测到解锁", "")
        if not self.options or not self.options.continue_when_locked:
            if locked:
                self.stop("检测到 Windows 锁屏；锁屏继续录制已关闭")
            return
        self.locked = locked
        self.transitioning = True
        process = self.process
        threading.Thread(target=self._switch_lock_segment, args=(process, locked),
                         name="lightrec-lock-transition", daemon=True).start()

    def _switch_lock_segment(self, process: subprocess.Popen, locked: bool) -> None:
        try:
            if process.stdin and not process.stdin.closed:
                process.stdin.write(b"q\n")
                process.stdin.flush()
        except (BrokenPipeError, OSError):
            pass
        try:
            process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        if self.video_tmp and self.video_tmp.exists() and self.video_tmp.stat().st_size:
            self._record_current_segment()
        else:
            self.root.after(0, lambda: self._schedule_failure("锁屏切换时当前视频片段未能保存。"))
            return
        if locked and not self._prepare_lock_image(self.video_tmp):
            self.root.after(0, lambda: self._schedule_failure("无法生成锁屏黑屏占位画面；请查看诊断日志。"))
            return
        self.root.after(0, lambda: self._resume_after_lock_transition(locked))

    def _prepare_lock_image(self, segment: Path) -> bool:
        if not self.options or not self.lock_image_path:
            return False
        try:
            result = subprocess.run(
                [self.options.ffmpeg, "-hide_banner", "-nostdin", "-y", "-loglevel", "error",
                 "-i", str(segment), "-frames:v", "1", "-update", "1", str(self.lock_image_path)],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                timeout=30, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            if result.returncode:
                self._log_event("锁屏占位帧提取失败", result.stderr.decode("utf-8", errors="replace"))
                return False
            from PIL import Image
            with Image.open(self.lock_image_path) as image:
                black = Image.new("RGB", image.size, "black")
            black.save(self.lock_image_path)
            return True
        except Exception as exc:
            self._log_event("锁屏占位帧生成失败", str(exc))
            return False

    def _resume_after_lock_transition(self, locked: bool) -> None:
        if self.finalizing or not self.options or not self.current_encoder:
            self.transitioning = False
            return
        self.video_tmp = self._new_segment_path()
        audio_bitrate = self.options.audio_bitrate if self.audio_active else 0
        bitrate = video_bitrate_for(self.options.total_bitrate, audio_bitrate)
        duplicates = 0 if self.options.skip_unchanged else 1
        self._restart_process(
            self.current_encoder, bitrate, duplicates,
            "锁屏切换后视频进程无法重新启动：", append_log=True,
            still_image_path=self.lock_image_path if locked else None,
        )
        self.transitioning = False
        self._log_event("锁屏片段已开始" if locked else "桌面捕获已恢复",
                        f"片段={self.video_tmp}; 视频 PID={self.process.pid if self.process else '无'}")
        self.on_status("锁屏期间使用黑屏占位，录制仍在继续" if locked else "已解锁，桌面录制已恢复")
        stop_reason = self.pending_stop_reason
        self.pending_stop_reason = None
        pending_lock = self.pending_lock_state
        self.pending_lock_state = None
        if stop_reason:
            self.root.after(0, lambda: self.stop(stop_reason))
        elif pending_lock is not None and pending_lock != self.locked:
            self.root.after(0, lambda: self.set_locked(pending_lock))

    def take_audio_error(self) -> str | None:
        try:
            return self.audio_errors.get_nowait()
        except queue.Empty:
            return None

    def _video_args(self, encoder: str, video_bitrate: int,
                    duplicate_frames: int, direct: bool,
                    still_image_path: Path | None = None) -> list[str]:
        assert self.options and self.video_tmp
        return build_video_args(
            ffmpeg=self.options.ffmpeg, encoder=encoder, fps=self.options.fps,
            video_bitrate=video_bitrate, duplicate_frames=duplicate_frames,
            display=self.options.display, region=self.options.region,
            output_path=self.video_tmp, direct=direct,
            still_image_path=still_image_path,
        )

    def _new_segment_path(self) -> Path:
        assert self.final_path is not None
        self.segment_index += 1
        return self.final_path.with_name(
            f".{self.final_path.stem}.segment-{self.segment_index:03d}.mp4")

    def _record_current_segment(self) -> None:
        if not self.video_tmp or self.video_tmp in self.video_segments:
            return
        self.video_segments.append(self.video_tmp)
        started = self.segment_started_at or time.monotonic()
        self.video_segment_durations[self.video_tmp] = max(0.04, time.monotonic() - started)

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
        self.root.after(0, lambda: self._encoder_exited(code, proc))

    def _restart_compat_path(self, failed_process: subprocess.Popen, code: int) -> None:
        if self.process is not failed_process or self.stopping or not self.direct_d3d11:
            return
        self.direct_d3d11 = False
        self._log_event("切换录制路径", f"Direct D3D11 失败，退出码={code}；改用兼容路径")
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
        self._log_event("切换编码器", f"{previous} 启动失败，退出码={code}；切换到 {encoder}")
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
                         error_prefix: str, append_log: bool = False,
                         still_image_path: Path | None = None) -> None:
        assert self.log_path
        try:
            with open(self.log_path, "ab" if append_log else "wb") as log:
                self.process = subprocess.Popen(
                    self._video_args(encoder, bitrate, duplicates, direct=False,
                                     still_image_path=still_image_path),
                    stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=log,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
        except OSError as exc:
            self._schedule_failure(error_prefix + str(exc))
            return
        self.segment_started_at = time.monotonic()
        self.record_thread = threading.Thread(target=self._watch_encoder,
                                              name="lightrec-video", daemon=True)
        self.record_thread.start()

    def _schedule_failure(self, message: str) -> None:
        self._log_event("录制失败", message)
        def finish_failure() -> None:
            self._stop_audio_encoder()
            result = RecordingResult(False, message, self.final_path, self._elapsed(),
                                     self.audio_runtime_error, "")
            self.root.after(0, lambda: self._complete(result))
        threading.Thread(target=finish_failure, name="lightrec-cleanup", daemon=True).start()

    def _encoder_exited(self, code: int, process: subprocess.Popen) -> None:
        if self.process is not process or self.stopping or self.transitioning:
            return
        if code != 0 and self.options and self.options.continue_when_locked:
            try:
                if self.root._is_desktop_locked():
                    self.set_locked(True)
                    return
            except Exception:
                pass
        self.unexpected_exit = True
        self._log_event("录制进程意外退出", f"FFmpeg退出码={code}; 已录制={self._elapsed()}秒; PID={self.process.pid}; FFmpeg日志={self.log_path}")
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
        self._log_event("进程收尾", f"视频退出码={proc.returncode}; 音频退出码={self.audio_process.returncode if self.audio_process else '未启动'}; 已录制={self._elapsed()}秒")
        if proc.returncode == 0 and self.video_tmp and self.video_tmp.exists():
            self._record_current_segment()
        if self.audio_active and not self.audio_started.is_set():
            self.audio_active = False
        audio_error = self.take_audio_error()
        if proc.returncode != 0:
            self._log_event("保存失败", f"视频 FFmpeg 非零退出: {proc.returncode}")
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
            interrupted = bool(getattr(self, "unexpected_exit", False))
            if interrupted:
                message += f"\n\n录制进程提前结束（FFmpeg 退出码 {proc.returncode}）。诊断日志：\n{self.diagnostic_path}"
            if self.audio_runtime_error:
                message += f"\n\n视频已保存，但没有声音。{self.audio_runtime_error}"
            result = RecordingResult(True, message, self.final_path, self._elapsed(),
                                     self.audio_runtime_error, self._saved_text(), interrupted)
            self._log_event("录制完成", f"结果=已保存; 时长={self._elapsed()}秒; 文件={self.final_path}")
        except Exception as exc:
            self._log_event("保存失败", str(exc))
            result = RecordingResult(False, f"无法完成视频保存：{exc}", self.final_path,
                                     self._elapsed(), self.audio_runtime_error, "")
        self.root.after(0, lambda result=result: self._complete(result))

    def _mux_or_move(self) -> None:
        assert self.video_tmp and self.final_path and self.options and self.log_path
        segments = self.video_segments or [self.video_tmp]
        if len(segments) > 1:
            assert self.concat_path is not None
            rows = ["ffconcat version 1.0"]
            for segment in segments:
                escaped = segment.resolve().as_posix().replace("'", "'\\''")
                rows.append(f"file '{escaped}'")
                rows.append(f"duration {self.video_segment_durations.get(segment, 0.04):.6f}")
            self.concat_path.write_text("\n".join(rows) + "\n", encoding="utf-8")
            video_input = ["-f", "concat", "-safe", "0", "-i", str(self.concat_path)]
        else:
            video_input = ["-i", str(segments[0])]
        if self.audio_active and self.audio_tmp and self.audio_tmp.exists():
            audio_start = self.audio_clock[0]
            video_start = self.started_at
            offset = (video_start or time.perf_counter()) - (audio_start or time.perf_counter())
            args = [self.options.ffmpeg, "-hide_banner", "-nostdin", "-y", "-loglevel", "error",
                    *video_input]
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
        else:
            args = [self.options.ffmpeg, "-hide_banner", "-nostdin", "-y", "-loglevel", "error",
                    *video_input, "-map", "0:v:0", "-c", "copy",
                    "-movflags", "+faststart", str(self.final_path)]
            with open(self.log_path, "ab") as log:
                done = subprocess.run(args, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                      stderr=log, timeout=90,
                                      creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            if done.returncode:
                raise RuntimeError(f"FFmpeg finalize failed (exit {done.returncode}). Log: {self.log_path}")
        for segment in segments:
            segment.unlink(missing_ok=True)
        if self.concat_path:
            self.concat_path.unlink(missing_ok=True)
        if self.lock_image_path:
            self.lock_image_path.unlink(missing_ok=True)

    def _elapsed(self) -> int:
        return int(time.perf_counter() - self.started_at) if self.started_at else 0

    def _saved_text(self) -> str:
        elapsed = self._elapsed()
        text = f"已保存：{self.final_path} · {elapsed // 60}:{elapsed % 60:02d}"
        if self.audio_runtime_error:
            text += f" · 声音录制失败：{self.audio_runtime_error}"
        if self.diagnostic_path:
            text += f" · 诊断日志：{self.diagnostic_path}"
        return text

    def _log_event(self, event: str, details: str = "") -> None:
        if not self.diagnostic_path:
            return
        try:
            with self.diagnostic_path.open("a", encoding="utf-8") as log:
                timestamp = datetime.now().astimezone().isoformat(timespec="seconds")
                log.write(f"{timestamp} | {event}" + (f" | {details}" if details else "") + "\n")
        except OSError:
            pass

    def _schedule_heartbeat(self) -> None:
        if self.process is not None and not self.finalizing:
            self.heartbeat_id = self.root.after(60_000, self._write_heartbeat)

    def _write_heartbeat(self) -> None:
        self.heartbeat_id = None
        if self.process is None or self.finalizing:
            return
        try:
            free_bytes = shutil.disk_usage(self.options.output_dir).free if self.options else -1
        except OSError as exc:
            free_bytes = f"不可用({exc})"
        self._log_event("录制心跳", f"已录制={self._elapsed()}秒; 视频进程运行={self.process.poll() is None}; 音频进程运行={self.audio_process is not None and self.audio_process.poll() is None}; 输出盘剩余={free_bytes}字节")
        self._schedule_heartbeat()

    def _complete(self, result: RecordingResult) -> None:
        if not result.success:
            retained_segments = [path for path in self.video_segments if path.exists()]
            if self.video_tmp and self.video_tmp.exists() and self.video_tmp not in retained_segments:
                retained_segments.append(self.video_tmp)
            if retained_segments:
                result = RecordingResult(
                    False, result.message + "\n未完成的视频片段保留在：" + ", ".join(map(str, retained_segments)),
                    result.path, result.elapsed_seconds, result.audio_error, result.saved_text)
            if self.log_path and self.log_path.exists():
                result = RecordingResult(
                    False, result.message + f"\n\nDiagnostic log: {self.log_path}",
                    result.path, result.elapsed_seconds, result.audio_error, result.saved_text)
            if self.audio_log_path and self.audio_log_path.exists():
                result = RecordingResult(
                    False, result.message + f"\nAudio diagnostic log: {self.audio_log_path}",
                    result.path, result.elapsed_seconds, result.audio_error, result.saved_text)
            if self.diagnostic_path and self.diagnostic_path.exists():
                result = RecordingResult(
                    False, result.message + f"\n\n录制诊断日志：{self.diagnostic_path}",
                    result.path, result.elapsed_seconds, result.audio_error, result.saved_text)
        self._log_event("会话结束", f"成功={result.success}; 时长={result.elapsed_seconds}秒")
        if self.heartbeat_id is not None:
            try:
                self.root.after_cancel(self.heartbeat_id)
            except Exception:
                pass
            self.heartbeat_id = None
        self.process = None
        self.audio_process = None
        self.audio_thread = None
        self.record_thread = None
        self.stopping = False
        self.finalizing = False
        self.audio_active = False
        self.on_finished(result)
