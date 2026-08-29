"""Task 025 (requirement H, #63): a worker can report that a task's criteria
cannot be satisfied AS WRITTEN, with a reason, through its contract -- and the
stagnation guard stays honest about the other direction.

Two acceptance halves, from `docs/prds/v0.9-declarative-loop.md`'s requirement
H:

* A worker's `verdict: "unsatisfiable"` report (`task_id` + `reason`, on the
  contract's final JSON line -- `transitions.HAT_SCHEMAS["worker"]`) is applied
  by the harness (`LoopSupervisor._apply_unsatisfiable_report`): the named task
  moves to `skipped` with `unsatisfiableReason` recorded, in ONE write through
  `RunState.write_tasks` (task 013's sole writer). That write is a genuine
  state change, so it is progress, not the silence the stagnation guard exists
  to catch (`stagnant` resets to 0). The table then routes deterministically
  (`transitions.TABLE["worker"]`): other tasks still actionable keeps the SAME
  approach (`unsatisfiable_targeted_replan`, a worker self-loop); nothing left
  routes to `review` for a verdict (`unsatisfiable_and_nothing_left`), same
  reasoning as the pre-existing `blocked_with_failures` guard.
* A genuinely silent hat -- no report, no state change at all -- still fails
  the approach after three (the pre-existing `stagnation_exhausted` guard,
  proven here NOT to have gained an escape hatch from the new mechanism), and
  the approach-failure event now names the outstanding tasks and their
  statuses directly on the event payload (`outstandingTasks`), not just baked
  into a message string a reader has to parse.

Mutation cases (recorded in the commit message): dropping the
`unsatisfiable_targeted_replan` self-loop and always routing to `review`
instead would still pass every OTHER test in this module but fail
`test_other_actionable_work_keeps_the_same_approach_and_the_worker_loop_continues`
(the approach would bump / review would be entered a run early); dropping the
semantic check in `transitions.worker_unsatisfiable_report_errors` would let a
`reason`-less report through, failing
`test_a_report_with_no_reason_is_a_contract_violation_and_aborts_loudly`;
dropping the `outstandingTasks` field would fail
`test_the_approach_failure_event_names_the_outstanding_tasks_on_its_payload`.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from ralphd.engine import transitions
from ralphd.engine.config import JobConfig
from ralphd.engine.loop import LoopSupervisor
from ralphd.engine.runner import VERIFIED, IterationResult
from ralphd.engine.state import RunDir, atomic_write_json

TASK_1 = "001"
TASK_2 = "002"


# -- scaffolding (mirrors tests/test_failed_task_vocabulary.py) ------------


def _ok(text: str) -> IterationResult:
    r = IterationResult(exit_code=0)
    r.final_text = text
    r.duration_s = 5.0
    r.usage = {"input": 10, "output": 5, "totalTokens": 15}
    return r


def _unsatisfiable(task_id: str, reason: str) -> IterationResult:
    """A worker's final message ending in the task 025 contract line -- a
    valid JSON object on the LAST non-blank line, exactly what
    `contracts.validate_payload("worker", ...)` and
    `LoopSupervisor._apply_unsatisfiable_report` both read."""
    payload = json.dumps({"verdict": "unsatisfiable", "task_id": task_id,
                          "reason": reason})
    return _ok(f"I looked at task {task_id} and its criteria contradict "
              f"each other.\n{payload}")


def _write_plan(sup: LoopSupervisor, tasks: list[dict]) -> None:
    atomic_write_json(sup.run.tasks_file, {"version": 1, "tasks": tasks})


def _task(**over) -> dict:
    task = {"id": TASK_1, "title": "do the impossible thing",
            "status": "in-progress", "successCriteria": "square the circle"}
    task.update(over)
    return task


class _ScriptedAgent:
    """Same shape as `tests/test_failed_task_vocabulary.py`'s: dispatches on
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
    sup = LoopSupervisor(JobConfig(run_id="v09-unsatisfiable", **kw), run, root)
    agent = _ScriptedAgent(handlers)
    agent.sup = sup                      # type: ignore[attr-defined]
    sup.runner = agent                    # type: ignore[assignment]

    async def no_backoff(seconds):
        return seconds, False

    sup._wait_out_backoff = no_backoff    # type: ignore[method-assign]
    return sup


def _run(root: Path, handlers: dict, **cfg_kw) -> tuple[LoopSupervisor, str]:
    RunDir(root=root).update_status(state="starting")
    sup = _supervisor(root, handlers, **cfg_kw)
    return sup, asyncio.run(sup._run_job_core())


