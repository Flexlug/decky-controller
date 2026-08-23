"""Decky Controller — Decky Loader backend (runs as root, plugin.json ``"flags": ["root"]``).

Responsibilities (see docs/ARCHITECTURE.md):

* the callables used by the frontend: ``get_status`` / ``start`` / ``stop`` / ``get_settings`` /
  ``set_settings`` / ``get_diagnostics`` — every one of them returns a JSON dict and never raises;
* supervising the controller daemon (``/usr/bin/python3 -m deckgadget run …``, docs/ARCHITECTURE.md) as a child
  process, translating its JSON-lines stdout into ``status`` / ``toast`` events (``decky.emit``);
* guaranteeing a full rollback (``python3 -m deckgadget recover``) at plugin load, after *every* daemon exit,
  inside ``stop()`` and on unload / uninstall — all volatile kernel state must never outlive a session;
* a tiny JSON settings store in ``DECKY_PLUGIN_SETTINGS_DIR/settings.json`` (stdlib only).

The ``deckgadget`` package is deliberately **never imported** here — it is only ever executed as a subprocess
(``cwd=<plugin>/py_modules``), so a broken core package cannot take the Decky backend down with it.

Runtime: SteamOS Python 3.13, stdlib only (no pip packages).
"""
from __future__ import annotations

import asyncio
import collections
import copy
import json
import os
import signal
import sys
import time
from typing import Any, Optional

# ---------------------------------------------------------------------------------------------------------
# decky import — with a tiny shim so ``python3 -c "import main"`` works on a dev machine without Decky Loader
# ---------------------------------------------------------------------------------------------------------
try:
    import decky  # type: ignore[import-not-found]  # provided by Decky Loader at runtime
except ImportError:  # pragma: no cover — local development / syntax checks only
    import logging as _logging
    import tempfile as _tempfile
    import types as _types

    _DEV_ROOT = os.path.join(_tempfile.gettempdir(), "decky-controller-dev")
    _logging.basicConfig(level=_logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    _shim_logger = _logging.getLogger("decky-controller")

    async def _shim_emit(event: str, *args: Any) -> None:
        _shim_logger.debug("emit %s %s", event, args)

    decky = _types.SimpleNamespace(  # type: ignore[assignment]
        logger=_shim_logger,
        emit=_shim_emit,
        DECKY_PLUGIN_DIR=os.path.dirname(os.path.abspath(__file__)),
        DECKY_PLUGIN_SETTINGS_DIR=os.path.join(_DEV_ROOT, "settings"),
        DECKY_PLUGIN_RUNTIME_DIR=os.path.join(_DEV_ROOT, "runtime"),
        DECKY_PLUGIN_LOG_DIR=os.path.join(_DEV_ROOT, "logs"),
        DECKY_PLUGIN_VERSION=None,
        DECKY_VERSION=None,
    )

JsonDict = dict[str, Any]

# ---------------------------------------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------------------------------------
PLUGIN_NAME = "Decky Controller"
PYTHON_BIN = "/usr/bin/python3"          # SteamOS system Python 3.13 (docs/ARCHITECTURE.md)
DAEMON_MODULE = "deckgadget"             # executed as ``python3 -m deckgadget <cmd>`` in <plugin>/py_modules

STATUS_CLI_TIMEOUT_S = 5.0               # ``deckgadget status`` hard timeout
STATUS_CACHE_TTL_S = 1.0                 # never spawn ``deckgadget status`` more often than this
RECOVER_TIMEOUT_S = 30.0                 # ``deckgadget recover`` hard timeout
STOP_TERM_GRACE_S = 3.0                  # SIGTERM → wait this long → SIGKILL
STOP_KILL_GRACE_S = 2.0
START_FIRST_EVENT_TIMEOUT_S = 2.0        # start() waits this long for the daemon's first event before answering
STATUS_PERIOD_RUNNING_S = 2.0            # periodic ``status`` emit while the daemon runs
STATUS_PERIOD_IDLE_S = 5.0               # connectivity poll (sysfs, cheap) while idle
OUTPUT_RING_SIZE = 200                   # last N daemon stdout/stderr lines kept in memory for diagnostics
LOG_TAIL_LINES = 50
SUBPROCESS_LINE_LIMIT = 1 << 20

PROFILES = ("xbox360", "hid_gamepad")
TRANSPORTS = ("auto", "raw", "hid")
KILL_COMBOS = ("L4+R4", "L5+R5", "L4+L5+R4+R5", "STEAM+QAM")
PADDLES = ("L4", "L5", "R4", "R5")
PADDLE_ACTIONS = ("none", "A", "B", "X", "Y", "LB", "RB", "L3", "R3", "VIEW", "MENU",
                  "DPAD_UP", "DPAD_DOWN", "DPAD_LEFT", "DPAD_RIGHT")
KILL_HOLD_MS_RANGE = (200, 10_000)
TOUCH_WAKE_RANGE = (1, 60)

DEFAULT_SETTINGS: JsonDict = {
    "profile": "xbox360",
    "transport": "auto",                 # auto = raw for xbox360, hid for hid_gamepad
    "kill_combo": "L4+R4",
    "kill_hold_ms": 1500,
    "screen_off": True,
    "touch_wake_seconds": 5,
    "paddles": {"L4": "none", "L5": "none", "R4": "none", "R5": "none"},
}

# Session states as reported by the daemon (docs/ARCHITECTURE.md); "STOPPED" is mapped to "IDLE" for the Status.
SESSION_STATES = ("IDLE", "CAPTURING", "GADGET_UP", "WAITING_HOST", "ACTIVE", "STOPPING")
CAPTURED_STATES = frozenset({"CAPTURING", "GADGET_UP", "WAITING_HOST", "ACTIVE"})
DEFAULT_METRICS: JsonDict = {"hz": 0, "reports": 0, "dropped": 0}

NEPTUNE_VID, NEPTUNE_PID = "28de", "1205"   # built-in Steam Deck controller (docs/HARDWARE.md)


# ---------------------------------------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------------------------------------
def _read_text(path: str) -> Optional[str]:
    """Read a small (sysfs) text file; None if missing/unreadable."""
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read().strip()
    except OSError:
        return None


def _tail_file(path: str, lines: int, max_bytes: int = 64 * 1024) -> list[str]:
    """Last ``lines`` lines of a text file (reads at most ``max_bytes`` from the end)."""
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - max_bytes))
            data = f.read()
    except OSError:
        return []
    out = data.decode("utf-8", "replace").splitlines()
    if size > max_bytes and out:
        out = out[1:]  # first line is almost certainly partial
    return out[-lines:]


