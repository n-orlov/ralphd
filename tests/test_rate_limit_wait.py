"""v0.30 F.1/F.2: a rate limit is a wait, not a failure.

A `claude-code` iteration the provider rate-limited is classified
`rate_limit`, refunded, and re-run with the same work once the window resets.
The wait goes through `_wait_out_backoff` (recorded here, never slept), the
clock is the `_now_epoch` seam and the jitter is pinned to 0, so every number
below is exact.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

import pytest

from ralphd.engine.faults import FAULT_CLASS_RATE_LIMIT, rate_limit_resets_at
from ralphd.engine.runner import ClaudeCodeRunner, IterationResult
from ralphd.engine.state import parse_utc, utc_from_epoch
from tests.conftest import ScriptedRunner, make_supervisor

NOW = 1_790_000_000.0
RL_ERROR = ('429 rate_limit: API Error: 429 {"type":"error","error":'
            '{"type":"rate_limit_error","message":"rate limited"}}')


def _rate_limited(resets_at=NOW + 600, rl_type="five_hour") -> IterationResult:
    info = {"status": "rejected", "rateLimitType": rl_type,
            "unifiedWindows": {rl_type: {}}}
    if resets_at is not None:
        info["resetsAt"] = resets_at
        info["unifiedWindows"][rl_type]["resetsAt"] = resets_at
    r = IterationResult(exit_code=1, harness="claude-code", rate_limit=info,
                        rate_limit_fresh=True, http_status=429,
                        error_type="rate_limit")
    r.error_message = RL_ERROR
    r.duration_s = 2.0
    return r


def _ok() -> IterationResult:
    r = IterationResult(exit_code=0, harness="claude-code")
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


def _harness(tmp_path, steps, **cfg):
    """A supervisor whose attempts come from `steps`, with the clock fixed at
    NOW, no jitter, and a recording `_wait_out_backoff` that also captures
    status.json while the wait is in progress."""
    sup = make_supervisor(tmp_path, runner=ScriptedRunner({"worker": steps}),
                          prd=True, job_timeout_s=3600, **cfg)
    sup._now_epoch = lambda: NOW                         # type: ignore[method-assign]
    sup._rate_limit_jitter_s = lambda: 0.0               # type: ignore[method-assign]
    rec = {"waits": [], "during": [], "calls": [], "notes": []}

    async def wait(seconds):
        rec["waits"].append(seconds)
        rec["during"].append(_status(sup).get("rateLimitWait"))
        return seconds, False

    sup._wait_out_backoff = wait                         # type: ignore[method-assign]
    sup.notifier = rec["notes"].append
    real_once = sup._run_iteration_once

    async def once(phase, extra="", prompt_name=None, tier=None, task_id=None):
        rec["calls"].append((phase, extra, prompt_name, tier, task_id))
        return await real_once(phase, extra, prompt_name, tier, task_id=task_id)

    sup._run_iteration_once = once                       # type: ignore[method-assign]
    return sup, rec


@pytest.mark.asyncio
async def test_a_rejected_window_waits_to_the_reset_and_reruns_the_same_work(tmp_path):
    sup, rec = _harness(tmp_path, [_step(_rate_limited()), _step(_ok())])
    deadline, epoch = sup.deadline, sup._deadline_epoch
    assert sup.iterations_used == 0

    result = await sup.run_iteration("worker", "EXTRA", "worker", "standard",
                                     task_id="T7")

    assert result.final_text == "done"
    # the class, recorded in the rate-limited iteration's meta.json
    meta1 = json.loads((sup.run.iteration_dir(1) / "meta.json").read_text())
    assert meta1["faultClass"] == FAULT_CLASS_RATE_LIMIT
    assert meta1["rateLimit"]["status"] == "rejected"
    assert meta1["rateLimitFresh"] is True
    assert meta1["httpStatus"] == 429 and meta1["errorType"] == "rate_limit"
    # the wait asked for is exactly resetsAt - now
    assert rec["waits"] == [600.0]
    # same work re-run
    assert rec["calls"] == [("worker", "EXTRA", "worker", "standard", "T7")] * 2
    # the deadline moved by the waited span
    assert sup.deadline == pytest.approx(deadline + 600)
    assert sup._deadline_epoch == pytest.approx(epoch + 600)
    status = _status(sup)
    assert parse_utc(status["deadlineAt"]) == pytest.approx(epoch + 600, abs=1.5)
    assert status["rateLimitWaitTotalS"] == pytest.approx(600)
    ext = _events(sup, "deadline_extended")
    assert [(e["reason"], e["waitedS"]) for e in ext] == [("rate limit wait", 600.0)]
    # the rate-limited attempt is refunded: two attempts ran, one is charged
    assert sup.iterations_used == 2
    assert sup.iterations_used_charged == 1
    assert status["iterationsUsed"] == 1
    assert status["iterationsRefunded"]["infra"] == 1
    # the infra outage counters are untouched
    assert sup._infra_episode_waited_s == 0.0
    assert sup._infra_wait_total_s == 0.0
    assert status.get("infraWaitTotalS", 0) == 0
    assert _events(sup, "infra_retry") == []
    # the per-harness state keeps the last report
    assert sup._rate_limit_state["claude-code"]["status"] == "rejected"
    # the ceiling counter
    assert sup._rate_limit_waited_s == pytest.approx(600)


@pytest.mark.asyncio
async def test_the_wait_is_published_while_it_lasts_and_cleared_after(tmp_path):
    sup, rec = _harness(tmp_path, [_step(_rate_limited()), _step(_ok())])
    await sup.run_iteration("worker")
    assert rec["during"] == [{"type": "five_hour",
                              "resetsAt": utc_from_epoch(NOW + 600),
                              "since": utc_from_epoch(NOW)}]
    assert _status(sup)["rateLimitWait"] is None
    started = _events(sup, "rate_limit_wait_started")
    ended = _events(sup, "rate_limit_wait_ended")
    assert len(started) == 1 and started[0]["rateLimitType"] == "five_hour"
    assert started[0]["resetsAt"] == utc_from_epoch(NOW + 600)
    assert len(ended) == 1 and ended[0]["wokenBy"] == "reset"
    assert ended[0]["waitedS"] == 600.0


@pytest.mark.asyncio
async def test_the_notifier_hears_the_start_and_the_end(tmp_path):
    sup, rec = _harness(tmp_path, [_step(_rate_limited()), _step(_ok())])
    await sup.run_iteration("worker")
    assert len(rec["notes"]) == 2, rec["notes"]
    start, end = rec["notes"]
    assert "rate limit (five_hour)" in start and "waiting until" in start
    assert "rate-limit wait over" in end and "re-running worker" in end


@pytest.mark.asyncio
async def test_an_iso_reset_gives_the_same_wait(tmp_path):
    iso = utc_from_epoch(NOW + 600)
    sup, rec = _harness(tmp_path, [_step(_rate_limited(resets_at=iso)), _step(_ok())])
    await sup.run_iteration("worker")
    assert rec["waits"] == [600.0]


@pytest.mark.asyncio
async def test_the_jitter_is_added_to_the_reset(tmp_path):
    sup, rec = _harness(tmp_path, [_step(_rate_limited()), _step(_ok())])
    sup._rate_limit_jitter_s = lambda: 42.0              # type: ignore[method-assign]
    await sup.run_iteration("worker")
    assert rec["waits"] == [642.0]
    # the published reset is the provider's own
    assert rec["during"][0]["resetsAt"] == utc_from_epoch(NOW + 600)


def test_the_default_jitter_is_between_0_and_60_seconds(tmp_path):
    sup = make_supervisor(tmp_path)
    for _ in range(50):
        assert 0.0 <= sup._rate_limit_jitter_s() <= 60.0


@pytest.mark.asyncio
async def test_no_reset_known_waits_15_minutes_and_reprobes(tmp_path):
    sup, rec = _harness(tmp_path, [_step(_rate_limited(resets_at=None)),
                                   _step(_ok())])
    await sup.run_iteration("worker")
    assert rec["waits"] == [900.0]
    assert len(rec["calls"]) == 2
    assert rec["during"][0]["resetsAt"] == utc_from_epoch(NOW + 900)


@pytest.mark.asyncio
async def test_an_error_type_alone_still_waits(tmp_path):
    r = IterationResult(exit_code=1, harness="claude-code", error_type="rate_limit")
    r.error_message = RL_ERROR
    sup, rec = _harness(tmp_path, [_step(r), _step(_ok())])
    await sup.run_iteration("worker")
    assert rec["waits"] == [900.0]
    assert rec["during"][0]["type"] is None


@pytest.mark.asyncio
async def test_an_abort_during_the_wait_returns(tmp_path):
    sup, rec = _harness(tmp_path, [_step(_rate_limited()), _step(_ok())])

    async def wait(seconds):
        rec["waits"].append(seconds)
        sup.abort("operator says stop")
        return 5.0, False

    sup._wait_out_backoff = wait                         # type: ignore[method-assign]
    result = await sup.run_iteration("worker")
    assert result.error_message == RL_ERROR
    assert len(rec["calls"]) == 1, "an aborted wait never re-runs the work"
    assert _events(sup, "rate_limit_wait_ended")[0]["wokenBy"] == "abort"
    assert _status(sup)["rateLimitWait"] is None
    # the 5 s actually waited are still credited
    assert _events(sup, "deadline_extended")[0]["waitedS"] == 5.0


@pytest.mark.asyncio
async def test_a_steer_wake_resumes_the_rest_of_the_wait(tmp_path):
    sup, rec = _harness(tmp_path, [_step(_rate_limited()), _step(_ok())])
    seen: list[float] = []

    async def wait(seconds):
        seen.append(seconds)
        return (100.0, False) if len(seen) == 1 else (seconds, False)

    sup._wait_out_backoff = wait                         # type: ignore[method-assign]
    await sup.run_iteration("worker")
    assert seen == [600.0, 500.0]
    assert len(rec["calls"]) == 2
    assert _events(sup, "deadline_extended")[0]["waitedS"] == 600.0


@pytest.mark.asyncio
async def test_a_stale_rejected_report_is_not_waited_on(tmp_path):
    """A `rejected` carried over from an earlier iteration (not fresh) does
    not make a later work failure a rate limit."""
    r = IterationResult(exit_code=1, harness="claude-code",
                        rate_limit=_rate_limited().rate_limit,
                        rate_limit_fresh=False)
    r.error_message = "tests failed"
    r.usage = {"input": 10, "output": 5, "totalTokens": 15}
    r.duration_s = 30.0
    sup, rec = _harness(tmp_path, [_step(r)])
    result = await sup.run_iteration("worker")
    assert result is not None
    assert rec["waits"] == []
    assert sup._last_fault_class == "work"


def test_a_pi_text_429_is_still_an_infra_fault(tmp_path):
    sup = make_supervisor(tmp_path)
    r = IterationResult(exit_code=0)
    r.error_message = "429 Too Many Requests: rate limit exceeded"
    assert sup._classify_result(r) == "infra"


def test_the_rate_limit_total_survives_a_resume(tmp_path):
    make_supervisor(tmp_path, status={"rateLimitWaitTotalS": 120.5})
    resumed = make_supervisor(tmp_path)
    assert resumed._rate_limit_wait_total_s == 120.5


# -- end to end: the stub CLI, the real wrapper and runner, the loop's verdict --

STUB = Path(__file__).resolve().parent / "stub-claude" / "claude"


def _stub_iteration(tmp_path, monkeypatch, runner, name, **stub):
    monkeypatch.setenv("RALPHD_CLAUDE_CLI_PATH", str(STUB))
    for k in [k for k in os.environ if k.startswith("STUB_CLAUDE_")]:
        monkeypatch.delenv(k)
    for k, v in stub.items():
        monkeypatch.setenv(k, v)
    itdir = tmp_path / name
    itdir.mkdir()
    return asyncio.run(runner.run("the prompt", itdir / "output.jsonl",
                                  model="anthropic/claude-sonnet-5-5",
                                  timeout_s=60))


def test_a_stub_rate_limited_iteration_classifies_rate_limit(tmp_path, monkeypatch):
    """The stub's `rate_limited` scenario, through the wrapper and
    `ClaudeCodeRunner`, is a `rate_limit` verdict in `_classify_result`; the
    ISO `resetsAt` it reports parses to the reset the wait would target. The
    next clean iteration carries the old report, not fresh, and is no fault."""
    ws = tmp_path / "ws"
    ws.mkdir()
    runner = ClaudeCodeRunner(ws)
    sup = make_supervisor(tmp_path / "sup")
    iso = "2026-10-02T15:00:00Z"
    result = _stub_iteration(tmp_path, monkeypatch, runner, "one",
                             STUB_CLAUDE_SCENARIO="rate_limited",
                             STUB_CLAUDE_RL_RESETS_AT=iso)
    assert sup._classify_result(result) == FAULT_CLASS_RATE_LIMIT
    assert rate_limit_resets_at(result.rate_limit) == 1790953200.0
    clean = _stub_iteration(tmp_path, monkeypatch, runner, "two")
    assert clean.rate_limit == result.rate_limit and not clean.rate_limit_fresh
    assert sup._classify_result(clean) is None
