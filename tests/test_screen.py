import os
import shutil
import struct
import tempfile
import threading
import time
import unittest

import _path  # noqa: F401

from deckgadget.platform import screen as SC
from deckgadget.util.log import NullEventSink
from fakes import make_socket, read, write


class FakeRunner:
    """Injected command runner: records (argv, env, timeout, user) and returns canned results."""

    def __init__(self, rc=0, stdout="", stderr="", error=None):
        self.calls = []
        self.rc = rc
        self.stdout = stdout
        self.stderr = stderr
        self.error = error
        self.raise_exc = None

    def __call__(self, argv, env, timeout, user=None):
        self.calls.append({"argv": list(argv), "env": dict(env), "timeout": timeout, "user": user})
        if self.raise_exc is not None:
            raise self.raise_exc
        return SC.CommandResult(self.rc, self.stdout, self.stderr, self.error)

    @property
    def argvs(self):
        return [c["argv"] for c in self.calls]


class FakeMethod(SC.ScreenMethod):
    """Scriptable strategy for controller / guard tests."""

    def __init__(self, name, available=True, sleep_ok=True, wake_ok=True):
        self.name = name
        self._available = available
        self.sleep_ok = sleep_ok
        self.wake_ok = wake_ok
        self.calls = []

    def available(self):
        self.calls.append("available")
        return self._available

    def sleep(self):
        self.calls.append("sleep")
        return self.sleep_ok

    def wake(self):
        self.calls.append("wake")
        return self.wake_ok

    def release(self):
        self.calls.append("release")
        return self.wake_ok



class BacklightTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.bl = os.path.join(self.tmp, "amdgpu_bl0")
        write(os.path.join(self.bl, "brightness"), "200\n")
        write(os.path.join(self.bl, "max_brightness"), "255\n")
        self.state = os.path.join(self.tmp, "run", "brightness")

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def test_save_off_restore(self):
        b = SC.Backlight(self.bl, self.state)
        self.assertTrue(b.available)
        self.assertEqual(b.brightness(), 200)
        b.save_and_off()
        self.assertEqual(read(os.path.join(self.bl, "brightness")), "0")
        self.assertEqual(read(self.state), "200")
        self.assertEqual(b.restore(forget=False), 200)
        self.assertEqual(read(os.path.join(self.bl, "brightness")), "200")
        self.assertTrue(os.path.exists(self.state))
        b.off()
        self.assertEqual(b.restore(forget=True), 200)
        self.assertFalse(os.path.exists(self.state))
        # nothing saved + screen on -> no-op
        self.assertIsNone(SC.Backlight(self.bl, self.state).restore())

    def test_crash_recovery_keeps_original_value(self):
        b = SC.Backlight(self.bl, self.state)
        b.save_and_off()
        # new process after crash: brightness is 0, state file says 200
        b2 = SC.Backlight(self.bl, self.state)
        b2.save_and_off()                      # must not overwrite the saved 200 with 0
        self.assertEqual(read(self.state), "200")
        self.assertEqual(b2.restore(), 200)

    def test_unavailable_backlight(self):
        b = SC.Backlight(os.path.join(self.tmp, "missing"), self.state)
        self.assertFalse(b.available)
        b.save_and_off()   # no exception
        self.assertIsNone(b.restore())

    def test_backlight_dim_strategy(self):
        m = SC.BacklightDim(SC.Backlight(self.bl, self.state))
        self.assertEqual(m.name, "backlight")
        self.assertTrue(m.available())
        self.assertTrue(m.sleep())
        self.assertEqual(read(os.path.join(self.bl, "brightness")), "0")
        self.assertEqual(read(self.state), "200")
        self.assertTrue(m.wake())                    # temporary: state file kept
        self.assertEqual(read(os.path.join(self.bl, "brightness")), "200")
        self.assertTrue(os.path.exists(self.state))
        self.assertTrue(m.sleep())                   # re-sleep does not re-save
        self.assertEqual(read(os.path.join(self.bl, "brightness")), "0")
        self.assertEqual(read(self.state), "200")
        self.assertTrue(m.release())                 # permanent: state file gone
        self.assertEqual(read(os.path.join(self.bl, "brightness")), "200")
        self.assertFalse(os.path.exists(self.state))
        missing = SC.BacklightDim(SC.Backlight(os.path.join(self.tmp, "missing"), self.state))
        self.assertFalse(missing.available())
        self.assertFalse(missing.sleep())



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
        self.assertIsNone(SC.find_gamescope_socket(self.run_user))
        os.makedirs(os.path.join(self.run_user, "1000"))
        self.assertIsNone(SC.find_gamescope_socket(self.run_user))          # dir, no socket
        write(os.path.join(self.run_user, "1000", "gamescope-9"), "")       # plain file: ignored
        self.assertIsNone(SC.find_gamescope_socket(self.run_user))
        self.sock(1001, "gamescope-0")
        self.assertEqual(SC.find_gamescope_socket(self.run_user), (os.path.join(self.run_user, "1001"), "gamescope-0"))
        self.sock(1000, "gamescope-1")
        self.sock(1000, "gamescope-0")
        self.assertEqual(SC.find_gamescope_socket(self.run_user), (os.path.join(self.run_user, "1000"), "gamescope-0"))
        # explicit runtime dir restricts the scan
        self.assertEqual(SC.find_gamescope_socket(self.run_user, runtime_dir=os.path.join(self.run_user, "1001")),
                         (os.path.join(self.run_user, "1001"), "gamescope-0"))
        self.assertIsNone(SC.find_gamescope_socket(self.run_user, runtime_dir=os.path.join(self.run_user, "1002")))

    def test_unavailable_without_socket_never_runs_anything(self):
        runner = FakeRunner()
        gs = SC.GamescopeSleep(run_user_base=self.run_user, binary="/usr/bin/gamescopectl", runner=runner)
        self.assertFalse(gs.available())
        self.assertFalse(gs.sleep())
        self.assertFalse(gs.wake())
        self.assertEqual(runner.calls, [])
        self.assertIsNone(gs.socket_path)
        self.assertEqual(gs.info()["available"], False)

    def test_sleep_wake_argv_env(self):
        path = self.sock(1000)
        runner = FakeRunner(rc=0)
        gs = SC.GamescopeSleep(run_user_base=self.run_user, binary="/usr/bin/gamescopectl", runner=runner)
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
            self.assertEqual(call["timeout"], SC.COMMAND_TIMEOUT_S)
            self.assertIsNone(call["user"])          # gamescopectl runs as root (socket is world-accessible)

    def test_failure_modes_return_false(self):
        self.sock(1000)
        runner = FakeRunner(rc=1, stdout="Failed to open GAMESCOPE_WAYLAND_DISPLAY.\n")
        gs = SC.GamescopeSleep(run_user_base=self.run_user, binary="/usr/bin/gamescopectl", runner=runner)
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
        gs2 = SC.GamescopeSleep(run_user_base=self.run_user, binary="/nope/gamescopectl", runner=runner2)
        self.assertTrue(gs2.available())             # an injected path is trusted until it is run
        self.assertFalse(gs2.sleep())
        self.assertEqual(runner2.argvs, [["/nope/gamescopectl", "drm_sleep_internal_screen", "1"]])

    def test_explicit_runtime_dir_and_display(self):
        runner = FakeRunner()
        rd = os.path.join(self.run_user, "1000")
        gs = SC.GamescopeSleep(runtime_dir=rd, display="gamescope-0", binary="/x/gamescopectl", runner=runner)
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
        ks = SC.KscreenDpms(runtime_dir=self.rd, binary="/usr/bin/kscreen-doctor", runner=runner)
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
            self.assertEqual(call["timeout"], SC.COMMAND_TIMEOUT_S)
        runner.rc = 2
        self.assertFalse(ks.wake())
        runner.raise_exc = OSError("nope")
        self.assertFalse(ks.sleep())


