"""Deadline / pause / review-clock behaviour, folded (task 028, D.7):

- Requirement B (#72) -- a run that earned COMPLETE does not lose it to the
  clock -- `deadlineBeatVerdict` (task 006, folded as section A below).
- Requirement P -- a paused run's parked time is credited back to the
  deadline, distinguished from an infra-wait credit by `reason` (task 016,
  folded as section B below).
- PRD requirement F.3 (#146) -- a deadline landing mid-review ends the run
  `verdict: "unreviewed"`, never an ordinary rejection, and resumes straight
  back onto review for the same approach (task 016, folded as section C
  below; supersedes the `"unverified"` string section A originally pinned,
  as noted inline there).
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

from ralphd.engine import loop as loop_mod
from ralphd.engine.loop import LoopSupervisor
from ralphd.engine.runner import IterationResult
from ralphd.engine.state import RunDir, atomic_write_json, read_events, utc_from_epoch
from tests.conftest import make_supervisor, supervisor_factory

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
    # Task 016 (requirement F.3, #146): this specific path -- review cut
    # short at budget end -- writes the dedicated "unreviewed" verdict, not
    # the ordinary "unverified" this file was written against; the marker
    # assertions below (this task's own subject) are unaffected.
    assert status["verdict"] == "unreviewed"
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
    assert status["verdict"] == "unreviewed"
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
    assert status2["verdict"] == "unreviewed"
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


# -- pin 3: the extension actually rescues the 60s iteration-timeout floor -


@pytest.mark.asyncio
async def test_pause_credit_avoids_the_60s_floor(tmp_path):
    # A deadline that is already effectively "now" gives every non-reflect
    # phase's per-iteration timeout formula (`max(60, deadline - now)`,
    # min-ed against `iteration_timeout_s`) exactly the 60s floor.
    sup = _pause_supervisor(tmp_path, job_timeout_s=0, iteration_timeout_s=3600)
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
    `_step_review`/`_maybe_grace_review` ever see it)."""
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
# 1. Ordinary path: self-loop exhaustion ends `unreviewed`, not `rejected`.
# ---------------------------------------------------------------------------

def test_ordinary_path_interrupted_review_at_budget_end_is_unreviewed(tmp_path):
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
    assert status["verdict"] == "unreviewed"
    assert status["reason"] == "deadline during review"
    assert status["approach"] == 1
    assert not (sup.run.root / "approaches" / "01").exists()
    assert _rejected_events(sup.run) == []
    assert len(_unreviewed_events(sup.run)) == 1
    assert _unreviewed_events(sup.run)[0]["approach"] == 1


# ---------------------------------------------------------------------------
# 2. The off-budget grace review itself interrupted: same outcome.
# ---------------------------------------------------------------------------

def test_grace_review_interrupted_is_also_unreviewed_not_rejected(tmp_path):
    handlers = {
        "planning": [_plan],
        "worker": [_complete_task],
        "review": [_interrupted],
    }
    # 2 iterations: plan, worker(COMPLETE) -- the ordinary budget is spent
    # the instant the task completes, with review never having started;
    # `_maybe_grace_review` grants one OFF-budget attempt, which is itself
    # interrupted.
    sup = _review_supervisor(tmp_path / "run", handlers, iterations=2)
    state = asyncio.run(sup._run_job_core())

    assert state == "failed"
    status = sup.run.read_status()
    assert status["verdict"] == "unreviewed"
    assert status["reason"] == "deadline during review"
    assert status["approach"] == 1
    assert not (sup.run.root / "approaches" / "01").exists()
    assert _rejected_events(sup.run) == []
    assert len(_unreviewed_events(sup.run)) == 1
    assert _unreviewed_events(sup.run)[0]["approach"] == 1
    # the grace review really was granted and really did run (off-budget,
    # so it does not appear in status.json's iterationsUsed count).
    grace_logs = [e for e in read_events(sup.run.root, types=("log",))
                  if "grace review" in (e.get("message") or "")]
    assert grace_logs


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
    assert sup1.run.read_status()["verdict"] == "unreviewed"

    # Episode 2: a FRESH LoopSupervisor over the SAME run dir -- what
    # `ralphctl resume` leaves behind (`self._pause`/`self._grace_review_
    # granted` etc. are in-memory only; nothing here carries over).
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


def test_resume_after_grace_review_unreviewed_also_lands_on_review(tmp_path):
    root = tmp_path / "run"
    handlers1 = {
        "planning": [_plan],
        "worker": [_complete_task],
        "review": [_interrupted],
    }
    sup1 = _review_supervisor(root, handlers1, iterations=2)
    state1 = asyncio.run(sup1._run_job_core())
    assert state1 == "failed"
    assert sup1.run.read_status()["verdict"] == "unreviewed"

    handlers2 = {"review": [_verified]}
    sup2 = _review_supervisor(root, handlers2, iterations=6)

    assert sup2._unreviewed_awaiting_resume(1) is True
    start_approach, skip_planning, step = sup2._resume_point()
    assert (start_approach, step) == (1, "review")

    state2 = asyncio.run(sup2._run_job_core())
    assert state2 == "succeeded"
    assert sup2.run.read_status()["approach"] == 1


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
