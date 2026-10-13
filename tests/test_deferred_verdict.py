"""v0.20 task 021 (PRD requirement C.1, #156): a worker's `verdict:
"deferred"` report -- "a task that must wait" -- moves the task assigned
this iteration BEHIND every runnable task instead of absorbing further
iterations on it right now.

Four things this module pins directly, mirroring the split
`tests/test_unsatisfiable_criteria.py` already uses for the sibling
`"unsatisfiable"` verdict (schema/semantic check in isolation, the
declared table edge, the whole-run reorder, and the ladder refusal of a
malformed report):

* The contract accepts `{"verdict": "deferred", "until": <ISO-8601>,
  "reason": <str>}` (`transitions.HAT_SCHEMAS["worker"]`); the enum stays
  closed to everything else.
* `transitions.worker_deferred_report_errors` is the semantic half: a
  non-empty, PARSEABLE `until` and a non-empty `reason` -- `contracts.py`'s
  restricted schema engine has no date-format keyword, so a malformed
  timestamp still passes schema validation and must be caught here.
* `LoopSupervisor._apply_worker_deferred_report` writes `deferredUntil`/
  `deferredReason` onto the assigned task's own record and moves its
  `status` back to `pending` -- never a terminal status -- in one write
  through `RunState.write_tasks`.
* `engine.scheduler.select_next_task` (pinned again, in isolation, in
  `tests/test_worker_hat.py`'s folded scheduler-unit block) excludes any
  `pending` task carrying `deferredUntil` from the ordinary candidate pool
  entirely, so the very next dispatch this module drives through a real
  `LoopSupervisor` picks a DIFFERENT, independent task -- "the picker
  offers the next independent task" -- and the deferred pass itself
  never resets/increments `stagnant` through the ordinary diff formula at
  all (it `continue`s the outer dispatch loop directly, the same
  `dispatch_outcome` mechanism `unsatisfiable_targeted_replan` already
  uses) and never touches `status.json`'s `progressBudget` key (PRD:
  "never spends the task's budget").

Mutation cases (recorded in the commit message): dropping the semantic
check in `transitions.worker_deferred_report_errors` would let a
`reason`-less or unparseable-`until` report through, failing
`test_a_report_with_a_blank_reason_is_a_contract_violation_and_the_move_is_refused`;
dropping the scheduler's `deferredUntil` exclusion would let the picker
re-pick the just-deferred task ahead of the other runnable one, failing
`test_deferring_the_assigned_task_reorders_it_behind_the_other_runnable_task`
(task 002 would never reach `completed`); dropping `dispatch_outcome =
"continue"` for the deferred branch (falling through to the ordinary
`applied_this_iteration`/stagnation-diff bookkeeping instead) would still
likely reset `stagnant` via witness (a) (`deferredUntil` IS a genuine
`tasks.json` change), but would also wrongly run the vigilant
verify sub-cycle bookkeeping against a task that was never actually
completed -- exactly the class of defect `dispatch_outcome` exists to
keep out of a verdict that is a reorder, not a completion.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from ralphd.engine import contracts, transitions
from ralphd.engine.loop import LoopSupervisor
from ralphd.engine.runner import IterationResult
from ralphd.engine.state import RunDir, atomic_write_json
from tests.conftest import scripted_supervisor

TASK_1 = "001"
TASK_2 = "002"

UNTIL = "2099-01-01T00:00:00Z"


def _ok(text: str) -> IterationResult:
    r = IterationResult(exit_code=0)
    r.final_text = text
    r.duration_s = 5.0
    r.usage = {"input": 10, "output": 5, "totalTokens": 15}
    return r


def _deferred(until: str, reason: str) -> IterationResult:
    payload = json.dumps({"verdict": "deferred", "until": until,
                          "reason": reason})
    return _ok(f"this task must wait on a gated dependency.\n{payload}")


def _complete(task_id: str) -> IterationResult:
    payload = json.dumps({"verdict": "complete", "task_id": task_id})
    return _ok(f"task {task_id} done\n{payload}")


def _task(**over) -> dict:
    task = {"id": TASK_1, "title": "wait on a gated window",
            "status": "in-progress", "successCriteria": "the window opened"}
    task.update(over)
    return task


_supervisor = scripted_supervisor(run_id="v20-deferred", iterations=15,
                                  max_approaches=1, vigilant=False,
                                  on_complete="exit",
                                  infra_outage_budget_s=1000.0)


def _run_resumed(root: Path, handlers: dict, tasks: list[dict],
                 **cfg_kw) -> tuple[LoopSupervisor, str]:
    """Same shape as `tests/test_unsatisfiable_criteria.py`'s own helper:
    seed disk state exactly like a resumed run (a task starting
    `in-progress`, a shape a real `plan-proposal.json` contract can never
    hold at ingestion) and skip planning entirely."""
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


def _log_messages(root: Path) -> str:
    return "\n".join(ev.get("message") or "" for ev in _events(root)
                     if ev.get("type") == "log")


# -- the semantic check in isolation ----------------------------------------


def test_a_non_deferred_payload_is_never_in_violation():
    assert transitions.worker_deferred_report_errors(
        {"verdict": "complete"}) == []
    assert transitions.worker_deferred_report_errors("not a dict") == []
    assert transitions.worker_deferred_report_errors(None) == []


def test_a_deferred_report_needs_a_parseable_until_and_a_reason():
    assert transitions.worker_deferred_report_errors(
        {"verdict": "deferred", "until": UNTIL, "reason": "why"}) == []

    missing_until = transitions.worker_deferred_report_errors(
        {"verdict": "deferred", "reason": "why"})
    assert missing_until and "until" in missing_until[0]

    blank_until = transitions.worker_deferred_report_errors(
        {"verdict": "deferred", "until": "   ", "reason": "why"})
    assert blank_until and "until" in blank_until[0]

    unparseable_until = transitions.worker_deferred_report_errors(
        {"verdict": "deferred", "until": "not a timestamp", "reason": "why"})
    assert unparseable_until and "ISO-8601" in unparseable_until[0]

    missing_reason = transitions.worker_deferred_report_errors(
        {"verdict": "deferred", "until": UNTIL})
    assert missing_reason and "reason" in missing_reason[0]

    blank_reason = transitions.worker_deferred_report_errors(
        {"verdict": "deferred", "until": UNTIL, "reason": "  "})
    assert blank_reason and "reason" in blank_reason[0]


def test_the_table_declares_the_deferred_reorder_self_loop():
    guards = transitions.guard_names("worker", vigilant=False)
    assert "worker_deferred_reordered" in guards
    assert transitions.is_legal("worker", "worker", vigilant=False)


def test_the_worker_schema_accepts_deferred_with_until_and_reason():
    errors = contracts.validate_payload(
        "worker",
        {"verdict": "deferred", "until": UNTIL, "reason": "waiting"})
    assert errors == []
    # the enum is still closed -- an unrecognised verdict is still refused
    assert contracts.validate_payload("worker", {"verdict": "postpone"}) != []


# -- the whole run: the reorder, the picker's next choice, both non-effects -


def test_deferring_the_assigned_task_reorders_it_behind_the_other_runnable_task(
        tmp_path):
    """The reorder: task 001's own record moves to `pending` with
    `deferredUntil`/`deferredReason` recorded. The picker's next choice:
    task 002 -- the OTHER runnable task -- is what the very next dispatch
    actually completes, never a re-pick of the just-deferred task 001.
    Both non-effects: `status.json["progressBudget"]` is never written
    (never spends the task's budget) and the approach never bumped or
    archived (never counts as stagnation)."""
    root = tmp_path / "run"
    tasks = [_task(status="in-progress"),
             _task(id=TASK_2, status="pending", title="the other thing",
                   successCriteria="the other thing works")]

    sup, state = _run_resumed(root, {
        "worker": [lambda s, p: _deferred(UNTIL, "waiting on a gated "
                                          "dependency to open"),
                  lambda s, p: _complete(TASK_2)],
        "review": [],
    # v0.20 task 022 (PRD requirement C.1, #156): an `until` past the job's
    # own `deadlineAt` is refused at the contract now -- `UNTIL` (2099) is
    # deliberately "some far future ISO-8601 string" for this module's own
    # purpose (a task that must wait, not a deadline probe), so this one
    # real-supervisor accept path widens `job_timeout_s` far enough that
    # 2099 sits comfortably inside the job's own deadline; the pure
    # schema/semantic checks above never construct a supervisor at all and
    # are unaffected.
    }, tasks, iterations=3, job_timeout_s=10**10)  # _run_resumed seeds iterations_used=1: 2 dispatches

    doc = sup.run.read_tasks()
    by_id = {t["id"]: t for t in doc["tasks"]}

    # the reorder
    assert by_id[TASK_1]["status"] == "pending", (
        "a deferred task must move BEHIND every runnable task, never to a "
        "terminal status")
    assert by_id[TASK_1]["deferredUntil"] == UNTIL
    assert "gated" in by_id[TASK_1]["deferredReason"]

    # the picker's next choice: the OTHER task is what actually completed
    assert by_id[TASK_2]["status"] == "completed"
    assert sup.runner.calls["worker"] == 2      # type: ignore[attr-defined]

    task_events = [ev for ev in _events(root) if ev.get("type") == "task"]
    deferred_events = [ev for ev in task_events if ev.get("taskId") == TASK_1]
    assert any(ev.get("oldStatus") == "in-progress"
              and ev.get("newStatus") == "pending"
              and ev.get("deferredUntil") == UNTIL
              for ev in deferred_events)

    # non-effect 1: never spends the task's progress budget
    assert sup.run.read_status().get("progressBudget") is None

    # non-effect 2: never counts as stagnation -- the approach never
    # bumped and was never archived (task 001's own deferral pass reset
    # `stagnant` directly rather than falling through to the ordinary
    # diff-based formula at all).
    assert sup._approach == 1
    assert not (root / "approaches" / "01").exists()

    # the run ends `exhausted`/`verdict: null` (budget genuinely
    # exhausted -- task 001 is still outstanding, deferred, with no
    # wake-up mechanism yet, task 022's job) rather than "paused"/
    # "aborted"/"failed": no stagnation trip, no contract violation.
    # Task 011 (v0.24 requirement C.1, #169): this plain out-of-budget
    # ending now holds through the same `_finish_exhausted()` path task
    # 010 established.
    assert state == "exhausted", sup.run.read_status()
    assert sup.run.read_status()["verdict"] is None
    assert "contract violation" not in _log_messages(root)


def test_a_report_with_a_blank_reason_is_a_contract_violation_and_the_move_is_refused(
        tmp_path):
    """Same cutover shape as `test_unsatisfiable_criteria.py`'s own
    no-reason case: the illegal move is refused, the task record is
    untouched, and the ladder ran the FULL `contract_retry_max + 1`
    attempts before falling through to the ordinary `no_verdict_yet`
    self-loop -- never a "contract violation" abort."""
    root = tmp_path / "run"
    tasks = [_task(status="in-progress")]
    bad = json.dumps({"verdict": "deferred", "until": UNTIL, "reason": "  "})

    sup, state = _run_resumed(root, {
        "worker": [lambda s, p: _ok(f"must wait\n{bad}")],
        "review": [],
    }, tasks, iterations=2)  # _run_resumed seeds iterations_used=1 already

    task = sup.run.read_tasks()["tasks"][0]
    assert task["status"] == "in-progress", \
        "a contract violation must not silently take the move anyway"
    assert "deferredUntil" not in task

    assert sup.runner.calls["worker"] == sup.cfg.contract_retry_max + 1  # type: ignore[attr-defined]
    events = _events(root)
    violation_events = [ev for ev in events if ev.get("type") == "contract.violation"]
    fault_events = [ev for ev in events if ev.get("type") == "contract.agent_fault"]
    assert len(violation_events) == sup.cfg.contract_retry_max + 1
    assert len(fault_events) == 1
    assert all("reason" in ev["errors"][0] for ev in violation_events)

    assert sup._abort_reason is None
    assert "contract violation" not in _log_messages(root)
    # Task 011 (v0.24 requirement C.1, #169): the ladder exhausted, the
    # run kept going per the table, and the ITERATION budget is what
    # actually ended it -- the plain shape now holds `exhausted`/
    # `verdict: null` through the same `_finish_exhausted()` path task
    # 010 established, never `"failed"`.
    assert state == "exhausted", sup.run.read_status()
    assert sup.run.read_status()["verdict"] is None
