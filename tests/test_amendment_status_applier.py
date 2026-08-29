"""Task 029 (#26, requirement E): the single engine-side task-status
applier for an operator's typed status ruling --
`engine.state.apply_amendment_status_rulings`, and the `write_amendment`/
`amendment_entries` extensions (`AMENDMENT_MODE_STATUS`) that give it
something to read.

Everything here is pure tasks.json bookkeeping plus the read-only
`amendments/` channel `test_operator_ruling.py` already exercises for the
waive/replace kinds -- no subprocess, no `pi` stub, mirroring
`test_criteria_fingerprint.py`'s own discipline for the sibling mechanism
this module must NOT trip (`criteriaEditedAfterValidationFailure`).
"""

from __future__ import annotations

import json
from pathlib import Path

from ralphd.engine.config import JobConfig
from ralphd.engine.loop import LoopSupervisor
from ralphd.engine.state import (
    AMENDMENT_MODE_STATUS,
    RunDir,
    amendment_entries,
    apply_amendment_status_rulings,
    write_amendment,
)


def _run(tmp_path: Path) -> RunDir:
    return RunDir(root=tmp_path / "run")


def _write_tasks(run: RunDir, tasks: list[dict]) -> None:
    run.tasks_file.write_text(json.dumps({"version": 1, "tasks": tasks}))


def _events(run: RunDir) -> list[dict]:
    path = run.root / "events.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


# -- write_amendment(status=...) / amendment_entries round trip -------------

def test_write_amendment_status_round_trips_through_amendment_entries(tmp_path):
    cdir = tmp_path / "config"
    fname = write_amendment(cdir, "007", "operator says skip it",
                            status="skipped")
    entries = amendment_entries(cdir)
    assert len(entries) == 1
    e = entries[0]
    assert e["file"] == fname
    assert e["taskId"] == "007"
    assert e["mode"] == AMENDMENT_MODE_STATUS
    assert e["status"] == "skipped"
    assert e["waive"] is False
    assert e["text"] == "operator says skip it"


def test_waive_and_replace_entries_carry_no_status(tmp_path):
    """A pre-existing waive/replace ruling must keep reading exactly as it
    always did -- `status` is simply None, never a KeyError or a spurious
    value, for either older mode."""
    cdir = tmp_path / "config"
    write_amendment(cdir, "001", "waived", waive=True)
    write_amendment(cdir, "002", "replaced")
    entries = amendment_entries(cdir)
    assert entries[0]["mode"] == "waive"
    assert entries[0]["status"] is None
    assert entries[1]["mode"] == "replace"
    assert entries[1]["status"] is None


# -- apply_amendment_status_rulings: the applier itself ----------------------

def test_legal_ruling_is_applied(tmp_path):
    run = _run(tmp_path)
    _write_tasks(run, [
        {"id": "010", "title": "t", "status": "validation-failed",
         "successCriteria": "do X"},
    ])
    cdir = tmp_path / "config"
    write_amendment(cdir, "010", "cannot be built in this sandbox",
                    status="skipped")

    results = apply_amendment_status_rulings(run, cdir, vigilant=False)

    assert len(results) == 1
    assert results[0]["applied"] is True
    assert results[0]["previous"] == "validation-failed"
    assert results[0]["status"] == "skipped"
    assert results[0]["reason"] == ""

    task = run.read_tasks()["tasks"][0]
    assert task["status"] == "skipped"


def test_illegal_transition_is_refused_and_nothing_is_written(tmp_path):
    run = _run(tmp_path)
    _write_tasks(run, [
        {"id": "011", "title": "t", "status": "pending",
         "successCriteria": "do Y"},
    ])
    cdir = tmp_path / "config"
    write_amendment(cdir, "011", "jump straight to validated",
                    status="validated")

    results = apply_amendment_status_rulings(run, cdir, vigilant=False)

    assert len(results) == 1
    assert results[0]["applied"] is False
    assert results[0]["reason"] == "illegal transition"

    task = run.read_tasks()["tasks"][0]
    # refused, so the status on disk is untouched
    assert task["status"] == "pending"
    assert _events(run) == []


def test_preserves_validation_attempts_tier_ladder_and_criteria_text(tmp_path):
    run = _run(tmp_path)
    _write_tasks(run, [
        {"id": "012", "title": "t", "status": "validation-failed",
         "successCriteria": "do Z exactly as written",
         "validationAttempts": 2, "tierLadderExhausted": True},
    ])
    cdir = tmp_path / "config"
    write_amendment(cdir, "012", "waiving after the fact", status="skipped")

    apply_amendment_status_rulings(run, cdir, vigilant=False)

    task = run.read_tasks()["tasks"][0]
    assert task["status"] == "skipped"
    assert task["validationAttempts"] == 2
    assert task["tierLadderExhausted"] is True
    assert task["successCriteria"] == "do Z exactly as written"


def test_audit_event_names_the_operator_as_source_like_budget_changed(tmp_path):
    run = _run(tmp_path)
    _write_tasks(run, [
        {"id": "013", "title": "t", "status": "in-progress",
         "successCriteria": "do W"},
    ])
    cdir = tmp_path / "config"
    write_amendment(cdir, "013", "operator says done", status="completed")

    apply_amendment_status_rulings(run, cdir, vigilant=False)

    events = [e for e in _events(run) if e["type"] == "amendment_status_applied"]
    assert len(events) == 1
    ev = events[0]
    assert ev["source"] == "operator"
    assert ev["taskId"] == "013"
    assert ev["previous"] == "in-progress"
    assert ev["status"] == "completed"


