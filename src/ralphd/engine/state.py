"""Run-dir state: paths, atomic JSON writes, events log."""

from __future__ import annotations

import calendar
import fcntl
import json
import os
import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TextIO

from ..log_merge import iteration_dir, iteration_numbers, iteration_output_path
from .faults import explain_fault, matched_signature
from .redact import redact_job_yaml, scrub_text


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
NONTERMINAL_STATES = ("starting", "running")

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


def record_operator_termination(run_root: Path, action: str,
                                reason: str = "", source: str = "") -> dict:
    """Write OPERATOR_TERMINATION_FILE into a run dir. `action` is the
    operator verb ("abort"/"stop"), `source` says who recorded it
    ("engine"/"cli"). Idempotent: a later record overwrites an earlier one
    (the most recent operator intent is the one that counts). Missing run
    dir -> no-op, so callers never have to guard."""
    doc = {"action": action, "at": utcnow(),
           "reason": reason or "", "source": source or ""}
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


# Task 023 (#8): the tasks.json `status` string -> /status `tasks` counts key
# mapping, in ONE place. Both the engine (GET /status) and the host-side CLI
# fallback (`ralphctl status` on an unreachable run) count the same tasks.json,
# so they must agree key-for-key -- a second copy of this mapping is how
# `tasks: 5/7 completed` and `tasks: 5/7 completed (1 in-progress)` start
# disagreeing about the same file.
_TASK_COUNT_KEYS = {"in-progress": "inProgress", "validation-failed": "validationFailed"}


# The names of a run's well-known documents, in ONE place: `RunDir`'s
# properties, `prd_path` and the run-document surfaces (task 021, #18.2) all
# spell them from here, so "which file is the handoff notes" is a constant
# rather than a string literal repeated per reader.
PRD_FILE = "prd.md"
COMPOSITE_PRD_FILE = "composite-prd.md"
NOTES_FILE = "notes.md"
FINDINGS_FILE = "review-findings.md"
# `job.yaml` lives in the run's *config* dir (mounted read-only at `/config`),
# not in the run dir -- see `run_documents`.
JOB_CONFIG_FILE = "job.yaml"


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
# iteration consumes it (`RunDir.consume_steering`). Both the engine's
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
    "hasTranscript", "transcriptBytes",
)

# Whether a duration is final or still growing (`ralphctl status`' own words).
DURATION_TOTAL = "total"
DURATION_ELAPSED = "elapsed"

# Said out loud for an iteration dir whose meta.json is absent or truncated:
# the transcript is still readable, the metadata is genuinely unknown, and a
# row of `None`s would look like recorded facts.
ITERATION_NO_META_NOTICE = ("!! no readable meta.json for this iteration "
                            "(nothing known but the transcript)")


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
    if detail.get("verifiedTask"):
        lines.append(f"verified:  task {detail['verifiedTask']} "
                     f"-> {detail.get('verifyOutcome')}")
    return lines


def iteration_detail(run_root: Path, number: int) -> dict | None:
    """Everything known about one iteration of a run, or None when the run dir
    holds no such iteration (task 019, #18.1).

    `iterations/NNNN/meta.json` verbatim (so nothing the engine records is
    dropped -- `steeringConsumed`, `verifiedTask`/`verifyOutcome`,
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
                         rendering lives).

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
    return detail


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


def task_counts(tasks: list) -> dict:
    """Count a tasks.json task list into the /status `tasks` shape:
    {"total": N, "completed": N, "inProgress": N, "pending": N,
    "validationFailed": N, ...}. Only statuses actually present get a key
    (plus `total`, always); an unrecognised status is passed through under
    its own name, and a task with no status at all counts as "unknown"."""
    counts = {"total": len(tasks)}
    for t in tasks:
        raw = t.get("status") if isinstance(t, dict) else None
        key = _TASK_COUNT_KEYS.get(raw, raw or "unknown")
        counts[key] = counts.get(key, 0) + 1
    return counts


# Task 013 (#21): how a `task_counts()` key is SPELLED for a human, in ONE
# place. `ralphctl status` (`cli/main.py:_summarize_tasks`), `ralphctl runs`
# and the hub's TASKS column all render the same counts dict, so the wording
# lives here beside the counter -- a second copy is how `1 in-progress` in
# one surface and `1 inProgress` in another get born.
TASK_STATUS_LABELS = {"inProgress": "in-progress",
                      "validationFailed": "validation-failed"}

# Which statuses are *trouble* an at-a-glance view should flag, in the order
# they are shown (worst first). Not a rendering decision the hub gets to make
# on its own: `ralphctl runs` flags the same two.
TASK_TROUBLE_KEYS = ("validationFailed", "inProgress")

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
    completed = counts.get("completed", 0)
    others = []
    for key, value in counts.items():
        if key in ("total", "completed") or not value:
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
        completed = int(counts.get("completed") or 0)
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
    out = []
    for key in TASK_TROUBLE_KEYS:
        value = counts.get(key)
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
     "the prompt/skill diff the reflect phase proposes (never applied)"),
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
      `body`      the text -- omitted entirely with `bodies=False`, for a file
                  that does not exist and for a binary one, since an empty
                  string would read like an empty file (`artifact_body` turns
                  each of those into its own wording).
    """
    rel = str(rel_path).replace("\\", "/")
    path = Path(run_root) / ARTIFACTS_DIR_NAME / rel
    entry = {"path": rel, "key": artifact_key(rel), "title": artifact_title(rel),
             "file": str(path), "available": True, "exists": False,
             "bytes": 0, "isText": True}
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
    if not entry.get("isText", True):
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
                            "costDisplay", "costSource", "byPhase", "byApproach")


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


