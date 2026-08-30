"""Task 018 (requirement E, #60 pillar 2): the router wired into a real
`LoopSupervisor` via `_step_generic_hat`/`_ask_router`, driven end to end
through the real dispatch loop with a phase-dispatching scripted runner (no
subprocess, milliseconds -- the same technique as
tests/test_transition_executor_cutover.py / tests/test_hat_extensibility.py).

tests/test_router.py already proves `engine.router.decide()` itself in
isolation. This module proves the ENGINE side of the acceptance text:

- deterministic-first, measured over the REAL table (planning/worker/
  review/replan): zero calls into `_ask_router` for a whole ordinary run;
- a genuinely ambiguous (synthetic, task-015-style) state is resolved
  through the router, honouring a valid answer and falling back on an
  exhausted/invalid/timed-out one, never accepting an illegal transition;
- router calls are visible in `usage.byPhase["router"]` and never bump
  `self.iterations_used` -- "not charged as work iterations", by
  construction, checked from the run's own recorded state.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from ralphd.engine import transitions as tr
from ralphd.engine.config import JobConfig
from ralphd.engine.loop import LoopSupervisor
from ralphd.engine.runner import IterationResult
from ralphd.engine.state import RunDir, atomic_write_json, read_events

AMBIGUOUS_STATE = "task018_ambiguous_engine_state"
AMBIGUOUS_HAT = "task018_ambiguous_engine_hat"


def _ok(text: str) -> IterationResult:
    r = IterationResult(exit_code=0)
    r.final_text = text
    r.duration_s = 5.0
    r.usage = {"input": 10, "output": 5, "totalTokens": 15}
    return r


class _ScriptedAgent:
    """Same technique as test_hat_extensibility._ScriptedAgent, widened to
    also answer router calls: `_ask_router` writes its transcript under
    `artifacts/router/`, which is how this stub tells a router question
    apart from a real hat iteration without needing any extra plumbing in
    the engine itself.
    """

    def __init__(self, sup: LoopSupervisor, handlers: dict,
                 router_answers: list | None = None):
        self.sup = sup
        self.handlers = handlers
        self.router_answers = list(router_answers or [])
        self.router_calls = 0
        self.phases: list[str] = []
        self.running = False

    def interrupt(self) -> bool:
        return self.running

    async def run(self, prompt, transcript, **kw) -> IterationResult:
        self.running = True
        try:
            if Path(transcript).parent.name == "router":
                self.router_calls += 1
                if self.router_calls <= len(self.router_answers):
                    return self.router_answers[self.router_calls - 1]
                raise AssertionError(
                    "router called more times than the test scripted "
                    f"({self.router_calls} calls, "
                    f"{len(self.router_answers)} answers provided)")
            phase = self.sup.run.read_status().get("phase")
            self.phases.append(phase)
            return self.handlers[phase](self.sup, prompt)
        finally:
            self.running = False


def _plan(sup: LoopSupervisor, prompt: str = "") -> IterationResult:
    atomic_write_json(sup.run.root / "plan-proposal.json", {"version": 1, "tasks": [
        {"id": "001", "title": "do the thing",
         "successCriteria": "the thing is done"},
    ]})
    return _ok("planned\n" +
              json.dumps({"proposal_path": "plan-proposal.json", "summary": "planned"}))


def _complete_task(sup: LoopSupervisor, prompt: str = "") -> IterationResult:
    # Task 015 (requirement O, #100): report the harness-owned `complete`
    # verdict for the task the scheduler assigned, instead of writing
    # tasks.json directly and ending with the now-retired
    # `<promise>COMPLETE</promise>` marker (see
    # tests/test_transition_executor_cutover.py's `_complete_task`, the same
    # fixture shape).
    task_id = sup.run.read_tasks()["tasks"][0]["id"]
    return _ok("done\n" + json.dumps({"verdict": "complete", "task_id": task_id}))


def _review_verified(sup: LoopSupervisor, prompt: str = "") -> IterationResult:
    # Task 119 (#60 pillar 3, fact 9): review reports through the `review`
    # JSON contract, not the retired `<promise>VERIFIED</promise>` sentinel.
    return _ok("looks right\n" + json.dumps({"verdict": "verified"}))


def _ambiguous_ok(sup: LoopSupervisor, prompt: str = "") -> IterationResult:
    return _ok("(deciding nothing myself)\n{}")


def _happy_handlers() -> dict:
    return {"planning": _plan, "worker": _complete_task,
            "review": _review_verified, AMBIGUOUS_STATE: _ambiguous_ok}


@pytest.fixture
def registered_ambiguous_hat(monkeypatch, tmp_path):
    """Registers a genuinely router-ambiguous state: two legal edges
    (`planning`, `review`), a hat, a schema and a declared
    `transitions.ROUTER_FALLBACK` -- the ADDITIONAL declaration task 018
    requires beside task 015's table entry / hat / schema, for exactly the
    case task 015 shipped without (a hat whose transition depends on WHAT
    it reported)."""
    prompts_dir = tmp_path / "config" / "prompts"
    prompts_dir.mkdir(parents=True)
    (prompts_dir / f"{AMBIGUOUS_HAT}.md").write_text(
        "You are a router-ambiguous test hat (task 018 fixture). Reply "
        "with `{}` as the last line of your final message.\n")
    from ralphd.engine import config as config_mod
    monkeypatch.setattr(config_mod, "CONFIG_DIR", tmp_path / "config")

    assert AMBIGUOUS_STATE not in tr.TABLE, "fixture name collided with a real state"
    original_entry = tr.ENTRY_STATE
    tr.TABLE[AMBIGUOUS_STATE] = (tr.T("planning", "picked_planning"),
                                 tr.T("review", "picked_review"))
    tr.STATE_HAT[AMBIGUOUS_STATE] = AMBIGUOUS_HAT
    tr.HAT_SCHEMAS[AMBIGUOUS_HAT] = {
        "type": "object", "properties": {}, "additionalProperties": False,
    }
    tr.ROUTER_FALLBACK[AMBIGUOUS_STATE] = "picked_planning"
    tr.ENTRY_STATE = AMBIGUOUS_STATE
    try:
        yield
    finally:
        tr.ENTRY_STATE = original_entry
        tr.TABLE.pop(AMBIGUOUS_STATE, None)
        tr.STATE_HAT.pop(AMBIGUOUS_STATE, None)
        tr.HAT_SCHEMAS.pop(AMBIGUOUS_HAT, None)
        tr.ROUTER_FALLBACK.pop(AMBIGUOUS_STATE, None)


def _run(root: Path, handlers: dict, router_answers=None, **cfg_kw):
    root.mkdir(parents=True, exist_ok=True)
    run = RunDir(root=root)
    run.update_status(state="starting")
    kw = {"iterations": 15, "max_approaches": 1, "vigilant": False,
          "on_complete": "exit", "infra_retry_backoff_s": [0.0],
          "infra_retry_backoff_max_s": 0.0, "infra_outage_budget_s": 1000.0,
          "contract_retry_max": 2, "router_timeout_s": 5.0,
          **cfg_kw}
    sup = LoopSupervisor(JobConfig(run_id="task018-router", **kw), run, root)
    agent = _ScriptedAgent(sup, handlers, router_answers)
    sup.runner = agent  # type: ignore[assignment]

    async def no_backoff(seconds):
        return seconds, False

    sup._wait_out_backoff = no_backoff  # type: ignore[method-assign]
    return sup, asyncio.run(sup._run_job_core()), agent


# ---------------------------------------------------------------------------
# 1. Deterministic-first, over the REAL table: a whole ordinary run never
#    calls the router at all.
# ---------------------------------------------------------------------------

def test_an_ordinary_run_over_the_real_table_never_calls_the_router(tmp_path):
    def _poisoned_ask(prompt):
        raise AssertionError(
            "the router was called for a state the four hand-written "
            "_step_* methods decide without a model")

    root = tmp_path / "run"
    root.mkdir(parents=True)
    run = RunDir(root=root)
    run.update_status(state="starting")
    sup = LoopSupervisor(JobConfig(
        run_id="task018-no-router", iterations=15, max_approaches=1,
        vigilant=False, on_complete="exit", infra_retry_backoff_s=[0.0],
        infra_retry_backoff_max_s=0.0, infra_outage_budget_s=1000.0), run, root)
    agent = _ScriptedAgent(sup, _happy_handlers())
    sup.runner = agent  # type: ignore[assignment]
    sup._ask_router = _poisoned_ask  # type: ignore[method-assign]

    async def no_backoff(seconds):
        return seconds, False
    sup._wait_out_backoff = no_backoff  # type: ignore[method-assign]

    final = asyncio.run(sup._run_job_core())
    assert final == "succeeded"
    assert agent.phases == ["planning", "worker", "review"]


# ---------------------------------------------------------------------------
# 2. The ambiguous state, resolved by a valid router answer.
# ---------------------------------------------------------------------------

def test_a_valid_router_answer_is_honoured(tmp_path, registered_ambiguous_hat):
    root = tmp_path / "run"
    sup, final, agent = _run(
        root, _happy_handlers(),
        router_answers=[_ok('{"next_step": "review"}')])
    assert final == "succeeded"
    assert agent.router_calls == 1
    assert agent.phases[0] == AMBIGUOUS_STATE
    # the router picked "review" directly -- "planning"/"worker" never ran.
    assert agent.phases[1:] == ["review"]


# ---------------------------------------------------------------------------
# 3. An invalid answer re-prompts; exhausting the retry budget falls back
#    to the state's OWN declared deterministic choice -- never an illegal
#    transition, whatever the model said.
# ---------------------------------------------------------------------------

def test_invalid_router_answers_fall_back_to_the_declared_choice(tmp_path, registered_ambiguous_hat):
    root = tmp_path / "run"
    bad = _ok("not json at all")
    sup, final, agent = _run(
        root, _happy_handlers(),
        router_answers=[bad, bad, bad],  # 1 + contract_retry_max(2) attempts
        contract_retry_max=2)
    assert final == "succeeded"
    assert agent.router_calls == 3
    # fell back to "picked_planning" -> the ordinary planning/worker/review
    # continuation, not "review" (the OTHER legal edge).
    assert agent.phases[0] == AMBIGUOUS_STATE
    assert agent.phases[1:] == ["planning", "worker", "review"]
    events = [e for e in read_events(sup.run.root) if e.get("type") == "log"]
    assert any("FELL BACK" in (e.get("message") or "") for e in events)


def test_an_illegal_router_target_is_never_accepted(tmp_path, registered_ambiguous_hat):
    """The router names a real state that is simply not one of THIS
    state's own legal edges -- must be refused exactly like malformed JSON,
    never taken."""
    root = tmp_path / "run"
    foreign = _ok('{"next_step": "worker"}')  # a real state, wrong edge
    sup, final, agent = _run(
        root, _happy_handlers(),
        router_answers=[foreign, foreign, foreign], contract_retry_max=2)
    assert final == "succeeded"
    assert agent.phases[1:] == ["planning", "worker", "review"]  # the fallback


def test_a_hanging_router_call_cannot_stall_the_loop(tmp_path, registered_ambiguous_hat):
    root = tmp_path / "run"
    root.mkdir(parents=True)
    run = RunDir(root=root)
    run.update_status(state="starting")
    sup = LoopSupervisor(JobConfig(
        run_id="task018-timeout", iterations=15, max_approaches=1,
        vigilant=False, on_complete="exit", infra_retry_backoff_s=[0.0],
        infra_retry_backoff_max_s=0.0, infra_outage_budget_s=1000.0,
        contract_retry_max=0, router_timeout_s=0.05), run, root)
    agent = _ScriptedAgent(sup, _happy_handlers())
    sup.runner = agent  # type: ignore[assignment]

    async def hanging_ask(prompt):
        await asyncio.sleep(3600)
        return '{"next_step": "review"}', {}

    sup._ask_router = hanging_ask  # type: ignore[method-assign]

    async def no_backoff(seconds):
        return seconds, False
    sup._wait_out_backoff = no_backoff  # type: ignore[method-assign]

    final = asyncio.run(asyncio.wait_for(sup._run_job_core(), timeout=10))
    assert final == "succeeded"
    assert agent.phases[1:] == ["planning", "worker", "review"]  # fell back


# ---------------------------------------------------------------------------
# 4. Accounting: router calls show up in usage.byPhase["router"], and never
#    increment self.iterations_used -- checked from the run's own recorded
#    state, not just from the mechanism's isolated unit tests.
# ---------------------------------------------------------------------------

def test_router_calls_are_not_charged_as_work_iterations(tmp_path, registered_ambiguous_hat):
    root = tmp_path / "run"
    bad = _ok("not json at all")
    sup, final, agent = _run(
        root, _happy_handlers(),
        router_answers=[bad, bad, bad], contract_retry_max=2)
    assert final == "succeeded"
    assert agent.router_calls == 3
    # Exactly 4 REAL iterations ran (the ambiguous hat, then
    # planning/worker/review) -- the 3 router attempts cost none of them.
    assert sup.iterations_used == 4
    status = sup.run.read_status()
    assert status["iterationsUsed"] == 4


def test_router_usage_is_accounted_under_its_own_phase_bucket(tmp_path, registered_ambiguous_hat):
    root = tmp_path / "run"
    sup, final, agent = _run(
        root, _happy_handlers(),
        router_answers=[_ok('{"next_step": "review"}')])
    assert final == "succeeded"
    status = sup.run.read_status()
    by_phase = status["usage"]["byPhase"]
    router_bucket = by_phase.get("router")
    assert router_bucket is not None
    assert router_bucket["totalTokens"] == 15
    # the hat's own iteration usage landed under its OWN phase bucket, kept
    # separate from the router's -- both are real, distinct buckets.
    hat_bucket = by_phase.get(AMBIGUOUS_STATE)
    assert hat_bucket is not None
    assert hat_bucket["totalTokens"] == 15
