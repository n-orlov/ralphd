"""v0.20 task 022 (PRD requirement C.1, #156): the sleep-and-wake mechanism
`engine.scheduler.select_next_task`'s own docstring promised task 021's
deferral reorder would eventually need -- "resuming a deferred task once
nothing else is runnable is task 022's sleep mechanism, layered on top of
this function's `None` return, not a rule this function encodes itself" --
and the gap task 021's own `test_deferred_verdict.py` flagged directly: "the
run ends 'failed' ... task 001 is still outstanding, deferred, with no
wake-up mechanism yet, task 022's job."

Three things this module pins directly, through a real `LoopSupervisor`
(the same `scripted_supervisor`/`_run_resumed` harness
`tests/test_deferred_verdict.py` already uses):

* Nothing runnable, one deferred task whose own `until` arrives before the
  job's `deadlineAt`: the engine records a `deferred_wait` event naming
  `boundedBy: "until"`, then clears that task's `deferredUntil`/
  `deferredReason` in the SAME pass so the very next dispatch picks it up
  as an ordinary candidate again -- never a second scripted call spent on
  the reorder itself.
* Nothing runnable, one deferred task whose `until` sits PAST the job's
  `deadlineAt`: the engine still records `deferred_wait`, this time naming
  `boundedBy: "deadlineAt"`, but the task's record is left completely
  untouched (its own wait never actually elapsed) and the ordinary
  "nothing selectable" worker dispatch runs exactly once -- never a second,
  spinning dispatch chasing the same unmet bound.
* A worker's `verdict: "deferred"` report naming an `until` past the job's
  own `deadlineAt` is refused at the contract, with the deadline itself
  named in the returned hint -- the task's record is left untouched, the
  same "illegal move refused" shape `test_deferred_verdict.py`'s own
  blank-reason case already pins.

Mutation cases (recorded in the commit message): dropping the `boundedBy
== "until"` clear in `_sleep_until_earliest_deferred` would leave the
scheduler excluding the task forever, failing
`test_the_earliest_until_wakes_the_task_and_the_picker_completes_it` (the
task would never reach `completed`); dropping the `deadlineAt` refusal in
`_apply_worker_deferred_report` would let a report naming an unreachable
`until` through, failing
`test_an_until_past_the_deadline_is_refused_with_the_deadline_in_the_hint`;
making the deadline-bound branch `continue` the dispatch loop instead of
falling through would busy-spin against a `deferredUntil` that can never
actually elapse under this suite's globally instant `asyncio.sleep`
(`tests/conftest.py`), which
`test_a_deadline_bound_leaves_the_task_untouched_and_dispatches_once` would
catch as an unbounded `sup.runner.calls["worker"]` growth (or a hang) well
before it grew large enough to matter.
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

from ralphd.engine.loop import LoopSupervisor
from ralphd.engine.runner import IterationResult
from ralphd.engine.state import RunDir, atomic_write_json, utc_from_epoch
from tests.conftest import scripted_supervisor

TASK_1 = "001"


def _ok(text: str) -> IterationResult:
    r = IterationResult(exit_code=0)
    r.final_text = text
    r.duration_s = 5.0
    r.usage = {"input": 10, "output": 5, "totalTokens": 15}
    return r


def _complete(task_id: str) -> IterationResult:
    payload = json.dumps({"verdict": "complete", "task_id": task_id})
    return _ok(f"task {task_id} done\n{payload}")


def _task(**over) -> dict:
    task = {"id": TASK_1, "title": "wait on a gated window", "status": "pending",
            "successCriteria": "the window opened"}
    task.update(over)
    return task


_supervisor = scripted_supervisor(run_id="v20-deferred-wait", iterations=15,
                                  max_approaches=1, vigilant=False,
                                  on_complete="exit",
                                  infra_outage_budget_s=1000.0)


def _run_resumed(root: Path, handlers: dict, tasks: list[dict],
                 **cfg_kw) -> tuple[LoopSupervisor, str]:
    """Same shape as `tests/test_deferred_verdict.py`'s own helper: seed
    disk state exactly like a resumed run and skip planning entirely."""
    RunDir(root=root).update_status(state="starting")
    sup = _supervisor(root, handlers, **cfg_kw)
    atomic_write_json(sup.run.tasks_file, {"version": 1, "tasks": tasks})
    sup.iterations_used = 1
    return sup, asyncio.run(sup._run_job_core())


def _events(root: Path) -> list[dict]:
    p = root / "events.jsonl"
    if not p.exists():
        return []
    return [json.loads(x) for x in p.read_text().splitlines() if x.strip()]


# -- the wake-up: the task's own `until` arrives first ----------------------


def test_the_earliest_until_wakes_the_task_and_the_picker_completes_it(tmp_path):
    root = tmp_path / "run"
    until = utc_from_epoch(time.time() + 2)
    tasks = [_task(deferredUntil=until, deferredReason="waiting on a "
                   "gated window to open")]

    sup, final_state = _run_resumed(root, {
        "worker": [lambda s, p: _complete(TASK_1)],
        "review": [lambda s, p: _ok("the PRD holds\n" +
                                   json.dumps({"verdict": "verified"}))],
    }, tasks, iterations=3, job_timeout_s=3600)  # deadline far past `until`

    doc = sup.run.read_tasks()
    task = doc["tasks"][0]
    assert task["status"] == "completed", doc
    assert "deferredUntil" not in task
    assert "deferredReason" not in task

    waits = [ev for ev in _events(root) if ev.get("type") == "deferred_wait"]
    assert len(waits) == 1, waits
    assert waits[0]["boundedBy"] == "until"
    assert waits[0]["taskId"] == TASK_1
    assert waits[0]["until"] == until

    # a single scripted worker call did the whole job: one dispatch woke
    # the task up (the sleep-and-clear pass itself spends no iteration),
    # and that SAME re-offered task is what the very next dispatch
    # actually completed.
    assert sup.runner.calls["worker"] == 1     # type: ignore[attr-defined]
    assert final_state == "succeeded", sup.run.read_status()


# -- the non-wake-up: the job's own deadline arrives first -------------------


def test_a_deadline_bound_leaves_the_task_untouched_and_dispatches_once(tmp_path):
    root = tmp_path / "run"
    far_until = "2099-01-01T00:00:00Z"
    tasks = [_task(deferredUntil=far_until, deferredReason="waiting on a "
                   "window that outlives this job")]

    sup, final_state = _run_resumed(root, {
        "worker": [lambda s, p: _ok(
            "nothing else to do\n" + json.dumps({"verdict": "no_verdict"}))],
        "review": [],
    }, tasks, iterations=2, job_timeout_s=1)  # deadline arrives well before 2099

    task = sup.run.read_tasks()["tasks"][0]
    assert task["status"] == "pending"
    assert task["deferredUntil"] == far_until, (
        "the deadline bound must leave an unelapsed deferral untouched")
    assert task["deferredReason"] == "waiting on a window that outlives this job"

    waits = [ev for ev in _events(root) if ev.get("type") == "deferred_wait"]
    assert len(waits) == 1, waits
    assert waits[0]["boundedBy"] == "deadlineAt"
    assert waits[0]["taskId"] == TASK_1
    assert waits[0]["until"] == far_until

    # fell through to the ordinary "nothing selectable" dispatch exactly
    # once -- never re-entered the sleep branch a second time chasing the
    # same unmet bound (the busy-spin this module's docstring warns about).
    assert sup.runner.calls["worker"] == 1     # type: ignore[attr-defined]


# -- the contract refusal: an unreachable `until` is never accepted ---------


def test_an_until_past_the_deadline_is_refused_with_the_deadline_in_the_hint(
        tmp_path):
    root = tmp_path / "run"
    tasks = [_task(status="in-progress")]
    far_until = "2099-01-01T00:00:00Z"
    bad = json.dumps({"verdict": "deferred", "until": far_until,
                      "reason": "waiting on a window that outlives this job"})

    sup, final_state = _run_resumed(root, {
        "worker": [lambda s, p: _ok(f"must wait\n{bad}")],
        "review": [],
    }, tasks, iterations=2, job_timeout_s=10)  # `until` sits well past deadline

    task = sup.run.read_tasks()["tasks"][0]
    assert task["status"] == "in-progress", (
        "a contract violation must not silently take the move anyway")
    assert "deferredUntil" not in task

    events = _events(root)
    violation_events = [ev for ev in events if ev.get("type") == "contract.violation"]
    fault_events = [ev for ev in events if ev.get("type") == "contract.agent_fault"]
    assert len(violation_events) == sup.cfg.contract_retry_max + 1
    assert len(fault_events) == 1
    assert all("deadline" in ev["errors"][0] for ev in violation_events)
    assert all(far_until in ev["errors"][0] for ev in violation_events)

    assert sup.runner.calls["worker"] == sup.cfg.contract_retry_max + 1  # type: ignore[attr-defined]
    # Task 011 (v0.24 requirement C.1, #169): the ladder exhausted (an
    # agent fault, not silence) and the run kept going per the table;
    # the ITERATION budget is what actually ended it -- the plain shape
    # now holds `timed_out`/`verdict: null` through the same
    # `_finish_timed_out()` path task 010 established, never `"failed"`.
    assert final_state == "timed_out", sup.run.read_status()
    assert sup.run.read_status()["verdict"] is None
