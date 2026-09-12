"""Task 006 (v0.20.1 PRD requirement B.4): task 004 deleted the legacy
absorption-streak forced-verify dispatch (`_record_task_absorption_streak`,
`TASK_ABSORPTION_STATUS_KEY`). Requirement B.4's own letter is explicit
that removing it "must not remove" the ORDINARY three-strike stagnation
witness (`tests/test_stagnation_progress_witnesses_task010.py`'s own
subject, folded into `tests/test_worker_hat.py`) for a claim the engine
cannot corroborate against a real workspace delta.

Task 013's unit-level node
(`tests/test_progress_budget_task013.py::test_an_uncorroborated_no_verdict_
still_trips_the_three_strike_guard`) already pins this shape at the
`_step_worker()` call-site level. This module drives the SAME shape
through the full production pipeline (`_run_job_core()`, a real vigilant
last-approach pause) the way task 005's own new module drove requirement
B.3, to pin it explicitly for THIS requirement/PRD.
"""
from __future__ import annotations

import asyncio
import json

from ralphd.engine.runner import IterationResult
from ralphd.engine.state import RunDir

from tests.test_worker_hat import (
    _drive_until_paused,
    _fv_events,
    _fv_log_messages,
    _fv_ok,
    _fv_task,
    _init_git_workspace,
    _spw010_supervisor,
    _worker_iteration_count,
    _write_plan,
)


def _uncorroborated_commit_claim(sup, prompt: str = "") -> IterationResult:
    """Claims `commit` progress evidence but touches NOTHING in the
    workspace -- `_apply_worker_progress_report` reads this as
    uncorroborated every time (no fingerprint delta, and the claim is not
    external-only either), the exact shape task 013's own unit-level node
    already established for this case."""
    payload = {"verdict": "no_verdict", "progress": {
        "summary": "checked, nothing to touch yet",
        "evidence": [{"kind": "commit", "ref": "deadbeef"}]}}
    return _fv_ok(json.dumps(payload))


def test_three_uncorroborated_no_verdict_claims_still_trip_the_three_strike_guard(
        tmp_path):
    """Three consecutive worker iterations on the one seeded task, each
    claiming `commit` progress evidence with no real workspace delta
    behind it -- uncorroborated every time -- driven through the REAL
    production pipeline (`_run_job_core()`, `max_approaches=1`, the
    default `vigilant=True` last-approach behaviour) rather than a raw
    `_step_worker()` call. Requirement B.4's own words: removing the
    legacy absorption dispatch "must not remove" this guard for a claim
    that fails corroboration -- so the ordinary three-strike stagnation
    witness must still fire exactly as it did before task 004 touched
    anything, landing the run at the SAME `paused`/`stagnation` outcome
    `tests/test_worker_hat.py::test_a_plan_with_work_left_is_never_
    routed_to_review` already pins for the pre-existing guard."""
    root = tmp_path / "run"
    workspace = tmp_path / "workspace"
    _init_git_workspace(workspace)

    RunDir(root=root).update_status(state="starting")
    sup = _spw010_supervisor(
        root, workspace, {"worker": _uncorroborated_commit_claim})
    _write_plan(sup, [_fv_task()])
    sup.iterations_used = 1

    asyncio.run(_drive_until_paused(sup))

    claims = [e for e in _fv_events(root) if e.get("type") == "progress_claim"]
    assert len(claims) == 3, claims
    assert all(c["corroborated"] is False for c in claims), claims

    msgs = _fv_log_messages(root)
    assert "3 iterations with no task progress" in msgs, msgs
    assert "routing to review" not in msgs, msgs

    status = sup.run.read_status()
    assert status.get("state") == "paused", status
    assert status.get("pauseReason") == "stagnation", status

    assert _worker_iteration_count(root) == 3, _worker_iteration_count(root)
