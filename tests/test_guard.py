import os
import shutil
import tempfile
import unittest
from unittest import mock

import _path  # noqa: F401

from deckgadget.platform import guard, neptune_binding
from deckgadget.platform.display.base import ScreenMethod
from deckhw.neptune import find_neptune
from fakes import FakeSysfs, KernelBinder, read, write


class NeptuneDiscoveryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.fs = FakeSysfs(self.tmp).add_neptune().add_backlight()

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def test_find_neptune(self):
        dev = find_neptune(self.fs.sys, self.fs.dev)
        self.assertIsNotNone(dev)
        self.assertEqual(dev.name, "3-3")
        self.assertEqual(dev.devnode, os.path.join(self.fs.dev, "bus", "usb", "003", "003"))
        self.assertEqual(sorted(dev.interfaces), [0, 1, 2, 3, 4])
        itf2 = dev.interface(2)
        self.assertEqual(itf2.driver, "usbhid")
        ep = itf2.interrupt_in()
        self.assertEqual((ep.address, ep.max_packet, ep.is_interrupt), (0x83, 64, True))
        self.assertEqual(dev.interface(3).driver, "cdc_acm")
        self.assertFalse(dev.captured)
        self.assertEqual(dev.as_dict()["interfaces"]["2"]["endpoints"], ["0x83/int/64"])
        self.fs.unbind(2)
        self.assertTrue(find_neptune(self.fs.sys, self.fs.dev).captured)

    def test_capture_and_release(self):
        dev = find_neptune(self.fs.sys, self.fs.dev)
        binder = neptune_binding.UsbhidBinder(self.fs.sys)
        detached = neptune_binding.capture_interfaces(dev, binder)
        self.assertEqual(detached, ["3-3:1.0", "3-3:1.1", "3-3:1.2"])
        self.assertEqual(read(os.path.join(self.fs.usbhid, "unbind")), "3-3:1.2")  # last write (fake file is overwritten)
        # simulate the kernel detaching the driver for all three
        for n in (0, 1, 2):
            self.fs.unbind(n)
        # second capture is a no-op
        write(os.path.join(self.fs.usbhid, "unbind"), "")
        self.assertEqual(neptune_binding.capture_interfaces(find_neptune(self.fs.sys, self.fs.dev), binder), [])
        self.assertEqual(read(os.path.join(self.fs.usbhid, "unbind")), "")
        rebound = neptune_binding.release_interfaces(find_neptune(self.fs.sys, self.fs.dev), binder)
        self.assertEqual(rebound, ["3-3:1.0", "3-3:1.1", "3-3:1.2"])
        self.assertEqual(read(os.path.join(self.fs.usbhid, "bind")), "3-3:1.2")


class FakeDisplay(ScreenMethod):
    """Scriptable display-sleep strategy (gamescope / kscreen stand-in) for recover()."""

    def __init__(self, name, available=False, wake_ok=True, raise_exc=None):
        self.name = name
        self._available = available
        self.wake_ok = wake_ok
        self.raise_exc = raise_exc
        self.socket_path = f"/run/user/1000/{name}-0"
        self.calls = []

    def available(self):
        self.calls.append("available")
        return self._available

    def sleep(self):
        self.calls.append("sleep")
        return True

    def wake(self):
        self.calls.append("wake")
        if self.raise_exc is not None:
            raise self.raise_exc
        return self.wake_ok


class RecoverTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.fs = FakeSysfs(self.tmp).add_neptune().add_backlight()
        self.gamescope = FakeDisplay("gamescope")
        self.kscreen = FakeDisplay("kscreen")

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def recover(self, kernel=True):
        """Run guard.recover(); with ``kernel`` the fake kernel 'probes' rebound interfaces."""
        binder = (lambda sysfs: KernelBinder(sysfs, self.fs)) if kernel else neptune_binding.UsbhidBinder
        with mock.patch.object(neptune_binding, "UsbhidBinder", binder):
            return guard.recover(sysfs=self.fs.sys, configfs=self.fs.configfs, dev=self.fs.dev,
                                 backlight_dir=self.fs.backlight, state_file=self.fs.state_file,
                                 gamescope=self.gamescope, kscreen=self.kscreen)

    def test_recover_nothing_to_do(self):
        rep = self.recover()
        self.assertTrue(rep["ok"])
        self.assertEqual(rep["gadgets"], [])
        self.assertEqual(rep["neptune"], {"present": True, "name": "3-3", "rebound": [], "still_captured": []})
        self.assertIsNone(rep["backlight"]["restored"])
        self.assertEqual(read(os.path.join(self.fs.usbhid, "bind")), "")
        self.assertEqual(read(os.path.join(self.fs.backlight, "brightness")), "120\n")
        # no compositor reachable: nothing woken, no warnings
        self.assertEqual(rep["display"], {"gamescope": {"available": False}, "kscreen": {"available": False}})
        self.assertEqual(rep["warnings"], [])
        self.assertEqual(self.gamescope.calls, ["available"])
        self.assertEqual(self.kscreen.calls, ["available"])

    def test_recover_wakes_gamescope_and_still_restores_backlight(self):
        """Crashed gamescope-sleep session: wake via gamescope AND restore a saved backlight value."""
        self.gamescope = FakeDisplay("gamescope", available=True)
        write(os.path.join(self.fs.backlight, "brightness"), "0\n")
        write(self.fs.state_file, "180")
        rep = self.recover()
        self.assertTrue(rep["ok"], rep)
        self.assertEqual(self.gamescope.calls, ["available", "wake"])
        self.assertEqual(rep["display"]["gamescope"],
                         {"available": True, "socket": "/run/user/1000/gamescope-0", "woken": True})
        self.assertEqual(rep["display"]["kscreen"], {"available": False})
        self.assertEqual(rep["backlight"]["restored"], 180)
        self.assertEqual(read(os.path.join(self.fs.backlight, "brightness")), "180")
        self.assertEqual(rep["warnings"], [])
        # idempotent: a second run wakes again (harmless) and finds nothing to restore
        rep2 = self.recover()
        self.assertTrue(rep2["ok"])
        self.assertEqual(self.gamescope.calls, ["available", "wake", "available", "wake"])
        self.assertIsNone(rep2["backlight"]["restored"])

    def test_recover_display_wake_failure_is_a_warning_not_an_error(self):
        self.gamescope = FakeDisplay("gamescope", available=True, wake_ok=False)
        self.kscreen = FakeDisplay("kscreen", available=True, raise_exc=RuntimeError("dbus down"))
        rep = self.recover()
        self.assertTrue(rep["ok"], rep)                       # warnings never flip ok
        self.assertEqual(rep["errors"], [])
        self.assertEqual(rep["display"]["gamescope"]["woken"], False)
        self.assertEqual(rep["display"]["kscreen"]["error"], "dbus down")
        self.assertTrue(any(w.startswith("gamescope:") for w in rep["warnings"]), rep["warnings"])
        self.assertTrue(any(w.startswith("kscreen:") for w in rep["warnings"]), rep["warnings"])

    def test_recover_after_crash_is_idempotent(self):
        # state left by a crashed session: ifaces unbound, gadget present, backlight 0 with saved value
        self.fs.unbind(0)
        self.fs.unbind(2)
        g = self.fs.add_gadget("deckctl_hid")
        self.fs.add_gadget("other_gadget")   # not ours: must survive
        write(os.path.join(self.fs.backlight, "brightness"), "0\n")
        write(self.fs.state_file, "180")
        rep = self.recover()
        self.assertTrue(rep["ok"], rep)
        self.assertEqual(rep["neptune"]["rebound"], ["3-3:1.0", "3-3:1.2"])
        self.assertEqual(rep["neptune"]["still_captured"], [])
        self.assertEqual(read(os.path.join(self.fs.usbhid, "bind")), "3-3:1.2")
        self.assertEqual(len(rep["gadgets"]), 1)
        self.assertTrue(rep["gadgets"][0]["removed"])
        self.assertTrue(rep["gadgets"][0]["unbound"])
        self.assertFalse(os.path.exists(g))
        self.assertTrue(os.path.isdir(os.path.join(self.fs.configfs, "usb_gadget", "other_gadget")))
        self.assertEqual(rep["backlight"]["restored"], 180)
        self.assertEqual(read(os.path.join(self.fs.backlight, "brightness")), "180")
        self.assertFalse(os.path.exists(self.fs.state_file))
        # interfaces are back on usbhid (KernelBinder) -> second run does nothing
        self.assertFalse(find_neptune(self.fs.sys, self.fs.dev).captured)
        write(os.path.join(self.fs.usbhid, "bind"), "")
        rep2 = self.recover()
        self.assertTrue(rep2["ok"])
        self.assertEqual(rep2["neptune"]["rebound"], [])
        self.assertEqual(rep2["gadgets"], [])
        self.assertIsNone(rep2["backlight"]["restored"])
        self.assertEqual(read(os.path.join(self.fs.usbhid, "bind")), "")
        self.assertEqual(read(os.path.join(self.fs.backlight, "brightness")), "180")

    def test_recover_reports_failed_rebind(self):
        """A rebind that does not stick must make the report ok=False (the backend toasts it)."""
        self.fs.unbind(2)
        rep = self.recover(kernel=False)   # plain binder: the fake sysfs never 'probes'
        self.assertFalse(rep["ok"], rep)
        self.assertEqual(rep["neptune"]["rebound"], ["3-3:1.2"])
        self.assertEqual(rep["neptune"]["still_captured"], ["3-3:1.2"])
        self.assertTrue(any("still detached" in e for e in rep["errors"]), rep["errors"])

    def test_recover_reports_bind_write_errors(self):
        """bind AND drivers_probe failing (e.g. EISDIR) is collected, not swallowed."""
        self.fs.unbind(0)
        for f in ("bind",):
            os.unlink(os.path.join(self.fs.usbhid, f))
            os.makedirs(os.path.join(self.fs.usbhid, f))
        os.unlink(os.path.join(self.fs.sys, "bus", "usb", "drivers_probe"))
        os.makedirs(os.path.join(self.fs.sys, "bus", "usb", "drivers_probe"))
        rep = self.recover(kernel=False)
        self.assertFalse(rep["ok"], rep)
        self.assertEqual(rep["neptune"]["rebound"], [])
        self.assertTrue(any("cannot rebind 3-3:1.0" in e for e in rep["errors"]), rep["errors"])
        self.assertTrue(any("still detached" in e for e in rep["errors"]), rep["errors"])

    def test_udc_unbind_writes_a_newline(self):
        """An empty str write never reaches write(2); the UDC attribute needs "\\n" to unbind."""
        g = self.fs.add_gadget("deckctl_hid")
        rep = guard.remove_configfs_gadget(g)
        self.assertTrue(rep["removed"])
        self.assertTrue(rep["unbound"])
        # the fake attribute file was overwritten with exactly the newline
        # (the directory is gone now; check through a second gadget whose removal is blocked)
        g2 = self.fs.add_gadget("deckctl_x")
        seen = {}
        real_write = guard.write_text

        def spy(path, text):
            seen[path] = text
            real_write(path, text)

        with mock.patch.object(guard, "write_text", spy):
            guard.remove_configfs_gadget(g2)
        self.assertEqual(seen.get(os.path.join(g2, "UDC")), "\n")

    def test_recover_without_neptune_or_backlight(self):
        shutil.rmtree(os.path.join(self.fs.devices, "3-3"))
        shutil.rmtree(self.fs.backlight)
        rep = self.recover()
        self.assertTrue(rep["ok"], rep)
        self.assertEqual(rep["neptune"], {"present": False})
        self.assertFalse(rep["backlight"]["available"])

    def test_saved_zero_never_restores_to_dark(self):
        write(os.path.join(self.fs.backlight, "brightness"), "0\n")
        write(self.fs.state_file, "0")
        rep = self.recover()
        self.assertEqual(rep["backlight"]["restored"], 127)
        self.assertEqual(read(os.path.join(self.fs.backlight, "brightness")), "127")

    def test_gadget_that_cannot_be_removed_is_an_error(self):
        if os.geteuid() == 0:
            self.skipTest("root ignores directory permissions")
        gadget = self.fs.add_gadget("deckctl_hid")
        locked = os.path.join(gadget, "functions", "hid.usb0", "locked")
        write(os.path.join(locked, "keep"), "x")
        os.chmod(locked, 0o555)                     # its file cannot be unlinked → the tree survives
        try:
            rep = self.recover()
        finally:
            os.chmod(locked, 0o755)
        self.assertFalse(rep["ok"], rep)
        self.assertFalse(rep["gadgets"][0]["removed"])
        self.assertTrue(any("still present" in e for e in rep["errors"]), rep["errors"])

    def test_remove_gadget_missing(self):
        rep = guard.remove_configfs_gadget(os.path.join(self.fs.configfs, "usb_gadget", "nope"))
        self.assertFalse(rep["existed"])
        self.assertFalse(rep["removed"])


if __name__ == "__main__":
    unittest.main()
