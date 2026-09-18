"""Feature module for the subject below (task 028, requirement D.7): the
per-task pin modules listed here are folded in whole -- every test body,
assertion and docstring verbatim, only the module they live in changed.
Sources absorbed (each was `tests/<name>`, deleted in the same commit, with
one `docs/test-suite-audit.md` § "Subsumed" row per node):

- `test_failed_task_vocabulary.py` -- Task 024 (#33): `failed` says WHICH kind of failed, and no longer strands a run. Two defects, one vocabulary: * `failed` on a task meant both "a verifier judged this requirement unmet" and "the engine consumed the last of this task's validation rounds", so a task record could not be read without a h
- `test_stagnation_progress_witnesses_task010.py` -- Task 010 (PRD requirement B.1, #153): the no-progress stagnation counter must read more than one witness of progress. Before this task, `_step_worker`'s ordinary fallback compared only `tasks.json` before/after (witness (a)) -- a worker iteration that made real progress by any OTHER measure still st
- `test_task_absorption_handoff_task011.py` -- Task 011 (PRD requirement B.2, #153): "a task that absorbs iterations is judged, not starved." `stagnant` (task 010, requirement B.1) only measures the OPPOSITE failure -- an iteration with none of its four progress witnesses. A task a worker keeps landing real commits against, pass after pass, neve
- `test_worker_dispatch_reentry_task008.py` -- v0.17 task 008 (requirement B.2, #128): a repaired worker verdict is applied by RE-ENTERING the verdict dispatch, at most once per iteration. Task 008's own module, kept SEPARATE from the contract-ladder module's own `test_a_malformed_complete_report_is_repaired_by_one_uncharged_re_ask` (`tests/test
- `test_worker_plan_complete_task015.py` -- Task 015 (PRD requirement O, #100): give the worker contract a plan-complete verdict value and retire the literal `<promise>COMPLETE</promise>` marker. Three things this module pins directly, at the unit level, that `tests/test_promise_marker_last_line.py`'s full-loop scenarios exercise end-to-end b
- `test_worker_retry_rejection_prompt_task015.py` -- Task 015 (requirement F.2, #145): the worker's retry prompt must carry the verifier's own rejection forward, not just the task's unchanged `successCriteria`. A task re-dispatched at `validationAttempts > 0` is a RETRY: the worker's prompt (`LoopSupervisor._worker_assignment_extra`) must gain a "## W
- `test_interrupt_not_substantive_outcome_task004.py` -- Task 004 (requirement A, #72): the audit that follows task 003's fix to `_step_review` through the rest of the engine's hat-driving paths -- `_step_generic_hat`, `_step_planning`'s inner retry (`_one_planning_attempt`), `_step_worker`'s own stagnation bookkeeping, and the post-terminal reflect path 
- `test_failed_to_skipped_dependent_doom_task011.py` -- Task 011 (requirement C.1, #123): drive task 009's `failed -> skipped` escape end to end through the PRODUCTION worker channel -- `LoopSupervisor. _step_worker` reading a real `worker` contract payload of exactly the shape worker.md (task 010) instructs -- with a DEPENDENT task present, in exactly o
- `test_failed_to_skipped_status_task009.py` -- Task 009 (C.1, #123, option a): sanction `failed -> skipped` in the declared task-status table, carried by the pre-existing `task_status` contract channel (`LoopSupervisor._apply_reported_task_status`) -- the harness applies the move, never the worker, and only when the payload carries a `justificat
- `test_settled_vs_done_task013.py` -- Task 013 (PRD requirement I, #98): split `settled` from `done`, and gate terminal success on steering-ORIGIN TASK state rather than a steering FILE. Before this task, `transitions.HAT_SCHEMAS["steer"]`'s required `settled` boolean was validated (schema-checked) and then simply discarded -- `_step_ge
- `test_scheduler_select_next_task.py` -- Task 115 (#60, requirement C/F): unit tests for the pure scheduler `engine/scheduler.select_next_task` -- see that module's docstring for the four-branch algorithm this drives. This module has no side effects and is not yet wired into `loop.py` (task 116's job) or `worker.md`; these tests exercise t
- `test_engine_owns_terminal_record_task008.py` -- Task 008 (requirement B.2, #140): review.md and the review contract context (`LoopSupervisor._terminal_record_review_context`, joined at the `_review_extra_context` seam every other review-prompt source uses) both state that petition/adjudication accounting, steer and ruling history, approach histor

Renamed to resolve cross-source collisions of module-private names (no test
function was renamed):
- `_supervisor` -> `_fv_supervisor` (from `test_failed_task_vocabulary.py`)
- `_run` -> `_fv_run` (from `test_failed_task_vocabulary.py`)
- `_plan` -> `_fv_plan` (from `test_failed_task_vocabulary.py`)
- `_ok` -> `_fv_ok` (from `test_failed_task_vocabulary.py`)
- `_review_verified` -> `_fv_review_verified` (from `test_failed_task_vocabulary.py`)
- `_task` -> `_fv_task` (from `test_failed_task_vocabulary.py`)
- `_run_resumed` -> `_fv_run_resumed` (from `test_failed_task_vocabulary.py`)
- `_ScriptedAgent` -> `_fv_ScriptedAgent` (from `test_failed_task_vocabulary.py`)
- `_events` -> `_fv_events` (from `test_failed_task_vocabulary.py`)
- `_log_messages` -> `_fv_log_messages` (from `test_failed_task_vocabulary.py`)
- `_supervisor` -> `_spw010_supervisor` (from `test_stagnation_progress_witnesses_task010.py`)
- `_run` -> `_spw010_run` (from `test_stagnation_progress_witnesses_task010.py`)
- `_plan` -> `_spw010_plan` (from `test_stagnation_progress_witnesses_task010.py`)
- `_ScriptedAgent` -> `_fv_ScriptedAgent` (from `test_stagnation_progress_witnesses_task010.py`)
- `_events` -> `_fv_events` (from `test_stagnation_progress_witnesses_task010.py`)
- `_log_messages` -> `_fv_log_messages` (from `test_stagnation_progress_witnesses_task010.py`)
- `_ok` -> `_fv_ok` (from `test_stagnation_progress_witnesses_task010.py`)
- `_task` -> `_fv_task` (from `test_stagnation_progress_witnesses_task010.py`)
- `_ok` -> `_fv_ok` (from `test_task_absorption_handoff_task011.py`)
- `_review_verified` -> `_fv_review_verified` (from `test_task_absorption_handoff_task011.py`)
- `_events` -> `_fv_events` (from `test_task_absorption_handoff_task011.py`)
- `_log_messages` -> `_fv_log_messages` (from `test_task_absorption_handoff_task011.py`)
- `_plan` -> `_spw010_plan` (from `test_task_absorption_handoff_task011.py`)
- `_run` -> `_spw010_run` (from `test_task_absorption_handoff_task011.py`)
- `_ok` -> `_wdr008_ok` (from `test_worker_dispatch_reentry_task008.py`)
- `_review_verified` -> `_wdr008_review_verified` (from `test_worker_dispatch_reentry_task008.py`)
- `_task` -> `_wdr008_task` (from `test_worker_dispatch_reentry_task008.py`)
- `_run_resumed` -> `_wdr008_run_resumed` (from `test_worker_dispatch_reentry_task008.py`)
- `TASK_1` -> `wdr008_TASK_1` (from `test_worker_dispatch_reentry_task008.py`)
- `TASK_1` -> `wpc015_TASK_1` (from `test_worker_plan_complete_task015.py`)
- `_ok` -> `_wpc015_ok` (from `test_worker_plan_complete_task015.py`)
- `_task` -> `_wpc015_task` (from `test_worker_plan_complete_task015.py`)
- `_supervisor` -> `_wpc015_supervisor` (from `test_worker_plan_complete_task015.py`)
- `_ScriptedAgent` -> `_wpc015_ScriptedAgent` (from `test_worker_plan_complete_task015.py`)
- `_run_resumed` -> `_wpc015_run_resumed` (from `test_worker_plan_complete_task015.py`)
- `_events` -> `_wpc015_events` (from `test_worker_plan_complete_task015.py`)
- `_supervisor` -> `_wrrp015_supervisor` (from `test_worker_retry_rejection_prompt_task015.py`)
- `_task` -> `_wrrp015_task` (from `test_worker_retry_rejection_prompt_task015.py`)
- `REPO_ROOT` -> `wrrp015_REPO_ROOT` (from `test_worker_retry_rejection_prompt_task015.py`)
- `_ok` -> `_inso004_ok` (from `test_interrupt_not_substantive_outcome_task004.py`)
- `_supervisor` -> `_inso004_supervisor` (from `test_interrupt_not_substantive_outcome_task004.py`)
- `_log_messages` -> `_inso004_log_messages` (from `test_interrupt_not_substantive_outcome_task004.py`)
- `_supervisor` -> `_ftsd011_supervisor` (from `test_failed_to_skipped_dependent_doom_task011.py`)
- `TASK_1` -> `svd013_TASK_1` (from `test_settled_vs_done_task013.py`)
- `_ok` -> `_svd013_ok` (from `test_settled_vs_done_task013.py`)
- `_supervisor` -> `_svd013_supervisor` (from `test_settled_vs_done_task013.py`)
- `_events` -> `_svd013_events` (from `test_settled_vs_done_task013.py`)
- `_task` -> `_ssn_task` (from `test_scheduler_select_next_task.py`)
- `REPO_ROOT` -> `eotr008_REPO_ROOT` (from `test_engine_owns_terminal_record_task008.py`)
- `_supervisor` -> `_eotr008_supervisor` (from `test_engine_owns_terminal_record_task008.py`)
"""

from __future__ import annotations


# ----------------------------------------------------------------------
# Folded from tests/test_failed_task_vocabulary.py (task 028, requirement D.7).
# ----------------------------------------------------------------------

import asyncio
import json
import re
from pathlib import Path

import pytest

from ralphd.engine import contracts
from ralphd.engine.config import JobConfig
from ralphd.engine.loop import LoopSupervisor
from ralphd.engine.runner import IterationResult
from ralphd.engine.state import (
    TASK_ACTIONABLE_STATUSES,
    TASK_FAILURE_KINDS,
    TASK_FAILURE_REQUIREMENT_UNMET,
    TASK_FAILURE_VALIDATION_EXHAUSTED,
    VALIDATION_ATTEMPT_LIMIT,
    RunDir,
    atomic_write_json,
    task_failure_kind,
    task_is_actionable,
)
from tests.conftest import supervisor_with_runner

REPO = Path(__file__).resolve().parents[1]

TASK_ID = "001"


# -- scaffolding -----------------------------------------------------------


def _fv_ok(text: str = "working") -> IterationResult:
    """A perfectly ordinary finished iteration (not an instant failure, not an
    infra fault): exit 0, real duration, some usage."""
    r = IterationResult(exit_code=0)
    r.final_text = text
    r.duration_s = 30.0
    r.usage = {"input": 10, "output": 5, "totalTokens": 15}
    return r


def _write_plan(sup: LoopSupervisor, tasks: list[dict]) -> None:
    atomic_write_json(sup.run.tasks_file, {"version": 1, "tasks": tasks})


# Task 004 (v0.20.1): mirrors `plan_ingestion._TASK_PASSTHROUGH_FIELDS`
# exactly -- `tier` and `estimatedIterations` are proposal-legal fields the
# real ingestion path carries through to tasks.json verbatim, so a helper
# that silently dropped them made a test's scenario parameter a no-op (the
# task landed on disk with the DEFAULT `estimatedIterations: 1` while the
# test claimed to exercise 3). Any test relying on a passthrough value
# should also assert it back off the ingested record.
_PROPOSAL_PASSTHROUGH = ("id", "title", "successCriteria", "dependsOn",
                         "orderingOnly", "priority", "tier",
                         "estimatedIterations")


def _write_proposal(sup: LoopSupervisor, tasks: list[dict],
                    name: str = "plan-proposal.json") -> None:
    """Task 120 (#60 pillar 1): write the file-borne `plan-proposal.json` a
    real planning iteration drafts -- passthrough fields only, exactly what
    `plan_ingestion.ingest_proposal` accepts (no `status`, no harness-owned
    bookkeeping like `validationAttempts`) -- for use by a "planning" phase
    handler in these scripted-agent tests. `_write_plan` above stays a
    direct tasks.json seed for the tests that call `_verify_task`/etc
    directly with no planning phase involved at all."""
    clean = [{k: v for k, v in t.items() if k in _PROPOSAL_PASSTHROUGH}
             for t in tasks]
    atomic_write_json(sup.run.root / name, {"version": 1, "tasks": clean})


def _plan_pointer(name: str = "plan-proposal.json", summary: str = "planned") -> str:
    return json.dumps({"proposal_path": name, "summary": summary})


def _fv_run_resumed(root: Path, handlers: dict, tasks: list[dict],
                **cfg_kw) -> tuple[LoopSupervisor, str]:
    """Task 120 (#60): planning's contract cannot carry a task's own
    `status`/`validationAttempts`/`validationNotes` (the harness always
    initialises every INGESTED task to `pending`) -- a plan already
    holding those fields is exactly what a RESUMED run's tasks.json
    legitimately looks like (a migration case, per this module's own
    docstring), so these tests seed disk state the way a resume would and
    skip planning entirely, the same way `_resume_point()` decides a real
    engine process should -- `handlers` must have no `"planning"` key.
    """
    RunDir(root=root).update_status(state="starting")
    sup = _fv_supervisor(root, handlers, **cfg_kw)
    _write_plan(sup, tasks)
    sup.iterations_used = 1
    return sup, asyncio.run(sup._run_job_core())


def _fv_task(**over) -> dict:
    task = {"id": TASK_ID, "title": "make the thing work",
            "status": "pending", "successCriteria": "the thing works"}
    task.update(over)
    return task


def _set_status(sup: LoopSupervisor, status: str) -> None:
    doc = sup.run.read_tasks()
    doc["tasks"][0]["status"] = status
    sup.run.write_tasks(doc)


class _fv_ScriptedAgent:
    """Stands in for PiRunner, dispatching on the phase the engine recorded in
    status.json just before launching the agent -- so one script can describe a
    whole run (planning, several worker/verify rounds, review) the way an
    operator reads it, instead of by call index."""

    def __init__(self, sup: LoopSupervisor, handlers: dict):
        self.sup = sup
        self.handlers = handlers
        self.phases: list[str] = []
        self.running = False

    def interrupt(self) -> bool:
        return self.running

    async def run(self, prompt, transcript, **kw) -> IterationResult:
        phase = self.sup.run.read_status().get("phase")
        self.phases.append(phase)
        self.running = True
        try:
            return self.handlers[phase](self.sup, prompt)
        finally:
            self.running = False


def _steps(*steps):
    """A per-phase handler built from a list of callables; the last repeats
    forever (a worker that has run out of ideas keeps having none)."""
    calls = {"n": 0}

    def handler(sup: LoopSupervisor, prompt: str) -> IterationResult:
        calls["n"] += 1
        return steps[min(calls["n"] - 1, len(steps) - 1)](sup, prompt)

    return handler


_fv_supervisor = supervisor_with_runner(
    lambda sup, handlers: _fv_ScriptedAgent(sup, handlers),
    run_id="v07-failed-vocab", iterations=15, max_approaches=1, vigilant=True,
    on_complete="exit", infra_retry_backoff_s=[0.0], infra_retry_backoff_max_s=0.0,
    infra_outage_budget_s=1000.0)


def _fv_run(root: Path, handlers: dict, **cfg_kw) -> tuple[LoopSupervisor, str]:
    RunDir(root=root).update_status(state="starting")
    sup = _fv_supervisor(root, handlers, **cfg_kw)
    return sup, asyncio.run(sup._run_job_core())


async def _drive_until_paused(sup: LoopSupervisor,
                             expect_reason: str = "stagnation") -> None:
    """Task 012 (requirement B.3, #153): a `_run_job_core()` that hits the
    stagnation guard on the LAST approach now pauses and escalates instead
    of ending the run `failed` -- and stays parked at `_gate()` forever
    without an operator's `resume()`, which none of these scripted-runner
    tests ever send. Used only by tests whose scenario runs the guard out
    on the (default, `max_approaches=1`) last approach: starts
    `_run_job_core()` as a background task, polls `status.json` for
    `state == "paused"`, then cancels the still-pending task so the test's
    own event loop can close cleanly -- the same shape
    `tests/test_last_approach.py` uses for its own
    in-process resume coverage, minus the resume.

    The bounded wait AND the pause's identity both live here (task 042,
    steer 004): the poll can only ever end at a `paused` status -- a run
    that ends any other way, or a stub engine that parks somewhere the
    guard never reaches, raises instead of hanging the suite -- and the
    helper then asserts `pauseReason == expect_reason` (`"stagnation"` for
    every caller so far), so no caller can mistake some other pause for the
    stagnation pause it meant to drive.
    """
    task = asyncio.ensure_future(sup._run_job_core())
    for _ in range(500):
        if sup.run.read_status().get("state") == "paused":
            break
        if task.done():
            break
        await asyncio.sleep(0.01)
    else:
        task.cancel()
        raise AssertionError("run never reached `paused`")
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    status = sup.run.read_status()
    assert status.get("state") == "paused", status
    if expect_reason is not None:
        assert status.get("pauseReason") == expect_reason, status


def _fv_events(root: Path) -> list[dict]:
    p = root / "events.jsonl"
    if not p.exists():
        return []
    return [json.loads(x) for x in p.read_text().splitlines() if x.strip()]


def _fv_log_messages(root: Path) -> str:
    return "\n".join(ev.get("message") or "" for ev in _fv_events(root)
                     if ev.get("type") == "log")


def _phases_recorded(root: Path) -> list[str]:
    return [ev.get("phase") for ev in _fv_events(root) if ev.get("type") == "phase"]


# The agent's side of the incident: it finishes the task, the verifier never
# agrees, and once the record says `failed` the worker has nothing legitimate
# left to do with it.
def _fv_plan(sup: LoopSupervisor, prompt: str = "") -> IterationResult:
    _write_proposal(sup, [_fv_task()])
    return _fv_ok("planned one task\n" + _plan_pointer())


def _claim_done(sup: LoopSupervisor, prompt: str = "") -> IterationResult:
    _set_status(sup, "completed")
    return _fv_ok("I believe this is done now")


def _give_up(sup: LoopSupervisor, prompt: str = "") -> IterationResult:
    """A worker iteration that touches nothing: it re-reads a `failed` task it
    cannot legitimately relabel and writes only prose."""
    return _fv_ok("task 001 is failed; I cannot advance it")


def _verify_miss(sup: LoopSupervisor, prompt: str = "") -> IterationResult:
    # Task 001 (#119, requirement A.1) widened `_verify_contract_ladder` to
    # re-ask ANY not-yet-valid verify payload (absent, malformed JSON, or a
    # schema violation) instead of letting `_verify_task` burn a validation
    # attempt on it directly -- this fixture used to return bare prose,
    # which is now exactly that shape and legitimately re-asks. This stub's
    # phase-dispatching `_steps` counter has no way to tell an uncharged
    # re-ask from an ordinary scripted call, so a re-ask silently consumes
    # the NEXT step of whatever `_steps(...)` sequence is wired to "verify"
    # for a caller that uses one (see
    # test_a_worker_that_carves_out_a_follow_up_task_is_not_pre_empted,
    # which relied on exactly one verify call per round). Returning an
    # already-valid `rejected` verdict -- the shape `_verify_contract_
    # ladder`'s own skip condition names verbatim -- keeps this fixture's
    # meaning ("verification does not agree yet") while never entering the
    # ladder at all, matching what every other test in this module already
    # assumed about it.
    m = re.search(r"\*\*id\*\*: (\S+)", prompt)
    tid = m.group(1) if m else TASK_ID
    return _fv_ok(json.dumps({"task_id": tid, "verdict": "rejected",
                          "reason": "criterion 2 does not hold: no test asserts it"}))


