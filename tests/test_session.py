import threading
import unittest
from typing import List, Optional

import _path  # noqa: F401

from deckgadget import session as SES
from deckgadget import state as S
from deckgadget.config import RunConfig
from deckgadget.profiles.base import Feedback
from deckgadget.profiles.xbox360 import Xbox360Profile
from deckgadget.state import ControllerState
from deckgadget.transports.base import TransportMetrics
from deckgadget.util.log import NullEventSink


class FakeClock:
    def __init__(self):
        self.t = 100.0

    def __call__(self):
        return self.t

    def advance(self, dt):
        self.t += dt


class FakeSource:
    """Scripted source: ``script`` is a list of (buttons, duration_s) or callables."""

    name = "fake"

    def __init__(self, clock, script=None, fail_open=False, tick=0.01):
        self.clock = clock
        self.script = list(script or [])
        self.fail_open = fail_open
        self.tick = tick
        self.opened = False
        self.closed = False
        self.rumbles: List = []
        self._cur = None
        self._until = None
        self.reads = 0

    def open(self):
        if self.fail_open:
            raise RuntimeError("no controller")
        self.opened = True

    def read(self, timeout):
        self.reads += 1
        self.clock.advance(self.tick)
        now = self.clock()
        if self._cur is None or now >= self._until:
            if not self.script:
                return None if self._cur is None else ControllerState(buttons=0, ts=now)
            item = self.script.pop(0)
            if callable(item):
                item()
                return None
            buttons, dur = item
            self._cur, self._until = buttons, now + dur
        return ControllerState(buttons=self._cur, lx=1, ts=now)

    def rumble(self, left, right):
        self.rumbles.append((left, right))

    def close(self):
        self.closed = True


class FakeTransport:
    name = "fake"

    def __init__(self, fail_start=False):
        self.fail_start = fail_start
        self.started = False
        self.stopped = False
        self.sent: List[bytes] = []
        self.is_connected = False
        self.error = None
        self.on_feedback = None
        self._m = TransportMetrics()

    def start(self, profile, on_feedback=None):
        if self.fail_start:
            raise RuntimeError("no udc")
        self.started = True
        self.on_feedback = on_feedback

    def send(self, report):
        self.sent.append(report)
        self._m.sent += 1

    def connected(self):
        return self.is_connected

    def metrics(self):
        return self._m

    def stop(self):
        self.stopped = True


class FakeScreen:
    def __init__(self):
        self.activated = 0
        self.deactivated = 0
        self.is_off = False

    def activate(self):
        self.activated += 1
        self.is_off = True

    def deactivate(self):
        self.deactivated += 1
        self.is_off = False


class HoldDetectorTest(unittest.TestCase):
    def test_requires_full_hold(self):
        d = SES.HoldDetector(S.BTN_L4 | S.BTN_R4, 1.5)
        self.assertFalse(d.feed(S.BTN_L4, 0.0))
        self.assertFalse(d.engaged)
        self.assertFalse(d.feed(S.BTN_L4 | S.BTN_R4, 1.0))
        self.assertTrue(d.engaged)
        self.assertFalse(d.feed(S.BTN_L4 | S.BTN_R4, 2.4))
        self.assertTrue(d.feed(S.BTN_L4 | S.BTN_R4, 2.5))    # fires at >= hold
        self.assertFalse(d.feed(S.BTN_L4 | S.BTN_R4, 3.0))   # fires only once
        self.assertFalse(d.feed(0, 3.1))
        self.assertFalse(d.feed(S.BTN_L4 | S.BTN_R4, 3.2))
        self.assertTrue(d.feed(S.BTN_L4 | S.BTN_R4, 4.7))    # re-armed after release

    def test_release_resets_timer(self):
        d = SES.HoldDetector(S.BTN_L4 | S.BTN_R4, 1.0)
        self.assertFalse(d.feed(S.BTN_L4 | S.BTN_R4, 0.0))
        self.assertFalse(d.feed(S.BTN_R4, 0.9))               # L4 released -> reset
        self.assertFalse(d.feed(S.BTN_L4 | S.BTN_R4, 1.0))
        self.assertFalse(d.feed(S.BTN_L4 | S.BTN_R4, 1.9))
        self.assertTrue(d.feed(S.BTN_L4 | S.BTN_R4, 2.0))
        # extra buttons pressed together with the combo still count
        d2 = SES.HoldDetector(S.BTN_L4 | S.BTN_R4, 1.0)
        d2.feed(S.BTN_L4 | S.BTN_R4 | S.BTN_A, 0.0)
        self.assertTrue(d2.feed(S.BTN_L4 | S.BTN_R4 | S.BTN_A, 1.0))

    def test_empty_mask_never_fires(self):
        d = SES.HoldDetector(0, 0.1)
        self.assertFalse(d.feed(0, 0.0))
        self.assertFalse(d.feed(0, 10.0))


