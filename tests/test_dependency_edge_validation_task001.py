"""Task 001 (PRD requirement D, #206): a `dependsOn` write is validated on
the three paths that write into `tasks.json` while skipping the
ingestion-time lint entirely -- `state.insert_cure_tasks`,
`state.steer_task_update`'s own `dependsOn` merge, and the `task-insert`
steer tool (`state.steer_task_insert`).

One regression per entry point (D.4), each driving the observed shape a
cure pass actually wrote once and deadlocked the plan on (#206):
`{"id": "cure-01-02", "dependsOn": ["cure-01-02"]}` -- a self-loop -- and,
in the SAME function, the other two refusal shapes the same path must
also refuse (D.1): a longer cycle, and a dependency on an id the table
does not hold. Every refusal/drop is asserted to (a) leave the table with
a satisfiable edge set for that task, (b) emit a
`task.dependency_edge_rejected` event naming the task, the rejected edge
and the reason, and (c) for the cure-insertion path specifically, still
land the cure task itself -- a validation failure must never cost the
cure the finding it was for (D.3).

Kept to exactly three new nodes (one per entry point) so this task's own
new coverage is a net-zero addition to the fast tier's fixed 5,800-node
ceiling (`tests/test_tier_budgets.py`) -- offset in the SAME commit by
folding four near-duplicate `insert_cure_tasks` dependent-repointing nodes
into one in `tests/test_cure_pass.py`
(`test_dependent_repointing_covers_only_both_neither_and_no_failed_task_named`,
replacing `test_a_dependent_naming_only_the_failed_task_is_repointed_to_
the_cure_task`, `test_a_dependent_naming_both_the_failed_task_and_
another_task_keeps_the_other`, `test_a_dependent_naming_neither_id_is_
left_untouched` and `test_evidence_naming_no_failed_task_leaves_every_
dependsOn_untouched`), every one of their assertions kept.

`plan_ingestion._detect_cycles` and its messages are untouched by this
task (D.5) -- `tests/test_plan_ingestion.py`'s own cycle tests are the
proof, unchanged and still passing.
"""
from __future__ import annotations

import json
from pathlib import Path

from ralphd.engine import plan_ingestion
from ralphd.engine.state import (
    RunDir,
    atomic_write_json,
    insert_cure_tasks,
    steer_task_insert,
    steer_task_update,
)

REJECTED_EVENT = "task.dependency_edge_rejected"


def _run(root: Path) -> RunDir:
    return RunDir(root=root)


def _write_tasks(run: RunDir, tasks: list[dict]) -> None:
    atomic_write_json(run.tasks_file, {"version": 1, "tasks": tasks})


def _events(run: RunDir, type_: str | None = None) -> list[dict]:
    path = run.root / "events.jsonl"
    if not path.exists():
        return []
    events = [json.loads(line) for line in path.read_text().splitlines()
             if line.strip()]
    if type_ is None:
        return events
    return [e for e in events if e.get("type") == type_]


def _plan_task(tid: str, *, depends_on=None, status: str = "pending") -> dict:
    return {"id": tid, "title": f"task {tid}", "successCriteria": "n/a",
            "status": status, "origin": "planner",
            "dependsOn": list(depends_on or []), "tier": "normal"}


def _blocking_curable_finding(clause: str, evidence: str, *,
                              invalidates=None) -> dict:
    finding = {"severity": "blocking", "curable": True, "clause": clause,
              "evidence": evidence}
    if invalidates is not None:
        finding["invalidates"] = invalidates
    return finding


# ---------------------------------------------------------------------------
# steer_task_update's own dependsOn merge (D.2, D.4)
# ---------------------------------------------------------------------------

