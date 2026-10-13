"""Task 009 (PRD v0.27 requirement C.2, #274): a bare `ralphctl unpause`/
`POST /resume` of review's A.1 pause-before-archive (`operator_input_
needed`) or the peer verdict gate's own C.1 sibling
(`verdict_gate_operator_input_needed`), with NO ruling filed to answer it,
records a disk-backed one-shot 'proceed' for that rejection's own
signature. The next rejection on that SAME signature consumes it and takes
the archive-and-replan edge (or finishing, when no approach is left)
without parking again -- mirroring the one-re-park bound a ruling already
has (`_ruling_already_reparked`/`_record_ruling_reparked`). A DIFFERENT
rejection never matches, and still parks.

Before this task, #274's own diagnosis held: "Unpausing review's A.1 pause
re-reviews and re-parks forever." `tests/test_operator_ruling_reask.py::
test_resuming_without_any_ruling_at_all_emits_no_honoured_event` used to
pin exactly that loop (rewritten in this same commit, see its own updated
docstring) -- this module is the dedicated coverage for the fix itself,
for both the review and the gate pause.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from ralphd.engine import findings
from ralphd.engine.config import JobConfig
from ralphd.engine.loop import LoopSupervisor
from ralphd.engine.runner import IterationResult
from ralphd.engine.state import RunDir, atomic_write_json
from tests.conftest import supervisor_factory


def _bounded(coro, timeout: float = 30.0):
    return asyncio.run(asyncio.wait_for(coro, timeout))


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


def _events(root: Path, type_: str) -> list[dict]:
    log = root / "events.jsonl"
    lines = log.read_text().splitlines() if log.exists() else []
    return [ev for ev in (json.loads(x) for x in lines) if ev.get("type") == type_]


# Neither all-curable nor all-incurable -- lands on the ordinary
# archive/replan tail `operator_input_needed` intercepts, exactly
# `tests/test_operator_input_needed_pause.py`'s own fixture.
_MIXED_FINDINGS = [
    _finding("blocking", False, "requirement B (#111)", "incurable one"),
    _finding("blocking", True, "requirement A.1 (#224)", "curable one"),
]

# A genuinely different blocking-clause set -- mixed curability like the
# fixture above, but wholly different wording (never merely a different
# digit -- `_normalize_blocker_signature_text` strips digits, so two
# clauses differing only by number still compare equal) -- a different
# rejection, never matching the signature the fixtures above pause on.
_OTHER_FINDINGS = [
    _finding("blocking", False, "the persisted config omits mandatory keys",
             "an unrelated incurable wall"),
    _finding("blocking", True, "the render loop drops trailing bytes",
             "an unrelated curable wall"),
]


_supervisor = supervisor_factory(
    run_id="task009-unpause-one-shot-proceed", cfg_defaults=False,
    prd="# task 009 fixture PRD\n\nDo the thing.\n",
    status={"state": "running", "phase": "review", "approach": 1})


def _reject(sup, reason: str):
    async def _review_rejects(prompt, transcript, **kw):
        return _result(json.dumps({"verdict": "rejected", "reason": reason}))
    sup.runner.run = _review_rejects  # type: ignore[method-assign]


# ===========================================================================
# 1. Review's own A.1 pause.
# ===========================================================================


def test_review_park_unpause_same_rejection_archives_and_replans_once(
        tmp_path):
    root = tmp_path / "run"
    sup1 = _supervisor(root, max_approaches=2, operator_input_needed_pause=True)
    sup1._approach = 1
    findings.write_findings_json(sup1.run.findings_json_file, _MIXED_FINDINGS)
    _reject(sup1, "mixed curability")

    outcome1 = _bounded(sup1._step_review())
    assert outcome1 == ("advance", "paused", "operator_input_needed")

    # a bare unpause, no ruling filed at all.
    sup1.resume()
    status = sup1.run.read_status()
    assert status["state"] == "running"
    assert status.get("noRulingProceed", {}).get("operator_input_needed")

    # a brand-new process reading the SAME on-disk run -- the grant is
    # disk-backed, not an in-memory flag this fresh supervisor never saw.
    sup2 = _supervisor(root, max_approaches=2, operator_input_needed_pause=True)
    sup2._approach = 1
    findings.write_findings_json(sup2.run.findings_json_file, _MIXED_FINDINGS)
    _reject(sup2, "the same wall, unmoved")

    outcome2 = _bounded(sup2._step_review())

    assert outcome2 == ("advance", "replan", "rejected_approach_left")
    status2 = sup2.run.read_status()
    assert status2["state"] == "running"
    assert not status2.get("noRulingProceed", {}).get("operator_input_needed")
    # archived, exactly like an ordinary un-paused rejection would be.
    assert (root / "approaches" / "01").exists()

    escalations = _events(root, "operator_escalation")
    assert len(escalations) == 1
    assert escalations[0]["reason"] == "operator_input_needed"


def test_review_park_unpause_different_rejection_still_parks(tmp_path):
    root = tmp_path / "run"
    sup1 = _supervisor(root, max_approaches=2, operator_input_needed_pause=True)
    sup1._approach = 1
    findings.write_findings_json(sup1.run.findings_json_file, _MIXED_FINDINGS)
    _reject(sup1, "mixed curability")

    outcome1 = _bounded(sup1._step_review())
    assert outcome1 == ("advance", "paused", "operator_input_needed")

    sup1.resume()

    sup2 = _supervisor(root, max_approaches=2, operator_input_needed_pause=True)
    sup2._approach = 1
    # a genuinely different rejection -- must not consume the proceed
    # granted for the earlier signature.
    findings.write_findings_json(sup2.run.findings_json_file, _OTHER_FINDINGS)
    _reject(sup2, "a fresh wall")

    outcome2 = _bounded(sup2._step_review())

    assert outcome2 == ("advance", "paused", "operator_input_needed")
    status2 = sup2.run.read_status()
    # v0.36: the park lands in `awaiting_input` (covering node: tests/test_awaiting_input_question.py)
    assert status2["state"] == "awaiting_input"
    assert status2["pauseReason"] == "operator_input_needed"
    assert not (root / "approaches" / "01").exists()

    escalations = _events(root, "operator_escalation")
    assert len(escalations) == 2


# ===========================================================================
# 2. The gate's own C.1 pause (task 008).
# ===========================================================================


def _gate_findings_a() -> list[dict]:
    return [
        {"severity": "blocking", "curable": True,
         "clause": "requirement C.1 (v0.27)",
         "evidence": "the peer gate found an unmet requirement",
         "cured_when": "a cure task adds the missing behaviour"},
    ]


def _gate_findings_b() -> list[dict]:
    return [
        {"severity": "blocking", "curable": True,
         "clause": "the peer gate omits a required checkpoint field",
         "evidence": "the peer gate found a wholly unrelated requirement",
         "cured_when": "a cure task adds the other missing behaviour"},
    ]


def _gate_rejects(reason: str):
    async def handler(self, phase, extra="", *a, **kw):
        r = IterationResult(exit_code=0)
        r.final_text = json.dumps({"verdict": "rejected", "reason": reason})
        return r
    return handler


def _gate_fixture(root: Path, *, gate_findings: list[dict]) -> LoopSupervisor:
    root.mkdir(parents=True, exist_ok=True)
    run = RunDir(root=root)
    run.update_status(state="running")
    if not run.prd_file.exists():
        run.prd_file.write_text("# task 009 gate fixture PRD\n\nDo the thing.\n")
    if not run.tasks_file.exists():
        atomic_write_json(run.tasks_file, {"version": 1, "tasks": []})
    cfg = JobConfig(run_id="task009-gate-unpause-one-shot", iterations=5,
                    max_approaches=2, vigilant=False,
                    peer_verdict_gate=True, cure_pass_ceiling=0,
                    infra_retry_backoff_s=[0.0], infra_retry_backoff_max_s=0.0)
    sup = LoopSupervisor(cfg, run, root)
    sup._approach = 1
    findings.write_findings_json(run.verdict_gate_findings_json_file,
                                 gate_findings)
    return sup


def test_gate_park_unpause_same_rejection_archives_and_replans_once(
        tmp_path):
    root = tmp_path / "run"
    sup1 = _gate_fixture(root, gate_findings=_gate_findings_a())

    orig = LoopSupervisor.run_iteration
    LoopSupervisor.run_iteration = _gate_rejects("curable but ceiling spent")
    try:
        outcome1 = _bounded(sup1._step_verdict_gate())
    finally:
        LoopSupervisor.run_iteration = orig

    assert outcome1 == ("advance", "paused", "verdict_gate_operator_input_needed")

    sup1.resume()
    status = sup1.run.read_status()
    assert status["state"] == "running"
    assert status.get("noRulingProceed", {}).get(
        "verdict_gate_operator_input_needed")

    # a brand-new supervisor over the same disk-backed run.
    sup2 = _gate_fixture(root, gate_findings=_gate_findings_a())

    orig = LoopSupervisor.run_iteration
    LoopSupervisor.run_iteration = _gate_rejects("the same wall, unmoved")
    try:
        outcome2 = _bounded(sup2._step_verdict_gate())
    finally:
        LoopSupervisor.run_iteration = orig

    assert outcome2 == ("advance", "replan", "verdict_gate_rejected_approach_left")
    status2 = sup2.run.read_status()
    assert status2["state"] == "running"
    assert not status2.get("noRulingProceed", {}).get(
        "verdict_gate_operator_input_needed")
    assert (root / "approaches" / "01").exists()

    escalations = _events(root, "operator_escalation")
    assert len(escalations) == 1
    assert escalations[0]["reason"] == "verdict_gate_operator_input_needed"


def test_gate_park_unpause_different_rejection_still_parks(tmp_path):
    root = tmp_path / "run"
    sup1 = _gate_fixture(root, gate_findings=_gate_findings_a())

    orig = LoopSupervisor.run_iteration
    LoopSupervisor.run_iteration = _gate_rejects("curable but ceiling spent")
    try:
        outcome1 = _bounded(sup1._step_verdict_gate())
    finally:
        LoopSupervisor.run_iteration = orig
    assert outcome1 == ("advance", "paused", "verdict_gate_operator_input_needed")

    sup1.resume()

    # a genuinely different gate rejection -- must not consume the proceed
    # granted for the earlier signature.
    sup2 = _gate_fixture(root, gate_findings=_gate_findings_b())

    orig = LoopSupervisor.run_iteration
    LoopSupervisor.run_iteration = _gate_rejects("a fresh gate wall")
    try:
        outcome2 = _bounded(sup2._step_verdict_gate())
    finally:
        LoopSupervisor.run_iteration = orig

    assert outcome2 == ("advance", "paused", "verdict_gate_operator_input_needed")
    status2 = sup2.run.read_status()
    # v0.36: the park lands in `awaiting_input` (covering node: tests/test_awaiting_input_question.py)
    assert status2["state"] == "awaiting_input"
    assert status2["pauseReason"] == "verdict_gate_operator_input_needed"
    assert not (root / "approaches" / "01").exists()

    escalations = _events(root, "operator_escalation")
    assert len(escalations) == 2


# ===========================================================================
# 3. Task 002 (this wave): a mixed (curable + non-curable) blocking set --
#    the spent-ceiling pause now widens to this shape too. A bare unpause
#    with no ruling records the one-shot proceed exactly like the all-
#    curable case above, and the SAME rejection's next run consumes it and
#    takes its OWN terminal edge (the immediate merits tail, never a fresh
#    replan -- A.1/A.2's own removal-specific rule) with no second
#    escalation.
# ===========================================================================


def _mixed_1c_1i_gate_findings() -> list[dict]:
    return [
        {"severity": "blocking", "curable": True,
         "clause": "requirement C.1 mixed curable clause (v0.27)",
         "evidence": "the peer gate found a fixable gap",
         "cured_when": "a cure task adds the missing behaviour"},
        {"severity": "blocking", "curable": False,
         "clause": "requirement C.1 mixed non-curable clause (v0.27)",
         "evidence": "an environment fact no cure task or fresh approach "
                    "can change"},
    ]


def test_gate_park_unpause_mixed_rejection_proceeds_to_the_unanswered_outcome(
        tmp_path):
    root = tmp_path / "run"
    sup1 = _gate_fixture(root, gate_findings=_mixed_1c_1i_gate_findings())

    orig = LoopSupervisor.run_iteration
    LoopSupervisor.run_iteration = _gate_rejects(
        "one blocking finding is curable, the other is not")
    try:
        outcome1 = _bounded(sup1._step_verdict_gate())
    finally:
        LoopSupervisor.run_iteration = orig
    assert outcome1 == ("advance", "paused", "verdict_gate_operator_input_needed")

    # a bare unpause, no ruling filed at all.
    sup1.resume()
    status = sup1.run.read_status()
    assert status["state"] == "running"
    assert status.get("noRulingProceed", {}).get(
        "verdict_gate_operator_input_needed")

    # a brand-new supervisor over the same disk-backed run, SAME rejection.
    sup2 = _gate_fixture(root, gate_findings=_mixed_1c_1i_gate_findings())

    orig = LoopSupervisor.run_iteration
    LoopSupervisor.run_iteration = _gate_rejects(
        "the same wall, unmoved")
    try:
        outcome2 = _bounded(sup2._step_verdict_gate())
    finally:
        LoopSupervisor.run_iteration = orig

    # v0.36 requirement B: the consumed no-ruling proceed is the park's
    # unanswered outcome -- the mixed (non-curable) rejection archives and
    # replans while an approach is left; it never ends the run on the spot.
    assert outcome2 == ("advance", "replan", "verdict_gate_rejected_approach_left")
    status2 = sup2.run.read_status()
    assert status2["state"] == "running"
    assert status2.get("gateRejectionEnding") == "rejected_on_the_merits"
    assert not status2.get("noRulingProceed", {}).get(
        "verdict_gate_operator_input_needed")
    assert (root / "approaches" / "01").exists()

    # no SECOND escalation -- the one from the first parked rejection is
    # still the only one on this run.
    escalations = _events(root, "operator_escalation")
    assert len(escalations) == 1
