"""Feature module for the subject below (task 028, requirement D.7): the
per-task pin modules listed here are folded in whole -- every test body,
assertion and docstring verbatim, only the module they live in changed.
Sources absorbed (each was `tests/<name>`, deleted in the same commit, with
one `docs/test-suite-audit.md` § "Subsumed" row per node):

- `test_contract_violation_task012.py` -- v0.13 task 012 (#92, requirement G's second half): surface `contract.violation`/`contract.agent_fault` through `ralphctl status`' own `reason:` line and `ralphctl iteration N`, for a hat that recovers as well as one that never does -- through ONE shared derivation, never two hand-kept strings. The s
- `test_contract_violation_and_agent_fault_events_task011.py` -- v0.13 task 011 (#92): prove `contract.violation`/`contract.agent_fault` are emitted from the LIVE path -- a real `LoopSupervisor._step_worker` call, stubbed `sup.runner` only -- not merely from `contract_enforcement.enforce_contract` in isolation (already covered by `test_contract_enforcement.py`) a
- `test_contract_exhaustion_never_solely_ends_the_run_task009.py` -- v0.13 task 009 (requirement C, #92): pin, directly and by name, the invariant the whole cutover (tasks 003-008) exists to establish: *a contract violation may never be the SOLE reason a run ends while an approach and iteration budget remain.* On `2026-08-28T06:24:03Z`, a run called `deck-phase3g` en
- `test_ingestion_blocker_signature_task012.py` -- Task 012 (requirement C.2, #124): the `empty_plan` boundary writes a blocker signature the same way worker stagnation writes its own (task 026, #61), reusing the SAME `blockerSignatures` map and the SAME `_step_replan` guard -- not a second lint-specific mechanism. Mirrors `tests/test_plan_ingestion
- `test_criteria_tamper_refusal_task024.py` -- Task 024 (requirement E.3, #116 section 2): the criteria-tamper residual task 008's draft left open, closed engine-side. Task 008 could only ever DETECT a worker- or planner-authored `successCriteria` rewrite (a sha256 cannot be inverted, so the pre-rewrite text was unrecoverable) and surface it to 
- `test_deck_phase3g_regression_task010.py` -- v0.13 task 010 (requirement F1/F2, #92): the `deck-phase3g` regression test, by name, against the EXACT payload that killed that run. **Cost this protects against:** on `2026-08-28T06:24:03Z`, run `deck- phase3g` ended `aborted`, `unverified`, at approach 2 of 12, iteration 135 of 900 (765 iteration
- `test_iteration_fault_cause_task013.py` -- v0.13 task 013 (#89, requirement H): `iteration_detail` derives the fault CAUSE -- `reason` + `signature` -- for its OWN iteration, not only for the run's last fault. #89's own correction comment is the point this pins: the cause was never missing from disk. `faults.explain_fault` is a pure function

Renamed to resolve cross-source collisions of module-private names (no test
function was renamed):
- `BAD_PAYLOAD` -> `cv012_BAD_PAYLOAD` (from `test_contract_violation_task012.py`)
- `GOOD_PAYLOAD` -> `cv012_GOOD_PAYLOAD` (from `test_contract_violation_task012.py`)
- `_ok` -> `_cv012_ok` (from `test_contract_violation_task012.py`)
- `_ScriptedWorker` -> `_cv012_ScriptedWorker` (from `test_contract_violation_task012.py`)
- `_supervisor` -> `_cv012_supervisor` (from `test_contract_violation_task012.py`)
- `BAD_PAYLOAD` -> `cvaa011_BAD_PAYLOAD` (from `test_contract_violation_and_agent_fault_events_task011.py`)
- `GOOD_PAYLOAD` -> `cvaa011_GOOD_PAYLOAD` (from `test_contract_violation_and_agent_fault_events_task011.py`)
- `_ok` -> `_cvaa011_ok` (from `test_contract_violation_and_agent_fault_events_task011.py`)
- `_ScriptedWorker` -> `_cvaa011_ScriptedWorker` (from `test_contract_violation_and_agent_fault_events_task011.py`)
- `_supervisor` -> `_cvaa011_supervisor` (from `test_contract_violation_and_agent_fault_events_task011.py`)
- `VALIDATION_ERROR` -> `cvaa011_VALIDATION_ERROR` (from `test_contract_violation_and_agent_fault_events_task011.py`)
- `KILLING_PAYLOAD` -> `cens009_KILLING_PAYLOAD` (from `test_contract_exhaustion_never_solely_ends_the_run_task009.py`)
- `_ok` -> `_ibs012_ok` (from `test_ingestion_blocker_signature_task012.py`)
- `_supervisor` -> `_ibs012_supervisor` (from `test_ingestion_blocker_signature_task012.py`)
- `_supervisor` -> `_ctr024_supervisor` (from `test_criteria_tamper_refusal_task024.py`)
- `_ok` -> `_ctr024_ok` (from `test_criteria_tamper_refusal_task024.py`)
- `KILLING_PAYLOAD` -> `dpr010_KILLING_PAYLOAD` (from `test_deck_phase3g_regression_task010.py`)
- `VALIDATION_ERROR` -> `dpr010_VALIDATION_ERROR` (from `test_deck_phase3g_regression_task010.py`)
- `_ok` -> `_dpr010_ok` (from `test_deck_phase3g_regression_task010.py`)
- `_supervisor` -> `_dpr010_supervisor` (from `test_deck_phase3g_regression_task010.py`)
"""

from __future__ import annotations


# ----------------------------------------------------------------------
# Folded from tests/test_contract_violation_task012.py (task 028, requirement D.7).
# ----------------------------------------------------------------------

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
from tests.conftest import supervisor_factory

TEST_STATE = "task012_contract_hat_state"
TEST_HAT = "task012_contract_hat"

# A missing required field -- the same "wrong shape" ordinary case
# `contract_enforcement`'s own module docstring names, distinct from
# tasks 009/010/011's `deck-phase3g` int-vs-string shape so this module
# does not just re-run their fixture under a new name.
cv012_BAD_PAYLOAD = json.dumps({})
EXPECTED_ERRORS = ["$: missing required field 'note'"]
cv012_GOOD_PAYLOAD = json.dumps({"note": "ok"})


def _cv012_ok(text: str) -> IterationResult:
    r = IterationResult(exit_code=0)
    r.final_text = text
    r.duration_s = 1.0
    r.usage = {"input": 1, "output": 1, "totalTokens": 2}
    return r


_cv012_supervisor = supervisor_factory(run_id="task012-contract", infra_retry_backoff_s=[0.0],
                                 infra_retry_backoff_max_s=0.0, infra_outage_budget_s=1000.0,
                                 vigilant=False, max_approaches=2, on_complete="exit")


