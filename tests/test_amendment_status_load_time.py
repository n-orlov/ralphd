"""Task 005 (requirement E, #99): the *load-time wiring* of the single
engine-side task-status applier.

`tests/test_amendment_status_applier.py` pins the applier itself
(`state.apply_amendment_status_rulings`) by calling it directly. That is
half of requirement E; the other half is the PRD's own wording -- a status
ruling on the read-only amendments mount is *"applied by the **engine** at
load time"*. Before this module there was no production caller at all:
`rg "apply_amendment_status_rulings\\(" src` found only the definition, so
an operator's ruling still did nothing to the scheduler, which is the
exact `deck-phase3g` failure requirement E exists to end.

So this module drives the ENGINE, not the function:

- `LoopSupervisor._apply_operator_status_rulings()` reads `loop.CONFIG_DIR`
  (the host's `:ro` mount as seen from inside the container -- never
  `overlay_or_config`, which the run itself can write) and applies through
  the one applier;
- `_run_job_core()` calls it before `_resume_point()`, proven end to end
  over a resumed run dir with a scripted agent (same scaffolding as
  `test_deck_phase3g_regression_task010.py`: `sup.runner` is stubbed and
  `_run_iteration_once` is never called directly);
- a refused ruling is surfaced, not silently dropped;
- a `mode: status` entry never reaches review's criteria context, where it
  would read as a criteria REPLACEment -- i.e. as a claim the criteria were
  met, which requirement E forbids a ruling from being able to look like.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from ralphd.engine import loop as loop_mod
from ralphd.engine.config import JobConfig
from ralphd.engine.loop import LoopSupervisor
from ralphd.engine.runner import IterationResult
from ralphd.engine.state import RunDir, atomic_write_json, write_amendment
from tests.conftest import supervisor_with_runner


# ---------------------------------------------------------------------------
# scaffolding
# ---------------------------------------------------------------------------

def _ok(text: str) -> IterationResult:
    r = IterationResult(exit_code=0)
    r.final_text = text
    r.duration_s = 5.0
    r.usage = {"input": 10, "output": 5, "totalTokens": 15}
    return r


class _ScriptedAgent:
    """One handler per phase, keyed by the engine's own recorded phase; the
    prompt of every call recorded in order. `sup.runner` is replaced with
    this, so no real `pi` process and no `_run_iteration_once` call ever
    happens in this module."""

    def __init__(self, handlers: dict):
        self.handlers = handlers
        self.phases: list[str] = []
        self.prompts: list[str] = []
        self.running = False

    def interrupt(self) -> bool:
        return self.running

    async def run(self, prompt, transcript, **kw) -> IterationResult:
        self.prompts.append(prompt)
        Path(transcript).parent.mkdir(parents=True, exist_ok=True)
        Path(transcript).write_text('{"type": "assistant", "text": "stub"}\n')
        phase = self.sup.run.read_status().get("phase")
        self.phases.append(phase)
        self.running = True
        try:
            return self.handlers[phase](self.sup, prompt)
        finally:
            self.running = False


_supervisor = supervisor_with_runner(
    lambda sup, handlers=None: _ScriptedAgent(handlers or {}),
    run_id="task005-load-time", iterations=4, max_approaches=2, vigilant=False,
    on_complete="exit", infra_retry_backoff_s=[0.0], infra_retry_backoff_max_s=0.0,
    infra_outage_budget_s=1000.0)


def _events(root: Path) -> list[dict]:
    path = root / "events.jsonl"
    if not path.exists():
        return []
    return [json.loads(x) for x in path.read_text().splitlines() if x.strip()]


def _config_dir(monkeypatch, tmp_path: Path) -> Path:
    """Point the engine's `CONFIG_DIR` at a scratch directory standing in
    for the host's `:ro` amendments mount."""
    cdir = tmp_path / "config"
    cdir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(loop_mod, "CONFIG_DIR", cdir)
    return cdir


def _tasks(sup: LoopSupervisor, tasks: list[dict]) -> None:
    atomic_write_json(sup.run.tasks_file, {"version": 1, "tasks": tasks})


def _task(**over) -> dict:
    t = {"id": "001", "title": "do the thing", "status": "validation-failed",
         "successCriteria": "the thing is done", "validationAttempts": 2,
         "tierLadderExhausted": True}
    t.update(over)
    return t


# ---------------------------------------------------------------------------
# 1. the supervisor's own load-time entry point
# ---------------------------------------------------------------------------

