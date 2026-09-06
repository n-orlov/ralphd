"""Task 013 (PRD requirement C.1, #149): a dependent of a `petitioned` task
is visible, not silently starved.

`state.task_blocked_by(tasks, include_petitioned=True)` widens the SAME
`taskBlockedBy` map task 021 built for dead (`failed`/`skipped`) dependencies
with a second, direct-only cause: a task whose `dependsOn` names a
`petitioned` ancestor is included too, with that ancestor as its cause.
`petitioned` is never folded into `TASK_DEPENDENCY_DEAD_STATUSES` itself (it
can still resolve -- REFUTED leaves the hold in place, CONFIRMED skips the
task and re-points dependents, out of this task's scope) -- so
`include_petitioned` is opt-in, `False` by default, and every existing doom
caller (`LoopSupervisor._step_worker`/`_doomed_plan_fault`) stays untouched.

One test (or small group) per surface named in the task's own criteria:

  * the derivation itself (`state.task_blocked_by`), default vs. opt-in;
  * `TasksRead.blocked_by`/`payload` (the on-disk half of `ralphctl tasks`
    and `GET /tasks`);
  * the live engine API (`GET /tasks`, through the same ASGI app the other
    surface-parity suites use);
  * `ralphctl tasks` (text and `--json`), container-gone on-disk path;
  * the hub's own re-derivation (`ui_server._with_task_blocked_by`) and
    `ui_server.run_detail` end to end;
  * the worker prompt: `worker.md`'s own prose names the petitioned cause
    on the same `taskBlockedBy` map, right next to the existing dead-
    dependency sentence.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
import pytest
from test_cli_docker import Ctl, ctl, unix_sock

from ralphd.cli import ui_server
from ralphd.engine.api import create_app
from ralphd.engine.config import JobConfig
from ralphd.engine.loop import LoopSupervisor
from ralphd.engine.state import (
    TASK_BLOCKED_FIELD,
    RunDir,
    read_tasks_doc,
    task_blocked_by,
)

__all__ = ["ctl", "unix_sock"]

REPO = Path(__file__).resolve().parents[1]
WORKER_MD = (REPO / "src" / "ralphd" / "prompts" / "worker.md").read_text()


def _plan_json(doc: dict) -> str:
    return json.dumps(doc, indent=2) + "\n"


# 004 is petitioned (held, not dead); 005 depends on it directly (blocked,
# cause 004); 006 depends on 005 (NOT included -- petitioned causes are
# direct-only, unlike a genuinely dead ancestor's transitive doom); 007
# depends on 001 (completed, a sink) and is untouched; 008 has no
# dependsOn at all.
PETITIONED_PLAN = {
    "version": 1,
    "goal": "ship it",
    "tasks": [
        {"id": "001", "title": "a", "status": "completed"},
        {"id": "004", "title": "d", "status": "petitioned"},
        {"id": "005", "title": "e", "status": "pending", "dependsOn": ["004"]},
        {"id": "006", "title": "f", "status": "pending", "dependsOn": ["005"]},
        {"id": "007", "title": "g", "status": "pending", "dependsOn": ["001"]},
        {"id": "008", "title": "h", "status": "pending"},
    ],
}


# --------------------------------------------------------------------------
# the derivation itself
# --------------------------------------------------------------------------

def test_default_call_never_sees_a_petitioned_cause():
    """Every existing doom caller passes no `include_petitioned` kwarg at
    all, so the default must stay exactly what it was before this task."""
    blocked = task_blocked_by(PETITIONED_PLAN["tasks"])
    assert blocked == {}


def test_opt_in_marks_the_direct_dependent_with_the_petitioned_cause():
    blocked = task_blocked_by(PETITIONED_PLAN["tasks"], include_petitioned=True)
    assert blocked["005"] == ["004"]
    assert "006" not in blocked   # direct-only: not propagated past 005
    assert "007" not in blocked   # a completed/satisfied dependency
    assert "008" not in blocked   # no dependsOn at all


def test_a_petitioned_task_is_a_cause_never_a_key_of_its_own_map():
    blocked = task_blocked_by(PETITIONED_PLAN["tasks"], include_petitioned=True)
    assert "004" not in blocked


def test_a_dead_and_a_petitioned_cause_can_both_be_named_at_once():
    tasks = [
        {"id": "001", "status": "failed"},
        {"id": "002", "status": "petitioned"},
        {"id": "003", "status": "pending", "dependsOn": ["001", "002"]},
    ]
    blocked = task_blocked_by(tasks, include_petitioned=True)
    assert blocked["003"] == ["001", "002"]


# --------------------------------------------------------------------------
# TasksRead / the on-disk derivation (ralphctl tasks + GET /tasks, on disk)
# --------------------------------------------------------------------------

def test_tasks_read_blocked_by_includes_the_petitioned_cause(tmp_path):
    (tmp_path / "tasks.json").write_text(_plan_json(PETITIONED_PLAN))
    res = read_tasks_doc(tmp_path)
    assert res.blocked_by == task_blocked_by(PETITIONED_PLAN["tasks"],
                                              include_petitioned=True)
    assert res.blocked_by["005"] == ["004"]
    assert res.payload[TASK_BLOCKED_FIELD] == res.blocked_by


# --------------------------------------------------------------------------
# the live engine API (`GET /tasks`)
# --------------------------------------------------------------------------

@pytest.fixture
def client(tmp_path):
    run = RunDir(root=tmp_path)
    run.update_status(state="running")
    sup = LoopSupervisor(JobConfig(run_id="unit"), run, tmp_path)
    app = create_app(sup.cfg, run, sup)

    def get(path: str) -> dict:
        async def go():
            async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app),
                    base_url="http://engine") as c:
                r = await c.get(path)
                assert r.status_code == 200, r.text
                return r.json()
        return asyncio.run(go())

    return get


def test_get_tasks_publishes_the_petitioned_cause(client, tmp_path):
    (tmp_path / "tasks.json").write_text(_plan_json(PETITIONED_PLAN))
    doc = client("/tasks")
    assert doc[TASK_BLOCKED_FIELD]["005"] == ["004"]


# --------------------------------------------------------------------------
# `ralphctl tasks`, container-gone (on-disk snapshot path)
# --------------------------------------------------------------------------

def test_ralphctl_tasks_shows_the_petitioned_blocked_by_suffix(ctl: Ctl):
    from test_cli_resume import _seed_run
    rdir, _ = _seed_run(ctl, "tst-petitioned-blocked")
    (rdir / "tasks.json").write_text(_plan_json(PETITIONED_PLAN))
    res = ctl.run("tasks", "tst-petitioned-blocked")
    assert res.returncode == 0, res.stderr
    lines = {ln.split("] ")[1].split(" ")[0]: ln.split("]")[0].strip("[").strip()
             for ln in res.stdout.splitlines()}
    assert lines["005"] == "pending (blocked by 004)"
    assert lines["007"] == "pending"


def test_ralphctl_tasks_json_carries_the_petitioned_cause(ctl: Ctl):
    from test_cli_resume import _seed_run
    rdir, _ = _seed_run(ctl, "tst-petitioned-blocked-json")
    (rdir / "tasks.json").write_text(_plan_json(PETITIONED_PLAN))
    doc = json.loads(
        ctl.run("--json", "tasks", "tst-petitioned-blocked-json").stdout)
    assert doc["live"] is False
    assert doc[TASK_BLOCKED_FIELD]["005"] == ["004"]


# --------------------------------------------------------------------------
# the hub's own re-derivation and `run_detail` end to end
# --------------------------------------------------------------------------

def test_hub_with_task_blocked_by_includes_the_petitioned_cause():
    live = {"tasks": PETITIONED_PLAN["tasks"]}
    out = ui_server._with_task_blocked_by(live)
    assert out[TASK_BLOCKED_FIELD]["005"] == ["004"]


def _seed_registry(tmp_path: Path, run_id: str, plan: dict) -> Path:
    reg = tmp_path / "registry"
    rdir = reg / "runs" / run_id
    rdir.mkdir(parents=True)
    (rdir / "status.json").write_text(json.dumps({
        "runId": run_id, "state": "running", "verdict": "unverified",
        "phase": "worker", "iterationsUsed": 3, "iterationsBudget": 25,
        "startedAt": "2026-01-01T00:00:00Z", "schemaVersion": 1}))
    (rdir / "tasks.json").write_text(_plan_json(plan))
    (rdir / "host.json").write_text(json.dumps({
        "runId": run_id, "container": f"ralphd-{run_id}", "port": 1,
        "apiUrl": "http://127.0.0.1:1", "image": "img", "startedAt": "x"}))
    return reg


def test_hub_run_detail_carries_the_petitioned_cause_on_disk(tmp_path):
    reg = _seed_registry(tmp_path, "hub-petitioned", PETITIONED_PLAN)
    detail = ui_server.run_detail(reg, "hub-petitioned")
    assert detail["tasks"][TASK_BLOCKED_FIELD]["005"] == ["004"]


def test_hub_run_detail_carries_the_petitioned_cause_when_live(tmp_path, monkeypatch):
    reg = _seed_registry(tmp_path, "hub-petitioned-live",
                          {"tasks": [{"id": "1", "status": "completed"}]})
    live = {"tasks": PETITIONED_PLAN["tasks"]}

    def fake_proxy(reg_, run_id, method, path, **kw):
        if path == "/tasks":
            return True, 200, json.loads(json.dumps(live))
        return False, 0, None

    monkeypatch.setattr(ui_server, "_proxy_json", fake_proxy)
    detail = ui_server.run_detail(reg, "hub-petitioned-live")
    assert detail["tasks"][TASK_BLOCKED_FIELD]["005"] == ["004"]


# --------------------------------------------------------------------------
# the worker prompt names the same rule
# --------------------------------------------------------------------------

def test_worker_prompt_names_the_petitioned_cause_on_the_same_map():
    assert "`petitioned` task is marked on the exact same `taskBlockedBy` map" \
        in WORKER_MD


def test_worker_prompt_distinguishes_held_from_genuinely_dead():
    assert "Held, not doomed" in WORKER_MD
    assert "not genuinely dead the way `failed`/" in WORKER_MD
