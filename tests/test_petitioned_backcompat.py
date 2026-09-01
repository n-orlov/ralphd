"""Task 011 (requirement C.3, #114): the default-safe fallback.

Task 009 (requirement C.1) taught `_verify_task` a second branch at
exhaustion: `petitioned` instead of `failed`. This module pins WHAT decides
which branch fires, which is requirement C.3's whole claim -- "a run whose
contract has no petition protocol keeps today's `failed` routing" -- and
therefore that the discriminant is the CONTRACT's own text and nothing else.

Task 009 shipped the discriminant as "does the workspace carry any
`artifacts/reports/*-petitions.md` file", and that is NOT the same
predicate: a repo routinely carries an earlier wave's petitions file (this
repo carries `artifacts/reports/v0.15-petitions.md` and
`v0.16-petitions.md`) while the contract the current run works from says
nothing about petitions at all. Under that rule such a run silently got the
NEW routing -- `petitioned`, a petition filed on its behalf -- which is
exactly the backward-compatibility break C.3 forbids. Task 011 moved the
decision onto the contract text (`petitions.contract_declares_petition_
protocol` over `LoopSupervisor._contract_text()`, the same
`prd_path(run.root) or run.prd_file` file `build_prompt` and the
materiality-rubric context already read) and made the workspace file a mere
filing TARGET, created on demand (`petitions.ensure_petitions_file`) so its
absence cannot be a second, hidden discriminant either.

Every test below drives the REAL production path -- `LoopSupervisor.
_verify_task` with a real `RunDir`, a real plan on disk and a real verifier
result carrying a `rejected` verdict, `VALIDATION_ATTEMPT_LIMIT` times --
never a test-local validation shim, so a test here cannot pass while the
engine does something else in a real run. What varies between the two
branches is one file: `prd.md`.
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

# A contract from BEFORE this wave: a full-looking PRD that never mentions
# petitions in any form. This is the C.3 case -- exhaustion must behave
# exactly as it did before the wave.
NO_PROTOCOL_CONTRACT = """# PRD v0.9 -- a contract from before this wave

## A -- the work

1. Ship the thing.
2. Pin it with tests.

## Non-negotiables

- A non-zero pytest exit at the shipped sha is RED.
- Never weaken a test to force green.
"""

# The same contract, plus an explicit petition protocol. Nothing else about
# the run changes.
PROTOCOL_CONTRACT = NO_PROTOCOL_CONTRACT + """
## Petition protocol

A task whose criteria cannot be satisfied as written files a petition per
`docs/petition-format.md`; an exhausted task is routed to `petitioned` for
adjudication rather than `failed`.
"""

# A contract that merely NAMES the noun in a sentence of history, with no
# protocol anywhere: still the pre-wave route (the by-presence detection is
# deliberately keyed on a declaration, not on the word appearing at all).
INCIDENTAL_MENTION_CONTRACT = NO_PROTOCOL_CONTRACT + """
The previous wave's closing report noted that no petition was ever needed.
"""


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


def _write_contract(sup: LoopSupervisor, text: str) -> None:
    """The run's contract -- the one file that differs between the two
    branches below. `prd.md` in the run dir is what `prd_path`/`_contract_
    text` reads when no composite exists."""
    sup.run.prd_file.write_text(text)


def _seed_historical_petitions_file(root: Path,
                                    name: str = "v0.15-petitions.md") -> Path:
    """An EARLIER wave's petitions file, exactly as this repo's own workspace
    carries. Present in BOTH branches below, so it can never be what
    distinguishes them."""
    reports = root / "artifacts" / "reports"
    reports.mkdir(parents=True, exist_ok=True)
    path = reports / name
    path.write_text("# v0.15 impossibility petitions\n\nNo petition filed.\n")
    return path


def _exhaust(sup: LoopSupervisor) -> dict:
    """Drive `_verify_task` VALIDATION_ATTEMPT_LIMIT times, identically for
    both branches, and return the task record after the last attempt."""
    for _ in range(VALIDATION_ATTEMPT_LIMIT):
        task = sup.run.read_tasks()["tasks"][0]
        assert asyncio.run(sup._verify_task(task)) is False
    return sup.run.read_tasks()["tasks"][0]


def _fixture(root: Path, contract: str, *, historical_file: bool = True
             ) -> LoopSupervisor:
    sup = _supervisor(root)
    if historical_file:
        _seed_historical_petitions_file(root)
    _write_contract(sup, contract)
    _write_plan(sup)
    return sup


# ---------------------------------------------------------------------------
# 1. no petition protocol in the contract -> the pre-wave `failed` route
# ---------------------------------------------------------------------------


def test_no_petition_protocol_keeps_the_pre_wave_failed_route(tmp_path):
    """The contract carries no petition protocol, and the workspace DOES
    carry an earlier wave's petitions file (the realistic case, and the one
    task 009's file-presence discriminant got wrong). Exhaustion lands
    exactly where it always did before this wave: `failed` with
    `failureKind == validation-exhausted`, no `petitioned` status, and no
    petition filed anywhere."""
    root = tmp_path / "run"
    sup = _fixture(root, NO_PROTOCOL_CONTRACT)
    historical = petitions.discover_petition_files(sup.workspace)
    assert historical, "the fixture's own point: a petitions file IS present"
    before = {p: p.read_text() for p in historical}

    final = _exhaust(sup)

    assert final["status"] == "failed"
    assert final["status"] != "petitioned"
    assert final["failureKind"] == TASK_FAILURE_VALIDATION_EXHAUSTED
    # no petition was filed: no file grew, and no new file appeared.
    assert petitions.discover_petition_files(sup.workspace) == historical
    assert {p: p.read_text() for p in historical} == before
    for path in historical:
        assert petitions.parse_petitions(path.read_text()) == []


def test_a_contract_that_merely_mentions_the_word_is_not_a_protocol(tmp_path):
    """Detection is by DECLARATION, not by the noun appearing somewhere: a
    contract whose only mention of petitions is a sentence of history keeps
    the pre-wave route. Same default-safe direction as requirement A.3's
    materiality rubric (a heading, not a keyword)."""
    root = tmp_path / "run"
    sup = _fixture(root, INCIDENTAL_MENTION_CONTRACT)

    assert not petitions.contract_declares_petition_protocol(
        INCIDENTAL_MENTION_CONTRACT)

    final = _exhaust(sup)

    assert final["status"] == "failed"
    assert final["failureKind"] == TASK_FAILURE_VALIDATION_EXHAUSTED


def test_no_contract_at_all_keeps_the_pre_wave_failed_route(tmp_path):
    """A run dir with no PRD file: absence reads as "the contract says
    nothing", so the fallback is the old route, never the new one."""
    root = tmp_path / "run"
    sup = _supervisor(root)
    _seed_historical_petitions_file(root)
    _write_plan(sup)
    assert not sup.run.prd_file.exists()

    final = _exhaust(sup)

    assert final["status"] == "failed"
    assert final["failureKind"] == TASK_FAILURE_VALIDATION_EXHAUSTED


