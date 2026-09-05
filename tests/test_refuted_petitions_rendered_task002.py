"""Task 002 (approach 02, cure for finding 2 of the approach-01 review,
requirement B.1): a REFUTED petition adjudication -- and its remedy text
-- must appear in the run record, not just CONFIRMED ones.

The review's own scenario: `petitions.record_confirmed_adjudications`
deliberately drops REFUTED entries from its trusted, tamper/MAC-checked
exemption store (`docs/petition-format.md`: REFUTED "is not something a
later approach inherits"), and `run_record.build_petitions_section` used
to read ONLY that store -- so a REFUTED obligation, and the remedy review
stated for it, silently never reached `run-record.json`/`.md` at all.

This module pins the fix without loosening the CONFIRMED store: passing
a REFUTED entry through the same pipeline the engine actually uses
(`record_confirmed_adjudications` -- a no-op for a REFUTED entry, exactly
as before -- alongside `record_refuted_adjudications`, the plain witness
log that DOES persist it) must make the obligation and its remedy show up
in both the JSON section and the rendered markdown, while a genuinely
CONFIRMED entry is unaffected and the tamper/MAC behaviour of the
CONFIRMED store is untouched.
"""

from __future__ import annotations

from pathlib import Path

from ralphd.engine import run_record
from ralphd.engine.petitions import (
    record_confirmed_adjudications,
    record_refuted_adjudications,
)
from ralphd.engine.state import RunDir

OBLIGATION = "Requirement Q -- task 002's own fixture obligation"
REMEDY = "Run the missing sweep at the post-fix sha and record it."


def _run(tmp_path: Path) -> RunDir:
    return RunDir(root=tmp_path / "run")


def _refute(run: RunDir, obligation: str = OBLIGATION,
            remedy: str = REMEDY) -> None:
    adjudications = [
        {"obligation": obligation, "adjudication": "REFUTED", "remedy": remedy},
    ]
    # The exact pair the engine calls at review-verdict time
    # (`LoopSupervisor._review_verdict` / `_apply_refuted_petition_
    # adjudications`): the CONFIRMED-only store drops it (unchanged
    # behaviour -- pinned below), the witness log keeps it.
    record_confirmed_adjudications(run, adjudications)
    record_refuted_adjudications(run, adjudications)


def test_confirmed_store_still_drops_the_refuted_entry(tmp_path):
    run = _run(tmp_path)
    _refute(run)
    from ralphd.engine.petitions import read_confirmed_adjudications
    entries, tampered = read_confirmed_adjudications(run)
    assert entries == []
    assert tampered is False


def test_petitions_section_includes_the_refuted_obligation_and_remedy(tmp_path):
    run = _run(tmp_path)
    _refute(run)
    section = run_record.build_petitions_section(run)
    assert section["tampered"] is False
    assert section["entries"] == [
        {"obligation": OBLIGATION, "adjudication": "REFUTED",
         "remedy": REMEDY}]


def test_run_record_json_carries_the_refuted_entry(tmp_path, monkeypatch):
    run = _run(tmp_path)
    _refute(run)
    monkeypatch.setattr(
        run_record.state, "amendment_entries", lambda config_root: [])
    record = run_record.build_run_record(
        run, run_id="task002-refuted", max_approaches=3,
        config_root=tmp_path / "config")
    entries = record["petitions"]["entries"]
    assert any(e["obligation"] == OBLIGATION and e["adjudication"] == "REFUTED"
               and e["remedy"] == REMEDY for e in entries)


def test_run_record_markdown_renders_the_refuted_obligation_and_remedy(tmp_path, monkeypatch):
    run = _run(tmp_path)
    _refute(run)
    monkeypatch.setattr(
        run_record.state, "amendment_entries", lambda config_root: [])
    record = run_record.build_run_record(
        run, run_id="task002-refuted", max_approaches=3,
        config_root=tmp_path / "config")
    md = run_record.render_run_record_markdown(record)
    assert OBLIGATION in md
    assert "REFUTED" in md
    assert REMEDY in md


def test_confirmed_entry_still_renders_and_is_never_duplicated(tmp_path, monkeypatch):
    """A CONFIRMED entry must keep working exactly as before, and if the
    same obligation is ALSO witnessed as REFUTED (e.g. a duplicate/stale
    event), the CONFIRMED entry wins -- the obligation is rendered once,
    not twice."""
    run = _run(tmp_path)
    record_confirmed_adjudications(run, [
        {"obligation": OBLIGATION, "adjudication": "CONFIRMED"}])
    record_refuted_adjudications(run, [
        {"obligation": OBLIGATION, "adjudication": "REFUTED", "remedy": REMEDY}])
    section = run_record.build_petitions_section(run)
    assert section["entries"] == [
        {"obligation": OBLIGATION, "adjudication": "CONFIRMED"}]

    monkeypatch.setattr(
        run_record.state, "amendment_entries", lambda config_root: [])
    record = run_record.build_run_record(
        run, run_id="task002-both", max_approaches=3,
        config_root=tmp_path / "config")
    md = run_record.render_run_record_markdown(record)
    assert md.count(OBLIGATION) == 1


def test_no_petitions_still_renders_none(tmp_path):
    run = _run(tmp_path)
    section = run_record.build_petitions_section(run)
    assert section == {"entries": [], "tampered": False}
