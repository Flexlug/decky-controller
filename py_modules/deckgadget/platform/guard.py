"""Idempotent rollback of everything a session touches: configfs gadgets, Neptune's usbhid
binding, display sleep, backlight. Best-effort, safe to repeat, never raises."""
from __future__ import annotations

import errno
import glob
import os
from typing import Dict, List, Optional

from deckhw.neptune import CAPTURE_INTERFACES, USBHID_DRIVER, find_neptune

from ..util.fs import write_text
from ..util.log import get_logger
from . import neptune_binding
from .display.backlight import BACKLIGHT_DIR, Backlight
from .display.base import ScreenMethod, default_state_file
from .display.compositor import GamescopeSleep, KscreenDpms

log = get_logger("guard")

CONFIGFS = "/sys/kernel/config"
GADGET_PREFIX = "deckctl"

Report = Dict[str, object]


def _rmdir_quiet(path: str) -> bool:
    try:
        os.rmdir(path)
        return True
    except OSError:
        return False


def _unlink_quiet(path: str) -> bool:
    try:
        os.unlink(path)
        return True
    except OSError:
        return False


def list_gadgets(configfs: str = CONFIGFS, prefix: str = GADGET_PREFIX) -> List[str]:
    """Gadget directories created by us (``usb_gadget/<prefix>*``)."""
    base = os.path.join(configfs, "usb_gadget")
    try:
        return sorted(os.path.join(base, name) for name in os.listdir(base) if name.startswith(prefix))
    except OSError:
        return []


def _sweep_quiet(path: str) -> None:
    """Bottom-up best-effort removal of whatever is left. On configfs attribute files cannot be unlinked
    and default groups cannot be rmdir'ed; both fail quietly and do not block the final gadget rmdir."""
    for root, dirs, files in os.walk(path, topdown=False):
        for name in files:
            _unlink_quiet(os.path.join(root, name))
        for name in dirs:
            entry_path = os.path.join(root, name)
            if os.path.islink(entry_path):
                _unlink_quiet(entry_path)
            else:
                _rmdir_quiet(entry_path)
    _rmdir_quiet(path)


def remove_configfs_gadget(gadget_dir: str) -> Report:
    """Unbind from the UDC and tear the configfs tree down."""
    report: Report = {"gadget": gadget_dir, "existed": os.path.isdir(gadget_dir), "removed": False}
    if not report["existed"]:
        return report
    udc_file = os.path.join(gadget_dir, "UDC")
    try:
        # An empty UDC name means "unregister", but writing "" from Python issues no write(2) at all;
        # the bare newline is what actually reaches gadget_dev_desc_UDC_store().
        write_text(udc_file, "\n")
        report["unbound"] = True
    except OSError as exc:
        report["unbound"] = False if exc.errno == errno.ENODEV else f"error: {exc}"
    # configfs only lets the tree go in this order: function symlinks, configs, functions, strings, gadget.
    for config_dir in sorted(glob.glob(os.path.join(gadget_dir, "configs", "*"))):
        for entry in sorted(os.listdir(config_dir)):
            entry_path = os.path.join(config_dir, entry)
            if os.path.islink(entry_path):
                _unlink_quiet(entry_path)
        for strings_dir in sorted(glob.glob(os.path.join(config_dir, "strings", "*"))):
            _rmdir_quiet(strings_dir)
        _rmdir_quiet(config_dir)
    for function_dir in sorted(glob.glob(os.path.join(gadget_dir, "functions", "*"))):
        _rmdir_quiet(function_dir)
    for strings_dir in sorted(glob.glob(os.path.join(gadget_dir, "strings", "*"))):
        _rmdir_quiet(strings_dir)
    if not _rmdir_quiet(gadget_dir):
        _sweep_quiet(gadget_dir)
    report["removed"] = not os.path.exists(gadget_dir)
    if not report["removed"]:
        log.warning("could not fully remove gadget %s", gadget_dir)
    return report


def _remove_gadgets(configfs: str, prefix: str, report: Report) -> None:
    """Step 1: every configfs gadget of ours. raw-gadget needs nothing here: it dies with the daemon's fd."""
    for gadget_dir in list_gadgets(configfs, prefix):
        try:
            report["gadgets"].append(remove_configfs_gadget(gadget_dir))  # type: ignore[union-attr]
        except Exception as exc:  # noqa: BLE001
            report["errors"].append(f"gadget {gadget_dir}: {exc}")  # type: ignore[union-attr]


