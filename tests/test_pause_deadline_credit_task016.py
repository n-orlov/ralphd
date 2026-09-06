"""Task 016 (requirement P): a paused run's parked time is credited back.

Pausing (`POST /pause`) parks the loop at `LoopSupervisor._gate()`'s
`self._pause.wait()` between iterations. Left unaccounted, that parked time
would silently eat into the job's own wall-clock budget exactly the way an
un-credited infra backoff wait would (task 011's own motivating bug) -- a
run paused for two hours would come back with two hours less to actually
work in, having done nothing wrong. The fix mirrors `_account_infra_wait`
exactly: the parked time (measured at the wait site, `_gate()`/
`_park_until_resumed()`, never inside `pause()`/`resume()` themselves)
extends BOTH `self.deadline` and `self._deadline_epoch`, republishes
`status.json`'s `deadlineAt`, and is booked into its own running total
(`pausedTotalS`, published beside `infraWaitTotalS`) via a
`deadline_extended` event whose `reason` says "operator pause" -- never
"infra wait" -- so the two credit sources stay distinguishable in the event
log.

None of these tests use a blocking sleep to wait for anything: the two
timing-sensitive tests below use short *cooperative* `asyncio.sleep` calls
(non-blocking -- they yield the event loop, they do not stall it) purely to
let a real pause actually be in progress before it is resumed, exactly the
pattern `tests/test_retry_now.py` already uses for the same reason; the
third test drives the crediting through a stubbed `_park_until_resumed`
(mirroring `_wait_out_backoff`'s own stubbing in
`tests/test_infra_outage_budget.py`) so a multi-hundred-second credit can be
asserted without a multi-hundred-second wait. `sup.runner` is stubbed in
every test; `_run_iteration_once` is exercised directly only to read the
`timeout_s` it computes and hands to the stub, never to run a real agent.
"""

from __future__ import annotations

import asyncio
import json
import time as real_time

import pytest

from ralphd.engine import loop as loop_mod
from ralphd.engine.config import JobConfig
from ralphd.engine.loop import LoopSupervisor
from ralphd.engine.runner import IterationResult
from ralphd.engine.state import RunDir, utc_from_epoch
from tests.conftest import supervisor_factory


_supervisor = supervisor_factory(run_id="unit", cfg_defaults=False)


class _TimeoutCapturingRunner:
    """Stands in for PiRunner at the one seam `_run_iteration_once` calls
    (`self.runner.run`), recording every `timeout_s` it was handed so a test
    can see the per-iteration timeout formula's *result* without spawning a
    real agent."""

    def __init__(self):
        self.timeouts: list[float] = []

    def interrupt(self) -> bool:
        return False

    async def run(self, prompt, transcript, **kw) -> IterationResult:
        self.timeouts.append(kw["timeout_s"])
        r = IterationResult(exit_code=0)
        r.final_text = "done"
        r.duration_s = 1.0
        return r


def _events(run: RunDir, type_: str) -> list[dict]:
    log = run.root / "events.jsonl"
    lines = log.read_text().splitlines() if log.exists() else []
    return [ev for ev in (json.loads(x) for x in lines) if ev.get("type") == type_]


class _FakeMonotonic:
    """A hand-cranked stand-in for the `time` module, scoped to `loop.py`'s
    own `time.monotonic` (task 004, #104): every OTHER attribute (`time()`,
    `sleep()`, ...) passes straight through to the real module, so only
    ``monotonic()`` -- the clock `_park_until_resumed`/`_extend_deadline_by`
    actually read -- is under the test's control. Advancing it is a plain
    counter bump, never a real wait, so a test can place the clock exactly
    where it wants between two genuinely-awaited events without a single
    `time.sleep`. Patched onto `ralphd.engine.loop`'s own module-global
    `time` name (not the process-wide `time` module), so `asyncio`'s own
    scheduler -- which reads the real `time.monotonic` for every timer --
    is completely unaffected by the advances below."""

    def __init__(self, real_time_module, start: float):
        self._real = real_time_module
        self._t = start

    def monotonic(self) -> float:
        return self._t

    def advance(self, delta: float) -> None:
        self._t += delta

    def __getattr__(self, name):
        return getattr(self._real, name)


# -- pin 1: a pause requested mid-iteration credits only the parked time ---


