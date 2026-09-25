"""#273: a task's verify budget and the paths that reopen it.

* Every engine reopen to `pending` for new work -- the three writes in
  `state.insert_cure_tasks`'s `invalidates` walk (a lane-evidence record,
  its verification steps, a reused re-take row) -- resets
  `validationAttempts` to 0 and keeps the spent count as
  `priorValidationAttempts`.
* `_verify_task` never silently skips an exhausted task: it emits
  `task.verify_skipped_exhausted` and fails the task as
  `validation-exhausted` instead of leaving it in `awaiting-validation`.
* Exhaustively over those reopen paths, every starting attempt count and
  both verdicts: after the verify dispatch no task rests in
  `awaiting-validation` with `validationAttempts >= VALIDATION_ATTEMPT_LIMIT`.
* A stagnation escalation names every task still `awaiting-validation`.
"""

from __future__ import annotations

import asyncio
import json


from ralphd.engine.runner import IterationResult
from ralphd.engine.state import (
    TASK_FAILURE_VALIDATION_EXHAUSTED,
    VALIDATION_ATTEMPT_LIMIT,
    insert_cure_tasks,
    read_events,
    reset_validation_attempts_for_reopen,
    task_failure_kind,
)
from tests.conftest import make_supervisor

RECORD_PATH = "artifacts/reports/lanes.md"
RETAKE_PATH = "src/ralphd/log_merge.py"


class _VerdictRunner:
    """A verify hat that answers every call with one fixed verdict."""

    def __init__(self, verdict: str):
        self.verdict = verdict
        self.calls = 0
        self.task_ids: list[str] = []
        self.running = False

    def interrupt(self) -> bool:
        return self.running

    async def run(self, prompt, transcript, **kw) -> IterationResult:
        self.calls += 1
        tid = prompt.split("- **id**: ", 1)[1].split("\n", 1)[0].strip()
        self.task_ids.append(tid)
        payload = {"task_id": tid, "verdict": self.verdict}
        if self.verdict == "rejected":
            payload["reason"] = "criterion not met"
        r = IterationResult(exit_code=0, duration_s=1.0)
        r.final_text = json.dumps(payload)
        r.usage = {"input": 1, "output": 1, "totalTokens": 2}
        return r


def _supervisor(tmp_path, tasks, verdict="verified"):
    return make_supervisor(
        tmp_path / "run", run_id="va273", iterations=40, max_approaches=1,
        vigilant=True, on_complete="exit", infra_retry_backoff_s=[0.0],
        infra_retry_backoff_max_s=0.0, infra_outage_budget_s=0.0,
        tasks=tasks, runner_factory=lambda sup: _VerdictRunner(verdict))


def _task(tid, *, status="validated", attempts=None, depends=None,
          criteria="n/a", origin="planner"):
    t = {"id": tid, "title": tid, "successCriteria": criteria,
         "status": status, "origin": origin,
         "dependsOn": list(depends or []), "tier": "normal"}
    if attempts is not None:
        t["validationAttempts"] = attempts
    return t


def _lane_tasks(attempts):
    """A verification step (057) and the lane-evidence record (058) that
    depends on it -- the chain an `invalidates` naming RECORD_PATH reopens."""
    return [
        _task("057", attempts=attempts, criteria="verify every lane"),
        _task("058", attempts=attempts, depends=["057"],
              criteria=f"One commit adds {RECORD_PATH} naming the sha."),
    ]


def _finding(path):
    return {"severity": "blocking", "curable": True, "clause": "stale",
            "evidence": "a later commit landed", "invalidates": [path]}


def _by_id(sup):
    return {t["id"]: t for t in sup.run.read_tasks()["tasks"]}


def _set(sup, tid, **fields):
    data = sup.run.read_tasks()
    for t in data["tasks"]:
        if t["id"] == tid:
            t.update(fields)
    sup.run.write_tasks(data)


def _seed_retake(sup, attempts):
    """Pass 1 appends a re-take row for RETAKE_PATH; mark it as having spent
    `attempts` verify rounds, so pass 2's reuse of the row is the reopen."""
    first = insert_cure_tasks(sup.run, 1, [_finding(RETAKE_PATH)])
    retake_id = first[1]
    _set(sup, first[0], status="validated")
    _set(sup, retake_id, status="validated", validationAttempts=attempts)
    return retake_id


