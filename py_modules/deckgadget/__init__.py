"""deckgadget — Decky Controller daemon core.

Turns a Steam Deck into a USB gamepad for another PC:

* ``sources``    — where controller input comes from (built-in Neptune controller via usbfs, demo generator)
* ``profiles``   — what the PC sees (Xbox 360 / XInput, generic HID gamepad): descriptors + report packing
* ``transports`` — how reports reach the PC (raw-gadget on /dev/raw-gadget, configfs f_hid)
* ``platform``   — sysfs plumbing: USB role/UDC, Neptune bind/unbind, screen backlight, recovery guard
* ``session``    — the state machine tying it all together (kill-combo, unplug detection, metrics)

Pure Python 3.13 stdlib + ctypes; no third-party dependencies (runs as root on SteamOS).
CLI: ``python3 -m deckgadget run|status|recover|probe|demo`` (see ``__main__``).
"""

__version__ = "0.1.0"
__all__ = ["__version__"]
