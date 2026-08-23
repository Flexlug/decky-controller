"""Decky Loader backend (root, stdlib only): frontend callables, daemon supervisor, settings store.

``deckgadget`` is never imported here — it only runs as a subprocess (``cwd=<plugin>/py_modules``), so a
broken core package cannot take the Decky backend down with it.
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

try:
    import decky  # type: ignore[import-not-found]
except ImportError:  # pragma: no cover — shim so ``import main`` works on a dev machine without Decky Loader
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

PLUGIN_NAME = "Decky Controller"
PYTHON_BIN = "/usr/bin/python3"          # the system interpreter, not Decky's bundled one
DAEMON_MODULE = "deckgadget"

STATUS_CLI_TIMEOUT_S = 5.0
STATUS_CACHE_TTL_S = 1.0
RECOVER_TIMEOUT_S = 30.0
STOP_TERM_GRACE_S = 3.0                  # SIGTERM → wait this long → SIGKILL → wait STOP_KILL_GRACE_S
STOP_KILL_GRACE_S = 2.0
START_FIRST_EVENT_TIMEOUT_S = 2.0
STATUS_PERIOD_RUNNING_S = 2.0
STATUS_PERIOD_IDLE_S = 5.0
OUTPUT_RING_SIZE = 200
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
    "transport": "auto",
    "kill_combo": "L4+R4",
    "kill_hold_ms": 1500,
    "screen_off": True,
    "touch_wake_seconds": 5,
    "paddles": {"L4": "none", "L5": "none", "R4": "none", "R5": "none"},
}

SESSION_STATES = ("IDLE", "CAPTURING", "GADGET_UP", "WAITING_HOST", "ACTIVE", "STOPPING")
CAPTURED_STATES = frozenset({"CAPTURING", "GADGET_UP", "WAITING_HOST", "ACTIVE"})
DEFAULT_METRICS: JsonDict = {"hz": 0, "reports": 0, "dropped": 0}

NEPTUNE_VID, NEPTUNE_PID = "28de", "1205"   # built-in Steam Deck controller


def _read_text(path: str) -> Optional[str]:
    """Stripped file content; None if missing/unreadable."""
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read().strip()
    except OSError:
        return None


def _tail_file(path: str, count: int, max_bytes: int = 64 * 1024) -> list[str]:
    """Last ``count`` lines, reading at most ``max_bytes`` from the end."""
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - max_bytes))
            data = f.read()
    except OSError:
        return []
    text_lines = data.decode("utf-8", "replace").splitlines()
    if size > max_bytes and text_lines:
        text_lines = text_lines[1:]  # first line is almost certainly partial
    return text_lines[-count:]


def _parse_json_object(text: str) -> Optional[JsonDict]:
    """Parse the JSON object printed by a CLI command; tolerates stray log lines before the JSON."""
    text = text.strip()
    if not text:
        return None
    parsed: Any = None
    try:
        parsed = json.loads(text)
    except ValueError:
        for line in reversed(text.splitlines()):
            line = line.strip()
            if line.startswith("{"):
                try:
                    parsed = json.loads(line)
                    break
                except ValueError:
                    continue
    return parsed if isinstance(parsed, dict) else None


def _plugin_version() -> str:
    version = getattr(decky, "DECKY_PLUGIN_VERSION", None)
    if isinstance(version, str) and version:
        return version
    try:
        with open(os.path.join(decky.DECKY_PLUGIN_DIR, "package.json"), encoding="utf-8") as f:
            version = json.load(f).get("version")
        if isinstance(version, str) and version:
            return version
    except Exception:
        pass
    return "0.0.0"


def _daemon_env() -> dict[str, str]:
    """Decky Loader (a PyInstaller bundle) exports LD_LIBRARY_PATH into itself, which breaks the system
    python3 (decky-loader issue #756) — drop it; PYTHONUNBUFFERED keeps the daemon's JSON lines unbuffered."""
    environment = {key: value for key, value in os.environ.items() if key != "LD_LIBRARY_PATH"}
    environment["PYTHONUNBUFFERED"] = "1"
    return environment


def _is_deckgadget_pid(pid: int) -> bool:
    """True if ``pid`` is alive and its command line mentions deckgadget (guards against PID reuse)."""
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            return b"deckgadget" in f.read()
    except OSError:
        return False


def _sysfs_snapshot() -> JsonDict:
    """Read-only USB-role / controller view straight from sysfs: cheap connectivity polling between
    ``deckgadget status`` calls, and the fallback when that CLI is unavailable (the CLI wins otherwise)."""
    snapshot: JsonDict = {
        "kernel": os.uname().release,
        "model": _read_text("/sys/class/dmi/id/product_name"),
        "drd_enabled": False,
        "udc_name": None,
        "udc_state": None,
        "udc_speed": None,
        "extcon": {"USB": 0, "USB-HOST": 0},
        "host_connected": False,
        "neptune_present": False,
        # what the port physically sees; udc_state only means something once a gadget is bound
        "cable_power": None,
        "pd_contract_mv": None,
        "pd_contract_ma": None,
        "cable_kind": "unknown",
    }
    # DRD on in BIOS ⇒ PCI 04:00.3 is re-classed by the kernel and claimed by dwc3-pci; off ⇒ xhci_hcd owns it
    # and the dwc3_pci module is not even loaded, so the driver directory does not exist.
    try:
        snapshot["drd_enabled"] = any(":" in entry for entry in os.listdir("/sys/bus/pci/drivers/dwc3-pci"))
    except OSError:
        snapshot["drd_enabled"] = False
    # A UDC exists only while dwc3 is in device role (Deck plugged into a host, nothing in host role).
    try:
        udcs = sorted(os.listdir("/sys/class/udc"))
    except OSError:
        udcs = []
    if udcs:
        snapshot["udc_name"] = udcs[0]
        snapshot["udc_state"] = _read_text(f"/sys/class/udc/{udcs[0]}/state")
        snapshot["udc_speed"] = _read_text(f"/sys/class/udc/{udcs[0]}/current_speed")
        snapshot["host_connected"] = snapshot["udc_state"] == "configured"
    # steamdeck-extcon: "USB=0\nUSB-HOST=1\nSDP=0…" — USB=1 ⇒ we are a device, USB-HOST=1 ⇒ we are a host.
    try:
        extcons = sorted(os.listdir("/sys/class/extcon"))
    except OSError:
        extcons = []
    for name in extcons:
        text = _read_text(f"/sys/class/extcon/{name}/state")
        if not text:
            continue
        for line in text.splitlines():
            key, separator, value = line.partition("=")
            if separator and key.strip() in ("USB", "USB-HOST"):
                try:
                    snapshot["extcon"][key.strip()] = int(value.strip())
                except ValueError:
                    pass
        break
    # Port power (EC "ACAD" supply) + negotiated USB-PD contract (steamdeck_hwmon, found by name: in0 = mV,
    # curr1 = mA). PC port = 5 V, PD charger = 15-20 V — same rule as usb_role.classify_cable.
    online = _read_text("/sys/class/power_supply/ACAD/online")
    if online in ("0", "1"):
        snapshot["cable_power"] = online == "1"
    try:
        hwmons = sorted(os.listdir("/sys/class/hwmon"))
    except OSError:
        hwmons = []
    for name in hwmons:
        base = f"/sys/class/hwmon/{name}"
        if _read_text(f"{base}/name") != "steamdeck_hwmon":
            continue
        for key, file_name in (("pd_contract_mv", "in0_input"), ("pd_contract_ma", "curr1_input")):
            try:
                snapshot[key] = int(_read_text(f"{base}/{file_name}") or "")
            except ValueError:
                snapshot[key] = None
        break
    if snapshot["extcon"].get("USB-HOST") == 1:
        snapshot["cable_kind"] = "host_device"
    elif snapshot["cable_power"] is False:
        snapshot["cable_kind"] = "none"
    elif isinstance(snapshot["pd_contract_mv"], int) and snapshot["pd_contract_mv"] > 0:
        snapshot["cable_kind"] = "pc" if snapshot["pd_contract_mv"] <= 5500 else "charger"
    # Built-in controller present on the USB bus (it lives on a different xHCI than the USB-C port).
    try:
        for device_name in os.listdir("/sys/bus/usb/devices"):
            # skip interfaces ("1-3:1.0") and root hubs ("usb1")
            if ":" in device_name or not device_name[:1].isdigit():
                continue
            base = f"/sys/bus/usb/devices/{device_name}"
            if _read_text(f"{base}/idVendor") == NEPTUNE_VID and _read_text(f"{base}/idProduct") == NEPTUNE_PID:
                snapshot["neptune_present"] = True
                break
    except OSError:
        pass
    return snapshot


def _connectivity_signature(snapshot: JsonDict) -> tuple:
    """The part of the sysfs snapshot whose change should trigger a ``status`` event while idle."""
    extcon = snapshot.get("extcon") or {}
    return (snapshot.get("drd_enabled"), snapshot.get("udc_name"), snapshot.get("udc_state"),
            snapshot.get("host_connected"), snapshot.get("neptune_present"), extcon.get("USB"), extcon.get("USB-HOST"),
            snapshot.get("cable_kind"), snapshot.get("cable_power"), snapshot.get("pd_contract_mv"))


# Status keys ← ``deckgadget status`` keys; shorter spellings are tolerated so a slightly different core
# build still renders a useful status.
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
    "cable_power": ("cable_power",),
    "pd_contract_mv": ("pd_contract_mv",),
    "pd_contract_ma": ("pd_contract_ma",),
    "cable_kind": ("cable_kind",),
}
_BOOL_STATUS_KEYS = frozenset({"drd_enabled", "host_connected", "neptune_present", "neptune_captured",
                               "cable_power"})


