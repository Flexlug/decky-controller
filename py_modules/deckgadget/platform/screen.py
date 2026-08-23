"""Screen handling while controller mode is active: display sleep + touch-to-wake.

Three strategies, tried in this order when ``method="auto"``:

1. :class:`GamescopeSleep` — **Gaming Mode**. gamescope listens on the Wayland socket
   ``$XDG_RUNTIME_DIR/gamescope-0`` (``/run/user/1000`` for user ``deck``) and exposes the ConVar
   ``drm_sleep_internal_screen`` ("Force the internal screen to be asleep"); this is what Steam's own
   idle "turn off screen" uses and it really powers the OLED panel down.  We set it through the
   ``gamescopectl`` CLI (``gamescopectl drm_sleep_internal_screen 1|0``).
2. :class:`KscreenDpms` — **Desktop Mode** (KDE ``kwin_wayland``): ``kscreen-doctor --dpms off|on``
   run as the ``deck`` user against ``/run/user/1000/wayland-0``.  Optional, may be unavailable.
3. :class:`Backlight` — ``/sys/class/backlight/amdgpu_bl0/brightness`` = 0.  On the OLED Deck this only
   dims the panel to its minimum (verified on the device), so it is the last resort.  The previous
   brightness is saved to a state file (``/run/deckgadget/brightness``, fallback ``/tmp``) **before**
   being set to 0 so the recovery path (``guard.recover``) can restore it even after a crash.

Touch wake: the FTS3528 touchscreen stays alive with the screen asleep; we read its evdev node
(``struct input_event``) in a thread and wake the screen for ``touch_wake_seconds`` on every touch
(with the *same* method that put it to sleep) so the user can press "Stop" in the Decky modal.
"""
from __future__ import annotations

import os
import select
import shutil
import stat
import struct
import subprocess
import threading
import time
from typing import Callable, Dict, List, NamedTuple, Optional, Tuple

from ..util.log import get_logger

log = get_logger("screen")

BACKLIGHT_DIR = "/sys/class/backlight/amdgpu_bl0"
TOUCHSCREEN_NAME_SUBSTR = "FTS3528"
STATE_DIRS = ("/run/deckgadget", "/tmp/deckgadget")
STATE_FILE_NAME = "brightness"

# Display-sleep strategies (gamescope / kscreen).
RUN_USER_BASE = "/run/user"
DECK_UID = 1000
DECK_GID = 1000
DECK_RUNTIME_DIR = os.path.join(RUN_USER_BASE, str(DECK_UID))
GAMESCOPE_SOCKET_PREFIX = "gamescope-"
GAMESCOPECTL = "gamescopectl"                    # /usr/bin/gamescopectl on SteamOS
GAMESCOPE_SLEEP_CONVAR = "drm_sleep_internal_screen"
KSCREEN_DOCTOR = "kscreen-doctor"
KDE_WAYLAND_DISPLAY = "wayland-0"
COMMAND_TIMEOUT_S = 3.0
SCREEN_METHODS = ("auto", "gamescope", "kscreen", "backlight")
DEFAULT_SCREEN_METHOD = "auto"

# struct input_event on x86_64: struct timeval (2 x long) + u16 type + u16 code + s32 value = 24 bytes
INPUT_EVENT = struct.Struct("<qqHHi")
EV_SYN = 0x00
EV_KEY = 0x01
EV_ABS = 0x03
BTN_TOUCH = 0x14A
ABS_MT_TRACKING_ID = 0x39


def _read(path: str) -> Optional[str]:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read().strip()
    except OSError:
        return None


def _write(path: str, text: str) -> None:
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


def default_state_file(run_dir: Optional[str] = None) -> str:
    """First writable state dir wins (``/run/deckgadget`` is created if possible)."""
    dirs = (run_dir,) if run_dir else STATE_DIRS
    for d in dirs:
        if not d:
            continue
        try:
            os.makedirs(d, exist_ok=True)
            if os.access(d, os.W_OK):
                return os.path.join(d, STATE_FILE_NAME)
        except OSError:
            continue
    return os.path.join("/tmp", "deckgadget-" + STATE_FILE_NAME)


