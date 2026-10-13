"""D.4 -- the steer hat's own subject matter, folded into one module (task
025, requirement D.4). Absorbs, whole, the six remaining `test_*_task*.py`
modules whose subject is the steer hat itself (the seventh, `tests/
test_steer_tool_spool_two_uid_task030.py`, was already folded into
`tests/flows/test_flow_steer.py` by task 021/C.10 -- its docker-tier sibling
below is the one that survived):

* `tests/test_steering_priority_routing_task012.py` (task 012, requirement
  I, #98): unsettled operator steering outranks a pending task and a
  validation-failed retry in `worker`, the declared `worker -> steer` edge
  is taken without ever consulting `engine.router`, several pending steers
  settle in one steer-hat iteration in arrival order, and `ralphctl steer
  --now` forces the next transition to `steer` rather than resuming
  `worker`'s own self-loop. This section's scaffolding (`_ok`,
  `_worker_completes`, `_steer_settles`, `_review_verifies`, `_supervisor`,
  `_run_resumed`, `TASK_1`) is reused, unchanged, by the two sections after
  it, which used to import it from this module by name.
* `tests/test_steer_redelivery_task006.py` (task 006, requirement B.1,
  #122): a steer pass that honestly reports `settled: false` leaves its
  message unconsumed and redelivered, a per-message `steerRedeliveries`
  counter survives a resumed process, and a message that hits the declared
  cap is recorded settled-BY-EXHAUSTION, with a reason, and stays consumed.
* `tests/test_steer_uncharged_task014.py` (task 014, requirements J):
  a clean steer pass gets its own numbered iteration slot but is refunded
  out of `iterationsUsed`/`budget_left()`, a malformed steer payload rides
  the one existing contract ladder (never a second, steer-specific one),
  pre-plan steering is untouched, and the read-only recent-steering echo
  appears in every hat's prompt with no consumption semantics.
* `tests/test_steer_task_update_justification_task017.py` (task 017,
  requirement F.4, #147, cured by task 038): the steer hat's `task-update`
  tool requires a non-empty justification on a `failed -> skipped` move,
  appends it as the authoritative tail of the task's `notes` rather than
  losing it to a same-call `changes["notes"]`, and refuses the move with no
  justification anywhere.
* `tests/test_steer_criteria_survives_worker_pass_task002.py` (task 002,
  following on from task 001, #132): a steer-tool `successCriteria` edit
  that moves the fingerprint forward atomically survives the very next
  `LoopSupervisor` worker pass's `_check_criteria_edits` call instead of
  being restored as tamper.
* `tests/test_steer_tool_spool_two_uid_docker_task030.py` (task 030,
  requirement G.2, #130, docker tier): the real uid boundary the in-process
  sections above cannot exercise -- the steer hat and its hat-tools client
  really run as uid 1000 and really cannot write the root-owned
  `tasks.json`; the spooled request is answered and applied by the
  engine's own process, with the applier's forced fields in place of what
  the hat asked for and an illegal status jump refused; a malformed
  request dropped into the spool is answered `ok: false` without ending
  the run. Its docker/skipif marks are applied per-function (never as a
  module-level `pytestmark`) so the other, in-process sections above stay
  in the `fast`/`smoke` tiers.
"""

from __future__ import annotations

import asyncio
import errno
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from ralphd.engine import findings as findings_mod
from ralphd.engine import hat_tools, state
from ralphd.engine import router as router_module
from ralphd.engine import scheduler, transitions
from ralphd.engine.config import JobConfig
from ralphd.engine.contract_enforcement import EVENT_AGENT_FAULT, EVENT_VIOLATION
from ralphd.engine.loop import (
    STEERING_ECHO_HEADING, STEERING_ECHO_MAX_BODY_CHARS,
    STEERING_ECHO_MAX_MESSAGES, LoopSupervisor,
)
from ralphd.engine.runner import IterationResult
from ralphd.engine.state import RunDir, atomic_write_json, read_events
from tests.conftest import scripted_supervisor, wait_hold_entered

from test_docker_sibling_e2e import (  # noqa: F401  (fixtures used by name)
    REAL_DOCKER,
    Ctl,
    _container_running,
    _unique_run_id,
    _wait_status,
    gateway_ip,
    pytestmark as _docker_pytestmark,
    test_image,
    work_root,
)

TASK_1 = "001"


def _docker_tier(fn):
    """Apply the docker/skipif marks this section's tests need, per-function
    -- never as this module's own `pytestmark`, which would misclassify
    every other (in-process, fast-tier) test above as docker-only."""
    for mark in _docker_pytestmark:
        fn = mark(fn)
    return fn


# ===========================================================================
# 1. `tests/test_steering_priority_routing_task012.py` -- steering outranks
#    everything else in `worker`, without ever consulting `engine.router`.
# ===========================================================================


def _ok(text: str) -> IterationResult:
    r = IterationResult(exit_code=0)
    r.final_text = text
    r.duration_s = 5.0
    r.usage = {"input": 10, "output": 5, "totalTokens": 15}
    return r


def _worker_completes(task_id: str):
    def handler(sup, prompt=""):
        return _ok(json.dumps({"verdict": "complete", "task_id": task_id}))
    return handler


def _steer_settles(sup, prompt=""):
    return _ok(json.dumps({"settled": True}))


def _review_verifies(sup, prompt=""):
    return _ok(json.dumps({"verdict": "verified"}))


_supervisor = scripted_supervisor(run_id="v014-steer-priority", iterations=15,
                                  max_approaches=1, vigilant=False,
                                  on_complete="exit",
                                  infra_outage_budget_s=1000.0)


def _run_resumed(root: Path, handlers: dict, tasks: list[dict],
                 steering: list[tuple[str, str]] = (),
                 **cfg_kw) -> tuple[LoopSupervisor, str]:
    """Same shape as `test_doomed_plan_fault.py`'s helper, plus an optional
    `steering` list of `(message, name)` pairs added to the run BEFORE
    `_run_job_core()` is ever invoked -- i.e. steering that is already
    unsettled at the very first pass through `_step_worker`."""
    RunDir(root=root).update_status(state="starting")
    sup = _supervisor(root, handlers, **cfg_kw)
    atomic_write_json(sup.run.tasks_file, {"version": 1, "tasks": tasks})
    for message, name in steering:
        sup.run.add_steering(message, name)
    sup.iterations_used = 1
    return sup, asyncio.run(sup._run_job_core())


def test_steering_outranks_a_pending_task(tmp_path, monkeypatch):
    calls = []
    real_select = scheduler.select_next_task

    def counting_select(tasks):
        calls.append(list(tasks))
        return real_select(tasks)

    monkeypatch.setattr(scheduler, "select_next_task", counting_select)

    root = tmp_path / "run"
    sup, final_state = _run_resumed(
        root,
        {"steer": [_steer_settles], "worker": [_worker_completes(TASK_1)],
         "review": [_review_verifies]},
        tasks=[{"id": TASK_1, "status": "pending", "title": "do stuff"}],
        steering=[("do X before anything else", "opX")],
    )

    agent = sup.runner
    assert agent.phases_seen[0] == "steer", (
        f"steering must be handled before the pending task, got "
        f"{agent.phases_seen!r}")
    # the scheduler IS consulted -- but only once, for the pass AFTER
    # steering settled (the ordinary pick that hands task 001 to worker).
    # It must never have been consulted for a pass that ran BEFORE `steer`.
    assert len(calls) == 1, (
        "scheduler.select_next_task must be consulted exactly once here -- "
        f"for the post-steer worker pick, not before it: {calls!r}")
    assert final_state == "succeeded", sup.run.read_status()
    assert agent.calls == {"steer": 1, "worker": 1, "review": 1}


def test_steering_outranks_a_validation_failed_retry(tmp_path, monkeypatch):
    calls = []
    real_select = scheduler.select_next_task

    def counting_select(tasks):
        calls.append(list(tasks))
        return real_select(tasks)

    monkeypatch.setattr(scheduler, "select_next_task", counting_select)

    root = tmp_path / "run"
    sup, final_state = _run_resumed(
        root,
        {"steer": [_steer_settles], "worker": [_worker_completes(TASK_1)],
         "review": [_review_verifies]},
        tasks=[{"id": TASK_1, "status": "validation-failed",
               "title": "retry me"}],
        steering=[("hold off on that retry", "opRetry")],
    )

    agent = sup.runner
    assert agent.phases_seen[0] == "steer", (
        "steering must be handled before a validation-failed retry, got "
        f"{agent.phases_seen!r}")
    # same shape as the pending-task case above: consulted once, only for
    # the post-steer pick -- never for a pass that ran before `steer`.
    assert len(calls) == 1, (
        "scheduler.select_next_task must be consulted exactly once here -- "
        f"for the post-steer retry pick, not before it: {calls!r}")
    assert final_state == "succeeded", sup.run.read_status()


def test_worker_takes_the_declared_edge_without_the_router(tmp_path):
    router_calls = []
    orig_decide = router_module.decide

    async def spying_decide(*args, **kwargs):
        router_calls.append((args, kwargs))
        return await orig_decide(*args, **kwargs)

    root = tmp_path / "run"
    RunDir(root=root).update_status(state="starting")
    sup = _supervisor(root, {"steer": [_steer_settles]})
    atomic_write_json(sup.run.tasks_file, {"version": 1, "tasks": []})
    sup.run.add_steering("just this one thing", "opOnly")
    sup.iterations_used = 1

    router_module.decide = spying_decide
    try:
        outcome = asyncio.run(sup._step_worker())
    finally:
        router_module.decide = orig_decide

    assert outcome == ("advance", "steer", "steering_pending")
    assert transitions.is_legal("worker", "steer", vigilant=False)
    assert router_calls == [], (
        "the worker->steer edge is a deterministic, hand-written guard -- "
        "engine.router.decide must never be called to resolve it")


