"""Settings window widget tree; behavior is supplied by the app controller."""

from __future__ import annotations

import tkinter as tk
from tkinter import ttk
from collections.abc import Callable

from ..config import AUDIO_BITRATE_CHOICES, FPS_CHOICES, TOTAL_BITRATE_CHOICES


class SettingsDialog:
    def __init__(self, parent: tk.Tk, fields: dict, *, ffmpeg_is_bundled: bool,
                 hotkeys: dict[str, str], conflicts: list[str],
                 on_apply: Callable[[], None], on_capture: Callable[[str], None],
                 on_color: Callable[[str], None], on_preview: Callable[[], None],
                 on_bitrate_change: Callable[..., None],
                 on_click_through: Callable[[], None]) -> None:
        self.parent = parent
        self.fields = fields
        self.ffmpeg_is_bundled = ffmpeg_is_bundled
        self.pending_hotkeys = hotkeys.copy()
        self.on_apply = on_apply
        self.on_capture = on_capture
        self.on_color = on_color
        self.on_preview = on_preview
        self.on_bitrate_change = on_bitrate_change
        self.on_click_through = on_click_through
        self.window: tk.Toplevel | None = None
        self.hotkey_labels: dict[str, tk.StringVar] = {}
        self.edge_color_labels: dict[str, ttk.Label] = {}
        self.hotkey_status: tk.StringVar | None = None

    def show(self) -> tk.Toplevel:
        window = tk.Toplevel(self.parent)
        self.window = window
        window.title("LightRec Settings")
        window.geometry("600x520")
        window.minsize(560, 470)
        window.transient(self.parent)
        body = ttk.Frame(window, padding=16)
        body.pack(fill="both", expand=True)

        footer = ttk.Frame(body)
        footer.pack(side="bottom", fill="x", pady=(12, 0))
        ttk.Label(footer, text="设置会自动保存在当前 Windows 用户下。", foreground="#666").pack(side="left")
        ttk.Button(footer, text="应用并关闭", command=self.on_apply).pack(side="right")

        tabs = ttk.Notebook(body)
        tabs.pack(side="top", fill="both", expand=True)

        self._add_recording_tab(tabs)
        self._add_edge_tab(tabs)
        self._add_hotkeys_tab(tabs)
        window.protocol("WM_DELETE_WINDOW", self.on_apply)
        window.bind("<Escape>", lambda _event: self.on_apply())
        return window

    def _add_recording_tab(self, tabs: ttk.Notebook) -> None:
        f = self.fields
        recording = ttk.Frame(tabs, padding=14)
        tabs.add(recording, text="录制设置")
        recording.columnconfigure(1, weight=1)
        self.on_bitrate_change()
        ttk.Label(recording, text="视频格式").grid(row=0, column=0, sticky="w", pady=6)
        ttk.Combobox(recording, textvariable=f["codec"], state="readonly",
                     values=("HEVC · 文件较小", "H.264 · 兼容性较好"), width=28).grid(
                         row=0, column=1, sticky="ew", padx=(12, 0), pady=6)
        ttk.Label(recording, text="帧率").grid(row=1, column=0, sticky="w", pady=6)
        ttk.Combobox(recording, textvariable=f["fps"], state="readonly",
                     values=FPS_CHOICES, width=10).grid(row=1, column=1, sticky="w", padx=(12, 0), pady=6)
        ttk.Label(recording, text="目标总码率").grid(row=2, column=0, sticky="w", pady=6)
        bitrate_row = ttk.Frame(recording)
        bitrate_row.grid(row=2, column=1, sticky="ew", padx=(12, 0), pady=6)
        rate_box = ttk.Combobox(bitrate_row, textvariable=f["target_total_bitrate"], state="readonly",
                                values=TOTAL_BITRATE_CHOICES, width=10)
        rate_box.pack(side="left")
        ttk.Label(bitrate_row, text=" kbps（含声音）").pack(side="left")
        rate_box.bind("<<ComboboxSelected>>", self.on_bitrate_change)
        ttk.Label(recording, textvariable=f["target_bitrate_text"], foreground="#666").grid(
            row=3, column=1, sticky="w", padx=(12, 0), pady=(0, 6))
        ttk.Label(recording, text="显示器").grid(row=4, column=0, sticky="w", pady=6)
        ttk.Spinbox(recording, from_=0, to=15, textvariable=f["display"], width=8).grid(
            row=4, column=1, sticky="w", padx=(12, 0), pady=6)
        ttk.Label(recording, text="声音码率").grid(row=5, column=0, sticky="w", pady=6)
        ttk.Combobox(recording, textvariable=f["audio_bitrate"], state="readonly",
                     values=AUDIO_BITRATE_CHOICES, width=10).grid(
                         row=5, column=1, sticky="w", padx=(12, 0), pady=6)
        ttk.Checkbutton(recording, text="跳过静止画面（输出可变帧率）",
                        variable=f["skip_unchanged"]).grid(row=6, column=0, columnspan=2,
                                                            sticky="w", pady=(8, 4))
        ttk.Checkbutton(recording, text="窗口穿透（点击穿过 LightRec 窗口）",
                        variable=f["click_through"], command=self.on_click_through).grid(
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
            ttk.Entry(path_row, textvariable=f["ffmpeg"]).pack(side="left", fill="x", expand=True)
            ttk.Button(path_row, text="浏览…", command=f["choose_ffmpeg"]).pack(side="left", padx=(8, 0))

    def _add_edge_tab(self, tabs: ttk.Notebook) -> None:
        f = self.fields
        edge = ttk.Frame(tabs, padding=14)
        tabs.add(edge, text="描边提醒")
        edge.columnconfigure(1, weight=1)
        ttk.Checkbutton(edge, text="录制开始和停止时显示描边", variable=f["edge_enabled"]).grid(
            row=0, column=0, columnspan=3, sticky="w", pady=(0, 10))
        for row, (name, title, variable) in enumerate((
            ("start", "开始录制颜色", f["edge_start_color"]),
            ("stop", "停止录制颜色", f["edge_stop_color"]),
        ), start=1):
            ttk.Label(edge, text=title).grid(row=row, column=0, sticky="w", pady=5)
            label = ttk.Label(edge, text=variable.get(), width=12, anchor="center")
            label.grid(row=row, column=1, sticky="w", padx=(12, 4))
            self.edge_color_labels[name] = label
            ttk.Button(edge, text="选择颜色…", command=lambda key=name: self.on_color(key)).grid(
                row=row, column=2, sticky="e")
        ttk.Label(edge, text="不透明度").grid(row=3, column=0, sticky="w", pady=5)
        ttk.Scale(edge, from_=5, to=100, variable=f["edge_opacity"], orient="horizontal").grid(
            row=3, column=1, sticky="ew", padx=(12, 6))
        ttk.Label(edge, textvariable=f["edge_opacity"], width=4).grid(row=3, column=2, sticky="e")
        ttk.Label(edge, text="粗细").grid(row=4, column=0, sticky="w", pady=5)
        ttk.Spinbox(edge, from_=1, to=16, textvariable=f["edge_thickness"], width=7).grid(
            row=4, column=1, sticky="w", padx=(12, 0))
        ttk.Label(edge, text="px").grid(row=4, column=2, sticky="w")
        ttk.Checkbutton(edge, text="两端渐隐（透明渐变）", variable=f["edge_gradient"]).grid(
            row=5, column=0, columnspan=3, sticky="w", pady=5)
        ttk.Label(edge, text="显示位置").grid(row=6, column=0, sticky="w", pady=5)
        sides = ttk.Frame(edge)
        sides.grid(row=6, column=1, columnspan=2, sticky="w", padx=(12, 0))
        for side, label in (("top", "上"), ("bottom", "下"), ("left", "左"), ("right", "右")):
            ttk.Checkbutton(sides, text=label, variable=f["edge_sides"][side]).pack(side="left", padx=(0, 10))
        ttk.Label(edge, text="提示时长").grid(row=7, column=0, sticky="w", pady=5)
        ttk.Spinbox(edge, from_=1, to=10, textvariable=f["edge_duration"], width=7).grid(
            row=7, column=1, sticky="w", padx=(12, 0))
        ttk.Label(edge, text="秒").grid(row=7, column=2, sticky="w")
        ttk.Button(edge, text="预览开始提示", command=self.on_preview).grid(
            row=8, column=0, columnspan=3, sticky="w", pady=(10, 0))

    def _add_hotkeys_tab(self, tabs: ttk.Notebook) -> None:
        hotkeys = ttk.Frame(tabs, padding=14)
        tabs.add(hotkeys, text="快捷键")
        ttk.Label(hotkeys, text="点击“录入”后按下快捷键：可用 Ctrl/Alt 组合键，也可单独使用 F1–F12。功能键可能与 Windows 或其他软件冲突；若无法注册，会在这里提示。",
                  wraplength=490).pack(anchor="w", pady=(0, 12))
        for action, label in (("start", "开始录制"), ("stop", "停止并保存"),
                              ("click_through", "切换窗口穿透")):
            row = ttk.Frame(hotkeys)
            row.pack(fill="x", pady=6)
            ttk.Label(row, text=label, width=18).pack(side="left")
            value = tk.StringVar(value=self.pending_hotkeys[action])
            self.hotkey_labels[action] = value
            ttk.Label(row, textvariable=value, width=22).pack(side="left", padx=8)
            ttk.Button(row, text="录入…", command=lambda name=action: self.on_capture(name)).pack(side="right")
        message = ("当前未注册，可能被 Windows 或其他程序占用：" + "、".join(self.pending_hotkeys.values())
                   if self.conflicts else "")
        self.hotkey_status = tk.StringVar(value=message)
        ttk.Label(hotkeys, textvariable=self.hotkey_status, foreground="#9a4a00",
                  wraplength=490).pack(anchor="w", pady=(12, 0))

