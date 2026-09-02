"""Task 019 (PRD requirement D.4, #125): the peer verdict gate gets its OWN
findings path, `verdict-gate-findings.md` + `.json` sidecar, deliberately
distinct from review's own `review-findings.md`/`.json` -- the two
independent judges (requirement L) must never be able to overwrite one
another's findings by sharing a fixed path. Every structured finding, from
either channel, now carries an `author` field the ENGINE itself attributes
(`findings.attribute_authorship`, called from `LoopSupervisor.
_archive_findings_json`) from which of the two fixed channels it came
from -- never trusted from whatever (if anything) the hat itself wrote --
so the fact holds even for a hand-written fixture, like the ones this
module uses, that never sets `author` at all.

`_write_composite_prd` folds both channels into the next approach's
composite PRD, distinguishable by NAME (the markdown section headers and
JSON sidecar filenames) and by the `author` field carried by every
structured entry.

Reuses `tests/test_peer_verdict_gate.py`'s own scripted-agent harness
(`_run_job`, `_ok`, `_plan`, `_worker_completes`, `TASK_1`) -- the same
cross-module import shape `tests/test_plan_review_deterministic_
undecidable_task016.py` already uses for `test_peer_plan_review_gate`.
"""

from __future__ import annotations

import json

from ralphd.engine import findings, state

from test_peer_verdict_gate import (  # noqa: F401 (re-exported fixtures)
    TASK_1,
    _ok,
    _plan,
    _run_job,
    _worker_completes,
)


# ---------------------------------------------------------------------------
# Unit: RunDir's new properties / state.py's new constants
# ---------------------------------------------------------------------------


def test_state_declares_a_distinct_fixed_path_for_the_gate():
    assert state.VERDICT_GATE_FINDINGS_FILE == "verdict-gate-findings.md"
    assert state.VERDICT_GATE_FINDINGS_JSON_FILE == "verdict-gate-findings.json"
    assert state.VERDICT_GATE_FINDINGS_FILE != state.FINDINGS_FILE
    assert state.VERDICT_GATE_FINDINGS_JSON_FILE != state.FINDINGS_JSON_FILE


def test_rundir_exposes_the_gate_s_own_findings_paths(tmp_path):
    run = state.RunDir(root=tmp_path)
    assert run.verdict_gate_findings_file == tmp_path / "verdict-gate-findings.md"
    assert run.verdict_gate_findings_json_file == tmp_path / "verdict-gate-findings.json"


# ---------------------------------------------------------------------------
# Unit: findings.attribute_authorship
# ---------------------------------------------------------------------------


def _finding(**overrides) -> dict:
    payload = {"severity": "blocking", "curable": True,
              "clause": "requirement Q", "evidence": "observed"}
    payload.update(overrides)
    return payload


def test_attribute_authorship_sets_the_field_when_absent():
    out = findings.attribute_authorship([_finding()], "verdict_gate")
    assert out[0]["author"] == "verdict_gate"


def test_attribute_authorship_overwrites_whatever_was_already_there():
    """The engine's own attribution is unconditional -- it never trusts a
    hat's own (possibly wrong) `author` claim over the fixed channel the
    finding was actually read from."""
    out = findings.attribute_authorship([_finding(author="somebody-else")],
                                        "review")
    assert out[0]["author"] == "review"


def test_attribute_authorship_never_mutates_its_input():
    original = [_finding()]
    findings.attribute_authorship(original, "review")
    assert "author" not in original[0]


def test_author_is_a_validating_optional_finding_field():
    """Adding `author` to `FINDING_SCHEMA` must not turn additional-
    properties enforcement off -- a genuinely unknown field is still
    refused, and a finding lacking `author` altogether (every fixture in
    `tests/test_structured_findings.py`) still validates unchanged."""
    assert findings.validate_finding(_finding(author="review")) == []
    assert findings.validate_finding(_finding()) == []
    assert findings.validate_finding(_finding(bogus_field="x")) != []


# ---------------------------------------------------------------------------
# End to end: review verifies with a RESIDUAL finding on record, the gate
# then rejects with its OWN blocking finding -- the two artifacts must be
# distinguishable by name and by author once archived and folded.
# ---------------------------------------------------------------------------


def _review_verifies_with_a_residual_finding(sup, prompt=""):
    findings.write_findings_json(sup.run.findings_json_file, [_finding(
        severity="residual", curable=True, clause="requirement Z (residual)",
        evidence="review's own residual observation")])
    sup.run.findings_file.write_text(
        "### Residual\nrequirement Z could use polish, but is not blocking.\n")
    return _ok(json.dumps({"verdict": "verified"}))


def _verdict_gate_rejects_with_a_blocking_finding(reason: str):
    def handler(sup, prompt=""):
        findings.write_findings_json(sup.run.verdict_gate_findings_json_file, [
            _finding(clause="requirement Q (blocking)",
                    evidence="the peer's own independent observation")])
        sup.run.verdict_gate_findings_file.write_text(
            "### Blocking\nrequirement Q is unmet, independently re-checked.\n")
        return _ok(json.dumps({"verdict": "rejected", "reason": reason}))
    return handler


def test_review_verified_with_residuals_then_gate_rejects_yields_two_distinguishable_artifacts(tmp_path):
    root = tmp_path / "run"
    root.mkdir(parents=True, exist_ok=True)
    (root / "prd.md").write_text("# test PRD\n\nDo the thing.\n")

    sup, final_state = _run_job(
        root,
        {"planning": [_plan()],
         "worker": [_worker_completes(TASK_1)],
         "review": [_review_verifies_with_a_residual_finding],
         "verdict_gate": [_verdict_gate_rejects_with_a_blocking_finding(
             "the peer independently found requirement Q unmet")]},
        peer_verdict_gate=True, max_approaches=1)

    # a peer rejection is never a terminal success, budget or not -- not
    # this test's own point, just the shape `test_peer_verdict_gate.py`
    # already pins for a no-approach-left rejection.
    assert final_state == "failed", sup.run.read_status()

    approach_dir = root / "approaches" / "01"
    review_md = approach_dir / state.FINDINGS_FILE
    gate_md = approach_dir / state.VERDICT_GATE_FINDINGS_FILE
    review_json_path = approach_dir / state.FINDINGS_JSON_FILE
    gate_json_path = approach_dir / state.VERDICT_GATE_FINDINGS_JSON_FILE

    # distinguishable by NAME: two different filenames, both archived.
    assert review_md.exists()
    assert gate_md.exists()
    assert review_md.name != gate_md.name
    assert "requirement Z" in review_md.read_text()
    assert "requirement Q" in gate_md.read_text()

    # distinguishable by AUTHOR: the archived JSON sidecars carry the
    # engine-attributed `author` field, correctly per channel, regardless
    # of the fact neither fixture handler above ever set one itself.
    review_items = findings.read_findings_json(review_json_path)
    gate_items = findings.read_findings_json(gate_json_path)
    assert review_items[0]["author"] == "review"
    assert gate_items[0]["author"] == "verdict_gate"
    assert review_items[0]["severity"] == "residual"
    assert gate_items[0]["severity"] == "blocking"

    # `_write_composite_prd` folded both, still distinguishable both ways,
    # in the artifact the NEXT approach actually reads from.
    composite = (root / "composite-prd.md").read_text()
    assert state.FINDINGS_FILE in composite
    assert state.VERDICT_GATE_FINDINGS_FILE in composite
    assert "author: review" in composite
    assert "author: verdict_gate" in composite
    assert '"author": "review"' in composite
    assert '"author": "verdict_gate"' in composite
    assert "requirement Z" in composite
    assert "requirement Q" in composite
