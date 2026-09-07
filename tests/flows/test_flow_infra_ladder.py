"""Flow (task 015, requirement C.4): **the infra-retry ladder** -- retry,
wait, recovered, and outage-budget exhaustion -- over a real `ralphd-engine`
subprocess with the stub `pi` binary on PATH, `STUB_SLEEP=0`.

`LoopSupervisor._run_iteration_with_infra_retry` (task 008/010/015, #5)
transparently retries a phase whenever `pi` produces zero LLM traffic before
the startup watchdog fires (`STUB_INFRA_HANG_COUNT` -- the stub sleeps 9999s
producing no stdout, the engine's own watchdog SIGINTs it after
`infra_startup_timeout_s`). From the run's own record this shows up as three
step-marking event types the ordinary flows never touch:

  * `infra_retry`  -- one per hung attempt, naming the attempt number and
    (unless the outage budget is already spent) the backoff about to be
    waited;
  * `infra_wait`   -- the backoff wait beginning, mirrored into the
    degraded half of `status.json` (`health: "degraded"`, `infraWait`);
  * `infra_recovered` -- the episode ends the moment an attempt finally
    reaches the model again, `health` back to `"ok"`.

Two tests cover the two ways an outage episode can end, and a third covers
the watchdog timing that makes either of them possible at all (folded in
from `tests/test_infra_fault_retry.py` by task 020, C.9):

  * **recovered** -- two hung attempts, then a healthy one: the retries cost
    no iteration budget, the run finishes exactly as if the outage had never
    happened, and `infraWaitTotalS` records the two backoff waits actually
    spent (`test_two_infra_faults_are_retried_then_recovered`);
  * **outage budget exhausted** -- a `RALPHD_INFRA_OUTAGE_BUDGET_S` far
    below what a permanently hung endpoint would need: the wrapper gives up
    once the episode's cumulative backoff wait reaches the budget, and the
    run ends `aborted` with a reason naming the outage duration and the
    spent/total budget (`test_outage_budget_exhaustion_aborts_naming_the_duration`);
  * **the startup window itself** -- a hung attempt is ended by
    `infra_startup_timeout_s`, not by the (900s, configured explicitly)
    `iteration_timeout_s` it would otherwise sit in: with the explicit
    attempt cap `RALPHD_INFRA_RETRY_MAX=1` the whole run finishes in a
    measured wall clock 45x under that timeout
    (`test_the_startup_watchdog_kills_a_hang_within_its_own_window`).

Every backoff value and the outage budget below are named explicitly (never
left to `tests/conftest.py`'s zero-backoff default, which would erase the
subject), and are small enough that the whole module -- both nodes -- stays
well under the 20s bound the task's own criterion sets: `_wait_out_backoff`
books only the backoff value itself (clamped, never the real wall-clock
overshoot -- see `loop.py`'s `_wait_out_backoff`), so the episode's
cumulative-wait arithmetic that decides how many attempts fire before budget
exhaustion is exact, not a race against process-spawn jitter.
"""

from __future__ import annotations

import json
import re
import time

import pytest

from tests.flows.flowlib import (assert_step_sequence, events_of, flow_steps,
                                 status)

# -- recovered: two hangs, then a healthy attempt ---------------------------

RECOVERED_STEPS = [
    "state:running",
    "phase:planning@1",
    "iteration:planning",
    "iteration:worker",          # attempt 1: hangs
    "infra_retry:worker:1",
    "infra_wait:worker:1",
    "iteration:worker",          # attempt 2: hangs
    "infra_retry:worker:2",
    "infra_wait:worker:2",
    "iteration:worker",          # attempt 3: reaches the model, succeeds
    "infra_recovered",
    "signal:COMPLETE",
    "phase:review@1",
    "iteration:review",
    "verdict:review:verified@1",
    "signal:VERIFIED",
    "state:succeeded",
]


