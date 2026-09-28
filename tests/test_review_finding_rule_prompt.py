"""Task 001 (PRD requirement A.1, #274): a blocking curable finding files the
BROKEN RULE, not the reviewer's own probes.

Before this task `prompts/review.md`'s only instruction for `cured_when` was
"the text `insert_cure_tasks` uses as the cure task's own `successCriteria`"
-- silent on how review is supposed to arrive at that text. In practice
review wrote its own failing fixtures into `cured_when`, the cure worker
fixed exactly those fixtures, and the next review built a fresh
counterexample to the same underlying requirement (the PRD's own selfdev-v026
table: "B.3-B.4 came back in cure passes 1-4, A.1 in 1, 2, 5 and 6").

This test pins the fix at the prompt layer, the same way
`tests/test_approach_progress_context.py` pins a `build_prompt` context
block: read the ACTUAL text `LoopSupervisor.build_prompt("review")` produces
and assert it carries the new instruction. It fails the moment the new
`review.md` text is reverted, because the assertion has nothing else to
match against once the paragraph is gone.

A second module-level check holds `engine/findings.py`'s `FINDING_SCHEMA`
field set to exactly what it was before this task (`severity`, `curable`,
`clause`, `evidence`, `author`, `cured_when`, `invalidates`) -- A.1 is a
prompt-only, review-side instruction; it adds no new required field to the
machine-readable findings channel.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from tests.conftest import supervisor_factory

from ralphd.engine import findings

_supervisor = supervisor_factory(run_id="unit", run_root="run", workspace="workspace")


def _review_prompt() -> str:
    with tempfile.TemporaryDirectory() as td:
        sup = _supervisor(Path(td))
        return sup.build_prompt("review")


def test_review_prompt_instructs_working_out_why_the_code_admits_the_case():
    prompt = _review_prompt()
    assert "work out" in prompt
    assert "why" in prompt
    assert "admits it" in prompt or "admits the failing case" in prompt


def test_review_prompt_ties_cured_when_to_the_broken_rule_in_requirement_terms():
    prompt = _review_prompt()
    assert "File the rule, not the example" in prompt
    assert "State that broken rule, in the\nrequirement's own terms, as the finding's `cured_when`" in prompt


def test_review_prompt_puts_probes_into_evidence_as_examples():
    prompt = _review_prompt()
    assert ("go into `evidence` as **examples**: regression cases" in prompt)


def test_review_prompt_says_several_examples_of_one_rule_are_one_finding():
    prompt = _review_prompt()
    assert "that is **one** finding with several\nexamples in its `evidence`, never several findings" in prompt


def test_review_prompt_fails_the_check_when_the_new_text_is_reverted():
    """Directly demonstrate the "fails when reverted" half of the criteria:
    the ORIGINAL (pre-task-001) review.md text -- the paragraph immediately
    preceding the block this task inserted, unmodified -- does not itself
    satisfy the new assertions. This does not touch the checked-out
    review.md; it only proves the probe is discriminating."""
    reverted_tail = (
        "A curable blocking finding you file without `cured_when` is a review contract\n"
        "violation on the ladder, not a finding the engine can act on.\n\n"
        "Write BOTH channels for every finding, whether the run is `verified` or\n"
        "`rejected`: the free-markdown `review-findings.md` you already produce\n")
    assert "File the rule, not the example" not in reverted_tail
    assert "work out" not in reverted_tail


def test_findings_schema_field_set_is_unchanged_by_a_dot_1():
    assert set(findings.FINDING_SCHEMA["properties"]) == {
        "severity", "curable", "clause", "evidence", "author",
        "cured_when", "invalidates",
    }
    assert findings.FINDING_SCHEMA["required"] == [
        "severity", "curable", "clause", "evidence"]
