"""Task 007 (deferred out of task 006, requirement B.2, #111): resuming a
run that was paused by the `rejected_incurable` self-loop must land BACK at
`review` -- never at a fresh approach (`replan`) and never at a throwaway
`worker` iteration first -- and the operator ruling recorded while it sat
paused must still be visible to that review iteration.

Two supervisors drive the same on-disk run dir, standing in for two engine
episodes over one `ralphctl resume` the way `tests/test_resume.py` uses two
`ralphd-engine` processes: episode 1 reaches the pause exactly the way
`tests/test_rejected_incurable_edge.py` already pins (a real `_step_review()`
call, all blocking findings incurable); episode 2 is a FRESH `LoopSupervisor`
constructed over the very same `RunDir` root -- exactly what a killed and
restarted container leaves behind, since `self._pause` is an in-memory
`asyncio.Event` that a process restart cannot carry over.

1. `LoopSupervisor._paused_awaiting_incurable_ruling(approach)` is True right
   after episode 1's pause, and `_resume_point()` returns `(approach, False,
   "review")` for episode 2 -- never `"replan"`, never the ordinary
   skip-planning default that would land at `worker`.
2. Driving episode 2 through the real dispatch loop (`_run_job_core()`) with
   a stub runner that only ever answers a `review`-phase prompt proves the
   routing end to end: exactly ONE iteration runs, it is that review
   iteration, and it contains the operator ruling text recorded (via
   `ralphctl amend`, `state.write_amendment`) into `CONFIG_DIR` WHILE episode
   1 was still paused -- proving the ruling is visible to the very review
   iteration resume lands on, not merely present on disk somewhere.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from ralphd.engine import findings, loop as loop_mod
from ralphd.engine.config import JobConfig
from ralphd.engine.loop import LoopSupervisor
from ralphd.engine.runner import IterationResult
from ralphd.engine.state import RunDir, atomic_write_json, write_amendment

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


def _supervisor(root: Path, run_id: str, **cfg_kw) -> LoopSupervisor:
    run = RunDir(root=root)
    kw = dict(iterations=8, max_approaches=3, vigilant=False,
             on_complete="exit", infra_retry_backoff_s=[0.0],
             infra_retry_backoff_max_s=0.0, infra_outage_budget_s=1000.0)
    kw.update(cfg_kw)
    cfg = JobConfig(run_id=run_id, **kw)
    return LoopSupervisor(cfg, run, root / "ws")


def _pause_via_incurable_rejection(root: Path) -> LoopSupervisor:
    """Episode 1: a real `_step_review()` call that reaches
    `rejected_incurable`, pausing the run -- the exact fixture
    `tests/test_rejected_incurable_edge.py` already pins in isolation."""
    root.mkdir(parents=True, exist_ok=True)
    (root / "prd.md").write_text("# test PRD\n\nDo the thing.\n")
    sup1 = _supervisor(root, "task007-resume")
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
    assert outcome == ("advance", "review", "rejected_incurable")
    assert sup1._pause.is_set() is False  # paused, exactly as task 006 left it
    return sup1


# ---------------------------------------------------------------------------
# 1. The predicate and the resume-point routing, in isolation.
# ---------------------------------------------------------------------------

def test_paused_awaiting_incurable_ruling_is_true_right_after_the_pause(tmp_path):
    root = tmp_path / "run"
    sup1 = _pause_via_incurable_rejection(root)
    assert sup1._paused_awaiting_incurable_ruling(1) is True


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
    assert sup1._paused_awaiting_incurable_ruling(1) is False


# ---------------------------------------------------------------------------
# 2. End-to-end through the real dispatch loop: routing AND visibility of
#    the operator ruling recorded while paused.
# ---------------------------------------------------------------------------

def test_resume_lands_on_review_and_sees_the_ruling_recorded_while_paused(
        tmp_path, monkeypatch):
    root = tmp_path / "run"
    _pause_via_incurable_rejection(root)

    # The operator's ruling, written HOST-SIDE via `ralphctl amend --waive`
    # (`state.write_amendment`) WHILE the run sat paused -- after episode 1
    # already escalated, before episode 2 ever starts.
    cdir = tmp_path / "config"
    write_amendment(cdir, "001", "Waived: the missing docker.sock is an "
                    "environment fact, not something this run can fix.",
                    waive=True)
    monkeypatch.setattr(loop_mod, "CONFIG_DIR", cdir)

    prompts: list[str] = []

    async def _review_sees_the_ruling(prompt, transcript, **kw):
        prompts.append(prompt)
        assert AMENDMENT_MARKER in prompt
        assert "Waived: the missing docker.sock" in prompt
        # A real review hat re-runs curability on THIS pass and, seeing
        # the operator's waiver, writes a fresh sidecar with no blocking
        # findings left -- `_review_verdict` recomputes the verdict from
        # findings.json on disk (task 002), so leaving episode 1's stale
        # incurable finding in place would make this episode reject again
        # (and re-pause) no matter what verdict this stub claims.
        findings.write_findings_json(sup2.run.findings_json_file, [])
        return _result(json.dumps({"verdict": "verified"}))

    sup2 = _supervisor(root, "task007-resume")
    sup2.runner.run = _review_sees_the_ruling  # type: ignore[method-assign]

    state = asyncio.run(sup2._run_job_core())

    assert state == "succeeded"
    # exactly one iteration ran in this episode, and it was the review one
    # the resume routed to directly -- not a worker iteration first, and
    # not a fresh approach's planning/worker pass.
    assert sup2.iterations_used == 2  # episode 1's review + this one
    assert len(prompts) == 1
    assert AMENDMENT_MARKER in prompts[0]


def test_resume_without_any_ruling_still_lands_on_review_and_rejects_again(
        tmp_path, monkeypatch):
    """Same resume routing, but with an EMPTY `CONFIG_DIR` (no ruling ever
    written) -- proving the `succeeded` outcome above is caused by the
    ruling reaching review, not by anything else in the fixture, exactly
    the way `tests/test_operator_ruling_review_verdict.py` pairs its two
    cases."""
    root = tmp_path / "run"
    _pause_via_incurable_rejection(root)

    cdir = tmp_path / "config"
    cdir.mkdir(parents=True)
    monkeypatch.setattr(loop_mod, "CONFIG_DIR", cdir)

    calls = []

    async def _review_rejects_again(prompt, transcript, **kw):
        calls.append(prompt)
        assert AMENDMENT_MARKER not in prompt
        return _result(json.dumps({
            "verdict": "rejected", "reason": "still no operator ruling",
            "findings_path": "review-findings.md"}))

    sup2 = _supervisor(root, "task007-resume", max_approaches=1)
    # Re-seed the same all-incurable finding so this second episode's
    # review self-loops right back onto `rejected_incurable` again --
    # with only one approach available, the budget check makes
    # `rejected_no_approach_left` win, which is fine: the point pinned
    # here is that resume reached `review` at all, on the SAME approach.
    findings.write_findings_json(
        sup2.run.findings_json_file,
        [_finding("blocking", False, "requirement B (#111)",
                 "the endpoint has no docker.sock in this container")])
    sup2.runner.run = _review_rejects_again  # type: ignore[method-assign]

    state = asyncio.run(sup2._run_job_core())

    assert state == "failed"
    assert len(calls) == 1
    status = sup2.run.read_status()
    # never bumped to a fresh approach: `_resume_point()` kept it at 1.
    assert (status.get("approach") or 1) == 1
