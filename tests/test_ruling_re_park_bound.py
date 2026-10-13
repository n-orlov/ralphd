"""Rulings and the reviewer's escalation flag over the one rejection ladder.

Task 016 (v0.23 PRD requirements A.5/A.6, #224) once bounded a ruling to ONE
re-park (`rulingReparked`) and let the reviewer's optional
`raise_operator_gate` field raise the gate. v0.36 requirement B removed the
one-re-park-per-ruling park together with the discretionary gate: a recorded
ruling is context for the judges, never a park trigger and never a bound on
one. Section A.5 below pins that -- a ruled signature is an ordinary
rejection that the ladder cures while the approach has rounds left and parks
when it has none -- and A.6 keeps pinning the reviewer's escalation field.

Modelled on `tests/test_operator_ruling_reask.py`'s own multi-episode
shape: a FRESH `LoopSupervisor` over the same on-disk run dir per episode,
never shared Python state, so "one process died and a new one resumed the
run" is exactly what each episode boundary represents.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from ralphd.engine import contracts, findings, transitions
from ralphd.engine.runner import IterationResult
from tests.conftest import supervisor_factory
from tests.test_discretionary_gate import _CURE_REOPEN_FINDING, _lane_fixture_tasks


def _result(text: str) -> IterationResult:
    r = IterationResult(exit_code=0)
    r.final_text = text
    r.duration_s = 5.0
    r.usage = {"input": 10, "output": 5, "totalTokens": 15}
    return r


def _finding(severity: str, curable: bool, clause: str, evidence: str,
             cured_when: str | None = None) -> dict:
    payload = {"severity": severity, "curable": curable, "clause": clause,
               "evidence": evidence}
    if cured_when is None and severity == "blocking" and curable is True:
        cured_when = f"{clause} is discharged"
    if cured_when is not None:
        payload["cured_when"] = cured_when
    return payload


# Neither all-curable nor all-incurable -- the same fixture
# `tests/test_operator_input_needed_pause.py`/`test_operator_ruling_reask.py`
# use, so this rejection lands on the ordinary archive/replan tail these
# tasks all intercept, never on `rejected_incurable`/`rejected_curable_
# pass_available`/`rejected_last_approach`.
_MIXED_FINDINGS = [
    _finding("blocking", False, "requirement B (#111)", "incurable one"),
    _finding("blocking", True, "requirement A.1 (#224)", "curable one"),
]


def _bounded(coro, timeout: float = 30.0):
    return asyncio.run(asyncio.wait_for(coro, timeout))


def _events(root: Path, type_: str) -> list[dict]:
    log = root / "events.jsonl"
    lines = log.read_text().splitlines() if log.exists() else []
    return [ev for ev in (json.loads(x) for x in lines) if ev.get("type") == type_]


_supervisor = supervisor_factory(
    run_id="task016-ruling-re-park", cfg_defaults=False,
    prd="# task 016 fixture PRD\n\nDo the thing.\n",
    status={"state": "running", "phase": "review", "approach": 1})


def _rejecting_runner(reason: str):
    async def _run(prompt, transcript, **kw):
        return _result(json.dumps({"verdict": "rejected", "reason": reason}))
    return _run


def _first_pause_no_ruling_yet(root: Path):
    """Episode 0: an ordinary rejection with NO ruling filed yet -- the
    pre-existing, unbounded `operator_input_needed` pause (task 014) that
    exists purely to produce a recorded rejection signature an operator can
    then rule against."""
    sup = _supervisor(root, max_approaches=2, operator_input_needed_pause=True)
    sup._approach = 1
    findings.write_findings_json(sup.run.findings_json_file, _MIXED_FINDINGS)
    sup.runner.run = _rejecting_runner("mixed curability")  # type: ignore[method-assign]

    outcome = _bounded(sup._step_review())
    assert outcome == ("advance", "paused", "operator_input_needed")
    return sup


# ===========================================================================
# A.5 -- a ruling is not a re-park trigger and not a bound on the ladder.
# ===========================================================================


def test_a_ruled_signature_is_an_ordinary_rejection_for_the_ladder(tmp_path):
    root = tmp_path / "run"
    sup0 = _first_pause_no_ruling_yet(root)
    signature = (sup0.run.read_status().get("rejectionSignatures") or {}).get("1")
    assert signature is not None
    sup0.run.add_ruling("Waived: ship it, these are residuals.")
    sup0.resume()

    # An incurable re-rejection on the ruled signature parks again -- every
    # time. There is no "one re-park per ruling" and no proceed it buys.
    for attempt in range(2):
        sup = _supervisor(root, max_approaches=2, operator_input_needed_pause=True)
        sup._approach = 1
        findings.write_findings_json(sup.run.findings_json_file, _MIXED_FINDINGS)
        sup.runner.run = _rejecting_runner(f"still mixed {attempt}")  # type: ignore[method-assign]
        assert _bounded(sup._step_review()) == (
            "advance", "paused", "operator_input_needed")
        status = sup.run.read_status()
        assert status["state"] == "awaiting_input"
        assert status["inputQuestion"]["trigger"] == "incurable"
        assert "rulingReparked" not in status
        assert not (root / "approaches" / "01").exists()
        assert not sup.run.composite_prd_file.exists()
        sup.resume()

    # A ruled signature whose findings are curable (and would reopen a
    # validated task) is cured while the approach has rounds left, with no
    # park and no gate spent.
    root_cure = tmp_path / "run-cure"
    tasks = _lane_fixture_tasks()
    sup_cure = _supervisor(root_cure, max_approaches=2,
                           operator_input_needed_pause=True, tasks=tasks)
    sup_cure._approach = 1
    cure_signature = sup_cure._review_rejection_signature(_CURE_REOPEN_FINDING, None)
    sup_cure.run.update_status(rejectionSignatures={"1": cure_signature})
    sup_cure.run.add_ruling("Those lanes are residual. Ship it.")
    findings.write_findings_json(sup_cure.run.findings_json_file,
                                 _CURE_REOPEN_FINDING)
    sup_cure.runner.run = _rejecting_runner("lanes moved")  # type: ignore[method-assign]
    assert _bounded(sup_cure._step_review()) == (
        "advance", "cure", "rejected_curable_pass_available")
    cure_status = sup_cure.run.read_status()
    assert cure_status.get("curePasses") == {"1": 1}
    assert "rulingReparked" not in cure_status
    assert "discretionaryGateSpent" not in cure_status
    assert cure_status.get("state") != "awaiting_input"


def test_a_ruled_rejection_in_a_fresh_approach_cures_like_any_other(tmp_path):
    """The ladder's cure rounds are per approach and a ruling bounds nothing:
    the same wall recurring in approach 2 (after an actual replan) is cured
    from approach 2's own fresh allowance."""
    root = tmp_path / "run"
    sup0 = _first_pause_no_ruling_yet(root)
    sup0.run.add_ruling("Waived: ship it, these are residuals.")
    sup0.resume()
    sup0._archive_approach(1)
    sup0._write_composite_prd(1)
    assert sup0._step_replan()[0] == "advance"
    assert sup0._approach == 2

    curable_wall = [dict(f, curable=True, cured_when="Fix " + f["clause"])
                    for f in _MIXED_FINDINGS]
    sup1 = _supervisor(root, max_approaches=3, operator_input_needed_pause=True,
                       cure_pass_ceiling=1)
    sup1.run.update_status(curePasses={"1": 1})
    sup1._approach = 2
    findings.write_findings_json(sup1.run.findings_json_file, curable_wall)
    sup1.runner.run = _rejecting_runner("same wall recurs in approach 2")  # type: ignore[method-assign]

    assert _bounded(sup1._step_review()) == (
        "advance", "cure", "rejected_curable_pass_available")
    status = sup1.run.read_status()
    assert status.get("curePasses") == {"1": 1, "2": 1}
    assert "rulingReparked" not in status


