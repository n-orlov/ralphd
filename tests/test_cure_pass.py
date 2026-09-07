"""D.2 -- the cure pass, folded into one module (task 023, requirement D.2).
Absorbs, whole, the five `test_*_task*.py` modules whose subject was the
`rejected_curable_pass_available` edge family (task 009, requirement C.1,
#141) and `state.insert_cure_tasks`, the call both the engine's own review
cure pass and the steer hat's `task-update` make to append cure tasks:

* `tests/test_cure_pass_task009.py` (task 009, requirement C.1, #141): the
  new `review -> worker` edge itself, declared in both variants and under
  the peer-verdict-gate delta; taking it appends one cure task per blocking
  finding, bumps `curePasses[approach]`, emits `approach.cure_pass`, and
  never archives the approach or writes a composite PRD; the cure task
  reaching `completed` re-reviews the SAME approach through the ordinary
  `verdict_ready` path.
* `tests/test_bounded_cure_pass_task010.py` (task 010, requirement C.2,
  #141): the one-per-approach bound on that edge -- a second rejection over
  the same approach takes the ordinary replan edge (or task 011's
  `rejected_last_approach` pause once the budget is spent), never a second
  cure pass; `rejected_incurable` keeps precedence over the cure pass on
  that second rejection too; and a brand-new `LoopSupervisor` over the same
  on-disk run dir enforces the identical bound with no shared Python state.
* `tests/test_cure_pass_id_collision_task006.py` (task 006, requirement
  C.1, finding 4): `state.insert_cure_tasks` picks a fresh, still-unused id
  instead of silently skipping an entry when its deterministic
  `cure-{approach:02d}-{index:02d}` id already exists in `tasks.json`.
* `tests/test_cure_pass_not_on_tamper_task039.py` (task 039, operator steer
  002): a review rejection carrying a criteria-tamper finding never takes
  the cure-pass edge, even though the engine's own tamper finding is itself
  `curable: True` -- curing hands the SAME approach another pass over the
  very tasks whose criteria are in dispute, so a tampered approach is
  archived and replanned exactly as it was before the cure-pass edge
  existed.
* `tests/test_cure_inherits_dependents_task041.py` (task 041, requirement
  C.4, #149): when the cure pass appends a cure task for a finding whose
  evidence names a `failed` task, every dependent of that failed task gets
  the cure task added to its `dependsOn` and the failed task removed --
  across all three `dependsOn` shapes the requirement's "no doomed
  remainder" claim distinguishes -- and `src/ralphd/prompts/steer.md`
  documents the identical move for the steer hat's own `task-update`.

No two absorbed modules collided on a node name; every node below keeps its
original function name. Everything below drives either a real
`LoopSupervisor` (`_step_review()`) or the real `state.insert_cure_tasks`
directly -- never a mock of the guard itself.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from ralphd.engine import findings, transitions
from ralphd.engine.loop import LoopSupervisor
from ralphd.engine.runner import IterationResult
from ralphd.engine.state import RunDir, atomic_write_json, insert_cure_tasks

from test_steering_priority_routing_task012 import TASK_1, _ok, _run_resumed
from tests.conftest import supervisor_factory

REPO_ROOT = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------------------
# Shared fixtures.
# ---------------------------------------------------------------------------


def _finding(severity: str, curable: bool, clause: str, evidence: str) -> dict:
    return {"severity": severity, "curable": curable, "clause": clause,
            "evidence": evidence}


def _blocking_curable_finding(clause: str, evidence: str) -> dict:
    """The `severity: "blocking", curable: True` shape task 006's and task
    041's `insert_cure_tasks` pins both build every finding from."""
    return {"severity": "blocking", "curable": True, "clause": clause,
            "evidence": evidence}


def _result(text: str) -> IterationResult:
    r = IterationResult(exit_code=0)
    r.final_text = text
    r.duration_s = 5.0
    r.usage = {"input": 10, "output": 5, "totalTokens": 15}
    return r


def _events(root: Path, type_: str) -> list[dict]:
    log = root / "events.jsonl"
    lines = log.read_text().splitlines() if log.exists() else []
    return [ev for ev in (json.loads(x) for x in lines) if ev.get("type") == type_]


# task 009's shape: a bare review-only run.
_supervisor = supervisor_factory(run_id="task009-cure-pass", cfg_defaults=False,
                                 status={"state": "running"},
                                 prd="# task 009 fixture PRD\n\nDo the thing.\n",
                                 max_approaches=3)

# task 010's shape: the same, under its own run id.
_supervisor_bounded = supervisor_factory(
    run_id="task010-bounded-cure-pass", cfg_defaults=False,
    status={"state": "running"},
    prd="# task 010 fixture PRD\n\nDo the thing.\n", max_approaches=3)

