"""Task 016 (approach 02, cure for task 002; review finding 2, B.1): a
REFUTED petition adjudication -- and the remedy review stated for it --
must reach the run record through `petitions.record_confirmed_
adjudications` ITSELF, with no second, caller-side write.

The review's reproduction, verbatim: "a valid REFUTED entry passed to
`petitions.record_confirmed_adjudications`, then the section built". Task
002 satisfied it only because its test ALSO called
`record_refuted_adjudications` (the plain witness log) -- the trusted
store still dropped REFUTED entries, so the render depended on a caller
doing the second call. This module therefore calls exactly the two
functions the criterion names -- `record_confirmed_adjudications` and
`run_record.build_petitions_section` (via `build_run_record`) -- and
NOTHING else, and asserts:

* the refuted obligation and its remedy text appear in `run-record.json`
  and in the rendered `run-record.md`;
* the store persists the REFUTED entry under the same keyed MAC (one
  record, one write path, `read_adjudications` reads it back
  authenticated);
* `read_confirmed_adjudications` -- the exemption read -- still yields
  CONFIRMED entries ONLY, so a REFUTED entry in the store never exempts
  an obligation (`confirmed_obligations`, `petitioned_task_accepted`,
  `downgrade_confirmed_findings`);
* the tamper/MAC behaviour of that store is unchanged: an edit to the
  REFUTED entry is detected exactly as an edit to a CONFIRMED one is, and
  the record then reads empty and tampered.
"""

from __future__ import annotations

import json
from pathlib import Path

from ralphd.engine import petitions, run_record
from ralphd.engine.petitions import (
    read_adjudications,
    read_confirmed_adjudications,
    record_confirmed_adjudications,
)
from ralphd.engine.state import RunDir, read_json

OBLIGATION = "Requirement Z.9 -- task 016's own fixture obligation"
REMEDY = ("Re-run the docker lane at the post-fix sha and record its exit "
          "code in the lane record.")
CONFIRMED_OBLIGATION = "Requirement Y.1 -- a genuinely confirmed obligation"


def _run(tmp_path: Path) -> RunDir:
    return RunDir(root=tmp_path / "run")


def _refuted(obligation: str = OBLIGATION, remedy: str = REMEDY) -> list[dict]:
    return [{"obligation": obligation, "adjudication": "REFUTED",
             "remedy": remedy}]


def _record(run: RunDir, tmp_path: Path, monkeypatch) -> dict:
    monkeypatch.setattr(
        run_record.state, "amendment_entries", lambda config_root: [])
    return run_record.build_run_record(
        run, run_id="task016-refuted", max_approaches=3,
        config_root=tmp_path / "config")


# ---------------------------------------------------------------------------
# The review's scenario, with NO second writer: record_confirmed_
# adjudications, then the section.
# ---------------------------------------------------------------------------

def test_refuted_entry_is_persisted_by_record_confirmed_adjudications(tmp_path):
    run = _run(tmp_path)
    record_confirmed_adjudications(run, _refuted())
    assert run.petition_adjudications_file.exists()
    entries, tampered = read_adjudications(run)
    assert tampered is False
    assert entries == [{"obligation": OBLIGATION, "adjudication": "REFUTED",
                        "remedy": REMEDY}]
    # One store, one keyed MAC -- the same write path CONFIRMED entries use.
    doc = read_json(run.petition_adjudications_file)
    assert isinstance(doc.get("mac"), str) and doc["mac"]
    assert doc["digest"] == petitions._adjudications_digest(doc["entries"])


def test_petitions_section_carries_the_refuted_adjudication_and_remedy(tmp_path):
    run = _run(tmp_path)
    record_confirmed_adjudications(run, _refuted())
    section = run_record.build_petitions_section(run)
    assert section["tampered"] is False
    assert section["entries"] == [
        {"obligation": OBLIGATION, "adjudication": "REFUTED",
         "remedy": REMEDY}]


def test_run_record_json_carries_the_refuted_obligation_and_remedy(
        tmp_path, monkeypatch):
    run = _run(tmp_path)
    record_confirmed_adjudications(run, _refuted())
    record = _record(run, tmp_path, monkeypatch)
    entries = record["petitions"]["entries"]
    assert any(e["obligation"] == OBLIGATION
               and e["adjudication"] == "REFUTED"
               and e["remedy"] == REMEDY for e in entries), entries
    # ... and it survives the JSON round trip `run-record.json` itself is.
    round_tripped = json.loads(json.dumps(record))
    assert round_tripped["petitions"]["entries"] == entries


