"""Deadline / pause / review-clock behaviour, folded (task 028, D.7):

- Requirement B (#72) -- a run that earned COMPLETE does not lose it to the
  clock -- `deadlineBeatVerdict` (task 006, folded as section A below).
- Requirement P -- a paused run's parked time is credited back to the
  deadline, distinguished from an infra-wait credit by `reason` (task 016,
  folded as section B below).
- PRD requirement F.3 (#146) -- a deadline landing mid-review ends the run
  `verdict: null` (task 009, v0.24 requirement B.2, #169 narrowed this from
  the original `"unreviewed"` string), never an ordinary rejection, and
  resumes straight back onto review for the same approach (task 016, folded
  as section C below; supersedes the `"unverified"` string section A
  originally pinned, as noted inline there).
- Task 048 (B.5 follow-on, operator steer 005) -- `LiveRun.stop()` skips the
  SIGTERM grace it cannot honour on an engine already parked `paused`
  (section D below).

Each section keeps its own source module's docstring, helpers and tests
verbatim; only names that collided across the four merged modules were
renamed to disambiguate (`_supervisor` -> `_pause_supervisor` /
`_review_supervisor`; section A's imported `_ok`/`_interrupted`/`_plan`/
`_complete_task`/`_supervisor` from `test_review_interrupted_not_rejection`
-> `_rir_*`, since section C defines its OWN same-named locals that are
DIFFERENT functions).
"""

from __future__ import annotations

import asyncio
import json
import time as real_time

import pytest

from ralphd.engine import findings, loop as loop_mod
from ralphd.engine.loop import LoopSupervisor
from ralphd.engine.runner import IterationResult
from ralphd.engine.state import RunDir, atomic_write_json, read_events, utc_from_epoch
from tests.conftest import (ScriptedRunner, attach_runner, make_supervisor,
                            supervisor_factory)

from test_review_interrupted_not_rejection import (
    _complete_task as _rir_complete_task,
    _interrupted as _rir_interrupted,
    _ok as _rir_ok,
    _plan as _rir_plan,
    _supervisor as _rir_supervisor,
)


# ===========================================================================
# Section A -- task 006 (requirement B, #72): deadlineBeatVerdict marker
# ===========================================================================

# ---------------------------------------------------------------------------
# 1. the named regression: the marker fires and names both components
# ---------------------------------------------------------------------------

def test_v09_iteration_183_regression_status_carries_the_marker(tmp_path):
    handlers = {
        "planning": [_rir_plan],
        "worker": [_rir_complete_task],
        "review": [_rir_interrupted],
    }
    # exactly 3 iterations: plan, worker(COMPLETE), review(interrupted) --
    # budget is gone the instant that third attempt returns.
    sup = _rir_supervisor(tmp_path / "run", handlers, iterations=3)
    state = asyncio.run(sup._run_job_core())

    assert state == "failed"
    status = sup.run.read_status()
    # Task 016 (requirement F.3, #146) had this specific path -- review
    # cut short at budget end -- write the dedicated "unreviewed" verdict,
    # not the ordinary "unverified" this file was written against. Task
    # 009 (v0.24 requirement B.2, #169) narrowed that further: a held run
    # carries NO verdict at all, so this now reads `None`, not a string --
    # the marker assertions below (this task's own subject) are unaffected.
    marker = status.get("deadlineBeatVerdict")
    assert isinstance(marker, dict), (
        "status.json must carry a deadlineBeatVerdict marker when every "
        "task was completed, COMPLETE was honoured, and review was cut "
        "off at budget end with no verdict ever scored")
    assert marker.get("deadline"), "marker must name the deadline"
    assert marker.get("completeHonouredAt"), (
        "marker must name the honoured COMPLETE (its event timestamp)")


# ---------------------------------------------------------------------------
# 2. the paired mutation: drop the marker computation, test 1 must then fail
# ---------------------------------------------------------------------------

def test_mutation_dropping_the_marker_fails_test_1(tmp_path, monkeypatch):
    monkeypatch.setattr(LoopSupervisor, "_deadline_beat_verdict_patch",
                        lambda self: {})

    handlers = {
        "planning": [_rir_plan],
        "worker": [_rir_complete_task],
        "review": [_rir_interrupted],
    }
    sup = _rir_supervisor(tmp_path / "run", handlers, iterations=3)
    asyncio.run(sup._run_job_core())

    status = sup.run.read_status()
    assert status.get("deadlineBeatVerdict") is None


# ---------------------------------------------------------------------------
# 3. verdict: verified stays the review hat's alone to grant
# ---------------------------------------------------------------------------

def test_path_never_writes_verdict_verified(tmp_path):
    # marker fires (every task completed, COMPLETE honoured)
    sup = _rir_supervisor(tmp_path / "run-marker", {}, iterations=5)
    doc = {"tasks": [{"id": "001", "title": "t", "successCriteria": "c",
                      "status": "completed"}]}
    atomic_write_json(sup.run.tasks_file, doc)
    sup.run.emit("signal", signal="COMPLETE")
    sup._review_unjudged_at_budget_end(1)
    state = sup._finish_tail_unverified()

    assert state == "failed"
    status = sup.run.read_status()
    # Task 009 (v0.24 requirement B.2, #169): null, never the "unreviewed"
    # string this used to be -- see test_deadline_mid_review_carries_a_
    # null_verdict below for the dedicated coverage.
    assert status["verdict"] is None
    assert status["verdict"] != "verified"
    assert isinstance(status.get("deadlineBeatVerdict"), dict)

    # marker does NOT fire (nothing completed) -- still never "verified"
    sup2 = _rir_supervisor(tmp_path / "run-no-marker", {}, iterations=5)
    atomic_write_json(sup2.run.tasks_file, {"tasks": [
        {"id": "001", "title": "t", "successCriteria": "c",
         "status": "pending"}]})
    sup2._review_unjudged_at_budget_end(1)
    state2 = sup2._finish_tail_unverified()

    assert state2 == "failed"
    status2 = sup2.run.read_status()
    assert status2["verdict"] is None
    assert status2["verdict"] != "verified"
    assert status2.get("deadlineBeatVerdict") is None


# ---------------------------------------------------------------------------
# 4. an ordinary failed fixture (tasks not completed) is unaffected
# ---------------------------------------------------------------------------

def test_ordinary_failed_fixture_unaffected_byte_for_byte(tmp_path):
    sup = _rir_supervisor(tmp_path / "run", {}, iterations=5)
    atomic_write_json(sup.run.tasks_file, {"tasks": [
        {"id": "001", "title": "still working", "successCriteria": "c",
         "status": "in-progress"}]})
    # No COMPLETE signal was ever emitted, and no review-unjudged path was
    # taken -- an ordinary abort, exactly the shape `_finish_tail_unverified`
    # already handled before this task existed.
    sup._abort_reason = "some ordinary engine-detected abort condition"
    state = sup._finish_tail_unverified()

    assert state == "aborted"
    status = sup.run.read_status()
    assert status["verdict"] == "unverified"
    assert status["reason"] == "some ordinary engine-detected abort condition"
    assert status.get("unconsumedSteering") == []
    assert "deadlineBeatVerdict" not in status


# ===========================================================================
# Section B -- task 016 (requirement P): a paused run's parked time is credited
# ===========================================================================

_pause_supervisor = supervisor_factory(run_id="unit", cfg_defaults=False)


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
    sup = _pause_supervisor(tmp_path, job_timeout_s=3600)
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
    sup = _pause_supervisor(tmp_path, job_timeout_s=3600)
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
    sup = _pause_supervisor(tmp_path, job_timeout_s=3600)

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


# -- pin 3: RETIRED (task 006, requirement A.1) -- the 60s floor this pin
# demonstrated pausing away no longer exists at all: `run_iteration`'s
# dispatch-timeout formula stopped reading `self.deadline` outright, so a
# deadline already past no longer collapses the dispatched subprocess
# timeout down to a floor for ANY pause to rescue. Surviving cover: pin 1
# above (`test_pause_credits_only_the_actually_parked_time`) already pins
# that a pause credit still moves `self.deadline`/`self._deadline_epoch`
# themselves (the one place the deadline still acts, via `budget_left()`);
# `test_dispatch_timeout_ignores_a_past_deadline_task006` below pins the
# new formula directly, and `tests/test_iteration_timeout_contained.py`'s
# pre-existing `test_a_timed_out_iteration_costs_one_iteration_not_the_engine`
# (a real 3-second `iteration_timeout_s` cutting off a genuinely hung
# worker iteration, end to end through the real engine) is the tracked
# test that `iteration_timeout_s` still interrupts a long iteration --
# untouched by this task, since it never sets `job_timeout_s` near the
# iteration and the removed deadline term was never the binding one there
# either.


