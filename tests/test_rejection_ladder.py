"""v0.36 requirement B: review and the peer verdict gate share ONE rejection
ladder (`LoopSupervisor._run_rejection_ladder`, deciding through
`ralphd.engine.ladder.decide`).

Every verdict either judge renders climbs the ladder, in this order, and the
first match wins:

1. the budget is spent -> `exhausted`, `verified` or `rejected` alike;
2. verified -> the peer gate if it is on, otherwise finish;
3. rejected with every blocking finding under a stored waiver -> verified;
4. park in `awaiting_input`: a blocking finding is `curable: false`, the
   reviewer set `raise_operator_gate`, or the approach's cure rounds are spent;
5. otherwise cure and count one round against the current approach.

Every test is parametrised over the judge. The 0.35 engine had a second
ladder for the gate (a rejection it could not cure ended the run `failed` on
the spot, `verdict_gate_rejected_on_merits`), a discretionary pre-archive gate,
a repeated-signature park and a one-re-park-per-ruling park -- these nodes fail
against it.
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import pytest

from ralphd.engine import findings, ladder
from ralphd.engine.loop import LoopSupervisor
from ralphd.engine.runner import IterationResult
from ralphd.engine.state import atomic_write_json, read_events
from tests.conftest import supervisor_factory

ROOT = Path(__file__).resolve().parents[1]
JUDGES = ["review", "verdict_gate"]

CURE_EDGE = {"review": "rejected_curable_pass_available",
             "verdict_gate": "verdict_gate_rejected_curable_pass_available"}
PARK_EDGE = {"review": "operator_input_needed",
             "verdict_gate": "verdict_gate_operator_input_needed"}
APPROACH_LEFT_EDGE = {"review": "rejected_approach_left",
                      "verdict_gate": "verdict_gate_rejected_approach_left"}

_supervisor = supervisor_factory(
    run_id="rejection-ladder", cfg_defaults=False,
    status={"state": "running"},
    prd="# rejection ladder fixture PRD\n\nDo the thing.\n", max_approaches=3,
    operator_input_needed_pause=True)


def _finding(curable: bool, clause: str, text: str = "still broken") -> dict:
    item = {"severity": "blocking", "curable": curable, "clause": clause,
            "evidence": text}
    if curable:
        item["cured_when"] = f"{clause}: {text} is fixed"
    return item


def _result(text: str) -> IterationResult:
    r = IterationResult(exit_code=0)
    r.final_text = text
    r.duration_s = 5.0
    r.usage = {"input": 10, "output": 5, "totalTokens": 15}
    return r


def _make(tmp_path, judge: str, **cfg) -> LoopSupervisor:
    cfg.setdefault("cure_pass_ceiling", 2)
    sup = _supervisor(tmp_path / "run", peer_verdict_gate=judge == "verdict_gate",
                      **cfg)
    sup._approach = 1
    atomic_write_json(sup.run.tasks_file, {"version": 1, "tasks": []})
    return sup


def _drive(sup: LoopSupervisor, judge: str, items: list[dict], *,
           escalate: bool = False, spend_budget: bool = False,
           verified: bool = False) -> tuple:
    """Run ONE real judge step whose hat answers `rejected` over `items`
    (or `verified`, with no findings, when `verified` is set). With
    `spend_budget` the deadline passes WHILE the judge's iteration runs,
    after the step's own dispatch-time budget check."""
    sidecar = (sup.run.findings_json_file if judge == "review"
               else sup.run.verdict_gate_findings_json_file)
    payload = ({"verdict": "verified"} if verified else
               {"verdict": "rejected", "reason": "the work is not there yet"})
    if escalate:
        payload["raise_operator_gate"] = True

    def _reply() -> IterationResult:
        if not verified:
            findings.write_findings_json(sidecar, items)
        if spend_budget:
            sup.deadline = time.monotonic() - 1
        return _result(json.dumps(payload))

    if judge == "review":
        async def review_run(prompt, transcript, **kw):
            return _reply()
        sup.runner.run = review_run  # type: ignore[method-assign]
        return asyncio.run(asyncio.wait_for(sup._step_review(), 30))

    async def gate_run(self, phase, extra="", *a, **kw):
        return _reply()
    original = LoopSupervisor.run_iteration
    LoopSupervisor.run_iteration = gate_run  # type: ignore[method-assign]
    try:
        return asyncio.run(asyncio.wait_for(sup._step_verdict_gate(), 30))
    finally:
        LoopSupervisor.run_iteration = original  # type: ignore[method-assign]


