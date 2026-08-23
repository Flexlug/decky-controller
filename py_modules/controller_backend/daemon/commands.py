"""One-shot daemon CLI calls with timeouts: ``deckgadget status`` (JSON snapshot) and ``deckgadget recover``."""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Optional

from controller_backend.daemon.events import JsonDict, parse_json_object
from controller_backend.daemon.launcher import DaemonPaths, SUBPROCESS_LINE_LIMIT, daemon_command, daemon_environment

log = logging.getLogger("controller_backend.daemon.commands")

STATUS_TIMEOUT_S = 5.0
RECOVER_TIMEOUT_S = 30.0

CliResult = tuple[Optional[int], str, str]   # exit code, stdout, stderr


class CliRunner:
    """Runs ``python3 -m deckgadget <subcommand>`` and returns (exit code, stdout, stderr); raises
    ``TimeoutError`` after killing a process that overran its timeout."""

    def __init__(self, paths: DaemonPaths) -> None:
        self.paths = paths

    async def run(self, subcommand: str, *args: str, timeout: float) -> CliResult:
        process = await asyncio.create_subprocess_exec(
            *daemon_command(subcommand, *args),
            cwd=self.paths.py_modules_dir,
            env=daemon_environment(),
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
            raise TimeoutError(f"deckgadget {subcommand} timed out after {timeout:g}s") from None
        return process.returncode, stdout.decode("utf-8", "replace"), stderr.decode("utf-8", "replace")


async def run_status(runner: CliRunner) -> tuple[Optional[JsonDict], Optional[str]]:
    """``(status json, None)`` or ``(None, error text)``; used for diagnostics only."""
    try:
        exit_code, stdout, stderr = await runner.run("status", timeout=STATUS_TIMEOUT_S)
    except (OSError, TimeoutError, ValueError) as exc:
        return None, f"deckgadget status failed: {type(exc).__name__}: {exc}"
    data = parse_json_object(stdout, "deckgadget status")
    if data is not None:
        return data, None
    error = f"deckgadget status (rc={exit_code}) printed no JSON object"
    last_stderr_line = stderr.strip().splitlines()[-1] if stderr.strip() else ""
    if last_stderr_line:
        error += f": {last_stderr_line}"
    return None, error


@dataclass
class RecoverReport:
    """Outcome of one ``deckgadget recover`` run. The CLI always exits 0; success is judged from its JSON
    report (``ok`` and an empty ``errors`` list)."""
    reason: str
    exit_code: Optional[int]
    ok: bool
    errors: list[str] = field(default_factory=list)
    detail: str = ""
    stdout: str = ""
    stderr: str = ""
    timestamp: float = field(default_factory=time.time)

    def as_dict(self) -> JsonDict:
        return {"ts": self.timestamp, "reason": self.reason, "rc": self.exit_code, "ok": self.ok,
                "errors": list(self.errors), "stdout": self.stdout[-2000:], "stderr": self.stderr[-2000:]}


async def run_recover(runner: CliRunner, reason: str) -> RecoverReport:
    try:
        exit_code, stdout, stderr = await runner.run("recover", timeout=RECOVER_TIMEOUT_S)
    except (OSError, TimeoutError, ValueError) as exc:
        exit_code, stdout, stderr = None, "", f"{type(exc).__name__}: {exc}"
    report = parse_json_object(stdout, "deckgadget recover") if stdout.strip() else None
    errors = [str(item) for item in (report.get("errors") or [])] if report is not None else []
    ok = exit_code == 0 and report is not None and bool(report.get("ok")) and not errors
    if ok:
        detail = ""
    elif errors:
        detail = "; ".join(errors)
    elif exit_code != 0:
        detail = f"'deckgadget recover' exited with {exit_code}: {(stderr or stdout).strip()[-300:]}"
    else:
        detail = "'deckgadget recover' printed no JSON report"
    return RecoverReport(reason=reason, exit_code=exit_code, ok=ok, errors=errors, detail=detail,
                         stdout=stdout, stderr=stderr)
