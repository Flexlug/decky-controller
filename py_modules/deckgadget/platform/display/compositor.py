"""Compositor-level display sleep: gamescope in Gaming Mode (``gamescopectl drm_sleep_internal_screen`` — what
Steam's idle screen-off uses; really powers the panel down) and KDE in Desktop Mode (``kscreen-doctor --dpms``
as the ``deck`` user)."""
from __future__ import annotations

import os
import shutil
import stat
import subprocess
from typing import Callable, Dict, List, NamedTuple, Optional, Tuple

from ...util.log import get_logger
from .base import ScreenMethod

log = get_logger("screen")

RUN_USER_BASE = "/run/user"
DECK_UID = 1000
DECK_GID = 1000
DECK_RUNTIME_DIR = os.path.join(RUN_USER_BASE, str(DECK_UID))
GAMESCOPE_SOCKET_PREFIX = "gamescope-"
GAMESCOPECTL = "gamescopectl"
GAMESCOPE_SLEEP_CONVAR = "drm_sleep_internal_screen"
KSCREEN_DOCTOR = "kscreen-doctor"
KDE_WAYLAND_DISPLAY = "wayland-0"
COMMAND_TIMEOUT_S = 3.0


class CommandResult(NamedTuple):
    """Outcome of :func:`run_command`; ``returncode`` is ``None`` when the command never finished."""
    returncode: Optional[int]
    stdout: str
    stderr: str
    error: Optional[str] = None     # launch failure / timeout description

    @property
    def ok(self) -> bool:
        return self.returncode == 0

    def tail(self, limit: int = 200) -> str:
        text = " ".join(part.strip() for part in (self.stdout, self.stderr, self.error or "") if part and part.strip())
        text = " ".join(text.split())
        return text[:limit]


# runner(argv, env, timeout, user=(uid, gid) | None) -> CommandResult
CommandRunner = Callable[..., CommandResult]


def run_command(argv: List[str], env: Dict[str, str], timeout: float = COMMAND_TIMEOUT_S,
                user: Optional[Tuple[int, int]] = None) -> CommandResult:
    """Run ``argv`` with exactly ``env``, capture output, never raise. ``user=(uid, gid)`` drops privileges
    via subprocess's own ``user=``/``group=`` (done between fork and exec — unlike ``preexec_fn``, thread-safe)."""
    kwargs: Dict[str, object] = {}
    if user is not None and (int(user[0]) != os.geteuid() or int(user[1]) != os.getegid()):
        # Already that user (non-root dev box): setgroups/setuid would fail with EPERM.
        kwargs.update(user=int(user[0]), group=int(user[1]), extra_groups=[])
    try:
        completed = subprocess.run(argv, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, timeout=timeout, text=True, errors="replace",
                                   **kwargs)  # type: ignore[arg-type]
        return CommandResult(completed.returncode, completed.stdout or "", completed.stderr or "")
    except subprocess.TimeoutExpired as exc:
        def _as_text(output: object) -> str:
            if isinstance(output, bytes):
                return output.decode("utf-8", "replace")
            return str(output) if output else ""
        return CommandResult(None, _as_text(exc.stdout), _as_text(exc.stderr), f"timeout after {timeout:.1f}s")
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        return CommandResult(None, "", "", f"{type(exc).__name__}: {exc}")


def _resolve_binary(name_or_path: str) -> Optional[str]:
    """Absolute path for ``name_or_path``; ``None`` if absent. System directories win over ``PATH`` —
    the daemon runs as root and must not pick up a same-named binary from an inherited PATH."""
    if os.path.isabs(name_or_path):
        return name_or_path if os.path.isfile(name_or_path) else None
    for directory in ("/usr/bin", "/usr/local/bin", "/bin"):
        candidate = os.path.join(directory, name_or_path)
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return shutil.which(name_or_path)


def _is_socket(path: str) -> bool:
    try:
        return stat.S_ISSOCK(os.stat(path).st_mode)
    except OSError:
        return False


