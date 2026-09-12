"""Task 004 (requirement A.4, #110): `review.md` demands the structured
findings channel (severity, curable, clause, evidence) and states that the
verdict is computed from blocking findings only, and both peer gates
(`plan_review.md`, `verdict_gate.md`) stay consistent with that payload
shape -- neither describes `{"verdict", "reason", "findings_path"}` as the
whole of a judging hat's findings channel.

This module pins the PROMPT TEXT itself (DoD predicate 8); the engine-side
computation these prompts describe is already pinned by
`tests/test_structured_findings.py` (task 001) and
`tests/test_review_verdict_findings.py` (task 002).
"""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
PROMPTS_DIR = REPO_ROOT / "src" / "ralphd" / "prompts"

REVIEW_MD = (PROMPTS_DIR / "review.md").read_text()
PLAN_REVIEW_MD = (PROMPTS_DIR / "plan_review.md").read_text()
VERDICT_GATE_MD = (PROMPTS_DIR / "verdict_gate.md").read_text()

FINDING_FIELDS = ("severity", "curable", "clause", "evidence")


# ---------------------------------------------------------------------------
# review.md: the new structured-findings demand
# ---------------------------------------------------------------------------

def test_review_md_names_every_structured_finding_field():
    for field in FINDING_FIELDS:
        assert f"`{field}`" in REVIEW_MD, (
            f"review.md must demand the structured finding field {field!r}")


def test_review_md_names_the_blocking_and_residual_severities():
    assert '"blocking"' in REVIEW_MD
    assert '"residual"' in REVIEW_MD


def test_review_md_states_the_verdict_is_computed_from_blocking_only():
    lowered = REVIEW_MD.lower()
    assert "blocking findings only" in lowered or (
        "computed from the blocking" in lowered)


def test_review_md_names_the_json_sidecar_beside_the_markdown_file():
    assert "review-findings.json" in REVIEW_MD
    assert "review-findings.md" in REVIEW_MD


# ---------------------------------------------------------------------------
# review.md: nothing that already existed to enforce distrust/independence
# may have been removed by this task (steps 3-7, Operator rulings, the
# closing "Be strict" paragraph) -- pinned directly so a future edit that
# regresses it fails loudly here, not only by diffing history.
# ---------------------------------------------------------------------------

def test_review_md_keeps_every_original_numbered_step():
    for n in range(1, 8):
        assert f"{n}. " in REVIEW_MD, f"review.md is missing step {n}"


def test_review_md_keeps_the_operator_rulings_section():
    assert "## Operator rulings" in REVIEW_MD
    assert "/config/amendments/" in REVIEW_MD
    assert "read-only" in REVIEW_MD


def test_review_md_keeps_the_closing_be_strict_paragraph():
    assert 'Be strict. "Probably fine" is not verified.' in REVIEW_MD
    assert REVIEW_MD.rstrip().endswith(
        "If you cannot check something, that is\na finding, not a pass.")


def test_review_md_keeps_trusting_nothing_the_worker_wrote():
    assert "trusting nothing the worker wrote about its own work" in REVIEW_MD


# ---------------------------------------------------------------------------
# Both peer gates: consistent with the new shape, no stale "whole channel"
# claim about `{"verdict", "reason", "findings_path"}`.
# ---------------------------------------------------------------------------

def test_verdict_gate_md_does_not_claim_its_own_payload_is_the_whole_channel():
    assert "not a claim that it is the whole" in VERDICT_GATE_MD


def test_verdict_gate_md_names_every_structured_finding_field_too():
    for field in FINDING_FIELDS:
        assert f"`{field}`" in VERDICT_GATE_MD, (
            f"verdict_gate.md must stay consistent with review.md's "
            f"structured finding field {field!r}")


def test_verdict_gate_md_still_passes_its_own_blindness_contract():
    """Guard against a future edit re-introducing a leak this task's own
    additions to verdict_gate.md could plausibly cause (a bare 'review'
    token sitting close to a literal verdict value) -- exercised properly
    by `tests/test_peer_verdict_gate.py`; this is a narrow smoke check that
    this task's own new section did not (re)introduce one.
    """
    import re

    verdict_value_re = re.compile(r'"(?:verified|rejected)"')
    review_token_re = re.compile(r"\breview(er|s|ed)?\b", re.IGNORECASE)
    masked = VERDICT_GATE_MD.replace("review-findings.md", "F" * len("review-findings.md"))
    for m in verdict_value_re.finditer(masked):
        lo = max(0, m.start() - 400)
        window = masked[lo:m.end() + 400]
        assert not review_token_re.search(window), (
            "a verdict literal sits within 400 chars of a bare 'review' "
            "token -- this leaks the other judge's conclusion; see "
            "tests/test_peer_verdict_gate.py")


def test_plan_review_md_has_no_stale_review_payload_description():
    """`plan_review.md` never described review's own payload shape at all
    (its contract is `approved`/`objected`, unrelated to findings) -- this
    pins that it still does not, so a future edit cannot quietly reintroduce
    a stale `{"verdict", "reason", "findings_path"}`-is-the-whole-channel
    claim about the review hat here either."""
    assert "findings_path" not in PLAN_REVIEW_MD


