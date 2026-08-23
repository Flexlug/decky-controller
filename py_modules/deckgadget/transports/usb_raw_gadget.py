"""raw-gadget transport: user space answers every EP0 request itself (descriptors come from the
:class:`Profile`), enables the report endpoints on SET_CONFIGURATION and moves data with EP_WRITE /
EP_READ.  Three worker threads (event loop, IN writer, OUT reader); ioctls go through ``util.ioctl``
(ctypes, GIL released).

Learned on hardware (``drivers/usb/gadget/legacy/raw_gadget.c``):

* ioctls have no timeouts — a blocked EP_READ/EP_WRITE/EVENT_FETCH is interrupted with
  :data:`~deckgadget.transports.base.CANCEL_SIGNAL` and returns EINTR.
* EP_DISABLE / VBUS_DRAW (and EP_SET_HALT/EP_CLEAR_HALT/EP_SET_WEDGE) take their value *in the ioctl
  argument*, not through a buffer; passing a pointer made every disable fail with EBUSY and leaked the
  endpoints in raw-gadget's bookkeeping until the fd was closed.
* After DISCONNECT/RESET the UDC has stopped the endpoints but raw-gadget still counts them enabled
  until EP_DISABLE: join the data threads first, then disable each handle once (EINVAL = already gone).
* A half-answered EP0 request makes raw-gadget answer every further setup packet with -EBUSY while
  ``ep0_in_pending``/``ep0_out_pending`` is set, so any failure inside control handling ends in EP0_STALL.

ABI: ``include/uapi/linux/usb/raw_gadget.h``.
"""
from __future__ import annotations

import ctypes
import errno
import os
import struct
import subprocess
import threading
import time
from typing import List, Optional, Tuple

from deckhw.udc import UDC_STATE_CONFIGURED, Udc, udc_names
from ..profiles.base import (
    USB_DT_CONFIG, USB_DT_DEVICE, USB_DT_DEVICE_QUALIFIER, USB_DT_OTHER_SPEED_CONFIG, USB_DT_STRING,
    USB_RECIP_DEVICE, USB_REQ_CLEAR_FEATURE, USB_REQ_GET_CONFIGURATION, USB_REQ_GET_DESCRIPTOR,
    USB_REQ_GET_INTERFACE, USB_REQ_GET_STATUS, USB_REQ_SET_CONFIGURATION, USB_REQ_SET_FEATURE,
    USB_REQ_SET_INTERFACE, USB_TYPE_STANDARD, GadgetDescriptors, Profile, SetupPacket,
)
from ..util.ioctl import IO, IOR, IOW, IOWR, ioctl
from ..util.log import get_logger
from .base import (
    FeedbackCallback, ReportSlot, TransportError, TransportMetrics, install_cancel_signal_handler,
    interrupt_thread, join_with_interrupts,
)

log = get_logger("raw_gadget")

UDC_NAME_LENGTH_MAX = 128
SZ_INIT = UDC_NAME_LENGTH_MAX * 2 + 1      # struct usb_raw_init {u8 driver[128]; u8 device[128]; u8 speed}
SZ_EVENT = 8                               # struct usb_raw_event {u32 type; u32 length; u8 data[]}
SZ_EP_IO = 8                               # struct usb_raw_ep_io {u16 ep; u16 flags; u32 length; u8 data[]}
SZ_EP_DESC = 9                             # struct usb_endpoint_descriptor (packed)
SZ_U32 = 4
USB_RAW_EPS_NUM_MAX = 30
SZ_EP_INFO = 32                            # struct usb_raw_ep_info {u8 name[16]; u32 addr; caps u32; limits 8}
SZ_EPS_INFO = USB_RAW_EPS_NUM_MAX * SZ_EP_INFO

