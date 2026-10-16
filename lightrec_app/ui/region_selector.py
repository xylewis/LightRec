"""Interactive full-monitor region selection overlay."""

from __future__ import annotations

import tkinter as tk
from collections.abc import Callable

from .monitor import monitor_bounds


Region = tuple[int, int, int, int]


class RegionSelector:
    def __init__(self, parent: tk.Tk, set_escape_hotkey: Callable[[bool], None]) -> None:
        self.parent = parent
        self.set_escape_hotkey = set_escape_hotkey
        self.overlay: tk.Toplevel | None = None
        self.on_complete: Callable[[Region | None, bool], None] | None = None
        self.start: tuple[int, int] | None = None
        self.rectangle: int | None = None
        self.canvas: tk.Canvas | None = None

    def open(self, display_index: int,
             on_complete: Callable[[Region | None, bool], None]) -> bool:
        if self.overlay is not None:
            self.overlay.lift()
            return True
        bounds = monitor_bounds(display_index)
        if not bounds:
            return False
        left, top, width, height = bounds
        self.on_complete = on_complete
        overlay = tk.Toplevel(self.parent)
        self.overlay = overlay
        overlay.title("LightRec - Select Region")
        overlay.geometry(f"{width}x{height}{left:+d}{top:+d}")
        overlay.attributes("-fullscreen", True)
        overlay.attributes("-topmost", True)
        overlay.configure(bg="black")
        overlay.attributes("-alpha", 0.28)
        canvas = tk.Canvas(overlay, bg="black", highlightthickness=0, cursor="crosshair")
        self.canvas = canvas
        canvas.pack(fill="both", expand=True)
        canvas.create_text(18, 18, anchor="nw", fill="white",
                           text="拖动鼠标框选录制区域；按 Esc 取消")
        self.parent.withdraw()
        canvas.bind("<Button-1>", self._begin)
        canvas.bind("<B1-Motion>", self._move)
        canvas.bind("<ButtonRelease-1>", self._finish)
        overlay.bind("<Escape>", lambda _event: self.cancel())
        canvas.bind("<Escape>", lambda _event: self.cancel())
        overlay.protocol("WM_DELETE_WINDOW", self.cancel)
        self.set_escape_hotkey(True)
        overlay.focus_force()
        canvas.focus_set()
        return True

    def cancel(self) -> None:
        self._close(None, False)

    def _begin(self, event) -> None:
        self.start = (event.x, event.y)
        if self.canvas is None:
            return
        if self.rectangle is not None:
            self.canvas.delete(self.rectangle)
        self.rectangle = self.canvas.create_rectangle(
            event.x, event.y, event.x, event.y, outline="#00e5ff", width=3,
            fill="#00e5ff", stipple="gray25")

    def _move(self, event) -> None:
        if self.start and self.rectangle is not None and self.canvas is not None:
            self.canvas.coords(self.rectangle, *self.start, event.x, event.y)

    def _finish(self, event) -> None:
        if not self.start:
            return
        x1, x2 = sorted((self.start[0], event.x))
        y1, y2 = sorted((self.start[1], event.y))
        x1 &= ~1
        y1 &= ~1
        width = (x2 - x1) & ~1
        height = (y2 - y1) & ~1
        valid = width >= 64 and height >= 64
        region = (x1, y1, width, height) if valid else None
        self._close(region, not valid)

    def _close(self, region: Region | None, too_small: bool) -> None:
        overlay = self.overlay
        if overlay is None:
            return
        self.overlay = None
        self.canvas = None
        self.rectangle = None
        self.start = None
        callback = self.on_complete
        self.on_complete = None
        try:
            overlay.destroy()
        except tk.TclError:
            pass
        self.parent.deiconify()
        self.parent.lift()
        self.set_escape_hotkey(False)
        if callback:
            callback(region, too_small)
