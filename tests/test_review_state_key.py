"""v0.38 B.1-B.2 (#320): the final review is never dispatched twice on the
state a deferred VERIFIED already judged.

Each final-review dispatch records, in the approach's state (`status.json`
`reviewStateKeys`), a key: the workspace HEAD sha plus the sorted
`(task id, status, settled)` triples over the plan. A VERIFIED that defers to
the worker ("VERIFIED verdict deferring") stores the key with the verdict.
A later dispatch on an equal key does not run the review hat: the run parks
`awaiting_input` with reason `verified_unchanged`, naming the non-terminal
tasks and what blocks each. A release with nothing changed parks again; so does an equal key that
nothing holds any more (no exception for a blocker that went away outside
the key: a consumed or withdrawn steering message, a cleared origin marker).

Driven through the real `_step_review` / `_step_worker` / `_step_paused`
with fake hats and a fake clock; the park's wait is a recorder, so nothing
sleeps.
"""

from __future__ import annotations

import asyncio
import json
import time

import pytest

from ralphd.engine import state as engine_state
from ralphd.engine.runner import IterationResult
from tests import test_review_state_key_edges as edges
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


def _all_terminal_with_consumed_steering(sup):
    for tid in [HELD] + STEERING:
        _set_status(sup, tid, "completed")
    sup.run.add_steering("also check the ordering", name="note")
    assert _step_review(sup)[2] == "steering_pending_at_verified"
    sup.run.consume_steering(sup.run.pending_steering(), 1)


def test_an_equal_key_with_nothing_holding_the_verified_still_parks(tmp_path):
    """Every task terminal, the VERIFIED deferred only on a steering message
    the `steer` hat then consumed without moving HEAD or a task: the blocker
    went away outside the key, which is no exception. The key is equal, so
    the review is not dispatched and the run parks `verified_unchanged`
    (B.2 / DoD 2); it does not take the remembered verdict."""
    sup, runner = _supervisor(tmp_path)
    _all_terminal_with_consumed_steering(sup)

    out = _step_review(sup)

    assert out == ("advance", "paused", "verified_unchanged")
    assert runner.calls["review"] == 1, "the review hat was not dispatched"
    status = sup.run.read_status()
    assert status["state"] == "awaiting_input"
    assert status["pauseReason"] == "verified_unchanged"
    assert status["inputQuestion"]["kind"] == "verified_unchanged"
    assert status["inputQuestion"]["heldTasks"] == []
    assert "no task is non-terminal now" in status["inputQuestion"]["text"]
    assert "review_verdict_reused" not in (
        sup.run.root / "events.jsonl").read_text()


def test_a_release_with_nothing_changed_and_no_blocker_parks_again(tmp_path):
    """Same rule, a second route: the operator answers the park of an equal
    key that nothing holds any more; the run re-enters review and parks
    again, never succeeding on the remembered VERIFIED."""
    sup, runner = _supervisor(tmp_path)
    _all_terminal_with_consumed_steering(sup)
    assert _step_review(sup)[2] == "verified_unchanged"
    sup.answer()

    out = _step_review(sup)

    assert out == ("advance", "paused", "verified_unchanged")
    assert runner.calls["review"] == 1
    assert sup.run.read_status()["state"] == "awaiting_input"


def test_the_recorded_blockers_are_not_part_of_the_equality_rule(tmp_path):
    """Same rule, a third route: the blockers stored beside the remembered
    key are context only. Whatever they say now (here: none), an equal key
    parks and a changed key dispatches."""
    sup, runner = _supervisor(tmp_path)
    assert _step_review(sup)[2] == "steering_pending_at_verified"
    entry = sup.run.read_status()["reviewStateKeys"]["1"]
    entry["verifiedDeferred"]["blockers"] = []
    sup.run.update_status(reviewStateKeys={"1": entry})

    assert _step_review(sup) == ("advance", "paused", "verified_unchanged")
    assert runner.calls["review"] == 1

    _set_status(sup, STEERING[0], "completed")
    sup.answer()
    assert _step_review(sup)[:2] == ("advance", "worker")
    assert runner.calls["review"] == 2, "a changed triple dispatches"


def _single_blocker_plan(sup, origin: str) -> None:
    """Every task terminal but one `origin` task still pending: it alone
    holds a VERIFIED (the terminal-success blocker its origin names)."""
    doc = sup.run.read_tasks()
    doc["tasks"] = [
        {"id": "101", "title": "done", "status": "completed",
         "successCriteria": "done"},
        {"id": "102", "title": "the remedy", "status": "pending",
         "origin": origin, "successCriteria": "done"},
    ]
    sup.run.write_tasks(doc)


