"""Minimal synchronous usbfs client: claim/release an interface, control transfers, bulk/interrupt IN reads.

ioctl numbers and struct layouts follow ``include/uapi/linux/usbdevice_fs.h`` (x86_64); ``USBDEVFS_BULK``
works for interrupt endpoints too (``usb_bulk_msg`` handles them).
"""
from __future__ import annotations

import ctypes
import errno
import os
from typing import Optional

from deckgadget.util.ioctl import IO, IOR, IOW, IOWR, ioctl


class UsbfsCtrlTransfer(ctypes.Structure):
    """``struct usbdevfs_ctrltransfer`` (24 bytes on x86_64)."""

    _fields_ = [
        ("bRequestType", ctypes.c_uint8),
        ("bRequest", ctypes.c_uint8),
        ("wValue", ctypes.c_uint16),
        ("wIndex", ctypes.c_uint16),
        ("wLength", ctypes.c_uint16),
        ("timeout", ctypes.c_uint32),      # milliseconds
        ("data", ctypes.c_void_p),
    ]


class UsbfsBulkTransfer(ctypes.Structure):
    """``struct usbdevfs_bulktransfer`` (24 bytes on x86_64)."""

    _fields_ = [
        ("ep", ctypes.c_uint),
        ("len", ctypes.c_uint),
        ("timeout", ctypes.c_uint),        # milliseconds
        ("data", ctypes.c_void_p),
    ]


class UsbfsGetDriver(ctypes.Structure):
    """``struct usbdevfs_getdriver`` (interface + char driver[USBDEVFS_MAXDRIVERNAME + 1])."""

    _fields_ = [("interface", ctypes.c_uint), ("driver", ctypes.c_char * 256)]


class UsbfsDisconnectClaim(ctypes.Structure):
    """``struct usbdevfs_disconnect_claim``."""

    _fields_ = [("interface", ctypes.c_uint), ("flags", ctypes.c_uint), ("driver", ctypes.c_char * 256)]


USBDEVFS_CONTROL = IOWR("U", 0, ctypes.sizeof(UsbfsCtrlTransfer))
USBDEVFS_BULK = IOWR("U", 2, ctypes.sizeof(UsbfsBulkTransfer))
USBDEVFS_RESETEP = IOR("U", 3, ctypes.sizeof(ctypes.c_uint))
USBDEVFS_SETINTERFACE = IOR("U", 4, 8)
USBDEVFS_GETDRIVER = IOW("U", 8, ctypes.sizeof(UsbfsGetDriver))
USBDEVFS_CLAIMINTERFACE = IOR("U", 15, ctypes.sizeof(ctypes.c_uint))
USBDEVFS_RELEASEINTERFACE = IOR("U", 16, ctypes.sizeof(ctypes.c_uint))
USBDEVFS_IOCTL = IOWR("U", 18, 16)                                         # struct usbdevfs_ioctl
USBDEVFS_RESET = IO("U", 20)
USBDEVFS_CLEAR_HALT = IOR("U", 21, ctypes.sizeof(ctypes.c_uint))
USBDEVFS_DISCONNECT = IO("U", 22)                                          # sub-command of USBDEVFS_IOCTL
USBDEVFS_CONNECT = IO("U", 23)                                             # sub-command of USBDEVFS_IOCTL
USBDEVFS_DISCONNECT_CLAIM = IOR("U", 27, ctypes.sizeof(UsbfsDisconnectClaim))
USBDEVFS_DISCONNECT_CLAIM_IF_DRIVER = 0x01
USBDEVFS_DISCONNECT_CLAIM_EXCEPT_DRIVER = 0x02

DEFAULT_READ_LENGTH = 64


class UsbfsDevice:
    def __init__(self, path: str, read_length: int = DEFAULT_READ_LENGTH) -> None:
        self.path = path
        self.fd = os.open(path, os.O_RDWR | os.O_CLOEXEC)
        self.read_length = read_length
        self._read_buffer = ctypes.create_string_buffer(read_length)
        self._bulk_transfer = UsbfsBulkTransfer(0, read_length, 0, ctypes.cast(self._read_buffer, ctypes.c_void_p))

    def close(self) -> None:
        if self.fd >= 0:
            try:
                os.close(self.fd)
            finally:
                self.fd = -1

    def claim_interface(self, number: int) -> None:
        ioctl(self.fd, USBDEVFS_CLAIMINTERFACE, ctypes.c_uint(number))

    def release_interface(self, number: int) -> None:
        ioctl(self.fd, USBDEVFS_RELEASEINTERFACE, ctypes.c_uint(number))

    def disconnect_claim(self, number: int) -> None:
        """Detach whatever kernel driver holds the interface and claim it (fallback when claim is EBUSY)."""
        claim = UsbfsDisconnectClaim(number, 0, b"")
        ioctl(self.fd, USBDEVFS_DISCONNECT_CLAIM, claim)

    def get_driver(self, number: int) -> Optional[str]:
        query = UsbfsGetDriver(number, b"")
        try:
            ioctl(self.fd, USBDEVFS_GETDRIVER, query)
        except OSError as exc:
            if exc.errno == errno.ENODATA:
                return None
            raise
        return query.driver.decode(errors="replace")

    def control_out(self, request_type: int, request: int, value: int, index: int, data: bytes,
                    timeout_ms: int = 1000) -> int:
        length = len(data)
        buffer = ctypes.create_string_buffer(max(1, length))
        if length:
            ctypes.memmove(buffer, data, length)
        transfer = UsbfsCtrlTransfer(request_type, request, value, index, length, timeout_ms,
                                     ctypes.cast(buffer, ctypes.c_void_p))
        return ioctl(self.fd, USBDEVFS_CONTROL, transfer)

    def control_in(self, request_type: int, request: int, value: int, index: int, length: int,
                   timeout_ms: int = 1000) -> bytes:
        buffer = ctypes.create_string_buffer(max(1, length))
        transfer = UsbfsCtrlTransfer(request_type | 0x80, request, value, index, length, timeout_ms,
                                     ctypes.cast(buffer, ctypes.c_void_p))
        received = ioctl(self.fd, USBDEVFS_CONTROL, transfer)
        return buffer.raw[:received]

    def interrupt_in(self, endpoint_address: int, timeout_ms: int) -> Optional[bytes]:
        """One ``USBDEVFS_BULK`` read on an (interrupt) IN endpoint; ``None`` on timeout/EINTR."""
        self._bulk_transfer.ep = endpoint_address
        self._bulk_transfer.len = self.read_length
        self._bulk_transfer.timeout = timeout_ms
        try:
            received = ioctl(self.fd, USBDEVFS_BULK, self._bulk_transfer)
        except OSError as exc:
            if exc.errno in (errno.ETIMEDOUT, errno.EINTR, errno.EAGAIN):
                return None
            raise
        return self._read_buffer.raw[:received]
