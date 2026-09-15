"""Task 009 (requirement C.1, #114): validation exhaustion routes to a new
`petitioned` status -- not `failed` -- whenever the run's CONTRACT carries a
petition protocol (`petitions.contract_declares_petition_protocol` over the
run's own PRD/composite-PRD text, the same default-safe by-presence
detection requirement A.3 (#110) already uses for the materiality rubric:
read what is actually present in the contract, default to today's behaviour
when nothing is). Task 011 (requirement C.3) corrected that discriminant --
it shipped here as "the workspace carries some `<wave>-petitions.md` file",
which also fired for a run whose contract never mentioned petitions but
whose repo still carried an earlier wave's file -- so every fixture below
that expects the `petitioned` route now declares the protocol in its
contract (`_seed_petitions_file`, which writes both the contract and the
filing target). `_verify_task`'s last-attempt handoff -- the
text it would otherwise only ever write into `validationNotes` -- becomes
the filed petition's own evidence/body, via the engine's new
`petitions.file_exhaustion_petition` (a second writer beside the worker's
own typed declaration, task 004's `file_worker_petition`).

Three things are pinned, each its own test:

1. the routing itself: three failed verify attempts against a workspace
   that already carries a petitions file end at `petitioned`, never
   `failed`, and `failureKind` is never set (that label means something
   specific about `failed`, task 024/#33, and must not leak onto a
   different status);
2. the handoff text -- the verifier's own rejection `reason`, the same
   text `validationNotes` carries -- appears as the filed petition's body,
   readable back through `petitions.parse_petitions`;
3. `tierLadderExhausted` bookkeeping (task 014/#61) is unaffected by which
   branch exhaustion takes -- a task already at `tiers.TOP_TIER` gets the
   field and the `tierLadderExhausted` signal exactly as it always did.

A negative control (no petitions file in the workspace at all) closes the
loop: the SAME three-attempt sequence still lands `failed` with
`failureKind` set, exactly as before this task -- the fuller backward-
compatibility pin is task 011's own module, this is just the sanity check
that the new branch is not taken unconditionally.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from ralphd.engine import petitions, tiers
from ralphd.engine.config import JobConfig
from ralphd.engine.loop import LoopSupervisor
from ralphd.engine.runner import IterationResult
from ralphd.engine.state import (
    TASK_EFFECTIVE_TIER_FIELD,
    TASK_FAILURE_VALIDATION_EXHAUSTED,
    TASK_TERMINAL_STATUSES,
    VALIDATION_ATTEMPT_LIMIT,
    RunDir,
    atomic_write_json,
    read_events,
)
from tests.conftest import supervisor_with_runner

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


_supervisor = supervisor_with_runner(
    lambda sup: _FixedRunner(lambda: _rejected(HANDOFF_TEXT)),
    run_id="task009-petitioned", iterations=9, max_approaches=1, vigilant=True,
    on_complete="exit", infra_retry_backoff_s=[0.0], infra_retry_backoff_max_s=0.0,
    infra_outage_budget_s=0.0)


def _write_plan(sup: LoopSupervisor, tasks: list[dict]) -> None:
    atomic_write_json(sup.run.tasks_file, {"version": 1, "tasks": tasks})


# The contract text that opts a run into the petition protocol -- the ONE
# discriminant between the two exhaustion routes since task 011 (requirement
# C.3, #114). Its own module pins both directions of the detection.
PROTOCOL_CONTRACT = """# PRD v0-test

## Petition protocol

