"""PRD v0.37 F.2: every `status.json` the engine publishes carries
`planPhase` -- the active phase of the persisted plan as the specified
object, or an explicit `null` when there is no phased plan -- including
initial planning and a resumed run dir before any task write.

These tests read the RAW status file (`json.loads` of the bytes on disk), never
`RunDir.read_status().get("planPhase")` with a default, so a missing key
cannot pass for `null`."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from ralphd.engine.state import REMOVE_STATUS_KEY, RunDir, atomic_write_json

from tests.conftest import scripted_supervisor
from tests.test_phase_review_dispatch import (
    _init_workspace, _ok, _plan, _worker)

_supervisor = scripted_supervisor(
    run_id="plan-phase-status-publish", run_root="run", workspace="workspace",
    iterations=40, max_approaches=1, vigilant=False, on_complete="exit",
    infra_outage_budget_s=1000.0, operator_input_needed_pause=False)

_PHASES = [
    {"id": "one", "title": "Phase one", "reviewFocus": "f1",
     "gate": "passed", "records": []},
    {"id": "two", "title": "Phase two", "reviewFocus": "f2",
     "gate": "closed", "records": []}]
_TWO = {"id": "two", "title": "Phase two", "index": 2, "count": 2,
        "gate": "closed"}


def _raw(run: RunDir) -> dict:
    return json.loads(Path(run.status_file).read_text())


def _tasks(phases) -> dict:
    doc = {"version": 1, "tasks": [
        {"id": "001", "title": "t1", "status": "completed",
         "successCriteria": "c", "dependsOn": []},
        {"id": "002", "title": "t2", "status": "pending",
         "successCriteria": "c", "dependsOn": []}]}
    if phases:
        doc["phases"] = [dict(p) for p in phases]
        doc["tasks"][0]["phase"] = "one"
        doc["tasks"][1]["phase"] = "two"
    return doc


def _seed(tmp_path, *, phases, status: dict | None) -> RunDir:
    """A run dir as an engine finds it on resume: a plan written by an older
    process (straight onto the view file, no `write_tasks`) and a status file
    that may or may not carry the key."""
    run = RunDir(root=tmp_path)
    atomic_write_json(run.tasks_file, _tasks(phases))
    if status is not None:
        atomic_write_json(run.status_file, status)
    return run


def test_the_very_first_status_write_has_an_explicit_null_before_any_plan(tmp_path):
    run = RunDir(root=tmp_path)
    assert not run.tasks_file.exists()
    run.update_status(state="starting")
    raw = _raw(run)
    assert "planPhase" in raw and raw["planPhase"] is None


def test_a_resumed_run_dir_without_phases_publishes_null_on_its_next_write(tmp_path):
    run = _seed(tmp_path, phases=None,
                status={"runId": "r", "state": "running", "schemaVersion": 1})
    assert "planPhase" not in _raw(run)          # as an older engine left it
    run.update_status(state="running", phase="worker")
    raw = _raw(run)
    assert "planPhase" in raw and raw["planPhase"] is None


def test_a_resumed_phased_run_dir_publishes_the_object_before_any_task_write(tmp_path):
    run = _seed(tmp_path, phases=_PHASES,
                status={"runId": "r", "state": "running", "schemaVersion": 1})
    run.update_status(iterationsUsed=4)
    assert _raw(run)["planPhase"] == _TWO


def test_a_stale_plan_phase_never_outlives_the_plan_it_described(tmp_path):
    # (a case the finding did not list) a status that still names a phase of
    # a plan that is gone -- a fresh approach before its planning pass wrote
    # tasks.json -- publishes null, not the old object
    run = _seed(tmp_path, phases=None, status={"state": "running",
                                                "planPhase": _TWO})
    run.tasks_file.unlink()
    run.update_status(phase="planning")
    raw = _raw(run)
    assert "planPhase" in raw and raw["planPhase"] is None


def test_no_patch_can_drop_or_override_the_published_plan_phase(tmp_path):
    # (a case the finding did not list) the key is derived at the one write
    # site: removing it or forging it through the patch changes nothing
    run = _seed(tmp_path, phases=_PHASES, status={"state": "running"})
    run.update_status(planPhase=REMOVE_STATUS_KEY)
    assert _raw(run)["planPhase"] == _TWO
    run.update_status(planPhase={"id": "forged"})
    assert _raw(run)["planPhase"] == _TWO


def test_every_publication_during_a_phased_run_carries_the_key(tmp_path):
    _init_workspace(tmp_path / "workspace")
    run = RunDir(root=tmp_path / "run")
    run.update_status(state="starting")
    sup = _supervisor(tmp_path, {
        "planning": [_plan], "worker": [_worker],
        "review": [lambda s, p="": _ok(json.dumps(
            {"verdict": "verified", "reason": "fine"}))]})
    snapshots: list[dict] = []
    real = RunDir.update_status

    def spy(self, **patch):
        out = real(self, **patch)
        snapshots.append(_raw(self))
        return out
    RunDir.update_status = spy
    try:
        final = asyncio.run(asyncio.wait_for(sup._run_job_core(), 60))
    finally:
        RunDir.update_status = real
    assert final == "succeeded", sup.run.read_status()
    assert snapshots
    assert all("planPhase" in s for s in snapshots)
    # initial planning (no plan on disk yet) published null, a phased plan
    # published the object, and the first snapshot predates any plan
    assert snapshots[0]["planPhase"] is None
    assert any(isinstance(s["planPhase"], dict)
               and s["planPhase"]["id"] == "one" for s in snapshots)
    assert any(isinstance(s["planPhase"], dict)
               and s["planPhase"]["id"] == "two" for s in snapshots)
