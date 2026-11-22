"""v0.38 B.2-B.4 (#320): the review state key holds on every edge into the
final judges, not only the petition-held one.

`tests/test_review_state_key.py` covers the key itself and the board shape.
This module covers the rest of B:

* one unchanged-key park test for each of the five `worker -> review` edges
  in `transitions.TABLE["worker"]` (`verdict_ready`, `blocked_with_failures`,
  `doomed_plan_unverifiable`, `petition_held_no_task_bound` and
  `unsatisfiable_and_nothing_left`): the worker takes the edge, the final
  review answers VERIFIED and defers to the worker, the same edge is taken
  again on the same HEAD and the same task statuses, and the review hat is
  not dispatched a second time -- the run parks `verified_unchanged`;
* the final peer verdict gate shares the key: a gate dispatch on the key its
  own earlier verdict already judged is held the same way;
* a phased plan's phase review is dispatched as before: the key applies to
  the final review and the final peer gate only.

Driven through the real `_step_worker` / `_step_review` / `_step_verdict_gate`
/ `_step_paused` with fake hats and a fake clock; nothing sleeps.
"""

from __future__ import annotations

import asyncio
import copy
import json

import pytest

from ralphd.engine import findings, transitions
from ralphd.engine.runner import IterationResult
from ralphd.engine.state import atomic_write_json
from tests.conftest import ScriptedRunner, attach_runner, make_supervisor

FAKE_NOW = 1_800_000_000.0
HEAD = "a" * 40
BLOCKER = "steering task 099 is not terminal"

EDGES = ("verdict_ready", "blocked_with_failures", "doomed_plan_unverifiable",
         "petition_held_no_task_bound", "unsatisfiable_and_nothing_left")


def _ok(payload: dict) -> IterationResult:
    r = IterationResult(exit_code=0)
    r.final_text = json.dumps(payload)
    r.duration_s = 1.0
    r.usage = {"input": 1, "output": 1, "totalTokens": 2}
    return r


def _reply(**payload):
    return lambda sup, prompt="": _ok(payload)


def _bounded(coro):
    return asyncio.run(asyncio.wait_for(coro, 30.0))


def _task(tid, status, **extra):
    return {"id": tid, "title": f"task {tid}", "status": status,
            "successCriteria": f"criterion of {tid}", **extra}


def _supervisor(tmp_path, tasks, handlers, *, phases=None, **cfg):
    sup = make_supervisor(
        tmp_path / "run", run_id="review-state-key-edges", cfg_defaults=False,
        max_approaches=1, vigilant=False, on_complete="exit", iterations=60,
        route_task_selection=False,
        infra_retry_backoff_s=[0.0], infra_retry_backoff_max_s=0.0,
        infra_outage_budget_s=0.0, approach=1, tasks=tasks, **cfg)
    if phases is not None:
        atomic_write_json(sup.run.tasks_file, {
            "version": 1, "tasks": [dict(t) for t in tasks],
            "phases": phases})
    sup._now_epoch = lambda: FAKE_NOW          # the fake clock
    sup._workspace_head_sha = lambda: HEAD     # a fixed workspace HEAD
    runner = attach_runner(sup, ScriptedRunner(handlers))
    return sup, runner


def _hold_terminal_success(sup):
    """Something outside the plan's own statuses still blocks terminal
    success (an unconsumed steering message, a steering-origin task): the
    shape that makes a VERIFIED defer to the worker."""
    sup._terminal_success_blockers = lambda: [BLOCKER]


# ---------------------------------------------------------------------------
# B.2: one unchanged-key park per `worker -> review` edge
# ---------------------------------------------------------------------------

def _verdict_ready():
    return ([_task("001", "in-progress")],
            [_reply(verdict="complete", task_id="001")])


def _blocked_with_failures():
    return ([_task("001", "failed")], [_reply(verdict="plan_complete")])


def _doomed_plan_unverifiable():
    return ([_task("001", "failed"),
             _task("002", "pending", dependsOn=["001"])],
            [_reply(verdict="no_verdict")])


def _petition_held_no_task_bound():
    return ([_task("001", "petitioned"),
             _task("002", "pending", dependsOn=["001"]),
             _task("003", "pending", dependsOn=["001"])],
            [_reply(verdict="no_verdict")])


