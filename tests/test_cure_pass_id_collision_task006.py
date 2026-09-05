"""Task 006 (PRD requirement C.1, finding 4): `state.insert_cure_tasks`
must append exactly one cure task per entry of `blocking`, even when the
deterministic `cure-{approach:02d}-{index:02d}` id it would normally use
is already present in `tasks.json`. Before this task the collision was
handled by silently SKIPPING that entry -- a genuinely blocking finding
could end up with no cure task at all, and `insert_cure_tasks` would
return fewer ids than findings with no error telling anyone why. Now a
collision picks a fresh, still-unused id instead.

Reproduces the review's exact scenario: a `tasks.json` already containing
id `cure-01-01`, then `insert_cure_tasks(run, 1, [one blocking finding])`.
"""

from __future__ import annotations

from pathlib import Path

from ralphd.engine.state import RunDir, atomic_write_json, insert_cure_tasks


def _finding(clause: str, evidence: str) -> dict:
    return {"severity": "blocking", "curable": True, "clause": clause,
            "evidence": evidence}


def _run(root: Path, existing_ids: list[str]) -> RunDir:
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
    run = _run(root, ["cure-01-01"])

    finding = _finding("requirement B.1 (#111)", "concrete evidence text")
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
    run = _run(root, ["cure-01-01", "cure-01-01-2"])

    finding = _finding("requirement D (#112)", "another concrete gap")
    inserted = insert_cure_tasks(run, 1, [finding])

    assert len(inserted) == 1
    new_id = inserted[0]
    assert new_id not in ("cure-01-01", "cure-01-01-2")

    data = run.read_tasks()
    ids = [t["id"] for t in data["tasks"]]
    assert ids.count(new_id) == 1


def test_no_collision_still_uses_the_plain_deterministic_id(tmp_path):
    root = tmp_path / "run"
    run = _run(root, [])

    finding = _finding("requirement E (#113)", "yet more evidence")
    inserted = insert_cure_tasks(run, 1, [finding])

    assert inserted == ["cure-01-01"]


def test_one_task_appended_per_blocking_finding_even_with_a_collision(tmp_path):
    root = tmp_path / "run"
    run = _run(root, ["cure-01-02"])  # collides with the SECOND finding's id

    findings_list = [
        _finding("requirement F.1 (#114)", "first evidence"),
        _finding("requirement F.2 (#115)", "second evidence"),
    ]
    inserted = insert_cure_tasks(run, 1, findings_list)

    assert len(inserted) == 2
    assert inserted[0] == "cure-01-01"
    assert inserted[1] != "cure-01-02"

    data = run.read_tasks()
    cure_tasks = [t for t in data["tasks"] if t.get("origin") == "review-cure"]
    assert len(cure_tasks) == 2
