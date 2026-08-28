"""v0.13 task 012 (#92, requirement G's second half): surface
`contract.violation`/`contract.agent_fault` through `ralphctl status`' own
`reason:` line and `ralphctl iteration N`, for a hat that recovers as well
as one that never does -- through ONE shared derivation, never two
hand-kept strings.

The shared derivation is `contract_enforcement.violation_message(hat,
errors)`:

  * `LoopSupervisor._step_generic_hat` (loop.py) now calls it to build
    `self._abort_reason` the moment the retry ladder gives up WHILE the
    run's own budget is also gone -- this is what lands in `status.json`'s
    `reason` field and therefore `ralphctl status`' `reason:` line.
  * `state.contract_fault_for_iteration` calls the SAME function to
    reconstruct the identical sentence from `EVENT_VIOLATION`/
    `EVENT_AGENT_FAULT` events for one specific iteration, wired into
    `iteration_detail`'s `contractFault` key and rendered by
    `iteration_summary_lines` as `ralphctl iteration N`'s own `contract:`
    line -- for an iteration whose ladder recovered (no agent fault, the
    run carries on) exactly as for one whose ladder never did.

This module pins three things:

1. `violation_message`'s own wording (unit).
2. `loop.py`'s abort path calls THROUGH `violation_message` rather than
   keeping an independent copy -- proved two ways: an exact-value assertion
   against the real validator's own errors, AND a monkeypatch that replaces
   `violation_message` with a sentinel and shows `_abort_reason` tracks it
   (the automated form of this repo's "hand-verified mutation" discipline --
   a hand-kept duplicate in `loop.py` would leave the sentinel test
   failing, since patching the shared function would not touch it).
3. `state.contract_fault_for_iteration`/`iteration_detail`/
   `iteration_summary_lines` reconstruct the identical text for BOTH a
   recovering and an exhausting real `LoopSupervisor._step_worker()` run
   (stubbed `sup.runner` only, `events.jsonl` read back off disk -- task
   011's own technique), joined to the specific iteration number with no
   new persisted field.

`ralphctl status`' `reason:` line itself is unit-level (`_format_reason_
lines` already renders any non-empty `status.json["reason"]` verbatim, task
003) -- `tests/test_cli_status_summaries.py` covers that renderer; what is
new here is that the TEXT it is handed, for a contract-violation abort, now
comes from this one shared function instead of an inline `loop.py` f-string.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from ralphd.engine import contract_enforcement as ce
from ralphd.engine import transitions as tr
from ralphd.engine.config import JobConfig
from ralphd.engine.loop import LoopSupervisor
from ralphd.engine.runner import IterationResult
from ralphd.engine.state import (
    RunDir,
    atomic_write_json,
    contract_fault_for_iteration,
    iteration_detail,
    iteration_summary_lines,
    read_events,
)

TEST_STATE = "task012_contract_hat_state"
TEST_HAT = "task012_contract_hat"

# A missing required field -- the same "wrong shape" ordinary case
# `contract_enforcement`'s own module docstring names, distinct from
# tasks 009/010/011's `deck-phase3g` int-vs-string shape so this module
# does not just re-run their fixture under a new name.
BAD_PAYLOAD = json.dumps({})
EXPECTED_ERRORS = ["$: missing required field 'note'"]
GOOD_PAYLOAD = json.dumps({"note": "ok"})


def _ok(text: str) -> IterationResult:
    r = IterationResult(exit_code=0)
    r.final_text = text
    r.duration_s = 1.0
    r.usage = {"input": 1, "output": 1, "totalTokens": 2}
    return r


def _supervisor(tmp_path, **cfg_kw) -> LoopSupervisor:
    run = RunDir(root=tmp_path)
    kw = {"infra_retry_backoff_s": [0.0], "infra_retry_backoff_max_s": 0.0,
          "infra_outage_budget_s": 1000.0, "vigilant": False,
          "max_approaches": 2, "on_complete": "exit", **cfg_kw}
    cfg = JobConfig(run_id="task012-contract", **kw)
    return LoopSupervisor(cfg, run, tmp_path)


def _stub_attempts(sup: LoopSupervisor, results: list[IterationResult]) -> list[str]:
    """Same technique `tests/test_interrupt_not_substantive_outcome_task004.
    py`'s `_stub_attempts` uses: stub `_run_iteration_once` (below the
    infra-retry wrapper) and charge `iterations_used` exactly as the real
    method does, so `budget_left()` genuinely runs out."""
    calls: list[str] = []

    async def fake_once(phase, extra="", prompt_name=None, tier=None):
        calls.append(phase)
        sup.iterations_used += 1
        return results[min(len(calls) - 1, len(results) - 1)]

    sup._run_iteration_once = fake_once  # type: ignore[method-assign]
    return calls


@pytest.fixture
def registered_test_hat():
    assert TEST_STATE not in tr.TABLE, "test fixture name collided with a real state"
    tr.TABLE[TEST_STATE] = (tr.T("planning", "task012_contract_done"),)
    tr.STATE_HAT[TEST_STATE] = TEST_HAT
    tr.HAT_SCHEMAS[TEST_HAT] = {
        "type": "object", "properties": {"note": {"type": "string"}},
        "required": ["note"], "additionalProperties": False}
    try:
        yield TEST_STATE
    finally:
        tr.TABLE.pop(TEST_STATE, None)
        tr.STATE_HAT.pop(TEST_STATE, None)
        tr.HAT_SCHEMAS.pop(TEST_HAT, None)


# ---------------------------------------------------------------------------
# 1. the wording itself
# ---------------------------------------------------------------------------

def test_violation_message_wording():
    assert ce.violation_message("worker", ["x"]) == "'worker' contract violation: x"
    assert (ce.violation_message("worker", ["a", "b"])
            == "'worker' contract violation: a; b")


# ---------------------------------------------------------------------------
# 2. loop.py's abort path goes THROUGH the shared function
# ---------------------------------------------------------------------------

def test_abort_reason_equals_the_shared_derivations_own_output(
        tmp_path, registered_test_hat):
    """The exhausted-ladder, budget-gone abort path builds `_abort_reason`
    from the validator's own real errors, through `violation_message` --
    not a re-typed copy of its format."""
    sup = _supervisor(tmp_path, iterations=1)  # exhausted after one pass
    _stub_attempts(sup, [_ok(BAD_PAYLOAD)])  # every attempt, forever
    # `enforce_contract`'s re-asks beyond attempt 0 go through
    # `_ask_contract_fix`, which calls `self.runner.run` directly (never
    # `_run_iteration_once`) -- stub it too, or the second attempt hangs
    # trying to reach a real agent.
    sup.runner = _ScriptedWorker([_ok(BAD_PAYLOAD)])  # type: ignore[assignment]
    edges = tr.successors(TEST_STATE, sup.cfg.vigilant)

    outcome = asyncio.run(sup._step_generic_hat(TEST_STATE, edges))

    assert outcome == ("tail",)
    assert sup._abort_reason == ce.violation_message(TEST_HAT, EXPECTED_ERRORS)
    assert "contract violation" in sup._abort_reason  # #92's own pinned substring


def test_mutating_the_shared_wording_changes_the_abort_reason(
        tmp_path, registered_test_hat, monkeypatch):
    """The automated form of this repo's "hand-verified mutation": replace
    `contract_enforcement.violation_message` with a sentinel and show
    `_abort_reason` tracks it exactly. A `loop.py` that kept its own
    hand-typed copy of the wording (the pre-task-012 shape) would leave
    this sentinel untouched and the assertion below would fail."""
    monkeypatch.setattr(ce, "violation_message",
                        lambda hat, errors: "SENTINEL-CONTRACT-TEXT")
    sup = _supervisor(tmp_path, iterations=1)
    _stub_attempts(sup, [_ok(BAD_PAYLOAD)])
    sup.runner = _ScriptedWorker([_ok(BAD_PAYLOAD)])  # type: ignore[assignment]
    edges = tr.successors(TEST_STATE, sup.cfg.vigilant)

    asyncio.run(sup._step_generic_hat(TEST_STATE, edges))

    assert sup._abort_reason == "SENTINEL-CONTRACT-TEXT"


# ---------------------------------------------------------------------------
# 3. the events-to-iteration join, both the recovering and exhausting shape,
#    driven through the real worker call path (task 011's own technique).
# ---------------------------------------------------------------------------

class _ScriptedWorker:
    def __init__(self, script: list[IterationResult]):
        self.script = script
        self.calls = 0

    def interrupt(self) -> bool:
        return False

    async def run(self, prompt, transcript, **kw) -> IterationResult:
        step = self.script[min(self.calls, len(self.script) - 1)]
        self.calls += 1
        return step


def _worker_run(tmp_path, name: str, *, iterations: int = 10):
    root = tmp_path / name
    root.mkdir(parents=True)
    run = RunDir(root=root)
    run.update_status(state="running")
    atomic_write_json(run.tasks_file, {"version": 1, "tasks": [
        {"id": "1", "title": "prove the join reconstructs the sentence",
         "status": "in-progress", "successCriteria": "n/a"},
    ]})
    cfg = JobConfig(run_id=f"task012-{name}", iterations=iterations,
                    max_approaches=3, vigilant=False, on_complete="exit",
                    infra_retry_backoff_s=[0.0], infra_retry_backoff_max_s=0.0,
                    infra_outage_budget_s=1000.0)
    sup = LoopSupervisor(cfg, run, root)
    sup._approach = 1
    return sup, run


def test_recovering_iteration_reconstructs_the_same_sentence_no_agent_fault(tmp_path):
    sup, run = _worker_run(tmp_path, "recovers")
    bad = json.dumps({"verdict": "complete", "task_id": 1})  # int, not str
    good = json.dumps({"verdict": "complete", "task_id": "1"})
    agent = _ScriptedWorker([_ok(bad), _ok(good)])
    sup.runner = agent  # type: ignore[assignment]

    asyncio.run(sup._step_worker())

    errors = ["$.task_id: expected string, got int"]
    expected_text = ce.violation_message("worker", errors)

    cf = contract_fault_for_iteration(run.root, 1)
    assert cf is not None
    assert cf["hat"] == "worker"
    assert cf["errors"] == errors
    assert cf["agentFault"] is False
    assert cf["text"] == expected_text

    detail = iteration_detail(run.root, 1)
    assert detail["contractFault"] == cf
    lines = iteration_summary_lines(detail)
    assert f"contract:  {expected_text}" in lines

    # no OTHER iteration is falsely attributed the same fault
    assert contract_fault_for_iteration(run.root, 2) is None


def test_exhausting_iteration_reconstructs_the_same_sentence_with_agent_fault(tmp_path):
    # iterations=1: the outer `_step_worker` budget loop charges exactly one
    # pass (task 009/011's own gotcha -- a larger budget keeps retrying
    # whole passes across the stagnation guard before returning).
    sup, run = _worker_run(tmp_path, "exhausts", iterations=1)
    bad = json.dumps({"verdict": "complete", "task_id": 1})
    agent = _ScriptedWorker([_ok(bad)])  # every attempt, forever
    sup.runner = agent  # type: ignore[assignment]

    asyncio.run(sup._step_worker())

    errors = ["$.task_id: expected string, got int"]
    expected_text = ce.violation_message("worker", errors)

    cf = contract_fault_for_iteration(run.root, 1)
    assert cf is not None
    assert cf["hat"] == "worker"
    assert cf["errors"] == errors
    assert cf["agentFault"] is True
    assert cf["text"] == expected_text

    detail = iteration_detail(run.root, 1)
    assert detail["contractFault"] == cf
    lines = iteration_summary_lines(detail)
    assert f"contract:  {expected_text}" in lines

    # the ladder genuinely ran out and left its own agent-fault event
    assert [e for e in read_events(run.root) if e.get("type") == ce.EVENT_AGENT_FAULT]


def test_an_iteration_with_no_violation_at_all_reports_none(tmp_path):
    sup, run = _worker_run(tmp_path, "clean")
    agent = _ScriptedWorker(
        [_ok(json.dumps({"verdict": "complete", "task_id": "1"}))])
    sup.runner = agent  # type: ignore[assignment]

    asyncio.run(sup._step_worker())

    assert contract_fault_for_iteration(run.root, 1) is None
    detail = iteration_detail(run.root, 1)
    assert detail["contractFault"] is None
    assert not any(line.startswith("contract:  ")
                  for line in iteration_summary_lines(detail))
