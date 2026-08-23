"""Decky backend (``main.py``): settings sanitising/persistence, sysfs snapshot, CLI status normalising,
daemon event → Status mapping and the daemon CLI argument contract. No real daemon is ever spawned."""
import contextlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import types
import unittest
from unittest import mock

import _path  # noqa: F401

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import main  # noqa: E402
from deckgadget.__main__ import build_parser, config_from_args  # noqa: E402
from fakes import FakeSysfs, read, write  # noqa: E402


def deck_sysfs(root, *, udc_state="not attached", acad_online=1, pd_mv=5000, pd_ma=1500, usb_host=0,
               drd=True, neptune=True):
    fs = FakeSysfs(root)
    fs.add_power_supply(acad_online=acad_online).add_hwmon(pd_mv=pd_mv, pd_ma=pd_ma)
    fs.add_extcon(usb=0 if usb_host else 1, usb_host=usb_host).add_udc(state=udc_state, speed="high-speed")
    fs.add_pci_bus()
    write(os.path.join(fs.sys, "class", "dmi", "id", "product_name"), "Galileo\n")
    if drd:
        os.makedirs(os.path.join(fs.sys, "bus", "pci", "drivers", "dwc3-pci", "0000:04:00.3"))
        write(os.path.join(fs.sys, "bus", "pci", "drivers", "dwc3-pci", "bind"), "")
    if neptune:
        fs.add_neptune()
    return fs


@contextlib.contextmanager
def plugin_dirs(root):
    with mock.patch.multiple(main.decky,
                             DECKY_PLUGIN_SETTINGS_DIR=os.path.join(root, "settings"),
                             DECKY_PLUGIN_RUNTIME_DIR=os.path.join(root, "runtime"),
                             DECKY_PLUGIN_LOG_DIR=os.path.join(root, "logs")):
        yield


class FileHelpersTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="main_helpers_")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def test_tail_file_returns_last_lines(self):
        path = os.path.join(self.tmp, "daemon.log")
        write(path, "one\ntwo\nthree\nfour\n")
        self.assertEqual(main._tail_file(path, 2), ["three", "four"])
        self.assertEqual(main._tail_file(path, 10), ["one", "two", "three", "four"])

    def test_tail_file_drops_partial_first_line_when_truncated(self):
        path = os.path.join(self.tmp, "daemon.log")
        write(path, "0123456789\nabcdef\nxyz\n")
        self.assertEqual(main._tail_file(path, 5, max_bytes=12), ["abcdef", "xyz"])

    def test_tail_file_missing_is_empty(self):
        self.assertEqual(main._tail_file(os.path.join(self.tmp, "nope"), 3), [])

    def test_parse_json_object_tolerates_log_noise(self):
        self.assertEqual(main._parse_json_object('{"ok": true}'), {"ok": True})
        self.assertEqual(main._parse_json_object('INFO starting\nWARN x\n{"ok": false, "n": 2}\n'),
                         {"ok": False, "n": 2})
        self.assertIsNone(main._parse_json_object(""))
        self.assertIsNone(main._parse_json_object("[1, 2]"))
        self.assertIsNone(main._parse_json_object("just text"))

    def test_daemon_env_drops_ld_library_path_and_unbuffers(self):
        with mock.patch.dict(os.environ, {"LD_LIBRARY_PATH": "/decky/bundle", "HOME": "/home/deck"}, clear=True):
            environment = main._daemon_env()
        self.assertNotIn("LD_LIBRARY_PATH", environment)
        self.assertEqual(environment["PYTHONUNBUFFERED"], "1")
        self.assertEqual(environment["HOME"], "/home/deck")

    def test_is_deckgadget_pid_checks_cmdline(self):
        self.assertFalse(main._is_deckgadget_pid(-1))
        self.assertFalse(main._is_deckgadget_pid(os.getpid()))
        sleeper = subprocess.Popen([sys.executable, "-c", "import time; print('up', flush=True); time.sleep(30)",
                                    "deckgadget"], stdout=subprocess.PIPE)
        try:
            sleeper.stdout.readline()   # cmdline is populated once the child runs
            self.assertTrue(main._is_deckgadget_pid(sleeper.pid))
        finally:
            sleeper.kill()
            sleeper.wait()
            sleeper.stdout.close()