def test_all_pending_steers_are_settled_in_one_iteration_in_arrival_order(
        tmp_path):
    """Driven at the `_step_worker`/`_step_generic_hat` level directly (not
    a whole `_run_job_core()` run) -- an empty task list would otherwise
    force this test to also script a `worker` handler for the run to make
    any progress at all, which is not what this test is about.
    """
    root = tmp_path / "run"
    RunDir(root=root).update_status(state="starting")
    sup = _supervisor(root, {"steer": [_steer_settles]})
    atomic_write_json(sup.run.tasks_file, {"version": 1, "tasks": []})
    for message, name in [("first thing", "alpha"), ("second thing", "beta"),
                          ("third thing", "gamma")]:
        sup.run.add_steering(message, name)
    sup.iterations_used = 1

    step_outcome = asyncio.run(sup._step_worker())
    assert step_outcome == ("advance", "steer", "steering_pending")

    edges = transitions.successors("steer", vigilant=False)
    hat_outcome = asyncio.run(sup._step_generic_hat("steer", edges))
    assert hat_outcome == ("advance", "worker", "steer_settled")

    agent = sup.runner
    assert agent.calls["steer"] == 1, (
        "three pending steers must be settled by exactly ONE steer-hat "
        "iteration, not one apiece")
    steer_prompt = agent.prompts[0]
    # arrival order: each body appears, and in the order they arrived --
    # STEERING_ACTIONABLE_PHASES now includes "steer" so build_prompt hands
    # it the actual bodies, not just the filenames.
    positions = [steer_prompt.index(body) for body in
                ("first thing", "second thing", "third thing")]
    assert positions == sorted(positions), (
        "the three steering bodies must appear in arrival order in the "
        "steer hat's own prompt")
    assert sup.run.pending_steering() == [], "all three must end up settled"


def test_steer_now_forces_the_next_transition_to_steer_not_worker(tmp_path):
    """Mirrors `POST /interrupt`'s own sequence (`api.py`): the message is
    enqueued via `RunDir.add_steering` first, THEN the in-flight iteration
    is interrupted (`LoopSupervisor.interrupt()`, the real method, not a
    hand-rolled `IterationResult.interrupted=True` stand-in) -- proving the
    forced target is `steer`, where the pre-task-012 engine would have
    simply resumed `worker`'s own self-loop with nothing to distinguish the
    interrupted pass from an ordinary one.
    """
    delivered_box: list[bool] = []

    def _interrupted_by_now(sup, prompt=""):
        sup.run.add_steering("stop, focus on this instead", "now-note")
        delivered_box.append(sup.interrupt())
        r = IterationResult(exit_code=-2)
        r.final_text = ""
        r.duration_s = 1.0
        r.interrupted = True
        r.usage = {"input": 1, "output": 0, "totalTokens": 1}
        return r

    root = tmp_path / "run"
    sup, final_state = _run_resumed(
        root,
        {"worker": [_interrupted_by_now, _worker_completes(TASK_1)],
         "steer": [_steer_settles], "review": [_review_verifies]},
        tasks=[{"id": TASK_1, "status": "pending", "title": "do stuff"}],
    )

    agent = sup.runner
    assert delivered_box == [True], "the interrupt() call must be delivered"
    # the interrupted worker pass is immediately followed by `steer`, never
    # by another bare `worker` self-loop pass first.
    worker_idx = agent.phases_seen.index("worker")
    assert agent.phases_seen[worker_idx + 1] == "steer", (
        f"an interrupted worker pass must be followed by steer, got "
        f"{agent.phases_seen!r}")
    assert agent.phases_seen == ["worker", "steer", "worker", "review"]
    assert final_state == "succeeded", sup.run.read_status()


# ===========================================================================
# 2. `tests/test_steer_redelivery_task006.py` -- an honest `settled: false`
#    redelivers, bounded, instead of being silently consumed anyway.
# ===========================================================================

STEER_EDGES = transitions.successors("steer", vigilant=False)


def _steer_unsettled(notes: str = "ran out of iteration time"):
    def handler(sup, prompt=""):
        return _ok(json.dumps({"settled": False, "notes": notes}))
    return handler


def _entry(sup: LoopSupervisor, file_name: str) -> dict:
    entries = {e["file"]: e for e in sup.run.steering_ledger()}
    assert file_name in entries, f"{file_name} missing from the ledger"
    return entries[file_name]


def _redeliveries(sup: LoopSupervisor) -> dict:
    return dict(sup.run.read_status().get("steerRedeliveries") or {})


def test_a_settled_false_steer_leaves_the_message_unconsumed_and_outstanding(
        tmp_path):
    root = tmp_path / "run"
    sup = _supervisor(root, {"steer": [_steer_unsettled("half done")]})
    name = sup.run.add_steering("do the three-part thing", "op-three-part")

    outcome = asyncio.run(sup._step_generic_hat("steer", STEER_EDGES))

    # the (unconditional, single-edge) state transition is unchanged --
    # this task never touches WHICH state comes next, only whether the
    # message the hat was handed is still deliverable afterward.
    assert outcome == ("advance", "worker", "steer_settled")

    # Task 014 (D.4, #184): an honest settled: false is counted right
    # away, but withheld -- redelivery is deferred until one non-steer
    # iteration has completed, so it must NOT be handed back out yet.
    assert sup.run.pending_steering() == [], (
        "redelivery must be withheld until a non-steer iteration completes")
    assert _redeliveries(sup) == {name: 1}

    # Once that non-steer iteration completes (simulated directly here --
    # the real path is `_run_iteration_once`'s own tail, task 014), the
    # message becomes genuinely deliverable again, and everything below
    # holds exactly as it did before task 014.
    sup._flush_deferred_steering_redelivery()
    assert [p.name for p in sup.run.pending_steering()] == [name], (
        "an honest settled: false must leave the note genuinely pending")
    entry = _entry(sup, name)
    assert entry["consumed"] is None, "the consumption stamp must be cleared"
    # Task 007 (#122) widens the vocabulary this test pins: a message that
    # HAS been delivered and judged `settled: false` -- distinct from one no
    # iteration has ever touched -- now reads `outstanding`, not the bare
    # `pending` this test asserted before task 007 gave that case its own
    # word (`state.STEERING_OUTSTANDING`, `state.steering_entries`).
    assert entry["state"] == state.STEERING_OUTSTANDING
    assert entry["settled"] is False
    assert entry["settledByExhaustion"] is False
    assert _redeliveries(sup) == {name: 1}


def test_a_settled_true_steer_still_consumes_exactly_once(tmp_path):
    """The non-change: this task must not touch the settled: true path."""
    root = tmp_path / "run"
    sup = _supervisor(root, {"steer": [_steer_settles]})
    name = sup.run.add_steering("a single clear ask", "op-clear")

    asyncio.run(sup._step_generic_hat("steer", STEER_EDGES))

    assert sup.run.pending_steering() == []
    entry = _entry(sup, name)
    assert entry["consumed"], "settled: true consumes on the first pass"
    assert entry["state"] == state.STEERING_APPLIED
    assert entry["settled"] is True
    assert _redeliveries(sup) == {}, (
        "settled: true never touches the redelivery counter at all")


def test_the_redelivery_counter_increments_and_is_read_back_on_resume(
        tmp_path):
    root = tmp_path / "run"
    sup1 = _supervisor(root, {"steer": [_steer_unsettled()]},
                       steer_redelivery_max=5)
    name = sup1.run.add_steering("still not done", "op-slow")

    asyncio.run(sup1._step_generic_hat("steer", STEER_EDGES))
    assert _redeliveries(sup1) == {name: 1}
    assert sup1.run.pending_steering() == [], (
        "withheld immediately after the pass that could not settle it")

    # A resumed process is a FRESH LoopSupervisor over the same run root --
    # nothing in-memory on sup1 may be what makes this work, and the
    # deferral itself (task 014, D.4) must survive the resume exactly like
    # the counter it rides beside: still withheld, not yet pending.
    sup2 = _supervisor(root, {"steer": [_steer_unsettled()]},
                       steer_redelivery_max=5)
    assert _redeliveries(sup2) == {name: 1}, (
        "the counter must be read back off status.json, not re-derived")
    assert sup2.run.pending_steering() == []

    # Only once a non-steer iteration completes IN THE RESUMED PROCESS is
    # it deliverable again.
    sup2._flush_deferred_steering_redelivery()
    assert [p.name for p in sup2.run.pending_steering()] == [name]

    asyncio.run(sup2._step_generic_hat("steer", STEER_EDGES))
    assert _redeliveries(sup2) == {name: 2}


def test_two_messages_are_redelivered_and_counted_independently(tmp_path):
    root = tmp_path / "run"
    sup = _supervisor(root, {"steer": [_steer_unsettled("both pending")]})
    one = sup.run.add_steering("first ask", "op-one")
    two = sup.run.add_steering("second ask", "op-two")

    asyncio.run(sup._step_generic_hat("steer", STEER_EDGES))
    assert sup.run.pending_steering() == [], (
        "both must stay withheld until a non-steer iteration completes")
    sup._flush_deferred_steering_redelivery()

    assert sorted(p.name for p in sup.run.pending_steering()) == sorted(
        [one, two])
    assert _redeliveries(sup) == {one: 1, two: 1}


