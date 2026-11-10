"""#313 (PRD v0.37 G.4): browser-tier proof that the hub's run-detail task
list is grouped by plan phase.

A two-phase plan shows two task groups, each header carrying the phase's gate
state and rounds and linking each phase record's `.md` artifact; a plan with no
phases renders the flat table it always did.

Marked `@pytest.mark.browser` for the same reason as `test_browser_hub.py`.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
from test_browser_hub import (  # noqa: F401
    ARTIFACTS_DIR,
    Pw,
    _skip_reason,
    _wait_for,
    pw,
)
from test_cli_ui import UiServer, _write_dead_run

SCREENSHOTS_DIR = ARTIFACTS_DIR / "screenshots" / "hub"

pytestmark = [pytest.mark.browser, pytest.mark.skipif(_skip_reason() is not None, reason=_skip_reason() or "")]

RECORD_MD = "phase-reviews/p1-r2-review.md"
RECORD_JSON = "phase-reviews/p1-r2-review.json"
RECORD_TEXT = "# Phase p1 review (round 2)\n\nNo findings.\n"


def _phased_plan() -> dict:
    return {
        "phases": [
            {"id": "p1", "title": "Foundations", "gate": "passed", "rounds": 2,
             "startHead": "abc", "records": [f"artifacts/{RECORD_JSON}", f"artifacts/{RECORD_MD}"]},
            {"id": "p2", "title": "Features", "gate": "closed", "rounds": 0,
             "startHead": None, "records": []},
        ],
        "tasks": [
            {"id": "001", "title": "lay the base", "status": "completed", "phase": "p1"},
            {"id": "002", "title": "wire the base", "status": "completed", "phase": "p1"},
            {"id": "003", "title": "add a feature", "status": "pending", "phase": "p2"},
        ],
    }


def _headers_js() -> str:
    return ("JSON.stringify([...document.querySelectorAll('#task-box .phase-group-header')]"
            ".map((r) => ({id: r.dataset.phaseId, gate: r.dataset.phaseGate, text: r.innerText})))")


def test_two_phase_plan_shows_two_task_groups_with_gate_rounds_and_record_link(tmp_path, pw):
    registry = tmp_path / "registry"
    run_dir = _write_dead_run(registry, "run-phased", state="aborted", verdict="unverified")
    (run_dir / "tasks.json").write_text(json.dumps(_phased_plan()))
    (run_dir / "artifacts" / "phase-reviews").mkdir(parents=True)
    (run_dir / "artifacts" / RECORD_MD).write_text(RECORD_TEXT)
    (run_dir / "artifacts" / RECORD_JSON).write_text("{}")
    server = UiServer(registry)
    server.wait_ready()
    try:
        pw.open(f"{server.base}/#/run/run-phased")
        _wait_for(pw, "document.getElementById('task-box').innerText", "add a feature")
        headers = json.loads(json.loads(pw.eval_js(_headers_js())))
        assert [h["id"] for h in headers] == ["p1", "p2"]
        assert headers[0]["gate"] == "passed" and headers[1]["gate"] == "closed"
        assert "Foundations" in headers[0]["text"] and "gate passed" in headers[0]["text"]
        assert "rounds 2" in headers[0]["text"]
        assert "Features" in headers[1]["text"] and "gate closed" in headers[1]["text"]
        assert "rounds 0" in headers[1]["text"]
        # rows sit under their own phase's header, in plan order
        order = json.loads(json.loads(pw.eval_js(
            "JSON.stringify([...document.querySelectorAll('#task-box tbody tr')]"
            ".map((r) => r.dataset.phaseId || r.dataset.taskId))")))
        assert order == ["p1", "001", "002", "p2", "003"]
        # exactly the one phase record, as a link to its .md artifact
        links = json.loads(json.loads(pw.eval_js(
            "JSON.stringify([...document.querySelectorAll('#task-box a.phase-record')]"
            ".map((a) => [a.dataset.artifact, a.getAttribute('href'), "
            "a.closest('tr').dataset.phaseId]))")))
        assert len(links) == 1
        assert links[0][0] == RECORD_MD and links[0][2] == "p1"
        assert links[0][1].endswith("/artifacts-download/" + "phase-reviews%2Fp1-r2-review.md")
        pw.screenshot(SCREENSHOTS_DIR / "plan-phases-grouped.png")
        pw.click("#task-box a.phase-record")
        _wait_for(pw, "document.querySelector('dialog[open]') ? "
                      "document.querySelector('dialog[open]').innerText : ''", "Phase p1 review")
    finally:
        server.stop()


def test_a_plan_with_no_phases_renders_no_group_headers(tmp_path, pw):
    registry = tmp_path / "registry"
    run_dir = _write_dead_run(registry, "run-unphased", state="aborted", verdict="unverified")
    plan = _phased_plan()
    (run_dir / "tasks.json").write_text(json.dumps({"tasks": [
        {k: v for k, v in t.items() if k != "phase"} for t in plan["tasks"]]}))
    server = UiServer(registry)
    server.wait_ready()
    try:
        pw.open(f"{server.base}/#/run/run-unphased")
        _wait_for(pw, "document.getElementById('task-box').innerText", "add a feature")
        assert json.loads(pw.eval_js("document.querySelectorAll('#task-box .phase-group-header').length")) == 0
        assert json.loads(pw.eval_js("document.querySelectorAll('#task-box a.phase-record').length")) == 0
        assert json.loads(pw.eval_js("document.querySelectorAll('#task-box tbody tr.task-row').length")) == 3
        assert json.loads(pw.eval_js("document.querySelectorAll('#task-box tbody tr').length")) == 3
    finally:
        server.stop()
