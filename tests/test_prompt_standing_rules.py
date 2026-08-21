"""The notes file must open with a `## Standing rules` block that survives
every worker rewrite (#39).

A job's conventions -- commit title format, branch and push target, commit
identity, credential file paths, the measured test commands, the PRD's
prohibitions -- are discovered once, by the planner, which is the only role
required to read the PRD. Workers get a fresh context each iteration and rewrite
the notes file as they go. Unless the conventions live in a block the worker is
told to carry over verbatim, they exist nowhere durable and the first iteration
that commits invents its own.

So:

  planning.md must instruct opening the notes with a `## Standing rules` block
  and must name what goes in it (commit title format, branch/push target, commit
  identity, credential paths, the measured test commands, prohibitions).

  worker.md must instruct preserving that block verbatim across notes rewrites
  and re-reading it before the first commit of the iteration.

Each element gets its own test, so a later refactor that drops any one of them
fails a *named* test rather than quietly widening the gap.
"""

from __future__ import annotations

import re
from pathlib import Path

PROMPTS_DIR = Path(__file__).resolve().parents[1] / "src" / "ralphd" / "prompts"

# the literal heading the planner writes and the worker preserves; both prompts
# must agree on it byte-for-byte or the worker cannot recognize the block
HEADING = "## Standing rules"


def _prompt_text(name: str) -> str:
    path = PROMPTS_DIR / f"{name}.md"
    assert path.is_file(), f"missing prompt file {path}"
    return path.read_text()


def _assert_all(name: str, rule: str, patterns: list[str]) -> None:
    text = _prompt_text(name)
    missing = [p for p in patterns
               if not re.search(p, text, re.IGNORECASE | re.DOTALL)]
    assert not missing, (
        f"{name}.md is missing the '{rule}' standing-rules instruction; "
        f"no match for: {missing}")


# ---- the literal heading, shared by both prompts ---------------------------

def test_planning_names_the_literal_standing_rules_heading():
    assert HEADING in _prompt_text("planning"), (
        f"planning.md must tell the planner to write the literal {HEADING!r} "
        "heading, otherwise the worker cannot recognize the block")


def test_worker_names_the_literal_standing_rules_heading():
    assert HEADING in _prompt_text("worker"), (
        f"worker.md must refer to the literal {HEADING!r} heading it has to "
        "preserve")


# ---- planning.md: open the notes with the block, and say what is in it -----

def test_planning_opens_the_notes_with_the_standing_rules_block():
    _assert_all("planning", "open the notes with the block", [
        r"Open the\s*\n?\s*file with a `## Standing rules` block",
    ])


def test_planning_lists_the_git_conventions_in_the_block():
    _assert_all("planning", "commit format, push target, identity", [
        r"commit title format",
        r"branch and push target",
        r"commit identity",
    ])


def test_planning_lists_credential_paths_in_the_block():
    _assert_all("planning", "credential paths", [
        r"credential file paths",
    ])


def test_planning_lists_the_measured_test_commands_in_the_block():
    _assert_all("planning", "measured test commands", [
        r"test\s*\n?\s*commands measured in step 2b",
    ])


def test_planning_lists_the_prds_prohibitions_in_the_block():
    _assert_all("planning", "prohibitions", [
        r"prohibition the PRD states",
    ])


def test_planning_says_the_block_must_survive_the_workers_rewrites():
    _assert_all("planning", "the block outlives worker rewrites", [
        r"rewrite the rest of the notes every iteration",
        r"must survive",
    ])


# ---- worker.md: preserve it verbatim, re-read it before the first commit ---

def test_worker_preserves_the_block_verbatim_across_notes_rewrites():
    _assert_all("worker", "preserve verbatim across rewrites", [
        r"Preserve the\s*\n?\s*notes' `## Standing rules` block verbatim",
        r"through every rewrite",
    ])


def test_worker_says_why_dropping_the_block_is_unrecoverable():
    _assert_all("worker", "why verbatim matters", [
        r"only place the job's conventions survive between iterations",
    ])


def test_worker_rereads_the_block_before_its_first_commit():
    _assert_all("worker", "re-read before the first commit", [
        r"Re-read the notes' `## Standing rules` block before your first commit",
    ])


def test_worker_names_what_the_block_governs_at_commit_time():
    _assert_all("worker", "what the block governs", [
        r"commit title format, branch and push target",
        r"commit identity, credential paths, prohibitions",
    ])
