"""The FTS3528 touchscreen stays alive while the panel sleeps; its evdev node is read in a thread so a
finger down can wake the screen."""
from __future__ import annotations

import os
import select
import struct
import threading
import time
from typing import Callable, Optional

from deckgadget.util.log import get_logger
from deckhw.sysfs import Sysfs

log = get_logger("screen")

TOUCHSCREEN_NAME_SUBSTR = "FTS3528"

# struct input_event on x86_64: struct timeval (2 x long) + u16 type + u16 code + s32 value = 24 bytes
INPUT_EVENT = struct.Struct("<qqHHi")
EV_SYN = 0x00
EV_KEY = 0x01
EV_ABS = 0x03
BTN_TOUCH = 0x14A
ABS_MT_TRACKING_ID = 0x39


def find_touchscreen(sysfs: str = "/sys", dev: str = "/dev",
                     name_substr: str = TOUCHSCREEN_NAME_SUBSTR) -> Optional[str]:
    """``/dev/input/eventN`` of the device whose name contains ``name_substr``."""
    tree = Sysfs(sysfs)
    for entry in sorted(tree.listdir("class", "input"), key=lambda name: (len(name), name)):
        if not entry.startswith("event"):
            continue
        name = tree.text("class", "input", entry, "device", "name") or ""
        if name_substr.lower() in name.lower():
            return os.path.join(dev, "input", entry)
    # Fallback: /proc/bus/input/devices ("N: Name=..." followed by "H: Handlers=... eventN")
    try:
        with open("/proc/bus/input/devices", "r", encoding="utf-8", errors="replace") as f:
            blocks = f.read().split("\n\n")
    except OSError as exc:
        log.debug("cannot read /proc/bus/input/devices: %s", exc)
        return None
    for block in blocks:
        if name_substr.lower() not in block.lower():
            continue
        for line in block.splitlines():
            if line.startswith("H:"):
                for token in line.split():
                    if token.startswith("event"):
                        return os.path.join(dev, "input", token)
    return None


def parse_input_events(data: bytes):
    """Yield ``(type, code, value)`` tuples from a raw evdev read."""
    count = len(data) // INPUT_EVENT.size
    for i in range(count):
        _sec, _usec, event_type, code, value = INPUT_EVENT.unpack_from(data, i * INPUT_EVENT.size)
        yield event_type, code, value


def is_touch_event(event_type: int, code: int, value: int) -> bool:
    if event_type == EV_KEY and code == BTN_TOUCH and value:
        return True
    if event_type == EV_ABS and code == ABS_MT_TRACKING_ID and value >= 0:
        return True
    return False


class TouchWatcher:
    """Background reader of an evdev node; calls ``on_touch()`` (debounced) on finger down."""

    def __init__(self, event_path: str, on_touch: Callable[[], None], debounce_s: float = 0.2) -> None:
        self.event_path = event_path
        self.on_touch = on_touch
        self.debounce_s = debounce_s
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._fd = -1
        self.last_touch = 0.0

    def start(self) -> None:
        self._fd = os.open(self.event_path, os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC)
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="touch-watch", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=1.0)
        self._thread = None
        if self._fd >= 0:
            try:
                os.close(self._fd)
            except OSError as exc:
                log.debug("closing touch fd: %s", exc)
            self._fd = -1

    def _run(self) -> None:
        fd = self._fd
        while not self._stop.is_set():
            try:
                readable, _, _ = select.select([fd], [], [], 0.25)
            except (OSError, ValueError) as exc:
                if not self._stop.is_set():
                    log.warning("touchscreen %s unreadable, touch-to-wake stops: %s", self.event_path, exc)
                break
            if not readable:
                continue
            try:
                data = os.read(fd, INPUT_EVENT.size * 64)
            except BlockingIOError:
                continue
            except OSError as exc:
                log.warning("touchscreen read failed: %s", exc)
                break
            if not data:
                break
            now = time.monotonic()
            for event_type, code, value in parse_input_events(data):
                if is_touch_event(event_type, code, value) and now - self.last_touch >= self.debounce_s:
                    self.last_touch = now
                    try:
                        self.on_touch()
                    except Exception as exc:  # noqa: BLE001 - never kill the watcher thread
                        log.warning("touch callback failed: %s", exc)
                    break