def _verify_pass(sup: LoopSupervisor, prompt: str = "") -> IterationResult:
    """Reports `verdict: "verified"` for WHICHEVER task the verify prompt
    names -- the id is the one thing `_verify_task` matches on."""
    m = re.search(r"\*\*id\*\*: (\S+)", prompt)
    tid = m.group(1) if m else TASK_ID
    return _fv_ok(json.dumps({"task_id": tid, "verdict": "verified"}))


def _fv_review_verified(sup: LoopSupervisor, prompt: str = "") -> IterationResult:
    # Task 119 (#60 pillar 3, fact 9): review reports through the `review`
    # JSON contract, not the retired `<promise>VERIFIED</promise>` sentinel.
    return _fv_ok("the PRD holds apart from the known hole\n"
               + json.dumps({"verdict": "verified"}))


def _review_rejects(sup: LoopSupervisor, prompt: str = "") -> IterationResult:
    # GH #209 defect 2: this used to reply with bare prose, which produced
    # the `review rejected approach 1` log its one caller asserts only
    # because an exhausted review contract ladder was SCORED as a
    # rejection. It no longer is (a reply whose verdict claim was never
    # readable holds for an operator instead), and this node's subject is
    # that a reviewer DID look and judged the hole fatal -- so the reviewer
    # now says exactly that through the contract, and every assertion at
    # the call site is unchanged.
    return _fv_ok(json.dumps(
        {"verdict": "rejected",
         "reason": "requirement 3 is not met; the failed task is the gap"}))


@pytest.fixture
def exhausted_run(tmp_path) -> tuple[LoopSupervisor, str]:
    """THE incident, driven end to end: three validation rounds are consumed on
    the only task, then the worker spends an iteration without resolving it."""
    root = tmp_path / "run"
    return _fv_run(root, {"planning": _fv_plan,
                       "worker": _steps(_claim_done, _claim_done,
                                        _claim_done, _give_up),
                       "verify": _verify_miss,
                       "review": _fv_review_verified})


# -- the vocabulary: which kind of `failed` --------------------------------


def test_the_two_kinds_are_named_and_there_are_exactly_two():
    """A closed vocabulary, not free text: a third value would have to be
    added here (and to SPEC 5.3) rather than invented by a writer."""
    assert TASK_FAILURE_VALIDATION_EXHAUSTED == "validation-exhausted"
    assert TASK_FAILURE_REQUIREMENT_UNMET == "requirement-unmet"
    assert TASK_FAILURE_KINDS == (TASK_FAILURE_VALIDATION_EXHAUSTED,
                                 TASK_FAILURE_REQUIREMENT_UNMET)
    assert VALIDATION_ATTEMPT_LIMIT == 3


@pytest.mark.parametrize("task, expected", [
    # not failed at all -> no kind, on every other status
    (_fv_task(status="pending"), None),
    (_fv_task(status="in-progress"), None),
    (_fv_task(status="completed"), None),
    (_fv_task(status="validation-failed", validationAttempts=2), None),
    (_fv_task(status="skipped"), None),
    # the label, when the engine wrote one
    (_fv_task(status="failed", failureKind="validation-exhausted"),
     TASK_FAILURE_VALIDATION_EXHAUSTED),
    (_fv_task(status="failed", failureKind="requirement-unmet"),
     TASK_FAILURE_REQUIREMENT_UNMET),
    # MIGRATION: a tasks.json written before the label existed. The engine's
    # own `failed` was only ever written at the attempt limit, so that is what
    # the evidence on the record means.
    (_fv_task(status="failed", validationAttempts=3),
     TASK_FAILURE_VALIDATION_EXHAUSTED),
    (_fv_task(status="failed", validationAttempts=4),
     TASK_FAILURE_VALIDATION_EXHAUSTED),
    # an agent's own verdict: no label, no exhausted rounds
    (_fv_task(status="failed"), TASK_FAILURE_REQUIREMENT_UNMET),
    (_fv_task(status="failed", validationAttempts=0), TASK_FAILURE_REQUIREMENT_UNMET),
    (_fv_task(status="failed", validationAttempts=2), TASK_FAILURE_REQUIREMENT_UNMET),
    # garbage cannot crash a reader nor invent a third meaning
    (_fv_task(status="failed", failureKind="explodey", validationAttempts=3),
     TASK_FAILURE_VALIDATION_EXHAUSTED),
    (_fv_task(status="failed", failureKind=None), TASK_FAILURE_REQUIREMENT_UNMET),
    (_fv_task(status="failed", validationAttempts="3"),
     TASK_FAILURE_REQUIREMENT_UNMET),
    ({}, None),
    ({"status": "failed"}, TASK_FAILURE_REQUIREMENT_UNMET),
])
def test_task_failure_kind_reads_every_record_shape(task, expected):
    assert task_failure_kind(task) == expected


def test_a_non_dict_task_is_not_read_as_failed():
    """`tasks.json` is agent-written: a reader must survive junk in the list."""
    assert task_failure_kind("001") is None            # type: ignore[arg-type]
    assert task_failure_kind(None) is None             # type: ignore[arg-type]


@pytest.mark.parametrize("status, actionable", [
    ("pending", True), ("in-progress", True), ("validation-failed", True),
    ("completed", False), ("skipped", False), ("failed", False),
])
def test_which_statuses_a_worker_iteration_could_still_act_on(status, actionable):
    assert task_is_actionable(_fv_task(status=status)) is actionable
    assert (status in TASK_ACTIONABLE_STATUSES) is actionable


def test_an_unreadable_task_counts_as_actionable():
    """Same rule as the empty-plan guard: nothing unreadable may make a plan
    look finished, because "finished" is what routes a run to review."""
    assert task_is_actionable({}) is True
    assert task_is_actionable({"status": "who knows"}) is False
    assert task_is_actionable("junk") is True          # type: ignore[arg-type]


# -- the engine labels its own verdict -------------------------------------


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


def test_the_engine_labels_its_own_failed_verdict(tmp_path):
    """`_verify_task` forcing `failed` at the attempt limit means exactly one
    thing -- the rounds are used up -- and the record says so."""
    root = tmp_path / "run"
    root.mkdir()
    run = RunDir(root=root)
    sup = LoopSupervisor(
        JobConfig(run_id="unit", iterations=9, max_approaches=1, vigilant=True,
                  on_complete="exit", infra_retry_backoff_s=[0.0],
                  infra_retry_backoff_max_s=0.0, infra_outage_budget_s=0.0),
        run, root)
    sup.runner = _FixedRunner(lambda: _fv_ok("criterion 2 does not hold"))  # type: ignore[assignment]
    _write_plan(sup, [_fv_task(status="completed")])

    kinds = []
    for _ in range(VALIDATION_ATTEMPT_LIMIT):
        task = sup.run.read_tasks()["tasks"][0]
        assert asyncio.run(sup._verify_task(task)) is False
        after = sup.run.read_tasks()["tasks"][0]
        kinds.append((after["status"], after.get("failureKind"),
                      after["validationAttempts"]))

    assert kinds[:-1] == [("validation-failed", None, 1),
                          ("validation-failed", None, 2)], kinds
    assert kinds[-1] == ("failed", TASK_FAILURE_VALIDATION_EXHAUSTED, 3)
    assert task_failure_kind(sup.run.read_tasks()["tasks"][0]) == \
        TASK_FAILURE_VALIDATION_EXHAUSTED

    # ...and the rounds really are spent: a task already at the limit is not
    # re-verified, so no further attempt can move the label either way.
    spent = sup.runner.calls                          # type: ignore[attr-defined]
    exhausted = sup.run.read_tasks()["tasks"][0]
    assert asyncio.run(sup._verify_task(exhausted)) is False
    assert sup.runner.calls == spent, \
        "an exhausted task must not consume another verify iteration"
    assert sup.run.read_tasks()["tasks"][0] == exhausted


# -- _unresolved_failures: when is a run out of moves ----------------------


def _blocked(tmp_path: Path, tasks: list[dict]) -> list[dict]:
    sup = _fv_supervisor(tmp_path / "b", {})
    return sup._unresolved_failures({"tasks": tasks})


@pytest.mark.parametrize("tasks, blocked_ids", [
    # nothing actionable and something failed -> the run is out of moves
    ([_fv_task(status="failed", validationAttempts=3)], [TASK_ID]),
    ([_fv_task(status="failed")], [TASK_ID]),                 # requirement-unmet too
    ([_fv_task(status="completed"), _fv_task(id="002", status="failed",
                                      validationAttempts=3)], ["002"]),
    ([_fv_task(status="skipped"), _fv_task(id="002", status="failed")], ["002"]),
    ([_fv_task(status="failed"), _fv_task(id="002", status="failed")],
     [TASK_ID, "002"]),
    # one actionable task anywhere -> a worker iteration still has work
    ([_fv_task(status="pending"), _fv_task(id="002", status="failed")], []),
    ([_fv_task(status="in-progress"), _fv_task(id="002", status="failed")], []),
    ([_fv_task(status="validation-failed", validationAttempts=1),
      _fv_task(id="002", status="failed")], []),
    # no failed task -> the worker's own COMPLETE is what gates review (R5)
    ([_fv_task(status="completed")], []),
    ([_fv_task(status="completed"), _fv_task(id="002", status="skipped")], []),
    # an empty or unreadable plan is never "out of moves"
    ([], []),
    ([{}], []),
])
def test_when_a_plan_has_no_move_left(tmp_path, tasks, blocked_ids):
    assert [t["id"] for t in _blocked(tmp_path, tasks)] == blocked_ids


# -- the whole run: a verdict with nobody editing the record ---------------


def test_the_run_reaches_a_terminal_verdict_with_nobody_editing_the_record(
        exhausted_run):
    """THE requirement (#33): the run that used to sit here until an operator
    hand-edited `tasks.json` now reaches a terminal state AND a verdict on its
    own."""
    sup, state = exhausted_run
    status = sup.run.read_status()
    assert (state, status["state"], status["verdict"]) == \
        ("succeeded", "succeeded", "verified"), status
    assert status["endedAt"]
    # the record is untouched: the task is still `failed`, and the verdict was
    # NOT bought by relabelling it
    task = sup.run.read_tasks()["tasks"][0]
    assert task["status"] == "failed"
    assert task["failureKind"] == TASK_FAILURE_VALIDATION_EXHAUSTED
    assert task["validationAttempts"] == VALIDATION_ATTEMPT_LIMIT


def test_the_route_to_review_is_taken_and_says_why(exhausted_run):
    """Not silently: the routing decision is a warning event naming the tasks
    that blocked and which kind of `failed` each one is."""
    sup, _state = exhausted_run
    msgs = _fv_log_messages(sup.run.root)
    assert "routing to review" in msgs, msgs
    assert f"{TASK_ID} ({TASK_FAILURE_VALIDATION_EXHAUSTED})" in msgs, msgs
    assert "review" in _phases_recorded(sup.run.root)
    assert [ev for ev in _fv_events(sup.run.root)
            if ev.get("type") == "signal" and ev.get("signal") == "VERIFIED"]


def test_the_worker_never_signalled_complete_and_the_approach_never_failed(
        exhausted_run):
    """The route is not "the worker got away with claiming COMPLETE", and it
    is not the stagnation guard either -- both would be a different bug."""
    sup, _state = exhausted_run
    events = _fv_events(sup.run.root)
    assert not [ev for ev in events
                if ev.get("type") == "signal" and ev.get("signal") == "COMPLETE"]
    msgs = _fv_log_messages(sup.run.root)
    assert "no task progress" not in msgs, msgs
    assert "review rejected" not in msgs
    assert sup.run.read_status()["approach"] == 1


def test_the_run_spends_one_iteration_on_the_blocked_state_not_three(
        exhausted_run):
    """The worker gets exactly one iteration to apply an honest resolution
    (both of which change tasks.json); the run does not burn the stagnation
    guard's three before anyone renders a verdict."""
    sup, _state = exhausted_run
    phases = sup.runner.phases                        # type: ignore[attr-defined]
    # planning, then 3 x (worker, verify), then ONE more worker, then review
    assert phases == ["planning"] + ["worker", "verify"] * 3 + \
        ["worker", "review"], phases
    assert sup.run.read_status()["iterationsUsed"] == len(phases)


def test_a_rejected_review_still_ends_the_run_with_a_verdict(tmp_path):
    """The engine renders entry to review, not the verdict: a reviewer who
    judges the hole fatal ends the run unverified -- but a reviewer DID look,
    which is the whole difference from the incident."""
    root = tmp_path / "run"
    sup, state = _fv_run(root, {"planning": _fv_plan,
                             "worker": _steps(_claim_done, _claim_done,
                                              _claim_done, _give_up),
                             "verify": _verify_miss,
                             "review": _review_rejects})
    status = sup.run.read_status()
    assert (state, status["verdict"]) == ("failed", "unverified"), status
    assert "review" in _phases_recorded(root)
    assert "review rejected approach 1" in _fv_log_messages(root)


def test_a_legacy_failed_record_also_routes_to_review(tmp_path):
    """MIGRATION, end to end: a plan written before `failureKind` existed --
    `failed` with three recorded rounds and no label -- reads as exhausted and
    routes exactly the same way. The engine does not rewrite it."""
    root = tmp_path / "run"
    sup, state = _fv_run_resumed(
        root, {"worker": _give_up, "verify": _verify_miss,
              "review": _fv_review_verified},
        [_fv_task(status="failed", validationAttempts=3,
              validationNotes="round 3: criterion 2 unmet")])
    status = sup.run.read_status()
    assert (state, status["verdict"]) == ("succeeded", "verified"), status
    assert f"{TASK_ID} ({TASK_FAILURE_VALIDATION_EXHAUSTED})" in \
        _fv_log_messages(root)
    task = sup.run.read_tasks()["tasks"][0]
    assert "failureKind" not in task, \
        "an old record is read, not retroactively rewritten"


def test_a_plan_with_work_left_is_never_routed_to_review(tmp_path):
    """The route must not fire while a worker iteration still has something to
    pick up -- otherwise one failed task would cut a plan's remaining work
    short. Here the second task stays `pending` and the worker keeps ignoring
    it, so the run ends on the stagnation guard, as before -- except this is
    the (default, `max_approaches=1`) LAST approach, so the guard now pauses
    and escalates rather than ending the run `failed` (task 012, requirement
    B.3)."""
    root = tmp_path / "run"
    RunDir(root=root).update_status(state="starting")
    sup = _fv_supervisor(root, {"worker": _give_up, "verify": _verify_miss,
                             "review": _fv_review_verified})
    _write_plan(sup, [_fv_task(status="failed", validationAttempts=3),
                      _fv_task(id="002", status="pending")])
    sup.iterations_used = 1
    asyncio.run(_drive_until_paused(sup))
    msgs = _fv_log_messages(root)
    assert "routing to review" not in msgs, msgs
    assert "3 iterations with no task progress" in msgs, msgs
    assert "review" not in _phases_recorded(root)
    status = sup.run.read_status()
    assert status.get("state") == "paused"
    assert status.get("pauseReason") == "stagnation"


def test_a_finished_plan_still_waits_for_the_workers_complete(tmp_path):
    """R5 is untouched where it applies: with every task `completed` and no
    `failed` one, entry to review is still the worker's COMPLETE. A worker that
    finishes the work and forgets to signal gets the stagnation guard, exactly
    as before this task -- except this is the (default, `max_approaches=1`)
    LAST approach, so the guard now pauses and escalates rather than ending
    the run `failed` (task 012, requirement B.3)."""
    def finish_quietly(sup: LoopSupervisor, prompt: str = "") -> IterationResult:
        _set_status(sup, "completed")
        return _fv_ok("done, no signal")

    root = tmp_path / "run"
    RunDir(root=root).update_status(state="starting")
    sup = _fv_supervisor(root, {"planning": _fv_plan,
                             "worker": _steps(finish_quietly, _give_up),
                             "verify": _verify_pass,
                             "review": _fv_review_verified})
    asyncio.run(_drive_until_paused(sup))
    msgs = _fv_log_messages(root)
    assert "routing to review" not in msgs, msgs
    assert "3 iterations with no task progress" in msgs, msgs
    status = sup.run.read_status()
    assert status.get("state") == "paused"
    assert status.get("pauseReason") == "stagnation"


def test_a_worker_that_carves_out_a_follow_up_task_is_not_pre_empted(tmp_path):
    """The better outcome stays reachable: the iteration after the task fails
    belongs to the worker, so `worker.md`'s first resolution (a NEW task for the
    residual gap) still happens and the run reaches review with the gap closed.

    RETARGETED by task 046 (#55): the run entered review through the ladder
    (an unearned COMPLETE refused, exhausted, then guard 9's own end-of-loop
    `_unresolved_failures` check caught it on the NEXT stagnant pass).

    RETARGETED AGAIN by task 003 (#166): `plan_complete` is the untyped
    claim's contract value now (task 015), and this scenario -- 001
    `failed` with its residual carved into 002, 002 `completed`, nothing
    else in the plan actionable -- is EXACTLY "a `plan_complete` refused
    for a `failed` task whose residual was carved out" (#166's own
    words). The engine now recognises that shape directly (`_unresolved_
    failures` non-empty, `assigned_id is None`) and routes straight to
    review with `blocked_with_failures`, the SAME guard 9 already uses at
    the end of the worker loop -- never through `contract_enforcement.
    enforce_contract`'s re-ask ladder at all, because there is no
    legitimate re-ask here: worker.md's own two resolutions (carve a new
    task, or relabel `skipped`) are both already proven unavailable by
    this very claim. What the test still pins is unchanged in substance:
    the worker was not pre-empted (it got its carve iteration), the
    follow-up task is in the plan, and the run ends succeeded/verified
    after a real review."""
    def carve_follow_up(sup: LoopSupervisor, prompt: str = "") -> IterationResult:
        doc = sup.run.read_tasks()
        doc["tasks"].append(_fv_task(id="002", status="completed",
                                  title="close the residual gap of 001"))
        sup.run.write_tasks(doc)
        return _fv_ok("carved 002 for the residual gap")

    def signal_complete(sup: LoopSupervisor, prompt: str = "") -> IterationResult:
        # Task 015 (requirement O, #100)/task 003 (#166): reported here
        # with nothing assigned this iteration (both 001 and 002 are
        # terminal, `failed`/`completed`) -- structurally valid, but a
        # semantically UNEARNED claim: 001 is `failed`, which blocks
        # completion. Task 003 (#166) means this is no longer carried to
        # the ladder at all -- see the assertions below.
        return _fv_ok("every task resolved\n" + json.dumps({"verdict": "plan_complete"}))

    root = tmp_path / "run"
    sup, state = _fv_run(root, {"planning": _fv_plan,
                             "worker": _steps(_claim_done, _claim_done,
                                              _claim_done, carve_follow_up,
                                              signal_complete),
                             "verify": _steps(_verify_miss, _verify_miss,
                                              _verify_miss, _verify_pass),
                             "review": _fv_review_verified})
    assert (state, sup.run.read_status()["verdict"]) == ("succeeded", "verified")
    ids = [t["id"] for t in sup.run.read_tasks()["tasks"]]
    assert ids == ["001", "002"], "the worker got its carve iteration"
    assert "review" in _phases_recorded(root)
    events = _fv_events(root)
    assert not [ev for ev in events if ev.get("type") == "contract.violation"], \
        "task 003 (#166): this claim is routed directly, never through the ladder"
    assert not [ev for ev in events if ev.get("type") == "contract.agent_fault"], \
        "no ladder exhaustion either -- there was no ladder pass to exhaust"
    msgs = _fv_log_messages(root)
    assert ("worker reported 'plan_complete' but 1 failed task(s) block it "
            "and nothing else is actionable: 001") in msgs, msgs
    assert "routing to review for a verdict instead of re-asking on the ladder" in msgs, msgs
    assert not [ev for ev in events
                if ev.get("type") == "signal" and ev.get("signal") == "COMPLETE"], \
        "an unearned COMPLETE is never signalled"


