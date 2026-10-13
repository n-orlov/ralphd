"""#292 (PRD C.1, C.2, C.5): `POST /tasks/{task_id}/undefer` releases a
deferred task early. The API handler only checks and queues; the loop thread
applies the release at its selection boundary, clears the deferral and emits
`task.undeferred`; a deferred wait it ends reads `boundedBy: "undefer"`; the
task's transition history shows it as an operator cut.
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import httpx
import pytest

from ralphd.engine.api import create_app
from ralphd.engine.runner import IterationResult
from ralphd.engine.state import (
    RunDir, atomic_write_json, read_events, task_transition_history,
    utc_from_epoch,
)
from tests.conftest import ScriptedRunner, make_supervisor, scripted_supervisor

CONTROL_TOKEN = "control-secret-for-tests"
CONTROL_HEADER = "X-Ralphd-Control-Token"
TASK_1, TASK_2 = "001", "002"
REASON = "waiting on a gated window to open"


def _ok(text: str) -> IterationResult:
    r = IterationResult(exit_code=0)
    r.final_text = text
    r.duration_s = 5.0
    r.usage = {"input": 10, "output": 5, "totalTokens": 15}
    return r


def _complete(task_id: str) -> IterationResult:
    return _ok(f"done\n{json.dumps({'verdict': 'complete', 'task_id': task_id})}")


def _review_verifies(sup, prompt=""):
    return _ok("holds\n" + json.dumps({"verdict": "verified"}))


def _task(tid=TASK_1, **over) -> dict:
    task = {"id": tid, "title": f"task {tid}", "status": "pending",
            "successCriteria": "it works"}
    task.update(over)
    return task


def _events(root: Path, *types) -> list[dict]:
    return read_events(root, types=types or None)


_supervisor = scripted_supervisor(run_id="undefer", iterations=15,
                                  max_approaches=1, vigilant=False,
                                  on_complete="exit",
                                  infra_outage_budget_s=1000.0)


def _build(root: Path, handlers: dict, tasks: list[dict], **cfg_kw):
    RunDir(root=root).update_status(state="starting")
    sup = _supervisor(root, handlers, **cfg_kw)
    atomic_write_json(sup.run.tasks_file, {"version": 1, "tasks": tasks})
    sup.iterations_used = 1
    return sup


# --------------------------------------------------------------- API answers


def _api_sup(tmp_path, tasks, control_token=None, **status_over):
    status = {"state": "running", "iterationsBudget": 5, "iterationsUsed": 0}
    status.update(status_over)
    kw = {"control_token": control_token} if control_token else {}
    sup = make_supervisor(tmp_path, run_id="undefer-api", cfg_defaults=False,
                          status=status, iterations=5,
                          runner=ScriptedRunner(), **kw)
    atomic_write_json(sup.run.tasks_file, {"version": 1, "tasks": tasks})
    return sup


def _client(sup) -> httpx.AsyncClient:
    app = create_app(sup.cfg, sup.run, sup)
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                             base_url="http://engine")


def _deferred_task(tid=TASK_1) -> dict:
    return _task(tid, deferredUntil=utc_from_epoch(time.time() + 600),
                 deferredReason=REASON)


@pytest.mark.asyncio
async def test_undefer_accepts_a_deferred_task_queues_it_and_rings_the_bell(tmp_path):
    sup = _api_sup(tmp_path, [_deferred_task()])
    before = sup.run.tasks_file.read_text()
    async with _client(sup) as client:
        resp = await client.post(f"/tasks/{TASK_1}/undefer")
    assert resp.status_code == 200, resp.text
    assert resp.json()["taskId"] == TASK_1
    assert sup._deferred_wake.is_set()
    assert sup._undefer_requests == [TASK_1]
    # the API thread never writes tasks.json nor emits the release event
    assert sup.run.tasks_file.read_text() == before
    assert _events(sup.run.root, "task.undeferred") == []


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["succeeded", "failed", "aborted",
                                    "exhausted"])
async def test_undefer_on_a_finished_run_is_409_and_enqueues_nothing(tmp_path, state):
    sup = _api_sup(tmp_path, [_deferred_task()], state=state)
    async with _client(sup) as client:
        resp = await client.post(f"/tasks/{TASK_1}/undefer")
    assert resp.status_code == 409, resp.text
    assert sup._undefer_requests == []
    assert not sup._deferred_wake.is_set()


@pytest.mark.asyncio
async def test_undefer_of_an_unknown_task_is_404(tmp_path):
    sup = _api_sup(tmp_path, [_deferred_task()])
    async with _client(sup) as client:
        resp = await client.post("/tasks/nope/undefer")
    assert resp.status_code == 404, resp.text
    assert sup._undefer_requests == []


@pytest.mark.asyncio
async def test_undefer_of_a_task_that_is_not_deferred_is_409_saying_so(tmp_path):
    sup = _api_sup(tmp_path, [_task()])
    async with _client(sup) as client:
        resp = await client.post(f"/tasks/{TASK_1}/undefer")
    assert resp.status_code == 409, resp.text
    assert "not deferred" in resp.text
    assert sup._undefer_requests == []
    assert not sup._deferred_wake.is_set()


@pytest.mark.asyncio
async def test_undefer_requires_the_control_token(tmp_path):
    sup = _api_sup(tmp_path, [_deferred_task()], control_token=CONTROL_TOKEN)
    async with _client(sup) as client:
        refused = await client.post(f"/tasks/{TASK_1}/undefer")
        wrong = await client.post(f"/tasks/{TASK_1}/undefer",
                                  headers={CONTROL_HEADER: "wrong"})
        assert refused.status_code == 401, refused.text
        assert wrong.status_code == 401, wrong.text
        assert sup._undefer_requests == []
        ok = await client.post(f"/tasks/{TASK_1}/undefer",
                               headers={CONTROL_HEADER: CONTROL_TOKEN})
    assert ok.status_code == 200, ok.text
    assert sup._undefer_requests == [TASK_1]


# ------------------------------------------------- loop application of a release


async def _undefer_after(sleep_fn, sup, delay: float, task_id: str):
    await sleep_fn(delay)
    sup.request_undefer(task_id)


def test_undefer_ends_a_deferred_wait_and_the_task_is_selectable_at_once(tmp_path):
    root = tmp_path / "run"
    until = utc_from_epoch(time.time() + 600)
    sup = _build(root, {"worker": [lambda s, p: _complete(TASK_1)],
                        "review": [_review_verifies]},
                 [_task(deferredUntil=until, deferredReason=REASON)],
                 job_timeout_s=3600, real_sleep=True)
    real_sleep = asyncio.sleep

    async def drive():
        waker = asyncio.ensure_future(
            _undefer_after(real_sleep, sup, 0.15, TASK_1))
        started = time.time()
        final = await sup._run_job_core()
        await waker
        return final, time.time() - started

    final, elapsed = asyncio.run(drive())
    assert elapsed < 60, "the 600 s deferral was not cut short"
    assert final == "succeeded", sup.run.read_status()

    waits = _events(root, "deferred_wait")
    assert len(waits) == 1, waits
    assert waits[0]["boundedBy"] == "undefer"
    assert waits[0]["taskId"] == TASK_1
    assert waits[0]["sleptUntil"] == until
    assert waits[0]["waitedS"] < 5

    released = _events(root, "task.undeferred")
    assert len(released) == 1, released
    ev = released[0]
    assert (ev["taskId"], ev["until"], ev["reason"], ev["by"]) == (
        TASK_1, until, REASON, "operator")
    # selectable at once: the task was worked, never waited out
    task = sup.run.read_tasks()["tasks"][0]
    assert task["status"] == "completed"
    assert "deferredUntil" not in task and "deferredReason" not in task


def test_undefer_while_another_task_is_worked_lands_before_the_next_selection(tmp_path):
    root = tmp_path / "run"
    until = utc_from_epoch(time.time() + 600)
    seen: dict = {}

    def work_one_and_undefer_two(sup, prompt=""):
        # the operator's request arrives mid-iteration (API handler's act)
        sup.request_undefer(TASK_2)
        seen["during"] = sup.run.read_tasks()["tasks"][1].get("deferredUntil")
        return _complete(TASK_1)

    def work_two(sup, prompt=""):
        # the release was applied before this selection
        seen["at_selection"] = sup.run.read_tasks()["tasks"][1].get("deferredUntil")
        return _complete(TASK_2)

    sup = _build(root, {"worker": [work_one_and_undefer_two, work_two],
                        "review": [_review_verifies, _review_verifies]},
                 [_task(TASK_1),
                  _task(TASK_2, deferredUntil=until, deferredReason=REASON)],
                 job_timeout_s=3600)
    final = asyncio.run(asyncio.wait_for(sup._run_job_core(), 30.0))
    assert final == "succeeded", sup.run.read_status()
    # not applied by the API/mid-iteration, only at the loop's boundary
    assert seen["during"] == until
    assert seen["at_selection"] is None
    # task 002 was worked directly: no deferred wait happened at all
    assert _events(root, "deferred_wait") == []
    released = _events(root, "task.undeferred")
    assert [e["taskId"] for e in released] == [TASK_2]
    assert released[0]["until"] == until and released[0]["by"] == "operator"


def test_a_release_whose_task_stopped_being_deferred_changes_nothing(tmp_path):
    root = tmp_path / "run"
    sup = _build(root, {"worker": [], "review": []}, [_task()])
    before = sup.run.tasks_file.read_text()
    mtime = sup.run.tasks_file.stat().st_mtime_ns
    sup.request_undefer(TASK_1)
    assert sup._apply_undefer_requests() == []
    assert sup._undefer_requests == []
    assert _events(root, "task.undeferred") == []
    assert sup.run.tasks_file.read_text() == before
    assert sup.run.tasks_file.stat().st_mtime_ns == mtime


def test_a_duplicate_release_emits_one_event(tmp_path):
    root = tmp_path / "run"
    sup = _build(root, {"worker": [], "review": []},
                 [_deferred_task()])
    sup.request_undefer(TASK_1)
    sup.request_undefer(TASK_1)
    assert sup._apply_undefer_requests() == [TASK_1]
    assert len(_events(root, "task.undeferred")) == 1


# ------------------------------------------------------------ deferral history


def test_history_renders_an_operator_release_distinct_from_an_expiry(tmp_path):
    root = tmp_path / "run"
    until = utc_from_epoch(time.time() + 600)
    sup = _build(root, {"worker": [], "review": []},
                 [_task(deferredUntil=until, deferredReason=REASON)])
    sup.request_undefer(TASK_1)
    sup._apply_undefer_requests()
    history = task_transition_history(root, TASK_1)
    cut = [h for h in history if h.get("kind") == "undeferred"]
    assert len(cut) == 1, history
    assert cut[0]["by"] == "operator"
    assert cut[0]["cause"].startswith("operator: undeferred")
    assert until in cut[0]["cause"] and REASON in cut[0]["cause"]
    assert "expired" not in cut[0]["cause"] and "elapsed" not in cut[0]["cause"]
    # a deferral's own expiry records no `task` event, so it can never take
    # this entry's shape
    assert all(h.get("kind") != "undeferred" for h in history if h is not cut[0])