def _stub_attempts(sup: LoopSupervisor, results: list[IterationResult]) -> list[str]:
    """Same technique `tests/test_worker_hat.
    py`'s `_stub_attempts` uses: stub `_run_iteration_once` (below the
    infra-retry wrapper) and charge `iterations_used` exactly as the real
    method does, so `budget_left()` genuinely runs out."""
    calls: list[str] = []

    async def fake_once(phase, extra="", prompt_name=None, tier=None, task_id=None):
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
    sup = _cv012_supervisor(tmp_path, iterations=1)  # exhausted after one pass
    _stub_attempts(sup, [_cv012_ok(cv012_BAD_PAYLOAD)])  # every attempt, forever
    # `enforce_contract`'s re-asks beyond attempt 0 go through
    # `_ask_contract_fix`, which calls `self.runner.run` directly (never
    # `_run_iteration_once`) -- stub it too, or the second attempt hangs
    # trying to reach a real agent.
    sup.runner = _cv012_ScriptedWorker([_cv012_ok(cv012_BAD_PAYLOAD)])  # type: ignore[assignment]
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
    sup = _cv012_supervisor(tmp_path, iterations=1)
    _stub_attempts(sup, [_cv012_ok(cv012_BAD_PAYLOAD)])
    sup.runner = _cv012_ScriptedWorker([_cv012_ok(cv012_BAD_PAYLOAD)])  # type: ignore[assignment]
    edges = tr.successors(TEST_STATE, sup.cfg.vigilant)

    asyncio.run(sup._step_generic_hat(TEST_STATE, edges))

    assert sup._abort_reason == "SENTINEL-CONTRACT-TEXT"


# ---------------------------------------------------------------------------
# 3. the events-to-iteration join, both the recovering and exhausting shape,
#    driven through the real worker call path (task 011's own technique).
# ---------------------------------------------------------------------------

class _cv012_ScriptedWorker:
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
    agent = _cv012_ScriptedWorker([_cv012_ok(bad), _cv012_ok(good)])
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
    agent = _cv012_ScriptedWorker([_cv012_ok(bad)])  # every attempt, forever
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
    agent = _cv012_ScriptedWorker(
        [_cv012_ok(json.dumps({"verdict": "complete", "task_id": "1"}))])
    sup.runner = agent  # type: ignore[assignment]

    asyncio.run(sup._step_worker())

    assert contract_fault_for_iteration(run.root, 1) is None
    detail = iteration_detail(run.root, 1)
    assert detail["contractFault"] is None
    assert not any(line.startswith("contract:  ")
                  for line in iteration_summary_lines(detail))


# ----------------------------------------------------------------------
# Folded from tests/test_contract_violation_and_agent_fault_events_task011.py (task 028, requirement D.7).
# ----------------------------------------------------------------------

import asyncio
import json

from ralphd.engine import state
from ralphd.engine.config import JobConfig
from ralphd.engine.contract_enforcement import EVENT_AGENT_FAULT, EVENT_VIOLATION
from ralphd.engine.loop import LoopSupervisor
from ralphd.engine.runner import IterationResult
from ralphd.engine.state import RunDir, atomic_write_json
from tests.conftest import make_supervisor

TASK_ID = "1"

# The `deck-phase3g` shape: `task_id` as a JSON int where the worker
# contract's schema requires a string (`transitions.HAT_SCHEMAS["worker"]
# ["properties"]["task_id"]` is `{"type": "string"}`).
cvaa011_BAD_PAYLOAD = json.dumps({"verdict": "complete", "task_id": 1})
cvaa011_GOOD_PAYLOAD = json.dumps({"verdict": "complete", "task_id": TASK_ID})
cvaa011_VALIDATION_ERROR = "$.task_id: expected string, got int"


def _cvaa011_ok(text: str) -> IterationResult:
    r = IterationResult(exit_code=0)
    r.final_text = text
    r.duration_s = 5.0
    r.usage = {"input": 10, "output": 5, "totalTokens": 15}
    return r


class _cvaa011_ScriptedWorker:
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


_cvaa011_supervisor = lambda tmp_path, name, *, iterations=10: make_supervisor(
    tmp_path, run_id=f"task011-{name}", cfg_defaults=False, run_root=name,
    workspace=name, status={"state": "running"}, approach=1,
    tasks=[{"id": TASK_ID, "title": "prove the events land on disk",
            "status": "in-progress", "successCriteria": "n/a"}],
    iterations=iterations, max_approaches=3, vigilant=False, on_complete="exit",
    infra_retry_backoff_s=[0.0], infra_retry_backoff_max_s=0.0,
    infra_outage_budget_s=1000.0)


def test_a_recovering_violation_emits_one_violation_event_and_no_fault(tmp_path):
    sup = _cvaa011_supervisor(tmp_path, "recovers")
    run = sup.run
    agent = _cvaa011_ScriptedWorker([_cvaa011_ok(cvaa011_BAD_PAYLOAD), _cvaa011_ok(cvaa011_GOOD_PAYLOAD)])
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
    assert v["errors"] == [cvaa011_VALIDATION_ERROR]

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
    sup = _cvaa011_supervisor(tmp_path, "exhausts", iterations=1)
    run = sup.run
    agent = _cvaa011_ScriptedWorker([_cvaa011_ok(cvaa011_BAD_PAYLOAD)])   # every attempt, forever
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
        assert v["errors"] == [cvaa011_VALIDATION_ERROR]

    assert len(faults) == 1, faults
    f = faults[0]
    assert f["hat"] == "worker"
    assert f["errors"] == [cvaa011_VALIDATION_ERROR]

    # the ladder genuinely ran out -- never fewer, never more attempts.
    assert agent.calls == max_attempts
    # the violation never took the move anyway.
    task = run.read_tasks()["tasks"][0]
    assert task["status"] == "in-progress", task


# ----------------------------------------------------------------------
# Folded from tests/test_contract_exhaustion_never_solely_ends_the_run_task009.py (task 028, requirement D.7).
# ----------------------------------------------------------------------

import asyncio
import json

from ralphd.engine import state
from ralphd.engine import transitions
from ralphd.engine.config import JobConfig
from ralphd.engine.loop import LoopSupervisor
from ralphd.engine.runner import IterationResult
from ralphd.engine.state import RunDir, atomic_write_json

# The exact payload that killed `deck-phase3g`: a JSON scalar where the
# contract requires a string.
cens009_KILLING_PAYLOAD = json.dumps({"verdict": "complete", "task_id": 104})