def test_a_pending_carved_follow_up_still_faces_the_ordinary_ladder(tmp_path):
    """Companion to the test above (task 003, #166): the new direct-to-review
    shortcut fires ONLY when nothing else in the plan is actionable
    (`_unresolved_failures`). Here the carved follow-up, 002, is left
    `pending` instead of `completed` -- a genuinely outstanding task the
    scheduler still has work to hand out for -- so it is THIS iteration's
    `assigned_id` (task 116's own scheduler: a pending task with no unmet
    dependency is always picked over nothing), which makes the very same
    `plan_complete` claim structurally invalid (`_apply_worker_plan_
    complete_report`'s "only valid when no task was assigned" check, task
    015) rather than caught by the new shortcut -- the shortcut is gated on
    `assigned_id is None` and never even runs `_unresolved_failures` when a
    task was assigned. So this claim reaches `contract_enforcement.
    enforce_contract`'s ordinary re-ask ladder exactly as it did before
    task 003, unrepaired every pass (002 stays `pending` forever -- the
    scripted worker only ever repeats the same illegal claim) -- and, since
    this is the (default, `max_approaches=1`) LAST approach, three such
    no-progress passes pause the run and escalate rather than route it to
    review, exactly like `test_a_plan_with_work_left_is_never_routed_to_
    review` above pins for its own (already-`failed`-vs-`pending`) shape."""
    def claim_plan_complete(sup: LoopSupervisor, prompt: str = "") -> IterationResult:
        return _fv_ok("every task resolved\n" + json.dumps({"verdict": "plan_complete"}))

    root = tmp_path / "run"
    RunDir(root=root).update_status(state="starting")
    sup = _fv_supervisor(root, {"worker": claim_plan_complete})
    _write_plan(sup, [_fv_task(status="failed", validationAttempts=3),
                      _fv_task(id="002", status="pending",
                            title="close the residual gap of 001")])
    sup.iterations_used = 1
    asyncio.run(_drive_until_paused(sup))

    events = _fv_events(root)
    violations = [ev for ev in events if ev.get("type") == "contract.violation"]
    assert violations, "a genuinely in-progress/pending task must still reach the ladder"
    all_errors = " ".join(" ".join(ev.get("errors", [])) for ev in violations)
    assert "only valid when no task was assigned" in all_errors, all_errors
    assert "'002'" in all_errors, all_errors
    fault_events = [ev for ev in events if ev.get("type") == "contract.agent_fault"]
    assert fault_events, "the ladder is exhausted, never skipped, for this shape"
    msgs = _fv_log_messages(root)
    assert "routing to review for a verdict instead of re-asking on the ladder" not in msgs, msgs
    task_002 = next(t for t in sup.run.read_tasks()["tasks"] if t["id"] == "002")
    assert task_002["status"] == "in-progress", \
        "a contract violation must not silently take the move anyway"
    assert not [ev for ev in events
                if ev.get("type") == "signal" and ev.get("signal") == "COMPLETE"], \
        "an unearned COMPLETE is never signalled"


# -- the docs ---------------------------------------------------------------


@pytest.mark.parametrize("path, needles", [
    ("SPEC.md", ["**The two meanings of `failed` (task 024, #33).**",
                 "| `tasks[].failureKind` | string |",
                 "| `validation-exhausted` |",
                 "`state.task_failure_kind()` is the one reader",
                 "`LoopSupervisor._unresolved_failures()`",
                 "`TASK_ACTIONABLE_STATUSES`",
                 "routed to review by the engine rather than left to stagnate",
                 "`validationAttempts` reaches `VALIDATION_ATTEMPT_LIMIT` (3)"]),
    ("docs/architecture.md",
     ["### The two meanings of `failed`",
      '`failureKind: "validation-exhausted"`',
      "`state.TASK_ACTIONABLE_STATUSES`",
      "*engine's* decision, not the worker's"]),
])
def test_the_docs_state_the_vocabulary_and_the_route(path, needles):
    text = (REPO / path).read_text()
    for needle in needles:
        assert needle in text, f"{path} is missing: {needle}"


# ----------------------------------------------------------------------
# Folded from tests/test_stagnation_progress_witnesses_task010.py (task 028, requirement D.7).
# ----------------------------------------------------------------------

import asyncio
import json
import subprocess
from pathlib import Path

from ralphd.engine import state
from ralphd.engine.config import JobConfig
from ralphd.engine.loop import LoopSupervisor
from ralphd.engine.runner import IterationResult
from ralphd.engine.state import RunDir

from tests.conftest import make_supervisor

__all__ = []


# -- scaffolding: a supervisor whose workspace is NOT the run dir ----------


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def _init_git_workspace(workspace: Path) -> None:
    workspace.mkdir(parents=True, exist_ok=True)
    _git(workspace, "init", "-q")
    _git(workspace, "config", "user.email", "t@example.com")
    _git(workspace, "config", "user.name", "t")
    (workspace / "seed.txt").write_text("seed")
    _git(workspace, "add", "-A")
    _git(workspace, "commit", "-q", "-m", "seed")


_spw010_supervisor = lambda root, workspace, handlers, **cfg: make_supervisor(
    root, **{"run_id": "v019-task010", "workspace": workspace, "iterations": 20,
    "max_approaches": 1, "vigilant": True, "on_complete": "exit",
    "infra_retry_backoff_s": [0.0], "infra_retry_backoff_max_s": 0.0,
    "infra_outage_budget_s": 1000.0, "runner_factory": lambda sup: _fv_ScriptedAgent(sup,
    handlers), **cfg})


def _spw010_run(root: Path, workspace: Path, handlers: dict, **cfg_kw):
    RunDir(root=root).update_status(state="starting")
    sup = _spw010_supervisor(root, workspace, handlers, **cfg_kw)
    return sup, asyncio.run(sup._run_job_core())


def _spw010_plan(sup: LoopSupervisor, prompt: str = "") -> IterationResult:
    _write_proposal(sup, [_fv_task()])
    return _fv_ok("planned one task\n" + _plan_pointer())


def _noop(sup: LoopSupervisor, prompt: str = "") -> IterationResult:
    """An ordinary worker iteration: no tasks.json write, no commit, no
    tool-call evidence -- the shape the guard exists to catch."""
    return _fv_ok("looked at task 001 and made no change")


def _commit_only(sup: LoopSupervisor, prompt: str = "") -> IterationResult:
    """Witness (b): a real commit lands in the workspace; tasks.json is
    untouched."""
    n = sup.iterations_used
    (sup.workspace / f"f{n}.txt").write_text("x")
    _git(sup.workspace, "add", "-A")
    _git(sup.workspace, "commit", "-q", "-m", f"progress at iteration {n}")
    return _fv_ok("made a commit but tasks.json is untouched")


def _commit_only_no_verdict(sup: LoopSupervisor, prompt: str = "") -> IterationResult:
    """Witness (b) exactly like `_commit_only` above -- a real commit lands
    in the workspace, tasks.json is untouched -- but reporting it the way
    the worker contract actually requires: the final non-blank line is the
    literal `{"verdict": "no_verdict"}` object ("I did not finish this
    task this iteration"), the ONE shape `_step_worker` reads as the
    `no_verdict` contract value.

    Task 004 (v0.20.1), second validation finding: `_commit_only`'s prose
    final line (`"made a commit but tasks.json is untouched"`) is not JSON
    at all, so `_step_worker`'s `json.loads` fails, `payload` becomes
    `None` and `verdict` is `None` -- which never even enters the
    `if verdict is not None:` verdict-dispatch block, let alone reaches
    its `# verdict == "no_verdict"` fall-through. A test that means to
    drive `no_verdict` iterations has to send this payload, and
    `_worker_payloads_seen` below is how it proves the engine really
    parsed it rather than the test merely intending it.
    """
    n = sup.iterations_used
    (sup.workspace / f"f{n}.txt").write_text("x")
    _git(sup.workspace, "add", "-A")
    _git(sup.workspace, "commit", "-q", "-m", f"progress at iteration {n}")
    return _fv_ok(json.dumps({"verdict": "no_verdict"}))


def _worker_payloads_seen(monkeypatch) -> list[tuple]:
    """Task 004 (v0.20.1): the engine's OWN view of each worker report,
    recorded where `_step_worker` itself reads it -- `contracts.validate_
    payload("worker", payload)`, called inside the `if verdict is not
    None:` verdict-dispatch block with the payload the engine parsed from
    the iteration's final non-blank line. Returns the list the spy appends
    `(payload, schema_errors)` to, so a test can assert what verdict value
    the ENGINE saw (and that the worker schema accepted it, i.e. no
    contract re-ask ladder was entered) instead of asserting only what its
    own fake agent meant to send. A prose reply never reaches this call at
    all (`verdict is None` skips the whole block), so an empty list is
    itself the signal that no worker report was read as a verdict."""
    seen: list[tuple] = []
    real_validate = contracts.validate_payload

    def spy(hat, payload):
        errors = real_validate(hat, payload)
        if hat == "worker":
            seen.append((payload, errors))
        return errors

    monkeypatch.setattr(contracts, "validate_payload", spy)
    return seen


def _tool_heavy(count: int):
    """Witness (d): `count` `tool_execution_start` events land in THIS
    iteration's own transcript (the file `_run_iteration_once` already
    created before dispatching to the runner); tasks.json is untouched."""
    def handler(sup: LoopSupervisor, prompt: str = "") -> IterationResult:
        n = sup.iterations_used
        path = state.iteration_output_path(sup.run.root, n)
        with open(path, "a") as f:
            for _ in range(count):
                f.write(json.dumps(
                    {"type": "tool_execution_start", "toolName": "bash",
                     "args": {"command": "true"}}) + "\n")
        return _fv_ok("read a lot of files, made no change")
    return handler


def _make_petitions_file(workspace: Path, wave: str = "v0.19") -> Path:
    reports = workspace / "artifacts" / "reports"
    reports.mkdir(parents=True, exist_ok=True)
    path = reports / f"{wave}-petitions.md"
    path.write_text(f"# {wave} petitions\n\nNo petition filed yet in this wave.\n")
    return path


def _petition_no_verdict(obligation: str) -> IterationResult:
    """Witness (c): a valid petition declaration riding on an otherwise
    ordinary `"no_verdict"` reply -- tasks.json is untouched."""
    payload = {"verdict": "no_verdict",
               "petition": {"obligation": obligation, "type": "environmental",
                            "evidence": "no such capability in this sandbox"}}
    return _fv_ok(json.dumps(payload))


def _worker_iteration_count(root: Path) -> int:
    return sum(1 for ev in _fv_events(root)
              if ev.get("type") == "iteration.end" and ev.get("phase") == "worker")


# -- direction 1: a witness other than tasks.json resets the counter -------


def test_commits_and_tool_activity_alone_do_not_increment_the_counter_and_reset_it(
        tmp_path):
    """Task 002 (PRD requirement E, #206) folded these two siblings into one
    node to stay net-zero against the fast tier's 5800-test ceiling
    (`tests/test_tier_budgets.py`) while adding this task's own new
    coverage elsewhere -- every assertion below survives unchanged from the
    two retired functions (`test_commits_alone_do_not_increment_the_
    counter_and_reset_it` and `test_tool_activity_above_threshold_does_not_
    increment_the_counter`), just run back to back in one node instead of
    two: two ordinary no-op iterations (stagnant -> 2), a commit-only
    iteration (witness (b) resets to 0), two more no-ops (stagnant -> 2
    again, never 3) -- then the same shape a second time, with witness (d)
    (tool-call activity above `STAGNATION_TOOL_ACTIVITY_MIN_CALLS`)
    resetting the counter instead of a commit. The guard must never fire in
    either run, and all five worker iterations of each must actually
    run."""
    root = tmp_path / "commits"
    workspace = tmp_path / "commits-workspace"
    _init_git_workspace(workspace)
    # `_steps`' last handler repeats forever, so the iteration BUDGET (not
    # just the step list) has to be bounded to exactly 1 planning + 5
    # worker slots -- otherwise a 6th, repeated no-op iteration would run
    # on top of the five below and legitimately trip the guard on its own,
    # unrelated to whether the commit-only iteration reset the counter.
    _spw010_run(root, workspace, {
        "planning": _spw010_plan,
        "worker": _steps(_noop, _noop, _commit_only, _noop, _noop),
    }, iterations=6)
    msgs = _fv_log_messages(root)
    assert "3 iterations with no task progress" not in msgs, msgs
    assert _worker_iteration_count(root) == 5

    root2 = tmp_path / "tool-activity"
    workspace2 = tmp_path / "tool-activity-workspace"
    workspace2.mkdir()
    threshold = LoopSupervisor.STAGNATION_TOOL_ACTIVITY_MIN_CALLS
    _spw010_run(root2, workspace2, {
        "planning": _spw010_plan,
        "worker": _steps(_noop, _noop, _tool_heavy(threshold), _noop, _noop),
    }, iterations=6)
    msgs2 = _fv_log_messages(root2)
    assert "3 iterations with no task progress" not in msgs2, msgs2
    assert _worker_iteration_count(root2) == 5


def test_a_petition_riding_on_no_verdict_counts_as_progress_without_a_tasks_json_change(
        tmp_path):
    """The regression this task actually fixes: a valid petition
    declaration alongside `"no_verdict"` must reset the counter (witness
    (c)) exactly like a commit or tool activity would -- NOT have its own
    `stagnant = 0` silently overwritten by the ordinary tasks.json-only
    formula a few lines later, which is what the pre-fix code did (the
    petition iteration itself would have read as the third no-progress
    iteration and failed the approach on the spot, so the two follow-up
    no-ops below would never even have run)."""
    root = tmp_path / "run"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _make_petitions_file(workspace)
    sup, outcome = _spw010_run(root, workspace, {
        "planning": _spw010_plan,
        "worker": _steps(
            _noop, _noop,
            lambda s, p="": _petition_no_verdict(
                "requirement Z (task 010 coverage fixture)"),
            _noop, _noop),
    }, iterations=6)
    msgs = _fv_log_messages(root)
    assert "3 iterations with no task progress" not in msgs, msgs
    assert _worker_iteration_count(root) == 5


# -- direction 2: absence of every witness still stagnates ------------------


def test_none_of_the_four_witnesses_still_increments_the_counter(tmp_path):
    """The guard this must NOT weaken: three ordinary no-op iterations
    (no tasks.json change, no commit, no contract payload, no tool-call
    evidence) still trip the counter exactly as before this task, even
    with a real git workspace present and simply never touched. This
    drives `_step_worker()` directly (default `max_approaches=1`, i.e.
    the LAST approach) rather than the full `_run_job_core()` dispatch
    loop, because on the LAST approach task 012 (requirement B.3) changes
    what happens once the counter fires: the run now pauses and
    escalates (`tests/test_last_approach.py` is
    that pause's own dedicated coverage) instead of ending `failed` on
    paperwork, and a run parked on that pause inside `_run_job_core()`
    would sit forever awaiting a `resume()` this test never sends. The
    counter itself is unchanged either way -- the log line still fires.
    """
    root = tmp_path / "run"
    workspace = tmp_path / "workspace"
    _init_git_workspace(workspace)
    sup = _spw010_supervisor(root, workspace, {})
    sup._approach = 1
    RunDir(root=root).update_status(state="starting")
    _write_plan(sup, [_fv_task()])
    sup._ensure_criteria_baseline()  # mimics planning's own baseline (task 008)
    handler = _steps(_noop, _noop, _noop, _noop)

    async def _dispatch(prompt, transcript, **kw):
        return handler(sup, prompt)

    sup.runner.run = _dispatch  # type: ignore[method-assign]

    outcome = asyncio.run(sup._step_worker())
    msgs = _fv_log_messages(root)
    assert "3 iterations with no task progress" in msgs, msgs
    assert outcome == ("advance", "paused", "stagnation_last_approach")
    status = sup.run.read_status()
    assert status.get("state") == "paused"
    assert status.get("pauseReason") == "stagnation"


def test_tool_activity_below_the_threshold_still_increments_the_counter(tmp_path):
    """Witness (d) is a THRESHOLD, not "any tool call counts": one call
    below `STAGNATION_TOOL_ACTIVITY_MIN_CALLS` on every iteration must
    still stagnate exactly like zero calls would. Same direct-`_step_
    worker()` shape as the test just above, for the same reason (default
    `max_approaches=1` is the LAST approach, task 012's pause).
    """
    root = tmp_path / "run"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    below = LoopSupervisor.STAGNATION_TOOL_ACTIVITY_MIN_CALLS - 1
    assert below >= 1, "the threshold must be > 1 for this test to mean anything"
    sup = _spw010_supervisor(root, workspace, {})
    sup._approach = 1
    RunDir(root=root).update_status(state="starting")
    _write_plan(sup, [_fv_task()])
    sup._ensure_criteria_baseline()  # mimics planning's own baseline (task 008)
    handler = _steps(_tool_heavy(below), _tool_heavy(below),
                     _tool_heavy(below), _tool_heavy(below))

    async def _dispatch(prompt, transcript, **kw):
        return handler(sup, prompt)

    sup.runner.run = _dispatch  # type: ignore[method-assign]

    outcome = asyncio.run(sup._step_worker())
    msgs = _fv_log_messages(root)
    assert "3 iterations with no task progress" in msgs, msgs
    assert outcome == ("advance", "paused", "stagnation_last_approach")
    status = sup.run.read_status()
    assert status.get("state") == "paused"
    assert status.get("pauseReason") == "stagnation"


# ----------------------------------------------------------------------
# Folded from tests/test_task_absorption_handoff_task011.py (task 028, requirement D.7).
# ----------------------------------------------------------------------

import json

from ralphd.engine.config import JobConfig
from ralphd.engine.loop import LoopSupervisor
from ralphd.engine.runner import IterationResult


__all__ = []


def _absorption_streak(root) -> dict:
    from ralphd.engine.state import RunDir
    return RunDir(root=root).read_status().get("taskAbsorptionStreak") or {}


def _verify_ran(root) -> bool:
    return any(ev.get("phase") == "verify" for ev in _fv_events(root)
              if ev.get("type") == "iteration.start")


def _task_record(root, task_id: str = TASK_ID) -> dict:
    from ralphd.engine.state import RunDir
    doc = RunDir(root=root).read_tasks()
    return next(t for t in doc.get("tasks", []) if t.get("id") == task_id)


# -- the config knob ---------------------------------------------------------


def test_default_config_knob_value_is_three():
    assert JobConfig().task_absorption_max_iterations == 3


def test_env_var_overrides_the_config_knob(tmp_path, monkeypatch):
    monkeypatch.setenv("RALPHD_TASK_ABSORPTION_MAX_ITERATIONS", "7")
    cfg = JobConfig.load(tmp_path / "nonexistent-job.yaml")
    assert cfg.task_absorption_max_iterations == 7


# -- the hand-off itself ------------------------------------------------------


