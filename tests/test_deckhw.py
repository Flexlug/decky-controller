"""``deckhw`` port/cable facts against a fake sysfs tree."""
import os
import shutil
import tempfile
import unittest

import _path  # noqa: F401

from deckhw import cable
from deckhw.drd import detect_drd
from deckhw.port import read_port_status
from deckhw.sysfs import Sysfs
from deckhw.udc import Udc, udc_names
from fakes import FakeSysfs, write


def port_tree(root, *, acad_online=1, pd_mv=5000, pd_ma=1500, usb_host=0, usb=0, udc_state="not attached",
              with_udc=True, with_acad=True, with_hwmon=True, with_labels=True):
    """Fake /sys as seen on the Deck: ACAD, steamdeck_hwmon (deliberately not hwmon0), extcon0, a UDC."""
    fs = FakeSysfs(root)
    fs.add_power_supply(acad_online=acad_online, with_acad=with_acad)
    fs.add_hwmon(pd_mv=pd_mv, pd_ma=pd_ma, with_steamdeck=with_hwmon, with_labels=with_labels)
    fs.add_extcon(usb=usb, usb_host=usb_host)
    if with_udc:
        fs.add_udc(state=udc_state)
    fs.add_pci_bus()
    return fs


class CableDetectionTest(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="deckhw_port_")
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)

    def status(self, **kw):
        fs = port_tree(tempfile.mkdtemp(dir=self.root), **kw)   # fresh tree per call (tests loop over variants)
        return read_port_status(fs.sys, fs.dev, use_modprobe=False)

    # --- the four kinds + unknown -------------------------------------------------------------
    def test_pc_port_idle(self):
        """The user's report: plugged into a PC, no gadget bound -> udc 'not attached' but cable_kind 'pc'."""
        st = self.status(acad_online=1, pd_mv=5000, pd_ma=1500, udc_state="not attached")
        self.assertEqual(st.cable_kind, "pc")
        self.assertIs(st.cable_power, True)
        self.assertEqual((st.pd_contract_mv, st.pd_contract_ma), (5000, 1500))
        self.assertFalse(st.host_connected)          # unchanged semantics: only 'configured' counts
        self.assertEqual(st.udc_state, "not attached")

    def test_pc_port_low_current_variant(self):
        st = self.status(acad_online=1, pd_mv=5000, pd_ma=900)
        self.assertEqual(st.cable_kind, "pc")

    def test_pc_port_enumerated(self):
        st = self.status(acad_online=1, pd_mv=5000, pd_ma=3000, udc_state="configured")
        self.assertEqual(st.cable_kind, "pc")
        self.assertTrue(st.host_connected)

    def test_charger(self):
        for mv in (9000, 15000, 20000):
            st = self.status(acad_online=1, pd_mv=mv, pd_ma=3000)
            self.assertEqual(st.cable_kind, "charger", mv)
            self.assertEqual(st.pd_contract_mv, mv)

    def test_nothing_plugged(self):
        st = self.status(acad_online=0, pd_mv=0, pd_ma=0)
        self.assertEqual(st.cable_kind, "none")
        self.assertIs(st.cable_power, False)
        self.assertEqual((st.pd_contract_mv, st.pd_contract_ma), (0, 0))

    def test_host_device_wins_over_everything(self):
        # dock attached: the port is a host; a dock may also feed power (PD 20 V) - still host_device
        st = self.status(usb_host=1, acad_online=1, pd_mv=20000, pd_ma=3000, with_udc=False)
        self.assertEqual(st.cable_kind, "host_device")
        self.assertIsNone(st.udc_name)
        self.assertFalse(st.host_connected)

    def test_unknown_when_power_unreadable_and_no_contract(self):
        st = self.status(with_acad=False, with_hwmon=False)
        self.assertEqual(st.cable_kind, "unknown")
        self.assertIsNone(st.cable_power)
        self.assertIsNone(st.pd_contract_mv)
        self.assertIsNone(st.pd_contract_ma)

    def test_unknown_when_power_present_but_zero_contract(self):
        # right after plugging in, or a port without a readable contract: power yes, contract 0
        st = self.status(acad_online=1, pd_mv=0, pd_ma=0)
        self.assertEqual(st.cable_kind, "unknown")
        self.assertIs(st.cable_power, True)

    def test_power_unreadable_but_contract_present(self):
        st = self.status(with_acad=False, pd_mv=5000, pd_ma=1500)
        self.assertEqual(st.cable_kind, "pc")
        self.assertIsNone(st.cable_power)

    # --- helpers -------------------------------------------------------------------------------
    def test_hwmon_found_by_name_not_index(self):
        fs = port_tree(self.root, pd_mv=5000, pd_ma=1500)
        self.assertEqual(cable.find_hwmon("steamdeck_hwmon", fs.sys), "hwmon3")
        self.assertIsNone(cable.find_hwmon("nope", fs.sys))
        self.assertEqual(cable.pd_contract(fs.sys), (5000, 1500))     # not hwmon0's 20000/999

    def test_hwmon_without_labels_falls_back_to_in0_curr1(self):
        fs = port_tree(self.root, pd_mv=5000, pd_ma=1500, with_labels=False)
        self.assertEqual(cable.pd_contract(fs.sys), (5000, 1500))

    def test_hwmon_with_mismatching_labels_returns_none(self):
        fs = port_tree(self.root, pd_mv=5000, pd_ma=1500)
        write(os.path.join(fs.hwmon, "in0_label"), "Something Else\n")
        self.assertEqual(cable.pd_contract(fs.sys), (None, 1500))

    def test_cable_power_mains_fallback(self):
        fs = port_tree(self.root, with_acad=False)
        self.assertIsNone(cable.cable_power(fs.sys))
        write(os.path.join(fs.sys, "class", "power_supply", "ADP1", "type"), "Mains\n")
        write(os.path.join(fs.sys, "class", "power_supply", "ADP1", "online"), "1\n")
        self.assertIs(cable.cable_power(fs.sys), True)

    def test_cable_power_garbage_is_none(self):
        fs = port_tree(self.root)
        write(os.path.join(fs.sys, "class", "power_supply", "ACAD", "online"), "abc\n")
        self.assertIsNone(cable.cable_power(fs.sys))

    def test_classify_cable_rule(self):
        c = cable.classify_cable
        self.assertEqual(c({"USB-HOST": 1}, False, 0), "host_device")
        self.assertEqual(c({"USB-HOST": 0}, False, 5000), "none")
        self.assertEqual(c({}, True, 5000), "pc")
        self.assertEqual(c({}, True, 5500), "pc")
        self.assertEqual(c({}, True, 5501), "charger")
        self.assertEqual(c({}, None, 5000), "pc")
        self.assertEqual(c({}, True, None), "unknown")
        self.assertEqual(c({}, None, None), "unknown")
        self.assertEqual(c({}, True, 0), "unknown")
        for kind in ("none", "pc", "charger", "host_device", "unknown"):
            self.assertIn(kind, cable.CABLE_KINDS)

    def test_empty_sysfs_does_not_crash(self):
        empty = os.path.join(self.root, "empty")
        os.makedirs(empty)
        st = read_port_status(empty, os.path.join(self.root, "nodev"), use_modprobe=False)
        self.assertEqual(st.cable_kind, "unknown")
        self.assertIsNone(st.cable_power)
        self.assertIsNone(st.pd_contract_mv)


