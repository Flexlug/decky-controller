"""What every screen-off strategy looks like, and where the backlight state file lives."""
from __future__ import annotations

import os
from typing import Dict

from deckgadget.util.log import get_logger

log = get_logger("display")

STATE_DIRS = ("/run/deckgadget", "/tmp/deckgadget")
STATE_FILE_NAME = "brightness"


def default_state_file() -> str:
    """The first state dir that is writable, or whose parent is (it is created on the first write — choosing the
    path has no side effects, so status/diagnostics never create ``/run/deckgadget``)."""
    for directory in STATE_DIRS:
        probe = directory if os.path.isdir(directory) else os.path.dirname(directory)
        if os.access(probe, os.W_OK):
            return os.path.join(directory, STATE_FILE_NAME)
    log.debug("no writable state dir among %s; using /tmp", STATE_DIRS)
    return os.path.join("/tmp", "deckgadget-" + STATE_FILE_NAME)


class ScreenMethod:
    """Common interface of the screen-off strategies. Every call is best-effort and never raises."""

    name: str = "none"

    def available(self) -> bool:
        return False

    def sleep(self) -> bool:
        """Put the panel to sleep; ``True`` when it was actually done."""
        return False

    def wake(self) -> bool:
        """Temporary wake (touch); the screen is expected to go back to sleep with :meth:`sleep`."""
        return False

    def release(self) -> bool:
        """Permanent wake at the end of the session (cleans up whatever ``sleep`` saved)."""
        return self.wake()

    def info(self) -> Dict[str, object]:
        return {"available": self.available()}
