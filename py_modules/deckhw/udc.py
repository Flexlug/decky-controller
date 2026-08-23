"""The USB device controller (``/sys/class/udc/<name>``): only meaningful while a gadget is bound —
idle it reads "not attached" even with a PC on the cable."""
from typing import List, Optional

from .sysfs import Sysfs

UDC_STATE_CONFIGURED = "configured"


def udc_names(sysfs_root: str = "/sys") -> List[str]:
    return Sysfs(sysfs_root).listdir("class", "udc")


class Udc:
    """Cheap reader of one UDC's attributes; ``name=None`` picks the first UDC when first used."""

    def __init__(self, sysfs_root: str = "/sys", name: Optional[str] = None) -> None:
        self._sysfs = Sysfs(sysfs_root)
        self.name = name

    def resolve(self) -> Optional[str]:
        if self.name is None:
            names = udc_names(self._sysfs.root)
            self.name = names[0] if names else None
        return self.name

    def attribute(self, attribute: str) -> Optional[str]:
        name = self.resolve()
        return None if name is None else self._sysfs.text("class", "udc", name, attribute)

    def state(self) -> Optional[str]:
        return self.attribute("state")

    def speed(self) -> Optional[str]:
        return self.attribute("current_speed")

    def function(self) -> Optional[str]:
        return self.attribute("function")

    def configured(self) -> bool:
        return self.state() == UDC_STATE_CONFIGURED
