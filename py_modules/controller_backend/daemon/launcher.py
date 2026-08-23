"""How the daemon process is started: interpreter, module, argv from the settings, environment, paths."""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any

from ..settings import PADDLES

log = logging.getLogger("controller_backend.daemon.launcher")

# Decky Loader is a PyInstaller bundle: its sys.executable is the loader binary, not an interpreter, so the
# daemon runs on SteamOS's own python3 (stdlib + ctypes are all it needs).
PYTHON_BIN = "/usr/bin/python3"
DAEMON_MODULE = "deckgadget"
SUBPROCESS_LINE_LIMIT = 1 << 20
DAEMON_LOG_NAME = "deckgadget.log"
PIDFILE_NAME = "deckgadget.pid"


@dataclass(frozen=True)
class DaemonPaths:
    py_modules_dir: str
    log_path: str
    pidfile: str

    @classmethod
    def for_plugin(cls, plugin_dir: str, log_dir: str, runtime_dir: str) -> "DaemonPaths":
        return cls(py_modules_dir=os.path.join(plugin_dir, "py_modules"),
                   log_path=os.path.join(log_dir, DAEMON_LOG_NAME),
                   pidfile=os.path.join(runtime_dir, PIDFILE_NAME))


def daemon_command(subcommand: str, *args: str) -> list[str]:
    return [PYTHON_BIN, "-m", DAEMON_MODULE, subcommand, *args]


def run_args(settings: dict[str, Any], profile: str, log_path: str) -> list[str]:
    """``deckgadget run`` flags for the given settings (``--screen-method`` is never passed: always auto)."""
    args = [
        "--profile", profile,
        "--transport", str(settings["transport"]),
        "--kill-combo", str(settings["kill_combo"]),
        "--kill-hold-ms", str(int(settings["kill_hold_ms"])),
    ]
    if settings["screen_off"]:
        args.append("--screen-off")
    args += ["--touch-wake-seconds", str(int(settings["touch_wake_seconds"]))]
    args += ["--paddles", ",".join(f"{paddle}={settings['paddles'].get(paddle, 'none')}" for paddle in PADDLES)]
    args += ["--log-file", log_path]
    return args


def daemon_environment() -> dict[str, str]:
    """Decky Loader (a PyInstaller bundle) exports LD_LIBRARY_PATH into itself, which breaks the system
    python3 (decky-loader issue #756) — drop it; PYTHONUNBUFFERED keeps the daemon's JSON lines unbuffered."""
    environment = {key: value for key, value in os.environ.items() if key != "LD_LIBRARY_PATH"}
    environment["PYTHONUNBUFFERED"] = "1"
    return environment


def is_deckgadget_pid(pid: int) -> bool:
    """True if ``pid`` is alive and its command line mentions deckgadget (guards against PID reuse)."""
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as cmdline:
            return b"deckgadget" in cmdline.read()
    except OSError as exc:
        log.debug("pid %s: no readable cmdline (%s)", pid, exc)
        return False
