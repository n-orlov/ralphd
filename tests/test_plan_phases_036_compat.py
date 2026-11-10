"""PRD v0.37 requirement K.2: a run directory written by 0.36 loads on 0.37
and continues as a no-phase plan, one test per persisted state a 0.36 engine
leaves behind: mid-worker, `awaiting_input`, `exhausted`.

Each run directory is written by hand in the 0.36 shape (no `phases` in
tasks.json, no `phase` on a task, no `planPhase` in status.json, no
`phase_gate.*` event), then continued by a 0.37 supervisor driven with scripted
hats. Nothing sleeps: the parked question is already past its `expiresAt` or is
answered through the engine's own answer channel.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from ralphd.engine.runner import IterationResult
from ralphd.engine.state import atomic_write_json, read_events

from tests.conftest import make_supervisor

PHASE_HEADINGS = ("## Phase review", "## Earlier phase reviews")


def _ok(text: str = "working") -> IterationResult:
    r = IterationResult(exit_code=0)
    r.final_text = text
    r.duration_s = 30.0
    r.usage = {"input": 10, "output": 5, "totalTokens": 15}
    return r


class _ScriptExhausted(BaseException):
    """A BaseException so the engine's infra retry cannot swallow it."""


class _Hats:
    """Scripted hats keyed by the phase the engine recorded; keeps every
    prompt it was handed."""

    def __init__(self, sup, handlers: dict) -> None:
        self.sup = sup
        self.handlers = handlers
        self.phases: list[str] = []
        self.prompts: list[str] = []

    def interrupt(self) -> bool:
        return False

    async def run(self, prompt, transcript, **kw) -> IterationResult:
        phase = self.sup.run.read_status().get("phase")
        self.phases.append(phase)
        self.prompts.append(prompt)
        queue = self.handlers.get(phase)
        if not queue:
            raise _ScriptExhausted(f"no scripted hat left for phase {phase!r}")
        return queue.pop(0)(self.sup)


def _complete(sup) -> IterationResult:
    tid = next(t["id"] for t in sup.run.read_tasks()["tasks"]
               if t["status"] in ("pending", "in-progress"))
    return _ok("done\n" + json.dumps({"verdict": "complete", "task_id": tid}))


def _verified(sup) -> IterationResult:
    return _ok("fine\n" + json.dumps({"verdict": "verified"}))


def _tasks_036(second_status: str) -> dict:
    """A 0.36 task list: no `phases`, no `phase` on any task."""
    return {"version": 1, "tasks": [
        {"id": "001", "title": "first", "status": "completed",
         "successCriteria": "the first thing is done"},
        {"id": "002", "title": "second", "status": second_status,
         "successCriteria": "the second thing is done"}]}


def _status_036(**fields) -> dict:
    """status.json as a 0.36 engine left it: `engineVersion` 0.36.0 and no
    `planPhase` key."""
    return {"runId": "compat-036", "engineVersion": "0.36.0", "approach": 1,
            "maxApproaches": 2, "iteration": 1, "iterationsUsed": 1,
            "iterationsBudget": 12, "startedAt": "2026-01-01T00:00:00Z",
            "verdict": None, **fields}


def _resume(tmp_path: Path, status: dict, tasks: dict, handlers: dict,
            **cfg):
    root = tmp_path / "run"
    # the one iteration the 0.36 engine had recorded (planning), so this
    # engine resumes the approach instead of planning it again
    (root / "iterations" / "0001").mkdir(parents=True)
    atomic_write_json(root / "iterations" / "0001" / "meta.json",
                      {"phase": "planning", "endedAt": "2026-01-01T00:00:00Z"})
    sup = make_supervisor(
        root, run_id="compat-036", status=status,
        prd="# PRD\n\nDo the thing.\n", vigilant=False, on_complete="exit",
        infra_retry_backoff_s=[0.0], infra_retry_backoff_max_s=0.0,
        infra_outage_budget_s=1000.0,
        runner_factory=lambda s: _Hats(s, handlers),
        **{"peer_verdict_gate": False, "max_approaches": 2, "iterations": 12,
           **cfg})
    sup.run.write_tasks(tasks)
    # the 0.36 run dir is exactly what is on disk before this engine starts
    assert "phases" not in sup.run.read_tasks()
    assert sup.run.read_status().get("planPhase") is None
    return sup


