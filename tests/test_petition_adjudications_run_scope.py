"""Task 011 (requirement C.3/C.4, #112): CONFIRMED petition adjudications
persist at RUN scope and survive `LoopSupervisor._archive_approach` --
approach N+1 inherits every CONFIRMED adjudication, and the obligation it
names is removed from the blocking set (a CONFIRMED finding is downgraded
to `severity: "residual"`, never dropped).

`engine.petitions` gains the write/read/digest layer
(`record_confirmed_adjudications`/`read_confirmed_adjudications`/
`confirmed_obligations`/`downgrade_confirmed_findings`/
`tamper_review_context`); `RunDir.petition_adjudications_file` is the new
run-scope path (`run.root/petition-adjudications.json`, deliberately NOT
one of the three paths `_archive_approach` copies into `approaches/NN/`);
`LoopSupervisor._review_findings_channel` applies the downgrade for every
caller (`_review_verdict`, `_review_contract_ladder`,
`_review_incurable_rejection`) in one place, and `_review_verdict` itself
persists CONFIRMED entries as a side effect of parsing review's own
contract payload -- never from a worker or review hat's own process, which
is the tamper-evidence property: only `LoopSupervisor`/`engine.petitions`
ever write the file. A worker-phase hand-edit is detected by
`confirmed_obligations`'s digest check and surfaced to review's own prompt
(`_petition_adjudications_tamper_context`) as a mandatory BLOCKING finding.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ralphd.engine import findings, petitions
from ralphd.engine.config import JobConfig
from ralphd.engine.loop import LoopSupervisor
from ralphd.engine.runner import IterationResult
from ralphd.engine.state import RunDir, read_json


def _finding(severity: str, **overrides) -> dict:
    payload = {
        "severity": severity,
        "curable": True,
        "clause": "Requirement B, DoD predicate 9",
        "evidence": "fixture evidence",
    }
    payload.update(overrides)
    return payload


def _result(text: str) -> IterationResult:
    r = IterationResult(exit_code=0)
    r.final_text = text
    return r


def _supervisor(root: Path) -> LoopSupervisor:
    root.mkdir(parents=True, exist_ok=True)
    run = RunDir(root=root)
    run.update_status(state="starting")
    cfg = JobConfig(run_id="task011-petition-adjudications")
    return LoopSupervisor(cfg, run, root)


# ---------------------------------------------------------------------------
# engine.petitions: the write/read/digest layer, in isolation.
# ---------------------------------------------------------------------------

def test_record_confirmed_adjudications_writes_the_run_scope_file(tmp_path):
    run = RunDir(root=tmp_path / "run")
    petitions.record_confirmed_adjudications(run, [
        {"obligation": "Requirement B, DoD predicate 9",
         "adjudication": "CONFIRMED"},
    ])
    assert run.petition_adjudications_file.exists()
    assert run.petition_adjudications_file.parent == run.root
    entries, tampered = petitions.read_confirmed_adjudications(run)
    assert tampered is False
    assert entries == [{"obligation": "Requirement B, DoD predicate 9",
                         "adjudication": "CONFIRMED"}]


def test_refuted_entries_are_never_persisted(tmp_path):
    run = RunDir(root=tmp_path / "run")
    petitions.record_confirmed_adjudications(run, [
        {"obligation": "Some other requirement", "adjudication": "REFUTED",
         "remedy": "do X instead"},
    ])
    assert not run.petition_adjudications_file.exists()
    obligations, tampered = petitions.confirmed_obligations(run)
    assert obligations == set()
    assert tampered is False


def test_record_merges_rather_than_overwrites_across_calls(tmp_path):
    run = RunDir(root=tmp_path / "run")
    petitions.record_confirmed_adjudications(
        run, [{"obligation": "Obligation One", "adjudication": "CONFIRMED"}])
    petitions.record_confirmed_adjudications(
        run, [{"obligation": "Obligation Two", "adjudication": "CONFIRMED"}])
    obligations, tampered = petitions.confirmed_obligations(run)
    assert tampered is False
    assert obligations == {"obligation one", "obligation two"}


def test_downgrade_confirmed_findings_converts_blocking_to_residual():
    findings_list = [
        _finding("blocking", clause="Requirement B, DoD predicate 9"),
        _finding("blocking", clause="An unrelated clause", curable=False),
    ]
    confirmed = {"requirement b, dod predicate 9"}
    out = petitions.downgrade_confirmed_findings(findings_list, confirmed)
    # never mutates the input list or its dicts.
    assert findings_list[0]["severity"] == "blocking"
    assert out[0]["severity"] == "residual"
    assert out[1]["severity"] == "blocking"
    assert findings.compute_verdict(out) == "rejected"  # the OTHER finding still blocks


def test_downgrade_with_no_confirmed_obligations_is_a_no_op_copy():
    findings_list = [_finding("blocking")]
    out = petitions.downgrade_confirmed_findings(findings_list, set())
    assert out == findings_list
    assert out is not findings_list


# ---------------------------------------------------------------------------
# Survives `_archive_approach`: approach N+1 inherits every CONFIRMED
# adjudication (requirement C.3).
# ---------------------------------------------------------------------------

def test_confirmed_adjudication_survives_archive_approach(tmp_path):
    sup = _supervisor(tmp_path / "run")
    petitions.record_confirmed_adjudications(sup.run, [
        {"obligation": "Requirement B, DoD predicate 9",
         "adjudication": "CONFIRMED"},
    ])
    # populate the three files `_archive_approach` DOES move, to prove the
    # adjudication file is untouched by the same call that resets those.
    sup.run.tasks_file.write_text('{"tasks": []}')
    sup.run.notes_file.write_text("approach 1 notes\n")
    sup.run.findings_file.write_text("approach 1 findings\n")

    sup._archive_approach(1)

    assert (sup.run.root / "approaches" / "01" / "tasks.json").exists()
    # the adjudication record was never archived away -- it never lived
    # under approaches/NN/ in the first place.
    assert not (sup.run.root / "approaches" / "01" /
                "petition-adjudications.json").exists()
    obligations, tampered = petitions.confirmed_obligations(sup.run)
    assert tampered is False
    assert obligations == {"requirement b, dod predicate 9"}


def test_approach_n_plus_1_review_no_longer_sees_the_obligation_as_blocking(
        tmp_path):
    """The end-to-end proof requirement C.4 asks for: approach 1's review
    CONFIRMS a petition against an obligation; `_archive_approach` runs (the
    ordinary rejection path); approach 2's review, seeing the SAME clause
    as a fresh blocking finding, has it downgraded to residual because the
    run-scope record survived -- the obligation is out of the blocking set
    without review re-litigating it."""
    sup = _supervisor(tmp_path / "run")

    # Approach 1: review confirms the petition (verdict payload carries
    # `petition_adjudications`), rejecting on the SAME clause it just
    # confirmed is unachievable (an overcautious hat, or one that reports
    # the finding and the adjudication in the same pass).
    findings.write_findings_json(sup.run.findings_json_file, [
        _finding("blocking", clause="Requirement B, DoD predicate 9",
                  curable=False),
    ])
    result_1 = _result(json.dumps({
        "verdict": "rejected",
        "reason": "the obligation cannot be met",
        "findings_path": "review-findings.md",
        "petition_adjudications": [
            {"obligation": "Requirement B, DoD predicate 9",
             "adjudication": "CONFIRMED"},
        ],
    }))
    verified_1, payload_1, errors_1 = sup._review_verdict(result_1)
    assert errors_1 == []
    assert payload_1["petition_adjudications"][0]["adjudication"] == "CONFIRMED"

    # the CONFIRMED entry landed at run scope, and the SAME review's own
    # findings channel already reads it back downgraded -- the blocking
    # set that fed `verified_1` was already residual-only, so this
    # particular rejection self-corrects to verified (predicate 3's own
    # honest-recomputation rule); the interaction this test actually pins
    # is the NEXT approach's independent findings channel below.
    assert verified_1 is True

    sup.run.notes_file.write_text("approach 1 notes\n")
    sup.run.findings_file.write_text("approach 1 findings\n")
    sup._archive_approach(1)

    # Approach 2: a FRESH review, on a fresh findings channel, reports the
    # SAME clause as blocking again (it never re-litigates the petition --
    # it doesn't even know one was ever filed). The run-scope record from
    # approach 1 is what removes it from the blocking set this time.
    findings.write_findings_json(sup.run.findings_json_file, [
        _finding("blocking", clause="Requirement B, DoD predicate 9",
                  curable=False),
        _finding("blocking", clause="A second, unrelated finding",
                  curable=False),
    ])
    result_2 = _result(json.dumps({
        "verdict": "rejected",
        "reason": "two findings this time",
        "findings_path": "review-findings.md",
    }))
    verified_2, payload_2, errors_2 = sup._review_verdict(result_2)
    assert errors_2 == []
    # still rejected -- the SECOND, unrelated finding is still blocking --
    # but the confirmed obligation itself is out of the blocking set: this
    # is what `_review_findings_channel` (the one place the downgrade
    # happens) proves.
    assert verified_2 is False
    live_findings, live_errors = sup._review_findings_channel()
    assert live_errors == []
    by_clause = {f["clause"]: f["severity"] for f in live_findings}
    assert by_clause["Requirement B, DoD predicate 9"] == "residual"
    assert by_clause["A second, unrelated finding"] == "blocking"


# ---------------------------------------------------------------------------
# Tamper-evidence against the worker (requirement C.3): review writes the
# record (via `LoopSupervisor`/`engine.petitions` only); a worker-phase
# hand-edit is detectable and surfaced as a mandatory blocking finding.
# ---------------------------------------------------------------------------

def test_a_worker_phase_edit_is_detected_as_tampered(tmp_path):
    run = RunDir(root=tmp_path / "run")
    petitions.record_confirmed_adjudications(run, [
        {"obligation": "Requirement B, DoD predicate 9",
         "adjudication": "CONFIRMED"},
    ])
    # simulate a worker-phase hand-edit: add a fabricated CONFIRMED entry
    # directly on disk, WITHOUT going through `record_confirmed_
    # adjudications` (the one function that keeps entries+digest
    # consistent) -- exactly what a worker iteration with filesystem
    # access to the run dir could do.
    doc = read_json(run.petition_adjudications_file)
    doc["entries"].append({"obligation": "A fabricated exemption",
                            "adjudication": "CONFIRMED"})
    run.petition_adjudications_file.write_text(json.dumps(doc))

    obligations, tampered = petitions.confirmed_obligations(run)
    assert tampered is True
    # fails towards the all-blocking default: nothing is trusted as
    # confirmed once tampering is detected, not even the genuine entry.
    assert obligations == set()


def test_tamper_review_context_demands_a_blocking_finding():
    assert petitions.tamper_review_context(False) == ""
    ctx = petitions.tamper_review_context(True)
    assert "tampered" in ctx.lower()
    assert "BLOCKING" in ctx


def test_review_extra_context_surfaces_tamper_to_the_prompt(tmp_path):
    sup = _supervisor(tmp_path / "run")
    petitions.record_confirmed_adjudications(sup.run, [
        {"obligation": "Requirement B, DoD predicate 9",
         "adjudication": "CONFIRMED"},
    ])
    doc = read_json(sup.run.petition_adjudications_file)
    doc["digest"] = "not-the-real-digest"
    sup.run.petition_adjudications_file.write_text(json.dumps(doc))

    ctx = sup._review_extra_context()
    assert "tampered" in ctx.lower()
    assert "BLOCKING" in ctx

    prompt = sup.build_prompt("review", extra=ctx)
    assert "tampered" in prompt.lower()


def test_untampered_run_has_no_tamper_context(tmp_path):
    sup = _supervisor(tmp_path / "run")
    assert sup._petition_adjudications_tamper_context() == ""
    petitions.record_confirmed_adjudications(sup.run, [
        {"obligation": "Requirement B, DoD predicate 9",
         "adjudication": "CONFIRMED"},
    ])
    assert sup._petition_adjudications_tamper_context() == ""


# ---------------------------------------------------------------------------
# `_review_verdict` persists CONFIRMED entries as a side effect (never a
# worker/review hat process writing the file directly).
# ---------------------------------------------------------------------------

def test_review_verdict_persists_confirmed_petition_adjudications(tmp_path):
    sup = _supervisor(tmp_path / "run")
    result = _result(json.dumps({
        "verdict": "verified",
        "petition_adjudications": [
            {"obligation": "Requirement B, DoD predicate 9",
             "adjudication": "CONFIRMED"},
            {"obligation": "Some other requirement",
             "adjudication": "REFUTED", "remedy": "do X"},
        ],
    }))
    sup._review_verdict(result)
    obligations, tampered = petitions.confirmed_obligations(sup.run)
    assert tampered is False
    assert obligations == {"requirement b, dod predicate 9"}


def test_review_verdict_with_no_petition_adjudications_writes_nothing(tmp_path):
    sup = _supervisor(tmp_path / "run")
    result = _result(json.dumps({"verdict": "verified"}))
    sup._review_verdict(result)
    assert not sup.run.petition_adjudications_file.exists()
