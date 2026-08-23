"""Recovery guard: idempotent "undo everything" used by ``deckgadget recover`` and the session.

Steps (each best-effort, each safe to repeat):

1. configfs: unbind and delete every gadget under ``/sys/kernel/config/usb_gadget/deckctl*``;
2. raw-gadget: nothing to do — the gadget disappears with the owning process (fd close);
3. Neptune: rebind ``<dev>:1.0``, ``:1.1``, ``:1.2`` to ``usbhid`` if they lost their driver,
   then re-scan sysfs and report an error if any of them is still detached;
4. display: wake the panel if a crashed session left it asleep — ``gamescopectl
   drm_sleep_internal_screen 0`` when a gamescope socket exists (Gaming Mode) and
   ``kscreen-doctor --dpms on`` when a KDE Wayland session is reachable (Desktop Mode); both are
   idempotent, failures are *warnings* (``report["warnings"]``), not errors;
5. backlight: restore the brightness saved by :class:`~deckgadget.platform.screen.Backlight`.

The function never raises; it returns a report dict describing what it did.
"""
from __future__ import annotations

import errno
import glob
import os
from typing import Dict, List, Optional

from ..util.log import get_logger
from . import neptune as neptune_mod
from .screen import BACKLIGHT_DIR, Backlight, GamescopeSleep, KscreenDpms, ScreenMethod, default_state_file

log = get_logger("guard")

CONFIGFS = "/sys/kernel/config"
GADGET_PREFIX = "deckctl"


def _write(path: str, text: str) -> None:
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


def _rmdir_quiet(path: str) -> bool:
    try:
        os.rmdir(path)
        return True
    except OSError:
        return False


def list_gadgets(configfs: str = CONFIGFS, prefix: str = GADGET_PREFIX) -> List[str]:
    """Gadget directories created by us (``usb_gadget/<prefix>*``)."""
    base = os.path.join(configfs, "usb_gadget")
    try:
        return sorted(os.path.join(base, d) for d in os.listdir(base) if d.startswith(prefix))
    except OSError:
        return []


def _unlink_quiet(path: str) -> bool:
    try:
        os.unlink(path)
        return True
    except OSError:
        return False


def _sweep_quiet(path: str) -> None:
    """Bottom-up best-effort removal of whatever is left (symlinks, files, empty dirs).

    On real configfs attribute files cannot be unlinked (EPERM) and default groups
    (``strings``/``configs``/``functions``/``os_desc``) cannot be rmdir'ed — both fail quietly
    and do not prevent the final ``rmdir`` of the gadget once user-created items are gone.
    """
    for root, dirs, files in os.walk(path, topdown=False):
        for name in files:
            _unlink_quiet(os.path.join(root, name))
        for name in dirs:
            p = os.path.join(root, name)
            if os.path.islink(p):
                _unlink_quiet(p)
            else:
                _rmdir_quiet(p)
    _rmdir_quiet(path)


def remove_configfs_gadget(gadget_dir: str) -> Dict[str, object]:
    """Unbind from the UDC and tear the configfs tree down (mirror of the configfs spike's ``down``)."""
    report: Dict[str, object] = {"gadget": gadget_dir, "existed": os.path.isdir(gadget_dir), "removed": False}
    if not report["existed"]:
        return report
    udc_file = os.path.join(gadget_dir, "UDC")
    try:
        # A bare "\n" (configfs spike: ``echo "" > UDC``) — gadget_dev_desc_UDC_store() strips the trailing
        # newline and an empty name means "unregister".  Writing "" from Python issues no write(2)
        # at all (TextIOWrapper drops empty strings), so the unbind would silently not happen.
        _write(udc_file, "\n")
        report["unbound"] = True
    except OSError as exc:
        # ENODEV: the gadget was not bound to any UDC — nothing to unbind.
        report["unbound"] = False if exc.errno == errno.ENODEV else f"error: {exc}"
    # Order matters on configfs: function symlinks first, then config strings/configs,
    # then functions, then gadget strings, finally the gadget directory itself.
    for cfg in sorted(glob.glob(os.path.join(gadget_dir, "configs", "*"))):
        for entry in sorted(os.listdir(cfg)):
            p = os.path.join(cfg, entry)
            if os.path.islink(p):
                _unlink_quiet(p)
        for s in sorted(glob.glob(os.path.join(cfg, "strings", "*"))):
            _rmdir_quiet(s)
        _rmdir_quiet(cfg)
    for fn in sorted(glob.glob(os.path.join(gadget_dir, "functions", "*"))):
        _rmdir_quiet(fn)
    for s in sorted(glob.glob(os.path.join(gadget_dir, "strings", "*"))):
        _rmdir_quiet(s)
    if not _rmdir_quiet(gadget_dir):
        _sweep_quiet(gadget_dir)
    report["removed"] = not os.path.exists(gadget_dir)
    if not report["removed"]:
        log.warning("could not fully remove gadget %s", gadget_dir)
    return report


