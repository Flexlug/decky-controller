"""The ``Status`` dict the frontend shows: hardware facts + the daemon session view."""
from __future__ import annotations

import os
from typing import Any, Optional

from deckhw.neptune import find_neptune
from deckhw.port import read_port_status
from deckhw.sysfs import Sysfs

from .daemon.events import CAPTURED_STATES, JsonDict
from .session import SessionView

DEFAULT_EXTCON: dict[str, int] = {"USB": 0, "USB-HOST": 0}


def hardware_facts(sysfs_root: str = "/sys", dev_root: str = "/dev") -> JsonDict:
    """What sysfs says right now; the backend's own view for connectivity polling and the fallback when
    ``deckgadget status`` is unavailable (the CLI wins otherwise). No modprobe from here — too frequent."""
    port = read_port_status(sysfs_root, dev_root, use_modprobe=False)
    neptune = find_neptune(sysfs_root, dev_root)
    return {
        "kernel": os.uname().release,
        "model": Sysfs(sysfs_root).text("class", "dmi", "id", "product_name"),
        "drd_enabled": port.drd_enabled,
        "udc_name": port.udc_name,
        "udc_state": port.udc_state,
        "udc_speed": port.udc_speed,
        "extcon": dict(port.extcon) or dict(DEFAULT_EXTCON),
        "host_connected": port.host_connected,
        "neptune_present": neptune is not None,
        "neptune_captured": bool(neptune and neptune.captured),
        "cable_power": port.cable_power,
        "pd_contract_mv": port.pd_contract_mv,
        "pd_contract_ma": port.pd_contract_ma,
        "cable_kind": port.cable_kind,
    }


def connectivity_signature(facts: JsonDict) -> tuple:
    """The part of the hardware facts whose change should trigger a ``status`` event while idle."""
    extcon = facts.get("extcon") or {}
    return (facts.get("drd_enabled"), facts.get("udc_name"), facts.get("udc_state"), facts.get("host_connected"),
            facts.get("neptune_present"), facts.get("neptune_captured"), extcon.get("USB"), extcon.get("USB-HOST"),
            facts.get("cable_kind"), facts.get("cable_power"), facts.get("pd_contract_mv"))


def build_status(*, plugin_version: str, facts: JsonDict, cli_status: Optional[JsonDict], cli_error: Optional[str],
                 session: SessionView, running: bool, daemon_pid: Optional[int], settings: JsonDict) -> JsonDict:
    """Hardware facts (CLI first, sysfs fallback) merged with the daemon session. The daemon's own ``screen``
    event decides ``screen_off``; before it arrives it is inferred from the settings and the state."""
    state = session.state if running else "IDLE"
    status: JsonDict = {"ok": True, "plugin_version": plugin_version, **facts}
    if cli_status:
        status.update(cli_status)
    if session.screen_off is not None:
        screen_off: Any = session.screen_off
    else:
        screen_off = bool(settings["screen_off"]) and state in CAPTURED_STATES
    status.update({
        "neptune_captured": bool(status.get("neptune_captured")) or (running and state in CAPTURED_STATES),
        "daemon_running": running,
        "daemon_pid": daemon_pid if running else None,
        "session_state": state,
        "session_detail": session.detail if running else "",
        "active_profile": session.active_profile if running else None,
        "transport": session.transport if running else None,
        "screen_off": bool(screen_off) if running else False,
        "last_error": session.last_error,
        "metrics": dict(session.metrics),
        "status_error": cli_error,   # non-null ⇒ hardware fields came from the sysfs fallback
    })
    return status
