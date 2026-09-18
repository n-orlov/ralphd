"""Task 007 (requirement B/DoD 4, #114): a REFUTED mid-run adjudication
leaves the affected task's criteria untouched and its validation ladder
running -- the deliberate mirror of task 006's CONFIRMED-amendment path,
proved the same way: driving the REAL production path
(`LoopSupervisor._step_worker` -> `_maybe_run_midrun_adjudication_episode`
(task 005 PART 2) -> `_review_verdict` -> `_apply_refuted_petition_
adjudications`/`_apply_criteria_amendments`) via a phase-dispatching
scripted agent, the same shape `tests/test_midrun_adjudication_episode.py`
and `tests/test_criteria_amendment.py` both use. No test-local
`_extra_validate`-style shim, no monkeypatch of the behaviour under test.

Production call sites (file and symbol), `rg`-findable outside `tests/` at
the shipped sha:

- `LoopSupervisor._apply_refuted_petition_adjudications`
  (`src/ralphd/engine/loop.py`) -- called from `_review_verdict`
  (`src/ralphd/engine/loop.py:5217`), the SAME seam both an ordinary
  run-ending review pass and task 005's bounded mid-run adjudication
  episode settle through;
- `engine.petitions.record_refuted_adjudications` (task 007, new) --
  called from `_apply_refuted_petition_adjudications` unconditionally,
  emitting one `petitions.PETITION_REFUTED_EVENT` line per REFUTED entry
  so the remedy is witnessed in `events.jsonl` (the run's own record)
  even in the common case that method's own tasks.json mutation never
  touches: a REFUTED entry naming an obligation with no matching
  `petitioned` task at all -- exactly the shape a worker's own mid-run
  impossibility declaration (`petitions.file_worker_petition`, task 005
  PART 1) leaves behind, since that write never moves the task off its
  ordinary status;
- `engine.petitions.criteria_amendment_candidates` (`src/ralphd/engine/
  petitions.py:1228`) -- gates on `item['adjudication'] == 'CONFIRMED'`
  (its own point 1); a REFUTED entry is never eligible, so
  `LoopSupervisor._apply_criteria_amendments` finds no candidate and
  writes nothing.

#209 defect 4 grew the enumeration below from five items to six rather than
adding a module of its own, because this module's own REFUTED entry (it
carries no `remedy_type`, so it is exactly the `substance` default) is that
defect's subject: such a remedy also BECOMES one cure task, through
`LoopSupervisor._insert_refuted_remedy_tasks` -> `state.insert_cure_tasks`,
on the same path review's own cure pass uses. The first five are unchanged
and still pass unchanged: the remedy task appends at the end of this
one-task plan (`last_code_touching_task_id` finds no named freeze task), so
every node reading `tasks[0]` still reads the same task record.

Six things pinned:

1. `successCriteria`/`criteriaFingerprint` byte-identical before/after;
2. no `task.criteria_amended` event anywhere in `events.jsonl`;
3. `validationAttempts` not reset (untouched from its pre-adjudication
   value);
4. the refutation's remedy text ("what would satisfy it") recorded on the
   run's own record -- a `petition_refuted` event in `events.jsonl`
   naming the obligation and the exact remedy text;
5. the task continues its ordinary validation ladder -- the SAME
   `_step_worker` call's verify pass, immediately after the episode,
   still reaches and verifies it (`validated`), never blocked or
   diverted by the REFUTED adjudication;
6. the remedy becomes WORK -- one extra `pending` task, `origin:
   "review-cure"`, stamped `remedyFor` with the obligation, witnessed by
   exactly one `petitions.PETITION_REMEDY_TASK_EVENT` line beside the
   pre-existing single `petition_refuted` line, and charged against neither
   cure-pass counter; and the `mid_run` guard that keeps an approach-end
   review from inserting one it could not act on.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from ralphd.engine import petitions
from ralphd.engine.config import JobConfig
from ralphd.engine.loop import LoopSupervisor
from ralphd.engine.runner import IterationResult
from ralphd.engine.state import RunDir, atomic_write_json

CRITERIA = (
    "The task's own success criteria this test invents for coverage; "
    "REFUTED must never rewrite it.")
# Names task `1` of `_task_record` below, the way a real filing names
# the task it was filed for: task 018 (requirement E.1, #150) treats a
# petition whose obligation names no task in the current tasks.json
# and no requirement of the contract as history, and history is never
# surfaced to review at all.
OBLIGATION = "Task 1: an obligation this test invents for coverage"
REMEDY = ("Run the missing migration script before the next validation "
          "attempt; the obligation is achievable once it has run.")


def _ok(text: str) -> IterationResult:
    r = IterationResult(exit_code=0)
    r.final_text = text
    r.duration_s = 1.0
    r.usage = {"input": 1, "output": 1, "totalTokens": 2}
    return r


def _make_petitions_file(workspace: Path, obligation: str,
                         wave: str = "v0.16") -> Path:
    reports = workspace / "artifacts" / "reports"
    reports.mkdir(parents=True)
    path = reports / f"{wave}-petitions.md"
    path.write_text(
        f"# {wave} petitions\n\n"
        f"## Petition: {obligation}\n\n"
        f"**Obligation**: {obligation}\n"
        "**Type**: self-referential\n"
        "**Evidence**: the criterion names its own not-yet-existing sha\n")
    return path


class _ScriptedAgent:
    """Phase-dispatching scripted agent (same shape
    `tests/test_midrun_adjudication_episode.py`'s own `_ScriptedAgent`
    uses): one script per phase, keyed off the CURRENT `status.json`
    phase, last step of each repeating forever."""

    def __init__(self, handlers: dict):
        self.handlers = handlers
        self.calls: dict[str, int] = {}
        self.running = False

    def interrupt(self) -> bool:
        return self.running

    async def run(self, prompt, transcript, **kw) -> IterationResult:
        phase = self.sup.run.read_status().get("phase")
        n = self.calls.get(phase, 0)
        self.calls[phase] = n + 1
        steps = self.handlers[phase]
        self.running = True
        try:
            return steps[min(n, len(steps) - 1)](self.sup, prompt)
        finally:
            self.running = False


def _task_record(**overrides) -> dict:
    base = {
        "id": "1", "title": "a task whose worker filed a mid-run petition",
        "status": "awaiting-validation", "successCriteria": CRITERIA,
        "validationAttempts": 2, "dependsOn": [], "tier": "hard",
    }
    base.update(overrides)
    return base


def _worker_supervisor(tmp_path: Path, task: dict) -> tuple[LoopSupervisor, RunDir, Path]:
    root = tmp_path / "run"
    root.mkdir(parents=True)
    run = RunDir(root=root)
    run.update_status(state="running")
    atomic_write_json(run.tasks_file, {"version": 1, "tasks": [task]})
    cfg = JobConfig(
        run_id="task007-midrun-refuted", iterations=10, max_approaches=3,
        vigilant=True, on_complete="exit", infra_retry_backoff_s=[0.0],
        infra_retry_backoff_max_s=0.0, infra_outage_budget_s=1000.0,
        model_overrides={"worker": "prov/worker-model-x",
                         "review": "prov/review-model-y"})
    sup = LoopSupervisor(cfg, run, root)
    sup._approach = 1
    return sup, run, root


def _refuting_review(sup, prompt=""):
    return _ok("adjudicating the open petition\n" + json.dumps({
        "verdict": "rejected",
        "petition_adjudications": [{
            "obligation": OBLIGATION,
            "adjudication": "REFUTED",
            "remedy": REMEDY,
        }],
    }))


def _verified(sup, prompt=""):
    return _ok(json.dumps({"task_id": "1", "verdict": "verified"}))


def _run_refuted_episode(
        tmp_path: Path, task: dict) -> tuple[LoopSupervisor, RunDir]:
    sup, run, workspace = _worker_supervisor(tmp_path, task)
    _make_petitions_file(workspace, OBLIGATION)

    agent = _ScriptedAgent({
        "worker": [lambda s, p: _ok(json.dumps({"verdict": "no_verdict"}))],
        "review": [_refuting_review],
        "verify": [_verified],
    })
    agent.sup = sup  # type: ignore[attr-defined]
    sup.runner = agent  # type: ignore[assignment]

    asyncio.run(sup._step_worker())
    return sup, run


def _events(run: RunDir) -> list[dict]:
    return [json.loads(x) for x in (run.root / "events.jsonl").read_text().splitlines()
           if x.strip()]


def test_success_criteria_and_fingerprint_byte_identical_after_refuted(tmp_path):
    old_fp = LoopSupervisor._criteria_fingerprint(CRITERIA)
    task = _task_record(criteriaFingerprint=old_fp)
    sup, run = _run_refuted_episode(tmp_path, task)

    on_disk = run.read_tasks()["tasks"][0]
    assert on_disk["successCriteria"] == CRITERIA
    assert on_disk["criteriaFingerprint"] == old_fp


def test_no_criteria_amended_event_is_ever_emitted(tmp_path):
    task = _task_record()
    sup, run = _run_refuted_episode(tmp_path, task)

    assert not any(e.get("type") == "task.criteria_amended"
                   for e in _events(run))


def test_validation_attempt_counter_is_not_reset(tmp_path):
    task = _task_record(validationAttempts=2)
    sup, run = _run_refuted_episode(tmp_path, task)

    on_disk = run.read_tasks()["tasks"][0]
    # The verify pass that follows in the same _step_worker call verified
    # on the first try, so the counter's own ordinary ladder machinery
    # never had reason to touch it either -- the REFUTED adjudication
    # itself certainly did not reset it to 0.
    assert on_disk["validationAttempts"] == 2


def test_remedy_text_is_recorded_on_the_runs_own_record(tmp_path):
    task = _task_record()
    sup, run = _run_refuted_episode(tmp_path, task)

    refuted = [e for e in _events(run) if e.get("type") == "petition_refuted"]
    assert len(refuted) == 1
    ev = refuted[0]
    assert ev.get("obligation") == OBLIGATION
    assert ev.get("remedy") == REMEDY


def test_task_continues_its_validation_ladder_to_validated(tmp_path):
    task = _task_record()
    sup, run = _run_refuted_episode(tmp_path, task)

    on_disk = run.read_tasks()["tasks"][0]
    # The REFUTED adjudication neither blocked nor diverted the ordinary
    # ladder: the SAME _step_worker call's verify pass, immediately after
    # the episode, still reached and verified this task.
    assert on_disk["status"] == "validated"
    assert on_disk["id"] == "1"
    assert on_disk["title"] == "a task whose worker filed a mid-run petition"


def test_other_task_fields_are_otherwise_untouched(tmp_path):
    fp = LoopSupervisor._criteria_fingerprint(CRITERIA)
    task = _task_record(dependsOn=["0"], tier="hard", validationAttempts=1,
                        criteriaFingerprint=fp)
    before = dict(task)
    sup, run = _run_refuted_episode(tmp_path, task)

    on_disk = run.read_tasks()["tasks"][0]
    # Only `status` (the ordinary verify pass's own legitimate move) may
    # differ from the pre-episode record.
    untouched = {"successCriteria", "criteriaFingerprint",
                "validationAttempts", "id", "title", "dependsOn", "tier"}
    for key in untouched:
        assert on_disk[key] == before[key], key
    assert on_disk["status"] == "validated"


def test_the_substance_remedy_becomes_a_pending_cure_task_through_the_real_engine(
        tmp_path):
    """#209 defect 4, through the SAME production path this module already
    drives (`_step_worker` -> `_maybe_run_midrun_adjudication_episode` ->
    `_review_verdict(mid_run=True)` -> `_insert_refuted_remedy_tasks`), with
    no monkeypatch of the behaviour under test. Before this the remedy
    reached the plan nowhere at all -- it was witnessed on `events.jsonl` and
    (for a `petitioned` task, which this fixture's task is not) written into
    `validationNotes` on a task whose only legal successor is `skipped`.
    """
    sup, run = _run_refuted_episode(tmp_path, _task_record())

    tasks = run.read_tasks()["tasks"]
    remedy = [t for t in tasks if t.get("origin") == "review-cure"]
    assert len(remedy) == 1
    assert remedy[0]["remedyFor"] == OBLIGATION
    assert remedy[0]["title"] == OBLIGATION
    assert REMEDY in remedy[0]["description"]

    # it entered the plan `pending` and the SAME `_step_worker` call's
    # scheduler then selected it -- i.e. this is actionable work the loop
    # picks up, not a row parked where nothing can reach it.
    moves = [(e.get("oldStatus"), e.get("newStatus")) for e in _events(run)
             if e.get("type") == "task" and e.get("taskId") == remedy[0]["id"]]
    assert moves[0] == (None, "pending")
    assert ("pending", "in-progress") in moves

    events = [e for e in _events(run)
              if e.get("type") == petitions.PETITION_REMEDY_TASK_EVENT]
    assert len(events) == 1
    assert events[0]["obligation"] == OBLIGATION
    assert events[0]["taskId"] == remedy[0]["id"]
    # the witness line the remedy always had is still emitted exactly once
    # beside it -- this path adds a channel, it replaces none.
    assert len([e for e in _events(run)
                if e.get("type") == "petition_refuted"]) == 1

    # and it spends neither cure-pass counter, so it can never starve
    # `JobConfig.cure_pass_ceiling` out from under review's or the peer
    # verdict gate's own cure pass.
    status = run.read_status()
    assert not status.get("curePasses")
    assert not status.get("gateCurePasses")
    assert sup._cure_passes_for_run() == 0


def test_an_approach_end_review_inserts_no_remedy_task(tmp_path):
    """The `mid_run` guard, and therefore the absence of a silent-loss
    regression of #209's own shape. The guard is about ORDER: `_review_
    verdict` runs before `_step_review` picks a branch, so at insert time
    the outcome is unknown, and it can be an accept that `_finish
    ("succeeded", ...)` with the task still `pending` (`_terminal_success_
    blockers` names only unsettled steering files and non-terminal
    STEERING-origin tasks, so it would not notice), an archive-and-replan
    that discards it, a curable rejection with a cure pass left -- which
    goes back into the worker over the SAME approach and WOULD work it --
    or one of the pause-and-escalate branches. Only the mid-run episode is
    known-safe in advance. If a later change lifts the guard, this is the
    node where widening `_terminal_success_blockers` gets forced.
    """
    sup, run, workspace = _worker_supervisor(tmp_path, _task_record())
    _make_petitions_file(workspace, OBLIGATION)
    before = run.read_tasks()["tasks"]

    sup._review_verdict(_refuting_review(sup), mid_run=False)

    after = run.read_tasks()["tasks"]
    assert len(after) == len(before)
    assert not [t for t in after if t.get("origin") == "review-cure"]
    assert not [e for e in _events(run)
                if e.get("type") == petitions.PETITION_REMEDY_TASK_EVENT]
    # the REFUTED witness line still fires on this path, unchanged.
    assert len([e for e in _events(run)
                if e.get("type") == "petition_refuted"]) == 1