# ---------------------------------------------------------------------------
# 1. every engine reopen resets the counter and keeps the prior value
# ---------------------------------------------------------------------------


def test_the_helper_resets_a_spent_count_and_leaves_an_unspent_one():
    task = {"id": "1", "validationAttempts": 3}
    reset_validation_attempts_for_reopen(task)
    assert task == {"id": "1", "validationAttempts": 0,
                    "priorValidationAttempts": 3}
    for unspent in ({"id": "1"}, {"id": "1", "validationAttempts": 0}):
        before = dict(unspent)
        reset_validation_attempts_for_reopen(unspent)
        assert unspent == before


def test_the_record_and_verification_reopen_resets_both_counters(tmp_path):
    sup = _supervisor(tmp_path, _lane_tasks(VALIDATION_ATTEMPT_LIMIT))
    insert_cure_tasks(sup.run, 1, [_finding(RECORD_PATH)])
    tasks = _by_id(sup)
    for tid in ("057", "058"):
        assert tasks[tid]["status"] == "pending"
        assert tasks[tid]["validationAttempts"] == 0
        assert tasks[tid]["priorValidationAttempts"] == VALIDATION_ATTEMPT_LIMIT

    # a reopen of a task that never spent an attempt records no prior count
    fresh = _supervisor(tmp_path / "fresh", _lane_tasks(None))
    insert_cure_tasks(fresh.run, 1, [_finding(RECORD_PATH)])
    for tid in ("057", "058"):
        assert "priorValidationAttempts" not in _by_id(fresh)[tid]


def test_the_reused_retake_row_reopen_resets_its_counter(tmp_path):
    sup = _supervisor(tmp_path, [])
    retake_id = _seed_retake(sup, VALIDATION_ATTEMPT_LIMIT)
    second = insert_cure_tasks(sup.run, 1, [_finding(RETAKE_PATH)])
    assert second[1] == retake_id
    row = _by_id(sup)[retake_id]
    assert row["status"] == "pending"
    assert row["validationAttempts"] == 0
    assert row["priorValidationAttempts"] == VALIDATION_ATTEMPT_LIMIT


def test_the_issue_273_sequence_reaches_a_verifier_after_the_reopen(tmp_path):
    """3 attempts -> petitioned -> validated, reopened by a cure pass and
    completed again: the task is dispatched to a verifier and validates."""
    sup = _supervisor(tmp_path, _lane_tasks(VALIDATION_ATTEMPT_LIMIT))
    insert_cure_tasks(sup.run, 1, [_finding(RECORD_PATH)])
    _set(sup, "058", status="awaiting-validation")   # the worker finished it

    task = _by_id(sup)["058"]
    assert asyncio.run(sup._verify_task(task)) is True
    assert sup.runner.task_ids == ["058"]
    assert _by_id(sup)["058"]["status"] == "validated"


# ---------------------------------------------------------------------------
# 2. the exhausted-attempts skip is never silent
# ---------------------------------------------------------------------------


def test_an_exhausted_task_awaiting_validation_fails_with_an_event(tmp_path):
    sup = _supervisor(tmp_path, [_task(
        "046", status="awaiting-validation",
        attempts=VALIDATION_ATTEMPT_LIMIT)])

    assert asyncio.run(sup._verify_task(_by_id(sup)["046"])) is False

    assert sup.runner.calls == 0, "no verifier may be dispatched"
    task = _by_id(sup)["046"]
    assert task["status"] == "failed"
    assert task["failureKind"] == TASK_FAILURE_VALIDATION_EXHAUSTED
    assert task_failure_kind(task) == TASK_FAILURE_VALIDATION_EXHAUSTED
    events = list(read_events(sup.run.root,
                              types={"task.verify_skipped_exhausted"}))
    assert len(events) == 1
    assert events[0]["taskId"] == "046"
    assert events[0]["validationAttempts"] == VALIDATION_ATTEMPT_LIMIT
    assert events[0]["limit"] == VALIDATION_ATTEMPT_LIMIT
    moves = [e for e in read_events(sup.run.root, types={"task"})
             if e.get("taskId") == "046"]
    assert moves and moves[-1]["newStatus"] == "failed"
    # with nothing actionable left, the failure routes to review
    # (`blocked_with_failures`), never to the stagnation guard
    blocked = sup._unresolved_failures(sup.run.read_tasks())
    assert [t["id"] for t in blocked] == ["046"]


