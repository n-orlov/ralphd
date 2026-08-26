"""Task 005 (requirement J, #75): `RunDir.migrate_legacy_vigilant_verified()`
must not lose an approach's verifications when the approach counter has
moved on ahead of the tasks.json it is still describing.

Pre-fix, the sidecar's approach-namespaced entries (`"<approach>:<taskId>"`,
task 020/#29) were matched ONLY against `current_approach()`'s own
namespace. That is safe exactly as long as the live tasks.json is a fresh
replan for the current approach -- which it is NOT on a run dir where the
approach counter advanced without the archive-then-replan that normally
goes with it (#72's bug, fixed by tasks 002-004; a future bug shaped like
it could reproduce the same symptom). Observed on `selfdev-v09-fsm-loop`:
`status.json.approach == 4` while tasks.json still held 20 tasks
`"completed"` and verified under approach 3, unrenumbered. Matching only
approach 4's (empty) namespace matched nothing, demoted all 20 to
`"awaiting-validation"`, and then unlinked the sidecar anyway -- the
sidecar was the only record of the lost verifications, and the migration is
documented as one-way.

The fix (`RunDir._resolve_legacy_vigilant_ids`, `engine/state.py`): fall
back to another approach's namespaced entries for a task id only when that
approach's own ARCHIVED snapshot (`archived_approach_dir`) shows the
identical task dict already `"completed"` -- i.e. the live tasks.json is
still, unrenumbered, that earlier approach's plan. This keeps #29's
collision guard intact (an approach that was never archived, or whose
archived copy of the id differs, contributes nothing -- pinned by
`tests/test_vigilant_verified_per_approach.py`'s
`test_migrate_does_not_let_an_earlier_approachs_key_verify_this_approachs_task`,
still passing unmodified).

Because the fallback is a heuristic (never proven beyond an
id/title/successCriteria/status comparison against the archived doc), using
it for ANY task -- or resolving zero of a nonempty sidecar's names against
a nonempty set of completed tasks -- withholds the sidecar delete rather
than destroying evidence the fold could not be certain it accounted for.
"""

from __future__ import annotations

from ralphd.engine.state import (
    RunDir,
    archived_approach_dir,
    atomic_write_json,
    read_events,
)


def _plan(*ids_statuses: tuple[str, str]) -> dict:
    return {"version": 1,
            "tasks": [{"id": i, "title": f"task {i}", "status": s,
                       "successCriteria": f"{i} works"}
                      for i, s in ids_statuses]}


def _archive(run: RunDir, approach: int, plan: dict) -> None:
    """Mimic the loop supervisor's `_archive_approach()` effect for this
    test's purposes: a frozen, byte-identical copy of `plan` under
    `approaches/NN/tasks.json` -- the artifact requirement J's fix shape
    names ("the archived per-approach records already carry that
    structure")."""
    adir = archived_approach_dir(run.root, approach)
    adir.mkdir(parents=True, exist_ok=True)
    atomic_write_json(adir / "tasks.json", plan)


def _twenty_task_ids() -> list[str]:
    return [f"{i:03d}" for i in range(1, 21)]


def _reproduction_plan() -> dict:
    return _plan(*[(i, "completed") for i in _twenty_task_ids()])


# ---------------------------------------------------------------------------
# 1. The selfdev-v09-fsm-loop reproduction: approach advanced past what the
#    sidecar and the still-unrenumbered tasks.json both agree on.
# ---------------------------------------------------------------------------

def test_approach_advanced_past_sidecar_does_not_lose_the_verifications(tmp_path):
    """20 tasks `completed`, verified under approach 3
    (`"3:001"`..`"3:020"`), `status.json.approach` sitting at 4 -- exactly
    `selfdev-v09-fsm-loop`'s shape. None of the 20 must be demoted to
    `awaiting-validation`, and the sidecar must not be destroyed: the fold
    only got there via the cross-approach fallback (a heuristic), so the
    migration withholds the delete rather than throw away the one durable
    record of what it just had to infer."""
    run = RunDir(tmp_path)
    plan = _reproduction_plan()
    # tasks.json was never rewritten for approach 4 (the #72-shaped bug):
    # it is still, byte-for-byte, what approach 3 archived.
    _archive(run, 3, plan)
    run.write_tasks(plan)
    run.update_status(approach=4)
    atomic_write_json(run.vigilant_verified_file,
                       [f"3:{i}" for i in _twenty_task_ids()])

    run.migrate_legacy_vigilant_verified()

    statuses = {t["id"]: t["status"] for t in run.read_tasks()["tasks"]}
    assert set(statuses.values()) == {"validated"}, (
        "every one of the 20 tasks verified under approach 3 must read "
        "validated, not demoted to awaiting-validation, once the approach "
        "counter has moved on to 4")
    assert run.vigilant_verified_file.exists(), (
        "the fold here only succeeded through the cross-approach fallback "
        "(a heuristic), so the sidecar -- the one durable record of what "
        "was actually verified -- must not be destroyed")

    events = read_events(tmp_path, types=["vigilant_verified_migration"])
    assert len(events) == 1
    assert events[0]["usedFallback"] is True
    assert events[0]["sidecarKept"] is True
    assert events[0]["matchedCount"] == 20
    assert events[0]["completedTaskCount"] == 20