class RunCommandTest(unittest.TestCase):
    def test_real_runner(self):
        res = SC.run_command(["/bin/sh", "-c", "echo out; echo err >&2; exit 3"], {"PATH": "/usr/bin:/bin"}, 3.0)
        self.assertEqual((res.returncode, res.stdout.strip(), res.stderr.strip()), (3, "out", "err"))
        self.assertFalse(res.ok)
        self.assertIn("out", res.tail())
        ok = SC.run_command(["/bin/true"], {}, 3.0)
        self.assertTrue(ok.ok)
        missing = SC.run_command(["/nonexistent/binary"], {}, 3.0)
        self.assertIsNone(missing.returncode)
        self.assertIn("FileNotFoundError", missing.error)
        slow = SC.run_command(["/bin/sh", "-c", "sleep 5"], {}, 0.2)
        self.assertIsNone(slow.returncode)
        self.assertIn("timeout", slow.error)



class TouchscreenTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.sys = os.path.join(self.tmp, "sys")
        write(os.path.join(self.sys, "class", "input", "event3", "device", "name"), "Steam Deck Controller\n")
        write(os.path.join(self.sys, "class", "input", "event14", "device", "name"), "FTS3528:00 2808:1015\n")

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def test_find_touchscreen(self):
        self.assertEqual(SC.find_touchscreen(self.sys, "/dev"), "/dev/input/event14")
        self.assertIsNone(SC.find_touchscreen(os.path.join(self.tmp, "nope"), "/dev", name_substr="ZZZ-none"))

    def test_parse_input_events(self):
        self.assertEqual(SC.INPUT_EVENT.size, 24)
        buf = struct.pack("<qqHHi", 1, 2, SC.EV_ABS, SC.ABS_MT_TRACKING_ID, 5) + \
              struct.pack("<qqHHi", 1, 2, SC.EV_KEY, SC.BTN_TOUCH, 1) + \
              struct.pack("<qqHHi", 1, 2, SC.EV_SYN, 0, 0)
        evs = list(SC.parse_input_events(buf))
        self.assertEqual(len(evs), 3)
        self.assertTrue(SC.is_touch_event(*evs[0]))
        self.assertTrue(SC.is_touch_event(*evs[1]))
        self.assertFalse(SC.is_touch_event(*evs[2]))
        self.assertFalse(SC.is_touch_event(SC.EV_KEY, SC.BTN_TOUCH, 0))
        self.assertFalse(SC.is_touch_event(SC.EV_ABS, SC.ABS_MT_TRACKING_ID, -1))

    def test_touch_watcher_reads_pipe(self):
        r, w = os.pipe()
        path = f"/proc/self/fd/{r}"
        hits = []
        done = threading.Event()

        def on_touch():
            hits.append(1)
            done.set()

        watcher = SC.TouchWatcher(path, on_touch, debounce_s=0.0)
        watcher.start()
        try:
            os.write(w, struct.pack("<qqHHi", 0, 0, SC.EV_KEY, SC.BTN_TOUCH, 1))
            self.assertTrue(done.wait(2.0))
        finally:
            watcher.stop()
            os.close(w)
            os.close(r)
        self.assertEqual(hits, [1])



def wait_until(pred, timeout=2.0):
    deadline = time.monotonic() + timeout
    while not pred() and time.monotonic() < deadline:
        time.sleep(0.01)
    return pred()


class ScreenControllerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.bl = os.path.join(self.tmp, "amdgpu_bl0")
        self.bl_file = os.path.join(self.bl, "brightness")
        write(self.bl_file, "150\n")
        write(os.path.join(self.bl, "max_brightness"), "255\n")
        self.state = os.path.join(self.tmp, "run", "brightness")
        self.run_user = os.path.join(self.tmp, "run", "user")
        self.sockets = []
        self.changes = []

    def tearDown(self):
        for s in self.sockets:
            s.close()
        shutil.rmtree(self.tmp)

    def gamescope_socket(self):
        self.sockets.append(make_socket(os.path.join(self.run_user, "1000", "gamescope-0")))

    def controller(self, method="auto", runner=None, kscreen=None, gamescope=None, wake_seconds=0.05):
        runner = runner or FakeRunner()
        gs = gamescope if gamescope is not None else SC.GamescopeSleep(
            run_user_base=self.run_user, binary="/usr/bin/gamescopectl", runner=runner)
        ks = kscreen if kscreen is not None else FakeMethod("kscreen", available=False)
        sc = SC.ScreenController(SC.Backlight(self.bl, self.state), touch_event="", wake_seconds=wake_seconds,
                                 on_change=lambda off, m: self.changes.append((off, m)), method=method,
                                 gamescope=gs, kscreen=ks)
        return sc, runner

    def assert_backlight_untouched(self):
        self.assertEqual(read(self.bl_file), "150\n")       # Backlight writes would drop the newline
        self.assertFalse(os.path.exists(self.state))

    def test_auto_picks_gamescope_and_never_touches_backlight(self):
        self.gamescope_socket()
        sc, runner = self.controller("auto")
        self.assertEqual(sc.method, "none")
        sc.activate()
        self.assertEqual(sc.method, "gamescope")
        self.assertTrue(sc.is_off)
        self.assertEqual(runner.argvs, [["/usr/bin/gamescopectl", "drm_sleep_internal_screen", "1"]])
        self.assertEqual(runner.calls[0]["env"]["XDG_RUNTIME_DIR"], os.path.join(self.run_user, "1000"))
        self.assertEqual(runner.calls[0]["env"]["GAMESCOPE_WAYLAND_DISPLAY"], "gamescope-0")
        self.assert_backlight_untouched()
        # touch -> wake with the same method; wake expires -> sleep again with the same method
        sc._on_touch()
        self.assertFalse(sc.is_off)
        self.assertEqual(runner.argvs[-1], ["/usr/bin/gamescopectl", "drm_sleep_internal_screen", "0"])
        self.assertTrue(wait_until(lambda: sc.is_off))
        self.assertEqual(runner.argvs[-1], ["/usr/bin/gamescopectl", "drm_sleep_internal_screen", "1"])
        self.assertEqual(len(runner.calls), 3)
        self.assert_backlight_untouched()
        # deactivate -> permanent wake
        sc.deactivate()
        self.assertFalse(sc.is_off)
        self.assertEqual(runner.argvs[-1], ["/usr/bin/gamescopectl", "drm_sleep_internal_screen", "0"])
        self.assertEqual(len(runner.calls), 4)
        self.assert_backlight_untouched()
        self.assertEqual(self.changes, [(True, "gamescope"), (False, "gamescope"), (True, "gamescope"),
                                        (False, "gamescope")])
        self.assertEqual(sc.method, "none")
        sc.deactivate()   # idempotent: no further commands
        self.assertEqual(len(runner.calls), 4)

    def test_auto_falls_back_to_backlight_without_gamescope(self):
        sc, runner = self.controller("auto")          # no socket
        sc.activate()
        self.assertEqual(sc.method, "backlight")
        self.assertTrue(sc.is_off)
        self.assertEqual(runner.calls, [])
        self.assertEqual(read(self.bl_file), "0")
        self.assertEqual(read(self.state), "150")
        sc._on_touch()
        self.assertFalse(sc.is_off)
        self.assertEqual(read(self.bl_file), "150")
        self.assertTrue(wait_until(lambda: sc.is_off))
        self.assertEqual(read(self.bl_file), "0")
        sc.deactivate()
        self.assertFalse(sc.is_off)
        self.assertEqual(read(self.bl_file), "150")
        self.assertFalse(os.path.exists(self.state))
        self.assertEqual(self.changes, [(True, "backlight"), (False, "backlight"), (True, "backlight"),
                                        (False, "backlight")])
        self.assertEqual(runner.calls, [])
        sc.deactivate()   # idempotent
        self.assertEqual(read(self.bl_file), "150")

    def test_auto_falls_back_when_gamescope_sleep_fails(self):
        self.gamescope_socket()
        sc, runner = self.controller("auto", runner=FakeRunner(rc=1, stdout="Failed to open GAMESCOPE_WAYLAND_DISPLAY."))
        sc.activate()
        self.assertEqual(sc.method, "backlight")
        self.assertEqual(runner.argvs, [["/usr/bin/gamescopectl", "drm_sleep_internal_screen", "1"]])
        self.assertEqual(read(self.bl_file), "0")
        sc.deactivate()
        self.assertEqual(read(self.bl_file), "150")
        self.assertEqual(len(runner.calls), 1)          # gamescope is not used for the wake

    def test_auto_prefers_kscreen_over_backlight(self):
        ks = FakeMethod("kscreen", available=True)
        sc, runner = self.controller("auto", kscreen=ks)  # no gamescope socket
        sc.activate()
        self.assertEqual(sc.method, "kscreen")
        self.assertEqual(ks.calls, ["available", "sleep"])
        self.assert_backlight_untouched()
        sc._on_touch()
        self.assertEqual(ks.calls[-1], "wake")
        self.assertTrue(wait_until(lambda: sc.is_off))
        self.assertEqual(ks.calls[-1], "sleep")
        sc.deactivate()
        self.assertEqual(ks.calls[-1], "release")
        self.assert_backlight_untouched()
        self.assertEqual(runner.calls, [])

    def test_explicit_gamescope_without_socket_keeps_screen_on(self):
        sc, runner = self.controller("gamescope")
        sc.activate()
        self.assertEqual(sc.method, "none")
        self.assertFalse(sc.is_off)
        self.assertEqual(runner.calls, [])
        self.assert_backlight_untouched()
        sc._on_touch()                                   # harmless
        sc.deactivate()
        self.assert_backlight_untouched()
        # the supervisor is told explicitly that the panel stayed on (Status.screen_off=false)
        self.assertEqual(self.changes, [(False, "none")])

    def test_auto_falls_back_when_gamescope_lacks_the_convar(self):
        """gamescopectl exits 0 for an unknown ConVar ("Command not found.") — must not count as asleep."""
        self.gamescope_socket()
        sc, runner = self.controller("auto", runner=FakeRunner(rc=0, stdout="Command not found.\n"))
        sc.activate()
        self.assertEqual(sc.method, "backlight")
        self.assertEqual(runner.argvs, [["/usr/bin/gamescopectl", "drm_sleep_internal_screen", "1"]])
        self.assertEqual(read(self.bl_file), "0")
        sc.deactivate()
        self.assertEqual(read(self.bl_file), "150")
        self.assertEqual(len(runner.calls), 1)

    def test_explicit_backlight_ignores_gamescope(self):
        self.gamescope_socket()
        sc, runner = self.controller("backlight")
        sc.activate()
        self.assertEqual(sc.method, "backlight")
        self.assertEqual(runner.calls, [])
        self.assertEqual(read(self.bl_file), "0")
        sc.deactivate()
        self.assertEqual(read(self.bl_file), "150")
        self.assertEqual(runner.calls, [])

    def test_candidates_order(self):
        sc, _ = self.controller("auto")
        self.assertEqual([m.name for m in sc.candidates()], ["gamescope", "kscreen", "backlight"])
        self.assertEqual([m.name for m in self.controller("kscreen")[0].candidates()], ["kscreen"])
        with self.assertRaises(ValueError):
            SC.ScreenController(SC.Backlight(self.bl, self.state), touch_event="", method="dpms")

    def test_stale_backlight_state_restored_when_nothing_in_charge(self):
        # a crashed backlight session left brightness 0 + state file; the new session could not sleep
        write(self.bl_file, "0\n")
        write(self.state, "180")
        sc, _ = self.controller("gamescope")   # unavailable -> method none
        sc.activate()
        sc.deactivate()
        self.assertEqual(read(self.bl_file), "180")
        self.assertFalse(os.path.exists(self.state))


class EventSinkScreenTest(unittest.TestCase):
    def test_screen_event_carries_method(self):
        sink = NullEventSink()
        sink.screen(True, "gamescope")
        sink.screen(False)
        self.assertEqual(sink.events, [{"ev": "screen", "off": True, "method": "gamescope"},
                                       {"ev": "screen", "off": False, "method": "none"}])


if __name__ == "__main__":
    unittest.main()