def test_two_infra_faults_are_retried_then_recovered(engine_factory):
    run = engine_factory(
        job={"run_id": "flow-infra-recovered", "on_complete": "exit",
             "max_approaches": 1},
        stub_env={
            "STUB_TASKS": "1",
            "STUB_INFRA_HANG_SKIP": "1",     # planning stays healthy
            "STUB_INFRA_HANG_COUNT": "2",    # the next two worker attempts hang
            "RALPHD_INFRA_STARTUP_TIMEOUT": "0.2",
            "RALPHD_INFRA_RETRY_BACKOFF_S": "0.05,0.1",
            "RALPHD_INFRA_OUTAGE_BUDGET_S": "60",
        })
    assert run.proc.wait(timeout=60) == 0, (
        "flow infra ladder (recovered): the engine did not exit 0 once the "
        "third attempt reached the model")

    assert_step_sequence(flow_steps(run), RECOVERED_STEPS,
                         flow="flow infra ladder (recovered)")

    st = status(run)
    assert st.get("state") == "succeeded" and st.get("verdict") == "verified", (
        f"flow infra ladder (recovered): expected succeeded/verified, got "
        f"state={st.get('state')!r} verdict={st.get('verdict')!r}")
    assert st.get("iterationsUsed") == 3, (
        f"flow infra ladder (recovered): planning + worker + review == 3 -- "
        f"the two retried worker attempts must be refunded, not charged, "
        f"got iterationsUsed={st.get('iterationsUsed')!r}")
    assert st.get("health") == "ok", (
        f"flow infra ladder (recovered): the episode ended in recovery, so "
        f"health must read back 'ok', got {st.get('health')!r}")
    assert st.get("infraWait") is None, (
        f"flow infra ladder (recovered): nothing is being waited on any "
        f"more, so infraWait must be null, got {st.get('infraWait')!r}")
    # Booked wait == the two backoffs actually spent (0.05 + 0.1), not the
    # real wall-clock time the hangs + watchdog + process overhead took.
    assert st.get("infraWaitTotalS") == 0.15, (
        f"flow infra ladder (recovered): infraWaitTotalS should book exactly "
        f"the two backoff waits (0.05 + 0.1 = 0.15), got "
        f"{st.get('infraWaitTotalS')!r}")


# -- outage budget exhausted -------------------------------------------------

# Arithmetic (exact, per loop.py's `_run_iteration_with_infra_retry`):
#   attempt 1: waited=0.00 < budget(0.5)  -> wait backoff=min(0.1,300,0.5)=0.1 -> waited=0.10
#   attempt 2: waited=0.10 < budget(0.5)  -> wait backoff=min(0.2,300,0.4)=0.2 -> waited=0.30
#   attempt 3: waited=0.30 < budget(0.5)  -> wait backoff=min(0.2,300,0.2)=0.2 -> waited=0.50
#   attempt 4: waited=0.50 >= budget(0.5) -> budget spent: aborts, no wait.
ABORTED_STEPS = [
    "state:running",
    "phase:planning@1",
    "iteration:planning",
    "iteration:worker",          # attempt 1
    "infra_retry:worker:1",
    "infra_wait:worker:1",
    "iteration:worker",          # attempt 2
    "infra_retry:worker:2",
    "infra_wait:worker:2",
    "iteration:worker",          # attempt 3
    "infra_retry:worker:3",
    "infra_wait:worker:3",
    "iteration:worker",          # attempt 4: budget already spent
    "infra_retry:worker:4",
    "state:aborted",
]


def test_outage_budget_exhaustion_aborts_naming_the_duration(engine_factory):
    run = engine_factory(
        job={"run_id": "flow-infra-aborted", "on_complete": "exit",
             "max_approaches": 1},
        stub_env={
            "STUB_TASKS": "1",
            "STUB_INFRA_HANG_SKIP": "1",     # planning stays healthy
            "STUB_INFRA_HANG_COUNT": "10",   # every worker attempt hangs
            "RALPHD_INFRA_STARTUP_TIMEOUT": "0.2",
            "RALPHD_INFRA_RETRY_BACKOFF_S": "0.1,0.2",
            "RALPHD_INFRA_OUTAGE_BUDGET_S": "0.5",
        })
    assert run.proc.wait(timeout=60) == 1, (
        "flow infra ladder (aborted): the engine must exit non-zero once "
        "the outage budget is spent")

    assert_step_sequence(flow_steps(run), ABORTED_STEPS,
                         flow="flow infra ladder (aborted)")

    st = status(run)
    assert st.get("state") == "aborted", (
        f"flow infra ladder (aborted): expected 'aborted', got "
        f"{st.get('state')!r} (status.json: {st})")
    reason = st.get("reason") or ""
    assert re.search(r"\d+s infra outage", reason), reason
    assert "outage budget spent waiting" in reason, reason
    assert "no LLM traffic" in reason, reason
    assert st.get("iterationsUsed") == 1, (
        f"flow infra ladder (aborted): only the healthy planning iteration "
        f"may be charged, got iterationsUsed={st.get('iterationsUsed')!r}")
    assert st.get("approach") == 1, (
        f"flow infra ladder (aborted): an infra outage never escalates the "
        f"approach, got {st.get('approach')!r}")
    # The episode never recovered: the degraded half of the status contract
    # is still showing it (infraWait itself is null between waits -- the
    # abort happens right after the LAST infra_retry, before a new wait
    # would begin -- but health never went back to "ok").
    assert st.get("health") == "degraded", (
        f"flow infra ladder (aborted): the run never recovered, so health "
        f"must still read 'degraded', got {st.get('health')!r}")


# -- the startup watchdog's own window --------------------------------------

