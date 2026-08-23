"""Read-only USB role / UDC / extcon / cable inspection — the port role is never switched here.

With DRD enabled in BIOS, PCI ``04:00.3`` is claimed by ``dwc3-pci`` and becomes UDC ``dwc3.1.auto``;
the Valve EC (``steamdeck-extcon``) picks the role: ``USB-HOST=1`` -> host (dock), else device.
``/sys/class/udc/<udc>/state`` only says something once a gadget is bound (idle = "not attached"
even with a PC on the cable), so what the port physically sees comes from the EC instead:
``power_supply/ACAD/online`` plus the USB-PD contract in ``steamdeck_hwmon`` (PC/hub port = 5 V,
PD charger = 15-20 V).
"""
from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from ..util.log import get_logger
from ..util.fs import read_text

log = get_logger("usb_role")

DWC3_PCI_MODULE = "dwc3_pci"
DWC3_PCI_DRIVER = "dwc3-pci"
UDC_STATE_CONFIGURED = "configured"

AC_SUPPLY_NAME = "ACAD"
STEAMDECK_HWMON_NAME = "steamdeck_hwmon"      # found by its ``name`` file, hwmon indexes are not stable
PD_VOLTAGE_LABEL = "PD Contract Voltage"      # in<N>_label -> in<N>_input (mV)
PD_CURRENT_LABEL = "PD Contract Current"      # curr<N>_label -> curr<N>_input (mA)
PC_PORT_MAX_MV = 5500                         # contract <= 5.5 V = plain USB port, above = PD charger
CABLE_KINDS = ("none", "pc", "charger", "host_device", "unknown")


def _read_int(path: str) -> Optional[int]:
    text = read_text(path)
    if text is None:
        return None
    try:
        return int(text)
    except ValueError:
        return None


def list_udcs(sysfs: str = "/sys") -> List[str]:
    try:
        return sorted(os.listdir(os.path.join(sysfs, "class", "udc")))
    except OSError:
        return []


def udc_state(udc: Optional[str] = None, sysfs: str = "/sys") -> Optional[str]:
    """Contents of ``/sys/class/udc/<udc>/state`` (``None`` if there is no UDC)."""
    if udc is None:
        udcs = list_udcs(sysfs)
        if not udcs:
            return None
        udc = udcs[0]
    return read_text(os.path.join(sysfs, "class", "udc", udc, "state"))


def udc_attr(name: str, udc: Optional[str] = None, sysfs: str = "/sys") -> Optional[str]:
    if udc is None:
        udcs = list_udcs(sysfs)
        if not udcs:
            return None
        udc = udcs[0]
    return read_text(os.path.join(sysfs, "class", "udc", udc, name))


def extcon_cables(sysfs: str = "/sys") -> Dict[str, int]:
    """``{"USB": 0, "USB-HOST": 1, ...}`` from the first extcon device (steamdeck-extcon)."""
    base = os.path.join(sysfs, "class", "extcon")
    result: Dict[str, int] = {}
    try:
        devices = sorted(os.listdir(base))
    except OSError:
        return result
    for device_name in devices:
        state = read_text(os.path.join(base, device_name, "state"))
        if not state:
            continue
        for line in state.splitlines():
            if "=" in line:
                name, _, value = line.partition("=")
                try:
                    result[name.strip()] = int(value.strip())
                except ValueError:
                    pass
        if result:
            break
    return result


def cable_power(sysfs: str = "/sys") -> Optional[bool]:
    """Power on the USB-C port (``ACAD/online``, falling back to any ``Mains`` supply); ``None`` if unreadable."""
    base = os.path.join(sysfs, "class", "power_supply")
    candidates = [AC_SUPPLY_NAME]
    try:
        for name in sorted(os.listdir(base)):
            if name != AC_SUPPLY_NAME and read_text(os.path.join(base, name, "type")) == "Mains":
                candidates.append(name)
    except OSError:
        pass
    for name in candidates:
        online = _read_int(os.path.join(base, name, "online"))
        if online is not None:
            return online != 0
    return None


