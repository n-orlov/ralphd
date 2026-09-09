"""Task 016 (PRD requirement B.1, #156): the FORCED split -- pinning the
three success-criteria behaviours the task's own text names:

1. a worker iteration dispatched after task T's progress budget is spent
   carries a budget-exhausted notice in its own prompt;
2. a report that omits `proposed_tasks` is a contract violation, refused
   and re-asked on `contract_enforcement`'s own retry ladder -- the SAME
   ladder `_apply_worker_task_status_report`'s petition/task_status side
   channels already ride, never a second mechanism;
3. on acceptance the parent task becomes `superseded` (not schedulable,
   not blocking -- its own `dependsOn` inherited by any child that named
   none of its own) and its split children become schedulable next.

`LoopSupervisor._fold_inherited_dependsOn` (the dependsOn-inheritance
fold itself) is pinned directly, unit-level, separately from the fuller
pipeline: a genuinely EXTERNAL inherited id (one of the parent's own
pre-existing neighbours in `tasks.json`, rather than a sibling freshly
proposed in the SAME batch) does not resolve against `plan_ingestion.
lint_worker_split`'s current batch-only `dependsOn` check -- task 017
("Resolve a worker split's dependsOn against the current tasks.json as
well as the batch (#162)") is the task that widens the lint to accept
that shape; this file's own end-to-end acceptance test therefore only
exercises a parent with NO `dependsOn` of its own for the full pipeline,
and pins the fold's own behaviour on inherited values through the
`@staticmethod` directly.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from ralphd.engine import scheduler
from ralphd.engine.loop import LoopSupervisor
from ralphd.engine.runner import IterationResult
from ralphd.engine.state import (
    TASK_DEPENDENCY_SATISFIED_STATUSES,
    is_worker_origin,
    supersede_task,
    task_blocked_by,
)

from tests.conftest import make_supervisor

TASK_1 = "016t"


def _ok(text: str) -> IterationResult:
    r = IterationResult(exit_code=0, duration_s=0.01)
    r.final_text = text
    r.usage = {"input": 1, "output": 1, "totalTokens": 2}
    return r


def _task(**over) -> dict:
    task = {"id": TASK_1, "title": "a task whose budget will be spent",
            "status": "in-progress",
            "successCriteria": "the whole thing works end to end",
            "estimatedIterations": 1}
    task.update(over)
    return task


def _spent_budget(used=3, budget=3, task_id=TASK_1) -> dict:
    return {"taskId": task_id, "used": used, "budget": budget}


class _RecordingRunner:
    """Same shape as `tests/test_worker_hat.py`'s `_SequencedRunner`, plus
    `.prompts` -- this file needs to inspect the actual prompt text the
    budget-exhausted notice lands in, not just count calls."""

    def __init__(self, texts):
        self.texts = list(texts)
        self.calls = 0
        self.prompts: list[str] = []
        self.running = False

    def interrupt(self) -> bool:
        return self.running

    async def run(self, prompt, transcript, **kw) -> IterationResult:
        self.prompts.append(prompt)
        idx = min(self.calls, len(self.texts) - 1)
        self.calls += 1
        return self.texts[idx]


def _sup(tmp_path: Path, texts: list[str], tasks: list[dict],
         budget: dict | None = None, **cfg) -> LoopSupervisor:
    runner = _RecordingRunner([_ok(t) for t in texts])
    kw = {"iterations": 1, "max_approaches": 1, "vigilant": False,
          "on_complete": "exit", "worker_split_budget_per_approach": 5,
          "contract_retry_max": 2, "tasks": tasks,
          "status": ({"state": "running", "progressBudget": budget}
                     if budget is not None else {"state": "running"}),
          "runner_factory": lambda sup: runner,
          **cfg}
    sup = make_supervisor(tmp_path / "run", run_id="task016", **kw)
    return sup


# ---------------------------------------------------------------------------
# 1. the budget-exhausted notice lands in the very next dispatch's prompt
# ---------------------------------------------------------------------------

def test_dispatch_after_budget_spent_carries_the_notice(tmp_path):
    payload = {"verdict": "no_verdict",
               "proposed_tasks": [{"id": "016t-a", "title": "part a",
                                   "successCriteria": "a on its own works"}]}
    sup = _sup(tmp_path, [json.dumps(payload)], [_task()],
               budget=_spent_budget())
    asyncio.run(sup._step_worker())
    runner = sup.runner
    assert runner.prompts, "no iteration was dispatched at all"
    notice = runner.prompts[0]
    assert "Progress budget exhausted" in notice
    assert TASK_1 in notice
    assert "3/3" in notice
    assert "proposed_tasks" in notice
    assert "superseded" in notice


def test_dispatch_under_budget_carries_no_notice(tmp_path):
    """The mirror-image pin: absent entirely while the budget is not yet
    spent -- same "presence is the signal" discipline task 015's retry
    block already established for `validationNotes`."""
    payload = {"verdict": "no_verdict"}
    sup = _sup(tmp_path, [json.dumps(payload)], [_task()],
               budget=_spent_budget(used=1, budget=3))
    asyncio.run(sup._step_worker())
    assert "Progress budget exhausted" not in sup.runner.prompts[0]


# ---------------------------------------------------------------------------
# 2. a report without proposed_tasks is refused on the contract ladder
# ---------------------------------------------------------------------------

def test_report_without_proposed_tasks_is_refused_on_the_ladder(tmp_path):
    payload = {"verdict": "no_verdict"}
    sup = _sup(tmp_path, [json.dumps(payload)], [_task()],
               budget=_spent_budget())
    asyncio.run(sup._step_worker())
    runner = sup.runner
    # original dispatch + 2 re-asks (`contract_retry_max=2`) -- the SAME
    # ladder shape `tests/test_verify_contract_ladder_task001.py` pins for
    # every other worker side channel.
    assert runner.calls == 3, runner.calls
    events = [json.loads(x) for x in
              (tmp_path / "run" / "events.jsonl").read_text().splitlines()]
    violations = [e for e in events if e.get("type") == "contract.violation"]
    assert violations, events
    assert any("proposed_tasks" in json.dumps(e) for e in violations)
    data = sup.run.read_tasks()
    task = next(t for t in data["tasks"] if t["id"] == TASK_1)
    assert task["status"] != "superseded"
    assert not any(is_worker_origin(t) for t in data["tasks"])


def test_report_that_eventually_carries_proposed_tasks_is_accepted(tmp_path):
    """A worker that gets it wrong twice, then right on the last rung of
    the ladder, is accepted exactly like every other side channel's own
    re-ask success case."""
    bad = json.dumps({"verdict": "no_verdict"})
    good = json.dumps({
        "verdict": "no_verdict",
        "proposed_tasks": [{"id": "016t-a", "title": "part a",
                            "successCriteria": "a on its own works"}]})
    sup = _sup(tmp_path, [bad, bad, good], [_task()], budget=_spent_budget())
    asyncio.run(sup._step_worker())
    assert sup.runner.calls == 3
    data = sup.run.read_tasks()
    assert any(t["id"] == "016t-a" for t in data["tasks"])
    parent = next(t for t in data["tasks"] if t["id"] == TASK_1)
    assert parent["status"] == "superseded"


# ---------------------------------------------------------------------------
# 3. acceptance: the parent is superseded, the children are schedulable
# ---------------------------------------------------------------------------

def test_acceptance_supersedes_parent_and_child_becomes_schedulable(tmp_path):
    payload = {"verdict": "no_verdict",
               "proposed_tasks": [{"id": "016t-a", "title": "part a",
                                   "successCriteria": "a on its own works"}]}
    sup = _sup(tmp_path, [json.dumps(payload)], [_task()],
               budget=_spent_budget())
    asyncio.run(sup._step_worker())
    data = sup.run.read_tasks()
    parent = next(t for t in data["tasks"] if t["id"] == TASK_1)
    child = next(t for t in data["tasks"] if t["id"] == "016t-a")
    assert parent["status"] == "superseded"
    assert child["status"] == "pending"
    assert child["splitFrom"] == TASK_1
    assert is_worker_origin(child)
    # not schedulable: `select_next_task` never re-picks the superseded
    # parent, and instead the child is what a fresh dispatch would pick.
    picked = scheduler.select_next_task(data["tasks"])
    assert picked is not None and picked["id"] == "016t-a"


def test_superseded_parent_does_not_block_a_dependent(tmp_path):
    """"not blocking" (PRD B.1): a task whose `dependsOn` names the
    now-superseded parent reads that dependency as satisfied, exactly like
    `completed`/`validated` -- never stuck waiting for a status the parent
    will never reach again."""
    dependent = {"id": "x", "title": "depends on the split parent",
                "status": "pending", "successCriteria": "x works",
                "dependsOn": [TASK_1]}
    payload = {"verdict": "no_verdict",
               "proposed_tasks": [{"id": "016t-a", "title": "part a",
                                   "successCriteria": "a on its own works",
                                   "priority": -5}]}
    sup = _sup(tmp_path, [json.dumps(payload)], [_task(), dependent],
               budget=_spent_budget())
    asyncio.run(sup._step_worker())
    data = sup.run.read_tasks()
    blocked = task_blocked_by(data["tasks"])
    assert "x" not in blocked
    picked = scheduler.select_next_task(data["tasks"])
    assert picked is not None and picked["id"] == "x"


def test_superseded_joins_the_dependency_satisfied_tuple():
    assert "superseded" in TASK_DEPENDENCY_SATISFIED_STATUSES
    assert "skipped" not in TASK_DEPENDENCY_SATISFIED_STATUSES
    assert "failed" not in TASK_DEPENDENCY_SATISFIED_STATUSES


# ---------------------------------------------------------------------------
# state.supersede_task -- the one writer, at the unit level
# ---------------------------------------------------------------------------

def test_supersede_task_writer(tmp_path):
    sup = _sup(tmp_path, ["{}"], [_task()])
    assert supersede_task(sup.run, TASK_1) is True
    data = sup.run.read_tasks()
    task = next(t for t in data["tasks"] if t["id"] == TASK_1)
    assert task["status"] == "superseded"


def test_supersede_task_writer_returns_false_for_an_unknown_id(tmp_path):
    sup = _sup(tmp_path, ["{}"], [_task()])
    assert supersede_task(sup.run, "does-not-exist") is False
    data = sup.run.read_tasks()
    task = next(t for t in data["tasks"] if t["id"] == TASK_1)
    assert task["status"] != "superseded"


# ---------------------------------------------------------------------------
# LoopSupervisor._fold_inherited_dependsOn -- the inheritance fold itself
# ---------------------------------------------------------------------------

def test_fold_inherited_dependsOn_applies_only_when_absent():
    proposed = [
        {"id": "a", "title": "a", "successCriteria": "a"},
        {"id": "b", "title": "b", "successCriteria": "b",
         "dependsOn": ["own-dep"]},
    ]
    folded = LoopSupervisor._fold_inherited_dependsOn(proposed, ["p-dep"])
    a = next(t for t in folded if t["id"] == "a")
    b = next(t for t in folded if t["id"] == "b")
    assert a["dependsOn"] == ["p-dep"]
    assert b["dependsOn"] == ["own-dep"]           # untouched


def test_fold_inherited_dependsOn_is_a_noop_when_parent_had_none():
    proposed = [{"id": "a", "title": "a", "successCriteria": "a"}]
    folded = LoopSupervisor._fold_inherited_dependsOn(proposed, None)
    assert folded == proposed
    assert "dependsOn" not in folded[0]


def test_fold_inherited_dependsOn_never_mutates_its_input():
    original = {"id": "a", "title": "a", "successCriteria": "a"}
    proposed = [original]
    LoopSupervisor._fold_inherited_dependsOn(proposed, ["p-dep"])
    assert "dependsOn" not in original
