"""One snapshot of everything the plugin shows about the hardware: kernel/model, the USB-C port, Neptune."""
import os
from typing import Any, Dict

from deckhw.neptune import find_neptune
from deckhw.port import read_port_status
from deckhw.sysfs import Sysfs


def hardware_facts(sysfs_root: str = "/sys", dev_root: str = "/dev", use_modprobe: bool = False) -> Dict[str, Any]:
    """Port status (``PortStatus.as_dict()`` keys) plus ``kernel``, ``model``, ``neptune_present``,
    ``neptune_captured`` and the Neptune device description. ``use_modprobe`` lets DRD detection ask
    ``modprobe -R`` (a subprocess) — fine once, not every few seconds."""
    port = read_port_status(sysfs_root, dev_root, use_modprobe=use_modprobe)
    neptune = find_neptune(sysfs_root, dev_root)
    return {
        "kernel": os.uname().release,
        "model": Sysfs(sysfs_root).text("class", "dmi", "id", "product_name"),
        **port.as_dict(),
        "neptune_present": neptune is not None,
        "neptune_captured": bool(neptune and neptune.captured),
        "neptune": neptune.as_dict() if neptune else None,
    }