def test_the_redelivery_cap_marks_settled_by_exhaustion_with_a_reason(
        tmp_path):
    root = tmp_path / "run"
    sup = _supervisor(root, {"steer": [_steer_unsettled("still stuck")]},
                      steer_redelivery_max=2)
    name = sup.run.add_steering("a message nobody ever settles", "op-stuck")

    # Pass 1: used=0 < cap(2) -> redelivered (redeliveries -> 1), but
    # withheld (task 014, D.4) until a non-steer iteration completes.
    asyncio.run(sup._step_generic_hat("steer", STEER_EDGES))
    assert sup.run.pending_steering() == []
    assert _redeliveries(sup) == {name: 1}
    assert _entry(sup, name)["settledByExhaustion"] is False
    sup._flush_deferred_steering_redelivery()
    assert [p.name for p in sup.run.pending_steering()] == [name]

    # Pass 2: used=1 < cap(2) -> redelivered again (redeliveries -> 2),
    # withheld the same way.
    asyncio.run(sup._step_generic_hat("steer", STEER_EDGES))
    assert sup.run.pending_steering() == []
    assert _redeliveries(sup) == {name: 2}
    assert _entry(sup, name)["settledByExhaustion"] is False
    sup._flush_deferred_steering_redelivery()
    assert [p.name for p in sup.run.pending_steering()] == [name]

    # Pass 3: used=2, no longer < cap(2) -> settled BY EXHAUSTION, stays
    # consumed for good, never handed back out again -- and never queued
    # for deferral either, since it is never redelivered at all.
    asyncio.run(sup._step_generic_hat("steer", STEER_EDGES))
    assert sup.run.pending_steering() == [], (
        "an exhausted message must stop being redelivered")
    entry = _entry(sup, name)
    assert entry["settledByExhaustion"] is True
    assert entry["exhaustionReason"], "the reason must be recorded"
    assert name in entry["exhaustionReason"] or "cap" in entry["exhaustionReason"]
    assert entry["settled"] is False, (
        "exhaustion is the harness giving up, not the hat claiming settled")
    assert entry["application"] == state.STEER_APPLICATION_APPLIED
    assert entry["state"] == state.STEERING_APPLIED
    # the counter itself is left as-is at the cap, not incremented past it
    assert _redeliveries(sup) == {name: 2}

    # A further pass, were one to happen, must not resurrect it: the file
    # stays consumed and no further redelivery event fires.
    log = [json.loads(ln) for ln in
          (root / "events.jsonl").read_text().splitlines()]
    exhausted = [e for e in log if e.get("type") == "steering.settled_by_exhaustion"]
    assert [e["file"] for e in exhausted] == [name]


def test_the_exhaustion_reason_names_the_declared_cap():
    """A plain-value assertion, independent of run-dir scaffolding: the
    reason text this task's own code produces must state the actual cap
    value, not a vague "too many times"."""
    import inspect

    from ralphd.engine.loop import LoopSupervisor as LS
    src = inspect.getsource(LS._handle_unsettled_steering)
    assert "steer_redelivery_max" in src
    assert "settled_by_exhaustion" in src or "settled-by-exhaustion" in src


def test_a_never_touched_message_reports_no_exhaustion(tmp_path):
    root = tmp_path / "run"
    sup = _supervisor(root, {"steer": []})
    name = sup.run.add_steering("nothing has looked at this yet", "op-fresh")

    entry = _entry(sup, name)
    assert entry["settledByExhaustion"] is False
    assert entry["exhaustionReason"] is None


def test_an_unsettled_message_is_withheld_until_a_non_steer_iteration_completes(
        tmp_path):
    """Task 014 (PRD requirement D.4, #184): the defect this task fixes,
    demonstrated end to end. Before this task, `_handle_unsettled_steering`
    unconsumed the message on the spot, so `worker`'s absolute-top-priority
    guard (`if self.run.pending_steering(): -> steer`, task 012) forced the
    very next pass straight back into `steer` -- turning one operator
    message no hat can settle into `1 + steer_redelivery_max` consecutive
    `steer` iterations with nothing else ever running in between. This test
    drives the real drain path (`_run_iteration_once`'s own tail, exercised
    here via `run_iteration("worker", ...)` directly, exactly the way
    other modules already call `run_iteration` without going through
    `_step_worker`'s own scheduling) and shows the message stays withheld
    across as many CONSECUTIVE steer passes as are attempted -- well past
    the declared cap -- and is handed back out only once a genuine
    non-steer iteration has run to completion.
    """
    root = tmp_path / "run"
    sup = _supervisor(
        root,
        {"steer": [_steer_unsettled("still working on it")],
         "worker": [lambda sup, prompt="": _ok("looked around")]},
        steer_redelivery_max=5)
    atomic_write_json(sup.run.tasks_file, {"version": 1, "tasks": []})
    name = sup.run.add_steering("a request nobody ever settles", "op-loop")

    # Pass 1: delivered, counted, withheld.
    asyncio.run(sup._step_generic_hat("steer", STEER_EDGES))
    assert _redeliveries(sup) == {name: 1}
    assert sup.run.pending_steering() == []

    # A second, third, fourth... consecutive `steer` pass, with no
    # non-steer iteration ever running in between, must never see it
    # pending again -- the counter must not move and no further
    # redelivery/exhaustion event fires, however many times this is
    # repeated, well past the declared cap of 5.
    for _ in range(8):
        asyncio.run(sup._step_generic_hat("steer", STEER_EDGES))
        assert sup.run.pending_steering() == []
        assert _redeliveries(sup) == {name: 1}, (
            "a steer pass with nothing pending must never touch the "
            "counter again -- this is the 'no longer burns consecutive "
            "iterations back-to-back' half of D.4")
    assert _entry(sup, name)["settledByExhaustion"] is False, (
        "never redelivered past pass 1, so the cap was never actually "
        "reached -- exhaustion must not fire from mere repetition")

    # One real non-steer iteration -- run through the actual production
    # code path this task changed, `_run_iteration_once`'s own tail --
    # completes...
    asyncio.run(sup.run_iteration("worker"))
    # ...and only NOW is the message deliverable again.
    assert [p.name for p in sup.run.pending_steering()] == [name]

    # It is redelivered on the very next steer pass, exactly as before
    # this task -- only the TIMING moved, never the eventual redelivery.
    asyncio.run(sup._step_generic_hat("steer", STEER_EDGES))
    assert _redeliveries(sup) == {name: 2}
    assert sup.run.pending_steering() == []


def test_a_crash_between_the_counter_persist_and_the_unconsume_never_exceeds_the_cap(
        tmp_path):
    """PRD requirement D.5 (#184): `_handle_unsettled_steering` writes
    `steerRedeliveries` and `STEER_DEFERRED_REDELIVERY_STATUS_KEY` in the
    ONE atomic `update_status` call at the tail of that method -- strictly
    BEFORE `_flush_deferred_steering_redelivery` (called from a later,
    separate, non-steer iteration) ever performs the actual
    `unconsume_steering` write. Before task 014 (whose restructuring this
    test pins), `_handle_unsettled_steering` called `unconsume_steering`
    immediately, per name, INSIDE its own loop, and only persisted the
    counter in a single `update_status` call AFTER that whole loop had
    finished -- so a crash landing between those two writes left the
    message visibly redelivered (pending again) while the counter on disk
    still read the OLD value, letting the message exceed its cap across
    enough crash/resume cycles.

    This test kills the engine -- a brand-new `LoopSupervisor` over the
    same run root, carrying no in-memory state, the same resume double
    every other test in this module already uses -- exactly inside that
    window: right after `_handle_unsettled_steering`'s counter/deferred
    write has landed on disk, but before `_flush_deferred_steering_
    redelivery` has ever run for this message. The cap must still hold
    once the resumed process keeps going.
    """
    root = tmp_path / "run"
    sup1 = _supervisor(root, {"steer": [_steer_unsettled("still not there")]},
                       steer_redelivery_max=1)
    name = sup1.run.add_steering("a message that never settles", "op-crash")

    # Pass 1: counted (used=0 < cap=1) and persisted; the actual unconsume
    # is deferred, never applied yet.
    asyncio.run(sup1._step_generic_hat("steer", STEER_EDGES))
    assert _redeliveries(sup1) == {name: 1}
    assert sup1.run.pending_steering() == []
    status_before_crash = sup1.run.read_status()
    assert status_before_crash[
        LoopSupervisor.STEER_DEFERRED_REDELIVERY_STATUS_KEY] == [name], (
        "the message is queued for redelivery but not yet unconsumed -- "
        "this IS the crash window D.5 closes")

    # "Kill the engine" exactly inside that window: nothing sup1 holds in
    # process memory survives -- only what `update_status` already wrote
    # to status.json. A fresh supervisor over the same root is the resume.
    sup2 = _supervisor(root, {"steer": [_steer_unsettled("still not there")]},
                       steer_redelivery_max=1)
    assert _redeliveries(sup2) == {name: 1}, (
        "the counter increment from pass 1 was already durable BEFORE the "
        "crash -- the resumed process must read it back exactly as sup1 "
        "left it, never reset to 0")
    assert sup2.run.pending_steering() == [], (
        "the actual unconsume never happened before the crash, so the "
        "resumed process must not see the message pending either")

    # Resume normally: one non-steer iteration flushes the deferral...
    sup2._flush_deferred_steering_redelivery()
    assert [p.name for p in sup2.run.pending_steering()] == [name]

    # ...and the message reaches `steer` again -- but `used` is already at
    # the cap (1), so this pass must land on exhaustion, never a second
    # redelivery that would push the counter to 2 and exceed the declared
    # cap because of the earlier crash.
    asyncio.run(sup2._step_generic_hat("steer", STEER_EDGES))
    assert _redeliveries(sup2) == {name: 1}, (
        "the cap (steer_redelivery_max=1) still holds after the resume -- "
        "the crash inside the window must never let this exceed 1")
    entry = _entry(sup2, name)
    assert entry["settledByExhaustion"] is True
    assert sup2.run.pending_steering() == []


