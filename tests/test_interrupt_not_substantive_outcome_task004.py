"""Task 004 (requirement A, #72): the audit that follows task 003's fix to
`_step_review` through the rest of the engine's hat-driving paths --
`_step_generic_hat`, `_step_planning`'s inner retry
(`_one_planning_attempt`), `_step_worker`'s own stagnation bookkeeping, and
the post-terminal reflect path -- for the same shape: an iteration the
ENGINE cut short (SIGINT, a timeout, an abnormal exit -- never a genuine
judged reply) misread as a substantive outcome that costs something
irreversible.

Findings (full evidence trail lives in notes.md; this docstring is the
short version):

- `_step_generic_hat` (loop.py, pre-fix): an interrupted/timed-out hat
  attempt produced no parseable payload, which fell straight into the
  SAME branch as a genuine schema violation -- `errors` non-empty ->
  `self._abort_reason` set -> the run ends `failed`/`aborted` with a
  "contract violation" reason, over an engine-side cutoff nobody
  committed. FIXED: retries the same hat call in place while budget/wall
  clock remain (mirroring `worker`'s undeclared `no_verdict_yet`
  self-loop -- no `self._advance()` call, since a hypothetical hat's
  table entry is not guaranteed to declare a self-loop edge the way
  task 002 added one for `review`), and ends via the ordinary tail
  without ever setting `_abort_reason` once genuinely out of budget.
  Tests 1-3 below.

- `_one_planning_attempt` (loop.py, pre-fix): `_instant_failure_signature`
  deliberately returns `None` for an interrupted/timed-out/no-traffic
  result ("not this shape") specifically so `_check_instant_failure` never
  scores it as the broken-environment streak -- but nothing else caught
  it either, so the interrupted attempt fell straight through to
  `respond()` and was handed to `plan_ingestion.enforce_plan_ingestion` as
  an ORDINARY attempt, burning one of its `contract_retry_max` retries
  on something the model never got a fair shot at. Enough consecutive
  engine-side cutoffs exhausts that budget and costs the whole approach
  via the `empty_plan` edge. FIXED: the SAME retry-in-place carve-out
  `_instant_failure_streak` already gets, keyed off
  `_iteration_cut_short_by_engine` instead. Tests 4-6 below.

- `_step_worker` (loop.py, already correct): the no-progress stagnation
  counter already excludes `result.interrupted` explicitly
  (`stagnant = stagnant + 1 if (... and not result.interrupted) else 0`,
  loop.py, task 117/steer 005) -- and, independently, any iteration whose
  OWN fault class is `"infra"`/`"signal"` skips the bookkeeping entirely
  via an earlier `continue` (also task 117,
  tests/test_stagnation_excludes_nonwork_faults.py). The residual case
  that explicit `and not result.interrupted` clause alone defends --
  `result.interrupted=True` but `_last_fault_class == "work"` because
  `operator_abort_requested` was true for that one attempt (a per-
  iteration `POST /interrupt`, not a run-ending abort) -- was not
  previously pinned by a test. Test 7 below pins it directly: it must
  keep passing unmodified; this task adds no fix here.

- the reflect path (loop.py `_reflect_failure`/`_record_reflect_outcome`,
  already correct, different in KIND): reflect has no FSM edge and no
  approach/verdict at stake -- `_finish()` has already settled the run's
  real `state`/`verdict` by the time reflect ever runs (task 012, #56).
  `reflect.ok=False` for an interrupted reflect attempt is an ACCURATE
  report ("no post-mortem was produced"), not a misread of the RUN's own
  outcome, and it cannot rewrite `state`/`verdict` (asserted directly in
  `_record_reflect_outcome`'s own docstring and pinned by test 8 below).
  This task adds no fix here either.

tests/test_stagnation_excludes_nonwork_faults.py and task 117's own
fault-shape tests (tests/test_aborted_stream_fault_class.py,
tests/test_fault_explanation.py) are untouched by this module and still
pass.
"""

