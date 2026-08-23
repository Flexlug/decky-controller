import contextlib
import io
import unittest

import _path  # noqa: F401

from deckgadget import config as C
from deckgadget import state as S
from deckgadget.__main__ import build_parser, config_from_args
from deckgadget.platform.display.backlight import Backlight
from deckgadget.platform.display.controller import AUTO_METHOD, ScreenController


class ConfigTest(unittest.TestCase):
    def test_resolve_transport(self):
        self.assertEqual(C.resolve_transport("xbox360", "auto"), "raw")
        self.assertEqual(C.resolve_transport("hid_gamepad", "auto"), "hid")
        self.assertEqual(C.resolve_transport("hid_gamepad", "raw"), "raw")
        self.assertEqual(C.resolve_transport("xbox360", "raw"), "raw")
        with self.assertRaises(C.ConfigError):
            C.resolve_transport("xbox360", "hid")
        with self.assertRaises(C.ConfigError):
            C.resolve_transport("ds4", "auto")
        with self.assertRaises(C.ConfigError):
            C.resolve_transport("xbox360", "ble")

    def test_kill_combo(self):
        self.assertEqual(C.parse_kill_combo("L4+R4"), S.BTN_L4 | S.BTN_R4)
        self.assertEqual(C.parse_kill_combo(" l5 + r5 "), S.BTN_L5 | S.BTN_R5)
        self.assertEqual(C.parse_kill_combo("L4+L5+R4+R5"), S.BTN_L4 | S.BTN_L5 | S.BTN_R4 | S.BTN_R5)
        self.assertEqual(C.parse_kill_combo("STEAM+QAM"), S.BTN_STEAM | S.BTN_QAM)
        for bad in ("A+B", "L4", "", "L4+R4+X"):
            with self.assertRaises(C.ConfigError):
                C.parse_kill_combo(bad)

    def test_paddles(self):
        self.assertEqual(C.parse_paddles(None), {"L4": "none", "L5": "none", "R4": "none", "R5": "none"})
        self.assertEqual(C.parse_paddles("L4=A,R4=dpad_left, L5 = NONE"),
                         {"L4": "A", "L5": "none", "R4": "DPAD_LEFT", "R5": "none"})
        for bad in ("L4", "L9=A", "L4=Z", "L4=A,junk"):
            with self.assertRaises(C.ConfigError):
                C.parse_paddles(bad)
        self.assertEqual(C.validate_paddles({"l4": "lb"})["L4"], "LB")
        with self.assertRaises(C.ConfigError):
            C.validate_paddles({"L4": "LT"})

    def test_run_config_defaults_and_validation(self):
        cfg = C.RunConfig()
        self.assertEqual(cfg.resolved_transport, "raw")
        self.assertEqual(cfg.kill_mask, S.BTN_L4 | S.BTN_R4)
        self.assertAlmostEqual(cfg.kill_hold_s, 1.5)
        self.assertEqual(cfg.as_dict()["paddles"], C.DEFAULT_PADDLES)
        with self.assertRaises(C.ConfigError):
            C.RunConfig(kill_hold_ms=50)
        with self.assertRaises(C.ConfigError):
            C.RunConfig(kill_hold_ms="abc")
        with self.assertRaises(C.ConfigError):
            C.RunConfig(touch_wake_seconds=0)
        with self.assertRaises(C.ConfigError):
            C.RunConfig(profile="xbox360", transport="hid")
        cfg = C.RunConfig(profile="hid_gamepad", transport="raw", kill_combo="STEAM+QAM", kill_hold_ms=2000,
                          screen_off=True, touch_wake_seconds=3, paddles={"R5": "menu"})
        self.assertEqual(cfg.resolved_transport, "raw")
        self.assertEqual(cfg.paddles["R5"], "MENU")
        self.assertEqual(cfg.screen_method, "auto")
        self.assertEqual(cfg.as_dict()["screen_method"], "auto")
        self.assertEqual(C.RunConfig(screen_method="Gamescope").screen_method, "gamescope")
        with self.assertRaises(C.ConfigError):
            C.RunConfig(screen_method="dpms")

    def test_screen_methods_match_the_controller_strategies(self):
        controller = ScreenController(backlight=Backlight("/nonexistent"), touch_event="")
        self.assertEqual(C.SCREEN_METHODS, (AUTO_METHOD, *(strategy.name for strategy in controller.strategies)))
        with self.assertRaises(ValueError):
            ScreenController(backlight=Backlight("/nonexistent"), touch_event="", method="dpms")

    def test_cli_parser_to_config(self):
        ap = build_parser()
        args = ap.parse_args(["run", "--profile", "hid_gamepad", "--kill-combo", "L5+R5", "--kill-hold-ms", "800",
                              "--screen-off", "--touch-wake-seconds", "7", "--paddles", "L4=A,R4=B"])
        cfg = config_from_args(args)
        self.assertEqual(cfg.profile, "hid_gamepad")
        self.assertEqual(cfg.resolved_transport, "hid")
        self.assertEqual(cfg.kill_mask, S.BTN_L5 | S.BTN_R5)
        self.assertEqual(cfg.kill_hold_ms, 800)
        self.assertTrue(cfg.screen_off)
        self.assertEqual(cfg.touch_wake_seconds, 7.0)
        self.assertEqual(cfg.screen_method, "auto")          # default when main.py passes only --screen-off
        self.assertEqual(cfg.paddles, {"L4": "A", "L5": "none", "R4": "B", "R5": "none"})
        self.assertFalse(cfg.demo)
        demo = config_from_args(ap.parse_args(["demo"]), demo=True)
        self.assertTrue(demo.demo)
        self.assertEqual(demo.resolved_transport, "raw")
        ks = config_from_args(ap.parse_args(["run", "--screen-off", "--screen-method", "kscreen"]))
        self.assertEqual(ks.screen_method, "kscreen")
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            ap.parse_args(["run", "--screen-method", "dpms"])
        args = ap.parse_args(["probe", "--seconds", "3"])
        self.assertEqual(args.seconds, 3.0)
        self.assertEqual(ap.parse_args(["status"]).cmd, "status")
        self.assertEqual(ap.parse_args(["recover"]).cmd, "recover")
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            ap.parse_args(["run", "--profile", "ds4"])


if __name__ == "__main__":
    unittest.main()
