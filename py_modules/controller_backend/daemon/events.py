"""What the daemon prints on stdout: one JSON object per line — ``state`` / ``error`` / ``metrics`` /
``kill`` / ``screen`` events. This is the contract between the daemon and the backend."""
from __future__ import annotations

import json
import logging
from typing import Any, Optional

log = logging.getLogger("controller_backend.daemon.events")

JsonDict = dict[str, Any]

EVENT_NAMES = ("state", "error", "metrics", "kill", "screen")
KILL_COMBO, KILL_UNPLUG, KILL_SIGNAL, KILL_ERROR = KILL_REASONS = ("combo", "unplug", "signal", "error")
SESSION_STATES = ("IDLE", "CAPTURING", "GADGET_UP", "WAITING_HOST", "ACTIVE", "STOPPING")
CAPTURED_STATES = frozenset({"CAPTURING", "GADGET_UP", "WAITING_HOST", "ACTIVE"})
DAEMON_STOPPED_STATE = "STOPPED"   # the daemon's last state; the backend shows STOPPING until the process is gone


def parse_json_object(text: str, what: str) -> Optional[JsonDict]:
    """The JSON object a one-shot CLI command prints on stdout (logs go to stderr), or ``None``."""
    try:
        parsed = json.loads(text)
    except ValueError as exc:
        log.warning("%s printed no JSON object (%s): %r", what, exc, text[-200:])
        return None
    if not isinstance(parsed, dict):
        log.warning("%s printed %s instead of a JSON object", what, type(parsed).__name__)
        return None
    return parsed
