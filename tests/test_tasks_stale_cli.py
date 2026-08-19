"""Task 004 (#15): the *host-side* readers of `tasks.json` -- `ralphctl
tasks`, `ralphctl status`'s on-disk fallback and the hub's run-detail JSON --
serve the hardened read instead of inventing an empty plan.

Tasks 002/003 fixed the engine (reader + `GET /tasks`/`GET /status`). Every
surface outside the container had its own `json.loads` with
`JSONDecodeError -> default`, so:

* `ralphctl tasks` printed NOTHING for a run whose container is gone (`api()`
  exits 4 on connection-refused) even though the plan is in the run dir, and
  nothing distinguishable for a `tasks.json` caught mid-rewrite;
* the hub's run detail rendered an empty task table for one poll cycle
  whenever an agent happened to be rewriting the plan.

Tiers here: black-box `ralphctl` subprocesses over run-dir fixtures (the
stub-docker `Ctl` harness), black-box HTTP against a real `ralphctl ui`
server, one real engine via the `live` fixture to prove a healthy live run's
output is unchanged, and two white-box probes of the `_read_json` guard that
keeps the bug from being reintroduced.
"""

from __future__ import annotations

import json
import time

import pytest
from test_cli_docker import Ctl, ctl, unix_sock
from test_cli_resume import _seed_run
from test_cli_ui import StubEngineApi, UiServer, _write_run_with_api, ui

from ralphd.engine.state import (
    TASKS_LAST_GOOD_NAME,
    TASKS_STALE_LABEL,
    TASKS_STALE_NOTICE,
    TASKS_UNREADABLE_NOTICE,
    task_counts,
    tasks_read_notice,
)

__all__ = ["UiServer", "ctl", "ui", "unix_sock"]


PLAN = {
    "version": 1,
    "goal": "ship it",
    "tasks": [
        {"id": "001", "title": "one", "status": "completed"},
        {"id": "002", "title": "two", "status": "in-progress"},
        {"id": "003", "title": "three", "status": "pending"},
    ],
}
# A real mid-write snapshot: the agent has truncated the file and not yet
# finished writing the new plan.
TRUNCATED = '{"version": 1, "goal": "ship it", "tasks": [{"id": "001", "sta'


def _seed(ctl: Ctl, run_id: str, *, tasks_text: str | None = None,
          last_good: dict | None = None):
    """A run dir whose recorded API endpoint answers nothing (container
    gone) -- the case both fallbacks exist for."""
    rdir, _cdir = _seed_run(ctl, run_id)
    if tasks_text is not None:
        (rdir / "tasks.json").write_text(tasks_text)
    if last_good is not None:
        (rdir / TASKS_LAST_GOOD_NAME).write_text(json.dumps(last_good))
    return rdir


def _task_ids(stdout: str) -> list[str]:
    return [ln.split("] ", 1)[1].split(" ", 1)[0]
            for ln in stdout.splitlines() if ln.startswith("[")]


# --------------------------------------------------------------------------
# ralphctl tasks: container gone
# --------------------------------------------------------------------------

def test_tasks_of_a_dead_run_prints_the_plan_from_the_run_dir(ctl: Ctl):
    """Before task 004 this exited 4 with no output at all."""
    _seed(ctl, "tst-dead", tasks_text=json.dumps(PLAN))
    res = ctl.run("tasks", "tst-dead")
    assert res.returncode == 0, (res.stdout, res.stderr)
    assert _task_ids(res.stdout) == ["001", "002", "003"]
    assert "on-disk snapshot" in res.stderr
    # a healthy plan is not a stale one: no false alarm
    assert TASKS_STALE_NOTICE not in res.stderr


def test_tasks_of_a_dead_run_serves_the_last_good_plan_flagged_stale(ctl: Ctl):
    """`tasks.json` truncated mid-rewrite + a last-good cache from a previous
    read: the operator sees the plan AND is told it is stale."""
    _seed(ctl, "tst-stale", tasks_text=TRUNCATED, last_good=PLAN)
    res = ctl.run("tasks", "tst-stale")
    assert res.returncode == 0, (res.stdout, res.stderr)
    assert _task_ids(res.stdout) == ["001", "002", "003"]
    assert TASKS_STALE_NOTICE in res.stderr


