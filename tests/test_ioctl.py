import ctypes
import unittest

import _path  # noqa: F401

from deckgadget.util import ioctl as I


class IocMacroTest(unittest.TestCase):
    def test_usbdevfs_numbers_x86_64(self):
        from deckgadget.sources import neptune_usb as N

        self.assertEqual(ctypes.sizeof(N.UsbfsCtrlTransfer), 24)
        self.assertEqual(ctypes.sizeof(N.UsbfsBulkTransfer), 24)
        self.assertEqual(ctypes.sizeof(N.UsbfsDisconnectClaim), 264)
        self.assertEqual(ctypes.sizeof(N.UsbfsGetDriver), 260)
        self.assertEqual(N.USBDEVFS_CONTROL, 0xC0185500)
        self.assertEqual(N.USBDEVFS_BULK, 0xC0185502)
        self.assertEqual(N.USBDEVFS_CLAIMINTERFACE, 0x8004550F)
        self.assertEqual(N.USBDEVFS_RELEASEINTERFACE, 0x80045510)
        self.assertEqual(N.USBDEVFS_DISCONNECT, 0x5516)
        self.assertEqual(N.USBDEVFS_CONNECT, 0x5517)
        self.assertEqual(N.USBDEVFS_IOCTL, 0xC0105512)
        self.assertEqual(N.USBDEVFS_DISCONNECT_CLAIM, 0x8108551B)
        self.assertEqual(N.USBDEVFS_GETDRIVER, 0x41045508)
        self.assertEqual(N.USBDEVFS_RESET, 0x5514)

    def test_raw_gadget_numbers_match_spike(self):
        from deckgadget.transports import usb_raw_gadget as R

        def ioc(d, t, nr, size):  # the spike's macro
            return (d << 30) | (size << 16) | (ord(t) << 8) | nr

        self.assertEqual(R.USB_RAW_IOCTL_INIT, ioc(1, "U", 0, 257))
        self.assertEqual(R.USB_RAW_IOCTL_RUN, ioc(0, "U", 1, 0))
        self.assertEqual(R.USB_RAW_IOCTL_EVENT_FETCH, ioc(2, "U", 2, 8))
        self.assertEqual(R.USB_RAW_IOCTL_EP0_WRITE, ioc(1, "U", 3, 8))
        self.assertEqual(R.USB_RAW_IOCTL_EP0_READ, ioc(3, "U", 4, 8))
        self.assertEqual(R.USB_RAW_IOCTL_EP_ENABLE, ioc(1, "U", 5, 9))
        self.assertEqual(R.USB_RAW_IOCTL_EP_DISABLE, ioc(1, "U", 6, 4))
        self.assertEqual(R.USB_RAW_IOCTL_EP_WRITE, ioc(1, "U", 7, 8))
        self.assertEqual(R.USB_RAW_IOCTL_EP_READ, ioc(3, "U", 8, 8))
        self.assertEqual(R.USB_RAW_IOCTL_CONFIGURE, ioc(0, "U", 9, 0))
        self.assertEqual(R.USB_RAW_IOCTL_VBUS_DRAW, ioc(1, "U", 10, 4))
        self.assertEqual(R.USB_RAW_IOCTL_EPS_INFO, ioc(2, "U", 11, 960))
        self.assertEqual(R.USB_RAW_IOCTL_EP0_STALL, ioc(0, "U", 12, 0))

    def test_macro_helpers(self):
        self.assertEqual(I.IO("U", 20), 0x5514)
        self.assertEqual(I.IOR("U", 15, 4), 0x8004550F)
        self.assertEqual(I.IOW("U", 6, 4), 0x40045506)
        self.assertEqual(I.IOWR("U", 0, 24), 0xC0185500)
        with self.assertRaises(ValueError):
            I.IOW("U", 0, 1 << 14)

    def test_ioctl_bad_fd_raises_oserror(self):
        with self.assertRaises(OSError) as cm:
            I.ioctl(-1, I.IO("U", 1))
        self.assertEqual(cm.exception.errno, 9)  # EBADF


class RawGadgetIoctlArgTest(unittest.TestCase):
    """raw_gadget.c takes EP_DISABLE's handle and VBUS_DRAW's value (2 mA units) as the ioctl
    argument itself, not through a buffer — a pointer there made every EP_DISABLE fail with EBUSY."""

    def setUp(self):
        self.calls = []
        calls = self.calls

        class FakeLibc:
            @staticmethod
            def ioctl(fd, request, argp):
                calls.append((fd, int(request.value), argp.value if isinstance(argp, ctypes.c_void_p) else argp))
                return 0

        self._saved = I._libc
        I._libc = FakeLibc()

    def tearDown(self):
        I._libc = self._saved

    def _dev(self):
        from deckgadget.transports import usb_raw_gadget as R

        dev = R.RawGadgetDevice.__new__(R.RawGadgetDevice)   # no /dev/raw-gadget here
        dev.path, dev.fd = "fake", 42
        return R, dev

    def test_ep_disable_passes_handle_by_value(self):
        R, dev = self._dev()
        dev.ep_disable(1)
        dev.ep_disable(3)
        self.assertEqual(self.calls, [(42, R.USB_RAW_IOCTL_EP_DISABLE, 1), (42, R.USB_RAW_IOCTL_EP_DISABLE, 3)])

    def test_vbus_draw_passes_2ma_units_by_value(self):
        R, dev = self._dev()
        dev.vbus_draw(500)   # == bMaxPower 0xFA in the config descriptor
        dev.vbus_draw(100)
        self.assertEqual(self.calls, [(42, R.USB_RAW_IOCTL_VBUS_DRAW, 250), (42, R.USB_RAW_IOCTL_VBUS_DRAW, 50)])

    def test_ep_enable_still_passes_a_buffer(self):
        R, dev = self._dev()
        dev.ep_enable(bytes([7, 5, 0x81, 0x03, 0x20, 0x00, 4]))
        fd, req, arg = self.calls[0]
        self.assertEqual((fd, req), (42, R.USB_RAW_IOCTL_EP_ENABLE))
        self.assertIsInstance(arg, int)
        self.assertGreater(arg, 0xFFFF)   # a real address, not a small value


if __name__ == "__main__":
    unittest.main()
