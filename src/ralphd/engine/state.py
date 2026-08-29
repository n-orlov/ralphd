"""Run-dir state: paths, atomic JSON writes, events log."""

from __future__ import annotations

import calendar
import fcntl
import json
import logging
import os
import re
import shutil
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
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
from . import privsep
from . import transitions
from .faults import explain_fault, matched_signature
from .redact import redact_job_yaml, scrub_text
from . import tiers

log = logging.getLogger("ralphd.state")


class RunDirLocked(Exception):
    """Raised when another live engine already holds the run dir's lock."""


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
                             f"{_bundle_suffix(rung)}")
            continue
        model = row.get("model") or "pi default"
        lines.append(f"{hat:<{_PROVISIONING_HAT_WIDTH}} {model}"
                     f"{_bundle_suffix(row)}")
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
    "(see artifacts/reports/pricing-anomaly.md)")


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


def _money(amount: float, decimals: int | None) -> str:
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
        parts = ([_money(amount, decimals)]
                 if isinstance(amount, float) and not is_zero_quote(usage) else [])
        parts.append(f"~{_money(derived, decimals)} {COST_DERIVED_WORD}")
        rendered = " + ".join(parts)
        return (f"{rendered}, partial (rest {COST_UNAVAILABLE})"
                if status == "partial" else rendered)
    if amount is None:
        return None
    rendered = _money(amount, decimals)
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
NONTERMINAL_STATES = ("starting", "running", "finishing")

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
    """
    rec = rec or {}
    ref = rec.get("image")
    short = format_image_id(rec.get("imageId"))
    if not isinstance(ref, str) or not ref.strip():
        return ""
    ref = ref.strip()
    if not short or short in ref:
        return ref
    return f"{ref}  (id {short})"


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
      `state`  `pending` or `applied` -- the vocabulary lives here, so the
               CLI, the hub and the API all say the same word,
      `consumed` the same fact as a bool, kept because the pre-v0.6
               `GET /steering` answered with exactly that key,
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
        entry = {
            "file": p.name,
            "seq": seq,
            "name": name,
            "ts": utc_from_epoch(st.st_mtime),
            "state": STEERING_APPLIED if applied else STEERING_PENDING,
            "consumed": applied,
            "bytes": st.st_size,
            "hasBody": bool(text.strip()),
        }
        if bodies:
            entry["body"] = text
        out.append(entry)
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


def amendments_dir(config_root: Path) -> Path:
    return Path(config_root) / AMENDMENT_DIRNAME


def write_amendment(config_root: Path, task_id: str, text: str, *,
                    waive: bool = False, status: str | None = None) -> str:
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
    header += f"ts: {utcnow()}\n\n"
    atomic_write(adir / fname, header + text.rstrip() + "\n")
    return fname


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
        out.append({
            "file": p.name,
            "seq": seq,
            "taskId": task_id,
            "waive": mode == AMENDMENT_MODE_WAIVE,
            "mode": mode,
            # Task 029: the ruled target status, only meaningful (and only
            # ever non-None) for a `mode: status` entry -- a waive/replace
            # entry never carries a `status:` header line at all.
            "status": fields.get("status") if mode == AMENDMENT_MODE_STATUS else None,
            "ts": ts,
            "text": body.strip(),
        })
    return out


def apply_task_status_change(run: "RunDir", task_id: str, target_status: str,
                             vigilant: bool, *, source: str,
                             reason: str = "", file: str | None = None) -> dict:
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
      every other status move in the engine is checked against) writes the
      new `status` via `run.write_tasks` (the one authorized writer, task
      013) and emits an `amendment_status_applied` audit event;
    - an ILLEGAL transition is refused: nothing is written, and the
      returned record says why.

    `validationAttempts`, `tierLadderExhausted` and `successCriteria` are
    never touched here -- only the one field, `status`, ever moves (PRD
    requirement E: a status change is not a claim the criteria were met).

    Returns `{"taskId", "status", "previous", "applied", "reason"}` --
    `reason` is `""` when applied, and one of `"no matching task"` /
    `"already at that status"` / `"illegal transition"` otherwise.
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
    task["status"] = target_status
    run.write_tasks(data)
    run.emit("amendment_status_applied", taskId=task_id, field="status",
            previous=current, status=target_status, source=source,
            file=file, reason=reason)
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
    """
    data = run.read_tasks()
    tasks = data.setdefault("tasks", [])
    task_id = task.get("id")
    if any(isinstance(t, dict) and t.get("id") == task_id for t in tasks):
        return {"inserted": False, "reason": "id already exists", "taskId": task_id}
    new_task = dict(task)
    new_task["status"] = "pending"
    new_task["origin"] = TASK_ORIGIN_STEER
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


