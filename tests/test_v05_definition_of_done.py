"""Task 059: the v0.5 definition-of-done bullet that no single earlier task
owned end to end -- "a 30-minute endpoint outage costs zero iterations, zero
approaches and zero tasks, and is interruptible".

Everything else in the DoD checklist is covered by a task-owned test (see
`artifacts/reports/v0.5-definition-of-done.md` for the mapping). What was
missing was the *duration* half: the earlier retry tests all run on a
compressed schedule, so nothing asserted that the SHIPPED DEFAULT schedule
(`JobConfig.infra_retry_backoff_s` / `infra_retry_backoff_max_s` /
`infra_outage_budget_s`) actually rides out half an hour of downtime rather
than giving up inside it.

No real sleeping happens here either: `_stub_attempts` replaces the
`_wait_out_backoff` seam, so the 30 minutes are virtual and the test costs
milliseconds.
"""

from __future__ import annotations

import asyncio
import json

import pytest
from test_infra_outage_budget import (
    _events,
    _infra_result,
    _ok_result,
    _stub_attempts,
    _supervisor,
)

from ralphd.engine.config import (
    DEFAULT_INFRA_OUTAGE_BUDGET_S,
    DEFAULT_INFRA_RETRY_BACKOFF_MAX_S,
    DEFAULT_INFRA_RETRY_BACKOFF_S,
    JobConfig,
)

OUTAGE_S = 30 * 60  # the DoD's "30-minute outage"


def _attempts_to_ride_out(seconds: float) -> int:
    """How many consecutive faults the default schedule absorbs before the
    cumulative wait passes `seconds`."""
    schedule = list(DEFAULT_INFRA_RETRY_BACKOFF_S)
    waited, n = 0.0, 0
    while waited < seconds:
        waited += min(schedule[min(n, len(schedule) - 1)],
                      DEFAULT_INFRA_RETRY_BACKOFF_MAX_S)
        n += 1
    return n


def test_defaults_can_ride_out_a_thirty_minute_outage():
    cfg = JobConfig(run_id="dod")
    assert cfg.infra_retry_backoff_s == list(DEFAULT_INFRA_RETRY_BACKOFF_S)
    assert cfg.infra_retry_backoff_max_s == DEFAULT_INFRA_RETRY_BACKOFF_MAX_S
    # The stopping rule is the outage budget, not an attempt count, and the
    # shipped budget (4h) is comfortably larger than the DoD's 30 minutes.
    assert cfg.infra_outage_budget_s == DEFAULT_INFRA_OUTAGE_BUDGET_S
    assert cfg.infra_outage_budget_s > OUTAGE_S
    assert not cfg.infra_retry_max, "no attempt cap by default"


@pytest.mark.asyncio
async def test_thirty_minute_outage_costs_no_iterations_approaches_or_tasks(
        tmp_path, monkeypatch):
    """The whole point of #5/#11: a half-hour gateway outage is *time*, not
    damage. Same phase, same iteration, same approach, same plan."""
    sup = _supervisor(tmp_path, job_timeout_s=8 * 3600)
    faults = _attempts_to_ride_out(OUTAGE_S)
    sup.run.tasks_file.write_text(json.dumps(
        {"tasks": [{"id": "001", "status": "pending"}]}))
    plan_before = sup.run.read_tasks()
    approach_before = sup.run.read_status().get("approach")
    charged_before = sup.iterations_used - sup._infra_refunded
    deadline_before = sup.deadline

    calls, waits, fake_sleep = _stub_attempts(
        sup, [_infra_result()] * faults + [_ok_result()])
    # `_stub_attempts` replaces the real `_run_iteration_once`, which is also
    # what allocates the iteration number; keep that side effect so the refund
    # arithmetic is the real thing (every attempt gets its own iteration
    # number, and only the working one is charged against the budget).
    inner = sup._run_iteration_once

    async def counting_once(phase, extra="", prompt_name=None):
        sup.iterations_used += 1
        return await inner(phase, extra, prompt_name)

    sup._run_iteration_once = counting_once  # type: ignore[method-assign]
    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    result = await sup.run_iteration("worker")

    # ... the endpoint came back and the phase's result is the healthy one.
    assert not result.error_message
    assert len(calls) == faults + 1
    assert calls == ["worker"] * (faults + 1), "same phase retried, not the next one"
    # ... after >= 30 minutes of waiting on the shipped default schedule.
    assert sum(waits) >= OUTAGE_S
    assert waits[:len(DEFAULT_INFRA_RETRY_BACKOFF_S)] == list(
        DEFAULT_INFRA_RETRY_BACKOFF_S), "escalating schedule as shipped"
    assert set(waits[len(DEFAULT_INFRA_RETRY_BACKOFF_S):]) <= {
        DEFAULT_INFRA_RETRY_BACKOFF_MAX_S}, "last value repeats, capped"

    # Zero iterations: every failed attempt was refunded, so the budget sees
    # exactly the one iteration that did work.
    assert sup._infra_refunded == faults
    assert sup.iterations_used - sup._infra_refunded == charged_before + 1
    # Zero approaches: an outage never escalates to a fresh approach.
    assert sup.run.read_status().get("approach") == approach_before
    # Zero tasks: no task was failed, retried or otherwise touched.
    assert sup.run.read_tasks() == plan_before
    # ... and the outage did not eat the job's own wall clock either.
    assert sup.deadline == pytest.approx(deadline_before + sum(waits))
    assert sup._infra_wait_total_s == pytest.approx(sum(waits))
    # ... while the episode clock reset once the endpoint answered again.
    assert sup._infra_episode_waited_s == 0.0


@pytest.mark.asyncio
async def test_the_thirty_minute_wait_is_interruptible_by_retry_now(
        tmp_path, monkeypatch):
    """Interruptibility half of the same bullet, on the real
    `_wait_out_backoff` seam: the operator's POST /retry (-> retry_now())
    releases a 5-minute default backoff immediately."""
    sup = _supervisor(tmp_path, job_timeout_s=8 * 3600)
    sup._infra_waiting = True  # what the wrapper sets around the wait
    sup._retry_now.clear()

    async def ring_the_doorbell():
        await asyncio.sleep(0.05)
        assert sup.retry_now() is True

    loop = asyncio.get_running_loop()
    started = loop.time()
    waited, woken = (await asyncio.gather(
        sup._wait_out_backoff(DEFAULT_INFRA_RETRY_BACKOFF_MAX_S),
        ring_the_doorbell()))[0]

    assert woken is True, "the wait must end because the operator asked"
    assert loop.time() - started < 5.0, "a 300s backoff was cut short"
    assert waited < 5.0, "only the seconds actually spent are booked"
    assert [ev["type"] for ev in _events(sup.run, "infra_retry_now")] == [
        "infra_retry_now"], "the manual retry is auditable"
    # Narrow by construction: nothing to wake -> False (the API's 409 path).
    assert sup.retry_now() is False
