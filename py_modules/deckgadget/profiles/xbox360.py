"""Xbox 360 wired controller (XInput) profile — ported from the original raw-gadget spike.

Descriptors are a faithful clone of the wired Xbox 360 pad (VID 045E PID 028E, four
vendor-specific interfaces with the undocumented 0x21 descriptors).  Verified on real
hardware against Linux ``xpad`` and Windows 11 ``xusb22.sys`` (docs/HARDWARE.md,
"Kernel gadget stack"): the host accepts our empty reply to the capability requests
(``0xc1/0x01``), so no MS OS descriptor / XSM3 handling is needed.

Input report (20 bytes, EP 0x81; Microsoft XINPUT_GAMEPAD bit layout, cf. XInput.h and
https://www.partsnotincluded.com/understanding-the-xbox-360-wired-controllers-usb-data/)::

    00 14 | buttons u16 LE | LT u8 | RT u8 | LX s16 | LY s16 | RX s16 | RY s16 | 6 bytes reserved

Output reports (EP 0x01): ``00 08 00 LL RR 00 00 00`` rumble (LL big/left motor, RR
small/right motor), ``01 03 xx`` LED pattern.  On Windows the spike also saw ``02 08 03 ..``
(not decoded yet; reported as ``kind="unknown"``).
"""
from __future__ import annotations

import struct
from typing import Dict, Optional

from .. import state as S
from ..state import ControllerState
from .base import (
    USB_TYPE_STANDARD, Feedback, GadgetDescriptors, HidFunction, ReadData, SetupPacket,
    endpoint_descriptor,
)

VID = 0x045E
PID = 0x028E
BCD_DEVICE = 0x0114

# XINPUT_GAMEPAD_* button bits (report bytes 2..3, little-endian u16).
XB_DPAD_UP = 0x0001
XB_DPAD_DOWN = 0x0002
XB_DPAD_LEFT = 0x0004
XB_DPAD_RIGHT = 0x0008
XB_START = 0x0010
XB_BACK = 0x0020
XB_L3 = 0x0040
XB_R3 = 0x0080
XB_LB = 0x0100
XB_RB = 0x0200
XB_GUIDE = 0x0400
XB_A = 0x1000
XB_B = 0x2000
XB_X = 0x4000
XB_Y = 0x8000

#: settings target name (``paddles.*`` in docs/ARCHITECTURE.md) -> XInput bit
XB_BY_TARGET: Dict[str, int] = {
    "A": XB_A, "B": XB_B, "X": XB_X, "Y": XB_Y, "LB": XB_LB, "RB": XB_RB, "L3": XB_L3, "R3": XB_R3,
    "VIEW": XB_BACK, "MENU": XB_START,
    "DPAD_UP": XB_DPAD_UP, "DPAD_DOWN": XB_DPAD_DOWN, "DPAD_LEFT": XB_DPAD_LEFT, "DPAD_RIGHT": XB_DPAD_RIGHT,
}

#: default Deck -> Xbox mapping (docs/ARCHITECTURE.md): A/B/X/Y, L1/R1 -> LB/RB, L3/R3, View -> Back, Menu -> Start, D-pad
DEFAULT_BUTTON_MAP = (
    (S.BTN_A, XB_A), (S.BTN_B, XB_B), (S.BTN_X, XB_X), (S.BTN_Y, XB_Y),
    (S.BTN_L1, XB_LB), (S.BTN_R1, XB_RB), (S.BTN_L3, XB_L3), (S.BTN_R3, XB_R3),
    (S.BTN_VIEW, XB_BACK), (S.BTN_MENU, XB_START),
    (S.BTN_DPAD_UP, XB_DPAD_UP), (S.BTN_DPAD_DOWN, XB_DPAD_DOWN),
    (S.BTN_DPAD_LEFT, XB_DPAD_LEFT), (S.BTN_DPAD_RIGHT, XB_DPAD_RIGHT),
)

PADDLE_BITS = {"L4": S.BTN_L4, "L5": S.BTN_L5, "R4": S.BTN_R4, "R5": S.BTN_R5}

REPORT_LEN = 20
_REPORT = struct.Struct("<BBHBBhhhh6x")

# --- descriptors (byte-exact copy of the spike, which is a clone of the real pad) -------------
EP_IN_DESC = endpoint_descriptor(0x81, 0x03, 0x20, 4)    # interrupt IN, 32 bytes, bInterval 4
EP_OUT_DESC = endpoint_descriptor(0x01, 0x03, 0x20, 8)   # interrupt OUT, 32 bytes, bInterval 8
CONFIG_BODY = bytes([
    # IF0 — gamepad (class 0xFF, subclass 0x5D, protocol 0x01) + 17-byte 0x21 descriptor
    9, 4, 0, 0, 2, 0xFF, 0x5D, 0x01, 0,
    0x11, 0x21, 0x00, 0x01, 0x01, 0x25, 0x81, 0x14, 0x00, 0x00, 0x00, 0x00, 0x13, 0x01, 0x08, 0x00, 0x00,
    *EP_IN_DESC, *EP_OUT_DESC,
    # IF1 — headset / audio (subclass 0x5D, protocol 0x03)
    9, 4, 1, 0, 4, 0xFF, 0x5D, 0x03, 0,
    0x1B, 0x21, 0x00, 0x01, 0x01, 0x01, 0x82, 0x40, 0x01, 0x02, 0x20, 0x16, 0x83, 0x00, 0x00, 0x00, 0x00,
    0x00, 0x00, 0x16, 0x03, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
    7, 5, 0x82, 3, 0x20, 0, 2, 7, 5, 0x02, 3, 0x20, 0, 4, 7, 5, 0x83, 3, 0x20, 0, 0x40, 7, 5, 0x03, 3, 0x20, 0, 0x10,
    # IF2 — plugin module (subclass 0x5D, protocol 0x02)
    9, 4, 2, 0, 1, 0xFF, 0x5D, 0x02, 0,
    0x09, 0x21, 0x00, 0x01, 0x01, 0x22, 0x84, 0x07, 0x00,
    7, 5, 0x84, 3, 0x20, 0, 0x10,
    # IF3 — security method (class 0xFF, subclass 0xFD, protocol 0x13), string index 4
    9, 4, 3, 0, 0, 0xFF, 0xFD, 0x13, 4,
    0x06, 0x41, 0x00, 0x01, 0x01, 0x03,
])
SECURITY_STRING = "Xbox Security Method 3, Version 1.00, © 2005 Microsoft Corporation. All rights reserved."