from __future__ import annotations

import asyncio
import copy
import json

import pytest

from ralphd.engine import transitions as tr
from ralphd.engine.config import JobConfig
from ralphd.engine.loop import LoopSupervisor
from ralphd.engine.runner import IterationResult
from ralphd.engine.state import RunDir, read_events

TEST_STATE = "task004_audit_hat_state"
TEST_HAT = "task004_audit_hat"


# ---------------------------------------------------------------------------
# shared scaffolding
# ---------------------------------------------------------------------------

def _ok(text: str = "{}") -> IterationResult:
    r = IterationResult(exit_code=0)
    r.final_text = text
    r.duration_s = 30.0
    r.usage = {"input": 10, "output": 5, "totalTokens": 15}
    return r


def _interrupted(text: str = "partial output before the cutoff") -> IterationResult:
    """The engine's own cutoff shape (fact 9): a signal-terminated attempt
    that DID produce traffic, so `classify_fault` scores it `"signal"`
    (never `"infra"`, never retried by `_run_iteration_with_infra_retry`)
    and the caller under test is the only thing left to discriminate it
    from a genuine reply."""
    r = IterationResult(exit_code=None, interrupted=True)
    r.final_text = text
    r.usage = {"input": 3, "output": 1, "totalTokens": 4}
    r.duration_s = 5.0
    return r


def _supervisor(tmp_path, **cfg_kw) -> LoopSupervisor:
    run = RunDir(root=tmp_path)
    kw = {"infra_retry_backoff_s": [0.0], "infra_retry_backoff_max_s": 0.0,
          "infra_outage_budget_s": 1000.0, "vigilant": False,
          "max_approaches": 2, "on_complete": "exit",
          **cfg_kw}
    cfg = JobConfig(run_id="task004-audit", **kw)
    return LoopSupervisor(cfg, run, tmp_path)


def _stub_attempts(sup: LoopSupervisor, results: list[IterationResult]) -> list[str]:
    """Same technique as tests/test_instant_infra_retry.py's `_stub_attempts`:
    stub `_run_iteration_once` (below the infra-retry wrapper, so the real
    wrapper's own infra-vs-not decision still runs) and record which phase
    was asked, in order.

    Steer 003's finding: `_run_iteration_once` is the ONLY place
    `self.iterations_used` moves (loop.py:1055/1060, `n =
    self.iterations_used + 1` ... `self.iterations_used = n`), and
    `budget_left()` (loop.py:1449-1456) is computed entirely from that
    counter. A stub that replaces the method wholesale but never touches
    the counter leaves `budget_left()` permanently True, so any retry
    loop keyed off it (`_step_generic_hat`'s `while True`,
    `_one_planning_attempt`'s) spins forever instead of ever reaching its
    budget-exhausted branch -- this is exactly what hung iteration 14 for
    95 minutes on tests 2/5/6b below. Faithful to what it stands in for:
    charge the counter on every call, same as the real method does."""
    calls: list[str] = []

    async def fake_once(phase, extra="", prompt_name=None):
        calls.append(phase)
        sup.iterations_used += 1
        return copy.copy(results[min(len(calls) - 1, len(results) - 1)])

    sup._run_iteration_once = fake_once  # type: ignore[method-assign]
    return calls


@pytest.fixture
def registered_test_hat():
    """A trivial single-edge hat, registered/torn down the same way
    tests/test_hat_extensibility.py's `registered_noop_hat` does, minus the
    `ENTRY_STATE`/prompt-file bookkeeping this module's direct-call tests
    never need (nothing here goes through `_run_job_core`'s dispatch
    entry -- `_step_generic_hat` is called directly with an explicit
    `state`/`edges` pair)."""
    assert TEST_STATE not in tr.TABLE, "test fixture name collided with a real state"
    tr.TABLE[TEST_STATE] = (tr.T("planning", "task004_audit_done"),)
    tr.STATE_HAT[TEST_STATE] = TEST_HAT
    tr.HAT_SCHEMAS[TEST_HAT] = {
        "type": "object", "properties": {}, "additionalProperties": False}
    try:
        yield TEST_STATE
    finally:
        tr.TABLE.pop(TEST_STATE, None)
        tr.STATE_HAT.pop(TEST_STATE, None)
        tr.HAT_SCHEMAS.pop(TEST_HAT, None)


