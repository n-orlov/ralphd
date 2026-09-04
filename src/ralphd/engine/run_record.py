"""Task 006 (PRD requirement B.1, #140): the harness's own terminal record.

`run-record.md` + `run-record.json` are written by the engine into the
run's `artifacts/` at every entry to `finishing` and rewritten on every
later terminal transition (`LoopSupervisor._write_run_record`, called from
`_finish()`/`_promote_finishing_state()`/`_promote_recorded_finishing_
state()` in `engine/loop.py`). Nothing here is written by a hat's own
process -- this module only ever reads engine-owned files
(`status.json`, `petition-adjudications.json`, the archived `approaches/
NN/` findings sidecars, the steering ledger, `CONFIG_DIR/amendments/`)
and renders them; a closing report is never faulted for omitting or
predating any of this (PRD requirement B.2 -- this is the engine's own
account of the run, not review's).

Two public entry points:

  `build_run_record(run, run_id=..., max_approaches=..., config_root=...)`
      -> the JSON-shaped dict (also what `run-record.json` holds).
  `render_run_record_markdown(record)`
      -> the same record, rendered for a human.

`write_run_record(...)` calls both and atomically writes
`artifacts/run-record.json` + `artifacts/run-record.md`, returning the
record dict (handy for a caller/test that wants to assert on it without a
second read-back).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from . import findings as findings_mod
from . import petitions as petitions_mod
from . import state

# Canonical in `engine.state` (task 007, B.1) -- that module's `RUN_DOCUMENTS`/
# `ARTIFACT_ALIASES` give both files a well-known key on `ralphctl docs`/
# `ralphctl artifacts` and the hub's panels, and importing them back here
# rather than redeclaring means this writer and those listings can never name
# the file two different ways.
RUN_RECORD_JSON_FILE = state.RUN_RECORD_JSON_FILE
RUN_RECORD_MD_FILE = state.RUN_RECORD_MD_FILE


# ---------------------------------------------------------------------------
# Approach history -- one entry per approach the run ever planned, archived
# ones from `approaches/NN/` (task 020, #24) plus, when the run's final
# approach never got archived (the ordinary success path never calls
# `_archive_approach` -- see `LoopSupervisor._step_review`), one more entry
# read live off the run root.
# ---------------------------------------------------------------------------

def _findings_channel(json_path: Path) -> dict | None:
    """One judge's structured findings for one approach, or `None` when
    that channel was never written for this approach (a run that never
    rejected, or a gate that never ran) -- never a fabricated empty list,
    so a caller can tell "no verdict from this channel" apart from
    "verified with zero findings"."""
    if not json_path.exists():
        return None
    try:
        items = findings_mod.read_findings_json(json_path)
    except ValueError as exc:
        return {"error": f"unreadable findings channel: {exc}"}
    return {
        "verdict": findings_mod.compute_verdict(items),
        "blocking": [f for f in items if f.get("severity") == "blocking"],
        "residual": [f for f in items if f.get("severity") == "residual"],
    }


def _approach_history_entry(run: "state.RunDir", approach: int, adir: Path,
                            archived: bool) -> dict:
    if archived:
        summary = state.archived_approach_summary(run.root, approach)
        counts = (summary or {}).get("counts") or {}
        review_json = adir / state.FINDINGS_JSON_FILE
        gate_json = adir / state.VERDICT_GATE_FINDINGS_JSON_FILE
    else:
        counts = state.task_counts(run.read_tasks().get("tasks", []))
        review_json = run.findings_json_file
        gate_json = run.verdict_gate_findings_json_file
    try:
        rel_dir = str(adir.relative_to(run.root))
    except ValueError:
        rel_dir = str(adir)
    return {
        "approach": approach,
        "archivedDir": rel_dir if archived else None,
        "archived": archived,
        "taskTally": counts,
        "review": _findings_channel(review_json),
        "verdictGate": _findings_channel(gate_json),
    }


def build_approach_history(run: "state.RunDir") -> list[dict]:
    """Every approach this run has planned, oldest first: each archived
    approach (`approaches/NN/`) plus, when the current approach was never
    archived (the run succeeded, or is still open), one more live entry
    read off the run root -- never both for the same approach number."""
    archived_numbers = state.archived_approach_numbers(run.root)
    history = [
        _approach_history_entry(
            run, n, state.archived_approach_dir(run.root, n), archived=True)
        for n in archived_numbers
    ]
    current = run.read_status().get("approach") or 1
    if current not in archived_numbers:
        history.append(_approach_history_entry(run, current, run.root,
                                                archived=False))
    return history


# ---------------------------------------------------------------------------
# Petitions + adjudications (task 011, requirement C.3, #114): read back
# ONLY from `petition_adjudications_file` -- the run-scope, tamper-checked
# record `petitions.record_confirmed_adjudications` is the sole writer of.
# ---------------------------------------------------------------------------

def build_petitions_section(run: "state.RunDir") -> dict:
    entries, tampered = petitions_mod.read_confirmed_adjudications(run)
    return {"entries": entries, "tampered": tampered}


# ---------------------------------------------------------------------------
# Steers consumed (task 016/#17 for the file shape; `consumed` here is the
# same "applied" fact `steering_entries` already reports).
# ---------------------------------------------------------------------------

def build_steers_section(run: "state.RunDir") -> list[dict]:
    return [e for e in run.steering_entries(bodies=False) if e.get("consumed")]


# ---------------------------------------------------------------------------
# Operator rulings found under `<config_root>/amendments/` -- the same
# unforgeable, host `:ro`-mounted channel `LoopSupervisor.
# _operator_amendment_review_context` reads (task 027/#26).
# ---------------------------------------------------------------------------

def build_operator_rulings_section(config_root: Path) -> list[dict]:
    return state.amendment_entries(config_root)


# ---------------------------------------------------------------------------
# Notifications (PRD requirement B.3): every `notification.sent` event this
# run ever emitted, oldest first -- possibly empty (the `notify` hat tool
# is B.3's own, separately-tasked, cuttable deliverable; this section is
# correct and empty until it lands).
# ---------------------------------------------------------------------------

NOTIFICATION_SENT_EVENT = "notification.sent"


def build_notifications_section(run: "state.RunDir") -> list[dict]:
    return state.read_events(run.root, types=[NOTIFICATION_SENT_EVENT])


# ---------------------------------------------------------------------------
# Usage/cost + iteration figures -- lifted straight off status.json, never
# recomputed: `LoopSupervisor._accumulate_usage` is the one place that
# actually adds anything up.
# ---------------------------------------------------------------------------

def build_usage_section(status: dict) -> dict:
    return {
        "iterationsUsed": status.get("iterationsUsed"),
        "usage": status.get("usage") or {},
    }


# ---------------------------------------------------------------------------
# The whole record.
# ---------------------------------------------------------------------------

def build_run_record(run: "state.RunDir", *, run_id: str,
                     max_approaches: int | None,
                     config_root: Path) -> dict[str, Any]:
    status = run.read_status()
    record: dict[str, Any] = {
        "runId": run_id,
        "generatedAt": state.utcnow(),
        "state": status.get("state"),
        "verdict": status.get("verdict"),
        "reason": status.get("reason"),
        "approach": status.get("approach"),
        "maxApproaches": max_approaches,
        "startedAt": status.get("startedAt"),
        "endedAt": status.get("endedAt"),
        "approaches": build_approach_history(run),
        "petitions": build_petitions_section(run),
        "steersConsumed": build_steers_section(run),
        "operatorRulings": build_operator_rulings_section(config_root),
        "notifications": build_notifications_section(run),
    }
    record.update(build_usage_section(status))
    return record


# ---------------------------------------------------------------------------
# Markdown rendering -- the SAME record, for a human (PRD requirement B.1:
# "the markdown renders the same record for a human").
# ---------------------------------------------------------------------------

def _finding_line(kind: str, f: dict) -> str:
    clause = f.get("clause", "")
    evidence = f.get("evidence", "")
    curable = f.get("curable")
    curable_note = "" if curable is None else f" (curable: {curable})"
    return f"  - {kind}: {clause}{curable_note} -- {evidence}"


def _render_channel(name: str, channel: dict | None) -> list[str]:
    if channel is None:
        return [f"- **{name}**: no channel recorded"]
    if "error" in channel:
        return [f"- **{name}**: {channel['error']}"]
    lines = [f"- **{name}**: verdict `{channel.get('verdict')}`"]
    for f in channel.get("blocking") or []:
        lines.append(_finding_line("blocking", f))
    for f in channel.get("residual") or []:
        lines.append(_finding_line("residual", f))
    return lines


def render_run_record_markdown(record: dict) -> str:
    lines: list[str] = [
        f"# Run record: {record.get('runId')}",
        "",
        f"- state: `{record.get('state')}`",
        f"- verdict: `{record.get('verdict')}`",
        f"- reason: {record.get('reason') or '(none)'}",
        f"- approach: {record.get('approach')} / {record.get('maxApproaches')}",
        f"- started: {record.get('startedAt')}",
        f"- ended: {record.get('endedAt')}",
        f"- generated: {record.get('generatedAt')}",
        f"- iterationsUsed: {record.get('iterationsUsed')}",
    ]
    usage = record.get("usage") or {}
    lines += [
        "",
        "## Usage",
        f"- totalTokens: {usage.get('totalTokens')}",
        f"- costUSD: {usage.get('costUSD')}",
        f"- costDerivedUSD: {usage.get('costDerivedUSD')}",
        f"- costStatus: {usage.get('costStatus')}",
    ]

    lines += ["", "## Approach history"]
    for entry in record.get("approaches") or []:
        lines.append(
            f"\n### Approach {entry.get('approach')}"
            + (" (archived)" if entry.get("archived") else " (live)"))
        if entry.get("archivedDir"):
            lines.append(f"- dir: `{entry['archivedDir']}`")
        lines.append(f"- task tally: {entry.get('taskTally')}")
        lines += _render_channel("review", entry.get("review"))
        lines += _render_channel("verdict gate", entry.get("verdictGate"))

    lines += ["", "## Petitions"]
    petitions = record.get("petitions") or {}
    if petitions.get("tampered"):
        lines.append("- **record tampered -- read as empty**")
    entries = petitions.get("entries") or []
    if not entries:
        lines.append("- (none)")
    for e in entries:
        lines.append(f"- {e.get('obligation')} -- {e.get('adjudication')}")

    lines += ["", "## Steers consumed"]
    steers = record.get("steersConsumed") or []
    if not steers:
        lines.append("- (none)")
    for s in steers:
        lines.append(f"- `{s.get('file')}` ({s.get('name')}), {s.get('ts')}")

    lines += ["", "## Operator rulings"]
    rulings = record.get("operatorRulings") or []
    if not rulings:
        lines.append("- (none)")
    for r in rulings:
        lines.append(
            f"- `{r.get('file')}` task {r.get('taskId')} mode {r.get('mode')}"
            + (f" -> status {r.get('status')}" if r.get("status") else ""))

    lines += ["", "## Notifications"]
    notifications = record.get("notifications") or []
    if not notifications:
        lines.append("- (none)")
    for n in notifications:
        lines.append(
            f"- {n.get('ts')} {n.get('endpoint')} httpStatus={n.get('httpStatus')}")

    return "\n".join(lines) + "\n"


def write_run_record(run: "state.RunDir", *, run_id: str,
                     max_approaches: int | None,
                     config_root: Path) -> dict[str, Any]:
    """Build the record and write both files into `run.artifacts_dir`,
    returning the record dict. Called by `LoopSupervisor._write_run_record`
    at every entry to `finishing` and at every later terminal transition
    (task 006, PRD requirement B.1, #140) -- both writes go through this
    one function so `run-record.json` and `run-record.md` can never
    disagree about the record they render."""
    record = build_run_record(run, run_id=run_id, max_approaches=max_approaches,
                              config_root=config_root)
    run.artifacts_dir.mkdir(parents=True, exist_ok=True)
    state.atomic_write_json(run.artifacts_dir / RUN_RECORD_JSON_FILE, record)
    state.atomic_write(run.artifacts_dir / RUN_RECORD_MD_FILE,
                       render_run_record_markdown(record))
    return record
