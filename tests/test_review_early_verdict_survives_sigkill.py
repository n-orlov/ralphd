"""Task 025 (PRD requirement H.1/H.4, #264): review's early-verdict
checkpoint survives a REAL SIGKILL of the review iteration that wrote it --
the black-box half of this task's success criteria, over a real
`ralphd-engine` subprocess and the stub `pi` binary, mirroring
`tests/test_crashed_attempt_preserved.py`'s and
`tests/flows/test_flow_crash_resume_refunds.py`'s own held-then-SIGKILLed
shape.

The hold (task 007/008, B.1/B.2, `STUB_HOLD_PHASE=review`) blocks the review
invocation deterministically -- but AFTER, not before, the stub's own
`STUB_REVIEW_TOOL_CALLS` drives a real `record-verdict` call through the
REAL hat-side client (`python -m ralphd.engine.hat_tools record-verdict`,
its own subprocess, crossing the actual request/response spool the engine's
review broker serves from the other side) -- see `tests/stub-pi/pi`'s own
Reviewer block and its comment on why Reviewer is excluded from the shared
top-of-file `_apply_hold` call, the same way Worker already is. So the
SIGKILL below always lands strictly AFTER that write has landed, never
racing it.
"""
from __future__ import annotations

import json
import os
import signal
import time
from pathlib import Path

from ralphd.engine.state import REVIEW_EARLY_VERDICT_FILE
from tests.flows.flowlib import wait_engine_exit

__all__ = ["engine_factory"]


def _wait_for(predicate, timeout=30, interval=0.02):
    deadline = time.time() + timeout
    while time.time() < deadline:
        result = predicate()
        if result:
            return result
        time.sleep(interval)
    raise TimeoutError("condition never became true")


def test_the_early_verdict_survives_a_sigkill_of_the_review_iteration(
        engine_factory, tmp_path):
    hold_file = tmp_path / "hold-review-early-verdict"
    review_calls = json.dumps([
        {"tool": "record-verdict",
         "args": {"verdict": "verified",
                  "reason": "all requirements checked, cheapest-first",
                  "findingsPath": "review-findings.md"}},
    ])
    e1 = engine_factory(
        job={"on_complete": "idle", "iterations": 15, "max_approaches": 1},
        stub_env={"STUB_TASKS": "1", "STUB_SLEEP": "0",
                  "STUB_REVIEW_TOOL_CALLS": review_calls,
                  "STUB_HOLD_FILE": str(hold_file),
                  "STUB_HOLD_PHASE": "review",
                  "STUB_HOLD_TIMEOUT_S": "60"},
    )
    e1.wait_api()

    checkpoint_path = e1.run_dir / REVIEW_EARLY_VERDICT_FILE
    _wait_for(lambda: checkpoint_path.exists() and
              json.loads(checkpoint_path.read_text()).get("verdict"),
              timeout=30)

    def review_started_not_finished():
        itdir = e1.run_dir / "iterations"
        if not itdir.is_dir():
            return None
        for d in sorted(itdir.iterdir()):
            p = d / "meta.json"
            if not p.exists():
                continue
            try:
                meta = json.loads(p.read_text())
            except json.JSONDecodeError:
                continue
            if meta.get("phase") == "review" and "endedAt" not in meta:
                return meta
        return None

    # the review iteration this checkpoint belongs to is genuinely still
    # in flight (held, not finished) -- the SIGKILL below lands strictly
    # inside it, never after it has already recorded its own contract-line
    # verdict.
    review_meta = _wait_for(review_started_not_finished, timeout=30)
    assert review_meta.get("phase") == "review"

    # the checkpoint's own bounded, fixed key set, written before the kill.
    before = json.loads(checkpoint_path.read_text())
    assert before["verdict"] == "verified"
    assert before["reason"] == "all requirements checked, cheapest-first"
    assert before["findingsPath"] == "review-findings.md"
    assert before["approach"] == 1
    assert set(before) == {"approach", "verdict", "reason", "findingsPath",
                           "recordedAt"}

    pid = e1.proc.pid
    os.kill(pid, signal.SIGKILL)
    wait_engine_exit(e1, timeout=10)
    assert e1.proc.returncode is not None and e1.proc.returncode != 0

    crashed_meta = json.loads(
        (e1.run_dir / "iterations" / f"{review_meta['number']:04d}"
         / "meta.json").read_text())
    assert "endedAt" not in crashed_meta, (
        "the held review iteration must genuinely never finish -- the "
        "SIGKILL below must land mid-iteration, not after it settled")

    # -- the checkpoint, read back AFTER the process is confirmed dead --
    # usable: parses, and says exactly what it said before the kill.
    after = json.loads(checkpoint_path.read_text())
    assert after == before, (
        "the early-verdict checkpoint must be unchanged and fully readable "
        "immediately after a SIGKILL landing right after it was written")

    # Resume: a fresh engine over the SAME run dir. This is not itself part
    # of this task's own success criteria (task 026/H.3 is what makes a
    # provisional verdict legible on resume) -- included only to prove the
    # crash did not corrupt the run or wedge it.
    e2 = engine_factory(
        job={"on_complete": "exit", "iterations": 15, "max_approaches": 1},
        stub_env={"STUB_TASKS": "1", "STUB_SLEEP": "0"},
    )
    assert e2.run_dir == e1.run_dir
    assert wait_engine_exit(e2, timeout=60) == 0
    status = json.loads((e2.run_dir / "status.json").read_text())
    assert status.get("state") == "succeeded"


def test_a_refused_review_tool_call_never_lands_a_checkpoint(
        engine_factory, tmp_path):
    """The stub's response-collection round trip itself, proving the
    request genuinely crossed the spool and the engine's own refusal
    (illegal `verdict` value) came back -- never a checkpoint file for an
    invocation that was refused."""
    review_calls = json.dumps([
        {"tool": "record-verdict", "args": {"verdict": "not-a-real-verdict"}},
    ])
    e1 = engine_factory(
        job={"on_complete": "exit", "iterations": 15, "max_approaches": 1},
        stub_env={"STUB_TASKS": "1", "STUB_SLEEP": "0",
                  "STUB_REVIEW_TOOL_CALLS": review_calls},
    )
    assert wait_engine_exit(e1, timeout=60) == 0
    responses_path = e1.run_dir / ".stub-review-tool-responses.json"
    responses = json.loads(_wait_for(lambda: responses_path.exists() and
                                     responses_path.read_text(), timeout=30))
    assert len(responses) == 1
    assert responses[0]["ok"] is False
    checkpoint_path = e1.run_dir / REVIEW_EARLY_VERDICT_FILE
    assert not checkpoint_path.exists()
