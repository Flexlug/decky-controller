"""The backend service: starts/stops the daemon, keeps the session view, answers status, emits events.

Every exit path of a session ends in ``deckgadget recover`` (after every daemon exit, inside ``stop()``,
at plugin load); ``start`` / ``stop`` / recover never overlap (one lock).
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Awaitable, Callable, Optional

from controller_backend.daemon.commands import CliRunner, normalize_cli_status, run_recover, run_status
from controller_backend.daemon.events import JsonDict
from controller_backend.daemon.launcher import DaemonPaths, run_args
from controller_backend.daemon.supervisor import DaemonRun, DaemonSupervisor
from controller_backend.diagnostics import build_diagnostics
from controller_backend.session import SessionView, Toast
from controller_backend.settings import PROFILES, SettingsStore, resolve_transport
from controller_backend.status import build_status, connectivity_signature, hardware_facts

EmitCallback = Callable[[str, JsonDict], Awaitable[None]]

START_FIRST_EVENT_TIMEOUT_S = 2.0
STATUS_CACHE_TTL_S = 1.0
STATUS_PERIOD_RUNNING_S = 2.0
STATUS_PERIOD_IDLE_S = 5.0


class Service:
    def __init__(self, *, emit: EmitCallback, plugin_dir: str, settings_dir: str, runtime_dir: str, log_dir: str,
                 plugin_version: str, decky_version: Optional[str] = None, logger: Optional[logging.Logger] = None,
                 sysfs_root: str = "/sys", dev_root: str = "/dev", cli_runner: Optional[CliRunner] = None) -> None:
        self.log = logger or logging.getLogger("controller_backend.service")
        self._emit_callback = emit
        self.plugin_dir = plugin_dir
        self.settings_dir = settings_dir
        self.runtime_dir = runtime_dir
        self.log_dir = log_dir
        self.plugin_version = plugin_version
        self.decky_version = decky_version
        self.sysfs_root = sysfs_root
        self.dev_root = dev_root

        self.paths = DaemonPaths.for_plugin(plugin_dir, log_dir, runtime_dir)
        self.settings = SettingsStore(os.path.join(settings_dir, "settings.json"))
        self.session = SessionView()
        self.supervisor = DaemonSupervisor(self.paths, on_event=self._on_daemon_event, on_exit=self._on_daemon_exit)
        self.cli = cli_runner or CliRunner(self.paths)

        self.operation_lock = asyncio.Lock()
        self.cli_lock = asyncio.Lock()
        self.cli_cache_time = 0.0
        self.cli_cache: Optional[JsonDict] = None
        self.cli_error: Optional[str] = None
        self.last_recover: Optional[JsonDict] = None
        self.status_task: Optional[asyncio.Task[None]] = None

    # --- lifecycle ------------------------------------------------------------------------------

    async def startup(self) -> None:
        for directory in (self.settings_dir, self.runtime_dir, self.log_dir):
            try:
                os.makedirs(directory, exist_ok=True)
            except OSError as exc:
                self.log.warning("cannot create %s: %s", directory, exc)
        # Hold the lock for the whole load-time rollback: a start() arriving while the (up to 30 s) recover
        # still runs must wait, or recover would rebind usbhid / remove the gadget under the fresh daemon.
        async with self.operation_lock:
            await self.supervisor.kill_stale()
            await self._recover("plugin-load")   # a previous backend instance may have died mid-session
        if self.status_task is None or self.status_task.done():
            self.status_task = asyncio.create_task(self._status_loop(), name="decky-controller-status")

    async def shutdown(self, reason: str) -> None:
        if self.status_task is not None:
            self.status_task.cancel()
            try:
                await self.status_task
            except asyncio.CancelledError:
                pass
            except Exception:
                self.log.exception("status loop ended with an error")
            self.status_task = None
        await self.stop(reason)

    def daemon_alive(self) -> bool:
        return self.supervisor.alive()

    async def start(self, profile: Optional[str]) -> JsonDict:
        async with self.operation_lock:
            settings = self.settings.load()
            if not profile:
                profile = str(settings["profile"])
            if profile not in PROFILES:
                raise ValueError(f"unknown profile {profile!r} (expected one of {list(PROFILES)})")
            if self.supervisor.alive():
                self.log.info("start(%s): daemon already running (pid %s) — nothing to do", profile, self.supervisor.pid)
                return await self.build_status()
            if not os.path.isdir(self.paths.py_modules_dir):
                raise FileNotFoundError(f"daemon package directory missing: {self.paths.py_modules_dir}")
            for directory in (self.runtime_dir, self.log_dir):
                os.makedirs(directory, exist_ok=True)
            previous = self.supervisor.run
            if previous is not None and not previous.stop_requested and not previous.exit_handled:
                previous.exit_handled = True   # died unexpectedly and its exit handler has not run yet
                await self._recover(f"before-start after daemon-exit rc={previous.exit_code}")
            self.session.begin(profile, resolve_transport(profile, str(settings["transport"])))
            await self.supervisor.spawn(run_args(settings, profile, self.paths.log_path))
        # Outside the lock (a fast-failing daemon needs it for its rollback): wait briefly for the first event
        # so the caller gets something meaningful back.
        await self.supervisor.wait_first_event(START_FIRST_EVENT_TIMEOUT_S)
        status = await self.build_status(force=True)
        await self._emit("status", status)
        return status

    async def stop(self, reason: str = "user") -> JsonDict:
        """Idempotent full rollback: stop the daemon (if any), then always ``deckgadget recover``."""
        async with self.operation_lock:
            if self.supervisor.alive():
                self.session.state = "STOPPING"
            await self.supervisor.terminate(reason)
            self.session.reset()
            await self._recover(f"stop:{reason}")
        status = await self.build_status(force=True)
        await self._emit("status", status)
        return status

    async def build_status(self, force: bool = False) -> JsonDict:
        cli_status, cli_error = await self._cli_status(force)
        return build_status(
            plugin_version=self.plugin_version,
            facts=hardware_facts(self.sysfs_root, self.dev_root),
            cli_status=normalize_cli_status(cli_status) if cli_status else None,
            cli_error=cli_error,
            session=self.session,
            running=self.supervisor.alive(),
            daemon_pid=self.supervisor.pid,
            settings=self.settings.load(),
        )

    async def emit_status(self, force: bool = False) -> None:
        try:
            await self._emit("status", await self.build_status(force))
        except Exception:
            self.log.exception("emit status failed")

    async def diagnostics(self) -> JsonDict:
        return build_diagnostics(
            status=await self.build_status(force=True),
            plugin_version=self.plugin_version, decky_version=self.decky_version,
            settings=self.settings.load(), settings_path=self.settings.path,
            supervisor=self.supervisor, session_last_kill=self.session.last_kill,
            cli_status_raw=self.cli_cache, cli_status_error=self.cli_error, last_recover=self.last_recover,
            paths=self.paths, plugin_dir=self.plugin_dir, runtime_dir=self.runtime_dir, log_dir=self.log_dir,
        )

    # --- daemon callbacks -----------------------------------------------------------------------

    async def _on_daemon_event(self, event: JsonDict) -> None:
        outcome = self.session.apply(event, stop_requested=self.supervisor.stop_requested)
        if outcome.toast is not None:
            await self._toast(outcome.toast)
        if outcome.emit_status:
            await self.emit_status()

    async def _on_daemon_exit(self, run: DaemonRun) -> None:
        if run.stop_requested:
            return   # stop() owns the rollback and the status refresh
        async with self.operation_lock:
            if run.exit_handled or self.supervisor.run is not run:
                self.log.info("daemon pid %s: exit already handled or a newer session is running", run.process.pid)
                return
            run.exit_handled = True
            self.session.reset()
            if run.exit_code != 0:
                body = self.session.last_error or f"daemon exited with code {run.exit_code} — see the Decky log"
                await self._toast(Toast("Controller mode failed", body, "error"))
            await self._recover(f"daemon-exit rc={run.exit_code}")
        await self.emit_status(force=True)

    async def _recover(self, reason: str) -> bool:
        self.log.info("recover (%s)", reason)
        report = await run_recover(self.cli, reason)
        self.last_recover = report.as_dict()
        if report.ok:
            self.log.info("recover ok")
            return True
        self.log.error("recover failed (rc=%s): %s", report.exit_code, report.detail[:500])
        self.session.last_error = f"Controller recovery failed: {report.detail}"[:500]
        await self._toast(Toast("Controller recovery failed",
                                f"{report.detail[:200]}. Press Stop again or reboot the Deck — a reboot always "
                                "restores everything.", "error"))
        return False

    # --- status ---------------------------------------------------------------------------------

    async def _cli_status(self, force: bool = False) -> tuple[Optional[JsonDict], Optional[str]]:
        """Cached ``deckgadget status`` JSON (spawned at most once per STATUS_CACHE_TTL_S unless forced)."""
        async with self.cli_lock:
            now = time.monotonic()
            if not force and self.cli_cache_time and now - self.cli_cache_time < STATUS_CACHE_TTL_S:
                return self.cli_cache, self.cli_error
            data, error = await run_status(self.cli)
            if error and error != self.cli_error:
                self.log.warning("%s — using sysfs fallback", error)
            self.cli_cache_time = time.monotonic()
            self.cli_cache, self.cli_error = data, error
            return data, error

    async def _status_loop(self) -> None:
        """Every 2 s while the daemon runs; while idle, poll sysfs every 5 s and emit only on a change."""
        last_signature: Optional[tuple] = None
        while True:
            try:
                if self.supervisor.alive():
                    await self.emit_status()
                    await asyncio.sleep(STATUS_PERIOD_RUNNING_S)
                else:
                    signature = connectivity_signature(hardware_facts(self.sysfs_root, self.dev_root))
                    if signature != last_signature:
                        last_signature = signature
                        await self.emit_status(force=True)
                    await asyncio.sleep(STATUS_PERIOD_IDLE_S)
            except asyncio.CancelledError:
                raise
            except Exception:
                self.log.exception("status loop iteration failed")
                await asyncio.sleep(STATUS_PERIOD_IDLE_S)

    # --- events to the frontend -----------------------------------------------------------------

    async def _emit(self, event: str, payload: JsonDict) -> None:
        try:
            await self._emit_callback(event, payload)
        except Exception:
            self.log.exception("emit %s failed", event)

    async def _toast(self, toast: Toast) -> None:
        self.log.log({"error": logging.ERROR, "warn": logging.WARNING}.get(toast.severity, logging.INFO),
                     "toast: %s — %s", toast.title, toast.body)
        await self._emit("toast", toast.as_dict())
