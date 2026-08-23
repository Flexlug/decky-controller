"""InputSource protocol."""
from __future__ import annotations

from typing import Optional, Protocol, runtime_checkable

from deckgadget.state import ControllerState


@runtime_checkable
class InputSource(Protocol):
    name: str

    def open(self) -> None:
        """Take exclusive ownership of the device."""

    def read(self, timeout: float) -> Optional[ControllerState]:
        """Block up to ``timeout`` seconds for the next state; ``None`` on timeout / non-state packet."""

    def rumble(self, left: int, right: int) -> None:
        """Motor speeds 0..65535 (best effort, may be a no-op)."""

    def close(self) -> None:
        """Release the device and undo ``open`` (idempotent)."""
