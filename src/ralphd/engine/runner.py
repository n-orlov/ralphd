"""Spawn one pi iteration and capture its transcript."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import signal
import time
from dataclasses import dataclass, field
from pathlib import Path

from .pricing import PricingMap
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
    # True when the engine's own startup-window watchdog (task 001a) killed
    # this iteration because it observed zero LLM traffic (no parseable pi
    # NDJSON event at all) within the configured startup window -- distinct
    # from `timed_out`, which is the *full* iteration_timeout_s firing.
    no_traffic_timeout: bool = False

    @property
    def saw_complete(self) -> bool:
        return COMPLETE in self.final_text

    @property
    def saw_verified(self) -> bool:
        return VERIFIED in self.final_text


class PiRunner:
    """Runs `pi -p --mode json` as a subprocess, one call per iteration."""

    def __init__(self, workspace: Path, pi_bin: str = "pi",
                 pricing: PricingMap | None = None):
        self.workspace = workspace
        self.pi_bin = pi_bin
        # Task 052 (#10): optional host-side rate table, consulted ONLY when
        # the provider quotes no price of its own (see _accumulate_cost).
        self.pricing = pricing
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
        startup_timeout_s: float | None = None,
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

            first_traffic = asyncio.Event()

            async def pump() -> None:
                with open(transcript, "a") as out:
                    while True:
                        line = await self._proc.stdout.readline()
                        if not line:
                            break
                        saw_event = self._scan_line(line, result,
                                                    pricing=self.pricing,
                                                    model=model)
                        if saw_event and not first_traffic.is_set():
                            first_traffic.set()
                        # Mechanical secret redaction (task 060): scrub any
                        # known secret value before it ever touches disk --
                        # scanning above uses the raw bytes so sentinel/usage
                        # extraction is unaffected either way.
                        out.write(scrub_text(line.decode(errors="replace")))
                        out.flush()

            async def startup_watchdog() -> None:
                # Task 001a: fail fast on a hang with zero LLM traffic --
                # e.g. a DNS/gateway glitch the process blocks on
                # internally -- instead of waiting out the full iteration
                # timeout (the live incident this guards against: a
                # transient "getaddrinfo ENOTFOUND" hung the *entire*
                # 45-minute iteration timeout before finally dying).
                if not startup_timeout_s:
                    return
                try:
                    await asyncio.wait_for(first_traffic.wait(), timeout=startup_timeout_s)
                except TimeoutError:
                    result.no_traffic_timeout = True
                    self.interrupt()

            pump_task = asyncio.ensure_future(pump())
            watchdog_task = asyncio.ensure_future(startup_watchdog())
            try:
                await asyncio.wait_for(pump_task, timeout=timeout_s)
            except TimeoutError:
                result.timed_out = True
                self.interrupt()
                try:
                    await asyncio.wait_for(pump_task, timeout=30)
                except TimeoutError:
                    pass
            finally:
                watchdog_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await watchdog_task
            try:
                await asyncio.wait_for(self._proc.wait(), timeout=30)
            except TimeoutError:
                self._proc.kill()
                await self._proc.wait()
            result.exit_code = self._proc.returncode
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
    def _scan_line(line: bytes, result: IterationResult,
                   pricing: PricingMap | None = None,
                   model: str | None = None) -> bool:
        """Extract final assistant text + usage from pi's NDJSON events.

        Returns True iff `line` parsed as JSON at all -- i.e. this is the
        first observable sign of LLM/agent traffic (task 001a's startup-
        window watchdog uses this to distinguish a hang that produces
        nothing at all from an agent that's genuinely streaming).
        """
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            return False
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
                _accumulate_cost(usage, result, pricing=pricing, model=model)
        return True


def _accumulate_cost(usage: dict, result: IterationResult,
                     pricing: PricingMap | None = None,
                     model: str | None = None) -> None:
    """Fold one message's `usage.cost.total` into `result.usage` (task 049, #10).

    A *missing* price is not a price of zero. Real gateways (and Bedrock via
    some gateways) bill plenty of tokens while reporting no `cost` block at
    all; coercing that to 0 silently understated whole runs as $0.0000 with
    no way to tell "free" from "unknown". So:

    * price reported -> add it to `costUSD`, and mark `costPriced` true
      (unless some other message in this iteration already came back
      unpriced -- see below);
    * no price but tokens were billed -> record `costPriced: false` and add
      NOTHING, so a fully-unpriced iteration has no `costUSD` key at all
      (unknown, not zero) and a mixed one keeps the priced subtotal flagged
      as partial;
    * no price and nothing billed (pi zero-fills `usage` on an in-band error,
      so a no-traffic iteration still reaches this line) -> $0 is the truth
      and the int-0 accumulation stays byte-for-byte what it always was.

    Task 052 (#10) adds one branch to the unpriced case: when a host-side
    `pricing` map knows a rate for this `model`, the cost is *derived* from
    the token counters. It accumulates into a SEPARATE `costDerivedUSD`
    field marked `costDerived: true` -- never folded into `costUSD`, so "the
    provider quoted this" and "we computed this from a local rate table" stay
    distinguishable in the stored contract and on every surface. `costPriced`
    stays `false` either way (the provider still quoted nothing), and
    `costDerived: false` means at least one unpriced message had no rate, so
    part of this iteration's cost remains genuinely unknown.
    """
    cost = (usage.get("cost") or {}).get("total")
    if cost is not None:
        result.usage["costUSD"] = round(result.usage.get("costUSD", 0) + cost, 6)
        result.usage.setdefault("costPriced", True)
        return
    billed = sum(int(usage.get(k) or 0) for k in
                 ("input", "output", "cacheRead", "cacheWrite", "totalTokens"))
    if billed:
        result.usage["costPriced"] = False
        derived = pricing.derive(usage, model) if pricing else None
        if derived is None:
            result.usage["costDerived"] = False
        else:
            result.usage["costDerivedUSD"] = round(
                result.usage.get("costDerivedUSD", 0.0) + derived, 6)
            result.usage.setdefault("costDerived", True)
    else:
        result.usage["costUSD"] = round(result.usage.get("costUSD", 0) + 0, 6)
