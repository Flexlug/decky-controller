"""Feature reports the daemon sends to the controller (SDL, zlib: ``SDL_hidapi_steamdeck.c``,
``steam/controller_constants.h``, ``steam/controller_structs.h``).

They go out as HID SET_REPORT 0x21/0x09, wValue 0x0300 (feature, report id 0), wIndex = the controller
interface — what usbhid itself does for a report-id-0 feature report.
"""
from __future__ import annotations

import struct
from typing import List, Optional, Sequence, Tuple

# HID class requests (USB HID 1.11 §7.2)
HID_REQ_GET_REPORT = 0x01
HID_REQ_SET_REPORT = 0x09
HID_REPORT_TYPE_FEATURE = 0x03
USB_REQTYPE_SET_CLASS_INTERFACE = 0x21
USB_REQTYPE_GET_CLASS_INTERFACE = 0xA1
FEATURE_REPORT_ID = 0
FEATURE_WVALUE = (HID_REPORT_TYPE_FEATURE << 8) | FEATURE_REPORT_ID

HID_FEATURE_REPORT_BYTES = 64                 # SDL: controller_structs.h HID_FEATURE_REPORT_BYTES

# Feature report message ids (SDL: controller_constants.h FeatureReportMessageIDs)
ID_CLEAR_DIGITAL_MAPPINGS = 0x81
ID_GET_ATTRIBUTES_VALUES = 0x83
ID_SET_SETTINGS_VALUES = 0x87
ID_SET_CONTROLLER_MODE = 0x8D
ID_TRIGGER_HAPTIC_PULSE = 0x8F
ID_TURN_OFF_CONTROLLER = 0x9F
ID_TRIGGER_HAPTIC_CMD = 0xEA
ID_TRIGGER_RUMBLE_CMD = 0xEB

# Settings (SDL: controller_constants.h enum ControllerSettings — implicit enumeration, counted;
# cross-checked with hid-steam.c register numbers: 0x07/0x08 pad mode, 0x18 rpad margin,
# 0x30 gyro mode, 0x34/0x35 click pressure, 0x47 watchdog)
SETTING_LEFT_TRACKPAD_MODE = 7
SETTING_RIGHT_TRACKPAD_MODE = 8
SETTING_LIZARD_MODE = 9
SETTING_SMOOTH_ABSOLUTE_MOUSE = 24
SETTING_LED_USER_BRIGHTNESS = 45
SETTING_IMU_MODE = 48
SETTING_LEFT_TRACKPAD_CLICK_PRESSURE = 52
SETTING_RIGHT_TRACKPAD_CLICK_PRESSURE = 53
SETTING_STEAM_WATCHDOG_ENABLE = 71
TRACKPAD_NONE = 7                             # SDL: controller_constants.h TrackpadDPadMode
HAPTIC_INTENSITY_SYSTEM = 0                   # SDL: controller_structs.h haptic_intensity_t

RUMBLE_PAYLOAD = struct.Struct("<BHHHbb")     # MsgSimpleRumbleCmd (SDL: controller_structs.h), 9 bytes


def build_feature_report(msg_type: int, payload: bytes = b"", length: Optional[int] = None) -> bytes:
    """``FeatureReportMsg``: u8 type, u8 length (defaults to ``len(payload)``), payload, zero-padded to
    64 bytes, no report id."""
    if len(payload) > HID_FEATURE_REPORT_BYTES - 2:
        raise ValueError("feature payload too long")
    body = bytes([msg_type & 0xFF, (len(payload) if length is None else length) & 0xFF]) + payload
    return body.ljust(HID_FEATURE_REPORT_BYTES, b"\0")


def cmd_clear_digital_mappings() -> bytes:
    return build_feature_report(ID_CLEAR_DIGITAL_MAPPINGS)


def cmd_set_settings(pairs: Sequence[Tuple[int, int]]) -> bytes:
    """``ID_SET_SETTINGS_VALUES`` with ``ControllerSetting {u8 settingNum; u16 settingValue}`` triples."""
    payload = b"".join(struct.pack("<BH", setting & 0xFF, value & 0xFFFF) for setting, value in pairs)
    return build_feature_report(ID_SET_SETTINGS_VALUES, payload)


def lizard_off_sequence() -> List[bytes]:
    """SDL ``DisableDeckLizardMode``: clear mappings, then 5 settings (mouse smoothing off,
    both trackpads NONE, trackpad click pressure 0xFFFF)."""
    return [
        cmd_clear_digital_mappings(),
        cmd_set_settings([
            (SETTING_SMOOTH_ABSOLUTE_MOUSE, 0),
            (SETTING_LEFT_TRACKPAD_MODE, TRACKPAD_NONE),
            (SETTING_RIGHT_TRACKPAD_MODE, TRACKPAD_NONE),
            (SETTING_LEFT_TRACKPAD_CLICK_PRESSURE, 0xFFFF),
            (SETTING_RIGHT_TRACKPAD_CLICK_PRESSURE, 0xFFFF),
        ]),
    ]


def heartbeat_sequence() -> List[bytes]:
    """SDL ``FeedDeckLizardWatchdog``: clear mappings + right trackpad NONE (keeps lizard mode off)."""
    return [cmd_clear_digital_mappings(), cmd_set_settings([(SETTING_RIGHT_TRACKPAD_MODE, TRACKPAD_NONE)])]


def cmd_rumble(left: int, right: int, intensity: int = HAPTIC_INTENSITY_SYSTEM,
               left_gain: int = 2, right_gain: int = 0) -> bytes:
    """``ID_TRIGGER_RUMBLE_CMD`` as SDL ``HIDAPI_DriverSteamDeck_RumbleJoystick`` sends it (type 0, intensity,
    left/right 0..65535, gains 2/0). Header length stays **0** — both SDL and hid-steam's
    ``steam_haptic_rumble`` send 0 there and that is what works on the hardware."""
    left = max(0, min(65535, int(left)))
    right = max(0, min(65535, int(right)))
    return build_feature_report(ID_TRIGGER_RUMBLE_CMD,
                                RUMBLE_PAYLOAD.pack(0, intensity & 0xFFFF, left, right, left_gain, right_gain),
                                length=0)
