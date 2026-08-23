"""What the daemon prints on stdout: one JSON object per line — ``state`` / ``error`` / ``metrics`` /
``kill`` / ``screen`` events. This is the contract between the daemon and the backend."""
from __future__ import annotations

import json
import logging
from typing import Any, Optional

log = logging.getLogger("controller_backend.daemon.events")

JsonDict = dict[str, Any]

EVENT_NAMES = ("state", "error", "metrics", "kill", "screen")
SESSION_STATES = ("IDLE", "CAPTURING", "GADGET_UP", "WAITING_HOST", "ACTIVE", "STOPPING")
CAPTURED_STATES = frozenset({"CAPTURING", "GADGET_UP", "WAITING_HOST", "ACTIVE"})
DAEMON_STOPPED_STATE = "STOPPED"   # the daemon's last state; the backend shows STOPPING until the process is gone


def parse_event_line(line: str) -> Optional[JsonDict]:
    """One stdout line → event dict, or ``None`` when it is not a JSON object (plain log text)."""
    try:
        event = json.loads(line)
    except ValueError:
        return None
    return event if isinstance(event, dict) else None


def parse_json_object(text: str) -> Optional[JsonDict]:
    """The JSON object printed by a one-shot CLI command; tolerates stray log lines before the JSON."""
    text = text.strip()
    if not text:
        return None
    parsed: Any = None
    try:
        parsed = json.loads(text)
    except ValueError:
        for line in reversed(text.splitlines()):
            line = line.strip()
            if line.startswith("{"):
                try:
                    parsed = json.loads(line)
                    break
                except ValueError:
                    continue
    if not isinstance(parsed, dict):
        log.debug("no JSON object in CLI output (%d chars)", len(text))
        return None
    return parsed
