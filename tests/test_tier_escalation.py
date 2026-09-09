"""Task 014 (#68 half 2, requirements E/G): escalate one tier per
validation-failed retry, and persist baseline/effective tier as
harness-owned state on the task record.

No new lifecycle edge: the escalation happens ALONGSIDE the existing
`validation-failed -> in-progress` claim (`engine.transitions.TASK_STATUS_
TABLE`/`TASK_STATUS_VIGILANT_DELTA` are untouched by this task -- see the
guard test below) -- it is state carried on the task record, not a new
status.

Layers covered:

1. `engine.state`'s pure tier helpers: escalate-by-one-capped-at-hard, no
   de-escalation ever, ladder exhaustion recorded once at the top.
2. `LoopSupervisor._step_worker`'s claim site: a `validation-failed`
   re-entry bumps `effectiveTier` on disk, a plain `pending` claim only
   seeds it at the baseline, and a worker's reported payload can never
   set/override either field itself (schema `additionalProperties: False`
   refuses the attempt as a contract violation, and the harness's own
   applier never reads such a key even if it were present).
3. `LoopSupervisor._verify_task`: a validation failure while already at
   `hard` records ladder exhaustion as a queryable fact/event.
"""

from __future__ import annotations

import asyncio
import json

from ralphd.engine import state
from ralphd.engine import tiers as T
from ralphd.engine import transitions
from ralphd.engine.config import JobConfig
from ralphd.engine.loop import LoopSupervisor
from ralphd.engine.runner import IterationResult
from ralphd.engine.state import RunDir, atomic_write_json


# ---------------------------------------------------------------------------
# 1. Pure helpers in engine.state
# ---------------------------------------------------------------------------

def test_baseline_tier_reads_the_planner_annotated_field_and_degrades_to_normal():
    assert state.task_baseline_tier({"tier": "hard"}) == "hard"
    assert state.task_baseline_tier({"tier": "bogus"}) == T.DEFAULT_TIER
    assert state.task_baseline_tier({}) == T.DEFAULT_TIER
    assert state.task_baseline_tier(None) == T.DEFAULT_TIER


def test_effective_tier_falls_back_to_baseline_when_never_escalated():
    task = {"tier": "trivial"}
    assert state.task_effective_tier(task) == "trivial"
    task2 = {"tier": "hard", "effectiveTier": "hard"}
    assert state.task_effective_tier(task2) == "hard"


def test_claim_task_effective_tier_seeds_from_baseline_once_and_never_again():
    task = {"tier": "trivial"}
    assert state.claim_task_effective_tier(task) == "trivial"
    assert task[state.TASK_EFFECTIVE_TIER_FIELD] == "trivial"
    # Manually bump it (simulating a prior escalation) and re-claim: must be
    # left alone, never reset to the baseline.
    task[state.TASK_EFFECTIVE_TIER_FIELD] = "hard"
    assert state.claim_task_effective_tier(task) == "hard"
    assert task[state.TASK_EFFECTIVE_TIER_FIELD] == "hard"


def test_escalate_is_one_step_capped_at_hard_with_no_way_back_down():
    task = {"tier": "trivial"}
    assert state.escalate_task_effective_tier(task) == "normal"
    assert state.escalate_task_effective_tier(task) == "hard"
    # Already at the ceiling: repeated escalation is a no-op, not an error,
    # and there is no argument/flag anywhere in this module that moves a
    # tier down again.
    assert state.escalate_task_effective_tier(task) == "hard"
    assert state.escalate_task_effective_tier(task) == "hard"
    assert task[state.TASK_EFFECTIVE_TIER_FIELD] == "hard"


def test_ladder_exhaustion_is_recorded_once_at_the_top_and_stays_true():
    task = {"tier": "hard", "effectiveTier": "hard"}
    assert state.mark_tier_ladder_exhausted_if_at_top(task) is True
    assert task[state.TASK_TIER_LADDER_EXHAUSTED_FIELD] is True
    # A task not yet at the top is not marked.
    task2 = {"tier": "normal", "effectiveTier": "normal"}
    assert state.mark_tier_ladder_exhausted_if_at_top(task2) is False
    assert state.TASK_TIER_LADDER_EXHAUSTED_FIELD not in task2


