"""The Valve EC extcon (``steamdeck-extcon``) picks the port role: ``USB-HOST=1`` means a dock or a
peripheral is attached and the port is a host; otherwise it is a device."""
import logging
from typing import Dict

from deckhw.sysfs import Sysfs

log = logging.getLogger("deckhw.extcon")


def extcon_cables(sysfs_root: str = "/sys") -> Dict[str, int]:
    """``{"USB": 0, "USB-HOST": 1, ...}`` from the first extcon device that reports anything."""
    sysfs = Sysfs(sysfs_root)
    for device_name in sysfs.listdir("class", "extcon"):
        state = sysfs.text("class", "extcon", device_name, "state")
        if not state:
            continue
        cables: Dict[str, int] = {}
        for line in state.splitlines():
            name, separator, value = line.partition("=")
            if not separator:
                continue
            try:
                cables[name.strip()] = int(value.strip())
            except ValueError:
                log.debug("extcon %s: unparsable line %r", device_name, line)
        if cables:
            return cables
    return {}
