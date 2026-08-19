"""Task 018 (#5): the post-terminal `reflect` iteration is really covered by
the infra-retry wrapper, and it waits before its first attempt when the job
just ended on an infra-shaped failure.

PRD incident 2 (`est6534-impl-phase1-ui-safety`): four consecutive
"Connection error." iterations failed the last approach, `_run_reflection`
then launched into the same dead gateway *in the same second*, got nothing,
and discarded the result -- a 105-iteration, 102M-token run with no
post-mortem. `reflect` had been listed in `INFRA_RETRY_PHASES` since task 009,
but two pieces of the job's own ending made the wrapper a no-op for it:

- the episode clock arrived spent (a job that died of an outage has already
  burned its whole outage budget), so the first reflect fault scored "budget
  exhausted" instead of being retried;
- `operator_abort_requested` is true for *any* recorded abort reason -- the
  wrapper's own give-up included -- and `classify_fault(operator_abort=True)`
  never returns "infra", so every reflect failure was scored "work" and
  handed straight back.

The unit half asserts both, plus that an *operator* abort keeps its veto (a
run the operator stopped must not sit in backoff), with `_wait_out_backoff`
stubbed so nothing really sleeps. The black-box half drives the real engine +
stub-pi through the incident's exact shape: the job dies of an outage, the
first reflect attempt dies with it, the second one succeeds and the report
exists -- one reflect phase, strictly after the terminal state, terminal state
untouched.
"""

from __future__ import annotations

import json
from datetime import datetime

import pytest
from test_e2e import EngineProc

from ralphd.engine.config import JobConfig
from ralphd.engine.loop import LoopSupervisor
from ralphd.engine.runner import IterationResult
from ralphd.engine.state import RunDir

INFRA_ERROR = "Connection error."
JOB_REASON = ("infra fault: planning iteration failed throughout a 300s infra "
              f"outage (5 attempts, 300s of the 300s outage budget spent "
              f"waiting): {INFRA_ERROR}")
# > LoopSupervisor.INSTANT_FAILURE_MAX_DURATION_S (5.0): an instant no-traffic
# fault belongs to the broken-environment carve-out (task 010), not here.
SLOW = 30.0
INBAND_DELAY_S = "6"


def _events(run_dir, type_):
    root = run_dir.root if isinstance(run_dir, RunDir) else run_dir
    log = root / "events.jsonl"
    lines = log.read_text().splitlines() if log.exists() else []
    return [ev for ev in (json.loads(x) for x in lines) if ev.get("type") == type_]


def _iso(ts: str) -> float:
    return datetime.fromisoformat(ts).timestamp()


def _infra_result() -> IterationResult:
    r = IterationResult(exit_code=0)
    r.error_message = INFRA_ERROR
    r.duration_s = SLOW
    return r


def _ok_result() -> IterationResult:
    r = IterationResult(exit_code=0)
    r.duration_s = SLOW
    r.final_text = "reflection complete"
    return r


def _supervisor(tmp_path, **cfg_kw) -> LoopSupervisor:
    run = RunDir(root=tmp_path)
    defaults = {"infra_retry_backoff_s": [0.5], "infra_retry_backoff_max_s": 0.5,
                "infra_outage_budget_s": 10.0}
    cfg = JobConfig(run_id="unit", reflect=True, **{**defaults, **cfg_kw})
    return LoopSupervisor(cfg, run, tmp_path)


def _stub_attempts(sup: LoopSupervisor, results: list[IterationResult]):
    """Feeds `results` to the wrapper one attempt at a time (the last repeats
    forever) and replaces every infra wait with a recorder -- both the
    wrapper's backoff and reflect's pre-attempt delay go through
    `_wait_out_backoff`, so nothing sleeps for real."""
    calls: list[str] = []
    waits: list[float] = []

    async def fake_once(phase, extra="", prompt_name=None):
        calls.append(phase)
        return results[min(len(calls) - 1, len(results) - 1)]

    async def fake_backoff(seconds):
        waits.append(seconds)
        return seconds, False

    sup._run_iteration_once = fake_once  # type: ignore[method-assign]
    sup._wait_out_backoff = fake_backoff  # type: ignore[method-assign]
    return calls, waits


