"""Task 015 (#68 half 2, requirement H): where a VERIFY dispatch
provisions -- one rung down from the effective tier of the task it is
checking -- and where the run-ending verification provisions instead,
which is always verify's top tier regardless of any task's tier at all.

Decision 7: per-task verify runs at `max(effective tier - 1, trivial)` on
verify's OWN ladder, computed from the tier the work actually ran at AFTER
escalation (`state.task_effective_tier`), never the planner-annotated
baseline.

Decision 8: the run-level final verification -- the pass whose VERIFIED
verdict ends the run -- always resolves verify's top tier. `state.
run_level_verify_tier()` takes no task argument on purpose: there is no
task's tier, present or absent, that could ever change this answer.
"""

from __future__ import annotations

import pytest

from ralphd.engine import state
from ralphd.engine import tiers as T


# ---------------------------------------------------------------------------
# Decision 7: per-task verify, one step down from the EFFECTIVE tier
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("task,expected_verify_tier", [
    # baseline normal (never escalated) -> verify one step down: trivial
    ({"tier": "normal"}, "trivial"),
    # escalated to hard (whatever the baseline was) -> verify one step
    # down: normal
    ({"tier": "trivial", "effectiveTier": "hard"}, "normal"),
    # baseline/effective trivial -> floored at trivial, never below it
    ({"tier": "trivial"}, "trivial"),
])
def test_task_verify_tier_is_effective_tier_minus_one_floored(
        task, expected_verify_tier):
    assert state.task_verify_tier(task) == expected_verify_tier


def test_task_verify_tier_reads_effective_not_baseline():
    """The baseline alone must never drive this -- only the tier the work
    actually ran at, post-escalation, does."""
    never_escalated = {"tier": "hard"}
    escalated_from_trivial = {"tier": "trivial", "effectiveTier": "hard"}
    assert (state.task_verify_tier(never_escalated) ==
            state.task_verify_tier(escalated_from_trivial) == "normal")


def test_task_verify_tier_never_mutates_the_task_record():
    task = {"tier": "hard", "effectiveTier": "hard"}
    before = dict(task)
    state.task_verify_tier(task)
    assert task == before


@pytest.mark.parametrize("effective,expected", [
    ("hard", "normal"),
    ("normal", "trivial"),
    ("trivial", "trivial"),
])
def test_task_verify_tier_matches_step_down_tier_directly(effective, expected):
    task = {"tier": "normal", "effectiveTier": effective}
    assert state.task_verify_tier(task) == T.step_down_tier(effective) == expected


# ---------------------------------------------------------------------------
# Decision 8: the run-ending verification always resolves the top tier
# ---------------------------------------------------------------------------

def test_run_level_verify_tier_is_always_top_tier():
    assert state.run_level_verify_tier() == T.TOP_TIER == "hard"


def test_run_level_verify_tier_takes_no_task_argument():
    """A pure nullary fact, not a resolver over a task -- the whole point
    of decision 8 is that no task's tier could ever change this answer."""
    import inspect
    sig = inspect.signature(state.run_level_verify_tier)
    assert len(sig.parameters) == 0


@pytest.mark.parametrize("task", [
    {"tier": "trivial", "effectiveTier": "trivial"},
    {"tier": "normal"},
    {"tier": "hard", "effectiveTier": "hard"},
    {},
    None,
])
def test_run_level_verify_tier_is_unaffected_by_any_tasks_tier(task):
    """Belt-and-suspenders on the "regardless of any task's tier" wording:
    even feeding this function's SIBLING (`task_verify_tier`) every one of
    these tasks first, the run-level answer never moves."""
    if task is not None:
        state.task_verify_tier(task)
    assert state.run_level_verify_tier() == "hard"