def _status(sup: LoopSupervisor) -> dict:
    return sup.run.read_status()


def _assert_parked(sup: LoopSupervisor, judge: str, outcome: tuple,
                   trigger: str) -> None:
    assert outcome == ("advance", "paused", PARK_EDGE[judge])
    status = _status(sup)
    assert status["state"] == "awaiting_input"
    question = status["inputQuestion"]
    assert question["judge"] == judge
    assert question["trigger"] == trigger
    assert not sup.run.composite_prd_file.exists(), "a park never archives"
    assert status.get("approach", 1) == 1


# -- step 1: the budget is spent --------------------------------------------

@pytest.mark.parametrize("judge", JUDGES)
def test_step_1_budget_spent_is_exhausted_not_a_park_or_cure(tmp_path, judge):
    sup = _make(tmp_path, judge)

    outcome = _drive(sup, judge, [_finding(True, "requirement A")],
                     spend_budget=True)

    assert outcome == ("tail",)
    assert sup._budget_exhausted_tail is True
    assert _status(sup).get("state") != "awaiting_input"
    assert not sup.run.composite_prd_file.exists()
    assert _status(sup).get("curePasses") in (None, {})
    assert _status(sup).get("gateCurePasses") in (None, {})


@pytest.mark.parametrize("judge", JUDGES)
def test_step_1_budget_spent_holds_a_verified_verdict_exhausted(tmp_path, judge):
    """Budget first, verdict second: a judge that passes the approach after
    the deadline went by while it ran still holds the run `exhausted` --
    never `succeeded`, never a dispatch of the peer gate."""
    sup = _make(tmp_path, judge)
    if judge == "review":
        sup.cfg.peer_verdict_gate = True

    outcome = _drive(sup, judge, [], verified=True, spend_budget=True)

    assert sup.budget_left() is False
    assert outcome == ("tail",)
    assert sup._budget_exhausted_tail is True
    status = _status(sup)
    assert status.get("state") not in ("succeeded", "awaiting_input")
    assert status.get("verdict") is None
    assert [e for e in read_events(sup.run.root, types=("signal",))
            if e.get("signal") == "VERIFIED"] == [], (
        "no VERIFIED signal: the verdict never reached its verified edge")


@pytest.mark.parametrize("judge", JUDGES)
def test_step_1_spent_budget_runs_on_to_the_exhausted_hold(tmp_path, judge):
    """The whole run, not just the step: a verified verdict after the
    deadline ends the episode in the `exhausted` (time) hold."""
    sup = _make(tmp_path, judge)
    sup._budget_exhausted_tail = False
    outcome = _drive(sup, judge, [], verified=True, spend_budget=True)
    assert outcome == ("tail",)
    final = sup._finish_tail_unverified()
    status = _status(sup)
    assert final == "exhausted" and status["state"] == "exhausted"
    assert status["exhaustedBudget"] == "time"
    assert status.get("verdict") is None
    assert judge.replace("_", " ") in status["reason"]


# -- step 2: verified -> the peer gate or finish -----------------------------

@pytest.mark.parametrize("judge", JUDGES)
def test_step_2_verified_finishes_when_no_peer_gate_follows(tmp_path, judge):
    sup = _make(tmp_path, judge)

    outcome = _drive(sup, judge, [], verified=True)

    assert outcome == ("done", "succeeded")
    assert _status(sup)["state"] == "succeeded"
    assert sup._budget_exhausted_tail is False


def test_step_2_verified_review_goes_to_the_peer_gate_when_it_is_on(tmp_path):
    sup = _make(tmp_path, "verdict_gate")  # peer gate on

    outcome = _drive(sup, "review", [], verified=True)

    assert outcome == ("advance", "verdict_gate",
                       "verified_peer_verdict_gate_enabled")
    assert _status(sup).get("state") != "succeeded"


# -- step 3: every blocking finding waived -> treated as verified -----------

