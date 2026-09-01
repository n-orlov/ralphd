"""worker.md must document the structured impossibility petition (task 004
requirement A.1, #114) exactly as the worker contract accepts it: the named
obligation, the five petition types, the evidence, that the ENGINE -- never
the worker -- files and validates the entry into
`artifacts/reports/<wave>-petitions.md`, that a duplicate declaration is
refused, and that the run never grants itself a petition outcome or a
criteria amendment.

Each claim is asserted separately by name, so deleting any one of them fails
a *named* test here -- the same "grep for a specific claim string" discipline
`tests/test_prompt_last_attempt_handoff.py` already uses for task-verify.md's
last-attempt handoff step. The commit that adds this module greps these
exact claim strings out of `src/ralphd/prompts/worker.md`:

  * ``"the named obligation"`` shape: ``obligation``, ``requirement letter``,
    ``DoD item`` / ``Definition-of-done``, and the worker's own task's
    criterion clause.
  * the five literal type strings: ``environmental``, ``temporal``,
    ``contradictory-clauses``, ``external``, ``self-referential``.
  * ``evidence`` as a required, non-empty, checkable field.
  * ``"The ENGINE"`` files/validates the entry, and the worker never edits
    ``artifacts/reports/<wave>-petitions.md`` by hand.
  * ``"A duplicate declaration is refused"``.
  * ``"never grants itself a petition outcome or a criteria amendment"``.

A later edit that removes any of those strings from worker.md fails the
matching test below by name.
"""

from __future__ import annotations

import re
from pathlib import Path

PROMPTS_DIR = Path(__file__).resolve().parents[1] / "src" / "ralphd" / "prompts"

PETITION_TYPES = (
    "environmental", "temporal", "contradictory-clauses", "external",
    "self-referential",
)


def _worker_text() -> str:
    path = PROMPTS_DIR / "worker.md"
    assert path.is_file(), f"missing prompt file {path}"
    return path.read_text()


def _declaration_section() -> str:
    """The impossibility-petition section's text, from its own heading to
    the end of the file (it is the last section in worker.md)."""
    text = _worker_text()
    match = re.search(
        r"^## Declaring a structured impossibility petition.*$",
        text, re.MULTILINE)
    assert match, (
        "worker.md has no '## Declaring a structured impossibility "
        "petition...' heading documenting task 004's declaration channel")
    return text[match.start():]


def test_worker_md_names_the_obligation_shape():
    section = _declaration_section()
    for pattern in (r"\bobligation\b", r"requirement letter",
                    r"Definition-of-done item", r"own task's\s*\n?\s*`successCriteria`"):
        assert re.search(pattern, section, re.IGNORECASE), (
            f"worker.md's petition section is missing the obligation-shape "
            f"claim matching {pattern!r}")


def test_worker_md_lists_all_five_petition_types_by_name():
    section = _declaration_section()
    missing = [t for t in PETITION_TYPES if t not in section]
    assert not missing, (
        f"worker.md's petition section is missing petition type(s): {missing}")
    assert re.search(r"five petition types", section, re.IGNORECASE)


def test_worker_md_requires_non_empty_checkable_evidence():
    section = _declaration_section()
    assert re.search(r"non-empty\s*\n?\s*`evidence`", section, re.IGNORECASE)
    assert re.search(r"concrete, checkable\s*\n?\s*support", section,
                      re.IGNORECASE)


def test_worker_md_states_the_engine_files_and_validates_the_entry():
    section = _declaration_section()
    assert "The ENGINE" in section
    assert re.search(r"files and validates\s*\n?\s*the\s*\n?\s*petition entry",
                      section, re.IGNORECASE)
    assert re.search(
        r"never touch `artifacts/reports/<wave>-petitions\.md`\s*\n?\s*"
        r"yourself, by hand", section, re.IGNORECASE)


def test_worker_md_states_a_duplicate_declaration_is_refused():
    section = _declaration_section()
    assert re.search(r"A duplicate declaration is refused, not filed",
                      section)


def test_worker_md_states_the_run_never_grants_itself_a_petition_outcome_or_amendment():
    section = _declaration_section()
    assert re.search(
        r"never grants itself a petition outcome or a\s*\n?\s*criteria "
        r"amendment", section, re.IGNORECASE)