def test_a_deferred_but_not_yet_flushed_redelivery_still_blocks_the_terminal_gate(
        tmp_path):
    """PRD requirement D.6 (#184): D.4 changes WHEN a withheld message is
    redelivered, never whether an outstanding message blocks the run's own
    ending. Right after `_handle_unsettled_steering` counts an honest
    `settled: false` but before `_flush_deferred_steering_redelivery` ever
    runs (the exact window `STEER_DEFERRED_REDELIVERY_STATUS_KEY`'s queue
    exists for, task 014/D.4), `steering/.consumed.json` still lists the
    file consumed, so a bare `pending_steering()` read is blind to it.
    Without this task, `_terminal_success_blockers()` -- the ONE predicate
    both exit-gate sites consult (task 013/I) -- would read empty in that
    exact window, letting a VERIFIED verdict make the run terminal-
    succeeded with the message only deferred, never actually settled.
    """
    root = tmp_path / "run"
    sup = _supervisor(root, {"steer": [_steer_unsettled("still working on it")]})
    atomic_write_json(sup.run.tasks_file, {"version": 1, "tasks": []})

    # Sanity: nothing outstanding before any steering message ever lands.
    assert sup._terminal_success_blockers() == []

    name = sup.run.add_steering("hold on, one more thing", "op-hold")
    asyncio.run(sup._step_generic_hat("steer", STEER_EDGES))

    # In this exact window -- counted, queued, not yet flushed -- a bare
    # `pending_steering()` read is empty (task 014's own withholding), yet
    # the message is genuinely still outstanding: it must still block.
    assert sup.run.pending_steering() == []
    assert sup.run.read_status()[
        LoopSupervisor.STEER_DEFERRED_REDELIVERY_STATUS_KEY] == [name]
    blockers = sup._terminal_success_blockers()
    assert len(blockers) == 1, blockers
    assert name in blockers[0]
    assert "not settled" in blockers[0]

    # Once the non-steer iteration this window is waiting on completes and
    # drains the queue, the message is genuinely pending again -- and the
    # SAME predicate must still name it, for the ordinary reason
    # (`pending_steering()` now sees it directly).
    sup._flush_deferred_steering_redelivery()
    assert [p.name for p in sup.run.pending_steering()] == [name]
    blockers = sup._terminal_success_blockers()
    assert len(blockers) == 1, blockers
    assert name in blockers[0]

    # And once the steer hat actually settles it, the gate clears -- a
    # fresh `LoopSupervisor` over the same root, the same resume double
    # every other test in this module already uses.
    sup2 = _supervisor(root, {"steer": [_steer_settles]})
    asyncio.run(sup2._step_generic_hat("steer", STEER_EDGES))
    assert sup2.run.pending_steering() == []
    assert sup2._terminal_success_blockers() == []


# ===========================================================================
# 3. `tests/test_steer_uncharged_task014.py` -- the steer hat's own
#    iteration is refunded, rides the one existing contract ladder, leaves
#    pre-plan steering alone, and the read-only echo reaches every hat.
# ===========================================================================


def _events(root: Path, type_: str) -> list[dict]:
    log = root / "events.jsonl"
    lines = log.read_text().splitlines() if log.exists() else []
    return [ev for ev in (json.loads(x) for x in lines) if ev.get("type") == type_]


def _garbage(sup, prompt=""):
    """A steer attempt that satisfies no schema at all -- not even valid
    JSON -- the plainest "malformed" case `contract_enforcement.
    parse_response` names."""
    return _ok("this is not JSON, and steer requires a `settled` boolean")


def test_steer_hat_state_is_refunded_like_the_other_kinds():
    assert LoopSupervisor.REFUND_KINDS == ("infra", "steer", "adjudication")


def test_a_clean_steer_iteration_gets_a_slot_but_is_refunded(tmp_path):
    root = tmp_path / "run"
    sup, final_state = _run_resumed(
        root,
        {"steer": [_steer_settles], "worker": [_worker_completes(TASK_1)],
         "review": [_review_verifies]},
        tasks=[{"id": TASK_1, "status": "pending", "title": "do stuff"}],
        steering=[("do X before anything else", "opX")],
    )
    assert final_state == "succeeded"

    # raw attempts: seeded at 1 (planning), then steer, worker, review = 4
    assert sup.iterations_used == 4
    # exactly one of those four was the steer hat, and it is refunded
    assert sup._steer_refunded == 1
    assert sup._infra_refunded == 0 and sup._adjudication_refunded == 0

    status = json.loads(sup.run.status_file.read_text())
    assert status["iterationsRefunded"] == {"infra": 0, "steer": 1, "adjudication": 0}
    # the published, budget-relevant figure excludes the steer slot entirely
    assert status["iterationsUsed"] == 3 == sup.iterations_used_charged
    # ... yet the steer iteration still has its own real, numbered slot --
    # tooling that lists iterations/NNNN must still find it there.
    assert sorted(p.name for p in (root / "iterations").iterdir()) == \
        ["0002", "0003", "0004"]


def test_a_steer_iteration_never_moves_budget_left(tmp_path):
    """The literal comparison the PRD draws: a job with a 4-iteration
    budget that needs one planning-equivalent seed iteration plus a
    worker and a review (3 charged) to finish must still finish -- one
    operator message and its steer pass along the way must not eat any of
    them. v0.36 requirement B holds a verdict on the last charged iteration
    `exhausted`, so the budget is one past the 3 the run needs; a charged
    steer pass would make the review that last iteration and hold the run,
    so `succeeded` still discriminates."""
    root = tmp_path / "run"
    sup, final_state = _run_resumed(
        root,
        {"steer": [_steer_settles], "worker": [_worker_completes(TASK_1)],
         "review": [_review_verifies]},
        tasks=[{"id": TASK_1, "status": "pending", "title": "do stuff"}],
        steering=[("hurry up", "opHurry")],
        iterations=4,
    )
    assert final_state == "succeeded"
    status = json.loads(sup.run.status_file.read_text())
    assert status["iterationsUsed"] == 3
    assert sup.iterations_used == 4, "the steer pass still got its own slot"


def test_repeated_steer_passes_are_each_individually_refunded(tmp_path):
    """Three separate operator messages that each land while `worker` is
    in flight (three separate `worker -> steer -> worker` round trips)
    refund THREE times, one per steer pass -- not once for "the steer
    phase" as a whole."""
    root = tmp_path / "run"
    RunDir(root=root).update_status(state="starting")
    sup = _supervisor(root, {"steer": [_steer_settles, _steer_settles,
                                        _steer_settles]})
    atomic_write_json(sup.run.tasks_file, {"version": 1, "tasks": []})
    sup.iterations_used = 1

    edges = STEER_EDGES
    for name in ("alpha", "beta", "gamma"):
        sup.run.add_steering(f"message {name}", name)
        outcome = asyncio.run(sup._step_generic_hat("steer", edges))
        assert outcome == ("advance", "worker", "steer_settled")

    assert sup._steer_refunded == 3
    assert sup.iterations_used == 4  # seeded 1 + three steer passes
    assert sup.iterations_used_charged == 1


def test_a_malformed_steer_attempt_is_repaired_through_ask_contract_fix(
        tmp_path):
    """The first attempt is garbage; `enforce_contract`'s own re-prompt
    loop (never a bespoke steer-specific one) repairs it through the
    UNCHARGED `_ask_contract_fix` channel, and the steer hat still only
    costs ONE (refunded) iteration overall."""
    root = tmp_path / "run"
    RunDir(root=root).update_status(state="starting")
    sup = _supervisor(root, {"steer": [_garbage, _steer_settles]},
                      contract_retry_max=1)
    atomic_write_json(sup.run.tasks_file, {"version": 1, "tasks": []})
    sup.run.add_steering("please settle this", "opFix")
    sup.iterations_used = 1

    outcome = asyncio.run(sup._step_generic_hat("steer", STEER_EDGES))
    assert outcome == ("advance", "worker", "steer_settled")

    violations = _events(root, EVENT_VIOLATION)
    assert len(violations) == 1 and violations[0]["hat"] == "steer"
    assert _events(root, EVENT_AGENT_FAULT) == [], (
        "the ladder recovered on the re-ask -- this must not also be "
        "recorded as an exhausted (agent-fault) ladder")

    # exactly one steer ITERATION (not one per ladder attempt) was charged
    # and then refunded -- the repair rode inside the same attempt.
    assert sup.iterations_used == 2
    assert sup._steer_refunded == 1
    agent = sup.runner
    assert agent.calls["steer"] == 2, (
        "one run_iteration() call plus one _ask_contract_fix repair, both "
        "counted the same way this stub counts every runner.run() call")


def test_an_exhausted_steer_ladder_scores_one_agent_fault_and_retries_in_place(
        tmp_path):
    """`contract_retry_max=1` means 2 total attempts per ladder call. Both
    fail here, so the ladder is exhausted and `EVENT_AGENT_FAULT` fires --
    exactly the outcome `contract_enforcement.enforce_contract` documents
    for ANY hat, never a special steer-only path. Budget still remains
    (steer's own attempts are refunded), so `_step_generic_hat` retries
    the SAME steer call in place, and the SECOND full pass succeeds."""
    root = tmp_path / "run"
    RunDir(root=root).update_status(state="starting")
    sup = _supervisor(
        root, {"steer": [_garbage, _garbage, _steer_settles]},
        contract_retry_max=1)
    atomic_write_json(sup.run.tasks_file, {"version": 1, "tasks": []})
    sup.run.add_steering("please settle this too", "opRetry")
    sup.iterations_used = 1

    outcome = asyncio.run(sup._step_generic_hat("steer", STEER_EDGES))
    assert outcome == ("advance", "worker", "steer_settled")

    violations = _events(root, EVENT_VIOLATION)
    assert len(violations) == 2 and {v["hat"] for v in violations} == {"steer"}
    agent_faults = _events(root, EVENT_AGENT_FAULT)
    assert len(agent_faults) == 1 and agent_faults[0]["hat"] == "steer"

    # two full steer ITERATIONS happened (the exhausted one, then the
    # retry-in-place that succeeded) -- both refunded, neither charged.
    assert sup.iterations_used == 3
    assert sup._steer_refunded == 2
    status = json.loads(sup.run.status_file.read_text())
    assert status["iterationsUsed"] == 1 == sup.iterations_used_charged


def test_no_second_ladder_module_is_involved_for_steer():
    """There is exactly one call site in `_step_generic_hat` that reaches
    `contract_enforcement.enforce_contract`, and it is shared by every
    generic hat including `steer` -- proven at the source level, not
    merely by absence of a failure above."""
    import inspect

    from ralphd.engine import loop as loop_module

    src = inspect.getsource(loop_module.LoopSupervisor._step_generic_hat)
    assert src.count("contract_enforcement.enforce_contract,") == 1, (
        "steer must ride the ONE existing ladder call site, never a "
        "second copy of it")


