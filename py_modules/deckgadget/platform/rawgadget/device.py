"""One ``/dev/raw-gadget`` fd with the ioctls as methods. ioctls go through ctypes (GIL released) and have
no timeouts — a blocked call is interrupted with the cancel signal and returns EINTR.

Learned on hardware (``drivers/usb/gadget/legacy/raw_gadget.c``): EP_DISABLE / VBUS_DRAW (and the HALT/WEDGE
ioctls) take their value in the ioctl argument itself, not through a buffer — a pointer there made every
disable fail with EBUSY and leaked the endpoints in raw-gadget's bookkeeping until the fd was closed.
"""
from __future__ import annotations

import ctypes
import os
import struct
from typing import List, Optional, Tuple

from ...util.ioctl import ioctl
from .ioctls import (
    SZ_EP_DESC, SZ_EP_INFO, SZ_EP_IO, SZ_EPS_INFO, SZ_EVENT, SZ_INIT, UDC_NAME_LENGTH_MAX,
    USB_RAW_IOCTL_CONFIGURE, USB_RAW_IOCTL_EP0_READ, USB_RAW_IOCTL_EP0_STALL, USB_RAW_IOCTL_EP0_WRITE,
    USB_RAW_IOCTL_EP_DISABLE, USB_RAW_IOCTL_EP_ENABLE, USB_RAW_IOCTL_EP_READ, USB_RAW_IOCTL_EP_WRITE,
    USB_RAW_IOCTL_EPS_INFO, USB_RAW_IOCTL_EVENT_FETCH, USB_RAW_IOCTL_INIT, USB_RAW_IOCTL_RUN,
    USB_RAW_IOCTL_VBUS_DRAW,
)

DEFAULT_DEVICE = "/dev/raw-gadget"


class RawGadgetDevice:
    def __init__(self, path: str = DEFAULT_DEVICE) -> None:
        self.path = path
        self.fd = os.open(path, os.O_RDWR | os.O_CLOEXEC)

    def close(self) -> None:
        """Closing the fd unregisters the gadget driver; the UDC drops off the bus."""
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
        """Handle by value (``raw_ioctl_ep_disable``: ``int i = value``). EBUSY = bad handle / gadget
        unbound, EINVAL = not enabled or a URB still queued."""
        ioctl(self.fd, USB_RAW_IOCTL_EP_DISABLE, int(handle))

    def ep_write(self, handle: int, data: bytes) -> int:
        return self._ep_io(USB_RAW_IOCTL_EP_WRITE, handle, data)[0]

    def ep_read(self, handle: int, length: int) -> bytes:
        return self._ep_io(USB_RAW_IOCTL_EP_READ, handle, b"", length)[1]

    def configure(self) -> None:
        ioctl(self.fd, USB_RAW_IOCTL_CONFIGURE)

    def vbus_draw(self, milliamps: int) -> None:
        """By value in 2 mA units, i.e. the same number as bMaxPower (``raw_ioctl_vbus_draw``:
        ``usb_gadget_vbus_draw(gadget, 2 * value)``). dwc3 has no ``.vbus_draw`` -> EOPNOTSUPP, harmless."""
        ioctl(self.fd, USB_RAW_IOCTL_VBUS_DRAW, max(0, int(milliamps)) // 2)

    def eps_info(self) -> List[Tuple[str, int, int, int]]:
        buffer = ctypes.create_string_buffer(SZ_EPS_INFO)
        count = ioctl(self.fd, USB_RAW_IOCTL_EPS_INFO, buffer)
        endpoints = []
        for index in range(count):
            name, address, capabilities, max_packet = struct.unpack_from("<16sIIH", buffer.raw, index * SZ_EP_INFO)
            endpoints.append((name.split(b"\0")[0].decode(errors="replace"), address, capabilities, max_packet))
        return endpoints
