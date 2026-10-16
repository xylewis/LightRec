"""Portable Windows 10/11 screen recorder front-end for FFmpeg."""

from __future__ import annotations

import datetime as dt
import ctypes
from ctypes import wintypes
import json
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
import tkinter as tk
from tkinter import colorchooser, filedialog, messagebox, ttk


APP_NAME = "LightRec"
FPS_CHOICES = (8, 10, 15, 20, 24, 30, 60)
AUDIO_BITRATE_CHOICES = (96, 128, 160, 192)
TOTAL_BITRATE_CHOICES = (800, 1000, 1200, 1400, 1600, 1800, 2000, 2500, 3000)
DEFAULT_HOTKEYS = {
    "start": "Ctrl+Shift+F9",
    "stop": "Ctrl+Shift+F10",
    "click_through": "Ctrl+Shift+F11",
}


def config_path() -> Path:
    appdata = os.environ.get("APPDATA")
    base = Path(appdata) if appdata else Path.home() / "AppData" / "Roaming"
    return base / APP_NAME / "settings.json"


def read_settings() -> dict:
    try:
        value = json.loads(config_path().read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError, TypeError):
        return {}


def parse_hotkey(binding: str) -> tuple[int, int] | None:
    """Return RegisterHotKey flags and virtual key for a user chord."""
    parts = [part.strip().upper() for part in binding.split("+") if part.strip()]
    if not parts:
        return None
    modifiers = {"CTRL": 0x0002, "CONTROL": 0x0002, "ALT": 0x0001, "SHIFT": 0x0004}
    flags = 0x4000  # MOD_NOREPEAT
    keys = []
    for part in parts:
        if part in modifiers:
            bit = modifiers[part]
            if flags & bit:
                return None
            flags |= bit
        else:
            keys.append(part)
    if len(keys) != 1:
        return None
    key = keys[0]
    has_ctrl_or_alt = bool(flags & (0x0001 | 0x0002))
    if not has_ctrl_or_alt and (flags & 0x0004 or not re.fullmatch(r"F(?:[1-9]|1[0-2])", key)):
        return None
    if len(key) == 1 and key.isalpha():
        virtual_key = ord(key)
    elif len(key) == 1 and key.isdigit():
        virtual_key = ord(key)
    elif re.fullmatch(r"F(?:[1-9]|1[0-9]|2[0-4])", key):
        virtual_key = 0x70 + int(key[1:]) - 1
    else:
        virtual_key = {
            "SPACE": 0x20, "INSERT": 0x2D, "DELETE": 0x2E,
            "HOME": 0x24, "END": 0x23, "PAGEUP": 0x21,
            "PAGEDOWN": 0x22, "PAUSE": 0x13,
        }.get(key, 0)
    if not virtual_key:
        return None
    ordered = []
    if flags & 0x0002:
        ordered.append("Ctrl")
    if flags & 0x0001:
        ordered.append("Alt")
    if flags & 0x0004:
        ordered.append("Shift")
    ordered.append(key if len(key) == 1 else key.title())
    return flags, virtual_key


def normalize_hotkey(binding: str) -> str | None:
    parsed = parse_hotkey(binding)
    if not parsed:
        return None
    flags, key = parsed
    names = []
    if flags & 0x0002:
        names.append("Ctrl")
    if flags & 0x0001:
        names.append("Alt")
    if flags & 0x0004:
        names.append("Shift")
    if 0x41 <= key <= 0x5A or 0x30 <= key <= 0x39:
        names.append(chr(key))
    elif 0x70 <= key <= 0x87:
        names.append(f"F{key - 0x70 + 1}")
    else:
        names.append({0x20: "Space", 0x2D: "Insert", 0x2E: "Delete", 0x24: "Home",
                      0x23: "End", 0x21: "Pageup", 0x22: "Pagedown", 0x13: "Pause"}[key])
    return "+".join(names)


def find_ffmpeg() -> str:
    # In a packaged build, prefer the copy shipped beside LightRec.exe so the
    # portable folder works without relying on a user-specific PATH entry.
    if getattr(sys, "frozen", False):
        extracted = Path(getattr(sys, "_MEIPASS", Path(sys.executable).resolve().parent)) / "ffmpeg.exe"
        if extracted.is_file():
            return str(extracted)
        app_dir = Path(sys.executable).resolve().parent
    else:
        app_dir = Path(__file__).resolve().parent
    bundled = app_dir / "ffmpeg.exe"
    if bundled.is_file():
        return str(bundled)
    return shutil.which("ffmpeg") or ""


