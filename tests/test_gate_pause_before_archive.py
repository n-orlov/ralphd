"""Task 008 (v0.27 requirement C.1): the peer verdict gate's own mirror of
`TABLE["review"]`'s A.1 pause-before-archive edge (task 014, PRD
requirement A.1, #224) -- an ordinary gate rejection that would otherwise
reach `_archive_approach`/`_write_composite_prd` holds instead when it
would have earned the cure-pass edge (`verdict_gate_rejected_curable_pass_
available`) if the run-wide `cure_pass_ceiling` were not already spent
(`LoopSupervisor._gate_rejection_ending` reporting `state.GATE_REJECTION_
ENDING_CURE_UNAVAILABLE`), ON by default through the SAME `JobConfig.
operator_input_needed_pause` flag review's own edge reads.

Before this task `_step_verdict_gate` had no such edge: a rejection whose
findings were all curable but whose ceiling was spent fell straight
through to the ordinary archive/replan-or-finish edges, spending (and
losing) the approach on findings the run had no cure budget left to fix
faster than a fresh approach would either -- exactly the gap A.1 already
closed for review's own cure channel, left open for the gate's.

Drives `_step_verdict_gate` directly on a fresh `LoopSupervisor`/`RunDir`
fixture (never this run's own engine), mirroring `tests/test_peer_verdict_
gate.py::test_gate_rejection_all_curable_takes_a_cure_pass_not_a_replan`'s
own fixture shape.
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


def _bounded(coro, timeout: float = 30.0):
    return asyncio.run(asyncio.wait_for(coro, timeout))


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


def _fixture(tmp_path: Path, *, cure_pass_ceiling: int,
            max_approaches: int = 2) -> LoopSupervisor:
    root = tmp_path / "run"
    root.mkdir(parents=True, exist_ok=True)
    run = RunDir(root=root)
    run.update_status(state="running")
    atomic_write_json(run.tasks_file, {"version": 1, "tasks": []})
    cfg = JobConfig(run_id="task008-gate-pause", iterations=5,
                    max_approaches=max_approaches, vigilant=False,
                    peer_verdict_gate=True, cure_pass_ceiling=cure_pass_ceiling,
                    infra_retry_backoff_s=[0.0], infra_retry_backoff_max_s=0.0)
    sup = LoopSupervisor(cfg, run, root)
    sup._approach = 1
    findings.write_findings_json(run.verdict_gate_findings_json_file,
                                 _all_curable_gate_findings())
    return sup


def _escalations(root: Path) -> list[dict]:
    log = root / "events.jsonl"
    lines = log.read_text().splitlines() if log.exists() else []
    events = [json.loads(line) for line in lines]
    return [ev for ev in events if ev.get("type") == "operator_escalation"]


def test_gate_rejection_with_ceiling_spent_parks_instead_of_archiving(
        tmp_path):
    """`cure_pass_ceiling=0`: every check `_gate_cure_pass_available` makes
    passes EXCEPT the run-wide ceiling comparison -- `_gate_rejection_
    ending` reports `GATE_REJECTION_ENDING_CURE_UNAVAILABLE`, and
    `operator_input_needed_pause` is on (the default), so the gate must
    hold instead of taking the ordinary archive/replan-or-finish edges.
    """
    sup = _fixture(tmp_path, cure_pass_ceiling=0, max_approaches=2)
    root = sup.run.root
    assert sup._pause.is_set() is True  # not paused yet

    orig = LoopSupervisor.run_iteration
    LoopSupervisor.run_iteration = _gate_rejects(
        "the one blocking finding is curable")
    try:
        outcome = _bounded(sup._step_verdict_gate())
    finally:
        LoopSupervisor.run_iteration = orig

    assert outcome == ("advance", "paused", "verdict_gate_operator_input_needed")
    # paused through the EXISTING pause machinery.
    assert sup._pause.is_set() is False
    status = sup.run.read_status()
    assert status.get("state") == "paused"
    assert status.get("pauseReason") == "verdict_gate_operator_input_needed"
    assert status.get("gateRejectionEnding") == "rejected_cure_unavailable"

    # never archived, never a composite PRD, never a fresh cure pass --
    # resume re-enters `verdict_gate` over the SAME approach.
    assert sup._approach == 1
    assert not (root / "approaches" / "01").exists()
    assert not sup.run.composite_prd_file.exists()
    assert status.get("gateCurePasses") is None

    escalations = _escalations(root)
    assert len(escalations) == 1
    assert escalations[0]["reason"] == "verdict_gate_operator_input_needed"
    assert escalations[0]["approach"] == 1
    assert escalations[0]["findings"][0]["clause"] == "requirement C.1 (v0.27)"
    assert "requirement C.1 (v0.27)" in escalations[0]["escalation"]


def test_gate_rejection_with_a_pass_left_still_takes_the_cure_edge(
        tmp_path):
    """The complement: with the run-wide ceiling NOT spent
    (`cure_pass_ceiling`'s default, 3, and no cure pass taken yet this
    run), the identical all-curable rejection still takes the ordinary
    cure-pass edge (`verdict_gate_rejected_curable_pass_available`) --
    this task's new pause edge must never preempt a cure pass that is
    actually available.
    """
    sup = _fixture(tmp_path, cure_pass_ceiling=3, max_approaches=2)
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
    status = sup.run.read_status()
    assert status.get("gateRejectionEnding") == "rejected_cure_taken"
    assert status.get("gateCurePasses") == {"1": 1}
    assert status.get("state") != "paused"
    assert not (root / "approaches" / "01").exists()
    assert _escalations(root) == []