def _run_resumed(root: Path, handlers: dict, tasks: list[dict],
                 **cfg_kw) -> tuple[LoopSupervisor, str]:
    """Task 120 (#60): these seeded tasks start `in-progress`/carry fields
    a real plan-proposal.json contract can never hold (a task's own
    `status` is rejected at ingestion) -- exactly what tasks.json
    legitimately looks like on a RESUMED run, so seed disk state that way
    and skip planning entirely, the same way `_resume_point()` decides a
    real engine process should. `handlers` must have no `"planning"` key.
    """
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


def _phases_recorded(root: Path) -> list[str]:
    return [ev.get("phase") for ev in _events(root) if ev.get("type") == "phase"]


# -- the semantic check in isolation ----------------------------------------


def test_a_non_unsatisfiable_payload_is_never_in_violation():
    assert transitions.worker_unsatisfiable_report_errors(
        {"verdict": "complete"}) == []
    assert transitions.worker_unsatisfiable_report_errors("not a dict") == []
    assert transitions.worker_unsatisfiable_report_errors(None) == []


def test_an_unsatisfiable_report_needs_both_task_id_and_reason():
    assert transitions.worker_unsatisfiable_report_errors(
        {"verdict": "unsatisfiable", "task_id": "001", "reason": "why"}) == []
    missing_reason = transitions.worker_unsatisfiable_report_errors(
        {"verdict": "unsatisfiable", "task_id": "001"})
    assert missing_reason and "reason" in missing_reason[0]
    missing_task_id = transitions.worker_unsatisfiable_report_errors(
        {"verdict": "unsatisfiable", "reason": "why"})
    assert missing_task_id and "task_id" in missing_task_id[0]
    blank_reason = transitions.worker_unsatisfiable_report_errors(
        {"verdict": "unsatisfiable", "task_id": "001", "reason": "   "})
    assert blank_reason


def test_the_table_declares_both_routes_for_worker():
    """Requirement H: "the table routes deterministically to review or to a
    targeted replan of that one task, keeping the approach" -- both edges are
    real entries beside `worker`'s other guards, not something only
    `loop.py` knows about."""
    guards = transitions.guard_names("worker", vigilant=False)
    assert "unsatisfiable_targeted_replan" in guards
    assert "unsatisfiable_and_nothing_left" in guards
    assert transitions.is_legal("worker", "worker", vigilant=False)
    assert transitions.is_legal("worker", "review", vigilant=False)


# -- the schema accepts the third verdict -----------------------------------


def test_the_worker_schema_accepts_unsatisfiable_with_task_id_and_reason():
    from ralphd.engine import contracts

    errors = contracts.validate_payload(
        "worker",
        {"verdict": "unsatisfiable", "task_id": "001", "reason": "why"})
    assert errors == []
    # the enum is still closed -- an unrecognised verdict is still refused
    assert contracts.validate_payload("worker", {"verdict": "give_up"}) != []


# -- the whole run, both routes ---------------------------------------------


def _review_verified(sup, prompt=""):
    # Task 119 (#60 pillar 3, fact 9): review reports through the `review`
    # JSON contract, not the retired `<promise>VERIFIED</promise>` sentinel.
    return _ok("the PRD holds for what remains\n"
               + json.dumps({"verdict": "verified"}))


def _review_rejects(sup, prompt=""):
    return _ok("task 001's criteria were never actually met; rejecting")


def test_other_actionable_work_keeps_the_same_approach_and_the_worker_loop_continues(
        tmp_path):
    """The "targeted replan of that one task, keeping the approach" route:
    task 002 is still actionable, so the worker loop stays in `worker`
    (self-loop, `unsatisfiable_targeted_replan`) instead of bumping the
    approach or entering review early."""
    root = tmp_path / "run"

    def _complete_task_2(sup, prompt=""):
        # Task 015 (requirement O, #100): report the harness-owned `complete`
        # verdict instead of writing tasks.json directly and ending with the
        # retired `<promise>COMPLETE</promise>` marker -- with task 001
        # already `skipped` (from the unsatisfiable report above), applying
        # this move leaves `_outstanding_for_completion` empty, so the SAME
        # iteration's `applied_this_iteration` branch (task 116) ends the
        # worker loop.
        return _ok("task 002 done\n" +
                  json.dumps({"verdict": "complete", "task_id": TASK_2}))

    tasks = [_task(status="in-progress"),
             _task(id=TASK_2, status="pending", title="the other thing",
                   successCriteria="the other thing works")]

    sup, state = _run_resumed(root, {
        "worker": [lambda s, p: _unsatisfiable(TASK_1, "the two halves of "
                                              "this criterion contradict"),
                  _complete_task_2],
        "review": [_review_verified],
    }, tasks)

    assert state == "succeeded", sup.run.read_status()
    doc = sup.run.read_tasks()
    by_id = {t["id"]: t for t in doc["tasks"]}
    assert by_id[TASK_1]["status"] == "skipped"
    assert "contradict" in by_id[TASK_1]["unsatisfiableReason"]
    assert by_id[TASK_2]["status"] == "completed"

    # the approach never bumped and was never archived -- this stayed the
    # SAME approach throughout, exactly what "keeping the approach" means.
    # (`approaches/` itself always exists -- part of every run dir's
    # scaffolding, `RunDir`'s own directory setup -- so the real signal is
    # the per-approach archive subdirectory `_archive_approach` creates.)
    assert sup._approach == 1
    assert not (root / "approaches" / "01").exists()

    task_events = [ev for ev in _events(root) if ev.get("type") == "task"]
    assert any(ev.get("taskId") == TASK_1 and ev.get("newStatus") == "skipped"
              and ev.get("unsatisfiableReason") for ev in task_events)
    # worker ran twice (the report, then the completion) before review
    assert sup.runner.calls["worker"] == 2      # type: ignore[attr-defined]
    assert _phases_recorded(root).count("review") >= 1


