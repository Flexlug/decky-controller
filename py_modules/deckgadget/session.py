"""Session state machine (docs/ARCHITECTURE.md, "Session state machine").

IDLE -> CAPTURING (screen off [gamescope sleep / kscreen dpms / backlight], source.open: unbind usbhid /
                   claim / lizard-off / heartbeat)
     -> GADGET_UP (transport.start(profile))
     -> WAITING_HOST (until the UDC reports ``configured``)
     -> ACTIVE (source.read -> profile.pack -> transport.send; OUT reports logged / rumble)
     -> STOPPING -> STOPPED

* kill-combo (hold ``kill_hold_ms``) is armed from CAPTURING on and is never forwarded;
* cable unplug in ACTIVE (UDC state leaves ``configured`` for > ``unplug_grace_s``) -> ``kill unplug``;
* SIGTERM/SIGINT -> ``request_stop("signal")``; any exception -> ``kill error`` + exit code 1;
* teardown always runs in ``finally`` and every step is individually guarded.
"""
from __future__ import annotations

import threading
import time
from typing import Callable, Optional

from . import state as S
from .config import RunConfig
from .profiles.base import Feedback, Profile
from .sources.base import InputSource
from .transports.base import Transport
from .util.log import JsonEventSink, get_logger

log = get_logger("session")

IDLE = "IDLE"
CAPTURING = "CAPTURING"
GADGET_UP = "GADGET_UP"
WAITING_HOST = "WAITING_HOST"
ACTIVE = "ACTIVE"
STOPPING = "STOPPING"
STOPPED = "STOPPED"

KILL_COMBO = "combo"
KILL_UNPLUG = "unplug"
KILL_SIGNAL = "signal"
KILL_ERROR = "error"


class HoldDetector:
    """Fires once when *all* bits of ``mask`` have been held continuously for ``hold_s``."""

    def __init__(self, mask: int, hold_s: float) -> None:
        self.mask = mask
        self.hold_s = hold_s
        self._since: Optional[float] = None
        self._fired = False

    @property
    def engaged(self) -> bool:
        """True while the full combo is currently held (used to mask it from the host)."""
        return self._since is not None

    def feed(self, buttons: int, now: float) -> bool:
        if self.mask and (buttons & self.mask) == self.mask:
            if self._since is None:
                self._since = now
            elif not self._fired and now - self._since >= self.hold_s:
                self._fired = True
                return True
        else:
            self._since = None
            self._fired = False
        return False

    def reset(self) -> None:
        self._since = None
        self._fired = False


class ScreenLike:
    """Minimal interface the session needs from ``platform.screen.ScreenController``."""

    def activate(self) -> None: ...
    def deactivate(self) -> None: ...
    is_off: bool = False