def _assert_continued_as_a_no_phase_plan(sup) -> None:
    data = sup.run.read_tasks()
    assert "phases" not in data
    assert all("phase" not in t for t in data["tasks"])
    assert not [e for e in read_events(sup.run.root)
                if str(e.get("type") or e.get("event")).startswith(
                    "phase_gate.")]
    assert sup.run.read_status().get("planPhase") is None
    for meta in sorted(sup.run.root.glob("iterations/*/meta.json")):
        assert "planPhase" not in json.loads(meta.read_text())
    for prompt in sup.runner.prompts:
        for heading in PHASE_HEADINGS:
            assert heading not in prompt


def test_a_036_run_dir_stopped_mid_worker_continues_as_a_no_phase_plan(tmp_path):
    sup = _resume(tmp_path,
                  _status_036(state="running", phase="worker"),
                  _tasks_036("in-progress"),
                  {"worker": [_complete], "review": [_verified]})
    final = asyncio.run(asyncio.wait_for(sup._run_job_core(), 60))
    assert final == "succeeded", sup.run.read_status()
    # planning is not redone; the interrupted task finishes, then review
    assert sup.runner.phases == ["worker", "review"]
    assert {t["id"]: t["status"] for t in sup.run.read_tasks()["tasks"]}[
        "002"] == "completed"
    _assert_continued_as_a_no_phase_plan(sup)


def _question_036(**fields) -> dict:
    """The `inputQuestion` a 0.36 engine parks on (no `planPhase` key)."""
    return {"kind": "findings", "trigger": "cure_rounds_spent",
            "judge": "review",
            "findings": [{"id": "F1", "clause": "requirement A (#1)",
                          "severity": "blocking", "curable": True,
                          "text": "still broken"}],
            "cureRoundsUsed": 1, "cureRoundsAllowed": 1, "approach": 1,
            "maxApproaches": 2, "parkedAt": "2027-01-15T08:00:00Z",
            "expiresAt": "2027-01-15T09:00:00Z",
            "onTimeout": "next_approach", **fields}


def test_a_036_run_dir_parked_in_awaiting_input_continues_as_a_no_phase_plan(tmp_path):
    question = _question_036()
    assert "planPhase" not in question
    sup = _resume(
        tmp_path,
        _status_036(state="awaiting_input", phase="review",
                    pauseReason="operator_input_needed",
                    inputQuestion=question, curePasses={"1": 1}),
        _tasks_036("completed"), {"review": [_verified]})
    seen: dict = {}

    async def wait(timeout_s: float) -> bool:
        if not seen:
            status = sup.run.read_status()
            seen.update(state=status["state"],
                        question=dict(status["inputQuestion"]))
            sup.answer(waive=["F1"])         # the operator answers
        return sup._pause.is_set()
    sup._wait_pause_event = wait             # type: ignore[method-assign]

    final = asyncio.run(asyncio.wait_for(sup._run_job_core(), 60))

    # the 0.36 question is kept as parked, not rewritten as a phase question
    assert seen["state"] == "awaiting_input"
    assert seen["question"] == question
    assert final == "succeeded", sup.run.read_status()
    _assert_continued_as_a_no_phase_plan(sup)


def test_a_036_run_dir_held_exhausted_continues_as_a_no_phase_plan(tmp_path):
    sup = _resume(tmp_path,
                  _status_036(state="exhausted", phase="worker",
                              exhaustedBudget="iterations",
                              iterationsUsed=12, iteration=12),
                  _tasks_036("pending"),
                  {"worker": [_complete], "review": [_verified]},
                  iterations=16)
    final = asyncio.run(asyncio.wait_for(sup._run_job_core(), 60))
    assert final == "succeeded", sup.run.read_status()
    assert sup.runner.phases == ["worker", "review"]
    assert "exhaustedBudget" not in sup.run.read_status()
    _assert_continued_as_a_no_phase_plan(sup)