# task 039's shape: the same, under its own run id.
_supervisor_tamper = supervisor_factory(
    run_id="task039-tamper-not-curable", cfg_defaults=False,
    status={"state": "running"},
    prd="# task 039 fixture PRD\n\nDo the thing.\n", max_approaches=3)


# ---------------------------------------------------------------------------
# task 009 (PRD requirement C.1, #141): "a curable rejection is a cure
# pass, not a replan."
# ---------------------------------------------------------------------------
# 1. The declared edge itself.


def test_the_edge_is_declared_in_both_variants():
    for vigilant in (True, False):
        assert transitions.is_legal("review", "worker", vigilant)
        guards = transitions.guard_names("review", vigilant)
        assert "rejected_curable_pass_available" in guards
        by_guard = {e.guard: e.target for e in
                   transitions.successors("review", vigilant)}
        assert by_guard["rejected_curable_pass_available"] == "cure"


def test_the_edge_is_also_declared_under_the_peer_verdict_gate_delta():
    edges = transitions.successors("review", vigilant=False,
                                   peer_verdict_gate=True)
    by_guard = {e.guard: e.target for e in edges}
    assert by_guard["rejected_curable_pass_available"] == "cure"


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


# 2-3. Driving a real LoopSupervisor to the edge: append, counter, event,
#      and no archive/composite-PRD.


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

    assert outcome == ("advance", "cure", "rejected_curable_pass_available")

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


# 4. Re-review over the SAME approach once the cure task is done, driven
#    end to end through `_run_job_core()` (never `_step_worker`/
#    `_step_review` in isolation) so "the ordinary COMPLETE -> review path
#    runs again" is checked against the real dispatch loop.


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


# ---------------------------------------------------------------------------
# task 010 (PRD requirement C.2, #141): bound the cure pass to exactly ONE
# per approach, and re-pin that `rejected_incurable` keeps precedence over
# it in every case -- not just the first rejection.
# ---------------------------------------------------------------------------


def _rejects_with(findings_list: list[dict]):
    async def _review(prompt, transcript, **kw):
        findings.write_findings_json(
            _review.sup.run.findings_json_file, findings_list)
        return _result(json.dumps({"verdict": "rejected", "reason": "no"}))
    return _review


def _cure_once(root: Path, approach: int = 1, max_approaches: int = 3
              ) -> LoopSupervisor:
    """Episode 1: drive a real `_step_review()` to the cure-pass edge,
    exactly like the task 009 fixture above -- the starting point every
    test below builds its SECOND rejection on top of."""
    sup = _supervisor_bounded(root, max_approaches=max_approaches)
    sup._approach = approach
    atomic_write_json(sup.run.tasks_file, {"version": 1, "tasks": []})

    curable = [_finding("blocking", True, "requirement C.1 (#141)",
                        "the worker never wrote the notify tool")]
    findings.write_findings_json(sup.run.findings_json_file, curable)

    reviewer = _rejects_with(curable)
    reviewer.sup = sup  # type: ignore[attr-defined]
    sup.runner.run = reviewer  # type: ignore[method-assign]

    outcome = asyncio.run(sup._step_review())
    assert outcome == ("advance", "cure", "rejected_curable_pass_available")
    assert sup.run.read_status().get("curePasses") == {str(approach): 1}
    return sup


def _cure_tasks(run: RunDir) -> list[dict]:
    return [t for t in run.read_tasks()["tasks"]
           if t.get("origin") == "review-cure"]


