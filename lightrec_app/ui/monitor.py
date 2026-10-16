"""Windows monitor geometry helpers."""

from __future__ import annotations

import ctypes
from ctypes import wintypes
import os


def monitor_bounds(index: int) -> tuple[int, int, int, int] | None:
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
            rect = info.rcMonitor
            bounds.append((rect.left, rect.top, rect.right - rect.left, rect.bottom - rect.top))
        return True

    callback_ref = callback_type(callback)
    ctypes.windll.user32.EnumDisplayMonitors(None, None, callback_ref, 0)
    return bounds[index] if 0 <= index < len(bounds) else None
