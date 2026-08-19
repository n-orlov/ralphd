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
        return read_json(self.tasks_file, {})

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
