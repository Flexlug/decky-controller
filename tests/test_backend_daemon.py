"""``controller_backend.daemon``: launcher argv/env, stdout event parsing, one-shot CLI commands."""
import asyncio
import json
import os
import subprocess
import sys
import unittest
from unittest import mock

import _path  # noqa: F401

from controller_backend.daemon import commands, events, launcher
from controller_backend.settings import DEFAULT_SETTINGS
from deckgadget import config as daemon_config
from deckgadget.__main__ import build_parser, config_from_args


class FakeCliRunner:
    """Answers ``run(subcommand)`` from ``replies`` (a tuple or an exception) and records the calls."""

    def __init__(self, replies):
        self.replies = replies
        self.calls = []

    async def run(self, subcommand, *args, timeout):
        self.calls.append((subcommand, args, timeout))
        reply = self.replies[subcommand]
        if isinstance(reply, BaseException):
            raise reply
        return reply


class LauncherTest(unittest.TestCase):
    def test_run_args_round_trip_through_the_daemon_cli(self):
        settings = {**DEFAULT_SETTINGS, "transport": "raw", "kill_combo": "L5+R5", "kill_hold_ms": 800,
                    "screen_off": True, "touch_wake_seconds": 7,
                    "paddles": {"L4": "A", "L5": "none", "R4": "DPAD_LEFT", "R5": "MENU"}}
        args = launcher.run_args(settings, "hid_gamepad", "/tmp/deckgadget.log")
        config = config_from_args(build_parser().parse_args(["run", *args]))
        self.assertEqual((config.profile, config.transport, config.resolved_transport), ("hid_gamepad", "raw", "raw"))
        self.assertEqual((config.kill_combo, config.kill_hold_ms), ("L5+R5", 800))
        self.assertTrue(config.screen_off)
        self.assertEqual(config.touch_wake_seconds, 7.0)
        self.assertEqual(config.paddles, settings["paddles"])
        self.assertEqual(config.screen_method, "auto")
        self.assertEqual(config.log_file, "/tmp/deckgadget.log")

    def test_screen_off_false_omits_the_flag(self):
        args = launcher.run_args({**DEFAULT_SETTINGS, "screen_off": False}, "xbox360", "/tmp/x.log")
        self.assertNotIn("--screen-off", args)
        self.assertFalse(config_from_args(build_parser().parse_args(["run", *args])).screen_off)

    def test_daemon_command_uses_the_system_interpreter(self):
        self.assertEqual(launcher.daemon_command("status", "--no-modprobe"),
                         ["/usr/bin/python3", "-m", "deckgadget", "status", "--no-modprobe"])

    def test_paths_under_plugin_dirs(self):
        paths = launcher.DaemonPaths.under("/plugin", "/logs", "/run")
        self.assertEqual((paths.py_modules_dir, paths.log_path, paths.pidfile),
                         ("/plugin/py_modules", "/logs/deckgadget.log", "/run/deckgadget.pid"))

    def test_environment_drops_ld_library_path_and_unbuffers(self):
        with mock.patch.dict(os.environ, {"LD_LIBRARY_PATH": "/decky/bundle", "HOME": "/home/deck"}, clear=True):
            environment = launcher.daemon_environment()
        self.assertNotIn("LD_LIBRARY_PATH", environment)
        self.assertEqual(environment["PYTHONUNBUFFERED"], "1")
        self.assertEqual(environment["HOME"], "/home/deck")

    def test_is_deckgadget_pid_checks_cmdline(self):
        self.assertFalse(launcher.is_deckgadget_pid(-1))
        self.assertFalse(launcher.is_deckgadget_pid(os.getpid()))
        sleeper = subprocess.Popen([sys.executable, "-c", "import time; print('up', flush=True); time.sleep(30)",
                                    "deckgadget"], stdout=subprocess.PIPE)
        try:
            sleeper.stdout.readline()   # cmdline is populated once the child runs
            self.assertTrue(launcher.is_deckgadget_pid(sleeper.pid))
        finally:
            sleeper.kill()
            sleeper.wait()
            sleeper.stdout.close()

    def test_backend_value_lists_match_the_daemon(self):
        from controller_backend import settings
        self.assertEqual(settings.PROFILES, daemon_config.PROFILES)
        self.assertEqual(settings.TRANSPORTS, daemon_config.TRANSPORTS)
        self.assertEqual(settings.KILL_COMBOS, daemon_config.KILL_COMBOS)
        self.assertEqual(settings.PADDLE_ACTIONS, daemon_config.PADDLE_TARGETS)
        self.assertEqual(settings.PADDLES, daemon_config.PADDLE_NAMES)