def _unsatisfiable_and_nothing_left():
    return ([_task("001", "in-progress")],
            [_reply(verdict="unsatisfiable", task_id="001",
                    reason="cannot be met")])


SHAPES = {
    "verdict_ready": _verdict_ready,
    "blocked_with_failures": _blocked_with_failures,
    "doomed_plan_unverifiable": _doomed_plan_unverifiable,
    "petition_held_no_task_bound": _petition_held_no_task_bound,
    "unsatisfiable_and_nothing_left": _unsatisfiable_and_nothing_left,
}


@pytest.mark.parametrize("edge", EDGES)
def test_an_unchanged_review_state_key_parks_on_every_worker_to_review_edge(
        tmp_path, edge):
    # the edge list this module covers is the table's own: a
    # `worker -> review` edge added later is a missing case here
    assert {t.guard for t in transitions.TABLE["worker"]
            if t.target == "review"} == set(EDGES) == set(SHAPES)
    tasks, worker = SHAPES[edge]()
    sup, runner = _supervisor(
        tmp_path, tasks,
        {"worker": worker, "review": [_reply(verdict="verified")]})
    _hold_terminal_success(sup)
    seeded = copy.deepcopy(sup.run.read_tasks())

    # first visit: the worker takes the edge, the review judges the state
    # and, with something still blocking terminal success, defers
    if edge == "petition_held_no_task_bound":
        # v0.38 A.5 rewrote this edge's first visit: a hold the engine cannot
        # settle parks `petition_hold` before any review, so this edge is
        # only taken for a hold on a state a deferred VERIFIED already
        # judged. The first judgement is therefore dispatched directly; the
        # second visit below is the edge itself (covered, for the first
        # visit's park, by tests/test_petition_hold_chain_and_unfiled.py).
        pass
    else:
        first = _bounded(sup._step_worker())
        assert first == ("advance", "review", edge)
    deferred = _bounded(sup._step_review())
    assert deferred[:2] == ("advance", "worker"), deferred
    assert runner.calls["review"] == 1
    worker_calls = runner.calls.get("worker", 0)

    # second visit: the plan stands exactly as it did when the worker took
    # the edge the first time (same HEAD, same task statuses)
    sup.run.write_tasks(copy.deepcopy(seeded))
    second = _bounded(sup._step_worker())
    assert second == ("advance", "review", edge)
    if edge == "petition_held_no_task_bound":
        assert runner.calls.get("worker", 0) == worker_calls, (
            "a hold on an already-judged state spends no no-task iteration")
    parked = _bounded(sup._step_review())

    assert parked == ("advance", "paused", "verified_unchanged")
    assert runner.calls["review"] == 1, "the review hat was not dispatched again"
    status = sup.run.read_status()
    assert status["state"] == "awaiting_input"
    assert status["pauseReason"] == "verified_unchanged"
    assert status["inputQuestion"]["kind"] == "verified_unchanged"
    assert status["inputQuestion"]["judge"] == "review"
    assert status["inputQuestion"]["reviewStateKey"]["head"] == HEAD


# ---------------------------------------------------------------------------
# B.3: the final peer verdict gate shares the key
# ---------------------------------------------------------------------------

def _mixed_gate_findings():
    return [
        {"severity": "blocking", "curable": True,
         "clause": "requirement B.3 curable clause (#320)",
         "evidence": "the peer gate found a fixable gap",
         "cured_when": "a cure task adds the missing behaviour"},
        {"severity": "blocking", "curable": False,
         "clause": "requirement B.3 incurable clause (#320)",
         "evidence": "an environment fact no cure task can change"},
    ]


def _gate_supervisor(tmp_path):
    sup, runner = _supervisor(
        tmp_path, [_task("001", "completed")],
        {"verdict_gate": [_reply(verdict="rejected",
                                 reason="one finding is incurable")]},
        peer_verdict_gate=True, operator_input_needed_pause=True)
    findings.write_findings_json(sup.run.verdict_gate_findings_json_file,
                                 _mixed_gate_findings())
    return sup, runner


async def _release_with_nothing_changed(sup):
    """`_step_paused` parked on a gate question, then the park released the
    way an unpause does: nothing changed."""
    async def released(*a, **k):
        return False
    sup._wait_for_answer_or_expiry = released
    return await sup._step_paused()