def _parse_json_object(text: str) -> Optional[JsonDict]:
    """Parse the JSON object printed by a CLI command; tolerates stray log lines before the JSON."""
    text = text.strip()
    if not text:
        return None
    obj: Any = None
    try:
        obj = json.loads(text)
    except ValueError:
        for line in reversed(text.splitlines()):
            line = line.strip()
            if line.startswith("{"):
                try:
                    obj = json.loads(line)
                    break
                except ValueError:
                    continue
    return obj if isinstance(obj, dict) else None


def _plugin_version() -> str:
    v = getattr(decky, "DECKY_PLUGIN_VERSION", None)
    if isinstance(v, str) and v:
        return v
    try:
        with open(os.path.join(decky.DECKY_PLUGIN_DIR, "package.json"), encoding="utf-8") as f:
            v = json.load(f).get("version")
        if isinstance(v, str) and v:
            return v
    except Exception:
        pass
    return "0.0.0"


def _daemon_env() -> dict[str, str]:
    """Environment for every deckgadget subprocess.

    Decky Loader is a PyInstaller bundle and exports LD_LIBRARY_PATH pointing into itself, which breaks the
    system python3 (decky-loader issue #756) — it must be removed. PYTHONUNBUFFERED guarantees the daemon's
    JSON-lines stdout arrives line by line.
    """
    env = {k: v for k, v in os.environ.items() if k != "LD_LIBRARY_PATH"}
    env["PYTHONUNBUFFERED"] = "1"
    return env


def _is_deckgadget_pid(pid: int) -> bool:
    """True if ``pid`` is alive and its command line mentions deckgadget (guards against PID reuse)."""
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            return b"deckgadget" in f.read()
    except OSError:
        return False


def _sysfs_snapshot() -> JsonDict:
    """Cheap, read-only view of the USB-role / controller state taken straight from sysfs.

    Used (a) to detect connectivity changes between polls without spawning a process and (b) as a fallback
    when the ``deckgadget status`` CLI is unavailable. The CLI remains the source of truth (docs/ARCHITECTURE.md);
    everything here mirrors the facts in docs/HARDWARE.md.
    """
    snap: JsonDict = {
        "kernel": os.uname().release,
        "model": _read_text("/sys/class/dmi/id/product_name"),
        "drd_enabled": False,
        "udc_name": None,
        "udc_state": None,
        "udc_speed": None,
        "extcon": {"USB": 0, "USB-HOST": 0},
        "host_connected": False,
        "neptune_present": False,
        # what the USB-C port physically sees while idle (udc_state is only meaningful once a gadget is bound)
        "cable_power": None,
        "pd_contract_mv": None,
        "pd_contract_ma": None,
        "cable_kind": "unknown",
    }
    # DRD on in BIOS ⇒ PCI 04:00.3 is re-classed by the kernel and claimed by dwc3-pci; off ⇒ xhci_hcd owns it
    # and the dwc3_pci module is not even loaded, so the driver directory does not exist.
    try:
        snap["drd_enabled"] = any(":" in e for e in os.listdir("/sys/bus/pci/drivers/dwc3-pci"))
    except OSError:
        snap["drd_enabled"] = False
    # A UDC exists only while dwc3 is in device role (Deck plugged into a host, nothing in host role).
    try:
        udcs = sorted(os.listdir("/sys/class/udc"))
    except OSError:
        udcs = []
    if udcs:
        snap["udc_name"] = udcs[0]
        snap["udc_state"] = _read_text(f"/sys/class/udc/{udcs[0]}/state")
        snap["udc_speed"] = _read_text(f"/sys/class/udc/{udcs[0]}/current_speed")
        snap["host_connected"] = snap["udc_state"] == "configured"
    # steamdeck-extcon: "USB=0\nUSB-HOST=1\nSDP=0…" — USB=1 ⇒ we are a device, USB-HOST=1 ⇒ we are a host.
    try:
        extcons = sorted(os.listdir("/sys/class/extcon"))
    except OSError:
        extcons = []
    for name in extcons:
        txt = _read_text(f"/sys/class/extcon/{name}/state")
        if not txt:
            continue
        for line in txt.splitlines():
            key, sep, val = line.partition("=")
            if sep and key.strip() in ("USB", "USB-HOST"):
                try:
                    snap["extcon"][key.strip()] = int(val.strip())
                except ValueError:
                    pass
        break
    # Power on the port (EC "ACAD" supply) + the negotiated USB-PD contract (steamdeck_hwmon, found by name:
    # in0 "PD Contract Voltage" mV, curr1 "PD Contract Current" mA). PC port = 5 V, PD charger = 15-20 V.
    # Mirrors deckgadget.platform.usb_role.classify_cable (the CLI value wins when it is available).
    online = _read_text("/sys/class/power_supply/ACAD/online")
    if online in ("0", "1"):
        snap["cable_power"] = online == "1"
    try:
        hwmons = sorted(os.listdir("/sys/class/hwmon"))
    except OSError:
        hwmons = []
    for name in hwmons:
        base = f"/sys/class/hwmon/{name}"
        if _read_text(f"{base}/name") != "steamdeck_hwmon":
            continue
        for key, fn in (("pd_contract_mv", "in0_input"), ("pd_contract_ma", "curr1_input")):
            try:
                snap[key] = int(_read_text(f"{base}/{fn}") or "")
            except ValueError:
                snap[key] = None
        break
    if snap["extcon"].get("USB-HOST") == 1:
        snap["cable_kind"] = "host_device"
    elif snap["cable_power"] is False:
        snap["cable_kind"] = "none"
    elif isinstance(snap["pd_contract_mv"], int) and snap["pd_contract_mv"] > 0:
        snap["cable_kind"] = "pc" if snap["pd_contract_mv"] <= 5500 else "charger"
    # Built-in controller present on the USB bus (it lives on a different xHCI than the USB-C port).
    try:
        for dev in os.listdir("/sys/bus/usb/devices"):
            if ":" in dev or not dev[:1].isdigit():   # skip interfaces ("1-3:1.0") and root hubs ("usb1")
                continue
            base = f"/sys/bus/usb/devices/{dev}"
            if _read_text(f"{base}/idVendor") == NEPTUNE_VID and _read_text(f"{base}/idProduct") == NEPTUNE_PID:
                snap["neptune_present"] = True
                break
    except OSError:
        pass
    return snap


