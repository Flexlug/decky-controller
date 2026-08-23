"""raw-gadget transport: EP0 handling, SET_CONFIGURATION lifecycle, IN/OUT data paths — against a
scripted stand-in for ``/dev/raw-gadget`` (no kernel involved)."""
import errno
import logging
import os
import queue
import shutil
import struct
import tempfile
import threading
import unittest
from unittest import mock

import _path  # noqa: F401

from deckgadget.profiles import hid_gamepad as H
from deckgadget.profiles import xbox360 as X
from deckgadget.profiles.base import Feedback, USB_DT_HID_REPORT
from deckgadget.state import ControllerState
from deckgadget.transports import base as B
from deckgadget.platform.rawgadget import ioctls as IOC
from deckgadget.transports.rawgadget import transport as R
from fakes import FakeSysfs

GET_DESCRIPTOR_IN = 0x80
SET_CONFIGURATION = 9
GET_CONFIGURATION = 8
GET_STATUS = 0
CLEAR_FEATURE = 1
SET_FEATURE = 3
GET_INTERFACE = 10
SET_INTERFACE = 11
INTERFACE_IN = 0x81
INTERFACE_OUT = 0x01


def setup_packet(request_type, request, value=0, index=0, length=0):
    return struct.pack("<BBHHH", request_type, request, value, index, length)


def interrupted():
    return OSError(errno.EINTR, "Interrupted system call")


class FakeRawGadgetDevice:
    """Records every ioctl-level call; ``event_fetch`` / ``ep_read`` serve scripted queues and report
    EINTR when idle, exactly like the real device does after the cancel signal."""

    IDLE_TIMEOUT = 0.02

    def __init__(self, path="fake"):
        self.path = path
        self.fd = 42
        self.calls = []
        self.events = queue.Queue()
        self.out_packets = queue.Queue()
        self.next_handle = 1
        self.enable_errors = []
        self.fetch_error = None
        self.closed = False
        self._changed = threading.Condition()

    def _record(self, name, *args):
        with self._changed:
            self.calls.append((name, *args))
            self._changed.notify_all()

    def wait_for(self, predicate, timeout=1.0):
        with self._changed:
            return self._changed.wait_for(lambda: predicate(self.calls), timeout)

    def calls_named(self, name):
        return [call for call in self.calls if call[0] == name]

    def close(self):
        self._record("close")
        self.closed = True

    def init(self, driver, device, speed):
        self._record("init", driver, device, speed)

    def run(self):
        self._record("run")

    def event_fetch(self, max_length=256):
        if self.fetch_error is not None:
            error, self.fetch_error = self.fetch_error, None
            raise error
        try:
            return self.events.get(timeout=self.IDLE_TIMEOUT)
        except queue.Empty:
            raise interrupted() from None

    def ep0_write(self, data):
        self._record("ep0_write", bytes(data))
        return len(data)

    def ep0_read(self, length):
        self._record("ep0_read", length)
        return b"\0" * length

    def ep0_stall(self):
        self._record("ep0_stall")

    def ep_enable(self, endpoint_descriptor):
        if self.enable_errors:
            error = self.enable_errors.pop(0)
            if error is not None:
                raise error
        handle, self.next_handle = self.next_handle, self.next_handle + 1
        self._record("ep_enable", bytes(endpoint_descriptor), handle)
        return handle

    def ep_disable(self, handle):
        self._record("ep_disable", handle)

    def ep_read(self, handle, length):
        try:
            return self.out_packets.get(timeout=self.IDLE_TIMEOUT)
        except queue.Empty:
            raise interrupted() from None

    def configure(self):
        self._record("configure")

    def vbus_draw(self, milliamps):
        self._record("vbus_draw", milliamps)

    def eps_info(self):
        self._record("eps_info")
        return []


class FastSlot(B.ReportSlot):
    """The IN writer polls the slot with a 0.25 s timeout; cap it so teardown joins quickly in tests."""

    def take(self, timeout):
        return super().take(min(timeout, 0.02))


