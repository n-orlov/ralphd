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


def _plan_review_edges():
    from ralphd.engine import transitions
    return transitions.successors("plan_review", False, peer_plan_review=True)


def test_router_decision_event_carries_the_same_keys_for_plan_review(tmp_path):
    """The existing `plan_review` entry (`transitions.ROUTER_FALLBACK`),
    routed through `LoopSupervisor._route_with_router` over plan_review's
    REAL peer-gate edges -- the state name, the edge set and the declared
    fallback are all plan_review's own, never a synthetic fixture state."""
    from ralphd.engine import transitions
    sup, run = _make_sup(tmp_path, [dict(TWO_TASKS[0])],
                         peer_plan_review=True)
    agent = _RoutingAgent(run, router_answers=[
        _ok(json.dumps({"next_step": "planning"}))])
    sup.runner = agent  # type: ignore[assignment]

    edges = _plan_review_edges()
    target, guard, _outcome = asyncio.run(sup._route_with_router(
        "plan_review", edges, peer_plan_review=True))

    assert agent.router_calls == 1
    assert (target, guard) == (
        "planning", "plan_review_objected_revision_available")
    events = _router_decision_events(run)
    assert len(events) == 1
    event = events[0]
    assert REQUIRED_ROUTER_DECISION_KEYS <= set(event)
    assert event["state"] == "plan_review"
    assert event["candidateIds"] == sorted({e.target for e in edges})
    fallback_target = next(
        e.target for e in edges
        if e.guard == transitions.fallback_guard("plan_review"))
    assert event["deterministicPick"] == fallback_target == "worker"
    assert event["choice"] == "planning"
    assert event["fallback"] is False
    assert event["fallbackCause"] is None
    assert event["model"] == sup.cfg.model_for("router")
    assert event["tokens"] == 15


def test_a_plan_review_router_fallback_emits_its_cause(tmp_path):
    """Same plan_review entry; an answer that never validates falls back to
    the declared `plan_review_revision_bound_reached` edge, and the event
    says so."""
    sup, run = _make_sup(tmp_path, [dict(TWO_TASKS[0])],
                         peer_plan_review=True, contract_retry_max=0)
    agent = _RoutingAgent(run, router_answers=[_ok("no json here")])
    sup.runner = agent  # type: ignore[assignment]

    target, guard, _outcome = asyncio.run(sup._route_with_router(
        "plan_review", _plan_review_edges(), peer_plan_review=True))

    assert (target, guard) == ("worker", "plan_review_revision_bound_reached")
    events = _router_decision_events(run)
    assert len(events) == 1
    assert events[0]["state"] == "plan_review"
    assert events[0]["fallback"] is True
    assert events[0]["fallbackCause"] == "unparseable"
    assert events[0]["choice"] == events[0]["deterministicPick"] == "worker"


def test_router_decision_event_carries_the_same_keys_for_a_generic_ambiguous_state(
        tmp_path, registered_ambiguous_hat):
    # `_step_generic_hat`'s own call into `_route_with_router`, end to end,
    # over the registered-ambiguous-state fixture tests/test_router_engine.py
    # itself uses -- the same helper both routed-state tests above share.
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


# ---------------------------------------------------------------------------
# #289: the router panel / `ralphctl router` list task_selection calls.
# `state.router_call_entries` used to read only `decisions.jsonl`, which only
# the edge-based axis writes; the task_selection axis lives in `events.jsonl`.
# ---------------------------------------------------------------------------

def _write_decision_line(run: RunDir, ts: str, target: str = "worker") -> None:
    run.record_router_decision({
        "state": "plan_review", "vigilant": False,
        "candidateEdges": [{"target": target, "guard": "g"}],
        "chosenTarget": target, "chosenGuard": "g", "usedRouter": True,
        "usedFallback": False, "fallbackGuard": "g", "attempts": 1,
        "model": "m", "usage": {"totalTokens": 3}, "transcripts": ["0001.jsonl"]})
    # `record_router_decision` stamps "now"; pin the ts so ordering is testable.
    path = run.artifacts_dir / "router" / "decisions.jsonl"
    rows = [json.loads(x) for x in path.read_text().splitlines()]
    rows[-1]["ts"] = ts
    path.write_text("".join(json.dumps(r) + "\n" for r in rows))


def _append_event(run: RunDir, ts: str, **fields) -> None:
    with open(run.root / "events.jsonl", "a") as f:
        f.write(json.dumps({"ts": ts, "type": "router.decision", **fields}) + "\n")


def _ts_event(choice: str, **extra) -> dict:
    return {"state": "task_selection", "candidateIds": ["001", "002"],
            "deterministicPick": "001", "choice": choice, "reason": "why",
            "fallback": False, "fallbackCause": None, "model": "m",
            "tokens": 15, "costUSD": None, **extra}


