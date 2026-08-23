import struct
import unittest

import _path  # noqa: F401

from deckgadget import state as S
from deckgadget.profiles import make_profile
from deckgadget.profiles import xbox360 as X
from deckgadget.profiles import hid_gamepad as H
from deckgadget.profiles.base import SetupPacket, USB_DT_HID_REPORT
from deckgadget.state import ControllerState


class Xbox360PackTest(unittest.TestCase):
    def setUp(self):
        self.p = X.Xbox360Profile()

    def test_neutral_report(self):
        rep = self.p.pack(ControllerState())
        self.assertEqual(len(rep), 20)
        self.assertEqual(rep, bytes.fromhex("0014" + "00" * 18))

    def test_known_vector_buttons_and_axes(self):
        st = ControllerState(buttons=S.BTN_A | S.BTN_L1 | S.BTN_DPAD_UP | S.BTN_MENU,
                             lx=1000, ly=-2000, rx=32767, ry=-32768, lt=S.TRIGGER_MAX, rt=S.TRIGGER_MAX // 2)
        rep = self.p.pack(st)
        self.assertEqual(rep[:2], b"\x00\x14")
        buttons = struct.unpack_from("<H", rep, 2)[0]
        self.assertEqual(buttons, X.XB_A | X.XB_LB | X.XB_DPAD_UP | X.XB_START)
        self.assertEqual(rep[4], 255)        # LT full
        self.assertEqual(rep[5], 127)        # RT half
        lx, ly, rx, ry = struct.unpack_from("<hhhh", rep, 6)
        self.assertEqual((lx, ly, rx, ry), (1000, -2000, 32767, -32768))   # +Y = up, no inversion
        self.assertEqual(rep[14:], b"\x00" * 6)

    def test_known_vector_a_and_left_stick(self):
        rep = self.p.pack(ControllerState(buttons=S.BTN_A, lx=30000))
        self.assertEqual(rep, struct.pack("<BBHBBhhhh6x", 0, 0x14, 0x1000, 0, 0, 30000, 0, 0, 0))

    def test_steam_qam_not_forwarded_by_default(self):
        rep = self.p.pack(ControllerState(buttons=S.BTN_STEAM | S.BTN_QAM | S.BTN_L4 | S.BTN_R5))
        self.assertEqual(struct.unpack_from("<H", rep, 2)[0], 0)
        p2 = X.Xbox360Profile(forward_steam=True)
        rep = p2.pack(ControllerState(buttons=S.BTN_STEAM))
        self.assertEqual(struct.unpack_from("<H", rep, 2)[0], X.XB_GUIDE)

    def test_paddle_mapping(self):
        p = X.Xbox360Profile(paddles={"L4": "A", "R4": "DPAD_LEFT", "L5": "none", "R5": "RB"})
        rep = p.pack(ControllerState(buttons=S.BTN_L4 | S.BTN_R4 | S.BTN_L5 | S.BTN_R5))
        self.assertEqual(struct.unpack_from("<H", rep, 2)[0], X.XB_A | X.XB_DPAD_LEFT | X.XB_RB)
        with self.assertRaises(ValueError):
            X.Xbox360Profile(paddles={"L4": "BOGUS"})

    def test_clamping(self):
        rep = self.p.pack(ControllerState(lx=99999, ly=-99999, lt=-5, rt=99999))
        lx, ly = struct.unpack_from("<hh", rep, 6)
        self.assertEqual((lx, ly), (32767, -32768))
        self.assertEqual((rep[4], rep[5]), (0, 255))

    def test_trigger_scaling(self):
        self.assertEqual(X.trigger_to_u8(0), 0)
        self.assertEqual(X.trigger_to_u8(32767), 255)
        self.assertEqual(X.trigger_to_u8(16384), 128)


class Xbox360DescriptorTest(unittest.TestCase):
    def test_descriptors(self):
        d = X.DESCRIPTORS
        dev = d.device_descriptor()
        self.assertEqual(len(dev), 18)
        self.assertEqual(dev, struct.pack("<BBHBBBBHHHBBBB", 18, 1, 0x0200, 0xFF, 0xFF, 0xFF, 64, 0x045E, 0x028E,
                                          0x0114, 1, 2, 3, 1))
        cfg = d.config_descriptor()
        self.assertEqual(len(cfg), 0x99)
        self.assertEqual(cfg[:9], struct.pack("<BBHBBBBB", 9, 2, 0x99, 4, 1, 0, 0xA0, 0xFA))
        # interface 0 vendor descriptor exactly as a real wired Xbox 360 pad reports it
        self.assertIn(bytes.fromhex("1121000101258114000000001301080000"), cfg)
        other = d.config_descriptor(7)
        self.assertEqual(other[1], 7)
        self.assertEqual(len(d.qualifier_descriptor()), 10)
        self.assertEqual(d.string(0), bytes([4, 3, 0x09, 0x04]))
        self.assertEqual(d.string(2), bytes([2 + 2 * len("Controller"), 3]) + "Controller".encode("utf-16-le"))
        self.assertIsNotNone(d.string(4))
        self.assertIsNone(d.string(9))
        self.assertEqual(d.ep_in_address, 0x81)
        self.assertEqual(d.ep_in_max_packet, 32)
        self.assertEqual(d.ep_out_max_packet, 32)

    def test_on_output(self):
        p = X.Xbox360Profile()
        fb = p.on_output(bytes.fromhex("0008008040000000"))
        self.assertEqual((fb.kind, fb.left, fb.right), ("rumble", 0x80 * 257, 0x40 * 257))
        fb = p.on_output(bytes.fromhex("010302"))
        self.assertEqual((fb.kind, fb.value), ("led", 2))
        fb = p.on_output(bytes.fromhex("020803"))
        self.assertEqual(fb.kind, "unknown")
        self.assertIsNone(p.on_output(b"\x00"))

    def test_handle_control(self):
        p = X.Xbox360Profile()
        # Windows capability query 0xc1/0x01 -> empty reply
        setup = SetupPacket(0xC1, 0x01, 0x0100, 0, 20)
        self.assertEqual(p.handle_control(setup, lambda: b""), b"")
        consumed = []
        setup = SetupPacket(0x41, 0x01, 0, 0, 4)
        self.assertEqual(p.handle_control(setup, lambda: consumed.append(1) or b"\0\0\0\0"), b"")
        self.assertEqual(consumed, [1])
        # unknown standard interface request -> stall
        self.assertIsNone(p.handle_control(SetupPacket(0x81, 0x06, 0x2200, 0, 9), lambda: b""))
        self.assertIsNone(p.hid_function())


class HidGamepadTest(unittest.TestCase):
    def setUp(self):
        self.p = H.HidGamepadProfile()

    def test_neutral(self):
        rep = self.p.pack(ControllerState())
        self.assertEqual(len(rep), 9)
        self.assertEqual(rep, struct.pack("<bbbbbbBH", 0, 0, 0, 0, 0, 0, H.HAT_NULL, 0))

    def test_known_vector_a_and_x_axis(self):
        rep = self.p.pack(ControllerState(buttons=S.BTN_A, lx=100 << 8))
        self.assertEqual(rep, struct.pack("<bbbbbbBH", 100, 0, 0, 0, 0, 0, 0x08, 1))

    def test_axes_and_hat(self):
        st = ControllerState(buttons=S.BTN_DPAD_UP | S.BTN_DPAD_RIGHT, lx=32767, ly=32767, rx=-32768, ry=-32768,
                             lt=S.TRIGGER_MAX, rt=0)
        x, y, z, rz, rx, ry, hat, btn = struct.unpack("<bbbbbbBH", self.p.pack(st))
        self.assertEqual((x, y), (127, -127))       # HID +Y = down -> Deck up becomes -127
        self.assertEqual((z, rz), (-127, 127))
        self.assertEqual((rx, ry), (127, 0))
        self.assertEqual(hat, 1)                     # up-right
        self.assertEqual(btn, 0)
        self.assertEqual(H.hat_from_buttons(S.BTN_DPAD_DOWN | S.BTN_DPAD_LEFT), 5)
        self.assertEqual(H.hat_from_buttons(S.BTN_DPAD_UP | S.BTN_DPAD_DOWN), H.HAT_NULL)

    def test_buttons_and_paddles(self):
        rep = self.p.pack(ControllerState(buttons=S.BTN_B | S.BTN_R1 | S.BTN_MENU | S.BTN_L4 | S.BTN_STEAM))
        btn = struct.unpack_from("<H", rep, 7)[0]
        self.assertEqual(btn, (1 << 1) | (1 << 5) | (1 << 9) | (1 << 12))   # B, R1, MENU, L4 (own button 13), no STEAM
        p = H.HidGamepadProfile(paddles={"L4": "A", "R4": "DPAD_DOWN"})
        rep = p.pack(ControllerState(buttons=S.BTN_L4 | S.BTN_R4))
        btn = struct.unpack_from("<H", rep, 7)[0]
        self.assertEqual(btn, 1)
        self.assertEqual(rep[6], 4)   # hat down

    def test_descriptors_and_control(self):
        self.assertEqual(len(H.REPORT_DESC), 75)
        hf = self.p.hid_function()
        self.assertEqual((hf.report_length, hf.protocol, hf.subclass), (9, 0, 0))
        self.assertEqual(hf.report_desc, H.REPORT_DESC)
        d = self.p.gadget_descriptors()
        cfg = d.config_descriptor()
        self.assertEqual(len(cfg), 9 + 9 + 9 + 7 + 7)
        self.assertEqual(struct.unpack_from("<H", cfg, 2)[0], len(cfg))
        self.assertEqual(struct.unpack_from("<H", H.HID_DESC, 7)[0], len(H.REPORT_DESC))
        # GET_DESCRIPTOR(report) at the interface
        out = self.p.handle_control(SetupPacket(0x81, 0x06, USB_DT_HID_REPORT << 8, 0, 75), lambda: b"")
        self.assertEqual(out, H.REPORT_DESC)
        # SET_IDLE / GET_REPORT / SET_REPORT
        self.assertEqual(self.p.handle_control(SetupPacket(0x21, 0x0A, 0x0000, 0, 0), lambda: b""), b"")
        self.assertEqual(self.p.handle_control(SetupPacket(0xA1, 0x01, 0x0100, 0, 9), lambda: b""),
                         self.p.pack(ControllerState()))
        self.assertEqual(self.p.handle_control(SetupPacket(0x21, 0x09, 0x0200, 0, 2), lambda: b"\x01\x02"), b"")
        self.assertIsNone(self.p.handle_control(SetupPacket(0xC1, 0x01, 0, 0, 2), lambda: b""))


class FactoryTest(unittest.TestCase):
    def test_factory(self):
        self.assertEqual(make_profile("xbox360").name, "xbox360")
        self.assertEqual(make_profile("hid_gamepad", paddles={"L4": "A"}).name, "hid_gamepad")
        with self.assertRaises(ValueError):
            make_profile("nope")


if __name__ == "__main__":
    unittest.main()
