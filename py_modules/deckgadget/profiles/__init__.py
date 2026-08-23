"""Device profiles: what the PC sees. ``make_profile(name, ...)`` is the factory."""
from __future__ import annotations

from typing import Dict, Optional

from .base import Profile


def make_profile(name: str, paddles: Optional[Dict[str, str]] = None,
                 forward_steam: bool = False, forward_qam: bool = False) -> Profile:
    if name == "xbox360":
        from .xbox360 import Xbox360Profile
        return Xbox360Profile(paddles=paddles, forward_steam=forward_steam, forward_qam=forward_qam)
    if name == "hid_gamepad":
        from .hid_gamepad import HidGamepadProfile
        return HidGamepadProfile(paddles=paddles, forward_steam=forward_steam, forward_qam=forward_qam)
    raise ValueError(f"unknown profile {name!r}")


__all__ = ["Profile", "make_profile"]
