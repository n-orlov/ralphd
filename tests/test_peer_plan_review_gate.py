"""Task 018 (PRD requirement K, #101): the opt-in peer plan-review gate.

`peer_plan_review: false` (the default) must reproduce today's engine
BYTE-FOR-BYTE: the composed transition table, the dispatch loop's behaviour,
and the shipped `MODEL_HATS`/`HAT_SCHEMAS` inventory all gain a real
`plan_review` entry (exactly like `verify` exists in both vigilant variants
even though only one of them ever reaches it), but nothing about a
gate-off run's OWN reachable table changes.

`peer_plan_review: true` inserts one hat between `planning` and `worker`:
it either approves the freshly ingested plan (`worker` proceeds unchanged)
or objects with a reason, routing back to `planning` for a revision --
bounded by `peer_plan_review_max_revisions` (an explicit NUMBER, decided
and recorded in this task's own commit message and
`artifacts/reports/task-018-requirement-k-budget-decision.md`), never
"until it agrees". Once the bound is spent the plan proceeds to `worker`
regardless -- a bounded gate fails OPEN, it never deadlocks or faults a
run.

Two layers, both exercised here:

1. the pure table/schema/prompt/MODEL_HATS entry (mirrors
   `tests/test_steer_hat_table_entry.py`'s shape for `steer`);
2. a real `LoopSupervisor`, stubbed `sup.runner`, driven through
   `sup._run_job_core()` -- the same scaffolding
   `tests/test_steering_priority_routing_task012.py` uses -- proving the
   approval path, the objection-then-revision path, the revision-bound
   fail-open path, and the off-by-default byte-identical table.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from ralphd.engine import contracts
from ralphd.engine import tiers as T
from ralphd.engine import transitions as tr
from ralphd.engine.config import (DEFAULT_HAT_MODEL_REF, MODEL_HATS,
                                  UNRECOGNISED_HAT_FALLBACK, JobConfig)
from ralphd.engine.loop import LoopSupervisor
from ralphd.engine.runner import IterationResult
from ralphd.engine.state import RunDir, atomic_write_json

REPO_ROOT = Path(__file__).resolve().parents[1]
PLAN_REVIEW_PROMPT = REPO_ROOT / "src" / "ralphd" / "prompts" / "plan_review.md"

TASK_1 = "001"


# ---------------------------------------------------------------------------
# 1. off by default: the composed table is byte-identical to today's
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("vigilant", [True, False])
def test_gate_off_by_default_table_is_byte_identical_to_today(vigilant):
    """`table_for(vigilant)` (the pre-task-018 call shape, no third
    argument) and `table_for(vigilant, peer_plan_review=False)` -- the
    ONLY two ways every pre-existing caller invokes it -- must produce the
    exact same dict: same keys, same values, same tuple identity of
    content. A `plan_review` key silently appearing (even mapped to `()`)
    would NOT be byte-identical to a dict that lacks the key entirely."""
    today = tr.table_for(vigilant)
    off = tr.table_for(vigilant, peer_plan_review=False)
    assert today == off
    assert "plan_review" not in today
    assert "plan_review" not in off
    assert today["planning"] == (
        tr.T("planning", "instant_failure_streak"),
        tr.T("replan", "empty_plan"),
        tr.T("worker", "planning_produced_tasks"),
    )


def test_gate_off_by_default_on_a_default_jobconfig():
    assert JobConfig().peer_plan_review is False


def test_gate_off_transition_executor_matches_the_two_arg_call():
    executor_default = tr.TransitionExecutor(vigilant=False)
    executor_explicit_off = tr.TransitionExecutor(vigilant=False, peer_plan_review=False)
    assert executor_default.advance("planning", "worker", "planning_produced_tasks") == "worker"
    assert executor_explicit_off.advance("planning", "worker", "planning_produced_tasks") == "worker"
    with pytest.raises(tr.IllegalTransition):
        executor_default.advance("planning", "plan_review",
                                 "planning_produced_tasks_peer_review_enabled")


# ---------------------------------------------------------------------------
# 2. on: the declared edges, and nothing undeclared
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("vigilant", [True, False])
def test_plan_review_is_in_the_state_inventory(vigilant):
    assert "plan_review" in tr.ALL_STATES
    assert tr.successors("plan_review", vigilant, peer_plan_review=False) == ()
    succ = tr.successors("plan_review", vigilant, peer_plan_review=True)
    assert succ != (), "plan_review must not be a dead end when the gate is on"
    assert all(edge.target in tr.ALL_STATES for edge in succ)


@pytest.mark.parametrize("vigilant", [True, False])
def test_planning_routes_to_plan_review_only_when_the_gate_is_on(vigilant):
    assert not tr.is_legal("planning", "plan_review", vigilant, peer_plan_review=False)
    assert tr.is_legal("planning", "plan_review", vigilant, peer_plan_review=True)
    edges = tr.successors("planning", vigilant, peer_plan_review=True)
    peer_edges = [e for e in edges if e.target == "plan_review"]
    assert len(peer_edges) == 1
    assert peer_edges[0].guard == "planning_produced_tasks_peer_review_enabled"
    # and `planning -> worker` under the OLD guard is gone once the gate
    # is on -- the two guards must never both be legal at once, or a doc
    # reader could not tell which one actually fires.
    assert not tr.is_legal("planning", "worker", vigilant, peer_plan_review=True)


@pytest.mark.parametrize("vigilant", [True, False])
def test_plan_review_has_exactly_the_three_declared_edges(vigilant):
    edges = tr.successors("plan_review", vigilant, peer_plan_review=True)
    assert edges == (
        tr.T("worker", "plan_review_approved"),
        tr.T("planning", "plan_review_objected_revision_available"),
        tr.T("worker", "plan_review_revision_bound_reached"),
    )


@pytest.mark.parametrize("vigilant", [True, False])
def test_an_undeclared_target_out_of_plan_review_is_refused(vigilant):
    assert not tr.is_legal("plan_review", "bogus_undeclared_target", vigilant,
                           peer_plan_review=True)
    with pytest.raises(tr.IllegalTransition):
        tr.TransitionExecutor(vigilant, peer_plan_review=True).advance(
            "plan_review", "bogus_undeclared_target", "plan_review_approved")


@pytest.mark.parametrize("vigilant", [True, False])
def test_no_other_state_declares_an_edge_into_plan_review_except_planning(vigilant):
    table = tr.table_for(vigilant, peer_plan_review=True)
    sources = {state for state, edges in table.items()
              if any(edge.target == "plan_review" for edge in edges)}
    assert sources == {"planning"}


# ---------------------------------------------------------------------------
# 3. STATE_HAT / HATS / HAT_SCHEMAS entries
# ---------------------------------------------------------------------------


def test_state_hat_entry():
    assert tr.STATE_HAT["plan_review"] == "plan_review"


def test_plan_review_is_in_hats():
    assert "plan_review" in tr.HATS


def test_hat_schemas_entry_shape():
    schema = tr.HAT_SCHEMAS["plan_review"]
    assert schema["type"] == "object"
    assert schema["required"] == ["verdict"]
    assert schema["properties"]["verdict"]["enum"] == ["approved", "objected"]
    assert schema["additionalProperties"] is False


def test_every_hat_including_plan_review_has_exactly_one_schema():
    assert contracts.hats_with_schemas() == frozenset(tr.HATS)
    assert "plan_review" in contracts.hats_with_schemas()


def test_an_approved_payload_validates():
    assert contracts.validate_payload("plan_review", {"verdict": "approved"}) == []


def test_an_objected_payload_with_a_reason_validates():
    errors = contracts.validate_payload(
        "plan_review", {"verdict": "objected", "reason": "missing a requirement"})
    assert errors == []


def test_an_objected_payload_with_no_reason_is_a_semantic_violation():
    payload = {"verdict": "objected"}
    assert contracts.validate_payload("plan_review", payload) == []
    assert tr.plan_review_report_errors(payload) != []


def test_an_objected_payload_with_a_blank_reason_is_a_semantic_violation():
    payload = {"verdict": "objected", "reason": "   "}
    assert tr.plan_review_report_errors(payload) != []


def test_an_approved_payload_has_no_reason_requirement():
    assert tr.plan_review_report_errors({"verdict": "approved"}) == []


def test_a_payload_missing_verdict_is_rejected():
    errors = contracts.validate_payload("plan_review", {"reason": "x"})
    assert errors


def test_an_extra_property_is_rejected():
    errors = contracts.validate_payload(
        "plan_review", {"verdict": "approved", "task_status": "completed"})
    assert errors


def test_an_unrecognised_verdict_value_is_rejected():
    errors = contracts.validate_payload("plan_review", {"verdict": "verified"})
    assert errors


# ---------------------------------------------------------------------------
# 4. the prompt exists and states the bounded-revision contract
# ---------------------------------------------------------------------------


def test_plan_review_prompt_exists():
    assert PLAN_REVIEW_PROMPT.is_file(), f"missing {PLAN_REVIEW_PROMPT}"


def test_plan_review_prompt_declares_the_verdict_contract():
    text = PLAN_REVIEW_PROMPT.read_text()
    assert '"approved"' in text
    assert '"objected"' in text
    assert "reason" in text


def test_plan_review_prompt_says_objecting_is_bounded():
    text = PLAN_REVIEW_PROMPT.read_text().lower()
    assert "bounded" in text


# ---------------------------------------------------------------------------
# 5. config.MODEL_HATS gains a REAL entry, tier rungs included
# ---------------------------------------------------------------------------


def test_plan_review_is_a_real_model_hats_entry():
    assert "plan_review" in MODEL_HATS
    assert "plan_review" != UNRECOGNISED_HAT_FALLBACK


def test_plan_review_shipped_default_matches_every_other_known_hat():
    cfg = JobConfig()
    assert cfg.model_for("plan_review") is DEFAULT_HAT_MODEL_REF is None
    cfg2 = JobConfig(model="shipped-model")
    assert cfg2.model_for("plan_review") == "shipped-model"


def test_plan_review_has_tier_rungs_via_all_hat_tier_pairs():
    pairs = T.all_hat_tier_pairs()
    peer_pairs = {(h, t) for h, t in pairs if h == "plan_review"}
    assert peer_pairs == {("plan_review", tier) for tier in T.TIERS}


def test_peer_plan_review_max_revisions_default():
    assert JobConfig().peer_plan_review_max_revisions == 2


# ---------------------------------------------------------------------------
# 6. a real LoopSupervisor, driven end to end
# ---------------------------------------------------------------------------


def _ok(text: str) -> IterationResult:
    r = IterationResult(exit_code=0)
    r.final_text = text
    r.duration_s = 5.0
    r.usage = {"input": 10, "output": 5, "totalTokens": 15}
    return r


def _plan(task_id: str = TASK_1):
    def handler(sup, prompt=""):
        atomic_write_json(sup.run.root / "plan-proposal.json", {
            "version": 1,
            "tasks": [{"id": task_id, "title": "t", "successCriteria": "c"}],
        })
        return _ok(json.dumps(
            {"proposal_path": "plan-proposal.json", "summary": "planned"}))
    return handler


def _plan_review_approves(sup, prompt=""):
    return _ok(json.dumps({"verdict": "approved"}))


def _plan_review_objects(reason: str):
    def handler(sup, prompt=""):
        return _ok(json.dumps({"verdict": "objected", "reason": reason}))
    return handler


def _worker_completes(task_id: str):
    def handler(sup, prompt=""):
        return _ok(json.dumps({"verdict": "complete", "task_id": task_id}))
    return handler


def _review_verifies(sup, prompt=""):
    return _ok(json.dumps({"verdict": "verified"}))


class _ScriptedAgent:
    """Same shape as
    `tests/test_steering_priority_routing_task012.py::_ScriptedAgent`."""

    def __init__(self, handlers: dict):
        self.handlers = handlers
        self.calls: dict[str, int] = {}
        self.phases_seen: list[str] = []
        self.prompts: list[str] = []
        self.running = False

    def interrupt(self) -> bool:
        return self.running

    async def run(self, prompt, transcript, **kw) -> IterationResult:
        phase = self.sup.run.read_status().get("phase")
        self.phases_seen.append(phase)
        self.prompts.append(prompt)
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
    sup = LoopSupervisor(JobConfig(run_id="task018-plan-review", **kw), run, root)
    agent = _ScriptedAgent(handlers)
    agent.sup = sup                      # type: ignore[attr-defined]
    sup.runner = agent                    # type: ignore[assignment]

    async def no_backoff(seconds):
        return seconds, False

    sup._wait_out_backoff = no_backoff    # type: ignore[method-assign]
    return sup


def _run_job(root: Path, handlers: dict, **cfg_kw) -> tuple[LoopSupervisor, str]:
    RunDir(root=root).update_status(state="starting")
    sup = _supervisor(root, handlers, **cfg_kw)
    return sup, asyncio.run(sup._run_job_core())


def test_gate_off_never_runs_the_plan_review_phase_at_all(tmp_path):
    root = tmp_path / "run"
    sup, final_state = _run_job(
        root,
        {"planning": [_plan()], "worker": [_worker_completes(TASK_1)],
         "review": [_review_verifies]},
        peer_plan_review=False)

    agent = sup.runner
    assert "plan_review" not in agent.phases_seen
    assert final_state == "succeeded", sup.run.read_status()
    assert agent.phases_seen == ["planning", "worker", "review"]


def test_gate_on_approval_path_proceeds_straight_to_worker(tmp_path):
    root = tmp_path / "run"
    sup, final_state = _run_job(
        root,
        {"planning": [_plan()], "plan_review": [_plan_review_approves],
         "worker": [_worker_completes(TASK_1)], "review": [_review_verifies]},
        peer_plan_review=True)

    agent = sup.runner
    assert agent.phases_seen == ["planning", "plan_review", "worker", "review"]
    assert agent.calls == {"planning": 1, "plan_review": 1, "worker": 1,
                           "review": 1}
    assert final_state == "succeeded", sup.run.read_status()
    # a bare approval spends no revision.
    assert sup.run.read_status().get("planReviewRevisions") is None


def test_gate_on_objection_then_revision_then_approval(tmp_path):
    root = tmp_path / "run"
    sup, final_state = _run_job(
        root,
        {"planning": [_plan(), _plan()],
         "plan_review": [_plan_review_objects("the plan omits the PRD's "
                                              "logging requirement"),
                         _plan_review_approves],
         "worker": [_worker_completes(TASK_1)], "review": [_review_verifies]},
        peer_plan_review=True)

    agent = sup.runner
    assert agent.phases_seen == [
        "planning", "plan_review", "planning", "plan_review", "worker",
        "review"]
    assert agent.calls == {"planning": 2, "plan_review": 2, "worker": 1,
                           "review": 1}
    assert final_state == "succeeded", sup.run.read_status()

    # the objection's reason reached the SECOND planning attempt's prompt.
    second_planning_prompt = agent.prompts[agent.phases_seen.index(
        "planning", 1)]
    assert "the plan omits the PRD's logging requirement" in \
        second_planning_prompt

    # the revision was persisted, disk-side, for approach 1.
    revisions = sup.run.read_status().get("planReviewRevisions")
    assert revisions == {"1": 1}


def test_gate_on_revision_bound_reached_fails_open_to_worker(tmp_path):
    """`peer_plan_review_max_revisions=1`: the first objection still gets a
    revision (planning runs again), but the SECOND objection has none left
    -- the plan proceeds to `worker` regardless, rather than looping or
    ending the run."""
    root = tmp_path / "run"
    sup, final_state = _run_job(
        root,
        {"planning": [_plan(), _plan()],
         "plan_review": [_plan_review_objects("first objection"),
                         _plan_review_objects("second objection, bound spent")],
         "worker": [_worker_completes(TASK_1)], "review": [_review_verifies]},
        peer_plan_review=True, peer_plan_review_max_revisions=1)

    agent = sup.runner
    assert agent.phases_seen == [
        "planning", "plan_review", "planning", "plan_review", "worker",
        "review"]
    assert agent.calls == {"planning": 2, "plan_review": 2, "worker": 1,
                           "review": 1}
    assert final_state == "succeeded", sup.run.read_status()
    revisions = sup.run.read_status().get("planReviewRevisions")
    assert revisions == {"1": 1}, (
        "the bound-reached objection must NOT increment the counter past "
        "the configured bound")


def test_gate_on_malformed_plan_review_payload_is_treated_as_an_objection(tmp_path):
    """An absent/malformed payload is a safe-default implicit objection --
    it still spends a revision -- never a run-ending contract violation,
    the same philosophy `_review_verdict` already applies to review."""
    def _garbage(sup, prompt=""):
        return _ok("not json at all")

    root = tmp_path / "run"
    sup, final_state = _run_job(
        root,
        {"planning": [_plan(), _plan()],
         "plan_review": [_garbage, _plan_review_approves],
         "worker": [_worker_completes(TASK_1)], "review": [_review_verifies]},
        peer_plan_review=True)

    agent = sup.runner
    assert agent.phases_seen == [
        "planning", "plan_review", "planning", "plan_review", "worker",
        "review"]
    assert final_state == "succeeded", sup.run.read_status()
    revisions = sup.run.read_status().get("planReviewRevisions")
    assert revisions == {"1": 1}


def test_the_router_is_never_consulted_for_plan_review(tmp_path):
    """Task 018's own dedicated `_step_plan_review` picks its own edge --
    it must never fall through to `_step_generic_hat`/`engine.router.decide`
    (that widening is task 019's business, requirement M)."""
    from ralphd.engine import router as router_module

    router_calls = []
    orig_decide = router_module.decide

    async def spying_decide(*args, **kwargs):
        router_calls.append((args, kwargs))
        return await orig_decide(*args, **kwargs)

    root = tmp_path / "run"
    router_module.decide = spying_decide
    try:
        sup, final_state = _run_job(
            root,
            {"planning": [_plan()], "plan_review": [_plan_review_approves],
             "worker": [_worker_completes(TASK_1)],
             "review": [_review_verifies]},
            peer_plan_review=True)
    finally:
        router_module.decide = orig_decide

    assert final_state == "succeeded", sup.run.read_status()
    assert router_calls == [], (
        "plan_review's own verdict must decide its edge without ever "
        "consulting engine.router.decide")