class SanitizeSettingsTest(unittest.TestCase):
    def sanitize(self, partial):
        return main.sanitize_settings(partial, main.DEFAULT_SETTINGS)

    def test_none_and_non_dict(self):
        merged, warnings = self.sanitize(None)
        self.assertEqual((merged, warnings), (main.DEFAULT_SETTINGS, []))
        merged, warnings = self.sanitize(["profile"])
        self.assertEqual(merged, main.DEFAULT_SETTINGS)
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
        self.assertEqual(merged["paddles"], main.DEFAULT_SETTINGS["paddles"])
        self.assertEqual(len(warnings), 3)
        merged, warnings = self.sanitize({"paddles": "A"})
        self.assertEqual(warnings, ["paddles: must be an object"])

    def test_unknown_keys_are_ignored_and_base_untouched(self):
        base = {**main.DEFAULT_SETTINGS, "paddles": dict(main.DEFAULT_SETTINGS["paddles"])}
        merged, warnings = main.sanitize_settings({"bogus": 1, "paddles": {"L4": "B"}}, base)
        self.assertNotIn("bogus", merged)
        self.assertEqual(warnings, [])
        self.assertEqual(base["paddles"]["L4"], "none")
        self.assertEqual(merged["paddles"]["L4"], "B")

    def test_resolve_transport(self):
        self.assertEqual(main.resolve_transport("xbox360", "auto"), "raw")
        self.assertEqual(main.resolve_transport("hid_gamepad", "auto"), "hid")
        self.assertEqual(main.resolve_transport("hid_gamepad", "raw"), "raw")


class SettingsStoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="main_settings_")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.path = os.path.join(self.tmp, "settings", "settings.json")
        self.store = main.SettingsStore(self.path)

    def test_missing_file_gives_defaults_without_creating_it(self):
        self.assertEqual(self.store.load(), main.DEFAULT_SETTINGS)
        self.assertFalse(os.path.exists(self.path))

    def test_load_merges_persisted_partial_file(self):
        write(self.path, json.dumps({"profile": "hid_gamepad", "paddles": {"R4": "RB"}, "kill_hold_ms": 20}))
        settings = self.store.load()
        self.assertEqual(settings["profile"], "hid_gamepad")
        self.assertEqual(settings["paddles"], {"L4": "none", "L5": "none", "R4": "RB", "R5": "none"})
        self.assertEqual(settings["kill_hold_ms"], 200)
        self.assertEqual(settings["kill_combo"], "L4+R4")

    def test_corrupt_file_gives_defaults(self):
        write(self.path, "{not json")
        with self.assertLogs(main.decky.logger, level="WARNING"):
            self.assertEqual(self.store.load(), main.DEFAULT_SETTINGS)

    def test_save_writes_json_atomically(self):
        settings = {**main.DEFAULT_SETTINGS, "profile": "hid_gamepad"}
        self.store.save(settings)
        self.assertEqual(json.loads(read(self.path))["profile"], "hid_gamepad")
        self.assertFalse(os.path.exists(self.path + ".tmp"))
        self.assertEqual(main.SettingsStore(self.path).load()["profile"], "hid_gamepad")

    def test_update_persists_and_reports_warnings(self):
        merged, warnings = self.store.update({"kill_combo": "L5+R5", "profile": "nope"})
        self.assertEqual(merged["kill_combo"], "L5+R5")
        self.assertEqual(len(warnings), 1)
        self.assertEqual(json.loads(read(self.path))["kill_combo"], "L5+R5")
        merged["kill_combo"] = "mutated"
        self.assertEqual(self.store.load()["kill_combo"], "L5+R5")


