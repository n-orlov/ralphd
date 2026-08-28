"""v0.13 task 012 (#92, requirement G's second half): black-box CLI proof
that `ralphctl status`' `reason:` line and `ralphctl iteration N` both say a
hat broke its contract and quote the validation error, for a run-dir
fixture whose `events.jsonl` holds `contract.violation` (a hat that
recovered) and one that also holds `contract.agent_fault` (a hat that never
did) -- through the ONE shared derivation
(`contract_enforcement.violation_message`), never two independently
hand-kept strings.

Two hand-written run-dir fixtures, no container, no live API (the CLI's own
`status.json`/`events.jsonl`/`meta.json` fallback path, same tier
`tests/test_cli_fault_explanation.py`/`tests/test_cli_iteration_detail.py`
already exercise):

  * `recovered`: iteration 1's `events.jsonl` carries exactly ONE
    `contract.violation` (no `contract.agent_fault` -- the re-ask fixed
    it), the run itself is still `running`. `ralphctl iteration <run> 1`
    prints a `contract:` line quoting the validation error;
    `ralphctl status` prints no `reason:` line at all (nothing ended the
    run -- `status.json` genuinely carries none), which is asserted
    explicitly rather than left unchecked, so a future change that starts
    inventing a reason for a run that is still running is caught.
  * `exhausted`: iteration 1's `events.jsonl` carries a full ladder of
    `contract.violation` events plus one `contract.agent_fault`, and
    `status.json` records the run as `aborted` with `reason` set to
    EXACTLY `contract_enforcement.violation_message(...)`'s own output for
    the same hat/errors (never a hand-typed literal in this test) --
    exactly what the engine's generic hat step now writes there
    (`tests/test_contract_violation_task012.py` pins that engine-side
    half). `ralphctl status`'s `reason:` line and `ralphctl iteration
    <run> 1`'s `contract:` line are asserted to carry the SAME error text,
    proving the two surfaces did not each keep their own copy of the
    wording.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from ralphd.engine.contract_enforcement import (
    EVENT_AGENT_FAULT,
    EVENT_VIOLATION,
    violation_message,
)
from tests.conftest import RALPHCTL

HAT = "worker"
ERRORS = ["$.task_id: expected string, got int"]
MAX_ATTEMPTS = 3


def _ctl(registry: Path, *argv: str, timeout: int = 60):
    env = {**os.environ, "RALPHD_REGISTRY": str(registry)}
    return subprocess.run([str(RALPHCTL), *argv], env=env,
                          capture_output=True, text=True, timeout=timeout)


def _write_iteration(run_dir: Path, n: int, meta: dict) -> None:
    d = run_dir / "iterations" / f"{n:04d}"
    d.mkdir(parents=True)
    (d / "meta.json").write_text(json.dumps(meta))


def _base_meta(n: int = 1, **over) -> dict:
    meta = {"number": n, "phase": "worker", "approach": 1,
            "startedAt": "2026-09-10T10:00:00Z", "endedAt": "2026-09-10T10:00:31Z",
            "exitCode": 0, "interrupted": False, "timedOut": False,
            "noTrafficTimeout": False, "sawComplete": True, "sawVerified": False,
            "error": None, "faultClass": None, "usage": {"input": 5, "output": 5,
                                                          "totalTokens": 10}}
    meta.update(over)
    return meta


def _violation_event(eid: int, attempt: int) -> dict:
    return {"id": eid, "type": EVENT_VIOLATION, "hat": HAT, "attempt": attempt,
            "maxAttempts": MAX_ATTEMPTS, "errors": list(ERRORS)}


def _fault_event(eid: int) -> dict:
    return {"id": eid, "type": EVENT_AGENT_FAULT, "hat": HAT,
            "attempts": MAX_ATTEMPTS, "errors": list(ERRORS)}


def _write_events(run_dir: Path, events: list[dict]) -> None:
    (run_dir / "events.jsonl").write_text(
        "".join(json.dumps(ev) + "\n" for ev in events))


def _recovered_run(tmp_path: Path, run_id: str = "recovered") -> tuple[Path, Path]:
    """A run still going: iteration 1's ladder recovered on the re-ask, so
    there is exactly one `contract.violation` and no `contract.agent_fault`,
    and the run itself never ended -- `status.json` carries no `reason` at
    all."""
    registry = tmp_path / "registry"
    run_dir = registry / "runs" / run_id
    run_dir.mkdir(parents=True)
    (run_dir / "status.json").write_text(json.dumps(
        {"runId": run_id, "state": "running", "iterationsUsed": 1}))
    _write_iteration(run_dir, 1, _base_meta())
    _write_events(run_dir, [
        {"id": 1, "type": "iteration.start", "number": 1, "phase": "worker"},
        {"id": 2, "type": "iteration.end", "number": 1, "phase": "worker",
         "exitCode": 0},
        _violation_event(3, attempt=1),
    ])
    return registry, run_dir


def _exhausted_run(tmp_path: Path, run_id: str = "exhausted") -> tuple[Path, Path]:
    """A run the ladder never recovered for: every attempt at iteration 1
    repeated the same violation, `contract.agent_fault` was emitted, and --
    since this happened with no budget left -- the run ended `aborted`
    with `reason` set to exactly what the engine now writes there
    (`contract_enforcement.violation_message`, computed here, never a
    literal re-typed by hand)."""
    registry = tmp_path / "registry"
    run_dir = registry / "runs" / run_id
    run_dir.mkdir(parents=True)
    reason = violation_message(HAT, ERRORS)
    (run_dir / "status.json").write_text(json.dumps(
        {"runId": run_id, "state": "aborted", "verdict": "unverified",
         "reason": reason, "iterationsUsed": 1}))
    _write_iteration(run_dir, 1, _base_meta(sawComplete=False))
    events = [
        {"id": 1, "type": "iteration.start", "number": 1, "phase": "worker"},
        {"id": 2, "type": "iteration.end", "number": 1, "phase": "worker",
         "exitCode": 0},
    ]
    eid = 3
    for attempt in range(1, MAX_ATTEMPTS + 1):
        events.append(_violation_event(eid, attempt))
        eid += 1
    events.append(_fault_event(eid))
    _write_events(run_dir, events)
    return registry, run_dir


# --------------------------------------------------------------- recovered

def test_iteration_n_names_the_contract_violation_when_it_recovered(tmp_path):
    registry, _ = _recovered_run(tmp_path)
    res = _ctl(registry, "iteration", "recovered", "1", "--no-log")
    assert res.returncode == 0, res.stderr
    expected = violation_message(HAT, ERRORS)
    assert f"contract:  {expected}" in res.stdout
    assert "expected string, got int" in res.stdout


def test_iteration_n_json_carries_the_contract_fault_recovered(tmp_path):
    registry, _ = _recovered_run(tmp_path)
    res = _ctl(registry, "--json", "iteration", "recovered", "1", "--no-log")
    assert res.returncode == 0, res.stderr
    doc = json.loads(res.stdout)
    cf = doc["contractFault"]
    assert cf["hat"] == HAT
    assert cf["errors"] == ERRORS
    assert cf["agentFault"] is False
    assert cf["text"] == violation_message(HAT, ERRORS)


def test_status_prints_no_reason_line_for_a_run_that_is_still_going(tmp_path):
    """The recovered case genuinely never ended -- `status.json` carries no
    `reason`, so `ralphctl status` must print none either (never invent
    one from the mere PRESENCE of a `contract.violation` event)."""
    registry, _ = _recovered_run(tmp_path)
    res = _ctl(registry, "status", "recovered")
    assert res.returncode == 0, res.stderr
    assert "reason:" not in res.stdout


# --------------------------------------------------------------- exhausted

def test_status_reason_line_names_the_contract_violation_when_it_did_not_recover(tmp_path):
    registry, _ = _exhausted_run(tmp_path)
    res = _ctl(registry, "status", "exhausted")
    assert res.returncode == 0, res.stderr
    # long text is wrapped across `reason:`/continuation lines (task 003) --
    # normalise before checking the full sentence is there, verbatim.
    rejoined = " ".join(
        line.removeprefix("reason:    ").removeprefix("           ").strip()
        for line in res.stdout.splitlines()
        if line.startswith("reason:") or line.startswith("           "))
    assert violation_message(HAT, ERRORS) in rejoined


def test_iteration_n_names_the_contract_violation_when_it_did_not_recover(tmp_path):
    registry, _ = _exhausted_run(tmp_path)
    res = _ctl(registry, "iteration", "exhausted", "1", "--no-log")
    assert res.returncode == 0, res.stderr
    expected = violation_message(HAT, ERRORS)
    assert f"contract:  {expected}" in res.stdout


def test_iteration_n_json_carries_the_contract_fault_exhausted(tmp_path):
    registry, _ = _exhausted_run(tmp_path)
    res = _ctl(registry, "--json", "iteration", "exhausted", "1", "--no-log")
    assert res.returncode == 0, res.stderr
    doc = json.loads(res.stdout)
    cf = doc["contractFault"]
    assert cf["hat"] == HAT
    assert cf["errors"] == ERRORS
    assert cf["agentFault"] is True
    assert cf["text"] == violation_message(HAT, ERRORS)


def test_status_and_iteration_agree_on_the_exact_same_sentence(tmp_path):
    """The point of the shared derivation: both surfaces, over the SAME
    fixture, quote byte-identical text -- not two independently-worded
    strings that happen to mention the same error today."""
    registry, _ = _exhausted_run(tmp_path)
    status_out = _ctl(registry, "status", "exhausted").stdout
    iteration_out = _ctl(registry, "iteration", "exhausted", "1",
                         "--no-log").stdout
    expected = violation_message(HAT, ERRORS)
    rejoined = " ".join(
        line.removeprefix("reason:    ").removeprefix("           ").strip()
        for line in status_out.splitlines()
        if line.startswith("reason:") or line.startswith("           "))
    assert expected in rejoined
    assert f"contract:  {expected}" in iteration_out
