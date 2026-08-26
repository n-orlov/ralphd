"""Task 016 (#68 half 2, requirements I/J): the resolver is applied at the
ONE dispatch call site, and non-task hats provably stay static.

`LoopSupervisor._run_iteration_once` (loop.py) is the single place a
phase's model is resolved through `tiers.resolve_bundle`/`tiers.
default_host_map` -- every other subprocess-model call in the module is
the router's own tiny, uncounted `cfg.model_for("router")` in `_ask_router`
(pre-existing, task 018), never a second resolution pathway.

Four things this module proves by actually driving the real step methods
(fake `sup.runner`, real state machine -- the standing "stub sup.runner,
never _run_iteration_once" rule), never by reading the code and assuming:

1. There is exactly one call site for `tiers.resolve_bundle`/`tiers.
   default_host_map` in the whole `src/ralphd` tree, and exactly two
   subprocess-dispatch sites (`runner.run(...)`) in loop.py -- the
   dispatch call site plus the router's.
2. Worker and per-task verify carry the IN-HAND task's tier
   (`state.task_effective_tier`/`state.task_verify_tier`) through that
   call site.
3. Planning, reflect and the run-ending review ALWAYS resolve their
   ladder's designated tier (`tiers.DEFAULT_TIER` / `state.
   run_level_verify_tier()`) no matter what tiers the plan's tasks carry.
4. A steering-injected task (no `tier` annotation at all) starts at
   `normal` and escalates through the SAME worker-claim code path as any
   annotated task -- no special case.
"""

from __future__ import annotations

import asyncio
import json
import pathlib

from ralphd.engine import loop as loop_module
from ralphd.engine import state
from ralphd.engine import tiers as T
from ralphd.engine.config import JobConfig
from ralphd.engine.runner import IterationResult
from ralphd.engine.state import RunDir, atomic_write_json


def _make_sup(tmp_path, tasks, **cfg_kw):
    root = tmp_path / "run"
    root.mkdir(parents=True, exist_ok=True)
    run = RunDir(root=root)
    run.update_status(state="running")
    atomic_write_json(run.tasks_file, {"version": 1, "tasks": tasks})
    kw = {"iterations": 5, "max_approaches": 1, "vigilant": False,
          "on_complete": "exit", "infra_retry_backoff_s": [0.0],
          "infra_retry_backoff_max_s": 0.0, "infra_outage_budget_s": 1000.0,
          **cfg_kw}
    cfg = JobConfig(run_id="task016-tier-dispatch", **kw)
    sup = loop_module.LoopSupervisor(cfg, run, root)
    return sup, run


def _spy_resolve_bundle(monkeypatch):
    """Wraps the REAL `tiers.resolve_bundle` -- never replaces its
    behaviour -- and records every `(hat, tier)` pair `_run_iteration_once`
    hands it, in call order."""
    calls: list[tuple[str, str]] = []
    real = T.resolve_bundle

    def spy(hat, tier, host_map):
        calls.append((hat, tier))
        return real(hat, tier, host_map)

    monkeypatch.setattr(loop_module.tiers, "resolve_bundle", spy)
    return calls


class _Agent:
    """A minimal fake `sup.runner`. `task_id`/`verdict` shape the last
    line's JSON contract payload when the caller under test needs one
    (worker/verify); with neither set, a bare non-JSON success is enough
    (planning/reflect/review never parse a payload inside the methods this
    module drives directly)."""

    def __init__(self, task_id: str | None = None, verdict: str | None = None,
                 hat: str | None = None):
        self.task_id = task_id
        self.verdict = verdict
        self.hat = hat

    def interrupt(self):
        return False

    async def run(self, prompt, transcript, **kw):
        r = IterationResult(exit_code=0)
        if self.task_id and self.verdict:
            payload = {"task_id": self.task_id, "verdict": self.verdict}
            if self.verdict == "rejected":
                payload["reason"] = "no"
            r.final_text = json.dumps(payload)
        else:
            r.final_text = "ok"
        r.duration_s = 0.05
        r.usage = {"input": 1, "output": 1, "totalTokens": 2}
        return r


