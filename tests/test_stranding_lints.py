"""Task 012 (PRD requirement D, #114): the first three of the plan's own
five stranding lints (`docs/prds/v0.16-a-petition-is-not-a-postmortem.md`
contract rule 3, lines 79-84) -- relational sha anchors, single-commit
demands on a deliverable whose own criteria also mandate a separate
disclosure/notification/record commit, and forward citation of a task
ordered later in the same plan. Each is folded into `lint_proposal()`
itself (`src/ralphd/engine/plan_ingestion.py:177`), reusing that function's
existing per-task loop rather than adding a fourth separately-run lint.

Every lint below is scoped to ONE clause (`_CLAUSE_SPLIT_RE`), never the
whole criteria string -- the same #86/#76 discipline
`_depends_on_own_terminal_state` already applies to
`lint_own_terminal_state` (`plan_ingestion.py:291-311`), because a criterion
long enough to be useful will otherwise trip on an unrelated mention
elsewhere in the same string. Every lint below ships at least one must-pass
sibling proving exactly that.

Task 013 (this module's own continuation, sections (d) and (e) below) adds
the remaining two: an unscoped write-time occurrence count of a string the
task's own mandated output contains, and a criterion conditioning on the
content or format of untracked run state.
"""

from __future__ import annotations

from pathlib import Path

from ralphd.engine import plan_ingestion as pi


def _basic_task(task_id: str, **overrides) -> dict:
    task = {
        "id": task_id,
        "title": f"do thing {task_id}",
        "successCriteria": f"thing {task_id} is verifiably done",
    }
    task.update(overrides)
    return task


# ---------------------------------------------------------------------------
# (a) relational sha anchors -- HEAD~1, HEAD^, "this commit's parent"
# ---------------------------------------------------------------------------

# The must-catch fixture named by task 012's own success criteria -- "the
# real v0.15 approach-3 task 004 HEAD~1 clause" -- cannot be lifted
# byte-for-byte: that run's own tasks.json lived only in its own run
# directory, torn down once the run finished, never committed here (the
# same loss `plan_ingestion.TASK_042_SUCCESS_CRITERIA`'s own comment
# documents for v0.8's task 042, `tests/test_plan_ingestion.py:155-176`).
# What survives, verifiably, in THIS repository's tracked history is this
# PRD's own description of that clause -- checked directly against the
# tracked file below, not merely asserted in a comment.
def test_the_prd_itself_names_approach_3_task_004s_head_anchor_clause():
    """Proves the reconstruction's own premise against the tracked PRD
    file rather than trusting a comment: the PRD names this exact instance
    (line 204) and the generic pattern it instantiates (line 14)."""
    prd = Path(__file__).resolve().parents[1] / (
        "docs/prds/v0.16-a-petition-is-not-a-postmortem.md"
    )
    text = prd.read_text()
    assert "approach 3 task 004's `HEAD~1` clause" in text
    assert "verify with `HEAD~1`" in text


def test_v015_approach3_task004s_reconstructed_head_anchor_is_rejected():
    """The fixture is a reconstruction, not a recovered original -- see the
    module docstring and the comment immediately above. It still has to
    trip the lint the same way the real clause did."""
    payload = {
        "tasks": [_basic_task("004", successCriteria=pi.TASK_004_HEAD_ANCHOR_CRITERIA)]
    }
    errors = pi.lint_proposal(payload)
    assert any(
        "004" in e and "relational sha" in e and "HEAD~1" in e for e in errors
    ), errors


def test_head_caret_and_this_commits_parent_are_also_rejected():
    payload = {
        "tasks": [
            _basic_task(
                "001",
                successCriteria="Verify by diffing against HEAD^ once landed.",
            ),
            _basic_task(
                "002",
                successCriteria=(
                    "The change must be verifiable by inspecting this "
                    "commit's parent for the prior value."
                ),
            ),
        ]
    }
    errors = pi.lint_proposal(payload)
    assert any("001" in e and "relational sha" in e for e in errors)
    assert any("002" in e and "relational sha" in e for e in errors)


def test_a_criterion_that_merely_mentions_a_sha_or_head_is_not_flagged():
    """Must-pass sibling: an absolute sha, or a bare mention of HEAD/a
    commit with no relational anchor, is legitimate and must not trip."""
    payload = {
        "tasks": [
            _basic_task(
                "001",
                successCriteria=(
                    "Verify against the commit sha `28f3a766` once this "
                    "task's own commit lands; HEAD's own sha at push time "
                    "is quoted in the report."
                ),
            )
        ]
    }
    assert pi.lint_proposal(payload) == []


