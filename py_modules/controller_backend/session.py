"""The backend's view of the daemon session, kept up to date from the daemon's stdout events."""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional

from controller_backend.daemon.events import DAEMON_STOPPED_STATE, JsonDict, SESSION_STATES

log = logging.getLogger("controller_backend.session")

DEFAULT_METRICS: JsonDict = {"hz": 0, "reports": 0, "dropped": 0}


@dataclass
class Toast:
    title: str
    body: str
    severity: str = "info"

    def as_dict(self) -> JsonDict:
        return {"title": self.title, "body": self.body, "severity": self.severity}


@dataclass
class EventOutcome:
    """What the service should do after an event was applied to the view."""
    emit_status: bool = False
    toast: Optional[Toast] = None


@dataclass
class SessionView:
    state: str = "IDLE"
    detail: str = ""
    active_profile: Optional[str] = None
    transport: Optional[str] = None
    metrics: JsonDict = field(default_factory=lambda: dict(DEFAULT_METRICS))
    screen_off: Optional[bool] = None   # None until the daemon's first "screen" event
    last_error: Optional[str] = None
    last_kill: Optional[str] = None

    def begin(self, profile: str, transport: str) -> None:
        self.reset()
        self.active_profile = profile
        self.transport = transport
        self.last_error = None
        self.last_kill = None

    def reset(self) -> None:
        """Back to IDLE; ``last_error`` / ``last_kill`` stay so the UI can show why the session ended."""
        self.state = "IDLE"
        self.detail = ""
        self.active_profile = None
        self.transport = None
        self.metrics = dict(DEFAULT_METRICS)
        self.screen_off = None

    def apply(self, event: JsonDict, stop_requested: bool = False) -> EventOutcome:
        kind = event.get("ev")
        if kind == "state":
            return self._apply_state(event)
        if kind == "error":
            self.last_error = str(event.get("msg") or "unknown daemon error")
            log.error("[deckgadget] error: %s", self.last_error)
            return EventOutcome(emit_status=True)
        if kind == "metrics":
            # the periodic status loop carries metrics to the UI — no emit here
            for key in DEFAULT_METRICS:
                value = event.get(key)
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    self.metrics[key] = value
            return EventOutcome()
        if kind == "kill":
            return self._apply_kill(event, stop_requested)
        if kind == "screen":
            self.screen_off = bool(event.get("off"))   # authoritative for this session
            return EventOutcome(emit_status=True)
        log.debug("[deckgadget] unhandled event %r", event)
        return EventOutcome()

    def _apply_state(self, event: JsonDict) -> EventOutcome:
        state = str(event.get("state") or "")
        self.detail = str(event.get("detail") or "")
        if state == DAEMON_STOPPED_STATE:
            state = "STOPPING"   # the process is about to exit; status shows IDLE as soon as it is gone
        if state in SESSION_STATES:
            self.state = state
        else:
            log.warning("daemon reported unknown state %r", state)
        return EventOutcome(emit_status=True)

    def _apply_kill(self, event: JsonDict, stop_requested: bool) -> EventOutcome:
        reason = str(event.get("reason") or "unknown")
        self.last_kill = reason
        log.info("[deckgadget] kill reason=%s", reason)
        if reason == "combo":
            return EventOutcome(toast=Toast("Controller mode stopped", "Exit combo held — the Deck is a Deck again."))
        if reason == "unplug":
            return EventOutcome(toast=Toast("Controller mode stopped", "USB cable disconnected."))
        if reason == "signal" and not stop_requested:
            return EventOutcome(toast=Toast("Controller mode stopped", "Daemon was signalled to exit."))
        return EventOutcome()   # "error": the exit handler reports it together with the error text