@pytest.mark.asyncio
async def test_pause_credits_only_the_actually_parked_time(tmp_path):
    sup = _supervisor(tmp_path, job_timeout_s=3600)
    orig_deadline, orig_epoch = sup.deadline, sup._deadline_epoch

    # Time that passes BEFORE the pause is even requested must not be
    # credited: nothing has parked yet, so `_gate()` has nothing to measure.
    await asyncio.sleep(0.03)
    assert sup.deadline == orig_deadline
    assert sup._deadline_epoch == orig_epoch
    assert sup._paused_wait_total_s == 0.0

    sup.pause()
    task = asyncio.ensure_future(sup._gate())
    # Let the task actually start and park at `self._pause.wait()` -- a bare
    # `create_task` only schedules it, it does not run until the loop yields.
    await asyncio.sleep(0)
    assert not task.done(), "the gate must actually be parked, not skipped"

    parked_for = 0.05
    await asyncio.sleep(parked_for)
    sup.resume()
    waited_s = await asyncio.wait_for(task, timeout=5)

    # The credited amount is (approximately) exactly the parked interval --
    # not the interval since the test started, not zero.
    assert waited_s == pytest.approx(parked_for, abs=0.05)
    assert sup._paused_wait_total_s == pytest.approx(waited_s)
    assert sup.deadline == pytest.approx(orig_deadline + waited_s, abs=0.05)
    assert sup._deadline_epoch == pytest.approx(orig_epoch + waited_s, abs=0.05)

    status = json.loads((sup.run.root / "status.json").read_text())
    assert status["pausedTotalS"] == pytest.approx(sup._paused_wait_total_s, abs=1e-3)


@pytest.mark.asyncio
async def test_gate_not_paused_credits_and_emits_nothing(tmp_path):
    sup = _supervisor(tmp_path, job_timeout_s=3600)
    orig_deadline, orig_epoch = sup.deadline, sup._deadline_epoch

    await sup._gate()  # never paused: must return immediately, no credit

    assert sup.deadline == orig_deadline
    assert sup._deadline_epoch == orig_epoch
    assert sup._paused_wait_total_s == 0.0
    assert _events(sup.run, "deadline_extended") == []


# -- pin 2: the reason distinguishes an operator pause from an infra wait --


@pytest.mark.asyncio
async def test_deadline_extended_reason_distinguishes_pause_from_infra_wait(
        tmp_path):
    sup = _supervisor(tmp_path, job_timeout_s=3600)

    # Drive a real pause/resume (stubbed park below removes the timing
    # dependency -- this test is about the *label*, not the arithmetic).
    async def fake_park() -> float:
        return 12.5

    sup._park_until_resumed = fake_park  # type: ignore[method-assign]
    sup.pause()
    await sup._gate()

    pause_events = _events(sup.run, "deadline_extended")
    assert len(pause_events) == 1
    assert pause_events[0]["reason"] == "operator pause"
    assert pause_events[0]["waitedS"] == pytest.approx(12.5)
    assert pause_events[0]["pausedTotalS"] == pytest.approx(12.5)
    assert "infraWaitTotalS" not in pause_events[0]

    # An infra wait booked through the SAME event name must carry the other
    # reason -- the two credit sources are distinguished by `reason`, not by
    # which fields happen to be present.
    sup._account_infra_wait(7.0, "worker", 1)
    infra_events = _events(sup.run, "deadline_extended")
    assert infra_events[-1]["reason"] == "infra wait"
    assert infra_events[-1]["reason"] != pause_events[0]["reason"]

    status = json.loads((sup.run.root / "status.json").read_text())
    assert status["pausedTotalS"] == pytest.approx(12.5)
    assert status["infraWaitTotalS"] == pytest.approx(7.0)


# -- pin 3: the extension actually rescues the 60s iteration-timeout floor -


@pytest.mark.asyncio
async def test_pause_credit_avoids_the_60s_floor(tmp_path):
    # A deadline that is already effectively "now" gives every non-reflect
    # phase's per-iteration timeout formula (`max(60, deadline - now)`,
    # min-ed against `iteration_timeout_s`) exactly the 60s floor.
    sup = _supervisor(tmp_path, job_timeout_s=0, iteration_timeout_s=3600)
    runner = _TimeoutCapturingRunner()
    sup.runner = runner  # type: ignore[assignment]

    await sup._run_iteration_once("worker")
    assert runner.timeouts[-1] == 60

    # Crediting a (stubbed, no real wait) pause of 1000s -- exactly what
    # `_gate()` would do for a pause actually parked that long -- pushes the
    # deadline back out well past the floor.
    async def fake_park() -> float:
        return 1000.0

    sup._park_until_resumed = fake_park  # type: ignore[method-assign]
    sup.pause()
    await sup._gate()

    await sup._run_iteration_once("worker")
    assert runner.timeouts[-1] != 60
    assert runner.timeouts[-1] > 900


