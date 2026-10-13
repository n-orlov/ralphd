"""Task 005 PART 2 (requirement A.2, #114): on a filed impossibility
declaration, the engine schedules ONE bounded mid-run adjudication
iteration -- on the review-hat's own model, never the worker's -- scoped
to every open petition, running strictly BEFORE the affected task's next
validation attempt. `LoopSupervisor._maybe_run_midrun_adjudication_episode`
(`src/ralphd/engine/loop.py`), called from `_step_worker` right before the
`pending_verify` loop that calls `_verify_task`.

Three things pinned here, driving the real `LoopSupervisor._step_worker`
path with a phase-dispatching scripted agent (never a test-local shim):

1. ordering -- the adjudication episode's own "review" phase call happens
   strictly BEFORE the "verify" phase call for the task that was
   `awaiting-validation`;
2. the review-hat MODEL is used for the episode (`self.cfg.model_for
   ("review")`), never the worker's model -- proved with two DIFFERENT
   configured model refs;
3. a CONFIRMED mid-run adjudication removes its obligation from the
   blocking set at RUN SCOPE (`petitions.confirmed_obligations`), the
   same persistence an ordinary run-ending review's CONFIRMED call gets
   (`petitions.record_confirmed_adjudications`, survives
   `_archive_approach`).
"""

from __future__ import annotations

import asyncio
import json
import time as real_time
from pathlib import Path

import pytest

from ralphd.engine import petitions
from ralphd.engine.config import JobConfig
from ralphd.engine.loop import LoopSupervisor
from ralphd.engine.runner import IterationResult
from ralphd.engine.state import RunDir, atomic_write_json

OBLIGATION = ("Task 1: an obligation this test invents for coverage"
              # Task 018 (requirement E.1, #150): a petition whose
              # obligation names NO task in the current tasks.json
              # and no requirement of the contract is history and is
              # never surfaced to review, so this fixture names the
              # plan's own task the way a real engine-filed
              # exhaustion petition does
              " that the plan below still carries")


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
    """Phase-dispatching scripted agent, the same shape
    `tests/test_unsatisfiable_criteria.py`'s own `_ScriptedAgent` uses:
    one script per phase (keyed off the CURRENT `status.json` phase, which
    `_run_iteration_once` sets before ever calling `self.runner.run`), the
    last step of each repeating forever. Also records call ORDER (across
    every phase) and the `model` kwarg each call was actually made with."""

    def __init__(self, handlers: dict):
        self.handlers = handlers
        self.calls: dict[str, int] = {}
        self.model_calls: dict[str, list] = {}
        self.order: list[str] = []
        self.running = False

    def interrupt(self) -> bool:
        return self.running

    async def run(self, prompt, transcript, **kw) -> IterationResult:
        phase = self.sup.run.read_status().get("phase")
        n = self.calls.get(phase, 0)
        self.calls[phase] = n + 1
        self.model_calls.setdefault(phase, []).append(kw.get("model"))
        self.order.append(phase)
        steps = self.handlers[phase]
        self.running = True
        try:
            return steps[min(n, len(steps) - 1)](self.sup, prompt)
        finally:
            self.running = False


def _worker_supervisor(tmp_path: Path, **cfg_kw) -> tuple[LoopSupervisor, RunDir, Path]:
    root = tmp_path / "run"
    root.mkdir(parents=True)
    run = RunDir(root=root)
    run.update_status(state="running")
    atomic_write_json(run.tasks_file, {"version": 1, "tasks": [
        {"id": "1", "title": "already completed by a prior worker pass",
         "status": "awaiting-validation", "successCriteria": "n/a"},
    ]})
    kw = {"iterations": 10, "max_approaches": 3, "vigilant": True,
          "on_complete": "exit", "infra_retry_backoff_s": [0.0],
          "infra_retry_backoff_max_s": 0.0, "infra_outage_budget_s": 1000.0,
          "model_overrides": {"worker": "prov/worker-model-x",
                              "review": "prov/review-model-y"},
          **cfg_kw}
    cfg = JobConfig(run_id="task005-part2-episode", **kw)
    sup = LoopSupervisor(cfg, run, root)
    sup._approach = 1
    return sup, run, root


