import struct
import unittest

import _path  # noqa: F401

from deckgadget import state as S
from deckgadget.sources import neptune_usb as N


def make_report(buttons_l=0, buttons_h=0, packet=1, lpad=(0, 0), rpad=(0, 0), accel=(0, 0, 0), gyro=(0, 0, 0),
                quat=(0, 0, 0, 0), trig=(0, 0), lstick=(0, 0), rstick=(0, 0), press=(0, 0), version=1, typ=9, length=64):
    body = N.REPORT_STRUCT.pack(version, typ, length, packet, buttons_l, buttons_h, *lpad, *rpad, *accel, *gyro,
                                *quat, *trig, *lstick, *rstick, *press)
    return body.ljust(64, b"\0")


class ParserTest(unittest.TestCase):
    def test_layout_offsets(self):
        rep = make_report(buttons_l=0x11223344, buttons_h=0x55667788, packet=0xAABBCCDD, trig=(1000, 2000),
                          lstick=(-1, 2), rstick=(3, -4))
        self.assertEqual(len(rep), 64)
        self.assertEqual(struct.unpack_from("<H", rep, 0)[0], 1)
        self.assertEqual(rep[2], 9)
        self.assertEqual(rep[3], 64)
        self.assertEqual(struct.unpack_from("<I", rep, N.OFF_PACKET_NUM)[0], 0xAABBCCDD)
        self.assertEqual(struct.unpack_from("<I", rep, N.OFF_BUTTONS_L)[0], 0x11223344)
        self.assertEqual(struct.unpack_from("<I", rep, N.OFF_BUTTONS_H)[0], 0x55667788)
        self.assertEqual(struct.unpack_from("<H", rep, N.OFF_TRIGGER_L)[0], 1000)
        self.assertEqual(struct.unpack_from("<H", rep, N.OFF_TRIGGER_R)[0], 2000)
        self.assertEqual(struct.unpack_from("<hhhh", rep, N.OFF_LSTICK_X), (-1, 2, 3, -4))

    def test_parse_synthetic_report(self):
        bl = (N.STEAMDECK_LBUTTON_A | N.STEAMDECK_LBUTTON_L | N.STEAMDECK_LBUTTON_DPAD_LEFT
              | N.STEAMDECK_LBUTTON_VIEW | N.STEAMDECK_LBUTTON_L3 | N.STEAMDECK_LBUTTON_R3 | N.STEAMDECK_LBUTTON_L5)
        bh = N.STEAMDECK_HBUTTON_L4 | N.STEAMDECK_HBUTTON_QAM
        rep = make_report(buttons_l=bl, buttons_h=bh, packet=42, trig=(32767, 100), lstick=(-30000, 30000),
                          rstick=(12, -12), lpad=(5, 6), rpad=(7, 8), press=(9, 10), accel=(1, 2, 3), gyro=(4, 5, 6))
        st = N.parse_report(rep, timestamp=1.5)
        self.assertIsNotNone(st)
        self.assertEqual(st.buttons, S.BTN_A | S.BTN_L1 | S.BTN_DPAD_LEFT | S.BTN_VIEW | S.BTN_L3 | S.BTN_R3
                         | S.BTN_L5 | S.BTN_L4 | S.BTN_QAM)
        self.assertEqual((st.lx, st.ly, st.rx, st.ry), (-30000, 30000, 12, -12))
        self.assertEqual((st.lt, st.rt), (32767, 100))
        self.assertEqual(st.packet, 42)
        self.assertEqual(st.ts, 1.5)
        self.assertIsNone(st.gyro)
        st2 = N.parse_report(rep, with_sensors=True)
        self.assertEqual(st2.lpad, (5, 6, 9))
        self.assertEqual(st2.rpad, (7, 8, 10))
        self.assertEqual(st2.accel, (1, 2, 3))
        self.assertEqual(st2.gyro, (4, 5, 6))

    def test_hid_steam_cross_check_bytes(self):
        # Facts from hid-steam.c: byte 8 bit7 = A, byte 9 bit4 = VIEW, byte 10 bit6 = L3, byte 11 bit2 = R3,
        # byte 13 bit1 = L4, byte 13 bit2 = R4, byte 14 bit2 = QAM.
        rep = bytearray(make_report())
        rep[8] |= 1 << 7
        rep[9] |= 1 << 4
        rep[10] |= 1 << 6
        rep[11] |= 1 << 2
        rep[13] |= (1 << 1) | (1 << 2)
        rep[14] |= 1 << 2
        st = N.parse_report(bytes(rep))
        self.assertEqual(st.buttons, S.BTN_A | S.BTN_VIEW | S.BTN_L3 | S.BTN_R3 | S.BTN_L4 | S.BTN_R4 | S.BTN_QAM)

    def test_all_table_entries_roundtrip(self):
        for mask, bit, name in N.BUTTONS_L:
            st = N.parse_report(make_report(buttons_l=mask))
            self.assertEqual(st.buttons, bit, name)
            self.assertEqual(S.button_names(st.buttons), [name] if name in S.BUTTON_BY_NAME else S.button_names(bit))
        for mask, bit, name in N.BUTTONS_H:
            st = N.parse_report(make_report(buttons_h=mask))
            self.assertEqual(st.buttons, bit, name)

    def test_rejects_other_packets(self):
        self.assertIsNone(N.parse_report(make_report(typ=4)))
        self.assertIsNone(N.parse_report(make_report(version=2)))
        self.assertIsNone(N.parse_report(make_report(length=10)))
        self.assertIsNone(N.parse_report(b"\x01\x00\x09"))
        self.assertIsNone(N.parse_report(b""))

    def test_decode_report_names_unknown_bits(self):
        rep = make_report(buttons_l=N.STEAMDECK_LBUTTON_B | (1 << 21), buttons_h=N.STEAMDECK_HBUTTON_R4 | (1 << 3),
                          trig=(1, 2), lstick=(3, 4), rstick=(5, 6))
        dec = N.decode_report(rep)
        self.assertTrue(dec["deck_state"])
        self.assertEqual(dec["buttons"], ["B", "R4"])
        self.assertEqual(dec["unknown_bits"], ["L:bit21", "H:bit3"])
        self.assertEqual((dec["trigger_l"], dec["trigger_r"]), (1, 2))
        self.assertEqual(dec["lstick"], (3, 4))
        self.assertEqual(dec["rstick"], (5, 6))
        self.assertEqual(dec["hex"], rep.hex())
        self.assertFalse(N.decode_report(b"\x01\x00\x04\x40" + b"\0" * 60)["deck_state"])


