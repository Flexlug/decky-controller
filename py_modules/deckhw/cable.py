"""What is physically on the USB-C port, independent of the gadget: power from ``ACAD`` and the negotiated
USB-PD contract from ``steamdeck_hwmon`` (a PC/hub port gives 5 V, a PD charger 15-20 V)."""
from typing import Dict, Optional, Tuple

from .sysfs import Sysfs

AC_SUPPLY_NAME = "ACAD"
STEAMDECK_HWMON_NAME = "steamdeck_hwmon"
PD_VOLTAGE_LABEL = "PD Contract Voltage"
PD_CURRENT_LABEL = "PD Contract Current"
PC_PORT_MAX_MV = 5500
CABLE_KINDS = ("none", "pc", "charger", "host_device", "unknown")


def cable_power(sysfs_root: str = "/sys") -> Optional[bool]:
    """``ACAD/online`` (or any other ``Mains`` supply); ``None`` when no supply is readable."""
    sysfs = Sysfs(sysfs_root)
    candidates = [AC_SUPPLY_NAME] + [
        name for name in sysfs.listdir("class", "power_supply")
        if name != AC_SUPPLY_NAME and sysfs.text("class", "power_supply", name, "type") == "Mains"]
    for name in candidates:
        online = sysfs.int("class", "power_supply", name, "online")
        if online is not None:
            return online != 0
    return None


def find_hwmon(name: str, sysfs_root: str = "/sys") -> Optional[str]:
    """``hwmonN`` directory name whose ``name`` attribute equals ``name`` (indexes are not stable)."""
    sysfs = Sysfs(sysfs_root)
    for entry in sysfs.listdir("class", "hwmon"):
        if sysfs.text("class", "hwmon", entry, "name") == name:
            return entry
    return None


def pd_contract(sysfs_root: str = "/sys") -> Tuple[Optional[int], Optional[int]]:
    """``(mV, mA)`` of the negotiated USB-PD contract; ``None`` parts when unreadable."""
    hwmon = find_hwmon(STEAMDECK_HWMON_NAME, sysfs_root)
    if hwmon is None:
        return None, None
    sysfs = Sysfs(sysfs_root)
    return (_labelled_channel(sysfs, hwmon, "in", PD_VOLTAGE_LABEL, default_channel=0),
            _labelled_channel(sysfs, hwmon, "curr", PD_CURRENT_LABEL, default_channel=1))


def _labelled_channel(sysfs: Sysfs, hwmon: str, prefix: str, label: str, default_channel: int) -> Optional[int]:
    """``<prefix>N_input`` of the channel labelled ``label``. ``default_channel`` is used only when the driver
    exposes no labels at all for that prefix; labels present but none matching means ``None``, never a guess."""
    label_files = [name for name in sysfs.listdir("class", "hwmon", hwmon)
                   if name.startswith(prefix) and name.endswith("_label") and name[len(prefix):-6].isdigit()]
    if not label_files:
        return sysfs.int("class", "hwmon", hwmon, f"{prefix}{default_channel}_input")
    for label_file in label_files:
        if sysfs.text("class", "hwmon", hwmon, label_file) == label:
            return sysfs.int("class", "hwmon", hwmon, label_file[:-len("_label")] + "_input")
    return None


def classify_cable(extcon: Dict[str, int], power: Optional[bool], pd_contract_mv: Optional[int]) -> str:
    """One of ``CABLE_KINDS``: a host-side device (dock) wins, then "no power", then the PD contract
    (≤ 5.5 V = PC/hub port, above = charger); ``unknown`` when power is unreadable or the contract reads 0."""
    if extcon.get("USB-HOST") == 1:
        return "host_device"
    if power is False:
        return "none"
    if pd_contract_mv is not None and pd_contract_mv > 0:
        return "pc" if pd_contract_mv <= PC_PORT_MAX_MV else "charger"
    return "unknown"