# ---------------------------------------------------------------------------
# (b) single-commit demand vs a mandated separate disclosure/notification/
#     record commit
# ---------------------------------------------------------------------------

def test_a_single_commit_demand_with_a_separate_notification_commit_is_rejected():
    payload = {
        "tasks": [
            _basic_task(
                "005",
                successCriteria=(
                    "Ship the whole deliverable in a single commit. This "
                    "task also mandates a separate notification commit "
                    "logging the send to the operator."
                ),
            )
        ]
    }
    errors = pi.lint_proposal(payload)
    assert any(
        "005" in e and "single commit" in e and "separate" in e for e in errors
    ), errors


def test_exactly_one_commit_with_its_own_record_commit_is_also_rejected():
    payload = {
        "tasks": [
            _basic_task(
                "006",
                successCriteria=(
                    "The fix lands in exactly one commit. The closing "
                    "record needs its own record commit disclosing the "
                    "deviation."
                ),
            )
        ]
    }
    errors = pi.lint_proposal(payload)
    assert any("006" in e and "single commit" in e for e in errors)


def test_legitimate_one_commit_mention_without_a_second_mandate_is_accepted():
    """Must-pass sibling: 'one commit'/'a single commit' with NO separate
    disclosure/notification/record commit mandated elsewhere is legitimate
    and must not trip -- this is exactly this task's own boundary clause
    shape used throughout this wave's own plan."""
    payload = {
        "tasks": [
            _basic_task(
                "007",
                successCriteria=(
                    "At least one commit lands the fix; the notification "
                    "entry may be appended in that same commit."
                ),
            )
        ]
    }
    assert pi.lint_proposal(payload) == []


# ---------------------------------------------------------------------------
# (c) forward citation of a later task's commit sha
# ---------------------------------------------------------------------------

def test_citing_a_later_tasks_commit_sha_is_rejected():
    payload = {
        "tasks": [
            _basic_task(
                "010",
                successCriteria="Cite the commit sha of task 015 as evidence.",
            ),
            _basic_task("015"),
        ]
    }
    errors = pi.lint_proposal(payload)
    assert any(
        "010" in e and "015" in e and "LATER" in e for e in errors
    ), errors


def test_citing_task_ns_own_commit_worded_the_other_way_is_also_rejected():
    payload = {
        "tasks": [
            _basic_task(
                "010",
                successCriteria="Name task 015's own commit sha in the report.",
            ),
            _basic_task("015"),
        ]
    }
    errors = pi.lint_proposal(payload)
    assert any("010" in e and "015" in e for e in errors)


def test_citing_an_earlier_tasks_commit_sha_is_accepted():
    """Must-pass sibling: citing a task ordered EARLIER in the same plan
    (already committed by the time this task runs) is order-honest and
    must not trip."""
    payload = {
        "tasks": [
            _basic_task("002"),
            _basic_task(
                "010",
                successCriteria="Cite the commit sha of task 002 as evidence.",
            ),
        ]
    }
    assert pi.lint_proposal(payload) == []


def test_a_bare_task_reference_with_no_commit_language_is_accepted():
    """Must-pass sibling: naming another task at all (no commit/sha
    language) is ordinary cross-referencing, not a forward citation."""
    payload = {
        "tasks": [
            _basic_task(
                "010",
                successCriteria="This task's scope overlaps task 015's; see its title.",
            ),
            _basic_task("015"),
        ]
    }
    assert pi.lint_proposal(payload) == []


def test_a_tasks_own_self_reference_is_never_a_forward_citation():
    """'task 010's own commit sha' inside task 010's OWN criteria names
    itself, never anything ordered later -- not a forward citation."""
    payload = {
        "tasks": [
            _basic_task(
                "010",
                successCriteria="Cite task 010's own commit sha in the report.",
            ),
        ]
    }
    assert pi.lint_proposal(payload) == []


# ---------------------------------------------------------------------------
# (d) unscoped write-time occurrence count of a string the task's own
#     mandated output contains
# ---------------------------------------------------------------------------

def test_the_prd_itself_describes_the_write_time_occurrence_count_pattern():
    """No specific v0.15 task number survives for this pattern (unlike (a)'s
    task 004) -- what survives, verifiably, is the PRD's own generic
    description of the two generators that stranded v0.15's criteria,
    checked directly against the tracked file rather than trusted in a
    comment."""
    import re
    prd = Path(__file__).resolve().parents[1] / (
        "docs/prds/v0.16-a-petition-is-not-a-postmortem.md"
    )
    normalised = re.sub(r"\s+", " ", prd.read_text())
    assert (
        "a write-time occurrence count of a sha that the task's own "
        "mandated additions increment"
    ) in normalised


