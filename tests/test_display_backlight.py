import os
import shutil
import tempfile
import unittest

import _path  # noqa: F401

from deckgadget.platform.display.backlight import Backlight, BacklightDim
from fakes import read, write


class BacklightTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.bl = os.path.join(self.tmp, "amdgpu_bl0")
        write(os.path.join(self.bl, "brightness"), "200\n")
        write(os.path.join(self.bl, "max_brightness"), "255\n")
        self.state = os.path.join(self.tmp, "run", "brightness")

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def test_save_off_restore(self):
        b = Backlight(self.bl, self.state)
        self.assertTrue(b.available)
        self.assertEqual(b.brightness(), 200)
        b.save_and_off()
        self.assertEqual(read(os.path.join(self.bl, "brightness")), "0")
        self.assertEqual(read(self.state), "200")
        self.assertEqual(b.restore(forget=False), 200)
        self.assertEqual(read(os.path.join(self.bl, "brightness")), "200")
        self.assertTrue(os.path.exists(self.state))
        b.off()
        self.assertEqual(b.restore(forget=True), 200)
        self.assertFalse(os.path.exists(self.state))
        # nothing saved + screen on -> no-op
        self.assertIsNone(Backlight(self.bl, self.state).restore())

    def test_crash_recovery_keeps_original_value(self):
        b = Backlight(self.bl, self.state)
        b.save_and_off()
        # new process after crash: brightness is 0, state file says 200
        b2 = Backlight(self.bl, self.state)
        b2.save_and_off()                      # must not overwrite the saved 200 with 0
        self.assertEqual(read(self.state), "200")
        self.assertEqual(b2.restore(), 200)

    def test_unavailable_backlight(self):
        b = Backlight(os.path.join(self.tmp, "missing"), self.state)
        self.assertFalse(b.available)
        b.save_and_off()   # no exception
        self.assertIsNone(b.restore())

    def test_backlight_dim_strategy(self):
        m = BacklightDim(Backlight(self.bl, self.state))
        self.assertEqual(m.name, "backlight")
        self.assertTrue(m.available())
        self.assertTrue(m.sleep())
        self.assertEqual(read(os.path.join(self.bl, "brightness")), "0")
        self.assertEqual(read(self.state), "200")
        self.assertTrue(m.wake())                    # temporary: state file kept
        self.assertEqual(read(os.path.join(self.bl, "brightness")), "200")
        self.assertTrue(os.path.exists(self.state))
        self.assertTrue(m.sleep())                   # re-sleep does not re-save
        self.assertEqual(read(os.path.join(self.bl, "brightness")), "0")
        self.assertEqual(read(self.state), "200")
        self.assertTrue(m.release())                 # permanent: state file gone
        self.assertEqual(read(os.path.join(self.bl, "brightness")), "200")
        self.assertFalse(os.path.exists(self.state))
        missing = BacklightDim(Backlight(os.path.join(self.tmp, "missing"), self.state))
        self.assertFalse(missing.available())
        self.assertFalse(missing.sleep())


if __name__ == "__main__":
    unittest.main()
