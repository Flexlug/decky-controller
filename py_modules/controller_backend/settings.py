"""Plugin settings: allowed values, sanitising of partial updates, the JSON store."""
from __future__ import annotations

import copy
import json
import logging
import os
from typing import Any, Optional

from controller_backend.daemon.events import JsonDict

log = logging.getLogger("controller_backend.settings")


PROFILES = ("xbox360", "hid_gamepad")
TRANSPORTS = ("auto", "raw", "hid")
KILL_COMBOS = ("L4+R4", "L5+R5", "L4+L5+R4+R5", "STEAM+QAM")
PADDLES = ("L4", "L5", "R4", "R5")
PADDLE_ACTIONS = ("none", "A", "B", "X", "Y", "LB", "RB", "L3", "R3", "VIEW", "MENU",
                  "DPAD_UP", "DPAD_DOWN", "DPAD_LEFT", "DPAD_RIGHT")
KILL_HOLD_MS_RANGE = (200, 10_000)
TOUCH_WAKE_RANGE = (1, 60)

DEFAULT_SETTINGS: JsonDict = {
    "profile": "xbox360",
    "transport": "auto",
    "kill_combo": "L4+R4",
    "kill_hold_ms": 1500,
    "screen_off": True,
    "touch_wake_seconds": 5,
    "paddles": {"L4": "none", "L5": "none", "R4": "none", "R5": "none"},
}


def sanitize_settings(partial: Any, base: JsonDict) -> tuple[JsonDict, list[str]]:
    """Merge ``partial`` onto ``base``: invalid values are skipped (previous kept) and reported in the
    returned warnings, integers are clamped to their range, unknown keys are ignored."""
    merged = copy.deepcopy(base)
    warnings: list[str] = []
    if partial is None:
        return merged, warnings
    if not isinstance(partial, dict):
        return merged, ["settings must be a JSON object"]

    def choice(key: str, allowed: tuple[str, ...]) -> None:
        if key in partial:
            value = partial[key]
            if isinstance(value, str) and value in allowed:
                merged[key] = value
            else:
                warnings.append(f"{key}: {value!r} is not one of {list(allowed)}")

    def integer(key: str, low: int, high: int) -> None:
        if key in partial:
            value = partial[key]
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                warnings.append(f"{key}: {value!r} is not a number")
            else:
                merged[key] = int(min(high, max(low, value)))

    choice("profile", PROFILES)
    choice("transport", TRANSPORTS)
    choice("kill_combo", KILL_COMBOS)
    integer("kill_hold_ms", *KILL_HOLD_MS_RANGE)
    integer("touch_wake_seconds", *TOUCH_WAKE_RANGE)
    if "screen_off" in partial:
        if isinstance(partial["screen_off"], bool):
            merged["screen_off"] = partial["screen_off"]
        else:
            warnings.append("screen_off: must be a boolean")
    if "paddles" in partial:
        paddles = partial["paddles"]
        if isinstance(paddles, dict):
            for name, action in paddles.items():
                if name in PADDLES and isinstance(action, str) and action in PADDLE_ACTIONS:
                    merged["paddles"][name] = action
                else:
                    warnings.append(f"paddles.{name}: {action!r} is not a valid paddle action")
        else:
            warnings.append("paddles: must be an object")
    return merged, warnings


def resolve_transport(profile: str, transport: str) -> str:
    """``auto`` → raw for xbox360 (vendor descriptors need raw-gadget), hid for hid_gamepad (configfs f_hid);
    xbox360 over hid is rejected, as the daemon's config.resolve_transport does."""
    if transport == "auto":
        return "raw" if profile == "xbox360" else "hid"
    if profile == "xbox360" and transport == "hid":
        raise ValueError("profile xbox360 requires transport raw (configfs f_hid cannot emulate XInput)")
    return transport


class SettingsStore:
    """``settings.json``, sanitized on load, written atomically; a corrupt file yields the defaults. The file
    is re-read whenever its mtime changes, so a hand edit of the file is picked up and never
    overwritten by the next UI change."""

    def __init__(self, path: str) -> None:
        self.path = path
        self._cached: Optional[JsonDict] = None
        self._cached_mtime: Optional[int] = None

    def load(self) -> JsonDict:
        mtime = self._mtime()
        if self._cached is None or mtime != self._cached_mtime:
            data: Any = None
            try:
                with open(self.path, encoding="utf-8") as settings_file:
                    data = json.load(settings_file)
            except FileNotFoundError:
                log.debug("%s does not exist yet — defaults", self.path)
            except (OSError, ValueError) as exc:
                log.warning("%s unreadable (%s) — using defaults", self.path, exc)
            settings, warnings = sanitize_settings(data if isinstance(data, dict) else {},
                                                   copy.deepcopy(DEFAULT_SETTINGS))
            for warning in warnings:
                log.warning("%s: %s (ignored)", self.path, warning)
            self._cached, self._cached_mtime = settings, mtime
        return copy.deepcopy(self._cached)

    def save(self, settings: JsonDict) -> None:
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        temp_path = f"{self.path}.tmp"
        with open(temp_path, "w", encoding="utf-8") as settings_file:
            json.dump(settings, settings_file, indent=2, sort_keys=True)
            settings_file.write("\n")
        os.replace(temp_path, self.path)
        self._cached, self._cached_mtime = copy.deepcopy(settings), self._mtime()

    def _mtime(self) -> Optional[int]:
        try:
            return os.stat(self.path).st_mtime_ns
        except OSError:
            return None

    def update(self, partial: Any) -> tuple[JsonDict, list[str]]:
        merged, warnings = sanitize_settings(partial, self.load())
        self.save(merged)
        return copy.deepcopy(merged), warnings
