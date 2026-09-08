"""A GitHub closing keyword (`closes #N`, `fixes #N`, `resolved #N`, ...) in a
commit message must name an issue the closure record actually lists as
closed -- otherwise GitHub closes it on push regardless of what the author
meant, which is exactly what happened once already (#58).

Commit `9eaee65` is the real, already-pushed proof this fault class exists:
its body explains a *future* step in the sentence "a later wave that closes
#50 must move it in both places" -- prose about work still to come, not an
intended closure -- and GitHub closed #50 on push anyway, even though the
closure record (`artifacts/reports/issue-closure.md`, parsed by
`closed_issues_v07()`/`left_open_issues_v07()` in `tests/records/test_roadmap.py`) lists
#50 as left open deliberately. `tests/test_prompt_git_policy.py` already
guards the *prompt* against writing that sentence again; this module guards a
*check* that would have caught it before push, using that same commit's real
body as a fixture -- not a synthesized one.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

from tests.records.test_roadmap import closed_issues_v07 as closed_issues

REPO_ROOT = Path(__file__).resolve().parents[1]

# A GitHub closing keyword directly followed by `#<number>`, case-insensitive,
# matching GitHub's own recognised verb forms (close/closes/closed,
# fix/fixes/fixed, resolve/resolves/resolved). Deliberately excludes bare
# `#N` / `GH-N` references and "resolves this" with no number -- those are the
# substitute wording worker.md teaches (task 002) and must never be flagged.
CLOSING_KEYWORD_RE = re.compile(
    r"\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\s+#(\d+)\b", re.IGNORECASE
)

# The real, already-pushed commit whose body accidentally closed #50.
OFFENDING_SHA = "9eaee65"
# The line-wrapped body reads "...and a\nlater wave that closes #50 must move
# it in both places." -- match on the fragment that survives the wrap.
OFFENDING_SENTENCE = "later wave that closes #50 must move it in both places"


def commit_body(sha: str) -> str:
    """The full commit message body, read locally from this repo's own git
    history -- no network, no GitHub API, no ralphd env vars."""
    result = subprocess.run(
        ["git", "show", "-s", "--format=%B", sha],
        cwd=REPO_ROOT, capture_output=True, text=True, check=True,
    )
    return result.stdout


def closing_keyword_matches(message: str) -> list[int]:
    """Every issue number a closing keyword in `message` names."""
    return [int(n) for n in CLOSING_KEYWORD_RE.findall(message)]


def unauthorized_closes(message: str, closed: set[int]) -> list[int]:
    """Issue numbers a closing keyword in `message` names that `closed` does
    not list as actually closed -- the fault class #58 is about."""
    return [n for n in closing_keyword_matches(message) if n not in closed]


def test_the_real_9eaee65_body_still_contains_the_offending_sentence():
    """Fixture assumption: the real commit body this check is built to catch
    has not been rewritten out from under this test."""
    assert OFFENDING_SENTENCE in commit_body(OFFENDING_SHA)


def test_the_check_catches_the_real_9eaee65_body():
    """The fixture that proves this check catches a real, already-pushed
    violation: #50 is left open deliberately in the closure record, but
    9eaee65's body names it with the closing keyword "closes"."""
    body = commit_body(OFFENDING_SHA)
    violations = unauthorized_closes(body, closed_issues())
    assert violations == [50], (
        f"the check must catch #50 in 9eaee65's body, got {violations}"
    )


def test_a_closing_keyword_naming_an_actually_closed_issue_is_not_flagged():
    closed = closed_issues()
    some_closed = next(iter(closed))
    message = f"task 999: tidy up\n\nThis closes #{some_closed} for real."
    assert unauthorized_closes(message, closed) == []


def test_the_substitute_wording_worker_md_teaches_is_never_flagged():
    """Bare `#N` / `GH-N` references and "resolves this" with no number --
    the substitute wording task 002 put in worker.md -- must not trip the
    check, or the substitute would be worthless."""
    message = "task 999: partial fix\n\nSee #50 and GH-51; resolves this later."
    assert unauthorized_closes(message, closed_issues()) == []


def test_the_check_is_case_insensitive_and_covers_the_named_verb_forms():
    closed = closed_issues()
    not_closed = next(n for n in range(1, 1000) if n not in closed)
    for verb in ("Closes", "CLOSED", "close", "Fix", "fixes", "Fixed",
                 "Resolve", "resolves", "RESOLVED"):
        message = f"{verb} #{not_closed}"
        assert unauthorized_closes(message, closed) == [not_closed], verb


def test_mutation_rewriting_the_offending_sentence_to_the_substitute_clears_it():
    """Mutation case: rewrite 9eaee65's real offending sentence to the
    substitute wording worker.md now prescribes (a bare `#N` reference plus
    "resolves this"), and confirm the check no longer flags it -- proving the
    positive result above is not a false positive baked into the regex, and
    that the substitute wording genuinely defuses the fault class."""
    body = commit_body(OFFENDING_SHA)
    mutated = body.replace(
        OFFENDING_SENTENCE,
        "a later wave that resolves this (#50) must move it in both places",
    )
    assert mutated != body, "fixture assumption: the sentence is still there to mutate"
    assert unauthorized_closes(mutated, closed_issues()) == []


def test_mutation_introducing_a_bad_closing_keyword_makes_the_check_fail():
    """Mutation case, the other direction: take a message with no violation
    and introduce one, and confirm the check now flags it -- proving the
    check is not vacuously silent."""
    closed = closed_issues()
    not_closed = next(n for n in range(1, 1000) if n not in closed)
    clean = "task 999: unrelated cleanup, no issue closed here"
    assert unauthorized_closes(clean, closed) == []
    dirty = clean + f"\n\nfixes #{not_closed}"
    assert unauthorized_closes(dirty, closed) == [not_closed]