def test_no_new_lifecycle_edge_in_the_declared_task_status_table():
    """The escalation ladder rides along the EXISTING `validation-failed ->
    in-progress` edge -- this task adds no new status and no new transition
    to either table variant."""
    non_vigilant = transitions.task_status_table_for(False)
    vigilant = transitions.task_status_table_for(True)
    assert non_vigilant == transitions.task_status_table_for(False)
    # Task 009 (#114) adds `petitioned` to the vocabulary and v0.20
    # requirement B.1 (#156, task 019) adds `superseded` (both later waves'
    # requirements, not this task's) -- the enumeration below tracks the
    # full set as it stands today, exactly as it already absorbed
    # `awaiting-validation`/`validated` from task 019; this test's own
    # claim (no new EDGE from task 014) is the second assertion below.
    assert set(transitions.TASK_STATUSES) == {
        "pending", "in-progress", "awaiting-validation", "validated",
        "validation-failed", "completed", "failed", "skipped", "petitioned",
        "superseded"}
    assert vigilant["validation-failed"] == ("in-progress", "failed", "skipped")


# ---------------------------------------------------------------------------
# 2. The claim site inside LoopSupervisor._step_worker
# ---------------------------------------------------------------------------

def _make_sup(tmp_path, tasks, vigilant=False, iterations=1):
    root = tmp_path / "run"
    if not root.exists():
        root.mkdir(parents=True)
    run = RunDir(root=root)
    run.update_status(state="running")
    if not run.tasks_file.exists():
        atomic_write_json(run.tasks_file, {"version": 1, "tasks": tasks})
    cfg = JobConfig(run_id="task014-tier-escalation", iterations=iterations,
                    max_approaches=1, vigilant=vigilant, on_complete="exit",
                    infra_retry_backoff_s=[0.0], infra_retry_backoff_max_s=0.0,
                    infra_outage_budget_s=1000.0)
    sup = LoopSupervisor(cfg, run, root)
    return sup, run


class _NoVerdictAgent:
    """Reports nothing usable -- the claim just made must stand."""

    def interrupt(self):
        return False

    async def run(self, prompt, transcript, **kw):
        r = IterationResult(exit_code=0)
        r.final_text = "still working on it"
        r.duration_s = 0.01
        r.usage = {"input": 1, "output": 1, "totalTokens": 2}
        return r


def test_pending_claim_seeds_effective_tier_at_the_baseline(tmp_path):
    sup, run = _make_sup(tmp_path, [
        {"id": "001", "title": "t", "status": "pending",
         "successCriteria": "c", "tier": "trivial"},
    ])
    sup.runner = _NoVerdictAgent()  # type: ignore[assignment]
    asyncio.run(sup._step_worker())
    t = run.read_tasks()["tasks"][0]
    assert t["status"] == "in-progress"
    assert t[state.TASK_EFFECTIVE_TIER_FIELD] == "trivial"


def test_validation_failed_reentry_escalates_effective_tier_by_one_step(tmp_path):
    sup, run = _make_sup(tmp_path, [
        {"id": "001", "title": "t", "status": "validation-failed",
         "successCriteria": "c", "tier": "normal",
         "effectiveTier": "normal", "validationAttempts": 1},
    ])
    sup.runner = _NoVerdictAgent()  # type: ignore[assignment]
    asyncio.run(sup._step_worker())
    t = run.read_tasks()["tasks"][0]
    assert t["status"] == "in-progress"
    # normal -> hard is exactly one rung on the three-tier ladder.
    assert t[state.TASK_EFFECTIVE_TIER_FIELD] == "hard"


def test_two_validation_failed_reentries_escalate_twice_then_cap(tmp_path):
    sup, run = _make_sup(tmp_path, [
        {"id": "001", "title": "t", "status": "validation-failed",
         "successCriteria": "c", "tier": "trivial",
         "effectiveTier": "trivial", "validationAttempts": 1},
    ])
    sup.runner = _NoVerdictAgent()  # type: ignore[assignment]
    sup._approach = 1  # a run's own approach bookkeeping, not this task's concern

    asyncio.run(sup._step_worker())
    assert run.read_tasks()["tasks"][0][state.TASK_EFFECTIVE_TIER_FIELD] == "normal"

    # Simulate the next validation-failed re-entry for the same task, on
    # the SAME supervisor (iterations_used is a real, disk-derived count of
    # iterations already run, so `cfg.iterations` is bumped by exactly one
    # to give this call its own one-iteration budget, same as the first).
    tasks_doc = run.read_tasks()
    tasks_doc["tasks"][0]["status"] = "validation-failed"
    run.write_tasks(tasks_doc)
    sup.cfg.iterations += 1
    asyncio.run(sup._step_worker())
    assert run.read_tasks()["tasks"][0][state.TASK_EFFECTIVE_TIER_FIELD] == "hard"

    # A THIRD re-entry after it already reached `hard` -- no de-escalation,
    # and no escalation past the ceiling either.
    tasks_doc = run.read_tasks()
    tasks_doc["tasks"][0]["status"] = "validation-failed"
    run.write_tasks(tasks_doc)
    sup.cfg.iterations += 1
    asyncio.run(sup._step_worker())
    assert run.read_tasks()["tasks"][0][state.TASK_EFFECTIVE_TIER_FIELD] == "hard"


