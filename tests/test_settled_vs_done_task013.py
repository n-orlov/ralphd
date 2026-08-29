"""Task 013 (PRD requirement I, #98): split `settled` from `done`, and gate
terminal success on steering-ORIGIN TASK state rather than a steering FILE.

Before this task, `transitions.HAT_SCHEMAS["steer"]`'s required `settled`
boolean was validated (schema-checked) and then simply discarded --
`_step_generic_hat` never read `payload.get("settled")` at all. The only
thing anyone could actually observe was a steering FILE's `consumed` marker,
which flips to `applied` the instant an ACTIONABLE-phase iteration succeeds,
whatever it actually did. That is the PRD's own diagnosis of the defect:
"today's `consumed` conflates the two [settled and done] and that conflation
is the whole defect -- steer 003 was `consumed` at iteration 87 with three
quarters of it undone."

This module pins three things:

1. `settled` is now a harness-observable fact, separate from `done`: a
   `steer.settled` event, and a `steerSettled` status.json field, both
   recorded the moment the steer hat's payload validates (`test_steer_
   hats_settled_claim_is_recorded_as_a_harness_observable_fact`).
2. The exit gate (`_step_review`'s VERIFIED check, `_maybe_grace_review`)
   now blocks terminal success while ANY steering-origin task
   (`state.is_steering_origin`) is non-terminal (`state.task_is_terminal`)
   -- independently of whether the message that produced it was ever
   marked `settled` at all (`test_settled_but_not_done_blocks_terminal_
   success`) -- and permits it once every steering-origin task IS terminal
   (`test_all_done_permits_terminal_success`).
3. Both gates ask ONE predicate, `LoopSupervisor._terminal_success_
   blockers()`, which REPLACES the pre-013 mechanism: a `while verified and
   self.run.pending_steering():` loop inside `_step_review` that discarded
   the verdict and dispatched a no-task `worker` iteration in place until
   every steering FILE was consumed, plus a second hand-rolled
   `pending_steering()` test inside `_maybe_grace_review`. The replacement
   defers ONCE through the declared `steering_pending_at_verified` edge and
   lets the ordinary dispatch loop route to the `steer` hat (task 012's
   absolute-top-priority check) -- `test_unsettled_steering_at_verified_
   defers_once_through_the_declared_edge`,
   `test_the_deferred_edge_is_settled_by_the_steer_hat_then_review_
   verifies`, `test_both_blockers_are_named_by_the_one_shared_predicate`,
   `test_the_grace_gate_asks_the_same_predicate`.
4. The at-least-once property of steering consumption a dying iteration
   must never defeat is unaffected by any of this -- pinned again here,
   specifically for the newly-reachable `steer` phase (task 012), mirroring
   `tests/test_steering_consumed_on_success.py`'s own pattern for `worker`
   (`test_a_crash_between_processing_and_marking_still_represents_the_steer`).
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from ralphd.engine import state, transitions
from ralphd.engine.runner import IterationResult
from ralphd.engine.state import RunDir, atomic_write_json

from test_steering_priority_routing_task012 import (
    TASK_1, _ok, _review_verifies, _steer_settles, _supervisor,
)

STEER_EDGES = transitions.successors("steer", vigilant=False)


def _events(root: Path, type_: str) -> list[dict]:
    log = root / "events.jsonl"
    lines = log.read_text().splitlines() if log.exists() else []
    return [ev for ev in (json.loads(x) for x in lines) if ev.get("type") == type_]


def _seed(root: Path, tasks: list[dict]) -> RunDir:
    root.mkdir(parents=True, exist_ok=True)
    run = RunDir(root=root)
    run.update_status(state="running")
    atomic_write_json(run.tasks_file, {"version": 1, "tasks": tasks})
    return run


# ---------------------------------------------------------------------------
# 1. settled-but-not-done: observable, and blocks terminal success
# ---------------------------------------------------------------------------

def test_settled_but_not_done_blocks_terminal_success(tmp_path):
    root = tmp_path / "run"
    _seed(root, [
        {"id": TASK_1, "status": "completed"},
        {"id": "002", "status": "pending", "title": "steer-inserted",
         "origin": "steer"},
    ])
    sup = _supervisor(root, {"review": [_review_verifies]})
    sup._approach = 1

    outcome = asyncio.run(sup._step_review())

    # `settled` (the steer hat processed the message that produced task
    # 002 -- irrelevant here on purpose, no steer iteration even ran) says
    # nothing about `done` (task 002's own status) -- the exit gate looks
    # at DONE, and must defer.
    assert outcome == ("advance", "worker", "steering_pending_at_verified")
    status = sup.run.read_status()
    assert status.get("state") != "succeeded", (
        "a steering-origin task pending must never let VERIFIED go terminal")
    assert status.get("verdict") is None

    # independently observable, not just an internal side effect of the gate
    outstanding = state.steering_origin_outstanding(sup.run.read_tasks())
    assert [t["id"] for t in outstanding] == ["002"]


@pytest.mark.parametrize("terminal_status",
                         ["completed", "validated", "failed", "skipped"])
def test_all_done_permits_terminal_success(tmp_path, terminal_status):
    root = tmp_path / (terminal_status + "-run")
    _seed(root, [
        {"id": TASK_1, "status": "completed"},
        {"id": "002", "status": terminal_status, "title": "steer-inserted",
         "origin": "steer"},
    ])
    sup = _supervisor(root, {"review": [_review_verifies]})
    sup._approach = 1

    outcome = asyncio.run(sup._step_review())

    assert outcome == ("done", "succeeded"), (
        f"a steering-origin task already at terminal status {terminal_status!r} "
        "must not block VERIFIED")
    status = sup.run.read_status()
    assert status.get("state") == "succeeded"
    assert status.get("verdict") == "verified"
    assert state.steering_origin_outstanding(sup.run.read_tasks()) == []


def test_no_steering_origin_tasks_at_all_permits_terminal_success(tmp_path):
    """The ordinary case: a plan the planner alone proposed, no steering
    ever happened. Must behave exactly as it always did."""
    root = tmp_path / "run"
    _seed(root, [{"id": TASK_1, "status": "completed"}])
    sup = _supervisor(root, {"review": [_review_verifies]})
    sup._approach = 1

    outcome = asyncio.run(sup._step_review())

    assert outcome == ("done", "succeeded")
    assert sup.run.read_status().get("state") == "succeeded"


# ---------------------------------------------------------------------------
# 1b. the REPLACEMENT itself: one predicate, one deferral, the steer hat
#     rather than an in-place worker/re-review loop
# ---------------------------------------------------------------------------

def test_unsettled_steering_at_verified_defers_once_through_the_declared_edge(
        tmp_path):
    """The pre-013 mechanism looped IN PLACE: it discarded the VERIFIED
    verdict, dispatched a no-task `worker` iteration from inside
    `_step_review` itself and re-reviewed, over and over, until
    `pending_steering()` came back empty -- the dispatch loop never saw a
    state change at all. Task 013 replaces that with `_terminal_success_
    blockers()` and a single return through the declared
    `steering_pending_at_verified` edge: exactly ONE review iteration runs,
    no worker iteration is dispatched from here, and the run does not go
    terminal."""
    root = tmp_path / "run"
    run = _seed(root, [{"id": TASK_1, "status": "completed"}])
    fname = run.add_steering("hold on, check one more thing", "lastcall")

    sup = _supervisor(root, {"review": [_review_verifies]})
    sup._approach = 1

    outcome = asyncio.run(sup._step_review())

    assert outcome == ("advance", "worker", "steering_pending_at_verified")
    assert sup.runner.calls == {"review": 1}, (
        "the replacement must not dispatch a worker iteration (nor a second "
        f"review) from inside _step_review: {sup.runner.calls!r}")
    assert sup.runner.phases_seen == ["review"]
    status = sup.run.read_status()
    assert status.get("state") != "succeeded"
    assert status.get("verdict") is None
    # untouched, therefore still deliverable: at-least-once is the file's
    # own consume-on-clean-exit ordering, not something this gate does.
    assert sup.run.pending_steering() == [root / "steering" / fname]
    warnings = [ev for ev in _events(root, "log")
                if ev.get("level") == "warning"]
    assert any("deferring" in ev.get("message", "")
               and fname in ev.get("message", "") for ev in warnings), (
        f"the deferral must name the unsettled file: {warnings!r}")


def test_the_deferred_edge_is_settled_by_the_steer_hat_then_review_verifies(
        tmp_path):
    """What the removed in-place loop used to do by hand, now done by the
    ordinary dispatch loop: the deferral lands in `worker`, whose
    absolute-top-priority check (task 012) routes to the `steer` hat, which
    settles the message and consumes the file on its clean exit -- and only
    then does a re-review's VERIFIED verdict make the run terminal."""
    root = tmp_path / "run"
    run = _seed(root, [{"id": TASK_1, "status": "completed"}])
    fname = run.add_steering("hold on, check one more thing", "lastcall")

    sup = _supervisor(root, {"review": [_review_verifies],
                             "steer": [_steer_settles]})
    sup._approach = 1

    assert asyncio.run(sup._step_review()) == (
        "advance", "worker", "steering_pending_at_verified")
    # the declared edge's target, reached the ordinary way -- and it routes
    # to `steer`, not into a worker iteration.
    assert asyncio.run(sup._step_worker()) == (
        "advance", "steer", "steering_pending")
    assert asyncio.run(sup._step_generic_hat("steer", STEER_EDGES)) == (
        "advance", "worker", "steer_settled")
    assert sup.run.pending_steering() == [], (
        "the steer hat's clean exit must consume the message")
    consumed = json.loads((root / "steering" / ".consumed.json").read_text())
    assert fname in consumed
    assert sup._terminal_success_blockers() == []

    assert asyncio.run(sup._step_review()) == ("done", "succeeded")
    status = sup.run.read_status()
    assert status.get("state") == "succeeded"
    assert status.get("verdict") == "verified"
    assert status.get("unconsumedSteering") == []