def _confirming_review(sup, prompt=""):
    return _ok("adjudicating the open petition\n" + json.dumps({
        "verdict": "rejected",
        "petition_adjudications": [
            {"obligation": OBLIGATION, "adjudication": "CONFIRMED"}],
    }))


def _verified(sup, prompt=""):
    return _ok(json.dumps({"task_id": "1", "verdict": "verified"}))


def test_episode_runs_before_verify_on_the_review_hat_model_and_confirms_at_run_scope(
        tmp_path):
    sup, run, workspace = _worker_supervisor(tmp_path)
    _make_petitions_file(workspace, OBLIGATION)

    agent = _ScriptedAgent({
        "worker": [lambda s, p: _ok(json.dumps({"verdict": "no_verdict"}))],
        "review": [_confirming_review],
        "verify": [_verified],
    })
    agent.sup = sup  # type: ignore[attr-defined]
    sup.runner = agent  # type: ignore[assignment]

    asyncio.run(sup._step_worker())

    # 1. ordering: the episode's own "review" call happened strictly
    # before the "verify" call for the task that was awaiting-validation.
    assert "review" in agent.order and "verify" in agent.order
    assert agent.order.index("review") < agent.order.index("verify")
    assert agent.calls.get("review") == 1
    assert agent.calls.get("verify") == 1

    # 2. the review-hat's OWN model was used for the episode, never the
    # worker's -- proved against two genuinely different configured refs.
    assert agent.model_calls["review"][0] == sup.cfg.model_for("review")
    assert agent.model_calls["review"][0] != sup.cfg.model_for("worker")
    assert sup.cfg.model_for("review") == "prov/review-model-y"

    # 3. the CONFIRMED adjudication is persisted at RUN SCOPE, removing
    # the obligation from the blocking set for the rest of the run.
    confirmed, tampered = petitions.confirmed_obligations(sup.run)
    assert not tampered
    assert any(OBLIGATION.lower() in c or c in OBLIGATION.lower()
              for c in confirmed)

    # The task was genuinely verified afterwards -- the episode did not
    # replace or block the ordinary verify step.
    task = sup.run.read_tasks()["tasks"][0]
    assert task["status"] == "validated"

    # The episode never went through `_step_worker`'s own dispatch table
    # as a terminal review verdict -- no VERIFIED signal was emitted for
    # this bounded, off-budget call.
    events = [json.loads(x) for x in (run.root / "events.jsonl").read_text().splitlines()
             if x.strip()]
    assert not any(e.get("type") == "signal" and e.get("signal") == "VERIFIED"
                  for e in events)

    # Off-budget: the episode never competed with the run's ordinary
    # iteration budget (iterations=2 covered exactly worker + verify).
    # Task 015 (requirement E.1, #169) renamed this refund bucket from the
    # retired off-budget grace review's "grace" to "adjudication" --
    # same off-budget mechanism, new name.
    assert sup.run.read_status().get("iterationsRefunded", {}).get("adjudication", 0) >= 1