# ===========================================================================
# A.6 -- the reviewer's own optional `raise_operator_gate` field.
# ===========================================================================


def test_a_reviewer_raised_gate_with_the_park_disabled_resolves_at_once(
        tmp_path):
    """`operator_input_needed_pause: false` means the input timeout fires at
    the moment of the park (v0.36 requirement D.3): the run never waits on
    findings, but the park's outcome is still taken -- a reviewer escalation
    over curable findings with rounds left cures now, and an incurable
    rejection archives and replans while an approach is left."""
    async def _review_raises_the_gate(prompt, transcript, **kw):
        return _result(json.dumps({
            "verdict": "rejected", "reason": "a case the predicate misses",
            "raise_operator_gate": True,
        }))

    root = tmp_path / "run"
    (root).mkdir(parents=True, exist_ok=True)
    (root / "prd.md").write_text("# test PRD\n\nDo the thing.\n")
    sup = _supervisor(root, max_approaches=2, operator_input_needed_pause=False)
    sup._approach = 1
    findings.write_findings_json(sup.run.findings_json_file, _MIXED_FINDINGS)
    sup.runner.run = _review_raises_the_gate  # type: ignore[method-assign]
    assert _bounded(sup._step_review()) == (
        "advance", "replan", "rejected_approach_left")
    assert sup.run.read_status().get("state") != "awaiting_input"
    assert (root / "approaches" / "01").exists()

    root2 = tmp_path / "run2"
    sup2 = _supervisor(root2, max_approaches=2, operator_input_needed_pause=False)
    sup2._approach = 1
    findings.write_findings_json(
        sup2.run.findings_json_file,
        [_finding("blocking", True, "requirement A.1 (#224)", "curable one")])
    sup2.runner.run = _review_raises_the_gate  # type: ignore[method-assign]
    assert _bounded(sup2._step_review()) == (
        "advance", "cure", "rejected_curable_pass_available")
    assert sup2.run.read_status().get("state") != "awaiting_input"
    assert not (root2 / "approaches" / "01").exists()