class NormalizeCliStatusTest(unittest.TestCase):
    def test_canonical_keys_pass_through_and_unknown_are_dropped(self):
        raw = {"drd_enabled": 1, "udc_name": "dwc3.1.auto", "udc_state": "configured", "host_connected": 1,
               "neptune_present": True, "neptune_captured": 0, "cable_kind": "pc", "pd_contract_mv": 5000,
               "kernel": "6.16", "errors": [], "gadgets": [], "version": "0.1.0"}
        result = main._normalize_cli_status(raw)
        self.assertEqual(result, {"drd_enabled": True, "udc_name": "dwc3.1.auto", "udc_state": "configured",
                                  "host_connected": True, "neptune_present": True, "neptune_captured": False,
                                  "cable_kind": "pc", "pd_contract_mv": 5000, "kernel": "6.16"})

    def test_nested_and_short_spellings(self):
        raw = {"drd": {"enabled": True}, "udc": {"name": "dwc3.1.auto", "state": "not attached"},
               "neptune": {"present": 1, "captured": 1}, "connected": 0,
               "extcon": {"USB": 1, "USB-HOST": 0}}
        result = main._normalize_cli_status(raw)
        self.assertEqual(result, {"drd_enabled": True, "udc_name": "dwc3.1.auto", "udc_state": "not attached",
                                  "neptune_present": True, "neptune_captured": True, "host_connected": False,
                                  "extcon": {"USB": 1, "USB-HOST": 0}})

    def test_nulls_and_malformed_extcon_are_skipped(self):
        result = main._normalize_cli_status({"cable_power": None, "extcon": "garbage", "udc_name": None})
        self.assertEqual(result, {})


class SysfsSnapshotTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="main_sysfs_")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def test_pc_plugged_idle(self):
        fs = deck_sysfs(self.tmp)
        snapshot = main._sysfs_snapshot(fs.sys)
        self.assertEqual(snapshot["model"], "Galileo")
        self.assertTrue(snapshot["drd_enabled"])
        self.assertEqual((snapshot["udc_name"], snapshot["udc_state"], snapshot["udc_speed"]),
                         ("dwc3.1.auto", "not attached", "high-speed"))
        self.assertFalse(snapshot["host_connected"])
        self.assertEqual(snapshot["extcon"], {"USB": 1, "USB-HOST": 0})
        self.assertIs(snapshot["cable_power"], True)
        self.assertEqual((snapshot["pd_contract_mv"], snapshot["pd_contract_ma"]), (5000, 1500))
        self.assertEqual(snapshot["cable_kind"], "pc")
        self.assertTrue(snapshot["neptune_present"])

    def test_configured_means_host_connected(self):
        fs = deck_sysfs(self.tmp, udc_state="configured")
        self.assertTrue(main._sysfs_snapshot(fs.sys)["host_connected"])

    def test_charger_dock_and_unplugged_classification(self):
        self.assertEqual(main._sysfs_snapshot(deck_sysfs(os.path.join(self.tmp, "a"), pd_mv=20000).sys)["cable_kind"],
                         "charger")
        self.assertEqual(main._sysfs_snapshot(deck_sysfs(os.path.join(self.tmp, "b"), usb_host=1).sys)["cable_kind"],
                         "host_device")
        unplugged = main._sysfs_snapshot(deck_sysfs(os.path.join(self.tmp, "c"), acad_online=0, pd_mv=0).sys)
        self.assertEqual((unplugged["cable_kind"], unplugged["cable_power"]), ("none", False))

    def test_drd_off_and_no_neptune(self):
        fs = deck_sysfs(self.tmp, drd=False, neptune=False)
        snapshot = main._sysfs_snapshot(fs.sys)
        self.assertFalse(snapshot["drd_enabled"])
        self.assertFalse(snapshot["neptune_present"])

    def test_empty_sysfs_is_all_unknown(self):
        snapshot = main._sysfs_snapshot(os.path.join(self.tmp, "empty"))
        self.assertEqual((snapshot["drd_enabled"], snapshot["udc_name"], snapshot["cable_kind"],
                          snapshot["neptune_present"], snapshot["cable_power"]),
                         (False, None, "unknown", False, None))

    def test_connectivity_signature_tracks_only_port_facts(self):
        fs = deck_sysfs(self.tmp)
        before = main._connectivity_signature(main._sysfs_snapshot(fs.sys))
        self.assertEqual(before, main._connectivity_signature(main._sysfs_snapshot(fs.sys)))
        write(os.path.join(fs.sys, "class", "dmi", "id", "product_name"), "Jupiter\n")
        self.assertEqual(before, main._connectivity_signature(main._sysfs_snapshot(fs.sys)))
        fs.set_udc_state("configured")
        self.assertNotEqual(before, main._connectivity_signature(main._sysfs_snapshot(fs.sys)))