class CollectStatusTest(unittest.TestCase):
    def test_cli_status_carries_cable_classification(self):
        from deckgadget.__main__ import collect_status

        root = tempfile.mkdtemp(prefix="deckhw_cli_")
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        fs = port_tree(root, acad_online=1, pd_mv=15000, pd_ma=3000)
        fs.add_backlight(brightness=90)
        fs.add_gadget("deckctl_hid")
        state_file = os.path.join(root, "run", "deckgadget", "brightness")
        out = collect_status(fs.sys, fs.dev, use_modprobe=False, configfs=fs.configfs,
                             run_user_base=os.path.join(root, "run", "user"), state_file=state_file)
        self.assertEqual((out["cable_kind"], out["cable_power"], out["pd_contract_mv"], out["pd_contract_ma"]),
                         ("charger", True, 15000, 3000))
        self.assertFalse(out["host_connected"])
        self.assertEqual(out["udc_state"], "not attached")
        self.assertEqual(out["gadgets"], [os.path.join(fs.configfs, "usb_gadget", "deckctl_hid")])
        self.assertEqual((out["backlight"]["available"], out["backlight"]["brightness"], out["backlight"]["state_file"]),
                         (True, 90, state_file))
        self.assertEqual(out["screen_methods"], {"gamescope": False, "kscreen": False, "backlight": True})
        self.assertFalse(os.path.exists(os.path.dirname(state_file)))   # status never creates the state dir
        self.assertTrue(out["ok"], out["errors"])


