"""``main.py`` — the Decky glue: every callable answers a dict and never raises; the service is wired from
the (stubbed) ``decky`` module."""
import json
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

import _path  # noqa: F401

import decky_stub
from test_backend_daemon import FakeCliRunner
from test_backend_status import deck_sysfs

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)


class PluginTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.mkdtemp(prefix="backend_plugin_")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.decky = decky_stub.install(self.tmp)
        import main
        self.main = main
        fs = deck_sysfs(os.path.join(self.tmp, "deck"))
        main._SERVICE = None
        service = main._service()
        service.sysfs_root, service.dev_root = fs.sys, fs.dev
        service.cli = FakeCliRunner({
            "status": (0, json.dumps({"ok": True}), ""),
            "recover": (0, json.dumps({"ok": True, "errors": []}), ""),
        })
        self.service = service
        self.plugin = main.Plugin()
        self.addCleanup(setattr, main, "_SERVICE", None)

    async def test_service_is_wired_from_decky(self):
        self.assertEqual(self.service.settings_dir, os.path.join(self.tmp, "settings"))
        self.assertEqual(self.service.paths.py_modules_dir, os.path.join(REPO_ROOT, "py_modules"))
        self.assertEqual(self.service.plugin_version, self.main._plugin_version())
        self.assertRegex(self.service.plugin_version, r"^\d+\.\d+\.\d+")   # from package.json

    async def test_get_and_set_settings(self):
        from controller_backend.settings import DEFAULT_SETTINGS
        self.assertEqual(await self.plugin.get_settings(), {"ok": True, **DEFAULT_SETTINGS})
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
        self.assertFalse(self.service.daemon_alive())
        with mock.patch.object(self.service, "build_status", side_effect=RuntimeError("boom")):
            self.assertEqual(await self.plugin.get_status(), {"ok": False, "error": "boom"})

    async def test_get_status_stop_and_diagnostics(self):
        status = await self.plugin.get_status()
        self.assertTrue(status["ok"])
        self.assertEqual(status["session_state"], "IDLE")
        stopped = await self.plugin.stop()
        self.assertTrue(stopped["ok"])
        self.assertEqual(self.decky.emitted[-1][0], "status")
        diagnostics = await self.plugin.get_diagnostics()
        self.assertTrue(diagnostics["ok"])
        self.assertEqual(diagnostics["daemon"]["running"], False)

    async def test_lifecycle_hooks_never_raise(self):
        with mock.patch.object(self.service, "startup", side_effect=RuntimeError("no")):
            await self.plugin._main()
        with mock.patch.object(self.service, "shutdown", side_effect=RuntimeError("no")):
            await self.plugin._unload()
            await self.plugin._uninstall()
        await self.plugin._migration()


if __name__ == "__main__":
    unittest.main()