@pytest.mark.asyncio
async def test_dispatch_timeout_ignores_a_past_deadline_task006(tmp_path):
    """Requirement A.1 (#169): the job deadline stops starting iterations
    instead of shortening them. A deadline that is already effectively
    "now" (`job_timeout_s=0`) used to collapse a non-reflect phase's
    dispatched subprocess timeout down to a 60s floor (`max(60, deadline -
    now)`, min-ed against `iteration_timeout_s`); `budget_left()` is the
    one place the deadline should act (refusing to START the next
    iteration at all), so the dispatched timeout for an iteration that DID
    start must equal the configured `iteration_timeout_s` regardless of
    how far in the past the deadline already is."""
    sup = _pause_supervisor(tmp_path, job_timeout_s=0, iteration_timeout_s=3600)
    runner = _TimeoutCapturingRunner()
    sup.runner = runner  # type: ignore[assignment]

    await sup._run_iteration_once("worker")

    assert runner.timeouts[-1] == sup.cfg.iteration_timeout_s == 3600


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

    sup = _pause_supervisor(tmp_path, job_timeout_s=3600)
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


# -- pin 5 (task 007, requirement A.2): a deadline that expires WHILE a
# worker iteration is genuinely in flight does not disturb that iteration's
# own outcome -- it ends on its own exit code (no engine-side cutoff, no
# `faultClass: "signal"`), and the completion claim it reports is applied to
# the fixture's `tasks.json` exactly as any other successful worker
# iteration's claim would be. Built on `make_supervisor` (`tests/
# conftest.py`), never this run's own directory -- the SAME blocking-runner/
# fake-monotonic-clock technique pin 4 above uses to hold an iteration
# genuinely in flight with no real sleep of any kind on either side.


@pytest.mark.asyncio
async def test_deadline_expiring_mid_flight_leaves_the_iterations_own_outcome_untouched(
        tmp_path, monkeypatch):
    fake_time = _FakeMonotonic(real_time, start=1_000_000.0)
    monkeypatch.setattr(loop_mod, "time", fake_time)

    # Two tasks, not one: this fixture predates task 015 (requirement E.1,
    # #169), which retired the off-budget grace review that a single
    # completed task used to trigger here (`_maybe_grace_review`) instead
    # of ending the pass with the one worker iteration this probe drives.
    # Kept at two tasks anyway -- it is still the simplest fixture that
    # keeps `_outstanding_for_completion` non-empty after the claim lands,
    # which is what actually drives the assertion below.
    sup = make_supervisor(
        tmp_path, run_id="task007-inflight-deadline", prd=True,
        vigilant=False, job_timeout_s=100,
        tasks=[{"id": "001", "title": "do the thing",
                "successCriteria": "the thing is done", "status": "pending"},
               {"id": "002", "title": "do another thing",
                "successCriteria": "the other thing is done",
                "status": "pending"}])
    orig_deadline = sup.deadline

    entered = asyncio.Event()
    release = asyncio.Event()

    class _BlockingCompleteRunner:
        """Mirrors pin 4's `_BlockingRunner` exactly (signals `entered` the
        instant it is called, then parks on `release`), but on release
        reports the worker's own `verdict: "complete"` contract payload
        for the task the harness assigned -- read off disk, exactly like
        `test_review_interrupted_not_rejection.py`'s `_complete_task`
        does -- so this pins the full production claim-application path,
        not just the dispatch-timeout formula pin 4/pin 3 already cover."""

        def interrupt(self) -> bool:
            return False

        async def run(self, prompt, transcript, **kw) -> IterationResult:
            entered.set()
            await release.wait()
            doc = sup.run.read_tasks()
            task_id = next(t["id"] for t in doc["tasks"]
                          if t["status"] == "in-progress")
            r = IterationResult(exit_code=0)
            r.final_text = "done\n" + json.dumps(
                {"verdict": "complete", "task_id": task_id})
            r.duration_s = 1.0
            r.usage = {"input": 10, "output": 5, "totalTokens": 15}
            return r

    sup.runner = _BlockingCompleteRunner()  # type: ignore[assignment]

    task = asyncio.ensure_future(sup._step_worker())
    await asyncio.wait_for(entered.wait(), timeout=5)

    # The iteration is genuinely in flight -- blocked inside the runner,
    # nothing recorded yet -- when the deadline itself expires.
    assert not task.done()
    assert sup.budget_left() is True  # not yet: the deadline is still ahead
    fake_time.advance(200.0)  # now well past orig_deadline (job_timeout_s=100)
    assert sup.deadline == orig_deadline, "nothing paused/credited here"
    assert sup.budget_left() is False, "the deadline must have genuinely expired"
    assert not task.done(), "the in-flight iteration must not be disturbed"

    release.set()
    advanced = await asyncio.wait_for(task, timeout=5)

    # The iteration ended on its OWN exit code, never the engine's: no
    # signal-shaped cutoff, no timeout, and the fault classifier's verdict
    # is null (a clean success), never "signal".
    meta = json.loads((sup.run.iteration_dir(1) / "meta.json").read_text())
    assert meta["exitCode"] == 0
    assert meta["interrupted"] is False
    assert meta["timedOut"] is False
    assert meta.get("faultClass") != "signal"
    assert meta.get("faultClass") is None

    # The completion claim that iteration made -- `{"verdict": "complete",
    # "task_id": "001"}` -- was applied to the fixture's OWN tasks.json,
    # exactly like any other successful worker iteration's claim; task
    # "002" (never touched) is untouched proof this is the real per-task
    # write, not some coarser fixture-wide stub.
    tasks_doc = sup.run.read_tasks()
    task_status = {t["id"]: t["status"] for t in tasks_doc["tasks"]}
    assert task_status["001"] == "completed"
    assert task_status["002"] == "pending"

    # Exactly one iteration ran: the second task still outstanding means
    # `_outstanding_for_completion` is non-empty, so the loop returns to
    # its own `while self.budget_left():` guard -- which now reads False,
    # the deadline having expired while the first iteration was in flight
    # -- and ends the pass here.
    assert advanced == ("tail",)
    assert sup.iterations_used == 1


# ===========================================================================
# Section C -- task 016 (PRD requirement F.3, #146): deadline mid-review
# ===========================================================================

def _ok(text: str = "working") -> IterationResult:
    r = IterationResult(exit_code=0)
    r.final_text = text
    r.duration_s = 30.0
    r.usage = {"input": 10, "output": 5, "totalTokens": 15}
    return r


def _interrupted(sup: LoopSupervisor | None = None, _prompt: str = "") -> IterationResult:
    """The engine's own cutoff -- see `tests/test_review_interrupted_not_
    rejection.py`'s `_interrupted` for why `final_text`/`usage` are non-empty
    (a no-traffic interrupt is scored `infra` and silently retried before
    `_step_review` ever sees it)."""
    r = IterationResult(exit_code=None)
    r.interrupted = True
    r.duration_s = 5.0
    r.final_text = "partial output before the engine cut this attempt short"
    r.usage = {"input": 3, "output": 1, "totalTokens": 4}
    return r


class _ScriptedAgent:
    def __init__(self, sup: LoopSupervisor, handlers: dict):
        self.sup = sup
        self.handlers = handlers
        self.calls = 0
        self.running = False

    def interrupt(self) -> bool:
        return self.running

    async def run(self, prompt, transcript, **kw) -> IterationResult:
        self.calls += 1
        phase = self.sup.run.read_status().get("phase")
        self.running = True
        try:
            queue = self.handlers.get(phase)
            if queue is None:
                raise AssertionError(
                    f"no scripted handler registered for phase {phase!r} "
                    f"(call {self.calls})")
            if isinstance(queue, list):
                if not queue:
                    raise AssertionError(
                        f"{phase} asked for a {self.calls}th time; script "
                        f"had no entries left for phase {phase!r}")
                step = queue.pop(0)
            else:
                step = queue
            return step(self.sup, prompt)
        finally:
            self.running = False


def _plan(sup: LoopSupervisor, _prompt: str = "") -> IterationResult:
    atomic_write_json(sup.run.root / "plan-proposal.json", {"version": 1, "tasks": [
        {"id": "001", "title": "do the thing",
         "successCriteria": "the thing is done"},
    ]})
    return _ok("planned\n" +
              json.dumps({"proposal_path": "plan-proposal.json", "summary": "planned"}))


