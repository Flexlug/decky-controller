"""stderr/file logging plus the JSON-lines event sink on stdout.

stdout is reserved for the events the Decky backend consumes, one JSON object per line
(``state``, ``error``, ``metrics``, ``kill``, ``screen`` — a contract); humans read stderr / ``--log-file``."""
from __future__ import annotations

import json
import logging
import sys
import threading
import time
from typing import Any, Optional, TextIO

LOGGER_NAME = "deckgadget"


LOGGER_NAMES = (LOGGER_NAME, "deckhw")


def setup_logging(level: int = logging.INFO, log_file: Optional[str] = None) -> logging.Logger:
    """Configure the daemon's loggers (``deckgadget`` and the shared ``deckhw``) once; safe to call repeatedly."""
    log = logging.getLogger(LOGGER_NAME)
    if getattr(log, "_deckgadget_configured", False):
        for name in LOGGER_NAMES:
            logging.getLogger(name).setLevel(level)
        return log
    formatter = logging.Formatter("%(asctime)s %(levelname).1s %(name)s: %(message)s", "%H:%M:%S")
    handlers = [logging.StreamHandler(sys.stderr)]
    if log_file:
        try:
            handlers.append(logging.FileHandler(log_file))
        except OSError as exc:  # never let logging setup kill the daemon
            log.warning("cannot open log file %s: %s", log_file, exc)
    for handler in handlers:
        handler.setFormatter(formatter)
    for name in LOGGER_NAMES:
        logger = logging.getLogger(name)
        logger.setLevel(level)
        logger.propagate = False
        for handler in handlers:
            logger.addHandler(handler)
    log._deckgadget_configured = True  # type: ignore[attr-defined]
    return log


def get_logger(suffix: Optional[str] = None) -> logging.Logger:
    return logging.getLogger(LOGGER_NAME if not suffix else f"{LOGGER_NAME}.{suffix}")


class JsonEventSink:
    """Thread-safe JSON-lines writer (default: stdout)."""

    def __init__(self, stream: Optional[TextIO] = None) -> None:
        self._stream = stream if stream is not None else sys.stdout
        self._lock = threading.Lock()

    def emit(self, event: str, **fields: Any) -> None:
        record = {"ev": event, "ts": round(time.time(), 3)}
        record.update(fields)
        line = json.dumps(record, separators=(",", ":"), ensure_ascii=False)
        with self._lock:
            try:
                self._stream.write(line + "\n")
                self._stream.flush()
            except (OSError, ValueError) as exc:
                logging.getLogger(LOGGER_NAME).debug("event sink closed (supervisor gone?): %s", exc)

    def state(self, state: str, detail: str = "") -> None:
        self.emit("state", state=state, detail=detail)

    def error(self, message: str) -> None:
        self.emit("error", msg=message)

    def metrics(self, hz: float, reports: int, dropped: int, **extra: Any) -> None:
        self.emit("metrics", hz=round(hz, 1), reports=reports, dropped=dropped, **extra)

    def kill(self, reason: str) -> None:
        self.emit("kill", reason=reason)

    def screen(self, off: bool, method: str = "none") -> None:
        self.emit("screen", off=bool(off), method=str(method or "none"))


class NullEventSink(JsonEventSink):
    """Event sink that records events in memory (tests) instead of writing."""

    def __init__(self) -> None:
        super().__init__(stream=None)
        self.events: list = []

    def emit(self, event: str, **fields: Any) -> None:  # type: ignore[override]
        record = {"ev": event}
        record.update(fields)
        self.events.append(record)
