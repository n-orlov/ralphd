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