def _cost_buckets(raw, numeric_keys: bool = False) -> list[dict]:
    """`byPhase`/`byApproach` as an ordered list of shaped buckets. Phase order
    is the engine's own insertion order; approaches are sorted numerically
    (`"10"` after `"2"`), falling back to string order for junk keys."""
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
    buckets = [total, *by_phase, *by_approach]
    notices = []
    # Task 049's anomaly is worded ONCE (COST_ZERO_QUOTE_NOTICE) and named here
    # whenever any bucket carries it -- a breakdown full of `unavailable` rows
    # otherwise leaves the operator guessing why a route that answered every
    # request priced none of it.
    if any(is_zero_quote(b) for b in [usage, *_raw_buckets(usage)]):
        notices.append(COST_ZERO_QUOTE_NOTICE)
    out = {
        "hasUsage": bool(total["tokens"] or total["costDisplay"]
                         or by_phase or by_approach),
        "total": total,
        "byPhase": by_phase,
        "byApproach": by_approach,
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
    for key in ("byPhase", "byApproach"):
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
    for label, key in (("by phase:", "byPhase"), ("by approach:", "byApproach")):
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

    # -- status ----------------------------------------------------------
    def read_status(self) -> dict:
        return read_json(self.status_file, {})

    def update_status(self, **patch: Any) -> dict:
        status = self.read_status()
        status.update(patch)
        status["updatedAt"] = utcnow()
        atomic_write_json(self.status_file, status)
        return status

    def read_tasks(self) -> dict:
        """The tasks.json payload, via the hardened read path (task 002/#15):
        a mid-write file yields the last-good payload, never `{}`."""
        return self.read_tasks_result().doc

    def read_tasks_result(self) -> TasksRead:
        """Full three-way `TasksRead` (absent / file / last-good / unreadable)
        for callers that must render the stale flag -- `GET /tasks`, the
        `/status` task counts, `ralphctl tasks`, the hub table."""
        return read_tasks_doc(self.root)

    # -- vigilant-mode verification tracking (task 052) -------------------
    # A record, engine-owned and never touched by the agent (unlike
    # tasks.json), of which task ids have already received a passing
    # ("taskVerified") verify iteration under vigilant mode. This is what
    # lets a resumed engine tell "this task is still 'completed' because it
    # was already verified" apart from "this task is still 'completed'
    # because the prior process crashed between the worker iteration that
    # completed it and the verify iteration that was supposed to check it"
    # -- a per-process before/after tasks.json diff can't distinguish those
    # two cases across a crash/resume boundary, since both leave the task
    # showing status "completed" in the very first snapshot the new process
    # ever reads.
    @property
    def vigilant_verified_file(self) -> Path:
        return self.root / "vigilant-verified.json"

    def read_verified_tasks(self) -> set[str]:
        return set(read_json(self.vigilant_verified_file, []))

    def mark_task_verified(self, task_id: str) -> None:
        verified = self.read_verified_tasks()
        verified.add(task_id)
        atomic_write_json(self.vigilant_verified_file, sorted(verified))

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
        that slot's number is reused and its files overwritten by the
        next attempt.
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
        consumed = read_json(self.consumed_marker(), [])
        consumed.extend(p.name for p in files)
        atomic_write_json(self.consumed_marker(), consumed)
        for p in files:
            self.emit("steering.consumed", file=p.name, iteration=iteration)
