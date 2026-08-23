"""Screen-off for one session: the first strategy whose ``sleep()`` succeeds stays in charge (touch wake,
re-sleep and ``deactivate()`` all go through it), so the backlight is never touched while gamescope or
kscreen is."""
from __future__ import annotations

import threading
from typing import Callable, List, Optional

from deckgadget.config import DEFAULT_SCREEN_METHOD, SCREEN_METHODS
from deckgadget.platform.display.backlight import Backlight, BacklightDim
from deckgadget.platform.display.base import ScreenMethod
from deckgadget.platform.display.compositor import GamescopeSleep, KscreenDpms
from deckgadget.platform.display.touch import TouchWatcher, find_touchscreen
from deckgadget.util.log import get_logger

log = get_logger("screen")


class ScreenController:
    """``on_change(off, method)`` fires on every effective change, plus once with ``(False, "none")`` at
    activation when nothing could turn the screen off."""

    def __init__(self, backlight: Optional[Backlight] = None, touch_event: Optional[str] = None,
                 wake_seconds: float = 5.0, on_change: Optional[Callable[[bool, str], None]] = None,
                 sysfs: str = "/sys", dev: str = "/dev", method: str = DEFAULT_SCREEN_METHOD,
                 gamescope: Optional[ScreenMethod] = None, kscreen: Optional[ScreenMethod] = None) -> None:
        if method not in SCREEN_METHODS:
            raise ValueError(f"unknown screen method {method!r} (expected one of {SCREEN_METHODS})")
        self.backlight = backlight or Backlight()
        self.gamescope: ScreenMethod = gamescope if gamescope is not None else GamescopeSleep()
        self.kscreen: ScreenMethod = kscreen if kscreen is not None else KscreenDpms()
        self.backlight_method: ScreenMethod = BacklightDim(self.backlight)
        self.requested_method = method
        self.touch_event = touch_event if touch_event is not None else find_touchscreen(sysfs, dev)
        self.wake_seconds = wake_seconds
        self.on_change = on_change
        self._active = False
        self._off = False
        self._method: Optional[ScreenMethod] = None
        self._lock = threading.Lock()
        self._timer: Optional[threading.Timer] = None
        self._watcher: Optional[TouchWatcher] = None

    @property
    def active(self) -> bool:
        return self._active

    @property
    def is_off(self) -> bool:
        return self._off

    @property
    def method(self) -> str:
        """Name of the strategy in charge (``"none"`` before activation / when nothing worked)."""
        return self._method.name if self._method is not None else "none"

    def candidates(self) -> List[ScreenMethod]:
        """Strategies in the order they are tried for the requested method."""
        order = {"gamescope": self.gamescope, "kscreen": self.kscreen, "backlight": self.backlight_method}
        if self.requested_method == "auto":
            return [order["gamescope"], order["kscreen"], order["backlight"]]
        return [order[self.requested_method]]

    def activate(self) -> None:
        with self._lock:
            if self._active:
                return
            self._active = True
            self._method = self._choose_and_sleep()
            if self._method is not None:
                self._set_off(True)
            elif self.on_change:
                # Say so explicitly: the backend would otherwise infer screen_off from the settings.
                try:
                    self.on_change(False, "none")
                except Exception as exc:  # noqa: BLE001
                    log.warning("screen on_change callback failed: %s", exc)
            if self.touch_event:
                try:
                    self._watcher = TouchWatcher(self.touch_event, self._on_touch)
                    self._watcher.start()
                    log.info("touch wake armed on %s (%.1fs, method %s)", self.touch_event, self.wake_seconds,
                             self.method)
                except OSError as exc:
                    log.warning("touch wake unavailable (%s): %s", self.touch_event, exc)
                    self._watcher = None
            else:
                log.warning("touchscreen not found; touch wake disabled")

    def deactivate(self) -> None:
        with self._lock:
            was_active = self._active
            self._active = False
            if self._timer is not None:
                self._timer.cancel()
                self._timer = None
            watcher, self._watcher = self._watcher, None
            method = self._method
        if watcher is not None:
            watcher.stop()
        try:
            if method is not None:
                if not method.release():
                    log.warning("screen release via %s reported failure", method.name)
            elif was_active or self._off or self.backlight.saved_value() is not None:
                # Nothing was in charge, or a crashed backlight session left a state file: restore only what we saved.
                self.backlight.restore(forget=True)
        except Exception as exc:  # noqa: BLE001
            log.warning("cannot restore the screen: %s", exc)
        self._set_off(False)
        with self._lock:
            self._method = None

    def _set_off(self, off: bool) -> None:
        if off != self._off:
            self._off = off
            if self.on_change:
                try:
                    self.on_change(off, self.method)
                except Exception as exc:  # noqa: BLE001
                    log.warning("screen on_change callback failed: %s", exc)

    def _choose_and_sleep(self) -> Optional[ScreenMethod]:
        for method in self.candidates():
            try:
                if not method.available():
                    log.info("screen method %s not available", method.name)
                    continue
                if method.sleep():
                    log.info("screen off via %s", method.name)
                    return method
                log.warning("screen method %s could not turn the screen off", method.name)
            except Exception as exc:  # noqa: BLE001 - cosmetic feature, never fatal
                log.warning("screen method %s failed: %s", method.name, exc)
        log.warning("screen stays on: no working screen-off method (requested %s)", self.requested_method)
        return None

    def _on_touch(self) -> None:
        with self._lock:
            if not self._active:
                return
            if self._off and self._method is not None:
                try:
                    if self._method.wake():
                        self._set_off(False)
                    else:
                        log.warning("cannot wake the screen on touch (%s)", self._method.name)
                except Exception as exc:  # noqa: BLE001
                    log.warning("cannot wake the screen on touch: %s", exc)
            if self._timer is not None:
                self._timer.cancel()
            self._timer = threading.Timer(self.wake_seconds, self._wake_expired)
            self._timer.daemon = True
            self._timer.start()

    def _wake_expired(self) -> None:
        with self._lock:
            self._timer = None
            if not self._active or self._off or self._method is None:
                return
            try:
                if self._method.sleep():
                    self._set_off(True)
                else:
                    log.warning("cannot turn the screen off again (%s)", self._method.name)
            except Exception as exc:  # noqa: BLE001
                log.warning("cannot turn the screen off again: %s", exc)
