"""Task 011 (requirement C.3, #114): the default-safe fallback.

Task 009 (requirement C.1) taught `_verify_task` a second branch at
exhaustion: `petitioned` instead of `failed`, taken only when the run's own
workspace carries a petition protocol (at least one `<wave>-petitions.md`
file under `artifacts/reports/`, `petitions.discover_petition_files`). That
module already pins the routing in one direction each, in separate tests.

This module pins the SAME thing a different way: it drives the identical
task, plan, runner script and validation-attempt sequence through
`LoopSupervisor._verify_task` TWICE, changing only whether the workspace's
contract carries a petition protocol, and asserts the two runs diverge
exactly where requirement C.3 says they must -- and nowhere else:

* no petition protocol in the workspace -> `failed`,
  `failureKind == TASK_FAILURE_VALIDATION_EXHAUSTED`, no `petitioned`
  status anywhere in the vocabulary is reached, and no petition is filed
  (`petitions.discover_petition_files` stays empty, no new petitions file
  is created);
* the SAME workspace plus a petition-protocol contract (a
  `<wave>-petitions.md` file already present) -> `petitioned`, no
  `failureKind` key at all, and the last-attempt handoff text is filed as
  a petition, readable back through `petitions.parse_petitions`.

Because every other input -- task id, title, criteria, the verifier's
rejection text, the number of attempts, `VALIDATION_ATTEMPT_LIMIT` itself --
is held byte-for-byte identical between the two runs, the only thing left
that could explain the different terminal status is the contract's own
petition protocol. That is requirement C.3's whole claim: the two branches
are distinguished by the contract and nothing else.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from ralphd.engine import petitions
from ralphd.engine.config import JobConfig
from ralphd.engine.loop import LoopSupervisor
from ralphd.engine.runner import IterationResult
from ralphd.engine.state import (
    TASK_FAILURE_VALIDATION_EXHAUSTED,
    VALIDATION_ATTEMPT_LIMIT,
    RunDir,
    atomic_write_json,
)

TASK_ID = "001"
HANDOFF_TEXT = ("criterion 2 can only be verified against a state its own "
                "satisfaction would have to create -- it cannot be "
                "satisfied as written")


def _rejected(reason: str) -> IterationResult:
    r = IterationResult(exit_code=0)
    r.final_text = json.dumps(
        {"task_id": TASK_ID, "verdict": "rejected", "reason": reason})
    r.duration_s = 1.0
    r.usage = {"input": 1, "output": 1, "totalTokens": 2}
    return r


class _FixedRunner:
    def __init__(self, make_result):
        self.make_result = make_result
        self.calls = 0
        self.running = False

    def interrupt(self) -> bool:
        return self.running

    async def run(self, prompt, transcript, **kw) -> IterationResult:
        self.calls += 1
        return self.make_result()


def _task(**over) -> dict:
    task = {"id": TASK_ID, "title": "make the impossible thing work",
            "status": "awaiting-validation",
            "successCriteria": "criterion 2 holds"}
    task.update(over)
    return task


def _supervisor(root: Path) -> LoopSupervisor:
    root.mkdir(parents=True, exist_ok=True)
    run = RunDir(root=root)
    sup = LoopSupervisor(
        JobConfig(run_id="task011-backcompat", iterations=9,
                  max_approaches=1, vigilant=True, on_complete="exit",
                  infra_retry_backoff_s=[0.0], infra_retry_backoff_max_s=0.0,
                  infra_outage_budget_s=0.0),
        run, root)
    sup.runner = _FixedRunner(lambda: _rejected(HANDOFF_TEXT))  # type: ignore[assignment]
    return sup


def _write_plan(sup: LoopSupervisor) -> None:
    atomic_write_json(sup.run.tasks_file, {"version": 1, "tasks": [_task()]})


def _exhaust(sup: LoopSupervisor) -> dict:
    """Drive `_verify_task` VALIDATION_ATTEMPT_LIMIT times, identically for
    both branches, and return the task record after the last attempt."""
    for _ in range(VALIDATION_ATTEMPT_LIMIT):
        task = sup.run.read_tasks()["tasks"][0]
        assert asyncio.run(sup._verify_task(task)) is False
    return sup.run.read_tasks()["tasks"][0]


def test_no_petition_protocol_keeps_the_pre_wave_failed_route(tmp_path):
    """The contract carries no petition protocol at all -- no
    `<wave>-petitions.md` anywhere under the workspace's `artifacts/reports/`
    -- so exhaustion lands exactly where it always did, before this wave."""
    root = tmp_path / "run"
    sup = _supervisor(root)
    _write_plan(sup)

    assert not petitions.discover_petition_files(sup.workspace)

    final = _exhaust(sup)

    assert final["status"] == "failed"
    assert final["failureKind"] == TASK_FAILURE_VALIDATION_EXHAUSTED
    assert final["status"] != "petitioned"
    # exhaustion never files anything when there is no protocol to file into.
    assert not petitions.discover_petition_files(sup.workspace)


def test_a_petition_protocol_contract_lands_petitioned_same_everything_else(
        tmp_path):
    """Identical task, plan, runner script and attempt count -- the ONLY
    difference from the test above is that the workspace's contract now
    carries a petition protocol (a `<wave>-petitions.md` file already
    present under `artifacts/reports/`). That single difference is enough
    to flip the terminal status."""
    root = tmp_path / "run"
    sup = _supervisor(root)
    reports = root / "artifacts" / "reports"
    reports.mkdir(parents=True, exist_ok=True)
    petitions_path = reports / "test011-petitions.md"
    petitions_path.write_text("# Test wave petitions\n\nNo petition filed yet.\n")
    _write_plan(sup)

    assert petitions.discover_petition_files(sup.workspace)

    final = _exhaust(sup)

    assert final["status"] == "petitioned"
    assert "failureKind" not in final
    assert final["status"] != "failed"

    filed = petitions.parse_petitions(petitions_path.read_text())
    assert len(filed) == 1
    assert filed[0]["evidence"] == HANDOFF_TEXT
    assert TASK_ID in filed[0]["obligation"]


def test_the_two_branches_diverge_on_the_contract_and_nothing_else(tmp_path):
    """Run the exact same fixture twice in the same test, side by side, so
    the only variable that ever changes between the two invocations is
    whether the workspace's contract carries a petition protocol -- pinning
    requirement C.3's own claim ("the two branches are distinguished by the
    contract and nothing else") in one place rather than across two
    independent test functions that a future edit could drift apart."""
    without_root = tmp_path / "without"
    with_root = tmp_path / "with"

    sup_without = _supervisor(without_root)
    _write_plan(sup_without)

    sup_with = _supervisor(with_root)
    reports = with_root / "artifacts" / "reports"
    reports.mkdir(parents=True, exist_ok=True)
    (reports / "test011-petitions.md").write_text(
        "# Test wave petitions\n\nNo petition filed yet.\n")
    _write_plan(sup_with)

    final_without = _exhaust(sup_without)
    final_with = _exhaust(sup_with)

    # Every other input was identical: same task id/title/criteria, same
    # rejection text, same attempt count, same VALIDATION_ATTEMPT_LIMIT.
    assert final_without["id"] == final_with["id"] == TASK_ID
    assert final_without["title"] == final_with["title"]
    assert final_without["successCriteria"] == final_with["successCriteria"]
    assert final_without["validationAttempts"] == \
        final_with["validationAttempts"] == VALIDATION_ATTEMPT_LIMIT
    assert final_without["validationNotes"] == final_with["validationNotes"] \
        == HANDOFF_TEXT

    # Only the terminal status (and its failureKind companion) diverges.
    assert final_without["status"] == "failed"
    assert final_without["failureKind"] == TASK_FAILURE_VALIDATION_EXHAUSTED
    assert final_with["status"] == "petitioned"
    assert "failureKind" not in final_with
