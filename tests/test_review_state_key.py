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
    # the hold on the judged state spends no no-task worker iteration
    assert runner.calls.get("worker", 0) == 0, runner.phases_seen
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


def test_the_deferral_stores_the_key_the_review_was_dispatched_on(tmp_path):
    """HEAD moving while the review runs is a state the review never saw:
    the remembered key is the dispatch's, so the next dispatch runs."""
    sup, runner = _supervisor(tmp_path)
    dispatched_head = sup._head

    def verified_after_a_commit(s, prompt):
        s._head = "c" * 40          # a commit lands mid-review
        return _ok({"verdict": "verified"})

    runner.handlers["review"] = [verified_after_a_commit, _verified]
    assert _step_review(sup)[2] == "steering_pending_at_verified"
    entry = sup.run.read_status()["reviewStateKeys"]["1"]
    assert entry["verifiedDeferred"]["key"] == entry["dispatched"]
    assert entry["verifiedDeferred"]["key"]["head"] == dispatched_head

    out = _step_review(sup)

    assert runner.calls["review"] == 2, "the new HEAD is judged"
    assert out[:2] == ("advance", "worker")


def test_a_consumed_noop_steering_message_on_an_equal_key_parks(tmp_path):
    """A message the `steer` hat consumed without moving HEAD or any task
    triple leaves the judged state as it was: the equal key parks
    `verified_unchanged` instead of dispatching the review again."""
    sup, runner = _supervisor(tmp_path)
    sup.run.add_steering("also check the ordering", name="note")
    assert _step_review(sup)[2] == "steering_pending_at_verified"
    assert runner.calls["review"] == 1

    sup.run.consume_steering(sup.run.pending_steering(), 1)
    out = _step_review(sup)

    assert out == ("advance", "paused", "verified_unchanged")
    assert runner.calls["review"] == 1, "the review hat was not dispatched"
    status = sup.run.read_status()
    assert status["state"] == "awaiting_input"
    assert status["pauseReason"] == "verified_unchanged"


def test_an_equal_key_with_nothing_holding_the_verified_reuses_it(tmp_path):
    """Every task terminal, the VERIFIED deferred only on a steering message
    the `steer` hat then consumed without moving HEAD or a task: the equal
    key does not dispatch the review again; the remembered VERIFIED takes
    review's verified edge (a park would name nothing an answer changes)."""
    sup, runner = _supervisor(tmp_path)
    for tid in [HELD] + STEERING:
        _set_status(sup, tid, "completed")
    sup.run.add_steering("also check the ordering", name="note")
    assert _step_review(sup)[2] == "steering_pending_at_verified"

    sup.run.consume_steering(sup.run.pending_steering(), 1)
    out = _step_review(sup)

    assert out == ("done", "succeeded")
    assert runner.calls["review"] == 1, "the review hat was not dispatched"
    assert '"review_verdict_reused"' in (sup.run.root / "events.jsonl").read_text()


def _truncated(sup, prompt):
    """A reply that ended `stopReason: length` with no text: re-asked."""
    r = IterationResult(exit_code=0)
    r.final_text = ""
    r.output_truncated = True
    r.duration_s = 1.0
    r.usage = {"input": 1, "output": 1, "totalTokens": 2}
    return r


def _interrupted(sup, prompt):
    """An attempt the engine cut short: the review self-loops in place."""
    r = IterationResult(exit_code=-2, interrupted=True)
    r.final_text = ""
    r.duration_s = 1.0
    return r


def _first_attempt_then_commit(shape):
    def attempt(s, prompt):
        result = shape(s, prompt)
        s._head = "d" * 40          # a commit lands before the retry
        return result
    return attempt


@pytest.mark.parametrize("shape", [_truncated, _interrupted],
                         ids=["truncated_reply", "interrupted_attempt"])
def test_a_retried_review_binds_its_verified_to_the_retry_s_head(
        tmp_path, shape):
    """The key is recorded per ATTEMPT: when the first attempt yields no
    verdict and the retry returns VERIFIED on a newer HEAD, the deferral
    names the retry's HEAD. With nothing changed after it, the next entry
    into review parks `verified_unchanged` instead of judging that HEAD
    again."""
    sup, runner = _supervisor(tmp_path)
    first_head = sup._head
    runner.handlers["review"] = [_first_attempt_then_commit(shape),
                                 _verified, _verified]

    assert _step_review(sup)[2] == "steering_pending_at_verified"
    assert runner.calls["review"] == 2, "the first attempt was retried"
    entry = sup.run.read_status()["reviewStateKeys"]["1"]
    assert entry["verifiedDeferred"]["key"]["head"] == "d" * 40 != first_head
    assert entry["verifiedDeferred"]["key"] == entry["dispatched"]

    out = _step_review(sup)

    assert out == ("advance", "paused", "verified_unchanged")
    assert runner.calls["review"] == 2, "the judged HEAD is not re-judged"


@pytest.mark.parametrize("shape", [_truncated, _interrupted],
                         ids=["truncated_reply", "interrupted_attempt"])
def test_the_first_attempt_s_unjudged_head_is_a_changed_key(tmp_path, shape):
    """The converse: the HEAD only the unanswered first attempt saw was never
    judged, so returning to it dispatches the review normally."""
    sup, runner = _supervisor(tmp_path)
    first_head = sup._head
    runner.handlers["review"] = [_first_attempt_then_commit(shape),
                                 _verified, _verified]
    assert _step_review(sup)[2] == "steering_pending_at_verified"
    assert runner.calls["review"] == 2

    sup._head = first_head
    out = _step_review(sup)

    assert runner.calls["review"] == 3, "the unjudged HEAD is judged"
    assert out[:2] == ("advance", "worker")
    assert sup.run.read_status().get("pauseReason") != "verified_unchanged"
