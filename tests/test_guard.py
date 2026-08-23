import os
import shutil
import tempfile
import unittest
from unittest import mock

import _path  # noqa: F401

from deckgadget.platform import guard, neptune, screen


def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(text)


class FakeSysfs:
    """Builds a fake /sys with a Neptune device (bus 3 dev 3), usbhid driver dirs and a backlight."""

    def __init__(self, root):
        self.root = root
        self.sys = os.path.join(root, "sys")
        self.dev = os.path.join(root, "dev")
        self.configfs = os.path.join(root, "configfs")
        self.devices = os.path.join(self.sys, "bus", "usb", "devices")
        self.usbhid = os.path.join(self.sys, "bus", "usb", "drivers", "usbhid")
        os.makedirs(self.usbhid)
        write(os.path.join(self.usbhid, "bind"), "")
        write(os.path.join(self.usbhid, "unbind"), "")
        write(os.path.join(self.sys, "bus", "usb", "drivers_probe"), "")
        d = os.path.join(self.devices, "3-3")
        write(os.path.join(d, "idVendor"), "28de\n")
        write(os.path.join(d, "idProduct"), "1205\n")
        write(os.path.join(d, "busnum"), "3\n")
        write(os.path.join(d, "devnum"), "3\n")
        write(os.path.join(d, "product"), "Steam Controller\n")
        # a decoy device
        write(os.path.join(self.devices, "1-1", "idVendor"), "05e3\n")
        write(os.path.join(self.devices, "1-1", "idProduct"), "0610\n")
        write(os.path.join(self.devices, "usb1", "idVendor"), "1d6b\n")
        write(os.path.join(self.devices, "usb1", "idProduct"), "0002\n")
        for n, (cls, sub, proto, eps) in {0: (3, 0, 2, [(0x81, 3, 64)]), 1: (3, 1, 1, [(0x82, 3, 64)]),
                                          2: (3, 0, 0, [(0x83, 3, 64)]), 3: (2, 2, 1, [(0x84, 3, 16)]),
                                          4: (10, 0, 0, [(0x85, 2, 64), (0x05, 2, 64)])}.items():
            itf = os.path.join(d, f"3-3:1.{n}")
            write(os.path.join(itf, "bInterfaceNumber"), f"{n:02x}\n")
            write(os.path.join(itf, "bInterfaceClass"), f"{cls:02x}\n")
            write(os.path.join(itf, "bInterfaceSubClass"), f"{sub:02x}\n")
            write(os.path.join(itf, "bInterfaceProtocol"), f"{proto:02x}\n")
            for addr, attr, mp in eps:
                ep = os.path.join(itf, f"ep_{addr:02x}")
                write(os.path.join(ep, "bEndpointAddress"), f"{addr:02x}\n")
                write(os.path.join(ep, "bmAttributes"), f"{attr:02x}\n")
                write(os.path.join(ep, "wMaxPacketSize"), f"{mp:04x}\n")
                write(os.path.join(ep, "bInterval"), "04\n")
                write(os.path.join(ep, "direction"), "in\n" if addr & 0x80 else "out\n")
            # devices/<itf> symlink like real sysfs
            os.symlink(itf, os.path.join(self.devices, f"3-3:1.{n}"))
            self.bind(n, "usbhid" if n < 3 else "cdc_acm")
        # backlight
        self.bl = os.path.join(self.sys, "class", "backlight", "amdgpu_bl0")
        write(os.path.join(self.bl, "brightness"), "120\n")
        write(os.path.join(self.bl, "max_brightness"), "255\n")
        self.state_file = os.path.join(root, "run", "brightness")

    def itf(self, n):
        return os.path.join(self.devices, "3-3", f"3-3:1.{n}")

    def bind(self, n, driver):
        link = os.path.join(self.itf(n), "driver")
        if os.path.lexists(link):
            os.unlink(link)
        os.symlink(os.path.join(self.sys, "bus", "usb", "drivers", driver), link)

    def unbind(self, n):
        link = os.path.join(self.itf(n), "driver")
        if os.path.lexists(link):
            os.unlink(link)

    def read(self, *parts):
        with open(os.path.join(*parts)) as f:
            return f.read()

    def add_gadget(self, name):
        g = os.path.join(self.configfs, "usb_gadget", name)
        write(os.path.join(g, "UDC"), "dwc3.1.auto\n")
        write(os.path.join(g, "idVendor"), "0x1d6b\n")
        os.makedirs(os.path.join(g, "strings", "0x409"))
        os.makedirs(os.path.join(g, "configs", "c.1", "strings", "0x409"))
        write(os.path.join(g, "configs", "c.1", "MaxPower"), "250\n")
        os.makedirs(os.path.join(g, "functions", "hid.usb0"))
        os.symlink(os.path.join(g, "functions", "hid.usb0"), os.path.join(g, "configs", "c.1", "hid.usb0"))
        return g


