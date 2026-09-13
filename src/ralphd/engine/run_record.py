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


def _metadata_channel(run: "state.RunDir", approach: int,
                      channel: str) -> dict | None:
    """Task 035 (requirement B.1, cure for task 006): one judge channel's
    verdict as the ENGINE recorded it when that judge ran
    (`state.approach_metadata_channel` <- `LoopSupervisor.
    _record_approach_verdict`), shaped exactly like `_findings_channel`'s
    return above plus `source`/`recordedAt`/`findingsChannel`, so a reader of
    the record does not have to know which of the two origins answered.

    `None` when that judge never settled a verdict for this approach -- the
    caller then falls back to the judge's optional sidecar, which is all a run
    from before this task ever had.
    """
    entry = state.approach_metadata_channel(run.root, approach, channel)
    if entry is None:
        return None
    return {
        "verdict": entry.get("verdict"),
        "blocking": entry.get("blocking") or [],
        "residual": entry.get("residual") or [],
        "reason": entry.get("reason"),
        "recordedAt": entry.get("recordedAt"),
        "findingsChannel": bool(entry.get("findingsChannel")),
        "source": "engine-metadata",
    }


def _channel(run: "state.RunDir", approach: int, channel: str,
             sidecar: Path) -> dict | None:
    """The verdict for one judge channel of one approach: the engine's own
    metadata first (authoritative -- written when the judge ran, task 035),
    the judge's optional findings sidecar only as a FALLBACK for an approach
    that has no metadata entry (every approach judged before this task
    existed). A sidecar-sourced channel is labelled `source:
    "findings-sidecar"` so the two can never be mistaken for each other.
    """
    from_meta = _metadata_channel(run, approach, channel)
    if from_meta is not None:
        return from_meta
    from_sidecar = _findings_channel(sidecar)
    if from_sidecar is None:
        return None
    return {**from_sidecar, "source": "findings-sidecar"}


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
        "review": _channel(run, approach, "review", review_json),
        "verdictGate": _channel(run, approach, "verdictGate", gate_json),
    }


def build_approach_history(run: "state.RunDir") -> list[dict]:
    """Every approach this run has planned, oldest first: each archived
    approach (`approaches/NN/`) plus, when the current approach was never
    archived (the run succeeded, or is still open), one more live entry
    read off the run root -- never both for the same approach number.

    Task 035 (requirement B.1): an approach the engine recorded a verdict for
    (`state.read_approach_metadata`) is listed EVEN IF it was neither archived
    nor the live approach -- a judged approach can never fall out of the
    history because its archive dir is missing. Both the live and the archived
    entries take their verdicts from that same metadata (`_channel`).
    """
    archived_numbers = state.archived_approach_numbers(run.root)
    current = run.read_status().get("approach") or 1
    judged = []
    for key in state.read_approach_metadata(run.root)["approaches"]:
        try:
            judged.append(int(key))
        except (TypeError, ValueError):
            continue
    numbers = sorted({*archived_numbers, *judged, current})
    return [
        _approach_history_entry(
            run, n,
            state.archived_approach_dir(run.root, n) if n in archived_numbers
            else run.root,
            archived=n in archived_numbers)
        for n in numbers
    ]


# ---------------------------------------------------------------------------
# Petitions + adjudications (task 011, requirement C.3, #114): read back
# ONLY from `petition_adjudications_file` -- the run-scope, tamper-checked
# record `petitions.record_confirmed_adjudications` is the sole writer of
# (task 016: it holds REFUTED entries too, so this render needs no second
# writer).
# ---------------------------------------------------------------------------