@pytest.mark.parametrize("judge", JUDGES)
def test_step_3_every_blocking_finding_waived_is_treated_as_verified(
        tmp_path, judge):
    sup = _make(tmp_path, judge)
    sup.run.add_waiver(clause="requirement W (#1)", finding="a known gap",
                       judge=judge, note="accepted by the operator")

    outcome = _drive(sup, judge, [_finding(False, "requirement W (#1)")])

    assert outcome == ("done", "succeeded")
    assert _status(sup)["state"] != "awaiting_input"
    assert not sup.run.composite_prd_file.exists()


@pytest.mark.parametrize("judge", JUDGES)
def test_step_3_a_blocking_finding_outside_the_waived_clauses_is_not_waived(
        tmp_path, judge):
    sup = _make(tmp_path, judge)
    sup.run.add_waiver(clause="requirement W (#1)", finding="a known gap",
                       judge=judge)

    outcome = _drive(sup, judge, [_finding(False, "requirement W (#1)"),
                                  _finding(False, "requirement Z (#2)")])

    _assert_parked(sup, judge, outcome, "incurable")


# -- step 4a: a blocking finding is curable:false ---------------------------

@pytest.mark.parametrize("judge", JUDGES)
def test_step_4a_an_incurable_blocking_finding_parks(tmp_path, judge):
    sup = _make(tmp_path, judge)

    outcome = _drive(sup, judge, [_finding(False, "requirement B (#111)")])

    _assert_parked(sup, judge, outcome, "incurable")


@pytest.mark.parametrize("judge", JUDGES)
def test_step_4a_a_mixed_rejection_parks_instead_of_curing_the_curable_part(
        tmp_path, judge):
    sup = _make(tmp_path, judge)

    outcome = _drive(sup, judge, [_finding(True, "requirement A"),
                                  _finding(False, "requirement B")])

    _assert_parked(sup, judge, outcome, "incurable")
    assert _status(sup).get("curePasses") in (None, {})
    assert _status(sup).get("gateCurePasses") in (None, {})


# -- step 4b: the reviewer set raise_operator_gate (review only) ------------

def test_step_4b_the_reviewers_escalation_flag_parks_with_rounds_left(
        tmp_path):
    sup = _make(tmp_path, "review", cure_pass_ceiling=3)

    outcome = _drive(sup, "review", [_finding(True, "requirement A")],
                     escalate=True)

    _assert_parked(sup, "review", outcome, "reviewer_escalation")
    assert _status(sup)["inputQuestion"]["cureRoundsUsed"] == 0


# -- step 4c: the approach's cure rounds are spent --------------------------

@pytest.mark.parametrize("judge", JUDGES)
def test_step_4c_cure_rounds_spent_parks(tmp_path, judge):
    sup = _make(tmp_path, judge, cure_pass_ceiling=1)
    sup.run.update_status(curePasses={"1": 1})

    outcome = _drive(sup, judge, [_finding(True, "requirement A")])

    _assert_parked(sup, judge, outcome, "cure_rounds_spent")
    assert _status(sup)["inputQuestion"]["cureRoundsAllowed"] == 1


# -- step 5: otherwise cure, one round against the current approach ---------

@pytest.mark.parametrize("judge", JUDGES)
def test_step_5_a_curable_rejection_with_rounds_left_cures_one_round(
        tmp_path, judge):
    sup = _make(tmp_path, judge, cure_pass_ceiling=2)

    outcome = _drive(sup, judge, [_finding(True, "requirement A")])

    assert outcome == ("advance", "cure", CURE_EDGE[judge])
    assert sup._cure_rounds_used(1) == 1
    assert _status(sup).get("state") != "awaiting_input"
    assert not sup.run.composite_prd_file.exists()


# -- the gate: rejection with rounds left cures, spent parks, never failed --

def test_gate_rejection_with_rounds_left_cures_and_is_not_failed(tmp_path):
    sup = _make(tmp_path, "verdict_gate", cure_pass_ceiling=2)

    outcome = _drive(sup, "verdict_gate", [_finding(True, "requirement A")])

    assert outcome == ("advance", "cure", CURE_EDGE["verdict_gate"])
    assert _status(sup).get("gateCurePasses") == {"1": 1}
    assert _status(sup).get("state") != "failed"


