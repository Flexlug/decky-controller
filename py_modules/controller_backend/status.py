"""The ``Status`` dict the frontend shows: hardware facts + the daemon session view."""
from __future__ import annotations

from typing import Any, Optional

from controller_backend.daemon.events import CAPTURED_STATES, JsonDict
from controller_backend.session import SessionView
from deckhw.facts import hardware_facts

STATUS_HARDWARE_KEYS = ("kernel", "model", "drd_enabled", "udc_name", "udc_state", "udc_speed", "extcon",
                        "host_connected", "neptune_present", "neptune_captured", "cable_power",
                        "pd_contract_mv", "pd_contract_ma", "cable_kind")
DEFAULT_EXTCON: dict[str, int] = {"USB": 0, "USB-HOST": 0}


def status_facts(sysfs_root: str = "/sys", dev_root: str = "/dev", drd_known_enabled: bool = False) -> JsonDict:
    """The hardware part of ``Status``, read from sysfs now (no modprobe — this runs every few seconds);
    ``drd_known_enabled`` carries the result of the one-time modprobe-assisted DRD probe."""
    facts = hardware_facts(sysfs_root, dev_root, use_modprobe=False)
    status = {key: facts.get(key) for key in STATUS_HARDWARE_KEYS}
    status["extcon"] = dict(facts.get("extcon") or DEFAULT_EXTCON)
    status["drd_enabled"] = bool(facts.get("drd_enabled")) or drd_known_enabled
    return status


def connectivity_signature(facts: JsonDict) -> tuple:
    """The part of the hardware facts whose change should trigger a ``status`` event while idle."""
    extcon = facts.get("extcon") or {}
    return (facts.get("drd_enabled"), facts.get("udc_name"), facts.get("udc_state"), facts.get("host_connected"),
            facts.get("neptune_present"), facts.get("neptune_captured"), extcon.get("USB"), extcon.get("USB-HOST"),
            facts.get("cable_kind"), facts.get("cable_power"), facts.get("pd_contract_mv"))


def build_status(*, plugin_version: str, facts: JsonDict, session: SessionView, running: bool,
                 daemon_pid: Optional[int], settings: JsonDict) -> JsonDict:
    """Hardware facts merged with the daemon session. The daemon's own ``screen`` event decides
    ``screen_off``; before it arrives it is inferred from the settings and the state."""
    state = session.state if running else "IDLE"
    if session.screen_off is not None:
        screen_off: Any = session.screen_off
    else:
        screen_off = bool(settings["screen_off"]) and state in CAPTURED_STATES
    return {
        "ok": True,
        "plugin_version": plugin_version,
        **facts,
        "neptune_captured": bool(facts.get("neptune_captured")) or (running and state in CAPTURED_STATES),
        "daemon_running": running,
        "daemon_pid": daemon_pid if running else None,
        "session_state": state,
        "session_detail": session.detail if running else "",
        "active_profile": session.active_profile if running else None,
        "transport": session.transport if running else None,
        "screen_off": bool(screen_off) if running else False,
        "last_error": session.last_error,
        "metrics": dict(session.metrics),
    }