def test_three_consecutive_commit_only_iterations_never_force_verify_dispatch(
        tmp_path, monkeypatch):
    """Task 004 (v0.20.1 PRD requirement B.2, gate finding VG-002): the
    replacement for the three retired v0.19 B.2 hand-off nodes this module
    used to pin --
    `test_three_consecutive_commit_only_iterations_hand_the_task_to_verify`,
    `test_a_lower_configured_threshold_fires_sooner` and
    `test_a_broken_streak_resets_the_counter_and_needs_a_fresh_run` (see
    `docs/test-suite-audit.md` "Subsumed" for the mapping).
    A vigilant task carrying `estimatedIterations: 3` runs exactly three
    consecutive worker iterations, each landing a real commit and
    reporting the worker contract's `{"verdict": "no_verdict"}` ("still
    working") and nothing else, never a completion signal -- the exact
    shape that used to force
    `awaiting-validation` and dispatch `verify` on the worker's own behalf
    the instant the third one landed (`LoopSupervisor._record_task_
    absorption_streak`, deleted in this same commit). Now nothing does:
    the task is still `in-progress` after all three of them, `verify`
    never ran at all, and the `notes` field never grows the legacy
    hand-off sentence. `estimatedIterations: 3` also puts the task's own
    progress budget (`estimatedIterations + 2` = 5) safely ABOVE the three
    iterations driven here, so what this node observes is the absence of
    the deleted streak and nothing else -- no forced split is in play.
    Governing
    what SHOULD happen once a task genuinely exhausts its budget is the
    pre-existing progress-budget/forced-split path (task 016) -- pinned
    for THIS shape by tasks 005/006 (requirements B.3/B.4), never this
    test's own job."""
    root = tmp_path / "run"
    workspace = tmp_path / "workspace"
    _init_git_workspace(workspace)
    seen = _worker_payloads_seen(monkeypatch)

    def _plan_with_estimate(sup: LoopSupervisor, prompt: str = "") -> IterationResult:
        _write_proposal(sup, [_fv_task(estimatedIterations=3)])
        return _fv_ok("planned one task\n" + _plan_pointer())

    sup, outcome = _spw010_run(root, workspace, {
        "planning": _plan_with_estimate,
        "worker": _steps(*([_commit_only_no_verdict] * 3)),
    }, iterations=4)
    # the three iterations really were `no_verdict` CONTRACT iterations,
    # read from where the engine itself parses each worker report (not
    # from what the fake agent meant to send): a prose final line leaves
    # `payload = None` / `verdict = None` and never enters the verdict-
    # dispatch block at all, which is v0.20.1 task 004's second
    # validation finding. Empty `schema_errors` on each one also says no
    # contract re-ask ladder was entered for any of them.
    assert [p.get("verdict") for p, _errs in seen] == ["no_verdict"] * 3, seen
    assert [errs for _p, errs in seen] == [[], [], []], seen
    # the scenario parameter is real, not merely requested: the ingested
    # record on disk carries the estimate this docstring claims (a helper
    # that filtered it out left the default 1 here, silently testing a
    # different scenario -- v0.20.1 task 004's own validation finding).
    assert _task_record(root)["estimatedIterations"] == 3, _task_record(root)
    assert _worker_iteration_count(root) == 3
    assert not _verify_ran(root)
    task = _task_record(root)
    assert task["status"] == "in-progress", task
    assert "task 011" not in (task.get("notes") or "")
    assert "awaiting-validation" not in (task.get("notes") or "")
    msgs = _fv_log_messages(root)
    assert "absorbed 3 consecutive worker iterations" not in msgs, msgs
    # the legacy status-key bookkeeping is gone too, not just its effect
    assert _absorption_streak(root) == {}


# ----------------------------------------------------------------------
# Folded from tests/test_worker_dispatch_reentry_task008.py (task 028, requirement D.7).
# ----------------------------------------------------------------------

import asyncio
import json

from test_contract_ladder import TASK_1 as wdr008_TASK_1, _ok as _wdr008_ok, _review_verified as _wdr008_review_verified, _task as _wdr008_task
from test_contract_ladder import _run_resumed_worker_e2e as _wdr008_run_resumed

from ralphd.engine.config import JobConfig
from ralphd.engine.loop import LoopSupervisor
from ralphd.engine.runner import IterationResult
from ralphd.engine.state import RunDir, atomic_write_json


def test_generic_schema_site_repair_counts_the_re_entry_once(tmp_path):
    """Requirement B.2 (#128), the letter of the criterion: after
    `enforcement.ok` at the GENERIC schema site (`elif schema_errors:`,
    reached only when `verdict` matches none of the known enum branches --
    unlike the sibling test above, which never leaves the `"complete"`
    branch's own pre-existing ladder), the dispatch is re-entered with the
    repaired payload EXACTLY once, counted directly on `sup._last_worker_
    dispatch_reentries` -- never merely inferred from the task having
    moved (that inference is `test_a_malformed_complete_report_is_repaired_
    by_one_uncharged_re_ask`'s job, and it exercises a different ladder).
    The first payload's `verdict: "done"` is not a member of the enum at
    all, so it fails schema validation and matches none of `"unsatisfiable"`/
    `"complete"`/`"plan_complete"` -- landing in `elif schema_errors:`,
    task 008's own site -- where the re-ask ladder repairs it into a valid
    `"complete"` report, re-dispatched once into the branch that owns it."""
    root = tmp_path / "run"
    tasks = [_wdr008_task(status="in-progress")]

    malformed = json.dumps({"verdict": "done"})               # invalid enum
    corrected = json.dumps({"verdict": "complete", "task_id": wdr008_TASK_1})

    sup, state = _wdr008_run_resumed(root, {
        "worker": [lambda s, p: _wdr008_ok(malformed), lambda s, p: _wdr008_ok(corrected)],
        "review": [_wdr008_review_verified],
    }, tasks, iterations=3)

    final_tasks = sup.run.read_tasks()["tasks"]
    assert final_tasks[0]["status"] == "completed", final_tasks[0]
    assert sup._last_worker_dispatch_reentries == 1
    assert sup._abort_reason is None
    assert state == "succeeded", sup.run.read_status()


def test_three_consecutive_repaired_completes_never_trip_the_3_strike_guard(
        tmp_path):
    """Requirement B.2 (#128): a repaired `"complete"` must be applied by
    the branch that owns it -- and that application resets the no-progress
    counter the SAME way an ordinary, first-try-valid `"complete"` already
    does (the `stagnant = 0` inside that branch, task008's own re-dispatch
    changes nothing about it). Proven by driving exactly THREE consecutive
    worker passes, each one a malformed-then-repaired `"complete"` for a
    DIFFERENT task -- the same count `stagnant >= 3` would fail the
    approach on if a re-dispatched repair were silently read as three
    passes of silence instead of three passes of real progress."""
    root = tmp_path / "run"
    tasks = [_wdr008_task(id=tid, status="pending") for tid in ("001", "002", "003")]

    def completed(tid):
        return json.dumps({"verdict": "complete", "task_id": tid})

    # genuinely schema-invalid (not merely missing the optional `task_id`)
    # so every pass lands in the generic `elif schema_errors:` site --
    # task 008's own reentry -- rather than the `"complete"` branch's
    # pre-existing (task 006) semantic ladder.
    malformed = json.dumps({"verdict": "done"})

    sup, state = _wdr008_run_resumed(root, {
        "worker": [
            lambda s, p: _wdr008_ok(malformed), lambda s, p: _wdr008_ok(completed("001")),
            lambda s, p: _wdr008_ok(malformed), lambda s, p: _wdr008_ok(completed("002")),
            lambda s, p: _wdr008_ok(malformed), lambda s, p: _wdr008_ok(completed("003")),
        ],
        "review": [_wdr008_review_verified],
    }, tasks, iterations=6)

    final_tasks = {t["id"]: t["status"] for t in sup.run.read_tasks()["tasks"]}
    assert final_tasks == {"001": "completed", "002": "completed",
                            "003": "completed"}, final_tasks

    # the run reached an ordinary review verdict, NOT
    # `stagnation_exhausted`/a replanned approach -- the events log carries
    # no "no task progress" stagnation message anywhere.
    events = [json.loads(x) for x in
              (root / "events.jsonl").read_text().splitlines() if x.strip()]
    assert not any("no task progress" in e.get("message", "") for e in events)
    assert sup._approach == 1, "a stagnation-triggered replan bumps the approach"
    assert sup._last_worker_dispatch_reentries == 1
    assert sup._abort_reason is None
    assert state == "succeeded", sup.run.read_status()


class _AlwaysBogusVerdictAgent:
    """Requirement B.2 (#128)'s other half: a payload the schema rejects
    on EVERY attempt (the re-ask ladder never repairs it), so the generic
    schema site's own `enforce_contract` call genuinely exhausts. Same
    unconditional-repeat shape as
    `test_contract_events.py`'s
    `_RepeatedlyBrokenAgent`, but `"bogus"` is not a member of the
    `verdict` enum at all -- unlike that module's fixture (`verdict:
    "complete"`, invalid `task_id` type), which is caught by the
    `elif verdict == "complete":` branch, not the `elif schema_errors:`
    branch task 008 changed."""

    def __init__(self):
        self.calls = 0

    def interrupt(self) -> bool:
        return False

    async def run(self, prompt, transcript, **kw):
        self.calls += 1
        r = IterationResult(exit_code=0)
        r.final_text = json.dumps({"verdict": "bogus"})
        r.duration_s = 5.0
        r.usage = {"input": 10, "output": 5, "totalTokens": 15}
        return r


def test_schema_site_ladder_exhaustion_still_falls_through_to_stagnation(
        tmp_path):
    """Requirement B.2 (#128): ladder EXHAUSTION at the generic schema
    site must still fall through to the SAME ordinary `stagnation_
    exhausted` bookkeeping every other exhausted site already takes --
    task 008's re-dispatch only fires on `enforcement.ok`, never changing
    what happens when the ladder never repairs. Mirrors `test_contract_
    exhaustion_never_solely_ends_the_run_task009.py`'s own invariant test
    (`sup._step_worker()` driven directly, once), at task 008's own site."""
    root = tmp_path / "run"
    root.mkdir(parents=True)
    run = RunDir(root=root)
    run.update_status(state="running")
    atomic_write_json(run.tasks_file, {"version": 1, "tasks": [_wdr008_task()]})
    cfg = JobConfig(run_id="v013-task008-schema-site-exhaustion",
                    iterations=900, max_approaches=12, vigilant=False,
                    on_complete="exit", infra_retry_backoff_s=[0.0],
                    infra_retry_backoff_max_s=0.0, infra_outage_budget_s=1000.0)
    sup = LoopSupervisor(cfg, run, root)
    sup._approach = 1
    agent = _AlwaysBogusVerdictAgent()
    sup.runner = agent  # type: ignore[assignment]

    outcome = asyncio.run(sup._step_worker())

    assert sup._abort_reason is None, (
        "an exhausted contract ladder must never be the SOLE reason the "
        "run ends while budget and approach remain")
    assert run.read_status().get("state") == "running"
    assert outcome == ("advance", "replan", "stagnation_exhausted")
    # never repaired -- the re-entry counter for the LAST pass stays at 0.
    assert sup._last_worker_dispatch_reentries == 0


# ----------------------------------------------------------------------
# Folded from tests/test_worker_plan_complete_task015.py (task 028, requirement D.7).
# ----------------------------------------------------------------------

import asyncio
import json
from pathlib import Path

from ralphd.engine import contracts
from ralphd.engine.config import JobConfig
from ralphd.engine.loop import LoopSupervisor
from ralphd.engine.runner import IterationResult
from ralphd.engine.state import RunDir, atomic_write_json
from tests.conftest import supervisor_factory

wpc015_TASK_1 = "001"
TASK_2 = "002"


def _wpc015_ok(text: str) -> IterationResult:
    r = IterationResult(exit_code=0)
    r.final_text = text
    r.duration_s = 5.0
    r.usage = {"input": 10, "output": 5, "totalTokens": 15}
    return r


def _wpc015_task(id_=wpc015_TASK_1, **over) -> dict:
    task = {"id": id_, "title": "do the thing",
            "status": "pending", "successCriteria": "the thing works"}
    task.update(over)
    return task


# -- the schema ---------------------------------------------------------


def test_the_schema_accepts_the_bare_plan_complete_verdict():
    assert contracts.validate_payload("worker", {"verdict": "plan_complete"}) == []


def test_the_schema_still_recognises_the_other_three_verdicts():
    for verdict in ("complete", "no_verdict", "unsatisfiable"):
        payload = {"verdict": verdict}
        if verdict == "complete":
            payload["task_id"] = wpc015_TASK_1
        if verdict == "unsatisfiable":
            payload.update(task_id=wpc015_TASK_1, reason="contradictory criteria")
        assert contracts.validate_payload("worker", payload) == [], verdict


def test_the_schema_rejects_plan_complete_misspelled():
    errors = contracts.validate_payload("worker", {"verdict": "plan-complete"})
    assert errors != []


def test_plan_complete_is_a_name_distinct_from_the_per_task_complete():
    # Task 015: deliberately NOT named "complete" -- the per-task claim
    # above already owns that name, so a payload can never be misread as
    # one or the other.
    from ralphd.engine.transitions import HAT_SCHEMAS
    enum = HAT_SCHEMAS["worker"]["properties"]["verdict"]["enum"]
    assert "plan_complete" in enum
    assert enum.count("complete") == 1
    assert enum.count("plan_complete") == 1


# -- _apply_worker_plan_complete_report -----------------------------------


_wpc015_supervisor = supervisor_factory(run_id="v015-plan-complete-unit", cfg_defaults=False,
                                 iterations=5, max_approaches=1, vigilant=False,
                                 on_complete="exit")


def test_a_task_assigned_this_iteration_is_refused_structurally(tmp_path):
    sup = _wpc015_supervisor(tmp_path / "run")
    errors = sup._apply_worker_plan_complete_report(
        {"verdict": "plan_complete"}, wpc015_TASK_1, {"version": 1, "tasks": []})
    assert errors
    assert "only valid when no task was assigned" in errors[0]
    assert repr(wpc015_TASK_1) in errors[0]


def test_nothing_assigned_but_outstanding_work_is_refused_semantically(tmp_path):
    sup = _wpc015_supervisor(tmp_path / "run")
    doc = {"version": 1, "tasks": [_wpc015_task(wpc015_TASK_1, status="completed"),
                                   _wpc015_task(TASK_2, status="pending")]}
    errors = sup._apply_worker_plan_complete_report(None, None, doc)
    assert errors
    assert "not completed or skipped" in errors[0]
    assert TASK_2 in errors[0]
    assert wpc015_TASK_1 not in errors[0]


def test_nothing_assigned_and_nothing_outstanding_is_accepted(tmp_path):
    sup = _wpc015_supervisor(tmp_path / "run")
    doc = {"version": 1, "tasks": [_wpc015_task(wpc015_TASK_1, status="completed"),
                                   _wpc015_task(TASK_2, status="skipped")]}
    assert sup._apply_worker_plan_complete_report(None, None, doc) == []


def test_a_validated_task_counts_the_same_as_completed_or_skipped(tmp_path):
    # `_outstanding_for_completion` recognises `state.TASK_COMPLETION_STATUSES`
    # (task 019, requirement F), which -- unlike worker.md's own prose --
    # already includes the vigilant mini-FSM's `validated` terminal.
    sup = _wpc015_supervisor(tmp_path / "run")
    doc = {"version": 1, "tasks": [_wpc015_task(wpc015_TASK_1, status="validated")]}
    assert sup._apply_worker_plan_complete_report(None, None, doc) == []


def test_a_failed_task_blocks_the_claim_forever(tmp_path):
    sup = _wpc015_supervisor(tmp_path / "run")
    doc = {"version": 1, "tasks": [_wpc015_task(wpc015_TASK_1, status="failed")]}
    errors = sup._apply_worker_plan_complete_report(None, None, doc)
    assert errors and wpc015_TASK_1 in errors[0] and "failed" in errors[0]


def test_an_empty_or_unreadable_plan_is_accepted(tmp_path):
    """Refusing here would strand a run with no plan at all in the
    stagnation guard with no reviewer ever looking at it -- matches
    `_outstanding_for_completion`'s own fail-open contract."""
    sup = _wpc015_supervisor(tmp_path / "run")
    assert sup._apply_worker_plan_complete_report(None, None, {}) == []


# -- the ladder: a malformed/refused attempt is never silent -------------


class _wpc015_ScriptedAgent:
    """Same shape as `tests/test_worker_verdict_schema_error_task007.py`'s:
    one script per phase, dispatched by the phase status.json records --
    a contract-fix re-ask lands on the SAME phase ("worker" never changes
    during one), so it hits the SAME step list, one call further in."""

    def __init__(self, handlers: dict):
        self.handlers = handlers
        self.calls: dict[str, int] = {}
        self.running = False

    def interrupt(self) -> bool:
        return self.running

    async def run(self, prompt, transcript, **kw) -> IterationResult:
        phase = self.sup.run.read_status().get("phase")
        n = self.calls.get(phase, 0)
        self.calls[phase] = n + 1
        steps = self.handlers[phase]
        self.running = True
        try:
            return steps[min(n, len(steps) - 1)](self.sup, prompt)
        finally:
            self.running = False


def _wpc015_run_resumed(root: Path, handlers: dict, tasks: list[dict],
                 **cfg_kw) -> tuple[LoopSupervisor, str]:
    RunDir(root=root).update_status(state="starting")
    root.mkdir(parents=True, exist_ok=True)
    run = RunDir(root=root)
    kw = {"iterations": 4, "max_approaches": 1, "vigilant": False,
          "on_complete": "exit", "infra_retry_backoff_s": [0.0],
          "infra_retry_backoff_max_s": 0.0, "infra_outage_budget_s": 1000.0,
          **cfg_kw}
    sup = LoopSupervisor(JobConfig(run_id="v015-plan-complete-ladder", **kw),
                         run, root)
    agent = _wpc015_ScriptedAgent(handlers)
    agent.sup = sup                      # type: ignore[attr-defined]
    sup.runner = agent                    # type: ignore[assignment]

    async def no_backoff(seconds):
        return seconds, False

    sup._wait_out_backoff = no_backoff    # type: ignore[method-assign]
    atomic_write_json(sup.run.tasks_file, {"version": 1, "tasks": tasks})
    sup.iterations_used = 1
    return sup, asyncio.run(sup._run_job_core())


def _wpc015_events(root: Path) -> list[dict]:
    p = root / "events.jsonl"
    if not p.exists():
        return []
    return [json.loads(x) for x in p.read_text().splitlines() if x.strip()]


def test_an_unearned_plan_complete_claim_exhausts_the_ladder_and_is_refused(tmp_path):
    """A worker with an assigned task claiming the WHOLE plan is done in the
    same breath -- structurally illegal -- is carried back through
    `contract_enforcement.enforce_contract`'s ladder, never read as a
    silent no-progress iteration the retired marker's own bare-log refusal
    used to be. The move is refused (the task record never moves off
    `pending`); the ladder runs the FULL `contract_retry_max + 1` attempts;
    and the run continues per the table (the undeclared `worker` self-loop)
    rather than aborting outright."""
    root = tmp_path / "run"
    tasks = [_wpc015_task(wpc015_TASK_1, status="pending")]

    def claim_it(sup, prompt):
        return _wpc015_ok("nothing to do here\n" + json.dumps({"verdict": "plan_complete"}))

    sup, state = _wpc015_run_resumed(root, {"worker": [claim_it]}, tasks, iterations=2)

    task = sup.run.read_tasks()["tasks"][0]
    assert task["status"] == "in-progress", \
        "a contract violation must not silently take the move anyway"

    assert sup.runner.calls["worker"] == sup.cfg.contract_retry_max + 1  # type: ignore[attr-defined]

    events = _wpc015_events(root)
    violation_events = [ev for ev in events if ev.get("type") == "contract.violation"]
    fault_events = [ev for ev in events if ev.get("type") == "contract.agent_fault"]
    assert len(violation_events) == sup.cfg.contract_retry_max + 1
    assert len(fault_events) == 1, "the ladder must be exhausted exactly once"
    all_errors = " ".join(" ".join(ev.get("errors", [])) for ev in violation_events)
    assert "only valid when no task was assigned" in all_errors, all_errors

    assert sup._abort_reason is None
    assert not [ev for ev in events if ev.get("type") == "signal"
                and ev.get("signal") == "COMPLETE"], \
        "an unearned plan_complete is never signalled COMPLETE"
    assert state == "failed", sup.run.read_status()