def test_gate_rejection_with_rounds_spent_parks_in_awaiting_input(tmp_path):
    sup = _make(tmp_path, "verdict_gate", cure_pass_ceiling=1)
    sup.run.update_status(gateCurePasses={"1": 1})

    outcome = _drive(sup, "verdict_gate", [_finding(True, "requirement A")])

    _assert_parked(sup, "verdict_gate", outcome, "cure_rounds_spent")
    assert _status(sup).get("state") != "failed"


@pytest.mark.parametrize("approach_left", [True, False])
def test_gate_incurable_rejection_never_finishes_failed_on_the_spot(
        tmp_path, approach_left):
    """A gate rejection carrying an incurable finding used to end the run
    `failed` right there (`verdict_gate_rejected_on_merits`), approach left or
    not. It parks now."""
    sup = _make(tmp_path, "verdict_gate", max_approaches=3 if approach_left else 1)

    outcome = _drive(sup, "verdict_gate", [_finding(False, "requirement B")])

    _assert_parked(sup, "verdict_gate", outcome, "incurable")
    assert _status(sup).get("state") != "failed"


# -- the park is the ladder's, not a flag's: a fresh approach cures ---------

@pytest.mark.parametrize("judge", JUDGES)
def test_a_fresh_approach_cures_though_the_earlier_one_spent_its_rounds(
        tmp_path, judge):
    sup = _make(tmp_path, judge, cure_pass_ceiling=1)
    sup.run.update_status(curePasses={"1": 1})
    sup._approach = 2

    outcome = _drive(sup, judge, [_finding(True, "requirement A")])

    assert outcome == ("advance", "cure", CURE_EDGE[judge])


# -- one implementation ------------------------------------------------------

def test_both_judges_call_the_one_ladder_implementation():
    import inspect

    from ralphd.engine import loop

    review = inspect.getsource(LoopSupervisor._step_review)
    gate = inspect.getsource(LoopSupervisor._step_verdict_gate)
    assert "_run_rejection_ladder(" in review
    assert "_run_rejection_ladder(" in gate
    assert hasattr(ladder, "decide")
    source = inspect.getsource(loop)
    assert source.count("ladder.decide(") == 2, (
        "only the one ladder method decides, and it decides twice over the "
        "same facts")


def test_the_ladder_orders_budget_then_verified_then_waiver_then_park_then_cure():
    blocking = [_finding(False, "c")]
    waivers = [{"clause": "c"}]

    def decide(**kw):
        base = {"budget_left": True, "blocking": blocking, "waivers": [],
                "incurable": False, "reviewer_escalation": False,
                "rounds_spent": False}
        base.update(kw)
        return ladder.decide(**base)

    assert decide(budget_left=False, waivers=waivers).step == "exhausted"
    assert decide(budget_left=False, verified=True).step == "exhausted"
    assert decide(verified=True, incurable=True,
                  rounds_spent=True).step == "verified"
    assert decide(waivers=waivers, incurable=True,
                  reviewer_escalation=True, rounds_spent=True).step == "waived"
    assert decide(incurable=True).step == "park"
    assert decide(reviewer_escalation=True).step == "park"
    assert decide(rounds_spent=True).step == "park"
    assert decide().step == "cure"
    # a rejection with no blocking finding is never "every finding waived"
    assert decide(blocking=[], waivers=waivers).step == "cure"


# -- the retired names and the spec ------------------------------------------

def test_the_discretionary_repeated_signature_and_on_merits_paths_are_gone():
    import subprocess
    out = subprocess.run(
        ["grep", "-rnE", "--include=*.py", "--include=*.js",
         ("discretionaryGateSpent|rejected_repeated_signature|"
          "verdict_gate_rejected_on_merits"), str(ROOT / "src" / "ralphd")],
        capture_output=True, text=True)
    assert out.stdout == ""
    loop_src = (ROOT / "src" / "ralphd" / "engine" / "loop.py").read_text()
    assert "_record_ruling_reparked" not in loop_src
    assert "_ruling_already_reparked" not in loop_src


def test_spec_4_2_describes_the_ladder_in_the_present_tense():
    spec = (ROOT / "SPEC.md").read_text()
    start = spec.index("### 4.2")
    end = spec.index("\n### ", start + 5)
    section = spec[start:end]
    assert "rejection ladder" in section
    for step in ("exhausted", "waiver", "awaiting_input", "cure round"):
        assert step in section
    assert "raise_operator_gate" in section