def find_hwmon(name: str, sysfs: str = "/sys") -> Optional[str]:
    """Directory of the hwmon whose ``name`` file equals ``name``."""
    base = os.path.join(sysfs, "class", "hwmon")
    try:
        entries = sorted(os.listdir(base))
    except OSError:
        return None
    for entry in entries:
        path = os.path.join(base, entry)
        if read_text(os.path.join(path, "name")) == name:
            return path
    return None


def _hwmon_channel(hwmon_dir: str, prefix: str, label: str, default_channel: int) -> Optional[int]:
    """``<prefix>N_input`` of the channel labelled ``label``; ``default_channel`` only when the driver exposes
    no labels at all for that prefix (labels present but none matching -> ``None``, never a guess)."""
    try:
        names = os.listdir(hwmon_dir)
    except OSError:
        return None
    labels = sorted(name for name in names
                    if name.startswith(prefix) and name.endswith("_label") and name[len(prefix):-6].isdigit())
    if not labels:
        return _read_int(os.path.join(hwmon_dir, f"{prefix}{default_channel}_input"))
    for label_file in labels:
        if read_text(os.path.join(hwmon_dir, label_file)) == label:
            return _read_int(os.path.join(hwmon_dir, label_file[:-len("_label")] + "_input"))
    return None


def pd_contract(sysfs: str = "/sys") -> Tuple[Optional[int], Optional[int]]:
    """``(mV, mA)`` of the negotiated USB-PD contract from ``steamdeck_hwmon`` (``None`` when unreadable)."""
    hwmon = find_hwmon(STEAMDECK_HWMON_NAME, sysfs)
    if hwmon is None:
        return None, None
    return (_hwmon_channel(hwmon, "in", PD_VOLTAGE_LABEL, 0),
            _hwmon_channel(hwmon, "curr", PD_CURRENT_LABEL, 1))


def classify_cable(extcon: Dict[str, int], power: Optional[bool], pd_contract_mv: Optional[int]) -> str:
    """What is on the USB-C port, independent of whether a gadget is bound (one of ``CABLE_KINDS``):
    dock/peripheral (port is host) > no power > PD contract <= 5.5 V = PC/hub port, above = PD charger;
    ``unknown`` when power is unreadable or the contract reads 0 (e.g. right after plugging in)."""
    if extcon.get("USB-HOST") == 1:
        return "host_device"
    if power is False:
        return "none"
    if pd_contract_mv is not None and pd_contract_mv > 0:
        return "pc" if pd_contract_mv <= PC_PORT_MAX_MV else "charger"
    return "unknown"


