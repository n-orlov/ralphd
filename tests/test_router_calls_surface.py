"""Task 022 (P, #80): router calls are now TRACEABLE -- a hub region and a
`ralphctl` command list this run's router decisions -- without being
charged as work iterations.

`transitions.ROUTER_FALLBACK` is `{}` by design (transitions.py:310-323) and
no shipped state has ever reached a genuine multi-edge choice
(`tests/test_router_engine.py::
test_an_ordinary_run_over_the_real_table_never_calls_the_router` pins this
directly), so this module proves the new surface the ONLY honest way: by
registering a synthetic router-ambiguous state -- the exact "no-op hat
fixture" technique `tests/test_router_engine.py`'s `registered_ambiguous_hat`
already established for #60 pillar 2 -- driving one real router decision
through a real `LoopSupervisor`, and reading it back off BOTH surfaces this
task adds:

- `state.router_call_entries()` / `ralphctl router`'s own reader;
- `ui_server.router_calls_view()`, the hub's reader (same shaped entries,
  never a second, independently-worded copy).

Never by hunting for a live router call: none has ever occurred, and this
module does not pretend otherwise.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from ralphd.cli import ui_server
from ralphd.engine import state as state_mod
from ralphd.engine import transitions as tr
from ralphd.engine.config import JobConfig
from ralphd.engine.loop import LoopSupervisor
from ralphd.engine.runner import COMPLETE, IterationResult
from ralphd.engine.state import RunDir, atomic_write_json

AMBIGUOUS_STATE = "task022_ambiguous_engine_state"
AMBIGUOUS_HAT = "task022_ambiguous_engine_hat"


def _ok(text: str, usage: dict | None = None) -> IterationResult:
    r = IterationResult(exit_code=0)
    r.final_text = text
    r.duration_s = 5.0
    r.usage = usage if usage is not None else {"input": 10, "output": 5,
                                                "totalTokens": 15}
    return r


class _ScriptedAgent:
    """Same technique as test_router_engine.py's `_ScriptedAgent`: a router
    question is told apart from a real hat iteration by the transcript's own
    parent directory name (`router`), never by extra plumbing."""

    def __init__(self, sup, handlers, router_answers=None):
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
            if transcript.parent.name == "router":
                self.router_calls += 1
                if self.router_calls <= len(self.router_answers):
                    return self.router_answers[self.router_calls - 1]
                raise AssertionError("router called more times than scripted")
            phase = self.sup.run.read_status().get("phase")
            self.phases.append(phase)
            return self.handlers[phase](self.sup, prompt)
        finally:
            self.running = False


def _plan(sup, prompt: str = "") -> IterationResult:
    atomic_write_json(sup.run.root / "plan-proposal.json", {"version": 1, "tasks": [
        {"id": "001", "title": "do the thing", "successCriteria": "done"},
    ]})
    return _ok("planned\n" +
              json.dumps({"proposal_path": "plan-proposal.json", "summary": "planned"}))


def _complete_task(sup, prompt: str = "") -> IterationResult:
    doc = sup.run.read_tasks()
    doc["tasks"][0]["status"] = "completed"
    atomic_write_json(sup.run.tasks_file, doc)
    return _ok(f"done\n{COMPLETE}")


def _review_verified(sup, prompt: str = "") -> IterationResult:
    return _ok("looks right\n" + json.dumps({"verdict": "verified"}))


def _ambiguous_ok(sup, prompt: str = "") -> IterationResult:
    return _ok("(deciding nothing myself)\n{}")


def _happy_handlers() -> dict:
    return {"planning": _plan, "worker": _complete_task,
            "review": _review_verified, AMBIGUOUS_STATE: _ambiguous_ok}


@pytest.fixture
def registered_ambiguous_hat(monkeypatch, tmp_path):
    """Registers a genuinely router-ambiguous state: two legal edges
    (`planning`, `review`), a hat, a schema and a declared
    `transitions.ROUTER_FALLBACK` -- mirrors `test_router_engine.py`'s
    fixture of the same shape (a different state/hat name so the two
    modules never collide when run together)."""
    prompts_dir = tmp_path / "config" / "prompts"
    prompts_dir.mkdir(parents=True)
    (prompts_dir / f"{AMBIGUOUS_HAT}.md").write_text(
        "You are a router-ambiguous test hat (task 022 fixture). Reply "
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


def _run(root, handlers, router_answers=None, **cfg_kw):
    root.mkdir(parents=True, exist_ok=True)
    run = RunDir(root=root)
    run.update_status(state="starting")
    kw = {"iterations": 15, "max_approaches": 1, "vigilant": False,
          "on_complete": "exit", "infra_retry_backoff_s": [0.0],
          "infra_retry_backoff_max_s": 0.0, "infra_outage_budget_s": 1000.0,
          "contract_retry_max": 2, "router_timeout_s": 5.0,
          **cfg_kw}
    sup = LoopSupervisor(JobConfig(run_id="task022-router", **kw), run, root)
    agent = _ScriptedAgent(sup, handlers, router_answers)
    sup.runner = agent  # type: ignore[assignment]

    async def no_backoff(seconds):
        return seconds, False

    sup._wait_out_backoff = no_backoff  # type: ignore[method-assign]
    return sup, asyncio.run(sup._run_job_core()), agent


# ---------------------------------------------------------------------------
# 1. A valid router answer lands one entry on BOTH surfaces, with the right
#    fields, and never bumps iterations_used.
# ---------------------------------------------------------------------------

def test_a_router_decision_appears_on_both_surfaces_and_charges_no_iteration(
        tmp_path, registered_ambiguous_hat):
    reg = tmp_path / "registry"
    run_id = "task022-run"
    root = reg / "runs" / run_id
    sup, final, agent = _run(
        root, _happy_handlers(),
        router_answers=[_ok('{"next_step": "review"}',
                            usage={"input": 7, "output": 3, "totalTokens": 10})])
    assert final == "succeeded"
    assert agent.router_calls == 1
    # the ambiguous hat's own iteration, then straight to "review" -- the
    # router picked it directly, "planning"/"worker" never ran.
    assert agent.phases == [AMBIGUOUS_STATE, "review"]

    # -- router calls consume no iteration and no approach ---------------
    assert sup.iterations_used == 2  # the ambiguous hat + review, nothing more
    status = sup.run.read_status()
    assert status["iterationsUsed"] == 2

    # -- surface 1: state.router_call_entries (ralphctl router's reader) -
    entries = state_mod.router_call_entries(sup.run.root)
    assert len(entries) == 1
    entry = entries[0]
    assert entry["index"] == 1
    assert entry["state"] == AMBIGUOUS_STATE
    assert entry["chosenTarget"] == "review"
    assert entry["chosenGuard"] == "picked_review"
    assert entry["usedRouter"] is True
    assert entry["usedFallback"] is False
    assert entry["fallbackGuard"] == "picked_planning"
    assert entry["attempts"] == 1
    assert {"target": "planning", "guard": "picked_planning"} in entry["candidateEdges"]
    assert {"target": "review", "guard": "picked_review"} in entry["candidateEdges"]
    assert entry["tokens"] == 10
    assert entry["chargesIteration"] is False
    assert "no iteration" in entry["note"] and "no approach" in entry["note"]

    # -- surface 2: the hub's own reader (ui_server.router_calls_view) ---
    hub_view = ui_server.router_calls_view(reg, run_id)
    assert hub_view["routerCalls"] == entries
    assert hub_view["notice"] == ""

    # -- the CLI's empty-run wording is never shown for a run that DID have
    #    a decision recorded.
    assert state_mod.NO_ROUTER_CALLS not in json.dumps(hub_view)


# ---------------------------------------------------------------------------
# 2. A decision that fell back is recorded with usedFallback=true and the
#    declared fallback guard -- the surface never has to guess which
#    branch fired.
# ---------------------------------------------------------------------------

def test_a_fallback_decision_is_recorded_as_such(tmp_path, registered_ambiguous_hat):
    reg = tmp_path / "registry"
    run_id = "task022-fallback-run"
    root = reg / "runs" / run_id
    bad = _ok("not json at all")
    sup, final, agent = _run(
        root, _happy_handlers(),
        router_answers=[bad, bad, bad], contract_retry_max=2)
    assert final == "succeeded"
    entries = state_mod.router_call_entries(sup.run.root)
    assert len(entries) == 1
    entry = entries[0]
    assert entry["usedFallback"] is True
    assert entry["chosenTarget"] == "planning"
    assert entry["chosenGuard"] == "picked_planning"
    assert entry["fallbackGuard"] == "picked_planning"
    assert entry["attempts"] == 3
    hub_view = ui_server.router_calls_view(reg, run_id)
    assert hub_view["routerCalls"][0]["usedFallback"] is True


# ---------------------------------------------------------------------------
# 3. A run that never reaches an ambiguous state -- every shipped run,
#    today -- answers with an empty list plus the explicit notice, on both
#    surfaces.
# ---------------------------------------------------------------------------

def test_a_run_with_no_router_calls_answers_with_the_empty_notice(tmp_path):
    reg = tmp_path / "registry"
    run_id = "task022-ordinary-run"
    root = reg / "runs" / run_id
    root.mkdir(parents=True)
    run = RunDir(root=root)
    run.update_status(state="starting")
    sup = LoopSupervisor(JobConfig(
        run_id="task022-no-router", iterations=15, max_approaches=1,
        vigilant=False, on_complete="exit", infra_retry_backoff_s=[0.0],
        infra_retry_backoff_max_s=0.0, infra_outage_budget_s=1000.0), run, root)
    agent = _ScriptedAgent(sup, _happy_handlers())
    sup.runner = agent  # type: ignore[assignment]

    async def no_backoff(seconds):
        return seconds, False
    sup._wait_out_backoff = no_backoff  # type: ignore[method-assign]

    final = asyncio.run(sup._run_job_core())
    assert final == "succeeded"
    assert agent.router_calls == 0
    assert state_mod.router_call_entries(sup.run.root) == []
    hub_view = ui_server.router_calls_view(reg, run_id)
    assert hub_view["routerCalls"] == []
    assert hub_view["notice"] == state_mod.NO_ROUTER_CALLS