def test_planning_still_gets_the_full_steering_body_injected(tmp_path):
    root = tmp_path / "run"
    run = RunDir(root=root)
    run.update_status(state="starting")
    run.add_steering("prefer approach B over approach A", "opPlanPref")

    sup = _supervisor(root, {})
    sup.run = run
    atomic_write_json(sup.run.tasks_file, {"version": 1, "tasks": []})

    prompt = sup.build_prompt("planning")
    assert "prefer approach B over approach A" in prompt
    assert "MUST take priority" in prompt


# Every hat there is (`transitions.HAT_SCHEMAS`) and the prompt file each one
# is built from -- `verify` is the only hat whose prompt name differs from its
# phase name (`prompts/task-verify.md`, see `_step_verify`'s
# `prompt_name="task-verify"`). Derived from the shipped hat table itself
# (not hand-listed) so a new hat (e.g. task 018/020's `plan_review`/
# `verdict_gate`) is picked up automatically instead of silently passing --
# only the `verify` exception is hand-carried, because nothing in the table
# names a hat's prompt file.
HAT_PROMPT_NAMES = {hat: ("task-verify" if hat == "verify" else None)
                    for hat in transitions.HAT_SCHEMAS}


def test_the_hat_list_this_module_checks_is_the_whole_hat_table():
    """So "every hat's prompt" cannot quietly become "every hat we happened
    to list here" when a later task adds a seventh hat."""
    assert set(HAT_PROMPT_NAMES) == set(transitions.HAT_SCHEMAS)


def _echo(prompt: str) -> str:
    """The read-only echo section of a prompt (from its heading to the end)."""
    assert STEERING_ECHO_HEADING in prompt, "no read-only echo in this prompt"
    return prompt[prompt.index(STEERING_ECHO_HEADING):]


def _prompt_supervisor(root: Path) -> LoopSupervisor:
    sup = _supervisor(root, {})
    atomic_write_json(sup.run.tasks_file, {"version": 1, "tasks": []})
    return sup


def test_every_hat_prompt_echoes_a_message_that_is_already_settled(tmp_path):
    """The property the requirement names: RECENT operator messages, in
    EVERY hat's prompt. A message that has been consumed/settled is exactly
    the case the old filename-only notice dropped on the floor -- it
    vanished from every prompt the moment it was applied, so `review`,
    `verify` and `reflect` could never see what the operator had just
    asked for."""
    root = tmp_path / "run"
    RunDir(root=root).update_status(state="starting")
    sup = _prompt_supervisor(root)
    sup.run.add_steering("do not touch the migration yet", "opEcho")
    sup.run.consume_steering(sup.run.pending_steering(), 2)
    assert sup.run.pending_steering() == [], "precondition: already settled"

    for phase, prompt_name in HAT_PROMPT_NAMES.items():
        prompt = sup.build_prompt(phase, prompt_name=prompt_name)
        echo = _echo(prompt)
        assert "001-opEcho.md" in echo, phase          # the file name
        assert "do not touch the migration yet" in echo, phase   # the body
        assert "applied (settled)" in echo, phase      # its harness state
        # ... and nothing anywhere claims this phase may act on it: there is
        # nothing pending at all, so no actionable section exists.
        assert "MUST take priority" not in prompt, phase
        assert "pending, not for this phase" not in prompt, phase


def test_the_echo_carries_no_consumption_semantics(tmp_path):
    """Reading (or building) the echo must never consume, settle, or
    re-present anything, and it must not read as permission to act:
    consumption is exclusively `RunDir.consume_steering`, called only after
    a real actionable-phase iteration finishes cleanly, and `settled` is
    never `done` (requirement I -- `tasks.json` is the single source of
    truth for that)."""
    root = tmp_path / "run"
    RunDir(root=root).update_status(state="starting")
    sup = _prompt_supervisor(root)
    sup.run.add_steering("do not touch the migration yet", "opEcho")
    sup.run.add_steering("and land the fix today", "opToday")
    before = [p.name for p in sup.run.pending_steering()]

    for phase, prompt_name in HAT_PROMPT_NAMES.items():
        echo = _echo(sup.build_prompt(phase, prompt_name=prompt_name))
        assert "read-only" in echo and "no consumption semantics" in echo
        assert "not permission to act" in echo
        assert "single source of truth" in echo
        assert str(sup.run.tasks_file) in echo
        assert "pending" in echo  # each message's state, plainly labelled

    # building all six prompts changed nothing about what is pending, and
    # wrote no consumption record at all.
    assert [p.name for p in sup.run.pending_steering()] == before == [
        "001-opEcho.md", "002-opToday.md"]
    assert not (root / "steering" / ".consumed.json").exists()


def test_a_non_actionable_phase_still_gets_the_pending_not_for_this_phase_notice(
        tmp_path):
    """The echo's predecessor is KEPT, because it says the one thing the echo
    deliberately does not: whether THIS hat may act.

    Assertion changed by task 014's rework: this test used to assert the
    body of a pending message appears NOWHERE in a `review` prompt. That
    claim contradicted requirement J's echo (recent operator messages in
    every hat's prompt), so it is replaced by a stricter pair -- the body
    appears ONLY inside the read-only echo section and never in the
    act-on-this part of the prompt, and the filename notice is still there.
    """
    root = tmp_path / "run"
    RunDir(root=root).update_status(state="starting")
    sup = _prompt_supervisor(root)
    sup.run.add_steering("do not touch the migration yet", "opEcho")

    prompt = sup.build_prompt("review")
    head, echo = prompt.split(STEERING_ECHO_HEADING, 1)
    assert "pending, not for this phase" in head
    assert "opEcho" in head                                  # named
    assert "do not touch the migration yet" not in head, (
        "a non-actionable hat must not be handed the body as something to "
        "act on -- only as read-only context, below the echo heading")
    assert "do not touch the migration yet" in echo
    assert "MUST take priority" not in prompt

    # read-only: nothing about building this prompt may mark anything
    # consumed -- consumption is exclusively `RunDir.consume_steering`,
    # called only after a real actionable-phase iteration finishes cleanly.
    assert sup.run.pending_steering() != []


def test_the_echo_is_capped_at_the_most_recent_messages_and_says_so(tmp_path):
    """A job that collects dozens of steers must not have its prompt crowded
    out by them: the echo keeps the most recent
    `STEERING_ECHO_MAX_MESSAGES` and states the cap instead of applying it
    silently."""
    root = tmp_path / "run"
    RunDir(root=root).update_status(state="starting")
    sup = _prompt_supervisor(root)
    total = STEERING_ECHO_MAX_MESSAGES + 2
    for i in range(1, total + 1):
        sup.run.add_steering(f"message body number {i}", f"m{i}")

    echo = _echo(sup.build_prompt("reflect"))
    assert f"most recent of {total}" in echo
    for i in (1, 2):        # the two oldest are dropped, by name and body
        assert f"00{i}-m{i}.md" not in echo
        assert f"message body number {i}\n" not in echo
    kept = [i for i in range(3, total + 1)]
    positions = [echo.index(f"message body number {i}") for i in kept]
    assert positions == sorted(positions), "oldest first, arrival order"


def test_a_very_long_message_is_truncated_with_a_pointer_to_the_file(tmp_path):
    root = tmp_path / "run"
    RunDir(root=root).update_status(state="starting")
    sup = _prompt_supervisor(root)
    sup.run.add_steering("x" * (STEERING_ECHO_MAX_BODY_CHARS + 500), "opLong")

    echo = _echo(sup.build_prompt("worker"))
    assert f"truncated at {STEERING_ECHO_MAX_BODY_CHARS} characters" in echo
    assert str(root / "steering" / "001-opLong.md") in echo


def _infra_fault(sup, prompt=""):
    """What the infra classifier reads as an outage, not an agent fault:
    a connection error after a non-instant duration."""
    r = IterationResult(exit_code=0)
    r.error_message = "Connection error."
    r.duration_s = 30.0
    return r


@pytest.mark.asyncio
async def test_an_infra_fault_after_a_steer_pass_never_recharges_it(tmp_path):
    """The published `iterationsUsed` -- the figure the operator reads and
    the one a budget change is validated against -- must never re-charge a
    refunded steer pass. The infra-retry path used to publish
    `iterations_used - _infra_refunded` by hand, silently putting the steer
    slot back on the bill; it now publishes the single charged-count
    expression, `iterations_used_charged`."""
    root = tmp_path / "run"
    RunDir(root=root).update_status(state="starting")
    seen: list[int] = []

    def _capture_then_complete(sup, prompt=""):
        return _worker_completes(TASK_1)(sup, prompt)

    sup = _supervisor(root, {"steer": [_steer_settles],
                             "worker": [_infra_fault, _capture_then_complete]})

    async def capture_backoff(seconds):
        # runs between the infra classification's own status write and the
        # retry attempt -- i.e. exactly on the figure that write published.
        seen.append(json.loads(sup.run.status_file.read_text())[
            "iterationsUsed"])
        return seconds, False

    sup._wait_out_backoff = capture_backoff  # type: ignore[method-assign]
    atomic_write_json(sup.run.tasks_file, {"version": 1, "tasks": []})
    sup.run.add_steering("one small thing", "opCharge")
    sup.iterations_used = 1

    outcome = await sup._step_generic_hat("steer", STEER_EDGES)
    assert outcome == ("advance", "worker", "steer_settled")
    assert sup._steer_refunded == 1

    result = await sup._run_iteration_with_infra_retry("worker", "", None)
    assert result.final_text, "the wrapper rode the outage out"
    assert sup._infra_refunded == 1

    # raw attempts: the seeded 1 + steer + 2 worker attempts = 4. Charged: the
    # seeded one plus the one clean worker pass = 2 (the steer pass and the
    # infra-faulted attempt are both refunded). The mid-outage write must
    # already agree with that arithmetic.
    assert sup.iterations_used == 4
    assert sup.iterations_used_charged == 2
    assert seen == [1], (
        "the figure published between the infra fault and its retry "
        "re-charged the refunded steer pass (seeded 1 + nothing chargeable "
        "yet = 1)")
    status = json.loads(sup.run.status_file.read_text())
    assert status["iterationsUsed"] == 2


