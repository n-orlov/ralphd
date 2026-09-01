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

Task 013 adds the remaining two lints (unscoped self-growing counts,
scratch-state format); this module is task 012's alone.
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