def test_nothing_left_actionable_routes_to_review_instead_of_stagnating(
        tmp_path):
    """The other route: no OTHER task is actionable once this one is off the
    table, so the harness enters review directly rather than looping the
    worker on a plan with nothing left to do."""
    root = tmp_path / "run"
    tasks = [_task(status="in-progress")]

    sup, state = _run_resumed(root, {
        "worker": [lambda s, p: _unsatisfiable(
            TASK_1, "this criterion assumes hardware this box does not have")],
        "review": [_review_rejects],
    }, tasks, max_approaches=1)

    doc = sup.run.read_tasks()
    task = doc["tasks"][0]
    assert task["status"] == "skipped"
    assert "hardware" in task["unsatisfiableReason"]

    # review really was entered -- the guard routed there, not stagnation
    assert "review" in _phases_recorded(root)
    assert sup.runner.calls["worker"] == 1      # type: ignore[attr-defined]
    assert sup.runner.calls.get("review", 0) >= 1  # type: ignore[attr-defined]
    # rejected with no approach left -> the run ends, not stuck retrying
    assert state in ("failed", "aborted")


def test_a_report_with_no_reason_is_a_contract_violation_with_the_move_still_refused(
        tmp_path):
    """v0.13 task 005 (#92 site 3) strengthens this. The OLD claim, pinned
    here before the cutover, was: `assert state == "aborted"` -- a single
    reported illegal move was fatal on the FIRST offence, with no re-ask
    and no ladder at all, and the run ended loudly with a "contract
    violation" log message.

    The NEW claim keeps the old one's real content -- the illegal move is
    still refused, the task record is still untouched -- and adds what the
    old assertion could not see: the errors were carried back to the SAME
    worker verbatim on each re-ask, the ladder ran the FULL
    `contract_retry_max + 1` attempts through
    `contract_enforcement.enforce_contract` (the report check as its
    `extra_validate`) -- the EXACT SAME retry loop `_step_generic_hat`'s
    sites 1/2 (tasks 003/004) already use for the OTHER four hats' own
    schema/semantic refusals -- and the outcome is an agent-fault
    iteration that continues per the table
    (`T("worker", "no_verdict_yet")`) rather than aborting: `_step_worker`'s
    own `while self.budget_left():` loop, not the ladder, decides when this
    run ends, and it NEVER sets `self._abort_reason` for an exhausted
    ladder -- with the run's own iteration budget already spent by the one
    charged attempt below (`iterations=1`), the run ends `"failed"`
    (unverified, no abort) via the ordinary tail, not `"aborted"`.
    """
    root = tmp_path / "run"
    tasks = [_task(status="in-progress")]
    bad = json.dumps({"verdict": "unsatisfiable", "task_id": TASK_1})

    sup, state = _run_resumed(root, {
        "worker": [lambda s, p: _ok(f"no reason given\n{bad}")],
        "review": [_review_verified],
    }, tasks, iterations=2)  # _run_resumed seeds iterations_used=1 already

    # the move is still refused -- the task record is untouched, exactly
    # like the old claim.
    task = sup.run.read_tasks()["tasks"][0]
    assert task["status"] == "in-progress", \
        "a contract violation must not silently take the move anyway"

    # the errors were carried back on every re-ask: the ladder ran the
    # FULL contract_retry_max + 1 attempts (never fewer, never more) --
    # every call (the one charged `run_iteration` plus every uncharged
    # `_ask_contract_fix` re-ask) lands on the SAME `_ScriptedAgent.run`,
    # so this call count is the ladder's own attempt count directly.
    assert sup.runner.calls["worker"] == sup.cfg.contract_retry_max + 1  # type: ignore[attr-defined]

    events = _events(root)
    violation_events = [ev for ev in events if ev.get("type") == "contract.violation"]
    fault_events = [ev for ev in events if ev.get("type") == "contract.agent_fault"]
    assert len(violation_events) == sup.cfg.contract_retry_max + 1
    assert len(fault_events) == 1, "the ladder must be exhausted exactly once"
    assert all("reason" in ev["errors"][0] for ev in violation_events)

    # agent-fault continuation, NEVER a contract-violation abort: no
    # `_abort_reason`, no "contract violation" log message, and the run
    # ends `"failed"` (budget genuinely exhausted), not `"aborted"`.
    assert sup._abort_reason is None
    msgs = _log_messages(root)
    assert "contract violation" not in msgs
    assert state == "failed", sup.run.read_status()