# `iteration_timeout_s` for the watchdog node: deliberately far larger than
# any bound this module's own assertions use, so "the watchdog fired, not the
# iteration timeout" is a claim the wall clock itself can settle. The stub
# hangs for 9999s producing zero stdout; only the startup watchdog can end it.
WATCHDOG_ITERATION_TIMEOUT_S = 900
# The watchdog window itself, and the wall-clock bound the whole run must fit
# inside: one healthy planning iteration plus one hung worker attempt killed
# 0.2s in. 20s leaves generous room for process-spawn jitter on a loaded host
# while staying 45x below the iteration timeout above.
WATCHDOG_WINDOW_S = 0.2
WATCHDOG_WALL_CLOCK_BOUND_S = 20


def test_the_startup_watchdog_kills_a_hang_within_its_own_window(engine_factory):
    """The watchdog's timing half of the ladder, folded in from
    `tests/test_infra_fault_retry.py::test_startup_watchdog_kills_hang_within_
    startup_window_not_full_timeout` when task 020 (C.9) deleted that module:
    a `pi` invocation that produces ZERO LLM traffic is killed after
    `infra_startup_timeout_s`, NOT after the (900s here) `iteration_timeout_s`
    a fully-hung iteration would otherwise sit in. `RALPHD_INFRA_RETRY_MAX=1`
    caps the episode at that single attempt, so the whole run -- healthy
    planning, one watchdog kill, abort -- finishes inside a wall clock nearly
    two orders of magnitude below the iteration timeout it never waited out;
    without the watchdog the same run could not finish inside it at all."""
    run = engine_factory(
        job={"run_id": "flow-infra-watchdog", "on_complete": "exit",
             "max_approaches": 1,
             "iteration_timeout_s": WATCHDOG_ITERATION_TIMEOUT_S},
        stub_env={
            "STUB_TASKS": "1",
            "STUB_INFRA_HANG_SKIP": "1",     # planning stays healthy
            "STUB_INFRA_HANG_COUNT": "10",   # every worker attempt hangs 9999s
            "RALPHD_INFRA_STARTUP_TIMEOUT": str(WATCHDOG_WINDOW_S),
            "RALPHD_INFRA_RETRY_BACKOFF_S": "0.05",
            "RALPHD_INFRA_RETRY_MAX": "1",   # back-compat explicit attempt cap
            "RALPHD_INFRA_OUTAGE_BUDGET_S": "60",
        })
    t0 = time.monotonic()
    assert run.proc.wait(timeout=WATCHDOG_WALL_CLOCK_BOUND_S + 40) == 1, (
        "flow infra ladder (watchdog): a permanently hung stub with the "
        "attempt cap spent must end the engine non-zero")
    elapsed = time.monotonic() - t0
    assert elapsed < WATCHDOG_WALL_CLOCK_BOUND_S, (
        f"flow infra ladder (watchdog): the {WATCHDOG_WINDOW_S}s startup "
        f"window -- not the {WATCHDOG_ITERATION_TIMEOUT_S}s iteration timeout "
        f"-- must be what ends each hung attempt, but the run took "
        f"{elapsed:.1f}s (bound {WATCHDOG_WALL_CLOCK_BOUND_S}s)")

    assert_step_sequence(flow_steps(run), [
        "state:running",
        "phase:planning@1",
        "iteration:planning",
        "iteration:worker",          # hung: killed by the startup watchdog
        "infra_retry:worker:1",      # the explicit attempt cap (1) is spent
        "state:aborted",             # ...so no backoff wait, straight to abort
    ], flow="flow infra ladder (watchdog)")

    st = status(run)
    assert st.get("state") == "aborted", (
        f"flow infra ladder (watchdog): expected 'aborted', got "
        f"{st.get('state')!r} (status.json: {st})")
    reason = (st.get("reason") or "").lower()
    assert "infra fault" in reason, reason
    assert "no llm traffic" in reason, reason
    # The timing claim above is anchored to configuration, not to this
    # test's prose: the engine loaded exactly this `iteration_timeout_s`
    # (the fixture writes one `key: <json value>` line per job knob), so a
    # future default change cannot quietly make the bound vacuous.
    job_yaml = (run.config_dir / "job.yaml").read_text().splitlines()
    configured = {line.split(":", 1)[0].strip():
                  json.loads(line.split(":", 1)[1].strip())
                  for line in job_yaml if ":" in line}
    assert configured["iteration_timeout_s"] == WATCHDOG_ITERATION_TIMEOUT_S, (
        f"flow infra ladder (watchdog): the run must be configured with the "
        f"large iteration timeout this node's bound is measured against, got "
        f"{configured.get('iteration_timeout_s')!r}")
    assert WATCHDOG_WALL_CLOCK_BOUND_S * 10 < WATCHDOG_ITERATION_TIMEOUT_S, (
        "flow infra ladder (watchdog): the wall-clock bound must stay an "
        "order of magnitude below the iteration timeout, or passing it says "
        "nothing about which timer fired")