def test_a_reply_omitting_the_field_is_accepted_with_no_contract_error_or_warning(
        tmp_path):
    root = tmp_path / "run"
    sup = _supervisor(root, max_approaches=2, operator_input_needed_pause=False)
    sup._approach = 1
    findings.write_findings_json(sup.run.findings_json_file, _MIXED_FINDINGS)
    sup.runner.run = _rejecting_runner("no field at all")  # type: ignore[method-assign]

    outcome = _bounded(sup._step_review())

    # the pre-016 behaviour, byte for byte: the flag is off, no reviewer
    # field was supplied, so the ordinary archive/replan tail runs.
    assert outcome == ("advance", "replan", "rejected_approach_left")
    assert sup.run.read_status().get("pauseReason") is None
    assert _events(root, "contract.agent_fault") == []
    assert not any(ev.get("level") == "warning"
                  for ev in _events(root, "log"))

    # the schema-level half of the same claim: a payload with no
    # `raise_operator_gate` key at all validates with zero errors.
    errors = contracts.validate_payload(
        "review", {"verdict": "rejected", "reason": "no field at all"})
    assert errors == []


def test_the_field_is_optional_additive_never_required(tmp_path):
    # `required` is untouched by this task -- still exactly `["verdict"]`.
    assert transitions.HAT_SCHEMAS["review"]["required"] == ["verdict"]
    assert transitions.HAT_SCHEMAS["review"]["properties"]["raise_operator_gate"] == {
        "type": "boolean"}
    # a payload carrying the field, `True` or `False`, validates cleanly.
    assert contracts.validate_payload(
        "review", {"verdict": "rejected", "reason": "x",
                  "raise_operator_gate": True}) == []
    assert contracts.validate_payload(
        "review", {"verdict": "rejected", "reason": "x",
                  "raise_operator_gate": False}) == []


def test_the_reader_names_absence_as_the_backward_compatible_default_in_its_own_docstring(
        tmp_path):
    from ralphd.engine.loop import LoopSupervisor
    doc = LoopSupervisor._review_reviewer_requested_operator_gate.__doc__ or ""
    assert "backward-compatible default" in doc

    root = tmp_path / "run"
    sup = _supervisor(root, max_approaches=2, operator_input_needed_pause=False)
    assert sup._review_reviewer_requested_operator_gate(None) is False
    assert sup._review_reviewer_requested_operator_gate({}) is False
    assert sup._review_reviewer_requested_operator_gate(
        {"verdict": "rejected"}) is False
    assert sup._review_reviewer_requested_operator_gate(
        {"verdict": "rejected", "raise_operator_gate": False}) is False
    assert sup._review_reviewer_requested_operator_gate(
        {"verdict": "rejected", "raise_operator_gate": True}) is True


# ===========================================================================
# The re-park reuses the existing pause seam, never a new state.
# ===========================================================================


def test_the_re_park_adds_no_new_terminal_or_nonterminal_state():
    from ralphd.engine import state
    assert "operator_input_needed" not in state.NONTERMINAL_STATES
    assert "operator_input_needed" not in state.TERMINAL_STATES
    assert state.PAUSED_STATE in state.NONTERMINAL_STATES
