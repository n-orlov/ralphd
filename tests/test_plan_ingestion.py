"""Task 011 (requirement C, #60 pillar 1): ingest the planner's file-borne
plan proposal -- schema validation of the pointer, structural shape
validation of the proposal file, semantic lint, then ingestion.

No engine cutover is asserted here -- task 014's executor cutover decides
where in the real loop a planning iteration's proposal reaches this module.
This module only proves the ingestion mechanism itself is correct and
observable in isolation, exactly as `test_response_contracts.py` and
`test_contract_enforcement.py` did for tasks 009 and 010.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ralphd.engine import plan_ingestion as pi
from ralphd.engine.state import RunDir


def _pointer(path: str, summary: str = "a plan") -> str:
    return json.dumps({"proposal_path": path, "summary": summary})


def _write(tmp_path, name: str, doc: dict | str) -> None:
    text = doc if isinstance(doc, str) else json.dumps(doc)
    (tmp_path / name).write_text(text)


def _basic_task(task_id: str, **overrides) -> dict:
    task = {
        "id": task_id,
        "title": f"do thing {task_id}",
        "successCriteria": f"thing {task_id} is verifiably done",
    }
    task.update(overrides)
    return task


# ---------------------------------------------------------------------------
# validate_proposal_shape -- structural, ad hoc (not schema-shaped)
# ---------------------------------------------------------------------------

def test_a_well_formed_proposal_has_no_shape_errors():
    payload = {"tasks": [_basic_task("001")]}
    assert pi.validate_proposal_shape(payload) == []


def test_a_non_object_top_level_is_reported():
    errors = pi.validate_proposal_shape(["not", "an", "object"])
    assert errors and "object" in errors[0]


def test_a_missing_tasks_key_is_reported():
    errors = pi.validate_proposal_shape({"goal": "x"})
    assert errors == ["$.tasks: missing"]


def test_tasks_not_a_list_is_reported():
    errors = pi.validate_proposal_shape({"tasks": {"001": {}}})
    assert "array" in errors[0]


def test_a_task_missing_required_fields_is_named_by_position_and_id():
    errors = pi.validate_proposal_shape({"tasks": [{"id": "001"}]})
    assert any("title" in e for e in errors)
    assert any("successCriteria" in e for e in errors)
    assert all("001" in e for e in errors)


def test_dependson_must_be_an_array():
    errors = pi.validate_proposal_shape(
        {"tasks": [_basic_task("001", dependsOn="002")]}
    )
    assert any("dependsOn" in e for e in errors)


# ---------------------------------------------------------------------------
# lint_proposal -- semantic, each error names the offending task
# ---------------------------------------------------------------------------

def test_duplicate_ids_are_named():
    payload = {"tasks": [_basic_task("001"), _basic_task("001")]}
    errors = pi.lint_proposal(payload)
    assert any("duplicate id" in e and "001" in e for e in errors)


def test_unresolved_dependson_is_named():
    payload = {"tasks": [_basic_task("001", dependsOn=["999"])]}
    errors = pi.lint_proposal(payload)
    assert any("001" in e and "999" in e and "dependsOn" in e for e in errors)


def test_a_dependson_cycle_is_named():
    payload = {
        "tasks": [
            _basic_task("001", dependsOn=["002"]),
            _basic_task("002", dependsOn=["001"]),
        ]
    }
    errors = pi.lint_proposal(payload)
    assert any("cycle" in e for e in errors)
    assert any("001" in e or "002" in e for e in errors)


def test_a_three_task_cycle_is_named():
    payload = {
        "tasks": [
            _basic_task("001", dependsOn=["003"]),
            _basic_task("002", dependsOn=["001"]),
            _basic_task("003", dependsOn=["002"]),
        ]
    }
    errors = pi.lint_proposal(payload)
    assert any("cycle" in e for e in errors)


def test_empty_success_criteria_is_named():
    payload = {"tasks": [_basic_task("001", successCriteria="   ")]}
    errors = pi.lint_proposal(payload)
    assert any("001" in e and "boilerplate" in e for e in errors)


def test_boilerplate_success_criteria_is_named():
    payload = {
        "tasks": [_basic_task("001", successCriteria=pi.BOILERPLATE_SUCCESS_CRITERIA)]
    }
    errors = pi.lint_proposal(payload)
    assert any("001" in e and "boilerplate" in e for e in errors)


def test_a_half_filled_placeholder_is_also_boilerplate():
    payload = {"tasks": [_basic_task("001", successCriteria="<fill this in later>")]}
    errors = pi.lint_proposal(payload)
    assert any("boilerplate" in e for e in errors)


def test_a_real_criterion_is_not_flagged():
    payload = {"tasks": [_basic_task("001")]}
    assert pi.lint_proposal(payload) == []


def test_a_status_field_is_rejected_not_honoured():
    """Requirement C: a proposal carrying a status field is rejected, not
    honoured -- the enforcement point for the property tested end to end
    below in test_ingest_proposal_never_honours_a_smuggled_status."""
    payload = {"tasks": [_basic_task("001", status="completed")]}
    errors = pi.lint_proposal(payload)
    assert any("001" in e and "status" in e for e in errors)


# ---------------------------------------------------------------------------
# lint_own_terminal_state -- requirement H's plan-time half (task 012,
# #63's incident, fact 13). The literal v0.8 task 042 JSON no longer exists
# anywhere reachable from this repo (it lived only in that run's own run
# directory, torn down once the run finished, and was never committed
# here). `pi.TASK_042_SUCCESS_CRITERIA` is therefore a reconstruction, not a
# recovered original. What *is* checked, by the test right below, is
# narrower and stronger than a comment's say-so: `git show 5f6b6d9^` (the
# commit *before* the one that rewrote `docs/prds/README.md`'s row for that
# run) holds the clause "required `status.json` to leave `state: running`
# before its closing report could send" as prose, reflowed across a hard
# line wrap in the markdown source. The test below reads that historical
# git blob directly and asserts the clause is a literal substring of it
# (after collapsing the source's own whitespace-only line wrap -- no word
# changes), and that the same clause is a literal substring of
# `TASK_042_SUCCESS_CRITERIA` itself -- so the "verbatim" claim is proven
# against the repository's own history in this run, not merely asserted in
# a comment (task 012's first pass shipped exactly that unproven assertion
# and a validation pass caught it: the fixture text did not actually
# contain the clause it claimed to quote).
# ---------------------------------------------------------------------------

def test_task_042_fixture_embeds_the_clause_that_survives_in_git_history():
    """Proves the "verbatim" claim in `TASK_042_SUCCESS_CRITERIA`'s own
    comment against the actual git blob, rather than trusting the comment."""
    import re
    import subprocess

    blob = subprocess.check_output(
        ["git", "show", "5f6b6d9^:docs/prds/README.md"],
        cwd=Path(__file__).resolve().parents[1],
    ).decode()
    normalised = re.sub(r"\s+", " ", blob)
    clause = (
        "required `status.json` to leave `state: running` before its "
        "closing report could send"
    )
    assert clause in normalised, "the surviving clause is not in the cited git blob"
    assert clause in pi.TASK_042_SUCCESS_CRITERIA, (
        "the fixture no longer embeds the clause it claims to quote verbatim"
    )


def test_v08_task_042s_reconstructed_criteria_is_rejected():
    """The fixture is a reconstruction, not a recovered original -- see the
    section comment above and `plan_ingestion.TASK_042_SUCCESS_CRITERIA`'s
    own comment for exactly which clause survives verbatim and why the rest
    does not. It still has to trip the lint the same way the real task did."""
    payload = {
        "tasks": [_basic_task("042", successCriteria=pi.TASK_042_SUCCESS_CRITERIA)]
    }
    errors = pi.lint_own_terminal_state(payload)
    assert any("042" in e and "terminal state" in e for e in errors)


def test_leaving_state_running_is_rejected_regardless_of_terminal_wording():
    r"""The `leave/leaves/left/leaving \`state: running\`` clause alone is
    already v0.8 task 042's exact shape, independent of whether the literal
    words "terminal state" also appear."""
    payload = {
        "tasks": [_basic_task(
            "001",
            successCriteria=(
                "this task's report can only send once status.json leaves "
                "`state: running`"
            ),
        )]
    }
    errors = pi.lint_own_terminal_state(payload)
    assert any("001" in e for e in errors)


def test_a_criterion_that_merely_mentions_status_is_accepted():
    """Both directions asserted, per task 012's own success criteria: a
    criterion that mentions "status" (or even a *different* thing's
    terminal-ness, task 008's `finishing` non-terminal *run state* included)
    without gating this task's own completion on this run's own terminal
    state must not be flagged."""
    payload = {
        "tasks": [
            _basic_task(
                "001",
                successCriteria=(
                    "GET /status exposes the current task counts, and a test "
                    "asserts each key is present"
                ),
            ),
            _basic_task(
                "002",
                successCriteria=(
                    "`finishing` lands as a non-terminal run state in the "
                    "transition table, asserted by a test"
                ),
            ),
        ]
    }
    assert pi.lint_own_terminal_state(payload) == []


def test_own_terminal_state_lint_is_wired_into_proposal_parsing(tmp_path):
    """`_parse_proposal` (ingestion's step 3) runs `lint_own_terminal_state`
    alongside `lint_proposal`, not instead of it -- a proposal can fail on
    both lints at once and both errors surface."""
    payload = {"tasks": [_basic_task("042", successCriteria=pi.TASK_042_SUCCESS_CRITERIA)]}
    _, errors = pi._parse_proposal(json.dumps(payload))
    assert any("terminal state" in e for e in errors)


# ---------------------------------------------------------------------------
# ingest_proposal -- ids survive verbatim, every task starts pending
# ---------------------------------------------------------------------------

def test_ingest_preserves_ids_verbatim():
    payload = {"tasks": [_basic_task("007"), _basic_task("013")]}
    doc = pi.ingest_proposal(payload)
    assert [t["id"] for t in doc["tasks"]] == ["007", "013"]


def test_ingest_sets_every_task_to_pending():
    payload = {"tasks": [_basic_task("001"), _basic_task("002")]}
    doc = pi.ingest_proposal(payload)
    assert all(t["status"] == "pending" for t in doc["tasks"])


def test_ingest_proposal_never_honours_a_smuggled_status():
    """Even if a status field slipped past lint somehow, ingestion itself
    still overwrites it -- belt-and-suspenders, per the module docstring."""
    payload = {"tasks": [_basic_task("001", status="completed")]}
    doc = pi.ingest_proposal(payload)
    assert doc["tasks"][0]["status"] == "pending"


def test_ingest_preserves_dependson_and_priority():
    payload = {"tasks": [_basic_task("001", dependsOn=["000"], priority=5)]}
    doc = pi.ingest_proposal(payload)
    assert doc["tasks"][0]["dependsOn"] == ["000"]
    assert doc["tasks"][0]["priority"] == 5


def test_ingest_carries_goal_and_scope_through():
    payload = {
        "goal": "ship it",
        "scope": {"level": "single-repo", "reasoning": "one repo"},
        "tasks": [_basic_task("001")],
    }
    doc = pi.ingest_proposal(payload)
    assert doc["goal"] == "ship it"
    assert doc["scope"]["level"] == "single-repo"


# ---------------------------------------------------------------------------
# enforce_plan_ingestion -- the full retry loop, pointer + file
# ---------------------------------------------------------------------------

def test_a_valid_first_attempt_ingests_without_a_reprompt(tmp_path):
    _write(tmp_path, "plan-proposal.json", {"tasks": [_basic_task("001")]})
    calls = []

    def respond(attempt, prior_errors):
        calls.append((attempt, prior_errors))
        return _pointer("plan-proposal.json")

    result = pi.enforce_plan_ingestion(
        respond, max_retries=2, read_proposal=pi.file_reader(tmp_path)
    )
    assert result.ok
    assert result.attempts == 1
    assert not result.agent_fault
    assert [t["id"] for t in result.tasks_doc["tasks"]] == ["001"]
    assert calls == [(0, None)]


def test_an_absent_proposal_file_reprompts_with_the_specific_error(tmp_path):
    attempts = []

    def respond(attempt, prior_errors):
        attempts.append(prior_errors)
        if attempt == 0:
            return _pointer("plan-proposal.json")  # file does not exist yet
        _write(tmp_path, "plan-proposal.json", {"tasks": [_basic_task("001")]})
        return _pointer("plan-proposal.json")

    result = pi.enforce_plan_ingestion(
        respond, max_retries=2, read_proposal=pi.file_reader(tmp_path)
    )
    assert result.ok
    assert result.attempts == 2
    assert attempts[0] is None
    assert attempts[1] and "absent" in attempts[1][0]


def test_a_lint_failure_reprompts_with_the_specific_error_then_recovers(tmp_path):
    _write(tmp_path, "plan-proposal.json",
           {"tasks": [_basic_task("001"), _basic_task("001")]})  # duplicate id
    attempts = []

    def respond(attempt, prior_errors):
        attempts.append(prior_errors)
        if attempt == 1:
            _write(tmp_path, "plan-proposal.json", {"tasks": [_basic_task("001")]})
        return _pointer("plan-proposal.json")

    result = pi.enforce_plan_ingestion(
        respond, max_retries=2, read_proposal=pi.file_reader(tmp_path)
    )
    assert result.ok
    assert result.attempts == 2
    assert attempts[0] is None
    assert attempts[1] and any("duplicate id" in e for e in attempts[1])


def test_a_status_smuggling_proposal_never_ingests_even_within_budget(tmp_path):
    """The whole point of requirement C's rule: a proposal that never stops
    trying to smuggle a status field never ingests, however many retries it
    gets -- it is not honoured under any budget."""
    _write(tmp_path, "plan-proposal.json",
           {"tasks": [_basic_task("001", status="completed")]})

    def respond(attempt, prior_errors):
        return _pointer("plan-proposal.json")

    result = pi.enforce_plan_ingestion(
        respond, max_retries=2, read_proposal=pi.file_reader(tmp_path)
    )
    assert not result.ok
    assert result.agent_fault
    assert result.tasks_doc is None
    assert any("status" in e for e in result.errors)


def test_agent_fault_after_exhausting_retries_on_a_persistently_bad_pointer(tmp_path):
    def respond(attempt, prior_errors):
        return "{not json"

    result = pi.enforce_plan_ingestion(
        respond, max_retries=1, read_proposal=pi.file_reader(tmp_path)
    )
    assert not result.ok
    assert result.agent_fault
    assert result.attempts == 2


def test_violations_and_agent_fault_land_on_disk_in_order(tmp_path):
    run = RunDir(root=tmp_path / "run")
    _write(tmp_path, "plan-proposal.json", {"tasks": [{"id": "001"}]})  # blank fields

    def respond(attempt, prior_errors):
        return _pointer("plan-proposal.json")

    result = pi.enforce_plan_ingestion(
        respond, max_retries=1, read_proposal=pi.file_reader(tmp_path), state=run
    )
    assert result.agent_fault

    events = [json.loads(line) for line in
              (tmp_path / "run" / "events.jsonl").read_text().splitlines()]
    types = [e["type"] for e in events]
    assert types == ["contract.violation", "contract.violation", "contract.agent_fault"]
    for e in events:
        assert e["hat"] == "planning"


def test_zero_retries_means_exactly_one_attempt(tmp_path):
    calls = []

    def respond(attempt, prior_errors):
        calls.append(attempt)
        return None

    result = pi.enforce_plan_ingestion(
        respond, max_retries=0, read_proposal=pi.file_reader(tmp_path)
    )
    assert calls == [0]
    assert result.attempts == 1
    assert result.agent_fault


def test_negative_retries_is_rejected():
    with pytest.raises(ValueError):
        pi.enforce_plan_ingestion(
            lambda a, e: None, max_retries=-1, read_proposal=lambda p: None
        )


# ---------------------------------------------------------------------------
# The same ingestion path serves an approach-boundary replan
# ---------------------------------------------------------------------------

def test_the_same_function_serves_a_replan(tmp_path):
    """Nothing about enforce_plan_ingestion is first-plan-only: driving it
    twice, as an approach boundary would, ingests each proposal on its own
    terms -- ids from the replan survive verbatim too, and there is no
    hidden state carried over from the first call."""
    first_dir = tmp_path / "approach-1"
    second_dir = tmp_path / "approach-2"
    first_dir.mkdir()
    second_dir.mkdir()
    _write(first_dir, "plan-proposal.json", {"tasks": [_basic_task("001"), _basic_task("002")]})
    _write(second_dir, "plan-proposal.json", {"tasks": [_basic_task("001")]})

    def respond_factory(base_dir):
        def respond(attempt, prior_errors):
            return _pointer("plan-proposal.json")
        return respond

    first = pi.enforce_plan_ingestion(
        respond_factory(first_dir), max_retries=0, read_proposal=pi.file_reader(first_dir)
    )
    second = pi.enforce_plan_ingestion(
        respond_factory(second_dir), max_retries=0, read_proposal=pi.file_reader(second_dir)
    )

    assert first.ok and second.ok
    assert [t["id"] for t in first.tasks_doc["tasks"]] == ["001", "002"]
    assert [t["id"] for t in second.tasks_doc["tasks"]] == ["001"]
    # the replan's renumbered task "001" is a fresh pending task, not
    # anything read back from the first call's state (there is none).
    assert second.tasks_doc["tasks"][0]["status"] == "pending"


def test_file_reader_returns_none_for_a_missing_file(tmp_path):
    reader = pi.file_reader(tmp_path)
    assert reader("does-not-exist.json") is None


def test_file_reader_reads_a_real_file(tmp_path):
    _write(tmp_path, "plan-proposal.json", {"tasks": []})
    reader = pi.file_reader(tmp_path)
    assert json.loads(reader("plan-proposal.json"))["tasks"] == []