def test_a_schema_malformed_plan_complete_line_also_reaches_the_ladder(tmp_path):
    """Not just the semantic refusal above -- a `plan_complete` payload
    the schema itself would already reject on shape (an unexpected extra
    field the closed schema forbids) reaches the SAME ladder as the
    generic `elif schema_errors:` site (v0.13 task 007), never a
    special-cased abort for this one new value."""
    root = tmp_path / "run"
    tasks = [_wpc015_task(wpc015_TASK_1, status="pending")]

    def bad_shape(sup, prompt):
        payload = json.dumps({"verdict": "plan_complete", "bogus_field": True})
        return _wpc015_ok("all done\n" + payload)

    sup, state = _wpc015_run_resumed(root, {"worker": [bad_shape]}, tasks, iterations=2)

    assert sup.runner.calls["worker"] == sup.cfg.contract_retry_max + 1  # type: ignore[attr-defined]
    events = _wpc015_events(root)
    fault_events = [ev for ev in events if ev.get("type") == "contract.agent_fault"]
    assert len(fault_events) == 1
    assert not [ev for ev in events if ev.get("type") == "signal"
                and ev.get("signal") == "COMPLETE"]
    assert state == "failed", sup.run.read_status()


def test_an_earned_plan_complete_after_a_refused_attempt_still_ends_the_run(tmp_path):
    """The ladder's re-ask channel gets a genuine second chance: the SAME
    worker corrects itself on the re-ask (`_ask_contract_fix`'s reply) and
    the claim is honoured -- `contract_enforcement.enforce_contract`
    treats a corrected payload exactly like a first-try success."""
    root = tmp_path / "run"
    tasks = [_wpc015_task(wpc015_TASK_1, status="completed")]  # nothing outstanding

    calls = {"n": 0}

    def flaky_then_fixed(sup, prompt):
        calls["n"] += 1
        if calls["n"] == 1:
            # the real dispatch: a schema-invalid `plan_complete` (an
            # unexpected extra field) -- attempt 0 of the ladder reuses
            # this text verbatim and fails.
            return _wpc015_ok(json.dumps({"verdict": "plan_complete", "oops": 1}))
        # the `_ask_contract_fix` re-ask's reply: corrected.
        return _wpc015_ok(json.dumps({"verdict": "plan_complete"}))

    sup, state = _wpc015_run_resumed(root, {
        "worker": [flaky_then_fixed],
        "review": [lambda s, p: _wpc015_ok(json.dumps({"verdict": "verified"}))],
    }, tasks)

    events = _wpc015_events(root)
    assert [ev for ev in events if ev.get("type") == "contract.violation"], \
        "the first, malformed attempt is still on the record"
    assert not [ev for ev in events if ev.get("type") == "contract.agent_fault"], \
        "the re-ask fixed it before the ladder exhausted"
    assert [ev for ev in events
            if ev.get("type") == "signal" and ev.get("signal") == "COMPLETE"], \
        "the corrected claim is honoured and still signals COMPLETE"
    assert (state, sup.run.read_status()["verdict"]) == ("succeeded", "verified")


# ----------------------------------------------------------------------
# Folded from tests/test_worker_retry_rejection_prompt_task015.py (task 028, requirement D.7).
# ----------------------------------------------------------------------

import re
from pathlib import Path

from ralphd.engine.config import JobConfig
from ralphd.engine.loop import LoopSupervisor
from ralphd.engine.state import RunDir
from tests.conftest import supervisor_factory

wrrp015_REPO_ROOT = Path(__file__).resolve().parents[1]
WORKER = wrrp015_REPO_ROOT / "src" / "ralphd" / "prompts" / "worker.md"

HEADING = "## Why the previous attempt was rejected"
NOTES = ("Missing evidence for criterion 2 -- the log file cited was never "
         "actually produced by the command run.")


# The shared harness (tests/conftest.py, G.1): this module's run dir and
# workspace sit side by side under the test's own tmp_path.
_wrrp015_supervisor = supervisor_factory(run_id="task015-retry-rejection", run_root="run",
                                workspace="ws")



def _wrrp015_task(**overrides) -> dict:
    task = {
        "id": "042",
        "title": "Some task",
        "status": "in-progress",
        "successCriteria": "Some criteria.",
    }
    task.update(overrides)
    return task


# ---------------------------------------------------------------------------
# In-process cases.
# ---------------------------------------------------------------------------

def test_retry_dispatch_carries_attempts_and_notes_verbatim(tmp_path):
    sup = _wrrp015_supervisor(tmp_path)
    task = _wrrp015_task(validationAttempts=2, validationNotes=NOTES)
    extra = sup._worker_assignment_extra(task)
    assert HEADING in extra
    assert "2" in extra.split(HEADING, 1)[1]
    # verbatim: the exact notes string appears unmangled in the block
    assert NOTES in extra


def test_first_dispatch_has_no_rejection_heading_zero_attempts(tmp_path):
    sup = _wrrp015_supervisor(tmp_path)
    task = _wrrp015_task(validationAttempts=0, validationNotes=NOTES)
    extra = sup._worker_assignment_extra(task)
    assert HEADING not in extra


def test_first_dispatch_has_no_rejection_heading_missing_key(tmp_path):
    sup = _wrrp015_supervisor(tmp_path)
    task = _wrrp015_task()
    assert "validationAttempts" not in task
    extra = sup._worker_assignment_extra(task)
    assert HEADING not in extra


def test_no_task_selectable_has_no_rejection_heading(tmp_path):
    sup = _wrrp015_supervisor(tmp_path)
    extra = sup._worker_assignment_extra(None)
    assert HEADING not in extra
    assert "No task is currently selectable" in extra


# ---------------------------------------------------------------------------
# Lexical check of worker.md.
# ---------------------------------------------------------------------------

def _worker_text() -> str:
    assert WORKER.is_file(), f"missing prompt file {WORKER}"
    return WORKER.read_text()


def test_worker_md_tells_the_worker_to_address_notes_or_petition():
    text = _worker_text()
    assert re.search(r"Why the previous attempt was rejected", text), (
        "worker.md does not mention the "
        "'## Why the previous attempt was rejected' block by name")
    assert re.search(r"validationNotes", text), (
        "worker.md does not reference validationNotes")
    assert re.search(r"petition|unsatisfiable", text, re.IGNORECASE), (
        "worker.md does not tell the worker it may petition or report "
        "unsatisfiable instead of blindly re-attempting")


# ----------------------------------------------------------------------
# Folded from tests/test_interrupt_not_substantive_outcome_task004.py (task 028, requirement D.7).
# ----------------------------------------------------------------------

import asyncio
import copy
import json

import pytest

from ralphd.engine import transitions as tr
from ralphd.engine.config import JobConfig
from ralphd.engine.loop import LoopSupervisor
from ralphd.engine.runner import IterationResult
from ralphd.engine.state import RunDir, read_events
from tests.conftest import supervisor_factory, stub_contract_reask

TEST_STATE = "task004_audit_hat_state"
TEST_HAT = "task004_audit_hat"


# ---------------------------------------------------------------------------
# shared scaffolding
# ---------------------------------------------------------------------------

def _inso004_ok(text: str = "{}") -> IterationResult:
    r = IterationResult(exit_code=0)
    r.final_text = text
    r.duration_s = 30.0
    r.usage = {"input": 10, "output": 5, "totalTokens": 15}
    return r


def _interrupted(text: str = "partial output before the cutoff") -> IterationResult:
    """The engine's own cutoff shape (fact 9): a signal-terminated attempt
    that DID produce traffic, so `classify_fault` scores it `"signal"`
    (never `"infra"`, never retried by `_run_iteration_with_infra_retry`)
    and the caller under test is the only thing left to discriminate it
    from a genuine reply."""
    r = IterationResult(exit_code=None, interrupted=True)
    r.final_text = text
    r.usage = {"input": 3, "output": 1, "totalTokens": 4}
    r.duration_s = 5.0
    return r


_inso004_supervisor = supervisor_factory(run_id="task004-audit", infra_retry_backoff_s=[0.0],
                                 infra_retry_backoff_max_s=0.0, infra_outage_budget_s=1000.0,
                                 vigilant=False, max_approaches=2, on_complete="exit")


def _stub_attempts(sup: LoopSupervisor, results: list[IterationResult]) -> list[str]:
    """Same technique as tests/test_instant_infra_retry.py's `_stub_attempts`:
    stub `_run_iteration_once` (below the infra-retry wrapper, so the real
    wrapper's own infra-vs-not decision still runs) and record which phase
    was asked, in order.

    Steer 003's finding: `_run_iteration_once` is the ONLY place
    `self.iterations_used` moves (loop.py:1055/1060, `n =
    self.iterations_used + 1` ... `self.iterations_used = n`), and
    `budget_left()` (loop.py:1449-1456) is computed entirely from that
    counter. A stub that replaces the method wholesale but never touches
    the counter leaves `budget_left()` permanently True, so any retry
    loop keyed off it (`_step_generic_hat`'s `while True`,
    `_one_planning_attempt`'s) spins forever instead of ever reaching its
    budget-exhausted branch -- this is exactly what hung iteration 14 for
    95 minutes on tests 2/5/6b below. Faithful to what it stands in for:
    charge the counter on every call, same as the real method does."""
    calls: list[str] = []

    async def fake_once(phase, extra="", prompt_name=None, tier=None, task_id=None):
        calls.append(phase)
        sup.iterations_used += 1
        return copy.copy(results[min(len(calls) - 1, len(results) - 1)])

    sup._run_iteration_once = fake_once  # type: ignore[method-assign]
    return calls


@pytest.fixture
def registered_test_hat():
    """A trivial single-edge hat, registered/torn down the same way
    tests/test_hat_extensibility.py's `registered_noop_hat` does, minus the
    `ENTRY_STATE`/prompt-file bookkeeping this module's direct-call tests
    never need (nothing here goes through `_run_job_core`'s dispatch
    entry -- `_step_generic_hat` is called directly with an explicit
    `state`/`edges` pair)."""
    assert TEST_STATE not in tr.TABLE, "test fixture name collided with a real state"
    tr.TABLE[TEST_STATE] = (tr.T("planning", "task004_audit_done"),)
    tr.STATE_HAT[TEST_STATE] = TEST_HAT
    tr.HAT_SCHEMAS[TEST_HAT] = {
        "type": "object", "properties": {}, "additionalProperties": False}
    try:
        yield TEST_STATE
    finally:
        tr.TABLE.pop(TEST_STATE, None)
        tr.STATE_HAT.pop(TEST_STATE, None)
        tr.HAT_SCHEMAS.pop(TEST_HAT, None)


def _inso004_log_messages(sup: LoopSupervisor) -> str:
    return "\n".join(
        e.get("message") or "" for e in read_events(sup.run.root, types=("log",)))


# ---------------------------------------------------------------------------
# 1-3. `_step_generic_hat`
# ---------------------------------------------------------------------------

def test_1_generic_hat_retries_in_place_when_interrupted_with_budget_left(
        tmp_path, registered_test_hat):
    sup = _inso004_supervisor(tmp_path, iterations=5)
    calls = _stub_attempts(sup, [_interrupted(), _inso004_ok("{}")])
    edges = tr.successors(TEST_STATE, sup.cfg.vigilant)

    outcome = asyncio.run(sup._step_generic_hat(TEST_STATE, edges))

    assert calls == [TEST_STATE, TEST_STATE]  # retried once, in place
    assert outcome == ("advance", "planning", "task004_audit_done")
    assert sup._abort_reason is None
    assert "contract violation" not in _inso004_log_messages(sup)


def test_2_generic_hat_interrupted_at_budget_end_is_not_a_contract_violation(
        tmp_path, registered_test_hat):
    sup = _inso004_supervisor(tmp_path, iterations=1)  # exhausted after one attempt
    calls = _stub_attempts(sup, [_interrupted()])
    edges = tr.successors(TEST_STATE, sup.cfg.vigilant)

    outcome = asyncio.run(sup._step_generic_hat(TEST_STATE, edges))

    assert calls == [TEST_STATE]
    assert outcome == ("tail",)
    # THE regression this guards: before the fix, this exact scenario set
    # `_abort_reason` to a "contract violation" message and the run ended
    # aborted/failed over an engine-side cutoff nobody committed.
    assert sup._abort_reason is None
    assert "contract violation" not in _inso004_log_messages(sup)
    assert sup._terminal_reason_note is not None
    assert "cut short" in sup._terminal_reason_note
    # The note itself SAYS "not ... a contract violation" (that is the
    # point being recorded) -- assert the denial, not a bare substring
    # absence that would flag the note's own honest phrasing as a defect.
    assert "not read as a contract violation" in sup._terminal_reason_note


def test_3_mutation_stops_discriminating_generic_hat_reads_interrupt_as_contract_violation(
        tmp_path, registered_test_hat, monkeypatch):
    """The paired mutation: undo the fix (make the discrimination always
    say "this was not a cutoff") and show the SAME scenario as test 2 now
    DOES set `_abort_reason` to a contract-violation message -- proving
    test 2 actually exercises the discrimination rather than passing by
    construction."""
    monkeypatch.setattr(
        LoopSupervisor, "_iteration_cut_short_by_engine",
        staticmethod(lambda result: False))
    sup = _inso004_supervisor(tmp_path, iterations=1)
    calls = _stub_attempts(sup, [_interrupted()])
    # With the discrimination mutated away, the interrupted attempt is read as
    # a real reply and enters the contract ladder, whose re-ask channel is
    # `_ask_contract_fix` -- not `_run_iteration_once`, so `_stub_attempts`
    # does not cover it. Left unstubbed it spawns the real `pi` binary twice
    # (`contract_retry_max` = 2), each bounded only by `router_timeout_s` =
    # 60 s: 120 s of real waiting to learn what the empty reply below says
    # instantly, with the ladder still exhausting exactly as before (task
    # 050, G.2).
    stub_contract_reask(sup)
    edges = tr.successors(TEST_STATE, sup.cfg.vigilant)

    outcome = asyncio.run(sup._step_generic_hat(TEST_STATE, edges))

    assert calls == [TEST_STATE]
    assert outcome == ("tail",)
    assert sup._abort_reason is not None
    assert "contract violation" in sup._abort_reason


# ---------------------------------------------------------------------------
# 4-6. `_one_planning_attempt`
# ---------------------------------------------------------------------------

def test_4_one_planning_attempt_retries_in_place_when_interrupted_with_budget_left(
        tmp_path):
    sup = _inso004_supervisor(tmp_path, iterations=5)
    calls = _stub_attempts(sup, [_interrupted(), _inso004_ok("planned\n{}")])

    presult = asyncio.run(sup._one_planning_attempt(""))

    assert calls == ["planning", "planning"]
    assert presult is not None
    assert presult.interrupted is False
    assert presult.final_text == "planned\n{}"


def test_5_one_planning_attempt_falls_through_only_once_budget_is_actually_gone(
        tmp_path):
    """Once the run's own budget is exhausted (not `enforce_plan_ingestion`'s
    retry budget -- this method's caller's business, tested at the
    `_step_planning` level in test 6), the interrupted result is finally
    handed back rather than retried forever."""
    sup = _inso004_supervisor(tmp_path, iterations=1)
    calls = _stub_attempts(sup, [_interrupted()])

    presult = asyncio.run(sup._one_planning_attempt(""))

    assert calls == ["planning"]
    assert presult is not None
    assert presult.interrupted is True


def test_6_mutation_stops_discriminating_planning_burns_a_contract_retry_on_a_cutoff(
        tmp_path, monkeypatch):
    """The paired mutation: with the discrimination removed, an interrupted
    attempt is handed straight to the caller on the FIRST try even though
    budget remains -- the exact defect this task's fix removes (it would
    otherwise burn one of `plan_ingestion.enforce_plan_ingestion`'s finite
    retries on an attempt the model never got a fair shot at)."""
    monkeypatch.setattr(
        LoopSupervisor, "_iteration_cut_short_by_engine",
        staticmethod(lambda result: False))
    sup = _inso004_supervisor(tmp_path, iterations=5)
    calls = _stub_attempts(sup, [_interrupted(), _inso004_ok("planned\n{}")])

    presult = asyncio.run(sup._one_planning_attempt(""))

    # Without the fix, only ONE attempt is made (the interrupted one is
    # handed straight back) even though 4 more iterations of budget remain.
    assert calls == ["planning"]
    assert presult is not None
    assert presult.interrupted is True


def test_6b_step_planning_interrupted_at_budget_end_does_not_cost_the_approach(
        tmp_path):
    """End-to-end at the `_step_planning` level: budget exhausted while
    planning kept getting cut short by the engine must not read as
    "planning produced no usable plan proposal" (the `empty_plan` edge,
    which advances to `replan` and would consume this approach) -- it
    must fall straight to the ordinary tail, exactly like `_step_planning`
    already does for the pre-existing "budget ran out mid-ingestion"
    case, and the approach counter must not move."""
    sup = _inso004_supervisor(tmp_path, iterations=1)
    # `_step_planning` reads `self._approach` (only set for real by
    # `_run_job_core`, loop.py:2163) to stamp `status.json`'s `approach`
    # field -- this test drives `_step_planning` directly, so it has to
    # set the same thing `_run_job_core` would have.
    sup._approach = 1
    calls = _stub_attempts(sup, [_interrupted()])

    outcome = asyncio.run(sup._step_planning(skip_planning=False))

    assert calls == ["planning"]
    assert outcome == ("tail",)
    assert sup._abort_reason is None
    assert "planning produced no usable plan proposal" not in _inso004_log_messages(sup)
    assert sup.run.read_status().get("approach") == 1


# ---------------------------------------------------------------------------
# 7. `_step_worker`'s stagnation counter (already correct -- pinned, not
#    fixed): the residual case
#    tests/test_stagnation_excludes_nonwork_faults.py does not reach --
#    `result.interrupted=True` with `_last_fault_class == "work"` because
#    THIS attempt's own `operator_abort_requested` was true (a
#    per-iteration `POST /interrupt`, not a run-ending abort), so the
#    earlier `_last_fault_class in ("infra", "signal")` gate does NOT
#    short-circuit before the stagnation line -- only the explicit
#    `and not result.interrupted` clause does.
# ---------------------------------------------------------------------------

def test_7_stagnation_counter_excludes_an_operator_interrupted_work_classified_iteration(
        tmp_path):
    # `_steps`/`_write_proposal` are defined in this module since the fold
    # absorbed test_failed_task_vocabulary.py (task 028, requirement D.7).

    def _plan(sup, prompt=""):
        _write_proposal(sup, [{"id": "001", "title": "do the thing",
                              "successCriteria": "the thing is done"}])
        return _inso004_ok("planned\n" + json.dumps(
            {"proposal_path": "plan-proposal.json", "summary": "planned"}))

    def _operator_interrupted_worker(sup, prompt=""):
        # A per-iteration POST /interrupt, delivered while this attempt was
        # running: `operator_abort_requested` reads True for THIS attempt's
        # `_classify_result` (`_abort_reason` stays None -- no run-ending
        # abort is recorded), so `classify_fault`'s operator-abort carve-out
        # scores it `"work"`, not `"signal"`/`"infra"` -- the ONLY thing
        # left to exclude it from stagnation is `result.interrupted` itself.
        sup._operator_interrupted = True
        r = IterationResult(exit_code=None, interrupted=True)
        r.final_text = "cut short mid-task by a per-iteration interrupt"
        r.usage = {"input": 4, "output": 2, "totalTokens": 6}
        r.duration_s = 12.0
        return r

    class _Runner:
        def __init__(self, sup, handlers):
            self.sup = sup
            self.handlers = handlers

        def interrupt(self):
            return False

        async def run(self, prompt, transcript, **kw):
            phase = self.sup.run.read_status().get("phase")
            return self.handlers[phase](self.sup, prompt)

    root = tmp_path / "run"
    root.mkdir()
    run = RunDir(root=root)
    run.update_status(state="starting")
    sup = _inso004_supervisor(root, iterations=4, max_approaches=1)
    handlers = {"planning": _plan,
               "worker": _steps(_operator_interrupted_worker,
                                _operator_interrupted_worker,
                                _operator_interrupted_worker)}
    sup.run = run
    sup.runner = _Runner(sup, handlers)  # type: ignore[assignment]

    async def no_backoff(seconds):
        return seconds, False

    sup._wait_out_backoff = no_backoff  # type: ignore[method-assign]

    state = asyncio.run(sup._run_job_core())

    msgs = _inso004_log_messages(sup)
    assert "3 iterations with no task progress" not in msgs, msgs
    assert "failing approach" not in msgs, msgs
    faults = [ev.get("faultClass") for ev in read_events(sup.run.root)
             if ev.get("type") == "iteration.end" and ev.get("phase") == "worker"]
    assert faults and set(faults) == {"work"}, faults
    # Budget ran out mid-worker-loop (4 iterations: 1 planning + 3 worker),
    # none of which counted as stagnation evidence -- the run ends
    # unverified, not `succeeded`, and NOT via the stagnation guard.
    assert state == "failed"
    assert sup.run.read_status()["verdict"] == "unverified"