def _complete_task(sup: LoopSupervisor, _prompt: str = "") -> IterationResult:
    doc = sup.run.read_tasks()
    task_id = doc["tasks"][0]["id"]
    return _ok("done\n" + json.dumps({"verdict": "complete", "task_id": task_id}))


def _verified(sup: LoopSupervisor, _prompt: str = "") -> IterationResult:
    return _ok("looks right\n" + json.dumps({"verdict": "verified"}))


_review_supervisor = lambda root, handlers, run_id='task016-review-deadline', **cfg: make_supervisor(
    root, run_id=run_id, status={"state": "starting"}, prd="# PRD\n\nDo the thing.\n",
    max_approaches=2, vigilant=False, on_complete="exit", infra_retry_backoff_s=[0.0],
    infra_retry_backoff_max_s=0.0, infra_outage_budget_s=1000.0,
    runner_factory=lambda sup: _ScriptedAgent(sup, handlers), **cfg)


def _rejected_events(run: RunDir) -> list[dict]:
    return [e for e in read_events(run.root, types=("log",))
            if "review rejected approach" in (e.get("message") or "")]


def _unreviewed_events(run: RunDir) -> list[dict]:
    return read_events(run.root, types=("review_deadline_unreviewed",))


# ---------------------------------------------------------------------------
# 1. Ordinary path: self-loop exhaustion ends with a null verdict, not
#    `"rejected"` and (task 009, v0.24 requirement B.2, #169) not the
#    `"unreviewed"` string either -- a held run carries no verdict at all.
# ---------------------------------------------------------------------------

def test_ordinary_path_interrupted_review_at_budget_end_carries_a_null_verdict(tmp_path):
    handlers = {
        "planning": [_plan],
        "worker": [_complete_task],
        "review": [_interrupted],
    }
    # 3 iterations exactly: plan, worker(COMPLETE), review(interrupted) --
    # the ordinary budget is gone the instant that third attempt returns.
    sup = _review_supervisor(tmp_path / "run", handlers, iterations=3)
    state = asyncio.run(sup._run_job_core())

    assert state == "failed"
    status = sup.run.read_status()
    assert status["verdict"] is None
    assert status["reason"] == "deadline during review"
    assert status["approach"] == 1
    assert not (sup.run.root / "approaches" / "01").exists()
    assert _rejected_events(sup.run) == []
    assert len(_unreviewed_events(sup.run)) == 1
    assert _unreviewed_events(sup.run)[0]["approach"] == 1


# ---------------------------------------------------------------------------
# 2. RETIRED (task 015, requirement E.1, #169): the off-budget grace review
#    itself interrupted used to land here with the same null-verdict/
#    "deadline during review" outcome as section 1 above. Requirements A
#    and D already make the ordinary review path finish before the
#    budget/deadline cuts it short, so the grace review's own bonus
#    attempt is gone -- this exact scenario (worker completes the LAST
#    task on the LAST budgeted iteration, so review's own state is never
#    even entered) now holds `timed_out` instead, pinned below.
# ---------------------------------------------------------------------------

def test_worker_budget_exhaustion_with_all_tasks_complete_holds_timed_out(tmp_path):
    """What replaced the retired off-budget grace review's own
    review-interrupted branch: a worker that completes the LAST task on
    the LAST budgeted iteration never even reaches review's own state --
    `_step_worker`'s own `budget_left()` check fires first and the run
    holds `timed_out` (task 011, requirement C.1, #169's own pre-existing
    freeze path), never a review verdict of any kind, never the retired
    `unreviewed`/"deadline during review" outcome this scenario used to
    produce through the grace review's own interrupted attempt."""
    handlers = {
        "planning": [_plan],
        "worker": [_complete_task],
    }
    # 2 iterations: plan, worker(COMPLETE) -- review's own state is never
    # entered; `_step_worker`'s own out-of-budget check fires first.
    sup = _review_supervisor(tmp_path / "run", handlers, iterations=2)
    state = asyncio.run(sup._run_job_core())

    assert state == "timed_out"
    status = sup.run.read_status()
    assert status["verdict"] is None
    assert status["approach"] == 1
    assert not (sup.run.root / "approaches" / "01").exists()
    assert _rejected_events(sup.run) == []
    assert _unreviewed_events(sup.run) == []
    grace_logs = [e for e in read_events(sup.run.root, types=("log",))
                  if "grace review" in (e.get("message") or "")]
    assert not grace_logs


# ---------------------------------------------------------------------------
# 3. Resume with a fresh window re-reviews the SAME approach.
# ---------------------------------------------------------------------------

def test_resume_after_unreviewed_lands_on_review_same_approach_and_succeeds(tmp_path):
    root = tmp_path / "run"
    handlers1 = {
        "planning": [_plan],
        "worker": [_complete_task],
        "review": [_interrupted],
    }
    sup1 = _review_supervisor(root, handlers1, iterations=3)
    state1 = asyncio.run(sup1._run_job_core())
    assert state1 == "failed"
    assert sup1.run.read_status()["verdict"] is None

    # Episode 2: a FRESH LoopSupervisor over the SAME run dir -- what
    # `ralphctl resume` leaves behind (`self._pause` etc. are in-memory
    # only; nothing here carries over).
    handlers2 = {"review": [_verified]}
    sup2 = _review_supervisor(root, handlers2, iterations=6)  # fresh, bigger window

    assert sup2._unreviewed_awaiting_resume(1) is True
    start_approach, skip_planning, step = sup2._resume_point()
    assert (start_approach, step) == (1, "review")
    assert step != "worker"
    assert step != "replan"

    state2 = asyncio.run(sup2._run_job_core())

    assert state2 == "succeeded"
    status2 = sup2.run.read_status()
    assert status2["verdict"] == "verified"
    # the SAME approach was re-reviewed -- never a fresh one.
    assert status2["approach"] == 1
    assert not (sup2.run.root / "approaches" / "01").exists()
    # exactly one iteration ran this episode: the review resume routed to
    # directly, no throwaway worker pass first.
    assert sup2.runner.calls == 1


def test_resume_of_a_timed_out_budget_exhaustion_run_does_not_reenter_review(tmp_path):
    """RETIRED/replaced (task 015, requirement E.1, #169): this used to pin
    that a resume after the retired off-budget grace review's own
    interrupted attempt lands back on `review`, same as an ordinary
    unreviewed ending does (`test_resume_after_unreviewed_lands_on_review_
    same_approach_and_succeeds` above). That scenario -- worker completes
    the LAST task on the LAST budgeted iteration -- now holds `timed_out`
    with NO `review_deadline_unreviewed` event at all (review's own state
    is never entered, see `test_worker_budget_exhaustion_with_all_tasks_
    complete_holds_timed_out` above), so there is nothing `_unreviewed_
    awaiting_resume` could find here: this is a different terminal shape
    now, not one this file's resume family covers."""
    root = tmp_path / "run"
    handlers1 = {
        "planning": [_plan],
        "worker": [_complete_task],
    }
    sup1 = _review_supervisor(root, handlers1, iterations=2)
    state1 = asyncio.run(sup1._run_job_core())
    assert state1 == "timed_out"
    assert sup1.run.read_status()["verdict"] is None

    sup2 = _review_supervisor(root, {}, iterations=6)
    assert sup2._unreviewed_awaiting_resume(1) is False


# ---------------------------------------------------------------------------
# 4. A completed iteration since the unreviewed ending stops the predicate
#    from firing on stale evidence (the same staleness guard every sibling
#    resume predicate already has -- `_paused_awaiting_incurable_ruling`'s
#    own pinned case).
# ---------------------------------------------------------------------------

def test_an_iteration_since_the_unreviewed_ending_stops_the_predicate(tmp_path):
    handlers = {
        "planning": [_plan],
        "worker": [_complete_task],
        "review": [_interrupted],
    }
    sup = _review_supervisor(tmp_path / "run", handlers, iterations=3)
    asyncio.run(sup._run_job_core())
    assert sup._unreviewed_awaiting_resume(1) is True

    sup.run.emit("iteration.start", number=99, phase="review")
    assert sup._unreviewed_awaiting_resume(1) is False


# ===========================================================================
# Section F -- task 010 (v0.24 requirement B.3, #169): freeze on the spot
# when entering the `timed_out` hold.
#
# `_finish_timed_out()` is the one call path that enters TIMED_OUT_STATE
# (later tasks -- C.1's budget exhaustion, D.2's deadline-during-review
# rejection -- wire real triggers onto it; this section proves the path
# itself, built on `make_supervisor`, never this run's own directory).
# ===========================================================================