def _connectivity_signature(snap: JsonDict) -> tuple:
    """The part of the sysfs snapshot whose change should trigger a ``status`` event while idle."""
    ext = snap.get("extcon") or {}
    return (snap.get("drd_enabled"), snap.get("udc_name"), snap.get("udc_state"), snap.get("host_connected"),
            snap.get("neptune_present"), ext.get("USB"), ext.get("USB-HOST"),
            snap.get("cable_kind"), snap.get("cable_power"), snap.get("pd_contract_mv"))


# Canonical keys of ``deckgadget status`` are the Status keys themselves (docs/ARCHITECTURE.md); a few shorter
# spellings are tolerated so a slightly different core build still renders a useful status.
_CLI_KEY_ALIASES: dict[str, tuple[str, ...]] = {
    "udc_speed": ("udc_speed",),
    "kernel": ("kernel",),
    "model": ("model",),
    "drd_enabled": ("drd_enabled", "drd"),
    "udc_name": ("udc_name", "udc"),
    "udc_state": ("udc_state",),
    "extcon": ("extcon",),
    "host_connected": ("host_connected", "connected"),
    "neptune_present": ("neptune_present", "neptune"),
    "neptune_captured": ("neptune_captured", "captured"),
    # what the USB-C port physically sees (platform/usb_role.py); additive, all optional in Status
    "cable_power": ("cable_power",),
    "pd_contract_mv": ("pd_contract_mv",),
    "pd_contract_ma": ("pd_contract_ma",),
    "cable_kind": ("cable_kind",),
}
_BOOL_STATUS_KEYS = frozenset({"drd_enabled", "host_connected", "neptune_present", "neptune_captured",
                               "cable_power"})


def _normalize_cli_status(raw: JsonDict) -> JsonDict:
    """Pick the Status fields out of the ``deckgadget status`` JSON (unknown keys are ignored)."""
    out: JsonDict = {}
    for key, aliases in _CLI_KEY_ALIASES.items():
        for alias in aliases:
            if alias not in raw or raw[alias] is None:
                continue
            val = raw[alias]
            if isinstance(val, dict):
                # nested spellings: {"udc": {"name", "state"}}, {"neptune": {"present", "captured"}},
                # {"drd": {"enabled"}}, {"extcon": {"USB": 0, "USB-HOST": 0}}
                if key == "udc_name":
                    out["udc_name"] = val.get("name")
                    out["udc_state"] = val.get("state")
                elif key == "neptune_present":
                    out["neptune_present"] = bool(val.get("present"))
                    if "captured" in val:
                        out["neptune_captured"] = bool(val.get("captured"))
                elif key == "drd_enabled":
                    out["drd_enabled"] = bool(val.get("enabled"))
                elif key == "extcon":
                    out["extcon"] = {str(k): v for k, v in val.items()}
            elif key in _BOOL_STATUS_KEYS:
                out[key] = bool(val)
            elif key == "extcon":
                continue  # malformed — keep the sysfs fallback
            else:
                out[key] = val
            break
    return {k: v for k, v in out.items() if v is not None}


def sanitize_settings(partial: Any, base: JsonDict) -> tuple[JsonDict, list[str]]:
    """Merge ``partial`` onto ``base`` keeping only whitelisted values (docs/ARCHITECTURE.md).

    Invalid values are skipped (the previous value is kept) and reported in the returned warnings list.
    Integers are clamped to their sane range. Unknown keys are ignored silently.
    """
    out = copy.deepcopy(base)
    warnings: list[str] = []
    if partial is None:
        return out, warnings
    if not isinstance(partial, dict):
        return out, ["settings must be a JSON object"]

    def choice(key: str, allowed: tuple[str, ...]) -> None:
        if key in partial:
            val = partial[key]
            if isinstance(val, str) and val in allowed:
                out[key] = val
            else:
                warnings.append(f"{key}: {val!r} is not one of {list(allowed)}")

    def integer(key: str, lo: int, hi: int) -> None:
        if key in partial:
            val = partial[key]
            if isinstance(val, bool) or not isinstance(val, (int, float)):
                warnings.append(f"{key}: {val!r} is not a number")
            else:
                out[key] = int(min(hi, max(lo, val)))

    choice("profile", PROFILES)
    choice("transport", TRANSPORTS)
    choice("kill_combo", KILL_COMBOS)
    integer("kill_hold_ms", *KILL_HOLD_MS_RANGE)
    integer("touch_wake_seconds", *TOUCH_WAKE_RANGE)
    if "screen_off" in partial:
        if isinstance(partial["screen_off"], bool):
            out["screen_off"] = partial["screen_off"]
        else:
            warnings.append("screen_off: must be a boolean")
    if "paddles" in partial:
        paddles = partial["paddles"]
        if isinstance(paddles, dict):
            for name, action in paddles.items():
                if name in PADDLES and isinstance(action, str) and action in PADDLE_ACTIONS:
                    out["paddles"][name] = action
                else:
                    warnings.append(f"paddles.{name}: {action!r} is not a valid paddle action")
        else:
            warnings.append("paddles: must be an object")
    return out, warnings


