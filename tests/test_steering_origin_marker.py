"""Task 011 (PRD requirement I, #98): a task the steer hat inserted carries
an explicit `origin` marker in `tasks.json`, and that marker is rendered on
every surface a plan's tasks already reach -- `ralphctl tasks`, the engine
API (`GET /tasks`, the hub's run-detail read), and the hub's browser
rendering (`app.js`) -- so a steer-injected task is never indistinguishable
from one the planner reasoned about.

`tasks.json` being planner-proposed is a deliberate property (PRD wording);
this pins the one field that breaks that property honestly, on the exact
same surfaces task 025 (#33) pinned for the sibling `failureKind` overlay:

  * the persisted field itself (`steer_task_insert` always stamps it,
    regardless of what the caller supplied -- the same discipline that
    function already applies to `status`);
  * `engine.state.is_steering_origin` / `format_task_status`'s marker,
    composed correctly with the failure-kind and blocked-by suffixes it
    already carries;
  * `ralphctl tasks` (text and `--json`), container-gone (on-disk snapshot)
    path;
  * the engine API (`GET /tasks`) and the hub's run-detail read -- both pass
    the raw task record through verbatim, so the field survives without any
    new server-side derivation;
  * the hub's browser rendering (`app.js`): `isSteeringOrigin`, the row's
    `data-task-origin` attribute and pill, and the task dialog's `origin:`
    line -- read as text, so this lives in the fast tier with no browser
    needed.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
import pytest
from test_cli_docker import Ctl, ctl, unix_sock
from test_cli_resume import _seed_run

from ralphd.cli import ui_server
from ralphd.engine.api import create_app
from ralphd.engine.config import JobConfig
from ralphd.engine.loop import LoopSupervisor
from ralphd.engine.state import (
    TASK_ORIGIN_STEER,
    RunDir,
    format_task_status,
    is_steering_origin,
    read_tasks_doc,
    steer_task_insert,
)

__all__ = ["ctl", "unix_sock"]

REPO = Path(__file__).resolve().parents[1]
APP_JS = (REPO / "src" / "ralphd" / "cli" / "web" / "app.js").read_text()

PLANNER_PLUS_STEER_PLAN = {
    "version": 1,
    "goal": "ship it",
    "tasks": [
        {"id": "001", "title": "planner task", "status": "in-progress"},
        {"id": "002", "title": "steer task", "status": "pending",
         "origin": TASK_ORIGIN_STEER},
    ],
}


def _plan_json(doc: dict) -> str:
    return json.dumps(doc, indent=2) + "\n"


def _run(tmp_path: Path) -> RunDir:
    return RunDir(root=tmp_path / "run")


# --------------------------------------------------------------------------
# the persisted field itself
# --------------------------------------------------------------------------

def test_steer_task_insert_always_stamps_the_origin(tmp_path):
    run = _run(tmp_path)
    run.tasks_file.parent.mkdir(parents=True, exist_ok=True)
    run.tasks_file.write_text(json.dumps({"version": 1, "tasks": []}))
    steer_task_insert(run, {"id": "010", "title": "new work"})
    data = run.read_tasks()
    task = next(t for t in data["tasks"] if t["id"] == "010")
    assert task["origin"] == TASK_ORIGIN_STEER


def test_steer_task_insert_overwrites_a_forged_origin(tmp_path):
    """Same discipline as `status`: a caller cannot claim planner origin (or
    any other spelling) for a row this tool is inserting."""
    run = _run(tmp_path)
    run.tasks_file.parent.mkdir(parents=True, exist_ok=True)
    run.tasks_file.write_text(json.dumps({"version": 1, "tasks": []}))
    steer_task_insert(run, {"id": "011", "title": "sneaky",
                            "origin": "planner"})
    data = run.read_tasks()
    task = next(t for t in data["tasks"] if t["id"] == "011")
    assert task["origin"] == TASK_ORIGIN_STEER


def test_a_planner_task_carries_no_origin_key():
    """Backward compatible: a plan written before this task existed (or any
    task never touched by `steer_task_insert`) carries no `origin` key at
    all -- there is no second spelling of "planner" to forge or omit."""
    assert "origin" not in PLANNER_PLUS_STEER_PLAN["tasks"][0]


# --------------------------------------------------------------------------
# the predicate and the shared formatter
# --------------------------------------------------------------------------

def test_is_steering_origin_predicate():
    assert is_steering_origin({"id": "1", "origin": "steer"}) is True
    assert is_steering_origin({"id": "1"}) is False
    assert is_steering_origin({"id": "1", "origin": "planner"}) is False
    assert is_steering_origin("not a dict") is False


def test_format_task_status_marks_a_steering_origin_task():
    task = {"id": "002", "status": "pending", "origin": TASK_ORIGIN_STEER}
    assert format_task_status(task) == "pending (steering-origin)"


def test_format_task_status_leaves_a_planner_task_unmarked():
    task = {"id": "001", "status": "in-progress"}
    assert format_task_status(task) == "in-progress"


def test_the_marker_composes_with_failure_kind_and_blocked_by():
    task = {"id": "002", "status": "failed",
            "failureKind": "requirement-unmet", "origin": TASK_ORIGIN_STEER}
    assert format_task_status(task) == \
        "failed (requirement-unmet) (steering-origin)"
    assert format_task_status(task, ["003"]) == \
        "failed (requirement-unmet) (steering-origin) (blocked by 003)"


# --------------------------------------------------------------------------
# `ralphctl tasks`, container-gone (on-disk snapshot) path
# --------------------------------------------------------------------------

def test_tasks_of_a_dead_run_marks_the_steering_origin_row(ctl: Ctl):
    rdir, _ = _seed_run(ctl, "tst-tasks-origin")
    (rdir / "tasks.json").write_text(_plan_json(PLANNER_PLUS_STEER_PLAN))
    res = ctl.run("tasks", "tst-tasks-origin")
    assert res.returncode == 0, res.stderr
    rows = {ln.split("] ")[1].split(" ")[0]: ln.split("]")[0].strip("[").strip()
            for ln in res.stdout.splitlines()}
    assert rows["001"] == "in-progress"
    assert rows["002"] == "pending (steering-origin)"
    # aligned: every status cell is padded to the same width
    widths = {len(ln.split("]")[0]) for ln in res.stdout.splitlines()}
    assert len(widths) == 1, res.stdout


def test_tasks_json_carries_the_raw_origin_field(ctl: Ctl):
    rdir, _ = _seed_run(ctl, "tst-tasks-origin-json")
    (rdir / "tasks.json").write_text(_plan_json(PLANNER_PLUS_STEER_PLAN))
    doc = json.loads(ctl.run("--json", "tasks", "tst-tasks-origin-json").stdout)
    by_id = {t["id"]: t for t in doc["tasks"]}
    assert by_id["002"]["origin"] == TASK_ORIGIN_STEER
    assert "origin" not in by_id["001"]


# --------------------------------------------------------------------------
# the engine API
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


def test_get_tasks_publishes_the_origin_verbatim(client, tmp_path):
    (tmp_path / "tasks.json").write_text(_plan_json(PLANNER_PLUS_STEER_PLAN))
    doc = client("/tasks")
    by_id = {t["id"]: t for t in doc["tasks"]}
    assert by_id["002"]["origin"] == TASK_ORIGIN_STEER
    assert "origin" not in by_id["001"]


# --------------------------------------------------------------------------
# the hub's run-detail read (`ui_server.run_detail` / `run_list` bases)
# --------------------------------------------------------------------------

def _seed_registry(tmp_path: Path, run_id: str, plan: dict) -> Path:
    reg = tmp_path / "registry"
    rdir = reg / "runs" / run_id
    rdir.mkdir(parents=True)
    (rdir / "status.json").write_text(json.dumps({
        "runId": run_id, "state": "failed", "verdict": "unverified",
        "phase": "review", "iterationsUsed": 9, "iterationsBudget": 25,
        "startedAt": "2026-01-01T00:00:00Z", "schemaVersion": 1}))
    (rdir / "tasks.json").write_text(_plan_json(plan))
    (rdir / "host.json").write_text(json.dumps({
        "runId": run_id, "container": f"ralphd-{run_id}", "port": 1,
        "apiUrl": "http://127.0.0.1:1", "image": "img", "startedAt": "x"}))
    return reg


def test_the_hub_run_detail_serves_the_origin_field_verbatim(tmp_path):
    reg = _seed_registry(tmp_path, "hub-origin", PLANNER_PLUS_STEER_PLAN)
    detail = ui_server.run_detail(reg, "hub-origin")
    assert detail["tasks"]["tasks"] == PLANNER_PLUS_STEER_PLAN["tasks"]
    by_id = {t["id"]: t for t in detail["tasks"]["tasks"]}
    assert by_id["002"]["origin"] == TASK_ORIGIN_STEER


def test_the_hub_read_and_the_cli_read_agree(tmp_path):
    reg = _seed_registry(tmp_path, "hub-origin-parity", PLANNER_PLUS_STEER_PLAN)
    detail = ui_server.run_detail(reg, "hub-origin-parity")
    disk = read_tasks_doc(reg / "runs" / "hub-origin-parity",
                          persist=False).payload
    assert detail["tasks"]["tasks"] == disk["tasks"]


# --------------------------------------------------------------------------
# the hub's browser rendering (`app.js`), read as text -- fast tier
# --------------------------------------------------------------------------

def test_app_js_declares_the_steering_origin_predicate():
    block = APP_JS.split("function isSteeringOrigin(")[1].split("\n}")[0]
    assert 't.origin === "steer"' in block, block


def test_task_status_text_marks_the_row():
    block = APP_JS.split("function taskStatusText(")[1].split("\nfunction ")[0]
    assert "isSteeringOrigin(t)" in block, block
    assert "(steering-origin)" in block, block


def test_render_tasks_carries_a_data_attribute_and_a_pill():
    block = APP_JS.split("function renderTasks(")[1].split("\nfunction ")[0]
    assert "data-task-origin" in block, block
    assert 'pill("steering-origin")' in block, block


def test_task_dialog_names_the_origin_explicitly():
    dialog = APP_JS.split("function taskDialogText(")[1].split("\n}")[0]
    assert "isSteeringOrigin(t)" in dialog, dialog
    assert '"origin: steer"' in dialog, dialog
