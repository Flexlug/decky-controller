"""sysfs/udev plumbing: USB role & UDC state, Neptune bind/unbind, screen backlight, recovery guard.

Every helper takes an injectable ``sysfs`` (default ``/sys``) so the logic can be unit
tested against a fake tree.
"""
