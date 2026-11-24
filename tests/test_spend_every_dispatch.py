"""v0.39 (#321, B.2/C.1/C.3): EVERY model-bearing dispatch is held to the caps.

Not only a hat's first attempt: the internal retries of the infra, rate-limit
and refusal ladders, and the router and contract re-ask calls (uncounted as
iterations, but real model calls that cost real money) check the current hat
and total spend at both cap levels before a runner starts, run under the live
cap watch, and count what they cost. A spent cap means no further dispatch.

Hats are scripted (injected costs), and the router calls go through a REAL
`PiRunner` against a fake wrapper that streams priced messages.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from ralphd.engine.loop import _SpendCapSpent
from ralphd.engine.runner import COMPLETE, IterationResult, PiRunner
from tests.conftest import ScriptedRunner, make_supervisor
from tests.test_rate_limit_wait import _rate_limited
from tests.test_spend_enforcement import _plan
from tests.test_spend_enforcement import _run as _run_job
from tests.test_spend_enforcement import _work as _work_forever
from tests.test_spend_interrupt import (
    PRICING,
    _caps,
    _fake_wrapper,
    _message,
)

INFRA_ERROR = "502 Bad Gateway: upstream connect error"


def _paid(cost: float, *, error: str | None = None, text: str = "ok",
          **fields) -> IterationResult:
    r = IterationResult(exit_code=1 if error else 0, **fields)
    r.final_text = text
    r.error_message = error
    r.duration_s = 30.0
    r.usage = {"input": 10, "output": 5, "totalTokens": 15, "costUSD": cost}
    return r


def _step(result):
    return lambda sup, prompt="": result


def _harness(tmp_path, steps, **cfg):
    sup = make_supervisor(tmp_path, runner=ScriptedRunner({"worker": steps}),
                          prd=True, job_timeout_s=3600, **cfg)
    sup.runner.sup = sup
    sup._now_epoch = lambda: 1_790_000_000.0             # type: ignore[method-assign]
    sup._rate_limit_jitter_s = lambda: 0.0               # type: ignore[method-assign]
    waits: list[float] = []

    async def wait(seconds):
        waits.append(seconds)
        return seconds, False

    sup._wait_out_backoff = wait                         # type: ignore[method-assign]
    sup.notifier = None
    return sup, waits


def _events(sup, type_: str) -> list[dict]:
    path = Path(sup.run.root) / "events.jsonl"
    rows = [json.loads(x) for x in path.read_text().splitlines() if x.strip()]
    return [e for e in rows if e.get("type") == type_]


def _spend(sup) -> dict:
    return json.loads((Path(sup.run.root) / "status.json").read_text())["spend"]


def _run(sup, coro):
    return asyncio.run(asyncio.wait_for(coro, 60))


# -- a paid failing attempt, then an internal retry -------------------------------

def test_a_paid_infra_failure_that_spends_the_cap_is_not_retried(tmp_path):
    steps = [_step(_paid(1.2, error=INFRA_ERROR)), _step(_paid(0.1))]
    sup, waits = _harness(tmp_path, steps, spend_caps=_caps(overall={"worker": 1.0}))
    assert sup._classify_result(steps[0](sup)) == "infra"

    with pytest.raises(_SpendCapSpent) as raised:
        _run(sup, sup.run_iteration("worker"))

    assert sup.runner.calls["worker"] == 1, "the retry was dispatched past the cap"
    assert waits == [], "no backoff is sat out for a retry the cap refuses"
    assert raised.value.action == "exhausted"
    (event,) = _events(sup, "spend.exhausted")
    assert (event["hat"], event["level"], event["cap"], event["action"]) == (
        "worker", "overall", 1.0, "exhausted")
    assert event["spend"] == pytest.approx(1.2) and event["refusedHat"] == "worker"
    # the failed attempt's money is counted; its iteration slot is refunded
    assert _spend(sup)["overall"] == {"worker": pytest.approx(1.2),
                                      "total": pytest.approx(1.2)}
    assert sup.iterations_used_charged == 0


def test_a_paid_infra_failure_under_the_cap_is_still_retried(tmp_path):
    steps = [_step(_paid(0.3, error=INFRA_ERROR)), _step(_paid(0.3, text="done"))]
    sup, waits = _harness(tmp_path, steps, spend_caps=_caps(overall={"worker": 1.0}))
    result = _run(sup, sup.run_iteration("worker"))
    assert result.final_text == "done" and sup.runner.calls["worker"] == 2
    assert len(waits) == 1 and _events(sup, "spend.exhausted") == []


@pytest.mark.parametrize("level", ["overall", "per_approach"])
def test_a_total_cap_at_either_level_refuses_the_retry_of_any_hat(tmp_path, level):
    steps = [_step(_paid(0.7, error=INFRA_ERROR)), _step(_paid(0.7))]
    sup, _ = _harness(tmp_path, steps, spend_caps=_caps(**{level: {"total": 0.5}}))
    with pytest.raises(_SpendCapSpent):
        _run(sup, sup.run_iteration("worker"))
    assert sup.runner.calls["worker"] == 1
    (event,) = _events(sup, "spend.exhausted")
    assert (event["hat"], event["level"]) == ("total", level)


def test_a_paid_rate_limited_attempt_that_spends_the_cap_is_not_waited_out(tmp_path):
    limited = _rate_limited()
    limited.usage = {"input": 10, "output": 5, "totalTokens": 15, "costUSD": 2.0}
    sup, waits = _harness(tmp_path, [_step(limited), _step(_paid(0.1))],
                          spend_caps=_caps(overall={"worker": 1.0}))
    with pytest.raises(_SpendCapSpent):
        _run(sup, sup.run_iteration("worker"))
    assert sup.runner.calls["worker"] == 1 and waits == []
    assert _spend(sup)["overall"]["worker"] == pytest.approx(2.0)


def test_a_run_without_caps_retries_exactly_as_before(tmp_path):
    steps = [_step(_paid(5.0, error=INFRA_ERROR)), _step(_paid(5.0, text="done"))]
    sup, _ = _harness(tmp_path, steps)
    assert _run(sup, sup.run_iteration("worker")).final_text == "done"
    assert _events(sup, "spend.exhausted") == []


# -- a paid failure that spends a cap AND reaches an infra terminal exit ----------
# The cap decides the outcome (B.2: `exhausted` with its spend reason, or the
# approach archived and replanned), never an infra abort, and nothing is
# dispatched after it -- whichever infra stop (the retry limit, the outage
# budget) the same attempt also reached.

PLAN_503 = "503 Service Unavailable: upstream overloaded"


def _paid_planning_failure(cost: float, *, duration: float = 30.0):
    def step(sup, prompt=""):
        r = _paid(cost, error=PLAN_503)
        r.duration_s = duration
        return r
    return step


def test_a_paid_failure_on_the_infra_retry_limit_and_an_overall_cap_ends_exhausted(
        tmp_path):
    sup, final, status = _run_job(
        tmp_path, {"planning": [_paid_planning_failure(0.75), _plan(0.0)]},
        spend_caps=_caps(overall={"planning": 0.5}), infra_retry_max=1,
        max_approaches=2)
    assert final == "exhausted", status
    assert status["exhaustedBudget"] == "spend:planning"
    assert "infra fault" not in (status.get("reason") or "")
    assert sup.runner.calls == {"planning": 1}
    (event,) = _events(sup, "spend.exhausted")
    assert (event["hat"], event["level"], event["cap"], event["action"],
            event["refusedHat"]) == ("planning", "overall", 0.5, "exhausted",
                                     "planning")
    assert event["spend"] == pytest.approx(0.75)
    assert status["spend"]["overall"] == {"planning": pytest.approx(0.75),
                                          "total": pytest.approx(0.75)}


def test_a_paid_failure_on_the_infra_retry_limit_and_a_per_approach_cap_replans(
        tmp_path):
    seen: dict = {}

    def second_plan(sup, prompt=""):
        seen["approach"] = sup.run.read_status()["approach"]
        return _plan(0.0)(sup, prompt)

    sup, final, status = _run_job(
        tmp_path, {"planning": [_paid_planning_failure(0.75), second_plan],
                   "worker": [_work_forever(0.0)]},
        spend_caps=_caps(per_approach={"planning": 0.5}), infra_retry_max=1,
        max_approaches=2)
    assert seen.get("approach") == 2, status
    assert (Path(sup.run.root) / "approaches" / "01").is_dir()
    first = _events(sup, "spend.exhausted")[0]
    assert (first["hat"], first["level"], first["action"]) == (
        "planning", "per_approach", "replan")
    assert first["spend"] == pytest.approx(0.75)
    assert status["spend"]["byApproach"]["1"]["planning"] == pytest.approx(0.75)
    assert not any("infra fault" in (e.get("message") or "")
                   for e in _events(sup, "log") if e.get("level") == "error")


def test_a_paid_failure_that_spends_the_outage_budget_and_a_total_cap_ends_exhausted(
        tmp_path):
    # infra_outage_budget_s=0: the first infra attempt alone exhausts the
    # outage budget -- the other infra terminal exit, beside the retry limit.
    sup, final, status = _run_job(
        tmp_path, {"planning": [_plan(0.0)],
                   "worker": [_step(_paid(0.9, error=INFRA_ERROR))]},
        spend_caps=_caps(per_approach={"total": 0.5}), infra_outage_budget_s=0.0,
        max_approaches=1)
    assert final == "exhausted", status
    assert status["exhaustedBudget"] == "spend:total:approach"
    assert sup.runner.calls == {"planning": 1, "worker": 1}
    (event,) = _events(sup, "spend.exhausted")
    assert (event["hat"], event["level"], event["refusedHat"]) == (
        "total", "per_approach", "worker")
    assert status["spend"]["byApproach"]["1"]["total"] == pytest.approx(0.9)


def test_a_paid_instant_failure_handed_to_the_phase_retry_spends_no_further_dispatch(
        tmp_path):
    # Under INSTANT_FAILURE_MAX_DURATION_S with tokens billed: the wrapper hands
    # it back to the phase's own bounded error retry -- after the cap check.
    steps = [_paid_planning_failure(0.8, duration=0.3), _step(_paid(0.1))]
    sup, waits = _harness(tmp_path, steps, spend_caps=_caps(overall={"worker": 0.5}))
    with pytest.raises(_SpendCapSpent) as raised:
        _run(sup, sup.run_iteration("worker"))
    assert raised.value.action == "exhausted"
    assert sup.runner.calls["worker"] == 1 and waits == []
    assert _spend(sup)["overall"]["worker"] == pytest.approx(0.8)


# -- router and contract calls on a real runner -------------------------------------

def _router_sup(tmp_path, wrapper_messages, *, linger, **cfg):
    wrapper = _fake_wrapper(tmp_path, wrapper_messages, linger=linger)
    ws = tmp_path / "ws"
    ws.mkdir(exist_ok=True)
    sup = make_supervisor(tmp_path / "run", prd=True, job_timeout_s=3600, **cfg)
    sup.runner = PiRunner(ws, pi_bin=str(wrapper), pricing=PRICING)
    sup.notifier = None
    return sup


def _stream(count: int) -> list[dict]:
    return [_message(f"answer {i + 1}", quoted=True) for i in range(count - 1)] + [
        _message(f"last answer\n{COMPLETE}", quoted=True)]


def test_a_router_call_on_a_spent_total_cap_is_refused_before_a_runner_starts(tmp_path):
    sup = _router_sup(tmp_path, _stream(1), linger=False,
                      spend_caps=_caps(overall={"total": 1.0}))
    sup.run.record_spend(1, "worker", {"costUSD": 1.0}, amount=None)
    with pytest.raises(_SpendCapSpent):
        _run(sup, sup._ask_router("pick an edge"))
    assert not (Path(sup.run.artifacts_dir) / "router" / "0001.jsonl").exists()
    assert _spend(sup)["overall"]["total"] == pytest.approx(1.0)
    (event,) = _events(sup, "spend.exhausted")
    assert (event["hat"], event["refusedHat"], event["action"]) == (
        "total", "router", "exhausted")


def test_the_third_message_of_a_router_call_crossing_the_total_cap_interrupts_it(tmp_path):
    # $0.40 a message; $0.30 already spent, so the third message reaches $1.50
    sup = _router_sup(tmp_path, _stream(4), linger=True,
                      spend_caps=_caps(overall={"total": 1.4}))
    sup.run.record_spend(1, "worker", {"costUSD": 0.3}, amount=None)
    used = sup.iterations_used
    with pytest.raises(_SpendCapSpent) as raised:
        _run(sup, sup._ask_router("pick an edge"))
    assert raised.value.action == "exhausted"
    (interrupted,) = _events(sup, "spend.interrupted")
    assert (interrupted["hat"], interrupted["level"], interrupted["iterationHat"]) == (
        "total", "overall", "router")
    # three messages' cost is counted (the fourth was never read), in the
    # router's and the total's counters; no iteration is charged for it
    spend = _spend(sup)["overall"]
    assert spend["router"] == pytest.approx(1.2)
    assert spend["total"] == pytest.approx(1.5)
    assert sup.iterations_used == used
    assert sup.iterations_used_charged == 0


def test_a_router_call_is_held_to_the_routers_own_per_approach_cap(tmp_path):
    sup = _router_sup(tmp_path, _stream(4), linger=True,
                      spend_caps=_caps(per_approach={"router": 1.0}))
    with pytest.raises(_SpendCapSpent):
        _run(sup, sup._ask_router("pick an edge"))
    (interrupted,) = _events(sup, "spend.interrupted")
    assert (interrupted["hat"], interrupted["level"]) == ("router", "per_approach")
    assert _spend(sup)["byApproach"]["1"]["router"] == pytest.approx(1.2)


def test_a_contract_re_ask_is_held_to_its_own_hats_cap_and_counted_to_it(tmp_path):
    sup = _router_sup(tmp_path, _stream(4), linger=True,
                      spend_caps=_caps(overall={"verify": 1.0}))
    with pytest.raises(_SpendCapSpent):
        _run(sup, sup._ask_contract_fix("verify", ["verdict missing"], None))
    (interrupted,) = _events(sup, "spend.interrupted")
    assert (interrupted["hat"], interrupted["iterationHat"]) == ("verify", "verify")
    assert _spend(sup)["overall"]["verify"] == pytest.approx(1.2)
    assert sup.iterations_used_charged == 0


def test_router_spend_is_counted_even_with_no_cap_on_it_and_a_call_under_caps_answers(
        tmp_path):
    sup = _router_sup(tmp_path, _stream(2), linger=False,
                      spend_caps=_caps(overall={"worker": 50.0}))
    text, usage = _run(sup, sup._ask_router("pick an edge"))
    assert text.endswith(COMPLETE) and usage
    assert _spend(sup)["overall"]["router"] == pytest.approx(0.8)
    assert _events(sup, "spend.interrupted") == []