def collecting_callback(expected):
    """A list that sets ``seen`` once ``expected`` items were appended (for thread-delivered feedback)."""
    seen = threading.Event()

    class Collector(list):
        def append(self, item):
            super().append(item)
            if len(self) >= expected:
                seen.set()

    return Collector(), seen


def fake_ep_write_ioctl(device):
    """Stand-in for the module-level ``ioctl`` used by the IN writer: decodes ``struct usb_raw_ep_io``."""

    def fake_ioctl(fd, request, arg=0):
        if request != IOC.USB_RAW_IOCTL_EP_WRITE:
            raise AssertionError(f"unexpected ioctl 0x{request:x}")
        handle, _flags, length = struct.unpack_from("<HHI", arg.raw, 0)
        device._record("ep_write", fd, handle, bytes(arg.raw[IOC.SZ_EP_IO:IOC.SZ_EP_IO + length]))
        return length

    return fake_ioctl


class ReportSlotTest(unittest.TestCase):
    def test_newest_wins_and_drops_are_counted(self):
        slot = B.ReportSlot()
        self.assertFalse(slot.put(b"first"))
        self.assertTrue(slot.put(b"second"))
        self.assertEqual(slot.dropped, 1)
        self.assertEqual(slot.take(0.0), b"second")
        self.assertIsNone(slot.take(0.0))

    def test_take_waits_for_a_report(self):
        slot = B.ReportSlot()
        threading.Timer(0.01, slot.put, args=(b"late",)).start()
        self.assertEqual(slot.take(1.0), b"late")

    def test_clear_discards_without_counting(self):
        slot = B.ReportSlot()
        slot.put(b"x")
        slot.clear()
        self.assertIsNone(slot.take(0.0))
        self.assertEqual(slot.dropped, 0)

    def test_metrics_as_dict(self):
        self.assertEqual(B.TransportMetrics(sent=3, dropped=1, errors=2, out_reports=4).as_dict(),
                         {"sent": 3, "dropped": 1, "errors": 2, "out_reports": 4})


