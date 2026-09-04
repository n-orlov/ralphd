"""Task 013 (requirement E, #143): the seventh ingestion lint --
`plan_ingestion.lint_proposal()` now also refuses a `successCriteria`
clause carrying one of six structurally-unsatisfiable shapes, quoting the
offending clause and naming a satisfiable alternative. Each check lives in
`plan_ingestion.py` right above `lint_proposal` itself
(`_unsatisfiable_shape_clause`, backed by `_UNSATISFIABLE_SHAPE_CHECKS`),
scoped to ONE clause at a time (`_CLAUSE_SPLIT_RE`) -- the same #86/#76
discipline every earlier lint in that module already follows.

Provenance, per shape (see `plan_ingestion.py`'s own block comment
immediately above `_LATER_FINAL_LAST_COMMIT_RE` for the full account):

  (h) later/final/last commit -- recovered verbatim from this repository's
      own tracked PRDs (v0.14's non-negotiable 12; v0.15's/v0.17's
      identical "recorded as the last commit" requirement-list bullet,
      the exact clause `v0.17-a3-closing-report.md` quotes when review
      rejected approach 3 on it).
  (i) predeclared event state -- recovered verbatim from
      `artifacts/reports/v0.17-a3-closing-report.md` (task 014's own
      successCriteria clause 2 parenthetical).
  (j) a record citing the commit that writes it -- the impossibility
      `v0.17-a3-closing-report.md` states in so many words; issue #143
      attributes the concrete recurrence to `deck-3i-2`, a sibling run
      whose files this sandbox cannot reach -- disclosed as such, exactly
      like `TASK_042_SUCCESS_CRITERIA` already discloses for v0.8 task 042.
  (k) an empty git range -- issue #143's own account of `deck-3h`/`3i-2`/
      `spike`, likewise unreachable from this sandbox; modelled on that
      account, disclosed as modelled, not recovered.
  (l) an "exactly once" commit count -- issue #143's own account of
      `deck-spike`, same disclosure as (k).
  (m) notification/HTTP-status clause -- recovered verbatim from
      `artifacts/reports/v0.17-a4-closing-report.md` Finding 2's own
      quoted clause.

Every shape below ships at least one must-catch fixture and one must-pass
sibling, exactly like `tests/test_stranding_lints.py` and
`tests/test_criteria_bound_lint.py` already do for their own lints.
"""

from __future__ import annotations

import re
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


REPO_ROOT = Path(__file__).resolve().parents[1]


def _flattened(rel_path: str) -> str:
    """Whitespace-flattened text of a tracked file, with markdown emphasis
    markers (`**`/`*`) and leading blockquote `>` markers stripped too --
    several of the recovered clauses below sit inside a bolded PRD bullet
    or a blockquoted review finding, and neither is part of the clause's
    own words."""
    lines = (REPO_ROOT / rel_path).read_text().splitlines()
    cleaned = [re.sub(r"^\s*>\s?", "", ln) for ln in lines]
    text = " ".join(cleaned).replace("**", "").replace("*", "")
    return re.sub(r"\s+", " ", text)


# ---------------------------------------------------------------------------
# (h) a reference to a later/final/last commit or its sha
# ---------------------------------------------------------------------------

V014_RECOVERED_LAST_COMMIT_CLAUSE = (
    "The last commit is followed by requirement B's guard subset"
)

V015_V017_RECOVERED_LAST_COMMIT_CLAUSE = (
    "The final guard subset at the true final sha, recorded as the last commit,"
)


def test_the_v014_last_commit_clause_is_recovered_verbatim():
    text = _flattened("docs/prds/v0.14-every-hand-on-the-plan.md")
    assert V014_RECOVERED_LAST_COMMIT_CLAUSE in text


