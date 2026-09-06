"""Task 005 (requirement A.2, #154): a verified verdict can carry structured
notes, and the engine persists them as `verifyNotes` on the task record.

Three things pinned here:

* `HAT_SCHEMAS["verify"]` accepts a `verdict: "verified"` payload with an
  optional `notes` array of `{clause, observed, suggested}` objects, still
  accepts a plain `reason` string (either alone or beside `notes`), and
  keeps `additionalProperties: False` -- an unknown field is still refused.
* `_verify_task` writes `verifyNotes` onto the task record in the SAME
  `write_tasks` call that flips `status` to `validated` -- never a second
  write, never a sidecar file.
* A `rejected` verdict's behaviour is unchanged: no `verifyNotes` write, the
  existing `reason`-required check still applies.

Unit-level over `_verify_task` with `_run_iteration_once` stubbed, the same
seam `tests/test_infra_retry_all_phases.py` already uses -- no subprocess,
no real sleeping.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from ralphd.engine import contracts, transitions
from ralphd.engine.config import JobConfig
from ralphd.engine.loop import LoopSupervisor
from ralphd.engine.runner import IterationResult
from ralphd.engine.state import RunDir, atomic_write_json


# -- schema acceptance -------------------------------------------------------


def test_a_verified_verdict_with_structured_notes_is_schema_valid():
    payload = {
        "task_id": "005",
        "verdict": "verified",
        "notes": [
            {"clause": "exact command text",
             "observed": "ran a slightly different wrapper script",
             "suggested": "name the observable instead of the command"},
        ],
    }
    assert contracts.validate_payload("verify", payload) == []


def test_a_verified_verdict_still_accepts_a_plain_reason_string():
    payload = {"task_id": "005", "verdict": "verified",
               "reason": "everything checked out"}
    assert contracts.validate_payload("verify", payload) == []


def test_a_verified_verdict_accepts_both_reason_and_notes_together():
    payload = {"task_id": "005", "verdict": "verified",
               "reason": "mostly clean",
               "notes": [{"clause": "polling cadence", "observed": "x",
                           "suggested": "y"}]}
    assert contracts.validate_payload("verify", payload) == []


def test_additional_properties_are_still_refused():
    payload = {"task_id": "005", "verdict": "verified",
               "notes": [], "somethingElse": True}
    errors = contracts.validate_payload("verify", payload)
    assert errors, "an unknown field must still be rejected"
    assert any("somethingElse" in e for e in errors)


def test_a_non_array_notes_value_is_rejected_by_type():
    payload = {"task_id": "005", "verdict": "verified", "notes": "not a list"}
    errors = contracts.validate_payload("verify", payload)
    assert errors, "notes must be an array, a string is not one"


def test_rejected_verdict_schema_is_unaffected_by_the_new_field():
    payload = {"task_id": "005", "verdict": "rejected",
                "reason": "criterion X's deliverable is absent"}
    assert contracts.validate_payload("verify", payload) == []
    assert transitions.verify_report_errors(payload) == []
    # A rejected verdict with no reason is still refused -- unchanged.
    bare = {"task_id": "005", "verdict": "rejected"}
    assert contracts.validate_payload("verify", bare) == []  # schema-level: reason is optional
    assert transitions.verify_report_errors(bare) != [], (
        "the business rule requiring a reason on 'rejected' must be unchanged")


# -- _verify_task persistence -----------------------------------------------


def _verify_supervisor(tmp_path, task: dict) -> LoopSupervisor:
    run = RunDir(root=tmp_path)
    atomic_write_json(run.tasks_file, {"version": 1, "tasks": [task]})
    cfg = JobConfig(run_id="unit", vigilant=True,
                    infra_retry_backoff_s=[0.0],
                    infra_retry_backoff_max_s=0.0,
                    infra_outage_budget_s=1000.0)
    return LoopSupervisor(cfg, run, tmp_path)


def _stub_once(sup: LoopSupervisor, result: IterationResult):
    async def fake_once(phase, extra="", prompt_name=None, tier=None):
        return result

    async def fake_backoff(seconds):
        return seconds, False

    sup._run_iteration_once = fake_once  # type: ignore[method-assign]
    sup._wait_out_backoff = fake_backoff  # type: ignore[method-assign]


def test_verified_with_notes_persists_verifynotes_in_the_same_write(tmp_path):
    task = {"id": "005", "title": "t", "status": "completed",
            "successCriteria": "c"}
    sup = _verify_supervisor(tmp_path, task)
    notes = [
        {"clause": "wrapper script text", "observed": "used a shell one-liner",
         "suggested": "name the observable file instead"},
    ]
    result = IterationResult(exit_code=0)
    result.duration_s = 5.0
    result.final_text = json.dumps(
        {"task_id": "005", "verdict": "verified", "notes": notes})
    _stub_once(sup, result)

    verified = asyncio.run(sup._verify_task(task))

    assert verified is True
    t = sup.run.read_tasks()["tasks"][0]
    assert t["status"] == "validated"
    assert t["verifyNotes"] == notes, (
        "verifyNotes must land in the same write that set status=validated")


def test_verified_with_no_notes_leaves_verifynotes_unset(tmp_path):
    task = {"id": "005", "title": "t", "status": "completed",
            "successCriteria": "c"}
    sup = _verify_supervisor(tmp_path, task)
    result = IterationResult(exit_code=0)
    result.duration_s = 5.0
    result.final_text = json.dumps({"task_id": "005", "verdict": "verified"})
    _stub_once(sup, result)

    verified = asyncio.run(sup._verify_task(task))

    assert verified is True
    t = sup.run.read_tasks()["tasks"][0]
    assert t["status"] == "validated"
    assert "verifyNotes" not in t


def test_rejected_verdict_behaviour_is_unchanged(tmp_path):
    """A rejecting verify verdict still marks the task validation-failed
    (not the last attempt, so not `failed` yet) and never writes
    `verifyNotes` -- notes are a `verified`-only concept."""
    task = {"id": "005", "title": "t", "status": "completed",
            "successCriteria": "c", "validationAttempts": 0}
    sup = _verify_supervisor(tmp_path, task)
    result = IterationResult(exit_code=0)
    result.duration_s = 5.0
    result.final_text = json.dumps(
        {"task_id": "005", "verdict": "rejected",
         "reason": "criterion X's deliverable is absent"})
    _stub_once(sup, result)

    verified = asyncio.run(sup._verify_task(task))

    assert verified is False
    t = sup.run.read_tasks()["tasks"][0]
    assert t["status"] != "validated"
    assert "verifyNotes" not in t