class ControlHandlingTest(unittest.TestCase):
    """Drives ``_handle_control`` directly (no event thread) and asserts what reaches the device."""

    def setUp(self):
        B.install_cancel_signal_handler()
        self.device = FakeRawGadgetDevice()
        patcher = mock.patch.object(R, "ioctl", fake_ep_write_ioctl(self.device))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.transport = None

    def tearDown(self):
        if self.transport is not None:
            self.transport.stop()

    def make(self, profile, speed="high"):
        transport = R.UsbRawGadgetTransport(udc="dwc3.1.auto", speed=speed, log_control=False)
        transport.device = self.device
        transport.profile = profile
        transport.descriptors = profile.gadget_descriptors()
        transport._slot = FastSlot()
        self.transport = transport
        return transport

    def control(self, request_type, request, value=0, index=0, length=0):
        self.transport._handle_control(setup_packet(request_type, request, value, index, length))
        return self.device.calls[-1] if self.device.calls else None

    def test_device_descriptor_full_and_truncated(self):
        transport = self.make(X.Xbox360Profile())
        descriptor = transport.descriptors.device_descriptor()
        self.assertEqual(self.control(GET_DESCRIPTOR_IN, 6, 0x0100, 0, 18), ("ep0_write", descriptor))
        self.assertEqual(self.control(GET_DESCRIPTOR_IN, 6, 0x0100, 0, 8), ("ep0_write", descriptor[:8]))

    def test_configuration_and_other_speed_descriptors(self):
        transport = self.make(X.Xbox360Profile())
        config = transport.descriptors.config_descriptor()
        self.assertEqual(self.control(GET_DESCRIPTOR_IN, 6, 0x0200, 0, 0x99), ("ep0_write", config))
        self.assertEqual(self.control(GET_DESCRIPTOR_IN, 6, 0x0200, 0, 9), ("ep0_write", config[:9]))
        other = self.control(GET_DESCRIPTOR_IN, 6, 0x0700, 0, 0x99)
        self.assertEqual(other[1][1], 7)
        self.assertEqual(other[1][2:], config[2:])

    def test_string_descriptors(self):
        transport = self.make(X.Xbox360Profile())
        self.assertEqual(self.control(GET_DESCRIPTOR_IN, 6, 0x0300, 0, 255), ("ep0_write", bytes([4, 3, 0x09, 0x04])))
        self.assertEqual(self.control(GET_DESCRIPTOR_IN, 6, 0x0302, 0x0409, 255),
                         ("ep0_write", transport.descriptors.string(2)))
        self.assertEqual(self.control(GET_DESCRIPTOR_IN, 6, 0x0309, 0x0409, 255), ("ep0_stall",))

    def test_qualifier_only_at_high_speed(self):
        transport = self.make(X.Xbox360Profile())
        self.assertEqual(self.control(GET_DESCRIPTOR_IN, 6, 0x0600, 0, 10),
                         ("ep0_write", transport.descriptors.qualifier_descriptor()))
        self.transport.stop()
        self.device = FakeRawGadgetDevice()
        self.make(X.Xbox360Profile(), speed="full")
        self.assertEqual(self.control(GET_DESCRIPTOR_IN, 6, 0x0600, 0, 10), ("ep0_stall",))
        self.assertEqual(self.control(GET_DESCRIPTOR_IN, 6, 0x0700, 0, 9), ("ep0_stall",))

    def test_unknown_descriptors_are_stalled(self):
        self.make(X.Xbox360Profile())
        self.assertEqual(self.control(GET_DESCRIPTOR_IN, 6, 0x0F00, 0, 5), ("ep0_stall",))
        self.assertEqual(self.control(GET_DESCRIPTOR_IN, 6, 0x03EE, 0, 18), ("ep0_stall",))

    def test_device_status_configuration_and_features(self):
        transport = self.make(H.HidGamepadProfile())
        self.assertEqual(self.control(GET_DESCRIPTOR_IN, GET_STATUS, 0, 0, 2), ("ep0_write", b"\x00\x00"))
        self.assertEqual(self.control(GET_DESCRIPTOR_IN, GET_CONFIGURATION, 0, 0, 1), ("ep0_write", b"\x00"))
        self.assertEqual(self.control(0x00, SET_FEATURE, 1, 0, 0), ("ep0_read", 0))
        self.assertEqual(self.control(0x00, CLEAR_FEATURE, 1, 0, 0), ("ep0_read", 0))
        self.assertEqual(self.control(0x00, 7, 0, 0, 0), ("ep0_stall",))
        self.control(0x00, SET_CONFIGURATION, 1)
        self.assertTrue(transport.connected())
        self.assertEqual(self.control(GET_DESCRIPTOR_IN, GET_CONFIGURATION, 0, 0, 1), ("ep0_write", b"\x01"))

    def test_interface_recipient_requests(self):
        self.make(H.HidGamepadProfile())
        self.assertEqual(self.control(INTERFACE_IN, GET_INTERFACE, 0, 0, 1), ("ep0_write", b"\x00"))
        self.assertEqual(self.control(INTERFACE_IN, GET_STATUS, 0, 0, 2), ("ep0_write", b"\x00\x00"))
        self.assertEqual(self.control(INTERFACE_OUT, SET_INTERFACE, 0, 0, 0), ("ep0_read", 0))
        self.assertEqual(self.control(0x82, GET_STATUS, 0, 0x81, 2), ("ep0_write", b"\x00\x00"))

    def test_set_configuration_enables_endpoints_then_tears_them_down(self):
        transport = self.make(X.Xbox360Profile())
        self.control(0x00, SET_CONFIGURATION, 1)
        self.assertEqual(self.device.calls_named("ep_enable"),
                         [("ep_enable", X.EP_IN_DESC, 1), ("ep_enable", X.EP_OUT_DESC, 2)])
        self.assertEqual(self.device.calls_named("vbus_draw"), [("vbus_draw", 500)])
        self.assertEqual(self.device.calls_named("configure"), [("configure",)])
        self.assertEqual(self.device.calls[-1], ("ep0_read", 0))
        self.assertTrue(transport.connected())
        self.assertTrue(transport._in_thread.is_alive() and transport._out_thread.is_alive())
        self.control(0x00, SET_CONFIGURATION, 0)
        self.assertEqual(self.device.calls_named("ep_disable"), [("ep_disable", 1), ("ep_disable", 2)])
        self.assertFalse(transport.connected())
        self.assertIsNone(transport._in_thread)

    def test_reconfigure_disables_old_handles_first(self):
        transport = self.make(H.HidGamepadProfile())
        self.control(0x00, SET_CONFIGURATION, 1)
        self.control(0x00, SET_CONFIGURATION, 1)
        names = [call[0] for call in self.device.calls if call[0] in ("ep_enable", "ep_disable")]
        self.assertEqual(names, ["ep_enable", "ep_enable", "ep_disable", "ep_disable", "ep_enable", "ep_enable"])
        self.assertEqual(transport.generation, 2)
        self.assertTrue(transport.connected())

    def test_failed_endpoint_enable_rolls_back_and_propagates(self):
        transport = self.make(X.Xbox360Profile())
        self.device.enable_errors = [None, OSError(errno.EBUSY, "busy")]
        with self.assertRaises(OSError):
            transport._set_configuration(1)
        self.assertEqual(self.device.calls_named("ep_disable"), [("ep_disable", 1)])
        self.assertFalse(transport.connected())
        self.assertEqual(self.device.calls_named("configure"), [])

    def test_xbox_vendor_requests_are_delegated_to_the_profile(self):
        self.make(X.Xbox360Profile())
        self.assertEqual(self.control(0xC1, 0x01, 0x0100, 0, 20), ("ep0_write", b""))
        self.assertEqual(self.control(0x41, 0x01, 0, 0, 4), ("ep0_read", 4))
        self.assertEqual(self.control(0x41, 0x01, 0, 0, 0), ("ep0_read", 0))

    def test_hid_class_requests_are_delegated_to_the_profile(self):
        transport = self.make(H.HidGamepadProfile())
        self.assertEqual(self.control(INTERFACE_IN, 6, USB_DT_HID_REPORT << 8, 0, len(H.REPORT_DESC)),
                         ("ep0_write", H.REPORT_DESC))
        self.assertEqual(self.control(0x21, 0x0A, 0x0000, 0, 0), ("ep0_read", 0))
        self.assertEqual(self.control(0xA1, 0x01, 0x0100, 0, 9),
                         ("ep0_write", transport.profile.pack(ControllerState())))
        self.assertEqual(self.control(0x21, 0x09, 0x0200, 0, 2), ("ep0_read", 2))
        self.assertEqual(self.control(0xC1, 0x01, 0, 0, 2), ("ep0_stall",))

    def test_reports_are_written_to_the_in_endpoint_only_once_configured(self):
        transport = self.make(X.Xbox360Profile())
        transport.send(b"too early")
        self.control(0x00, SET_CONFIGURATION, 1)
        report = transport.profile.pack(ControllerState())
        transport.send(report)
        self.assertTrue(self.device.wait_for(lambda calls: any(call[0] == "ep_write" for call in calls)))
        self.assertEqual(self.device.calls_named("ep_write"), [("ep_write", 42, 1, report)])
        self.assertEqual(transport.metrics().sent, 1)

    def test_out_reports_reach_the_feedback_callback(self):
        feedback, seen = collecting_callback(expected=2)
        transport = self.make(X.Xbox360Profile())
        transport.on_feedback = feedback.append
        self.control(0x00, SET_CONFIGURATION, 1)
        self.device.out_packets.put(bytes.fromhex("0008008040000000"))
        self.device.out_packets.put(bytes.fromhex("010302"))
        self.assertTrue(seen.wait(1.0))
        self.assertEqual([(item.kind, item.left, item.right, item.value) for item in feedback],
                         [("rumble", 0x80 * 257, 0x40 * 257, 0), ("led", 0, 0, 2)])
        self.assertEqual(transport.metrics().out_reports, 2)