def test_tasks_json_of_a_stale_read_carries_the_wire_contract(ctl: Ctl):
    _seed(ctl, "tst-stale-json", tasks_text=TRUNCATED, last_good=PLAN)
    res = ctl.run("--json", "tasks", "tst-stale-json")
    assert res.returncode == 0, res.stderr
    doc = json.loads(res.stdout)
    assert doc["tasksStale"] is True
    assert doc["tasksSource"] == "last-good"
    assert doc["live"] is False
    assert [t["id"] for t in doc["tasks"]] == ["001", "002", "003"]


def test_tasks_of_an_unreadable_plan_says_so_instead_of_printing_nothing(ctl: Ctl):
    """Truncated with no last-good anywhere: `total: 0` would be ignorance,
    so the command prints no task lines but names the condition."""
    _seed(ctl, "tst-unreadable", tasks_text=TRUNCATED)
    res = ctl.run("tasks", "tst-unreadable")
    assert res.returncode == 0, (res.stdout, res.stderr)
    assert _task_ids(res.stdout) == []
    assert TASKS_UNREADABLE_NOTICE in res.stderr


def test_tasks_of_a_run_with_no_plan_yet_does_not_cry_wolf(ctl: Ctl):
    """No `tasks.json` at all: the empty plan is the truth, so there is no
    stale/unreadable marker -- only the snapshot notice."""
    _seed(ctl, "tst-noplan")
    res = ctl.run("tasks", "tst-noplan")
    assert res.returncode == 0, (res.stdout, res.stderr)
    assert _task_ids(res.stdout) == []
    assert "on-disk snapshot" in res.stderr
    assert TASKS_STALE_NOTICE not in res.stderr
    assert TASKS_UNREADABLE_NOTICE not in res.stderr


def test_tasks_of_an_unknown_run_is_still_an_error(ctl: Ctl):
    res = ctl.run("tasks", "tst-nope")
    assert res.returncode == 3, (res.returncode, res.stderr)


def test_tasks_never_writes_a_cache_into_someone_elses_run_dir(ctl: Ctl):
    """`persist=False`: a read-only viewer must not leave
    `.tasks-last-good.json` behind in a run dir it does not own."""
    rdir = _seed(ctl, "tst-nopersist", tasks_text=TRUNCATED)
    assert ctl.run("tasks", "tst-nopersist").returncode == 0
    assert not (rdir / TASKS_LAST_GOOD_NAME).exists()


# --------------------------------------------------------------------------
# ralphctl status: the same reader behind the task counts
# --------------------------------------------------------------------------

def test_status_of_a_dead_run_counts_a_mid_write_plan_from_last_good(ctl: Ctl):
    """Task 023's CLI-side reconstruction used the naive reader, so a poll
    inside a rewrite printed `tasks: (none)` for a run with 3 tasks."""
    _seed(ctl, "tst-status-stale", tasks_text=TRUNCATED, last_good=PLAN)
    res = ctl.run("status", "tst-status-stale")
    assert res.returncode == 0, res.stderr
    line = [ln for ln in res.stdout.splitlines() if ln.startswith("tasks:")]
    assert len(line) == 1 and "(none)" not in line[0], res.stdout
    assert "1/3 completed" in line[0]
    doc = json.loads(ctl.run("--json", "status", "tst-status-stale").stdout)
    assert doc["tasks"] == task_counts(PLAN["tasks"])
    assert (doc["tasksStale"], doc["tasksSource"]) == (True, "last-good")


def test_status_of_a_dead_run_with_a_healthy_plan_is_not_flagged(ctl: Ctl):
    _seed(ctl, "tst-status-fresh", tasks_text=json.dumps(PLAN))
    doc = json.loads(ctl.run("--json", "status", "tst-status-fresh").stdout)
    assert (doc["tasksStale"], doc["tasksSource"]) == (False, "file")


# --------------------------------------------------------------------------
# the hub's run-detail JSON
# --------------------------------------------------------------------------

def _hub_run(registry, run_id: str, *, tasks_text: str | None = None,
             last_good: dict | None = None):
    run_dir = registry / "runs" / run_id
    run_dir.mkdir(parents=True)
    (run_dir / "status.json").write_text(json.dumps(
        {"runId": run_id, "state": "running", "phase": "worker",
         "approach": 1, "iterationsUsed": 2,
         "startedAt": "2026-01-01T00:00:00Z"}))
    if tasks_text is not None:
        (run_dir / "tasks.json").write_text(tasks_text)
    if last_good is not None:
        (run_dir / TASKS_LAST_GOOD_NAME).write_text(json.dumps(last_good))
    return run_dir