def test_run_record_markdown_renders_the_refuted_obligation_and_remedy(
        tmp_path, monkeypatch):
    run = _run(tmp_path)
    record_confirmed_adjudications(run, _refuted())
    md = run_record.render_run_record_markdown(
        _record(run, tmp_path, monkeypatch))
    assert OBLIGATION in md
    assert f"{OBLIGATION} -- REFUTED" in md
    assert REMEDY in md


def test_confirmed_and_refuted_both_render_once_each(tmp_path, monkeypatch):
    run = _run(tmp_path)
    record_confirmed_adjudications(run, [
        {"obligation": CONFIRMED_OBLIGATION, "adjudication": "CONFIRMED"},
        *_refuted(),
    ])
    md = run_record.render_run_record_markdown(
        _record(run, tmp_path, monkeypatch))
    assert md.count(OBLIGATION) == 1
    assert md.count(CONFIRMED_OBLIGATION) == 1
    assert f"{CONFIRMED_OBLIGATION} -- CONFIRMED" in md


# ---------------------------------------------------------------------------
# Criterion (1)'s other half: exemptions stay CONFIRMED-only, and the
# store's tamper/MAC behaviour is unchanged.
# ---------------------------------------------------------------------------

def test_a_refuted_entry_in_the_store_never_exempts_an_obligation(tmp_path):
    run = _run(tmp_path)
    record_confirmed_adjudications(run, _refuted())
    entries, tampered = read_confirmed_adjudications(run)
    assert entries == []
    assert tampered is False
    obligations, tampered = petitions.confirmed_obligations(run)
    assert obligations == set()
    assert tampered is False
    # ... so a finding naming that obligation stays BLOCKING, and a
    # `petitioned` task naming it is not "accepted".
    finding = {"severity": "blocking", "curable": True,
               "clause": OBLIGATION, "evidence": "fixture evidence"}
    out = petitions.downgrade_confirmed_findings([finding], obligations)
    assert out[0]["severity"] == "blocking"
    task = {"id": "016", "status": "petitioned",
            "title": OBLIGATION, "successCriteria": OBLIGATION}
    assert petitions.petitioned_task_accepted(task, obligations) is False


def test_confirmed_exemption_still_granted_alongside_a_refuted_entry(tmp_path):
    run = _run(tmp_path)
    record_confirmed_adjudications(run, [
        {"obligation": CONFIRMED_OBLIGATION, "adjudication": "CONFIRMED"},
        *_refuted(),
    ])
    entries, tampered = read_confirmed_adjudications(run)
    assert tampered is False
    assert entries == [{"obligation": CONFIRMED_OBLIGATION,
                        "adjudication": "CONFIRMED"}]
    obligations, _ = petitions.confirmed_obligations(run)
    assert obligations == {CONFIRMED_OBLIGATION.strip().lower()}


def test_a_later_refuted_entry_never_downgrades_a_confirmed_one(tmp_path):
    run = _run(tmp_path)
    record_confirmed_adjudications(run, [
        {"obligation": CONFIRMED_OBLIGATION, "adjudication": "CONFIRMED"}])
    record_confirmed_adjudications(
        run, _refuted(obligation=CONFIRMED_OBLIGATION, remedy="ignored"))
    entries, tampered = read_adjudications(run)
    assert tampered is False
    assert entries == [{"obligation": CONFIRMED_OBLIGATION,
                        "adjudication": "CONFIRMED"}]
    obligations, _ = petitions.confirmed_obligations(run)
    assert obligations == {CONFIRMED_OBLIGATION.strip().lower()}


def test_editing_the_refuted_entry_is_detected_as_tampering(tmp_path):
    run = _run(tmp_path)
    record_confirmed_adjudications(run, _refuted())
    doc = read_json(run.petition_adjudications_file)
    doc["entries"][0]["adjudication"] = "CONFIRMED"
    doc["digest"] = petitions._adjudications_digest(doc["entries"])
    run.petition_adjudications_file.write_text(json.dumps(doc))
    entries, tampered = read_adjudications(run)
    assert tampered is True
    assert entries == []
    # The exemption read fails closed the same way, and the detection is
    # sticky for the rest of the run.
    obligations, tampered = petitions.confirmed_obligations(run)
    assert obligations == set()
    assert tampered is True
    section = run_record.build_petitions_section(run)
    assert section["tampered"] is True
    assert section["entries"] == []


def test_nothing_adjudicated_writes_nothing_and_renders_none(tmp_path):
    run = _run(tmp_path)
    record_confirmed_adjudications(run, [])
    record_confirmed_adjudications(run, [{"obligation": "  ",
                                          "adjudication": "REFUTED"}])
    record_confirmed_adjudications(run, [{"obligation": "x",
                                          "adjudication": "PENDING"}])
    assert not run.petition_adjudications_file.exists()
    assert run_record.build_petitions_section(run) == {
        "entries": [], "tampered": False}