def test_supervisor_applies_a_ruling_from_config_dir(tmp_path, monkeypatch):
    sup = _supervisor(tmp_path / "run")
    cdir = _config_dir(monkeypatch, tmp_path)
    _tasks(sup, [_task()])
    write_amendment(cdir, "001", "unmeetable as written; stand it down",
                    status="skipped")

    sup._apply_operator_status_rulings()

    on_disk = sup.run.read_tasks()["tasks"][0]
    assert on_disk["status"] == "skipped"
    # the record the ruling must not be able to rewrite
    assert on_disk["validationAttempts"] == 2
    assert on_disk["tierLadderExhausted"] is True
    assert on_disk["successCriteria"] == "the thing is done"
    applied = [e for e in _events(sup.run.root)
               if e.get("type") == "amendment_status_applied"]
    assert len(applied) == 1, applied
    assert applied[0]["source"] == "operator"
    assert applied[0]["taskId"] == "001"
    assert applied[0]["previous"] == "validation-failed"
    assert applied[0]["status"] == "skipped"


def test_supervisor_reads_config_dir_not_the_writable_overlay(tmp_path, monkeypatch):
    """The channel's whole point: a ruling forged in the container's own
    writable overlay must do nothing. `overlay_or_config` would find it;
    `CONFIG_DIR` cannot."""
    sup = _supervisor(tmp_path / "run")
    _config_dir(monkeypatch, tmp_path)
    _tasks(sup, [_task()])
    forged = tmp_path / "overlay"
    write_amendment(forged, "001", "the run rules for itself", status="skipped")

    sup._apply_operator_status_rulings()

    assert sup.run.read_tasks()["tasks"][0]["status"] == "validation-failed"
    assert [e for e in _events(sup.run.root)
            if e.get("type") == "amendment_status_applied"] == []


def test_a_refused_ruling_is_surfaced_and_writes_nothing(tmp_path, monkeypatch):
    sup = _supervisor(tmp_path / "run")
    cdir = _config_dir(monkeypatch, tmp_path)
    _tasks(sup, [_task(status="pending", validationAttempts=0,
                       tierLadderExhausted=False)])
    fname = write_amendment(cdir, "001", "just call it done",
                            status="validated")

    sup._apply_operator_status_rulings()

    assert sup.run.read_tasks()["tasks"][0]["status"] == "pending"
    logs = [e for e in _events(sup.run.root) if e.get("type") == "log"]
    refusals = [e for e in logs if "REFUSED" in (e.get("message") or "")]
    assert len(refusals) == 1, logs
    msg = refusals[0]["message"]
    assert fname in msg and "illegal transition" in msg
    assert "pending -> validated" in msg


def test_an_already_applied_ruling_is_not_re_reported_every_episode(
        tmp_path, monkeypatch):
    """This method runs on every episode of the run; a ruling already in
    effect must be silent, not one more line per load."""
    sup = _supervisor(tmp_path / "run")
    cdir = _config_dir(monkeypatch, tmp_path)
    _tasks(sup, [_task()])
    write_amendment(cdir, "001", "stand it down", status="skipped")

    sup._apply_operator_status_rulings()
    sup._apply_operator_status_rulings()
    sup._apply_operator_status_rulings()

    events = _events(sup.run.root)
    assert len([e for e in events
                if e.get("type") == "amendment_status_applied"]) == 1
    assert [e for e in events if e.get("type") == "log"] == []


def test_no_amendments_at_all_is_a_silent_no_op(tmp_path, monkeypatch):
    sup = _supervisor(tmp_path / "run")
    _config_dir(monkeypatch, tmp_path)
    _tasks(sup, [_task()])

    sup._apply_operator_status_rulings()

    assert sup.run.read_tasks()["tasks"][0]["status"] == "validation-failed"
    assert _events(sup.run.root) == []


def test_an_unreadable_plan_costs_nothing_more_than_itself(
        tmp_path, monkeypatch):
    """A truncated tasks.json (a crash mid-write, a hand-edit gone wrong)
    must not turn a ruling into a crashed episode: `RunDir.read_tasks`
    already answers `{}` for an unparseable plan, so the ruling matches no
    task and says so, and the damaged file is left exactly as it stands."""
    sup = _supervisor(tmp_path / "run")
    cdir = _config_dir(monkeypatch, tmp_path)
    _tasks(sup, [_task()])
    write_amendment(cdir, "001", "stand it down", status="skipped")
    sup.run.tasks_file.write_text("{not json")

    sup._apply_operator_status_rulings()   # must not raise

    logs = [e for e in _events(sup.run.root) if e.get("type") == "log"]
    assert any("no matching task" in (e.get("message") or "") for e in logs), logs
    assert sup.run.tasks_file.read_text() == "{not json"


# ---------------------------------------------------------------------------
# 2. the engine actually calls it, at load time, before it decides anything
# ---------------------------------------------------------------------------