class _RepeatedlyBrokenAgent:
    """Every call -- the real work iteration AND every uncharged
    `_ask_contract_fix` re-ask alike -- returns the SAME schema-violating
    payload. The ladder can never repair itself; every pass exhausts."""

    def __init__(self):
        self.calls = 0

    def interrupt(self) -> bool:
        return False

    async def run(self, prompt, transcript, **kw) -> IterationResult:
        self.calls += 1
        r = IterationResult(exit_code=0)
        r.final_text = ("Task 104's success criteria are met: all sites "
                        "converged, evidence quoted and committed.\n"
                        + cens009_KILLING_PAYLOAD)
        r.duration_s = 5.0
        r.usage = {"input": 10, "output": 5, "totalTokens": 15}
        return r


def _deck_phase3g_shaped_supervisor(tmp_path):
    """approach 2 of 12, iteration 135 of 900 -- `deck-phase3g`'s own
    shape at the moment its worker wrote the killing payload."""
    root = tmp_path / "run"
    root.mkdir(parents=True)
    run = RunDir(root=root)
    run.update_status(state="running")
    atomic_write_json(run.tasks_file, {"version": 1, "tasks": [
        {"id": "104", "title": "convert clientOpensCreateModalForAgent",
         "status": "in-progress",
         "successCriteria": "the targeted validation run exits 0"},
    ]})
    cfg = JobConfig(run_id="deck-phase3g-shaped", iterations=900,
                    max_approaches=12, vigilant=False, on_complete="exit",
                    infra_retry_backoff_s=[0.0], infra_retry_backoff_max_s=0.0,
                    infra_outage_budget_s=1000.0)
    sup = LoopSupervisor(cfg, run, root)
    sup._approach = 2
    sup.iterations_used = 134  # this pass is iteration 135
    agent = _RepeatedlyBrokenAgent()
    sup.runner = agent  # type: ignore[assignment]
    return sup, run, agent


def test_an_exhausted_ladder_with_budget_and_approach_remaining_never_aborts(
        tmp_path):
    sup, run, agent = _deck_phase3g_shaped_supervisor(tmp_path)

    outcome = asyncio.run(sup._step_worker())

    # -- the invariant itself --------------------------------------------
    assert sup._abort_reason is None, (
        "an exhausted contract ladder must never be the SOLE reason the "
        "run ends while budget and approach remain")
    assert run.read_status().get("state") == "running", (
        "the run must never be pushed to a terminal state by a contract "
        "violation alone")

    # -- the state machine advanced along a DECLARED edge -----------------
    assert outcome == ("advance", "replan", "stagnation_exhausted")
    assert transitions.is_legal("worker", "replan", vigilant=False)
    assert "stagnation_exhausted" in transitions.guard_names("worker", vigilant=False)

    # approach and iteration budget both remain wide open -- this is not
    # an ending, contract violation or not.
    assert sup._approach == 2
    assert sup.cfg.max_approaches == 12
    assert sup.iterations_used == 138  # 134 seeded + 4 charged passes (the
    # first records the one-time criteriaFingerprint baseline, task 008)
    assert sup.budget_left()

    # the illegal move was still genuinely refused throughout -- task 104
    # never silently completes just because the ladder gave up.
    task = run.read_tasks()["tasks"][0]
    assert task["status"] == "in-progress", \
        "a contract violation must not silently take the move anyway"

    # the ladder ran the FULL contract_retry_max + 1 attempts, four
    # separate times (once per charged pass) -- never fewer, never more.
    assert agent.calls == 4 * (sup.cfg.contract_retry_max + 1)

    events = state.read_events(run.root)
    violations = [e for e in events if e.get("type") == "contract.violation"]
    faults = [e for e in events if e.get("type") == "contract.agent_fault"]
    assert len(violations) == 4 * (sup.cfg.contract_retry_max + 1)
    assert len(faults) == 4
    assert all("task_id" in ev["errors"][0] for ev in violations)


# ----------------------------------------------------------------------
# Folded from tests/test_ingestion_blocker_signature_task012.py (task 028, requirement D.7).
# ----------------------------------------------------------------------

import asyncio
import json
from pathlib import Path

import pytest

from ralphd.engine.config import JobConfig
from ralphd.engine.loop import LoopSupervisor
from ralphd.engine.runner import IterationResult
from ralphd.engine.state import RunDir, atomic_write_json
from tests.conftest import supervisor_with_runner


# -- scaffolding -- mirrors tests/test_plan_ingestion_rejection.py --------


class _ScriptedRunner:
    """Stands in for PiRunner. Each script entry takes the supervisor and
    returns an IterationResult; the last entry repeats forever."""

    def __init__(self, sup: LoopSupervisor, script: list):
        self.sup = sup
        self.script = script
        self.calls = 0
        self.running = False

    def interrupt(self) -> bool:
        return self.running

    async def run(self, prompt, transcript, **kw) -> IterationResult:
        self.calls += 1
        self.running = True
        try:
            step = self.script[min(self.calls - 1, len(self.script) - 1)]
            return step(self.sup)
        finally:
            self.running = False


def _ibs012_ok(text: str) -> IterationResult:
    r = IterationResult(exit_code=0)
    r.final_text = text
    r.duration_s = 30.0          # not an "instant" failure
    r.usage = {"input": 10, "output": 5, "totalTokens": 15}
    return r


def _pointer_line(path: str = "plan-proposal.json", summary: str = "planned") -> str:
    return json.dumps({"proposal_path": path, "summary": summary})


def _bad_plan_step(dep_id: str):
    """A proposal whose single task depends on a nonexistent id -- the
    `plan_ingestion.lint_proposal` "unresolvable dependsOn" lint, worded by
    which id it names so two calls can be made to name either the SAME id
    (same normalized error) or DISTINCT ones (different error)."""

    def step(sup: LoopSupervisor) -> IterationResult:
        atomic_write_json(sup.run.root / "plan-proposal.json", {
            "version": 1, "tasks": [
                {"id": "001", "title": "t1", "successCriteria": "c1",
                 "dependsOn": [dep_id]},
            ]})
        return _ibs012_ok("planned\n" + _pointer_line())

    return step


_ibs012_supervisor = supervisor_with_runner(
    lambda sup, script: _ScriptedRunner(sup, script),
    run_id="task012-ingestion-sig", iterations=12, max_approaches=3, vigilant=False,
    on_complete="exit", infra_retry_backoff_s=[0.0], infra_retry_backoff_max_s=0.0,
    infra_outage_budget_s=1000.0, contract_retry_max=0)


PRIOR_TASKS_DOC = {
    "version": 1, "goal": "prior", "scope": {}, "repositories": [],
    "tasks": [{"id": "prior-1", "title": "already here",
               "successCriteria": "was here before the bad replan"}],
    "discovered": {},
}


