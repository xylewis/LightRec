"""Application preferences and hotkey value helpers."""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path


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


def write_settings(values: dict) -> None:
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(values, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def saved_choice(saved: dict, name: str, choices, default):
    value = saved.get(name, default)
    return value if value in choices else default


def saved_int(saved: dict, name: str, choices, default: int) -> int:
    try:
        value = int(saved.get(name, default))
    except (TypeError, ValueError):
        return default
    return value if value in choices else default


def saved_color(saved: dict, name: str, default: str) -> str:
    value = saved.get(name, default)
    return value if isinstance(value, str) and re.fullmatch(r"#[0-9A-Fa-f]{6}", value) else default


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


def resource_path(relative: str) -> Path:
    """Resolve packaged data beside the executable or source checkout."""
    if getattr(sys, "frozen", False):
        base = Path(getattr(sys, "_MEIPASS", Path(sys.executable).resolve().parent))
    else:
        base = Path(__file__).resolve().parent.parent
    return base / relative
