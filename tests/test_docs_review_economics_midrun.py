"""Task 016 (#114): pin the mid-run adjudication and criteria-amendment
sections added to `docs/review-economics.md`, and the five-type list
agreement with `docs/petition-format.md`.

No test module referenced either doc path at the launch sha
(`git grep -l 'review-economics\\|petition-format' tests` was empty), so a
later deletion of these load-bearing sentences would go unnoticed. Each test
here greps the *specific claim* task 016 added, not just a heading, so a
future edit that keeps the heading but guts the content still fails.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
REVIEW_ECONOMICS = REPO_ROOT / "docs" / "review-economics.md"
PETITION_FORMAT = REPO_ROOT / "docs" / "petition-format.md"


def _section(text: str, heading: str) -> str:
    """Return the text of the ``## heading`` section, up to the next ``##``."""
    pattern = re.compile(
        rf"^##\s+{re.escape(heading)}\s*$(.*?)(?=^##\s+|\Z)",
        re.MULTILINE | re.DOTALL,
    )
    match = pattern.search(text)
    assert match is not None, f"no '## {heading}' section found"
    return match.group(1)


def _review_economics_text() -> str:
    return REVIEW_ECONOMICS.read_text(encoding="utf-8")


def _petition_format_text() -> str:
    return PETITION_FORMAT.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Mid-run adjudication section
# ---------------------------------------------------------------------------


def test_midrun_adjudication_section_exists():
    text = _review_economics_text()
    section = _section(text, "Mid-run adjudication")
    assert section.strip()


def test_midrun_adjudication_names_declaration_at_moment_of_impossibility():
    section = _section(_review_economics_text(), "Mid-run adjudication")
    assert "does not have to wait for approach-end review" in section
    assert "unachievable" in section


def test_midrun_adjudication_names_the_review_hat_model_and_ordering():
    section = _section(_review_economics_text(), "Mid-run adjudication")
    assert "review-hat model" in section
    assert "never the worker's own model" in section
    assert "before the\naffected task's next validation attempt" in section


def test_midrun_adjudication_names_confirmed_and_refuted_effects():
    section = _section(_review_economics_text(), "Mid-run adjudication")
    assert "**CONFIRMED**" in section
    assert "disclosed residual for the" in section
    assert "**REFUTED**" in section
    assert "states concretely what would satisfy the obligation" in section
    assert "keeps running rather than stopping" in section


def test_midrun_adjudication_names_petitioned_as_adjudicated_outcome():
    section = _section(_review_economics_text(), "Mid-run adjudication")
    assert "**`petitioned`**" in section
    assert "waypoint toward an adjudicated outcome, not a verdict in its own right" in section
    assert "accepted-with-residual" in section
    assert "becomes `failed` with the" in section


# ---------------------------------------------------------------------------
# Criteria amendment section
# ---------------------------------------------------------------------------


def test_criteria_amendment_section_exists():
    text = _review_economics_text()
    section = _section(text, "Criteria amendment")
    assert section.strip()


def test_criteria_amendment_is_judge_authored_only():
    section = _section(_review_economics_text(), "Criteria amendment")
    assert "authored by the\nadjudicating judge and by no one else" in section or (
        "authored by the" in section and "adjudicating judge and by no one else" in section
    )
    assert "not the worker who filed the" in section
    assert "not the planner who wrote the original text" in section


def test_criteria_amendment_is_scoped_to_one_task():
    section = _section(_review_economics_text(), "Criteria amendment")
    assert "for that **one** task" in section


def test_criteria_amendment_is_recorded_and_refingerprinted():
    section = _section(_review_economics_text(), "Criteria amendment")
    assert "`task.criteria_amended` event" in section
    assert "`criteriaFingerprint` is\nrecomputed" in section or (
        "`criteriaFingerprint`" in section and "recomputed" in section
    )
    assert "disclosed in the closing report" in section


def test_criteria_amendment_never_targets_prd_dod_other_task_or_config():
    section = _section(_review_economics_text(), "Criteria amendment")
    assert "never the PRD" in section
    assert "Definition-of-done" in section
    assert "never another task" in section
    assert "`/config`" in section


def test_criteria_amendment_names_refusal_as_a_blocking_finding():
    section = _section(_review_economics_text(), "Criteria amendment")
    assert "is refused and is itself a blocking finding" in section


# ---------------------------------------------------------------------------
# Second-instance citation of this PRD
# ---------------------------------------------------------------------------


def test_document_cites_the_v016_prd_as_a_second_instance():
    text = _review_economics_text()
    assert "**Second instance:**" in text
    assert "docs/prds/v0.16-a-petition-is-not-a-postmortem.md" in text


# ---------------------------------------------------------------------------
# The three pre-existing guarantees stay textually intact (no deletion)
# ---------------------------------------------------------------------------


def test_materiality_rubric_guarantee_text_is_intact():
    section = _section(_review_economics_text(), "Materiality rubric")
    assert "Review computes its verdict from **blocking** findings only" in section
    assert "**Residual** — everything else" in section


def test_once_per_obligation_guarantee_text_is_intact():
    section = _section(_review_economics_text(), "Petition protocol")
    assert "**Once per obligation.**" in section
    assert (
        "is itself a blocking finding, not a\nsecond attempt at the same claim"
        in section
    )


def test_late_cure_guarantee_text_is_intact():
    section = _section(_review_economics_text(), "Late-cure rule")
    assert "is **cured** by a" in section
    assert "is not grounds for\nrejecting this or any later approach" in section


# ---------------------------------------------------------------------------
# review-economics.md and petition-format.md agree on the type list
# ---------------------------------------------------------------------------


PETITION_TYPES = (
    "environmental",
    "temporal",
    "contradictory-clauses",
    "external",
    "self-referential",
)


def test_petition_format_table_has_exactly_the_five_types():
    text = _petition_format_text()
    for petition_type in PETITION_TYPES:
        assert f"`{petition_type}`" in text, petition_type


def test_review_economics_type_list_matches_petition_format_five_types():
    text = _review_economics_text()
    inline_list = (
        "`environmental | temporal | contradictory-clauses | external | "
        "self-referential`"
    )
    assert inline_list in text
    # No stray reference to a stale four-type count anywhere in the doc.
    assert "the four types" not in text
    assert "the five types" in text
