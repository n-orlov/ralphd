"""Task 011 (v0.27 requirement C.4, #275): an A.7 `operator_escalation`
preview (`insertCount`, `taskIdsValidatedToPending`, `nextStepIfUnanswered`)
names the edge the engine will ACTUALLY take if the pause is not answered
-- a cure pass only when one is genuinely available -- never a stale,
real-looking reopen/insert prediction left over from a rejection that
WOULD have earned a cure pass had the run-wide `cure_pass_ceiling`
(`JobConfig.cure_pass_ceiling`, default 3) not already been spent.

Before this task, `_pause_and_escalate_operator_input_needed`/`_pause_and_
escalate_repeated_rejection_signature` fed the caller's own real, non-zero
`cure_preview` (or `_effective_cure_preview`'s fresh walk over the SAME
findings) straight through even when `_review_cure_pass_available`
already said no cure pass would run -- an operator reading `insertCount:
1`/`taskIdsValidatedToPending: ["057"]` on a `nextStepIfUnanswered:
"archive_and_replan"` escalation was told two contradictory things: the
edge that will run (archive-and-replan) and a reopen/insert count that
edge will never produce. And the peer verdict gate's own C.1 mirror,
`_pause_and_escalate_gate_operator_input_needed` (which fires ONLY when
ceiling-spent is the blocker, `GATE_REJECTION_ENDING_CURE_UNAVAILABLE`),
carried no A.7 fields on its `operator_escalation` event at all.

Both sides now zero the preview (and name `ralphctl budget <run>
--cure-passes` in the escalation text) exactly when the run-wide ceiling
being spent is what stopped an otherwise-curable rejection from taking a
cure pass -- never for a rejection that was never curable at all (mixed
findings, tamper, a superset repeat), whose own honest partial preview is
untouched (`tests/test_discretionary_gate.py`'s own `insertCount == 2`
pins for `_MIXED_FINDINGS` stay green).
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from ralphd.engine import findings
from ralphd.engine.config import JobConfig
from ralphd.engine.loop import LoopSupervisor
from ralphd.engine.runner import IterationResult
from ralphd.engine.state import (
    NEXT_STEP_ARCHIVE_AND_REPLAN,
    NEXT_STEP_CURE_PASS,
    RunDir,
    atomic_write_json,
)

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


RECORD_PATH = "artifacts/reports/task011-lanes.md"


def _lane_tasks() -> list:
    # `058` is `validated` and depends on `057` (also `validated`) --
    # `_CURE_REOPEN_FINDING`'s own `invalidates` path reopens both.
    return [
        _task("057", status="validated", criteria="verify every lane"),
        _task("058", status="validated", depends=["057"],
              criteria=f"One commit adds {RECORD_PATH} naming the shipped sha."),
    ]


# All-curable, and its own `invalidates` path reopens both `_lane_tasks()`
# rows -- the SAME fixture shape `tests/test_discretionary_gate.py`'s own
# `_CURE_REOPEN_FINDING` uses, so a genuinely available cure pass over it
# predicts a real, non-zero reopen/insert count.
_CURE_REOPEN_FINDING = [
    _finding("blocking", True, "the shipped sha's lanes moved",
              "a later commit landed after the record",
              invalidates=[RECORD_PATH]),
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
    run_id="task011-escalation-preview-real-edge", cfg_defaults=False,
    prd="# task 011 fixture PRD\n\nDo the thing.\n",
    status={"state": "running", "phase": "review", "approach": 1})


# ===========================================================================
# 1. Review's own `operator_input_needed` escalation, ceiling spent: the
#    curable, would-reopen-tasks rejection above CANNOT take a cure pass
#    (`_review_cure_pass_available` is False on the ceiling alone), so the
#    unbudgeted condition-1 pause-before-archive gate is what actually
#    fires -- and its preview must read the edge it will actually take.
# ===========================================================================


def test_review_escalation_with_ceiling_spent_names_archive_and_replan(
        tmp_path):
    root = tmp_path / "run"
    sup = _supervisor(
        root, max_approaches=2, operator_input_needed_pause=True,
        cure_pass_ceiling=0, tasks=_lane_tasks(),
        status={"state": "running", "phase": "review", "approach": 1})
    sup._approach = 1
    findings.write_findings_json(sup.run.findings_json_file, _CURE_REOPEN_FINDING)
    sup.runner.run = _rejecting_runner("lanes moved")  # type: ignore[method-assign]

    outcome = _bounded(sup._step_review())

    # never a cure pass: the ceiling is spent, so `_review_cure_pass_
    # available` refuses it and the ordinary A.1 pause-before-archive gate
    # (task 014, unbudgeted) is what actually holds this rejection.
    assert outcome == ("advance", "paused", "operator_input_needed")
    assert sup.run.read_status().get("curePasses") in (None, {})
    tasks = {t["id"]: t for t in sup.run.read_tasks()["tasks"]}
    assert tasks["057"]["status"] == "validated"  # never reopened on disk

    escalations = _events(root, "operator_escalation")
    assert len(escalations) == 1
    ev = escalations[0]
    assert ev["reason"] == "operator_input_needed"
    # the preview names the edge that will ACTUALLY be taken -- archive-
    # and-replan, not the cure pass this ceiling refuses -- so the reopen/
    # insert numbers read zero/empty, never the real {"057", "058"}/1 a
    # genuinely available cure pass over this SAME finding would predict.
    assert ev["taskIdsValidatedToPending"] == []
    assert ev["insertCount"] == 0
    assert ev["nextStepIfUnanswered"] == NEXT_STEP_ARCHIVE_AND_REPLAN

    text = ev["escalation"]
    assert "ralphctl budget" in text
    assert "--cure-passes" in text
    assert "tasks that would be inserted: 0" in text


# ===========================================================================
# 2. The complement: with the ceiling NOT spent, the identical rejection
#    still takes trigger condition 2's own driven gate, whose preview
#    already named the real edge before this task -- unchanged by it.
# ===========================================================================


def test_review_escalation_with_a_pass_left_keeps_the_real_preview(
        tmp_path):
    root = tmp_path / "run"
    sup = _supervisor(
        root, max_approaches=2, operator_input_needed_pause=True,
        tasks=_lane_tasks(),
        status={"state": "running", "phase": "review", "approach": 1})
    sup._approach = 1
    findings.write_findings_json(sup.run.findings_json_file, _CURE_REOPEN_FINDING)
    sup.runner.run = _rejecting_runner("lanes moved")  # type: ignore[method-assign]

    outcome = _bounded(sup._step_review())

    assert outcome == ("advance", "paused", "operator_input_needed")
    escalations = _events(root, "operator_escalation")
    assert len(escalations) == 1
    ev = escalations[0]
    # a cure pass IS available here (ceiling unspent) -- the preview keeps
    # naming it, real reopened ids and a non-zero insert count, exactly as
    # it did before this task.
    assert set(ev["taskIdsValidatedToPending"]) == {"057", "058"}
    assert ev["insertCount"] == 1
    assert ev["nextStepIfUnanswered"] == NEXT_STEP_CURE_PASS

    text = ev["escalation"]
    assert "057" in text and "058" in text
    # no ceiling-spent note: a pass is genuinely available.
    assert "--cure-passes" not in text


# ===========================================================================
# 3. The peer verdict gate's own C.1 mirror, ceiling spent -- fires ONLY
#    for the curable-but-ceiling-spent edge, and carried NO A.7 fields at
#    all before this task.
# ===========================================================================


def _all_curable_gate_findings() -> list[dict]:
    return [
        {"severity": "blocking", "curable": True,
         "clause": "requirement C.1 (v0.27)",
         "evidence": "the peer gate found an unmet requirement",
         "cured_when": "a cure task adds the missing behaviour"},
    ]


def _gate_rejects(reason: str):
    async def handler(self, phase, extra="", *a, **kw):
        r = IterationResult(exit_code=0)
        r.final_text = json.dumps({"verdict": "rejected", "reason": reason})
        return r
    return handler


def _gate_fixture(tmp_path: Path, *, cure_pass_ceiling: int,
                  max_approaches: int = 2) -> LoopSupervisor:
    root = tmp_path / "run"
    root.mkdir(parents=True, exist_ok=True)
    run = RunDir(root=root)
    run.update_status(state="running")
    atomic_write_json(run.tasks_file, {"version": 1, "tasks": []})
    cfg = JobConfig(run_id="task011-gate-preview", iterations=5,
                    max_approaches=max_approaches, vigilant=False,
                    peer_verdict_gate=True, cure_pass_ceiling=cure_pass_ceiling,
                    infra_retry_backoff_s=[0.0], infra_retry_backoff_max_s=0.0)
    sup = LoopSupervisor(cfg, run, root)
    sup._approach = 1
    findings.write_findings_json(run.verdict_gate_findings_json_file,
                                 _all_curable_gate_findings())
    return sup


def test_gate_escalation_with_ceiling_spent_names_archive_and_replan(
        tmp_path):
    sup = _gate_fixture(tmp_path, cure_pass_ceiling=0, max_approaches=2)
    root = sup.run.root

    orig = LoopSupervisor.run_iteration
    LoopSupervisor.run_iteration = _gate_rejects(
        "the one blocking finding is curable")
    try:
        outcome = _bounded(sup._step_verdict_gate())
    finally:
        LoopSupervisor.run_iteration = orig

    assert outcome == ("advance", "paused", "verdict_gate_operator_input_needed")
    escalations = _events(root, "operator_escalation")
    assert len(escalations) == 1
    ev = escalations[0]
    assert ev["reason"] == "verdict_gate_operator_input_needed"
    # never a cure prediction: the ceiling being spent is the ONLY reason
    # this pause fires at all -- a cure pass is never taken by this edge.
    assert ev["taskIdsValidatedToPending"] == []
    assert ev["insertCount"] == 0
    assert ev["nextStepIfUnanswered"] == NEXT_STEP_ARCHIVE_AND_REPLAN

    text = ev["escalation"]
    assert "ralphctl budget" in text
    assert "--cure-passes" in text


# ===========================================================================
# 4. The complement on the gate side: with a pass left, this pause never
#    fires at all -- there is no preview to name, because the ordinary
#    cure-pass edge runs instead (`tests/test_gate_pause_before_archive.
#    py`'s own coverage; pinned again here for this module's own subject).
# ===========================================================================


def test_gate_escalation_never_fires_with_a_pass_left(tmp_path):
    sup = _gate_fixture(tmp_path, cure_pass_ceiling=3, max_approaches=2)
    root = sup.run.root

    orig = LoopSupervisor.run_iteration
    LoopSupervisor.run_iteration = _gate_rejects(
        "the one blocking finding is curable")
    try:
        outcome = _bounded(sup._step_verdict_gate())
    finally:
        LoopSupervisor.run_iteration = orig

    assert outcome == ("advance", "cure",
                       "verdict_gate_rejected_curable_pass_available")
    assert _events(root, "operator_escalation") == []
