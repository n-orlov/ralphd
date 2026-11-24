"""v0.39 (#321, C): a live iteration whose running cost reaches a spend cap is
interrupted, in the pi, claude-code and codex runners.

Each runner is driven through its real spawn path against a fake wrapper that
streams pi-shaped `message_end` events with injected usage, writes a file in
the workspace, and then keeps running (it would stream a fourth message and
more work if nothing stopped it). No real model, no real dollar, no clock.
"""

from __future__ import annotations

import asyncio
import json
import stat
import sys
import time
from pathlib import Path

import pytest

from ralphd.engine import spend_caps
from ralphd.engine.pricing import PricingMap
from ralphd.engine.runner import (
    COMPLETE,
    ClaudeCodeRunner,
    CodexRunner,
    IterationResult,
    PiRunner,
)
from ralphd.engine.state import RunDir, atomic_write_json
from tests.conftest import ScriptedRunner, scripted_supervisor

# $0.40 a message: 100k output tokens at $4 per million.
PER_MESSAGE_USD = 0.4
TOKENS = {"input": 0, "output": 100_000, "cacheRead": 0, "cacheWrite": 0,
          "totalTokens": 100_000}
MODELS = {"pi": "openai/gpt-5", "claude-code": "anthropic/claude-sonnet-5-5",
          "codex": "openai/gpt-5"}
PRICING = PricingMap.from_config({"models": {
    "openai/gpt-5": {"input": 1.0, "output": 4.0},
    "anthropic/claude-sonnet-5-5": {"input": 1.0, "output": 4.0}}})
HARNESSES = ("pi", "claude-code", "codex")


def _message(text: str, *, quoted: bool) -> dict:
    usage = dict(TOKENS)
    if quoted:
        usage["cost"] = {"total": PER_MESSAGE_USD}
    return {"type": "message_end", "message": {
        "role": "assistant", "content": [{"type": "text", "text": text}],
        "usage": usage}}


def _fake_wrapper(tmp_path: Path, messages: list[dict], *, linger: bool) -> Path:
    """An executable that ignores its argv, writes `worker-file.txt` in its
    cwd (the workspace), streams every message at once, and then lingers
    (`linger`) the way a worker that is still going would."""
    script = tmp_path / "fake-wrapper"
    script.write_text(
        f"#!{sys.executable}\n"
        "import os, signal, sys, time\n"
        "signal.signal(signal.SIGINT, signal.SIG_DFL)\n"
        "sys.stdin.read()\n"
        "open('worker-file.txt', 'w').write('edited before the cap\\n')\n"
        f"for line in {[json.dumps(m) for m in messages]!r}:\n"
        "    print(line, flush=True)\n"
        f"if {linger!r}:\n"
        "    time.sleep(120)\n")
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    return script


def _runner(harness: str, tmp_path: Path, wrapper: Path):
    ws = tmp_path / "ws"
    ws.mkdir(exist_ok=True)
    if harness == "pi":
        return PiRunner(ws, pi_bin=str(wrapper), pricing=PRICING)
    cls = ClaudeCodeRunner if harness == "claude-code" else CodexRunner
    return cls(ws, python_bin=str(wrapper), pricing=PRICING)


def _stream(harness: str, count: int, *, claim_last: bool) -> list[dict]:
    # pi quotes a price per message; the wrappers' runners price from tokens.
    quoted = harness == "pi"
    texts = [f"working, message {i + 1}" for i in range(count)]
    if claim_last:
        texts[-1] = f"all done\n{COMPLETE}"
    return [_message(t, quoted=quoted) for t in texts]


def _run(runner, tmp_path: Path, harness: str, watch=None) -> IterationResult:
    itdir = tmp_path / "it"
    itdir.mkdir(exist_ok=True)
    kw = {"spend_watch": watch} if watch is not None else {}
    return asyncio.run(asyncio.wait_for(runner.run(
        "the prompt", itdir / "output.jsonl", model=MODELS[harness],
        timeout_s=60, **kw), 90))


def _watch(events: list, caps: dict, hat: str = "worker",
           prior: dict | None = None) -> spend_caps.SpendWatch:
    return spend_caps.SpendWatch(
        caps=caps, hat=hat, prior=prior or {},
        emit=lambda event, **fields: events.append({"type": event, **fields}))