def test_two_consecutive_planning_attempts_with_the_same_ingestion_errors_end_on_the_two_strike_rule_without_charging_an_approach(
        tmp_path):
    root = tmp_path / "run"
    # max_approaches=3, but the two-strike guard must fire the moment the
    # SECOND planning attempt's own ingestion-error signature matches the
    # first's -- and, per task 003 (requirement C), NEITHER zero-
    # deliverable attempt ever charges the approach budget: this run ends
    # at approach 1, never having bumped even once, however much of
    # `max_approaches=3` remained unspent.
    sup = _ibs012_supervisor(root, [_bad_plan_step("999"), _bad_plan_step("999")])
    sup.run.write_tasks(dict(PRIOR_TASKS_DOC))

    seen_edges: list[tuple] = []
    real_advance = sup._advance

    def spy_advance(state, target, guard):
        seen_edges.append((state, target, guard))
        return real_advance(state, target, guard)

    sup._advance = spy_advance  # type: ignore[method-assign]

    final_state = asyncio.run(sup.run_job())

    assert final_state == "failed"
    status = sup.run.read_status()
    assert status["state"] == "failed"
    assert status["verdict"] == "unverified"
    # Requirement C: neither zero-deliverable planning attempt charged the
    # approach budget -- the run never left approach 1.
    assert status["approach"] == 1
    # Task 012 (requirement C.2, #124) / task 003 (requirement C): a
    # repeated INGESTION-origin signature is a degraded run, not merely an
    # ordinary #61 stagnation repeat.
    assert status.get("health") == "degraded"
    assert status.get("planningDeathSignature") is not None
    # The reason names the lint (the ingestion error text itself, RAW --
    # task 007, requirement B.3, #135 -- not the folded/lowercased
    # comparison signature), not a boilerplate placeholder.
    reason = status.get("reason") or ""
    assert "dependsOn references unknown task" in reason
    assert "lexical lint" in reason
    assert "neither zero-deliverable attempt spent" in reason
    # `_step_planning` decides the two-strike itself now, before `replan`
    # is ever entered a second time for this pair: the FIRST attempt's
    # own advance into `replan` (`empty_plan`) still shows up, but the
    # run's actual termination edge is `planning`->`replan`
    # (`planning_death_repeated`), never the #61 worker-stagnation-only
    # `repeated_blocker_signature` guard (task 003 moved ingestion's own
    # two-strike off that shared, approach-keyed map entirely).
    assert ("planning", "replan", "empty_plan") in seen_edges
    assert ("planning", "replan", "planning_death_repeated") in seen_edges
    assert ("replan", "finishing", "repeated_blocker_signature") not in seen_edges
    # Only two planning iterations ever ran.
    iters = sorted((root / "iterations").iterdir())
    metas = [json.loads((d / "meta.json").read_text()) for d in iters]
    assert sum(1 for m in metas if m["phase"] == "planning") == 2
    # tasks.json was never overwritten by either bad proposal.
    assert sup.run.read_tasks() == PRIOR_TASKS_DOC


def test_consecutive_distinct_ingestion_errors_never_trigger_the_two_strike_rule_and_never_charge_an_approach(
        tmp_path):
    """Requirement C: as long as every consecutive planning attempt's
    lexical lint differs from its predecessor's, the two-strike guard
    never fires -- and, per C.1/C.3, NEITHER of these zero-deliverable
    attempts ever charges the approach budget either. `max_approaches=1`
    on purpose: the OLD behaviour this replaces would have failed the run
    the instant the FIRST attempt exhausted ingestion (`approach ==
    max_approaches` already); the fix keeps retrying, uncharged, until the
    run's own ITERATION budget -- never the approach budget -- runs out.
    """
    root = tmp_path / "run"
    # digits are stripped by the normaliser (`_normalize_blocker_signature_
    # text`), so distinct dep ids must differ on non-digit text to compare
    # as genuinely distinct signatures.
    distinct_ids = ["alpha", "bravo", "charlie", "delta", "echo", "foxtrot",
                    "golf", "hotel", "india", "juliet", "kilo", "lima"]
    sup = _ibs012_supervisor(
        root, [_bad_plan_step(dep) for dep in distinct_ids],
        max_approaches=1)
    sup.run.write_tasks(dict(PRIOR_TASKS_DOC))

    final_state = asyncio.run(sup.run_job())

    # Task 011 (v0.24 requirement C.1, #169): nothing but the run's own
    # ITERATION budget ended this run -- exactly the plain shape C.1 now
    # routes into `timed_out`/`verdict: null` instead of `failed`/
    # `unverified`, through the same `_finish_timed_out()` path task 010
    # established (this is `_step_planning`'s own plain `if not self.
    # budget_left():` guard, one of the five sites that flag sets).
    assert final_state == "timed_out"
    status = sup.run.read_status()
    assert status["state"] == "timed_out"
    assert status["verdict"] is None
    # The repeated-blocker-signature guard never fired -- every
    # consecutive pair's own ingestion-error signature is genuinely
    # distinct -- so `health` never moved off "ok" either.
    assert status.get("health") in (None, "ok")
    # Requirement C: no zero-deliverable attempt in this never-repeating
    # streak ever charged the approach budget -- the run stayed at
    # approach 1 for its whole life, even though `max_approaches=1` was
    # already "spent" by the very first attempt under the OLD behaviour
    # this replaces.
    assert status["approach"] == 1
    # All 12 iterations of budget were spent retrying planning -- never on
    # a worker iteration (never reached) and never on a second, charged
    # approach (never existed).
    iters = sorted((root / "iterations").iterdir())
    metas = [json.loads((d / "meta.json").read_text()) for d in iters]
    assert len(metas) == 12
    assert all(m["phase"] == "planning" for m in metas)
    assert all(m.get("approach") == 1 for m in metas)
    # tasks.json was never overwritten by any of the bad proposals.
    assert sup.run.read_tasks() == PRIOR_TASKS_DOC


# ----------------------------------------------------------------------
# Folded from tests/test_criteria_tamper_refusal_task024.py (task 028, requirement D.7).
# ----------------------------------------------------------------------

import json
from pathlib import Path

from ralphd.engine import findings as findings_mod
from ralphd.engine.config import JobConfig
from ralphd.engine.loop import LoopSupervisor
from ralphd.engine.runner import IterationResult
from ralphd.engine.state import RunDir
from tests.conftest import supervisor_factory


# The shared harness (tests/conftest.py, G.1).
_ctr024_supervisor = supervisor_factory(run_id="unit")



def _write_tasks(run: RunDir, tasks: list[dict]) -> None:
    run.tasks_file.write_text(json.dumps({"version": 1, "tasks": tasks}))


def _ctr024_ok(text: str) -> IterationResult:
    r = IterationResult(exit_code=0)
    r.final_text = text
    r.duration_s = 1.0
    r.usage = {"input": 1, "output": 1, "totalTokens": 2}
    return r