def _display_number(socket_name: str) -> Tuple[int, str]:
    suffix = socket_name[len(GAMESCOPE_SOCKET_PREFIX):]
    return (int(suffix) if suffix.isdigit() else 1 << 30, socket_name)


def find_gamescope_socket(run_user_base: str = RUN_USER_BASE, prefer_uid: int = DECK_UID,
                          runtime_dir: Optional[str] = None) -> Optional[Tuple[str, str]]:
    """``(runtime_dir, display_name)`` of a live gamescope Wayland socket under ``<run_user_base>/<uid>/``,
    or ``None``; ``prefer_uid``'s directory wins, then ``gamescope-0`` over higher numbers."""
    if runtime_dir:
        dirs = [runtime_dir]
    else:
        try:
            entries = os.listdir(run_user_base)
        except OSError as exc:
            log.debug("cannot list %s: %s", run_user_base, exc)
            return None
        uid_entries = [entry for entry in entries if entry.isdigit()]
        uid_entries.sort(key=lambda entry: (0 if int(entry) == prefer_uid else 1, int(entry)))
        dirs = [os.path.join(run_user_base, entry) for entry in uid_entries]
    for directory in dirs:
        try:
            names = [name for name in os.listdir(directory) if name.startswith(GAMESCOPE_SOCKET_PREFIX)]
        except OSError as exc:
            log.debug("cannot list %s: %s", directory, exc)
            continue
        for name in sorted(names, key=_display_number):
            if _is_socket(os.path.join(directory, name)):
                return directory, name
    return None


class GamescopeSleep(ScreenMethod):
    """Gaming Mode: ``gamescopectl drm_sleep_internal_screen 1|0`` on the gamescope Wayland socket."""

    name = "gamescope"

    def __init__(self, runtime_dir: Optional[str] = None, display: Optional[str] = None,
                 binary: Optional[str] = None, runner: CommandRunner = run_command,
                 run_user_base: str = RUN_USER_BASE, prefer_uid: int = DECK_UID,
                 timeout: float = COMMAND_TIMEOUT_S) -> None:
        self._runtime_dir = runtime_dir
        self._display = display
        self._binary = binary
        self.runner = runner
        self.run_user_base = run_user_base
        self.prefer_uid = prefer_uid
        self.timeout = timeout
        self.runtime_dir: Optional[str] = None
        self.display: Optional[str] = None

    def discover(self) -> Optional[str]:
        """Locate the socket; returns its path (and caches ``runtime_dir``/``display``)."""
        if self._runtime_dir and self._display:
            path = os.path.join(self._runtime_dir, self._display)
            found = (self._runtime_dir, self._display) if _is_socket(path) else None
        else:
            found = find_gamescope_socket(self.run_user_base, self.prefer_uid, runtime_dir=self._runtime_dir)
        if found is None:
            self.runtime_dir = self.display = None
            return None
        self.runtime_dir, self.display = found
        return os.path.join(*found)

    @property
    def socket_path(self) -> Optional[str]:
        runtime_dir, display = self.runtime_dir, self.display
        return os.path.join(runtime_dir, display) if runtime_dir and display else None

    def binary(self) -> Optional[str]:
        return self._binary if self._binary else _resolve_binary(GAMESCOPECTL)

    def available(self) -> bool:
        return self.discover() is not None and self.binary() is not None

    def env(self) -> Dict[str, str]:
        return {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "XDG_RUNTIME_DIR": self.runtime_dir or DECK_RUNTIME_DIR,
            "GAMESCOPE_WAYLAND_DISPLAY": self.display or "gamescope-0",
        }

    def _set(self, asleep: bool) -> bool:
        what = "sleep" if asleep else "wake"
        try:
            if self.discover() is None:
                log.warning("gamescope %s skipped: no gamescope socket under %s (Desktop Mode?)",
                            what, self._runtime_dir or self.run_user_base)
                return False
            binary = self.binary()
            if binary is None:
                log.warning("gamescope %s skipped: %s not found", what, GAMESCOPECTL)
                return False
            argv = [binary, GAMESCOPE_SLEEP_CONVAR, "1" if asleep else "0"]
            result = self.runner(argv, self.env(), self.timeout, None)
        except Exception as exc:  # noqa: BLE001 - never raise
            log.warning("gamescope %s failed: %s", what, exc)
            return False
        # gamescopectl exits 0 even for an unknown ConVar (prints "Command not found.", seen on gamescope 3.16);
        # only a connection failure gives rc=1. Both must count as failure or ``auto`` would believe the panel sleeps.
        output = result.tail()
        if result.ok and "command not found" not in output.lower():
            log.info("gamescope display %s (%s=%s via %s)", what, GAMESCOPE_SLEEP_CONVAR, "1" if asleep else "0",
                     self.socket_path)
            return True
        log.warning("gamescope %s failed (rc=%s): %s", what, result.returncode, output or "no output")
        return False

    def sleep(self) -> bool:
        return self._set(True)

    def wake(self) -> bool:
        return self._set(False)

    def info(self) -> Dict[str, object]:
        return {"available": self.available(), "socket": self.socket_path, "binary": self.binary()}


