"""Task 003 (#11): the operator-abort carve-out around the bare "aborted"
error.

pi records a SIGINT as the in-band error text "aborted", with no traffic and
no exit code of its own. That is textually identical whether the signal came
from a provider-side stream abort (a transient infra fault: retry it) or from
the operator asking this run to stop (never retry it -- the wrapper would sit
in backoff and then re-run the very iteration that was just aborted). These
tests pin both directions of the classifier and the loop wrapper that must
honour it.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ralphd.engine.config import JobConfig
from ralphd.engine.faults import classify_fault
from ralphd.engine.loop import LoopSupervisor
from ralphd.engine.runner import IterationResult
from ralphd.engine.state import RunDir

# -- classifier ------------------------------------------------------------


def test_bare_aborted_without_traffic_or_operator_abort_is_infra():
    assert classify_fault(
        error_text="aborted",
        exit_code=None,
        interrupted=True,
        produced_traffic=False,
        operator_abort=False,
    ) == "infra"


def test_bare_aborted_with_operator_abort_is_not_infra():
    verdict = classify_fault(
        error_text="aborted",
        exit_code=None,
        interrupted=True,
        produced_traffic=False,
        operator_abort=True,
    )
    assert verdict != "infra"
    assert verdict == "work"


def test_bare_aborted_with_traffic_is_not_infra():
    # The other half of "only when there was no traffic": an agent that made
    # real LLM calls and then got aborted is not an endpoint outage.
    assert classify_fault(
        error_text="aborted",
        exit_code=None,
        interrupted=True,
        produced_traffic=True,
        operator_abort=False,
    ) == "work"


@pytest.mark.parametrize(
    "kwargs",
    [
        {"error_text": "Connection error.", "exit_code": 0},
        {"error_text": "read ECONNRESET", "exit_code": 1},
        {"error_text": "", "exit_code": None, "interrupted": True,
         "no_traffic_timeout": True},
    ],
    ids=["sdk-connection-error", "econnreset", "startup-watchdog"],
)
def test_operator_abort_beats_every_infra_signal(kwargs):
    # An operator abort must not be delayed by a retry episode even when the
    # endpoint happens to be broken at the same moment: the run is ending.
    assert classify_fault(operator_abort=True, **kwargs) != "infra"


def test_operator_abort_still_lets_a_clean_iteration_be_a_success():
    assert classify_fault(exit_code=0, operator_abort=True) is None


# -- loop wrapper ----------------------------------------------------------


class _FakeRunner:
    """Stands in for PiRunner: pretends an agent is running so
    LoopSupervisor.interrupt() records a *delivered* operator interrupt."""

    def __init__(self, running: bool = True):
        self.running = running
        self.interrupts = 0

    def interrupt(self) -> bool:
        self.interrupts += 1
        return self.running


def _supervisor(tmp_path: Path, retry_max: int = 3) -> LoopSupervisor:
    run = RunDir(root=tmp_path)
    cfg = JobConfig(run_id="unit", infra_retry_max=retry_max,
                    infra_retry_backoff_s=[0.01])
    sup = LoopSupervisor(cfg, run, tmp_path)
    sup.runner = _FakeRunner()
    return sup


def _aborted_result() -> IterationResult:
    r = IterationResult(exit_code=None, interrupted=True)
    r.error_message = "aborted"
    r.duration_s = 120.0  # not an "instant" failure: the wrapper's own path
    return r


def _stub_attempts(sup: LoopSupervisor, hook=None) -> list[str]:
    """Replace _run_iteration_once with an always-"aborted" attempt,
    returning the list attempts are recorded into."""
    calls: list[str] = []

    async def fake_once(phase, extra="", prompt_name=None):
        calls.append(phase)
        if hook is not None:
            hook(sup)
        return _aborted_result()

    sup._run_iteration_once = fake_once  # type: ignore[method-assign]
    return calls


def _infra_retry_events(run: RunDir) -> list[dict]:
    log = run.root / "events.jsonl"
    lines = log.read_text().splitlines() if log.exists() else []
    return [ev for ev in (json.loads(x) for x in lines)
            if ev.get("type") == "infra_retry"]


@pytest.mark.asyncio
async def test_wrapper_retries_provider_side_aborted(tmp_path):
    # Control case: no operator abort recorded, so a bare "aborted" with no
    # traffic IS an infra fault and the wrapper retries it to exhaustion.
    sup = _supervisor(tmp_path, retry_max=3)
    calls = _stub_attempts(sup)
    await sup.run_iteration("worker")
    assert len(calls) == 3
    assert [ev["attempt"] for ev in _infra_retry_events(sup.run)] == [1, 2, 3]


@pytest.mark.asyncio
async def test_operator_abort_never_triggers_the_infra_retry_loop(tmp_path):
    sup = _supervisor(tmp_path, retry_max=3)
    calls = _stub_attempts(sup)
    sup.abort("operator asked to stop")
    await sup.run_iteration("worker")
    assert calls == ["worker"], "an operator abort must not be retried"
    assert _infra_retry_events(sup.run) == []
    assert sup._infra_refunded == 0
    assert sup.operator_abort_requested is True


@pytest.mark.asyncio
async def test_operator_interrupt_during_the_attempt_is_not_retried(tmp_path):
    # POST /interrupt (no abort reason recorded) landing while the agent runs:
    # the resulting "aborted" belongs to the operator, not the provider.
    sup = _supervisor(tmp_path, retry_max=3)
    calls = _stub_attempts(sup, hook=lambda s: s.interrupt())
    await sup.run_iteration("worker")
    assert calls == ["worker"]
    assert _infra_retry_events(sup.run) == []


@pytest.mark.asyncio
async def test_stale_interrupt_with_nothing_running_does_not_shield(tmp_path):
    # An interrupt that reached no process changed no iteration's outcome, so
    # it must not suppress the next iteration's infra retry.
    sup = _supervisor(tmp_path, retry_max=2)
    sup.runner = _FakeRunner(running=False)
    assert sup.interrupt() is False
    assert sup.operator_abort_requested is False
    calls = _stub_attempts(sup)
    await sup.run_iteration("worker")
    assert len(calls) == 2