def steer_task_update(run: "RunDir", task_id: str, changes: dict,
                      vigilant: bool) -> dict:
    """Update one task's fields. THE status-changing tool (PRD requirement
    H): a `status` key in `changes` is NEVER written directly here -- it is
    routed through `apply_task_status_change`, the same applier
    `apply_amendment_status_rulings` uses for an operator's own ruling, so
    exactly one place validates the transition, emits the audit event and
    preserves `validationAttempts`/`tierLadderExhausted`. Every other key
    (`title`, `successCriteria`, `dependsOn`, `priority`, ...) is a plain
    dict-merge write with no transition to validate.
    """
    changes = dict(changes)
    target_status = changes.pop("status", None)
    status_result = None
    if target_status is not None:
        status_result = apply_task_status_change(
            run, task_id, target_status, vigilant, source="steer")
    if changes:
        data = run.read_tasks()
        tasks = data.get("tasks", [])
        by_id = {t.get("id"): t for t in tasks if isinstance(t, dict)}
        task = by_id.get(task_id)
        if task is None:
            return {"updated": False, "reason": "no matching task",
                    "taskId": task_id, "statusResult": status_result}
        task.update(changes)
        run.write_tasks(data)
    return {"updated": True, "taskId": task_id, "statusResult": status_result}


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
        lines.append(model_line)
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
            produced_traffic=bool(usage) or bool(detail.get("sawComplete")),
        )
        detail["reason"] = derived["reason"]
        detail["signature"] = derived["signature"]
        if derived["faultClass"] != recorded_fault:
            detail["notices"].append(FAULT_VERDICT_DIVERGED_NOTICE)
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
        such field at all, so there is nothing to fall back to but derive."""
        return task_blocked_by(self.tasks)

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
        TASK_COMPLETION_STATUSES + ("awaiting-validation",))


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
# verdict ends the run (`LoopSupervisor._review_verdict`/`_maybe_grace_
# review`) -- is the one dispatch requirement I's "hats with no task in
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
TASK_DEPENDENCY_SATISFIED_STATUSES = ("completed", "validated")
TASK_DEPENDENCY_DEAD_STATUSES = ("failed", "skipped")

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
# `tasks.json` verbatim -- the same set `plan_ingestion._TASK_PASSTHROUGH_
# FIELDS` uses for a planner's proposal, duplicated here (not imported)
# because this module must not depend on `plan_ingestion` (the reverse
# dependency already exists: `plan_ingestion.lint_worker_split` is called
# FROM `LoopSupervisor`, never the other way).
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
TASK_TERMINAL_STATUSES = ("validated", "completed", "failed", "skipped")


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


def task_blocked_by(tasks: list) -> dict[str, list[str]]:
    """Task 021 (#60 pillar 3): `{task id: [dead/doomed ancestor ids]}` for
    every task that can never be completed because a `dependsOn` entry --
    directly, or transitively through another doomed task -- reached
    `failed` or `skipped`. This is the PRD's "dead dependencies are
    harness-derived" requirement: the harness marks the dependents here, so
    no prompt has to notice and no agent has to write a note about it
    (worker.md used to ask for exactly that, by hand).

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
        if tid in dead or tid not in doomed:
            continue
        causes = sorted(dep for dep in depends_on.get(tid, []) if dep in doomed)
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


def format_task_status(task: dict, blocked_by: list | None = None) -> str:
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
    return text


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

# Where a document lives: the run dir, or the run's job config dir (which the
# CLI/hub know as `<registry>/configs/<run-id>` and the engine as `/config`).
DOC_IN_RUN = "run"
DOC_IN_CONFIG = "config"

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
    ("job", JOB_CONFIG_FILE, DOC_IN_CONFIG,
     "effective job config as inlined at start, secret values redacted"),
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
        base = root if where == DOC_IN_RUN else cdir
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
    lines = [f"{'DOCUMENT':<14}{'FILE':<20}{'SIZE':>13}  DESCRIPTION"]
    for doc in docs:
        lines.append(f"{doc.get('key', ''):<14}{doc.get('name', ''):<20}"
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
      `body`      the text -- omitted entirely with `bodies=False`, for a file
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
            entry["body"] = path.read_text(errors="replace")
        except OSError:
            entry["body"] = RUN_DOCUMENT_UNREADABLE
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
# Task 019 (requirement M): `ROUTER_FALLBACK` now has exactly ONE real entry
# (`plan_review`, the opt-in peer gate's own multi-edge state), so this file
# is still empty for every run that leaves that gate off -- which is every
# default run -- and non-empty only when an enabled gate got a reply it
# could not decide from. See `artifacts/reports/router-calls-surface.md` for
# the pre-gate claim's evidence.

# `artifacts/router/decisions.jsonl`'s own filename, spelled once.
ROUTER_DECISIONS_FILENAME = "decisions.jsonl"

NO_ROUTER_CALLS = (
    "(no router calls this run -- every state this run reached decided its "
    "own edge deterministically; the only router-ambiguous state shipped "
    "today is the opt-in peer plan-review gate's own `plan_review`, and it "
    "asks the router only for a reply it cannot decide from; see "
    "artifacts/reports/router-calls-surface.md)"
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


def router_call_entries(run_root: Path) -> list[dict]:
    """Every completed router decision this run recorded, oldest first, as
    `router_call_entry` dicts -- the ONE reader `ralphctl router` and the
    hub's router panel both use, mirroring `artifact_entries`'s role for
    `artifacts/`.

    Purely on-disk, like every other artifacts-derived surface: a live run
    and one whose container is long gone read identically. A run that never
    reached a router-ambiguous state (every shipped run, today) answers with
    an empty list, which the caller pairs with `NO_ROUTER_CALLS`.
    """
    path = Path(run_root) / ARTIFACTS_DIR_NAME / "router" / ROUTER_DECISIONS_FILENAME
    try:
        lines = path.read_text().splitlines()
    except OSError:
        return []
    entries = []
    for i, line in enumerate(lines, start=1):
        line = line.strip()
        if not line:
            continue
        try:
            raw = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(raw, dict):
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

# Event types the explanation reads, so a huge events.jsonl is not held in
# memory to answer one question.
FAULT_EVENT_TYPES = ("infra_retry", "infra_wait", "infra_recovered",
                     "reflect_infra_delay")


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
            produced_traffic=bool(usage) or bool(detail.get("sawComplete")),
        )
        exp.update(hasFault=True,
                   faultClass=recorded,
                   reason=derived["reason"],
                   signature=derived["signature"],
                   iteration=detail.get("number"),
                   phase=detail.get("phase"),
                   error=str(detail.get("error") or "") or None,
                   iterationDetail=detail)
        if derived["faultClass"] != recorded:
            exp["notices"].append(FAULT_VERDICT_DIVERGED_NOTICE)
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
                            "costDisplay", "costSource", "byPhase", "byApproach", "byTier")


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
    cells = [(b["key"], b["costDisplay"] or USAGE_NONE, b["tokensTotalDisplay"])
             for b in buckets]
    cost_w = max(len(c[1]) for c in cells)
    tok_w = max(len(c[2]) for c in cells)
    return [f"  {k:<{key_w}}  {cost:<{cost_w}}  {tokens:>{tok_w}}".rstrip()
            for k, cost, tokens in cells]


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
    lines = [f"cost:      {total.get('costDisplay') or USAGE_NONE}",
             f"tokens:    {total.get('tokensDisplay') or USAGE_NONE}"]
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
        return self.root / "tasks.json"

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

        Raises RunDirLocked if another live process already holds it.
        """
        fh = open(self.lock_file, "a+")  # noqa: SIM115 - kept open for process lifetime
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            fh.close()
            raise RunDirLocked(
                f"run dir {self.root} is locked by another live engine "
                f"(exclusive flock held on {self.lock_file})"
            ) from None
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
        status = self.read_status()
        status.update(patch)
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
        """
        privsep.write_root_owned_json(self.tasks_file, data)

    def read_tasks(self) -> dict:
        """The tasks.json payload, via the hardened read path (task 002/#15):
        a mid-write file yields the last-good payload, never `{}`."""
        return self.read_tasks_result().doc

    def read_tasks_result(self) -> TasksRead:
        """Full three-way `TasksRead` (absent / file / last-good / unreadable)
        for callers that must render the stale flag -- `GET /tasks`, the
        `/status` task counts, `ralphctl tasks`, the hub table."""
        return read_tasks_doc(self.root)

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
        return fname

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
        for p in fresh:
            self.emit("steering.consumed", file=p.name, iteration=iteration)
