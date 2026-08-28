"""v0.13 task 011 (#92): prove `contract.violation`/`contract.agent_fault`
are emitted from the LIVE path -- a real `LoopSupervisor._step_worker`
call, stubbed `sup.runner` only -- not merely from
`contract_enforcement.enforce_contract` in isolation (already covered by
`test_contract_enforcement.py`) and not merely as an incidental side
assertion inside a larger end-to-end fixture (tasks 008/009/010's own
modules each check *some* events.jsonl shape, but none pins the FULL per-
attempt event shape -- `hat`, `attempt`, `maxAttempts`, `errors` -- by name
against `contract_enforcement.EVENT_VIOLATION`/`EVENT_AGENT_FAULT` rather
than the literal strings those constants happen to equal today).

Two cases, two independent run directories:

- `test_a_recovering_violation_emits_one_violation_event_and_no_fault`:
  the worker's first attempt is a schema violation (`task_id` as a JSON
  int where the contract requires a string -- the exact `deck-phase3g`
  shape tasks 009/010 already used), the second (the re-ask) is corrected.
  Exactly one `EVENT_VIOLATION` on disk, carrying `hat="worker"`,
  `attempt=1`, `maxAttempts=contract_retry_max+1` and the verbatim
  `errors` list; zero `EVENT_AGENT_FAULT` events -- the ladder recovered,
  so there is no fault to report.
- `test_an_exhausted_ladder_emits_one_violation_per_attempt_and_one_fault`:
  every attempt repeats the SAME violation, so the ladder runs its full
  `contract_retry_max + 1` attempts and never recovers. Exactly
  `contract_retry_max + 1` `EVENT_VIOLATION` events on disk, one per
  attempt, `attempt` counting up `1..maxAttempts` and `maxAttempts`/`hat`/
  `errors` identical across all of them; exactly ONE `EVENT_AGENT_FAULT`
  event (never more, never fewer) once the ladder gives up.

Both cases read `events.jsonl` straight off disk via `state.read_events`
(not from any in-memory return value) -- `enforce_contract`'s own
docstring promise is that every violation is written "at the moment it is
discovered... so a run that dies mid-retry still leaves the violation on
disk", and this is what proves that promise against the real
`LoopSupervisor` call path rather than trusting it by construction.

Hand-verified mutation: swapping either assertion's event-type filter from
`contract_enforcement.EVENT_VIOLATION`/`EVENT_AGENT_FAULT` to a near-miss
literal (`"contract.violations"`, plural) makes both filters return empty
lists and the length assertions fail immediately -- confirming the test
actually exercises the constant rather than vacuously passing on an empty
list either way.
"""

from __future__ import annotations

import asyncio
import json

from ralphd.engine import state
from ralphd.engine.config import JobConfig
from ralphd.engine.contract_enforcement import EVENT_AGENT_FAULT, EVENT_VIOLATION
from ralphd.engine.loop import LoopSupervisor
from ralphd.engine.runner import IterationResult
from ralphd.engine.state import RunDir, atomic_write_json

TASK_ID = "1"

# The `deck-phase3g` shape: `task_id` as a JSON int where the worker
# contract's schema requires a string (`transitions.HAT_SCHEMAS["worker"]
# ["properties"]["task_id"]` is `{"type": "string"}`).
BAD_PAYLOAD = json.dumps({"verdict": "complete", "task_id": 1})
GOOD_PAYLOAD = json.dumps({"verdict": "complete", "task_id": TASK_ID})
VALIDATION_ERROR = "$.task_id: expected string, got int"


def _ok(text: str) -> IterationResult:
    r = IterationResult(exit_code=0)
    r.final_text = text
    r.duration_s = 5.0
    r.usage = {"input": 10, "output": 5, "totalTokens": 15}
    return r


class _ScriptedWorker:
    """Replays a fixed sequence of `IterationResult`s, one per call, the
    last one repeated forever -- the ladder's own `respond` shape, driven
    through the real `LoopSupervisor.runner` seam."""

    def __init__(self, script: list[IterationResult]):
        self.script = script
        self.calls = 0

    def interrupt(self) -> bool:
        return False

    async def run(self, prompt, transcript, **kw) -> IterationResult:
        step = self.script[min(self.calls, len(self.script) - 1)]
        self.calls += 1
        return step


