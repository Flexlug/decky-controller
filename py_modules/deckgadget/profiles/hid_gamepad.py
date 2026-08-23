"""Generic HID gamepad profile (the f_hid fallback).

Report descriptor (verified on a Linux host): Game Pad with X, Y, Z, Rz, Rx, Ry (6 x int8), hat switch
(4 bits + 4 bits padding) and 16 buttons -> 9-byte input report ``X Y Z Rz Rx Ry | hat | buttons u16 LE``.
X/Y = left stick, Z/Rz = right stick, Rx/Ry = triggers (0..127), hat = D-pad, buttons 1..16 =
A B X Y L1 R1 L3 R3 VIEW MENU STEAM QAM (Steam/QAM only when forwarded); buttons 13..16 are reserved. The
back paddles send nothing unless assigned in the settings, the same as on the XInput profile.
"""
from __future__ import annotations

import struct
from typing import Dict, Optional

from deckgadget import state as S
from deckgadget.profiles.base import (
    USB_DT_HID, USB_DT_HID_REPORT, USB_RECIP_INTERFACE, USB_REQ_GET_DESCRIPTOR, USB_TYPE_CLASS,
    USB_TYPE_STANDARD, Feedback, GadgetDescriptors, HidFunction, ReadData, SetupPacket, endpoint_descriptor,
)
from deckgadget.state import ControllerState

VID = 0x1D6B   # Linux Foundation test id
PID = 0x0104
REPORT_LEN = 9

REPORT_DESC = bytes([
    0x05, 0x01,        # Usage Page (Generic Desktop)
    0x09, 0x05,        # Usage (Game Pad)
    0xA1, 0x01,        # Collection (Application)
    0xA1, 0x00,        #   Collection (Physical)
    0x05, 0x01,        #     Usage Page (Generic Desktop)
    0x09, 0x30,        #     Usage (X)
    0x09, 0x31,        #     Usage (Y)
    0x09, 0x32,        #     Usage (Z)
    0x09, 0x35,        #     Usage (Rz)
    0x09, 0x33,        #     Usage (Rx)
    0x09, 0x34,        #     Usage (Ry)
    0x15, 0x81,        #     Logical Minimum (-127)
    0x25, 0x7F,        #     Logical Maximum (127)
    0x75, 0x08,        #     Report Size (8)
    0x95, 0x06,        #     Report Count (6)
    0x81, 0x02,        #     Input (Data, Var, Abs)
    0x09, 0x39,        #     Usage (Hat switch)
    0x15, 0x00,        #     Logical Minimum (0)
    0x25, 0x07,        #     Logical Maximum (7)
    0x35, 0x00,        #     Physical Minimum (0)
    0x46, 0x3B, 0x01,  #     Physical Maximum (315)
    0x65, 0x14,        #     Unit (degrees)
    0x75, 0x04,        #     Report Size (4)
    0x95, 0x01,        #     Report Count (1)
    0x81, 0x42,        #     Input (Data, Var, Abs, Null State)
    0x75, 0x04,        #     Report Size (4)
    0x95, 0x01,        #     Report Count (1)
    0x81, 0x03,        #     Input (Const) — padding
    0x05, 0x09,        #     Usage Page (Button)
    0x19, 0x01,        #     Usage Minimum (1)
    0x29, 0x10,        #     Usage Maximum (16)
    0x15, 0x00,        #     Logical Minimum (0)
    0x25, 0x01,        #     Logical Maximum (1)
    0x75, 0x01,        #     Report Size (1)
    0x95, 0x10,        #     Report Count (16)
    0x81, 0x02,        #     Input (Data, Var, Abs)
    0xC0,              #   End Collection
    0xC0,              # End Collection
])
assert len(REPORT_DESC) == 75

HAT_NULL = 8
#: (up, right, down, left) pressed -> hat value 0..7 clockwise from up; 8 = centred
_HAT_TABLE = {
    (1, 0, 0, 0): 0, (1, 1, 0, 0): 1, (0, 1, 0, 0): 2, (0, 1, 1, 0): 3,
    (0, 0, 1, 0): 4, (0, 0, 1, 1): 5, (0, 0, 0, 1): 6, (1, 0, 0, 1): 7,
}