class BackendTestCase(unittest.IsolatedAsyncioTestCase):
    """A ``_Backend`` with plugin dirs in a temp tree, sysfs redirected to a fake Deck, ``deckgadget``
    CLI calls answered from ``self.cli_replies`` and emitted events collected in ``self.emitted``."""

    async def asyncSetUp(self):
        self.tmp = tempfile.mkdtemp(prefix="main_backend_")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.fs = deck_sysfs(os.path.join(self.tmp, "deck"))
        self.emitted = []
        self.cli_calls = []
        self.cli_replies = {
            "status": (0, json.dumps({"ok": True, "drd_enabled": True, "neptune_captured": False}), ""),
            "recover": (0, json.dumps({"ok": True, "errors": []}), ""),
        }
        real_snapshot = main._sysfs_snapshot

        async def fake_emit(event, *args):
            self.emitted.append((event, args[0] if args else None))

        async def fake_run_cli(backend, *args, timeout):
            self.cli_calls.append(args)
            return self.cli_replies[args[0]]

        self.enterContext(plugin_dirs(self.tmp))
        self.enterContext(mock.patch.object(main.decky, "emit", fake_emit))
        self.enterContext(mock.patch.object(main._Backend, "_run_cli", fake_run_cli))
        self.enterContext(mock.patch.object(main, "_sysfs_snapshot", lambda sysfs="/sys": real_snapshot(self.fs.sys)))
        self.backend = main._Backend()

    def events(self, name):
        return [payload for event, payload in self.emitted if event == name]

    def fake_process(self, pid=4242, returncode=None):
        self.backend.process = types.SimpleNamespace(pid=pid, returncode=returncode)


