"""Write helpers for sysfs / configfs / state files (reads go through ``deckhw.sysfs.Sysfs``)."""
from typing import Union


def write_text(path: str, text: str) -> None:
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


def write_bytes(path: str, data: Union[bytes, bytearray]) -> None:
    with open(path, "wb") as f:
        f.write(data)
