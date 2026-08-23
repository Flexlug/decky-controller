"""Run configuration: parsing/validation of the CLI options shared by ``run``/``demo``.

Mirrors the ``Settings`` object of docs/ARCHITECTURE.md (profile, transport, kill_combo,
kill_hold_ms, screen_off, touch_wake_seconds, paddles) and resolves ``transport=auto``.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, Optional

from . import state as S

PROFILES = ("xbox360", "hid_gamepad")
TRANSPORTS = ("auto", "raw", "hid")
KILL_COMBOS = ("L4+R4", "L5+R5", "L4+L5+R4+R5", "STEAM+QAM")
PADDLE_NAMES = ("L4", "L5", "R4", "R5")
PADDLE_TARGETS = ("none", "A", "B", "X", "Y", "LB", "RB", "L3", "R3", "VIEW", "MENU",
                  "DPAD_UP", "DPAD_DOWN", "DPAD_LEFT", "DPAD_RIGHT")
# How the screen is turned off while active: auto = gamescope display sleep (Gaming Mode) ->
# kscreen-doctor DPMS (Desktop Mode) -> backlight 0 (only dims the OLED panel).
SCREEN_METHODS = ("auto", "gamescope", "kscreen", "backlight")

DEFAULT_PROFILE = "xbox360"
DEFAULT_TRANSPORT = "auto"
DEFAULT_KILL_COMBO = "L4+R4"
DEFAULT_KILL_HOLD_MS = 1500
DEFAULT_TOUCH_WAKE_SECONDS = 5
DEFAULT_SCREEN_METHOD = "auto"
DEFAULT_PADDLES: Dict[str, str] = {p: "none" for p in PADDLE_NAMES}


class ConfigError(ValueError):
    """Invalid option value."""


def resolve_transport(profile: str, transport: str) -> str:
    """``auto`` -> ``raw`` for xbox360 (vendor descriptors need raw-gadget), ``hid`` for hid_gamepad."""
    if profile not in PROFILES:
        raise ConfigError(f"unknown profile {profile!r} (expected one of {PROFILES})")
    if transport not in TRANSPORTS:
        raise ConfigError(f"unknown transport {transport!r} (expected one of {TRANSPORTS})")
    if transport == "auto":
        return "raw" if profile == "xbox360" else "hid"
    if profile == "xbox360" and transport == "hid":
        # f_hid cannot expose the Xbox 360 vendor-specific (0xFF/0x5D) interface — see docs/HARDWARE.md ("Kernel gadget stack").
        raise ConfigError("profile xbox360 requires transport raw (configfs f_hid cannot emulate XInput)")
    return transport


def parse_kill_combo(text: str) -> int:
    """``"L4+R4"`` -> canonical button mask. Only the combos listed in docs/ARCHITECTURE.md are allowed."""
    norm = "+".join(p.strip().upper() for p in text.split("+") if p.strip())
    if norm not in KILL_COMBOS:
        raise ConfigError(f"unsupported kill combo {text!r} (expected one of {KILL_COMBOS})")
    mask = 0
    for part in norm.split("+"):
        mask |= S.button_from_name(part)
    return mask


def parse_paddles(text: Optional[str]) -> Dict[str, str]:
    """``"L4=none,L5=A,R4=none,R5=B"`` -> dict; unspecified paddles default to ``none``."""
    result = dict(DEFAULT_PADDLES)
    if not text:
        return result
    for item in text.split(","):
        item = item.strip()
        if not item:
            continue
        if "=" not in item:
            raise ConfigError(f"bad paddle mapping {item!r} (expected NAME=TARGET)")
        name, target = (x.strip() for x in item.split("=", 1))
        name = name.upper()
        if name not in PADDLE_NAMES:
            raise ConfigError(f"unknown paddle {name!r} (expected one of {PADDLE_NAMES})")
        target = "none" if target.lower() == "none" else target.upper()
        if target not in PADDLE_TARGETS:
            raise ConfigError(f"unknown paddle target {target!r} (expected one of {PADDLE_TARGETS})")
        result[name] = target
    return result


def validate_paddles(paddles: Dict[str, str]) -> Dict[str, str]:
    out = dict(DEFAULT_PADDLES)
    for name, target in (paddles or {}).items():
        n = str(name).upper()
        if n not in PADDLE_NAMES:
            raise ConfigError(f"unknown paddle {name!r}")
        t = "none" if str(target).lower() == "none" else str(target).upper()
        if t not in PADDLE_TARGETS:
            raise ConfigError(f"unknown paddle target {target!r}")
        out[n] = t
    return out


@dataclass
class RunConfig:
    profile: str = DEFAULT_PROFILE
    transport: str = DEFAULT_TRANSPORT          # as requested (may be "auto")
    kill_combo: str = DEFAULT_KILL_COMBO
    kill_hold_ms: int = DEFAULT_KILL_HOLD_MS
    screen_off: bool = False
    touch_wake_seconds: float = DEFAULT_TOUCH_WAKE_SECONDS
    screen_method: str = DEFAULT_SCREEN_METHOD    # auto | gamescope | kscreen | backlight
    paddles: Dict[str, str] = field(default_factory=lambda: dict(DEFAULT_PADDLES))
    log_file: Optional[str] = None
    demo: bool = False                          # demo source instead of the Neptune controller
    udc: Optional[str] = None                   # force a UDC name (default: first in /sys/class/udc)
    forward_steam: bool = False                 # Steam -> Guide (off by default, see docs/ARCHITECTURE.md)
    forward_qam: bool = False

    def __post_init__(self) -> None:
        if self.profile not in PROFILES:
            raise ConfigError(f"unknown profile {self.profile!r}")
        if self.transport not in TRANSPORTS:
            raise ConfigError(f"unknown transport {self.transport!r}")
        self.resolved_transport = resolve_transport(self.profile, self.transport)
        self.kill_mask = parse_kill_combo(self.kill_combo)
        try:
            self.kill_hold_ms = int(self.kill_hold_ms)
        except (TypeError, ValueError):
            raise ConfigError(f"kill_hold_ms must be an integer, got {self.kill_hold_ms!r}") from None
        if not 100 <= self.kill_hold_ms <= 10000:
            raise ConfigError("kill_hold_ms must be within 100..10000")
        try:
            self.touch_wake_seconds = float(self.touch_wake_seconds)
        except (TypeError, ValueError):
            raise ConfigError("touch_wake_seconds must be a number") from None
        if not 0 < self.touch_wake_seconds <= 120:
            raise ConfigError("touch_wake_seconds must be within (0, 120]")
        self.screen_method = str(self.screen_method or DEFAULT_SCREEN_METHOD).lower()
        if self.screen_method not in SCREEN_METHODS:
            raise ConfigError(f"unknown screen method {self.screen_method!r} (expected one of {SCREEN_METHODS})")
        self.paddles = validate_paddles(self.paddles)

    @property
    def kill_hold_s(self) -> float:
        return self.kill_hold_ms / 1000.0

    def as_dict(self) -> dict:
        return {
            "profile": self.profile, "transport": self.transport, "resolved_transport": self.resolved_transport,
            "kill_combo": self.kill_combo, "kill_hold_ms": self.kill_hold_ms,
            "screen_off": self.screen_off, "touch_wake_seconds": self.touch_wake_seconds,
            "screen_method": self.screen_method,
            "paddles": dict(self.paddles), "demo": self.demo, "udc": self.udc,
        }