def _normalize_cli_status(raw: JsonDict) -> JsonDict:
    """Pick the Status fields out of the ``deckgadget status`` JSON (unknown keys are ignored)."""
    result: JsonDict = {}
    for key, aliases in _CLI_KEY_ALIASES.items():
        for alias in aliases:
            if alias not in raw or raw[alias] is None:
                continue
            value = raw[alias]
            if isinstance(value, dict):
                # nested spellings: {"udc": {"name", "state"}}, {"neptune": {"present", "captured"}}, {"drd": {"enabled"}}
                if key == "udc_name":
                    result["udc_name"] = value.get("name")
                    result["udc_state"] = value.get("state")
                elif key == "neptune_present":
                    result["neptune_present"] = bool(value.get("present"))
                    if "captured" in value:
                        result["neptune_captured"] = bool(value.get("captured"))
                elif key == "drd_enabled":
                    result["drd_enabled"] = bool(value.get("enabled"))
                elif key == "extcon":
                    result["extcon"] = {str(role): flag for role, flag in value.items()}
            elif key in _BOOL_STATUS_KEYS:
                result[key] = bool(value)
            elif key == "extcon":
                continue  # malformed — keep the sysfs fallback
            else:
                result[key] = value
            break
    return {key: value for key, value in result.items() if value is not None}


