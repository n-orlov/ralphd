"""Task 012 (PRD requirement I, #98): unsettled operator steering is
ABSOLUTE TOP PRIORITY in the `worker` state -- above a validation-failed
retry, above any pending task, taken deterministically without ever
consulting `engine.router`. `ralphctl steer --now` (`POST /interrupt`) still
interrupts the in-flight iteration, but the transition it now forces is to
`steer`, not back into `worker`'s own self-loop.

The fix, pinned here: `LoopSupervisor._step_worker` checks
`self.run.pending_steering()` FIRST, every pass through its own `while`
loop -- strictly before `scheduler.select_next_task` is ever called and
before that pass's would-be task claim is written -- and takes the
already-declared `worker -> steer` edge (`transitions.TABLE["worker"]`'s
first entry, guard `steering_pending`, task 009) unconditionally when it is
non-empty. `scheduler.select_next_task()` itself is untouched: this task
adds no code to `engine/scheduler.py` at all, the priority lives entirely in
the hand-written `_step_worker` guard, exactly where `no_verdict_yet` and
`verdict_ready` already live -- never one of `_step_generic_hat`'s
multi-edge, router-eligible states (`worker` keeps its own dedicated
`_step_worker` handler in `_run_job_core`'s dispatch ladder; the router is
never reached for it, with or without pending steering).

`STEERING_ACTIONABLE_PHASES` gains `"steer"` alongside the pre-existing
`"planning"`/`"worker"` entries in this same commit -- without it, the
newly-reachable `steer` hat's own iteration would be handed only the
filename-only "pending, not for this phase" echo `build_prompt` gives
review/verify, never the actual message bodies `prompts/steer.md` step 1
tells it to read and act on.

Two directions, both driven through a real `LoopSupervisor` with a stubbed
`sup.runner` (never `_run_iteration_once`'s real subprocess seam), the same
scaffolding `tests/test_doomed_plan_fault.py` uses.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from ralphd.engine import router as router_module
from ralphd.engine import scheduler
from ralphd.engine import transitions
from ralphd.engine.config import JobConfig
from ralphd.engine.loop import LoopSupervisor
from ralphd.engine.runner import IterationResult
from ralphd.engine.state import RunDir, atomic_write_json

TASK_1 = "001"


# -- scaffolding (mirrors tests/test_doomed_plan_fault.py) -----------------


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


class _ScriptedAgent:
    """Same shape as `tests/test_doomed_plan_fault.py`'s `_ScriptedAgent`,
    plus a flat `phases_seen`/`prompts` record so a test can pin ORDER
    directly, without depending on which internal call sites happen to emit
    a `"phase"` event (`_step_worker`'s own while loop never does)."""

    def __init__(self, handlers: dict):
        self.handlers = handlers
        self.calls: dict[str, int] = {}
        self.phases_seen: list[str] = []
        self.prompts: list[str] = []
        self.running = False

    def interrupt(self) -> bool:
        return self.running

    async def run(self, prompt, transcript, **kw) -> IterationResult:
        phase = self.sup.run.read_status().get("phase")
        self.phases_seen.append(phase)
        self.prompts.append(prompt)
        n = self.calls.get(phase, 0)
        self.calls[phase] = n + 1
        steps = self.handlers[phase]
        self.running = True
        try:
            return steps[min(n, len(steps) - 1)](self.sup, prompt)
        finally:
            self.running = False


def _supervisor(root: Path, handlers: dict, **cfg_kw) -> LoopSupervisor:
    root.mkdir(parents=True, exist_ok=True)
    run = RunDir(root=root)
    kw = {"iterations": 15, "max_approaches": 1, "vigilant": False,
          "on_complete": "exit", "infra_retry_backoff_s": [0.0],
          "infra_retry_backoff_max_s": 0.0, "infra_outage_budget_s": 1000.0,
          **cfg_kw}
    sup = LoopSupervisor(JobConfig(run_id="v014-steer-priority", **kw), run, root)
    agent = _ScriptedAgent(handlers)
    agent.sup = sup                      # type: ignore[attr-defined]
    sup.runner = agent                    # type: ignore[assignment]

    async def no_backoff(seconds):
        return seconds, False

    sup._wait_out_backoff = no_backoff    # type: ignore[method-assign]
    return sup


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


# ---------------------------------------------------------------------------
# 1. priority over a pending task
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# 2. priority over a validation-failed retry
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# 3. the table edge itself, and that the router is never consulted for it
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# 4. all pending steers in one iteration, in arrival order
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# 5. `ralphctl steer --now` forces the transition to `steer`, not `worker`
# ---------------------------------------------------------------------------


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
