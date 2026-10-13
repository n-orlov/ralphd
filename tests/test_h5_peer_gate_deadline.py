"""Task 027 (PRD requirement H.5, #264, and v0.24's own D.1 CONFIRMED
contradiction petition cure-01-04): with `peer_verdict_gate` enabled, a
post-deadline PASSING review verdict must reach a decisive conclusion
without dispatching a new iteration past the boundary, instead of holding
`exhausted` with a null verdict the way v0.24 shipped it.

v0.24's own two D.1 probes -- `tests/test_deadline.py::
test_peer_verdict_gate_verified_review_after_the_deadline_holds_pending_the_gate`
and its resume sibling `::test_peer_verdict_gate_hold_resumes_and_the_
peer_gate_still_renders_a_verdict` -- are RETIRED by this task's own
commit, NOT left in place: each pinned a scenario that can no longer
occur once this hole closes (a peer-gate-on post-deadline PASSING verdict
no longer holds at all, so there is nothing left for the resume sibling to
resume into). They were retired rather than mutated into asserting the
opposite of their own names; the surviving coverage for exactly that
shape is this module's own two nodes below, named as such in the comment
left at their old location in `tests/test_deadline.py`. That module's
"Section Jc" comment block -- the record of the CONFIRMED contradiction
petition itself -- keeps its original text verbatim, with one appended
`UPDATE (task 027 ...)` paragraph pointing here, so the adjudication
stays readable as history rather than being silently overwritten.

H.5 grants a way out of that contradiction that did not exist when those
probes were written -- one new declared transition-table edge
(`transitions.py`'s `PEER_VERDICT_GATE_DELTA["review"]`, guard
`verified_peer_verdict_gate_deferred_past_deadline`) -- and this module's
own two probes below exercise THAT edge specifically.

Mechanism chosen: H.5's option (b) -- "the run concludes on the review
verdict alone, with the gate's non-dispatch disclosed in the record as a
named residual rather than as a null verdict" -- recorded in this task's
own commit message. Option (a) (running the gate inside review's own
iteration) would require collapsing two independently-dispatched blind hat
calls into one, which does not fit this engine's existing one-hat-per-
iteration shape without a restructuring well past this task's scope.

v0.36 requirement B retires that edge: budget spent is the ONE rejection
ladder's first step, ahead of `verified`, so a post-deadline PASSING review
verdict now holds the run `exhausted` (time) with a null verdict and the
peer gate is still never dispatched past the boundary; a resume with a time
top-up judges the approach again. The pass probe below is rewritten to pin
that landing (covering ladder nodes: `tests/test_rejection_ladder.py::
test_step_1_budget_spent_holds_a_verified_verdict_exhausted`).

Per H.8 (restated by the standing rules): NEITHER half of requirement H is
live in the engine actually running this development run (the installed
0.24.0) -- these are `LoopSupervisor` probes over a scripted runner, the
same shape every other test in `tests/test_deadline.py` already uses, and
this run's own behaviour is cited as evidence for nothing here.
"""

from __future__ import annotations

import asyncio
import json
import time as real_time

from ralphd.engine import findings
from ralphd.engine.runner import IterationResult
from ralphd.engine.state import read_events
from tests.conftest import ScriptedRunner, attach_runner, make_supervisor


def _worker_completes(sup, prompt):
    r = IterationResult(exit_code=0)
    r.final_text = json.dumps({"verdict": "complete", "task_id": "T1"})
    r.duration_s = 0.5
    r.usage = {"input": 1, "output": 1, "totalTokens": 2}
    return r


def _review_verified_expires_deadline_mid_flight(sup, prompt):
    """Same shape as `tests/test_deadline.py`'s own `_task013_review_
    verified_expires_deadline_mid_flight` / `_cure0104_review_verified_
    expires_deadline_mid_flight`: the deadline is pushed into the past
    from INSIDE the already-dispatched review iteration -- `_step_review`
    has already consulted `budget_left()` (True) and dispatched this very
    iteration before this function ever runs, so `_step_review`'s own
    pre-dispatch boundary check (task cure-01-03) is never what is being
    exercised here; only the post-return `peer_verdict_gate` check H.5
    adds is.
    """
    sup.deadline = real_time.monotonic() - 1.0
    sup._deadline_epoch = real_time.time() - 1.0
    r = IterationResult(exit_code=0)
    r.final_text = json.dumps({"verdict": "verified"})
    r.duration_s = 0.5
    r.usage = {"input": 1, "output": 1, "totalTokens": 2}
    return r


def _gate_verified(sup, prompt):
    r = IterationResult(exit_code=0)
    r.final_text = json.dumps({"verdict": "verified"})
    r.duration_s = 0.1
    r.usage = {"input": 1, "output": 1, "totalTokens": 2}
    return r