# 1. A second `rejected` review over the same approach -> replan, not a
#    second cure pass.


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
    pass, never the ordinary mid-run replan edge either)."""
    root = tmp_path / "run"
    sup = _cure_once(root, approach=1, max_approaches=1)

    still_curable = [_finding("blocking", True, "requirement C.1 (#141)",
                              "still not fixed")]
    reviewer = _rejects_with(still_curable)
    reviewer.sup = sup  # type: ignore[attr-defined]
    sup.runner.run = reviewer  # type: ignore[method-assign]

    assert sup._pause.is_set() is True  # not paused yet

    outcome = asyncio.run(sup._step_review())

    assert outcome == ("advance", "paused", "rejected_last_approach")
    assert sup._pause.is_set() is False  # paused, not failed
    status = sup.run.read_status()
    assert status.get("state") != "succeeded"
    assert status.get("state") != "failed"  # requirement D.1: never failed on paperwork
    assert status.get("curePasses") == {"1": 1}
    assert len(_cure_tasks(sup.run)) == 1
    # the approach is never archived on this edge -- resume re-reviews the
    # SAME approach.
    assert not (root / "approaches" / "01").exists()


# 2. `rejected_incurable` keeps precedence over the cure pass on the
#    SECOND rejection too, not merely the first.


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


# 3. Resume mid-cure: a brand-new `LoopSupervisor` over the same run dir
#    (no shared Python state) reads `curePasses` off `status.json` and
#    enforces the same bound -- the cure tasks are never appended twice.


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
    sup2 = _supervisor_bounded(root, max_approaches=3)
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

    sup2 = _supervisor_bounded(root, max_approaches=3)
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


# ---------------------------------------------------------------------------
# task 006 (PRD requirement C.1, finding 4): `state.insert_cure_tasks` must
# append exactly one cure task per entry of `blocking`, even when the
# deterministic `cure-{approach:02d}-{index:02d}` id it would normally use
# is already present in `tasks.json`. Before this task the collision was
# handled by silently SKIPPING that entry -- a genuinely blocking finding
# could end up with no cure task at all. Now a collision picks a fresh,
# still-unused id instead.
# ---------------------------------------------------------------------------


def _make_run(root: Path, existing_ids: list[str]) -> RunDir:
    root.mkdir(parents=True, exist_ok=True)
    run = RunDir(root=root)
    existing_tasks = [
        {"id": tid, "title": "pre-existing", "successCriteria": "n/a",
         "status": "pending", "origin": "planner", "dependsOn": [], "tier": "normal"}
        for tid in existing_ids
    ]
    atomic_write_json(run.tasks_file, {"version": 1, "tasks": existing_tasks})
    return run


def test_a_colliding_deterministic_id_gets_a_fresh_id_instead_of_being_skipped(tmp_path):
    root = tmp_path / "run"
    run = _make_run(root, ["cure-01-01"])

    finding = _blocking_curable_finding("requirement B.1 (#111)", "concrete evidence text")
    inserted = insert_cure_tasks(run, 1, [finding])

    # Exactly one id returned, one per finding entry -- never skipped.
    assert len(inserted) == 1
    new_id = inserted[0]
    assert new_id != "cure-01-01"

    data = run.read_tasks()
    tasks = data["tasks"]
    ids = [t["id"] for t in tasks]
    assert ids.count(new_id) == 1
    assert "cure-01-01" in ids  # untouched pre-existing task survives

    new_task = next(t for t in tasks if t["id"] == new_id)
    assert new_task["title"] == finding["clause"]
    assert finding["clause"] in new_task["successCriteria"]
    assert finding["evidence"] in new_task["successCriteria"]
    assert new_task["origin"] == "review-cure"


def test_multiple_collisions_each_get_a_distinct_fresh_id(tmp_path):
    root = tmp_path / "run"
    # Pre-occupy the deterministic id AND the first fallback slot too.
    run = _make_run(root, ["cure-01-01", "cure-01-01-2"])

    finding = _blocking_curable_finding("requirement D (#112)", "another concrete gap")
    inserted = insert_cure_tasks(run, 1, [finding])

    assert len(inserted) == 1
    new_id = inserted[0]
    assert new_id not in ("cure-01-01", "cure-01-01-2")

    data = run.read_tasks()
    ids = [t["id"] for t in data["tasks"]]
    assert ids.count(new_id) == 1


def test_no_collision_still_uses_the_plain_deterministic_id(tmp_path):
    root = tmp_path / "run"
    run = _make_run(root, [])

    finding = _blocking_curable_finding("requirement E (#113)", "yet more evidence")
    inserted = insert_cure_tasks(run, 1, [finding])

    assert inserted == ["cure-01-01"]


def test_one_task_appended_per_blocking_finding_even_with_a_collision(tmp_path):
    root = tmp_path / "run"
    run = _make_run(root, ["cure-01-02"])  # collides with the SECOND finding's id

    findings_list = [
        _blocking_curable_finding("requirement F.1 (#114)", "first evidence"),
        _blocking_curable_finding("requirement F.2 (#115)", "second evidence"),
    ]
    inserted = insert_cure_tasks(run, 1, findings_list)

    assert len(inserted) == 2
    assert inserted[0] == "cure-01-01"
    assert inserted[1] != "cure-01-02"

    data = run.read_tasks()
    cure_tasks = [t for t in data["tasks"] if t.get("origin") == "review-cure"]
    assert len(cure_tasks) == 2


# ---------------------------------------------------------------------------
# task 039 (operator steer 002): a review rejection that carries a
# criteria-tamper finding must NEVER take this wave's cure-pass edge.
# ---------------------------------------------------------------------------

TAMPERED_TASK_ID = "001"


def _tampered_task(tampered: bool) -> dict:
    task = {
        "id": TAMPERED_TASK_ID,
        "title": "the task whose criteria were rewritten",
        "successCriteria": "the ORIGINAL, authoritative criteria text",
        "criteriaOfRecord": "the ORIGINAL, authoritative criteria text",
        "status": "completed",
        "validationAttempts": 1,
    }
    if tampered:
        task["criteriaEditedAfterValidationFailure"] = True
    return task


def _drive_review_over(sup: LoopSupervisor) -> tuple:
    """One real `_step_review()` pass whose review payload rejects, with a
    single blocking-but-curable finding of its own in the sidecar (so the
    only difference between the two cases below is the task marker)."""
    findings.write_findings_json(sup.run.findings_json_file, [
        _finding("blocking", True, "requirement C.1 (#141)",
                 "the worker never wrote the notify tool"),
    ])

    async def _review_rejects(prompt, transcript, **kw):
        return _result(json.dumps({
            "verdict": "rejected",
            "reason": "one blocking finding, curable on its face",
        }))

    sup.runner.run = _review_rejects  # type: ignore[method-assign]
    return asyncio.run(sup._step_review())


# 1. The recogniser.


def test_the_engines_own_tamper_finding_is_recognised():
    flagged = findings.flagged_criteria_findings([_tampered_task(tampered=True)])
    assert len(flagged) == 1
    assert findings.CRITERIA_TAMPER_CLAUSE_MARKER in flagged[0]["clause"]
    assert findings.any_criteria_tamper_finding(flagged) is True
    # and it really is the curable-flagged finding whose remedy this task
    # withholds -- the gate cannot be derived from `curable` alone.
    assert flagged[0]["curable"] is True


def test_an_ordinary_findings_list_is_not_mistaken_for_a_tamper_finding():
    assert findings.any_criteria_tamper_finding([]) is False
    assert findings.any_criteria_tamper_finding([
        _finding("blocking", True, "requirement C.1 (#141)", "curable one"),
        _finding("residual", True, "requirement D.3 (#142)", "cosmetic"),
    ]) is False


def test_the_recogniser_is_severity_blind_on_purpose():
    """A downgraded copy must not buy the cure pass back: the recogniser
    answers on the FACT, not on the severity the fact currently carries."""
    downgraded = findings.flagged_criteria_findings([_tampered_task(tampered=True)])
    downgraded[0]["severity"] = "residual"
    assert findings.any_criteria_tamper_finding(downgraded) is True


# 2. A real `_step_review()` over a flagged task: replan, never cure.


def test_a_tamper_rejection_never_takes_the_cure_pass_edge(tmp_path):
    root = tmp_path / "run"
    sup = _supervisor_tamper(root)
    sup._approach = 1
    atomic_write_json(sup.run.tasks_file,
                      {"version": 1, "tasks": [_tampered_task(tampered=True)]})

    outcome = _drive_review_over(sup)

    # the ordinary archive/replan edge, exactly as before the cure-pass edge
    # existed -- not `rejected_curable_pass_available`.
    assert outcome == ("advance", "replan", "rejected_approach_left")

    # no counter, no appended cure task, no event.
    assert sup.run.read_status().get("curePasses") is None
    assert [t for t in sup.run.read_tasks()["tasks"]
            if t.get("origin") == "review-cure"] == []
    assert _events(root, "approach.cure_pass") == []

    # the engine's own tamper finding really was in the channel this
    # decision was made over (i.e. the case is live, not vacuous).
    channel = json.loads(sup.run.findings_json_file.read_text())
    assert any(findings.CRITERIA_TAMPER_CLAUSE_MARKER in f.get("clause", "")
               for f in channel)


def test_the_gate_holds_even_with_a_cure_pass_still_unspent(tmp_path):
    """`curePasses[approach] == 0` is the cure pass's *other* precondition;
    this pins that a tamper rejection is refused the edge for its own
    reason, with the budget provably untouched before and after."""
    root = tmp_path / "run"
    sup = _supervisor_tamper(root)
    sup._approach = 1
    atomic_write_json(sup.run.tasks_file,
                      {"version": 1, "tasks": [_tampered_task(tampered=True)]})
    assert sup._cure_passes_for_approach(1) == 0

    assert sup._review_cure_pass_available(1) is False
    assert sup._cure_passes_for_approach(1) == 0


# 3. The paired control: the same fixture, no marker -> the cure pass fires.


def test_the_same_fixture_without_the_marker_does_take_the_cure_pass(tmp_path):
    root = tmp_path / "run"
    sup = _supervisor_tamper(root)
    sup._approach = 1
    atomic_write_json(sup.run.tasks_file,
                      {"version": 1, "tasks": [_tampered_task(tampered=False)]})

    outcome = _drive_review_over(sup)

    assert outcome == ("advance", "cure", "rejected_curable_pass_available")
    assert sup.run.read_status().get("curePasses") == {"1": 1}
    assert len([t for t in sup.run.read_tasks()["tasks"]
                if t.get("origin") == "review-cure"]) == 1
    assert len(_events(root, "approach.cure_pass")) == 1


# ---------------------------------------------------------------------------
# task 041 (PRD requirement C.4, #149; re-inserted by operator steer 003
# after task 016's cut of C.4 turned out to rest on a void 62-minutes-remain
# premise): "When the cure pass appends a cure task for a finding whose
# evidence names a `failed` task, every dependent of that failed task gets
# the cure task added to its `dependsOn` and the failed task removed."
# ---------------------------------------------------------------------------


def _plan_task(tid: str, *, status: str = "pending", depends_on=None) -> dict:
    return {
        "id": tid, "title": f"task {tid}", "successCriteria": "n/a",
        "status": status, "origin": "planner",
        "dependsOn": list(depends_on or []), "tier": "normal",
    }


def _run_with_tasks(root: Path, tasks: list) -> RunDir:
    root.mkdir(parents=True, exist_ok=True)
    run = RunDir(root=root)
    atomic_write_json(run.tasks_file, {"version": 1, "tasks": tasks})
    return run


def test_a_dependent_naming_only_the_failed_task_is_repointed_to_the_cure_task(tmp_path):
    tasks = [
        _plan_task("020", status="failed"),
        _plan_task("021", depends_on=["020"]),
    ]
    run = _run_with_tasks(tmp_path / "run", tasks)

    finding = _blocking_curable_finding(
        "requirement X (#149)",
        "task 020 failed its verification with a genuine gap")
    inserted = insert_cure_tasks(run, 1, [finding])
    assert len(inserted) == 1
    cure_id = inserted[0]

    data = run.read_tasks()
    dependent = next(t for t in data["tasks"] if t["id"] == "021")
    assert dependent["dependsOn"] == [cure_id]

    failed = next(t for t in data["tasks"] if t["id"] == "020")
    assert failed["status"] == "failed"  # the failed task itself is untouched


def test_a_dependent_naming_both_the_failed_task_and_another_task_keeps_the_other(tmp_path):
    tasks = [
        _plan_task("020", status="failed"),
        _plan_task("019"),
        _plan_task("022", depends_on=["020", "019"]),
    ]
    run = _run_with_tasks(tmp_path / "run", tasks)

    finding = _blocking_curable_finding("requirement X (#149)",
                                        "evidence naming task 020 by id")
    inserted = insert_cure_tasks(run, 1, [finding])
    cure_id = inserted[0]

    data = run.read_tasks()
    dependent = next(t for t in data["tasks"] if t["id"] == "022")
    assert set(dependent["dependsOn"]) == {cure_id, "019"}
    assert "020" not in dependent["dependsOn"]


def test_a_dependent_naming_neither_id_is_left_untouched(tmp_path):
    tasks = [
        _plan_task("020", status="failed"),
        _plan_task("018"),
        _plan_task("023", depends_on=["018"]),
    ]
    run = _run_with_tasks(tmp_path / "run", tasks)

    finding = _blocking_curable_finding("requirement X (#149)",
                                        "evidence naming task 020 by id")
    insert_cure_tasks(run, 1, [finding])

    data = run.read_tasks()
    dependent = next(t for t in data["tasks"] if t["id"] == "023")
    assert dependent["dependsOn"] == ["018"]


def test_evidence_naming_no_failed_task_leaves_every_dependsOn_untouched(tmp_path):
    tasks = [
        _plan_task("020", status="pending"),
        _plan_task("021", depends_on=["020"]),
    ]
    run = _run_with_tasks(tmp_path / "run", tasks)

    finding = _blocking_curable_finding("requirement X (#149)",
                                        "no failed task named here")
    insert_cure_tasks(run, 1, [finding])

    data = run.read_tasks()
    dependent = next(t for t in data["tasks"] if t["id"] == "021")
    assert dependent["dependsOn"] == ["020"]  # 020 is not failed, so untouched


def test_steer_md_documents_the_same_repointing_move_for_task_update():
    text = (REPO_ROOT / "src" / "ralphd" / "prompts" / "steer.md").read_text()
    assert "A cure inherits dependents" in text
    assert "task-update" in text.split("A cure inherits dependents")[1][:800]
