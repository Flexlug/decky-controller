"""raw-gadget transport: descriptors come from the :class:`Profile`, the report endpoints are enabled on
SET_CONFIGURATION and data moves with EP_WRITE / EP_READ on three worker threads (event loop, IN writer,
OUT reader).

Learned on hardware (``drivers/usb/gadget/legacy/raw_gadget.c``): after DISCONNECT/RESET the UDC has stopped
the endpoints but raw-gadget still counts them enabled until EP_DISABLE — join the data threads first, then
disable each handle once (EINVAL = already gone).
"""
from __future__ import annotations

import ctypes
import errno
import os
import struct
import subprocess
import threading
import time
from typing import Optional

from deckhw.udc import UDC_STATE_CONFIGURED, Udc, udc_names

from ...platform.rawgadget.device import DEFAULT_DEVICE, RawGadgetDevice
from ...platform.rawgadget.ioctls import (
    EVENT_NAMES, SZ_EP_IO, USB_RAW_EVENT_CONNECT, USB_RAW_EVENT_CONTROL, USB_RAW_EVENT_DISCONNECT,
    USB_RAW_EVENT_RESET, USB_RAW_EVENT_RESUME, USB_RAW_EVENT_SUSPEND, USB_RAW_IOCTL_EP_WRITE, USB_SPEED,
)
from ...profiles.base import GadgetDescriptors, Profile
from ...util.ioctl import ioctl
from ...util.log import get_logger
from ..base import (
    FeedbackCallback, ReportSlot, TransportError, TransportMetrics, install_cancel_signal_handler,
    join_with_interrupts,
)
from .control import ControlHandler

log = get_logger("raw_gadget")

DEFAULT_DRIVER = "dwc3-gadget"
#: errnos that mean "the endpoint is gone" (host went away / config torn down) — not worth a warning
_EP_GONE_ERRNOS = (errno.ESHUTDOWN, errno.ENODEV, errno.ECONNRESET, errno.EINVAL, errno.EPIPE, errno.EBUSY)


