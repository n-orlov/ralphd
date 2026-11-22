"""v0.38 B.1-B.2 (#320): the final review is never dispatched twice on the
state a deferred VERIFIED already judged.

Each final-review dispatch records, in the approach's state (`status.json`
`reviewStateKeys`), a key: the workspace HEAD sha plus the sorted
`(task id, status, settled)` triples over the plan. A VERIFIED that defers to
the worker ("VERIFIED verdict deferring") stores the key with the verdict.
A later dispatch on an equal key does not run the review hat: the run parks
`awaiting_input` with reason `verified_unchanged`, naming the non-terminal
tasks and what blocks each. A release with nothing changed parks again.

Driven through the real `_step_review` / `_step_worker` / `_step_paused`
with fake hats and a fake clock; the park's wait is a recorder, so nothing
sleeps.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from ralphd.engine import state as engine_state
from ralphd.engine.loop import LoopSupervisor
from ralphd.engine.runner import IterationResult
from tests.conftest import ScriptedRunner, attach_runner, make_supervisor

HELD = "001"
STEERING = ["002", "003", "004"]
FAKE_NOW = 1_800_000_000.0


class _Parked(Exception):
    """Raised by the stubbed park wait: the run reached `awaiting_input`."""


def _ok(payload: dict) -> IterationResult:
    r = IterationResult(exit_code=0)
    r.final_text = json.dumps(payload)
    r.duration_s = 1.0
    r.usage = {"input": 1, "output": 1, "totalTokens": 2}
    return r


def _verified(sup, prompt):
    return _ok({"verdict": "verified"})


def _no_task_worker(sup, prompt):
    return _ok({"verdict": "no_verdict"})


def _plan() -> list[dict]:
    """The board-phase5 shape: a petitioned task and three steering-origin
    tasks that wait on it, so a VERIFIED review is deferred."""
    return [
        {"id": HELD, "title": "the impossible one", "status": "petitioned",
         "successCriteria": "criterion 2 holds"},
    ] + [
        {"id": tid, "title": f"steered {tid}", "status": "pending",
         "origin": "steer", "dependsOn": [HELD], "successCriteria": "done"}
        for tid in STEERING]


def _supervisor(tmp_path):
    sup = make_supervisor(
        tmp_path / "run", run_id="review-state-key", cfg_defaults=False,
        max_approaches=1, vigilant=False, on_complete="exit", iterations=60,
        route_task_selection=False,
        infra_retry_backoff_s=[0.0], infra_retry_backoff_max_s=0.0,
        infra_outage_budget_s=0.0, approach=1, tasks=_plan())
    sup._now_epoch = lambda: FAKE_NOW          # the fake clock
    sup._head = "a" * 40
    sup._workspace_head_sha = lambda: sup._head
    runner = attach_runner(sup, ScriptedRunner({
        "review": [_verified], "worker": [_no_task_worker]}))
    return sup, runner


def _recorder(sup):
    """Replace the awaiting_input wait: record `status.json` and stop the
    drive by raising instead of sleeping."""
    snapshot: dict = {}

    async def parked(*a, **k):
        snapshot.clear()
        snapshot.update(sup.run.read_status())
        raise _Parked()

    sup._wait_for_answer_or_expiry = parked
    return snapshot


async def _drive(sup, start: str, limit: int = 40):
    """Step the FSM from `start` until a step stops advancing; returns the
    state it stopped in."""
    st = start
    for _ in range(limit):
        out = await getattr(sup, f"_step_{st}")()
        if out[0] != "advance":
            return st, out
        st = out[1]
    raise AssertionError("the drive never settled")


def _run(sup, start: str):
    return asyncio.run(asyncio.wait_for(_drive(sup, start), 30.0))


def _status_of(sup, task_id: str) -> str:
    return next(t["status"] for t in sup.run.read_tasks()["tasks"]
                if t["id"] == task_id)


def _set_status(sup, task_id: str, status: str) -> None:
    doc = sup.run.read_tasks()
    for t in doc["tasks"]:
        if t["id"] == task_id:
            t["status"] = status
    sup.run.write_tasks(doc)


def _step_review(sup):
    return asyncio.run(asyncio.wait_for(sup._step_review(), 30.0))


def _deferral_events(sup) -> int:
    return sum(
        1 for line in sup.run.events_file.read_text().splitlines()
        if "VERIFIED verdict deferring" in line)


def test_the_key_is_recorded_per_dispatch_and_stored_with_a_deferred_verified(
        tmp_path):
    sup, runner = _supervisor(tmp_path)

    out = _step_review(sup)

    assert out == ("advance", "worker", "steering_pending_at_verified")
    entry = sup.run.read_status()["reviewStateKeys"]["1"]
    expected = {
        "head": "a" * 40,
        "tasks": [[HELD, "petitioned", False]]
        + [[tid, "pending", False] for tid in STEERING]}
    assert entry["dispatched"] == expected
    assert entry["verifiedDeferred"]["key"] == expected
    assert entry["verifiedDeferred"]["verdict"] == "VERIFIED"
    assert entry["verifiedDeferred"]["blockers"]


def test_the_key_marks_a_settled_petitioned_task_settled(tmp_path):
    sup, _runner = _supervisor(tmp_path)
    doc = sup.run.read_tasks()
    key = engine_state.review_state_key(
        "h", list(reversed(doc["tasks"])), settled={HELD})
    assert key["tasks"][0] == [HELD, "petitioned", True]
    assert [row[0] for row in key["tasks"]] == sorted(
        [HELD] + STEERING), "the triples are sorted by task id"
    assert key == engine_state.review_state_key("h", doc["tasks"], {HELD})


def test_board_shape_dispatches_review_at_most_twice_then_parks_verified_unchanged(
        tmp_path):
    sup, runner = _supervisor(tmp_path)
    snapshot = _recorder(sup)

    with pytest.raises(_Parked):
        _run(sup, "review")

    assert runner.calls["review"] <= 2, runner.phases_seen
    # the hold's own bound was spent once, on the way back to review
    assert runner.calls["worker"] == (
        LoopSupervisor.PETITION_HELD_MAX_NO_TASK_ITERATIONS)
    assert snapshot["state"] == "awaiting_input"
    assert snapshot["pauseReason"] == "verified_unchanged"
    question = snapshot["inputQuestion"]
    assert question["kind"] == "verified_unchanged"
    named = {e["taskId"]: e for e in question["heldTasks"]}
    assert set(STEERING) <= set(named), "the non-terminal tasks are named"
    for tid in STEERING:
        assert named[tid]["blockedBy"] == [HELD]
    for tid in STEERING + [HELD]:
        assert tid in question["text"]
    assert "blocked by 001" in question["text"]


def test_an_unpause_with_no_change_parks_again_without_a_review_dispatch(
        tmp_path):
    sup, runner = _supervisor(tmp_path)
    snapshot = _recorder(sup)
    with pytest.raises(_Parked):
        _run(sup, "review")
    calls = dict(runner.calls)
    assert calls["review"] == 1

    # the operator answers with nothing changed: the park is released ...
    del sup._wait_for_answer_or_expiry
    record = sup.answer()
    assert record["kind"] == "verified_unchanged"
    # ... and the run re-parks instead of judging the same state again
    snapshot = _recorder(sup)
    snapshot.clear()
    with pytest.raises(_Parked):
        _run(sup, "paused")

    assert runner.calls == calls, "no review and no worker iteration"
    assert snapshot["state"] == "awaiting_input"
    assert snapshot["pauseReason"] == "verified_unchanged"


@pytest.mark.parametrize("change", ["head", "task_status"])
def test_a_changed_key_dispatches_review_normally(tmp_path, change):
    sup, runner = _supervisor(tmp_path)
    assert _step_review(sup)[2] == "steering_pending_at_verified"
    assert runner.calls["review"] == 1

    if change == "head":
        sup._head = "b" * 40
    else:
        _set_status(sup, STEERING[0], "completed")

    out = _step_review(sup)

    assert runner.calls["review"] == 2, "the review hat was dispatched"
    assert out[:2] == ("advance", "worker")
    status = sup.run.read_status()
    assert status.get("state") != "awaiting_input"
    assert status.get("pauseReason") != "verified_unchanged"
    entry = status["reviewStateKeys"]["1"]
    assert entry["dispatched"]["head"] == sup._head
    assert entry["verifiedDeferred"]["key"] == entry["dispatched"]


def test_a_settled_steering_message_dispatches_review_on_an_equal_key(tmp_path):
    """A message the `steer` hat settled changes neither HEAD nor a task
    triple, yet it is what held the VERIFIED: the review is asked again."""
    sup, runner = _supervisor(tmp_path)
    doc = sup.run.read_tasks()
    for t in doc["tasks"]:
        t["status"] = "completed"
    sup.run.write_tasks(doc)
    sup.run.add_steering("also check the ordering", name="note")
    assert _step_review(sup)[2] == "steering_pending_at_verified"
    assert runner.calls["review"] == 1

    sup.run.consume_steering(sup.run.pending_steering(), 1)
    out = _step_review(sup)

    assert runner.calls["review"] == 2
    assert out == ("done", "succeeded")