def _log_messages(sup: LoopSupervisor) -> str:
    return "\n".join(
        e.get("message") or "" for e in read_events(sup.run.root, types=("log",)))


# ---------------------------------------------------------------------------
# 1-3. `_step_generic_hat`
# ---------------------------------------------------------------------------

def test_1_generic_hat_retries_in_place_when_interrupted_with_budget_left(
        tmp_path, registered_test_hat):
    sup = _supervisor(tmp_path, iterations=5)
    calls = _stub_attempts(sup, [_interrupted(), _ok("{}")])
    edges = tr.successors(TEST_STATE, sup.cfg.vigilant)

    outcome = asyncio.run(sup._step_generic_hat(TEST_STATE, edges))

    assert calls == [TEST_STATE, TEST_STATE]  # retried once, in place
    assert outcome == ("advance", "planning", "task004_audit_done")
    assert sup._abort_reason is None
    assert "contract violation" not in _log_messages(sup)


def test_2_generic_hat_interrupted_at_budget_end_is_not_a_contract_violation(
        tmp_path, registered_test_hat):
    sup = _supervisor(tmp_path, iterations=1)  # exhausted after one attempt
    calls = _stub_attempts(sup, [_interrupted()])
    edges = tr.successors(TEST_STATE, sup.cfg.vigilant)

    outcome = asyncio.run(sup._step_generic_hat(TEST_STATE, edges))

    assert calls == [TEST_STATE]
    assert outcome == ("tail",)
    # THE regression this guards: before the fix, this exact scenario set
    # `_abort_reason` to a "contract violation" message and the run ended
    # aborted/failed over an engine-side cutoff nobody committed.
    assert sup._abort_reason is None
    assert "contract violation" not in _log_messages(sup)
    assert sup._terminal_reason_note is not None
    assert "cut short" in sup._terminal_reason_note
    # The note itself SAYS "not ... a contract violation" (that is the
    # point being recorded) -- assert the denial, not a bare substring
    # absence that would flag the note's own honest phrasing as a defect.
    assert "not read as a contract violation" in sup._terminal_reason_note


def test_3_mutation_stops_discriminating_generic_hat_reads_interrupt_as_contract_violation(
        tmp_path, registered_test_hat, monkeypatch):
    """The paired mutation: undo the fix (make the discrimination always
    say "this was not a cutoff") and show the SAME scenario as test 2 now
    DOES set `_abort_reason` to a contract-violation message -- proving
    test 2 actually exercises the discrimination rather than passing by
    construction."""
    monkeypatch.setattr(
        LoopSupervisor, "_iteration_cut_short_by_engine",
        staticmethod(lambda result: False))
    sup = _supervisor(tmp_path, iterations=1)
    calls = _stub_attempts(sup, [_interrupted()])
    edges = tr.successors(TEST_STATE, sup.cfg.vigilant)

    outcome = asyncio.run(sup._step_generic_hat(TEST_STATE, edges))

    assert calls == [TEST_STATE]
    assert outcome == ("tail",)
    assert sup._abort_reason is not None
    assert "contract violation" in sup._abort_reason


# ---------------------------------------------------------------------------
# 4-6. `_one_planning_attempt`
# ---------------------------------------------------------------------------

