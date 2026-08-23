"""Locate the built-in controller ("Neptune", USB 28de:1205) in sysfs.

It hangs off the other xHCI (04:00.4), so device mode on the USB-C port never disturbs it. Interfaces
1.0/1.1 are the lizard-mode HID mouse/keyboard, 1.2 the controller (64-byte state reports), 1.3/1.4 CDC ACM.
"""
import os
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from deckhw.sysfs import Sysfs

NEPTUNE_VID = "28de"
NEPTUNE_PID = "1205"
USBHID_DRIVER = "usbhid"
CAPTURE_INTERFACES = (0, 1, 2)
CONTROLLER_INTERFACE = 2
EP_XFER_INTERRUPT = 0x03


@dataclass
class Endpoint:
    address: int
    attributes: int
    max_packet: int
    interval: int
    direction: str

    @property
    def is_in(self) -> bool:
        return bool(self.address & 0x80)

    @property
    def is_interrupt(self) -> bool:
        return (self.attributes & 0x03) == EP_XFER_INTERRUPT


@dataclass
class Interface:
    name: str
    number: int
    driver: Optional[str]
    usb_class: int = 0
    subclass: int = 0
    protocol: int = 0
    endpoints: List[Endpoint] = field(default_factory=list)

    @property
    def bound(self) -> bool:
        return self.driver is not None

    def interrupt_in(self) -> Optional[Endpoint]:
        for endpoint in self.endpoints:
            if endpoint.is_in and endpoint.is_interrupt:
                return endpoint
        return None


@dataclass
class NeptuneDevice:
    sysfs_path: str
    name: str
    busnum: int
    devnum: int
    devnode: str
    interfaces: Dict[int, Interface] = field(default_factory=dict)
    product: Optional[str] = None
    serial: Optional[str] = None

    @property
    def captured(self) -> bool:
        """True when any capture interface is not bound to usbhid (someone detached it)."""
        return any(interface.driver != USBHID_DRIVER
                   for number in CAPTURE_INTERFACES
                   if (interface := self.interfaces.get(number)) is not None)

    def interface(self, number: int) -> Optional[Interface]:
        return self.interfaces.get(number)

    def as_dict(self) -> dict:
        return {
            "name": self.name, "busnum": self.busnum, "devnum": self.devnum, "devnode": self.devnode,
            "product": self.product, "serial": self.serial, "captured": self.captured,
            "interfaces": {str(number): {"name": interface.name, "driver": interface.driver,
                                          "class": interface.usb_class, "subclass": interface.subclass,
                                          "protocol": interface.protocol,
                                          "endpoints": [f"0x{endpoint.address:02x}/"
                                                        f"{'int' if endpoint.is_interrupt else endpoint.attributes}/"
                                                        f"{endpoint.max_packet}" for endpoint in interface.endpoints]}
                           for number, interface in sorted(self.interfaces.items())},
        }


def find_neptune(sysfs_root: str = "/sys", dev_root: str = "/dev", vid: str = NEPTUNE_VID,
                 pid: str = NEPTUNE_PID) -> Optional[NeptuneDevice]:
    """Scan ``/sys/bus/usb/devices`` for idVendor/idProduct and collect interfaces and endpoints."""
    sysfs = Sysfs(sysfs_root)
    for entry in sysfs.listdir("bus", "usb", "devices"):
        if ":" in entry or entry.startswith("usb"):
            continue  # interfaces / root hubs
        device_dir = ("bus", "usb", "devices", entry)
        if sysfs.text(*device_dir, "idVendor") != vid or sysfs.text(*device_dir, "idProduct") != pid:
            continue
        busnum = sysfs.int(*device_dir, "busnum")
        devnum = sysfs.int(*device_dir, "devnum")
        if busnum is None or devnum is None:
            continue
        device = NeptuneDevice(
            sysfs_path=sysfs.path(*device_dir), name=entry, busnum=busnum, devnum=devnum,
            devnode=os.path.join(dev_root, "bus", "usb", f"{busnum:03d}", f"{devnum:03d}"),
            product=sysfs.text(*device_dir, "product"), serial=sysfs.text(*device_dir, "serial"),
        )
        for child in sysfs.listdir(*device_dir):
            if child.startswith(entry + ":"):
                interface = _read_interface(sysfs, (*device_dir, child))
                if interface is not None:
                    device.interfaces[interface.number] = interface
        return device
    return None


def _read_interface(sysfs: Sysfs, interface_dir: tuple) -> Optional[Interface]:
    number = sysfs.hex(*interface_dir, "bInterfaceNumber")
    if number is None:
        return None
    endpoints = []
    for entry in sysfs.listdir(*interface_dir):
        if not entry.startswith("ep_"):
            continue
        address = sysfs.hex(*interface_dir, entry, "bEndpointAddress")
        if address is None:
            continue
        endpoints.append(Endpoint(
            address=address,
            attributes=sysfs.hex(*interface_dir, entry, "bmAttributes", default=0),
            max_packet=sysfs.hex(*interface_dir, entry, "wMaxPacketSize", default=0),
            interval=sysfs.hex(*interface_dir, entry, "bInterval", default=0),
            direction=(sysfs.text(*interface_dir, entry, "direction") or ("in" if address & 0x80 else "out")).lower(),
        ))
    return Interface(
        name=interface_dir[-1], number=number, driver=sysfs.link_name(*interface_dir, "driver"),
        usb_class=sysfs.hex(*interface_dir, "bInterfaceClass", default=0),
        subclass=sysfs.hex(*interface_dir, "bInterfaceSubClass", default=0),
        protocol=sysfs.hex(*interface_dir, "bInterfaceProtocol", default=0),
        endpoints=endpoints,
    )
