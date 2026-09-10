"""Task 025 (PRD requirement D.1, #157): the planner sizes tasks -- the
ingestion half. `plan-proposal.json` tasks may carry an `estimatedIterations`
(integer >= 1); `plan_ingestion` accepts it, defaults a missing one to 1 and
records that it defaulted (a WARNING, the exact channel/shape
`normalize_task_tiers` already uses for `tier`, task 013), and refuses a
non-integer or < 1 value at shape-validation time, naming the offending
task, exactly like an unresolvable `dependsOn`.

`src/ralphd/prompts/planning.md` documents the field in plain prose beside
`dependsOn`/`priority` -- never inside the module's one pre-existing
schema-example code fence, mirroring the convention
`tests/test_worker_progress_claim.py` and `tests/test_deferred_tasks_surface.py`
already pin for their own new contract fields ("no contract object inside a
code fence").

Requirements D.2-D.5 (the >3 refusal, the 2x-baseline smell, the budget
derivation, `ralphctl tasks`'s `est` column) are tasks 026-028's own scope,
not this one's -- this module only proves acceptance, the default, the
recorded defaulting and the refusal.
"""

from __future__ import annotations

import json
from pathlib import Path

from ralphd.engine import plan_ingestion as pi

PLANNING_MD = (
    Path(__file__).resolve().parents[1]
    / "src" / "ralphd" / "prompts" / "planning.md"
).read_text()


def _basic_task(task_id: str, **overrides) -> dict:
    task = {
        "id": task_id,
        "title": f"do thing {task_id}",
        "successCriteria": f"thing {task_id} is verifiably done",
    }
    task.update(overrides)
    return task


def _pointer(path: str, summary: str = "a plan") -> str:
    return json.dumps({"proposal_path": path, "summary": summary})


def _write(tmp_path, name: str, doc: dict) -> None:
    (tmp_path / name).write_text(json.dumps(doc))


# ---------------------------------------------------------------------------
# validate_proposal_shape -- acceptance and refusal of a present value
# ---------------------------------------------------------------------------

def test_a_present_integer_of_at_least_one_has_no_shape_errors():
    payload = {"tasks": [_basic_task("001", estimatedIterations=1)]}
    assert pi.validate_proposal_shape(payload) == []
    payload = {"tasks": [_basic_task("001", estimatedIterations=3)]}
    assert pi.validate_proposal_shape(payload) == []


def test_a_missing_estimatedIterations_has_no_shape_errors():
    """Absence is fine at shape time -- defaulting is
    `normalize_task_estimated_iterations`'s job (step 3), not a shape
    concern."""
    payload = {"tasks": [_basic_task("001")]}
    assert pi.validate_proposal_shape(payload) == []


def test_a_string_estimatedIterations_is_refused():
    errors = pi.validate_proposal_shape(
        {"tasks": [_basic_task("001", estimatedIterations="2")]}
    )
    assert any("estimatedIterations" in e and "001" in e for e in errors)


def test_a_float_estimatedIterations_is_refused():
    errors = pi.validate_proposal_shape(
        {"tasks": [_basic_task("001", estimatedIterations=2.5)]}
    )
    assert any("estimatedIterations" in e for e in errors)


def test_a_zero_estimatedIterations_is_refused():
    errors = pi.validate_proposal_shape(
        {"tasks": [_basic_task("001", estimatedIterations=0)]}
    )
    assert any("estimatedIterations" in e for e in errors)


def test_a_negative_estimatedIterations_is_refused():
    errors = pi.validate_proposal_shape(
        {"tasks": [_basic_task("001", estimatedIterations=-1)]}
    )
    assert any("estimatedIterations" in e for e in errors)


def test_a_bool_estimatedIterations_is_refused():
    """`bool` is an `int` subclass in Python -- `True`/`False` are not
    iteration counts, and must not slip through the `isinstance(x, int)`
    check."""
    errors = pi.validate_proposal_shape(
        {"tasks": [_basic_task("001", estimatedIterations=True)]}
    )
    assert any("estimatedIterations" in e for e in errors)


# ---------------------------------------------------------------------------
# normalize_task_estimated_iterations -- default + recorded defaulting
# ---------------------------------------------------------------------------

def test_a_present_valid_value_passes_through_with_no_warning():
    payload = {"tasks": [_basic_task("001", estimatedIterations=3)]}
    warnings = pi.normalize_task_estimated_iterations(payload)
    assert warnings == []
    assert payload["tasks"][0]["estimatedIterations"] == 3


def test_a_missing_value_defaults_to_one_with_one_recorded_warning():
    payload = {"tasks": [_basic_task("001")]}
    warnings = pi.normalize_task_estimated_iterations(payload)
    assert len(warnings) == 1
    assert "001" in warnings[0] and "defaulted" in warnings[0] and "1" in warnings[0]
    assert payload["tasks"][0]["estimatedIterations"] == 1


