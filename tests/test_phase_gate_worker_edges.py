"""PRD v0.37 requirements B.6 and B.7: while a gate before the last phase is
not `passed`, a worker's `plan_complete` verdict is refused (the refusal names
the phase), and each of the five `worker -> review` edges in
`transitions.TABLE["worker"]` dispatches the open phase's review (scope
"phase") instead of the final review, carrying the same edge reason.
`stagnation_exhausted` still archives and replans the approach.

`LoopSupervisor._step_worker` is driven with fake hats over a hand-seeded
`tasks.json`; nothing sleeps."""

from __future__ import annotations

import asyncio
import json

import pytest

from ralphd.engine import transitions
from ralphd.engine.runner import IterationResult
from ralphd.engine.state import atomic_write_json
from tests.conftest import (ScriptedRunner, make_supervisor,
                            stub_contract_reask)

EDGES = ("verdict_ready", "blocked_with_failures", "doomed_plan_unverifiable",
         "petition_held_no_task_bound", "unsatisfiable_and_nothing_left")


def _ok(text: str) -> IterationResult:
    r = IterationResult(exit_code=0)
    r.final_text = text
    r.duration_s = 1.0
    r.usage = {"input": 1, "output": 1, "totalTokens": 2}
    return r


def _reply(**payload):
    return lambda sup, prompt="": _ok(json.dumps(payload))


def _phases(one_gate="closed"):
    return [{"id": "one", "title": "Phase one", "reviewFocus": "f1",
             "gate": one_gate, "records": []},
            {"id": "two", "title": "Phase two", "reviewFocus": "f2",
             "gate": "closed", "records": []}]


class _Review:
    """The review fake: records the prompt and the iteration meta it ran
    under, and passes."""

    def __init__(self):
        self.seen: list[dict] = []

    def __call__(self, sup, prompt=""):
        n = sup.iterations_used
        meta = json.loads((sup.run.iteration_dir(n) / "meta.json").read_text())
        self.seen.append({"meta": meta, "prompt": prompt})
        return _ok(json.dumps({"verdict": "verified", "reason": "fine"}))


def _build(tmp_path, tasks, worker, *, phases=None, **cfg):
    review = _Review()
    sup = make_supervisor(
        tmp_path / "run", ScriptedRunner({"worker": worker,
                                          "review": [review]}),
        run_id="phase-gate-worker-edges", cfg_defaults=False,
        max_approaches=1, vigilant=False, on_complete="exit",
        infra_retry_backoff_s=[0.0], infra_retry_backoff_max_s=0.0,
        infra_outage_budget_s=0.0, approach=1, iterations=20,
        tasks=tasks, **cfg)
    atomic_write_json(sup.run.tasks_file, {
        "version": 1, "tasks": [dict(t) for t in tasks],
        "phases": phases if phases is not None else _phases()})
    return sup, review


def _events(sup, kind):
    return [json.loads(l) for l in
            (sup.run.root / "events.jsonl").read_text().splitlines()
            if l.strip() and json.loads(l).get("type") == kind]


def _assert_redirected(sup, review, outcome, edge):
    """The edge dispatched the open phase's review, and only that."""
    assert outcome == ("advance", "worker", "no_verdict_yet"), outcome
    assert len(review.seen) == 1, "exactly one review ran, the phase review"
    seen = review.seen[0]
    assert seen["meta"]["scope"] == "phase"
    assert seen["meta"]["planPhase"] == "one"
    assert seen["meta"]["edge"] == edge
    assert "## Phase review" in seen["prompt"]
    due = _events(sup, "phase_gate.due")
    assert [(e["planPhase"], e["edge"]) for e in due] == [("one", edge)]
    assert [e["planPhase"] for e in _events(sup, "phase_gate.passed")] == ["one"]
    # the final review never ran: the loop did not take the `review` state
    assert not [e for e in _events(sup, "signal")
                if e.get("signal") == "VERIFIED"]


def test_every_listed_worker_edge_is_a_declared_worker_to_review_edge():
    review_edges = {t.guard for t in transitions.TABLE["worker"]
                    if t.target == "review"}
    assert review_edges == set(EDGES)


def test_verdict_ready_dispatches_the_open_phase_review(tmp_path):
    tasks = [{"id": "001", "title": "t1", "status": "in-progress",
              "phase": "one", "successCriteria": "c1"}]
    sup, review = _build(tmp_path, tasks, [_reply(verdict="complete",
                                                  task_id="001")])
    outcome = asyncio.run(sup._step_worker())
    _assert_redirected(sup, review, outcome, "verdict_ready")


def test_blocked_with_failures_dispatches_the_open_phase_review(tmp_path):
    tasks = [{"id": "001", "title": "t1", "status": "failed", "phase": "one",
              "successCriteria": "c1"}]
    sup, review = _build(tmp_path, tasks, [_reply(verdict="plan_complete")])
    # the phase was already reviewed by this process without opening its
    # gate, so the top-of-pass due check does not offer it again
    sup._phase_review_unresolved.add("one")
    outcome = asyncio.run(sup._step_worker())
    _assert_redirected(sup, review, outcome, "blocked_with_failures")


