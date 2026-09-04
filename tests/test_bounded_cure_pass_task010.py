"""Task 010 (PRD requirement C.2, #141): bound the cure pass task 009 added
to exactly ONE per approach, and re-pin that the `rejected_incurable` edge
still wins over it in every case -- not just the first rejection.

Task 009's own `_review_cure_pass_available` already gates the cure-pass
edge on `curePasses[approach] == 0`, read fresh off `status.json` every
time (`_cure_passes_for_approach`, disk not memory) -- so the one-per-
approach bound and its survival across a process restart are both, in
principle, already load-bearing side effects of that one check. This
module is the DEDICATED test coverage requirement 010 asks for: it drives
the real `LoopSupervisor._step_review()` (never a mock of the guard
itself) to prove, independently of task 009's own tests, that:

1. a SECOND `rejected` review over the same approach -- even with every
   blocking finding curable -- takes the ordinary `rejected_approach_left`
   replan edge (or, once the budget is spent on the last approach, task
   011's `rejected_last_approach` pause-and-escalate edge, superseding
   this module's own original pin of `rejected_no_approach_left` there --
   see that test's own docstring), never a second cure pass, and never
   appends a second batch of cure tasks;
2. `rejected_incurable` keeps its precedence over the cure pass on that
   SECOND rejection too, exactly as it does on the first;
3. a brand-new `LoopSupervisor` instance constructed over the SAME on-disk
   run dir -- standing in for a killed-and-restarted process, the exact
   fixture shape `tests/test_resume_after_incurable_rejection.py` uses --
   reads `curePasses` from `status.json` and enforces the same one-per-
   approach bound with no shared Python state at all, so a resumed run
   never appends the cure tasks twice.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from ralphd.engine import findings
from ralphd.engine.config import JobConfig
from ralphd.engine.loop import LoopSupervisor
from ralphd.engine.runner import IterationResult
from ralphd.engine.state import RunDir, atomic_write_json


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
    (root / "prd.md").write_text("# task 010 fixture PRD\n\nDo the thing.\n")
    kw = {"max_approaches": 3}
    kw.update(cfg_kw)
    cfg = JobConfig(run_id="task010-bounded-cure-pass", **kw)
    return LoopSupervisor(cfg, run, root)


def _rejects_with(findings_list: list[dict]):
    async def _review(prompt, transcript, **kw):
        findings.write_findings_json(
            _review.sup.run.findings_json_file, findings_list)
        return _result(json.dumps({"verdict": "rejected", "reason": "no"}))
    return _review


def _cure_once(root: Path, approach: int = 1, max_approaches: int = 3
              ) -> LoopSupervisor:
    """Episode 1: drive a real `_step_review()` to the cure-pass edge,
    exactly like `tests/test_cure_pass_task009.py`'s own fixture -- the
    starting point every test below builds its SECOND rejection on top
    of."""
    sup = _supervisor(root, max_approaches=max_approaches)
    sup._approach = approach
    atomic_write_json(sup.run.tasks_file, {"version": 1, "tasks": []})

    curable = [_finding("blocking", True, "requirement C.1 (#141)",
                        "the worker never wrote the notify tool")]
    findings.write_findings_json(sup.run.findings_json_file, curable)

    reviewer = _rejects_with(curable)
    reviewer.sup = sup  # type: ignore[attr-defined]
    sup.runner.run = reviewer  # type: ignore[method-assign]

    outcome = asyncio.run(sup._step_review())
    assert outcome == ("advance", "worker", "rejected_curable_pass_available")
    assert sup.run.read_status().get("curePasses") == {str(approach): 1}
    return sup


def _cure_tasks(run: RunDir) -> list[dict]:
    return [t for t in run.read_tasks()["tasks"]
           if t.get("origin") == "review-cure"]


def _events(root: Path, type_: str) -> list[dict]:
    log = root / "events.jsonl"
    lines = log.read_text().splitlines() if log.exists() else []
    return [ev for ev in (json.loads(x) for x in lines) if ev.get("type") == type_]


# ---------------------------------------------------------------------------
# 1. A second `rejected` review over the same approach -> replan, not a
#    second cure pass.
# ---------------------------------------------------------------------------


def test_second_rejection_same_approach_takes_the_ordinary_replan_edge(
        tmp_path):
    root = tmp_path / "run"
    sup = _cure_once(root, approach=1, max_approaches=3)
    before_cure_tasks = _cure_tasks(sup.run)
    assert len(before_cure_tasks) == 1

    still_curable = [_finding("blocking", True, "requirement C.1 (#141)",
                              "still not fixed")]
    reviewer = _rejects_with(still_curable)
    reviewer.sup = sup  # type: ignore[attr-defined]
    sup.runner.run = reviewer  # type: ignore[method-assign]

    outcome = asyncio.run(sup._step_review())

    assert outcome == ("advance", "replan", "rejected_approach_left")
    # exactly one cure pass ever taken for this approach -- the bound.
    assert sup.run.read_status().get("curePasses") == {"1": 1}
    # no second batch of cure tasks appended.
    assert _cure_tasks(sup.run) == before_cure_tasks
    events = _events(root, "approach.cure_pass")
    assert len(events) == 1  # only the first pass ever fired the event
    # the archive/composite-PRD side effects of the ordinary replan edge
    # DID run this time (unlike the cure pass itself).
    assert sup.run.composite_prd_file.exists()


def test_second_rejection_with_no_approach_left_takes_task011s_pause_edge(
        tmp_path):
    """Task 011 (PRD requirement D.1, #142) supersedes this test's
    original pin: once the approach budget is spent, a SECOND rejection
    (still all-curable, cure already spent) no longer ends the run
    `failed` on `rejected_no_approach_left` -- it takes the new `rejected_
    last_approach` pause-and-escalate edge instead (never a second cure
    pass, never the ordinary mid-run replan edge either), exactly as
    `test_bounded_cure_pass_task010.py`'s sibling module documents its own
    precedent for updating an already-passing pinned test to match a real,
    intended behaviour change (task 009's notes, re: `test_peer_verdict_
    gate.py`/`test_criteria_amendment_authority.py`)."""
    root = tmp_path / "run"
    sup = _cure_once(root, approach=1, max_approaches=1)

    still_curable = [_finding("blocking", True, "requirement C.1 (#141)",
                              "still not fixed")]
    reviewer = _rejects_with(still_curable)
    reviewer.sup = sup  # type: ignore[attr-defined]
    sup.runner.run = reviewer  # type: ignore[method-assign]

    assert sup._pause.is_set() is True  # not paused yet

    outcome = asyncio.run(sup._step_review())

    assert outcome == ("advance", "review", "rejected_last_approach")
    assert sup._pause.is_set() is False  # paused, not failed
    status = sup.run.read_status()
    assert status.get("state") != "succeeded"
    assert status.get("state") != "failed"  # requirement D.1: never failed on paperwork
    assert status.get("curePasses") == {"1": 1}
    assert len(_cure_tasks(sup.run)) == 1
    # the approach is never archived on this edge -- resume re-reviews the
    # SAME approach.
    assert not (root / "approaches" / "01").exists()


# ---------------------------------------------------------------------------
# 2. `rejected_incurable` keeps precedence over the cure pass on the
#    SECOND rejection too, not merely the first.
# ---------------------------------------------------------------------------


def test_incurable_precedence_wins_on_the_second_rejection_too(tmp_path):
    root = tmp_path / "run"
    sup = _cure_once(root, approach=1, max_approaches=3)

    all_incurable = [_finding("blocking", False, "requirement B (#111)",
                              "an environment fact a fresh approach and a "
                              "cure task cannot change")]
    reviewer = _rejects_with(all_incurable)
    reviewer.sup = sup  # type: ignore[attr-defined]
    sup.runner.run = reviewer  # type: ignore[method-assign]

    outcome = asyncio.run(sup._step_review())

    assert outcome == ("advance", "review", "rejected_incurable")
    # neither the cure pass counter nor the cure task set changed: the
    # incurable branch never even reaches `_review_cure_pass_available`.
    assert sup.run.read_status().get("curePasses") == {"1": 1}
    assert len(_cure_tasks(sup.run)) == 1
    events = _events(root, "approach.cure_pass")
    assert len(events) == 1
    assert not sup.run.composite_prd_file.exists()


# ---------------------------------------------------------------------------
# 3. Resume mid-cure: a brand-new `LoopSupervisor` over the same run dir
#    (no shared Python state) reads `curePasses` off `status.json` and
#    enforces the same bound -- the cure tasks are never appended twice.
# ---------------------------------------------------------------------------


def test_resume_mid_cure_never_appends_the_cure_tasks_twice(tmp_path):
    root = tmp_path / "run"
    sup1 = _cure_once(root, approach=1, max_approaches=3)
    cure_tasks_before = _cure_tasks(sup1.run)
    assert len(cure_tasks_before) == 1
    # mark the appended cure task `completed`, the way the ordinary
    # `worker -> review` `verdict_ready` path would leave it before asking
    # review again over the same approach.
    tasks_doc = sup1.run.read_tasks()
    for t in tasks_doc["tasks"]:
        if t.get("origin") == "review-cure":
            t["status"] = "completed"
    atomic_write_json(sup1.run.tasks_file, tasks_doc)

    # Episode 2: a FRESH `LoopSupervisor`, constructed over the SAME
    # on-disk run dir -- what a killed-and-restarted process leaves
    # behind, exactly `tests/test_resume_after_incurable_rejection.py`'s
    # own two-episode shape. Nothing from `sup1` (its `curePasses`
    # counter, its cure-task list) is passed to it in memory; everything
    # this episode knows comes from a fresh `RunDir.read_status()`/
    # `read_tasks()`.
    sup2 = _supervisor(root, max_approaches=3)
    sup2._approach = 1

    assert sup2._cure_passes_for_approach(1) == 1, (
        "a fresh process must read `curePasses` for this approach off "
        "status.json, not default back to 0")

    still_curable = [_finding("blocking", True, "requirement C.1 (#141)",
                              "still not fixed after the cure")]
    reviewer = _rejects_with(still_curable)
    reviewer.sup = sup2  # type: ignore[attr-defined]
    sup2.runner.run = reviewer  # type: ignore[method-assign]

    outcome = asyncio.run(sup2._step_review())

    # the resumed process takes the ordinary replan edge, never a second
    # cure pass, even though it never ran the first cure pass itself.
    assert outcome == ("advance", "replan", "rejected_approach_left")
    assert sup2.run.read_status().get("curePasses") == {"1": 1}
    # no duplicate cure tasks appended by the resumed episode.
    after = _cure_tasks(sup2.run)
    assert len(after) == 1
    assert after[0]["id"] == cure_tasks_before[0]["id"]
    # the first episode's cure-pass event is still the only one on disk.
    events = _events(root, "approach.cure_pass")
    assert len(events) == 1


def test_resume_mid_cure_also_keeps_the_incurable_precedence(tmp_path):
    """Same two-episode resume shape as above, but the resumed episode's
    review finds every blocking finding incurable -- `rejected_incurable`
    must still win, from a process that never took the cure pass itself
    and only knows about it via disk."""
    root = tmp_path / "run"
    sup1 = _cure_once(root, approach=1, max_approaches=3)
    tasks_doc = sup1.run.read_tasks()
    for t in tasks_doc["tasks"]:
        if t.get("origin") == "review-cure":
            t["status"] = "completed"
    atomic_write_json(sup1.run.tasks_file, tasks_doc)

    sup2 = _supervisor(root, max_approaches=3)
    sup2._approach = 1
    assert sup2._cure_passes_for_approach(1) == 1

    all_incurable = [_finding("blocking", False, "requirement B (#111)",
                              "an environment fact")]
    reviewer = _rejects_with(all_incurable)
    reviewer.sup = sup2  # type: ignore[attr-defined]
    sup2.runner.run = reviewer  # type: ignore[method-assign]

    outcome = asyncio.run(sup2._step_review())

    assert outcome == ("advance", "review", "rejected_incurable")
    assert sup2.run.read_status().get("curePasses") == {"1": 1}
    assert len(_cure_tasks(sup2.run)) == 1