# ===========================================================================
# 4. `tests/test_steer_task_update_justification_task017.py` -- the steer
#    hat's `task-update` tool requires a justification on `failed ->
#    skipped` and appends it as the authoritative tail of `notes`.
# ===========================================================================


def _broker(tmp_path: Path, tasks: list[dict]) -> hat_tools.SteerToolBroker:
    run = RunDir(root=tmp_path / "run")
    atomic_write_json(run.tasks_file, {"version": 1, "tasks": tasks})
    return hat_tools.SteerToolBroker(run, vigilant=False,
                                     steering=("001-opX.md",))


def _task(broker: hat_tools.SteerToolBroker, task_id: str) -> dict:
    for t in broker.run.read_tasks()["tasks"]:
        if t.get("id") == task_id:
            return t
    raise AssertionError(f"no task {task_id!r}")


def _broker_events(broker: hat_tools.SteerToolBroker, type_: str) -> list[dict]:
    log = broker.run.root / "events.jsonl"
    lines = log.read_text().splitlines() if log.exists() else []
    return [ev for ev in (json.loads(x) for x in lines)
            if ev.get("type") == type_]


def test_explicit_justification_is_accepted_and_lands_in_notes(tmp_path):
    broker = _broker(tmp_path, [
        {"id": "013", "title": "t", "status": "failed",
         "successCriteria": "do X"},
    ])
    response = broker.execute("task-update", {
        "taskId": "013",
        "changes": {"status": "skipped"},
        "justification": "unmeetable per petition p1; residual disclosed",
    })
    assert response["ok"] is True, response
    task = _task(broker, "013")
    assert task["status"] == "skipped"
    assert task["notes"] == "unmeetable per petition p1; residual disclosed"
    # never touched by the status-only move
    assert task["successCriteria"] == "do X"
    events = _broker_events(broker, "amendment_status_applied")
    assert len(events) == 1
    assert events[0]["source"] == "steer"
    assert events[0]["previous"] == "failed"
    assert events[0]["status"] == "skipped"
    assert events[0]["justification"] == (
        "unmeetable per petition p1; residual disclosed")


def test_justification_defaults_to_changes_notes_when_omitted(tmp_path):
    broker = _broker(tmp_path, [
        {"id": "014", "title": "t", "status": "failed"},
    ])
    response = broker.execute("task-update", {
        "taskId": "014",
        "changes": {"status": "skipped",
                    "notes": "superseded by task 020, carved gap landed"},
    })
    assert response["ok"] is True, response
    task = _task(broker, "014")
    assert task["status"] == "skipped"
    assert task["notes"] == "superseded by task 020, carved gap landed"


def test_explicit_justification_survives_a_distinct_changes_notes(tmp_path):
    """Task 038 (the cure for task 017's residual gap, requirement F.4):
    when the SAME call carries both a top-level `justification` and a
    different `changes["notes"]`, the justification used to be written by
    the applier and then silently overwritten by the plain dict-merge of
    `changes`. It is now appended to the requested notes -- as the last
    segment, never replaced by them -- and every other requested field
    update still lands."""
    broker = _broker(tmp_path, [
        {"id": "018", "title": "t", "status": "failed",
         "successCriteria": "do Z", "priority": 3},
    ])
    response = broker.execute("task-update", {
        "taskId": "018",
        "changes": {"status": "skipped",
                    "notes": "see artifacts/reports/v0.18-petitions.md",
                    "priority": 9},
        "justification": "criteria unmeetable in this sandbox; "
                         "residual disclosed",
    })
    assert response["ok"] is True, response
    task = _task(broker, "018")
    assert task["status"] == "skipped"
    notes = task["notes"]
    # BOTH texts are present ...
    assert "see artifacts/reports/v0.18-petitions.md" in notes
    assert ("criteria unmeetable in this sandbox; residual disclosed"
            in notes)
    # ... and the justification is the authoritative tail, appended after
    # the requested notes rather than overwritten by them.
    assert notes.endswith(
        "criteria unmeetable in this sandbox; residual disclosed")
    assert notes != "see artifacts/reports/v0.18-petitions.md"
    # other requested field updates are preserved
    assert task["priority"] == 9
    assert task["successCriteria"] == "do Z"
    events = _broker_events(broker, "amendment_status_applied")
    assert len(events) == 1
    assert events[0]["justification"] == (
        "criteria unmeetable in this sandbox; residual disclosed")


def test_a_justification_equal_to_changes_notes_is_not_duplicated(tmp_path):
    """The append is idempotent: a caller that passes the same text in both
    places (or lets `justification` default to `changes.notes`) gets it
    exactly once, not twice."""
    broker = _broker(tmp_path, [
        {"id": "019", "title": "t", "status": "failed"},
    ])
    response = broker.execute("task-update", {
        "taskId": "019",
        "changes": {"status": "skipped", "notes": "superseded by task 038"},
        "justification": "superseded by task 038",
    })
    assert response["ok"] is True, response
    assert _task(broker, "019")["notes"] == "superseded by task 038"


def test_notes_that_merely_mention_the_justification_still_get_it_appended(
        tmp_path):
    """Task 038's own residual gap: the suppression test is a TAIL test, not
    a substring test. Requested notes that mention the justification inside
    a longer sentence are not the authoritative tail of the move's record,
    so the justification is still appended after them."""
    broker = _broker(tmp_path, [
        {"id": "023", "title": "t", "status": "failed"},
    ])
    response = broker.execute("task-update", {
        "taskId": "023",
        "changes": {"status": "skipped",
                    "notes": "The reason is GAP, with additional context."},
        "justification": "GAP",
    })
    assert response["ok"] is True, response
    notes = _task(broker, "023")["notes"]
    assert notes.endswith("GAP")
    assert notes == "The reason is GAP, with additional context.\n\nGAP"


def test_a_justification_that_is_a_suffix_word_is_still_appended(tmp_path):
    """A requested notes value that merely ENDS with the justification's
    characters as part of a longer word (`"the megaGAP"` for `"GAP"`) is not
    this function's own appended tail either -- only the exact separator +
    justification it writes itself suppresses a second append."""
    broker = _broker(tmp_path, [
        {"id": "024", "title": "t", "status": "failed"},
    ])
    response = broker.execute("task-update", {
        "taskId": "024",
        "changes": {"status": "skipped", "notes": "the megaGAP"},
        "justification": "GAP",
    })
    assert response["ok"] is True, response
    assert _task(broker, "024")["notes"] == "the megaGAP\n\nGAP"


def test_applying_the_append_twice_is_a_no_op(tmp_path):
    """Idempotence over the function's OWN output: feeding an already
    appended notes value back in (the shape a second steer pass would see)
    appends nothing further."""
    broker = _broker(tmp_path, [
        {"id": "025", "title": "t", "status": "failed"},
        {"id": "026", "title": "t", "status": "failed"},
    ])
    first = broker.execute("task-update", {
        "taskId": "025",
        "changes": {"status": "skipped",
                    "notes": "see the petition file"},
        "justification": "unmeetable here",
    })
    assert first["ok"] is True, first
    once = _task(broker, "025")["notes"]
    second = broker.execute("task-update", {
        "taskId": "026",
        "changes": {"status": "skipped", "notes": once},
        "justification": "unmeetable here",
    })
    assert second["ok"] is True, second
    assert _task(broker, "026")["notes"] == once
    assert once.count("unmeetable here") == 1


def test_a_blank_changes_notes_does_not_shadow_the_justification(tmp_path):
    broker = _broker(tmp_path, [
        {"id": "020", "title": "t", "status": "failed"},
    ])
    response = broker.execute("task-update", {
        "taskId": "020",
        "changes": {"status": "skipped", "notes": "   "},
        "justification": "gap carved into task 041",
    })
    assert response["ok"] is True, response
    assert _task(broker, "020")["notes"] == "gap carved into task 041"


def test_only_failed_to_skipped_appends_the_justification(tmp_path):
    """The extra condition -- and so the notes rule that carries it -- is
    declared for exactly one edge; any other move writes the requested
    `notes` verbatim."""
    broker = _broker(tmp_path, [
        {"id": "021", "title": "t", "status": "validation-failed"},
    ])
    response = broker.execute("task-update", {
        "taskId": "021",
        "changes": {"status": "skipped", "notes": "operator call"},
        "justification": "not required on this edge",
    })
    assert response["ok"] is True, response
    assert _task(broker, "021")["notes"] == "operator call"


def test_missing_justification_is_refused_with_the_appliers_own_diagnostic(
        tmp_path):
    broker = _broker(tmp_path, [
        {"id": "015", "title": "t", "status": "failed",
         "successCriteria": "do Y"},
    ])
    response = broker.execute("task-update", {
        "taskId": "015",
        "changes": {"status": "skipped"},
    })
    assert response["ok"] is False, response
    assert "requires a non-empty justification" in (response["error"] or "")
    assert response["detail"]["statusResult"]["applied"] is False
    task = _task(broker, "015")
    # the status must NOT have moved, and notes must not have been touched
    assert task["status"] == "failed"
    assert "notes" not in task
    assert _broker_events(broker, "amendment_status_applied") == []


def test_missing_justification_with_blank_notes_is_still_refused(tmp_path):
    """An empty/whitespace-only `notes` does not count as a stated reason
    any more than an empty `justification` argument would -- both are
    "no justification given" to `transitions.task_status_report_errors`."""
    broker = _broker(tmp_path, [
        {"id": "016", "title": "t", "status": "failed"},
    ])
    response = broker.execute("task-update", {
        "taskId": "016",
        "changes": {"status": "skipped", "notes": "   "},
    })
    assert response["ok"] is False, response
    task = _task(broker, "016")
    assert task["status"] == "failed"


