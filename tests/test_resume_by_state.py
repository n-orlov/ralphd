"""Task 017: reconstruct FSM position from disk on resume, one test case per
step state that can genuinely be interrupted mid-invocation or mid-decision,
driven through the real `ralphd-engine` binary (`engine_factory`, the same
shared harness `test_e2e.py`/`tests/flows/test_flow_crash_resume_refunds.py`
use) -- plus `ralphctl repair` diagnosing the same crashed run dirs without
ever starting an engine.

`worker` and `verify` already have dedicated real-binary SIGKILL/resume tests
(`tests/flows/test_flow_crash_resume_refunds.py::test_sigkill_mid_worker_iteration_then_resume_completes`,
`tests/flows/test_flow_crash_resume_refunds.py::test_sigkill_at_iteration_boundary_then_resume_completes`,
`tests/flows/test_flow_crash_resume_refunds.py::test_sigkill_after_task_completed_before_verify_finishes_then_resume_verifies`,
formerly `tests/test_crash_consistency.py`/`tests/test_vigilant_crash_resume.py`,
folded by task 021, requirement C.10)
and are not duplicated here. This module covers the remaining states:

* `planning` -- killed before a single task exists on disk (guard 23: resume
  must NOT skip planning just because an iteration was attempted).
* `review` -- killed mid-invocation, before any verdict is recorded (the
  worker/review round trip must still happen for real on resume, not be
  silently treated as already decided). Found and fixed a real defect here:
  a plain resume always re-entered at `worker` (`_step_planning`'s
  skip-planning path unconditionally advances there), which cost one
  throwaway worker iteration -- nothing actionable, every task already
  `completed` -- before ever reaching review again; `LoopSupervisor`
  reconstructs the crashed-mid-review position from disk instead and
  resumes straight into `review` (see `_crashed_mid_review` in
  `engine/loop.py`, ledger guard 23's fourth case).
* `replan` (the approach boundary) -- killed in the window between review's
  rejection (`_archive_approach`, already on disk) and the bump/finish that
  must follow it (no sleep hook exists there; the crash is reproduced by
  fabricating the exact on-disk snapshot that window leaves, then resuming
  it with the real binary -- the same technique
  `tests/test_engine_finishing_state.py`'s crashed-`finishing`-window tests
  use, one level up: fabricate the state a crash leaves, not the crash
  itself). A second real, previously-undiscovered defect was found and
  fixed while writing this test -- see
  `LoopSupervisor._archived_approach_awaiting_replan` and `_resume_point` in
  `engine/loop.py` (ledger guard 25).
* `finishing`/`reflect` -- killed mid-reflect-invocation, after `_finish()`
  already wrote `state: finishing` to disk. Found and fixed a third real
  defect here too: `engine/main.py`'s startup status write unconditionally
  clobbered `state` to `"starting"` on every process start, so
  `_promote_recorded_finishing_state` (task 012, #56) could never actually
  run through the real binary -- only the in-process unit tests that
  construct `LoopSupervisor` directly and skip `amain()` ever exercised it
  (ledger guard 24).
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import time
from pathlib import Path

from test_cli_docker import Ctl, ctl, unix_sock
from test_cli_resume import _seed_run

__all__ = ["ctl", "unix_sock", "engine_factory"]


def _wait_for(predicate, timeout=30, interval=0.02):
    deadline = time.time() + timeout
    while time.time() < deadline:
        result = predicate()
        if result:
            return result
        time.sleep(interval)
    raise TimeoutError("condition never became true")


def _kill(e):
    """SIGKILL this specific engine subprocess PID (never pkill/killall)."""
    pid = e.proc.pid
    os.kill(pid, signal.SIGKILL)
    e.proc.wait(timeout=10)
    assert e.proc.returncode is not None
    assert e.proc.returncode != 0


def _events(run_dir: Path) -> list[dict]:
    p = run_dir / "events.jsonl"
    if not p.exists():
        return []
    return [json.loads(line) for line in p.read_text().splitlines() if line.strip()]


def _iteration_phase(run_dir: Path, phase: str, *, finished: bool):
    itdir = run_dir / "iterations"
    if not itdir.is_dir():
        return None
    for d in sorted(itdir.iterdir()):
        p = d / "meta.json"
        if not p.exists():
            continue
        try:
            meta = json.loads(p.read_text())
        except json.JSONDecodeError:
            continue
        if meta.get("phase") != phase:
            continue
        has_ended = "endedAt" in meta
        if has_ended == finished:
            return meta
    return None


def _repair_over_copy(ctl_: Ctl, run_id: str, source_run_dir: Path):
    """Copies `source_run_dir`'s status.json/tasks.json (the crashed shape)
    into a freshly `_seed_run`-ed registry entry, then runs the real
    `ralphctl repair` against it -- WITHOUT the stub-docker knobs that
    would tell it a container is running, exactly like a real crashed run
    whose container is gone. Since the copied status.json genuinely
    records a non-terminal state with no live container, the CORRECT
    diagnosis is `dangling` (a zombie, task 021/#31) -- not "no issues";
    proving repair reaches that diagnosis, rather than choking on the
    crashed shape (a JSON error, an unhandled state, a crash of its own),
    is what this helper's caller asserts."""
    rdir, _cdir = _seed_run(ctl_, run_id)
    for name in ("status.json", "tasks.json"):
        src = source_run_dir / name
        if src.exists():
            shutil.copy2(src, rdir / name)
    res = ctl_.run("--json", "repair", run_id)
    return res, rdir


def _assert_repair_correctly_diagnosed_the_crash(res):
    """`repair` on a genuinely crashed (non-terminal, container-gone) run
    dir exits 1 (issues found) -- never 0 ("no issues") and never a code
    that means repair itself choked -- and the one issue it reports is the
    dangling/zombie diagnosis with `ralphctl resume` named as a remedy, not
    a report of malformed/corrupted JSON (this run dir is well-formed, just
    abandoned mid-flight)."""
    assert res.returncode == 1, (res.returncode, res.stdout, res.stderr)
    doc = json.loads(res.stdout)
    assert doc["ok"] is False
    assert doc["dangling"] is not None
    assert len(doc["issues"]) == 1
    issue = doc["issues"][0]
    assert "no longer exists" in issue
    assert "ralphctl resume" in issue
    assert "malformed" not in issue


# ---------------------------------------------------------------------------
# planning
# ---------------------------------------------------------------------------

def test_killed_before_planning_ever_produces_tasks_then_resume_replans(
        engine_factory, ctl, tmp_path):
    """Killed while the very first planning invocation is still running --
    no tasks.json has ever existed. Resume must NOT treat this as "planning
    already ran" (guard 23's condition, exactly): it must replan from
    scratch, not skip straight into a worker loop with nothing to work on."""
    hold_file = tmp_path / "hold-planning"
    e1 = engine_factory(job={"on_complete": "idle", "iterations": 15,
                             "max_approaches": 1},
                        # No STUB_HOLD_PHASE needed: planning is necessarily
                        # the very first invocation of a fresh run, so the
                        # unconditional hold (never released by this test)
                        # blocks it deterministically before it can write
                        # anything.
                        stub_env={"STUB_HOLD_FILE": str(hold_file),
                                  "STUB_HOLD_TIMEOUT_S": "60", "STUB_TASKS": "3"})
    e1.wait_api()
    meta = _wait_for(lambda: _iteration_phase(e1.run_dir, "planning",
                                              finished=False), timeout=20)
    assert meta["number"] == 1
    assert not (e1.run_dir / "tasks.json").exists(), \
        "planning must not have written anything yet for this test to prove what it claims"
    _kill(e1)

    repair_res, _rdir = _repair_over_copy(ctl, "tst-planning-crash", e1.run_dir)
    _assert_repair_correctly_diagnosed_the_crash(repair_res)

    e2 = engine_factory(job={"on_complete": "exit", "iterations": 15,
                             "max_approaches": 1},
                        stub_env={"STUB_SLEEP": "0", "STUB_TASKS": "3"})
    assert e2.run_dir == e1.run_dir
    assert e2.proc.wait(timeout=60) == 0
    status = json.loads((e2.run_dir / "status.json").read_text())
    assert status["state"] == "succeeded"
    assert status["verdict"] == "verified"

    tasks = json.loads((e2.run_dir / "tasks.json").read_text())["tasks"]
    assert len(tasks) == 3
    assert all(t["status"] == "completed" for t in tasks), \
        "planning genuinely re-ran and produced a real, workable plan"

    # the crashed attempt is archived, not silently discarded or duplicated
    iters = sorted((e2.run_dir / "iterations").iterdir())
    assert iters[0].name == "0001", "the crashed attempt's number was reused, not skipped"
    assert (iters[0] / "attempts").is_dir(), \
        "the crashed planning attempt's files are archived under attempts/"
    metas = [json.loads((d / "meta.json").read_text()) for d in iters]
    phases = [m["phase"] for m in metas]
    assert phases.count("planning") == 1, "planning ran exactly once for real"
    assert all(m.get("endedAt") for m in metas)


# ---------------------------------------------------------------------------
# review
# ---------------------------------------------------------------------------

def test_killed_mid_review_before_any_verdict_then_resume_reviews_for_real(
        engine_factory, ctl, tmp_path):
    """Killed while the review invocation is hung, before it ever wrote a
    verdict. The completed task must not be redone; review must genuinely
    run again on resume rather than the run silently treating the crashed,
    verdict-less attempt as though a decision had already been made."""
    hold_file = tmp_path / "hold-review"
    e1 = engine_factory(job={"on_complete": "idle", "iterations": 15,
                             "max_approaches": 1},
                        # STUB_HOLD_PHASE restricts the hold to review, so
                        # planning and the worker iteration that completes
                        # the one task run unheld first.
                        stub_env={"STUB_HOLD_FILE": str(hold_file),
                                  "STUB_HOLD_PHASE": "review",
                                  "STUB_HOLD_TIMEOUT_S": "60", "STUB_TASKS": "1"})
    e1.wait_api()
    meta = _wait_for(lambda: _iteration_phase(e1.run_dir, "review",
                                              finished=False), timeout=30)
    review_number = meta["number"]
    tasks_mid = json.loads((e1.run_dir / "tasks.json").read_text())["tasks"]
    assert len(tasks_mid) == 1
    assert tasks_mid[0]["status"] == "completed"
    _kill(e1)

    crashed_meta = json.loads(
        (e1.run_dir / "iterations" / f"{review_number:04d}" /
         "meta.json").read_text())
    assert "endedAt" not in crashed_meta
    events_before = _events(e1.run_dir)
    assert not any(ev.get("type") == "signal" and ev.get("signal") == "VERIFIED"
                   for ev in events_before)

    repair_res, _rdir = _repair_over_copy(ctl, "tst-review-crash", e1.run_dir)
    _assert_repair_correctly_diagnosed_the_crash(repair_res)

    e2 = engine_factory(job={"on_complete": "exit", "iterations": 15,
                             "max_approaches": 1},
                        stub_env={"STUB_SLEEP": "0", "STUB_TASKS": "1"})
    assert e2.run_dir == e1.run_dir
    assert e2.proc.wait(timeout=60) == 0
    status = json.loads((e2.run_dir / "status.json").read_text())
    assert status["state"] == "succeeded"
    assert status["verdict"] == "verified"

    tasks_after = json.loads((e2.run_dir / "tasks.json").read_text())["tasks"]
    assert len(tasks_after) == 1
    assert tasks_after[0]["status"] == "completed"
    assert tasks_after[0]["title"] == tasks_mid[0]["title"], \
        "the already-completed task was not redone/replaced"

    events_after = _events(e2.run_dir)
    assert sum(1 for ev in events_after
              if ev.get("type") == "signal" and ev.get("signal") == "VERIFIED") == 1, \
        "review genuinely reached a real verdict exactly once after resume"

    # the crashed attempt is archived under the SAME reused slot number,
    # not kept as a second top-level entry -- the identical pattern the
    # planning case above proves (`begin_iteration_dir`, task 019/#44); a
    # resumed review does not cost a throwaway worker iteration first (the
    # fix this task made, see `LoopSupervisor._crashed_mid_review`), so the
    # crashed slot is reused directly by the real review attempt.
    iters = sorted((e2.run_dir / "iterations").iterdir())
    assert iters[-1].name == f"{review_number:04d}", \
        "the crashed review attempt's number was reused, not skipped"
    assert (iters[-1] / "attempts").is_dir(), \
        "the crashed review attempt's files are archived under attempts/"
    metas = [json.loads((d / "meta.json").read_text()) for d in iters]
    phases = [m["phase"] for m in metas]
    assert phases.count("review") == 1, "review ran exactly once for real"
    assert not any(m["phase"] == "worker" and m["number"] > 2 for m in metas), \
        "resume did not spend a throwaway worker iteration before reaching review"
    assert all(m.get("endedAt") for m in metas), \
        "no half-written iteration record left behind"


# ---------------------------------------------------------------------------
# replan (the approach boundary)
# ---------------------------------------------------------------------------

def _fabricate_rejected_approach_crash(run_dir: Path, *, rejected_approach: int):
    """The exact on-disk shape a crash leaves in the window between
    `_step_review`'s rejection branch archiving `approaches/<NN>/` and the
    bump to the next approach (or the run's terminal write) that must
    follow it -- see the module docstring. There is no sleep hook inside
    that window (it is synchronous, no I/O in between) so it cannot be hit
    with a live SIGKILL; this fabricates the snapshot instead, the same
    technique `tests/test_engine_finishing_state.py` uses for the
    `finishing` window one level up."""
    run_dir.mkdir(parents=True, exist_ok=True)
    tasks_doc = {"version": 1, "tasks": [
        {"id": "001", "title": "rejected approach's task", "status": "completed",
         "successCriteria": "criteria that did not survive review"}]}
    (run_dir / "tasks.json").write_text(json.dumps(tasks_doc))
    for n, phase in [(1, "planning"), (2, "worker"), (3, "review")]:
        d = run_dir / "iterations" / f"{n:04d}"
        d.mkdir(parents=True)
        (d / "meta.json").write_text(json.dumps({
            "number": n, "phase": phase, "startedAt": "2026-01-01T00:00:00Z",
            "endedAt": "2026-01-01T00:00:01Z"}))
    adir = run_dir / "approaches" / f"{rejected_approach:02d}"
    adir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(run_dir / "tasks.json", adir / "tasks.json")
    (run_dir / "status.json").write_text(json.dumps({
        "state": "running", "approach": rejected_approach, "iterationsUsed": 3,
        "schemaVersion": 1}))


def test_killed_between_review_rejection_and_the_approach_bump_then_resume_replans(
        engine_factory, tmp_path, ctl):
    """The approach boundary crash: `approaches/01/` is already archived
    (review genuinely rejected approach 1) but status.json's `approach`
    field is still `1` because the bump only happens at the top of the
    NEXT episode's planning step. Before the fix this task made (see the
    module docstring), resuming here silently re-entered `worker` over the
    stale, already-rejected tasks.json instead of replanning -- the
    approach boundary itself was the gate that got skipped."""
    run_dir = tmp_path / "run"
    _fabricate_rejected_approach_crash(run_dir, rejected_approach=1)

    repair_res, _rdir = _repair_over_copy(ctl, "tst-replan-crash", run_dir)
    _assert_repair_correctly_diagnosed_the_crash(repair_res)

    e = engine_factory(job={"on_complete": "exit", "max_approaches": 2,
                            "iterations": 20},
                       stub_env={"STUB_TASKS": "2"})
    assert e.run_dir == run_dir
    assert e.proc.wait(timeout=60) == 0

    status = json.loads((e.run_dir / "status.json").read_text())
    assert status["state"] == "succeeded"
    assert status["verdict"] == "verified"
    assert status["approach"] == 2, "the boundary was actually crossed, not stuck at 1"

    tasks = json.loads((e.run_dir / "tasks.json").read_text())["tasks"]
    assert len(tasks) == 2, "approach 2's OWN fresh plan, not the rejected approach's 1 task"
    assert all(t["title"] != "rejected approach's task" for t in tasks), \
        "the rejected approach's stale tasks were not reused as live work"

    events = _events(e.run_dir)
    replan_logs = [ev for ev in events if ev.get("type") == "log"
                   and "already reviewed and rejected" in ev.get("message", "")]
    assert replan_logs, "expected a log event naming the crashed-approach-boundary resume"


def test_last_approach_rejected_and_crashed_before_finishing_still_fails_terminally(
        engine_factory, tmp_path):
    """The other half of the same defect: the rejected approach was the
    LAST one available. Resuming must not grant an extra approach beyond
    `max_approaches` by silently re-planning it -- it must go straight to
    the run's real terminal outcome (`failed`, no approaches left), exactly
    as the crashed episode was always going to settle on."""
    run_dir = tmp_path / "run"
    _fabricate_rejected_approach_crash(run_dir, rejected_approach=1)

    e = engine_factory(job={"on_complete": "exit", "max_approaches": 1,
                            "iterations": 20},
                       stub_env={"STUB_TASKS": "2"})
    assert e.run_dir == run_dir
    assert e.proc.wait(timeout=60) == 1

    status = json.loads((e.run_dir / "status.json").read_text())
    assert status["state"] == "failed"
    assert status["approach"] == 1, "no approach was ever granted beyond the budget"
    tasks = json.loads((e.run_dir / "tasks.json").read_text())["tasks"]
    assert len(tasks) == 1 and tasks[0]["title"] == "rejected approach's task", \
        "no re-planning happened at all -- the rejected approach's plan is untouched"


# ---------------------------------------------------------------------------
# finishing / reflect
# ---------------------------------------------------------------------------

def test_killed_mid_reflect_after_finish_wrote_finishing_then_resume_promotes(
        engine_factory, ctl, tmp_path):
    """Killed while the post-terminal reflect invocation is hung --
    status.json already reads `state: finishing` (`_finish()` settled the
    outcome before `_run_reflection()` was ever called). Before the fix
    this task made (see the module docstring), the real engine binary's
    own startup write clobbered `state` back to `starting` on the resumed
    process, so it silently redid the FULL job loop instead of promoting
    straight to the already-settled outcome -- `_promote_recorded_finishing_state`
    (task 012, #56) was unreachable code in practice."""
    e1 = engine_factory(job={"on_complete": "idle", "iterations": 15,
                             "max_approaches": 1, "reflect": True},
                        # STUB_HOLD_PHASE restricts the hold to reflect, so
                        # planning, worker and review/verify all run unheld
                        # first, up to the settled terminal outcome.
                        stub_env={"STUB_HOLD_FILE": str(tmp_path / "hold-reflect"),
                                  "STUB_HOLD_PHASE": "reflect",
                                  "STUB_HOLD_TIMEOUT_S": "60", "STUB_TASKS": "1"})
    e1.wait_api()
    meta = _wait_for(lambda: _iteration_phase(e1.run_dir, "reflect",
                                              finished=False), timeout=40)
    status_mid = json.loads((e1.run_dir / "status.json").read_text())
    assert status_mid["state"] == "finishing"
    assert status_mid["pendingTerminalState"] == "succeeded"
    n_iterations_before = len(list((e1.run_dir / "iterations").iterdir()))
    _kill(e1)

    repair_res, _rdir = _repair_over_copy(ctl, "tst-reflect-crash", e1.run_dir)
    _assert_repair_correctly_diagnosed_the_crash(repair_res)

    e2 = engine_factory(job={"on_complete": "exit", "iterations": 15,
                             "max_approaches": 1, "reflect": True},
                        stub_env={"STUB_SLEEP": "0", "STUB_TASKS": "1"})
    assert e2.run_dir == e1.run_dir
    assert e2.proc.wait(timeout=60) == 0

    status_after = json.loads((e2.run_dir / "status.json").read_text())
    assert status_after["state"] == "succeeded"
    assert status_after["verdict"] == "verified"
    assert status_after["pendingTerminalState"] is None

    n_iterations_after = len(list((e2.run_dir / "iterations").iterdir()))
    assert n_iterations_after == n_iterations_before, (
        "no iteration was redone or added -- the crashed reflect attempt "
        "was never rerun, per _promote_recorded_finishing_state's own "
        "documented contract")

    events = _events(e2.run_dir)
    promote_logs = [ev for ev in events if ev.get("type") == "log"
                    and "promoting directly to the settled outcome" in
                    ev.get("message", "")]
    assert promote_logs, "expected the resumed-finishing-window promotion log"