def _events(sup: LoopSupervisor, kind: str) -> list[dict]:
    path = sup.run.root / "events.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines()
            if line.strip() and json.loads(line).get("type") == kind]


# ---------------------------------------------------------------------------
# 1. _check_criteria_edits REFUSES a rewrite -- the rewrite does not stand.
# ---------------------------------------------------------------------------

def test_check_criteria_edits_refuses_rewrite_at_validation_attempts_zero(tmp_path):
    """At `validationAttempts` 0 -- task 008's original silent case -- the
    rewrite is both flagged AND undone."""
    sup = _ctr024_supervisor(tmp_path)
    original = "do X, exactly as the plan says"
    _write_tasks(sup.run, [
        {"id": "1", "title": "t", "status": "pending",
         "successCriteria": original, "validationAttempts": 0},
    ])
    sup._ensure_criteria_baseline()

    tasks_data = sup.run.read_tasks()
    tasks_data["tasks"][0]["successCriteria"] = "do X, rewritten with no failure yet"
    sup.run.write_tasks(tasks_data)

    sup._check_criteria_edits(sup.run.read_tasks())
    on_disk = sup.run.read_tasks()["tasks"][0]

    assert on_disk.get("validationAttempts", 0) == 0
    assert on_disk["criteriaEditedAfterValidationFailure"] is True
    # the refusal: the rewrite does not stand
    assert on_disk["successCriteria"] == original
    assert on_disk["criteriaFingerprint"] == sup._criteria_fingerprint(original)

    refused = _events(sup, "task.criteria_edit_refused")
    assert len(refused) == 1
    assert refused[0]["taskId"] == "1"
    assert refused[0]["refusedCriteria"] == "do X, rewritten with no failure yet"
    assert refused[0]["restoredCriteria"] == original


def test_check_criteria_edits_refuses_rewrite_after_a_validation_failure(tmp_path):
    """The same refusal after a real validation failure -- the shape the
    marker was originally named for -- and it is idempotent: a second pass
    over the now-restored task changes nothing and emits nothing new."""
    sup = _ctr024_supervisor(tmp_path)
    original = "ship the feature and prove it with a test"
    _write_tasks(sup.run, [
        {"id": "1", "title": "t", "status": "validation-failed",
         "successCriteria": original, "validationAttempts": 1,
         "validationNotes": "did not meet the bar"},
    ])
    sup._ensure_criteria_baseline()

    tasks_data = sup.run.read_tasks()
    tasks_data["tasks"][0]["successCriteria"] = "ship anything at all"
    tasks_data["tasks"][0]["status"] = "completed"
    sup.run.write_tasks(tasks_data)

    sup._check_criteria_edits(sup.run.read_tasks())
    on_disk = sup.run.read_tasks()["tasks"][0]
    assert on_disk["successCriteria"] == original
    assert on_disk["criteriaEditedAfterValidationFailure"] is True
    # nothing else about the record is rewritten by the refusal
    assert on_disk["status"] == "completed"
    assert on_disk["validationAttempts"] == 1
    assert len(_events(sup, "task.criteria_edit_refused")) == 1

    sup._check_criteria_edits(sup.run.read_tasks())
    assert sup.run.read_tasks()["tasks"][0]["successCriteria"] == original
    assert len(_events(sup, "task.criteria_edit_refused")) == 1


def test_unrestorable_legacy_task_is_flagged_and_tracked_forward(tmp_path):
    """Fallback, pinned deliberately: a task carrying a fingerprint but no
    verbatim `criteriaOfRecord` (a run resumed from before task 024) cannot
    be restored -- sha256 is one-way -- so task 008's behaviour stands:
    flag, track the new text forward, and let the engine-forced blocking
    finding below carry the enforcement."""
    sup = _ctr024_supervisor(tmp_path)
    _write_tasks(sup.run, [
        {"id": "1", "title": "t", "status": "completed",
         "successCriteria": "do X (rewritten)", "validationAttempts": 1,
         "criteriaFingerprint": sup._criteria_fingerprint("do X")},
    ])
    sup._check_criteria_edits(sup.run.read_tasks())
    on_disk = sup.run.read_tasks()["tasks"][0]
    assert on_disk["criteriaEditedAfterValidationFailure"] is True
    assert on_disk["successCriteria"] == "do X (rewritten)"
    assert on_disk["criteriaFingerprint"] == sup._criteria_fingerprint(
        "do X (rewritten)")
    assert _events(sup, "task.criteria_edit_refused") == []


# ---------------------------------------------------------------------------
# 2. The engine-forced, RECORDED blocking finding + the refused verdict.
# ---------------------------------------------------------------------------

def test_flagged_criteria_findings_reports_one_blocking_finding_per_flagged_task():
    tasks = [
        {"id": "1", "successCriteria": "rewritten text",
         "criteriaEditedAfterValidationFailure": True},
        {"id": "2", "successCriteria": "clean text"},
    ]
    out = findings_mod.flagged_criteria_findings(tasks)
    assert len(out) == 1
    assert out[0]["severity"] == "blocking"
    assert out[0]["curable"] is True
    assert "1" in out[0]["clause"]
    assert findings_mod.validate_finding(out[0]) == []


def test_flagged_criteria_findings_empty_when_nothing_flagged():
    tasks = [{"id": "1", "successCriteria": "clean text"}]
    assert findings_mod.flagged_criteria_findings(tasks) == []


def test_review_verdict_refuses_verified_claim_over_a_flagged_edit(tmp_path):
    """End to end at unit level: the worker rewrites the criteria, the
    engine refuses the rewrite, and the review model's `verified` claim
    over that same task is refused with a RECORDED blocking finding."""
    sup = _ctr024_supervisor(tmp_path)
    original = "the bar the plan actually set"
    _write_tasks(sup.run, [
        {"id": "1", "title": "t", "status": "awaiting-validation",
         "successCriteria": original, "validationAttempts": 1},
    ])
    sup._ensure_criteria_baseline()

    tasks_data = sup.run.read_tasks()
    tasks_data["tasks"][0]["successCriteria"] = (
        "the criteria text a worker rewrote on its own authority")
    sup.run.write_tasks(tasks_data)
    sup._check_criteria_edits(sup.run.read_tasks())

    # review.md always produces a findings.json in production (task 004);
    # an empty-but-present channel is the review model reporting "I found
    # nothing blocking" over a task whose criteria were tampered with.
    findings_mod.write_findings_json(sup.run.findings_json_file, [])

    result = _ctr024_ok(json.dumps({"verdict": "verified"}))
    verified, payload, contract_errors = sup._review_verdict(result)

    assert verified is False
    assert contract_errors
    assert any("blocking" in e for e in contract_errors)

    # the rewrite does not stand
    on_disk = sup.run.read_tasks()["tasks"][0]
    assert on_disk["successCriteria"] == original

    # the blocking finding is RECORDED, not merely computed in memory
    persisted = findings_mod.read_findings_json(sup.run.findings_json_file)
    blocking = [f for f in persisted if f["severity"] == "blocking"]
    assert len(blocking) == 1
    assert "1" in blocking[0]["clause"]
    assert findings_mod.validate_finding(blocking[0]) == []
    recorded = _events(sup, "review.criteria_tamper_finding")
    assert len(recorded) == 1
    assert recorded[0]["severity"] == "blocking"

    # re-reading the channel does not duplicate the engine's own finding
    again, channel_errors = sup._review_findings_channel()
    assert channel_errors == []
    assert len([f for f in again if f["severity"] == "blocking"]) == 1
    assert len(findings_mod.read_findings_json(sup.run.findings_json_file)) == 1