# ---------------------------------------------------------------------------
# Task 021 (requirement C.10): the two black-box nodes folded in from
# `tests/test_infra_outage_budget.py` (deleted in the same commit, one §
# "Subsumed" row per node; that module's five UNIT nodes and the shared
# `_infra_result`/`_ok_result`/`_stub_attempts` helpers its five importers
# use moved, unchanged, to `tests/test_infra_retry_schedule.py`).
#
# Same subject as this flow, one tier up from its own nodes: MORE
# consecutive infra faults than the historical 3-attempt cap allowed are
# ridden out free (five, with the escalating backoff ladder and the
# cumulative `waitedS` book asserted value by value), and a permanently dead
# endpoint gives up naming the outage duration and the budget spent waiting.
# Moved verbatim -- same `engine_factory`, same stub knobs, same assertions,
# only the module and its own event reader changed (`flowlib.events_of`).
#
# Both keep `@pytest.mark.nightly` (task 048, B.5 follow-on): they drive a
# REAL hang-timeout + real backoff ladder in the engine subprocess, so they
# are inherently multi-second and were moved out of the fast tier rather
# than have the very schedule they prove shrunk. The autouse class-level
# `_wait_out_backoff` stub of `tests/conftest.py` (task 047, B.4) is a
# no-op for them either way: the backoff being waited out here is inside the
# engine's OWN process, configured through `RALPHD_INFRA_RETRY_BACKOFF_S`,
# not in this test process -- which is exactly why these two nodes need no
# `real_backoff` opt-out where the deleted module's unit half does.
# ---------------------------------------------------------------------------


@pytest.mark.nightly
def test_five_consecutive_infra_faults_are_ridden_out_free(engine_factory):
    # No RALPHD_INFRA_RETRY_MAX at all: five hung worker invocations (the old
    # default would have given up after three) are retried on a compressed
    # schedule, then the healthy attempt finishes the only task. Charged
    # budget is exactly the happy path (planning + worker + review == 3), so
    # the run can only succeed if every retried attempt was refunded.
    e = engine_factory(
        job={"on_complete": "exit", "iterations": 3, "max_approaches": 1},
        stub_env={
            "STUB_TASKS": "1",
            "STUB_INFRA_HANG_SKIP": "1",   # invocation 1 (planning) is healthy
            "STUB_INFRA_HANG_COUNT": "5",  # the next five worker attempts hang
            "RALPHD_INFRA_STARTUP_TIMEOUT": "1",
            "RALPHD_INFRA_RETRY_BACKOFF_S": "0.1,0.2,0.4",
            "RALPHD_INFRA_OUTAGE_BUDGET_S": "60",
        })
    assert e.proc.wait(timeout=60) == 0

    st = status(e)
    assert st["state"] == "succeeded"
    assert st["verdict"] == "verified"
    assert st["iterationsUsed"] == 3, "waiting out an outage costs no iterations"
    assert st["approach"] == 1, "an infra outage never escalates the approach"

    infra = events_of(e, "infra_retry")
    assert [ev["attempt"] for ev in infra] == [1, 2, 3, 4, 5]
    assert [ev["backoffS"] for ev in infra] == [0.1, 0.2, 0.4, 0.4, 0.4]
    assert all(ev["maxAttempts"] is None for ev in infra), "no cap configured"
    assert [ev["waitedS"] for ev in infra] == [0.0, 0.1, 0.3, 0.7, 1.1]


@pytest.mark.nightly
def test_outage_budget_exhaustion_ends_terminal_naming_the_duration(engine_factory):
    # A permanently broken endpoint: with a 2s outage budget the run gives up
    # quickly and the terminal reason tells the operator how long the outage
    # lasted, how much of the budget went into waiting, and the last error.
    e = engine_factory(
        job={"on_complete": "exit", "iterations": 12, "max_approaches": 3},
        stub_env={
            "STUB_TASKS": "1",
            "STUB_INFRA_HANG_SKIP": "1",
            "STUB_INFRA_HANG_COUNT": "20",  # every worker attempt hangs
            "RALPHD_INFRA_STARTUP_TIMEOUT": "1",
            "RALPHD_INFRA_RETRY_BACKOFF_S": "0.5,1",
            "RALPHD_INFRA_OUTAGE_BUDGET_S": "2",
        })
    assert e.proc.wait(timeout=60) == 1

    st = status(e)
    assert st["state"] == "aborted"
    reason = st["reason"]
    assert re.search(r"\d+s infra outage", reason), reason
    assert "2s outage budget spent waiting" in reason
    assert "no llm traffic" in reason.lower()
    assert st["approach"] == 1
    assert st["iterationsUsed"] == 1, "only the healthy planning iteration"
