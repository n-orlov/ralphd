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

#209 defect 3 narrows both branches without touching that invariant, and
its own group of nodes sits between the second and the third below: a task
the ENGINE authored (`origin: "review-cure"`) is never recognised as the
record or as a verification step, so a later pass stops re-opening an
earlier pass's LANDED cure task; and one path re-uses this approach's
existing re-take row rather than growing a second one with the same title.
Its RESIDUAL narrows the same walk once more, in the group after that: a
dependency the PLAN declares to be a pure ordering edge
(`tasks[].orderingOnly`) is not an evidence step, so the re-open leaves it
alone -- while an absent declaration still means "every edge produces
evidence", the reading every plan written without the field gets.

Five groups of nodes:

* the per-path insertion itself, direct against `insert_cure_tasks`
  (`_make_run`, the same harness `tests/test_cure_pass.py`'s own
  collision-avoidance section already uses);
* the lane re-open chain, same harness, a fixture pair shaped like a
  "verify every lane" task depended on by a "record every lane" task;
* the cure-origin exclusion and the per-path re-take reuse, same harness
  (#209 defect 3);
* the planner-declared ordering edge, same harness (#209 defect 3's
  residual): the narrowing, the absent-field default, a re-pointed edge
  defaulting back to evidence-producing, and the one behaviour the narrowing
  gives up (a declared edge at `failed` is not resurrected);
* the emitted event's payload -- driving a real `LoopSupervisor.
  _step_review()` pass over a finding carrying `invalidates` of both
  shapes at once, and reading `approach.cure_pass`'s own `taskIds`
  back off `events.jsonl`.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path

from ralphd.engine import findings
from ralphd.engine.loop import LoopSupervisor
from ralphd.engine.runner import IterationResult
from ralphd.engine.state import (
    PRE_CURE_HEAD_RECORDED_EVENT,
    RunDir,
    apply_worker_dependency_repoints,
    atomic_write_json,
    insert_cure_tasks,
    invalidates_reopen_walk,
    task_blocked_by,
)

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
# Task 005 (PRD requirement B.1, #221): the pure predicate extracted from
# the walk above, called directly over a tasks list -- no `RunDir`, no
# write.
# ---------------------------------------------------------------------------


def test_the_reopen_walk_predicate_pins_the_would_reopen_ids_without_mutating(tmp_path):
    tasks = _lane_fixture_tasks()
    before = json.loads(json.dumps(tasks))

    walk = invalidates_reopen_walk(
        tasks, [RECORD_PATH, "docs/testing.md", "", 7, None])

    assert len(walk) == 5
    record_task, verification_tasks = walk[0]
    assert record_task["id"] == "058"
    assert [t["id"] for t in verification_tasks] == ["057"]
    # a path with no lane-evidence chain, an empty string, and two
    # non-string entries all come back `None` -- the re-take branch's
    # territory, never this predicate's.
    assert walk[1:] == [None, None, None, None]

    # pure: the tasks list handed in is exactly as it went in.
    assert tasks == before


def test_the_reopen_walk_predicate_returns_empty_for_a_non_list_invalidates(tmp_path):
    tasks = _lane_fixture_tasks()
    assert invalidates_reopen_walk(tasks, None) == []
    assert invalidates_reopen_walk(tasks, "not-a-list") == []
    assert invalidates_reopen_walk(tasks, []) == []


# ---------------------------------------------------------------------------
# #209 defect 3: the engine's own cure output is never lane evidence, and
# one path never grows a second re-take task.
# ---------------------------------------------------------------------------

CURE_PATH = "src/ralphd/log_merge.py"


def _cure_task(tid: str, *, status: str = "validated", depends=None,
               title: str | None = None, criteria: str = "cure the finding") -> dict:
    """A row the ENGINE authored on an earlier cure pass over this same
    approach (`state.cure_task_from_finding`/`_retake_task_from_path`'s own
    shape): the distinguishing fact is `origin: "review-cure"`."""
    return {"id": tid, "title": title if title is not None else tid,
            "successCriteria": criteria, "status": status,
            "origin": "review-cure", "dependsOn": list(depends or []),
            "tier": "normal"}


def test_the_engines_own_cure_task_is_never_a_verification_step(tmp_path):
    """The measured deck-phase4/selfdev-v022 shape: pass 1 adds a `cure ->
    record` edge onto a genuine planner record, and on pass 2 that edge made
    the pass-1 CURE TASK look like a verification step to re-open --
    `deck-phase4` reset `cure-03-01`/`cure-03-02` from `validated` to
    `pending` and re-worked them; `selfdev-v022` left `cure-01-01` `pending`
    behind three LATER cure tasks. All three phases of the filter are pinned
    here because they are one decision with three consequences -- the third
    added by the unit-3 review, which found the CANDIDATE half of the filter
    discriminated by no fixture at all: for an ordinary re-take row the
    verification half already decides the outcome by itself, so the candidate
    half needs the one shape where it does not.
    """
    # Phase 1: the record's dependsOn names a planner verification step AND
    # pass 1's own cure task. Only the planner pair is re-opened.
    run = _make_run(tmp_path / "run", [
        _task("057", status="validated", criteria="verify every lane"),
        _task("058", status="validated", depends=["057", "cure-01-01"],
              criteria=f"One commit adds {RECORD_PATH} naming the shipped sha."),
        _cure_task("cure-01-01"),
    ])
    finding = _blocking_curable_finding(
        "the lanes moved again", "a second cure landed after the record",
        invalidates=[RECORD_PATH])
    inserted = insert_cure_tasks(run, 1, [finding])

    tasks = {t["id"]: t for t in run.read_tasks()["tasks"]}
    assert tasks["057"]["status"] == "pending"
    assert tasks["058"]["status"] == "pending"
    # the landed cure task keeps its terminal status, its own dependsOn, and
    # is absent from the ids the pass reports as put back into motion.
    assert tasks["cure-01-01"]["status"] == "validated"
    assert tasks["cure-01-01"]["dependsOn"] == []
    assert "cure-01-01" not in inserted
    assert set(inserted[1:]) == {"057", "058"}

    # Phase 2: the behaviour this gives up, stated rather than implied -- a
    # record whose ONLY verification step is an engine-authored row is no
    # longer re-opened, so the path falls back to a re-take task and is
    # still re-recorded at the new cure's tree.
    run2 = _make_run(tmp_path / "run2", [
        _task("058", status="validated", depends=["cure-01-01"],
              criteria=f"One commit adds {RECORD_PATH} naming the shipped sha."),
        _cure_task("cure-01-01"),
    ])
    inserted2 = insert_cure_tasks(run2, 1, [finding])
    tasks2 = {t["id"]: t for t in run2.read_tasks()["tasks"]}
    assert tasks2["058"]["status"] == "validated"  # not re-opened
    assert tasks2["cure-01-01"]["status"] == "validated"
    retakes = [t for t in tasks2.values() if t["title"] == f"Re-take {RECORD_PATH}"]
    assert len(retakes) == 1
    assert retakes[0]["id"] in inserted2
    assert retakes[0]["dependsOn"] == [inserted2[0]]

    # Phase 3: the shape only the CANDIDATE half of the filter reaches -- an
    # engine-authored re-take row whose own `dependsOn` was re-pointed onto
    # PLANNER work, which an operator `steer task-update` writes today (and a
    # worker's accepted `proposed_task_updates` will). Its verification list
    # is non-empty and legitimate, so the verification half of the filter
    # does not skip it; without the candidate half the engine's own row is
    # matched AS the record, `060` is dragged out of `validated`, and the row
    # never receives this pass's cure edge at all.
    run3 = _make_run(tmp_path / "run3", [
        _task("060", status="validated", criteria="verify every lane"),
        {**_cure_task("retake-01-01-01", status="validated",
                      title=f"Re-take {RECORD_PATH}", depends=["060"]),
         "successCriteria": (f"{RECORD_PATH} is re-taken (re-recorded) at "
                             "the tree cure task cure-01-01 leaves.")},
    ])
    inserted3 = insert_cure_tasks(run3, 1, [finding])
    tasks3 = {t["id"]: t for t in run3.read_tasks()["tasks"]}
    assert tasks3["060"]["status"] == "validated"     # never dragged back
    assert tasks3["060"]["dependsOn"] == []           # and never chained onto
    assert "060" not in inserted3
    # the re-take row is re-used instead, exactly as any other pass would.
    assert tasks3["retake-01-01-01"]["status"] == "pending"
    assert tasks3["retake-01-01-01"]["dependsOn"] == ["060", inserted3[0]]
    assert inserted3 == [inserted3[0], "retake-01-01-01"]


def test_a_later_cure_pass_reuses_the_retake_task_and_never_reopens_the_cure(tmp_path):
    """Route 1 of the same misclassification, plus Change 2's reuse. A
    re-take task's own `successCriteria` names its path verbatim and it
    `dependsOn`s its cure task, so from pass 2 on it satisfied "criteria
    names the path AND a non-empty dependsOn" and was matched as the plan's
    own lane-evidence record -- which re-opened its cure task and wrote a
    cure-to-cure edge (`selfdev-v022`: `cure-01-01.dependsOn ==
    ['cure-01-02']`). Now the re-take is invisible to that recognition and
    the path re-uses that ONE row at the new cure's tree instead.
    """
    run = _make_run(tmp_path / "run", [])
    finding = _blocking_curable_finding(
        "requirement E.3 (#167)", "the merge went stale",
        invalidates=[CURE_PATH])
    first = insert_cure_tasks(run, 1, [finding])
    assert len(first) == 2
    first_cure, retake_id = first

    # the worker landed both: the re-take is validated when pass 2 arrives.
    # Written through `run.write_tasks` (not `atomic_write_json` against the
    # view copy `_make_run` seeds) because `read_tasks` reads only the
    # protected, authoritative copy the first call above just created.
    data = run.read_tasks()
    for t in data["tasks"]:
        t["status"] = "validated"
        # and `LoopSupervisor._check_criteria_edits` -- which runs on EVERY
        # worker pass -- has stamped each row's criteria baseline by now, so
        # the re-take row pass 2 re-uses carries pass 1's fingerprint. This
        # is what makes the assertion at the end of this node non-vacuous
        # (#209, unit 8): without the engine moving that baseline forward in
        # the same write as the restatement, the next `_check_criteria_edits`
        # RESTORES pass 1's sentence and accuses a worker of tampering.
        t["criteriaFingerprint"] = hashlib.sha256(
            t["successCriteria"].encode("utf-8")).hexdigest()
        t["criteriaOfRecord"] = t["successCriteria"]
    run.write_tasks(data)

    second = insert_cure_tasks(run, 1, [finding])
    assert len(second) == 2
    second_cure = second[0]
    assert second[1] == retake_id  # the SAME row, named once

    tasks = {t["id"]: t for t in run.read_tasks()["tasks"]}
    assert len([t for t in tasks.values()
                if t["title"] == f"Re-take {CURE_PATH}"]) == 1
    assert tasks[retake_id]["status"] == "pending"      # stale at the new tree
    assert tasks[retake_id]["dependsOn"] == [first_cure, second_cure]
    # and its criterion is restated over THIS pass's cure task rather than
    # left naming pass 1's (unit-3 review): a verify hat scoring the
    # re-record must be told the tree it is owed, not one a pass out of date.
    assert tasks[retake_id]["successCriteria"] == (
        f"{CURE_PATH} is re-taken (re-recorded) at the tree cure task "
        f"{second_cure} leaves.")
    # and the criteria baseline moved forward in the SAME write as the
    # restatement (fix #132's rule, applied to this second engine writer):
    # otherwise the next `_check_criteria_edits` reverts the restatement,
    # blames a worker for the engine's own write, and sets the sticky
    # `criteriaEditedAfterValidationFailure` marker that
    # `findings.flagged_criteria_findings` turns into a blocking finding on
    # every later review pass -- no `verified` verdict possible thereafter.
    assert tasks[retake_id]["criteriaFingerprint"] == hashlib.sha256(
        tasks[retake_id]["successCriteria"].encode("utf-8")).hexdigest()
    assert (tasks[retake_id]["criteriaOfRecord"]
            == tasks[retake_id]["successCriteria"])
    # pass 1's landed cure task is left exactly where the worker left it.
    assert tasks[first_cure]["status"] == "validated"
    assert tasks[first_cure]["dependsOn"] == []


def test_two_findings_naming_the_same_path_share_one_retake_task(tmp_path):
    """Change 2 within ONE call: the per-(finding, path) re-take append was
    the one non-idempotent cost left inside a single `insert_cure_tasks`
    call. Contrast `test_multiple_paths_on_one_finding_each_get_their_own_
    retake_task` above, which keeps the one-task-per-DISTINCT-path property.
    """
    run = _make_run(tmp_path / "run", [])
    findings = [
        _blocking_curable_finding("requirement A", "first",
                                  invalidates=["docs/testing.md"]),
        _blocking_curable_finding("requirement B", "second",
                                  invalidates=["docs/testing.md"]),
    ]
    inserted = insert_cure_tasks(run, 1, findings)

    tasks = {t["id"]: t for t in run.read_tasks()["tasks"]}
    retakes = [t for t in tasks.values()
               if t["title"] == "Re-take docs/testing.md"]
    assert len(retakes) == 1
    # one re-take, depending on BOTH cure tasks in finding order, and named
    # exactly once in the returned ids.
    assert retakes[0]["dependsOn"] == ["cure-01-01", "cure-01-02"]
    assert inserted.count(retakes[0]["id"]) == 1
    assert inserted == ["cure-01-01", retakes[0]["id"], "cure-01-02"]


def test_a_retake_blocked_by_a_dead_cure_dependency_is_not_reused(tmp_path):
    """The deadlock guard on Change 2's reuse: a re-take whose own
    `dependsOn` names a `failed`/`skipped` task can never be selected again
    (`scheduler.select_next_task`), so reusing it would turn today's
    duplicate row into #206's "no selectable tasks" shape. A fresh re-take
    is appended instead -- exactly the pre-change behaviour -- and the dead
    edge is never inherited.
    """
    run = _make_run(tmp_path / "run", [
        _cure_task("cure-01-09", status="failed"),
        _cure_task("retake-01-09-01", status="validated",
                   title=f"Re-take {CURE_PATH}", depends=["cure-01-09"],
                   criteria=f"{CURE_PATH} is re-taken (re-recorded)."),
    ])
    finding = _blocking_curable_finding(
        "requirement E.3 (#167)", "stale again", invalidates=[CURE_PATH])
    inserted = insert_cure_tasks(run, 1, [finding])

    tasks = {t["id"]: t for t in run.read_tasks()["tasks"]}
    assert tasks["retake-01-09-01"]["status"] == "validated"      # untouched
    assert tasks["retake-01-09-01"]["dependsOn"] == ["cure-01-09"]
    fresh = [t for t in tasks.values()
             if t["title"] == f"Re-take {CURE_PATH}"
             and t["id"] != "retake-01-09-01"]
    assert len(fresh) == 1
    assert fresh[0]["dependsOn"] == [inserted[0]]
    assert fresh[0]["id"] in inserted


# ---------------------------------------------------------------------------
# #209 defect 3's residual: a dependency the PLAN declares to be a pure
# ordering edge is not evidence, so the re-open leaves it alone.
# ---------------------------------------------------------------------------


def test_a_dependency_declared_ordering_only_is_not_dragged_back(tmp_path):
    """The `selfdev-v022` shape the earlier nodes above could not reach: a
    requirement-G finding named a test module, the record `013`'s criteria
    cited that module, and `012` -- `validated`, its work landed at
    `751abe5e` with a green nightly per `013`'s own notes -- was re-opened
    and re-worked purely for sitting in `013.dependsOn`. The engine cannot
    tell that ordering edge from an evidence-producing one by reading
    criteria text, so the plan declares it (`tasks[].orderingOnly`, read
    through `plan_ingestion.ordering_only_dependencies`).

    Four phases, one decision:

    * a declared ordering edge keeps its terminal status and is absent from
      the returned ids, and -- the requirement E.3 half, which is why this
      belongs in the same node -- the cure edge lands on the RECORD itself
      when no verification step was re-opened to carry it, so the re-record
      still cannot be picked ahead of the cure;
    * the same fixture with the field ABSENT re-opens both tasks, chaining
      cure -> verification -> record exactly as every plan written without
      the field already does. That backward compatibility is the field's
      load-bearing safety property, so it is pinned here and not merely
      implied by the older nodes above;
    * a declaration is intersected with the row's LIVE `dependsOn`, so an
      edge some other writer re-pointed (here the real one, `state.apply_
      worker_dependency_repoints` from a worker's `proposed_task_updates`)
      defaults to evidence-producing, and the leftover declaration for an id
      that is no longer a dependency is inert;
    * the ONE behaviour this narrowing gives up, pinned explicitly rather
      than left to the source docstring (unit-7 review, the same way #209
      defect 3's own carve-out is pinned in `test_the_engines_own_cure_task_
      is_never_a_verification_step`): a declared ordering edge sitting at
      `failed` is NOT resurrected to `pending` by the re-open, so the
      re-opened record is blocked on it -- which is the input the doomed-plan
      net (`LoopSupervisor._doomed_plan_fault`, task 007/#99) reads to route
      the approach to review, not a starved plan.
    """
    record_criteria = f"One commit adds {RECORD_PATH} naming the shipped sha."
    finding = _blocking_curable_finding(
        "the shipped sha's lanes moved", "a later commit landed after the record",
        invalidates=[RECORD_PATH])

    # Phase 1: declared ordering-only.
    run = _make_run(tmp_path / "run", [
        _task("012", status="validated", criteria="the earlier deliverable"),
        {**_task("013", status="validated", depends=["012"],
                 criteria=record_criteria),
         "orderingOnly": ["012"]},
    ])
    inserted = insert_cure_tasks(run, 1, [finding])

    tasks = {t["id"]: t for t in run.read_tasks()["tasks"]}
    cure_id = inserted[0]
    assert tasks["012"]["status"] == "validated"      # never dragged back
    assert tasks["012"]["dependsOn"] == []            # and never chained onto
    assert "012" not in inserted
    assert inserted == [cure_id, "013"]
    # the record is still re-opened, every pass, and carries the cure edge
    # itself now that no verification step was re-opened to carry it.
    assert tasks["013"]["status"] == "pending"
    assert tasks["013"]["dependsOn"] == ["012", cure_id]

    # Phase 2: the identical fixture with no declaration -- today's behaviour,
    # unchanged, because an absent field means "every edge produces evidence".
    run2 = _make_run(tmp_path / "run2", [
        _task("012", status="validated", criteria="the earlier deliverable"),
        _task("013", status="validated", depends=["012"], criteria=record_criteria),
    ])
    inserted2 = insert_cure_tasks(run2, 1, [finding])
    tasks2 = {t["id"]: t for t in run2.read_tasks()["tasks"]}
    assert tasks2["012"]["status"] == "pending"
    assert tasks2["013"]["status"] == "pending"
    assert tasks2["012"]["dependsOn"] == [inserted2[0]]   # cure -> verification
    assert tasks2["013"]["dependsOn"] == ["012"]          # -> record, untouched
    assert set(inserted2[1:]) == {"012", "013"}

    # Phase 3: a re-pointed edge nobody declared is evidence-producing.
    run3 = _make_run(tmp_path / "run3", [
        _task("012", status="validated", criteria="the earlier deliverable"),
        _task("014", status="validated", criteria="the replacement deliverable"),
        {**_task("013", status="validated", depends=["012"],
                 criteria=record_criteria),
         "orderingOnly": ["012"]},
    ])
    assert apply_worker_dependency_repoints(run3, [
        {"task_id": "013", "dependsOn": ["014"],
         "reason": "014 carries the work 012 was standing in for"},
    ]) == ["013"]
    inserted3 = insert_cure_tasks(run3, 1, [finding])
    tasks3 = {t["id"]: t for t in run3.read_tasks()["tasks"]}
    assert tasks3["014"]["status"] == "pending"           # the new edge IS evidence
    assert tasks3["014"]["dependsOn"] == [inserted3[0]]
    assert tasks3["013"]["status"] == "pending"
    assert tasks3["013"]["dependsOn"] == ["014"]          # no edge on the record
    assert set(inserted3[1:]) == {"013", "014"}
    # the stale declaration for the dropped id is inert, never re-applied
    assert tasks3["013"]["orderingOnly"] == ["012"]

    # Phase 4: the give-up. A declared ordering edge at `failed` is left
    # `failed` -- an evidence re-open must not write `pending` over a terminal
    # failure for an edge the plan says is a real ordering requirement -- so
    # the re-opened record is blocked on it rather than schedulable, and THAT
    # map is what routes the approach to review (`_doomed_plan_fault`).
    run4 = _make_run(tmp_path / "run4", [
        _task("012", status="failed", criteria="the earlier deliverable"),
        {**_task("013", status="validated", depends=["012"],
                 criteria=record_criteria),
         "orderingOnly": ["012"]},
    ])
    inserted4 = insert_cure_tasks(run4, 1, [finding])
    doc4 = run4.read_tasks()
    tasks4 = {t["id"]: t for t in doc4["tasks"]}
    assert tasks4["012"]["status"] == "failed"            # not resurrected
    assert inserted4 == [inserted4[0], "013"]             # and never named
    assert tasks4["013"]["status"] == "pending"
    assert tasks4["013"]["dependsOn"] == ["012", inserted4[0]]
    assert task_blocked_by(doc4["tasks"]) == {"013": ["012"]}


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


# ---------------------------------------------------------------------------
# Requirement B.2 (task 006, #221): at the point of re-open, the workspace
# HEAD the cure pass started at is persisted per re-opened RECORD task (never
# per verification/retake task) into `status.json`'s `preCureHeads` map, in
# the exact `{"<taskId>": "<sha>"|null}` shape `approachStartHeads` already
# uses, and the same sha is emitted on `PRE_CURE_HEAD_RECORDED_EVENT`. Nothing
# reads the map back yet -- requirement B.4 (task 008) is the consumer.
# ---------------------------------------------------------------------------


def test_the_reopen_persists_and_emits_the_pre_cure_head(tmp_path):
    root = tmp_path / "run"
    run = _make_run(root, _lane_fixture_tasks())

    finding = _blocking_curable_finding(
        "the shipped sha's lanes moved", "a later commit landed after the record",
        invalidates=[RECORD_PATH])
    inserted = insert_cure_tasks(run, 1, [finding], pre_cure_head="deadbeef01")
    cure_id = inserted[0]

    # persisted, keyed by the re-opened RECORD task's own id (058) -- never
    # the verification task (057), which is not this requirement's subject.
    status = run.read_status()
    assert status.get("preCureHeads") == {"058": "deadbeef01"}

    events = _events(root, PRE_CURE_HEAD_RECORDED_EVENT)
    assert len(events) == 1
    assert events[0]["taskId"] == "058"
    assert events[0]["sha"] == "deadbeef01"
    assert events[0]["cureTaskId"] == cure_id
    # the fixture assertion the requirement names: the event's own sha
    # matches the persisted map entry for the same task id.
    assert events[0]["sha"] == status["preCureHeads"][events[0]["taskId"]]


def test_a_second_reopen_of_the_same_record_overwrites_rather_than_accumulates(tmp_path):
    root = tmp_path / "run"
    run = _make_run(root, _lane_fixture_tasks())

    finding = _blocking_curable_finding(
        "the shipped sha's lanes moved", "a later commit landed after the record",
        invalidates=[RECORD_PATH])
    insert_cure_tasks(run, 1, [finding], pre_cure_head="deadbeef01")
    insert_cure_tasks(run, 2, [finding], pre_cure_head="deadbeef02")

    status = run.read_status()
    # one live baseline per record at a time -- the newer pass's own sha.
    assert status.get("preCureHeads") == {"058": "deadbeef02"}
    events = _events(root, PRE_CURE_HEAD_RECORDED_EVENT)
    assert [e["sha"] for e in events] == ["deadbeef01", "deadbeef02"]


def test_no_pre_cure_head_given_persists_null_same_as_approach_start_heads(tmp_path):
    root = tmp_path / "run"
    run = _make_run(root, _lane_fixture_tasks())

    finding = _blocking_curable_finding(
        "the shipped sha's lanes moved", "a later commit landed after the record",
        invalidates=[RECORD_PATH])
    insert_cure_tasks(run, 1, [finding])   # no pre_cure_head kwarg at all

    status = run.read_status()
    assert status.get("preCureHeads") == {"058": None}


# ---------------------------------------------------------------------------
# The literal probe: re-open a record through a real `LoopSupervisor`
# cure pass, then restart the supervisor in a GENUINELY FRESH OS PROCESS
# (`tests/_pre_cure_head_fresh_process_episode.py`, no python object, module
# cache or event loop shared with this test's own process) over the SAME
# run root/workspace, and read the persisted sha back off `status.json`
# unchanged -- the same fresh-process shape
# `tests/test_approach_start_head_persistence.py` already proves for
# `approachStartHeads`.
# ---------------------------------------------------------------------------


def test_a_fresh_os_process_reads_the_persisted_pre_cure_head_back_unchanged(tmp_path):
    import subprocess
    import sys

    workspace = tmp_path / "workspace"
    root = tmp_path / "run"
    workspace.mkdir(parents=True)
    subprocess.run(["git", "init", "-q"], cwd=workspace, check=True)
    subprocess.run(["git", "config", "user.email", "t@example.com"],
                   cwd=workspace, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=workspace, check=True)
    (workspace / "seed.txt").write_text("seed")
    subprocess.run(["git", "add", "-A"], cwd=workspace, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "seed"], cwd=workspace, check=True)
    seed_sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=workspace,
                              check=True, capture_output=True,
                              text=True).stdout.strip()

    helper = Path(__file__).parent / "_pre_cure_head_fresh_process_episode.py"
    assert helper.exists()

    episode1 = subprocess.run(
        [sys.executable, str(helper), "1", str(root), str(workspace), seed_sha],
        capture_output=True, text=True, timeout=60)
    assert episode1.returncode == 0, (
        f"episode 1 (its own fresh OS process) failed:\n"
        f"stdout:\n{episode1.stdout}\nstderr:\n{episode1.stderr}")

    episode2 = subprocess.run(
        [sys.executable, str(helper), "2", str(root), str(workspace), seed_sha],
        capture_output=True, text=True, timeout=60)
    assert episode2.returncode == 0, (
        f"episode 2 (a SECOND, independently spawned fresh OS process, no "
        f"python state shared with episode 1's process or this test's own) "
        f"failed:\nstdout:\n{episode2.stdout}\nstderr:\n{episode2.stderr}")

    status = json.loads((root / "status.json").read_text())
    assert status.get("preCureHeads") == {"058": seed_sha}