def test_absent_findings_channel_is_not_an_escape_hatch(tmp_path):
    """With NO findings sidecar on disk at all, a `verified` claim over a
    flagged task must still be refused (it used to be trusted: the
    no-channel early return happened before the engine's own finding was
    added)."""
    sup = _ctr024_supervisor(tmp_path)
    rewritten = "a rewritten bar nobody authorized"
    _write_tasks(sup.run, [
        {"id": "1", "title": "t", "status": "awaiting-validation",
         "successCriteria": rewritten, "validationAttempts": 1,
         "criteriaEditedAfterValidationFailure": True,
         "criteriaFingerprint": sup._criteria_fingerprint(rewritten)},
    ])
    assert not sup.run.findings_json_file.exists()

    result = _ctr024_ok(json.dumps({"verdict": "verified"}))
    verified, payload, contract_errors = sup._review_verdict(result)

    assert verified is False
    assert contract_errors
    # and the finding the refusal rests on is on disk afterwards
    persisted = findings_mod.read_findings_json(sup.run.findings_json_file)
    assert [f["severity"] for f in persisted] == ["blocking"]


def test_review_verdict_unaffected_when_no_task_is_flagged(tmp_path):
    sup = _ctr024_supervisor(tmp_path)
    _write_tasks(sup.run, [
        {"id": "1", "title": "t", "status": "awaiting-validation",
         "successCriteria": "clean text", "validationAttempts": 1,
         "criteriaFingerprint": sup._criteria_fingerprint("clean text")},
    ])
    findings_mod.write_findings_json(sup.run.findings_json_file, [])

    result = _ctr024_ok(json.dumps({"verdict": "verified"}))
    verified, payload, contract_errors = sup._review_verdict(result)

    assert verified is True
    assert not contract_errors
    assert findings_mod.read_findings_json(sup.run.findings_json_file) == []
    assert _events(sup, "review.criteria_tamper_finding") == []


def test_no_findings_channel_and_nothing_flagged_still_trusts_the_claim(tmp_path):
    """The pre-existing no-channel posture is unchanged for a clean plan:
    absent sidecar + no tamper marker still means "nothing exists to check
    the claim against", and the engine creates no sidecar of its own."""
    sup = _ctr024_supervisor(tmp_path)
    _write_tasks(sup.run, [
        {"id": "1", "title": "t", "status": "awaiting-validation",
         "successCriteria": "clean text", "validationAttempts": 1,
         "criteriaFingerprint": sup._criteria_fingerprint("clean text")},
    ])
    findings_list, channel_errors = sup._review_findings_channel()
    assert findings_list is None
    assert channel_errors == []
    assert not sup.run.findings_json_file.exists()


# ---------------------------------------------------------------------------
# 3. The judge-authored amendment path is untouched, in both directions.
# ---------------------------------------------------------------------------

def test_judge_amendment_is_applied_and_survives_the_next_edit_check(tmp_path):
    """The legitimate amendment path's own review payload reports
    `rejected` alongside `petition_adjudications` (never `verified` over
    the SAME settled payload -- `_apply_criteria_amendments` runs from
    inside `_review_verdict` itself, on this exact call). The amendment
    must be applied, must NOT set the tamper marker, and -- task 024's own
    new obligation -- must SURVIVE the next `_check_criteria_edits` pass
    rather than being reverted as if a worker had written it."""
    sup = _ctr024_supervisor(tmp_path)
    old_text = ("this task's own successCriteria can only be verified using "
                "a state its own satisfaction would have to create first")
    new_text = "a fresh, achievable criterion that names no unmet state"
    _write_tasks(sup.run, [
        {"id": "1", "title": "t", "status": "awaiting-validation",
         "successCriteria": old_text, "validationAttempts": 2},
    ])
    sup._ensure_criteria_baseline()
    reports = tmp_path / "artifacts" / "reports"
    reports.mkdir(parents=True)
    (reports / "v0.16-petitions.md").write_text(
        "# v0.16 petitions\n\n## Petition: task024 fixture\n\n"
        f"**Obligation**: {old_text}\n**Type**: self-referential\n"
        "**Evidence**: fixture evidence\n")

    result = _ctr024_ok(json.dumps({
        "verdict": "rejected",
        "reason": "re-scoping task 1's criteria via a CONFIRMED petition",
        "petition_adjudications": [{
            "obligation": old_text,
            "adjudication": "CONFIRMED",
            "task_id": "1",
            "replacement_criteria": new_text,
        }],
    }))
    verified, payload, contract_errors = sup._review_verdict(result)

    assert verified is False  # honestly rejected, exactly as the payload claims
    on_disk = sup.run.read_tasks()["tasks"][0]
    assert on_disk["successCriteria"] == new_text
    assert on_disk["criteriaFingerprint"] == sup._criteria_fingerprint(new_text)
    assert on_disk["criteriaOfRecord"] == new_text
    assert on_disk["validationAttempts"] == 0
    assert "criteriaEditedAfterValidationFailure" not in on_disk

    sup._check_criteria_edits(sup.run.read_tasks())
    after = sup.run.read_tasks()["tasks"][0]
    assert after["successCriteria"] == new_text
    assert "criteriaEditedAfterValidationFailure" not in after
    assert _events(sup, "task.criteria_edit_refused") == []