def test_the_v017_last_commit_clause_is_recovered_verbatim():
    text = _flattened("docs/prds/v0.17-a-slip-is-not-a-verdict.md")
    assert V015_V017_RECOVERED_LAST_COMMIT_CLAUSE in text
    # The v0.17 approach-3 closing report discusses this same requirement's
    # last-commit demand at length (its own "Guard subset" section, e.g.
    # "the run's now-declared last commit") without re-quoting the PRD
    # bullet verbatim -- checked as a related but distinct substring.
    report_text = _flattened("artifacts/reports/v0.17-a3-closing-report.md")
    assert "the run's now-declared last commit" in report_text


def test_v014_recovered_last_commit_clause_is_rejected():
    payload = {
        "tasks": [
            _basic_task(
                "001",
                successCriteria=(
                    "This task's own commit lands the fix. "
                    + V014_RECOVERED_LAST_COMMIT_CLAUSE
                    + "."
                ),
            )
        ]
    }
    errors = pi.lint_proposal(payload)
    assert any(
        "001" in e and "structurally unsatisfiable" in e and "last commit" in e
        for e in errors
    ), errors


def test_v015_v017_recovered_last_commit_clause_is_rejected():
    payload = {
        "tasks": [
            _basic_task("002", successCriteria=V015_V017_RECOVERED_LAST_COMMIT_CLAUSE)
        ]
    }
    errors = pi.lint_proposal(payload)
    assert any("002" in e and "structurally unsatisfiable" in e for e in errors), errors


def test_other_last_final_later_commit_phrasings_are_also_rejected():
    payload = {
        "tasks": [
            _basic_task("003", successCriteria="This is the final commit of the wave."),
            _basic_task(
                "004",
                successCriteria="The sha of a later commit must be cited here.",
            ),
        ]
    }
    errors = pi.lint_proposal(payload)
    assert any("003" in e and "structurally unsatisfiable" in e for e in errors)
    assert any("004" in e and "structurally unsatisfiable" in e for e in errors)


def test_the_code_sha_this_task_lands_is_accepted():
    """Must-pass sibling named by this task's own successCriteria."""
    payload = {
        "tasks": [
            _basic_task(
                "005",
                successCriteria=(
                    "The record cites the code sha this task lands, once it "
                    "exists."
                ),
            )
        ]
    }
    assert pi.lint_proposal(payload) == []


def test_a_past_sha_reference_is_accepted():
    payload = {
        "tasks": [
            _basic_task(
                "006",
                successCriteria="At commit `8db9739`, the guard subset was green.",
            )
        ]
    }
    assert pi.lint_proposal(payload) == []


# ---------------------------------------------------------------------------
# (i) a predeclared event state
# ---------------------------------------------------------------------------

V017_RECOVERED_PREDECLARED_EVENT_STATE_CLAUSE = (
    "its petition state (both prior petitions REFUTED, none newly filed)"
)


def test_the_v017_predeclared_event_state_clause_is_recovered_verbatim():
    text = _flattened("artifacts/reports/v0.17-a3-closing-report.md")
    assert V017_RECOVERED_PREDECLARED_EVENT_STATE_CLAUSE in text
    # The a4 report restates the same underlying fact more tersely (without
    # the "its petition state (" framing words) -- checked as its own,
    # slightly shorter, substring rather than claimed identical.
    text_a4 = _flattened("artifacts/reports/v0.17-a4-closing-report.md")
    assert "both prior petitions REFUTED, none newly filed" in text_a4


def test_v017_recovered_predeclared_event_state_clause_is_rejected():
    payload = {
        "tasks": [
            _basic_task(
                "007",
                successCriteria=(
                    "The closing report states "
                    + V017_RECOVERED_PREDECLARED_EVENT_STATE_CLAUSE
                    + "."
                ),
            )
        ]
    }
    errors = pi.lint_proposal(payload)
    assert any("007" in e and "structurally unsatisfiable" in e for e in errors), errors


def test_the_task_wordings_own_example_no_approach_rejected_is_rejected():
    payload = {
        "tasks": [
            _basic_task(
                "008",
                successCriteria="The closing report states no approach rejected.",
            )
        ]
    }
    errors = pi.lint_proposal(payload)
    assert any("008" in e and "structurally unsatisfiable" in e for e in errors), errors


