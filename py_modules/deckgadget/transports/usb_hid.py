"""configfs + f_hid transport (port of the original configfs spike) — the plain-HID fallback.

Creates ``/sys/kernel/config/usb_gadget/deckctl_hid`` with one ``hid.usb0`` function,
binds it to the UDC and writes reports to ``/dev/hidgN`` (O_NONBLOCK + select so the
writer never blocks forever; f_hid returns ``ESHUTDOWN`` until the host configures us).
Output reports from the host are read from the same node and passed to
``profile.on_output``.  Teardown reuses :func:`deckgadget.platform.guard.remove_configfs_gadget`.
"""
from __future__ import annotations

import errno
import os
import select
import subprocess
import threading
import time
from typing import Optional

from ..platform import guard, usb_role
from ..profiles.base import HidFunction, Profile
from ..util.log import get_logger
from ..util.fs import write_bytes, write_text
from .base import FeedbackCallback, ReportSlot, TransportError, TransportMetrics

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
        self._udc_watch: Optional[usb_role.UdcWatcher] = None

    # --- Transport protocol -----------------------------------------------------------
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
        udc = self.udc or (usb_role.list_udcs(self.sysfs) or [None])[0]
        if not udc:
            raise TransportError("no UDC in /sys/class/udc: DRD disabled in BIOS, or Deck is USB host (dock attached?)")
        self.udc = udc
        self._udc_watch = usb_role.UdcWatcher(udc, self.sysfs)
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
            # _build_gadget() already wrote UDC (the gadget is live on the cable): never leak it.
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
        for t in (self._writer, self._reader):
            if t is not None and t.is_alive() and t is not threading.current_thread():
                t.join(timeout=1.0)
        self._writer = self._reader = None
        fd, self._fd = self._fd, -1
        if fd >= 0:
            try:
                os.close(fd)
            except OSError:
                pass
        if os.path.isdir(self.gadget_dir):
            guard.remove_configfs_gadget(self.gadget_dir)
            log.info("f_hid gadget removed (sent=%d dropped=%d)", self._metrics.sent, self._slot.dropped)

    # --- setup helpers ---------------------------------------------------------------
    def _ensure_configfs(self) -> None:
        if self.modprobe:
            subprocess.run(["modprobe", "libcomposite"], check=False, capture_output=True)
        if not os.path.isdir(os.path.join(self.configfs, "usb_gadget")):
            if self.modprobe:
                subprocess.run(["mount", "-t", "configfs", "none", self.configfs], check=False, capture_output=True)
            if not os.path.isdir(os.path.join(self.configfs, "usb_gadget")):
                raise TransportError(f"{self.configfs}/usb_gadget missing (configfs not mounted / libcomposite?)")

    def _build_gadget(self, hid: HidFunction, udc: str) -> None:
        g = self.gadget_dir
        os.makedirs(g, exist_ok=True)
        write_text(os.path.join(g, "idVendor"), f"0x{hid.vid:04x}")
        write_text(os.path.join(g, "idProduct"), f"0x{hid.pid:04x}")
        write_text(os.path.join(g, "bcdDevice"), "0x0100")
        write_text(os.path.join(g, "bcdUSB"), "0x0200")
        strings = os.path.join(g, "strings", LANG)
        os.makedirs(strings, exist_ok=True)
        write_text(os.path.join(strings, "serialnumber"), hid.serial)
        write_text(os.path.join(strings, "manufacturer"), hid.manufacturer)
        write_text(os.path.join(strings, "product"), hid.product)
        cfg = os.path.join(g, "configs", CONFIG_NAME)
        os.makedirs(os.path.join(cfg, "strings", LANG), exist_ok=True)
        write_text(os.path.join(cfg, "strings", LANG, "configuration"), "Config 1")
        write_text(os.path.join(cfg, "MaxPower"), "250")
        fn = os.path.join(g, "functions", FUNCTION_NAME)
        os.makedirs(fn, exist_ok=True)
        write_text(os.path.join(fn, "protocol"), str(hid.protocol))
        write_text(os.path.join(fn, "subclass"), str(hid.subclass))
        write_text(os.path.join(fn, "report_length"), str(hid.report_length))
        write_bytes(os.path.join(fn, "report_desc"), bytes(hid.report_desc))
        link = os.path.join(cfg, FUNCTION_NAME)
        if not os.path.lexists(link):
            os.symlink(fn, link)
        write_text(os.path.join(g, "UDC"), udc)

    def _find_hidg_node(self, timeout: float = 2.0) -> str:
        """Map ``functions/hid.usb0/dev`` (``major:minor``) to ``/dev/hidgN``."""
        dev_attr = os.path.join(self.gadget_dir, "functions", FUNCTION_NAME, "dev")
        deadline = time.monotonic() + timeout
        want = None
        while time.monotonic() < deadline:
            try:
                with open(dev_attr, "r", encoding="utf-8") as f:
                    major, minor = (int(x) for x in f.read().strip().split(":"))
                want = os.makedev(major, minor)
            except (OSError, ValueError):
                want = None
            try:
                for entry in sorted(os.listdir(self.dev)):
                    if not entry.startswith("hidg"):
                        continue
                    path = os.path.join(self.dev, entry)
                    if want is None or os.stat(path).st_rdev == want:
                        return path
            except OSError:
                pass
            time.sleep(0.05)
        raise TransportError("no /dev/hidg* node appeared after binding the gadget")

    # --- threads ----------------------------------------------------------------------
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
                except (OSError, ValueError):
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
                r, _, _ = select.select([fd], [], [], 0.25)
            except (OSError, ValueError):
                break
            if not r:
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
                fb = profile.on_output(data)
            except Exception as exc:  # noqa: BLE001
                log.warning("on_output failed: %s", exc)
                continue
            if fb is not None and self.on_feedback is not None:
                try:
                    self.on_feedback(fb)
                except Exception as exc:  # noqa: BLE001
                    log.warning("feedback callback failed: %s", exc)
