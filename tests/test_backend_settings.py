"""``controller_backend.settings``: sanitising partial updates and the JSON settings store."""
import json
import os
import shutil
import tempfile
import unittest

import _path  # noqa: F401

from controller_backend import settings as S
from fakes import read, write


class SanitizeSettingsTest(unittest.TestCase):
    def sanitize(self, partial):
        return S.sanitize_settings(partial, S.DEFAULT_SETTINGS)

    def test_none_and_non_dict(self):
        merged, warnings = self.sanitize(None)
        self.assertEqual((merged, warnings), (S.DEFAULT_SETTINGS, []))
        merged, warnings = self.sanitize(["profile"])
        self.assertEqual(merged, S.DEFAULT_SETTINGS)
        self.assertEqual(warnings, ["settings must be a JSON object"])

    def test_valid_choices_are_applied(self):
        merged, warnings = self.sanitize({"profile": "hid_gamepad", "transport": "hid", "kill_combo": "STEAM+QAM",
                                          "screen_off": False})
        self.assertEqual(warnings, [])
        self.assertEqual((merged["profile"], merged["transport"], merged["kill_combo"], merged["screen_off"]),
                         ("hid_gamepad", "hid", "STEAM+QAM", False))

    def test_invalid_choices_keep_previous_and_warn(self):
        merged, warnings = self.sanitize({"profile": "ds4", "transport": "ble", "kill_combo": "l4+r4",
                                          "screen_off": "yes"})
        self.assertEqual((merged["profile"], merged["transport"], merged["kill_combo"], merged["screen_off"]),
                         ("xbox360", "auto", "L4+R4", True))
        self.assertEqual(len(warnings), 4)
        self.assertTrue(any(warning.startswith("profile:") for warning in warnings))
        self.assertIn("screen_off: must be a boolean", warnings)

    def test_integers_are_clamped(self):
        merged, warnings = self.sanitize({"kill_hold_ms": 50, "touch_wake_seconds": 999})
        self.assertEqual((merged["kill_hold_ms"], merged["touch_wake_seconds"]), (200, 60))
        self.assertEqual(warnings, [])
        merged, _ = self.sanitize({"kill_hold_ms": 99999, "touch_wake_seconds": 0})
        self.assertEqual((merged["kill_hold_ms"], merged["touch_wake_seconds"]), (10000, 1))
        merged, _ = self.sanitize({"kill_hold_ms": 2500.7})
        self.assertEqual(merged["kill_hold_ms"], 2500)

    def test_non_numbers_and_booleans_are_rejected_as_integers(self):
        merged, warnings = self.sanitize({"kill_hold_ms": "abc", "touch_wake_seconds": True})
        self.assertEqual((merged["kill_hold_ms"], merged["touch_wake_seconds"]), (1500, 5))
        self.assertEqual(len(warnings), 2)

    def test_paddles_merge_partially(self):
        merged, warnings = self.sanitize({"paddles": {"L4": "A", "R5": "DPAD_LEFT"}})
        self.assertEqual(merged["paddles"], {"L4": "A", "L5": "none", "R4": "none", "R5": "DPAD_LEFT"})
        self.assertEqual(warnings, [])
        merged, warnings = self.sanitize({"paddles": {"L4": "a", "L9": "A", "R4": 3}})
        self.assertEqual(merged["paddles"], S.DEFAULT_SETTINGS["paddles"])
        self.assertEqual(len(warnings), 3)
        merged, warnings = self.sanitize({"paddles": "A"})
        self.assertEqual(warnings, ["paddles: must be an object"])

    def test_unknown_keys_are_ignored_and_base_untouched(self):
        base = {**S.DEFAULT_SETTINGS, "paddles": dict(S.DEFAULT_SETTINGS["paddles"])}
        merged, warnings = S.sanitize_settings({"bogus": 1, "paddles": {"L4": "B"}}, base)
        self.assertNotIn("bogus", merged)
        self.assertEqual(warnings, [])
        self.assertEqual(base["paddles"]["L4"], "none")
        self.assertEqual(merged["paddles"]["L4"], "B")

    def test_resolve_transport(self):
        self.assertEqual(S.resolve_transport("xbox360", "auto"), "raw")
        self.assertEqual(S.resolve_transport("hid_gamepad", "auto"), "hid")
        self.assertEqual(S.resolve_transport("hid_gamepad", "raw"), "raw")


class SettingsStoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="backend_settings_")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.path = os.path.join(self.tmp, "settings", "settings.json")
        self.store = S.SettingsStore(self.path)

    def test_missing_file_gives_defaults_without_creating_it(self):
        self.assertEqual(self.store.load(), S.DEFAULT_SETTINGS)
        self.assertFalse(os.path.exists(self.path))

    def test_load_merges_persisted_partial_file(self):
        write(self.path, json.dumps({"profile": "hid_gamepad", "paddles": {"R4": "RB"}, "kill_hold_ms": 20}))
        loaded = self.store.load()
        self.assertEqual(loaded["profile"], "hid_gamepad")
        self.assertEqual(loaded["paddles"], {"L4": "none", "L5": "none", "R4": "RB", "R5": "none"})
        self.assertEqual(loaded["kill_hold_ms"], 200)
        self.assertEqual(loaded["kill_combo"], "L4+R4")

    def test_corrupt_file_gives_defaults_with_a_warning(self):
        write(self.path, "{not json")
        with self.assertLogs("controller_backend.settings", level="WARNING"):
            self.assertEqual(self.store.load(), S.DEFAULT_SETTINGS)

    def test_save_writes_json_atomically(self):
        self.store.save({**S.DEFAULT_SETTINGS, "profile": "hid_gamepad"})
        self.assertEqual(json.loads(read(self.path))["profile"], "hid_gamepad")
        self.assertFalse(os.path.exists(self.path + ".tmp"))
        self.assertEqual(S.SettingsStore(self.path).load()["profile"], "hid_gamepad")

    def test_update_persists_and_reports_warnings(self):
        merged, warnings = self.store.update({"kill_combo": "L5+R5", "profile": "nope"})
        self.assertEqual(merged["kill_combo"], "L5+R5")
        self.assertEqual(len(warnings), 1)
        self.assertEqual(json.loads(read(self.path))["kill_combo"], "L5+R5")
        merged["kill_combo"] = "mutated"
        self.assertEqual(self.store.load()["kill_combo"], "L5+R5")


if __name__ == "__main__":
    unittest.main()
