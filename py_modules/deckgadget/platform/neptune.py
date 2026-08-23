"""Locate the built-in Steam Deck controller ("Neptune", USB 28de:1205) and (un)bind usbhid.

Facts (docs/HARDWARE.md): the device sits on the *other* xHCI (04:00.4), so putting the USB-C
port into device mode never disturbs it.  Interfaces:

* ``<dev>:1.0`` HID boot mouse (lizard mode), ``<dev>:1.1`` HID boot keyboard (lizard mode),
* ``<dev>:1.2`` HID controller (``hid-steam``; the 64-byte state reports),
* ``<dev>:1.3/1.4`` CDC ACM — left alone.

Exclusive capture = unbind 1.0/1.1/1.2 from ``usbhid`` via
``/sys/bus/usb/drivers/usbhid/unbind`` and talk to interface 2 through usbfs
(``/dev/bus/usb/BBB/DDD``).  Recovery rebinds via ``.../usbhid/bind``.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from ..util.log import get_logger
from ..util.fs import read_text, write_text

log = get_logger("neptune")

NEPTUNE_VID = "28de"
NEPTUNE_PID = "1205"
USBHID_DRIVER = "usbhid"
#: interfaces to detach from usbhid for exclusive capture (mouse, keyboard, controller)
CAPTURE_INTERFACES = (0, 1, 2)
#: interface carrying the controller state reports / accepting feature reports
CONTROLLER_INTERFACE = 2
EP_XFER_INTERRUPT = 0x03


@dataclass
class Endpoint:
    address: int          # bEndpointAddress (0x83 ...)
    attributes: int       # bmAttributes (3 = interrupt)
    max_packet: int
    interval: int
    direction: str        # "in" / "out"

    @property
    def is_in(self) -> bool:
        return bool(self.address & 0x80)

    @property
    def is_interrupt(self) -> bool:
        return (self.attributes & 0x03) == EP_XFER_INTERRUPT


@dataclass
class Interface:
    name: str             # e.g. "3-3:1.2"
    number: int
    driver: Optional[str] # currently bound driver (basename of the ``driver`` symlink) or None
    cls: int = 0
    subclass: int = 0
    protocol: int = 0
    endpoints: List[Endpoint] = field(default_factory=list)

    @property
    def bound(self) -> bool:
        return self.driver is not None

    def interrupt_in(self) -> Optional[Endpoint]:
        for ep in self.endpoints:
            if ep.is_in and ep.is_interrupt:
                return ep
        return None


@dataclass
class NeptuneDevice:
    sysfs_path: str           # /sys/bus/usb/devices/3-3
    name: str                 # "3-3"
    busnum: int
    devnum: int
    devnode: str              # /dev/bus/usb/003/003
    interfaces: Dict[int, Interface] = field(default_factory=dict)
    product: Optional[str] = None
    serial: Optional[str] = None

    def interface(self, number: int) -> Optional[Interface]:
        return self.interfaces.get(number)

    @property
    def captured(self) -> bool:
        """True when any capture interface is *not* bound to usbhid (someone detached it)."""
        for n in CAPTURE_INTERFACES:
            itf = self.interfaces.get(n)
            if itf is not None and itf.driver != USBHID_DRIVER:
                return True
        return False

    def as_dict(self) -> dict:
        return {
            "name": self.name, "busnum": self.busnum, "devnum": self.devnum, "devnode": self.devnode,
            "product": self.product, "serial": self.serial, "captured": self.captured,
            "interfaces": {str(n): {"name": i.name, "driver": i.driver, "class": i.cls,
                                     "subclass": i.subclass, "protocol": i.protocol,
                                     "endpoints": [f"0x{e.address:02x}/{'int' if e.is_interrupt else e.attributes}/"
                                                   f"{e.max_packet}" for e in i.endpoints]}
                           for n, i in sorted(self.interfaces.items())},
        }


def _parse_hex(text: Optional[str], default: int = 0) -> int:
    if not text:
        return default
    try:
        return int(text, 16)
    except ValueError:
        return default


def _parse_interface(itf_path: str) -> Optional[Interface]:
    number = read_text(os.path.join(itf_path, "bInterfaceNumber"))
    if number is None:
        return None
    driver = None
    try:
        driver = os.path.basename(os.readlink(os.path.join(itf_path, "driver")))
    except OSError:
        pass
    eps: List[Endpoint] = []
    try:
        entries = sorted(os.listdir(itf_path))
    except OSError:
        entries = []
    for entry in entries:
        if not entry.startswith("ep_"):
            continue
        ep_path = os.path.join(itf_path, entry)
        addr = _parse_hex(read_text(os.path.join(ep_path, "bEndpointAddress")), -1)
        if addr < 0:
            continue
        eps.append(Endpoint(
            address=addr,
            attributes=_parse_hex(read_text(os.path.join(ep_path, "bmAttributes"))),
            max_packet=_parse_hex(read_text(os.path.join(ep_path, "wMaxPacketSize"))),
            interval=_parse_hex(read_text(os.path.join(ep_path, "bInterval"))),
            direction=(read_text(os.path.join(ep_path, "direction")) or ("in" if addr & 0x80 else "out")).lower(),
        ))
    return Interface(
        name=os.path.basename(itf_path), number=int(number, 16), driver=driver,
        cls=_parse_hex(read_text(os.path.join(itf_path, "bInterfaceClass"))),
        subclass=_parse_hex(read_text(os.path.join(itf_path, "bInterfaceSubClass"))),
        protocol=_parse_hex(read_text(os.path.join(itf_path, "bInterfaceProtocol"))),
        endpoints=eps,
    )


def find_neptune(sysfs: str = "/sys", dev: str = "/dev", vid: str = NEPTUNE_VID,
                 pid: str = NEPTUNE_PID) -> Optional[NeptuneDevice]:
    """Scan ``/sys/bus/usb/devices`` for idVendor/idProduct and collect interfaces + endpoints."""
    base = os.path.join(sysfs, "bus", "usb", "devices")
    try:
        entries = sorted(os.listdir(base))
    except OSError:
        return None
    for entry in entries:
        if ":" in entry or entry.startswith("usb"):
            continue  # interfaces / root hubs
        path = os.path.join(base, entry)
        if read_text(os.path.join(path, "idVendor")) != vid or read_text(os.path.join(path, "idProduct")) != pid:
            continue
        try:
            busnum = int(read_text(os.path.join(path, "busnum")) or "0")
            devnum = int(read_text(os.path.join(path, "devnum")) or "0")
        except ValueError:
            continue
        device = NeptuneDevice(
            sysfs_path=path, name=entry, busnum=busnum, devnum=devnum,
            devnode=os.path.join(dev, "bus", "usb", f"{busnum:03d}", f"{devnum:03d}"),
            product=read_text(os.path.join(path, "product")), serial=read_text(os.path.join(path, "serial")),
        )
        prefix = entry + ":"
        for sub in sorted(os.listdir(path)):
            if sub.startswith(prefix):
                itf = _parse_interface(os.path.join(path, sub))
                if itf is not None:
                    device.interfaces[itf.number] = itf
        return device
    return None


class UsbhidBinder:
    """Bind/unbind USB interfaces to the ``usbhid`` driver through sysfs (idempotent helpers)."""

    def __init__(self, sysfs: str = "/sys") -> None:
        self.sysfs = sysfs
        self.driver_dir = os.path.join(sysfs, "bus", "usb", "drivers", USBHID_DRIVER)

    def bound_driver(self, itf_name: str) -> Optional[str]:
        path = os.path.join(self.sysfs, "bus", "usb", "devices", itf_name, "driver")
        try:
            return os.path.basename(os.readlink(path))
        except OSError:
            return None

    def unbind(self, itf_name: str) -> bool:
        """Detach ``itf_name`` from usbhid. Returns True if a write happened."""
        if self.bound_driver(itf_name) != USBHID_DRIVER:
            return False
        try:
            write_text(os.path.join(self.driver_dir, "unbind"), itf_name)
            log.info("unbound %s from usbhid", itf_name)
            return True
        except OSError as exc:
            if exc.errno == 19:  # ENODEV: raced with somebody else — already unbound
                return False
            raise

    def bind(self, itf_name: str) -> bool:
        """Attach ``itf_name`` to usbhid if it has no driver. Returns True if a write happened."""
        if self.bound_driver(itf_name) is not None:
            return False
        try:
            write_text(os.path.join(self.driver_dir, "bind"), itf_name)
            log.info("bound %s to usbhid", itf_name)
            return True
        except OSError as exc:
            if exc.errno in (16, 19):  # EBUSY already bound / ENODEV gone
                return False
            # Fall back to letting the core pick a driver (drivers_probe).
            probe = os.path.join(self.sysfs, "bus", "usb", "drivers_probe")
            try:
                write_text(probe, itf_name)
                log.info("re-probed %s via drivers_probe (bind failed: %s)", itf_name, exc)
                return True
            except OSError:
                raise exc


def capture_interfaces(device: NeptuneDevice, binder: UsbhidBinder) -> List[str]:
    """Unbind the capture interfaces from usbhid; returns the names that were detached by us."""
    detached: List[str] = []
    for n in CAPTURE_INTERFACES:
        itf = device.interface(n)
        if itf is None:
            continue
        if binder.unbind(itf.name):
            detached.append(itf.name)
    return detached


def release_interfaces(device: Optional[NeptuneDevice], binder: UsbhidBinder,
                       names: Optional[List[str]] = None, errors: Optional[List[str]] = None) -> List[str]:
    """Rebind interfaces to usbhid (all capture interfaces if ``names`` is None). Idempotent.

    Returns the names that were rebound by us.  Bind failures never raise (every interface gets
    its chance); they are logged and, when ``errors`` is given, appended to it as strings so the
    caller (``guard.recover``) can report the rollback as failed instead of silently succeeding.
    """
    if names is None:
        names = []
        if device is not None:
            for n in CAPTURE_INTERFACES:
                itf = device.interface(n)
                if itf is not None:
                    names.append(itf.name)
    rebound: List[str] = []
    for name in names:
        try:
            if binder.bind(name):
                rebound.append(name)
        except OSError as exc:
            log.warning("cannot rebind %s to usbhid: %s", name, exc)
            if errors is not None:
                errors.append(f"cannot rebind {name} to usbhid: {exc}")
    return rebound