def _caps(**levels) -> dict:
    return {level: {hat: {"usd": usd, "source": "flag"}
                    for hat, usd in entries.items()}
            for level, entries in levels.items()}


# -- a stream that crosses the cap on its third message -----------------------

@pytest.mark.parametrize("harness", HARNESSES)
def test_a_cap_crossed_on_the_third_message_stops_the_iteration_after_it(
        tmp_path, harness):
    # four messages are on the wire; the third carries the completion claim
    stream = _stream(harness, 4, claim_last=False)
    stream[2] = _message(f"all done\n{COMPLETE}", quoted=harness == "pi")
    runner = _runner(harness, tmp_path, _fake_wrapper(tmp_path, stream, linger=True))
    events: list[dict] = []
    # $0.40 a message: $0.80 after two (under $1.00), $1.20 after three
    t0 = time.monotonic()
    result = _run(runner, tmp_path, harness,
                  _watch(events, _caps(overall={"worker": 1.0})))
    assert time.monotonic() - t0 < 25, "the kill path must not wait out the child"

    # stopped after the third message: the fourth was never folded
    assert result.usage["output"] == 300_000
    assert result.spend_interrupted is True
    # the same verdict an iteration_timeout_s kill gives
    assert result.timed_out is True and result.interrupted is True
    # the completion claim the third message streamed is discarded
    assert not result.saw_complete and result.final_text == ""
    # the file the fake worker wrote stays in the workspace
    assert (tmp_path / "ws" / "worker-file.txt").read_text() == "edited before the cap\n"
    # no process of the fake worker survives
    assert runner.running is False


@pytest.mark.parametrize("harness", HARNESSES)
def test_the_interrupt_event_carries_the_hat_level_cap_spend_and_overshoot(
        tmp_path, harness):
    stream = _stream(harness, 4, claim_last=False)
    runner = _runner(harness, tmp_path, _fake_wrapper(tmp_path, stream, linger=True))
    events: list[dict] = []
    # $0.10 of the worker's cap is already spent: $0.10 + 3 x $0.40 = $1.30
    _run(runner, tmp_path, harness,
         _watch(events, _caps(overall={"worker": 1.0}),
                prior={"overall": {"worker": 0.1}}))
    assert len(events) == 1
    event = events[0]
    assert event["type"] == "spend.interrupted"
    assert event["hat"] == "worker" and event["level"] == "overall"
    assert event["cap"] == 1.0
    assert event["spend"] == pytest.approx(1.3)
    assert event["overshoot"] == pytest.approx(0.3)


@pytest.mark.parametrize("harness", HARNESSES)
def test_a_per_approach_and_a_total_cap_interrupt_with_their_own_level_and_hat(
        tmp_path, harness):
    stream = _stream(harness, 4, claim_last=False)
    events: list[dict] = []
    runner = _runner(harness, tmp_path, _fake_wrapper(tmp_path, stream, linger=True))
    _run(runner, tmp_path, harness,
         _watch(events, _caps(per_approach={"total": 1.0}),
                prior={"per_approach": {"total": 0.5}}))
    (event,) = events
    assert (event["hat"], event["level"], event["cap"]) == ("total", "per_approach", 1.0)
    assert event["spend"] == pytest.approx(1.3)
    assert event["overshoot"] == pytest.approx(0.3)


# -- a stream that stays under the cap ----------------------------------------

@pytest.mark.parametrize("harness", HARNESSES)
def test_a_stream_under_the_cap_runs_to_its_end_with_an_unchanged_result(
        tmp_path, harness):
    stream = _stream(harness, 2, claim_last=True)
    wrapper = _fake_wrapper(tmp_path, stream, linger=False)

    bare = _run(_runner(harness, tmp_path, wrapper), tmp_path, harness)
    events: list[dict] = []
    watched = _run(_runner(harness, tmp_path, wrapper), tmp_path, harness,
                   _watch(events, _caps(overall={"worker": 1.0, "total": 5.0},
                                        per_approach={"worker": 1.0})))

    assert events == []
    assert watched.spend_interrupted is False and watched.timed_out is False
    assert watched.exit_code == 0 and watched.saw_complete
    assert watched.final_text == bare.final_text and watched.final_text.endswith(COMPLETE)
    assert watched.usage == bare.usage and watched.usage["output"] == 200_000
    assert (tmp_path / "ws" / "worker-file.txt").exists()