def test_an_unknown_task_id_exhausts_the_ladder_and_still_refuses_the_move(
        tmp_path):
    """v0.13 task 005: same cutover as the no-reason case above -- an
    unresolvable `task_id` is carried back through the SAME re-ask ladder
    (`"not found"` on every `contract.violation` event, never a `log`
    message the old pre-cutover assertion grepped) rather than aborting on
    the first offence. With no budget ceiling here (`_run_resumed`'s
    default `iterations=15`), the worker loop keeps self-looping on
    `no_verdict_yet` every pass -- three genuinely stuck passes (each its
    own full ladder) still trips the PRE-EXISTING `stagnation_exhausted`
    guard exactly as a silent hat would (this task adds no escape hatch
    from that), ending the run `"failed"`, never `"aborted"`."""
    root = tmp_path / "run"
    tasks = [_task(status="in-progress")]

    sup, state = _run_resumed(root, {
        "worker": [lambda s, p: _unsatisfiable("does-not-exist", "nope")],
        "review": [_review_verified],
    }, tasks)

    assert state == "failed", sup.run.read_status()
    assert sup._abort_reason is None
    assert "contract violation" not in _log_messages(root)
    events = _events(root)
    violation_events = [ev for ev in events if ev.get("type") == "contract.violation"]
    assert violation_events, "the violation must still be recorded, just not aborted over"
    assert all("not found" in ev["errors"][0] for ev in violation_events)
    task = sup.run.read_tasks()["tasks"][0]
    assert task["status"] == "in-progress", \
        "a contract violation must not silently take the move anyway"


# -- the other direction: a genuinely silent hat still fails after three ---


def test_a_genuinely_silent_hat_still_fails_the_approach_after_three(
        tmp_path):
    """Requirement H's own guardrail against itself: a worker that reports
    NOTHING and changes NOTHING must not be rescued by the new mechanism --
    it still exhausts the pre-existing stagnation guard exactly as before."""
    root = tmp_path / "run"
    tasks = [_task(status="in-progress")]

    sup, state = _run_resumed(root, {
        "worker": [lambda s, p: _ok("I looked at this and made no progress")],
        "review": [_review_verified],
    }, tasks)

    assert state == "failed", sup.run.read_status()
    assert sup.runner.calls["worker"] == 3      # type: ignore[attr-defined]
    task = sup.run.read_tasks()["tasks"][0]
    assert task["status"] == "in-progress", \
        "the silent hat must not have moved the task at all"
    assert "review" not in _phases_recorded(root)


def test_the_approach_failure_event_names_the_outstanding_tasks_on_its_payload(
        tmp_path):
    """Requirement H: "The approach-failure event names the outstanding
    tasks and their statuses" -- asserted on the EVENT PAYLOAD (a field a
    reader can consume directly), not just grepped out of a message string."""
    root = tmp_path / "run"
    tasks = [_task(status="in-progress"),
             _task(id=TASK_2, status="pending", title="another one")]

    sup, state = _run_resumed(root, {
        "worker": [lambda s, p: _ok("stuck, no progress made")],
        "review": [_review_verified],
    }, tasks)

    assert state == "failed"
    failure_events = [
        ev for ev in _events(root)
        if ev.get("type") == "log" and ev.get("level") == "error"
        and "failing approach" in (ev.get("message") or "")
    ]
    assert len(failure_events) == 1
    outstanding = failure_events[0]["outstandingTasks"]
    assert {"id": TASK_1, "status": "in-progress"} in outstanding
    assert {"id": TASK_2, "status": "pending"} in outstanding
    assert TASK_1 in failure_events[0]["message"]
    assert TASK_2 in failure_events[0]["message"]
