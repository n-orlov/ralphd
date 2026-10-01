"""Task 006 (v0.20.1 PRD requirement B.4): task 004 deleted the legacy
absorption-streak forced-verify dispatch (`_record_task_absorption_streak`,
`TASK_ABSORPTION_STATUS_KEY`). Requirement B.4's own letter is explicit
that removing it "must not remove" the ORDINARY three-strike stagnation
witness (`tests/test_stagnation_progress_witnesses_task010.py`'s own
subject, folded into `tests/test_worker_hat.py`) for a claim the engine
cannot corroborate against a real workspace delta.

Task 013's unit-level node pinned this shape at the `_step_worker()`
call-site level, and this module drove the SAME shape through the full
production pipeline (`_run_job_core()`, a real vigilant last-approach
pause) the way task 005's own new module drove requirement B.3, to pin it
explicitly for THIS requirement/PRD.

PRD v0.29 #279 (task 014's sweep cure) now supersedes that shape
specifically: a repeated uncorroborated-commit-claim pattern is ALSO
#279's own re-poll-backoff trigger, which intercepts on the second claim
and every one after it for as long as the pattern continues, so the
ordinary three-strike counter this module used to drive to 3 can no
longer get there for this exact shape (task 013's and task 015's own
sibling nodes hit the identical wall at the unit level and were rewritten
the same way). The guard this requirement names is unaffected for every
OTHER uncorroborated shape (anything that is not itself a `no_verdict` +
zero-delta + uncorroborated re-poll) -- only this module's own scripted
scenario, which happens to be exactly that shape on every pass, is
affected.

The rewrite below also bounds its own `_run_job_core()` drive
(`tests/test_loop_driving_await_guard.py`'s guard: a bare
`asyncio.run(sup._run_job_core())` can wait forever) with the same
`_bounded(coro, timeout=30.0)` helper `tests/test_peer_verdict_gate.py`
and `tests/test_peer_plan_review_gate.py` already define, instead of
`_drive_until_paused` -- the run no longer pauses for this scenario at
all, so polling for a `paused` status it will never reach would itself
hang.
"""
from __future__ import annotations

import asyncio
import json

from ralphd.engine.runner import IterationResult
from ralphd.engine.state import RunDir

from tests.test_worker_hat import (
    _fv_events,
    _fv_log_messages,
    _fv_ok,
    _fv_task,
    _init_git_workspace,
    _spw010_supervisor,
    _worker_iteration_count,
    _write_plan,
)


def _bounded(coro, timeout: float = 30.0):
    """Bound a loop-driving await the same way
    `tests/test_peer_verdict_gate.py` and
    `tests/test_peer_plan_review_gate.py` already do -- a bare
    `asyncio.run(sup._run_job_core())` can wait forever
    (`tests/test_loop_driving_await_guard.py`'s guard)."""
    return asyncio.run(asyncio.wait_for(coro, timeout))


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


def test_three_uncorroborated_no_verdict_claims_now_back_off_instead_of_tripping_the_three_strike_guard(
        tmp_path):
    """Repeated worker iterations on the one seeded task, each claiming
    `commit` progress evidence with no real workspace delta behind it --
    uncorroborated every time -- driven through the REAL production
    pipeline (`_run_job_core()`, `max_approaches=1`, the default
    `vigilant=True` last-approach behaviour) rather than a raw
    `_step_worker()` call.

    This test used to prove requirement B.4's own words: that removing the
    legacy absorption dispatch (task 004) "must not remove" the ordinary
    three-strike stagnation witness for a claim that fails corroboration,
    landing the run at `paused`/`stagnation`. PRD v0.29 #279 (task 014's
    sweep cure, same reasoning as `tests/test_progress_budget_task013.py`'s
    and `tests/test_progress_pin_task015.py`'s own sibling rewrites)
    supersedes that for this exact shape: `_apply_engine_repoll_backoff`'s
    trigger is the SAME signal (`no_verdict`, `corroborated: false`,
    `deltaSize: 0`, two consecutive times on one task) this scenario
    scripts on every single pass, so from the second claim onward every
    further claim is ALSO the second-or-later of a still-running re-poll
    streak -- it backs off (doubling each time, capped at 1800s) rather
    than ever feeding the three-strike counter a second or third witness,
    for as long as the task keeps re-polling identically. With the
    harness's `asyncio.sleep` patched instant (`make_supervisor`'s
    default), the run spins through its own 20-iteration budget
    (`_spw010_supervisor`'s own `iterations=20`) in increasingly
    backed-off re-polls and ends `timed_out` (budget exhausted, task never
    completed, never paused) rather than ever reaching the old
    `paused`/`stagnation` outcome
    `tests/test_worker_hat.py::test_a_plan_with_work_left_is_never_
    routed_to_review` still pins for the pre-#279, non-repoll-shaped
    guard."""
    root = tmp_path / "run"
    workspace = tmp_path / "workspace"
    _init_git_workspace(workspace)

    RunDir(root=root).update_status(state="starting")
    sup = _spw010_supervisor(
        root, workspace, {"worker": _uncorroborated_commit_claim})
    _write_plan(sup, [_fv_task()])
    sup.iterations_used = 1

    _bounded(sup._run_job_core())

    claims = [e for e in _fv_events(root) if e.get("type") == "progress_claim"]
    assert len(claims) == 19, claims
    assert all(c["corroborated"] is False for c in claims), claims

    backoffs = [e for e in _fv_events(root) if e.get("type") == "task.repoll_backoff"]
    assert [b["consecutiveCount"] for b in backoffs] == list(range(2, 20)), backoffs

    msgs = _fv_log_messages(root)
    assert "3 iterations with no task progress" not in msgs, msgs
    assert "routing to review" not in msgs, msgs

    status = sup.run.read_status()
    assert status.get("state") == "timed_out", status
    assert "iteration budget" in status.get("reason", ""), status

    assert _worker_iteration_count(root) == 19, _worker_iteration_count(root)
