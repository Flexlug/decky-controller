"""Steam Deck input report layout (SDL, zlib: ``SDL_hidapi_steamdeck.c``, ``steam/controller_structs.h``).

Offsets were cross-checked against facts from Linux ``hid-steam.c`` (facts only, no code).
"""
from __future__ import annotations

import struct
from typing import Dict, Optional, Tuple

from deckgadget import state as S
from deckgadget.state import ControllerState

REPORT_LEN = 64
VALVE_IN_REPORT_MSG_VERSION = 0x01            # SDL: controller_structs.h k_ValveInReportMsgVersion
ID_CONTROLLER_DECK_STATE = 9                  # SDL: controller_structs.h ValveInReportMessageIDs

# Deck button bits (SDL: SDL_hidapi_steamdeck.c enum SteamDeckButtons)
STEAMDECK_LBUTTON_R2 = 0x00000001
STEAMDECK_LBUTTON_L2 = 0x00000002
STEAMDECK_LBUTTON_R = 0x00000004
STEAMDECK_LBUTTON_L = 0x00000008
STEAMDECK_LBUTTON_Y = 0x00000010
STEAMDECK_LBUTTON_B = 0x00000020
STEAMDECK_LBUTTON_X = 0x00000040
STEAMDECK_LBUTTON_A = 0x00000080
STEAMDECK_LBUTTON_DPAD_UP = 0x00000100
STEAMDECK_LBUTTON_DPAD_RIGHT = 0x00000200
STEAMDECK_LBUTTON_DPAD_LEFT = 0x00000400
STEAMDECK_LBUTTON_DPAD_DOWN = 0x00000800
STEAMDECK_LBUTTON_VIEW = 0x00001000
STEAMDECK_LBUTTON_STEAM = 0x00002000
STEAMDECK_LBUTTON_MENU = 0x00004000
STEAMDECK_LBUTTON_L5 = 0x00008000
STEAMDECK_LBUTTON_R5 = 0x00010000
STEAMDECK_LBUTTON_LEFT_PAD = 0x00020000
STEAMDECK_LBUTTON_RIGHT_PAD = 0x00040000
STEAMDECK_LBUTTON_LEFT_TOUCHPAD_TOUCH = 0x00080000
STEAMDECK_LBUTTON_RIGHT_TOUCHPAD_TOUCH = 0x00100000
STEAMDECK_LBUTTON_L3 = 0x00400000
STEAMDECK_LBUTTON_R3 = 0x04000000
STEAMDECK_HBUTTON_L4 = 0x00000200
STEAMDECK_HBUTTON_R4 = 0x00000400
STEAMDECK_HBUTTON_LSTICK_TOUCH = 0x00004000
STEAMDECK_HBUTTON_RSTICK_TOUCH = 0x00008000
STEAMDECK_HBUTTON_QAM = 0x00040000