def sanitize_settings(partial: Any, base: JsonDict) -> tuple[JsonDict, list[str]]:
    """Merge ``partial`` onto ``base``: invalid values are skipped (previous kept) and reported in the
    returned warnings, integers are clamped to their range, unknown keys are ignored."""
    merged = copy.deepcopy(base)
    warnings: list[str] = []
    if partial is None:
        return merged, warnings
    if not isinstance(partial, dict):
        return merged, ["settings must be a JSON object"]

    def choice(key: str, allowed: tuple[str, ...]) -> None:
        if key in partial:
            value = partial[key]
            if isinstance(value, str) and value in allowed:
                merged[key] = value
            else:
                warnings.append(f"{key}: {value!r} is not one of {list(allowed)}")

    def integer(key: str, low: int, high: int) -> None:
        if key in partial:
            value = partial[key]
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                warnings.append(f"{key}: {value!r} is not a number")
            else:
                merged[key] = int(min(high, max(low, value)))

    choice("profile", PROFILES)
    choice("transport", TRANSPORTS)
    choice("kill_combo", KILL_COMBOS)
    integer("kill_hold_ms", *KILL_HOLD_MS_RANGE)
    integer("touch_wake_seconds", *TOUCH_WAKE_RANGE)
    if "screen_off" in partial:
        if isinstance(partial["screen_off"], bool):
            merged["screen_off"] = partial["screen_off"]
        else:
            warnings.append("screen_off: must be a boolean")
    if "paddles" in partial:
        paddles = partial["paddles"]
        if isinstance(paddles, dict):
            for name, action in paddles.items():
                if name in PADDLES and isinstance(action, str) and action in PADDLE_ACTIONS:
                    merged["paddles"][name] = action
                else:
                    warnings.append(f"paddles.{name}: {action!r} is not a valid paddle action")
        else:
            warnings.append("paddles: must be an object")
    return merged, warnings


