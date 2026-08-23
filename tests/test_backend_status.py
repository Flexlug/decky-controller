"""``controller_backend.status``: hardware facts from a fake Deck sysfs and the Status dict assembly."""
import os
import shutil
import tempfile
import unittest

import _path  # noqa: F401

from controller_backend.session import SessionView
from controller_backend.settings import DEFAULT_SETTINGS
from controller_backend.status import build_status, connectivity_signature, hardware_facts
from fakes import FakeSysfs, write


def deck_sysfs(root, *, udc_state="not attached", acad_online=1, pd_mv=5000, pd_ma=1500, usb_host=0,
               drd=True, neptune=True):
    """A Deck as seen from sysfs: PC on the cable, DRD on, Neptune bound to usbhid (unless told otherwise)."""
    fs = FakeSysfs(root)
    fs.add_power_supply(acad_online=acad_online).add_hwmon(pd_mv=pd_mv, pd_ma=pd_ma)
    fs.add_extcon(usb=0 if usb_host else 1, usb_host=usb_host).add_udc(state=udc_state, speed="high-speed")
    fs.add_pci_bus()
    write(os.path.join(fs.sys, "class", "dmi", "id", "product_name"), "Galileo\n")
    pci_device = os.path.join(fs.sys, "bus", "pci", "devices", "0000:04:00.3")
    write(os.path.join(pci_device, "class"), "0x0c0330\n")
    os.symlink(os.path.join(fs.sys, "bus", "pci", "drivers", "dwc3-pci" if drd else "xhci_hcd"),
               os.path.join(pci_device, "driver"))
    if neptune:
        fs.add_neptune()
    return fs


class HardwareFactsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="backend_status_")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def test_pc_plugged_idle(self):
        fs = deck_sysfs(self.tmp)
        facts = hardware_facts(fs.sys, fs.dev)
        self.assertEqual(facts["model"], "Galileo")
        self.assertTrue(facts["drd_enabled"])
        self.assertEqual((facts["udc_name"], facts["udc_state"], facts["udc_speed"]),
                         ("dwc3.1.auto", "not attached", "high-speed"))
        self.assertFalse(facts["host_connected"])
        self.assertEqual(facts["extcon"], {"USB": 1, "USB-HOST": 0, "SDP": 0, "CDP": 0, "DCP": 0, "ACA": 0})
        self.assertIs(facts["cable_power"], True)
        self.assertEqual((facts["pd_contract_mv"], facts["pd_contract_ma"]), (5000, 1500))
        self.assertEqual(facts["cable_kind"], "pc")
        self.assertTrue(facts["neptune_present"])
        self.assertFalse(facts["neptune_captured"])

    def test_configured_means_host_connected(self):
        fs = deck_sysfs(self.tmp, udc_state="configured")
        self.assertTrue(hardware_facts(fs.sys, fs.dev)["host_connected"])

    def test_charger_dock_and_unplugged_classification(self):
        charger = deck_sysfs(os.path.join(self.tmp, "a"), pd_mv=20000)
        self.assertEqual(hardware_facts(charger.sys, charger.dev)["cable_kind"], "charger")
        dock = deck_sysfs(os.path.join(self.tmp, "b"), usb_host=1)
        self.assertEqual(hardware_facts(dock.sys, dock.dev)["cable_kind"], "host_device")
        unplugged = deck_sysfs(os.path.join(self.tmp, "c"), acad_online=0, pd_mv=0)
        facts = hardware_facts(unplugged.sys, unplugged.dev)
        self.assertEqual((facts["cable_kind"], facts["cable_power"]), ("none", False))

    def test_drd_off_no_neptune_and_detached_neptune(self):
        fs = deck_sysfs(self.tmp, drd=False, neptune=False)
        facts = hardware_facts(fs.sys, fs.dev)
        self.assertFalse(facts["drd_enabled"])
        self.assertFalse(facts["neptune_present"])
        captured = deck_sysfs(os.path.join(self.tmp, "captured"))
        captured.unbind(2)
        self.assertTrue(hardware_facts(captured.sys, captured.dev)["neptune_captured"])

    def test_empty_sysfs_is_all_unknown(self):
        empty = os.path.join(self.tmp, "empty")
        os.makedirs(empty)
        facts = hardware_facts(empty, os.path.join(self.tmp, "nodev"))
        self.assertEqual((facts["drd_enabled"], facts["udc_name"], facts["cable_kind"], facts["neptune_present"],
                          facts["cable_power"], facts["extcon"]),
                         (False, None, "unknown", False, None, {"USB": 0, "USB-HOST": 0}))

    def test_connectivity_signature_tracks_only_port_facts(self):
        fs = deck_sysfs(self.tmp)
        before = connectivity_signature(hardware_facts(fs.sys, fs.dev))
        write(os.path.join(fs.sys, "class", "dmi", "id", "product_name"), "Jupiter\n")
        self.assertEqual(before, connectivity_signature(hardware_facts(fs.sys, fs.dev)))
        fs.set_udc_state("configured")
        self.assertNotEqual(before, connectivity_signature(hardware_facts(fs.sys, fs.dev)))


class BuildStatusTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="backend_build_status_")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        fs = deck_sysfs(self.tmp)
        self.facts = hardware_facts(fs.sys, fs.dev)
        self.session = SessionView()

    def status(self, **overrides):
        arguments = dict(plugin_version="0.1.0", facts=self.facts, cli_status=None, cli_error=None,
                         session=self.session, running=False, daemon_pid=None, settings=DEFAULT_SETTINGS)
        arguments.update(overrides)
        return build_status(**arguments)

    def test_idle_status(self):
        status = self.status(cli_error="unavailable")
        self.assertTrue(status["ok"])
        self.assertEqual((status["session_state"], status["daemon_running"], status["daemon_pid"]), ("IDLE", False, None))
        self.assertEqual(status["cable_kind"], "pc")
        self.assertFalse(status["screen_off"])
        self.assertEqual(status["status_error"], "unavailable")
        self.assertEqual(status["metrics"], {"hz": 0, "reports": 0, "dropped": 0})

    def test_cli_status_overrides_sysfs_facts(self):
        status = self.status(cli_status={"cable_kind": "charger", "neptune_captured": True})
        self.assertEqual(status["cable_kind"], "charger")
        self.assertTrue(status["neptune_captured"])

    def test_running_session_fields_and_inferred_screen_off(self):
        self.session.begin("xbox360", "raw")
        self.session.apply({"ev": "state", "state": "ACTIVE", "detail": "250 Hz"})
        status = self.status(running=True, daemon_pid=4242)
        self.assertEqual((status["session_state"], status["session_detail"], status["daemon_pid"]), ("ACTIVE", "250 Hz", 4242))
        self.assertEqual((status["active_profile"], status["transport"]), ("xbox360", "raw"))
        self.assertTrue(status["neptune_captured"])
        self.assertTrue(status["screen_off"])   # inferred from settings + state until the daemon says otherwise
        self.session.apply({"ev": "screen", "off": False, "method": "none"})
        self.assertFalse(self.status(running=True, daemon_pid=4242)["screen_off"])

    def test_session_fields_are_idle_once_the_process_is_gone(self):
        self.session.begin("xbox360", "raw")
        self.session.apply({"ev": "state", "state": "STOPPED"})
        self.assertEqual(self.status(running=True, daemon_pid=1)["session_state"], "STOPPING")
        status = self.status(running=False)
        self.assertEqual((status["session_state"], status["active_profile"], status["screen_off"]), ("IDLE", None, False))


if __name__ == "__main__":
    unittest.main()
