"""Synthetic input (sine sticks, blinking A, cycling D-pad): exercises the transport path without
capturing the real controller."""
from __future__ import annotations

import math
import time
from typing import Optional

from deckgadget import state as S
from deckgadget.state import ControllerState
from deckgadget.util.log import get_logger

log = get_logger("demo")

_DPAD_SEQ = (S.BTN_DPAD_UP, S.BTN_DPAD_RIGHT, S.BTN_DPAD_DOWN, S.BTN_DPAD_LEFT)


class DemoSource:
    name = "demo"

    def __init__(self, hz: float = 250.0, amplitude: int = 30000, clock=time.monotonic, sleep=time.sleep) -> None:
        self.period = 1.0 / hz
        self.amplitude = amplitude
        self._clock = clock
        self._sleep = sleep
        self._started_at = 0.0
        self._next = 0.0
        self._packet = 0
        self.opened = False

    def open(self) -> None:
        self._started_at = self._clock()
        self._next = self._started_at
        self.opened = True
        log.info("demo source: %.0f Hz, sticks circling, A blinking", 1.0 / self.period)

    def read(self, timeout: float) -> Optional[ControllerState]:
        now = self._clock()
        wait = self._next - now
        if wait > 0:
            if wait > timeout:
                self._sleep(timeout)
                return None
            self._sleep(wait)
            now = self._clock()
        self._next += self.period
        if self._next < now - 1.0:  # we fell far behind: resync instead of bursting
            self._next = now + self.period
        elapsed = now - self._started_at
        self._packet += 1
        buttons = S.BTN_A if int(elapsed * 2) % 2 == 0 else 0
        buttons |= _DPAD_SEQ[int(elapsed) % 4] if int(elapsed / 4) % 2 else 0
        phase = elapsed % 2.0
        trigger = int((phase if phase < 1.0 else 2.0 - phase) * S.TRIGGER_MAX)
        return ControllerState(
            buttons=buttons,
            lx=int(self.amplitude * math.sin(elapsed)), ly=int(self.amplitude * math.cos(elapsed)),
            rx=int(self.amplitude * math.sin(elapsed * 0.5)), ry=int(self.amplitude * math.cos(elapsed * 0.5)),
            lt=trigger, rt=S.TRIGGER_MAX - trigger,
            packet=self._packet, ts=now,
        )

    def rumble(self, left: int, right: int) -> None:
        log.info("demo rumble left=%d right=%d", left, right)

    def close(self) -> None:
        self.opened = False