def test_the_reconstructed_unscoped_occurrence_count_criteria_is_rejected():
    payload = {
        "tasks": [
            _basic_task("009", successCriteria=pi.UNSCOPED_SHA_OCCURRENCE_CRITERIA)
        ]
    }
    errors = pi.lint_proposal(payload)
    assert any(
        "009" in e and "unscoped write-time occurrence count" in e for e in errors
    ), errors


def test_other_occurrence_count_phrasings_are_also_rejected():
    payload = {
        "tasks": [
            _basic_task(
                "011",
                successCriteria=(
                    "The notification log must show the phrase 'petition "
                    "filed' 12 instances, though this task's own commit "
                    "appends further entries containing that phrase."
                ),
            ),
            _basic_task(
                "012",
                successCriteria=(
                    "The report contains exactly 4 entries naming the "
                    "deviation, and this task's own additions grow that "
                    "count."
                ),
            ),
        ]
    }
    errors = pi.lint_proposal(payload)
    assert any("011" in e and "unscoped write-time occurrence count" in e for e in errors)
    assert any("012" in e and "unscoped write-time occurrence count" in e for e in errors)


def test_a_self_growing_count_written_with_a_spelled_out_number_is_rejected():
    """Second pass (validation finding): the first cut of this lint only
    understood digits, so `four occurrences` -- the same banned shape in
    ordinary prose -- sailed through."""
    payload = {
        "tasks": [
            _basic_task(
                "018",
                successCriteria=(
                    "The closing report must show four occurrences of the "
                    "launch sha, even though this task's own commit appends "
                    "further occurrences of it to that same report."
                ),
            ),
            _basic_task(
                "019",
                successCriteria=(
                    "The notification log shows up nineteen times the phrase "
                    "'sweep recorded'; this task's own entry increments that "
                    "count."
                ),
            ),
        ]
    }
    errors = pi.lint_proposal(payload)
    assert any("018" in e and "unscoped write-time occurrence count" in e for e in errors), errors
    assert any("019" in e and "unscoped write-time occurrence count" in e for e in errors), errors


def test_a_static_count_the_task_does_not_grow_is_accepted():
    """Must-pass sibling, and the second pass's other validation finding: a
    count of something the task's own mandated output does NOT grow is
    static, verifiable and legitimate -- the banned shape is a *write-time*
    count, not any count."""
    payload = {
        "tasks": [
            _basic_task(
                "020",
                successCriteria="The API response fixture must contain exactly 4 entries.",
            ),
            _basic_task(
                "021",
                successCriteria=(
                    "The API fixture must contain exactly 4 entries. This "
                    "task's own commit adds the fixture."
                ),
            ),
        ]
    }
    assert pi.lint_proposal(payload) == []


def test_a_sha_scoped_figure_is_accepted():
    """Must-pass sibling: 'at `<sha>`: N' is exactly the sha-scoped form
    the standing rules mandate for every figure in a tracked record
    (PRD contract rule 2) -- it must never trip this lint."""
    payload = {
        "tasks": [
            _basic_task(
                "013",
                successCriteria=(
                    "The record states, at `28f3a766`: 19 occurrences of "
                    "the retired sha, discharged by this task's own commit."
                ),
            )
        ]
    }
    assert pi.lint_proposal(payload) == []


def test_a_bare_count_with_no_occurrence_language_is_accepted():
    """Must-pass sibling: '19 tasks remain' names a count with no
    occurrence-of-a-string language at all and must not trip."""
    payload = {
        "tasks": [
            _basic_task("014", successCriteria="19 tasks remain pending in the plan.")
        ]
    }
    assert pi.lint_proposal(payload) == []


# ---------------------------------------------------------------------------
# (e) conditioning on the content or format of untracked run state
# ---------------------------------------------------------------------------

def test_the_prd_itself_names_approach_4_task_001s_notes_md_demand():
    """Proves the reconstruction's own premise against the tracked PRD
    file: the PRD both describes the incident (lines 18-20) and names this
    exact instance (line 204)."""
    prd = Path(__file__).resolve().parents[1] / (
        "docs/prds/v0.16-a-petition-is-not-a-postmortem.md"
    )
    text = prd.read_text()
    assert "approach 4 task 001's notes.md demand" in text
    assert "pointed to*" in text
    assert "the artifact holding 19 stale-sha pairs instead of inlining them" in text


