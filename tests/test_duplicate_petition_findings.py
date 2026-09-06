"""Task 012 (requirement C.1, #112): a duplicate petition for the same
obligation, and a re-filed petition whose earlier filing was already
adjudicated REFUTED, are both detected and reported as blocking findings
-- the detection lives in `engine.petitions` (pure, structural, no review
judgement call needed) and the reporting is engine/prompt: `engine.
petitions.duplicate_petition_findings` is appended, unconditionally, to
review's own findings channel by
`LoopSupervisor._review_findings_channel`, and `engine.petitions.
duplicate_petitions_review_context` names the same fact in review's own
prompt via `_review_extra_context`. Neither depends on the run's own
prose ever having mentioned the duplicate.

Mirrors the existing `tests/test_petitions_review.py`'s shape: parse-layer
unit tests first, then the `LoopSupervisor` join tests.
"""

from __future__ import annotations

from pathlib import Path

from ralphd.engine import findings
from ralphd.engine import petitions
from ralphd.engine.config import JobConfig
from ralphd.engine.loop import LoopSupervisor
from ralphd.engine.state import RunDir
from tests.conftest import supervisor_factory

DUPLICATE_OPEN_MD = """# v0.15 petitions

## First filing

**Obligation**: Requirement Z cannot be met.
**Type**: environmental
**Evidence**: no such capability in this sandbox.

## Second filing, same obligation

**Obligation**: Requirement Z cannot be met.
**Type**: environmental
**Evidence**: repeating the claim in different words.
"""

REFILED_REFUTED_MD = """# v0.15 petitions

## Earlier filing, already refuted

**Obligation**: Requirement Y is unachievable.
**Type**: temporal
**Evidence**: claimed the deadline had passed.
**Adjudication**: REFUTED -- file it before the deadline next time.

## Re-filed after refutation

**Obligation**: Requirement Y is unachievable.
**Type**: temporal
**Evidence**: still claiming the same thing.
"""

SINGLE_FILING_MD = """# v0.15 petitions

## Only one filing

**Obligation**: Requirement X.
**Type**: external
**Evidence**: outside the run's control.
"""


# The shared harness (tests/conftest.py, G.1): this module's run dir and
# workspace sit side by side under the test's own tmp_path.
_supervisor = supervisor_factory(run_id="unit", run_root="run",
                                workspace="ws")



def _write(tmp_path: Path, name: str, text: str) -> None:
    reports = tmp_path / "ws" / "artifacts" / "reports"
    reports.mkdir(parents=True, exist_ok=True)
    (reports / name).write_text(text)


# -- engine.petitions: detection ---------------------------------------------

def test_no_duplicate_when_every_obligation_has_one_filing(tmp_path):
    _write(tmp_path, "v0.15-petitions.md", SINGLE_FILING_MD)
    assert petitions.duplicate_petition_obligations(tmp_path / "ws") == []
    assert petitions.duplicate_petition_findings(tmp_path / "ws") == []


def test_duplicate_petition_for_the_same_obligation_is_detected(tmp_path):
    _write(tmp_path, "v0.15-petitions.md", DUPLICATE_OPEN_MD)
    dupes = petitions.duplicate_petition_obligations(tmp_path / "ws")
    assert len(dupes) == 1
    assert "Requirement Z" in dupes[0]["obligation"]
    assert len(dupes[0]["filings"]) == 2


def test_duplicate_petition_becomes_a_blocking_finding(tmp_path):
    _write(tmp_path, "v0.15-petitions.md", DUPLICATE_OPEN_MD)
    out = petitions.duplicate_petition_findings(tmp_path / "ws")
    assert len(out) == 1
    f = out[0]
    assert f["severity"] == "blocking"
    assert f["curable"] is True
    assert "Requirement Z" in f["clause"]
    assert findings.validate_finding(f) == []