def test_both_blockers_are_named_by_the_one_shared_predicate(tmp_path):
    """One predicate, two blockers, so the two gate sites cannot drift: an
    unsettled FILE (not `settled`) and a non-terminal steering-origin TASK
    (not `done`) are both named, and both clear independently."""
    root = tmp_path / "run"
    run = _seed(root, [
        {"id": TASK_1, "status": "completed"},
        {"id": "002", "status": "in-progress", "origin": "steer"},
    ])
    fname = run.add_steering("and one more thing", "more")
    sup = _supervisor(root, {})

    blockers = sup._terminal_success_blockers()
    assert len(blockers) == 2, blockers
    assert fname in blockers[0] and "not settled" in blockers[0]
    assert "002" in blockers[1] and "not terminal" in blockers[1]

    # settle the file: only the task half is left
    run.consume_steering([root / "steering" / fname], 1)
    blockers = sup._terminal_success_blockers()
    assert len(blockers) == 1 and "002" in blockers[0]

    # finish the task: nothing blocks terminal success any more
    doc = sup.run.read_tasks()
    for t in doc["tasks"]:
        if t["id"] == "002":
            t["status"] = "validated"
    atomic_write_json(sup.run.tasks_file, doc)
    assert sup._terminal_success_blockers() == []


def test_the_grace_gate_asks_the_same_predicate(tmp_path):
    """`_maybe_grace_review`'s own hand-rolled `pending_steering()` test is
    replaced by the same predicate: a VERIFIED grace review still may not
    make the run terminal-succeeded while an operator message is unsettled,
    and the reason note now says so in the shared vocabulary."""
    root = tmp_path / "run"
    run = _seed(root, [{"id": TASK_1, "status": "completed"}])
    fname = run.add_steering("one last thing", "last")

    sup = _supervisor(root, {"review": [_review_verifies]})
    sup._approach = 1

    assert asyncio.run(sup._maybe_grace_review(1)) is False
    assert sup.run.read_status().get("state") != "succeeded"
    note = sup._terminal_reason_note or ""
    assert "grace" in note and fname in note and "not settled" in note, note


