"""Task 009 (PRD requirement C.1, #141): "a curable rejection is a cure
pass, not a replan" -- the new `review -> worker` edge
(`rejected_curable_pass_available`), taken when review's verdict is
`rejected`, every blocking finding is `curable: true`, and this approach's
`curePasses` is still `0`.

Four things pinned here, all driven through a real `LoopSupervisor`:

1. the edge is declared, in both variants and under the peer-verdict-gate
   delta, exactly like `rejected_incurable` already is
   (`tests/test_rejected_incurable_edge.py`'s own shape);
2. taking it appends one cure task per blocking finding to the current
   approach's `tasks.json` (`origin: "review-cure"`, title from the
   finding's `clause`, `successCriteria` from `clause`+`evidence`),
   increments `curePasses[approach]` on status.json (surviving a fresh
   read of it), and emits `approach.cure_pass`;
3. the run never archives the approach or writes a composite PRD for a
   cure pass -- unlike the ordinary replan edge;
4. once the appended cure task reaches `completed`, the ordinary
   `worker -> review` `verdict_ready` path asks review again, over the
   SAME approach, and a clean verdict there ends the run terminal-
   succeeded without ever spending a second approach.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from ralphd.engine import findings, transitions
from ralphd.engine.config import JobConfig
from ralphd.engine.loop import LoopSupervisor
from ralphd.engine.runner import IterationResult
from ralphd.engine.state import RunDir, atomic_write_json

from test_steering_priority_routing_task012 import (
    TASK_1, _ok, _run_resumed,
)


def _finding(severity: str, curable: bool, clause: str, evidence: str) -> dict:
    return {"severity": severity, "curable": curable, "clause": clause,
            "evidence": evidence}


def _result(text: str) -> IterationResult:
    r = IterationResult(exit_code=0)
    r.final_text = text
    r.duration_s = 5.0
    r.usage = {"input": 10, "output": 5, "totalTokens": 15}
    return r


def _supervisor(root: Path, **cfg_kw) -> LoopSupervisor:
    root.mkdir(parents=True, exist_ok=True)
    run = RunDir(root=root)
    run.update_status(state="running")
    (root / "prd.md").write_text("# task 009 fixture PRD\n\nDo the thing.\n")
    cfg = JobConfig(run_id="task009-cure-pass", max_approaches=3, **cfg_kw)
    return LoopSupervisor(cfg, run, root)


def _events(root: Path, type_: str) -> list[dict]:
    log = root / "events.jsonl"
    lines = log.read_text().splitlines() if log.exists() else []
    return [ev for ev in (json.loads(x) for x in lines) if ev.get("type") == type_]


# ---------------------------------------------------------------------------
# 1. The declared edge itself.
# ---------------------------------------------------------------------------


def test_the_edge_is_declared_in_both_variants():
    for vigilant in (True, False):
        assert transitions.is_legal("review", "worker", vigilant)
        guards = transitions.guard_names("review", vigilant)
        assert "rejected_curable_pass_available" in guards
        by_guard = {e.guard: e.target for e in
                   transitions.successors("review", vigilant)}
        assert by_guard["rejected_curable_pass_available"] == "worker"


def test_the_edge_is_also_declared_under_the_peer_verdict_gate_delta():
    edges = transitions.successors("review", vigilant=False,
                                   peer_verdict_gate=True)
    by_guard = {e.guard: e.target for e in edges}
    assert by_guard["rejected_curable_pass_available"] == "worker"


def test_rejected_incurable_keeps_precedence_over_the_cure_pass(tmp_path):
    """Requirement C.2: an all-incurable rejection still takes
    `rejected_incurable`'s pause-and-escalate edge, never the cure pass --
    `_step_review` checks `_review_incurable_rejection()` before
    `_review_cure_pass_available()`."""
    root = tmp_path / "run"
    sup = _supervisor(root)
    sup._approach = 1
    atomic_write_json(sup.run.tasks_file, {"version": 1, "tasks": []})

    all_incurable = [
        _finding("blocking", False, "requirement B (#111)", "env fact"),
    ]
    findings.write_findings_json(sup.run.findings_json_file, all_incurable)

    async def _review_rejects(prompt, transcript, **kw):
        return _result(json.dumps({"verdict": "rejected", "reason": "no"}))

    sup.runner.run = _review_rejects  # type: ignore[method-assign]

    outcome = asyncio.run(sup._step_review())

    assert outcome == ("advance", "review", "rejected_incurable")
    assert sup.run.read_status().get("curePasses") is None
    cure_tasks = [t for t in sup.run.read_tasks()["tasks"]
                 if t.get("origin") == "review-cure"]
    assert cure_tasks == []


# ---------------------------------------------------------------------------
# 2-3. Driving a real LoopSupervisor to the edge: append, counter, event,
#      and no archive/composite-PRD.
# ---------------------------------------------------------------------------


def test_all_curable_blocking_findings_take_the_cure_pass_edge(tmp_path):
    root = tmp_path / "run"
    sup = _supervisor(root)
    sup._approach = 1
    atomic_write_json(sup.run.tasks_file, {"version": 1, "tasks": []})

    all_curable = [
        _finding("blocking", True, "requirement C.1 (#141)",
                "the worker never wrote the notify tool"),
        _finding("blocking", True, "requirement C.2 (#141)",
                "curePasses was never persisted"),
    ]
    findings.write_findings_json(sup.run.findings_json_file, all_curable)

    async def _review_rejects(prompt, transcript, **kw):
        return _result(json.dumps({
            "verdict": "rejected",
            "reason": "both blocking findings are curable defects",
        }))

    sup.runner.run = _review_rejects  # type: ignore[method-assign]

    outcome = asyncio.run(sup._step_review())

    assert outcome == ("advance", "worker", "rejected_curable_pass_available")

    # the counter, persisted on status.json (survives a fresh read).
    status = sup.run.read_status()
    assert status.get("curePasses") == {"1": 1}

    # one cure task per blocking finding, in finding order, `origin:
    # "review-cure"`, title from the clause, criteria from clause+evidence.
    tasks = sup.run.read_tasks()["tasks"]
    cure_tasks = [t for t in tasks if t.get("origin") == "review-cure"]
    assert len(cure_tasks) == 2
    for task, finding in zip(cure_tasks, all_curable):
        assert task["title"] == finding["clause"]
        assert task["status"] == "pending"
        assert finding["clause"] in task["successCriteria"]
        assert finding["evidence"] in task["successCriteria"]
    assert cure_tasks[0]["id"] != cure_tasks[1]["id"]

    # the event.
    events = _events(root, "approach.cure_pass")
    assert len(events) == 1
    assert events[0]["approach"] == 1
    assert events[0]["curePasses"] == 1
    assert set(events[0]["taskIds"]) == {t["id"] for t in cure_tasks}

    # never archived, never composited into a fresh approach's PRD -- the
    # SAME approach is what review is asked about again once the cure
    # tasks are done.
    assert not sup.run.composite_prd_file.exists()
    assert sup._approach == 1


def test_a_mix_of_curable_and_incurable_findings_never_takes_the_cure_pass(
        tmp_path):
    """Not every blocking finding curable -> the ordinary replan edge, not
    the cure pass -- `all_blocking_findings_curable` requires the WHOLE
    blocking set."""
    root = tmp_path / "run"
    sup = _supervisor(root)
    sup._approach = 1
    atomic_write_json(sup.run.tasks_file, {"version": 1, "tasks": []})

    mixed = [
        _finding("blocking", True, "requirement C.1 (#141)", "curable one"),
        _finding("blocking", False, "requirement B (#111)", "incurable one"),
    ]
    findings.write_findings_json(sup.run.findings_json_file, mixed)

    async def _review_rejects(prompt, transcript, **kw):
        return _result(json.dumps({"verdict": "rejected", "reason": "mixed"}))

    sup.runner.run = _review_rejects  # type: ignore[method-assign]

    outcome = asyncio.run(sup._step_review())

    assert outcome == ("advance", "replan", "rejected_approach_left")
    assert sup.run.read_status().get("curePasses") is None
    cure_tasks = [t for t in sup.run.read_tasks()["tasks"]
                 if t.get("origin") == "review-cure"]
    assert cure_tasks == []


# ---------------------------------------------------------------------------
# 4. Re-review over the SAME approach once the cure task is done, driven
#    end to end through `_run_job_core()` (never `_step_worker`/
#    `_step_review` in isolation) so "the ordinary COMPLETE -> review path
#    runs again" is checked against the real dispatch loop.
# ---------------------------------------------------------------------------


def _worker_completes_whatever_is_assigned(sup, prompt=""):
    tasks = sup.run.read_tasks().get("tasks", [])
    assigned = next(t["id"] for t in tasks if t.get("status") == "in-progress")
    return _ok(json.dumps({"verdict": "complete", "task_id": assigned}))


def _review_rejects_and_writes_a_curable_finding(sup, prompt=""):
    findings.write_findings_json(sup.run.findings_json_file, [
        _finding("blocking", True, "requirement C.1 (#141)",
                "the worker never wrote the notify tool"),
    ])
    return _ok(json.dumps({"verdict": "rejected", "reason": "curable defect"}))


def _review_verifies_once_cured(sup, prompt=""):
    findings.write_findings_json(sup.run.findings_json_file, [])
    return _ok(json.dumps({"verdict": "verified"}))


def test_the_cure_task_reaching_completed_re_reviews_the_same_approach(
        tmp_path):
    root = tmp_path / "run"
    handlers = {
        "worker": [_worker_completes_whatever_is_assigned],
        "review": [_review_rejects_and_writes_a_curable_finding,
                  _review_verifies_once_cured],
    }
    tasks = [{"id": TASK_1, "title": "the original task",
             "status": "pending",
             "successCriteria": "ship the thing", "dependsOn": [],
             "tier": "normal"}]

    sup, final_state = _run_resumed(root, handlers, tasks, max_approaches=1)

    assert final_state == "succeeded"
    # the whole run stayed on approach 1 -- the cure pass never spent the
    # (single, here) approach budget.
    assert sup._approach == 1
    assert not (root / "approaches" / "01" / "notes.md").exists()
    assert not sup.run.composite_prd_file.exists()

    tasks_on_disk = sup.run.read_tasks()["tasks"]
    original = next(t for t in tasks_on_disk if t["id"] == TASK_1)
    assert original["status"] == "completed"
    cure_tasks = [t for t in tasks_on_disk if t.get("origin") == "review-cure"]
    assert len(cure_tasks) == 1
    assert cure_tasks[0]["status"] == "completed"

    assert sup.run.read_status().get("curePasses") == {"1": 1}
    cure_events = _events(root, "approach.cure_pass")
    assert len(cure_events) == 1
    assert cure_events[0]["approach"] == 1