# -- the running cost -------------------------------------------------------------

def test_the_running_cost_of_an_unpriced_claude_code_iteration_is_its_tokens_at_the_chain_rate():
    usage = {"input": 1_000_000, "output": 1_000_000, "costBasis": "subscription",
             "costUSD": 0.0, "costFree": True}
    assert spend_caps.running_spend(usage, "anthropic/claude-sonnet-5-5", PRICING) == 5.0


def test_a_priced_iteration_runs_at_exactly_the_cost_it_folded():
    usage = {"input": 10, "output": 10, "costUSD": 0.7, "costPriced": True}
    assert spend_caps.running_spend(usage, "openai/gpt-5", PRICING) == 0.7


def test_a_hat_without_a_cap_is_never_held_to_one():
    watch = _watch([], _caps(overall={"review": 1.0}))
    assert watch.observe(50.0) is None and watch.crossed is None


# -- the boundary after an interrupt (B.2) --------------------------------------------

_supervisor = scripted_supervisor(
    run_id="spend-interrupt", iterations=60, vigilant=False, on_complete="exit",
    infra_outage_budget_s=1000.0)


class _InterruptingRunner(ScriptedRunner):
    """A scripted runner that behaves like the real ones under a watch: each
    worker iteration streams `$cost` per message and is cut off, with the
    `timed_out` verdict, once the watch says a cap is reached."""

    def __init__(self, handlers, per_message: float, messages: int = 3,
                 tokens: bool = True):
        super().__init__(handlers)
        self.tokens = tokens
        self.per_message = per_message
        self.messages = messages
        self.watches: list = []

    async def run(self, prompt, transcript, **kw):
        phase = self.sup.run.read_status().get("phase")
        watch = kw.get("spend_watch")
        if phase != "worker":
            assert watch is None or phase in ("planning", "review")
            return await super().run(prompt, transcript, **kw)
        self.calls["worker"] = self.calls.get("worker", 0) + 1
        self.watches.append(watch)
        result = IterationResult(exit_code=0)
        result.duration_s = 5.0
        result.final_text = f"working\n{COMPLETE}"
        n = 1 if self.tokens else 0     # pi can quote dollars with no tokens
        result.usage = {"input": n, "output": n, "totalTokens": 2 * n,
                        "costUSD": 0.0}
        for _ in range(self.messages):
            result.usage["costUSD"] = round(
                result.usage["costUSD"] + self.per_message, 6)
            if watch is not None and watch.observe(result.usage["costUSD"]):
                result.spend_interrupted = True
                result.timed_out, result.interrupted = True, True
                result.exit_code, result.final_text = -9, ""
                break
        return result


def _plan(sup, prompt=""):
    atomic_write_json(sup.run.root / "plan-proposal.json", {
        "version": 1, "tasks": [{"id": "001", "title": "task 001",
                                 "successCriteria": "task 001 works"}]})
    r = IterationResult(exit_code=0)
    r.final_text = "planned\n" + json.dumps(
        {"proposal_path": "plan-proposal.json", "summary": "planned"})
    r.duration_s = 5.0
    r.usage = {"input": 1, "output": 1, "totalTokens": 2, "costUSD": 0.0}
    return r


def _drive(tmp_path, tokens=True, **cfg):
    root = tmp_path / "run"
    sup = _supervisor(root, {"planning": [_plan]}, **cfg)
    sup.runner = _InterruptingRunner({"planning": [_plan]}, per_message=0.4,
                                     tokens=tokens)
    sup.runner.sup = sup
    RunDir(root=root).update_status(state="starting")
    final = asyncio.run(asyncio.wait_for(sup._run_job_core(), 120))
    rows = [json.loads(x) for x in (root / "events.jsonl").read_text().splitlines()
            if x.strip()]
    return sup, final, json.loads((root / "status.json").read_text()), rows


