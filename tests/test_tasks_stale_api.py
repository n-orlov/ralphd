"""Task 003 (#15): `GET /tasks` and `GET /status` serve the hardened read.

Task 002 hardened the *reader* (`engine/state.read_tasks_doc`); this module
pins what the two engine endpoints do with it, black-box over real ASGI:

* both carry the read's provenance (`tasksStale` always present,
  `tasksSource` naming which of absent/file/last-good/unreadable produced the
  payload), so a consumer never has to infer freshness;
* neither collapses to an empty plan / `total: 0` for a `tasks.json` that
  exists and previously parsed -- including under a live agent-style
  truncate-and-rewrite loop, which is the whole reason the feature exists.
"""

from __future__ import annotations

import asyncio
import json
import threading
import time

import httpx
import pytest

from ralphd.engine import state as state_mod
from ralphd.engine.api import create_app
from ralphd.engine.config import JobConfig
from ralphd.engine.loop import LoopSupervisor
from ralphd.engine.state import RunDir

PLAN = {
    "version": 1,
    "goal": "ship it",
    "tasks": [
        {"id": "001", "title": "one", "status": "completed"},
        {"id": "002", "title": "two", "status": "in-progress"},
        {"id": "003", "title": "three", "status": "pending"},
    ],
}
PLAN_COUNTS = {"total": 3, "completed": 1, "inProgress": 1, "pending": 1}
TRUNCATED = '{"version": 1, "goal": "ship it", "tasks": [{"id": "001", "sta'


def _forget_process_cache() -> None:
    """Drop the in-memory last-good map -- what a fresh engine looks like."""
    with state_mod._tasks_last_good_lock:
        state_mod._tasks_last_good.clear()


@pytest.fixture
def client(tmp_path):
    """An ASGI client factory over the engine app on an empty run dir."""
    _forget_process_cache()
    run = RunDir(root=tmp_path)
    run.update_status(state="running")
    sup = LoopSupervisor(JobConfig(run_id="unit"), run, tmp_path)
    app = create_app(sup.cfg, run, sup)

    def open_client() -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://engine")

    return open_client


def _write(tmp_path, doc: dict) -> None:
    (tmp_path / "tasks.json").write_text(json.dumps(doc, indent=2) + "\n")


def _get(client, path: str) -> dict:
    async def go():
        async with client() as c:
            r = await c.get(path)
            assert r.status_code == 200, r.text
            return r.json()
    return asyncio.run(go())


def _get_both(client) -> tuple[dict, dict]:
    async def go():
        async with client() as c:
            rt = await c.get("/tasks")
            rs = await c.get("/status")
            assert (rt.status_code, rs.status_code) == (200, 200)
            return rt.json(), rs.json()
    return asyncio.run(go())


# -- the happy path is unchanged, plus an explicit "not stale" --------------

def test_fresh_plan_is_served_verbatim_and_flagged_not_stale(client, tmp_path):
    _write(tmp_path, PLAN)
    tasks, status = _get_both(client)
    assert {k: tasks[k] for k in PLAN} == PLAN          # tasks.json verbatim
    assert (tasks["tasksStale"], tasks["tasksSource"]) == (False, "file")
    assert status["tasks"] == PLAN_COUNTS
    assert (status["tasksStale"], status["tasksSource"]) == (False, "file")


def test_absent_plan_is_absent_not_stale(client):
    tasks, status = _get_both(client)
    assert tasks == {"tasksStale": False, "tasksSource": "absent"}
    assert status["tasks"] == {"total": 0}
    assert (status["tasksStale"], status["tasksSource"]) == (False, "absent")


# -- a mid-write file: last-good, flagged, never empty ----------------------

def test_truncated_plan_serves_last_good_flagged_stale_on_both_endpoints(
        client, tmp_path):
    _write(tmp_path, PLAN)
    _get(client, "/tasks")                              # remembers the plan
    (tmp_path / "tasks.json").write_text(TRUNCATED)     # agent mid-rewrite

    tasks, status = _get_both(client)
    assert [t["id"] for t in tasks["tasks"]] == ["001", "002", "003"]
    assert (tasks["tasksStale"], tasks["tasksSource"]) == (True, "last-good")
    # ... and the counts do NOT drop to zero for a file that exists and parsed
    assert status["tasks"] == PLAN_COUNTS
    assert (status["tasksStale"], status["tasksSource"]) == (True, "last-good")


def test_unparseable_with_no_last_good_is_flagged_rather_than_zero(
        client, tmp_path):
    (tmp_path / "tasks.json").write_text("{{{ not json")
    tasks, status = _get_both(client)
    assert tasks == {"tasksStale": True, "tasksSource": "unreadable"}
    # `total: 0` here is ignorance, and the flag is what says so -- a renderer
    # must label it instead of claiming a plan with no tasks.
    assert status["tasks"] == {"total": 0}
    assert (status["tasksStale"], status["tasksSource"]) == (True, "unreadable")


def test_a_plan_key_cannot_forge_the_freshness_flag(client, tmp_path):
    """The provenance is appended last, so an agent's own `tasksStale: false`
    inside tasks.json cannot make a stale read look fresh."""
    _write(tmp_path, {**PLAN, "tasksStale": False, "tasksSource": "file"})
    _get(client, "/tasks")
    (tmp_path / "tasks.json").write_text(TRUNCATED)
    tasks = _get(client, "/tasks")
    assert (tasks["tasksStale"], tasks["tasksSource"]) == (True, "last-good")


# -- the property the feature exists for, through HTTP ---------------------

def test_polling_both_endpoints_during_agent_rewrites_never_sees_an_empty_plan(
        client, tmp_path):
    """A pi-style non-atomic rewrite loop while an operator polls: every
    response keeps the plan and its counts, stale-flagged when it came from
    the fallback."""
    _write(tmp_path, PLAN)
    _get(client, "/tasks")
    tasks_file = tmp_path / "tasks.json"
    stop = threading.Event()

    def rewriter():
        while not stop.is_set():
            with open(tasks_file, "w") as fh:           # non-atomic, like json.dump
                fh.write('{"version": 1, "tasks": [')
                fh.flush()
                time.sleep(0.001)
                fh.write(json.dumps(PLAN["tasks"])[1:-1])
                fh.write("]}")
            time.sleep(0.001)

    async def poll():
        seen_sources: set[str] = set()
        async with client() as c:
            for _ in range(60):
                rt = await c.get("/tasks")
                rs = await c.get("/status")
                doc, status = rt.json(), rs.json()
                assert len(doc.get("tasks") or []) == 3, doc
                assert status["tasks"]["total"] == 3, status["tasks"]
                assert status["tasksSource"] in ("file", "last-good")
                # freshness and the flag always agree
                assert status["tasksStale"] is (status["tasksSource"] == "last-good")
                seen_sources.add(status["tasksSource"])
        return seen_sources

    writer = threading.Thread(target=rewriter)
    writer.start()
    try:
        seen = asyncio.run(poll())
    finally:
        stop.set()
        writer.join()
    assert seen  # at least one source observed; both are legitimate outcomes