# ---------------------------------------------------------------------------
# 1. Exactly one dispatch call site, no second provisioning pathway
# ---------------------------------------------------------------------------

def _grep_calls(src_dir: pathlib.Path, symbol: str) -> list[tuple[str, int]]:
    hits = []
    needle = f"{symbol}("
    def_needle = f"def {symbol}("
    for path in sorted(src_dir.glob("*.py")):
        for lineno, line in enumerate(path.read_text().splitlines(), start=1):
            stripped = line.strip()
            if stripped.startswith("#") or def_needle in line:
                continue
            if needle in line:
                hits.append((path.name, lineno))
    return hits


def test_resolver_has_exactly_one_call_site_in_src():
    src_dir = pathlib.Path(loop_module.__file__).parent
    resolve_calls = _grep_calls(src_dir, "resolve_bundle")
    host_map_calls = _grep_calls(src_dir, "default_host_map")
    assert [name for name, _ in resolve_calls] == ["loop.py"], resolve_calls
    assert [name for name, _ in host_map_calls] == ["loop.py"], host_map_calls


def test_loop_has_exactly_two_subprocess_dispatch_sites():
    """`runner.run(...)` -- the actual subprocess launch -- appears exactly
    twice in loop.py: `_run_iteration_once` (the tiered dispatch call site)
    and `_ask_router` (the router's own, pre-existing, untiered call). No
    third pathway was introduced by this task."""
    src = pathlib.Path(loop_module.__file__)
    hits = [lineno for lineno, line in
            enumerate(src.read_text().splitlines(), start=1)
            if "runner.run(" in line and not line.strip().startswith("#")]
    assert len(hits) == 2, hits


# ---------------------------------------------------------------------------
# 2. Task-scoped dispatches carry the in-hand task's tier
# ---------------------------------------------------------------------------

def test_worker_dispatch_resolves_the_claimed_tasks_baseline_tier(tmp_path, monkeypatch):
    calls = _spy_resolve_bundle(monkeypatch)
    sup, run = _make_sup(tmp_path, [
        {"id": "001", "title": "t", "status": "pending",
         "successCriteria": "c", "tier": "hard"},
    ])
    sup.runner = _Agent(task_id="001", verdict="complete")
    asyncio.run(sup._step_worker())

    worker_calls = [c for c in calls if c[0] == "worker"]
    assert worker_calls == [("worker", "hard")]


def test_worker_dispatch_resolves_the_escalated_effective_tier_on_retry(
        tmp_path, monkeypatch):
    calls = _spy_resolve_bundle(monkeypatch)
    sup, run = _make_sup(tmp_path, [
        {"id": "001", "title": "t", "status": "validation-failed",
         "successCriteria": "c", "tier": "trivial", "effectiveTier": "trivial",
         "validationAttempts": 1},
    ])
    sup.runner = _Agent(task_id="001", verdict="complete")
    asyncio.run(sup._step_worker())

    worker_calls = [c for c in calls if c[0] == "worker"]
    # trivial -> +1 step on the validation-failed re-entry == normal
    assert worker_calls == [("worker", "normal")]
    assert run.read_tasks()["tasks"][0][state.TASK_EFFECTIVE_TIER_FIELD] == "normal"


def test_per_task_verify_dispatch_resolves_one_step_down_from_effective_tier(
        tmp_path, monkeypatch):
    calls = _spy_resolve_bundle(monkeypatch)
    task = {"id": "001", "title": "t", "status": "in-progress",
            "successCriteria": "c", "tier": "normal", "effectiveTier": "hard"}
    sup, run = _make_sup(tmp_path, [task])
    sup.runner = _Agent(task_id="001", verdict="verified")
    verified = asyncio.run(sup._verify_task(task))

    assert verified is True
    verify_calls = [c for c in calls if c[0] == "verify"]
    assert verify_calls == [("verify", "normal")]  # step_down_tier("hard")
    assert verify_calls[0][1] == state.task_verify_tier(task)


