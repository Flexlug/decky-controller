"""Tiny file helpers for sysfs / configfs / state files."""
import logging
from typing import Optional, Union

log = logging.getLogger("deckgadget.fs")


def read_text(path: str, default: Optional[str] = None) -> Optional[str]:
    """Whole file, stripped; ``default`` when the file is missing or unreadable."""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read().strip()
    except OSError as exc:
        log.debug("cannot read %s: %s", path, exc)
        return default


def write_text(path: str, text: str) -> None:
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


def write_bytes(path: str, data: Union[bytes, bytearray]) -> None:
    with open(path, "wb") as f:
        f.write(data)