# ---------------------------------------------------------------------------
# Task 015 (requirement B, #114): review.md's description of the mid-run
# adjudication episode's payload shape -- the scoped OPEN-petition set
# (`LoopSupervisor._midrun_adjudication_extra`), the CONFIRMED/REFUTED
# vocabulary (`transitions.HAT_SCHEMAS["review"]["petition_adjudications"]`),
# the optional judge-authored `replacement_criteria` for EXACTLY ONE task
# (`task_id`), and the narrow prohibition on amending the PRD, a
# Definition-of-done item, another task's criteria or anything under
# `/config`. Both peer gates (`verdict_gate.md`, `plan_review.md`) must
# carry no claim that contradicts this -- neither hat's own schema has a
# `petition_adjudications`/`replacement_criteria` field
# (`transitions.HAT_SCHEMAS["verdict_gate"]`,
# `transitions.HAT_SCHEMAS["plan_review"]`), so neither prompt may claim
# that authority either. Pinned here, in the SAME module the existing
# "no stale review payload description" test already lives in, so one
# file is the one place a future edit to any of the three prompts is
# checked for drift against this payload shape.
# ---------------------------------------------------------------------------

def test_review_md_describes_the_scoped_open_petition_set():
    assert "## Impossibility petitions" in REVIEW_MD
    assert "Open impossibility petitions" in REVIEW_MD
    assert "adjudicate EVERY petition listed there" in REVIEW_MD


def test_review_md_names_the_confirmed_refuted_vocabulary():
    assert "`CONFIRMED`" in REVIEW_MD
    assert "`REFUTED`" in REVIEW_MD
    assert "petition_adjudications" in REVIEW_MD


def test_review_md_describes_the_replacement_criteria_for_exactly_one_task():
    assert "`task_id`" in REVIEW_MD
    assert "`replacement_criteria`" in REVIEW_MD
    assert "only the ONE task" in REVIEW_MD
    assert "you, and only you, MAY re-scope" in REVIEW_MD


def test_review_md_prohibits_amending_prd_dod_other_task_or_config():
    assert "you cannot amend the PRD, a Definition-of-done item," in REVIEW_MD
    assert "any OTHER task's criteria, or anything under `/config`" in REVIEW_MD


def test_verdict_gate_md_claims_no_amendment_authority():
    """`verdict_gate`'s own schema has no `petition_adjudications` field
    (`transitions.HAT_SCHEMAS["verdict_gate"]`) -- this pins that its
    prompt never claims the `replacement_criteria`/`task_id` amendment
    authority review.md reserves to itself alone."""
    assert "replacement_criteria" not in VERDICT_GATE_MD
    assert "task_id" not in VERDICT_GATE_MD


def test_plan_review_md_claims_no_amendment_authority():
    """Same check for `plan_review`, whose own schema
    (`transitions.HAT_SCHEMAS["plan_review"]`) also has no
    `petition_adjudications`/`replacement_criteria` field."""
    assert "replacement_criteria" not in PLAN_REVIEW_MD
    assert "task_id" not in PLAN_REVIEW_MD


# ---------------------------------------------------------------------------
# Task 027 (requirement G.4, #inspection-is-not-evidence): the three rules
# tasks 024-026 added to review.md/verdict_gate.md -- probe-per-behavioural-
# requirement, the disposable-clone measurement protocol (both judging
# prompts), and the removal-requirement check. Each node below names the
# concrete phrase the underlying commit landed, not a paraphrase, so a
# future edit that waters down or removes any of the three trips here.
# ---------------------------------------------------------------------------

def _normalize_ws(text: str) -> str:
    return " ".join(text.split())


REVIEW_MD_NORM = _normalize_ws(REVIEW_MD)
VERDICT_GATE_MD_NORM = _normalize_ws(VERDICT_GATE_MD)


def test_review_md_requires_a_traceability_table_row_per_behavioural_requirement():
    assert "For every **behavioural**" in REVIEW_MD_NORM
    assert "build a traceability table with one row per requirement" in REVIEW_MD_NORM
    assert (
        "the **smallest test that would fail if the behaviour were absent**"
        in REVIEW_MD_NORM
    )
    assert "**run that test** and **record its result**" in REVIEW_MD_NORM


def test_review_md_labels_an_inspection_only_discharge_and_blocks_the_unlabelled_case():
    assert "label that row `inspection-only`" in REVIEW_MD_NORM
    assert (
        "An inspection-only discharge left unlabelled, or labelled for a "
        "requirement that is in fact behavioural, is itself a blocking "
        "finding, not a residual" in REVIEW_MD_NORM
    )
    assert "inspection is not evidence for anything a probe could have checked instead" in REVIEW_MD_NORM


def test_review_md_and_verdict_gate_md_both_describe_the_disposable_clone_protocol():
    for name, text, norm in (
        ("review.md", REVIEW_MD, REVIEW_MD_NORM),
        ("verdict_gate.md", VERDICT_GATE_MD, VERDICT_GATE_MD_NORM),
    ):
        assert "## Measuring in a disposable clone" in text, name
        assert "git clone --no-hardlinks" in norm, name
        assert "pip install -e .` the clone" in norm, name
        assert "ralphd.__file__" in norm, name
        assert (
            "resolves inside the temp clone's own tree, not the installed "
            "engine's copy and not the original workspace" in norm
        ), name


def test_review_md_states_a_green_suite_is_not_evidence_for_a_removal_requirement():
    assert (
        "## A green shipped suite is not evidence for a removal requirement"
        in REVIEW_MD
    )
    assert (
        "the shipped tests are exactly as likely to still **pin the behaviour "
        "being removed**" in REVIEW_MD_NORM
    )
    assert (
        "**find the test that asserts the behaviour being removed and confirm "
        "it was retired or rewritten.**" in REVIEW_MD_NORM
    )
    assert "A row in the traceability table" in REVIEW_MD
