"""PRD v0.37 requirement A.1/A.2 (decision 3): plan phases in `plan-proposal.json`.

One test per refusal case A.2 (a)-(h), each asserting that case's own
distinct message; one valid phased proposal; the ingested `phases` shape;
and the engine's ownership of gate state (a worker's edit of the agent-
readable `tasks.json` view never reaches the engine's own read).
"""

from __future__ import annotations

import copy
import json

import pytest

from ralphd.engine import plan_ingestion as pi
from ralphd.engine.state import RunDir


def _task(task_id: str, phase: str | None = None, **extra) -> dict:
    task = {
        "id": task_id,
        "title": f"do thing {task_id}",
        "successCriteria": f"thing {task_id} is verifiably done",
    }
    if phase is not None:
        task["phase"] = phase
    task.update(extra)
    return task


def _phase(phase_id: str, **extra) -> dict:
    return {"id": phase_id, "title": f"Phase {phase_id}",
            "reviewFocus": f"check {phase_id}", **extra}


def _valid() -> dict:
    return {
        "goal": "g",
        "phases": [_phase("one"), _phase("two")],
        "tasks": [
            _task("001", "one"),
            _task("002", "one", dependsOn=["001"]),
            _task("003", "two", dependsOn=["002"]),
        ],
    }


def _errors(payload: dict) -> list[str]:
    return pi.validate_proposal_shape(payload)


# The substring that identifies each refusal case's own message.
_MARKERS = {
    "a-not-list": "$.phases: expected an array",
    "a-missing-title": "missing or blank 'title'",
    "a-missing-id": "missing or blank 'id'",
    "a-bad-id": "must match [a-z0-9-]+",
    "b": "duplicate phase id",
    "c": "names no phase in 'phases'",
    "d": "has no 'phase'",
    "e": "has no task",
    "f": "the later phase",
    "g": "a single phase is the no-phase plan",
    "h": "must be in the last phase",
}


def _only(errors: list[str], key: str) -> None:
    marker = _MARKERS[key]
    assert any(marker in e for e in errors), errors
    # distinct: no other case's marker appears in this refusal.
    for other, other_marker in _MARKERS.items():
        if other.split("-")[0] == key.split("-")[0]:
            continue
        assert not any(other_marker in e for e in errors), (key, other, errors)


def test_a_valid_phased_proposal_is_accepted():
    assert _errors(_valid()) == []
    parsed, errors, _warnings, _repairs = pi._parse_proposal(json.dumps(_valid()))
    assert errors == []
    assert parsed is not None


def test_a_refuses_phases_that_is_not_a_list():
    payload = _valid()
    payload["phases"] = {"one": "x"}
    _only(_errors(payload), "a-not-list")


def test_a_refuses_an_entry_missing_id_or_title():
    payload = _valid()
    del payload["phases"][0]["title"]
    _only(_errors(payload), "a-missing-title")
    payload = _valid()
    del payload["phases"][1]["id"]
    _only(_errors(payload), "a-missing-id")


@pytest.mark.parametrize("bad", ["Phase One", "one_two", "ONE", "a/b"])
def test_a_refuses_an_id_outside_the_pattern(bad):
    payload = _valid()
    payload["phases"][0]["id"] = bad
    for t in payload["tasks"]:
        if t["phase"] == "one":
            t["phase"] = bad
    _only(_errors(payload), "a-bad-id")


def test_b_refuses_a_duplicate_phase_id():
    payload = _valid()
    payload["phases"][1]["id"] = "one"
    _only(_errors(payload), "b")


def test_c_refuses_a_task_naming_an_unknown_phase():
    payload = _valid()
    payload["tasks"][2]["phase"] = "three"
    _only(_errors(payload), "c")


def test_d_refuses_a_phased_plan_with_an_untagged_task():
    payload = _valid()
    del payload["tasks"][1]["phase"]
    _only(_errors(payload), "d")


def test_e_refuses_a_phase_with_no_task():
    payload = _valid()
    payload["phases"].append(_phase("three"))
    _only(_errors(payload), "e")