def test_judge_amendment_of_a_non_task_target_is_still_refused_and_named(tmp_path):
    """Negative direction, unaffected: an amendment naming something that
    is not a real task at all is still refused by
    `petitions.criteria_amendment_refusals`, exactly as task 008 pinned,
    with the new engine check adding nothing and taking nothing away."""
    sup = _ctr024_supervisor(tmp_path)
    subject_text = "task 1's own, perfectly ordinary successCriteria text"
    _write_tasks(sup.run, [
        {"id": "1", "title": "t", "status": "awaiting-validation",
         "successCriteria": subject_text, "validationAttempts": 1,
         "criteriaFingerprint": sup._criteria_fingerprint(subject_text)},
    ])
    reports = tmp_path / "artifacts" / "reports"
    reports.mkdir(parents=True)
    (reports / "v0.16-petitions.md").write_text(
        "# v0.16 petitions\n\n## Petition: task024 fixture\n\n"
        f"**Obligation**: {subject_text}\n**Type**: contradictory-clauses\n"
        "**Evidence**: fixture evidence\n")

    result = _ctr024_ok(json.dumps({
        "verdict": "rejected",
        "reason": "attempted amendment of a non-task target",
        "petition_adjudications": [{
            "obligation": subject_text,
            "adjudication": "CONFIRMED",
            "task_id": "PRD requirement B.2",
            "replacement_criteria": "a rewritten PRD text",
        }],
    }))
    verified, payload, contract_errors = sup._review_verdict(result)

    on_disk = sup.run.read_tasks()["tasks"][0]
    assert on_disk["successCriteria"] == subject_text

    refused = _events(sup, "task.criteria_amendment_refused")
    assert len(refused) == 1
    assert refused[0]["target"] == "PRD requirement B.2"


# ----------------------------------------------------------------------
# Folded from tests/test_deck_phase3g_regression_task010.py (task 028, requirement D.7).
# ----------------------------------------------------------------------

import asyncio
import json
from pathlib import Path

from ralphd.engine.config import JobConfig
from ralphd.engine.loop import LoopSupervisor
from ralphd.engine.runner import IterationResult
from ralphd.engine.state import RunDir, atomic_write_json
from tests.conftest import supervisor_with_runner

TASK_104 = "104"

# The exact payload that killed `deck-phase3g` -- a JSON number where the
# worker contract's `task_id` property requires a string.
dpr010_KILLING_PAYLOAD = json.dumps({"verdict": "complete", "task_id": 104})
assert dpr010_KILLING_PAYLOAD == '{"verdict": "complete", "task_id": 104}', dpr010_KILLING_PAYLOAD

# The verbatim schema-validation error `contracts.validate_payload` raises
# for that exact payload -- what a real re-prompt states to the hat.
dpr010_VALIDATION_ERROR = "$.task_id: expected string, got int"

# A recognisable fragment of the worker hat's own task prompt body, used
# the same way task 008's end-to-end test uses it: present in the ordinary
# work turn's prompt, absent from the re-ask's.
WORKER_PROMPT_FRAGMENT = "THE ONE RULE: exactly one task, then stop"


def _dpr010_ok(text: str) -> IterationResult:
    r = IterationResult(exit_code=0)
    r.final_text = text
    r.duration_s = 5.0
    r.usage = {"input": 10, "output": 5, "totalTokens": 15}
    return r


def _review_verified(sup, prompt="") -> IterationResult:
    return _dpr010_ok("task 104's success criteria hold; nothing left for review "
               "to reject\n" + json.dumps({"verdict": "verified"}))


def _task(**over) -> dict:
    task = {"id": TASK_104,
            "title": "convert clientOpensCreateModalForAgent",
            "status": "in-progress",
            "successCriteria": "the targeted validation run exits 0"}
    task.update(over)
    return task


class _RecordingScriptedAgent:
    """Same dispatch shape as
    `test_worker_contract_reask_end_to_end_task008.py`'s
    `_RecordingScriptedAgent`: one script per phase, keyed by the engine's
    own recorded phase, the last step repeated forever, every call's
    prompt recorded in order."""

    def __init__(self, handlers: dict):
        self.handlers = handlers
        self.calls: dict[str, int] = {}
        self.prompts: list[str] = []
        self.running = False

    def interrupt(self) -> bool:
        return self.running

    async def run(self, prompt, transcript, **kw) -> IterationResult:
        self.prompts.append(prompt)
        Path(transcript).parent.mkdir(parents=True, exist_ok=True)
        Path(transcript).write_text('{"type": "assistant", "text": "stub"}\n')
        phase = self.sup.run.read_status().get("phase")
        n = self.calls.get(phase, 0)
        self.calls[phase] = n + 1
        steps = self.handlers[phase]
        self.running = True
        try:
            return steps[min(n, len(steps) - 1)](self.sup, prompt)
        finally:
            self.running = False


_dpr010_supervisor = supervisor_with_runner(
    lambda sup, handlers: _RecordingScriptedAgent(handlers),
    run_id="deck-phase3g-regression-task010", iterations=3, max_approaches=12, vigilant=False,
    on_complete="exit", infra_retry_backoff_s=[0.0], infra_retry_backoff_max_s=0.0,
    infra_outage_budget_s=1000.0)


def _run_resumed(root: Path, handlers: dict, tasks: list[dict],
                 **cfg_kw) -> tuple[LoopSupervisor, str]:
    """Mirrors task 008's helper of the same name: seed tasks.json exactly
    like a resumed run, skip planning entirely."""
    RunDir(root=root).update_status(state="starting")
    sup = _dpr010_supervisor(root, handlers, **cfg_kw)
    atomic_write_json(sup.run.tasks_file, {"version": 1, "tasks": tasks})
    sup.iterations_used = 1
    return sup, asyncio.run(sup._run_job_core())


def test_deck_phase3g_killing_payload_is_refused_then_repaired_and_task104_moves(
        tmp_path):
    root = tmp_path / "run"
    tasks = [_task(status="in-progress")]

    corrected = json.dumps({"verdict": "complete", "task_id": TASK_104})

    sup, final_state = _run_resumed(root, {
        "worker": [lambda s, p: _dpr010_ok(dpr010_KILLING_PAYLOAD),
                   lambda s, p: _dpr010_ok(corrected)],
        "review": [_review_verified],
    }, tasks, iterations=4)

    # -- the violation was genuinely refused, not silently taken ----------
    # (checked via the recorded events below, and via the fact that only
    # the SECOND, corrected attempt could have produced the move).
    events = [json.loads(x) for x in
              (root / "events.jsonl").read_text().splitlines() if x.strip()]
    violation_events = [e for e in events if e.get("type") == "contract.violation"]
    fault_events = [e for e in events if e.get("type") == "contract.agent_fault"]
    assert len(violation_events) == 1, violation_events
    # -- the error was carried back VERBATIM ------------------------------
    assert violation_events[0]["errors"] == [dpr010_VALIDATION_ERROR]
    assert violation_events[0]["hat"] == "worker"
    assert fault_events == [], "the ladder recovered; there is no agent-fault"

    # -- on the corrected re-ask, task 104 moves exactly as reported -------
    final_tasks = sup.run.read_tasks()["tasks"]
    assert len(final_tasks) == 1
    assert final_tasks[0]["id"] == TASK_104
    assert final_tasks[0]["status"] == "completed", final_tasks[0]

    # the ladder took exactly two attempts for the one worker episode.
    assert sup.runner.calls["worker"] == 2

    # the re-ask never opened a second iteration slot.
    assert sup.iterations_used == 3   # seeded 1, +1 worker charge, +1 review charge
    iterations_root = root / "iterations"
    iteration_dirs = sorted(p.name for p in iterations_root.iterdir() if p.is_dir())
    assert iteration_dirs == ["0002", "0003"], iteration_dirs

    # the re-ask prompt states the verbatim error and excludes the worker
    # hat's own task-prompt body; contrasted against the FIRST call's
    # prompt, which genuinely does carry that fragment.
    assert len(sup.runner.prompts) == 3
    first_prompt, reask_prompt, _review_prompt = sup.runner.prompts
    assert WORKER_PROMPT_FRAGMENT in first_prompt, \
        "sanity: the ordinary work turn's prompt must carry worker.md"
    assert dpr010_VALIDATION_ERROR in reask_prompt
    assert WORKER_PROMPT_FRAGMENT not in reask_prompt

    # a re-ask transcript exists on disk.
    transcript = root / "artifacts" / "contract" / "0001.jsonl"
    assert transcript.exists(), transcript

    # the run survived and finished -- the invariant this test exists for.
    assert sup._abort_reason is None
    assert final_state == "succeeded", sup.run.read_status()


