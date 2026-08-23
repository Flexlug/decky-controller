"""Canonical, source-agnostic controller state. Button bits are this package's own numbering, not the
Deck wire format (that lives in ``sources/neptune_usb.py``)."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, Optional, Tuple

BTN_A = 1 << 0
BTN_B = 1 << 1
BTN_X = 1 << 2
BTN_Y = 1 << 3
BTN_L1 = 1 << 4          # left bumper (LB)
BTN_R1 = 1 << 5          # right bumper (RB)
BTN_L2 = 1 << 6          # left trigger full press (digital)
BTN_R2 = 1 << 7          # right trigger full press (digital)
BTN_L3 = 1 << 8
BTN_R3 = 1 << 9
BTN_VIEW = 1 << 10       # "Back" on Xbox
BTN_MENU = 1 << 11       # "Start" on Xbox
BTN_STEAM = 1 << 12      # "Guide" on Xbox
BTN_QAM = 1 << 13        # "..." quick-access button
BTN_L4 = 1 << 14         # back paddles
BTN_L5 = 1 << 15
BTN_R4 = 1 << 16
BTN_R5 = 1 << 17
BTN_DPAD_UP = 1 << 18
BTN_DPAD_DOWN = 1 << 19
BTN_DPAD_LEFT = 1 << 20
BTN_DPAD_RIGHT = 1 << 21
BTN_LPAD_CLICK = 1 << 22
BTN_RPAD_CLICK = 1 << 23
BTN_LPAD_TOUCH = 1 << 24
BTN_RPAD_TOUCH = 1 << 25
BTN_LSTICK_TOUCH = 1 << 26
BTN_RSTICK_TOUCH = 1 << 27

DPAD_MASK = BTN_DPAD_UP | BTN_DPAD_DOWN | BTN_DPAD_LEFT | BTN_DPAD_RIGHT

#: stable names: used by settings (kill_combo / paddles) and probe output
BUTTON_NAMES: Dict[int, str] = {
    BTN_A: "A", BTN_B: "B", BTN_X: "X", BTN_Y: "Y",
    BTN_L1: "L1", BTN_R1: "R1", BTN_L2: "L2", BTN_R2: "R2",
    BTN_L3: "L3", BTN_R3: "R3",
    BTN_VIEW: "VIEW", BTN_MENU: "MENU", BTN_STEAM: "STEAM", BTN_QAM: "QAM",
    BTN_L4: "L4", BTN_L5: "L5", BTN_R4: "R4", BTN_R5: "R5",
    BTN_DPAD_UP: "DPAD_UP", BTN_DPAD_DOWN: "DPAD_DOWN",
    BTN_DPAD_LEFT: "DPAD_LEFT", BTN_DPAD_RIGHT: "DPAD_RIGHT",
    BTN_LPAD_CLICK: "LPAD_CLICK", BTN_RPAD_CLICK: "RPAD_CLICK",
    BTN_LPAD_TOUCH: "LPAD_TOUCH", BTN_RPAD_TOUCH: "RPAD_TOUCH",
    BTN_LSTICK_TOUCH: "LSTICK_TOUCH", BTN_RSTICK_TOUCH: "RSTICK_TOUCH",
}

#: name -> bit, plus a few aliases used in settings (LB/RB are Xbox names for L1/R1)
BUTTON_BY_NAME: Dict[str, int] = {name: bit for bit, name in BUTTON_NAMES.items()}
BUTTON_BY_NAME.update({"LB": BTN_L1, "RB": BTN_R1, "BACK": BTN_VIEW, "START": BTN_MENU, "GUIDE": BTN_STEAM})


def button_names(mask: int) -> list:
    """Names of all canonical bits set in ``mask`` (bit order)."""
    return [BUTTON_NAMES[bit] for bit in sorted(BUTTON_NAMES) if mask & bit]


def button_from_name(name: str) -> int:
    """Canonical bit for a button name (case-insensitive). Raises ValueError if unknown."""
    try:
        return BUTTON_BY_NAME[name.strip().upper()]
    except KeyError:
        raise ValueError(f"unknown button name {name!r}") from None


# Axis ranges used throughout: sticks are signed 16-bit, triggers 0..TRIGGER_MAX
# (the Deck reports raw triggers as 0..32767, see SDL_hidapi_steamdeck.c: "sTriggerRawL * 2 - 32768").
STICK_MIN = -32768
STICK_MAX = 32767
TRIGGER_MAX = 32767


def clamp_s16(value: int) -> int:
    return STICK_MIN if value < STICK_MIN else STICK_MAX if value > STICK_MAX else int(value)


def clamp_trigger(value: int) -> int:
    return 0 if value < 0 else TRIGGER_MAX if value > TRIGGER_MAX else int(value)


@dataclass(slots=True)
class ControllerState:
    """Sticks are signed 16-bit with **+Y = up** (Deck native and XInput convention); triggers 0..TRIGGER_MAX;
    pads are ``(x, y, pressure)`` or ``None``; ``ts`` is a monotonic timestamp."""

    buttons: int = 0
    lx: int = 0
    ly: int = 0
    rx: int = 0
    ry: int = 0
    lt: int = 0
    rt: int = 0
    lpad: Optional[Tuple[int, int, int]] = None
    rpad: Optional[Tuple[int, int, int]] = None
    gyro: Optional[Tuple[int, int, int]] = None
    accel: Optional[Tuple[int, int, int]] = None
    packet: int = 0
    ts: float = 0.0

    def pressed(self, mask: int) -> bool:
        """True when *all* bits of ``mask`` are pressed."""
        return (self.buttons & mask) == mask

    def as_dict(self) -> dict:
        return {
            "buttons": self.buttons,
            "names": button_names(self.buttons),
            "lx": self.lx, "ly": self.ly, "rx": self.rx, "ry": self.ry,
            "lt": self.lt, "rt": self.rt,
            "lpad": self.lpad, "rpad": self.rpad,
            "gyro": self.gyro, "accel": self.accel,
            "packet": self.packet,
        }
