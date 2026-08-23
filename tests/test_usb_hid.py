"""configfs + f_hid transport (``transports/usb_hid.py``) against a fake configfs / sysfs / dev tree.

``/dev/hidg0`` is a FIFO: the transport opens it O_RDWR like the real node, the test reads what the
writer thread pushes out and writes what the host would send in.
"""
import errno
import os
import select
import shutil
import tempfile
import threading
import unittest
from unittest import mock

import _path  # noqa: F401

from deckgadget.profiles.hid_gamepad import REPORT_DESC, HidGamepadProfile
from deckgadget.profiles.xbox360 import Xbox360Profile
from deckgadget.state import ControllerState
from deckgadget.transports import usb_hid
from deckgadget.transports.base import TransportError
from fakes import FakeSysfs, read, write


def read_from_fifo(path, timeout=1.0):
    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
    try:
        readable, _, _ = select.select([fd], [], [], timeout)
        return os.read(fd, 64) if readable else b""
    finally:
        os.close(fd)


def idle(self):
    """Stand-in for a worker loop: exits at once so stop() never waits on select()."""


class UsbHidTransportTest(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="usb_hid_")
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.fs = FakeSysfs(self.root).add_udc()
        os.makedirs(os.path.join(self.fs.configfs, "usb_gadget"))
        self.node = os.path.join(self.fs.dev, "hidg0")
        os.mkfifo(self.node)
        self.profile = HidGamepadProfile()
        self.transport = self.make_transport()

    def make_transport(self, **overrides):
        options = dict(configfs=self.fs.configfs, sysfs=self.fs.sys, dev=self.fs.dev, modprobe=False)
        options.update(overrides)
        transport = usb_hid.UsbHidTransport(**options)
        self.addCleanup(transport.stop)
        return transport

    def start(self, transport=None, writer=False, reader=False, on_feedback=None):
        """Start with only the requested worker loops running for real."""
        transport = transport or self.transport
        patches = []
        if not writer:
            patches.append(mock.patch.object(usb_hid.UsbHidTransport, "_write_loop", idle))
        if not reader:
            patches.append(mock.patch.object(usb_hid.UsbHidTransport, "_read_loop", idle))
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        transport.start(self.profile, on_feedback)
        return transport

    def gadget_file(self, *parts):
        return read(os.path.join(self.transport.gadget_dir, *parts))

    def test_start_builds_the_gadget_tree_and_binds_the_udc_last(self):
        writes = []
        real_write_text, real_write_bytes = usb_hid.write_text, usb_hid.write_bytes

        def spy_text(path, text):
            writes.append(path)
            real_write_text(path, text)

        def spy_bytes(path, data):
            writes.append(path)
            real_write_bytes(path, data)

        with mock.patch.object(usb_hid, "write_text", spy_text), mock.patch.object(usb_hid, "write_bytes", spy_bytes):
            self.start()
        hid = self.profile.hid_function()
        gadget = self.transport.gadget_dir
        self.assertEqual(gadget, os.path.join(self.fs.configfs, "usb_gadget", "deckctl_hid"))
        self.assertEqual(self.gadget_file("idVendor"), f"0x{hid.vid:04x}")
        self.assertEqual(self.gadget_file("idProduct"), f"0x{hid.pid:04x}")
        self.assertEqual(self.gadget_file("bcdDevice"), "0x0100")
        self.assertEqual(self.gadget_file("bcdUSB"), "0x0200")
        self.assertEqual(self.gadget_file("strings", "0x409", "manufacturer"), hid.manufacturer)
        self.assertEqual(self.gadget_file("strings", "0x409", "product"), hid.product)
        self.assertEqual(self.gadget_file("strings", "0x409", "serialnumber"), hid.serial)
        self.assertEqual(self.gadget_file("configs", "c.1", "MaxPower"), "250")
        self.assertEqual(self.gadget_file("configs", "c.1", "strings", "0x409", "configuration"), "Config 1")
        function_dir = os.path.join(gadget, "functions", "hid.usb0")
        self.assertEqual(self.gadget_file("functions", "hid.usb0", "protocol"), "0")
        self.assertEqual(self.gadget_file("functions", "hid.usb0", "subclass"), "0")
        self.assertEqual(self.gadget_file("functions", "hid.usb0", "report_length"), "9")
        with open(os.path.join(function_dir, "report_desc"), "rb") as f:
            self.assertEqual(f.read(), REPORT_DESC)
        self.assertEqual(os.readlink(os.path.join(gadget, "configs", "c.1", "hid.usb0")), function_dir)
        self.assertEqual(self.gadget_file("UDC"), "dwc3.1.auto")
        self.assertEqual(writes[-1], os.path.join(gadget, "UDC"))
        self.assertEqual(self.transport.udc, "dwc3.1.auto")
        self.assertEqual(self.transport.node, self.node)

    def test_explicit_udc_wins_over_detection(self):
        transport = self.make_transport(udc="my.udc")
        self.start(transport)
        self.assertEqual(read(os.path.join(transport.gadget_dir, "UDC")), "my.udc")

    def test_start_without_udc_raises_and_leaves_nothing_behind(self):
        shutil.rmtree(os.path.join(self.fs.sys, "class", "udc"))
        with self.assertRaises(TransportError) as raised:
            self.start()
        self.assertIn("no UDC", str(raised.exception))
        self.assertFalse(os.path.exists(self.transport.gadget_dir))
        self.transport.stop()

    def test_start_without_configfs_raises(self):
        os.rmdir(os.path.join(self.fs.configfs, "usb_gadget"))
        with self.assertRaises(TransportError) as raised:
            self.start()
        self.assertIn("configfs", str(raised.exception))

    def test_start_rejects_profiles_without_a_hid_function(self):
        self.profile = Xbox360Profile()
        with self.assertRaises(TransportError) as raised:
            self.start()
        self.assertIn("transport=raw", str(raised.exception))
        self.assertFalse(os.path.exists(self.transport.gadget_dir))

    def test_start_twice_raises(self):
        self.start()
        with self.assertRaises(TransportError):
            self.transport.start(self.profile)

    def test_start_replaces_a_stale_gadget(self):
        stale = self.fs.add_gadget("deckctl_hid")
        write(os.path.join(stale, "idVendor"), "0xdead")
        with self.assertLogs(usb_hid.log, level="WARNING") as logs:
            self.start()
        self.assertIn("stale gadget", logs.output[0])
        self.assertEqual(self.gadget_file("idVendor"), "0x1d6b")
        self.assertTrue(os.path.exists(os.path.join(self.transport.gadget_dir, "functions", "hid.usb0", "report_desc")))

    def test_start_removes_the_gadget_when_no_node_appears(self):
        with mock.patch.object(usb_hid.UsbHidTransport, "_find_hidg_node", side_effect=TransportError("no node")):
            with self.assertRaises(TransportError):
                self.start()
        self.assertFalse(os.path.exists(self.transport.gadget_dir))
        self.assertEqual(self.transport._fd, -1)

    def test_find_hidg_node_takes_the_first_node_without_a_dev_attribute(self):
        write(os.path.join(self.fs.dev, "hidg1"), "")
        self.assertEqual(self.transport._find_hidg_node(timeout=0.06), self.node)

    def test_find_hidg_node_times_out_without_a_matching_node(self):
        os.unlink(self.node)
        with self.assertRaises(TransportError):
            self.transport._find_hidg_node(timeout=0.06)
        write(os.path.join(self.transport.gadget_dir, "functions", "hid.usb0", "dev"), "240:7\n")
        write(os.path.join(self.fs.dev, "hidg0"), "")
        with self.assertRaises(TransportError):
            self.transport._find_hidg_node(timeout=0.06)

    def test_send_writes_the_report_to_the_node(self):
        self.start(writer=True)
        report = self.profile.pack(ControllerState(buttons=1, lx=1000))
        self.transport.send(report)
        self.assertEqual(read_from_fifo(self.node), report)
        self.transport.stop()                       # joins the writer: its counters are final
        self.assertEqual(self.transport.metrics().sent, 1)

    def test_send_keeps_only_the_newest_unsent_report(self):
        self.start()
        self.transport.send(b"\x01" * 9)
        self.transport.send(b"\x02" * 9)
        self.assertEqual(self.transport.metrics().dropped, 1)
        self.assertEqual(self.transport._slot.take(0), b"\x02" * 9)

    def test_eshutdown_drops_the_report_and_keeps_writing(self):
        self.start(writer=True)
        real_write = os.write
        failures = []
        failed = threading.Event()

        def write_not_configured_once(fd, data):
            if fd == self.transport._fd and not failures:
                failures.append(data)
                failed.set()
                raise OSError(errno.ESHUTDOWN, "not configured")
            return real_write(fd, data)

        with mock.patch.object(usb_hid.os, "write", write_not_configured_once):
            self.transport.send(b"\x01" * 9)
            self.assertTrue(failed.wait(1.0))
            self.transport.send(b"\x02" * 9)
            self.assertEqual(read_from_fifo(self.node), b"\x02" * 9)
        self.transport.stop()
        self.assertEqual((self.transport.metrics().sent, self.transport.metrics().errors), (1, 0))

    def test_host_output_reports_reach_the_feedback_callback(self):
        feedbacks = []
        received = threading.Event()

        def on_feedback(feedback):
            feedbacks.append(feedback)
            received.set()

        self.start(reader=True, on_feedback=on_feedback)
        fd = os.open(self.node, os.O_WRONLY | os.O_NONBLOCK)
        try:
            os.write(fd, b"\x01\x02")
        finally:
            os.close(fd)
        self.assertTrue(received.wait(1.0))
        self.assertEqual((feedbacks[0].kind, feedbacks[0].raw), ("unknown", b"\x01\x02"))
        self.assertEqual(self.transport.metrics().out_reports, 1)

    def test_connected_follows_the_udc_state(self):
        self.assertFalse(self.transport.connected())
        self.start()
        self.assertFalse(self.transport.connected())
        self.fs.set_udc_state("configured")
        self.assertTrue(self.transport.connected())
        self.fs.set_udc_state("not attached")
        self.assertFalse(self.transport.connected())

    def test_stop_tears_down_and_is_idempotent(self):
        self.start()
        fd = self.transport._fd
        self.assertGreaterEqual(fd, 0)
        self.transport.stop()
        self.assertFalse(os.path.exists(self.transport.gadget_dir))
        self.assertEqual(self.transport._fd, -1)
        with self.assertRaises(OSError):
            os.fstat(fd)
        self.transport.stop()
        self.assertFalse(os.path.exists(self.transport.gadget_dir))

    def test_stop_before_start_is_a_no_op(self):
        self.transport.stop()
        self.assertFalse(os.path.exists(self.transport.gadget_dir))


if __name__ == "__main__":
    unittest.main()
