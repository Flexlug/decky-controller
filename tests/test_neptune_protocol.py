import struct
import unittest

import _path  # noqa: F401

from deckgadget import state as S
from deckgadget.sources.neptune import protocol as P


def make_report(buttons_l=0, buttons_h=0, packet=1, lpad=(0, 0), rpad=(0, 0), accel=(0, 0, 0), gyro=(0, 0, 0),
                quat=(0, 0, 0, 0), trig=(0, 0), lstick=(0, 0), rstick=(0, 0), press=(0, 0), version=1, typ=9, length=64):
    body = P.REPORT_STRUCT.pack(version, typ, length, packet, buttons_l, buttons_h, *lpad, *rpad, *accel, *gyro,
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
        self.assertEqual(struct.unpack_from("<I", rep, P.OFF_PACKET_NUM)[0], 0xAABBCCDD)
        self.assertEqual(struct.unpack_from("<I", rep, P.OFF_BUTTONS_L)[0], 0x11223344)
        self.assertEqual(struct.unpack_from("<I", rep, P.OFF_BUTTONS_H)[0], 0x55667788)
        self.assertEqual(struct.unpack_from("<H", rep, P.OFF_TRIGGER_L)[0], 1000)
        self.assertEqual(struct.unpack_from("<H", rep, P.OFF_TRIGGER_R)[0], 2000)
        self.assertEqual(struct.unpack_from("<hhhh", rep, P.OFF_LSTICK_X), (-1, 2, 3, -4))

    def test_parse_synthetic_report(self):
        bl = (P.STEAMDECK_LBUTTON_A | P.STEAMDECK_LBUTTON_L | P.STEAMDECK_LBUTTON_DPAD_LEFT
              | P.STEAMDECK_LBUTTON_VIEW | P.STEAMDECK_LBUTTON_L3 | P.STEAMDECK_LBUTTON_R3 | P.STEAMDECK_LBUTTON_L5)
        bh = P.STEAMDECK_HBUTTON_L4 | P.STEAMDECK_HBUTTON_QAM
        rep = make_report(buttons_l=bl, buttons_h=bh, packet=42, trig=(32767, 100), lstick=(-30000, 30000),
                          rstick=(12, -12), lpad=(5, 6), rpad=(7, 8), press=(9, 10), accel=(1, 2, 3), gyro=(4, 5, 6))
        st = P.parse_report(rep, timestamp=1.5)
        self.assertIsNotNone(st)
        self.assertEqual(st.buttons, S.BTN_A | S.BTN_L1 | S.BTN_DPAD_LEFT | S.BTN_VIEW | S.BTN_L3 | S.BTN_R3
                         | S.BTN_L5 | S.BTN_L4 | S.BTN_QAM)
        self.assertEqual((st.lx, st.ly, st.rx, st.ry), (-30000, 30000, 12, -12))
        self.assertEqual((st.lt, st.rt), (32767, 100))
        self.assertEqual(st.packet, 42)
        self.assertEqual(st.ts, 1.5)
        self.assertIsNone(st.gyro)
        st2 = P.parse_report(rep, with_sensors=True)
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
        st = P.parse_report(bytes(rep))
        self.assertEqual(st.buttons, S.BTN_A | S.BTN_VIEW | S.BTN_L3 | S.BTN_R3 | S.BTN_L4 | S.BTN_R4 | S.BTN_QAM)

    def test_all_table_entries_roundtrip(self):
        for mask, bit, name in P.BUTTONS_L:
            st = P.parse_report(make_report(buttons_l=mask))
            self.assertEqual(st.buttons, bit, name)
            self.assertEqual(S.button_names(st.buttons), [name] if name in S.BUTTON_BY_NAME else S.button_names(bit))
        for mask, bit, name in P.BUTTONS_H:
            st = P.parse_report(make_report(buttons_h=mask))
            self.assertEqual(st.buttons, bit, name)

    def test_rejects_other_packets(self):
        self.assertIsNone(P.parse_report(make_report(typ=4)))
        self.assertIsNone(P.parse_report(make_report(version=2)))
        self.assertIsNone(P.parse_report(make_report(length=10)))
        self.assertIsNone(P.parse_report(b"\x01\x00\x09"))
        self.assertIsNone(P.parse_report(b""))

    def test_decode_report_names_unknown_bits(self):
        rep = make_report(buttons_l=P.STEAMDECK_LBUTTON_B | (1 << 21), buttons_h=P.STEAMDECK_HBUTTON_R4 | (1 << 3),
                          trig=(1, 2), lstick=(3, 4), rstick=(5, 6))
        dec = P.decode_report(rep)
        self.assertTrue(dec["deck_state"])
        self.assertEqual(dec["buttons"], ["B", "R4"])
        self.assertEqual(dec["unknown_bits"], ["L:bit21", "H:bit3"])
        self.assertEqual((dec["trigger_l"], dec["trigger_r"]), (1, 2))
        self.assertEqual(dec["lstick"], (3, 4))
        self.assertEqual(dec["rstick"], (5, 6))
        self.assertEqual(dec["hex"], rep.hex())
        self.assertFalse(P.decode_report(b"\x01\x00\x04\x40" + b"\0" * 60)["deck_state"])


if __name__ == "__main__":
    unittest.main()