def make_session(clock, source, transport, screen=None):
    cfg = RunConfig(kill_combo="L4+R4", kill_hold_ms=1500, screen_off=screen is not None)
    events = NullEventSink()
    ses = SES.Session(cfg, source, Xbox360Profile(), transport, screen=screen, events=events,
                      clock=clock, read_timeout=0.01, unplug_grace_s=1.0, metrics_interval=2.0,
                      udc_poll_interval=0.0)
    return ses, events


def states(events):
    return [e["state"] for e in events.events if e["ev"] == "state"]


class SessionTest(unittest.TestCase):
    def test_combo_kill_flow(self):
        clock = FakeClock()
        tr = FakeTransport()

        def plug():
            tr.is_connected = True

        src = FakeSource(clock, script=[(0, 0.5), plug, (S.BTN_A, 0.5), (S.BTN_L4, 0.2),
                                        (S.BTN_L4 | S.BTN_R4 | S.BTN_B, 3.0), (0, 5.0)])
        screen = FakeScreen()
        ses, ev = make_session(clock, src, tr, screen=screen)
        rc = ses.run()
        self.assertEqual(rc, 0)
        self.assertEqual(ses.kill_reason, "combo")
        self.assertEqual(states(ev), ["CAPTURING", "GADGET_UP", "WAITING_HOST", "ACTIVE", "STOPPING", "STOPPED"])
        self.assertIn({"ev": "kill", "reason": "combo"}, ev.events)
        self.assertTrue(src.opened and src.closed and tr.started and tr.stopped)
        self.assertEqual((screen.activated, screen.deactivated), (1, 1))
        # nothing sent before ACTIVE, A forwarded while ACTIVE, combo bits never forwarded
        self.assertTrue(tr.sent)
        import struct
        from deckgadget.profiles.xbox360 import XB_A, XB_B
        btns = [struct.unpack_from("<H", r, 2)[0] for r in tr.sent]
        self.assertIn(XB_A, btns)
        self.assertIn(XB_B, btns)                 # B pressed together with the combo still goes through
        # L4/R4 are not mapped by default anyway; check the paddle-mapped case separately below
        metrics = [e for e in ev.events if e["ev"] == "metrics"]
        self.assertTrue(metrics)
        self.assertGreater(metrics[-1]["reports"], 0)

    def test_combo_masked_when_paddles_mapped(self):
        clock = FakeClock()
        tr = FakeTransport()
        tr.is_connected = True
        src = FakeSource(clock, script=[(S.BTN_L4, 0.3), (S.BTN_L4 | S.BTN_R4, 2.0), (0, 2.0)])
        cfg = RunConfig(kill_combo="L4+R4", kill_hold_ms=1500, paddles={"L4": "A", "R4": "B"})
        ev = NullEventSink()
        prof = Xbox360Profile(paddles=cfg.paddles)
        ses = SES.Session(cfg, src, prof, tr, events=ev, clock=clock, read_timeout=0.01, udc_poll_interval=0.0)
        self.assertEqual(ses.run(), 0)
        self.assertEqual(ses.kill_reason, "combo")
        import struct
        from deckgadget.profiles.xbox360 import XB_A, XB_B
        btns = [struct.unpack_from("<H", r, 2)[0] for r in tr.sent]
        self.assertIn(XB_A, btns)                        # L4 alone -> A
        self.assertNotIn(XB_A | XB_B, btns)              # full combo never reaches the host

    def test_unplug_kill(self):
        clock = FakeClock()
        tr = FakeTransport()
        tr.is_connected = True

        def unplug():
            tr.is_connected = False

        src = FakeSource(clock, script=[(0, 0.5), unplug, (0, 5.0)])
        ses, ev = make_session(clock, src, tr)
        self.assertEqual(ses.run(), 0)
        self.assertEqual(ses.kill_reason, "unplug")
        self.assertEqual(states(ev)[-3:], ["ACTIVE", "STOPPING", "STOPPED"])
        self.assertIn({"ev": "kill", "reason": "unplug"}, ev.events)
        self.assertTrue(tr.stopped and src.closed)

    def test_brief_disconnect_is_tolerated(self):
        clock = FakeClock()
        tr = FakeTransport()
        tr.is_connected = True

        def glitch():
            tr.is_connected = False

        def restore():
            tr.is_connected = True

        src = FakeSource(clock, script=[(0, 0.3), glitch, (0, 0.3), restore, (0, 0.5),
                                        lambda: ses.request_stop("signal")])
        ses, ev = make_session(clock, src, tr)
        self.assertEqual(ses.run(), 0)
        self.assertEqual(ses.kill_reason, "signal")
        self.assertNotIn({"ev": "kill", "reason": "unplug"}, ev.events)

    def test_signal_stop_while_waiting_host(self):
        clock = FakeClock()
        tr = FakeTransport()
        src = FakeSource(clock, script=[(0, 0.5), lambda: ses.request_stop("signal"), (0, 1.0)])
        ses, ev = make_session(clock, src, tr)
        self.assertEqual(ses.run(), 0)
        self.assertEqual(ses.kill_reason, "signal")
        self.assertEqual(states(ev), ["CAPTURING", "GADGET_UP", "WAITING_HOST", "STOPPING", "STOPPED"])
        self.assertEqual(tr.sent, [])
        self.assertTrue(tr.stopped)

    def test_source_open_failure_is_error(self):
        clock = FakeClock()
        tr = FakeTransport()
        src = FakeSource(clock, fail_open=True)
        screen = FakeScreen()
        ses, ev = make_session(clock, src, tr, screen=screen)
        self.assertEqual(ses.run(), 1)
        self.assertEqual(ses.kill_reason, "error")
        self.assertFalse(tr.started)
        self.assertTrue(any(e["ev"] == "error" and "no controller" in e["msg"] for e in ev.events))
        self.assertEqual(states(ev), ["CAPTURING", "STOPPING", "STOPPED"])
        self.assertEqual(screen.deactivated, 1)

    def test_transport_start_failure_closes_source(self):
        clock = FakeClock()
        tr = FakeTransport(fail_start=True)
        src = FakeSource(clock)
        ses, ev = make_session(clock, src, tr)
        self.assertEqual(ses.run(), 1)
        self.assertTrue(src.closed)
        # stop() is idempotent and is called even when start() raised (a transport may have
        # partially brought the gadget up before failing)
        self.assertTrue(tr.stopped)
        self.assertIn({"ev": "kill", "reason": "error"}, ev.events)

    def test_transport_background_error(self):
        clock = FakeClock()
        tr = FakeTransport()
        tr.is_connected = True

        def boom():
            tr.error = RuntimeError("raw-gadget died")

        src = FakeSource(clock, script=[(0, 0.2), boom, (0, 1.0)])
        ses, ev = make_session(clock, src, tr)
        self.assertEqual(ses.run(), 1)
        self.assertEqual(ses.kill_reason, "error")
        self.assertTrue(tr.stopped and src.closed)

    def test_host_feedback_is_observed_and_never_forwarded_to_the_controller(self):
        clock = FakeClock()
        tr = FakeTransport()
        tr.is_connected = True
        src = FakeSource(clock, script=[(0, 0.1), lambda: tr.on_feedback(Feedback("rumble", 100, 200)),
                                        lambda: ses.request_stop("signal")])
        ses, ev = make_session(clock, src, tr)
        ses.run()
        self.assertEqual(src.rumbles, [])
        self.assertEqual(ses.last_feedback.kind, "rumble")

    def test_metrics_event_carries_out_reports(self):
        clock = FakeClock()
        tr = FakeTransport()
        tr.is_connected = True
        tr._m.out_reports = 3
        src = FakeSource(clock, script=[(0, 2.5), lambda: ses.request_stop("signal")])
        ses, ev = make_session(clock, src, tr)
        ses.run()
        metrics = [e for e in ev.events if e["ev"] == "metrics"]
        self.assertTrue(metrics)
        self.assertEqual(metrics[-1]["out_reports"], 3)

    def test_request_stop_before_run(self):
        clock = FakeClock()
        tr = FakeTransport()
        src = FakeSource(clock)
        ses, ev = make_session(clock, src, tr)
        ses.request_stop("signal")
        self.assertEqual(ses.run(), 0)
        self.assertFalse(src.opened)
        self.assertEqual(states(ev)[-1], "STOPPED")


if __name__ == "__main__":
    unittest.main()
