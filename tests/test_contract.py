"""Pins the frontend <-> backend <-> daemon contract: value lists, keys, callable and event names."""
import asyncio
import inspect
import os
import re
import shutil
import tempfile
import unittest
from unittest import mock

import _path  # noqa: F401

import main
from deckgadget import config
from deckgadget import session
from deckgadget.__main__ import collect_status
from deckhw.cable import CABLE_KINDS
from deckgadget.util import log as daemon_log
from fakes import FakeSysfs

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
UI_TEXT_BUDGET = 14


def read_frontend(name):
    with open(os.path.join(REPO_ROOT, "src", name), encoding="utf-8") as f:
        return f.read()


TYPES_TS = read_frontend("types.ts")
API_TS = read_frontend("api.ts")
INDEX_TSX = read_frontend("index.tsx")
CONTENT_TSX = read_frontend("Content.tsx")


def ts_union(name):
    """``export type Name = "a" | "b";`` -> ["a", "b"] (in declaration order)."""
    body = re.search(r"export type %s =\s*([^;]+);" % name, TYPES_TS).group(1)
    return re.findall(r'"([^"]+)"', body)


def ts_interface_fields(name):
    """``export interface Name { … }`` -> {field: is_optional}."""
    body = re.search(r"export interface %s \{(.*?)\n\}" % name, TYPES_TS, re.S).group(1)
    return {match.group(1): match.group(2) == "?" for match in re.finditer(r"^\s*(\w+)(\??):", body, re.M)}


def ts_record(const_name):
    """``export const NAME: Record<…> = { key: "text", … };`` -> {key: text} (in declaration order)."""
    body = re.search(r"export const %s\b[^=]*=\s*\{(.*?)\n\};" % const_name, TYPES_TS, re.S).group(1)
    return {match.group(1): match.group(2)
            for match in re.finditer(r'^\s*"?([^"\s:]+)"?:\s*"([^"]*)",?\s*$', body, re.M)}


def ts_default_settings():
    body = re.search(r"export const DEFAULT_SETTINGS: Settings = \{(.*?)\n\};", TYPES_TS, re.S).group(1)
    paddles_body = re.search(r"paddles:\s*\{([^}]*)\}", body).group(1)
    defaults = {"paddles": dict(re.findall(r'(\w+):\s*"([^"]*)"', paddles_body))}
    for key, raw_value in re.findall(r'^\s*(\w+):\s*("[^"]*"|\d+|true|false),', body, re.M):
        if raw_value.startswith('"'):
            defaults[key] = raw_value.strip('"')
        elif raw_value in ("true", "false"):
            defaults[key] = raw_value == "true"
        else:
            defaults[key] = int(raw_value)
    return defaults


def api_callables():
    """api.ts ``callable<[a: A, b: B], R>("name")`` -> {name: [a, b]}."""
    found = {}
    for params, name in re.findall(r'callable<\[([^\]]*)\],[^(]*\(\s*"(\w+)"', API_TS, re.S):
        found[name] = [part.split(":")[0].strip() for part in params.split(",") if part.strip()]
    return found


def plugin_callables():
    return {name: [parameter for parameter in inspect.signature(member).parameters if parameter != "self"]
            for name, member in inspect.getmembers(main.Plugin, inspect.iscoroutinefunction)
            if not name.startswith("_")}


def daemon_session_states():
    return {name for name, value in vars(session).items()
            if isinstance(value, str) and value == name and name.isupper()}


def daemon_event_names():
    return set(re.findall(r'self\.emit\("(\w+)"', inspect.getsource(daemon_log.JsonEventSink)))


class BackendHarness(unittest.TestCase):
    """A ``_Backend`` on temp dirs that never spawns the daemon."""

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="contract_")
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        for attribute, sub in (("DECKY_PLUGIN_DIR", "plugin"), ("DECKY_PLUGIN_SETTINGS_DIR", "settings"),
                               ("DECKY_PLUGIN_RUNTIME_DIR", "runtime"), ("DECKY_PLUGIN_LOG_DIR", "logs")):
            patcher = mock.patch.object(main.decky, attribute, os.path.join(self.root, sub))
            patcher.start()
            self.addCleanup(patcher.stop)
        self.emitted = []

        async def record_emit(event, payload):
            self.emitted.append((event, payload))

        emit_patcher = mock.patch.object(main.decky, "emit", record_emit)
        emit_patcher.start()
        self.addCleanup(emit_patcher.stop)
        self.backend = main._Backend()

    def run_event(self, event):
        async def no_status():
            pass

        with mock.patch.object(self.backend, "emit_status", no_status):
            asyncio.run(self.backend._on_daemon_event(event))

    def fallback_status(self):
        empty_sysfs = os.path.join(self.root, "empty-sys")
        os.makedirs(empty_sysfs, exist_ok=True)

        async def no_cli(force=False):
            return None, "unavailable"

        real_snapshot = main._sysfs_snapshot
        with mock.patch.object(main, "_sysfs_snapshot", lambda: real_snapshot(empty_sysfs)), \
                mock.patch.object(self.backend, "_cli_status", no_cli):
            return asyncio.run(self.backend.build_status())


