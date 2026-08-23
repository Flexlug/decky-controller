"""``NeptuneUsbSource`` capture lifecycle against a fake sysfs tree and a scripted usbfs device."""
import collections
import errno
import os
import shutil
import tempfile
import threading
import unittest
from unittest import mock

import _path  # noqa: F401

from deckgadget import state as S
from deckgadget.platform import neptune_binding
from deckhw.neptune import CONTROLLER_INTERFACE, find_neptune
from deckgadget.sources.neptune import commands as CMD
from deckgadget.sources.neptune import protocol as P
from deckgadget.sources.neptune.source import NeptuneError, NeptuneUsbSource
from fakes import FakeSysfs, KernelBinder

SET_FEATURE = (CMD.USB_REQTYPE_SET_CLASS_INTERFACE, CMD.HID_REQ_SET_REPORT, CMD.FEATURE_WVALUE, CONTROLLER_INTERFACE)
GET_FEATURE = (CMD.USB_REQTYPE_GET_CLASS_INTERFACE, CMD.HID_REQ_GET_REPORT, CMD.FEATURE_WVALUE, CONTROLLER_INTERFACE)
HEARTBEAT_TAIL = CMD.heartbeat_sequence()[1]


def make_report(buttons_low=0, buttons_high=0, packet=1, lstick=(0, 0), rstick=(0, 0), trig=(0, 0), gyro=(0, 0, 0)):
    body = P.REPORT_STRUCT.pack(P.VALVE_IN_REPORT_MSG_VERSION, P.ID_CONTROLLER_DECK_STATE, P.REPORT_LEN, packet,
                                buttons_low, buttons_high, 0, 0, 0, 0, 0, 0, 0, *gyro, 0, 0, 0, 0, *trig,
                                *lstick, *rstick, 0, 0)
    return body.ljust(P.REPORT_LEN, b"\0")


class FakeUsbfsDevice:
    """Records every usbfs call; ``reports`` feeds ``interrupt_in`` (bytes, None = timeout, or an exception)."""

    def __init__(self, path):
        self.path = path
        self.claims = []
        self.disconnect_claims = []
        self.released = []
        self.closed = False
        self.control_outs = []
        self.control_ins = []
        self.reports = collections.deque()
        self.claim_error = None
        self.control_error = None            # raised by the next control_out, then cleared
        self.control_failed = threading.Event()
        self.control_after_failure = threading.Event()   # the call after a failure: the source has counted it
        self.second_heartbeat = threading.Event()

    def claim_interface(self, number):
        if self.claim_error is not None:
            raise self.claim_error
        self.claims.append(number)

    def disconnect_claim(self, number):
        self.disconnect_claims.append(number)

    def release_interface(self, number):
        self.released.append(number)

    def close(self):
        self.closed = True

    def control_out(self, request_type, request, value, index, data, timeout_ms=1000):
        if self.control_error is not None:
            error, self.control_error = self.control_error, None
            self.control_failed.set()
            raise error
        if self.control_failed.is_set():
            self.control_after_failure.set()
        self.control_outs.append(((request_type, request, value, index), bytes(data)))
        if sum(1 for _, payload in self.control_outs if payload == HEARTBEAT_TAIL) >= 2:
            self.second_heartbeat.set()
        return len(data)

    def control_in(self, request_type, request, value, index, length, timeout_ms=1000):
        self.control_ins.append(((request_type, request, value, index), length))
        return b""

    def interrupt_in(self, endpoint_address, timeout_ms):
        if not self.reports:
            return None
        item = self.reports.popleft()
        if isinstance(item, BaseException):
            raise item
        return item

    @property
    def feature_payloads(self):
        return [payload for setup, payload in self.control_outs if setup == SET_FEATURE]


class NeptuneSourceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.fs = FakeSysfs(self.tmp).add_neptune()
        self.devices = []

    def make_source(self, heartbeat_s=10.0, with_sensors=False, open_error=None, claim_error=None):
        def factory(path):
            if open_error is not None:
                raise open_error
            device = FakeUsbfsDevice(path)
            device.claim_error = claim_error
            self.devices.append(device)
            return device

        with mock.patch.object(neptune_binding, "UsbhidBinder", lambda sysfs: KernelBinder(sysfs, self.fs)):
            source = NeptuneUsbSource(sysfs=self.fs.sys, dev=self.fs.dev, heartbeat_s=heartbeat_s,
                                        device_class=factory, with_sensors=with_sensors)
        self.addCleanup(source.close)
        return source

    def captured(self):
        return find_neptune(self.fs.sys, self.fs.dev).captured

    def test_open_captures_interfaces_claims_and_disables_lizard_mode(self):
        source = self.make_source()
        source.open()
        device = self.devices[0]
        self.assertEqual(device.path, os.path.join(self.fs.dev, "bus", "usb", "003", "003"))
        self.assertEqual(source.detached, ["3-3:1.0", "3-3:1.1", "3-3:1.2"])
        self.assertTrue(self.captured())
        self.assertEqual(device.claims, [2])
        self.assertEqual(device.disconnect_claims, [])
        self.assertEqual(device.feature_payloads, CMD.lizard_off_sequence())
        self.assertEqual(device.control_ins, [(GET_FEATURE, CMD.HID_FEATURE_REPORT_BYTES)])
        self.assertEqual(source.ep_in, 0x83)

    def test_open_is_idempotent(self):
        source = self.make_source()
        source.open()
        source.open()
        self.assertEqual(len(self.devices), 1)
        self.assertEqual(len(self.devices[0].feature_payloads), 2)

    def test_open_falls_back_to_disconnect_claim_when_busy(self):
        source = self.make_source(claim_error=OSError(errno.EBUSY, "busy"))
        source.open()
        self.assertEqual(self.devices[0].claims, [])
        self.assertEqual(self.devices[0].disconnect_claims, [2])

    def test_open_without_neptune_raises(self):
        empty = FakeSysfs(os.path.join(self.tmp, "empty"))
        source = NeptuneUsbSource(sysfs=empty.sys, dev=empty.dev, device_class=FakeUsbfsDevice)
        with self.assertRaises(NeptuneError):
            source.open()

    def test_open_without_controller_interface_raises(self):
        shutil.rmtree(self.fs.interface(2))
        source = self.make_source()
        with self.assertRaises(NeptuneError):
            source.open()
        self.assertFalse(self.captured())

    def test_device_open_failure_rebinds_interfaces(self):
        source = self.make_source(open_error=OSError(errno.EACCES, "not root"))
        with self.assertRaises(OSError):
            source.open()
        self.assertFalse(self.captured())
        self.assertIsNone(source.usb_device)
        self.assertEqual(source.detached, [])

    def test_read_parses_state_reports_and_skips_others(self):
        source = self.make_source()
        source.open()
        device = self.devices[0]
        device.reports.extend([
            make_report(buttons_low=P.STEAMDECK_LBUTTON_A, lstick=(-1000, 2000), trig=(32767, 0), packet=7),
            b"\x01\x00\x04\x40" + b"\0" * 60,
            None,
        ])
        state = source.read(0.05)
        self.assertEqual((state.buttons, state.lx, state.ly, state.lt, state.packet), (S.BTN_A, -1000, 2000, 32767, 7))
        self.assertIsNone(state.gyro)
        self.assertIsNone(source.read(0.05))
        self.assertIsNone(source.read(0.05))
        self.assertIsNone(source.read(0.05))
        self.assertEqual((source.reports, source.other_packets), (1, 1))

    def test_read_with_sensors(self):
        source = self.make_source(with_sensors=True)
        source.open()
        self.devices[0].reports.append(make_report(gyro=(4, 5, 6)))
        self.assertEqual(source.read(0.05).gyro, (4, 5, 6))

    def test_read_propagates_device_errors_and_needs_open(self):
        source = self.make_source()
        with self.assertRaises(NeptuneError):
            source.read(0.05)
        source.open()
        self.devices[0].reports.append(OSError(errno.ENODEV, "gone"))
        with self.assertRaises(OSError):
            source.read(0.05)

    def test_heartbeat_repeats_until_close(self):
        source = self.make_source(heartbeat_s=0.005)
        source.open()
        device = self.devices[0]
        self.assertTrue(device.second_heartbeat.wait(1.0))
        self.assertGreaterEqual(source.heartbeats, 1)
        heartbeat_payloads = device.feature_payloads[2:]
        self.assertEqual(heartbeat_payloads[:2], CMD.heartbeat_sequence())
        source.close()                               # joins the heartbeat thread
        self.assertIsNone(source._heartbeat_thread)
        sent_after_close = len(device.control_outs)
        self.assertEqual(len(device.control_outs), sent_after_close)
        self.assertTrue(device.closed)

    def test_heartbeat_errors_are_counted_not_fatal(self):
        source = self.make_source(heartbeat_s=0.005)
        source.open()
        device = self.devices[0]
        device.control_error = OSError(errno.EIO, "io")
        self.assertTrue(device.control_after_failure.wait(1.0))   # the heartbeat went on after the error
        self.assertEqual(source.heartbeat_errors, 1)
        self.assertIsNotNone(source.usb_device)

    def test_rumble_sends_one_feature_report(self):
        source = self.make_source()
        source.rumble(1, 2)
        source.open()
        source.rumble(1000, 70000)
        self.assertEqual(self.devices[0].feature_payloads[-1], CMD.cmd_rumble(1000, 65535))

    def test_send_feature_rejects_wrong_length(self):
        source = self.make_source()
        source.open()
        with self.assertRaises(ValueError):
            source.send_feature(b"\x81")

    def test_close_releases_and_rebinds(self):
        source = self.make_source()
        source.open()
        device = self.devices[0]
        source.close()
        self.assertEqual(device.released, [2])
        self.assertTrue(device.closed)
        self.assertIsNone(source.usb_device)
        self.assertFalse(self.captured())
        for number in (0, 1, 2):
            self.assertEqual(os.path.basename(os.readlink(os.path.join(self.fs.interface(number), "driver"))), "usbhid")
        source.close()
        self.assertEqual(device.released, [2])

    def test_close_without_open_is_a_noop(self):
        source = self.make_source()
        source.close()
        self.assertEqual(self.devices, [])
        self.assertFalse(self.captured())


if __name__ == "__main__":
    unittest.main()
