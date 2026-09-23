"""Task 027 (PRD requirement H.5, #264, and v0.24's own D.1 CONFIRMED
contradiction petition cure-01-04): with `peer_verdict_gate` enabled, a
post-deadline PASSING review verdict must reach a decisive conclusion
without dispatching a new iteration past the boundary, instead of holding
`timed_out` with a null verdict the way v0.24 shipped it.

v0.24's own probes -- `tests/test_deadline.py::
test_peer_verdict_gate_verified_review_after_the_deadline_holds_pending_the_gate`
and its resume sibling -- are LEFT UNCHANGED here: they pin the CONFIRMED
contradiction as history (the behaviour a petition left standing, not a
bug this task silently overwrites without a trace). H.5 grants a way out
of that contradiction that did not exist when those probes were written --
one new declared transition-table edge (`transitions.py`'s
`PEER_VERDICT_GATE_DELTA["review"]`, guard
`verified_peer_verdict_gate_deferred_past_deadline`) -- and this module's
own two probes below exercise THAT edge specifically, never by mutating
the older probes' fixtures.

Mechanism chosen: H.5's option (b) -- "the run concludes on the review
verdict alone, with the gate's non-dispatch disclosed in the record as a
named residual rather than as a null verdict" -- recorded in this task's
own commit message. Option (a) (running the gate inside review's own
iteration) would require collapsing two independently-dispatched blind hat
calls into one, which does not fit this engine's existing one-hat-per-
iteration shape without a restructuring well past this task's scope.

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


def test_peer_gate_on_post_deadline_pass_concludes_instead_of_holding(tmp_path):
    """The inverse of v0.24's own parked case
    (`test_peer_verdict_gate_verified_review_after_the_deadline_holds_
    pending_the_gate`): same fixture shape (`peer_verdict_gate=True`,
    review renders `verified` after the deadline expired mid-flight), but
    the run must now conclude `succeeded` -- never `timed_out` -- with the
    peer gate's own handler never called (deferred, not rubber-stamped),
    and a named residual disclosing exactly that in the terminal record.
    """
    sup = make_supervisor(
        tmp_path, run_id="task027-peer-gate-passing-past-deadline-concludes",
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
    assert state == "succeeded", (
        "a peer-gate-on post-deadline PASSING verdict must conclude "
        f"decisively rather than hold with a null verdict, got state={state!r}")

    status = sup.run.read_status()
    assert status["state"] == "succeeded"
    assert status["verdict"] in ("verified", "verified-with-residuals")
    assert status.get("pendingTerminalState") is None
    assert status.get("endedAt") is not None, "a concluded run has ended"

    # The peer gate's own handler was never dispatched -- deferred, never
    # rubber-stamped, exactly as H.5's own text (both mechanisms below the
    # "what is NOT acceptable" line) requires.
    assert sup.runner.calls.get("verdict_gate", 0) == 0

    # The gate's non-dispatch is disclosed as a NAMED residual in the
    # record, per H.5's own wording ("disclosed in the record as a named
    # residual rather than as a null verdict") -- never silently absent.
    residual = status.get("verdictGateResidual")
    assert isinstance(residual, dict), (
        f"expected a disclosed verdictGateResidual object, got {residual!r}")
    assert residual.get("gate") == "peer_verdict_gate"
    assert residual.get("reason")

    # Wrote no resume position: review positively rendered a judgement
    # this approach.
    assert read_events(sup.run.root, types=("review_deadline_unreviewed",)) == []


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
    unaffected by the gate being on: still `timed_out`, still a null
    verdict, still the rejection's findings and cure task intact on disk.
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
    assert state == "timed_out", (
        "a rejecting verdict after the deadline must hold the run in "
        f"B.1's state, unchanged by the peer gate being on, got state={state!r}")
    assert sup._budget_exhausted_tail is True

    status = sup.run.read_status()
    assert status["state"] == "timed_out"
    assert status["verdict"] is None
    assert status.get("pendingTerminalState") is None
    assert status.get("endedAt") is None, "a held run has not ended"
    assert status.get("verdictGateResidual") is None, (
        "the residual is only ever written on the deferred-PASS edge -- a "
        "rejection never reaches it")

    findings_list = findings.read_findings_json(sup.run.findings_json_file)
    assert findings_list is not None and len(findings_list) == 1
    assert findings_list[0]["clause"] == "task 027 fixture clause unmet"

    tasks = sup.run.read_tasks()["tasks"]
    cure_tasks = [t for t in tasks if str(t.get("id", "")).startswith("cure-")]
    assert len(cure_tasks) == 1
    assert cure_tasks[0]["status"] == "pending"

    # The peer gate's own handler was never dispatched here either -- the
    # rejection ladder runs entirely before the gate is ever consulted.
    assert sup.runner.calls.get("verdict_gate", 0) == 0