# 1-based HID button numbers
(HB_A, HB_B, HB_X, HB_Y, HB_L1, HB_R1, HB_L3, HB_R3, HB_VIEW, HB_MENU, HB_STEAM, HB_QAM,
 HB_L4, HB_L5, HB_R4, HB_R5) = range(1, 17)


def _hid_button_bit(number: int) -> int:
    return 1 << (number - 1)


HB_BY_TARGET: Dict[str, int] = {
    "A": _hid_button_bit(HB_A), "B": _hid_button_bit(HB_B), "X": _hid_button_bit(HB_X), "Y": _hid_button_bit(HB_Y), "LB": _hid_button_bit(HB_L1), "RB": _hid_button_bit(HB_R1),
    "L3": _hid_button_bit(HB_L3), "R3": _hid_button_bit(HB_R3), "VIEW": _hid_button_bit(HB_VIEW), "MENU": _hid_button_bit(HB_MENU),
}
DEFAULT_BUTTON_MAP = (
    (S.BTN_A, _hid_button_bit(HB_A)), (S.BTN_B, _hid_button_bit(HB_B)), (S.BTN_X, _hid_button_bit(HB_X)), (S.BTN_Y, _hid_button_bit(HB_Y)),
    (S.BTN_L1, _hid_button_bit(HB_L1)), (S.BTN_R1, _hid_button_bit(HB_R1)), (S.BTN_L3, _hid_button_bit(HB_L3)), (S.BTN_R3, _hid_button_bit(HB_R3)),
    (S.BTN_VIEW, _hid_button_bit(HB_VIEW)), (S.BTN_MENU, _hid_button_bit(HB_MENU)),
)
PADDLE_BITS = {"L4": S.BTN_L4, "L5": S.BTN_L5, "R4": S.BTN_R4, "R5": S.BTN_R5}
DPAD_TARGETS = {"DPAD_UP": S.BTN_DPAD_UP, "DPAD_DOWN": S.BTN_DPAD_DOWN,
                "DPAD_LEFT": S.BTN_DPAD_LEFT, "DPAD_RIGHT": S.BTN_DPAD_RIGHT}

_REPORT = struct.Struct("<bbbbbbBH")

EP_IN_DESC = endpoint_descriptor(0x81, 0x03, REPORT_LEN, 4)
EP_OUT_DESC = endpoint_descriptor(0x01, 0x03, REPORT_LEN, 4)
HID_DESC = bytes([9, USB_DT_HID, 0x11, 0x01, 0x00, 0x01, USB_DT_HID_REPORT,
                  len(REPORT_DESC) & 0xFF, len(REPORT_DESC) >> 8])
CONFIG_BODY = bytes([9, 4, 0, 0, 2, 0x03, 0x00, 0x00, 0]) + HID_DESC + EP_IN_DESC + EP_OUT_DESC

DESCRIPTORS = GadgetDescriptors(
    vid=VID, pid=PID, bcd_device=0x0100, bcd_usb=0x0200, device_class=0, device_subclass=0, device_protocol=0,
    manufacturer="Decky Controller", product="Steam Deck Gamepad", serial="DECK0001",
    config_body=CONFIG_BODY, num_interfaces=1, config_attributes=0xA0, max_power_ma=500,
    ep_in=EP_IN_DESC, ep_out=EP_OUT_DESC, high_speed=True,
)

# HID class requests
HID_REQ_GET_REPORT = 0x01
HID_REQ_GET_IDLE = 0x02
HID_REQ_GET_PROTOCOL = 0x03
HID_REQ_SET_REPORT = 0x09
HID_REQ_SET_IDLE = 0x0A
HID_REQ_SET_PROTOCOL = 0x0B


def s16_to_s8(value: int) -> int:
    value = S.clamp_s16(value) >> 8
    return -127 if value < -127 else value


