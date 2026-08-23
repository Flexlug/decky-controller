"""USB role / UDC / extcon inspection (read-only; we never switch the port role ourselves).

Facts (docs/HARDWARE.md): with DRD enabled in BIOS the PCI function ``04:00.3`` is claimed by
``dwc3-pci`` (its modalias resolves to ``dwc3_pci``), creating platform device
``dwc3.1.auto``.  The Valve EC (``steamdeck-extcon``, ``/sys/class/extcon/extcon0``) picks
the role: ``USB-HOST=1`` -> host (dock), otherwise -> device, in which case
``/sys/class/udc/dwc3.1.auto`` exists and its ``state`` file tells whether a host
enumerated us (``not attached`` / ``attached`` / ``powered`` / ``default`` /
``addressed`` / ``configured``).

``udc_state`` only says something once a gadget is bound; while idle it reads ``not attached``
even with a PC on the cable.  What the port *physically* sees is read from the EC instead
(verified on a Deck OLED, idle, plugged into a PC): ``/sys/class/power_supply/ACAD/online`` = 1
and the ``steamdeck_hwmon`` hwmon reports the USB-PD contract — ``in0_label`` "PD Contract
Voltage" ``in0_input`` 5000 (mV), ``curr1_label`` "PD Contract Current" ``curr1_input`` 1500
(mA).  A PC/hub port negotiates 5 V (0.9/1.5/3 A); a PD charger negotiates 15-20 V.
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

AC_SUPPLY_NAME = "ACAD"                       # /sys/class/power_supply/ACAD (type "Mains") on the Deck
STEAMDECK_HWMON_NAME = "steamdeck_hwmon"      # EC hwmon; found by its ``name`` file, never by index
PD_VOLTAGE_LABEL = "PD Contract Voltage"      # in<N>_label  -> in<N>_input   (mV)
PD_CURRENT_LABEL = "PD Contract Current"      # curr<N>_label -> curr<N>_input (mA)
PC_PORT_MAX_MV = 5500                         # <= 5.5 V contract = plain USB port (PC/hub); above = PD charger
CABLE_KINDS = ("none", "pc", "charger", "host_device", "unknown")


def _read_int(path: str) -> Optional[int]:
    txt = read_text(path)
    if txt is None:
        return None
    try:
        return int(txt)
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
    for dev in devices:
        state = read_text(os.path.join(base, dev, "state"))
        if not state:
            continue
        for line in state.splitlines():
            if "=" in line:
                name, _, val = line.partition("=")
                try:
                    result[name.strip()] = int(val.strip())
                except ValueError:
                    pass
        if result:
            break
    return result


def cable_power(sysfs: str = "/sys") -> Optional[bool]:
    """Is there power on the USB-C port?  ``/sys/class/power_supply/ACAD/online`` (``None`` if unreadable).

    Falls back to any other supply whose ``type`` is ``Mains`` so a renamed EC node still works.
    """
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
    """Directory of the hwmon whose ``name`` file equals ``name`` (hwmon indexes are not stable)."""
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
    """``<prefix>N_input`` of the channel whose ``<prefix>N_label`` equals ``label``.

    When the driver exposes no labels at all for that prefix, ``default_channel`` is used; when it
    exposes labels but none matches, ``None`` (never guess a different quantity).
    """
    try:
        names = os.listdir(hwmon_dir)
    except OSError:
        return None
    labels = sorted(n for n in names if n.startswith(prefix) and n.endswith("_label") and n[len(prefix):-6].isdigit())
    if not labels:
        return _read_int(os.path.join(hwmon_dir, f"{prefix}{default_channel}_input"))
    for fn in labels:
        if read_text(os.path.join(hwmon_dir, fn)) == label:
            return _read_int(os.path.join(hwmon_dir, fn[:-len("_label")] + "_input"))
    return None


def pd_contract(sysfs: str = "/sys") -> Tuple[Optional[int], Optional[int]]:
    """``(mV, mA)`` of the negotiated USB-PD contract from ``steamdeck_hwmon`` (``None`` when unreadable)."""
    hwmon = find_hwmon(STEAMDECK_HWMON_NAME, sysfs)
    if hwmon is None:
        return None, None
    return (_hwmon_channel(hwmon, "in", PD_VOLTAGE_LABEL, 0),
            _hwmon_channel(hwmon, "curr", PD_CURRENT_LABEL, 1))


def classify_cable(extcon: Dict[str, int], power: Optional[bool], pd_mv: Optional[int]) -> str:
    """What is on the USB-C port, independent of whether a gadget is bound (one of ``CABLE_KINDS``).

    * ``host_device`` - extcon ``USB-HOST=1``: a dock/peripheral is attached, the port is a host;
    * ``none``        - no power on the port (``ACAD online = 0``);
    * ``pc``          - a PD contract of <= 5.5 V: a plain USB port (PC, hub, non-PD charger);
    * ``charger``     - a PD contract above 5.5 V (15-20 V): a PD charger, no data partner;
    * ``unknown``     - power state unreadable or no/zero contract reading (e.g. right after plugging in).
    """
    if extcon.get("USB-HOST") == 1:
        return "host_device"
    if power is False:
        return "none"
    if pd_mv is not None and pd_mv > 0:
        return "pc" if pd_mv <= PC_PORT_MAX_MV else "charger"
    return "unknown"


def _modalias_resolves_to(modalias: str, module: str, timeout: float = 2.0) -> bool:
    """``modprobe -R <modalias>`` lists modules that would claim the device."""
    try:
        out = subprocess.run(["modprobe", "-R", modalias], capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return False
    return module in out.stdout.replace("-", "_").split()


def detect_drd(sysfs: str = "/sys", use_modprobe: bool = True) -> Dict[str, object]:
    """DRD detection: is there a PCI device bound to / matching ``dwc3-pci``?

    Returns ``{"enabled": bool, "pci": "0000:04:00.3"|None, "driver": str|None, "via": "driver|modalias|none"}``.
    """
    base = os.path.join(sysfs, "bus", "pci", "devices")
    try:
        devices = sorted(os.listdir(base))
    except OSError:
        return {"enabled": False, "pci": None, "driver": None, "via": "none"}
    candidates = []
    for dev in devices:
        path = os.path.join(base, dev)
        drv = None
        try:
            drv = os.path.basename(os.readlink(os.path.join(path, "driver")))
        except OSError:
            pass
        if drv == DWC3_PCI_DRIVER:
            return {"enabled": True, "pci": dev, "driver": drv, "via": "driver"}
        cls = read_text(os.path.join(path, "class"), "") or ""
        if cls.lower().startswith("0x0c03"):  # USB controllers only, keeps status fast
            candidates.append((dev, drv, read_text(os.path.join(path, "modalias"), "") or ""))
    if use_modprobe:
        for dev, drv, modalias in candidates:
            if modalias and _modalias_resolves_to(modalias, DWC3_PCI_MODULE):
                return {"enabled": True, "pci": dev, "driver": drv, "via": "modalias"}
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
    host_connected: bool = False          # a host enumerated our gadget (udc_state == configured)
    raw_gadget: bool = False
    cable_power: Optional[bool] = None    # power on the USB-C port (ACAD online); None = unreadable
    pd_contract_mv: Optional[int] = None  # negotiated USB-PD contract, mV (steamdeck_hwmon)
    pd_contract_ma: Optional[int] = None  # negotiated USB-PD contract, mA
    cable_kind: str = "unknown"           # one of CABLE_KINDS (classify_cable)

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
    pd_mv, pd_ma = pd_contract(sysfs)
    return UsbRoleStatus(
        drd_enabled=bool(drd["enabled"]), drd_pci=drd["pci"],  # type: ignore[arg-type]
        udc_name=udc, udc_state=state,
        udc_speed=udc_attr("current_speed", udc, sysfs) if udc else None,
        udc_function=udc_attr("function", udc, sysfs) if udc else None,
        extcon=extcon,
        host_connected=(state == UDC_STATE_CONFIGURED),
        raw_gadget=raw_gadget_available(dev, sysfs),
        cable_power=power, pd_contract_mv=pd_mv, pd_contract_ma=pd_ma,
        cable_kind=classify_cable(extcon, power, pd_mv),
    )


class UdcWatcher:
    """Cheap poller for ``/sys/class/udc/<udc>/state`` used by the session loop."""

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