@pytest.mark.parametrize("boundary", ["deadline", "iterations"])
def test_no_adjudication_episode_starts_at_an_already_expired_boundary(
        tmp_path, boundary):
    """Task cure-01-01-2 (v0.24 requirement A.1/B.3, DoD 1-3, #169): this
    episode is a bare async call from inside `_step_worker`'s own `while
    self.budget_left():` loop BODY, so the loop's own boundary check (only
    re-consulted at the top of the next turn) does not stand between a
    worker iteration that just spent the last of the budget/deadline and
    this call -- exactly the `review_edges.py::test_petition_
    adjudication_does_not_start_after_deadline` reproduction this task
    cures, now driven through the SAME real `_step_worker` path and
    `_worker_supervisor` fixture the ordinary in-budget test above uses
    (canonical run-side open petition, task already `awaiting-
    validation`, so `_maybe_run_midrun_adjudication_episode` is reached
    from the `pending_verify` branch, strictly after the worker
    iteration that expires the boundary INSIDE itself).

    An already-expired boundary must start NO new adjudication review
    iteration; the petition stays open (unsettled, eligible again after
    an operator resume -- `_petition_adjudication_seen` is never touched
    by the early return); and the pass still lands in the shared
    `exhausted`/null-verdict freeze path (task 010/011's C.1 path), never
    a second freeze implementation of its own.
    """
    sup, run, workspace = _worker_supervisor(tmp_path)
    _make_petitions_file(workspace, OBLIGATION)
    assert petitions.open_unconfirmed_petitions(
        workspace, run, tasks=run.read_tasks()["tasks"])
    if boundary == "iterations":
        sup.cfg.iterations = 1

    def _worker_expires_the_boundary(s, prompt=""):
        if boundary == "deadline":
            s.deadline = real_time.monotonic() - 1.0
            s._deadline_epoch = real_time.time() - 1.0
        return _ok(json.dumps({"verdict": "no_verdict"}))

    agent = _ScriptedAgent({
        "worker": [_worker_expires_the_boundary],
        "review": [_confirming_review],
        "verify": [_verified],
    })
    agent.sup = sup  # type: ignore[attr-defined]
    sup.runner = agent  # type: ignore[assignment]

    assert sup.budget_left() is True, (
        "precondition: the boundary is still open at `_step_worker`'s own "
        "loop top, so the worker iteration below really is dispatched")

    outcome = asyncio.run(sup._step_worker())

    # 1. no new adjudication review iteration was dispatched after the
    # boundary expired mid-worker.
    assert sup.budget_left() is False
    assert agent.calls.get("review", 0) == 0, (
        "requirement A.1: an already-expired boundary must start NO new "
        "adjudication review iteration")
    assert agent.order == ["worker"], (
        f"only the worker iteration that spent the budget ran, got "
        f"{agent.order!r}")
    starts = [json.loads(x) for x in
             (run.root / "events.jsonl").read_text().splitlines() if x.strip()
             and json.loads(x).get("type") == "iteration.start"]
    assert [e["phase"] for e in starts] == ["worker"]

    # 2. the petition is left unsettled -- never adjudicated by an
    # episode that never ran -- and stays eligible again after an
    # operator resume.
    assert sup._petition_adjudication_seen == set()
    confirmed, tampered = petitions.confirmed_obligations(sup.run)
    assert not tampered
    assert not confirmed
    open_after = petitions.open_unconfirmed_petitions(
        workspace, run, tasks=run.read_tasks()["tasks"])
    assert open_after

    # 3. the pass ends in the SHARED freeze path -- `exhausted`, null
    # verdict, no second freeze mechanism of its own.
    assert outcome == ("tail",)
    assert sup._budget_exhausted_tail is True
    assert sup._finish_tail_unverified() == "exhausted"
    status = run.read_status()
    assert status["state"] == "exhausted"
    assert status["verdict"] is None
    assert status.get("endedAt") is None, "a held run has not ended"


def test_no_open_petition_is_a_no_op_episode(tmp_path):
    """The overwhelming common case: no petition filed at all -- the
    episode never runs, "review" is never called, and verify proceeds
    exactly as it always did."""
    sup, run, workspace = _worker_supervisor(tmp_path)
    # no petitions file at all under this workspace

    agent = _ScriptedAgent({
        "worker": [lambda s, p: _ok(json.dumps({"verdict": "no_verdict"}))],
        "review": [_confirming_review],
        "verify": [_verified],
    })
    agent.sup = sup  # type: ignore[attr-defined]
    sup.runner = agent  # type: ignore[assignment]

    asyncio.run(sup._step_worker())

    assert agent.calls.get("review", 0) == 0
    assert agent.calls.get("verify") == 1
    task = sup.run.read_tasks()["tasks"][0]
    assert task["status"] == "validated"