USB_RAW_IOCTL_INIT = IOW("U", 0, SZ_INIT)
USB_RAW_IOCTL_RUN = IO("U", 1)
USB_RAW_IOCTL_EVENT_FETCH = IOR("U", 2, SZ_EVENT)
USB_RAW_IOCTL_EP0_WRITE = IOW("U", 3, SZ_EP_IO)
USB_RAW_IOCTL_EP0_READ = IOWR("U", 4, SZ_EP_IO)
USB_RAW_IOCTL_EP_ENABLE = IOW("U", 5, SZ_EP_DESC)
USB_RAW_IOCTL_EP_DISABLE = IOW("U", 6, SZ_U32)
USB_RAW_IOCTL_EP_WRITE = IOW("U", 7, SZ_EP_IO)
USB_RAW_IOCTL_EP_READ = IOWR("U", 8, SZ_EP_IO)
USB_RAW_IOCTL_CONFIGURE = IO("U", 9)
USB_RAW_IOCTL_VBUS_DRAW = IOW("U", 10, SZ_U32)
USB_RAW_IOCTL_EPS_INFO = IOR("U", 11, SZ_EPS_INFO)
USB_RAW_IOCTL_EP0_STALL = IO("U", 12)
USB_RAW_IOCTL_EP_SET_HALT = IOW("U", 13, SZ_U32)
USB_RAW_IOCTL_EP_CLEAR_HALT = IOW("U", 14, SZ_U32)
USB_RAW_IOCTL_EP_SET_WEDGE = IOW("U", 15, SZ_U32)

USB_RAW_EVENT_CONNECT = 1
USB_RAW_EVENT_CONTROL = 2
USB_RAW_EVENT_SUSPEND = 3
USB_RAW_EVENT_RESUME = 4
USB_RAW_EVENT_RESET = 5
USB_RAW_EVENT_DISCONNECT = 6
EVENT_NAMES = {1: "CONNECT", 2: "CONTROL", 3: "SUSPEND", 4: "RESUME", 5: "RESET", 6: "DISCONNECT"}

#: enum usb_device_speed (include/uapi/linux/usb/ch9.h)
USB_SPEED = {"low": 1, "full": 2, "high": 3, "super": 5, "super-plus": 6}

DEFAULT_DEVICE = "/dev/raw-gadget"
DEFAULT_DRIVER = "dwc3-gadget"
#: errnos that mean "the endpoint is gone" (host went away / config torn down) — not worth a warning
_EP_GONE_ERRNOS = (errno.ESHUTDOWN, errno.ENODEV, errno.ECONNRESET, errno.EINVAL, errno.EPIPE, errno.EBUSY)


