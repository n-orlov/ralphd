"""worker.md's git policy must forbid a closing keyword for an issue the
commit does not close, and give a substitute wording for talking about an
issue you are not closing (#58, PRD requirement D).

The wave that just ended closed #50 by accident: a commit body wrote "a later
wave that closes #50 must move it in both places" while explaining a *future*
step, and GitHub closed #50 on push regardless. A bare prohibition is weak on
its own -- v0.7's #55 lesson -- because the agent still needs a way to write
about an issue it is not closing. So worker.md must state both the
prohibition and the substitute, and each is tested separately so dropping
either one fails a named test rather than a catch-all.
"""

from __future__ import annotations

import re
from pathlib import Path

WORKER_MD = (
    Path(__file__).resolve().parents[1] / "src" / "ralphd" / "prompts" / "worker.md"
)


def _text() -> str:
    assert WORKER_MD.is_file(), f"missing prompt file {WORKER_MD}"
    return WORKER_MD.read_text()


# the exact sentences the check below removes one at a time to prove it can fail
PROHIBITION_SENTENCE = (
    "**Never write a GitHub closing keyword followed by an issue number for an\n"
    "  issue the commit does not close.**"
)
SUBSTITUTE_SENTENCE = (
    "use the substitute wording instead: a bare issue reference with no closing\n"
    "  verb (`#50`, `GH-50`), or a phrase like \"a later wave that *resolves* this\" /\n"
    "  \"resolves this\"."
)


def test_worker_has_a_git_policy_section():
    text = _text()
    assert re.search(r"^##\s+Git policy", text, re.MULTILINE), (
        "worker.md has no '## Git policy' heading"
    )


def test_worker_states_the_closing_keyword_prohibition():
    text = _text()
    assert PROHIBITION_SENTENCE in text, (
        "worker.md's git policy must forbid a GitHub closing keyword + issue "
        "number for an issue the commit does not close"
    )
    # name the concrete keywords the prohibition covers, not just "keyword"
    for kw in (r"closes #N", r"fixes #N", r"resolved #N", r"closed #N"):
        assert kw in text, f"worker.md's git policy must name {kw!r}"


def test_worker_gives_the_substitute_wording():
    text = _text()
    assert SUBSTITUTE_SENTENCE in text, (
        "worker.md's git policy must give the substitute wording for writing "
        "about an issue you are not closing"
    )
    for substitute in (r"#50", r"GH-50", r"resolves this"):
        assert substitute in text, (
            f"worker.md's git policy must include the substitute {substitute!r}"
        )


def test_dropping_the_prohibition_sentence_fails_the_check():
    """Mutation case: strip the prohibition sentence and confirm the presence
    check above would no longer find it."""
    mutated = _text().replace(PROHIBITION_SENTENCE, "")
    assert PROHIBITION_SENTENCE not in mutated
    assert "issue the commit does not close" not in mutated


def test_dropping_the_substitute_sentence_fails_the_check():
    """Mutation case: strip the substitute-wording sentence and confirm the
    presence check above would no longer find it."""
    mutated = _text().replace(SUBSTITUTE_SENTENCE, "")
    assert SUBSTITUTE_SENTENCE not in mutated
    assert "a later wave that *resolves* this" not in mutated