def test_a_worker_payload_reporting_a_tier_cannot_change_it(tmp_path):
    """A hat's contract has no `tier`/`effectiveTier` property at all, and
    `additionalProperties: False` refuses one that tries -- proving the
    field is genuinely harness-only, not merely unread.

    v0.13 task 006 (#92 site 4) strengthens this. The OLD claim, pinned
    here before the cutover, was: `sup._abort_reason and "contract
    violation" in sup._abort_reason` -- a single schema-violating payload
    was fatal on the FIRST offence, with no re-ask and no ladder at all.

    The NEW claim keeps the old one's real content -- the payload's
    `tier`/`effectiveTier` still never reach the record, the claim's own
    escalation stands untouched -- and adds what the old assertion could
    not see: the schema error was carried back to the SAME worker
    verbatim on every re-ask through `contract_enforcement.
    enforce_contract` (the EXACT SAME ladder sites 1-3 already use), the
    ladder ran the FULL `contract_retry_max + 1` attempts (never fewer,
    never more), and the outcome is an agent-fault iteration that
    continues per the table rather than aborting: no `_abort_reason`, no
    "contract violation" log message. With this test's one-iteration
    budget (`_make_sup`'s default) already spent by the single charged
    attempt, the run ends via the ordinary budget-exhausted tail
    (`outcome == ("tail",)`), not an abort.
    """
    sup, run = _make_sup(tmp_path, [
        {"id": "001", "title": "t", "status": "validation-failed",
         "successCriteria": "c", "tier": "trivial",
         "effectiveTier": "trivial", "validationAttempts": 1},
    ])

    class _SneakyAgent:
        def __init__(self):
            self.calls = 0

        def interrupt(self):
            return False

        async def run(self, prompt, transcript, **kw):
            self.calls += 1
            r = IterationResult(exit_code=0)
            r.final_text = json.dumps({
                "verdict": "complete", "task_id": "001",
                "tier": "hard", "effectiveTier": "hard"})
            r.duration_s = 0.01
            r.usage = {"input": 1, "output": 1, "totalTokens": 2}
            return r

    agent = _SneakyAgent()
    sup.runner = agent  # type: ignore[assignment]
    outcome = asyncio.run(sup._step_worker())

    assert outcome == ("tail",)
    t = run.read_tasks()["tasks"][0]
    # The claim's own escalation (trivial -> normal, applied by the harness
    # BEFORE the subprocess ran) stands; the payload's "hard" claim never
    # reached the record.
    assert t[state.TASK_EFFECTIVE_TIER_FIELD] == "normal"

    # the errors were carried back on every re-ask: the ladder ran the
    # FULL contract_retry_max + 1 attempts (never fewer, never more) --
    # every call (the one charged `run_iteration` plus every uncharged
    # `_ask_contract_fix` re-ask) lands on the SAME `_SneakyAgent.run`.
    assert agent.calls == sup.cfg.contract_retry_max + 1

    events = state.read_events(run.root)
    violation_events = [e for e in events if e.get("type") == "contract.violation"]
    fault_events = [e for e in events if e.get("type") == "contract.agent_fault"]
    assert len(violation_events) == sup.cfg.contract_retry_max + 1
    assert len(fault_events) == 1, "the ladder must be exhausted exactly once"
    assert any("tier" in e for v in violation_events for e in v["errors"])

    # agent-fault continuation, NEVER a contract-violation abort.
    assert sup._abort_reason is None
    msgs = "\n".join(ev.get("message") or "" for ev in events
                     if ev.get("type") == "log")
    assert "contract violation" not in msgs