class KscreenDpms(ScreenMethod):
    """Desktop Mode (KDE kwin_wayland): ``kscreen-doctor --dpms off|on`` as the ``deck`` user."""

    name = "kscreen"

    def __init__(self, runtime_dir: str = DECK_RUNTIME_DIR, display: str = KDE_WAYLAND_DISPLAY,
                 binary: Optional[str] = None, runner: CommandRunner = run_command,
                 uid: int = DECK_UID, gid: int = DECK_GID, timeout: float = COMMAND_TIMEOUT_S) -> None:
        self.runtime_dir = runtime_dir
        self.display = display
        self._binary = binary
        self.runner = runner
        self.uid = uid
        self.gid = gid
        self.timeout = timeout

    @property
    def socket_path(self) -> str:
        return os.path.join(self.runtime_dir, self.display)

    def binary(self) -> Optional[str]:
        return self._binary if self._binary else _resolve_binary(KSCREEN_DOCTOR)

    def available(self) -> bool:
        return _is_socket(self.socket_path) and self.binary() is not None

    def env(self) -> Dict[str, str]:
        return {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "XDG_RUNTIME_DIR": self.runtime_dir,
            "WAYLAND_DISPLAY": self.display,
            "DBUS_SESSION_BUS_ADDRESS": "unix:path=" + os.path.join(self.runtime_dir, "bus"),
            "QT_QPA_PLATFORM": "wayland",
        }

    def _set(self, asleep: bool) -> bool:
        what = "sleep" if asleep else "wake"
        try:
            if not _is_socket(self.socket_path):
                log.warning("kscreen %s skipped: %s is not a socket (no KDE Wayland session)", what, self.socket_path)
                return False
            binary = self.binary()
            if binary is None:
                log.warning("kscreen %s skipped: %s not found", what, KSCREEN_DOCTOR)
                return False
            argv = [binary, "--dpms", "off" if asleep else "on"]
            result = self.runner(argv, self.env(), self.timeout, (self.uid, self.gid))
        except Exception as exc:  # noqa: BLE001
            log.warning("kscreen %s failed: %s", what, exc)
            return False
        if result.ok:
            log.info("kscreen dpms %s (%s)", "off" if asleep else "on", self.socket_path)
            return True
        log.warning("kscreen %s failed (rc=%s): %s", what, result.returncode, result.tail() or "no output")
        return False

    def sleep(self) -> bool:
        return self._set(True)

    def wake(self) -> bool:
        return self._set(False)

    def info(self) -> Dict[str, object]:
        return {"available": self.available(), "socket": self.socket_path, "binary": self.binary()}