def test_the_final_peer_gate_is_not_dispatched_twice_on_an_unchanged_key(
        tmp_path):
    sup, runner = _gate_supervisor(tmp_path)

    # the gate's own earlier pass: it judged this state and parked the run
    first = _bounded(sup._step_verdict_gate())
    assert first[:2] == ("advance", "paused"), first
    assert runner.calls["verdict_gate"] == 1
    assert sup.run.read_status()["inputQuestion"]["judge"] == "verdict_gate"

    # released with nothing changed: the run re-enters the gate ...
    resumed = _bounded(_release_with_nothing_changed(sup))
    assert resumed == ("advance", "verdict_gate", "paused_resumed_verdict_gate")
    # ... which does not judge the same state twice
    held = _bounded(sup._step_verdict_gate())

    assert held == ("advance", "paused", "verified_unchanged")
    assert runner.calls["verdict_gate"] == 1, "the gate hat was not dispatched"
    status = sup.run.read_status()
    assert status["state"] == "awaiting_input"
    assert status["pauseReason"] == "verified_unchanged"
    question = status["inputQuestion"]
    assert question["kind"] == "verified_unchanged"
    assert question["judge"] == "verdict_gate"
    assert "peer verdict gate" in question["text"]
    assert question["reviewStateKey"]["head"] == HEAD


def test_an_answer_with_nothing_changed_re_parks_the_final_peer_gate(tmp_path):
    sup, runner = _gate_supervisor(tmp_path)
    _bounded(sup._step_verdict_gate())
    _bounded(_release_with_nothing_changed(sup))
    assert _bounded(sup._step_verdict_gate())[2] == "verified_unchanged"

    async def answer_the_park():
        parked = asyncio.ensure_future(sup._step_paused())
        for _ in range(200):
            await asyncio.sleep(0.01)
            if sup.run.read_status().get("state") == "awaiting_input":
                break
        sup.answer()
        return await parked

    # the park is released: the run re-enters the GATE, never the review
    assert _bounded(answer_the_park()) == (
        "advance", "verdict_gate", "paused_resumed_verdict_gate")
    again = _bounded(sup._step_verdict_gate())

    assert again == ("advance", "paused", "verified_unchanged")
    assert runner.calls["verdict_gate"] == 1


# ---------------------------------------------------------------------------
# B.4: a phased plan's phase review is dispatched as before
# ---------------------------------------------------------------------------

def test_a_phased_plan_s_phase_review_is_dispatched_as_before(tmp_path):
    """The key applies to the final review and the final peer gate only. On
    a state a deferred VERIFIED already judged, the open phase's review is
    still dispatched, from the same `worker -> review` edge."""
    seen: list[dict] = []

    def phase_review(sup, prompt=""):
        n = sup.iterations_used
        meta = json.loads((sup.run.iteration_dir(n) / "meta.json").read_text())
        seen.append(meta)
        return _ok({"verdict": "verified", "reason": "fine"})

    phases = [{"id": "one", "title": "Phase one", "reviewFocus": "f1",
               "gate": "closed", "records": []},
              {"id": "two", "title": "Phase two", "reviewFocus": "f2",
               "gate": "closed", "records": []}]
    tasks = [_task("001", "failed", phase="one")]
    sup, runner = _supervisor(
        tmp_path, tasks,
        {"worker": [_reply(verdict="plan_complete")], "review": [phase_review]},
        phases=phases)
    sup._phase_review_unresolved.add("one")
    # a deferred VERIFIED already judged exactly this state
    key = sup._review_state_key()
    sup._record_review_dispatch_key(1, key)
    sup._record_deferred_verified_key(1, [BLOCKER])
    entry = sup.run.read_status()["reviewStateKeys"]["1"]
    assert entry["verifiedDeferred"]["key"] == key

    outcome = _bounded(sup._step_worker())

    assert outcome == ("advance", "worker", "no_verdict_yet"), outcome
    assert len(seen) == 1, "the phase review was dispatched"
    assert seen[0]["scope"] == "phase"
    assert seen[0]["planPhase"] == "one"
    assert seen[0]["edge"] == "blocked_with_failures"
    status = sup.run.read_status()
    assert status.get("state") != "awaiting_input"
    assert status.get("pauseReason") != "verified_unchanged"
