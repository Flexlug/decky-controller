"""USB Dual-Role detection: with DRD enabled in the BIOS, PCI 04:00.3 is claimed by ``dwc3-pci``."""
import logging
import subprocess
from dataclasses import dataclass
from typing import Optional

from deckhw.sysfs import Sysfs

log = logging.getLogger("deckhw.drd")

DWC3_PCI_MODULE = "dwc3_pci"
DWC3_PCI_DRIVER = "dwc3-pci"
USB_CONTROLLER_CLASS_PREFIX = "0x0c03"


@dataclass
class DrdStatus:
    enabled: bool = False
    pci: Optional[str] = None
    driver: Optional[str] = None
    via: str = "none"  # "driver" | "modalias" | "none"

    def as_dict(self) -> dict:
        return {"enabled": self.enabled, "pci": self.pci, "driver": self.driver, "via": self.via}


def detect_drd(sysfs_root: str = "/sys", use_modprobe: bool = True, modprobe_timeout: float = 2.0) -> DrdStatus:
    """A PCI device bound to ``dwc3-pci`` means DRD; otherwise ask modprobe which module would claim each
    USB controller (``modprobe -R <modalias>``) — the driver may simply not be loaded yet."""
    sysfs = Sysfs(sysfs_root)
    unbound_controllers = []
    for device_name in sysfs.listdir("bus", "pci", "devices"):
        driver = sysfs.link_name("bus", "pci", "devices", device_name, "driver")
        if driver == DWC3_PCI_DRIVER:
            return DrdStatus(True, device_name, driver, "driver")
        device_class = sysfs.text("bus", "pci", "devices", device_name, "class", default="") or ""
        if device_class.lower().startswith(USB_CONTROLLER_CLASS_PREFIX):
            modalias = sysfs.text("bus", "pci", "devices", device_name, "modalias", default="") or ""
            unbound_controllers.append((device_name, driver, modalias))
    if use_modprobe:
        for device_name, driver, modalias in unbound_controllers:
            if modalias and _module_claims(modalias, DWC3_PCI_MODULE, modprobe_timeout):
                return DrdStatus(True, device_name, driver, "modalias")
    return DrdStatus()


def _module_claims(modalias: str, module: str, timeout: float) -> bool:
    try:
        result = subprocess.run(["modprobe", "-R", modalias], capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError) as exc:
        log.debug("modprobe -R %s failed: %s", modalias, exc)
        return False
    return module in result.stdout.replace("-", "_").split()