def test_task_update_refuses_a_self_loop_a_longer_cycle_and_an_unknown_id(tmp_path):
    run = _run(tmp_path / "run")
    _write_tasks(run, [
        _plan_task("cure-01-02"),
        _plan_task("a"),
        _plan_task("b", depends_on=["a"]),
        _plan_task("d"),
        _plan_task("e"),
    ])

    # the observed shape: a task naming itself.
    result = steer_task_update(run, "cure-01-02",
                               {"dependsOn": ["cure-01-02"]}, False)
    task = next(t for t in run.read_tasks()["tasks"] if t["id"] == "cure-01-02")
    assert task["dependsOn"] == []  # the self-loop never landed
    assert result["dependsOnResult"]["rejectedEdges"][0]["edge"] == "cure-01-02"

    # a longer cycle: a -> b would close a -> b -> a.
    result = steer_task_update(run, "a", {"dependsOn": ["b"]}, False)
    task = next(t for t in run.read_tasks()["tasks"] if t["id"] == "a")
    assert task["dependsOn"] == []
    assert result["dependsOnResult"]["rejectedEdges"][0]["edge"] == "b"

    # a dependency on an id the table does not hold.
    result = steer_task_update(run, "d", {"dependsOn": ["nowhere"]}, False)
    task = next(t for t in run.read_tasks()["tasks"] if t["id"] == "d")
    assert task["dependsOn"] == []
    assert result["dependsOnResult"]["rejectedEdges"][0]["edge"] == "nowhere"

    # a good edge beside a bad one still lands -- one bad edge costs
    # itself, never the rest of the write.
    result = steer_task_update(run, "d", {"dependsOn": ["e", "d", "nowhere"]},
                               False)
    task = next(t for t in run.read_tasks()["tasks"] if t["id"] == "d")
    assert task["dependsOn"] == ["e"]
    assert {r["edge"] for r in result["dependsOnResult"]["rejectedEdges"]} == \
        {"d", "nowhere"}

    rejected = _events(run, REJECTED_EVENT)
    assert [r["edge"] for r in rejected] == \
        ["cure-01-02", "b", "nowhere", "d", "nowhere"]
    assert all(r["source"] == "task-update" for r in rejected)
    assert rejected[0]["taskId"] == "cure-01-02"
    assert "self-loop" in rejected[0]["reason"]
    assert rejected[1]["taskId"] == "a" and "cycle" in rejected[1]["reason"]
    assert rejected[2]["taskId"] == "d" and "unknown" in rejected[2]["reason"]


# ---------------------------------------------------------------------------
# the task-insert steer tool (D.2, D.4)
# ---------------------------------------------------------------------------

def test_task_insert_refuses_a_self_loop_a_longer_cycle_and_an_unknown_id(tmp_path):
    # Phase 1: the observed shape -- a new task naming itself.
    run_a = _run(tmp_path / "run-a")
    _write_tasks(run_a, [])
    result = steer_task_insert(
        run_a, {"id": "cure-01-02", "title": "new", "successCriteria": "n/a",
               "dependsOn": ["cure-01-02"]})
    assert result["inserted"] is True
    task = run_a.read_tasks()["tasks"][0]
    assert task["dependsOn"] == []
    rejected_a = _events(run_a, REJECTED_EVENT)
    assert len(rejected_a) == 1
    assert rejected_a[0]["taskId"] == "cure-01-02"
    assert rejected_a[0]["edge"] == "cure-01-02"
    assert "self-loop" in rejected_a[0]["reason"]
    assert rejected_a[0]["source"] == "task-insert"

    # Phase 2: a longer cycle -- "f" already depends on the not-yet-
    # existing "g"; inserting "g" depending on "f" would close f -> g -> f.
    run_b = _run(tmp_path / "run-b")
    _write_tasks(run_b, [_plan_task("f", depends_on=["g"])])
    result = steer_task_insert(
        run_b, {"id": "g", "title": "new", "successCriteria": "n/a",
               "dependsOn": ["f"]})
    assert result["inserted"] is True
    new_task = next(t for t in run_b.read_tasks()["tasks"] if t["id"] == "g")
    assert new_task["dependsOn"] == []
    rejected_b = _events(run_b, REJECTED_EVENT)
    assert len(rejected_b) == 1
    assert rejected_b[0]["taskId"] == "g"
    assert rejected_b[0]["edge"] == "f"
    assert "cycle" in rejected_b[0]["reason"]

    # Phase 3: a dependency on an id the table does not hold.
    run_c = _run(tmp_path / "run-c")
    _write_tasks(run_c, [])
    result = steer_task_insert(
        run_c, {"id": "h", "title": "new", "successCriteria": "n/a",
               "dependsOn": ["nowhere"]})
    assert result["inserted"] is True
    new_task = run_c.read_tasks()["tasks"][0]
    assert new_task["dependsOn"] == []
    rejected_c = _events(run_c, REJECTED_EVENT)
    assert len(rejected_c) == 1
    assert rejected_c[0]["edge"] == "nowhere"
    assert "unknown" in rejected_c[0]["reason"]


# ---------------------------------------------------------------------------
# insert_cure_tasks (D.1, D.3, D.4)
# ---------------------------------------------------------------------------