def test_hub_run_detail_serves_the_last_good_plan_flagged_stale(ui, tmp_path):
    reg = tmp_path / "reg"
    _hub_run(reg, "hub-stale", tasks_text=TRUNCATED, last_good=PLAN)
    code, doc = ui(reg).get("/api/runs/hub-stale")
    assert code == 200, doc
    assert [t["id"] for t in doc["tasks"]["tasks"]] == ["001", "002", "003"]
    assert doc["tasks"]["tasksStale"] is True
    assert doc["tasks"]["tasksSource"] == "last-good"
    # the wording the hub label is built from lives server-side, one copy
    assert tasks_read_notice(doc["tasks"]["tasksSource"]) == TASKS_STALE_NOTICE


def test_hub_run_detail_flags_a_healthy_plan_as_fresh(ui, tmp_path):
    reg = tmp_path / "reg"
    _hub_run(reg, "hub-fresh", tasks_text=json.dumps(PLAN))
    code, doc = ui(reg).get("/api/runs/hub-fresh")
    assert code == 200, doc
    assert len(doc["tasks"]["tasks"]) == 3
    assert doc["tasks"]["tasksStale"] is False
    assert doc["tasks"]["tasksSource"] == "file"


def test_hub_run_detail_of_a_plan_less_run_reports_absent(ui, tmp_path):
    reg = tmp_path / "reg"
    _hub_run(reg, "hub-noplan")
    code, doc = ui(reg).get("/api/runs/hub-noplan")
    assert code == 200, doc
    assert doc["tasks"]["tasks"] == []
    assert (doc["tasks"]["tasksStale"], doc["tasks"]["tasksSource"]) == (False, "absent")


def test_hub_run_detail_never_renders_an_empty_table_under_a_rewrite_loop(ui, tmp_path):
    """The real failure mode: poll while an agent truncates and rewrites the
    plan. Every single response must carry the 3 tasks, and at least one must
    have come from the last-good fallback (otherwise the test proved nothing).
    """
    reg = tmp_path / "reg"
    run_dir = _hub_run(reg, "hub-loop", tasks_text=json.dumps(PLAN))
    server = ui(reg)
    path = run_dir / "tasks.json"
    sources = []
    for i in range(40):
        # the agent's non-atomic write, mid-flight
        path.write_text(TRUNCATED if i % 2 else json.dumps(PLAN))
        code, doc = server.get("/api/runs/hub-loop")
        assert code == 200, doc
        ids = [t["id"] for t in doc["tasks"]["tasks"]]
        assert ids == ["001", "002", "003"], (i, doc["tasks"])
        sources.append(doc["tasks"]["tasksSource"])
    assert "last-good" in sources, sources
    # read-only viewer: no cache written into the run dir it is watching
    assert not (run_dir / TASKS_LAST_GOOD_NAME).exists()


# --------------------------------------------------------------------------
# the guard that keeps the naive reader out
# --------------------------------------------------------------------------


def test_neither_host_side_read_json_will_touch_tasks_json(tmp_path):
    """White-box on purpose: both host-side `_read_json` helpers refuse
    `tasks.json` by name, so a future caller cannot quietly reintroduce
    `JSONDecodeError -> empty plan` (the whole bug of issue #15)."""
    from ralphd.cli import main as cli_main
    from ralphd.cli import ui_server

    (tmp_path / "tasks.json").write_text(json.dumps(PLAN))
    for mod in (cli_main, ui_server):
        with pytest.raises(ValueError, match="read_tasks_doc"):
            mod._read_json(tmp_path / "tasks.json", {"tasks": []})
        # every other document still reads normally
        (tmp_path / "status.json").write_text('{"state": "running"}')
        assert mod._read_json(tmp_path / "status.json", {})["state"] == "running"


# --------------------------------------------------------------------------
# live run: unchanged
# --------------------------------------------------------------------------

