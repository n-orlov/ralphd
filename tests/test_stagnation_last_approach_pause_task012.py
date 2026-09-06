"""Task 012 (PRD requirement B.3, #153): "on the last approach, stagnation
never ends `failed` silently."

Before this task, three consecutive no-progress worker iterations
(`stagnant >= 3`) always took `_step_worker`'s `stagnation_exhausted` edge
straight to `replan`, which -- on the LAST approach -- immediately
archives the approach and falls through to `budget_or_approach_exhausted`
-> `finishing` -> `failed`, with nobody ever having looked at it. This
generalises `review`'s existing `rejected_last_approach` pause (task 011,
v0.18 D.1) to the SAME situation reached from `worker` instead of
`review`: a stagnant LAST approach now pauses through the existing pause
machinery (`self.pause()`), emits `operator_escalation` with
`reason: "stagnation"`, and notifies, leaving the approach unarchived so a
`resume` re-enters it directly -- into `worker`, not `review` (there is no
rejection verdict to reconsider, only unfinished tasks). An approach that
still has a successor left takes the ORDINARY `stagnation_exhausted` edge
exactly as before -- this task changes nothing about that case.

Driven the same way `tests/test_last_approach_pause_task011.py` drives
`_step_review()` directly: a real `LoopSupervisor._step_worker()` call
against a scripted, no-subprocess runner, never a mock of the guard
itself. Two levels: (1) a single `_step_worker()` call exercises the
whole three-iteration stagnation formula and the pause/escalation side
effects; (2) `_step_paused()`/`_resume_point()` exercise the resume side,
in-process and across a fresh process respectively -- exactly like task
011's own in-process vs. fresh-process resume tests.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from ralphd.engine import transitions
from ralphd.engine.config import JobConfig
from ralphd.engine.loop import LoopSupervisor
from ralphd.engine.runner import IterationResult
from ralphd.engine.state import RunDir, atomic_write_json
from tests.conftest import make_supervisor, supervisor_with_runner

TASK_ID = "001"


def _task(**over) -> dict:
    task = {"id": TASK_ID, "title": "make the thing work",
            "status": "pending", "successCriteria": "the thing works"}
    task.update(over)
    return task


def _ok(text: str = "looked at the task and made no change") -> IterationResult:
    r = IterationResult(exit_code=0)
    r.final_text = text
    r.duration_s = 5.0
    r.usage = {"input": 10, "output": 5, "totalTokens": 15}
    return r


class _NoopRunner:
    """Every iteration is an ordinary successful one: this module is about the
    stagnation counter, not about what any hat says."""

    def interrupt(self) -> bool:
        return False

    async def run(self, prompt, transcript, **kw):
        return _ok()


_supervisor = supervisor_with_runner(
    lambda sup: _NoopRunner(),
    run_id="task012-stagnation-last-approach", cfg_defaults=False,
    prd="# task 012 fixture PRD\n\nDo the thing.\n", status={"state": "running"},
    tasks=[_task()], max_approaches=1, vigilant=False, iterations=20)


class _StubNotifier:
    def __init__(self) -> None:
        self.messages: list[str] = []

    def __call__(self, text: str) -> None:
        self.messages.append(text)


def _events(root: Path, type_: str) -> list[dict]:
    log = root / "events.jsonl"
    lines = log.read_text().splitlines() if log.exists() else []
    return [ev for ev in (json.loads(x) for x in lines) if ev.get("type") == type_]


# ---------------------------------------------------------------------------
# 1. The declared edges themselves, and the generated mermaid.
# ---------------------------------------------------------------------------


def test_the_worker_to_paused_edge_is_declared_in_both_variants():
    for vigilant in (True, False):
        assert transitions.is_legal("worker", "paused", vigilant)
        by_guard = {e.guard: e.target for e in
                   transitions.successors("worker", vigilant)}
        assert by_guard["stagnation_last_approach"] == "paused"


def test_the_paused_to_worker_resume_edge_is_declared_in_both_variants():
    for vigilant in (True, False):
        by_guard = {e.guard: e.target for e in
                   transitions.successors("paused", vigilant)}
        assert by_guard["paused_resumed_stagnation"] == "worker"
        # the pre-existing rejected_last_approach resume target is unchanged.
        assert by_guard["paused_resumed"] == "review"


def test_the_generated_mermaid_carries_the_new_edge():
    import sys
    scripts_dir = str(Path(__file__).resolve().parents[1] / "scripts")
    if scripts_dir not in sys.path:
        sys.path.insert(0, scripts_dir)
    import render_transition_diagram as rtd
    rendered = rtd.render()
    assert "worker --> paused : stagnation_last_approach" in rendered
    assert "paused --> worker : paused_resumed_stagnation" in rendered


# ---------------------------------------------------------------------------
# 2. The pause outcome: last approach, three no-progress worker iterations
#    -> pause and escalate, never `failed`, approach unarchived.
# ---------------------------------------------------------------------------


def test_stagnant_last_approach_pauses_and_escalates_instead_of_failing(tmp_path):
    root = tmp_path / "run"
    sup = _supervisor(root, max_approaches=1)
    sup._approach = 1
    notifier = _StubNotifier()
    sup.notifier = notifier

    assert sup._pause.is_set() is True  # not paused yet

    outcome = asyncio.run(sup._step_worker())

    assert outcome == ("advance", "paused", "stagnation_last_approach")
    # paused through the EXISTING pause machinery -- the same asyncio.Event
    # POST /pause's loop.pause() clears.
    assert sup._pause.is_set() is False
    status = sup.run.read_status()
    assert status.get("state") == "paused"
    assert status.get("pauseReason") == "stagnation"
    assert status.get("state") != "failed"  # never failed silently

    # never archived, never a composite PRD for a fresh (nonexistent)
    # approach -- resume asks worker again over the SAME approach.
    assert not (root / "approaches" / "01").exists()
    assert not sup.run.composite_prd_file.exists()
    assert sup._approach == 1

    escalations = _events(root, "operator_escalation")
    assert len(escalations) == 1
    assert escalations[0]["reason"] == "stagnation"
    assert escalations[0]["approach"] == 1
    assert escalations[0]["outstandingTasks"][0]["id"] == TASK_ID

    assert len(notifier.messages) == 1
    text = notifier.messages[0]
    assert "LAST" in text
    assert "ralphctl resume task012-stagnation-last-approach" in text
    assert TASK_ID in text


def test_the_escalation_is_emitted_to_events_jsonl_with_no_notifier(tmp_path):
    root = tmp_path / "run"
    sup = _supervisor(root, max_approaches=1)
    sup._approach = 1
    assert sup.notifier is None

    outcome = asyncio.run(sup._step_worker())
    assert outcome == ("advance", "paused", "stagnation_last_approach")
    assert sup._pause.is_set() is False

    escalations = _events(root, "operator_escalation")
    assert len(escalations) == 1
    assert escalations[0]["reason"] == "stagnation"


# ---------------------------------------------------------------------------
# 3. Resume: in-process (`_step_paused`) and across a fresh process
#    (`_resume_point`) both re-enter the SAME approach, into `worker` --
#    never `review`, never a fresh `replan`.
# ---------------------------------------------------------------------------


def test_in_process_resume_re_enters_worker_over_the_same_approach(tmp_path):
    root = tmp_path / "run"
    sup = _supervisor(root, max_approaches=1)
    sup._approach = 1

    outcome = asyncio.run(sup._step_worker())
    assert outcome == ("advance", "paused", "stagnation_last_approach")

    async def _resume_flow():
        # `_step_paused` reads `pauseReason` BEFORE it awaits `_gate()` --
        # exactly like the real engine, where an operator's `resume()`
        # (a separate concurrent call, e.g. from `POST /resume`) only
        # ever runs WHILE `_gate()` is already parked, never before this
        # coroutine has had a chance to read the still-persisted reason.
        task = asyncio.ensure_future(sup._step_paused())
        await asyncio.sleep(0)
        sup.resume()
        return await task

    outcome2 = asyncio.run(_resume_flow())
    assert outcome2 == ("advance", "worker", "paused_resumed_stagnation")
    status = sup.run.read_status()
    assert status.get("state") == "running"
    assert status.get("pauseReason") is None
    assert sup._approach == 1


def test_fresh_process_resume_detects_the_stagnation_pause_and_re_enters_worker(
        tmp_path):
    root = tmp_path / "run"
    sup1 = _supervisor(root, max_approaches=1)
    sup1._approach = 1

    outcome = asyncio.run(sup1._step_worker())
    assert outcome == ("advance", "paused", "stagnation_last_approach")
    assert sup1._pause.is_set() is False

    # a brand-new process, no shared Python state at all -- reads
    # everything fresh off disk (`events.jsonl`, `status.json`).
    sup2 = _supervisor(root, max_approaches=1)
    assert sup2._paused_awaiting_stagnation_last_approach_ruling(1) is True
    # the OTHER predicate (rejected_last_approach) must not also fire.
    assert sup2._paused_awaiting_last_approach_ruling(1) is False

    start_approach, skip_planning, start_step = sup2._resume_point()
    assert start_approach == 1
    assert skip_planning is False
    assert start_step == "worker"


def test_paused_awaiting_stagnation_ruling_is_false_once_a_new_iteration_started(
        tmp_path):
    root = tmp_path / "run"
    sup = _supervisor(root, max_approaches=1)
    sup._approach = 1

    outcome = asyncio.run(sup._step_worker())
    assert outcome == ("advance", "paused", "stagnation_last_approach")
    assert sup._paused_awaiting_stagnation_last_approach_ruling(1) is True

    sup.run.emit("iteration.start", phase="worker", approach=1)
    assert sup._paused_awaiting_stagnation_last_approach_ruling(1) is False


# ---------------------------------------------------------------------------
# 4. The unchanged case: a stagnant approach with a successor left still
#    fails the approach exactly as today -- no pause, no escalation.
# ---------------------------------------------------------------------------


def test_a_stagnant_approach_with_approaches_left_still_fails_as_today(tmp_path):
    root = tmp_path / "run"
    sup = _supervisor(root, max_approaches=2)
    sup._approach = 1
    notifier = _StubNotifier()
    sup.notifier = notifier

    outcome = asyncio.run(sup._step_worker())

    assert outcome == ("advance", "replan", "stagnation_exhausted")
    # the approach WAS archived -- this is the ordinary exhaustion path,
    # not the new pause -- and the run was never paused or escalated.
    assert sup._pause.is_set() is True
    assert notifier.messages == []
    assert _events(root, "operator_escalation") == []
    status = sup.run.read_status()
    assert status.get("state") != "paused"
    assert status.get("pauseReason") is None