def test_task_selection_events_are_listed_as_router_calls(tmp_path):
    from ralphd.engine import state as state_mod
    sup, run = _make_sup(tmp_path, [dict(t) for t in TWO_TASKS],
                         route_task_selection=True)
    sup.runner = _RoutingAgent(run, router_answers=[
        _ok(json.dumps({"candidate_id": "002", "reason": "picked"}))])  # type: ignore[assignment]
    asyncio.run(sup._step_worker())

    entries = state_mod.router_call_entries(run.root)
    assert len(entries) == 1
    e = entries[0]
    assert (e["index"], e["state"], e["chosenTarget"]) == (1, "task_selection", "002")
    assert e["taskCandidateIds"] == ["001", "002"]
    assert e["deterministicPick"] == "001" and e["reason"] == "picked"
    assert e["usedFallback"] is False and e["chargesIteration"] is False
    assert e["tokens"] == 15
    # the going-forward half: the call's own transcript is named
    assert e["transcripts"] == ["0001.jsonl"]
    text = "\n".join(state_mod.router_call_summary_lines(e))
    assert "002" in text and "picked" in text and "0001.jsonl" in text
    assert "candidates:    001, 002" in text
    # decisions.jsonl stays edge-axis-only
    assert not (run.artifacts_dir / "router" / "decisions.jsonl").exists()


def test_edge_based_calls_are_not_listed_twice(tmp_path):
    """`_route_with_router` writes a decisions line AND a router.decision
    event; only the decisions line may become an entry."""
    from ralphd.engine import state as state_mod
    sup, run = _make_sup(tmp_path, [dict(TWO_TASKS[0])], peer_plan_review=True)
    sup.runner = _RoutingAgent(run, router_answers=[
        _ok(json.dumps({"next_step": "planning"}))])  # type: ignore[assignment]
    asyncio.run(sup._route_with_router(
        "plan_review", _plan_review_edges(), peer_plan_review=True))

    assert len(_router_decision_events(run)) == 1
    entries = state_mod.router_call_entries(run.root)
    assert [e["state"] for e in entries] == ["plan_review"]


def test_mixed_axes_merge_chronologically(tmp_path):
    from ralphd.cli import ui_server
    from ralphd.engine import state as state_mod
    _, run = _make_sup(tmp_path, [dict(TWO_TASKS[0])])
    _append_event(run, "2026-01-01T00:00:03Z", **_ts_event("002"))
    _write_decision_line(run, "2026-01-01T00:00:02Z")
    _append_event(run, "2026-01-01T00:00:01Z", **_ts_event("001"))
    # the edge axis's own event twin must be ignored
    _append_event(run, "2026-01-01T00:00:02Z", state="plan_review",
                  candidateIds=["worker"], choice="worker")

    entries = state_mod.router_call_entries(run.root)
    assert [(e["index"], e["state"], e["chosenTarget"]) for e in entries] == [
        (1, "task_selection", "001"), (2, "plan_review", "worker"),
        (3, "task_selection", "002")]
    view = ui_server.router_calls_view(run.root.parent.parent, run.root.name)
    assert [c["index"] for c in view["routerCalls"]] == [1, 2, 3]
    assert view["notice"] == ""


def test_a_task_selection_fallback_entry_says_why(tmp_path):
    from ralphd.engine import state as state_mod
    _, run = _make_sup(tmp_path, [dict(TWO_TASKS[0])])
    _append_event(run, "2026-01-01T00:00:01Z", **_ts_event(
        "001", fallback=True, fallbackCause="timeout", reason=""))
    (e,) = state_mod.router_call_entries(run.root)
    assert e["usedFallback"] is True and e["fallbackCause"] == "timeout"
    assert "cause=timeout" in "\n".join(state_mod.router_call_summary_lines(e))


def test_router_decision_event_carries_the_derived_cost(tmp_path):
    """#289: on a derived-pricing run the provider quotes no `costUSD`; the
    event carries the call's `costDerivedUSD` (set by the runner from the
    pricing map and accumulated by `_accumulate_usage`) in its own field."""
    from ralphd.engine import state as state_mod
    sup, run = _make_sup(tmp_path, [dict(t) for t in TWO_TASKS],
                         route_task_selection=True)
    derived = _ok(json.dumps({"candidate_id": "002", "reason": "picked"}))
    derived.usage = {"input": 10, "output": 5, "totalTokens": 15,
                     "costPriced": False, "costDerived": True,
                     "costDerivedUSD": 0.000123}
    sup.runner = _RoutingAgent(run, router_answers=[derived])  # type: ignore[assignment]
    asyncio.run(sup._step_worker())

    (ev,) = _router_decision_events(run)
    assert ev["costUSD"] is None
    assert ev["costDerivedUSD"] == 0.000123
    (entry,) = state_mod.router_call_entries(run.root)
    assert entry["costSource"] == "derived"
    assert "0.0001" in entry["costDisplay"]
