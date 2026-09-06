"""Task 041 (PRD requirement C.4, #149; re-inserted by operator steer 003
after task 016's cut of C.4 turned out to rest on a void 62-minutes-remain
premise): "When the cure pass appends a cure task for a finding whose
evidence names a `failed` task, every dependent of that failed task gets
the cure task added to its `dependsOn` and the failed task removed."

Exercises `state.insert_cure_tasks` directly (the same call the engine's
own review cure pass makes) across the three shapes the requirement's
"no doomed remainder" claim distinguishes:

* a dependent whose `dependsOn` names ONLY the failed task -- it must end
  up depending on ONLY the cure task;
* a dependent whose `dependsOn` names BOTH the failed task and an
  unrelated, still-pending task -- the failed id is removed, the cure id
  is added, the unrelated id survives;
* a dependent whose `dependsOn` names NEITHER id -- left byte-for-byte
  untouched.

Also pins that `src/ralphd/prompts/steer.md` documents the identical move
for the steer hat's own `task-update`.
"""

from __future__ import annotations

from pathlib import Path

from ralphd.engine.state import RunDir, atomic_write_json, insert_cure_tasks

REPO_ROOT = Path(__file__).resolve().parents[1]


def _finding(clause: str, evidence: str) -> dict:
    return {"severity": "blocking", "curable": True, "clause": clause,
            "evidence": evidence}


def _task(tid: str, *, status: str = "pending", depends_on=None) -> dict:
    return {
        "id": tid, "title": f"task {tid}", "successCriteria": "n/a",
        "status": status, "origin": "planner",
        "dependsOn": list(depends_on or []), "tier": "normal",
    }


def _run(root: Path, tasks: list) -> RunDir:
    root.mkdir(parents=True, exist_ok=True)
    run = RunDir(root=root)
    atomic_write_json(run.tasks_file, {"version": 1, "tasks": tasks})
    return run


def test_a_dependent_naming_only_the_failed_task_is_repointed_to_the_cure_task(tmp_path):
    tasks = [
        _task("020", status="failed"),
        _task("021", depends_on=["020"]),
    ]
    run = _run(tmp_path / "run", tasks)

    finding = _finding("requirement X (#149)",
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
        _task("020", status="failed"),
        _task("019"),
        _task("022", depends_on=["020", "019"]),
    ]
    run = _run(tmp_path / "run", tasks)

    finding = _finding("requirement X (#149)", "evidence naming task 020 by id")
    inserted = insert_cure_tasks(run, 1, [finding])
    cure_id = inserted[0]

    data = run.read_tasks()
    dependent = next(t for t in data["tasks"] if t["id"] == "022")
    assert set(dependent["dependsOn"]) == {cure_id, "019"}
    assert "020" not in dependent["dependsOn"]


def test_a_dependent_naming_neither_id_is_left_untouched(tmp_path):
    tasks = [
        _task("020", status="failed"),
        _task("018"),
        _task("023", depends_on=["018"]),
    ]
    run = _run(tmp_path / "run", tasks)

    finding = _finding("requirement X (#149)", "evidence naming task 020 by id")
    insert_cure_tasks(run, 1, [finding])

    data = run.read_tasks()
    dependent = next(t for t in data["tasks"] if t["id"] == "023")
    assert dependent["dependsOn"] == ["018"]


def test_evidence_naming_no_failed_task_leaves_every_dependsOn_untouched(tmp_path):
    tasks = [
        _task("020", status="pending"),
        _task("021", depends_on=["020"]),
    ]
    run = _run(tmp_path / "run", tasks)

    finding = _finding("requirement X (#149)", "no failed task named here")
    insert_cure_tasks(run, 1, [finding])

    data = run.read_tasks()
    dependent = next(t for t in data["tasks"] if t["id"] == "021")
    assert dependent["dependsOn"] == ["020"]  # 020 is not failed, so untouched


def test_steer_md_documents_the_same_repointing_move_for_task_update():
    text = (REPO_ROOT / "src" / "ralphd" / "prompts" / "steer.md").read_text()
    assert "A cure inherits dependents" in text
    assert "task-update" in text.split("A cure inherits dependents")[1][:800]