def test_a_criterion_naming_whatever_the_true_outcome_turns_out_to_be_is_accepted():
    """Must-pass sibling: the satisfiable form the lint's own error text
    recommends -- naming the true outcome, not a specific outcome in
    advance."""
    payload = {
        "tasks": [
            _basic_task(
                "009",
                successCriteria=(
                    "The closing report names the review verdict, "
                    "whatever it turns out to be."
                ),
            )
        ]
    }
    assert pi.lint_proposal(payload) == []


# ---------------------------------------------------------------------------
# (j) a record required to cite the commit that writes it
# ---------------------------------------------------------------------------

V017_RECOVERED_CITES_OWN_COMMIT_CLAUSE = (
    "A file cannot contain the sha of the commit that writes it"
)


def test_the_v017_cites_own_commit_clause_is_recovered_verbatim():
    text = _flattened("artifacts/reports/v0.17-a3-closing-report.md")
    assert V017_RECOVERED_CITES_OWN_COMMIT_CLAUSE in text


def test_a_record_required_to_cite_the_commit_that_writes_it_is_rejected():
    payload = {
        "tasks": [
            _basic_task(
                "010",
                successCriteria=(
                    "Each ledger row must cite the sha of the commit that "
                    "writes it."
                ),
            )
        ]
    }
    errors = pi.lint_proposal(payload)
    assert any("010" in e and "structurally unsatisfiable" in e for e in errors), errors


def test_deck_3i_2_shaped_row_naming_its_own_writing_commit_is_rejected():
    """Modelled on issue #143's own account of `deck-3i-2` (x2) -- disclosed
    as modelled/not recovered, since that run's files are not reachable
    from this sandbox (a different product's run directory, never
    committed here); the ISSUE ITSELF is a tracked, checkable source
    (`GET /repos/n-orlov/ralphd/issues/143`)."""
    payload = {
        "tasks": [
            _basic_task(
                "011",
                successCriteria=(
                    "The audit-trail row this task appends names the "
                    "commit that writes it as its own sha."
                ),
            )
        ]
    }
    errors = pi.lint_proposal(payload)
    assert any("011" in e and "structurally unsatisfiable" in e for e in errors), errors


def test_citing_the_report_state_sha_the_parent_of_head_is_accepted():
    """Must-pass sibling: the satisfiable convention
    `docs/sweep-records.md` already documents -- the commit that WROTE the
    previous row, not this row's own commit."""
    payload = {
        "tasks": [
            _basic_task(
                "012",
                successCriteria=(
                    "The record names its own report-state sha, the parent "
                    "of the commit that adds it."
                ),
            )
        ]
    }
    assert pi.lint_proposal(payload) == []


# ---------------------------------------------------------------------------
# (k) a git range required to be empty
# ---------------------------------------------------------------------------


def test_a_git_log_range_required_to_be_empty_is_rejected():
    """Modelled on issue #143's own account of `deck-3h`/`deck-3i-2`/
    `deck-spike`'s protected-path audit ranges -- disclosed as modelled,
    not recovered, for the same reason as (j) above."""
    payload = {
        "tasks": [
            _basic_task(
                "013",
                successCriteria=(
                    "`git log --oneline -- config/protected/` since this "
                    "plan's own first commit must show no commits, an "
                    "empty range."
                ),
            )
        ]
    }
    errors = pi.lint_proposal(payload)
    assert any("013" in e and "structurally unsatisfiable" in e for e in errors), errors


def test_other_empty_git_range_phrasings_are_also_rejected():
    payload = {
        "tasks": [
            _basic_task(
                "014",
                successCriteria=(
                    "The range `git diff --stat` computes over that "
                    "protected path must be empty."
                ),
            )
        ]
    }
    errors = pi.lint_proposal(payload)
    assert any("014" in e and "structurally unsatisfiable" in e for e in errors), errors