# ---------------------------------------------------------------------------
# 8. the reflect path (already correct -- pinned, not fixed): an
#    interrupted reflect attempt is reported honestly (`reflect.ok=False`)
#    but cannot rewrite the run's OWN already-settled `state`/`verdict` --
#    the thing that would make it a "substantive outcome" misread in the
#    requirement A sense.
# ---------------------------------------------------------------------------

def test_8_reflect_interrupted_records_failure_without_touching_the_run_outcome(
        tmp_path):
    sup = _inso004_supervisor(tmp_path, iterations=10)
    sup.run.update_status(state="succeeded", verdict="verified")
    _stub_attempts(sup, [_interrupted("cut short during reflection")])

    asyncio.run(sup._run_reflection())

    status = sup.run.read_status()
    # the run's own settled outcome is untouched
    assert status["state"] == "succeeded"
    assert status["verdict"] == "verified"
    # reflect's own (accurate) verdict is recorded separately
    assert status["reflect"]["ok"] is False
    assert "interrupted" in status["reflect"]["error"]
    assert (sup.run.artifacts_dir / "reflection" / "FAILED.md").exists()


# ----------------------------------------------------------------------
# Folded from tests/test_failed_to_skipped_dependent_doom_task011.py (task 028, requirement D.7).
# ----------------------------------------------------------------------

import asyncio
import json

import pytest

from ralphd.engine import contracts
from ralphd.engine import state
from ralphd.engine import transitions as tr
from ralphd.engine.config import JobConfig
from ralphd.engine.loop import LoopSupervisor
from ralphd.engine.runner import IterationResult
from ralphd.engine.state import RunDir, atomic_write_json
from tests.conftest import make_supervisor

TASK_FAILED = "010"
TASK_DEPENDENT = "011"
TASK_ASSIGNED = "012"
JUSTIFICATION = ("criteria required a live third-party endpoint this sandbox "
                 "cannot reach; the parser half landed in abc1234, the live "
                 "probe did not land at all")


def _tasks() -> list[dict]:
    return [
        # permanently failed: validation attempts exhausted
        {"id": TASK_FAILED, "title": "reach the unreachable",
         "status": "failed", "successCriteria": "reach the unreachable",
         "validationAttempts": 3},
        # doomed by the one above, before AND after the escape
        {"id": TASK_DEPENDENT, "title": "build on top of it",
         "status": "pending", "successCriteria": "build on 010",
         "dependsOn": [TASK_FAILED]},
        # the actionable task the scheduler actually assigns this pass, so
        # the worker gets a real iteration instead of `select_next_task`
        # returning None and the doomed-plan routing firing first
        {"id": TASK_ASSIGNED, "title": "unrelated work",
         "status": "pending", "successCriteria": "the unrelated thing works"},
    ]


def _payload(task_id: str = TASK_FAILED, justification: str | None = JUSTIFICATION,
             verdict: str = "no_verdict") -> dict:
    """Exactly the shape worker.md instructs: the status report riding
    ALONGSIDE an ordinary verdict in the same JSON reply."""
    p: dict = {"verdict": verdict, "task_status": "skipped", "task_id": task_id}
    if justification is not None:
        p["justification"] = justification
    return p


class _SequencedRunner:
    """One `IterationResult` per call, in order, last one repeating -- the
    charged worker call and any uncharged `_ask_contract_fix` re-ask that
    follows it need to be countable and separately answerable (task 001's
    `tests/test_verify_contract_ladder_task001.py` pattern)."""

    def __init__(self, texts):
        self.texts = list(texts)
        self.calls = 0
        self.running = False

    def interrupt(self) -> bool:
        return self.running

    async def run(self, prompt, transcript, **kw) -> IterationResult:
        idx = min(self.calls, len(self.texts) - 1)
        self.calls += 1
        r = IterationResult(exit_code=0, duration_s=0.01)
        r.final_text = self.texts[idx]
        r.usage = {"input": 1, "output": 1, "totalTokens": 2}
        return r


_ftsd011_supervisor = lambda tmp_path, texts, vigilant, **cfg: make_supervisor(
    tmp_path, **{"run_id": f'task011-escape-{vigilant}', "run_root": "run", "workspace": "run",
    "status": {"state": "running"}, "iterations": 1, "max_approaches": 1,
    "vigilant": vigilant, "on_complete": "exit", "infra_retry_backoff_s": [0.0],
    "infra_retry_backoff_max_s": 0.0, "infra_outage_budget_s": 0.0, "tasks": _tasks(),
    "runner_factory": lambda sup: _SequencedRunner(texts), **cfg})


# ---------------------------------------------------------------------------
# the schema the shipped prompt's payload has to satisfy
# ---------------------------------------------------------------------------

def test_the_worker_schema_declares_the_two_optional_escape_fields():
    schema = tr.HAT_SCHEMAS["worker"]
    assert schema["properties"]["task_status"] == {"type": "string"}
    assert schema["properties"]["justification"] == {"type": "string"}
    assert "task_status" not in schema["required"]
    assert "justification" not in schema["required"]
    # the escape payload worker.md instructs validates clean, verdict and all
    assert contracts.validate_payload("worker", _payload()) == []


# ---------------------------------------------------------------------------
# 1-3: the escape, end to end, with the dependent present
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("vigilant", [True, False])
def test_failed_to_skipped_travels_the_worker_channel_with_a_dependent_present(
        tmp_path, vigilant):
    sup = _ftsd011_supervisor(tmp_path, [json.dumps(_payload())], vigilant)
    outcome = asyncio.run(sup._step_worker())

    # -- one approach, one charged iteration, first attempt accepted: no
    #    re-ask ladder ran at all --------------------------------------
    assert sup.iterations_used == 1
    assert sup.runner.calls == 1
    # budget spent with work still outstanding -> the ordinary tail, never
    # an abort and never a replan on this pass
    assert outcome == ("tail",)
    assert sup._abort_reason is None

    tasks_after = sup.run.read_tasks()["tasks"]
    by_id = {t["id"]: t for t in tasks_after}

    # -- the move landed, and is legal against the declared table for the
    #    variant actually in force --------------------------------------
    assert by_id[TASK_FAILED]["status"] == "skipped"
    table = tr.task_status_table_for(vigilant)
    assert "skipped" in table["failed"]
    assert tr.is_legal_task_status_transition("failed", "skipped", vigilant)
    for t in tasks_after:
        assert t["status"] in table

    # -- the justification landed in the task's own `notes` --------------
    assert by_id[TASK_FAILED]["notes"] == JUSTIFICATION
    # the consumed attempts are part of the record, untouched
    assert by_id[TASK_FAILED]["validationAttempts"] == 3

    # -- dependency doom is NOT cleared: the dependent is doomed by the
    #    SAME ancestor id both before and after the move ----------------
    before_blocked = state.task_blocked_by(_tasks())
    after_blocked = state.task_blocked_by(tasks_after)
    assert before_blocked[TASK_DEPENDENT] == [TASK_FAILED]
    assert after_blocked[TASK_DEPENDENT] == [TASK_FAILED]
    assert by_id[TASK_DEPENDENT]["status"] == "pending"
    assert "skipped" not in state.TASK_DEPENDENCY_SATISFIED_STATUSES
    assert "failed" not in state.TASK_DEPENDENCY_SATISFIED_STATUSES

    # -- the assigned task's own status is the harness's business and was
    #    not touched by the escape ---------------------------------------
    assert by_id[TASK_ASSIGNED]["status"] == "in-progress"

    # -- a task event was emitted for the move ---------------------------
    events = [e for e in state.read_events(sup.run.root) if e.get("type") == "task"]
    assert any(e.get("taskId") == TASK_FAILED and e.get("newStatus") == "skipped"
              for e in events)


# ---------------------------------------------------------------------------
# the two refusals, driven through the same production path
# ---------------------------------------------------------------------------

def test_a_report_without_a_justification_is_re_asked_and_never_applied(tmp_path):
    unjustified = json.dumps(_payload(justification=None))
    sup = _ftsd011_supervisor(tmp_path, [unjustified], vigilant=False)
    asyncio.run(sup._step_worker())

    # the ladder asked for a fix `contract_retry_max` times and never got
    # one: charged call + every uncharged re-ask, exactly task 001's bound
    assert sup.runner.calls == 1 + sup.cfg.contract_retry_max
    assert sup.iterations_used == 1

    by_id = {t["id"]: t for t in sup.run.read_tasks()["tasks"]}
    assert by_id[TASK_FAILED]["status"] == "failed"
    assert "notes" not in by_id[TASK_FAILED]


def test_a_report_naming_the_assigned_task_is_re_asked_and_never_applied(tmp_path):
    wrong_target = json.dumps(_payload(task_id=TASK_ASSIGNED))
    sup = _ftsd011_supervisor(tmp_path, [wrong_target], vigilant=False)
    asyncio.run(sup._step_worker())

    assert sup.runner.calls == 1 + sup.cfg.contract_retry_max
    by_id = {t["id"]: t for t in sup.run.read_tasks()["tasks"]}
    # the assigned task keeps the status the HARNESS wrote for it
    assert by_id[TASK_ASSIGNED]["status"] == "in-progress"
    assert by_id[TASK_FAILED]["status"] == "failed"


def test_a_repaired_report_is_applied_on_the_uncharged_reask(tmp_path):
    sup = _ftsd011_supervisor(tmp_path, [json.dumps(_payload(justification=None)),
                                 json.dumps(_payload())], vigilant=False)
    asyncio.run(sup._step_worker())

    assert sup.runner.calls == 2          # charged call + one re-ask
    assert sup.iterations_used == 1       # the re-ask charges no iteration
    by_id = {t["id"]: t for t in sup.run.read_tasks()["tasks"]}
    assert by_id[TASK_FAILED]["status"] == "skipped"
    assert by_id[TASK_FAILED]["notes"] == JUSTIFICATION


# ----------------------------------------------------------------------
# Folded from tests/test_failed_to_skipped_status_task009.py (task 028, requirement D.7).
# ----------------------------------------------------------------------

import json
from pathlib import Path

from ralphd.engine import transitions as tr
from ralphd.engine.config import JobConfig
from ralphd.engine.loop import LoopSupervisor
from ralphd.engine.state import TASK_DEPENDENCY_SATISFIED_STATUSES, RunDir


def _sup(tmp_path: Path, tasks: list[dict]) -> LoopSupervisor:
    run = RunDir(root=tmp_path / "run")
    run.tasks_file.write_text(json.dumps({"version": 1, "tasks": tasks}))
    cfg = JobConfig(run_id="task009", iterations=1, max_approaches=1,
                    vigilant=True, on_complete="exit",
                    infra_retry_backoff_s=[0.0], infra_retry_backoff_max_s=0.0,
                    infra_outage_budget_s=1000.0)
    return LoopSupervisor(cfg, run, tmp_path / "workspace")


# -- 1. declared in both variants -------------------------------------------

def test_failed_to_skipped_declared_in_the_base_table():
    assert tr.TASK_STATUS_TABLE["failed"] == ("skipped",)


def test_failed_to_skipped_declared_in_both_composed_variants():
    assert tr.task_status_table_for(True)["failed"] == ("skipped",)
    assert tr.task_status_table_for(False)["failed"] == ("skipped",)
    # not part of the vigilant delta -- it is inherited from the base
    # table unchanged, same as every other terminal-shaped entry that does
    # not differ across variants.
    assert "failed" not in tr.TASK_STATUS_VIGILANT_DELTA


def test_failed_to_skipped_is_legal_in_both_variants():
    assert tr.is_legal_task_status_transition("failed", "skipped", True)
    assert tr.is_legal_task_status_transition("failed", "skipped", False)


# -- 2. the semantic check: justification required, then written to notes --

def test_report_errors_refuses_a_failed_to_skipped_move_with_no_justification():
    errors = tr.task_status_report_errors(
        {"task_id": "010", "task_status": "skipped"}, "failed", True)
    assert errors
    assert "justification" in errors[0]


def test_report_errors_refuses_a_blank_justification():
    errors = tr.task_status_report_errors(
        {"task_id": "010", "task_status": "skipped", "justification": "   "},
        "failed", False)
    assert errors
    assert "justification" in errors[0]


def test_report_errors_accepts_a_real_justification():
    errors = tr.task_status_report_errors(
        {"task_id": "010", "task_status": "skipped",
         "justification": "criteria required hardware this sandbox lacks"},
        "failed", True)
    assert errors == []


def test_other_declared_moves_are_untouched_by_the_new_condition():
    # the justification requirement is scoped to failed -> skipped only --
    # an ordinary in-progress -> completed report needs no such field.
    errors = tr.task_status_report_errors(
        {"task_id": "010", "task_status": "completed"}, "in-progress", False)
    assert errors == []


def test_applier_refuses_and_writes_nothing_without_a_justification(tmp_path):
    sup = _sup(tmp_path, [
        {"id": "010", "title": "t", "status": "failed",
         "successCriteria": "do X"},
    ])
    errors = sup._apply_reported_task_status(
        {"task_id": "010", "task_status": "skipped"})
    assert errors
    assert "justification" in errors[0]
    task = sup.run.read_tasks()["tasks"][0]
    assert task["status"] == "failed"
    assert "notes" not in task


def test_applier_applies_the_move_and_writes_the_justification_into_notes(tmp_path):
    sup = _sup(tmp_path, [
        {"id": "010", "title": "t", "status": "failed",
         "successCriteria": "do X"},
    ])
    errors = sup._apply_reported_task_status({
        "task_id": "010", "task_status": "skipped",
        "justification": "the residual gap was carved into task 011 instead",
    })
    assert errors == []
    task = sup.run.read_tasks()["tasks"][0]
    assert task["status"] == "skipped"
    assert task["notes"] == "the residual gap was carved into task 011 instead"


def test_applier_emits_the_task_status_event_for_the_move(tmp_path):
    sup = _sup(tmp_path, [
        {"id": "010", "title": "t", "status": "failed",
         "successCriteria": "do X"},
    ])
    sup._apply_reported_task_status({
        "task_id": "010", "task_status": "skipped",
        "justification": "cannot be built in this sandbox",
    })
    from ralphd.engine.state import read_events
    events = [e for e in read_events(sup.run.root) if e.get("type") == "task"]
    assert any(e.get("taskId") == "010" and e.get("newStatus") == "skipped"
              for e in events)


# -- 3. the caveat: skipped never un-dooms a dependent -----------------------

def test_skipped_is_still_absent_from_dependency_satisfied_statuses():
    assert "skipped" not in TASK_DEPENDENCY_SATISFIED_STATUSES
    # Task 016 (PRD requirement B.1, #156) added `superseded` alongside the
    # original pair -- a forced split's parent goes there, and a dependent
    # of it is not left blocked forever waiting for a status the parent
    # will never reach again. `skipped`/`failed` stay outside the tuple,
    # which is this test's own point and is unchanged by that addition.
    assert TASK_DEPENDENCY_SATISFIED_STATUSES == ("completed", "validated", "superseded")


# ----------------------------------------------------------------------
# Folded from tests/test_settled_vs_done_task013.py (task 028, requirement D.7).
# ----------------------------------------------------------------------

import asyncio
import json
from pathlib import Path

import pytest

from ralphd.engine import state, transitions
from ralphd.engine.runner import IterationResult
from ralphd.engine.state import RunDir, atomic_write_json

from test_steer import TASK_1 as svd013_TASK_1, _ok as _svd013_ok, _review_verifies, _steer_settles, _supervisor as _svd013_supervisor

STEER_EDGES = transitions.successors("steer", vigilant=False)


def _svd013_events(root: Path, type_: str) -> list[dict]:
    log = root / "events.jsonl"
    lines = log.read_text().splitlines() if log.exists() else []
    return [ev for ev in (json.loads(x) for x in lines) if ev.get("type") == type_]


def _seed(root: Path, tasks: list[dict]) -> RunDir:
    root.mkdir(parents=True, exist_ok=True)
    run = RunDir(root=root)
    run.update_status(state="running")
    atomic_write_json(run.tasks_file, {"version": 1, "tasks": tasks})
    return run


# ---------------------------------------------------------------------------
# 1. settled-but-not-done: observable, and blocks terminal success
# ---------------------------------------------------------------------------

def test_settled_but_not_done_blocks_terminal_success(tmp_path):
    root = tmp_path / "run"
    _seed(root, [
        {"id": svd013_TASK_1, "status": "completed"},
        {"id": "002", "status": "pending", "title": "steer-inserted",
         "origin": "steer"},
    ])
    sup = _svd013_supervisor(root, {"review": [_review_verifies]})
    sup._approach = 1

    outcome = asyncio.run(sup._step_review())

    # `settled` (the steer hat processed the message that produced task
    # 002 -- irrelevant here on purpose, no steer iteration even ran) says
    # nothing about `done` (task 002's own status) -- the exit gate looks
    # at DONE, and must defer.
    assert outcome == ("advance", "worker", "steering_pending_at_verified")
    status = sup.run.read_status()
    assert status.get("state") != "succeeded", (
        "a steering-origin task pending must never let VERIFIED go terminal")
    assert status.get("verdict") is None

    # independently observable, not just an internal side effect of the gate
    outstanding = state.steering_origin_outstanding(sup.run.read_tasks())
    assert [t["id"] for t in outstanding] == ["002"]


@pytest.mark.parametrize("terminal_status",
                         ["completed", "validated", "failed", "skipped"])
def test_all_done_permits_terminal_success(tmp_path, terminal_status):
    root = tmp_path / (terminal_status + "-run")
    _seed(root, [
        {"id": svd013_TASK_1, "status": "completed"},
        {"id": "002", "status": terminal_status, "title": "steer-inserted",
         "origin": "steer"},
    ])
    sup = _svd013_supervisor(root, {"review": [_review_verifies]})
    sup._approach = 1

    outcome = asyncio.run(sup._step_review())

    assert outcome == ("done", "succeeded"), (
        f"a steering-origin task already at terminal status {terminal_status!r} "
        "must not block VERIFIED")
    status = sup.run.read_status()
    assert status.get("state") == "succeeded"
    assert status.get("verdict") == "verified"
    assert state.steering_origin_outstanding(sup.run.read_tasks()) == []


def test_no_steering_origin_tasks_at_all_permits_terminal_success(tmp_path):
    """The ordinary case: a plan the planner alone proposed, no steering
    ever happened. Must behave exactly as it always did."""
    root = tmp_path / "run"
    _seed(root, [{"id": svd013_TASK_1, "status": "completed"}])
    sup = _svd013_supervisor(root, {"review": [_review_verifies]})
    sup._approach = 1

    outcome = asyncio.run(sup._step_review())

    assert outcome == ("done", "succeeded")
    assert sup.run.read_status().get("state") == "succeeded"


# ---------------------------------------------------------------------------
# 1b. the REPLACEMENT itself: one predicate, one deferral, the steer hat
#     rather than an in-place worker/re-review loop
# ---------------------------------------------------------------------------

