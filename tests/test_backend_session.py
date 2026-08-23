"""``controller_backend.session.SessionView``: daemon events → session view + what the service must do."""
import unittest

import _path  # noqa: F401

from controller_backend.session import DEFAULT_METRICS, SessionView


class SessionViewTest(unittest.TestCase):
    def setUp(self):
        self.view = SessionView()

    def test_begin_and_reset_keep_the_last_error(self):
        self.view.last_error = "old"
        self.view.begin("xbox360", "raw")
        self.assertEqual((self.view.state, self.view.active_profile, self.view.transport), ("IDLE", "xbox360", "raw"))
        self.assertIsNone(self.view.last_error)
        self.view.state, self.view.last_error, self.view.screen_off = "ACTIVE", "boom", True
        self.view.reset()
        self.assertEqual((self.view.state, self.view.active_profile, self.view.screen_off), ("IDLE", None, None))
        self.assertEqual(self.view.last_error, "boom")
        self.assertEqual(self.view.metrics, DEFAULT_METRICS)

    def test_state_events_update_state_and_detail(self):
        outcome = self.view.apply({"ev": "state", "state": "CAPTURING", "detail": "lizard off"})
        self.assertTrue(outcome.emit_status)
        self.assertEqual((self.view.state, self.view.detail), ("CAPTURING", "lizard off"))
        self.view.apply({"ev": "state", "state": "ACTIVE"})
        self.assertEqual((self.view.state, self.view.detail), ("ACTIVE", ""))

    def test_stopped_shows_as_stopping(self):
        self.view.apply({"ev": "state", "state": "STOPPED"})
        self.assertEqual(self.view.state, "STOPPING")

    def test_unknown_state_is_ignored_with_a_warning(self):
        with self.assertLogs("controller_backend.session", level="WARNING"):
            self.view.apply({"ev": "state", "state": "FLYING"})
        self.assertEqual(self.view.state, "IDLE")

    def test_error_event_sets_last_error(self):
        with self.assertLogs("controller_backend.session", level="ERROR"):
            outcome = self.view.apply({"ev": "error", "msg": "no UDC"})
        self.assertTrue(outcome.emit_status)
        self.assertEqual(self.view.last_error, "no UDC")

    def test_metrics_update_numbers_only_and_do_not_emit(self):
        outcome = self.view.apply({"ev": "metrics", "hz": 249.9, "reports": 1200, "dropped": "x", "extra": 1})
        self.assertFalse(outcome.emit_status)
        self.assertEqual(self.view.metrics, {"hz": 249.9, "reports": 1200, "dropped": 0})

    def test_kill_reasons_toast(self):
        combo = self.view.apply({"ev": "kill", "reason": "combo"})
        unplug = self.view.apply({"ev": "kill", "reason": "unplug"})
        error = self.view.apply({"ev": "kill", "reason": "error"})
        self.assertIn("Exit combo", combo.toast.body)
        self.assertIn("cable", unplug.toast.body)
        self.assertEqual(combo.toast.severity, "info")
        self.assertIsNone(error.toast)
        self.assertEqual(self.view.last_kill, "error")

    def test_signal_kill_toasts_only_when_not_requested(self):
        self.assertIsNone(self.view.apply({"ev": "kill", "reason": "signal"}, stop_requested=True).toast)
        self.assertIsNotNone(self.view.apply({"ev": "kill", "reason": "signal"}, stop_requested=False).toast)

    def test_screen_event_is_authoritative(self):
        outcome = self.view.apply({"ev": "screen", "off": False, "method": "none"})
        self.assertTrue(outcome.emit_status)
        self.assertIs(self.view.screen_off, False)

    def test_unknown_event_is_ignored(self):
        with self.assertLogs("controller_backend.session", level="DEBUG") as logs:
            outcome = self.view.apply({"ev": "weather", "sunny": True})
        self.assertFalse(outcome.emit_status)
        self.assertIn("unhandled", logs.output[0])


if __name__ == "__main__":
    unittest.main()