# ---------------------------------------------------------------------------
# 2. a petition-protocol contract -> `petitioned`
# ---------------------------------------------------------------------------


def test_a_petition_protocol_contract_lands_petitioned(tmp_path):
    """Identical task, plan, runner script, attempt count and workspace file
    inventory -- the ONLY difference from the tests above is the contract's
    own text. That single difference flips the terminal status and files the
    last-attempt handoff as the petition's body."""
    root = tmp_path / "run"
    sup = _fixture(root, PROTOCOL_CONTRACT)
    assert petitions.contract_declares_petition_protocol(PROTOCOL_CONTRACT)

    final = _exhaust(sup)

    assert final["status"] == "petitioned"
    assert final["status"] != "failed"
    assert "failureKind" not in final

    filed = [p for path in petitions.discover_petition_files(sup.workspace)
             for p in petitions.parse_petitions(path.read_text())]
    assert len(filed) == 1
    assert filed[0]["evidence"] == HANDOFF_TEXT
    assert TASK_ID in filed[0]["obligation"]


def test_a_protocol_contract_with_no_petitions_file_still_lands_petitioned(
        tmp_path):
    """The workspace's file inventory is not a second discriminant: with a
    protocol-declaring contract and NO petitions file at all, the engine
    creates the wave file itself and still routes `petitioned`."""
    root = tmp_path / "run"
    sup = _fixture(root, PROTOCOL_CONTRACT, historical_file=False)
    assert not petitions.discover_petition_files(sup.workspace)

    final = _exhaust(sup)

    assert final["status"] == "petitioned"
    assert "failureKind" not in final
    created = petitions.discover_petition_files(sup.workspace)
    assert len(created) == 1
    # the wave name comes from the contract's own version token.
    assert created[0].name == "v0.9-petitions.md"
    filed = petitions.parse_petitions(created[0].read_text())
    assert len(filed) == 1 and filed[0]["evidence"] == HANDOFF_TEXT


# ---------------------------------------------------------------------------
# 3. the two branches, side by side, differing in the contract alone
# ---------------------------------------------------------------------------


def test_the_two_branches_diverge_on_the_contract_and_nothing_else(tmp_path):
    """Run the same fixture twice in one test, so the only variable that ever
    changes between the two invocations is the contract's text -- pinning
    requirement C.3's own claim ("the two branches are distinguished by the
    contract and nothing else") in one place rather than across two
    independent test functions that a future edit could drift apart.

    Note what is held identical: the task record, the plan, the verifier's
    rejection text, the attempt count, AND the workspace's petitions-file
    inventory (both runs carry the same earlier-wave file). The contract
    text is the entire delta."""
    without_root = tmp_path / "without"
    with_root = tmp_path / "with"

    sup_without = _fixture(without_root, NO_PROTOCOL_CONTRACT)
    sup_with = _fixture(with_root, PROTOCOL_CONTRACT)

    # the only difference between the two fixtures on disk:
    assert sup_without.run.prd_file.read_text() != \
        sup_with.run.prd_file.read_text()
    assert [p.name for p in petitions.discover_petition_files(
                sup_without.workspace)] == \
        [p.name for p in petitions.discover_petition_files(sup_with.workspace)]

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


# ---------------------------------------------------------------------------
# 4. the detector itself, both directions
# ---------------------------------------------------------------------------


def test_contract_protocol_detection_both_directions():
    declares = petitions.contract_declares_petition_protocol
    # must-detect: the shapes an opt-in actually takes.
    assert declares("## Petition protocol\n\nFile one per obligation.\n")
    assert declares("See `docs/petition-format.md` for the field shape.\n")
    assert declares("An exhausted task routes to `petitioned`.\n")
    assert declares("## C -- exhaustion is a petition, not a failure\n")
    assert declares("- Petitions: one per obligation, adjudicated by review\n")
    assert declares("A worker may file a petition instead of grinding.\n")
    # must-not-detect: silence, and history that merely names the noun.
    assert not declares("")
    assert not declares(NO_PROTOCOL_CONTRACT)
    assert not declares(INCIDENTAL_MENTION_CONTRACT)
    assert not declares("## A -- the work\n\n1. Ship the thing.\n")
