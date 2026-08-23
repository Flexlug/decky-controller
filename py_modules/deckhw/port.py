"""Everything about the USB-C port in one snapshot: DRD, UDC, extcon, cable classification."""
import os
from dataclasses import dataclass, field
from typing import Dict, Optional

from .cable import cable_power, classify_cable, pd_contract
from .drd import detect_drd
from .extcon import extcon_cables
from .sysfs import Sysfs
from .udc import UDC_STATE_CONFIGURED, Udc


@dataclass
class PortStatus:
    drd_enabled: bool = False
    drd_pci: Optional[str] = None
    udc_name: Optional[str] = None
    udc_state: Optional[str] = None
    udc_speed: Optional[str] = None
    udc_function: Optional[str] = None
    extcon: Dict[str, int] = field(default_factory=dict)
    host_connected: bool = False
    raw_gadget: bool = False
    cable_power: Optional[bool] = None
    pd_contract_mv: Optional[int] = None
    pd_contract_ma: Optional[int] = None
    cable_kind: str = "unknown"

    def as_dict(self) -> dict:
        return {
            "drd_enabled": self.drd_enabled, "drd_pci": self.drd_pci,
            "udc_name": self.udc_name, "udc_state": self.udc_state, "udc_speed": self.udc_speed,
            "udc_function": self.udc_function, "extcon": dict(self.extcon),
            "host_connected": self.host_connected, "raw_gadget_available": self.raw_gadget,
            "cable_power": self.cable_power, "pd_contract_mv": self.pd_contract_mv,
            "pd_contract_ma": self.pd_contract_ma, "cable_kind": self.cable_kind,
        }


def read_port_status(sysfs_root: str = "/sys", dev_root: str = "/dev", use_modprobe: bool = True) -> PortStatus:
    drd = detect_drd(sysfs_root, use_modprobe=use_modprobe)
    udc = Udc(sysfs_root)
    udc_name = udc.resolve()
    udc_state = udc.state()
    extcon = extcon_cables(sysfs_root)
    power = cable_power(sysfs_root)
    contract_mv, contract_ma = pd_contract(sysfs_root)
    return PortStatus(
        drd_enabled=drd.enabled, drd_pci=drd.pci,
        udc_name=udc_name, udc_state=udc_state, udc_speed=udc.speed(), udc_function=udc.function(),
        extcon=extcon, host_connected=(udc_state == UDC_STATE_CONFIGURED),
        raw_gadget=raw_gadget_available(dev_root, sysfs_root),
        cable_power=power, pd_contract_mv=contract_mv, pd_contract_ma=contract_ma,
        cable_kind=classify_cable(extcon, power, contract_mv),
    )


def raw_gadget_available(dev_root: str = "/dev", sysfs_root: str = "/sys") -> bool:
    """``/dev/raw-gadget`` exists, the module is loaded, or its file is in the running kernel's tree."""
    if os.path.exists(os.path.join(dev_root, "raw-gadget")) or Sysfs(sysfs_root).isdir("module", "raw_gadget"):
        return True
    release = Sysfs("/proc").text("sys", "kernel", "osrelease")
    if not release:
        return False
    module_dir = f"/lib/modules/{release}/kernel/drivers/usb/gadget/legacy"
    return any(os.path.exists(os.path.join(module_dir, name))
               for name in ("raw_gadget.ko.zst", "raw_gadget.ko", "raw_gadget.ko.xz"))