def test_4_one_planning_attempt_retries_in_place_when_interrupted_with_budget_left(
        tmp_path):
    sup = _supervisor(tmp_path, iterations=5)
    calls = _stub_attempts(sup, [_interrupted(), _ok("planned\n{}")])

    presult = asyncio.run(sup._one_planning_attempt(""))

    assert calls == ["planning", "planning"]
    assert presult is not None
    assert presult.interrupted is False
    assert presult.final_text == "planned\n{}"


def test_5_one_planning_attempt_falls_through_only_once_budget_is_actually_gone(
        tmp_path):
    """Once the run's own budget is exhausted (not `enforce_plan_ingestion`'s
    retry budget -- this method's caller's business, tested at the
    `_step_planning` level in test 6), the interrupted result is finally
    handed back rather than retried forever."""
    sup = _supervisor(tmp_path, iterations=1)
    calls = _stub_attempts(sup, [_interrupted()])

    presult = asyncio.run(sup._one_planning_attempt(""))

    assert calls == ["planning"]
    assert presult is not None
    assert presult.interrupted is True


def test_6_mutation_stops_discriminating_planning_burns_a_contract_retry_on_a_cutoff(
        tmp_path, monkeypatch):
    """The paired mutation: with the discrimination removed, an interrupted
    attempt is handed straight to the caller on the FIRST try even though
    budget remains -- the exact defect this task's fix removes (it would
    otherwise burn one of `plan_ingestion.enforce_plan_ingestion`'s finite
    retries on an attempt the model never got a fair shot at)."""
    monkeypatch.setattr(
        LoopSupervisor, "_iteration_cut_short_by_engine",
        staticmethod(lambda result: False))
    sup = _supervisor(tmp_path, iterations=5)
    calls = _stub_attempts(sup, [_interrupted(), _ok("planned\n{}")])

    presult = asyncio.run(sup._one_planning_attempt(""))

    # Without the fix, only ONE attempt is made (the interrupted one is
    # handed straight back) even though 4 more iterations of budget remain.
    assert calls == ["planning"]
    assert presult is not None
    assert presult.interrupted is True


def test_6b_step_planning_interrupted_at_budget_end_does_not_cost_the_approach(
        tmp_path):
    """End-to-end at the `_step_planning` level: budget exhausted while
    planning kept getting cut short by the engine must not read as
    "planning produced no usable plan proposal" (the `empty_plan` edge,
    which advances to `replan` and would consume this approach) -- it
    must fall straight to the ordinary tail, exactly like `_step_planning`
    already does for the pre-existing "budget ran out mid-ingestion"
    case, and the approach counter must not move."""
    sup = _supervisor(tmp_path, iterations=1)
    # `_step_planning` reads `self._approach` (only set for real by
    # `_run_job_core`, loop.py:2163) to stamp `status.json`'s `approach`
    # field -- this test drives `_step_planning` directly, so it has to
    # set the same thing `_run_job_core` would have.
    sup._approach = 1
    calls = _stub_attempts(sup, [_interrupted()])

    outcome = asyncio.run(sup._step_planning(skip_planning=False))

    assert calls == ["planning"]
    assert outcome == ("tail",)
    assert sup._abort_reason is None
    assert "planning produced no usable plan proposal" not in _log_messages(sup)
    assert sup.run.read_status().get("approach") == 1


# ---------------------------------------------------------------------------
# 7. `_step_worker`'s stagnation counter (already correct -- pinned, not
#    fixed): the residual case
#    tests/test_stagnation_excludes_nonwork_faults.py does not reach --
#    `result.interrupted=True` with `_last_fault_class == "work"` because
#    THIS attempt's own `operator_abort_requested` was true (a
#    per-iteration `POST /interrupt`, not a run-ending abort), so the
#    earlier `_last_fault_class in ("infra", "signal")` gate does NOT
#    short-circuit before the stagnation line -- only the explicit
#    `and not result.interrupted` clause does.
# ---------------------------------------------------------------------------

