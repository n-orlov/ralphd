"""Spawn one pi iteration and capture its transcript."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import signal
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import state
from .pricing import PricingSource
from .privsep import agent_child_kwargs
from .redact import scrub_text

COMPLETE = "<promise>COMPLETE</promise>"
VERIFIED = "<promise>VERIFIED</promise>"

log = logging.getLogger("ralphd.runner")

# pi emits full message snapshots per NDJSON event — single lines routinely
# exceed asyncio's 64 KiB default readline limit
STREAM_LIMIT = 16 * 1024 * 1024

# Task 010 (#28): how long a timed-out iteration is given to shut down
# gracefully -- first for its output pump to drain whatever pi writes on the
# way out, then for the process itself to exit before it is SIGKILLed. A
# module constant so tests can compress it instead of sleeping for real.
SHUTDOWN_GRACE_S = 30


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
    # Task 012 (#14): the model pi actually used, as observed in its own
    # message stream -- `model` is the pi-style `provider/model` ref, and
    # `model_raw` the provider-side id when the two differ (see
    # `state.model_ids`). Both stay None when no assistant message named a
    # model (an instant startup failure, an in-band error with no traffic),
    # so the engine can tell "nothing observed" from "observed nothing".
    model: str | None = None
    model_raw: str | None = None

    @property
    def saw_complete(self) -> bool:
        return COMPLETE in self.final_text

    @property
    def saw_verified(self) -> bool:
        return VERIFIED in self.final_text


class PiRunner:
    """Runs `pi -p --mode json` as a subprocess, one call per iteration."""

    def __init__(self, workspace: Path, pi_bin: str = "pi",
                 pricing: PricingSource | None = None):
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
            # Task 020 (#48): under the uid boundary the iteration runs as the
            # `agent` uid while the engine keeps a real uid of 0, so nothing
            # this subprocess (or any tool it spawns) can signal the
            # supervisor -- not by pid, not by pgid, not by any `pkill`
            # pattern. Empty kwargs when the engine was not started as root,
            # so a test-suite or `--user 1000` engine spawns pi exactly as
            # before. The engine can still signal *it*: `interrupt()`'s
            # killpg is permitted downward (the engine's effective uid is the
            # child's real uid).
            **agent_child_kwargs(),
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
                    # Task 011 (#28) audit of this wait_for: safe. The
                    # awaitable is a fresh `first_traffic.wait()` coroutine
                    # created right here, so the task wait_for cancels on
                    # timeout is private to this call and is never awaited
                    # again -- the shape that made the old pump_task idiom
                    # re-raise CancelledError cannot arise. TimeoutError, not
                    # CancelledError, is what comes out. (A CancelledError
                    # DOES come out when the caller's `finally` cancels
                    # watchdog_task, which is exactly what that cancel means;
                    # the caller suppresses it there.)
                    await asyncio.wait_for(first_traffic.wait(), timeout=startup_timeout_s)
                except TimeoutError:
                    result.no_traffic_timeout = True
                    self.interrupt()

            pump_task = asyncio.ensure_future(pump())
            watchdog_task = asyncio.ensure_future(startup_watchdog())
            try:
                # Task 010 (#28): asyncio.wait(), NOT asyncio.wait_for(), on
                # the timeout path. wait_for cancels the future it timed out
                # on and awaits that cancellation, so the old idiom's second
                # `await asyncio.wait_for(pump_task, timeout=30)` re-awaited
                # an already-cancelled task -- which re-raises
                # CancelledError. CancelledError is a BaseException, so it
                # slipped past every `except Exception` between here and the
                # loop's per-iteration guard: an iteration that merely blew
                # its timeout took the whole engine down instead of being
                # recorded as one failed iteration. asyncio.wait() leaves the
                # task alone, so the pump survives the timeout and we can
                # SIGINT pi and still drain what it writes on the way out.
                done, _ = await asyncio.wait({pump_task}, timeout=timeout_s)
                if done:
                    # Surface an engine-side pump failure (stream/OS error)
                    # exactly as the old wait_for did: the caller turns it
                    # into one failed iteration.
                    await pump_task
                else:
                    result.timed_out = True
                    self.interrupt()
                    done, _ = await asyncio.wait({pump_task},
                                                 timeout=SHUTDOWN_GRACE_S)
                    if not done:
                        # pi ignored the SIGINT (or is wedged in a read):
                        # stop pumping. The outer `finally` SIGKILLs the
                        # process group, and this iteration is already
                        # recorded as timed out.
                        pump_task.cancel()
                    # Either way the timeout verdict stands, so a late pump
                    # error must not replace it with an exception.
                    with contextlib.suppress(BaseException):
                        await pump_task
            finally:
                watchdog_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await watchdog_task
            try:
                # Task 011 (#28) audit of this wait_for: safe, for the same
                # reason as the watchdog's -- `self._proc.wait()` is a fresh
                # coroutine, and the retry below builds ANOTHER one instead
                # of re-awaiting the task this call cancelled.
                await asyncio.wait_for(self._proc.wait(),
                                       timeout=SHUTDOWN_GRACE_S)
            except TimeoutError:
                # ...one real race though: the process may exit between the
                # timeout and the kill, and asyncio's Process.kill() raises
                # ProcessLookupError once returncode is set. Reaping it is
                # all that is left to do, so don't let it become a spurious
                # "engine iteration failure".
                with contextlib.suppress(ProcessLookupError):
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
                   pricing: PricingSource | None = None,
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
                # Task 012 (#14): the model *pi resolved*, not the ref the
                # engine asked for -- which may be None (pi's own default),
                # exactly the case where run state used to say `model: null`
                # while every message on the wire named a concrete id.
                resolved, raw = state.model_ids(msg.get("provider"), msg.get("model"))
                if resolved:
                    result.model, result.model_raw = resolved, raw
                usage = msg.get("usage") or {}
                for key in ("input", "output", "cacheRead", "cacheWrite", "totalTokens"):
                    result.usage[key] = result.usage.get(key, 0) + (usage.get(key) or 0)
                # Task 050 (#14): price against the id pi *reported* when the
                # engine pinned no ref of its own. `model` is None for every
                # unpinned run (`cfg.model_for(phase)` returns None and pi
                # picks its own model) -- which is precisely the route
                # `price_strategy: aws` was written for, so keying the rate
                # lookup on the request alone meant it could never fire.
                # A pinned ref still wins: an operator naming a ref decides
                # which rate applies, even when it resolves to nothing (an
                # unknown pinned ref stays `unavailable` rather than quietly
                # borrowing the observed id's rate).
                _accumulate_cost(usage, result, pricing=pricing,
                                 model=model or result.model)
        return True


def _accumulate_cost(usage: dict, result: IterationResult,
                     pricing: PricingSource | None = None,
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
    Task 049 (v0.6, steering 001) adds the *implausible zero*: this same
    gateway was later observed quoting `cost.total: 0` for 505 628 billed
    tokens (pi zero-fills the block when the resolved model definition carries
    no rates -- `artifacts/reports/pricing-anomaly.md`), which the rules above
    recorded as `costPriced: true` and every surface rendered as `$0.00`. A
    zero money quote next to billed tokens is therefore treated exactly like
    an absent one (unpriced, derivable), plus a `costZeroQuoted: true` marker
    and a warning naming the anomaly. The single exception is declared, never
    inferred: `pricing.free` patterns (`PricingMap.is_free`) mark the usage
    `costFree: true` and keep the honest `$0.00`.

    `model` is what the CALLER decided this message should be priced as: the
    ref the engine requested if it pinned one, else the id pi reported having
    resolved (task 050 -- see `_scan_line`). This function does not choose
    between them; it only ever looks up the one id it was given.
    """
    cost = (usage.get("cost") or {}).get("total")
    billed = sum(int(usage.get(k) or 0) for k in state.COST_TOKEN_KEYS)
    declared_free = bool(pricing and pricing.is_free(model))
    # A quote of exactly 0 for billable tokens is only believed when the route
    # declares itself free; otherwise it is a missing price, not a price.
    zero_quote = (cost is not None and not cost and billed and not declared_free)
    if cost is not None and not zero_quote:
        result.usage["costUSD"] = round(result.usage.get("costUSD", 0) + cost, 6)
        result.usage.setdefault("costPriced", True)
        if billed and not cost:
            # record the declaration alongside the zero, so a later reader
            # (state.is_zero_quote, the status.json rollup) can tell this $0
            # from the anomaly without access to the config
            result.usage["costFree"] = True
        return
    if not billed:
        # no price and nothing billed: $0 is the truth, byte-for-byte as before
        result.usage["costUSD"] = round(result.usage.get("costUSD", 0) + 0, 6)
        return
    result.usage["costPriced"] = False
    if zero_quote:
        result.usage["costZeroQuoted"] = True
        log.warning("cost: %s (model=%s, %d tokens)",
                    state.COST_ZERO_QUOTE_NOTICE, model or "unknown", billed)
    derived = pricing.derive(usage, model) if pricing else None
    if derived is None:
        result.usage["costDerived"] = False
    else:
        result.usage["costDerivedUSD"] = round(
            result.usage.get("costDerivedUSD", 0.0) + derived, 6)
        result.usage.setdefault("costDerived", True)