def test_tasks_of_a_live_run_is_unchanged(live):
    """A reachable engine with a healthy plan: the plain task lines, exit 0,
    NOTHING on stderr, and the live contract says fresh."""
    run = live(run_id="tasks-live", job={"iterations": 4},
               stub_env={"STUB_TASKS": "2", "STUB_SLEEP": "3"})
    run.wait_api()
    res = None
    for _ in range(60):
        res = run.ralphctl("tasks", run.run_id)
        if res.returncode == 0 and res.stdout.strip():
            break
        time.sleep(0.5)
    assert res is not None
    assert res.returncode == 0, (res.stdout, res.stderr)
    assert _task_ids(res.stdout), res.stdout
    assert res.stderr == "", res.stderr
    doc = json.loads(run.ralphctl("--json", "tasks", run.run_id).stdout)
    assert doc["live"] is True
    assert doc["tasksStale"] is False
    assert doc["tasksSource"] == "file"
    run.wait_terminal(timeout=120)

# --------------------------------------------------------------------------
# task 005 (#15): the hub payload carries the DISPLAY strings for the label
# --------------------------------------------------------------------------
# `app.js` must not re-spell engine wording in JS (the `usage.costDisplay` /
# `startedAtLocal` discipline), so `ui_server` renders the read's provenance
# into `tasksLabel`/`tasksNotice` from `engine.state`'s one copy of it.

def test_hub_run_detail_carries_the_stale_label_strings(ui, tmp_path):
    reg = tmp_path / "reg"
    _hub_run(reg, "hub-label", tasks_text=TRUNCATED, last_good=PLAN)
    code, doc = ui(reg).get("/api/runs/hub-label")
    assert code == 200, doc
    assert doc["tasks"]["tasksLabel"] == TASKS_STALE_LABEL
    assert doc["tasks"]["tasksNotice"] == TASKS_STALE_NOTICE


def test_hub_run_detail_label_of_an_unreadable_plan_says_ignorance(ui, tmp_path):
    """No last-good anywhere: the browser must be handed the *unreadable*
    sentence, not the stale one -- an empty table there is ignorance."""
    reg = tmp_path / "reg"
    _hub_run(reg, "hub-unreadable", tasks_text=TRUNCATED)
    code, doc = ui(reg).get("/api/runs/hub-unreadable")
    assert code == 200, doc
    assert doc["tasks"]["tasks"] == []
    assert doc["tasks"]["tasksLabel"] == TASKS_STALE_LABEL
    assert doc["tasks"]["tasksNotice"] == TASKS_UNREADABLE_NOTICE


@pytest.mark.parametrize("tasks_text", [json.dumps(PLAN), None])
def test_hub_run_detail_adds_no_label_when_the_read_is_not_stale(ui, tmp_path, tasks_text):
    reg = tmp_path / "reg"
    run_id = "hub-nolabel-" + ("file" if tasks_text else "absent")
    _hub_run(reg, run_id, tasks_text=tasks_text)
    code, doc = ui(reg).get(f"/api/runs/{run_id}")
    assert code == 200, doc
    assert "tasksLabel" not in doc["tasks"]
    assert "tasksNotice" not in doc["tasks"]


def test_a_forged_label_in_tasks_json_cannot_fake_staleness(ui, tmp_path):
    """The plan file is agent-written: keys it invents must not survive into
    the display strings (same reason task 003 appends the contract last)."""
    reg = tmp_path / "reg"
    forged = {**PLAN, "tasksStale": True, "tasksLabel": "FRESH",
              "tasksNotice": "trust me"}
    _hub_run(reg, "hub-forged", tasks_text=json.dumps(forged))
    code, doc = ui(reg).get("/api/runs/hub-forged")
    assert code == 200, doc
    assert doc["tasks"]["tasksStale"] is False
    assert doc["tasks"]["tasksSource"] == "file"
    assert "tasksLabel" not in doc["tasks"]
    assert "tasksNotice" not in doc["tasks"]


def test_a_live_pre_v06_engine_answer_gets_no_invented_label(ui, tmp_path):
    """A live `GET /tasks` from an engine older than #15 carries no flags at
    all; the hub labels nothing rather than vouching for freshness."""
    engine = StubEngineApi(tasks=dict(PLAN))
    reg = tmp_path / "reg"
    try:
        _write_run_with_api(reg, "hub-old-engine", engine, state="running", verdict=None)
        code, doc = ui(reg).get("/api/runs/hub-old-engine")
    finally:
        engine.close()
    assert code == 200, doc
    assert len(doc["tasks"]["tasks"]) == 3
    assert "tasksStale" not in doc["tasks"]
    assert "tasksLabel" not in doc["tasks"]
    assert "tasksNotice" not in doc["tasks"]