def _rebind_neptune(sysfs: str, dev: str, report: Report) -> None:
    """Step 2: interfaces back on usbhid. Re-scans instead of trusting the writes: bind/drivers_probe probe
    synchronously, so the scan is authoritative, and a silently ignored bind would otherwise leave the Deck
    without its controller."""
    errors: List[str] = report["errors"]  # type: ignore[assignment]
    try:
        device = find_neptune(sysfs, dev)
        if device is None:
            report["neptune"] = {"present": False}
            return
        binder = neptune_binding.UsbhidBinder(sysfs)
        bind_errors: List[str] = []
        rebound = neptune_binding.release_interfaces(device, binder, errors=bind_errors)
        rescanned = find_neptune(sysfs, dev)
        still_captured = ([interface.name for number in CAPTURE_INTERFACES
                           if (interface := rescanned.interface(number)) is not None
                           and interface.driver != USBHID_DRIVER]
                          if rescanned is not None else [])
        report["neptune"] = {"present": True, "name": device.name, "rebound": rebound,
                             "still_captured": still_captured}
        for bind_error in bind_errors:
            errors.append(f"neptune: {bind_error}")
        if still_captured:
            errors.append(f"neptune: interfaces still detached from usbhid: {', '.join(still_captured)}")
    except Exception as exc:  # noqa: BLE001
        errors.append(f"neptune: {exc}")


def _wake_display(gamescope: Optional[ScreenMethod], kscreen: Optional[ScreenMethod], report: Report) -> None:
    """Step 3: a crashed session must never leave the panel asleep. Per compositor: ``available`` and, when it
    was reachable, ``woken``; failures are warnings, never errors."""
    warnings: List[str] = report["warnings"]  # type: ignore[assignment]
    display: Report = {}
    try:
        for method in (gamescope if gamescope is not None else GamescopeSleep(),
                       kscreen if kscreen is not None else KscreenDpms()):
            entry: Report = {"available": False}
            try:
                if method.available():
                    entry["available"] = True
                    entry["socket"] = getattr(method, "socket_path", None)
                    woken = bool(method.wake())
                    entry["woken"] = woken
                    if not woken:
                        warnings.append(f"{method.name}: display wake failed")
            except Exception as exc:  # noqa: BLE001
                entry["error"] = str(exc)
                warnings.append(f"{method.name}: {exc}")
            display[method.name] = entry
        report["display"] = display
    except Exception as exc:  # noqa: BLE001
        warnings.append(f"display: {exc}")


def _restore_backlight(backlight_dir: str, state_file: Optional[str], report: Report) -> None:
    """Step 4: the brightness a crashed backlight session saved, if any."""
    try:
        backlight = Backlight(backlight_dir, state_file or default_state_file())
        restored = backlight.restore(forget=True)
        report["backlight"] = {"available": backlight.available, "restored": restored}
    except Exception as exc:  # noqa: BLE001
        report["errors"].append(f"backlight: {exc}")  # type: ignore[union-attr]


def recover(sysfs: str = "/sys", configfs: str = CONFIGFS, dev: str = "/dev",
            backlight_dir: str = BACKLIGHT_DIR, state_file: Optional[str] = None,
            gadget_prefix: str = GADGET_PREFIX, gamescope: Optional[ScreenMethod] = None,
            kscreen: Optional[ScreenMethod] = None) -> Report:
    """Full idempotent rollback; never raises. ``gamescope``/``kscreen`` override the wake strategies."""
    report: Report = {"ok": True, "gadgets": [], "neptune": None, "display": None,
                      "backlight": None, "errors": [], "warnings": []}
    _remove_gadgets(configfs, gadget_prefix, report)
    _rebind_neptune(sysfs, dev, report)
    _wake_display(gamescope, kscreen, report)
    _restore_backlight(backlight_dir, state_file, report)
    for warning in report["warnings"]:  # type: ignore[union-attr]
        log.warning("recover: %s", warning)
    report["ok"] = not report["errors"]
    return report
