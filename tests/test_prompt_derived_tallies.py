"""Numbers in a run's prose must be derived, and the reviewer must re-derive
them (#40).

Two halves of the same failure mode:

  A worker writes a tally into the handoff notes ("52/55 done"). The next
  iteration has a fresh context, cannot tell the number from the state it
  summarizes, and copies it forward verbatim. Several iterations later the
  stale figure is typed into a report, a release note or an issue comment as
  fact. So worker.md must forbid writing any count it did not derive from
  `tasks.json` in that same iteration, and require recording how to recompute
  it instead.

  The reviewer must account per task id for every task that did not reach
  `completed` -- a status tally alone hides a dropped requirement. (Numbers in
  prose outside the spec are not evidence for review at all; that rule is
  pinned in tests/test_prompt_no_paperwork_rules.py.)

Each element gets its own named test, so dropping any one of them fails a test
that says which rule went missing.
"""

from __future__ import annotations

import re
from pathlib import Path

PROMPTS_DIR = Path(__file__).resolve().parents[1] / "src" / "ralphd" / "prompts"


def _prompt_text(name: str) -> str:
    path = PROMPTS_DIR / f"{name}.md"
    assert path.is_file(), f"missing prompt file {path}"
    return path.read_text()


def _assert_all(name: str, rule: str, patterns: list[str]) -> None:
    text = _prompt_text(name)
    missing = [p for p in patterns
               if not re.search(p, text, re.IGNORECASE | re.DOTALL)]
    assert not missing, (
        f"{name}.md is missing the '{rule}' rule; no match for: {missing}")


# ---- worker.md: no un-derived counts in the notes ---------------------------

def test_worker_forbids_writing_an_underived_count_into_the_notes():
    _assert_all("worker", "never write an un-derived count", [
        r"Never write a count into the\s*\n?\s*notes",
        r"did not derive from `tasks\.json`",
    ])


def test_worker_requires_the_count_to_come_from_this_same_iteration():
    _assert_all("worker", "derived in this same iteration", [
        r"in this same iteration",
    ])


def test_worker_requires_recording_how_to_recompute_the_count_instead():
    _assert_all("worker", "record how to recompute it instead", [
        r"Record how to recompute it\s*\n?\s*instead",
        r"name the source and the operation",
    ])


def test_worker_says_why_a_stale_tally_is_dangerous():
    _assert_all("worker", "why a stale tally is dangerous", [
        r"copies it forward",
        r"prints it in a deliverable as\s*\n?\s*fact",
    ])


# ---- review.md: per-task-id accounting for everything not completed --------

def test_review_accounts_per_task_id_for_every_non_completed_task():
    _assert_all("review", "account per task id", [
        r"Account, per task id, for every task whose final status is not\s*\n?\s*`completed`",
    ])


def test_review_names_the_non_completed_statuses_it_must_account_for():
    _assert_all("review", "which statuses need accounting", [
        r"`skipped`, `failed`, still `pending`",
    ])


def test_review_requires_naming_the_commits_that_delivered_the_scope():
    _assert_all("review", "delivered elsewhere or genuinely missing", [
        r"delivered\s*\n?\s*elsewhere \u2014 name the commits \u2014 or is genuinely missing",
    ])


def test_review_says_a_skipped_task_nobody_looked_at_is_not_shippable():
    _assert_all("review", "skipped is allowed, unexamined is not", [
        r"skipped task nobody looked at is\s*\n?\s*not",
        r"requirement coverage, not\s*\n?\s*task status, decides the verdict",
    ])