def test_estimated_iterations_warnings_are_a_channel_distinct_from_shape_errors():
    payload = {"tasks": [_basic_task("001")]}
    assert pi.validate_proposal_shape(payload) == []
    assert pi.lint_proposal(payload) == []


# ---------------------------------------------------------------------------
# ingest_proposal -- estimatedIterations survives as a passthrough field
# ---------------------------------------------------------------------------

def test_ingest_preserves_a_planner_supplied_estimate_verbatim():
    payload = {"tasks": [_basic_task("001", estimatedIterations=2)]}
    doc = pi.ingest_proposal(payload)
    assert doc["tasks"][0]["estimatedIterations"] == 2


def test_an_old_proposal_with_no_estimate_anywhere_ingests_with_no_key():
    """`ingest_proposal` itself never invents the key -- exactly the same
    discipline `normalize_task_tiers`/`ingest_proposal` already follow for
    `tier`: defaulting is `normalize_task_estimated_iterations`'s job, run
    by `_parse_proposal`, not `ingest_proposal`'s own."""
    payload = {"tasks": [_basic_task("001")]}
    doc = pi.ingest_proposal(payload)
    assert "estimatedIterations" not in doc["tasks"][0]


# ---------------------------------------------------------------------------
# The full loop: enforce_plan_ingestion
# ---------------------------------------------------------------------------

def test_a_missing_estimate_defaults_to_one_through_the_full_loop(tmp_path):
    _write(tmp_path, "plan-proposal.json", {"tasks": [_basic_task("001")]})

    def respond(attempt, prior_errors):
        return _pointer("plan-proposal.json")

    result = pi.enforce_plan_ingestion(
        respond, max_retries=2, read_proposal=pi.file_reader(tmp_path)
    )
    assert result.ok
    assert result.attempts == 1
    assert result.tasks_doc["tasks"][0]["estimatedIterations"] == 1
    assert any("estimatedIterations" in w and "defaulted" in w for w in result.warnings)


def test_an_invalid_estimate_is_refused_and_reprompted_naming_the_task(tmp_path):
    _write(tmp_path, "plan-proposal.json",
           {"tasks": [_basic_task("001", estimatedIterations="soon")]})

    attempts_seen = []

    def respond(attempt, prior_errors):
        attempts_seen.append((attempt, prior_errors))
        return _pointer("plan-proposal.json")

    result = pi.enforce_plan_ingestion(
        respond, max_retries=1, read_proposal=pi.file_reader(tmp_path)
    )
    assert not result.ok
    assert result.agent_fault
    assert any("estimatedIterations" in e and "001" in e for e in result.errors)
    # re-prompted with the specific error on the second attempt
    assert attempts_seen[1][1] is not None
    assert any("estimatedIterations" in e for e in attempts_seen[1][1])


def test_a_valid_estimate_survives_the_full_loop_verbatim(tmp_path):
    _write(tmp_path, "plan-proposal.json",
           {"tasks": [_basic_task("001", estimatedIterations=3)]})

    def respond(attempt, prior_errors):
        return _pointer("plan-proposal.json")

    result = pi.enforce_plan_ingestion(
        respond, max_retries=0, read_proposal=pi.file_reader(tmp_path)
    )
    assert result.ok
    assert result.tasks_doc["tasks"][0]["estimatedIterations"] == 3
    assert not any("estimatedIterations" in w for w in result.warnings)


# ---------------------------------------------------------------------------
# planning.md documents the field, never inside the schema-example fence
# ---------------------------------------------------------------------------

def test_planning_md_documents_estimatedIterations():
    assert "`estimatedIterations`" in PLANNING_MD
    assert "OPTIONAL per-task integer" in PLANNING_MD
    assert "defaults it to `1`" in PLANNING_MD
    assert "recording that it defaulted" in PLANNING_MD
    assert "rejected at ingestion" in PLANNING_MD


def test_planning_md_estimatedIterations_doc_is_outside_every_code_fence():
    """No new fenced code block was added to document the field -- the
    one pre-existing schema-example fence is untouched, and
    `estimatedIterations` never appears inside it (the "no contract object
    inside a code fence" convention `tests/test_worker_progress_claim.py`
    and `tests/test_deferred_tasks_surface.py` already pin for their own
    new contract fields)."""
    fences = re_findall_fences(PLANNING_MD)
    assert len(fences) == 1, "a new code fence was added to planning.md"
    assert "estimatedIterations" not in fences[0]


def re_findall_fences(text: str) -> list[str]:
    import re
    return re.findall(r"```.*?```", text, re.DOTALL)
