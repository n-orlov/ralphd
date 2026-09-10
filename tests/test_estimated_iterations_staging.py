"""Task 026 (PRD requirement D.2, #157): a task the planner estimates above
3 iterations is refused at plan ingestion unless THE PROPOSAL decomposes it
into staged TASKS with their own measurable criteria -- an intermediate
numeric target or a named module group each. Folded into `lint_proposal()`
itself (`src/ralphd/engine/plan_ingestion.py`), reusing that function's
existing per-task loop rather than adding a fourth separately-run lint,
exactly the same discipline tasks 012/013/009 already followed for their own
additions to the same function.

`plan_review.md`'s peer checklist item 8 asks the peer hat to catch the
same shape when the peer gate is ON; this module only proves the ingestion
half (the one enforcement point when the gate is OFF), which is this
task's own scope.

Three nodes are the pinned regression the commit message quotes:
`test_an_estimate_of_four_with_no_staging_is_refused_naming_the_task`
(refusal at 4), `test_an_estimate_of_three_passes_unstaged` (acceptance at
3, the boundary), and
`test_an_estimate_of_four_with_a_valid_staged_decomposition_is_accepted`
(acceptance at 4-with-stages -- an oversized parent beside two measurable,
dependency-ordered stage tasks).

Third attempt. Attempt 2 read `Stage N` headings inside the oversized
task's OWN `successCriteria` as the staging, which inverted the
requirement: a proposal carrying an estimated-4 parent plus two measurable
dependency-ordered stage tasks was REFUSED, while one estimated-4
monolithic task that had merely written two Stage headings into its flat
criterion was ACCEPTED. The verifier rejected exactly that, and the
detector is now a cross-task read: a stage task is a SEPARATE task in the
same proposal carrying a `Stage N` marker in its title or criteria and
bound to the oversized task by `dependsOn` (either direction) or by naming
its id. `test_stage_headings_inside_the_oversized_tasks_own_criteria_are_
not_a_decomposition` and
`test_a_parent_with_two_dependency_ordered_stage_tasks_is_accepted` pin the
two halves of that inversion.

Attempt 2's surviving work is the measurability of each stage's own
criteria, kept node for node at the stage-task level: labels must be the
sequence 1..N in proposal order, a numeric target must bind its number to a
unit or a comparator (identifier noise -- issue numbers, task ids, versions
-- is stripped before the scan), a module group must be a path or a
backticked identifier (never the bare word "module"), and no two stage
tasks may repeat the same criteria text.
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


def _stage_task(task_id: str, number: int, criteria: str, **overrides) -> dict:
    """A stage task of the oversized parent `001`: it carries its own
    `Stage N` marker and depends on the parent, the shape `planning.md`
    documents."""
    task = {
        "id": task_id,
        "title": f"Stage {number}: part {number} of the descent",
        "successCriteria": criteria,
        "dependsOn": ["001"],
    }
    task.update(overrides)
    return task


# The reference accepted decomposition: an oversized parent (`001`) beside
# three measurable, dependency-ordered stage tasks.
def _staged_proposal(estimate: int = 4) -> dict:
    return {
        "tasks": [
            _basic_task(
                "001",
                estimatedIterations=estimate,
                successCriteria="the whole descent has landed and stays green",
            ),
            _stage_task("001a", 1, "`tests/test_a.py` passes in full."),
            _stage_task(
                "001b",
                2,
                "the `some_module.py` module reads the new config key and at "
                "least 12 nodes in `tests/test_b.py` pass.",
                dependsOn=["001", "001a"],
            ),
            _stage_task(
                "001c",
                3,
                "docs/architecture.md documents the change, and at least 1 "
                "new example is added.",
                dependsOn=["001", "001b"],
            ),
        ]
    }


def _gap(*stage_criteria: str, **parent_overrides) -> str | None:
    """`_staged_decomposition_gap` over a proposal whose oversized parent
    `001` is followed by one stage task per argument, numbered 1..N."""
    parent = _basic_task("001", estimatedIterations=4, **parent_overrides)
    tasks = [parent] + [
        _stage_task(f"001{chr(ord('a') + i)}", i + 1, criteria)
        for i, criteria in enumerate(stage_criteria)
    ]
    return pi._staged_decomposition_gap(parent, tasks)


# ---------------------------------------------------------------------------
# _staged_decomposition_gap -- the proposal-level detector
# ---------------------------------------------------------------------------

def test_fewer_than_two_stage_markers_is_a_gap():
    gap = _gap("`tests/test_a.py` passes.")
    assert gap is not None
    assert "fewer than two stage tasks" in gap


def test_no_stage_markers_at_all_is_a_gap():
    parent = _basic_task("001", estimatedIterations=4)
    other = _basic_task("002", successCriteria="a flat, unstaged demand")
    gap = pi._staged_decomposition_gap(parent, [parent, other])
    assert gap is not None
    assert "fewer than two stage tasks" in gap


def test_stage_headings_inside_the_oversized_tasks_own_criteria_are_not_a_decomposition():
    """The inversion attempt 2 shipped: headings inside ONE task leave the
    work in one task of the same size, so they decompose nothing."""
    parent = _basic_task(
        "001",
        estimatedIterations=4,
        successCriteria=(
            "Stage 1: `tests/test_a.py` passes. "
            "Stage 2: at least 12 nodes in `tests/test_b.py` pass."
        ),
    )
    gap = pi._staged_decomposition_gap(parent, [parent])
    assert gap is not None
    assert "fewer than two stage tasks" in gap


def test_a_parent_with_two_dependency_ordered_stage_tasks_is_accepted():
    """The other half of the inversion: the shape the PRD asks for, which
    attempt 2 refused."""
    parent = _basic_task("001", estimatedIterations=4)
    stage_one = _stage_task("001a", 1, "at least 5 nodes in `tests/test_a.py` pass.")
    stage_two = _stage_task(
        "001b",
        2,
        "at least 10 nodes in `tests/test_b.py` pass.",
        dependsOn=["001a"],
    )
    assert (
        pi._staged_decomposition_gap(parent, [parent, stage_one, stage_two]) is None
    )


def test_stage_tasks_bound_only_by_the_parents_own_dependsOn_are_accepted():
    """Either dependency direction binds a stage to its parent."""
    parent = _basic_task("001", estimatedIterations=4, dependsOn=["001a", "001b"])
    stage_one = _stage_task("001a", 1, "at least 5 tests pass.", dependsOn=[])
    stage_two = _stage_task("001b", 2, "at least 10 tests pass.", dependsOn=[])
    assert (
        pi._staged_decomposition_gap(parent, [parent, stage_one, stage_two]) is None
    )


def test_stage_tasks_bound_only_by_naming_the_parents_id_are_accepted():
    parent = _basic_task("001", estimatedIterations=4)
    stage_one = {
        "id": "007",
        "title": "Stage 1 of task 001",
        "successCriteria": "at least 5 tests pass.",
    }
    stage_two = {
        "id": "008",
        "title": "Stage 2 of task 001",
        "successCriteria": "at least 10 tests pass.",
    }
    assert (
        pi._staged_decomposition_gap(parent, [parent, stage_one, stage_two]) is None
    )


def test_an_unbound_task_carrying_a_stage_marker_is_not_a_stage_of_this_task():
    """A `Stage N` task belonging to some other parent does not stage this
    one."""
    parent = _basic_task("001", estimatedIterations=4)
    stray_one = {
        "id": "009",
        "title": "Stage 1: something in another descent",
        "successCriteria": "at least 5 tests pass.",
        "dependsOn": ["002"],
    }
    stray_two = {
        "id": "010",
        "title": "Stage 2: something else in another descent",
        "successCriteria": "at least 10 tests pass.",
        "dependsOn": ["002"],
    }
    gap = pi._staged_decomposition_gap(parent, [parent, stray_one, stray_two])
    assert gap is not None
    assert "fewer than two stage tasks" in gap


def test_a_stage_with_neither_a_numeric_target_nor_a_module_group_is_a_gap():
    gap = _gap("the code compiles cleanly.", "the feature works well.")
    assert gap is not None
    assert "stage task" in gap.lower()


def test_two_stages_each_naming_a_numeric_target_has_no_gap():
    assert _gap("at least 5 tests pass.", "at least 10 tests pass.") is None


def test_two_stages_each_naming_a_module_group_has_no_gap():
    assert _gap("`tests/test_a.py` passes.", "`tests/test_b.py` passes.") is None


def test_the_full_staged_fixture_has_no_gap():
    payload = _staged_proposal()
    assert (
        pi._staged_decomposition_gap(payload["tasks"][0], payload["tasks"]) is None
    )


def test_spelled_out_stage_numbers_do_not_count_as_markers():
    """Only the digit form counts (detector docstring) -- "Stage one" is not
    a stage marker."""
    parent = _basic_task("001", estimatedIterations=4)
    stage_one = {
        "id": "001a",
        "title": "Stage one: the first part",
        "successCriteria": "at least 5 tests pass.",
        "dependsOn": ["001"],
    }
    stage_two = {
        "id": "001b",
        "title": "Stage two: the second part",
        "successCriteria": "at least 10 tests pass.",
        "dependsOn": ["001"],
    }
    gap = pi._staged_decomposition_gap(parent, [parent, stage_one, stage_two])
    assert gap is not None


def test_a_stage_marker_in_a_stage_tasks_criteria_also_counts():
    """The marker may sit in either field, so a plain title with a
    `Stage N:` criterion still stages."""
    parent = _basic_task("001", estimatedIterations=4)
    stage_one = {
        "id": "001a",
        "title": "first slice of task 001",
        "successCriteria": "Stage 1: at least 5 tests pass.",
        "dependsOn": ["001"],
    }
    stage_two = {
        "id": "001b",
        "title": "second slice of task 001",
        "successCriteria": "Stage 2: at least 10 tests pass.",
        "dependsOn": ["001"],
    }
    assert (
        pi._staged_decomposition_gap(parent, [parent, stage_one, stage_two]) is None
    )


# ---------------------------------------------------------------------------
# The three false acceptances attempt 1 of this task shipped, kept at the
# stage-task level and (further down) through `lint_proposal` itself
# ---------------------------------------------------------------------------

def test_a_stage_whose_only_number_is_an_issue_number_is_a_gap():
    """Counterexample 1: an issue number is an identifier, not an
    intermediate target."""
    gap = _gap("investigate issue 42.", "investigate issue 43.")
    assert gap is not None
    assert "stage 1" in gap.lower()


def test_hash_and_gh_issue_references_are_not_numeric_targets():
    gap = _gap("land the fix for #182.", "land the fix for GH-183.")
    assert gap is not None


def test_a_task_id_reference_is_not_a_numeric_target():
    gap = _gap("redo what task 026 did.", "redo what task 027 did.")
    assert gap is not None


def test_a_version_string_is_not_a_numeric_target():
    gap = _gap("support v0.20 fully.", "support v0.21 fully.")
    assert gap is not None


def test_the_bare_word_module_is_not_a_named_module_group():
    """Counterexample 2: "the module works" names nothing a worker could
    open."""
    gap = _gap("the module works.", "the modules work.")
    assert gap is not None
    assert "module" in gap.lower()


def test_duplicate_stage_labels_are_a_gap():
    """Counterexample 3: two `Stage 1`s are restatements, not a descent --
    even when each stage task carries a perfectly good numeric target."""
    parent = _basic_task("001", estimatedIterations=4)
    stage_one = _stage_task("001a", 1, "at least 5 tests pass.")
    stage_two = _stage_task("001b", 1, "at least 9 tests pass.")
    gap = pi._staged_decomposition_gap(parent, [parent, stage_one, stage_two])
    assert gap is not None
    assert "[1, 1]" in gap


def test_stage_labels_must_start_at_one():
    parent = _basic_task("001", estimatedIterations=4)
    stage_one = _stage_task("001a", 2, "at least 5 tests pass.")
    stage_two = _stage_task("001b", 3, "at least 9 tests pass.")
    gap = pi._staged_decomposition_gap(parent, [parent, stage_one, stage_two])
    assert gap is not None
    assert "[2, 3]" in gap


def test_out_of_order_stage_labels_are_a_gap():
    parent = _basic_task("001", estimatedIterations=4)
    stage_two = _stage_task("001b", 2, "at least 9 tests pass.")
    stage_one = _stage_task("001a", 1, "at least 5 tests pass.")
    gap = pi._staged_decomposition_gap(parent, [parent, stage_two, stage_one])
    assert gap is not None
    assert "[2, 1]" in gap


def test_a_skipped_stage_number_is_a_gap():
    parent = _basic_task("001", estimatedIterations=4)
    stage_one = _stage_task("001a", 1, "at least 5 tests pass.")
    stage_three = _stage_task("001c", 3, "at least 9 tests pass.")
    gap = pi._staged_decomposition_gap(parent, [parent, stage_one, stage_three])
    assert gap is not None
    assert "[1, 3]" in gap


def test_two_stage_tasks_repeating_the_same_criteria_verbatim_is_a_gap():
    gap = _gap("`tests/test_a.py` passes.", "`tests/test_a.py`   passes.")
    assert gap is not None
    assert "same successCriteria" in gap


def test_a_number_bound_to_a_unit_is_a_numeric_target():
    assert _gap("5 nodes pass.", "9 nodes pass.") is None


def test_a_comparator_bound_number_is_a_numeric_target():
    assert _gap(">= 5 land.", "at most 9 remain.") is None


def test_a_bare_directory_group_counts_as_a_module_group():
    assert _gap("tests/records/ goes green.", "docs/architecture.md documents it.") is None


def test_a_slashed_conjunction_is_not_a_module_group():
    """`and/or` has a slash but names no module."""
    gap = _gap("fix and/or refactor the thing.", "fix and/or refactor the other thing.")
    assert gap is not None


# ---------------------------------------------------------------------------
# lint_proposal -- the three pinned nodes (refusal at 4, acceptance at 3,
# acceptance at 4-with-stages)
# ---------------------------------------------------------------------------

def test_an_estimate_of_four_with_no_staging_is_refused_naming_the_task():
    payload = {
        "tasks": [
            _basic_task(
                "001",
                estimatedIterations=4,
                successCriteria="one giant undifferentiated deliverable",
            )
        ]
    }
    errors = pi.lint_proposal(payload)
    assert any(
        "001" in e and "estimatedIterations" in e and "4" in e for e in errors
    )


def test_an_estimate_of_three_passes_unstaged():
    payload = {
        "tasks": [
            _basic_task(
                "001",
                estimatedIterations=3,
                successCriteria="one giant undifferentiated deliverable",
            )
        ]
    }
    assert pi.lint_proposal(payload) == []


def test_an_estimate_of_four_with_a_valid_staged_decomposition_is_accepted():
    assert pi.lint_proposal(_staged_proposal()) == []


def test_an_estimate_of_five_with_no_staging_is_also_refused():
    payload = {
        "tasks": [
            _basic_task(
                "001",
                estimatedIterations=5,
                successCriteria="one giant undifferentiated deliverable",
            )
        ]
    }
    errors = pi.lint_proposal(payload)
    assert any("001" in e and "5" in e for e in errors)


def test_an_estimate_of_four_with_only_one_stage_task_is_still_refused():
    payload = {
        "tasks": [
            _basic_task("001", estimatedIterations=4),
            _stage_task("001a", 1, "`tests/test_a.py` passes in full."),
        ]
    }
    errors = pi.lint_proposal(payload)
    assert any("001" in e and "estimatedIterations" in e for e in errors)


def test_lint_refuses_four_whose_stages_live_inside_its_own_criteria():
    """The monolith attempt 2 accepted: two Stage headings, one task."""
    payload = {
        "tasks": [
            _basic_task(
                "001",
                estimatedIterations=4,
                successCriteria=(
                    "Stage 1: `tests/test_a.py` passes. "
                    "Stage 2: at least 12 nodes in `tests/test_b.py` pass."
                ),
            )
        ]
    }
    errors = pi.lint_proposal(payload)
    assert any("001" in e and "estimatedIterations" in e for e in errors)


def _staged_errors(*stage_criteria: str) -> list[str]:
    payload = {
        "tasks": [_basic_task("001", estimatedIterations=4)]
        + [
            _stage_task(f"001{chr(ord('a') + i)}", i + 1, criteria)
            for i, criteria in enumerate(stage_criteria)
        ]
    }
    return pi.lint_proposal(payload)


def test_lint_refuses_four_staged_only_by_issue_numbers_naming_the_task():
    errors = _staged_errors("investigate issue 42.", "investigate issue 43.")
    assert any("001" in e and "estimatedIterations" in e for e in errors)


def test_lint_refuses_four_staged_only_by_the_bare_word_module():
    errors = _staged_errors("the module works.", "the modules work.")
    assert any("001" in e and "estimatedIterations" in e for e in errors)


def test_lint_refuses_four_with_duplicate_stage_labels():
    payload = {
        "tasks": [
            _basic_task("001", estimatedIterations=4),
            _stage_task("001a", 1, "at least 5 tests pass."),
            _stage_task("001b", 1, "at least 9 tests pass."),
        ]
    }
    errors = pi.lint_proposal(payload)
    assert any("001" in e and "estimatedIterations" in e for e in errors)


def test_lint_refuses_four_whose_two_stage_tasks_repeat_one_criterion():
    errors = _staged_errors("`tests/test_a.py` passes.", "`tests/test_a.py` passes.")
    assert any("001" in e and "estimatedIterations" in e for e in errors)


def test_the_refusal_message_says_what_a_target_and_a_group_must_be():
    payload = {
        "tasks": [
            _basic_task(
                "001",
                estimatedIterations=4,
                successCriteria="one giant undifferentiated deliverable",
            )
        ]
    }
    joined = " ".join(pi.lint_proposal(payload))
    assert "bound to a" in joined and "bare word 'module'" in joined


def test_the_refusal_message_says_a_stage_task_is_a_separate_task():
    payload = {
        "tasks": [
            _basic_task(
                "001",
                estimatedIterations=4,
                successCriteria="one giant undifferentiated deliverable",
            )
        ]
    }
    joined = " ".join(pi.lint_proposal(payload))
    assert "SEPARATE task" in joined and "dependsOn" in joined


def test_a_missing_estimate_never_trips_this_lint():
    payload = {"tasks": [_basic_task("001", successCriteria="a flat unstaged demand")]}
    assert pi.lint_proposal(payload) == []


def test_full_ingestion_loop_refuses_the_oversized_unstaged_task(tmp_path):
    import json

    (tmp_path / "plan-proposal.json").write_text(
        json.dumps(
            {
                "tasks": [
                    _basic_task(
                        "001",
                        estimatedIterations=4,
                        successCriteria="one giant undifferentiated deliverable",
                    )
                ]
            }
        )
    )

    def respond(attempt, prior_errors):
        return json.dumps({"proposal_path": "plan-proposal.json", "summary": "a plan"})

    result = pi.enforce_plan_ingestion(
        respond, max_retries=1, read_proposal=pi.file_reader(tmp_path)
    )
    assert not result.ok
    assert any("estimatedIterations" in e and "001" in e for e in result.errors)


def test_full_ingestion_loop_accepts_the_staged_form(tmp_path):
    import json

    (tmp_path / "plan-proposal.json").write_text(json.dumps(_staged_proposal()))

    def respond(attempt, prior_errors):
        return json.dumps({"proposal_path": "plan-proposal.json", "summary": "a plan"})

    result = pi.enforce_plan_ingestion(
        respond, max_retries=1, read_proposal=pi.file_reader(tmp_path)
    )
    assert result.ok
    assert result.tasks_doc["tasks"][0]["estimatedIterations"] == 4