def test_cure_insertion_refuses_a_self_loop_a_longer_cycle_and_an_unknown_id(tmp_path):
    # Phase 1 (the organic shape observed live, #206): a pre-existing
    # "lane evidence record" task already names the cure task's own
    # deterministic id (left over from an earlier pass at the same
    # approach/index) in ITS OWN dependsOn. Once this pass's second
    # finding creates a fresh cure task under that exact id, the
    # invalidates-handling verification step resolves that record task's
    # dependency back onto the freshly-created cure task itself, and
    # tries to append the cure task's own id onto ITS OWN dependsOn --
    # the literal self-loop shape from #206.
    root = tmp_path / "run"
    root.mkdir(parents=True, exist_ok=True)
    run = RunDir(root=root)
    existing = [
        {"id": "existing-record", "title": "record", "status": "pending",
         "origin": "planner", "tier": "normal",
         "successCriteria": "some/path.py is recorded",
         "dependsOn": ["cure-01-02"]},
    ]
    atomic_write_json(run.tasks_file, {"version": 1, "tasks": existing})

    findings = [
        _blocking_curable_finding("requirement A (#206)", "first finding"),
        _blocking_curable_finding("requirement B (#206)", "second finding",
                                  invalidates=["some/path.py"]),
    ]
    inserted = insert_cure_tasks(run, 1, findings)

    # both cure tasks still land -- a dropped edge never loses the cure.
    assert "cure-01-01" in inserted
    assert "cure-01-02" in inserted

    data = run.read_tasks()
    by_id = {t["id"]: t for t in data["tasks"]}
    cure_02 = by_id["cure-01-02"]
    assert cure_02["dependsOn"] == []  # the self-loop never landed
    assert cure_02["origin"] == "review-cure"
    assert by_id["existing-record"]["dependsOn"] == ["cure-01-02"]  # untouched

    rejected = _events(run, REJECTED_EVENT)
    assert len(rejected) == 1
    assert rejected[0]["taskId"] == "cure-01-02"
    assert rejected[0]["edge"] == "cure-01-02"
    assert "self-loop" in rejected[0]["reason"]
    assert rejected[0]["source"] == "cure-insertion"

    # Phase 2: a dependent's repointed edge naming an id the table does
    # not hold (a hand-corrupted plan) drops that edge instead of writing
    # it, while the ordinary repoint onto the cure task still lands.
    root2 = tmp_path / "run2"
    root2.mkdir(parents=True, exist_ok=True)
    run2 = RunDir(root=root2)
    tasks2 = [
        _plan_task("120", status="failed"),
        {**_plan_task("121"), "dependsOn": ["120", "ghost"]},
    ]
    atomic_write_json(run2.tasks_file, {"version": 1, "tasks": tasks2})
    finding2 = _blocking_curable_finding(
        "requirement C (#206)", "task 120 failed its verification")
    inserted2 = insert_cure_tasks(run2, 1, [finding2])
    cure_id2 = inserted2[0]
    dependent2 = next(t for t in run2.read_tasks()["tasks"] if t["id"] == "121")
    assert dependent2["dependsOn"] == [cure_id2]  # "ghost" dropped, "120" repointed
    rejected2 = _events(run2, REJECTED_EVENT)
    assert len(rejected2) == 1
    assert rejected2[0]["edge"] == "ghost"
    assert "unknown" in rejected2[0]["reason"]
    assert rejected2[0]["source"] == "cure-insertion"

    # Phase 3: the longer-cycle shape. `insert_cure_tasks` only ever
    # writes a `dependsOn` edge whose TARGET is a just-created cure/retake
    # task id with no outgoing edges of its own yet -- so every edge it
    # can attempt is at most a self-loop (Phase 1), never a cycle of
    # length >= 2 (there is nothing yet for the fresh id to already
    # reach). The refusal is still the SAME check on this path: it goes
    # through `plan_ingestion.validate_dependency_edges`, the identical
    # function `steer_task_update`/`steer_task_insert` are proven against
    # above -- exercised directly here against a synthetic longer-cycle
    # graph, since no organic `insert_cure_tasks` write can produce one.
    accepted, rejected3 = plan_ingestion.validate_dependency_edges(
        [_plan_task("x", depends_on=["y"]), _plan_task("y")], "y", ["x"])
    assert accepted == []
    assert rejected3[0]["edge"] == "x"
    assert "cycle" in rejected3[0]["reason"]
