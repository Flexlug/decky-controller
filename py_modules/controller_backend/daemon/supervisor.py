"""Lifecycle of the daemon process: spawn, pump its output, wait, stop (SIGTERM → SIGKILL), pidfile."""
from __future__ import annotations

import asyncio
import collections
import logging
import os
import signal
import time
from typing import Awaitable, Callable, Optional

from .events import JsonDict, parse_event_line
from .launcher import DaemonPaths, SUBPROCESS_LINE_LIMIT, daemon_command, daemon_environment, is_deckgadget_pid

log = logging.getLogger("controller_backend.daemon.supervisor")

STOP_TERM_GRACE_S = 3.0                  # SIGTERM → wait this long → SIGKILL → wait STOP_KILL_GRACE_S
STOP_KILL_GRACE_S = 2.0
DRAIN_TIMEOUT_S = 2.0
OUTPUT_RING_SIZE = 200

EventCallback = Callable[[JsonDict], Awaitable[None]]
ExitCallback = Callable[[int, bool], Awaitable[None]]


class DaemonSupervisor:
    """Owns at most one ``deckgadget run`` process. ``on_event`` gets every JSON event from stdout;
    ``on_exit(exit_code, stop_requested)`` runs once the process is gone and its output is drained."""

    def __init__(self, paths: DaemonPaths, on_event: EventCallback, on_exit: ExitCallback) -> None:
        self.paths = paths
        self.on_event = on_event
        self.on_exit = on_exit
        self.process: Optional[asyncio.subprocess.Process] = None
        self.task: Optional[asyncio.Task[None]] = None
        self.args: list[str] = []
        self.started_at: Optional[float] = None
        self.exit_code: Optional[int] = None
        self.stop_requested = False
        self.first_event = asyncio.Event()   # set by the first daemon event or by its exit
        self.output: collections.deque[str] = collections.deque(maxlen=OUTPUT_RING_SIZE)

    def alive(self) -> bool:
        return self.process is not None and self.process.returncode is None

    @property
    def pid(self) -> Optional[int]:
        return self.process.pid if self.alive() and self.process is not None else None

    async def spawn(self, args: list[str]) -> None:
        command = daemon_command("run", *args)
        log.info("starting daemon: %s", " ".join(command))
        self.args = list(args)
        self.exit_code = None
        self.stop_requested = False
        self.first_event = asyncio.Event()
        self.output.clear()
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
        self.process = process
        self.started_at = time.time()
        self._write_pidfile(process.pid)
        self.task = asyncio.create_task(self._supervise(process), name="deckgadget-supervisor")

    async def wait_first_event(self, timeout: float) -> None:
        try:
            await asyncio.wait_for(self.first_event.wait(), timeout)
        except asyncio.TimeoutError:
            log.debug("no daemon event within %.1fs", timeout)

    async def terminate(self, reason: str) -> None:
        """SIGTERM, then SIGKILL after the grace period; then let the supervisor task drain the output."""
        self.stop_requested = True
        process, task = self.process, self.task
        if process is not None and process.returncode is None:
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
        if task is not None and not task.done():
            # never cancel the supervisor: it must finish draining and run on_exit
            try:
                await asyncio.wait_for(asyncio.shield(task), DRAIN_TIMEOUT_S)
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

    async def _supervise(self, process: asyncio.subprocess.Process) -> None:
        try:
            await asyncio.gather(self._pump_stdout(process), self._pump_stderr(process))
        except Exception:
            log.exception("daemon output pump failed")
        exit_code = await process.wait()
        self.exit_code = exit_code
        self._remove_pidfile()
        if self.process is process:
            self.process = None
        requested = self.stop_requested
        log.info("daemon pid %s exited with code %s (%s)", process.pid, exit_code,
                 "requested" if requested else "unexpected")
        self.first_event.set()
        try:
            await self.on_exit(exit_code, requested)
        except Exception:
            log.exception("daemon exit handler failed")

    async def _pump_stdout(self, process: asyncio.subprocess.Process) -> None:
        assert process.stdout is not None
        while True:
            try:
                raw = await process.stdout.readline()
            except ValueError:   # line longer than SUBPROCESS_LINE_LIMIT — asyncio drops it, keep going
                log.warning("daemon stdout: over-long line skipped")
                continue
            if not raw:
                break
            line = raw.decode("utf-8", "replace").rstrip("\r\n")
            if not line.strip():
                continue
            self.output.append(f"out: {line}")
            event = parse_event_line(line)
            if event is None:
                log.info("[deckgadget] %s", line)
                continue
            self.first_event.set()
            try:
                await self.on_event(event)
            except Exception:
                log.exception("error handling daemon event %r", event)

    async def _pump_stderr(self, process: asyncio.subprocess.Process) -> None:
        assert process.stderr is not None
        while True:
            try:
                raw = await process.stderr.readline()
            except ValueError:
                log.warning("daemon stderr: over-long line skipped")
                continue
            if not raw:
                break
            line = raw.decode("utf-8", "replace").rstrip("\r\n")
            if line.strip():
                self.output.append(f"err: {line}")
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