class Backlight:
    """Save / turn off / restore brightness for one backlight device."""

    def __init__(self, backlight_dir: str = BACKLIGHT_DIR, state_file: Optional[str] = None) -> None:
        self.dir = backlight_dir
        self.state_file = state_file or default_state_file()
        self._saved: Optional[int] = None

    @property
    def available(self) -> bool:
        return os.path.exists(os.path.join(self.dir, "brightness"))

    def brightness(self) -> Optional[int]:
        v = _read(os.path.join(self.dir, "brightness"))
        try:
            return int(v) if v is not None else None
        except ValueError:
            return None

    def max_brightness(self) -> int:
        v = _read(os.path.join(self.dir, "max_brightness"))
        try:
            return max(1, int(v)) if v else 255
        except ValueError:
            return 255

    def set_brightness(self, value: int) -> None:
        _write(os.path.join(self.dir, "brightness"), str(int(value)))

    def saved_value(self) -> Optional[int]:
        """Value remembered in memory or in the state file (``None`` when nothing saved)."""
        if self._saved is not None:
            return self._saved
        v = _read(self.state_file)
        try:
            return int(v) if v else None
        except ValueError:
            return None

    def _safe_value(self, saved: Optional[int]) -> int:
        # Never "restore" to 0 — that would leave the Deck dark.
        if saved is None or saved <= 0:
            return max(1, self.max_brightness() // 2)
        return saved

    def save_and_off(self) -> bool:
        """Save the current brightness and switch the backlight off.

        Returns ``True`` when the backlight was actually turned off, ``False`` when there is
        no backlight device (the caller must not report the screen as off in that case).
        """
        if not self.available:
            log.warning("backlight %s not available; screen off skipped", self.dir)
            return False
        cur = self.brightness()
        prev = self.saved_value()
        # Keep an earlier saved value if the current one is 0 (e.g. we crashed mid-session).
        value = cur if cur and cur > 0 else (prev if prev else self._safe_value(None))
        try:
            os.makedirs(os.path.dirname(self.state_file), exist_ok=True)
            _write(self.state_file, str(value))
        except OSError as exc:
            log.warning("cannot persist brightness to %s: %s", self.state_file, exc)
        self._saved = value
        self.set_brightness(0)
        log.info("backlight off (saved brightness %d)", value)
        return True

    def off(self) -> None:
        if self.available:
            self.set_brightness(0)

    def restore(self, forget: bool = True) -> Optional[int]:
        """Write the saved brightness back; returns the value written (``None`` if nothing to do)."""
        if not self.available:
            return None
        saved = self.saved_value()
        if saved is None:
            return None  # nothing saved by us: leave the user's brightness alone
        value = self._safe_value(saved)
        self.set_brightness(value)
        if forget:
            self._saved = None
            try:
                os.unlink(self.state_file)
            except OSError:
                pass
        log.info("backlight restored to %d", value)
        return value


# --------------------------------------------------------------------------------------
# Command runner (injectable for tests)
# --------------------------------------------------------------------------------------

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
        text = " ".join(s.strip() for s in (self.stdout, self.stderr, self.error or "") if s and s.strip())
        text = " ".join(text.split())
        return text[:limit]


# runner(argv, env, timeout, user=(uid, gid) | None) -> CommandResult
CommandRunner = Callable[..., CommandResult]


def run_command(argv: List[str], env: Dict[str, str], timeout: float = COMMAND_TIMEOUT_S,
                user: Optional[Tuple[int, int]] = None) -> CommandResult:
    """Run ``argv`` with exactly ``env``, capture output, never raise.

    ``user=(uid, gid)`` drops privileges in the child.  ``subprocess`` does the setgid/setuid itself
    (``user=``/``group=``/``extra_groups=`` are handled by ``_posixsubprocess`` between fork and exec),
    which — unlike ``preexec_fn`` — is safe in a multi-threaded daemon.
    """
    kwargs: Dict[str, object] = {}
    if user is not None and (int(user[0]) != os.geteuid() or int(user[1]) != os.getegid()):
        # Only switch when we are not that user already (root on the Deck); a non-root dev box
        # running as uid 1000 needs no setgroups/setgid/setuid (which would fail with EPERM).
        kwargs.update(user=int(user[0]), group=int(user[1]), extra_groups=[])
    try:
        cp = subprocess.run(argv, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, timeout=timeout, text=True, errors="replace",
                            **kwargs)  # type: ignore[arg-type]
        return CommandResult(cp.returncode, cp.stdout or "", cp.stderr or "")
    except subprocess.TimeoutExpired as exc:
        def _s(b: object) -> str:
            if isinstance(b, bytes):
                return b.decode("utf-8", "replace")
            return str(b) if b else ""
        return CommandResult(None, _s(exc.stdout), _s(exc.stderr), f"timeout after {timeout:.1f}s")
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        return CommandResult(None, "", "", f"{type(exc).__name__}: {exc}")


def _resolve_binary(name_or_path: str) -> Optional[str]:
    """Absolute path for ``name_or_path``; ``None`` if absent.

    System directories are checked before ``PATH``: the daemon runs as root and must not pick up a
    same-named binary from whatever PATH the supervisor happened to export.
    """
    if os.path.isabs(name_or_path):
        return name_or_path if os.path.isfile(name_or_path) else None
    for d in ("/usr/bin", "/usr/local/bin", "/bin"):
        p = os.path.join(d, name_or_path)
        if os.path.isfile(p) and os.access(p, os.X_OK):
            return p
    return shutil.which(name_or_path)


def _is_socket(path: str) -> bool:
    try:
        return stat.S_ISSOCK(os.stat(path).st_mode)
    except OSError:
        return False


# --------------------------------------------------------------------------------------
# Strategies
# --------------------------------------------------------------------------------------

class ScreenMethod:
    """Common interface of the screen-off strategies.  Every call is best-effort and never raises."""

    name: str = "none"

    def available(self) -> bool:
        return False

    def sleep(self) -> bool:
        """Put the panel to sleep; ``True`` when it was actually done."""
        return False

    def wake(self) -> bool:
        """Temporary wake (touch); the screen is expected to go back to sleep with :meth:`sleep`."""
        return False

    def release(self) -> bool:
        """Permanent wake at the end of the session (cleans up whatever ``sleep`` saved)."""
        return self.wake()

    def info(self) -> Dict[str, object]:
        return {"available": self.available()}


def find_gamescope_socket(run_user_base: str = RUN_USER_BASE, prefer_uid: int = DECK_UID,
                          runtime_dir: Optional[str] = None) -> Optional[Tuple[str, str]]:
    """``(runtime_dir, display_name)`` of a live gamescope Wayland socket, or ``None``.

    Scans ``<run_user_base>/<uid>/gamescope-*`` (``runtime_dir`` restricts the scan to one directory).
    The ``deck`` user's directory (uid ``prefer_uid``) wins over others, ``gamescope-0`` over higher numbers.
    """
    if runtime_dir:
        dirs = [runtime_dir]
    else:
        try:
            entries = os.listdir(run_user_base)
        except OSError:
            return None
        entries = [e for e in entries if e.isdigit()]
        entries.sort(key=lambda e: (0 if int(e) == prefer_uid else 1, int(e)))
        dirs = [os.path.join(run_user_base, e) for e in entries]
    for d in dirs:
        try:
            names = [n for n in os.listdir(d) if n.startswith(GAMESCOPE_SOCKET_PREFIX)]
        except OSError:
            continue

        def _key(n: str) -> Tuple[int, str]:
            suffix = n[len(GAMESCOPE_SOCKET_PREFIX):]
            return (int(suffix) if suffix.isdigit() else 1 << 30, n)

        for n in sorted(names, key=_key):
            if _is_socket(os.path.join(d, n)):
                return d, n
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

    # -- discovery --------------------------------------------------------------------
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
        rd, disp = self.runtime_dir, self.display
        return os.path.join(rd, disp) if rd and disp else None

    def binary(self) -> Optional[str]:
        # An explicitly injected path is trusted (tests; unusual installs).
        return self._binary if self._binary else _resolve_binary(GAMESCOPECTL)

    def available(self) -> bool:
        return self.discover() is not None and self.binary() is not None

    def env(self) -> Dict[str, str]:
        return {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "XDG_RUNTIME_DIR": self.runtime_dir or DECK_RUNTIME_DIR,
            "GAMESCOPE_WAYLAND_DISPLAY": self.display or "gamescope-0",
        }

    # -- actions ----------------------------------------------------------------------
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
            res = self.runner(argv, self.env(), self.timeout, None)
        except Exception as exc:  # noqa: BLE001 - never raise
            log.warning("gamescope %s failed: %s", what, exc)
            return False
        # gamescopectl exits 0 even when gamescope does not know the ConVar (it prints
        # "Command not found." — verified with gamescope 3.16); only a connection failure
        # ("Failed to open GAMESCOPE_WAYLAND_DISPLAY.") gives rc=1.  Treat both as failure so
        # ``auto`` falls through instead of believing the panel is asleep.
        output = res.tail()
        if res.ok and "command not found" not in output.lower():
            log.info("gamescope display %s (%s=%s via %s)", what, GAMESCOPE_SLEEP_CONVAR, "1" if asleep else "0",
                     self.socket_path)
            return True
        log.warning("gamescope %s failed (rc=%s): %s", what, res.returncode, output or "no output")
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
            res = self.runner(argv, self.env(), self.timeout, (self.uid, self.gid))
        except Exception as exc:  # noqa: BLE001
            log.warning("kscreen %s failed: %s", what, exc)
            return False
        if res.ok:
            log.info("kscreen dpms %s (%s)", "off" if asleep else "on", self.socket_path)
            return True
        log.warning("kscreen %s failed (rc=%s): %s", what, res.returncode, res.tail() or "no output")
        return False

    def sleep(self) -> bool:
        return self._set(True)

    def wake(self) -> bool:
        return self._set(False)

    def info(self) -> Dict[str, object]:
        return {"available": self.available(), "socket": self.socket_path, "binary": self.binary()}


class BacklightDim(ScreenMethod):
    """Fallback: :class:`Backlight` brightness 0 (state file + safe restore, unchanged semantics)."""

    name = "backlight"

    def __init__(self, backlight: Optional[Backlight] = None) -> None:
        self.backlight = backlight or Backlight()
        self._engaged = False

    def available(self) -> bool:
        return self.backlight.available

    def sleep(self) -> bool:
        if not self._engaged:
            # First time: remember the brightness (memory + state file), then 0.
            ok = self.backlight.save_and_off()
            self._engaged = ok
            return ok
        self.backlight.off()   # re-sleep after a touch wake: saved value stays
        return self.backlight.available

    def wake(self) -> bool:
        if not self.backlight.available:
            return False
        self.backlight.restore(forget=False)
        return True

    def release(self) -> bool:
        self._engaged = False
        if not self.backlight.available:
            return False
        self.backlight.restore(forget=True)
        return True

    def info(self) -> Dict[str, object]:
        return {"available": self.available(), "dir": self.backlight.dir}


# --------------------------------------------------------------------------------------
# Touchscreen
# --------------------------------------------------------------------------------------

def find_touchscreen(sysfs: str = "/sys", dev: str = "/dev",
                     name_substr: str = TOUCHSCREEN_NAME_SUBSTR) -> Optional[str]:
    """``/dev/input/eventN`` of the device whose name contains ``name_substr``."""
    base = os.path.join(sysfs, "class", "input")
    try:
        entries = sorted(os.listdir(base), key=lambda s: (len(s), s))
    except OSError:
        entries = []
    for entry in entries:
        if not entry.startswith("event"):
            continue
        name = _read(os.path.join(base, entry, "device", "name")) or ""
        if name_substr.lower() in name.lower():
            return os.path.join(dev, "input", entry)
    # Fallback: /proc/bus/input/devices ("N: Name=..." followed by "H: Handlers=... eventN")
    try:
        with open("/proc/bus/input/devices", "r", encoding="utf-8", errors="replace") as f:
            blocks = f.read().split("\n\n")
    except OSError:
        return None
    for block in blocks:
        if name_substr.lower() not in block.lower():
            continue
        for line in block.splitlines():
            if line.startswith("H:"):
                for tok in line.split():
                    if tok.startswith("event"):
                        return os.path.join(dev, "input", tok)
    return None


def parse_input_events(buf: bytes):
    """Yield ``(type, code, value)`` tuples from a raw evdev read."""
    n = len(buf) // INPUT_EVENT.size
    for i in range(n):
        _sec, _usec, typ, code, value = INPUT_EVENT.unpack_from(buf, i * INPUT_EVENT.size)
        yield typ, code, value


def is_touch_event(typ: int, code: int, value: int) -> bool:
    if typ == EV_KEY and code == BTN_TOUCH and value:
        return True
    if typ == EV_ABS and code == ABS_MT_TRACKING_ID and value >= 0:
        return True
    return False


class TouchWatcher:
    """Background reader of an evdev node; calls ``on_touch()`` (debounced) on finger down."""

    def __init__(self, event_path: str, on_touch: Callable[[], None], debounce_s: float = 0.2) -> None:
        self.path = event_path
        self.on_touch = on_touch
        self.debounce_s = debounce_s
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._fd = -1
        self.last_touch = 0.0

    def start(self) -> None:
        self._fd = os.open(self.path, os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC)
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="touch-watch", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        t = self._thread
        if t is not None and t is not threading.current_thread():
            t.join(timeout=1.0)
        self._thread = None
        if self._fd >= 0:
            try:
                os.close(self._fd)
            except OSError:
                pass
            self._fd = -1

    def _run(self) -> None:
        fd = self._fd
        while not self._stop.is_set():
            try:
                r, _, _ = select.select([fd], [], [], 0.25)
            except (OSError, ValueError):
                break
            if not r:
                continue
            try:
                buf = os.read(fd, INPUT_EVENT.size * 64)
            except BlockingIOError:
                continue
            except OSError as exc:
                log.warning("touchscreen read failed: %s", exc)
                break
            if not buf:
                break
            now = time.monotonic()
            for typ, code, value in parse_input_events(buf):
                if is_touch_event(typ, code, value) and now - self.last_touch >= self.debounce_s:
                    self.last_touch = now
                    try:
                        self.on_touch()
                    except Exception as exc:  # noqa: BLE001 - never kill the watcher thread
                        log.warning("touch callback failed: %s", exc)
                    break


# --------------------------------------------------------------------------------------
# Controller
# --------------------------------------------------------------------------------------

class ScreenController:
    """Coordinates screen-off (gamescope → kscreen → backlight) + touch wake for the session.

    ``activate()`` when entering CAPTURING picks the method (``method="auto"`` tries the strategies in
    order and keeps the first one whose ``sleep()`` succeeds; an explicit method is used alone) and
    remembers it: touch wake / re-sleep / ``deactivate()`` all go through the *same* strategy, and the
    backlight is never touched while gamescope or kscreen is in charge.
    ``on_change(off: bool, method: str)`` is called whenever the effective screen state changes, plus
    once at activation with ``(False, "none")`` when no strategy could turn the screen off.
    """

    def __init__(self, backlight: Optional[Backlight] = None, touch_event: Optional[str] = None,
                 wake_seconds: float = 5.0, on_change: Optional[Callable[[bool, str], None]] = None,
                 sysfs: str = "/sys", dev: str = "/dev", method: str = DEFAULT_SCREEN_METHOD,
                 gamescope: Optional[ScreenMethod] = None, kscreen: Optional[ScreenMethod] = None) -> None:
        if method not in SCREEN_METHODS:
            raise ValueError(f"unknown screen method {method!r} (expected one of {SCREEN_METHODS})")
        self.backlight = backlight or Backlight()
        self.gamescope: ScreenMethod = gamescope if gamescope is not None else GamescopeSleep()
        self.kscreen: ScreenMethod = kscreen if kscreen is not None else KscreenDpms()
        self.backlight_method: ScreenMethod = BacklightDim(self.backlight)
        self.requested_method = method
        self.touch_event = touch_event if touch_event is not None else find_touchscreen(sysfs, dev)
        self.wake_seconds = wake_seconds
        self.on_change = on_change
        self._active = False
        self._off = False
        self._method: Optional[ScreenMethod] = None
        self._lock = threading.Lock()
        self._timer: Optional[threading.Timer] = None
        self._watcher: Optional[TouchWatcher] = None

    @property
    def active(self) -> bool:
        return self._active

    @property
    def is_off(self) -> bool:
        return self._off

    @property
    def method(self) -> str:
        """Name of the strategy in charge (``"none"`` before activation / when nothing worked)."""
        return self._method.name if self._method is not None else "none"

    def candidates(self) -> List[ScreenMethod]:
        """Strategies in the order they are tried for the requested method."""
        order = {"gamescope": self.gamescope, "kscreen": self.kscreen, "backlight": self.backlight_method}
        if self.requested_method == "auto":
            return [order["gamescope"], order["kscreen"], order["backlight"]]
        return [order[self.requested_method]]

    def _set_off(self, off: bool) -> None:
        if off != self._off:
            self._off = off
            if self.on_change:
                try:
                    self.on_change(off, self.method)
                except Exception as exc:  # noqa: BLE001
                    log.warning("screen on_change callback failed: %s", exc)

    def _choose_and_sleep(self) -> Optional[ScreenMethod]:
        for m in self.candidates():
            try:
                if not m.available():
                    log.info("screen method %s not available", m.name)
                    continue
                if m.sleep():
                    log.info("screen off via %s", m.name)
                    return m
                log.warning("screen method %s could not turn the screen off", m.name)
            except Exception as exc:  # noqa: BLE001 - cosmetic feature, never fatal
                log.warning("screen method %s failed: %s", m.name, exc)
        log.warning("screen stays on: no working screen-off method (requested %s)", self.requested_method)
        return None

    def activate(self) -> None:
        with self._lock:
            if self._active:
                return
            self._active = True
            self._method = self._choose_and_sleep()
            if self._method is not None:
                # Only report "screen off" (-> {"ev":"screen","off":true,"method":...}) when it really is.
                self._set_off(True)
            elif self.on_change:
                # Nothing worked: say so explicitly ({"ev":"screen","off":false,"method":"none"}) — the
                # backend otherwise infers Status.screen_off from the settings while CAPTURING+.
                try:
                    self.on_change(False, "none")
                except Exception as exc:  # noqa: BLE001
                    log.warning("screen on_change callback failed: %s", exc)
            if self.touch_event:
                try:
                    self._watcher = TouchWatcher(self.touch_event, self._on_touch)
                    self._watcher.start()
                    log.info("touch wake armed on %s (%.1fs, method %s)", self.touch_event, self.wake_seconds,
                             self.method)
                except OSError as exc:
                    log.warning("touch wake unavailable (%s): %s", self.touch_event, exc)
                    self._watcher = None
            else:
                log.warning("touchscreen not found; touch wake disabled")

    def _on_touch(self) -> None:
        with self._lock:
            if not self._active:
                return
            if self._off and self._method is not None:
                try:
                    if self._method.wake():
                        self._set_off(False)
                    else:
                        log.warning("cannot wake the screen on touch (%s)", self._method.name)
                except Exception as exc:  # noqa: BLE001
                    log.warning("cannot wake the screen on touch: %s", exc)
            if self._timer is not None:
                self._timer.cancel()
            self._timer = threading.Timer(self.wake_seconds, self._wake_expired)
            self._timer.daemon = True
            self._timer.start()

    def _wake_expired(self) -> None:
        with self._lock:
            self._timer = None
            if not self._active or self._off or self._method is None:
                return
            try:
                if self._method.sleep():
                    self._set_off(True)
                else:
                    log.warning("cannot turn the screen off again (%s)", self._method.name)
            except Exception as exc:  # noqa: BLE001
                log.warning("cannot turn the screen off again: %s", exc)

    def deactivate(self) -> None:
        with self._lock:
            was_active = self._active
            self._active = False
            if self._timer is not None:
                self._timer.cancel()
                self._timer = None
            watcher, self._watcher = self._watcher, None
            method = self._method
        if watcher is not None:
            watcher.stop()
        try:
            if method is not None:
                # Permanent wake with the SAME method; gamescope/kscreen never touch the backlight.
                if not method.release():
                    log.warning("screen release via %s reported failure", method.name)
            elif was_active or self._off or self.backlight.saved_value() is not None:
                # Nothing was in charge (or a stale state file from a crashed backlight session): the
                # old backlight semantics — restore only what we saved, and never to 0.
                self.backlight.restore(forget=True)
        except Exception as exc:  # noqa: BLE001
            log.warning("cannot restore the screen: %s", exc)
        self._set_off(False)
        with self._lock:
            self._method = None
