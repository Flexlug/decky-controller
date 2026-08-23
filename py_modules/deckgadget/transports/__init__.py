"""Transports: how reports reach the PC (raw-gadget or configfs f_hid)."""
from __future__ import annotations

from typing import Optional

from deckgadget.transports.base import ReportSlot, Transport, TransportError, TransportMetrics


def make_transport(name: str, udc: Optional[str] = None) -> Transport:
    if name == "raw":
        from deckgadget.transports.rawgadget.transport import UsbRawGadgetTransport
        return UsbRawGadgetTransport(udc=udc)
    if name == "hid":
        from deckgadget.transports.usb_hid import UsbHidTransport
        return UsbHidTransport(udc=udc)
    raise ValueError(f"unknown transport {name!r}")


__all__ = ["Transport", "TransportError", "TransportMetrics", "ReportSlot", "make_transport"]
