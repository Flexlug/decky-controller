"""Decky Controller daemon core: capture the Deck's controller (``sources``), pack it into a gamepad
profile (``profiles``) and push it to a PC through a USB gadget (``transports``). Stdlib + ctypes only;
``python3 -m deckgadget --help``."""

__version__ = "0.1.0"
__all__ = ["__version__"]
