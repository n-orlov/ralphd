"""The notes file must open with a `## Standing rules` block that survives
every worker rewrite (#39), now by CONSTRUCTION rather than convention
(task 107, #60 pillar 3): the planner supplies the block's content once
through the planning contract's `standing_rules` field, the harness pins it,
and re-injects it into the notes file after every later iteration --
regardless of what that iteration's own notes.md write did.

A job's conventions -- commit title format, branch and push target, commit
identity, credential file paths, the measured test commands, the PRD's
prohibitions -- are discovered once, by the planner, which is the only role
required to read the PRD. Workers get a fresh context each iteration and rewrite
the notes file as they go; the harness's own reinjection (not the worker's own
discipline) is what keeps the block intact durably.

So:

  planning.md must instruct supplying a `## Standing rules` block through the
  planning contract's `standing_rules` field and must name what goes in it
  (commit title format, branch/push target, commit identity, credential
  paths, the measured test commands, prohibitions).

  worker.md must instruct re-reading that block before the first commit of
  the iteration and say the harness -- not the worker's own care -- is what
  keeps it intact.

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
    _assert_all("planning", "supply the block through the standing_rules contract field", [
        r"Supply the `## Standing rules` block as the `standing_rules` field",
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
        r"harness pins this text once\s*\n?\s*and re-injects it",
        r"cannot lose it either way",
    ])


# ---- worker.md: preserve it verbatim, re-read it before the first commit ---

def test_worker_preserves_the_block_verbatim_across_notes_rewrites():
    _assert_all("worker", "harness re-injects it verbatim across rewrites", [
        r"harness-pinned:",
        r"re-injects it verbatim after every iteration",
    ])


def test_worker_says_why_dropping_the_block_is_unrecoverable():
    _assert_all("worker", "why the block can never be lost", [
        r"restored before\s*\n?\s*the next iteration reads the file either way",
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
