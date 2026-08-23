"""Exclusive capture of the Steam Deck controller (Neptune, 28de:1205) through usbfs.

Pipeline: find device in sysfs -> unbind interfaces 1.0/1.1/1.2 from ``usbhid`` -> open
``/dev/bus/usb/BBB/DDD`` -> ``USBDEVFS_CLAIMINTERFACE(2)`` -> read 64-byte interrupt IN
reports with ``USBDEVFS_BULK`` (``usb_bulk_msg`` transparently handles interrupt
endpoints) -> parse -> :class:`~deckgadget.state.ControllerState`.  Feature reports
(lizard-mode off, 1 s heartbeat, rumble) go through ``USBDEVFS_CONTROL``::

    bmRequestType 0x21 (host->device, class, interface), bRequest 0x09 SET_REPORT,
    wValue 0x0300 (feature report, id 0), wIndex = interface 2, 64-byte payload

(that is exactly what hidraw/usbhid does for a report-id-0 feature report).

Wire format and commands come from SDL (zlib licence):
``src/joystick/hidapi/SDL_hidapi_steamdeck.c``, ``steam/controller_structs.h``,
``steam/controller_constants.h`` — constants below carry a ``# SDL:`` note.  Bit
positions are table-driven so they can be corrected after ``deckgadget probe`` on the
device; offsets were cross-checked against facts from Linux ``hid-steam.c`` (facts only).

ioctl numbers / struct layouts: ``include/uapi/linux/usbdevice_fs.h`` (x86_64 ABI).
"""
from __future__ import annotations

import ctypes
import errno
import os
import struct
import threading
import time
from typing import Dict, List, Optional, Sequence, Tuple

from .. import state as S
from ..platform import neptune as neptune_mod
from ..state import ControllerState
from ..util.ioctl import IO, IOR, IOW, IOWR, ioctl
from ..util.log import get_logger

log = get_logger("neptune_usb")

# ---------------------------------------------------------------------------------------
# usbfs ABI (include/uapi/linux/usbdevice_fs.h)
# ---------------------------------------------------------------------------------------


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


USBDEVFS_CONTROL = IOWR("U", 0, ctypes.sizeof(UsbfsCtrlTransfer))          # 0xC0185500
USBDEVFS_BULK = IOWR("U", 2, ctypes.sizeof(UsbfsBulkTransfer))             # 0xC0185502
USBDEVFS_RESETEP = IOR("U", 3, ctypes.sizeof(ctypes.c_uint))               # 0x80045503
USBDEVFS_SETINTERFACE = IOR("U", 4, 8)                                     # 0x80085504
USBDEVFS_GETDRIVER = IOW("U", 8, ctypes.sizeof(UsbfsGetDriver))            # 0x41045508
USBDEVFS_CLAIMINTERFACE = IOR("U", 15, ctypes.sizeof(ctypes.c_uint))       # 0x8004550F
USBDEVFS_RELEASEINTERFACE = IOR("U", 16, ctypes.sizeof(ctypes.c_uint))     # 0x80045510
USBDEVFS_IOCTL = IOWR("U", 18, 16)                                         # 0xC0105512 (struct usbdevfs_ioctl)
USBDEVFS_RESET = IO("U", 20)                                               # 0x5514
USBDEVFS_CLEAR_HALT = IOR("U", 21, ctypes.sizeof(ctypes.c_uint))           # 0x80045515
USBDEVFS_DISCONNECT = IO("U", 22)                                          # 0x5516 (via USBDEVFS_IOCTL)
USBDEVFS_CONNECT = IO("U", 23)                                             # 0x5517 (via USBDEVFS_IOCTL)
USBDEVFS_DISCONNECT_CLAIM = IOR("U", 27, ctypes.sizeof(UsbfsDisconnectClaim))  # 0x8108551B
USBDEVFS_DISCONNECT_CLAIM_IF_DRIVER = 0x01
USBDEVFS_DISCONNECT_CLAIM_EXCEPT_DRIVER = 0x02

