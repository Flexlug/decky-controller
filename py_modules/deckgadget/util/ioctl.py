"""ctypes-based ioctl(2) wrapper and the ``_IOC`` request-number macros.

Why not :func:`fcntl.ioctl`?  It holds the GIL for the whole call, so a blocking
``USB_RAW_IOCTL_EVENT_FETCH`` / ``USBDEVFS_BULK`` would stall every other Python
thread (see docs/HARDWARE.md, "Kernel gadget stack").  Calling ``ioctl`` through ``ctypes``
releases the GIL, so the IN-writer, OUT-reader, heartbeat and source-reader
threads really run concurrently.

Request numbers follow ``include/uapi/asm-generic/ioctl.h`` for x86_64
(``_IOC_NRBITS=8, _IOC_TYPEBITS=8, _IOC_SIZEBITS=14, _IOC_DIRBITS=2``).
"""
from __future__ import annotations

import ctypes
import os
from typing import Optional, Union

_IOC_NRBITS = 8
_IOC_TYPEBITS = 8
_IOC_SIZEBITS = 14
_IOC_NRSHIFT = 0
_IOC_TYPESHIFT = _IOC_NRSHIFT + _IOC_NRBITS
_IOC_SIZESHIFT = _IOC_TYPESHIFT + _IOC_TYPEBITS
_IOC_DIRSHIFT = _IOC_SIZESHIFT + _IOC_SIZEBITS

IOC_NONE = 0
IOC_WRITE = 1
IOC_READ = 2


def _ioc(direction: int, type_: Union[str, int], nr: int, size: int) -> int:
    """``_IOC(dir, type, nr, size)`` from asm-generic/ioctl.h."""
    type_code = ord(type_) if isinstance(type_, str) else type_
    if not 0 <= size < (1 << _IOC_SIZEBITS):
        raise ValueError(f"ioctl size {size} does not fit in {_IOC_SIZEBITS} bits")
    return ((direction << _IOC_DIRSHIFT) | (size << _IOC_SIZESHIFT) | (type_code << _IOC_TYPESHIFT)
            | (nr << _IOC_NRSHIFT))


def IO(type_: Union[str, int], nr: int) -> int:  # noqa: N802 - mirrors the C macro name
    return _ioc(IOC_NONE, type_, nr, 0)


def IOR(type_: Union[str, int], nr: int, size: int) -> int:  # noqa: N802
    return _ioc(IOC_READ, type_, nr, size)


def IOW(type_: Union[str, int], nr: int, size: int) -> int:  # noqa: N802
    return _ioc(IOC_WRITE, type_, nr, size)


def IOWR(type_: Union[str, int], nr: int, size: int) -> int:  # noqa: N802
    return _ioc(IOC_READ | IOC_WRITE, type_, nr, size)


# Keep the legacy lowercase alias used by the spike.
_IOC = _ioc

_libc: Optional[ctypes.CDLL] = None


def _get_libc() -> ctypes.CDLL:
    global _libc
    if _libc is None:
        libc = ctypes.CDLL(None, use_errno=True)
        libc.ioctl.restype = ctypes.c_int
        libc.ioctl.argtypes = [ctypes.c_int, ctypes.c_ulong, ctypes.c_void_p]
        _libc = libc
    return _libc


def ioctl(fd: int, request: int, arg: Optional[object] = None) -> int:
    """Call ``ioctl(fd, request, arg)`` via libc, releasing the GIL while blocked.

    ``arg`` may be ``None`` (passed as NULL), a ctypes object (``create_string_buffer``,
    a ``Structure`` instance, ``c_uint`` ...) or anything ``ctypes.byref`` accepts.
    Raises :class:`OSError` with the real errno on failure (including ``EINTR`` when a
    blocking call was interrupted by a signal — callers decide whether to retry).
    Returns the non-negative ioctl result.
    """
    if arg is None:
        arg_pointer: object = ctypes.c_void_p(0)
    elif isinstance(arg, (ctypes.Array, ctypes.Structure, ctypes.Union, ctypes._SimpleCData)):
        arg_pointer = ctypes.c_void_p(ctypes.addressof(arg))   # pointer to the caller-owned buffer
    elif isinstance(arg, int):
        arg_pointer = ctypes.c_void_p(arg)
    else:
        arg_pointer = arg
    # ctypes performs its own errno save/restore when use_errno=True.
    ctypes.set_errno(0)
    result = _get_libc().ioctl(fd, ctypes.c_ulong(request), arg_pointer)
    if result < 0:
        errno_value = ctypes.get_errno()
        raise OSError(errno_value, os.strerror(errno_value))
    return result