def test_infra_fault_rollback_gives_the_tier_bump_back_too(tmp_path):
    """The pre-spawn claim is a bet the harness takes back on a pure infra
    fault (task 123) -- the tier bump that rides along with a
    validation-failed re-entry must be given back in the same rollback, or
    a phantom retry (nothing was actually attempted) would look escalated
    forever. Driven through a genuinely infra-classified fault (three
    identical instant zero-work failures -- `faults.classify_fault`'s own
    broken-environment carve-out, MAX_CONSECUTIVE_INSTANT_FAILURES), not a
    monkeypatched fault class, so the real `_run_iteration_with_infra_retry`
    wrapper is what decides `_last_fault_class` here."""
    sup, run = _make_sup(tmp_path, [
        {"id": "001", "title": "t", "status": "validation-failed",
         "successCriteria": "c", "tier": "trivial",
         "effectiveTier": "trivial", "validationAttempts": 1},
    ])

    class _BrokenEndpointAgent:
        def interrupt(self):
            return False

        async def run(self, prompt, transcript, **kw):
            # An instant, zero-work, identically-signatured failure --
            # classify_fault's "infra" case for a failure with no
            # recognisable observable work at all, repeated identically so
            # the broken-environment carve-out trips fast (3 attempts)
            # instead of sitting out the outage budget.
            r = IterationResult(exit_code=1)
            r.error_message = "no LLM credentials configured"
            r.duration_s = 0.1
            return r

    sup.runner = _BrokenEndpointAgent()  # type: ignore[assignment]

    async def no_backoff(seconds):
        return seconds, False
    sup._wait_out_backoff = no_backoff  # type: ignore[method-assign]

    asyncio.run(sup._step_worker())

    assert sup._last_fault_class == "infra"
    t = run.read_tasks()["tasks"][0]
    assert t["status"] == "validation-failed"
    assert t[state.TASK_EFFECTIVE_TIER_FIELD] == "trivial"


# ---------------------------------------------------------------------------
# 3. Ladder exhaustion recorded from `_verify_task`
# ---------------------------------------------------------------------------

def _verify_sup(tmp_path, task, make_result):
    run = RunDir(root=tmp_path)
    cfg = JobConfig(run_id="task014-verify-exhaustion", iterations=8,
                    max_approaches=1, vigilant=True, on_complete="exit",
                    infra_retry_backoff_s=[0.0], infra_retry_backoff_max_s=0.0,
                    infra_outage_budget_s=0.0)
    sup = LoopSupervisor(cfg, run, tmp_path)

    class _FixedRunner:
        def interrupt(self):
            return False

        async def run(self, prompt, transcript, **kw):
            return make_result()

    sup.runner = _FixedRunner()  # type: ignore[assignment]

    async def no_backoff(seconds):
        return seconds, False
    sup._wait_out_backoff = no_backoff  # type: ignore[method-assign]

    atomic_write_json(run.tasks_file, {"version": 1, "tasks": [task]})
    return sup


def _rejected_result():
    r = IterationResult(exit_code=0, duration_s=1.0)
    r.final_text = json.dumps({
        "task_id": "007", "verdict": "rejected", "reason": "still broken"})
    r.usage = {"input": 5, "output": 5, "totalTokens": 10}
    return r


def test_verify_failure_at_hard_records_ladder_exhaustion_field(tmp_path):
    task = {
        "id": "007", "title": "t", "status": "awaiting-validation",
        "successCriteria": "c", "tier": "hard", "effectiveTier": "hard",
        "validationAttempts": 0,
    }
    sup = _verify_sup(tmp_path, task, _rejected_result)
    verified = asyncio.run(sup._verify_task(dict(task)))
    assert verified is False

    t = sup.run.read_tasks()["tasks"][0]
    assert t["status"] == "validation-failed"
    assert t[state.TASK_TIER_LADDER_EXHAUSTED_FIELD] is True

    events = state.read_events(sup.run.root)
    assert any(e.get("signal") == "tierLadderExhausted" and
              e.get("taskId") == "007" for e in events)


def test_verify_failure_below_hard_does_not_record_ladder_exhaustion(tmp_path):
    task = {
        "id": "007", "title": "t", "status": "awaiting-validation",
        "successCriteria": "c", "tier": "normal", "effectiveTier": "normal",
        "validationAttempts": 0,
    }
    sup = _verify_sup(tmp_path, task, _rejected_result)
    asyncio.run(sup._verify_task(dict(task)))

    t = sup.run.read_tasks()["tasks"][0]
    assert t["status"] == "validation-failed"
    assert state.TASK_TIER_LADDER_EXHAUSTED_FIELD not in t

    events = state.read_events(sup.run.root)
    assert not any(e.get("signal") == "tierLadderExhausted" for e in events)