def _clear_origin(sup, task_id: str) -> None:
    doc = sup.run.read_tasks()
    for t in doc["tasks"]:
        if t["id"] == task_id:
            t.pop("origin", None)
    sup.run.write_tasks(doc)


def _assert_parked_without_dispatch(sup, runner) -> None:
    status = sup.run.read_status()
    assert runner.calls["review"] == 1, "the review hat was not dispatched"
    assert status["state"] == "awaiting_input"
    assert status["pauseReason"] == "verified_unchanged"
    assert status["inputQuestion"]["kind"] == "verified_unchanged"
    assert "review_verdict_reused" not in (
        sup.run.root / "events.jsonl").read_text()


@pytest.mark.parametrize("origin,guard", [
    ("review-cure", "review_cure_pending_at_verified"),
    ("steer", "steering_pending_at_verified"),
], ids=["review_cure_origin_cleared", "steering_origin_cleared"])
def test_an_origin_marker_cleared_outside_the_key_still_parks(
        tmp_path, origin, guard):
    """The only terminal-success blocker is an origin-marked task; clearing
    its `origin` (no HEAD move, no status change) removes the blocker but
    leaves every `(id, status, settled)` triple as it was. The key is equal,
    so the final review parks `verified_unchanged` with one dispatch."""
    sup, runner = _supervisor(tmp_path)
    _single_blocker_plan(sup, origin)
    assert _step_review(sup)[2] == guard
    assert sup.run.read_status()["reviewStateKeys"]["1"]["verifiedDeferred"]

    _clear_origin(sup, "102")
    assert sup._terminal_success_blockers() == []

    out = _step_review(sup)

    assert out == ("advance", "paused", "verified_unchanged")
    _assert_parked_without_dispatch(sup, runner)
    held = sup.run.read_status()["inputQuestion"]["heldTasks"]
    assert [e["taskId"] for e in held] == ["102"]


def test_a_withdrawn_steering_file_on_an_equal_key_parks(tmp_path):
    """Every task terminal; the VERIFIED deferred only on a steering file no
    iteration processed. The file is then withdrawn from the steering
    directory (never consumed): the blocker is gone outside the key, so the
    next attempt parks with no dispatch."""
    sup, runner = _supervisor(tmp_path)
    for tid in [HELD] + STEERING:
        _set_status(sup, tid, "completed")
    sup.run.add_steering("also check the ordering", name="note")
    assert _step_review(sup)[2] == "steering_pending_at_verified"

    for path in sup.run.pending_steering():
        path.unlink()
    assert sup._terminal_success_blockers() == []

    out = _step_review(sup)

    assert out == ("advance", "paused", "verified_unchanged")
    _assert_parked_without_dispatch(sup, runner)
    assert sup.run.read_status()["inputQuestion"]["heldTasks"] == []


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


# ---------------------------------------------------------------------------
# B.3 controls (the peer verdict gate shares the key; the hold itself is
# pinned in tests/test_review_state_key_edges.py)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("change", ["head", "task_status"])
def test_a_changed_key_dispatches_the_final_peer_gate_normally(
        tmp_path, change):
    """A changed HEAD or task status is a changed key: the gate is
    dispatched."""
    sup, runner = edges._gate_supervisor(tmp_path)
    edges._bounded(sup._step_verdict_gate())
    edges._bounded(edges._release_with_nothing_changed(sup))

    if change == "head":
        sup._workspace_head_sha = lambda: "b" * 40
    else:
        doc = sup.run.read_tasks()
        doc["tasks"].append(edges._task("002", "completed"))
        sup.run.write_tasks(doc)

    out = edges._bounded(sup._step_verdict_gate())

    assert runner.calls["verdict_gate"] == 2, "the gate hat was dispatched"
    assert out != ("advance", "paused", "verified_unchanged")
    assert sup.run.read_status().get("pauseReason") != "verified_unchanged"


def _truncated_until(state: dict):
    """A reply that ended `stopReason: length` with no text (no verdict)
    until `state["usable"]` is set, then a passing verdict."""
    def hat(sup, prompt=""):
        if state.get("usable"):
            return edges._ok({"verdict": "verified"})
        r = IterationResult(exit_code=0)
        r.final_text = ""
        r.output_truncated = True
        r.duration_s = 1.0
        r.usage = {"input": 1, "output": 1, "totalTokens": 2}
        return r
    return hat