def test_doomed_plan_unverifiable_dispatches_the_open_phase_review(tmp_path):
    tasks = [{"id": "001", "title": "t1", "status": "failed", "phase": "one",
              "successCriteria": "c1"},
             {"id": "002", "title": "t2", "status": "pending", "phase": "one",
              "dependsOn": ["001"], "successCriteria": "c2"}]
    sup, review = _build(tmp_path, tasks, [_reply(verdict="no_verdict")])
    outcome = asyncio.run(sup._step_worker())
    _assert_redirected(sup, review, outcome, "doomed_plan_unverifiable")
    assert sup.runner.calls.get("worker", 0) == 0, (
        "a doomed plan routes before any worker iteration")


def test_petition_held_no_task_bound_dispatches_the_open_phase_review(tmp_path):
    tasks = [{"id": "001", "title": "t1", "status": "petitioned",
              "phase": "one", "successCriteria": "c1"},
             {"id": "002", "title": "t2", "status": "pending", "phase": "one",
              "dependsOn": ["001"]},
             {"id": "003", "title": "t3", "status": "pending", "phase": "one",
              "dependsOn": ["001"]}]
    sup, review = _build(tmp_path, tasks, [_reply(verdict="no_verdict")])
    outcome = asyncio.run(sup._step_worker())
    _assert_redirected(sup, review, outcome, "petition_held_no_task_bound")


def test_unsatisfiable_and_nothing_left_dispatches_the_open_phase_review(tmp_path):
    tasks = [{"id": "001", "title": "t1", "status": "in-progress",
              "phase": "one", "successCriteria": "c1"}]
    sup, review = _build(tmp_path, tasks, [_reply(
        verdict="unsatisfiable", task_id="001", reason="cannot be met")])
    outcome = asyncio.run(sup._step_worker())
    _assert_redirected(sup, review, outcome, "unsatisfiable_and_nothing_left")


@pytest.mark.parametrize("edge,tasks,worker", [
    ("verdict_ready",
     [{"id": "001", "title": "t", "status": "in-progress",
       "successCriteria": "c"}],
     _reply(verdict="complete", task_id="001")),
    ("unsatisfiable_and_nothing_left",
     [{"id": "001", "title": "t", "status": "in-progress",
       "successCriteria": "c"}],
     _reply(verdict="unsatisfiable", task_id="001", reason="no")),
])
def test_a_plan_without_phases_still_reaches_the_final_review(
        tmp_path, edge, tasks, worker):
    """Control: the redirect never applies when there are no phases."""
    sup, review = _build(tmp_path, tasks, [worker], phases=[])
    atomic_write_json(sup.run.tasks_file, {
        "version": 1, "tasks": [dict(t) for t in tasks]})
    outcome = asyncio.run(sup._step_worker())
    assert outcome == ("advance", "review", edge)
    assert review.seen == []


def test_the_open_last_phase_still_reaches_the_final_review(tmp_path):
    """Control: with every earlier gate passed, the open phase is the last,
    which has no gate; the edge goes to the final review."""
    tasks = [{"id": "001", "title": "t", "status": "in-progress",
              "phase": "two", "successCriteria": "c"}]
    sup, review = _build(tmp_path, tasks, [_reply(verdict="complete",
                                                  task_id="001")],
                         phases=_phases("passed"))
    outcome = asyncio.run(sup._step_worker())
    assert outcome == ("advance", "review", "verdict_ready")
    assert review.seen == []


def _all_done_plan():
    return [{"id": "001", "title": "t1", "status": "completed",
             "phase": "one", "successCriteria": "c1"},
            {"id": "002", "title": "t2", "status": "completed",
             "phase": "two", "successCriteria": "c2"}]


def test_plan_complete_is_refused_while_a_gate_before_the_last_phase_is_not_passed(tmp_path):
    sup, _review = _build(tmp_path, _all_done_plan(),
                          [_reply(verdict="plan_complete")])
    data = sup.run.read_tasks()
    # every task is terminal, so the only reason left to refuse is the gate
    assert sup._outstanding_for_completion(data) == []
    errors = sup._apply_worker_plan_complete_report(
        {"verdict": "plan_complete"}, None, data)
    assert len(errors) == 1
    assert "'one'" in errors[0] and "not passed" in errors[0]
    # once the gate passed, the same claim is honoured
    data["phases"][0]["gate"] = "passed"
    assert sup._apply_worker_plan_complete_report(
        {"verdict": "plan_complete"}, None, data) == []


def test_a_plan_complete_reply_reaches_the_ladder_with_the_phase_named(tmp_path):
    sup, review = _build(tmp_path, _all_done_plan(),
                         [_reply(verdict="plan_complete")])
    sup._phase_review_unresolved.add("one")
    asked = stub_contract_reask(sup)
    outcome = asyncio.run(sup._step_worker())
    assert asked, "the refused claim is carried back to the same worker"
    assert any("'one'" in e for errors in asked for e in errors)
    assert review.seen == [], "the refusal never runs the final review"
    assert outcome[:2] != ("advance", "review")


def test_stagnation_exhausted_on_a_phased_plan_still_archives_and_replans(tmp_path):
    tasks = [{"id": "001", "title": "t1", "status": "pending", "phase": "one",
              "successCriteria": "c1"},
             {"id": "002", "title": "t2", "status": "pending", "phase": "two",
              "successCriteria": "c2"}]
    sup, review = _build(tmp_path, tasks, [_reply(verdict="no_verdict")])
    outcome = asyncio.run(sup._step_worker())
    assert outcome == ("advance", "replan", "stagnation_exhausted")
    assert review.seen == [], "a stagnant approach is not sent to review"
    assert (sup.run.root / "approaches" / "01").is_dir(), "archived"
    assert transitions.is_legal("worker", "replan", vigilant=False)
