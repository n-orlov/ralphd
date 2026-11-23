"""v0.39 (#321) B.2 / C.2: reflection is held to the spend caps like every
other hat.

The run is driven end to end (`LoopSupervisor.run_job`) with fake hats whose
iterations return injected costs, so no test spends a real dollar or waits out
a clock. Each run reaches a settled outcome (`succeeded`), and then the
reflection is refused before it dispatches, interrupted while it runs, or
refused on an internal retry or a contract re-ask. The cap's outcome rules --
`exhausted` with the spend reason for an overall cap or the last approach,
archive-and-replan for a per-approach cap on any other approach -- decide the
run instead of the outcome the job had settled.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from ralphd.engine.runner import IterationResult
from ralphd.engine.state import RunDir, atomic_write_json
from tests.conftest import scripted_supervisor


def _result(text: str = "ok", cost: float = 0.0, **fields) -> IterationResult:
    r = IterationResult(exit_code=0)
    r.final_text = text
    r.duration_s = 5.0
    r.usage = {"input": 10, "output": 5, "totalTokens": 15, "costUSD": cost}
    for key, value in fields.items():
        setattr(r, key, value)
    return r


def _plan(sup, prompt=""):
    atomic_write_json(sup.run.root / "plan-proposal.json", {
        "version": 1, "tasks": [{"id": "001", "title": "task 001",
                                 "successCriteria": "task 001 works"}]})
    return _result("planned\n" + json.dumps(
        {"proposal_path": "plan-proposal.json", "summary": "planned"}))


def _complete(sup, prompt=""):
    task_id = sup.run.read_tasks()["tasks"][0]["id"]
    return _result("done\n" + json.dumps(
        {"verdict": "complete", "task_id": task_id}))


def _verified(cost=0.0):
    """A review that passes the run, at `cost` on approach 1 only."""
    return lambda sup, prompt="": _result(
        "all good\n" + json.dumps({"verdict": "verified"}),
        cost if sup._approach == 1 else 0.0)


def _report(sup):
    out = sup.run.artifacts_dir / "reflection"
    out.mkdir(parents=True, exist_ok=True)
    (out / "report.md").write_text("# post-mortem\n")


def _reflected(cost=0.0):
    """A reflection that writes its report and answers, at `cost` on approach 1."""
    def step(sup, prompt=""):
        _report(sup)
        return _result("reflected\n" + json.dumps({"notes": "n"}),
                       cost if sup._approach == 1 else 0.0)
    return step


def _spend_interrupted(cost):
    """A reflection a live spend cap cut short: claim discarded, cost counted."""
    def step(sup, prompt=""):
        return _result("", cost, timed_out=True, spend_interrupted=True)
    return step


def _caps(**levels) -> dict:
    return {level: {hat: {"usd": usd, "source": "flag"}
                    for hat, usd in entries.items()}
            for level, entries in levels.items()}


_supervisor = scripted_supervisor(
    run_id="spend-reflect", iterations=60, vigilant=False, on_complete="exit",
    reflect=True, infra_outage_budget_s=1000.0, infra_retry_backoff_s=[0.0],
    infra_retry_backoff_max_s=0.0)


def _run(tmp_path, handlers, *, seed=None, **cfg):
    root = tmp_path / "run"
    sup = _supervisor(root, {"planning": [_plan], "worker": [_complete],
                             "review": [_verified()], **handlers}, **cfg)
    RunDir(root=root).update_status(state="starting")
    if seed:
        sup.run.update_status(approach=1)
        for phase, cost in seed.items():
            sup.run.record_spend(1, phase, {"costUSD": cost})
    final = asyncio.run(asyncio.wait_for(sup.run_job(), 120))
    return sup, final, json.loads((root / "status.json").read_text())


def _events(sup, type_: str) -> list[dict]:
    path = Path(sup.run.root) / "events.jsonl"
    rows = [json.loads(x) for x in path.read_text().splitlines() if x.strip()]
    return [e for e in rows if e.get("type") == type_]


def _held_exhausted(sup, final, status, budget, *, refused="reflect"):
    assert final == "exhausted", status
    assert status["state"] == "exhausted"
    assert status["exhaustedBudget"] == budget
    assert budget in status["reason"]
    assert status["verdict"] is None
    assert status.get("pendingTerminalState") is None
    assert "endedAt" not in status          # a hold, not an ending
    [event] = _events(sup, "spend.exhausted")
    assert (event["action"], event["refusedHat"]) == ("exhausted", refused)
    return event


# -- pre-dispatch: a spent cap refuses the reflection ---------------------------

def test_a_spent_overall_reflect_cap_refuses_the_reflection_and_holds_the_run_exhausted(
        tmp_path):
    sup, final, status = _run(
        tmp_path, {"reflect": [_reflected()]},
        spend_caps=_caps(overall={"reflect": 1.0}), seed={"reflect": 1.2},
        max_approaches=3)
    event = _held_exhausted(sup, final, status, "spend:reflect")
    assert (event["hat"], event["level"]) == ("reflect", "overall")
    assert "reflect" not in sup.runner.calls      # zero dispatches past the cap
    # not silently skipped as success: the refusal is on the record
    assert status["reflect"]["attempted"] is False
    assert "spend cap" in status["reflect"]["skipped"]
    assert len(_events(sup, "reflect_skipped")) == 1


def test_a_spent_overall_total_cap_refuses_the_reflection_and_holds_the_run_exhausted(
        tmp_path):
    # the review that passed the run was the iteration that spent `total`
    sup, final, status = _run(
        tmp_path, {"review": [_verified(1.2)], "reflect": [_reflected()]},
        spend_caps=_caps(overall={"total": 1.0}), max_approaches=3)
    event = _held_exhausted(sup, final, status, "spend:total")
    assert (event["hat"], event["level"]) == ("total", "overall")
    assert "reflect" not in sup.runner.calls


def test_a_spent_per_approach_cap_on_the_last_approach_refuses_the_reflection(
        tmp_path):
    sup, final, status = _run(
        tmp_path, {"review": [_verified(1.2)], "reflect": [_reflected()]},
        spend_caps=_caps(per_approach={"total": 1.0}), max_approaches=1)
    event = _held_exhausted(sup, final, status, "spend:total:approach")
    assert (event["hat"], event["level"]) == ("total", "per_approach")
    assert "reflect" not in sup.runner.calls


def test_a_spent_per_approach_cap_on_a_non_last_approach_archives_and_replans_instead_of_reflecting(
        tmp_path):
    sup, final, status = _run(
        tmp_path, {"review": [_verified(1.2)], "reflect": [_reflected()]},
        spend_caps=_caps(per_approach={"total": 1.0}), max_approaches=2)
    root = Path(sup.run.root)
    [event] = _events(sup, "spend.exhausted")
    assert (event["hat"], event["level"], event["action"],
            event["refusedHat"]) == ("total", "per_approach", "replan",
                                     "reflect")
    assert (root / "approaches" / "01").is_dir()       # archived
    assert sup.runner.calls["planning"] == 2           # replanned
    # approach 2 started from zero per-approach counters; overall carried on
    spend = status["spend"]
    assert spend["byApproach"]["1"]["total"] == 1.2
    assert spend["byApproach"]["2"].get("total", 0.0) == 0.0
    assert spend["overall"]["total"] == 1.2
    # the new approach finished and was reflected on within its own counters
    assert (final, status["state"], status["approach"]) == ("succeeded",
                                                           "succeeded", 2)
    assert sup.runner.calls["reflect"] == 1
    assert status["reflect"]["ok"] is True
    assert status.get("pendingTerminalState") is None
    assert len(status["previousEndings"]) == 1         # the withdrawn outcome


# -- live interruption: the interrupt applies the rules at the boundary -----------

def test_a_reflection_interrupted_by_its_overall_reflect_cap_holds_the_run_exhausted(
        tmp_path):
    sup, final, status = _run(
        tmp_path, {"reflect": [_spend_interrupted(1.2)]},
        spend_caps=_caps(overall={"reflect": 1.0}), max_approaches=3)
    event = _held_exhausted(sup, final, status, "spend:reflect")
    assert (event["hat"], event["level"], event["spend"]) == (
        "reflect", "overall", 1.2)
    assert sup.runner.calls["reflect"] == 1
    assert status["spend"]["overall"]["reflect"] == 1.2      # counted


def test_a_reflection_interrupted_by_the_total_cap_holds_the_run_exhausted(
        tmp_path):
    sup, final, status = _run(
        tmp_path, {"reflect": [_spend_interrupted(1.2)]},
        spend_caps=_caps(overall={"total": 1.0}), max_approaches=3)
    event = _held_exhausted(sup, final, status, "spend:total")
    assert (event["hat"], event["level"]) == ("total", "overall")
    assert sup.runner.calls["reflect"] == 1


def test_a_reflection_interrupted_by_its_per_approach_cap_on_the_last_approach_holds_exhausted(
        tmp_path):
    sup, final, status = _run(
        tmp_path, {"reflect": [_spend_interrupted(1.2)]},
        spend_caps=_caps(per_approach={"reflect": 1.0}), max_approaches=1)
    _held_exhausted(sup, final, status, "spend:reflect:approach")


def test_a_reflection_interrupted_by_its_per_approach_cap_on_a_non_last_approach_replans(
        tmp_path):
    def reflect(sup, prompt=""):
        if sup._approach == 1:
            return _spend_interrupted(1.2)(sup, prompt)
        return _reflected()(sup, prompt)

    sup, final, status = _run(
        tmp_path, {"reflect": [reflect]},
        spend_caps=_caps(per_approach={"reflect": 1.0}), max_approaches=2)
    [event] = _events(sup, "spend.exhausted")
    assert (event["hat"], event["level"], event["action"]) == (
        "reflect", "per_approach", "replan")
    assert (Path(sup.run.root) / "approaches" / "01").is_dir()
    assert sup.runner.calls["planning"] == 2
    assert sup.runner.calls["reflect"] == 2
    assert (final, status["state"]) == ("succeeded", "succeeded")
    assert status["spend"]["byApproach"]["2"].get("reflect", 0.0) == 0.0


# -- the other dispatches a reflection makes -------------------------------------

def test_a_paid_failed_reflection_attempt_is_not_retried_past_a_spent_cap(
        tmp_path):
    def outage(sup, prompt=""):
        return _result("", 1.2, exit_code=1, http_status=503,
                       error_type="server_error")

    sup, final, status = _run(
        tmp_path, {"reflect": [outage]},
        spend_caps=_caps(overall={"reflect": 1.0}), max_approaches=3)
    _held_exhausted(sup, final, status, "spend:reflect")
    assert sup.runner.calls["reflect"] == 1          # no internal retry


def test_a_contract_reask_of_the_reflection_is_not_dispatched_past_a_spent_cap(
        tmp_path):
    def malformed(sup, prompt=""):
        _report(sup)
        return _result("wrote the report, no payload", 1.2)

    sup, final, status = _run(
        tmp_path, {"reflect": [malformed]},
        spend_caps=_caps(overall={"reflect": 1.0}), max_approaches=3)
    _held_exhausted(sup, final, status, "spend:reflect")
    assert sup.runner.calls["reflect"] == 1


# -- what does not change ---------------------------------------------------------

def test_a_reflection_under_its_caps_leaves_the_settled_outcome_alone(tmp_path):
    sup, final, status = _run(
        tmp_path, {"reflect": [_reflected(0.4)]},
        spend_caps=_caps(overall={"reflect": 1.0, "total": 5.0},
                         per_approach={"reflect": 1.0}), max_approaches=3)
    assert (final, status["state"]) == ("succeeded", "succeeded")
    assert status["reflect"]["ok"] is True
    assert _events(sup, "spend.exhausted") == []


def test_an_operator_abort_keeps_its_veto_over_a_spent_reflect_cap(tmp_path):
    from ralphd.engine.loop import _SpendCapSpent

    sup = _supervisor(tmp_path / "run", {
        "planning": [_plan], "worker": [_complete], "review": [_verified()],
        "reflect": [_reflected()]},
        spend_caps=_caps(overall={"reflect": 1.0}), max_approaches=3)
    RunDir(root=tmp_path / "run").update_status(state="starting")
    sup.run.update_status(approach=1)
    sup.run.record_spend(1, "reflect", {"costUSD": 1.2})
    sup._abort_reason = "operator abort"
    sup._pending_terminal = ("aborted", None, {"reason": "operator abort"})
    try:
        sup._enforce_spend_caps("reflect")
    except _SpendCapSpent as spent:
        # the abort stays the ending: nothing is promoted to `exhausted`
        assert sup._apply_reflect_spend(spent) == "aborted"
    else:                                              # pragma: no cover
        raise AssertionError("the spent reflect cap was not refused")
    assert sup._pending_terminal is not None
    assert sup.run.read_status()["state"] != "exhausted"
