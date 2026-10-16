"""System tray lifecycle, menu, and app-window callbacks."""

from __future__ import annotations

import tkinter as tk
import threading
from typing import Callable

from .config import APP_NAME, resource_path


class TrayController:
    def __init__(self, root: tk.Tk, on_exit: Callable[[], None]) -> None:
        self.root = root
        self.on_exit = on_exit
        self.icon = None
        self.thread: threading.Thread | None = None

    def minimize(self) -> None:
        if self.icon is None:
            import pystray
            from PIL import Image, ImageDraw

            try:
                with Image.open(resource_path("assets/onenote-tray.ico")) as source:
                    image = source.convert("RGBA").resize((64, 64), Image.Resampling.LANCZOS)
            except (OSError, ValueError):
                image = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
                draw = ImageDraw.Draw(image)
                draw.rounded_rectangle((5, 5, 59, 59), radius=12, fill="#168A5B")
                draw.rectangle((19, 17, 45, 43), outline="white", width=4)
                draw.line((24, 48, 40, 48), fill="white", width=4)
            menu = pystray.Menu(
                pystray.MenuItem("打开 LightRec", self._restore, default=True),
                pystray.MenuItem("退出程序", self._exit),
            )
            self.icon = pystray.Icon(APP_NAME, image, APP_NAME, menu)
            self.thread = threading.Thread(target=self.icon.run, name="lightrec-tray", daemon=True)
            self.thread.start()
        self.root.withdraw()

    def restore(self, *_args) -> None:
        self.root.after(0, self._show_window)

    def _restore(self, _icon=None, _item=None) -> None:
        self.restore()

    def _show_window(self) -> None:
        self.root.deiconify()
        self.root.state("normal")
        self.root.lift()
        self.root.focus_force()

    def _exit(self, _icon=None, _item=None) -> None:
        self.root.after(0, self.on_exit)

    def stop(self) -> None:
        if self.icon:
            try:
                self.icon.stop()
            except Exception:
                pass
            self.icon = None
