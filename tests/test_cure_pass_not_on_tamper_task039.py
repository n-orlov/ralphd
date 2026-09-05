"""Task 039 (operator steer 002): a review rejection that carries a
criteria-tamper finding must NEVER take this wave's cure-pass edge
(`rejected_curable_pass_available`, task 009, requirement C.1, #141).

Why this pin exists. The engine's own criteria-tamper finding
(`findings.flagged_criteria_findings`, task 024, #116 section 2) is
deliberately `curable: True` -- a *fresh* approach that never rewrites its
own `successCriteria` again does not repeat the violation. But curing is not
a fresh approach: it hands the SAME approach, the one that already rewrote
its criteria, another worker pass over the very tasks whose criteria are in
dispute. Until this task, `_review_cure_pass_available` looked only at the
`curable` flags, so a tamper rejection took the cure pass and the archived
approach ended up holding an engine-appended third task -- which is exactly
how `tests/test_e2e.py::test_criteria_edited_after_validation_failure_is_
refused` (green at `1c61b32`, red at `b1f54a4`) caught the regression. A
tampered approach is archived and replanned exactly as it was before the
cure-pass edge existed.

Three levels, all against the real code:

1. the recogniser itself (`findings.any_criteria_tamper_finding`), including
   its deliberate severity-blindness;
2. a real supervisor driven through `_step_review()` over a flagged task ->
   `rejected_approach_left`, no `curePasses`, no `review-cure` task, no
   `approach.cure_pass` event;
3. the paired control: the identical fixture with the tamper marker removed
   DOES take the cure pass, so level 2 is proof of the new gate and not of
   an accidentally uncurable fixture.
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

TAMPERED_TASK_ID = "001"


def _finding(severity: str, curable: bool, clause: str, evidence: str) -> dict:
    return {"severity": severity, "curable": curable, "clause": clause,
            "evidence": evidence}


def _result(text: str) -> IterationResult:
    r = IterationResult(exit_code=0)
    r.final_text = text
    r.duration_s = 5.0
    r.usage = {"input": 10, "output": 5, "totalTokens": 15}
    return r


def _supervisor(root: Path, **cfg_kw) -> LoopSupervisor:
    root.mkdir(parents=True, exist_ok=True)
    run = RunDir(root=root)
    run.update_status(state="running")
    (root / "prd.md").write_text("# task 039 fixture PRD\n\nDo the thing.\n")
    cfg = JobConfig(run_id="task039-tamper-not-curable", max_approaches=3,
                    **cfg_kw)
    return LoopSupervisor(cfg, run, root)


def _events(root: Path, type_: str) -> list[dict]:
    log = root / "events.jsonl"
    lines = log.read_text().splitlines() if log.exists() else []
    return [ev for ev in (json.loads(x) for x in lines)
            if ev.get("type") == type_]


def _task(tampered: bool) -> dict:
    task = {
        "id": TAMPERED_TASK_ID,
        "title": "the task whose criteria were rewritten",
        "successCriteria": "the ORIGINAL, authoritative criteria text",
        "criteriaOfRecord": "the ORIGINAL, authoritative criteria text",
        "status": "completed",
        "validationAttempts": 1,
    }
    if tampered:
        task["criteriaEditedAfterValidationFailure"] = True
    return task


def _drive_review_over(sup: LoopSupervisor) -> tuple:
    """One real `_step_review()` pass whose review payload rejects, with a
    single blocking-but-curable finding of its own in the sidecar (so the
    only difference between the two cases below is the task marker)."""
    findings.write_findings_json(sup.run.findings_json_file, [
        _finding("blocking", True, "requirement C.1 (#141)",
                 "the worker never wrote the notify tool"),
    ])

    async def _review_rejects(prompt, transcript, **kw):
        return _result(json.dumps({
            "verdict": "rejected",
            "reason": "one blocking finding, curable on its face",
        }))

    sup.runner.run = _review_rejects  # type: ignore[method-assign]
    return asyncio.run(sup._step_review())


# ---------------------------------------------------------------------------
# 1. The recogniser.
# ---------------------------------------------------------------------------


def test_the_engines_own_tamper_finding_is_recognised():
    flagged = findings.flagged_criteria_findings([_task(tampered=True)])
    assert len(flagged) == 1
    assert findings.CRITERIA_TAMPER_CLAUSE_MARKER in flagged[0]["clause"]
    assert findings.any_criteria_tamper_finding(flagged) is True
    # and it really is the curable-flagged finding whose remedy this task
    # withholds -- the gate cannot be derived from `curable` alone.
    assert flagged[0]["curable"] is True


def test_an_ordinary_findings_list_is_not_mistaken_for_a_tamper_finding():
    assert findings.any_criteria_tamper_finding([]) is False
    assert findings.any_criteria_tamper_finding([
        _finding("blocking", True, "requirement C.1 (#141)", "curable one"),
        _finding("residual", True, "requirement D.3 (#142)", "cosmetic"),
    ]) is False


def test_the_recogniser_is_severity_blind_on_purpose():
    """A downgraded copy must not buy the cure pass back: the recogniser
    answers on the FACT, not on the severity the fact currently carries."""
    downgraded = findings.flagged_criteria_findings([_task(tampered=True)])
    downgraded[0]["severity"] = "residual"
    assert findings.any_criteria_tamper_finding(downgraded) is True


# ---------------------------------------------------------------------------
# 2. A real `_step_review()` over a flagged task: replan, never cure.
# ---------------------------------------------------------------------------


def test_a_tamper_rejection_never_takes_the_cure_pass_edge(tmp_path):
    root = tmp_path / "run"
    sup = _supervisor(root)
    sup._approach = 1
    atomic_write_json(sup.run.tasks_file,
                      {"version": 1, "tasks": [_task(tampered=True)]})

    outcome = _drive_review_over(sup)

    # the ordinary archive/replan edge, exactly as before the cure-pass edge
    # existed -- not `rejected_curable_pass_available`.
    assert outcome == ("advance", "replan", "rejected_approach_left")

    # no counter, no appended cure task, no event.
    assert sup.run.read_status().get("curePasses") is None
    assert [t for t in sup.run.read_tasks()["tasks"]
            if t.get("origin") == "review-cure"] == []
    assert _events(root, "approach.cure_pass") == []

    # the engine's own tamper finding really was in the channel this
    # decision was made over (i.e. the case is live, not vacuous).
    channel = json.loads(sup.run.findings_json_file.read_text())
    assert any(findings.CRITERIA_TAMPER_CLAUSE_MARKER in f.get("clause", "")
               for f in channel)


def test_the_gate_holds_even_with_a_cure_pass_still_unspent(tmp_path):
    """`curePasses[approach] == 0` is the cure pass's *other* precondition;
    this pins that a tamper rejection is refused the edge for its own
    reason, with the budget provably untouched before and after."""
    root = tmp_path / "run"
    sup = _supervisor(root)
    sup._approach = 1
    atomic_write_json(sup.run.tasks_file,
                      {"version": 1, "tasks": [_task(tampered=True)]})
    assert sup._cure_passes_for_approach(1) == 0

    assert sup._review_cure_pass_available(1) is False
    assert sup._cure_passes_for_approach(1) == 0


# ---------------------------------------------------------------------------
# 3. The paired control: the same fixture, no marker -> the cure pass fires.
# ---------------------------------------------------------------------------


def test_the_same_fixture_without_the_marker_does_take_the_cure_pass(tmp_path):
    root = tmp_path / "run"
    sup = _supervisor(root)
    sup._approach = 1
    atomic_write_json(sup.run.tasks_file,
                      {"version": 1, "tasks": [_task(tampered=False)]})

    outcome = _drive_review_over(sup)

    assert outcome == ("advance", "worker", "rejected_curable_pass_available")
    assert sup.run.read_status().get("curePasses") == {"1": 1}
    assert len([t for t in sup.run.read_tasks()["tasks"]
                if t.get("origin") == "review-cure"]) == 1
    assert len(_events(root, "approach.cure_pass")) == 1