def test_7_stagnation_counter_excludes_an_operator_interrupted_work_classified_iteration(
        tmp_path):
    from test_failed_task_vocabulary import _steps, _write_proposal

    def _plan(sup, prompt=""):
        _write_proposal(sup, [{"id": "001", "title": "do the thing",
                              "successCriteria": "the thing is done"}])
        return _ok("planned\n" + json.dumps(
            {"proposal_path": "plan-proposal.json", "summary": "planned"}))

    def _operator_interrupted_worker(sup, prompt=""):
        # A per-iteration POST /interrupt, delivered while this attempt was
        # running: `operator_abort_requested` reads True for THIS attempt's
        # `_classify_result` (`_abort_reason` stays None -- no run-ending
        # abort is recorded), so `classify_fault`'s operator-abort carve-out
        # scores it `"work"`, not `"signal"`/`"infra"` -- the ONLY thing
        # left to exclude it from stagnation is `result.interrupted` itself.
        sup._operator_interrupted = True
        r = IterationResult(exit_code=None, interrupted=True)
        r.final_text = "cut short mid-task by a per-iteration interrupt"
        r.usage = {"input": 4, "output": 2, "totalTokens": 6}
        r.duration_s = 12.0
        return r

    class _Runner:
        def __init__(self, sup, handlers):
            self.sup = sup
            self.handlers = handlers

        def interrupt(self):
            return False

        async def run(self, prompt, transcript, **kw):
            phase = self.sup.run.read_status().get("phase")
            return self.handlers[phase](self.sup, prompt)

    root = tmp_path / "run"
    root.mkdir()
    run = RunDir(root=root)
    run.update_status(state="starting")
    sup = _supervisor(root, iterations=4, max_approaches=1)
    handlers = {"planning": _plan,
               "worker": _steps(_operator_interrupted_worker,
                                _operator_interrupted_worker,
                                _operator_interrupted_worker)}
    sup.run = run
    sup.runner = _Runner(sup, handlers)  # type: ignore[assignment]

    async def no_backoff(seconds):
        return seconds, False

    sup._wait_out_backoff = no_backoff  # type: ignore[method-assign]

    state = asyncio.run(sup._run_job_core())

    msgs = _log_messages(sup)
    assert "3 iterations with no task progress" not in msgs, msgs
    assert "failing approach" not in msgs, msgs
    faults = [ev.get("faultClass") for ev in read_events(sup.run.root)
             if ev.get("type") == "iteration.end" and ev.get("phase") == "worker"]
    assert faults and set(faults) == {"work"}, faults
    # Budget ran out mid-worker-loop (4 iterations: 1 planning + 3 worker),
    # none of which counted as stagnation evidence -- the run ends
    # unverified, not `succeeded`, and NOT via the stagnation guard.
    assert state == "failed"
    assert sup.run.read_status()["verdict"] == "unverified"


# ---------------------------------------------------------------------------
# 8. the reflect path (already correct -- pinned, not fixed): an
#    interrupted reflect attempt is reported honestly (`reflect.ok=False`)
#    but cannot rewrite the run's OWN already-settled `state`/`verdict` --
#    the thing that would make it a "substantive outcome" misread in the
#    requirement A sense.
# ---------------------------------------------------------------------------

def test_8_reflect_interrupted_records_failure_without_touching_the_run_outcome(
        tmp_path):
    sup = _supervisor(tmp_path, iterations=10)
    sup.run.update_status(state="succeeded", verdict="verified")
    _stub_attempts(sup, [_interrupted("cut short during reflection")])

    asyncio.run(sup._run_reflection())

    status = sup.run.read_status()
    # the run's own settled outcome is untouched
    assert status["state"] == "succeeded"
    assert status["verdict"] == "verified"
    # reflect's own (accurate) verdict is recorded separately
    assert status["reflect"]["ok"] is False
    assert "interrupted" in status["reflect"]["error"]
    assert (sup.run.artifacts_dir / "reflection" / "FAILED.md").exists()
