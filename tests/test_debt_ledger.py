"""Task 006 (PRD requirement A.3, engine half, #155): the debt ledger.

A verifier that verifies a task but leaves structured `notes` on the payload
(task 005) is recording a clause the deliverable met only in form, not in
substance. This module pins that those notes are ALSO appended, through the
ONE write path `engine.state.record_debt_entries` (bound as
`RunDir.record_debt`), to a per-run debt ledger:

  * the write path itself -- task-scoped ids (`"<task>/<n>"`), the entry
    shape, the no-op case, and that `debt.json`/`debt.md` can never disagree
    about what the ledger holds;
  * `_verify_task` calls it, in the same pass that persists `verifyNotes`
    (task 005), never a second write site;
  * the run record renders the same ledger's open and resolved split;
  * `ralphctl docs <run>` lists `debt.md` and the hub's document panel
    serves it -- both through `state.RUN_DOCUMENTS`, the one shaping every
    other document surface already goes through.

Unit-level throughout: `_verify_task` is exercised with `_run_iteration_once`
stubbed (the same seam `tests/test_verify_notes.py` uses), and the CLI/hub
surfaces are exercised through the shared shaping functions
(`state.run_documents`, `ui_server.document_list`/`document_view`) rather
than a live server or a live engine.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
from pathlib import Path

from ralphd.engine import run_record
from ralphd.engine.config import JobConfig
from ralphd.engine.loop import LoopSupervisor
from ralphd.engine.runner import IterationResult
from ralphd.engine.state import (
    DEBT_FILE,
    DEBT_JSON_FILE,
    DEBT_STATUS_OPEN,
    DEBT_STATUS_RESOLVED,
    RunDir,
    atomic_write_json,
    read_debt_entries,
    record_debt_entries,
    render_debt_markdown,
    run_document_keys,
    run_documents,
)
from tests.conftest import RALPHCTL

NOTE_A = {"clause": "exact command text", "observed": "ran a wrapper script",
          "suggested": "name the observable instead of the command"}
NOTE_B = {"clause": "polling cadence", "observed": "hard-coded 4s",
          "suggested": "no criterion should pin a cadence"}


# ---------------------------------------------------------------------------
# The write path (state.record_debt_entries / RunDir.record_debt)
# ---------------------------------------------------------------------------

def test_record_debt_entries_appends_task_scoped_ids(tmp_path):
    entries = record_debt_entries(tmp_path, "006", [NOTE_A, NOTE_B])
    assert [e["id"] for e in entries] == ["006/1", "006/2"]
    for e, note in zip(entries, (NOTE_A, NOTE_B)):
        assert e["taskId"] == "006"
        assert e["clause"] == note["clause"]
        assert e["observed"] == note["observed"]
        assert e["suggested"] == note["suggested"]
        assert e["status"] == DEBT_STATUS_OPEN
        assert e["resolvedBy"] is None
        assert e["ts"]

    on_disk = read_debt_entries(tmp_path)
    assert on_disk == entries
    assert (tmp_path / DEBT_JSON_FILE).exists()
    assert (tmp_path / DEBT_FILE).exists()


def test_record_debt_entries_continues_the_per_task_sequence(tmp_path):
    record_debt_entries(tmp_path, "006", [NOTE_A])
    second = record_debt_entries(tmp_path, "006", [NOTE_B])
    assert [e["id"] for e in second] == ["006/1", "006/2"]
    # A different task id starts its own sequence at 1, not the global count.
    third = record_debt_entries(tmp_path, "007", [NOTE_A])
    assert third[-1]["id"] == "007/1"


def test_record_debt_entries_is_a_noop_for_empty_or_non_list_notes(tmp_path):
    assert record_debt_entries(tmp_path, "006", []) == []
    assert record_debt_entries(tmp_path, "006", None) == []
    assert not (tmp_path / DEBT_JSON_FILE).exists()
    assert not (tmp_path / DEBT_FILE).exists()


def test_debt_json_and_debt_md_never_disagree(tmp_path):
    record_debt_entries(tmp_path, "006", [NOTE_A])
    entries = read_debt_entries(tmp_path)
    md = (tmp_path / DEBT_FILE).read_text()
    assert md == render_debt_markdown(entries)
    assert NOTE_A["clause"] in md
    assert "006/1" in md


# ---------------------------------------------------------------------------
# The render: open and resolved
# ---------------------------------------------------------------------------

def test_render_debt_markdown_splits_open_and_resolved():
    entries = [
        {"id": "006/1", "taskId": "006", "clause": "c1", "observed": "o1",
         "suggested": "s1", "ts": "2026-01-01T00:00:00Z",
         "status": DEBT_STATUS_OPEN, "resolvedBy": None},
        {"id": "006/2", "taskId": "006", "clause": "c2", "observed": "o2",
         "suggested": "s2", "ts": "2026-01-02T00:00:00Z",
         "status": DEBT_STATUS_RESOLVED, "resolvedBy": "task 008"},
    ]
    md = render_debt_markdown(entries)
    open_section, _, resolved_section = md.partition("## Resolved")
    assert "006/1" in open_section and "006/2" not in open_section
    assert "006/2" in resolved_section
    assert "resolved by task 008" in resolved_section


def test_render_debt_markdown_of_an_empty_ledger_says_none():
    md = render_debt_markdown([])
    assert "## Open (0)" in md and "## Resolved (0)" in md
    assert md.count("(none)") == 2


# ---------------------------------------------------------------------------
# _verify_task calls the ONE write path, in the same pass as verifyNotes
# ---------------------------------------------------------------------------

def _verify_supervisor(tmp_path, task: dict) -> LoopSupervisor:
    run = RunDir(root=tmp_path)
    atomic_write_json(run.tasks_file, {"version": 1, "tasks": [task]})
    cfg = JobConfig(run_id="unit", vigilant=True,
                    infra_retry_backoff_s=[0.0],
                    infra_retry_backoff_max_s=0.0,
                    infra_outage_budget_s=1000.0)
    return LoopSupervisor(cfg, run, tmp_path)


def _stub_once(sup: LoopSupervisor, result: IterationResult):
    async def fake_once(phase, extra="", prompt_name=None, tier=None):
        return result

    async def fake_backoff(seconds):
        return seconds, False

    sup._run_iteration_once = fake_once  # type: ignore[method-assign]
    sup._wait_out_backoff = fake_backoff  # type: ignore[method-assign]


def test_verify_task_with_notes_appends_them_to_the_debt_ledger(tmp_path):
    task = {"id": "006", "title": "t", "status": "completed",
            "successCriteria": "c"}
    sup = _verify_supervisor(tmp_path, task)
    notes = [NOTE_A, NOTE_B]
    result = IterationResult(exit_code=0)
    result.duration_s = 5.0
    result.final_text = json.dumps(
        {"task_id": "006", "verdict": "verified", "notes": notes})
    _stub_once(sup, result)

    verified = asyncio.run(sup._verify_task(task))

    assert verified is True
    t = sup.run.read_tasks()["tasks"][0]
    assert t["verifyNotes"] == notes

    entries = read_debt_entries(tmp_path)
    assert [e["id"] for e in entries] == ["006/1", "006/2"]
    assert [e["clause"] for e in entries] == [NOTE_A["clause"], NOTE_B["clause"]]
    assert all(e["status"] == DEBT_STATUS_OPEN for e in entries)
    assert (tmp_path / DEBT_FILE).exists()


def test_verify_task_without_notes_leaves_the_ledger_untouched(tmp_path):
    task = {"id": "006", "title": "t", "status": "completed",
            "successCriteria": "c"}
    sup = _verify_supervisor(tmp_path, task)
    result = IterationResult(exit_code=0)
    result.duration_s = 5.0
    result.final_text = json.dumps({"task_id": "006", "verdict": "verified"})
    _stub_once(sup, result)

    verified = asyncio.run(sup._verify_task(task))

    assert verified is True
    assert read_debt_entries(tmp_path) == []
    assert not (tmp_path / DEBT_JSON_FILE).exists()


# ---------------------------------------------------------------------------
# The run record renders open and resolved debt
# ---------------------------------------------------------------------------

def test_run_record_renders_open_and_resolved_debt(tmp_path):
    run = RunDir(root=tmp_path / "run")
    record_debt_entries(run.root, "006", [NOTE_A])
    entries = read_debt_entries(run.root)
    entries[0]["status"] = DEBT_STATUS_RESOLVED
    entries[0]["resolvedBy"] = "task 008"
    atomic_write_json(run.debt_json_file, entries)

    record_debt_entries(run.root, "007", [NOTE_B])

    record = run_record.build_run_record(
        run, run_id="unit-run", max_approaches=1, config_root=tmp_path / "cfg")
    assert set(record) == set(run_record.RECORD_SECTIONS)
    assert [e["id"] for e in record["debt"]["open"]] == ["007/1"]
    assert [e["id"] for e in record["debt"]["resolved"]] == ["006/1"]

    md = run_record.render_run_record_markdown(record)
    assert "## Debt" in md
    assert "007/1" in md and "006/1" in md
    assert "resolved by task 008" in md


# ---------------------------------------------------------------------------
# `ralphctl docs <run>` lists debt.md
# ---------------------------------------------------------------------------

def test_run_documents_lists_debt_md(tmp_path):
    root = tmp_path / "run"
    root.mkdir()
    record_debt_entries(root, "006", [NOTE_A])
    docs = {d["key"]: d for d in run_documents(root)}
    assert "debt" in run_document_keys()
    assert docs["debt"]["name"] == DEBT_FILE
    assert docs["debt"]["exists"] is True
    assert NOTE_A["clause"] in docs["debt"]["body"]


def test_run_documents_lists_debt_as_absent_before_any_write(tmp_path):
    root = tmp_path / "run"
    root.mkdir()
    docs = {d["key"]: d for d in run_documents(root)}
    assert docs["debt"]["exists"] is False
    assert docs["debt"]["available"] is True


def test_ralphctl_docs_prints_the_debt_row(tmp_path):
    registry = tmp_path / "registry"
    rdir = registry / "runs" / "debt-run"
    cdir = registry / "configs" / "debt-run"
    (rdir / "iterations").mkdir(parents=True)
    cdir.mkdir(parents=True)
    (rdir / "status.json").write_text(json.dumps(
        {"runId": "debt-run", "state": "failed"}))
    (rdir / "prd.md").write_text("# PRD\n")
    (cdir / "job.yaml").write_text("run_id: \"debt-run\"\n")
    record_debt_entries(rdir, "006", [NOTE_A])

    env = {**os.environ, "RALPHD_REGISTRY": str(registry)}
    res = subprocess.run([str(RALPHCTL), "docs", "debt-run"], env=env,
                         capture_output=True, text=True, timeout=60)
    assert res.returncode == 0, res.stderr
    assert "debt" in res.stdout
    assert "debt.md" in res.stdout

    res_body = subprocess.run([str(RALPHCTL), "docs", "debt-run", "debt"],
                              env=env, capture_output=True, text=True,
                              timeout=60)
    assert res_body.returncode == 0, res_body.stderr
    assert NOTE_A["clause"] in res_body.stdout


# ---------------------------------------------------------------------------
# The hub links debt.md through the same document surface
# ---------------------------------------------------------------------------

def test_hub_document_list_and_view_include_debt(tmp_path):
    from ralphd.cli.ui_server import document_list, document_view

    registry = tmp_path / "registry"
    rdir = registry / "runs" / "debt-run"
    cdir = registry / "configs" / "debt-run"
    rdir.mkdir(parents=True)
    cdir.mkdir(parents=True)
    (cdir / "job.yaml").write_text("run_id: \"debt-run\"\n")
    record_debt_entries(rdir, "006", [NOTE_A])

    listing = document_list(registry, "debt-run")
    keys = [d["key"] for d in listing["documents"]]
    assert "debt" in keys
    by_key = {d["key"]: d for d in listing["documents"]}
    assert by_key["debt"]["exists"] is True
    assert "body" not in by_key["debt"]  # listing never carries bodies

    view = document_view(registry, "debt-run", "debt")
    assert view is not None
    assert NOTE_A["clause"] in view["text"]
    assert view["key"] == "debt"
