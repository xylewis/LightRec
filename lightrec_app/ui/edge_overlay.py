"""Recording start/stop edge indicator windows."""

from __future__ import annotations

import ctypes
import os
import tkinter as tk
from dataclasses import dataclass

from .monitor import monitor_bounds


@dataclass(frozen=True)
class EdgeStyle:
    enabled: bool
    start_color: str
    stop_color: str
    opacity: int
    thickness: int
    gradient: bool
    duration: int
    sides: dict[str, bool]


class RecordingEdgeOverlay:
    def __init__(self, parent: tk.Tk) -> None:
        self.parent = parent
        self.windows: list[tk.Toplevel] = []
        self.after_id: str | None = None

    def show(self, display_index: int, style: EdgeStyle, recording: bool) -> None:
        self.hide()
        if not style.enabled:
            return
        bounds = monitor_bounds(display_index)
        if not bounds:
            bounds = (0, 0, self.parent.winfo_screenwidth(), self.parent.winfo_screenheight())
        x, y, width, height = bounds
        thickness = max(1, min(16, int(style.thickness)))
        opacity = max(5, min(100, int(style.opacity))) / 100
        color = style.start_color if recording else style.stop_color
        sides = (
            ("top", max(1, width), thickness, x, y, True),
            ("bottom", max(1, width), thickness, x, y + height - thickness, True),
            ("left", max(1, height - 2 * thickness), thickness, x, y + thickness, False),
            ("right", max(1, height - 2 * thickness), thickness, x + width - thickness, y + thickness, False),
        )
        segments = 24 if style.gradient else 1
        for side, length, band, origin_x, origin_y, horizontal in sides:
            if not style.sides.get(side, True):
                continue
            for index in range(segments):
                start = round(length * index / segments)
                end = round(length * (index + 1) / segments)
                if end <= start:
                    continue
                segment_alpha = opacity
                if segments > 1:
                    midpoint = (index + 0.5) / segments
                    segment_alpha *= max(0.04, 1 - abs(midpoint * 2 - 1))
                if horizontal:
                    geometry = (end - start, band, origin_x + start, origin_y)
                else:
                    geometry = (band, end - start, origin_x, origin_y + start)
                edge_width, edge_height, edge_x, edge_y = geometry
                try:
                    window = tk.Toplevel(self.parent)
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
                        flags = get_style(hwnd, -20) | 0x00080000 | 0x00000020 | 0x00000080
                        set_style(hwnd, -20, flags)
                        user32.SetWindowPos(hwnd, None, 0, 0, 0, 0,
                                            0x0001 | 0x0002 | 0x0004 | 0x0020)
                    self.windows.append(window)
                except tk.TclError:
                    continue
        self.after_id = self.parent.after(max(1, int(style.duration)) * 1000, self.hide)

    def hide(self) -> None:
        if self.after_id:
            try:
                self.parent.after_cancel(self.after_id)
            except tk.TclError:
                pass
            self.after_id = None
        for window in self.windows:
            try:
                if window.winfo_exists():
                    window.destroy()
            except tk.TclError:
                pass
        self.windows.clear()