class LifecycleTest(unittest.TestCase):
    """``start`` / event loop / ``stop`` with the device class and the IN-writer ioctl replaced."""

    def setUp(self):
        B.install_cancel_signal_handler()
        R.log.setLevel(logging.CRITICAL)
        self.addCleanup(R.log.setLevel, logging.NOTSET)
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.fs = FakeSysfs(self.tmp).add_udc(state="configured")
        self.dev_path = os.path.join(self.tmp, "raw-gadget")
        open(self.dev_path, "w").close()
        self.device = FakeRawGadgetDevice()
        for patcher in (mock.patch.object(R, "RawGadgetDevice", lambda path: self.device),
                        mock.patch.object(R, "ioctl", fake_ep_write_ioctl(self.device))):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.transport = None

    def tearDown(self):
        if self.transport is not None:
            self.transport.stop()

    def make(self, **kwargs):
        options = dict(dev_path=self.dev_path, sysfs=self.fs.sys, modprobe=False, log_control=False)
        options.update(kwargs)
        self.transport = R.UsbRawGadgetTransport(**options)
        self.transport._slot = FastSlot()
        return self.transport

    def started(self, profile=None, on_feedback=None, **kwargs):
        transport = self.make(**kwargs)
        transport.start(profile or X.Xbox360Profile(), on_feedback)
        return transport

    def configure(self):
        self.device.events.put((IOC.USB_RAW_EVENT_CONTROL, setup_packet(0x00, SET_CONFIGURATION, 1)))
        self.assertTrue(self.device.wait_for(lambda calls: ("configure",) in calls))
        self.assertTrue(self.device.wait_for(lambda calls: calls[-1] == ("ep0_read", 0)))

    def test_start_initialises_and_runs_the_gadget_on_the_detected_udc(self):
        transport = self.started()
        self.assertEqual(transport.udc, "dwc3.1.auto")
        self.assertEqual(self.device.calls[:2], [("init", "dwc3-gadget", "dwc3.1.auto", 3), ("run",)])
        self.assertTrue(transport._event_thread.is_alive())
        self.assertFalse(transport.connected())
        with self.assertRaises(B.TransportError):
            transport.start(X.Xbox360Profile())

    def test_explicit_udc_and_speed_are_passed_through(self):
        self.started(udc="other.udc", speed="full", driver="dummy")
        self.assertEqual(self.device.calls[0], ("init", "dummy", "other.udc", 2))

    def test_start_fails_without_udc_or_device_node(self):
        shutil.rmtree(os.path.join(self.fs.sys, "class", "udc"))
        with self.assertRaises(B.TransportError):
            self.make().start(X.Xbox360Profile())
        self.fs.add_udc()
        os.unlink(self.dev_path)
        with self.assertRaises(B.TransportError):
            self.make().start(X.Xbox360Profile())
        self.assertEqual(self.device.calls, [])
        with self.assertRaises(B.TransportError):
            R.UsbRawGadgetTransport(speed="warp")

    def test_connect_and_control_events_are_answered(self):
        transport = self.started()
        self.device.events.put((IOC.USB_RAW_EVENT_CONNECT, b""))
        self.device.events.put((IOC.USB_RAW_EVENT_CONTROL, setup_packet(GET_DESCRIPTOR_IN, 6, 0x0100, 0, 18)))
        self.assertTrue(self.device.wait_for(lambda calls: any(call[0] == "ep0_write" for call in calls)))
        self.assertIn(("eps_info",), self.device.calls)
        self.assertEqual(self.device.calls_named("ep0_write"), [("ep0_write", transport.descriptors.device_descriptor())])
        self.assertEqual(transport.control_requests, 1)

    def test_set_configuration_then_disconnect(self):
        transport = self.started()
        self.configure()
        self.assertTrue(transport.connected())
        self.fs.set_udc_state("not attached")
        self.assertFalse(transport.connected())
        self.fs.set_udc_state("configured")
        self.device.events.put((IOC.USB_RAW_EVENT_DISCONNECT, b""))
        self.assertTrue(self.device.wait_for(lambda calls: ("ep_disable", 2) in calls))
        self.assertEqual(self.device.calls_named("ep_disable"), [("ep_disable", 1), ("ep_disable", 2)])
        self.assertFalse(transport.connected())

    def test_reset_tears_endpoints_down(self):
        transport = self.started(profile=H.HidGamepadProfile())
        self.configure()
        self.device.events.put((IOC.USB_RAW_EVENT_RESET, b""))
        self.assertTrue(self.device.wait_for(lambda calls: ("ep_disable", 2) in calls))
        self.assertFalse(transport.connected())

    def test_sent_report_reaches_the_in_endpoint_and_feedback_flows_back(self):
        feedback, seen = collecting_callback(expected=1)
        transport = self.started(on_feedback=feedback.append)
        self.configure()
        report = transport.profile.pack(ControllerState(buttons=1))
        transport.send(report)
        self.assertTrue(self.device.wait_for(lambda calls: ("ep_write", 42, 1, report) in calls))
        self.device.out_packets.put(bytes.fromhex("0008001020000000"))
        self.assertTrue(seen.wait(1.0))
        self.assertEqual(feedback[0], Feedback("rumble", left=0x10 * 257, right=0x20 * 257,
                                               raw=bytes.fromhex("0008001020000000")))

    def test_control_failure_stalls_ep0_and_keeps_the_loop_alive(self):
        transport = self.started()
        transport.descriptors = mock.Mock(wraps=transport.descriptors)
        transport.descriptors.device_descriptor.side_effect = OSError(errno.EIO, "io")
        self.device.events.put((IOC.USB_RAW_EVENT_CONTROL, setup_packet(GET_DESCRIPTOR_IN, 6, 0x0100, 0, 18)))
        self.assertTrue(self.device.wait_for(lambda calls: ("ep0_stall",) in calls))
        self.assertIsNone(transport.error)
        self.assertTrue(transport._event_thread.is_alive())

    def test_unexpected_exception_records_error_and_ends_the_loop(self):
        transport = self.started()
        transport.profile = mock.Mock(wraps=transport.profile)
        transport.profile.handle_control.side_effect = RuntimeError("boom")
        self.device.events.put((IOC.USB_RAW_EVENT_CONTROL, setup_packet(0xC1, 0x01, 0, 0, 2)))
        self.assertTrue(self.device.wait_for(lambda calls: ("ep0_stall",) in calls))
        transport._event_thread.join(1.0)
        self.assertFalse(transport._event_thread.is_alive())
        self.assertIsInstance(transport.error, RuntimeError)

    def test_event_fetch_failure_is_fatal(self):
        transport = self.started()
        self.configure()
        self.device.fetch_error = OSError(errno.ENODEV, "gone")
        transport._event_thread.join(1.0)
        self.assertFalse(transport._event_thread.is_alive())
        self.assertIsInstance(transport.error, B.TransportError)
        self.assertFalse(transport.connected())

    def test_stop_disables_endpoints_closes_once_and_is_idempotent(self):
        transport = self.started()
        self.configure()
        transport.stop()
        self.assertEqual(self.device.calls_named("ep_disable"), [("ep_disable", 1), ("ep_disable", 2)])
        self.assertEqual(self.device.calls_named("close"), [("close",)])
        self.assertIsNone(transport.device)
        self.assertFalse(transport.connected())
        transport.stop()
        self.assertEqual(self.device.calls_named("close"), [("close",)])
        self.make().stop()


if __name__ == "__main__":
    unittest.main()
