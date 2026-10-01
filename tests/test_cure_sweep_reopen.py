"""PRD v0.29 requirement B (#280): a cure re-opens the sweep it makes
stale. Today `state.insert_cure_tasks` re-opens a validated record only
through a finding's own `invalidates` paths; a finding with none re-opens
nothing, so a `sweep: True` task can stay `validated` at a tree a later
cure pass just moved past.

B.1 gives the plan-proposal side: an optional boolean `sweep` field,
passed through ingestion unchanged (`plan_ingestion._TASK_PASSTHROUGH_
FIELDS`) and refused at shape time when it is not a boolean
(`plan_ingestion.validate_proposal_shape`).

B.2/B.3 give the engine side: once `insert_cure_tasks` has inserted at
least one cure task, from any caller, every `sweep: True` task that is
`validated`/`completed` is re-opened to `pending`, gains a `dependsOn` on
every cure task this pass inserted, has its pre-cure head recorded
through the same `preCureHeads` path the `invalidates` walk already uses,
and gets one `cure.sweep_reopened` event naming its id, its old status
and the cure task ids. A sweep already re-opened this same pass by the
`invalidates` walk is re-opened once, not twice. A sweep that is not yet
`validated`/`completed` only gains the `dependsOn`.

Five groups of nodes, matching PRD B.6's five cases:

* (i) the v028 shape: a validated sweep, an empty `invalidates`, one cure
  pass -- `pending`, a `dependsOn` on the cure task, one event;
* (ii) the same plan with no `sweep: True` task at all -- unchanged
  (today's behaviour);
* (iii) a finding whose `invalidates` names the sweep's OWN lane-evidence
  path -- re-opened once (through the existing chain), one `preCureHeads`
  entry, no double-processing by the new sweep step;
* (iv) a REFUTED-remedy insertion (`pass_kind="remedy"`) re-opens a
  validated sweep exactly as a granted pass does;
* (v) a non-boolean `sweep` is refused by `validate_proposal_shape`, by
  name.
"""

from __future__ import annotations

import json
from pathlib import Path

from ralphd.engine import plan_ingestion
from ralphd.engine.state import (
    PRE_CURE_HEAD_RECORDED_EVENT,
    SWEEP_REOPENED_EVENT,
    RunDir,
    atomic_write_json,
    insert_cure_tasks,
)


def _make_run(root: Path, existing_tasks: list) -> RunDir:
    root.mkdir(parents=True, exist_ok=True)
    run = RunDir(root=root)
    atomic_write_json(run.tasks_file, {"version": 1, "tasks": existing_tasks})
    return run


def _task(tid: str, *, status: str = "pending", depends: list | None = None,
          criteria: str = "n/a", sweep: bool | None = None) -> dict:
    task = {"id": tid, "title": tid, "successCriteria": criteria,
            "status": status, "origin": "planner",
            "dependsOn": list(depends or []), "tier": "normal"}
    if sweep is not None:
        task["sweep"] = sweep
    return task


def _blocking_curable_finding(clause: str, evidence: str, **overrides) -> dict:
    payload = {"severity": "blocking", "curable": True, "clause": clause,
               "evidence": evidence}
    payload.update(overrides)
    return payload


def _events(root: Path, type_: str) -> list[dict]:
    log = root / "events.jsonl"
    lines = log.read_text().splitlines() if log.exists() else []
    return [ev for ev in (json.loads(x) for x in lines) if ev.get("type") == type_]


# ---------------------------------------------------------------------------
# (i) the v028 shape: a validated sweep, empty invalidates, one cure pass.
# ---------------------------------------------------------------------------


def test_a_validated_sweep_with_empty_invalidates_is_reopened_with_one_event(tmp_path):
    root = tmp_path / "run"
    run = _make_run(root, [
        _task("sweep-task", status="validated", sweep=True,
              criteria="run the final tiers"),
    ])

    finding = _blocking_curable_finding(
        "requirement E (#113)", "no paths named", invalidates=[])
    inserted = insert_cure_tasks(run, 1, [finding])

    assert inserted == ["cure-01-01", "sweep-task"]

    data = run.read_tasks()
    tasks = {t["id"]: t for t in data["tasks"]}
    sweep_task = tasks["sweep-task"]
    assert sweep_task["status"] == "pending"
    assert sweep_task["dependsOn"] == ["cure-01-01"]

    events = _events(root, SWEEP_REOPENED_EVENT)
    assert len(events) == 1
    assert events[0]["taskId"] == "sweep-task"
    assert events[0]["oldStatus"] == "validated"
    assert events[0]["cureTaskIds"] == ["cure-01-01"]


# ---------------------------------------------------------------------------
# (ii) the same plan without `sweep` stays validated -- today's behaviour.
# ---------------------------------------------------------------------------


def test_a_validated_task_with_no_sweep_field_stays_validated(tmp_path):
    root = tmp_path / "run"
    run = _make_run(root, [
        _task("ordinary-task", status="validated",
              criteria="run the final tiers"),
    ])

    finding = _blocking_curable_finding(
        "requirement E (#113)", "no paths named", invalidates=[])
    inserted = insert_cure_tasks(run, 1, [finding])

    assert inserted == ["cure-01-01"]
    data = run.read_tasks()
    tasks = {t["id"]: t for t in data["tasks"]}
    assert tasks["ordinary-task"]["status"] == "validated"
    assert tasks["ordinary-task"].get("dependsOn") == []
    assert _events(root, SWEEP_REOPENED_EVENT) == []