class Session:
    def __init__(self, config: RunConfig, source: InputSource, profile: Profile, transport: Transport,
                 screen: Optional[ScreenLike] = None, udc_state: Optional[Callable[[], Optional[str]]] = None,
                 events: Optional[JsonEventSink] = None, clock: Callable[[], float] = time.monotonic,
                 read_timeout: float = 0.05, unplug_grace_s: float = 1.0, metrics_interval: float = 2.0,
                 udc_poll_interval: float = 0.1, forward_rumble: bool = False) -> None:
        self.config = config
        self.source = source
        self.profile = profile
        self.transport = transport
        self.screen = screen
        self.udc_state = udc_state
        self.events = events or JsonEventSink()
        self.clock = clock
        self.read_timeout = read_timeout
        self.unplug_grace_s = unplug_grace_s
        self.metrics_interval = metrics_interval
        self.udc_poll_interval = udc_poll_interval
        self.forward_rumble = forward_rumble
        self.state = IDLE
        self.kill_reason: Optional[str] = None
        self.error: Optional[BaseException] = None
        self.reports = 0
        self.reports_sent = 0
        self._stop = threading.Event()
        self._combo = HoldDetector(config.kill_mask, config.kill_hold_s)
        self._source_open = False
        self._transport_started = False
        self._lock = threading.Lock()
        self.last_feedback: Optional[Feedback] = None

    # --- public API ------------------------------------------------------------------
    def request_stop(self, reason: str = KILL_SIGNAL) -> None:
        """Thread/signal-safe: ask the loop to stop with ``reason`` (first reason wins)."""
        with self._lock:
            if self.kill_reason is None:
                self.kill_reason = reason
        self._stop.set()

    @property
    def stopping(self) -> bool:
        return self._stop.is_set()

    def run(self) -> int:
        """Run to completion; returns the process exit code (0 = clean, 1 = error)."""
        exit_code = 0
        try:
            self._set_state(CAPTURING, f"source={self.source.name}")
            if self.screen is not None:
                try:
                    self.screen.activate()
                except Exception as exc:  # noqa: BLE001 - screen is cosmetic, never fatal
                    log.warning("screen activate failed: %s", exc)
            if self._stop.is_set():
                return 0
            self.source.open()
            self._source_open = True
            if self._stop.is_set():
                return 0
            self._set_state(GADGET_UP, f"transport={self.transport.name} profile={self.profile.name}")
            self.transport.start(self.profile, on_feedback=self._on_feedback)
            self._transport_started = True
            self._set_state(WAITING_HOST, "waiting for UDC state=configured")
            self._loop()
        except Exception as exc:  # noqa: BLE001
            self.error = exc
            with self._lock:
                if self.kill_reason is None:
                    self.kill_reason = KILL_ERROR
            log.exception("session failed: %s", exc)
            self.events.error(f"{type(exc).__name__}: {exc}")
            exit_code = 1
        finally:
            self._teardown()
        return exit_code

    # --- internals -------------------------------------------------------------------
    def _set_state(self, state: str, detail: str = "") -> None:
        self.state = state
        log.info("state %s %s", state, detail)
        self.events.state(state, detail)

    def _kill(self, reason: str) -> None:
        with self._lock:
            if self.kill_reason is None:
                self.kill_reason = reason
        self._stop.set()

    def _on_feedback(self, feedback: Feedback) -> None:
        self.last_feedback = feedback
        if feedback.kind == "rumble":
            log.info("host rumble left=%d right=%d", feedback.left, feedback.right)
            if self.forward_rumble:
                try:
                    self.source.rumble(feedback.left, feedback.right)
                except Exception as exc:  # noqa: BLE001
                    log.debug("rumble forward failed: %s", exc)
        elif feedback.kind == "led":
            log.info("host LED pattern 0x%02x", feedback.value)
        else:
            log.info("host output report: %s", feedback.raw.hex())

    def _host_connected(self) -> bool:
        if self.udc_state is not None:
            udc_state = self.udc_state()
            if udc_state is not None:
                # sysfs is the authority (raw-gadget may lag on DISCONNECT); transport must agree
                return udc_state == "configured" and self.transport.connected()
        return self.transport.connected()

    def _loop(self) -> None:
        clock = self.clock
        now = clock()
        next_udc = now
        next_metrics = now + self.metrics_interval
        window_start = now
        window_reports = 0
        disconnected_since: Optional[float] = None
        combo = self._combo
        mask = combo.mask
        while not self._stop.is_set():
            controller_state = self.source.read(self.read_timeout)
            now = clock()
            if controller_state is not None:
                self.reports += 1
                window_reports += 1
                if combo.feed(controller_state.buttons, now):
                    log.info("kill combo held for %.1fs", combo.hold_s)
                    self._kill(KILL_COMBO)
                    break
                if combo.engaged:
                    controller_state.buttons &= ~mask   # the kill combo is never forwarded to the host
            if now >= next_udc:
                next_udc = now + self.udc_poll_interval
                transport_error = self.transport.error
                if transport_error is not None:
                    raise RuntimeError(f"transport failed: {transport_error}")
                connected = self._host_connected()
                if self.state == WAITING_HOST:
                    if connected:
                        self._set_state(ACTIVE, "host configured")
                        disconnected_since = None
                elif self.state == ACTIVE:
                    if connected:
                        disconnected_since = None
                    elif disconnected_since is None:
                        disconnected_since = now
                    elif now - disconnected_since >= self.unplug_grace_s:
                        log.info("host gone for %.1fs -> unplug", now - disconnected_since)
                        self._kill(KILL_UNPLUG)
                        break
            if controller_state is not None and self.state == ACTIVE:
                self.transport.send(self.profile.pack(controller_state))
                self.reports_sent += 1
            if now >= next_metrics:
                elapsed = max(1e-6, now - window_start)
                transport_metrics = self.transport.metrics()
                self.events.metrics(hz=window_reports / elapsed, reports=self.reports,
                                    dropped=transport_metrics.dropped, sent=transport_metrics.sent,
                                    errors=transport_metrics.errors, state=self.state)
                window_start, window_reports = now, 0
                next_metrics = now + self.metrics_interval

    def _teardown(self) -> None:
        reason = self.kill_reason or KILL_SIGNAL
        if self.state not in (STOPPING, STOPPED):
            self._set_state(STOPPING, f"reason={reason}")
        self.events.kill(reason)
        # Always stop the transport (stop() is idempotent by protocol): start() may have raised after
        # partially bringing the gadget up, and the session must not leave it live on the cable.
        try:
            self.transport.stop()
        except Exception as exc:  # noqa: BLE001
            log.warning("transport stop failed: %s", exc)
        self._transport_started = False
        if self._source_open:
            try:
                self.source.close()
            except Exception as exc:  # noqa: BLE001
                log.warning("source close failed: %s", exc)
            self._source_open = False
        if self.screen is not None:
            try:
                self.screen.deactivate()
            except Exception as exc:  # noqa: BLE001
                log.warning("screen deactivate failed: %s", exc)
        self._set_state(STOPPED, f"reason={reason}")


def build_session(config: RunConfig, events: Optional[JsonEventSink] = None, sysfs: str = "/sys",
                  dev: str = "/dev") -> Session:
    """Wire real components according to ``config`` (``config.demo`` selects the demo source)."""
    from .platform.screen import ScreenController
    from .platform.usb_role import UdcWatcher
    from .profiles import make_profile
    from .transports import make_transport

    events = events or JsonEventSink()
    profile = make_profile(config.profile, paddles=config.paddles, forward_steam=config.forward_steam,
                           forward_qam=config.forward_qam)
    transport = make_transport(config.resolved_transport, udc=config.udc)
    if config.demo:
        from .sources.demo import DemoSource
        source: InputSource = DemoSource()
    else:
        from .sources.neptune_usb import NeptuneUsbSource
        source = NeptuneUsbSource(sysfs=sysfs, dev=dev)
    screen = None
    if config.screen_off:
        screen = ScreenController(wake_seconds=config.touch_wake_seconds, sysfs=sysfs, dev=dev,
                                  method=config.screen_method,
                                  on_change=lambda off, method: events.screen(off, method))
    watcher = UdcWatcher(config.udc, sysfs)
    return Session(config, source, profile, transport, screen=screen, udc_state=watcher.state, events=events)
