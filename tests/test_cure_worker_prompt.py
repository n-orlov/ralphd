"""Task 002 (PRD requirement A.2): a `review-cure`-origin task
(`state.is_review_cure_origin`) is engine-inserted output from a REFUTED
review finding, not an ordinary planner task -- its `successCriteria` IS
the finding's own `cured_when` VERBATIM whenever the finding carried one
(`state.cure_task_from_finding`). A worker dispatched onto that task needs
told, in its own prompt, what "cure" means here: fix the CAUSE
`cured_when` names (not a narrower patch of the finding's own probe
cases), write and pass at least two same-rule cases the finding did not
list beside the finding's own examples, and petition rather than ship a
narrower fix when `cured_when` conflicts with something else.

`LoopSupervisor._worker_assignment_extra` renders this "## Fixing a
review finding" section, conditioned on `state.is_review_cure_origin
(task)`, and it is folded into the text `build_prompt` itself returns via
the `extra` parameter every worker dispatch already passes through
(`build_prompt(phase, extra, ...)` -> `"".join(parts)` with `extra`
appended) -- this module drives `build_prompt("worker", extra=...)`
directly, the same "worker prompt built by build_prompt" surface the
task's own criteria name, rather than reading `_worker_assignment_extra`'s
return value in isolation.

Unit-level, the same style `tests/test_worker_split_refusal_reprompt.py`
and `tests/test_approach_progress_context.py` already use for a block
`_worker_assignment_extra` conditionally appends: a `LoopSupervisor` built
directly via `supervisor_factory`, a task dict built by hand, and
`build_prompt("worker", extra=sup._worker_assignment_extra(task))` read
for the rendered section.
"""

from __future__ import annotations

from pathlib import Path

from ralphd.engine import state
from tests.conftest import supervisor_factory

_supervisor = supervisor_factory(run_id="cure-worker-prompt", run_root="run",
                                 workspace="workspace")

SECTION_HEADING = "## Fixing a review finding"


def _cure_task(**over) -> dict:
    task = {
        "id": "cure-01-01",
        "title": "the parser rejects a leading zero on any numeric literal",
        "status": "pending",
        "successCriteria": "any numeric literal token rejects a leading "
                            "zero, not only the two probed in evidence",
        "origin": state.TASK_ORIGIN_REVIEW_CURE,
    }
    task.update(over)
    return task


def _planner_task(**over) -> dict:
    task = {"id": "014", "title": "add the export command",
            "status": "in-progress",
            "successCriteria": "cli export writes a valid tarball"}
    task.update(over)
    return task


def test_review_cure_task_prompt_carries_the_fixing_section(tmp_path):
    sup = _supervisor(Path(tmp_path))
    task = _cure_task()
    assert state.is_review_cure_origin(task)
    extra = sup._worker_assignment_extra(task)
    prompt = sup.build_prompt("worker", extra=extra)

    assert SECTION_HEADING in prompt
    # Tells the worker to fix the CAUSE `cured_when` names (the task's own
    # `successCriteria` is that text verbatim).
    assert "`cured_when`" in prompt
    assert "Fix the CAUSE" in prompt
    # Tells the worker to add >= two same-rule cases the finding did not
    # list, beside (not instead of) the finding's own examples.
    assert "at least two" in prompt
    assert "the finding did not list" in prompt
    assert "finding's own examples" in prompt
    # Tells the worker to petition rather than ship a narrower fix.
    assert "petition" in prompt
    assert "narrower fix" in prompt


def test_planner_origin_task_prompt_omits_the_fixing_section(tmp_path):
    sup = _supervisor(Path(tmp_path))
    task = _planner_task()
    assert not state.is_review_cure_origin(task)
    extra = sup._worker_assignment_extra(task)
    prompt = sup.build_prompt("worker", extra=extra)

    assert SECTION_HEADING not in prompt
    assert "cured_when" not in prompt


def test_no_task_selectable_prompt_omits_the_fixing_section(tmp_path):
    """`task` is `None` (nothing selectable) -- the section is about a
    task's own origin, so it must not appear when there is no task at
    all."""
    sup = _supervisor(Path(tmp_path))
    extra = sup._worker_assignment_extra(None)
    prompt = sup.build_prompt("worker", extra=extra)

    assert SECTION_HEADING not in prompt


def test_section_removed_would_fail_the_pinning_assertions(tmp_path):
    """A control that pins the same section against a hand-reverted copy
    of the extra text -- if `_worker_assignment_extra`'s review-cure
    branch is ever deleted, this assertion (run against the REAL method)
    fails exactly the way the pinning assertions above would."""
    sup = _supervisor(Path(tmp_path))
    task = _cure_task()
    extra = sup._worker_assignment_extra(task)
    reverted = extra.replace(SECTION_HEADING, "## Task info (reverted)")
    prompt = sup.build_prompt("worker", extra=reverted)

    assert SECTION_HEADING not in prompt
    # ...but the real, un-reverted method output still carries it -- this
    # is the assertion that actually catches a real removal of the branch.
    real_prompt = sup.build_prompt("worker", extra=sup._worker_assignment_extra(task))
    assert SECTION_HEADING in real_prompt