def test_a_gate_dispatch_with_no_usable_verdict_judged_nothing(tmp_path):
    """The converse of B.3: a gate dispatch that returned no usable verdict
    never judged the state, so the next entry dispatches the gate again."""
    hat_state: dict = {}
    sup, runner = edges._supervisor(
        tmp_path, [edges._task("001", "completed")],
        {"verdict_gate": [_truncated_until(hat_state)]},
        peer_verdict_gate=True)
    first = edges._bounded(sup._step_verdict_gate())
    assert first == ("tail",), first
    calls = runner.calls["verdict_gate"]
    recorded = (sup.run.read_status().get("reviewStateKeys") or {}).get("1")
    assert "gateJudged" not in (recorded or {})

    hat_state["usable"] = True
    second = edges._bounded(sup._step_verdict_gate())

    assert runner.calls["verdict_gate"] > calls, "the gate hat was dispatched"
    assert second == ("done", "succeeded")


# ---------------------------------------------------------------------------
# cure-01-05: the peer gate's key is B.1's key alone -- no budget in it
# ---------------------------------------------------------------------------

def _judge_then_release(sup):
    edges._bounded(sup._step_verdict_gate())
    edges._bounded(edges._release_with_nothing_changed(sup))


@pytest.mark.parametrize("delta", [+1, +5, -1], ids=["up_one", "up_five", "down"])
def test_a_cure_round_budget_change_does_not_reopen_a_judged_gate_key(
        tmp_path, delta):
    """After the gate judged a key, a cure-round top-up (or a lowered
    allowance) since that verdict is not part of the key: the same HEAD and
    task triples park `verified_unchanged` with no second gate dispatch."""
    sup, runner = edges._gate_supervisor(tmp_path)
    sup.set_cure_pass_ceiling(sup._cure_rounds_allowed(1) + 1)
    edges._bounded(sup._step_verdict_gate())
    edges._bounded(edges._release_with_nothing_changed(sup))
    before = sup._cure_rounds_allowed(1)

    sup.set_cure_pass_ceiling(before + delta)
    assert sup._cure_rounds_allowed(1) == before + delta

    out = edges._bounded(sup._step_verdict_gate())

    assert out == ("advance", "paused", "verified_unchanged")
    assert runner.calls["verdict_gate"] == 1, "the gate hat was dispatched"
    status = sup.run.read_status()
    assert status["pauseReason"] == "verified_unchanged"
    assert status["inputQuestion"]["judge"] == "verdict_gate"


def test_an_approving_gate_verdict_s_key_is_held_across_a_budget_change(
        tmp_path):
    """The rule is about the key a verdict was given on, not about a
    rejection: a gate that APPROVED the key (the run goes on to finish) has
    that key on record, and a second entry on it, whatever the budget now
    allows, parks instead of dispatching."""
    sup, runner = edges._supervisor(
        tmp_path, [edges._task("001", "completed")],
        {"verdict_gate": [edges._reply(verdict="verified")]},
        peer_verdict_gate=True)
    first = edges._bounded(sup._step_verdict_gate())
    assert first == ("done", "succeeded"), first
    judged = sup.run.read_status()["reviewStateKeys"]["1"]["gateJudged"]
    assert judged["key"] == sup._review_state_key()

    sup.set_cure_pass_ceiling(sup._cure_rounds_allowed(1) + 3)
    again = edges._bounded(sup._step_verdict_gate())

    assert again == ("advance", "paused", "verified_unchanged")
    assert runner.calls["verdict_gate"] == 1


def test_the_judged_gate_key_records_no_budget(tmp_path):
    """The record holds the key and nothing that is not part of it."""
    sup, _ = edges._gate_supervisor(tmp_path)
    edges._bounded(sup._step_verdict_gate())

    judged = sup.run.read_status()["reviewStateKeys"]["1"]["gateJudged"]

    assert set(judged) == {"key"}


def test_a_legacy_gate_record_with_an_allowance_is_still_held_by_the_key(
        tmp_path):
    """A `gateJudged` record written with a `cureRoundsAllowed` (earlier
    shape) that differs from the allowance now: the key alone decides."""
    sup, runner = edges._gate_supervisor(tmp_path)
    key = sup._review_state_key()
    sup._write_review_key_entry(1, {"gateJudged": {
        "key": key, "cureRoundsAllowed": sup._cure_rounds_allowed(1) + 7}})

    out = edges._bounded(sup._step_verdict_gate())

    assert out == ("advance", "paused", "verified_unchanged")
    assert runner.calls.get("verdict_gate", 0) == 0


