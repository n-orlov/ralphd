"""Spawn one pi iteration and capture its transcript."""

from __future__ import annotations

import asyncio
import json
import os
import signal
import time
from dataclasses import dataclass, field
from pathlib import Path

from .redact import scrub_text

COMPLETE = "<promise>COMPLETE</promise>"
VERIFIED = "<promise>VERIFIED</promise>"

# pi emits full message snapshots per NDJSON event — single lines routinely
# exceed asyncio's 64 KiB default readline limit
STREAM_LIMIT = 16 * 1024 * 1024


@dataclass
class IterationResult:
    exit_code: int | None = None
    interrupted: bool = False
    timed_out: bool = False
    final_text: str = ""
    error_message: str = ""
    usage: dict = field(default_factory=dict)
    # Wall-clock seconds the agent subprocess ran for, start to exit (task
    # 059: used to distinguish an instant startup/infra failure -- e.g. no
    # LLM credentials, the process errors out before doing any work at all
    # -- from a genuine attempted-but-failed work iteration). None only if
    # the subprocess was never actually spawned (engine-side failure before
    # create_subprocess_exec).
    duration_s: float | None = None

    @property
    def saw_complete(self) -> bool:
        return COMPLETE in self.final_text

    @property
    def saw_verified(self) -> bool:
        return VERIFIED in self.final_text


class PiRunner:
    """Runs `pi -p --mode json` as a subprocess, one call per iteration."""

    def __init__(self, workspace: Path, pi_bin: str = "pi"):
        self.workspace = workspace
        self.pi_bin = pi_bin
        self._proc: asyncio.subprocess.Process | None = None

    @property
    def running(self) -> bool:
        return self._proc is not None and self._proc.returncode is None

    def interrupt(self) -> bool:
        """SIGINT the running pi process group. Returns False if none running."""
        if not self.running:
            return False
        try:
            os.killpg(os.getpgid(self._proc.pid), signal.SIGINT)
            return True
        except ProcessLookupError:
            return False

    async def run(
        self,
        prompt: str,
        transcript: Path,
        model: str | None = None,
        thinking: str | None = None,
        timeout_s: int = 2700,
        extra_env: dict | None = None,
    ) -> IterationResult:
        cmd = [self.pi_bin, "-p", "--mode", "json", "--no-session"]
        if model:
            cmd += ["--model", model]
        if thinking:
            cmd += ["--thinking", thinking]
        env = {**os.environ, **(extra_env or {})}

        result = IterationResult()
        _t0 = time.monotonic()
        self._proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            cwd=str(self.workspace),
            env=env,
            limit=STREAM_LIMIT,
            start_new_session=True,  # own pgid so SIGINT hits pi, not the engine
        )
        try:
            try:
                self._proc.stdin.write(prompt.encode())
                self._proc.stdin.write_eof()
            except (BrokenPipeError, ConnectionResetError):
                pass

            async def pump() -> None:
                with open(transcript, "a") as out:
                    while True:
                        line = await self._proc.stdout.readline()
                        if not line:
                            break
                        self._scan_line(line, result)
                        # Mechanical secret redaction (task 060): scrub any
                        # known secret value before it ever touches disk --
                        # scanning above uses the raw bytes so sentinel/usage
                        # extraction is unaffected either way.
                        out.write(scrub_text(line.decode(errors="replace")))
                        out.flush()

            try:
                await asyncio.wait_for(pump(), timeout=timeout_s)
            except TimeoutError:
                result.timed_out = True
                self.interrupt()
                try:
                    await asyncio.wait_for(self._proc.wait(), timeout=30)
                except TimeoutError:
                    self._proc.kill()
            result.exit_code = await self._proc.wait()
        finally:
            # whatever went wrong above, never leave an orphaned agent running
            if self._proc is not None and self._proc.returncode is None:
                try:
                    os.killpg(os.getpgid(self._proc.pid), signal.SIGKILL)
                except ProcessLookupError:
                    pass
                await self._proc.wait()
            self._proc = None
        result.duration_s = time.monotonic() - _t0
        if result.exit_code and result.exit_code < 0:
            result.interrupted = True
        if result.exit_code == -signal.SIGINT or result.exit_code == 130:
            result.interrupted = True
        return result

    @staticmethod
    def _scan_line(line: bytes, result: IterationResult) -> None:
        """Extract final assistant text + usage from pi's NDJSON events."""
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            return
        if event.get("type") == "message_end":
            msg = event.get("message", {})
            if msg.get("role") == "assistant":
                if msg.get("stopReason") == "error":
                    result.error_message = msg.get("errorMessage", "unknown agent error")
                text = "".join(c.get("text", "") for c in msg.get("content", [])
                               if c.get("type") == "text")
                if text.strip():
                    result.final_text = text
                usage = msg.get("usage") or {}
                for key in ("input", "output", "cacheRead", "cacheWrite", "totalTokens"):
                    result.usage[key] = result.usage.get(key, 0) + (usage.get(key) or 0)
                cost = (usage.get("cost") or {}).get("total") or 0
                result.usage["costUSD"] = round(result.usage.get("costUSD", 0) + cost, 6)