# HID class requests (USB HID 1.11 §7.2)
HID_REQ_GET_REPORT = 0x01
HID_REQ_SET_REPORT = 0x09
HID_REPORT_TYPE_FEATURE = 0x03
USB_REQTYPE_SET_CLASS_INTERFACE = 0x21
USB_REQTYPE_GET_CLASS_INTERFACE = 0xA1
FEATURE_REPORT_ID = 0
FEATURE_WVALUE = (HID_REPORT_TYPE_FEATURE << 8) | FEATURE_REPORT_ID   # 0x0300

# ---------------------------------------------------------------------------------------
# Valve protocol constants  (SDL: steam/controller_constants.h, steam/controller_structs.h)
# ---------------------------------------------------------------------------------------

HID_FEATURE_REPORT_BYTES = 64                 # SDL: controller_structs.h HID_FEATURE_REPORT_BYTES
REPORT_LEN = 64
VALVE_IN_REPORT_MSG_VERSION = 0x01            # SDL: controller_structs.h k_ValveInReportMsgVersion
ID_CONTROLLER_DECK_STATE = 9                  # SDL: controller_structs.h ValveInReportMessageIDs

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

# Deck button bits (SDL: SDL_hidapi_steamdeck.c enum SteamDeckButtons) -----------------------
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

#: table-driven mapping of ulButtonsL bits -> canonical bits (name for probe output)
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
_KNOWN_L = 0
for _m, _c, _n in BUTTONS_L:
    _KNOWN_L |= _m
_KNOWN_H = 0
for _m, _c, _n in BUTTONS_H:
    _KNOWN_H |= _m

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


# ---------------------------------------------------------------------------------------
# Feature report builders (pure functions; unit-tested)
# ---------------------------------------------------------------------------------------

def build_feature_report(msg_type: int, payload: bytes = b"", length: Optional[int] = None) -> bytes:
    """``FeatureReportMsg``: u8 type, u8 length, payload; zero-padded to 64 bytes (no report id).

    ``length`` defaults to ``len(payload)``; a few commands are sent with an explicit value
    (see :func:`cmd_rumble`, which mirrors SDL's 0)."""
    if len(payload) > HID_FEATURE_REPORT_BYTES - 2:
        raise ValueError("feature payload too long")
    body = bytes([msg_type & 0xFF, (len(payload) if length is None else length) & 0xFF]) + payload
    return body.ljust(HID_FEATURE_REPORT_BYTES, b"\0")


def cmd_clear_digital_mappings() -> bytes:
    return build_feature_report(ID_CLEAR_DIGITAL_MAPPINGS)


def cmd_set_settings(pairs: Sequence[Tuple[int, int]]) -> bytes:
    """``ID_SET_SETTINGS_VALUES`` with ``ControllerSetting {u8 settingNum; u16 settingValue}`` triples."""
    payload = b"".join(struct.pack("<BH", num & 0xFF, val & 0xFFFF) for num, val in pairs)
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


RUMBLE_PAYLOAD = struct.Struct("<BHHHbb")   # MsgSimpleRumbleCmd (SDL: controller_structs.h), 9 bytes


def cmd_rumble(left: int, right: int, intensity: int = HAPTIC_INTENSITY_SYSTEM,
               left_gain: int = 2, right_gain: int = 0) -> bytes:
    """``ID_TRIGGER_RUMBLE_CMD`` as sent by SDL ``HIDAPI_DriverSteamDeck_RumbleJoystick``
    (unRumbleType 0, unIntensity, left/right motor speed 0..65535, gains 2/0).
    Header length is **0**, byte-for-byte like both references known to work on real hardware:
    SDL leaves ``msg->header.length`` at 0 and hid-steam's ``steam_haptic_rumble`` also sends 0
    in that byte (``u8 report[11] = {ID_TRIGGER_RUMBLE_CMD, 0}``)."""
    left = max(0, min(65535, int(left)))
    right = max(0, min(65535, int(right)))
    return build_feature_report(ID_TRIGGER_RUMBLE_CMD,
                                RUMBLE_PAYLOAD.pack(0, intensity & 0xFFFF, left, right, left_gain, right_gain),
                                length=0)


# ---------------------------------------------------------------------------------------
# Report parser (table-driven)
# ---------------------------------------------------------------------------------------

