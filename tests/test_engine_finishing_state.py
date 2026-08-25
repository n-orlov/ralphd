"""Task 012 (#56): a non-terminal `finishing` state covers the reflect unwind.

Until this task, `_run_job_core()` wrote the real terminal `state`
(`succeeded`/`failed`/`aborted`) plus `endedAt` the instant the outcome was
decided -- *before* the post-terminal `reflect` iteration (PRD req 24) ever
ran. Every surface that gates a destructive action on `state`/
`TERMINAL_STATES` (`rm`, the hub's delete button, a follower's terminal-event
check) therefore treated a run that was still doing agent work as finished
and safe to act on.

With `reflect: true`, the outcome is now recorded as the non-terminal
`finishing` state (a member of `NONTERMINAL_STATES`) carrying the settled
`verdict` and the `pendingTerminalState` it is going to become; `run_job()`
promotes it to the real terminal state once the reflect iteration returns
(whatever it returned). With `reflect: false` nothing changes: `_finish()`
writes the terminal state directly, exactly as before this task.

A process that resumes a run dir already recorded `finishing` (the engine
died mid-reflect) promotes straight from the recorded fields without
re-entering the job loop at all -- see
test_a_resumed_process_promotes_a_crashed_finishing_window_without_any_work.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from test_cli_docker import Ctl, ctl, unix_sock
from test_cli_resume import _seed_run

from ralphd.engine.config import JobConfig
from ralphd.engine.loop import LoopSupervisor
from ralphd.engine.runner import COMPLETE, VERIFIED, IterationResult
from ralphd.engine.state import (
    NONTERMINAL_STATES,
    TERMINAL_STATES,
    RunDir,
    atomic_write_json,
)

__all__ = ["ctl", "unix_sock"]


# -- scaffolding, mirroring tests/test_episode_scoped_ending.py ------------


class _ScriptedRunner:
    """Stands in for PiRunner. Each script entry takes the supervisor and
    returns an IterationResult; the last entry repeats forever."""

    def __init__(self, sup: LoopSupervisor, script: list):
        self.sup = sup
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
            return step(self.sup)
        finally:
            self.running = False


def _ok(text: str = "working") -> IterationResult:
    r = IterationResult(exit_code=0)
    r.final_text = text
    r.duration_s = 30.0          # not an "instant" failure
    r.usage = {"input": 10, "output": 5, "totalTokens": 15}
    return r


def _plan(sup: LoopSupervisor) -> IterationResult:
    atomic_write_json(sup.run.root / "plan-proposal.json", {"version": 1, "tasks": [
        {"id": "001", "title": "t", "successCriteria": "c"}]})
    return _ok("planned\n" +
              json.dumps({"proposal_path": "plan-proposal.json", "summary": "planned"}))


def _finish_task(sup: LoopSupervisor) -> IterationResult:
    doc = sup.run.read_tasks()
    doc["tasks"][0]["status"] = "completed"
    atomic_write_json(sup.run.tasks_file, doc)
    return _ok(f"done\n{COMPLETE}")


def _review_ok(sup: LoopSupervisor) -> IterationResult:
    # Task 119 (#60 pillar 3, fact 9): review reports through the `review`
    # JSON contract, not the retired `<promise>VERIFIED</promise>` sentinel.
    return _ok("looks good\n" + json.dumps({"verdict": "verified"}))


def _no_progress(sup: LoopSupervisor) -> IterationResult:
    """An ordinary worker attempt that changes nothing and does not signal
    COMPLETE -- three of these in a row fail the approach (stagnation)."""
    return _ok("still working")


def _abort(sup: LoopSupervisor) -> IterationResult:
    sup.abort("operator asked to stop")
    return _ok("interrupted")


def _write_reflect_report(sup: LoopSupervisor) -> IterationResult:
    outdir = sup.run.artifacts_dir / "reflection"
    outdir.mkdir(parents=True, exist_ok=True)
    (outdir / "report.md").write_text("# stub report\n")
    return _ok("reflected")


def _supervisor(root: Path, script: list, **cfg_kw) -> LoopSupervisor:
    root.mkdir(parents=True, exist_ok=True)
    run = RunDir(root=root)
    kw = {"iterations": 12, "max_approaches": 1, "vigilant": False,
          "on_complete": "exit", "infra_retry_backoff_s": [0.0],
          "infra_retry_backoff_max_s": 0.0, "infra_outage_budget_s": 1000.0,
          **cfg_kw}
    sup = LoopSupervisor(JobConfig(run_id="v08-finishing", **kw), run, root)
    sup.runner = _ScriptedRunner(sup, script)   # type: ignore[assignment]

    async def no_backoff(seconds):
        return seconds, False

    sup._wait_out_backoff = no_backoff          # type: ignore[method-assign]
    return sup


def _events(root: Path, type_: str) -> list[dict]:
    log = root / "events.jsonl"
    lines = log.read_text().splitlines() if log.exists() else []
    return [ev for ev in (json.loads(x) for x in lines) if ev.get("type") == type_]


# -- succeeded / failed / aborted, all through the finishing window --------

def test_succeeded_outcome_goes_through_finishing_before_promoting(tmp_path):
    """The whole review->VERIFIED->reflect path: status.json reads
    `finishing` (with the settled verdict and pendingTerminalState) while the
    reflect iteration is still running, and only becomes `succeeded` once
    run_job() promotes it after reflect returns."""
    root = tmp_path / "run"
    RunDir(root=root).update_status(state="starting")
    seen_during_reflect: list[dict] = []

    def snapshot_then_reflect(sup: LoopSupervisor) -> IterationResult:
        seen_during_reflect.append(sup.run.read_status())
        return _write_reflect_report(sup)

    sup = _supervisor(root, [_plan, _finish_task, _review_ok,
                            snapshot_then_reflect], reflect=True)

    final_state = asyncio.run(sup.run_job())

    assert final_state == "succeeded"
    assert seen_during_reflect, "reflect never ran"
    mid = seen_during_reflect[0]
    assert mid["state"] == "finishing"
    assert mid["state"] in NONTERMINAL_STATES
    assert mid["verdict"] == "verified"
    assert mid["pendingTerminalState"] == "succeeded"
    assert mid["endedAt"]

    final = sup.run.read_status()
    assert final["state"] == "succeeded"
    assert final["verdict"] == "verified"
    assert final["pendingTerminalState"] is None
    assert final["phase"] is None
    # the settled endedAt survives promotion unchanged
    assert final["endedAt"] == mid["endedAt"]

    # exactly one extra state event ("finishing") beyond the ordinary
    # `running` startup one -- the terminal state event is emitted by
    # main.py (engine/main.py: `run.emit("state", state=final_state)`) once
    # run_job() returns, not by loop.py itself, so calling run_job() directly
    # (no real engine process here) never adds it either.
    state_events = _events(root, "state")
    assert [ev["state"] for ev in state_events] == ["running", "finishing"]


def test_failed_outcome_goes_through_finishing_before_promoting(tmp_path):
    """The stagnation path (3 iterations, no progress, no COMPLETE)."""
    root = tmp_path / "run"
    RunDir(root=root).update_status(state="starting")
    seen_during_reflect: list[dict] = []

    def snapshot_then_reflect(sup: LoopSupervisor) -> IterationResult:
        seen_during_reflect.append(sup.run.read_status())
        return _write_reflect_report(sup)

    # _plan produces a task, but nothing ever completes it or signals
    # COMPLETE -- three stagnant iterations fail the approach.
    sup = _supervisor(root, [_plan, _no_progress, _no_progress, _no_progress,
                            snapshot_then_reflect], reflect=True)

    final_state = asyncio.run(sup.run_job())

    assert final_state == "failed"
    mid = seen_during_reflect[0]
    assert mid["state"] == "finishing"
    assert mid["verdict"] == "unverified"
    assert mid["pendingTerminalState"] == "failed"

    final = sup.run.read_status()
    assert final["state"] == "failed"
    assert final["verdict"] == "unverified"
    assert final["pendingTerminalState"] is None


def test_aborted_outcome_goes_through_finishing_before_promoting(tmp_path):
    root = tmp_path / "run"
    RunDir(root=root).update_status(state="starting")
    seen_during_reflect: list[dict] = []

    def snapshot_then_reflect(sup: LoopSupervisor) -> IterationResult:
        seen_during_reflect.append(sup.run.read_status())
        return _write_reflect_report(sup)

    sup = _supervisor(root, [_plan, _abort, snapshot_then_reflect], reflect=True)

    final_state = asyncio.run(sup.run_job())

    assert final_state == "aborted"
    mid = seen_during_reflect[0]
    assert mid["state"] == "finishing"
    assert mid["pendingTerminalState"] == "aborted"

    final = sup.run.read_status()
    assert final["state"] == "aborted"
    assert final["pendingTerminalState"] is None


# -- reflect: false gets none of this --------------------------------------

def test_reflect_false_never_records_a_finishing_state_or_event(tmp_path):
    """Mutation guard in the other direction: a run with `reflect: false`
    must see NO new state, NO new event, NO new status.json field -- the
    terminal write happens exactly as it always did."""
    root = tmp_path / "run"
    RunDir(root=root).update_status(state="starting")
    sup = _supervisor(root, [_plan, _finish_task, _review_ok], reflect=False)

    final_state = asyncio.run(sup.run_job())

    assert final_state == "succeeded"
    final = sup.run.read_status()
    assert final["state"] == "succeeded"
    assert "pendingTerminalState" not in final
    assert sup._pending_terminal is None

    state_events = _events(root, "state")
    assert [ev["state"] for ev in state_events] == ["running"], (
        "reflect: false must never write a `finishing` state event -- the "
        "terminal state event itself comes from main.py, not loop.py, so a "
        "bare run_job() call (no real engine process here) shows none of it "
        "either way")


# -- a crash mid-window: resume reaches the same terminal outcome ----------

def test_a_resumed_process_promotes_a_crashed_finishing_window_without_any_work(
        tmp_path):
    """Simulates the engine dying mid-reflect: status.json already reads
    `finishing` with the settled fields, but this is a BRAND NEW
    LoopSupervisor that never called `_finish()` itself. `run_job()` must
    promote straight from disk and never touch the job loop -- a runner
    that raises on any call proves nothing was re-attempted."""
    root = tmp_path / "run"
    run = RunDir(root=root)
    run.update_status(
        state="finishing", verdict="verified", phase=None,
        pendingTerminalState="succeeded", endedAt="2026-01-01T00:00:00Z",
        reason=None, graceReview=True)
    # a fully-realistic crashed run dir also has tasks.json etc, but the
    # promotion path must not need any of it.

    def _raises(sup):
        raise AssertionError("the job loop was re-entered after a crash "
                             "inside the finishing window")

    sup = _supervisor(root, [_raises], reflect=True)

    final_state = asyncio.run(sup.run_job())

    assert final_state == "succeeded"
    final = run.read_status()
    assert final["state"] == "succeeded"
    assert final["verdict"] == "verified"
    assert final["pendingTerminalState"] is None
    assert final["graceReview"] is True, "fields recorded before the crash survive"
    assert final["endedAt"] == "2026-01-01T00:00:00Z", (
        "the settled endedAt is not re-derived at promotion time")


def test_a_resumed_process_over_a_corrupted_finishing_record_fails_loudly(
        tmp_path):
    """A `finishing` record with no valid `pendingTerminalState` (hand-edited
    or corrupted) must not crash the engine or loop forever -- it fails
    terminally with a reason naming the problem."""
    root = tmp_path / "run"
    run = RunDir(root=root)
    run.update_status(state="finishing", verdict="verified", phase=None,
                      pendingTerminalState="not-a-real-state")
    sup = _supervisor(
        root, [lambda sup: (_ for _ in ()).throw(
            AssertionError("the job loop was re-entered"))], reflect=True)

    final_state = asyncio.run(sup.run_job())

    assert final_state == "failed"
    final = run.read_status()
    assert final["state"] == "failed"
    assert "pendingTerminalState" in final["reason"]


# -- state vocabulary --------------------------------------------------------

def test_finishing_is_nonterminal_and_terminal_states_are_unchanged():
    assert "finishing" in NONTERMINAL_STATES
    assert set(TERMINAL_STATES) == {"succeeded", "failed", "aborted"}
    assert not (set(NONTERMINAL_STATES) & set(TERMINAL_STATES))


# -- ralphctl repair reaches the identical outcome, no engine started ------

def test_repair_set_state_reaches_the_same_outcome_a_crashed_finishing_run_had(
        ctl: Ctl):
    """The other escape hatch task 012 promises: an operator who does not
    even want to start an engine can read `pendingTerminalState` straight off
    status.json and hand it to `ralphctl repair --set-state`, reaching the
    identical terminal outcome the run was always going to settle on."""
    rdir, _cdir = _seed_run(ctl, "tst-finishing-crash")
    (rdir / "status.json").write_text(json.dumps({
        "state": "finishing", "verdict": "verified",
        "pendingTerminalState": "succeeded"}))

    res = ctl.run("repair", "tst-finishing-crash", "--set-state", "succeeded")
    assert res.returncode == 0, res.stderr

    final = json.loads((rdir / "status.json").read_text())
    assert final["state"] == "succeeded"
    assert final["pendingTerminalState"] == "succeeded"  # repair only sets `state`


def test_repair_reports_finishing_as_dangling_not_as_an_unrecognized_state(
        ctl: Ctl):
    """A `finishing` run whose container is gone is a genuine zombie -- an
    engine that dies mid-reflect leaves the run exactly as dangling as one
    that dies mid-worker, and `_STATUS_STATES` must accept `finishing` as a
    recognized value rather than flagging it as corrupt."""
    rdir, _cdir = _seed_run(ctl, "tst-finishing-clean")
    (rdir / "status.json").write_text(json.dumps({
        "state": "finishing", "verdict": "verified", "schemaVersion": 1,
        "pendingTerminalState": "succeeded"}))
    (rdir / "tasks.json").write_text(json.dumps({"version": 1, "tasks": []}))

    res = ctl.run("repair", "tst-finishing-clean")
    assert "unrecognized state" not in res.stdout
    # dangling (no container recorded at all -- the ordinary crashed-engine
    # shape), reported with the real remedy, not a schema complaint
    assert "no longer exists" in res.stdout
    assert "ralphctl resume tst-finishing-clean" in res.stdout
