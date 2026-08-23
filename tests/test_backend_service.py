"""``controller_backend.service.Service`` on temp dirs, a fake Deck sysfs and a fake daemon CLI — no process
is ever spawned; emitted events are collected."""
import json
import os
import shutil
import tempfile
import types
import unittest
from unittest import mock

import _path  # noqa: F401

from controller_backend.service import Service
from controller_backend.settings import DEFAULT_SETTINGS
from test_backend_daemon import FakeCliRunner
from test_backend_status import deck_sysfs


class ServiceTestCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.mkdtemp(prefix="backend_service_")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.fs = deck_sysfs(os.path.join(self.tmp, "deck"))
        self.emitted = []
        self.cli = FakeCliRunner({
            "status": (0, json.dumps({"ok": True, "drd_enabled": True, "neptune_captured": False}), ""),
            "recover": (0, json.dumps({"ok": True, "errors": []}), ""),
        })

        async def record_emit(event, payload):
            self.emitted.append((event, payload))

        self.service = Service(emit=record_emit, plugin_dir=os.path.join(self.tmp, "plugin"),
                               settings_dir=os.path.join(self.tmp, "settings"),
                               runtime_dir=os.path.join(self.tmp, "runtime"), log_dir=os.path.join(self.tmp, "logs"),
                               plugin_version="0.1.0", decky_version="test",
                               sysfs_root=self.fs.sys, dev_root=self.fs.dev, cli_runner=self.cli)

    def events(self, name):
        return [payload for event, payload in self.emitted if event == name]

    def fake_process(self, pid=4242, returncode=None):
        self.service.supervisor.process = types.SimpleNamespace(pid=pid, returncode=returncode)


class EventMappingTest(ServiceTestCase):
    async def test_state_events_drive_the_status_while_running(self):
        self.fake_process()
        await self.service._on_daemon_event({"ev": "state", "state": "CAPTURING", "detail": "lizard off"})
        await self.service._on_daemon_event({"ev": "state", "state": "ACTIVE", "detail": ""})
        status = self.events("status")[-1]
        self.assertEqual((status["session_state"], status["daemon_running"], status["daemon_pid"]), ("ACTIVE", True, 4242))
        self.assertTrue(status["neptune_captured"])
        self.assertEqual(self.events("status")[0]["session_detail"], "lizard off")

    async def test_stopped_shows_as_stopping_until_the_process_is_gone(self):
        self.fake_process()
        await self.service._on_daemon_event({"ev": "state", "state": "STOPPED"})
        self.assertEqual(self.events("status")[-1]["session_state"], "STOPPING")
        self.service.supervisor.process = None
        self.assertEqual((await self.service.build_status())["session_state"], "IDLE")

    async def test_error_event_reaches_the_status(self):
        self.fake_process()
        await self.service._on_daemon_event({"ev": "error", "msg": "no UDC"})
        self.assertEqual(self.events("status")[-1]["last_error"], "no UDC")

    async def test_metrics_do_not_emit(self):
        await self.service._on_daemon_event({"ev": "metrics", "hz": 250, "reports": 10, "dropped": 0})
        self.assertEqual(self.emitted, [])

    async def test_kill_toasts_and_signal_respects_stop_requested(self):
        await self.service._on_daemon_event({"ev": "kill", "reason": "combo"})
        self.service.supervisor.stop_requested = True
        await self.service._on_daemon_event({"ev": "kill", "reason": "signal"})
        toasts = self.events("toast")
        self.assertEqual(len(toasts), 1)
        self.assertEqual((toasts[0]["severity"], set(toasts[0])), ("info", {"title", "body", "severity"}))

    async def test_screen_event_is_authoritative(self):
        self.fake_process()
        await self.service._on_daemon_event({"ev": "state", "state": "ACTIVE"})
        self.assertTrue(self.events("status")[-1]["screen_off"])
        await self.service._on_daemon_event({"ev": "screen", "off": False, "method": "none"})
        self.assertFalse(self.events("status")[-1]["screen_off"])