A task whose criteria cannot be satisfied as written files a petition per
`docs/petition-format.md`; an exhausted task routes to `petitioned`.
"""


def _seed_petitions_file(root: Path, name: str = "test-petitions.md") -> Path:
    """Declare the petition protocol in the run's contract (what actually
    selects the `petitioned` route) AND create the wave file a filing lands
    in. The file alone no longer decides the branch -- see task 011's
    `tests/test_petitioned_backcompat.py`."""
    (root / "prd.md").write_text(PROTOCOL_CONTRACT)
    reports = root / "artifacts" / "reports"
    reports.mkdir(parents=True, exist_ok=True)
    path = reports / name
    path.write_text("# Test wave petitions\n\nNo petition filed yet.\n")
    return path


def _exhaust(sup: LoopSupervisor) -> dict:
    """Drive `_verify_task` VALIDATION_ATTEMPT_LIMIT times and return the
    task record after the last one."""
    for _ in range(VALIDATION_ATTEMPT_LIMIT):
        task = sup.run.read_tasks()["tasks"][0]
        assert asyncio.run(sup._verify_task(task)) is False
    return sup.run.read_tasks()["tasks"][0]


# ---------------------------------------------------------------------------
# 1. the routing itself
# ---------------------------------------------------------------------------


def test_exhaustion_routes_to_petitioned_when_the_contract_declares_one(
        tmp_path):
    root = tmp_path / "run"
    sup = _supervisor(root)
    _seed_petitions_file(root)
    _write_plan(sup, [_task()])

    final = _exhaust(sup)

    assert final["status"] == "petitioned"
    assert "failureKind" not in final
    assert final["validationAttempts"] == VALIDATION_ATTEMPT_LIMIT
    # `petitioned` is threaded through the terminal-status vocabulary --
    # nothing further moves this task without an adjudication.
    assert "petitioned" in TASK_TERMINAL_STATUSES


def test_an_exhausted_task_already_petitioned_is_never_reverified(tmp_path):
    root = tmp_path / "run"
    sup = _supervisor(root)
    _seed_petitions_file(root)
    _write_plan(sup, [_task()])
    _exhaust(sup)

    spent = sup.runner.calls  # type: ignore[attr-defined]
    petitioned = sup.run.read_tasks()["tasks"][0]
    assert asyncio.run(sup._verify_task(petitioned)) is False
    assert sup.runner.calls == spent, \
        "an exhausted task must not consume another verify iteration"
    assert sup.run.read_tasks()["tasks"][0] == petitioned


# ---------------------------------------------------------------------------
# 2. the handoff text becomes the filed petition's body
# ---------------------------------------------------------------------------


def test_the_last_attempt_handoff_becomes_the_filed_petitions_body(tmp_path):
    """Task 065 (requirement N.6, #150): the filing now lands in the RUN
    dir's own `petitions.md` (`sup.run.petitions_file`), never in the
    workspace's tracked wave file -- `petitions_path` below stays exactly
    at its seeded, empty-of-filings text.
    """
    root = tmp_path / "run"
    sup = _supervisor(root)
    petitions_path = _seed_petitions_file(root)
    before = petitions_path.read_text()
    _write_plan(sup, [_task()])

    final = _exhaust(sup)
    assert final["validationNotes"] == HANDOFF_TEXT

    # the workspace's own tracked file never moved.
    assert petitions_path.read_text() == before

    text = sup.run.petitions_file.read_text()
    filed = petitions.parse_petitions(text)
    assert len(filed) == 1
    assert filed[0]["evidence"] == HANDOFF_TEXT
    assert filed[0]["adjudication"] is None          # open, not yet ruled on
    assert TASK_ID in filed[0]["obligation"]


def test_a_duplicate_obligation_refuses_the_filing_and_falls_back_to_failed(
        tmp_path):
    """`file_exhaustion_petition` refuses -- never re-files -- an obligation
    already on record (the same one-petition-per-obligation rule
    `file_worker_petition` enforces, task 004). Exhaustion must still land
    the task SOMEWHERE: the safe fallback is the ordinary `failed` route,
    with `failureKind` set exactly as if no protocol existed at all."""
    root = tmp_path / "run"
    sup = _supervisor(root)
    petitions_path = _seed_petitions_file(root)
    task = _task()
    obligation = f"Task {TASK_ID}: {task['title']}"
    petitions_path.write_text(
        petitions.format_petition_section(
            obligation, "self-referential", "filed some other way first"))
    _write_plan(sup, [task])

    final = _exhaust(sup)

    assert final["status"] == "failed"
    assert final["failureKind"] == TASK_FAILURE_VALIDATION_EXHAUSTED
    # the pre-existing filing is untouched -- still exactly one petition.
    assert len(petitions.parse_petitions(petitions_path.read_text())) == 1


# ---------------------------------------------------------------------------
# 3. tierLadderExhausted bookkeeping is unchanged by which branch fires
# ---------------------------------------------------------------------------


def test_tier_ladder_exhausted_bookkeeping_is_unchanged_when_petitioned(tmp_path):
    root = tmp_path / "run"
    sup = _supervisor(root)
    _seed_petitions_file(root)
    _write_plan(sup, [_task(**{TASK_EFFECTIVE_TIER_FIELD: tiers.TOP_TIER})])

    final = _exhaust(sup)

    assert final["status"] == "petitioned"
    assert final.get("tierLadderExhausted") is True
    events = read_events(sup.run.root)
    signals = [e for e in events
              if e.get("type") == "signal" and e.get("signal") == "tierLadderExhausted"]
    assert signals and signals[0].get("taskId") == TASK_ID


def test_tier_ladder_exhausted_bookkeeping_is_unchanged_when_failed(tmp_path):
    """Same bookkeeping, no petition protocol in the contract at all -- the
    pre-existing `failed` branch, unaffected by this task's addition."""
    root = tmp_path / "run"
    sup = _supervisor(root)
    _write_plan(sup, [_task(**{TASK_EFFECTIVE_TIER_FIELD: tiers.TOP_TIER})])

    final = _exhaust(sup)

    assert final["status"] == "failed"
    assert final.get("tierLadderExhausted") is True
    events = read_events(sup.run.root)
    signals = [e for e in events
              if e.get("type") == "signal" and e.get("signal") == "tierLadderExhausted"]
    assert signals and signals[0].get("taskId") == TASK_ID


# ---------------------------------------------------------------------------
# negative control: no petition protocol in the contract at all
# ---------------------------------------------------------------------------


def test_no_petition_protocol_in_the_contract_keeps_the_failed_route(tmp_path):
    """No contract text declaring a protocol (and, here, no petitions file
    either): the same three-attempt sequence lands `failed` with
    `failureKind` set, and nothing is ever filed. Task 011's own module
    pins the sharper case -- a petitions file present while the contract
    declares nothing."""
    root = tmp_path / "run"
    sup = _supervisor(root)
    _write_plan(sup, [_task()])

    final = _exhaust(sup)

    assert final["status"] == "failed"
    assert final["failureKind"] == TASK_FAILURE_VALIDATION_EXHAUSTED
    assert not petitions.discover_petition_files(sup.workspace)