def build_petitions_section(run: "state.RunDir") -> dict:
    """Every adjudicated petition, CONFIRMED and REFUTED alike, with its
    adjudication value and -- for a REFUTED one -- review's remedy text
    (review finding 2; approach 02 task 016, cure for task 002).

    The read is `petitions.read_adjudications`: the ONE run-scope,
    tamper/MAC-checked store `petitions.record_confirmed_adjudications`
    is the sole writer of, which since task 016 persists EVERY adjudicated
    entry it is handed -- REFUTED included, under the same keyed MAC. That
    store, not a caller-side side effect, is why a REFUTED obligation and
    its remedy reach `run-record.json`/`run-record.md`: task 002's fix
    read the REFUTED entries out of the `PETITION_REFUTED_EVENT` witness
    log instead, so the render only worked when the caller ALSO happened
    to call `petitions.record_refuted_adjudications`.

    Exemptions are untouched by this: `read_adjudications` is a RENDER
    read, and `petitions.read_confirmed_adjudications` (the exemption
    read, used by `confirmed_obligations` and everything downstream of it)
    filters the identical authenticated record down to CONFIRMED entries,
    so a REFUTED entry can never exempt an obligation. The `tampered`
    flag, and the whole MAC/witness path behind it, are shared verbatim.

    The `PETITION_REFUTED_EVENT` witness log is still merged in
    afterwards, for a REFUTED adjudication that reached only that channel
    (`record_refuted_adjudications` called on its own -- e.g. an entry
    adjudicated by an engine episode older than task 016, or a record
    destroyed/unauthenticatable so the store reads empty). An obligation
    present in both channels is rendered ONCE, from the store -- the
    authenticated record wins.
    """
    entries, tampered = petitions_mod.read_adjudications(run)
    stored_keys = {
        petitions_mod._normalize_obligation(e["obligation"]) for e in entries
        if isinstance(e, dict) and isinstance(e.get("obligation"), str)}
    refuted: dict[str, dict] = {}
    for ev in state.read_events(
            run.root, types=[petitions_mod.PETITION_REFUTED_EVENT]):
        obligation = ev.get("obligation")
        if not isinstance(obligation, str) or not obligation.strip():
            continue
        key = petitions_mod._normalize_obligation(obligation)
        if key in stored_keys:
            continue
        remedy = ev.get("remedy")
        refuted[key] = {
            "obligation": obligation.strip(),
            "adjudication": "REFUTED",
            "remedy": remedy.strip() if isinstance(remedy, str) else "",
        }
    merged = list(entries) + sorted(refuted.values(),
                                    key=lambda e: e["obligation"])
    return {"entries": merged, "tampered": tampered}


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
# correct and empty until it lands). A FAILED send (task 009, PRD
# requirement B.1, #197) never emits `notification.sent` -- it emits the
# distinct `notification.failed` event below instead, so a reader grepping
# the `sent` name for "was the operator told" gets a true answer -- and
# this section stays exactly the sends that actually happened.
# ---------------------------------------------------------------------------

NOTIFICATION_SENT_EVENT = "notification.sent"
NOTIFICATION_FAILED_EVENT = "notification.failed"

# Task 011 (PRD requirement B.3, #197): emitted at most once, at engine
# startup (`LoopSupervisor.__init__`, never inside `_run_job_core`, same
# "once per process start" placement E.3's `workspace_version_mismatch`
# already uses), when the `notify` hat tool is wired for this run -- it
# always is, both hats carry it unconditionally -- but no `notifier_url`
# was configured. A distinct event name from `notification.*`: this is
# never itself a send attempt (no `endpoint`/`textSha256`/`httpStatus`),
# it is the engine saying, once, that every `notify` call this run makes
# is a guaranteed no-op until one of `--notifier-url`/the `job.yaml` key/
# `RALPHD_NOTIFIER_URL` supplies a URL.
NOTIFIER_UNCONFIGURED_EVENT = "notifier.unconfigured"


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
# The debt ledger (task 006, requirement A.3, #155): open and resolved,
# split the same way `state.render_debt_markdown` splits `debt.md` -- the
# run record renders the SAME ledger, through the SAME read
# (`state.read_debt_entries`), never a second parse of `debt.json`.
# ---------------------------------------------------------------------------

def build_debt_section(run: "state.RunDir") -> dict:
    entries = state.read_debt_entries(run.root)
    open_entries = [e for e in entries if e.get("status") != state.DEBT_STATUS_RESOLVED]
    resolved_entries = [e for e in entries if e.get("status") == state.DEBT_STATUS_RESOLVED]
    return {"open": open_entries, "resolved": resolved_entries}


# ---------------------------------------------------------------------------
# The whole record.
# ---------------------------------------------------------------------------