def test_a_final_review_dispatch_on_the_same_key_keeps_the_gate_s_judgement(
        tmp_path):
    """Another attempt on the same HEAD and task triples is held whichever
    stage made it: a final-review attempt on the key the gate judged does not
    wipe the gate's record."""
    sup, runner = edges._gate_supervisor(tmp_path)
    edges._bounded(sup._step_verdict_gate())
    key = sup._review_state_key()

    sup._record_review_dispatch_key(1, key)
    assert sup._verdict_gate_key_unchanged(1, key)
    out = edges._bounded(sup._step_verdict_gate())

    assert out == ("advance", "paused", "verified_unchanged")
    assert runner.calls["verdict_gate"] == 1


def test_a_gate_judgement_belongs_to_its_approach(tmp_path):
    """The record is per approach: the key judged on approach 1 does not hold
    a gate entry of approach 2."""
    sup, _ = edges._gate_supervisor(tmp_path)
    edges._bounded(sup._step_verdict_gate())
    key = sup._review_state_key()

    assert sup._verdict_gate_key_unchanged(1, key)
    assert not sup._verdict_gate_key_unchanged(2, key)


# ---------------------------------------------------------------------------
# cure-01-05 retry: the key is compared before the budget -- an exhausted
# iteration budget or a passed deadline never turns an equal key into an
# `exhausted` tail
# ---------------------------------------------------------------------------

def _spend_budget(sup, how: str) -> None:
    """Leave no budget: the iteration limit lowered to what is used (the
    `PATCH /config/budget` path, `set_iteration_budget`), lowered below it,
    or the job deadline passed."""
    if how == "deadline_passed":
        sup.deadline = time.monotonic() - 1.0
    else:
        sup.iterations_used = max(sup.iterations_used, 1)
        target = (sup.iterations_used if how == "iterations_to_used"
                  else sup.iterations_used - 1)
        sup.set_iteration_budget(target)
    assert not sup.budget_left()


@pytest.mark.parametrize(
    "how", ["iterations_to_used", "iterations_below_used", "deadline_passed"])
def test_an_equal_gate_key_parks_with_no_budget_left(tmp_path, how):
    """The verifier's probe and its siblings: after the gate judged the key
    and its park was released with nothing changed, the budget is spent
    (iteration limit lowered to the iterations used, lowered below them, or
    the deadline passed). Re-entering the gate on the same HEAD and task
    triples parks `verified_unchanged` -- no gate dispatch, and no
    `exhausted` hold."""
    sup, runner = edges._gate_supervisor(tmp_path)
    _judge_then_release(sup)
    _spend_budget(sup, how)

    out = edges._bounded(sup._step_verdict_gate())

    assert out == ("advance", "paused", "verified_unchanged")
    assert runner.calls["verdict_gate"] == 1, "the gate hat was dispatched"
    assert not sup._budget_exhausted_tail
    status = sup.run.read_status()
    assert status["state"] == "awaiting_input"
    assert status["pauseReason"] == "verified_unchanged"
    assert status.get("exhaustedBudget") is None
    assert status["inputQuestion"]["judge"] == "verdict_gate"


def test_an_approved_gate_key_parks_after_the_deadline_passed(tmp_path):
    """The same rule for a key an APPROVING verdict judged: a passed deadline
    is not a change of the judged state."""
    sup, runner = edges._supervisor(
        tmp_path, [edges._task("001", "completed")],
        {"verdict_gate": [edges._reply(verdict="verified")]},
        peer_verdict_gate=True)
    assert edges._bounded(sup._step_verdict_gate()) == ("done", "succeeded")
    _spend_budget(sup, "deadline_passed")

    out = edges._bounded(sup._step_verdict_gate())

    assert out == ("advance", "paused", "verified_unchanged")
    assert runner.calls["verdict_gate"] == 1


def test_a_changed_gate_key_with_no_budget_left_takes_the_budget_tail(
        tmp_path):
    """The budget still governs a DISPATCH: a changed key with no budget left
    is not parked and not dispatched; it takes the out-of-budget tail."""
    sup, runner = edges._gate_supervisor(tmp_path)
    _judge_then_release(sup)
    sup._workspace_head_sha = lambda: "b" * 40
    _spend_budget(sup, "iterations_to_used")

    out = edges._bounded(sup._step_verdict_gate())

    assert out == ("tail",)
    assert sup._budget_exhausted_tail
    assert runner.calls["verdict_gate"] == 1