def trigger_to_s8(raw: int) -> int:
    """0..32767 -> 0..127 (the descriptor has no unsigned axes)."""
    raw = S.clamp_trigger(raw)
    return (raw * 127 + S.TRIGGER_MAX // 2) // S.TRIGGER_MAX


def hat_from_buttons(buttons: int) -> int:
    key = (1 if buttons & S.BTN_DPAD_UP else 0, 1 if buttons & S.BTN_DPAD_RIGHT else 0,
           1 if buttons & S.BTN_DPAD_DOWN else 0, 1 if buttons & S.BTN_DPAD_LEFT else 0)
    return _HAT_TABLE.get(key, HAT_NULL)


class HidGamepadProfile:
    name = "hid_gamepad"
    report_length = REPORT_LEN

    def __init__(self, paddles: Optional[Dict[str, str]] = None, forward_steam: bool = False,
                 forward_qam: bool = False) -> None:
        table = list(DEFAULT_BUTTON_MAP)
        if forward_steam:
            table.append((S.BTN_STEAM, _hid_button_bit(HB_STEAM)))
        if forward_qam:
            table.append((S.BTN_QAM, _hid_button_bit(HB_QAM)))
        dpad_extra = []  # paddles mapped to D-pad directions go through the hat
        for paddle, target in (paddles or {}).items():
            target = str(target).upper()
            if target == "NONE":
                continue
            elif target in HB_BY_TARGET:
                table.append((PADDLE_BITS[paddle.upper()], HB_BY_TARGET[target]))
            elif target in DPAD_TARGETS:
                dpad_extra.append((PADDLE_BITS[paddle.upper()], DPAD_TARGETS[target]))
            else:
                raise ValueError(f"unknown paddle target {target!r}")
        self._table = tuple(table)
        self._dpad_extra = tuple(dpad_extra)
        self._last_report = _REPORT.pack(0, 0, 0, 0, 0, 0, HAT_NULL, 0)
        self._idle = 0
        self._protocol = 1

    def pack(self, state: ControllerState) -> bytes:
        buttons = state.buttons
        hid_buttons = 0
        for canonical_bit, hid_bit in self._table:
            if buttons & canonical_bit:
                hid_buttons |= hid_bit
        for canonical_bit, dpad_bit in self._dpad_extra:
            if buttons & canonical_bit:
                buttons |= dpad_bit
        # HID Y axes are +down, the Deck reports +up
        report = _REPORT.pack(s16_to_s8(state.lx), s16_to_s8(-state.ly), s16_to_s8(state.rx), s16_to_s8(-state.ry),
                              trigger_to_s8(state.lt), trigger_to_s8(state.rt), hat_from_buttons(buttons),
                              hid_buttons)
        self._last_report = report
        return report

    def on_output(self, data: bytes) -> Optional[Feedback]:
        return Feedback("unknown", raw=bytes(data)) if data else None

    def gadget_descriptors(self) -> GadgetDescriptors:
        return DESCRIPTORS

    def hid_function(self) -> Optional[HidFunction]:
        return HidFunction(report_desc=REPORT_DESC, report_length=REPORT_LEN, protocol=0, subclass=0,
                           vid=VID, pid=PID, manufacturer=DESCRIPTORS.manufacturer,
                           product=DESCRIPTORS.product, serial=DESCRIPTORS.serial)

    def handle_control(self, setup: SetupPacket, read_data: ReadData) -> Optional[bytes]:
        if setup.req_type == USB_TYPE_STANDARD:
            if (setup.bRequest == USB_REQ_GET_DESCRIPTOR and setup.dir_in
                    and setup.recipient == USB_RECIP_INTERFACE):
                descriptor_type = setup.wValue >> 8
                if descriptor_type == USB_DT_HID_REPORT:
                    return REPORT_DESC
                if descriptor_type == USB_DT_HID:
                    return HID_DESC
            return None
        if setup.req_type != USB_TYPE_CLASS:
            return None
        request = setup.bRequest
        if setup.dir_in:
            if request == HID_REQ_GET_REPORT:
                return self._last_report
            if request == HID_REQ_GET_IDLE:
                return bytes([self._idle])
            if request == HID_REQ_GET_PROTOCOL:
                return bytes([self._protocol])
            return None
        if request == HID_REQ_SET_IDLE:
            self._idle = setup.wValue >> 8
            return b""
        if request == HID_REQ_SET_PROTOCOL:
            self._protocol = setup.wValue & 0xFF
            return b""
        if request == HID_REQ_SET_REPORT:
            data = read_data() if setup.wLength else b""
            self.on_output(data)
            return b""
        return None


__all__ = ["HidGamepadProfile", "REPORT_DESC", "REPORT_LEN", "DESCRIPTORS", "hat_from_buttons", "HAT_NULL"]
