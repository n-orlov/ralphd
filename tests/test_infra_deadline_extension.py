"""Task 011 (#5): sitting out an infra outage must not eat the job's own
wall-clock time.

`self.deadline` (and its published twin `deadlineAt`) is wall clock, so a
4-hour gateway outage would silently consume half an 8-hour job and the run
would then die of "timeout" having done no work at all. The decision taken is
therefore to *extend* the deadline by exactly the time spent waiting, account
the total in `status.json`'s `infraWaitTotalS`, and emit a
`deadline_extended` event for every extension so the clock adjustment is
auditable instead of invisible.

The unit half asserts the arithmetic exactly (stubbed attempts, recorded
backoffs, no real sleeping); the black-box half proves the same numbers land
in the real run dir's status.json and events.jsonl.
"""

from __future__ import annotations

import asyncio
import json

import pytest
from test_e2e import engine_factory
from test_infra_outage_budget import (
    _events,
    _infra_result,
    _ok_result,
    _stub_attempts,
    _supervisor,
)

from ralphd.engine.state import parse_utc

__all__ = ["engine_factory"]


# -- unit: the arithmetic --------------------------------------------------


@pytest.mark.asyncio
async def test_deadline_extended_by_the_waited_time_and_total_accounted(
        tmp_path, monkeypatch):
    sup = _supervisor(tmp_path, job_timeout_s=3600,
                      infra_retry_backoff_s=[0.1, 0.2, 0.4],
                      infra_outage_budget_s=1000.0)
    orig_deadline, orig_epoch = sup.deadline, sup._deadline_epoch
    assert sup._infra_wait_total_s == 0.0
    _calls, waits, fake_sleep = _stub_attempts(
        sup, [_infra_result()] * 3 + [_ok_result()])
    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    await sup.run_iteration("worker")

    assert waits == [0.1, 0.2, 0.4]
    total = sum(waits)
    # infraWaitTotalS == the sum of every wait in the episode ...
    assert sup._infra_wait_total_s == pytest.approx(total)
    # ... and both deadlines moved out by exactly that much.
    assert sup.deadline == pytest.approx(orig_deadline + total)
    assert sup._deadline_epoch == pytest.approx(orig_epoch + total)

    status = json.loads((sup.run.root / "status.json").read_text())
    assert status["infraWaitTotalS"] == pytest.approx(total)
    # deadlineAt is second-granularity ISO, hence the 1.5s tolerance.
    assert parse_utc(status["deadlineAt"]) == pytest.approx(
        orig_epoch + total, abs=1.5)

    ext = _events(sup.run, "deadline_extended")
    assert [ev["waitedS"] for ev in ext] == waits
    assert [ev["infraWaitTotalS"] for ev in ext] == [
        pytest.approx(0.1), pytest.approx(0.3), pytest.approx(0.7)]
    assert [ev["attempt"] for ev in ext] == [1, 2, 3]
    assert all(ev["phase"] == "worker" for ev in ext)
    assert parse_utc(ext[-1]["deadlineAt"]) == pytest.approx(
        orig_epoch + total, abs=1.5)


@pytest.mark.asyncio
async def test_separate_episodes_accumulate_into_one_total(tmp_path, monkeypatch):
    sup = _supervisor(tmp_path, job_timeout_s=3600,
                      infra_retry_backoff_s=[0.1, 0.2],
                      infra_outage_budget_s=1000.0)
    orig_deadline = sup.deadline
    seq = [_infra_result(), _infra_result(), _ok_result(),
           _infra_result(), _ok_result()]
    _calls, waits, fake_sleep = _stub_attempts(sup, seq)
    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    await sup.run_iteration("worker")
    await sup.run_iteration("worker")

    assert waits == [0.1, 0.2, 0.1]
    # The episode clock resets between outages; the *run* total does not.
    assert sup._infra_episode_waited_s == 0.0
    assert sup._infra_wait_total_s == pytest.approx(sum(waits))
    assert sup.deadline == pytest.approx(orig_deadline + sum(waits))
    assert len(_events(sup.run, "deadline_extended")) == 3


@pytest.mark.asyncio
async def test_clean_iteration_never_touches_the_deadline(tmp_path, monkeypatch):
    sup = _supervisor(tmp_path, job_timeout_s=3600)
    orig_deadline, orig_epoch = sup.deadline, sup._deadline_epoch
    _calls, _waits, fake_sleep = _stub_attempts(sup, [_ok_result()])
    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    await sup.run_iteration("worker")

    assert sup.deadline == orig_deadline
    assert sup._deadline_epoch == orig_epoch
    assert sup._infra_wait_total_s == 0.0
    assert _events(sup.run, "deadline_extended") == []


def test_total_survives_a_resume(tmp_path):
    # A resumed engine picks the accumulated total back up from status.json
    # (the deadline itself is per-process by construction) so the operator
    # sees the run's whole outage cost, not just this process's share.
    sup = _supervisor(tmp_path, job_timeout_s=3600)
    sup.run.update_status(infraWaitTotalS=42.5)
    resumed = _supervisor(tmp_path, job_timeout_s=3600)
    assert resumed._infra_wait_total_s == 42.5


# -- black box: through the real engine ------------------------------------


def test_status_json_records_infra_wait_total_and_extended_deadline(engine_factory):
    e = engine_factory(
        job={"on_complete": "exit", "iterations": 3, "max_approaches": 1,
             "job_timeout_s": 600},
        stub_env={
            "STUB_TASKS": "1",
            "STUB_INFRA_HANG_SKIP": "1",   # planning is healthy
            "STUB_INFRA_HANG_COUNT": "3",  # three worker attempts hang
            "RALPHD_INFRA_STARTUP_TIMEOUT": "1",
            "RALPHD_INFRA_RETRY_BACKOFF_S": "0.5,1",
            "RALPHD_INFRA_OUTAGE_BUDGET_S": "60",
        })
    assert e.proc.wait(timeout=90) == 0

    status = json.loads((e.run_dir / "status.json").read_text())
    assert status["state"] == "succeeded"
    events = [json.loads(line) for line in
              (e.run_dir / "events.jsonl").read_text().splitlines()]
    waits = [ev["backoffS"] for ev in events if ev.get("type") == "infra_retry"]
    assert waits == [0.5, 1.0, 1.0]
    assert status["infraWaitTotalS"] == pytest.approx(sum(waits))

    ext = [ev for ev in events if ev.get("type") == "deadline_extended"]
    assert [ev["waitedS"] for ev in ext] == waits
    # deadlineAt == start + job_timeout_s + the waited time (the whole point:
    # the outage is added on top, not taken out of the job's budget).
    expected = parse_utc(status["startedAt"]) + 600 + sum(waits)
    assert parse_utc(status["deadlineAt"]) == pytest.approx(expected, abs=5)
