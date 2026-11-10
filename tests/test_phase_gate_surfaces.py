"""PRD v0.37 requirements F.1-F.3: the plan phase surfaces on the
`phase_gate.*` events, in `status.json`, in `ralphctl status` (and its
`--json`) and in `GET /status`.

The loop is driven with fake hats over a git workspace, as
`tests/test_phase_review_dispatch.py` does (its helpers are reused); the CLI
runs over on-disk run dirs through the `Ctl` harness; nothing sleeps."""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest
from test_cli_docker import Ctl, ctl, unix_sock
from test_cli_resume import _seed_run

from ralphd.engine import state
from ralphd.engine.api import create_app
from ralphd.engine.config import JobConfig
from ralphd.engine.loop import LoopSupervisor
from ralphd.engine.state import RunDir, atomic_write_json

from tests.conftest import scripted_supervisor
from tests.test_phase_gate_cure import _rejecting, _seeded_sup
from tests.test_phase_review_dispatch import (
    _events, _init_workspace, _ok, _plan, _worker)

__all__ = ["ctl", "unix_sock"]

_supervisor = scripted_supervisor(
    run_id="phase-gate-surfaces", run_root="run", workspace="workspace",
    iterations=40, max_approaches=1, vigilant=False, on_complete="exit",
    infra_outage_budget_s=1000.0, operator_input_needed_pause=False)


def _verified(sup, prompt=""):
    return _ok(json.dumps({"verdict": "verified", "reason": "fine"}))


def _run(tmp_path, hats):
    _init_workspace(tmp_path / "workspace")
    RunDir(root=tmp_path / "run").update_status(state="starting")
    sup = _supervisor(tmp_path, hats)
    final = asyncio.run(asyncio.wait_for(sup._run_job_core(), 60))
    return sup, final


# -- F.1: the three events ----------------------------------------------------

def test_phase_gate_due_and_passed_carry_plan_phase_approach_and_round(tmp_path):
    sup, final = _run(tmp_path, {"planning": [_plan], "worker": [_worker],
                                 "review": [_verified]})
    assert final == "succeeded", sup.run.read_status()
    (due,) = _events(sup, "phase_gate.due")
    (passed,) = _events(sup, "phase_gate.passed")
    for ev in (due, passed):
        assert ev["planPhase"] == "one"
        assert ev["approach"] == 1
        assert ev["round"] == 1


def test_phase_gate_rejected_carries_plan_phase_approach_round_and_the_blocking_finding_count(tmp_path):
    sup = _seeded_sup(tmp_path, {"review": [lambda s, p="": _rejecting(s)]})
    asyncio.run(sup._run_phase_review("one"))
    (due,) = _events(sup, "phase_gate.due")
    (rejected,) = _events(sup, "phase_gate.rejected")
    assert (due["planPhase"], due["approach"], due["round"]) == ("one", 1, 1)
    assert rejected["planPhase"] == "one"
    assert rejected["approach"] == 1
    assert rejected["round"] == 1
    # the fixture's verdict carries exactly two blocking findings
    assert rejected["blockingFindings"] == 2


# -- F.2: status.json -----------------------------------------------------------

def test_status_json_carries_the_active_plan_phase_while_a_phased_plan_runs(tmp_path):
    seen: list[dict | None] = []

    def worker(sup, prompt=""):
        seen.append(sup.run.read_status().get("planPhase", "missing"))
        return _worker(sup, prompt)

    sup, final = _run(tmp_path, {"planning": [_plan], "worker": [worker],
                                 "review": [_verified]})
    assert final == "succeeded", sup.run.read_status()
    # worker 001 runs in phase one, worker 002 after its gate passed
    assert seen == [
        {"id": "one", "title": "Phase one", "index": 1, "count": 2,
         "gate": "closed"},
        {"id": "two", "title": "Phase two", "index": 2, "count": 2,
         "gate": "closed"}]


def test_status_json_says_null_for_plan_phase_on_a_plan_without_phases(tmp_path):
    seen: list = []

    def plan(sup, prompt=""):
        atomic_write_json(sup.run.root / "plan-proposal.json", {
            "version": 1,
            "tasks": [{"id": "001", "title": "t1",
                       "successCriteria": "criterion one"}]})
        return _ok(json.dumps({"proposal_path": "plan-proposal.json",
                               "summary": "planned"}))

    def worker(sup, prompt=""):
        seen.append(sup.run.read_status().get("planPhase", "missing"))
        return _worker(sup, prompt)

    sup, final = _run(tmp_path, {"planning": [plan], "worker": [worker],
                                 "review": [_verified]})
    assert final == "succeeded", sup.run.read_status()
    assert seen == [None]
    assert sup.run.read_status()["planPhase"] is None