def test_an_operator_abort_on_an_equal_gate_key_still_ends_the_run(tmp_path):
    """An abort is not a budget change: on an equal key the gate does not
    park over it, it takes the tail the abort ends the run through."""
    sup, runner = edges._gate_supervisor(tmp_path)
    _judge_then_release(sup)
    sup._abort_reason = "operator abort"

    out = edges._bounded(sup._step_verdict_gate())

    assert out == ("tail",)
    assert runner.calls["verdict_gate"] == 1
    assert sup.run.read_status().get("pauseReason") != "verified_unchanged"


# ---------------------------------------------------------------------------
# cure-01-05 retry: the gate's park HOLDS with no budget left -- the paused
# step that waits on it does not turn it into an `exhausted` tail either
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("how", ["iterations_to_used", "deadline_passed"])
def test_the_gate_park_holds_through_the_paused_step_with_no_budget_left(
        tmp_path, how):
    """The verifier's probe end to end: the gate judged the key, the budget
    is spent, and the park is released with nothing changed -- twice. Each
    pass through `_step_paused` re-enters the gate (never the out-of-budget
    tail), and each gate pass parks `verified_unchanged` again with no
    dispatch and no `exhausted` record."""
    sup, runner = edges._gate_supervisor(tmp_path)
    _judge_then_release(sup)
    _spend_budget(sup, how)

    for _ in range(2):
        held = edges._bounded(sup._step_verdict_gate())
        assert held == ("advance", "paused", "verified_unchanged")
        resumed = edges._bounded(edges._release_with_nothing_changed(sup))
        assert resumed == ("advance", "verdict_gate",
                           "paused_resumed_verdict_gate")
        assert not sup._budget_exhausted_tail

    assert runner.calls["verdict_gate"] == 1, "the gate hat was dispatched"
    status = sup.run.read_status()
    assert status["state"] != "exhausted"
    assert status.get("exhaustedBudget") is None


def test_an_abort_over_the_gate_park_with_no_budget_left_takes_the_tail(
        tmp_path):
    """An operator abort is not a budget change: over the gate's own park
    the paused step still takes the tail the abort ends the run through."""
    sup, runner = edges._gate_supervisor(tmp_path)
    _judge_then_release(sup)
    assert edges._bounded(sup._step_verdict_gate()) == (
        "advance", "paused", "verified_unchanged")
    sup._abort_reason = "operator abort"

    out = edges._bounded(edges._release_with_nothing_changed(sup))

    assert out == ("tail",)
    assert runner.calls["verdict_gate"] == 1


@pytest.mark.parametrize("judge", ["review", "stagnation"])
def test_other_parks_with_no_budget_left_still_take_the_budget_tail(
        tmp_path, judge):
    """The exception is the final peer gate's own park only: review's B.2
    `verified_unchanged` park and an ordinary pause still hold through the
    out-of-budget tail once no budget is left."""
    sup, runner = edges._gate_supervisor(tmp_path)
    if judge == "review":
        sup.run.update_status(
            state="awaiting_input", pauseReason="verified_unchanged",
            inputQuestion={"judge": "review", "kind": "verified_unchanged",
                           "approach": 1})
    else:
        sup.run.update_status(state="paused", pauseReason="stagnation",
                              inputQuestion=None)
    _spend_budget(sup, "iterations_to_used")

    out = edges._bounded(edges._release_with_nothing_changed(sup))

    assert out == ("tail",)
    assert sup._budget_exhausted_tail
    assert runner.calls.get("verdict_gate", 0) == 0


@pytest.mark.parametrize("change", ["iterations_raised", "approaches_raised"])
def test_a_raised_budget_does_not_reopen_a_judged_gate_key(tmp_path, change):
    """Independent of budget changes in the other direction too: an
    iteration limit or an approach allowance RAISED after the gate judged
    the key is not a change of the judged state -- no second dispatch."""
    sup, runner = edges._gate_supervisor(tmp_path)
    _judge_then_release(sup)
    if change == "iterations_raised":
        sup.set_iteration_budget(int(sup.cfg.iterations) + 50)
    else:
        sup.set_max_approaches(int(sup.cfg.max_approaches) + 2)

    out = edges._bounded(sup._step_verdict_gate())

    assert out == ("advance", "paused", "verified_unchanged")
    assert runner.calls["verdict_gate"] == 1, "the gate hat was dispatched"
