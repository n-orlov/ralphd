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

import pytest

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
    module drives directly). Every call's kwargs (`model`/`thinking`/
    `effort`/...) are recorded in order in `self.calls`, so task 003's
    "did the CONFIGURED rung's ref actually reach the runner call" tests
    below can inspect them without a second spy on `resolve_bundle`."""

    def __init__(self, task_id: str | None = None, verdict: str | None = None,
                 hat: str | None = None):
        self.task_id = task_id
        self.verdict = verdict
        self.hat = hat
        self.calls: list[dict] = []

    def interrupt(self):
        return False

    async def run(self, prompt, transcript, **kw):
        self.calls.append(kw)
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


_DEF_RE = __import__("re").compile(r"^    (?:async )?def (\w+)\(")

_ALLOWED_DISPATCH_OWNERS = {
    "_run_iteration_once",  # the tiered dispatch call site
    "_ask_router",          # the router's own, pre-existing, untiered call
    "_ask_contract_fix",    # task 002's contract-repair re-ask
}


def _owning_function(lines: list[str], lineno: int) -> str | None:
    """Walk backwards from `lineno` (1-indexed) to the nearest enclosing
    method definition at 4-space (class-body) indent."""
    for i in range(lineno - 1, -1, -1):
        m = _DEF_RE.match(lines[i])
        if m:
            return m.group(1)
    return None


def test_loop_subprocess_dispatch_sites_are_owned_by_exactly_these_functions():
    """Task 002 (steer 002 item 2): `runner.run(...)` -- the actual
    subprocess launch -- appears in loop.py ONLY inside the three named
    owning functions. The old assertion here was a bare `len(hits) == 2`,
    which only proved a *count*: a 4th call site inside some brand-new
    function would have kept the count wrong (3 != 2) but a *swap* --
    deleting one of the two known sites while adding two new ones
    elsewhere -- would have sailed through unnoticed, and simply bumping
    the literal to `== 3` (the naive fix for `_ask_contract_fix` landing)
    would reopen exactly that hole for a would-be 4th site. Asserting the
    SET of owning function names instead pins identity, not just
    cardinality: any unlisted owner -- a stray new call site, or a call
    site moved out of its named function -- fails by naming the offender.
    """
    src = pathlib.Path(loop_module.__file__)
    lines = src.read_text().splitlines()
    hits = [lineno for lineno, line in enumerate(lines, start=1)
            if "runner.run(" in line and not line.strip().startswith("#")]

    owners = {lineno: _owning_function(lines, lineno) for lineno in hits}
    owner_set = set(owners.values())

    unexpected = owner_set - _ALLOWED_DISPATCH_OWNERS
    assert not unexpected, (
        f"runner.run( dispatched from unlisted function(s) {unexpected} "
        f"(lines: {owners}); only {_ALLOWED_DISPATCH_OWNERS} may own a "
        "subprocess-dispatch call site in loop.py"
    )
    missing = _ALLOWED_DISPATCH_OWNERS - owner_set
    assert not missing, (
        f"expected owning function(s) {missing} to each have a "
        f"runner.run( call site, but found none (lines: {owners})"
    )
    assert owner_set == _ALLOWED_DISPATCH_OWNERS, owners


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
# 2b. Task 004 (PRD requirement D): the per-task verify dispatch write
# carries the in-hand task's id and effective tier BEFORE the runner even
# starts -- proved by reading `meta.json` back from INSIDE the fake
# runner's own `run()` call (i.e. while `run_iteration` still considers
# this attempt "running"), for three separate outcomes the attempt can go
# on to produce: a verified verdict, an engine-classified fault (no
# traffic at all), and a clean exit with no usable verdict payload. All
# three share the SAME dispatch write (loop.py's `_run_iteration_once`),
# so the two fields are present identically in every case; only what
# happens AFTER `run()` returns differs.
# ---------------------------------------------------------------------------

class _InFlightVerifyAgent:
    """Reads the CURRENTLY-DISPATCHED iteration's `meta.json` back off disk
    from inside its own `run()` coroutine -- before returning anything at
    all -- and records what it found in `self.observed_meta`. Since
    `_run_iteration_once` writes that file, with `assignedTaskId`/
    `effectiveTier` already set, before it ever awaits `runner.run(...)`,
    this proves the two fields are on disk WHILE the runner is still
    running, not merely by the time it finishes.
    """

    def __init__(self, sup, result_factory):
        self.sup = sup
        self.result_factory = result_factory
        self.observed_metas: list[dict] = []

    def interrupt(self):
        return False

    async def run(self, prompt, transcript, **kw):
        n = self.sup.iterations_used
        meta_path = self.sup.run.iteration_dir(n) / "meta.json"
        self.observed_metas.append(json.loads(meta_path.read_text()))
        return self.result_factory()


def _verify_result_verified():
    r = IterationResult(exit_code=0)
    r.final_text = json.dumps({"task_id": "001", "verdict": "verified"})
    r.duration_s = 0.05
    r.usage = {"input": 1, "output": 1, "totalTokens": 2}
    return r


def _verify_result_fault():
    """An engine-classified INFRA fault: no traffic at all within the
    startup window -- `classify_fault` scores this `infra` unconditionally,
    regardless of exit code or error text."""
    r = IterationResult(exit_code=None)
    r.no_traffic_timeout = True
    r.final_text = ""
    r.usage = {}
    r.duration_s = 5.0
    return r


def _verify_result_no_usable_verdict():
    """A clean exit that never emitted a parseable `verify` contract
    payload at all -- an explicit verdict miss, never an engine fault."""
    r = IterationResult(exit_code=0)
    r.final_text = "not a contract payload, not JSON either"
    r.duration_s = 0.05
    r.usage = {"input": 1, "output": 1, "totalTokens": 2}
    return r


@pytest.mark.parametrize("result_factory", [
    _verify_result_verified,
    _verify_result_fault,
    _verify_result_no_usable_verdict,
])
def test_per_task_verify_dispatch_meta_carries_task_id_and_tier_while_running(
        tmp_path, result_factory):
    task = {"id": "001", "title": "t", "status": "in-progress",
            "successCriteria": "c", "tier": "normal", "effectiveTier": "hard"}
    sup, run = _make_sup(tmp_path, [task], infra_outage_budget_s=0.0)
    agent = _InFlightVerifyAgent(sup, result_factory)
    sup.runner = agent
    expected_tier = state.task_verify_tier(task)

    # Drives the REAL per-task verify call site (`_verify_task`, which is
    # what `_step_verify` calls for a completed task) -- never `run_
    # iteration` directly, so this exercises the actual production call
    # site's own `task_id=tid` argument, not a bypass of it.
    asyncio.run(sup._verify_task(task))

    # In-flight: read from INSIDE the fake runner's own run() call, before
    # this attempt's outcome (verified / fault / no usable verdict) was
    # even decided. At least one verify iteration must have dispatched,
    # and EVERY one of them (the fault/no-traffic-timeout case retries
    # inside `_verify_task` itself) carried both fields already.
    assert agent.observed_metas, "runner.run() was never called"
    for meta in agent.observed_metas:
        assert meta.get("assignedTaskId") == "001", meta
        assert meta.get("effectiveTier") == expected_tier, meta

    # End write: every dispatched verify iteration's meta.json, after
    # run_iteration() returned, must still carry both values -- the end
    # write only ever updates the dispatch write's own dict, never
    # rebuilds it.
    verify_end_metas = []
    for n in range(1, sup.iterations_used + 1):
        meta_path = run.iteration_dir(n) / "meta.json"
        if not meta_path.exists():
            continue
        end_meta = json.loads(meta_path.read_text())
        if end_meta.get("phase") == "verify":
            verify_end_metas.append(end_meta)
    assert verify_end_metas, "no verify iteration meta.json found"
    for end_meta in verify_end_metas:
        assert "endedAt" in end_meta, end_meta
        assert end_meta.get("assignedTaskId") == "001", end_meta
        assert end_meta.get("effectiveTier") == expected_tier, end_meta
        # The pre-existing `verifiedTask` reader (task 004/#116) keeps
        # working unmodified: `_verify_task`'s own end-of-attempt
        # enrichment sets it on the SAME dict the dispatch write already
        # populated, regardless of outcome.
        assert end_meta.get("verifiedTask") == "001", end_meta


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


# ---------------------------------------------------------------------------
# 5. Task 003: a CONFIGURED rung's ref (job.yaml `model_tiers`) actually
#    reaches the runner call at the one dispatch site -- proved on the
#    `sup.runner` seam, never by re-reading `default_host_map`/
#    `resolve_bundle`'s own unit tests (those only prove the pure host-map
#    merge; this proves the merged map is the one loop.py's dispatch site
#    actually builds and hands to the real subprocess call).
# ---------------------------------------------------------------------------

def test_configured_hard_rung_ref_reaches_the_runner_call_when_tier_is_hard(tmp_path):
    model_tiers = {"worker": {"hard": {"model": "hard-only-ref",
                                       "thinking": "high"}}}
    sup, run = _make_sup(tmp_path, [
        {"id": "001", "title": "t", "status": "pending",
         "successCriteria": "c", "tier": "hard"},
    ], model_tiers=model_tiers)
    agent = _Agent(task_id="001", verdict="complete")
    sup.runner = agent
    asyncio.run(sup._step_worker())

    worker_call = agent.calls[-1]
    assert worker_call["model"] == "hard-only-ref"
    assert worker_call["thinking"] == "high"
    assert worker_call["effort"] is None


def test_configured_normal_rung_ref_reaches_the_runner_call_when_tier_is_normal(tmp_path):
    model_tiers = {"worker": {"hard": {"model": "hard-only-ref", "thinking": "high"},
                               "normal": {"model": "normal-only-ref", "effort": "medium"}}}
    sup, run = _make_sup(tmp_path, [
        {"id": "001", "title": "t", "status": "pending",
         "successCriteria": "c", "tier": "normal"},
    ], model_tiers=model_tiers)
    agent = _Agent(task_id="001", verdict="complete")
    sup.runner = agent
    asyncio.run(sup._step_worker())

    worker_call = agent.calls[-1]
    assert worker_call["model"] == "normal-only-ref"
    assert worker_call["effort"] == "medium"
    assert worker_call["thinking"] is None
    # the hard-only rung's ref never leaks onto the normal dispatch
    assert worker_call["model"] != "hard-only-ref"