class RawGadgetDevice:
    """Thin ioctl wrapper around one ``/dev/raw-gadget`` fd."""

    def __init__(self, path: str = DEFAULT_DEVICE) -> None:
        self.path = path
        self.fd = os.open(path, os.O_RDWR | os.O_CLOEXEC)

    def close(self) -> None:
        fd, self.fd = self.fd, -1
        if fd >= 0:
            try:
                os.close(fd)
            except OSError:
                pass

    def init(self, driver: str, device: str, speed: int) -> None:
        arg = ctypes.create_string_buffer(
            driver.encode().ljust(UDC_NAME_LENGTH_MAX, b"\0") + device.encode().ljust(UDC_NAME_LENGTH_MAX, b"\0")
            + bytes([speed]), SZ_INIT)
        ioctl(self.fd, USB_RAW_IOCTL_INIT, arg)

    def run(self) -> None:
        ioctl(self.fd, USB_RAW_IOCTL_RUN)

    def event_fetch(self, max_length: int = 256) -> Tuple[int, bytes]:
        buffer = ctypes.create_string_buffer(struct.pack("<II", 0, max_length) + b"\0" * max_length,
                                             SZ_EVENT + max_length)
        ioctl(self.fd, USB_RAW_IOCTL_EVENT_FETCH, buffer)
        event_type, length = struct.unpack_from("<II", buffer.raw, 0)
        return event_type, buffer.raw[SZ_EVENT:SZ_EVENT + length]

    def _ep_io(self, request: int, endpoint: int, data: bytes, read_length: Optional[int] = None) -> Tuple[int, bytes]:
        payload = data if read_length is None else b"\0" * read_length
        length = len(data) if read_length is None else read_length
        buffer = ctypes.create_string_buffer(struct.pack("<HHI", endpoint, 0, length) + payload,
                                             SZ_EP_IO + len(payload))
        transferred = ioctl(self.fd, request, buffer)
        return transferred, buffer.raw[SZ_EP_IO:SZ_EP_IO + max(transferred, 0)]

    def ep0_write(self, data: bytes) -> int:
        return self._ep_io(USB_RAW_IOCTL_EP0_WRITE, 0, data)[0]

    def ep0_read(self, length: int) -> bytes:
        return self._ep_io(USB_RAW_IOCTL_EP0_READ, 0, b"", length)[1]

    def ep0_stall(self) -> None:
        ioctl(self.fd, USB_RAW_IOCTL_EP0_STALL)

    def ep_enable(self, endpoint_descriptor: bytes) -> int:
        buffer = ctypes.create_string_buffer(bytes(endpoint_descriptor) + b"\0\0", SZ_EP_DESC)
        return ioctl(self.fd, USB_RAW_IOCTL_EP_ENABLE, buffer)

    def ep_disable(self, handle: int) -> None:
        # handle by value (raw_ioctl_ep_disable: ``int i = value``); EBUSY = bad handle / gadget unbound,
        # EINVAL = not enabled or a URB still queued
        ioctl(self.fd, USB_RAW_IOCTL_EP_DISABLE, int(handle))

    def ep_write(self, handle: int, data: bytes) -> int:
        return self._ep_io(USB_RAW_IOCTL_EP_WRITE, handle, data)[0]

    def ep_read(self, handle: int, length: int) -> bytes:
        return self._ep_io(USB_RAW_IOCTL_EP_READ, handle, b"", length)[1]

    def configure(self) -> None:
        ioctl(self.fd, USB_RAW_IOCTL_CONFIGURE)

    def vbus_draw(self, milliamps: int) -> None:
        # by value in 2 mA units, i.e. the same number as bMaxPower (raw_ioctl_vbus_draw:
        # ``usb_gadget_vbus_draw(gadget, 2 * value)``); dwc3 has no .vbus_draw -> EOPNOTSUPP, harmless
        ioctl(self.fd, USB_RAW_IOCTL_VBUS_DRAW, max(0, int(milliamps)) // 2)

    def eps_info(self) -> List[Tuple[str, int, int, int]]:
        buffer = ctypes.create_string_buffer(SZ_EPS_INFO)
        count = ioctl(self.fd, USB_RAW_IOCTL_EPS_INFO, buffer)
        endpoints = []
        for i in range(count):
            name, address, capabilities, max_packet = struct.unpack_from("<16sIIH", buffer.raw, i * SZ_EP_INFO)
            endpoints.append((name.split(b"\0")[0].decode(errors="replace"), address, capabilities, max_packet))
        return endpoints


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
        # closing the fd unregisters the gadget driver; the UDC drops off the bus
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

    @staticmethod
    def _abort_control(device: RawGadgetDevice) -> None:
        """STALL a half-handled EP0 request; otherwise raw-gadget answers every further setup packet
        with -EBUSY and the device never enumerates again.  EBUSY from the stall itself = nothing pending."""
        try:
            device.ep0_stall()
        except OSError as exc:
            log.debug("ep0_stall after failed control handling: %s", exc)

    def _handle_control(self, raw: bytes) -> None:
        device = self.device
        descriptors = self.descriptors
        profile = self.profile
        assert device is not None and descriptors is not None and profile is not None
        setup = SetupPacket.unpack(raw)
        self.control_requests += 1
        if self.log_control:
            log.debug("ep0: %s", setup.describe())

        def reply(data: bytes) -> None:
            # raw-gadget marks an IN request with wLength == 0 as OUT-pending (gadget_setup), so EP0_WRITE
            # would fail with EBUSY: finish it with the zero-length status read instead
            if setup.wLength == 0:
                device.ep0_read(0)
            else:
                device.ep0_write(bytes(data[:setup.wLength]))

        def ack() -> None:
            device.ep0_read(0)

        def stall() -> None:
            device.ep0_stall()

        def read_data() -> bytes:
            return device.ep0_read(setup.wLength) if setup.wLength else b""

        if setup.req_type == USB_TYPE_STANDARD and setup.recipient == USB_RECIP_DEVICE:
            request = setup.bRequest
            if request == USB_REQ_GET_DESCRIPTOR and setup.dir_in:
                descriptor_type, descriptor_index = setup.wValue >> 8, setup.wValue & 0xFF
                if descriptor_type == USB_DT_DEVICE:
                    return reply(descriptors.device_descriptor())
                if descriptor_type == USB_DT_CONFIG:
                    return reply(descriptors.config_descriptor(USB_DT_CONFIG))
                if descriptor_type == USB_DT_STRING:
                    string_descriptor = descriptors.string(descriptor_index)
                    return reply(string_descriptor) if string_descriptor else stall()
                if descriptor_type == USB_DT_DEVICE_QUALIFIER:
                    return (reply(descriptors.qualifier_descriptor())
                            if descriptors.high_speed and self.speed == "high" else stall())
                if descriptor_type == USB_DT_OTHER_SPEED_CONFIG:
                    return (reply(descriptors.config_descriptor(USB_DT_OTHER_SPEED_CONFIG))
                            if descriptors.high_speed and self.speed == "high" else stall())
                return stall()  # BOS, MS OS 0xEE, …: STALL is accepted by Linux and Windows
            if request == USB_REQ_SET_CONFIGURATION and not setup.dir_in:
                self._set_configuration(setup.wValue & 0xFF)
                return ack()
            if request == USB_REQ_GET_CONFIGURATION and setup.dir_in:
                return reply(bytes([1 if self._configured else 0]))
            if request == USB_REQ_GET_STATUS and setup.dir_in:
                return reply(b"\x00\x00")
            if request in (USB_REQ_CLEAR_FEATURE, USB_REQ_SET_FEATURE) and not setup.dir_in:
                return ack()
            return stall()
        if setup.req_type == USB_TYPE_STANDARD and setup.bRequest in (USB_REQ_SET_INTERFACE, USB_REQ_GET_INTERFACE,
                                                                        USB_REQ_GET_STATUS, USB_REQ_CLEAR_FEATURE,
                                                                        USB_REQ_SET_FEATURE):
            # interface / endpoint recipients
            if setup.bRequest == USB_REQ_GET_INTERFACE and setup.dir_in:
                return reply(b"\x00")
            if setup.bRequest == USB_REQ_GET_STATUS and setup.dir_in:
                return reply(b"\x00\x00")
            if not setup.dir_in:
                return ack()
            return stall()
        # Everything else (interface GET_DESCRIPTOR for HID, class, vendor) -> profile
        result = profile.handle_control(setup, read_data)
        if result is None:
            return stall()
        if setup.dir_in:
            return reply(result)
        # OUT: the profile consumed the data stage (if any) via read_data(); finish the status stage
        if setup.wLength == 0:
            return ack()

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
                if handle is None:
                    continue
                for attempt in range(1, attempts + 1):
                    try:
                        device.ep_disable(handle)
                        break
                    except OSError as exc:
                        if exc.errno == errno.EINVAL and attempt < attempts:
                            time.sleep(0.05)
                            continue
                        if exc.errno in (errno.EINVAL, errno.EBUSY):
                            # EINVAL: already disabled; EBUSY: gadget already unbound — expected
                            log.debug("ep_disable(%s) after %s: %s", handle, reason, exc)
                        else:
                            log.warning("ep_disable(%s) after %s failed: %s", handle, reason, exc)
                        break

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