def _modalias_resolves_to(modalias: str, module: str, timeout: float = 2.0) -> bool:
    """``modprobe -R <modalias>`` lists modules that would claim the device."""
    try:
        result = subprocess.run(["modprobe", "-R", modalias], capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return False
    return module in result.stdout.replace("-", "_").split()


def detect_drd(sysfs: str = "/sys", use_modprobe: bool = True) -> Dict[str, object]:
    """Is a PCI device bound to (or matching by modalias) ``dwc3-pci``?
    Returns ``{"enabled", "pci", "driver", "via": "driver|modalias|none"}``."""
    base = os.path.join(sysfs, "bus", "pci", "devices")
    try:
        devices = sorted(os.listdir(base))
    except OSError:
        return {"enabled": False, "pci": None, "driver": None, "via": "none"}
    candidates = []
    for device_name in devices:
        path = os.path.join(base, device_name)
        driver = None
        try:
            driver = os.path.basename(os.readlink(os.path.join(path, "driver")))
        except OSError:
            pass
        if driver == DWC3_PCI_DRIVER:
            return {"enabled": True, "pci": device_name, "driver": driver, "via": "driver"}
        device_class = read_text(os.path.join(path, "class"), "") or ""
        if device_class.lower().startswith("0x0c03"):  # USB controllers only
            candidates.append((device_name, driver, read_text(os.path.join(path, "modalias"), "") or ""))
    if use_modprobe:
        for device_name, driver, modalias in candidates:
            if modalias and _modalias_resolves_to(modalias, DWC3_PCI_MODULE):
                return {"enabled": True, "pci": device_name, "driver": driver, "via": "modalias"}
    return {"enabled": False, "pci": None, "driver": None, "via": "none"}


def raw_gadget_available(dev: str = "/dev", sysfs: str = "/sys") -> bool:
    """/dev/raw-gadget exists, or the module is present in the running kernel's tree."""
    if os.path.exists(os.path.join(dev, "raw-gadget")):
        return True
    release = read_text("/proc/sys/kernel/osrelease")
    if not release:
        return False
    for candidate in (f"/lib/modules/{release}/kernel/drivers/usb/gadget/legacy/raw_gadget.ko.zst",
                      f"/lib/modules/{release}/kernel/drivers/usb/gadget/legacy/raw_gadget.ko",
                      f"/lib/modules/{release}/kernel/drivers/usb/gadget/legacy/raw_gadget.ko.xz"):
        if os.path.exists(candidate):
            return True
    return os.path.isdir(os.path.join(sysfs, "module", "raw_gadget"))


@dataclass
class UsbRoleStatus:
    drd_enabled: bool = False
    drd_pci: Optional[str] = None
    udc_name: Optional[str] = None
    udc_state: Optional[str] = None
    udc_speed: Optional[str] = None
    udc_function: Optional[str] = None
    extcon: Dict[str, int] = field(default_factory=dict)
    host_connected: bool = False          # udc_state == configured
    raw_gadget: bool = False
    cable_power: Optional[bool] = None    # None = unreadable
    pd_contract_mv: Optional[int] = None
    pd_contract_ma: Optional[int] = None
    cable_kind: str = "unknown"           # one of CABLE_KINDS

    def as_dict(self) -> dict:
        return {
            "drd_enabled": self.drd_enabled, "drd_pci": self.drd_pci,
            "udc_name": self.udc_name, "udc_state": self.udc_state, "udc_speed": self.udc_speed,
            "udc_function": self.udc_function, "extcon": dict(self.extcon),
            "host_connected": self.host_connected, "raw_gadget_available": self.raw_gadget,
            "cable_power": self.cable_power, "pd_contract_mv": self.pd_contract_mv,
            "pd_contract_ma": self.pd_contract_ma, "cable_kind": self.cable_kind,
        }


def usb_role_status(sysfs: str = "/sys", dev: str = "/dev", use_modprobe: bool = True) -> UsbRoleStatus:
    drd = detect_drd(sysfs, use_modprobe=use_modprobe)
    udcs = list_udcs(sysfs)
    udc = udcs[0] if udcs else None
    state = udc_state(udc, sysfs) if udc else None
    extcon = extcon_cables(sysfs)
    power = cable_power(sysfs)
    pd_contract_mv, pd_contract_ma = pd_contract(sysfs)
    return UsbRoleStatus(
        drd_enabled=bool(drd["enabled"]), drd_pci=drd["pci"],  # type: ignore[arg-type]
        udc_name=udc, udc_state=state,
        udc_speed=udc_attr("current_speed", udc, sysfs) if udc else None,
        udc_function=udc_attr("function", udc, sysfs) if udc else None,
        extcon=extcon,
        host_connected=(state == UDC_STATE_CONFIGURED),
        raw_gadget=raw_gadget_available(dev, sysfs),
        cable_power=power, pd_contract_mv=pd_contract_mv, pd_contract_ma=pd_contract_ma,
        cable_kind=classify_cable(extcon, power, pd_contract_mv),
    )


class UdcWatcher:
    """Cheap poller for ``/sys/class/udc/<udc>/state``."""

    def __init__(self, udc: Optional[str] = None, sysfs: str = "/sys") -> None:
        self.sysfs = sysfs
        self.udc = udc
        self._path = None if udc is None else os.path.join(sysfs, "class", "udc", udc, "state")

    def resolve(self) -> Optional[str]:
        if self.udc is None:
            udcs = list_udcs(self.sysfs)
            if udcs:
                self.udc = udcs[0]
                self._path = os.path.join(self.sysfs, "class", "udc", self.udc, "state")
        return self.udc

    def state(self) -> Optional[str]:
        if self._path is None and self.resolve() is None:
            return None
        return read_text(self._path)  # type: ignore[arg-type]

    def configured(self) -> bool:
        return self.state() == UDC_STATE_CONFIGURED