class KernelBinder(neptune.UsbhidBinder):
    """UsbhidBinder whose successful ``bind`` also creates the ``driver`` symlink — what the kernel
    does synchronously when usbhid probes the interface (recover() re-scans sysfs to verify)."""

    def __init__(self, sysfs, fs):
        super().__init__(sysfs)
        self.fs = fs

    def bind(self, itf_name):
        ok = super().bind(itf_name)
        if ok:
            self.fs.bind(int(itf_name.rsplit(".", 1)[1]), "usbhid")
        return ok


class NeptuneDiscoveryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.fs = FakeSysfs(self.tmp)

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def test_find_neptune(self):
        dev = neptune.find_neptune(self.fs.sys, self.fs.dev)
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
        self.assertTrue(neptune.find_neptune(self.fs.sys, self.fs.dev).captured)

    def test_capture_and_release(self):
        dev = neptune.find_neptune(self.fs.sys, self.fs.dev)
        binder = neptune.UsbhidBinder(self.fs.sys)
        detached = neptune.capture_interfaces(dev, binder)
        self.assertEqual(detached, ["3-3:1.0", "3-3:1.1", "3-3:1.2"])
        self.assertEqual(self.fs.read(self.fs.usbhid, "unbind"), "3-3:1.2")  # last write (fake file is overwritten)
        # simulate the kernel detaching the driver for all three
        for n in (0, 1, 2):
            self.fs.unbind(n)
        # second capture is a no-op
        write(os.path.join(self.fs.usbhid, "unbind"), "")
        self.assertEqual(neptune.capture_interfaces(neptune.find_neptune(self.fs.sys, self.fs.dev), binder), [])
        self.assertEqual(self.fs.read(self.fs.usbhid, "unbind"), "")
        rebound = neptune.release_interfaces(neptune.find_neptune(self.fs.sys, self.fs.dev), binder)
        self.assertEqual(rebound, ["3-3:1.0", "3-3:1.1", "3-3:1.2"])
        self.assertEqual(self.fs.read(self.fs.usbhid, "bind"), "3-3:1.2")


