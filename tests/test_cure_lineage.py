"""Task 004 (v0.27, B.5 foundation, PRD requirement B.5, #274): the
disk-backed foundation a later confirmation review (B.1) and its
identity-based no-progress rule (B.4) build on.

Every granted cure pass -- review's own (`LoopSupervisor._take_cure_pass`)
and the peer verdict gate's (`_take_gate_cure_pass`) -- calls
`state.insert_cure_tasks` with the pass's blocking findings and the
workspace HEAD it was taken at (`pre_cure_head`). This task makes THAT one
call persist a run-scope, disk-backed ledger (`state.cure_lineage_path`/
`CURE_LINEAGE_FILE`) surviving a resume: one entry per granted pass,
keyed by the cure task id built for each finding, carrying that finding's
own `clause`/`cured_when` and the pass's `pre_cure_head` -- what B.1's
"## Confirmation review" section (a later task) reads to list "every
blocking finding of the previous granted pass, named by the id of the
cure task `insert_cure_tasks` built for each ... with its clause and
`cured_when`" and "the diff range ... the `pre_cure_head` handed to
`insert_cure_tasks`".

It also makes a finding carrying `reopens` (a listed cure task id a
confirmation review found still open) stamp the cure task built from it
with the ancestor it descends from, on that task's own `tasks.json`
entry -- what B.4 (a later task) follows back to a finding's root
identity "never from the id's spelling".

Two groups of nodes, both driving the real `state.insert_cure_tasks` (the
one call both `_take_cure_pass` and `_take_gate_cure_pass` make) rather
than a mock of either call site:

* the lineage record itself -- exists after one pass, accumulates across
  passes, and reads back identical from a brand-new `LoopSupervisor` over
  the same on-disk run dir (no shared Python state);
* the ancestor field a `reopens` finding's cure task carries in
  `tasks.json`.

Every node here is red without this task's change: before it, `state` has
no `cure_lineage_path`/`read_cure_lineage`/`record_cure_pass_lineage` at
all (an `AttributeError`/`ImportError` on the very first line of each
test), and `cure_task_from_finding` stamps no `reopensCureTaskId`.
"""

from __future__ import annotations

import json
from pathlib import Path

from ralphd.engine import findings
from ralphd.engine.state import (
    RunDir,
    atomic_write_json,
    insert_cure_tasks,
    read_cure_lineage,
)

from tests.conftest import supervisor_factory

_supervisor = supervisor_factory(
    run_id="task004-cure-lineage", cfg_defaults=False,
    status={"state": "running"},
    prd="# task 004 fixture PRD\n\nDo the thing.\n", max_approaches=3,
    operator_input_needed_pause=False)


def _finding(clause: str, evidence: str, cured_when: str, **overrides) -> dict:
    payload = {"severity": "blocking", "curable": True, "clause": clause,
               "evidence": evidence, "cured_when": cured_when}
    payload.update(overrides)
    return payload


def _cure_tasks(run: RunDir) -> list[dict]:
    return [t for t in run.read_tasks()["tasks"]
           if t.get("origin") == "review-cure"]


# ---------------------------------------------------------------------------
# The lineage record itself.
# ---------------------------------------------------------------------------


def test_the_record_exists_after_one_pass_keyed_by_cure_task_id(tmp_path):
    root = tmp_path / "run"
    sup = _supervisor(root, max_approaches=3)
    sup._approach = 1
    atomic_write_json(sup.run.tasks_file, {"version": 1, "tasks": []})

    blocking = [
        _finding("requirement C.1 (#141)",
                 "the worker never wrote the notify tool",
                 "a notify tool call is always emitted"),
        _finding("requirement C.2 (#141)",
                 "the worker skipped the retry backoff",
                 "a retry always waits out the backoff"),
    ]
    findings.write_findings_json(sup.run.findings_json_file, blocking)

    inserted = sup._take_cure_pass(1)
    assert len(inserted) == 2

    lineage = read_cure_lineage(sup.run.root)
    assert len(lineage["passes"]) == 1
    entry = lineage["passes"][0]
    assert entry["approach"] == 1
    # a non-git workspace's `_workspace_head_sha()` is `None` -- the record
    # still persists the pass, with that exact value, same optionality
    # `preCureHeads` already gives it.
    assert entry["preCureHead"] == sup._workspace_head_sha()
    assert set(entry["findings"]) == set(inserted)
    for cure_task_id, finding_payload in zip(inserted, blocking):
        assert entry["findings"][cure_task_id] == {
            "clause": finding_payload["clause"],
            "curedWhen": finding_payload["cured_when"],
        }