def map_buttons(buttons_l: int, buttons_h: int) -> int:
    canon = 0
    for mask, bit, _ in BUTTONS_L:
        if buttons_l & mask:
            canon |= bit
    for mask, bit, _ in BUTTONS_H:
        if buttons_h & mask:
            canon |= bit
    return canon


def is_deck_state_report(data: bytes) -> bool:
    return (len(data) >= REPORT_STRUCT.size and data[2] == ID_CONTROLLER_DECK_STATE
            and struct.unpack_from("<H", data, 0)[0] == VALVE_IN_REPORT_MSG_VERSION and data[3] == REPORT_LEN)


def parse_report(data: bytes, ts: float = 0.0, with_sensors: bool = False) -> Optional[ControllerState]:
    """64-byte Neptune report -> ControllerState, or ``None`` if it is not a deck-state packet."""
    if not is_deck_state_report(data):
        return None
    (_ver, _typ, _len, packet, bl, bh,
     lpad_x, lpad_y, rpad_x, rpad_y,
     ax, ay, az, gx, gy, gz, _qw, _qx, _qy, _qz,
     trig_l, trig_r, lx, ly, rx, ry, press_l, press_r) = REPORT_STRUCT.unpack_from(data, 0)
    st = ControllerState(
        buttons=map_buttons(bl, bh),
        lx=lx, ly=ly, rx=rx, ry=ry,
        lt=S.clamp_trigger(trig_l), rt=S.clamp_trigger(trig_r),
        packet=packet, ts=ts,
    )
    if with_sensors:
        st.lpad = (lpad_x, lpad_y, press_l)
        st.rpad = (rpad_x, rpad_y, press_r)
        st.gyro = (gx, gy, gz)
        st.accel = (ax, ay, az)
    return st


def decode_report(data: bytes) -> Dict[str, object]:
    """Verbose decode for ``deckgadget probe``: raw fields, named bits, and *unknown* set bits."""
    out: Dict[str, object] = {"len": len(data), "hex": bytes(data).hex()}
    if len(data) < 4:
        return out
    ver = struct.unpack_from("<H", data, 0)[0]
    out.update({"version": ver, "type": data[2], "length": data[3], "deck_state": is_deck_state_report(data)})
    if not is_deck_state_report(data):
        return out
    (_v, _t, _l, packet, bl, bh, lpx, lpy, rpx, rpy, ax, ay, az, gx, gy, gz, qw, qx, qy, qz,
     tl, tr, lx, ly, rx, ry, pl, pr) = REPORT_STRUCT.unpack_from(data, 0)
    names = [n for m, _, n in BUTTONS_L if bl & m] + [n for m, _, n in BUTTONS_H if bh & m]
    unknown = [f"L:bit{i}" for i in range(32) if (bl & ~_KNOWN_L) & (1 << i)]
    unknown += [f"H:bit{i}" for i in range(32) if (bh & ~_KNOWN_H) & (1 << i)]
    out.update({
        "packet": packet, "buttons_l": f"0x{bl:08x}", "buttons_h": f"0x{bh:08x}",
        "buttons": names, "unknown_bits": unknown,
        "lpad": (lpx, lpy, pl), "rpad": (rpx, rpy, pr), "accel": (ax, ay, az), "gyro": (gx, gy, gz),
        "quat": (qw, qx, qy, qz), "trigger_l": tl, "trigger_r": tr,
        "lstick": (lx, ly), "rstick": (rx, ry),
    })
    return out


# ---------------------------------------------------------------------------------------
# usbfs device wrapper
# ---------------------------------------------------------------------------------------