def resolve_transport(profile: str, transport: str) -> str:
    """``auto`` → raw for xbox360 (vendor descriptors need raw-gadget), hid for hid_gamepad (configfs f_hid)."""
    if transport != "auto":
        return transport
    return "raw" if profile == "xbox360" else "hid"


class SettingsStore:
    """Minimal JSON settings persistence in DECKY_PLUGIN_SETTINGS_DIR/settings.json (no external modules)."""

    def __init__(self, path: str) -> None:
        self.path = path
        self._cached: Optional[JsonDict] = None

    def load(self) -> JsonDict:
        if self._cached is None:
            data: Any = None
            try:
                with open(self.path, encoding="utf-8") as f:
                    data = json.load(f)
            except FileNotFoundError:
                pass
            except Exception as e:  # corrupt file → defaults (and the next save rewrites it)
                decky.logger.warning("settings.json unreadable (%s) — using defaults", e)
            settings, warnings = sanitize_settings(data if isinstance(data, dict) else {},
                                                   copy.deepcopy(DEFAULT_SETTINGS))
            for w in warnings:
                decky.logger.warning("settings.json: %s (ignored)", w)
            self._cached = settings
        return copy.deepcopy(self._cached)

    def save(self, settings: JsonDict) -> None:
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        tmp = f"{self.path}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(settings, f, indent=2, sort_keys=True)
            f.write("\n")
        os.replace(tmp, self.path)  # atomic on the same filesystem
        self._cached = copy.deepcopy(settings)

    def update(self, partial: Any) -> tuple[JsonDict, list[str]]:
        merged, warnings = sanitize_settings(partial, self.load())
        self.save(merged)
        return copy.deepcopy(merged), warnings


