"""Task 011 (#28): a CancelledError escaping an iteration cannot end the job.

Task 010 fixed the one *known* producer of that shape (the runner's
`wait_for(pump_task, ...)` re-await on the timeout path). This module is
about the structural half: the per-iteration guard in
`LoopSupervisor._run_iteration_once` was `except Exception`, and
`asyncio.CancelledError` is a `BaseException` -- so any stray cancellation
from anywhere in the agent plumbing walked straight past it, past the
`except Exception` around `_run_job_core`, and killed the engine. One bad
iteration is allowed to cost one iteration; it is never allowed to end the
run.

What the boundary must and must not swallow (see the comment at the boundary
itself in loop.py):

* a CancelledError raised from *inside* the iteration -> contained, recorded
  as one failed iteration, the loop carries on;
* KeyboardInterrupt / SystemExit -> NOT contained (the engine is being shut
  down and must unwind);
* a cancellation genuinely requested on the iteration's own task from
  outside (`asyncio.Task.cancelling() > 0`) -> NOT contained (somebody asked
  this coroutine to stop).

Mutation case (recorded in the commit message): narrowing the boundary back
to `except Exception as exc` makes
test_a_cancellation_from_inside_the_iteration_is_recorded_not_raised,
test_the_engine_survives_a_cancelled_iteration_and_reaches_its_verdict and
test_a_cancelled_iteration_is_a_recorded_failed_iteration fail.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from ralphd.engine.config import JobConfig
from ralphd.engine.loop import LoopSupervisor
from ralphd.engine.runner import COMPLETE, VERIFIED, IterationResult
from ralphd.engine.state import RunDir, atomic_write_json


# -- scaffolding -----------------------------------------------------------


class _ScriptedRunner:
    """Stands in for PiRunner at the exact seam the boundary guards.

    Each entry of `script` is a callable taking the supervisor's RunDir and
    returning an IterationResult -- or raising, which is the whole point:
    the exception then originates *inside* the iteration, below
    `_run_iteration_once`'s try, like the real 010 defect did. The last
    entry repeats forever.
    """

    def __init__(self, run: RunDir, script: list):
        # NB: not `self.run` -- that would shadow the `run()` method below,
        # which is the very seam the supervisor calls.
        self.rundir = run
        self.script = script
        self.calls = 0
        self.running = False

    def interrupt(self) -> bool:
        return self.running

    async def run(self, prompt, transcript, **kw) -> IterationResult:
        self.calls += 1
        self.running = True
        try:
            step = self.script[min(self.calls - 1, len(self.script) - 1)]
            return step(self.rundir)
        finally:
            self.running = False


def _ok(text: str = "working") -> IterationResult:
    r = IterationResult(exit_code=0)
    r.final_text = text
    r.duration_s = 30.0          # not an "instant" failure
    r.usage = {"input": 10, "output": 5, "totalTokens": 15}
    return r


def _cancel(_run: RunDir) -> IterationResult:
    raise asyncio.CancelledError("stray cancellation from the agent plumbing")


def _supervisor(tmp_path: Path, script: list, **cfg_kw) -> LoopSupervisor:
    run = RunDir(root=tmp_path)
    kw = {"iterations": 8, "max_approaches": 1, "vigilant": False,
          "on_complete": "exit", "infra_retry_backoff_s": [0.0],
          "infra_retry_backoff_max_s": 0.0, "infra_outage_budget_s": 1000.0,
          **cfg_kw}
    sup = LoopSupervisor(JobConfig(run_id="unit", **kw), run, tmp_path)
    sup.runner = _ScriptedRunner(run, script)   # type: ignore[assignment]

    async def no_backoff(seconds):
        return seconds, False

    sup._wait_out_backoff = no_backoff          # type: ignore[method-assign]
    return sup


def _metas(run_dir: Path) -> list[dict]:
    return [json.loads((d / "meta.json").read_text())
            for d in sorted((run_dir / "iterations").iterdir())
            if (d / "meta.json").exists()]


# -- the boundary itself ---------------------------------------------------


@pytest.mark.asyncio
async def test_a_cancellation_from_inside_the_iteration_is_recorded_not_raised(
        tmp_path):
    sup = _supervisor(tmp_path, [_cancel])

    # No CancelledError may cross this call.
    result = await sup._run_iteration_once("worker")

    assert isinstance(result, IterationResult)
    assert "CancelledError" in result.error_message
    assert result.error_message.startswith("engine iteration failure:")


@pytest.mark.asyncio
async def test_a_cancelled_iteration_is_a_recorded_failed_iteration(tmp_path):
    sup = _supervisor(tmp_path, [_cancel])

    await sup._run_iteration_once("worker")

    meta = _metas(tmp_path)[-1]
    assert meta["endedAt"], "reached the normal iteration-recording path"
    assert "CancelledError" in (meta["error"] or "")
    assert meta["faultClass"] is not None, "classified, not scored a success"
    assert meta["sawComplete"] is False
    events = [json.loads(x) for x in
              (tmp_path / "events.jsonl").read_text().splitlines()]
    ends = [ev for ev in events if ev.get("type") == "iteration.end"]
    assert len(ends) == 1 and "CancelledError" in (ends[0]["error"] or "")
    # status.json is still a live, non-terminal run: the iteration failed,
    # the run did not.
    assert sup.run.read_status().get("state") in (None, "running")


@pytest.mark.asyncio
@pytest.mark.parametrize("exc", [KeyboardInterrupt, SystemExit],
                         ids=["keyboard-interrupt", "system-exit"])
async def test_a_shutdown_exception_is_deliberately_not_contained(tmp_path, exc):
    def boom(_run):
        raise exc("engine is being shut down")

    sup = _supervisor(tmp_path, [boom])
    with pytest.raises(exc):
        await sup._run_iteration_once("worker")


@pytest.mark.asyncio
async def test_a_cancellation_requested_from_outside_is_honoured(tmp_path):
    """The other half of "deliberate": when somebody cancels the iteration's
    own task, the boundary must not eat the cancellation and pretend the
    iteration merely failed."""
    started = asyncio.Event()

    async def hang(_prompt, _transcript, **_kw):
        started.set()
        await asyncio.sleep(3600)

    sup = _supervisor(tmp_path, [_ok])
    sup.runner.run = hang                       # type: ignore[method-assign]

    task = asyncio.ensure_future(sup._run_iteration_once("worker"))
    await asyncio.wait_for(started.wait(), timeout=5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert task.cancelled()


# -- the engine around it --------------------------------------------------


def _plan(run: RunDir) -> IterationResult:
    atomic_write_json(run.tasks_file, {"version": 1, "tasks": [
        {"id": "001", "title": "t", "status": "pending",
         "successCriteria": "c"}]})
    return _ok("planned")


def _finish_task(run: RunDir) -> IterationResult:
    doc = run.read_tasks()
    doc["tasks"][0]["status"] = "completed"
    atomic_write_json(run.tasks_file, doc)
    return _ok(f"done {COMPLETE}")


@pytest.fixture
def cancelled_worker_run(tmp_path):
    """A whole job whose FIRST worker iteration dies of a stray
    CancelledError; everything else is healthy."""
    sup = _supervisor(tmp_path, [
        _plan,          # planning
        _cancel,        # worker attempt 1: the stray cancellation
        _finish_task,   # worker (the engine carried on)
        lambda run: _ok(f"looks good {VERIFIED}"),   # review
    ])
    state = asyncio.run(sup._run_job_core())
    return sup, state


def test_the_engine_survives_a_cancelled_iteration_and_reaches_its_verdict(
        cancelled_worker_run):
    sup, state = cancelled_worker_run
    metas = _metas(sup.run.root)
    cancelled = [m for m in metas if "CancelledError" in (m.get("error") or "")]
    assert len(cancelled) == 1, [(m["number"], m["phase"], m.get("error"))
                                 for m in metas]

    # (a) the engine went on to run further iterations after it
    later = [m for m in metas if m["number"] > cancelled[0]["number"]]
    assert [m["phase"] for m in later], [m["number"] for m in metas]
    assert all(m["endedAt"] for m in later)

    # (b) the run's terminal state was decided by the work, not by the
    #     cancellation
    assert state == "succeeded"
    status = sup.run.read_status()
    assert status["state"] == "succeeded" and status["verdict"] == "verified"
    assert not status.get("reason")
    assert "CancelledError" not in json.dumps(status)


def test_a_cancelled_iteration_is_not_scored_as_engine_death(
        cancelled_worker_run):
    sup, _ = cancelled_worker_run
    events = [json.loads(x) for x in
              (sup.run.root / "events.jsonl").read_text().splitlines()]
    # _run_job_core's own `except Exception` guard never fired...
    assert not [ev for ev in events
                if ev.get("type") == "log"
                and "engine error" in (ev.get("message") or "")], events
    # ...and the cancellation was reported as one iteration's error.
    assert [ev for ev in events if ev.get("type") == "log"
            and "CancelledError" in (ev.get("message") or "")]
