"""Task 032 (PRD requirement E.3, #167, steer 001): `insert_cure_tasks`
reads a finding's own optional `invalidates` -- zero or more in-checkout
paths the finding's cure invalidates (`engine/findings.py`'s
`FINDING_SCHEMA`) -- one path at a time, in `invalidates` order:

* an ordinary path with no existing task already tracking it gets one
  brand-new re-take task appended, `dependsOn` the cure task, whose
  `successCriteria` states the path is re-taken at the tree the cure
  leaves (never a bare repeat of the finding's own prose);
* a path that IS the plan's own final lane-evidence record -- a task
  whose `successCriteria` names the path and that itself `dependsOn`s
  the check-run verification step(s) it records -- instead re-opens
  that record task and its verification task(s) (`status` back to
  `"pending"`) and chains cure -> verification -> record by adding the
  cure task to each verification task's own `dependsOn` (the record
  task already depends on its verification task, so the chain is
  complete once that one edge is added). No new task is appended for
  that path.

Three groups of nodes:

* the per-path insertion itself, direct against `insert_cure_tasks`
  (`_make_run`, the same harness `tests/test_cure_pass.py`'s own
  collision-avoidance section already uses);
* the lane re-open chain, same harness, a fixture pair shaped like a
  "verify every lane" task depended on by a "record every lane" task;
* the emitted event's payload -- driving a real `LoopSupervisor.
  _step_review()` pass over a finding carrying `invalidates` of both
  shapes at once, and reading `approach.cure_pass`'s own `taskIds`
  back off `events.jsonl`.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from ralphd.engine import findings
from ralphd.engine.loop import LoopSupervisor
from ralphd.engine.runner import IterationResult
from ralphd.engine.state import RunDir, atomic_write_json, insert_cure_tasks

from tests.conftest import supervisor_factory


def _blocking_curable_finding(clause: str, evidence: str, **overrides) -> dict:
    payload = {"severity": "blocking", "curable": True, "clause": clause,
               "evidence": evidence}
    payload.update(overrides)
    return payload


def _make_run(root: Path, existing_tasks: list) -> RunDir:
    root.mkdir(parents=True, exist_ok=True)
    run = RunDir(root=root)
    atomic_write_json(run.tasks_file, {"version": 1, "tasks": existing_tasks})
    return run


def _task(tid: str, *, status: str = "pending", depends: list | None = None,
          criteria: str = "n/a") -> dict:
    return {"id": tid, "title": tid, "successCriteria": criteria,
            "status": status, "origin": "planner",
            "dependsOn": list(depends or []), "tier": "normal"}


# ---------------------------------------------------------------------------
# Per-path insertion: an ordinary invalidated path with no existing task.
# ---------------------------------------------------------------------------


def test_a_plain_invalidated_path_gets_one_new_retake_task(tmp_path):
    root = tmp_path / "run"
    run = _make_run(root, [])

    finding = _blocking_curable_finding(
        "requirement E.3 (#167)", "the record went stale",
        invalidates=["docs/testing.md"])
    inserted = insert_cure_tasks(run, 1, [finding])

    # the cure task, plus exactly one re-take task for the one path.
    assert len(inserted) == 2
    cure_id, retake_id = inserted

    data = run.read_tasks()
    tasks = {t["id"]: t for t in data["tasks"]}
    assert set(tasks) == {cure_id, retake_id}

    retake_task = tasks[retake_id]
    assert retake_task["status"] == "pending"
    assert retake_task["dependsOn"] == [cure_id]
    assert "docs/testing.md" in retake_task["successCriteria"]
    assert "re-taken" in retake_task["successCriteria"]
    assert retake_task["origin"] == "review-cure"


def test_multiple_paths_on_one_finding_each_get_their_own_retake_task(tmp_path):
    root = tmp_path / "run"
    run = _make_run(root, [])

    finding = _blocking_curable_finding(
        "requirement E.3 (#167)", "two records went stale",
        invalidates=["docs/testing.md", "docs/architecture.md"])
    inserted = insert_cure_tasks(run, 1, [finding])

    assert len(inserted) == 3
    cure_id = inserted[0]
    retake_ids = inserted[1:]
    assert len(set(retake_ids)) == 2

    data = run.read_tasks()
    tasks = {t["id"]: t for t in data["tasks"]}
    named_paths = {tasks[rid]["successCriteria"].split(" is ")[0]
                  for rid in retake_ids}
    assert named_paths == {"docs/testing.md", "docs/architecture.md"}
    for rid in retake_ids:
        assert tasks[rid]["dependsOn"] == [cure_id]


def test_a_finding_with_no_invalidates_leaves_the_prior_behaviour_unchanged(tmp_path):
    root = tmp_path / "run"
    run = _make_run(root, [])

    finding = _blocking_curable_finding("requirement E (#113)", "no paths named")
    inserted = insert_cure_tasks(run, 1, [finding])

    assert inserted == ["cure-01-01"]


# ---------------------------------------------------------------------------
# The lane re-open chain: a path that IS the plan's own final
# lane-evidence record.
# ---------------------------------------------------------------------------

RECORD_PATH = "artifacts/reports/v0.20-lanes.md"


def _lane_fixture_tasks(record_status: str = "completed",
                        verify_status: str = "validated") -> list:
    return [
        _task("057", status=verify_status, criteria="verify every lane"),
        _task("058", status=record_status, depends=["057"],
              criteria=f"One commit adds {RECORD_PATH} naming the shipped sha."),
    ]


def test_a_finding_against_the_final_lane_evidence_record_reopens_the_chain(tmp_path):
    root = tmp_path / "run"
    run = _make_run(root, _lane_fixture_tasks())

    finding = _blocking_curable_finding(
        "the shipped sha's lanes moved", "a later commit landed after the record",
        invalidates=[RECORD_PATH])
    inserted = insert_cure_tasks(run, 1, [finding])

    # the cure task, plus the reopened verification (057) and record (058)
    # ids -- no brand-new task appended for this path.
    assert len(inserted) == 3
    cure_id = inserted[0]
    assert set(inserted[1:]) == {"057", "058"}

    data = run.read_tasks()
    tasks = {t["id"]: t for t in data["tasks"]}
    assert len(tasks) == 3  # 057, 058, and the one new cure task -- no retake task

    verify = tasks["057"]
    record = tasks["058"]
    assert verify["status"] == "pending"
    assert record["status"] == "pending"
    # chained cure -> verification -> record: the cure task joins 057's own
    # dependsOn; 058 already depended on 057, untouched.
    assert cure_id in verify["dependsOn"]
    assert record["dependsOn"] == ["057"]


def test_the_chain_reopens_regardless_of_the_tasks_prior_terminal_status(tmp_path):
    root = tmp_path / "run"
    run = _make_run(root, _lane_fixture_tasks(record_status="validated",
                                              verify_status="validated"))

    finding = _blocking_curable_finding(
        "the shipped sha's lanes moved", "a later commit landed after the record",
        invalidates=[RECORD_PATH])
    insert_cure_tasks(run, 1, [finding])

    data = run.read_tasks()
    tasks = {t["id"]: t for t in data["tasks"]}
    assert tasks["057"]["status"] == "pending"
    assert tasks["058"]["status"] == "pending"


def test_an_unrelated_path_alongside_the_record_path_still_gets_its_own_task(tmp_path):
    root = tmp_path / "run"
    run = _make_run(root, _lane_fixture_tasks())

    finding = _blocking_curable_finding(
        "two things went stale", "the lanes moved and the suite budget too",
        invalidates=[RECORD_PATH, "artifacts/reports/v0.20-suite-budget.md"])
    inserted = insert_cure_tasks(run, 1, [finding])

    cure_id = inserted[0]
    assert set(inserted[1:]) & {"057", "058"} == {"057", "058"}
    remaining = [i for i in inserted[1:] if i not in ("057", "058")]
    assert len(remaining) == 1
    retake_id = remaining[0]

    data = run.read_tasks()
    tasks = {t["id"]: t for t in data["tasks"]}
    assert "artifacts/reports/v0.20-suite-budget.md" in tasks[retake_id]["successCriteria"]
    assert tasks[retake_id]["dependsOn"] == [cure_id]


# ---------------------------------------------------------------------------
# The emitted event's payload: a real cure pass through `_step_review()`.
# ---------------------------------------------------------------------------

_supervisor = supervisor_factory(run_id="task032-cure-invalidates", cfg_defaults=False,
                                 status={"state": "running"},
                                 prd="# task 032 fixture PRD\n\nDo the thing.\n",
                                 max_approaches=3)


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


def test_the_emitted_event_lists_every_id_the_pass_inserted_or_reopened(tmp_path):
    root = tmp_path / "run"
    sup = _supervisor(root)
    sup._approach = 1
    atomic_write_json(sup.run.tasks_file,
                      {"version": 1, "tasks": _lane_fixture_tasks()})

    finding = _blocking_curable_finding(
        "requirement E.3 (#167)", "the lanes moved and a doc went stale",
        invalidates=[RECORD_PATH, "docs/testing.md"])
    findings.write_findings_json(sup.run.findings_json_file, [finding])

    async def _review_rejects(prompt, transcript, **kw):
        return _result(json.dumps({
            "verdict": "rejected",
            "reason": "one blocking finding, curable, invalidating two paths",
        }))

    sup.runner.run = _review_rejects  # type: ignore[method-assign]

    outcome = asyncio.run(sup._step_review())
    assert outcome == ("advance", "cure", "rejected_curable_pass_available")

    events = _events(root, "approach.cure_pass")
    assert len(events) == 1
    task_ids = events[0]["taskIds"]

    data = sup.run.read_tasks()
    tasks = {t["id"]: t for t in data["tasks"]}
    cure_ids = [tid for tid in task_ids if tid.startswith("cure-")]
    assert len(cure_ids) == 1
    cure_id = cure_ids[0]

    # the reopened verification/record pair, and the brand-new retake task
    # for the unrelated path, are all named in the event's own payload.
    assert "057" in task_ids
    assert "058" in task_ids
    retake_ids = [tid for tid in task_ids
                 if tid not in (cure_id, "057", "058")]
    assert len(retake_ids) == 1
    assert "docs/testing.md" in tasks[retake_ids[0]]["successCriteria"]

    assert tasks["057"]["status"] == "pending"
    assert tasks["058"]["status"] == "pending"
    assert cure_id in tasks["057"]["dependsOn"]
    assert tasks["058"]["dependsOn"] == ["057"]
