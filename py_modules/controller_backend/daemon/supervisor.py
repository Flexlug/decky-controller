"""Lifecycle of the daemon process: spawn, pump its output, wait, stop (SIGTERM → SIGKILL), pidfile."""
from __future__ import annotations

import asyncio
import collections
import json
import logging
import os
import signal
import time
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Optional

from .events import JsonDict
from .launcher import DaemonPaths, SUBPROCESS_LINE_LIMIT, daemon_command, daemon_environment, is_deckgadget_pid

log = logging.getLogger("controller_backend.daemon.supervisor")

STOP_TERM_GRACE_S = 3.0                  # SIGTERM → wait this long → SIGKILL → wait STOP_KILL_GRACE_S
STOP_KILL_GRACE_S = 2.0
DRAIN_TIMEOUT_S = 2.0
OUTPUT_RING_SIZE = 200

EventCallback = Callable[[JsonDict], Awaitable[None]]
ExitCallback = Callable[[int, bool], Awaitable[None]]


@dataclass
class DaemonRun:
    """One ``deckgadget run`` process from spawn to exit; kept after exit for diagnostics."""
    process: asyncio.subprocess.Process
    args: list[str]
    started_at: float
    exit_code: Optional[int] = None
    stop_requested: bool = False
    first_event: asyncio.Event = field(default_factory=asyncio.Event)   # first stdout event, or exit
    output: collections.deque[str] = field(default_factory=lambda: collections.deque(maxlen=OUTPUT_RING_SIZE))
    task: Optional[asyncio.Task[None]] = None

    @property
    def alive(self) -> bool:
        return self.process.returncode is None