def wake_display(gamescope: Optional[ScreenMethod] = None, kscreen: Optional[ScreenMethod] = None,
                 warnings: Optional[List[str]] = None) -> Dict[str, object]:
    """Best-effort, idempotent display wake (step 4 of :func:`recover`).

    Returns ``{"gamescope": {...}, "kscreen": {...}}``; each entry has ``available`` and, when the
    compositor was reachable, ``woken``.  Failures are appended to ``warnings`` (never raised).
    """
    warnings = warnings if warnings is not None else []
    out: Dict[str, object] = {}
    for m in (gamescope if gamescope is not None else GamescopeSleep(),
              kscreen if kscreen is not None else KscreenDpms()):
        entry: Dict[str, object] = {"available": False}
        try:
            if m.available():
                entry["available"] = True
                entry["socket"] = getattr(m, "socket_path", None)
                woken = bool(m.wake())
                entry["woken"] = woken
                if not woken:
                    warnings.append(f"{m.name}: display wake failed")
        except Exception as exc:  # noqa: BLE001
            entry["error"] = str(exc)
            warnings.append(f"{m.name}: {exc}")
        out[m.name] = entry
    return out


def recover(sysfs: str = "/sys", configfs: str = CONFIGFS, dev: str = "/dev",
            backlight_dir: str = BACKLIGHT_DIR, state_file: Optional[str] = None,
            gadget_prefix: str = GADGET_PREFIX, gamescope: Optional[ScreenMethod] = None,
            kscreen: Optional[ScreenMethod] = None) -> Dict[str, object]:
    """Full idempotent rollback. Never raises.

    ``gamescope`` / ``kscreen`` override the display-wake strategies (tests inject fakes; the
    defaults discover the real sockets).
    """
    report: Dict[str, object] = {"ok": True, "gadgets": [], "neptune": None, "display": None,
                                 "backlight": None, "errors": [], "warnings": []}
    errors: List[str] = report["errors"]  # type: ignore[assignment]
    warnings: List[str] = report["warnings"]  # type: ignore[assignment]

    # 1. configfs gadgets
    for g in list_gadgets(configfs, gadget_prefix):
        try:
            report["gadgets"].append(remove_configfs_gadget(g))  # type: ignore[union-attr]
        except Exception as exc:  # noqa: BLE001
            errors.append(f"gadget {g}: {exc}")

    # 2. raw-gadget: owned by the (now dead) daemon process; nothing persistent to undo.

    # 3. Neptune -> usbhid
    try:
        device = neptune_mod.find_neptune(sysfs, dev)
        if device is None:
            report["neptune"] = {"present": False}
        else:
            binder = neptune_mod.UsbhidBinder(sysfs)
            bind_errors: List[str] = []
            rebound = neptune_mod.release_interfaces(device, binder, errors=bind_errors)
            # Verify instead of trusting the writes: a failed/ignored ``bind`` would otherwise leave
            # the Deck without its controller while we report success.  ``bind``/``drivers_probe``
            # probe synchronously, so a re-scan right away is authoritative.
            after = neptune_mod.find_neptune(sysfs, dev)
            still = ([itf.name for n in neptune_mod.CAPTURE_INTERFACES
                      if (itf := after.interface(n)) is not None and itf.driver != neptune_mod.USBHID_DRIVER]
                     if after is not None else [])
            report["neptune"] = {"present": True, "name": device.name, "rebound": rebound,
                                 "still_captured": still}
            for e in bind_errors:
                errors.append(f"neptune: {e}")
            if still:
                errors.append(f"neptune: interfaces still detached from usbhid: {', '.join(still)}")
    except Exception as exc:  # noqa: BLE001
        errors.append(f"neptune: {exc}")

    # 4. display sleep (gamescope / kscreen): a crashed session must never leave the panel asleep.
    try:
        report["display"] = wake_display(gamescope, kscreen, warnings)
    except Exception as exc:  # noqa: BLE001
        warnings.append(f"display: {exc}")

    # 5. backlight
    try:
        bl = Backlight(backlight_dir, state_file or default_state_file())
        restored = bl.restore(forget=True)
        report["backlight"] = {"available": bl.available, "restored": restored}
    except Exception as exc:  # noqa: BLE001
        errors.append(f"backlight: {exc}")

    for w in warnings:
        log.warning("recover: %s", w)
    report["ok"] = not errors
    return report
