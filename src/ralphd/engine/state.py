"""Run-dir state: paths, atomic JSON writes, events log."""

from __future__ import annotations

import calendar
import fcntl
import hashlib
import json
import logging
import os
import re
import shutil
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, TextIO

from ..log_merge import (
    ITERATION_ATTEMPTS_DIR,
    OUTPUT_FILENAME,
    PROMPT_FILENAME,
    iteration_attempt_dirs,
    iteration_dir,
    iteration_numbers,
    iteration_output_path,
    iteration_prompt_path,
)
from . import contract_enforcement
from . import plan_ingestion
from . import privsep
from . import transitions
from .faults import (
    FAULT_CLASS_RATE_LIMIT,
    FAULT_CLASS_REFUSED,
    FAULT_REASON_RATE_LIMIT,
    FAULT_REASON_REFUSED,
    RATE_LIMIT_CEILING_REASON,
    explain_fault,
    matched_refusal,
    matched_signature,
)
from .redact import redact_job_yaml, scrub_text
from . import tiers

log = logging.getLogger("ralphd.state")


class RunDirLocked(Exception):
    """Raised when another live engine already holds the run dir's lock."""


# #216 measured a predecessor engine's terminal-state -> process-exit lag at
# 0.16-0.32 s in every condition tested, including under CPU starvation --
# the flock is released only on process exit (see acquire_lock's docstring),
# so a successor starting in that window used to see a false-positive
# RunDirLocked. 2.0 s covers that measured tail by roughly an order of
# magnitude while still failing fast (well under a human-noticeable delay)
# against a genuinely concurrent live engine, whose lock is held for the
# whole run rather than a brief exit tail.
ACQUIRE_LOCK_TIMEOUT_S = 2.0
# Small enough that the worst-case overshoot past the deadline is negligible
# relative to ACQUIRE_LOCK_TIMEOUT_S, large enough not to busy-spin.
ACQUIRE_LOCK_POLL_INTERVAL_S = 0.02


class SchemaVersionTooNew(Exception):
    """Raised when a run dir's recorded schemaVersion is newer than this
    engine build knows how to run against (PRD req 18)."""


# Bump only when the run-dir *on-disk shape* changes in a way older engines
# cannot safely continue against (new required fields/files, renamed keys,
# etc). Recorded in status.json's "schemaVersion" field on every startup.
# Policy: a recorded version newer than this refuses to start (clear
# diagnostic naming both versions, nothing else touched); a recorded version
# older than this (or absent -- pre-schema run dirs predating this feature)
# is accepted and the field is stamped/upgraded to CURRENT_SCHEMA_VERSION.
CURRENT_SCHEMA_VERSION = 1


def utcnow() -> str:
    return utc_from_epoch(time.time())


def utc_from_epoch(epoch: float) -> str:
    """utcnow()-format timestamp for an arbitrary UTC epoch seconds value --
    the inverse of parse_utc(), used wherever a *future* wall-clock instant is
    published (status.json's deadlineAt, infraWait.nextAttemptAt)."""
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch))


def parse_utc(ts: str) -> float:
    """Parse a utcnow()-format timestamp into UTC epoch seconds."""
    return calendar.timegm(time.strptime(ts, "%Y-%m-%dT%H:%M:%SZ"))


def elapsed_seconds(start_ts: str | None, end_ts: str | None = None) -> float | None:
    """Seconds between `start_ts` and `end_ts` (or now, if `end_ts` is
    falsy) -- the single source of truth PRD-051 duration fields/lines are
    derived from everywhere (status, logs pretty renderer). Returns None
    when `start_ts` itself is missing/falsy (nothing to measure yet)."""
    if not start_ts:
        return None
    start = parse_utc(start_ts)
    end = parse_utc(end_ts) if end_ts else time.time()
    return max(0.0, end - start)


def format_duration(seconds: float | None) -> str:
    """Compact human duration string with no millisecond noise, e.g.
    '45s', '3h 12m', '2d 1h'. The one shared formatter every duration
    display (status text, --json helpers that render text, logs pretty
    renderer) goes through -- no copy-pasted arithmetic anywhere else."""
    if seconds is None:
        return "n/a"
    total = max(0, round(seconds))
    if total < 60:
        return f"{total}s"
    minutes, s = divmod(total, 60)
    if minutes < 60:
        return f"{minutes}m {s}s" if s else f"{minutes}m"
    hours, m = divmod(minutes, 60)
    if hours < 24:
        return f"{hours}h {m}m" if m else f"{hours}h"
    days, h = divmod(hours, 24)
    return f"{days}d {h}h" if h else f"{days}d"


def format_approach(approach, max_approaches) -> str:
    """Render the approach counter as `n/m` -- task 007 (#16).

    The ONE shared renderer for the approach column/line (`ralphctl status`,
    `ralphctl runs`, and the hub through `ui_server`), so no surface invents
    its own denominator. Three cases, each of them honest:

    - no approach recorded yet (`None`/empty)  -> `""`. A run that has not
      started an approach has no counter, and printing `/3` would claim a
      position in a ladder it never entered.
    - approach but no `maxApproaches` (a pre-v0.6 run dir, where
      `GET /status` publishes an explicit `null` -- see api.py) -> `"2"`
      bare. The limit is genuinely unknown; the live config's value must
      never be guessed in, which is exactly why this takes the denominator
      as an argument instead of reading it from anywhere.
    - both known -> `"2/3"`.

    Values are rendered as given (ints in practice); non-numeric junk
    degrades to its string form rather than raising -- a status line must
    never be the thing that breaks output (same contract as
    `format_duration`/`format_local_time`).
    """
    if approach is None or approach == "":
        return ""
    if max_approaches is None or max_approaches == "":
        return str(approach)
    return f"{approach}/{max_approaches}"


# Column width for the hat name in `format_provisioning_lines` below --
# widest of today's six `MODEL_HATS` names ("planning") plus one space, so a
# shorter hat's model ref still lines up in `ralphctl status`'s block. Not
# derived from the live table on purpose: a stable width means the block
# does not jitter run to run depending on which hats are configured.
_PROVISIONING_HAT_WIDTH = 9


def format_provisioning_lines(table: list[dict]) -> list[str]:
    """Task 010 (#79, PRD requirement O), widened by task 006 (#68) to the
    full hat x tier matrix: the ONE shared formatter for the resolved hat ->
    model mapping -- `JobConfig.provisioning_table()`'s structured rows,
    worded exactly once here and rendered UNCHANGED by both `ralphctl
    status` (plain text) and the hub's run-header card (`renderSummary` in
    app.js, the same `startedAtLocal`/`endedAtLocal` discipline task 048
    established: Python words the fact, the browser only puts the string
    in a text node).

    A collapsed (single-rung) row -- `provisioning_table`'s form for a
    one-model configuration, or any hat with no configured `model_tiers`
    rung -- renders exactly as task 010 shipped: one line, `<hat>  <model
    ref>` plus a `(thinking: ...)` or `(effort: ...)` suffix when that
    hat's bundle carries one -- never both, the same mutual exclusion
    `HatBundle`/`bundle_for` enforce, so this function never has to guess
    which one to print.

    A widened row (a `"rungs"` list -- at least one tier's bundle differs
    from the others) instead renders one line PER TIER, labelled
    `<hat>.<tier>` (e.g. `worker.hard`) so a distinct rung is visibly tied
    to both its hat and the tier that configured it, never collapsed back
    down to a single ambiguous line. Same suffix discipline per line.

    Either way, a hat resolving to pi's own default (no configured ref)
    reads `pi default`, never the Python `None` an f-string would
    otherwise print, and never the retired `strategy: balanced`-style
    wording the ORIGINAL task 006 (#60) deleted.

    Deliberately independent of whether any iteration has run: the table
    comes from job.yaml alone (`JobConfig.provisioning_table`), so this
    reads the same the instant a run is created as it does after its
    hundredth iteration.
    """
    def _bundle_suffix(bundle_row: dict) -> str:
        if bundle_row.get("thinking"):
            return f"  (thinking: {bundle_row['thinking']})"
        if bundle_row.get("effort"):
            return f"  (effort: {bundle_row['effort']})"
        return ""

    def _harness_suffix(row: dict) -> str:
        # Every hat line names the agent harness it runs on, after the model
        # and any thinking/effort suffix.
        return f"  [{row.get('harness') or 'pi'}]"

    lines = []
    for row in table:
        if not isinstance(row, dict):
            continue
        hat = str(row.get("hat", ""))
        rungs = row.get("rungs")
        if isinstance(rungs, list) and rungs:
            for rung in rungs:
                if not isinstance(rung, dict):
                    continue
                tier = str(rung.get("tier", ""))
                label = f"{hat}.{tier}"
                model = rung.get("model") or "pi default"
                lines.append(f"{label:<{_PROVISIONING_HAT_WIDTH}} {model}"
                             f"{_bundle_suffix(rung)}{_harness_suffix(row)}")
            continue
        model = row.get("model") or "pi default"
        lines.append(f"{hat:<{_PROVISIONING_HAT_WIDTH}} {model}"
                     f"{_bundle_suffix(row)}{_harness_suffix(row)}")
    return lines


def model_ids(provider, model) -> tuple[str | None, str | None]:
    """`(resolved id, raw gateway id)` for one assistant message -- task 012 (#14).

    pi reports the model it actually used per message as a `provider` plus a
    provider-side `model` id (`amazon-bedrock` + `eu.anthropic.claude-opus-5`).
    The engine records BOTH halves of that, because they answer two different
    operator questions:

    - the *resolved id* is the pi-style `provider/model` ref -- the same string
      an operator would pass to `--model`, and the string the pricing tables
      match against, so "why is this unpriced" is answerable from run state;
    - the *raw gateway id* is what the provider itself called the model. It is
      returned only when it genuinely differs from the resolved ref (i.e. the
      provider prefix was added), so a surface never shows the same string
      twice claiming they are two facts.

    A message that names no model at all yields `(None, None)`: the engine then
    leaves whatever it already recorded alone rather than overwriting a known
    id with ignorance. Defensive like `format_duration`/`format_approach`:
    junk degrades to its string form instead of raising.
    """
    raw = "" if model is None or isinstance(model, bool) else str(model).strip()
    prov = "" if provider is None or isinstance(provider, bool) else str(provider).strip()
    if not raw:
        return None, None
    if not prov or raw.startswith(prov + "/"):
        return raw, None
    return f"{prov}/{raw}", raw


# Absolute-timestamp display format (task 048, #4): local wall clock plus the
# UTC offset, so a timestamp copied out of the hub or the CLI is unambiguous
# without the reader having to know which machine's timezone it came from.
# Deliberately NOT the ISO/`Z` wire format: the stored/published value stays
# `utcnow()`-shaped everywhere (payloads keep the raw ISO field for sorting
# and machine consumers), this is purely the human rendering of it.
LOCAL_TIME_FORMAT = "%Y-%m-%d %H:%M:%S %z"


def format_local_time(ts: str | None) -> str:
    """Absolute local-time rendering of a `utcnow()`-format timestamp.

    The ONE shared absolute-timestamp formatter (task 048, #4): `ralphctl
    status`, the `ralphctl logs` iteration boundary lines and the hub's
    run list / summary card / iteration timeline all render timestamps
    through this function -- the hub gets the already-formatted string from
    `ui_server` (alongside the untouched raw ISO field) rather than
    reimplementing the format in JavaScript, which is why "local" always
    means *the host running ralphd*, not the browser's timezone.

    Mirrors `format_duration`'s defensive contract: `None`/empty renders
    `"n/a"`, and an unparseable value degrades to itself rather than
    raising -- a status line must never be the thing that breaks output.
    """
    if not ts:
        return "n/a"
    try:
        epoch = parse_utc(ts)
    except (ValueError, TypeError):
        return str(ts)
    return time.strftime(LOCAL_TIME_FORMAT, time.localtime(epoch))


def first_event_ts(run_root: Path) -> str | None:
    """`ts` of the first line of `events.jsonl`, read as ONE line -- never
    the whole file (a long run's events file is megabytes). None when the
    file is missing/empty/unparseable or the line carries no `ts`."""
    try:
        with open(Path(run_root) / "events.jsonl", "rb") as fh:
            line = fh.readline()
        ts = json.loads(line.decode("utf-8")).get("ts") if line.strip() else None
    except (OSError, ValueError, AttributeError):
        return None
    return ts if isinstance(ts, str) and ts else None


def run_timing(status: dict, run_root: Path | None = None, *,
               end_ts: str | None = None) -> dict:
    """The ONE shared header/status timing helper (#291): the hub's run header
    and `ralphctl status` both render what this returns.

    * `startedAt` -- the ORIGINAL start: `status.firstStartedAt`, else (a run
      recorded before that field existed) the `ts` of the first line of the
      run's `events.jsonl`, else the current episode's `startedAt`.
    * `resumedAt` -- the current episode's `startedAt`, present only when the
      run has more than one episode (`episodes`).
    * `wallSeconds` -- `end_ts` (the caller passes `endedAt` for a finished
      run; falsy means now) minus the original start.
    * `activeSeconds` -- every episode's own wall clock summed (each
      `previousEndings` entry's `startedAt`..`endedAt`, plus the current
      episode's `startedAt`..end), minus `pausedTotalS`. OMITTED, never
      guessed, when any episode's start (or a finished episode's end) is
      unknown.
    Every value is optional: a status with nothing to measure yields `{}`-ish
    output with no wall/active keys.
    """
    status = status if isinstance(status, dict) else {}
    current = status.get("startedAt") or None
    original = status.get("firstStartedAt") or None
    if not original and run_root is not None:
        original = first_event_ts(run_root)
    original = original or current
    endings = [e for e in (status.get("previousEndings") or [])
               if isinstance(e, dict)]
    episodes = len(endings) + 1 if (original or current) else 0
    out: dict = {"episodes": episodes}
    if original:
        out["startedAt"] = original
    if episodes > 1 and current:
        out["resumedAt"] = current
    try:
        if original:
            out["wallSeconds"] = elapsed_seconds(original, end_ts)
        spans = []
        for entry in endings:
            if not (entry.get("startedAt") and entry.get("endedAt")):
                raise ValueError("episode start unknown")
            spans.append(elapsed_seconds(entry["startedAt"], entry["endedAt"]))
        if not current:
            raise ValueError("current episode start unknown")
        spans.append(elapsed_seconds(current, end_ts))
        paused = status.get("pausedTotalS")
        paused = float(paused) if isinstance(paused, (int, float)) \
            and not isinstance(paused, bool) else 0.0
        out["activeSeconds"] = max(0.0, sum(spans) - paused)
    except (ValueError, TypeError):
        out.pop("activeSeconds", None)
    return out


def format_run_timing_lines(timing: dict) -> list[list[str]]:
    """`[label, text]` rows for the four header/status lines -- `started`,
    `resumed` (multi-episode runs only), `wall`, `active` (only when
    computable) -- worded here once so the hub and `ralphctl status` cannot
    describe the same run differently."""
    lines: list[list[str]] = []
    if timing.get("startedAt"):
        lines.append(["started", format_local_time(timing["startedAt"])])
    if timing.get("resumedAt"):
        lines.append(["resumed", format_local_time(timing["resumedAt"])])
    if timing.get("wallSeconds") is not None:
        lines.append(["wall", format_duration(timing["wallSeconds"])])
    if timing.get("activeSeconds") is not None:
        lines.append(["active", format_duration(timing["activeSeconds"])])
    return lines


# Task 051 (#10): the one word every surface uses for "the provider quoted no
# price, so there is no cost figure" -- deliberately not `$0.0000`, which is a
# real (free) price and was exactly the lie #10 reported.
COST_UNAVAILABLE = "unavailable"
COST_PARTIAL_SUFFIX = f"+ (partial, rest {COST_UNAVAILABLE})"
# Task 052 (#10): the one word every surface uses for money computed from the
# host-side pricing map instead of quoted by the provider. Always rendered
# with a `~` on the amount so a derived figure can never be mistaken for a
# provider-reported one, whatever surface it appears on.
COST_DERIVED_WORD = "derived"
# Task 049 (v0.6, steering 001): the token counters that mean "this call was
# billable". Any one of them being non-zero next to a quoted cost of exactly 0
# is the implausible-zero anomaly (see `is_zero_quote`).
COST_TOKEN_KEYS = ("input", "output", "cacheRead", "cacheWrite", "totalTokens")
# The one sentence every surface/log uses for that anomaly.
COST_ZERO_QUOTE_NOTICE = (
    "provider quoted $0 for billable tokens; treated as an unpriced route "
    "(declare its rates, or declare it free, in `pricing:` to settle it)")


def billable_tokens(usage: dict | None) -> int:
    """Total tokens `usage` says were billed, 0 when it says none were.

    Defensive like `format_duration`: junk counters count as zero rather than
    raising, because this feeds a *renderer* (`cost_status`).
    """
    total = 0
    for key in COST_TOKEN_KEYS:
        raw = (usage or {}).get(key)
        if isinstance(raw, bool) or raw is None:
            continue
        try:
            total += int(raw)
        except (TypeError, ValueError):
            continue
    return total


def is_zero_quote(usage: dict | None) -> bool:
    """True when `usage` carries an *implausible* zero cost (task 049, v0.6).

    Live evidence from the v0.6 self-development run: the AIGW/Bedrock route
    reported `costUSD: 0` with `costPriced: true` for 505 628 billed tokens,
    and ralphd printed `$0.00, 506k tokens`. A zero money quote next to
    non-zero billable tokens is not a price -- it is a missing price wearing a
    quote's clothes (pi zero-fills its `cost` block when the resolved model
    definition has no rates: `artifacts/reports/pricing-anomaly.md` §4). So it
    is classified as *unknown*, never as priced and never rendered `$0.00`.

    Two shapes stay honest zeros, and neither is *inferred* from the zero:

    * nothing was billed (`billable_tokens == 0`) -- the historical int-0
      no-traffic sentinel of #10, byte-for-byte unchanged;
    * the route DECLARED itself free, recorded as `costFree: true` by
      `runner._accumulate_cost` from the operator's `pricing.free` patterns
      (`engine/pricing.PricingMap.is_free`). "Free" is always a declaration in
      the data, so a pure formatter can honour it without reading config.
    """
    if not usage or usage.get("costFree") is True:
        return False
    amount = usage.get("costUSD")
    if isinstance(amount, bool) or not isinstance(amount, (int, float)):
        return False
    if amount != 0:
        return False
    return billable_tokens(usage) > 0


def cost_status(usage: dict | None) -> str | None:
    """How much of `usage`'s cost is actually known: None (fully priced, or
    nothing billed), `"derived"`, `"partial"` or `"unknown"`.

    Accepts BOTH published shapes so one formatter can serve every surface:

    * a *bucket* (status.json `usage`, `byPhase[p]`, `byApproach[a]`) carries
      the merged verdict as `costStatus` (task 050, `loop._merge_cost_status`);
    * a payload of EITHER shape recorded by a pre-v0.6 engine (or by a
      provider that quotes zero) may carry an implausible zero -- `costUSD: 0`
      with billable tokens -- which is `"unknown"` however it was marked
      (task 049, v0.6; see `is_zero_quote`);
    * a single *iteration*'s usage carries task 049's `costPriced` marker
      instead -- `False` means tokens were billed that the provider quoted no
      price for, which is `"derived"` when the host-side pricing map covered
      every one of them (task 052's `costDerived: true`), `"partial"` when the
      iteration also collected a real (float) `costUSD` and `"unknown"` when
      it never did.
    """
    if not usage:
        return None
    status = usage.get("costStatus")
    if status in ("partial", "unknown", "derived"):
        return status
    zero_quote = is_zero_quote(usage)
    if usage.get("costPriced") is False or zero_quote:
        if usage.get("costDerived") is True:
            return "derived"
        # An implausible zero is not money, so it can never make a bucket
        # "partial" -- otherwise the $0 lie would come back as `$0.00+`.
        known = ((isinstance(usage.get("costUSD"), float) and not zero_quote)
                 or isinstance(usage.get("costDerivedUSD"), float))
        return "partial" if known else "unknown"
    if isinstance(usage.get("costDerivedUSD"), float):
        return "derived"
    return None


def format_money(amount: float, decimals: int | None) -> str:
    """`$1.2300`-style money, the one spelling every cost surface shares."""
    return f"${amount}" if decimals is None else f"${amount:.{decimals}f}"


def format_cost(usage: dict | None, decimals: int | None = 2) -> str | None:
    """Render `usage`'s cost for humans, or None when there is nothing to say.

    The ONE shared cost formatter (task 051, #10): `ralphctl status`, the
    `ralphctl logs` iteration footer, the hub's usage card/tables (via
    `ui_server`, which ships the formatted string next to the raw numbers,
    the same pattern as `format_local_time`) and any future `ralphctl watch`
    TUI cost gauge all go through it, so "we don't know what this cost" is
    worded identically everywhere and can never render as `$0.0000` again:

    * fully priced (or nothing billed) -> `"$0.56"`, byte-for-byte what each
      surface printed before this task (`decimals=None` keeps the logs
      footer's raw `str(float)` rendering);
    * `"partial"` -> `"$0.56+ (partial, rest unavailable)"`: the priced
      subtotal is a lower bound, never presented as the total;
    * `"unknown"` -> `"unavailable"`, with no number at all;
    * `"derived"` (task 052) -> `"~$0.45 derived"`, or `"$0.56 + ~$0.45
      derived"` when provider-quoted and host-derived money are both present:
      the two are shown as separate amounts, never one sum, so a derived
      figure is never mistaken for what the provider actually billed;
    * no cost information whatsoever -> None, so the caller keeps its own
      "omit the field" / legacy `$0.00` behaviour.
    """
    status = cost_status(usage)
    amount = (usage or {}).get("costUSD")
    derived = (usage or {}).get("costDerivedUSD")
    if status == "unknown" or (status == "partial" and amount is None
                               and derived is None):
        return COST_UNAVAILABLE
    if isinstance(derived, float):
        # provider-quoted part first (only when a price was actually QUOTED --
        # a float `costUSD`, per loop._has_reported_price; the int `0` a
        # no-traffic iteration contributes is not a quote, and neither is
        # task 049's implausible zero), derived part marked
        parts = ([format_money(amount, decimals)]
                 if isinstance(amount, float) and not is_zero_quote(usage) else [])
        parts.append(f"~{format_money(derived, decimals)} {COST_DERIVED_WORD}")
        rendered = " + ".join(parts)
        return (f"{rendered}, partial (rest {COST_UNAVAILABLE})"
                if status == "partial" else rendered)
    if amount is None:
        return None
    rendered = format_money(amount, decimals)
    return rendered + COST_PARTIAL_SUFFIX if status == "partial" else rendered


def atomic_write(path: Path, data: str) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(data)
    os.replace(tmp, path)


def atomic_write_json(path: Path, obj: Any) -> None:
    atomic_write(path, json.dumps(obj, indent=2) + "\n")


def read_json(path: Path, default: Any = None) -> Any:
    try:
        return json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return default


# Task 024 (#8): THE set of recorded `state` values that mean "a live engine
# still owns this run". Lives here, next to the other state-contract
# vocabulary, because three surfaces have to agree on it: the engine, the
# host-side CLI (`ralphctl status`/`doctor`/`repair`'s zombie condition) and
# the hub server (a run recorded in one of these states whose API does not
# answer is a dead run, not a healthy one). A second copy is how those
# surfaces start disagreeing about which runs are zombies.
#
# Task 012 (#56) adds "finishing": once the job's verdict is settled but
# `reflect: true` still has its post-terminal iteration to run, the run is
# neither actively working the plan (the ordinary meaning "running" carries
# everywhere -- deletion, `rm`, the hub's live pill) nor safely inert (a
# terminal state is the ONE gate every destructive action opens on). It IS
# still "a live engine owns this run" for liveness/zombie purposes -- an
# engine killed mid-reflect leaves a dangling run exactly like one killed
# mid-worker, and belongs in this tuple for that reason alone. See
# LoopSupervisor._finish()/_promote_finishing_state() (engine/loop.py) for
# how status.json carries the settled verdict and the pending terminal state
# across the window, and how a resumed process promotes it without redoing
# any of the job.
#
# Task 036 (v0.18 requirement D.1/D.2, #142) adds "paused": when the ENGINE
# pauses itself on an escalation edge (`rejected_last_approach` -- the last
# approach was rejected on findings that are all curable with no approach
# budget left to cure them in), the pause is not a passing hiccup an operator
# will release in seconds: the run sits there until a human records a ruling,
# and that has to be observable from the HOST (`ralphctl status`, the hub, a
# plain read of status.json) rather than only inferrable from a `log` event in
# events.jsonl. It is non-terminal for exactly the reason `finishing` is: a
# live engine still owns the run (`self._pause` is an in-process
# `asyncio.Event`), an engine killed while parked leaves a dangling run, and
# no destructive action may open on it. An OPERATOR pause (`POST /pause`)
# still records no state at all -- see `LoopSupervisor.pause`.
PAUSED_STATE = "paused"
# Task 008 (v0.24 requirement B.1, #169): `timed_out` -- the deadline hold,
# entered when a run's job deadline expires (or a rejecting verdict holds
# it, D.2) and the current iteration/hat call has finished. A live engine
# still owns the run's on-disk position (task/iteration to resume from), so
# it belongs beside `paused`/`finishing` here, never in `TERMINAL_STATES`
# below -- see `engine.transitions.TIMED_OUT_STATE`/`ALL_STATES` for the
# same state declared in the transition table, and `cli/web/app.js`'s
# `RUN_STATE_ORDER` for the third spelling this PRD requirement names.
TIMED_OUT_STATE = "timed_out"
NONTERMINAL_STATES = ("starting", "running", PAUSED_STATE, "finishing",
                      TIMED_OUT_STATE)

# v0.23 requirement A.7, resolved tension 3 (task 017, #224): the ONE shared
# marker text `ralphctl runs` and the hub run list both render for a run
# parked with `pauseReason: "operator_input_needed"` -- the ONE reason
# whose own bare `ralphctl unpause`, with no ruling ever filed, re-enters
# `review` over the EXACT SAME approach with nothing new to go on, so it
# is likely to re-raise the identical rejection rather than progress
# (cure task cure-01-01-9, PRD A.1/A.6, D.1). Every OTHER `paused` reason
# that also waits on a ruling (`rejected_last_approach`, `rejected_
# incurable`, `stagnation`) still has a real, distinct default disposition
# a bare unpause resolves into (accept the rejection as final, finish the
# run, or simply continue past the stagnation) -- their own escalation
# text names it -- while the dependency/review-contract reasons resolve
# entirely on their own (a `tasks.json` edit) and need no ruling at all.
# One spelling here so the two
# surfaces (`cmd_runs`, `ui_server.run_list` -> `app.js`) can never drift.
OPERATOR_INPUT_NEEDED_PAUSE_REASON = "operator_input_needed"
PARKED_FOR_INPUT_MARKER = "\u23F8 parked for input"


def format_pause_marker(pause_reason) -> str:
    """`PARKED_FOR_INPUT_MARKER` for a run parked on
    `OPERATOR_INPUT_NEEDED_PAUSE_REASON`, `""` for anything else (including
    every other `paused` reason, and a non-`paused` run whose `pauseReason`
    is absent/`null`) -- task 017 (PRD requirement A.7, #224).
    """
    return (PARKED_FOR_INPUT_MARKER
            if pause_reason == OPERATOR_INPUT_NEEDED_PAUSE_REASON else "")


# Task 005 (PRD requirement A.5, #269): the ONE shared marker text
# `ralphctl status`, the hub's runs-list row and the hub's run page all
# render for a run whose most recent operator escalation NEVER reached the
# operator at all (every retry in `LoopSupervisor._notify_escalation_
# delivery` failed) and which is still sitting on that exact escalation --
# `format_pause_marker` above is the sibling for the ONE reason a bare
# `unpause` re-raises rather than actually answering; this is the sibling
# for the delivery FAILING, orthogonal to which reason paused the run.
#
# The literal string mirrors `engine.run_record.NOTIFICATION_ESCALATION_
# UNDELIVERED_EVENT` -- never imported here, since `run_record` imports
# THIS module (`from . import state`) and importing back would cycle.
_NOTIFICATION_ESCALATION_UNDELIVERED_EVENT = "notification.escalation_undelivered"
ESCALATION_UNDELIVERED_MARKER = "\u2757 escalation undelivered"


# The ONE escalation site whose pause is never persisted to status.json:
# `LoopSupervisor._pause_and_escalate_incurable_rejection` takes the
# reason-less, in-memory `self.pause()` (SPEC.md 4.6), so `state` keeps
# reading `running` while the run is in fact held. Its hold is derived from
# `events.jsonl` instead -- see `escalation_pause_markers` below -- and gets
# its own marker, since `format_pause_marker` has no `pauseReason` to read.
IN_MEMORY_ESCALATION_PAUSE_REASON = "rejected_incurable"
ESCALATION_HOLD_MARKER = "\u23F8 held on rejected_incurable escalation"

# The recorded states an in-memory escalation hold can sit under: the run is
# still owned by a live (or dead-but-unrestarted) engine and was not moved to
# a persisted pause, a wind-down or a terminal state since. `None` is a
# status.json that has not recorded a `state` yet (an engine driven straight
# into its review step, as the test harness does) -- the hold is just as
# real there.
_IN_MEMORY_HOLD_STATES = (None, "starting", "running")
# The `log` event `LoopSupervisor.resume()` emits -- the one record every
# answer to a pause leaves on `events.jsonl`, persisted or in-memory.
RESUMED_LOG_MESSAGE = "resumed"


def _escalation_scan(run_root: Path) -> dict:
    """One pass over `events.jsonl` for the latest `operator_escalation`:
    its `reason`, whether `notification.escalation_undelivered` followed it
    (with no later escalation since), and whether the hold it took was
    answered since -- an `iteration.start` (the loop ran again: an in-process
    resume got past the gate, or a restarted episode re-entered review) or
    the `log` event `LoopSupervisor.resume()` emits (`"resumed"`: `ralphctl
    unpause`, `POST /resume`, a ruling that releases the pause, and the
    abort wind-down all go through it)."""
    reason = None
    undelivered = False
    answered = False
    for ev in read_events(run_root, types=(
            "operator_escalation",
            _NOTIFICATION_ESCALATION_UNDELIVERED_EVENT,
            "iteration.start", "log")):
        kind = ev.get("type")
        if kind == "operator_escalation":
            reason = ev.get("reason")
            undelivered = False
            answered = False
        elif kind == _NOTIFICATION_ESCALATION_UNDELIVERED_EVENT:
            undelivered = True
        elif kind == "iteration.start" or (
                kind == "log" and ev.get("message") == RESUMED_LOG_MESSAGE):
            # An answered hold's undelivered escalation is history: only a
            # `notification.escalation_undelivered` recorded AFTER this
            # (a later pause's own exhausted delivery) re-arms the flag.
            answered = True
            undelivered = False
    return {"reason": reason, "undelivered": undelivered, "answered": answered}


def escalation_pause_markers(run_root: Path, status: dict) -> dict:
    """The three facts `ralphctl status`, the hub's runs-list row and the
    hub's run page carry side by side (task 005, PRD requirement A.5,
    #269), from ONE read of `events.jsonl`:

    * `pauseMarker` -- `format_pause_marker(pauseReason)` for a persisted
      pause; for the in-memory `rejected_incurable` hold (state still
      `running`/`starting`, latest `operator_escalation` has that reason,
      not answered since -- see `_escalation_scan`) it is
      `ESCALATION_HOLD_MARKER`, so the hold has a marker at all;
    * `escalationUndelivered` -- True iff the run is still paused on its
      latest escalation (a persisted `state == PAUSED_STATE`, or the
      in-memory hold above) AND that escalation was followed, after every
      delivery retry, by `notification.escalation_undelivered`, with no
      later `operator_escalation` since;
    * `escalationUndeliveredMarker` -- `ESCALATION_UNDELIVERED_MARKER` when
      the above holds, `""` otherwise.

    Read straight off `events.jsonl`/`status` -- no live engine required,
    so a dead run's on-disk snapshot answers this exactly like a live one.
    Answering the pause removes the marker at once: `resume()` clears a
    persisted `state`/`pauseReason` synchronously and emits its `"resumed"`
    log event, which ends the in-memory hold's derivation too.
    """
    pause_marker = format_pause_marker(status.get("pauseReason"))
    state_value = status.get("state")
    scan = None
    held = False
    if state_value == PAUSED_STATE:
        scan = _escalation_scan(run_root)
        held = True
    elif state_value in _IN_MEMORY_HOLD_STATES:
        scan = _escalation_scan(run_root)
        held = (scan["reason"] == IN_MEMORY_ESCALATION_PAUSE_REASON
                and not scan["answered"])
        if held and not pause_marker:
            pause_marker = ESCALATION_HOLD_MARKER
    undelivered = bool(held and scan and scan["undelivered"])
    return {
        "pauseMarker": pause_marker,
        "escalationUndelivered": undelivered,
        "escalationUndeliveredMarker": (
            ESCALATION_UNDELIVERED_MARKER if undelivered else ""),
    }


def escalation_undelivered(run_root: Path, status: dict) -> bool:
    """`escalation_pause_markers(...)["escalationUndelivered"]` -- True iff
    the run is still paused on its latest `operator_escalation` (persisted,
    or the in-memory `rejected_incurable` hold) and that escalation went
    undelivered after every retry."""
    return escalation_pause_markers(run_root, status)["escalationUndelivered"]


def format_escalation_undelivered_marker(run_root: Path, status: dict) -> str:
    """`ESCALATION_UNDELIVERED_MARKER` when `escalation_undelivered` holds,
    `""` otherwise -- the render-ready twin `format_pause_marker` above is,
    for the same reason: callers print/attach a string, never a bool.
    """
    return escalation_pause_markers(run_root, status)[
        "escalationUndeliveredMarker"]


# Task 007 (v0.26, #269): the two `pauseReason` values an operator RULING can
# actually answer -- `ralphctl rule <run> --file <ruling.md> --name <slug>`
# or a plain `ralphctl unpause <run>` -- as opposed to a plain operator pause
# (no `pauseReason` at all) or a pause a ruling has no vocabulary for
# (`stagnation`, `rejected_last_approach`, and the other five reasons the
# `operator_escalation` docs list). `parked_question` below returns `None`
# for every OTHER pause reason, this pair being the only two it shapes.
PARKED_QUESTION_REASONS = ("operator_input_needed", "rejected_repeated_signature")

# The three answers `parked_question`'s own `consequence.
# nextStepIfUnanswered` can report -- "if this pause is resumed with no
# ruling at all, does the run take a cure pass over the SAME approach's
# blocking findings, archive this approach and replan a fresh one, or
# archive this approach and END THE RUN because no approach budget is
# left?" Derived, per pause, from `LoopSupervisor._review_cure_pass_
# available(approach)` (cure pass vs. the other two) and `replan_
# available`/`approach < cfg.max_approaches` (replan vs. finish) at the
# moment the pause is raised (never guessed here from the findings list
# alone, since curability, the run's own cure-pass ceiling, this
# approach's already-taken passes and the approach budget all feed that
# decision) -- see `_pause_and_escalate_operator_input_needed`/
# `_pause_and_escalate_repeated_rejection_signature`/`_pause_and_escalate_
# gate_operator_input_needed` in `engine/loop.py`, the only three writers
# of `nextStepIfUnanswered` on the `operator_escalation` event.
#
# Cure task cure-01-05 (PRD v0.27 requirement C.4, DoD 3): before this
# task every pause that could not take a cure pass named `"archive_and_
# replan"` unconditionally, even on a run's LAST approach, where the
# actual edge an unanswered pause takes is `_step_review`/`_step_verdict_
# gate`'s own `rejected_no_approach_left`/`verdict_gate_rejected_no_
# approach_left` tail (`("tail",)`, ending the run) -- archiving the
# approach without ever replanning a fresh one. `NEXT_STEP_FINISH` is
# that third, distinct outcome.
NEXT_STEP_CURE_PASS = "cure_pass"
NEXT_STEP_ARCHIVE_AND_REPLAN = "archive_and_replan"
NEXT_STEP_FINISH = "finishing"


def parked_question(run_root: Path, status: dict) -> dict | None:
    """Task 007 (v0.26, #269): the hub run-detail payload's `parkedQuestion`
    object -- a ruling-answerable pause shaped from the latest matching
    `operator_escalation` event, or `None` when there is nothing a ruling
    could answer right now.

    `None` for:
      * a running run (`status["pauseReason"]` absent/`null` -- the ordinary
        case, including a run never paused at all and a plain operator
        `POST /pause`, which persists no `pauseReason`);
      * every pause reason outside `PARKED_QUESTION_REASONS` above
        (`stagnation`, `rejected_last_approach`, `review_contract_
        unrepaired`, `review_findings_unusable`, `rejected_incurable`'s
        in-memory hold, the two dependency-edge reasons) -- a plain
        operator pause and these seven all hold the run for a reason a
        ruling has no vocabulary to answer, so the panel this feeds must
        not render for them either;
      * a `pauseReason` in that set with no matching `operator_escalation`
        event on record at all (defensive only -- should not happen for a
        persisted pause, which is always preceded by the event that
        caused it).

    Cure task cure-01-02-4 (PRD B.1, D.1; DoD 2/4): the LATEST matching-
    reason event is not enough on its own -- the task-boundary commit-
    secret-scan hold (`_pause_for_commit_secret_hit`, task 030) persists
    `pauseReason: "operator_input_needed"` on status.json WITHOUT ever
    emitting a fresh `operator_escalation` event (see that method's own
    docstring: its record is the value-free `commit_secret_scan` event,
    deliberately not this one, so a crashed-episode resume never misroutes
    into `review` over nothing it reviewed). If review had earlier raised
    and ANSWERED a same-reason `operator_input_needed` pause (a ruling or a
    plain `POST /resume`) on this same run, that earlier event is still the
    latest one carrying this reason -- and, unpatched, it would be handed
    back out as if it were the CURRENT commit-scan hold's own question,
    resurrecting an already-answered finding list and cure preview the
    operator has no reason to see again. So the scan below tracks, for the
    matching-reason event only, whether an `iteration.start` or a `resume()`
    `"resumed"` log line (the same answered signal `_escalation_scan` above
    uses) followed it with no LATER matching-reason escalation to re-arm the
    question: if so, that event is stale -- answered, then re-paused for an
    unrelated reason that merely reuses the same reason string -- and this
    returns `None` rather than the old event. A genuinely fresh escalation
    (any pause, including a repeat of the SAME reason, raised after the
    last resume) resets the tracking and is reported normally.

    Otherwise, built from the LATEST `operator_escalation` event whose own
    `reason` equals `status["pauseReason"]`, not yet answered by a later
    resume with no fresher matching escalation since (read oldest-first off
    `events.jsonl`, keeping the last unanswered match -- the same one-pass-
    and-keep-last discipline `_escalation_scan` above already uses): `reason`,
    `approach`, `maxApproaches` (`status["maxApproaches"]`, `None` when
    absent), `approachOfMax` (the event's own pre-rendered `"approach <n>
    of <N>"` text), `parkedAt` (the event's own `ts` -- the moment the
    pause was recorded, never re-derived from `status.json`, which carries
    no timestamp of its own for this), `replanAvailable` (the event's own
    field, `None` when absent -- `rejected_repeated_signature` never
    carries it), `blockingCount`/`residualCount` and `findings` (EVERY
    finding, blocking and residual alike: first each blocking finding, in
    the SAME order the escalation text and its `ralphctl amend`
    invocations enumerate them, tagged `id: "F<n>"`, 1-based; then each
    residual finding from the on-disk sidecar, in the sidecar's own order,
    tagged `id: "R<n>"`, 1-based -- a separate prefix so a residual never
    shifts or collides with the `F<n>` ids the escalation text and
    `ralphctl amend` already name; every entry carries `id`, `severity`,
    `clause`, the whole `evidence`, `cured_when` and `invalidates`), and
    `consequence`
    (`taskIdsValidatedToPending`, `insertCount`, `costSpentThisApproach`,
    `tokensSpentThisApproach`, `nextStepIfUnanswered`).

    The residual entries and `residualCount` are read straight off the
    on-disk structured findings sidecar (`FINDINGS_JSON_FILE`) rather than
    the event (which never carries residual findings at all, only the
    blocking set review acted on) -- a missing, empty or unreadable sidecar reads as `0`, matching
    `_review_findings_channel`'s own no-channel default of "nothing to
    count instead of raising".
    """
    reason = status.get("pauseReason")
    if reason not in PARKED_QUESTION_REASONS:
        return None
    event = None
    answered_since = False
    for ev in read_events(run_root, types=(
            "operator_escalation", "iteration.start", "log")):
        kind = ev.get("type")
        if kind == "operator_escalation":
            if ev.get("reason") == reason:
                event = ev
                answered_since = False
        elif kind == "iteration.start" or (
                kind == "log" and ev.get("message") == RESUMED_LOG_MESSAGE):
            answered_since = True
    if event is None or answered_since:
        return None
    blocking = [f for f in (event.get("findings") or []) if isinstance(f, dict)]
    findings_out = []
    for index, finding in enumerate(blocking, start=1):
        findings_out.append({
            "id": f"F{index}",
            "severity": finding.get("severity", "blocking"),
            "clause": finding.get("clause"),
            "evidence": finding.get("evidence"),
            "cured_when": finding.get("cured_when"),
            "invalidates": finding.get("invalidates") or [],
        })
    # An `operator_escalation` event written before every ruling-answerable
    # pause carried the A.7 fields unconditionally lacks `approachOfMax` and
    # the spend pair; derive them from the event's approach and
    # `status.json`'s own `usage.byApproach` bucket (a parked run spends
    # nothing while parked, so the bucket still reads the at-park figure).
    approach = event.get("approach")
    max_approaches = status.get("maxApproaches")
    approach_of_max = event.get("approachOfMax")
    if approach_of_max is None and approach is not None and max_approaches:
        approach_of_max = f"approach {approach} of {max_approaches}"
    cost_text = event.get("costSpentThisApproach")
    tokens = event.get("tokensSpentThisApproach")
    if "costSpentThisApproach" not in event and approach is not None:
        by_approach = (status.get("usage") or {}).get("byApproach") or {}
        bucket = by_approach.get(str(approach)) or {}
        if bucket:
            cost_text = format_cost(bucket)
            tokens = bucket.get("totalTokens")
    raw_findings = read_json(Path(run_root) / FINDINGS_JSON_FILE, [])
    residual = []
    if isinstance(raw_findings, list):
        residual = [f for f in raw_findings
                    if isinstance(f, dict) and f.get("severity") == "residual"]
    blocking_count = len(findings_out)
    for index, finding in enumerate(residual, start=1):
        findings_out.append({
            "id": f"R{index}",
            "severity": "residual",
            "clause": finding.get("clause"),
            "evidence": finding.get("evidence"),
            "cured_when": finding.get("cured_when"),
            "invalidates": finding.get("invalidates") or [],
        })
    residual_count = len(residual)
    return {
        "reason": reason,
        "approach": approach,
        "maxApproaches": max_approaches,
        "approachOfMax": approach_of_max,
        "parkedAt": event.get("ts"),
        "replanAvailable": event.get("replanAvailable"),
        "blockingCount": blocking_count,
        "residualCount": residual_count,
        "findings": findings_out,
        "consequence": {
            "taskIdsValidatedToPending": list(
                event.get("taskIdsValidatedToPending") or []),
            "insertCount": event.get("insertCount", 0),
            "costSpentThisApproach": cost_text,
            "tokensSpentThisApproach": tokens,
            "nextStepIfUnanswered": event.get("nextStepIfUnanswered"),
        },
    }


# Task 030 (#19): its complement -- the recorded `state` values that mean "no
# engine owns this run any more", i.e. the ONE gate a destructive action may
# open on. Lives here beside `NONTERMINAL_STATES` for the same reason: three
# surfaces have to agree (`ralphctl stop`/`rm --force` and the hub's delete
# endpoint), and neither of them may treat an absent, unreadable or
# unrecognized state as permission -- membership in this tuple is the whole
# test, so `unknown` can never accidentally pass it.
TERMINAL_STATES = ("succeeded", "failed", "aborted")


# Task 029 (#8): the run-dir marker recording that this run's termination was
# *operator-initiated* (`ralphctl abort`, POST /abort from the hub or curl,
# `ralphctl stop`) rather than a container that vanished on its own.
#
# Its whole reason to exist is auto-resume: a deliberately stopped run and a
# crashed one can look identical on disk (`ralphctl stop --force` removes the
# container, and an abort whose container dies before the engine writes its
# terminal state leaves `state: running` behind), and resurrecting a run the
# operator just killed is the worst possible failure mode for self-recovery.
#
# A separate small file rather than a status.json field on purpose: both the
# engine (inside the container) and the host-side CLI have to be able to write
# it, and status.json is the engine's read-patch-write document -- a host-side
# patch would race the engine and could be silently clobbered.
OPERATOR_TERMINATION_FILE = "operator-termination.json"

# Task 015 (#46): WHICH termination this marker records. Until v0.7 the file's
# mere existence meant "an operator did this", and `LoopSupervisor.abort()`
# wrote it for *every* way a run can be told to stop -- including the engine's
# own SIGTERM/SIGINT handler. So a run shot from inside its own container (the
# agent's `pkill -f ralphd-engine`, an OOM-adjacent `docker stop` of the job
# from a sibling it started) recorded `{"action": "abort", "reason":
# "signal 15", "source": "engine"}` and became permanently unrecoverable: the
# never-resurrect guarantee below is exactly right for a run the operator
# killed and exactly wrong for one killed by accident from inside, which is
# the case where resuming is the whole point of auto-resume.
#
# What the engine can and cannot establish, honestly: a process cannot see who
# sent it a signal (no sender uid/pid is available to an asyncio signal
# handler), so the class is decided by ATTRIBUTION, not by provenance -- an
# abort somebody claimed through the API (`POST /abort`, i.e. `ralphctl abort`
# and `ralphctl stop`, and the hub's abort button) is `operator`; a bare signal
# nobody claimed is `self-inflicted`. `ralphctl stop --force`/`rm --force`
# reach the same verdict from the other side: they POST /abort first and then
# overwrite this marker host-side (source `cli`), so the operator's intent wins
# whichever half of the race lands last. Residual, documented in docs/cli.md:
# a raw `docker stop` of a job container by an operator who bypassed ralphctl
# entirely is indistinguishable from a self-inflicted signal and is therefore
# resumable -- the deliberate trade, since the alternative (the pre-v0.7
# reading) permanently strands every accidental self-kill.
TERMINATION_CLASS_OPERATOR = "operator"
TERMINATION_CLASS_SELF = "self-inflicted"
TERMINATION_CLASSES: tuple[str, ...] = (TERMINATION_CLASS_OPERATOR,
                                        TERMINATION_CLASS_SELF)

# v0.21 requirement A.4 (#192, task 004): status.json's `gateRejectionEnding`
# -- which of the three endings the peer verdict gate's MOST RECENT rejection
# was, so an operator reading `ralphctl status` (`cli.main._format_gate_
# rejection_ending_lines`) can tell a rejection that was never curable
# (`MERITS`) apart from one where a cure pass was actually taken
# (`CURE_TAKEN`, `LoopSupervisor._take_gate_cure_pass`) apart from one that
# WOULD have been cured but found the run's own cure-pass ceiling
# (`JobConfig.cure_pass_ceiling`, `curePasses`/`gateCurePasses` above) already
# spent (`CURE_UNAVAILABLE`) -- the exact ambiguity v0.20's #192 left an
# operator with no way to resolve. Written by `LoopSupervisor.
# _step_verdict_gate` on every gate rejection (`_gate_rejection_ending`),
# before the cure/replan/tail edge that rejection takes; overwritten by each
# later gate rejection; absent for a run that has never had one.
GATE_REJECTION_ENDING_MERITS = "rejected_on_the_merits"
GATE_REJECTION_ENDING_CURE_TAKEN = "rejected_cure_taken"
GATE_REJECTION_ENDING_CURE_UNAVAILABLE = "rejected_cure_unavailable"
GATE_REJECTION_ENDINGS: tuple[str, ...] = (
    GATE_REJECTION_ENDING_MERITS,
    GATE_REJECTION_ENDING_CURE_TAKEN,
    GATE_REJECTION_ENDING_CURE_UNAVAILABLE,
)
# How much of a tool call's arguments the evidence keeps, and how much of a
# transcript's tail is scanned for it: a transcript can be tens of MB, and
# the last tool call is by construction at its end.
TERMINATION_EVIDENCE_ARGS_MAX = 200
TERMINATION_EVIDENCE_TAIL_BYTES = 256 * 1024


def record_operator_termination(run_root: Path, action: str,
                                reason: str = "", source: str = "",
                                termination_class: str =
                                TERMINATION_CLASS_OPERATOR,
                                evidence: dict | None = None) -> dict:
    """Write OPERATOR_TERMINATION_FILE into a run dir. `action` is the
    operator verb ("abort"/"stop"), `source` says who recorded it
    ("engine"/"cli"). Idempotent: a later record overwrites an earlier one
    (the most recent operator intent is the one that counts). Missing run
    dir -> no-op, so callers never have to guard.

    Task 015 (#46): `termination_class` is one of TERMINATION_CLASSES and
    defaults to `operator`, so every pre-existing caller keeps its exact
    meaning; `evidence` is the optional dict a self-inflicted termination
    carries (see `last_tool_call`). The class is stored under `"class"` and
    read back through `termination_class()`, which is the ONE place the
    back-compat reading of a marker without one lives.
    """
    doc = {"action": action, "at": utcnow(),
           "reason": reason or "", "source": source or "",
           "class": termination_class or TERMINATION_CLASS_OPERATOR}
    if evidence:
        doc["evidence"] = evidence
    if not run_root.is_dir():
        return doc
    atomic_write_json(run_root / OPERATOR_TERMINATION_FILE, doc)
    return doc


# Task 005 (#173): where a superseded episode's operator-termination marker
# is archived on resume -- see archive_operator_termination() below.
PREVIOUS_ENDINGS_DIR = "previous-endings"


class _RemoveStatusKey:
    """Type of REMOVE_STATUS_KEY below; instantiated exactly once."""

    def __repr__(self) -> str:      # pragma: no cover - diagnostics only
        return "REMOVE_STATUS_KEY"


# Task 005 (#173): pass this as a value to `RunDir.update_status()` to REMOVE
# that key from status.json instead of writing it. `None` is not good enough
# for `termination`: a run stopped by an operator and then resumed into a
# clean success would still carry `"termination": null`, and a consumer that
# only asks whether the key is present (a `--json` reader, the hub, a future
# classifier) cannot tell that from "this episode was told to stop". Absence
# is the only honest encoding of "never told to stop".
REMOVE_STATUS_KEY = _RemoveStatusKey()


def archive_operator_termination(run_root: Path, index: int) -> Path | None:
    """Move OPERATOR_TERMINATION_FILE out of the run dir root into
    `previous-endings/NN-operator-termination.json` (task 005, #173).

    The file (and status.json's `termination` field, cleared by the caller
    in the same episode-opening write) describes the episode that just
    ended -- once a resume moves the run into a fresh episode, the marker at
    the root is stale, and a `--json` consumer, the hub or any future
    classifier reading it sees an aborted run that may since have succeeded
    (the exact symptom #173 reports). `index` is the 1-based position of
    the matching `previousEndings` entry, zero-padded to 2 digits, so the
    archived name lines up with the entry that now carries the same record
    under `termination`.

    No-op (returns None) when there is nothing to archive -- a clean
    episode never wrote the file at all. `shutil.move` is used rather than
    a copy-then-delete: the root file must not be readable as current once
    this returns.
    """
    src = run_root / OPERATOR_TERMINATION_FILE
    if not src.is_file():
        return None
    dest_dir = run_root / PREVIOUS_ENDINGS_DIR
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"{index:02d}-{OPERATOR_TERMINATION_FILE}"
    shutil.move(str(src), str(dest))
    return dest


def read_operator_termination(run_root: Path) -> dict | None:
    """The recorded operator-initiated termination for a run dir, or None.
    Malformed/absent -> None (never raises: every caller is diagnostic)."""
    doc = read_json(run_root / OPERATOR_TERMINATION_FILE, None)
    if not isinstance(doc, dict) or not doc.get("action"):
        return None
    return doc


def termination_class(doc: dict | None) -> str | None:
    """Which class a termination marker records (task 015, #46): one of
    TERMINATION_CLASSES, or None for no marker at all.

    A marker with no `class` field -- every marker written before v0.7 -- reads
    as `operator`, and so does any unrecognized value. That default is the safe
    one in the only direction that matters: it can only ever *refuse* to
    resurrect a run, never resurrect one the operator killed.
    """
    if not isinstance(doc, dict) or not doc.get("action"):
        return None
    cls = doc.get("class")
    if cls not in TERMINATION_CLASSES:
        return TERMINATION_CLASS_OPERATOR
    return cls


def is_operator_termination(doc: dict | None) -> bool:
    """True when this marker records a termination an operator asked for --
    THE question auto-resume's never-resurrect refusal asks (task 015, #46).
    False for no marker and for a self-inflicted one, which stays eligible for
    recovery."""
    return termination_class(doc) == TERMINATION_CLASS_OPERATOR


def _transcript_tail_lines(path: Path) -> list[str]:
    """The last TERMINATION_EVIDENCE_TAIL_BYTES of an NDJSON transcript, split
    into whole lines (a partial first line is dropped). Never raises."""
    try:
        size = path.stat().st_size
        with open(path, "rb") as fh:
            if size > TERMINATION_EVIDENCE_TAIL_BYTES:
                fh.seek(size - TERMINATION_EVIDENCE_TAIL_BYTES)
                fh.readline()  # discard the partial line we landed inside
            blob = fh.read()
    except OSError:
        return []
    return blob.decode("utf-8", "replace").splitlines()


def last_tool_call(run_root: Path) -> dict | None:
    """The most recent tool call this run's transcripts recorded, or None
    (task 015, #46).

    The evidence a self-inflicted termination carries: "signal 15" tells an
    operator nothing, while "the last thing this run did before the signal was
    `bash` with this command line" is usually the whole answer -- the agent's
    own `pkill`/`docker stop`/`kill` is right there in the arguments. It is
    already on disk in the iteration's transcript (`log_merge`'s
    `iteration_output_path`, the one place that file's name is spelled), so this
    reads it rather than asking the runner to remember anything.

    Returns ``{"iteration", "tool", "args", "transcript"}``: the iteration
    number, the tool name, its arguments rendered as JSON and truncated to
    TERMINATION_EVIDENCE_ARGS_MAX chars (and run through `scrub_text`, because
    a tool call's arguments can contain a credential value and this ends up on
    disk in two documents), and the run-relative transcript path the evidence
    came from. Iterations are searched newest-first and only
    `tool_execution_start` events carry arguments (docs/json.md), so this is
    the last *started* call -- which is the one that was in flight when the
    signal arrived. Absent/unreadable/junk transcripts yield None: evidence is
    a nice-to-have, never a reason for the abort path to raise.
    """
    for number in reversed(iteration_numbers(run_root)):
        path = iteration_output_path(run_root, number)
        for line in reversed(_transcript_tail_lines(path)):
            line = line.strip()
            if not line or "tool_execution_start" not in line:
                continue
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if not isinstance(event, dict) or \
                    event.get("type") != "tool_execution_start":
                continue
            args = event.get("args") or event.get("arguments") or {}
            try:
                rendered = json.dumps(args, sort_keys=True)
            except (TypeError, ValueError):
                rendered = str(args)
            rendered = scrub_text(rendered)
            if len(rendered) > TERMINATION_EVIDENCE_ARGS_MAX:
                rendered = rendered[:TERMINATION_EVIDENCE_ARGS_MAX] + "..."
            return {"iteration": number,
                    "tool": str(event.get("toolName") or "?"),
                    "args": rendered,
                    # run-relative, and spelled by the module that owns the
                    # file name rather than a second time here
                    "transcript": str(path.relative_to(run_root))}
    return None


def format_last_tool_call(evidence: dict | None) -> str:
    """One line naming the evidence `last_tool_call` found, for a human-facing
    `reason`/status line (task 015, #46). Empty string when there is none --
    a run killed before its first tool call has nothing to show, and saying
    "unknown" would be noise."""
    if not isinstance(evidence, dict) or not evidence.get("tool"):
        return ""
    return (f"last tool call before the signal: {evidence['tool']}"
            f"({evidence.get('args') or ''}) in iteration "
            f"{evidence.get('iteration')} "
            f"({evidence.get('transcript')})")


# Task 020 (D.5, #125 A): the three path fragments a genuinely BLIND
# verdict_gate iteration must never read -- any of them, anywhere in a tool
# call's rendered arguments, means the hat reached outside the finished
# workspace it is supposed to judge on its own and read another iteration's
# transcript, the run's own event stream, or a raw output transcript
# instead. Spelled once so the detector and its tests never drift on the
# three literals.
# `OUTPUT_FILENAME` (imported above from `..log_merge`, the ONE module
# allowed to spell the transcript file's name -- tests/test_log_merge.py's
# `test_no_duplicate_merge_implementation` grep-guards it) is used here
# instead of the literal string, so this pattern list names the file
# without becoming a second place the literal is spelled.
#
# Task 051 (J.2, J.3, #136/#134): these three are common English/code words
# too -- `events.jsonl` is spelled throughout this very module's own
# docstrings, and a `verdict_gate` iteration legitimately greps `src/` for
# it while reviewing the workspace. A bare substring match over the WHOLE
# rendered call therefore flags that legitimate grep exactly as it flags a
# real peek at the run dir. `detect_verdict_gate_peek` below anchors every
# match to `run_root` itself -- the call's rendered arguments must mention
# the run-state directory's own path for a pattern hit to count at all -- so
# a workspace-only occurrence of one of these words no longer flags, while a
# call that actually reaches into `run_root` still does.
VERDICT_GATE_PEEK_PATTERNS: tuple[str, ...] = (
    "iterations/", "events.jsonl", OUTPUT_FILENAME)


def detect_verdict_gate_peek(
        run_root: Path, number: int,
        patterns: tuple[str, ...] = VERDICT_GATE_PEEK_PATTERNS) -> list[dict]:
    """Task 020 (D.5, #125 A): a post-hoc, honor-system check over ONE
    `verdict_gate` iteration's OWN transcript for a tool call that reached
    past the blindness contract `prompts/verdict_gate.md` states in words --
    a `bash`/`read`/etc. call whose rendered arguments mention any of
    `VERDICT_GATE_PEEK_PATTERNS` (`iterations/`, `events.jsonl`, and the
    per-iteration transcript filename `..log_merge.OUTPUT_FILENAME` names)
    -- the three paths that would let the peer see review's own
    reasoning/verdict or any other iteration's raw transcript instead of
    judging the finished workspace on its own.

    This is a RECORDED OBSERVATION, never a verdict override: nothing in
    `LoopSupervisor._verdict_gate_verdict` consults this function, and no
    caller may let its result change `approved`/`payload` -- see
    `LoopSupervisor._record_verdict_gate_peek`, which calls this strictly
    AFTER the verdict is already decided, at the peer gate's call site
    (`_step_verdict_gate`; the off-budget `_grace_verdict_gate` twin task
    015/E.1 retired used to be a second).

    Scans the WHOLE transcript, not just the tail `last_tool_call` reads
    (which truncates to its newest `TERMINATION_EVIDENCE_TAIL_BYTES`): a
    peek can happen at any point in a single iteration's tool-call
    sequence, not only at its very end, and one gate iteration's own
    transcript is never the many-megabyte, many-iteration size that tail
    truncation exists to bound.

    Returns one dict per flagged call -- `tool`, `args` (rendered, scrubbed
    and truncated the same way `last_tool_call` truncates evidence) and
    `matched` (which of `patterns` fired) -- `[]` for a clean iteration.
    Absent/unreadable transcript -> `[]`: like `last_tool_call`, this is
    diagnostic, never a reason to raise.

    Task 051 (J.2, J.3): a `patterns` hit ALONE is never enough -- the
    call's rendered arguments must also mention `run_root`'s own path
    (`anchor` below) before any pattern counts. This is what keeps a gate
    iteration legitimately grepping `src/` for the word `events.jsonl`
    (which this repository spells throughout) from being flagged: that
    call never mentions the run-state directory at all, only the bare
    word, so `anchor not in rendered` short-circuits it before `patterns`
    is even consulted. A call that actually reaches into `run_root` --
    `cat <run_root>/events.jsonl`, a `read` of
    `<run_root>/iterations/000N/` joined with `..log_merge.OUTPUT_FILENAME`
    -- mentions the anchor and is still flagged exactly as before.
    """
    path = iteration_output_path(run_root, number)
    if not path.is_file():
        return []
    try:
        lines = path.read_text(errors="replace").splitlines()
    except OSError:
        return []
    anchor = str(Path(run_root))
    flagged: list[dict] = []
    for line in lines:
        line = line.strip()
        if not line or "tool_execution_start" not in line:
            continue
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict) or \
                event.get("type") != "tool_execution_start":
            continue
        args = event.get("args") or event.get("arguments") or {}
        try:
            rendered = json.dumps(args, sort_keys=True)
        except (TypeError, ValueError):
            rendered = str(args)
        if anchor not in rendered:
            continue
        matched = [p for p in patterns if p in rendered]
        if not matched:
            continue
        scrubbed = scrub_text(rendered)
        if len(scrubbed) > TERMINATION_EVIDENCE_ARGS_MAX:
            scrubbed = scrubbed[:TERMINATION_EVIDENCE_ARGS_MAX] + "..."
        flagged.append({"tool": str(event.get("toolName") or "?"),
                        "args": scrubbed, "matched": matched})
    return flagged


def count_tool_calls(run_root: Path, number: int) -> int:
    """How many `tool_execution_start` events one iteration's OWN transcript
    recorded (task 010, PRD requirement B.1's witness (d): "the transcript
    shows tool activity above a threshold documented in LoopSupervisor" --
    see `LoopSupervisor.STAGNATION_TOOL_ACTIVITY_MIN_CALLS`, the threshold
    this count is compared against).

    Scans the WHOLE transcript, the same shape `detect_verdict_gate_peek`
    already uses and for the same reason: a single worker iteration's own
    transcript is never the many-iteration, many-megabyte size the tail
    truncation `last_tool_call` relies on exists to bound.

    Absent/unreadable transcript -> 0: like its siblings above, this is a
    diagnostic input to an ADDITIONAL progress witness, never a reason to
    raise.
    """
    path = iteration_output_path(run_root, number)
    if not path.is_file():
        return 0
    try:
        lines = path.read_text(errors="replace").splitlines()
    except OSError:
        return 0
    count = 0
    for line in lines:
        line = line.strip()
        if not line or "tool_execution_start" not in line:
            continue
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if isinstance(event, dict) and event.get("type") == "tool_execution_start":
            count += 1
    return count


# Task 036 (#20 H4): the host's record of WHICH IMAGE a run's container was
# started from -- written into the run dir's `host.json` by `ralphctl
# start`/`resume`, read from here by everything that reports or reproduces it.
#
# host.json is host-side state (ralphctl writes it, the engine never does), but
# it is the only place this answer can exist: a process inside a container
# cannot see the image it was created from -- nothing in /proc names it, and the
# docker socket is not there unless the operator mounted it. So the engine
# READS this record to answer "which engine is this run actually running?"
# through `GET /status`, and `ralphctl resume` prefers it over re-deriving a tag
# from possibly-changed sources (requirement H4: a resume must not swap the
# engine mid-run). One reader for both, here, so the API and the CLI can never
# disagree about the same file.
HOST_FILE = "host.json"
# `image` is the reference the container was started with, `imageId` the
# daemon's content id for the image it actually got (`docker inspect
# --format {{.Image}}` on the container, i.e. observed rather than assumed),
# and the remaining three are the provenance `resolve_job_image` reported:
# how the reference was arrived at (cli/main.py's IMAGE_SOURCE_* vocabulary),
# the content hash it was tagged by (None when it was not content-derived, so
# staleness is unknowable), the base it was derived from and the operator
# Dockerfile that base was built from.
IMAGE_RECORD_KEYS = ("image", "imageId", "imageSource", "imageHash",
                     "imageBase", "imageDockerfile")
# docker's own short-id width, for display only -- `--json` keeps the full id.
IMAGE_ID_SHORT = 12


def image_record_from(host_doc) -> dict:
    """The job-image record carried by a parsed `host.json` document: every
    IMAGE_RECORD_KEY present, None for anything the record does not say.

    Absence is never a third case a consumer has to handle -- the same
    discipline as `maxApproaches`/`model` in GET /status. A pre-v0.6 run dir, a
    missing host.json, and a host.json whose container's image id could not be
    observed all yield explicit nulls. Non-string junk is dropped rather than
    propagated (defensive like `format_approach`): this file is written by
    another process and every reader of it here is a viewer.
    """
    rec = dict.fromkeys(IMAGE_RECORD_KEYS)
    if not isinstance(host_doc, dict):
        return rec
    for key in IMAGE_RECORD_KEYS:
        val = host_doc.get(key)
        if isinstance(val, str) and val.strip():
            rec[key] = val.strip()
    return rec


def image_record(run_root: Path) -> dict:
    """`image_record_from` over a run dir's host.json (absent/unreadable ->
    all-None record, never an exception)."""
    return image_record_from(read_json(Path(run_root) / HOST_FILE, None))


def format_image_id(image_id) -> str:
    """A docker-style short image id (12 hex, algorithm prefix dropped) for
    display; '' when there is nothing to show."""
    if not isinstance(image_id, str) or not image_id.strip():
        return ""
    val = image_id.strip()
    _, _, digest = val.rpartition(":")
    return (digest or val)[:IMAGE_ID_SHORT]


def format_image(rec: dict | None) -> str:
    """THE rendering of "which image is this run running":
    `ralphd:ab12cd34ef56  (id 0f1e2d3c4b5a)`.

    '' when nothing is recorded (callers omit the line entirely rather than
    printing `image: None` -- the discipline of the `model:`/approach segments).
    The id is appended only when it adds information: a reference the daemon
    could not resolve when the run started has none, and a reference that *is*
    the id needs no parenthetical repeat of itself.

    Task 032 (F.6): when `rec` also carries an `engineVersion` (as the full
    `GET /status` document does, since task 030), it is rendered right beside
    the content-hash tag -- `ralphd:ab12cd34ef56  (engine 0.20.2)`, or
    `ralphd:ab12cd34ef56  (engine 0.20.2, id 0f1e2d3c4b5a)` alongside the id --
    with the tag itself unchanged either way. Callers that pass a bare
    `IMAGE_RECORD_KEYS`-only dict (no `engineVersion` key) see no change.
    """
    rec = rec or {}
    ref = rec.get("image")
    short = format_image_id(rec.get("imageId"))
    if not isinstance(ref, str) or not ref.strip():
        return ""
    ref = ref.strip()
    version = rec.get("engineVersion")
    version_part = (f"engine {version.strip()}"
                    if isinstance(version, str) and version.strip() else "")
    id_part = f"id {short}" if short and short not in ref else ""
    extras = ", ".join(p for p in (version_part, id_part) if p)
    return f"{ref}  ({extras})" if extras else ref


# Task 023 (#8): the tasks.json `status` string -> /status `tasks` counts key
# mapping, in ONE place. Both the engine (GET /status) and the host-side CLI
# fallback (`ralphctl status` on an unreachable run) count the same tasks.json,
# so they must agree key-for-key -- a second copy of this mapping is how
# `tasks: 5/7 completed` and `tasks: 5/7 completed (1 in-progress)` start
# disagreeing about the same file.
# Task 019 (requirement F): `awaiting-validation` is the only new key that
# needs a camelCase mapping here -- `validated` has no hyphen, so it
# already passes through unchanged (see `task_counts()` below).
_TASK_COUNT_KEYS = {"in-progress": "inProgress", "validation-failed": "validationFailed",
                    "awaiting-validation": "awaitingValidation"}


# The names of a run's well-known documents, in ONE place: `RunDir`'s
# properties, `prd_path` and the run-document surfaces (task 021, #18.2) all
# spell them from here, so "which file is the handoff notes" is a constant
# rather than a string literal repeated per reader.
PRD_FILE = "prd.md"
COMPOSITE_PRD_FILE = "composite-prd.md"
NOTES_FILE = "notes.md"
FINDINGS_FILE = "review-findings.md"
# Task 001 (requirement A, #110): the machine-readable sidecar beside the
# human-readable findings markdown above -- a JSON list of structured
# findings (`engine.findings.FINDING_SCHEMA`), never a replacement for it.
FINDINGS_JSON_FILE = "review-findings.json"
# Task 019 (requirement D.4, #125): the peer verdict gate's OWN findings
# channel, deliberately a DIFFERENT fixed path from `FINDINGS_FILE`/
# `FINDINGS_JSON_FILE` above -- review and the gate are two independent
# judges (requirement L), and folding their findings into one composite
# PRD (`LoopSupervisor._write_composite_prd`) must be able to tell which
# artifact came from which judge by NAME alone, not only by the `author`
# field the same task adds to `engine.findings.FINDING_SCHEMA`.
VERDICT_GATE_FINDINGS_FILE = "verdict-gate-findings.md"
VERDICT_GATE_FINDINGS_JSON_FILE = "verdict-gate-findings.json"
# Task 025 (PRD requirement H.1/H.4, #264): review's own EARLY verdict
# checkpoint -- written the instant review forms a verdict (through the
# `record-verdict` hat tool, `hat_tools.SteerToolBroker._record_verdict`),
# refined or superseded once the hat's own turn actually completes and
# `LoopSupervisor._review_verdict` reads the ordinary end-of-iteration
# contract payload. Deliberately its OWN file, never `FINDINGS_FILE`/
# `FINDINGS_JSON_FILE` above: those are the judge's full findings channel
# (unbounded prose plus a structured list, written by the hat's own file
# tools, no atomicity guaranteed mid-write); this is a small, bounded,
# engine-written checkpoint meant to survive a kill that lands before the
# findings channel -- or the hat's own final reply -- is ever written.
REVIEW_EARLY_VERDICT_FILE = "review-verdict-early.json"
# The one field on this checkpoint whose length the hat controls (`reason`)
# is capped hard, independent of whatever length a hat pastes in -- the
# whole point of a small, atomic checkpoint is that writing it is cheap
# enough to never itself become the thing a kill catches half-written.
REVIEW_EARLY_VERDICT_REASON_MAX_CHARS = 4000
# Task 006 (requirement A.3, #155 engine half): the debt ledger a verifier's
# structured `notes` (task 005's `verifyNotes`) land in -- `DEBT_JSON_FILE` is
# the machine-readable list `record_debt_entries` below is the ONE write path
# for, `DEBT_FILE` the human render of the same list (`render_debt_markdown`).
DEBT_FILE = "debt.md"
DEBT_JSON_FILE = "debt.json"
# Task 035 (requirement B.1, cure for task 006): the ENGINE's own record of
# what each judge decided, per approach -- written by `record_approach_verdict`
# below at the moment a verdict is settled, never by a hat's process, and
# NEVER derived from the two optional findings sidecars above. The sidecars
# are review's/the gate's own deliverable and may be missing, empty or
# unreadable; this file always carries the verdict word plus an EXPLICIT
# `blocking`/`residual` array per channel, so `run_record.build_approach_
# history` can report what a judge decided even for an approach whose sidecar
# was never written. Like `petition-adjudications.json` (and unlike
# `tasks.json`/`notes.md`/the findings files) it is NOT copied into
# `approaches/NN/` by `_archive_approach`: it stays at the run root keyed by
# approach number, which is what lets one file answer for the live approach
# and every archived one at once.
APPROACH_METADATA_FILE = "approach-metadata.json"
# The two judge channels this metadata records, spelled once. `review` is
# review's own verdict (`LoopSupervisor._review_verdict`); `verdictGate` is the
# opt-in peer gate's (`LoopSupervisor._verdict_gate_verdict`) -- the same two
# keys `run_record`'s approach-history entries use, so metadata and record can
# never name a channel two different ways.
APPROACH_METADATA_CHANNELS = ("review", "verdictGate")
# Task 107 (#60 pillar 3): the notes' `## Standing rules` block, pinned by
# the harness rather than trusted to worker.md's "preserve verbatim"
# convention. This file is the SOURCE OF TRUTH the planner's `standing_rules`
# contract field (transitions.HAT_SCHEMAS["planning"]) is copied into once
# (RunDir.write_standing_rules), and every iteration re-injects it into
# notes.md verbatim (see reinject_standing_rules) after the agent subprocess
# has had its turn -- so an agent that rewrites notes.md wholesale can still
# lose everything else it wrote, but never this block. Never exposed as an
# operator-editable RUN_DOCUMENTS entry: it is mirrored INTO notes.md, which
# already is one, and a second editable copy would just be a second place to
# go stale against the first.
STANDING_RULES_FILE = "standing-rules.md"
# `job.yaml` lives in the run's *config* dir (mounted read-only at `/config`),
# not in the run dir -- see `run_documents`.
JOB_CONFIG_FILE = "job.yaml"

# The one heading `reinject_standing_rules` looks for. Deliberately a plain
# literal, matched at the start of a line -- the block it delimits runs from
# this line to the next line starting with "## " (any other level-2 heading)
# or end of file, exactly the span `worker.md`'s prose already describes to
# an agent by convention; this function is what makes that span survive
# without trusting the agent to have read the convention at all.
STANDING_RULES_HEADING = "## Standing rules"

# Written immediately after the pinned block's own content on every
# harness injection (see `reinject_standing_rules`) -- an exact, private
# delimiter that makes finding the block's END unambiguous no matter what
# follows it (free prose with no heading of its own, a heading, nothing at
# all). Once ANY reinjection has happened, every later one matches on this
# marker, not on "the next heading" -- the heading-scan below is kept only
# as the first-time fallback, for a notes.md that still carries the OLD,
# pre-task-107 hand-written block (no marker yet).
STANDING_RULES_END_MARKER = "<!-- /standing-rules: harness-pinned, task 107 -->"

# Any markdown heading, any level -- the first-time fallback's "next
# section starts here" guess, used only when `STANDING_RULES_END_MARKER`
# is not present in the text at all.
_MD_HEADING_RE = re.compile(r"^#{1,6}\s")


def reinject_standing_rules(notes_text: str, pinned_block: str) -> str:
    """Task 107 (#60 pillar 3): return `notes_text` with its
    `STANDING_RULES_HEADING` section replaced by `pinned_block` verbatim --
    or `pinned_block` prepended, if the agent's rewrite dropped the section
    entirely. Every OTHER line of `notes_text` survives untouched, in place,
    exactly as the agent wrote it: this is a splice, not a merge, and it
    never inspects or judges the rest of the file.

    The block's end is `STANDING_RULES_END_MARKER`, a private line this
    function itself always writes right after the block's own content --
    so a rewrite whose very next section has no heading at all (or one
    that isn't `## `) still splices correctly on every reinjection after
    the first. The very first time a run's notes.md is touched (or a
    notes.md that predates this mechanism and carries the old hand-
    written block with no marker yet), the marker is absent and the block
    end falls back to "the next markdown heading, any level, or end of
    file" -- a best-effort guess for content this function has never
    itself produced.

    `pinned_block` is stored (and passed in here) as the harness's own copy
    -- `RunDir.read_standing_rules()` -- never anything an agent's own
    notes.md write could influence, which is the whole point: a worker that
    overwrites notes.md wholesale, heading and all, gets the pinned text
    back the moment this runs, byte-for-byte, while every other line it
    wrote is preserved as written.

    A blank `pinned_block` is not a valid call (the one caller,
    `LoopSupervisor._reinject_standing_rules`, already skips calling this at
    all when nothing was ever pinned) but is handled harmlessly here too:
    the heading section is stripped and nothing is re-added, rather than
    raising, so this function stays a pure splice with no policy of its
    own about when pinning is "on".
    """
    pinned = pinned_block.rstrip("\n")
    lines = notes_text.splitlines()
    start = None
    end = len(lines)
    marker_seen = False
    for i, line in enumerate(lines):
        if start is None:
            if line.rstrip() == STANDING_RULES_HEADING:
                start = i
            continue
        if line.rstrip() == STANDING_RULES_END_MARKER:
            end = i + 1
            marker_seen = True
            break
        if not marker_seen and _MD_HEADING_RE.match(line):
            end = i
            break
    if start is None:
        # No existing block to replace -- prepend, so the pinned text is
        # always first regardless of what the agent's rewrite led with.
        rest = notes_text.lstrip("\n")
        if not pinned:
            return rest
        block = pinned + "\n" + STANDING_RULES_END_MARKER
        return block + ("\n\n" + rest if rest else "\n")
    before = "\n".join(lines[:start])
    after = "\n".join(lines[end:])
    block = (pinned + "\n" + STANDING_RULES_END_MARKER) if pinned else ""
    pieces = [p for p in (before.rstrip("\n"), block, after.lstrip("\n")) if p]
    return "\n\n".join(pieces) + ("\n" if pieces else "")


def prd_path(run_root: Path, original: bool = False) -> Path | None:
    """Which file *is* a run's PRD (task 056, #1) -- decided in ONE place.

    A run dir can hold two: `prd.md` (exactly what the operator handed to
    `ralphctl start`) and, when the engine composed one, `composite-prd.md`
    (the text the agent actually works from). "The run's PRD" therefore
    means the composite when it exists and the original otherwise;
    `original=True` forces the raw one.

    Shared by the engine's `GET /prd` and the host-side on-disk fallback the
    hub's PRD dialog uses for an unreachable run (`ui_server.prd_text`, same
    live-first/on-disk shape as the log merge in tasks 038/039), so the two
    readers can never disagree about which file a reader is shown. Returns
    None when the requested PRD is not there at all (caller -> 404 / the
    "no PRD" message).

    Deliberately a plain function over a path, not a `RunDir` method: the
    host side must be able to read a registry run dir WITHOUT constructing a
    `RunDir` (whose `__post_init__` creates directories -- a read-only
    viewer must never write into a run dir).
    """
    root = Path(run_root)
    composite = root / COMPOSITE_PRD_FILE
    if not original and composite.exists():
        return composite
    plain = root / PRD_FILE
    return plain if plain.exists() else None


# Task 016 (#17): the shape of a run's steering directory, in ONE place.
#
# `<run>/steering/NNN-<name>.md` holds the operator messages; the engine marks
# a file *applied* by appending its name to `steering/.consumed.json` when an
# iteration has FINISHED cleanly with it in its prompt (`RunDir.consume_steering`,
# called from `LoopSupervisor._run_iteration_once` after the outcome is known --
# task 018, #34: an iteration that failed, was interrupted or timed out leaves
# its notes pending for the next one). Both the engine's
# `GET /steering` and the host-side hub fallback (`ui_server.steering_list`,
# for a run whose container is gone) read the pair through `steering_entries`
# below, so a live answer and an on-disk answer cannot disagree about which
# entries exist or which of them are still pending.
STEERING_GLOB = "[0-9][0-9][0-9]-*.md"
STEERING_CONSUMED_FILE = ".consumed.json"
STEERING_PENDING = "pending"
STEERING_APPLIED = "applied"
# Task 007 (requirement B.1, #122): a THIRD state, distinct from both.
# `pending` means "no iteration has ever delivered this message";
# `applied` means "consumed for good" -- either because the steer hat
# claimed `settled: true`, or because task 006's redelivery cap gave up on
# it (settled-by-exhaustion, which forces `application` to `applied` and
# leaves the file in `.consumed.json` on purpose). Before task 006/007, a
# clean steer iteration that honestly reported `settled: false` still left
# a THIRD, unrepresented case once it was redelivered: the message had
# been delivered and judged (the ledger holds a `settled: false` record for
# it), it is not consumed on disk (task 006 already unconsumed it so it is
# genuinely deliverable again), yet plain pending/applied has no word for
# "already tried, still not settled, waiting for the next pass" -- it read
# exactly like a message no iteration has ever touched. `outstanding` is
# that word: reported for a file that is not currently consumed AND whose
# steering ledger record (`steering_ledger_records`) says `settled: False`
# without `settledByExhaustion` -- i.e. a redelivery still in flight.
STEERING_OUTSTANDING = "outstanding"


def steering_consumed_names(run_root: Path) -> set[str]:
    """The set of steering file names an iteration has already consumed."""
    names = read_json(Path(run_root) / "steering" / STEERING_CONSUMED_FILE, [])
    return {n for n in names if isinstance(n, str)} if isinstance(names, list) else set()


def steering_entries(run_root: Path, *, bodies: bool = True) -> list[dict]:
    """Every steering message of a run, oldest first (task 016, #17).

    One entry per `steering/NNN-<name>.md` file:

      `file`   the file name (the engine's own identifier for an entry,
               already used by `POST /steering`'s answer and the
               `steering.received`/`steering.consumed` events),
      `seq`    the engine-assigned sequence number as an int (sortable),
      `name`   the operator-supplied suffix (`--name`), i.e. the file stem
               without its sequence prefix -- what a human called it,
      `ts`     when the message was written, from the file's mtime in
               `utcnow()` format (the file is written once, atomically, by
               `add_steering`, so its mtime *is* its arrival time; no
               separate index to drift out of sync),
      `state`  `pending`, `outstanding` or `applied` -- the vocabulary
               lives here, so the CLI, the hub and the API all say the
               same word. `applied` means consumed for good (a `settled:
               true` claim, or task 006's redelivery cap giving up on it);
               `outstanding` (task 007, #122) means the message has been
               delivered and judged `settled: false` by the steering
               ledger, is not currently consumed on disk, and is waiting
               to be redelivered -- distinct from `pending`, which is a
               message no iteration has ever looked at yet;
      `consumed` the same disk fact as a bool (kept because the pre-v0.6
               `GET /steering` answered with exactly that key) -- `False`
               for BOTH `pending` and `outstanding`, since neither is
               consumed on disk; only the ledger tells them apart,
      `body`   the message text (omit with `bodies=False` for list views
               that only need the header line; `hasBody` is always there).

    Deliberately a plain function over a path, not a `RunDir` method, for the
    same reason as `prd_path`: the host side reads a registry run dir without
    constructing a `RunDir` (which would create directories in somebody
    else's run dir). Returns `[]` when there is no `steering/` dir at all --
    an operator who never steered, which is not an error.
    """
    sdir = Path(run_root) / "steering"
    try:
        files = sorted(sdir.glob(STEERING_GLOB))
    except OSError:
        return []
    consumed = steering_consumed_names(run_root)
    ledger_records = steering_ledger_records(run_root)
    out = []
    for p in files:
        try:
            st = p.stat()
            text = p.read_text(errors="replace")
        except OSError:
            continue
        stem = p.name.removesuffix(".md")
        m = re.match(r"^(\d{3})-(.*)$", stem)
        seq = int(m.group(1)) if m else None
        name = (m.group(2) if m else stem) or stem
        applied = p.name in consumed
        record = ledger_records.get(p.name) or {}
        if applied:
            entry_state = STEERING_APPLIED
        else:
            if record.get("settled") is False and not record.get("settledByExhaustion"):
                entry_state = STEERING_OUTSTANDING
            else:
                entry_state = STEERING_PENDING
        entry = {
            "file": p.name,
            "seq": seq,
            "name": name,
            "ts": utc_from_epoch(st.st_mtime),
            "state": entry_state,
            "consumed": applied,
            "bytes": st.st_size,
            "hasBody": bool(text.strip()),
            # Task 012 (PRD requirement D.2, #184): the steer hat's own
            # settlement notes for THIS entry, when the ledger has any --
            # the one field that carries a `dependsOn`-claimed-task
            # refusal onto the steering surface (`ralphctl steer --list`),
            # additive and `None` for a file no steer pass has settled
            # yet, never invented.
            "notes": record.get("notes"),
        }
        if bodies:
            entry["body"] = text
        out.append(entry)
    return out


# Task 034 (operator-inserted): the STEERING LEDGER -- one run-scope,
# engine-owned record per steering file, joining what the operator sent to
# what the steer hat did with it.
#
# The gap it closes, observed in this run: `steering_entries` above knows
# only two states (`pending`/`applied`), and "applied" there means merely
# "some iteration finished cleanly with the file in its prompt". The steer
# hat's own settlement claim (`settled` plus its `notes`, recorded by
# `LoopSupervisor._step_generic_hat` as the `steer.settled` event and the
# `steerSettled` status field) lived only in those two places, so a verify
# or review hat had NO first-class view of what was steered, what the steer
# hat applied, and what is still waiting -- and could fail work for
# conforming to a steer it never saw.
#
# `steering/.ledger.json` holds the engine's own per-file bookkeeping
# (consumption timestamp + iteration, the steer hat's settlement claim, its
# application status and notes); `steering_ledger` JOINS it with the files
# themselves, so a steering file that no steer iteration has touched yet
# appears in the ledger as `pending` IMMEDIATELY -- the ledger never waits
# for a record to exist before reporting a message.
#
# The application vocabulary lives here, in ONE place, exactly like
# `STEERING_PENDING`/`STEERING_APPLIED` above.
STEERING_LEDGER_FILE = ".ledger.json"
STEER_APPLICATION_APPLIED = "applied"
STEER_APPLICATION_PARTIAL = "partially-applied"
STEER_APPLICATION_PENDING = "pending"
STEER_APPLICATION_STATUSES: tuple[str, ...] = (
    STEER_APPLICATION_APPLIED,
    STEER_APPLICATION_PARTIAL,
    STEER_APPLICATION_PENDING,
)


def steering_ledger_path(run_root: Path) -> Path:
    return Path(run_root) / "steering" / STEERING_LEDGER_FILE


def steering_ledger_records(run_root: Path) -> dict:
    """The engine's raw per-file ledger bookkeeping, keyed by file name.

    A missing or malformed file reads as `{}` -- the ledger is derived from
    the steering files themselves (`steering_ledger` below), so losing this
    record degrades the join to "nothing applied yet", never to a crash.
    """
    raw = read_json(steering_ledger_path(run_root), {})
    if not isinstance(raw, dict):
        return {}
    return {k: v for k, v in raw.items()
            if isinstance(k, str) and isinstance(v, dict)}


def _write_steering_ledger_records(run_root: Path, records: dict) -> None:
    path = steering_ledger_path(run_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(path, records)


def record_steering_consumption(run_root: Path, names, iteration: int) -> dict:
    """Stamp the consumption timestamp of each name in `names`.

    Called by `RunDir.consume_steering` -- the ONE writer of
    `.consumed.json` -- so the ledger's `consumed` timestamp can never
    disagree with the consumed set: both are written from the same call,
    for the same freshly-consumed names.
    """
    records = steering_ledger_records(run_root)
    ts = utcnow()
    for name in names:
        entry = dict(records.get(name) or {})
        entry.setdefault("consumedAt", ts)
        entry.setdefault("consumedIteration", iteration)
        records[name] = entry
    _write_steering_ledger_records(run_root, records)
    return records


def record_steering_settlement(run_root: Path, names, *, settled: bool,
                               notes=None, application=None) -> dict:
    """Record the steer hat's settlement payload against `names`.

    `settled` and `notes` are the hat's own contract fields
    (`transitions.HAT_SCHEMAS["steer"]`); `application` is its optional
    self-report of HOW MUCH it applied. The per-entry application status is
    derived here, in ONE place:

      * an explicit `application` the hat declared, when it is one of
        `STEER_APPLICATION_STATUSES`;
      * else `applied` when the hat claimed `settled: true`;
      * else `partially-applied` -- the hat processed the message and said
        so, but did not claim it settled, which is exactly the state a
        later hat must weigh rather than assume closed.

    Never `pending`: `pending` is the absence of any settlement record,
    which `steering_ledger` derives without a write.
    """
    if application in STEER_APPLICATION_STATUSES:
        status = application
    elif settled:
        status = STEER_APPLICATION_APPLIED
    else:
        status = STEER_APPLICATION_PARTIAL
    records = steering_ledger_records(run_root)
    ts = utcnow()
    for name in names:
        entry = dict(records.get(name) or {})
        entry["settled"] = bool(settled)
        entry["application"] = status
        entry["settledAt"] = ts
        if notes is not None:
            entry["notes"] = notes
        records[name] = entry
    _write_steering_ledger_records(run_root, records)
    return records


def unconsume_steering_names(run_root: Path, names) -> list[str]:
    """Task 006 (requirement B.1, #122): undo `RunDir.consume_steering`'s
    marker write for `names`, so a message a clean steer iteration honestly
    reported `settled: false` on stays genuinely deliverable rather than
    merely reported `pending` while secretly already consumed.

    By the time the steer hat's own payload is read (`_step_generic_hat`,
    strictly after `run_iteration()` returned), `_run_iteration_once`
    already banked the file it delivered -- a clean exit
    (`fault_class is None`) is exactly the condition it consumes on, and it
    has no way to know what the hat's payload will say. This function is
    the one place that reverses that write, once the payload is known.

    Returns the subset of `names` that were actually removed (already-
    unconsumed names are a no-op, exactly like `consume_steering`'s own
    idempotence). Also clears each removed name's `consumedAt`/
    `consumedIteration` ledger stamps, so a LATER genuine consumption
    re-stamps fresh values instead of `record_steering_consumption`'s
    `setdefault` silently skipping a value that is now a lie.
    """
    root = Path(run_root)
    marker = root / "steering" / STEERING_CONSUMED_FILE
    raw = read_json(marker, [])
    consumed = [n for n in raw if isinstance(n, str)] if isinstance(raw, list) else []
    names_set = {n for n in names}
    removed = [n for n in consumed if n in names_set]
    if not removed:
        return []
    remaining = [n for n in consumed if n not in names_set]
    atomic_write_json(marker, remaining)
    records = steering_ledger_records(root)
    changed = False
    for name in removed:
        entry = dict(records.get(name) or {})
        if "consumedAt" in entry or "consumedIteration" in entry:
            entry.pop("consumedAt", None)
            entry.pop("consumedIteration", None)
            records[name] = entry
            changed = True
    if changed:
        _write_steering_ledger_records(root, records)
    return removed


def mark_steering_settled_by_exhaustion(run_root: Path, names, *,
                                        reason: str) -> dict:
    """Task 006 (requirement B.1, #122): the per-message redelivery
    counter's (`steerRedeliveries` in status.json) fail-closed outcome.

    Once a message has been redelivered past `JobConfig.
    steer_redelivery_max` without ever earning a `settled: true` claim, it
    stops being redelivered -- `unconsume_steering_names` is never called
    for it again -- and is instead recorded here as settled BY EXHAUSTION,
    never by the hat's own claim: `settledByExhaustion`/`exhaustionReason`/
    `exhaustionAt` are new, distinct fields (never overwriting `settled`,
    which stays exactly what the hat last claimed -- almost always False --
    because exhaustion is the harness giving up on redelivery, not the hat
    pretending it finished). `application` is forced to `applied` so a
    later hat's ledger read stops weighing this as a live, still-open ask.
    """
    records = steering_ledger_records(run_root)
    ts = utcnow()
    for name in names:
        entry = dict(records.get(name) or {})
        entry["settledByExhaustion"] = True
        entry["exhaustionReason"] = reason
        entry["exhaustionAt"] = ts
        entry["application"] = STEER_APPLICATION_APPLIED
        records[name] = entry
    _write_steering_ledger_records(run_root, records)
    return records


def steering_ledger(run_root: Path) -> list[dict]:
    """The steering ledger of a run, oldest first -- one entry per file.

    Keys, per entry:

      `file`        the steering file name (the engine's own identifier),
      `seq`/`name`  as `steering_entries` above spells them,
      `received`    when the operator's message landed,
      `consumed`    when an iteration finished cleanly with it in its
                    prompt, or None when no such timestamp was recorded,
      `state`       `pending` | `applied`, the consumption fact
                    `steering_entries` reports from `.consumed.json`
                    itself (so a file consumed before this record existed
                    still reads as consumed, with no timestamp),
      `consumedIteration` which iteration that was, when known,
      `settled`     the steer hat's own settlement claim as a bool,
      `application` `applied` | `partially-applied` | `pending`,
      `notes`       the steer hat's settlement notes, when it wrote any.
      `settledByExhaustion` task 006 (#122): True once the per-message
                    redelivery cap (`steerRedeliveries`, status.json) was
                    reached without a `settled: true` claim -- distinct
                    from `settled` itself, which stays the hat's own last
                    claim (almost always False when this fires).
      `exhaustionReason` the reason recorded at that moment, or None.

    A plain function over a path, like `steering_entries`/`prd_path`, so
    the host side can read a registry run dir without constructing a
    `RunDir` in somebody else's run dir.
    """
    records = steering_ledger_records(run_root)
    out = []
    for entry in steering_entries(run_root, bodies=False):
        record = records.get(entry["file"]) or {}
        application = record.get("application")
        if application not in STEER_APPLICATION_STATUSES:
            application = STEER_APPLICATION_PENDING
        out.append({
            "file": entry["file"],
            "seq": entry.get("seq"),
            "name": entry.get("name"),
            "received": entry.get("ts"),
            "consumed": record.get("consumedAt"),
            "state": entry.get("state"),
            "consumedIteration": record.get("consumedIteration"),
            "settledByExhaustion": bool(record.get("settledByExhaustion")),
            "exhaustionReason": record.get("exhaustionReason"),
            "settled": bool(record.get("settled")),
            "application": application,
            "notes": record.get("notes"),
        })
    return out


# Task 028 (#26): the shape of an operator ruling ('amendment'), in ONE place.
#
# `<config_root>/amendments/NNN-<task-id>.md` holds an operator ruling review
# MUST honour -- a waiver of a task's successCriteria, or replacement text
# for it -- written host-side, ONLY by `ralphctl amend` (never through the
# HTTP API, never through the runtime config overlay). `config_root(run_id)`
# is the same host directory bind-mounted `:ro` as `/config` into every job
# container (`cli/main.py config_root`, used identically by `cmd_start` and
# `cmd_resume`): nothing running inside a job container -- not the worker's
# shell, not `PUT /config/*`, not the writable overlay under $HOME -- can
# write there, which is the whole point (see
# artifacts/reports/task-027-operator-ruling-design.md). The numbering
# convention mirrors STEERING_GLOB above; the format is a tiny fixed header
# (`task:`/`mode:`/`ts:`) followed by a blank line and the ruling text
# verbatim, so a reader never needs a YAML dependency to recover either half.
#
# `write_amendment` is the ONLY writer, called from `ralphctl amend` on the
# HOST -- it must never be called from inside a job container.
# `amendment_entries` is the ONLY reader, called both by `ralphctl amend
# --list` (host, always on-disk) and by `engine.loop`'s
# `_operator_amendment_review_context()` (in-container, reading
# `engine.config.CONFIG_DIR` directly -- never `overlay_or_config`, since the
# overlay lives inside the container's own writable filesystem and preferring
# it here would let the run forge the very thing this channel exists to keep
# unforgeable).
AMENDMENT_GLOB = "[0-9][0-9][0-9]-*.md"
AMENDMENT_DIRNAME = "amendments"
AMENDMENT_MODE_WAIVE = "waive"
AMENDMENT_MODE_REPLACE = "replace"
# Task 029 (#26, requirement E): a THIRD ruling kind, alongside waive/
# replace -- a typed status ruling the engine itself applies to that task's
# `status` field in tasks.json (see `apply_amendment_status_rulings` below),
# rather than one that only binds review's judgement of successCriteria.
# `ralphctl amend --status` (task 030) is the intended writer; nothing
# stops a hand-written file of this shape either, since the channel's
# unforgeability comes from WHERE it lives (`config_root`'s `:ro` mount),
# never from which tool wrote it.
AMENDMENT_MODE_STATUS = "status"

# Task 017 (D.7, #196): the reserved `taskId` spelling a WAVE-SCOPED ruling
# is written under -- `ralphctl amend --wave` (never a bare positional, so a
# typo can never collide with it) writes this exact string in place of a
# real task id. It can never collide with a genuine task id (those are
# always the zero-padded digit strings `plan_ingestion` assigns, `"001"`,
# `"002"`, ...), so a wave-scoped entry is NEVER subject to the
# plan-membership check `_amend_echo_target_or_die`/`_operator_amendment_
# review_context` run for a task-scoped one -- there is no task id for a
# replan to make disappear, which is the whole point (D.7: "recorded so
# that no replan can orphan it"). D.8 (task 018) is what actually teaches
# `_operator_amendment_review_context` to render this scope as
# unconditionally binding regardless of the current approach; this task
# only introduces the reserved spelling, the CLI target, and the
# `--list`-visible scope column.
AMENDMENT_WAVE_TARGET = "WAVE"


def amendments_dir(config_root: Path) -> Path:
    return Path(config_root) / AMENDMENT_DIRNAME


def write_amendment(config_root: Path, task_id: str, text: str, *,
                    waive: bool = False, status: str | None = None,
                    approach: int | None = None) -> str:
    """Record one operator ruling as the next numbered file under
    `<config_root>/amendments/`. HOST-SIDE ONLY -- `config_root` here is
    `cli.main.config_root(run_id)`, the directory bind-mounted `:ro` into
    the job container; calling this from inside a container would be
    writing into a read-only mount and fail on a real one (and must never
    be made to succeed by loosening that mount). Returns the file name
    written (`NNN-<task-id>.md`), the same identifier `amendment_entries`
    reports back.

    Task 029/030 (requirement E): `status`, when given, records a
    status ruling (`AMENDMENT_MODE_STATUS`) instead of a waive/replace one
    -- `text` is then the operator's optional reason, never the criteria
    text a waive/replace ruling carries. `waive` is ignored when `status`
    is given (a ruling is exactly one kind, never two at once).

    Task 036 (#168): `approach`, when given, stamps the fixed header with
    the approach this ruling was recorded against (`ralphctl amend`'s
    caller resolves it from the run's own `status.json`, or from an
    explicit `--approach N` override -- this function never reads
    `status.json` itself, since it only ever sees `config_root`, not the
    run root the approach lives under). `None` (the default, and every
    call site before task 036) omits the header line entirely --
    'header-only, backward compatible' per #168's proposed fix item 5: an
    entry with no `approach:` line reads as "the approach it was recorded
    in is unknown", never as approach 0 or any other invented value.

    Task 017 (D.7, #196): `task_id` may be the reserved sentinel
    `AMENDMENT_WAVE_TARGET` (`"WAVE"`) instead of a real task id --
    `ralphctl amend --wave` is the intended caller. This function does not
    special-case it in any way (it is just a string that happens never to
    collide with a real, zero-padded task id); the special handling lives
    entirely in `amendment_entries`'s `scope` field and in the CLI's own
    argument validation.
    """
    adir = amendments_dir(config_root)
    adir.mkdir(parents=True, exist_ok=True)
    existing = sorted(adir.glob(AMENDMENT_GLOB))
    seq = int(existing[-1].name[:3]) + 1 if existing else 1
    fname = f"{seq:03d}-{task_id}.md"
    if status:
        mode = AMENDMENT_MODE_STATUS
    else:
        mode = AMENDMENT_MODE_WAIVE if waive else AMENDMENT_MODE_REPLACE
    header = f"task: {task_id}\nmode: {mode}\n"
    if status:
        header += f"status: {status}\n"
    if approach is not None:
        header += f"approach: {approach}\n"
    header += f"ts: {utcnow()}\n\n"
    atomic_write(adir / fname, header + text.rstrip() + "\n")
    return fname


def repoint_amendment(config_root: Path, seq: int, new_target: str) -> str:
    """Task 021 (D.12, #196): re-address an ALREADY-RECORDED ruling (found
    by its `seq` -- the same number `amend --list`'s `SEQ` column and
    `amendment_entries()['seq']` show) to `new_target`, a live task id or
    `AMENDMENT_WAVE_TARGET` -- the amendment-analogue of `steer
    task-update`'s `dependsOn` rewrite, so an orphaning (D.11's escalation)
    is a one-command fix rather than a hand-edit of the file under
    `config_root/amendments/`.

    Rewrites the fixed header's `task:` line in place (mode/status/
    approach/ts and the body text are untouched) and renames the file so
    its own name (`NNN-<task-id>.md`) stays truthful about what
    `amendment_entries()['taskId']` will report for it -- the SEQ number
    never changes, only the target. Raises `FileNotFoundError` naming
    `seq` when no amendment file with that sequence number exists (the
    CLI turns that into a `die()` before writing anything); this function
    does not itself check `new_target` against any plan -- that
    plan-membership refusal is the CLI's job (`_amend_echo_target_or_die`
    reads `run_root`, this function only ever touches `config_root`, the
    same host/container boundary every other writer here keeps).
    """
    adir = amendments_dir(config_root)
    matches = sorted(adir.glob(f"{seq:03d}-*.md"))
    if not matches:
        raise FileNotFoundError(
            f"no recorded ruling with seq {seq} under {adir}")
    old_path = matches[0]
    raw = old_path.read_text(errors="replace")
    header, sep, body = raw.partition("\n\n")
    new_lines = []
    replaced = False
    for line in header.splitlines():
        key, _, val = line.partition(":")
        if key.strip() == "task":
            new_lines.append(f"task: {new_target}")
            replaced = True
        else:
            new_lines.append(line)
    if not replaced:
        new_lines.append(f"task: {new_target}")
    new_header = "\n".join(new_lines)
    new_fname = f"{seq:03d}-{new_target}.md"
    atomic_write(adir / new_fname, new_header + sep + body)
    if old_path.name != new_fname:
        old_path.unlink()
    return new_fname


def amendment_entries(config_root: Path) -> list[dict]:
    """Every operator ruling recorded for a run, oldest first. Reads
    `<config_root>/amendments/` directly -- `config_root` is EITHER the host
    path `cli.main.config_root(run_id)` (used by `ralphctl amend --list`) OR
    `engine.config.CONFIG_DIR` as seen from inside the job container (used by
    `engine.loop._operator_amendment_review_context()`) -- the same pair of
    callers, and the same read-only relationship, as `job.yaml`/`prd.md`.
    Returns `[]` when there is no `amendments/` dir at all (the common case:
    an operator who has never amended anything), never an error."""
    adir = amendments_dir(config_root)
    try:
        files = sorted(adir.glob(AMENDMENT_GLOB))
    except OSError:
        return []
    out = []
    for p in files:
        try:
            st = p.stat()
            raw = p.read_text(errors="replace")
        except OSError:
            continue
        stem = p.name.removesuffix(".md")
        m = re.match(r"^(\d{3})-(.*)$", stem)
        seq = int(m.group(1)) if m else None
        header, _, body = raw.partition("\n\n")
        fields: dict[str, str] = {}
        for line in header.splitlines():
            key, _, val = line.partition(":")
            fields[key.strip()] = val.strip()
        task_id = fields.get("task") or (m.group(2) if m else stem)
        mode = fields.get("mode") or AMENDMENT_MODE_REPLACE
        ts = fields.get("ts") or utc_from_epoch(st.st_mtime)
        # Task 036 (#168): the approach the ruling was recorded against,
        # when the header carries one -- `None` for every entry written
        # before this task, and for one written with no resolvable
        # `status.json` approach either (write_amendment's own
        # header-only-when-given contract, see its docstring).
        raw_approach = fields.get("approach")
        approach = None
        if raw_approach is not None:
            try:
                approach = int(raw_approach)
            except ValueError:
                approach = None
        out.append({
            "file": p.name,
            "seq": seq,
            "taskId": task_id,
            "waive": mode == AMENDMENT_MODE_WAIVE,
            "mode": mode,
            # Task 017 (D.7, #196): "wave" for the reserved
            # `AMENDMENT_WAVE_TARGET` spelling, "task" for every ordinary
            # task-scoped ruling (every entry ever written before this task
            # falls in the latter bucket) -- the one field `ralphctl amend
            # --list` reads to render the SCOPE column (`cmd_amend_list`).
            "scope": "wave" if task_id == AMENDMENT_WAVE_TARGET else "task",
            # Task 029: the ruled target status, only meaningful (and only
            # ever non-None) for a `mode: status` entry -- a waive/replace
            # entry never carries a `status:` header line at all.
            "status": fields.get("status") if mode == AMENDMENT_MODE_STATUS else None,
            "approach": approach,
            "ts": ts,
            "text": body.strip(),
        })
    return out


# v0.23 requirement B.3 (#221, task 007): the un-reopen edge's own
# dedicated event -- `apply_task_status_change` emits THIS, never the
# generic `amendment_status_applied` audit event, for the one transition
# `transitions.is_un_reopen_transition` recognises (`pending` back to a
# record's own pre-reopen terminal status). `amendment_status_applied`'s
# own name and payload shape (`file`, an amendment ruling's own filename)
# is honestly an operator-ruling/steer-tool audit record; this transition
# is neither of those in its intended caller (requirement B.4, task 008,
# `LoopSupervisor._reconcile_pre_cure_heads`, the completion-path
# reconciliation hook -- `source="cure-reconciliation"`) even though it
# is also reachable through the two pre-existing callers
# (`apply_amendment_status_rulings`/`steer_task_update`) the declared
# table permits it for -- `source` still names whichever caller drove
# it, exactly as `amendment_status_applied` already does.
UN_REOPEN_EVENT = "task.un_reopened"


def apply_task_status_change(run: "RunDir", task_id: str, target_status: str,
                             vigilant: bool, *, source: str,
                             reason: str = "", file: str | None = None,
                             justification: str = "") -> dict:
    """Task 010 (PRD requirement H, #98): THE one place that decides
    whether a task's `status` may move, writes it when it may, and emits
    the audit event -- extracted from `apply_amendment_status_rulings`
    (task 029/#26, requirement E) so that function and the steer hat's
    `task-update` tool (`STEER_HAT_TOOLS`, below) are two CALLERS of the
    same applier rather than two copies of these rules (PRD requirement H:
    "One place decides which transitions are legal ... two callers use
    it"). `source` names which caller this is (`"operator"` for an
    amendment ruling, `"steer"` for the steer hat), surfaced on the audit
    event exactly like `budget_changed` already names its own trigger.
    `file` is the amendment file's own name for the operator path, and
    `None` for the steer path (there is no amendment file); it is passed
    through to the audit event unchanged either way, matching what
    `apply_amendment_status_rulings` always emitted.

    - a no-op (`current == target_status`) changes nothing and emits
      nothing -- safe to call repeatedly once a status has already taken
      effect;
    - a LEGAL transition (`transitions.is_legal_task_status_transition`,
      the SAME declared `TASK_STATUS_TABLE`/`TASK_STATUS_VIGILANT_DELTA`
      every other status move in the engine is checked against) is then
      run through `transitions.task_status_report_errors` -- the SAME
      semantic check the worker/review contract channel's own
      `_apply_reported_task_status` already applies (task 011/#123) -- so
      the one declared move with an extra condition (`failed -> skipped`
      requires a non-empty justification) is enforced on THIS caller too,
      not just the hat-report channel. `justification` defaults to
      `reason` when the caller passes no explicit one (the operator
      path's `reason` IS its own stated justification for an amendment
      ruling); a caller with neither is refused exactly like a hat report
      with none. A transition that clears both checks writes the new
      `status` via `run.write_tasks` (the one authorized writer, task
      013), and for `failed -> skipped` ALSO writes `justification` into
      the task's own `notes` field in that SAME write -- never into
      `validationNotes`/`unsatisfiableReason` -- then emits an
      `amendment_status_applied` audit event, EXCEPT for the one declared
      move `transitions.is_un_reopen_transition` recognises (`pending`
      back to a record's own pre-reopen terminal status, requirement B.3,
      task 007, #221), which emits its own dedicated `UN_REOPEN_EVENT`
      (`"task.un_reopened"`) instead -- naming `taskId`, `previous`
      (always `"pending"`), `status` (the restored terminal status),
      `source` and `reason` -- so a reader of the event stream can tell an
      automated un-reopen apart from an ordinary operator/steer ruling
      without inspecting `status` itself. `LoopSupervisor._reconcile_pre_
      cure_heads` (requirement B.4, task 008) is the completion-path hook
      that now takes this edge, with `source="cure-reconciliation"`; this
      task itself only declared the edge and wired the event;
    - an ILLEGAL transition, or a `failed -> skipped` move with no
      justification, is refused: nothing is written, and the returned
      record says why.

    `validationAttempts`, `tierLadderExhausted` and `successCriteria` are
    never touched here -- only `status` (and, for `failed -> skipped`
    only, `notes`) ever move (PRD requirement E: a status change is not a
    claim the criteria were met).

    Returns `{"taskId", "status", "previous", "applied", "reason"}` --
    `reason` is `""` when applied, and one of `"no matching task"` /
    `"already at that status"` / `"illegal transition"` / the transition
    check's own `"task_status: 'failed' -> 'skipped' requires a non-empty
    justification"` diagnostic otherwise.
    """
    data = run.read_tasks()
    tasks = data.get("tasks", [])
    by_id = {t.get("id"): t for t in tasks if isinstance(t, dict)}
    task = by_id.get(task_id)
    record = {"taskId": task_id, "status": target_status}
    if task is None:
        record.update(previous=None, applied=False, reason="no matching task")
        return record
    current = task.get("status", "pending")
    record["previous"] = current
    if current == target_status:
        record.update(applied=False, reason="already at that status")
        return record
    if not transitions.is_legal_task_status_transition(current, target_status, vigilant):
        record.update(applied=False, reason="illegal transition")
        return record
    effective_justification = justification or reason
    # Cure-01-05 (PRD requirement B.3/B.4, F05, #221): the ONE source
    # allowed to take the un-reopen RESTORE through this channel is this
    # function's own reconciliation branch
    # (`LoopSupervisor._reconcile_pre_cure_heads`, `source="cure-
    # reconciliation"`) -- restoring the exact status the engine itself
    # persisted (`preCureStatuses`) before the reopen, never a hat's
    # free-form claim. That is what lets the declared edge be
    # variant-independent (`transitions.UN_REOPEN_TASK_STATUS_TARGETS`,
    # all six targets in the BASE table) without widening what anyone
    # else may ask for: every other caller (an operator amendment ruling,
    # the steer hat's `task-update`, a hat's contract report) keeps the
    # ordinary refusals -- `superseded` out of any status, and the OTHER
    # variant's claim terminal out of `pending`.
    status_errors = transitions.task_status_report_errors(
        {"task_status": target_status, "justification": effective_justification},
        current, vigilant,
        allow_un_reopen_restore=(source == "cure-reconciliation"))
    if status_errors:
        record.update(applied=False, reason=status_errors[0])
        return record
    task["status"] = target_status
    if current == "failed" and target_status == "skipped":
        task["notes"] = effective_justification
    run.write_tasks(data)
    if transitions.is_un_reopen_transition(current, target_status):
        run.emit(UN_REOPEN_EVENT, taskId=task_id, previous=current,
                 status=target_status, source=source, reason=reason)
    else:
        run.emit("amendment_status_applied", taskId=task_id, field="status",
                previous=current, status=target_status, source=source,
                file=file, reason=reason, justification=effective_justification)
    record.update(applied=True, reason="")
    return record


def apply_amendment_status_rulings(run: "RunDir", config_root: Path,
                                   vigilant: bool) -> list[dict]:
    """Task 029 (#26, requirement E): the single engine-side applier for an
    operator's typed status ruling -- the E half of task 028's channel
    that, before this, only bound review (`_operator_amendment_review_
    context`) and never touched `tasks.json` at all (PRD: *"That is why the
    `deck-phase3g` operator's amendment did nothing for the scheduler and a
    prompt-overriding steer was still needed"*).

    Reads every `mode: status` entry recorded under
    `<config_root>/amendments/` (`amendment_entries`, the same unforgeable
    `:ro`-mounted channel `_operator_amendment_review_context` already
    reads) and, for each one whose named task exists in `run`'s current
    `tasks.json`, routes it through `apply_task_status_change` (`source=
    "operator"`, `reason=` the ruling's own text, `file=` its own name) --
    task 010 (requirement H) extracted the actual legality-check/write/
    audit-event logic out of this function into that one, so this is now
    a thin driver over `amendment_entries`, not a second copy of the rules.

    Returns one record per `mode: status` entry found, oldest first, each
    `{"file", "taskId", "status", "previous", "applied", "reason"}` --
    `reason` is `""` for an applied ruling, and one of `"no matching
    task"` / `"already at that status"` / `"illegal transition"`
    otherwise, so a caller can report every ruling's fate without
    re-deriving it.
    """
    entries = [e for e in amendment_entries(config_root)
              if e.get("mode") == AMENDMENT_MODE_STATUS and e.get("status")]
    if not entries:
        return []
    results: list[dict] = []
    for e in entries:
        task_id = e.get("taskId")
        target = e.get("status")
        record = apply_task_status_change(run, task_id, target, vigilant,
                                          source="operator",
                                          reason=e.get("text") or "",
                                          file=e.get("file"))
        record["file"] = e.get("file")
        results.append(record)
    return results


# ---------------------------------------------------------------------------
# Task 013 (v0.23 PRD requirement A.2, #224): the OPERATOR ANSWER CHANNEL --
# `ralphctl rule <run> --file ruling.md --name <slug>`, `POST /rule` on the
# container API, and the hub run page's textarea -- all three land a ruling
# into ONE run-scoped cumulative record, `<run_root>/rulings/NNN-<name>.md`.
#
# Deliberately NOT `amendments/` and NOT `config_root`: unlike an `amend`
# ruling (waives/replaces a task's `successCriteria`, unforgeable because it
# lives on the host's `:ro` mount), an operator RULING here answers a
# specific rejection review is about to archive the approach over, and the
# hub's `POST /api/runs/<id>/rule` proxies straight to the run's own
# container API -- a route that has to be able to WRITE the record from
# inside the container, which a `:ro` mount can never do. Storage is
# `run_root` (bind-mounted read-write, the same directory `steering/`
# already lives under), and `ralphctl rule` reaches it the same way `ralphctl
# steer` reaches `steering/`: through the live engine's own API
# (`POST /rule`), never a second, host-side writer -- one writer
# (`write_ruling`, called only from `RunDir.add_ruling`), never two ways to
# land the same fact.
#
# The record is keyed to `LoopSupervisor._review_rejection_signature`'s own
# signature text, NOT a task id (operator ruling 3, PRD requirement A.2):
# task ids restart at 001 after a replan, which is exactly how a task-keyed
# ruling (like an `amend` entry) becomes orphaned and reads as "NOT
# authoritative" to the next review -- a signature is content-derived and
# survives that trivially, by never mentioning a task id at all.
# `RunDir.add_ruling` resolves the CURRENT signature itself, from the same
# `status.json` `rejectionSignatures`/`approach` pair `_repeated_rejection_
# signature` reads -- the caller (the CLI, the API route, the hub) never
# passes one explicitly, so there is no ruling-text vocabulary for an
# operator to get wrong. `signature=None` (no rejection has been recorded
# for the run yet, e.g. a ruling filed ahead of time) is still written and
# still rendered -- see `LoopSupervisor._operator_ruling_review_context` --
# just with no specific signature to scope it to.
RULING_DIRNAME = "rulings"
RULING_GLOB = "[0-9][0-9][0-9]-*.md"


def rulings_dir(run_root: Path) -> Path:
    return Path(run_root) / RULING_DIRNAME


def write_ruling(run_root: Path, text: str, *, signature: str | None = None,
                 name: str | None = None) -> str:
    """Record one operator ruling as the next numbered file under
    `<run_root>/rulings/`. The ONLY writer -- called from `RunDir.
    add_ruling`, itself called from `POST /rule` (the CLI goes through that
    same route, exactly like `ralphctl steer` goes through `POST
    /steering`), never a second, independent writer. Returns the file name
    written (`NNN-<name-or-'ruling'>.md`), the same identifier `ruling_
    entries` reports back.

    The numbering convention mirrors `add_steering`'s: a bare `--name`
    already carrying its own `NNN-` prefix (e.g. copy-pasted from a prior
    ruling filename) has it stripped before the engine's own prefix is
    applied, so the result is never doubled.

    The fixed header is `signature:` (present only when `signature` is not
    `None` -- absent, never the literal string `"None"`, so `ruling_
    entries` can tell "unscoped" from "scoped to the empty string") then
    `ts:`, a blank line, and the ruling text verbatim -- the same tiny,
    YAML-free shape `write_amendment` uses.
    """
    rdir = rulings_dir(run_root)
    rdir.mkdir(parents=True, exist_ok=True)
    existing = sorted(rdir.glob(RULING_GLOB))
    seq = int(existing[-1].name[:3]) + 1 if existing else 1
    suffix = name or "ruling"
    m = re.match(r"^\d{3}-(.+)$", suffix)
    if m:
        suffix = m.group(1)
    fname = f"{seq:03d}-{suffix}.md"
    header = ""
    if signature is not None:
        header += f"signature: {signature}\n"
    header += f"ts: {utcnow()}\n\n"
    atomic_write(rdir / fname, header + text.rstrip() + "\n")
    return fname


def ruling_entries(run_root: Path) -> list[dict]:
    """Every recorded ruling for this run, oldest first, as `{seq, file,
    signature, ts, text}` -- `signature` is `None` for an entry recorded
    with no rejection signature yet available (never the string `"None"`).
    The ONE reader, used by both `LoopSupervisor._operator_ruling_review_
    context` (in-container) and any future `ralphctl rule --list` /
    hub-panel view, so "what was ruled" is never described two ways.

    `[]` when `<run_root>/rulings/` does not exist at all -- the common
    case for a run nobody has ever ruled on.
    """
    rdir = rulings_dir(Path(run_root))
    if not rdir.is_dir():
        return []
    out: list[dict] = []
    for p in sorted(rdir.glob(RULING_GLOB)):
        try:
            raw = p.read_text()
        except OSError:
            continue
        header, _, body = raw.partition("\n\n")
        try:
            seq = int(p.name[:3])
        except ValueError:
            continue
        signature = None
        ts = None
        for line in header.splitlines():
            if line.startswith("signature:"):
                signature = line[len("signature:"):].strip()
            elif line.startswith("ts:"):
                ts = line[len("ts:"):].strip()
        out.append({"seq": seq, "file": p.name, "signature": signature,
                    "ts": ts, "text": body.rstrip("\n")})
    return out


# ---------------------------------------------------------------------------
# Task 010 (PRD requirement H, #98): the steer hat's tool surface.
#
# "The authority boundary is the tool surface, not the prompt" -- decided
# 2026-08-28: tasks and standing rules only, never budgets. Enforced here by
# WHAT TOOLS EXIST, declared ONCE as a name -> callable mapping so a test
# can assert the WHOLE set by equality (the same shape requirement A of
# v0.9 used for `engine.config.CONTROL_PLANE_ENV_VARS`, #62) -- a tool
# added to the steer hat later that is not also added to this exact dict
# fails that equality check instead of silently expanding what the hat can
# do by omission. There is deliberately no budget/iteration/approach/
# deadline tool in this set: the steer hat may reshape `tasks.json` and the
# standing rules and nothing else.
# Task 001 (PRD requirement D.3, #206): the one event type every
# `dependsOn` edge a writer below drops emits, naming the task, the
# rejected edge and the reason -- shared so the cure-insertion, `task-
# update` and `task-insert` paths all read identically on `events.jsonl`.
DEPENDENCY_EDGE_REJECTED_EVENT = "task.dependency_edge_rejected"

# #209 defect 7: the ACCEPTED-write sibling of the rejection event just
# above, for the one writer that replaces an existing task's whole
# `dependsOn` list on a hat's own report rather than dropping a single bad
# edge -- `apply_worker_dependency_repoints` below. Same `taskId`/`reason`/
# `source` field names, so the two read as one family on `events.jsonl`.
#
# Task 003 (PRD requirement E.1): `_repoint_dependents_onto_cure_task`
# below is the SECOND writer, `source="cure-insertion"` -- the engine's
# own dependent-repointing on a cure insertion, rather than a worker's
# `proposed_task_updates` report, gets the same audit trail.
DEPENDENCY_REPOINTED_EVENT = "task.dependency_repointed"


def _emit_rejected_dependency_edges(run: "RunDir", task_id: str,
                                    rejected: list, source: str) -> None:
    """Emit one `DEPENDENCY_EDGE_REJECTED_EVENT` per entry of `rejected`
    (`plan_ingestion.validate_dependency_edges`'s own return shape,
    `{"edge", "reason"}`) -- `source` names which of the three writers
    this task validates dropped it (`"cure-insertion"`, `"task-update"`,
    `"task-insert"`).
    """
    for r in rejected:
        run.emit(DEPENDENCY_EDGE_REJECTED_EVENT, taskId=task_id,
                 edge=r.get("edge"), reason=r.get("reason"), source=source)


def steer_task_insert(run: "RunDir", task: dict) -> dict:
    """Insert one new task at the end of `tasks.json`'s task list. Always
    lands `pending` -- any `status` key the caller supplied is IGNORED,
    never written verbatim: a freshly-inserted task has never been worked,
    and handing it a status here would be exactly the kind of hand-written
    status field this module exists to keep out of every OTHER path too.

    Task 011 (PRD requirement I, #98): also always stamps `origin:
    "steer"` (`TASK_ORIGIN_STEER`), the same way and for the same reason it
    always forces `status` to `pending` -- any `origin` key the caller
    supplied is IGNORED and overwritten, because the fact being recorded is
    "the steer hat inserted this row", not whatever the tool call happened
    to say about itself.

    Task 001 (PRD requirement D.2/D.3, #206): a candidate `dependsOn` --
    e.g. the observed self-loop shape `["<the task's own id>"]` -- is run
    through `plan_ingestion.validate_dependency_edges` before the new task
    is ever appended. Every bad edge (a self-loop, a longer cycle, or a
    dependency on an id the table does not hold) is dropped, one event per
    drop, naming the task, the rejected edge and the reason; every other
    edge, and the insert itself, still lands -- a bad edge costs itself,
    never the new task.
    """
    data = run.read_tasks()
    tasks = data.setdefault("tasks", [])
    task_id = task.get("id")
    if any(isinstance(t, dict) and t.get("id") == task_id for t in tasks):
        return {"inserted": False, "reason": "id already exists", "taskId": task_id}
    new_task = dict(task)
    new_task["status"] = "pending"
    new_task["origin"] = TASK_ORIGIN_STEER
    candidate_depends_on = new_task.get("dependsOn")
    if isinstance(candidate_depends_on, list):
        accepted, rejected = plan_ingestion.validate_dependency_edges(
            tasks, task_id, candidate_depends_on)
        _emit_rejected_dependency_edges(run, task_id, rejected, "task-insert")
        new_task["dependsOn"] = accepted
    tasks.append(new_task)
    run.write_tasks(data)
    return {"inserted": True, "taskId": task_id}


def steer_task_delete(run: "RunDir", task_id: str) -> dict:
    """Remove one task by id. Refuses (writes nothing) while some OTHER
    task's `dependsOn` still names it -- the steer hat's own prompt already
    asks it to re-point or drop those first; this only stops the tool from
    silently orphaning a dependency if it doesn't.
    """
    data = run.read_tasks()
    tasks = data.get("tasks", [])
    if not any(isinstance(t, dict) and t.get("id") == task_id for t in tasks):
        return {"deleted": False, "reason": "no matching task", "taskId": task_id}
    referrers = [t.get("id") for t in tasks
                if isinstance(t, dict) and task_id in (t.get("dependsOn") or [])]
    if referrers:
        return {"deleted": False, "taskId": task_id,
                "reason": f"still depended on by {referrers}"}
    data["tasks"] = [t for t in tasks
                     if not (isinstance(t, dict) and t.get("id") == task_id)]
    run.write_tasks(data)
    return {"deleted": True, "taskId": task_id}


# The one separator `steer_task_update` appends a transition justification
# with. Named because the suppression test below has to recognize its OWN
# earlier output to stay idempotent (task 038). #209 defect 7:
# `apply_worker_dependency_repoints` is the second user -- it appends a
# re-point's mandatory `reason` to the row's `notes` through this same
# separator.
_JUSTIFICATION_SEPARATOR = "\n\n"


# Task 012 (PRD requirement D.1, #184): the two statuses the picker has
# already CLAIMED a task under -- `in-progress` (a worker is mid-attempt)
# and `validation-failed` (a worker attempt just failed and the task is
# eligible to be picked up again next). Both are alive, not terminal --
# unlike `pending`, which `TASK_ACTIONABLE_STATUSES` (state.py:2949) also
# counts as actionable -- so a `dependsOn` edit landing here can be
# invalidated by whatever the CURRENT attempt is doing with the graph
# right under it, with nobody re-reading the edit before it matters. A
# `dependsOn` write to a `pending` task stays legal without limit: nothing
# has claimed it yet.
TASK_CLAIMED_STATUSES: tuple[str, ...] = ("in-progress", "validation-failed")


def _depends_on_claimed_refusal(task_id: str, status: str) -> str:
    """The one error text a refused `dependsOn` write reads verbatim, named
    the task, its status, and the remedy (PRD requirement D.1) -- insert a
    replacement task, re-point dependents onto it, then let this task reach
    a terminal status on its own. Built in one place so the tool-broker
    refusal (`hat_tools._execute`), the steering-ledger notes
    (`LoopSupervisor._step_generic_hat`) and every test asserting on the
    wording read the identical string.
    """
    return (
        f"dependsOn refused: task {task_id!r} is {status!r} -- the picker "
        "has already claimed it, so its dependsOn cannot be rewritten out "
        "from under the attempt in progress. Insert a replacement task, "
        "re-point dependents onto the replacement, then let this task "
        "reach a terminal status (or be skipped) on its own.")


def steer_task_update(run: "RunDir", task_id: str, changes: dict,
                      vigilant: bool, *, justification: str | None = None
                      ) -> dict:
    """Update one task's fields. THE status-changing tool (PRD requirement
    H): a `status` key in `changes` is NEVER written directly here -- it is
    routed through `apply_task_status_change`, the same applier
    `apply_amendment_status_rulings` uses for an operator's own ruling, so
    exactly one place validates the transition, emits the audit event and
    preserves `validationAttempts`/`tierLadderExhausted`. Every other key
    (`title`, `successCriteria`, `dependsOn`, `priority`, ...) is a plain
    dict-merge write with no transition to validate.

    Task 017 (PRD requirement F.4, #147): `justification` -- the caller's
    top-level argument, distinct from any `changes["notes"]` key -- is
    threaded to `apply_task_status_change` so the ONE declared move that
    carries an extra condition (`failed -> skipped` requires a non-empty
    justification, `transitions.task_status_report_errors`) is actually
    enforced on this tool, and lands in the task's own `notes` the same
    way the worker contract channel's `_apply_reported_task_status`
    already does. When the hat's call carries no explicit `justification`,
    it defaults to `changes.get("notes")` -- the steer's own stated reason
    for the change, when it wrote one into the same call.

    Task 038 (the cure for task 017's residual gap, same requirement F.4):
    a call that carries BOTH an explicit `justification` AND a distinct
    `changes["notes"]` used to lose the justification -- the applier wrote
    it into `notes`, and the plain dict-merge below then overwrote that
    field with `changes["notes"]`, leaving the record of an applied
    `failed -> skipped` move with no justification in it at all. The
    justification is now APPENDED to the requested notes (never
    overwritten by them) in the same merge, as the last segment, so it is
    the authoritative tail of the task's final `notes`; every other
    requested field update still lands, and when no explicit
    justification was supplied (the default-to-`notes` case above) the
    text is already the notes' own and nothing is appended twice.

    The suppression test for that last case is a TAIL test, not a
    substring test (task 038's own residual gap): requested notes that
    merely MENTION the justification inside a longer sentence (`"The
    reason is GAP, with additional context."` for justification `"GAP"`)
    still get the justification appended, because a mention buried
    mid-sentence is not the authoritative tail the move's record needs.
    Only two shapes suppress the append -- notes that ARE the
    justification (the default-to-`notes` case, left byte-identical) and
    notes that already end with the exact separator + justification this
    function itself would append (so applying the rule twice is a no-op).
    """
    changes = dict(changes)
    target_status = changes.pop("status", None)
    status_result = None
    justification_to_keep = None
    if target_status is not None:
        effective_justification = justification
        if not effective_justification:
            effective_justification = changes.get("notes")
        status_result = apply_task_status_change(
            run, task_id, target_status, vigilant, source="steer",
            justification=effective_justification or "")
        if (status_result.get("applied")
                and status_result.get("previous") == "failed"
                and target_status == "skipped"):
            # the applier just wrote this into `notes`; keep it there
            # through the merge below (task 038).
            justification_to_keep = (effective_justification or "").strip()
    if justification_to_keep and "notes" in changes:
        requested = (changes.get("notes") or "").strip()
        already_the_tail = (
            requested == justification_to_keep
            or requested.endswith(_JUSTIFICATION_SEPARATOR
                                  + justification_to_keep))
        if not already_the_tail:
            changes["notes"] = _JUSTIFICATION_SEPARATOR.join(
                part for part in (requested, justification_to_keep) if part)
    depends_on_result = None
    if changes:
        data = run.read_tasks()
        tasks = data.get("tasks", [])
        by_id = {t.get("id"): t for t in tasks if isinstance(t, dict)}
        task = by_id.get(task_id)
        if task is None:
            return {"updated": False, "reason": "no matching task",
                    "taskId": task_id, "statusResult": status_result}
        # Task 012 (PRD requirement D.1, #184): a `dependsOn` write to a
        # task the picker has already CLAIMED (`TASK_CLAIMED_STATUSES`) is
        # refused -- popped out of `changes` before the plain dict-merge
        # below, never written -- because the graph a claimed task's own
        # attempt is executing against must not be rewritten out from under
        # it mid-attempt. Every OTHER requested field in the same call still
        # lands; this refuses exactly the one hazardous key, the same
        # shape the status-move refusal above already uses (one bad field
        # does not sink the rest of the call). A `pending` task (not yet
        # claimed) is unaffected -- `current_status` there is never in
        # `TASK_CLAIMED_STATUSES` and `depends_on_result` stays None.
        if "dependsOn" in changes:
            current_status = task.get("status")
            if current_status in TASK_CLAIMED_STATUSES:
                depends_on_result = {
                    "applied": False,
                    "taskId": task_id,
                    "status": current_status,
                    "reason": _depends_on_claimed_refusal(
                        task_id, current_status),
                }
                changes = {k: v for k, v in changes.items()
                          if k != "dependsOn"}
            else:
                # Task 001 (PRD requirement D.2/D.3, #206): the candidate
                # `dependsOn` -- e.g. the observed self-loop shape
                # `["<the task's own id>"]` -- is validated edge by edge
                # against the rest of `tasks` before it is written. A bad
                # edge (a self-loop, a longer cycle, or a dependency on an
                # id the table does not hold) is dropped and reported --
                # never the whole write -- with one `task.dependency_edge_
                # rejected` event per drop naming the task, the edge and
                # the reason (`plan_ingestion.validate_dependency_edges`
                # is the shared cycle detector `_detect_cycles` itself
                # uses, D.5: neither its call site nor its messages
                # change).
                candidate_depends_on = changes.get("dependsOn")
                if isinstance(candidate_depends_on, list):
                    accepted, rejected = plan_ingestion.validate_dependency_edges(
                        tasks, task_id, candidate_depends_on)
                    _emit_rejected_dependency_edges(
                        run, task_id, rejected, "task-update")
                    changes["dependsOn"] = accepted
                    depends_on_result = ({"applied": True,
                                          "rejectedEdges": rejected}
                                         if rejected else {"applied": True})
                else:
                    depends_on_result = {"applied": True}
        # Task 013 (PRD requirement D.3, #184): the audit of every OTHER
        # non-`status` field this merge can land on a CLAIMED task.
        # `title`/`notes`/`priority` are plain display/bookkeeping fields
        # nothing in the picker or the worker loop reads back off a
        # CLAIMED task's own record mid-attempt -- `priority` only
        # re-enters the picture as a tie-break among MULTIPLE claimed or
        # pending tasks competing for the NEXT iteration
        # (`scheduler.select_next_task`), never as something this task's
        # own already-dispatched attempt executes against -- so none of
        # the three carry `dependsOn`'s hazard and none is guarded.
        # `successCriteria` is the one field that DOES carry the exact
        # same hazard `dependsOn` does (it is literally the text a claimed
        # attempt is executing against, mid-attempt) -- see fix #132 just
        # below, which is what keeps it safe rather than a new guard.
        #
        # Fix #132: a steer-tool edit to successCriteria is operator-origin
        # and just as legal as a judge-authored criteria amendment
        # (`LoopSupervisor._apply_criteria_amendments`, loop.py:6891-6898).
        # Move criteriaFingerprint and criteriaOfRecord forward atomically
        # with the text, in this SAME write_tasks call -- otherwise
        # `_check_criteria_edits` (loop.py:7253-7321) sees the mismatch on
        # the very next pass, RESTORES the old text, emits
        # `task.criteria_edit_refused` and sets the sticky
        # `criteriaEditedAfterValidationFailure` marker, undoing the edit
        # and dooming the approach on every later review. The digest here
        # is computed with the literal same formula as
        # `LoopSupervisor._criteria_fingerprint` (loop.py:7213-7215,
        # `sha256((text or "").encode("utf-8")).hexdigest()`) -- not
        # imported from loop.py, which imports this module and would make
        # the import circular.
        if "successCriteria" in changes:
            new_criteria = changes["successCriteria"]
            changes["criteriaFingerprint"] = hashlib.sha256(
                (new_criteria or "").encode("utf-8")).hexdigest()
            changes["criteriaOfRecord"] = new_criteria
        if changes:
            task.update(changes)
            run.write_tasks(data)
    return {"updated": True, "taskId": task_id, "statusResult": status_result,
            "dependsOnResult": depends_on_result}


def steer_standing_rules(run: "RunDir", text: str) -> dict:
    """Amend the pinned standing-rules block. A callable of its own (not a
    bare re-export of `RunDir.write_standing_rules`) so `STEER_HAT_TOOLS`
    below is a table of tools, every entry the same shape.
    """
    run.write_standing_rules(text)
    return {"written": True}


STEER_HAT_TOOLS = {
    "task-insert": steer_task_insert,
    "task-update": steer_task_update,
    "task-delete": steer_task_delete,
    "standing-rules": steer_standing_rules,
}


# ---------------------------------------------------------------------------
# Task 019 (#18.1): one iteration's story, shaped ONCE.
#
# `iterations/NNNN/meta.json` has always held everything an operator asks when
# something looks wrong in iteration 47 -- phase, model, start/end, exit code,
# the raw failure signals and that iteration's token/cost usage -- and no
# surface rendered it: `ralphctl logs --iteration 47` served the transcript,
# the hub timeline showed a summary row, and the *why did this one end like
# that* had to be reconstructed from exitCode/timedOut/noTrafficTimeout/error
# by hand. `iteration_detail()` below is the single shaping of that record,
# and `format_exit_reason()` the single wording of the verdict, so `ralphctl
# iteration` (task 019) and the hub's iteration dialog (task 020) cannot grow
# two vocabularies for the same file -- the discipline `steering_entries`
# above and `format_task_column` below already follow.
#
# Note this is a pure on-disk read with no live-API fallback, unlike the plan
# (`read_tasks_doc`) or the steering list: `meta.json` is written by the ENGINE
# itself, atomically, at the start and the end of every iteration, so the run
# dir is the authoritative copy even while the container is alive (the engine's
# own `GET /iterations/{n}` just serves this file back).

# The verdict vocabulary. `unknown` is the honest answer for an iteration whose
# meta.json is missing or truncated -- ignorance, not a clean exit (#15's rule
# applied to a second reader).
EXIT_REASON_UNKNOWN = "unknown"
EXIT_REASON_RUNNING = "still running"
EXIT_REASON_CLEAN = "clean exit"
# Steering 004: NOT "interrupted by operator" -- `IterationResult.interrupted`
# is set by any delivered signal (POST /interrupt, POST /abort, the engine's own
# give-up, a SIGTERM from outside the container), and this reader cannot tell
# which. It says what it knows; `ralphctl fault` says the rest.
EXIT_REASON_INTERRUPTED = "interrupted (a signal ended the iteration)"
EXIT_REASON_NO_TRAFFIC = "no-traffic timeout (the model never answered)"
EXIT_REASON_TIMEOUT = "iteration timeout"
# Longest error text carried into the one-line reason; the full string stays in
# the `error` field for anyone who wants all of it.
EXIT_REASON_ERROR_MAX = 200

# Rendered for an iteration that recorded no usage at all (a crash before the
# first token) -- for its tokens and for its cost. Never "0 tokens" and never
# "$0.00": nobody counted.
USAGE_NONE = "(none)"
TOKEN_LABELS = (("input", "in"), ("output", "out"),
                ("cacheRead", "cache read"), ("cacheWrite", "cache write"))

# Keys `iteration_detail` derives itself. Stripped from the meta.json passthrough
# first, so a hand-edited (or hostile) meta.json cannot smuggle in a display
# string the raw fields do not support -- the `_with_approach_display`
# discipline, applied at the source this time.
ITERATION_DERIVED_KEYS = (
    "hasMeta", "startedAtLocal", "endedAtLocal", "durationS", "durationDisplay",
    "durationLabel", "exitReason", "costDisplay", "costStatus", "tokensDisplay",
    "hasTranscript", "transcriptBytes", "hasPrompt", "promptBytes",
    "contractFault",
    # v0.13 task 013 (#89, requirement H): this iteration's own fault CAUSE,
    # re-derived (never persisted) through `faults.explain_fault` -- see
    # `iteration_detail`'s own note below for why a hand-edited/hostile
    # meta.json cannot smuggle a cause in by writing these directly.
    "reason", "signature", "notices",
    # v0.13 task 015 (#89, requirement I, hub half): the ONE rendered cause
    # sentence (`<faultClass> -- <reason>`), so the hub timeline row and the
    # CLI/dialog's `cause:` line format the SAME string from the SAME
    # `reason`/`faultClass` instead of each spelling the join themselves.
    "causeText",
    # #290: the one rendered `thinking: <level>` / `effort: <level>` string.
    "thinkingDisplay",
)

# Whether a duration is final or still growing (`ralphctl status`' own words).
DURATION_TOTAL = "total"
DURATION_ELAPSED = "elapsed"

# Said out loud for an iteration dir whose meta.json is absent or truncated:
# the transcript is still readable, the metadata is genuinely unknown, and a
# row of `None`s would look like recorded facts.
ITERATION_NO_META_NOTICE = ("!! no readable meta.json for this iteration "
                            "(nothing known but the transcript)")

# Task 020 (#81): the "input" region's one wording for an iteration dir that
# never got a `prompt.md` (or whose file is blank) -- the `NO_TRANSCRIPT`/
# `RUN_DOCUMENT_ABSENT` discipline applied a third time, so the region reads
# like an explained absence rather than a blank box.
ITERATION_NO_PROMPT_NOTICE = "(no prompt.md recorded for this iteration)"

# Task 020 (#81): the three on-disk faces of one iteration, labelled the SAME
# one word everywhere (`ralphctl iteration`, the hub's dialog and their shared
# tests) so a region cannot be named differently on the two surfaces.
ITERATION_REGION_OUTCOME = "outcome"
ITERATION_REGION_INPUT = "input"
ITERATION_REGION_LOGS = "logs"


def _one_line(text: str, limit: int) -> str:
    """Collapse whitespace and truncate -- an error message is arbitrary text
    (it can be a whole traceback) and this goes on one line."""
    flat = " ".join(str(text).split())
    return flat if len(flat) <= limit else flat[: limit - 3] + "..."


def format_exit_reason(meta: dict | None) -> str:
    """Why iteration `meta` ended, in one line (task 019, #18.1).

    The raw signals `loop._run_iteration_once` records are ranked, first match
    winning, because they overlap (a timed-out iteration also has an exit code,
    an interrupted one usually has an error message):

      no/unreadable meta -> `unknown`   (nothing is claimed)
      no `endedAt`       -> `still running`
      `interrupted`      -> operator interrupt (`POST /interrupt`, abort)
      `noTrafficTimeout` -> the startup watchdog fired: no tokens at all
      `timedOut`         -> the per-iteration limit fired
      `error`            -> `error (exit N): <message>`
      `exitCode == 0`    -> `clean exit`
      other int exitCode -> `exit N`

    A non-null `faultClass` (`engine/faults.py`' verdict, the reason an attempt
    was retried and refunded) is appended as ` [infra fault]`/` [work fault]`
    rather than replacing the signal it was derived from. Defensive like
    `format_duration`: junk in, a word out, never an exception -- this feeds a
    renderer.
    """
    if not isinstance(meta, dict):
        return EXIT_REASON_UNKNOWN
    reason = _exit_reason_core(meta)
    fault = meta.get("faultClass")
    if isinstance(fault, str) and fault:
        reason += f" [{fault} fault]"
    return reason


def _exit_reason_core(meta: dict) -> str:
    if not meta.get("endedAt"):
        return EXIT_REASON_RUNNING
    if meta.get("interrupted"):
        return EXIT_REASON_INTERRUPTED
    if meta.get("noTrafficTimeout"):
        return EXIT_REASON_NO_TRAFFIC
    if meta.get("timedOut"):
        return EXIT_REASON_TIMEOUT
    code = meta.get("exitCode")
    code_is_int = isinstance(code, int) and not isinstance(code, bool)
    error = meta.get("error")
    if error:
        detail = _one_line(error, EXIT_REASON_ERROR_MAX)
        return (f"error (exit {code}): {detail}" if code_is_int
                else f"error: {detail}")
    if code == 0 and code_is_int:
        return EXIT_REASON_CLEAN
    if code_is_int:
        return f"exit {code}"
    return EXIT_REASON_UNKNOWN


def format_tokens(usage: dict | None) -> str:
    """One iteration's token counters for humans (task 019, #18.1):
    `180,661 total (in 18, out 2,118, cache read 136,849, cache write 41,676)`.

    Only counters actually present are named -- a provider that reports no
    cache split gets no zeroed cache fields invented for it -- and a usage
    dict with nothing countable renders `TOKENS_NONE`, never `0 tokens`.
    Cost is deliberately NOT part of this string: it goes through
    `format_cost`, the one formatter that knows what is unknown (`USAGE_NONE`
    is shared with it for the nothing-recorded case).
    """
    if not isinstance(usage, dict):
        return USAGE_NONE

    def count(key):
        raw = usage.get(key)
        if raw is None or isinstance(raw, bool):
            return None
        try:
            return int(raw)
        except (TypeError, ValueError):
            return None
    parts = [f"{label} {n:,}" for key, label in TOKEN_LABELS
             if (n := count(key)) is not None]
    total = count("totalTokens")
    if total is None and not parts:
        return USAGE_NONE
    head = f"{total:,} total" if total is not None else "total unreported"
    return f"{head} ({', '.join(parts)})" if parts else head


def format_iteration_log_header(count: int) -> str:
    """The separator between one iteration's header block and its transcript
    (task 019/020, #18.1) -- worded here so `ralphctl iteration` and the hub's
    iteration dialog announce the same line count the same way."""
    return f"--- log ({count} lines) ---"


def iteration_summary_lines(detail: dict) -> list[str]:
    """One iteration's header block as labelled text lines (tasks 019/020,
    #18.1): the `iteration_detail` payload rendered for a human, ONCE.

    `ralphctl iteration <run> <n>` prints these under its own `run:` line and
    the hub's iteration dialog (task 020) shows the very same lines inside
    `openTextDialog`, so the two surfaces cannot word the same meta.json
    differently -- the discipline `format_task_column`/`format_approach`
    already follow, applied to a whole block instead of one cell.

    Every value here is a display string `iteration_detail` already derived
    (`durationDisplay`, `exitReason`, `tokensDisplay`, `costDisplay`, the
    `*Local` instants), and a line is OMITTED rather than printed empty when
    the underlying fact was never recorded -- an absent `endedAt` means the
    iteration has no end, not an end of `None`.
    """
    if not isinstance(detail, dict):
        return []
    head = f"iteration: {detail.get('number')}"
    if detail.get("phase"):
        head += f"  phase {detail['phase']}"
    if detail.get("approach") is not None:
        head += f"  approach {detail['approach']}"
    lines = [head]
    if not detail.get("hasMeta"):
        lines.append(ITERATION_NO_META_NOTICE)
    if detail.get("startedAtLocal"):
        lines.append(f"started:   {detail['startedAtLocal']}")
    if detail.get("endedAtLocal"):
        lines.append(f"ended:     {detail['endedAtLocal']}")
    lines += [
        f"duration:  {detail.get('durationDisplay')}  ({detail.get('durationLabel')})",
        f"exit:      {detail.get('exitReason')}",
    ]
    # v0.13 task 014 (#89, requirement I): THIS iteration's own fault cause --
    # `reason` re-derived by `iteration_detail` through `faults.explain_fault`
    # (task 013) -- worded `<faultClass> -- <reason>` (task 015's `causeText`,
    # the ONE join both this CLI/dialog line and the hub timeline row's cause
    # span read) so the sentence names WHAT the engine classified it as
    # (`infra`/`work`/`signal`) alongside WHY, never a bare `error:` line that
    # could be read as raw text with no verdict attached. `faultClass` is the
    # engine's own recorded verdict (never the re-derivation); a disagreement
    # between the two is said out loud via `notices`
    # (`FAULT_VERDICT_DIVERGED_NOTICE`), the same wording `fault_summary_lines`
    # uses for the run's last fault.
    if detail.get("reason"):
        lines.append(f"cause:     {detail.get('causeText')}")
        lines += list(detail.get("notices") or [])
    # The model pi actually used, with the raw gateway id only when it adds
    # information -- exactly `ralphctl status`' model line (task 012).
    model = detail.get("modelResolved") or detail.get("model")
    if model:
        model_line = f"model:     {model}"
        if detail.get("modelRaw") and detail["modelRaw"] != model:
            model_line += f"  (gateway id: {detail['modelRaw']})"
        # v0.31 (#285): the agent harness beside the model, when recorded
        # (iterations from before v0.30 carry none and print as before).
        if isinstance(detail.get("harness"), str) and detail["harness"]:
            model_line += f"  \u00b7 {detail['harness']}"
        thinking_display = detail.get("thinkingDisplay")
        if isinstance(thinking_display, str) and thinking_display:
            model_line += f"  \u00b7 {thinking_display}"
        lines.append(model_line)
    elif isinstance(detail.get("harness"), str) and detail["harness"]:
        harness_line = f"harness:   {detail['harness']}"
        thinking_display = detail.get("thinkingDisplay")
        if isinstance(thinking_display, str) and thinking_display:
            harness_line += f"  \u00b7 {thinking_display}"
        lines.append(harness_line)
    elif detail.get("thinkingDisplay"):
        lines.append(str(detail["thinkingDisplay"]))
    lines += [
        f"tokens:    {detail.get('tokensDisplay')}",
        f"cost:      {detail.get('costDisplay')}",
    ]
    steering = detail.get("steeringConsumed") or []
    if steering:
        lines.append(f"steering:  {', '.join(str(s) for s in steering)}")
    # Task 018 (#34): notes this iteration was handed but did not consume --
    # it failed/was interrupted, so they are still pending for a later
    # iteration. Rendered only for that difference: a clean iteration's line
    # is unchanged (delivered == consumed says nothing extra).
    stranded = [s for s in (detail.get("steeringDelivered") or [])
                if s not in steering]
    if stranded:
        lines.append(f"steering:  {', '.join(str(s) for s in stranded)}  "
                     f"(delivered, not consumed -- still pending)")
    if detail.get("verifiedTask"):
        lines.append(f"verified:  task {detail['verifiedTask']} "
                     f"-> {detail.get('verifyOutcome')}")
    # Task 019 (#44): this slot was attempted more than once -- an engine died
    # in it and the resumed engine reused the number. Rendered only for that
    # difference, and it names where the earlier attempt's files went so the
    # operator can read the crash rather than wonder whether it was eaten.
    archived = detail.get("archivedAttempts") or 0
    if archived:
        lines.append(f"attempts:  {archived} earlier "
                     f"{'attempt' if archived == 1 else 'attempts'} archived "
                     f"under {ITERATION_ATTEMPTS_DIR}/ (killed mid-iteration; "
                     f"prompt, transcript and meta.json kept)")
    cf = detail.get("contractFault")
    if cf:
        # v0.13 task 012 (#92): the SAME sentence
        # (`contract_enforcement.violation_message`) `LoopSupervisor` uses
        # for `self._abort_reason`/`status.json`'s `reason` when the ladder
        # never recovers before the run's own budget is gone -- shown here
        # for THIS iteration whether it recovered (no agent fault) or not.
        lines.append(f"contract:  {cf['text']}")
    return lines


# v0.13 task 012 (#92, requirement G's second half): joining
# `contract.violation`/`contract.agent_fault` events back to the specific
# iteration they happened during, with no new persisted field.
#
# Why a join, not a stored field: `contract_enforcement.enforce_contract`'s
# retry ladder runs from inside `LoopSupervisor._step_generic_hat`/`_step_
# worker` AFTER `run_iteration()` has already returned and written that
# iteration's own `meta.json`/`iteration.end` event -- every re-ask attempt
# beyond the first is a deliberately UNCHARGED `_ask_contract_fix` call, not
# a fresh `run_iteration()`, so it never gets an `iteration.start`/`end` of
# its own. That means every `EVENT_VIOLATION`/`EVENT_AGENT_FAULT` event for
# iteration N is emitted strictly AFTER `iteration.end number=N` and
# strictly BEFORE the next `iteration.start` (any number) -- or before the
# run's own end, for the last iteration ever taken. Bucketing events between
# those two boundaries (by their monotonic `id`, not `ts`: two events can
# share a timestamp) attributes a violation to its iteration without the
# engine having to stamp one on -- the `ITERATION_DERIVED_KEYS` discipline
# (derive it from what is already on disk, once) applied to requirement G's
# own events.
_CONTRACT_EVENT_TYPES = (contract_enforcement.EVENT_VIOLATION,
                        contract_enforcement.EVENT_AGENT_FAULT)


def contract_fault_for_iteration(run_root: Path, number: int) -> dict | None:
    """The contract violation(s), if any, that happened during iteration
    `number` -- read straight off `events.jsonl` (see the module note just
    above for how the join works), or None when this iteration recorded
    none at all.

    Returns a dict:
      `hat`         which hat broke its contract,
      `errors`      the LAST attempt's own validation errors, verbatim,
      `attempts`    the last violation's own 1-based attempt number,
      `maxAttempts` the ladder's declared cap (`contract_retry_max + 1`),
      `agentFault`  True iff the ladder never recovered for THIS iteration
                    (an `EVENT_AGENT_FAULT` was emitted) -- False for an
                    iteration that got a violation re-asked and corrected,
      `text`        the one human sentence
                    (`contract_enforcement.violation_message`), verbatim --
                    the SAME function `LoopSupervisor` calls to build
                    `self._abort_reason` (`status.json`'s `reason`,
                    `ralphctl status`'s `reason:` line) when the ladder ends
                    the run outright, so the two surfaces can never word the
                    same fact two different ways.
    """
    events = read_events(run_root)
    end_id = None
    for ev in events:
        if ev.get("type") == "iteration.end" and ev.get("number") == number:
            eid = ev.get("id")
            if isinstance(eid, int):
                end_id = eid  # last match wins: a re-attempted slot's LATEST end
    if end_id is None:
        return None
    next_start_id = None
    for ev in events:
        eid = ev.get("id")
        if (isinstance(eid, int) and eid > end_id
                and ev.get("type") == "iteration.start"):
            next_start_id = eid
            break
    window = [ev for ev in events
             if isinstance(ev.get("id"), int) and ev["id"] > end_id
             and (next_start_id is None or ev["id"] < next_start_id)
             and ev.get("type") in _CONTRACT_EVENT_TYPES]
    if not window:
        return None
    last = window[-1]
    hat = last.get("hat")
    errors = list(last.get("errors") or [])
    agent_fault = any(ev.get("type") == contract_enforcement.EVENT_AGENT_FAULT
                      for ev in window)
    violations = [ev for ev in window
                 if ev.get("type") == contract_enforcement.EVENT_VIOLATION]
    last_violation = violations[-1] if violations else None
    return {
        "hat": hat,
        "errors": errors,
        "attempts": (last_violation or {}).get("attempt"),
        "maxAttempts": (last_violation or {}).get("maxAttempts"),
        "agentFault": agent_fault,
        "text": contract_enforcement.violation_message(hat, errors),
    }


def format_thinking(thinking: str | None, effort: str | None) -> str:
    """`thinking: <level>` / `effort: <level>` for one iteration, or the empty
    string when neither was recorded (the bundle never carries both; thinking
    wins if a hand-edited meta.json names both)."""
    if thinking:
        return f"thinking: {thinking}"
    if effort:
        return f"effort: {effort}"
    return ""


def iteration_detail(run_root: Path, number: int) -> dict | None:
    """Everything known about one iteration of a run, or None when the run dir
    holds no such iteration (task 019, #18.1).

    `iterations/NNNN/meta.json` verbatim (so nothing the engine records is
    dropped -- `steeringDelivered`/`steeringConsumed`, `verifiedTask`/`verifyOutcome`,
    `modelResolved`/`modelRaw`, the raw failure signals) PLUS the derived
    display fields both detail surfaces need:

      `hasMeta`          False for an iteration dir whose meta.json is absent
                         or truncated -- the transcript is still readable, and
                         the fields simply are not known,
      `startedAtLocal`/`endedAtLocal`  absolute instants through the one shared
                         `format_local_time` (absent when there is no such
                         timestamp -- its `n/a` would read like a real one),
      `durationS`/`durationDisplay`    how long it ran (`elapsed_seconds` +
                         `format_duration`), with `durationLabel` saying which
                         it is -- `total` for a finished iteration, `elapsed`
                         for one still in flight (the wording `ralphctl
                         status`' duration line already uses),
      `exitReason`       `format_exit_reason` above,
      `thinking`/`effort`/`thinkingDisplay`  the recorded level (None when
                         absent) and `format_thinking`' string (`""` when
                         neither was recorded),
      `tokensDisplay`    `format_tokens` above,
      `costDisplay`      `format_cost(usage, decimals=4)` -- 4 decimals
                         because a single iteration is small money; a cost
                         nobody knows renders `unavailable`, never `$0.0000`,
      `costStatus`       `cost_status`' verdict (None/derived/partial/unknown),
      `hasTranscript`/`transcriptBytes`  whether this iteration wrote any
                         transcript at all (via `log_merge.
                         iteration_output_path`, which owns the file name; the
                         log itself is rendered by the caller through
                         `log_merge`/`cli.log_render`, where transcript
                         rendering lives),
      `archivedAttempts` how many EARLIER attempts at this same slot are
                         archived under `attempts/NN/` (task 019, #44) -- `0`
                         for the overwhelmingly common single-attempt
                         iteration,
      `contractFault`    the contract violation(s) this iteration itself
                         recorded, if any (`contract_fault_for_iteration`,
                         v0.13 task 012, #92) -- None for an iteration whose
                         hat never broke its response contract at all.
      `reason`/`signature`  THIS iteration's own fault cause, re-derived
                         through `faults.explain_fault` from its OWN
                         meta.json fields -- the same call `fault_explanation`
                         makes for the run's LAST fault, now made per
                         iteration (v0.13 task 013, #89) so an earlier fault's
                         cause stays reachable once a later one happens.
                         `faultClass` above is left exactly as recorded --
                         never overwritten by this re-derivation; a
                         disagreement is appended to `notices` instead
                         (`FAULT_VERDICT_DIVERGED_NOTICE`). Both are `None`
                         for an iteration that never recorded a fault.
      `causeText`        the ONE rendered join of the two above --
                         `f"{faultClass} -- {reason}"` -- or `None` when
                         `reason` is `None` (v0.13 task 015, #89, requirement
                         I, hub half). `iteration_summary_lines`' `cause:`
                         line and the hub timeline row's cause span both read
                         THIS field rather than each formatting the join
                         themselves, so a faulted iteration cannot be worded
                         two different ways across the CLI and the browser.

    The log lines are NOT part of this dict on purpose: rendering them is the
    job of `log_merge.iteration_lines` plus the shared renderer, and a caller
    that only wants the header (a hub timeline row, `--no-log`) must not pay
    for reading a 20 MB transcript.
    """
    d = iteration_dir(Path(run_root), number)
    if not d.is_dir():
        return None
    raw = read_json(d / "meta.json")
    has_meta = isinstance(raw, dict)
    meta = raw if has_meta else {}
    detail = {k: v for k, v in meta.items() if k not in ITERATION_DERIVED_KEYS}
    recorded = meta.get("number")
    detail["number"] = (recorded if isinstance(recorded, int)
                        and not isinstance(recorded, bool) else number)
    detail["hasMeta"] = has_meta
    for key in ("startedAt", "endedAt"):
        if detail.get(key):
            detail[key + "Local"] = format_local_time(detail[key])
    duration = elapsed_seconds(detail.get("startedAt"), detail.get("endedAt"))
    detail["durationS"] = round(duration, 3) if duration is not None else None
    detail["durationDisplay"] = format_duration(duration)
    # #290: the thinking/effort level the dispatch recorded (both optional --
    # an iteration from before the field, or a hat with neither, has none)
    # and the one display string the hub row, dialog and CLI all place.
    for key in ("thinking", "effort"):
        value = meta.get(key)
        detail[key] = value if isinstance(value, str) and value else None
    detail["thinkingDisplay"] = format_thinking(detail["thinking"], detail["effort"])
    # An unfinished iteration's duration is elapsed-so-far, not a total: said
    # out loud in the same two words `ralphctl status`' duration line uses,
    # rather than left for each surface to guess.
    detail["durationLabel"] = (DURATION_TOTAL if detail.get("endedAt")
                               else DURATION_ELAPSED)
    # `raw`, not `meta`: an absent/truncated meta.json is `unknown` (nothing is
    # known), while an existing one with no `endedAt` is `still running` -- the
    # empty dict must not silently promise the second.
    detail["exitReason"] = format_exit_reason(raw if has_meta else None)
    usage = detail.get("usage") if isinstance(detail.get("usage"), dict) else None
    detail["tokensDisplay"] = format_tokens(usage)
    detail["costDisplay"] = format_cost(usage, decimals=4) or USAGE_NONE
    detail["costStatus"] = cost_status(usage)
    try:
        size = iteration_output_path(run_root, number).stat().st_size
    except OSError:
        size = 0
    detail["transcriptBytes"] = size
    detail["hasTranscript"] = size > 0
    # Task 020 (#81): the "input" region's own header-only facts (paralleling
    # `hasTranscript`/`transcriptBytes` just above) -- a stat, never a read, so
    # a caller that only wants the timeline row does not pay for it.
    try:
        psize = iteration_prompt_path(run_root, number).stat().st_size
    except OSError:
        psize = 0
    detail["promptBytes"] = psize
    detail["hasPrompt"] = psize > 0
    detail["archivedAttempts"] = len(iteration_attempt_dirs(run_root, number))
    # v0.13 task 012 (#92): the contract violation(s) this iteration itself
    # recorded, if any -- joined from events.jsonl, never a new persisted
    # field (see `contract_fault_for_iteration`'s own module note).
    detail["contractFault"] = contract_fault_for_iteration(run_root, detail["number"])
    # v0.13 task 013 (#89, requirement H): re-derive THIS iteration's own
    # fault cause -- `reason` + `signature` -- through the exact same
    # `faults.explain_fault` call `fault_explanation` makes for the run's
    # LAST fault. #89's own correction comment is the point: the cause was
    # never missing from disk, only ever derived for the last faulted
    # iteration -- so #2's cause became unreachable the moment #40 faulted,
    # though #2's own meta still holds every input `explain_fault` needs.
    # `faultClass` is left exactly as the engine recorded it above (never
    # overwritten here); a disagreement is appended to `notices` instead of
    # silently preferred, same as `fault_explanation`'s own
    # `FAULT_VERDICT_DIVERGED_NOTICE`. Both `reason`/`signature`/`notices`
    # are in `ITERATION_DERIVED_KEYS`, so a hand-edited/hostile meta.json
    # cannot smuggle a fake cause in through the passthrough above.
    detail["reason"] = None
    detail["signature"] = None
    detail["notices"] = []
    detail["causeText"] = None
    recorded_fault = detail.get("faultClass")
    if isinstance(recorded_fault, str) and recorded_fault:
        usage = detail.get("usage") if isinstance(detail.get("usage"), dict) else None
        derived = explain_fault(
            error_text=str(detail.get("error") or ""),
            exit_code=(detail.get("exitCode")
                       if isinstance(detail.get("exitCode"), int)
                       and not isinstance(detail.get("exitCode"), bool) else None),
            interrupted=bool(detail.get("interrupted")),
            timed_out=bool(detail.get("timedOut")),
            no_traffic_timeout=bool(detail.get("noTrafficTimeout")),
            produced_traffic=billable_tokens(usage) > 0,
            **meta_provider_evidence(detail),
        )
        detail["reason"] = derived["reason"]
        detail["signature"] = derived["signature"]
        notice = _recorded_fault_notice(detail, recorded_fault, derived)
        if notice:
            detail["notices"].append(notice)
    if detail["reason"]:
        fault_class = detail.get("faultClass") or EXIT_REASON_UNKNOWN
        detail["causeText"] = f"{fault_class} -- {detail['reason']}"
    return detail


def read_iteration_prompt(run_root: Path, number: int) -> str:
    """One iteration's own prompt as sent to the agent -- task 020's (#81)
    "input" region.

    Read directly, unlike the transcript: `prompt.md` is plain markdown the
    ENGINE writes once at the start of the iteration
    (`engine.loop._run_iteration_once`), not a JSONL stream, so none of
    `log_merge`/`log_render`'s shaping applies -- this is the ONE place that
    reads it back, the `iteration_prompt_path` name-owning discipline applied
    to the read side too.

    An iteration dir with no `prompt.md` (or a blank one) answers with
    `ITERATION_NO_PROMPT_NOTICE` -- the `NO_TRANSCRIPT`/`RUN_DOCUMENT_ABSENT`
    discipline applied a third time, so the region says *why* it is empty
    instead of looking broken.
    """
    path = iteration_prompt_path(Path(run_root), number)
    try:
        text = path.read_text(errors="replace")
    except OSError:
        return ITERATION_NO_PROMPT_NOTICE
    return text if text.strip() else ITERATION_NO_PROMPT_NOTICE


def format_iteration_region_header(region: str, filename: str) -> str:
    """The separator line above one of the three region bodies (task 020,
    #81) -- `format_iteration_log_header`'s role, generalized to all three
    regions and worded the SAME everywhere: `ralphctl iteration`, the hub's
    dialog and their shared tests never invent a second spelling of these
    words."""
    return f"=== {region} ({filename}) ==="


def iteration_detail_text(detail: dict, *, prompt_text: str | None,
                          log_lines: list[str] | None) -> list[str]:
    """The complete, LABELLED story of one iteration as text lines (task 020,
    #81): outcome (`meta.json`) FIRST -- so an iteration that ended
    `exitCode: -2, interrupted: true` is readable without scrolling past the
    transcript -- then input (its prompt) then logs (its transcript).

    `ralphctl iteration` and the hub's dialog (`ui_server.iteration_view`)
    both build their printed/rendered text by calling this ONE function on the
    SAME inputs (`iteration_summary_lines(detail)` for the outcome body,
    `read_iteration_prompt` for the input body, the shared `log_render`
    output for the logs body), so a region cannot be labelled, ordered or
    worded differently on the two surfaces.

    `log_lines` is None (not an empty list) exactly when the caller asked for
    no transcript (`ralphctl iteration --no-log`, `?log=0`) -- the logs
    region is then OMITTED entirely, the `iteration_view`/`cmd_iteration`
    rule already applied to the bare `log` key.
    """
    lines = [format_iteration_region_header(ITERATION_REGION_OUTCOME, "meta.json"),
             *iteration_summary_lines(detail), "",
             format_iteration_region_header(ITERATION_REGION_INPUT, PROMPT_FILENAME),
             *(prompt_text or ITERATION_NO_PROMPT_NOTICE).splitlines()]
    if log_lines is not None:
        lines += ["",
                  format_iteration_region_header(ITERATION_REGION_LOGS, OUTPUT_FILENAME),
                  format_iteration_log_header(len(log_lines)),
                  *log_lines]
    return lines


# ---------------------------------------------------------------------------
# Task 002 (#15): the hardened tasks.json read path.
#
# `tasks.json` is written by the AGENT (pi, per prompts/worker.md), not by the
# engine, so no reader can assume it was written atomically: a poll that lands
# inside the agent's write window sees a truncated file. Every reader used to
# turn that `JSONDecodeError` into its default ({} / []), which is how "the
# plan vanished for one poll cycle" reached the hub table, the /status task
# counts and `ralphctl tasks`. Unknown is not zero -- the same principle #10
# established for cost.
#
# The fix lives in the READ path (one place, every surface), and does three
# things:
#   1. bounded re-read on a parse failure (the write window is milliseconds);
#   2. serve the LAST SUCCESSFULLY PARSED payload, flagged stale, when it
#      still will not parse;
#   3. distinguish the three cases that collapsed into one default --
#      absent (no plan yet: empty really is correct), unparseable (serve
#      last-good, flag stale), parsed-but-empty (empty really is correct).
#
# Why this is NOT a mirror: the last-good payload is kept in memory, and the
# on-disk cache (`<run-dir>/.tasks-last-good.json`) is written ONLY at the
# moment a read actually fails -- never on the happy path. So the engine never
# maintains a second copy of the plan that could drift from, or be mistaken
# for, `tasks.json` itself; the cache exists purely so the fallback survives an
# engine restart, and its content is by construction something that was read
# out of `tasks.json` verbatim.
TASKS_LAST_GOOD_NAME = ".tasks-last-good.json"

# Bounded re-read budget: 4 attempts, 10ms apart (~30ms worst case). Long
# enough to outlast an agent's rewrite, short enough that a genuinely corrupt
# file does not stall a request.
TASKS_READ_ATTEMPTS = 4
TASKS_READ_DELAY = 0.01

# Task 004 (#15): the wording every surface uses for a stale/unreadable task
# read, kept here next to the reader that produces the condition -- same
# discipline as `log_merge.NO_TRANSCRIPT`, so `ralphctl tasks`, the hub run
# detail and any future viewer name the same fact the same way instead of each
# inventing a phrase. Retrieved through `TasksRead.notice`, never re-spelled.
TASKS_STALE_NOTICE = (
    "stale task list: tasks.json did not parse on the last read (an agent is "
    "rewriting it, or it is corrupt) -- showing the last plan that did parse")
TASKS_UNREADABLE_NOTICE = (
    "unreadable task list: tasks.json did not parse and no earlier plan was "
    "ever read -- this is ignorance, not an empty plan")
# Short form for tabular/badge surfaces (hub task table, CLI columns).
TASKS_STALE_LABEL = "stale"


def tasks_read_notice(source: str | None, stale: bool = False) -> str | None:
    """The human sentence for a task read described by `tasksSource` /
    `tasksStale`, or None on the happy path (`absent`/`file`: an empty plan
    there is a fact, and a surface must stay silent rather than cry wolf).

    Takes the two wire fields rather than a `TasksRead` so a surface holding
    only the *serialised* contract -- `ralphctl tasks` printing a live `GET
    /tasks` answer, the hub reading its own JSON -- reaches the same wording
    without fabricating a reader result.
    """
    if source == "last-good":
        return TASKS_STALE_NOTICE
    if source == "unreadable":
        return TASKS_UNREADABLE_NOTICE
    # An engine that flags staleness without naming a source still gets a
    # sentence: `tasksStale` is the field whose absence means "old engine".
    return TASKS_STALE_NOTICE if stale else None

_tasks_last_good_lock = threading.Lock()
_tasks_last_good: dict[str, dict] = {}


@dataclass(frozen=True)
class TasksRead:
    """Three-way result of reading a run dir's `tasks.json`.

    `source` is the distinction callers care about:
      * `"absent"`    -- the file is not there (no plan yet). `doc` is `{}`,
                         `stale` is False: an empty task list is the truth.
      * `"file"`      -- parsed straight off disk (possibly an empty plan;
                         that too is the truth). `stale` is False.
      * `"last-good"` -- the file would not parse; `doc` is the last payload
                         that did (this process's, or the on-disk cache left
                         by a previous engine). `stale` is True.
      * `"unreadable"`-- the file would not parse and there is no last-good
                         payload at all. `doc` is `{}` and `stale` is True:
                         the emptiness here is ignorance, not a fact, and
                         surfaces must label it rather than print 0.
    """

    doc: dict
    source: str
    stale: bool
    error: str | None = None

    @property
    def tasks(self) -> list:
        """The task list, always a list (`doc["tasks"]` when it is one)."""
        tasks = self.doc.get("tasks") if isinstance(self.doc, dict) else None
        return tasks if isinstance(tasks, list) else []

    @property
    def present(self) -> bool:
        """True when a `tasks.json` exists at all (parseable or not) -- i.e.
        when `total: 0` would be a claim about a plan rather than about the
        absence of one."""
        return self.source != "absent"

    @property
    def counts(self) -> dict:
        return task_counts(self.tasks)

    @property
    def failure_kinds(self) -> dict:
        """Task 025 (#33): `{task id: kind}` for the failed tasks of this read
        -- derived, so a pre-v0.7 plan answers too (`task_failure_kinds`)."""
        return task_failure_kinds(self.tasks)

    @property
    def blocked_by(self) -> dict:
        """Task 021 (#60 pillar 3): `{task id: [dead/doomed ancestor ids]}`
        for this read's tasks -- derived through `task_blocked_by()`, exactly
        the same pattern `failure_kinds` follows for a different overlay, and
        for the same reason: a plan written before this existed carries no
        such field at all, so there is nothing to fall back to but derive.

        Task 013 (PRD requirement C.1, #149): `include_petitioned=True` --
        this is the on-disk half of the four display surfaces, so a
        dependent of a `petitioned` task shows up here (and, through
        `payload` below, in `GET /tasks` and `ralphctl tasks`) with the
        petitioned task named as its cause, same map, same field."""
        return task_blocked_by(self.tasks, include_petitioned=True)

    @property
    def payload(self) -> dict:
        """Task 025 (#33): the plan as every surface SERVES it -- `GET /tasks`,
        `ralphctl tasks --json`'s on-disk path and the hub's run-detail payload
        -- so the three cannot answer differently.

        tasks.json verbatim, then the read's provenance (`contract`), then the
        derived failure kinds, then (task 021) the derived blocked-by map --
        all three appended LAST, so a plan key of the same name can forge
        none of them. Each derived key is omitted entirely when its map is
        empty: for a derived map, empty and absent state the same fact
        ("nothing failed/blocked here"), so an older engine's answer needs no
        translating.
        """
        out = {**self.doc, **self.contract}
        kinds = self.failure_kinds
        if kinds:
            out[TASK_FAILURE_KINDS_FIELD] = kinds
        blocked = self.blocked_by
        if blocked:
            out[TASK_BLOCKED_FIELD] = blocked
        return out

    @property
    def contract(self) -> dict:
        """Task 003 (#15): the two fields every surface that serves this read
        carries verbatim -- `GET /tasks`, `GET /status`, the hub's run
        payloads, `ralphctl tasks --json` -- so a stale read is labelled the
        same way wherever it surfaces, and never has to be inferred.

        `tasksStale` is ALWAYS present (False on the happy path), so its
        absence only ever means "an older engine wrote this", never "fresh";
        `tasksSource` carries which of the four cases produced the payload.
        Deliberately siblings of the counts rather than keys inside them:
        `/status`'s `tasks` dict is consumed by summarisers that iterate its
        items as counts (`cli/main.py:_summarize_tasks`), so a boolean in
        there would render as a bogus task status."""
        return {"tasksStale": self.stale, "tasksSource": self.source}

    @property
    def notice(self) -> str | None:
        """Task 004 (#15): the human sentence for this read, or None on the
        happy path -- delegated to `tasks_read_notice()`, which owns the
        wording so a surface holding only the serialised contract agrees."""
        return tasks_read_notice(self.source, self.stale)

    @property
    def row_fields(self) -> dict:
        """Task 013/015 (#21): the task-progress fields ONE run-list row
        carries, for every surface that lists runs -- the hub's `/api/runs`
        (`cli/ui_server.py:_row_tasks`) and `ralphctl runs`.

        Lives on the read rather than in either surface so the two cannot
        drift: same field names, same raw counts, same server-rendered
        strings, same `{tasksStale,tasksSource}` contract. Each surface still
        does its OWN read (the hub asserts exactly one per row, `persist=False`
        -- a viewer writes nothing into somebody else's run dir).

        Raw counts travel beside the rendered strings so a table can sort on
        progress numerically (`app.js taskRatio` / `cli/main.py _task_ratio`)
        instead of on cell text, exactly like `approach`/`approachDisplay`.
        """
        counts = self.counts
        fraction = format_task_fraction(counts)
        return {
            "tasksTotal": counts.get("total", 0),
            "tasksCompleted": counts.get("completed", 0),
            "tasksInProgress": counts.get("inProgress", 0),
            "tasksValidationFailed": counts.get("validationFailed", 0),
            # Task 025 (#33): the terminal-failure counts, and both meanings
            # of them, travel raw as well -- a row that can only be sorted on
            # `completed/total` cannot tell a plan that finished from one that
            # gave up. The rendered strings below already say it in words.
            "tasksFailed": counts.get("failed", 0),
            "tasksFailedValidationExhausted": counts.get(
                "failedValidationExhausted", 0),
            "tasksFailedRequirementUnmet": counts.get(
                "failedRequirementUnmet", 0),
            # Task 021 (#60 pillar 3): how many tasks the dependency graph
            # dooms, raw -- same reason the failure counts travel raw above,
            # a row sortable only on `completed/total` cannot tell a plan
            # that is merely mid-flight from one with a dead subtree in it.
            "tasksBlocked": counts.get("blocked", 0),
            # Task 019 (requirement L, #77/#78): the vigilant lifecycle's two
            # extra terminal/quasi-terminal statuses, raw, beside the
            # completed/inProgress/... set above -- the hub cannot render a
            # lifecycle it is never SENT, and parsing it back out of
            # `tasksSummary`'s rendered sentence would be exactly the kind of
            # second spelling this module exists to prevent.
            "tasksValidated": counts.get("validated", 0),
            "tasksAwaitingValidation": counts.get("awaitingValidation", 0),
            # Task 019 (#68 requirement G): the effective-tier ladder's worst
            # rung reached by ANY task in this plan, raw -- `None` when no
            # task carries a recognised `effectiveTier` at all (nothing has
            # ever escalated, or the plan predates the field). A summary
            # across the whole plan rather than one task's tier: the row has
            # no single "current" task to point at once several tasks have
            # been claimed and released, so the worst rung reached is the one
            # fact worth surfacing without opening tasks.json.
            "taskEffectiveTier": task_row_effective_tier(self.tasks),
            "tasksDisplay": fraction,
            # A plan-less run gets no summary either: `0/0 completed` would be
            # a claim about a plan that does not exist.
            "tasksSummary": format_task_counts(counts) if fraction else "",
            "tasksTrouble": format_task_trouble(counts),
            # The flattened one-string cell `ralphctl runs` prints (the hub
            # composes the same parts as styled spans instead).
            "tasksColumn": format_task_column(counts, stale=self.stale),
            **self.contract,
        }


def _remember_tasks(key: str, doc: dict) -> None:
    with _tasks_last_good_lock:
        _tasks_last_good[key] = doc


def _remembered_tasks(key: str) -> dict | None:
    with _tasks_last_good_lock:
        return _tasks_last_good.get(key)


def read_tasks_doc(
    run_root: Path | str,
    *,
    attempts: int = TASKS_READ_ATTEMPTS,
    delay: float = TASKS_READ_DELAY,
    persist: bool = True,
) -> TasksRead:
    """Read `<run_root>/tasks.json` without ever inventing an empty plan.

    See the block comment above for the semantics. `persist=False` makes the
    read strictly side-effect free (no `.tasks-last-good.json` write) -- what a
    read-only viewer of somebody else's run dir passes.

    Deliberately a plain function over a path, like `prd_path()`: the
    host-side CLI and hub server must be able to read a registry run dir
    WITHOUT constructing a `RunDir` (whose `__post_init__` creates
    directories).
    """
    root = Path(run_root)
    path = root / "tasks.json"
    key = os.path.abspath(path)
    last_error: str | None = None
    for attempt in range(max(1, attempts)):
        try:
            text = path.read_text()
        except FileNotFoundError:
            return TasksRead(doc={}, source="absent", stale=False)
        except OSError as exc:  # unreadable for some other reason
            last_error = f"{type(exc).__name__}: {exc}"
            break
        try:
            doc = json.loads(text)
        except json.JSONDecodeError as exc:
            last_error = f"JSONDecodeError: {exc}"
        else:
            if isinstance(doc, dict):
                _remember_tasks(key, doc)
                return TasksRead(doc=doc, source="file", stale=False)
            # Valid JSON of the wrong shape is as unusable as a truncated
            # file; treat it the same rather than pretending it is a plan.
            last_error = f"tasks.json is a {type(doc).__name__}, not an object"
        if attempt + 1 < max(1, attempts):
            time.sleep(delay)
    good = _remembered_tasks(key)
    if good is None:
        good = read_json(root / TASKS_LAST_GOOD_NAME, None)
        if isinstance(good, dict):
            _remember_tasks(key, good)
        else:
            good = None
    if good is None:
        return TasksRead(doc={}, source="unreadable", stale=True, error=last_error)
    if persist:
        # Only ever written on the sad path, so the cache can never become a
        # second source of truth the happy path also maintains.
        try:
            cache = root / TASKS_LAST_GOOD_NAME
            if read_json(cache, None) != good:
                atomic_write_json(cache, good)
        except OSError:
            pass  # read-only/vanished run dir: the in-memory fallback still works
    return TasksRead(doc=good, source="last-good", stale=True, error=last_error)


# Task 024 (#33): the two meanings of a `failed` task, told apart in ONE place.
#
# `failed` used to mean both "a verifier judged this requirement unmet" and
# "the engine consumed the last of this task's validation rounds", so a task
# record could not be read without a human explaining it -- and, worse, either
# meaning blocked a run from reaching a verdict at all (the worker's
# `<promise>COMPLETE</promise>` recognises only `completed`/`skipped`), which is
# exactly what happened to `043d`: four iterations and two steering notes spent,
# resolved only by an operator hand-editing the record.
#
# The vocabulary is deliberately NOT a new status (`skipped` already exists and
# every surface that counts statuses would have to learn a sixth one). It is one
# engine-written label on the task record, `failureKind`, with the two values
# below; `status` stays `failed` for both, so every existing reader keeps
# working and the distinction is additive.
VALIDATION_ATTEMPT_LIMIT = 3
TASK_FAILURE_VALIDATION_EXHAUSTED = "validation-exhausted"
TASK_FAILURE_REQUIREMENT_UNMET = "requirement-unmet"
TASK_FAILURE_KINDS = (TASK_FAILURE_VALIDATION_EXHAUSTED,
                      TASK_FAILURE_REQUIREMENT_UNMET)


def reset_validation_attempts_for_reopen(task: dict) -> None:
    """Give a task the engine itself reopens to `pending` for new work a
    fresh verify budget (#273), in the caller's own write.

    A reopen is a new obligation, like a judge-authored criteria amendment:
    the attempts spent on the old claim say nothing about the new one, and a
    counter left at `VALIDATION_ATTEMPT_LIMIT` would make the next verify
    dispatch refuse the task outright. The spent count is kept on the record
    as `priorValidationAttempts` for the audit trail; a task that never
    spent an attempt is left untouched.
    """
    attempts = task.get("validationAttempts")
    if isinstance(attempts, int) and attempts > 0:
        task["priorValidationAttempts"] = attempts
        task["validationAttempts"] = 0

# Task 019 (requirement F, #60 pillar 3): the two harness-advanced statuses
# that replace an agent's own claim of `completed` in VIGILANT mode --
# `awaiting-validation` is the worker's claim state (the harness sets it
# once a worker reports done, before verify ever runs) and `validated` is
# the verifier-granted terminal. `completed` stays the non-vigilant
# terminal unchanged. See `engine.transitions.TASK_STATUS_TABLE`/
# `TASK_STATUS_VIGILANT_DELTA` for the declared table these two are legal
# moves in, and #60's rename note (PRD fact 8) for the readers that had to
# move with them: `_TASK_COUNT_KEYS`, `TASK_STATUS_LABELS`, `/status`
# counters, `ralphctl status`, `ralphctl tasks` and the hub's labels/task
# dialog (all below and in `cli/main.py`/`cli/web/app.js`, generically --
# neither hardcodes a status enumeration, so most of them moved for free
# once these two constants and the two mappings below exist).
TASK_STATUS_AWAITING_VALIDATION = "awaiting-validation"
TASK_STATUS_VALIDATED = "validated"

# The statuses a worker iteration can still ACT on -- the scheduler in
# `worker.md` picks a task in exactly this set (validation-failed first, then
# in-progress, then an unblocked pending one). Everything else -- `completed`,
# `skipped`, `failed` under either meaning, and (vigilant-only)
# `awaiting-validation`/`validated` -- is resolved as far as the worker loop
# is concerned, whether or not the requirement behind it was met. A task
# `awaiting-validation` is NOT actionable: it is waiting on verify, not on
# another worker iteration, exactly like `completed` was before task 019.
TASK_ACTIONABLE_STATUSES = ("pending", "in-progress", "validation-failed")

# Task 046 (#55): the statuses that make a task DONE as far as the worker's
# completion marker is concerned -- exactly the two worker.md's completion
# signal recognises, PLUS `validated` (task 019): a vigilant task the
# verifier has granted its terminal is done in exactly the same sense
# `completed` is done in non-vigilant mode -- neither is waiting on
# anything else. Deliberately narrower than "not actionable": a `failed`
# task is unactionable but is NOT completion, which is the whole point of the
# #33 vocabulary above and of the route-to-review path that resolves it. The
# engine cross-checks a COMPLETE against this set before honouring it, so a
# marker that appears for any reason (an agent quoting the protocol, a
# formatting slip, a model deciding it is close enough) cannot end the worker
# loop while the plan itself still says otherwise.
TASK_COMPLETION_STATUSES = ("completed", "validated", "skipped")

# Task 016 (PRD requirement B.1, #156): the statuses that do NOT count as
# completion (they are not in the tuple above, and no surface reports them
# as done) but must not BLOCK a completion signal either.
#
# `awaiting-validation` was always this shape -- see `task_blocks_
# completion`'s own docstring for why. `superseded`, the status a forced
# split leaves on the PARENT it just split, is the second: the parent's
# whole obligation moved to the children `insert_worker_split_tasks`
# inserted in the same acceptance, and those children are ordinary plan
# tasks that block completion themselves until they resolve. Leaving the
# parent blocking as well would stall the plan on a record nothing can
# ever move again -- `superseded` is written once, by `supersede_task`,
# and has no transition out of it -- i.e. exactly the strand `failed`
# deliberately causes (there a worker still has two honest resolutions
# open to it) and this status deliberately must not.
TASK_COMPLETION_NONBLOCKING_STATUSES = ("awaiting-validation", "superseded")


def task_blocks_completion(task: dict) -> bool:
    """True iff this task record must stop a COMPLETE signal being honoured.

    A task with no status at all blocks, same rule as `task_is_actionable`: an
    unreadable record must never look finished.

    `awaiting-validation` deliberately does NOT block, even though it is
    outside `TASK_COMPLETION_STATUSES` (task 025's counts and every other
    surface must still show it as its own thing, distinct from `validated`).
    By the time this runs, this pass's `_step_worker` has already tried
    verify on every such task -- pending_verify is drained synchronously,
    same pass, before this check ever sees the record (task 020) -- so a task
    still sitting here is not "unverified because nobody looked", it is
    either the negative-verdict paths (`validation-failed`, `failed`, both
    still outside this set and still actionable/blocking respectively) or
    verify tried and could not reach ANY verdict at all (task 050: retries on
    a transient provider/infra error, exhausted). Pre-020, that ambiguity
    had nowhere else to live and was folded into `completed` itself, which
    never blocked a COMPLETE signal either -- an infra fault must not be able
    to strand a run in the stagnation guard any more than #33's `failed`
    tasks can (task 024's same review-gets-a-look reasoning, just for a
    verdict nobody managed to render rather than one that came back
    negative)."""
    if not isinstance(task, dict):
        return True
    return task.get("status", "pending") not in (
        TASK_COMPLETION_STATUSES + TASK_COMPLETION_NONBLOCKING_STATUSES)


# ---------------------------------------------------------------------------
# Task 014 (#68 half 2, requirements E/G): the effective-tier ladder as
# HARNESS STATE on the task record itself.
#
# `tier` (task 013) is the planner-annotated BASELINE -- set once, at plan
# ingestion, and never touched again. `effectiveTier` is the harness-owned
# rung a task's next dispatch actually resolves at (task 016's business);
# it starts equal to the baseline and is bumped ONE step, capped at `hard`
# (`tiers.escalate_tier`, decision 6/G), each time a task RE-ENTERS
# `in-progress` from `validation-failed` -- i.e. each validation-failed
# retry, not each iteration. There is no de-escalation path anywhere in
# this module (requirement G): once a task's `effectiveTier` reaches
# `tiers.TOP_TIER` it can never move again, whatever a later success or
# failure cycle does to its status.
#
# Both fields live ONLY on the harness-owned tasks.json record, written
# ONLY by the functions below and their call site in `engine.loop`'s worker
# loop -- never by a hat's own reported payload (`HAT_SCHEMAS["worker"]` has
# no `tier`/`effectiveTier` property at all, and `additionalProperties:
# False` refuses one that tries; even if a payload smuggled one in, nothing
# in `_apply_reported_task_status`/`_apply_worker_completion_report` ever
# reads or copies it -- the functions below are the only writers).
TASK_EFFECTIVE_TIER_FIELD = "effectiveTier"

# Task 014: a task that fails validation while its effective tier is
# ALREADY at `tiers.TOP_TIER` has nowhere left to escalate to -- this is a
# distinct, queryable fact from the ordinary validation-failed retry (it is
# #61's stagnation input: "this task has spent its whole tier ladder and
# still failed"), recorded on the task record under this field name rather
# than inferred by a reader re-deriving "was it already at hard" from
# `effectiveTier` alone -- the same "engine writes the label, the field is
# additive" discipline `failureKind`/`TASK_FAILURE_VALIDATION_EXHAUSTED`
# already uses above.
TASK_TIER_LADDER_EXHAUSTED_FIELD = "tierLadderExhausted"


def task_baseline_tier(task: dict) -> str:
    """This task's planner-annotated baseline tier (task 013's `tier`
    field), normalised to one of `tiers.TIERS` -- a missing or otherwise
    unrecognised value (a plan ingested before task 013, or a hand-edited
    record) reads as `tiers.DEFAULT_TIER`, the same degrade-to-`normal`
    rule ingestion itself already applies (`plan_ingestion.
    normalize_task_tiers`), so a reader never has to special-case a legacy
    record."""
    if not isinstance(task, dict):
        return tiers.DEFAULT_TIER
    tier = task.get("tier")
    return tier if tiers.is_tier(tier) else tiers.DEFAULT_TIER


def task_effective_tier(task: dict) -> str:
    """This task's CURRENT effective tier: the persisted `effectiveTier`
    if the record carries a recognised one, else the baseline tier (a task
    that has never been claimed, or was claimed before this field existed,
    has not escalated at all -- its effective tier IS its baseline).

    Read-only and pure: never writes anything, never invents a value that
    is not already implied by the record it was handed -- the mirror of
    `task_baseline_tier` just above, one rung up the fallback chain.
    """
    if not isinstance(task, dict):
        return tiers.DEFAULT_TIER
    effective = task.get(TASK_EFFECTIVE_TIER_FIELD)
    if tiers.is_tier(effective):
        return effective
    return task_baseline_tier(task)


def task_row_effective_tier(tasks: list) -> str | None:
    """Task 019 (#68 requirement G, hub row): the highest effective tier ANY
    task in `tasks` currently sits at (task 014's escalation ladder,
    `task_effective_tier` above) -- for `TasksRead.row_fields`'s run-list
    row. `None` when no task carries a recognised `effectiveTier` at all: a
    plan that predates the field, or one where nothing has ever escalated
    past its own baseline, has nothing to report, and `None` says so rather
    than a confident `tiers.DEFAULT_TIER` that would claim escalation never
    happened when really the record just never says.

    Read-only and pure, exactly like `task_effective_tier`: it never writes
    anything, and a task missing or malformed is simply skipped rather than
    degraded to a fallback value that would then out-rank a genuine `trivial`.
    """
    best = None
    for t in tasks:
        if not isinstance(t, dict):
            continue
        raw = t.get(TASK_EFFECTIVE_TIER_FIELD)
        if not tiers.is_tier(raw):
            continue
        if best is None or tiers.tier_rank(raw) > tiers.tier_rank(best):
            best = raw
    return best


def claim_task_effective_tier(task: dict) -> str:
    """Persist `effectiveTier` on a task claimed for the FIRST time (a
    `pending` -> `in-progress` move): equal to its baseline tier, written
    explicitly rather than left implicit, so `effectiveTier` is present on
    the record from the very first claim onward. A task already carrying a
    recognised `effectiveTier` is left byte-for-byte alone -- this is not
    an escalation, and it must never move an already-escalated task
    backward to its baseline (requirement G: no de-escalation path, full
    stop). Mutates `task` in place and returns the (possibly unchanged)
    tier.
    """
    current = task.get(TASK_EFFECTIVE_TIER_FIELD)
    if tiers.is_tier(current):
        return current
    baseline = task_baseline_tier(task)
    task[TASK_EFFECTIVE_TIER_FIELD] = baseline
    return baseline


def escalate_task_effective_tier(task: dict) -> str:
    """Bump `task`'s effective tier ONE step, capped at `tiers.TOP_TIER`
    (`tiers.escalate_tier`, decision 6/G) -- the harness's OWN response to a
    `validation-failed` re-entry, called exactly once per re-claim (the
    `validation-failed` -> `in-progress` move in `engine.loop`'s worker
    loop), never per iteration and never from anything a hat reports.
    Mutates `task` in place and returns the new (or, at `hard`, unchanged)
    tier -- there is no way to call this and end up BELOW where the task
    already stood, which is what makes "no de-escalation path" true by
    construction rather than by a caller's discipline.
    """
    new_tier = tiers.escalate_tier(task_effective_tier(task))
    task[TASK_EFFECTIVE_TIER_FIELD] = new_tier
    return new_tier


def mark_tier_ladder_exhausted_if_at_top(task: dict) -> bool:
    """Task 014: called at the moment a validation verdict comes back
    negative for `task` -- BEFORE any escalation is attempted for the NEXT
    retry. If this task's current effective tier is already `tiers.
    TOP_TIER`, there is nowhere left on the ladder to escalate to on the
    next re-entry, so that fact is recorded on the record itself
    (`TASK_TIER_LADDER_EXHAUSTED_FIELD`), once it becomes true, and stays
    true afterward (never cleared -- exhaustion, once real, does not
    un-happen). Returns whether the task was (already, or newly) at the
    ladder's top -- the caller (`engine.loop._verify_task`) uses this to
    decide whether to also emit an event, without re-deriving the same
    tier comparison itself.
    """
    if not isinstance(task, dict):
        return False
    at_top = task_effective_tier(task) == tiers.TOP_TIER
    if at_top:
        task[TASK_TIER_LADDER_EXHAUSTED_FIELD] = True
    return bool(at_top or task.get(TASK_TIER_LADDER_EXHAUSTED_FIELD))


# ---------------------------------------------------------------------------
# Task 015 (#68 half 2, requirement H): where a VERIFY dispatch provisions,
# one rung down from the work it is checking -- and where the run-ending
# verification provisions instead, which is never one of this ladder's rungs
# at all.
#
# Decision 7: per-task verify runs at `max(effective tier - 1, trivial)` on
# VERIFY's OWN ladder (`tiers.step_down_tier`, task 012), computed from the
# tier the work actually ran at AFTER escalation -- `task_effective_tier`
# above, never `task_baseline_tier`. A thrice-escalated task's verify still
# gets a serious check; a task that never escalated gets verify's cheapest
# rung, never a rung below it (the floor is `tiers.BOTTOM_TIER`, same as the
# ladder itself).
#
# Decision 8: the run-level final verification -- the pass whose VERIFIED
# verdict ends the run (`LoopSupervisor._review_verdict`, and previously
# the retired off-budget `_maybe_grace_review` too) -- is the one dispatch requirement I's "hats with no task in
# hand keep their static half-1 role" rule does NOT cover: it always
# provisions at `tiers.TOP_TIER`, regardless of any task's baseline or
# effective tier (there is no task in hand to read one from, and even if
# there were, the exit gate is never economised -- "never a bottom-rung
# rubber stamp"). Both functions are pure and read-only: neither one
# mutates the task record or touches `effectiveTier`/`tier` itself -- the
# tier a VERIFY dispatch provisions at is a separate fact from the tier the
# WORK it is checking is recorded at.
def task_verify_tier(task: dict) -> str:
    """The tier a PER-TASK verify/review dispatch provisions at (decision
    7): one step down from `task`'s CURRENT effective tier
    (`task_effective_tier` -- post-escalation, never the baseline),
    floored at `tiers.BOTTOM_TIER` (`tiers.step_down_tier`). Checking is
    cheaper than doing the work, but never cheaper than the ladder's own
    floor."""
    return tiers.step_down_tier(task_effective_tier(task))


def run_level_verify_tier() -> str:
    """The tier the run-ending verification pass always provisions at
    (decision 8): `tiers.TOP_TIER`, unconditionally. Takes no argument on
    purpose -- the whole point of decision 8 is that no task's tier,
    present or absent, ever changes this answer; economise per task, never
    on the exit gate."""
    return tiers.TOP_TIER


# Task 025 (#33): how the two meanings are COUNTED, and how they are SPELLED.
#
# A failure kind is not a sixth status, so it never gets a status key of its
# own in `task_counts()`: both kinds keep counting under `failed`, which is
# what keeps every tally adding up (the status keys still sum to `total`,
# `_TASK_STATUSES` is unchanged, and a row sorting on `tasksCompleted`/`total`
# is untouched). The breakdown travels as SUB-counts beside the status counts,
# named here; `TASK_COUNT_SUBKEYS` is the list every consumer that walks a
# counts dict as "statuses" must exclude, and the sub-counts sum to `failed`.
TASK_FAILURE_COUNT_KEYS = {
    TASK_FAILURE_VALIDATION_EXHAUSTED: "failedValidationExhausted",
    TASK_FAILURE_REQUIREMENT_UNMET: "failedRequirementUnmet",
}
TASK_COUNT_SUBKEYS = tuple(TASK_FAILURE_COUNT_KEYS.values())

# The field name under which the DERIVED per-task kinds are published by every
# surface that serves the plan itself (`GET /tasks`, `ralphctl tasks --json`,
# the hub's run-detail payload): `{task id: kind}`, for failed tasks only.
# Derived rather than copied, because `failureKind` is absent from every
# tasks.json written before task 024 -- see `task_failure_kind()`.
TASK_FAILURE_KINDS_FIELD = "taskFailureKinds"

# Task 021 (#60 pillar 3, PRD requirement F): a second OVERLAY count, same
# discipline as the failure-kind sub-counts above -- `blocked` is not a
# seventh status (a task the dependency graph dooms keeps its own real
# status, `pending`/`in-progress`/`validation-failed`/`awaiting-validation`),
# so it never gets a status key of its own either. `TASK_COUNT_OVERLAY_KEYS`
# is the full exclusion list -- both this wave's overlay and task 025's --
# every consumer that walks a counts dict as "statuses" (i.e. expects the
# keys it is summing to add to `total`) must exclude, exactly the way
# `TASK_COUNT_SUBKEYS` alone used to be that list before this key existed.
TASK_COUNT_OVERLAY_KEYS = TASK_COUNT_SUBKEYS + ("blocked",)

# A `dependsOn` entry is SATISFIED only by these two statuses -- the same pair
# `tests/stub-pi/pi`'s scheduler emulation already uses
# (`_TASK_SATISFIES_DEPENDENCY`), brought into the engine here as the one
# place that answer is decided rather than reimplemented a third time.
# `skipped` deliberately does NOT satisfy it: the PRD's own vocabulary calls
# `failed` AND `skipped` "terminal dead" for a dependency chain's purposes
# (docs/prds/v0.9-declarative-loop.md, requirement F) -- a skipped dependency
# never did the work a dependent needs, any more than a failed one did.
# Task 016 (PRD requirement B.1, #156): `superseded` -- the status a
# forced split (a task whose progress budget is spent, #156 requirement B)
# leaves on the PARENT it just split -- joins this tuple too, deliberately,
# unlike `skipped`/`failed` just below. The PRD's own words for the status
# are "not schedulable, not blocking": "not schedulable" is free (a
# superseded task matches none of `scheduler.select_next_task`'s three
# buckets -- `validation-failed`/`in-progress`/`pending` -- so it is never
# picked again without this tuple's help); "not blocking" is what adding it
# HERE buys -- a task whose `dependsOn` names the now-superseded parent
# reads that dependency as satisfied rather than staying stuck forever
# waiting for a status the parent will never reach again, because the
# parent's own obligation moved to whichever children `insert_worker_split_
# tasks` just inserted (`_apply_forced_split_report`, `engine/loop.py`).
TASK_DEPENDENCY_SATISFIED_STATUSES = ("completed", "validated", "superseded")
TASK_DEPENDENCY_DEAD_STATUSES = ("failed", "skipped")

# The one spelling of the status itself, so a caller outside this module
# (`LoopSupervisor._all_tasks_completed`) names it rather than re-typing
# the literal a fourth time.
TASK_STATUS_SUPERSEDED = "superseded"


def supersede_task(run: "RunDir", task_id: str) -> bool:
    """Task 016 (PRD requirement B.1, #156): the ONE writer of the
    `superseded` status -- called by `LoopSupervisor._apply_forced_split_
    report` the instant a forced split (a spent progress budget's
    mandatory `proposed_tasks` report) is accepted, strictly AFTER the
    split's own children are already inserted (`insert_worker_split_
    tasks`), so a crash between the two writes never leaves a plan with a
    superseded parent and no children to pick up its work.

    `superseded` is terminal for scheduling for free (see the tuple's own
    comment just above this function) -- this function's only job is the
    one write, plain and unconditional, same discipline `insert_worker_
    split_tasks`/`steer_task_insert` already apply to their own field.
    Returns True iff `task_id` resolved and was moved, False otherwise
    (never raises -- a caller that already knows `task_id` is real, as
    `_apply_forced_split_report` does, never needs to inspect this).
    """
    data = run.read_tasks()
    for t in data.get("tasks", []):
        if isinstance(t, dict) and t.get("id") == task_id:
            t["status"] = "superseded"
            run.write_tasks(data)
            return True
    return False

# Task 011 (PRD requirement I, #98): a task the steer hat inserted carries
# this `origin` value in `tasks.json` -- `tasks.json` being planner-proposed
# is a deliberate property (PRD: "an injected task must not be
# indistinguishable from one the planner reasoned about"), so an operator
# reading a plan, or a run's own dependency graph, can tell which tasks were
# never reasoned about by the planning hat at all. A planner-proposed task
# carries NO `origin` key (backward compatible with every plan written
# before this existed, not a second spelling of "planner"); only
# `steer_task_insert` ever writes this value, and it writes it
# unconditionally -- the same way that function always forces `status` to
# `pending` regardless of what the caller supplied.
TASK_ORIGIN_STEER = "steer"


def is_steering_origin(task: dict) -> bool:
    """True for a task `steer_task_insert` created -- the one predicate every
    surface that marks steering-origin tasks (`format_task_status` below,
    the CLI table, the hub row/dialog) calls, so "what counts as steering-
    origin" is decided once rather than re-testing `task.get("origin") ==
    "steer"` in three languages."""
    return isinstance(task, dict) and task.get("origin") == TASK_ORIGIN_STEER


# Task 021 (PRD requirement N, #102): the sibling marker for a task the
# WORKER hat inserted through its own optional `proposed_tasks` split
# proposal (`transitions.HAT_SCHEMAS["worker"]`) -- same reasoning as
# `TASK_ORIGIN_STEER` just above, for the same reason: a plan reader must be
# able to tell a task the planner reasoned about from one a worker carved
# out of an assignment mid-run, not just tasks the operator injected.
# `insert_worker_split_tasks` below is the only writer, and it writes this
# value unconditionally, exactly as `steer_task_insert` always forces
# `status` and `origin` regardless of what the caller supplied.
TASK_ORIGIN_WORKER = "worker"


def is_worker_origin(task: dict) -> bool:
    """True for a task `insert_worker_split_tasks` created -- the sibling
    predicate to `is_steering_origin`, called by the same surfaces
    (`format_task_status` below, the CLI table, the hub row/dialog)."""
    return isinstance(task, dict) and task.get("origin") == TASK_ORIGIN_WORKER


# Fields a worker's proposed split task is allowed to carry through to
# `tasks.json` verbatim -- the planner-proposal set `plan_ingestion._TASK_
# PASSTHROUGH_FIELDS` uses MINUS its planner-only annotations, duplicated
# here (not imported) because this module must not depend on `plan_ingestion`
# (the reverse dependency already exists: `plan_ingestion.lint_worker_split`
# is called FROM `LoopSupervisor`, never the other way).
#
# The two tuples are deliberately NOT equal, and no later change should sync
# them to "restore" an invariant that never held (unit-7 review): a split
# child carries neither `estimatedIterations` (a planner's own estimate of a
# task it planned; the child's is defaulted at READ time instead, by
# `task_progress_budget_estimate`'s own `task.get("estimatedIterations", 1)` --
# NOT by `plan_ingestion.normalize_task_estimated_iterations`, which runs only
# on the planner's file-channel proposal inside `_parse_proposal` and never
# over a split batch)
# nor `orderingOnly` (GH #209 defect 3's residual -- a declaration about the
# PLANNER's edges, whose omission is the conservative default, since an
# undeclared edge counts as evidence-producing and is re-opened by a cure
# pass rather than silently left behind). Both are accepted by `lint_worker_
# split` and dropped here, the same way round.
_WORKER_SPLIT_PASSTHROUGH_FIELDS = (
    "id", "title", "successCriteria", "dependsOn", "priority", "tier",
)


def insert_worker_split_tasks(run: "RunDir", proposed_tasks: list,
                              assigned_task_id: str) -> list:
    """Requirement N (#102): append every task in an already-linted
    `proposed_tasks` batch (`plan_ingestion.lint_worker_split` returned no
    errors for it -- this function trusts its caller the same way
    `steer_task_insert` trusts ITS caller's tool-call payload) to
    `tasks.json`, stamping each one `status: "pending"`, `origin:
    "worker"` (`TASK_ORIGIN_WORKER`) and `splitFrom: assigned_task_id` --
    always, regardless of what the proposal itself said, the identical
    discipline `steer_task_insert` already applies to `status`/`origin`.
    `splitFrom` is what makes "restricted to a split of the assigned task"
    an auditable fact on the created row, not just an ingestion-time rule
    that leaves no trace afterwards.

    A proposed id that already exists in the live plan is skipped rather
    than overwriting the existing row -- belt-and-suspenders alongside
    `lint_worker_split`'s own collision check, the same layering `ingest_
    proposal` already has relative to `lint_proposal`'s `status`-field
    refusal. Returns the ids actually inserted, in order.
    """
    data = run.read_tasks()
    tasks = data.setdefault("tasks", [])
    existing_ids = {t.get("id") for t in tasks if isinstance(t, dict)}
    inserted = []
    for t in proposed_tasks:
        if not isinstance(t, dict):
            continue
        tid = t.get("id")
        if tid in existing_ids:
            continue
        new_task = {k: t[k] for k in _WORKER_SPLIT_PASSTHROUGH_FIELDS if k in t}
        new_task["status"] = "pending"
        new_task["origin"] = TASK_ORIGIN_WORKER
        new_task["splitFrom"] = assigned_task_id
        tasks.append(new_task)
        existing_ids.add(tid)
        inserted.append(tid)
    if inserted:
        run.write_tasks(data)
    return inserted


def apply_worker_dependency_repoints(run: "RunDir", updates: list) -> list:
    """#209 defect 7: replace the `dependsOn` list of every task named in an
    already-linted `proposed_task_updates` batch
    (`plan_ingestion.lint_worker_task_updates` returned no errors for it --
    this function trusts its caller exactly the way `insert_worker_split_
    tasks` above trusts its own). One `read_tasks()`, one `write_tasks()`,
    and the ids actually re-pointed returned in order, the same shape that
    function returns its inserted ids in.

    This is the RESIDUAL writer. The engine already re-points dependents
    itself on two paths that need no worker at all --
    `_repoint_dependents_onto_cure_task` below (from `insert_cure_tasks`,
    whenever a blocking finding names the dead task) and
    `petitions.apply_confirmed_petition_adjudications` -- and neither is
    replaced here. This one covers only the case where there is no cure task
    to re-point ONTO: a doomed `pending` task whose replacement is a
    sibling that already exists.

    Two properties beyond the write itself:

    * the entry's mandatory `reason` is appended to the task's own `notes`
      through `_JUSTIFICATION_SEPARATOR`, so the re-point leaves a trace on
      the ROW and not only at ingestion time -- the same auditability
      `insert_worker_split_tasks`' `splitFrom` stamp exists for, since the
      lint can check the graph's shape but never the semantic claim that the
      new edge carries the dead one's work. Appended, never deduplicated:
      two re-points of one task are two facts about it, and the lint already
      refuses the repeat that would actually matter (a task whose doom the
      first re-point cleared is no longer in `task_blocked_by`'s map, so it
      has no standing for a second);
    * one `DEPENDENCY_REPOINTED_EVENT` per update, naming the removed and
      added ids and the reason, with `source="worker-repoint"` -- the same
      discriminator `_emit_rejected_dependency_edges` already carries.

    A `task_id` that has vanished between the lint and this write is skipped
    rather than inserted, belt-and-suspenders in exactly the shape
    `insert_worker_split_tasks`' own collision skip uses.
    """
    data = run.read_tasks()
    tasks = data.get("tasks", [])
    by_id = {str(t.get("id")): t for t in tasks
             if isinstance(t, dict) and t.get("id")}
    repointed: list = []
    for update in updates:
        if not isinstance(update, dict):
            continue
        task_id = str(update.get("task_id") or "").strip()
        row = by_id.get(task_id)
        if row is None:
            continue
        old = [str(d) for d in (row.get("dependsOn") or [])]
        new = [str(d).strip() for d in (update.get("dependsOn") or [])]
        reason = str(update.get("reason") or "").strip()
        row["dependsOn"] = new
        existing_notes = (row.get("notes") or "").strip()
        row["notes"] = _JUSTIFICATION_SEPARATOR.join(
            part for part in (existing_notes, reason) if part)
        repointed.append(task_id)
        run.emit(DEPENDENCY_REPOINTED_EVENT, taskId=task_id,
                 removed=sorted(set(old) - set(new)),
                 added=sorted(set(new) - set(old)),
                 dependsOn=new, reason=reason, source="worker-repoint")
    if repointed:
        run.write_tasks(data)
    return repointed


# Task 009 (PRD requirement C.1, #141): the sibling marker for a task the
# ENGINE inserted from a curable rejection's blocking findings
# (`insert_cure_tasks` below) -- same reasoning as `TASK_ORIGIN_STEER`/
# `TASK_ORIGIN_WORKER`: a plan reader must be able to tell a task review's
# own rejection produced from one the planner reasoned about, or one a
# worker split off its own assignment, or one an operator steered in.
# `insert_cure_tasks` is the only writer, and it writes this value
# unconditionally, exactly as the other two origins always do.
TASK_ORIGIN_REVIEW_CURE = "review-cure"


def is_review_cure_origin(task: dict) -> bool:
    """True for a task `insert_cure_tasks` created -- the sibling
    predicate to `is_steering_origin`/`is_worker_origin`, called by the
    same surfaces."""
    return isinstance(task, dict) and task.get("origin") == TASK_ORIGIN_REVIEW_CURE


# The same clause boundary `plan_ingestion._CLAUSE_SPLIT_RE` uses --
# duplicated here (not imported) for the same reason `_WORKER_SPLIT_
# PASSTHROUGH_FIELDS` above is duplicated rather than imported: this
# module must not depend on `plan_ingestion` (the reverse dependency
# already exists).
_CURE_CLAUSE_SPLIT_RE = re.compile(r"[.!?;]+(?=\s|$)")


def _cure_task_success_criteria(finding: dict) -> str:
    """Task 009 (requirement C.1, #141): build a cure task's
    `successCriteria` from a single blocking finding's own `clause` and
    `evidence`, split at the same sentence/clause boundary a planner's
    own criteria are held to and bounded to at most 5 clauses -- so an
    engine-generated cure task's criteria stays inside the same shape a
    planner-authored one must, never an unbounded dump of a verbose
    finding's full prose.
    """
    clause = str(finding.get("clause") or "").strip()
    evidence = str(finding.get("evidence") or "").strip()
    parts: list[str] = []
    for text in (clause, evidence):
        if not text:
            continue
        for piece in _CURE_CLAUSE_SPLIT_RE.split(text):
            piece = piece.strip()
            if piece:
                parts.append(piece)
    if not parts:
        parts = ["resolve the review finding"]
    return "; ".join(parts[:5])


def _cure_task_description(finding: dict) -> str:
    """Task 031 (PRD requirement E.2, #171): a cure task's own
    `description` field -- distinct from `successCriteria`, which this
    same task now sources from the finding's own `cured_when` when it
    carries one (`cure_task_from_finding` below) -- built from the SAME
    `clause` + `evidence` pair `_cure_task_success_criteria` used to fold
    into `successCriteria` alone before `cured_when` existed. Plain
    concatenation, no clause-count bound: this field is not itself a
    criterion a worker is scored against, only the human-readable context
    a worker or reader sees beside the specification (`cured_when`, or on
    a legacy finding missing it, the same fallback text `_cure_task_
    success_criteria` still builds) that IS the criterion.
    """
    clause = str(finding.get("clause") or "").strip()
    evidence = str(finding.get("evidence") or "").strip()
    parts = [p for p in (clause, evidence) if p]
    return " -- ".join(parts) if parts else "resolve the review finding"


def cure_task_from_finding(approach: int, index: int, finding: dict) -> dict:
    """Task 009 (PRD requirement C.1, #141): the one cure task built from
    a single BLOCKING, curable finding a `rejected` review just returned
    -- `title` from the finding's own `clause`, `successCriteria` built
    from that same `clause` plus its `evidence` (`_cure_task_success_
    criteria` above), `origin: "review-cure"` always. `id` is
    deterministic from `(approach, index)` -- `f"cure-{approach:02d}-
    {index:02d}"` -- so every cure task this run ever creates has a
    unique, human-legible id without reading the rest of `tasks.json`
    first; `index` is 1-based within THIS append (one per blocking
    finding, in finding order).

    Task 031 (PRD requirement E.2, #171): `successCriteria` now comes from
    the finding's own `cured_when` VERBATIM whenever it carries one (a
    non-empty string) -- `cured_when` is the specification a curable
    blocking finding is now REQUIRED to carry (`transitions.review_cured_
    when_errors`, enforced on review's own contract ladder before a cure
    pass is ever granted), held to the identical bounded-checkable-clause
    lint a planner's own `successCriteria` is (`plan_ingestion.clause_
    count_overrun`). #209 defect 1 moved WHERE that bound is enforced for
    the common case without changing the bound: an overrun `cured_when`
    whose clauses fold losslessly is repaired at insertion instead
    (`insert_cure_tasks` -> `_repair_cure_task_criteria`), so review's
    ladder above sees only the residue -- an overrun no legal rewrite can
    fix. This function itself is unaffected and still copies `cured_when`
    VERBATIM. A finding with no `cured_when` -- a hand-built
    fixture, or a finding from before this task existed -- falls back to
    `_cure_task_success_criteria`'s own clause+evidence derivation,
    unchanged. Either way, `description` (new) is always built from
    `clause` + `evidence` alone (`_cure_task_description` above) -- the
    human-readable context beside whichever text is actually the
    criterion.

    #209 defect 4: an optional `remedy_for` string on the finding is
    stamped onto the task as `remedyFor` -- the REFUTED petition
    obligation a remedy-derived cure task was built from (`petitions.
    refuted_remedy_cure_findings`). Same "the engine stamps provenance,
    never the caller" pattern `insert_worker_split_tasks` uses for
    `splitFrom`, and for the same two reasons: a plan reader must be able
    to tell a remedy-derived cure task from a rejection-derived one, and
    `refuted_remedy_cure_findings`' own idempotence gate reads this field
    back off `tasks.json` (which survives a resume, unlike the in-memory
    `LoopSupervisor._petition_adjudication_seen` set). `origin` stays
    `TASK_ORIGIN_REVIEW_CURE` -- the task IS engine-inserted review output,
    so every existing surface (`is_review_cure_origin`, `format_task_
    status`, the CLI table, the hub row) already marks it correctly and no
    new origin value is owed. A finding with no `remedy_for` -- every
    ordinary rejection finding -- yields a task with no `remedyFor` key at
    all, byte-for-byte as before this change.
    """
    clause = str(finding.get("clause") or "finding").strip() or "finding"
    cured_when = finding.get("cured_when")
    if isinstance(cured_when, str) and cured_when.strip():
        success_criteria = cured_when.strip()
    else:
        success_criteria = _cure_task_success_criteria(finding)
    task = {
        "id": f"cure-{approach:02d}-{index:02d}",
        "title": clause,
        "successCriteria": success_criteria,
        "description": _cure_task_description(finding),
        "status": "pending",
        "origin": TASK_ORIGIN_REVIEW_CURE,
        "dependsOn": [],
        "tier": "normal",
    }
    remedy_for = finding.get("remedy_for")
    if isinstance(remedy_for, str) and remedy_for.strip():
        task["remedyFor"] = remedy_for.strip()
    # Task 004 (v0.27, B.5 foundation, #274): a finding carrying `reopens`
    # -- the cure task id of a listed finding a confirmation review found
    # still open (B.1/B.3) -- means THIS finding is a new example of that
    # same broken rule, not a fresh one. The new cure task built from it
    # records the ancestor it descends from here, on its own `tasks.json`
    # entry, so B.4's identity-based no-progress comparison can follow the
    # link back to the root finding without ever reading a finding id's
    # spelling (PRD B.5: "the new task records the cure task it descends
    # from"). Absent on every finding written before `reopens` existed, and
    # on every ordinary first-review finding, exactly as `remedyFor` above
    # is absent on every finding with no `remedy_for`.
    reopens = finding.get("reopens")
    if isinstance(reopens, str) and reopens.strip():
        task["reopensCureTaskId"] = reopens.strip()
    return task


def _unique_task_id(base_id: str, existing_ids: set) -> str:
    """Task 032 (PRD requirement E.3, #167): a fresh id derived from
    `base_id` -- itself if unused, otherwise `f"{base_id}-{suffix}"`
    counting up from 2 until unused. Factored out of `insert_cure_tasks`'s
    own inline collision-avoidance loop (task 006, C.1 finding 4) so the
    identical never-drop-the-entry guarantee also covers this task's own
    re-take tasks, not just the cure task itself.
    """
    if base_id not in existing_ids:
        return base_id
    suffix = 2
    while f"{base_id}-{suffix}" in existing_ids:
        suffix += 1
    return f"{base_id}-{suffix}"


def _lane_evidence_record_chain(tasks: list, path: str) -> tuple[dict | None, list]:
    """Task 032 (PRD requirement E.3, #167, steer 001): is `path` the
    plan's OWN final lane-evidence record, rather than an ordinary
    invalidated path with no task already tracking it? Steer 001 retired
    the local-sweep launch/record shape this requirement's PRD text
    ("the final-sha sweep") originally named; what a plan built under
    the check-run-record regime carries instead is a task whose own
    `successCriteria` names the record's path verbatim and that itself
    `dependsOn` the check-run verification step(s) it records -- e.g. a
    "record every lane's check-run result" task depending on a "verify
    every lane from its GitHub check runs" task. That shape -- criteria
    naming the path, AND a non-empty `dependsOn` -- is what this looks
    for, engine-generically, never by a hardcoded task id: the first task
    in `tasks` whose `successCriteria` contains `path` and whose
    `dependsOn` is a non-empty list.

    Returns `(record_task, verification_tasks)` -- `verification_tasks`
    is every task named in `record_task["dependsOn"]` that is actually
    present in `tasks`, in `dependsOn` order. `(None, [])` when no task
    matches, which is the ordinary case for a path with no such existing
    task -- `insert_cure_tasks` below falls back to appending a brand
    new re-take task for it instead.

    #209 defect 3: a task the ENGINE itself authored on this very path
    (`is_review_cure_origin` -- a cure task or a re-take task) is
    invisible to this recognition, BOTH as a candidate record and as a
    verification step. Both halves are needed, but they do not divide the
    observed bug evenly, and the unit-3 review was right to ask which shape
    each one actually decides:

    - as a candidate: a re-take task's own `successCriteria` names its
      path verbatim (`_retake_task_from_path` below writes it) and it
      `dependsOn`s its cure task, so from the SECOND cure pass over the
      same approach onward it satisfied "criteria names the path AND a
      non-empty dependsOn" and was matched as "the plan's OWN final
      lane-evidence record" it never was. For THAT shape the verification
      filter below is now enough on its own: every id such a row depends
      on is a cure id, so its verification list comes back empty and the
      existing `if not verification: continue` already skips it. This
      filter earns its place on the shape the other one cannot reach -- an
      engine-authored row whose `dependsOn` was RE-POINTED onto planner
      work, which an operator `steer task-update` writes today and a
      worker's own accepted `proposed_task_updates` will write too. There
      the verification list is non-empty and legitimate, so without this
      filter the engine's own re-take row is matched AS the record, the
      planner task it depends on is dragged from `validated` back to
      `pending`, and the row itself never receives the new cure edge --
      measured both ways, and pinned by phase 3 of `tests/test_cure_
      invalidates.py::test_the_engines_own_cure_task_is_never_a_
      verification_step`.
    - as a verification step: this is the load-bearing half. Pass 1's own
      `cure -> verification` edge (measured: `006.dependsOn ==
      ['cure-01-01']` in `selfdev-v022`, `003.dependsOn == ['002',
      'cure-03-01','cure-03-02']` in `deck-phase4`) is what made an
      ORDINARY planner task a candidate on pass 2 with pass 1's landed
      cure task as its "verification step" to re-open. `deck-phase4` reset
      `cure-03-01`/`cure-03-02` from `validated` back to `pending` at
      2026-09-17T13:36:35Z and re-worked them; `selfdev-v022` re-opened
      `cure-01-01` on passes 2 AND 3 and left it `pending` with
      `dependsOn: ['cure-01-02','cure-01-02-2','cure-01-03-2',
      'cure-01-01-3']` -- a landed code cure blocked on three LATER cure
      tasks, with three more tasks blocked behind it (#206's "no
      selectable tasks" shape).

    With the filter the verification list of such a task is empty, the
    existing `if not verification: continue` below skips it, and the scan
    reaches the SAME record/verification pair pass 1 chose. Chain
    identification is pass-invariant. The one behaviour deliberately given
    up: a record whose `dependsOn` names ONLY engine-authored rows is no
    longer re-opened -- its path takes the re-take branch instead, so it
    is still re-recorded at the cure's tree and nothing is skipped.

    #209 defect 3's RESIDUAL narrows the same walk once more, and this one is
    the plan's own call rather than the engine's: a dependency the plan
    DECLARES to be a pure ordering edge (`plan_ingestion.ordering_only_
    dependencies`, the planner's optional per-task `orderingOnly` list) is
    not returned as a verification step, so THAT record's re-open does not
    drag it back to `pending`. Per-(record, edge), never per-task (unit-7
    review): the declaration read here is the one on the record row `t`,
    intersected with `t`'s own `dependsOn`, so the very same task is still
    re-opened when some OTHER record depends on it without declaring it, and
    still re-opened as a record in its own right when its criteria name an
    invalidated path. Measured in `selfdev-v022`: a requirement-G
    finding named a test module, task `013`'s criteria cited that module so
    `013` matched as the record, and `012` -- `validated`, its work landed
    at `751abe5e` with a green nightly, per `013`'s own notes -- was re-opened
    for no reason but sitting in `013.dependsOn`. An ordering edge is
    unguessable from criteria text, which is why it is declared; and the
    declaration is BACKWARD COMPATIBLE by construction, an absent field
    meaning "every edge produces evidence", so every plan written without it
    keeps exactly the re-open it has today (see that function's own
    docstring, and `tests/test_cure_invalidates.py`'s pin of the absent-field
    default).

    Requirement E.3 is untouched by this: the RECORD is still re-opened on
    every pass from any terminal status, so the invalidated path is still
    re-taken at the cure's tree before review judges it. What a wrong
    declaration costs is narrower and bounded -- an upstream step that did
    contribute evidence is not re-run, so the record may report over one
    cure-old upstream artifact. Nothing else can bound it: no lint can check
    the semantic claim (`plan_ingestion.lint_proposal` checks only that each
    entry names one of the task's own edges), so the conservative default is
    the protection, and the declaration is visible on the row in `tasks.json`
    to the review hat and the operator that read it.

    One consequence of leaving a declared ordering edge alone: the re-open no
    longer resurrects it when it is `failed`/`skipped`, so the record stays
    blocked on it (`task_blocked_by`) instead. That is the right outcome for
    an edge the plan declares is a real ordering requirement, and clearing it
    is what the re-point levers exist for (`apply_worker_dependency_repoints`,
    an operator `steer task-update`) -- not something an evidence re-open
    should be doing by writing `pending` over a terminal failure. Where that
    leaves the run, stated because it is the reason the outcome is tolerable
    (unit-7 review): the re-opened record is `pending` but unschedulable
    (`scheduler.select_next_task` skips a task with an unmet edge), and the
    moment it is the whole actionable remainder the doomed-plan net routes the
    approach to `review` for a verdict -- `LoopSupervisor._doomed_plan_fault`,
    task 007/#99, whose input is exactly the `task_blocked_by` map above. It
    does NOT starve into the stagnation guard, and review is the judge of a
    plan with a dead ordering requirement in it, which is what that net is
    for. Pinned as phase 4 of `tests/test_cure_invalidates.py::test_a_
    dependency_declared_ordering_only_is_not_dragged_back`.
    """
    by_id = {t.get("id"): t for t in tasks if isinstance(t, dict)}
    for t in tasks:
        if not isinstance(t, dict) or is_review_cure_origin(t):
            continue
        criteria = t.get("successCriteria")
        if not isinstance(criteria, str) or path not in criteria:
            continue
        depends = t.get("dependsOn")
        if not isinstance(depends, list) or not depends:
            continue
        verification = [by_id[d] for d in depends
                        if d in by_id and not is_review_cure_origin(by_id[d])]
        if not verification:
            continue
        # #209 defect 3's residual: of the evidence steps that survive, drop
        # the ones the PLAN ITSELF declares are pure ordering edges. The
        # emptiness check above deliberately runs against the UNFILTERED list,
        # so the two narrowings stay separable: a record whose steps are all
        # engine-authored still falls through to the re-take branch (the
        # behaviour the paragraph above gives up on purpose), while a record
        # whose every step is declared ordering-only is still recognised and
        # re-opened -- `insert_cure_tasks` then puts the cure edge on the
        # record itself, since there is no verification step left to carry it.
        ordering_only = plan_ingestion.ordering_only_dependencies(t)
        return t, [v for v in verification
                   if str(v.get("id")) not in ordering_only]
    return None, []


def _retake_task_from_path(approach: int, index: int, path_index: int,
                           path: str, cure_task_id: str) -> dict:
    """Task 032 (PRD requirement E.3, #167): the one re-take task
    `insert_cure_tasks` appends for a single path in a finding's own
    `invalidates` list that is NOT the plan's own final lane-evidence
    record (`_lane_evidence_record_chain` above returned no match for
    it) -- `id` deterministic from `(approach, index, path_index)`
    (`index` is the finding's own 1-based position, `path_index` the
    path's own 1-based position within THAT finding's `invalidates`),
    `dependsOn: [cure_task_id]` so it only becomes actionable once the
    cure task it follows actually lands, `origin: "review-cure"` --
    the same sibling marker a cure task itself carries, since a re-take
    task is exactly as much the engine's own review-cure output as the
    cure task beside it. `successCriteria` states the end state plainly:
    `path` re-taken (re-recorded) at the tree the cure task leaves --
    never a bare repeat of the finding's own prose, the same
    end-state-not-defect-description discipline task 031's `cured_when`
    already holds cure criteria to.
    """
    return {
        "id": f"retake-{approach:02d}-{index:02d}-{path_index:02d}",
        "title": f"Re-take {path}",
        "successCriteria": (
            f"{path} is re-taken (re-recorded) at the tree cure task "
            f"{cure_task_id} leaves."),
        "status": "pending",
        "origin": TASK_ORIGIN_REVIEW_CURE,
        "dependsOn": [cure_task_id],
        "tier": "normal",
    }


def _existing_retake_task(tasks: list, path: str) -> dict | None:
    """#209 defect 3: the re-take task an earlier finding of this SAME
    call -- or an earlier cure pass over this SAME approach, since
    `tasks.json` only ever holds the current approach's rows
    (`LoopSupervisor._archive_approach` writes the authoritative copy to
    `approaches/NN/tasks.json` before a replan overwrites it) -- already
    appended for `path`, or `None` when the path has none yet.

    Keyed on the engine's OWN deterministic title (`_retake_task_from_
    path` above writes `f"Re-take {path}"`) plus `origin: "review-cure"`
    (`is_review_cure_origin`, the single provenance predicate this defect
    also filters `_lane_evidence_record_chain` by), never on a planner's
    prose: the title is engine-authored, so this also matches rows written
    by versions before this change, which is why a run resumed mid-approach
    keeps its collapse instead of starting a fresh series.

    Reuse is DECLINED for a re-take whose own `dependsOn` names a task in
    `TASK_DEPENDENCY_DEAD_STATUSES`: that row can never be selected again
    (`scheduler.select_next_task` excludes any candidate with an
    unsatisfiable dependency), so reusing it would convert today's
    duplicate row into a deadlock -- #206's "no selectable tasks" shape.
    Pruning the dead edge instead is a policy the engine deliberately does
    not have (the sanctioned cure is an operator `steer task-update`
    re-point), so `insert_cure_tasks` appends a fresh re-take for that
    path, exactly as it did before this change.
    """
    dead = {t.get("id") for t in tasks
            if isinstance(t, dict)
            and t.get("status", "pending") in TASK_DEPENDENCY_DEAD_STATUSES}
    title = f"Re-take {path}"
    for t in tasks:
        if not isinstance(t, dict) or not is_review_cure_origin(t):
            continue
        if t.get("title") != title:
            continue
        depends = t.get("dependsOn")
        if isinstance(depends, list) and any(d in dead for d in depends):
            continue
        return t
    return None


# Task 033 (PRD requirement E.4, #167): the phrase a plan uses to name its
# own last code-touching task -- either an explicit override in the
# standing rules (a line naming "the last code-touching task" and its id)
# or a task's own text self-declaring the role
# (the PRD's own K-series convention, "This is the last code-touching
# task"). Matched case-insensitively with an optional space/hyphen between
# each word, so "last code touching task" and "last-code-touching task"
# are the same phrase.
_LAST_CODE_TOUCHING_PHRASE_RE = re.compile(
    r"last[\s-]code[\s-]touching\s+task", re.IGNORECASE)

# The window (characters) either side of a phrase match `_nearest_known_
# task_id` searches for a task id token in -- generous enough for "The
# last code-touching task is still **053** (retiring ...)" or "**053**
# ... is the last code-touching task", never so wide it picks up an
# unrelated id from a different sentence.
_LAST_CODE_TOUCHING_ID_WINDOW = 80

_TASK_ID_TOKEN_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]*")


def _nearest_known_task_id(text: str, match_start: int, match_end: int,
                           known_ids: set) -> str | None:
    """The first token within `_LAST_CODE_TOUCHING_ID_WINDOW` characters of
    a `_LAST_CODE_TOUCHING_PHRASE_RE` match at `[match_start, match_end)`
    that is a member of `known_ids` -- checked AFTER the phrase first (the
    ordinary "the last code-touching task is still 053" phrasing), then
    BEFORE it ("053 ... is the last code-touching task"), so either
    direction is found. Never a bare numeric substring: `_TASK_ID_TOKEN_RE`
    matches whole tokens only, and membership is checked against actual
    task ids, so an unrelated nearby number (a PR/issue number, a percent)
    never matches unless a task genuinely carries that id.
    """
    window_after = text[match_end:match_end + _LAST_CODE_TOUCHING_ID_WINDOW]
    for token in _TASK_ID_TOKEN_RE.findall(window_after):
        if token in known_ids:
            return token
    window_before = text[max(0, match_start - _LAST_CODE_TOUCHING_ID_WINDOW):match_start]
    for token in reversed(_TASK_ID_TOKEN_RE.findall(window_before)):
        if token in known_ids:
            return token
    return None


def last_code_touching_task_id(run: "RunDir", tasks: list) -> str | None:
    """Task 033 (PRD requirement E.4, #167): the id of the plan's own
    named last-code-touching task, or `None` when the plan names no such
    task at all -- the fallback case `insert_cure_tasks` below preserves
    today's append-at-the-end ordering for.

    Two sources, an explicit standing-rules override checked before a
    task's own self-declared default (the convention it recognises:
    a task says of itself "this is the last code-touching task ... unless
    the plan names a later one explicitly in the standing rules"):

    1. `run.read_standing_rules()` -- the harness's own pinned copy of the
       notes' `## Standing rules` block (`RunDir.read_standing_rules`) --
       searched for `_LAST_CODE_TOUCHING_PHRASE_RE` with a task id found
       nearby (`_nearest_known_task_id`).
    2. Failing that, each task's own `title`/`successCriteria`/
       `description`, in plan order and in that field order, for the
       first task whose own text carries the phrase -- the match itself
       identifies the task, no id lookup needed.

    Never invented from a hardcoded id: absence of the phrase anywhere in
    either source returns `None`, exactly as absence of the lane-evidence
    shape `_lane_evidence_record_chain` looks for returns `(None, [])`.
    """
    known_ids = {t.get("id") for t in tasks
                 if isinstance(t, dict) and t.get("id")}
    standing_rules = run.read_standing_rules()
    if standing_rules:
        for match in _LAST_CODE_TOUCHING_PHRASE_RE.finditer(standing_rules):
            found = _nearest_known_task_id(
                standing_rules, match.start(), match.end(), known_ids)
            if found is not None:
                return found
    for t in tasks:
        if not isinstance(t, dict):
            continue
        tid = t.get("id")
        if not tid:
            continue
        for field in ("title", "successCriteria", "description"):
            value = t.get(field)
            if isinstance(value, str) and _LAST_CODE_TOUCHING_PHRASE_RE.search(value):
                return tid
    return None


def _insert_before_id(tasks: list, new_task: dict, anchor_id: str | None) -> bool:
    """Task 033 (PRD requirement E.4, #167): splice `new_task` into `tasks`
    immediately before the task whose id is `anchor_id`, re-finding
    `anchor_id`'s CURRENT index on every call (never a cached index) --
    `True` on success. A sequence of calls sharing the same `anchor_id`
    (one per cure task in a finding-order loop) lands every entry in
    finding order immediately to the left of the anchor: each insert
    lands at the anchor's current position, pushing the anchor (and
    nothing already inserted, which sits to its left) one slot right.
    `False`, a no-op on `tasks`, when `anchor_id` is `None` or not found
    -- the caller's job to fall back to `tasks.append(new_task)` in that
    case, exactly as before this task.
    """
    if anchor_id is None:
        return False
    for i, t in enumerate(tasks):
        if isinstance(t, dict) and t.get("id") == anchor_id:
            tasks.insert(i, new_task)
            return True
    return False


def _insert_after_id(tasks: list, new_task: dict, anchor_id: str | None) -> bool:
    """Task 033 (PRD requirement E.4, #167): the mirror of `_insert_before_
    id` -- splice `new_task` immediately AFTER the task whose id is
    `anchor_id`. A caller inserting several re-take tasks in finding order
    passes each one's OWN newly-inserted id as the NEXT call's `anchor_id`
    (rather than always the frozen tail's own id) so each new entry lands
    right after the previous one, not all repeatedly right after the tail
    (which would land them in REVERSE finding order). `False`, a no-op, on
    the same absent/not-found cases `_insert_before_id` refuses.
    """
    if anchor_id is None:
        return False
    for i, t in enumerate(tasks):
        if isinstance(t, dict) and t.get("id") == anchor_id:
            tasks.insert(i + 1, new_task)
            return True
    return False


def _failed_task_ids_named_in(text: str, tasks: list) -> list:
    """Task 041 (PRD requirement C.4, #149): every `failed` task in
    `tasks` whose id appears in `text` -- a finding's own free prose about
    itself: its `evidence`, and since #209 defect 7 its `clause` joined to
    it for a REVIEWER-authored finding (the caller drops the clause half for
    defect 4's synthesized `remedy_for` findings, whose `clause` is a filed
    obligation rather than prose about the finding -- see the call site in
    `insert_cure_tasks`) -- as a whole word (`\btid\b`, never a bare
    numeric substring of some unrelated id or of surrounding evidence
    prose). Order
    follows `tasks`, so a caller iterating the result gets a stable,
    reproducible order across calls against the same plan.

    #209 defect 7 (unit-3 review): an occurrence immediately preceded by
    `#` does NOT name a task. `#NNN` is this project's -- and every
    reviewer's -- citation form for an issue or a PR (`requirement E.3
    (#167)`), while a task is named `task 205` or by its literal id, and
    plans in the observed incident's own family number their tasks in
    exactly the range issues are numbered in (`deck-phase3k` held tasks
    `205`, `209`, `210`). Without the lookbehind, a clause citing `#205`
    against a plan whose task `205` is `failed` silently strips a real
    `dependsOn` edge off every dependent of it and re-points them onto an
    unrelated finding's cure task. Widening the scan to the `clause` is
    what made that collision likely -- issue citations live in a
    reviewer's prose about the finding, not in its evidence shas -- so the
    narrowing lands with it, and it narrows the `evidence` half the same
    way, on purpose: a dead task has never been named `#205` there either.
    """
    if not isinstance(text, str) or not text:
        return []
    found: list = []
    for t in tasks:
        if not isinstance(t, dict) or t.get("status") != "failed":
            continue
        tid = t.get("id")
        if not isinstance(tid, str) or not tid:
            continue
        if re.search(rf"(?<!#)\b{re.escape(tid)}\b", text):
            found.append(tid)
    return found


def _append_validated_dependency_edge(run: "RunDir", tasks: list, task: dict,
                                      dep_id: str, source: str) -> None:
    """Task 001 (PRD requirement D.1/D.3, #206): append `dep_id` to
    `task`'s own `dependsOn` (creating the list if absent) through the
    same edge validation `steer_task_update`/`steer_task_insert` run a
    candidate edge through (`plan_ingestion.validate_dependency_edges`) --
    a self-loop, a longer cycle, or a dependency on an id `tasks` does not
    hold is dropped, with a `task.dependency_edge_rejected` event naming
    the task, the edge and the reason, rather than written. An edge
    already present is a no-op, same as the unguarded `.append` this
    replaces. `source` names which of `insert_cure_tasks`'s own several
    dependsOn writes this one is (always `"cure-insertion"` today).
    """
    deps = task.get("dependsOn")
    if not isinstance(deps, list):
        deps = []
    task_id = task.get("id")
    if dep_id in deps:
        task["dependsOn"] = deps
        return
    accepted, rejected = plan_ingestion.validate_dependency_edges(
        tasks, task_id, deps + [dep_id])
    _emit_rejected_dependency_edges(run, task_id, rejected, source)
    task["dependsOn"] = accepted


def _repoint_dependents_onto_cure_task(run: "RunDir", tasks: list, failed_id: str,
                                       cure_task_id: str) -> None:
    """Task 041 (PRD requirement C.4, #149): "When the cure pass appends
    a cure task for a finding whose evidence names a `failed` task,
    every dependent of that failed task gets the cure task added to its
    `dependsOn` and the failed task removed, so the plan has no doomed
    remainder once the cure lands." This is the mechanical half of that
    move, run once per `(failed_id, cure_task_id)` pair `insert_cure_
    tasks` below discovers: for every task in `tasks` whose `dependsOn`
    names `failed_id`, remove `failed_id` and add `cure_task_id` in its
    place (skipped if already present, so a dependent naming BOTH the
    failed task and the cure task -- e.g. a hand-edited plan -- never
    ends up with a duplicate entry). A task with neither id in
    `dependsOn` is untouched. Mirrors `petitions.apply_confirmed_
    petition_adjudications`'s own dependent re-pointing for the CONFIRMED
    path, the analogous gap C.1-C.3 already closed.

    Task 001 (PRD requirement D.1/D.3, #206): the repointed `dependsOn`
    is validated (`plan_ingestion.validate_dependency_edges`) before it
    is written -- a repoint that would leave a self-loop or a longer
    cycle on the dependent drops exactly that edge, with an event naming
    the task, the edge and the reason, instead of writing it; the
    `failed_id` -> `cure_task_id` swap for every other, valid dependent
    still lands.

    Task 003 (PRD requirement E.1): the ACCEPTED write itself now also
    emits `DEPENDENCY_REPOINTED_EVENT` -- `apply_worker_dependency_
    repoints`' own audit event, `source="cure-insertion"` in place of its
    `"worker-repoint"` -- for every dependent whose `dependsOn` actually
    changes, so the engine's OWN re-point (never a worker report) leaves
    the same trace on `events.jsonl` the worker-repoint path already
    does: naming the task, the ids removed and added, the resulting list
    and why. A dependent this loop never touches (`failed_id` not in its
    `dependsOn`, the `continue` just above) emits nothing, and so does one
    whose accepted write ends up identical to what it already held --
    "changes nothing" is judged against the WRITTEN list, after edge
    validation, not the candidate before it.
    """
    for t in tasks:
        if not isinstance(t, dict):
            continue
        depends = t.get("dependsOn")
        if not isinstance(depends, list) or failed_id not in depends:
            continue
        new_depends = [d for d in depends if d != failed_id]
        if cure_task_id not in new_depends:
            new_depends.append(cure_task_id)
        tid = t.get("id")
        accepted, rejected = plan_ingestion.validate_dependency_edges(
            tasks, tid, new_depends)
        _emit_rejected_dependency_edges(run, tid, rejected, "cure-insertion")
        if accepted != depends:
            run.emit(DEPENDENCY_REPOINTED_EVENT, taskId=tid,
                     removed=sorted(set(depends) - set(accepted)),
                     added=sorted(set(accepted) - set(depends)),
                     dependsOn=accepted,
                     reason=(f"cure task {cure_task_id} covers the scope of "
                             f"failed task {failed_id}"),
                     source="cure-insertion")
        t["dependsOn"] = accepted


def _repair_cure_task_criteria(run: "RunDir", task: dict) -> None:
    """#209 defect 1: apply `plan_ingestion.repair_criteria` to a
    freshly-built cure task's `successCriteria` -- the point at which a
    finding's own `cured_when` BECOMES a criterion -- and emit
    `plan_ingestion.CRITERIA_REPAIRED_EVENT` naming both texts when it
    does. Same shared-constant-plus-`source` shape
    `_emit_rejected_dependency_edges` above already uses, so the
    plan-ingestion and cure-insertion writers read identically on
    `events.jsonl`.

    Only `clause-count-overrun` is in scope here, and both halves of that
    matter (#209 §1, SD-2):

    - `transitions.review_cured_when_errors` holds a `cured_when` to the
      overrun bound and to NOTHING else, so the quantifier lint has no
      jurisdiction over this text -- refusing it was never on the table
      here, and repairing it would be the harness inventing a criterion
      nobody refused.
    - the quantifier rewrite's bound is hardcoded (`plan_ingestion.
      _quantifier_bound_phrase`: ", verified by rerunning pytest against
      ...") and this path serves whatever workspace the run points at,
      including Go ones -- "every frame renders" would silently gain a
      pytest bound nobody asked for, and there is deliberately no
      insert-time lint on a cure task at all to catch it.

    A `cured_when` within the bound, and one whose overrun has no lossless
    fold, both leave `successCriteria` exactly as `cure_task_from_finding`
    built it -- `repair_criteria` returns `None` and this is a no-op.
    """
    old = task.get("successCriteria")
    repaired = plan_ingestion.repair_criteria(
        old, lints=("clause-count-overrun",))
    if repaired is None:
        return
    lints, new = repaired
    task["successCriteria"] = new
    run.emit(plan_ingestion.CRITERIA_REPAIRED_EVENT, taskId=task.get("id"),
             oldCriteria=old, newCriteria=new, lints=list(lints),
             source="cure-insertion")


# Task 006 (PRD requirement B.2, #221): the accepted-write sibling of
# `DEPENDENCY_REPOINTED_EVENT` for a re-opened record -- `insert_cure_
# tasks` below emits one of these per record task the `invalidates` walk
# re-opens, naming the workspace HEAD the cure pass started at (the SAME
# value it persists into `status.json`'s `preCureHeads` map, keyed by this
# event's own `taskId`) so a caller can read the persisted sha back off
# the event stream alone, without a second read of `status.json`, and
# confirm the two agree. Read back by `state.records_pending_reconciliation`
# (task 008, requirement B.4) once this event's own `cureTaskId` reaches
# `completed`/`validated`, to reconcile the map against the cure's real
# diff on the completion path.
PRE_CURE_HEAD_RECORDED_EVENT = "task.pre_cure_head_recorded"

# PRD v0.29 requirement B.3 (#280): emitted exactly once per sweep task
# one `insert_cure_tasks` call re-opens -- whether its own sweep re-open
# step (B.2, below) flips it from `validated`/`completed` back to
# `pending`, or the SAME call's `invalidates` walk already did (B.2: a
# sweep caught by both mechanisms in one pass is re-opened once, not
# twice, so it gets one event, not two). Payload: the sweep's own task id (`taskId`), the status it held
# immediately before this re-open (`oldStatus`), and every cure task id
# this pass inserted (`cureTaskIds`, in finding order) -- not only the one
# whose finding happened to trigger the walk, since B.2's `dependsOn` edge
# is added for every cure task this pass inserted, not just one.
SWEEP_REOPENED_EVENT = "cure.sweep_reopened"

# Task 008 (PRD requirement B.4, #221): the two statuses whose arrival on
# ANY task fires the completion-path reconciliation hook (`LoopSupervisor.
# _reconcile_pre_cure_heads`), and -- the same pair, deliberately spelled
# once -- the statuses a re-opening cure task must already hold before the
# record it re-opened is eligible to be walked back at all
# (`records_pending_reconciliation` below). One spelling so the trigger and
# the eligibility test can never drift apart: a record whose re-open is
# still owned by a cure task that has NOT reached one of these cannot be
# reconciled yet, because that cure's own diff is not in the record yet.
CURE_RECONCILIATION_TRIGGER_STATUSES = ("completed", "validated")


def invalidates_reopen_walk(tasks: list, invalidates) -> list:
    """Task 005 (PRD requirement B.1, #221): the pure half of `insert_
    cure_tasks`'s per-finding `invalidates` walk, extracted with no
    behaviour change so a future dry-run trigger predicate (requirement
    A's second trigger condition) and requirement B's own reconciliation
    hook can both ask what a cure pass WOULD re-open without a `RunDir`
    and without writing anything.

    Returns one entry per item of `invalidates`, in order: `None` for an
    item that is not a non-empty string, or for one `_lane_evidence_
    record_chain` does not recognise as the plan's own final lane-
    evidence record -- `insert_cure_tasks`'s own re-take branch is the
    only thing that ever happens to those, and nothing here decides that
    branch or does any of its bookkeeping -- else the `(record_task,
    verification_tasks)` pair the chain returned, exactly what the walk
    WOULD re-open (`status` back to `"pending"` on each). Every lookup
    reads `tasks` only; nothing here is ever assigned back into it, so a
    caller that only inspects or discards the returned entries mutates
    no task by calling this.

    `invalidates` absent, `None`, or anything else that is not a `list`
    returns `[]` -- the same no-op shape `insert_cure_tasks`'s own loop
    already treated it as before this extraction.
    """
    if not isinstance(invalidates, list):
        return []
    walk = []
    for path in invalidates:
        if not isinstance(path, str) or not path:
            walk.append(None)
            continue
        record_task, verification_tasks = _lane_evidence_record_chain(
            tasks, path)
        walk.append((record_task, verification_tasks)
                    if record_task is not None else None)
    return walk


def cure_pass_preview(tasks: list, blocking: list[dict]) -> dict:
    """PRD v0.23 requirement A.3 trigger condition 2, and requirement A.7's
    escalation payload (task 017, #224): a read-only preview of what a cure
    pass over `blocking` (the review verdict's own blocking findings) WOULD
    do to `tasks` if `insert_cure_tasks` were called right now -- no writes,
    no `RunDir`, built entirely on task 005's own `invalidates_reopen_walk`
    (B.1) rather than a second copy of that walk. `LoopSupervisor._step_
    review` calls this BEFORE `_take_cure_pass` ever runs, so the trigger
    and the escalation it feeds both read the SAME pre-spend prediction.

    Returns `{"reopenedTaskIds": [...], "insertCount": N}`:

    - `reopenedTaskIds`: every task id (a record task or one of its
      verification tasks) the walk would set back to `pending` that is
      CURRENTLY `validated` -- in first-seen order, deduplicated. Trigger
      condition 2 fires only on this ALREADY-validated case (the PRD's own
      wording); a walked-to task that is not currently `validated` (already
      `pending`, or some other non-terminal status this walk does not
      distinguish) is not named here.
    - `insertCount`: one entry per finding in `blocking` (the cure task
      `insert_cure_tasks` always appends, one per finding), plus one more
      for every syntactically valid `invalidates` path the walk does NOT
      recognise as a lane-evidence record chain (`invalidates_reopen_walk`
      returning `None` for that path) -- the shape of the brand-new re-take
      task `insert_cure_tasks` appends for it -- counted through the SAME
      dedup the write path applies, so the number an A.7 escalation reports
      is the number of rows a cure pass would actually add. Two findings
      naming one re-take anchor path collapse to a single task at write
      time, and a path this approach ALREADY has a re-usable re-take row
      for adds no row at all (both decided by `insert_cure_tasks`' own
      `_existing_retake_task` bookkeeping, which this preview calls rather
      than second-guessing): such a path is counted once, or not at all,
      exactly as the write would.
    """
    reopened_ids: list[str] = []
    insert_count = len(blocking)
    # Every unresolved re-take anchor path already accounted for by this
    # preview -- `insert_cure_tasks` appends at most ONE re-take row per
    # path across the whole pass (a later finding naming the same path
    # re-uses the row the earlier finding just appended), so a path seen
    # twice here must be counted once.
    retake_paths_seen: set[str] = set()
    for finding in blocking:
        invalidates = finding.get("invalidates")
        if not isinstance(invalidates, list):
            continue
        walk = invalidates_reopen_walk(tasks, invalidates)
        for path, entry in zip(invalidates, walk):
            if not isinstance(path, str) or not path:
                continue
            if entry is None:
                if path in retake_paths_seen:
                    continue
                retake_paths_seen.add(path)
                # A re-usable row already on disk (an earlier cure pass over
                # this same approach) is re-opened in place, not appended:
                # no new task, so nothing to count. `_existing_retake_task`
                # itself declines a row whose `dependsOn` names a dead task,
                # and the write appends a fresh one in that case -- reading
                # the decision from that one function is what keeps the
                # count and the write from drifting apart.
                if _existing_retake_task(tasks, path) is None:
                    insert_count += 1
                continue
            record_task, verification_tasks = entry
            for candidate in (record_task, *verification_tasks):
                if candidate.get("status") == "validated":
                    cid = candidate.get("id")
                    if cid is not None and cid not in reopened_ids:
                        reopened_ids.append(cid)
    return {"reopenedTaskIds": reopened_ids, "insertCount": insert_count}


def insert_cure_tasks(run: "RunDir", approach: int, blocking: list,
                      pre_cure_head: str | None = None,
                      pass_kind: str = "granted") -> list:
    """Task 009 (PRD requirement C.1, #141; cured under task 006, C.1
    finding 4): append exactly one cure task per entry of `blocking` (in
    finding order) to `tasks.json` and return exactly one id per entry.

    cure-01-01 (PRD v0.27 B.1/B.3/B.4, review verdict-gate VG-01): `pass_
    kind` tags the `cure-lineage.json` entry this call appends (through
    `record_cure_pass_lineage` below) `"granted"` (the default -- both
    `LoopSupervisor._take_cure_pass` and `_take_gate_cure_pass` call this
    with no override) or `"remedy"` (`_insert_refuted_remedy_tasks`'s own
    call, the ONE other call site). A REFUTED-petition remedy insertion is
    not itself a cure PASS -- PRD B.1 requires "at least one cure pass"
    before a confirmation review exists at all, and a remedy answers one
    obligation, never the whole blocking set a granted pass was taken
    over -- so it must never be mistaken for one by a reader that walks
    `cure-lineage.json`'s `passes` list and reads `passes[-1]`, the bug
    this tag exists to close: before it, a remedy entry sat in the exact
    same list a granted pass's own entry did, indistinguishable except by
    inspecting which caller wrote it, so a remedy landing after a granted
    pass silently replaced that pass as confirmation review's own source
    of listed ids and diff range, and narrowed `_cure_pass_targeted_
    roots`'s no-progress comparison down to the remedy's own single
    target. `LoopSupervisor._latest_granted_cure_pass` is the one reader
    every confirmation-review-scoped call now goes through instead of
    `passes[-1]` directly.

    `cure_task_from_finding`'s id is deterministic per `(approach,
    index)`, and in the ordinary case (a fresh approach, `curePasses
    [approach] == 0`, `LoopSupervisor._step_review` calling this exactly
    once) that id is always unused. But a hand-edited or resumed-mid-
    write `tasks.json` -- or a worker-origin task that happened to reuse
    a `cure-{approach}-{index}` id -- can already carry it; the old
    behaviour silently SKIPPED that entry, which meant a genuinely
    blocking finding could end up with no cure task at all and no error
    telling anyone why. Now a collision picks a fresh, still-unused id
    (`cure-{approach:02d}-{index:02d}-{suffix}`, suffix counting up from
    2) instead of dropping the entry -- every blocking finding still
    gets its cure task, and the return value always has exactly
    `len(blocking)` entries, one per finding, in finding order.

    Task 041 (PRD requirement C.4, #149, re-inserted by operator steer
    003 after task 016 cut it): when a finding's own `evidence` text
    names a task that is currently `failed` (`_failed_task_ids_named_
    in` above), the new cure task inherits every dependent of that
    failed task -- `_repoint_dependents_onto_cure_task` adds the cure
    task to each such dependent's `dependsOn` and removes the failed
    task, so the plan has no doomed remainder once the cure lands. A
    finding whose evidence names no `failed` task (the ordinary case)
    leaves every other task's `dependsOn` untouched, exactly as before
    this task. #209 defect 7: that search now covers the finding's own
    `clause` as well as its `evidence` -- see the comment at the call site
    for the measured incident where the dead id sat in the clause and the
    repoint therefore never fired.

    Task 032 (PRD requirement E.3, #167, steer 001): a finding's own
    `invalidates` -- zero or more in-checkout paths the finding's cure
    invalidates (`engine/findings.py`'s `FINDING_SCHEMA`) -- is now read
    here too, one path at a time, in `invalidates` order, AFTER this
    finding's own cure task and dependent-repointing above. For each
    path:

    - if `_lane_evidence_record_chain` recognises it as the plan's OWN
      final lane-evidence record (a task whose `successCriteria` names
      the path and that itself `dependsOn`s the check-run verification
      step(s) it records), this call RE-OPENS that record task and every
      one of its verification tasks (`status` set back to `"pending"`)
      and adds the cure task to each verification task's own `dependsOn`
      -- chaining cure -> verification -> record, since the record
      task's `dependsOn` already names its verification task(s). No new
      task is appended for this path; the reopened ids are still
      returned, in `verification-then-record` order, so a caller
      (`LoopSupervisor._take_cure_pass`'s `taskIds=inserted`) can see
      exactly what this call put back into motion.
    - otherwise, a brand-new re-take task is appended
      (`_retake_task_from_path`), `dependsOn` the cure task, its id
      returned same as any other appended task's.

    A finding with no `invalidates` (absent, or an empty list -- every
    fixture predating this task) leaves this loop a no-op, so the return
    value is unchanged from before this task in that case.

    #209 defect 3 narrows both of those branches, and neither narrowing
    touches the invariant requirement E.3 exists for (no task may stay
    `validated` while the evidence it recorded describes a tree older than
    the last cure commit touching a path that evidence covers):

    - the chain branch no longer recognises a task the ENGINE itself
      authored, as the record OR as a verification step
      (`_lane_evidence_record_chain`'s own filters). Until this change,
      pass 1's own `cure -> verification` edge made pass 2 re-open pass 1's
      LANDED cure task as though it were evidence -- measured in two runs,
      once leaving a code cure `pending` behind three later cure tasks.
    - the re-take branch re-uses this approach's existing re-take task for
      a path (`_existing_retake_task`) rather than appending a second row
      with the same title: `status` back to `pending`, the new cure id
      added to its own `dependsOn`, its id named once in the return value.
      So two findings of one pass sharing a path yield ONE re-take task
      depending on BOTH cure tasks, and a later pass over the same approach
      re-opens that same row. `taskIds` still names every id the pass put
      into motion; there are simply fewer duplicate rows to name.
    - the chain branch also skips a dependency the PLAN declares to be a
      pure ordering edge rather than an evidence-producing one
      (`plan_ingestion.ordering_only_dependencies`, the planner's optional
      per-task `orderingOnly` list, absent meaning "all of them produce
      evidence"): such a task keeps its terminal status and is absent from
      the return value. Measured in `selfdev-v022`, where a `validated` `012`
      was re-worked purely for sitting in the record `013`'s `dependsOn`.
      When that leaves the record with no re-opened step at all, the cure
      edge lands on the RECORD itself, so the re-record still cannot run
      ahead of the cure -- requirement E.3 holds either way.

    The re-open of a GENUINE planner evidence record and its planner
    verification tasks is deliberately unchanged -- every pass, from any
    terminal status. Paying it once per cure pass is the invariant's price:
    "re-record only after the LAST cure of the approach" is not
    implementable here, because whether a further pass is granted is
    decided by a review verdict that has not been produced yet
    (`LoopSupervisor._review_cure_pass_available`), and deferring the
    re-record is worse still -- review only runs once every task is
    complete, so a re-record left `pending` means reviewing on stale
    evidence, which is the incident E.3 was written to close.

    Task 006/007/008 (PRD requirement B.2/B.3/B.4, #221) closed the gap the
    paragraph above used to disclose as unimplemented: the `invalidates`
    prediction this call re-opens a record on IS now reconciled against the
    diff the cure actually landed, in three pieces. (1) The pre-cure sha --
    this call's own `pre_cure_head` parameter, the workspace HEAD the caller
    read before invoking it -- is persisted per re-opened record in
    `status.json`'s `preCureHeads` map and emitted on `task.pre_cure_head_
    recorded`, keyed by the RECORD task's own id (task 006). (2) A declared
    `pending -> {completed,validated,failed,skipped,superseded}` edge now
    exists in `transitions.TASK_STATUS_TABLE` -- all six targets in the
    BASE table, declared for both variants, since the restore target is
    the status this call persisted and not a function of the verification
    mode (cure-01-05, PRD requirement B.3/B.4, F05) --
    (`transitions.is_un_reopen_transition`), applied
    through `state.apply_task_status_change` and emitting `task.un_reopened`
    rather than the generic amendment event (task 007) -- this does not
    relax the standing rule that there is no edge INTO `pending`; it is the
    one declared way OUT of a `pending` this same call put a record into on
    its own prediction. (3) `state.records_pending_reconciliation` plus
    `LoopSupervisor._reconcile_pre_cure_heads` (task 008) call that edge from
    the completion path: the moment EVERY cure task that named a given
    record's live `preCureHeads` baseline has itself reached `completed`/
    `validated`, the persisted sha is diffed against HEAD over all their
    commits, and a record none of whose cited paths that diff touched is
    un-reopened, with a follow-up notification naming the owning cure tasks,
    the diffed range and the paths checked. A record still reconciles to
    NOTHING (stays `pending`) when any owner is still open, ended `failed`/
    `skipped`, the diff is uncomputable (non-git workspace, missing sha), or
    the diff DOES touch a cited path -- when in doubt the record stays open,
    since a false un-reopen ships unverified work and a missed one only
    costs one extra re-record. This reconciliation is entirely POST-HOC: the
    re-open above still happens first and unconditionally, from any terminal
    status, on the prediction alone, exactly as it always has -- only the
    LATER walk-back is new. It also does not make A.3's pre-spend trigger
    quieter: that gate fires before this call has run and before any cure
    commit exists, so it has only the same prediction to act on; measured
    against the existing reference incidents, this reconciliation would have
    suppressed none of them, because every one of those predictions turned
    out to be correct.

    Task 033 (PRD requirement E.4, #167): every cure task this call
    appends -- always the product of a BLOCKING review finding, so
    always a code cure in the sense this requirement means -- is spliced
    in immediately before the plan's own named last-code-touching task
    (`last_code_touching_task_id`), in finding order, rather than
    appended at the very end; every freshly-appended re-take task
    (`_retake_task_from_path`'s branch just below) is spliced in
    immediately after it instead, also in finding order. Neither move
    touches a REOPENED record/verification task's own position -- only
    `status`/`dependsOn` change for those, exactly as before this task --
    since those already sit at the plan's own final-lane-evidence tail.
    When the plan names no such task (`last_code_touching_task_id`
    returns `None`), both kinds fall back to `tasks.append`, unchanged
    from every approach before this task.

    #209 defect 1: `cure_task_from_finding` still copies a finding's own
    `cured_when` VERBATIM into `successCriteria`, but this function then
    runs `_repair_cure_task_criteria` over the result, so the criteria that
    reaches `tasks.json` is that text UNLESS its clause count overruns the
    plan's bounded-checkable-clause lint and a lossless fold exists -- in
    which case the fold lands instead and `task.criteria_repaired`
    (`source="cure-insertion"`) names both texts. Until this change an
    overrun `cured_when` was refused three times on review's contract
    ladder (`transitions.review_cured_when_errors`) and then used verbatim
    anyway, because `LoopSupervisor._step_review` discards those contract
    errors before granting the cure pass.

    #209 defect 4: this function has a THIRD caller beside review's own
    cure pass and the peer verdict gate's -- `LoopSupervisor._insert_
    refuted_remedy_tasks`, which turns a REFUTED petition's `substance`
    remedy into one cure task on exactly this path (`petitions.refuted_
    remedy_cure_findings` synthesises the finding). Almost nothing here is
    special-cased for it: those findings carry a `cured_when` their own
    author already folded to the clause bound (so `_repair_cure_task_
    criteria` above is a no-op on them), no `invalidates` (so the loop above
    is a no-op for them), and a `remedy_for` string that
    `cure_task_from_finding` stamps onto the task as `remedyFor`. They do
    get the freeze splice and the failed-dependent repoint above, which is
    the whole point: the petitioned task the refutation just failed is
    dependency-dead, so its dependents are handed to the remedy task
    instead of being left doomed. The ONE special case is which text that
    repoint scans (#209, unit 8): a `remedy_for` finding's `clause` is the
    filed obligation, not prose about the finding, so only its `evidence` is
    scanned -- see the comment at the scan itself.

    Task 006 (PRD requirement B.2, #221): the optional `pre_cure_head`
    parameter -- the caller's own `_workspace_head_sha()`, read once before
    this call, `None` for a non-git workspace or a caller that has none to
    give (every pre-existing call site keeps working unchanged, since a
    finding with no `invalidates` -- or one naming only fresh re-take
    paths -- never reads this parameter at all) -- is the workspace HEAD
    sha this cure pass STARTS at, i.e. the tree the re-opened record's next
    evidence must be taken AFTER. For every record task the `invalidates`
    walk re-opens (the chain branch just below, never the fresh re-take
    branch, which owns no record), this call now: (1) writes that same sha
    into `status.json`'s `preCureHeads` map, keyed by the record task's own
    id, in the exact `{"<taskId>": "<sha>"|null}` shape `approachStartHeads`
    already uses (a later re-open of the SAME record id overwrites the
    entry with the newer pass's own sha -- there is exactly one live
    pre-cure baseline per record at a time, the one the CURRENT re-open
    owes); (2) emits `PRE_CURE_HEAD_RECORDED_EVENT` naming that same sha,
    so a caller can read it back off the event stream and confirm it
    matches the map without a second read of `status.json`. Read back by
    `state.records_pending_reconciliation` (requirement B.4, task 008) once
    this call's own cure task reaches `completed`/`validated`.

    Task 007 (PRD requirement C.1, gate finding VG-003): list position
    alone is not a scheduling constraint -- `scheduler.select_next_task`
    picks a `pending` candidate by `priority` (highest wins), never by
    where it happens to sit in `tasks.json`'s list, so a cure task
    spliced immediately before a HIGHER-`priority` pending freeze task
    (the move just above, unchanged) could still lose the freeze the
    scheduler's own pick to the freeze if nothing but position separated
    them. The mechanism this task adds: the plan's own named freeze task
    (`last_code_touching_task_id`, looked up once, before any insert, so
    it is the SAME object throughout this call even though `tasks`
    itself grows) gets every cure task this call inserts added to its own
    `dependsOn` list (`_unique_task_id`-deduplicated, order-preserving,
    never a duplicate entry on a call that runs a second finding against
    an already-cured freeze). `scheduler.select_next_task`'s `pending`
    candidate pool already excludes any task whose `dependsOn` entries
    are not ALL `TASK_DEPENDENCY_SATISFIED_STATUSES` -- so once the
    freeze depends on the cure, the freeze is excluded from that pool
    until the cure task itself reaches `completed`/`validated`,
    regardless of the freeze's own `priority`. A plan with no named
    freeze task (`freeze_id is None`) leaves this a no-op, same as the
    list-position splice already does in that case.
    """
    data = run.read_tasks()
    tasks = data.setdefault("tasks", [])
    existing_ids = {t.get("id") for t in tasks if isinstance(t, dict)}
    freeze_id = last_code_touching_task_id(run, tasks)
    freeze_task = next(
        (t for t in tasks if isinstance(t, dict) and t.get("id") == freeze_id),
        None) if freeze_id is not None else None
    retake_anchor_id = freeze_id
    inserted: list = []
    # PRD v0.29 requirement B.3 (#280): every `sweep: True` task's status
    # as it stood BEFORE this call touched anything -- the sweep step below
    # reads it to tell a sweep the `invalidates` walk re-opened during this
    # same call (now `pending`, held something else here) from one that was
    # already pending, so the walk's re-open still emits its one
    # `SWEEP_REOPENED_EVENT` with the status it genuinely held.
    sweep_statuses_before = {
        t.get("id"): t.get("status", "pending")
        for t in tasks if isinstance(t, dict) and t.get("sweep") is True}
    # Task 004 (v0.27, B.5 foundation, #274): this pass's own findings,
    # keyed by the cure task id built for each -- the map `record_cure_
    # pass_lineage` below persists once the whole pass is inserted. Built
    # from the finding's RAW `clause`/`cured_when`, before `_repair_cure_
    # task_criteria` may fold an overrun `cured_when` -- a confirmation
    # review reading this record back wants the same text review itself
    # wrote, not a later engine-side repair of it.
    pass_findings: dict = {}
    for index, finding in enumerate(blocking, start=1):
        new_task = cure_task_from_finding(approach, index, finding)
        new_task["id"] = _unique_task_id(new_task["id"], existing_ids)
        cure_task_id = new_task["id"]
        pass_findings[cure_task_id] = {
            "clause": str(finding.get("clause") or ""),
            "curedWhen": (finding.get("cured_when")
                         if isinstance(finding.get("cured_when"), str)
                         else None),
        }
        # #209 defect 1: repair an over-long `cured_when` here, after the
        # id is final (so the event names the collision-resolved id) and
        # before the task is spliced in. This placement -- rather than
        # inside `cure_task_from_finding`, which stays a pure function --
        # covers BOTH cure writers in one line: review's own cure pass and
        # the peer verdict gate's, which had no bound check of any kind.
        _repair_cure_task_criteria(run, new_task)
        if not _insert_before_id(tasks, new_task, freeze_id):
            tasks.append(new_task)
        existing_ids.add(cure_task_id)
        inserted.append(cure_task_id)
        if freeze_task is not None:
            _append_validated_dependency_edge(
                run, tasks, freeze_task, cure_task_id, "cure-insertion")
        # #209 defect 7's cheap substitute: search the finding's `clause`
        # as well as its `evidence` for a `failed` task's id. Measured in
        # `deck-phase3k`: the dead id `205` sat in the finding's CLAUSE
        # while its `evidence` held only commit shas, so this repoint never
        # fired and the operator hand-wrote two `steer task-update` calls to
        # do it. Both fields are the same reviewer's free prose about the
        # same finding -- there is no reason a dead id counts in one and not
        # the other -- and the pre-write edge validation below is unchanged,
        # so a clause that merely mentions a LIVE task's id still names no
        # `failed` task and changes nothing. The whole-word match itself
        # gained ONE narrowing with this widening (see `_failed_task_ids_
        # named_in`): a `#NNN` citation is an issue reference, never a task
        # id, and issue citations are exactly what a clause is full of.
        #
        # The clause half holds for a REVIEWER's finding and only for one
        # (#209, unit 8). A `remedy_for` finding is #209 defect 4's
        # synthesized one (`petitions.refuted_remedy_cure_findings` is its
        # only writer), and ITS `clause` is not prose about the finding at
        # all -- it is the FILED OBLIGATION, which for a `contradictory-
        # clauses`/`self-referential` petition is the affected task's own
        # `successCriteria` verbatim, planner prose that this codebase's own
        # plan lints encourage to name other task ids. A bare `failed` id
        # anywhere in it re-pointed every dependent of THAT task onto a
        # remedy task which does not do its work -- schedulable again,
        # reporting over work nobody will now do, and with no event to read
        # it off (only REJECTED edges emit). Scanning `evidence` alone costs
        # defect 4 nothing: it plants the one id it wants re-pointed there
        # itself, as `"(petition REFUTED for task <id>)"`, under the same
        # obligation match that failed the task in the first place.
        scan_keys = (("evidence",) if finding.get("remedy_for")
                     else ("clause", "evidence"))
        for failed_id in _failed_task_ids_named_in(
                " ".join(str(finding.get(key) or "")
                         for key in scan_keys), tasks):
            _repoint_dependents_onto_cure_task(
                run, tasks, failed_id, cure_task_id)

        invalidates = finding.get("invalidates")
        if not isinstance(invalidates, list):
            continue
        # Task 005 (PRD requirement B.1): the pure lookup is now
        # `invalidates_reopen_walk` above -- one entry per path, aligned
        # with `invalidates` itself, `None` where the walk would not
        # re-open anything (an invalid entry, or a path the re-take
        # branch below owns instead). Only the write side -- setting
        # `status`, splicing the cure edge, and the re-take branch's own
        # cross-path bookkeeping (`retake_anchor_id`, `_existing_retake_
        # task`) -- stays inlined here; none of that is pure.
        reopen_walk = invalidates_reopen_walk(tasks, invalidates)
        for path_index, (path, reopened) in enumerate(
                zip(invalidates, reopen_walk), start=1):
            if not isinstance(path, str) or not path:
                continue
            if reopened is not None:
                record_task, verification_tasks = reopened
                for verification_task in verification_tasks:
                    verification_task["status"] = "pending"
                    reset_validation_attempts_for_reopen(verification_task)
                    _append_validated_dependency_edge(
                        run, tasks, verification_task, cure_task_id,
                        "cure-insertion")
                    vid = verification_task.get("id")
                    if vid not in inserted:
                        inserted.append(vid)
                # Task 006 (PRD requirement B.2, #221): persist and emit
                # the pre-cure workspace head THIS record is being
                # re-opened at, before any of the writes below that could
                # make `record_task.get("id")` harder to trace back to
                # this exact reopen. Keyed by the record task's own id --
                # never the verification task(s) above, which are not the
                # subject of B.2 -- and overwritten, not accumulated, on a
                # later pass's re-open of the SAME record.
                #
                # Cure-01-05 (PRD requirement B.3/B.4, F05, #221): the
                # record's own status is read HERE, before the `pending`
                # write just below overwrites it, and persisted alongside
                # the sha -- the ACTUAL pre-reopen terminal status
                # (`completed`, `validated`, `failed`, `skipped` or
                # `superseded`, whichever it genuinely held), never a
                # value inferred from `cfg.vigilant` after the fact. A
                # record already `pending` here (a still-unreconciled
                # earlier reopen this same approach) is NOT a terminal
                # status to capture -- its own persisted `preCureStatuses`
                # entry from that earlier reopen already names the real
                # one, and this later reopen must not clobber it with
                # `"pending"`.
                rid_for_head = record_task.get("id")
                pre_cure_heads_status = run.read_status()
                pre_cure_heads = dict(
                    pre_cure_heads_status.get("preCureHeads") or {})
                pre_cure_heads[rid_for_head] = pre_cure_head
                pre_cure_statuses = dict(
                    pre_cure_heads_status.get("preCureStatuses") or {})
                current_record_status = record_task.get("status", "pending")
                if current_record_status == "pending":
                    prior_status = pre_cure_statuses.get(
                        rid_for_head, current_record_status)
                else:
                    prior_status = current_record_status
                record_task["status"] = "pending"
                reset_validation_attempts_for_reopen(record_task)
                pre_cure_statuses[rid_for_head] = prior_status
                run.update_status(preCureHeads=pre_cure_heads,
                                  preCureStatuses=pre_cure_statuses)
                run.emit(PRE_CURE_HEAD_RECORDED_EVENT, taskId=rid_for_head,
                         sha=pre_cure_head, priorStatus=prior_status,
                         approach=approach, cureTaskId=cure_task_id)
                if not verification_tasks:
                    # #209 defect 3's residual: every evidence step this
                    # record depends on is declared a pure ordering edge
                    # (`plan_ingestion.ordering_only_dependencies`), so none
                    # of them was re-opened and none of them is carrying the
                    # cure edge. Put it on the RECORD itself, or the re-record
                    # could be picked before the cure lands and review would
                    # judge evidence taken at the pre-cure tree -- the exact
                    # incident requirement E.3 exists to close. With a
                    # verification step present this edge stays where it
                    # always was (on the step), completing the same chain
                    # cure -> verification -> record.
                    _append_validated_dependency_edge(
                        run, tasks, record_task, cure_task_id,
                        "cure-insertion")
                rid = record_task.get("id")
                if rid not in inserted:
                    inserted.append(rid)
            else:
                existing_retake = _existing_retake_task(tasks, path)
                if existing_retake is not None:
                    # #209 defect 3: this approach already has a re-take
                    # task for this path (an earlier finding of this same
                    # call, or an earlier cure pass over the same approach).
                    # Re-open that ONE row at the new cure's tree and add
                    # the new cure edge, instead of appending a second row
                    # with the identical title. Nothing is re-spliced:
                    # `dependsOn`, not list position, is what schedules
                    # (`scheduler.select_next_task`, requirement C.1/VG-003
                    # above), so the row keeps its original position
                    # relative to the freeze task.
                    #
                    # Its `successCriteria` is restated over THIS pass's cure
                    # task (#209 defect 3, unit-3 review): the sentence
                    # `_retake_task_from_path` writes names the cure task the
                    # re-record must follow, so a row left carrying pass 1's
                    # sentence tells a verify hat to score fresh evidence
                    # against a tree two passes old. Rebuilt through that
                    # same function so the prose is written in exactly one
                    # place, and the newest cure is the right one to name:
                    # the row cannot start until every cure in its own
                    # `dependsOn` has landed, so the tree its evidence is
                    # taken at is at or past all of them.
                    #
                    # #209 defect 3 (unit-8 integration), fix #132 again:
                    # this row is ALREADY on disk, so unlike the fresh cure
                    # and re-take rows beside it, it already carries a
                    # `criteriaFingerprint`/`criteriaOfRecord` baseline --
                    # stamped by `LoopSupervisor._ensure_criteria_baseline`
                    # or `_check_criteria_edits`, which runs on EVERY worker
                    # pass. Move that baseline forward in this SAME
                    # `write_tasks` call, or the next `_check_criteria_edits`
                    # sees the mismatch, RESTORES pass 1's sentence (undoing
                    # the restatement just above), emits `task.criteria_
                    # edit_refused` blaming a worker for the ENGINE's own
                    # write, and sets the sticky
                    # `criteriaEditedAfterValidationFailure` marker -- which
                    # `findings.flagged_criteria_findings` then turns into a
                    # blocking finding on every later review pass, so no
                    # `verified` verdict can stand for the rest of the run.
                    # That is precisely #209's own shape: the engine's repair
                    # refused as unauthorised tampering. The digest is the
                    # literal same formula as `LoopSupervisor._criteria_
                    # fingerprint`, not imported from loop.py, which imports
                    # this module and would make the import circular -- the
                    # same reason given at fix #132 in `steer_task_update`.
                    existing_retake["status"] = "pending"
                    reset_validation_attempts_for_reopen(existing_retake)
                    restated = _retake_task_from_path(
                        approach, index, path_index, path,
                        cure_task_id)["successCriteria"]
                    existing_retake["successCriteria"] = restated
                    existing_retake["criteriaFingerprint"] = hashlib.sha256(
                        restated.encode("utf-8")).hexdigest()
                    existing_retake["criteriaOfRecord"] = restated
                    _append_validated_dependency_edge(
                        run, tasks, existing_retake, cure_task_id,
                        "cure-insertion")
                    reused_id = existing_retake.get("id")
                    if reused_id not in inserted:
                        inserted.append(reused_id)
                    continue
                retake_task = _retake_task_from_path(
                    approach, index, path_index, path, cure_task_id)
                retake_task["id"] = _unique_task_id(
                    retake_task["id"], existing_ids)
                retake_id = retake_task["id"]
                accepted, rejected = plan_ingestion.validate_dependency_edges(
                    tasks, retake_id, retake_task.get("dependsOn") or [])
                _emit_rejected_dependency_edges(
                    run, retake_id, rejected, "cure-insertion")
                retake_task["dependsOn"] = accepted
                if not _insert_after_id(tasks, retake_task, retake_anchor_id):
                    tasks.append(retake_task)
                retake_anchor_id = retake_task["id"]
                existing_ids.add(retake_task["id"])
                inserted.append(retake_task["id"])
    # PRD v0.29 requirement B.2 (#280): once this call has inserted at
    # least one cure task (`pass_findings` non-empty iff `blocking` was --
    # the same condition the lineage write below already keys on), every
    # task in the plan carrying `sweep: True` gains the `dependsOn` edge on
    # EVERY cure task this pass inserted, in finding order -- not only the
    # cure task of whichever finding happened to name its path. A sweep
    # currently `validated`/`completed` is additionally re-opened to
    # `pending`, with its pre-cure head recorded through the exact same
    # `preCureHeads`/`preCureStatuses` map and `PRE_CURE_HEAD_RECORDED_EVENT`
    # the `invalidates` walk above already uses (one event per owning cure
    # task, the same multi-owner shape `records_pending_reconciliation`
    # already reads), and a `SWEEP_REOPENED_EVENT` naming the sweep, its
    # prior status and the cure task ids. A sweep the `invalidates` walk
    # above ALREADY re-opened this same call is read here with `status`
    # already `"pending"`: it is not re-opened again (once, not twice, as
    # B.2 requires), but it still emits its one `SWEEP_REOPENED_EVENT`
    # carrying the status it held before this call (B.3).
    if pass_findings:
        cure_task_ids_this_pass = list(pass_findings.keys())
        for t in tasks:
            if not isinstance(t, dict) or t.get("sweep") is not True:
                continue
            tid = t.get("id")
            for cid in cure_task_ids_this_pass:
                _append_validated_dependency_edge(
                    run, tasks, t, cid, "cure-insertion")
            current_status = t.get("status", "pending")
            status_before = sweep_statuses_before.get(tid, current_status)
            if current_status == "pending" and status_before != "pending":
                # Re-opened by this same call's `invalidates` walk: that
                # walk already flipped the status (and recorded the
                # pre-cure head for a record it re-opened), so it is not
                # re-opened a second time here -- but it IS a re-opened
                # sweep, and B.3 gives every re-opened sweep its event.
                run.emit(SWEEP_REOPENED_EVENT, taskId=tid,
                         oldStatus=status_before,
                         cureTaskIds=list(cure_task_ids_this_pass))
                continue
            if current_status not in ("validated", "completed"):
                continue
            old_status = current_status
            t["status"] = "pending"
            reset_validation_attempts_for_reopen(t)
            pre_cure_heads_status = run.read_status()
            pre_cure_heads = dict(pre_cure_heads_status.get("preCureHeads") or {})
            pre_cure_heads[tid] = pre_cure_head
            pre_cure_statuses = dict(
                pre_cure_heads_status.get("preCureStatuses") or {})
            pre_cure_statuses[tid] = old_status
            run.update_status(preCureHeads=pre_cure_heads,
                              preCureStatuses=pre_cure_statuses)
            for cid in cure_task_ids_this_pass:
                run.emit(PRE_CURE_HEAD_RECORDED_EVENT, taskId=tid,
                         sha=pre_cure_head, priorStatus=old_status,
                         approach=approach, cureTaskId=cid)
            run.emit(SWEEP_REOPENED_EVENT, taskId=tid, oldStatus=old_status,
                     cureTaskIds=list(cure_task_ids_this_pass))
            if tid not in inserted:
                inserted.append(tid)
    if inserted:
        run.write_tasks(data)
    if pass_findings:
        # Task 004 (v0.27, B.5 foundation, #274): persisted AFTER the
        # `tasks.json` write above, never before it -- a caller reading
        # the lineage record back must never see a pass recorded whose
        # cure task(s) never actually landed on disk. Every pass that
        # reaches this line inserted at least one blocking finding's cure
        # task (`pass_findings` is non-empty iff `blocking` was), so this
        # is unconditional on any granted pass, never skipped for lack of
        # a `pre_cure_head` (a non-git workspace still gets a record, with
        # `preCureHead: null` -- the identical optionality `preCureHeads`
        # already gives that same caller-supplied value).
        record_cure_pass_lineage(run.root, approach=approach,
                                 pre_cure_head=pre_cure_head,
                                 findings_by_cure_task_id=pass_findings,
                                 kind=pass_kind)
    return inserted


def records_pending_reconciliation(run: "RunDir", cure_task_id: str) -> list[dict]:
    """Task 008 (PRD requirement B.4, #221): every re-opened record THIS
    cure task's own reopen(s) put into motion (`PRE_CURE_HEAD_RECORDED_
    EVENT` entries on `events.jsonl` naming `cureTaskId == cure_task_id`),
    still eligible for reconciliation right now -- the caller
    (`LoopSupervisor._reconcile_pre_cure_heads`) diffs each entry's `sha`
    against workspace HEAD and, when that diff never touches the record's
    own cited path, walks it back through the declared un-reopen edge
    (`transitions.is_un_reopen_transition`, `apply_task_status_change`).

    Returns one `{"recordId", "sha", "priorStatus", "successCriteria",
    "cureTaskIds"}` dict per eligible record, deduplicated by `recordId` (a
    finding naming the same record for two `invalidates` paths at once
    emits two identical events; only one entry is returned). `priorStatus`
    is the ACTUAL terminal status the record held immediately before this
    reopen (cure-01-05, requirement B.3/B.4, F05) -- read from `status.
    json`'s `preCureStatuses` map, the same live-baseline-per-record
    discipline `preCureHeads` already uses, never re-derived from `cfg.
    vigilant` -- so the caller restores `completed`, `validated`, `failed`,
    `skipped` or `superseded` alike, whichever this record genuinely held.
    "Eligible" means ALL FOUR:

    - the record's own `sweep` field is NOT `True` (cure-01-01, PRD
      requirement B.2/B.5, DoD 2, #280): a `sweep: True` task is excluded
      outright, before any diff is even considered, and stays `pending`
      until the plan's own scheduler re-runs it -- whatever paths the
      cure's real diff touches. A sweep's own `successCriteria` names an
      observable (a tier/CI-lane run), never the files a cure happens to
      edit, so the diff-vs-criteria substring check below can never see a
      cure as having "touched" a sweep's cited text, and would otherwise
      un-reopen every sweep a cure pass ever re-opens -- the exact gap
      review.md's B.5 rule ("stale even when the cure touches only
      tests") and SPEC.md's `tasks[].sweep` row name;
    - the record's OWN `preCureHeads[recordId]` entry in `status.json`
      still equals this cure task's own reopen `sha` -- a LATER reopen of
      the same record at a NEWER sha overwrites that entry (B.2's own
      overwrite-not-accumulate rule), and a stale event must never drive a
      write against a sha nobody re-opened it at anymore; the caller
      conservatively does nothing for that record instead (PRD B rule 4:
      "when in doubt, leave the record open");
    - the record's current `status` is still `"pending"` -- an operator
      ruling or a steer `task-update` may already have moved it on, and
      this walk must never re-decide a move someone else already made;
    - EVERY cure task that re-opened this record at that same live sha --
      not only `cure_task_id` -- has itself already reached one of
      `CURE_RECONCILIATION_TRIGGER_STATUSES`. One cure PASS routinely
      inserts several cure tasks (one per blocking curable finding) and
      more than one of them can name the same record in its own
      `invalidates`; all of those reopens share the one `pre_cure_head`
      the caller read before the pass, so they are one baseline owned by
      several cure tasks, not several baselines. A record whose re-open is
      still owned by a cure task yet to land is NOT eligible: that cure's
      own diff is not in the tree yet, so a reconciliation now would
      decide the record against a strictly narrower diff than the one it
      owes, and restoring it there would let the LAST cure's changes to a
      cited path land against an already-restored record. Waiting until
      the last owner is terminal makes the outcome independent of which
      order the owners complete in, and means the single `sha`..HEAD diff
      the caller takes then spans every owner's own commits.

    A record whose baseline is owned by a cure task that ends `failed` or
    `skipped` therefore stays `pending` for good -- deliberately the same
    conservative direction as an uncomputable diff: nothing proved the
    cited evidence survived, so the record is re-taken rather than
    restored.

    No `RunDir` write happens here -- this is a read-only lookup, same
    discipline as `invalidates_reopen_walk` (B.1): the actual un-reopen
    write is the caller's, through `apply_task_status_change`, never a
    direct dict assignment.
    """
    events = read_events(run.root, types=(PRE_CURE_HEAD_RECORDED_EVENT,))
    reopens: dict[str, list[dict]] = {}
    for ev in events:
        rid = ev.get("taskId")
        if not rid:
            continue
        reopens.setdefault(rid, []).append(ev)
    mine = [rid for rid, evs in reopens.items()
            if any(ev.get("cureTaskId") == cure_task_id for ev in evs)]
    if not mine:
        return []
    pre_cure_heads = run.read_status().get("preCureHeads") or {}
    pre_cure_statuses = run.read_status().get("preCureStatuses") or {}
    by_id = {t.get("id"): t for t in run.read_tasks().get("tasks", [])
             if isinstance(t, dict)}
    out = []
    for rid in mine:
        task = by_id.get(rid)
        if task is None or task.get("status") != "pending":
            continue
        # Cure-01-01 (PRD requirement B.2/B.5, DoD 2, #280): a `sweep:
        # True` task is NEVER eligible for reconciliation, whatever paths
        # a cure's real diff touches. A sweep's own `successCriteria`
        # names observables ("run the final tiers", a CI lane) rather
        # than the files a cure happens to edit, so the substring check a
        # few lines down the call site (`path in criteria`) can never
        # recognise a cure diff as having "touched" a sweep's own cited
        # text -- a cure that lands no commit at all, or one that touches
        # only a test file, reads exactly like one that touched nothing a
        # sweep cites, and the un-reopen edge would fire over a tree the
        # sweep never actually re-measured. That contradicts review.md's
        # own B.5 rule ("stale even when the cure touches only tests") and
        # SPEC.md's `tasks[].sweep` row ("the mechanism that keeps a
        # plan's final sweep from staying validated at a tree a later
        # cure pass moved past") -- a sweep re-opened by `insert_cure_
        # tasks` (either its own B.2 step or the `invalidates` walk) stays
        # `pending` until the plan's OWN scheduler re-runs it, never
        # restored by this reconciliation hook. Filtered here, before the
        # diff is even computed, rather than at the `_reconcile_pre_cure_
        # heads` call site: a sweep this function never returns is a
        # sweep the caller cannot restore no matter what its own diff
        # logic does, which is the one guarantee this task's successCriteria
        # asks for ("whatever paths the cure diff touches").
        if task.get("sweep") is True:
            continue
        live_sha = pre_cure_heads.get(rid)
        at_live_sha = [ev for ev in reopens[rid] if ev.get("sha") == live_sha]
        if not any(ev.get("cureTaskId") == cure_task_id for ev in at_live_sha):
            continue
        owners = [ev.get("cureTaskId") for ev in at_live_sha
                  if ev.get("cureTaskId")]
        deduped_owners = list(dict.fromkeys(owners))
        if any((by_id.get(owner) or {}).get("status")
               not in CURE_RECONCILIATION_TRIGGER_STATUSES
               for owner in deduped_owners):
            continue
        # `pre_cure_statuses` is the live map (overwritten, not
        # accumulated, on a later reopen -- same rule as `preCureHeads`);
        # a record predating this map (no entry at all) falls back to the
        # matching event's own `priorStatus` field, and only then to the
        # record's OWN current status (a legacy event with neither is the
        # one shape that could only arise before this field existed).
        live_prior_status = pre_cure_statuses.get(rid)
        if live_prior_status is None:
            live_prior_status = next(
                (ev.get("priorStatus") for ev in at_live_sha
                 if ev.get("priorStatus")), task.get("status"))
        out.append({"recordId": rid, "sha": live_sha,
                    "priorStatus": live_prior_status,
                    "successCriteria": task.get("successCriteria", ""),
                    "cureTaskIds": deduped_owners})
    return out


def tasks_doc_excluding_new_entries(tasks_doc: dict, known_ids: set, *,
                                    origin: str) -> dict:
    """Requirement N (#102): a view of `tasks_doc` with any task whose
    `origin` equals `origin` AND whose id is NOT already in `known_ids`
    filtered out of its `tasks` list -- everything else passes through
    unchanged. This is what keeps a worker's `proposed_tasks` split from
    ever registering as "progress" against `_step_worker`'s 3-iteration
    stagnation guard: `LoopSupervisor` computes that guard's before/after
    diff over THIS view (with `known_ids` fixed at the ids present BEFORE
    the iteration ran), so inserting a brand-new worker-origin task changes
    nothing the diff can see, while a REAL later change to an EXISTING
    task -- including one this exact mechanism created on an earlier pass,
    once its id is already in `known_ids` -- still does. Proposing a split
    is deliberately excluded from counting as forward motion; completing
    one is not, and this is the one function both properties run through.
    """
    tasks = tasks_doc.get("tasks", [])
    filtered = [
        t for t in tasks
        if not (isinstance(t, dict) and t.get("origin") == origin
                and t.get("id") not in known_ids)
    ]
    return {**tasks_doc, "tasks": filtered}


# Task 013 (PRD requirement I, #98): a task's TERMINAL statuses -- the set
# `steering_origin_outstanding` below waits for. Deliberately WIDER than
# `TASK_COMPLETION_STATUSES` (which excludes `failed` on purpose: a
# `COMPLETE` signal must never be honoured by a failed task, task 046/#55).
# For this gate a `failed` steering-origin task is just as SETTLED a fact as
# a `completed` one -- nothing further moves it on its own, and worker.md's
# own escape hatches (carve a new task, or relabel `skipped`) are what turn
# it into forward progress on a LATER iteration, not this gate holding the
# run open until that happens.
# Task 009 (requirement C.1, #114): `petitioned` joins this tuple --
# nothing further moves a petitioned task on its own either, until an
# adjudication (mid-run or approach-end review, task 010's scope) resolves
# it one way or the other. Deliberately NOT added to `TASK_COMPLETION_
# STATUSES` or `TASK_DEPENDENCY_SATISFIED_STATUSES`/`_DEAD_STATUSES` here --
# whether an un-adjudicated `petitioned` task counts as done or as doomed
# for a dependent task is task 010's `plan_complete` scope, not this one's.
# Task 016 (PRD requirement B.1, #156): `superseded` joins this tuple for
# the same reason `petitioned` did not join `TASK_COMPLETION_STATUSES` --
# terminality here means "nothing further moves it on its own", and a
# superseded parent has no transition out of `superseded` at all (its work
# moved to the children the same acceptance inserted). Without it a
# STEERING-ORIGIN task that gets force-split would hold the exit gate
# (`steering_origin_outstanding` -> `LoopSupervisor._terminal_success_
# blockers`) open for the rest of the run.
TASK_TERMINAL_STATUSES = ("validated", "completed", "failed", "skipped",
                          "petitioned", "superseded")


def task_is_terminal(task: dict) -> bool:
    """True iff `task`'s status is one of `TASK_TERMINAL_STATUSES` -- nothing
    further moves it without an operator/worker action creating a NEW
    status transition. A task with no status at all (malformed) is not
    terminal."""
    return isinstance(task, dict) and task.get("status") in TASK_TERMINAL_STATUSES


def steering_origin_outstanding(tasks_doc) -> list:
    """Every steering-origin task (`is_steering_origin`) that is NOT yet
    terminal (`task_is_terminal`), in plan order.

    Task 013 (PRD requirement I, #98): this is what the exit gate
    (`LoopSupervisor._steering_origin_outstanding`) waits on INSTEAD of a
    steering FILE being unconsumed (`RunDir.pending_steering`) -- "the run
    cannot reach terminal success while a steering-origin task is
    non-terminal", expressed against task state rather than the message
    that produced it. `settled` (the steer hat processed the message and
    applied its changes -- see `LoopSupervisor._step_generic_hat`'s
    `steer.settled` event) says nothing about `done` (this predicate): a
    message can be settled while the task it produced sits `pending`,
    `in-progress`, `validation-failed` or `awaiting-validation` for many
    iterations afterwards -- exactly the defect the PRD names (steer 003,
    `consumed` at iteration 87 with three quarters of it undone).

    Accepts either the whole `tasks.json` document (a dict with a `tasks`
    key) or a bare list of task dicts, mirroring `task_blocked_by`'s own
    tolerance -- callers that already hold one or the other should not have
    to unwrap it first.
    """
    tasks = (tasks_doc.get("tasks") if isinstance(tasks_doc, dict)
             else tasks_doc)
    return [t for t in (tasks or [])
            if is_steering_origin(t) and not task_is_terminal(t)]


def review_cure_origin_outstanding(tasks_doc) -> list:
    """Every `review-cure`-origin task (`is_review_cure_origin`) that is NOT
    yet terminal (`task_is_terminal`), in plan order -- the sibling of
    `steering_origin_outstanding` just above, for `_terminal_success_
    blockers`'s THIRD blocker (task 018, PRD requirement D.1, #220).

    A `review-cure`-origin task is engine-inserted output from a REFUTED
    petition's remedy (or an ordinary curable-rejection cure pass) -- the
    run must not go terminal-succeeded while that remedy task still sits
    non-terminal, for the exact same reason a steering-origin task's own
    `settled` is not `done`: the engine created real work and nothing has
    checked whether it actually landed.

    Accepts either the whole `tasks.json` document (a dict with a `tasks`
    key) or a bare list of task dicts, mirroring `steering_origin_
    outstanding`'s own tolerance.
    """
    tasks = (tasks_doc.get("tasks") if isinstance(tasks_doc, dict)
             else tasks_doc)
    return [t for t in (tasks or [])
            if is_review_cure_origin(t) and not task_is_terminal(t)]


def task_blocked_by(tasks: list, *,
                     include_petitioned: bool = False) -> dict[str, list[str]]:
    """Task 021 (#60 pillar 3): `{task id: [dead/doomed ancestor ids]}` for
    every task that can never be completed because a `dependsOn` entry --
    directly, or transitively through another doomed task -- reached
    `failed` or `skipped`. This is the PRD's "dead dependencies are
    harness-derived" requirement: the harness marks the dependents here, so
    no prompt has to notice and no agent has to write a note about it
    (worker.md used to ask for exactly that, by hand).

    Task 013 (PRD requirement C.1, #149): `include_petitioned=True` widens
    the same map with a SECOND, unrelated cause -- a task whose `dependsOn`
    names a `petitioned` ancestor DIRECTLY (one hop only, never propagated
    through the fixpoint below) is included too, with that ancestor as its
    cause. `petitioned` is deliberately never added to `dead`/`doomed`
    itself: a petition can still resolve (REFUTED keeps it exactly where it
    is; CONFIRMED moves it to `skipped` and re-points dependents, C.3's
    scope) so treating it as permanently dead here would make
    `_doomed_plan_fault`'s "nothing can ever move this plan again" verdict
    fire on a plan that is merely holding for an adjudication -- the one
    reason this is opt-in, `False` by default, so every existing caller
    (doom detection at `LoopSupervisor._step_worker`/`_doomed_plan_fault`)
    is untouched. The four display surfaces (`ralphctl tasks`, `GET
    /tasks`/`TasksRead.blocked_by`, the hub's `ui_server._with_task_
    blocked_by`) pass `True`; worker.md's own prose names the same map for
    the fourth (the worker prompt).

    A task already `completed`/`validated` is a SINK: whatever its own
    `dependsOn` said, the work is done, so doom does not propagate PAST it to
    its own dependents either -- a well-formed engine never lets that happen,
    but a hand-edited or legacy plan might, and the answer here must not
    invent a blocked label for a task that finished. A task already
    `failed`/`skipped` is a SOURCE of doom, never a KEY of the returned map
    (it is dead in its own right -- `task_failure_kind()`/plain `status`
    already say so; being a cause for someone else is a different fact from
    being blocked oneself). Everything else -- `pending`, `in-progress`,
    `validation-failed`, `awaiting-validation` -- is still in flight and can
    be doomed.

    Fixpoint over the dependency graph, so a chain of several doomed tasks
    (task 004 dead -> 005 doomed -> 006 doomed) is caught, not just direct
    dependents. Each task's list is its OWN direct `dependsOn` entries that
    are themselves doomed (dead or doomed), sorted, so a reader can follow
    the chain one hop at a time from any task without a second traversal.
    Ids only, never a live task dict held by reference. A task with no id is
    skipped, same rule as `task_failure_kinds`: there is nothing for a
    consumer to key on.
    """
    by_id: dict[str, dict] = {}
    depends_on: dict[str, list[str]] = {}
    order: list[str] = []
    for t in tasks:
        if not isinstance(t, dict):
            continue
        tid = t.get("id")
        if tid is None or tid == "":
            continue
        tid = str(tid)
        by_id[tid] = t
        order.append(tid)
        raw_deps = t.get("dependsOn")
        depends_on[tid] = ([str(d) for d in raw_deps]
                            if isinstance(raw_deps, list) else [])

    dead = {tid for tid, t in by_id.items()
            if t.get("status", "pending") in TASK_DEPENDENCY_DEAD_STATUSES}
    held: set[str] = set()
    if include_petitioned:
        held = {tid for tid, t in by_id.items()
                if t.get("status", "pending") == "petitioned"}
    doomed = set(dead)
    changed = True
    while changed:
        changed = False
        for tid in order:
            if tid in doomed:
                continue
            status = by_id[tid].get("status", "pending")
            if status in TASK_DEPENDENCY_SATISFIED_STATUSES:
                continue  # a sink: done regardless of what it depended on
            if any(dep in doomed for dep in depends_on.get(tid, [])):
                doomed.add(tid)
                changed = True

    out: dict[str, list[str]] = {}
    for tid in order:
        if tid in dead or tid in held:
            continue
        status = by_id[tid].get("status", "pending")
        if status in TASK_DEPENDENCY_SATISFIED_STATUSES:
            continue  # a sink: done regardless of what it depended on
        causes = sorted(dep for dep in depends_on.get(tid, [])
                         if dep in doomed or dep in held)
        if causes:
            out[tid] = causes
    return out


# Task 021: the field name under which the derived blocked-by map is
# published -- the exact sibling of `TASK_FAILURE_KINDS_FIELD` above, same
# reasoning: `GET /tasks`, `ralphctl tasks --json` and the hub's run-detail
# payload all serve `{task id: [ancestor ids]}`, never a copy.
TASK_BLOCKED_FIELD = "taskBlockedBy"


def task_failure_kind(task: dict) -> str | None:
    """Which kind of `failed` a task record is, or None when it is not failed.

    Reading rule, and the reason this is a function rather than a plain field
    read: `failureKind` is written by the engine from task 024 (#33) on, so
    every tasks.json written BEFORE it -- and every one an agent writes by hand
    without the label -- has no such key. Those are read by DERIVING the kind
    from the evidence already on the record: a task at or past
    `VALIDATION_ATTEMPT_LIMIT` recorded validation rounds is
    `validation-exhausted` (that is the only way the engine itself ever wrote
    `failed`), and anything else is an agent's own judgement that the
    requirement was not met. An unrecognised label is treated the same way, so a
    garbled value can never make a reader crash or invent a third meaning.
    """
    if not isinstance(task, dict) or task.get("status") != "failed":
        return None
    kind = task.get("failureKind")
    if kind in TASK_FAILURE_KINDS:
        return kind
    attempts = task.get("validationAttempts")
    if isinstance(attempts, int) and attempts >= VALIDATION_ATTEMPT_LIMIT:
        return TASK_FAILURE_VALIDATION_EXHAUSTED
    return TASK_FAILURE_REQUIREMENT_UNMET


def task_is_actionable(task: dict) -> bool:
    """True iff a worker iteration could still pick this task up.

    A task with no status at all counts as actionable: an unreadable plan must
    never look finished (same rule as `_all_tasks_completed`'s empty-plan
    guard)."""
    if not isinstance(task, dict):
        return True
    return task.get("status", "pending") in TASK_ACTIONABLE_STATUSES


def task_counts(tasks: list) -> dict:
    """Count a tasks.json task list into the /status `tasks` shape:
    {"total": N, "completed": N, "inProgress": N, "pending": N,
    "validationFailed": N, ...}. Only statuses actually present get a key
    (plus `total`, always); an unrecognised status is passed through under
    its own name, and a task with no status at all counts as "unknown".

    Task 025 (#33): a plan holding `failed` tasks also gets the sub-counts
    `failedValidationExhausted`/`failedRequirementUnmet` (only the kinds
    actually present, so a plan with none looks exactly as it did before this
    change and a pre-v0.7 engine's payload is still a valid counts dict).
    They are SUB-counts, not statuses: the status keys still sum to `total`,
    and the sub-counts sum to `failed`. Both invariants hold for a legacy
    record too, because the kind is derived (`task_failure_kind`).

    Task 021 (#60 pillar 3): a plan holding a dead dependency also gets a
    `blocked` OVERLAY count -- how many tasks `task_blocked_by()` dooms --
    present only when at least one task is actually doomed (same "only what
    is there gets a key" rule as the sub-counts, so a plan with no
    `dependsOn` at all, or none of them dead, looks exactly as it did before
    this existed). It is not a status either -- see
    `TASK_COUNT_OVERLAY_KEYS`, the exclusion list a consumer walking these
    keys as statuses must use, now that there are two kinds of overlay.
    """
    counts = {"total": len(tasks)}
    subcounts: dict = {}
    for t in tasks:
        raw = t.get("status") if isinstance(t, dict) else None
        key = _TASK_COUNT_KEYS.get(raw, raw or "unknown")
        counts[key] = counts.get(key, 0) + 1
        kind = task_failure_kind(t) if isinstance(t, dict) else None
        if kind:
            sub = TASK_FAILURE_COUNT_KEYS[kind]
            subcounts[sub] = subcounts.get(sub, 0) + 1
    counts.update(subcounts)
    blocked = task_blocked_by(tasks)
    if blocked:
        counts["blocked"] = len(blocked)
    return counts


def task_failure_kinds(tasks: list) -> dict:
    """Task 025 (#33): `{task id: kind}` for every failed task in a plan --
    the per-task half of the same answer `task_counts()`' sub-counts give in
    aggregate, for the surfaces that serve individual task records.

    Derived through `task_failure_kind()` (never copied out of the record), so
    a plan written before task 024 -- which carries no `failureKind` key at
    all -- gets the same answer as a plan the current engine labelled, and no
    renderer has to re-implement the derivation rule in another language.
    Tasks with no id are skipped: there is nothing for a consumer to key on.
    """
    out: dict = {}
    for t in tasks:
        kind = task_failure_kind(t) if isinstance(t, dict) else None
        if not kind:
            continue
        tid = t.get("id")
        if tid is None or tid == "":
            continue
        out[str(tid)] = kind
    return out


def format_task_status(task: dict, blocked_by: list | None = None,
                        progress_budget: dict | None = None) -> str:
    """Task 025 (#33): ONE task record's status as a human reads it --
    `failed (validation-exhausted)` / `failed (requirement-unmet)` for a failed
    task, the bare status for everything else.

    The single spelling behind `ralphctl tasks`, the hub's task row and its
    task dialog, worded exactly as `TASK_STATUS_LABELS` words the same two
    kinds in a counts summary -- so "1 failed (validation-exhausted)" in a
    tally and `failed (validation-exhausted)` on the task itself are the same
    words, not two vocabularies for one fact.

    Task 021 (#60 pillar 3): `blocked_by`, when given a non-empty list --
    this task's entry in `task_blocked_by()`'s map -- appends
    ` (blocked by 004, 005)` after the raw status, so the record's real
    status (`pending`, `in-progress`, ...) is never hidden by the overlay,
    only annotated. Optional and keyword-friendly so every existing call
    site that has no dependency graph handy keeps working unchanged.

    Task 011 (PRD requirement I, #98): a steering-origin task (`is_steering_
    origin()`, `task["origin"] == TASK_ORIGIN_STEER`) gets ` (steering-
    origin)` appended after the kind and before the blocked-by suffix, so
    every surface that already renders through this one function -- `ralphctl
    tasks`, the hub's task row and dialog -- marks an injected task without
    each surface re-testing the `origin` field itself.

    Task 021 (PRD requirement N, #102): the sibling case, `is_worker_
    origin()` (`task["origin"] == TASK_ORIGIN_WORKER`), gets ` (worker-
    origin)` in the same slot -- mutually exclusive with steering-origin
    (a task has exactly one `origin` value or none), so this is `elif`, not
    a second independent append.

    Task 014 (PRD requirement A.4, #156): `progress_budget` -- this run's
    `progressBudget` status.json record (`{taskId, used, budget}`, task
    013) -- appends ` (progress budget 2/3)` after the blocked-by suffix,
    but ONLY when this task's own `status` is `"in-progress"` and its id
    matches `progress_budget["taskId"]`: the record persists on disk after
    the task it named moves on, and re-showing a stale usage count next to
    a task that finished (or a DIFFERENT task that is now in progress)
    would misdescribe both. Optional and keyword-friendly, like
    `blocked_by` above, so every existing call site keeps working
    unchanged.

    Task 023 (PRD requirement C.1, #156): a task still carrying its own
    `deferredUntil` (a worker's `verdict: "deferred"` report, task 021)
    appends ` (deferred until <t>)` verbatim from that field -- the exact
    words `ralphctl tasks`, the hub and the worker prompt's own rendered
    section all use, read straight off the record itself rather than
    re-derived, so it renders even for a task record no other overlay in
    this function recognises.
    """
    if not isinstance(task, dict):
        return "unknown"
    status = task.get("status")
    status = str(status) if status else "unknown"
    kind = task_failure_kind(task)
    text = f"{status} ({kind})" if kind else status
    if is_steering_origin(task):
        text += " (steering-origin)"
    elif is_worker_origin(task):
        text += " (worker-origin)"
    if blocked_by:
        text += f" (blocked by {', '.join(str(b) for b in blocked_by)})"
    if (isinstance(progress_budget, dict) and status == "in-progress"
            and task.get("id") is not None
            and str(progress_budget.get("taskId")) == str(task.get("id"))):
        text += (f" (progress budget {progress_budget.get('used')}"
                 f"/{progress_budget.get('budget')})")
    if task.get("deferredUntil"):
        text += f" (deferred until {task['deferredUntil']})"
    return text


# Task 028 (PRD requirement D.4, #157): the SAME `estimatedIterations + 2`
# rule `LoopSupervisor._progress_budget_for` (`engine/loop.py`) already
# spends a task's progress budget by, named once here so `ralphctl tasks`
# (a plain CLI read with no `LoopSupervisor` instance to call a method on)
# can derive and DISPLAY the same number for every task in the plan, not
# just whichever one is currently in-progress (that live usage count is
# `format_task_status`'s separate ` (progress budget N/M)` suffix above,
# sourced from status.json's `progressBudget` record -- this is the static
# per-task figure the estimate alone determines, unconditional on status).
DEFAULT_TASK_PROGRESS_BUDGET_ESTIMATE = 3


def task_progress_budget_estimate(task: dict | None) -> int:
    """`estimatedIterations + 2` (PRD requirement A.3/D.4, #156/#157) for
    ONE task -- a non-dict task or a missing/non-numeric
    `estimatedIterations` both degrade to
    `DEFAULT_TASK_PROGRESS_BUDGET_ESTIMATE` (3), never raise, matching
    `plan_ingestion.DEFAULT_ESTIMATED_ITERATIONS` (1) + 2. This mirrors
    `LoopSupervisor._progress_budget_for` exactly (same arithmetic, same
    default) so the runtime exemption check and this display-only reader
    can never disagree about what a task's own budget is."""
    if not isinstance(task, dict):
        return DEFAULT_TASK_PROGRESS_BUDGET_ESTIMATE
    try:
        estimated = int(task.get("estimatedIterations", 1))
    except (TypeError, ValueError):
        estimated = 1
    return estimated + 2


# Task 013 (#21): how a `task_counts()` key is SPELLED for a human, in ONE
# place. `ralphctl status` (`cli/main.py:_summarize_tasks`), `ralphctl runs`
# and the hub's TASKS column all render the same counts dict, so the wording
# lives here beside the counter -- a second copy is how `1 in-progress` in
# one surface and `1 inProgress` in another get born.
TASK_STATUS_LABELS = {"inProgress": "in-progress",
                      "validationFailed": "validation-failed",
                      # Task 019 (requirement F): `validated` needs no entry
                      # here (no hyphen to spell out -- the raw key already
                      # reads as a human sentence), only `awaiting-validation`
                      # does, via its counts key.
                      "awaitingValidation": "awaiting-validation",
                      # Task 025 (#33): the two sub-counts read as what they
                      # are -- a kind OF `failed`, never a status of their own.
                      "failedValidationExhausted": "failed (validation-exhausted)",
                      "failedRequirementUnmet": "failed (requirement-unmet)"}

# Which statuses are *trouble* an at-a-glance view should flag, in the order
# they are shown (worst first). Not a rendering decision the hub gets to make
# on its own: `ralphctl runs` flags the same ones.
#
# Task 021 (#60 pillar 3): `blocked` leads -- a dead subtree cannot be
# resolved by another worker iteration at all, which is worse news than a
# task merely awaiting one (unlike `failed`, nothing routes a `blocked` task
# back to review on its own; an operator or a targeted replan has to act).
#
# Task 025 (#33): a `failed` task is trouble under BOTH meanings -- a
# requirement nobody met, or a task the engine gave up validating -- and it is
# worse news than a plan merely mid-flight, so the two kinds lead the rest.
# Plain `failed` is listed last of the three as the FALLBACK for a counts dict
# with no sub-counts (a pre-v0.7 engine's `GET /status`); `_failed_kinds_known`
# suppresses it whenever the kinds are known, so one failed task can never be
# flagged twice.
TASK_TROUBLE_KEYS = ("blocked", "failedRequirementUnmet", "failedValidationExhausted",
                     "failed", "validationFailed", "inProgress")


def _failed_kinds_known(counts: dict) -> bool:
    """True when a counts dict carries the failure-kind sub-counts, i.e. when
    rendering plain `failed` as well would count the same tasks twice."""
    if not isinstance(counts, dict):
        return False
    return any(counts.get(key) for key in TASK_COUNT_SUBKEYS)

# Task 015 (#21): the one glyph that says "this plan has trouble in it" in a
# COLUMN, where the flag sentences themselves do not fit. Spelled here so the
# hub cell (`app.js taskCell`, which appends the same `\u26A0`) and `ralphctl
# runs` mark the same fact with the same character; the *wording* of what is
# wrong stays `format_task_trouble`'s, served verbatim in `--json` and printed
# in full by `ralphctl status`.
TASK_TROUBLE_MARKER = "\u26a0"

# What `format_task_counts` says for a run with no counts at all.
NO_TASKS = "(none)"


def format_task_counts(counts: dict) -> str:
    """Render a `task_counts()`/`GET /status` `tasks` dict as the one human
    summary every surface uses: `7/7 completed`, or
    `5/7 completed (1 in-progress, 1 pending)` when something is outstanding
    (task 003; moved here from `cli/main.py` by task 013 so the hub can render
    exactly the same sentence instead of re-spelling it in JS).

    `NO_TASKS` for an empty dict -- an absent plan is not `0/0`.
    """
    if not counts:
        return NO_TASKS
    total = counts.get("total", 0)
    # Task 018 (#77/#78): the leading number is the SAME numerator
    # `format_task_fraction` uses -- every `TASK_COMPLETION_STATUSES` key
    # (`completed`, `validated`, `skipped`), not just the literal
    # `completed` -- so a fully-validated vigilant plan reads `17/17
    # completed`, not `0/17 completed (17 validated)`.
    completed = sum(counts.get(key, 0) for key in TASK_COMPLETION_STATUSES)
    kinds_known = _failed_kinds_known(counts)
    others = []
    for key, value in counts.items():
        if key in ("total",) or key in TASK_COMPLETION_STATUSES or not value:
            continue
        # Task 025 (#33): with the kinds known, `failed` is said twice over by
        # its own sub-counts -- printing both would make the parenthesised
        # list stop adding up to `total`.
        if key == "failed" and kinds_known:
            continue
        others.append(f"{value} {TASK_STATUS_LABELS.get(key, key)}")
    summary = f"{completed}/{total} completed"
    if others:
        summary += " (" + ", ".join(others) + ")"
    return summary


def format_task_fraction(counts: dict) -> str:
    """Task 013 (#21): the at-a-glance form of the same counts -- `5/7`, or
    an EMPTY string when there is no plan to have progress through.

    Never `0/0`: a run whose agent has not written a plan yet (and a run whose
    `tasks.json` could not be read at all) has no denominator, and printing
    one claims a fact -- "a plan of zero tasks" -- that nobody stated. Same
    discipline as `format_approach`: junk degrades to no answer rather than to
    a confident wrong one.
    """
    try:
        total = int(counts.get("total") or 0)
        # Task 018 (#77/#78): numerator is every `TASK_COMPLETION_STATUSES`
        # key summed, not just `completed` -- a fully-validated vigilant
        # plan (all `validated`, none literally `completed`) must read
        # `17/17`, not `0/17`.
        completed = sum(
            int(counts.get(key) or 0) for key in TASK_COMPLETION_STATUSES)
    except (TypeError, ValueError, AttributeError):
        return ""
    if total <= 0:
        return ""
    return f"{completed}/{total}"


def format_task_trouble(counts: dict) -> list[str]:
    """Task 013 (#21): the trouble flags for a counts dict, worded EXACTLY as
    `format_task_counts` words them (`['1 validation-failed', '2
    in-progress']`) -- so a compact column can flag a stuck plan without
    inventing a second vocabulary for the same statuses.

    Empty list when neither is present; counts of 0 are not flags.
    """
    if not isinstance(counts, dict):
        return []
    kinds_known = _failed_kinds_known(counts)
    out = []
    for key in TASK_TROUBLE_KEYS:
        value = counts.get(key)
        if key == "failed" and kinds_known:
            continue  # already flagged, by kind (task 025)
        if value:
            out.append(f"{value} {TASK_STATUS_LABELS.get(key, key)}")
    return out


def format_task_column(counts: dict, *, stale: bool = False) -> str:
    """Task 015 (#21): the compact TASKS cell for a run-list ROW, as one
    string -- `5/7`, `5/7 \u26a0` when something is validation-failed or
    in-progress, `5/7 \u26a0 stale` when the fraction came from the last-good
    payload (task 002's reader).

    This is the hub cell's text content flattened for a terminal column: the
    fraction is `format_task_fraction`'s, the trouble decision is
    `format_task_trouble`'s and the stale label is `TASKS_STALE_LABEL` -- one
    vocabulary, no second spelling of any of the three. The flag *sentences*
    do not fit a column, so they travel in `--json` (`tasksTrouble`) and in
    `ralphctl status`' sentence instead of being abbreviated here into a
    private wording.

    Empty string for a run with no plan (never `0/0`), and then no marker
    either: there is nothing to be in trouble about, and an `unreadable` read
    is ignorance rather than a stuck plan.
    """
    fraction = format_task_fraction(counts)
    if not fraction:
        return ""
    parts = [fraction]
    if format_task_trouble(counts):
        parts.append(TASK_TROUBLE_MARKER)
    if stale:
        parts.append(TASKS_STALE_LABEL)
    return " ".join(parts)


# ---------------------------------------------------------------------------
# Task 021 (#18.2): a run's state DOCUMENTS -- the prose an operator asks for
# when a run looks wrong and there is no container left to ask.
#
# Four things, all already on disk and none of them reachable from any surface
# until now: the worker's handoff `notes.md`, the reviewer's
# `review-findings.md`, the `composite-prd.md` a later approach works from, and
# the run's *effective* `job.yaml` -- the config as inlined at `start`, which is
# the only record of what the run was actually launched with (a `resume`
# re-reads that same file, so it is the truth `GET /config`'s effective view is
# derived FROM).
#
# ONE shaping, here, for both surfaces: `ralphctl docs` (task 021) and the hub's
# document dialogs (task 022) render the same dicts and the same wordings -- the
# discipline `iteration_detail`/`iteration_summary_lines` established for #18.1.
# On-disk only, like the iteration detail: these files are written by the engine
# and the agent into directories the host holds, so a dead run's documents read
# exactly like a live one's and there is nothing to fall back FROM (hence no
# `live` flag and no snapshot notice).
#
# `job.yaml` is REDACTED, mechanically, by `redact.redact_job_yaml` -- see that
# function for why name-masking and value-scrubbing are both needed.

# Where a document lives: the run dir, the run's job config dir (which the
# CLI/hub know as `<registry>/configs/<run-id>` and the engine as `/config`),
# or the run's `artifacts/` dir.
DOC_IN_RUN = "run"
DOC_IN_CONFIG = "config"
DOC_IN_ARTIFACTS = "artifacts"

# Task 006 (#140)'s run record, canonical HERE -- `run_record.py` imports these
# back (`from . import state` -> `state.RUN_RECORD_MD_FILE`) rather than
# redeclaring them, so the writer and this module's listings/aliases below can
# never name the file two different ways. `artifact_entries` already walks the
# whole `artifacts/` tree generically, so the pair is servable by `ralphctl
# artifacts`/the hub's artifacts panel with no further wiring; the two
# `RUN_DOCUMENTS` rows below and the `ARTIFACT_ALIASES` rows further down
# (task 007, B.1) give both files a well-known key and a description on both
# surfaces (`ralphctl docs`/`ralphctl artifacts`, and the hub panels reading
# the same dicts).
RUN_RECORD_MD_FILE = "run-record.md"
RUN_RECORD_JSON_FILE = "run-record.json"

# `(key, file name, where, what it is)`. The key is what an operator types
# (`ralphctl docs <run> notes`) and what the hub's dialog buttons carry; the
# file name is accepted as an alias, so both spellings work.
RUN_DOCUMENTS: tuple[tuple[str, str, str, str], ...] = (
    ("notes", NOTES_FILE, DOC_IN_RUN,
     "handoff notes the worker rewrites every iteration"),
    ("findings", FINDINGS_FILE, DOC_IN_RUN,
     "the reviewer's findings that sent the run into another approach"),
    ("composite-prd", COMPOSITE_PRD_FILE, DOC_IN_RUN,
     "the PRD text the agent works from (written when an approach restarts)"),
    ("debt", DEBT_FILE, DOC_IN_RUN,
     "the debt ledger, rendered for a human (task 006, requirement A.3)"),
    ("job", JOB_CONFIG_FILE, DOC_IN_CONFIG,
     "effective job config as inlined at start, secret values redacted"),
    ("run-record", RUN_RECORD_MD_FILE, DOC_IN_ARTIFACTS,
     "the engine's own terminal record, human-rendered (task 006, B.1)"),
    ("run-record-json", RUN_RECORD_JSON_FILE, DOC_IN_ARTIFACTS,
     "the JSON `run-record.md` renders from (task 006, B.1)"),
)

# Wordings, server-side and shared (the `NO_PRD`/`NO_TRANSCRIPT` discipline): a
# document this run never wrote, one that exists but is blank, one this reader
# cannot get at, and the note that says a body was redacted.
RUN_DOCUMENT_ABSENT = "(not written)"
RUN_DOCUMENT_EMPTY = "(empty)"
RUN_DOCUMENT_UNREADABLE = "(unreadable)"
RUN_DOCUMENT_REDACTED_NOTICE = "secret values redacted"


def run_document_keys() -> list[str]:
    """The operator-facing document keys, in listing order."""
    return [key for key, _, _, _ in RUN_DOCUMENTS]


def run_document_key(name: str) -> str | None:
    """Resolve what an operator typed to a document key: the key itself or the
    file name (`notes`, `notes.md`), case-insensitively. None when it names no
    known document (caller -> a usage error listing `run_document_keys()`)."""
    wanted = str(name).strip().lower()
    for key, filename, _, _ in RUN_DOCUMENTS:
        if wanted in (key, filename.lower()):
            return key
    return None


def _document_body(path: Path, redact_config_dir: Path | None) -> tuple[str, bool]:
    """`(body, redacted)` for one existing document file."""
    try:
        text = path.read_text(errors="replace")
    except OSError:
        return RUN_DOCUMENT_UNREADABLE, False
    if redact_config_dir is None:
        return text, False
    return redact_job_yaml(text, config_dir=redact_config_dir), True


def run_documents(run_root: Path, config_dir: Path | None = None, *,
                  bodies: bool = True) -> list[dict]:
    """Every known state document of a run, in `RUN_DOCUMENTS` order.

    One entry per document, present or not -- *which* documents exist is
    itself an answer an operator needs, so a file this run never wrote is
    reported as an entry with `exists: false` rather than dropped:

      `key`/`name`   the operator-facing key and the file name,
      `where`        `run` or `config` (which directory it came from),
      `title`        what the document is, in one line,
      `path`         where it would be, absent when this reader has no
                     directory to look in,
      `available`    False only for that case (no `config_dir` given, so
                     `job.yaml` is not *missing*, it is out of reach),
      `exists`       whether the file is there,
      `bytes`        its size on disk (0 when absent),
      `redacted`     True for a body that went through
                     `redact.redact_job_yaml` (only `job.yaml` does),
      `body`         the text, redacted where applicable -- omitted entirely
                     with `bodies=False` and for a document that does not
                     exist, since an empty string would read like an empty
                     file (`run_document_body` turns either into a wording).

    `config_dir` is the run's job config dir (`<registry>/configs/<run-id>`
    host-side, `/config` inside the container). Optional, so a caller holding
    only a run dir still gets the three run-dir documents.

    A plain function over paths, like `prd_path`/`steering_entries`: a
    read-only viewer must never construct a `RunDir` (whose `__post_init__`
    creates directories in somebody else's run dir).
    """
    root = Path(run_root)
    cdir = Path(config_dir) if config_dir is not None else None
    out = []
    for key, filename, where, title in RUN_DOCUMENTS:
        if where == DOC_IN_RUN:
            base = root
        elif where == DOC_IN_ARTIFACTS:
            base = root / ARTIFACTS_DIR_NAME
        else:
            base = cdir
        entry = {"key": key, "name": filename, "where": where, "title": title,
                 "available": base is not None, "exists": False, "bytes": 0,
                 "redacted": False}
        path = (base / filename) if base is not None else None
        if path is not None:
            entry["path"] = str(path)
            try:
                entry["exists"] = path.is_file()
                entry["bytes"] = path.stat().st_size if entry["exists"] else 0
            except OSError:
                entry["exists"] = False
        if entry["exists"] and bodies and path is not None:
            # The config dir is BOTH where `job.yaml` lives and where this
            # run's own secret values are staged, so it is exactly what the
            # redactor needs. A run-dir document is prose the agent wrote and
            # is served verbatim (transcript bytes were already scrubbed at
            # write time -- see docs/architecture.md's redaction section).
            body, redacted = _document_body(
                path, cdir if where == DOC_IN_CONFIG else None)
            entry["body"] = body
            entry["redacted"] = redacted
        out.append(entry)
    return out


def run_document(run_root: Path, key: str,
                 config_dir: Path | None = None) -> dict | None:
    """One document by key or file name (`run_document_key`'s aliases), or None
    when the name matches no known document."""
    resolved = run_document_key(key)
    if resolved is None:
        return None
    for entry in run_documents(run_root, config_dir):
        if entry["key"] == resolved:
            return entry
    return None


def run_document_body(doc: dict) -> str:
    """A document's body as something always printable: the text itself, or the
    one wording for blank / never-written / out-of-reach."""
    if not doc.get("exists"):
        return RUN_DOCUMENT_ABSENT if doc.get("available", True) \
            else RUN_DOCUMENT_UNREADABLE
    body = doc.get("body")
    if body is None:
        return RUN_DOCUMENT_ABSENT
    return body if body.strip() else RUN_DOCUMENT_EMPTY


def format_run_document_size(doc: dict) -> str:
    """One document's size cell, worded ONCE: its byte count when the file is
    there, else the one wording for never-written / out-of-reach. `ralphctl
    docs`' listing and header block print it and the hub's document panel
    (task 022) labels its buttons with it, so a file cannot be described as
    missing in one surface and empty in the other."""
    if doc.get("exists"):
        return f"{doc.get('bytes', 0):,}"
    return RUN_DOCUMENT_ABSENT if doc.get("available", True) \
        else RUN_DOCUMENT_UNREADABLE


def format_run_document_listing(docs: list[dict]) -> list[str]:
    """The "which documents exist" table as text lines, worded ONCE: `ralphctl
    docs <run>` prints these and the hub's document panel (task 022) labels its
    buttons from the very same fields."""
    lines = [f"{'DOCUMENT':<18}{'FILE':<20}{'SIZE':>13}  DESCRIPTION"]
    for doc in docs:
        lines.append(f"{doc.get('key', ''):<18}{doc.get('name', ''):<20}"
                     f"{format_run_document_size(doc):>13}  {doc.get('title', '')}")
    return lines


def run_document_summary_lines(doc: dict) -> list[str]:
    """One document's header block, worded ONCE: `ralphctl docs <run> <name>`
    prints these above the body and the hub's dialog (task 022) shows the very
    same lines, so the two surfaces cannot describe the same file differently.
    """
    if not isinstance(doc, dict):
        return []
    lines = [f"document:  {doc.get('key')}  ({doc.get('name')})",
             f"purpose:   {doc.get('title')}",
             f"size:      {format_run_document_size(doc)}"
             + (" bytes" if doc.get("exists") else "")]
    if doc.get("redacted"):
        lines.append(f"note:      {RUN_DOCUMENT_REDACTED_NOTICE}")
    return lines


def format_run_document_header(name) -> str:
    """The separator between a document's header block and its body -- the
    `format_iteration_log_header` role for #18.2."""
    return f"--- {name} ---"


def run_document_text(doc: dict) -> str:
    """The complete rendering of one document -- header block, separator, body
    -- as the single string both surfaces show (`iteration_view`'s `text`)."""
    return "\n".join([*run_document_summary_lines(doc),
                      format_run_document_header(doc.get("name")),
                      run_document_body(doc)])


# ---------------------------------------------------------------------------
# The debt ledger (task 006, requirement A.3 engine half, #155)
#
# A verifier that verifies a task anyway, but leaves `notes` on the payload
# (task 005, `HAT_SCHEMAS["verify"]`'s optional `notes` array), is recording a
# clause the deliverable met only in form, not in substance -- exactly the
# gap requirement A.1's rewritten "be strict" rule tells it to note rather
# than reject on. Those notes used to evaporate the moment `_verify_task`
# copied them onto the task's own `verifyNotes` field: nothing else ever read
# them again. `record_debt_entries` is the ONE place they are also appended,
# durably, to a per-RUN ledger (`debt.json` + a human render `debt.md`) that
# survives whichever task they were noted against -- `ralphctl docs`/the hub's
# document panel serve `debt.md` for free (`RUN_DOCUMENTS` below), and the run
# record (`run_record.py`) renders the same ledger's open/resolved split.
#
# An entry's `status` starts `"open"`; nothing in THIS task ever writes
# `"resolved"` -- that is task 007/008's job (letting a worker or an
# adjudication remedy resolve one). The render and the run record already
# handle both, so landing the resolution path later needs no second writer.
# ---------------------------------------------------------------------------

DEBT_STATUS_OPEN = "open"
DEBT_STATUS_RESOLVED = "resolved"


def read_debt_entries(run_root: Path) -> list[dict]:
    """Every debt entry this run has ever recorded, oldest first -- `[]` for
    a run that has none (never written, or the file does not parse)."""
    raw = read_json(Path(run_root) / DEBT_JSON_FILE, [])
    return raw if isinstance(raw, list) else []


def render_debt_markdown(entries: list[dict]) -> str:
    """The debt ledger rendered for a human -- `debt.md`'s whole body, and
    the section `run_record.render_run_record_markdown` echoes for the run
    record's own "open and resolved debt" (task 006's own wording). Open
    entries first (the ones still owed), then resolved ones, each worded
    identically to how `run_record.py` walks the same split -- one place,
    `state.py`, spells both."""
    lines = ["# Debt ledger", ""]
    open_entries = [e for e in entries if e.get("status") != DEBT_STATUS_RESOLVED]
    resolved_entries = [e for e in entries if e.get("status") == DEBT_STATUS_RESOLVED]
    lines.append(f"## Open ({len(open_entries)})")
    if not open_entries:
        lines.append("- (none)")
    for e in open_entries:
        lines.append(debt_entry_line(e))
    lines.append("")
    lines.append(f"## Resolved ({len(resolved_entries)})")
    if not resolved_entries:
        lines.append("- (none)")
    for e in resolved_entries:
        lines.append(debt_entry_line(e))
    return "\n".join(lines) + "\n"


def debt_entry_line(e: dict) -> str:
    """One debt entry as a single markdown bullet -- shared by `debt.md`
    and the run record's own render, so the two can never describe the same
    entry differently."""
    line = (f"- `{e.get('id')}` task {e.get('taskId')}: {e.get('clause')} -- "
           f"observed: {e.get('observed')}; suggested: {e.get('suggested')} "
           f"({e.get('ts')})")
    if e.get("status") == DEBT_STATUS_RESOLVED and e.get("resolvedBy"):
        line += f" [resolved by {e['resolvedBy']}]"
    return line


def record_debt_entries(run_root: Path, task_id: str,
                        notes: list[dict]) -> list[dict]:
    """THE one write path for the debt ledger (task 006): append one entry
    per item of `notes` (each `{clause, observed, suggested}`, task 005's
    `verifyNotes` shape) to `debt.json`, keyed `"<task_id>/<n>"` with `n`
    the 1-based count of entries this task id already has in the ledger --
    NOT a run-global counter, so re-reading `debt.json` alone tells you how
    many times a given task has been noted -- then re-render `debt.md` from
    the whole updated list in the same call, so the two files can never
    disagree about what the ledger holds.

    A non-list or empty `notes` writes nothing and returns the ledger
    unchanged -- called unconditionally from `_verify_task`'s verified
    path is safe exactly because of this no-op case.

    Returns the FULL ledger (existing entries plus whatever was just
    appended), so a caller that wants to render or inspect it need not
    read the file back.
    """
    entries = read_debt_entries(run_root)
    if not isinstance(notes, list) or not notes:
        return entries
    existing_for_task = sum(1 for e in entries if e.get("taskId") == task_id)
    ts = utcnow()
    for i, note in enumerate(notes):
        if not isinstance(note, dict):
            continue
        n = existing_for_task + i + 1
        entries.append({
            "id": f"{task_id}/{n}",
            "taskId": task_id,
            "clause": note.get("clause"),
            "observed": note.get("observed"),
            "suggested": note.get("suggested"),
            "ts": ts,
            "status": DEBT_STATUS_OPEN,
            "resolvedBy": None,
        })
    root = Path(run_root)
    atomic_write_json(root / DEBT_JSON_FILE, entries)
    atomic_write(root / DEBT_FILE, render_debt_markdown(entries))
    return entries


def resolve_debt_entries(run_root: Path, ids: list[str],
                         resolved_by: str) -> list[str]:
    """Task 007 (requirement A.3, #155 prompt half): the resolution
    counterpart `record_debt_entries`' own docstring named as "task
    007/008's job" -- flips every entry in `ids` whose current `status`
    is still `DEBT_STATUS_OPEN` to `DEBT_STATUS_RESOLVED`, stamping
    `resolvedBy` with `resolved_by` (the iteration's own commit sha, or
    whatever else a caller passes -- this function trusts it verbatim,
    the same way `record_debt_entries` trusts a verifier's note text).

    An id in `ids` that does not exist in the ledger, or that is already
    `DEBT_STATUS_RESOLVED`, is silently skipped -- never an error. A
    worker's `debt_resolved` report is advisory, not authoritative (task
    007's own PRD wording: "a worker MAY resolve an item"), so a stale
    or mistyped id must degrade to a no-op rather than reject the whole
    completion report over an unrelated typo.

    Re-renders `debt.md` from the whole updated list in the SAME call,
    exactly like `record_debt_entries` -- the two files can never
    disagree about what the ledger holds, whichever writer touched it
    last. Writes nothing, and returns `[]`, when nothing in `ids` was
    actually flipped (including the case where `ids` is empty/not a
    list), so a caller with nothing to apply never dirties the ledger's
    mtime for no reason.

    Returns the ids ACTUALLY flipped this call (a subset of `ids`, in
    ledger order) -- never `ids` itself echoed back -- so a caller can
    log or event exactly what changed rather than what was merely asked
    for.
    """
    entries = read_debt_entries(run_root)
    wanted = set(ids) if isinstance(ids, list) else set()
    flipped: list[str] = []
    for e in entries:
        if e.get("id") in wanted and e.get("status") != DEBT_STATUS_RESOLVED:
            e["status"] = DEBT_STATUS_RESOLVED
            e["resolvedBy"] = resolved_by
            flipped.append(e["id"])
    if flipped:
        root = Path(run_root)
        atomic_write_json(root / DEBT_JSON_FILE, entries)
        atomic_write(root / DEBT_FILE, render_debt_markdown(entries))
    return flipped


# ---------------------------------------------------------------------------
# Archived approaches (task 020, #24)
#
# Every approach an engine supersedes gets copied, verbatim, into
# `approaches/NN/` by `LoopSupervisor._archive_approach()` (`engine/loop.py`):
# that approach's own `tasks.json`, `notes.md` and `review-findings.md`,
# frozen at the moment it was abandoned. Task 019 (#24) made `ralphctl tasks
# <run> --approach N|--all` read them; the resolvers below are the SAME path
# logic (moved here from `cli/main.py` so the hub route added by this task
# reads through the identical resolver, never a second one) -- the CLI keeps
# calling these, unchanged in behaviour.

APPROACHES_DIR_NAME = "approaches"


def archived_approaches_dir(run_root: Path) -> Path:
    return Path(run_root) / APPROACHES_DIR_NAME


def archived_approach_dir(run_root: Path, approach: int) -> Path:
    """Where `_archive_approach()` writes approach `approach`'s frozen files
    -- zero-padded to two digits, exactly as `_archive_approach` names the
    directory."""
    return archived_approaches_dir(run_root) / f"{approach:02d}"


def archived_approach_numbers(run_root: Path) -> list[int]:
    """Every archived approach for a run, sorted numerically -- a directory
    name that is not a plain integer (nothing `_archive_approach` ever
    writes) is skipped rather than raising."""
    adir = archived_approaches_dir(run_root)
    if not adir.is_dir():
        return []
    numbers = []
    for p in adir.iterdir():
        if not p.is_dir():
            continue
        try:
            numbers.append(int(p.name))
        except ValueError:
            continue
    return sorted(numbers)


def archived_approach_summary(run_root: Path, approach: int) -> dict | None:
    """One archived approach's task counts, taken from ITS OWN archived
    `tasks.json` -- never recomputed from the live plan. None when this
    approach was never archived (caller -> a clean error/404, not a
    fabricated zero-count row)."""
    adir = archived_approach_dir(run_root, approach)
    if not (adir / "tasks.json").is_file():
        return None
    res = read_tasks_doc(adir, persist=False)
    counts = res.counts
    return {"approach": approach, "counts": counts,
            "summary": format_task_counts(counts), **res.contract}


def approach_metadata_path(run_root: Path) -> Path:
    """Task 035 (requirement B.1): where the engine keeps its own per-approach
    verdict metadata (`APPROACH_METADATA_FILE`) -- the run root, never an
    archive dir, so a single file answers for every approach the run planned."""
    return Path(run_root) / APPROACH_METADATA_FILE


def read_approach_metadata(run_root: Path) -> dict:
    """The whole engine-owned approach metadata document, `{"approaches":
    {"1": {...}}}` -- an empty shell when the file was never written (a run
    from before this task, or one whose judges have not run yet) or when it
    is unreadable, so every caller can index into `["approaches"]` without a
    guard and nothing here can raise into a terminal write path."""
    doc = read_json(approach_metadata_path(run_root), {})
    if not isinstance(doc, dict):
        return {"approaches": {}}
    approaches = doc.get("approaches")
    if not isinstance(approaches, dict):
        approaches = {}
    return {**doc, "approaches": approaches}


def approach_metadata_entry(run_root: Path, approach: int) -> dict:
    """Everything the engine recorded about ONE approach's judges -- `{}` when
    no judge ever settled a verdict for it."""
    entry = read_approach_metadata(run_root)["approaches"].get(str(approach))
    return entry if isinstance(entry, dict) else {}


def approach_metadata_channel(run_root: Path, approach: int,
                              channel: str) -> dict | None:
    """One judge channel's recorded verdict for one approach, or `None` when
    that judge never settled a verdict for it (a gate-off run has no
    `verdictGate` entry) -- never a fabricated empty verdict, the same
    "absent channel is not a verified one" discipline `run_record.
    _findings_channel` documents for the sidecars."""
    got = approach_metadata_entry(run_root, approach).get(channel)
    return got if isinstance(got, dict) else None


def record_approach_verdict(run_root: Path, approach: int, channel: str, *,
                            verdict: str, findings: list[dict] | None = None,
                            reason: str | None = None) -> dict:
    """Task 035 (requirement B.1, cure for task 006): PERSIST one judge's
    settled verdict for one approach into the engine-owned metadata above,
    and return the entry written.

    Called at the moment the judge's verdict is decided
    (`LoopSupervisor._review_verdict`, `LoopSupervisor._verdict_gate_verdict`)
    -- not at `finishing`, and not from the findings sidecars: `findings` may
    be `None` (no sidecar at all, or one that did not re-validate) and the
    entry is still written, with `blocking` and `residual` as EXPLICIT empty
    lists and `findingsChannel: false` saying why they are empty. That is the
    whole point of this record: "the judge rejected this approach" survives a
    missing sidecar.

    Idempotent per (approach, channel): a re-recorded verdict (a resumed
    process re-reviewing the same approach, requirement F.3) overwrites that
    channel's entry and leaves every other approach and channel untouched.
    """
    items = findings if isinstance(findings, list) else None
    entry = {
        "channel": channel,
        "verdict": verdict,
        "reason": reason,
        "recordedAt": utcnow(),
        "findingsChannel": items is not None,
        "blocking": [f for f in (items or []) if f.get("severity") == "blocking"],
        "residual": [f for f in (items or []) if f.get("severity") == "residual"],
    }
    doc = read_approach_metadata(run_root)
    approaches = dict(doc["approaches"])
    existing = approaches.get(str(approach))
    merged = dict(existing) if isinstance(existing, dict) else {}
    merged[channel] = entry
    merged["approach"] = approach
    approaches[str(approach)] = merged
    atomic_write_json(approach_metadata_path(run_root),
                      {**doc, "approaches": approaches})
    return entry


# Task 004 (v0.27, B.5 foundation, #274/#275 PRD): the RUN-scope, disk-backed
# record of every granted cure pass's own findings-to-cure-task-id map and
# the `pre_cure_head` it was handed -- the foundation B.4/B.5 build on:
# B.5's own words are "when a reopen becomes a new cure task, the new task
# records the cure task it descends from" and "the B.4 comparison reads
# that link from `tasks.json`, never from the id's spelling" -- this file is
# the OTHER half, the pass-level ledger a later confirmation review
# (`## Confirmation review`, B.1) reads to list "every blocking finding of
# the previous granted pass, named by the id of the cure task `insert_
# cure_tasks` built for each ... with its clause and `cured_when`" and "the
# diff range ... the `pre_cure_head` handed to `insert_cure_tasks`,
# persisted per pass if it is not already". Same run-root placement as
# `approach-metadata.json` beside it -- one file answers for every approach
# and every pass this run ever takes, never archived per-approach by
# `_archive_approach` (a later confirmation review needs the PREVIOUS
# approach's own passes exactly as much as the live one's).
#
# `insert_cure_tasks` below is the ONE writer, called from both granted-
# cure-pass call sites (`LoopSupervisor._take_cure_pass` for review's own
# edge, `_take_gate_cure_pass` for the peer verdict gate's) -- neither of
# those methods needs its own new call: both already pass `pre_cure_head`
# into `insert_cure_tasks`, so wiring the record there covers both without
# a second writer to keep in sync.
CURE_LINEAGE_FILE = "cure-lineage.json"


def cure_lineage_path(run_root: Path) -> Path:
    """Where the engine persists `CURE_LINEAGE_FILE` -- the run root,
    exactly like `approach_metadata_path` above."""
    return Path(run_root) / CURE_LINEAGE_FILE


def read_cure_lineage(run_root: Path) -> dict:
    """The whole lineage document, `{"passes": [...]}` -- an empty shell
    when the file was never written (a run from before this task, or one
    whose cure pass never touched a blocking finding) or is unreadable, so
    every caller can index `["passes"]` without a guard.

    Deliberately re-read from disk on every call, never cached on the
    `LoopSupervisor`/`RunDir` instance -- the same discipline `read_status`/
    `read_approach_metadata` already hold to, and what lets a fresh process
    over the same run dir (a resume, or a second `LoopSupervisor` in a test)
    see byte-for-byte the same record a live one wrote, with no shared
    Python state between them.
    """
    doc = read_json(cure_lineage_path(run_root), {})
    if not isinstance(doc, dict):
        return {"passes": []}
    passes = doc.get("passes")
    if not isinstance(passes, list):
        passes = []
    return {**doc, "passes": passes}


def record_cure_pass_lineage(run_root: Path, *, approach: int,
                             pre_cure_head: str | None,
                             findings_by_cure_task_id: dict,
                             kind: str = "granted") -> dict:
    """Append one pass entry -- `{"approach", "preCureHead", "findings",
    "kind", "recordedAt"}` -- to the lineage document and persist it.
    `findings` is `findings_by_cure_task_id` verbatim: one `{"clause",
    "curedWhen"}` per cure task `insert_cure_tasks` built for a blocking
    finding on THIS pass, keyed by that cure task's own (collision-
    resolved) id.

    cure-01-01 (PRD v0.27 B.1/B.3/B.4, review verdict-gate VG-01): `kind`
    (`insert_cure_tasks`'s own `pass_kind`, threaded through verbatim) is
    `"granted"` for a genuine cure pass (`_take_cure_pass`/`_take_gate_
    cure_pass`, this parameter's default) or `"remedy"` for a REFUTED-
    petition remedy insertion (`_insert_refuted_remedy_tasks`) -- so a
    reader can tell the two apart on disk without inspecting which caller
    wrote the entry. `LoopSupervisor._latest_granted_cure_pass` is the one
    place that reads it back.

    Called once per granted cure pass, from `insert_cure_tasks` itself
    (never from its two callers directly) -- so a THIRD caller of `insert_
    cure_tasks` (`LoopSupervisor._insert_refuted_remedy_tasks`, #209 defect
    4) also gets a lineage entry for its own remedy-derived cure task, with
    no special case needed here beyond the `kind` tag above: a remedy
    finding carries `clause`/`cured_when` exactly like a rejection finding
    does.

    Appends rather than overwrites -- a run's later cure pass (a fresh
    approach, or a second granted pass over the same one) grows the list;
    nothing already recorded is ever replaced or dropped, which is what
    lets a confirmation review's own diff range span every pass the
    approach has taken, not only its most recent one.
    """
    doc = read_cure_lineage(run_root)
    passes = list(doc["passes"])
    entry = {
        "approach": approach,
        "preCureHead": pre_cure_head,
        "findings": findings_by_cure_task_id,
        "kind": kind,
        "recordedAt": utcnow(),
    }
    passes.append(entry)
    atomic_write_json(cure_lineage_path(run_root), {**doc, "passes": passes})
    return entry


def root_cure_task_id(tasks: list, task_id: str) -> str:
    """Task 007 (PRD v0.27 requirement B.4/B.5, #274): follow `task_id`
    back through `tasks[].reopensCureTaskId` to its ultimate ancestor --
    the root cure task the whole reopen chain descends from (`cure-01-
    01-3` descends from `cure-01-01-2`, which may itself descend from
    `cure-01-01-1`; this returns `cure-01-01-1` for any of the three).
    `_review_cure_pass_available`'s identity-based comparison
    (`LoopSupervisor`, task 007) uses this to compare a confirmation
    review's own `reopens` targets by WHICH FINDING they are, never by a
    cure task id's spelling or its finding's `clause` text.

    `task_id` need not itself be present in `tasks` at all (a `reopens`
    value naming a task id this walk cannot resolve) -- such an id
    degrades to being its own root, the same posture a task genuinely
    carrying no `reopensCureTaskId` already gets. The walk is capped at
    `len(tasks) + 1` hops so a malformed/cyclic document (should never
    happen -- only `cure_task_from_finding` ever writes this field, and
    only backward to an EARLIER pass's own task) can never spin forever;
    it stops the moment an id repeats.
    """
    by_id = {t.get("id"): t for t in tasks if isinstance(t, dict)}
    seen: set = set()
    current = task_id
    for _ in range(len(tasks) + 1):
        if current in seen:
            break
        seen.add(current)
        task = by_id.get(current)
        if not isinstance(task, dict):
            break
        parent = task.get("reopensCureTaskId")
        if not isinstance(parent, str) or not parent.strip():
            break
        current = parent.strip()
    return current


def review_early_verdict_path(run_root: Path) -> Path:
    """Where `write_review_early_verdict`/`read_review_early_verdict` below
    keep review's early-verdict checkpoint -- the run root, exactly like
    `approach_metadata_path` above, so a single file answers across every
    approach's own review pass. Each write overwrites the previous
    checkpoint (there is never more than one review iteration in flight at
    once), and nothing reads this once that approach's ordinary contract
    payload has settled -- see `LoopSupervisor._review_verdict`."""
    return Path(run_root) / REVIEW_EARLY_VERDICT_FILE


def write_review_early_verdict(run_root: Path, *, approach: int, verdict: str,
                               reason: str | None = None,
                               findings_path: str | None = None) -> dict:
    """Task 025 (PRD requirement H.1/H.4, #264): the early-verdict
    checkpoint's ONE writer. Called from `hat_tools.SteerToolBroker`'s
    `record-verdict` tool handler, in the ENGINE process, the moment the
    review hat calls that tool -- never from inside the hat's own
    subprocess, so the write happens (and is durable) regardless of what
    that subprocess does next, including being SIGKILLed the instant after
    this call returns.

    Small and bounded by construction: five fixed keys, `reason` capped at
    `REVIEW_EARLY_VERDICT_REASON_MAX_CHARS`, and `atomic_write_json` below
    is `atomic_write`'s rename-into-place (`os.replace`, the same primitive
    every other engine-owned document in this module uses) over a document
    whose size never grows with anything review has produced so far -- the
    write this method performs costs the same whether review has written
    one line or a hundred pages of findings.
    """
    reason_text = None
    if isinstance(reason, str) and reason:
        reason_text = (reason
                       if len(reason) <= REVIEW_EARLY_VERDICT_REASON_MAX_CHARS
                       else reason[:REVIEW_EARLY_VERDICT_REASON_MAX_CHARS - 3]
                       + "...")
    payload = {
        "approach": approach,
        "verdict": verdict,
        "reason": reason_text,
        "findingsPath": findings_path if isinstance(findings_path, str) else None,
        "recordedAt": utcnow(),
    }
    atomic_write_json(review_early_verdict_path(run_root), payload)
    return payload


def read_review_early_verdict(run_root: Path) -> dict | None:
    """The checkpoint `write_review_early_verdict` last wrote, or `None`
    when review has never recorded one this run (a run from before task
    025, or an approach whose review call has not reached the tool yet).
    Never raises on a malformed/missing file -- a checkpoint is a
    nice-to-have salvage aid, not a document any caller may treat as
    required."""
    doc = read_json(review_early_verdict_path(run_root))
    return doc if isinstance(doc, dict) else None


def archived_approaches_list(run_root: Path) -> dict:
    """Every archived approach's summary, in order -- the payload `ralphctl
    tasks --all` (task 019) prints and the hub's approach-history panel
    (task 021) lists from, so the two agree on which approaches exist and
    what each one's counts are."""
    approaches = [archived_approach_summary(run_root, n)
                  for n in archived_approach_numbers(run_root)]
    return {"approaches": [a for a in approaches if a is not None]}


def archived_approach_view(run_root: Path, approach: int) -> dict | None:
    """One archived approach's full task table plus its `notes.md`/
    `review-findings.md` text, for the hub's approach-history dialog (task
    020, #24) -- None when this approach was never archived (caller -> 404).

    Tasks: `read_tasks_doc(...).payload`, the exact shape `GET /tasks` and
    `ralphctl tasks --approach --json` (task 019) already serve, so an
    archived approach's task table reads identically everywhere it appears.

    Notes/findings: shaped through `run_documents` -- the SAME dicts and
    `RUN_DOCUMENT_ABSENT` wording the live document dialogs use (task 022) --
    restricted to the two documents `_archive_approach` actually copies.
    `composite-prd.md`/`job.yaml` are never written into an archive dir, so
    they are left out of this view entirely rather than reported as an
    always-absent entry with no `config_dir` to blame; a caller that wants
    those still has the live document endpoints.
    """
    adir = archived_approach_dir(run_root, approach)
    if not (adir / "tasks.json").is_file():
        return None
    res = read_tasks_doc(adir, persist=False)
    docs = {d["key"]: d for d in run_documents(adir, None, bodies=True)
            if d["key"] in ("notes", "findings")}
    return {"approach": approach, "tasks": res.payload,
            "notice": res.notice or "",
            "notes": docs["notes"], "findings": docs["findings"]}


# ---------------------------------------------------------------------------
# Artifacts (task 023, #18.3)
#
# `artifacts/` is where the job leaves everything it wants an operator to see
# -- above all the reflect phase's post-mortem (`reflection/report.md`) and the
# prompt/skill diff it proposes (`reflection/suggestions.diff`), which until
# now could only be read by knowing the registry layout and `cat`-ing files.
# The shaping lives here, once, exactly like `run_documents` (#18.2): the CLI
# (`ralphctl artifacts`) and the hub (task 024) render the same dicts, so an
# artifact cannot be described as missing in one surface and empty in the other.

ARTIFACTS_DIR_NAME = "artifacts"

# `(key, path under artifacts/, what it is)` for the artifacts an operator asks
# for by name. Everything else in the tree is still listed and printable by its
# path -- these are only the well-known names and their one-line descriptions.
ARTIFACT_ALIASES: tuple[tuple[str, str, str], ...] = (
    ("report", "reflection/report.md",
     "the reflect phase's post-mortem report"),
    ("suggestions", "reflection/suggestions.diff",
     "the prompt/skill diff the reflect phase proposes (applied only by "
     "`ralphctl suggestions <run> apply --yes`)"),
    ("reflect-failed", "reflection/FAILED.md",
     "why the reflect phase left no report"),
    ("run-record", RUN_RECORD_MD_FILE,
     "the engine's own terminal record, human-rendered (task 006, B.1)"),
    ("run-record-json", RUN_RECORD_JSON_FILE,
     "the JSON `run-record.md` renders from (task 006, B.1)"),
)

# This run produced no artifacts at all -- the `NO_PRD`/`RUN_DOCUMENT_ABSENT`
# discipline, wording server-side so the terminal and the hub agree. Kept
# verbatim from what `ralphctl artifacts ls` has always printed.
NO_ARTIFACTS = "(no artifacts)"

# A file that is not text: printing it would spray a terminal (or a browser
# dialog) with bytes, so both surfaces say so and name the way to get it.
ARTIFACT_BINARY = "(binary file -- copy it out with `ralphctl artifacts <run> pull`)"

# How much of a file the text/binary sniff looks at.
ARTIFACT_SNIFF_BYTES = 8192

# The one server-decided "what is this file" answer both `ralphctl artifacts
# ls` and the hub's artifacts panel show (task 022, #54) -- decided from
# sniffed magic bytes, never a filename's extension, so a `.png` holding a ZIP
# and a `.bin` holding a PNG are never confused for one another.
ARTIFACT_KIND_TEXT = "text"
ARTIFACT_KIND_IMAGE = "image"
ARTIFACT_KIND_BINARY = "binary"

# The four raster signatures ralphd is willing to call "previewable" -- the
# same allow-list the raw byte route (task 023) will serve a `Content-Type`
# from. Spelled once so a later route cannot invent a fifth.
ARTIFACT_IMAGE_MIME_TYPES: tuple[str, ...] = (
    "image/png", "image/jpeg", "image/gif", "image/webp",
)

# The raw byte route (task 023, #54) never streams an artifact past this many
# bytes -- an operator's browser has no business rendering, say, a 200MB
# screenshot inline, and a job could otherwise make the hub's own response
# huge just by writing a big enough PNG. Above the cap the route answers with
# `ARTIFACT_BINARY` (today's `pull` wording) instead of the bytes, exactly as
# it already does for a non-image kind.
ARTIFACT_INLINE_MAX_BYTES = 5 * 1024 * 1024

# The text view (`artifact_entry` with bodies) never reads more than this many
# bytes of a file: a job can write a multi-gigabyte log, and the hub (or
# `ralphctl artifacts show`) has no business loading it whole. Over the cap the
# entry carries the first `ARTIFACT_PREVIEW_MAX_BYTES` bytes, `truncated: True`
# and the file's full size; `GET /api/runs/<id>/artifacts-download/<path>` (and
# `ralphctl artifacts <run> pull`) hand out the whole file.
ARTIFACT_PREVIEW_MAX_BYTES = 1024 * 1024

# The paged listing (`artifact_listing`): `limit` defaults to 200 and is
# clamped to 1000, so one request never describes more than a bounded page.
ARTIFACT_LIST_DEFAULT_LIMIT = 200
ARTIFACT_LIST_MAX_LIMIT = 1000

# What the paged listing calls a subdirectory (its entry carries `fileCount`),
# and the kinds it derives from a file name's extension alone -- the listing
# never opens a file, so it cannot sniff. `binary` is "no preview handles it".
ARTIFACT_KIND_DIR = "dir"
ARTIFACT_KIND_MARKDOWN = "markdown"
ARTIFACT_KIND_JSON = "json"
ARTIFACT_KIND_HTML = "html"
_ARTIFACT_EXTENSION_KINDS: dict[str, str] = {
    **{ext: ARTIFACT_KIND_IMAGE for ext in
       (".png", ".jpg", ".jpeg", ".gif", ".webp")},
    **{ext: ARTIFACT_KIND_MARKDOWN for ext in (".md", ".markdown")},
    ".json": ARTIFACT_KIND_JSON, ".jsonl": ARTIFACT_KIND_JSON,
    **{ext: ARTIFACT_KIND_HTML for ext in (".html", ".htm")},
    **{ext: ARTIFACT_KIND_TEXT for ext in
       (".txt", ".log", ".diff", ".patch", ".yaml", ".yml", ".toml", ".ini",
        ".cfg", ".csv", ".tsv", ".py", ".js", ".ts", ".sh", ".xml", ".rst",
        ".out", ".err", ".env")},
}


def _sniff_image_mime(data: bytes) -> str | None:
    """The `Content-Type` for `data` if its LEADING BYTES carry one of the
    four raster signatures ralphd previews inline, else None.

    Decided from magic bytes only, never a filename's extension: a file named
    `screenshot.png` whose bytes are actually a ZIP's signature (`PK\x03\x04`)
    answers None here, and a file named `payload.bin` whose bytes actually
    start with the PNG signature answers `"image/png"`.

    SVG is deliberately absent from this list even though it is, in the
    everyday sense, an image format. It has no raster magic-byte signature --
    it is XML text -- and rendering agent-authored SVG inline (an `<img>` or
    `<object>` whose `src` is a run's own artifact) can execute an embedded
    `<script>` or event handler against the hub's own origin: stored XSS
    wearing a picture's extension, not a preview. Do not add an SVG signature
    here; a later "just add svg support" patch is a regression against #54's
    acceptance criteria, not a feature, and the test pinning this is deliberate.
    """
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"GIF87a") or data.startswith(b"GIF89a"):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def _artifact_kind(is_text: bool, image_mime: str | None) -> str:
    """`ARTIFACT_KIND_IMAGE`/`_TEXT`/`_BINARY` from the two sniffs
    `artifact_entry` already runs. An image signature wins even if the head
    happened to decode as text (it never does for the four formats above, but
    the ordering is the deliberate one: previewable beats printable)."""
    if image_mime:
        return ARTIFACT_KIND_IMAGE
    return ARTIFACT_KIND_TEXT if is_text else ARTIFACT_KIND_BINARY


def artifact_names() -> list[str]:
    """The well-known artifact keys, in listing order."""
    return [key for key, _, _ in ARTIFACT_ALIASES]


def artifact_key(name) -> str | None:
    """The well-known key for a name -- the key itself or the path it stands
    for (`report`, `reflection/report.md`), case-insensitively -- or None for
    any other artifact (which is addressed by its path and has no key)."""
    wanted = str(name or "").strip().replace("\\", "/").lower()
    wanted = wanted.removeprefix(f"{ARTIFACTS_DIR_NAME}/")
    for key, path, _ in ARTIFACT_ALIASES:
        if wanted in (key, path.lower()):
            return key
    return None


def artifact_title(rel_path) -> str:
    """The one-line description of a well-known artifact, or '' for the rest.
    An arbitrary file the agent wrote describes itself by its path."""
    key = artifact_key(rel_path)
    for k, _, title in ARTIFACT_ALIASES:
        if k == key:
            return title
    return ""


def artifact_relpath(name) -> str | None:
    """Resolve what an operator (or a URL) named to a path *under* the run's
    `artifacts/` dir, or None when the name cannot be one.

    Accepts a well-known key (`report`), a path relative to `artifacts/`
    (`reflection/report.md`) and the same path spelled with the directory
    (`artifacts/reflection/report.md`), so every spelling a listing shows also
    works as an argument.

    None for anything that is not addressing an artifact: an empty name, an
    absolute path, or one containing `..`. That last one is the traversal
    guard, and it lives HERE rather than in each caller precisely because the
    hub (task 024) puts this string in a URL -- one resolver, one guard.
    """
    raw = str(name or "").strip().replace("\\", "/")
    if "\x00" in raw or raw.startswith("/"):
        return None
    for key, path, _ in ARTIFACT_ALIASES:
        if raw.lower() == key:
            return path
    raw = raw.removeprefix(f"{ARTIFACTS_DIR_NAME}/")
    parts = [p for p in raw.split("/") if p not in ("", ".")]
    if not parts or any(p == ".." for p in parts):
        return None
    return "/".join(parts)


def _artifact_is_text(data: bytes) -> bool:
    """Whether a file's leading bytes read as text. A NUL byte or a chunk that
    will not decode is binary -- deliberately crude, because the only decision
    it drives is "print this or point at `pull`"."""
    if b"\x00" in data:
        return False
    try:
        data.decode()
    except UnicodeDecodeError:
        # A multi-byte character cut in half by the sniff window is not a
        # binary file; anything else is.
        try:
            data[:-4].decode()
        except UnicodeDecodeError:
            return False
    return True


def artifact_entry(run_root: Path, rel_path: str, *,
                   bodies: bool = True) -> dict:
    """One artifact as both surfaces see it (the `run_documents` entry shape):

      `path`      its path relative to `artifacts/` (what a caller passes back),
      `key`       the well-known key, or None,
      `title`     the one-line description of a well-known artifact, else '',
      `file`      the absolute path on this host,
      `available` always True (an artifact is never out of reach the way
                  `job.yaml` can be) -- kept so the shared size/absence
                  wordings apply unchanged,
      `exists`    whether the file is there,
      `bytes`     its size on disk (0 when absent),
      `isText`    whether it can be printed at all (`ARTIFACT_BINARY` if not),
      `imageMime` the sniffed `Content-Type` (task 023's allow-list) when
                  `kind` is `ARTIFACT_KIND_IMAGE`, else None,
      `kind`      `ARTIFACT_KIND_TEXT`/`_IMAGE`/`_BINARY` -- the ONE
                  server-decided answer (task 022, #54) both `ralphctl
                  artifacts ls` and the hub's artifacts panel show, sniffed
                  from magic bytes and never from the extension,
      `body`      the text, at most `ARTIFACT_PREVIEW_MAX_BYTES` of it (a file
                  over the cap sets `truncated: True`; `bytes` is still the
                  full size) -- omitted entirely with `bodies=False`, for a file
                  that does not exist and for a binary one, since an empty
                  string would read like an empty file (`artifact_body` turns
                  each of those into its own wording).
    """
    rel = str(rel_path).replace("\\", "/")
    path = Path(run_root) / ARTIFACTS_DIR_NAME / rel
    entry = {"path": rel, "key": artifact_key(rel), "title": artifact_title(rel),
             "file": str(path), "available": True, "exists": False,
             "bytes": 0, "isText": True, "imageMime": None,
             "kind": ARTIFACT_KIND_TEXT}
    try:
        entry["exists"] = path.is_file()
        if entry["exists"]:
            entry["bytes"] = path.stat().st_size
    except OSError:
        entry["exists"] = False
        return entry
    if not entry["exists"]:
        return entry
    try:
        head = path.open("rb").read(ARTIFACT_SNIFF_BYTES)
    except OSError:
        entry["body"] = RUN_DOCUMENT_UNREADABLE
        return entry
    entry["isText"] = _artifact_is_text(head)
    entry["imageMime"] = _sniff_image_mime(head)
    entry["kind"] = _artifact_kind(entry["isText"], entry["imageMime"])
    if bodies and entry["isText"]:
        try:
            with path.open("rb") as fh:
                data = fh.read(ARTIFACT_PREVIEW_MAX_BYTES + 1)
        except OSError:
            entry["body"] = RUN_DOCUMENT_UNREADABLE
            return entry
        if len(data) > ARTIFACT_PREVIEW_MAX_BYTES:
            # The cap can cut a multi-byte character in half; drop the
            # fragment rather than show a replacement character at the seam.
            data = data[:ARTIFACT_PREVIEW_MAX_BYTES]
            for cut in range(4):
                try:
                    data[:len(data) - cut].decode()
                except UnicodeDecodeError:
                    continue
                data = data[:len(data) - cut]
                break
            entry["truncated"] = True
        entry["body"] = data.decode(errors="replace")
    return entry


def artifact_entries(run_root: Path, *, bodies: bool = False) -> list[dict]:
    """Every file under a run's `artifacts/`, in path order, as `artifact_entry`
    dicts. Bodies are off by default: a listing of a run's artifacts must not
    ship the artifacts themselves (the hub polls it).

    Only regular files: the directories are structure, not answers, and an
    empty `artifacts/` (or none at all) is simply an empty list, which the
    caller pairs with `NO_ARTIFACTS`.

    A plain function over paths, like `run_documents`/`steering_entries`: a
    read-only viewer must never construct a `RunDir` (whose `__post_init__`
    creates directories in somebody else's run dir).
    """
    root = Path(run_root) / ARTIFACTS_DIR_NAME
    try:
        found = sorted(p for p in root.rglob("*") if p.is_file())
    except OSError:
        return []
    return [artifact_entry(run_root, p.relative_to(root).as_posix(),
                           bodies=bodies)
            for p in found]


def artifact_kind_for_name(name) -> str:
    """The kind of a file decided from its NAME's extension alone -- what the
    paged listing reports, since it never opens a file. One of
    `ARTIFACT_KIND_IMAGE`/`_MARKDOWN`/`_JSON`/`_HTML`/`_TEXT`; every other
    extension (and none) is `ARTIFACT_KIND_BINARY`, i.e. no preview handles it.
    """
    return _ARTIFACT_EXTENSION_KINDS.get(
        PurePosixPath(str(name or "")).suffix.lower(), ARTIFACT_KIND_BINARY)


def _clamp_page(offset, limit) -> tuple[int, int]:
    try:
        offset = max(0, int(offset))
    except (TypeError, ValueError):
        offset = 0
    try:
        limit = int(limit)
    except (TypeError, ValueError):
        limit = ARTIFACT_LIST_DEFAULT_LIMIT
    return offset, max(1, min(limit, ARTIFACT_LIST_MAX_LIMIT))


def _count_files(directory: str) -> int:
    return sum(len(files) for _, _, files in os.walk(directory))


def artifact_listing(run_root: Path, *, offset=0, limit=None, q: str = "",
                     dir: str = "") -> dict | None:
    """One page of a run's artifacts, WITHOUT opening any file: the paged
    listing behind `GET /api/runs/<id>/artifacts` (#294).

    Returns `{total, offset, limit, entries}`, or None when `dir` names a
    directory `artifact_relpath` refuses (the caller answers 404 -- the one
    resolver, no second guard here). Each entry is `{path, name, size, mtime,
    kind}`: `path` is relative to `artifacts/` (what the view/download routes
    take back), `kind` comes from the extension alone, and the page's files
    are `stat`ed, never read.

    Without `dir` the listing is every file under the tree, flat, in path
    order. With `dir` it is that directory's direct children only: its
    subdirectories first, as entries of kind `ARTIFACT_KIND_DIR` carrying
    `fileCount` (every file beneath them), then its files. `q` keeps the
    entries whose relative path contains it, case-insensitively. `limit`
    defaults to `ARTIFACT_LIST_DEFAULT_LIMIT` and is clamped to
    `ARTIFACT_LIST_MAX_LIMIT`.

    Only the page's entries are stat-ed/counted, so a first page of a huge
    tree costs a directory walk plus 200 stats.
    """
    offset, limit = _clamp_page(offset, limit)
    base = Path(run_root) / ARTIFACTS_DIR_NAME
    rel_dir = ""
    if str(dir or "") != "":
        resolved = artifact_relpath(dir)
        if resolved is None:
            return None
        rel_dir = resolved
    needle = str(q or "").lower()
    found: list[tuple[int, str, str]] = []  # (sort group, rel path, abs path)
    try:
        if rel_dir:
            top = base / rel_dir
            with os.scandir(top) as it:
                children = list(it)
            for child in children:
                rel = f"{rel_dir}/{child.name}"
                try:
                    if child.is_dir(follow_symlinks=False):
                        found.append((0, rel, child.path))
                    elif child.is_file():
                        found.append((1, rel, child.path))
                except OSError:
                    continue
        else:
            for root, dirs, files in os.walk(base):
                prefix = os.path.relpath(root, base).replace(os.sep, "/")
                prefix = "" if prefix == "." else prefix + "/"
                for name in files:
                    found.append((1, prefix + name, os.path.join(root, name)))
    except OSError:
        found = []
    if needle:
        found = [f for f in found if needle in f[1].lower()]
    found.sort(key=lambda f: (f[0], f[1]))
    entries = []
    for group, rel, absolute in found[offset:offset + limit]:
        name = rel.rsplit("/", 1)[-1]
        try:
            st = os.stat(absolute)
        except OSError:
            continue
        entry = {"path": rel, "name": name, "mtime": round(st.st_mtime, 3)}
        if group == 0:
            entry.update(size=0, kind=ARTIFACT_KIND_DIR,
                         fileCount=_count_files(absolute))
        else:
            entry.update(size=st.st_size, kind=artifact_kind_for_name(name))
        entries.append(entry)
    return {"total": len(found), "offset": offset, "limit": limit,
            "entries": entries}


def artifact_download(run_root: Path, name) -> dict | None:
    """What the download route streams for `name`: `{file, size, filename}`
    (`file` a Path to open, never read whole here), or None when `name` cannot
    address an artifact (`artifact_relpath`'s guard -- the one resolver) or
    names no regular file. Any kind, any size: the caller streams it."""
    rel = artifact_relpath(name)
    if rel is None:
        return None
    path = Path(run_root) / ARTIFACTS_DIR_NAME / rel
    try:
        if not path.is_file():
            return None
        size = path.stat().st_size
    except OSError:
        return None
    return {"file": path, "size": size, "filename": path.name}


def artifact(run_root: Path, name, *, bodies: bool = True) -> dict | None:
    """One artifact by well-known key or path, or None when the name cannot
    address an artifact at all (`artifact_relpath`'s guard -- the caller turns
    that into a usage error, and a legal name that is simply not there into a
    `not written` answer)."""
    rel = artifact_relpath(name)
    if rel is None:
        return None
    return artifact_entry(run_root, rel, bodies=bodies)


def artifact_raw(run_root: Path, name) -> dict | None:
    """The raw-bytes answer for the hub's byte route (task 023, #54).

    Resolves `name` through `artifact_relpath` -- the ONE resolver, the same
    traversal/absolute-path guard every other artifact lookup uses -- and
    answers None for anything that name cannot address at all (caller ->
    404). This is deliberate: a second, ad-hoc path check anywhere on this
    route would be a second resolver, and the one guarding traversal is
    `artifact_relpath`'s docstring's whole point.

    For a name that DOES resolve, this never guesses a `Content-Type` from
    the URL or the extension -- only a file whose SNIFFED kind is
    `ARTIFACT_KIND_IMAGE` (the same sniff `artifact_entry` already runs) and
    whose size is at or under `ARTIFACT_INLINE_MAX_BYTES` is ever answered
    with its real bytes and a `Content-Type` drawn from
    `ARTIFACT_IMAGE_MIME_TYPES`. Everything else -- text, opaque binary, a
    file that was never written, or an image over the cap -- answers with
    `contentType=None` and today's `pull` wording (`ARTIFACT_BINARY`) as
    `body`, so a browser is never handed a stream of bytes it cannot make
    sense of; it gets the exact sentence a terminal already prints instead.

    Returns a dict with:
      `contentType`  one of `ARTIFACT_IMAGE_MIME_TYPES`, or None,
      `body`         the raw image bytes (contentType set), else the UTF-8
                     encoded `pull` wording.
    """
    rel = artifact_relpath(name)
    if rel is None:
        return None
    entry = artifact_entry(run_root, rel, bodies=False)
    if (not entry["exists"] or entry["kind"] != ARTIFACT_KIND_IMAGE
            or entry["bytes"] > ARTIFACT_INLINE_MAX_BYTES):
        return {"contentType": None, "body": ARTIFACT_BINARY.encode()}
    path = Path(run_root) / ARTIFACTS_DIR_NAME / rel
    try:
        data = path.read_bytes()
    except OSError:
        return {"contentType": None, "body": ARTIFACT_BINARY.encode()}
    return {"contentType": entry["imageMime"], "body": data}


def format_artifact_size(entry: dict) -> str:
    """One artifact's size cell -- the document rule
    (`format_run_document_size`) applied to artifacts, so a file's size and its
    absence are worded identically wherever ralphd shows a file."""
    return format_run_document_size(entry)


def format_artifact_listing(entries: list[dict]) -> list[str]:
    """The "what did this run leave behind" table as text lines, worded ONCE:
    `ralphctl artifacts <run> ls` prints these and the hub's artifacts panel
    (task 024) labels its rows from the very same fields. An empty list yields
    only the header -- the caller prints `NO_ARTIFACTS` instead."""
    lines = [f"{'SIZE':>10}  {'NAME':<16}PATH"]
    for entry in entries:
        lines.append(f"{format_artifact_size(entry):>10}  "
                     f"{entry.get('key') or '':<16}{entry.get('path', '')}")
    return lines


def artifact_summary_lines(entry: dict) -> list[str]:
    """One artifact's header block, worded ONCE: `ralphctl artifacts <run> show
    <name>` prints these above the body and the hub's dialog (task 024) shows
    the very same lines."""
    if not isinstance(entry, dict):
        return []
    name = entry.get("key")
    lines = [f"artifact:  {entry.get('path')}"
             + (f"  ({name})" if name else "")]
    if entry.get("title"):
        lines.append(f"purpose:   {entry.get('title')}")
    lines.append(f"size:      {format_artifact_size(entry)}"
                 + (" bytes" if entry.get("exists") else ""))
    if entry.get("truncated"):
        lines.append(f"preview:   first {ARTIFACT_PREVIEW_MAX_BYTES:,} bytes "
                     "only (download or `ralphctl artifacts <run> pull` "
                     "for the whole file)")
    return lines


def artifact_body(entry: dict) -> str:
    """An artifact's body as something always printable: the text itself, or
    the one wording for blank / never-written / unreadable / binary."""
    if not isinstance(entry, dict) or not entry.get("exists"):
        return RUN_DOCUMENT_ABSENT
    if entry.get("kind", ARTIFACT_KIND_TEXT) != ARTIFACT_KIND_TEXT:
        return ARTIFACT_BINARY
    body = entry.get("body")
    if body is None:
        return RUN_DOCUMENT_ABSENT
    if body == RUN_DOCUMENT_UNREADABLE:
        return RUN_DOCUMENT_UNREADABLE
    return body if body.strip() else RUN_DOCUMENT_EMPTY


def artifact_text(entry: dict) -> str:
    """The complete rendering of one artifact -- header block, separator, body
    -- as the single string both surfaces show (`run_document_text`'s role for
    #18.3)."""
    return "\n".join([*artifact_summary_lines(entry),
                      format_run_document_header(entry.get("path")),
                      artifact_body(entry)])


# ---------------------------------------------------------------------------
# Task 022 (P, #80): the router-calls surface.
#
# `engine.loop.LoopSupervisor._ask_router` (loop.py:2675-2699) is the ONLY
# model call in this engine that is deliberately, permanently uncounted: it
# never increments `self.iterations_used`, never opens an `iterations/NNNN/`
# directory, and its transcripts live under `artifacts/router/NNNN.jsonl`
# instead (the `_router_calls` counter at loop.py:164-170 numbers them). That
# is correct and load-bearing for the loop's own accounting, but it also
# means a router call had NO surface at all before this task -- an operator
# could not tell "the router ran and picked X" from "the router never ran"
# without reading a raw NDJSON transcript by hand and cross-referencing
# `transitions.TABLE` themselves.
#
# `LoopSupervisor._route_with_router` writes one JSON line to
# `artifacts/router/decisions.jsonl` per COMPLETED router decision (not per
# `_ask_router` attempt -- a decision that took three attempts before it
# fell back is still one entry, with `attempts: 3`), through
# `RunDir.record_router_decision` below. This is the ONE place that file is
# written; everything here is a read-only shaping of it, exactly like
# `artifact_entries`/`cost_breakdown` are read-only shapings of files the
# engine writes elsewhere.
#
# Task 019 (requirement M): `ROUTER_FALLBACK` has exactly ONE real entry
# (`plan_review`, the opt-in peer gate's own multi-edge state), which is why
# this file was still empty for every run that leaves that gate off. Task
# 016 (requirement D.2, #117 half B) then replaced `plan_review`'s own
# undecidable-reply call with a deterministic policy, so this file is now
# empty for EVERY shipped run, gate on or off -- `plan_review` stays the
# first *declared* router-eligible state (its table entry is still the one
# real multi-edge case), but no production caller reaches
# `engine.router.decide()` any more. See
# `artifacts/reports/router-calls-surface.md` for the pre-gate claim's
# evidence.

# `artifacts/router/decisions.jsonl`'s own filename, spelled once.
ROUTER_DECISIONS_FILENAME = "decisions.jsonl"

NO_ROUTER_CALLS = (
    "(no router calls this run -- every state this run reached decided its "
    "own edge deterministically; the only state whose table entry is "
    "genuinely router-eligible today is the opt-in peer plan-review gate's "
    "own `plan_review`, but its undecidable-reply branch now resolves "
    "deterministically too (task 016) -- no shipped run currently asks the "
    "router at all; see artifacts/reports/router-calls-surface.md)"
)

# Said on EVERY entry, not just in a header: a router call must never be
# mistaken for a work iteration by someone who only opened one entry's
# dialog rather than reading the panel's own caption.
ROUTER_CALL_NOTE = (
    "a router call decides which of a state's legal edges to take when "
    "there is more than one; it consumes no iteration and no approach "
    "(engine.loop.LoopSupervisor._ask_router never touches "
    "iterations_used) and opens no iterations/NNNN/ directory of its own"
)


def router_call_entry(raw: dict, index: int) -> dict:
    """One router decision, shaped for both surfaces from one
    `decisions.jsonl` line (`raw`) -- `cost_bucket` supplies the same
    `costDisplay`/`tokensDisplay` vocabulary every other usage surface
    uses, so a router call's own spend reads identically to a hat's.
    """
    usage = raw.get("usage") if isinstance(raw.get("usage"), dict) else {}
    bucket = cost_bucket(usage, key="router")
    return {
        "index": index,
        "ts": raw.get("ts"),
        "state": raw.get("state"),
        "vigilant": raw.get("vigilant"),
        "candidateEdges": raw.get("candidateEdges") or [],
        "chosenTarget": raw.get("chosenTarget"),
        "chosenGuard": raw.get("chosenGuard"),
        "usedRouter": raw.get("usedRouter"),
        "usedFallback": raw.get("usedFallback"),
        "fallbackGuard": raw.get("fallbackGuard"),
        "attempts": raw.get("attempts"),
        "model": raw.get("model"),
        "transcripts": raw.get("transcripts") or [],
        "tokens": bucket["tokens"],
        "tokensDisplay": bucket["tokensDisplay"],
        "costDisplay": bucket["costDisplay"],
        "costSource": bucket["costSource"],
        "chargesIteration": False,
        "note": ROUTER_CALL_NOTE,
    }


# #289: the `router.decision` event's `state` for the task-selection axis --
# the one axis `decisions.jsonl` never recorded (only `_route_with_router`
# writes there; `_route_task_selection` only emits the event).
TASK_SELECTION_ROUTER_STATE = "task_selection"


def task_selection_call_entry(ev: dict, index: int) -> dict:
    """#289: one `router.decision` event on the `task_selection` axis, shaped
    like `router_call_entry` so both surfaces render either kind from one
    dict. The task-selection shape has no edges or guards: its candidates are
    task ids (`taskCandidateIds`), it has a deterministic pick, and it
    carries the model's one-line `reason` -- all added as extra keys, with
    the edge-shaped keys filled so a reader that only knows the edge shape
    (`chosenTarget`, `usedFallback`) still prints something true.

    The event holds the call's own tokens/cost, not a usage dict, so one is
    rebuilt for `cost_bucket` -- `costUSD` (provider-quoted) and
    `costDerivedUSD` (host pricing map) stay separate, exactly as everywhere
    else; a run that predates the derived field simply shows no cost.
    """
    usage: dict = {}
    tokens = ev.get("tokens")
    if isinstance(tokens, (int, float)) and not isinstance(tokens, bool):
        usage["totalTokens"] = tokens
    cost = ev.get("costUSD")
    if isinstance(cost, (int, float)) and not isinstance(cost, bool):
        usage["costUSD"] = float(cost)
        usage["costPriced"] = True
    derived = ev.get("costDerivedUSD")
    if isinstance(derived, (int, float)) and not isinstance(derived, bool):
        usage["costDerivedUSD"] = float(derived)
        usage["costDerived"] = True
    bucket = cost_bucket(usage, key="router")
    ids = [str(i) for i in ev.get("candidateIds") or []]
    # #293: the OPTIONAL worker iteration this decision selected a task for;
    # an event written before the field existed has none, and the entry then
    # simply omits the key.
    it = ev.get("iteration")
    link = ({"iteration": it}
            if isinstance(it, int) and not isinstance(it, bool) and it > 0
            else {})
    return {
        **link,
        "index": index,
        "ts": ev.get("ts"),
        "state": ev.get("state"),
        "vigilant": None,
        "candidateEdges": [],
        "taskCandidateIds": ids,
        "deterministicPick": ev.get("deterministicPick"),
        "reason": ev.get("reason") or "",
        "chosenTarget": ev.get("choice"),
        "chosenGuard": None,
        "usedRouter": not ev.get("fallback"),
        "usedFallback": bool(ev.get("fallback")),
        "fallbackGuard": None,
        "fallbackCause": ev.get("fallbackCause"),
        "attempts": None,
        "model": ev.get("model"),
        "transcripts": ev.get("transcripts") or [],
        "tokens": bucket["tokens"],
        "tokensDisplay": bucket["tokensDisplay"],
        "costDisplay": bucket["costDisplay"],
        "costSource": bucket["costSource"],
        "chargesIteration": False,
        "note": ROUTER_CALL_NOTE,
    }


def router_call_entries(run_root: Path) -> list[dict]:
    """Every completed router decision this run recorded, oldest first, as
    `router_call_entry` dicts -- the ONE reader `ralphctl router` and the
    hub's router panel both use, mirroring `artifact_entries`'s role for
    `artifacts/`.

    Purely on-disk, like every other artifacts-derived surface: a live run
    and one whose container is long gone read identically. A run that never
    reached a router-ambiguous state (every shipped run, today) answers with
    an empty list, which the caller pairs with `NO_ROUTER_CALLS`.

    #289: two sources, merged by timestamp. The edge-based axis is read from
    `artifacts/router/decisions.jsonl` ONLY -- `_route_with_router` writes
    that line AND a `router.decision` event for the same call, so the event
    is deliberately ignored for it (never listed twice). The task-selection
    axis never wrote a decisions line, so it is read from `events.jsonl`
    (`state == "task_selection"` events): a pure on-disk read, which is why
    runs finished before this fix list their task-selection calls too.
    `index` is the 1-based position in the merged list (it used to be the
    decisions.jsonl line number, which is the same thing for a file without
    blank lines).
    """
    path = Path(run_root) / ARTIFACTS_DIR_NAME / "router" / ROUTER_DECISIONS_FILENAME
    try:
        lines = path.read_text().splitlines()
    except OSError:
        lines = []
    shaped: list[tuple[str, dict]] = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            raw = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(raw, dict):
            shaped.append((str(raw.get("ts") or ""), raw))
    for ev in read_events(run_root, types=("router.decision",)):
        if ev.get("state") == TASK_SELECTION_ROUTER_STATE:
            shaped.append((str(ev.get("ts") or ""), {"__event__": ev}))
    # Stable: equal timestamps (second resolution) keep decisions-then-events.
    shaped.sort(key=lambda pair: pair[0])
    entries = []
    for i, (_, raw) in enumerate(shaped, start=1):
        if "__event__" in raw:
            entries.append(task_selection_call_entry(raw["__event__"], i))
        else:
            entries.append(router_call_entry(raw, i))
    return entries


def router_call_summary_lines(entry: dict) -> list[str]:
    """One router call's header block, worded ONCE: `ralphctl router <run>
    show <index>` prints these and the hub's dialog (once one exists) would
    show the very same lines. Always ends with `ROUTER_CALL_NOTE` -- the
    "never a work iteration" fact belongs on every single reading of an
    entry, not only on the panel wrapping all of them.
    """
    if not isinstance(entry, dict):
        return []
    # #289: a task-selection entry has candidate task ids, a deterministic
    # pick and a reason instead of edges/guards/attempts.
    task_ids = entry.get("taskCandidateIds")
    if task_ids is not None:
        return [
            f"router call:  #{entry.get('index')}",
            f"state:         {entry.get('state')}",
            f"candidates:    {', '.join(task_ids) or '(none recorded)'}",
            f"deterministic: {entry.get('deterministicPick')}",
            f"chosen:        {entry.get('chosenTarget')}",
            f"reason:        {entry.get('reason') or '(none)'}",
            f"fallback used: {bool(entry.get('usedFallback'))}"
            + (f" (cause={entry.get('fallbackCause')})"
               if entry.get("usedFallback") else ""),
            f"model:         {entry.get('model') or '(unset -- pi default)'}",
            "transcripts:   "
            + (", ".join(entry.get("transcripts") or []) or "(none recorded)"),
            f"tokens:        {entry.get('tokensDisplay')}",
            f"cost:          {entry.get('costDisplay') or USAGE_NONE}",
            "",
            entry.get("note") or ROUTER_CALL_NOTE,
        ]
    candidates = ", ".join(
        f"{e.get('target')}({e.get('guard')})"
        for e in entry.get("candidateEdges") or [] if isinstance(e, dict)
    ) or "(none recorded)"
    lines = [
        f"router call:  #{entry.get('index')}",
        f"state:         {entry.get('state')}",
        f"candidates:    {candidates}",
        f"chosen:        {entry.get('chosenTarget')} "
        f"(guard={entry.get('chosenGuard')})",
        f"fallback used: {bool(entry.get('usedFallback'))}"
        + (f" (declared fallback guard={entry.get('fallbackGuard')})"
           if entry.get("usedFallback") else ""),
        f"attempts:      {entry.get('attempts')}",
        f"model:         {entry.get('model') or '(unset -- pi default)'}",
        f"tokens:        {entry.get('tokensDisplay')}",
        f"cost:          {entry.get('costDisplay') or USAGE_NONE}",
        "",
        entry.get("note") or ROUTER_CALL_NOTE,
    ]
    return lines


def format_router_calls_listing(entries: list[dict]) -> list[str]:
    """The "what has this run's router decided" table as text lines, worded
    ONCE: `ralphctl router <run>` prints these and the hub's router panel
    labels its rows from the very same fields. An empty list yields only the
    header -- the caller prints `NO_ROUTER_CALLS` instead."""
    lines = [f"{'#':>3}  {'STATE':<28}{'CHOSEN':<20}{'FALLBACK':<10}"
             f"{'ATTEMPTS':>9}  MODEL"]
    for e in entries:
        lines.append(
            f"{e.get('index'):>3}  {str(e.get('state') or ''):<28}"
            f"{str(e.get('chosenTarget') or ''):<20}"
            f"{str(bool(e.get('usedFallback'))):<10}"
            f"{e.get('attempts') or 0:>9}  {e.get('model') or '(unset)'}")
    return lines


# ---------------------------------------------------------------------------
# Task 025 (#18.4): the fault explanation.
#
# The engine already records everything needed to explain a fault -- the
# classifier's verdict per iteration (`faultClass` in
# `iterations/NNNN/meta.json`), the retry attempts and their backoffs
# (`infra_retry`/`infra_wait` events), the degraded half of the status
# contract (`health`/`infraWait`/`infraWaitTotalS`) -- but nothing joined
# them up: an operator staring at `faultClass: "infra"` still had to know
# `engine/faults.py`' signature table by heart to learn WHY, grep
# `events.jsonl` for the attempt number, and do the outage-budget arithmetic
# by hand. This is that join, shaped ONCE (the `iteration_detail` /
# `run_documents` discipline): `ralphctl fault` prints these dicts and the
# hub's fault dialog (task 026) renders the very same lines.
#
# Purely on-disk, like every other explanation surface: status.json,
# events.jsonl and the iteration metas are the engine's own writes, so a live
# run and one whose container is long gone read identically.

# This run never recorded a fault -- said out loud, the `NO_ARTIFACTS` /
# `RUN_DOCUMENT_ABSENT` discipline, because "nothing went wrong" is an answer.
NO_FAULT = "(no fault recorded)"

# The error text was not matched by any row of `faults.INFRA_SIGNATURES`
# (which is normal for a work fault, and is itself why an unclassifiable
# no-traffic failure is treated as infra).
FAULT_SIGNATURE_NONE = "(no signature matched)"

# Nothing was retried: no `infra_retry` attempt is recorded for the episode.
FAULT_LADDER_NONE = "(nothing retried)"

# The retry wrapper is demonstrably acting on an infra fault (its own
# `infra_retry`/`infra_wait` events say so) but the failing iteration's
# meta.json is not readable -- mid-write, or an iteration dir removed by hand.
# The class is the engine's own; the classifier's reasoning is not re-derivable
# from an event, so this says where the verdict came from instead of inventing
# a branch it did not take.
FAULT_REASON_FROM_EVENT = (
    "the engine's retry wrapper recorded an infra fault (read from this run's "
    "own retry events -- the iteration's meta.json is not readable)")

# `cfg.infra_retry_max` is None by default: the wall-clock outage budget is
# the stopping rule, not an attempt count (loop.py's own words).
FAULT_LADDER_UNCAPPED = "no cap: the outage budget is the stopping rule"

# `loop._reflect_pre_attempt_wait` publishes its pre-reflect wait as attempt 0
# -- deliberately not one of the episode's retries, because the job has already
# ended and this is only the delay before reflect's single attempt.
FAULT_LADDER_REFLECT_DELAY = (
    "waiting before the first reflect attempt (not a retry: the job already "
    "ended on an infra fault)")

# An outage episode that has ended: the engine emitted `infra_recovered`, i.e.
# an iteration reached the model again.
FAULT_RECOVERED_NOTICE = "the endpoint recovered: a later iteration reached the model"

# The verdict recorded by the engine and the one this shaping re-derives from
# the same meta.json disagree. The usual, legitimate cause is the abort carve-out
# (`operator_abort`, task 003 of #11): an abort or interrupt recorded for the run
# -- by the operator, or by the engine giving up on its own -- makes a failure a
# `work` fault though its error text and traffic look infra, and that input is
# not part of the iteration's meta.json. Never silently resolved -- the ENGINE's
# verdict is what the run acted on, and the divergence is shown.
#
# Steering 004: this notice does NOT name the operator as the cause, because the
# engine's input could not (see `faults.explain_fault`); the run's own
# `abortReason` is reported verbatim beside it instead.
FAULT_VERDICT_DIVERGED_NOTICE = (
    "!! the engine recorded a different class than this error alone implies "
    "(usually an abort/interrupt recorded for the run, which is never retried)")

# Task cure-01-08 (requirement G, F08): a recorded/derived disagreement has
# two very different causes, and only one of them is the genuine kind
# `FAULT_VERDICT_DIVERGED_NOTICE` exists for. A run dir recorded before this
# wave's `billable_tokens` fix (task 021) can show `faultClass=work` for a
# zero-token `terminated` iteration purely because the OLD engine read a
# zero-filled `usage` dict's mere PRESENCE as traffic (`bool(usage)`) where
# today's rule reads its token COUNT (`billable_tokens(usage) > 0`) -- see
# `_recorded_fault_notice`, which re-asks `explain_fault` the OLD question
# and only uses this notice when that reproduces the recorded verdict
# exactly. Any other disagreement (an abort carve-out, a genuinely different
# reason) still gets the generic notice above, unchanged.
FAULT_VERDICT_RECLASSIFIED_NOTICE = (
    "the engine recorded this before the zero-token traffic fix (requirement "
    "G): today's token-count rule reads it differently, but the OLD "
    "presence-of-usage rule reproduces exactly what was recorded, so this is "
    "the known historical reclassification, not a new disagreement")


def _recorded_fault_notice(detail: dict, recorded_fault, derived: dict) -> str | None:
    """Which notice (if any) belongs beside a recorded/derived `faultClass`
    disagreement -- shared by `read_iteration_detail` and `fault_explanation`
    so the two readbacks can never word the same disagreement differently.

    Returns `None` when there is no disagreement, `FAULT_VERDICT_RECLASSIFIED_
    NOTICE` when the disagreement is fully explained by task 021's own fix
    (the old presence-of-usage formula reproduces the recorded verdict
    exactly), and `FAULT_VERDICT_DIVERGED_NOTICE` for every other kind of
    disagreement -- so a genuine one (an abort carve-out, or anything else)
    is never globally suppressed by this check.
    """
    if derived["faultClass"] == recorded_fault:
        return None
    usage = detail.get("usage") if isinstance(detail.get("usage"), dict) else None
    old_style = explain_fault(
        error_text=str(detail.get("error") or ""),
        exit_code=(detail.get("exitCode")
                   if isinstance(detail.get("exitCode"), int)
                   and not isinstance(detail.get("exitCode"), bool) else None),
        interrupted=bool(detail.get("interrupted")),
        timed_out=bool(detail.get("timedOut")),
        no_traffic_timeout=bool(detail.get("noTrafficTimeout")),
        produced_traffic=bool(usage),
        **meta_provider_evidence(detail),
    )
    if old_style["faultClass"] == recorded_fault:
        return FAULT_VERDICT_RECLASSIFIED_NOTICE
    return FAULT_VERDICT_DIVERGED_NOTICE

# Event types the explanation reads, so a huge events.jsonl is not held in
# memory to answer one question.
FAULT_EVENT_TYPES = ("infra_retry", "infra_wait", "infra_recovered",
                     "reflect_infra_delay", "rate_limit_wait_started",
                     "rate_limit_wait_ended", "provider_refused",
                     "rate_limit_ceiling_reached")


def meta_provider_evidence(meta: dict) -> dict:
    """The structured provider evidence an iteration's meta.json records
    (v0.30 F.1: `rateLimit`, `rateLimitFresh`, `httpStatus`, `errorType`), as
    `faults.explain_fault` keyword arguments, so a re-derived class matches
    the one the engine recorded. Empty for a meta.json without them."""
    out: dict = {}
    rate_limit = meta.get("rateLimit")
    if isinstance(rate_limit, dict):
        out["rate_limit"] = rate_limit
        out["rate_limit_fresh"] = bool(meta.get("rateLimitFresh"))
    status = meta.get("httpStatus")
    if isinstance(status, int) and not isinstance(status, bool):
        out["http_status"] = status
    error_type = meta.get("errorType")
    if isinstance(error_type, str) and error_type:
        out["error_type"] = error_type
    return out


def format_rate_limit_wait_line(wait) -> str | None:
    """The one wording of a rate-limit wait in progress (v0.30 F.2), shared by
    `ralphctl status`, `ralphctl fault` and the hub: `waiting:   rate limit
    (<type>) until <local time>`. None when `wait` is not a wait."""
    if not isinstance(wait, dict):
        return None
    rl_type = wait.get("type") or "unknown type"
    return (f"waiting:   rate limit ({rl_type}) until "
            f"{format_local_time(wait.get('resetsAt'))}")


# v0.30 G.2: how much of a refused provider message the `paused:` line quotes.
PAUSE_DETAIL_MESSAGE_MAX = 200


def format_pause_detail_line(detail) -> str | None:
    """The one wording of a pause's sub-reason (status.json `pauseDetail`),
    shared by `ralphctl status` and `ralphctl fault`: for a provider refusal,
    `paused:    provider refused (<family>): <message> -- fix the profile or
    credentials, then ralphctl unpause`; for the rate-limit ceiling, `paused:
    rate-limit ceiling reached (<waited> waited of <max>); the <type> window
    resets at <local time> -- ...`. None when there is no detail."""
    if not isinstance(detail, dict) or not detail.get("reason"):
        return None
    reason = str(detail["reason"])
    if reason == "provider_refused":
        line = f"paused:    provider refused ({detail.get('family') or 'unknown'})"
        message = _one_line(str(detail.get("message") or "").strip(),
                            PAUSE_DETAIL_MESSAGE_MAX)
        if message:
            line += f": {message}"
        return (line + " -- fix the profile or credentials (PUT /config/llm), "
                "then ralphctl unpause")
    if reason == RATE_LIMIT_CEILING_REASON:
        rl_type = detail.get("rateLimitType") or "unknown type"
        return (f"paused:    rate-limit ceiling reached "
                f"({format_duration(detail.get('waitedS'))} waited of "
                f"{format_duration(detail.get('maxWaitS'))}); the {rl_type} "
                f"window resets at {format_local_time(detail.get('resetsAt'))} "
                "-- ralphctl unpause resets the count, ralphctl budget "
                "--rate-limit-wait raises the ceiling")
    return f"paused:    {reason.replace('_', ' ')}"


def read_events(run_root: Path, types=None, limit: int | None = None) -> list[dict]:
    """A run dir's `events.jsonl` as dicts, oldest first (task 025, #18.4).

    `types` keeps only those event types; `limit` keeps only the LAST that many
    matching events. A line that does not parse is skipped rather than raised
    on: the file is appended to by a live engine, so the last line can be a
    half-written one -- the #15 rule (a mid-write file is not an empty one),
    applied to the event log. A missing file is an empty list.
    """
    wanted = set(types) if types else None
    out: list[dict] = []
    try:
        with open(Path(run_root) / "events.jsonl") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    ev = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(ev, dict):
                    continue
                if wanted is not None and ev.get("type") not in wanted:
                    continue
                out.append(ev)
    except OSError:
        return []
    return out[-limit:] if limit is not None and limit >= 0 else out


def _last_fault_iteration(run_root: Path) -> dict | None:
    """The most recent iteration whose meta.json records a fault verdict, as
    `iteration_detail`'s dict (so the explanation reuses that shaping, exit
    reason and all), or None when no iteration ever failed.

    Scanned newest-first and stopped at the first hit: "the current/last
    fault" is the one an operator is asking about, and a finished run's
    earlier faults are still readable through `ralphctl iteration`.
    """
    for number in reversed(iteration_numbers(Path(run_root))):
        detail = iteration_detail(Path(run_root), number)
        if not isinstance(detail, dict):
            continue
        fault = detail.get("faultClass")
        if isinstance(fault, str) and fault:
            return detail
    return None


def _fault_episode(events: list[dict]) -> dict:
    """The infra-retry episode the run is in (or ended in), from its own
    events: the `infra_retry` attempts recorded AFTER the last
    `infra_recovered`.

    That boundary is the engine's own (`loop._reset_infra_episode` emits
    `infra_recovered` exactly when an iteration reaches the model again and
    the backoff schedule/outage budget start over), so this reads the episode
    clock rather than inventing a second definition of "one outage".
    """
    recovered = False
    attempts: list[dict] = []
    for ev in events:
        kind = ev.get("type")
        if kind == "infra_recovered":
            # A new episode starts from a clean clock: forget the old attempts,
            # but remember that recovery happened (it is the answer for a run
            # that rode an outage out successfully).
            attempts = []
            recovered = True
        elif kind == "infra_retry":
            attempts.append(ev)
            recovered = False
    return {"attempts": attempts, "recovered": recovered}


def format_fault_signature(signature: dict | None) -> str:
    """Which row of `faults.INFRA_SIGNATURES` matched, in one line:
    `dns -- the endpoint's name did not resolve (pattern EAI_AGAIN, matched
    "EAI_AGAIN")`, or `FAULT_SIGNATURE_NONE`."""
    if not isinstance(signature, dict) or not signature.get("family"):
        return FAULT_SIGNATURE_NONE
    line = str(signature["family"])
    if signature.get("description"):
        line += f" -- {signature['description']}"
    bits = []
    if signature.get("pattern"):
        bits.append(f"pattern {signature['pattern']}")
    if signature.get("match"):
        bits.append(f'matched "{signature["match"]}"')
    return line + (f" ({', '.join(bits)})" if bits else "")


def format_fault_ladder(ladder: dict | None) -> str:
    """Where the run stands in the infra retry ladder:
    `attempt 4 of 6, waits so far 30s, 1m, 2m, next attempt in 58s` -- or
    `FAULT_LADDER_NONE` when nothing was retried.

    The ladder is the run's OWN recorded backoffs (one `infra_retry` event per
    attempt), not `cfg.infra_retry_backoff_s` re-simulated: a wait cut short by
    POST /retry, a wait clamped by what was left of the outage budget and a
    reflect-phase episode on its own shorter budget all really happened that
    way, and the config is not in the run dir to be trusted about it anyway.
    """
    if not isinstance(ladder, dict) or ladder.get("attempt") is None:
        return FAULT_LADDER_NONE
    cap = ladder.get("maxAttempts")
    if ladder["attempt"] == 0:
        head = FAULT_LADDER_REFLECT_DELAY
    else:
        head = f"attempt {ladder['attempt']}"
        head += f" of {cap}" if cap else f" ({FAULT_LADDER_UNCAPPED})"
    waits = [format_duration(w) for w in (ladder.get("backoffsS") or [])
             if w is not None]
    if waits:
        head += f", waits so far {', '.join(waits)}"
    if ladder.get("nextAttemptAt"):
        head += f", next attempt at {ladder.get('nextAttemptAtLocal') or ladder['nextAttemptAt']}"
    return head


def format_fault_budget(budget: dict | None) -> str:
    """How much of the outage budget this episode has spent:
    `52s of 4h spent waiting (4h left)`, plus the run-wide infra-wait total
    when it is larger than this episode's (earlier outages this run rode out).

    `USAGE_NONE`'s discipline: a run with no recorded budget arithmetic gets
    `FAULT_LADDER_NONE`-style honesty rather than a `0s of 0s`.
    """
    if not isinstance(budget, dict):
        return USAGE_NONE
    total_wait = budget.get("totalWaitedS")
    if budget.get("budgetS") is None:
        # No episode budget recorded (nothing was ever waited out in one).
        if total_wait:
            return f"{format_duration(total_wait)} of infra waits in this run"
        return USAGE_NONE
    line = (f"{format_duration(budget.get('waitedS'))} of "
            f"{format_duration(budget.get('budgetS'))} spent waiting")
    if budget.get("remainingS") is not None:
        line += f" ({format_duration(budget['remainingS'])} left)"
    if total_wait and total_wait > (budget.get("waitedS") or 0) + 1:
        line += f"; {format_duration(total_wait)} of infra waits in this run"
    return line


def fault_explanation(run_root: Path) -> dict:
    """Why this run is (or last was) in trouble -- the whole story in one dict
    (task 025, #18.4).

    Joins the three records the engine already keeps:

      the last failing iteration   `_last_fault_iteration` -> `iteration`
                          (`iteration_detail`'s dict: phase, timing, exit
                          reason, the raw failure signals), from which
                          `faults.explain_fault` re-derives the CLASSIFIER's
                          own reasoning -- `reason` (which branch of the
                          ladder decided it) and `signature` (which row of
                          `faults.INFRA_SIGNATURES` matched, with the exact
                          substring). `faultClass` is always the verdict the
                          ENGINE recorded and acted on; when the re-derived
                          one differs, `FAULT_VERDICT_DIVERGED_NOTICE` says so
                          instead of either being quietly preferred.
      the retry attempts  `infra_retry` events of the current episode ->
                          `ladder` (attempt, cap, the backoffs actually
                          waited, the next attempt's instant),
      the outage budget   status.json's `infraWait`/`infraWaitTotalS` plus the
                          episode's last `infra_retry` -> `budget`.

    Also carries the run's `state`/`health`, `waiting` (is a backoff wait
    pending right now), `recovered` (the episode ended with an iteration
    reaching the model), and `abortReason` when the run gave up. `hasFault` is
    False only when there is genuinely nothing to explain -- then every other
    field is null and the renderers print `NO_FAULT`.

    Steering 004: the re-derivation deliberately passes NO abort input to
    `faults.explain_fault` -- whether an abort/interrupt was recorded is not
    part of an iteration's meta.json, and guessing it from status.json's
    `abortReason` would mislabel every run whose engine gave up *after* the
    fault (the usual infra-budget case) as an abort. So the abort branch's
    reasons never appear here; an iteration a signal ended is named as that
    (`FAULT_REASON_INTERRUPTED`), `FAULT_VERDICT_DIVERGED_NOTICE` says when the
    engine's own verdict differs, and the `gave up:` line quotes the run's
    recorded `abortReason` verbatim rather than attributing it to anyone.
    """
    root = Path(run_root)
    status = read_json(root / "status.json", {}) or {}
    if not isinstance(status, dict):
        status = {}
    events = read_events(root, FAULT_EVENT_TYPES)
    episode = _fault_episode(events)
    attempts = episode["attempts"]
    detail = _last_fault_iteration(root)

    wait = status.get("infraWait")
    wait = wait if isinstance(wait, dict) else None
    last_retry = attempts[-1] if attempts else None
    rate_limit_wait = status.get("rateLimitWait")
    rate_limit_wait = rate_limit_wait if isinstance(rate_limit_wait, dict) else None
    pause_detail = status.get("pauseDetail")
    pause_detail = pause_detail if isinstance(pause_detail, dict) else None

    exp: dict = {
        "hasFault": False,
        "state": status.get("state"),
        "health": status.get("health") or None,
        "waiting": wait is not None,
        "infraWait": wait,
        "recovered": bool(episode["recovered"]),
        "abortReason": status.get("abortReason") or None,
        "faultClass": None,
        "reason": None,
        "signature": None,
        "iteration": None,
        "phase": None,
        "error": None,
        "ladder": None,
        "budget": None,
        "notices": [],
        # v0.30 F.2: status.json's rate-limit wait in progress, or None.
        "rateLimitWait": rate_limit_wait,
        # v0.30 G.2: status.json's pause sub-reason (a provider refusal), or
        # None.
        "pauseDetail": pause_detail,
    }

    if detail is not None:
        recorded = detail.get("faultClass")
        usage = detail.get("usage") if isinstance(detail.get("usage"), dict) else None
        derived = explain_fault(
            error_text=str(detail.get("error") or ""),
            exit_code=(detail.get("exitCode")
                       if isinstance(detail.get("exitCode"), int)
                       and not isinstance(detail.get("exitCode"), bool) else None),
            interrupted=bool(detail.get("interrupted")),
            timed_out=bool(detail.get("timedOut")),
            no_traffic_timeout=bool(detail.get("noTrafficTimeout")),
            produced_traffic=billable_tokens(usage) > 0,
            **meta_provider_evidence(detail),
        )
        exp.update(hasFault=True,
                   faultClass=recorded,
                   reason=derived["reason"],
                   signature=derived["signature"],
                   iteration=detail.get("number"),
                   phase=detail.get("phase"),
                   error=str(detail.get("error") or "") or None,
                   iterationDetail=detail)
        notice = _recorded_fault_notice(detail, recorded, derived)
        if notice:
            exp["notices"].append(notice)
    elif last_retry is not None or wait is not None:
        # The retry wrapper is acting on a fault whose iteration meta this
        # reader cannot see (a mid-write meta.json, an iteration dir pruned by
        # hand): the episode is still the honest answer, without a verdict
        # invented for it.
        source = wait or last_retry or {}
        exp.update(hasFault=True, faultClass="infra",
                   reason=FAULT_REASON_FROM_EVENT,
                   phase=source.get("phase"),
                   error=str(source.get("error") or "") or None)
        exp["signature"] = matched_signature(exp["error"])

    if attempts or wait is not None:
        backoffs = [ev.get("backoffS") for ev in attempts
                    if isinstance(ev.get("backoffS"), (int, float))]
        attempt = None
        for candidate in ((wait or {}).get("attempt"),
                          (last_retry or {}).get("attempt")):
            if isinstance(candidate, int) and not isinstance(candidate, bool):
                attempt = candidate
                break
        cap = (last_retry or {}).get("maxAttempts")
        exp["ladder"] = {
            "attempt": attempt,
            "maxAttempts": cap if isinstance(cap, int) and not isinstance(cap, bool)
            else None,
            "attempts": len(attempts),
            "backoffsS": backoffs,
            "phase": (wait or last_retry or {}).get("phase"),
            "nextAttemptAt": (wait or {}).get("nextAttemptAt"),
        }
        if exp["ladder"]["nextAttemptAt"]:
            exp["ladder"]["nextAttemptAtLocal"] = format_local_time(
                exp["ladder"]["nextAttemptAt"])
        exp["ladder"]["display"] = format_fault_ladder(exp["ladder"])

    def number(value):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        return float(value)

    total_wait = number(status.get("infraWaitTotalS"))
    source = wait or last_retry or {}
    budget_s = number(source.get("budgetS"))
    waited_s = number(source.get("waitedS"))
    if budget_s is not None or total_wait:
        remaining = number((wait or {}).get("remainingS"))
        if remaining is None and budget_s is not None and waited_s is not None:
            remaining = max(0.0, budget_s - waited_s)
        exp["budget"] = {"waitedS": waited_s, "budgetS": budget_s,
                         "remainingS": remaining, "totalWaitedS": total_wait}
        exp["budget"]["display"] = format_fault_budget(exp["budget"])
        exp["hasFault"] = exp["hasFault"] or bool(attempts) or wait is not None

    if rate_limit_wait is not None:
        # `waiting` stays the infra backoff's flag; the rate-limit wait has
        # its own line (`format_rate_limit_wait_line`).
        exp["hasFault"] = True
        if exp["faultClass"] is None:
            exp["faultClass"] = FAULT_CLASS_RATE_LIMIT
            exp["reason"] = FAULT_REASON_RATE_LIMIT
    if (pause_detail is not None
            and pause_detail.get("reason") == RATE_LIMIT_CEILING_REASON):
        # Parked on the rate-limit ceiling (v0.30 F.3): the pause is the
        # explanation when no rate-limited iteration says more.
        exp["hasFault"] = True
        if exp["faultClass"] is None:
            exp["faultClass"] = FAULT_CLASS_RATE_LIMIT
            exp["reason"] = FAULT_REASON_RATE_LIMIT
            exp["phase"] = pause_detail.get("phase")
    if pause_detail is not None and pause_detail.get("reason") == "provider_refused":
        # The run is parked on a refusal; when the refused iteration's
        # meta.json cannot be read, the pause itself is the explanation.
        exp["hasFault"] = True
        if exp["faultClass"] is None:
            exp["faultClass"] = FAULT_CLASS_REFUSED
            exp["reason"] = FAULT_REASON_REFUSED
            exp["phase"] = pause_detail.get("phase")
            exp["error"] = pause_detail.get("message") or None
            exp["signature"] = matched_refusal(
                exp["error"], pause_detail.get("httpStatus"))
    exp["signatureDisplay"] = format_fault_signature(exp["signature"])
    exp["summaryLines"] = fault_summary_lines(exp)
    return exp


def fault_summary_lines(exp: dict) -> list[str]:
    """One run's fault explanation as labelled text lines, worded ONCE
    (`iteration_summary_lines`' role for #18.4): `ralphctl fault` prints these
    and the hub's fault dialog (task 026) shows the very same block.

    A line is omitted rather than printed empty when the fact behind it was
    never recorded -- and a run with nothing to explain gets the single
    `NO_FAULT` line, not a column of `None`s.
    """
    if not isinstance(exp, dict) or not exp.get("hasFault"):
        return [NO_FAULT]
    head = f"fault:     {exp.get('faultClass') or EXIT_REASON_UNKNOWN}"
    if exp.get("iteration") is not None:
        head += f" (iteration {exp['iteration']}"
        head += f", phase {exp['phase']})" if exp.get("phase") else ")"
    elif exp.get("phase"):
        head += f" (phase {exp['phase']})"
    lines = [head, *(exp.get("notices") or [])]
    if exp.get("reason"):
        lines.append(f"because:   {exp['reason']}")
    lines.append(f"signature: {exp.get('signatureDisplay') or FAULT_SIGNATURE_NONE}")
    error = _one_line(str(exp.get("error") or "").strip(), EXIT_REASON_ERROR_MAX)
    detail = exp.get("iterationDetail")
    exit_reason = (detail.get("exitReason") if isinstance(detail, dict) else None)
    # The ranked verdict (`error (exit 0): … [infra fault]`) already quotes the
    # error text it was derived from -- printing both would say the same
    # sentence twice. The error gets its own line only when the verdict does
    # not carry it (a watchdog kill, an event-sourced explanation with no
    # readable meta.json).
    if error and error not in (exit_reason or ""):
        lines.append(f"error:     {error}")
    if exit_reason:
        lines.append(f"exit:      {exit_reason}")
    ladder = exp.get("ladder")
    if isinstance(ladder, dict):
        lines.append(f"ladder:    {ladder.get('display') or FAULT_LADDER_NONE}")
    budget = exp.get("budget")
    if isinstance(budget, dict):
        lines.append(f"budget:    {budget.get('display') or USAGE_NONE}")
    waiting_line = format_rate_limit_wait_line(exp.get("rateLimitWait"))
    if waiting_line:
        lines.append(waiting_line)
    pause_line = format_pause_detail_line(exp.get("pauseDetail"))
    if pause_line:
        lines.append(pause_line)
    health = exp.get("health")
    if health:
        state = f"health:    {health}"
        if exp.get("waiting"):
            state += " (sitting out a backoff wait right now)"
        elif health == "degraded":
            state += " (a retry attempt is running, no backoff wait pending)"
        lines.append(state)
    if exp.get("recovered") and not exp.get("waiting"):
        lines.append(f"recovered: {FAULT_RECOVERED_NOTICE}")
    if exp.get("abortReason"):
        lines.append(f"gave up:   {_one_line(exp['abortReason'], EXIT_REASON_ERROR_MAX)}")
    return lines


def fault_text(exp: dict) -> str:
    """The complete rendering of one fault explanation as a single string --
    what the hub's dialog shows and what `ralphctl fault --json` carries as
    `text` (`artifact_text`/`run_document_text`'s role for #18.4)."""
    return "\n".join(fault_summary_lines(exp))

# -- cost breakdown (task 027, #18.5) ---------------------------------------
# status.json's `usage` already carries `byPhase`/`byApproach` buckets
# (`loop._accumulate_usage`), but the only surfaces that ever read them were
# `ralphctl status`' one-line summary (planning/worker/review only, phases the
# engine no longer even uses) and the hub's usage card. So "which phase spent
# this, and how much of that number is actually known" meant reading raw JSON.
# The shaping and wording live here so `ralphctl cost` and the hub's
# cost-breakdown dialog (task 028) say the same thing in the same words --
# `iteration_detail`/`fault_explanation`' discipline.
COST_TOTAL_KEY = "total"
# Said out loud for a run whose status.json records no usage at all: a column
# of `$0.00`s would look like a run that cost nothing, rather than one that
# never reported anything.
COST_NO_USAGE = "(no usage recorded)"
# The per-bucket verdict words. `format_cost` already SPELLS derived/partial/
# unavailable inside its own string; these are the machine-readable summary
# (`costSource`) plus the two cases a money string cannot express on its own.
COST_SOURCE_PROVIDER = "provider-priced"
COST_SOURCE_DERIVED = COST_DERIVED_WORD
COST_SOURCE_PARTIAL = "partial"
COST_SOURCE_UNAVAILABLE = COST_UNAVAILABLE
COST_SOURCE_FREE = "declared free"
COST_SOURCE_SUBSCRIPTION = "subscription"
COST_SOURCE_NO_TRAFFIC = "no traffic"
# Printed once, and only when one of those qualifiers actually appears, so a
# fully priced run's breakdown is not padded with an explanation of vocabulary
# it never uses.
COST_BREAKDOWN_LEGEND = (
    "legend:    a bare amount was quoted by the provider; ~ marks money "
    f"derived from the host-side rate table; {COST_UNAVAILABLE} means tokens "
    "were billed that nothing priced")
COST_LEGEND_SOURCES = (COST_SOURCE_DERIVED, COST_SOURCE_PARTIAL,
                       COST_SOURCE_UNAVAILABLE)
# Keys `cost_bucket` derives itself, stripped from the raw passthrough first so
# a hand-edited status.json cannot smuggle in a display string its own numbers
# do not support (`ITERATION_DERIVED_KEYS`' rule, same reason).
COST_BUCKET_DERIVED_KEYS = ("key", "tokens", "tokensDisplay", "tokensTotalDisplay",
                            "costDisplay", "costSource", "listEquivalentDisplay",
                            "byPhase", "byApproach", "byTier")
# `costBasis` values a usage dict or bucket carries. A claude-code iteration
# bills against a subscription: `costUSD` is exactly 0 and the SDK's list-price
# figure rides apart as `listEquivalentUSD`, never added to the cost. A bucket
# whose contributors are not all subscription is "mixed".
COST_BASIS_SUBSCRIPTION = "subscription"
COST_BASIS_MIXED = "mixed"


def _token_total(usage: dict | None) -> int | None:
    """How many tokens a bucket counted, or None when it counted nothing.

    `totalTokens` when the provider reported one; otherwise the sum of the
    counters it DID report (`billable_tokens`) -- a provider reporting no total
    is not the same as no tokens. NB `billable_tokens` itself includes
    `totalTokens` in its sum (it answers "was anything billable at all"), so it
    is only ever consulted here when there is no total to double-count.
    """
    if not isinstance(usage, dict):
        return None
    raw = usage.get("totalTokens")
    if raw is not None and not isinstance(raw, bool):
        try:
            return int(raw)
        except (TypeError, ValueError):
            pass
    return billable_tokens(usage) or None


def format_token_total(usage: dict | None) -> str:
    """The one-number token column of the cost breakdown: `505,628 tokens`,
    or `''` when the bucket counted nothing at all.

    `format_tokens` renders the same bucket in full (`… total (in 32, out …)`)
    which is what the header line and `--json` carry; a table needs one number
    per row.
    """
    total = _token_total(usage)
    return "" if total is None else f"{total:,} tokens"


def cost_source(usage: dict | None) -> str | None:
    """Which kind of money a bucket holds, in one word -- the machine-readable
    twin of what `format_cost`' string says, or None when the bucket says
    nothing at all.

    `cost_status` (the shared classifier) answers three of the cases straight
    off; the two it renders as None are told apart here, because "the provider
    priced this" and "nothing was ever billed" are very different facts that
    both come out of `format_cost` as a bare amount:

      `"unknown"`  -> `unavailable`      (tokens billed, nothing priced them)
      `"partial"`  -> `partial`          (a priced subtotal, not the total)
      `"derived"`  -> `derived`          (host-side rate table, not a quote)
      no tokens    -> `no traffic`       (the int-0 sentinel of #10)
      `costFree`   -> `declared free`    (a route that DECLARED itself free)
      otherwise    -> `provider-priced`  (a real quote), or None with no cost
    """
    if not isinstance(usage, dict) or not usage:
        return None
    status = cost_status(usage)
    if status == "unknown":
        return COST_SOURCE_UNAVAILABLE
    if status == "partial":
        return COST_SOURCE_PARTIAL
    if status == "derived":
        return COST_SOURCE_DERIVED
    if billable_tokens(usage) == 0:
        return COST_SOURCE_NO_TRAFFIC
    if usage.get("costBasis") == COST_BASIS_SUBSCRIPTION:
        return COST_SOURCE_SUBSCRIPTION
    if usage.get("costFree") is True:
        return COST_SOURCE_FREE
    if usage.get("costUSD") is not None:
        return COST_SOURCE_PROVIDER
    return None


def cost_bucket(usage: dict | None, key: str = COST_TOTAL_KEY) -> dict:
    """One usage bucket (the total, a phase or an approach) shaped for display:
    its own numbers passed through verbatim plus the rendered strings, by the
    same formatters every other cost surface uses (`format_cost`,
    `format_tokens`)."""
    raw = usage if isinstance(usage, dict) else {}
    out = {k: v for k, v in raw.items() if k not in COST_BUCKET_DERIVED_KEYS}
    out["key"] = key
    out["tokens"] = _token_total(raw)
    out["tokensDisplay"] = format_tokens(raw) if raw else USAGE_NONE
    out["tokensTotalDisplay"] = format_token_total(raw)
    out["costDisplay"] = format_cost(raw, decimals=4)
    out["costSource"] = cost_source(raw)
    listed = raw.get("listEquivalentUSD")
    if isinstance(listed, (int, float)) and not isinstance(listed, bool):
        out["listEquivalentDisplay"] = format_money(listed, 4)
    return out


def _cost_buckets(raw, numeric_keys: bool = False, tier_keys: bool = False) -> list[dict]:
    """`byPhase`/`byApproach`/`byTier` as an ordered list of shaped buckets.
    Phase order is the engine's own insertion order; approaches are sorted
    numerically (`"10"` after `"2"`); tiers sort by the declared ladder order
    (`tiers.TIERS`: trivial < normal < hard), falling back to string order for
    an unrecognised key (task 017, #68 half 2)."""
    if not isinstance(raw, dict):
        return []
    keys = list(raw)
    if numeric_keys:
        def sort_key(k):
            try:
                return (0, int(k), "")
            except (TypeError, ValueError):
                return (1, 0, str(k))
        keys.sort(key=sort_key)
    elif tier_keys:
        def sort_key(k):
            return (0, tiers.tier_rank(k)) if tiers.is_tier(k) else (1, 0)
        keys.sort(key=sort_key)
    return [cost_bucket(raw.get(k), str(k)) for k in keys]


def price_reported(d: dict) -> bool:
    """True when `d` (an iteration usage dict or a bucket) holds at least one
    provider-reported price: a float `costUSD`. A subscription iteration's
    `0.0` is not a price (nothing was quoted, nothing is billed per token),
    so it never turns an unknown bucket into a partial one; a mixed or
    subscription bucket counts only when it holds real money."""
    amount = d.get("costUSD")
    if not isinstance(amount, float):
        return False
    if d.get("costBasis") in (COST_BASIS_SUBSCRIPTION, COST_BASIS_MIXED):
        return amount > 0
    return True


def merge_cost_flags(bucket: dict, usage: dict) -> None:
    """Fold one iteration's `costFree` and `costBasis` into `bucket`, called
    BEFORE the counters are added so the bucket still describes what came
    earlier. Shared by `LoopSupervisor._merge_usage` and
    `_accumulate_task_usage` so a task rollup never disagrees with the
    status.json rollup.

    `costFree` holds only while every contributor was free: a bucket that
    mixes priced money (or billed-but-unpriced tokens) with free iterations
    is priced. `costBasis` is "subscription" while every contributor was
    subscription and "mixed" as soon as one billed anything else; a
    contributor that billed nothing is neutral."""
    sub = usage.get("costBasis") == COST_BASIS_SUBSCRIPTION
    free = usage.get("costFree") is True
    prev_basis = bucket.get("costBasis")
    prev_free = bucket.get("costFree") is True

    def _spent(d: dict) -> bool:
        amount = d.get("costUSD")
        return (isinstance(amount, (int, float)) and not isinstance(amount, bool)
                and amount > 0)

    paid_now = not free and (_spent(usage) or usage.get("costPriced") is False)
    paid_prev = _spent(bucket) or bucket.get("costStatus") in ("partial", "unknown")
    if (prev_free or free) and not (paid_now or paid_prev):
        bucket["costFree"] = True
    else:
        bucket.pop("costFree", None)

    other_now = not sub and (billable_tokens(usage) > 0 or _spent(usage))
    prior_other = (prev_basis not in (None, COST_BASIS_SUBSCRIPTION)
                   or (prev_basis is None
                       and (billable_tokens(bucket) > 0 or _spent(bucket))))
    if sub:
        basis = COST_BASIS_MIXED if prior_other else COST_BASIS_SUBSCRIPTION
    elif other_now:
        basis = (COST_BASIS_MIXED if prev_basis == COST_BASIS_SUBSCRIPTION
                 else prev_basis)
    else:
        basis = prev_basis
    if basis:
        bucket["costBasis"] = basis


def _accumulate_task_usage(bucket: dict, usage: dict) -> dict:
    """Add one iteration's `usage` dict into `bucket` (a per-task rollup) in
    place, returning it -- the SAME algorithm `LoopSupervisor._merge_usage`/
    `_merge_cost_status` (`engine/loop.py`) use to roll a per-iteration usage
    dict into status.json's byPhase/byApproach/byTier buckets (tasks 017/049/
    050/052), duplicated here rather than imported: `engine.loop` imports
    `engine.state`, so the dependency cannot run the other way. Kept
    algorithmically identical -- same cost-status monotonicity, same
    `costFree` carry, same non-numeric-marker skip -- so a task's aggregated
    cost (task 036, G.4) can never disagree with what the SAME iterations
    would produce inside status.json's own rollup.
    """
    prev = bucket.get("costStatus")
    unpriced_gap = ((usage.get("costPriced") is False or is_zero_quote(usage))
                    and usage.get("costDerived") is not True)
    has_unknown = prev in ("partial", "unknown") or unpriced_gap
    has_derived = (prev == "derived"
                   or isinstance(bucket.get("costDerivedUSD"), float)
                   or isinstance(usage.get("costDerivedUSD"), float))
    if not has_unknown:
        status = "derived" if has_derived else None
    else:
        has_price = (prev == "partial" or has_derived
                     or price_reported(bucket) or price_reported(usage))
        status = "partial" if has_price else "unknown"
    merge_cost_flags(bucket, usage)
    for k, v in usage.items():
        if isinstance(v, (bool, str)):
            continue
        bucket[k] = (round(bucket.get(k, 0) + v, 6) if isinstance(v, float)
                     else bucket.get(k, 0) + v)
    if status:
        bucket["costStatus"] = status
    return bucket


def task_totals(iterations: list) -> dict:
    """Per-task aggregated time/cost for the hub tasks table (task 036,
    G.4; re-keyed task 001/PRD I.1, #29's defect recurring in this new
    place): `{"<approach>:<taskId>": {"durationS": float, "durationDisplay":
    str, "costDisplay": str, "iterationCount": int, "iterations": [...]}}`,
    summed from every entry in `iterations` (`iteration_detail`'s own shape
    -- this run's `run_detail` iteration list) whose `assignedTaskId` names
    that task, INCLUDING a validation-failed re-entry that dispatched a
    second iteration against the same task id -- a task's total reflects
    every attempt at it, never only its last one.

    Keys are approach-namespaced -- `"<approach>:<taskId>"` -- following the
    #29 fix site's own stated rule (`RunDir.migrate_legacy_vigilant_verified`
    above): every approach's planning pass renumbers `tasks.json` from
    "001", so a flat `taskId`-only key would let approach 2's task "001"
    inherit approach 1's minutes and dollars, exactly the bug this task
    fixes (#207 -- "$52.50 reported where $8.31 belonged"). The approach
    namespace comes from EACH iteration's own `approach` field (the
    dispatch-time write, `engine/loop.py`'s `assignedTaskId` sibling) --
    never a single approach passed in here -- so one call still separates
    every approach's iterations correctly even though the caller
    (`_with_task_totals`) only ever reads one approach's slice back out. An
    iteration recorded before that field existed (predates the dispatch
    write, task 027) falls back to approach 1, the same convention the #29
    fix site uses for its own pre-fix sidecar entries.

    Reads `durationS`/`usage` off `iteration_detail`'s already-derived
    fields -- never re-reads meta.json here -- the same "shape once, render
    everywhere" discipline `iteration_detail`'s own `costDisplay` follows. A
    task id no iteration ever carried gets no entry at all, so a caller can
    tell "never attempted" apart from "attempted for $0.00"; `task_totals_zero`
    below is the bucket a caller renders for a task THIS approach never
    dispatched once the run records attribution at all.

    `iterations` (task 037, G.5): the SAME breakdown the total was computed
    from, one entry per iteration that named this task -- `{"number":
    it["number"], "tier": it["effectiveTier"], "costDisplay":
    it["costDisplay"]}` -- in the order they appear in the caller's own
    `iterations` list (this run's chronological order), so a task detail
    view can list exactly the iterations the total above was summed from,
    never a re-derived or re-ordered set.
    """
    raw: dict[str, dict] = {}
    for it in iterations:
        if not isinstance(it, dict):
            continue
        tid = it.get("assignedTaskId")
        if tid is None:
            continue
        approach = it.get("approach")
        approach = approach if isinstance(approach, int) and not isinstance(approach, bool) else 1
        key = f"{approach}:{tid}"
        bucket = raw.setdefault(key, {"durationS": 0.0, "usage": {},
                                      "iterationCount": 0, "iterations": []})
        dur = it.get("durationS")
        if isinstance(dur, (int, float)) and not isinstance(dur, bool):
            bucket["durationS"] += dur
        usage = it.get("usage")
        if isinstance(usage, dict):
            _accumulate_task_usage(bucket["usage"], usage)
        bucket["iterationCount"] += 1
        bucket["iterations"].append({
            "number": it.get("number"),
            "tier": it.get("effectiveTier"),
            "costDisplay": it.get("costDisplay") or USAGE_NONE,
        })
    out = {}
    for tid, bucket in raw.items():
        out[tid] = {
            "durationS": round(bucket["durationS"], 3),
            "durationDisplay": format_duration(bucket["durationS"]),
            "costDisplay": format_cost(bucket["usage"], decimals=4) or USAGE_NONE,
            "iterationCount": bucket["iterationCount"],
            "iterations": bucket["iterations"],
        }
    return out


def task_totals_records_attribution(iterations: list) -> bool:
    """Whether THIS run's iteration list is capable of answering "how much
    did this task cost" at all -- cure-01-06 (PRD I.1, DoD 10): the fix for
    the defect `task_totals`'s own absent-entry rule (below) could not tell
    apart on its own.

    `task_totals` returns `{}` in TWO different situations that must render
    differently in the hub: a legacy run dir whose iterations never carry
    an `assignedTaskId` key at all (task 038/G.6's em-dash case -- nothing
    about this run's dispatch attribution is known), and a brand-new
    v0.24+ run that DOES record attribution on every dispatch (task 027)
    but has not yet dispatched any iteration against ANY task -- a
    planning-only iteration still carries `"assignedTaskId": null`, a
    written, present key, never an absent one. An empty `task_totals()`
    result cannot distinguish those two on its own, because a bucket is
    only ever built from a NON-null `assignedTaskId` (a real dispatch); the
    key's mere PRESENCE (whatever its value) is the signal this function
    reads instead.

    True the moment any iteration in the list is a dict carrying the
    `assignedTaskId` key at all (including a `None` value, exactly the
    planning/reflect/run-review dispatch write leaves) -- one such
    iteration is enough to know every OTHER iteration in this same run dir
    would carry the key too, since the write is unconditional from task 027
    onward. False for an empty iteration list, or one where not a single
    entry carries the key (the pre-task-027 case).
    """
    return any(isinstance(it, dict) and "assignedTaskId" in it
               for it in iterations)


def task_totals_zero() -> dict:
    """The MEASURED-ZERO bucket (task 001, PRD I.1 definition-of-done 10:
    "a `pending` task in the current approach shows zero") -- `task_totals`'s
    own bucket shape with nothing in it, formatted by the same two formatters
    a real bucket goes through (`format_duration`/`format_cost`), so a task
    the current approach never dispatched renders `"0s"`/`"$0.0000"` rather
    than a hand-written literal here that could drift from the real
    rendering.

    Spelled next to `task_totals` rather than in its caller: the rule that
    zero minutes and zero dollars are FORMATTED the same way one minute and
    one cent are belongs to the module that owns the rollup.

    This is NOT the same statement as `task_totals`'s absent entry. An absent
    entry says "this run records no dispatch attribution at all" (a pre-task-
    027 run dir, whose iterations carry no `assignedTaskId` -- task 038/G.6),
    which is a genuine "nothing to say", and the hub still renders its em
    dash for that. A zero bucket says "this run DOES record attribution, and
    this approach dispatched nothing against this task", which is a number,
    not a gap.
    """
    return {
        "durationS": 0.0,
        "durationDisplay": format_duration(0.0),
        "costDisplay": format_cost({"costUSD": 0.0}, decimals=4) or USAGE_NONE,
        "iterationCount": 0,
        "iterations": [],
    }


# Task 039 (PRD I.1): cause/actor labels for exactly the extra fields a
# "task" event is KNOWN to carry today (`LoopSupervisor._emit_task_status`'s
# own call sites, `engine/loop.py`) -- spelled once here so a task detail
# view's label can never disagree with which field on events.jsonl it read.
# Not exhaustive of every future extra a "task" event might carry: a
# transition whose event has none of these three keys gets no cause at all,
# which is exactly what I.1 means by "where recorded" -- never an invented
# label for a transition nothing on disk explains.
def _task_transition_cause(event: dict) -> str | None:
    reason = event.get("unsatisfiableReason")
    if isinstance(reason, str) and reason:
        return f"worker: unsatisfiable ({reason})"
    deferred_reason = event.get("deferredReason")
    if isinstance(deferred_reason, str) and deferred_reason:
        until = event.get("deferredUntil")
        return f"worker: deferred until {until} ({deferred_reason})"
    children = event.get("splitChildren")
    if isinstance(children, list) and children:
        return "harness: forced split into " + ", ".join(str(c) for c in children)
    return None


def task_transition_history(run_root: Path, task_id: str) -> list[dict]:
    """Task 039 (PRD I.1): one entry per `"task"` event on `events.jsonl`
    (`LoopSupervisor._emit_task_status`/`_emit_task_changes`, the ONLY
    writers of that event type) whose own `taskId` names `task_id`, oldest
    first -- `{"ts", "tsLocal", "oldStatus", "newStatus"}` plus an optional
    `cause` (`_task_transition_cause` above), so a task detail view can show
    every state the task passed through, not only the single current status
    `tasks.json` itself records.

    `oldStatus` is `None` for a task's very first recorded transition (the
    run's first `_emit_task_changes()` diff, compared against an empty
    in-memory snapshot) -- the caller renders that, this function does not
    paper over it with an invented starting label.

    Purely on-disk, like `read_events` itself (which this is a thin filter
    over): a dead run with no container still answers from its own
    `events.jsonl`.
    """
    out: list[dict] = []
    for ev in read_events(run_root, types=("task", "task.undeferred")):
        if str(ev.get("taskId")) != str(task_id):
            continue
        if ev.get("type") == "task.undeferred":
            # #292: an operator cut. Its own entry and cause wording, never
            # the deferral-expiry shape (an expiry records no `task` event).
            until, reason = ev.get("until"), ev.get("reason")
            cause = "operator: undeferred"
            if until:
                cause += f" (was deferred until {until}"
                cause += f": {reason})" if reason else ")"
            out.append({
                "ts": ev.get("ts"),
                "tsLocal": format_local_time(ev.get("ts")),
                "oldStatus": "pending", "newStatus": "pending",
                "kind": "undeferred", "by": ev.get("by") or "operator",
                "cause": cause,
            })
            continue
        entry = {
            "ts": ev.get("ts"),
            "tsLocal": format_local_time(ev.get("ts")),
            "oldStatus": ev.get("oldStatus"),
            "newStatus": ev.get("newStatus"),
        }
        cause = _task_transition_cause(ev)
        if cause:
            entry["cause"] = cause
        out.append(entry)
    return out


def cost_breakdown(run_root: Path) -> dict:
    """Everything status.json knows about what this run spent, per phase and
    per approach (task 027, #18.5).

    Purely on-disk, like `iteration_detail`/`fault_explanation`: status.json is
    the engine's own atomic write, so a live run and one whose container is
    long gone read identically and there is nothing to fall back from.

    The headline stays `total["costDisplay"]` -- the same `format_cost` string
    `ralphctl status` and the hub's usage card show -- so a breakdown can never
    disagree with the number next to it.
    """
    status = read_json(run_root / "status.json", {})
    if not isinstance(status, dict):
        status = {}
    usage = status.get("usage")
    if not isinstance(usage, dict):
        usage = {}
    total = cost_bucket(usage)
    by_phase = _cost_buckets(usage.get("byPhase"))
    by_approach = _cost_buckets(usage.get("byApproach"), numeric_keys=True)
    by_tier = _cost_buckets(usage.get("byTier"), tier_keys=True)
    buckets = [total, *by_phase, *by_approach, *by_tier]
    notices = []
    # Task 049's anomaly is worded ONCE (COST_ZERO_QUOTE_NOTICE) and named here
    # whenever any bucket carries it -- a breakdown full of `unavailable` rows
    # otherwise leaves the operator guessing why a route that answered every
    # request priced none of it.
    if any(is_zero_quote(b) for b in [usage, *_raw_buckets(usage)]):
        notices.append(COST_ZERO_QUOTE_NOTICE)
    out = {
        "hasUsage": bool(total["tokens"] or total["costDisplay"]
                         or by_phase or by_approach or by_tier),
        "total": total,
        "byPhase": by_phase,
        "byApproach": by_approach,
        "byTier": by_tier,
        "notices": notices,
        "costDisplay": total["costDisplay"],
        "costStatus": cost_status(usage),
        "costSource": total["costSource"],
        "model": status.get("model"),
        "modelRaw": status.get("modelRaw"),
        "sources": sorted({b["costSource"] for b in buckets if b["costSource"]}),
    }
    if isinstance(usage.get("listEquivalentUSD"), (int, float)):
        out["listEquivalentUSD"] = usage["listEquivalentUSD"]
    out["summaryLines"] = cost_breakdown_lines(out)
    return out


def _raw_buckets(usage: dict) -> list[dict]:
    out = []
    for key in ("byPhase", "byApproach", "byTier"):
        raw = usage.get(key)
        if isinstance(raw, dict):
            out.extend(v for v in raw.values() if isinstance(v, dict))
    return out


def _cost_rows(buckets: list[dict]) -> list[str]:
    """The indented table body of one group. Columns are sized to their own
    content (a phase name is 6-8 characters, an approach key is 1-2), and a
    bucket that recorded nothing renders `USAGE_NONE` rather than a zero."""
    key_w = max(len(b["key"]) for b in buckets)
    cells = [(b["key"], b["costDisplay"] or USAGE_NONE, b["tokensTotalDisplay"],
              b.get("listEquivalentDisplay") if b.get("listEquivalentUSD") else None)
             for b in buckets]
    cost_w = max(len(c[1]) for c in cells)
    tok_w = max(len(c[2]) for c in cells)
    # The list-equivalent column appears only when some row in the group has
    # one, so a pure pi run's table is unchanged.
    show_list = any(c[3] for c in cells)
    rows = []
    for k, cost, tokens, listed in cells:
        row = f"  {k:<{key_w}}  {cost:<{cost_w}}  {tokens:>{tok_w}}"
        if show_list:
            row += f"  list-equiv: {listed}" if listed else ""
        rows.append(row.rstrip())
    return rows


def cost_breakdown_lines(bd: dict) -> list[str]:
    """One run's cost breakdown as labelled text lines, worded ONCE
    (`fault_summary_lines`' role for #18.5): `ralphctl cost` prints these and
    task 028's hub dialog shows the very same block.

    A group is omitted rather than printed empty, and a run with no usage at
    all gets the single `COST_NO_USAGE` line instead of a table of zeros.
    """
    if not isinstance(bd, dict) or not bd.get("hasUsage"):
        return [COST_NO_USAGE]
    total = bd.get("total") or {}
    lines = [f"cost:      {total.get('costDisplay') or USAGE_NONE}"]
    if total.get("listEquivalentDisplay") and total.get("listEquivalentUSD"):
        lines.append(f"list-equiv: {total['listEquivalentDisplay']}"
                     "  (subscription list price, not billed)")
    lines.append(f"tokens:    {total.get('tokensDisplay') or USAGE_NONE}")
    # The verdict word is added only when it is NOT already spelled inside the
    # money string: `format_cost` says `derived`/`partial`/`unavailable` itself,
    # and repeating it would say the same thing twice -- but a bare `$0.12` (or
    # a `$0.0000` that is honest because the route declared itself free, or
    # because nothing was billed at all) says nothing about where it came from.
    source = total.get("costSource")
    if source and source not in (total.get("costDisplay") or ""):
        lines.append(f"source:    {source}")
    if bd.get("model"):
        model = f"model:     {bd['model']}"
        if bd.get("modelRaw") and bd["modelRaw"] != bd["model"]:
            model += f"  (gateway id: {bd['modelRaw']})"
        lines.append(model)
    lines.extend(f"!! {n}" for n in (bd.get("notices") or []))
    for label, key in (("by phase:", "byPhase"), ("by approach:", "byApproach"),
                       ("by tier:", "byTier")):
        buckets = bd.get(key) or []
        if buckets:
            lines.append(label)
            lines.extend(_cost_rows(buckets))
    if any(s in COST_LEGEND_SOURCES for s in (bd.get("sources") or [])):
        lines.append(COST_BREAKDOWN_LEGEND)
    return lines


def cost_breakdown_text(bd: dict) -> str:
    """The complete rendering of one cost breakdown as a single string -- what
    the hub's dialog shows and what `ralphctl cost --json` carries as `text`
    (`fault_text`/`artifact_text`' role for #18.5)."""
    return "\n".join(cost_breakdown_lines(bd))

@dataclass
class RunDir:
    """Layout of /run and helpers over it. Engine-owned."""

    root: Path
    _events_lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _event_id: int = 0
    # v0.25 task 028a (PRD requirement K.1/K.2, #267): an `asyncio.Event`
    # the loop registers on itself via `set_wake_event` -- `None` until
    # then, so every non-loop caller of this same class (the CLI, the API
    # handler process before the loop attaches, a bare `RunDir(root=...)`
    # in a test that never runs the dispatch loop) keeps working exactly
    # as before. Untyped as `asyncio.Event` here on purpose: this module
    # does not otherwise import `asyncio`, and a bare attribute is enough
    # for `add_steering` below to ring the bell when one is registered.
    _wake_event: object | None = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        for sub in ("steering", "iterations", "approaches", "artifacts"):
            (self.root / sub).mkdir(parents=True, exist_ok=True)
        last = 0
        try:
            for line in (self.root / "events.jsonl").read_text().splitlines():
                last = max(last, json.loads(line).get("id", 0))
        except FileNotFoundError:
            pass
        self._event_id = last

    # -- well-known files ------------------------------------------------
    @property
    def status_file(self) -> Path:
        return self.root / "status.json"

    @property
    def tasks_file(self) -> Path:
        """The agent-readable VIEW copy (task 004, requirement B.3) --
        what every reader that is not the engine itself uses: the worker/
        steer prompts, `ralphctl tasks`, the hub's task table, the steer
        hat's `context-read` tool. Kept in sync with the authoritative
        record on every `write_tasks()` call, but never itself the source
        `read_tasks`/`read_tasks_result` reads -- see
        `protected_tasks_file` for that."""
        return self.root / "tasks.json"

    @property
    def protected_tasks_dir(self) -> Path:
        """Task 004 (requirement B, #202): the directory holding the
        authoritative task record -- root-owned 0700 under the uid
        boundary, re-asserted on every write (see
        `privsep.write_protected_tasks_json`), and, unlike this task's
        first attempt, one whose every ANCESTOR entry is root-owned and
        agent-unwritable too (B.1). Resolved by
        `privsep.protected_tasks_dir()`: the bind-mounted
        `/var/lib/ralphd/record` (parented by the root-owned
        `/var/lib/ralphd`, backed by a host directory under the run's own
        config dir so it survives a `resume`'s fresh container), or -- with
        no such mount, the documented degrade -- the pre-mount run-dir
        location `privsep.legacy_protected_tasks_dir()` names."""
        return privsep.protected_tasks_dir(self.root)

    @property
    def protected_tasks_file(self) -> Path:
        """The authoritative tasks.json (task 004, requirement B) --
        engine reads/writes ONLY through this path; every other reader
        uses `tasks_file`, the agent-readable view."""
        return self.protected_tasks_dir / "tasks.json"

    @property
    def prd_file(self) -> Path:
        return self.root / PRD_FILE

    @property
    def composite_prd_file(self) -> Path:
        return self.root / COMPOSITE_PRD_FILE

    @property
    def notes_file(self) -> Path:
        return self.root / NOTES_FILE

    @property
    def standing_rules_file(self) -> Path:
        return self.root / STANDING_RULES_FILE

    def read_standing_rules(self) -> str:
        """The harness's own pinned copy of the notes' `## Standing rules`
        block (task 107), or `""` when nothing has pinned one yet (an
        older run, or a planning proposal that carried no `standing_rules`
        field at all -- reinjection is a no-op in that case, see
        `LoopSupervisor._reinject_standing_rules`)."""
        try:
            return self.standing_rules_file.read_text()
        except FileNotFoundError:
            return ""

    def write_standing_rules(self, text: str) -> None:
        """Pin `text` (the planner's `standing_rules` contract field,
        already validated as a non-empty string by
        `transitions.HAT_SCHEMAS["planning"]`) as this run's harness-owned
        copy. Called exactly once per planning/replan that supplied one --
        every later iteration's `_reinject_standing_rules` reads it back
        unchanged, so this is the one place it can ever be updated."""
        atomic_write(self.standing_rules_file, text)

    @property
    def findings_file(self) -> Path:
        return self.root / FINDINGS_FILE

    @property
    def findings_json_file(self) -> Path:
        return self.root / FINDINGS_JSON_FILE

    @property
    def review_early_verdict_file(self) -> Path:
        """Task 025 (PRD requirement H.1/H.4, #264): review's early-verdict
        checkpoint -- see `REVIEW_EARLY_VERDICT_FILE`'s own module-level
        comment for what it holds and why it is a separate file from
        `findings_file`/`findings_json_file` above."""
        return self.root / REVIEW_EARLY_VERDICT_FILE

    @property
    def debt_file(self) -> Path:
        """Task 006 (requirement A.3, #155): the debt ledger's human render --
        never written directly, only through `record_debt`/`state.
        record_debt_entries` below."""
        return self.root / DEBT_FILE

    @property
    def debt_json_file(self) -> Path:
        """The machine-readable list `debt_file` renders -- same pairing as
        `findings_file`/`findings_json_file` above."""
        return self.root / DEBT_JSON_FILE

    def record_debt(self, task_id: str, notes: list[dict]) -> list[dict]:
        """Bound form of `record_debt_entries` -- what `_verify_task` calls
        after copying a verifier's `notes` onto `verifyNotes` (task 005),
        so no caller has to know where the ledger lives."""
        return record_debt_entries(self.root, task_id, notes)

    def read_debt(self) -> list[dict]:
        """Bound form of `read_debt_entries` -- what the worker/review
        prompt context (task 007, `LoopSupervisor._debt_ledger_context`)
        and `_apply_worker_debt_resolution` read, so neither has to know
        the ledger's on-disk path."""
        return read_debt_entries(self.root)

    def resolve_debt(self, ids: list[str], resolved_by: str) -> list[str]:
        """Bound form of `resolve_debt_entries` -- what
        `LoopSupervisor._apply_worker_debt_resolution` calls when a
        worker's completion payload carries `debt_resolved` (task 007,
        requirement A.3 prompt half, #155)."""
        return resolve_debt_entries(self.root, ids, resolved_by)

    @property
    def verdict_gate_findings_file(self) -> Path:
        """Task 019 (requirement D.4, #125): the peer verdict gate's own
        free-markdown findings channel, `verdict-gate-findings.md` --
        never the same path `findings_file`/`FINDINGS_FILE` names, so a
        run whose gate rejects never overwrites review's own findings
        with the gate's."""
        return self.root / VERDICT_GATE_FINDINGS_FILE

    @property
    def verdict_gate_findings_json_file(self) -> Path:
        """The structured sidecar beside `verdict_gate_findings_file`
        above -- same shape `findings_json_file` uses for review's own
        channel (`engine.findings.FINDING_SCHEMA`)."""
        return self.root / VERDICT_GATE_FINDINGS_JSON_FILE

    @property
    def approach_metadata_file(self) -> Path:
        """Task 035 (requirement B.1): this run's engine-owned per-approach
        verdict metadata (`APPROACH_METADATA_FILE`). At `self.root` beside
        `petition_adjudications_file` and, for the same reason, NOT one of the
        files `_archive_approach` copies into `approaches/NN/`: it is keyed by
        approach number and answers for all of them at once."""
        return approach_metadata_path(self.root)

    def record_approach_verdict(self, approach: int, channel: str, *,
                                verdict: str,
                                findings: list[dict] | None = None,
                                reason: str | None = None) -> dict:
        """Bound form of `record_approach_verdict` -- what the supervisor
        calls, so no caller has to know where the file lives."""
        return record_approach_verdict(self.root, approach, channel,
                                        verdict=verdict, findings=findings,
                                        reason=reason)

    @property
    def petition_adjudications_file(self) -> Path:
        """Task 011 (requirement C.3, #112): the RUN-scope record of every
        CONFIRMED impossibility-petition adjudication, written ONLY by
        `engine.petitions.record_confirmed_adjudications` (never by a
        worker or review hat's own process). Deliberately at `self.root`
        beside `tasks_file`/`notes_file`/`findings_file` -- but, unlike
        those three, NOT one of the files `_archive_approach` copies into
        `approaches/NN/` on a rejection, so it sits at the same path for
        every approach the run ever plans; that is the whole mechanism
        that lets approach N+1 inherit approach N's CONFIRMED
        adjudications without any explicit carry-forward step.
        """
        return self.root / "petition-adjudications.json"

    @property
    def petitions_file(self) -> Path:
        """Task 065 (requirement N.6, #150): the RUN-scope home for OPEN
        impossibility-petition filings -- a worker's own typed
        declaration (`engine.petitions.file_worker_petition`) and the
        engine's own exhaustion filing (`engine.petitions.
        file_exhaustion_petition`) both write HERE, at `self.root`, once a
        caller passes this run's own handle -- never into a tracked
        `artifacts/reports/<wave>-petitions.md` file in the WORKSPACE
        (`docs/petition-format.md`'s original file, `#150`'s glob-
        discovery half). #150 stays CLOSED: this is not that bug fixed
        again, it is the *tree-side file the glob needs* removed for a
        run built on this code, per N.6's own words. Same markdown
        section shape `format_petition_section`/`parse_petitions` already
        read and write, just rooted here instead of under the workspace,
        so every existing parser/renderer keeps working unchanged. Never
        one of the files `_archive_approach` copies into `approaches/NN/`
        on a rejection -- an open petition, like a CONFIRMED adjudication
        beside it, is a RUN-scope fact every approach shares, not a
        per-approach artifact.
        """
        return self.root / "petitions.md"

    @property
    def steering_dir(self) -> Path:
        return self.root / "steering"

    @property
    def artifacts_dir(self) -> Path:
        return self.root / "artifacts"

    @property
    def lock_file(self) -> Path:
        return self.root / ".lock"

    def acquire_lock(self) -> TextIO:
        """Take an exclusive, non-blocking flock on <run-dir>/.lock.

        Returns the open file object; the caller MUST keep a reference to it
        for the lifetime of the process (closing it, or process exit/SIGKILL,
        releases the flock automatically -- this is relied on for crash
        recovery: a killed engine never leaves a stale false-positive lock).

        The flock is non-blocking on every individual attempt, but a failed
        attempt is re-tried on a short sleep interval
        (ACQUIRE_LOCK_POLL_INTERVAL_S) until ACQUIRE_LOCK_TIMEOUT_S has
        elapsed -- this rides out a predecessor engine's own measured
        terminal-state->exit lag (see ACQUIRE_LOCK_TIMEOUT_S's comment)
        instead of treating that brief overlap as a genuinely locked run
        dir. A lock that is free on the first attempt is returned
        immediately with no sleep at all.

        Raises RunDirLocked if the lock is still held once the deadline
        passes.
        """
        fh = open(self.lock_file, "a+")  # noqa: SIM115 - kept open for process lifetime
        deadline = time.monotonic() + ACQUIRE_LOCK_TIMEOUT_S
        while True:
            try:
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    fh.close()
                    raise RunDirLocked(
                        f"run dir {self.root} is locked by another live engine "
                        f"(exclusive flock held on {self.lock_file})"
                    ) from None
                time.sleep(ACQUIRE_LOCK_POLL_INTERVAL_S)
        fh.seek(0)
        fh.truncate()
        fh.write(f"{os.getpid()}\n")
        fh.flush()
        return fh

    def iteration_dir(self, n: int) -> Path:
        d = self.root / "iterations" / f"{n:04d}"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def begin_iteration_dir(self, n: int) -> Path:
        """The directory a NEW attempt at iteration `n` writes into, with
        anything an EARLIER attempt at the same number left behind archived
        into `iterations/NNNN/attempts/NN/` first (task 019, #44).

        Slot numbers are reused: `max_iteration_number()` counts only finished
        iterations, so an engine killed mid-iteration leaves a half-written
        directory whose number the resumed engine hands to its next attempt.
        Before this, that attempt simply wrote over the dead one -- and the
        prompt, transcript and partial meta.json of the very iteration an
        operator most wants to read (the one that crashed) were gone by the
        time they looked. Archiving keeps the numbering rule (monotonic, no
        renumbering, no gaps) and the evidence, in the same `NN/` shape
        `_archive_approach` uses for a superseded approach.

        Best effort: a filesystem that refuses the move (read-only run dir,
        permissions) gets a warning and the fresh attempt proceeds -- losing a
        crashed transcript is bad, refusing to run the next iteration over it
        is worse.
        """
        d = self.iteration_dir(n)
        try:
            archived = self._archive_previous_attempt(d)
        except OSError as exc:
            log.warning("could not archive iteration %d's earlier attempt: %s",
                        n, exc)
            return d
        if archived is not None:
            files = sorted(p.name for p in archived.iterdir())
            self.emit("iteration.attempt_archived", number=n,
                      attempt=int(archived.name),
                      path=str(archived.relative_to(self.root)), files=files)
            log.info("iteration %d: earlier attempt archived to %s (%s)",
                     n, archived.relative_to(self.root), ", ".join(files))
        return d

    def _archive_previous_attempt(self, itdir: Path) -> Path | None:
        """Move everything an earlier attempt wrote in `itdir` into the next
        free `attempts/NN/`, or return None when the slot is untouched.

        Deliberately generic: it moves whatever files are there rather than a
        list of names, so an attempt record that grows a fourth file is
        archived too, and `attempts/` itself (holding older attempts) is the
        only thing left in place. Not conditional on a missing `endedAt`
        either -- if anything at all is in a slot the engine is about to write,
        it belongs to a previous attempt and is worth more than nothing.
        """
        leftovers = [p for p in itdir.iterdir() if p.name != ITERATION_ATTEMPTS_DIR]
        if not leftovers:
            return None
        attempts = itdir / ITERATION_ATTEMPTS_DIR
        attempts.mkdir(exist_ok=True)
        used = [int(p.name) for p in attempts.iterdir()
                if p.is_dir() and p.name.isdigit()]
        dest = attempts / f"{(max(used) + 1) if used else 1:02d}"
        dest.mkdir()
        for p in leftovers:
            shutil.move(str(p), str(dest / p.name))
        return dest

    # -- status ----------------------------------------------------------
    def read_status(self) -> dict:
        return read_json(self.status_file, {})

    def update_status(self, **patch: Any) -> dict:
        """Merge-patch status.json.

        Task 005 (#173): a patch value of `REMOVE_STATUS_KEY` (above) DELETES
        that key instead of setting it -- `termination=None` and "this
        episode was never told to stop" are different claims to a `--json`
        consumer, and only key absence states the second one.
        """
        status = self.read_status()
        for key, value in patch.items():
            if value is REMOVE_STATUS_KEY:
                status.pop(key, None)
            else:
                status[key] = value
        status["updatedAt"] = utcnow()
        atomic_write_json(self.status_file, status)
        return status

    def write_tasks(self, data: dict) -> None:
        """THE one authorized write path for tasks.json (task 013). Every
        authoritative task-state mutation -- across the whole engine --
        must land on disk through this method and no other. Before this,
        three separate call sites in loop.py (:2478, :2508, :2800 at the
        commit that wrote this comment) each called `atomic_write_json`
        directly against `self.run.tasks_file`; a fourth or fifth write
        site could appear anywhere in the tree with nothing to notice.
        Now they all route through here, and
        `tests/test_task_state_writer.py` runs an AST audit over the
        whole of `src/ralphd` that fails if any module (this one
        included, outside this exact method) ever again calls a
        write-shaped function with `tasks_file` as its target -- with a
        planted-second-writer mutation case proving the audit actually
        catches one.

        Task 105 (#60 requirement D): the actual write is
        `privsep.write_root_owned_json`, not `atomic_write_json` directly --
        under the uid boundary (`privsep.separated()`) the file lands
        root-owned, unreachable for writing by the agent uid that every
        iteration runs as; without the boundary the same call degrades to an
        ordinary agent-owned write with one warning per process. Either way
        this remains the sole writer and the AST audit above is unaffected,
        since the write it's watching for is inside `privsep`, targeting a
        parameter named `path`, not `tasks_file`.

        Task 004 (requirement B, #202): root-ownership of the FILE was not
        enough -- its directory (`self.root`, bind-mounted agent-owned)
        still let a dropped child `os.replace()` a whole substituted
        document onto it. The write below now lands the authoritative copy
        in `self.protected_tasks_file` (a directory the agent cannot write
        into at all, not merely a file it cannot open) and refreshes
        `self.tasks_file` as an ordinary agent-readable VIEW copy for every
        reader that is not the engine (`privsep.write_protected_tasks_json`).
        `read_tasks`/`read_tasks_result` below read only the protected copy.
        """
        privsep.write_protected_tasks_json(
            self.protected_tasks_file, self.tasks_file, data)

    def read_tasks(self) -> dict:
        """The tasks.json payload, via the hardened read path (task 002/#15):
        a mid-write file yields the last-good payload, never `{}`."""
        return self.read_tasks_result().doc

    def read_tasks_result(self) -> TasksRead:
        """Full three-way `TasksRead` (absent / file / last-good / unreadable)
        for callers that must render the stale flag -- `GET /tasks`, the
        `/status` task counts, `ralphctl tasks`, the hub table.

        Task 004 (requirement B): reads the protected, authoritative copy
        (`self.protected_tasks_dir`) once it exists -- never
        `self.tasks_file`, the agent-readable view -- so the engine's own
        trust decisions can never be steered by anything an agent wrote.
        Under the uid boundary that directory is root-owned 0700,
        unreadable at the engine's own (unelevated, agent) effective uid,
        so the read is made inside the same momentary raise the write
        already uses -- symmetric with `petitions._read_stored_key`, which
        borrows this exact context manager for the same reason.

        Falls back to `self.tasks_file` -- the classic, pre-task-004
        location -- ONLY when the record lives in the run dir at all (no
        `privsep.task_record_dir()` mount: the documented degrade, and
        every in-process test fixture) AND no protected copy exists yet: a
        run whose plan was written before this task shipped, or a test
        fixture that seeds a plan by writing `run.tasks_file` directly
        rather than going through `write_tasks()`. The very first
        `write_tasks()` call after either migrates the record into the
        protected copy for good.

        With the mount in force there is no fallback at all, deliberately:
        `privsep.migrate_task_record()` has already lifted a pre-existing
        run's record into the protected directory at engine startup --
        before the API listens and before any iteration exists -- so a
        fallback here could only ever be reached by an agent-planted
        document, never by a genuine older record.
        """
        def _pick_source_dir() -> Path:
            protected_dir = self.protected_tasks_dir
            if protected_dir != privsep.legacy_protected_tasks_dir(self.root):
                return protected_dir      # mounted: the only source, ever
            # Task 004 correction: this existence check has to run INSIDE
            # the same elevated window as the read below, not before it --
            # `Path.exists()` swallows the `PermissionError` a 0700
            # root-owned directory raises at the engine's own (unelevated,
            # agent) effective uid and reports `False` regardless of the
            # true answer, which would silently misroute every read back
            # onto the unprotected view the moment the boundary is active.
            protected_exists = (protected_dir / "tasks.json").exists()
            if protected_exists or not self.tasks_file.exists():
                return protected_dir
            return self.root

        if privsep.separated():
            with privsep.root_for_write():
                return read_tasks_doc(_pick_source_dir())
        return read_tasks_doc(_pick_source_dir())

    # -- vigilant-mode verification: task record, not a sidecar (task 020) -
    # Through v0.8 this was a separate file, engine-owned and never touched
    # by the agent, of which task ids had already received a passing
    # ("taskVerified") verify iteration under vigilant mode -- what let a
    # resumed engine tell "this task is still 'completed' because it was
    # already verified" apart from "... because the prior process crashed
    # between the worker iteration that completed it and the verify
    # iteration that was supposed to check it", since a per-process
    # before/after tasks.json diff can't distinguish those two cases across
    # a crash/resume boundary (both leave the task reading "completed" in
    # the very first snapshot the new process ever sees). Entries were
    # stored **approach-namespaced** (`"2:001"`) because every approach's
    # planning pass rewrites tasks.json renumbering its tasks from "001", so
    # a flat list of bare ids let approach 2's task 001 collide with
    # approach 1's already-verified task 001 (issue #29).
    #
    # Requirement F bullet 4 folds that whole role into the task record
    # itself: `LoopSupervisor._relabel_vigilant_claims` and `_verify_task`
    # (`engine/loop.py`) write the vigilant mini-FSM's own `awaiting-
    # validation` / `validated` statuses directly onto each task, so
    # "already verified" and "still needs verification" are just that
    # task's `status` field -- no second file to keep in sync, and #29's
    # property holds for free: a later approach's tasks.json is a
    # completely fresh document (renumbered from "001", no prior
    # `status`), so it can never be misread as inheriting an earlier
    # approach's `validated` task.
    @property
    def vigilant_verified_file(self) -> Path:
        """The pre-0.9 sidecar's path -- read-only now, and only by
        `migrate_legacy_vigilant_verified()` below, for a run dir that
        predates the fold."""
        return self.root / "vigilant-verified.json"

    def current_approach(self) -> int:
        """The approach number this run is on -- engine-owned status, so it
        is stable across crash/resume. Used by the legacy-sidecar migration
        below (and, historically, by the record it is migrating away
        from)."""
        return self.read_status().get("approach") or 1

    def _resolve_legacy_vigilant_ids(
            self, by_approach: dict[int, set[str]], approach_now: int,
            completed_tasks: list[dict]) -> tuple[set[str], bool]:
        """The seam `migrate_legacy_vigilant_verified()`'s mutation test
        patches to reproduce the pre-fix, current-approach-only matching:
        direct hits under `approach_now`'s own namespace, PLUS -- the task
        005/#75 fix -- any other approach's namespaced entry for a task id
        whose archived snapshot for that same approach shows the identical
        task dict already `"completed"` (guards #29's collision: an
        approach that never got archived, or whose archived copy of this id
        differs, contributes nothing). Returns `(resolved_ids, used_fallback)`
        -- `used_fallback` is True the instant any id was resolved via the
        second path, which the caller treats as reason enough to keep the
        sidecar around rather than delete it.
        """
        resolved_ids: set[str] = set(by_approach.get(approach_now, set()))
        used_fallback = False
        for other_approach, ids in by_approach.items():
            if other_approach == approach_now:
                continue
            archived_doc = read_tasks_doc(
                archived_approach_dir(self.root, other_approach), persist=False
            ).doc
            archived_by_id = {t.get("id"): t for t in archived_doc.get("tasks", [])}
            for t in completed_tasks:
                task_id = t["id"]
                if (task_id in ids and task_id not in resolved_ids
                        and archived_by_id.get(task_id) == t):
                    resolved_ids.add(task_id)
                    used_fallback = True
        return resolved_ids, used_fallback

    @staticmethod
    def _legacy_vigilant_fold_is_uncertain(
            completed_tasks: list[dict], used_fallback: bool,
            matched_completed: int) -> bool:
        """The second seam the mutation test patches alongside
        `_resolve_legacy_vigilant_ids`, to reproduce the pre-fix behaviour
        in full: the old code unlinked the sidecar unconditionally, with no
        notion of "the fold had to guess" or "matched nothing despite
        something to match". True keeps the sidecar; False is the only
        case confident enough to unlink -- no completed tasks needed
        classifying at all (`not completed_tasks`), or every one of them
        was resolved via a direct current-approach match with no fallback
        needed.
        """
        return bool(completed_tasks) and (used_fallback or matched_completed == 0)

    def migrate_legacy_vigilant_verified(self) -> None:
        """One-way-WHEN-SAFE migration off the pre-0.9 `vigilant-verified.json`
        sidecar (task 020), for a run dir an older engine build already
        started. A no-op for a run dir that never had vigilant mode on (no
        sidecar ever written).

        Every task still sitting at the legacy vigilant CLAIM state
        `"completed"` becomes either `"validated"` (the sidecar says it
        already passed a verify iteration) or `"awaiting-validation"` (it
        does not -- exactly the crash-between-completion-and-verification
        window guard 4 protects, now expressed as the task's own status
        instead of the absence of a sidecar entry, so the resumed engine's
        ordinary `pending_verify` computation picks it back up unchanged).

        Task 005 (requirement J, #75): the sidecar's keys are
        approach-namespaced (`"<approach>:<taskId>"`, task 020/#29) because
        every approach's planning pass renumbers tasks.json from "001", so a
        flat match would let approach 2's task "001" inherit approach 1's
        already-verified task "001". The pre-fix code enforced that by
        matching ONLY `current_approach()`'s namespace -- which is safe
        exactly as long as the live tasks.json really is a fresh replan for
        the current approach. It is NOT, on a run dir where the approach
        counter advanced without the archive-then-replan that normally goes
        with it (#72's now-fixed bug, or any future one shaped like it):
        `selfdev-v09-fsm-loop` had `status.json.approach == 4` while
        tasks.json still held the 20 tasks *completed and verified under
        approach 3*, unrenumbered. Matching only approach 4's (empty)
        namespace found nothing, demoted all 20 to `awaiting-validation`,
        and then unlinked the sidecar anyway -- the sidecar was the only
        record of the lost verifications, and it is one-way.

        Fix: for a completed task the CURRENT approach's namespace does not
        cover, fall back to any OTHER approach's namespaced entries for
        that same task id, but only when that approach's own ARCHIVED
        snapshot (`archived_approach_dir`) shows the identical task dict
        (same id, title, successCriteria, status) already `"completed"` --
        i.e. the live tasks.json is still, unrenumbered, that earlier
        approach's plan, not a same-numbered-but-unrelated task from an
        approach that WAS properly replanned (which is exactly what keeps
        the #29 collision guarded: `archived_approach_dir` for an approach
        that never got archived, or whose archived task differs, matches
        nothing).

        The fallback is a heuristic, not a certainty (it depends on the
        archived snapshot really being the same document, never proven
        beyond the id/title/successCriteria/status comparison), so using it
        for ANY task, or resolving zero of a nonempty sidecar's names
        against a nonempty set of completed tasks, withholds the delete: a
        fold that had nothing to do (no completed tasks at all) is safe to
        clean up; a fold that had to guess, or came up empty despite there
        being something to match, is not -- an `vigilant_verified_migration`
        event records which case fired, so "legitimately nothing to fold"
        and "matched nothing while there was something to match" are
        distinguishable in the record, not just in this method's return
        value.
        """
        if not self.vigilant_verified_file.exists():
            return
        approach_now = self.current_approach()
        by_approach: dict[int, set[str]] = {}
        for entry in read_json(self.vigilant_verified_file, []):
            prefix, sep, task_id = str(entry).partition(":")
            if not sep:
                # Predates #29's own namespacing fix -- always approach 1.
                by_approach.setdefault(1, set()).add(prefix)
                continue
            try:
                approach_key = int(prefix)
            except ValueError:
                continue
            by_approach.setdefault(approach_key, set()).add(task_id)

        tasks_data = self.read_tasks()
        completed_tasks = [t for t in tasks_data.get("tasks", []) if t.get("status") == "completed"]

        resolved_ids, used_fallback = self._resolve_legacy_vigilant_ids(
            by_approach, approach_now, completed_tasks)

        changed = False
        matched_completed = 0
        for t in completed_tasks:
            t["status"] = "validated" if t["id"] in resolved_ids else "awaiting-validation"
            changed = True
            if t["id"] in resolved_ids:
                matched_completed += 1
        if changed:
            self.write_tasks(tasks_data)

        uncertain = self._legacy_vigilant_fold_is_uncertain(
            completed_tasks, used_fallback, matched_completed)
        self.emit("vigilant_verified_migration",
                   approach=approach_now,
                   completedTaskCount=len(completed_tasks),
                   matchedCount=matched_completed,
                   usedFallback=used_fallback,
                   sidecarKept=uncertain)
        if uncertain:
            return
        self.vigilant_verified_file.unlink()

    # -- resume (PRD req 16) ---------------------------------------------
    def max_iteration_number(self) -> int:
        """Highest iteration number with a *completed* meta.json (i.e. its
        "endedAt" field is set) already on disk, or 0 if none.

        Used to resume iteration numbering monotonically when an engine
        restarts over an existing run dir: LoopSupervisor seeds its
        iterations_used counter from this so the next iteration continues
        from N+1 instead of restarting at 1. An iteration dir that exists
        but never finished (no "endedAt" -- e.g. the previous engine
        process was killed mid-iteration) is deliberately not counted;
        that slot's number is reused by the next attempt, which since task
        019 (#44) archives the crashed attempt's files into
        `iterations/NNNN/attempts/NN/` (RunDir.begin_iteration_dir) instead
        of overwriting them. The rule itself is unchanged: what is reused is
        the NUMBER, not the record.
        """
        best = 0
        itdir = self.root / "iterations"
        if not itdir.is_dir():
            return 0
        for d in itdir.iterdir():
            if not (d.is_dir() and d.name.isdigit()):
                continue
            meta = read_json(d / "meta.json", {})
            if meta.get("endedAt"):
                best = max(best, int(d.name))
        return best

    # -- schema version (PRD req 18) --------------------------------------
    def check_schema_version(self) -> int:
        """Read the run dir's recorded schemaVersion (0 if status.json is
        absent or predates this field -- a pre-schema run dir).

        Raises SchemaVersionTooNew if the recorded version is newer than
        this engine build's CURRENT_SCHEMA_VERSION. Touches nothing on
        disk either way -- callers stamp the (possibly upgraded) current
        version back only once they've decided startup proceeds.
        """
        recorded = self.read_status().get("schemaVersion", 0)
        if recorded > CURRENT_SCHEMA_VERSION:
            raise SchemaVersionTooNew(
                f"run dir {self.root} has schemaVersion {recorded}, but this "
                f"engine build only knows schemaVersion {CURRENT_SCHEMA_VERSION} "
                f"(older engine, newer run dir); refusing to start"
            )
        return recorded

    # -- events ----------------------------------------------------------
    def emit(self, type_: str, **data: Any) -> dict:
        with self._events_lock:
            self._event_id += 1
            event = {"id": self._event_id, "ts": utcnow(), "type": type_, **data}
            # Mechanical secret redaction (task 060): scrub the persisted
            # line -- never the in-memory dict returned to the caller, which
            # isn't written anywhere else.
            line = scrub_text(json.dumps(event))
            with open(self.root / "events.jsonl", "a") as f:
                f.write(line + "\n")
        return event

    # -- router calls (task 022, P, #80) -----------------------------------
    def record_router_decision(self, record: dict) -> None:
        """Append one COMPLETED router decision to
        `artifacts/router/decisions.jsonl` -- the ONE write path this file
        ever gets, called exactly once by
        `LoopSupervisor._route_with_router` right after `router.decide()`
        returns (never per `_ask_router` attempt: a decision that took
        several attempts is still one line, carrying its own `attempts`
        count).

        Deliberately a plain append, not `self.emit()`: this is a second,
        purpose-built stream living beside the router's own transcripts
        (`artifacts/router/NNNN.jsonl`) rather than a line in
        `events.jsonl`, mirroring how `artifacts/` already holds files no
        `RunDir` method reads back except through their own dedicated
        shaping (`state.artifact_entries`'s role for `state.
        router_call_entries`, task 022).
        """
        router_dir = self.artifacts_dir / "router"
        router_dir.mkdir(parents=True, exist_ok=True)
        line = scrub_text(json.dumps({"ts": utcnow(), **record}))
        with open(router_dir / ROUTER_DECISIONS_FILENAME, "a") as f:
            f.write(line + "\n")

    # -- steering --------------------------------------------------------
    def add_steering(self, message: str, name: str | None = None) -> str:
        existing = sorted(self.steering_dir.glob(STEERING_GLOB))
        seq = int(existing[-1].name[:3]) + 1 if existing else 1
        suffix = name or "steering"
        # The sequence prefix is always engine-assigned. If the caller's
        # --name already carries its own NNN- prefix (e.g. copy-pasted from
        # a previous steering filename), strip it so we don't double up
        # into "022-019-steering.md"; a bare name is left untouched.
        m = re.match(r"^\d{3}-(.+)$", suffix)
        if m:
            suffix = m.group(1)
        fname = f"{seq:03d}-{suffix}.md"
        atomic_write(self.steering_dir / fname, message.rstrip() + "\n")
        self.emit("steering.received", file=fname)
        # v0.25 task 028a (PRD requirement K.1/K.2, #267): ring the loop's
        # own wake bell, if one is registered (`set_wake_event`, called by
        # `LoopSupervisor.__init__` in production; never in a bare
        # `RunDir(root=...)` test that has no loop at all) -- AFTER the
        # file is on disk, so a waiter this wakes always finds the new
        # message already in `pending_steering()`'s next read.
        if self._wake_event is not None:
            self._wake_event.set()
        return fname

    def set_wake_event(self, event) -> None:
        """v0.25 task 028a (PRD requirement K.1/K.2, #267): register the
        one `asyncio.Event` `_sleep_until_earliest_deferred` (and, task
        028b, `_wait_out_backoff`) races against its
        timer -- set here by `LoopSupervisor.__init__` and by nothing
        else, so `add_steering` above (and `LoopSupervisor._record_abort`)
        have a bell to ring without this module importing `asyncio` or
        knowing the loop exists."""
        self._wake_event = event

    def consumed_marker(self) -> Path:
        return self.steering_dir / STEERING_CONSUMED_FILE

    def pending_steering(self) -> list[Path]:
        consumed = steering_consumed_names(self.root)
        return [p for p in sorted(self.steering_dir.glob(STEERING_GLOB))
                if p.name not in consumed]

    def steering_entries(self, *, bodies: bool = True) -> list[dict]:
        """Task 016 (#17): this run's steering messages, through the ONE shared
        reader the host side uses too (module-level `steering_entries`)."""
        return steering_entries(self.root, bodies=bodies)

    def steering_ledger(self) -> list[dict]:
        """Task 034: this run's steering LEDGER, through the ONE shared
        reader (module-level `steering_ledger`) -- one entry per steering
        file, joining the operator's message with the steer hat's own
        settlement claim."""
        return steering_ledger(self.root)

    # -- operator rulings (task 013, v0.23 requirement A.2, #224) --------
    def add_ruling(self, text: str, name: str | None = None) -> str:
        """Record one operator ruling, resolving its `signature` key from
        THIS run's own `status.json` -- the entry for the approach that
        just rejected (`rejectionSignatures[str(approach)]`, the exact map
        `_record_rejection_signature` writes and `_repeated_rejection_
        signature` reads), never a value the caller supplies. `None` when
        no rejection signature has been recorded yet (a ruling filed ahead
        of one, or a run that has never taken the ordinary-rejection
        branch at all) -- `write_ruling` still records the entry, unscoped.

        The ONE writer both `POST /rule` and `ralphctl rule` (which posts
        to that same route, exactly like `ralphctl steer` posts to `POST
        /steering`) go through -- never a second, host-side writer.
        """
        status = self.read_status()
        approach = status.get("approach")
        signatures = status.get("rejectionSignatures") or {}
        signature = signatures.get(str(approach)) if approach is not None else None
        return write_ruling(self.root, text, signature=signature, name=name)

    def ruling_entries(self) -> list[dict]:
        """This run's recorded rulings, through the ONE shared reader
        (module-level `ruling_entries`)."""
        return ruling_entries(self.root)

    def consume_steering(self, files: list[Path], iteration: int) -> None:
        """Mark `files` applied by iteration `iteration` (task 018, #34).

        Called by `LoopSupervisor._run_iteration_once` only AFTER the
        iteration that carried the notes finished cleanly -- see the
        delivery-vs-consumption comment there for the at-least-once choice
        and the ordering that implements it.

        Idempotent by construction: a name already in `.consumed.json` is
        neither appended again nor re-announced, so "applied" is a set and no
        entry can be counted (or evented) twice however often this is
        called.
        """
        raw = read_json(self.consumed_marker(), [])
        consumed = [n for n in raw if isinstance(n, str)] if isinstance(raw, list) else []
        already = set(consumed)
        fresh = [p for p in files if p.name not in already]
        if not fresh:
            return
        consumed.extend(p.name for p in fresh)
        atomic_write_json(self.consumed_marker(), consumed)
        # Task 034: the steering ledger's consumption timestamp is stamped
        # from THIS call, for exactly the freshly-consumed names, so the
        # ledger and `.consumed.json` cannot disagree about which files an
        # iteration actioned or when.
        record_steering_consumption(self.root, [p.name for p in fresh],
                                    iteration)
        for p in fresh:
            self.emit("steering.consumed", file=p.name, iteration=iteration)

    def unconsume_steering(self, names) -> list[str]:
        """Task 006 (requirement B.1, #122): reverse `consume_steering`'s
        marker write for `names` (through the module-level
        `unconsume_steering_names`), so a message a clean steer iteration
        honestly reported `settled: false` on is redelivered rather than
        silently kept applied. Emits `steering.redelivered` per name
        actually removed -- a no-op name (already unconsumed) is neither
        re-removed nor re-announced, mirroring `consume_steering`'s own
        idempotence.
        """
        removed = unconsume_steering_names(self.root, names)
        for name in removed:
            self.emit("steering.redelivered", file=name)
        return removed

    def mark_steering_settled_by_exhaustion(self, names, *,
                                            reason: str) -> dict:
        """Task 006 (requirement B.1, #122): the redelivery cap's
        fail-closed outcome (through the module-level
        `mark_steering_settled_by_exhaustion`) -- `names` stop being
        redelivered and are recorded settled BY EXHAUSTION, with `reason`,
        never by the hat's own claim. Emits `steering.settled_by_exhaustion`
        per name.
        """
        records = mark_steering_settled_by_exhaustion(self.root, names,
                                                       reason=reason)
        for name in names:
            self.emit("steering.settled_by_exhaustion", file=name, reason=reason)
        return records