class EventParsingTest(unittest.TestCase):
    def test_event_line(self):
        self.assertEqual(events.parse_event_line('{"ev": "state", "state": "ACTIVE"}'), {"ev": "state", "state": "ACTIVE"})
        self.assertIsNone(events.parse_event_line("plain log line"))
        self.assertIsNone(events.parse_event_line("[1, 2]"))

    def test_parse_json_object_tolerates_log_noise(self):
        self.assertEqual(events.parse_json_object('{"ok": true}'), {"ok": True})
        self.assertEqual(events.parse_json_object('INFO starting\nWARN x\n{"ok": false, "n": 2}\n'),
                         {"ok": False, "n": 2})
        self.assertIsNone(events.parse_json_object(""))
        self.assertIsNone(events.parse_json_object("[1, 2]"))
        self.assertIsNone(events.parse_json_object("just text"))


class NormalizeCliStatusTest(unittest.TestCase):
    def test_canonical_keys_pass_through_and_unknown_are_dropped(self):
        raw = {"drd_enabled": 1, "udc_name": "dwc3.1.auto", "udc_state": "configured", "host_connected": 1,
               "neptune_present": True, "neptune_captured": 0, "cable_kind": "pc", "pd_contract_mv": 5000,
               "kernel": "6.16", "errors": [], "gadgets": [], "version": "0.1.0"}
        self.assertEqual(commands.normalize_cli_status(raw),
                         {"drd_enabled": True, "udc_name": "dwc3.1.auto", "udc_state": "configured",
                          "host_connected": True, "neptune_present": True, "neptune_captured": False,
                          "cable_kind": "pc", "pd_contract_mv": 5000, "kernel": "6.16"})

    def test_nested_and_short_spellings(self):
        raw = {"drd": {"enabled": True}, "udc": {"name": "dwc3.1.auto", "state": "not attached"},
               "neptune": {"present": 1, "captured": 1}, "connected": 0, "extcon": {"USB": 1, "USB-HOST": 0}}
        self.assertEqual(commands.normalize_cli_status(raw),
                         {"drd_enabled": True, "udc_name": "dwc3.1.auto", "udc_state": "not attached",
                          "neptune_present": True, "neptune_captured": True, "host_connected": False,
                          "extcon": {"USB": 1, "USB-HOST": 0}})

    def test_nulls_and_malformed_extcon_are_skipped(self):
        self.assertEqual(commands.normalize_cli_status({"cable_power": None, "extcon": "garbage", "udc_name": None}), {})


class OneShotCommandsTest(unittest.TestCase):
    def test_status_returns_json_or_an_error(self):
        runner = FakeCliRunner({"status": (0, 'INFO x\n{"ok": true, "drd_enabled": true}', "")})
        self.assertEqual(asyncio.run(commands.run_status(runner)), ({"ok": True, "drd_enabled": True}, None))
        self.assertEqual(runner.calls[0][0], "status")
        runner = FakeCliRunner({"status": (1, "Traceback…", "boom\n")})
        data, error = asyncio.run(commands.run_status(runner))
        self.assertIsNone(data)
        self.assertIn("printed no JSON object", error)
        self.assertIn("boom", error)
        runner = FakeCliRunner({"status": TimeoutError("deckgadget status timed out after 5s")})
        data, error = asyncio.run(commands.run_status(runner))
        self.assertEqual(data, None)
        self.assertIn("timed out", error)

    def test_recover_ok_only_with_exit_zero_and_clean_report(self):
        runner = FakeCliRunner({"recover": (0, json.dumps({"ok": True, "errors": []}), "")})
        report = asyncio.run(commands.run_recover(runner, "test"))
        self.assertTrue(report.ok)
        self.assertEqual((report.reason, report.exit_code, report.errors, report.detail), ("test", 0, [], ""))
        self.assertEqual(set(report.as_dict()), {"ts", "reason", "rc", "ok", "errors", "stdout", "stderr"})

    def test_recover_report_errors_make_it_fail(self):
        runner = FakeCliRunner({"recover": (0, json.dumps({"ok": False, "errors": ["neptune: still detached"]}), "")})
        report = asyncio.run(commands.run_recover(runner, "test"))
        self.assertFalse(report.ok)
        self.assertEqual(report.detail, "neptune: still detached")

    def test_recover_without_report_or_with_exception_fails(self):
        report = asyncio.run(commands.run_recover(FakeCliRunner({"recover": (0, "nothing useful", "")}), "x"))
        self.assertFalse(report.ok)
        self.assertIn("no JSON report", report.detail)
        report = asyncio.run(commands.run_recover(FakeCliRunner({"recover": (2, "", "crash")}), "x"))
        self.assertIn("exited with 2", report.detail)
        report = asyncio.run(commands.run_recover(FakeCliRunner({"recover": TimeoutError("slow")}), "x"))
        self.assertFalse(report.ok)
        self.assertIsNone(report.exit_code)
        self.assertIn("TimeoutError", report.stderr)


if __name__ == "__main__":
    unittest.main()
