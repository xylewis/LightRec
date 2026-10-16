"""Thread-owned Windows global hotkey registration and event queue."""

from __future__ import annotations

import ctypes
from ctypes import wintypes
import os
import queue
import threading

from .config import DEFAULT_HOTKEYS, parse_hotkey


class GlobalHotkeyManager:
    START, STOP, CLICK_THROUGH, CANCEL_REGION = 0x4C01, 0x4C02, 0x4C03, 0x4C04
    _REGISTER_ESCAPE = 0x8001
    _UNREGISTER_ESCAPE = 0x8002

    def __init__(self) -> None:
        self.ready = threading.Event()
        self.messages: queue.Queue[int] = queue.Queue()
        self.thread: threading.Thread | None = None
        self.thread_id: int | None = None
        self.registered_ids: list[int] = []
        self.failures: list[str] = []
        self.registered = False
        self._bindings = DEFAULT_HOTKEYS.copy()

    def start(self, bindings: dict[str, str], wait: bool = True) -> None:
        if os.name != "nt":
            return
        self.stop()
        self._bindings = bindings.copy()
        self.ready.clear()
        self.registered_ids.clear()
        self.failures.clear()
        self.thread = threading.Thread(target=self._listen, name="lightrec-hotkeys", daemon=True)
        self.thread.start()
        if wait:
            self.ready.wait(timeout=2)

    def poll(self) -> list[int]:
        events = []
        while True:
            try:
                events.append(self.messages.get_nowait())
            except queue.Empty:
                return events

    def set_region_cancel(self, enabled: bool) -> None:
        if os.name == "nt" and self.thread_id:
            message = self._REGISTER_ESCAPE if enabled else self._UNREGISTER_ESCAPE
            ctypes.windll.user32.PostThreadMessageW(self.thread_id, message, 0, 0)

    def stop(self) -> None:
        if os.name == "nt" and self.thread_id:
            ctypes.windll.user32.PostThreadMessageW(self.thread_id, 0x0012, 0, 0)
        if self.thread and self.thread.is_alive():
            self.thread.join(timeout=1)
        self.thread = None
        self.thread_id = None
        self.registered_ids.clear()
        self.registered = False

    def _listen(self) -> None:
        user32 = ctypes.windll.user32
        msg = wintypes.MSG()
        try:
            user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 0)
            self.thread_id = int(ctypes.windll.kernel32.GetCurrentThreadId())
            actions = tuple(DEFAULT_HOTKEYS)
            ids = (self.START, self.STOP, self.CLICK_THROUGH)
            for hotkey_id, action in zip(ids, actions):
                chord = parse_hotkey(self._bindings[action])
                if chord and user32.RegisterHotKey(None, hotkey_id, chord[0], chord[1]):
                    self.registered_ids.append(hotkey_id)
            self.failures = [self._bindings[action] for action, hotkey_id in zip(actions, ids)
                             if hotkey_id not in self.registered_ids]
            self.registered = len(self.registered_ids) == len(ids)
            self.ready.set()

            escape_registered = False
            while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                if msg.message == self._REGISTER_ESCAPE:
                    if not escape_registered:
                        escape_registered = bool(user32.RegisterHotKey(None, self.CANCEL_REGION, 0, 0x1B))
                elif msg.message == self._UNREGISTER_ESCAPE:
                    if escape_registered:
                        user32.UnregisterHotKey(None, self.CANCEL_REGION)
                        escape_registered = False
                elif msg.message == 0x0312:
                    self.messages.put(int(msg.wParam))
        except Exception:
            self.failures = list(self._bindings.values())
            self.registered = False
            self.ready.set()
        finally:
            user32.UnregisterHotKey(None, self.CANCEL_REGION)
            for hotkey_id in self.registered_ids:
                user32.UnregisterHotKey(None, hotkey_id)
