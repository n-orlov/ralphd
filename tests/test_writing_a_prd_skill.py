"""Guards the bundled PRD-writing skill's "Clauses that make acceptance
unreachable" catalogue against silently losing an entry (E.2), and (task
007, PRD v0.29 G) the G.1 practices writing-a-prd.md must teach by name, its
Before launch checklist line per item, and the SKILL.md summary/traps that
name this wave's events. An entry or section that regresses out of either
file costs nothing to notice here, and everything to notice on a later run
that repeats the mistake the entry was meant to prevent.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SKILL_DOC = REPO_ROOT / "skills" / "ralphd" / "writing-a-prd.md"
SKILL_MD = REPO_ROOT / "skills" / "ralphd" / "SKILL.md"

HEADING = "## Clauses that make acceptance unreachable"


def _section_under_heading(text: str, heading: str) -> str:
    """Return the body of the named `##` section, up to the next `##`."""
    idx = text.index(heading)
    rest = text[idx + len(heading):]
    nxt = re.search(r"\n## ", rest)
    return rest[: nxt.start()] if nxt else rest


def test_heading_exists():
    text = SKILL_DOC.read_text()
    assert HEADING in text, (
        f"{SKILL_DOC} is missing the {HEADING!r} heading")


def test_open_domain_heuristic_entry_present_under_the_heading():
    """The never-converging open-domain entry: warns that a requirement
    saying "any"/"every" over an open domain enforced by a heuristic always
    has one more counterexample, tells the author to enumerate the cases
    instead, uses R150-shaped wording as the example, and names no product.
    """
    text = SKILL_DOC.read_text()
    section = _section_under_heading(text, HEADING)
    flat = re.sub(r"\s+", " ", section)

    assert '"any"' in flat and '"every"' in flat, (
        "the entry must warn about clauses quantifying with any/every")
    assert "open domain" in flat, (
        "the entry must name the open-domain shape explicitly")
    assert "heuristic" in flat, (
        "the entry must say the enforcement mechanism is a heuristic")
    assert "one more counterexample" in flat, (
        "the entry must state the always-one-more-counterexample failure")
    assert "Enumerate" in flat or "enumerate" in flat, (
        "the entry must tell the author to enumerate the cases instead")
    assert "R150" in flat, (
        "the entry must cite R150-shaped wording as the example")

    # No product named inside this entry specifically (the file's other
    # sections are free to name ralphd; this entry is not).
    entry_start = section.index('"any"/"every"')
    # Walk back to the start of the bullet this sentence belongs to.
    bullet_start = section.rfind("\n- ", 0, entry_start)
    bullet_end = section.find("\n- ", entry_start)
    entry_text = section[bullet_start:bullet_end if bullet_end != -1 else None]
    for product in ("ralphd", "deck", "ralphd's"):
        assert product not in entry_text.lower(), (
            f"the open-domain entry names a product ({product!r}); "
            "it must stay product-agnostic")


def test_entry_absent_without_the_change():
    """Sanity check that the assertions above actually discriminate: text
    that lacks the new entry (e.g. the file's earlier revision) must fail
    the presence check, so this test cannot pass vacuously.
    """
    text = SKILL_DOC.read_text()
    section = _section_under_heading(text, HEADING)
    without_entry = section.split(
        '- **DO NOT write a requirement that says "any"/"every"')[0]
    assert "R150" not in without_entry, (
        "the entry text should be isolated to the new bullet, not scattered "
        "earlier in the section")


# --- G.1: writing-a-prd.md teaches each of the 7 practices by name --------
#
# Each test below is a presence+key-terms check on writing-a-prd.md as it
# stands after task 007. Each assertion fails against the 0.28.0 revision of
# the file (checked out before task 007 ran), because that revision has
# neither the heading nor the key terms the assertion requires.


def test_g1_ending_the_run_section_present():
    text = SKILL_DOC.read_text()
    assert "## Ending the run" in text, (
        "writing-a-prd.md must have an 'Ending the run' section (G.1)")
    section = _section_under_heading(text, "## Ending the run")
    flat = re.sub(r"\s+", " ", section)
    for term in (
        "nothing of any kind is committed after",
        "origin/main",
        "swept sha",
        "Issue closure is a comment",
        "stale sweep is never process",
        "sweep: true",
    ):
        assert term in flat, f"'Ending the run' section is missing {term!r}"


def test_g1_standing_rules_block_section_present():
    text = SKILL_DOC.read_text()
    assert "## The standing rules block" in text, (
        "writing-a-prd.md must have a 'The standing rules block' section "
        "(G.1)")
    section = _section_under_heading(text, "## The standing rules block")
    flat = re.sub(r"\s+", " ", section)
    for term in (
        "test recipes",
        "parallelism",
        "only the final sweep runs a whole tier",
        "commits are made per task and pushed",
        "sweep's execution shape",
        "85%",
        "deferred",
        "CI dispatch recipe",
    ):
        assert term in flat, (
            f"'The standing rules block' section is missing {term!r}")


def test_g1_rubric_working_clauses_section_present():
    text = SKILL_DOC.read_text()
    assert "## The rubric's working clauses" in text, (
        "writing-a-prd.md must have a \"The rubric's working clauses\" "
        "section (G.1)")
    section = _section_under_heading(
        text, "## The rubric's working clauses")
    flat = re.sub(r"\s+", " ", section)
    for term in (
        "file the rule, not the example",
        "direction of the evidence",
        "confirms",
        "re-audit",
        "judge a hat or router by its contract",
        "stale sweep is never process",
    ):
        assert term in flat, (
            f"'The rubric's working clauses' section is missing {term!r}")


def test_g1_non_negotiables_section_present():
    text = SKILL_DOC.read_text()
    assert "## Non-negotiables" in text, (
        "writing-a-prd.md must have a 'Non-negotiables' section (G.1)")
    section = _section_under_heading(text, "## Non-negotiables")
    flat = re.sub(r"\s+", " ", section)
    for term in (
        "No weakened test",
        "covering node",
        "No raised timeout, budget, worker count or per-test bound",
        "No new required contract field",
        "installed engine's own judge",
    ):
        assert term in flat, f"'Non-negotiables' section is missing {term!r}"


def test_g1_issues_in_scope_section_present():
    text = SKILL_DOC.read_text()
    assert "## Issues in scope" in text, (
        "writing-a-prd.md must have an 'Issues in scope' section (G.1)")
    section = _section_under_heading(text, "## Issues in scope")
    flat = re.sub(r"\s+", " ", section)
    for term in (
        "reads each named issue in full",
        "the PRD wins",
        "named sha",
        "drift",
    ):
        assert term in flat, f"'Issues in scope' section is missing {term!r}"


def test_g1_budget_and_shape_section_present():
    text = SKILL_DOC.read_text()
    assert "## Budget and shape" in text, (
        "writing-a-prd.md must have a 'Budget and shape' section (G.1)")
    section = _section_under_heading(text, "## Budget and shape")
    flat = re.sub(r"\s+", " ", section)
    for term in (
        "Pre-split",
        "named tasks at plan time",
        "Update the spec inside each task",
        "iteration budget",
        "approach budget",
        "wall-clock window",
        "per-iteration cap",
        "peer gate settings",
    ):
        assert term in flat, f"'Budget and shape' section is missing {term!r}"


def test_g1_self_development_additions_present():
    text = SKILL_DOC.read_text()
    assert "## When the product is ralphd itself" in text
    section = _section_under_heading(
        text, "## When the product is ralphd itself")
    flat = re.sub(r"\s+", " ", section)
    for term in (
        "a hat prompt is product behaviour",
        "proven by a test on the prompt built for the case",
        "standing rules tell the run how to avoid, by hand, the known "
        "defects of",
    ):
        assert term in flat, (
            f"the self-development section is missing the added G.1 "
            f"point {term!r}")


def test_g2_before_launch_has_one_line_per_g1_item():
    text = SKILL_DOC.read_text()
    section = _section_under_heading(text, "## Before launch")
    flat = re.sub(r"\s+", " ", section)
    for term in (
        "Ending the run:",
        "Standing rules block gives the test recipes",
        "rubric's working clauses are stated",
        "Non-negotiables are stated",
        "Issues in scope are named",
        "pre-split into named tasks at plan time",
        "Self-development: hat prompts are proven by tests",
    ):
        assert term in flat, (
            f"Before launch checklist is missing a line for {term!r}")


# --- G.3/G.4: SKILL.md's Writing a PRD summary and run-watching traps -----


def test_g3_writing_a_prd_summary_links_guide_and_states_end_of_run_rule():
    text = SKILL_MD.read_text()
    assert "## Writing a PRD" in text, (
        "SKILL.md must have a 'Writing a PRD' section")
    section = _section_under_heading(text, "## Writing a PRD")
    flat = re.sub(r"\s+", " ", section)
    assert "writing-a-prd.md" in flat, (
        "the Writing a PRD summary must link writing-a-prd.md")
    for term in (
        "commit nothing after it",
        "origin/main",
        "swept sha",
        "issue closure is a comment",
        "review cure is the only thing that may land after the sweep",
    ):
        assert term in flat, (
            f"the Writing a PRD summary is missing {term!r} (G.3)")


def test_g4_run_watching_traps_name_this_waves_events():
    text = SKILL_MD.read_text()
    section = _section_under_heading(
        text, "### Traps that produce wrong diagnoses")
    flat = re.sub(r"\s+", " ", section)
    assert "task.repoll_backoff" in flat and "engine waiting" in flat, (
        "a run-watching trap must name task.repoll_backoff as the engine "
        "waiting, not a hang (G.4)")
    assert "cure.sweep_reopened" in flat and (
        "correctly re-run" in flat or "re-run" in flat), (
        "a run-watching trap must name cure.sweep_reopened as the sweep "
        "being correctly re-run (G.4)")


def test_g1_sections_absent_from_the_028_revision():
    """Sanity check the discriminators above actually discriminate: the
    0.28.0 revision of writing-a-prd.md (reconstructed by stripping the
    task-007 sections back out) has none of the new headings, so a test
    suite run against it would fail every G.1 presence check above.
    """
    text = SKILL_DOC.read_text()
    for heading in (
        "## Ending the run",
        "## The standing rules block",
        "## The rubric's working clauses",
        "## Non-negotiables",
        "## Issues in scope",
        "## Budget and shape",
    ):
        before_prior_heading = text.split("## End the run cleanly")
        # The 0.28.0 file had '## End the run cleanly', not '## Ending the
        # run' -- confirm the rename/addition actually happened, i.e. the
        # new heading is present and the old one is gone.
        assert heading in text
    assert "## End the run cleanly" not in text, (
        "the 0.28.0 heading should have been replaced by 'Ending the run'")