# -- pin 4: a pause requested WHILE an iteration is genuinely in flight ----
# credits only the span actually spent parked at the real `_gate()` site,
# never the span between the `pause()` call and the iteration finishing
# (#104 residual: pins 1-3 above only ever drive `_gate()` with nothing
# else running, or stub `_park_until_resumed` away entirely -- neither
# proves an in-flight iteration's own busy time is excluded).


@pytest.mark.asyncio
async def test_pause_mid_flight_iteration_credits_only_the_real_park(
        tmp_path, monkeypatch):
    fake_time = _FakeMonotonic(real_time, start=1_000_000.0)
    monkeypatch.setattr(loop_mod, "time", fake_time)

    sup = _supervisor(tmp_path, job_timeout_s=3600)
    orig_deadline, orig_epoch = sup.deadline, sup._deadline_epoch

    runner_entered = asyncio.Event()
    release_runner = asyncio.Event()

    class _BlockingRunner:
        """Stands in for PiRunner (the one seam `_run_iteration_once`
        calls) but never actually returns until told to -- signalling
        `runner_entered` the instant it is called and then parking on
        `release_runner`, an asyncio.Event, so the test can hold a real
        iteration "in flight" for as long as it likes with no sleep of
        any kind on either side."""

        def interrupt(self) -> bool:
            return False

        async def run(self, prompt, transcript, **kw) -> IterationResult:
            runner_entered.set()
            await release_runner.wait()
            r = IterationResult(exit_code=0)
            r.final_text = "done"
            r.duration_s = 1.0
            return r

    sup.runner = _BlockingRunner()  # type: ignore[assignment]

    # Wraps the REAL `_park_until_resumed` (never replaces it) purely to
    # get an event-driven signal the instant the driving task actually
    # reaches the parked await -- so the test never has to guess how many
    # scheduler ticks a synchronous return chain takes.
    orig_park = sup._park_until_resumed
    parked = asyncio.Event()

    async def _signalling_park() -> float:
        parked.set()
        return await orig_park()

    sup._park_until_resumed = _signalling_park  # type: ignore[method-assign]

    async def drive() -> float:
        # The exact production sequence (`_step_worker`'s own while loop:
        # this pass's iteration, then the NEXT pass's `_gate()` at the top)
        # -- `run_iteration`, never `_run_iteration_once` directly, so the
        # test drives the same public seam production call sites use.
        await sup.run_iteration("worker")
        return await sup._gate()

    task = asyncio.ensure_future(drive())
    await runner_entered.wait()  # the iteration is genuinely in flight now

    # Pause is requested WHILE the iteration is still running: nothing has
    # reached a `_gate()` site yet, so nothing is parked and nothing may be
    # credited from this moment on.
    sup.pause()

    # Time that passes before the runner is even released -- the loop is
    # still busy inside the "in-flight" iteration, nowhere near `_gate()` --
    # must not be credited.
    fake_time.advance(500.0)
    assert not task.done()
    assert sup._paused_wait_total_s == 0.0
    assert sup.deadline == orig_deadline
    assert sup._deadline_epoch == orig_epoch

    release_runner.set()
    # Driven entirely by the `parked` event above: resolves only once the
    # driving task has actually finished the iteration and reached the
    # real parked await inside `_park_until_resumed`.
    await asyncio.wait_for(parked.wait(), timeout=5)

    # Still not credited: `_park_until_resumed` has captured its own
    # `started` reading, but `resume()` has not been called yet, so nothing
    # has actually elapsed at the wait site as far as `_account_paused_wait`
    # is concerned.
    assert not task.done()
    assert sup._paused_wait_total_s == 0.0
    assert sup.deadline == orig_deadline
    assert sup._deadline_epoch == orig_epoch

    # Only the span genuinely spent parked at the gate -- from here on --
    # may move the clock.
    fake_time.advance(37.0)
    sup.resume()
    waited_s = await asyncio.wait_for(task, timeout=5)

    assert waited_s == pytest.approx(37.0)
    assert sup._paused_wait_total_s == pytest.approx(37.0)
    assert sup.deadline == pytest.approx(orig_deadline + 37.0)
    assert sup._deadline_epoch == pytest.approx(orig_epoch + 37.0)

    events = _events(sup.run, "deadline_extended")
    assert len(events) == 1
    assert events[0]["reason"] == "operator pause"
    assert events[0]["waitedS"] == pytest.approx(37.0)
    assert events[0]["pausedTotalS"] == pytest.approx(37.0)
    assert "infraWaitTotalS" not in events[0]

    status = json.loads((sup.run.root / "status.json").read_text())
    assert status["pausedTotalS"] == pytest.approx(37.0)
    assert status["deadlineAt"] == utc_from_epoch(sup._deadline_epoch)
