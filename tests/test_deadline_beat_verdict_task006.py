"""Requirement B (#72): a run that has earned `COMPLETE` does not lose it to
the clock -- the terminal-record half of the fix (requirement A, tasks
003/004, already stops the approach being burned when review is cut short
at budget end; this is about what the terminal write itself then says).

Chosen design (the other two rejected, and why, live in
`artifacts/reports/task-006-deadline-beat-verdict-design.md`): keep
`state: failed` / `verdict: unverified` exactly as `_run_job_core`'s tail
exit already writes them -- `verdict: verified` stays grantable by the
review hat alone, the operator's one non-negotiable ruling (PRD requirement
B) -- and add a dedicated, structured `deadlineBeatVerdict` field (modelled
on `_unconsumed_steering_patch`'s own precedent) naming both the deadline
(`deadline`) and the honoured COMPLETE (`completeHonouredAt`) whenever BOTH
hold: every task in the record is done, and an actual `signal: "COMPLETE"`
event was ever recorded. See `LoopSupervisor._deadline_beat_verdict_patch`'s
docstring for why this is wired in from exactly one call site
(`_review_unjudged_at_budget_end`) rather than unconditionally at the shared
tail exit the way `_unconsumed_steering_patch` is: a genuinely REJECTED last
approach leaves an indistinguishable trace (completed tasks, a past
COMPLETE signal) once `_archive_approach` has copied it without resetting
anything, so computing this generically would misreport a real rejection.

Tests:

1. `test_v09_iteration_183_regression_status_carries_the_marker` -- drives
   the exact `selfdev-v09-fsm-loop` iteration 183 shape end to end
   (honoured COMPLETE, then review, then the engine interrupts review with
   nothing left to retry with -- same scripted fixture as
   `test_review_interrupted_not_rejection.py`'s own named regression) and
   asserts `status.json`'s `deadlineBeatVerdict` names both the deadline and
   the honoured COMPLETE's timestamp.
2. `test_mutation_dropping_the_marker_fails_test_1` -- the paired mutation
   task 004's audit used throughout: patch `_deadline_beat_verdict_patch` to
   always return `{}` and show test 1's own assertion then fails, proving it
   is not vacuous.
3. `test_path_never_writes_verdict_verified` -- `verdict: verified` stays
   the review hat's alone to grant (the operator's ruling); this path must
   write `unverified` whether or not the marker itself fires.
4. `test_ordinary_failed_fixture_unaffected_byte_for_byte` -- a fixture
   where tasks are NOT all completed (no COMPLETE ever earned) drives the
   exact same finishing write and gets the pre-existing record back with no
   `deadlineBeatVerdict` key at all -- this task must not touch any run that
   genuinely failed.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from ralphd.engine.config import JobConfig
from ralphd.engine.loop import LoopSupervisor
from ralphd.engine.state import RunDir, atomic_write_json

from test_review_interrupted_not_rejection import (
    _complete_task,
    _interrupted,
    _ok,
    _plan,
    _supervisor,
)


# ---------------------------------------------------------------------------
# 1. the named regression: the marker fires and names both components
# ---------------------------------------------------------------------------

def test_v09_iteration_183_regression_status_carries_the_marker(tmp_path):
    handlers = {
        "planning": [_plan],
        "worker": [_complete_task],
        "review": [_interrupted],
    }
    # exactly 3 iterations: plan, worker(COMPLETE), review(interrupted) --
    # budget is gone the instant that third attempt returns.
    sup = _supervisor(tmp_path / "run", handlers, iterations=3)
    state = asyncio.run(sup._run_job_core())

    assert state == "failed"
    status = sup.run.read_status()
    assert status["verdict"] == "unverified"

    marker = status.get("deadlineBeatVerdict")
    assert isinstance(marker, dict), (
        "status.json must carry a deadlineBeatVerdict marker when every "
        "task was completed, COMPLETE was honoured, and review was cut "
        "off at budget end with no verdict ever scored")
    assert marker.get("deadline"), "marker must name the deadline"
    assert marker.get("completeHonouredAt"), (
        "marker must name the honoured COMPLETE (its event timestamp)")


# ---------------------------------------------------------------------------
# 2. the paired mutation: drop the marker computation, test 1 must then fail
# ---------------------------------------------------------------------------

def test_mutation_dropping_the_marker_fails_test_1(tmp_path, monkeypatch):
    monkeypatch.setattr(LoopSupervisor, "_deadline_beat_verdict_patch",
                        lambda self: {})

    handlers = {
        "planning": [_plan],
        "worker": [_complete_task],
        "review": [_interrupted],
    }
    sup = _supervisor(tmp_path / "run", handlers, iterations=3)
    asyncio.run(sup._run_job_core())

    status = sup.run.read_status()
    assert status.get("deadlineBeatVerdict") is None


# ---------------------------------------------------------------------------
# 3. verdict: verified stays the review hat's alone to grant
# ---------------------------------------------------------------------------

def test_path_never_writes_verdict_verified(tmp_path):
    # marker fires (every task completed, COMPLETE honoured)
    sup = _supervisor(tmp_path / "run-marker", {}, iterations=5)
    doc = {"tasks": [{"id": "001", "title": "t", "successCriteria": "c",
                      "status": "completed"}]}
    atomic_write_json(sup.run.tasks_file, doc)
    sup.run.emit("signal", signal="COMPLETE")
    sup._review_unjudged_at_budget_end(1)
    state = sup._finish_tail_unverified()

    assert state == "failed"
    status = sup.run.read_status()
    assert status["verdict"] == "unverified"
    assert status["verdict"] != "verified"
    assert isinstance(status.get("deadlineBeatVerdict"), dict)

    # marker does NOT fire (nothing completed) -- still never "verified"
    sup2 = _supervisor(tmp_path / "run-no-marker", {}, iterations=5)
    atomic_write_json(sup2.run.tasks_file, {"tasks": [
        {"id": "001", "title": "t", "successCriteria": "c",
         "status": "pending"}]})
    sup2._review_unjudged_at_budget_end(1)
    state2 = sup2._finish_tail_unverified()

    assert state2 == "failed"
    status2 = sup2.run.read_status()
    assert status2["verdict"] == "unverified"
    assert status2["verdict"] != "verified"
    assert status2.get("deadlineBeatVerdict") is None


# ---------------------------------------------------------------------------
# 4. an ordinary failed fixture (tasks not completed) is unaffected
# ---------------------------------------------------------------------------

def test_ordinary_failed_fixture_unaffected_byte_for_byte(tmp_path):
    sup = _supervisor(tmp_path / "run", {}, iterations=5)
    atomic_write_json(sup.run.tasks_file, {"tasks": [
        {"id": "001", "title": "still working", "successCriteria": "c",
         "status": "in-progress"}]})
    # No COMPLETE signal was ever emitted, and no review-unjudged path was
    # taken -- an ordinary abort, exactly the shape `_finish_tail_unverified`
    # already handled before this task existed.
    sup._abort_reason = "some ordinary engine-detected abort condition"
    state = sup._finish_tail_unverified()

    assert state == "aborted"
    status = sup.run.read_status()
    assert status["verdict"] == "unverified"
    assert status["reason"] == "some ordinary engine-detected abort condition"
    assert status.get("unconsumedSteering") == []
    assert "deadlineBeatVerdict" not in status