# ----------------------------------------------------------------------
# Folded from tests/test_iteration_fault_cause_task013.py (task 028, requirement D.7).
# ----------------------------------------------------------------------

import json
from pathlib import Path

from ralphd.engine.faults import (
    FAULT_REASON_SIGNATURE,
    FAULT_REASON_WORK,
)
from ralphd.engine.state import (
    FAULT_VERDICT_DIVERGED_NOTICE,
    ITERATION_DERIVED_KEYS,
    iteration_detail,
)

DNS_ERROR = "request to https://aigw.internal/v1 failed, reason: getaddrinfo EAI_AGAIN aigw.internal"
WORK_ERROR = "Traceback (most recent call last):\n  ...\nAssertionError: task 5 criteria not met"


def _write_iteration(run_dir: Path, n: int, meta: dict) -> Path:
    d = run_dir / "iterations" / f"{n:04d}"
    d.mkdir(parents=True)
    (d / "meta.json").write_text(json.dumps(meta))
    return d


def _meta(n: int, **over) -> dict:
    meta = {"number": n, "phase": "worker", "approach": 1,
            "startedAt": "2026-09-04T10:00:00Z", "endedAt": "2026-09-04T10:00:31Z",
            "exitCode": 0, "interrupted": False, "timedOut": False,
            "noTrafficTimeout": False, "sawComplete": False, "sawVerified": False,
            "error": None, "faultClass": None, "usage": {}}
    meta.update(over)
    return meta


def test_reason_and_signature_are_in_the_derived_keys_tuple():
    assert "reason" in ITERATION_DERIVED_KEYS
    assert "signature" in ITERATION_DERIVED_KEYS


def test_non_faulted_iteration_has_no_cause(tmp_path):
    _write_iteration(tmp_path, 1, _meta(1))
    d = iteration_detail(tmp_path, 1)
    assert d["faultClass"] is None
    assert d["reason"] is None
    assert d["signature"] is None
    assert d["notices"] == []


def test_two_faulted_iterations_of_different_causes_each_name_their_own(tmp_path):
    """The exact shape #89's correction comment describes: an infra
    signature fault, followed by a later, unrelated work fault -- neither
    iteration's cause is lost or overwritten by the other's."""
    _write_iteration(tmp_path, 2, _meta(
        2, error=DNS_ERROR, exitCode=0, faultClass="infra"))
    _write_iteration(tmp_path, 5, _meta(
        5, error=WORK_ERROR, exitCode=1, faultClass="work",
        usage={"totalTokens": 4000, "input": 3000, "output": 1000}))

    d2 = iteration_detail(tmp_path, 2)
    assert d2["faultClass"] == "infra"
    assert d2["reason"] == FAULT_REASON_SIGNATURE
    assert d2["signature"]["family"] == "dns"
    assert d2["notices"] == []

    # Reading iteration 5 (the LATER fault) does not touch iteration 2's own
    # cause -- re-reading #2 fresh still names its own cause, unmodified.
    d5 = iteration_detail(tmp_path, 5)
    assert d5["faultClass"] == "work"
    assert d5["reason"] == FAULT_REASON_WORK
    assert d5["signature"] is None
    assert d5["notices"] == []

    d2_again = iteration_detail(tmp_path, 2)
    assert d2_again["reason"] == FAULT_REASON_SIGNATURE
    assert d2_again["signature"]["family"] == "dns"


def test_divergence_between_recorded_and_rederived_is_reported_not_resolved(tmp_path):
    """The engine recorded `work` for an error that textually matches an
    infra signature (e.g. an abort/interrupt carve-out that predates this
    engine's own classifier) -- the recorded verdict stands, and the
    disagreement is said out loud via `notices`."""
    _write_iteration(tmp_path, 3, _meta(3, error=DNS_ERROR, exitCode=0,
                                        faultClass="work"))
    d = iteration_detail(tmp_path, 3)
    assert d["faultClass"] == "work", "never overwritten by the re-derivation"
    assert d["reason"] == FAULT_REASON_SIGNATURE
    assert d["signature"]["family"] == "dns"
    assert FAULT_VERDICT_DIVERGED_NOTICE in d["notices"]


def test_hostile_meta_cannot_smuggle_a_fake_cause(tmp_path):
    """A hand-edited (or hostile) meta.json that writes `reason`/`signature`
    directly, plus a `notices` list of its own invention, gets neither
    passed through nor merged with the real derivation -- the
    `ITERATION_DERIVED_KEYS` passthrough strips all three before this
    iteration's own cause is recomputed from its OTHER (raw) fields."""
    _write_iteration(tmp_path, 4, _meta(
        4, error=DNS_ERROR, exitCode=0, faultClass="infra",
        reason="forged: everything is fine",
        signature={"family": "forged", "description": "not real"},
        notices=["forged notice"]))
    d = iteration_detail(tmp_path, 4)
    assert d["reason"] == FAULT_REASON_SIGNATURE
    assert d["signature"]["family"] == "dns"
    assert d["notices"] == []

    # And a non-faulted iteration forging `faultClass` itself as a truthy
    # non-string sentinel does not fool the guard either -- only a real
    # string faultClass triggers re-derivation.
    _write_iteration(tmp_path, 6, _meta(6, faultClass="", reason="forged"))
    d6 = iteration_detail(tmp_path, 6)
    assert d6["reason"] is None
    assert d6["signature"] is None