def test_timed_out_freezes_on_the_spot_no_reflect_no_accounting(tmp_path):
    """Entering `timed_out` goes through `_finish()` with the reflect
    iteration, the terminal accounting (`endedAt`, `pendingTerminalState`)
    skipped -- `reflect: true` is configured here specifically so a
    reflect iteration that fired would be caught."""
    sup = make_supervisor(
        tmp_path, run_id="task010-timed-out", reflect=True,
        tasks=[{"id": "T1", "title": "do the thing",
                "successCriteria": "done", "status": "in-progress"}])
    # Position a mid-flight episode the way a real deadline expiring
    # between iterations would leave one: `running`, mid `worker`, approach
    # 1, with an `iteration.start` already on record.
    sup.run.emit("iteration.start", number=1, phase="worker")
    sup.run.update_status(state="running", phase="worker", approach=1)
    sup._approach = 1

    reflect_calls: list[bool] = []
    orig_reflection = sup._run_reflection

    async def _spy_reflection():
        reflect_calls.append(True)
        await orig_reflection()

    sup._run_reflection = _spy_reflection  # type: ignore[method-assign]

    async def _fake_run_job_core():
        # Stands in for the future budget-exhaustion/deadline-during-review
        # call site (C.1/D.2) that decides the ending IS the hold -- the
        # decision itself is out of this task's scope; only the entry
        # mechanism through `_finish()` is.
        return sup._finish_timed_out(reason="deadline expired between iterations")

    sup._run_job_core = _fake_run_job_core  # type: ignore[method-assign]

    iterations_dir = sup.run.root / "iterations"
    before = sorted(p.name for p in iterations_dir.glob("*")) if iterations_dir.exists() else []

    state = asyncio.run(asyncio.wait_for(sup.run_job(), 30.0))

    assert state == "timed_out"
    assert reflect_calls == [], (
        "no reflect iteration may run on the way into the timed_out hold")

    after = sorted(p.name for p in iterations_dir.glob("*")) if iterations_dir.exists() else []
    assert after == before, (
        "no reflect iteration directory may appear in the fixture run's "
        "iterations/ tree for this ending")

    status = sup.run.read_status()
    assert status["state"] == "timed_out"
    assert status["verdict"] is None
    assert status.get("pendingTerminalState") is None
    assert status.get("endedAt") is None, (
        "a held run has not ended -- no terminal accounting")
    # Enough position to resume from: the mid-flight fields this episode
    # already recorded are left intact, not cleared the way a genuine
    # terminal write clears them.
    assert status.get("approach") == 1

    state_events = _events(sup.run, "state")
    assert state_events[-1]["state"] == "timed_out"
    # events.jsonl is flushed synchronously by `RunDir.emit` on every call
    # (task 010's own criterion: readable back immediately, not buffered).
    raw = (sup.run.root / "events.jsonl").read_text()
    assert json.loads(raw.splitlines()[-1])["state"] == "timed_out"

    assert (sup.run.artifacts_dir / "run-record.json").exists()
    assert (sup.run.artifacts_dir / "run-record.md").exists()
    record = json.loads((sup.run.artifacts_dir / "run-record.json").read_text())
    assert record.get("state") == "timed_out"


def test_finish_timed_out_never_runs_the_two_phase_finishing_dance(tmp_path):
    """`_finish_timed_out()` calling `_finish()` directly (not through
    `run_job()`) never leaves the non-terminal `finishing` state on disk
    even with `reflect: true` -- `_finish()`'s own `state ==
    TIMED_OUT_STATE` branch is checked first and returns immediately,
    which is the mechanism `run_job()`'s reflect-skip in the sibling test
    above builds on."""
    sup = make_supervisor(tmp_path, run_id="task010-finish-direct",
                          reflect=True)
    result = sup._finish_timed_out(reason="deadline expired")

    assert result == "timed_out"
    status = sup.run.read_status()
    assert status["state"] == "timed_out"
    assert status["verdict"] is None
    assert status.get("pendingTerminalState") is None
    assert sup._pending_terminal is None, (
        "the timed_out branch must never arm the reflect-promotion "
        "machinery -- there is nothing to promote later")


# ===========================================================================
# Section G -- task 011 (v0.24 requirement C.1, #169): an exhausted
# iteration budget holds through B.3's path.
#
# Section F above proved `_finish_timed_out()`'s own entry mechanism by
# calling it directly / stubbing `_run_job_core`; this section proves the
# REAL trigger this task wires onto it -- `_step_worker`'s ordinary
# `while self.budget_left():` loop running dry with tasks still
# incomplete -- drives the SAME path, through a real `run_job()` call, no
# second implementation of the freeze.
# ===========================================================================

def _task011_no_verdict_worker(sup, prompt):
    """A bare `no_verdict` reply -- the worker loop never finishes a task,
    so `budget_left()` alone (never a grace review: `_maybe_grace_review`
    only fires when every task is already `completed`) is what ends this
    run."""
    r = IterationResult(exit_code=0)
    r.final_text = json.dumps({"verdict": "no_verdict"})
    r.duration_s = 1.0
    r.usage = {"input": 1, "output": 1, "totalTokens": 2}
    return r


def test_iteration_budget_exhaustion_holds_timed_out_through_the_real_worker_loop(tmp_path):
    """A tracked probe: `iterations=2`, one already spent (simulating a
    completed planning attempt via `iterations_used=1`, which also makes
    `_resuming_existing_run_dir()`/`_resume_point()` skip straight to
    `worker` over the pre-seeded, still-`pending` plan) -- `_step_worker`'s
    own `while self.budget_left():` loop runs exactly one real worker
    iteration (spending the last unit of budget), the loop condition then
    reads false with the sole task still incomplete, and
    `_finish_tail_unverified` (this task's own new branch, checked ahead of
    the ordinary `unverified` fallback) routes through `_finish_timed_out`
    -- task 010's B.3 path -- instead of ending `failed`/`unverified`.
    `reflect=True` here specifically so a reflect iteration that fired
    would be caught, exactly like Section F's own no-reflect node."""
    sup = make_supervisor(
        tmp_path, run_id="task011-budget-exhausted", reflect=True,
        max_approaches=1, iterations=2, iterations_used=1, approach=1,
        tasks=[{"id": "T1", "title": "do the thing",
                "successCriteria": "done", "status": "pending"}])
    attach_runner(sup, ScriptedRunner({"worker": [_task011_no_verdict_worker]}))

    reflect_calls: list[bool] = []
    orig_reflection = sup._run_reflection

    async def _spy_reflection():
        reflect_calls.append(True)
        await orig_reflection()

    sup._run_reflection = _spy_reflection  # type: ignore[method-assign]

    iterations_dir = sup.run.root / "iterations"

    state = asyncio.run(asyncio.wait_for(sup.run_job(), 30.0))

    assert state == "timed_out"
    assert sup._budget_exhausted_tail is True, (
        "the plain out-of-budget worker-loop tail must set the C.1 flag "
        "_finish_tail_unverified reads")
    assert reflect_calls == [], (
        "no reflect iteration may run on the way into the timed_out hold")

    # The real worker iteration this last unit of budget bought DOES leave
    # an iteration directory behind (this is a genuine budget-exhaustion
    # drive, not a no-op fixture) -- but none of them may be a `reflect`
    # phase, which is the thing this node actually pins.
    phases = [json.loads((d / "meta.json").read_text()).get("phase")
             for d in iterations_dir.glob("*")] if iterations_dir.exists() else []
    assert "reflect" not in phases, (
        f"no reflect iteration may run on the way into the timed_out hold, "
        f"got phases={phases!r}")
    # Exactly the one real worker iteration the remaining budget afforded
    # ran -- this is a genuine iteration-count exhaustion, not a fixture
    # that started already exhausted.
    assert sup.iterations_used == 2

    status = sup.run.read_status()
    assert status["state"] == "timed_out"
    assert status["verdict"] is None
    assert status.get("pendingTerminalState") is None
    assert status.get("endedAt") is None, (
        "a held run has not ended -- no terminal accounting")

    tasks = sup.run.read_tasks()["tasks"]
    assert tasks[0]["status"] != "completed", (
        "the sole task never completed -- this is a genuine budget-only "
        "hold, not a disguised grace-review success")

    state_events = _events(sup.run, "state")
    assert state_events[-1]["state"] == "timed_out"