def test_f_refuses_a_dependson_edge_to_a_later_phase_task():
    payload = _valid()
    payload["tasks"][0]["dependsOn"] = ["003"]
    _only(_errors(payload), "f")


def test_f_allows_an_edge_within_a_phase_and_back_to_an_earlier_one():
    # the valid fixture already holds both; stated on its own for clarity.
    assert not any("later phase" in e for e in _errors(_valid()))


def test_g_refuses_a_single_entry_phases_list():
    payload = _valid()
    payload["phases"] = [_phase("one")]
    for t in payload["tasks"]:
        t["phase"] = "one"
    _only(_errors(payload), "g")


def test_h_refuses_a_sweep_task_outside_the_last_phase():
    payload = _valid()
    payload["tasks"][0]["sweep"] = True
    _only(_errors(payload), "h")


def test_a_sweep_task_in_the_last_phase_is_accepted():
    payload = _valid()
    payload["tasks"][2]["sweep"] = True
    assert _errors(payload) == []


def test_a_phase_tag_without_phases_is_refused_not_dropped():
    payload = _valid()
    del payload["phases"]
    errors = _errors(payload)
    assert errors and all("no 'phases' list" in e for e in errors)


def test_a_phased_refusal_re_asks_planning(tmp_path):
    payload = _valid()
    payload["phases"] = [_phase("one")]
    for t in payload["tasks"]:
        t["phase"] = "one"
    (tmp_path / "bad.json").write_text(json.dumps(payload))
    (tmp_path / "good.json").write_text(json.dumps(_valid()))
    calls: list[list[str] | None] = []

    def respond(attempt, prior_errors):
        calls.append(prior_errors)
        name = "bad.json" if attempt == 0 else "good.json"
        return json.dumps({"proposal_path": name, "summary": "s"})

    result = pi.enforce_plan_ingestion(
        respond, max_retries=2, read_proposal=pi.file_reader(tmp_path))
    assert result.ok and result.attempts == 2
    assert any("no-phase plan" in e for e in calls[1])


# -- ingestion --------------------------------------------------------------

def test_ingestion_copies_phases_with_a_closed_gate_and_keeps_task_phases():
    doc = pi.ingest_proposal(_valid())
    assert doc["phases"] == [
        {"id": "one", "title": "Phase one", "reviewFocus": "check one",
         "gate": "closed", "rounds": 0, "startHead": None, "records": []},
        {"id": "two", "title": "Phase two", "reviewFocus": "check two",
         "gate": "closed", "rounds": 0, "startHead": None, "records": []},
    ]
    assert [t["phase"] for t in doc["tasks"]] == ["one", "one", "two"]


def test_ingestion_ignores_gate_state_a_proposal_smuggles_in():
    payload = _valid()
    payload["phases"][0].update(gate="passed", rounds=4, startHead="abc",
                                records=[{"x": 1}])
    phase = pi.ingest_proposal(payload)["phases"][0]
    assert (phase["gate"], phase["rounds"], phase["startHead"],
            phase["records"]) == ("closed", 0, None, [])


def test_a_proposal_without_phases_writes_no_phases_and_no_task_phase():
    payload = {"goal": "g", "tasks": [_task("001"), _task("002")]}
    assert _errors(payload) == []
    doc = pi.ingest_proposal(payload)
    assert "phases" not in doc
    assert all("phase" not in t for t in doc["tasks"])


# -- engine ownership of gate state -----------------------------------------

def test_a_workers_edit_of_phase_gate_is_discarded_like_a_status_edit(tmp_path):
    run = RunDir(tmp_path)
    doc = pi.ingest_proposal(_valid())
    run.write_tasks(doc)
    # The worker's raw write lands in the agent-readable view copy only.
    forged = copy.deepcopy(doc)
    forged["phases"][0]["gate"] = "passed"
    forged["phases"][0]["rounds"] = 9
    forged["tasks"][0]["status"] = "completed"
    run.tasks_file.write_text(json.dumps(forged))
    seen = run.read_tasks()
    assert seen["phases"][0]["gate"] == "closed"
    assert seen["phases"][0]["rounds"] == 0
    assert seen["tasks"][0]["status"] == "pending"  # same treatment
