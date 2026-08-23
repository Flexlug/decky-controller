"""The diagnostics dump behind the panel's Diagnostics button."""
from __future__ import annotations

import logging
import os
import sys
from typing import Optional

from controller_backend.daemon.events import JsonDict
from controller_backend.daemon.launcher import DaemonPaths, PYTHON_BIN
from controller_backend.daemon.supervisor import DaemonSupervisor

log = logging.getLogger("controller_backend.diagnostics")

LOG_TAIL_LINES = 50


def tail_file(path: str, count: int, max_bytes: int = 64 * 1024) -> list[str]:
    """Last ``count`` lines, reading at most ``max_bytes`` from the end; an absent file is simply empty."""
    try:
        with open(path, "rb") as log_file:
            log_file.seek(0, os.SEEK_END)
            size = log_file.tell()
            log_file.seek(max(0, size - max_bytes))
            data = log_file.read()
    except OSError as exc:
        log.debug("cannot tail %s: %s", path, exc)
        return []
    lines = data.decode("utf-8", "replace").splitlines()
    if size > max_bytes and lines:
        lines = lines[1:]   # the first line is almost certainly partial
    return lines[-count:]


def build_diagnostics(*, status: JsonDict, plugin_version: str, decky_version: Optional[str], settings: JsonDict,
                      settings_path: str, supervisor: DaemonSupervisor, session_last_kill: Optional[str],
                      cli_status_raw: Optional[JsonDict], cli_status_error: Optional[str],
                      last_recover: Optional[JsonDict], paths: DaemonPaths, plugin_dir: str, runtime_dir: str,
                      log_dir: str) -> JsonDict:
    run = supervisor.run
    return {
        "ok": True,
        "plugin_version": plugin_version,
        "decky_version": decky_version,
        "python": sys.version,
        "python_bin": PYTHON_BIN,
        "kernel": status.get("kernel"),
        "model": status.get("model"),
        "status": status,
        "cli_status_raw": cli_status_raw,
        "cli_status_error": cli_status_error,
        "settings": settings,
        "daemon": {
            "running": supervisor.alive(),
            "pid": supervisor.pid,
            "args": list(run.args) if run else [],
            "started_at": run.started_at if run else None,
            "exit_code": run.exit_code if run else None,
            "stop_requested": bool(run and run.stop_requested),
            "last_kill": session_last_kill,
        },
        "last_recover": last_recover,
        "daemon_log_tail": tail_file(paths.log_path, LOG_TAIL_LINES),
        "daemon_output_tail": list(run.output)[-LOG_TAIL_LINES:] if run else [],
        "paths": {
            "plugin_dir": plugin_dir,
            "py_modules_dir": paths.py_modules_dir,
            "settings": settings_path,
            "runtime_dir": runtime_dir,
            "log_dir": log_dir,
            "daemon_log": paths.log_path,
        },
    }