def test_the_reconstructed_notes_inline_demand_is_rejected():
    payload = {
        "tasks": [
            _basic_task("001", successCriteria=pi.TASK_001_NOTES_INLINE_CRITERIA)
        ]
    }
    errors = pi.lint_proposal(payload)
    assert any(
        "001" in e and "untracked run state" in e and "notes.md" in e for e in errors
    ), errors


def test_other_untracked_state_content_demands_are_also_rejected():
    payload = {
        "tasks": [
            _basic_task(
                "015",
                successCriteria=(
                    "The handoff notes must quote the closing report's "
                    "figures verbatim before this task is considered done."
                ),
            ),
            _basic_task(
                "016",
                successCriteria=(
                    "A file outside the repository tree must contain the "
                    "same table in the exact same format as the tracked "
                    "artifact."
                ),
            ),
        ]
    }
    errors = pi.lint_proposal(payload)
    assert any("015" in e and "untracked run state" in e for e in errors)
    assert any("016" in e and "untracked run state" in e for e in errors)


def test_content_demands_on_other_out_of_tree_paths_are_also_rejected():
    """Second pass (validation finding): the first cut's path cues covered
    `notes.md`/`handoff notes` but not other concrete paths that cannot be
    inside the repository tree, and its demand cues left out the commonest
    wording of all -- `must list`."""
    payload = {
        "tasks": [
            _basic_task(
                "022",
                successCriteria="/tmp/handoff.txt must contain the full table.",
            ),
            _basic_task(
                "023",
                successCriteria=(
                    "/run/ralphd/tasks.json must record this task's status "
                    "before the closing report sends."
                ),
            ),
            _basic_task(
                "024",
                successCriteria="The handoff notes must list all 19 stale sha pairs.",
            ),
        ]
    }
    errors = pi.lint_proposal(payload)
    for tid in ("022", "023", "024"):
        assert any(tid in e and "untracked run state" in e for e in errors), (tid, errors)


def test_an_artifact_existence_pointer_is_accepted():
    """Must-pass sibling: requiring an artifact FILE to exist, with the
    scratch-state path merely pointing at it, is exactly the pointer form
    the contract's scratch-state rule declares sufficient -- it must never
    trip."""
    payload = {
        "tasks": [
            _basic_task(
                "017",
                successCriteria=(
                    "artifacts/reports/v0.16-example.md must exist, listing "
                    "the 19 stale-sha pairs; /run/ralphd/notes.md need only "
                    "point to that artifact."
                ),
            )
        ]
    }
    assert pi.lint_proposal(payload) == []


# ---------------------------------------------------------------------------
# End to end: the refusal reaches the operator through the existing plan
# re-ask ladder, not just `lint_proposal` in isolation -- drives
# `enforce_plan_ingestion` (pointer schema -> file read -> `_parse_proposal`
# -> `lint_proposal`), same shape as
# `test_plan_ingestion.py::test_a_lint_failure_reprompts_with_the_specific_error_then_recovers`.
# ---------------------------------------------------------------------------

def _pointer(path: str) -> str:
    import json as _json
    return _json.dumps({"proposal_path": path, "summary": "a plan"})


def _write(tmp_path, name: str, doc) -> None:
    import json as _json
    text = doc if isinstance(doc, str) else _json.dumps(doc)
    (tmp_path / name).write_text(text)


def test_an_unscoped_occurrence_count_reprompts_then_recovers_end_to_end(tmp_path):
    _write(tmp_path, "plan-proposal.json",
           {"tasks": [_basic_task("009", successCriteria=pi.UNSCOPED_SHA_OCCURRENCE_CRITERIA)]})
    attempts = []

    def respond(attempt, prior_errors):
        attempts.append(prior_errors)
        if attempt == 1:
            _write(tmp_path, "plan-proposal.json", {"tasks": [_basic_task("009")]})
        return _pointer("plan-proposal.json")

    result = pi.enforce_plan_ingestion(
        respond, max_retries=2, read_proposal=pi.file_reader(tmp_path)
    )
    assert result.ok
    assert result.attempts == 2
    assert attempts[0] is None
    assert attempts[1] and any(
        "unscoped write-time occurrence count" in e for e in attempts[1]
    )


def test_a_scratch_state_content_demand_reprompts_then_recovers_end_to_end(tmp_path):
    _write(tmp_path, "plan-proposal.json",
           {"tasks": [_basic_task("001", successCriteria=pi.TASK_001_NOTES_INLINE_CRITERIA)]})
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
    assert attempts[1] and any(
        "untracked run state" in e for e in attempts[1]
    )