# ===========================================================================
# Section H -- task 012 (v0.24 requirement C.2, #169): exhausting
# max_approaches still fails the run, unchanged by task 011's C.1.
#
# `set_max_approaches`' own docstring (`loop.py:2811`-`:2814`) names the two
# places `self.cfg.max_approaches` is read at a replan decision: `_step_
# replan`'s own `if self._approach < self.cfg.max_approaches:` (its
# `budget_or_approach_exhausted` edge), and `_step_review`'s post-rejection
# twin, `if approach < self.cfg.max_approaches:` -- the bottom archive/
# replan tail an ordinary, uncured, unescalated rejection falls through to
# once every earlier branch (incurable/curable/repeated-signature/ruled)
# has declined to act -- whose own `rejected_no_approach_left` edge is the
# SAME shape. (`_step_verdict_gate` carries a THIRD, structurally identical
# `approach < self.cfg.max_approaches` check of its own, `verdict_gate_
# rejected_no_approach_left`, already pinned end to end by `tests/test_
# peer_verdict_gate.py::test_gate_on_rejection_with_no_approach_left_fails_
# open_to_finishing` -- not re-pinned here.)
#
# Both sites already live on a SEPARATE code path from the budget boundary
# task 011 built (`self._budget_exhausted_tail`, set only by the five plain
# `if not self.budget_left(): return ("tail",)` guards) -- `_finish_tail_
# unverified`'s own docstring already names both `_step_replan`'s and
# `_step_review`'s/`_step_verdict_gate`'s `..._no_approach_left` edges in its
# C.1 exception list, and neither call site ever sets that flag. So this
# section is a REGRESSION PIN, not a separation: no code moved, nothing was
# shared to begin with, and each node below asserts `_budget_exhausted_tail
# is False` alongside the terminal `failed` read, so a future change that
# DID start sharing the path would be caught here.
# ===========================================================================


def _task012_worker_completes(task_id):
    def handler(sup, prompt):
        r = IterationResult(exit_code=0)
        r.final_text = json.dumps({"verdict": "complete", "task_id": task_id})
        r.duration_s = 0.5
        r.usage = {"input": 1, "output": 1, "totalTokens": 2}
        return r
    return handler


def _task012_review_rejects(reason):
    def handler(sup, prompt):
        r = IterationResult(exit_code=0)
        r.final_text = json.dumps({"verdict": "rejected", "reason": reason})
        r.duration_s = 0.5
        r.usage = {"input": 1, "output": 1, "totalTokens": 2}
        return r
    return handler


def test_step_replans_own_check_exhausts_max_approaches_to_failed(tmp_path):
    """`_step_replan`'s OWN read of the live budget (docstring site 1).

    Reachable at this state only because the run already arrived at
    `replan` from an edge that checked the SAME live field at an earlier
    moment (`_step_review`'s/`_step_verdict_gate`'s own approach-left
    edges both check `approach < self.cfg.max_approaches` before ever
    transitioning to `replan`; the worker stagnation guard's uncured
    `stagnation_exhausted` edge, `loop.py:9584`, transitions to `replan`
    with no approach check of its own at all, relying on `_step_replan`
    to be the one that actually decides) -- a live `PATCH /config/budget`
    lowering `maxApproaches` between that earlier decision and this
    dispatch is exactly why two separate reads of one live field exist.
    Seeded directly at `approach == max_approaches` (the post-PATCH
    shape), `_step_replan()` must read the CURRENT value, not one carried
    from whichever edge put the run in `replan`, and take `budget_or_
    approach_exhausted` straight to the ordinary `failed` tail through
    `_finish_tail_unverified()` -- never `_finish_timed_out()`, which is
    what task 011's `_budget_exhausted_tail` flag would route it through
    if this edge ever started setting it."""
    sup = make_supervisor(tmp_path, run_id="task012-step-replan-exhausted",
                          max_approaches=2, approach=2)

    outcome = sup._step_replan()

    assert outcome == ("tail",)
    assert sup._budget_exhausted_tail is False, (
        "max_approaches exhaustion must never set the C.1 budget-boundary "
        "flag -- it is a structural tail, not a budget one")

    state = sup._finish_tail_unverified()

    assert state == "failed"
    status = sup.run.read_status()
    assert status["state"] == "failed"
    assert status["verdict"] == "unverified"


def test_review_post_rejection_tail_exhausts_max_approaches_to_failed(tmp_path):
    """`_step_review`'s post-rejection twin (docstring site 2): an
    ordinary, bare-reason rejection (no findings.json written, so none of
    the curable/incurable/repeated-signature/ruled branches above the
    bottom tail ever fire) on the LAST approach falls straight through to
    `rejected_no_approach_left`, driven through a real `run_job()` over
    `make_supervisor` (never this run's own directory) -- the in-process
    sibling of `tests/flows/test_flow_review_replan_pause.py::test_a_
    rejection_on_the_last_approach_with_nothing_curable_fails`, which
    already pins the same edge end to end over a real engine subprocess;
    this node additionally asserts the C.1 flag directly, which that flow
    test (reading only `state`/`verdict`) does not."""
    sup = make_supervisor(
        tmp_path, run_id="task012-review-tail-exhausted", max_approaches=1,
        approach=1, iterations_used=1, prd=True,
        tasks=[{"id": "T1", "title": "do the thing", "successCriteria": "done",
               "status": "pending"}])
    attach_runner(sup, ScriptedRunner({
        "worker": [_task012_worker_completes("T1")],
        "review": [_task012_review_rejects("no budget left to try again")],
    }))

    state = asyncio.run(asyncio.wait_for(sup.run_job(), 30.0))

    assert state == "failed"
    assert sup._budget_exhausted_tail is False, (
        "an ordinary review rejection with no approach left must never set "
        "the C.1 budget-boundary flag -- it is a structural tail, not a "
        "budget one")
    status = sup.run.read_status()
    assert status["state"] == "failed"
    assert status["verdict"] == "unverified"


# ===========================================================================
# Section I -- task 013 (v0.24 requirement D.1, #169): a passing verdict
# after the deadline concludes the run normally.
#
# `_step_review` (loop.py) declines to dispatch a NEW review iteration once
# `budget_left()` is already False at the boundary (task cure-01-03,
# requirement A.1) -- but a review that was ALREADY dispatched while budget
# was still available runs to completion under its own
# `iteration_timeout_s` regardless of the clock, exactly like every other
# in-flight iteration requirement A protects. So the deadline here is
# pushed into the past from INSIDE the running review iteration itself --
# after `_step_review` has already consulted `budget_left()` and dispatched
# it, never before -- and a passing verdict rendered from that point must
# still reach `_finish("succeeded", ...)` -- loop.py's `_finish(self,
# state, verdict, **fields)` (the one call site that decides `state` and
# `verdict` together) -- exactly like an in-budget pass does: never the
# `timed_out` hold task 010/011 built, and no resume-position marker
# (`review_deadline_unreviewed` / `_unreviewed_awaiting_resume`) either,
# since review positively DID render a judgement this approach.
# ===========================================================================


def _task013_worker_completes(sup, prompt):
    r = IterationResult(exit_code=0)
    r.final_text = json.dumps({"verdict": "complete", "task_id": "T1"})
    r.duration_s = 0.5
    r.usage = {"input": 1, "output": 1, "totalTokens": 2}
    return r


def _task013_review_verified_expires_deadline_mid_flight(sup, prompt):
    """Expires the deadline from INSIDE the running review iteration --
    `_step_review` has already consulted `budget_left()` (True) and
    dispatched this very iteration before this function ever runs, so
    this is the deadline landing WHILE review runs, never before its own
    dispatch (task cure-01-03: the shipped D.1 probe used to expire the
    deadline between the worker's COMPLETE and review's dispatch, which
    passed for the wrong reason -- `_step_review` dispatched unconditionally
    regardless of the clock -- and would no longer exercise D.1 at all now
    that `_step_review` declines a boundary dispatch on an already-expired
    clock).

    Both halves of the deadline -- `self.deadline` (monotonic, what
    `budget_left()` actually compares against) and its wall-clock twin
    `self._deadline_epoch` -- moved together, the same pair `set_job_
    deadline`/`_extend_deadline_by` always move together, and set squarely
    into the past (not merely nudged: the default job timeout is 8 hours,
    so a relative nudge of a few thousand seconds would still read as "in
    the future").
    """
    sup.deadline = real_time.monotonic() - 1.0
    sup._deadline_epoch = real_time.time() - 1.0
    r = IterationResult(exit_code=0)
    r.final_text = json.dumps({"verdict": "verified"})
    r.duration_s = 0.5
    r.usage = {"input": 1, "output": 1, "totalTokens": 2}
    return r