def test_git_status_porcelain_being_empty_is_accepted():
    """Must-pass sibling: `git status --porcelain` empty is an ordinary
    clean-tree check, not a range demand -- and is already a must-pass
    fixture for a different lint in `tests/test_criteria_bound_lint.py`,
    which this lint must not disturb."""
    payload = {
        "tasks": [
            _basic_task(
                "015",
                successCriteria=(
                    "All changes are committed, confirmed by `git status "
                    "--porcelain` being empty."
                ),
            )
        ]
    }
    assert pi.lint_proposal(payload) == []


def test_a_relational_sha_diff_stat_clause_is_not_double_counted_here():
    """The recovered v0.15 task 004 relational-sha fixture already caught
    by the FIRST stranding lint (task 012) must not additionally trip this
    empty-git-range check -- it names no 'empty'/'no commits'/'range'
    cue."""
    payload = {"tasks": [_basic_task("016", successCriteria=pi.TASK_004_HEAD_ANCHOR_CRITERIA)]}
    errors = pi.lint_proposal(payload)
    assert not any(
        "016" in e and "structurally unsatisfiable" in e and "empty" in e
        for e in errors
    ), errors


# ---------------------------------------------------------------------------
# (l) an "exactly once" commit count
# ---------------------------------------------------------------------------


def test_an_exactly_once_commit_count_is_rejected():
    """Modelled on issue #143's own account of `deck-spike` (x4) --
    disclosed as modelled, not recovered, for the same reason as (j)/(k)
    above."""
    payload = {
        "tasks": [
            _basic_task(
                "017",
                successCriteria=(
                    "The fix for the report the review faulted commits "
                    "exactly once, correcting only that report."
                ),
            )
        ]
    }
    errors = pi.lint_proposal(payload)
    assert any("017" in e and "structurally unsatisfiable" in e for e in errors), errors


def test_exactly_once_commit_count_is_distinct_from_the_single_commit_lint():
    """'Exactly once' (this lint) is unconditional; the existing
    single-commit-demand lint (task 012) only fires when a SEPARATE
    disclosure/record commit is also mandated. A bare 'exactly one commit'
    with no such contradiction must still pass the OLDER lint, proving the
    two checks are independent."""
    payload = {
        "tasks": [
            _basic_task(
                "018",
                successCriteria="The fix lands in exactly one commit.",
            )
        ]
    }
    errors = pi.lint_proposal(payload)
    assert not any(
        "018" in e and "single commit" in e for e in errors
    ), errors


def test_at_least_one_commit_wording_is_accepted():
    payload = {
        "tasks": [
            _basic_task(
                "019",
                successCriteria="At least one commit lands the fix.",
            )
        ]
    }
    assert pi.lint_proposal(payload) == []


# ---------------------------------------------------------------------------
# (m) a notification or HTTP-status clause
# ---------------------------------------------------------------------------

V017_A4_RECOVERED_NOTIFICATION_HTTP_STATUS_CLAUSE = (
    "every task appends endpoint, substance, and observed HTTP status"
)


def test_the_v017_a4_notification_http_status_clause_is_recovered_verbatim():
    text = _flattened("artifacts/reports/v0.17-a4-closing-report.md")
    assert V017_A4_RECOVERED_NOTIFICATION_HTTP_STATUS_CLAUSE in text


def test_a_notification_ledger_entry_clause_is_rejected():
    payload = {
        "tasks": [
            _basic_task(
                "020",
                successCriteria=(
                    "This task's own successCriteria requires a "
                    "notification ledger entry: "
                    + V017_A4_RECOVERED_NOTIFICATION_HTTP_STATUS_CLAUSE
                    + "."
                ),
            )
        ]
    }
    errors = pi.lint_proposal(payload)
    assert any("020" in e and "structurally unsatisfiable" in e for e in errors), errors


def test_a_modal_http_status_demand_is_rejected():
    payload = {
        "tasks": [
            _basic_task(
                "021",
                successCriteria="This task's send is required to record an HTTP status.",
            )
        ]
    }
    errors = pi.lint_proposal(payload)
    assert any("021" in e and "structurally unsatisfiable" in e for e in errors), errors


