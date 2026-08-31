"""Task 003 (requirement A.3, #110): review reads the PRD's own materiality
rubric; the engine's default when a contract supplies NONE is that every
finding is blocking -- today's behaviour, unchanged (non-negotiable #2 names
this default explicitly as something never to weaken).

`engine.findings.extract_materiality_rubric` finds the contract's own
"Materiality rubric" heading (if any) and returns its text verbatim;
`materiality_rubric_context` turns that into the review-prompt context
block, falling back to the explicit `ALL_BLOCKING_DEFAULT_NOTICE` when no
heading exists. `LoopSupervisor._review_materiality_rubric_context` is the
engine call site that reads the run's own PRD/composite-PRD file (the same
file `build_prompt` already names "the PRD file") and is wired into
`_review_extra_context`, so every review-phase prompt carries this hand-off.

DoD predicate 2 ("a contract with no rubric yields all-blocking behaviour")
is pinned directly by `test_a_contract_with_no_rubric_yields_the_explicit_
all_blocking_default` below. A second scenario,
`test_a_rubric_bearing_contract_still_lets_a_finding_be_classified_
residual`, shows the presence of a rubric changing nothing about the
existing (task 002) blocking-only verdict computation -- a rubric-bearing
contract's findings can still carry `severity: "residual"` and verify.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ralphd.engine import findings
from ralphd.engine.config import JobConfig
from ralphd.engine.loop import LoopSupervisor
from ralphd.engine.state import RunDir

NO_RUBRIC_CONTRACT = """# Some PRD

## Goal

Ship the thing. No materiality rubric section exists anywhere in this
contract.

## Requirements

Do the work.
"""

RUBRIC_BEARING_CONTRACT = """# Some PRD

## Goal

Ship the thing.

## The contract-side prototype

### Materiality rubric (review: compute your verdict from blocking findings only)

**Blocking** -- a red test at the shipped sha.

**Residual** -- a stale snapshot label, imprecise prose.

### Petition protocol

Irrelevant section that must not be swept into the rubric text above.
"""


# ---------------------------------------------------------------------------
# extract_materiality_rubric
# ---------------------------------------------------------------------------

def test_extract_materiality_rubric_returns_none_when_absent():
    assert findings.extract_materiality_rubric(NO_RUBRIC_CONTRACT) is None
    assert findings.extract_materiality_rubric("") is None


def test_extract_materiality_rubric_finds_the_heading_case_insensitively():
    rubric = findings.extract_materiality_rubric(RUBRIC_BEARING_CONTRACT)
    assert rubric is not None
    assert rubric.lower().startswith("### materiality rubric")


def test_extract_materiality_rubric_stops_before_the_next_same_level_heading():
    rubric = findings.extract_materiality_rubric(RUBRIC_BEARING_CONTRACT)
    assert "Petition protocol" not in rubric
    assert "Residual" in rubric


# ---------------------------------------------------------------------------
# materiality_rubric_context -- DoD predicate 2 (all-blocking default) and
# the rubric-bearing hand-off.
# ---------------------------------------------------------------------------

def test_a_contract_with_no_rubric_yields_the_explicit_all_blocking_default():
    """DoD predicate 2: a contract with no rubric yields all-blocking
    behaviour -- pinned directly against the explicit notice text, not
    inferred."""
    context = findings.materiality_rubric_context(NO_RUBRIC_CONTRACT)
    assert findings.ALL_BLOCKING_DEFAULT_NOTICE in context
    assert "every finding is blocking" in findings.ALL_BLOCKING_DEFAULT_NOTICE


def test_an_empty_contract_also_yields_the_all_blocking_default():
    context = findings.materiality_rubric_context("")
    assert findings.ALL_BLOCKING_DEFAULT_NOTICE in context


def test_a_rubric_bearing_contract_is_handed_the_rubric_text_verbatim():
    context = findings.materiality_rubric_context(RUBRIC_BEARING_CONTRACT)
    assert findings.ALL_BLOCKING_DEFAULT_NOTICE not in context
    assert "Materiality rubric" in context
    assert "Blocking" in context and "Residual" in context
    assert "Petition protocol" not in context


# ---------------------------------------------------------------------------
# LoopSupervisor._review_materiality_rubric_context -- the engine call site.
# ---------------------------------------------------------------------------

def _supervisor(root: Path) -> LoopSupervisor:
    root.mkdir(parents=True, exist_ok=True)
    run = RunDir(root=root)
    run.update_status(state="starting")
    cfg = JobConfig(run_id="task003-materiality-rubric")
    return LoopSupervisor(cfg, run, root)


def test_the_engine_call_site_defaults_to_all_blocking_with_no_prd_file(
        tmp_path):
    sup = _supervisor(tmp_path)
    # No PRD file has been written at all -- the ordinary "very early in
    # planning" case, must not raise and must still hand review the
    # explicit default rather than omitting the context.
    context = sup._review_materiality_rubric_context()
    assert findings.ALL_BLOCKING_DEFAULT_NOTICE in context


def test_the_engine_call_site_surfaces_a_rubric_from_the_run_s_own_prd_file(
        tmp_path):
    sup = _supervisor(tmp_path)
    sup.run.prd_file.write_text(RUBRIC_BEARING_CONTRACT)
    context = sup._review_materiality_rubric_context()
    assert findings.ALL_BLOCKING_DEFAULT_NOTICE not in context
    assert "Blocking" in context and "Residual" in context


def test_the_engine_call_site_prefers_the_composite_prd_when_present(
        tmp_path):
    sup = _supervisor(tmp_path)
    sup.run.prd_file.write_text(NO_RUBRIC_CONTRACT)
    sup.run.composite_prd_file.write_text(RUBRIC_BEARING_CONTRACT)
    context = sup._review_materiality_rubric_context()
    assert findings.ALL_BLOCKING_DEFAULT_NOTICE not in context
    assert "Blocking" in context and "Residual" in context


def test_review_extra_context_includes_the_rubric_context(tmp_path):
    sup = _supervisor(tmp_path)
    sup.run.prd_file.write_text(RUBRIC_BEARING_CONTRACT)
    extra = sup._review_extra_context()
    assert "Materiality rubric" in extra
    assert "Blocking" in extra and "Residual" in extra


# ---------------------------------------------------------------------------
# Second assertion (task 003's own criterion): a rubric-bearing contract
# still lets at least one finding be classified `residual` -- the rubric's
# presence changes nothing about task 002's blocking-only verdict
# computation; it is purely a hand-off of text to review, never an engine
# override of a finding's own severity field.
# ---------------------------------------------------------------------------

def test_a_rubric_bearing_contract_still_lets_a_finding_be_classified_residual(
        tmp_path):
    sup = _supervisor(tmp_path)
    sup.run.prd_file.write_text(RUBRIC_BEARING_CONTRACT)
    # Review read the rubric above and classified accordingly: one finding
    # residual (a stale snapshot label), nothing blocking.
    findings_list = [
        {
            "severity": "residual",
            "curable": True,
            "clause": "prose polish",
            "evidence": "a stale snapshot label, per the rubric's own "
                        "residual example",
        },
    ]
    assert findings.compute_verdict(findings_list) == "verified"
    # The rubric's presence is visible to review at the same time.
    context = sup._review_materiality_rubric_context()
    assert "Residual" in context


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
