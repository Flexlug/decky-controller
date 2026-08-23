"""Cable detection (``platform/usb_role.py``) against a fake sysfs tree.

The fixtures mirror what was read on a Deck OLED (docs/HARDWARE.md, docs/ARCHITECTURE.md):
``/sys/class/power_supply/ACAD/online``, the ``steamdeck_hwmon`` hwmon with the PD contract
channels and ``steamdeck-extcon``.
"""
import os
import shutil
import tempfile
import unittest

import _path  # noqa: F401

from deckgadget.platform import usb_role as UR


def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(text)


class FakeSysfs:
    """Fake /sys: ACAD power supply, steamdeck_hwmon (deliberately not hwmon0), extcon0 and a UDC."""

    def __init__(self, root, *, acad_online=1, pd_mv=5000, pd_ma=1500, usb_host=0, usb=0,
                 udc_state="not attached", with_udc=True, with_acad=True, with_hwmon=True, with_labels=True):
        self.sys = os.path.join(root, "sys")
        self.dev = os.path.join(root, "dev")
        os.makedirs(self.dev)
        ps = os.path.join(self.sys, "class", "power_supply")
        # a battery (decoy) is always there
        write(os.path.join(ps, "BAT1", "type"), "Battery\n")
        write(os.path.join(ps, "BAT1", "online"), "1\n")
        if with_acad:
            write(os.path.join(ps, "ACAD", "type"), "Mains\n")
            write(os.path.join(ps, "ACAD", "online"), f"{acad_online}\n")
        hw = os.path.join(self.sys, "class", "hwmon")
        # decoys: a hwmon with its own in0/curr1 channels (must never be picked) and an unreadable one
        write(os.path.join(hw, "hwmon0", "name"), "amdgpu\n")
        write(os.path.join(hw, "hwmon0", "in0_input"), "20000\n")
        write(os.path.join(hw, "hwmon0", "in0_label"), "vddgfx\n")
        write(os.path.join(hw, "hwmon0", "curr1_input"), "999\n")
        os.makedirs(os.path.join(hw, "hwmon1"))
        if with_hwmon:
            self.hwmon = os.path.join(hw, "hwmon3")
            write(os.path.join(self.hwmon, "name"), "steamdeck_hwmon\n")
            if with_labels:
                # real layout: in0 = PD voltage, curr1 = PD current, plus other channels
                write(os.path.join(self.hwmon, "in0_label"), "PD Contract Voltage\n")
                write(os.path.join(self.hwmon, "curr1_label"), "PD Contract Current\n")
                write(os.path.join(self.hwmon, "temp1_label"), "Battery Temp\n")
                write(os.path.join(self.hwmon, "temp1_input"), "30000\n")
            write(os.path.join(self.hwmon, "in0_input"), f"{pd_mv}\n")
            write(os.path.join(self.hwmon, "curr1_input"), f"{pd_ma}\n")
        ext = os.path.join(self.sys, "class", "extcon", "extcon0")
        write(os.path.join(ext, "name"), "steamdeck-extcon\n")
        write(os.path.join(ext, "state"), f"USB={usb}\nUSB-HOST={usb_host}\nSDP=0\nCDP=0\nDCP=0\nACA=0\n")
        if with_udc:
            udc = os.path.join(self.sys, "class", "udc", "dwc3.1.auto")
            write(os.path.join(udc, "state"), f"{udc_state}\n")
            write(os.path.join(udc, "current_speed"), "UNKNOWN\n")
            write(os.path.join(udc, "function"), "\n")
        # no PCI devices -> detect_drd must not crash
        os.makedirs(os.path.join(self.sys, "bus", "pci", "devices"))


class CableDetectionTest(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="usb_role_")
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)

    def status(self, **kw):
        fs = FakeSysfs(tempfile.mkdtemp(dir=self.root), **kw)   # fresh tree per call (tests loop over variants)
        return UR.usb_role_status(fs.sys, fs.dev, use_modprobe=False)

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
        fs = FakeSysfs(self.root, pd_mv=5000, pd_ma=1500)
        self.assertEqual(UR.find_hwmon("steamdeck_hwmon", fs.sys), fs.hwmon)
        self.assertIsNone(UR.find_hwmon("nope", fs.sys))
        self.assertEqual(UR.pd_contract(fs.sys), (5000, 1500))     # not hwmon0's 20000/999

    def test_hwmon_without_labels_falls_back_to_in0_curr1(self):
        fs = FakeSysfs(self.root, pd_mv=5000, pd_ma=1500, with_labels=False)
        self.assertEqual(UR.pd_contract(fs.sys), (5000, 1500))

    def test_hwmon_with_mismatching_labels_returns_none(self):
        fs = FakeSysfs(self.root, pd_mv=5000, pd_ma=1500)
        write(os.path.join(fs.hwmon, "in0_label"), "Something Else\n")
        self.assertEqual(UR.pd_contract(fs.sys), (None, 1500))

    def test_cable_power_mains_fallback(self):
        fs = FakeSysfs(self.root, with_acad=False)
        self.assertIsNone(UR.cable_power(fs.sys))
        write(os.path.join(fs.sys, "class", "power_supply", "ADP1", "type"), "Mains\n")
        write(os.path.join(fs.sys, "class", "power_supply", "ADP1", "online"), "1\n")
        self.assertIs(UR.cable_power(fs.sys), True)

    def test_cable_power_garbage_is_none(self):
        fs = FakeSysfs(self.root)
        write(os.path.join(fs.sys, "class", "power_supply", "ACAD", "online"), "abc\n")
        self.assertIsNone(UR.cable_power(fs.sys))

    def test_classify_cable_rule(self):
        c = UR.classify_cable
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
            self.assertIn(kind, UR.CABLE_KINDS)

    def test_as_dict_exposes_new_keys(self):
        d = self.status(acad_online=1, pd_mv=5000, pd_ma=1500).as_dict()
        for key in ("cable_power", "pd_contract_mv", "pd_contract_ma", "cable_kind", "host_connected", "udc_state"):
            self.assertIn(key, d)
        self.assertEqual(d["cable_kind"], "pc")

    def test_empty_sysfs_does_not_crash(self):
        empty = os.path.join(self.root, "empty")
        os.makedirs(empty)
        st = UR.usb_role_status(empty, os.path.join(self.root, "nodev"), use_modprobe=False)
        self.assertEqual(st.cable_kind, "unknown")
        self.assertIsNone(st.cable_power)
        self.assertIsNone(st.pd_contract_mv)


class CollectStatusTest(unittest.TestCase):
    """``deckgadget status`` JSON carries the new keys (what main.py reads)."""

    def test_collect_status_has_cable_keys(self):
        from deckgadget.__main__ import collect_status

        root = tempfile.mkdtemp(prefix="usb_role_cli_")
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        fs = FakeSysfs(root, acad_online=1, pd_mv=15000, pd_ma=3000)
        out = collect_status(fs.sys, fs.dev, use_modprobe=False)
        self.assertEqual(out["cable_kind"], "charger")
        self.assertIs(out["cable_power"], True)
        self.assertEqual(out["pd_contract_mv"], 15000)
        self.assertEqual(out["pd_contract_ma"], 3000)
        self.assertFalse(out["host_connected"])


if __name__ == "__main__":
    unittest.main()
