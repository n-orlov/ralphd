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

from .redact import scrub_text


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
    composite = root / "composite-prd.md"
    if not original and composite.exists():
        return composite
    plain = root / "prd.md"
    return plain if plain.exists() else None


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
        return self.root / "prd.md"

    @property
    def composite_prd_file(self) -> Path:
        return self.root / "composite-prd.md"

    @property
    def notes_file(self) -> Path:
        return self.root / "notes.md"

    @property
    def findings_file(self) -> Path:
        return self.root / "review-findings.md"

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
        existing = sorted(self.steering_dir.glob("[0-9][0-9][0-9]-*.md"))
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
        return self.steering_dir / ".consumed.json"

    def pending_steering(self) -> list[Path]:
        consumed = set(read_json(self.consumed_marker(), []))
        return [p for p in sorted(self.steering_dir.glob("[0-9][0-9][0-9]-*.md"))
                if p.name not in consumed]

    def consume_steering(self, files: list[Path], iteration: int) -> None:
        consumed = read_json(self.consumed_marker(), [])
        consumed.extend(p.name for p in files)
        atomic_write_json(self.consumed_marker(), consumed)
        for p in files:
            self.emit("steering.consumed", file=p.name, iteration=iteration)
