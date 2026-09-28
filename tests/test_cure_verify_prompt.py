"""Task 003 (PRD requirement A.3): a `review-cure`-origin task
(`state.is_review_cure_origin`) has a `successCriteria` that IS a review
finding's own `cured_when` verbatim -- the broken RULE review named, not a
checklist of the finding's own probe cases. A verifier that only re-checks
the finding's own listed examples and the worker's own new tests would
validate a fix that only covers cases already thought of, never checking
whether the rule itself is closed.

`LoopSupervisor._verify_task_extra` (the factored-out piece of `_verify_task`
that builds the `extra` text folded into `build_prompt("verify", extra=...,
prompt_name="task-verify")`) gains a "Checking the rule, not just the listed
cases" section, conditioned on `state.is_review_cure_origin(task)`, telling
the verifier to try at least one case of the rule that is NOT taken from the
finding or the worker's tests before validating.

Unit-level, the same style `tests/test_cure_worker_prompt.py` already uses
for the worker-side sibling section: a `LoopSupervisor` built directly via
`supervisor_factory`, a task dict built by hand, and
`build_prompt("verify", extra=sup._verify_task_extra(task), prompt_name=
"task-verify")` read for the rendered section -- both directions (a
`review-cure`-origin task carries it; a planner-origin task and a
steering-origin task do not), plus a reverted-text control that pins the
real method's output against a hand-reverted copy the way the worker-side
test's own control does.
"""

from __future__ import annotations

from pathlib import Path

from ralphd.engine import state
from tests.conftest import supervisor_factory

_supervisor = supervisor_factory(run_id="cure-verify-prompt", run_root="run",
                                 workspace="workspace")

SECTION_HEADING = "## Checking the rule, not just the listed cases"


def _cure_task(**over) -> dict:
    task = {
        "id": "cure-01-01",
        "title": "the parser rejects a leading zero on any numeric literal",
        "status": "completed",
        "successCriteria": "any numeric literal token rejects a leading "
                            "zero, not only the two probed in evidence",
        "origin": state.TASK_ORIGIN_REVIEW_CURE,
    }
    task.update(over)
    return task


def _planner_task(**over) -> dict:
    task = {"id": "014", "title": "add the export command",
            "status": "completed",
            "successCriteria": "cli export writes a valid tarball"}
    task.update(over)
    return task


def _steer_task(**over) -> dict:
    task = {"id": "steer-01", "title": "rename the flag per operator note",
            "status": "completed",
            "successCriteria": "the flag is named --dry-run everywhere",
            "origin": state.TASK_ORIGIN_STEER}
    task.update(over)
    return task


def test_review_cure_task_verify_prompt_carries_the_rule_check_step(tmp_path):
    sup = _supervisor(Path(tmp_path))
    task = _cure_task()
    assert state.is_review_cure_origin(task)
    extra = sup._verify_task_extra(task)
    prompt = sup.build_prompt("verify", extra=extra, prompt_name="task-verify")

    assert SECTION_HEADING in prompt
    # Tells the verifier the successCriteria above IS the finding's own
    # `cured_when` -- the broken rule, not a checklist of examples.
    assert "`cured_when`" in prompt
    assert "REFUTED review" in prompt
    # Tells the verifier to try at least one case NOT taken from the
    # finding's own evidence and NOT taken from the worker's own tests.
    assert "at least one case" in prompt
    assert "NOT taken from the finding" in prompt
    assert "NOT taken from any test the worker added" in prompt
    assert "reject" in prompt


def test_planner_origin_task_verify_prompt_omits_the_rule_check_step(tmp_path):
    sup = _supervisor(Path(tmp_path))
    task = _planner_task()
    assert not state.is_review_cure_origin(task)
    extra = sup._verify_task_extra(task)
    prompt = sup.build_prompt("verify", extra=extra, prompt_name="task-verify")

    assert SECTION_HEADING not in prompt
    assert "cured_when" not in prompt


def test_steer_origin_task_verify_prompt_omits_the_rule_check_step(tmp_path):
    sup = _supervisor(Path(tmp_path))
    task = _steer_task()
    assert not state.is_review_cure_origin(task)
    extra = sup._verify_task_extra(task)
    prompt = sup.build_prompt("verify", extra=extra, prompt_name="task-verify")

    assert SECTION_HEADING not in prompt
    assert "cured_when" not in prompt


def test_section_removed_would_fail_the_pinning_assertions(tmp_path):
    """A control that pins the same section against a hand-reverted copy
    of the extra text -- if `_verify_task_extra`'s review-cure branch is
    ever deleted, this assertion (run against the REAL method) fails
    exactly the way the pinning assertions above would."""
    sup = _supervisor(Path(tmp_path))
    task = _cure_task()
    extra = sup._verify_task_extra(task)
    reverted = extra.replace(SECTION_HEADING, "## Task info (reverted)")
    prompt = sup.build_prompt("verify", extra=reverted, prompt_name="task-verify")

    assert SECTION_HEADING not in prompt
    # ...but the real, un-reverted method output still carries it -- this
    # is the assertion that actually catches a real removal of the branch.
    real_prompt = sup.build_prompt(
        "verify", extra=sup._verify_task_extra(task), prompt_name="task-verify")
    assert SECTION_HEADING in real_prompt
