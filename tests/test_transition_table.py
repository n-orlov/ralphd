"""Task 008: the declared transition table is data, complete, and the two
variants differ only by the declared delta — not by two hand-written tables.

Task 014 cut the engine over to this table (`engine.loop`'s `_run_job_core`
now drives it through `TransitionExecutor`); the behavioural proof of that
cutover (illegal transition refused, variants differ only by selection, no
legacy dual path) lives in `tests/test_transition_executor_cutover.py`, not
here. These tests only prove the table itself is a correct, enumerable
design.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from ralphd.engine import transitions as tr

REPO_ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = REPO_ROOT / "src" / "ralphd" / "engine" / "transitions.py"


# ---------------------------------------------------------------------------
# Every state's successors are enumerable
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("vigilant", [True, False])
def test_every_declared_state_has_enumerable_successors(vigilant):
    for state in tr.ALL_STATES:
        succ = tr.successors(state, vigilant)
        assert isinstance(succ, tuple)
        for edge in succ:
            assert isinstance(edge, tr.Transition)
            assert edge.target in tr.ALL_STATES
            assert isinstance(edge.guard, str) and edge.guard


def test_terminal_states_have_no_successors_and_nothing_else_does():
    # `verify` is the one declared exception: it is a real hat-bearing
    # state in both variants (STATE_HAT/HATS), but it is only *reachable*
    # -- and only has successors -- under vigilant (VIGILANT_DELTA). It is
    # not a terminal state; it is simply unreachable, non-terminal state
    # under non-vigilant, which is exactly what the delta declares.
    #
    # `plan_review` (task 018, requirement K, #101) is the SAME shape on a
    # DIFFERENT axis: it is a real hat-bearing state either way, but this
    # test never turns `peer_plan_review` on (it only parametrizes
    # `vigilant`), so its successors are `()` regardless of `vigilant`
    # here -- reachability is gated by `JobConfig.peer_plan_review`
    # (default OFF), never by `vigilant` at all.
    for vigilant in (True, False):
        for state in tr.ALL_STATES:
            succ = tr.successors(state, vigilant)
            if state in tr.RUN_TERMINAL_STATES:
                assert succ == (), f"{state} ({vigilant=}) must be terminal"
            elif state == "verify" and not vigilant:
                assert succ == (), "verify is unreachable, not terminal, non-vigilant"
            elif state == "plan_review":
                assert succ == (), (
                    "plan_review is unreachable, not terminal, when "
                    "peer_plan_review is off (this test's default)")
            elif state == "verdict_gate":
                assert succ == (), (
                    "verdict_gate is unreachable, not terminal, when "
                    "peer_verdict_gate is off (this test's default) -- "
                    "task 020, requirement L, #101, the SAME shape as "
                    "plan_review just above, on its own axis")
            else:
                assert succ != (), (
                    f"{state} ({vigilant=}) is not terminal but has no "
                    "successors")


def test_entry_state_is_planning():
    assert tr.ENTRY_STATE == "planning"
    assert tr.ENTRY_STATE in tr.ALL_STATES


# ---------------------------------------------------------------------------
# The two variants differ ONLY by the declared delta
# ---------------------------------------------------------------------------

def test_vigilant_and_non_vigilant_differ_only_by_the_declared_delta():
    vigilant_table = tr.table_for(True)
    plain_table = tr.table_for(False)
    differing_keys = {
        k for k in tr.ALL_STATES
        if vigilant_table.get(k, ()) != plain_table.get(k, ())
    }
    assert differing_keys == set(tr.VIGILANT_DELTA), (
        "the only states allowed to differ between variants are exactly "
        "the ones named in VIGILANT_DELTA")
    for key, edges in tr.VIGILANT_DELTA.items():
        assert vigilant_table[key] == edges


def test_non_vigilant_never_reaches_verify():
    plain_table = tr.table_for(False)
    assert plain_table["verify"] == ()
    for state, edges in plain_table.items():
        assert all(edge.target != "verify" for edge in edges), (
            f"{state} must not target verify in the non-vigilant variant")


def test_vigilant_worker_can_reach_verify():
    vigilant_table = tr.table_for(True)
    assert any(edge.target == "verify" for edge in vigilant_table["worker"])


def test_there_is_exactly_one_function_that_produces_a_variant_table():
    """`table_for` is the only producer of a variant of the STEP-state
    table (`TABLE`/`VIGILANT_DELTA`) -- guards against a second,
    hand-written non-vigilant table creeping in later.

    Task 019 (requirement F) adds a SECOND declared table --
    `TASK_STATUS_TABLE`/`TASK_STATUS_VIGILANT_DELTA`, a different concern
    (one task's `status` field, not the run's own FSM position) -- with
    its own analogous producer, `task_status_table_for`. The invariant this
    test guards is now "exactly one producer PER declared table", not
    "exactly one producer in the whole module": it still fails if either
    table grows a second, duplicate producer.
    """
    tree = ast.parse(MODULE_PATH.read_text())
    producers = [
        node.name for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name.endswith("table_for")
    ]
    assert sorted(producers) == ["table_for", "task_status_table_for"]


# ---------------------------------------------------------------------------
# The specific transitions requirement B names, at minimum
# ---------------------------------------------------------------------------

def test_planning_to_worker():
    assert tr.is_legal("planning", "worker", vigilant=True)
    assert tr.is_legal("planning", "worker", vigilant=False)


def test_worker_to_worker_or_verify_when_vigilant():
    assert tr.is_legal("worker", "worker", vigilant=True)
    assert tr.is_legal("worker", "verify", vigilant=True)


def test_worker_to_worker_or_review_when_non_vigilant():
    assert tr.is_legal("worker", "worker", vigilant=False)
    assert tr.is_legal("worker", "review", vigilant=False)
    assert not tr.is_legal("worker", "verify", vigilant=False)


def test_verify_to_worker_both_directions():
    guards = tr.guard_names("verify", vigilant=True)
    assert "verify_failed_retry_same_task" in guards
    assert "verify_passed_next_task" in guards
    for edge in tr.successors("verify", vigilant=True):
        assert edge.target == "worker"


def test_review_to_worker_replan_finishing_or_review():
    # requirement A, #72: `review_interrupted` self-loops back to `review`,
    # `review_unjudged_at_budget_end` lands on `finishing` (already a
    # target via `rejected_no_approach_left`) -- so `review` joins the
    # target set alongside the four pre-existing targets.
    targets = {edge.target for edge in tr.successors("review", vigilant=True)}
    assert targets == {"worker", "replan", "finishing", "review"}
    targets_plain = {edge.target for edge in tr.successors("review", vigilant=False)}
    assert targets_plain == {"worker", "replan", "finishing", "review"}


def test_review_interrupted_edges_declared_in_both_variants_requirement_a_72():
    """Requirement A (#72): the two new interrupted-review edges must be
    named in `guard_names("review", vigilant)` for BOTH variants, and the
    four pre-existing guard names/targets must be byte-for-byte unchanged.
    """
    pre_existing = {
        "steering_pending_at_verified": "worker",
        "verified": "finishing",
        "rejected_approach_left": "replan",
        "rejected_no_approach_left": "finishing",
    }
    for vigilant in (True, False):
        guards = tr.guard_names("review", vigilant=vigilant)
        assert "review_interrupted" in guards
        assert "review_unjudged_at_budget_end" in guards
        by_guard = {edge.guard: edge.target for edge in tr.successors("review", vigilant=vigilant)}
        for guard, target in pre_existing.items():
            assert guard in by_guard, f"{guard} missing for vigilant={vigilant}"
            assert by_guard[guard] == target, (
                f"{guard} target changed for vigilant={vigilant}")
        # the new edges' own meanings: interrupted stays in review, budget
        # exhaustion at the same interrupted condition ends in finishing.
        assert by_guard["review_interrupted"] == "review"
        assert by_guard["review_unjudged_at_budget_end"] == "finishing"


def test_approach_boundary_is_a_transition_with_a_guard():
    assert "replan" in tr.ALL_STATES
    edges = tr.successors("replan", vigilant=True)
    assert edges, "replan must have declared successors"
    for edge in edges:
        assert edge.guard  # every approach-boundary edge is named/gated
    assert tr.is_legal("replan", "planning", vigilant=True)
    assert tr.is_legal("replan", "finishing", vigilant=True)


def test_finishing_is_non_terminal():
    assert "finishing" not in tr.RUN_TERMINAL_STATES
    assert tr.successors("finishing", vigilant=True) != ()
    assert tr.successors("finishing", vigilant=False) != ()


def test_reflect_unwind_follows_finishing():
    assert tr.is_legal("finishing", "reflect", vigilant=True)
    reflect_targets = {edge.target for edge in tr.successors("reflect", vigilant=True)}
    assert reflect_targets == set(tr.RUN_TERMINAL_STATES)


def test_finishing_can_also_go_straight_to_a_terminal_outcome_without_reflect():
    targets = {edge.target for edge in tr.successors("finishing", vigilant=True)}
    assert targets == {"reflect"} | set(tr.RUN_TERMINAL_STATES)


# ---------------------------------------------------------------------------
# Per-hat entries
# ---------------------------------------------------------------------------

def test_every_hat_has_a_table_entry_and_every_step_state_with_a_hat_is_in_hats():
    for hat in tr.HATS:
        assert hat in tr.STATE_HAT.values() or hat in tr.STATE_HAT
    hat_bearing_states = {s for s, h in tr.STATE_HAT.items() if h is not None}
    assert hat_bearing_states == set(tr.HATS)


def test_deterministic_states_have_no_hat():
    assert tr.STATE_HAT["replan"] is None
    assert tr.STATE_HAT["finishing"] is None
    for terminal in tr.RUN_TERMINAL_STATES:
        assert tr.STATE_HAT[terminal] is None


# ---------------------------------------------------------------------------
# The engine cutover (task 014)
# ---------------------------------------------------------------------------

def test_loop_py_now_imports_the_table():
    """Task 008 was declaration only; task 014 is the cutover -- loop.py now
    drives this table instead of a hand-written imperative flow. See
    tests/test_transition_executor_cutover.py for the behavioural proof
    (illegal transition refused, variants differ only by selection)."""
    loop_src = (REPO_ROOT / "src" / "ralphd" / "engine" / "loop.py").read_text()
    assert ("from . import transitions" in loop_src
            or "from ralphd.engine import transitions" in loop_src
            or "engine.transitions" in loop_src)
