"""Demo input source: sine sticks, blinking A, cycling D-pad — for ``deckgadget demo``.

Lets the transport path (Deck -> cable -> PC) be verified without capturing the real
controller (the same demo pattern the spikes used: left stick circles, A blinks).
"""
from __future__ import annotations

import math
import time
from typing import Optional

from .. import state as S
from ..state import ControllerState
from ..util.log import get_logger

log = get_logger("demo")

_DPAD_SEQ = (S.BTN_DPAD_UP, S.BTN_DPAD_RIGHT, S.BTN_DPAD_DOWN, S.BTN_DPAD_LEFT)


class DemoSource:
    name = "demo"

    def __init__(self, hz: float = 250.0, amplitude: int = 30000, clock=time.monotonic, sleep=time.sleep) -> None:
        self.period = 1.0 / hz
        self.amplitude = amplitude
        self._clock = clock
        self._sleep = sleep
        self._t0 = 0.0
        self._next = 0.0
        self._packet = 0
        self.opened = False

    def open(self) -> None:
        self._t0 = self._clock()
        self._next = self._t0
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
        t = now - self._t0
        self._packet += 1
        buttons = S.BTN_A if int(t * 2) % 2 == 0 else 0
        buttons |= _DPAD_SEQ[int(t) % 4] if int(t / 4) % 2 else 0
        tri = t % 2.0
        trig = int((tri if tri < 1.0 else 2.0 - tri) * S.TRIGGER_MAX)
        return ControllerState(
            buttons=buttons,
            lx=int(self.amplitude * math.sin(t)), ly=int(self.amplitude * math.cos(t)),
            rx=int(self.amplitude * math.sin(t * 0.5)), ry=int(self.amplitude * math.cos(t * 0.5)),
            lt=trig, rt=S.TRIGGER_MAX - trig,
            packet=self._packet, ts=now,
        )

    def rumble(self, left: int, right: int) -> None:
        log.info("demo rumble left=%d right=%d", left, right)

    def close(self) -> None:
        self.opened = False
