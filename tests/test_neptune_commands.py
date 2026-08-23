import struct
import unittest

import _path  # noqa: F401

from deckgadget.sources.neptune import commands as CMD


class CommandTest(unittest.TestCase):
    def test_feature_builders(self):
        clr = CMD.cmd_clear_digital_mappings()
        self.assertEqual(len(clr), 64)
        self.assertEqual(clr[:2], bytes([0x81, 0]))
        self.assertEqual(clr[2:], b"\0" * 62)
        seq = CMD.lizard_off_sequence()
        self.assertEqual(len(seq), 2)
        self.assertEqual(seq[0], clr)
        s = seq[1]
        self.assertEqual(s[0], 0x87)
        self.assertEqual(s[1], 15)   # 5 x ControllerSetting (u8 + u16)
        self.assertEqual(s[2:17], struct.pack("<BHBHBHBHBH", 24, 0, 7, 7, 8, 7, 52, 0xFFFF, 53, 0xFFFF))
        hb = CMD.heartbeat_sequence()
        self.assertEqual(hb[0], clr)
        self.assertEqual(hb[1][:5], bytes([0x87, 3, 8, 7, 0]))
        with self.assertRaises(ValueError):
            CMD.build_feature_report(0x87, b"\0" * 63)

    def test_rumble(self):
        r = CMD.cmd_rumble(0x1234, 0xABCD)
        self.assertEqual(r[0], 0xEB)
        self.assertEqual(r[1], 0)   # header.length 0, exactly like SDL / hid-steam
        self.assertEqual(len(r), 64)
        self.assertEqual(r[2:11], struct.pack("<BHHHbb", 0, 0, 0x1234, 0xABCD, 2, 0))
        self.assertEqual(CMD.cmd_rumble(-5, 999999)[2:11], struct.pack("<BHHHbb", 0, 0, 0, 65535, 2, 0))


if __name__ == "__main__":
    unittest.main()
