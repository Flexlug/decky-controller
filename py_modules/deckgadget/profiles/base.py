"""Profile protocol + descriptor containers shared by the transports.

A *profile* decides how the Deck presents itself on the USB bus and how a
:class:`~deckgadget.state.ControllerState` is serialised into the report the
host expects.  It must be usable by both transports:

* raw-gadget (``transports/usb_raw_gadget.py``) needs full USB descriptors and a hook
  for class/vendor control requests on EP0 (:meth:`Profile.handle_control`);
* configfs f_hid (``transports/usb_hid.py``) only needs a HID report descriptor
  (:meth:`Profile.hid_function`) — profiles that are not plain HID return ``None``.
"""
from __future__ import annotations

import struct
from dataclasses import dataclass, field
from typing import Callable, Dict, Optional, Protocol, runtime_checkable

from ..state import ControllerState

# USB setup-packet helpers ------------------------------------------------------------

USB_DIR_IN = 0x80
USB_TYPE_MASK = 0x60
USB_TYPE_STANDARD = 0x00
USB_TYPE_CLASS = 0x20
USB_TYPE_VENDOR = 0x40
USB_RECIP_MASK = 0x1F
USB_RECIP_DEVICE = 0
USB_RECIP_INTERFACE = 1
USB_RECIP_ENDPOINT = 2

USB_REQ_GET_STATUS = 0
USB_REQ_CLEAR_FEATURE = 1
USB_REQ_SET_FEATURE = 3
USB_REQ_SET_ADDRESS = 5
USB_REQ_GET_DESCRIPTOR = 6
USB_REQ_SET_DESCRIPTOR = 7
USB_REQ_GET_CONFIGURATION = 8
USB_REQ_SET_CONFIGURATION = 9
USB_REQ_GET_INTERFACE = 10
USB_REQ_SET_INTERFACE = 11

USB_DT_DEVICE = 1
USB_DT_CONFIG = 2
USB_DT_STRING = 3
USB_DT_INTERFACE = 4
USB_DT_ENDPOINT = 5
USB_DT_DEVICE_QUALIFIER = 6
USB_DT_OTHER_SPEED_CONFIG = 7
USB_DT_BOS = 0x0F
USB_DT_HID = 0x21
USB_DT_HID_REPORT = 0x22


@dataclass(frozen=True)
class SetupPacket:
    """Decoded 8-byte USB setup packet."""

    bmRequestType: int
    bRequest: int
    wValue: int
    wIndex: int
    wLength: int

    @classmethod
    def unpack(cls, raw: bytes) -> "SetupPacket":
        return cls(*struct.unpack("<BBHHH", bytes(raw[:8])))

    @property
    def dir_in(self) -> bool:
        return bool(self.bmRequestType & USB_DIR_IN)

    @property
    def req_type(self) -> int:
        return self.bmRequestType & USB_TYPE_MASK

    @property
    def recipient(self) -> int:
        return self.bmRequestType & USB_RECIP_MASK

    def describe(self) -> str:
        return (f"bmRT=0x{self.bmRequestType:02x} bReq=0x{self.bRequest:02x} wValue=0x{self.wValue:04x} "
                f"wIndex=0x{self.wIndex:04x} wLength={self.wLength}")


def endpoint_descriptor(address: int, attributes: int, max_packet: int, interval: int) -> bytes:
    """7-byte standard endpoint descriptor (``struct usb_endpoint_descriptor`` without audio bytes)."""
    return struct.pack("<BBBBHB", 7, USB_DT_ENDPOINT, address, attributes, max_packet, interval)


def string_descriptor(text: Optional[str], index: int) -> Optional[bytes]:
    """USB string descriptor; index 0 is the LANGID list (en-US)."""
    if index == 0:
        return bytes([4, USB_DT_STRING, 0x09, 0x04])
    if text is None:
        return None
    data = text.encode("utf-16-le")
    return bytes([2 + len(data), USB_DT_STRING]) + data


