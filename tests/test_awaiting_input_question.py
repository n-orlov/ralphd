"""v0.36 requirement B/C (task: `awaiting_input` with a persisted
`inputQuestion`): the findings parks that 0.35 took as `operator_input_needed`,
`verdict_gate_operator_input_needed`, `rejected_incurable` (held in memory only,
so `status.json` kept saying `running`) and `rejected_last_approach` now write
`state: "awaiting_input"` to `status.json` BEFORE the engine waits, never
`state: "paused"` (which is the operator's alone), carry an `inputQuestion`
and emit `input.parked`.

Each node drives the real `_step_review` / `_step_verdict_gate` through a
scripted judge reply (fake hats), with a fixed clock for the park time, and
asserts the state, every `inputQuestion` field and the event. Against the 0.35
code they fail: the state is `paused` (or `running` for the incurable hold),
there is no `inputQuestion` key and no `input.parked` event.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from ralphd.engine import findings
from ralphd.engine.config import JobConfig
from ralphd.engine.loop import LoopSupervisor
from ralphd.engine.runner import IterationResult
from ralphd.engine.state import (
    AWAITING_INPUT_STATE,
    INPUT_ON_TIMEOUT_CURE_NOW,
    INPUT_ON_TIMEOUT_EXHAUSTED,
    INPUT_ON_TIMEOUT_NEXT_APPROACH,
    RunDir,
    atomic_write_json,
    build_input_question,
    parked_question,
    utc_from_epoch,
)
from tests.conftest import supervisor_factory

# 2026-01-01T00:00:00Z: the fake park time.
PARK_EPOCH = 1767225600.0
EXPECTED_PARKED_AT = utc_from_epoch(PARK_EPOCH)
EXPECTED_EXPIRES_AT = utc_from_epoch(PARK_EPOCH + 60 * 60)

_QUESTION_KEYS = {
    "kind", "trigger", "judge", "findings", "cureRoundsUsed",
    "cureRoundsAllowed", "approach", "maxApproaches", "parkedAt",
    "expiresAt", "onTimeout",
}


def _bounded(coro, timeout: float = 30.0):
    return asyncio.run(asyncio.wait_for(coro, timeout))


def _result(text: str) -> IterationResult:
    r = IterationResult(exit_code=0)
    r.final_text = text
    r.duration_s = 5.0
    r.usage = {"input": 10, "output": 5, "totalTokens": 15}
    return r


def _finding(curable: bool, clause: str, evidence: str) -> dict:
    payload = {"severity": "blocking", "curable": curable, "clause": clause,
               "evidence": evidence}
    if curable:
        payload["cured_when"] = f"{clause} is discharged"
    return payload


def _events(root: Path, type_: str) -> list[dict]:
    log = root / "events.jsonl"
    lines = log.read_text().splitlines() if log.exists() else []
    return [ev for ev in (json.loads(x) for x in lines)
            if ev.get("type") == type_]


_review_supervisor = supervisor_factory(
    run_id="awaiting-input-question", cfg_defaults=False,
    prd="# awaiting input fixture PRD\n\nDo the thing.\n",
    status={"state": "running", "phase": "review", "approach": 1})


def _fix_clock(sup: LoopSupervisor) -> None:
    sup._now_epoch = lambda: PARK_EPOCH  # type: ignore[method-assign]


def _review_replies(sup: LoopSupervisor, payload: dict) -> None:
    async def reply(prompt, transcript, **kw):
        return _result(json.dumps(payload))
    sup.runner.run = reply  # type: ignore[method-assign]


def _gate_supervisor(tmp_path: Path, gate_findings: list[dict]
                     ) -> LoopSupervisor:
    root = tmp_path / "run"
    root.mkdir(parents=True, exist_ok=True)
    run = RunDir(root=root)
    run.update_status(state="running")
    run.prd_file.write_text("# awaiting input gate fixture PRD\n")
    atomic_write_json(run.tasks_file, {"version": 1, "tasks": []})
    cfg = JobConfig(run_id="awaiting-input-gate", iterations=5,
                    max_approaches=3, vigilant=False, peer_verdict_gate=True,
                    cure_pass_ceiling=0,
                    infra_retry_backoff_s=[0.0], infra_retry_backoff_max_s=0.0)
    sup = LoopSupervisor(cfg, run, root)
    sup._approach = 1
    findings.write_findings_json(run.verdict_gate_findings_json_file,
                                 gate_findings)
    _fix_clock(sup)
    return sup


def _assert_question(sup: LoopSupervisor, *, judge: str, trigger: str,
                     approach: int, max_approaches: int, used: int,
                     allowed: int, on_timeout: str, blocking: list[dict]
                     ) -> dict:
    status = sup.run.read_status()
    assert status["state"] == AWAITING_INPUT_STATE == "awaiting_input"
    assert status["state"] != "paused"
    question = status["inputQuestion"]
    assert set(question) == _QUESTION_KEYS
    assert question["kind"] == "findings"
    assert question["trigger"] == trigger
    assert question["judge"] == judge
    assert question["cureRoundsUsed"] == used
    assert question["cureRoundsAllowed"] == allowed
    assert question["approach"] == approach
    assert question["maxApproaches"] == max_approaches
    assert question["parkedAt"] == EXPECTED_PARKED_AT
    assert question["expiresAt"] == EXPECTED_EXPIRES_AT  # park + 60 min
    assert question["onTimeout"] == on_timeout
    # every finding: stable id, clause, severity, curable, text.
    assert [f["id"] for f in question["findings"]] == [
        f"F{i}" for i in range(1, len(blocking) + 1)]
    for entry, source in zip(question["findings"], blocking):
        assert set(entry) == {"id", "clause", "severity", "curable", "text"}
        assert entry["clause"] == source["clause"]
        assert entry["severity"] == "blocking"
        assert entry["curable"] is source["curable"]
        assert entry["text"] == source["evidence"]
    # the in-process hold is released only by an answer.
    assert sup._pause.is_set() is False
    # input.parked is emitted, carrying the same question.
    parked = _events(sup.run.root, "input.parked")
    assert len(parked) == 1
    for key in _QUESTION_KEYS - {"parkedAt"}:
        assert parked[0][key] == question[key]
    return question


# ---------------------------------------------------------------------------
# review: raise_operator_gate (the reviewer escalates)
# ---------------------------------------------------------------------------

def test_reviewer_escalation_parks_in_awaiting_input(tmp_path):
    blocking = [_finding(True, "requirement A.1 (#224)", "one fixable gap")]
    sup = _review_supervisor(
        tmp_path / "run", max_approaches=3, cure_pass_ceiling=3,
        operator_input_needed_pause=True)
    sup._approach = 1
    _fix_clock(sup)
    findings.write_findings_json(sup.run.findings_json_file, blocking)
    _review_replies(sup, {"verdict": "rejected", "reason": "one gap",
                          "raise_operator_gate": True})

    outcome = _bounded(sup._step_review())

    assert outcome == ("advance", "paused", "operator_input_needed")
    _assert_question(
        sup, judge="review", trigger="reviewer_escalation", approach=1,
        max_approaches=3, used=0, allowed=3,
        on_timeout=INPUT_ON_TIMEOUT_CURE_NOW, blocking=blocking)


# ---------------------------------------------------------------------------
# review: an incurable blocking finding
# ---------------------------------------------------------------------------

def test_incurable_review_rejection_parks_in_awaiting_input(tmp_path):
    blocking = [
        _finding(False, "requirement B (#111)", "an environment fact"),
        _finding(False, "requirement B.2 (#111)", "a second fact"),
    ]
    sup = _review_supervisor(
        tmp_path / "run", max_approaches=3, cure_pass_ceiling=3)
    sup._approach = 1
    _fix_clock(sup)
    findings.write_findings_json(sup.run.findings_json_file, blocking)
    _review_replies(sup, {"verdict": "rejected", "reason": "incurable"})

    outcome = _bounded(sup._step_review())

    # the old in-memory hold: a self-loop back into review, state `running`.
    assert outcome == ("advance", "review", "rejected_incurable")
    _assert_question(
        sup, judge="review", trigger="incurable", approach=1,
        max_approaches=3, used=0, allowed=3,
        on_timeout=INPUT_ON_TIMEOUT_NEXT_APPROACH, blocking=blocking)


def test_incurable_review_rejection_on_the_last_approach_reads_exhausted(
        tmp_path):
    """`rejected_last_approach` (curable findings, the last approach, cure
    rounds spent) also parks in `awaiting_input`; with no approach left the
    timeout outcome is the `approaches` hold."""
    blocking = [_finding(True, "requirement D.1 (#142)", "a curable gap")]
    sup = _review_supervisor(
        tmp_path / "run", max_approaches=1, cure_pass_ceiling=0,
        operator_input_needed_pause=True)
    sup._approach = 1
    _fix_clock(sup)
    findings.write_findings_json(sup.run.findings_json_file, blocking)
    _review_replies(sup, {"verdict": "rejected", "reason": "last try"})

    outcome = _bounded(sup._step_review())

    assert outcome[0] == "advance" and outcome[1] == "paused"
    _assert_question(
        sup, judge="review", trigger="cure_rounds_spent", approach=1,
        max_approaches=1, used=0, allowed=0,
        on_timeout=INPUT_ON_TIMEOUT_EXHAUSTED, blocking=blocking)


# ---------------------------------------------------------------------------
# verdict gate: an incurable gate rejection
# ---------------------------------------------------------------------------

def test_incurable_verdict_gate_rejection_parks_in_awaiting_input(tmp_path):
    blocking = [
        _finding(False, "requirement C.1 (v0.27)", "an environment fact"),
        _finding(True, "requirement C.2 (v0.27)", "a fixable gap"),
    ]
    sup = _gate_supervisor(tmp_path, blocking)

    async def gate_rejects(self, phase, extra="", *a, **kw):
        return _result(json.dumps({"verdict": "rejected",
                                   "reason": "the gate found a wall"}))
    orig = LoopSupervisor.run_iteration
    LoopSupervisor.run_iteration = gate_rejects
    try:
        outcome = _bounded(sup._step_verdict_gate())
    finally:
        LoopSupervisor.run_iteration = orig

    assert outcome == ("advance", "paused",
                       "verdict_gate_operator_input_needed")
    _assert_question(
        sup, judge="verdict_gate", trigger="incurable", approach=1,
        max_approaches=3, used=0, allowed=0,
        on_timeout=INPUT_ON_TIMEOUT_NEXT_APPROACH, blocking=blocking)


# ---------------------------------------------------------------------------
# the question builder, and what leaves the state
# ---------------------------------------------------------------------------

def test_the_timeout_setting_zero_means_no_expiry_and_a_secret_never_expires():
    blocking = [_finding(False, "clause", "evidence")]
    common = dict(judge="review", trigger="incurable", blocking=blocking,
                  cure_rounds_used=0, cure_rounds_allowed=3, approach=1,
                  max_approaches=2, parked_at_epoch=PARK_EPOCH)
    assert build_input_question(timeout_min=0, **common)["expiresAt"] is None
    assert build_input_question(
        timeout_min=30, **common)["expiresAt"] == utc_from_epoch(
            PARK_EPOCH + 30 * 60)
    secret = build_input_question(timeout_min=60, kind="secret", **common)
    assert secret["expiresAt"] is None


def test_an_answer_leaves_awaiting_input_and_clears_the_question(tmp_path):
    blocking = [_finding(False, "requirement B (#111)", "an environment fact")]
    sup = _review_supervisor(
        tmp_path / "run", max_approaches=3, cure_pass_ceiling=3)
    sup._approach = 1
    _fix_clock(sup)
    findings.write_findings_json(sup.run.findings_json_file, blocking)
    _review_replies(sup, {"verdict": "rejected", "reason": "incurable"})
    _bounded(sup._step_review())
    assert sup.run.read_status()["state"] == "awaiting_input"

    sup.resume()

    status = sup.run.read_status()
    assert status["state"] == "running"
    assert status.get("inputQuestion") is None
    assert sup._pause.is_set() is True


def test_parked_question_is_only_for_awaiting_input_runs(tmp_path):
    """The hub's parked-question payload reads `inputQuestion` and exists for
    every `awaiting_input` run and for no other state."""
    blocking = [_finding(False, "requirement B (#111)", "an environment fact")]
    sup = _review_supervisor(
        tmp_path / "run", max_approaches=3, cure_pass_ceiling=3)
    sup._approach = 1
    _fix_clock(sup)
    findings.write_findings_json(sup.run.findings_json_file, blocking)
    _review_replies(sup, {"verdict": "rejected", "reason": "incurable"})
    _bounded(sup._step_review())
    status = sup.run.read_status()

    payload = parked_question(sup.run.root, status)
    assert payload is not None
    assert payload["inputQuestion"] == status["inputQuestion"]

    for other in ("running", "paused", "exhausted", "finishing", "failed"):
        assert parked_question(sup.run.root, {**status, "state": other}) is None
