"""v0.30 F.3: a ceiling on the run's total rate-limit waiting, then the
operator.

`JobConfig.rate_limit_max_wait_s` bounds the waiting. A rate-limit wait that
would take the run past it does not start: the run pauses
`operator_input_needed` with `pauseDetail.reason == "rate_limit_ceiling"`,
tells the notifier when the window resets, and parks. An unpause resets the
count and re-probes the same work. The review-pause machinery (the one-shot
no-ruling proceed, a ruling releasing the pause) stays away from it.

Waits go through the recorded `_wait_out_backoff` stand-in (never slept), the
clock is the `_now_epoch` seam and the jitter is 0, so every number is exact.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from ralphd.engine import state
from ralphd.engine.faults import FAULT_CLASS_RATE_LIMIT, RATE_LIMIT_CEILING_REASON
from ralphd.engine.runner import IterationResult
from ralphd.engine.state import utc_from_epoch
from tests.conftest import ScriptedRunner, make_supervisor

NOW = 1_790_000_000.0
RL_ERROR = ('429 rate_limit: API Error: 429 {"type":"error","error":'
            '{"type":"rate_limit_error","message":"rate limited"}}')


def _rate_limited(resets_at=NOW + 600, rl_type="five_hour") -> IterationResult:
    info = {"status": "rejected", "rateLimitType": rl_type, "resetsAt": resets_at,
            "unifiedWindows": {rl_type: {"resetsAt": resets_at}}}
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


def _harness(tmp_path, steps, *, on_park=None, **cfg):
    """A supervisor whose worker attempts come from `steps`, the clock fixed
    at NOW with no jitter, waits recorded and not slept, and a park that
    records the run while parked and then runs `on_park(sup)` (default: an
    unpause, as `ralphctl unpause` does)."""
    sup = make_supervisor(tmp_path, runner=ScriptedRunner({"worker": steps}),
                          prd=True, job_timeout_s=3600, **cfg)
    sup._now_epoch = lambda: NOW                         # type: ignore[method-assign]
    sup._rate_limit_jitter_s = lambda: 0.0               # type: ignore[method-assign]
    rec = {"waits": [], "calls": [], "notes": [], "parked": [],
           "waited_at_park": []}

    async def wait(seconds):
        rec["waits"].append(seconds)
        return seconds, False

    async def park():
        rec["parked"].append(_status(sup))
        rec["waited_at_park"].append(sup._rate_limit_waited_s)
        (on_park or (lambda s: s.resume()))(sup)
        return 30.0

    sup._wait_out_backoff = wait                         # type: ignore[method-assign]
    sup._park_until_resumed = park                       # type: ignore[method-assign]
    sup.notifier = rec["notes"].append
    real_once = sup._run_iteration_once

    async def once(phase, extra="", prompt_name=None, tier=None, task_id=None):
        rec["calls"].append((phase, extra, prompt_name, tier, task_id))
        return await real_once(phase, extra, prompt_name, tier, task_id=task_id)

    sup._run_iteration_once = once                       # type: ignore[method-assign]
    return sup, rec


def test_the_ceiling_and_the_preempt_threshold_default_to_a_day_and_95_percent(tmp_path):
    sup = make_supervisor(tmp_path)
    assert sup.cfg.rate_limit_max_wait_s == 86400
    assert sup.cfg.rate_limit_preempt_utilization == 0.95


@pytest.mark.asyncio
async def test_a_wait_crossing_the_ceiling_pauses_and_unpause_reruns_the_same_work(tmp_path):
    sup, rec = _harness(
        tmp_path,
        [_step(_rate_limited()), _step(_rate_limited()), _step(_ok())],
        rate_limit_max_wait_s=1000)

    result = await sup.run_iteration("worker", "EXTRA", "worker", "standard",
                                     task_id="T7")

    # the first 600 s wait fits under 1000; the second would make 1200
    assert rec["waits"] == [600.0]
    (parked,) = rec["parked"]
    assert parked["state"] == "paused"
    assert parked["pauseReason"] == "operator_input_needed"
    detail = parked["pauseDetail"]
    assert detail == {"reason": RATE_LIMIT_CEILING_REASON,
                      "resetsAt": utc_from_epoch(NOW + 600),
                      "waitedS": 600.0, "maxWaitS": 1000, "plannedS": 600.0,
                      "rateLimitType": "five_hour", "phase": "worker"}
    # the second wait never started
    assert len(_events(sup, "rate_limit_wait_started")) == 1
    (reached,) = _events(sup, "rate_limit_ceiling_reached")
    assert reached["waitedS"] == 600.0 and reached["maxWaitS"] == 1000
    assert reached["resetsAt"] == utc_from_epoch(NOW + 600)
    assert reached["reason"] == "rate_limit"
    # never an operator_escalation (that shape routes a restart into review)
    assert _events(sup, "operator_escalation") == []
    # the notifier names the reset in local time
    ceiling_notes = [n for n in rec["notes"] if "ceiling" in n]
    assert len(ceiling_notes) == 1
    note = ceiling_notes[0]
    assert state.format_local_time(utc_from_epoch(NOW + 600)) in note
    assert f"ralphctl unpause {sup.cfg.run_id}" in note
    assert "--rate-limit-wait" in note
    # the unpause reset the count and re-probed the same work, which succeeded
    assert result.final_text == "done"
    assert rec["calls"] == [("worker", "EXTRA", "worker", "standard", "T7")] * 3
    assert sup._rate_limit_waited_s == 0.0
    status = _status(sup)
    assert status["state"] == "running"
    assert status["pauseReason"] is None and status["pauseDetail"] is None
    # both rate-limited attempts are refunded
    assert sup.iterations_used == 3 and sup.iterations_used_charged == 1
    # the parked time extends the deadline as an operator pause
    assert [e["reason"] for e in _events(sup, "deadline_extended")] == [
        "rate limit wait", "operator pause"]


@pytest.mark.asyncio
async def test_after_the_unpause_the_count_starts_again(tmp_path):
    """The reset is real: after the ceiling unpause, a further 600 s wait
    fits under the 1000 s ceiling again and is taken, not paused on."""
    sup, rec = _harness(
        tmp_path,
        [_step(_rate_limited()), _step(_rate_limited()), _step(_rate_limited()),
         _step(_ok())],
        rate_limit_max_wait_s=1000)
    await sup.run_iteration("worker")
    assert rec["waits"] == [600.0, 600.0]
    assert len(rec["parked"]) == 1
    assert rec["waited_at_park"] == [600.0]
    assert sup._rate_limit_waited_s == 600.0


@pytest.mark.asyncio
async def test_a_single_wait_longer_than_the_ceiling_never_starts(tmp_path):
    sup, rec = _harness(tmp_path, [_step(_rate_limited()), _step(_ok())],
                        rate_limit_max_wait_s=300)
    await sup.run_iteration("worker")
    assert rec["waits"] == []
    assert rec["parked"][0]["pauseDetail"]["waitedS"] == 0.0
    assert _events(sup, "rate_limit_wait_started") == []
    # nothing waited, so no rate-limit deadline extension
    assert [e["reason"] for e in _events(sup, "deadline_extended")] == [
        "operator pause"]


@pytest.mark.asyncio
async def test_unpause_grants_no_proceed_and_a_ruling_does_not_release(tmp_path):
    seen = {}

    def on_park(sup):
        sup.run.add_ruling("a ruling filed against the hold")
        seen["paused_after_rule"] = _status(sup)["state"]
        sup.resume()

    sup, _rec = _harness(tmp_path, [_step(_rate_limited()), _step(_ok())],
                         on_park=on_park, rate_limit_max_wait_s=300)
    sup._approach = 1
    sup.run.update_status(approach=1, rejectionSignatures={"1": "sig-abc"})
    await sup.run_iteration("worker")
    # v0.36 D.1: a ruling is recorded but releases nothing.
    assert seen == {"paused_after_rule": "paused"}
    assert len(sup.run.ruling_entries()) == 1
    assert not _status(sup).get("noRulingProceed")


@pytest.mark.asyncio
async def test_an_abort_while_parked_returns_the_rate_limited_result(tmp_path):
    def on_park(sup):
        sup.abort("operator says stop")
        sup.resume()

    limited = _rate_limited()
    sup, rec = _harness(tmp_path, [_step(limited), _step(_ok())],
                        on_park=on_park, rate_limit_max_wait_s=300)
    result = await sup.run_iteration("worker")
    assert result is limited
    assert len(rec["calls"]) == 1, "an aborted pause never re-runs the work"
    assert _status(sup)["pauseDetail"] is None


@pytest.mark.asyncio
async def test_a_real_park_waits_for_unpause(tmp_path):
    """No stubbed park: the iteration really sits in `_gate()` until
    `resume()`, and only then re-probes."""
    sup = make_supervisor(tmp_path, prd=True, job_timeout_s=3600,
                          real_sleep=True, rate_limit_max_wait_s=300)
    sup._now_epoch = lambda: NOW                         # type: ignore[method-assign]
    sup._rate_limit_jitter_s = lambda: 0.0               # type: ignore[method-assign]
    sup.runner = ScriptedRunner({"worker": [_step(_rate_limited()), _step(_ok())]})
    sup.runner.sup = sup
    task = asyncio.create_task(sup.run_iteration("worker"))
    for _ in range(500):
        await asyncio.sleep(0.01)
        if _status(sup).get("state") == "paused":
            break
    assert _status(sup)["pauseDetail"]["reason"] == RATE_LIMIT_CEILING_REASON
    await asyncio.sleep(0.05)
    assert not task.done(), "a run past its ceiling waits for the operator"
    assert sup.runner.calls == {"worker": 1}
    sup.resume()
    result = await asyncio.wait_for(task, timeout=10)
    assert result.final_text == "done"
    assert sup.runner.calls == {"worker": 2}


@pytest.mark.asyncio
async def test_a_raised_ceiling_lets_the_wait_start(tmp_path):
    """`set_rate_limit_max_wait` (what `ralphctl budget --rate-limit-wait`
    reaches) is read before the next wait."""
    sup, rec = _harness(tmp_path, [_step(_rate_limited()), _step(_ok())],
                        rate_limit_max_wait_s=300)
    sup.set_rate_limit_max_wait(3600)
    assert _status(sup)["rateLimitMaxWaitS"] == 3600
    await sup.run_iteration("worker")
    assert rec["waits"] == [600.0]
    assert rec["parked"] == []


# -- what the operator reads ---------------------------------------------------

def _ceiling_detail():
    return {"reason": RATE_LIMIT_CEILING_REASON,
            "resetsAt": utc_from_epoch(NOW + 600), "waitedS": 600.0,
            "maxWaitS": 1000, "plannedS": 600.0, "rateLimitType": "five_hour",
            "phase": "worker"}


def test_the_pause_line_names_the_count_and_the_reset():
    line = state.format_pause_detail_line(_ceiling_detail())
    assert line == (
        "paused:    rate-limit ceiling reached (10m waited of 16m 40s); the "
        f"five_hour window resets at "
        f"{state.format_local_time(utc_from_epoch(NOW + 600))} -- ralphctl "
        "unpause resets the count, ralphctl budget --rate-limit-wait raises "
        "the ceiling")


class _Args:
    def __init__(self, run_id: str):
        self.run_id = run_id
        self.json = False


def test_ralphctl_status_and_fault_print_the_ceiling_pause(tmp_path, monkeypatch, capsys):
    from ralphd.cli import main as cli_main

    registry = tmp_path / "registry"
    monkeypatch.setenv("RALPHD_REGISTRY", str(registry))
    monkeypatch.setattr(cli_main, "_dangling_run_entry", lambda run_id: None)
    root = registry / "runs" / "ceiling"
    root.mkdir(parents=True)
    (root / "status.json").write_text(json.dumps({
        "runId": "ceiling", "state": "paused", "phase": "worker",
        "pauseReason": "operator_input_needed",
        "pauseDetail": _ceiling_detail(),
        "iterationsUsed": 1, "iterationsBudget": 6,
        "startedAt": "2026-10-02T14:00:00Z"}))
    line = state.format_pause_detail_line(_ceiling_detail())

    cli_main.cmd_status(_Args("ceiling"))
    out = capsys.readouterr().out
    assert [ln for ln in out.splitlines() if ln.startswith("paused:")] == [line]

    cli_main.cmd_fault(_Args("ceiling"))
    out = capsys.readouterr().out
    assert f"fault:     {FAULT_CLASS_RATE_LIMIT} (phase worker)" in out.splitlines()
    assert line in out.splitlines()