# ---------------------------------------------------------------------------
# 2. `settled` itself is a harness-observable fact, recorded whether true
#    or false, distinct from any task's own `done` status
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("settled_claim,notes", [
    (True, None),
    (False, "the operator's message was ambiguous against the current plan"),
])
def test_steer_hats_settled_claim_is_recorded_as_a_harness_observable_fact(
        tmp_path, settled_claim, notes):
    root = tmp_path / "run"
    root.mkdir()
    run = RunDir(root=root)
    run.update_status(state="running")
    atomic_write_json(run.tasks_file, {"version": 1, "tasks": []})
    run.add_steering("do the thing", "opX")

    payload = {"settled": settled_claim}
    if notes is not None:
        payload["notes"] = notes

    def _steer_handler(sup, prompt=""):
        return _ok(json.dumps(payload))

    sup = _supervisor(root, {"steer": [_steer_handler]})

    outcome = asyncio.run(sup._step_generic_hat("steer", STEER_EDGES))
    assert outcome == ("advance", "worker", "steer_settled")

    settle_events = _events(root, "steer.settled")
    assert len(settle_events) == 1
    assert settle_events[0]["settled"] is settled_claim
    assert settle_events[0].get("notes") == notes

    status = sup.run.read_status()
    recorded = status.get("steerSettled")
    assert recorded is not None, "settled must be observable from status.json too"
    assert recorded["settled"] is settled_claim
    assert recorded.get("notes") == notes
    assert "at" in recorded

    # `settled` says nothing about `done` -- no task exists at all here, so
    # there is nothing yet for the ordinary lifecycle to have judged.
    assert state.steering_origin_outstanding(sup.run.read_tasks()) == []