# ---------------------------------------------------------------------------------------------------------
# Backend: daemon supervisor + status + settings (one instance per plugin lifetime)
# ---------------------------------------------------------------------------------------------------------
class _Backend:
    def __init__(self) -> None:
        self.plugin_dir: str = decky.DECKY_PLUGIN_DIR
        self.py_modules_dir = os.path.join(self.plugin_dir, "py_modules")
        self.settings_dir: str = decky.DECKY_PLUGIN_SETTINGS_DIR
        self.runtime_dir: str = decky.DECKY_PLUGIN_RUNTIME_DIR
        self.log_dir: str = decky.DECKY_PLUGIN_LOG_DIR
        self.daemon_log_path = os.path.join(self.log_dir, "deckgadget.log")
        self.pidfile = os.path.join(self.runtime_dir, "deckgadget.pid")
        self.version = _plugin_version()
        self.settings = SettingsStore(os.path.join(self.settings_dir, "settings.json"))

        # daemon process
        self.proc: Optional[asyncio.subprocess.Process] = None
        self.proc_task: Optional[asyncio.Task[None]] = None
        self.proc_args: list[str] = []
        self.proc_started_at: Optional[float] = None
        self.proc_exit_code: Optional[int] = None
        self.stop_requested = False
        self.first_event = asyncio.Event()  # set by the first daemon event or by its exit

        # session view mirrored from daemon events
        self.session_state = "IDLE"
        self.session_detail = ""
        self.active_profile: Optional[str] = None
        self.transport: Optional[str] = None
        self.metrics: JsonDict = dict(DEFAULT_METRICS)
        self.screen_off: Optional[bool] = None   # last {"ev":"screen","off":…} of this session (None = none yet)
        self.last_error: Optional[str] = None
        self.last_kill: Optional[str] = None
        self.output: collections.deque[str] = collections.deque(maxlen=OUTPUT_RING_SIZE)

        # serialization + caches
        self.op_lock = asyncio.Lock()      # start / stop / recover never overlap
        self.cli_lock = asyncio.Lock()     # one ``deckgadget status`` at a time
        self.cli_cache_ts = 0.0
        self.cli_cache: Optional[JsonDict] = None
        self.cli_error: Optional[str] = None
        self.last_recover: Optional[JsonDict] = None
        self.status_task: Optional[asyncio.Task[None]] = None

    # ---- lifecycle ------------------------------------------------------------------------------------
    async def startup(self) -> None:
        for d in (self.settings_dir, self.runtime_dir, self.log_dir):
            try:
                os.makedirs(d, exist_ok=True)
            except OSError as e:
                decky.logger.warning("cannot create %s: %s", d, e)
        # Hold op_lock for the whole load-time rollback: a start() arriving while the (up to 30 s)
        # recover is still running must wait, otherwise recover would rebind usbhid / remove the gadget
        # underneath the freshly spawned daemon.
        async with self.op_lock:
            await self._kill_stale_daemon()
            await self._recover("plugin-load")   # a previous backend instance may have died mid-session
        if self.status_task is None or self.status_task.done():
            self.status_task = asyncio.create_task(self._status_loop(), name="decky-controller-status")

    async def shutdown(self, reason: str) -> None:
        if self.status_task is not None:
            self.status_task.cancel()
            try:
                await self.status_task
            except (asyncio.CancelledError, Exception):
                pass
            self.status_task = None
        await self.stop(reason)

    # ---- daemon control -------------------------------------------------------------------------------
    def daemon_alive(self) -> bool:
        return self.proc is not None and self.proc.returncode is None

    def _daemon_args(self, settings: JsonDict, profile: str) -> list[str]:
        """CLI arguments for ``deckgadget run`` (docs/ARCHITECTURE.md)."""
        args = [
            "--profile", profile,
            "--transport", str(settings["transport"]),
            "--kill-combo", str(settings["kill_combo"]),
            "--kill-hold-ms", str(int(settings["kill_hold_ms"])),
        ]
        if settings["screen_off"]:
            args.append("--screen-off")
        args += ["--touch-wake-seconds", str(int(settings["touch_wake_seconds"]))]
        args += ["--paddles", ",".join(f"{p}={settings['paddles'].get(p, 'none')}" for p in PADDLES)]
        args += ["--log-file", self.daemon_log_path]
        return args

    def _reset_session(self) -> None:
        """Back to IDLE (keeps last_error / last_kill so the UI can show why the session ended)."""
        self.session_state = "IDLE"
        self.session_detail = ""
        self.active_profile = None
        self.transport = None
        self.metrics = dict(DEFAULT_METRICS)
        self.screen_off = None

    async def start(self, profile: Optional[str]) -> JsonDict:
        async with self.op_lock:
            settings = self.settings.load()
            if not profile:
                profile = str(settings["profile"])
            if profile not in PROFILES:
                raise ValueError(f"unknown profile {profile!r} (expected one of {list(PROFILES)})")
            if self.daemon_alive():
                assert self.proc is not None
                decky.logger.info("start(%s): daemon already running (pid %s) — nothing to do", profile, self.proc.pid)
                return await self.build_status()
            if not os.path.isdir(self.py_modules_dir):
                raise FileNotFoundError(f"daemon package directory missing: {self.py_modules_dir}")
            for d in (self.runtime_dir, self.log_dir):
                os.makedirs(d, exist_ok=True)

            args = self._daemon_args(settings, profile)
            self._reset_session()
            self.active_profile = profile
            self.transport = resolve_transport(profile, str(settings["transport"]))
            self.last_error = None
            self.last_kill = None
            self.proc_exit_code = None
            self.stop_requested = False
            self.first_event = asyncio.Event()
            self.output.clear()

            cmd = [PYTHON_BIN, "-m", DAEMON_MODULE, "run", *args]
            decky.logger.info("starting daemon: %s", " ".join(cmd))
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=self.py_modules_dir,
                env=_daemon_env(),
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                limit=SUBPROCESS_LINE_LIMIT,
                start_new_session=True,   # own process group: its lifetime is managed here, not by signals
            )
            self.proc = proc
            self.proc_args = args
            self.proc_started_at = time.time()
            self._write_pidfile(proc.pid)
            self.proc_task = asyncio.create_task(self._supervise(proc), name="deckgadget-supervisor")

        # Outside the lock (a fast-failing daemon needs the lock for its rollback): give the daemon a moment
        # to report its first state so the caller gets something meaningful back.
        try:
            await asyncio.wait_for(self.first_event.wait(), START_FIRST_EVENT_TIMEOUT_S)
        except asyncio.TimeoutError:
            pass
        status = await self.build_status(force=True)
        await self._emit("status", status)
        return status

    async def stop(self, reason: str = "user") -> JsonDict:
        """Idempotent full rollback: stop the daemon (if any), then always ``deckgadget recover``."""
        async with self.op_lock:
            self.stop_requested = True
            proc, task = self.proc, self.proc_task
            if proc is not None and proc.returncode is None:
                decky.logger.info("stop(%s): SIGTERM → daemon pid %s", reason, proc.pid)
                self.session_state = "STOPPING"
                try:
                    proc.terminate()
                except ProcessLookupError:
                    pass
                try:
                    await asyncio.wait_for(proc.wait(), STOP_TERM_GRACE_S)
                except asyncio.TimeoutError:
                    decky.logger.warning("daemon pid %s ignored SIGTERM for %.0fs — SIGKILL",
                                         proc.pid, STOP_TERM_GRACE_S)
                    try:
                        proc.kill()
                    except ProcessLookupError:
                        pass
                    try:
                        await asyncio.wait_for(proc.wait(), STOP_KILL_GRACE_S)
                    except asyncio.TimeoutError:
                        decky.logger.error("daemon pid %s did not die after SIGKILL", proc.pid)
            if task is not None and not task.done():
                # let the supervisor drain the remaining output; never cancel it (it must not be left half-way)
                try:
                    await asyncio.wait_for(asyncio.shield(task), 2.0)
                except (asyncio.TimeoutError, Exception):
                    pass
            self._reset_session()
            await self._recover(f"stop:{reason}")
        status = await self.build_status(force=True)
        await self._emit("status", status)
        return status

    async def _supervise(self, proc: asyncio.subprocess.Process) -> None:
        """Pump stdout/stderr until the daemon exits, then roll back unless stop() is doing it."""
        try:
            await asyncio.gather(self._pump_stdout(proc), self._pump_stderr(proc))
        except Exception:
            decky.logger.exception("daemon output pump failed")
        rc = await proc.wait()
        self.proc_exit_code = rc
        self._remove_pidfile()
        if self.proc is proc:
            self.proc = None
        requested = self.stop_requested
        decky.logger.info("daemon pid %s exited with code %s (%s)", proc.pid, rc,
                          "requested" if requested else "unexpected")
        self.first_event.set()
        if requested:
            return  # stop() owns the rollback and the status refresh
        self._reset_session()
        if rc != 0:
            body = self.last_error or f"daemon exited with code {rc} — see the Decky log"
            await self._toast("Controller mode failed", body, "error")
        async with self.op_lock:
            if not self.daemon_alive():   # a newer session may already be running — never roll that one back
                await self._recover(f"daemon-exit rc={rc}")
        await self.emit_status(force=True)

    async def _pump_stdout(self, proc: asyncio.subprocess.Process) -> None:
        assert proc.stdout is not None
        while True:
            try:
                raw = await proc.stdout.readline()
            except ValueError:   # line longer than SUBPROCESS_LINE_LIMIT — asyncio drops it, keep going
                decky.logger.warning("daemon stdout: over-long line skipped")
                continue
            if not raw:
                break
            line = raw.decode("utf-8", "replace").rstrip("\r\n")
            if not line.strip():
                continue
            self.output.append(f"out: {line}")
            try:
                event = json.loads(line)
            except ValueError:
                decky.logger.info("[deckgadget] %s", line)   # plain text on stdout → just log it
                continue
            if isinstance(event, dict):
                try:
                    await self._on_daemon_event(event)
                except Exception:
                    decky.logger.exception("error handling daemon event %r", event)

    async def _pump_stderr(self, proc: asyncio.subprocess.Process) -> None:
        assert proc.stderr is not None
        while True:
            try:
                raw = await proc.stderr.readline()
            except ValueError:
                continue
            if not raw:
                break
            line = raw.decode("utf-8", "replace").rstrip("\r\n")
            if line.strip():
                self.output.append(f"err: {line}")
                decky.logger.info("[deckgadget] %s", line)

    async def _on_daemon_event(self, event: JsonDict) -> None:
        """JSON-lines protocol of ``deckgadget run`` (docs/ARCHITECTURE.md)."""
        kind = event.get("ev")
        if kind == "state":
            state = str(event.get("state") or "")
            self.session_detail = str(event.get("detail") or "")
            if state == "STOPPED":
                # the process is about to exit; Status shows IDLE as soon as it is gone (see build_status)
                state = "STOPPING"
            if state in SESSION_STATES:
                self.session_state = state
            else:
                decky.logger.warning("daemon reported unknown state %r", state)
            self.first_event.set()
            await self.emit_status()
        elif kind == "error":
            self.last_error = str(event.get("msg") or "unknown daemon error")
            decky.logger.error("[deckgadget] error: %s", self.last_error)
            self.first_event.set()
            await self.emit_status()
        elif kind == "metrics":
            for key in ("hz", "reports", "dropped"):
                val = event.get(key)
                if isinstance(val, (int, float)) and not isinstance(val, bool):
                    self.metrics[key] = val
            # the periodic status loop (every 2 s) carries metrics to the UI — no emit here
        elif kind == "kill":
            reason = str(event.get("reason") or "unknown")
            self.last_kill = reason
            decky.logger.info("[deckgadget] kill reason=%s", reason)
            if reason == "combo":
                await self._toast("Controller mode stopped", "Exit combo held — the Deck is a Deck again.", "info")
            elif reason == "unplug":
                await self._toast("Controller mode stopped", "USB cable disconnected.", "info")
            elif reason == "signal" and not self.stop_requested:
                await self._toast("Controller mode stopped", "Daemon was signalled to exit.", "info")
            # reason == "error": the exit handler reports it together with the error text
        elif kind == "screen":
            # daemon extension: {"ev":"screen","off":bool} — authoritative Status.screen_off for this session
            self.screen_off = bool(event.get("off"))
            await self.emit_status()
        else:
            decky.logger.debug("[deckgadget] unhandled event %r", event)

    # ---- rollback / CLI -------------------------------------------------------------------------------
    async def _run_cli(self, *args: str, timeout: float) -> tuple[Optional[int], str, str]:
        """Run ``python3 -m deckgadget <args>`` → (returncode, stdout, stderr); raises TimeoutError."""
        proc = await asyncio.create_subprocess_exec(
            PYTHON_BIN, "-m", DAEMON_MODULE, *args,
            cwd=self.py_modules_dir,
            env=_daemon_env(),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            limit=SUBPROCESS_LINE_LIMIT,
        )
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout)
        except asyncio.TimeoutError:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            await proc.wait()
            raise TimeoutError(f"deckgadget {args[0]} timed out after {timeout:g}s") from None
        return proc.returncode, out.decode("utf-8", "replace"), err.decode("utf-8", "replace")

    async def _recover(self, reason: str) -> bool:
        """``deckgadget recover`` — idempotent full rollback (gadget down, Neptune back to usbhid, backlight).

        The CLI always exits 0 (docs/ARCHITECTURE.md); success is judged from its JSON report (``ok`` /
        ``errors``), so a Deck left without its controller is surfaced as a toast + ``last_error``.
        """
        decky.logger.info("recover (%s)", reason)
        rc: Optional[int]
        try:
            rc, out, err = await self._run_cli("recover", timeout=RECOVER_TIMEOUT_S)
        except Exception as e:
            rc, out, err = None, "", f"{type(e).__name__}: {e}"
        report = _parse_json_object(out)
        report_errors = [str(x) for x in report.get("errors") or []] if report is not None else []
        report_ok = bool(report.get("ok")) and not report_errors if report is not None else False
        self.last_recover = {"ts": time.time(), "reason": reason, "rc": rc, "ok": report_ok,
                             "errors": report_errors, "stdout": out[-2000:], "stderr": err[-2000:]}
        if rc == 0 and report_ok:
            decky.logger.info("recover ok")
            return True
        if report_errors:
            detail = "; ".join(report_errors)
        elif rc != 0:
            detail = f"'deckgadget recover' exited with {rc}: {(err or out).strip()[-300:]}"
        else:
            detail = "'deckgadget recover' printed no JSON report"
        decky.logger.error("recover failed (rc=%s ok=%s): %s", rc, report_ok, detail[:500])
        self.last_error = f"Controller recovery failed: {detail}"[:500]
        await self._toast("Controller recovery failed",
                          f"{detail[:200]}. Press Stop again or reboot the Deck — a reboot always restores "
                          "everything.", "error")
        return False

    async def _cli_status(self, force: bool = False) -> tuple[Optional[JsonDict], Optional[str]]:
        """Cached ``deckgadget status`` JSON (spawned at most once per STATUS_CACHE_TTL_S unless forced)."""
        async with self.cli_lock:
            now = time.monotonic()
            if not force and self.cli_cache_ts and now - self.cli_cache_ts < STATUS_CACHE_TTL_S:
                return self.cli_cache, self.cli_error
            data: Optional[JsonDict] = None
            err: Optional[str] = None
            try:
                rc, out, stderr = await self._run_cli("status", timeout=STATUS_CLI_TIMEOUT_S)
                data = _parse_json_object(out)
                if data is None:
                    tail = stderr.strip().splitlines()[-1] if stderr.strip() else ""
                    err = f"deckgadget status (rc={rc}) printed no JSON object" + (f": {tail}" if tail else "")
            except Exception as e:
                err = f"deckgadget status failed: {type(e).__name__}: {e}"
            if err and err != self.cli_error:
                decky.logger.warning("%s — using sysfs fallback", err)
            self.cli_cache_ts = time.monotonic()
            self.cli_cache, self.cli_error = data, err
            return data, err

    # ---- status ---------------------------------------------------------------------------------------
    async def build_status(self, force: bool = False) -> JsonDict:
        """Status dict per docs/ARCHITECTURE.md: hardware facts from the CLI (sysfs fallback) + daemon session."""
        cli, cli_err = await self._cli_status(force)
        snap = _sysfs_snapshot()
        settings = self.settings.load()
        running = self.daemon_alive()
        state = self.session_state if running else "IDLE"

        status: JsonDict = {
            "ok": True,
            "plugin_version": self.version,
            "kernel": snap["kernel"],
            "model": snap["model"],
            "drd_enabled": snap["drd_enabled"],
            "udc_name": snap["udc_name"],
            "udc_state": snap["udc_state"],
            "udc_speed": snap["udc_speed"],
            "extcon": dict(snap["extcon"]),
            "host_connected": snap["host_connected"],
            "neptune_present": snap["neptune_present"],
            "neptune_captured": False,
            # what the port physically sees while idle (the CLI's values replace these when available)
            "cable_power": snap["cable_power"],
            "pd_contract_mv": snap["pd_contract_mv"],
            "pd_contract_ma": snap["pd_contract_ma"],
            "cable_kind": snap["cable_kind"],
        }
        if cli:
            status.update(_normalize_cli_status(cli))
        status.update({
            "neptune_captured": bool(status.get("neptune_captured")) or (running and state in CAPTURED_STATES),
            "daemon_running": running,
            "daemon_pid": self.proc.pid if running and self.proc is not None else None,
            "session_state": state,
            "session_detail": self.session_detail if running else "",
            "active_profile": self.active_profile if running else None,
            "transport": self.transport if running else None,
            # the daemon's own "screen" event wins; before it arrives, infer from the settings + state
            "screen_off": (self.screen_off if self.screen_off is not None
                           else bool(settings["screen_off"]) and state in CAPTURED_STATES) if running else False,
            "last_error": self.last_error,
            "metrics": dict(self.metrics),
            "status_error": cli_err,   # non-null ⇒ hardware fields came from the sysfs fallback
        })
        return status

    async def emit_status(self, force: bool = False) -> None:
        try:
            await self._emit("status", await self.build_status(force))
        except Exception:
            decky.logger.exception("emit status failed")

    async def _status_loop(self) -> None:
        """Every 2 s while the daemon runs; while idle, poll sysfs every 5 s and emit only on a change."""
        last_sig: Optional[tuple] = None
        while True:
            try:
                if self.daemon_alive():
                    await self.emit_status()
                    await asyncio.sleep(STATUS_PERIOD_RUNNING_S)
                else:
                    sig = _connectivity_signature(_sysfs_snapshot())
                    if sig != last_sig:
                        last_sig = sig
                        await self.emit_status(force=True)
                    await asyncio.sleep(STATUS_PERIOD_IDLE_S)
            except asyncio.CancelledError:
                raise
            except Exception:
                decky.logger.exception("status loop iteration failed")
                await asyncio.sleep(STATUS_PERIOD_IDLE_S)

    async def diagnostics(self) -> JsonDict:
        status = await self.build_status(force=True)
        return {
            "ok": True,
            "plugin_version": self.version,
            "decky_version": getattr(decky, "DECKY_VERSION", None),
            "python": sys.version,
            "python_bin": PYTHON_BIN,
            "kernel": status.get("kernel"),
            "model": status.get("model"),
            "status": status,
            "cli_status_raw": self.cli_cache,
            "cli_status_error": self.cli_error,
            "settings": self.settings.load(),
            "daemon": {
                "running": self.daemon_alive(),
                "pid": self.proc.pid if self.daemon_alive() and self.proc is not None else None,
                "args": list(self.proc_args),
                "started_at": self.proc_started_at,
                "exit_code": self.proc_exit_code,
                "stop_requested": self.stop_requested,
                "last_kill": self.last_kill,
            },
            "last_recover": self.last_recover,
            "daemon_log_tail": _tail_file(self.daemon_log_path, LOG_TAIL_LINES),
            "daemon_output_tail": list(self.output)[-LOG_TAIL_LINES:],
            "paths": {
                "plugin_dir": self.plugin_dir,
                "py_modules_dir": self.py_modules_dir,
                "settings": self.settings.path,
                "runtime_dir": self.runtime_dir,
                "log_dir": self.log_dir,
                "daemon_log": self.daemon_log_path,
            },
        }

    # ---- events ---------------------------------------------------------------------------------------
    async def _emit(self, event: str, payload: JsonDict) -> None:
        try:
            await decky.emit(event, payload)
        except Exception:
            decky.logger.exception("emit %s failed", event)

    async def _toast(self, title: str, body: str, severity: str = "info") -> None:
        decky.logger.log({"error": 40, "warn": 30}.get(severity, 20), "toast: %s — %s", title, body)
        await self._emit("toast", {"title": title, "body": body, "severity": severity})

    # ---- pidfile / stale daemon -----------------------------------------------------------------------
    def _write_pidfile(self, pid: int) -> None:
        try:
            with open(self.pidfile, "w", encoding="utf-8") as f:
                f.write(f"{pid}\n")
        except OSError as e:
            decky.logger.warning("cannot write pidfile %s: %s", self.pidfile, e)

    def _remove_pidfile(self) -> None:
        try:
            os.unlink(self.pidfile)
        except OSError:
            pass

    async def _kill_stale_daemon(self) -> None:
        """A daemon left behind by a previous backend instance (Decky restart/crash) must go before recover."""
        txt = _read_text(self.pidfile)
        if not txt:
            return
        try:
            pid = int(txt)
        except ValueError:
            self._remove_pidfile()
            return
        if pid <= 1 or not _is_deckgadget_pid(pid):
            self._remove_pidfile()
            return
        decky.logger.warning("stale deckgadget daemon (pid %d) from a previous session — terminating", pid)
        for sig, grace in ((signal.SIGTERM, STOP_TERM_GRACE_S), (signal.SIGKILL, STOP_KILL_GRACE_S)):
            try:
                os.kill(pid, sig)
            except ProcessLookupError:
                break
            deadline = time.monotonic() + grace
            while time.monotonic() < deadline and _is_deckgadget_pid(pid):
                await asyncio.sleep(0.1)
            if not _is_deckgadget_pid(pid):
                break
        self._remove_pidfile()


