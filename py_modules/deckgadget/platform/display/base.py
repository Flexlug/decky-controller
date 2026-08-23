"""What every screen-off strategy looks like, and where the backlight state file lives."""
from __future__ import annotations

import os
from typing import Dict, Optional

STATE_DIRS = ("/run/deckgadget", "/tmp/deckgadget")
STATE_FILE_NAME = "brightness"


def default_state_file(run_dir: Optional[str] = None) -> str:
    """First writable state dir wins (``/run/deckgadget`` is created if possible)."""
    candidates = (run_dir,) if run_dir else STATE_DIRS
    for directory in candidates:
        if not directory:
            continue
        try:
            os.makedirs(directory, exist_ok=True)
            if os.access(directory, os.W_OK):
                return os.path.join(directory, STATE_FILE_NAME)
        except OSError:
            continue
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