class AllowedValuesTest(unittest.TestCase):
    """Order matters: the dropdowns are built from these lists."""

    def test_profiles_agree_on_all_three_sides(self):
        self.assertEqual(list(config.PROFILES), list(main.PROFILES))
        self.assertEqual(list(main.PROFILES), ts_union("Profile"))
        for record in ("PROFILE_LABELS", "PROFILE_NOUNS", "PROFILE_DESCRIPTIONS"):
            self.assertEqual(list(ts_record(record)), list(main.PROFILES), record)

    def test_transports_agree_and_active_transport_excludes_auto(self):
        self.assertEqual(list(config.TRANSPORTS), list(main.TRANSPORTS))
        self.assertEqual(list(main.TRANSPORTS), ts_union("TransportSetting"))
        self.assertEqual(ts_union("ActiveTransport"), [transport for transport in main.TRANSPORTS if transport != "auto"])

    def test_kill_combos_agree(self):
        self.assertEqual(list(config.KILL_COMBOS), list(main.KILL_COMBOS))
        self.assertEqual(list(main.KILL_COMBOS), ts_union("KillCombo"))
        self.assertEqual(list(ts_record("KILL_COMBO_LABELS")), list(main.KILL_COMBOS))

    def test_paddles_and_actions_agree(self):
        self.assertEqual(list(config.PADDLE_NAMES), list(main.PADDLES))
        self.assertEqual(list(main.PADDLES), ts_union("Paddle"))
        ts_paddles = re.search(r"export const PADDLES[^=]*=\s*\[([^\]]*)\]", TYPES_TS).group(1)
        self.assertEqual(re.findall(r'"(\w+)"', ts_paddles), list(main.PADDLES))
        self.assertEqual(list(config.PADDLE_TARGETS), list(main.PADDLE_ACTIONS))
        self.assertEqual(list(main.PADDLE_ACTIONS), ts_union("PaddleAction"))
        self.assertEqual(list(ts_record("PADDLE_ACTION_LABELS")), list(main.PADDLE_ACTIONS))

    def test_backend_clamp_ranges_are_inside_daemon_limits(self):
        low, high = main.KILL_HOLD_MS_RANGE
        config.RunConfig(kill_hold_ms=low)
        config.RunConfig(kill_hold_ms=high)
        low, high = main.TOUCH_WAKE_RANGE
        config.RunConfig(touch_wake_seconds=low)
        config.RunConfig(touch_wake_seconds=high)

    def test_default_settings_agree(self):
        self.assertEqual(ts_default_settings(), main.DEFAULT_SETTINGS)
        self.assertEqual(set(main.DEFAULT_SETTINGS), set(ts_interface_fields("Settings")))
        daemon_defaults = config.RunConfig()
        for key in ("profile", "transport", "kill_combo", "kill_hold_ms", "touch_wake_seconds", "paddles"):
            self.assertEqual(getattr(daemon_defaults, key), main.DEFAULT_SETTINGS[key], key)
        self.assertFalse(daemon_defaults.screen_off)   # a CLI flag: off unless the backend passes --screen-off
        self.assertTrue(main.DEFAULT_SETTINGS["screen_off"])


class SessionStateTest(BackendHarness):
    def test_session_states_agree(self):
        self.assertEqual(list(main.SESSION_STATES), ts_union("SessionState"))
        self.assertEqual(list(ts_record("SESSION_STATE_LABELS")), list(main.SESSION_STATES))
        self.assertEqual(set(ts_record("SESSION_STATE_DESCRIPTIONS")), set(main.SESSION_STATES))

    def test_daemon_emits_backend_states_plus_stopped(self):
        self.assertEqual(daemon_session_states(), set(main.SESSION_STATES) | {"STOPPED"})
        self.run_event({"ev": "state", "state": "STOPPED", "detail": ""})
        self.assertEqual(self.backend.session_state, "STOPPING")


