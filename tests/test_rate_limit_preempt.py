"""v0.30 F.4/F.5: do not start what will be cut.

Before a `claude-code` iteration starts, the run's last rate-limit report for
that harness is read. `allowed_warning` at or above
`JobConfig.rate_limit_preempt_utilization` (default 0.95) makes the loop wait
for that window's reset at the boundary (the F.2 wait, and the F.3 ceiling
applies); below it, with no report at all, or on a pi hat, the iteration
starts at once.

Waits go through the recorded `_wait_out_backoff` stand-in (never slept), the
clock is the `_now_epoch` seam and the jitter is 0.
"""

from __future__ import annotations

import json

import pytest

from ralphd.engine.faults import RATE_LIMIT_CEILING_REASON
from ralphd.engine.runner import IterationResult
from ralphd.engine.state import utc_from_epoch
from tests.conftest import ScriptedRunner, make_supervisor

NOW = 1_790_000_000.0
CLAUDE_WORKER = {"worker": "claude-code"}


def _warning(utilization: float, resets_at=NOW + 1200, rl_type="five_hour",
             *, top_level: bool = False) -> dict:
    """The CLI's shape: utilization and reset per window, none at the top
    level (sdk.md §7.6); `top_level=True` puts it at the top instead."""
    window = {"status": "allowed_warning", "utilization": utilization,
              "resetsAt": resets_at}
    info = {"status": "allowed_warning", "rateLimitType": rl_type,
            "unifiedWindows": {rl_type: window}}
    if top_level:
        info = {"status": "allowed_warning", "rateLimitType": rl_type,
                "utilization": utilization, "resetsAt": resets_at}
    return info


def _ok(harness="claude-code", rate_limit=None) -> IterationResult:
    r = IterationResult(exit_code=0, harness=harness, rate_limit=rate_limit,
                        rate_limit_fresh=rate_limit is not None)
    r.final_text = "done"
    r.usage = {"input": 10, "output": 5, "totalTokens": 15}
    r.duration_s = 30.0
    return r


def _step(result):
    return lambda sup, prompt: result


def _events(sup, type_):
    log = sup.run.root / "events.jsonl"
    lines = log.read_text().splitlines() if log.exists() else []
    return [ev for ev in map(json.loads, lines) if ev.get("type") == type_]


def _status(sup):
    return json.loads((sup.run.root / "status.json").read_text())


def _harness(tmp_path, steps, *, harnesses=CLAUDE_WORKER, state=None, **cfg):
    sup = make_supervisor(tmp_path, runner=ScriptedRunner({"worker": steps}),
                          prd=True, job_timeout_s=3600, harnesses=harnesses,
                          **cfg)
    sup._now_epoch = lambda: NOW                         # type: ignore[method-assign]
    sup._rate_limit_jitter_s = lambda: 0.0               # type: ignore[method-assign]
    if state is not None:
        sup._rate_limit_state.update(state)
    rec = {"waits": [], "during": [], "calls": 0, "notes": [], "parked": []}

    async def wait(seconds):
        rec["waits"].append(seconds)
        rec["during"].append((rec["calls"], _status(sup).get("rateLimitWait")))
        return seconds, False

    async def park():
        rec["parked"].append(_status(sup))
        sup.resume()
        return 1.0

    sup._wait_out_backoff = wait                         # type: ignore[method-assign]
    sup._park_until_resumed = park                       # type: ignore[method-assign]
    sup.notifier = rec["notes"].append
    real_once = sup._run_iteration_once

    async def once(*a, **kw):
        rec["calls"] += 1
        return await real_once(*a, **kw)

    sup._run_iteration_once = once                       # type: ignore[method-assign]
    return sup, rec


@pytest.mark.asyncio
async def test_a_warning_at_96_percent_waits_for_the_reset_before_starting(tmp_path):
    sup, rec = _harness(tmp_path, [_step(_ok())],
                        state={"claude-code": _warning(0.96)})
    deadline = sup.deadline

    result = await sup.run_iteration("worker")

    assert result.final_text == "done"
    # one wait, to the window's reset, taken before the only attempt started
    assert rec["waits"] == [1200.0]
    assert rec["during"] == [(0, {"type": "five_hour",
                                  "resetsAt": utc_from_epoch(NOW + 1200),
                                  "since": utc_from_epoch(NOW)})]
    assert rec["calls"] == 1
    (started,) = _events(sup, "rate_limit_wait_started")
    assert started["reason"] == "preempt" and started["utilization"] == 0.96
    (ended,) = _events(sup, "rate_limit_wait_ended")
    assert ended["reason"] == "preempt" and ended["wokenBy"] == "reset"
    # the F.2 wait: deadline extended, no iteration spent, no outage touched
    assert sup.deadline == pytest.approx(deadline + 1200)
    assert [e["reason"] for e in _events(sup, "deadline_extended")] == [
        "rate limit wait"]
    assert sup.iterations_used == 1 and sup.iterations_used_charged == 1
    assert sup._infra_wait_total_s == 0.0
    # the notifier hears both ends, worded for a preempt
    assert len(rec["notes"]) == 2, rec["notes"]
    assert "at 96%" in rec["notes"][0] and "not starting it" in rec["notes"][0]
    assert "starting worker" in rec["notes"][1]
    # the report that caused it is forgotten: the window has reset
    assert sup._rate_limit_state["claude-code"] is None