@dataclass
class GadgetDescriptors:
    """Everything raw-gadget needs to enumerate: device descriptor fields, config body, strings."""

    vid: int
    pid: int
    bcd_device: int = 0x0100
    bcd_usb: int = 0x0200
    dev_class: int = 0
    dev_subclass: int = 0
    dev_protocol: int = 0
    ep0_max_packet: int = 64
    manufacturer: str = "Decky Controller"
    product: str = "Steam Deck Gamepad"
    serial: str = "DECK0001"
    #: extra string descriptors (index -> text) beyond the three standard ones (1..3)
    extra_strings: Dict[int, str] = field(default_factory=dict)
    #: interface/class/endpoint descriptors that follow the 9-byte configuration header
    config_body: bytes = b""
    num_interfaces: int = 1
    config_attributes: int = 0xA0            # bus powered + remote wakeup
    max_power_ma: int = 500
    #: 7-byte endpoint descriptors used for reports (IN mandatory, OUT optional)
    ep_in: bytes = b""
    ep_out: Optional[bytes] = None
    #: answer device_qualifier / other_speed_configuration (high-speed capable device)
    high_speed: bool = True

    def device_descriptor(self) -> bytes:
        return struct.pack("<BBHBBBBHHHBBBB", 18, USB_DT_DEVICE, self.bcd_usb, self.dev_class,
                           self.dev_subclass, self.dev_protocol, self.ep0_max_packet, self.vid, self.pid,
                           self.bcd_device, 1, 2, 3, 1)

    def config_descriptor(self, dtype: int = USB_DT_CONFIG) -> bytes:
        total = 9 + len(self.config_body)
        header = struct.pack("<BBHBBBBB", 9, dtype, total, self.num_interfaces, 1, 0,
                             self.config_attributes, self.max_power_ma // 2)
        return header + self.config_body

    def qualifier_descriptor(self) -> bytes:
        return struct.pack("<BBHBBBBBB", 10, USB_DT_DEVICE_QUALIFIER, self.bcd_usb, self.dev_class,
                           self.dev_subclass, self.dev_protocol, self.ep0_max_packet, 1, 0)

    def string(self, index: int) -> Optional[bytes]:
        table = {1: self.manufacturer, 2: self.product, 3: self.serial}
        table.update(self.extra_strings)
        return string_descriptor(table.get(index), index)

    @property
    def ep_in_address(self) -> int:
        return self.ep_in[2]

    @property
    def ep_in_max_packet(self) -> int:
        return struct.unpack_from("<H", self.ep_in, 4)[0]

    @property
    def ep_out_max_packet(self) -> int:
        return struct.unpack_from("<H", self.ep_out, 4)[0] if self.ep_out else 0


@dataclass
class HidFunction:
    """Parameters for a configfs ``functions/hid.usbN`` instance."""

    report_desc: bytes
    report_length: int
    protocol: int = 0
    subclass: int = 0
    vid: int = 0x1D6B
    pid: int = 0x0104
    manufacturer: str = "Decky Controller"
    product: str = "Steam Deck Gamepad"
    serial: str = "DECK0001"


@dataclass
class Feedback:
    """Decoded host -> device output report (rumble/LED)."""

    kind: str                   # "rumble" | "led" | "unknown"
    left: int = 0               # 0..65535 (big / low-frequency motor)
    right: int = 0              # 0..65535 (small / high-frequency motor)
    value: int = 0              # LED pattern etc.
    raw: bytes = b""


#: callback the transport uses to hand the OUT data stage to the profile: ``read_data() -> bytes``
ReadData = Callable[[], bytes]


@runtime_checkable
class Profile(Protocol):
    name: str
    report_length: int

    def pack(self, state: ControllerState) -> bytes:
        """Serialise ``state`` into one input report (exactly ``report_length`` bytes)."""

    def on_output(self, data: bytes) -> Optional[Feedback]:
        """Decode a host output report (EP OUT / SET_REPORT). Return ``None`` if irrelevant."""

    def gadget_descriptors(self) -> GadgetDescriptors:
        """Descriptors for the raw-gadget transport."""

    def hid_function(self) -> Optional[HidFunction]:
        """configfs f_hid parameters, or ``None`` if this profile is not a plain HID device."""

    def handle_control(self, setup: SetupPacket, read_data: ReadData) -> Optional[bytes]:
        """Handle a non-default EP0 request (class/vendor, or interface-recipient GET_DESCRIPTOR).

        Return ``bytes`` to reply (IN) / acknowledge (OUT — return ``b""`` after consuming
        ``read_data()`` when ``wLength > 0``), or ``None`` to STALL.
        """
