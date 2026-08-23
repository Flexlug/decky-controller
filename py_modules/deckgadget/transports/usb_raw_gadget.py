"""Production raw-gadget transport (port of the original raw-gadget spike).

``/dev/raw-gadget`` (``CONFIG_USB_RAW_GADGET``) hands the whole device to user space: we
answer every EP0 request ourselves (descriptors come from the :class:`Profile`), enable
the report endpoints on SET_CONFIGURATION and move data with ``EP_WRITE``/``EP_READ``.
All ioctls go through ``util.ioctl`` (ctypes, GIL released) so the three worker threads
(event loop, IN writer, OUT reader) run truly concurrently.

Teardown subtleties learned on hardware (docs/HARDWARE.md):

* raw-gadget ioctls have no timeouts; a blocked ``EP_READ``/``EP_WRITE``/``EVENT_FETCH`` is
  ``wait_for_completion_interruptible`` in the kernel, so we interrupt the worker threads
  with :data:`~deckgadget.transports.base.CANCEL_SIGNAL` (``pthread_kill``) and they see
  ``EINTR``.
* ``EP_DISABLE`` / ``VBUS_DRAW`` (and ``EP_SET_HALT``/``EP_CLEAR_HALT``/``EP_SET_WEDGE``) take
  their value *in the ioctl argument itself*, not through a buffer (raw_gadget.c
  ``raw_ioctl_ep_disable``: ``int i = value``; ``raw_ioctl_vbus_draw``:
  ``usb_gadget_vbus_draw(gadget, 2 * value)``).  The first spike passed a pointer, so every
  disable failed with ``EBUSY`` ("invalid endpoint") — the "cosmetic EBUSY after DISCONNECT"
  in the spike notes — and the endpoints stayed enabled in raw-gadget's bookkeeping until the
  fd was closed (a later ``EP_ENABLE`` could then run out of endpoints).
* After ``DISCONNECT``/``RESET`` the UDC has already stopped the endpoints, but raw-gadget
  still counts them as enabled until ``EP_DISABLE``.  We join the IN/OUT threads first (so no
  URB is queued), then disable each handle once; ``EINVAL`` (already disabled) is expected.
* A half-answered EP0 request is fatal: raw-gadget answers every further setup packet with
  ``-EBUSY`` while ``ep0_in_pending``/``ep0_out_pending`` is set (``gadget_setup``), so any
  failure inside control handling is terminated with ``EP0_STALL``.

ABI: ``include/uapi/linux/usb/raw_gadget.h`` (struct sizes: usb_raw_init 257,
usb_raw_event 8 + data, usb_raw_ep_io 8 + data, usb_endpoint_descriptor 9,
usb_raw_eps_info 30*32 = 960).
"""
from __future__ import annotations

import ctypes
import errno
import os
import struct
import subprocess
import threading
import time
from typing import Dict, List, Optional, Tuple

from ..platform import usb_role
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

