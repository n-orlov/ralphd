"""Task 007 (requirement D, #99): a plan whose entire actionable remainder is
doomed raises a NAMED fault instead of starving quietly.

The defect this fixes (`docs/prds/v0.14-every-hand-on-the-plan.md`,
requirement D): `scheduler.select_next_task()` is graph-aware -- a doomed
`pending` task (its `dependsOn` names a `failed`/`skipped` ancestor,
directly or transitively) is never a candidate, so selection returns `None`.
But `state.task_is_actionable()` is per-task and dependency-blind
(`status in ("pending", "in-progress", "validation-failed")`, nothing
more), so `LoopSupervisor._unresolved_failures()` (ledger guard 9) still
reads a doomed task as actionable and returns `[]` (nothing routes to
review). Selection finds nothing, the worker is handed "nothing
selectable", changes no status, and three of those in a row trip the
pre-existing stagnation guard (ledger guard 10) -- misattributing an
UNREACHABLE plan as a merely-stuck approach.

The fix, pinned here: `LoopSupervisor._doomed_plan_fault()` (called from
`_step_worker`, the SAME place the pre-existing `blocked_by` notes-writing
already lives) checks whether `state.task_blocked_by()`'s doomed set covers
the ENTIRE actionable remainder -- not just a subset. When it does, the
harness raises the fault immediately (BEFORE dispatching that pass's worker
iteration at all -- there is no reply any worker could give that changes
anything) with a payload naming `doomedTaskIds`, `deadAncestorIds` and
`planUnverifiable: True`, and routes straight to `review` for a verdict
through the new `transitions.TABLE["worker"]` edge `doomed_plan_unverifiable`
-- reusing the #33 `blocked_with_failures` mechanism (a reviewer's verdict on
work-with-a-hole) rather than inventing a dependency-graph replan or a
terminal degraded state. That consequence choice is recorded here, in
`loop.py`'s module docstring, in `_doomed_plan_fault`'s own docstring, and in
the commit message.

Two directions, both driven through a real `LoopSupervisor` with a stubbed
`sup.runner` (never `_run_iteration_once`):

* `test_a_doomed_subset_with_actionable_work_left_behaves_exactly_as_today`:
  one task is doomed, another is genuinely actionable and never finishes --
  the run stagnates and fails after three no-progress iterations, EXACTLY
  the pre-existing behaviour, and the fault never fires.
* `test_a_wholly_doomed_remainder_raises_the_fault_instead_of_stagnating`:
  the only task left is doomed -- the fault fires on the very FIRST pass,
  before the worker hat is ever called, routing straight to review.

Mutation case (recorded in the commit message): dropping the `actionable_ids
<= doomed_ids` subset check in `_doomed_plan_fault` (i.e. firing on ANY
non-empty `blocked_by`, not just when it covers the whole remainder) would
still pass the second test but fail the first one -- the doomed-subset run
would raise the fault on its very first pass, before task 001 (still
actionable) ever gets a chance to run at all, and `sup.runner.calls["worker"]`
would read 0 instead of 3.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from ralphd.engine import transitions
from ralphd.engine.config import JobConfig
from ralphd.engine.loop import LoopSupervisor
from ralphd.engine.runner import IterationResult
from ralphd.engine.state import RunDir, atomic_write_json

TASK_1 = "001"
TASK_2 = "002"
TASK_3 = "003"


# -- scaffolding (mirrors tests/test_unsatisfiable_criteria.py) -----------


def _ok(text: str) -> IterationResult:
    r = IterationResult(exit_code=0)
    r.final_text = text
    r.duration_s = 5.0
    r.usage = {"input": 10, "output": 5, "totalTokens": 15}
    return r


def _no_progress(sup, prompt="") -> IterationResult:
    return _ok("I looked at this and made no progress")


def _review_rejects(sup, prompt="") -> IterationResult:
    return _ok("this plan cannot be verified; rejecting")


class _ScriptedAgent:
    """Same shape as `tests/test_unsatisfiable_criteria.py`'s: dispatches on
    the phase the engine recorded, one script per phase, the last step of
    each repeating forever."""

    def __init__(self, handlers: dict):
        self.handlers = handlers
        self.calls: dict[str, int] = {}
        self.running = False

    def interrupt(self) -> bool:
        return self.running

    async def run(self, prompt, transcript, **kw) -> IterationResult:
        phase = self.sup.run.read_status().get("phase")
        n = self.calls.get(phase, 0)
        self.calls[phase] = n + 1
        steps = self.handlers[phase]
        self.running = True
        try:
            return steps[min(n, len(steps) - 1)](self.sup, prompt)
        finally:
            self.running = False


def _supervisor(root: Path, handlers: dict, **cfg_kw) -> LoopSupervisor:
    root.mkdir(parents=True, exist_ok=True)
    run = RunDir(root=root)
    kw = {"iterations": 15, "max_approaches": 1, "vigilant": False,
          "on_complete": "exit", "infra_retry_backoff_s": [0.0],
          "infra_retry_backoff_max_s": 0.0, "infra_outage_budget_s": 1000.0,
          **cfg_kw}
    sup = LoopSupervisor(JobConfig(run_id="v014-doomed-plan", **kw), run, root)
    agent = _ScriptedAgent(handlers)
    agent.sup = sup                      # type: ignore[attr-defined]
    sup.runner = agent                    # type: ignore[assignment]

    async def no_backoff(seconds):
        return seconds, False

    sup._wait_out_backoff = no_backoff    # type: ignore[method-assign]
    return sup


def _run_resumed(root: Path, handlers: dict, tasks: list[dict],
                 **cfg_kw) -> tuple[LoopSupervisor, str]:
    """These seeded tasks bypass planning entirely (a real
    plan-proposal.json contract can never carry a task's own `status`), the
    same resumed-run shape `tests/test_unsatisfiable_criteria.py` uses.
    `handlers` must have no `"planning"` key."""
    RunDir(root=root).update_status(state="starting")
    sup = _supervisor(root, handlers, **cfg_kw)
    atomic_write_json(sup.run.tasks_file, {"version": 1, "tasks": tasks})
    sup.iterations_used = 1
    return sup, asyncio.run(sup._run_job_core())


async def _drive_until_paused(sup: LoopSupervisor) -> None:
    """Task 012 (requirement B.3, #153): a `_run_job_core()` that hits the
    stagnation guard on the LAST approach now pauses and escalates instead
    of ending the run `failed` -- and stays parked at `_gate()` forever
    without an operator's `resume()`, which none of these scripted-runner
    tests ever send. Starts `_run_job_core()` as a background task, polls
    `status.json` for `state == "paused"`, then cancels the still-pending
    task so the test's own event loop can close cleanly."""
    task = asyncio.ensure_future(sup._run_job_core())
    for _ in range(500):
        if sup.run.read_status().get("state") == "paused":
            break
        if task.done():
            break
        await asyncio.sleep(0.01)
    else:
        task.cancel()
        raise AssertionError("run never reached `paused`")
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


def _events(root: Path) -> list[dict]:
    p = root / "events.jsonl"
    if not p.exists():
        return []
    return [json.loads(x) for x in p.read_text().splitlines() if x.strip()]


def _fault_events(root: Path) -> list[dict]:
    return [ev for ev in _events(root)
            if ev.get("type") == "log" and "doomedTaskIds" in ev]


def _phases_recorded(root: Path) -> list[str]:
    return [ev.get("phase") for ev in _events(root) if ev.get("type") == "phase"]


# -- the table declares the new edge ---------------------------------------


def test_the_table_declares_the_doomed_plan_edge():
    guards = transitions.guard_names("worker", vigilant=False)
    assert "doomed_plan_unverifiable" in guards
    assert transitions.is_legal("worker", "review", vigilant=False)
    # the vigilant variant inherits the same edge (VIGILANT_DELTA composes
    # on top of TABLE["worker"], never replaces it).
    assert "doomed_plan_unverifiable" in transitions.guard_names(
        "worker", vigilant=True)


# -- the pure helper in isolation -------------------------------------------


def _bare_sup(tmp_path: Path) -> LoopSupervisor:
    root = tmp_path / "bare"
    return _supervisor(root, {})


def test_helper_returns_none_for_a_doomed_subset_with_actionable_work(tmp_path):
    sup = _bare_sup(tmp_path)
    tasks = [
        {"id": TASK_1, "status": "pending"},
        {"id": TASK_2, "status": "failed"},
        {"id": TASK_3, "status": "pending", "dependsOn": [TASK_2]},
    ]
    from ralphd.engine.state import task_blocked_by
    blocked_by = task_blocked_by(tasks)
    assert blocked_by == {TASK_3: [TASK_2]}
    assert sup._doomed_plan_fault({"tasks": tasks}, blocked_by) is None


def test_helper_fires_when_the_whole_remainder_is_doomed_and_names_dead_ancestors(
        tmp_path):
    sup = _bare_sup(tmp_path)
    tasks = [
        {"id": TASK_2, "status": "failed"},
        {"id": TASK_3, "status": "pending", "dependsOn": [TASK_2]},
    ]
    from ralphd.engine.state import task_blocked_by
    blocked_by = task_blocked_by(tasks)
    fault = sup._doomed_plan_fault({"tasks": tasks}, blocked_by)
    assert fault == {
        "doomedTaskIds": [TASK_3],
        "deadAncestorIds": [TASK_2],
        "planUnverifiable": True,
    }


def test_helper_returns_none_with_no_doomed_tasks_at_all(tmp_path):
    sup = _bare_sup(tmp_path)
    tasks = [{"id": TASK_1, "status": "pending"}]
    assert sup._doomed_plan_fault({"tasks": tasks}, {}) is None


# -- the whole run, both directions -----------------------------------------


def test_a_doomed_subset_with_actionable_work_left_behaves_exactly_as_today(
        tmp_path):
    """Task 001 is genuinely actionable (no `dependsOn`) and never finishes;
    task 003 is a doomed SUBSET (blocked on failed task 002). This must
    behave byte-for-byte like the pre-existing stagnation path: the fault
    never fires, the worker hat is actually called (three times, the
    ordinary stagnation count), and the run pauses through the ordinary
    last-approach stagnation guard (task 012, requirement B.3), not
    through the new doomed-plan guard."""
    root = tmp_path / "run"
    tasks = [
        {"id": TASK_1, "status": "pending", "title": "keep going"},
        {"id": TASK_2, "status": "failed", "title": "already dead"},
        {"id": TASK_3, "status": "pending", "title": "doomed",
         "dependsOn": [TASK_2]},
    ]

    sup = _supervisor(root, {
        "worker": [_no_progress],
        "review": [_review_rejects],
    })
    RunDir(root=root).update_status(state="starting")
    atomic_write_json(sup.run.tasks_file, {"version": 1, "tasks": tasks})
    sup.iterations_used = 1
    asyncio.run(_drive_until_paused(sup))

    status = sup.run.read_status()
    assert status.get("state") == "paused", status
    assert status.get("pauseReason") == "stagnation"
    assert sup.runner.calls["worker"] == 3        # type: ignore[attr-defined]
    assert not _fault_events(root), \
        "a doomed SUBSET with actionable work left must never raise the fault"
    assert "review" not in _phases_recorded(root), \
        "stagnation on the last approach pauses, it never routes to review"

    doc = sup.run.read_tasks()
    by_id = {t["id"]: t for t in doc["tasks"]}
    # task 001 was claimed `in-progress` on the first pass (the harness's
    # own pre-spawn bet, task 116) and stays there -- it never finished,
    # but it WAS worked on, unlike task 003, which was never even claimed.
    assert by_id[TASK_1]["status"] == "in-progress"
    assert by_id[TASK_3]["status"] == "pending"

    # the pre-existing per-task blocked-notes mechanism (task 021/#60) is
    # untouched by this task: task 003's doom is still noted, exactly as
    # before -- this task only adds a NEW behaviour on top for the wholly
    # doomed case, it does not remove the old one for the subset case.
    # (Not asserted strictly here since task 001 is always selectable and
    # `selected` therefore never reads `None` in this fixture -- the point
    # this test pins is that nothing NEW happens, not that the notes path
    # specifically fires.)


def test_a_wholly_doomed_remainder_raises_the_fault_instead_of_stagnating(
        tmp_path):
    """The only task left is doomed: the fault fires on the very FIRST
    pass, before the worker hat is ever dispatched, and routes straight to
    review -- not three silent no-progress iterations and a stagnation
    verdict."""
    root = tmp_path / "run"
    tasks = [
        {"id": TASK_2, "status": "failed", "title": "already dead"},
        {"id": TASK_3, "status": "pending", "title": "doomed",
         "dependsOn": [TASK_2]},
    ]

    sup, state = _run_resumed(root, {
        "review": [_review_rejects],
    }, tasks)

    assert sup.runner.calls.get("worker", 0) == 0, \
        "no worker iteration could ever change anything here -- none must run"
    fault_events = _fault_events(root)
    assert len(fault_events) == 1
    fault = fault_events[0]
    assert fault["doomedTaskIds"] == [TASK_3]
    assert fault["deadAncestorIds"] == [TASK_2]
    assert fault["planUnverifiable"] is True
    assert "review" in _phases_recorded(root)
    assert sup.runner.calls.get("review", 0) >= 1  # type: ignore[attr-defined]

    doc = sup.run.read_tasks()
    task = next(t for t in doc["tasks"] if t["id"] == TASK_3)
    assert task["status"] == "pending", \
        "the doomed task's own record is untouched by the fault"

    # rejected with no approach left -> the run ends, not stuck retrying.
    assert state in ("failed", "aborted"), sup.run.read_status()