def test_a_passing_verdict_after_the_deadline_concludes_the_run_normally(tmp_path):
    """A tracked probe over `make_supervisor` (never this run's own
    directory): `iterations_used=1` plus a pre-seeded task skip planning
    straight to `worker` (the same shape task 012's own nodes
    (`test_step_replans_own_check_exhausts_max_approaches_to_failed` et al.)
    use). The worker completes the sole task while the deadline is still
    ordinarily in the future, so review is dispatched normally (`_step_
    review`'s own `budget_left()` boundary check still passes); the
    deadline is then pushed into the past from INSIDE the running review
    iteration itself, the same boundary a real deadline expiring while
    review is already running would cross. Review still renders an
    ordinary `verified` verdict, and the run must still reach `succeeded`,
    never the `timed_out` hold, and must leave no resume position behind.
    """
    sup = make_supervisor(
        tmp_path, run_id="task013-passing-verdict-past-deadline",
        max_approaches=1, approach=1, iterations_used=1, prd=True,
        tasks=[{"id": "T1", "title": "do the thing", "successCriteria": "done",
               "status": "pending"}])
    attach_runner(sup, ScriptedRunner({
        "worker": [_task013_worker_completes],
        "review": [_task013_review_verified_expires_deadline_mid_flight],
    }))

    assert sup.budget_left() is True, (
        "precondition: the deadline is still ordinarily in the future "
        "before review is ever dispatched")

    state = asyncio.run(asyncio.wait_for(sup.run_job(), 30.0))

    assert sup.budget_left() is False, (
        "precondition actually exercised: the deadline really is in the "
        "past by the time review's own verdict is read back -- expired "
        "from INSIDE the running review iteration, never before its "
        "dispatch (task cure-01-03)")
    assert state == "succeeded", (
        f"normally, got state={state!r}")
    status = sup.run.read_status()
    assert status["state"] == "succeeded"
    assert status["state"] != "timed_out"
    assert status["verdict"] in ("verified", "verified-with-residuals")

    # Never entered the hold: no pause reason, no terminal-write left
    # pending, and the run genuinely ended.
    assert status.get("pauseReason") is None
    assert status.get("pendingTerminalState") is None
    assert status.get("endedAt") is not None, "a concluded run has ended"

    # Wrote no resume position: review positively rendered a judgement this
    # approach, so the deadline-mid-review marker (task 016's own
    # `review_deadline_unreviewed` event) and its `_unreviewed_awaiting_
    # resume` predicate must both stay unset -- a fresh episode over this
    # run dir would find nothing to resume into.
    assert read_events(sup.run.root, types=("review_deadline_unreviewed",)) == []
    assert sup._unreviewed_awaiting_resume(1) is False


# ===========================================================================
# Section J -- task 014 (v0.24 requirement D.2, #169): a rejecting verdict
# after the deadline holds the run.
#
# The pre-named D petition ("the verdict is not readable at the point the
# run's ending is chosen") was checked again for this sibling task and NOT
# filed: `_finish(self, state, verdict, **fields)` (loop.py:3376) is still
# the one call site that decides `state` and `verdict` together, reached
# here through `_finish_tail_unverified` -> `_finish_timed_out` exactly as
# it is for the plain budget-exhaustion tail (task 011, C.1).
#
# Same shape as Section I (task 013): the worker completes the sole task
# while the deadline is still ordinarily in the future, so `_step_review`'s
# own `budget_left()` boundary check (task cure-01-03) still passes and
# review is dispatched normally -- the deadline is pushed into the past
# from INSIDE that already-running review iteration itself, never before
# its dispatch, and this time review renders a REJECTING verdict with one
# blocking, curable finding. The rejection runs its ordinary ladder: with
# `max_approaches=2` and `approach=1`, no incurable/repeated-signature/
# ruled branch applies (a fresh approach, one curable finding, no prior
# rejection signature), so `_review_cure_pass_available` takes the
# cure-pass edge -- `_take_cure_pass` appends one cure task to tasks.json
# and bumps `curePasses["1"]` to 1 -- and `_step_cure` advances straight to
# `worker`, in the same dispatch pass. `_step_worker`'s own `while self.
# budget_left():` loop (one of the plain out-of-budget `_budget_exhausted_
# tail` sites, task 011) never runs its body -- the deadline is already
# past -- and the freshly-inserted cure task is still `pending`, so
# `_all_tasks_completed()` is False and `_maybe_grace_review` never even
# attempts a second review: the run holds `timed_out` with the rejection's
# findings and cure task intact on disk, exactly as requirement D.2 asks.
# ===========================================================================


def _task014_worker_completes(sup, prompt):
    r = IterationResult(exit_code=0)
    r.final_text = json.dumps({"verdict": "complete", "task_id": "T1"})
    r.duration_s = 0.5
    r.usage = {"input": 1, "output": 1, "totalTokens": 2}
    return r


def _task014_review_rejects_curable_expires_deadline_mid_flight(sup, prompt):
    """Same reasoning as Section I's `_task013_review_verified_expires_
    deadline_mid_flight`: `_step_review` has already consulted `budget_
    left()` (True) and dispatched this very iteration before this
    function ever runs, so the deadline lands WHILE review runs, never
    before its own dispatch (task cure-01-03).

    Written from inside the scripted review step itself -- the same
    position a real reviewer hat's own sidecar write would occupy,
    immediately before its contract payload is read back.
    """
    sup.deadline = real_time.monotonic() - 1.0
    sup._deadline_epoch = real_time.time() - 1.0
    findings.write_findings_json(sup.run.findings_json_file, [
        {"severity": "blocking", "curable": True,
         "clause": "requirement D.2 fixture clause unmet",
         "evidence": "the thing is not actually done",
         "cured_when": "the thing is actually done"},
    ])
    r = IterationResult(exit_code=0)
    r.final_text = json.dumps({"verdict": "rejected", "reason": "not done yet"})
    r.duration_s = 0.5
    r.usage = {"input": 1, "output": 1, "totalTokens": 2}
    return r


def test_a_rejecting_verdict_after_the_deadline_holds_the_run(tmp_path):
    """Same fixture shape as Section I's own passing-verdict sibling
    (`test_a_passing_verdict_after_the_deadline_concludes_the_run_normally`):
    `iterations_used=1` plus a pre-seeded task skip planning straight to
    `worker`; the worker completes the sole task while the deadline is
    still ordinarily in the future, so review is dispatched normally; the
    deadline is then pushed into the past from INSIDE the running review
    iteration itself. This time review renders a REJECTING verdict over
    one blocking, curable finding -- the run must hold `timed_out` with a
    null verdict, and the rejection's findings and the cure task it opened
    must both still be on disk, with the ordinary cure-pass counter exactly
    as it would read for an in-budget rejection.
    """
    sup = make_supervisor(
        tmp_path, run_id="task014-rejecting-verdict-past-deadline",
        max_approaches=2, approach=1, iterations_used=1, prd=True,
        operator_input_needed_pause=False,
        tasks=[{"id": "T1", "title": "do the thing", "successCriteria": "done",
               "status": "pending"}])
    attach_runner(sup, ScriptedRunner({
        "worker": [_task014_worker_completes],
        "review": [_task014_review_rejects_curable_expires_deadline_mid_flight],
    }))

    assert sup.budget_left() is True, (
        "precondition: the deadline is still ordinarily in the future "
        "before review is ever dispatched")

    state = asyncio.run(asyncio.wait_for(sup.run_job(), 30.0))

    assert sup.budget_left() is False, (
        "precondition actually exercised: the deadline really is in the "
        "past by the time review renders its verdict")
    assert state == "timed_out", (
        f"a rejecting verdict after the deadline must hold the run in "
        f"B.1's state, got state={state!r}")
    assert sup._budget_exhausted_tail is True, (
        "the hold must reach TIMED_OUT_STATE through the same C.1 flag "
        "the plain budget-exhaustion tail sets, never a second freeze path")

    status = sup.run.read_status()
    assert status["state"] == "timed_out"
    assert status["verdict"] is None
    assert status.get("pendingTerminalState") is None
    assert status.get("endedAt") is None, "a held run has not ended"

    # The rejection's own findings channel is still on disk -- untouched,
    # never archived away, exactly as an in-budget rejection leaves it
    # while parked mid-cure.
    findings_list = findings.read_findings_json(sup.run.findings_json_file)
    assert findings_list is not None and len(findings_list) == 1
    assert findings_list[0]["clause"] == "requirement D.2 fixture clause unmet"
    assert findings_list[0]["severity"] == "blocking"

    # The cure task the rejection opened is present on disk, still pending
    # -- it is what left `_all_tasks_completed()` False and so kept the
    # (pre-E) grace-review mechanism from ever firing a second review.
    tasks = sup.run.read_tasks()["tasks"]
    cure_tasks = [t for t in tasks if str(t.get("id", "")).startswith("cure-")]
    assert len(cure_tasks) == 1, (
        f"the rejection's own cure task must be present on disk, got "
        f"tasks={tasks!r}")
    assert cure_tasks[0]["status"] == "pending"

    # The cure-pass counter reads exactly the value it would for an
    # in-budget rejection: one cure pass spent on approach 1, same shape
    # `_take_cure_pass`/`_cure_passes_for_approach` always persist.
    assert status.get("curePasses", {}).get("1") == 1
    assert sup._cure_passes_for_approach(1) == 1

    state_events = _events(sup.run, "state")
    assert state_events[-1]["state"] == "timed_out"