def test_the_gate_state_in_plan_phase_follows_the_gate(tmp_path):
    sup = _seeded_sup(tmp_path, {"review": [lambda s, p="": _rejecting(s)]})
    asyncio.run(sup._run_phase_review("one"))
    status = sup.run.read_status()["planPhase"]
    assert (status["id"], status["gate"]) == ("one", "closed")
    data = sup.run.read_tasks()
    state.set_phase_gate(data, "one", state.PHASE_GATE_PASSED)
    sup.run.write_tasks(data)
    status = sup.run.read_status()["planPhase"]
    assert (status["id"], status["index"], status["count"]) == ("two", 2, 2)


# -- F.3: ralphctl status and GET /status -----------------------------------------

_PHASES = [
    {"id": "one", "title": "Foundations", "reviewFocus": "f1",
     "gate": "passed", "records": []},
    {"id": "two", "title": "Wiring", "reviewFocus": "f2",
     "gate": "closed", "records": []},
    {"id": "three", "title": "Polish", "reviewFocus": "f3",
     "gate": "closed", "records": []}]


def _tasks_doc(phases):
    doc = {"version": 1, "tasks": [
        {"id": "001", "title": "t1", "status": "completed",
         "successCriteria": "c", "dependsOn": []}]}
    if phases:
        doc["phases"] = [dict(p) for p in phases]
        for t in doc["tasks"]:
            t["phase"] = "one"
    return doc


def _seed_status(ctl: Ctl, run_id: str, phases) -> None:
    """A run dir with NO container record: `ralphctl` takes its on-disk path."""
    rdir, _cdir = _seed_run(ctl, run_id)
    (rdir / "host.json").unlink()
    (rdir / "status.json").write_text(json.dumps({
        "runId": run_id, "state": "failed", "verdict": "unverified",
        "phase": "worker", "iterationsUsed": 3, "iterationsBudget": 40,
        "startedAt": "2024-01-01T00:00:00Z", "schemaVersion": 1}))
    atomic_write_json(rdir / "tasks.json", _tasks_doc(phases))


def test_ralphctl_status_prints_the_phase_index_title_and_gate_on_a_phased_plan(ctl):
    _seed_status(ctl, "phased", _PHASES)
    res = ctl.run("status", "phased")
    assert res.returncode == 0, res.stderr
    assert "phase 2/3 Wiring · gate closed" in res.stdout


def test_ralphctl_status_json_carries_plan_phase(ctl):
    _seed_status(ctl, "phased-json", _PHASES)
    doc = json.loads(ctl.run("--json", "status", "phased-json").stdout)
    assert doc["planPhase"] == {"id": "two", "title": "Wiring", "index": 2,
                                "count": 3, "gate": "closed"}


def test_ralphctl_status_has_no_phase_line_and_a_null_plan_phase_without_phases(ctl):
    _seed_status(ctl, "unphased", None)
    res = ctl.run("status", "unphased")
    assert res.returncode == 0, res.stderr
    assert "\u00b7 gate" not in res.stdout
    assert not [ln for ln in res.stdout.splitlines()
                if ln.startswith("plan:")]
    doc = json.loads(ctl.run("--json", "status", "unphased").stdout)
    assert doc["planPhase"] is None


def _get_status(tmp_path, phases) -> dict:
    run = RunDir(root=tmp_path)
    run.update_status(state="running")
    run.write_tasks(_tasks_doc(phases))
    sup = LoopSupervisor(JobConfig(run_id="unit", max_approaches=3,
                                  infra_retry_backoff_s=[0.0],
                                  infra_retry_backoff_max_s=0.0), run,
                         tmp_path)
    app = create_app(sup.cfg, run, sup)

    async def go():
        async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://engine") as c:
            r = await c.get("/status")
            assert r.status_code == 200, r.text
            return r.json()
    return asyncio.run(go())


def test_get_status_carries_plan_phase_on_a_phased_plan(tmp_path):
    assert _get_status(tmp_path, _PHASES)["planPhase"] == {
        "id": "two", "title": "Wiring", "index": 2, "count": 3,
        "gate": "closed"}


def test_get_status_carries_a_null_plan_phase_without_phases(tmp_path):
    s = _get_status(tmp_path, None)
    assert "planPhase" in s and s["planPhase"] is None
