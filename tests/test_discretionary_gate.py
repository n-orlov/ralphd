"""Task 017 (PRD requirements A.3/A.7, #224): trigger conditions 2 and 3
over their SHARED discretionary gate, the six-item A.7 escalation payload,
and `state.cure_pass_preview` (task 005's own pure predicate, task 006's
`invalidates_reopen_walk`) that both feed off.

Trigger condition 2 -- a cure pass that would re-open task(s) already
`validated` -- and trigger condition 3 -- a rejection whose blocking-clause
signature repeats the immediately preceding approach's own recorded
signature -- draw from ONE `status.json`-backed counter per approach
(`LoopSupervisor._discretionary_gate_available`/`_spend_discretionary_
gate`): whichever fires first in an approach spends it, the other (should
it also fire later in the SAME approach) falls through to its ordinary
edge instead, and a fresh approach reads the budget unspent again.
Condition 1's own `operator_input_needed` gate (task 014) is a SEPARATE,
unbudgeted pause and is unaffected either way.

Modelled on `tests/test_ruling_re_park_bound.py`'s own multi-episode shape
for the "condition 2 then condition 3" (and its reverse) probes: a FRESH
`LoopSupervisor` per rejection episode over the SAME on-disk run dir,
never shared Python state across episodes.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from ralphd.engine import findings
from ralphd.engine.runner import IterationResult
from ralphd.engine.state import cure_pass_preview

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
             cured_when: str | None = None, invalidates=None) -> dict:
    payload = {"severity": severity, "curable": curable, "clause": clause,
               "evidence": evidence}
    if cured_when is None and severity == "blocking" and curable is True:
        cured_when = f"{clause} is discharged"
    if cured_when is not None:
        payload["cured_when"] = cured_when
    if invalidates is not None:
        payload["invalidates"] = invalidates
    return payload


def _task(tid: str, *, status: str = "pending", depends: list | None = None,
          criteria: str = "n/a") -> dict:
    return {"id": tid, "title": tid, "successCriteria": criteria,
            "status": status, "origin": "planner",
            "dependsOn": list(depends or []), "tier": "normal"}


RECORD_PATH = "artifacts/reports/v0.23-lanes.md"


def _lane_fixture_tasks(record_status: str = "validated",
                        verify_status: str = "validated") -> list:
    return [
        _task("057", status=verify_status, criteria="verify every lane"),
        _task("058", status=record_status, depends=["057"],
              criteria=f"One commit adds {RECORD_PATH} naming the shipped sha."),
    ]


# Neither all-curable nor all-incurable -- the same fixture
# `tests/test_operator_input_needed_pause.py`/`test_ruling_re_park_bound.py`
# use, so this rejection lands on the ordinary archive/replan tail below
# `_review_cure_pass_available` -- never `rejected_incurable`/`rejected_
# curable_pass_available`/`rejected_last_approach`.
_MIXED_FINDINGS = [
    _finding("blocking", False, "requirement B (#111)", "incurable one"),
    _finding("blocking", True, "requirement A.1 (#224)", "curable one"),
]

_CURE_REOPEN_FINDING = [
    _finding("blocking", True, "the shipped sha's lanes moved",
              "a later commit landed after the record",
              invalidates=[RECORD_PATH]),
]

_CURE_INSERT_ONLY_FINDING = [
    _finding("blocking", True, "requirement E.3 (#167)",
              "the record went stale", invalidates=["docs/testing.md"]),
]


def _events(root: Path, type_: str) -> list[dict]:
    log = root / "events.jsonl"
    lines = log.read_text().splitlines() if log.exists() else []
    return [ev for ev in (json.loads(x) for x in lines) if ev.get("type") == type_]


def _rejecting_runner(reason: str):
    async def _run(prompt, transcript, **kw):
        return _result(json.dumps({"verdict": "rejected", "reason": reason}))
    return _run


_supervisor = supervisor_factory(
    run_id="task017-discretionary-gate", cfg_defaults=False,
    prd="# task 017 fixture PRD\n\nDo the thing.\n",
    status={"state": "running", "phase": "review", "approach": 1})


# ===========================================================================
# 1. `state.cure_pass_preview` itself -- the pure predicate, no `RunDir`.
# ===========================================================================


def test_reopened_validated_tasks_are_named_and_deduped():
    tasks = _lane_fixture_tasks(record_status="validated",
                                verify_status="validated")
    finding = _CURE_REOPEN_FINDING[0]
    preview = cure_pass_preview(tasks, [finding, dict(finding)])

    assert set(preview["reopenedTaskIds"]) == {"057", "058"}
    # deduped even though the SAME record path was named by two findings.
    assert len(preview["reopenedTaskIds"]) == 2
    # two findings, one already-recognised record chain each -- no
    # unresolved path, so no extra retake slot either.
    assert preview["insertCount"] == 2


def test_a_reopened_task_not_currently_validated_is_not_named():
    tasks = _lane_fixture_tasks(record_status="completed",
                                verify_status="pending")
    preview = cure_pass_preview(tasks, _CURE_REOPEN_FINDING)

    assert preview["reopenedTaskIds"] == []
    assert preview["insertCount"] == 1


def test_a_plain_retake_path_only_grows_the_insert_count():
    preview = cure_pass_preview([], _CURE_INSERT_ONLY_FINDING)

    assert preview["reopenedTaskIds"] == []
    # one for the cure task itself, one for the brand-new retake task the
    # unresolved path earns.
    assert preview["insertCount"] == 2


def test_no_invalidates_at_all_is_the_plain_not_a_trigger_case():
    plain = [_finding("blocking", True, "requirement E (#113)", "no paths named")]
    preview = cure_pass_preview([], plain)

    assert preview["reopenedTaskIds"] == []
    assert preview["insertCount"] == 1


# ===========================================================================
# 2. Trigger condition 2 over `_step_review`: fires only when the cure
#    pass would re-open an already-`validated` task, never for the
#    not-a-trigger case of a cure pass that only inserts new tasks.
# ===========================================================================


def test_condition_2_holds_before_any_spend_and_carries_the_six_a7_items(
        tmp_path):
    root = tmp_path / "run"
    sup = _supervisor(
        root, max_approaches=2, operator_input_needed_pause=True,
        tasks=_lane_fixture_tasks(),
        status={"state": "running", "phase": "review", "approach": 1,
                "usage": {"byApproach": {"1": {"costUSD": 0.42,
                                               "totalTokens": 321}}}})
    sup._approach = 1
    findings.write_findings_json(sup.run.findings_json_file, _CURE_REOPEN_FINDING)
    sup.runner.run = _rejecting_runner("lanes moved")  # type: ignore[method-assign]

    outcome = _bounded(sup._step_review())

    assert outcome == ("advance", "paused", "operator_input_needed")
    status = sup.run.read_status()
    assert status["pauseReason"] == "operator_input_needed"
    assert status["approach"] == 1
    # never actually taken: no cure pass spent, no reopen landed on disk.
    assert status.get("curePasses") in (None, {})
    tasks = {t["id"]: t for t in sup.run.read_tasks()["tasks"]}
    assert tasks["057"]["status"] == "validated"
    assert tasks["058"]["status"] == "validated"
    assert status.get("discretionaryGateSpent") == {"1": True}

    escalations = _events(root, "operator_escalation")
    assert len(escalations) == 1
    ev = escalations[0]
    assert ev["reason"] == "operator_input_needed"
    # 1: blocking findings verbatim.
    assert ev["findings"] == _CURE_REOPEN_FINDING
    # 2: task ids validated -> pending.
    assert set(ev["taskIdsValidatedToPending"]) == {"057", "058"}
    # 3: count of tasks that would be inserted.
    assert ev["insertCount"] == 1
    # 4: dollars and tokens spent this approach.
    assert ev["costSpentThisApproach"] == "$0.42"
    assert ev["tokensSpentThisApproach"] == 336
    # 5: approach n of N.
    assert ev["approachOfMax"] == "approach 1 of 2"
    # 6: hub deep link.
    assert ev["hubLink"] == f"/#/run/{sup.cfg.run_id}"

    text = ev["escalation"]
    assert "057" in text and "058" in text
    assert "$0.42" in text
    assert "336 tokens" in text
    assert "approach 1 of 2" in text
    assert f"/#/run/{sup.cfg.run_id}" in text


def test_a_cure_pass_that_only_inserts_tasks_is_not_a_trigger(tmp_path):
    root = tmp_path / "run"
    sup = _supervisor(root, max_approaches=2, operator_input_needed_pause=True)
    sup._approach = 1
    findings.write_findings_json(sup.run.findings_json_file,
                                 _CURE_INSERT_ONLY_FINDING)
    sup.runner.run = _rejecting_runner("record went stale")  # type: ignore[method-assign]

    outcome = _bounded(sup._step_review())

    assert outcome == ("advance", "cure", "rejected_curable_pass_available")
    status = sup.run.read_status()
    # the discretionary gate is UNTOUCHED for the not-a-trigger case.
    assert status.get("discretionaryGateSpent") in (None, {})
    assert status.get("curePasses") == {"1": 1}
    assert _events(root, "operator_escalation") == []
    tasks = sup.run.read_tasks()["tasks"]
    assert len(tasks) == 2  # the cure task, plus the one new retake task


# ===========================================================================
# 3. Trigger condition 3 over `_step_review`: a repeated blocking-clause
#    signature raises the SAME shared gate, and carries the six A.7 items
#    too, exactly like condition 2.
# ===========================================================================


def _mixed_signature() -> str:
    """The exact signature `_MIXED_FINDINGS` produces -- a scratch
    supervisor's own `_review_rejection_signature` is a pure function of
    its two arguments (`findings.blocking_finding_clauses` plus a text
    normaliser), so this never has to duplicate that logic by hand."""
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        scratch = _supervisor(Path(d) / "scratch")
        return scratch._review_rejection_signature(_MIXED_FINDINGS, None)


def test_condition_3_holds_before_any_spend_and_carries_the_six_a7_items(
        tmp_path):
    root = tmp_path / "run"
    signature = _mixed_signature()
    sup = _supervisor(
        root, max_approaches=2, operator_input_needed_pause=True,
        status={"state": "running", "phase": "review", "approach": 1,
                "rejectionSignatures": {"0": signature}})
    sup._approach = 1
    findings.write_findings_json(sup.run.findings_json_file, _MIXED_FINDINGS)
    sup.runner.run = _rejecting_runner("mixed curability")  # type: ignore[method-assign]

    outcome = _bounded(sup._step_review())

    assert outcome == ("advance", "paused", "rejected_repeated_signature")
    status = sup.run.read_status()
    assert status["pauseReason"] == "rejected_repeated_signature"
    assert status.get("discretionaryGateSpent") == {"1": True}
    # never archived -- the ordinary archive/replan tail never ran.
    assert not (root / "approaches" / "01").exists()

    escalations = _events(root, "operator_escalation")
    assert len(escalations) == 1
    ev = escalations[0]
    assert ev["reason"] == "rejected_repeated_signature"
    assert ev["signature"] == signature
    assert ev["taskIdsValidatedToPending"] == []
    assert ev["insertCount"] == 2
    assert ev["approachOfMax"] == "approach 1 of 2"
    assert ev["hubLink"] == f"/#/run/{sup.cfg.run_id}"


# ===========================================================================
# 4. The shared budget: one gate for conditions 2 and 3 together, in
#    either order, within one approach; condition 1's own pause is
#    separate/unbudgeted; a fresh approach's own budget is unspent again.
# ===========================================================================


def test_condition_2_then_condition_3_in_one_approach_share_one_gate(
        tmp_path):
    root = tmp_path / "run"
    signature = _mixed_signature()
    # A ficticious approach 0 already recorded this rejection's own
    # signature, so episode B's rejection -- still over approach 1 -- reads
    # as "the same wall as the immediately preceding approach".
    sup_a = _supervisor(
        root, max_approaches=2, operator_input_needed_pause=True,
        tasks=_lane_fixture_tasks(),
        status={"state": "running", "phase": "review", "approach": 1,
                "rejectionSignatures": {"0": signature}})
    sup_a._approach = 1
    findings.write_findings_json(sup_a.run.findings_json_file, _CURE_REOPEN_FINDING)
    sup_a.runner.run = _rejecting_runner("lanes moved")  # type: ignore[method-assign]

    outcome_a = _bounded(sup_a._step_review())
    assert outcome_a == ("advance", "paused", "operator_input_needed")
    assert sup_a.run.read_status().get("discretionaryGateSpent") == {"1": True}
    sup_a.resume()

    # Episode B: fresh supervisor, same run dir, same approach -- a MIXED
    # rejection whose signature matches approach 0's recorded one.
    # `operator_input_needed_pause=False` here so condition 1's own
    # UNBUDGETED gate (which would otherwise fire on any ordinary
    # rejection regardless of the shared budget) never masks what this
    # probe is actually watching: whether condition 3 itself fires.
    sup_b = _supervisor(root, max_approaches=2, operator_input_needed_pause=False)
    sup_b._approach = 1
    findings.write_findings_json(sup_b.run.findings_json_file, _MIXED_FINDINGS)
    sup_b.runner.run = _rejecting_runner("still mixed")  # type: ignore[method-assign]

    outcome_b = _bounded(sup_b._step_review())

    # condition 3 would otherwise fire here -- the gate is already spent
    # for approach 1, so this falls through to the ordinary archive/replan
    # tail instead of a second pause.
    assert outcome_b == ("advance", "replan", "rejected_approach_left")
    status = sup_b.run.read_status()
    assert status["pauseReason"] is None
    assert (root / "approaches" / "01").exists()
    # exactly ONE escalation total across both episodes.
    escalations = _events(root, "operator_escalation")
    assert len(escalations) == 1
    assert escalations[0]["reason"] == "operator_input_needed"


def test_condition_3_then_condition_2_in_one_approach_share_the_same_gate(
        tmp_path):
    root = tmp_path / "run"
    signature = _mixed_signature()
    sup_a = _supervisor(
        root, max_approaches=2, operator_input_needed_pause=True,
        status={"rejectionSignatures": {"0": signature}})
    sup_a._approach = 1
    findings.write_findings_json(sup_a.run.findings_json_file, _MIXED_FINDINGS)
    sup_a.runner.run = _rejecting_runner("mixed curability")  # type: ignore[method-assign]

    outcome_a = _bounded(sup_a._step_review())
    assert outcome_a == ("advance", "paused", "rejected_repeated_signature")
    assert sup_a.run.read_status().get("discretionaryGateSpent") == {"1": True}
    sup_a.resume()

    # Episode B: a cure pass that would re-open validated tasks -- the
    # gate is already spent, so it is taken silently instead of pausing.
    sup_b = _supervisor(root, max_approaches=2, operator_input_needed_pause=True,
                        tasks=_lane_fixture_tasks())
    sup_b._approach = 1
    findings.write_findings_json(sup_b.run.findings_json_file, _CURE_REOPEN_FINDING)
    sup_b.runner.run = _rejecting_runner("lanes moved")  # type: ignore[method-assign]

    outcome_b = _bounded(sup_b._step_review())

    assert outcome_b == ("advance", "cure", "rejected_curable_pass_available")
    tasks = {t["id"]: t for t in sup_b.run.read_tasks()["tasks"]}
    assert tasks["057"]["status"] == "pending"
    assert tasks["058"]["status"] == "pending"
    escalations = _events(root, "operator_escalation")
    assert len(escalations) == 1
    assert escalations[0]["reason"] == "rejected_repeated_signature"


def test_a_condition_2_gate_early_leaves_condition_1_raised_later(tmp_path):
    root = tmp_path / "run"
    sup_a = _supervisor(root, max_approaches=2, operator_input_needed_pause=True,
                        tasks=_lane_fixture_tasks())
    sup_a._approach = 1
    findings.write_findings_json(sup_a.run.findings_json_file, _CURE_REOPEN_FINDING)
    sup_a.runner.run = _rejecting_runner("lanes moved")  # type: ignore[method-assign]

    outcome_a = _bounded(sup_a._step_review())
    assert outcome_a == ("advance", "paused", "operator_input_needed")
    assert sup_a.run.read_status().get("discretionaryGateSpent") == {"1": True}
    sup_a.resume()

    # Episode B: an ordinary mixed rejection with NO repeated signature
    # (no approach 0 ever recorded one) -- condition 1's own unbudgeted
    # gate still raises, even though the shared discretionary budget for
    # approach 1 is already spent.
    sup_b = _supervisor(root, max_approaches=2, operator_input_needed_pause=True)
    sup_b._approach = 1
    findings.write_findings_json(sup_b.run.findings_json_file, _MIXED_FINDINGS)
    sup_b.runner.run = _rejecting_runner("still mixed")  # type: ignore[method-assign]

    outcome_b = _bounded(sup_b._step_review())

    assert outcome_b == ("advance", "paused", "operator_input_needed")
    status = sup_b.run.read_status()
    assert status["pauseReason"] == "operator_input_needed"
    assert status["approach"] == 1
    escalations = _events(root, "operator_escalation")
    assert len(escalations) == 2
    assert [e["reason"] for e in escalations] == [
        "operator_input_needed", "operator_input_needed"]
    # condition 1's own escalation carries no A.7 items -- it is not a
    # driven discretionary gate.
    assert "taskIdsValidatedToPending" not in escalations[1]


def test_the_gate_is_available_again_once_the_approach_is_crossed(tmp_path):
    root = tmp_path / "run"
    sup = _supervisor(root, max_approaches=2, operator_input_needed_pause=True)
    sup._approach = 1
    sup._spend_discretionary_gate(1)
    assert sup._discretionary_gate_available(1) is False
    assert sup._discretionary_gate_available(2) is True
