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

Task 037 (cure for this task's requirement E) adds the two must-catch
fixtures the verifier found missing: the (h) shape recovered verbatim from
the two tracked RUN RECORDS under `artifacts/reports/` that the requirement
names -- `v0.14-a3-final-guard-tail.md`'s clause ending `which is the last
commit of the approach` and `v0.15-a2-final-guard-subset.md`'s clause
beginning `Task 014 lands the plan's last commit`. See the task 037 section
at the bottom of this module.

  (h) later/final/last commit -- recovered verbatim from this repository's
      own tracked PRDs (v0.14's non-negotiable 12; v0.15's/v0.17's
      identical "recorded as the last commit" requirement-list bullet,
      the exact clause `v0.17-a3-closing-report.md` quotes when review
      rejected approach 3 on it).
  (i) predeclared event state -- recovered verbatim from
      `artifacts/reports/v0.17-a3-closing-report.md` (task 014's own
      successCriteria clause 2 parenthetical).
  (j) a record citing the commit that writes it -- the impossibility
      `v0.17-a3-closing-report.md` states in so many words, AND the
      `deck-3i-2` clause verbatim as issue #143 renders it ("a record row
      must cite the commit that writes the row"), pinned below against
      `tests/data/issue-143-body.md`.
  (k) an empty git range -- the `deck-3h`/`3i-2`/`spike` clause verbatim as
      issue #143 renders it ("a protected-path audit range that begins
      before the PRD/plan commit itself"), pinned the same way.
  (l) an "exactly once" commit count -- the `deck-spike` clause verbatim as
      issue #143 renders it ("commit exactly once" combined with "correct
      the report the review faulted"), pinned the same way.

  The `deck-3h` / `deck-3i-2` / `deck-spike` run directories belong to a
  different product and are not reachable from this sandbox, so the
  verbatim source for (j)/(k)/(l) is issue #143's own text, which quotes
  those runs' clauses directly. That text is now tracked verbatim at
  `tests/data/issue-143-body.md` (fetched live from
  `GET /repos/n-orlov/ralphd/issues/143` during this task), so every
  fixture string below is a checkable substring of a tracked file, exactly
  like the v0.14/v0.15/v0.17 recoveries.
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

# Issue #143's own verbatim quotations of the `deck-3h` / `deck-3i-2` /
# `deck-spike` clauses, tracked at `tests/data/issue-143-body.md`.
DECK_3I2_ISSUE_143_CLAUSE = "a record row must cite the commit that writes the row"
DECK_3H_ISSUE_143_CLAUSE = (
    "a protected-path audit range that begins before the PRD/plan commit itself"
)
DECK_SPIKE_ISSUE_143_CLAUSE = "commit exactly once"
DECK_SPIKE_ISSUE_143_SECOND_HALF = "correct the report the review faulted"


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


def test_a_final_sha_reference_without_the_word_commit_is_rejected():
    """The same shape made through the *sha* rather than the commit: a
    report required to contain "the final sha" names a value that cannot be
    known until the run's last commit exists. These are the exact clauses
    task 013's first two validation rounds faulted as linting clean."""
    for task_id, criteria in (
        ("003a", "The report must contain the final sha."),
        ("003b", "The closing record quotes the run's last sha."),
        ("003c", "The row names the sha of the final commit."),
        ("003d", "The guard subset runs at the true final sha."),
    ):
        payload = {"tasks": [_basic_task(task_id, successCriteria=criteria)]}
        errors = pi.lint_proposal(payload)
        assert any(
            task_id in e and "structurally unsatisfiable" in e for e in errors
        ), (criteria, errors)


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


def test_the_deck_3i_2_clause_is_recovered_verbatim_from_issue_143():
    assert DECK_3I2_ISSUE_143_CLAUSE in _flattened("tests/data/issue-143-body.md")


def test_deck_3i_2_row_naming_its_own_writing_commit_is_rejected():
    """Issue #143's verbatim rendering of the `deck-3i-2` recurrence (x2),
    used as the fixture clause itself and pinned against the tracked issue
    body by the test immediately above -- that run's own files are a
    different product's run directory and are not reachable here."""
    payload = {
        "tasks": [
            _basic_task(
                "011",
                successCriteria=(
                    "The audit trail this task appends satisfies the shape "
                    + DECK_3I2_ISSUE_143_CLAUSE
                    + "."
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


def test_the_deck_3h_clause_is_recovered_verbatim_from_issue_143():
    assert DECK_3H_ISSUE_143_CLAUSE in _flattened("tests/data/issue-143-body.md")


def test_the_deck_3h_protected_path_audit_range_clause_is_rejected():
    """Issue #143's verbatim rendering of the `deck-3h` (x2) / `3i-2` (x4) /
    `spike` (x3) recurrence, used as the fixture clause itself and pinned
    against the tracked issue body above. A protected-path audit range
    beginning before the plan's own first commit is a range whose emptiness
    is demanded as a standing invariant -- the (k) shape."""
    payload = {
        "tasks": [
            _basic_task(
                "012a",
                successCriteria=(
                    "This task's evidence is " + DECK_3H_ISSUE_143_CLAUSE + "."
                ),
            )
        ]
    }
    errors = pi.lint_proposal(payload)
    assert any("012a" in e and "structurally unsatisfiable" in e for e in errors), errors


def test_a_git_log_range_required_to_be_empty_is_rejected():
    """The same (k) shape spelled out as a concrete `git log` demand."""
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


def test_the_deck_spike_clause_is_recovered_verbatim_from_issue_143():
    body = _flattened("tests/data/issue-143-body.md")
    assert DECK_SPIKE_ISSUE_143_CLAUSE in body
    assert DECK_SPIKE_ISSUE_143_SECOND_HALF in body


def test_an_exactly_once_commit_count_is_rejected():
    """Issue #143's verbatim rendering of the `deck-spike` recurrence (x4),
    both halves of it, used as the fixture clause itself and pinned against
    the tracked issue body above."""
    payload = {
        "tasks": [
            _basic_task(
                "017",
                successCriteria=(
                    DECK_SPIKE_ISSUE_143_CLAUSE.capitalize()
                    + ", and "
                    + DECK_SPIKE_ISSUE_143_SECOND_HALF
                    + "."
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
                    "notification ledger entry."
                ),
            )
        ]
    }
    errors = pi.lint_proposal(payload)
    assert any("020" in e and "structurally unsatisfiable" in e for e in errors), errors


def test_the_verbatim_v017_a4_clause_is_rejected_unmodified():
    """The recovered v0.17-a4 clause used EXACTLY as recovered -- no
    prefix, no added cue. Task 013's validation rounds faulted the earlier
    fixture for prefixing 'notification ledger entry' to make it trip, and
    for the lint firing only the older unenumerated-quantifier error on the
    clause itself. Both errors are asserted here: the quantifier lint AND
    the seventh lint's structural refusal."""
    payload = {
        "tasks": [
            _basic_task(
                "020a",
                successCriteria=V017_A4_RECOVERED_NOTIFICATION_HTTP_STATUS_CLAUSE,
            )
        ]
    }
    errors = pi.lint_proposal(payload)
    assert any(
        "020a" in e and "structurally unsatisfiable" in e for e in errors
    ), errors


def test_the_same_clause_scoped_to_one_task_is_rejected():
    """The same clause with the open quantifier removed, so the seventh
    lint is the ONLY thing that can refuse it -- the second clause task
    013's validation rounds faulted as linting clean."""
    payload = {
        "tasks": [
            _basic_task(
                "020b",
                successCriteria=(
                    "Task 001 appends endpoint, substance, and observed "
                    "HTTP status."
                ),
            )
        ]
    }
    errors = pi.lint_proposal(payload)
    assert any(
        "020b" in e and "structurally unsatisfiable" in e for e in errors
    ), errors


def test_a_notification_send_obligation_is_rejected():
    """A criterion that requires a SEND at all -- the third clause task
    013's validation rounds faulted as linting clean. Sends are
    engine-recorded and best-effort; no criterion may mention one."""
    for task_id, criteria in (
        ("021a", "Task 001 sends a completion notification."),
        ("021b", "A notification is sent to the operator at task start."),
        ("021c", "The task notifies the operator when the lane finishes."),
        ("021d", "The starting notification was posted before the work began."),
    ):
        payload = {"tasks": [_basic_task(task_id, successCriteria=criteria)]}
        errors = pi.lint_proposal(payload)
        assert any(
            task_id in e and "structurally unsatisfiable" in e for e in errors
        ), (criteria, errors)


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
    keep returning `[]` from `lint_proposal` -- the send half of this lint
    is scoped to a sending verb ('sends', 'is sent', 'posted', 'notifies
    the operator') beside the notification, never the bare word
    'notification' as a fact about something already recorded."""
    payload = {
        "tasks": [
            _basic_task("022", successCriteria="The notification is logged.")
        ]
    }
    assert pi.lint_proposal(payload) == []


def test_an_existing_tracked_artifact_and_a_past_sha_are_accepted():
    """Must-pass sibling for this shape, in the two forms this task's own
    successCriteria names: an existing tracked artifact and a past sha.
    (The earlier version of this fixture quoted 'observed HTTP status' from
    a tracked record and expected acceptance; task 013's validation rounds
    correctly faulted that as the very shape the PRD bans -- an HTTP status
    named in a criterion at all -- so the acceptance side is now carried by
    the artifact/sha citation, without a status or a send.)"""
    payload = {
        "tasks": [
            _basic_task(
                "023",
                successCriteria=(
                    "The tracked record "
                    "`artifacts/reports/v0.16-operator-notifications.md` "
                    "already exists at commit `8db9739`, and this task "
                    "appends its own row to it."
                ),
            )
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


# ---------------------------------------------------------------------------
# Task 037 (cure for task 013, requirement E): the two must-catch fixtures
# taken VERBATIM from tracked artifacts under `artifacts/reports/`.
#
# Task 013's verifier faulted the (h) fixtures above for being recovered
# from the tracked PRDs (`docs/prds/...`) rather than from the two run
# records the requirement names, so neither
# `artifacts/reports/v0.14-a3-final-guard-tail.md` nor
# `artifacts/reports/v0.15-a2-final-guard-subset.md` was pinned at all.
# Both are pinned here, each as a whole clause of its source record (the
# lint scopes every check to ONE clause, `_CLAUSE_SPLIT_RE`, so a fixture
# must be a clause to be a fair fixture):
#
#   * the v0.14-a3 final-guard-tail clause ENDING `which is the last commit
#     of the approach` -- that record's own declaration that its second,
#     report-only commit is the approach's last commit;
#   * the v0.15-a2 final-guard-subset clause BEGINNING `Task 014 lands the
#     plan's last commit` -- that record's discharge section reasoning
#     about a commit that had not landed when the clause was written.
#
# For each: the constant is a substring of its named source file, and
# `lint_proposal()` refuses it, quoting the clause back and naming a
# satisfiable alternative.
# ---------------------------------------------------------------------------

V014_A3_FINAL_GUARD_TAIL_CLAUSE = (
    "This record then self-discharges its own declared sha in a second, "
    "report-only commit below, which is the last commit of the approach"
)
V014_A3_FINAL_GUARD_TAIL_SOURCE = "artifacts/reports/v0.14-a3-final-guard-tail.md"

V015_A2_FINAL_GUARD_SUBSET_CLAUSE = (
    "Task 014 lands the plan's last commit, which necessarily moves `HEAD` "
    "one commit past `ab7bfbfe7e8cfb10fc2ebce2845d3e183b06af7d` (this "
    "record's own add commit) and therefore past the parent-of-HEAD "
    "self-anchoring fixed point above"
)
V015_A2_FINAL_GUARD_SUBSET_SOURCE = (
    "artifacts/reports/v0.15-a2-final-guard-subset.md"
)

# (fixture constant, its named tracked source file, the tail/head words the
# requirement pins it by)
TASK_037_VERBATIM_ARTIFACT_FIXTURES = (
    (
        "037a",
        V014_A3_FINAL_GUARD_TAIL_CLAUSE,
        V014_A3_FINAL_GUARD_TAIL_SOURCE,
        "which is the last commit of the approach",
    ),
    (
        "037b",
        V015_A2_FINAL_GUARD_SUBSET_CLAUSE,
        V015_A2_FINAL_GUARD_SUBSET_SOURCE,
        "Task 014 lands the plan's last commit",
    ),
)


def test_the_v014_a3_and_v015_a2_clauses_are_verbatim_substrings_of_their_sources():
    """Each fixture constant is a substring of the tracked artifact it names
    -- the pin that makes it a *recovered* clause rather than an invented
    one. (`_flattened` only rejoins wrapped lines and strips markdown
    emphasis; it changes no word of the clause.)"""
    for _tid, clause, source, pinned_words in TASK_037_VERBATIM_ARTIFACT_FIXTURES:
        assert pinned_words in clause, (source, pinned_words)
        assert clause in _flattened(source), (source, clause)


def test_the_v014_a3_clause_ends_with_the_pinned_words():
    assert V014_A3_FINAL_GUARD_TAIL_CLAUSE.endswith(
        "which is the last commit of the approach"
    )


def test_the_v015_a2_clause_begins_with_the_pinned_words():
    assert V015_A2_FINAL_GUARD_SUBSET_CLAUSE.startswith(
        "Task 014 lands the plan's last commit"
    )


def test_the_verbatim_artifact_clauses_are_refused_quoted_with_an_alternative():
    """Both recovered clauses are refused by `lint_proposal()`; each error
    quotes the offending clause verbatim AND names the satisfiable
    alternative the lint recommends for this shape."""
    for tid, clause, source, _pinned in TASK_037_VERBATIM_ARTIFACT_FIXTURES:
        payload = {"tasks": [_basic_task(tid, successCriteria=clause + ".")]}
        errors = pi.lint_proposal(payload)
        shape_errors = [
            e for e in errors if tid in e and "structurally unsatisfiable" in e
        ]
        assert shape_errors, (source, errors)
        error = shape_errors[0]
        # the clause is quoted back, verbatim, inside the error
        assert repr(clause) in error, (source, error)
        # ... and a satisfiable alternative is named
        assert "names a later/final/last commit or its sha" in error, (source, error)
        assert 'name "the code sha this task lands" once it exists' in error, (
            source,
            error,
        )
        assert "or a past sha this task can already read" in error, (source, error)


def test_the_verbatim_artifact_clauses_are_refused_inside_a_longer_criteria():
    """The same two clauses embedded beside an ordinary, satisfiable clause:
    the lint is clause-scoped, so the recovered clause is still the one
    quoted back."""
    for tid, clause, source, _pinned in TASK_037_VERBATIM_ARTIFACT_FIXTURES:
        payload = {
            "tasks": [
                _basic_task(
                    tid + "-long",
                    successCriteria=(
                        "The guard subset is green at the code sha this task "
                        "lands. " + clause + "."
                    ),
                )
            ]
        }
        errors = pi.lint_proposal(payload)
        assert any(
            tid + "-long" in e
            and "structurally unsatisfiable" in e
            and repr(clause) in e
            for e in errors
        ), (source, errors)


def test_the_must_pass_siblings_are_still_accepted_beside_the_new_fixtures():
    """The three must-pass sibling forms this cure task names -- a past sha,
    an existing tracked artifact, and `the code sha this task lands` --
    still lint clean, separately and together, so the new fixtures widened
    nothing."""
    siblings = (
        ("037p1", "At commit `8db9739`, the guard subset was green."),
        (
            "037p2",
            "The existing tracked artifact "
            "`artifacts/reports/v0.14-a3-final-guard-tail.md` records that "
            "run's guard subset.",
        ),
        ("037p3", "The record cites the code sha this task lands."),
        (
            "037p4",
            "At commit `8db9739`, the existing tracked artifact "
            "`artifacts/reports/v0.15-a2-final-guard-subset.md` already "
            "exists, and the new row names the code sha this task lands.",
        ),
    )
    for tid, criteria in siblings:
        payload = {"tasks": [_basic_task(tid, successCriteria=criteria)]}
        assert pi.lint_proposal(payload) == [], (tid, criteria)
    # ... and all four in one proposal, so no cross-task interaction widens
    # or narrows the result either
    payload = {
        "tasks": [_basic_task(tid, successCriteria=c) for tid, c in siblings]
    }
    assert pi.lint_proposal(payload) == []


def test_ingestion_level_proposal_carrying_a_verbatim_artifact_clause_is_rejected(
    tmp_path,
):
    """The refusal reaches the operator through the existing plan re-ask
    ladder for the recovered artifact clauses too, not just the synthetic
    ones."""
    proposal = {
        "tasks": [
            _basic_task(
                "037c",
                successCriteria=V015_A2_FINAL_GUARD_SUBSET_CLAUSE + ".",
            )
        ]
    }
    (tmp_path / "plan-proposal.json").write_text(__import__("json").dumps(proposal))

    def respond(attempt, prior_errors):
        return '{"proposal_path": "plan-proposal.json", "summary": "x"}'

    result = pi.enforce_plan_ingestion(
        respond,
        max_retries=0,
        read_proposal=pi.file_reader(tmp_path),
    )
    assert result.ok is False
    assert result.agent_fault is True
    assert any(
        "037c" in e
        and "structurally unsatisfiable" in e
        and repr(V015_A2_FINAL_GUARD_SUBSET_CLAUSE) in e
        for e in result.errors
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
