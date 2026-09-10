"""Task 026 (PRD requirement D.2, #157): a task the planner estimates above
3 iterations is refused at plan ingestion unless its own `successCriteria`
already decomposes the work into a staged sequence -- at least two
`Stage N: ...` clauses, each naming an intermediate numeric target or a
named module group. Folded into `lint_proposal()` itself
(`src/ralphd/engine/plan_ingestion.py`), reusing that function's existing
per-task loop rather than adding a fourth separately-run lint, exactly the
same discipline tasks 012/013/009 already followed for their own additions
to the same function.

`plan_review.md`'s peer checklist item 8 asks the peer hat to catch the
same shape when the peer gate is ON; this module only proves the ingestion
half (the one enforcement point when the gate is OFF), which is this
task's own scope.

Three nodes are the pinned regression the commit message quotes:
`test_an_estimate_of_four_with_no_staging_is_refused_naming_the_task`
(refusal at 4), `test_an_estimate_of_three_passes_unstaged` (acceptance at
3, the boundary), and
`test_an_estimate_of_four_with_a_valid_staged_decomposition_is_accepted`
(acceptance at 4-with-stages).

Second attempt (the first was rejected in validation): "measurable" is now
enforced rather than assumed. The verifier's own three counterexamples --
`Stage 1: investigate issue 42. Stage 2: investigate issue 43.` (bare
integers read as numeric targets), two generic `module works` clauses (the
bare noun read as a named module group) and duplicate `Stage 1` labels (no
sequence check at all) -- were all accepted by attempt 1 and each now has
its own node here, at both the detector level and through
`lint_proposal()`. The tightened rules are: labels must be the sequence
1..N, a numeric target must bind its number to a unit or a comparator
(identifier noise -- issue numbers, task ids, versions -- is stripped
before the scan), a module group must be a path or a backticked
identifier, and no two stages may repeat the same clause text.
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


STAGED_CRITERIA = (
    "Stage 1: `tests/test_a.py` passes in full. "
    "Stage 2: the `some_module.py` module reads the new config key and at "
    "least 12 nodes in `tests/test_b.py` pass. "
    "Stage 3: docs/architecture.md documents the change, and at least 1 "
    "new example is added."
)


# ---------------------------------------------------------------------------
# _staged_decomposition_gap -- the text-level detector
# ---------------------------------------------------------------------------

def test_fewer_than_two_stage_markers_is_a_gap():
    gap = pi._staged_decomposition_gap("Stage 1: tests/test_a.py passes.")
    assert gap is not None
    assert "fewer than two" in gap


def test_no_stage_markers_at_all_is_a_gap():
    gap = pi._staged_decomposition_gap("a flat, unstaged demand with no stages named")
    assert gap is not None


def test_a_stage_with_neither_a_numeric_target_nor_a_module_group_is_a_gap():
    gap = pi._staged_decomposition_gap(
        "Stage 1: the code compiles cleanly. Stage 2: the feature works well."
    )
    assert gap is not None
    assert "stage" in gap.lower()


def test_two_stages_each_naming_a_numeric_target_has_no_gap():
    gap = pi._staged_decomposition_gap(
        "Stage 1: at least 5 tests pass. Stage 2: at least 10 tests pass."
    )
    assert gap is None


def test_two_stages_each_naming_a_module_group_has_no_gap():
    gap = pi._staged_decomposition_gap(
        "Stage 1: `tests/test_a.py` passes. Stage 2: `tests/test_b.py` passes."
    )
    assert gap is None


def test_the_full_staged_fixture_has_no_gap():
    assert pi._staged_decomposition_gap(STAGED_CRITERIA) is None


def test_spelled_out_stage_numbers_do_not_count_as_markers():
    """Only the digit form counts (module docstring) -- "Stage one" is not
    a stage marker this detector recognises."""
    gap = pi._staged_decomposition_gap(
        "Stage one: 5 tests pass. Stage two: 10 tests pass."
    )
    assert gap is not None


# ---------------------------------------------------------------------------
# The three false acceptances attempt 1 of this task shipped, each pinned
# at the detector level and (further down) through `lint_proposal` itself
# ---------------------------------------------------------------------------

def test_a_stage_whose_only_number_is_an_issue_number_is_a_gap():
    """Counterexample 1: an issue number is an identifier, not an
    intermediate target."""
    gap = pi._staged_decomposition_gap(
        "Stage 1: investigate issue 42. Stage 2: investigate issue 43."
    )
    assert gap is not None
    assert "stage 1" in gap.lower()


def test_hash_and_gh_issue_references_are_not_numeric_targets():
    gap = pi._staged_decomposition_gap(
        "Stage 1: land the fix for #182. Stage 2: land the fix for GH-183."
    )
    assert gap is not None


def test_a_task_id_reference_is_not_a_numeric_target():
    gap = pi._staged_decomposition_gap(
        "Stage 1: redo what task 026 did. Stage 2: redo what task 027 did."
    )
    assert gap is not None


def test_a_version_string_is_not_a_numeric_target():
    gap = pi._staged_decomposition_gap(
        "Stage 1: support v0.20 fully. Stage 2: support v0.21 fully."
    )
    assert gap is not None


def test_the_bare_word_module_is_not_a_named_module_group():
    """Counterexample 2: "the module works" names nothing a worker could
    open."""
    gap = pi._staged_decomposition_gap(
        "Stage 1: the module works. Stage 2: the modules work."
    )
    assert gap is not None
    assert "module" in gap.lower()


def test_duplicate_stage_labels_are_a_gap():
    """Counterexample 3: two `Stage 1`s are restatements, not a descent --
    even when each clause carries a perfectly good numeric target."""
    gap = pi._staged_decomposition_gap(
        "Stage 1: at least 5 tests pass. Stage 1: at least 9 tests pass."
    )
    assert gap is not None
    assert "[1, 1]" in gap


def test_stage_labels_must_start_at_one():
    gap = pi._staged_decomposition_gap(
        "Stage 2: at least 5 tests pass. Stage 3: at least 9 tests pass."
    )
    assert gap is not None
    assert "[2, 3]" in gap


def test_out_of_order_stage_labels_are_a_gap():
    gap = pi._staged_decomposition_gap(
        "Stage 2: at least 9 tests pass. Stage 1: at least 5 tests pass."
    )
    assert gap is not None


def test_a_skipped_stage_number_is_a_gap():
    gap = pi._staged_decomposition_gap(
        "Stage 1: at least 5 tests pass. Stage 3: at least 9 tests pass."
    )
    assert gap is not None


def test_two_stages_repeating_the_same_clause_verbatim_is_a_gap():
    gap = pi._staged_decomposition_gap(
        "Stage 1: `tests/test_a.py` passes. Stage 2: `tests/test_a.py` passes."
    )
    assert gap is not None
    assert "same clause" in gap


def test_a_number_bound_to_a_unit_is_a_numeric_target():
    assert pi._staged_decomposition_gap(
        "Stage 1: 5 nodes pass. Stage 2: 9 nodes pass."
    ) is None


def test_a_comparator_bound_number_is_a_numeric_target():
    assert pi._staged_decomposition_gap(
        "Stage 1: >= 5 land. Stage 2: at most 9 remain."
    ) is None


def test_a_bare_directory_group_counts_as_a_module_group():
    assert pi._staged_decomposition_gap(
        "Stage 1: tests/records/ goes green. "
        "Stage 2: docs/architecture.md documents it."
    ) is None


def test_a_slashed_conjunction_is_not_a_module_group():
    """`and/or` has a slash but names no module."""
    gap = pi._staged_decomposition_gap(
        "Stage 1: fix and/or refactor the thing. "
        "Stage 2: fix and/or refactor the other thing."
    )
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
    payload = {
        "tasks": [
            _basic_task(
                "001",
                estimatedIterations=4,
                successCriteria=STAGED_CRITERIA,
            )
        ]
    }
    assert pi.lint_proposal(payload) == []


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


def test_an_estimate_of_four_with_only_one_stage_marker_is_still_refused():
    payload = {
        "tasks": [
            _basic_task(
                "001",
                estimatedIterations=4,
                successCriteria="Stage 1: `tests/test_a.py` passes. Then the rest lands.",
            )
        ]
    }
    errors = pi.lint_proposal(payload)
    assert any("001" in e for e in errors)


def _oversized_errors(criteria: str) -> list[str]:
    payload = {
        "tasks": [
            _basic_task("001", estimatedIterations=4, successCriteria=criteria)
        ]
    }
    return pi.lint_proposal(payload)


def test_lint_refuses_four_staged_only_by_issue_numbers_naming_the_task():
    errors = _oversized_errors(
        "Stage 1: investigate issue 42. Stage 2: investigate issue 43."
    )
    assert any("001" in e and "estimatedIterations" in e for e in errors)


def test_lint_refuses_four_staged_only_by_the_bare_word_module():
    errors = _oversized_errors(
        "Stage 1: the module works. Stage 2: the modules work."
    )
    assert any("001" in e and "estimatedIterations" in e for e in errors)


def test_lint_refuses_four_with_duplicate_stage_labels():
    errors = _oversized_errors(
        "Stage 1: at least 5 tests pass. Stage 1: at least 9 tests pass."
    )
    assert any("001" in e and "estimatedIterations" in e for e in errors)


def test_lint_refuses_four_whose_two_stages_repeat_one_clause():
    errors = _oversized_errors(
        "Stage 1: `tests/test_a.py` passes. Stage 2: `tests/test_a.py` passes."
    )
    assert any("001" in e and "estimatedIterations" in e for e in errors)


def test_the_refusal_message_says_what_a_target_and_a_group_must_be():
    errors = _oversized_errors("one giant undifferentiated deliverable")
    joined = " ".join(errors)
    assert "bound to a" in joined and "bare word 'module'" in joined


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

    (tmp_path / "plan-proposal.json").write_text(
        json.dumps(
            {
                "tasks": [
                    _basic_task(
                        "001",
                        estimatedIterations=4,
                        successCriteria=STAGED_CRITERIA,
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
    assert result.ok
    assert result.tasks_doc["tasks"][0]["estimatedIterations"] == 4