class UsbfsDevice:
    """Minimal synchronous usbfs client (claim/release, control, bulk/interrupt IN)."""

    def __init__(self, path: str) -> None:
        self.path = path
        self.fd = os.open(path, os.O_RDWR | os.O_CLOEXEC)
        self._rbuf = ctypes.create_string_buffer(REPORT_LEN)
        self._bulk = UsbfsBulkTransfer(0, REPORT_LEN, 0, ctypes.cast(self._rbuf, ctypes.c_void_p))

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
        """Detach whatever kernel driver holds the interface and claim it (fallback path)."""
        arg = UsbfsDisconnectClaim(number, 0, b"")
        ioctl(self.fd, USBDEVFS_DISCONNECT_CLAIM, arg)

    def get_driver(self, number: int) -> Optional[str]:
        arg = UsbfsGetDriver(number, b"")
        try:
            ioctl(self.fd, USBDEVFS_GETDRIVER, arg)
        except OSError as exc:
            if exc.errno == errno.ENODATA:
                return None
            raise
        return arg.driver.decode(errors="replace")

    def control_out(self, request_type: int, request: int, value: int, index: int, data: bytes,
                    timeout_ms: int = 1000) -> int:
        n = len(data)
        buf = ctypes.create_string_buffer(max(1, n))
        if n:
            ctypes.memmove(buf, data, n)
        ctrl = UsbfsCtrlTransfer(request_type, request, value, index, n, timeout_ms,
                                 ctypes.cast(buf, ctypes.c_void_p))
        return ioctl(self.fd, USBDEVFS_CONTROL, ctrl)

    def control_in(self, request_type: int, request: int, value: int, index: int, length: int,
                   timeout_ms: int = 1000) -> bytes:
        buf = ctypes.create_string_buffer(max(1, length))
        ctrl = UsbfsCtrlTransfer(request_type | 0x80, request, value, index, length, timeout_ms,
                                 ctypes.cast(buf, ctypes.c_void_p))
        n = ioctl(self.fd, USBDEVFS_CONTROL, ctrl)
        return buf.raw[:n]

    def interrupt_in(self, ep: int, timeout_ms: int) -> Optional[bytes]:
        """One USBDEVFS_BULK read on an (interrupt) IN endpoint; ``None`` on timeout/EINTR."""
        self._bulk.ep = ep
        self._bulk.len = REPORT_LEN
        self._bulk.timeout = timeout_ms
        try:
            n = ioctl(self.fd, USBDEVFS_BULK, self._bulk)
        except OSError as exc:
            if exc.errno in (errno.ETIMEDOUT, errno.EINTR, errno.EAGAIN):
                return None
            raise
        return self._rbuf.raw[:n]


# ---------------------------------------------------------------------------------------
# The source
# ---------------------------------------------------------------------------------------

class NeptuneError(RuntimeError):
    pass