def resolve_transport(profile: str, transport: str) -> str:
    """``auto`` → raw for xbox360 (vendor descriptors need raw-gadget), hid for hid_gamepad (configfs f_hid)."""
    if transport != "auto":
        return transport
    return "raw" if profile == "xbox360" else "hid"


class SettingsStore:
    """settings.json in DECKY_PLUGIN_SETTINGS_DIR, sanitized on load, written atomically."""

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
            for warning in warnings:
                decky.logger.warning("settings.json: %s (ignored)", warning)
            self._cached = settings
        return copy.deepcopy(self._cached)

    def save(self, settings: JsonDict) -> None:
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        temp_path = f"{self.path}.tmp"
        with open(temp_path, "w", encoding="utf-8") as f:
            json.dump(settings, f, indent=2, sort_keys=True)
            f.write("\n")
        os.replace(temp_path, self.path)  # atomic on the same filesystem
        self._cached = copy.deepcopy(settings)

    def update(self, partial: Any) -> tuple[JsonDict, list[str]]:
        merged, warnings = sanitize_settings(partial, self.load())
        self.save(merged)
        return copy.deepcopy(merged), warnings


class _Backend:
    """Daemon supervisor + status + settings; one instance per plugin lifetime."""

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

        self.process: Optional[asyncio.subprocess.Process] = None
        self.process_task: Optional[asyncio.Task[None]] = None
        self.process_args: list[str] = []
        self.process_started_at: Optional[float] = None
        self.process_exit_code: Optional[int] = None
        self.stop_requested = False
        self.first_event = asyncio.Event()  # set by the first daemon event or by its exit

        # mirrored from daemon events
        self.session_state = "IDLE"
        self.session_detail = ""
        self.active_profile: Optional[str] = None
        self.transport: Optional[str] = None
        self.metrics: JsonDict = dict(DEFAULT_METRICS)
        self.screen_off: Optional[bool] = None   # None until the daemon's first "screen" event
        self.last_error: Optional[str] = None
        self.last_kill: Optional[str] = None
        self.output: collections.deque[str] = collections.deque(maxlen=OUTPUT_RING_SIZE)

        self.operation_lock = asyncio.Lock()      # start / stop / recover never overlap
        self.cli_lock = asyncio.Lock()
        self.cli_cache_time = 0.0
        self.cli_cache: Optional[JsonDict] = None
        self.cli_error: Optional[str] = None
        self.last_recover: Optional[JsonDict] = None
        self.status_task: Optional[asyncio.Task[None]] = None

    async def startup(self) -> None:
        for directory in (self.settings_dir, self.runtime_dir, self.log_dir):
            try:
                os.makedirs(directory, exist_ok=True)
            except OSError as e:
                decky.logger.warning("cannot create %s: %s", directory, e)
        # Hold the lock for the whole load-time rollback: a start() arriving while the (up to 30 s) recover
        # still runs must wait, or recover would rebind usbhid / remove the gadget under the fresh daemon.
        async with self.operation_lock:
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

    def daemon_alive(self) -> bool:
        return self.process is not None and self.process.returncode is None

    def _daemon_args(self, settings: JsonDict, profile: str) -> list[str]:
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
        async with self.operation_lock:
            settings = self.settings.load()
            if not profile:
                profile = str(settings["profile"])
            if profile not in PROFILES:
                raise ValueError(f"unknown profile {profile!r} (expected one of {list(PROFILES)})")
            if self.daemon_alive():
                assert self.process is not None
                decky.logger.info("start(%s): daemon already running (pid %s) — nothing to do",
                                  profile, self.process.pid)
                return await self.build_status()
            if not os.path.isdir(self.py_modules_dir):
                raise FileNotFoundError(f"daemon package directory missing: {self.py_modules_dir}")
            for directory in (self.runtime_dir, self.log_dir):
                os.makedirs(directory, exist_ok=True)

            args = self._daemon_args(settings, profile)
            self._reset_session()
            self.active_profile = profile
            self.transport = resolve_transport(profile, str(settings["transport"]))
            self.last_error = None
            self.last_kill = None
            self.process_exit_code = None
            self.stop_requested = False
            self.first_event = asyncio.Event()
            self.output.clear()

            command = [PYTHON_BIN, "-m", DAEMON_MODULE, "run", *args]
            decky.logger.info("starting daemon: %s", " ".join(command))
            process = await asyncio.create_subprocess_exec(
                *command,
                cwd=self.py_modules_dir,
                env=_daemon_env(),
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                limit=SUBPROCESS_LINE_LIMIT,
                start_new_session=True,   # own process group: its lifetime is managed here, not by signals
            )
            self.process = process
            self.process_args = args
            self.process_started_at = time.time()
            self._write_pidfile(process.pid)
            self.process_task = asyncio.create_task(self._supervise(process), name="deckgadget-supervisor")

        # Outside the lock (a fast-failing daemon needs it for its rollback): wait briefly for the first event
        # so the caller gets something meaningful back.
        try:
            await asyncio.wait_for(self.first_event.wait(), START_FIRST_EVENT_TIMEOUT_S)
        except asyncio.TimeoutError:
            pass
        status = await self.build_status(force=True)
        await self._emit("status", status)
        return status

    async def stop(self, reason: str = "user") -> JsonDict:
        """Idempotent full rollback: stop the daemon (if any), then always ``deckgadget recover``."""
        async with self.operation_lock:
            self.stop_requested = True
            process, task = self.process, self.process_task
            if process is not None and process.returncode is None:
                decky.logger.info("stop(%s): SIGTERM → daemon pid %s", reason, process.pid)
                self.session_state = "STOPPING"
                try:
                    process.terminate()
                except ProcessLookupError:
                    pass
                try:
                    await asyncio.wait_for(process.wait(), STOP_TERM_GRACE_S)
                except asyncio.TimeoutError:
                    decky.logger.warning("daemon pid %s ignored SIGTERM for %.0fs — SIGKILL",
                                         process.pid, STOP_TERM_GRACE_S)
                    try:
                        process.kill()
                    except ProcessLookupError:
                        pass
                    try:
                        await asyncio.wait_for(process.wait(), STOP_KILL_GRACE_S)
                    except asyncio.TimeoutError:
                        decky.logger.error("daemon pid %s did not die after SIGKILL", process.pid)
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

    async def _supervise(self, process: asyncio.subprocess.Process) -> None:
        """Pump stdout/stderr until the daemon exits, then roll back unless stop() is doing it."""
        try:
            await asyncio.gather(self._pump_stdout(process), self._pump_stderr(process))
        except Exception:
            decky.logger.exception("daemon output pump failed")
        exit_code = await process.wait()
        self.process_exit_code = exit_code
        self._remove_pidfile()
        if self.process is process:
            self.process = None
        requested = self.stop_requested
        decky.logger.info("daemon pid %s exited with code %s (%s)", process.pid, exit_code,
                          "requested" if requested else "unexpected")
        self.first_event.set()
        if requested:
            return  # stop() owns the rollback and the status refresh
        self._reset_session()
        if exit_code != 0:
            body = self.last_error or f"daemon exited with code {exit_code} — see the Decky log"
            await self._toast("Controller mode failed", body, "error")
        async with self.operation_lock:
            if not self.daemon_alive():   # a newer session may already be running — never roll that one back
                await self._recover(f"daemon-exit rc={exit_code}")
        await self.emit_status(force=True)

    async def _pump_stdout(self, process: asyncio.subprocess.Process) -> None:
        assert process.stdout is not None
        while True:
            try:
                raw = await process.stdout.readline()
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
                decky.logger.info("[deckgadget] %s", line)
                continue
            if isinstance(event, dict):
                try:
                    await self._on_daemon_event(event)
                except Exception:
                    decky.logger.exception("error handling daemon event %r", event)

    async def _pump_stderr(self, process: asyncio.subprocess.Process) -> None:
        assert process.stderr is not None
        while True:
            try:
                raw = await process.stderr.readline()
            except ValueError:
                continue
            if not raw:
                break
            line = raw.decode("utf-8", "replace").rstrip("\r\n")
            if line.strip():
                self.output.append(f"err: {line}")
                decky.logger.info("[deckgadget] %s", line)

    async def _on_daemon_event(self, event: JsonDict) -> None:
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
                value = event.get(key)
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    self.metrics[key] = value
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
            # authoritative Status.screen_off for this session
            self.screen_off = bool(event.get("off"))
            await self.emit_status()
        else:
            decky.logger.debug("[deckgadget] unhandled event %r", event)

    async def _run_cli(self, *args: str, timeout: float) -> tuple[Optional[int], str, str]:
        """Run ``python3 -m deckgadget <args>`` → (returncode, stdout, stderr); raises TimeoutError."""
        process = await asyncio.create_subprocess_exec(
            PYTHON_BIN, "-m", DAEMON_MODULE, *args,
            cwd=self.py_modules_dir,
            env=_daemon_env(),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            limit=SUBPROCESS_LINE_LIMIT,
        )
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout)
        except asyncio.TimeoutError:
            try:
                process.kill()
            except ProcessLookupError:
                pass
            await process.wait()
            raise TimeoutError(f"deckgadget {args[0]} timed out after {timeout:g}s") from None
        return process.returncode, stdout.decode("utf-8", "replace"), stderr.decode("utf-8", "replace")

    async def _recover(self, reason: str) -> bool:
        """Idempotent full rollback via ``deckgadget recover``. The CLI always exits 0; success is judged from
        its JSON report (``ok`` / ``errors``) and a failure is surfaced as a toast + ``last_error``."""
        decky.logger.info("recover (%s)", reason)
        exit_code: Optional[int]
        try:
            exit_code, stdout, stderr = await self._run_cli("recover", timeout=RECOVER_TIMEOUT_S)
        except Exception as e:
            exit_code, stdout, stderr = None, "", f"{type(e).__name__}: {e}"
        report = _parse_json_object(stdout)
        report_errors = [str(item) for item in report.get("errors") or []] if report is not None else []
        report_ok = bool(report.get("ok")) and not report_errors if report is not None else False
        self.last_recover = {"ts": time.time(), "reason": reason, "rc": exit_code, "ok": report_ok,
                             "errors": report_errors, "stdout": stdout[-2000:], "stderr": stderr[-2000:]}
        if exit_code == 0 and report_ok:
            decky.logger.info("recover ok")
            return True
        if report_errors:
            detail = "; ".join(report_errors)
        elif exit_code != 0:
            detail = f"'deckgadget recover' exited with {exit_code}: {(stderr or stdout).strip()[-300:]}"
        else:
            detail = "'deckgadget recover' printed no JSON report"
        decky.logger.error("recover failed (rc=%s ok=%s): %s", exit_code, report_ok, detail[:500])
        self.last_error = f"Controller recovery failed: {detail}"[:500]
        await self._toast("Controller recovery failed",
                          f"{detail[:200]}. Press Stop again or reboot the Deck — a reboot always restores "
                          "everything.", "error")
        return False

    async def _cli_status(self, force: bool = False) -> tuple[Optional[JsonDict], Optional[str]]:
        """Cached ``deckgadget status`` JSON (spawned at most once per STATUS_CACHE_TTL_S unless forced)."""
        async with self.cli_lock:
            now = time.monotonic()
            if not force and self.cli_cache_time and now - self.cli_cache_time < STATUS_CACHE_TTL_S:
                return self.cli_cache, self.cli_error
            data: Optional[JsonDict] = None
            error: Optional[str] = None
            try:
                exit_code, stdout, stderr = await self._run_cli("status", timeout=STATUS_CLI_TIMEOUT_S)
                data = _parse_json_object(stdout)
                if data is None:
                    last_stderr_line = stderr.strip().splitlines()[-1] if stderr.strip() else ""
                    error = f"deckgadget status (rc={exit_code}) printed no JSON object"
                    if last_stderr_line:
                        error += f": {last_stderr_line}"
            except Exception as e:
                error = f"deckgadget status failed: {type(e).__name__}: {e}"
            if error and error != self.cli_error:
                decky.logger.warning("%s — using sysfs fallback", error)
            self.cli_cache_time = time.monotonic()
            self.cli_cache, self.cli_error = data, error
            return data, error

    async def build_status(self, force: bool = False) -> JsonDict:
        """Hardware facts from the CLI (sysfs fallback) + the daemon session view."""
        cli_status, cli_error = await self._cli_status(force)
        snapshot = _sysfs_snapshot()
        settings = self.settings.load()
        running = self.daemon_alive()
        state = self.session_state if running else "IDLE"

        status: JsonDict = {
            "ok": True,
            "plugin_version": self.version,
            "kernel": snapshot["kernel"],
            "model": snapshot["model"],
            "drd_enabled": snapshot["drd_enabled"],
            "udc_name": snapshot["udc_name"],
            "udc_state": snapshot["udc_state"],
            "udc_speed": snapshot["udc_speed"],
            "extcon": dict(snapshot["extcon"]),
            "host_connected": snapshot["host_connected"],
            "neptune_present": snapshot["neptune_present"],
            "neptune_captured": False,
            "cable_power": snapshot["cable_power"],
            "pd_contract_mv": snapshot["pd_contract_mv"],
            "pd_contract_ma": snapshot["pd_contract_ma"],
            "cable_kind": snapshot["cable_kind"],
        }
        if cli_status:
            status.update(_normalize_cli_status(cli_status))
        status.update({
            "neptune_captured": bool(status.get("neptune_captured")) or (running and state in CAPTURED_STATES),
            "daemon_running": running,
            "daemon_pid": self.process.pid if running and self.process is not None else None,
            "session_state": state,
            "session_detail": self.session_detail if running else "",
            "active_profile": self.active_profile if running else None,
            "transport": self.transport if running else None,
            # the daemon's own "screen" event wins; before it arrives, infer from the settings + state
            "screen_off": (self.screen_off if self.screen_off is not None
                           else bool(settings["screen_off"]) and state in CAPTURED_STATES) if running else False,
            "last_error": self.last_error,
            "metrics": dict(self.metrics),
            "status_error": cli_error,   # non-null ⇒ hardware fields came from the sysfs fallback
        })
        return status

    async def emit_status(self, force: bool = False) -> None:
        try:
            await self._emit("status", await self.build_status(force))
        except Exception:
            decky.logger.exception("emit status failed")

    async def _status_loop(self) -> None:
        """Every 2 s while the daemon runs; while idle, poll sysfs every 5 s and emit only on a change."""
        last_signature: Optional[tuple] = None
        while True:
            try:
                if self.daemon_alive():
                    await self.emit_status()
                    await asyncio.sleep(STATUS_PERIOD_RUNNING_S)
                else:
                    signature = _connectivity_signature(_sysfs_snapshot())
                    if signature != last_signature:
                        last_signature = signature
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
                "pid": self.process.pid if self.daemon_alive() and self.process is not None else None,
                "args": list(self.process_args),
                "started_at": self.process_started_at,
                "exit_code": self.process_exit_code,
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

    async def _emit(self, event: str, payload: JsonDict) -> None:
        try:
            await decky.emit(event, payload)
        except Exception:
            decky.logger.exception("emit %s failed", event)

    async def _toast(self, title: str, body: str, severity: str = "info") -> None:
        decky.logger.log({"error": 40, "warn": 30}.get(severity, 20), "toast: %s — %s", title, body)
        await self._emit("toast", {"title": title, "body": body, "severity": severity})

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
        text = _read_text(self.pidfile)
        if not text:
            return
        try:
            pid = int(text)
        except ValueError:
            self._remove_pidfile()
            return
        if pid <= 1 or not _is_deckgadget_pid(pid):
            self._remove_pidfile()
            return
        decky.logger.warning("stale deckgadget daemon (pid %d) from a previous session — terminating", pid)
        for signal_number, grace in ((signal.SIGTERM, STOP_TERM_GRACE_S), (signal.SIGKILL, STOP_KILL_GRACE_S)):
            try:
                os.kill(pid, signal_number)
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


def _error(error: BaseException, **extra: Any) -> JsonDict:
    """Uniform error answer for callables; expected (validation / missing file) errors log without a traceback."""
    if isinstance(error, (ValueError, FileNotFoundError, TimeoutError)):
        decky.logger.error("callable failed: %s: %s", type(error).__name__, error)
    else:
        decky.logger.exception("callable failed: %s", error)
    return {"ok": False, "error": str(error) or type(error).__name__, **extra}


class Plugin:
    """Decky Loader entry point. Decky may call these on an instance or with the class itself as ``self``
    (older loaders), so no state lives on ``self``; every callable answers ``{"ok": false, "error": …}``
    instead of raising."""

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
            for warning in warnings:
                decky.logger.warning("set_settings: %s (ignored)", warning)
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

    async def _main(self) -> None:
        backend = _backend()
        decky.logger.info("%s %s backend starting (python %s, plugin dir %s)",
                          PLUGIN_NAME, backend.version, sys.version.split()[0], backend.plugin_dir)
        try:
            await backend.startup()
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
        # settings have always lived in DECKY_PLUGIN_SETTINGS_DIR/settings.json
        decky.logger.info("%s: no migration needed", PLUGIN_NAME)
