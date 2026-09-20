"""Task 016 (v0.23 PRD requirements A.5/A.6, #224): two things on top of
tasks 013-015's ruling channel/pause/re-ask machinery.

A.5 -- a re-run review that RE-REJECTS on a finding signature a recorded
operator ruling already addressed does not spend the approach: the engine
refuses that verdict and re-parks through the SAME `operator_input_needed`
pause seam, exactly once per ruling (`_ruling_honoured_for_approach`, task
015's own reader, plus a new `rulingReparked` disk-backed counter keyed on
the ruling's own `seq`). A SECOND such rejection on the SAME ruling
proceeds to the ordinary archive/replan tail instead of re-parking again.

Cure task cure-01-02 (F02, #224): the mechanical re-park decision above
must pre-empt EVERY other branch a matching rejection could otherwise take,
not only the plain archive/replan tail the pre-cure shape checked it
against. Two extra episodes below drive that through the supervisor: a
ruled signature that would otherwise take task 009/017's CURE pass
(`_review_cure_pass_available`, trigger condition 2) still re-parks
instead, with `curePasses` left unspent; and a ruled signature that ALSO
repeats the previous approach's own recorded one (task 016/017's own
repeated-signature trigger condition 3) still re-parks instead of falling
through to `_archive_approach`, because the ordinary tail's own combined
check ANDs `not repeated_signature` and would otherwise never reach the
ruling at all.

A.6 -- the reviewer's own OPTIONAL `raise_operator_gate` field
(`transitions.HAT_SCHEMAS["review"]`) raises that same gate on a case the
mechanical `operator_input_needed_pause` flag would not otherwise catch;
its absence is the identical, backward-compatible default every other
optional field on this contract already has.

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
# A.5 -- one re-park per ruling.
# ===========================================================================


def test_a_re_rejection_on_a_ruled_signature_re_parks_once_and_does_not_spend(
        tmp_path):
    root = tmp_path / "run"
    sup0 = _first_pause_no_ruling_yet(root)
    signature = (sup0.run.read_status().get("rejectionSignatures") or {}).get("1")
    assert signature is not None
    sup0.run.add_ruling("Waived: ship it, these are residuals.")
    sup0.resume()

    # Episode 2: fresh supervisor, review re-asked (task 015/A.4), rejects
    # AGAIN with the identical blocking-clause set -- same signature.
    sup1 = _supervisor(root, max_approaches=2, operator_input_needed_pause=True)
    sup1._approach = 1
    findings.write_findings_json(sup1.run.findings_json_file, _MIXED_FINDINGS)
    sup1.runner.run = _rejecting_runner("still mixed")  # type: ignore[method-assign]

    outcome = _bounded(sup1._step_review())

    # A.5's own cure: the verdict is refused and the run re-parks -- never
    # archived, never a fresh approach directory, never a composite PRD.
    assert outcome == ("advance", "paused", "operator_input_needed")
    status = sup1.run.read_status()
    assert status["state"] == "paused"
    assert status["pauseReason"] == "operator_input_needed"
    assert status["approach"] == 1
    assert not (root / "approaches" / "01").exists()
    assert not sup1.run.composite_prd_file.exists()
    # the ruling that was matched has now spent its one re-park.
    assert status.get("rulingReparked") == [1]

    # Cure task cure-01-02 (F02, #224), regression 1: the identical ruled
    # signature, but this time the rejection's OWN blocking findings are
    # all curable and would reopen a `validated` task -- trigger condition
    # 2's own cure branch (`_review_cure_pass_available`,
    # `state.cure_pass_preview`). Before the cure, this fell straight into
    # `_take_cure_pass`/`("advance", "cure", ...)`, spending the approach's
    # cure budget on findings a ruling already addressed and never
    # re-parking at all -- the mechanical decision was computed only at
    # the bottom archive/replan tail, after this branch had already
    # returned.
    root_cure = tmp_path / "run-cure"
    tasks = _lane_fixture_tasks()
    sup_cure0 = _supervisor(root_cure, max_approaches=2,
                            operator_input_needed_pause=True, tasks=tasks)
    sup_cure0._approach = 1
    findings.write_findings_json(sup_cure0.run.findings_json_file,
                                 _CURE_REOPEN_FINDING)
    sup_cure0.runner.run = _rejecting_runner("lanes moved")  # type: ignore[method-assign]
    outcome_cure0 = _bounded(sup_cure0._step_review())
    assert outcome_cure0 == ("advance", "paused", "operator_input_needed")
    sup_cure0.run.add_ruling("Those lanes are residual. Ship it.")
    sup_cure0.resume()

    sup_cure1 = _supervisor(root_cure, max_approaches=2,
                            operator_input_needed_pause=True, tasks=tasks)
    sup_cure1._approach = 1
    findings.write_findings_json(sup_cure1.run.findings_json_file,
                                 _CURE_REOPEN_FINDING)
    sup_cure1.runner.run = _rejecting_runner("lanes moved again")  # type: ignore[method-assign]
    outcome_cure1 = _bounded(sup_cure1._step_review())
    assert outcome_cure1 == ("advance", "paused", "operator_input_needed")
    cure_status = sup_cure1.run.read_status()
    # the cure branch never fired -- no cure pass was ever spent.
    assert not cure_status.get("curePasses")
    assert not (root_cure / "approaches" / "01").exists()
    assert cure_status.get("rulingReparked") == [1]

    # Cure task cure-01-02 (F02, #224), regression 2: the identical ruled
    # signature, but this time it ALSO repeats the previous approach's own
    # recorded rejection signature -- trigger condition 3's own repeated-
    # signature branch. Before the cure, the ordinary archive/replan
    # tail's combined pause check ANDs `not repeated_signature`, so once
    # condition 3's own shared discretionary gate was unavailable this
    # fell through to `_archive_approach`/`("advance", "replan", ...)`
    # instead of re-parking for the ruling at all.
    root_repeat = tmp_path / "run-repeat"
    sup_rep0 = _supervisor(root_repeat, max_approaches=2,
                          operator_input_needed_pause=True)
    sup_rep0._approach = 1
    sig = sup_rep0._review_rejection_signature(_MIXED_FINDINGS, None)
    sup_rep0.run.update_status(rejectionSignatures={"0": sig})
    findings.write_findings_json(sup_rep0.run.findings_json_file, _MIXED_FINDINGS)
    sup_rep0.runner.run = _rejecting_runner("mixed, repeats approach 0")  # type: ignore[method-assign]
    outcome_rep0 = _bounded(sup_rep0._step_review())
    assert outcome_rep0[1] == "paused"  # condition 3's own repeated-signature pause
    sup_rep0.run.add_ruling("Those clauses are residual. Ship it.")
    sup_rep0.resume()

    sup_rep1 = _supervisor(root_repeat, max_approaches=2,
                          operator_input_needed_pause=True)
    sup_rep1._approach = 1
    findings.write_findings_json(sup_rep1.run.findings_json_file, _MIXED_FINDINGS)
    sup_rep1.runner.run = _rejecting_runner("mixed, still repeats")  # type: ignore[method-assign]
    outcome_rep1 = _bounded(sup_rep1._step_review())
    assert outcome_rep1 == ("advance", "paused", "operator_input_needed")
    rep_status = sup_rep1.run.read_status()
    assert not (root_repeat / "approaches" / "01").exists()
    assert not sup_rep1.run.composite_prd_file.exists()
    assert rep_status.get("rulingReparked") == [1]


def test_a_second_rejection_on_the_same_ruling_proceeds_instead_of_re_parking_again(
        tmp_path):
    root = tmp_path / "run"
    sup0 = _first_pause_no_ruling_yet(root)
    sup0.run.add_ruling("Waived: ship it, these are residuals.")
    sup0.resume()

    sup1 = _supervisor(root, max_approaches=2, operator_input_needed_pause=True)
    sup1._approach = 1
    findings.write_findings_json(sup1.run.findings_json_file, _MIXED_FINDINGS)
    sup1.runner.run = _rejecting_runner("still mixed")  # type: ignore[method-assign]
    outcome1 = _bounded(sup1._step_review())
    assert outcome1 == ("advance", "paused", "operator_input_needed")  # re-park #1
    sup1.resume()

    # Episode 3: SAME ruling (seq 1, same signature) rejects a THIRD time.
    # The bound is spent -- this proceeds to the ordinary archive/replan
    # tail instead of re-parking a second time.
    sup2 = _supervisor(root, max_approaches=2, operator_input_needed_pause=True)
    sup2._approach = 1
    findings.write_findings_json(sup2.run.findings_json_file, _MIXED_FINDINGS)
    sup2.runner.run = _rejecting_runner("still mixed, again")  # type: ignore[method-assign]

    outcome2 = _bounded(sup2._step_review())

    assert outcome2 == ("advance", "replan", "rejected_approach_left")
    status = sup2.run.read_status()
    assert status["state"] != "paused"
    assert (root / "approaches" / "01").exists()
    assert sup2.run.composite_prd_file.exists()
    assert status.get("approach") == 1  # _step_review never bumps it -- _step_replan would
    # the re-park bound counter is unchanged -- still exactly one entry.

    assert status.get("rulingReparked") == [1]


def test_a_rejection_on_a_different_signature_is_not_bounded_by_an_unrelated_ruling(
        tmp_path):
    """A ruling addressing signature S1 must not silently cover a LATER
    rejection whose own blocking-clause set (and therefore signature) has
    changed -- the bound is per SIGNATURE MATCH, never "any ruling exists
    for this approach"."""
    root = tmp_path / "run"
    sup0 = _first_pause_no_ruling_yet(root)
    sup0.run.add_ruling("Waived: ship it.")
    sup0.resume()

    other_findings = [
        _finding("blocking", False, "a completely different wall", "evidence"),
        _finding("blocking", True, "another curable one", "evidence",
                  cured_when="fixed"),
    ]
    sup1 = _supervisor(root, max_approaches=2, operator_input_needed_pause=True)
    sup1._approach = 1
    findings.write_findings_json(sup1.run.findings_json_file, other_findings)
    sup1.runner.run = _rejecting_runner("a new wall entirely")  # type: ignore[method-assign]

    outcome = _bounded(sup1._step_review())

    # unbounded, ordinary pause -- the ruling on file never matched this
    # rejection's own (different) signature.
    assert outcome == ("advance", "paused", "operator_input_needed")
    assert sup1.run.read_status().get("rulingReparked") in (None, [])


# ===========================================================================
# A.6 -- the reviewer's own optional `raise_operator_gate` field.
# ===========================================================================


def test_a_reviewer_raised_gate_pauses_even_with_the_mechanical_flag_disabled(
        tmp_path):
    root = tmp_path / "run"
    sup = _supervisor(root, max_approaches=2, operator_input_needed_pause=False)
    sup._approach = 1
    findings.write_findings_json(sup.run.findings_json_file, _MIXED_FINDINGS)

    async def _review_raises_the_gate(prompt, transcript, **kw):
        return _result(json.dumps({
            "verdict": "rejected", "reason": "a case the predicate misses",
            "raise_operator_gate": True,
        }))
    sup.runner.run = _review_raises_the_gate  # type: ignore[method-assign]

    outcome = _bounded(sup._step_review())

    assert outcome == ("advance", "paused", "operator_input_needed")
    # the probe that reads the recorded pause reason.
    status = sup.run.read_status()
    assert status["state"] == "paused"
    assert status["pauseReason"] == "operator_input_needed"
    assert not (root / "approaches" / "01").exists()


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
