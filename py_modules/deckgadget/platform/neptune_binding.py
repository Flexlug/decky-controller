"""Detach the built-in controller's interfaces from ``usbhid`` for exclusive capture, and put them back."""
from __future__ import annotations

import os
from typing import List, Optional

from deckgadget.util.fs import write_text
from deckgadget.util.log import get_logger
from deckhw.neptune import CAPTURE_INTERFACES, USBHID_DRIVER, NeptuneDevice
from deckhw.sysfs import Sysfs

log = get_logger("neptune")


class UsbhidBinder:
    """Bind/unbind USB interfaces to ``usbhid`` through sysfs; both directions are idempotent."""

    def __init__(self, sysfs: str = "/sys") -> None:
        self.sysfs = sysfs
        self.driver_dir = os.path.join(sysfs, "bus", "usb", "drivers", USBHID_DRIVER)

    def bound_driver(self, interface_name: str) -> Optional[str]:
        return Sysfs(self.sysfs).link_name("bus", "usb", "devices", interface_name, "driver")

    def unbind(self, interface_name: str) -> bool:
        """Detach ``interface_name`` from usbhid. Returns True if a write happened."""
        if self.bound_driver(interface_name) != USBHID_DRIVER:
            return False
        try:
            write_text(os.path.join(self.driver_dir, "unbind"), interface_name)
            log.info("unbound %s from usbhid", interface_name)
            return True
        except OSError as exc:
            if exc.errno == 19:  # ENODEV: raced with somebody else — already unbound
                log.debug("unbind %s: already gone (%s)", interface_name, exc)
                return False
            raise

    def bind(self, interface_name: str) -> bool:
        """Attach ``interface_name`` to usbhid if it has no driver. Returns True if a write happened."""
        if self.bound_driver(interface_name) is not None:
            return False
        try:
            write_text(os.path.join(self.driver_dir, "bind"), interface_name)
            log.info("bound %s to usbhid", interface_name)
            return True
        except OSError as exc:
            if exc.errno in (16, 19):  # EBUSY already bound / ENODEV gone
                log.debug("bind %s: %s", interface_name, exc)
                return False
            probe = os.path.join(self.sysfs, "bus", "usb", "drivers_probe")
            try:
                write_text(probe, interface_name)
                log.info("re-probed %s via drivers_probe (bind failed: %s)", interface_name, exc)
                return True
            except OSError as probe_exc:
                log.warning("drivers_probe for %s failed too: %s", interface_name, probe_exc)
                raise exc


def capture_interfaces(device: NeptuneDevice, binder: UsbhidBinder) -> List[str]:
    """Unbind the capture interfaces from usbhid; returns the names detached by us."""
    detached: List[str] = []
    for number in CAPTURE_INTERFACES:
        interface = device.interface(number)
        if interface is not None and binder.unbind(interface.name):
            detached.append(interface.name)
    return detached


def release_interfaces(device: Optional[NeptuneDevice], binder: UsbhidBinder,
                       names: Optional[List[str]] = None, errors: Optional[List[str]] = None) -> List[str]:
    """Rebind interfaces to usbhid (all capture interfaces when ``names`` is None); returns the names rebound
    by us. Bind failures never raise — every interface gets its chance — they are appended to ``errors``."""
    if names is None:
        names = [interface.name for number in CAPTURE_INTERFACES
                 if device is not None and (interface := device.interface(number)) is not None]
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
