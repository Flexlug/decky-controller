"""Reader for attribute-file trees (sysfs, configfs, /proc). Failed reads are logged once, here, at DEBUG."""
import logging
import os
from typing import List, Optional

log = logging.getLogger("deckhw.sysfs")


class Sysfs:
    def __init__(self, root: str = "/sys") -> None:
        self.root = root

    def path(self, *parts: str) -> str:
        return os.path.join(self.root, *parts)

    def exists(self, *parts: str) -> bool:
        return os.path.exists(self.path(*parts))

    def isdir(self, *parts: str) -> bool:
        return os.path.isdir(self.path(*parts))

    def text(self, *parts: str, default: Optional[str] = None) -> Optional[str]:
        """Stripped file content; ``default`` when the file is missing or unreadable."""
        path = self.path(*parts)
        try:
            with open(path, encoding="utf-8", errors="replace") as attribute:
                return attribute.read().strip()
        except OSError as exc:
            log.debug("cannot read %s: %s", path, exc)
            return default

    def int(self, *parts: str, base: int = 10, default: Optional[int] = None) -> Optional[int]:
        text = self.text(*parts)
        if text is None:
            return default
        try:
            return int(text, base)
        except ValueError:
            log.debug("%s is not a base-%d integer: %r", self.path(*parts), base, text)
            return default

    def hex(self, *parts: str, default: Optional[int] = None) -> Optional[int]:
        return self.int(*parts, base=16, default=default)

    def listdir(self, *parts: str) -> List[str]:
        """Sorted entries; empty when the directory is missing or unreadable."""
        path = self.path(*parts)
        try:
            return sorted(os.listdir(path))
        except OSError as exc:
            log.debug("cannot list %s: %s", path, exc)
            return []

    def link_name(self, *parts: str) -> Optional[str]:
        """Basename of a symlink target (e.g. the ``driver`` link); ``None`` when there is no link."""
        path = self.path(*parts)
        try:
            return os.path.basename(os.readlink(path))
        except OSError as exc:
            log.debug("no link at %s: %s", path, exc)
            return None