def test_an_already_resolved_exhausted_task_is_left_alone(tmp_path):
    sup = _supervisor(tmp_path, [_task(
        "046", status="petitioned", attempts=VALIDATION_ATTEMPT_LIMIT)])
    before = _by_id(sup)["046"]
    assert asyncio.run(sup._verify_task(dict(before))) is False
    assert _by_id(sup)["046"] == before
    assert sup.runner.calls == 0
    assert not list(read_events(sup.run.root,
                                types={"task.verify_skipped_exhausted"}))


# ---------------------------------------------------------------------------
# 3. no task rests in awaiting-validation with a spent budget after dispatch
# ---------------------------------------------------------------------------


def _reopen_record(sup):
    insert_cure_tasks(sup.run, 1, [_finding(RECORD_PATH)])
    return ["057", "058"]


def _reopen_retake(sup):
    return [insert_cure_tasks(sup.run, 1, [_finding(RETAKE_PATH)])[1]]


def _no_reopen(sup):
    """Not an engine reopen: a steer or a crash-resume put the task back in
    `awaiting-validation` with whatever count it already had."""
    return ["058"]


_PATHS = {"record-and-verification": _reopen_record,
          "reused-retake": _reopen_retake,
          "no-reopen": _no_reopen}


def test_no_task_rests_awaiting_validation_with_a_spent_budget(tmp_path):
    """Exhaustive over the reopen paths, every starting count up to two past
    the limit, and both verdicts, in one node."""
    for path in sorted(_PATHS):
        for attempts in range(0, VALIDATION_ATTEMPT_LIMIT + 3):
            for verdict in ("verified", "rejected"):
                case = tmp_path / f"{path}-{attempts}-{verdict}"
                if path == "reused-retake":
                    sup = _supervisor(case, [], verdict=verdict)
                    _seed_retake(sup, attempts)
                else:
                    sup = _supervisor(case, _lane_tasks(attempts),
                                      verdict=verdict)
                for tid in _PATHS[path](sup):
                    # the worker completed it
                    _set(sup, tid, status="awaiting-validation")

                # the verify dispatch, as `_step_worker` drives it
                for task in [t for t in sup.run.read_tasks()["tasks"]
                             if t.get("status") == "awaiting-validation"]:
                    asyncio.run(sup._verify_task(task))

                stuck = [t["id"] for t in sup.run.read_tasks()["tasks"]
                         if t.get("status") == "awaiting-validation"
                         and t.get("validationAttempts", 0)
                         >= VALIDATION_ATTEMPT_LIMIT]
                assert stuck == [], (path, attempts, verdict)
                if path != "no-reopen":
                    # an engine reopen always earns the new claim a verifier
                    assert sup.runner.calls >= 1, (path, attempts, verdict)


# ---------------------------------------------------------------------------
# 4. the stagnation escalation names the task stuck awaiting validation
# ---------------------------------------------------------------------------


def test_the_stagnation_escalation_names_a_task_awaiting_validation(tmp_path):
    sup = _supervisor(tmp_path, [
        _task("046", status="awaiting-validation", attempts=2),
        _task("047", status="pending", depends=["046"])])
    outstanding = [{"id": "046", "status": "awaiting-validation"},
                   {"id": "047", "status": "pending"}]

    sup._pause_and_escalate_stagnation_last_approach(1, outstanding)

    ev = list(read_events(sup.run.root, types={"operator_escalation"}))[-1]
    assert ev["awaitingValidation"] == [{"id": "046", "validationAttempts": 2}]
    assert (f"046 (awaiting-validation, validationAttempts 2 of "
            f"{VALIDATION_ATTEMPT_LIMIT})") in ev["escalation"]

    # and adds nothing when no task is waiting on verify
    quiet = _supervisor(tmp_path / "quiet", [_task("047", status="pending")])
    quiet._pause_and_escalate_stagnation_last_approach(
        1, [{"id": "047", "status": "pending"}])
    ev = list(read_events(quiet.run.root, types={"operator_escalation"}))[-1]
    assert ev["awaitingValidation"] == []
    assert "awaiting-validation," not in ev["escalation"]