def _job_ended_on_an_outage(sup: LoopSupervisor) -> None:
    """The state _run_job_core() leaves behind when the wrapper gave up on an
    infra outage: an engine-recorded abort reason, a fully spent episode
    clock, and a last iteration classified "infra"."""
    sup._abort_reason = JOB_REASON
    sup._last_fault_class = "infra"
    sup._last_fault_error = INFRA_ERROR
    sup._infra_episode_waited_s = sup.cfg.infra_outage_budget_s
    sup._infra_episode_attempts = 5


# -- unit: the wrapper is not a no-op for reflect any more ------------------


@pytest.mark.asyncio
async def test_reflect_is_retried_after_the_job_gave_up_on_an_outage(tmp_path):
    sup = _supervisor(tmp_path)
    _job_ended_on_an_outage(sup)
    calls, waits = _stub_attempts(sup, [_infra_result(), _ok_result()])

    await sup._run_reflection()

    assert calls == ["reflect", "reflect"], "the failed reflect attempt was retried"
    # The pre-attempt delay came first, then the wrapper's own backoff.
    assert waits == [0.5, 0.5]
    delay = _events(sup.run, "reflect_infra_delay")
    assert len(delay) == 1, delay
    assert delay[0]["delayS"] == 0.5
    assert INFRA_ERROR in delay[0]["error"]
    assert delay[0]["budgetS"] == min(sup.cfg.infra_outage_budget_s,
                                      sup.REFLECT_OUTAGE_BUDGET_S)
    # The delay is published as an ordinary infra wait (attempt 0: it precedes
    # the episode's numbered retries), so /status and the event stream explain
    # why a terminal run's container is still alive.
    waiting = _events(sup.run, "infra_wait")
    assert [(ev["phase"], ev["attempt"]) for ev in waiting] == [
        ("reflect", 0), ("reflect", 1)]
    assert [ev["attempt"] for ev in _events(sup.run, "infra_retry")] == [1]
    # The failed attempt was refunded, and the job's terminal reason is
    # exactly what _run_job_core() recorded -- reflect never rewrites it.
    assert sup._infra_refunded == 1
    assert sup._abort_reason == JOB_REASON
    assert sup.run.read_status().get("phase") is None


@pytest.mark.asyncio
async def test_reflect_gives_up_within_its_own_short_budget(tmp_path):
    # An endpoint that is simply gone: reflect must not hold a finished run's
    # container open for the job's whole 4h outage budget.
    sup = _supervisor(tmp_path, infra_outage_budget_s=1.0)
    _job_ended_on_an_outage(sup)
    calls, waits = _stub_attempts(sup, [_infra_result()])

    await sup._run_reflection()

    # pre-attempt delay 0.5s + one 0.5s backoff exhausts the 1.0s budget.
    assert waits == [0.5, 0.5]
    assert calls == ["reflect", "reflect"]
    assert sup._abort_reason == JOB_REASON, "the terminal reason is untouched"


def test_reflect_gets_a_capped_outage_budget_every_other_phase_does_not(tmp_path):
    sup = _supervisor(tmp_path, infra_outage_budget_s=14400.0)
    assert sup._outage_budget_for("reflect") == sup.REFLECT_OUTAGE_BUDGET_S
    for phase in ("planning", "worker", "review", "verify"):
        assert sup._outage_budget_for(phase) == 14400.0
    # ... and a job budget shorter than the cap still wins.
    short = _supervisor(tmp_path, infra_outage_budget_s=30.0)
    assert short._outage_budget_for("reflect") == 30.0


@pytest.mark.asyncio
async def test_operator_abort_keeps_its_veto_over_reflect(tmp_path):
    sup = _supervisor(tmp_path)
    sup._last_fault_class = "infra"
    sup._last_fault_error = INFRA_ERROR
    sup.abort("operator asked to stop")
    calls, waits = _stub_attempts(sup, [_infra_result(), _ok_result()])

    await sup._run_reflection()

    assert calls == ["reflect"], "an operator abort must not be retried"
    assert waits == [], "and must not be made to wait either"
    assert _events(sup.run, "reflect_infra_delay") == []
    assert _events(sup.run, "infra_retry") == []
    assert sup._abort_reason == "operator asked to stop"


