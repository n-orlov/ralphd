"""v0.38 A.4/A.5 (#306, #320): a held petitioned task is adjudicated at once
and, when the judge cannot settle it, parks the run for the operator.

When every actionable remaining task waits only on an unsettled `petitioned`
task, `_step_worker` runs `_maybe_run_midrun_adjudication_episode` on the
FIRST hold, before any no-task worker iteration:

* a CONFIRMED outcome settles the petitioned task and the dependents are
  selected in the same pass (no no-task worker iteration between the
  episode and the first dependent's claim);
* an episode that adjudicates nothing, or leaves the hold unchanged, parks
  the run `awaiting_input` (`inputQuestion.kind: petition_hold`) instead of
  routing to the final review. The question names each petitioned task, its
  petition, its dependents and both operator answers.

Driven through the real `LoopSupervisor._step_worker` with fake hats (a
`ScriptedRunner`) and a fake clock (`_now_epoch`); the park's wait is replaced
by a recorder, so nothing sleeps.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from ralphd.engine import transitions
from ralphd.engine.runner import IterationResult
from tests.conftest import ScriptedRunner, attach_runner, make_supervisor

HELD = "001"
DEP_A = "002"
DEP_B = "003"
TITLE = "the impossible one"
OBLIGATION = f"Task {HELD}: {TITLE}"
EVIDENCE = "criterion 2 refers to its own not-yet-existing sha"
FAKE_NOW = 1_800_000_000.0


class _Parked(Exception):
    """Raised by the stubbed park wait: the run reached `awaiting_input`."""


def _ok(payload: dict | str) -> IterationResult:
    r = IterationResult(exit_code=0)
    r.final_text = payload if isinstance(payload, str) else json.dumps(payload)
    r.duration_s = 1.0
    r.usage = {"input": 1, "output": 1, "totalTokens": 2}
    return r


def _plan() -> list[dict]:
    return [
        {"id": HELD, "title": TITLE, "status": "petitioned",
         "successCriteria": "criterion 2 holds"},
        {"id": DEP_A, "title": "build on it", "status": "pending",
         "dependsOn": [HELD], "successCriteria": "a builds"},
        {"id": DEP_B, "title": "also build on it", "status": "pending",
         "dependsOn": [HELD], "successCriteria": "b builds"},
    ]


def _supervisor(tmp_path):
    sup = make_supervisor(
        tmp_path / "run", run_id="petition-hold-prompt", cfg_defaults=False,
        max_approaches=1, vigilant=False, on_complete="exit", iterations=30,
        route_task_selection=False,
        infra_retry_backoff_s=[0.0], infra_retry_backoff_max_s=0.0,
        infra_outage_budget_s=0.0, approach=1, tasks=_plan())
    sup._now_epoch = lambda: FAKE_NOW          # the fake clock
    reports = sup.workspace / "artifacts" / "reports"
    reports.mkdir(parents=True)
    (reports / "v0.38-petitions.md").write_text(
        "# v0.38 petitions\n\n"
        f"## Petition: {OBLIGATION}\n\n"
        f"**Obligation**: {OBLIGATION}\n"
        "**Type**: self-referential\n"
        f"**Evidence**: {EVIDENCE}\n")
    return sup


def _judge(adjudications: list[dict]):
    def step(sup, prompt):
        payload = {"verdict": "rejected", "reason": "adjudicating the hold",
                   "petition_adjudications": adjudications}
        return _ok("adjudicating\n" + json.dumps(payload))
    return step


def _no_task_worker(sup, prompt):
    return _ok({"verdict": "no_verdict"})


def _completing_worker(seen: list):
    """Completes whichever task the harness claimed, recording the plan as it
    stood when this (first) worker iteration began."""
    def step(sup, prompt):
        doc = sup.run.read_tasks()
        claimed = [t["id"] for t in doc["tasks"]
                   if t.get("status") == "in-progress"]
        seen.append({"claimed": claimed,
                     "held": next(t["status"] for t in doc["tasks"]
                                  if t["id"] == HELD)})
        for t in doc["tasks"]:
            if t["id"] in claimed:
                t["status"] = "completed"
        sup.run.write_tasks(doc)
        return _ok({"verdict": "complete", "task_id": claimed[0]})
    return step


def _park_recorder(sup):
    """Replace the park's wait: record `status.json` as the run parked on it
    and stop the pass by raising instead of sleeping."""
    snapshot: dict = {}

    async def parked():
        snapshot.update(sup.run.read_status())
        raise _Parked()

    sup._park_until_resumed = parked
    return snapshot


def _run_to_park(sup, runner):
    snapshot = _park_recorder(sup)
    with pytest.raises(_Parked):
        asyncio.run(sup._step_worker())
    return snapshot


def test_first_hold_runs_the_episode_before_any_no_task_worker_iteration(tmp_path):
    sup = _supervisor(tmp_path)
    runner = attach_runner(sup, ScriptedRunner({
        "review": [_judge([])], "worker": [_no_task_worker]}))

    _run_to_park(sup, runner)

    assert runner.phases_seen, "the episode never ran"
    assert runner.phases_seen[0] == "review", runner.phases_seen
    assert "worker" not in runner.phases_seen, (
        "a no-task worker iteration was spent on the held plan: "
        f"{runner.phases_seen!r}")


def test_a_confirmed_outcome_unblocks_the_dependents_in_the_same_pass(tmp_path):
    sup = _supervisor(tmp_path)
    seen: list = []
    runner = attach_runner(sup, ScriptedRunner({
        "review": [_judge([{"obligation": OBLIGATION,
                            "adjudication": "CONFIRMED"}])],
        "worker": [_completing_worker(seen)]}))
    _park_recorder(sup)            # a regression parks (and fails) at once

    asyncio.run(sup._step_worker())

    # the episode came first and the very next worker iteration already had a
    # dependent claimed: nothing no-task ran in between.
    assert runner.phases_seen[0] == "review", runner.phases_seen
    assert runner.phases_seen[1] == "worker", runner.phases_seen
    assert seen[0]["claimed"] == [DEP_A], seen
    # CONFIRMED settles the petitioned task; it stays `petitioned`.
    assert seen[0]["held"] == "petitioned"
    statuses = {t["id"]: t["status"] for t in sup.run.read_tasks()["tasks"]}
    assert statuses[DEP_A] == "completed" and statuses[DEP_B] == "completed"
    assert runner.phases_seen.count("review") == 1


def test_a_fruitless_episode_parks_awaiting_input_without_routing_to_review(tmp_path):
    sup = _supervisor(tmp_path)
    runner = attach_runner(sup, ScriptedRunner({
        "review": [_judge([])], "worker": [_no_task_worker]}))

    snapshot = _run_to_park(sup, runner)

    assert snapshot["state"] == "awaiting_input"
    question = snapshot["inputQuestion"]
    assert question["kind"] == "petition_hold"
    assert question["expiresAt"] is None and question["onTimeout"] == "never"
    # not a review hand-off: the worker never advanced to `review`.
    assert snapshot.get("pauseReason") == "petition_hold"
    assert "worker" not in runner.phases_seen
    events = [json.loads(x) for x in
              (sup.run.root / "events.jsonl").read_text().splitlines() if x.strip()]
    assert any(e.get("type") == "input.parked" for e in events)
    assert not any(e.get("reason") == "petition_held_no_task_bound"
                   for e in events)

    held = question["heldTasks"]
    assert [h["taskId"] for h in held] == [HELD]
    assert held[0]["dependents"] == [DEP_A, DEP_B]
    assert held[0]["petition"]["obligation"] == OBLIGATION
    assert held[0]["petition"]["evidence"] == EVIDENCE
    text = question["text"]
    for needle in (HELD, OBLIGATION, EVIDENCE, DEP_A, DEP_B,
                   f"ralphctl amend {sup.cfg.run_id} --waive {HELD}",
                   f"ralphctl amend {sup.cfg.run_id} {HELD} --status "
                   "validated|skipped"):
        assert needle in text, (needle, text)


def test_a_hold_the_adjudication_leaves_unchanged_parks(tmp_path):
    """A REFUTED outcome for a different obligation changes nothing for the
    held task: the hold is unchanged, so the run parks."""
    sup = _supervisor(tmp_path)
    runner = attach_runner(sup, ScriptedRunner({
        "review": [_judge([{"obligation": "Task 099: some other thing",
                            "adjudication": "REFUTED",
                            "remedy": "do the thing"}])],
        "worker": [_no_task_worker]}))

    snapshot = _run_to_park(sup, runner)

    assert snapshot["state"] == "awaiting_input"
    assert snapshot["inputQuestion"]["kind"] == "petition_hold"
    assert "worker" not in runner.phases_seen


def test_the_park_is_released_by_an_answer_and_the_loop_reparks_an_unmoved_hold(tmp_path):
    sup = _supervisor(tmp_path)
    runner = attach_runner(sup, ScriptedRunner({
        "review": [_judge([])], "worker": [_no_task_worker]}))
    _run_to_park(sup, runner)

    from ralphd.engine.answer import AnswerRefused
    with pytest.raises(AnswerRefused):
        sup.answer(waive=["F1"])
    record = sup.answer()
    assert record["outcome"] == "petition_hold_released"
    assert sup.run.read_status()["state"] == "running"
    reviews_before = runner.phases_seen.count("review")

    _run_to_park(sup, runner)             # still held: parks again, no new judge
    assert runner.phases_seen.count("review") == reviews_before
    assert "worker" not in runner.phases_seen


def test_paused_declares_the_petition_hold_resume_edge():
    assert transitions.is_legal("paused", "worker", vigilant=False)
    assert "paused_resumed_petition_hold" in transitions.guard_names(
        "paused", vigilant=False)


def test_a_refuted_outcome_fails_the_task_and_does_not_park(tmp_path):
    """A REFUTED adjudication of the held task moves it to `failed` with its
    remedy. The petition hold is gone (the blocker id is unchanged, but it is
    now a failed dependency, not a petitioned one), so the run re-selects in
    the same pass instead of parking on a hold that no longer exists."""
    sup = _supervisor(tmp_path)
    remedy = "rewrite criterion 2 so it names an existing sha"
    runner = attach_runner(sup, ScriptedRunner({
        "review": [_judge([{"obligation": OBLIGATION,
                            "adjudication": "REFUTED",
                            "remedy_type": "form",
                            "remedy": remedy}])],
        "worker": [_no_task_worker]}))
    snapshot = _park_recorder(sup)

    try:
        asyncio.run(asyncio.wait_for(sup._step_worker(), 30.0))
    except _Parked:
        pytest.fail(f"parked on a hold the REFUTED outcome removed: {snapshot!r}")

    held = next(t for t in sup.run.read_tasks()["tasks"] if t["id"] == HELD)
    assert held["status"] == "failed"
    assert remedy in json.dumps(held)
    status = sup.run.read_status()
    assert status.get("state") != "awaiting_input"
    assert status.get("pauseReason") != "petition_hold"
    assert runner.phases_seen.count("review") == 1, runner.phases_seen