# ---------------------------------------------------------------------------
# 3. Non-task hats always resolve their ladder's designated tier
# ---------------------------------------------------------------------------

def test_planning_reflect_review_ignore_the_plans_task_tiers(tmp_path, monkeypatch):
    for plan_tier in ("trivial", "hard"):
        calls = _spy_resolve_bundle(monkeypatch)
        sup, run = _make_sup(tmp_path, [
            {"id": "001", "title": "t1", "status": "completed",
             "successCriteria": "c", "tier": plan_tier, "effectiveTier": plan_tier},
            {"id": "002", "title": "t2", "status": "completed",
             "successCriteria": "c", "tier": plan_tier, "effectiveTier": plan_tier},
        ])
        sup.runner = _Agent()

        asyncio.run(sup._one_planning_attempt(""))
        asyncio.run(sup._run_reflection())
        asyncio.run(sup._run_review_iteration(approach=1))

        planning_tiers = {t for h, t in calls if h == "planning"}
        reflect_tiers = {t for h, t in calls if h == "reflect"}
        review_tiers = {t for h, t in calls if h == "review"}

        assert planning_tiers == {T.DEFAULT_TIER}, (plan_tier, calls)
        assert reflect_tiers == {T.DEFAULT_TIER}, (plan_tier, calls)
        assert review_tiers == {state.run_level_verify_tier()}, (plan_tier, calls)
        # decision 8: the run-ending review is always verify's TOP tier,
        # never the plan's own (possibly cheaper) tier.
        assert state.run_level_verify_tier() == T.TOP_TIER


# ---------------------------------------------------------------------------
# 4. A steering-injected (unannotated) task starts at normal and escalates
#    like any other task -- no special case
# ---------------------------------------------------------------------------

def test_steering_injected_task_claims_at_normal_same_path_as_an_annotated_one(
        tmp_path, monkeypatch):
    calls = _spy_resolve_bundle(monkeypatch)
    # No "tier" key at all -- exactly what a steering-injected task (added
    # to the plan mid-run, never planner-annotated) looks like.
    sup, run = _make_sup(tmp_path, [
        {"id": "steer-1", "title": "consume steering", "status": "pending",
         "successCriteria": "c"},
    ])
    sup.runner = _Agent(task_id="steer-1", verdict="complete")
    asyncio.run(sup._step_worker())

    worker_calls = [c for c in calls if c[0] == "worker"]
    assert worker_calls == [("worker", "normal")]
    assert (run.read_tasks()["tasks"][0][state.TASK_EFFECTIVE_TIER_FIELD]
            == T.DEFAULT_TIER == "normal")


def test_steering_injected_task_escalates_through_the_same_code_as_an_annotated_task(
        tmp_path, monkeypatch):
    # Steering-injected: no "tier" field, only effectiveTier left by an
    # earlier claim, now failing validation.
    steering_task = {"id": "steer-1", "title": "t", "status": "validation-failed",
                      "successCriteria": "c", "effectiveTier": "normal",
                      "validationAttempts": 1}
    # An ordinary, explicitly-annotated task at the SAME effective tier, so
    # the two go through the identical escalation arithmetic.
    annotated_task = {"id": "001", "title": "t", "status": "validation-failed",
                       "successCriteria": "c", "tier": "normal",
                       "effectiveTier": "normal", "validationAttempts": 1}

    for task in (steering_task, annotated_task):
        calls = _spy_resolve_bundle(monkeypatch)
        sup, run = _make_sup(tmp_path / task["id"], [dict(task)])
        sup.runner = _Agent(task_id=task["id"], verdict="complete")
        asyncio.run(sup._step_worker())

        worker_calls = [c for c in calls if c[0] == "worker"]
        # normal -> +1 step == hard, whether or not "tier" was ever set
        assert worker_calls == [("worker", "hard")], (task["id"], calls)
        assert (run.read_tasks()["tasks"][0][state.TASK_EFFECTIVE_TIER_FIELD]
                == "hard")
