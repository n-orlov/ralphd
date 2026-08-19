"""Task 009 (#5): the infra-retry wrapper covers all five phases, and it does
not double-count an outage against a phase-local error budget.

Before this task `INFRA_RETRY_PHASES` was `("planning", "worker")` -- the two
phases the original incident hit -- so an endpoint outage that happened to
strike another phase was scored as *work*:

- an infra-shaped `review` failure rejected the approach and archived it
  (burning one of `max_approaches` plus the charged review iteration);
- an infra-shaped `verify` failure ate the task's bounded
  `MAX_VERIFY_ERROR_RETRIES` budget and, once that ran out, `_verify_task`
  gave up on the task;
- an infra-shaped `reflect` failure just lost the reflection report.

The precedence that must hold now: an infra-classified failure is handled by
the wrapper (retried in place, refunded) and consumes NEITHER
`MAX_VERIFY_ERROR_RETRIES`, NOR the review steering/approach bookkeeping, NOR
a task's `validationAttempts`.

The verify half is a unit test over `_verify_task` with `_run_iteration_once`
stubbed (so it can inject more consecutive infra faults than
`MAX_VERIFY_ERROR_RETRIES` allows without any real sleeping); the review half
is black-box (stub-pi + the real engine), reading events.jsonl/status.json the
way an operator would.
"""

from __future__ import annotations

import asyncio
import json

import pytest
from test_e2e import engine_factory

from ralphd.engine.config import JobConfig
from ralphd.engine.loop import LoopSupervisor
from ralphd.engine.runner import IterationResult
from ralphd.engine.state import RunDir, atomic_write_json

__all__ = ["engine_factory"]

INFRA_ERROR = "Connection error."
# > LoopSupervisor.INSTANT_FAILURE_MAX_DURATION_S (5.0): an *instant*
# no-traffic fault is still owned by the broken-environment carve-out
# (task 010), and these tests are about the retry path.
INBAND_DELAY_S = "6"


def _events(run_dir, type_):
    root = run_dir.root if isinstance(run_dir, RunDir) else run_dir
    log = root / "events.jsonl"
    lines = log.read_text().splitlines() if log.exists() else []
    return [ev for ev in (json.loads(x) for x in lines) if ev.get("type") == type_]


# -- all five phases are wrapped -------------------------------------------


def test_every_phase_goes_through_the_infra_retry_wrapper():
    assert set(LoopSupervisor.INFRA_RETRY_PHASES) == {
        "planning", "worker", "review", "verify", "reflect"}


# -- verify: no double-count against MAX_VERIFY_ERROR_RETRIES --------------


def _infra_result() -> IterationResult:
    r = IterationResult(exit_code=0)
    r.error_message = INFRA_ERROR
    r.duration_s = 30.0  # not "instant": the wrapper's retry path
    return r


def _verified_result(task_id: str) -> IterationResult:
    r = IterationResult(exit_code=0)
    r.duration_s = 30.0
    r.final_text = f"<task-verified>{task_id}</task-verified>"
    return r


def _verify_supervisor(tmp_path, task: dict) -> LoopSupervisor:
    run = RunDir(root=tmp_path)
    atomic_write_json(run.tasks_file, {"version": 1, "tasks": [task]})
    cfg = JobConfig(run_id="unit", vigilant=True,
                    infra_retry_backoff_s=[0.0],
                    infra_retry_backoff_max_s=0.0,
                    infra_outage_budget_s=1000.0)
    return LoopSupervisor(cfg, run, tmp_path)


def _stub_attempts(sup: LoopSupervisor, results: list[IterationResult]):
    """Feeds `results` to the wrapper one attempt at a time (the last repeats
    forever) and replaces the backoff wait with a recorder."""
    calls: list[str] = []
    waits: list[float] = []

    async def fake_once(phase, extra="", prompt_name=None):
        calls.append(phase)
        return results[min(len(calls) - 1, len(results) - 1)]

    async def fake_backoff(seconds):
        # Task 015 (#5): _wait_out_backoff is the interruptible-Event seam
        # that replaced the wrapper's asyncio.sleep.
        waits.append(seconds)
        return seconds, False

    async def fake_sleep(seconds):
        return None  # back-compat no-op for call sites patching asyncio.sleep

    sup._run_iteration_once = fake_once  # type: ignore[method-assign]
    sup._wait_out_backoff = fake_backoff  # type: ignore[method-assign]
    return calls, waits, fake_sleep


