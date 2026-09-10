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
