"""Task 015 (PRD requirement O, #100): give the worker contract a
plan-complete verdict value and retire the literal `<promise>COMPLETE</promise>`
marker.

Three things this module pins directly, at the unit level, that
`tests/test_promise_marker_last_line.py`'s full-loop scenarios exercise
end-to-end but do not isolate on their own:

1. **The schema.** `HAT_SCHEMAS["worker"]`'s `verdict` enum carries
   `"plan_complete"` alongside `"complete"`/`"no_verdict"`/`"unsatisfiable"`
   -- a distinct name from the PER-TASK `"complete"` claim, so a payload can
   never be misread as one or the other.
2. **`_apply_worker_plan_complete_report`.** The two independent refusal
   reasons -- structural (a task WAS assigned this iteration) and semantic
   (something in the freshly re-read plan is not `completed`/`skipped`/
   `validated`) -- cross-checked against `LoopSupervisor.
   _outstanding_for_completion` exactly as the retired marker's own
   `honoured_complete` cross-check used to be.
3. **The ladder.** A malformed/refused `plan_complete` attempt reaches
   `contract_enforcement.enforce_contract`'s re-ask ladder -- the SAME shape
   tasks 005/006/007 already gave the worker's other three verdict sites --
   never a silent no-progress iteration the way the retired marker's own
   refusal (a bare log message, no ladder, no `contract.violation` event)
   used to be. Mirrors `tests/test_worker_verdict_schema_error_task007.py`'s
   own exhaustion-pinning shape for site 5.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from ralphd.engine import contracts
from ralphd.engine.config import JobConfig
from ralphd.engine.loop import LoopSupervisor
from ralphd.engine.runner import IterationResult
from ralphd.engine.state import RunDir, atomic_write_json
from tests.conftest import supervisor_factory

TASK_1 = "001"
TASK_2 = "002"


def _ok(text: str) -> IterationResult:
    r = IterationResult(exit_code=0)
    r.final_text = text
    r.duration_s = 5.0
    r.usage = {"input": 10, "output": 5, "totalTokens": 15}
    return r


def _task(id_=TASK_1, **over) -> dict:
    task = {"id": id_, "title": "do the thing",
            "status": "pending", "successCriteria": "the thing works"}
    task.update(over)
    return task


# -- the schema ---------------------------------------------------------


def test_the_schema_accepts_the_bare_plan_complete_verdict():
    assert contracts.validate_payload("worker", {"verdict": "plan_complete"}) == []


def test_the_schema_still_recognises_the_other_three_verdicts():
    for verdict in ("complete", "no_verdict", "unsatisfiable"):
        payload = {"verdict": verdict}
        if verdict == "complete":
            payload["task_id"] = TASK_1
        if verdict == "unsatisfiable":
            payload.update(task_id=TASK_1, reason="contradictory criteria")
        assert contracts.validate_payload("worker", payload) == [], verdict


def test_the_schema_rejects_plan_complete_misspelled():
    errors = contracts.validate_payload("worker", {"verdict": "plan-complete"})
    assert errors != []


def test_plan_complete_is_a_name_distinct_from_the_per_task_complete():
    # Task 015: deliberately NOT named "complete" -- the per-task claim
    # above already owns that name, so a payload can never be misread as
    # one or the other.
    from ralphd.engine.transitions import HAT_SCHEMAS
    enum = HAT_SCHEMAS["worker"]["properties"]["verdict"]["enum"]
    assert "plan_complete" in enum
    assert enum.count("complete") == 1
    assert enum.count("plan_complete") == 1


# -- _apply_worker_plan_complete_report -----------------------------------


_supervisor = supervisor_factory(run_id="v015-plan-complete-unit", cfg_defaults=False,
                                 iterations=5, max_approaches=1, vigilant=False,
                                 on_complete="exit")


def test_a_task_assigned_this_iteration_is_refused_structurally(tmp_path):
    sup = _supervisor(tmp_path / "run")
    errors = sup._apply_worker_plan_complete_report(
        {"verdict": "plan_complete"}, TASK_1, {"version": 1, "tasks": []})
    assert errors
    assert "only valid when no task was assigned" in errors[0]
    assert repr(TASK_1) in errors[0]


def test_nothing_assigned_but_outstanding_work_is_refused_semantically(tmp_path):
    sup = _supervisor(tmp_path / "run")
    doc = {"version": 1, "tasks": [_task(TASK_1, status="completed"),
                                   _task(TASK_2, status="pending")]}
    errors = sup._apply_worker_plan_complete_report(None, None, doc)
    assert errors
    assert "not completed or skipped" in errors[0]
    assert TASK_2 in errors[0]
    assert TASK_1 not in errors[0]


def test_nothing_assigned_and_nothing_outstanding_is_accepted(tmp_path):
    sup = _supervisor(tmp_path / "run")
    doc = {"version": 1, "tasks": [_task(TASK_1, status="completed"),
                                   _task(TASK_2, status="skipped")]}
    assert sup._apply_worker_plan_complete_report(None, None, doc) == []


def test_a_validated_task_counts_the_same_as_completed_or_skipped(tmp_path):
    # `_outstanding_for_completion` recognises `state.TASK_COMPLETION_STATUSES`
    # (task 019, requirement F), which -- unlike worker.md's own prose --
    # already includes the vigilant mini-FSM's `validated` terminal.
    sup = _supervisor(tmp_path / "run")
    doc = {"version": 1, "tasks": [_task(TASK_1, status="validated")]}
    assert sup._apply_worker_plan_complete_report(None, None, doc) == []


def test_a_failed_task_blocks_the_claim_forever(tmp_path):
    sup = _supervisor(tmp_path / "run")
    doc = {"version": 1, "tasks": [_task(TASK_1, status="failed")]}
    errors = sup._apply_worker_plan_complete_report(None, None, doc)
    assert errors and TASK_1 in errors[0] and "failed" in errors[0]


def test_an_empty_or_unreadable_plan_is_accepted(tmp_path):
    """Refusing here would strand a run with no plan at all in the
    stagnation guard with no reviewer ever looking at it -- matches
    `_outstanding_for_completion`'s own fail-open contract."""
    sup = _supervisor(tmp_path / "run")
    assert sup._apply_worker_plan_complete_report(None, None, {}) == []