# ===========================================================================
# Section Jb -- task cure-01-03 (v0.24 requirement A.1/B.3, DoD 1-3, #169):
# the OTHER half of the boundary Sections I and J pin, and the one those two
# sections deliberately do NOT cover.
#
# Sections I/J are the IN-FLIGHT case: `_step_review` consulted
# `budget_left()` (True), dispatched a review iteration, and the deadline
# then expired while that iteration was still running -- the review
# finishes and its verdict is applied (I: `succeeded`; J: the hold, with
# the rejection's findings and cure task intact).
#
# This section is the NO-DISPATCH case: the boundary is ALREADY expired
# when `_step_review` is entered, so requirement A.1 forbids starting a new
# iteration at all -- no review hat process is launched, and the run holds
# `timed_out` with a null verdict through the SAME shared freeze path
# (`_budget_exhausted_tail` -> `_finish_tail_unverified` -> `_finish_timed_
# out`, task 010/011's own C.1 path) every other `_step_*` boundary already
# uses, never review's own `deadline during review` branch.
#
# Both halves of `budget_left()` are exercised (an expired job deadline and
# an exhausted iteration budget), because requirement A.1 names both and
# `_step_review`'s guard is one check over the pair. The sibling no-dispatch
# probe for the mid-plan review episode's own call site lives in
# `tests/test_mid_plan_review_episode.py::test_no_mid_plan_review_episode_
# starts_at_an_already_expired_boundary`.
# ===========================================================================


@pytest.mark.parametrize("boundary", ["deadline", "iterations"])
def test_no_new_review_iteration_starts_at_an_already_expired_boundary(
        tmp_path, boundary):
    """A supervisor entering `review` with its budget already spent
    dispatches NO review iteration (the scripted agent is never called at
    all) and holds `timed_out` with a null verdict through the shared C.1
    freeze path.

    Episode 1 is Section C's own resume shape (plan, worker COMPLETE,
    review interrupted), purely to leave a run directory whose resume
    point genuinely IS `review` with a task already complete -- so
    episode 2's fresh supervisor is positioned exactly where a resumed run
    that still owes a verdict would be, rather than at a boundary this
    file's harness hand-placed. Episode 2 is the probe: its boundary is
    already expired before its first dispatch -- by an expired job
    deadline (`job_timeout_s=0.0`) or by an exhausted iteration budget
    (`iterations_used == iterations`), the two halves `budget_left()`
    checks -- and it must start nothing.
    """
    root = tmp_path / "run"
    sup1 = _review_supervisor(root, {
        "planning": [_plan],
        "worker": [_complete_task],
        "review": [_interrupted],
    }, iterations=3)
    assert asyncio.run(asyncio.wait_for(sup1._run_job_core(), 30.0)) == "failed"
    assert sup1.run.read_status()["verdict"] is None
    iterations_before = len(read_events(root, types=("iteration.start",)))
    assert iterations_before == 3

    # Episode 2: a FRESH supervisor over the SAME run dir (what `ralphctl
    # resume` leaves behind), whose budget is already gone.
    expired = ({"iterations": 6, "job_timeout_s": 0.0} if boundary == "deadline"
               else {"iterations": 3, "iterations_used": 3})
    sup2 = _review_supervisor(root, {"review": [_verified]}, **expired)

    # Preconditions: review really is where this episode would go, and the
    # boundary really is expired before anything is dispatched.
    assert sup2._unreviewed_awaiting_resume(1) is True
    start_approach, _skip_planning, step = sup2._resume_point()
    assert (start_approach, step) == (1, "review")
    assert sup2.budget_left() is False
    if boundary == "deadline":
        # the deadline half, not the iteration half: budget remains
        assert (sup2.iterations_used - sup2._infra_refunded
                - sup2._steer_refunded - sup2._adjudication_refunded) < sup2.cfg.iterations
        assert real_time.monotonic() >= sup2.deadline
    else:
        # the iteration half, not the deadline half: wall clock remains
        assert real_time.monotonic() < sup2.deadline
        assert sup2.iterations_used == sup2.cfg.iterations

    state = asyncio.run(asyncio.wait_for(sup2._run_job_core(), 30.0))

    # 1. NO new review iteration was dispatched: the scripted agent (which
    # would have returned an ordinary `verified` verdict, concluding the
    # run `succeeded`) was never called, and no `iteration.start` joined
    # episode 1's three.
    assert sup2.runner.calls == 0, (
        "requirement A.1: an already-expired boundary must start NO new "
        "review iteration")
    assert len(read_events(root, types=("iteration.start",))) == iterations_before
    assert state != "succeeded"

    # 2. ... and the run held `timed_out` with a null verdict.
    assert state == "timed_out"
    status = sup2.run.read_status()
    assert status["state"] == "timed_out"
    assert status["verdict"] is None
    assert status.get("endedAt") is None, "a held run has not ended"
    assert status.get("pendingTerminalState") is None

    # 3. ... through the SHARED freeze path (task 011's own C.1 flag into
    # `_finish_timed_out`), never review's own deadline-during-review
    # branch and never a second freeze implementation.
    assert sup2._budget_exhausted_tail is True
    assert sup2._review_deadline_unreviewed is False
    assert status["reason"] != "deadline during review"
    assert _events(sup2.run, "state")[-1]["state"] == "timed_out"


# ===========================================================================
# Section K -- task 017 (v0.24 requirement F.1, #169): a bare `ralphctl
# resume` grants a held (`timed_out`) run a fresh full window.
#
# The PRD (F.1) is explicit that no engine code changes here: `self.deadline`
# / `self._deadline_epoch` are computed once, unconditionally, in
# `LoopSupervisor.__init__` from `cfg.job_timeout_s` (loop.py:524/537) --
# never seeded from a prior episode's `status.json` `deadlineAt` -- so a
# fresh process (a new container, which is exactly what `cmd_resume` starts)
# ALREADY grants a fresh full window, whatever state the run dir was left
# in. `make_supervisor` constructing a `LoopSupervisor` IS that fresh
# process, so these nodes pin the existing mechanism directly rather than
# separating anything. `--timeout`'s override semantics against `job.yaml`
# (`_apply_job_timeout_topup`) are already pinned end to end by
# `tests/test_cli_features.py::test_resume_timeout_topup_bumps_job_yaml`
# (task 004) -- not re-pinned here, since nothing about that path changed.
# ===========================================================================

def _task017_status_with_stale_deadline(state: str, *, approach: int = 1) -> dict:
    """The shape a genuinely held run dir left on disk: a `deadlineAt` that
    already passed (long before `real_time.time()`), so a resume that
    accidentally seeded `self._deadline_epoch`/`self.deadline` from it
    instead of from `cfg.job_timeout_s` would immediately re-expire -- the
    exact regression this section exists to catch."""
    return {"state": state, "approach": approach,
           "deadlineAt": "2020-01-01T00:00:00+00:00"}


