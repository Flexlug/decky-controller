"""Transport protocol, metrics and the latest-report slot shared by both transports.

Design note: ``send()`` never blocks the caller (the session hot loop).  It drops the
report into a :class:`ReportSlot`; a dedicated writer thread pushes the *newest* report
to the endpoint.  If the host polls slower than the source produces, older unsent
reports are replaced (counted as ``dropped``) — input latency stays minimal and a stalled
endpoint can never block kill-combo detection.
"""
from __future__ import annotations

import signal
import threading
from dataclasses import dataclass
from typing import Callable, Optional, Protocol, runtime_checkable

from ..profiles.base import Feedback, Profile

#: signal used to interrupt blocking ioctls in worker threads (raw-gadget has no timeouts)
CANCEL_SIGNAL = signal.SIGUSR1


class TransportError(RuntimeError):
    """Transport could not start / failed fatally."""


@dataclass
class TransportMetrics:
    sent: int = 0
    dropped: int = 0
    errors: int = 0
    out_reports: int = 0

    def as_dict(self) -> dict:
        return {"sent": self.sent, "dropped": self.dropped, "errors": self.errors, "out_reports": self.out_reports}


class ReportSlot:
    """Thread-safe single-slot mailbox holding the newest report."""

    def __init__(self) -> None:
        self._cond = threading.Condition()
        self._report: Optional[bytes] = None
        self.dropped = 0

    def put(self, report: bytes) -> bool:
        """Store ``report``; returns True if an unconsumed report was replaced (dropped)."""
        with self._cond:
            replaced = self._report is not None
            if replaced:
                self.dropped += 1
            self._report = report
            self._cond.notify()
            return replaced

    def take(self, timeout: float) -> Optional[bytes]:
        """Wait up to ``timeout`` seconds for a report and consume it."""
        with self._cond:
            if self._report is None:
                self._cond.wait(timeout)
            report, self._report = self._report, None
            return report

    def clear(self) -> None:
        with self._cond:
            self._report = None


FeedbackCallback = Callable[[Feedback], None]

_cancel_installed = False


def install_cancel_signal_handler() -> bool:
    """Make :data:`CANCEL_SIGNAL` a harmless, syscall-interrupting signal (main thread only).

    Returns True when the handler is installed (now or earlier)."""
    global _cancel_installed
    if _cancel_installed:
        return True
    if threading.current_thread() is not threading.main_thread():
        return False
    signal.signal(CANCEL_SIGNAL, lambda *_: None)
    signal.siginterrupt(CANCEL_SIGNAL, True)
    _cancel_installed = True
    return True


def interrupt_thread(thread: Optional[threading.Thread]) -> None:
    """Deliver :data:`CANCEL_SIGNAL` to ``thread`` so its blocking ioctl returns EINTR."""
    if thread is None or not thread.is_alive() or thread.ident is None:
        return
    try:
        signal.pthread_kill(thread.ident, CANCEL_SIGNAL)
    except (ProcessLookupError, ValueError, OSError):
        pass


def join_with_interrupts(threads, timeout: float, interval: float = 0.05) -> bool:
    """Repeatedly poke blocked threads with the cancel signal until they exit (or ``timeout``)."""
    import time

    deadline = time.monotonic() + timeout
    alive = [thread for thread in threads
             if thread is not None and thread.is_alive() and thread is not threading.current_thread()]
    while alive:
        for thread in alive:
            interrupt_thread(thread)
            thread.join(interval)
        alive = [thread for thread in alive if thread.is_alive()]
        if alive and time.monotonic() >= deadline:
            return False
    return True


@runtime_checkable
class Transport(Protocol):
    name: str

    def start(self, profile: Profile, on_feedback: Optional[FeedbackCallback] = None) -> None:
        """Bring the gadget up (non-blocking w.r.t. host enumeration)."""

    def send(self, report: bytes) -> None:
        """Queue the newest input report (never blocks)."""

    def connected(self) -> bool:
        """True while the host has configured us (reports are flowing)."""

    def metrics(self) -> TransportMetrics: ...

    @property
    def error(self) -> Optional[BaseException]:
        """Fatal background error, if any (session turns it into kill reason=error)."""

    def stop(self) -> None:
        """Tear the gadget down (idempotent, must not hang)."""
