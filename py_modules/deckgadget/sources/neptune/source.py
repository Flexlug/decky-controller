"""Exclusive capture of the built-in controller through usbfs: detach interfaces 1.0/1.1/1.2 from usbhid,
claim interface 2, read the 64-byte interrupt IN reports, keep lizard mode off with a 1 s heartbeat."""
from __future__ import annotations

import errno
import threading
import time
from typing import List, Optional

from deckhw.neptune import CONTROLLER_INTERFACE, NeptuneDevice, find_neptune

from deckgadget.platform import neptune_binding
from deckgadget.platform.usbfs import UsbfsDevice
from deckgadget.sources.neptune.commands import (
    FEATURE_WVALUE, HID_FEATURE_REPORT_BYTES, HID_REQ_GET_REPORT, HID_REQ_SET_REPORT,
    USB_REQTYPE_GET_CLASS_INTERFACE, USB_REQTYPE_SET_CLASS_INTERFACE, cmd_rumble, heartbeat_sequence,
    lizard_off_sequence,
)
from deckgadget.sources.neptune.protocol import REPORT_LEN, parse_report
from deckgadget.state import ControllerState
from deckgadget.util.log import get_logger

log = get_logger("neptune_usb")


class NeptuneError(RuntimeError):
    pass


class NeptuneUsbSource:
    """InputSource for the built-in controller."""

    name = "neptune_usb"

    def __init__(self, sysfs: str = "/sys", dev: str = "/dev", heartbeat_s: float = 1.0,
                 device_class=UsbfsDevice, with_sensors: bool = False) -> None:
        self.sysfs = sysfs
        self.dev = dev
        self.heartbeat_s = heartbeat_s
        self._device_class = device_class
        self.with_sensors = with_sensors
        self.device: Optional[NeptuneDevice] = None
        self.usb_device: Optional[UsbfsDevice] = None
        self.interface = CONTROLLER_INTERFACE
        self.ep_in = 0x83
        self.detached: List[str] = []
        self._binder = neptune_binding.UsbhidBinder(sysfs)
        self._control_lock = threading.Lock()
        self._heartbeat_stop = threading.Event()
        self._heartbeat_thread: Optional[threading.Thread] = None
        self._opened = False
        self.reports = 0
        self.other_packets = 0
        self.heartbeats = 0
        self.heartbeat_errors = 0

    def open(self) -> None:
        if self._opened:
            return
        device = find_neptune(self.sysfs, self.dev)
        if device is None:
            raise NeptuneError("Steam Deck controller (28de:1205) not found in sysfs")
        controller_interface = device.interface(self.interface)
        if controller_interface is None:
            raise NeptuneError(f"controller interface {self.interface} not present on {device.name}")
        endpoint = controller_interface.interrupt_in()
        if endpoint is None:
            raise NeptuneError(f"no interrupt IN endpoint on {controller_interface.name}")
        if endpoint.max_packet != REPORT_LEN:
            log.warning("unexpected wMaxPacketSize %d on ep 0x%02x (expected %d)",
                        endpoint.max_packet, endpoint.address, REPORT_LEN)
        self.device = device
        self.ep_in = endpoint.address
        log.info("neptune %s at %s, iface %d ep 0x%02x", device.name, device.devnode, self.interface, self.ep_in)
        try:
            self.detached = neptune_binding.capture_interfaces(device, self._binder)
            self.usb_device = self._device_class(device.devnode)
            try:
                self.usb_device.claim_interface(self.interface)
            except OSError as exc:
                if exc.errno != errno.EBUSY:
                    raise
                log.warning("claim iface %d busy (%s); trying USBDEVFS_DISCONNECT_CLAIM", self.interface, exc)
                self.usb_device.disconnect_claim(self.interface)
            self.disable_lizard_mode()
            self._heartbeat_stop.clear()
            self._heartbeat_thread = threading.Thread(target=self._heartbeat_loop, name="neptune-heartbeat",
                                                      daemon=True)
            self._heartbeat_thread.start()
            self._opened = True
        except Exception:
            self.close()
            raise

    def close(self) -> None:
        self._heartbeat_stop.set()
        thread = self._heartbeat_thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=2.0)
        self._heartbeat_thread = None
        usb_device, self.usb_device = self.usb_device, None
        if usb_device is not None:
            try:
                usb_device.release_interface(self.interface)
            except OSError as exc:
                log.warning("releasing interface %d failed: %s", self.interface, exc)
            usb_device.close()
        # all capture interfaces, not only the ones we detached: a crashed run may have left others unbound
        try:
            rebound = neptune_binding.release_interfaces(self.device, self._binder)
            if rebound:
                log.info("rebound to usbhid: %s", ", ".join(rebound))
        except Exception as exc:  # noqa: BLE001
            log.warning("rebind failed: %s", exc)
        self.detached = []
        self._opened = False

    def send_feature(self, report: bytes, timeout_ms: int = 1000) -> None:
        if self.usb_device is None:
            raise NeptuneError("device not open")
        if len(report) != HID_FEATURE_REPORT_BYTES:
            raise ValueError("feature report must be 64 bytes")
        with self._control_lock:
            self.usb_device.control_out(USB_REQTYPE_SET_CLASS_INTERFACE, HID_REQ_SET_REPORT, FEATURE_WVALUE,
                                        self.interface, report, timeout_ms)

    def get_feature(self, timeout_ms: int = 200) -> Optional[bytes]:
        if self.usb_device is None:
            return None
        with self._control_lock:
            try:
                return self.usb_device.control_in(USB_REQTYPE_GET_CLASS_INTERFACE, HID_REQ_GET_REPORT,
                                                  FEATURE_WVALUE, self.interface, HID_FEATURE_REPORT_BYTES,
                                                  timeout_ms)
            except OSError as exc:
                log.debug("feature read-back failed: %s", exc)
                return None

    def disable_lizard_mode(self) -> None:
        for report in lizard_off_sequence():
            self.send_feature(report)
        self.get_feature()  # SDL: "There may be a lingering report read back after changing settings."
        log.info("lizard mode disabled")

    def heartbeat(self) -> None:
        for report in heartbeat_sequence():
            self.send_feature(report)
        self.get_feature()
        self.heartbeats += 1

    def rumble(self, left: int, right: int) -> None:
        try:
            self.send_feature(cmd_rumble(left, right), timeout_ms=200)
        except (OSError, NeptuneError) as exc:
            log.debug("rumble failed: %s", exc)

    def read(self, timeout: float) -> Optional[ControllerState]:
        usb_device = self.usb_device
        if usb_device is None:
            raise NeptuneError("device not open")
        data = usb_device.interrupt_in(self.ep_in, max(1, int(timeout * 1000)))
        if data is None:
            return None
        state = parse_report(data, time.monotonic(), self.with_sensors)
        if state is None:
            self.other_packets += 1
            return None
        self.reports += 1
        return state

    def read_raw(self, timeout: float) -> Optional[bytes]:
        usb_device = self.usb_device
        if usb_device is None:
            raise NeptuneError("device not open")
        return usb_device.interrupt_in(self.ep_in, max(1, int(timeout * 1000)))

    def _heartbeat_loop(self) -> None:
        while not self._heartbeat_stop.wait(self.heartbeat_s):
            try:
                self.heartbeat()
            except OSError as exc:
                self.heartbeat_errors += 1
                if self.heartbeat_errors <= 3 or self.heartbeat_errors % 30 == 0:
                    log.warning("heartbeat failed (%d): %s", self.heartbeat_errors, exc)
            except NeptuneError as exc:
                log.debug("heartbeat stops: %s", exc)
                break