def test_approach_advanced_but_archive_missing_does_not_fabricate_a_match(tmp_path):
    """The other half of #29's guard, replayed against the NEW fallback
    path: if approach 3 was never actually archived (so there is no
    trustworthy evidence the live tasks.json is still approach 3's
    unrenumbered plan), the fallback must not fire just because the
    sidecar happens to name the same ids -- the tasks stay
    `awaiting-validation`, same as the pre-fix behaviour, and the sidecar
    is kept precisely because the fold matched nothing while there was
    something (20 completed tasks) it could have matched."""
    run = RunDir(tmp_path)
    plan = _reproduction_plan()
    run.write_tasks(plan)
    run.update_status(approach=4)
    atomic_write_json(run.vigilant_verified_file,
                       [f"3:{i}" for i in _twenty_task_ids()])
    # No `_archive(run, 3, plan)` call -- approach 3 was never archived.

    run.migrate_legacy_vigilant_verified()

    statuses = {t["id"]: t["status"] for t in run.read_tasks()["tasks"]}
    assert set(statuses.values()) == {"awaiting-validation"}
    assert run.vigilant_verified_file.exists()

    events = read_events(tmp_path, types=["vigilant_verified_migration"])
    assert events[-1]["usedFallback"] is False
    assert events[-1]["matchedCount"] == 0
    assert events[-1]["sidecarKept"] is True


# ---------------------------------------------------------------------------
# 2. The ordinary same-approach fold still works, and now cleans up the
#    sidecar (the "legitimately nothing left to be uncertain about" case).
# ---------------------------------------------------------------------------

def test_ordinary_same_approach_fold_still_validates_and_cleans_up(tmp_path):
    run = RunDir(tmp_path)
    run.update_status(approach=1)
    atomic_write_json(run.vigilant_verified_file, ["1:001", "1:002"])
    run.write_tasks(_plan(("001", "completed"), ("002", "completed")))

    run.migrate_legacy_vigilant_verified()

    tasks = {t["id"]: t["status"] for t in run.read_tasks()["tasks"]}
    assert tasks == {"001": "validated", "002": "validated"}
    assert not run.vigilant_verified_file.exists(), (
        "an ordinary current-approach-only fold needed no fallback and "
        "matched everything the sidecar named -- safe to delete, exactly "
        "as before this fix")

    events = read_events(tmp_path, types=["vigilant_verified_migration"])
    assert events[-1]["usedFallback"] is False
    assert events[-1]["matchedCount"] == 2
    assert events[-1]["sidecarKept"] is False


def test_migration_with_no_completed_tasks_is_legitimately_empty_and_cleans_up(tmp_path):
    """No completed tasks at all to classify -- distinct in the record from
    "matched nothing while there was something to match" (the prior two
    tests): `completedTaskCount == 0` names it explicitly, and the sidecar
    is safe to delete since nothing needed preserving."""
    run = RunDir(tmp_path)
    run.update_status(approach=1)
    atomic_write_json(run.vigilant_verified_file, ["1:001"])
    run.write_tasks(_plan(("001", "pending")))

    run.migrate_legacy_vigilant_verified()

    assert run.read_tasks()["tasks"][0]["status"] == "pending"
    assert not run.vigilant_verified_file.exists()

    events = read_events(tmp_path, types=["vigilant_verified_migration"])
    assert events[-1]["completedTaskCount"] == 0
    assert events[-1]["sidecarKept"] is False


# ---------------------------------------------------------------------------
# 3. Mutation: the pre-fix, current-approach-only matching reproduces the
#    exact incident against test 1's fixture.
# ---------------------------------------------------------------------------

def test_mutation_current_approach_only_matching_loses_the_verifications(
        tmp_path, monkeypatch):
    """Patch both seams `migrate_legacy_vigilant_verified()` uses back to
    the pre-fix shape -- `_resolve_legacy_vigilant_ids` to direct
    current-approach matches only (no cross-approach fallback), and
    `_legacy_vigilant_fold_is_uncertain` to the old unconditional-unlink
    behaviour (never withholds the delete) -- and show test 1's exact
    fixture now DOES lose everything: all 20 tasks demoted to
    `awaiting-validation` AND the sidecar deleted anyway. Proves test 1
    actually exercises this task's fix rather than passing by
    construction."""
    def _current_approach_only(self, by_approach, approach_now, completed_tasks):
        return set(by_approach.get(approach_now, set())), False

    monkeypatch.setattr(RunDir, "_resolve_legacy_vigilant_ids", _current_approach_only)
    monkeypatch.setattr(RunDir, "_legacy_vigilant_fold_is_uncertain",
                         staticmethod(lambda *a, **k: False))

    run = RunDir(tmp_path)
    plan = _reproduction_plan()
    _archive(run, 3, plan)
    run.write_tasks(plan)
    run.update_status(approach=4)
    atomic_write_json(run.vigilant_verified_file,
                       [f"3:{i}" for i in _twenty_task_ids()])

    run.migrate_legacy_vigilant_verified()

    statuses = {t["id"]: t["status"] for t in run.read_tasks()["tasks"]}
    assert set(statuses.values()) == {"awaiting-validation"}, (
        "the old current-approach-only matching finds nothing under "
        "approach 4's empty namespace and demotes every one of the 20 "
        "tasks -- the exact selfdev-v09-fsm-loop incident")
    assert not run.vigilant_verified_file.exists(), (
        "the old code unlinked unconditionally, with no notion of an "
        "uncertain fold -- the second half of the incident, alongside the "
        "lost verifications asserted above")

