"""v0.38 K.1: a run with no petitioned task is exactly the run 0.37 drove.

Every 0.38 change in the review/scheduling path (the settled-dependency
rule, the review state key and its `verified_unchanged` park, the
worker-to-review edge holds, the terminal sibling reap) is keyed on a
petitioned task or a deferred VERIFIED. A plan with none of those whose first
VERIFIED goes terminal therefore:

* schedules the same tasks in the same order (highest `priority` among tasks
  whose `dependsOn` are all completed, ties by list order);
* dispatches the review hat exactly once;
* never parks `verified_unchanged` and never reuses a verdict;
* emits no event type 0.37 did not emit, except `siblings.reaped`, and that
  one only when the job has docker access.

Driven through the real `run_job` with fake hats, a fake clock source and a
fake docker client; nothing sleeps.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from test_engine_finishing_state import _events, _ok, _review_ok, _supervisor
from test_terminal_sibling_reap import FakeDocker, _c

from ralphd.engine.runner import IterationResult
from ralphd.engine.state import RunDir, atomic_write_json

RUN = "v08-finishing"        # the run id `_supervisor` configures

# The plan: 002 waits on 001; 003 outranks 001 on `priority`; 004 ties with
# 003 and comes later in the list. No petitioned task, no steering origin.
PLAN = [
    {"id": "001", "title": "base", "successCriteria": "c1", "priority": 1},
    {"id": "002", "title": "needs base", "successCriteria": "c2",
     "dependsOn": ["001"], "priority": 9},
    {"id": "003", "title": "urgent", "successCriteria": "c3", "priority": 5},
    {"id": "004", "title": "also urgent", "successCriteria": "c4",
     "priority": 5},
]
# highest priority first among the unblocked; ties by list order; 002 only
# once 001 is completed.
EXPECTED_ORDER = ["003", "004", "001", "002"]

# Every event type a 0.37 engine emits for a succeeding run of this shape
# (each is emitted by the 0.37 sources at bdf105a7). Anything a run emits
# outside this set is a type 0.38 added.
EVENT_TYPES_0_37 = {
    "approach.verdict_recorded", "engine.started", "iteration.end",
    "iteration.start", "log", "notifier.unconfigured",
    "petitions_paths_read", "phase", "planning.warning", "signal", "state",
    "task",
}


def _bounded(coro, timeout: float = 30.0):
    return asyncio.run(asyncio.wait_for(coro, timeout))


def _plan_hat(sup) -> IterationResult:
    atomic_write_json(sup.run.root / "plan-proposal.json",
                      {"version": 1, "tasks": PLAN})
    return _ok("planned\n" + json.dumps(
        {"proposal_path": "plan-proposal.json", "summary": "planned"}))


def _worker_hat(order: list):
    def hat(sup) -> IterationResult:
        tasks = sup.run.read_tasks()["tasks"]
        assigned = [t["id"] for t in tasks if t["status"] == "in-progress"]
        assert len(assigned) == 1, assigned
        order.append(assigned[0])
        return _ok("done\n" + json.dumps(
            {"verdict": "complete", "task_id": assigned[0]}))
    return hat


def _drive(tmp_path, monkeypatch, *, docker):
    for k in ("RALPHD_HOST_WORKSPACE", "RALPHD_HOST_WORKSPACES",
              "RALPHD_HOST_RUN_DIR"):
        monkeypatch.delenv(k, raising=False)
    if docker:
        monkeypatch.setenv("RALPHD_HOST_RUN_DIR", "/host/run")
    root = tmp_path / "run"
    RunDir(root=root).update_status(state="starting")
    order: list[str] = []
    reviews: list[int] = []

    def review(sup) -> IterationResult:
        reviews.append(1)
        return _review_ok(sup)

    hats = [_plan_hat] + [_worker_hat(order)] * len(PLAN) + [review]
    sup = _supervisor(root, hats)
    fake = FakeDocker([_c("sibling-1"), _c("job", role="job")])
    if docker:
        sup.docker_runner = fake
    final = _bounded(sup.run_job())
    return sup, final, order, reviews, fake


def _all_events(root) -> list[dict]:
    log = root / "events.jsonl"
    return [json.loads(x) for x in log.read_text().splitlines() if x.strip()]


@pytest.fixture
def no_docker(tmp_path, monkeypatch):
    return _drive(tmp_path, monkeypatch, docker=False)


def test_the_same_tasks_run_in_the_same_order(no_docker):
    _sup, final, order, _reviews, _fake = no_docker
    assert final == "succeeded"
    assert order == EXPECTED_ORDER


def test_the_review_is_dispatched_once_and_the_first_verified_ends_the_run(
        no_docker):
    sup, final, _order, reviews, _fake = no_docker
    assert final == "succeeded" and len(reviews) == 1
    assert sup.run.read_status()["verdict"] == "verified"
    assert [t["status"] for t in sup.run.read_tasks()["tasks"]] == \
        ["completed"] * len(PLAN)


def test_no_verified_unchanged_park_and_no_reused_verdict(no_docker):
    sup, _final, _order, _reviews, _fake = no_docker
    status = sup.run.read_status()
    assert status.get("inputQuestion") in (None, {})
    assert "verified_unchanged" not in json.dumps(status)
    assert status.get("pauseReason") != "verified_unchanged"
    parks = [e for e in _all_events(sup.run.root)
             if "verified_unchanged" in json.dumps(
                 {k: v for k, v in e.items() if k != "path"})
             and e["type"] != "petitions_paths_read"]
    assert parks == []
    assert [e["state"] for e in _events(sup.run.root, "state")
            if e["state"] == "awaiting_input"] == []
    assert _events(sup.run.root, "review_verdict_reused") == []
    assert not (status.get("reviewStateKeys") or {}).get("1", {}).get(
        "verifiedDeferred")


def test_no_event_type_beyond_0_37s_without_docker_access(no_docker):
    sup, _final, _order, _reviews, _fake = no_docker
    types = {e["type"] for e in _all_events(sup.run.root)}
    assert "siblings.reaped" not in types
    assert types - EVENT_TYPES_0_37 == set()


def test_with_docker_access_only_siblings_reaped_is_new(tmp_path, monkeypatch):
    sup, final, order, reviews, fake = _drive(
        tmp_path, monkeypatch, docker=True)
    assert final == "succeeded" and order == EXPECTED_ORDER
    assert len(reviews) == 1
    types = {e["type"] for e in _all_events(sup.run.root)}
    assert types - EVENT_TYPES_0_37 == {"siblings.reaped"}
    assert "sibling-1" not in fake.ids() and "job" in fake.ids()