# Task 035 (requirement B.1, cure for task 006): every top-level key
# `build_run_record` writes, and which markdown section renders it. This is
# not decoration -- `render_run_record_markdown` walks it, and
# `tests/test_run_record.py` asserts the record's
# own key set equals this mapping's, so a field added to the JSON without a
# renderer for it fails a test instead of quietly going unrendered (which is
# exactly how `usage`'s input/output token fields and its byPhase/byApproach/
# byTier breakdowns went missing from `run-record.md`).
RECORD_SECTIONS: dict[str, str] = {
    "runId": "header",
    "generatedAt": "header",
    "state": "header",
    "verdict": "header",
    "reason": "header",
    "approach": "header",
    "maxApproaches": "header",
    "startedAt": "header",
    "endedAt": "header",
    "iterationsUsed": "header",
    "usage": "Usage",
    "approaches": "Approach history",
    "petitions": "Petitions",
    "steersConsumed": "Steers consumed",
    "operatorRulings": "Operator rulings",
    "notifications": "Notifications",
    "debt": "Debt",
}


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
        "debt": build_debt_section(run),
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
        return [f"- **{name}**: no verdict recorded"]
    if "error" in channel:
        return [f"- **{name}**: {channel['error']}"]
    # Task 035 (requirement B.1): say WHERE this verdict came from (the
    # engine's own approach metadata, or the judge's optional findings
    # sidecar), whether a findings channel backed it at all, and the reason
    # the judge gave -- the metadata carries all three and the markdown used
    # to render none of them.
    lines = [f"- **{name}**: verdict `{channel.get('verdict')}`"
             + (f" (source: {channel['source']})" if channel.get("source") else "")]
    if channel.get("reason"):
        lines.append(f"  - reason: {channel['reason']}")
    if channel.get("recordedAt"):
        lines.append(f"  - recorded: {channel['recordedAt']}")
    if "findingsChannel" in channel:
        lines.append(
            "  - structured findings channel: "
            + ("present" if channel.get("findingsChannel") else
               "none (verdict recorded by the engine regardless)"))
    blocking = channel.get("blocking") or []
    residual = channel.get("residual") or []
    lines.append(f"  - blocking: {len(blocking)}, residual: {len(residual)}")
    for f in blocking:
        lines.append(_finding_line("blocking", f))
    for f in residual:
        lines.append(_finding_line("residual", f))
    return lines


def _usage_bucket_lines(label: str, buckets: dict | None) -> list[str]:
    """Task 035 (requirement B.1): one of `usage`'s three breakdowns
    (`byPhase`/`byApproach`/`byTier`, written by `LoopSupervisor.
    _accumulate_usage`) rendered as a labelled block -- `(none)` when the run
    never accumulated that bucket, so the section is always present and a
    reader can tell "nothing spent here" from "this record does not say".
    Bucket keys are rendered through `state.cost_bucket`'s own display
    strings, the same wording `ralphctl cost` prints.
    """
    lines = [f"- {label}:"]
    if not isinstance(buckets, dict) or not buckets:
        lines.append("  - (none)")
        return lines
    for key in sorted(buckets, key=lambda k: str(k)):
        bucket = state.cost_bucket(buckets.get(key), str(key))
        lines.append(
            f"  - `{key}`: {bucket.get('costDisplay')}"
            f", {bucket.get('tokensDisplay')}")
    return lines


def _usage_lines(usage: dict) -> list[str]:
    """The whole `usage` section: the per-direction token counters
    (`input`/`output`/`cacheRead`/`cacheWrite`/`totalTokens` --
    `state.COST_TOKEN_KEYS`), the money fields and cost status, then all three
    breakdowns. Task 035 (requirement B.1): task 006's render stopped at
    `totalTokens`/`costUSD`, which is the gap this restores.
    """
    lines = ["", "## Usage"]
    for key in state.COST_TOKEN_KEYS:
        lines.append(f"- {key}: {usage.get(key)}")
    for key in ("costUSD", "costDerivedUSD", "costStatus", "costFree"):
        lines.append(f"- {key}: {usage.get(key)}")
    lines.append(f"- tokens: {state.format_tokens(usage) if usage else state.USAGE_NONE}")
    lines.append(f"- cost: {state.format_cost(usage, decimals=4)}")
    lines += _usage_bucket_lines("byPhase", usage.get("byPhase"))
    lines += _usage_bucket_lines("byApproach", usage.get("byApproach"))
    lines += _usage_bucket_lines("byTier", usage.get("byTier"))
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
    lines += _usage_lines(record.get("usage") or {})

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
        line = f"- {e.get('obligation')} -- {e.get('adjudication')}"
        remedy = e.get("remedy")
        if isinstance(remedy, str) and remedy.strip():
            line += f" (remedy: {remedy.strip()})"
        lines.append(line)

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

    lines += ["", "## Debt"]
    debt = record.get("debt") or {}
    open_entries = debt.get("open") or []
    resolved_entries = debt.get("resolved") or []
    lines.append(f"- open: {len(open_entries)}, resolved: {len(resolved_entries)}")
    for e in open_entries:
        lines.append(state.debt_entry_line(e))
    for e in resolved_entries:
        lines.append(state.debt_entry_line(e))

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