def test_bare_resume_of_a_timed_out_run_grants_a_fresh_full_window(tmp_path):
    job_timeout_s = 100
    before_monotonic = real_time.monotonic()
    before_epoch = real_time.time()

    sup = make_supervisor(
        tmp_path, run_id="task017-timedout-fresh-window",
        job_timeout_s=job_timeout_s,
        status=_task017_status_with_stale_deadline("timed_out"))

    # A full job_timeout_s from THIS engine start -- not from the stale,
    # already-past `deadlineAt` the held run dir carried on disk.
    assert before_monotonic + job_timeout_s <= sup.deadline <= (
        real_time.monotonic() + job_timeout_s + 5.0)
    assert before_epoch + job_timeout_s <= sup._deadline_epoch <= (
        real_time.time() + job_timeout_s + 5.0)
    assert sup.budget_left(), (
        "a fresh full window must read as time remaining, never as already "
        "expired")


@pytest.mark.parametrize("state", ["failed", "succeeded", "aborted", "paused"])
def test_bare_resume_of_any_other_state_is_unaffected_still_gets_a_fresh_window(
        tmp_path, state):
    """The control: F.1 adds nothing new for `failed`/`succeeded`/`aborted`/
    `paused` -- a bare resume of any of these keeps today's exact behaviour,
    which is the SAME unconditional fresh-window construction `timed_out`
    now relies on too (there is no per-state branch to add or remove)."""
    job_timeout_s = 100
    before_monotonic = real_time.monotonic()

    sup = make_supervisor(
        tmp_path, run_id=f"task017-resume-unaffected-{state}",
        job_timeout_s=job_timeout_s,
        status=_task017_status_with_stale_deadline(state))

    assert before_monotonic + job_timeout_s <= sup.deadline
    assert sup.budget_left()


def _task017_completes(task_id):
    def handler(sup, prompt):
        r = IterationResult(exit_code=0)
        r.final_text = json.dumps({"verdict": "complete", "task_id": task_id})
        r.duration_s = 0.5
        r.usage = {"input": 1, "output": 1, "totalTokens": 2}
        return r
    return handler


def _task017_reviews_verified():
    def handler(sup, prompt):
        r = IterationResult(exit_code=0)
        r.final_text = json.dumps({"verdict": "verified"})
        r.duration_s = 0.5
        r.usage = {"input": 1, "output": 1, "totalTokens": 2}
        return r
    return handler


def test_resumed_timed_out_run_continues_at_the_task_and_iteration_it_stopped_at(
        tmp_path):
    """A held run dir with T1 already `completed` and one iteration already
    recorded must resume straight into `worker` over T2 -- never replanning
    (no fresh `planning` iteration appears on disk) and never re-doing T1
    (the worker script below only ever answers for T2; a re-ask for T1 would
    raise `StopIteration` from the exhausted script)."""
    sup = make_supervisor(
        tmp_path, run_id="task017-timedout-continues", job_timeout_s=100,
        prd=True, iterations_used=1,
        status=_task017_status_with_stale_deadline("timed_out", approach=1),
        tasks=[
            {"id": "T1", "title": "already done", "successCriteria": "done",
             "status": "completed"},
            {"id": "T2", "title": "still pending", "successCriteria": "done",
             "status": "pending"},
        ])
    attach_runner(sup, ScriptedRunner({
        "worker": [_task017_completes("T2")],
        "review": [_task017_reviews_verified()],
    }))

    state = asyncio.run(asyncio.wait_for(sup.run_job(), 30.0))

    assert state == "succeeded"
    status = sup.run.read_status()
    assert status["verdict"] == "verified"
    tasks = sup.run.read_tasks()["tasks"]
    assert {t["id"]: t["status"] for t in tasks} == {
        "T1": "completed", "T2": "completed"}

    # No new `planning` iteration was ever recorded past resume -- the run
    # picked up exactly where it stopped, iteration numbering continuing
    # from the one already on disk (iterations_used=1 seeded above).
    phases = []
    for d in sorted((sup.run.root / "iterations").iterdir()):
        meta_path = d / "meta.json"
        if meta_path.exists():
            phases.append((int(d.name), json.loads(meta_path.read_text()).get("phase")))
    new_phases = [phase for number, phase in phases if number > 1]
    assert new_phases, "the resumed run must have recorded at least one new iteration"
    assert "planning" not in new_phases, (
        f"a resume over an existing plan must never replan, got phases={phases!r}")
    assert new_phases[0] == "worker", (
        "the FIRST iteration after resume must be worker over the SAME "
        f"approach's remaining task, got phases={phases!r}")


# ===========================================================================
# Section D -- task 048 (B.5 follow-on, operator steer 005): paused teardown
# ===========================================================================

import time


def test_stop_on_a_paused_engine_skips_the_sigterm_grace_it_cannot_honour(
        engine_factory):
    e = engine_factory(job={"on_complete": "exit", "max_approaches": 1},
                       stub_env={"STUB_WORKER_STALLS": "1"})
    status = e.wait_state(("paused",), timeout=60)
    assert status["pauseReason"] == "stagnation"
    assert e.proc.poll() is None, "still alive and parked, precondition"

    started = time.monotonic()
    e.stop()
    elapsed = time.monotonic() - started

    assert e.proc.poll() is not None, "stop() must have actually ended it"
    # The ordinary (non-paused) grace is 10s; a paused engine must never
    # wait it out -- bounded well under that, with headroom for a slow box.
    assert elapsed < 5.0, (
        f"stop() on a paused engine took {elapsed:.2f}s -- it must detect "
        "the paused state and go straight to a bounded kill() instead of "
        "waiting out the ordinary SIGTERM grace a paused engine ignores")

    # A second stop() call (what the fixture's own teardown does next) is a
    # no-op on an already-dead process, never a second wait.
    started = time.monotonic()
    e.stop()
    assert time.monotonic() - started < 1.0


# ===========================================================================
# Section E -- task 004 (#163): a paused engine now honours SIGTERM instead
# of blocking on the pause gate.
#
# `LoopSupervisor.abort_on_signal` used to record the abort and interrupt the
# child runner but never touch `self._pause` -- so an engine parked in
# `state: "paused"` (`_park_until_resumed`'s `await self._pause.wait()`)
# never woke up in-process at all: `main.py`'s `await job_task` never
# returned, and the process sat past SIGTERM/SIGINT until something signalled
# it harder (SIGKILL). The fix makes `abort_on_signal` call `self.resume()`
# right after recording the abort, exactly the way `POST /abort`'s own
# handler already calls `loop.abort()` then `loop.resume()`. Section D's own
# `LiveRun.stop()` optimisation (skip the ordinary grace on a paused engine)
# stays in place regardless -- it is now a fast path rather than the only
# path, but a real SIGTERM sent directly (bypassing `stop()`'s special case)
# must still end the engine well inside a short, test-controlled grace.
# ===========================================================================

import os
import signal


def test_a_paused_engine_stops_on_sigterm_inside_a_test_grace_period(
        engine_factory):
    e = engine_factory(job={"on_complete": "exit", "max_approaches": 1},
                       stub_env={"STUB_WORKER_STALLS": "1"})
    status = e.wait_state(("paused",), timeout=60)
    assert status["pauseReason"] == "stagnation"
    assert e.proc.poll() is None, "still alive and parked, precondition"

    # A direct SIGTERM, not `e.stop()`'s own paused-state fast path (section
    # D) -- this pins the engine's own signal handling, not the test
    # harness's teardown shortcut.
    os.kill(e.proc.pid, signal.SIGTERM)

    deadline = time.monotonic() + 10.0
    while e.proc.poll() is None and time.monotonic() < deadline:
        time.sleep(0.1)

    assert e.proc.poll() is not None, (
        "a paused engine must react to SIGTERM by waking the pause gate "
        "and unwinding, not by blocking on it forever")


def test_ralphctl_abort_against_a_paused_engine_stops_it_too(live):
    e = live(run_id="paused-abort", job={"on_complete": "exit",
                                         "max_approaches": 1},
             stub_env={"STUB_WORKER_STALLS": "1"})
    status = e.wait_state(("paused",), timeout=60)
    assert status["pauseReason"] == "stagnation"
    assert e.proc.poll() is None, "still alive and parked, precondition"

    res = e.ralphctl("abort", e.run_id)
    assert res.returncode == 0, res.stderr

    aborted = e.wait_terminal(timeout=10)
    assert aborted["state"] == "aborted"
    # The process itself exits a moment after status.json goes terminal
    # (uvicorn's own shutdown, `on_complete: exit`) -- poll for it rather
    # than asserting the exact instant `wait_terminal` returns.
    deadline = time.monotonic() + 10.0
    while e.proc.poll() is None and time.monotonic() < deadline:
        time.sleep(0.1)
    assert e.proc.poll() is not None, (
        "`ralphctl abort` must actually end a paused engine's process, not "
        "just record the termination on disk")