def test_the_notification_is_logged_bare_mention_is_accepted():
    """Must-pass sibling, and the exact regression this lint must avoid:
    `tests/test_criteria_bound_lint.py`'s own clause-count fixtures use
    'The notification is logged.' as an ordinary filler clause and must
    keep returning `[]` from `lint_proposal` -- this lint is scoped to
    'notification ledger'/'ledger entry'/'notification inventory'/a modal
    HTTP-status demand, never the bare word 'notification'."""
    payload = {
        "tasks": [
            _basic_task("022", successCriteria="The notification is logged.")
        ]
    }
    assert pi.lint_proposal(payload) == []


def test_an_observed_http_status_quoted_from_a_tracked_record_is_accepted():
    """Must-pass sibling, and the exact regression this lint must avoid:
    `tests/test_stranding_lints.py` already has two must-pass fixtures
    (ids 029, 035) quoting 'endpoint, substance and observed HTTP status'
    and 'records its observed HTTP status' as evidence ALREADY recorded in
    a tracked artifact, not a live per-task obligation -- this lint must
    not turn those green fixtures red."""
    payload = {
        "tasks": [
            _basic_task(
                "023",
                successCriteria=(
                    "This task's entry appended to "
                    "`artifacts/reports/v0.16-operator-notifications.md` "
                    "carries endpoint, substance and observed HTTP status."
                ),
            ),
            _basic_task(
                "024",
                successCriteria=(
                    "The notification `POST http://100.71.162.65:8090/notify` "
                    "records its observed HTTP status."
                ),
            ),
        ]
    }
    assert pi.lint_proposal(payload) == []


# ---------------------------------------------------------------------------
# Cross-shape: a generic must-pass sibling naming a past sha, an existing
# tracked artifact, and "the code sha this task lands" together, per this
# task's own successCriteria wording.
# ---------------------------------------------------------------------------


def test_the_generic_must_pass_trio_together_is_accepted():
    payload = {
        "tasks": [
            _basic_task(
                "025",
                successCriteria=(
                    "At commit `8db9739`, the record cites the code sha "
                    "this task lands, and `docs/sweep-records.md`, an "
                    "existing tracked artifact, already documents the "
                    "guard's report-only tail allowance."
                ),
            )
        ]
    }
    assert pi.lint_proposal(payload) == []


# ---------------------------------------------------------------------------
# Ingestion-level test: a proposal carrying a banned shape is rejected
# through the existing plan re-ask ladder.
# ---------------------------------------------------------------------------


def test_ingestion_level_proposal_carrying_a_banned_shape_is_rejected(tmp_path):
    proposal = {
        "tasks": [
            _basic_task(
                "026",
                successCriteria=(
                    "This task's own commit lands the fix. "
                    + V014_RECOVERED_LAST_COMMIT_CLAUSE
                    + "."
                ),
            )
        ]
    }
    (tmp_path / "plan-proposal.json").write_text(__import__("json").dumps(proposal))

    calls = []

    def respond(attempt, prior_errors):
        calls.append((attempt, prior_errors))
        return '{"proposal_path": "plan-proposal.json", "summary": "x"}'

    result = pi.enforce_plan_ingestion(
        respond,
        max_retries=0,
        read_proposal=pi.file_reader(tmp_path),
    )
    assert result.ok is False
    assert result.agent_fault is True
    assert any(
        "026" in e and "structurally unsatisfiable" in e for e in result.errors
    ), result.errors


def test_plan_review_md_carries_the_six_shape_checklist_item():
    text = _flattened("src/ralphd/prompts/plan_review.md")
    for cue in (
        "Structurally unsatisfiable criteria",
        "last commit",
        "none newly filed",
        "commit that writes it",
        "git range required to be empty",
        "exactly once",
        "HTTP status",
    ):
        assert cue in text, cue