# --- raw_gadget.h ---------------------------------------------------------------------
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

    def event_fetch(self, maxlen: int = 256) -> Tuple[int, bytes]:
        buf = ctypes.create_string_buffer(struct.pack("<II", 0, maxlen) + b"\0" * maxlen, SZ_EVENT + maxlen)
        ioctl(self.fd, USB_RAW_IOCTL_EVENT_FETCH, buf)
        typ, length = struct.unpack_from("<II", buf.raw, 0)
        return typ, buf.raw[SZ_EVENT:SZ_EVENT + length]

    def _ep_io(self, req: int, ep: int, data: bytes, read_len: Optional[int] = None) -> Tuple[int, bytes]:
        payload = data if read_len is None else b"\0" * read_len
        length = len(data) if read_len is None else read_len
        buf = ctypes.create_string_buffer(struct.pack("<HHI", ep, 0, length) + payload, SZ_EP_IO + len(payload))
        n = ioctl(self.fd, req, buf)
        return n, buf.raw[SZ_EP_IO:SZ_EP_IO + max(n, 0)]

    def ep0_write(self, data: bytes) -> int:
        return self._ep_io(USB_RAW_IOCTL_EP0_WRITE, 0, data)[0]

    def ep0_read(self, n: int) -> bytes:
        return self._ep_io(USB_RAW_IOCTL_EP0_READ, 0, b"", n)[1]

    def ep0_stall(self) -> None:
        ioctl(self.fd, USB_RAW_IOCTL_EP0_STALL)

    def ep_enable(self, desc7: bytes) -> int:
        buf = ctypes.create_string_buffer(bytes(desc7) + b"\0\0", SZ_EP_DESC)
        return ioctl(self.fd, USB_RAW_IOCTL_EP_ENABLE, buf)

    def ep_disable(self, handle: int) -> None:
        # Handle travels in the ioctl argument (raw_gadget.c raw_ioctl_ep_disable: ``int i = value``);
        # util.ioctl maps an int to c_void_p(value).  EBUSY = bad handle / gadget unbound,
        # EINVAL = not enabled (or a URB still queued on it).
        ioctl(self.fd, USB_RAW_IOCTL_EP_DISABLE, int(handle))

    def ep_write(self, handle: int, data: bytes) -> int:
        return self._ep_io(USB_RAW_IOCTL_EP_WRITE, handle, data)[0]

    def ep_read(self, handle: int, n: int) -> bytes:
        return self._ep_io(USB_RAW_IOCTL_EP_READ, handle, b"", n)[1]

    def configure(self) -> None:
        ioctl(self.fd, USB_RAW_IOCTL_CONFIGURE)

    def vbus_draw(self, ma: int) -> None:
        # By value, in 2 mA units (raw_gadget.c raw_ioctl_vbus_draw: ``usb_gadget_vbus_draw(gadget,
        # 2 * value)``) — i.e. the same number as bMaxPower (250 for 500 mA), like the reference
        # examples' ``usb_raw_vbus_draw(fd, usb_config.bMaxPower)``.  dwc3 has no .vbus_draw ->
        # EOPNOTSUPP from the kernel; callers treat that as harmless.
        ioctl(self.fd, USB_RAW_IOCTL_VBUS_DRAW, max(0, int(ma)) // 2)

    def eps_info(self) -> List[Tuple[str, int, int, int]]:
        buf = ctypes.create_string_buffer(SZ_EPS_INFO)
        n = ioctl(self.fd, USB_RAW_IOCTL_EPS_INFO, buf)
        out = []
        for i in range(n):
            name, addr, caps, maxp = struct.unpack_from("<16sIIH", buf.raw, i * SZ_EP_INFO)
            out.append((name.split(b"\0")[0].decode(errors="replace"), addr, caps, maxp))
        return out


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
        self.desc: Optional[GadgetDescriptors] = None
        self.on_feedback: Optional[FeedbackCallback] = None
        self.dev: Optional[RawGadgetDevice] = None
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
        self._udc_watch: Optional[usb_role.UdcWatcher] = None
        self.control_requests = 0
        self.generation = 0

    # --- Transport protocol -----------------------------------------------------------
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
            st = self._udc_watch.state()
            if st is not None and st != usb_role.UDC_STATE_CONFIGURED:
                return False
        return True

    def start(self, profile: Profile, on_feedback: Optional[FeedbackCallback] = None) -> None:
        if self.dev is not None:
            raise TransportError("transport already started")
        self.profile = profile
        self.desc = profile.gadget_descriptors()
        self.on_feedback = on_feedback
        if not install_cancel_signal_handler():
            log.warning("cancel signal handler not installed (not main thread): teardown may be slow")
        if not os.path.exists(self.dev_path) and self.modprobe:
            subprocess.run(["modprobe", "raw_gadget"], check=False, capture_output=True)
        if not os.path.exists(self.dev_path):
            raise TransportError(f"{self.dev_path} missing (modprobe raw_gadget failed?)")
        udc = self.udc
        if udc is None:
            udcs = usb_role.list_udcs(self.sysfs)
            if not udcs:
                raise TransportError("no UDC in /sys/class/udc: DRD disabled in BIOS, or Deck is USB host (dock attached?)")
            udc = udcs[0]
        self.udc = udc
        self._udc_watch = usb_role.UdcWatcher(udc, self.sysfs)
        busy = usb_role.udc_attr("function", udc, self.sysfs)
        if busy:
            log.warning("UDC %s already has function %r bound (stale gadget?) — trying anyway", udc, busy)
        try:
            self.dev = RawGadgetDevice(self.dev_path)
        except OSError as exc:
            raise TransportError(f"cannot open {self.dev_path}: {exc}") from exc
        try:
            self.dev.init(self.driver, udc, USB_SPEED[self.speed])
            self.dev.run()
        except OSError as exc:
            self.dev.close()
            self.dev = None
            raise TransportError(f"raw-gadget init/run failed on {udc}: {exc}") from exc
        self._stop.clear()
        self._error = None
        self._event_thread = threading.Thread(target=self._event_loop, name="rawgadget-ev", daemon=True)
        self._event_thread.start()
        log.info("raw-gadget up: udc=%s driver=%s speed=%s vid=%04x pid=%04x", udc, self.driver, self.speed,
                 self.desc.vid, self.desc.pid)

    def send(self, report: bytes) -> None:
        if self._configured:
            self._slot.put(report)

    def stop(self) -> None:
        self._stop.set()
        dev = self.dev
        if dev is None:
            return
        # 1. event thread (blocked in EVENT_FETCH) -> EINTR
        join_with_interrupts([self._event_thread], timeout=1.5)
        # 2. data threads + endpoint disable
        self._teardown_eps(reason="stop")
        # 3. closing the fd unregisters the gadget driver; the UDC drops off the bus
        self.dev = None
        dev.close()
        self._event_thread = None
        log.info("raw-gadget down (sent=%d dropped=%d)", self._metrics.sent, self._slot.dropped)

    # --- event loop ------------------------------------------------------------------
    def _event_loop(self) -> None:
        dev = self.dev
        assert dev is not None
        while not self._stop.is_set():
            try:
                typ, data = dev.event_fetch()
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
                if typ == USB_RAW_EVENT_CONTROL:
                    self._handle_control(data)
                elif typ == USB_RAW_EVENT_CONNECT:
                    try:
                        log.info("CONNECT; eps=%s", dev.eps_info())
                    except OSError:
                        log.info("CONNECT")
                elif typ == USB_RAW_EVENT_RESET:
                    log.info("RESET")
                    self._teardown_eps(reason="reset")
                elif typ == USB_RAW_EVENT_DISCONNECT:
                    log.info("DISCONNECT")
                    self._teardown_eps(reason="disconnect")
                elif typ == USB_RAW_EVENT_SUSPEND:
                    log.info("SUSPEND")
                elif typ == USB_RAW_EVENT_RESUME:
                    log.info("RESUME")
                else:
                    log.debug("event %d len=%d", typ, len(data))
            except OSError as exc:
                if exc.errno == errno.EINTR and self._stop.is_set():
                    break
                log.warning("error while handling %s: %s", EVENT_NAMES.get(typ, typ), exc)
                if typ == USB_RAW_EVENT_CONTROL:
                    self._abort_control(dev)
            except Exception as exc:  # noqa: BLE001 - keep the event loop alive, but record it
                log.exception("unhandled error in raw-gadget event loop: %s", exc)
                self._error = exc
                if typ == USB_RAW_EVENT_CONTROL:
                    self._abort_control(dev)
                break

    @staticmethod
    def _abort_control(dev: RawGadgetDevice) -> None:
        """Terminate a half-handled EP0 request with STALL (best effort).

        raw-gadget keeps ``ep0_in_pending``/``ep0_out_pending`` set until an EP0 read/write
        completes or ``EP0_STALL`` is issued and meanwhile answers *every* new setup packet with
        ``-EBUSY`` ("stalling, request already pending") — the device could never enumerate again
        until the daemon restarts.  ``EBUSY`` from the stall itself means nothing was pending
        (the reply had already gone out), which is fine.
        """
        try:
            dev.ep0_stall()
        except OSError as exc:
            log.debug("ep0_stall after failed control handling: %s", exc)

    def _handle_control(self, raw: bytes) -> None:
        dev = self.dev
        desc = self.desc
        profile = self.profile
        assert dev is not None and desc is not None and profile is not None
        setup = SetupPacket.unpack(raw)
        self.control_requests += 1
        if self.log_control:
            log.debug("ep0: %s", setup.describe())

        def reply(data: bytes) -> None:
            # raw-gadget marks an IN request with wLength == 0 as *OUT*-pending (gadget_setup:
            # ``if ((bRequestType & USB_DIR_IN) && wLength) ep0_in_pending else ep0_out_pending``),
            # so EP0_WRITE would fail with EBUSY ("wrong direction"): there is no data stage —
            # complete it with the zero-length status read, exactly like ack().
            if setup.wLength == 0:
                dev.ep0_read(0)
            else:
                dev.ep0_write(bytes(data[:setup.wLength]))

        def ack() -> None:
            dev.ep0_read(0)

        def stall() -> None:
            dev.ep0_stall()

        def read_data() -> bytes:
            return dev.ep0_read(setup.wLength) if setup.wLength else b""

        if setup.req_type == USB_TYPE_STANDARD and setup.recipient == USB_RECIP_DEVICE:
            req = setup.bRequest
            if req == USB_REQ_GET_DESCRIPTOR and setup.dir_in:
                dtype, dindex = setup.wValue >> 8, setup.wValue & 0xFF
                if dtype == USB_DT_DEVICE:
                    return reply(desc.device_descriptor())
                if dtype == USB_DT_CONFIG:
                    return reply(desc.config_descriptor(USB_DT_CONFIG))
                if dtype == USB_DT_STRING:
                    sd = desc.string(dindex)
                    return reply(sd) if sd else stall()
                if dtype == USB_DT_DEVICE_QUALIFIER:
                    return reply(desc.qualifier_descriptor()) if desc.high_speed and self.speed == "high" else stall()
                if dtype == USB_DT_OTHER_SPEED_CONFIG:
                    return (reply(desc.config_descriptor(USB_DT_OTHER_SPEED_CONFIG))
                            if desc.high_speed and self.speed == "high" else stall())
                return stall()  # BOS, MS OS 0xEE, ... -> STALL (accepted by Linux & Windows, see spike)
            if req == USB_REQ_SET_CONFIGURATION and not setup.dir_in:
                self._set_configuration(setup.wValue & 0xFF)
                return ack()
            if req == USB_REQ_GET_CONFIGURATION and setup.dir_in:
                return reply(bytes([1 if self._configured else 0]))
            if req == USB_REQ_GET_STATUS and setup.dir_in:
                return reply(b"\x00\x00")
            if req in (USB_REQ_CLEAR_FEATURE, USB_REQ_SET_FEATURE) and not setup.dir_in:
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

    # --- endpoints -------------------------------------------------------------------
    def _set_configuration(self, value: int) -> None:
        with self._ep_lock:
            self._teardown_eps(reason="reconfigure")
            if value == 0:
                return
            dev, desc = self.dev, self.desc
            assert dev is not None and desc is not None
            try:
                self._ep_in = dev.ep_enable(desc.ep_in)
                self._ep_out = dev.ep_enable(desc.ep_out) if desc.ep_out else None
                try:
                    dev.vbus_draw(desc.max_power_ma)
                except OSError as exc:
                    log.debug("vbus_draw: %s", exc)   # dwc3: EOPNOTSUPP (no .vbus_draw) — harmless
                dev.configure()
            except OSError as exc:
                # Free whatever got enabled so the host's retry starts from a clean slate; the
                # event loop STALLs the pending SET_CONFIGURATION.
                log.warning("SET_CONFIGURATION(%d) failed: %s — rolling endpoints back", value, exc)
                self._teardown_eps(reason="configure-failed")
                raise
            self.generation += 1
            self._slot.clear()
            self._configured = True
            gen = self.generation
            self._in_thread = threading.Thread(target=self._in_loop, args=(self._ep_in, gen),
                                               name="rawgadget-in", daemon=True)
            self._in_thread.start()
            if self._ep_out is not None:
                self._out_thread = threading.Thread(target=self._out_loop, args=(self._ep_out, gen),
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
            dev = self.dev
            handles, self._ep_in, self._ep_out = (self._ep_in, self._ep_out), None, None
            if dev is None:
                return
            # With the data threads joined no URB can be queued, so one EP_DISABLE per handle is
            # enough.  Only while a thread is still stuck in EP_READ/EP_WRITE may the kernel answer
            # EINVAL ("waiting for urb completion") — retry briefly in that case alone.
            attempts = 1 if joined else 10
            for h in handles:
                if h is None:
                    continue
                for attempt in range(1, attempts + 1):
                    try:
                        dev.ep_disable(h)
                        break
                    except OSError as exc:
                        if exc.errno == errno.EINVAL and attempt < attempts:
                            time.sleep(0.05)
                            continue
                        if exc.errno in (errno.EINVAL, errno.EBUSY):
                            # EINVAL: already disabled; EBUSY: gadget already unbound — expected
                            log.debug("ep_disable(%s) after %s: %s", h, reason, exc)
                        else:
                            log.warning("ep_disable(%s) after %s failed: %s", h, reason, exc)
                        break

    def _in_loop(self, handle: int, gen: int) -> None:
        dev = self.dev
        assert dev is not None
        desc = self.desc
        maxp = desc.ep_in_max_packet if desc else 64
        buf = ctypes.create_string_buffer(SZ_EP_IO + maxp)
        base = ctypes.addressof(buf) + SZ_EP_IO
        slot = self._slot
        fd = dev.fd
        sent = 0
        while self._configured and not self._stop.is_set() and self.generation == gen:
            rep = slot.take(0.25)
            if rep is None:
                continue
            n = min(len(rep), maxp)
            struct.pack_into("<HHI", buf, 0, handle, 0, n)
            ctypes.memmove(base, rep, n)
            try:
                ioctl(fd, USB_RAW_IOCTL_EP_WRITE, buf)
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

    def _out_loop(self, handle: int, gen: int) -> None:
        dev = self.dev
        assert dev is not None
        desc = self.desc
        profile = self.profile
        maxp = desc.ep_out_max_packet if desc else 64
        while self._configured and not self._stop.is_set() and self.generation == gen:
            try:
                data = dev.ep_read(handle, maxp)
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
                fb = profile.on_output(data)
            except Exception as exc:  # noqa: BLE001
                log.warning("on_output failed: %s", exc)
                continue
            if fb is None:
                continue
            log.debug("host -> %s: %s", fb.kind, data.hex())
            if self.on_feedback is not None:
                try:
                    self.on_feedback(fb)
                except Exception as exc:  # noqa: BLE001
                    log.warning("feedback callback failed: %s", exc)
        log.debug("OUT loop ended")