class BackendEventMappingTest(BackendTestCase):
    async def test_state_events_drive_session_state_while_running(self):
        self.fake_process()
        await self.backend._on_daemon_event({"ev": "state", "state": "CAPTURING", "detail": "lizard off"})
        await self.backend._on_daemon_event({"ev": "state", "state": "ACTIVE", "detail": ""})
        status = self.events("status")[-1]
        self.assertEqual((status["session_state"], status["daemon_running"], status["daemon_pid"]),
                         ("ACTIVE", True, 4242))
        self.assertTrue(status["neptune_captured"])
        self.assertTrue(self.backend.first_event.is_set())
        self.assertEqual(self.events("status")[0]["session_detail"], "lizard off")

    async def test_stopped_shows_as_stopping_until_the_process_is_gone(self):
        self.fake_process()
        await self.backend._on_daemon_event({"ev": "state", "state": "STOPPED"})
        self.assertEqual(self.backend.session_state, "STOPPING")
        self.assertEqual(self.events("status")[-1]["session_state"], "STOPPING")
        self.backend.process = None
        self.assertEqual((await self.backend.build_status())["session_state"], "IDLE")

    async def test_unknown_state_is_ignored(self):
        await self.backend._on_daemon_event({"ev": "state", "state": "FLYING"})
        self.assertEqual(self.backend.session_state, "IDLE")

    async def test_error_event_sets_last_error(self):
        self.fake_process()
        await self.backend._on_daemon_event({"ev": "error", "msg": "no UDC"})
        self.assertEqual(self.backend.last_error, "no UDC")
        self.assertEqual(self.events("status")[-1]["last_error"], "no UDC")

    async def test_metrics_event_updates_numbers_only_and_does_not_emit(self):
        await self.backend._on_daemon_event({"ev": "metrics", "hz": 249.9, "reports": 1200, "dropped": "x",
                                             "extra": 1})
        self.assertEqual(self.backend.metrics, {"hz": 249.9, "reports": 1200, "dropped": 0})
        self.assertEqual(self.events("status"), [])

    async def test_kill_reasons_toast(self):
        await self.backend._on_daemon_event({"ev": "kill", "reason": "combo"})
        await self.backend._on_daemon_event({"ev": "kill", "reason": "unplug"})
        await self.backend._on_daemon_event({"ev": "kill", "reason": "error"})
        toasts = self.events("toast")
        self.assertEqual([toast["severity"] for toast in toasts], ["info", "info"])
        self.assertIn("Exit combo", toasts[0]["body"])
        self.assertIn("cable", toasts[1]["body"])
        self.assertEqual(self.backend.last_kill, "error")

    async def test_signal_kill_toasts_only_when_not_requested(self):
        self.backend.stop_requested = True
        await self.backend._on_daemon_event({"ev": "kill", "reason": "signal"})
        self.assertEqual(self.events("toast"), [])
        self.backend.stop_requested = False
        await self.backend._on_daemon_event({"ev": "kill", "reason": "signal"})
        self.assertEqual(len(self.events("toast")), 1)

    async def test_screen_event_is_authoritative(self):
        self.fake_process()
        await self.backend._on_daemon_event({"ev": "state", "state": "ACTIVE"})
        self.assertTrue(self.events("status")[-1]["screen_off"])          # inferred from settings + state
        await self.backend._on_daemon_event({"ev": "screen", "off": False, "method": "none"})
        self.assertFalse(self.events("status")[-1]["screen_off"])


class BackendStatusTest(BackendTestCase):
    async def test_idle_status_combines_sysfs_and_cli(self):
        status = await self.backend.build_status()
        self.assertTrue(status["ok"])
        self.assertEqual((status["session_state"], status["daemon_running"], status["daemon_pid"]),
                         ("IDLE", False, None))
        self.assertEqual(status["cable_kind"], "pc")
        self.assertTrue(status["drd_enabled"])
        self.assertIsNone(status["status_error"])
        self.assertFalse(status["screen_off"])
        self.assertEqual(status["metrics"], {"hz": 0, "reports": 0, "dropped": 0})

    async def test_cli_failure_falls_back_to_sysfs_and_reports_status_error(self):
        self.cli_replies["status"] = (1, "Traceback…", "boom")
        status = await self.backend.build_status(force=True)
        self.assertIn("printed no JSON object", status["status_error"])
        self.assertIn("boom", status["status_error"])
        self.assertEqual(status["cable_kind"], "pc")

    async def test_cli_status_is_cached_within_ttl(self):
        await self.backend.build_status()
        await self.backend.build_status()
        self.assertEqual(self.cli_calls.count(("status",)), 1)
        await self.backend.build_status(force=True)
        self.assertEqual(self.cli_calls.count(("status",)), 2)


class BackendRecoverTest(BackendTestCase):
    async def test_stop_without_daemon_still_recovers_and_emits(self):
        status = await self.backend.stop("user")
        self.assertEqual(self.cli_calls[0], ("recover",))
        self.assertTrue(self.backend.last_recover["ok"])
        self.assertEqual(status["session_state"], "IDLE")
        self.assertEqual(len(self.events("status")), 1)
        self.assertEqual(self.events("toast"), [])

    async def test_recover_report_errors_surface_as_toast_and_last_error(self):
        self.cli_replies["recover"] = (0, json.dumps({"ok": False, "errors": ["neptune: still detached"]}), "")
        self.assertFalse(await self.backend._recover("test"))
        self.assertIn("still detached", self.backend.last_error)
        toast = self.events("toast")[-1]
        self.assertEqual(toast["severity"], "error")
        self.assertIn("reboot", toast["body"])

    async def test_recover_without_json_report_fails(self):
        self.cli_replies["recover"] = (0, "nothing useful", "")
        self.assertFalse(await self.backend._recover("test"))
        self.assertIn("no JSON report", self.backend.last_error)