def test_the_refusal_still_surfaces_the_diagnostic_beside_other_changes(
        tmp_path):
    """Task 038: the refused-without-justification case is unchanged by the
    append rule -- the status stays `failed`, the applier's own diagnostic
    reaches the caller, and nothing writes a justification into `notes`."""
    broker = _broker(tmp_path, [
        {"id": "022", "title": "t", "status": "failed", "priority": 2},
    ])
    response = broker.execute("task-update", {
        "taskId": "022",
        "changes": {"status": "skipped", "priority": 7},
    })
    assert response["ok"] is False, response
    assert "requires a non-empty justification" in (response["error"] or "")
    assert response["detail"]["statusResult"]["applied"] is False
    task = _task(broker, "022")
    assert task["status"] == "failed"
    assert "notes" not in task
    assert _broker_events(broker, "amendment_status_applied") == []


def test_a_transition_other_than_failed_to_skipped_needs_no_justification(
        tmp_path):
    """The extra condition is declared for exactly one edge
    (`transitions.task_status_report_errors`); every other legal move
    through this same tool is unaffected."""
    broker = _broker(tmp_path, [
        {"id": "017", "title": "t", "status": "validation-failed"},
    ])
    response = broker.execute("task-update", {
        "taskId": "017",
        "changes": {"status": "skipped"},
    })
    assert response["ok"] is True, response
    assert _task(broker, "017")["status"] == "skipped"


# ===========================================================================
# 4b. Task 012 (PRD requirement D.1/D.2, #184): `steer_task_update` refuses
#     a `dependsOn` write to a task the picker has already CLAIMED
#     (`in-progress`/`validation-failed`) -- naming the task, its status,
#     and the remedy -- while a `dependsOn` write to a still-`pending` task
#     stays legal without limit. `test_cli_steer_list.py::
#     test_a_dependson_refusal_is_readable_in_ralphctl_steer_list` is the
#     third test (D.2): the same refusal text landing on the steering
#     ledger and printed by `ralphctl steer --list`.
# ===========================================================================


@pytest.mark.parametrize("claimed_status", ["in-progress", "validation-failed"])
def test_dependson_write_is_refused_on_a_task_the_picker_has_claimed(
        tmp_path, claimed_status):
    broker = _broker(tmp_path, [
        {"id": "030", "title": "t", "status": claimed_status,
         "dependsOn": ["029"]},
    ])
    response = broker.execute("task-update", {
        "taskId": "030",
        "changes": {"dependsOn": ["099"]},
    })
    assert response["ok"] is False, response
    error = response["error"] or ""
    # names the task, its status, and the remedy (insert a replacement,
    # re-point dependents).
    assert "'030'" in error
    assert repr(claimed_status) in error
    assert "replacement" in error
    assert "re-point" in error
    # the write never landed.
    assert _task(broker, "030")["dependsOn"] == ["029"]
    depends_on_result = response["detail"]["dependsOnResult"]
    assert depends_on_result["applied"] is False
    assert depends_on_result["status"] == claimed_status


def test_dependson_refusal_leaves_other_requested_fields_applied(tmp_path):
    """One hazardous key does not sink the rest of the same call -- the
    same shape the missing-justification refusal above already uses."""
    broker = _broker(tmp_path, [
        {"id": "031", "title": "t", "status": "in-progress",
         "dependsOn": ["029"], "priority": 2},
    ])
    response = broker.execute("task-update", {
        "taskId": "031",
        "changes": {"dependsOn": ["099"], "priority": 9},
    })
    assert response["ok"] is False, response
    task = _task(broker, "031")
    assert task["dependsOn"] == ["029"]
    assert task["priority"] == 9


def test_dependson_write_to_a_pending_task_stays_legal(tmp_path):
    broker = _broker(tmp_path, [
        {"id": "032", "title": "t", "status": "pending",
         "dependsOn": ["029"]},
        {"id": "099", "title": "other", "status": "pending"},
    ])
    response = broker.execute("task-update", {
        "taskId": "032",
        "changes": {"dependsOn": ["099"]},
    })
    assert response["ok"] is True, response
    assert _task(broker, "032")["dependsOn"] == ["099"]
    assert response["detail"]["dependsOnResult"] == {"applied": True}


# ===========================================================================
# 4c. Task 013 (PRD requirement D.3, #184): the audit of `steer_task_update`'s
#     OTHER non-`status` merged fields. `successCriteria` carries the exact
#     same hazard `dependsOn` does -- rewriting what a claimed task's own
#     in-flight attempt is executing against, mid-attempt -- but is already
#     SAFE on a claimed task, unlike `dependsOn`: fix #132 (`e20175a`) moves
#     `criteriaFingerprint`/`criteriaOfRecord` forward atomically with the
#     text in the SAME `write_tasks` call, so `LoopSupervisor.
#     _check_criteria_edits`'s very next pass sees a fingerprint that
#     already matches the new text and never flags/restores it -- exactly
#     the same legal-edit path a judge-authored criteria amendment already
#     uses (`_apply_criteria_amendments`). This asserts that holds
#     unconditionally on BOTH claimed statuses `dependsOn` refuses --
#     `steer_task_update` never gates `successCriteria` on task status at
#     all, so there is nothing to unconditionally refuse.
# ===========================================================================


@pytest.mark.parametrize("claimed_status", ["in-progress", "validation-failed"])
def test_successcriteria_write_stays_legal_on_a_task_the_picker_has_claimed(
        tmp_path, claimed_status):
    original = "ship the widget exactly as originally scoped"
    fp = LoopSupervisor._criteria_fingerprint(original)
    broker = _broker(tmp_path, [
        {"id": "036", "title": "t", "status": claimed_status,
         "successCriteria": original,
         "criteriaFingerprint": fp, "criteriaOfRecord": original},
    ])
    new_criteria = "ship the widget, relaxed per operator steer"
    response = broker.execute("task-update", {
        "taskId": "036",
        "changes": {"successCriteria": new_criteria},
    })
    # unlike dependsOn, this is never refused for a claimed task.
    assert response["ok"] is True, response
    task = _task(broker, "036")
    assert task["successCriteria"] == new_criteria
    # criteriaFingerprint/criteriaOfRecord moved forward atomically, so a
    # later `_check_criteria_edits` pass sees no mismatch and never
    # restores the old text or flags the task.
    assert task["criteriaFingerprint"] == LoopSupervisor._criteria_fingerprint(
        new_criteria)
    assert task["criteriaOfRecord"] == new_criteria
    assert not task.get("criteriaEditedAfterValidationFailure")
    # the task's claimed status itself is untouched by this call.
    assert task["status"] == claimed_status


# ===========================================================================
# 5. `tests/test_steer_criteria_survives_worker_pass_task002.py` -- a steer
#    `successCriteria` edit that moves the fingerprint forward survives the
#    next `LoopSupervisor` worker pass's `_check_criteria_edits`.
# ===========================================================================


def _make_sup(tmp_path, tasks, vigilant=False):
    root = tmp_path / "run"
    root.mkdir(parents=True)
    run = RunDir(root=root)
    run.update_status(state="running")
    atomic_write_json(run.tasks_file, {"version": 1, "tasks": tasks})
    cfg = JobConfig(run_id="task002-steer-survives-worker-pass", iterations=5,
                    max_approaches=1, vigilant=vigilant, on_complete="exit",
                    infra_retry_backoff_s=[0.0], infra_retry_backoff_max_s=0.0,
                    infra_outage_budget_s=1000.0)
    sup = LoopSupervisor(cfg, run, root)
    return sup, run


def _criteria_edit_refused_events(run: RunDir) -> list[dict]:
    return [e for e in read_events(run.root)
            if e.get("type") == "task.criteria_edit_refused"]


def test_steer_criteria_edit_survives_the_next_worker_pass(tmp_path):
    original = "ship the widget exactly as originally scoped"
    fp = LoopSupervisor._criteria_fingerprint(original)
    sup, run = _make_sup(tmp_path, [
        {"id": "001", "title": "ship the widget", "status": "pending",
         "successCriteria": original,
         "criteriaFingerprint": fp, "criteriaOfRecord": original},
    ])

    # The steer-tool edit -- the exact call `steer_task_update` makes for a
    # `task-update` tool invocation carrying `successCriteria`.
    new_criteria = "ship the widget, relaxed per operator steer 003"
    result = state.steer_task_update(
        run, "001", {"successCriteria": new_criteria}, sup.cfg.vigilant)
    assert result["updated"] is True

    class _Agent:
        def interrupt(self):
            return False

        async def run(self, prompt, transcript, **kw):
            r = IterationResult(exit_code=0)
            r.final_text = json.dumps({"verdict": "complete", "task_id": "001"})
            r.duration_s = 0.01
            r.usage = {"input": 1, "output": 1, "totalTokens": 2}
            return r

    sup.runner = _Agent()  # type: ignore[assignment]
    asyncio.run(sup._step_worker())

    # 1) no refusal event fired on this pass.
    assert _criteria_edit_refused_events(run) == []

    tasks_after = run.read_tasks()["tasks"]
    task = tasks_after[0]

    # 2) the steer's text still holds, no tamper marker, no flagged finding.
    assert task["successCriteria"] == new_criteria
    assert not task.get("criteriaEditedAfterValidationFailure")
    assert findings_mod.flagged_criteria_findings(tasks_after) == []

    # the pass actually completed the task -- the edit did not just
    # silently fail to apply.
    assert task["status"] == "completed"
    assert task["criteriaFingerprint"] == LoopSupervisor._criteria_fingerprint(
        new_criteria)
    assert task["criteriaOfRecord"] == new_criteria


# ===========================================================================
# 6. `tests/test_steer_tool_spool_two_uid_docker_task030.py` -- the real uid
#    boundary: the steer hat and its hat-tools client really run as uid
#    1000 and really cannot write the root-owned `tasks.json`; the engine's
#    own process answers and applies the spooled request. Docker tier --
#    marks applied per-function (see `_docker_tier` above).
# ===========================================================================

