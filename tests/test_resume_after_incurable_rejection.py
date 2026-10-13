"""Task 007 (deferred out of task 006, requirement B.2, #111), re-pinned for
v0.36 requirement B (the incurable rejection is a trigger of the one ladder
park, `operator_input_needed`) and requirement E.2 (a park survives a
restart): the resume-point routing of a pre-0.36 run recorded as `running`
over an incurable escalation still lands BACK at `review` -- never at a fresh
approach (`replan`) and never at a throwaway `worker` iteration first -- while
a run recorded `awaiting_input` stays parked, with its `expiresAt`, for a fresh
engine process.

Two supervisors drive the same on-disk run dir, standing in for two engine
episodes over one `ralphctl resume` the way `tests/test_resume.py` uses two
`ralphd-engine` processes: episode 1 reaches the pause exactly the way
`tests/test_rejection_ladder.py` pins (a real `_step_review()`
call, all blocking findings incurable); episode 2 is a FRESH `LoopSupervisor`
constructed over the very same `RunDir` root -- exactly what a killed and
restarted container leaves behind, since `self._pause` is an in-memory
`asyncio.Event` that a process restart cannot carry over.

1. `LoopSupervisor._paused_awaiting_operator_input_needed_ruling(approach)` is True right
   after episode 1's pause, and `_resume_point()` returns `(approach, False,
   "review")` for episode 2 -- never `"replan"`, never the ordinary
   skip-planning default that would land at `worker`.
2. Driving episode 2 through the real dispatch loop (`_run_job_core()`):
   v0.36 requirement E.2 keeps a run parked in `awaiting_input` parked across
   a restart, with the same `expiresAt`, so episode 2 waits for an answer (or
   takes the timeout outcome when the expiry already passed) instead of
   re-judging. The routing in 1. is what a pre-0.36 `running` status with the
   escalation on record still gets.
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

from ralphd.engine import findings, loop as loop_mod
from ralphd.engine.config import JobConfig
from ralphd.engine.loop import LoopSupervisor
from ralphd.engine.runner import IterationResult
from ralphd.engine.state import (
    RunDir, atomic_write_json, read_events, write_amendment)
from tests.conftest import make_supervisor

AMENDMENT_MARKER = "Operator rulings (task 028, #26)"


def _finding(severity: str, curable: bool, clause: str, evidence: str) -> dict:
    return {"severity": severity, "curable": curable, "clause": clause,
            "evidence": evidence}


def _result(text: str) -> IterationResult:
    r = IterationResult(exit_code=0)
    r.final_text = text
    r.duration_s = 5.0
    r.usage = {"input": 10, "output": 5, "totalTokens": 15}
    return r


def _write_tasks(run: RunDir) -> None:
    atomic_write_json(run.tasks_file, {"version": 1, "tasks": [
        {"id": "001", "title": "do the thing", "status": "completed",
         "successCriteria": "the thing is done"},
    ]})


_supervisor = lambda root, run_id, **cfg: make_supervisor(
    root, **{"run_id": run_id, "workspace": "ws", "iterations": 8, "max_approaches": 3,
    "vigilant": False, "on_complete": "exit", "infra_retry_backoff_s": [0.0],
    "infra_retry_backoff_max_s": 0.0, "infra_outage_budget_s": 1000.0,
    "no_backoff": False, "real_sleep": True, **cfg})


def _pause_via_incurable_rejection(root: Path) -> LoopSupervisor:
    """Episode 1: a real `_step_review()` call that reaches
    `rejected_incurable`, pausing the run -- the exact fixture
    `tests/test_rejected_incurable_edge.py` already pins in isolation."""
    root.mkdir(parents=True, exist_ok=True)
    (root / "prd.md").write_text("# test PRD\n\nDo the thing.\n")
    sup1 = _supervisor(root, "task007-resume", operator_input_needed_pause=True)
    sup1.run.update_status(state="running")
    _write_tasks(sup1.run)
    sup1._approach = 1

    all_incurable = [
        _finding("blocking", False, "requirement B (#111)",
                 "the endpoint has no docker.sock in this container"),
    ]
    findings.write_findings_json(sup1.run.findings_json_file, all_incurable)

    async def _review_rejects(prompt, transcript, **kw):
        return _result(json.dumps({
            "verdict": "rejected",
            "reason": "the blocking finding is an environment fact a "
                      "fresh approach cannot change",
            "findings_path": "review-findings.md",
        }))

    sup1.runner.run = _review_rejects  # type: ignore[method-assign]

    outcome = asyncio.run(sup1._step_review())
    assert outcome == ("advance", "paused", "operator_input_needed")
    assert sup1._pause.is_set() is False  # paused, exactly as task 006 left it
    return sup1


# ---------------------------------------------------------------------------
# 1. The predicate and the resume-point routing, in isolation.
# ---------------------------------------------------------------------------

def test_paused_awaiting_incurable_ruling_is_true_right_after_the_pause(tmp_path):
    root = tmp_path / "run"
    sup1 = _pause_via_incurable_rejection(root)
    assert sup1._paused_awaiting_operator_input_needed_ruling(1) is True


def test_resume_point_routes_straight_to_review_not_worker_not_replan(tmp_path):
    root = tmp_path / "run"
    _pause_via_incurable_rejection(root)

    # Episode 2: a FRESH LoopSupervisor over the SAME run dir -- what a
    # killed-and-restarted container leaves behind. `self._pause` is
    # in-memory only, so this new instance starts with no memory of the
    # pause at all; `_resume_point()` alone must reconstruct it from disk.
    sup2 = _supervisor(root, "task007-resume")
    sup2.run.update_status(state="running")
    assert sup2._pause.is_set() is True  # nothing paused in THIS process

    start_approach, skip_planning, step = sup2._resume_point()

    assert step == "review"
    assert start_approach == 1
    # never a fresh approach: `replan` is the one thing this predicate
    # exists to keep resume away from.
    assert step != "replan"


def test_a_completed_iteration_since_the_escalation_stops_the_predicate(tmp_path):
    """Once a fresh iteration genuinely started after the escalation (an
    in-process resume that got all the way to running review again, or any
    later iteration), the stale escalation must not fire the predicate a
    second time -- `_resume_point()` falls through to its ordinary default
    instead of re-diagnosing an episode that already moved on."""
    root = tmp_path / "run"
    sup1 = _pause_via_incurable_rejection(root)
    # Simulate the in-process resume actually running another iteration:
    sup1.run.emit("iteration.start", number=99, phase="review")
    assert sup1._paused_awaiting_operator_input_needed_ruling(1) is False


# ---------------------------------------------------------------------------
# 2. End-to-end through the real dispatch loop: routing AND visibility of
#    the operator ruling recorded while paused.
# ---------------------------------------------------------------------------

def test_a_restart_over_the_park_stays_parked_and_a_ruling_does_not_release_it(
        tmp_path, monkeypatch):
    """v0.36 E.2/H: a fresh engine process over a run parked in
    `awaiting_input` no longer lands back on `review` (the 0.35 shape this
    node used to pin): it keeps the question and its `expiresAt` and waits for
    `ralphctl answer`. A ruling recorded while parked reaches the judges (the
    review-prompt half is pinned by tests/test_operator_ruling_review_verdict.py)
    but never releases the park (D.1); the answer then carries the run on."""
    root = tmp_path / "run"
    sup1 = _pause_via_incurable_rejection(root)
    parked_question = dict(sup1.run.read_status()["inputQuestion"])
    iterations_before = sup1.iterations_used

    cdir = tmp_path / "config"
    write_amendment(cdir, "001", "Waived: the missing docker.sock is an "
                    "environment fact, not something this run can fix.",
                    waive=True)
    monkeypatch.setattr(loop_mod, "CONFIG_DIR", cdir)

    sup2 = _supervisor(root, "task007-resume", status=None)
    seen = {}

    async def _wait(timeout_s: float) -> bool:
        if not seen:
            status = sup2.run.read_status()
            seen["state"] = status["state"]
            seen["question"] = dict(status["inputQuestion"])
            # the operator answers: the one finding is waived
            sup2.answer(waive=["F1"], note="docker.sock is an environment fact")
        return sup2._pause.is_set()
    sup2._wait_pause_event = _wait  # type: ignore[method-assign]

    async def _no_judge_runs(prompt, transcript, **kw):
        raise AssertionError("a restart over a park runs no judge")
    sup2.runner.run = _no_judge_runs  # type: ignore[method-assign]

    state = asyncio.run(asyncio.wait_for(sup2._run_job_core(), 60))

    assert seen["state"] == "awaiting_input"
    assert seen["question"] == parked_question
    assert state == "succeeded"
    # the answer verified the approach: no iteration ran in this episode.
    assert sup2.iterations_used == iterations_before


def test_a_restart_over_an_expired_park_takes_the_timeout_outcome_not_a_rejudge(
        tmp_path, monkeypatch):
    """The sibling of the node above with nobody answering: the park expired
    while the container was down, so the fresh process takes the timeout
    outcome at once -- here no approach is left, so the run holds `exhausted`
    (approaches) on the SAME approach, without re-asking the judge (the 0.35
    shape of this node let an unanswered rejection re-run `review`)."""
    root = tmp_path / "run"
    _pause_via_incurable_rejection(root)

    cdir = tmp_path / "config"
    cdir.mkdir(parents=True)
    monkeypatch.setattr(loop_mod, "CONFIG_DIR", cdir)

    sup2 = _supervisor(root, "task007-resume", status=None, max_approaches=1)
    sup2._now_epoch = lambda: time.time() + 2 * 3600   # past the 60 min expiry
    calls = []

    async def _no_judge_runs(prompt, transcript, **kw):
        calls.append(prompt)
        raise AssertionError("an expired park is not judged again")
    sup2.runner.run = _no_judge_runs  # type: ignore[method-assign]

    state = asyncio.run(asyncio.wait_for(sup2._run_job_core(), 60))

    assert state == "exhausted" and not calls
    status = sup2.run.read_status()
    assert status["exhaustedBudget"] == "approaches"
    # never bumped to a fresh approach.
    assert (status.get("approach") or 1) == 1
    fired = [e for e in read_events(root) if e.get("type") == "input.timed_out"]
    assert [e["outcome"] for e in fired] == ["exhausted_approaches"]
