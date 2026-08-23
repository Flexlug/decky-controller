import os
import shutil
import tempfile
import unittest

import _path  # noqa: F401

from deckgadget.platform.display.compositor import (COMMAND_TIMEOUT_S, GamescopeSleep, KscreenDpms,
                                                    find_gamescope_socket, run_command)
from fakes import FakeRunner, make_socket, write


class GamescopeSleepTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.run_user = os.path.join(self.tmp, "run", "user")
        self.sockets = []

    def tearDown(self):
        for s in self.sockets:
            s.close()
        shutil.rmtree(self.tmp)

    def sock(self, uid, name="gamescope-0"):
        path = os.path.join(self.run_user, str(uid), name)
        self.sockets.append(make_socket(path))
        return path

    def test_discovery_prefers_deck_uid_and_lowest_display(self):
        self.assertIsNone(find_gamescope_socket(self.run_user))
        os.makedirs(os.path.join(self.run_user, "1000"))
        self.assertIsNone(find_gamescope_socket(self.run_user))          # dir, no socket
        write(os.path.join(self.run_user, "1000", "gamescope-9"), "")       # plain file: ignored
        self.assertIsNone(find_gamescope_socket(self.run_user))
        self.sock(1001, "gamescope-0")
        self.assertEqual(find_gamescope_socket(self.run_user), (os.path.join(self.run_user, "1001"), "gamescope-0"))
        self.sock(1000, "gamescope-1")
        self.sock(1000, "gamescope-0")
        self.assertEqual(find_gamescope_socket(self.run_user), (os.path.join(self.run_user, "1000"), "gamescope-0"))
        # explicit runtime dir restricts the scan
        self.assertEqual(find_gamescope_socket(self.run_user, runtime_dir=os.path.join(self.run_user, "1001")),
                         (os.path.join(self.run_user, "1001"), "gamescope-0"))
        self.assertIsNone(find_gamescope_socket(self.run_user, runtime_dir=os.path.join(self.run_user, "1002")))

    def test_unavailable_without_socket_never_runs_anything(self):
        runner = FakeRunner()
        gs = GamescopeSleep(run_user_base=self.run_user, binary="/usr/bin/gamescopectl", runner=runner)
        self.assertFalse(gs.available())
        self.assertFalse(gs.sleep())
        self.assertFalse(gs.wake())
        self.assertEqual(runner.calls, [])
        self.assertIsNone(gs.socket_path)
        self.assertEqual(gs.info()["available"], False)

    def test_sleep_wake_argv_env(self):
        path = self.sock(1000)
        runner = FakeRunner(rc=0)
        gs = GamescopeSleep(run_user_base=self.run_user, binary="/usr/bin/gamescopectl", runner=runner)
        self.assertTrue(gs.available())
        self.assertEqual(gs.socket_path, path)
        self.assertTrue(gs.sleep())
        self.assertTrue(gs.wake())
        self.assertEqual(runner.argvs, [["/usr/bin/gamescopectl", "drm_sleep_internal_screen", "1"],
                                        ["/usr/bin/gamescopectl", "drm_sleep_internal_screen", "0"]])
        for call in runner.calls:
            self.assertEqual(call["env"]["XDG_RUNTIME_DIR"], os.path.join(self.run_user, "1000"))
            self.assertEqual(call["env"]["GAMESCOPE_WAYLAND_DISPLAY"], "gamescope-0")
            self.assertIn("PATH", call["env"])
            self.assertEqual(call["timeout"], COMMAND_TIMEOUT_S)
            self.assertIsNone(call["user"])          # gamescopectl runs as root (socket is world-accessible)

    def test_failure_modes_return_false(self):
        self.sock(1000)
        runner = FakeRunner(rc=1, stdout="Failed to open GAMESCOPE_WAYLAND_DISPLAY.\n")
        gs = GamescopeSleep(run_user_base=self.run_user, binary="/usr/bin/gamescopectl", runner=runner)
        self.assertFalse(gs.sleep())
        runner.rc, runner.error = None, "timeout after 3.0s"
        self.assertFalse(gs.wake())
        runner.rc, runner.error, runner.stdout = 0, None, "Command not found.\n"   # unknown ConVar, rc 0
        self.assertFalse(gs.sleep())
        runner.stdout = ""
        self.assertTrue(gs.sleep())
        runner.raise_exc = RuntimeError("boom")
        self.assertFalse(gs.sleep())                 # never raises
        # explicit binary path that does not exist -> the runner reports it, result False, no raise
        runner2 = FakeRunner(error="FileNotFoundError: /nope/gamescopectl")
        runner2.rc = None
        gs2 = GamescopeSleep(run_user_base=self.run_user, binary="/nope/gamescopectl", runner=runner2)
        self.assertTrue(gs2.available())             # an injected path is trusted until it is run
        self.assertFalse(gs2.sleep())
        self.assertEqual(runner2.argvs, [["/nope/gamescopectl", "drm_sleep_internal_screen", "1"]])

    def test_explicit_runtime_dir_and_display(self):
        runner = FakeRunner()
        rd = os.path.join(self.run_user, "1000")
        gs = GamescopeSleep(runtime_dir=rd, display="gamescope-0", binary="/x/gamescopectl", runner=runner)
        self.assertFalse(gs.available())
        self.sock(1000)
        self.assertTrue(gs.available())
        self.assertTrue(gs.sleep())
        self.assertEqual(runner.calls[0]["argv"][0], "/x/gamescopectl")
        self.assertEqual(runner.calls[0]["env"]["XDG_RUNTIME_DIR"], rd)



class KscreenDpmsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.rd = os.path.join(self.tmp, "run", "user", "1000")
        self.sockets = []

    def tearDown(self):
        for s in self.sockets:
            s.close()
        shutil.rmtree(self.tmp)

    def test_available_and_commands(self):
        runner = FakeRunner()
        ks = KscreenDpms(runtime_dir=self.rd, binary="/usr/bin/kscreen-doctor", runner=runner)
        self.assertFalse(ks.available())
        self.assertFalse(ks.sleep())
        self.assertEqual(runner.calls, [])
        self.sockets.append(make_socket(os.path.join(self.rd, "wayland-0")))
        self.assertTrue(ks.available())
        self.assertTrue(ks.sleep())
        self.assertTrue(ks.wake())
        self.assertEqual(runner.argvs, [["/usr/bin/kscreen-doctor", "--dpms", "off"],
                                        ["/usr/bin/kscreen-doctor", "--dpms", "on"]])
        for call in runner.calls:
            env = call["env"]
            self.assertEqual(env["XDG_RUNTIME_DIR"], self.rd)
            self.assertEqual(env["WAYLAND_DISPLAY"], "wayland-0")
            self.assertEqual(env["DBUS_SESSION_BUS_ADDRESS"], "unix:path=" + os.path.join(self.rd, "bus"))
            self.assertEqual(call["user"], (1000, 1000))
            self.assertEqual(call["timeout"], COMMAND_TIMEOUT_S)
        runner.rc = 2
        self.assertFalse(ks.wake())
        runner.raise_exc = OSError("nope")
        self.assertFalse(ks.sleep())



class RunCommandTest(unittest.TestCase):
    def test_real_runner(self):
        res = run_command(["/bin/sh", "-c", "echo out; echo err >&2; exit 3"], {"PATH": "/usr/bin:/bin"}, 3.0)
        self.assertEqual((res.returncode, res.stdout.strip(), res.stderr.strip()), (3, "out", "err"))
        self.assertFalse(res.ok)
        self.assertIn("out", res.tail())
        ok = run_command(["/bin/true"], {}, 3.0)
        self.assertTrue(ok.ok)
        missing = run_command(["/nonexistent/binary"], {}, 3.0)
        self.assertIsNone(missing.returncode)
        self.assertIn("FileNotFoundError", missing.error)
        slow = run_command(["/bin/sh", "-c", "sleep 5"], {}, 0.2)
        self.assertIsNone(slow.returncode)
        self.assertIn("timeout", slow.error)


if __name__ == "__main__":
    unittest.main()