def _supervisor(tmp_path, name: str, *, iterations: int = 10
                 ) -> tuple[LoopSupervisor, RunDir]:
    root = tmp_path / name
    root.mkdir(parents=True)
    run = RunDir(root=root)
    run.update_status(state="running")
    atomic_write_json(run.tasks_file, {"version": 1, "tasks": [
        {"id": TASK_ID, "title": "prove the events land on disk",
         "status": "in-progress", "successCriteria": "n/a"},
    ]})
    cfg = JobConfig(run_id=f"task011-{name}", iterations=iterations,
                    max_approaches=3, vigilant=False, on_complete="exit",
                    infra_retry_backoff_s=[0.0], infra_retry_backoff_max_s=0.0,
                    infra_outage_budget_s=1000.0)
    sup = LoopSupervisor(cfg, run, root)
    sup._approach = 1
    return sup, run


def test_a_recovering_violation_emits_one_violation_event_and_no_fault(tmp_path):
    sup, run = _supervisor(tmp_path, "recovers")
    agent = _ScriptedWorker([_ok(BAD_PAYLOAD), _ok(GOOD_PAYLOAD)])
    sup.runner = agent  # type: ignore[assignment]

    asyncio.run(sup._step_worker())

    events = state.read_events(run.root)
    violations = [e for e in events if e.get("type") == EVENT_VIOLATION]
    faults = [e for e in events if e.get("type") == EVENT_AGENT_FAULT]

    assert len(violations) == 1, violations
    v = violations[0]
    assert v["hat"] == "worker"
    assert v["attempt"] == 1
    assert v["maxAttempts"] == sup.cfg.contract_retry_max + 1
    assert v["errors"] == [VALIDATION_ERROR]

    assert faults == [], "the ladder recovered; there must be no agent-fault event"

    # the corrected re-ask genuinely landed -- this was not a vacuous pass.
    assert agent.calls == 2
    task = run.read_tasks()["tasks"][0]
    assert task["status"] == "completed", task


def test_an_exhausted_ladder_emits_one_violation_per_attempt_and_one_fault(tmp_path):
    # `iterations=1`: the outer `_step_worker` `while self.budget_left():`
    # loop (a SEPARATE thing from `enforce_contract`'s own inner retry
    # ladder) charges one iteration per pass and would otherwise keep
    # retrying whole passes across several stagnation-guard turns before
    # returning (task 009's own fixture needed FOUR charged passes to
    # reach `stagnation_exhausted`) -- capping the budget at exactly one
    # charged iteration means this call makes exactly one pass, so the
    # events on disk are exactly one ladder's worth: `contract_retry_max
    # + 1` violations and one fault, nothing folded in from a second pass.
    sup, run = _supervisor(tmp_path, "exhausts", iterations=1)
    agent = _ScriptedWorker([_ok(BAD_PAYLOAD)])   # every attempt, forever
    sup.runner = agent  # type: ignore[assignment]

    asyncio.run(sup._step_worker())

    events = state.read_events(run.root)
    violations = [e for e in events if e.get("type") == EVENT_VIOLATION]
    faults = [e for e in events if e.get("type") == EVENT_AGENT_FAULT]

    max_attempts = sup.cfg.contract_retry_max + 1
    assert len(violations) == max_attempts, violations
    for i, v in enumerate(violations, start=1):
        assert v["hat"] == "worker"
        assert v["attempt"] == i
        assert v["maxAttempts"] == max_attempts
        assert v["errors"] == [VALIDATION_ERROR]

    assert len(faults) == 1, faults
    f = faults[0]
    assert f["hat"] == "worker"
    assert f["errors"] == [VALIDATION_ERROR]

    # the ladder genuinely ran out -- never fewer, never more attempts.
    assert agent.calls == max_attempts
    # the violation never took the move anyway.
    task = run.read_tasks()["tasks"][0]
    assert task["status"] == "in-progress", task