@pytest.mark.asyncio
async def test_infra_shaped_verify_is_retried_without_touching_verify_budgets(
        tmp_path, monkeypatch):
    # FIVE consecutive infra faults: strictly more than
    # MAX_VERIFY_ERROR_RETRIES (3), so if the wrapper let them fall through
    # to _verify_task's bounded error-retry loop the task would be given up
    # on before ever reaching the healthy attempt.
    task = {"id": "001", "title": "t", "status": "completed",
            "successCriteria": "c"}
    sup = _verify_supervisor(tmp_path, task)
    assert sup.MAX_VERIFY_ERROR_RETRIES == 3
    faults = 5
    calls, waits, fake_sleep = _stub_attempts(
        sup, [_infra_result()] * faults + [_verified_result("001")])
    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    verified = await sup._verify_task(task)

    assert verified is True, "the wrapper rode out the outage and verify passed"
    assert calls == ["verify"] * (faults + 1)
    # Every failed attempt was handled by the wrapper: retried + refunded...
    infra = _events(sup.run, "infra_retry")
    assert [ev["attempt"] for ev in infra] == [1, 2, 3, 4, 5]
    assert {ev["phase"] for ev in infra} == {"verify"}
    assert waits == [0.0] * faults, "compressed schedule: no real sleeping"
    assert sup._infra_refunded == faults
    # ... and none of it was charged to the phase-local error budget: no
    # "retrying verification" bookkeeping fired at all.
    logs = [ev.get("message", "") for ev in _events(sup.run, "log")]
    assert not [m for m in logs if "retrying verification" in m], logs
    assert not [m for m in logs if "kept erroring" in m], logs
    # ... nor to the task's validationAttempts / status.
    t = sup.run.read_tasks()["tasks"][0]
    assert t.get("validationAttempts", 0) == 0
    assert t["status"] == "completed"
    assert "001" in sup.run.read_verified_tasks()


@pytest.mark.asyncio
async def test_non_infra_verify_error_still_consumes_the_verify_budget(
        tmp_path, monkeypatch):
    """The other side of the precedence rule: a *work* failure is exactly as
    before -- the wrapper hands it straight back and _verify_task's bounded
    error-retry loop owns it."""
    task = {"id": "001", "title": "t", "status": "completed",
            "successCriteria": "c"}
    sup = _verify_supervisor(tmp_path, task)
    work_fault = IterationResult(exit_code=1)
    work_fault.error_message = "agent produced no verdict: prompt was rejected"
    work_fault.duration_s = 30.0
    # Real LLM traffic + no infra signature == "work" (see classify_fault).
    work_fault.final_text = "I could not decide; giving up."
    work_fault.usage = {"totalTokens": 120}
    calls, _waits, fake_sleep = _stub_attempts(
        sup, [work_fault, _verified_result("001")])
    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    verified = await sup._verify_task(task)

    assert verified is True
    assert calls == ["verify", "verify"]
    assert not _events(sup.run, "infra_retry"), "not an infra fault"
    assert sup._infra_refunded == 0
    logs = [ev.get("message", "") for ev in _events(sup.run, "log")]
    assert [m for m in logs if "retrying verification (1/3)" in m], logs


# -- review: no double-count against approaches / the iteration budget -----


def test_infra_shaped_review_is_retried_and_does_not_cost_an_approach(
        engine_factory):
    # Charged budget is exactly the happy path (planning + 1 worker + review
    # == 3) and invocation 3 (the first review) fails in band with exit 0.
    # Before task 009 that review was scored as a rejected approach: the
    # approach was archived, a composite PRD written, and the charged review
    # iteration spent -- so with iterations=3/max_approaches=1 the run could
    # not reach a verified terminal state at all.
    e = engine_factory(
        job={"on_complete": "exit", "iterations": 3, "max_approaches": 1},
        stub_env={
            "STUB_TASKS": "1",
            "STUB_INBAND_ERROR_SKIP": "2",   # planning + worker are healthy
            "STUB_INBAND_ERROR_COUNT": "1",  # invocation 3 (1st review) errors
            "STUB_INBAND_ERROR_DELAY_S": INBAND_DELAY_S,
            "RALPHD_INFRA_RETRY_BACKOFF_S": "0.1,0.1,0.1",
        })
    assert e.proc.wait(timeout=90) == 0

    status = json.loads((e.run_dir / "status.json").read_text())
    assert status["state"] == "succeeded"
    assert status["verdict"] == "verified"

    infra = _events(e.run_dir, "infra_retry")
    assert len(infra) == 1, infra
    assert infra[0]["phase"] == "review"
    assert infra[0]["attempt"] == 1
    assert "Connection error" in infra[0]["error"]

    # No double-count: the outage cost no approach, no archived approach dir,
    # no composite PRD rewrite, and no charged iteration (4 attempts ran,
    # 3 are charged).
    assert status["approach"] == 1
    logs = [ev.get("message", "") for ev in _events(e.run_dir, "log")]
    assert not [m for m in logs if "review rejected" in m], logs
    assert list((e.run_dir / "approaches").iterdir()) == []
    assert not (e.run_dir / "composite-prd.md").exists()
    metas = [json.loads((d / "meta.json").read_text())
             for d in sorted((e.run_dir / "iterations").iterdir())
             if (d / "meta.json").exists()]
    assert [m["phase"] for m in metas] == ["planning", "worker", "review", "review"]
    assert [m["faultClass"] for m in metas] == [None, None, "infra", None]
    assert status["iterationsUsed"] == 3