def test_unsettled_steering_at_verified_defers_once_through_the_declared_edge(
        tmp_path):
    """The pre-013 mechanism looped IN PLACE: it discarded the VERIFIED
    verdict, dispatched a no-task `worker` iteration from inside
    `_step_review` itself and re-reviewed, over and over, until
    `pending_steering()` came back empty -- the dispatch loop never saw a
    state change at all. Task 013 replaces that with `_terminal_success_
    blockers()` and a single return through the declared
    `steering_pending_at_verified` edge: exactly ONE review iteration runs,
    no worker iteration is dispatched from here, and the run does not go
    terminal."""
    root = tmp_path / "run"
    run = _seed(root, [{"id": svd013_TASK_1, "status": "completed"}])
    fname = run.add_steering("hold on, check one more thing", "lastcall")

    sup = _svd013_supervisor(root, {"review": [_review_verifies]})
    sup._approach = 1

    outcome = asyncio.run(sup._step_review())

    assert outcome == ("advance", "worker", "steering_pending_at_verified")
    assert sup.runner.calls == {"review": 1}, (
        "the replacement must not dispatch a worker iteration (nor a second "
        f"review) from inside _step_review: {sup.runner.calls!r}")
    assert sup.runner.phases_seen == ["review"]
    status = sup.run.read_status()
    assert status.get("state") != "succeeded"
    assert status.get("verdict") is None
    # untouched, therefore still deliverable: at-least-once is the file's
    # own consume-on-clean-exit ordering, not something this gate does.
    assert sup.run.pending_steering() == [root / "steering" / fname]
    warnings = [ev for ev in _svd013_events(root, "log")
                if ev.get("level") == "warning"]
    assert any("deferring" in ev.get("message", "")
               and fname in ev.get("message", "") for ev in warnings), (
        f"the deferral must name the unsettled file: {warnings!r}")


def test_the_deferred_edge_is_settled_by_the_steer_hat_then_review_verifies(
        tmp_path):
    """What the removed in-place loop used to do by hand, now done by the
    ordinary dispatch loop: the deferral lands in `worker`, whose
    absolute-top-priority check (task 012) routes to the `steer` hat, which
    settles the message and consumes the file on its clean exit -- and only
    then does a re-review's VERIFIED verdict make the run terminal."""
    root = tmp_path / "run"
    run = _seed(root, [{"id": svd013_TASK_1, "status": "completed"}])
    fname = run.add_steering("hold on, check one more thing", "lastcall")

    sup = _svd013_supervisor(root, {"review": [_review_verifies],
                             "steer": [_steer_settles]})
    sup._approach = 1

    assert asyncio.run(sup._step_review()) == (
        "advance", "worker", "steering_pending_at_verified")
    # the declared edge's target, reached the ordinary way -- and it routes
    # to `steer`, not into a worker iteration.
    assert asyncio.run(sup._step_worker()) == (
        "advance", "steer", "steering_pending")
    assert asyncio.run(sup._step_generic_hat("steer", STEER_EDGES)) == (
        "advance", "worker", "steer_settled")
    assert sup.run.pending_steering() == [], (
        "the steer hat's clean exit must consume the message")
    consumed = json.loads((root / "steering" / ".consumed.json").read_text())
    assert fname in consumed
    assert sup._terminal_success_blockers() == []

    assert asyncio.run(sup._step_review()) == ("done", "succeeded")
    status = sup.run.read_status()
    assert status.get("state") == "succeeded"
    assert status.get("verdict") == "verified"
    assert status.get("unconsumedSteering") == []


def test_both_blockers_are_named_by_the_one_shared_predicate(tmp_path):
    """One predicate, two blockers, so the two gate sites cannot drift: an
    unsettled FILE (not `settled`) and a non-terminal steering-origin TASK
    (not `done`) are both named, and both clear independently."""
    root = tmp_path / "run"
    run = _seed(root, [
        {"id": svd013_TASK_1, "status": "completed"},
        {"id": "002", "status": "in-progress", "origin": "steer"},
    ])
    fname = run.add_steering("and one more thing", "more")
    sup = _svd013_supervisor(root, {})

    blockers = sup._terminal_success_blockers()
    assert len(blockers) == 2, blockers
    assert fname in blockers[0] and "not settled" in blockers[0]
    assert "002" in blockers[1] and "not terminal" in blockers[1]

    # settle the file: only the task half is left
    run.consume_steering([root / "steering" / fname], 1)
    blockers = sup._terminal_success_blockers()
    assert len(blockers) == 1 and "002" in blockers[0]

    # finish the task: nothing blocks terminal success any more
    doc = sup.run.read_tasks()
    for t in doc["tasks"]:
        if t["id"] == "002":
            t["status"] = "validated"
    atomic_write_json(sup.run.tasks_file, doc)
    assert sup._terminal_success_blockers() == []


def test_the_grace_gate_asks_the_same_predicate(tmp_path):
    """`_maybe_grace_review`'s own hand-rolled `pending_steering()` test is
    replaced by the same predicate: a VERIFIED grace review still may not
    make the run terminal-succeeded while an operator message is unsettled,
    and the reason note now says so in the shared vocabulary."""
    root = tmp_path / "run"
    run = _seed(root, [{"id": svd013_TASK_1, "status": "completed"}])
    fname = run.add_steering("one last thing", "last")

    sup = _svd013_supervisor(root, {"review": [_review_verifies]})
    sup._approach = 1

    assert asyncio.run(sup._maybe_grace_review(1)) is False
    assert sup.run.read_status().get("state") != "succeeded"
    note = sup._terminal_reason_note or ""
    assert "grace" in note and fname in note and "not settled" in note, note


# ---------------------------------------------------------------------------
# 2. `settled` itself is a harness-observable fact, recorded whether true
#    or false, distinct from any task's own `done` status
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("settled_claim,notes", [
    (True, None),
    (False, "the operator's message was ambiguous against the current plan"),
])
def test_steer_hats_settled_claim_is_recorded_as_a_harness_observable_fact(
        tmp_path, settled_claim, notes):
    root = tmp_path / "run"
    root.mkdir()
    run = RunDir(root=root)
    run.update_status(state="running")
    atomic_write_json(run.tasks_file, {"version": 1, "tasks": []})
    run.add_steering("do the thing", "opX")

    payload = {"settled": settled_claim}
    if notes is not None:
        payload["notes"] = notes

    def _steer_handler(sup, prompt=""):
        return _svd013_ok(json.dumps(payload))

    sup = _svd013_supervisor(root, {"steer": [_steer_handler]})

    outcome = asyncio.run(sup._step_generic_hat("steer", STEER_EDGES))
    assert outcome == ("advance", "worker", "steer_settled")

    settle_events = _svd013_events(root, "steer.settled")
    assert len(settle_events) == 1
    assert settle_events[0]["settled"] is settled_claim
    assert settle_events[0].get("notes") == notes

    status = sup.run.read_status()
    recorded = status.get("steerSettled")
    assert recorded is not None, "settled must be observable from status.json too"
    assert recorded["settled"] is settled_claim
    assert recorded.get("notes") == notes
    assert "at" in recorded

    # `settled` says nothing about `done` -- no task exists at all here, so
    # there is nothing yet for the ordinary lifecycle to have judged.
    assert state.steering_origin_outstanding(sup.run.read_tasks()) == []


# ---------------------------------------------------------------------------
# 3. at-least-once: a crash between processing and marking still
#    re-presents the steer to the very next actionable (steer) iteration
# ---------------------------------------------------------------------------

class _DyingThenOk:
    """A minimal scripted runner: the first call dies mid-work (an
    ordinary work error, `fault_class` non-None), the second succeeds with
    a schema-valid `{"settled": true}` payload."""

    def __init__(self):
        self.calls = 0
        self.running = False
        self.prompts: list[str] = []

    def interrupt(self) -> bool:
        return self.running

    async def run(self, prompt, transcript, **kw) -> IterationResult:
        self.calls += 1
        self.prompts.append(prompt)
        self.running = True
        try:
            if self.calls == 1:
                r = IterationResult(exit_code=1)
                r.error_message = "the agent reported an error"
                r.final_text = ""
                r.duration_s = 5.0
                r.usage = {"input": 4, "output": 0, "totalTokens": 4}
                return r
            r = IterationResult(exit_code=0)
            r.final_text = json.dumps({"settled": True})
            r.duration_s = 5.0
            r.usage = {"input": 10, "output": 5, "totalTokens": 15}
            return r
        finally:
            self.running = False


def test_a_crash_between_processing_and_marking_still_represents_the_steer(
        tmp_path):
    root = tmp_path / "run"
    root.mkdir()
    run = RunDir(root=root)
    run.update_status(state="running")
    fname = run.add_steering("stop rewriting the tests; fix the runner instead",
                             "focus")

    sup = _svd013_supervisor(root, {})
    sup.runner = _DyingThenOk()  # type: ignore[assignment]

    # attempt 1: dies mid-work -- the note must stay pending, not banked.
    # (`run_iteration` is the public seam; `steer` is not in
    # `INFRA_RETRY_PHASES`, so it is a straight pass-through -- no
    # `_run_iteration_once` call in a test, per the PRD's non-negotiable 5.)
    asyncio.run(sup.run_iteration("steer"))
    assert sup.run.pending_steering() == [root / "steering" / fname], (
        "a dying steer iteration must leave its note pending")
    consumed_path = root / "steering" / ".consumed.json"
    assert (not consumed_path.exists()
           or fname not in json.loads(consumed_path.read_text())), (
        "the note must not be banked as applied when the iteration died")

    # attempt 2: the following actionable (steer) iteration is handed the
    # SAME note again (at-least-once) and, having succeeded cleanly this
    # time, consumes it.
    asyncio.run(sup.run_iteration("steer"))
    assert sup.run.pending_steering() == [], (
        "the following clean steer iteration must consume the note")
    consumed = json.loads(consumed_path.read_text())
    assert fname in consumed
    # both attempts were actually handed the note's body -- it was
    # delivered every time, not just applied once.
    assert all("stop rewriting the tests" in p for p in sup.runner.prompts)


# ----------------------------------------------------------------------
# Folded from tests/test_scheduler_select_next_task.py (task 028, requirement D.7).
# ----------------------------------------------------------------------

from ralphd.engine.scheduler import select_next_task


def _ssn_task(id_, status="pending", **extra):
    t = {"id": id_, "title": f"task {id_}", "status": status,
         "successCriteria": "criteria"}
    t.update(extra)
    return t


def test_validation_failed_wins_over_everything_else():
    tasks = [
        _ssn_task("001", status="pending"),
        _ssn_task("002", status="in-progress"),
        _ssn_task("003", status="validation-failed"),
        _ssn_task("004", status="validation-failed"),
    ]
    picked = select_next_task(tasks)
    assert picked["id"] == "003"  # first validation-failed in list order


def test_in_progress_wins_over_pending_scheduler():
    tasks = [
        _ssn_task("001", status="pending"),
        _ssn_task("002", status="in-progress"),
        _ssn_task("003", status="in-progress"),
    ]
    picked = select_next_task(tasks)
    assert picked["id"] == "002"  # first in-progress in list order


def test_no_dependson_or_priority_degenerates_to_plain_list_order():
    tasks = [_ssn_task("001"), _ssn_task("002"), _ssn_task("003")]
    picked = select_next_task(tasks)
    assert picked["id"] == "001"


def test_dependson_excludes_a_pending_task_whose_dependency_is_unfinished():
    tasks = [
        _ssn_task("001", status="pending", dependsOn=["002"]),
        _ssn_task("002", status="pending"),
    ]
    picked = select_next_task(tasks)
    # 001 depends on 002 which is not yet completed/validated -- 002 is the
    # only viable candidate.
    assert picked["id"] == "002"


def test_dependson_excludes_a_pending_task_whose_dependency_is_dead():
    tasks = [
        _ssn_task("001", status="pending", dependsOn=["002"]),
        _ssn_task("002", status="failed"),
        _ssn_task("003", status="pending"),
    ]
    picked = select_next_task(tasks)
    # 001 can never become viable (dead dependency); 003 has none, so it's
    # the only candidate.
    assert picked["id"] == "003"


def test_dependson_satisfied_by_completed():
    tasks = [
        _ssn_task("001", status="pending", dependsOn=["002"]),
        _ssn_task("002", status="completed"),
    ]
    picked = select_next_task(tasks)
    assert picked["id"] == "001"


def test_dependson_satisfied_by_validated():
    tasks = [
        _ssn_task("001", status="pending", dependsOn=["002"]),
        _ssn_task("002", status="validated"),
    ]
    picked = select_next_task(tasks)
    assert picked["id"] == "001"


def test_dependson_not_satisfied_by_skipped():
    # skipped is terminal-dead, not satisfying -- same rule
    # state.TASK_DEPENDENCY_SATISFIED_STATUSES enforces elsewhere.
    tasks = [
        _ssn_task("001", status="pending", dependsOn=["002"]),
        _ssn_task("002", status="skipped"),
        _ssn_task("003", status="pending"),
    ]
    picked = select_next_task(tasks)
    assert picked["id"] == "003"


def test_highest_priority_wins_among_pending_candidates():
    tasks = [
        _ssn_task("001", status="pending", priority=1),
        _ssn_task("002", status="pending", priority=5),
        _ssn_task("003", status="pending", priority=2),
    ]
    picked = select_next_task(tasks)
    assert picked["id"] == "002"


def test_missing_priority_defaults_to_zero():
    tasks = [
        _ssn_task("001", status="pending"),  # implicit priority 0
        _ssn_task("002", status="pending", priority=-1),
    ]
    picked = select_next_task(tasks)
    assert picked["id"] == "001"


def test_priority_ties_break_by_list_order():
    tasks = [
        _ssn_task("001", status="pending", priority=3),
        _ssn_task("002", status="pending", priority=3),
    ]
    picked = select_next_task(tasks)
    assert picked["id"] == "001"


def test_nothing_viable_returns_none():
    tasks = [
        _ssn_task("001", status="completed"),
        _ssn_task("002", status="failed"),
        _ssn_task("003", status="skipped"),
        _ssn_task("004", status="pending", dependsOn=["002"]),  # dead dep
    ]
    assert select_next_task(tasks) is None


def test_empty_task_list_returns_none():
    assert select_next_task([]) is None


def test_non_dict_entries_are_ignored():
    tasks = ["not a task", _ssn_task("001", status="pending")]
    picked = select_next_task(tasks)
    assert picked["id"] == "001"


def test_task_with_no_status_key_defaults_to_pending():
    tasks = [{"id": "001", "title": "task 001", "successCriteria": "c"}]
    picked = select_next_task(tasks)
    assert picked["id"] == "001"


def test_returns_the_same_object_not_a_copy():
    tasks = [_ssn_task("001", status="pending")]
    picked = select_next_task(tasks)
    picked["status"] = "in-progress"
    assert tasks[0]["status"] == "in-progress"


# Task 013 (C.3, #127): the validation-failed and in-progress buckets honor
# `priority` (highest wins, missing -> 0, ties by list order) the same way
# the pending-task scheduler already did -- rather than always the first
# entry in list order.


def test_validation_failed_bucket_honors_priority():
    tasks = [
        _ssn_task("001", status="validation-failed", priority=1),
        _ssn_task("002", status="validation-failed", priority=5),
        _ssn_task("003", status="validation-failed", priority=2),
    ]
    picked = select_next_task(tasks)
    assert picked["id"] == "002"


def test_validation_failed_bucket_priority_ties_break_by_list_order():
    tasks = [
        _ssn_task("001", status="validation-failed", priority=3),
        _ssn_task("002", status="validation-failed", priority=3),
        _ssn_task("003", status="validation-failed"),  # priority 0, loses
    ]
    picked = select_next_task(tasks)
    assert picked["id"] == "001"


def test_in_progress_bucket_honors_priority():
    tasks = [
        _ssn_task("001", status="in-progress", priority=1),
        _ssn_task("002", status="in-progress", priority=5),
        _ssn_task("003", status="in-progress", priority=2),
    ]
    picked = select_next_task(tasks)
    assert picked["id"] == "002"


def test_in_progress_bucket_priority_ties_break_by_list_order():
    tasks = [
        _ssn_task("001", status="in-progress"),  # implicit priority 0
        _ssn_task("002", status="in-progress", priority=-1),
        _ssn_task("003", status="in-progress"),
    ]
    picked = select_next_task(tasks)
    assert picked["id"] == "001"  # 001 and 003 tie at 0, 001 is first


def test_degenerate_no_priority_anywhere_is_unchanged():
    # No task in the whole plan carries a `priority` key at all -- every
    # bucket must still degenerate to plain first-in-list-order, exactly
    # as before this task (all-zero priorities, ties broken by position).
    tasks = [
        _ssn_task("001", status="validation-failed"),
        _ssn_task("002", status="validation-failed"),
    ]
    assert select_next_task(tasks)["id"] == "001"

    tasks = [
        _ssn_task("001", status="in-progress"),
        _ssn_task("002", status="in-progress"),
    ]
    assert select_next_task(tasks)["id"] == "001"

    tasks = [_ssn_task("001"), _ssn_task("002"), _ssn_task("003")]
    assert select_next_task(tasks)["id"] == "001"


def test_bucket_precedence_is_unchanged_even_with_priority_set():
    # validation-failed still wins over in-progress and pending regardless
    # of priority, and in-progress still wins over pending regardless of
    # priority -- priority only orders WITHIN a bucket, never across them.
    tasks = [
        _ssn_task("001", status="pending", priority=100),
        _ssn_task("002", status="in-progress", priority=1),
        _ssn_task("003", status="validation-failed", priority=1),
    ]
    picked = select_next_task(tasks)
    assert picked["id"] == "003"

    tasks = [
        _ssn_task("001", status="pending", priority=100),
        _ssn_task("002", status="in-progress", priority=1),
    ]
    picked = select_next_task(tasks)
    assert picked["id"] == "002"


# v0.20 task 021 (PRD requirement C.1, #156): a `pending` task carrying
# `deferredUntil` (a worker's `verdict: "deferred"` report,
# `LoopSupervisor._apply_worker_deferred_report`) is excluded from the
# ordinary pending-task candidate pool entirely -- "moves behind every
# runnable task", pinned directly against the pure function.


def test_a_deferred_pending_task_is_never_picked_over_a_runnable_one():
    tasks = [
        _ssn_task("001", status="pending", priority=100,
                  deferredUntil="2099-01-01T00:00:00Z"),
        _ssn_task("002", status="pending"),
    ]
    picked = select_next_task(tasks)
    assert picked["id"] == "002", (
        "the deferred task's own higher priority must not preempt an "
        "ordinary runnable task -- it moved BEHIND every runnable one")


def test_only_deferred_tasks_left_returns_none():
    tasks = [
        _ssn_task("001", status="pending", deferredUntil="2099-01-01T00:00:00Z"),
        _ssn_task("002", status="completed"),
    ]
    assert select_next_task(tasks) is None, (
        "a deferred task is not itself runnable -- whether it is ever "
        "picked back up once nothing else is left is task 022's sleep "
        "mechanism, not this pure function's own fallback")


def test_a_deferred_task_does_not_block_a_lower_priority_runnable_one():
    tasks = [
        _ssn_task("001", status="pending", priority=5,
                  deferredUntil="2099-01-01T00:00:00Z"),
        _ssn_task("002", status="pending", priority=-5),
    ]
    picked = select_next_task(tasks)
    assert picked["id"] == "002"


# ----------------------------------------------------------------------
# Folded from tests/test_engine_owns_terminal_record_task008.py (task 028, requirement D.7).
# ----------------------------------------------------------------------

import re
from pathlib import Path

from ralphd.engine.config import JobConfig
from ralphd.engine.loop import LoopSupervisor
from ralphd.engine.state import RunDir
from tests.conftest import supervisor_factory

eotr008_REPO_ROOT = Path(__file__).resolve().parents[1]
REVIEW_MD = (eotr008_REPO_ROOT / "src" / "ralphd" / "prompts" / "review.md").read_text()


def _flatten(text: str) -> str:
    """Collapse markdown line-wrapping whitespace to single spaces so a
    sentence pinned here matches regardless of where prose happens to
    wrap in the source file."""
    return re.sub(r"\s+", " ", text.lower())