def _resumed_run_with_ruling(tmp_path, monkeypatch, **amend):
    """A RESUMED run dir (the only shape where a ruling can name an
    existing task at load time) driven through the real `_run_job_core`
    dispatch loop with a scripted agent."""
    root = tmp_path / "run"
    cdir = _config_dir(monkeypatch, tmp_path)
    RunDir(root=root).update_status(state="starting")
    sup = _supervisor(root, {
        "worker": lambda s, p: _ok("nothing actionable remains"),
        "review": lambda s, p: _ok(
            "nothing actionable remains\n" + json.dumps({"verdict": "verified"})),
    })
    _tasks(sup, [_task()])
    sup.iterations_used = 1
    write_amendment(cdir, "001", amend.pop("text", "stand it down"), **amend)
    final_state = asyncio.run(sup._run_job_core())
    return sup, final_state


def test_run_job_core_applies_the_ruling_at_load_time(tmp_path, monkeypatch):
    """The wiring requirement E actually asks for: the ENGINE applies the
    ruling, from the mount, without anybody calling the applier by hand.
    Without the call site in `_run_job_core` the task below stays
    `validation-failed` for the whole episode and the worker is handed it
    again -- exactly the `deck-phase3g` shape."""
    sup, final_state = _resumed_run_with_ruling(
        tmp_path, monkeypatch,
        text="unmeetable as written; stand it down", status="skipped")

    on_disk = sup.run.read_tasks()["tasks"][0]
    assert on_disk["status"] == "skipped", on_disk
    assert on_disk["validationAttempts"] == 2
    assert on_disk["tierLadderExhausted"] is True
    assert on_disk["successCriteria"] == "the thing is done"
    applied = [e for e in _events(sup.run.root)
               if e.get("type") == "amendment_status_applied"]
    assert len(applied) == 1 and applied[0]["source"] == "operator", applied
    assert final_state in ("succeeded", "failed"), final_state


def test_the_worker_is_never_assigned_a_task_the_operator_ruled_skipped(
        tmp_path, monkeypatch):
    """The scheduler's own consequence of the load-time application: every
    worker prompt of the episode reports nothing selectable, instead of
    handing task 001 (`validation-failed`, i.e. actionable) back out."""
    sup, _ = _resumed_run_with_ruling(tmp_path, monkeypatch, status="skipped")

    worker_prompts = [p for phase, p in zip(sup.runner.phases, sup.runner.prompts)
                      if phase == "worker"]
    assert worker_prompts, sup.runner.phases
    for p in worker_prompts:
        assert "No task is currently selectable" in p, p[-800:]
        assert "**id**: 001" not in p


def test_the_applied_event_precedes_the_resume_decision_in_the_log(
        tmp_path, monkeypatch):
    """Ordering, read off the append-only event log: the ruling is applied
    before `_resume_point()` emits its own `resuming existing run-dir
    state` line -- i.e. before anything in this episode has looked at the
    plan."""
    sup, _ = _resumed_run_with_ruling(tmp_path, monkeypatch, status="skipped")

    events = _events(sup.run.root)
    applied_at = next(i for i, e in enumerate(events)
                      if e.get("type") == "amendment_status_applied")
    resume_at = next(i for i, e in enumerate(events)
                     if e.get("type") == "log"
                     and "resuming existing run-dir state" in (e.get("message") or ""))
    assert applied_at < resume_at, events[:6]


# ---------------------------------------------------------------------------
# 3. a status ruling is not a criteria ruling
# ---------------------------------------------------------------------------

def test_a_status_ruling_never_appears_in_reviews_criteria_context(
        tmp_path, monkeypatch):
    sup = _supervisor(tmp_path / "run")
    cdir = _config_dir(monkeypatch, tmp_path)
    write_amendment(cdir, "001", "stand it down", status="skipped")

    context = sup._operator_amendment_review_context()

    assert context == "", context


def test_a_waive_ruling_still_reaches_reviews_criteria_context(
        tmp_path, monkeypatch):
    """Positive control for the test above: the pre-existing kinds are
    unaffected -- only `mode: status` is filtered out."""
    sup = _supervisor(tmp_path / "run")
    cdir = _config_dir(monkeypatch, tmp_path)
    write_amendment(cdir, "001", "good enough, ship it", waive=True)
    write_amendment(cdir, "002", "the bar is now X", waive=False)
    write_amendment(cdir, "003", "stand it down", status="skipped")

    context = sup._operator_amendment_review_context()

    assert "WAIVE" in context and "good enough, ship it" in context
    assert "REPLACE" in context and "the bar is now X" in context
    assert "stand it down" not in context
    assert "task 003" not in context
