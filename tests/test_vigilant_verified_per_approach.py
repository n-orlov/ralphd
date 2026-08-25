"""The vigilant verified-task record is per-approach (issue #29) -- now folded
into the task record itself rather than a separate `vigilant-verified.json`
sidecar (task 020, requirement F bullet 4).

Every approach's planning pass rewrites tasks.json renumbering its tasks from
"001". Pre-0.9, the record of which tasks already got a passing verify
iteration was a flat list of bare ids in a side file, so approach 2's task
"001" could collide with approach 1's already-verified task "001":
`pending_verify` computed empty on every worker iteration and per-task
verification was silently skipped for every approach after the first --
observed on a real run as 17 consecutive worker iterations with no verify at
all. Under the fold there is no second file to collide: "validated" (or
"awaiting-validation") lives on the task dict inside tasks.json, and tasks.json
itself is a fresh document every approach, renumbered from "001" with no
`status` carried over -- so the exact collision #29 fixed is now structurally
impossible rather than merely namespaced away.

`RunDir.migrate_legacy_vigilant_verified()` is the one place the old
approach-namespaced key shape (`"<approach>:<taskId>"`) still matters: folding
a pre-0.9 run dir's sidecar into a resumed run's task record must preserve
the exact same non-collision property the sidecar's own namespacing gave it.
"""

from __future__ import annotations

import json

from ralphd.engine.state import RunDir, atomic_write_json


def _plan(*ids_statuses: tuple[str, str]) -> dict:
    return {"version": 1,
            "tasks": [{"id": i, "title": f"task {i}", "status": s,
                       "successCriteria": f"{i} works"}
                      for i, s in ids_statuses]}


def test_a_replans_renumbered_task_001_does_not_inherit_the_earlier_approachs_validated_task_001(tmp_path):
    """The structural #29 proof: approach 1's task "001" reaches `validated`
    (the vigilant terminal, task 020's replacement for a sidecar entry);
    planning then replans into approach 2, overwriting tasks.json with a
    fresh, renumbered plan whose task "001" is a completely different dict.
    It must read as `pending`, never `validated` -- there is no sidecar left
    to smuggle the earlier approach's verdict across the boundary."""
    run = RunDir(tmp_path)
    run.update_status(approach=1)
    run.write_tasks(_plan(("001", "validated"), ("002", "validated")))
    assert run.read_tasks()["tasks"][0]["status"] == "validated"

    # A replan's planning pass: same run dir, approach bumped, tasks.json
    # entirely rewritten (task ids renumbered from "001" again, this time
    # genuinely different tasks).
    run.update_status(approach=2)
    run.write_tasks(_plan(("001", "pending"), ("002", "pending")))

    reread = run.read_tasks()
    assert reread["tasks"][0]["id"] == "001"
    assert reread["tasks"][0]["status"] == "pending", (
        "approach 2's fresh task 001 must not read as approach 1's "
        "already-validated task 001")


def test_migrate_legacy_sidecar_folds_verified_ids_into_validated_status(tmp_path):
    run = RunDir(tmp_path)
    run.update_status(approach=1)
    atomic_write_json(run.vigilant_verified_file, ["1:001", "1:002"])
    run.write_tasks(_plan(("001", "completed"), ("002", "completed")))

    run.migrate_legacy_vigilant_verified()

    tasks = {t["id"]: t["status"] for t in run.read_tasks()["tasks"]}
    assert tasks == {"001": "validated", "002": "validated"}
    assert not run.vigilant_verified_file.exists(), (
        "the sidecar is folded, one-way -- it must not remain on disk "
        "for a second migration to re-read")


def test_migrate_leaves_an_unverified_completed_task_at_awaiting_validation(tmp_path):
    """Guard 4 through the migration boundary: a task that was `completed`
    by a worker but never reached a passing verify iteration before the
    engine that wrote the legacy sidecar crashed must still read as needing
    verification after the fold -- not silently promoted to `validated`."""
    run = RunDir(tmp_path)
    run.update_status(approach=1)
    atomic_write_json(run.vigilant_verified_file, ["1:001"])  # 002 never verified
    run.write_tasks(_plan(("001", "completed"), ("002", "completed")))

    run.migrate_legacy_vigilant_verified()

    tasks = {t["id"]: t["status"] for t in run.read_tasks()["tasks"]}
    assert tasks == {"001": "validated", "002": "awaiting-validation"}


def test_migrate_does_not_let_an_earlier_approachs_key_verify_this_approachs_task(tmp_path):
    """The exact #29 shape, replayed through the migration path itself:
    the sidecar says approach 1's task "001" was verified; the CURRENT
    approach is 2, and its own (renumbered, unrelated) task "001" is
    `completed` but was never verified. The migration must not read
    approach 1's key as covering it."""
    run = RunDir(tmp_path)
    atomic_write_json(run.vigilant_verified_file, ["1:001"])
    run.update_status(approach=2)
    run.write_tasks(_plan(("001", "completed")))

    run.migrate_legacy_vigilant_verified()

    assert run.read_tasks()["tasks"][0]["status"] == "awaiting-validation"


def test_migrate_reads_unprefixed_legacy_entries_as_approach_1(tmp_path):
    """A run dir whose sidecar predates even #29's own namespacing fix
    (bare ids, no "1:" prefix) must still fold correctly for approach 1."""
    run = RunDir(tmp_path)
    atomic_write_json(run.vigilant_verified_file, ["001", "002"])
    run.update_status(approach=1)
    run.write_tasks(_plan(("001", "completed"), ("002", "completed")))

    run.migrate_legacy_vigilant_verified()

    tasks = {t["id"]: t["status"] for t in run.read_tasks()["tasks"]}
    assert tasks == {"001": "validated", "002": "validated"}


def test_migrate_is_a_no_op_when_no_legacy_sidecar_exists(tmp_path):
    run = RunDir(tmp_path)
    run.write_tasks(_plan(("001", "pending")))
    run.migrate_legacy_vigilant_verified()  # must not raise or touch tasks.json
    assert run.read_tasks()["tasks"][0]["status"] == "pending"


def test_migrate_is_idempotent_a_second_call_is_a_pure_no_op(tmp_path):
    run = RunDir(tmp_path)
    run.update_status(approach=1)
    atomic_write_json(run.vigilant_verified_file, ["1:001"])
    run.write_tasks(_plan(("001", "completed")))

    run.migrate_legacy_vigilant_verified()
    once = json.dumps(run.read_tasks(), sort_keys=True)
    run.migrate_legacy_vigilant_verified()  # sidecar is already gone
    assert json.dumps(run.read_tasks(), sort_keys=True) == once