def test_a_second_granted_pass_appends_rather_than_overwrites(tmp_path):
    root = tmp_path / "run"
    sup = _supervisor(root, max_approaches=3)
    sup._approach = 1
    atomic_write_json(sup.run.tasks_file, {"version": 1, "tasks": []})

    first = [_finding("requirement C.1 (#141)", "evidence one", "cure one")]
    findings.write_findings_json(sup.run.findings_json_file, first)
    sup._take_cure_pass(1)

    second = [_finding("requirement C.2 (#141)", "evidence two", "cure two")]
    findings.write_findings_json(sup.run.findings_json_file, second)
    sup._take_cure_pass(1)

    lineage = read_cure_lineage(sup.run.root)
    assert len(lineage["passes"]) == 2
    assert lineage["passes"][0]["findings"] != lineage["passes"][1]["findings"]


def test_the_gates_own_cure_pass_persists_the_same_shape_of_record(tmp_path):
    root = tmp_path / "run"
    sup = _supervisor(root, max_approaches=3)
    sup._approach = 1
    atomic_write_json(sup.run.tasks_file, {"version": 1, "tasks": []})

    blocking = [_finding("requirement D.1", "gate-observed evidence",
                         "gate cure specification")]
    findings.write_findings_json(sup.run.verdict_gate_findings_json_file,
                                 blocking)

    sup._take_gate_cure_pass(1)

    cure_task_ids = [t["id"] for t in _cure_tasks(sup.run)]
    assert len(cure_task_ids) == 1

    lineage = read_cure_lineage(sup.run.root)
    assert len(lineage["passes"]) == 1
    entry = lineage["passes"][0]
    assert entry["findings"][cure_task_ids[0]] == {
        "clause": blocking[0]["clause"],
        "curedWhen": blocking[0]["cured_when"],
    }


def test_a_fresh_loopsupervisor_over_the_same_run_dir_reads_it_identically(
        tmp_path):
    root = tmp_path / "run"
    sup1 = _supervisor(root, max_approaches=3)
    sup1._approach = 1
    atomic_write_json(sup1.run.tasks_file, {"version": 1, "tasks": []})

    blocking = [_finding("requirement C.1 (#141)",
                         "the worker never wrote the notify tool",
                         "a notify tool call is always emitted")]
    findings.write_findings_json(sup1.run.findings_json_file, blocking)
    sup1._take_cure_pass(1)

    before = read_cure_lineage(sup1.run.root)
    assert before["passes"], "the pass must actually have written a record"

    # Episode 2: a brand-new `LoopSupervisor`, constructed over the SAME
    # on-disk run dir, with nothing from `sup1` carried over in memory --
    # what a killed-and-restarted process leaves behind (the same shape
    # `tests/test_cure_pass.py::test_resume_mid_cure_never_appends_the_
    # cure_tasks_twice` already drives for `curePasses`).
    sup2 = _supervisor(root, max_approaches=3)

    after = read_cure_lineage(sup2.run.root)
    assert after == before


# ---------------------------------------------------------------------------
# A reopen's new cure task names its ancestor.
# ---------------------------------------------------------------------------


def test_a_reopens_finding_stamps_its_new_cure_task_with_the_ancestor(
        tmp_path):
    root = tmp_path / "run"
    run = RunDir(root=root)
    atomic_write_json(run.tasks_file, {"version": 1, "tasks": []})

    root_finding = _finding("requirement C.1 (#141)", "first counterexample",
                            "the broken rule is closed")
    inserted_first = insert_cure_tasks(run, 1, [root_finding],
                                       pre_cure_head="deadbeef01")
    ancestor_id = inserted_first[0]

    reopen_finding = _finding(
        "requirement C.1 (#141)", "a second, unlisted counterexample",
        "the broken rule is closed", reopens=ancestor_id)
    inserted_second = insert_cure_tasks(run, 1, [reopen_finding],
                                        pre_cure_head="deadbeef02")
    new_cure_task_id = inserted_second[0]
    assert new_cure_task_id != ancestor_id

    tasks_by_id = {t["id"]: t for t in run.read_tasks()["tasks"]}
    assert tasks_by_id[ancestor_id].get("reopensCureTaskId") is None
    assert tasks_by_id[new_cure_task_id]["reopensCureTaskId"] == ancestor_id

    # the lineage record for the second pass names the SAME cure task id,
    # so a later reader can cross-reference the ancestor link against the
    # finding text it was reopened over.
    lineage = read_cure_lineage(run.root)
    assert lineage["passes"][1]["findings"][new_cure_task_id]["clause"] == \
        reopen_finding["clause"]


def test_a_finding_with_no_reopens_leaves_the_field_absent(tmp_path):
    root = tmp_path / "run"
    run = RunDir(root=root)
    atomic_write_json(run.tasks_file, {"version": 1, "tasks": []})

    ordinary = _finding("requirement C.1 (#141)", "evidence", "cure spec")
    inserted = insert_cure_tasks(run, 1, [ordinary], pre_cure_head="deadbeef")

    tasks_by_id = {t["id"]: t for t in run.read_tasks()["tasks"]}
    assert "reopensCureTaskId" not in tasks_by_id[inserted[0]]