class CableKindTest(unittest.TestCase):
    def test_cable_kinds_agree_and_the_panel_handles_each(self):
        self.assertEqual(list(CABLE_KINDS), ts_union("CableKind"))
        cable_row = re.search(r"function cableRow\(.*?\n\}", CONTENT_TSX, re.S).group(0)
        self.assertEqual(set(re.findall(r'case "(\w+)":', cable_row)), set(CABLE_KINDS))


class UiTextBudgetTest(unittest.TestCase):
    def test_option_labels_fit_the_budget(self):
        for record in ("PROFILE_LABELS", "KILL_COMBO_LABELS", "PADDLE_ACTION_LABELS", "SESSION_STATE_LABELS"):
            for key, label in ts_record(record).items():
                self.assertLessEqual(len(label), UI_TEXT_BUDGET, f"{record}.{key} = {label!r}")

    def test_status_row_values_fit_the_budget_and_never_mention_volts(self):
        values = re.findall(r'value:\s*"([^"]*)"', CONTENT_TSX)
        self.assertTrue(values)
        for value in values:
            self.assertLessEqual(len(value), UI_TEXT_BUDGET, value)
        ui_strings = re.findall(r'(?:value|description|label):\s*"([^"]*)"', CONTENT_TSX)
        for record in ("PROFILE_LABELS", "PROFILE_NOUNS", "PROFILE_DESCRIPTIONS", "SESSION_STATE_DESCRIPTIONS"):
            ui_strings.extend(ts_record(record).values())
        for text in ui_strings:
            self.assertNotRegex(text, r"\b(mV|mA|volts?|amps?|PD contract)\b")


class CallablesAndEventsTest(BackendHarness):
    def test_frontend_callables_match_plugin_methods(self):
        self.assertEqual(api_callables(), plugin_callables())

    def test_frontend_subscribes_to_exactly_the_backend_events(self):
        subscribed = set(re.findall(r'addEventListener<[^>]*>\(\s*"(\w+)"', INDEX_TSX))
        emitted = set(re.findall(r'_emit\("(\w+)"', inspect.getsource(main)))
        self.assertEqual(subscribed, emitted)
        self.assertEqual(subscribed, {"status", "toast"})

    def test_toast_payload_matches_frontend_type(self):
        asyncio.run(self.backend._toast("title", "body", "warn"))
        self.assertEqual(self.emitted, [("toast", {"title": "title", "body": "body", "severity": "warn"})])
        self.assertEqual(set(self.emitted[0][1]), set(ts_interface_fields("ToastEvent")))
        severities = re.search(r"severity:\s*([^;]+);", TYPES_TS).group(1)
        self.assertIn('"warn"', severities)
        self.assertIn('"info"', severities)
        self.assertIn('"error"', severities)

    def test_every_daemon_event_is_handled_by_the_backend(self):
        self.assertEqual(daemon_event_names(), {"state", "error", "metrics", "kill", "screen"})
        sample = {"state": {"state": "ACTIVE"}, "error": {"msg": "x"}, "metrics": {"hz": 1, "reports": 2, "dropped": 0},
                  "kill": {"reason": "combo"}, "screen": {"off": True, "method": "gamescope"}}
        with mock.patch.object(main.decky, "logger") as logger:
            for name in daemon_event_names():
                self.run_event({"ev": name, **sample[name]})
        unhandled = [call for call in logger.debug.call_args_list if "unhandled" in str(call)]
        self.assertEqual(unhandled, [])


class StatusShapeTest(BackendHarness):
    def test_status_fields_match_the_frontend_type_exactly(self):
        status = self.fallback_status()
        self.assertTrue(status["ok"])
        self.assertEqual(status["session_state"], "IDLE")
        self.assertEqual(set(status), set(ts_interface_fields("Status")))
        ts_metrics = set(re.findall(r"\b(\w+): number", re.search(r"metrics: \{([^}]*)\}", TYPES_TS).group(1)))
        self.assertEqual(ts_metrics, set(status["metrics"]))

    def test_daemon_status_json_carries_every_key_the_backend_reads(self):
        fs = FakeSysfs(os.path.join(self.root, "daemon-sys"))
        daemon_status = collect_status(fs.sys, fs.dev, use_modprobe=False)
        self.assertLessEqual(set(main._CLI_KEY_ALIASES), set(daemon_status))
        normalized = main._normalize_cli_status(daemon_status)
        self.assertLessEqual(set(normalized), set(ts_interface_fields("Status")))


if __name__ == "__main__":
    unittest.main()
