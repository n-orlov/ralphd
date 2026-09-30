"""Task 002 (#278 B.1/B.4/B.7): `select_next_task`'s pending step, routed
through the router when it has 2+ runnable candidates.

`tests/test_scheduler.py`/`tests/test_worker_harness_owned_status.py` already
prove `scheduler.select_next_task()`/`LoopSupervisor._step_worker` in
isolation from each other; this module drives `_step_worker` for real (fake
subprocess, real state machine, same technique as
`tests/test_worker_harness_owned_status.py`) with a plan that has 2 runnable
pending tasks, and a fake agent that answers BOTH the router's own question
and the worker hat's own iteration -- proving the whole B.1/B.4/B.7 slice
end to end: deterministic-first (a single candidate never reaches the
router), a genuine choice honoured, the three distinct fallback causes
(unparseable / non-candidate id / timeout), and the shared `router.decision`
event shape on this axis and the pre-existing `plan_review` axis alike.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

from ralphd.engine.config import JobConfig
from ralphd.engine.loop import LoopSupervisor
from ralphd.engine.runner import IterationResult
from ralphd.engine.state import RunDir, atomic_write_json, read_events
from tests.conftest import call_ralphctl_in_process  # noqa: F401
from tests.test_router_engine import registered_ambiguous_hat  # noqa: F401


def _ok(text: str) -> IterationResult:
    r = IterationResult(exit_code=0)
    r.final_text = text
    r.duration_s = 0.01
    r.usage = {"input": 10, "output": 5, "totalTokens": 15}
    return r


class _RoutingAgent:
    """Answers a router call (transcript under `artifacts/router/`) from a
    scripted list -- an entry that is a `float` sleeps that long before
    answering at all, so `asyncio.wait_for`'s own timeout in
    `router.decide_task_selection` fires first, exactly like a hung real
    call -- and otherwise completes whichever task the harness just claimed
    `in-progress`, whatever its id, the same way `tests/test_router_engine.
    py`'s `_happy_handlers`' `_complete_task` does."""

    def __init__(self, run_dir: RunDir, router_answers: list | None = None):
        self._run_dir = run_dir
        self.router_answers = list(router_answers or [])
        self.router_calls = 0

    def interrupt(self) -> bool:
        return False

    async def run(self, prompt, transcript, **kw) -> IterationResult:
        if Path(transcript).parent.name == "router":
            idx = self.router_calls
            self.router_calls += 1
            if idx >= len(self.router_answers):
                raise AssertionError(
                    "router called more times than the test scripted "
                    f"({self.router_calls} calls, "
                    f"{len(self.router_answers)} answers provided)")
            answer = self.router_answers[idx]
            if isinstance(answer, (int, float)):
                await asyncio.sleep(answer)
                return _ok("irrelevant -- the caller already timed out")
            return answer
        doc = self._run_dir.read_tasks()
        task_id = next(t["id"] for t in doc["tasks"]
                       if t.get("status") == "in-progress")
        return _ok("done\n" + json.dumps(
            {"verdict": "complete", "task_id": task_id}))


def _make_sup(tmp_path, tasks, run_id="task002-routing", **cfg_kw):
    root = tmp_path / "registry" / "runs" / run_id
    root.mkdir(parents=True)
    run = RunDir(root=root)
    run.update_status(state="running")
    atomic_write_json(run.tasks_file, {"version": 1, "tasks": tasks})
    kw = {"iterations": 1, "max_approaches": 1, "vigilant": False,
          "on_complete": "exit", "infra_retry_backoff_s": [0.0],
          "infra_retry_backoff_max_s": 0.0, "infra_outage_budget_s": 1000.0,
          "contract_retry_max": 2, "router_timeout_s": 5.0,
          **cfg_kw}
    cfg = JobConfig(run_id=run_id, **kw)
    sup = LoopSupervisor(cfg, run, root)
    return sup, run


TWO_TASKS = [
    {"id": "001", "title": "the first task", "status": "pending",
     "successCriteria": "the first thing is done"},
    {"id": "002", "title": "the second task", "status": "pending",
     "successCriteria": "the second thing is done"},
]


def _router_decision_events(run: RunDir) -> list:
    return [e for e in read_events(run.root) if e.get("type") == "router.decision"]


# ---------------------------------------------------------------------------
# Criterion 1: a single candidate never reaches the router, whatever
# `route_task_selection` says.
# ---------------------------------------------------------------------------

def test_a_single_candidate_never_calls_the_router(tmp_path):
    sup, run = _make_sup(tmp_path, [dict(TWO_TASKS[0])],
                         route_task_selection=True)
    agent = _RoutingAgent(run, router_answers=[
        _ok("the router must never be asked at all")])
    sup.runner = agent  # type: ignore[assignment]

    asyncio.run(sup._step_worker())

    assert agent.router_calls == 0
    assert _router_decision_events(run) == []
    assert run.read_tasks()["tasks"][0]["status"] == "completed"


def test_routing_disabled_never_calls_the_router_even_with_two_candidates(tmp_path):
    sup, run = _make_sup(tmp_path, [dict(t) for t in TWO_TASKS],
                         route_task_selection=False)
    agent = _RoutingAgent(run, router_answers=[
        _ok("the router must never be asked at all")])
    sup.runner = agent  # type: ignore[assignment]

    asyncio.run(sup._step_worker())

    assert agent.router_calls == 0
    assert _router_decision_events(run) == []
    # the deterministic pick (list order, no priority) -- "001" -- was
    # claimed and completed, "002" untouched.
    tasks = {t["id"]: t["status"] for t in run.read_tasks()["tasks"]}
    assert tasks == {"001": "completed", "002": "pending"}


# ---------------------------------------------------------------------------
# Criterion 2: a genuine router choice is honoured -- the SECOND task is
# claimed and worked, not the deterministic first pick.
# ---------------------------------------------------------------------------

def test_a_valid_router_answer_routes_the_worker_to_the_named_task(tmp_path):
    sup, run = _make_sup(tmp_path, [dict(t) for t in TWO_TASKS],
                         route_task_selection=True)
    agent = _RoutingAgent(run, router_answers=[
        _ok(json.dumps({"candidate_id": "002",
                        "reason": "the second task blocks nothing else"}))])
    sup.runner = agent  # type: ignore[assignment]

    asyncio.run(sup._step_worker())

    assert agent.router_calls == 1
    tasks = {t["id"]: t["status"] for t in run.read_tasks()["tasks"]}
    # the router-named task ran and completed; the deterministic pick
    # ("001") was never touched at all.
    assert tasks == {"001": "pending", "002": "completed"}
    events = _router_decision_events(run)
    assert len(events) == 1
    assert events[0]["state"] == "task_selection"
    assert events[0]["choice"] == "002"
    assert events[0]["deterministicPick"] == "001"
    assert events[0]["fallback"] is False
    assert events[0]["reason"] == "the second task blocks nothing else"


# ---------------------------------------------------------------------------
# Criterion 3: the three distinct fallback causes, each falling back to the
# deterministic pick with exactly one router call (no contract retry).
# ---------------------------------------------------------------------------

def test_an_unparseable_answer_falls_back_to_the_deterministic_pick(tmp_path):
    sup, run = _make_sup(tmp_path, [dict(t) for t in TWO_TASKS],
                         route_task_selection=True)
    agent = _RoutingAgent(run, router_answers=[_ok("not json at all")])
    sup.runner = agent  # type: ignore[assignment]

    asyncio.run(sup._step_worker())

    assert agent.router_calls == 1
    tasks = {t["id"]: t["status"] for t in run.read_tasks()["tasks"]}
    assert tasks == {"001": "completed", "002": "pending"}
    events = _router_decision_events(run)
    assert len(events) == 1
    assert events[0]["fallback"] is True
    assert events[0]["fallbackCause"] == "unparseable"
    assert events[0]["choice"] == "001"


def test_a_non_candidate_id_falls_back_to_the_deterministic_pick(tmp_path):
    sup, run = _make_sup(tmp_path, [dict(t) for t in TWO_TASKS],
                         route_task_selection=True)
    agent = _RoutingAgent(run, router_answers=[
        _ok(json.dumps({"candidate_id": "999", "reason": "not a real task"}))])
    sup.runner = agent  # type: ignore[assignment]

    asyncio.run(sup._step_worker())

    assert agent.router_calls == 1
    tasks = {t["id"]: t["status"] for t in run.read_tasks()["tasks"]}
    assert tasks == {"001": "completed", "002": "pending"}
    events = _router_decision_events(run)
    assert len(events) == 1
    assert events[0]["fallback"] is True
    assert events[0]["fallbackCause"] == "non_candidate_id"
    assert events[0]["choice"] == "001"


def test_an_answer_past_the_timeout_falls_back_to_the_deterministic_pick(tmp_path):
    sup, run = _make_sup(tmp_path, [dict(t) for t in TWO_TASKS],
                         route_task_selection=True, router_timeout_s=0.05)
    agent = _RoutingAgent(run, router_answers=[0.3])  # hangs past 0.05s
    sup.runner = agent  # type: ignore[assignment]

    asyncio.run(asyncio.wait_for(sup._step_worker(), timeout=10))

    assert agent.router_calls == 1
    tasks = {t["id"]: t["status"] for t in run.read_tasks()["tasks"]}
    assert tasks == {"001": "completed", "002": "pending"}
    events = _router_decision_events(run)
    assert len(events) == 1
    assert events[0]["fallback"] is True
    assert events[0]["fallbackCause"] == "timeout"
    assert events[0]["choice"] == "001"


# ---------------------------------------------------------------------------
# Criterion 4a: the SAME `router.decision` event shape on both routed
# states -- task_selection (above) and the pre-existing plan_review axis.
# ---------------------------------------------------------------------------

REQUIRED_ROUTER_DECISION_KEYS = {
    "state", "candidateIds", "deterministicPick", "choice", "reason",
    "fallback", "fallbackCause", "model", "tokens", "costUSD",
}

def test_router_decision_event_carries_the_same_keys_for_task_selection(tmp_path):
    sup, run = _make_sup(tmp_path, [dict(t) for t in TWO_TASKS],
                         route_task_selection=True)
    agent = _RoutingAgent(run, router_answers=[
        _ok(json.dumps({"candidate_id": "002", "reason": "picked"}))])
    sup.runner = agent  # type: ignore[assignment]

    asyncio.run(sup._step_worker())

    events = _router_decision_events(run)
    assert len(events) == 1
    assert REQUIRED_ROUTER_DECISION_KEYS <= set(events[0])
    assert events[0]["tokens"] == 15


def test_router_decision_event_carries_the_same_keys_for_plan_review(
        tmp_path, registered_ambiguous_hat):
    # The SAME registered-ambiguous-state fixture and harness
    # tests/test_router_engine.py itself uses -- proven there to drive the
    # ambiguous state's real `review` continuation end to end without
    # hanging on a malformed review-contract reply.
    from tests.test_router_engine import (
        AMBIGUOUS_STATE as ENGINE_AMBIGUOUS_STATE, _happy_handlers, _run)

    root = tmp_path / "run"
    sup, final, agent = _run(
        root, _happy_handlers(),
        router_answers=[_ok('{"next_step": "review"}')])
    assert final == "succeeded"
    assert agent.router_calls == 1

    events = _router_decision_events(sup.run)
    assert len(events) == 1
    assert REQUIRED_ROUTER_DECISION_KEYS <= set(events[0])
    assert events[0]["state"] == ENGINE_AMBIGUOUS_STATE
    assert events[0]["choice"] == "review"


# ---------------------------------------------------------------------------
# Criterion 4b: `ralphctl cost` shows the router's own spend as its own
# phase, on the task-selection axis exactly like it already does for
# plan_review (tests/test_router_engine.py::
# test_router_usage_is_accounted_under_its_own_phase_bucket).
# ---------------------------------------------------------------------------

def test_ralphctl_cost_shows_router_spend_as_its_own_phase(tmp_path, capsys):
    sup, run = _make_sup(tmp_path, [dict(t) for t in TWO_TASKS],
                         route_task_selection=True)
    agent = _RoutingAgent(run, router_answers=[
        _ok(json.dumps({"candidate_id": "002", "reason": "picked"}))])
    sup.runner = agent  # type: ignore[assignment]

    asyncio.run(sup._step_worker())

    registry = run.root.parent.parent
    env = {**os.environ, "RALPHD_REGISTRY": str(registry)}
    result = call_ralphctl_in_process(
        ["--json", "cost", sup.cfg.run_id], env=env, capsys=capsys)
    assert result.returncode == 0, result.stderr
    doc = json.loads(result.stdout)
    phase_keys = {b["key"] for b in doc["byPhase"]}
    assert "router" in phase_keys
