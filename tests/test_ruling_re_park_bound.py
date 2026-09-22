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
    """Cure task cure-01-02-3 (PRD requirement A.5, #224): both rejections
    below carry `raise_operator_gate: true` on their own reply -- the bound
    is mechanical, not advisory, so a reviewer that keeps setting the field
    on the SAME ruled signature still only buys ONE re-park, never a
    second. Before this cure, the archive/replan tail's own combined check
    ORed in the reviewer's field unconditionally alongside the mechanical
    flag, so episode 3 below re-parked a second time on the identical
    ruling with `rulingReparked` never gaining a fresh entry (the record
    guard only fires when the ruling is NOT yet exhausted) while the
    outcome stayed `paused` regardless
    (`tests/_review_edges.py::test_A5_second_ruled_rejection_proceeds_after_reviewer_request`).
    """
    def _rejecting_with_gate(reason: str):
        async def _run(prompt, transcript, **kw):
            return _result(json.dumps({
                "verdict": "rejected", "reason": reason,
                "raise_operator_gate": True,
            }))
        return _run

    root = tmp_path / "run"
    sup0 = _first_pause_no_ruling_yet(root)
    sup0.run.add_ruling("Waived: ship it, these are residuals.")
    sup0.resume()

    sup1 = _supervisor(root, max_approaches=2, operator_input_needed_pause=True)
    sup1._approach = 1
    findings.write_findings_json(sup1.run.findings_json_file, _MIXED_FINDINGS)
    sup1.runner.run = _rejecting_with_gate("still mixed")  # type: ignore[method-assign]
    outcome1 = _bounded(sup1._step_review())
    assert outcome1 == ("advance", "paused", "operator_input_needed")  # re-park #1
    assert sup1.run.read_status().get("rulingReparked") == [1]
    sup1.resume()

    # Episode 3: SAME ruling (seq 1, same signature) rejects a THIRD time,
    # its own reply repeating `raise_operator_gate: true` again. The
    # bound is spent -- this proceeds to the ordinary archive/replan tail
    # instead of re-parking a second time, exactly as it would with the
    # field absent. A fresh `LoopSupervisor` over the SAME persisted run
    # dir, per this file's own multi-episode convention.
    sup2 = _supervisor(root, max_approaches=2, operator_input_needed_pause=True)
    sup2._approach = 1
    findings.write_findings_json(sup2.run.findings_json_file, _MIXED_FINDINGS)
    sup2.runner.run = _rejecting_with_gate("still mixed, again")  # type: ignore[method-assign]

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
    for this approach".

    Task 002 (PRD finding F02), extended in place: the SAME episode then
    goes back to the ORIGINAL signature S -- the mechanical re-park lookup
    must be keyed to the CURRENT rejection's own signature, never to
    `status.json`'s `rejectionSignatures[str(approach)]`, which only ever
    holds the approach's LAST recorded rejection signature and is
    overwritten by every intervening rejection on a different one
    (`_record_rejection_signature`) -- here, by the different-signature
    rejection just above. The ruling filed against S must still spend its
    one re-park once S recurs, whatever the intervening pause did to that
    per-approach map; a further rejection on S then proceeds to the
    ordinary archive/replan tail instead of re-parking a second time.
    Every episode is a FRESH `LoopSupervisor` over the SAME on-disk run
    dir, per this file's own multi-episode convention -- the bound is read
    back from PERSISTED state, never from Python state a single process
    happened to keep around.
    """
    root = tmp_path / "run"
    sup0 = _first_pause_no_ruling_yet(root)
    signature_s = (sup0.run.read_status().get("rejectionSignatures") or {}).get("1")
    assert signature_s is not None
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

    # Cure task cure-01-02-3 (PRD requirement A.5, #224): the reply on
    # this genuinely different signature carries `raise_operator_gate:
    # true` too -- the bound above is per-SIGNATURE-MATCH, never "a ruling
    # exists somewhere on this approach", so the field still requests its
    # own fresh, unbounded initial gate here exactly as if no ruling had
    # ever been filed.
    async def _new_wall_with_gate(prompt, transcript, **kw):
        return _result(json.dumps({
            "verdict": "rejected", "reason": "a new wall entirely",
            "raise_operator_gate": True,
        }))
    sup1.runner.run = _new_wall_with_gate  # type: ignore[method-assign]

    outcome = _bounded(sup1._step_review())

    # unbounded, ordinary pause -- the ruling on file never matched this
    # rejection's own (different) signature.
    assert outcome == ("advance", "paused", "operator_input_needed")
    assert sup1.run.read_status().get("rulingReparked") in (None, [])
    signature_t = sup1.run.read_status().get("rejectionSignatures", {}).get("1")
    assert signature_t is not None and signature_t != signature_s
    sup1.resume()

    # Episode 3 (task 002, F02): back to signature S -- the ruling filed
    # against it in episode 1 has never been spent, and the intervening T
    # pause (episode 2, which overwrote `rejectionSignatures["1"]` from S
    # to T) must not have cost it its one re-park.
    sup2 = _supervisor(root, max_approaches=2, operator_input_needed_pause=True)
    sup2._approach = 1
    findings.write_findings_json(sup2.run.findings_json_file, _MIXED_FINDINGS)
    sup2.runner.run = _rejecting_runner("back to the original wall")  # type: ignore[method-assign]
    outcome_s1 = _bounded(sup2._step_review())
    assert outcome_s1 == ("advance", "paused", "operator_input_needed")
    status_s1 = sup2.run.read_status()
    assert status_s1["pauseReason"] == "operator_input_needed"
    assert status_s1["approach"] == 1
    assert not (root / "approaches" / "01").exists()
    assert not sup2.run.composite_prd_file.exists()
    assert status_s1.get("rulingReparked") == [1]
    sup2.resume()

    # Episode 4: a SECOND rejection on S proceeds to the ordinary
    # archive/replan tail instead of re-parking again -- the same ruling's
    # one re-park is now spent.
    sup3 = _supervisor(root, max_approaches=2, operator_input_needed_pause=True)
    sup3._approach = 1
    findings.write_findings_json(sup3.run.findings_json_file, _MIXED_FINDINGS)
    sup3.runner.run = _rejecting_runner("the original wall, again")  # type: ignore[method-assign]
    outcome_s2 = _bounded(sup3._step_review())
    assert outcome_s2 == ("advance", "replan", "rejected_approach_left")
    status_s2 = sup3.run.read_status()
    assert status_s2["state"] != "paused"
    assert (root / "approaches" / "01").exists()
    assert sup3.run.composite_prd_file.exists()
    assert status_s2.get("rulingReparked") == [1]


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


# ===========================================================================
# Task 004 (J.1, PRD requirement A.5, #224): the two A.5 bound questions the
# v0.23 review's own probes (`_review_new_edges.py`/`_review_positive.py`,
# not present on this host -- reconstructed here from the v0.24 PRD's own
# descriptions of them) raised against this file's shipped code. Both
# probes judged RIGHT against A.5's own text ("a bounded re-ask and a
# one-re-park-per-ruling refusal on a re-rejection the ruling already
# addressed", `docs/roadmap.md`'s v0.23 section) -- each test below pins
# the probe's own expectation directly against the fixed product code,
# never a rewritten claim.
# ===========================================================================


def test_A5_second_ruled_rejection_cannot_use_unspent_reopen_gate(tmp_path):
    """Probe 1 (reconstructed): "A.5 requires the second rejection on the
    same ruling to proceed." Judged RIGHT against `loop.py`'s own
    `mechanical_ruled_park_due` computation (:10836) and its docstring's
    claim that the ruling decision must "pre-empt... EVERY branch below it"
    (:10796) -- but the deciding line for THIS test's own scenario is
    `mechanical_reopen_trigger`'s coupling to `_spend_discretionary_gate`
    (:10989-10999): the shared per-approach discretionary gate is spent the
    FIRST time a cure-reopen-eligible rejection fires, whether or not any
    ruling exists yet -- so by construction, once a ruling's OWN one-time
    re-park is later spent on that identical signature, the gate this
    second rejection could otherwise have used is already gone. The second
    ruled rejection genuinely "cannot use" it, and falls through to the
    ordinary, ungated cure pass instead of a second pause -- never a
    second `operator_input_needed`, and never a fresh `discretionaryGateSpent`
    write on this episode.

    Three episodes, one on-disk run dir, a fresh `LoopSupervisor` each time
    (this file's own multi-episode convention):

    1. no ruling yet -- the cure-reopen rejection pauses via the ordinary
       mechanical trigger (condition 2), spending the approach's one
       discretionary gate right there.
    2. a ruling is filed against that signature and the SAME rejection
       recurs -- `mechanical_ruled_park_due` re-parks it (task 016's own
       bound), spending nothing further; the gate is untouched by this
       branch (already spent from episode 1).
    3. the SAME rejection recurs a THIRD time, the ruling now exhausted --
       `mechanical_ruled_park_due` is `False`, and `mechanical_reopen_
       trigger` is ALSO `False` because `_discretionary_gate_available`
       already reads back spent, so this rejection takes the plain,
       unpaused cure pass (`rejected_curable_pass_available`) instead of
       any `operator_input_needed` pause -- the reopen gate it would need
       to pause a second time was never there to use.
    """
    root = tmp_path / "run"
    tasks = _lane_fixture_tasks()

    sup0 = _supervisor(root, max_approaches=2, operator_input_needed_pause=True,
                       tasks=tasks)
    sup0._approach = 1
    findings.write_findings_json(sup0.run.findings_json_file, _CURE_REOPEN_FINDING)
    sup0.runner.run = _rejecting_runner("lanes moved")  # type: ignore[method-assign]
    outcome0 = _bounded(sup0._step_review())
    assert outcome0 == ("advance", "paused", "operator_input_needed")
    status0 = sup0.run.read_status()
    # episode 1 already spent the shared gate, via the ordinary mechanical
    # reopen trigger -- no ruling exists yet to have re-parked anything.
    assert status0.get("discretionaryGateSpent") == {"1": True}
    assert not status0.get("rulingReparked")
    sup0.run.add_ruling("Those lanes are residual. Ship it.")
    sup0.resume()

    sup1 = _supervisor(root, max_approaches=2, operator_input_needed_pause=True,
                       tasks=tasks)
    sup1._approach = 1
    findings.write_findings_json(sup1.run.findings_json_file, _CURE_REOPEN_FINDING)
    sup1.runner.run = _rejecting_runner("lanes moved again")  # type: ignore[method-assign]
    outcome1 = _bounded(sup1._step_review())
    assert outcome1 == ("advance", "paused", "operator_input_needed")
    status1 = sup1.run.read_status()
    assert status1.get("rulingReparked") == [1]
    assert not status1.get("curePasses")
    sup1.resume()

    sup2 = _supervisor(root, max_approaches=2, operator_input_needed_pause=True,
                       tasks=tasks)
    sup2._approach = 1
    findings.write_findings_json(sup2.run.findings_json_file, _CURE_REOPEN_FINDING)
    sup2.runner.run = _rejecting_runner("lanes moved a third time")  # type: ignore[method-assign]

    outcome2 = _bounded(sup2._step_review())

    # the third episode -- same ruling, its one re-park already spent --
    # takes the plain cure pass, never a second pause: the reopen gate it
    # would have needed was already spent in episode 1, before this
    # ruling's own re-park bound was even relevant.
    assert outcome2 == ("advance", "cure", "rejected_curable_pass_available")
    status2 = sup2.run.read_status()
    assert status2.get("curePasses") == {"1": 1}
    # unchanged from episode 2 -- no new re-park, no new gate spend.
    assert status2.get("rulingReparked") == [1]
    assert status2.get("discretionaryGateSpent") == {"1": True}
    assert not (root / "approaches" / "01").exists()


def test_A5_same_ruling_bound_survives_actual_replan(tmp_path):
    """Probe 2 (reconstructed): "the A.5 bound is per ruling, not per
    approach." Judged RIGHT against `_record_ruling_reparked`'s own
    docstring (:12082): "additive... including across a replan that opens
    a fresh approach -- a ruling's own re-park is spent for the life of
    the RUN, never reset per approach." `rulingReparked` (`status.json`)
    carries no approach key at all -- unlike `discretionaryGateSpent` and
    `rejectionSignatures`, both keyed `{"<approach>": ...}` and correctly
    reset by a fresh approach -- so the ruling's exhaustion is the one
    counter in this family that must NOT reset. This test drives an
    ACTUAL archive/replan (never a seeded status field) and confirms the
    SAME ruling, recurring by signature in the FRESH approach, still does
    not re-park a second time.

    `rejectionSignatures` is cleared before the approach-2 episode purely
    to isolate this claim from trigger condition 3's own unrelated
    repeated-signature check (`_repeated_rejection_signature`), which would
    otherwise intercept first and pause on ITS OWN, different, pauseReason
    -- never because the ruling bound reset.
    """
    root = tmp_path / "run"
    sup0 = _first_pause_no_ruling_yet(root)
    sup0.run.add_ruling("Waived: ship it, these are residuals.")
    sup0.resume()

    # episode 1: the ruling's one re-park is spent, approach 1.
    sup1 = _supervisor(root, max_approaches=3, operator_input_needed_pause=True)
    sup1._approach = 1
    findings.write_findings_json(sup1.run.findings_json_file, _MIXED_FINDINGS)
    sup1.runner.run = _rejecting_runner("still mixed")  # type: ignore[method-assign]
    outcome1 = _bounded(sup1._step_review())
    assert outcome1 == ("advance", "paused", "operator_input_needed")
    assert sup1.run.read_status().get("rulingReparked") == [1]
    sup1.resume()

    # episode 2: a SECOND rejection on the same (now exhausted) ruling --
    # proceeds to the ordinary archive/replan tail, an ACTUAL replan, never
    # a seeded approach field.
    sup2 = _supervisor(root, max_approaches=3, operator_input_needed_pause=True)
    sup2._approach = 1
    findings.write_findings_json(sup2.run.findings_json_file, _MIXED_FINDINGS)
    sup2.runner.run = _rejecting_runner("still mixed, again")  # type: ignore[method-assign]
    outcome2 = _bounded(sup2._step_review())
    assert outcome2 == ("advance", "replan", "rejected_approach_left")
    assert (root / "approaches" / "01").exists()
    replan_outcome = sup2._step_replan()
    assert replan_outcome[0] == "advance"
    assert sup2._approach == 2
    # the ruling bound survives the boundary untouched -- still one entry.
    assert sup2.run.read_status().get("rulingReparked") == [1]

    # episode 3: fresh approach 2, the identical signature recurs (a
    # contrived but legitimate case -- the same underlying wall was never
    # actually fixed). Isolate from trigger condition 3's own unrelated
    # repeated-signature check by clearing the map it reads.
    sup3 = _supervisor(root, max_approaches=3, operator_input_needed_pause=True)
    sup3.run.update_status(rejectionSignatures={})
    sup3._approach = 2
    findings.write_findings_json(sup3.run.findings_json_file, _MIXED_FINDINGS)
    sup3.runner.run = _rejecting_runner("same wall recurs in approach 2")  # type: ignore[method-assign]

    outcome3 = _bounded(sup3._step_review())

    # the SAME ruling does not buy a second re-park just because a fresh
    # approach began: this falls straight through to the ordinary
    # archive/replan tail again, exactly as an unruled rejection would.
    assert outcome3 == ("advance", "replan", "rejected_approach_left")
    status3 = sup3.run.read_status()
    assert (root / "approaches" / "02").exists()
    # still exactly one entry -- the bound is per RULING, for the life of
    # the run, never reset by the approach boundary just crossed.
    assert status3.get("rulingReparked") == [1]