@pytest.mark.asyncio
async def test_a_top_level_utilization_preempts_too(tmp_path):
    sup, rec = _harness(tmp_path, [_step(_ok())],
                        state={"claude-code": _warning(0.97, top_level=True)})
    await sup.run_iteration("worker")
    assert rec["waits"] == [1200.0]


@pytest.mark.asyncio
async def test_exactly_the_threshold_preempts(tmp_path):
    sup, rec = _harness(tmp_path, [_step(_ok())],
                        state={"claude-code": _warning(0.95)})
    await sup.run_iteration("worker")
    assert rec["waits"] == [1200.0]


@pytest.mark.asyncio
async def test_a_warning_at_90_percent_does_not_preempt(tmp_path):
    sup, rec = _harness(tmp_path, [_step(_ok())],
                        state={"claude-code": _warning(0.90)})
    await sup.run_iteration("worker")
    assert rec["waits"] == []
    assert rec["calls"] == 1
    assert _events(sup, "rate_limit_wait_started") == []


@pytest.mark.asyncio
async def test_the_threshold_is_the_configured_one(tmp_path):
    sup, rec = _harness(tmp_path, [_step(_ok())],
                        state={"claude-code": _warning(0.90)},
                        rate_limit_preempt_utilization=0.85)
    await sup.run_iteration("worker")
    assert rec["waits"] == [1200.0]


@pytest.mark.asyncio
async def test_no_known_state_never_preempts(tmp_path):
    sup, rec = _harness(tmp_path, [_step(_ok())])
    assert sup._rate_limit_state == {}
    await sup.run_iteration("worker")
    assert rec["waits"] == []
    assert rec["calls"] == 1


@pytest.mark.asyncio
async def test_an_allowed_report_never_preempts(tmp_path):
    info = _warning(0.99)
    info["status"] = "allowed"
    sup, rec = _harness(tmp_path, [_step(_ok())], state={"claude-code": info})
    await sup.run_iteration("worker")
    assert rec["waits"] == []


@pytest.mark.asyncio
async def test_a_window_that_has_already_reset_does_not_preempt(tmp_path):
    sup, rec = _harness(tmp_path, [_step(_ok())],
                        state={"claude-code": _warning(0.99, resets_at=NOW - 5)})
    await sup.run_iteration("worker")
    assert rec["waits"] == []


@pytest.mark.asyncio
async def test_a_pi_hat_never_preempts(tmp_path):
    """F.5: rate-limit state is per harness. A pi hat starts at once even
    while the claude-code harness's window is nearly spent."""
    sup, rec = _harness(tmp_path, [_step(_ok(harness="pi"))], harnesses={},
                        state={"claude-code": _warning(0.99)})
    assert sup.cfg.harness_for("worker") == "pi"
    await sup.run_iteration("worker")
    assert rec["waits"] == []
    assert rec["calls"] == 1
    assert sup._rate_limit_state["claude-code"]["status"] == "allowed_warning"


@pytest.mark.asyncio
async def test_a_warning_reported_by_one_iteration_preempts_the_next(tmp_path):
    """The state the preempt reads is what the previous claude-code iteration
    reported (`_run_iteration_once`), not something seeded by hand."""
    sup, rec = _harness(tmp_path, [_step(_ok(rate_limit=_warning(0.96))),
                                   _step(_ok())])
    await sup.run_iteration("worker")
    assert rec["waits"] == []
    await sup.run_iteration("worker")
    assert rec["waits"] == [1200.0]
    assert rec["during"][0][0] == 1, "the wait came before the second attempt"


@pytest.mark.asyncio
async def test_the_ceiling_applies_to_a_preempt(tmp_path):
    sup, rec = _harness(tmp_path, [_step(_ok())],
                        state={"claude-code": _warning(0.99)},
                        rate_limit_max_wait_s=600)
    result = await sup.run_iteration("worker")
    assert rec["waits"] == []
    (parked,) = rec["parked"]
    assert parked["pauseDetail"]["reason"] == RATE_LIMIT_CEILING_REASON
    assert parked["pauseDetail"]["plannedS"] == 1200.0
    assert _events(sup, "rate_limit_ceiling_reached")[0]["reason"] == "preempt"
    # the unpause re-probes: the iteration starts
    assert result.final_text == "done" and rec["calls"] == 1


@pytest.mark.asyncio
async def test_an_abort_during_a_preempt_wait_starts_nothing(tmp_path):
    sup, rec = _harness(tmp_path, [_step(_ok())],
                        state={"claude-code": _warning(0.99)})

    async def wait(seconds):
        sup.abort("operator says stop")
        return 3.0, False

    sup._wait_out_backoff = wait                         # type: ignore[method-assign]
    result = await sup.run_iteration("worker")
    assert rec["calls"] == 0
    assert result.interrupted and result.error_message == "aborted"
    assert result.harness == "claude-code"
    assert sup.iterations_used == 0
    assert _events(sup, "rate_limit_wait_ended")[0]["wokenBy"] == "abort"