_BACKEND: Optional[_Backend] = None


def _backend() -> _Backend:
    global _BACKEND
    if _BACKEND is None:
        _BACKEND = _Backend()
    return _BACKEND


def _error(e: BaseException, **extra: Any) -> JsonDict:
    """Uniform error answer for callables; expected (validation / missing file) errors log without a traceback."""
    if isinstance(e, (ValueError, FileNotFoundError, TimeoutError)):
        decky.logger.error("callable failed: %s: %s", type(e).__name__, e)
    else:
        decky.logger.exception("callable failed: %s", e)
    return {"ok": False, "error": str(e) or type(e).__name__, **extra}


# ---------------------------------------------------------------------------------------------------------
# Decky entry point
# ---------------------------------------------------------------------------------------------------------
class Plugin:
    """Decky Loader entry point (docs/ARCHITECTURE.md).

    Decky may call these either on an instance or with the class itself as ``self`` (older loaders do the
    latter), so no state is kept on ``self`` — everything lives in the lazily created module-level backend.
    Every callable catches all exceptions and answers ``{"ok": false, "error": "..."}``.
    """

    async def get_status(self) -> JsonDict:
        try:
            return await _backend().build_status()
        except Exception as e:
            return _error(e)

    async def start(self, profile: Optional[str] = None) -> JsonDict:
        try:
            if profile is not None and not isinstance(profile, str):
                raise ValueError(f"profile must be a string, got {type(profile).__name__}")
            return await _backend().start(profile)
        except Exception as e:
            return _error(e)

    async def stop(self) -> JsonDict:
        try:
            return await _backend().stop("user")
        except Exception as e:
            return _error(e)

    async def get_settings(self) -> JsonDict:
        try:
            return {"ok": True, **_backend().settings.load()}
        except Exception as e:
            return _error(e)

    async def set_settings(self, settings: Optional[JsonDict] = None) -> JsonDict:
        try:
            merged, warnings = _backend().settings.update(settings if settings is not None else {})
            for w in warnings:
                decky.logger.warning("set_settings: %s (ignored)", w)
            result: JsonDict = {"ok": True, **merged}
            if warnings:
                result["warnings"] = warnings
            return result
        except Exception as e:
            return _error(e)

    async def get_diagnostics(self) -> JsonDict:
        try:
            return await _backend().diagnostics()
        except Exception as e:
            return _error(e)

    # ---- Decky lifecycle ------------------------------------------------------------------------------
    async def _main(self) -> None:
        b = _backend()
        decky.logger.info("%s %s backend starting (python %s, plugin dir %s)",
                          PLUGIN_NAME, b.version, sys.version.split()[0], b.plugin_dir)
        try:
            await b.startup()
        except Exception:
            decky.logger.exception("backend startup failed")

    async def _unload(self) -> None:
        decky.logger.info("%s: unloading — stopping daemon and rolling back", PLUGIN_NAME)
        try:
            await _backend().shutdown("unload")
        except Exception:
            decky.logger.exception("unload failed")

    async def _uninstall(self) -> None:
        decky.logger.info("%s: uninstalling — stopping daemon and rolling back", PLUGIN_NAME)
        try:
            await _backend().shutdown("uninstall")
        except Exception:
            decky.logger.exception("uninstall cleanup failed")

    async def _migration(self) -> None:
        # Nothing to migrate: settings have always lived in DECKY_PLUGIN_SETTINGS_DIR/settings.json.
        decky.logger.info("%s: no migration needed", PLUGIN_NAME)
