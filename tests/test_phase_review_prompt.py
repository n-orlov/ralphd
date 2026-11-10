"""PRD v0.37 requirement C.1: the `## Phase review` section of a phase-scoped
review prompt, and its absence from a final review on a plan without phases."""

from __future__ import annotations

import subprocess
from pathlib import Path

from ralphd.engine.state import atomic_write_json

from tests.conftest import supervisor_factory

_supervisor = supervisor_factory(run_id="phase-review-prompt",
                                 run_root="run", workspace="workspace")

HEADING = "## Phase review"


def _git(ws: Path, *args) -> str:
    return subprocess.run(
        ["git", "-C", str(ws), "-c", "user.name=t", "-c", "user.email=t@e.x",
         *args], check=True, capture_output=True, text=True).stdout.strip()


def _init_git(ws: Path) -> str:
    ws.mkdir(parents=True, exist_ok=True)
    _git(ws, "init", "-q")
    (ws / "a.txt").write_text("one\n")
    _git(ws, "add", "a.txt")
    _git(ws, "commit", "-q", "-m", "first")
    return _git(ws, "rev-parse", "HEAD")


def _phased_doc(start_head: str) -> dict:
    return {
        "version": 1,
        "phases": [
            {"id": "alpha", "title": "Alpha title", "reviewFocus": "focus-alpha",
             "gate": "closed", "rounds": 0, "startHead": start_head,
             "records": []},
            {"id": "beta", "title": "Beta title", "reviewFocus": "focus-beta",
             "gate": "closed", "rounds": 0, "startHead": None, "records": []}],
        "tasks": [
            {"id": "101", "title": "first", "phase": "alpha",
             "status": "validated", "successCriteria": "criteria-101-text",
             "verifyNotes": [{"note": "verify-note-101"}],
             "validationNotes": "validation-note-101"},
            {"id": "102", "title": "second", "phase": "alpha",
             "status": "failed", "successCriteria": "criteria-102-text",
             "verifyNotes": [{"note": "verify-note-102"}],
             "validationNotes": "validation-note-102"},
            {"id": "201", "title": "later", "phase": "beta",
             "status": "pending", "successCriteria": "criteria-201-text"}]}


def _phase_prompt(tmp_path):
    sup = _supervisor(tmp_path, approach=1)
    start = _init_git(sup.workspace)
    atomic_write_json(sup.run.tasks_file, _phased_doc(start))
    _git(sup.workspace, "commit", "--allow-empty", "-q", "-m", "x")
    new_head = _git(sup.workspace, "rev-parse", "HEAD")
    sup._phase_review_scope = {"planPhase": "alpha", "round": 1,
                               "startHead": start}
    prompt = sup.build_prompt("review", extra=sup._review_extra_context())
    return prompt, start, new_head


def test_the_section_carries_the_phase_both_tasks_the_range_and_the_four_rules(tmp_path):
    prompt, start, head = _phase_prompt(tmp_path)
    assert HEADING in prompt
    section = prompt[prompt.index(HEADING):]
    for text in ("alpha", "Alpha title", "focus-alpha"):
        assert text in section
    for tid in ("101", "102"):
        for text in (f"criteria-{tid}-text", f"verify-note-{tid}",
                     f"validation-note-{tid}"):
            assert text in section
    assert "status: validated" in section and "status: failed" in section
    # nothing of the later phase's task, nor its focus
    assert "criteria-201-text" not in section and "focus-beta" not in section
    assert f"{start}..{head}" in section
    # the four judging rules of C.1
    assert "Judge only the PRD requirements this phase's tasks claim" in section
    assert "live workspace and anything the phase produced that still exists" \
        in section
    assert "Do not judge later phases' work" in section
    for word in ("severity", "curable", "clause", "cured_when"):
        assert word in section.split("### Rules")[1]
    assert "normal `review` contract" in section


def test_the_section_is_absent_from_a_final_review_on_a_plan_without_phases(tmp_path):
    sup = _supervisor(tmp_path, approach=1)
    _init_git(sup.workspace)
    atomic_write_json(sup.run.tasks_file, {"version": 1, "tasks": [
        {"id": "001", "title": "t", "status": "completed",
         "successCriteria": "c"}]})
    prompt = sup.build_prompt("review", extra=sup._review_extra_context())
    assert HEADING not in prompt
    assert "Phase review" not in prompt