def test_noop_when_already_at_the_ruled_status_no_double_write_or_event(tmp_path):
    """The applier is safe to call every time a run loads (PRD: 'applied by
    the engine at load time') -- a second call after the ruling has already
    taken effect must change nothing further and emit nothing further."""
    run = _run(tmp_path)
    _write_tasks(run, [
        {"id": "014", "title": "t", "status": "validation-failed",
         "successCriteria": "do V"},
    ])
    cdir = tmp_path / "config"
    write_amendment(cdir, "014", "skip it", status="skipped")

    first = apply_amendment_status_rulings(run, cdir, vigilant=False)
    assert first[0]["applied"] is True

    second = apply_amendment_status_rulings(run, cdir, vigilant=False)
    assert second[0]["applied"] is False
    assert second[0]["reason"] == "already at that status"

    events = [e for e in _events(run) if e["type"] == "amendment_status_applied"]
    assert len(events) == 1  # not two


def test_unknown_task_id_is_reported_not_applied(tmp_path):
    run = _run(tmp_path)
    _write_tasks(run, [
        {"id": "015", "title": "t", "status": "pending"},
    ])
    cdir = tmp_path / "config"
    write_amendment(cdir, "999", "no such task", status="skipped")

    results = apply_amendment_status_rulings(run, cdir, vigilant=False)
    assert results[0]["applied"] is False
    assert results[0]["reason"] == "no matching task"


def test_waive_and_replace_rulings_are_never_treated_as_status_moves(tmp_path):
    """A pre-existing waive/replace ruling must never be mistaken for a
    status ruling just because it names a real task."""
    run = _run(tmp_path)
    _write_tasks(run, [
        {"id": "016", "title": "t", "status": "in-progress"},
    ])
    cdir = tmp_path / "config"
    write_amendment(cdir, "016", "waived at review time", waive=True)

    results = apply_amendment_status_rulings(run, cdir, vigilant=False)
    assert results == []
    task = run.read_tasks()["tasks"][0]
    assert task["status"] == "in-progress"


def test_vigilant_delta_governs_which_transitions_are_legal(tmp_path):
    """awaiting-validation -> validated is only a declared edge under the
    vigilant table variant (`TASK_STATUS_VIGILANT_DELTA`) -- the applier
    must consult the SAME variant-selection every other status move does,
    not a hand-picked copy of the base table alone."""
    run = _run(tmp_path)
    _write_tasks(run, [
        {"id": "017", "title": "t", "status": "awaiting-validation"},
    ])
    cdir = tmp_path / "config"
    write_amendment(cdir, "017", "operator confirms", status="validated")

    non_vigilant = apply_amendment_status_rulings(run, cdir, vigilant=False)
    assert non_vigilant[0]["applied"] is False
    assert non_vigilant[0]["reason"] == "illegal transition"

    vigilant = apply_amendment_status_rulings(run, cdir, vigilant=True)
    assert vigilant[0]["applied"] is True
    task = run.read_tasks()["tasks"][0]
    assert task["status"] == "validated"


# -- criteriaEditedAfterValidationFailure must not confuse a ruling with a --
# -- worker's own rewrite of the bar it just failed -------------------------

def _supervisor(tmp_path: Path) -> LoopSupervisor:
    run = RunDir(root=tmp_path / "run")
    cfg = JobConfig(run_id="unit")
    return LoopSupervisor(cfg, run, tmp_path / "ws")


def test_status_ruling_does_not_fire_the_criteria_edited_flag(tmp_path):
    sup = _supervisor(tmp_path)
    _write_tasks(sup.run, [
        {"id": "020", "title": "t", "status": "validation-failed",
         "successCriteria": "do X", "validationAttempts": 1},
    ])
    sup._ensure_criteria_baseline()

    cdir = tmp_path / "config"
    write_amendment(cdir, "020", "operator waives it", status="skipped")
    apply_amendment_status_rulings(sup.run, cdir, vigilant=False)

    sup._check_criteria_edits(sup.run.read_tasks())
    on_disk = sup.run.read_tasks()["tasks"][0]
    assert on_disk["status"] == "skipped"
    assert "criteriaEditedAfterValidationFailure" not in on_disk


def test_a_real_criteria_rewrite_still_fires_the_flag(tmp_path):
    """Positive control for the test above: the SAME task, but the actual
    successCriteria text is rewritten (not a status ruling) after a
    validation failure -- the flag must still fire, exactly as task 008
    already proves for the non-ruling case."""
    sup = _supervisor(tmp_path)
    _write_tasks(sup.run, [
        {"id": "021", "title": "t", "status": "validation-failed",
         "successCriteria": "do X", "validationAttempts": 1},
    ])
    sup._ensure_criteria_baseline()

    tasks_data = sup.run.read_tasks()
    tasks_data["tasks"][0]["successCriteria"] = "do X (rewritten by worker)"
    tasks_data["tasks"][0]["status"] = "completed"
    sup.run.tasks_file.write_text(json.dumps(tasks_data))

    sup._check_criteria_edits(sup.run.read_tasks())
    on_disk = sup.run.read_tasks()["tasks"][0]
    assert on_disk["criteriaEditedAfterValidationFailure"] is True