class NeptuneUsbSource:
    """InputSource for the built-in controller. See module docstring."""

    name = "neptune_usb"

    def __init__(self, sysfs: str = "/sys", dev: str = "/dev", heartbeat_s: float = 1.0,
                 device_cls=UsbfsDevice, with_sensors: bool = False) -> None:
        self.sysfs = sysfs
        self.dev = dev
        self.heartbeat_s = heartbeat_s
        self._device_cls = device_cls
        self.with_sensors = with_sensors
        self.device: Optional[neptune_mod.NeptuneDevice] = None
        self.usb: Optional[UsbfsDevice] = None
        self.interface = neptune_mod.CONTROLLER_INTERFACE
        self.ep_in = 0x83
        self.detached: List[str] = []
        self._binder = neptune_mod.UsbhidBinder(sysfs)
        self._ctrl_lock = threading.Lock()
        self._hb_stop = threading.Event()
        self._hb_thread: Optional[threading.Thread] = None
        self._opened = False
        self.reports = 0
        self.other_packets = 0
        self.heartbeats = 0
        self.heartbeat_errors = 0

    # --- lifecycle ------------------------------------------------------------------
    def open(self) -> None:
        if self._opened:
            return
        device = neptune_mod.find_neptune(self.sysfs, self.dev)
        if device is None:
            raise NeptuneError("Steam Deck controller (28de:1205) not found in sysfs")
        itf = device.interface(self.interface)
        if itf is None:
            raise NeptuneError(f"controller interface {self.interface} not present on {device.name}")
        ep = itf.interrupt_in()
        if ep is None:
            raise NeptuneError(f"no interrupt IN endpoint on {itf.name}")
        if ep.max_packet != REPORT_LEN:
            log.warning("unexpected wMaxPacketSize %d on ep 0x%02x (expected %d)", ep.max_packet, ep.address, REPORT_LEN)
        self.device = device
        self.ep_in = ep.address
        log.info("neptune %s at %s, iface %d ep 0x%02x", device.name, device.devnode, self.interface, self.ep_in)
        try:
            self.detached = neptune_mod.capture_interfaces(device, self._binder)
            self.usb = self._device_cls(device.devnode)
            try:
                self.usb.claim_interface(self.interface)
            except OSError as exc:
                if exc.errno != errno.EBUSY:
                    raise
                log.warning("claim iface %d busy (%s); trying USBDEVFS_DISCONNECT_CLAIM", self.interface, exc)
                self.usb.disconnect_claim(self.interface)
            self.disable_lizard_mode()
            self._hb_stop.clear()
            self._hb_thread = threading.Thread(target=self._heartbeat_loop, name="neptune-heartbeat", daemon=True)
            self._hb_thread.start()
            self._opened = True
        except Exception:
            self.close()
            raise

    def close(self) -> None:
        self._hb_stop.set()
        t = self._hb_thread
        if t is not None and t is not threading.current_thread():
            t.join(timeout=2.0)
        self._hb_thread = None
        usb, self.usb = self.usb, None
        if usb is not None:
            try:
                usb.release_interface(self.interface)
            except OSError:
                pass
            usb.close()
        # Give the interfaces back to usbhid (all capture interfaces, not just the ones we detached:
        # a previous crashed run may have left some unbound).
        try:
            rebound = neptune_mod.release_interfaces(self.device, self._binder)
            if rebound:
                log.info("rebound to usbhid: %s", ", ".join(rebound))
        except Exception as exc:  # noqa: BLE001
            log.warning("rebind failed: %s", exc)
        self.detached = []
        self._opened = False

    # --- feature reports ------------------------------------------------------------
    def send_feature(self, report: bytes, timeout_ms: int = 1000) -> None:
        if self.usb is None:
            raise NeptuneError("device not open")
        if len(report) != HID_FEATURE_REPORT_BYTES:
            raise ValueError("feature report must be 64 bytes")
        with self._ctrl_lock:
            self.usb.control_out(USB_REQTYPE_SET_CLASS_INTERFACE, HID_REQ_SET_REPORT, FEATURE_WVALUE,
                                 self.interface, report, timeout_ms)

    def get_feature(self, timeout_ms: int = 200) -> Optional[bytes]:
        if self.usb is None:
            return None
        with self._ctrl_lock:
            try:
                return self.usb.control_in(USB_REQTYPE_GET_CLASS_INTERFACE, HID_REQ_GET_REPORT, FEATURE_WVALUE,
                                           self.interface, HID_FEATURE_REPORT_BYTES, timeout_ms)
            except OSError:
                return None

    def disable_lizard_mode(self) -> None:
        for rep in lizard_off_sequence():
            self.send_feature(rep)
        self.get_feature()  # SDL: "There may be a lingering report read back after changing settings."
        log.info("lizard mode disabled")

    def heartbeat(self) -> None:
        for rep in heartbeat_sequence():
            self.send_feature(rep)
        self.get_feature()
        self.heartbeats += 1

    def _heartbeat_loop(self) -> None:
        while not self._hb_stop.wait(self.heartbeat_s):
            try:
                self.heartbeat()
            except OSError as exc:
                self.heartbeat_errors += 1
                if self.heartbeat_errors <= 3 or self.heartbeat_errors % 30 == 0:
                    log.warning("heartbeat failed (%d): %s", self.heartbeat_errors, exc)
            except NeptuneError:
                break

    def rumble(self, left: int, right: int) -> None:
        try:
            self.send_feature(cmd_rumble(left, right), timeout_ms=200)
        except (OSError, NeptuneError) as exc:
            log.debug("rumble failed: %s", exc)

    # --- hot path -------------------------------------------------------------------
    def read(self, timeout: float) -> Optional[ControllerState]:
        usb = self.usb
        if usb is None:
            raise NeptuneError("device not open")
        data = usb.interrupt_in(self.ep_in, max(1, int(timeout * 1000)))
        if data is None:
            return None
        st = parse_report(data, time.monotonic(), self.with_sensors)
        if st is None:
            self.other_packets += 1
            return None
        self.reports += 1
        return st

    def read_raw(self, timeout: float) -> Optional[bytes]:
        usb = self.usb
        if usb is None:
            raise NeptuneError("device not open")
        return usb.interrupt_in(self.ep_in, max(1, int(timeout * 1000)))