def test_peer_gate_on_post_deadline_pass_holds_exhausted_without_the_gate(
        tmp_path):
    """Rewritten for v0.36 requirement B (it was `test_peer_gate_on_post_
    deadline_pass_concludes_instead_of_holding`, which pinned `succeeded`
    with a `verdictGateResidual`): with `peer_verdict_gate=True` and review
    rendering `verified` after the deadline expired mid-flight, the ladder's
    budget step wins -- the run holds `exhausted` (time), the peer gate is
    never dispatched past the boundary, and no residual is written because
    the run did not conclude.
    """
    sup = make_supervisor(
        tmp_path, run_id="task027-peer-gate-passing-past-deadline-holds",
        max_approaches=1, approach=1, iterations_used=1, prd=True,
        peer_verdict_gate=True,
        tasks=[{"id": "T1", "title": "do the thing", "successCriteria": "done",
               "status": "pending"}])
    attach_runner(sup, ScriptedRunner({
        "worker": [_worker_completes],
        "review": [_review_verified_expires_deadline_mid_flight],
        "verdict_gate": [_gate_verified],
    }))

    assert sup.budget_left() is True, (
        "precondition: the deadline is still ordinarily in the future "
        "before review is ever dispatched")

    state = asyncio.run(asyncio.wait_for(sup.run_job(), 30.0))

    assert sup.budget_left() is False, (
        "precondition actually exercised: the deadline is genuinely in "
        "the past by the time review's own verdict is read back")
    assert state == "exhausted", f"got state={state!r}"

    status = sup.run.read_status()
    assert status["state"] == "exhausted"
    assert status["exhaustedBudget"] == "time"
    assert status["verdict"] is None
    assert status.get("endedAt") is None, "a held run has not ended"
    assert status.get("verdictGateResidual") is None

    # The peer gate's own handler was never dispatched past the boundary.
    assert sup.runner.calls.get("verdict_gate", 0) == 0
    assert read_events(sup.run.root,
                       types=("verdict_gate_deferred_past_deadline",)) == []


def _review_rejects_curable_expires_deadline_mid_flight(sup, prompt):
    sup.deadline = real_time.monotonic() - 1.0
    sup._deadline_epoch = real_time.time() - 1.0
    findings.write_findings_json(sup.run.findings_json_file, [
        {"severity": "blocking", "curable": True,
         "clause": "task 027 fixture clause unmet",
         "evidence": "the thing is not actually done",
         "cured_when": "the thing is actually done"},
    ])
    r = IterationResult(exit_code=0)
    r.final_text = json.dumps({"verdict": "rejected", "reason": "not done yet"})
    r.duration_s = 0.5
    r.usage = {"input": 1, "output": 1, "totalTokens": 2}
    return r


def test_peer_gate_on_post_deadline_rejection_still_holds_unchanged(tmp_path):
    """H.7's other half: "a rejecting post-deadline verdict still holds
    the run per v0.24 requirement D, unchanged." Same fixture shape as
    `tests/test_deadline.py::test_a_rejecting_verdict_after_the_deadline_
    holds_the_run` (Section J, requirement D.2), but with `peer_verdict_
    gate=True` this time -- H.5's new edge sits entirely on the VERIFIED
    branch of `_step_review` (`transitions.py`'s own comment: "this check
    runs entirely on the VERIFIED branch, ahead of the peer verdict gate
    itself"), so a REJECTING post-deadline verdict must be completely
    unaffected by the gate being on: still `exhausted`, still a null
    verdict, still the rejection's findings intact on disk (and no cure task opened).
    """
    sup = make_supervisor(
        tmp_path, run_id="task027-peer-gate-rejecting-past-deadline-unchanged",
        max_approaches=2, approach=1, iterations_used=1, prd=True,
        peer_verdict_gate=True,
        tasks=[{"id": "T1", "title": "do the thing", "successCriteria": "done",
               "status": "pending"}])
    attach_runner(sup, ScriptedRunner({
        "worker": [_worker_completes],
        "review": [_review_rejects_curable_expires_deadline_mid_flight],
    }))

    assert sup.budget_left() is True, (
        "precondition: the deadline is still ordinarily in the future "
        "before review is ever dispatched")

    state = asyncio.run(asyncio.wait_for(sup.run_job(), 30.0))

    assert sup.budget_left() is False
    assert state == "exhausted", (
        "a rejecting verdict after the deadline must hold the run in "
        f"B.1's state, unchanged by the peer gate being on, got state={state!r}")
    assert sup._budget_exhausted_tail is True

    status = sup.run.read_status()
    assert status["state"] == "exhausted"
    assert status["verdict"] is None
    assert status.get("pendingTerminalState") is None
    assert status.get("endedAt") is None, "a held run has not ended"
    assert status.get("verdictGateResidual") is None

    findings_list = findings.read_findings_json(sup.run.findings_json_file)
    assert findings_list is not None and len(findings_list) == 1
    assert findings_list[0]["clause"] == "task 027 fixture clause unmet"

    # v0.36 requirement B, ladder step 1: the budget is spent, so the
    # rejection opens no cure task (the findings above stay on disk).
    tasks = sup.run.read_tasks()["tasks"]
    assert [t for t in tasks if str(t.get("id", "")).startswith("cure-")] == []

    # The peer gate's own handler was never dispatched here either -- the
    # rejection ladder runs entirely before the gate is ever consulted.
    assert sup.runner.calls.get("verdict_gate", 0) == 0