#: (wire bit, canonical bit, name for probe output)
BUTTONS_L: Tuple[Tuple[int, int, str], ...] = (
    (STEAMDECK_LBUTTON_R2, S.BTN_R2, "R2"),
    (STEAMDECK_LBUTTON_L2, S.BTN_L2, "L2"),
    (STEAMDECK_LBUTTON_R, S.BTN_R1, "R1"),
    (STEAMDECK_LBUTTON_L, S.BTN_L1, "L1"),
    (STEAMDECK_LBUTTON_Y, S.BTN_Y, "Y"),
    (STEAMDECK_LBUTTON_B, S.BTN_B, "B"),
    (STEAMDECK_LBUTTON_X, S.BTN_X, "X"),
    (STEAMDECK_LBUTTON_A, S.BTN_A, "A"),
    (STEAMDECK_LBUTTON_DPAD_UP, S.BTN_DPAD_UP, "DPAD_UP"),
    (STEAMDECK_LBUTTON_DPAD_RIGHT, S.BTN_DPAD_RIGHT, "DPAD_RIGHT"),
    (STEAMDECK_LBUTTON_DPAD_LEFT, S.BTN_DPAD_LEFT, "DPAD_LEFT"),
    (STEAMDECK_LBUTTON_DPAD_DOWN, S.BTN_DPAD_DOWN, "DPAD_DOWN"),
    (STEAMDECK_LBUTTON_VIEW, S.BTN_VIEW, "VIEW"),
    (STEAMDECK_LBUTTON_STEAM, S.BTN_STEAM, "STEAM"),
    (STEAMDECK_LBUTTON_MENU, S.BTN_MENU, "MENU"),
    (STEAMDECK_LBUTTON_L5, S.BTN_L5, "L5"),
    (STEAMDECK_LBUTTON_R5, S.BTN_R5, "R5"),
    (STEAMDECK_LBUTTON_LEFT_PAD, S.BTN_LPAD_CLICK, "LPAD_CLICK"),
    (STEAMDECK_LBUTTON_RIGHT_PAD, S.BTN_RPAD_CLICK, "RPAD_CLICK"),
    (STEAMDECK_LBUTTON_LEFT_TOUCHPAD_TOUCH, S.BTN_LPAD_TOUCH, "LPAD_TOUCH"),
    (STEAMDECK_LBUTTON_RIGHT_TOUCHPAD_TOUCH, S.BTN_RPAD_TOUCH, "RPAD_TOUCH"),
    (STEAMDECK_LBUTTON_L3, S.BTN_L3, "L3"),
    (STEAMDECK_LBUTTON_R3, S.BTN_R3, "R3"),
)
BUTTONS_H: Tuple[Tuple[int, int, str], ...] = (
    (STEAMDECK_HBUTTON_L4, S.BTN_L4, "L4"),
    (STEAMDECK_HBUTTON_R4, S.BTN_R4, "R4"),
    (STEAMDECK_HBUTTON_LSTICK_TOUCH, S.BTN_LSTICK_TOUCH, "LSTICK_TOUCH"),
    (STEAMDECK_HBUTTON_RSTICK_TOUCH, S.BTN_RSTICK_TOUCH, "RSTICK_TOUCH"),
    (STEAMDECK_HBUTTON_QAM, S.BTN_QAM, "QAM"),
)
KNOWN_BITS_L = 0
for _mask, _bit, _name in BUTTONS_L:
    KNOWN_BITS_L |= _mask
KNOWN_BITS_H = 0
for _mask, _bit, _name in BUTTONS_H:
    KNOWN_BITS_H |= _mask

# SteamDeckStatePacket_t layout (SDL: controller_structs.h, #pragma pack(1)), preceded by the
# 4-byte ValveInReportHeader_t {u16 unReportVersion; u8 ucType; u8 ucLength}:
#   0 version u16 | 2 type u8 | 3 length u8 | 4 unPacketNum u32 | 8 ulButtonsL u32 | 12 ulButtonsH u32
#  16 sLeftPadX/Y s16 | 20 sRightPadX/Y | 24 sAccelX/Y/Z | 30 sGyroX/Y/Z | 36 sGyroQuatW/X/Y/Z
#  44 sTriggerRawL u16 | 46 sTriggerRawR u16 | 48 sLeftStickX/Y s16 | 52 sRightStickX/Y s16
#  56 sPressurePadLeft u16 | 58 sPressurePadRight u16 | 60..63 unused
REPORT_STRUCT = struct.Struct("<HBBIII4h3h3h4hHH4hHH")
assert REPORT_STRUCT.size == 60
OFF_PACKET_NUM, OFF_BUTTONS_L, OFF_BUTTONS_H = 4, 8, 12
OFF_TRIGGER_L, OFF_TRIGGER_R = 44, 46
OFF_LSTICK_X, OFF_LSTICK_Y, OFF_RSTICK_X, OFF_RSTICK_Y = 48, 50, 52, 54


