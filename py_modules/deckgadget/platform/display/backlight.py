"""Backlight fallback: ``amdgpu_bl0/brightness`` = 0. On the OLED this only dims to the minimum, so it is the
last resort; the previous value is written to a state file first so ``guard.recover`` can restore it after a
crash."""
from __future__ import annotations

import os
from typing import Dict, Optional

from deckhw.sysfs import Sysfs

from ...util.fs import read_text, write_text
from ...util.log import get_logger
from .base import ScreenMethod, default_state_file

log = get_logger("screen")

BACKLIGHT_DIR = "/sys/class/backlight/amdgpu_bl0"


class Backlight:
    """Save / turn off / restore brightness for one backlight device."""

    def __init__(self, backlight_dir: str = BACKLIGHT_DIR, state_file: Optional[str] = None) -> None:
        self.directory = backlight_dir
        self.state_file = state_file or default_state_file()
        self._saved: Optional[int] = None
        self._sysfs = Sysfs(backlight_dir)

    @property
    def available(self) -> bool:
        return self._sysfs.exists("brightness")

    def brightness(self) -> Optional[int]:
        return self._sysfs.int("brightness")

    def max_brightness(self) -> int:
        return max(1, self._sysfs.int("max_brightness", default=255))

    def set_brightness(self, value: int) -> None:
        write_text(os.path.join(self.directory, "brightness"), str(int(value)))

    def saved_value(self) -> Optional[int]:
        """Value remembered in memory or in the state file (``None`` when nothing saved)."""
        if self._saved is not None:
            return self._saved
        text = read_text(self.state_file)
        try:
            return int(text) if text else None
        except ValueError:
            log.warning("ignoring corrupt backlight state file %s: %r", self.state_file, text)
            return None

    def _safe_value(self, saved: Optional[int]) -> int:
        # Never "restore" to 0 — that would leave the Deck dark.
        if saved is None or saved <= 0:
            return max(1, self.max_brightness() // 2)
        return saved

    def save_and_off(self) -> bool:
        """Save the current brightness and switch the backlight off; ``False`` when there is no
        backlight device (the caller must not report the screen as off then)."""
        if not self.available:
            log.warning("backlight %s not available; screen off skipped", self.directory)
            return False
        current = self.brightness()
        previously_saved = self.saved_value()
        # Keep an earlier saved value if the current one is 0 (e.g. we crashed mid-session).
        value = current if current and current > 0 else (previously_saved if previously_saved else self._safe_value(None))
        try:
            os.makedirs(os.path.dirname(self.state_file), exist_ok=True)
            write_text(self.state_file, str(value))
        except OSError as exc:
            log.warning("cannot persist brightness to %s: %s", self.state_file, exc)
        self._saved = value
        self.set_brightness(0)
        log.info("backlight off (saved brightness %d)", value)
        return True

    def off(self) -> None:
        if self.available:
            self.set_brightness(0)

    def restore(self, forget: bool = True) -> Optional[int]:
        """Write the saved brightness back; returns the value written (``None`` if nothing to do)."""
        if not self.available:
            return None
        saved = self.saved_value()
        if saved is None:
            return None  # nothing saved by us: leave the user's brightness alone
        value = self._safe_value(saved)
        self.set_brightness(value)
        if forget:
            self._saved = None
            try:
                os.unlink(self.state_file)
            except OSError as exc:
                log.debug("cannot remove %s: %s", self.state_file, exc)
        log.info("backlight restored to %d", value)
        return value


class BacklightDim(ScreenMethod):
    """Fallback strategy: backlight brightness 0, restored from the state file."""

    name = "backlight"

    def __init__(self, backlight: Optional[Backlight] = None) -> None:
        self.backlight = backlight or Backlight()
        self._engaged = False

    def available(self) -> bool:
        return self.backlight.available

    def sleep(self) -> bool:
        if not self._engaged:
            ok = self.backlight.save_and_off()
            self._engaged = ok
            return ok
        self.backlight.off()   # re-sleep after a touch wake: saved value stays
        return self.backlight.available

    def wake(self) -> bool:
        if not self.backlight.available:
            return False
        self.backlight.restore(forget=False)
        return True

    def release(self) -> bool:
        self._engaged = False
        if not self.backlight.available:
            return False
        self.backlight.restore(forget=True)
        return True

    def info(self) -> Dict[str, object]:
        return {"available": self.available(), "dir": self.backlight.directory}
