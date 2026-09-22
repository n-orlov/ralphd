"""Task 021 (PRD requirement K.1, #169): `planning.md` instructs the planner
to emit one probe-audit task per requirement group it plans -- a task whose
`successCriteria` states that a probe exists which fails if the behaviour is
absent, and that the probe is named in the commit message -- and
`plan_review.md` carries a matching objection ground for a plan that has
none. Both additions are pinned here by their own literal phrasing, the same
way `tests/test_review_findings_prompt_shape.py` pins review.md's structured
findings section: a future edit that waters either one down or removes it
fails loudly here, not only by diffing history.

The engine-side lint these two prompt sections describe is exercised by
`tests/test_plan_ingestion_probe_audit_lint.py` (the flagged and clean
proposals against `plan_ingestion.lint_proposal`); this module pins the
PROMPT TEXT only.
"""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
PROMPTS_DIR = REPO_ROOT / "src" / "ralphd" / "prompts"

PLANNING_MD = (PROMPTS_DIR / "planning.md").read_text()
PLAN_REVIEW_MD = (PROMPTS_DIR / "plan_review.md").read_text()


def _normalize_ws(text: str) -> str:
    return " ".join(text.split())


PLANNING_MD_NORM = _normalize_ws(PLANNING_MD)
PLAN_REVIEW_MD_NORM = _normalize_ws(PLAN_REVIEW_MD)


# ---------------------------------------------------------------------------
# planning.md: one probe-audit task per requirement group
# ---------------------------------------------------------------------------

def test_planning_md_names_one_probe_audit_task_per_requirement_group():
    assert "One probe-audit task per requirement group" in PLANNING_MD


def test_planning_md_states_both_probe_audit_clauses():
    assert (
        "a probe exists which fails if the behaviour is absent"
        in PLANNING_MD_NORM
    )
    assert "the probe is named in the commit message" in PLANNING_MD_NORM


def test_planning_md_names_the_group_title_convention():
    assert "`<Letter>[.N]: <description>` convention" in PLANNING_MD


def test_planning_md_names_the_lint_that_enforces_it():
    assert "plan_ingestion.lint_proposal()" in PLANNING_MD
    assert (
        "refuses a proposal that names a lettered group with no such "
        "task in it" in PLANNING_MD_NORM
    )


def test_planning_md_names_the_review_incident_that_motivates_the_rule():
    assert "37 verify iterations" in PLANNING_MD_NORM
    assert "35-minute review" in PLANNING_MD_NORM
    assert (
        "asserting the OPPOSITE of its own criterion" in PLANNING_MD_NORM
    )


# ---------------------------------------------------------------------------
# plan_review.md: the matching objection ground
# ---------------------------------------------------------------------------

def test_plan_review_md_has_probe_audit_objection_ground():
    assert "No probe-audit task for a requirement group" in PLAN_REVIEW_MD


def test_plan_review_md_states_both_probe_audit_clauses_too():
    assert (
        "a probe exists which fails if the behaviour is absent"
        in PLAN_REVIEW_MD_NORM
    )
    assert "the probe is named in the commit message" in PLAN_REVIEW_MD_NORM


def test_plan_review_md_names_the_lint_it_matches():
    assert "plan_ingestion. lint_proposal()" in PLAN_REVIEW_MD_NORM


def test_plan_review_md_names_the_group_title_convention_too():
    assert "`<Letter>[.N]: <description>`" in PLAN_REVIEW_MD


def test_plan_review_md_instructs_the_concrete_fix():
    assert "add one probe-audit task for it" in PLAN_REVIEW_MD_NORM
