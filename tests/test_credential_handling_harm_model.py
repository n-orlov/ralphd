"""worker.md's `## Credential handling` section must reflect the mechanical
scrub that actually exists now, not the earlier claim that a tool call's
arguments/stdout are recorded verbatim to disk (task 032, requirement I.4/I.5).

That claim went stale the moment `src/ralphd/engine/runner.py` started
scrubbing every line of a `pi` subprocess's stdout against known secret
values before it ever reaches the transcript on disk (task 060's mechanical
redaction). Leaving the old "recorded verbatim ... (host-visible,
permanent)" sentence in place contradicts `runner.py`'s own behaviour, so it
is deleted outright.

In its place the section must:
  1. name the commit-message and staged-diff sink explicitly (the git
     commit-msg/pre-commit hooks installed by task 031 scan those, a
     *different* sink than the transcript runner.py scrubs),
  2. state plainly that an environment dump (`declare -x`, `env`,
     `printenv`, `docker inspect`) is never acceptable as committed
     diagnostic evidence, and ask for variable names and the shape of the
     failure instead,
  3. say that the section documents the mechanical rule rather than being
     the bound itself -- the scrub/scan in the engine's own code is what
     actually stops a leak; this prose only describes it.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

PROMPTS_DIR = Path(__file__).resolve().parents[1] / "src" / "ralphd" / "prompts"
WORKER = PROMPTS_DIR / "worker.md"


def _worker_text() -> str:
    assert WORKER.is_file(), f"missing prompt file {WORKER}"
    return WORKER.read_text()


def _credential_handling_section() -> str:
    text = _worker_text()
    m = re.search(r"^##\s+Credential handling\s*$(.*?)(?=^##\s|\Z)",
                  text, re.MULTILINE | re.DOTALL)
    assert m, "worker.md has no '## Credential handling' section"
    return m.group(1)


def _flat(section: str) -> str:
    """Collapse markdown line-wrap whitespace so a multi-word phrase can be
    matched as a single substring regardless of where the source wrapped."""
    return re.sub(r"\s+", " ", section).strip()


def test_stale_verbatim_transcript_claim_is_deleted():
    """The old claim -- that a tool call's arguments/stdout are recorded
    verbatim to the transcript, making that the whole reason not to leak --
    must be gone from this section; runner.py's own scrub contradicts it."""
    section = _credential_handling_section()
    assert "recorded verbatim" not in section, (
        "worker.md's Credential handling section still makes the stale "
        "'recorded verbatim in this run's iteration transcript' claim, "
        "which src/ralphd/engine/runner.py's line-by-line scrub (ahead of "
        "disk) contradicts")


def test_names_the_commit_message_and_staged_diff_sink():
    section = _credential_handling_section()
    low = section.lower()
    assert "commit message" in low or "commit-message" in low, (
        "Credential handling section must name the commit-message sink")
    assert "staged diff" in low, (
        "Credential handling section must name the staged-diff sink")


def test_forbids_environment_dumps_as_committed_diagnostic_evidence():
    section = _credential_handling_section()
    low = _flat(section).lower()
    for tool in ("declare -x", "env", "printenv", "docker inspect"):
        assert tool in low, (
            f"Credential handling section must name {tool!r} among the "
            "forbidden environment-dump commands")
    assert "committed diagnostic evidence" in low, (
        "Credential handling section must say an environment dump is never "
        "acceptable as committed diagnostic evidence")
    assert "variable" in low and ("shape of the failure" in low
                                   or "shape of failure" in low), (
        "Credential handling section must ask for variable names and the "
        "shape of the failure instead of a dump")


def test_documents_the_mechanical_rule_rather_than_being_the_bound():
    section = _credential_handling_section()
    low = _flat(section).lower()
    assert "documents the mechanical rule" in low, (
        "Credential handling section must say it documents the mechanical "
        "rule rather than being the bound itself")
    assert "not itself the bound" in low or "not the bound" in low, (
        "Credential handling section must disclaim being the bound itself")
    assert "the harness scrubs" in low, (
        "Credential handling section must name the harness's own stdout "
        "scrub as the actual (mechanical) bound")


@pytest.mark.parametrize("marker", ["print", "cat", "echo"])
def test_still_forbids_dumping_credential_file_contents(marker):
    """The pre-existing dump prohibition (task 049) must survive the
    rewrite."""
    section = _credential_handling_section()
    assert marker in section.lower()