class FakeDisplay(screen.ScreenMethod):
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
        self.fs = FakeSysfs(self.tmp)
        self.gamescope = FakeDisplay("gamescope")
        self.kscreen = FakeDisplay("kscreen")

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def recover(self, kernel=True):
        """Run guard.recover(); with ``kernel`` the fake kernel 'probes' rebound interfaces."""
        binder = (lambda sysfs: KernelBinder(sysfs, self.fs)) if kernel else neptune.UsbhidBinder
        with mock.patch.object(neptune, "UsbhidBinder", binder):
            return guard.recover(sysfs=self.fs.sys, configfs=self.fs.configfs, dev=self.fs.dev,
                                 backlight_dir=self.fs.bl, state_file=self.fs.state_file,
                                 gamescope=self.gamescope, kscreen=self.kscreen)

    def test_recover_nothing_to_do(self):
        rep = self.recover()
        self.assertTrue(rep["ok"])
        self.assertEqual(rep["gadgets"], [])
        self.assertEqual(rep["neptune"], {"present": True, "name": "3-3", "rebound": [], "still_captured": []})
        self.assertIsNone(rep["backlight"]["restored"])
        self.assertEqual(self.fs.read(self.fs.usbhid, "bind"), "")
        self.assertEqual(self.fs.read(self.fs.bl, "brightness"), "120\n")
        # no compositor reachable: nothing woken, no warnings
        self.assertEqual(rep["display"], {"gamescope": {"available": False}, "kscreen": {"available": False}})
        self.assertEqual(rep["warnings"], [])
        self.assertEqual(self.gamescope.calls, ["available"])
        self.assertEqual(self.kscreen.calls, ["available"])

    def test_recover_wakes_gamescope_and_still_restores_backlight(self):
        """Crashed gamescope-sleep session: wake via gamescope AND restore a saved backlight value."""
        self.gamescope = FakeDisplay("gamescope", available=True)
        write(os.path.join(self.fs.bl, "brightness"), "0\n")
        write(self.fs.state_file, "180")
        rep = self.recover()
        self.assertTrue(rep["ok"], rep)
        self.assertEqual(self.gamescope.calls, ["available", "wake"])
        self.assertEqual(rep["display"]["gamescope"],
                         {"available": True, "socket": "/run/user/1000/gamescope-0", "woken": True})
        self.assertEqual(rep["display"]["kscreen"], {"available": False})
        self.assertEqual(rep["backlight"]["restored"], 180)
        self.assertEqual(self.fs.read(self.fs.bl, "brightness"), "180")
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
        write(os.path.join(self.fs.bl, "brightness"), "0\n")
        write(self.fs.state_file, "180")
        rep = self.recover()
        self.assertTrue(rep["ok"], rep)
        self.assertEqual(rep["neptune"]["rebound"], ["3-3:1.0", "3-3:1.2"])
        self.assertEqual(rep["neptune"]["still_captured"], [])
        self.assertEqual(self.fs.read(self.fs.usbhid, "bind"), "3-3:1.2")
        self.assertEqual(len(rep["gadgets"]), 1)
        self.assertTrue(rep["gadgets"][0]["removed"])
        self.assertTrue(rep["gadgets"][0]["unbound"])
        self.assertFalse(os.path.exists(g))
        self.assertTrue(os.path.isdir(os.path.join(self.fs.configfs, "usb_gadget", "other_gadget")))
        self.assertEqual(rep["backlight"]["restored"], 180)
        self.assertEqual(self.fs.read(self.fs.bl, "brightness"), "180")
        self.assertFalse(os.path.exists(self.fs.state_file))
        # interfaces are back on usbhid (KernelBinder) -> second run does nothing
        self.assertFalse(neptune.find_neptune(self.fs.sys, self.fs.dev).captured)
        write(os.path.join(self.fs.usbhid, "bind"), "")
        rep2 = self.recover()
        self.assertTrue(rep2["ok"])
        self.assertEqual(rep2["neptune"]["rebound"], [])
        self.assertEqual(rep2["gadgets"], [])
        self.assertIsNone(rep2["backlight"]["restored"])
        self.assertEqual(self.fs.read(self.fs.usbhid, "bind"), "")
        self.assertEqual(self.fs.read(self.fs.bl, "brightness"), "180")

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
        shutil.rmtree(self.fs.bl)
        rep = self.recover()
        self.assertTrue(rep["ok"], rep)
        self.assertEqual(rep["neptune"], {"present": False})
        self.assertFalse(rep["backlight"]["available"])

    def test_saved_zero_never_restores_to_dark(self):
        write(os.path.join(self.fs.bl, "brightness"), "0\n")
        write(self.fs.state_file, "0")
        rep = self.recover()
        self.assertEqual(rep["backlight"]["restored"], 127)
        self.assertEqual(self.fs.read(self.fs.bl, "brightness"), "127")

    def test_remove_gadget_missing(self):
        rep = guard.remove_configfs_gadget(os.path.join(self.fs.configfs, "usb_gadget", "nope"))
        self.assertFalse(rep["existed"])
        self.assertFalse(rep["removed"])


if __name__ == "__main__":
    unittest.main()