def ffmpeg_has_encoder(ffmpeg: str, name: str) -> bool:
    try:
        proc = subprocess.run(
            [ffmpeg, "-hide_banner", "-encoders"], capture_output=True, text=True,
            timeout=8, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return proc.returncode == 0 and name in proc.stdout
    except (OSError, subprocess.SubprocessError):
        return False


def probe_encoder(ffmpeg: str, encoder: str) -> bool:
    """Check that a compiled encoder can actually initialize on this PC."""
    try:
        args = [ffmpeg, "-hide_banner", "-loglevel", "error", "-f", "lavfi",
                "-i", "color=size=640x360:rate=1,format=nv12", "-frames:v", "1", "-an",
                "-c:v", encoder, "-f", "null", "NUL"]
        if encoder.endswith("_amf"):
            args[args.index("-c:v"):args.index("-c:v")] = ["-usage", "transcoding", "-quality", "quality"]
        elif encoder.endswith("_qsv"):
            args[args.index("-c:v"):args.index("-c:v")] = ["-preset", "veryfast"]
        elif encoder.endswith("_nvenc"):
            args[args.index("-c:v"):args.index("-c:v")] = ["-preset", "p4"]
        elif encoder.endswith("_mf"):
            args[args.index("-c:v"):args.index("-c:v")] = ["-hw_encoding", "1", "-rate_control", "cbr"]
        elif encoder == "libx264":
            args[args.index("-c:v"):args.index("-c:v")] = ["-preset", "veryfast"]
        elif encoder == "libx265":
            args[args.index("-c:v"):args.index("-c:v")] = ["-preset", "ultrafast"]
        proc = subprocess.run(args, capture_output=True, text=True, timeout=15,
                              creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return proc.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def choose_encoder(ffmpeg: str, hevc: bool, after: str | None = None) -> str | None:
    candidates = ("hevc_amf", "hevc_qsv", "hevc_nvenc", "hevc_mf", "libx265") if hevc else (
        "h264_amf", "h264_qsv", "h264_nvenc", "h264_mf", "libx264")
    if after in candidates:
        candidates = candidates[candidates.index(after) + 1:]
    return next((name for name in candidates
                 if ffmpeg_has_encoder(ffmpeg, name) and probe_encoder(ffmpeg, name)), None)


def encoder_label(encoder: str | None) -> str:
    if not encoder:
        return "未知编码器"
    backend = ("AMD AMF" if encoder.endswith("_amf") else
               "Intel QSV" if encoder.endswith("_qsv") else
               "NVIDIA NVENC" if encoder.endswith("_nvenc") else
               "Windows MF" if encoder.endswith("_mf") else "CPU")
    codec = "HEVC" if encoder.startswith(("hevc_", "libx265")) else "H.264"
    return f"{backend} {codec}"


def video_bitrate_for(total_bitrate: int, audio_bitrate: int) -> int:
    return max(200, total_bitrate - audio_bitrate)


def initialize_audio_com() -> bool:
    """Initialize COM for the current WASAPI worker thread, if needed."""
    if os.name != "nt":
        return False
    ole32 = ctypes.OleDLL("ole32")
    co_initialize = ole32.CoInitializeEx
    co_initialize.argtypes = (ctypes.c_void_p, ctypes.c_uint32)
    co_initialize.restype = ctypes.c_long
    hr = co_initialize(None, 0)  # COINIT_MULTITHREADED
    if hr >= 0:
        return True
    if hr == ctypes.c_long(0x80010106).value:  # RPC_E_CHANGED_MODE
        return False  # This thread already has a COM apartment.
    raise OSError(f"COM 初始化失败 (0x{hr & 0xFFFFFFFF:08x})")


def uninitialize_audio_com(initialized: bool) -> None:
    if initialized and os.name == "nt":
        ctypes.OleDLL("ole32").CoUninitialize()


def ffmpeg_has_filter(ffmpeg: str, name: str) -> bool:
    try:
        proc = subprocess.run(
            [ffmpeg, "-hide_banner", "-filters"], capture_output=True, text=True,
            timeout=8, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return proc.returncode == 0 and name in proc.stdout
    except (OSError, subprocess.SubprocessError):
        return False


def capture_audio(encoder: subprocess.Popen, stop: threading.Event, started: threading.Event,
                  clock: list[float | None], errors: queue.Queue[str], source: str) -> None:
    """Capture the selected Windows audio source(s) and send stereo PCM to AAC."""
    com_initialized = False
    try:
        import numpy as np
        first_soundcard_import = os.name == "nt" and "soundcard.mediafoundation" not in sys.modules
        if not first_soundcard_import:
            com_initialized = initialize_audio_com()
        import soundcard as sc

        # SoundCard initializes a module-global COM token on whichever thread
        # imports it first. On the first recording this is the current capture
        # thread; Windows releases that apartment when the thread exits. Mark
        # the module token as borrowed so it cannot uninitialize COM later on
        # the GUI thread during interpreter shutdown. Later capture/readers
        # initialize their own apartments explicitly.
        if first_soundcard_import:
            from soundcard import mediafoundation
            if getattr(mediafoundation._com, "com_loaded", False):
                mediafoundation._com.com_loaded = False

        recorders = []
        if source in ("电脑声音", "电脑声音 + 麦克风"):
            speaker = sc.default_speaker()
            if speaker is None:
                raise RuntimeError("Windows 没有默认扬声器设备。")
            loopback = sc.get_microphone(id=speaker.name, include_loopback=True)
            if loopback is None:
                raise RuntimeError("无法打开默认扬声器的系统声音回环输入。")
            recorders.append(loopback)
        if source in ("麦克风", "电脑声音 + 麦克风"):
            microphone = sc.default_microphone()
            if microphone is None:
                raise RuntimeError("Windows 没有默认麦克风设备。")
            recorders.append(microphone)

        if not recorders:
            raise RuntimeError("请先选择电脑声音、麦克风或两者作为声音来源。")
        if encoder.stdin is None:
            raise RuntimeError("The AAC encoder input pipe is unavailable.")

        chunks = [queue.Queue(maxsize=8) for _ in recorders]
        ready: queue.Queue[Exception | None] = queue.Queue()
        reader_threads = []

        def read_source(device, output: queue.Queue) -> None:
            initialized = False
            reader_com_initialized = False
            try:
                reader_com_initialized = initialize_audio_com()
                # Keep each soundcard recorder on its own thread. Reading two
                # devices serially halves the effective audio rate.
                with device.recorder(samplerate=48_000, channels=2) as recorder:
                    ready.put(None)
                    initialized = True
                    while not stop.is_set():
                        samples = recorder.record(numframes=4_800)
                        while not stop.is_set():
                            try:
                                output.put(samples, timeout=0.2)
                                break
                            except queue.Full:
                                continue
            except Exception as exc:
                if not initialized:
                    ready.put(exc)
                else:
                    errors.put(str(exc))
            finally:
                uninitialize_audio_com(reader_com_initialized)

        for device, output in zip(recorders, chunks):
            reader = threading.Thread(target=read_source, args=(device, output), daemon=True)
            reader.start()
            reader_threads.append(reader)
        for _ in recorders:
            deadline = time.monotonic() + 5
            while not stop.is_set():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise RuntimeError("等待声音设备启动超时。")
                try:
                    issue = ready.get(timeout=min(0.1, remaining))
                    break
                except queue.Empty:
                    continue
            else:
                return
            if issue:
                raise issue

        clock[0] = time.perf_counter()
        started.set()
        pending: list[object | None] = [None] * len(chunks)
        while not stop.is_set():
            for index, (source_chunks, reader) in enumerate(zip(chunks, reader_threads)):
                if pending[index] is not None:
                    continue
                try:
                    pending[index] = source_chunks.get(timeout=0.2)
                except queue.Empty:
                    # Shutdown intentionally ends the reader thread. Do not
                    # report that normal stop race as a failed audio source.
                    if stop.is_set():
                        break
                    # The reader may have exited after reporting the real
                    # device error. Drain that queue before converting the
                    # failure into the less useful generic "stopped" error.
                    if not errors.empty():
                        raise RuntimeError(errors.get_nowait())
                    if not reader.is_alive():
                        raise RuntimeError("一个声音来源提前停止。")
                    break
            if stop.is_set():
                break
            if any(samples is None for samples in pending):
                continue
            samples_list = pending
            pending = [None] * len(chunks)
            frames = min(len(samples) for samples in samples_list)
            samples = sum(samples[:frames] for samples in samples_list) / len(samples_list)
            pcm = (np.clip(samples, -1.0, 1.0) * 32767).astype("<i2", copy=False)
            encoder.stdin.write(pcm.tobytes())
            encoder.stdin.flush()

    except ModuleNotFoundError as exc:
        errors.put(f"缺少声音组件 {exc.name}。请在 LightRec 文件夹运行：py -3 -m pip install -r requirements-audio.txt")
        started.set()
    except Exception as exc:  # Surface audio setup/capture failures in the UI.
        errors.put(str(exc))
        started.set()
    finally:
        stop.set()
        for reader in locals().get("reader_threads", ()):
            if reader.is_alive():
                reader.join(timeout=1)
        if encoder.stdin and not encoder.stdin.closed:
            try:
                encoder.stdin.close()
            except OSError:
                pass
        uninitialize_audio_com(com_initialized)


class LightRec(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title(APP_NAME)
        self.geometry("560x455")
        self.minsize(500, 410)
        self.resizable(True, True)

        saved = read_settings()
        def saved_choice(name: str, choices, default):
            value = saved.get(name, default)
            return value if value in choices else default
        def saved_int(name: str, choices, default: int) -> int:
            try:
                value = int(saved.get(name, default))
            except (TypeError, ValueError):
                return default
            return value if value in choices else default
        def saved_color(name: str, default: str) -> str:
            value = saved.get(name, default)
            return value if isinstance(value, str) and re.fullmatch(r"#[0-9A-Fa-f]{6}", value) else default

        self.ffmpeg = tk.StringVar(value=find_ffmpeg())
        self.ffmpeg_is_bundled = bool(getattr(sys, "frozen", False) and
                                      (Path(getattr(sys, "_MEIPASS", "")) / "ffmpeg.exe").is_file())
        self.output_dir = tk.StringVar(value=str(saved.get("output_dir") or Path.home() / "Videos" / APP_NAME))
        self.codec = tk.StringVar(value=saved_choice("codec", ("HEVC · 文件较小", "H.264 · 兼容性较好"), "HEVC · 文件较小"))
        self.fps = tk.IntVar(value=saved_int("fps", FPS_CHOICES, 20))
        self.target_total_bitrate = tk.IntVar(value=saved_int("target_total_bitrate", TOTAL_BITRATE_CHOICES, 1400))
        self.display = tk.IntVar(value=saved_int("display", tuple(range(16)), 0))
        self.region: tuple[int, int, int, int] | None = None
        self.click_through = tk.BooleanVar(value=bool(saved.get("click_through", False)))
        self.skip_unchanged = tk.BooleanVar(value=bool(saved.get("skip_unchanged", False)))
        self.audio = tk.BooleanVar(value=bool(saved.get("audio", True)))
        self.audio_source = tk.StringVar(value=saved_choice(
            "audio_source", ("电脑声音 + 麦克风", "电脑声音", "麦克风"), "电脑声音 + 麦克风"))
        self.audio_bitrate = tk.IntVar(value=saved_int("audio_bitrate", AUDIO_BITRATE_CHOICES, 128))
        self.edge_enabled = tk.BooleanVar(value=bool(saved.get("edge_enabled", True)))
        self.edge_start_color = tk.StringVar(value=saved_color("edge_start_color", "#20B878"))
        self.edge_stop_color = tk.StringVar(value=saved_color("edge_stop_color", "#D86767"))
        self.edge_opacity = tk.IntVar(value=saved_int("edge_opacity", tuple(range(5, 101)), 22))
        self.edge_thickness = tk.IntVar(value=saved_int("edge_thickness", tuple(range(1, 17)), 4))
        self.edge_gradient = tk.BooleanVar(value=bool(saved.get("edge_gradient", True)))
        self.edge_duration = tk.IntVar(value=saved_int("edge_duration", tuple(range(1, 11)), 2))
        saved_sides = saved.get("edge_sides", {})
        self.edge_sides = {
            name: tk.BooleanVar(value=bool(saved_sides.get(name, True)) if isinstance(saved_sides, dict) else True)
            for name in ("top", "bottom", "left", "right")
        }
        self.status = tk.StringVar(value="准备就绪")
        self.target_bitrate_text = tk.StringVar(value="")

        self.process: subprocess.Popen | None = None
        self.direct_d3d11 = False
        self.current_encoder: str | None = None
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
        self.close_when_done = False
        self.close_behavior = saved.get("close_behavior", "ask")
        if self.close_behavior not in ("ask", "tray", "exit"):
            self.close_behavior = "ask"
        self.tray_icon = None
        self.tray_thread: threading.Thread | None = None
        self.hotkey_ids = (0x4C01, 0x4C02, 0x4C03)
        configured_hotkeys = saved.get("hotkeys", {})
        self.hotkey_bindings = {}
        for action, default in DEFAULT_HOTKEYS.items():
            raw = configured_hotkeys.get(action, default) if isinstance(configured_hotkeys, dict) else default
            self.hotkey_bindings[action] = normalize_hotkey(raw) or default
        if len({parse_hotkey(value) for value in self.hotkey_bindings.values()}) != len(DEFAULT_HOTKEYS):
            self.hotkey_bindings = DEFAULT_HOTKEYS.copy()
        self.hotkeys_registered = False
        self.hotkey_failures: list[str] = []
        self.registered_hotkey_ids: list[int] = []
        self.hotkey_messages: queue.Queue[int] = queue.Queue()
        self.hotkey_ready = threading.Event()
        self.hotkey_ready_reported = False
        self.hotkey_thread: threading.Thread | None = None
        self.hotkey_thread_id: int | None = None
        self.region_cancel_callback = None
        self.edge_windows: list[tk.Toplevel] = []
        self.edge_after_id: str | None = None
        self.edge_color_labels: dict[str, ttk.Label] = {}
        self.settings_window: tk.Toplevel | None = None
        self.pending_hotkeys = self.hotkey_bindings.copy()
        self.encoder_cache: dict[tuple[str, bool], tuple[bool, str | None, str | None]] = {}
        self.encoder_probes: set[tuple[str, bool]] = set()
        self.encoder_results: queue.Queue[tuple[tuple[str, bool], bool, str | None, str | None]] = queue.Queue()
        self.pending_start = False
        self.pending_encoder_key: tuple[str, bool] | None = None

        self._build_ui()
        for variable in (self.output_dir, self.codec, self.fps, self.target_total_bitrate,
                         self.display, self.click_through, self.skip_unchanged, self.audio,
                         self.audio_source, self.audio_bitrate, self.edge_enabled,
                         self.edge_start_color, self.edge_stop_color, self.edge_opacity,
                         self.edge_thickness, self.edge_gradient, self.edge_duration,
                         *self.edge_sides.values()):
            variable.trace_add("write", self._settings_changed)
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self._register_hotkeys(wait=False)
        self.after(250, self._poll)
        # Let the main window paint before starting FFmpeg's filter and encoder
        # probes. The probe remains lazy and is also triggered by Record.
        self.after(900, lambda: self._ensure_encoder_probe(self.codec.get()))

    def _build_ui(self) -> None:
        root = ttk.Frame(self, padding=(22, 18))
        root.pack(fill="both", expand=True)

        header = ttk.Frame(root)
        header.pack(fill="x", pady=(0, 16))
        ttk.Label(header, text="LightRec", font=("Segoe UI", 19, "bold")).pack(side="left")
        self.settings_button = ttk.Button(header, text="设置…", command=self._open_settings)
        self.settings_button.pack(side="right")
        ttk.Label(root, text="一键录制屏幕，视频保存为 MP4。默认录制全屏、系统声和麦克风。",
                  foreground="#555", wraplength=490).pack(anchor="w", pady=(0, 14))

        capture = ttk.LabelFrame(root, text="录制内容", padding=(14, 10))
        capture.pack(fill="x", pady=(0, 12))
        range_row = ttk.Frame(capture)
        range_row.pack(fill="x", pady=(0, 9))
        self.region_text = tk.StringVar(value=self._fullscreen_label())
        ttk.Label(range_row, textvariable=self.region_text).pack(side="left", fill="x", expand=True)
        ttk.Button(range_row, text="选择区域…", command=self._select_region).pack(side="right")
        ttk.Button(range_row, text="全屏", command=self._clear_region).pack(side="right", padx=(0, 7))

        audio_row = ttk.Frame(capture)
        audio_row.pack(fill="x")
        ttk.Checkbutton(audio_row, text="录制声音", variable=self.audio).pack(side="left")
        ttk.Combobox(audio_row, textvariable=self.audio_source, state="readonly", width=21,
                     values=("电脑声音 + 麦克风", "电脑声音", "麦克风")).pack(side="left", padx=(8, 0))

        save_row = ttk.Frame(root)
        save_row.pack(fill="x", pady=(0, 10))
        ttk.Label(save_row, text="保存位置").pack(side="left")
        self.output_entry = ttk.Entry(save_row, textvariable=self.output_dir, state="readonly")
        self.output_entry.pack(side="left", fill="x", expand=True, padx=10)
        ttk.Button(save_row, text="打开", command=self._open_output_folder).pack(side="right", padx=(0, 6))
        ttk.Button(save_row, text="更改…", command=self._choose_folder).pack(side="right")

        actions = ttk.Frame(root)
        actions.pack(fill="x", pady=(5, 8))
        self.record_button = ttk.Button(actions, text="●  开始录制", command=self.start_recording)
        self.record_button.pack(side="left", ipadx=12, ipady=7)
        self.stop_button = ttk.Button(actions, text="■  停止并保存", command=self.stop_recording, state="disabled")
        self.stop_button.pack(side="left", padx=(9, 0), ipadx=8, ipady=7)
        ttk.Label(root, textvariable=self.status, wraplength=490).pack(anchor="w", pady=(3, 6))

        self.hotkey_summary = tk.StringVar()
        self._refresh_hotkey_summary()
        ttk.Label(root, textvariable=self.hotkey_summary, foreground="#555", wraplength=490).pack(anchor="w")
        ttk.Label(root, text="开始和停止时会短暂显示屏幕边缘提示，不会弹出完成窗口。",
                  foreground="#777", wraplength=490).pack(anchor="w", pady=(10, 0))

    def _fullscreen_label(self) -> str:
        return f"全屏 · 显示器 {int(self.display.get()) + 1}"

    def _refresh_hotkey_summary(self) -> None:
        if not hasattr(self, "hotkey_summary"):
            return
        summary = (f"快捷键：开始 {self.hotkey_bindings['start']}　停止 {self.hotkey_bindings['stop']}")
        if not self.hotkey_ready.is_set():
            summary += "　（正在注册快捷键…）"
        elif not self.hotkeys_registered:
            summary += "　（部分快捷键未注册，可在设置中修改）"
        self.hotkey_summary.set(summary)

    def _settings_changed(self, *_args) -> None:
        self._save_settings()
        if self.region is None and hasattr(self, "region_text"):
            try:
                self.region_text.set(self._fullscreen_label())
            except tk.TclError:
                pass

    def _save_settings(self, *_args) -> None:
        try:
            values = {
                "output_dir": self.output_dir.get(),
                "codec": self.codec.get(),
                "fps": int(self.fps.get()),
                "target_total_bitrate": int(self.target_total_bitrate.get()),
                "display": int(self.display.get()),
                "click_through": bool(self.click_through.get()),
                "skip_unchanged": bool(self.skip_unchanged.get()),
                "audio": bool(self.audio.get()),
                "audio_source": self.audio_source.get(),
                "audio_bitrate": int(self.audio_bitrate.get()),
                "hotkeys": self.hotkey_bindings,
                "close_behavior": self.close_behavior,
                "edge_enabled": bool(self.edge_enabled.get()),
                "edge_start_color": self.edge_start_color.get(),
                "edge_stop_color": self.edge_stop_color.get(),
                "edge_opacity": int(self.edge_opacity.get()),
                "edge_thickness": int(self.edge_thickness.get()),
                "edge_gradient": bool(self.edge_gradient.get()),
                "edge_duration": int(self.edge_duration.get()),
                "edge_sides": {name: bool(value.get()) for name, value in self.edge_sides.items()},
            }
            if not self.ffmpeg_is_bundled:
                values["ffmpeg_path"] = self.ffmpeg.get()
            path = config_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = path.with_suffix(".tmp")
            temporary.write_text(json.dumps(values, ensure_ascii=False, indent=2), encoding="utf-8")
            temporary.replace(path)
        except (OSError, tk.TclError, ValueError):
            pass

    def _open_settings(self) -> None:
        if self.settings_window and self.settings_window.winfo_exists():
            self.settings_window.lift()
            return
        window = tk.Toplevel(self)
        self.settings_window = window
        window.title(f"{APP_NAME} Settings")
        window.geometry("600x520")
        window.minsize(560, 470)
        window.transient(self)
        body = ttk.Frame(window, padding=16)
        body.pack(fill="both", expand=True)
        tabs = ttk.Notebook(body)
        tabs.pack(fill="both", expand=True)

        recording = ttk.Frame(tabs, padding=14)
        tabs.add(recording, text="录制设置")
        recording.columnconfigure(1, weight=1)
        self._target_bitrate_changed()
        ttk.Label(recording, text="视频格式").grid(row=0, column=0, sticky="w", pady=6)
        ttk.Combobox(recording, textvariable=self.codec, state="readonly",
                     values=("HEVC · 文件较小", "H.264 · 兼容性较好"), width=28).grid(
                         row=0, column=1, sticky="ew", padx=(12, 0), pady=6)
        ttk.Label(recording, text="帧率").grid(row=1, column=0, sticky="w", pady=6)
        ttk.Combobox(recording, textvariable=self.fps, state="readonly",
                     values=FPS_CHOICES, width=10).grid(row=1, column=1, sticky="w", padx=(12, 0), pady=6)
        ttk.Label(recording, text="目标总码率").grid(row=2, column=0, sticky="w", pady=6)
        bitrate_row = ttk.Frame(recording)
        bitrate_row.grid(row=2, column=1, sticky="ew", padx=(12, 0), pady=6)
        rate_box = ttk.Combobox(bitrate_row, textvariable=self.target_total_bitrate, state="readonly",
                                values=TOTAL_BITRATE_CHOICES, width=10)
        rate_box.pack(side="left")
        ttk.Label(bitrate_row, text=" kbps（含声音）").pack(side="left")
        rate_box.bind("<<ComboboxSelected>>", self._target_bitrate_changed)
        ttk.Label(recording, textvariable=self.target_bitrate_text, foreground="#666").grid(
            row=3, column=1, sticky="w", padx=(12, 0), pady=(0, 6))
        ttk.Label(recording, text="显示器").grid(row=4, column=0, sticky="w", pady=6)
        ttk.Spinbox(recording, from_=0, to=15, textvariable=self.display, width=8).grid(
            row=4, column=1, sticky="w", padx=(12, 0), pady=6)
        ttk.Label(recording, text="声音码率").grid(row=5, column=0, sticky="w", pady=6)
        ttk.Combobox(recording, textvariable=self.audio_bitrate, state="readonly",
                     values=AUDIO_BITRATE_CHOICES, width=10).grid(
                         row=5, column=1, sticky="w", padx=(12, 0), pady=6)
        ttk.Checkbutton(recording, text="跳过静止画面（输出可变帧率）",
                        variable=self.skip_unchanged).grid(row=6, column=0, columnspan=2,
                                                           sticky="w", pady=(8, 4))
        ttk.Checkbutton(recording, text="窗口穿透（点击穿过 LightRec 窗口）",
                        variable=self.click_through, command=self._apply_click_through).grid(
                            row=7, column=0, columnspan=2, sticky="w", pady=4)

        engine = ttk.LabelFrame(recording, text="录屏组件", padding=9)
        engine.grid(row=8, column=0, columnspan=2, sticky="ew", pady=(14, 4))
        if self.ffmpeg_is_bundled:
            ttk.Label(engine, text="FFmpeg 已包含在 LightRec.exe 中，无需安装或设置路径。",
                      wraplength=470).pack(anchor="w")
        else:
            ttk.Label(engine, text="便携版通常会自带 FFmpeg；此处仅用于开发版或修复缺失组件。",
                      wraplength=470).pack(anchor="w", pady=(0, 6))
            path_row = ttk.Frame(engine)
            path_row.pack(fill="x")
            ttk.Entry(path_row, textvariable=self.ffmpeg).pack(side="left", fill="x", expand=True)
            ttk.Button(path_row, text="浏览…", command=self._choose_ffmpeg).pack(side="left", padx=(8, 0))

        edge = ttk.Frame(tabs, padding=14)
        tabs.add(edge, text="描边提醒")
        edge.columnconfigure(1, weight=1)
        ttk.Checkbutton(edge, text="录制开始和停止时显示描边", variable=self.edge_enabled).grid(
            row=0, column=0, columnspan=3, sticky="w", pady=(0, 10))
        self.edge_color_labels = {}
        for row, (name, title, variable) in enumerate((
            ("start", "开始录制颜色", self.edge_start_color),
            ("stop", "停止录制颜色", self.edge_stop_color),
        ), start=1):
            ttk.Label(edge, text=title).grid(row=row, column=0, sticky="w", pady=5)
            color_label = ttk.Label(edge, text=variable.get(), width=12, anchor="center")
            color_label.grid(row=row, column=1, sticky="w", padx=(12, 4))
            self.edge_color_labels[name] = color_label
            ttk.Button(edge, text="选择颜色…", command=lambda n=name: self._choose_edge_color(n)).grid(
                row=row, column=2, sticky="e")
        ttk.Label(edge, text="不透明度").grid(row=3, column=0, sticky="w", pady=5)
        ttk.Scale(edge, from_=5, to=100, variable=self.edge_opacity, orient="horizontal").grid(
            row=3, column=1, sticky="ew", padx=(12, 6))
        ttk.Label(edge, textvariable=self.edge_opacity, width=4).grid(row=3, column=2, sticky="e")
        ttk.Label(edge, text="粗细").grid(row=4, column=0, sticky="w", pady=5)
        ttk.Spinbox(edge, from_=1, to=16, textvariable=self.edge_thickness, width=7).grid(
            row=4, column=1, sticky="w", padx=(12, 0))
        ttk.Label(edge, text="px").grid(row=4, column=2, sticky="w")
        ttk.Checkbutton(edge, text="两端渐隐（透明渐变）", variable=self.edge_gradient).grid(
            row=5, column=0, columnspan=3, sticky="w", pady=5)
        ttk.Label(edge, text="显示位置").grid(row=6, column=0, sticky="w", pady=5)
        sides = ttk.Frame(edge)
        sides.grid(row=6, column=1, columnspan=2, sticky="w", padx=(12, 0))
        for side, label in (("top", "上"), ("bottom", "下"), ("left", "左"), ("right", "右")):
            ttk.Checkbutton(sides, text=label, variable=self.edge_sides[side]).pack(side="left", padx=(0, 10))
        ttk.Label(edge, text="提示时长").grid(row=7, column=0, sticky="w", pady=5)
        ttk.Spinbox(edge, from_=1, to=10, textvariable=self.edge_duration, width=7).grid(
            row=7, column=1, sticky="w", padx=(12, 0))
        ttk.Label(edge, text="秒").grid(row=7, column=2, sticky="w")
        ttk.Button(edge, text="预览开始提示", command=self._preview_recording_edge).grid(
            row=8, column=0, columnspan=3, sticky="w", pady=(10, 0))

        hotkeys = ttk.Frame(tabs, padding=14)
        tabs.add(hotkeys, text="快捷键")
        ttk.Label(hotkeys, text="点击“录入”后按下快捷键：可用 Ctrl/Alt 组合键，也可单独使用 F1–F12。功能键可能与 Windows 或其他软件冲突；若无法注册，会在这里提示。",
                  wraplength=490).pack(anchor="w", pady=(0, 12))
        self.pending_hotkeys = self.hotkey_bindings.copy()
        self.hotkey_labels: dict[str, tk.StringVar] = {}
        for action, label in (("start", "开始录制"), ("stop", "停止并保存"),
                              ("click_through", "切换窗口穿透")):
            row = ttk.Frame(hotkeys)
            row.pack(fill="x", pady=6)
            ttk.Label(row, text=label, width=18).pack(side="left")
            value = tk.StringVar(value=self.pending_hotkeys[action])
            self.hotkey_labels[action] = value
            ttk.Label(row, textvariable=value, width=22).pack(side="left", padx=8)
            ttk.Button(row, text="录入…", command=lambda name=action: self._capture_hotkey(name)).pack(side="right")
        existing_conflicts = getattr(self, "hotkey_failures", [])
        initial_hotkey_status = (
            "当前未注册，可能被 Windows 或其他程序占用：" + "、".join(existing_conflicts)
            if existing_conflicts else ""
        )
        self.settings_hotkey_status = tk.StringVar(value=initial_hotkey_status)
        ttk.Label(hotkeys, textvariable=self.settings_hotkey_status, foreground="#9a4a00",
                  wraplength=490).pack(anchor="w", pady=(12, 0))

        footer = ttk.Frame(body)
        footer.pack(fill="x", pady=(12, 0))
        ttk.Label(footer, text="设置会自动保存在当前 Windows 用户下。", foreground="#666").pack(side="left")
        ttk.Button(footer, text="应用并关闭", command=self._apply_settings).pack(side="right")
        window.protocol("WM_DELETE_WINDOW", self._apply_settings)
        window.bind("<Escape>", lambda _event: self._apply_settings())

    def _choose_edge_color(self, name: str) -> None:
        variable = self.edge_start_color if name == "start" else self.edge_stop_color
        color = colorchooser.askcolor(color=variable.get(), parent=self.settings_window,
                                      title="选择描边颜色")[1]
        if color:
            variable.set(color.upper())
            label = self.edge_color_labels.get(name)
            if label:
                label.configure(text=color.upper())

    def _preview_recording_edge(self) -> None:
        self._save_settings()
        try:
            self.capture_idx = int(self.display.get())
        except (ValueError, tk.TclError):
            self.capture_idx = 0
        self._show_recording_edge(True)

    def _capture_hotkey(self, action: str) -> None:
        if not self.settings_window or not self.settings_window.winfo_exists():
            return
        self._unregister_hotkeys()
        dialog = tk.Toplevel(self.settings_window)
        dialog.title("录入快捷键")
        dialog.geometry("360x145")
        dialog.resizable(False, False)
        dialog.transient(self.settings_window)
        prompt = tk.StringVar(value="请按 Ctrl/Alt 组合键，或单独按 F1–F12；按 Esc 取消。")
        ttk.Label(dialog, textvariable=prompt, wraplength=310, justify="center").pack(
            fill="both", expand=True, padx=20, pady=18)
        held_modifiers: set[str] = set()

        def finish(restart: bool = True) -> None:
            try:
                dialog.grab_release()
            except tk.TclError:
                pass
            if dialog.winfo_exists():
                dialog.destroy()
            if restart and self.process is None:
                self._register_hotkeys()
                self._refresh_hotkey_summary()

        def key_pressed(event) -> str:
            if event.keysym == "Escape":
                finish()
                return "break"
            modifier_names = {
                "Control_L": "Ctrl", "Control_R": "Ctrl",
                "Shift_L": "Shift", "Shift_R": "Shift",
                "Alt_L": "Alt", "Alt_R": "Alt",
            }
            if event.keysym in modifier_names:
                held_modifiers.add(modifier_names[event.keysym])
                return "break"
            if os.name == "nt":
                get_key_state = ctypes.windll.user32.GetAsyncKeyState
                is_down = lambda vk: bool(get_key_state(vk) & 0x8000)
                modifiers = (["Ctrl"] if event.state & 0x0004 or is_down(0x11) else [])
                if "Alt" in held_modifiers or is_down(0x12):
                    modifiers.append("Alt")
                if event.state & 0x0001 or is_down(0x10):
                    modifiers.append("Shift")
            else:
                modifiers = list(dict.fromkeys(held_modifiers))
                if event.state & 0x0004 and "Ctrl" not in modifiers:
                    modifiers.append("Ctrl")
                if event.state & 0x0008 and "Alt" not in modifiers:
                    modifiers.append("Alt")
                if event.state & 0x0001 and "Shift" not in modifiers:
                    modifiers.append("Shift")
            key = {"space": "Space", "Prior": "Pageup", "Next": "Pagedown"}.get(
                event.keysym, event.keysym)
            candidate = normalize_hotkey("+".join((*modifiers, key)))
            if not candidate:
                prompt.set("快捷键无效。请使用 Ctrl/Alt 组合键，或单独使用 F1–F12。")
                return "break"
            self.pending_hotkeys[action] = candidate
            self.hotkey_labels[action].set(candidate)
            finish()
            return "break"

        def key_released(event) -> None:
            modifier_names = {
                "Control_L": "Ctrl", "Control_R": "Ctrl",
                "Shift_L": "Shift", "Shift_R": "Shift",
                "Alt_L": "Alt", "Alt_R": "Alt",
            }
            name = modifier_names.get(event.keysym)
            if name:
                held_modifiers.discard(name)

        dialog.bind("<KeyPress>", key_pressed)
        dialog.bind("<KeyRelease>", key_released)
        dialog.bind("<FocusOut>", lambda _event: finish())
        dialog.grab_set()
        dialog.focus_force()

    def _apply_settings(self) -> None:
        chords = [parse_hotkey(self.pending_hotkeys[action]) for action in DEFAULT_HOTKEYS]
        if None in chords or len(set(chords)) != len(chords):
            self.settings_hotkey_status.set("热键无效或重复，请为开始、停止和窗口穿透分别设置组合键。")
            return
        self._unregister_hotkeys()
        self.hotkey_bindings = self.pending_hotkeys.copy()
        self._register_hotkeys()
        self._save_settings()
        self._refresh_hotkey_summary()
        if self.hotkey_failures:
            self.settings_hotkey_status.set(
                "以下快捷键无法注册，可能被 Windows 或其他程序占用："
                + "、".join(self.hotkey_failures)
                + "。请换一组按键后再应用。"
            )
            self.status.set("有快捷键未能注册；请查看设置中的冲突提示。")
            return
        if self.settings_window and self.settings_window.winfo_exists():
            self.settings_window.destroy()
        self.settings_window = None

    def _register_hotkeys(self, wait: bool = True) -> None:
        if os.name != "nt":
            return
        self.hotkey_ready.clear()
        self.hotkey_ready_reported = False
        self.registered_hotkey_ids.clear()
        self.hotkey_thread = threading.Thread(target=self._hotkey_listener, daemon=True)
        self.hotkey_thread.start()
        if wait:
            self.hotkey_ready.wait(timeout=2)

    def _hotkey_listener(self) -> None:
        user32 = ctypes.windll.user32
        msg = wintypes.MSG()
        try:
            # Create this thread's message queue before registering thread-owned hotkeys.
            user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 0)
            self.hotkey_thread_id = int(ctypes.windll.kernel32.GetCurrentThreadId())
            actions = tuple(DEFAULT_HOTKEYS)
            for hotkey_id, action in zip(self.hotkey_ids, actions):
                chord = parse_hotkey(self.hotkey_bindings[action])
                if chord and user32.RegisterHotKey(None, hotkey_id, chord[0], chord[1]):
                    self.registered_hotkey_ids.append(hotkey_id)
            names = tuple(self.hotkey_bindings[action] for action in actions)
            self.hotkey_failures = [name for name, hotkey_id in zip(names, self.hotkey_ids)
                                    if hotkey_id not in self.registered_hotkey_ids]
            self.hotkeys_registered = len(self.registered_hotkey_ids) == len(self.hotkey_ids)
            self.hotkey_ready.set()

            region_escape_registered = False
            while True:
                result = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
                if result <= 0:
                    break
                if msg.message == 0x8001:  # Register the Escape hotkey only during region selection.
                    if not region_escape_registered:
                        region_escape_registered = bool(user32.RegisterHotKey(None, 0x4C04, 0, 0x1B))
                elif msg.message == 0x8002:
                    if region_escape_registered:
                        user32.UnregisterHotKey(None, 0x4C04)
                        region_escape_registered = False
                elif msg.message == 0x0312:  # WM_HOTKEY
                    self.hotkey_messages.put(int(msg.wParam))
        except Exception:
            self.hotkey_failures = list(self.hotkey_bindings.values())
            self.hotkeys_registered = False
            self.hotkey_ready.set()
        finally:
            user32.UnregisterHotKey(None, 0x4C04)
            for hotkey_id in self.registered_hotkey_ids:
                user32.UnregisterHotKey(None, hotkey_id)

    def _poll_hotkeys(self) -> None:
        while not self.hotkey_messages.empty():
            try:
                hotkey_id = self.hotkey_messages.get_nowait()
            except queue.Empty:
                break
            if hotkey_id == self.hotkey_ids[0] and self.process is None:
                self.start_recording()
            elif hotkey_id == self.hotkey_ids[1] and self.process is not None:
                self.stop_recording()
            elif hotkey_id == self.hotkey_ids[2]:
                self.click_through.set(not self.click_through.get())
                self._apply_click_through()
            elif hotkey_id == 0x4C04 and self.region_cancel_callback:
                self.region_cancel_callback()

    def _set_region_escape_hotkey(self, enabled: bool) -> None:
        if os.name == "nt" and self.hotkey_thread_id:
            message = 0x8001 if enabled else 0x8002
            ctypes.windll.user32.PostThreadMessageW(self.hotkey_thread_id, message, 0, 0)

    def _apply_click_through(self) -> None:
        if os.name != "nt":
            return
        self.update_idletasks()
        user32 = ctypes.windll.user32
        hwnd = self.winfo_id()
        get_style = getattr(user32, "GetWindowLongPtrW", user32.GetWindowLongW)
        set_style = getattr(user32, "SetWindowLongPtrW", user32.SetWindowLongW)
        ex_style = get_style(hwnd, -20)
        if self.click_through.get():
            ex_style |= 0x00080000 | 0x00000020  # WS_EX_LAYERED | WS_EX_TRANSPARENT
        else:
            ex_style &= ~(0x00080000 | 0x00000020)
        set_style(hwnd, -20, ex_style)
        user32.SetWindowPos(hwnd, None, 0, 0, 0, 0, 0x0001 | 0x0002 | 0x0004 | 0x0020)

    @staticmethod
    def _monitor_bounds(index: int) -> tuple[int, int, int, int] | None:
        if os.name != "nt":
            return None
        class RECT(ctypes.Structure):
            _fields_ = [("left", wintypes.LONG), ("top", wintypes.LONG),
                        ("right", wintypes.LONG), ("bottom", wintypes.LONG)]
        class MONITORINFO(ctypes.Structure):
            _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", RECT),
                        ("rcWork", RECT), ("dwFlags", wintypes.DWORD)]
        bounds: list[tuple[int, int, int, int]] = []
        callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HMONITOR, wintypes.HDC,
                                           ctypes.POINTER(RECT), wintypes.LPARAM)
        def callback(handle, _dc, _rect, _data):
            info = MONITORINFO()
            info.cbSize = ctypes.sizeof(info)
            if ctypes.windll.user32.GetMonitorInfoW(handle, ctypes.byref(info)):
                r = info.rcMonitor
                bounds.append((r.left, r.top, r.right-r.left, r.bottom-r.top))
            return True
        callback_ref = callback_type(callback)
        ctypes.windll.user32.EnumDisplayMonitors(None, None, callback_ref, 0)
        return bounds[index] if 0 <= index < len(bounds) else None

    def _select_region(self) -> None:
        try:
            display_index = int(self.display.get())
        except (ValueError, tk.TclError):
            messagebox.showerror("显示器编号无效", "请先输入有效的显示器编号。")
            return
        bounds = self._monitor_bounds(display_index)
        if not bounds:
            messagebox.showerror("无法选择区域", "无法读取所选显示器的位置；请检查显示器编号。")
            return
        left, top, width, height = bounds
        overlay = tk.Toplevel(self)
        overlay.title("LightRec - 选择录制区域")
        # Keep the full-screen selector visible to Windows accessibility and
        # automation APIs. An overrideredirect window disappears from the
        # normal window list, which also makes recovery from a stuck selector
        # difficult.
        overlay.geometry(f"{width}x{height}{left:+d}{top:+d}")
        overlay.attributes("-fullscreen", True)
        overlay.attributes("-topmost", True)
        overlay.configure(bg="black")
        overlay.attributes("-alpha", 0.28)
        canvas = tk.Canvas(overlay, bg="black", highlightthickness=0, cursor="crosshair")
        canvas.pack(fill="both", expand=True)
        canvas.create_text(18, 18, anchor="nw", fill="white",
                           text="拖动鼠标框选录制区域；按 Esc 取消")
        start: list[int] = []
        rectangle: list[int] = []
        self.withdraw()
        def begin(event):
            start[:] = [event.x, event.y]
            if rectangle:
                canvas.delete(rectangle.pop())
            rectangle.append(canvas.create_rectangle(event.x, event.y, event.x, event.y,
                                                      outline="#00e5ff", width=3, fill="#00e5ff",
                                                      stipple="gray25"))
        def move(event):
            if start and rectangle:
                canvas.coords(rectangle[0], start[0], start[1], event.x, event.y)
        def finish(event):
            if start:
                x1, x2 = sorted((start[0], event.x))
                y1, y2 = sorted((start[1], event.y))
                x1 &= ~1; y1 &= ~1
                w = (x2 - x1) & ~1; h = (y2 - y1) & ~1
                valid = w >= 64 and h >= 64
                if valid:
                    self.region = (x1, y1, w, h)
                    self.region_text.set(f"{w} × {h}（显示器内位置 {x1}, {y1}）")
                overlay.destroy()
                self.deiconify()
                self.lift()
                self.region_cancel_callback = None
                self._set_region_escape_hotkey(False)
                if not valid:
                    messagebox.showwarning("选区太小", "录制区域的宽和高都需要至少 64 像素。", parent=self)
        def cancel(_event=None):
            overlay.destroy()
            self.deiconify()
            self.lift()
            self.region_cancel_callback = None
            self._set_region_escape_hotkey(False)
        canvas.bind("<Button-1>", begin)
        canvas.bind("<B1-Motion>", move)
        canvas.bind("<ButtonRelease-1>", finish)
        overlay.bind("<Escape>", cancel)
        canvas.bind("<Escape>", cancel)
        self.region_cancel_callback = cancel
        self._set_region_escape_hotkey(True)
        overlay.focus_force()
        canvas.focus_set()

    def _clear_region(self) -> None:
        self.region = None
        self.region_text.set(self._fullscreen_label())

    def _target_bitrate_changed(self, _event=None) -> None:
        rate = int(self.target_total_bitrate.get())
        minutes = 71 + 11 / 60
        megabytes = rate * 1000 * minutes * 60 / 8 / 1_000_000
        self.target_bitrate_text.set(f"约 {megabytes:.0f} MB / 71 分钟")

    def _choose_folder(self) -> None:
        path = filedialog.askdirectory(initialdir=self.output_dir.get() or str(Path.home()))
        if path:
            self.output_dir.set(path)

    def _choose_ffmpeg(self) -> None:
        path = filedialog.askopenfilename(filetypes=[("FFmpeg executable", "ffmpeg.exe"), ("All files", "*.*")])
        if path:
            self.ffmpeg.set(path)

    def _show_recording_edge(self, recording: bool) -> None:
        self._hide_recording_edge()
        if not self.edge_enabled.get():
            return
        bounds = self._monitor_bounds(self.capture_idx)
        if not bounds:
            bounds = (0, 0, self.winfo_screenwidth(), self.winfo_screenheight())
        x, y, width, height = bounds
        thickness = max(1, min(16, int(self.edge_thickness.get())))
        opacity = max(5, min(100, int(self.edge_opacity.get()))) / 100
        color = self.edge_start_color.get() if recording else self.edge_stop_color.get()
        horizontal_length = max(1, width)
        vertical_length = max(1, height - 2 * thickness)
        sides = (
            ("top", horizontal_length, thickness, x, y, True),
            ("bottom", horizontal_length, thickness, x, y + height - thickness, True),
            ("left", vertical_length, thickness, x, y + thickness, False),
            ("right", vertical_length, thickness, x + width - thickness, y + thickness, False),
        )
        segments = 24 if self.edge_gradient.get() else 1
        for side, length, band, origin_x, origin_y, horizontal in sides:
            if not self.edge_sides[side].get():
                continue
            for index in range(segments):
                start = round(length * index / segments)
                end = round(length * (index + 1) / segments)
                if end <= start:
                    continue
                if segments == 1:
                    segment_alpha = opacity
                else:
                    midpoint = (index + 0.5) / segments
                    segment_alpha = opacity * max(0.04, 1 - abs(midpoint * 2 - 1))
                if horizontal:
                    geometry = (end - start, band, origin_x + start, origin_y)
                else:
                    geometry = (band, end - start, origin_x, origin_y + start)
                edge_width, edge_height, edge_x, edge_y = geometry
                try:
                    window = tk.Toplevel(self)
                    window.overrideredirect(True)
                    window.attributes("-topmost", True)
                    window.attributes("-alpha", segment_alpha)
                    window.configure(bg=color)
                    window.geometry(f"{edge_width}x{edge_height}{edge_x:+d}{edge_y:+d}")
                    window.update_idletasks()
                    if os.name == "nt":
                        user32 = ctypes.windll.user32
                        hwnd = window.winfo_id()
                        get_style = getattr(user32, "GetWindowLongPtrW", user32.GetWindowLongW)
                        set_style = getattr(user32, "SetWindowLongPtrW", user32.SetWindowLongW)
                        style = get_style(hwnd, -20) | 0x00080000 | 0x00000020 | 0x00000080
                        set_style(hwnd, -20, style)
                        user32.SetWindowPos(hwnd, None, 0, 0, 0, 0,
                                            0x0001 | 0x0002 | 0x0004 | 0x0020)
                    self.edge_windows.append(window)
                except tk.TclError:
                    continue
        self.edge_after_id = self.after(max(1, int(self.edge_duration.get())) * 1000,
                                        self._hide_recording_edge)

    def _hide_recording_edge(self) -> None:
        if self.edge_after_id:
            try:
                self.after_cancel(self.edge_after_id)
            except tk.TclError:
                pass
            self.edge_after_id = None
        for window in self.edge_windows:
            try:
                if window.winfo_exists():
                    window.destroy()
            except tk.TclError:
                pass
        self.edge_windows.clear()

    def _set_busy(self, busy: bool, status: str) -> None:
        self.record_button.configure(state="disabled" if busy else "normal")
        self.stop_button.configure(state="normal" if busy else "disabled")
        self.settings_button.configure(state="disabled" if busy else "normal")
        self.status.set(status)

    def _ensure_encoder_probe(self, codec: str) -> tuple[str, bool]:
        ffmpeg = self.ffmpeg.get().strip().strip('"')
        key = (ffmpeg, codec.startswith("HEVC"))
        if key in self.encoder_cache or key in self.encoder_probes or not ffmpeg:
            return key
        self.encoder_probes.add(key)

        def probe() -> None:
            try:
                if not Path(ffmpeg).is_file():
                    raise FileNotFoundError(ffmpeg)
                filter_available = ffmpeg_has_filter(ffmpeg, "ddagrab")
                encoder = choose_encoder(ffmpeg, key[1]) if filter_available else None
                self.encoder_results.put((key, filter_available, encoder, None))
            except Exception as exc:
                self.encoder_results.put((key, False, None, str(exc)))

        threading.Thread(target=probe, name="encoder-probe", daemon=True).start()
        return key

    def _finish_pending_start(self, key: tuple[str, bool], result) -> None:
        if not self.pending_start or self.pending_encoder_key != key:
            return
        self.pending_start = False
        self.pending_encoder_key = None
        self.record_button.configure(state="normal")
        self.settings_button.configure(state="normal")
        filter_available, encoder, error = result
        if error:
            self.status.set("准备就绪")
            messagebox.showerror("编码器检查失败", error)
        elif not filter_available:
            self.status.set("准备就绪")
            messagebox.showerror("无法录制桌面", "当前 FFmpeg 不支持桌面录制（ddagrab）。")
        elif not encoder:
            self.status.set("准备就绪")
            messagebox.showerror("编码器不可用", "这台电脑无法启动所选格式的编码器。")
        else:
            self.start_recording()

    def start_recording(self) -> None:
        ffmpeg = self.ffmpeg.get().strip().strip('"')
        if not ffmpeg or not Path(ffmpeg).is_file():
            messagebox.showerror("录屏组件不可用", "内置录屏组件无法启动。开发版可到“设置… → 录制设置”选择 ffmpeg.exe。")
            return
        key = self._ensure_encoder_probe(self.codec.get())
        probe = self.encoder_cache.get(key)
        if probe is None:
            self.pending_start = True
            self.pending_encoder_key = key
            self.record_button.configure(state="disabled")
            self.settings_button.configure(state="disabled")
            self.status.set("正在准备录屏编码器，请稍候…")
            return
        filter_available, encoder, probe_error = probe
        if probe_error:
            messagebox.showerror("编码器检查失败", probe_error)
            return
        if not filter_available:
            messagebox.showerror("无法录制桌面", "当前 FFmpeg 不支持桌面录制（ddagrab）。")
            return
        if not encoder:
            messagebox.showerror("编码器不可用", "这台电脑无法启动所选格式的编码器。请把此提示和录制日志发给我。")
            return
        folder = Path(self.output_dir.get()).expanduser()
        try:
            folder.mkdir(parents=True, exist_ok=True)
            if not os.access(folder, os.W_OK):
                raise OSError("The folder is not writable.")
        except OSError as exc:
            messagebox.showerror("无法保存到此位置", str(exc))
            return

        try:
            self.capture_idx = int(self.display.get())
            if self.capture_idx < 0:
                raise ValueError("negative output index")
        except (ValueError, tk.TclError):
            messagebox.showerror("显示器编号无效", "请输入 0 或更大的整数。")
            return
        stamp = dt.datetime.now().strftime("%Y-%m-%d_%H-%M-%S-%f")
        self.final_path = folder / f"Screen_{stamp}.mp4"
        self.video_tmp = folder / f".Screen_{stamp}.recording.mp4"
        self.audio_tmp = folder / f".Screen_{stamp}.audio.m4a"
        self.audio_log_path = folder / f".Screen_{stamp}.audio.ffmpeg.log"
        self.log_path = folder / f".Screen_{stamp}.ffmpeg.log"
        self.audio_stop.clear()
        self.audio_started.clear()
        self.audio_clock[0] = None
        self.audio_active = self.audio.get()
        self.audio_runtime_error = None
        self.stopping = False
        self.finalizing = False
        while not self.audio_errors.empty():
            self.audio_errors.get_nowait()

        if self.audio_active:
            audio_args = [
                ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "warning",
                "-f", "s16le", "-ar", "48000", "-ac", "2", "-i", "pipe:0",
                "-vn", "-c:a", "aac", "-b:a", f"{int(self.audio_bitrate.get())}k",
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
                messagebox.showerror("无法录制声音", str(exc))
                return
            self.audio_thread = threading.Thread(
                target=capture_audio,
                args=(self.audio_process, self.audio_stop, self.audio_started,
                      self.audio_clock, self.audio_errors, self.audio_source.get()),
                daemon=True,
            )
            self.audio_thread.start()

        fps = int(self.fps.get())
        total_bitrate = int(self.target_total_bitrate.get())
        audio_bitrate = int(self.audio_bitrate.get()) if self.audio_active else 0
        video_bitrate = video_bitrate_for(total_bitrate, audio_bitrate)
        # With dup_frames=0, ddagrab keeps real timestamps but does not emit
        # repeated frames during static periods. This makes screen capture VFR.
        duplicate_frames = 0 if self.skip_unchanged.get() else 1
        self.current_encoder = encoder
        # Keep captured desktop frames on the GPU for AMF and QSV where the
        # selected encoder can share the D3D11 device. An early failure retries
        # the same encoder through the compatibility download/upload path.
        self.direct_d3d11 = encoder.endswith(("_amf", "_qsv"))
        try:
            with open(self.log_path, "wb") as log:
                self.process = subprocess.Popen(
                    self._video_args(ffmpeg, encoder, fps, video_bitrate, duplicate_frames, direct=self.direct_d3d11),
                    stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=log,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
        except OSError as exc:
            self._stop_audio_encoder()
            if self.audio_tmp:
                self.audio_tmp.unlink(missing_ok=True)
            if self.audio_log_path:
                self.audio_log_path.unlink(missing_ok=True)
            messagebox.showerror("无法开始录制", str(exc))
            return

        self.started_at = time.perf_counter()
        self._set_busy(True, f"正在录制 · {encoder_label(self.current_encoder)}")
        self._show_recording_edge(True)
        self.record_thread = threading.Thread(target=self._watch_encoder, daemon=True)
        self.record_thread.start()

    def _video_args(self, ffmpeg: str, encoder: str, fps: int, video_bitrate: int,
                    duplicate_frames: int, direct: bool) -> list[str]:
        args = [ffmpeg, "-hide_banner", "-nostats", "-loglevel", "warning"]
        if direct:
            # Share the Desktop Duplication D3D11 device with the hardware
            # encoder to avoid downloading captured frames to system memory.
            args += ["-init_hw_device", "d3d11va=dev:0",
                     "-filter_hw_device", "dev"]
            if encoder.endswith("_amf"):
                args += ["-init_hw_device", "amf=amf@dev"]
            elif encoder.endswith("_qsv"):
                # Derive Quick Sync from the same D3D11 device used by ddagrab.
                args += ["-init_hw_device", "qsv=qsv@dev"]
        args += [
            "-f", "lavfi", "-i",
            self._capture_filter(fps, duplicate_frames),
        ]
        if direct and encoder == "h264_amf":
            # H.264 AMF fails on raw BGRA D3D11 surfaces on this driver; keep
            # the conversion on the shared GPU device instead of downloading.
            args += ["-vf", "scale_d3d11=format=nv12"]
        elif not direct:
            # Download D3D11 frames for QSV/NVENC upload or universal software encoding.
            pixel_format = "nv12" if encoder.endswith(("_qsv", "_nvenc", "_mf")) else "yuv420p"
            args += ["-vf", f"hwdownload,format=bgra,format={pixel_format}"]
        args += ["-an", "-c:v", encoder]
        if encoder.endswith("_amf"):
            args += ["-usage", "transcoding", "-quality", "quality", "-rc", "cbr",
                     "-minrate", f"{video_bitrate}k", "-enforce_hrd", "true", "-filler_data", "true"]
        elif encoder.endswith("_qsv"):
            args += ["-preset", "veryfast"]
        elif encoder.endswith("_nvenc"):
            args += ["-preset", "p4"]
        elif encoder.endswith("_mf"):
            args += ["-hw_encoding", "1", "-rate_control", "cbr"]
        elif encoder == "libx264":
            args += ["-preset", "veryfast"]
        elif encoder == "libx265":
            args += ["-preset", "ultrafast"]
        args += ["-b:v", f"{video_bitrate}k", "-maxrate", f"{video_bitrate}k",
            "-g", str(fps * 2),
            "-fps_mode", "vfr",
            "-movflags", "+frag_keyframe+empty_moov+default_base_moof",
            "-f", "mp4", str(self.video_tmp),
        ]
        return args

    def _capture_filter(self, fps: int, duplicate_frames: int) -> str:
        options = [f"output_idx={self.capture_idx}", f"framerate={fps}",
                   "draw_mouse=1", f"dup_frames={duplicate_frames}"]
        if self.region:
            x, y, width, height = self.region
            options.extend((f"video_size={width}x{height}", f"offset_x={x}", f"offset_y={y}"))
        return "ddagrab=" + ":".join(options)

    def _watch_encoder(self) -> None:
        proc = self.process
        if proc is None:
            return
        code = proc.wait()
        if (code != 0 and not self.stopping and self.started_at
                and time.perf_counter() - self.started_at < 5):
            if self.direct_d3d11:
                self.after(0, lambda proc=proc, code=code: self._restart_compat_path(proc, code))
                return
            if self.current_encoder and self.current_encoder.endswith(("_amf", "_qsv", "_nvenc", "_mf")):
                self.after(0, lambda proc=proc, code=code: self._restart_encoder_fallback(proc, code))
                return
        self.after(0, lambda: self._encoder_exited(code))

    def _restart_compat_path(self, failed_process: subprocess.Popen, code: int) -> None:
        if self.process is not failed_process or self.stopping or not self.direct_d3d11:
            return
        self.direct_d3d11 = False
        self.status.set("正在切换兼容录制路径…")
        if self.video_tmp:
            try:
                self.video_tmp.unlink(missing_ok=True)
            except OSError as exc:
                self.after(0, lambda exc=exc, code=code: self._finalize(False, f"Direct D3D11 capture failed (FFmpeg {code}); cannot discard its incomplete file: {exc}"))
                return
        ffmpeg = self.ffmpeg.get().strip().strip('"')
        encoder = self.current_encoder
        if not encoder:
            self.after(0, lambda: self._finalize(False, "没有可用的视频编码器。"))
            return
        fps = int(self.fps.get())
        audio_bitrate = int(self.audio_bitrate.get()) if self.audio_active else 0
        video_bitrate = video_bitrate_for(int(self.target_total_bitrate.get()), audio_bitrate)
        duplicate_frames = 0 if self.skip_unchanged.get() else 1
        try:
            with open(self.log_path, "ab") as log:
                self.process = subprocess.Popen(
                    self._video_args(ffmpeg, encoder, fps, video_bitrate, duplicate_frames, direct=False),
                    stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=log,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
        except OSError as exc:
            self.after(0, lambda exc=exc, code=code: self._finalize(False, f"Direct D3D11 capture failed (FFmpeg {code}); compatibility mode could not start: {exc}"))
            return
        self.record_thread = threading.Thread(target=self._watch_encoder, daemon=True)
        self.record_thread.start()

    def _restart_encoder_fallback(self, failed_process: subprocess.Popen, code: int) -> None:
        if self.process is not failed_process or self.stopping:
            return
        ffmpeg = self.ffmpeg.get().strip().strip('"')
        hevc = self.codec.get().startswith("HEVC")
        previous_encoder = self.current_encoder
        encoder = choose_encoder(ffmpeg, hevc, after=previous_encoder)
        if not encoder:
            self.after(0, lambda: self._finalize(False, "所选格式的硬件编码器与 CPU 编码器都无法启动。"))
            return
        self.current_encoder = encoder
        self.status.set(f"{encoder_label(previous_encoder)}启动失败，正在切换到 {encoder_label(encoder)}…")
        if self.video_tmp:
            try:
                self.video_tmp.unlink(missing_ok=True)
            except OSError as exc:
                self.after(0, lambda exc=exc, code=code: self._finalize(False, f"Hardware encoding failed (FFmpeg {code}); cannot discard its incomplete file: {exc}"))
                return
        fps = int(self.fps.get())
        audio_bitrate = int(self.audio_bitrate.get()) if self.audio_active else 0
        video_bitrate = video_bitrate_for(int(self.target_total_bitrate.get()), audio_bitrate)
        duplicate_frames = 0 if self.skip_unchanged.get() else 1
        try:
            with open(self.log_path, "ab") as log:
                self.process = subprocess.Popen(
                    self._video_args(ffmpeg, encoder, fps, video_bitrate, duplicate_frames, direct=False),
                    stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=log,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
        except OSError as exc:
            self.after(0, lambda exc=exc, code=code, previous_encoder=previous_encoder: self._finalize(False, f"{encoder_label(previous_encoder)}编码失败 (FFmpeg {code}); fallback could not start: {exc}"))
            return
        self.direct_d3d11 = False
        self.record_thread = threading.Thread(target=self._watch_encoder, daemon=True)
        self.record_thread.start()

    def _encoder_exited(self, code: int) -> None:
        if self.process is None:
            return
        if not self.stopping and self.record_button["state"] == "disabled":
            self._show_recording_edge(False)
            self.finalizing = True
            self.stop_button.configure(state="disabled")
            self.status.set("录制已中断，正在尝试保存…")
            threading.Thread(target=self._finish_after_encoder, daemon=True).start()

    def stop_recording(self) -> None:
        if self.process is None:
            return
        self.stopping = True
        self.finalizing = True
        self.stop_button.configure(state="disabled")
        self.status.set("正在保存视频，请稍候…")
        self._show_recording_edge(False)
        try:
            if self.process.stdin:
                self.process.stdin.write(b"q\n")
                self.process.stdin.flush()
        except (BrokenPipeError, OSError):
            pass
        threading.Thread(target=self._finish_after_encoder, daemon=True).start()

    def _on_close(self) -> None:
        if self.close_behavior == "tray":
            self._minimize_to_tray()
            return
        if self.close_behavior == "exit":
            self._exit_or_save()
            return
        self._show_close_dialog()

    def _show_close_dialog(self) -> None:
        dialog = tk.Toplevel(self)
        dialog.title("关闭提示")
        dialog.resizable(False, False)
        dialog.transient(self)
        dialog.geometry("390x190")
        choice = tk.StringVar(value="tray")
        ttk.Label(dialog, text="关闭 LightRec 时：").pack(anchor="w", padx=20, pady=(16, 8))
        ttk.Radiobutton(dialog, text="最小化到系统托盘，继续在后台运行", variable=choice,
                        value="tray").pack(anchor="w", padx=30, pady=3)
        exit_text = "停止并保存后退出程序" if self.process is not None else "退出程序"
        ttk.Radiobutton(dialog, text=exit_text, variable=choice, value="exit").pack(
            anchor="w", padx=30, pady=3)
        remember = tk.BooleanVar(value=False)
        ttk.Checkbutton(dialog, text="不再提示", variable=remember).pack(anchor="w", padx=18, pady=(8, 4))
        buttons = ttk.Frame(dialog)
        buttons.pack(anchor="e", padx=16, pady=(0, 12))

        def apply_choice() -> None:
            selected = choice.get()
            if remember.get():
                self.close_behavior = selected
                self._save_settings()
            dialog.destroy()
            if selected == "tray":
                self._minimize_to_tray()
            else:
                self._exit_or_save()

        ttk.Button(buttons, text="确定", command=apply_choice).pack(side="left", padx=(0, 8))
        ttk.Button(buttons, text="取消", command=dialog.destroy).pack(side="left")
        dialog.protocol("WM_DELETE_WINDOW", dialog.destroy)
        dialog.bind("<Return>", lambda _event: apply_choice())
        dialog.bind("<Escape>", lambda _event: dialog.destroy())
        dialog.grab_set()
        dialog.focus_force()

    def _exit_or_save(self) -> None:
        if self.process is not None:
            self.close_when_done = True
            if not self.finalizing:
                self.stop_recording()
            return
        self._exit_app()

    def _exit_app(self) -> None:
        self._hide_recording_edge()
        if self.tray_icon:
            try:
                self.tray_icon.stop()
            except Exception:
                pass
            self.tray_icon = None
        self._unregister_hotkeys()
        self.destroy()

    def _minimize_to_tray(self) -> None:
        if self.tray_icon is None:
            import pystray
            from PIL import Image, ImageDraw

            base_dir = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
            icon_path = base_dir / "assets" / "onenote-tray.ico"
            try:
                with Image.open(icon_path) as source:
                    image = source.convert("RGBA").resize((64, 64), Image.Resampling.LANCZOS)
            except (OSError, ValueError):
                # Keep the tray usable if the optional branded icon is missing.
                image = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
                draw = ImageDraw.Draw(image)
                draw.rounded_rectangle((5, 5, 59, 59), radius=12, fill="#168A5B")
                draw.rectangle((19, 17, 45, 43), outline="white", width=4)
                draw.line((24, 48, 40, 48), fill="white", width=4)
            menu = pystray.Menu(
                pystray.MenuItem("打开 LightRec", self._tray_restore, default=True),
                pystray.MenuItem("退出程序", self._tray_exit),
            )
            self.tray_icon = pystray.Icon(APP_NAME, image, APP_NAME, menu)
            self.tray_thread = threading.Thread(target=self.tray_icon.run, daemon=True)
            self.tray_thread.start()
        self.withdraw()

    def _tray_restore(self, _icon=None, _item=None) -> None:
        self.after(0, self._restore_from_tray)

    def _restore_from_tray(self) -> None:
        self.deiconify()
        self.state("normal")
        self.lift()
        self.focus_force()

    def _tray_exit(self, _icon=None, _item=None) -> None:
        self.after(0, self._exit_or_save)

    def _open_output_folder(self) -> None:
        folder = Path(self.output_dir.get()).expanduser()
        try:
            folder.mkdir(parents=True, exist_ok=True)
            os.startfile(str(folder))
        except (OSError, AttributeError) as exc:
            messagebox.showerror("无法打开保存位置", f"请检查文件夹路径：\n{folder}\n\n{exc}", parent=self)

    def _stop_audio_encoder(self) -> None:
        self.audio_stop.set()
        if self.audio_thread and self.audio_thread.is_alive():
            self.audio_thread.join(timeout=5)
        audio_proc = self.audio_process
        if audio_proc is None:
            return
        if audio_proc.stdin and not audio_proc.stdin.closed:
            try:
                audio_proc.stdin.close()
            except OSError:
                pass
        if audio_proc.poll() is None:
            self._wait_or_kill(audio_proc, timeout=10)

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
        error = None
        try:
            error = self.audio_errors.get_nowait()
        except queue.Empty:
            pass
        if proc.returncode != 0:
            self.after(0, lambda: self._finalize(False, f"FFmpeg stopped with exit code {proc.returncode}."))
            return
        if self.audio_active and self.audio_process and self.audio_process.returncode != 0:
            code = self.audio_process.returncode
            self.audio_runtime_error = self.audio_runtime_error or f"AAC encoding stopped (FFmpeg exit {code})."
            self.audio_active = False
        if error:
            self.audio_runtime_error = self.audio_runtime_error or f"Audio capture failed: {error}"
            self.audio_active = False
        if not self.audio_active and self.audio_tmp:
            self.audio_tmp.unlink(missing_ok=True)
        try:
            self._mux_or_move()
            message = f"视频已保存：\n{self.final_path}"
            if self.audio_runtime_error:
                message += f"\n\n视频已保存，但没有声音。{self.audio_runtime_error}"
            self.after(0, lambda message=message: self._finalize(True, message))
        except Exception as exc:
            message = f"无法完成视频保存：{exc}"
            self.after(0, lambda message=message: self._finalize(False, message))

    def _mux_or_move(self) -> None:
        assert self.video_tmp and self.final_path and self.ffmpeg.get()
        if self.audio_active and self.audio_tmp and self.audio_tmp.exists():
            assert self.log_path
            audio_start = self.audio_clock[0]
            video_start = self.started_at
            offset = (video_start or time.perf_counter()) - (audio_start or time.perf_counter())
            args = [self.ffmpeg.get(), "-hide_banner", "-nostdin", "-y", "-loglevel", "error",
                    "-i", str(self.video_tmp)]
            if offset > 0:
                args.extend(("-ss", f"{offset:.6f}"))
            elif offset < 0:
                args.extend(("-itsoffset", f"{-offset:.6f}"))
            args.extend(("-i", str(self.audio_tmp), "-map", "0:v:0", "-map", "1:a:0",
                         "-c", "copy", "-shortest", "-movflags", "+faststart",
                         str(self.final_path)))
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
            assert self.log_path
            args = [
                self.ffmpeg.get(), "-hide_banner", "-nostdin", "-y", "-loglevel", "error",
                "-i", str(self.video_tmp), "-map", "0:v:0", "-c", "copy",
                "-movflags", "+faststart", str(self.final_path),
            ]
            with open(self.log_path, "ab") as log:
                done = subprocess.run(args, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                      stderr=log, timeout=90,
                                      creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            if done.returncode:
                raise RuntimeError(f"FFmpeg finalize failed (exit {done.returncode}). Log: {self.log_path}")
        self.video_tmp.unlink(missing_ok=True)
        # Keep FFmpeg's audio diagnostics if capture failed so the reported
        # device/encoder issue can be inspected instead of being discarded.
        if self.audio_log_path and not self.audio_runtime_error:
            self.audio_log_path.unlink(missing_ok=True)
        if self.log_path:
            self.log_path.unlink(missing_ok=True)

    def _finalize(self, ok: bool, message: str) -> None:
        self.process = None
        self.audio_process = None
        self.audio_thread = None
        self.record_thread = None
        self.stopping = False
        self.finalizing = False
        if not ok:
            if self.video_tmp and self.video_tmp.exists():
                message += f"\nIncomplete recording retained for recovery: {self.video_tmp}"
            if self.log_path and self.log_path.exists():
                message += f"\n\nDiagnostic log: {self.log_path}"
            if self.audio_log_path and self.audio_log_path.exists():
                message += f"\nAudio diagnostic log: {self.audio_log_path}"
        elif self.started_at:
            elapsed = int(time.perf_counter() - self.started_at)
            saved = f"已保存：{self.final_path} · {elapsed // 60}:{elapsed % 60:02d}"
            if self.audio_runtime_error:
                saved += f" · 声音录制失败：{self.audio_runtime_error}"
            self._set_busy(False, saved)
        else:
            self._set_busy(False, "准备就绪")
        if not ok:
            self._set_busy(False, "录制遇到问题")
            messagebox.showerror("录制遇到问题", message)
        if self.close_when_done:
            self._exit_app()

    def _poll(self) -> None:
        self._poll_hotkeys()
        if self.hotkey_ready.is_set() and not self.hotkey_ready_reported:
            self.hotkey_ready_reported = True
            self._refresh_hotkey_summary()
            if self.hotkey_failures:
                self.status.set("有快捷键未能注册；请查看设置中的冲突提示。")
        while True:
            try:
                key, filter_available, encoder, error = self.encoder_results.get_nowait()
            except queue.Empty:
                break
            self.encoder_probes.discard(key)
            result = (filter_available, encoder, error)
            self.encoder_cache[key] = result
            self._finish_pending_start(key, result)
        if self.audio_active and self.process and not self.finalizing and not self.audio_errors.empty():
            self.audio_runtime_error = self.audio_errors.get_nowait()
            self.audio_active = False
            self.status.set(f"声音录制失败：{self.audio_runtime_error}；画面仍在录制")
        if self.process and self.started_at and self.process.poll() is None and not self.finalizing:
            if not self.audio_runtime_error:
                elapsed = int(time.perf_counter() - self.started_at)
                audio_status = " · 正在启动声音设备…" if self.audio_active and not self.audio_started.is_set() else ""
                self.status.set(
                    f"正在录制 · {encoder_label(self.current_encoder)} · "
                    f"{elapsed // 60}:{elapsed % 60:02d}{audio_status}"
                )
        self.after(250, self._poll)

    def _unregister_hotkeys(self) -> None:
        if os.name == "nt" and self.hotkey_thread_id:
            ctypes.windll.user32.PostThreadMessageW(self.hotkey_thread_id, 0x0012, 0, 0)  # WM_QUIT
        if self.hotkey_thread and self.hotkey_thread.is_alive():
            self.hotkey_thread.join(timeout=1)
        self.hotkey_thread = None
        self.hotkey_thread_id = None
        self.registered_hotkey_ids.clear()
        self.hotkeys_registered = False


if __name__ == "__main__":
    LightRec().mainloop()
