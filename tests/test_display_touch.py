import os
import shutil
import struct
import tempfile
import threading
import unittest

import _path  # noqa: F401

from deckgadget.platform.display import touch
from deckgadget.platform.display.touch import (ABS_MT_TRACKING_ID, BTN_TOUCH, EV_ABS, EV_KEY, EV_SYN, INPUT_EVENT,
                                               TouchWatcher, find_touchscreen, is_touch_event, parse_input_events)
from fakes import FakeSysfs, write


class TouchscreenTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.sys = os.path.join(self.tmp, "sys")
        self.fs = FakeSysfs(self.tmp)
        self.fs.add_input_device("event3", "Steam Deck Controller")
        self.fs.add_input_device("event14", "FTS3528:00 2808:1015", touchscreen=True)

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def test_find_touchscreen_by_capabilities(self):
        self.assertEqual(find_touchscreen(self.sys, "/dev"), "/dev/input/event14")
        self.assertIsNone(find_touchscreen(os.path.join(self.tmp, "nope"), "/dev"))

    def test_touchscreen_found_whatever_its_name(self):
        self.fs.add_input_device("event7", "Some Other Panel", touchscreen=True)
        shutil.rmtree(os.path.join(self.sys, "class", "input", "event14"))
        self.assertEqual(find_touchscreen(self.sys, "/dev"), "/dev/input/event7")

    def test_known_name_breaks_ties_between_two_touchscreens(self):
        self.fs.add_input_device("event2", "External Tablet", touchscreen=True)
        self.assertEqual(find_touchscreen(self.sys, "/dev"), "/dev/input/event14")

    def test_bitmask_parsing(self):
        self.assertEqual(touch._bitmask("261800000000003"), 0x261800000000003)
        self.assertEqual(touch._bitmask("1 3"), (1 << 64) | 0x3)
        self.assertEqual(touch._bitmask("0"), 0)
        self.assertEqual(touch._bitmask(None), 0)
        self.assertEqual(touch._bitmask("zz"), 0)

    def test_parse_input_events(self):
        self.assertEqual(INPUT_EVENT.size, 24)
        buf = struct.pack("<qqHHi", 1, 2, EV_ABS, ABS_MT_TRACKING_ID, 5) + \
              struct.pack("<qqHHi", 1, 2, EV_KEY, BTN_TOUCH, 1) + \
              struct.pack("<qqHHi", 1, 2, EV_SYN, 0, 0)
        evs = list(parse_input_events(buf))
        self.assertEqual(len(evs), 3)
        self.assertTrue(is_touch_event(*evs[0]))
        self.assertTrue(is_touch_event(*evs[1]))
        self.assertFalse(is_touch_event(*evs[2]))
        self.assertFalse(is_touch_event(EV_KEY, BTN_TOUCH, 0))
        self.assertFalse(is_touch_event(EV_ABS, ABS_MT_TRACKING_ID, -1))

    def test_touch_watcher_reads_pipe(self):
        r, w = os.pipe()
        path = f"/proc/self/fd/{r}"
        hits = []
        done = threading.Event()

        def on_touch():
            hits.append(1)
            done.set()

        watcher = TouchWatcher(path, on_touch, debounce_s=0.0)
        watcher.start()
        try:
            os.write(w, struct.pack("<qqHHi", 0, 0, EV_KEY, BTN_TOUCH, 1))
            self.assertTrue(done.wait(2.0))
        finally:
            watcher.stop()
            os.close(w)
            os.close(r)
        self.assertEqual(hits, [1])


if __name__ == "__main__":
    unittest.main()
