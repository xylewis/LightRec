from __future__ import annotations

import ctypes
import os
import queue
import sys
import threading
import time
from pathlib import Path
import tkinter as tk
from tkinter import colorchooser, filedialog, messagebox, ttk

from .config import (
    APP_NAME, AUDIO_BITRATE_CHOICES, DEFAULT_HOTKEYS, FPS_CHOICES,
    TOTAL_BITRATE_CHOICES, normalize_hotkey, parse_hotkey, read_settings,
    saved_choice, saved_color, saved_int, write_settings,
)
from .media import (
    choose_encoder, encoder_label, ffmpeg_has_filter, find_ffmpeg,
)
from .hotkeys import GlobalHotkeyManager
from .tray import TrayController
from .ui.edge_overlay import EdgeStyle, RecordingEdgeOverlay
from .ui.region_selector import RegionSelector
from .ui.settings_dialog import SettingsDialog
from .recording import RecordingOptions, RecordingResult, RecordingSession


class LightRec(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title(APP_NAME)
        self.geometry("560x455")
        self.minsize(500, 410)
        self.resizable(True, True)

        saved = read_settings()
        self.ffmpeg = tk.StringVar(value=find_ffmpeg())
        self.ffmpeg_is_bundled = bool(getattr(sys, "frozen", False) and
                                      (Path(getattr(sys, "_MEIPASS", "")) / "ffmpeg.exe").is_file())
        self.output_dir = tk.StringVar(value=str(saved.get("output_dir") or Path.home() / "Videos" / APP_NAME))
        self.codec = tk.StringVar(value=saved_choice(saved, "codec", ("HEVC · 文件较小", "H.264 · 兼容性较好"), "HEVC · 文件较小"))
        self.fps = tk.IntVar(value=saved_int(saved, "fps", FPS_CHOICES, 20))
        self.target_total_bitrate = tk.IntVar(value=saved_int(saved, "target_total_bitrate", TOTAL_BITRATE_CHOICES, 1400))
        self.display = tk.IntVar(value=saved_int(saved, "display", tuple(range(16)), 0))
        self.region: tuple[int, int, int, int] | None = None
        self.click_through = tk.BooleanVar(value=bool(saved.get("click_through", False)))
        self.skip_unchanged = tk.BooleanVar(value=bool(saved.get("skip_unchanged", False)))
        self.audio = tk.BooleanVar(value=bool(saved.get("audio", True)))
        self.audio_source = tk.StringVar(value=saved_choice(
            saved, "audio_source", ("电脑声音 + 麦克风", "电脑声音", "麦克风"), "电脑声音 + 麦克风"))
        self.audio_bitrate = tk.IntVar(value=saved_int(saved, "audio_bitrate", AUDIO_BITRATE_CHOICES, 128))
        self.edge_enabled = tk.BooleanVar(value=bool(saved.get("edge_enabled", True)))
        self.edge_start_color = tk.StringVar(value=saved_color(saved, "edge_start_color", "#20B878"))
        self.edge_stop_color = tk.StringVar(value=saved_color(saved, "edge_stop_color", "#D86767"))
        self.edge_opacity = tk.IntVar(value=saved_int(saved, "edge_opacity", tuple(range(5, 101)), 22))
        self.edge_thickness = tk.IntVar(value=saved_int(saved, "edge_thickness", tuple(range(1, 17)), 4))
        self.edge_gradient = tk.BooleanVar(value=bool(saved.get("edge_gradient", True)))
        self.edge_duration = tk.IntVar(value=saved_int(saved, "edge_duration", tuple(range(1, 11)), 2))
        saved_sides = saved.get("edge_sides", {})
        self.edge_sides = {
            name: tk.BooleanVar(value=bool(saved_sides.get(name, True)) if isinstance(saved_sides, dict) else True)
            for name in ("top", "bottom", "left", "right")
        }
        self.status = tk.StringVar(value="准备就绪")
        self.target_bitrate_text = tk.StringVar(value="")
        self.capture_idx = 0
        self.recorder = RecordingSession(
            self, on_started=self._recording_started, on_status=self._recording_status,
            on_finished=self._recording_finished, on_stop_signal=self._recording_stopping,
        )
        self.close_when_done = False
        self.close_behavior = saved.get("close_behavior", "ask")
        if self.close_behavior not in ("ask", "tray", "exit"):
            self.close_behavior = "ask"
        configured_hotkeys = saved.get("hotkeys", {})
        self.hotkey_bindings = {}
        for action, default in DEFAULT_HOTKEYS.items():
            raw = configured_hotkeys.get(action, default) if isinstance(configured_hotkeys, dict) else default
            self.hotkey_bindings[action] = normalize_hotkey(raw) or default
        if len({parse_hotkey(value) for value in self.hotkey_bindings.values()}) != len(DEFAULT_HOTKEYS):
            self.hotkey_bindings = DEFAULT_HOTKEYS.copy()
        self.hotkeys = GlobalHotkeyManager()
        self.hotkey_ready_reported = False
        self.region_selector = RegionSelector(self, self._set_region_escape_hotkey)
        self.edge_overlay = RecordingEdgeOverlay(self)
        self.tray = TrayController(self, self._exit_or_save)
        self.settings_window: tk.Toplevel | None = None
        self.settings_dialog: SettingsDialog | None = None
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
        if not self.hotkeys.ready.is_set():
            summary += "　（正在注册快捷键…）"
        elif not self.hotkeys.registered:
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
            write_settings(values)
        except (OSError, tk.TclError, ValueError):
            pass

    def _open_settings(self) -> None:
        if self.settings_dialog and self.settings_dialog.window and self.settings_dialog.window.winfo_exists():
            self.settings_dialog.window.lift()
            return
        fields = {
            "codec": self.codec, "fps": self.fps, "target_total_bitrate": self.target_total_bitrate,
            "target_bitrate_text": self.target_bitrate_text, "display": self.display,
            "audio_bitrate": self.audio_bitrate, "skip_unchanged": self.skip_unchanged,
            "click_through": self.click_through, "ffmpeg": self.ffmpeg,
            "edge_enabled": self.edge_enabled, "edge_start_color": self.edge_start_color,
            "edge_stop_color": self.edge_stop_color, "edge_opacity": self.edge_opacity,
            "edge_thickness": self.edge_thickness, "edge_gradient": self.edge_gradient,
            "edge_sides": self.edge_sides, "edge_duration": self.edge_duration,
            "choose_ffmpeg": self._choose_ffmpeg,
        }
        self.settings_dialog = SettingsDialog(
            self, fields, ffmpeg_is_bundled=self.ffmpeg_is_bundled,
            hotkeys=self.hotkey_bindings, conflicts=self.hotkeys.failures,
            on_apply=self._apply_settings, on_capture=self._capture_hotkey,
            on_color=self._choose_edge_color, on_preview=self._preview_recording_edge,
            on_bitrate_change=self._target_bitrate_changed,
            on_click_through=self._apply_click_through,
        )
        self.settings_window = self.settings_dialog.show()


    def _choose_edge_color(self, name: str) -> None:
        variable = self.edge_start_color if name == "start" else self.edge_stop_color
        color = colorchooser.askcolor(color=variable.get(), parent=self.settings_window,
                                      title="选择描边颜色")[1]
        if color:
            variable.set(color.upper())
            label = self.settings_dialog.edge_color_labels.get(name) if self.settings_dialog else None
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
        if not self.settings_dialog or not self.settings_dialog.window or not self.settings_dialog.window.winfo_exists():
            return
        self._unregister_hotkeys()
        dialog = tk.Toplevel(self.settings_dialog.window)
        dialog.title("录入快捷键")
        dialog.geometry("360x145")
        dialog.resizable(False, False)
        dialog.transient(self.settings_dialog.window)
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
            if restart and self.recorder.process is None:
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
            pending = self.settings_dialog.pending_hotkeys.copy()
            pending[action] = candidate
            chords = [parse_hotkey(pending[name]) for name in DEFAULT_HOTKEYS]
            if None in chords or len(set(chords)) != len(chords):
                prompt.set("这个快捷键与其他功能重复，请录入不同的按键。")
                return "break"

            self.settings_dialog.pending_hotkeys = pending
            self.settings_dialog.hotkey_labels[action].set(candidate)
            self.hotkey_bindings = pending.copy()
            self._save_settings()
            self._register_hotkeys()
            self._refresh_hotkey_summary()
            if not self.hotkeys.ready.is_set():
                self.settings_dialog.hotkey_status.set("快捷键已保存，正在注册…")
            elif self.hotkeys.failures:
                self.settings_dialog.hotkey_status.set(
                    "快捷键已保存，但无法注册，可能被 Windows 或其他程序占用："
                    + "、".join(self.hotkeys.failures)
                    + "。请录入其他按键。"
                )
            else:
                self.settings_dialog.hotkey_status.set("快捷键已保存并生效。")
            finish(restart=False)
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
        pending = self.settings_dialog.pending_hotkeys if self.settings_dialog else self.hotkey_bindings
        chords = [parse_hotkey(pending[action]) for action in DEFAULT_HOTKEYS]
        if None in chords or len(set(chords)) != len(chords):
            self.settings_dialog.hotkey_status.set("热键无效或重复，请为开始、停止和窗口穿透分别设置组合键。")
            return
        self._unregister_hotkeys()
        self.hotkey_bindings = pending.copy()
        self._register_hotkeys()
        self._save_settings()
        self._refresh_hotkey_summary()
        if self.hotkeys.failures:
            self.settings_dialog.hotkey_status.set(
                "以下快捷键无法注册，可能被 Windows 或其他程序占用："
                + "、".join(self.hotkeys.failures)
                + "。请换一组按键后再应用。"
            )
            self.status.set("有快捷键未能注册；请查看设置中的冲突提示。")
            return
        if self.settings_dialog and self.settings_dialog.window and self.settings_dialog.window.winfo_exists():
            self.settings_dialog.window.destroy()
        self.settings_window = None
        self.settings_dialog = None

    def _register_hotkeys(self, wait: bool = True) -> None:
        self.hotkey_ready_reported = False
        self.hotkeys.start(self.hotkey_bindings, wait=wait)



    def _poll_hotkeys(self) -> None:
        for hotkey_id in self.hotkeys.poll():
            if hotkey_id == self.hotkeys.START and self.recorder.process is None:
                self.start_recording()
            elif hotkey_id == self.hotkeys.STOP and self.recorder.process is not None:
                self.stop_recording()
            elif hotkey_id == self.hotkeys.CLICK_THROUGH:
                self.click_through.set(not self.click_through.get())
                self._apply_click_through()
            elif hotkey_id == self.hotkeys.CANCEL_REGION:
                self.region_selector.cancel()


    def _set_region_escape_hotkey(self, enabled: bool) -> None:
        self.hotkeys.set_region_cancel(enabled)


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

    def _select_region(self) -> None:
        try:
            display_index = int(self.display.get())
        except (ValueError, tk.TclError):
            messagebox.showerror("显示器编号无效", "请先输入有效的显示器编号。")
            return

        def completed(region, too_small: bool) -> None:
            if region:
                x, y, width, height = region
                self.region = region
                self.region_text.set(f"{width} × {height}（显示器内位置 {x}, {y}）")
            elif too_small:
                messagebox.showwarning("选区太小", "录制区域的宽和高都需要至少 64 像素。", parent=self)

        if not self.region_selector.open(display_index, completed):
            messagebox.showerror("无法选择区域", "无法读取所选显示器的位置；请检查显示器编号。")


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
        style = EdgeStyle(
            enabled=bool(self.edge_enabled.get()),
            start_color=self.edge_start_color.get(),
            stop_color=self.edge_stop_color.get(),
            opacity=int(self.edge_opacity.get()),
            thickness=int(self.edge_thickness.get()),
            gradient=bool(self.edge_gradient.get()),
            duration=int(self.edge_duration.get()),
            sides={name: bool(value.get()) for name, value in self.edge_sides.items()},
        )
        self.edge_overlay.show(self.capture_idx, style, recording)


    def _hide_recording_edge(self) -> None:
        self.edge_overlay.hide()


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
        filter_available, encoder, error = probe
        if error:
            messagebox.showerror("编码器检查失败", error)
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
            display = int(self.display.get())
            if display < 0:
                raise ValueError("negative display index")
        except OSError as exc:
            messagebox.showerror("无法保存到此位置", str(exc))
            return
        except (ValueError, tk.TclError):
            messagebox.showerror("显示器编号无效", "请输入 0 或更大的整数。")
            return
        self.capture_idx = display
        options = RecordingOptions(
            ffmpeg=ffmpeg, output_dir=folder, codec=self.codec.get(), fps=int(self.fps.get()),
            total_bitrate=int(self.target_total_bitrate.get()), display=display,
            region=self.region, audio_enabled=bool(self.audio.get()),
            audio_source=self.audio_source.get(), audio_bitrate=int(self.audio_bitrate.get()),
            skip_unchanged=bool(self.skip_unchanged.get()),
        )
        error = self.recorder.start(options, encoder)
        if error:
            messagebox.showerror("无法开始录制", error)

    def _recording_started(self, encoder: str) -> None:
        self._set_busy(True, f"正在录制 · {encoder_label(encoder)}")
        self._show_recording_edge(True)

    def _recording_status(self, status: str) -> None:
        self.status.set(status)

    def _recording_stopping(self, requested: bool) -> None:
        self.stop_button.configure(state="disabled")
        if requested:
            self.status.set("正在保存视频，请稍候…")
            self._show_recording_edge(False)
        else:
            self.status.set("录制已中断，正在尝试保存…")
            self._hide_recording_edge()

    def _recording_finished(self, result: RecordingResult) -> None:
        if result.success:
            self._set_busy(False, result.saved_text or "准备就绪")
        else:
            self._set_busy(False, "录制遇到问题")
            message = result.message
            if result.audio_error:
                message += chr(10) + "Audio error: " + result.audio_error
            messagebox.showerror("录制遇到问题", message)
        if self.close_when_done:
            self._exit_app()









    def stop_recording(self) -> None:
        self.recorder.stop()


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
        exit_text = "停止并保存后退出程序" if self.recorder.process is not None else "退出程序"
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
        if self.recorder.process is not None:
            self.close_when_done = True
            if not self.recorder.finalizing:
                self.stop_recording()
            return
        self._exit_app()

    def _exit_app(self) -> None:
        self._hide_recording_edge()
        self.tray.stop()
        self._unregister_hotkeys()
        self.destroy()

    def _minimize_to_tray(self) -> None:
        self.tray.minimize()





    def _open_output_folder(self) -> None:
        folder = Path(self.output_dir.get()).expanduser()
        try:
            folder.mkdir(parents=True, exist_ok=True)
            os.startfile(str(folder))
        except (OSError, AttributeError) as exc:
            messagebox.showerror("无法打开保存位置", f"请检查文件夹路径：\n{folder}\n\n{exc}", parent=self)
    def _poll(self) -> None:
        self._poll_hotkeys()
        if self.hotkeys.ready.is_set() and not self.hotkey_ready_reported:
            self.hotkey_ready_reported = True
            self._refresh_hotkey_summary()
            if self.hotkeys.failures:
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
        recorder = self.recorder
        audio_error = (recorder.take_audio_error()
                       if recorder.audio_active and recorder.process and not recorder.finalizing else None)
        if audio_error and recorder.audio_active and recorder.process and not recorder.finalizing:
            recorder.audio_runtime_error = audio_error
            recorder.audio_active = False
            self.status.set(f"声音录制失败：{audio_error}；画面仍在录制")
        if recorder.process and recorder.started_at and recorder.process.poll() is None and not recorder.finalizing:
            if not recorder.audio_runtime_error:
                elapsed = int(time.perf_counter() - recorder.started_at)
                audio_status = " · 正在启动声音设备…" if recorder.audio_active and not recorder.audio_started.is_set() else ""
                self.status.set(
                    f"正在录制 · {encoder_label(recorder.current_encoder)} · "
                    f"{elapsed // 60}:{elapsed % 60:02d}{audio_status}"
                )
        self.after(250, self._poll)

    def _unregister_hotkeys(self) -> None:
        self.hotkeys.stop()



def run() -> None:
    LightRec().mainloop()