class SysfsReaderTest(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="deckhw_sysfs_")
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.sysfs = Sysfs(self.root)
        write(os.path.join(self.root, "class", "thing", "text"), "  hello \n")
        write(os.path.join(self.root, "class", "thing", "number"), "42\n")
        write(os.path.join(self.root, "class", "thing", "hexnum"), "0x1f\n")
        write(os.path.join(self.root, "class", "thing", "junk"), "abc\n")
        os.symlink(os.path.join(self.root, "bus", "drivers", "usbhid"), os.path.join(self.root, "class", "thing", "driver"))

    def test_text_int_hex_and_defaults(self):
        self.assertEqual(self.sysfs.text("class", "thing", "text"), "hello")
        self.assertEqual(self.sysfs.int("class", "thing", "number"), 42)
        self.assertEqual(self.sysfs.hex("class", "thing", "hexnum"), 0x1f)
        self.assertIsNone(self.sysfs.text("class", "thing", "missing"))
        self.assertEqual(self.sysfs.text("class", "thing", "missing", default="x"), "x")
        self.assertIsNone(self.sysfs.int("class", "thing", "junk"))
        self.assertEqual(self.sysfs.int("class", "thing", "junk", default=7), 7)

    def test_listdir_link_and_existence(self):
        self.assertEqual(self.sysfs.listdir("class", "thing"), ["driver", "hexnum", "junk", "number", "text"])
        self.assertEqual(self.sysfs.listdir("class", "nope"), [])
        self.assertEqual(self.sysfs.link_name("class", "thing", "driver"), "usbhid")
        self.assertIsNone(self.sysfs.link_name("class", "thing", "text"))
        self.assertTrue(self.sysfs.isdir("class", "thing"))
        self.assertFalse(self.sysfs.exists("class", "thing", "missing"))

    def test_failed_reads_are_logged_at_debug(self):
        with self.assertLogs("deckhw.sysfs", level="DEBUG") as logs:
            self.sysfs.text("class", "thing", "missing")
            self.sysfs.int("class", "thing", "junk")
        self.assertEqual(len(logs.output), 2)


class UdcAndDrdTest(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="deckhw_udc_")
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)

    def test_udc_resolves_first_name_and_reads_attributes(self):
        fs = FakeSysfs(self.root).add_udc(name="dwc3.1.auto", state="configured", speed="high-speed")
        udc = Udc(fs.sys)
        self.assertEqual(udc_names(fs.sys), ["dwc3.1.auto"])
        self.assertEqual(udc.resolve(), "dwc3.1.auto")
        self.assertTrue(udc.configured())
        self.assertEqual((udc.state(), udc.speed(), udc.function()), ("configured", "high-speed", ""))
        fs.set_udc_state("not attached")
        self.assertFalse(udc.configured())

    def test_udc_without_controller(self):
        fs = FakeSysfs(self.root)
        self.assertEqual(udc_names(fs.sys), [])
        self.assertIsNone(Udc(fs.sys).state())
        self.assertFalse(Udc(fs.sys).configured())

    def test_drd_detected_via_driver_link(self):
        fs = FakeSysfs(self.root).add_pci_bus()
        device = os.path.join(fs.sys, "bus", "pci", "devices", "0000:04:00.3")
        write(os.path.join(device, "class"), "0x0c0330\n")
        os.symlink(os.path.join(fs.sys, "bus", "pci", "drivers", "dwc3-pci"), os.path.join(device, "driver"))
        status = detect_drd(fs.sys, use_modprobe=False)
        self.assertEqual((status.enabled, status.pci, status.via), (True, "0000:04:00.3", "driver"))

    def test_drd_absent_without_modprobe(self):
        fs = FakeSysfs(self.root).add_pci_bus()
        device = os.path.join(fs.sys, "bus", "pci", "devices", "0000:04:00.3")
        write(os.path.join(device, "class"), "0x0c0330\n")
        write(os.path.join(device, "modalias"), "pci:v00001022d000015E1sv00001022sd00001234bc0Csc03i30\n")
        os.symlink(os.path.join(fs.sys, "bus", "pci", "drivers", "xhci_hcd"), os.path.join(device, "driver"))
        status = detect_drd(fs.sys, use_modprobe=False)
        self.assertEqual((status.enabled, status.via), (False, "none"))
        self.assertEqual(detect_drd(os.path.join(self.root, "empty"), use_modprobe=False).as_dict(),
                         {"enabled": False, "pci": None, "driver": None, "via": "none"})


if __name__ == "__main__":
    unittest.main()