class CommandTest(unittest.TestCase):
    def test_feature_builders(self):
        clr = N.cmd_clear_digital_mappings()
        self.assertEqual(len(clr), 64)
        self.assertEqual(clr[:2], bytes([0x81, 0]))
        self.assertEqual(clr[2:], b"\0" * 62)
        seq = N.lizard_off_sequence()
        self.assertEqual(len(seq), 2)
        self.assertEqual(seq[0], clr)
        s = seq[1]
        self.assertEqual(s[0], 0x87)
        self.assertEqual(s[1], 15)   # 5 x ControllerSetting (u8 + u16)
        self.assertEqual(s[2:17], struct.pack("<BHBHBHBHBH", 24, 0, 7, 7, 8, 7, 52, 0xFFFF, 53, 0xFFFF))
        hb = N.heartbeat_sequence()
        self.assertEqual(hb[0], clr)
        self.assertEqual(hb[1][:5], bytes([0x87, 3, 8, 7, 0]))
        with self.assertRaises(ValueError):
            N.build_feature_report(0x87, b"\0" * 63)

    def test_rumble(self):
        r = N.cmd_rumble(0x1234, 0xABCD)
        self.assertEqual(r[0], 0xEB)
        self.assertEqual(r[1], 0)   # header.length 0, exactly like SDL / hid-steam
        self.assertEqual(len(r), 64)
        self.assertEqual(r[2:11], struct.pack("<BHHHbb", 0, 0, 0x1234, 0xABCD, 2, 0))
        self.assertEqual(N.cmd_rumble(-5, 999999)[2:11], struct.pack("<BHHHbb", 0, 0, 0, 65535, 2, 0))

    def test_constants(self):
        self.assertEqual(N.ID_CONTROLLER_DECK_STATE, 9)
        self.assertEqual(N.SETTING_LEFT_TRACKPAD_MODE, 7)
        self.assertEqual(N.SETTING_RIGHT_TRACKPAD_MODE, 8)
        self.assertEqual(N.SETTING_SMOOTH_ABSOLUTE_MOUSE, 24)
        self.assertEqual(N.SETTING_IMU_MODE, 48)
        self.assertEqual(N.SETTING_LEFT_TRACKPAD_CLICK_PRESSURE, 52)
        self.assertEqual(N.SETTING_RIGHT_TRACKPAD_CLICK_PRESSURE, 53)
        self.assertEqual(N.SETTING_STEAM_WATCHDOG_ENABLE, 71)
        self.assertEqual(N.TRACKPAD_NONE, 7)
        self.assertEqual(N.FEATURE_WVALUE, 0x0300)


if __name__ == "__main__":
    unittest.main()