class DaemonSupervisor:
    """Owns at most one ``deckgadget run`` process. ``on_event`` gets every JSON event from stdout;
    ``on_exit(exit_code, stop_requested)`` runs once the process is gone and its output is drained."""

    def __init__(self, paths: DaemonPaths, on_event: EventCallback, on_exit: ExitCallback) -> None:
        self.paths = paths
        self.on_event = on_event
        self.on_exit = on_exit
        self.run: Optional[DaemonRun] = None

    @property
    def pid(self) -> Optional[int]:
        return self.run.process.pid if self.alive() else None

    @property
    def stop_requested(self) -> bool:
        return self.run is not None and self.run.stop_requested

    def alive(self) -> bool:
        return self.run is not None and self.run.alive

    async def spawn(self, args: list[str]) -> None:
        command = daemon_command("run", *args)
        log.info("starting daemon: %s", " ".join(command))
        process = await asyncio.create_subprocess_exec(
            *command,
            cwd=self.paths.py_modules_dir,
            env=daemon_environment(),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            limit=SUBPROCESS_LINE_LIMIT,
            start_new_session=True,   # own process group: its lifetime is managed here, not by signals
        )
        run = DaemonRun(process=process, args=list(args), started_at=time.time())
        self.run = run
        self._write_pidfile(process.pid)
        run.task = asyncio.create_task(self._supervise(run), name="deckgadget-supervisor")

    async def wait_first_event(self, timeout: float) -> None:
        if self.run is None:
            return
        try:
            await asyncio.wait_for(self.run.first_event.wait(), timeout)
        except asyncio.TimeoutError:
            log.debug("no daemon event within %.1fs", timeout)

    async def terminate(self, reason: str) -> None:
        """SIGTERM, then SIGKILL after the grace period; then let the supervisor task drain the output."""
        run = self.run
        if run is None:
            return
        run.stop_requested = True
        process = run.process
        if run.alive:
            log.info("stop(%s): SIGTERM → daemon pid %s", reason, process.pid)
            _signal_quietly(process.terminate)
            try:
                await asyncio.wait_for(process.wait(), STOP_TERM_GRACE_S)
            except asyncio.TimeoutError:
                log.warning("daemon pid %s ignored SIGTERM for %.0fs — SIGKILL", process.pid, STOP_TERM_GRACE_S)
                _signal_quietly(process.kill)
                try:
                    await asyncio.wait_for(process.wait(), STOP_KILL_GRACE_S)
                except asyncio.TimeoutError:
                    log.error("daemon pid %s did not die after SIGKILL", process.pid)
        if run.task is not None and not run.task.done():
            # never cancel the supervisor: it must finish draining and run on_exit
            try:
                await asyncio.wait_for(asyncio.shield(run.task), DRAIN_TIMEOUT_S)
            except asyncio.TimeoutError:
                log.warning("daemon output not drained within %.0fs", DRAIN_TIMEOUT_S)
            except Exception:
                log.exception("supervisor task failed while draining")

    async def kill_stale(self) -> None:
        """A daemon left behind by a previous backend instance (Decky restart/crash) must go before recover."""
        try:
            with open(self.paths.pidfile, encoding="utf-8") as pidfile:
                text = pidfile.read().strip()
        except OSError as exc:
            log.debug("no pidfile to check (%s)", exc)
            return
        try:
            pid = int(text)
        except ValueError:
            log.warning("pidfile %s holds %r — removing", self.paths.pidfile, text)
            self._remove_pidfile()
            return
        if pid <= 1 or not is_deckgadget_pid(pid):
            self._remove_pidfile()
            return
        log.warning("stale deckgadget daemon (pid %d) from a previous session — terminating", pid)
        for signal_number, grace in ((signal.SIGTERM, STOP_TERM_GRACE_S), (signal.SIGKILL, STOP_KILL_GRACE_S)):
            try:
                os.kill(pid, signal_number)
            except ProcessLookupError:
                log.debug("stale daemon pid %d already gone", pid)
                break
            deadline = time.monotonic() + grace
            while time.monotonic() < deadline and is_deckgadget_pid(pid):
                await asyncio.sleep(0.1)
            if not is_deckgadget_pid(pid):
                break
        else:
            log.error("stale daemon pid %d survived SIGKILL", pid)
        self._remove_pidfile()

    async def _supervise(self, run: DaemonRun) -> None:
        try:
            await asyncio.gather(self._pump_stdout(run), self._pump_stderr(run))
        except Exception:
            log.exception("daemon output pump failed")
        run.exit_code = await run.process.wait()
        self._remove_pidfile()
        log.info("daemon pid %s exited with code %s (%s)", run.process.pid, run.exit_code,
                 "requested" if run.stop_requested else "unexpected")
        run.first_event.set()
        try:
            await self.on_exit(run.exit_code, run.stop_requested)
        except Exception:
            log.exception("daemon exit handler failed")

    async def _pump_stdout(self, run: DaemonRun) -> None:
        assert run.process.stdout is not None
        while True:
            try:
                raw = await run.process.stdout.readline()
            except ValueError:   # line longer than SUBPROCESS_LINE_LIMIT — asyncio drops it, keep going
                log.warning("daemon stdout: over-long line skipped")
                continue
            if not raw:
                break
            line = raw.decode("utf-8", "replace").rstrip("\r\n")
            if not line.strip():
                continue
            run.output.append(f"out: {line}")
            try:
                event = json.loads(line)
            except ValueError:
                event = None
            if not isinstance(event, dict):
                log.info("[deckgadget] %s", line)
                continue
            run.first_event.set()
            try:
                await self.on_event(event)
            except Exception:
                log.exception("error handling daemon event %r", event)

    async def _pump_stderr(self, run: DaemonRun) -> None:
        assert run.process.stderr is not None
        while True:
            try:
                raw = await run.process.stderr.readline()
            except ValueError:
                log.warning("daemon stderr: over-long line skipped")
                continue
            if not raw:
                break
            line = raw.decode("utf-8", "replace").rstrip("\r\n")
            if line.strip():
                run.output.append(f"err: {line}")
                log.info("[deckgadget] %s", line)

    def _write_pidfile(self, pid: int) -> None:
        try:
            with open(self.paths.pidfile, "w", encoding="utf-8") as pidfile:
                pidfile.write(f"{pid}\n")
        except OSError as exc:
            log.warning("cannot write pidfile %s: %s", self.paths.pidfile, exc)

    def _remove_pidfile(self) -> None:
        try:
            os.unlink(self.paths.pidfile)
        except FileNotFoundError:
            pass
        except OSError as exc:
            log.warning("cannot remove pidfile %s: %s", self.paths.pidfile, exc)


def _signal_quietly(send: Callable[[], None]) -> None:
    try:
        send()
    except ProcessLookupError:
        pass   # already gone — exactly what we wanted