# -- the ladder: a malformed/refused attempt is never silent -------------


class _ScriptedAgent:
    """Same shape as `tests/test_worker_verdict_schema_error_task007.py`'s:
    one script per phase, dispatched by the phase status.json records --
    a contract-fix re-ask lands on the SAME phase ("worker" never changes
    during one), so it hits the SAME step list, one call further in."""

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


def _run_resumed(root: Path, handlers: dict, tasks: list[dict],
                 **cfg_kw) -> tuple[LoopSupervisor, str]:
    RunDir(root=root).update_status(state="starting")
    root.mkdir(parents=True, exist_ok=True)
    run = RunDir(root=root)
    kw = {"iterations": 4, "max_approaches": 1, "vigilant": False,
          "on_complete": "exit", "infra_retry_backoff_s": [0.0],
          "infra_retry_backoff_max_s": 0.0, "infra_outage_budget_s": 1000.0,
          **cfg_kw}
    sup = LoopSupervisor(JobConfig(run_id="v015-plan-complete-ladder", **kw),
                         run, root)
    agent = _ScriptedAgent(handlers)
    agent.sup = sup                      # type: ignore[attr-defined]
    sup.runner = agent                    # type: ignore[assignment]

    async def no_backoff(seconds):
        return seconds, False

    sup._wait_out_backoff = no_backoff    # type: ignore[method-assign]
    atomic_write_json(sup.run.tasks_file, {"version": 1, "tasks": tasks})
    sup.iterations_used = 1
    return sup, asyncio.run(sup._run_job_core())


def _events(root: Path) -> list[dict]:
    p = root / "events.jsonl"
    if not p.exists():
        return []
    return [json.loads(x) for x in p.read_text().splitlines() if x.strip()]


