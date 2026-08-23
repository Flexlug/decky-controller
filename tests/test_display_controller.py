import os
import shutil
import tempfile
import time
import unittest

import _path  # noqa: F401

from deckgadget.platform.display.backlight import Backlight
from deckgadget.platform.display.compositor import GamescopeSleep
from deckgadget.platform.display.controller import ScreenController
from deckgadget.util.log import NullEventSink
from fakes import FakeRunner, FakeScreenMethod, make_socket, read, write


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
        gs = gamescope if gamescope is not None else GamescopeSleep(
            run_user_base=self.run_user, binary="/usr/bin/gamescopectl", runner=runner)
        ks = kscreen if kscreen is not None else FakeScreenMethod("kscreen", available=False)
        sc = ScreenController(Backlight(self.bl, self.state), touch_event="", wake_seconds=wake_seconds,
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
        ks = FakeScreenMethod("kscreen", available=True)
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
            ScreenController(Backlight(self.bl, self.state), touch_event="", method="dpms")

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