# ---------------------------------------------------------------------------
# 3. at-least-once: a crash between processing and marking still
#    re-presents the steer to the very next actionable (steer) iteration
# ---------------------------------------------------------------------------

class _DyingThenOk:
    """A minimal scripted runner: the first call dies mid-work (an
    ordinary work error, `fault_class` non-None), the second succeeds with
    a schema-valid `{"settled": true}` payload."""

    def __init__(self):
        self.calls = 0
        self.running = False
        self.prompts: list[str] = []

    def interrupt(self) -> bool:
        return self.running

    async def run(self, prompt, transcript, **kw) -> IterationResult:
        self.calls += 1
        self.prompts.append(prompt)
        self.running = True
        try:
            if self.calls == 1:
                r = IterationResult(exit_code=1)
                r.error_message = "the agent reported an error"
                r.final_text = ""
                r.duration_s = 5.0
                r.usage = {"input": 4, "output": 0, "totalTokens": 4}
                return r
            r = IterationResult(exit_code=0)
            r.final_text = json.dumps({"settled": True})
            r.duration_s = 5.0
            r.usage = {"input": 10, "output": 5, "totalTokens": 15}
            return r
        finally:
            self.running = False


def test_a_crash_between_processing_and_marking_still_represents_the_steer(
        tmp_path):
    root = tmp_path / "run"
    root.mkdir()
    run = RunDir(root=root)
    run.update_status(state="running")
    fname = run.add_steering("stop rewriting the tests; fix the runner instead",
                             "focus")

    sup = _supervisor(root, {})
    sup.runner = _DyingThenOk()  # type: ignore[assignment]

    # attempt 1: dies mid-work -- the note must stay pending, not banked.
    # (`run_iteration` is the public seam; `steer` is not in
    # `INFRA_RETRY_PHASES`, so it is a straight pass-through -- no
    # `_run_iteration_once` call in a test, per the PRD's non-negotiable 5.)
    asyncio.run(sup.run_iteration("steer"))
    assert sup.run.pending_steering() == [root / "steering" / fname], (
        "a dying steer iteration must leave its note pending")
    consumed_path = root / "steering" / ".consumed.json"
    assert (not consumed_path.exists()
           or fname not in json.loads(consumed_path.read_text())), (
        "the note must not be banked as applied when the iteration died")

    # attempt 2: the following actionable (steer) iteration is handed the
    # SAME note again (at-least-once) and, having succeeded cleanly this
    # time, consumes it.
    asyncio.run(sup.run_iteration("steer"))
    assert sup.run.pending_steering() == [], (
        "the following clean steer iteration must consume the note")
    consumed = json.loads(consumed_path.read_text())
    assert fname in consumed
    # both attempts were actually handed the note's body -- it was
    # delivered every time, not just applied once.
    assert all("stop rewriting the tests" in p for p in sup.runner.prompts)