class StatusTest(ServiceTestCase):
    async def test_idle_status_combines_sysfs_and_cli(self):
        status = await self.service.build_status()
        self.assertTrue(status["ok"])
        self.assertEqual((status["session_state"], status["daemon_running"], status["daemon_pid"]), ("IDLE", False, None))
        self.assertEqual(status["cable_kind"], "pc")
        self.assertTrue(status["drd_enabled"])
        self.assertIsNone(status["status_error"])
        self.assertFalse(status["screen_off"])

    async def test_cli_failure_falls_back_to_sysfs_and_reports_status_error(self):
        self.cli.replies["status"] = (1, "Traceback…", "boom")
        with self.assertLogs("controller_backend.service", level="WARNING"):
            status = await self.service.build_status(force=True)
        self.assertIn("printed no JSON object", status["status_error"])
        self.assertIn("boom", status["status_error"])
        self.assertEqual(status["cable_kind"], "pc")

    async def test_cli_status_is_cached_within_ttl(self):
        await self.service.build_status()
        await self.service.build_status()
        self.assertEqual([call[0] for call in self.cli.calls], ["status"])
        await self.service.build_status(force=True)
        self.assertEqual([call[0] for call in self.cli.calls], ["status", "status"])


class RecoverTest(ServiceTestCase):
    async def test_stop_without_daemon_still_recovers_and_emits(self):
        status = await self.service.stop("user")
        self.assertEqual(self.cli.calls[0][0], "recover")
        self.assertTrue(self.service.last_recover["ok"])
        self.assertEqual(status["session_state"], "IDLE")
        self.assertEqual(len(self.events("status")), 1)
        self.assertEqual(self.events("toast"), [])

    async def test_recover_errors_surface_as_toast_and_last_error(self):
        self.cli.replies["recover"] = (0, json.dumps({"ok": False, "errors": ["neptune: still detached"]}), "")
        with self.assertLogs("controller_backend.service", level="ERROR"):
            self.assertFalse(await self.service._recover("test"))
        self.assertIn("still detached", self.service.session.last_error)
        toast = self.events("toast")[-1]
        self.assertEqual(toast["severity"], "error")
        self.assertIn("reboot", toast["body"])

    async def test_unrequested_daemon_exit_recovers_and_toasts_on_failure(self):
        await self.service._on_daemon_exit(1, requested=False)
        self.assertEqual(self.cli.calls[0][0], "recover")
        self.assertEqual(self.events("toast")[0]["severity"], "error")
        self.assertEqual(self.events("status")[-1]["session_state"], "IDLE")
        self.cli.calls.clear()
        await self.service._on_daemon_exit(0, requested=True)
        self.assertEqual(self.cli.calls, [])   # stop() owns that rollback


class StartTest(ServiceTestCase):
    async def test_unknown_profile_is_rejected_before_spawning(self):
        with self.assertRaises(ValueError):
            await self.service.start("ds4")
        self.assertFalse(self.service.daemon_alive())

    async def test_missing_py_modules_dir_is_reported(self):
        with self.assertRaises(FileNotFoundError):
            await self.service.start(None)

    async def test_start_spawns_with_settings_and_reports_the_running_session(self):
        os.makedirs(self.service.paths.py_modules_dir)
        self.service.settings.update({"profile": "hid_gamepad", "transport": "auto"})
        spawned = []

        async def fake_spawn(args):
            spawned.append(args)
            self.fake_process(pid=77)
            self.service.supervisor.first_event.set()

        with mock.patch.object(self.service.supervisor, "spawn", fake_spawn):
            status = await self.service.start(None)
        self.assertIn("--profile", spawned[0])
        self.assertEqual(spawned[0][spawned[0].index("--profile") + 1], "hid_gamepad")
        self.assertEqual((status["daemon_running"], status["daemon_pid"], status["active_profile"], status["transport"]),
                         (True, 77, "hid_gamepad", "hid"))
        self.assertEqual(self.events("status")[-1]["daemon_pid"], 77)

    async def test_start_while_running_is_a_noop(self):
        os.makedirs(self.service.paths.py_modules_dir)
        self.fake_process()
        with mock.patch.object(self.service.supervisor, "spawn") as spawn:
            status = await self.service.start("xbox360")
        spawn.assert_not_called()
        self.assertTrue(status["daemon_running"])


class DiagnosticsTest(ServiceTestCase):
    async def test_shape(self):
        diagnostics = await self.service.diagnostics()
        self.assertTrue(diagnostics["ok"])
        self.assertEqual(diagnostics["status"]["cable_kind"], "pc")
        self.assertEqual(diagnostics["daemon"]["running"], False)
        self.assertEqual(diagnostics["settings"], DEFAULT_SETTINGS)
        self.assertEqual(diagnostics["decky_version"], "test")
        self.assertEqual(set(diagnostics["paths"]), {"plugin_dir", "py_modules_dir", "settings", "runtime_dir",
                                                     "log_dir", "daemon_log"})
        self.assertEqual(diagnostics["daemon_log_tail"], [])


if __name__ == "__main__":
    unittest.main()