def test_refiled_refuted_petition_is_detected(tmp_path):
    _write(tmp_path, "v0.15-petitions.md", REFILED_REFUTED_MD)
    dupes = petitions.duplicate_petition_obligations(tmp_path / "ws")
    assert len(dupes) == 1
    assert any(f["adjudication"] == "REFUTED" for f in dupes[0]["filings"])


def test_refiled_refuted_petition_becomes_a_blocking_finding(tmp_path):
    _write(tmp_path, "v0.15-petitions.md", REFILED_REFUTED_MD)
    out = petitions.duplicate_petition_findings(tmp_path / "ws")
    assert len(out) == 1
    f = out[0]
    assert f["severity"] == "blocking"
    assert f["curable"] is True
    assert "REFUTED" in f["evidence"]
    assert findings.validate_finding(f) == []


def test_duplicate_finding_evidence_names_the_filing_count(tmp_path):
    _write(tmp_path, "v0.15-petitions.md", DUPLICATE_OPEN_MD)
    f = petitions.duplicate_petition_findings(tmp_path / "ws")[0]
    assert "2 petitions filed" in f["evidence"]


def test_no_duplicates_across_disjoint_files_with_distinct_obligations(tmp_path):
    _write(tmp_path, "v0.14-petitions.md", SINGLE_FILING_MD)
    _write(tmp_path, "v0.15-petitions.md", REFILED_REFUTED_MD.replace(
        "Requirement Y", "Requirement Q"))
    assert len(petitions.duplicate_petition_obligations(tmp_path / "ws")) == 1


# -- LoopSupervisor: prompt surface -------------------------------------------

def test_duplicate_petitions_review_context_empty_when_none(tmp_path):
    sup = _supervisor(tmp_path)
    assert sup._duplicate_petitions_review_context() == ""


def test_duplicate_petitions_review_context_names_the_obligation(tmp_path):
    _write(tmp_path, "v0.15-petitions.md", DUPLICATE_OPEN_MD)
    sup = _supervisor(tmp_path)
    ctx = sup._duplicate_petitions_review_context()
    assert "Requirement Z" in ctx
    assert "Duplicate or re-filed petitions detected" in ctx


def test_duplicate_petitions_context_joins_review_extra_context(tmp_path, monkeypatch):
    from ralphd.engine import loop as loop_mod

    _write(tmp_path, "v0.15-petitions.md", REFILED_REFUTED_MD)
    sup = _supervisor(tmp_path)
    monkeypatch.setattr(loop_mod, "CONFIG_DIR", tmp_path / "config")
    ctx = sup._review_extra_context()
    assert "Requirement Y" in ctx
    assert "Duplicate or re-filed petitions detected" in ctx


# -- LoopSupervisor: findings-channel surface (engine, unconditional) --------

def test_review_findings_channel_appends_duplicate_finding_engine_side(tmp_path):
    """Even when review's OWN findings.json says nothing at all about the
    duplicate, `_review_findings_channel` still reports it as blocking --
    the detection is engine-side, never dependent on the run's own prose."""
    _write(tmp_path, "v0.15-petitions.md", DUPLICATE_OPEN_MD)
    sup = _supervisor(tmp_path)
    sup.run.root.mkdir(parents=True, exist_ok=True)
    findings.write_findings_json(sup.run.findings_json_file, [])
    findings_list, errors = sup._review_findings_channel()
    assert errors == []
    assert findings_list is not None
    blocking = [f for f in findings_list if f["severity"] == "blocking"]
    assert len(blocking) == 1
    assert "Requirement Z" in blocking[0]["clause"]


def test_review_findings_channel_no_extra_finding_without_a_duplicate(tmp_path):
    _write(tmp_path, "v0.15-petitions.md", SINGLE_FILING_MD)
    sup = _supervisor(tmp_path)
    sup.run.root.mkdir(parents=True, exist_ok=True)
    findings.write_findings_json(sup.run.findings_json_file, [])
    findings_list, errors = sup._review_findings_channel()
    assert errors == []
    assert findings_list == []