@pytest.mark.asyncio
async def test_a_job_that_ended_cleanly_reflects_immediately(tmp_path):
    sup = _supervisor(tmp_path)
    sup._last_fault_class = None  # the job's last iteration reached the model
    calls, waits = _stub_attempts(sup, [_ok_result()])

    await sup._run_reflection()

    assert calls == ["reflect"]
    assert waits == [], "no delay when there was no outage to wait out"
    assert _events(sup.run, "reflect_infra_delay") == []


# -- black-box: the incident's exact shape through the real engine ----------


def test_reflect_survives_the_outage_that_killed_the_job(tmp_path):
    """Invocations 1-4: planning, failing in band until the outage budget is
    spent (the job ends `aborted`). Invocation 5: reflect's first attempt,
    still inside the outage. Invocation 6: reflect's retry, which succeeds and
    writes the report."""
    e = EngineProc(
        tmp_path,
        {"run_id": "reflect-outage", "reflect": True, "iterations": 3,
         "max_approaches": 1, "on_complete": "exit"},
        {"STUB_TASKS": "1",
         "STUB_INBAND_ERROR_COUNT": "5",
         "STUB_INBAND_ERROR_DELAY_S": INBAND_DELAY_S,
         "RALPHD_INFRA_RETRY_BACKOFF_S": "1.0",
         "RALPHD_INFRA_RETRY_BACKOFF_MAX_S": "1.0",
         "RALPHD_INFRA_OUTAGE_BUDGET_S": "2.5"})
    try:
        terminal = e.wait_state(("succeeded", "failed", "aborted"), timeout=120)
        assert e.proc.wait(timeout=120) == 1  # aborted == nonzero exit
    finally:
        e.stop()

    # -- the reflection report exists despite the outage -------------------
    report = e.run_dir / "artifacts" / "reflection" / "report.md"
    assert report.exists(), "the outage swallowed the reflection report again"

    metas = [json.loads((d / "meta.json").read_text())
             for d in sorted((e.run_dir / "iterations").iterdir())
             if (d / "meta.json").exists()]
    phases = [m["phase"] for m in metas]
    assert set(phases[:-2]) == {"planning"}, phases
    # Exactly one reflect *phase*: two attempts of the same single iteration,
    # the first infra-classified (retried + refunded), the second clean.
    assert phases[-2:] == ["reflect", "reflect"], phases
    assert [m["faultClass"] for m in metas[-2:]] == ["infra", None]
    reflect_phase_events = [ev for ev in _events(e.run_dir, "phase")
                            if ev.get("phase") == "reflect"]
    assert len(reflect_phase_events) == 1, reflect_phase_events

    # -- the first attempt waited instead of firing into the dead endpoint --
    delay = _events(e.run_dir, "reflect_infra_delay")
    assert len(delay) == 1, delay
    assert delay[0]["delayS"] == 1.0
    assert "Connection error" in delay[0]["error"]
    # Observable in the timestamps too: reflect started strictly after the
    # terminal state was recorded, by at least (roughly) the delay.
    ended_at = terminal["endedAt"]
    first_reflect_started = metas[-2]["startedAt"]
    assert first_reflect_started > ended_at
    gap = (_iso(first_reflect_started) - _iso(ended_at))
    assert gap >= 0.9, f"reflect fired {gap:.2f}s after the terminal state"

    # -- the terminal state is exactly what the job decided ----------------
    final = json.loads((e.run_dir / "status.json").read_text())
    assert final["state"] == terminal["state"] == "aborted"
    assert final["verdict"] == terminal["verdict"] == "unverified"
    assert final["reason"] == terminal["reason"]
    assert "infra fault" in final["reason"]
    assert final["phase"] is None
    # Only the *successful* reflect attempt is charged (reflect always was
    # a charged iteration); the failed one was refunded like any other
    # infra-classified attempt.
    assert final["iterationsUsed"] == terminal["iterationsUsed"] + 1