def map_buttons(buttons_low: int, buttons_high: int) -> int:
    canonical = 0
    for mask, bit, _ in BUTTONS_L:
        if buttons_low & mask:
            canonical |= bit
    for mask, bit, _ in BUTTONS_H:
        if buttons_high & mask:
            canonical |= bit
    return canonical


def is_deck_state_report(data: bytes) -> bool:
    return (len(data) >= REPORT_STRUCT.size and data[2] == ID_CONTROLLER_DECK_STATE
            and struct.unpack_from("<H", data, 0)[0] == VALVE_IN_REPORT_MSG_VERSION and data[3] == REPORT_LEN)


def parse_report(data: bytes, timestamp: float = 0.0, with_sensors: bool = False) -> Optional[ControllerState]:
    """64-byte Neptune report -> ControllerState, or ``None`` if it is not a deck-state packet."""
    if not is_deck_state_report(data):
        return None
    (_version, _type, _length, packet, buttons_low, buttons_high,
     lpad_x, lpad_y, rpad_x, rpad_y,
     ax, ay, az, gx, gy, gz, _quat_w, _quat_x, _quat_y, _quat_z,
     trigger_left, trigger_right, lx, ly, rx, ry, pressure_left, pressure_right) = REPORT_STRUCT.unpack_from(data, 0)
    state = ControllerState(
        buttons=map_buttons(buttons_low, buttons_high),
        lx=lx, ly=ly, rx=rx, ry=ry,
        lt=S.clamp_trigger(trigger_left), rt=S.clamp_trigger(trigger_right),
        packet=packet, ts=timestamp,
    )
    if with_sensors:
        state.lpad = (lpad_x, lpad_y, pressure_left)
        state.rpad = (rpad_x, rpad_y, pressure_right)
        state.gyro = (gx, gy, gz)
        state.accel = (ax, ay, az)
    return state


def decode_report(data: bytes) -> Dict[str, object]:
    """Verbose decode for ``deckgadget probe``: raw fields, named bits, and *unknown* set bits."""
    decoded: Dict[str, object] = {"len": len(data), "hex": bytes(data).hex()}
    if len(data) < 4:
        return decoded
    version = struct.unpack_from("<H", data, 0)[0]
    decoded.update({"version": version, "type": data[2], "length": data[3], "deck_state": is_deck_state_report(data)})
    if not is_deck_state_report(data):
        return decoded
    (_version, _type, _length, packet, buttons_low, buttons_high,
     lpad_x, lpad_y, rpad_x, rpad_y,
     ax, ay, az, gx, gy, gz, quat_w, quat_x, quat_y, quat_z,
     trigger_left, trigger_right, lx, ly, rx, ry, pressure_left, pressure_right) = REPORT_STRUCT.unpack_from(data, 0)
    names = ([name for mask, _, name in BUTTONS_L if buttons_low & mask]
             + [name for mask, _, name in BUTTONS_H if buttons_high & mask])
    unknown = [f"L:bit{i}" for i in range(32) if (buttons_low & ~KNOWN_BITS_L) & (1 << i)]
    unknown += [f"H:bit{i}" for i in range(32) if (buttons_high & ~KNOWN_BITS_H) & (1 << i)]
    decoded.update({
        "packet": packet, "buttons_l": f"0x{buttons_low:08x}", "buttons_h": f"0x{buttons_high:08x}",
        "buttons": names, "unknown_bits": unknown,
        "lpad": (lpad_x, lpad_y, pressure_left), "rpad": (rpad_x, rpad_y, pressure_right),
        "accel": (ax, ay, az), "gyro": (gx, gy, gz),
        "quat": (quat_w, quat_x, quat_y, quat_z), "trigger_l": trigger_left, "trigger_r": trigger_right,
        "lstick": (lx, ly), "rstick": (rx, ry),
    })
    return decoded