def test_an_unearned_plan_complete_claim_exhausts_the_ladder_and_is_refused(tmp_path):
    """A worker with an assigned task claiming the WHOLE plan is done in the
    same breath -- structurally illegal -- is carried back through
    `contract_enforcement.enforce_contract`'s ladder, never read as a
    silent no-progress iteration the retired marker's own bare-log refusal
    used to be. The move is refused (the task record never moves off
    `pending`); the ladder runs the FULL `contract_retry_max + 1` attempts;
    and the run continues per the table (the undeclared `worker` self-loop)
    rather than aborting outright."""
    root = tmp_path / "run"
    tasks = [_task(TASK_1, status="pending")]

    def claim_it(sup, prompt):
        return _ok("nothing to do here\n" + json.dumps({"verdict": "plan_complete"}))

    sup, state = _run_resumed(root, {"worker": [claim_it]}, tasks, iterations=2)

    task = sup.run.read_tasks()["tasks"][0]
    assert task["status"] == "in-progress", \
        "a contract violation must not silently take the move anyway"

    assert sup.runner.calls["worker"] == sup.cfg.contract_retry_max + 1  # type: ignore[attr-defined]

    events = _events(root)
    violation_events = [ev for ev in events if ev.get("type") == "contract.violation"]
    fault_events = [ev for ev in events if ev.get("type") == "contract.agent_fault"]
    assert len(violation_events) == sup.cfg.contract_retry_max + 1
    assert len(fault_events) == 1, "the ladder must be exhausted exactly once"
    all_errors = " ".join(" ".join(ev.get("errors", [])) for ev in violation_events)
    assert "only valid when no task was assigned" in all_errors, all_errors

    assert sup._abort_reason is None
    assert not [ev for ev in events if ev.get("type") == "signal"
                and ev.get("signal") == "COMPLETE"], \
        "an unearned plan_complete is never signalled COMPLETE"
    assert state == "failed", sup.run.read_status()


def test_a_schema_malformed_plan_complete_line_also_reaches_the_ladder(tmp_path):
    """Not just the semantic refusal above -- a `plan_complete` payload
    the schema itself would already reject on shape (an unexpected extra
    field the closed schema forbids) reaches the SAME ladder as the
    generic `elif schema_errors:` site (v0.13 task 007), never a
    special-cased abort for this one new value."""
    root = tmp_path / "run"
    tasks = [_task(TASK_1, status="pending")]

    def bad_shape(sup, prompt):
        payload = json.dumps({"verdict": "plan_complete", "bogus_field": True})
        return _ok("all done\n" + payload)

    sup, state = _run_resumed(root, {"worker": [bad_shape]}, tasks, iterations=2)

    assert sup.runner.calls["worker"] == sup.cfg.contract_retry_max + 1  # type: ignore[attr-defined]
    events = _events(root)
    fault_events = [ev for ev in events if ev.get("type") == "contract.agent_fault"]
    assert len(fault_events) == 1
    assert not [ev for ev in events if ev.get("type") == "signal"
                and ev.get("signal") == "COMPLETE"]
    assert state == "failed", sup.run.read_status()


def test_an_earned_plan_complete_after_a_refused_attempt_still_ends_the_run(tmp_path):
    """The ladder's re-ask channel gets a genuine second chance: the SAME
    worker corrects itself on the re-ask (`_ask_contract_fix`'s reply) and
    the claim is honoured -- `contract_enforcement.enforce_contract`
    treats a corrected payload exactly like a first-try success."""
    root = tmp_path / "run"
    tasks = [_task(TASK_1, status="completed")]  # nothing outstanding

    calls = {"n": 0}

    def flaky_then_fixed(sup, prompt):
        calls["n"] += 1
        if calls["n"] == 1:
            # the real dispatch: a schema-invalid `plan_complete` (an
            # unexpected extra field) -- attempt 0 of the ladder reuses
            # this text verbatim and fails.
            return _ok(json.dumps({"verdict": "plan_complete", "oops": 1}))
        # the `_ask_contract_fix` re-ask's reply: corrected.
        return _ok(json.dumps({"verdict": "plan_complete"}))

    sup, state = _run_resumed(root, {
        "worker": [flaky_then_fixed],
        "review": [lambda s, p: _ok(json.dumps({"verdict": "verified"}))],
    }, tasks)

    events = _events(root)
    assert [ev for ev in events if ev.get("type") == "contract.violation"], \
        "the first, malformed attempt is still on the record"
    assert not [ev for ev in events if ev.get("type") == "contract.agent_fault"], \
        "the re-ask fixed it before the ladder exhausted"
    assert [ev for ev in events
            if ev.get("type") == "signal" and ev.get("signal") == "COMPLETE"], \
        "the corrected claim is honoured and still signals COMPLETE"
    assert (state, sup.run.read_status()["verdict"]) == ("succeeded", "verified")
