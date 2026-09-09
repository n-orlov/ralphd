"""Task 023 (PRD requirement C.1, #156): a task task 021's own `verdict:
"deferred"` report moved to `pending` with `deferredUntil`/`deferredReason`
recorded is otherwise invisible on every surface that already renders a
task's status -- `ralphctl tasks`, the hub's task row, and (crucially, since
a plan usually holds several tasks) the WORKER PROMPT itself, which needed
told that some tasks are missing from the runnable pool for a reason, not
silently dropped.

What is pinned here, mirroring the shape `tests/test_steering_origin_marker.py`
already uses for the sibling `origin` overlay:

  * the shared formatter, `engine.state.format_task_status`, appends
    ` (deferred until <t>)` verbatim from the task's own `deferredUntil`
    field, composing correctly with the blocked-by suffix;
  * `ralphctl tasks` (the on-disk snapshot path) renders the same words,
    through the one function every text surface already shares;
  * the hub's browser rendering (`app.js`): `taskStatusText` and the task
    row's own pill, read as text -- fast tier, no browser needed;
  * `LoopSupervisor._worker_assignment_extra` -- the one thing actually
    appended to `worker.md` to build the real dispatched prompt -- carries a
    "## Deferred tasks" section listing every OTHER pending deferred task's
    id/until/reason, present whether or not a task was assigned this
    iteration, absent entirely when nothing is deferred, and never wrapping
    a contract-shaped object inside a code fence; pinned once directly and
    once through the real production call site (`_step_worker`, a scripted
    runner recording the prompt it was actually handed).
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from test_cli_docker import Ctl, ctl, unix_sock
from test_cli_resume import _seed_run

from ralphd.engine.loop import LoopSupervisor
from ralphd.engine.runner import IterationResult
from ralphd.engine.state import RunDir, atomic_write_json, format_task_status
from tests.conftest import scripted_supervisor, supervisor_factory

__all__ = ["ctl", "unix_sock"]

REPO = Path(__file__).resolve().parents[1]
APP_JS = (REPO / "src" / "ralphd" / "cli" / "web" / "app.js").read_text()

UNTIL = "2099-01-01T00:00:00Z"
REASON = "waiting on a gated dependency to open"

DEFERRED_PLAN = {
    "version": 1,
    "goal": "ship it",
    "tasks": [
        {"id": "001", "title": "in-progress task", "status": "in-progress"},
        {"id": "002", "title": "waits on a gate", "status": "pending",
         "deferredUntil": UNTIL, "deferredReason": REASON},
    ],
}


def _plan_json(doc: dict) -> str:
    return json.dumps(doc, indent=2) + "\n"


# --------------------------------------------------------------------------
# the shared formatter
# --------------------------------------------------------------------------

def test_format_task_status_renders_deferred_until():
    task = {"id": "002", "status": "pending", "deferredUntil": UNTIL}
    assert format_task_status(task) == f"pending (deferred until {UNTIL})"


def test_format_task_status_leaves_a_non_deferred_task_unmarked():
    task = {"id": "001", "status": "in-progress"}
    assert format_task_status(task) == "in-progress"


def test_the_deferred_marker_composes_with_blocked_by():
    task = {"id": "002", "status": "pending", "deferredUntil": UNTIL}
    assert format_task_status(task, ["003"]) == (
        f"pending (blocked by 003) (deferred until {UNTIL})")


# --------------------------------------------------------------------------
# `ralphctl tasks`, container-gone (on-disk snapshot) path
# --------------------------------------------------------------------------

def test_ralphctl_tasks_renders_deferred_until_for_a_deferred_task(ctl: Ctl):
    rdir, _ = _seed_run(ctl, "tst-tasks-deferred")
    (rdir / "tasks.json").write_text(_plan_json(DEFERRED_PLAN))
    res = ctl.run("tasks", "tst-tasks-deferred")
    assert res.returncode == 0, res.stderr
    rows = {ln.split("] ")[1].split(" ")[0]: ln.split("]")[0].strip("[").strip()
            for ln in res.stdout.splitlines()}
    assert rows["001"] == "in-progress"
    assert rows["002"] == f"pending (deferred until {UNTIL})"


def test_tasks_json_carries_the_raw_deferred_fields(ctl: Ctl):
    rdir, _ = _seed_run(ctl, "tst-tasks-deferred-json")
    (rdir / "tasks.json").write_text(_plan_json(DEFERRED_PLAN))
    doc = json.loads(ctl.run("--json", "tasks", "tst-tasks-deferred-json").stdout)
    by_id = {t["id"]: t for t in doc["tasks"]}
    assert by_id["002"]["deferredUntil"] == UNTIL
    assert by_id["002"]["deferredReason"] == REASON
    assert "deferredUntil" not in by_id["001"]


# --------------------------------------------------------------------------
# the hub's browser rendering (`app.js`), read as text -- fast tier
# --------------------------------------------------------------------------

def test_task_status_text_appends_deferred_until():
    block = APP_JS.split("function taskStatusText(")[1].split("\nfunction ")[0]
    assert "t.deferredUntil" in block, block
    assert '"(deferred until "' in block or "\" (deferred until \"" in block, block


def test_render_tasks_carries_a_deferred_until_pill():
    block = APP_JS.split("function renderTasks(")[1].split("\nfunction ")[0]
    assert "t.deferredUntil" in block, block
    assert '"deferred until "' in block, block


# --------------------------------------------------------------------------
# the worker prompt's rendered section (`_worker_assignment_extra`)
# --------------------------------------------------------------------------

_ssn_supervisor = supervisor_factory(run_id="task023-deferred-surface",
                                     run_root="run", workspace="ws")


def _sup(tmp_path) -> LoopSupervisor:
    return _ssn_supervisor(tmp_path)


def test_worker_assignment_extra_lists_another_deferred_task_with_its_time(tmp_path):
    sup = _sup(tmp_path)
    task = {"id": "001", "title": "some task", "status": "in-progress",
            "successCriteria": "some criteria"}
    tasks_doc = {"version": 1, "tasks": [
        task,
        {"id": "002", "title": "waits", "status": "pending",
         "deferredUntil": UNTIL, "deferredReason": REASON},
    ]}
    extra = sup._worker_assignment_extra(task, tasks_doc=tasks_doc)
    assert "## Deferred tasks" in extra
    assert "002" in extra.split("## Deferred tasks", 1)[1]
    assert UNTIL in extra
    assert REASON in extra


def test_worker_assignment_extra_omits_the_section_when_nothing_is_deferred(tmp_path):
    sup = _sup(tmp_path)
    task = {"id": "001", "title": "some task", "status": "in-progress",
            "successCriteria": "some criteria"}
    tasks_doc = {"version": 1, "tasks": [task]}
    extra = sup._worker_assignment_extra(task, tasks_doc=tasks_doc)
    assert "## Deferred tasks" not in extra
    # no tasks_doc at all keeps working exactly as before task 023
    assert "## Deferred tasks" not in sup._worker_assignment_extra(task)


def test_worker_assignment_extra_lists_deferred_tasks_even_when_none_selectable(tmp_path):
    sup = _sup(tmp_path)
    tasks_doc = {"version": 1, "tasks": [
        {"id": "002", "title": "waits", "status": "pending",
         "deferredUntil": UNTIL, "deferredReason": REASON},
    ]}
    extra = sup._worker_assignment_extra(None, tasks_doc=tasks_doc)
    assert "No task is currently selectable" in extra
    assert "## Deferred tasks" in extra
    assert UNTIL in extra


def test_worker_assignment_extra_deferred_section_never_fences_a_contract_object(tmp_path):
    sup = _sup(tmp_path)
    task = {"id": "001", "title": "some task", "status": "in-progress",
            "successCriteria": "some criteria"}
    tasks_doc = {"version": 1, "tasks": [
        task,
        {"id": "002", "title": "waits", "status": "pending",
         "deferredUntil": UNTIL, "deferredReason": REASON},
    ]}
    extra = sup._worker_assignment_extra(task, tasks_doc=tasks_doc)
    section = extra.split("## Deferred tasks", 1)[1]
    assert "```" not in section
    assert '"verdict"' not in section


# -- the real production call site: `_step_worker`'s own dispatch -----------

def _ok(text: str) -> IterationResult:
    r = IterationResult(exit_code=0)
    r.final_text = text
    r.duration_s = 5.0
    r.usage = {"input": 10, "output": 5, "totalTokens": 15}
    return r


def _complete(task_id: str) -> IterationResult:
    payload = json.dumps({"verdict": "complete", "task_id": task_id})
    return _ok(f"task {task_id} done\n{payload}")


def test_a_real_worker_dispatch_prompt_names_the_other_deferred_task(tmp_path):
    root = tmp_path / "run"
    RunDir(root=root).update_status(state="starting")
    factory = scripted_supervisor(run_id="task023-real-dispatch",
                                  iterations=3, max_approaches=1,
                                  vigilant=False, on_complete="exit",
                                  infra_outage_budget_s=1000.0)
    sup = factory(root, {
        "worker": [lambda s, p: _complete("001")],
        "review": [],
    })
    tasks = [
        {"id": "001", "title": "in progress", "status": "in-progress",
         "successCriteria": "the thing works"},
        {"id": "002", "title": "waits on a gate", "status": "pending",
         "successCriteria": "the gate opens",
         "deferredUntil": UNTIL, "deferredReason": REASON},
    ]
    atomic_write_json(sup.run.tasks_file, {"version": 1, "tasks": tasks})
    sup.iterations_used = 1
    asyncio.run(sup._run_job_core())

    prompt = sup.runner.prompts[0]  # type: ignore[attr-defined]
    assert "## Deferred tasks" in prompt
    assert "002" in prompt.split("## Deferred tasks", 1)[1]
    assert UNTIL in prompt
    assert REASON in prompt