ENGINE_RECORD_SENTENCE = (
    "petition/adjudication accounting, steer and ruling history, "
    "approach history, terminal tallies and notification accounting are "
    "the engine's record"
)
NEVER_FAULTED_SENTENCE = (
    "nothing else in the run is faulted for omitting or predating them"
)
OBLIGATIONS_SENTENCE = (
    "there is no closing report, for this wave or any future one "
    "(requirement n.3) -- the run's terminal `notify` summary and the run "
    "dir's own artifacts carry what shipped per requirement, its evidence "
    "(commit, test node ids), and the residuals it knows of, at the code "
    "sha it names"
)


# The shared harness (tests/conftest.py, G.1); `status` is the resumed-run
# shape: the state is on disk before the supervisor exists.
_eotr008_supervisor = supervisor_factory(run_id="task008-terminal-record",
                                status={"state": "starting"})



def test_review_md_names_the_engine_owned_history_categories():
    assert ENGINE_RECORD_SENTENCE in _flatten(REVIEW_MD)


def test_review_md_states_nothing_else_is_faulted_for_these():
    assert NEVER_FAULTED_SENTENCE in _flatten(REVIEW_MD)


def test_review_md_states_there_is_no_closing_report():
    assert OBLIGATIONS_SENTENCE in _flatten(REVIEW_MD)


def test_review_md_names_commit_and_test_node_ids_as_evidence():
    assert "commit, test node ids" in _flatten(REVIEW_MD)


# ---------------------------------------------------------------------------
# The review contract context: `LoopSupervisor._terminal_record_review_
# context`, always present (unconditional, unlike the petition/operator-
# ruling contexts which return '' in the common case), and joined into
# `_review_extra_context` so every real review prompt carries it.
# ---------------------------------------------------------------------------

def test_terminal_record_review_context_names_the_engine_owned_categories(
        tmp_path):
    sup = _eotr008_supervisor(tmp_path)
    context = sup._terminal_record_review_context()
    assert ENGINE_RECORD_SENTENCE in _flatten(context)


def test_terminal_record_review_context_states_never_faulted(tmp_path):
    sup = _eotr008_supervisor(tmp_path)
    context = sup._terminal_record_review_context()
    assert NEVER_FAULTED_SENTENCE in _flatten(context)


def test_terminal_record_review_context_states_exact_obligations(tmp_path):
    sup = _eotr008_supervisor(tmp_path)
    context = sup._terminal_record_review_context()
    assert OBLIGATIONS_SENTENCE in _flatten(context)


def test_terminal_record_review_context_is_unconditional(tmp_path):
    """Unlike the petitions/operator-ruling contexts, this one carries no
    'common case: empty' -- it must be present on a bare run with no
    petitions, no ruling and no steering at all."""
    sup = _eotr008_supervisor(tmp_path)
    context = sup._terminal_record_review_context()
    assert context.strip()


def test_review_extra_context_always_includes_the_terminal_record_context(
        tmp_path):
    sup = _eotr008_supervisor(tmp_path)
    extra = sup._review_extra_context()
    assert ENGINE_RECORD_SENTENCE in _flatten(extra)
    assert NEVER_FAULTED_SENTENCE in _flatten(extra)
    assert OBLIGATIONS_SENTENCE in _flatten(extra)


# ---------------------------------------------------------------------------
# Task 002 (PRD requirement E, #206): the two behaviours added to
# `LoopSupervisor._step_worker`'s "nothing is selectable this pass" branch
# and its own stagnation-counter formula. Folded into THIS module (not a
# new one of its own) to keep the smoke tier's module ceiling (270, zero
# headroom in `tests/test_tier_budgets.py`) exactly where it was -- this
# module already absorbs several once-standalone modules the same way (see
# e.g. the review-contract-context section immediately above).
#
# 1. A `dependsOn` CYCLE among tasks that are themselves still `pending` (a
#    self-loop is the length-one case) is invisible to `_doomed_plan_fault`
#    (task 007, requirement D) -- that method only follows `failed`/
#    `skipped`/`petitioned` ANCESTORS, and a cycle among plain `pending`
#    tasks has none of those. `_unschedulable_cycle_fault` (this task) is
#    the third safety net, checked on the very FIRST pass nothing is
#    selectable, that catches this shape by its GRAPH STRUCTURE rather than
#    any task's status -- which is exactly what makes it never fire for a
#    task whose only unmet dependency is still legitimately IN FLIGHT
#    (`awaiting-validation`, PRD requirement E.6): that edge is not part of
#    any cycle, structurally, so the graph-shape check never trips on it,
#    however long the wait -- THE SEPARATING CONDITION this section proves
#    directly: a cycle is a property of the graph's SHAPE (every node on it
#    needs every OTHER node on it to finish first, so no future iteration
#    could ever resolve it), never of any task's current status, so an
#    in-flight dependency (whose far end simply has not finished YET) never
#    trips it.
#
# 2. The stagnation counter's `tool_activity` witness (task 010, PRD
#    requirement B.1) must not read tool-call evidence from an iteration
#    this pass never assigned a task to (`assigned_id is None`) -- #206's
#    own incident: a run burned 544 remaining iterations because such
#    iterations' own tool-heavy transcripts (21, 16, 4, 5 calls, always >=
#    `STAGNATION_TOOL_ACTIVITY_MIN_CALLS`) kept resetting `stagnant` to 0
#    forever, so "3 iterations with no task progress" was unreachable. The
#    unchanged half of the same regression -- an ASSIGNED iteration's
#    tool-heavy transcript still resets the counter exactly as before -- is
#    pinned by this module's own `test_commits_and_tool_activity_alone_
#    does_not_increment_the_counter_and_reset_it`, above, never duplicated
#    here.
#
# Both parts are folded into ONE test node below (not several) for the same
# reason: the fast tier's own test-count ceiling (5800, also zero headroom)
# is offset exactly by that same merge above (two pre-existing nodes folded
# into one), so this task's net addition to the suite is a single new node,
# net zero overall. Every scenario keeps its own full, independent
# assertion block; only the pytest node count is consolidated.
# ---------------------------------------------------------------------------

def _tc002_stub_out_verify_never_resolves(sup) -> None:
    """A task left `awaiting-validation` in one of this section's fixtures
    is meant to model a dependency LEGITIMATELY IN FLIGHT, forever, across
    several passes -- never a real provider call. `_verify_task` returning
    `False` with no status write is documented as its OWN "ran out of
    budget before producing one (task's status left untouched)" shape
    (`LoopSupervisor._verify_task`'s own docstring), so stubbing it this way
    keeps the fixture's meaning exact without ever reaching a runner."""
    async def _never_verified(task):
        return False
    sup._verify_task = _never_verified          # type: ignore[method-assign]


def test_unschedulable_cycle_fault_and_its_tool_activity_gating(tmp_path):
    """See this section's own banner comment, above, for why this is ONE
    node covering several independent scenarios rather than several."""
    from ralphd.engine import plan_ingestion, scheduler

    # -- part 1: a self-dependency cycle faults on the first pass ----------
    #
    # A fixture plan whose only pending task depends on itself: the fault
    # fires on the very FIRST selection pass -- no `"worker"` handler is
    # even registered below, so a real worker iteration dispatching at all
    # would raise `KeyError` inside the scripted runner -- names the
    # offending edge on its own event payload, marks the run's
    # `status.json` `health: "degraded"`, and routes to the pause-and-
    # escalate path (never `worker` self-looping, never `review`: there is
    # no verdict pending, only a `tasks.json` edit an operator has to make
    # by hand). Two dependents of the cycled task -- DOWNSTREAM-TASK
    # (direct) and TRANSITIVE-TASK (two hops, through DOWNSTREAM-TASK) --
    # are folded into the SAME fixture (review cure cure-01-04, PRD
    # requirement E.3, #206): the fault is on the same pass regardless,
    # so this also pins that the same-pass escalation notification names
    # every one of them, not merely the cycle itself.
    root1 = tmp_path / "self-loop"
    RunDir(root=root1).update_status(state="starting")
    sup1 = _fv_supervisor(root1, {})
    sent1: list[str] = []
    sup1.notifier = sent1.append
    _write_plan(sup1, [
        _fv_task(dependsOn=["001"]),
        _fv_task(id="DOWNSTREAM-TASK", dependsOn=["001"]),
        _fv_task(id="TRANSITIVE-TASK", dependsOn=["DOWNSTREAM-TASK"]),
    ])
    sup1.iterations_used = 1

    asyncio.run(_drive_until_paused(
        sup1, expect_reason="unschedulable_dependency_cycle"))

    status1 = sup1.run.read_status()
    assert status1.get("health") == "degraded", status1

    # the same-pass notification names the blocked task, its offending
    # edge, and BOTH the direct and the transitive dependent -- never
    # only the cycle itself (review cure cure-01-04, PRD requirement E.3)
    assert sent1, sent1
    assert "001" in sent1[-1], sent1
    assert "001 -> 001" in sent1[-1], sent1
    assert "DOWNSTREAM-TASK" in sent1[-1], sent1
    assert "TRANSITIVE-TASK" in sent1[-1], sent1

    events1 = _fv_events(root1)
    fault_events = [ev for ev in events1
                    if ev.get("type") == "log" and "cycleTaskIds" in ev]
    assert len(fault_events) == 1, fault_events
    payload = fault_events[0]
    assert payload["cycleTaskIds"] == ["001"], payload
    assert payload["cycleEdges"] == [{"from": "001", "to": "001"}], payload
    assert payload["dependentTaskIds"] == [
        "DOWNSTREAM-TASK", "TRANSITIVE-TASK"], payload
    assert payload["planUnverifiable"] is True, payload

    escalations1 = [ev for ev in events1 if ev.get("type") == "operator_escalation"]
    assert escalations1 and escalations1[0].get("reason") == \
        "unschedulable_dependency_cycle", escalations1

    # never dispatched a worker iteration at all
    assert "worker" not in _phases_recorded(root1)

    # -- part 2: the separating condition -- an in-flight dependency ------
    #
    # THE SEPARATING CONDITION as a direct, unit-level check first: task
    # 002's only unmet dependency (001) is `awaiting-validation` --
    # genuinely in flight, not dead, not part of any cycle -- so `scheduler.
    # select_next_task` agrees nothing is selectable this pass (the SAME
    # structural reason as part 1's self-loop: an unmet `dependsOn`
    # excludes 002 from the candidate pool), but `_unschedulable_cycle_
    # fault` must return `None` because the graph itself holds no cycle,
    # however this waits. `plan_ingestion.dependency_cycles` (this task's
    # own re-exposure of `_detect_cycles`'s DFS) agrees too.
    root2 = tmp_path / "in-flight"
    RunDir(root=root2).update_status(state="starting")
    sup2 = _fv_supervisor(root2, {})
    _write_plan(sup2, [
        _fv_task(status="awaiting-validation"),
        _fv_task(id="002", dependsOn=["001"]),
    ])

    tasks_before = sup2.run.read_tasks()
    selected = scheduler.select_next_task(tasks_before.get("tasks", []))
    assert selected is None, selected            # nothing selectable this pass
    assert sup2._unschedulable_cycle_fault(tasks_before) is None
    assert plan_ingestion.dependency_cycles(tasks_before["tasks"]) == []

    # Then the end-to-end companion, with the SAME in-flight fixture: the
    # worker keeps getting dispatched every pass (never short-circuited by
    # the cycle fault, never routed anywhere but its own ordinary self-
    # loop) and, since nothing about 001 ever resolves in this scripted
    # scenario, the run eventually reaches the PRE-EXISTING `stagnation_
    # last_approach` pause on its own three-strikes count -- proving the
    # ordinary path, never this task's new one, is what ends this run.
    root3 = tmp_path / "in-flight-dispatch"
    RunDir(root=root3).update_status(state="starting")
    sup3 = _fv_supervisor(
        root3, {"worker": lambda s, p="": _fv_ok(
            json.dumps({"verdict": "no_verdict"}))})
    _tc002_stub_out_verify_never_resolves(sup3)
    _write_plan(sup3, [
        _fv_task(status="awaiting-validation"),
        _fv_task(id="002", dependsOn=["001"]),
    ])
    # written once here, exactly like a real planning iteration would --
    # else the FIRST worker pass computes it lazily and its own before/
    # after tasks.json diff (`criteriaFingerprint`/`criteriaOfRecord`
    # appearing) is a false positive on the `tasks.json changed` witness,
    # unrelated to anything this scenario means to exercise.
    sup3._ensure_criteria_baseline()
    sup3.iterations_used = 1
    sup3._approach = 1

    outcome3 = asyncio.run(sup3._step_worker())

    assert outcome3 == ("advance", "paused", "stagnation_last_approach"), outcome3
    status3 = sup3.run.read_status()
    assert status3.get("pauseReason") == "stagnation", status3
    assert status3.get("health") != "degraded", status3

    events3 = _fv_events(root3)
    assert not [ev for ev in events3
               if ev.get("type") == "log" and "cycleTaskIds" in ev], events3
    assert not [ev for ev in events3
               if ev.get("type") == "operator_escalation"
               and ev.get("reason") == "unschedulable_dependency_cycle"], events3
    # the worker WAS dispatched, repeatedly, despite nothing being selectable
    assert _worker_iteration_count(root3) >= 3

    # -- part 3: the tool_activity witness is gated on `assigned_id` ------
    #
    # The regression: three ordinary no-op iterations with nothing ever
    # selectable (001 stays `awaiting-validation` throughout, via the same
    # stub as part 2) -- the THIRD one alone carries >=
    # `STAGNATION_TOOL_ACTIVITY_MIN_CALLS` `tool_execution_start` events --
    # must still trip the guard, because none of those three ever had an
    # `assigned_id`. The unchanged, assigned half of this same regression
    # (an assigned iteration's tool-heavy transcript still resets the
    # counter) is pinned by this module's own (already-merged) `test_
    # commits_and_tool_activity_alone_does_not_increment_the_counter_and_
    # reset_it`, above, never duplicated here.
    root4 = tmp_path / "tool-heavy-unassigned"
    workspace4 = tmp_path / "tool-heavy-unassigned-workspace"
    _init_git_workspace(workspace4)
    threshold = LoopSupervisor.STAGNATION_TOOL_ACTIVITY_MIN_CALLS
    RunDir(root=root4).update_status(state="starting")
    sup4 = _spw010_supervisor(root4, workspace4, {
        "worker": _steps(_noop, _noop, _tool_heavy(threshold)),
    })
    _tc002_stub_out_verify_never_resolves(sup4)
    _write_plan(sup4, [
        _fv_task(status="awaiting-validation"),
        _fv_task(id="002", dependsOn=["001"]),
    ])
    sup4._ensure_criteria_baseline()   # see part 2's own comment above
    sup4.iterations_used = 1
    sup4._approach = 1

    outcome4 = asyncio.run(sup4._step_worker())

    assert outcome4 == ("advance", "paused", "stagnation_last_approach"), outcome4
    msgs4 = _fv_log_messages(root4)
    assert "3 iterations with no task progress" in msgs4, msgs4
    # all three iterations ran -- the tool-heavy one did not short-circuit
    # anything, it simply failed to reset the counter
    assert _worker_iteration_count(root4) == 3

    # -- part 4: an ABSENT-id dependsOn edge faults on the first pass -----
    #
    # Review cure cure-01-03 (PRD requirement E.1/E.2, #206's second
    # defect): `_unschedulable_cycle_fault` (part 1 above) is invisible to
    # this shape -- `plan_ingestion.dependency_cycles`'s graph only has
    # nodes for ids that actually exist, so an edge naming an id that is
    # not in the plan AT ALL never closes a cycle -- and `_doomed_plan_
    # fault` is invisible to it too (there is no `failed`/`skipped`
    # ancestor to follow; the id is simply absent). Before this cure, this
    # exact fixture (a single pending task depending on a nonexistent id,
    # never a cycle, never a dead ancestor) spun through THREE ordinary
    # no-task worker dispatches and landed on `stagnation_last_approach`
    # (GH #206's own second incident) -- proven directly below by driving
    # the SAME fixture unpatched through the ordinary path first. The cure
    # makes `_unresolvable_edge_fault` catch it structurally, on the very
    # FIRST pass, through the REAL `_step_worker` (`_drive_until_paused`,
    # never the fault helper alone) -- and, in the same fixture, folds in
    # cure-01-04's own dependent-naming shape (DOWNSTREAM-TASK direct,
    # TRANSITIVE-TASK two hops) so this new fault's escalation is proven
    # to name dependents too, not merely the cycle fault's.
    root5 = tmp_path / "unresolved-edge"
    RunDir(root=root5).update_status(state="starting")
    sup5 = _fv_supervisor(root5, {})
    sent5: list[str] = []
    sup5.notifier = sent5.append
    _write_plan(sup5, [
        _fv_task(dependsOn=["nonexistent"]),
        _fv_task(id="DOWNSTREAM-TASK", dependsOn=["001"]),
        _fv_task(id="TRANSITIVE-TASK", dependsOn=["DOWNSTREAM-TASK"]),
    ])
    sup5.iterations_used = 1

    asyncio.run(_drive_until_paused(
        sup5, expect_reason="unresolvable_dependency_edge"))

    status5 = sup5.run.read_status()
    assert status5.get("health") == "degraded", status5

    assert sent5, sent5
    assert "001" in sent5[-1], sent5
    assert "001 -> nonexistent" in sent5[-1], sent5
    assert "DOWNSTREAM-TASK" in sent5[-1], sent5
    assert "TRANSITIVE-TASK" in sent5[-1], sent5

    events5 = _fv_events(root5)
    fault_events5 = [ev for ev in events5
                    if ev.get("type") == "log" and "unresolvedTaskIds" in ev]
    assert len(fault_events5) == 1, fault_events5
    payload5 = fault_events5[0]
    assert payload5["unresolvedTaskIds"] == ["001"], payload5
    assert payload5["unresolvedEdges"] == [
        {"from": "001", "to": "nonexistent"}], payload5
    assert payload5["dependentTaskIds"] == [
        "DOWNSTREAM-TASK", "TRANSITIVE-TASK"], payload5
    assert payload5["planUnverifiable"] is True, payload5

    escalations5 = [ev for ev in events5 if ev.get("type") == "operator_escalation"]
    assert escalations5 and escalations5[0].get("reason") == \
        "unresolvable_dependency_edge", escalations5

    # never dispatched a worker iteration at all
    assert "worker" not in _phases_recorded(root5)

    # THE SEPARATING CONDITION for this fault too: a `dependsOn` naming an
    # id that DOES exist -- however unfinished -- never trips it, however
    # long the wait (the legitimate awaiting-validation case part 2/3
    # above already drives end-to-end; this is the direct unit check).
    tasks_before5b = sup2.run.read_tasks()
    assert sup2._unresolvable_edge_fault(tasks_before5b) is None

    # and the pre-cure shape, proven directly: the SAME absent-id fixture,
    # run through the ordinary no-task worker dispatch with the new fault
    # helper patched out, still lands on the pre-existing `stagnation_
    # last_approach` guard after three dispatches -- exactly what this
    # fixture did before cure-01-03 existed, now reachable only by
    # deliberately bypassing the new safety net rather than by its absence.
    root6 = tmp_path / "unresolved-edge-precure-shape"
    RunDir(root=root6).update_status(state="starting")
    sup6 = _fv_supervisor(
        root6, {"worker": lambda s, p="": _fv_ok(
            json.dumps({"verdict": "no_verdict"}))})
    sup6._unresolvable_edge_fault = lambda tasks_doc: None   # type: ignore[method-assign]
    _write_plan(sup6, [_fv_task(dependsOn=["nonexistent"])])
    sup6._ensure_criteria_baseline()
    sup6.iterations_used = 1
    sup6._approach = 1

    outcome6 = asyncio.run(sup6._step_worker())

    assert outcome6 == ("advance", "paused", "stagnation_last_approach"), outcome6
    status6 = sup6.run.read_status()
    assert status6.get("pauseReason") == "stagnation", status6
    assert status6.get("health") != "degraded", status6
    assert _worker_iteration_count(root6) >= 3