def test_after_an_interrupt_an_overall_cap_ends_the_run_exhausted_at_the_boundary(tmp_path):
    sup, final, status, rows = _drive(
        tmp_path, spend_caps=_caps(overall={"worker": 1.0}), max_approaches=3)
    assert final == "exhausted", status
    assert status["exhaustedBudget"] == "spend:worker"
    # one worker iteration was interrupted on its third message ($1.20 of $1.00);
    # the boundary then refused the next dispatch: no second worker iteration
    assert sup.runner.calls["worker"] == 1
    (interrupted,) = [e for e in rows if e["type"] == "spend.interrupted"]
    assert (interrupted["hat"], interrupted["level"], interrupted["cap"]) == (
        "worker", "overall", 1.0)
    assert interrupted["spend"] == pytest.approx(1.2)
    assert interrupted["overshoot"] == pytest.approx(0.2)
    (refused,) = [e for e in rows if e["type"] == "spend.exhausted"]
    assert refused["hat"] == "worker" and refused["action"] == "exhausted"
    assert status["spend"]["overall"]["worker"] == pytest.approx(1.2)
    # the interrupt precedes the boundary's refusal
    assert rows.index(interrupted) < rows.index(refused)


def test_after_an_interrupt_a_per_approach_cap_replans_on_a_non_last_approach(tmp_path):
    sup, final, status, rows = _drive(
        tmp_path, spend_caps=_caps(per_approach={"worker": 1.0}), max_approaches=2)
    (interrupted, *_) = [e for e in rows if e["type"] == "spend.interrupted"]
    assert interrupted["level"] == "per_approach"
    assert interrupted["spend"] == pytest.approx(1.2)
    first = next(e for e in rows if e["type"] == "spend.exhausted")
    assert first["level"] == "per_approach" and first["action"] == "replan"
    # approach 2 starts its counters at zero, so its worker is interrupted
    # again; on the last approach the run ends `exhausted`
    assert final == "exhausted", status
    assert status["exhaustedBudget"] == "spend:worker:approach"
    assert sup.runner.calls["worker"] == 2
    assert status["spend"]["overall"]["worker"] == pytest.approx(2.4)


def test_a_cost_only_worker_interrupt_is_charged_and_never_retried_as_an_outage(
        tmp_path):
    # the cut reported $1.20 and no tokens: it still reached the model, so the
    # infra wrapper neither refunds nor re-runs it, and the boundary applies
    # the cap exactly as for a cut that carried tokens
    sup, final, status, rows = _drive(
        tmp_path, tokens=False, spend_caps=_caps(overall={"worker": 1.0}),
        max_approaches=3)
    assert final == "exhausted", status
    assert status["exhaustedBudget"] == "spend:worker"
    assert sup.runner.calls["worker"] == 1
    assert [e for e in rows if e["type"] == "infra_retry"] == []
    metas = [json.loads(p.read_text()) for p in sorted(
        (tmp_path / "run" / "iterations").glob("*/meta.json"))]
    (cut,) = [m for m in metas if m.get("spendInterrupted") is True]
    assert cut["phase"] == "worker" and cut["faultClass"] != "infra"
    assert status["iterationsUsed"] == len(metas)       # nothing refunded


def test_a_real_pi_cut_on_dollars_alone_is_classified_as_reaching_the_model(
        tmp_path):
    # pi quotes `usage.cost.total` with no token counts; the real watch still
    # crosses the reflect cap, and the classifier every retry wrapper reads
    # scores that cut as having reached the model, never as an infra outage
    stream = [{"type": "message_end", "message": {
        "role": "assistant", "content": [{"type": "text", "text": f"m{i}"}],
        "usage": {"cost": {"total": 0.5}}}} for i in range(4)]
    runner = _runner("pi", tmp_path, _fake_wrapper(tmp_path, stream, linger=True))
    events: list[dict] = []
    result = _run(runner, tmp_path, "pi",
                  _watch(events, _caps(overall={"reflect": 1.0}), hat="reflect"))
    assert result.spend_interrupted is True
    assert result.usage["totalTokens"] == 0
    assert result.usage["costUSD"] == pytest.approx(1.0)
    (interrupted,) = [e for e in events if e["type"] == "spend.interrupted"]
    assert interrupted["hat"] == "reflect"
    sup = _supervisor(tmp_path / "run", {})
    assert sup._classify_result(result) not in ("infra", None)
