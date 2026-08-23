"""configfs + f_hid transport (plain-HID fallback): builds ``usb_gadget/deckctl_hid`` with one ``hid.usb0``
function, binds it to the UDC and moves reports through ``/dev/hidgN`` (O_NONBLOCK + select; f_hid returns
ESHUTDOWN until the host configures us).  Teardown reuses :func:`deckgadget.platform.guard.remove_configfs_gadget`.
"""
from __future__ import annotations

import errno
import os
import select
import subprocess
import threading
import time
from typing import Optional

from deckgadget.platform import guard
from deckgadget.profiles.base import HidFunction, Profile
from deckgadget.transports.base import FeedbackCallback, ReportSlot, TransportError, TransportMetrics
from deckgadget.util.fs import write_bytes, write_text
from deckgadget.util.log import get_logger
from deckhw.udc import Udc, udc_names

log = get_logger("usb_hid")

DEFAULT_CONFIGFS = guard.CONFIGFS
GADGET_NAME = "deckctl_hid"
FUNCTION_NAME = "hid.usb0"
CONFIG_NAME = "c.1"
LANG = "0x409"


class UsbHidTransport:
    name = "hid"

    def __init__(self, udc: Optional[str] = None, configfs: str = DEFAULT_CONFIGFS, sysfs: str = "/sys",
                 dev: str = "/dev", gadget_name: str = GADGET_NAME, modprobe: bool = True) -> None:
        self.udc = udc
        self.configfs = configfs
        self.sysfs = sysfs
        self.dev = dev
        self.gadget_name = gadget_name
        self.modprobe = modprobe
        self.gadget_dir = os.path.join(configfs, "usb_gadget", gadget_name)
        self.profile: Optional[Profile] = None
        self.on_feedback: Optional[FeedbackCallback] = None
        self.hid: Optional[HidFunction] = None
        self.node: Optional[str] = None
        self._fd = -1
        self._slot = ReportSlot()
        self._metrics = TransportMetrics()
        self._stop = threading.Event()
        self._writer: Optional[threading.Thread] = None
        self._reader: Optional[threading.Thread] = None
        self._error: Optional[BaseException] = None
        self._udc_watch: Optional[Udc] = None

    @property
    def error(self) -> Optional[BaseException]:
        return self._error

    def metrics(self) -> TransportMetrics:
        self._metrics.dropped = self._slot.dropped
        return self._metrics

    def connected(self) -> bool:
        return self._udc_watch is not None and self._udc_watch.configured()

    def start(self, profile: Profile, on_feedback: Optional[FeedbackCallback] = None) -> None:
        if self._fd >= 0:
            raise TransportError("transport already started")
        hid = profile.hid_function()
        if hid is None:
            raise TransportError(f"profile {profile.name!r} is not a plain HID device; use transport=raw")
        self.profile, self.hid, self.on_feedback = profile, hid, on_feedback
        self._ensure_configfs()
        udc = self.udc or (udc_names(self.sysfs) or [None])[0]
        if not udc:
            raise TransportError("no UDC in /sys/class/udc: DRD disabled in BIOS, or Deck is USB host (dock attached?)")
        self.udc = udc
        self._udc_watch = Udc(self.sysfs, udc)
        if os.path.isdir(self.gadget_dir):
            log.warning("stale gadget %s found; removing", self.gadget_dir)
            guard.remove_configfs_gadget(self.gadget_dir)
        try:
            self._build_gadget(hid, udc)
        except OSError as exc:
            guard.remove_configfs_gadget(self.gadget_dir)
            raise TransportError(f"configfs gadget setup failed: {exc}") from exc
        try:
            self.node = self._find_hidg_node()
        except TransportError:
            # UDC is already written (the gadget is live on the cable): never leak it
            guard.remove_configfs_gadget(self.gadget_dir)
            raise
        try:
            self._fd = os.open(self.node, os.O_RDWR | os.O_NONBLOCK | os.O_CLOEXEC)
        except OSError as exc:
            guard.remove_configfs_gadget(self.gadget_dir)
            raise TransportError(f"cannot open {self.node}: {exc}") from exc
        self._stop.clear()
        self._error = None
        self._writer = threading.Thread(target=self._write_loop, name="hidg-write", daemon=True)
        self._reader = threading.Thread(target=self._read_loop, name="hidg-read", daemon=True)
        self._writer.start()
        self._reader.start()
        log.info("f_hid gadget up: %s on %s, node %s, report %d bytes", self.gadget_name, udc, self.node, hid.report_length)

    def send(self, report: bytes) -> None:
        self._slot.put(report)

    def stop(self) -> None:
        self._stop.set()
        for thread in (self._writer, self._reader):
            if thread is not None and thread.is_alive() and thread is not threading.current_thread():
                thread.join(timeout=1.0)
        self._writer = self._reader = None
        fd, self._fd = self._fd, -1
        if fd >= 0:
            try:
                os.close(fd)
            except OSError as exc:
                log.debug("closing hidg node: %s", exc)
        if os.path.isdir(self.gadget_dir):
            guard.remove_configfs_gadget(self.gadget_dir)
            log.info("f_hid gadget removed (sent=%d dropped=%d)", self._metrics.sent, self._slot.dropped)

    def _ensure_configfs(self) -> None:
        if self.modprobe:
            subprocess.run(["modprobe", "libcomposite"], check=False, capture_output=True)
        if not os.path.isdir(os.path.join(self.configfs, "usb_gadget")):
            if self.modprobe:
                subprocess.run(["mount", "-t", "configfs", "none", self.configfs], check=False, capture_output=True)
            if not os.path.isdir(os.path.join(self.configfs, "usb_gadget")):
                raise TransportError(f"{self.configfs}/usb_gadget missing (configfs not mounted / libcomposite?)")

    def _build_gadget(self, hid: HidFunction, udc: str) -> None:
        gadget_dir = self.gadget_dir
        os.makedirs(gadget_dir, exist_ok=True)
        write_text(os.path.join(gadget_dir, "idVendor"), f"0x{hid.vid:04x}")
        write_text(os.path.join(gadget_dir, "idProduct"), f"0x{hid.pid:04x}")
        write_text(os.path.join(gadget_dir, "bcdDevice"), "0x0100")
        write_text(os.path.join(gadget_dir, "bcdUSB"), "0x0200")
        strings_dir = os.path.join(gadget_dir, "strings", LANG)
        os.makedirs(strings_dir, exist_ok=True)
        write_text(os.path.join(strings_dir, "serialnumber"), hid.serial)
        write_text(os.path.join(strings_dir, "manufacturer"), hid.manufacturer)
        write_text(os.path.join(strings_dir, "product"), hid.product)
        config_dir = os.path.join(gadget_dir, "configs", CONFIG_NAME)
        os.makedirs(os.path.join(config_dir, "strings", LANG), exist_ok=True)
        write_text(os.path.join(config_dir, "strings", LANG, "configuration"), "Config 1")
        write_text(os.path.join(config_dir, "MaxPower"), "250")
        function_dir = os.path.join(gadget_dir, "functions", FUNCTION_NAME)
        os.makedirs(function_dir, exist_ok=True)
        write_text(os.path.join(function_dir, "protocol"), str(hid.protocol))
        write_text(os.path.join(function_dir, "subclass"), str(hid.subclass))
        write_text(os.path.join(function_dir, "report_length"), str(hid.report_length))
        write_bytes(os.path.join(function_dir, "report_desc"), bytes(hid.report_desc))
        link = os.path.join(config_dir, FUNCTION_NAME)
        if not os.path.lexists(link):
            os.symlink(function_dir, link)
        write_text(os.path.join(gadget_dir, "UDC"), udc)

    def _find_hidg_node(self, timeout: float = 2.0) -> str:
        """Map ``functions/hid.usb0/dev`` (``major:minor``) to ``/dev/hidgN``."""
        dev_attr = os.path.join(self.gadget_dir, "functions", FUNCTION_NAME, "dev")
        deadline = time.monotonic() + timeout
        wanted_rdev = None
        while time.monotonic() < deadline:
            try:
                with open(dev_attr, "r", encoding="utf-8") as f:
                    major, minor = (int(part) for part in f.read().strip().split(":"))
                wanted_rdev = os.makedev(major, minor)
            except (OSError, ValueError) as exc:
                log.debug("no usable %s yet (%s) — will take the first hidg node", dev_attr, exc)
                wanted_rdev = None
            try:
                for entry in sorted(os.listdir(self.dev)):
                    if not entry.startswith("hidg"):
                        continue
                    path = os.path.join(self.dev, entry)
                    if wanted_rdev is None or os.stat(path).st_rdev == wanted_rdev:
                        return path
            except OSError as exc:
                log.debug("scanning %s for hidg nodes: %s", self.dev, exc)
            time.sleep(0.05)
        raise TransportError("no /dev/hidg* node appeared after binding the gadget")

    def _write_loop(self) -> None:
        fd = self._fd
        pending: Optional[bytes] = None
        while not self._stop.is_set():
            if pending is None:
                pending = self._slot.take(0.25)
                if pending is None:
                    continue
            try:
                os.write(fd, pending)
                self._metrics.sent += 1
                pending = None
            except BlockingIOError:
                try:
                    select.select([], [fd], [], 0.25)
                except (OSError, ValueError) as exc:
                    if not self._stop.is_set():
                        log.warning("hidg node gone while writing: %s", exc)
                    break
            except OSError as exc:
                if exc.errno == errno.ESHUTDOWN:
                    pending = None          # host not configured yet / went away: drop and wait
                    time.sleep(0.05)
                    continue
                if exc.errno == errno.EINTR:
                    continue
                self._metrics.errors += 1
                log.warning("hidg write error: %s", exc)
                time.sleep(0.1)
                pending = None

    def _read_loop(self) -> None:
        fd = self._fd
        profile = self.profile
        size = self.hid.report_length if self.hid else 64
        while not self._stop.is_set():
            try:
                readable, _, _ = select.select([fd], [], [], 0.25)
            except (OSError, ValueError) as exc:
                if not self._stop.is_set():
                    log.warning("hidg node gone while reading: %s", exc)
                break
            if not readable:
                continue
            try:
                data = os.read(fd, max(size, 64))
            except BlockingIOError:
                continue
            except OSError as exc:
                if exc.errno in (errno.ESHUTDOWN, errno.EINTR):
                    time.sleep(0.05)
                    continue
                log.debug("hidg read ended: %s", exc)
                break
            if not data or profile is None:
                continue
            self._metrics.out_reports += 1
            try:
                feedback = profile.on_output(data)
            except Exception as exc:  # noqa: BLE001
                log.warning("on_output failed: %s", exc)
                continue
            if feedback is not None and self.on_feedback is not None:
                try:
                    self.on_feedback(feedback)
                except Exception as exc:  # noqa: BLE001
                    log.warning("feedback callback failed: %s", exc)