class UsbRawGadgetTransport:
    name = "raw"

    def __init__(self, udc: Optional[str] = None, driver: str = DEFAULT_DRIVER, speed: str = "high",
                 dev_path: str = DEFAULT_DEVICE, sysfs: str = "/sys", modprobe: bool = True,
                 log_control: bool = True) -> None:
        if speed not in USB_SPEED:
            raise TransportError(f"unknown speed {speed!r}")
        self.udc = udc
        self.driver = driver
        self.speed = speed
        self.dev_path = dev_path
        self.sysfs = sysfs
        self.modprobe = modprobe
        self.log_control = log_control
        self.profile: Optional[Profile] = None
        self.descriptors: Optional[GadgetDescriptors] = None
        self.on_feedback: Optional[FeedbackCallback] = None
        self.device: Optional[RawGadgetDevice] = None
        self._slot = ReportSlot()
        self._metrics = TransportMetrics()
        self._stop = threading.Event()
        self._configured = False
        self._ep_in: Optional[int] = None
        self._ep_out: Optional[int] = None
        self._ep_lock = threading.RLock()
        self._event_thread: Optional[threading.Thread] = None
        self._in_thread: Optional[threading.Thread] = None
        self._out_thread: Optional[threading.Thread] = None
        self._error: Optional[BaseException] = None
        self._udc_watch: Optional[Udc] = None
        self.control_requests = 0
        self.generation = 0

    @property
    def error(self) -> Optional[BaseException]:
        return self._error

    def metrics(self) -> TransportMetrics:
        self._metrics.dropped = self._slot.dropped
        return self._metrics

    def connected(self) -> bool:
        if not self._configured:
            return False
        if self._udc_watch is not None:
            state = self._udc_watch.state()
            if state is not None and state != UDC_STATE_CONFIGURED:
                return False
        return True

    def start(self, profile: Profile, on_feedback: Optional[FeedbackCallback] = None) -> None:
        if self.device is not None:
            raise TransportError("transport already started")
        self.profile = profile
        self.descriptors = profile.gadget_descriptors()
        self.on_feedback = on_feedback
        if not install_cancel_signal_handler():
            log.warning("cancel signal handler not installed (not main thread): teardown may be slow")
        if not os.path.exists(self.dev_path) and self.modprobe:
            subprocess.run(["modprobe", "raw_gadget"], check=False, capture_output=True)
        if not os.path.exists(self.dev_path):
            raise TransportError(f"{self.dev_path} missing (modprobe raw_gadget failed?)")
        udc = self.udc
        if udc is None:
            available = udc_names(self.sysfs)
            if not available:
                raise TransportError("no UDC in /sys/class/udc: DRD disabled in BIOS, or Deck is USB host (dock attached?)")
            udc = available[0]
        self.udc = udc
        self._udc_watch = Udc(self.sysfs, udc)
        bound_function = self._udc_watch.function()
        if bound_function:
            log.warning("UDC %s already has function %r bound (stale gadget?) — trying anyway", udc, bound_function)
        try:
            self.device = RawGadgetDevice(self.dev_path)
        except OSError as exc:
            raise TransportError(f"cannot open {self.dev_path}: {exc}") from exc
        try:
            self.device.init(self.driver, udc, USB_SPEED[self.speed])
            self.device.run()
        except OSError as exc:
            self.device.close()
            self.device = None
            raise TransportError(f"raw-gadget init/run failed on {udc}: {exc}") from exc
        self._stop.clear()
        self._error = None
        self._event_thread = threading.Thread(target=self._event_loop, name="rawgadget-ev", daemon=True)
        self._event_thread.start()
        log.info("raw-gadget up: udc=%s driver=%s speed=%s vid=%04x pid=%04x", udc, self.driver, self.speed,
                 self.descriptors.vid, self.descriptors.pid)

    def send(self, report: bytes) -> None:
        if self._configured:
            self._slot.put(report)

    def stop(self) -> None:
        self._stop.set()
        device = self.device
        if device is None:
            return
        join_with_interrupts([self._event_thread], timeout=1.5)
        self._teardown_eps(reason="stop")
        self.device = None
        device.close()
        self._event_thread = None
        log.info("raw-gadget down (sent=%d dropped=%d)", self._metrics.sent, self._slot.dropped)

    def _event_loop(self) -> None:
        device = self.device
        assert device is not None
        while not self._stop.is_set():
            try:
                event_type, data = device.event_fetch()
            except OSError as exc:
                if exc.errno == errno.EINTR or self._stop.is_set():
                    if self._stop.is_set():
                        break
                    continue
                self._error = TransportError(f"raw-gadget event fetch failed: {exc}")
                log.error("%s", self._error)
                self._configured = False
                break
            try:
                self._dispatch_event(device, event_type, data)
            except OSError as exc:
                if exc.errno == errno.EINTR and self._stop.is_set():
                    break
                log.warning("error while handling %s: %s", EVENT_NAMES.get(event_type, event_type), exc)
                if event_type == USB_RAW_EVENT_CONTROL:
                    self._abort_control(device)
            except Exception as exc:  # noqa: BLE001 - keep the event loop alive, but record it
                log.exception("unhandled error in raw-gadget event loop: %s", exc)
                self._error = exc
                if event_type == USB_RAW_EVENT_CONTROL:
                    self._abort_control(device)
                break

    def _dispatch_event(self, device: RawGadgetDevice, event_type: int, data: bytes) -> None:
        if event_type == USB_RAW_EVENT_CONTROL:
            self._handle_control(data)
        elif event_type == USB_RAW_EVENT_CONNECT:
            try:
                log.info("CONNECT; eps=%s", device.eps_info())
            except OSError:
                log.info("CONNECT")
        elif event_type == USB_RAW_EVENT_RESET:
            log.info("RESET")
            self._teardown_eps(reason="reset")
        elif event_type == USB_RAW_EVENT_DISCONNECT:
            log.info("DISCONNECT")
            self._teardown_eps(reason="disconnect")
        elif event_type == USB_RAW_EVENT_SUSPEND:
            log.info("SUSPEND")
        elif event_type == USB_RAW_EVENT_RESUME:
            log.info("RESUME")
        else:
            log.debug("event %d len=%d", event_type, len(data))

    @staticmethod
    def _abort_control(device: RawGadgetDevice) -> None:
        """STALL a half-handled EP0 request; otherwise raw-gadget answers every further setup packet with
        -EBUSY and the device never enumerates again. EBUSY from the stall itself = nothing pending."""
        try:
            device.ep0_stall()
        except OSError as exc:
            log.debug("ep0_stall after failed control handling: %s", exc)

    def _handle_control(self, raw: bytes) -> None:
        device, descriptors, profile = self.device, self.descriptors, self.profile
        assert device is not None and descriptors is not None and profile is not None
        self.control_requests += 1
        ControlHandler(descriptors, profile, high_speed=(self.speed == "high"),
                       configured=lambda: self._configured, set_configuration=self._set_configuration,
                       log_requests=self.log_control).handle(device, raw)

    def _set_configuration(self, value: int) -> None:
        with self._ep_lock:
            self._teardown_eps(reason="reconfigure")
            if value == 0:
                return
            device, descriptors = self.device, self.descriptors
            assert device is not None and descriptors is not None
            try:
                self._ep_in = device.ep_enable(descriptors.ep_in)
                self._ep_out = device.ep_enable(descriptors.ep_out) if descriptors.ep_out else None
                try:
                    device.vbus_draw(descriptors.max_power_ma)
                except OSError as exc:
                    log.debug("vbus_draw: %s", exc)
                device.configure()
            except OSError as exc:
                # free whatever got enabled so the host's retry starts clean; the event loop STALLs the request
                log.warning("SET_CONFIGURATION(%d) failed: %s — rolling endpoints back", value, exc)
                self._teardown_eps(reason="configure-failed")
                raise
            self.generation += 1
            self._slot.clear()
            self._configured = True
            generation = self.generation
            self._in_thread = threading.Thread(target=self._in_loop, args=(self._ep_in, generation),
                                               name="rawgadget-in", daemon=True)
            self._in_thread.start()
            if self._ep_out is not None:
                self._out_thread = threading.Thread(target=self._out_loop, args=(self._ep_out, generation),
                                                    name="rawgadget-out", daemon=True)
                self._out_thread.start()
            log.info("configured: ep_in handle=%s ep_out handle=%s", self._ep_in, self._ep_out)

    def _teardown_eps(self, reason: str) -> None:
        with self._ep_lock:
            self._configured = False
            threads = [self._in_thread, self._out_thread]
            self._in_thread = self._out_thread = None
            self._slot.clear()
            joined = join_with_interrupts(threads, timeout=1.0)
            if not joined:
                log.warning("data threads did not exit in time (%s)", reason)
            device = self.device
            handles, self._ep_in, self._ep_out = (self._ep_in, self._ep_out), None, None
            if device is None:
                return
            # with the data threads joined one EP_DISABLE per handle is enough; only a thread still stuck in
            # EP_READ/EP_WRITE makes the kernel answer EINVAL ("waiting for urb completion") — retry then
            attempts = 1 if joined else 10
            for handle in handles:
                if handle is not None:
                    self._disable_endpoint(device, handle, attempts, reason)

    @staticmethod
    def _disable_endpoint(device: RawGadgetDevice, handle: int, attempts: int, reason: str) -> None:
        for attempt in range(1, attempts + 1):
            try:
                device.ep_disable(handle)
                return
            except OSError as exc:
                if exc.errno == errno.EINVAL and attempt < attempts:
                    time.sleep(0.05)
                    continue
                if exc.errno in (errno.EINVAL, errno.EBUSY):
                    # EINVAL: already disabled; EBUSY: gadget already unbound — expected
                    log.debug("ep_disable(%s) after %s: %s", handle, reason, exc)
                else:
                    log.warning("ep_disable(%s) after %s failed: %s", handle, reason, exc)
                return

    def _in_loop(self, handle: int, generation: int) -> None:
        device = self.device
        assert device is not None
        descriptors = self.descriptors
        max_packet = descriptors.ep_in_max_packet if descriptors else 64
        buffer = ctypes.create_string_buffer(SZ_EP_IO + max_packet)
        payload_address = ctypes.addressof(buffer) + SZ_EP_IO
        slot = self._slot
        fd = device.fd
        sent = 0
        while self._configured and not self._stop.is_set() and self.generation == generation:
            report = slot.take(0.25)
            if report is None:
                continue
            length = min(len(report), max_packet)
            struct.pack_into("<HHI", buffer, 0, handle, 0, length)
            ctypes.memmove(payload_address, report, length)
            try:
                ioctl(fd, USB_RAW_IOCTL_EP_WRITE, buffer)
                sent += 1
                self._metrics.sent += 1
            except OSError as exc:
                if exc.errno == errno.EINTR:
                    continue
                self._metrics.errors += 1
                if self._configured and not self._stop.is_set() and exc.errno not in _EP_GONE_ERRNOS:
                    log.warning("IN write error: %s", exc)
                else:
                    log.debug("IN write ended: %s", exc)
                break
        log.debug("IN loop ended after %d reports", sent)

    def _out_loop(self, handle: int, generation: int) -> None:
        device = self.device
        assert device is not None
        descriptors = self.descriptors
        profile = self.profile
        max_packet = descriptors.ep_out_max_packet if descriptors else 64
        while self._configured and not self._stop.is_set() and self.generation == generation:
            try:
                data = device.ep_read(handle, max_packet)
            except OSError as exc:
                if exc.errno == errno.EINTR:
                    continue
                if self._configured and not self._stop.is_set() and exc.errno not in _EP_GONE_ERRNOS:
                    log.warning("OUT read error: %s", exc)
                else:
                    log.debug("OUT read ended: %s", exc)
                break
            if not data or profile is None:
                continue
            self._metrics.out_reports += 1
            try:
                feedback = profile.on_output(data)
            except Exception as exc:  # noqa: BLE001
                log.warning("on_output failed: %s", exc)
                continue
            if feedback is None:
                continue
            log.debug("host -> %s: %s", feedback.kind, data.hex())
            if self.on_feedback is not None:
                try:
                    self.on_feedback(feedback)
                except Exception as exc:  # noqa: BLE001
                    log.warning("feedback callback failed: %s", exc)
        log.debug("OUT loop ended")