DESCRIPTORS = GadgetDescriptors(
    vid=VID, pid=PID, bcd_device=BCD_DEVICE, bcd_usb=0x0200,
    device_class=0xFF, device_subclass=0xFF, device_protocol=0xFF, ep0_max_packet=64,
    manufacturer="Decky Controller", product="Controller", serial="DECK0001",
    extra_strings={4: SECURITY_STRING},
    config_body=CONFIG_BODY, num_interfaces=4, config_attributes=0xA0, max_power_ma=500,
    ep_in=EP_IN_DESC, ep_out=EP_OUT_DESC, high_speed=True,
)
assert len(DESCRIPTORS.config_descriptor()) == 0x99, len(DESCRIPTORS.config_descriptor())


def trigger_to_u8(raw: int) -> int:
    """Deck raw trigger 0..32767 -> XInput 0..255."""
    if raw <= 0:
        return 0
    if raw >= S.TRIGGER_MAX:
        return 255
    return (raw * 255 + S.TRIGGER_MAX // 2) // S.TRIGGER_MAX


class Xbox360Profile:
    name = "xbox360"
    report_length = REPORT_LEN

    def __init__(self, paddles: Optional[Dict[str, str]] = None, forward_steam: bool = False,
                 forward_qam: bool = False, invert_y: bool = False) -> None:
        # Build the (canonical bit -> xbox bit) table once; the hot path just loops over it.
        table = list(DEFAULT_BUTTON_MAP)
        if forward_steam:
            table.append((S.BTN_STEAM, XB_GUIDE))
        if forward_qam:
            table.append((S.BTN_QAM, XB_GUIDE))
        for paddle, target in (paddles or {}).items():
            action = str(target).upper()
            if action == "NONE":
                continue
            if action not in XB_BY_TARGET:
                raise ValueError(f"unknown paddle target {target!r}")
            table.append((PADDLE_BITS[paddle.upper()], XB_BY_TARGET[action]))
        self._table = tuple(table)
        # Deck sticks report +Y = up, same as XInput — no inversion by default.
        self._y_sign = -1 if invert_y else 1
        self._last_report = _REPORT.pack(0x00, REPORT_LEN, 0, 0, 0, 0, 0, 0, 0)

    # --- Profile protocol ----------------------------------------------------------
    def map_buttons(self, canonical: int) -> int:
        xbox_buttons = 0
        for canonical_bit, xbox_bit in self._table:
            if canonical & canonical_bit:
                xbox_buttons |= xbox_bit
        return xbox_buttons

    def pack(self, state: ControllerState) -> bytes:
        y_sign = self._y_sign
        report = _REPORT.pack(
            0x00, REPORT_LEN, self.map_buttons(state.buttons),
            trigger_to_u8(state.lt), trigger_to_u8(state.rt),
            S.clamp_s16(state.lx), S.clamp_s16(y_sign * state.ly),
            S.clamp_s16(state.rx), S.clamp_s16(y_sign * state.ry),
        )
        self._last_report = report
        return report

    def on_output(self, data: bytes) -> Optional[Feedback]:
        if len(data) < 2:
            return None
        kind, length = data[0], data[1]
        if kind == 0x00 and length == 0x08 and len(data) >= 5:
            # 00 08 00 LL RR 00 00 00 — rumble, motor speeds 0..255
            return Feedback("rumble", left=data[3] * 257, right=data[4] * 257, raw=bytes(data))
        if kind == 0x01 and length == 0x03 and len(data) >= 3:
            return Feedback("led", value=data[2], raw=bytes(data))
        return Feedback("unknown", raw=bytes(data))

    def gadget_descriptors(self) -> GadgetDescriptors:
        return DESCRIPTORS

    def hid_function(self) -> Optional[HidFunction]:
        return None  # vendor-specific interface: cannot be expressed with f_hid

    def handle_control(self, setup: SetupPacket, read_data: ReadData) -> Optional[bytes]:
        if setup.req_type == USB_TYPE_STANDARD:
            return None  # interface-level standard requests we don't know -> STALL
        # Class/vendor requests (xusb22 capability queries 0xc1/0x01, LED/XSM3 ...): an empty
        # IN reply and a plain ACK for OUT are accepted by Linux xpad and Windows xusb22.
        if setup.dir_in:
            return b""
        if setup.wLength:
            read_data()
        return b""


__all__ = ["Xbox360Profile", "DESCRIPTORS", "REPORT_LEN", "trigger_to_u8",
           "XB_A", "XB_B", "XB_X", "XB_Y", "XB_LB", "XB_RB", "XB_L3", "XB_R3", "XB_BACK", "XB_START", "XB_GUIDE",
           "XB_DPAD_UP", "XB_DPAD_DOWN", "XB_DPAD_LEFT", "XB_DPAD_RIGHT"]
