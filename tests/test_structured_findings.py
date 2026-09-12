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
import logging
import subprocess
import sys
import warnings as warnings_mod

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


def test_validating_a_finding_emits_one_warning_naming_the_extra_key(caplog):
    """Task 014 (requirement E.1, #190): the warning is emitted BY
    VALIDATION, not only available from `finding_warnings` on request --
    a caller that merely validates an otherwise-valid finding carrying one
    unrecognised key gets EXACTLY ONE `UnrecognisedFindingFieldWarning`
    naming that key, and no validation error. Exactly one means one
    emission in total, not one per channel: an earlier revision of this
    module ALSO logged the same text through a module logger, so a single
    unrecognised key was named twice (two stderr lines). This asserts the
    one stdlib-`warnings` emission and that nothing anywhere in the
    logging tree logs a duplicate of it at WARNING or above."""
    payload = _finding(future_key="written by a newer engine")
    with caplog.at_level(logging.WARNING):
        with warnings_mod.catch_warnings(record=True) as caught:
            warnings_mod.simplefilter("always")
            errors = findings.validate_finding(payload)
    assert errors == []
    raised = [w for w in caught
              if issubclass(w.category,
                            findings.UnrecognisedFindingFieldWarning)]
    assert len(raised) == 1
    assert "future_key" in str(raised[0].message)
    assert len(caught) == 1
    assert [r.getMessage() for r in caplog.records
            if "future_key" in r.getMessage()] == []


def test_one_extra_key_is_named_exactly_once_on_a_subprocess_stderr():
    """Task 014 (requirement E.1, #190): "one warning" as an ordinary
    process observes it -- under Python's DEFAULT warning and logging
    configuration, not a test harness's capture. Validating one otherwise
    valid finding carrying one unrecognised key writes that key's name to
    stderr exactly once, on exactly one line. This is the shape that
    caught the two-channel revision (`warnings.warn` plus a
    `logger.warning` of the same text: two stderr lines, two occurrences
    of `future_key` for one key), so it is pinned here."""
    program = (
        "from ralphd.engine import findings\n"
        "payload = {'severity': 'blocking', 'curable': True,\n"
        "           'clause': 'requirement E.1 (#190)',\n"
        "           'evidence': 'e', 'future_key': 'newer engine'}\n"
        "assert findings.validate_finding(payload) == []\n"
    )
    proc = subprocess.run([sys.executable, "-c", program],
                          capture_output=True, text=True, check=True)
    stderr = proc.stderr
    assert stderr.count("future_key") == 1, stderr
    assert len([line for line in stderr.splitlines()
                if "future_key" in line]) == 1, stderr
    assert "UnrecognisedFindingFieldWarning" in stderr


def test_a_findings_file_with_an_extra_key_reads_back_with_one_warning(
        tmp_path, caplog):
    """Task 014 (requirement E.1, #190): end to end through the API the
    engine itself uses (`read_findings_json`, called by `engine/loop.py`'s
    findings channel and `engine/run_record.py`) -- a findings file whose
    entry carries one key this engine's schema does not declare is
    ACCEPTED (no `ValueError`), the key is ignored (it is still present in
    the parsed payload, never rewritten), and reading it emits exactly one
    warning naming the key -- one in total, with no duplicate log record
    carrying the same text."""
    path = tmp_path / state.FINDINGS_JSON_FILE
    path.write_text(json.dumps(
        [_finding(future_key="written by a newer engine")]))
    with caplog.at_level(logging.WARNING):
        with warnings_mod.catch_warnings(record=True) as caught:
            warnings_mod.simplefilter("always")
            items = findings.read_findings_json(path)
    assert len(items) == 1 and items[0]["future_key"] == (
        "written by a newer engine")
    raised = [w for w in caught
              if issubclass(w.category,
                            findings.UnrecognisedFindingFieldWarning)]
    assert len(raised) == 1
    assert "future_key" in str(raised[0].message)
    assert len(caught) == 1
    assert [r.getMessage() for r in caplog.records
            if "future_key" in r.getMessage()] == []


def test_a_findings_file_missing_a_required_field_still_fails_to_read(
        tmp_path):
    """Task 014 (requirement E.1, #190): the unchanged half, at the file
    level -- tolerating an extra key made no field newly optional, so a
    findings file whose entry is missing a field `FINDING_SCHEMA
    ['required']` names still fails validation exactly the way it does
    today: `read_findings_json` raises `ValueError` naming the file and
    the missing field, and emits no unrecognised-key warning."""
    path = tmp_path / state.FINDINGS_JSON_FILE
    payload = _finding()
    del payload["evidence"]
    path.write_text(json.dumps([payload]))
    with warnings_mod.catch_warnings(record=True) as caught:
        warnings_mod.simplefilter("always")
        with pytest.raises(ValueError) as excinfo:
            findings.read_findings_json(path)
    assert "evidence" in str(excinfo.value)
    assert not [w for w in caught
                if issubclass(w.category,
                              findings.UnrecognisedFindingFieldWarning)]


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
