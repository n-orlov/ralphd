"""Task 021 (PRD requirement K.1, #169): "a probe-audit task per requirement
group, in the plan." `plan_ingestion.lint_proposal()` refuses a proposal that
names a requirement group -- a task titled in this plan's own
`<Letter>[.N]: <description>` convention -- with no task in that group whose
own `successCriteria` states both that a probe exists which fails if the
behaviour is absent, and that the probe is named in the commit message.

This front-loads, at plan time, exactly the review-time incident K's own PRD
prose names: 37 cheap verify iterations and one 35-minute review that
overturned eight of the tasks they had passed, twice on the same mechanical
failure -- a shipped, green test node asserting the OPPOSITE of its own
criterion.

Two nodes are the pinned regression the commit message quotes:
`test_a_proposal_whose_requirement_group_carries_no_probe_audit_task_is_flagged_naming_the_group`
(the flagged shape) and
`test_a_proposal_whose_requirement_group_carries_a_probe_audit_task_is_clean`
(the same group, with the probe-audit task added, accepted).
"""

from __future__ import annotations

from ralphd.engine import plan_ingestion as pi


def _basic_task(task_id: str, **overrides) -> dict:
    task = {
        "id": task_id,
        "title": f"do thing {task_id}",
        "successCriteria": f"thing {task_id} is verifiably done",
    }
    task.update(overrides)
    return task


def _proposal(tasks: list) -> dict:
    return {
        "version": 1,
        "goal": "a goal",
        "scope": {"level": "single-repo", "reasoning": "r"},
        "repositories": [],
        "tasks": tasks,
        "discovered": {},
    }


def test_a_proposal_whose_requirement_group_carries_no_probe_audit_task_is_flagged_naming_the_group():
    payload = _proposal([
        _basic_task(
            "001",
            title="A.1: implement the widget",
            successCriteria="The widget exists and unit tests cover it.",
        ),
        _basic_task(
            "002",
            title="A.2: wire the widget into the CLI",
            successCriteria="The CLI invokes the widget and a smoke test passes.",
        ),
    ])
    errors = pi.lint_proposal(payload)
    matches = [e for e in errors if "requirement group" in e and "'A'" in e]
    assert matches, errors
    assert "probe exists" in matches[0]
    assert "named in the commit message" in matches[0]


def test_a_proposal_whose_requirement_group_carries_a_probe_audit_task_is_clean():
    payload = _proposal([
        _basic_task(
            "001",
            title="A.1: implement the widget",
            successCriteria="The widget exists and unit tests cover it.",
        ),
        _basic_task(
            "002",
            title="A.2: wire the widget into the CLI",
            successCriteria="The CLI invokes the widget and a smoke test passes.",
        ),
        _basic_task(
            "003",
            title="A.3: probe-audit the widget group",
            successCriteria=(
                "A tracked probe exists that fails if the behaviour is "
                "absent, and the probe is named in the commit message."
            ),
        ),
    ])
    errors = pi.lint_proposal(payload)
    assert not [e for e in errors if "requirement group" in e]


def test_a_probe_audit_task_missing_the_named_in_commit_message_clause_does_not_satisfy_the_group():
    payload = _proposal([
        _basic_task(
            "001",
            title="B.1: implement the gadget",
            successCriteria="The gadget exists and unit tests cover it.",
        ),
        _basic_task(
            "002",
            title="B.2: an audit-flavoured task with only half the claim",
            successCriteria="A probe exists that fails if the behaviour is absent.",
        ),
    ])
    errors = pi.lint_proposal(payload)
    matches = [e for e in errors if "requirement group" in e and "'B'" in e]
    assert matches, errors


def test_a_task_outside_the_lettered_groups_names_no_group_and_is_exempt():
    payload = _proposal([
        _basic_task(
            "001",
            title="Lanes: gate, heavy and nightly green at the merged tip",
            successCriteria="Every lane is green, confirmed fresh from the API.",
        ),
    ])
    errors = pi.lint_proposal(payload)
    assert not [e for e in errors if "requirement group" in e]


def test_requirement_group_helper_reads_the_plans_own_title_convention():
    assert pi._requirement_group({"title": "K.1: require a probe-audit task"}) == "K"
    assert pi._requirement_group({"title": "H: re-aim the reserve guidance"}) == "H"
    assert pi._requirement_group({"title": "Lanes: gate green"}) is None


def test_is_probe_audit_task_requires_both_clauses():
    both = {
        "successCriteria": (
            "A probe exists which fails if the behaviour is absent, and "
            "the probe is named in the commit message."
        ),
    }
    only_absence = {
        "successCriteria": "A probe exists which fails if the behaviour is absent."
    }
    only_naming = {
        "successCriteria": "The probe is named in the commit message."
    }
    assert pi._is_probe_audit_task(both) is True
    assert pi._is_probe_audit_task(only_absence) is False
    assert pi._is_probe_audit_task(only_naming) is False