# Asked for by the hat, and every one of them refused or overridden by the
# applier on the engine's side of the spool: `origin`/`status` are stamped by
# `state.STEER_HAT_TOOLS["task-insert"]` (task 010, requirement H, #98), and
# `pending -> validated` is a move no steer `task-update` may claim in a
# non-vigilant run: the edge itself is declared in both variants
# (cure-01-05, PRD requirement B.3/B.4, F05, #221 -- it is the engine's own
# un-reopen restore of a status it persisted before a cure pass re-opened a
# record), and `transitions.task_status_report_errors` admits it for the
# single caller `source="cure-reconciliation"` alone, refusing it here.
SPOOLED_CALLS = [
    {"tool": "task-insert",
     "args": {"task": {"id": "spooled-1", "title": "via the spool",
                       "status": "completed", "origin": "attacker"}}},
    {"tool": "task-update",
     "args": {"taskId": "spooled-1", "changes": {"status": "validated"}}},
]


@pytest.fixture(scope="module")
def spool_run(work_root, gateway_ip, test_image):
    """One real job container, steered once, driven to its terminal state.

    Its own unique run id (never this job's `RALPHD_RUN_ID`), and teardown
    force-removes that exact container name -- never a label query, which
    could match this very job's container.
    """
    ctl = Ctl(work_root, gateway_ip, test_image)
    run_id = _unique_run_id("e130-spool")
    # task 009 (B.3): STUB_HOLD_FILE (a container-side path under
    # /workspace, mounted from work_root/ws below) holds the first worker
    # iteration -- after it has flipped its task to in-progress, the same
    # position `tests/stub-pi/pi`'s own `_apply_hold("worker")` uses --
    # deterministically wide enough for the `ralphctl steer` call below to
    # land while a worker iteration remains, instead of racing a fixed
    # STUB_SLEEP window across two tasks against how fast the run finishes.
    # Released (touched) right after the steer call; the shared hold file
    # then passes every later worker invocation (including the second
    # task's) through instantly, per the primitive's own documented
    # once-released-stays-released behaviour.
    hold_rel = ".hold-spool-steer"
    hold_file_host = work_root / "ws" / hold_rel
    res = ctl.start(run_id,
                    extra_env={"STUB_TASKS": "2",
                               "STUB_HOLD_FILE": f"/workspace/{hold_rel}",
                               "STUB_HOLD_PHASE": "worker",
                               "STUB_HOLD_TIMEOUT_S": "60",
                               "STUB_STEER_TOOL_CALLS": json.dumps(SPOOLED_CALLS),
                               "STUB_STEER_MALFORMED_REQUEST": "1"},
                    extra=("--on-complete", "idle"))
    assert res.returncode == 0, res.stderr
    meta = json.loads(res.stdout)
    # Steer while worker iterations remain: the `worker -> steer` edge is
    # taken when unsettled steering exists, so a message arriving after the
    # last task is done would never reach the steer hat at all. The hold
    # above (not yet released) guarantees a worker iteration is still
    # blocked, mid-run, when this call is made. Sent through the real
    # `ralphctl steer` (the operator's own channel), not an internal import.
    # Task 012 (requirement I, live docker-tier finding): `state: running`
    # is not proof the worker child this steer message means to land beside
    # has actually been spawned yet (`tests/conftest.py::wait_hold_entered`'s
    # own docstring: two hosted-gate flakes were exactly this). Caught live
    # under this task's own heavier (root) docker-tier job: `ctl.run("steer"
    # ...)` + the touch below landed AFTER the first task's worker iteration
    # had already run and completed unheld, so `resume`'s remaining task(s)
    # finished the whole run before the spooled request ever reached a
    # `worker -> steer` edge to be consumed on -- `steering: {pending: 1,
    # consumed: 0}` at a terminal state, forever. CAUSE and OBSERVE instead
    # of a longer timeout: block until the worker iteration's own
    # hold-entered marker exists, proof a worker iteration is genuinely
    # parked before sending the message or releasing the hold.
    wait_hold_entered(hold_file_host)
    # `ralphctl steer <run> <message>` -- the message is POSITIONAL (there is
    # no `--message` flag; `--file`/stdin are the alternatives).
    steered = ctl.run("steer", run_id, "insert a task through the spool",
                      "--name", "spool")
    assert steered.returncode == 0, steered.stderr
    hold_file_host.touch()  # release: let the held worker iteration proceed
    status = _wait_status(meta["apiUrl"], lambda s: s.get("state") in
                          ("succeeded", "failed", "aborted"), timeout=180)
    yield SimpleNamespace(ctl=ctl, run_id=run_id, container=f"ralphd-{run_id}",
                          rdir=ctl.registry / "runs" / run_id, status=status,
                          api=meta["apiUrl"])
    ctl.force_cleanup()


def _tasks(run) -> list[dict]:
    return json.loads((run.rdir / "tasks.json").read_text())["tasks"]


@_docker_tier
def test_the_hat_uid_cannot_write_tasks_json_and_the_engine_owns_it(spool_run):
    """The premise the whole spool exists for, measured on a live container:
    the hat's process is uid 1000, `tasks.json` is root's, and a write from
    uid 1000 -- attempted from inside the container, as the exact uid every
    iteration runs as -- is refused by the kernel."""
    probe_path = spool_run.rdir / ".stub-steer-uid.json"
    assert probe_path.is_file(), "the steer hat never ran in this container"
    # written by the hat's own process, so its owner IS the hat's uid
    assert probe_path.stat().st_uid == 1000, probe_path.stat().st_uid
    probe = json.loads(probe_path.read_text())
    assert probe["uid"] == probe["euid"] == 1000, probe
    assert probe["tasksOpenRwErrno"] == errno.EACCES, (
        "the steer hat's own process could open tasks.json for writing "
        f"(errno {probe['tasksOpenRwErrno']!r}): there is no uid boundary "
        "here, so nothing this module asserts about the spool would mean "
        "the engine performed the write")

    tasks_path = spool_run.rdir / "tasks.json"
    before = json.loads(tasks_path.read_text())
    assert tasks_path.stat().st_uid == 0, (
        f"tasks.json is owned by uid {tasks_path.stat().st_uid}, not root -- "
        "task 105 (#60 requirement D) makes it root-owned precisely so the "
        "agent uid cannot write its own task record")

    denied = subprocess.run(
        [REAL_DOCKER, "exec", "--user", "1000:1000", spool_run.container,
         "sh", "-c", "echo '{\"tasks\": []}' > /run/ralphd/tasks.json; echo RC=$?"],
        capture_output=True, text=True, timeout=15)
    assert denied.returncode == 0, denied.stderr  # docker exec itself worked
    assert "RC=0" not in denied.stdout, denied.stdout  # the shell write failed
    combined = denied.stdout + denied.stderr
    assert "Permission denied" in combined, combined
    assert json.loads(tasks_path.read_text()) == before  # untouched


@_docker_tier
def test_the_spooled_request_is_applied_by_the_engine_not_by_the_agent_uid(spool_run):
    """The hat asked for `status: completed` and `origin: attacker` on the
    inserted task, then for an illegal `pending -> validated` jump. What came
    back is the APPLIER's answer -- stamped `origin=steer`, the jump refused
    with the transition validator's own diagnostic -- and the file that
    changed is the root-owned one the hat cannot write. A hat that could
    write `tasks.json` itself would simply have what it asked for."""
    responses_path = spool_run.rdir / ".stub-steer-tool-responses.json"
    assert responses_path.is_file(), "the hat's client subprocess wrote no answers"
    assert responses_path.stat().st_uid == 1000, responses_path.stat().st_uid
    responses = json.loads(responses_path.read_text())
    assert len(responses) == 2, responses
    assert responses[0]["ok"] is True, responses[0]
    assert responses[0]["tool"] == "task-insert", responses[0]
    assert responses[1]["ok"] is False, responses[1]
    assert "status change refused" in responses[1]["error"], responses[1]

    inserted = next((t for t in _tasks(spool_run) if t["id"] == "spooled-1"), None)
    assert inserted is not None, _tasks(spool_run)
    assert inserted["origin"] == "steer", inserted
    # `status` is deliberately not asserted to `pending` (the applier's other
    # forced field): the ordinary worker lifecycle picks the new task up and
    # completes it through the normal, LEGAL transition afterwards. What must
    # never appear is the illegal jump's target.
    assert inserted["status"] != "validated", inserted

    # The engine's own event log -- written by the engine's process, never the
    # hat's -- recorded the call as served, naming the steering that caused it.
    events = [json.loads(line) for line
              in (spool_run.rdir / "events.jsonl").read_text().splitlines()]
    served = [ev for ev in events if ev.get("type") == "steer.tool_call"]
    assert any(ev["tool"] == "task-insert" and ev["ok"] is True
               and "001-spool.md" in ev.get("steering", []) for ev in served), served

    # Nothing left outstanding: the broker unlinks every request it serves.
    assert list((spool_run.rdir / "hat-tools" / "requests").glob("*.json")) == []


@_docker_tier
def test_a_malformed_spool_request_is_refused_without_ending_the_run(spool_run):
    """The same steer iteration also dropped one hand-written, unparseable
    file straight into the spool (never through `hat_tools.call()`). It is
    answered `ok: false` -- `SteerToolBroker.serve_once()`'s `except (OSError,
    json.JSONDecodeError)` branch -- and the run still reaches its ordinary
    terminal, verified state with its steering settled and its container up."""
    answer_path = spool_run.rdir / "hat-tools" / "responses" / "stub-malformed.json"
    assert answer_path.is_file(), "the malformed request was never answered"
    answer = json.loads(answer_path.read_text())
    assert answer["ok"] is False, answer
    assert "unreadable" in answer["error"], answer

    assert spool_run.status["state"] == "succeeded", spool_run.status
    assert spool_run.status["verdict"] == "verified", spool_run.status
    assert _container_running(spool_run.container) is True

    consumed = json.loads(
        (spool_run.rdir / "steering" / ".consumed.json").read_text())
    assert "001-spool.md" in consumed, consumed