class DaemonArgsContractTest(BackendTestCase):
    async def test_daemon_args_round_trip_through_the_daemon_cli(self):
        settings = {**main.DEFAULT_SETTINGS, "transport": "raw", "kill_combo": "L5+R5", "kill_hold_ms": 800,
                    "screen_off": True, "touch_wake_seconds": 7,
                    "paddles": {"L4": "A", "L5": "none", "R4": "DPAD_LEFT", "R5": "MENU"}}
        args = self.backend._daemon_args(settings, "hid_gamepad")
        config = config_from_args(build_parser().parse_args(["run", *args]))
        self.assertEqual((config.profile, config.transport, config.resolved_transport), ("hid_gamepad", "raw", "raw"))
        self.assertEqual((config.kill_combo, config.kill_hold_ms), ("L5+R5", 800))
        self.assertTrue(config.screen_off)
        self.assertEqual(config.touch_wake_seconds, 7.0)
        self.assertEqual(config.paddles, settings["paddles"])
        self.assertEqual(config.screen_method, "auto")
        self.assertIn("--log-file", args)

    async def test_screen_off_false_omits_the_flag(self):
        args = self.backend._daemon_args({**main.DEFAULT_SETTINGS, "screen_off": False}, "xbox360")
        self.assertNotIn("--screen-off", args)
        self.assertFalse(config_from_args(build_parser().parse_args(["run", *args])).screen_off)

    async def test_backend_value_lists_match_the_daemon(self):
        from deckgadget import config as daemon_config

        self.assertEqual(main.PROFILES, daemon_config.PROFILES)
        self.assertEqual(main.TRANSPORTS, daemon_config.TRANSPORTS)
        self.assertEqual(main.KILL_COMBOS, daemon_config.KILL_COMBOS)
        self.assertEqual(main.PADDLE_ACTIONS, daemon_config.PADDLE_TARGETS)
        self.assertEqual(main.PADDLES, daemon_config.PADDLE_NAMES)


class PluginCallablesTest(BackendTestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.enterContext(mock.patch.object(main, "_BACKEND", self.backend))
        self.plugin = main.Plugin()

    async def test_get_and_set_settings(self):
        self.assertEqual(await self.plugin.get_settings(), {"ok": True, **main.DEFAULT_SETTINGS})
        result = await self.plugin.set_settings({"profile": "hid_gamepad", "kill_hold_ms": 99999, "transport": "x"})
        self.assertTrue(result["ok"])
        self.assertEqual((result["profile"], result["kill_hold_ms"], result["transport"]), ("hid_gamepad", 10000, "auto"))
        self.assertEqual(len(result["warnings"]), 1)
        self.assertEqual((await self.plugin.get_settings())["profile"], "hid_gamepad")

    async def test_callables_answer_errors_as_dicts(self):
        self.assertEqual((await self.plugin.start(123))["ok"], False)
        result = await self.plugin.start("ds4")
        self.assertFalse(result["ok"])
        self.assertIn("unknown profile", result["error"])
        self.assertFalse(self.backend.daemon_alive())

    async def test_get_status_and_diagnostics(self):
        status = await self.plugin.get_status()
        self.assertTrue(status["ok"])
        self.assertEqual(status["session_state"], "IDLE")
        diagnostics = await self.plugin.get_diagnostics()
        self.assertTrue(diagnostics["ok"])
        self.assertEqual(diagnostics["status"]["cable_kind"], "pc")
        self.assertEqual(diagnostics["daemon"]["running"], False)
        self.assertEqual(diagnostics["settings"], main.DEFAULT_SETTINGS)


if __name__ == "__main__":
    unittest.main()