def test_a_pending_sweep_only_gains_the_dependson(tmp_path):
    root = tmp_path / "run"
    run = _make_run(root, [
        _task("sweep-task", status="pending", sweep=True,
              criteria="run the final tiers"),
    ])

    finding = _blocking_curable_finding(
        "requirement E (#113)", "no paths named", invalidates=[])
    insert_cure_tasks(run, 1, [finding])

    data = run.read_tasks()
    tasks = {t["id"]: t for t in data["tasks"]}
    sweep_task = tasks["sweep-task"]
    assert sweep_task["status"] == "pending"
    assert sweep_task["dependsOn"] == ["cure-01-01"]
    # not re-opened -- it was never validated/completed, so no event.
    assert _events(root, SWEEP_REOPENED_EVENT) == []


# ---------------------------------------------------------------------------
# (iii) invalidates naming the sweep's own lane-evidence path re-opens it
# once, through the existing chain, with one preCureHeads entry.
# ---------------------------------------------------------------------------

RECORD_PATH = "artifacts/reports/v0.20-lanes.md"


def test_invalidates_naming_the_sweeps_own_path_reopens_it_once(tmp_path):
    root = tmp_path / "run"
    run = _make_run(root, [
        _task("057", status="validated", criteria="verify every lane"),
        _task("058", status="validated", depends=["057"], sweep=True,
              criteria=f"One commit adds {RECORD_PATH} naming the shipped sha."),
    ])

    finding = _blocking_curable_finding(
        "the shipped sha's lanes moved", "a later commit landed after the record",
        invalidates=[RECORD_PATH])
    inserted = insert_cure_tasks(run, 1, [finding], pre_cure_head="deadbeef01")
    cure_id = inserted[0]

    data = run.read_tasks()
    tasks = {t["id"]: t for t in data["tasks"]}
    assert tasks["057"]["status"] == "pending"
    assert tasks["058"]["status"] == "pending"
    assert cure_id in tasks["057"]["dependsOn"]
    # the sweep step still ran (058 is sweep: True) but found it already
    # pending from the chain re-open above, so it only ensured the
    # dependsOn edge -- which the chain already placed on 057, not 058,
    # since 058 had a verification step. No duplicate preCureHeads write.
    status = run.read_status()
    assert status.get("preCureHeads") == {"058": "deadbeef01"}
    pre_cure_events = _events(root, PRE_CURE_HEAD_RECORDED_EVENT)
    assert len(pre_cure_events) == 1
    assert pre_cure_events[0]["taskId"] == "058"
    # re-opened once, not twice: no cure.sweep_reopened event fires for a
    # sweep the invalidates walk already re-opened this same pass.
    assert _events(root, SWEEP_REOPENED_EVENT) == []


# ---------------------------------------------------------------------------
# (iv) a REFUTED-remedy insertion re-opens a validated sweep too.
# ---------------------------------------------------------------------------


def test_a_refuted_remedy_insertion_reopens_a_validated_sweep(tmp_path):
    root = tmp_path / "run"
    run = _make_run(root, [
        _task("sweep-task", status="validated", sweep=True,
              criteria="run the final tiers"),
    ])

    finding = _blocking_curable_finding(
        "the obligation this remedy answers", "(petition REFUTED for task x)",
        remedy_for="petition-001")
    inserted = insert_cure_tasks(run, 1, [finding], pass_kind="remedy")

    assert "sweep-task" in inserted
    data = run.read_tasks()
    tasks = {t["id"]: t for t in data["tasks"]}
    sweep_task = tasks["sweep-task"]
    assert sweep_task["status"] == "pending"
    cure_id = inserted[0]
    assert sweep_task["dependsOn"] == [cure_id]
    events = _events(root, SWEEP_REOPENED_EVENT)
    assert len(events) == 1
    assert events[0]["taskId"] == "sweep-task"
    assert events[0]["oldStatus"] == "validated"


# ---------------------------------------------------------------------------
# (v) a non-boolean sweep is refused by validate_proposal_shape.
# ---------------------------------------------------------------------------


def test_a_non_boolean_sweep_is_refused_at_shape_time():
    payload = {
        "tasks": [
            {"id": "t1", "title": "t1", "successCriteria": "n/a",
             "sweep": "true"},
        ],
    }
    errors = plan_ingestion.validate_proposal_shape(payload)
    assert any("sweep" in e for e in errors)


def test_a_boolean_sweep_passes_shape_validation():
    payload = {
        "tasks": [
            {"id": "t1", "title": "t1", "successCriteria": "n/a",
             "sweep": True},
        ],
    }
    assert plan_ingestion.validate_proposal_shape(payload) == []


def test_sweep_is_a_passthrough_field_and_lands_on_the_ingested_task():
    assert "sweep" in plan_ingestion._TASK_PASSTHROUGH_FIELDS
    payload = {
        "tasks": [
            {"id": "t1", "title": "t1", "successCriteria": "n/a",
             "sweep": True},
        ],
    }
    ingested = plan_ingestion.ingest_proposal(payload)
    task = ingested["tasks"][0]
    assert task["sweep"] is True
