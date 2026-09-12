"""Task 001 (requirement A, #110): the structured findings channel.

`engine.findings` adds a machine-readable channel beside the existing
free-markdown `review-findings.md` -- a JSON sidecar of structured findings,
each carrying at minimum `severity` (`blocking|residual`), `curable`
(bool), `clause` (the PRD clause / DoD item violated) and `evidence`.
Validation goes through the ONE schema engine
(`ralphd.engine.contracts.validate_against`) -- there is deliberately no
second validator here, and `tests/test_response_contracts.py`'s whole-tree
scan (fact 16/#65) covers this module too.

Verdict computation from the blocking set (requirement A.2), the rubric
default (A.3) and `review.md`'s prompt update (A.4) are later tasks'
scope -- this module and these tests only prove the channel itself exists,
validates correctly, and round-trips.
"""

from __future__ import annotations

import json

import pytest

from ralphd.engine import findings, state


def _finding(**overrides):
    payload = {
        "severity": "blocking",
        "curable": True,
        "clause": "requirement A (#110)",
        "evidence": "no structured findings channel existed before this task",
    }
    payload.update(overrides)
    return payload


# ---------------------------------------------------------------------------
# validate_finding / validate_findings
# ---------------------------------------------------------------------------

def test_a_well_formed_finding_validates_clean():
    assert findings.validate_finding(_finding()) == []


def test_a_residual_finding_validates_clean_too():
    assert findings.validate_finding(
        _finding(severity="residual", curable=False)) == []


def test_a_finding_missing_severity_is_rejected():
    payload = _finding()
    del payload["severity"]
    errors = findings.validate_finding(payload)
    assert any("severity" in e for e in errors)


def test_a_finding_missing_curable_is_rejected():
    payload = _finding()
    del payload["curable"]
    errors = findings.validate_finding(payload)
    assert any("curable" in e for e in errors)


def test_a_finding_with_a_non_boolean_curable_is_rejected():
    errors = findings.validate_finding(_finding(curable="yes"))
    assert any("curable" in e for e in errors)


def test_a_finding_with_an_out_of_enum_severity_is_rejected():
    errors = findings.validate_finding(_finding(severity="catastrophic"))
    assert any("catastrophic" in e for e in errors)


def test_an_unexpected_field_on_a_finding_is_ignored_with_one_named_warning():
    """Task 014 (requirement E.1, #190): a key `FINDING_SCHEMA` does not
    declare no longer fails the finding -- it used to (this test's old
    name/assertion was exactly the opposite: 'reported not dropped'), and
    that strictness is what let one workspace-version-ahead field self-lock
    the whole findings channel. Now the finding still validates clean, and
    `finding_warnings` names the ignored key by name -- ignored, not
    silently lost."""
    payload = _finding(extra_field="oops")
    assert findings.validate_finding(payload) == []
    warnings = findings.finding_warnings(payload)
    assert len(warnings) == 1
    assert "extra_field" in warnings[0]


def test_a_finding_with_no_unexpected_field_has_no_warnings():
    assert findings.finding_warnings(_finding()) == []


def test_a_finding_missing_a_required_field_still_fails_validation():
    """Task 014 (requirement E.1, #190): the other half of E.1 -- nothing
    about tolerating an unrecognised EXTRA key changes what happens to a
    finding missing a field `FINDING_SCHEMA['required']` actually names.
    `test_a_finding_missing_severity_is_rejected`/`..._curable_...` above
    already cover this per-field; this is the named end-to-end version E.1
    asks for, through the same public API a caller actually uses."""
    payload = _finding()
    del payload["severity"]
    errors = findings.validate_finding(payload)
    assert errors
    assert any("severity" in e for e in errors)


def test_validate_findings_accepts_a_clean_list():
    assert findings.validate_findings(
        [_finding(), _finding(severity="residual", curable=False)]) == []


def test_validate_findings_reports_the_index_of_the_bad_element():
    payload = _finding()
    del payload["curable"]
    errors = findings.validate_findings([_finding(), payload])
    assert any(e.startswith("$[1]") for e in errors)
    assert not any(e.startswith("$[0]") for e in errors)


def test_validate_findings_rejects_a_non_list_top_level():
    errors = findings.validate_findings({"not": "a list"})
    assert errors and "array" in errors[0]


# ---------------------------------------------------------------------------
# Round-trip through the JSON sidecar channel
# ---------------------------------------------------------------------------

def test_a_structured_finding_round_trips_through_the_channel(tmp_path):
    path = tmp_path / state.FINDINGS_JSON_FILE
    original = [
        _finding(severity="blocking", curable=True, clause="requirement A",
                  evidence="observed X"),
        _finding(severity="residual", curable=False, clause="requirement D",
                  evidence="observed Y"),
    ]
    findings.write_findings_json(path, original)
    round_tripped = findings.read_findings_json(path)
    assert round_tripped == original
    # Both fields requirement B needs (`curable`) and requirement A needs
    # (`severity`) survive the round trip on every element, not just some.
    for finding in round_tripped:
        assert finding["severity"] in ("blocking", "residual")
        assert isinstance(finding["curable"], bool)


def test_the_sidecar_is_valid_json_on_disk(tmp_path):
    path = tmp_path / state.FINDINGS_JSON_FILE
    findings.write_findings_json(path, [_finding()])
    on_disk = json.loads(path.read_text())
    assert on_disk == [_finding()]


def test_write_refuses_a_finding_missing_severity(tmp_path):
    path = tmp_path / state.FINDINGS_JSON_FILE
    payload = _finding()
    del payload["severity"]
    with pytest.raises(ValueError, match="severity"):
        findings.write_findings_json(path, [payload])
    assert not path.exists()


def test_write_refuses_a_finding_missing_curable(tmp_path):
    path = tmp_path / state.FINDINGS_JSON_FILE
    payload = _finding()
    del payload["curable"]
    with pytest.raises(ValueError, match="curable"):
        findings.write_findings_json(path, [payload])
    assert not path.exists()


def test_read_rejects_a_hand_edited_file_missing_severity(tmp_path):
    path = tmp_path / state.FINDINGS_JSON_FILE
    payload = _finding()
    del payload["severity"]
    path.write_text(json.dumps([payload]))
    with pytest.raises(ValueError, match="severity"):
        findings.read_findings_json(path)


def test_read_rejects_a_hand_edited_file_missing_curable(tmp_path):
    path = tmp_path / state.FINDINGS_JSON_FILE
    payload = _finding()
    del payload["curable"]
    path.write_text(json.dumps([payload]))
    with pytest.raises(ValueError, match="curable"):
        findings.read_findings_json(path)


def test_read_rejects_a_missing_file(tmp_path):
    with pytest.raises(ValueError, match="no findings channel"):
        findings.read_findings_json(tmp_path / state.FINDINGS_JSON_FILE)


# ---------------------------------------------------------------------------
# The markdown channel is kept, never replaced
# ---------------------------------------------------------------------------

def test_the_markdown_findings_file_name_is_unchanged():
    assert state.FINDINGS_FILE == "review-findings.md"


def test_the_json_sidecar_sits_beside_the_markdown_file_not_instead_of_it():
    assert state.FINDINGS_JSON_FILE == "review-findings.json"
    assert state.FINDINGS_JSON_FILE != state.FINDINGS_FILE


def test_rundir_exposes_both_findings_paths_side_by_side(tmp_path):
    run = state.RunDir(tmp_path)
    assert run.findings_file.parent == run.findings_json_file.parent
    assert run.findings_file.name == "review-findings.md"
    assert run.findings_json_file.name == "review-findings.json"
