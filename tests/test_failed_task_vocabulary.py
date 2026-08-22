"""Task 024 (#33): `failed` says WHICH kind of failed, and no longer strands a run.

Two defects, one vocabulary:

* `failed` on a task meant both "a verifier judged this requirement unmet" and
  "the engine consumed the last of this task's validation rounds", so a task
  record could not be read without a human explaining it;
* worse, either meaning stranded the whole run. `worker.md`'s completion signal
  recognises only `completed` and `skipped`, so with a `failed` task in the plan
  no `<promise>COMPLETE</promise>` is ever legitimate again: the run never
  reaches `review`, the stagnation guard eventually fails the approach, and the
  next one replans the wave against an already-finished repo. That is the `043d`
  incident -- roughly four iterations and two steering notes, resolved only by an
  operator hand-editing `tasks.json`.

The fix is additive, not a sixth status (`skipped` already exists and every
surface that counts statuses would have to learn one): an engine-written
`failureKind` label read in ONE place (`state.task_failure_kind()`, which
*derives* the kind for a plan written before the label existed), plus one routing
rule in the worker loop -- a plan with nothing a worker iteration could act on
and at least one `failed` task goes to `review` instead of stagnating
(`LoopSupervisor._unresolved_failures()`). Entry to review only; the reviewer
still decides the verdict.

Tiers here: the two readers as a table, `_verify_task` writing the label with a
fake runner, and whole runs through `LoopSupervisor._run_job_core()` with a
phase-dispatching scripted runner (no subprocess, milliseconds) -- which is the
only level at which "the run reaches a terminal verdict with nobody editing the
record" can actually be observed.

Mutation cases (recorded in the commit message): dropping the
`stagnant and blocked` route in the worker loop reproduces the stuck run
(test_the_run_reaches_a_terminal_verdict_with_nobody_editing_the_record and
test_a_legacy_failed_record_also_routes_to_review fail with
`failed`/`unverified`); dropping the `failureKind` write makes
test_the_engine_labels_its_own_failed_verdict fail; making
`task_failure_kind` a plain field read makes the migration tests fail;
letting `_unresolved_failures` ignore actionability makes
test_a_plan_with_work_left_is_never_routed_to_review fail.
"""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path

import pytest

from ralphd.engine.config import JobConfig
from ralphd.engine.loop import LoopSupervisor
from ralphd.engine.runner import COMPLETE, VERIFIED, IterationResult
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

REPO = Path(__file__).resolve().parents[1]

TASK_ID = "001"


# -- scaffolding -----------------------------------------------------------


def _ok(text: str = "working") -> IterationResult:
    """A perfectly ordinary finished iteration (not an instant failure, not an
    infra fault): exit 0, real duration, some usage."""
    r = IterationResult(exit_code=0)
    r.final_text = text
    r.duration_s = 30.0
    r.usage = {"input": 10, "output": 5, "totalTokens": 15}
    return r


def _write_plan(sup: LoopSupervisor, tasks: list[dict]) -> None:
    atomic_write_json(sup.run.tasks_file, {"version": 1, "tasks": tasks})


def _task(**over) -> dict:
    task = {"id": TASK_ID, "title": "make the thing work",
            "status": "pending", "successCriteria": "the thing works"}
    task.update(over)
    return task


def _set_status(sup: LoopSupervisor, status: str) -> None:
    doc = sup.run.read_tasks()
    doc["tasks"][0]["status"] = status
    atomic_write_json(sup.run.tasks_file, doc)


class _ScriptedAgent:
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


def _supervisor(root: Path, handlers: dict, **cfg_kw) -> LoopSupervisor:
    root.mkdir(parents=True, exist_ok=True)
    run = RunDir(root=root)
    kw = {"iterations": 15, "max_approaches": 1, "vigilant": True,
          "on_complete": "exit", "infra_retry_backoff_s": [0.0],
          "infra_retry_backoff_max_s": 0.0, "infra_outage_budget_s": 1000.0,
          **cfg_kw}
    sup = LoopSupervisor(JobConfig(run_id="v07-failed-vocab", **kw), run, root)
    sup.runner = _ScriptedAgent(sup, handlers)   # type: ignore[assignment]

    async def no_backoff(seconds):
        return seconds, False

    sup._wait_out_backoff = no_backoff          # type: ignore[method-assign]
    return sup


def _run(root: Path, handlers: dict, **cfg_kw) -> tuple[LoopSupervisor, str]:
    RunDir(root=root).update_status(state="starting")
    sup = _supervisor(root, handlers, **cfg_kw)
    return sup, asyncio.run(sup._run_job_core())


def _events(root: Path) -> list[dict]:
    p = root / "events.jsonl"
    if not p.exists():
        return []
    return [json.loads(x) for x in p.read_text().splitlines() if x.strip()]


def _log_messages(root: Path) -> str:
    return "\n".join(ev.get("message") or "" for ev in _events(root)
                     if ev.get("type") == "log")


def _phases_recorded(root: Path) -> list[str]:
    return [ev.get("phase") for ev in _events(root) if ev.get("type") == "phase"]


# The agent's side of the incident: it finishes the task, the verifier never
# agrees, and once the record says `failed` the worker has nothing legitimate
# left to do with it.
def _plan(sup: LoopSupervisor, prompt: str = "") -> IterationResult:
    _write_plan(sup, [_task()])
    return _ok("planned one task")


def _claim_done(sup: LoopSupervisor, prompt: str = "") -> IterationResult:
    _set_status(sup, "completed")
    return _ok("I believe this is done now")


def _give_up(sup: LoopSupervisor, prompt: str = "") -> IterationResult:
    """A worker iteration that touches nothing: it re-reads a `failed` task it
    cannot legitimately relabel and writes only prose."""
    return _ok("task 001 is failed; I cannot advance it")


def _verify_miss(sup: LoopSupervisor, prompt: str = "") -> IterationResult:
    return _ok("criterion 2 does not hold: no test asserts it")


def _verify_pass(sup: LoopSupervisor, prompt: str = "") -> IterationResult:
    """Emits the sentinel for WHICHEVER task the verify prompt names -- the id
    is the one thing `_verify_task` matches on."""
    m = re.search(r"\*\*id\*\*: (\S+)", prompt)
    return _ok(f"<task-verified>{m.group(1) if m else TASK_ID}</task-verified>")


def _review_verified(sup: LoopSupervisor, prompt: str = "") -> IterationResult:
    return _ok(f"the PRD holds apart from the known hole {VERIFIED}")


def _review_rejects(sup: LoopSupervisor, prompt: str = "") -> IterationResult:
    return _ok("requirement 3 is not met; the failed task is the gap")


@pytest.fixture
def exhausted_run(tmp_path) -> tuple[LoopSupervisor, str]:
    """THE incident, driven end to end: three validation rounds are consumed on
    the only task, then the worker spends an iteration without resolving it."""
    root = tmp_path / "run"
    return _run(root, {"planning": _plan,
                       "worker": _steps(_claim_done, _claim_done,
                                        _claim_done, _give_up),
                       "verify": _verify_miss,
                       "review": _review_verified})


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
    (_task(status="pending"), None),
    (_task(status="in-progress"), None),
    (_task(status="completed"), None),
    (_task(status="validation-failed", validationAttempts=2), None),
    (_task(status="skipped"), None),
    # the label, when the engine wrote one
    (_task(status="failed", failureKind="validation-exhausted"),
     TASK_FAILURE_VALIDATION_EXHAUSTED),
    (_task(status="failed", failureKind="requirement-unmet"),
     TASK_FAILURE_REQUIREMENT_UNMET),
    # MIGRATION: a tasks.json written before the label existed. The engine's
    # own `failed` was only ever written at the attempt limit, so that is what
    # the evidence on the record means.
    (_task(status="failed", validationAttempts=3),
     TASK_FAILURE_VALIDATION_EXHAUSTED),
    (_task(status="failed", validationAttempts=4),
     TASK_FAILURE_VALIDATION_EXHAUSTED),
    # an agent's own verdict: no label, no exhausted rounds
    (_task(status="failed"), TASK_FAILURE_REQUIREMENT_UNMET),
    (_task(status="failed", validationAttempts=0), TASK_FAILURE_REQUIREMENT_UNMET),
    (_task(status="failed", validationAttempts=2), TASK_FAILURE_REQUIREMENT_UNMET),
    # garbage cannot crash a reader nor invent a third meaning
    (_task(status="failed", failureKind="explodey", validationAttempts=3),
     TASK_FAILURE_VALIDATION_EXHAUSTED),
    (_task(status="failed", failureKind=None), TASK_FAILURE_REQUIREMENT_UNMET),
    (_task(status="failed", validationAttempts="3"),
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
    assert task_is_actionable(_task(status=status)) is actionable
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
    sup.runner = _FixedRunner(lambda: _ok("criterion 2 does not hold"))  # type: ignore[assignment]
    _write_plan(sup, [_task(status="completed")])

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
    sup = _supervisor(tmp_path / "b", {})
    return sup._unresolved_failures({"tasks": tasks})


@pytest.mark.parametrize("tasks, blocked_ids", [
    # nothing actionable and something failed -> the run is out of moves
    ([_task(status="failed", validationAttempts=3)], [TASK_ID]),
    ([_task(status="failed")], [TASK_ID]),                 # requirement-unmet too
    ([_task(status="completed"), _task(id="002", status="failed",
                                      validationAttempts=3)], ["002"]),
    ([_task(status="skipped"), _task(id="002", status="failed")], ["002"]),
    ([_task(status="failed"), _task(id="002", status="failed")],
     [TASK_ID, "002"]),
    # one actionable task anywhere -> a worker iteration still has work
    ([_task(status="pending"), _task(id="002", status="failed")], []),
    ([_task(status="in-progress"), _task(id="002", status="failed")], []),
    ([_task(status="validation-failed", validationAttempts=1),
      _task(id="002", status="failed")], []),
    # no failed task -> the worker's own COMPLETE is what gates review (R5)
    ([_task(status="completed")], []),
    ([_task(status="completed"), _task(id="002", status="skipped")], []),
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
    msgs = _log_messages(sup.run.root)
    assert "routing to review" in msgs, msgs
    assert f"{TASK_ID} ({TASK_FAILURE_VALIDATION_EXHAUSTED})" in msgs, msgs
    assert "review" in _phases_recorded(sup.run.root)
    assert [ev for ev in _events(sup.run.root)
            if ev.get("type") == "signal" and ev.get("signal") == "VERIFIED"]


def test_the_worker_never_signalled_complete_and_the_approach_never_failed(
        exhausted_run):
    """The route is not "the worker got away with claiming COMPLETE", and it
    is not the stagnation guard either -- both would be a different bug."""
    sup, _state = exhausted_run
    events = _events(sup.run.root)
    assert not [ev for ev in events
                if ev.get("type") == "signal" and ev.get("signal") == "COMPLETE"]
    msgs = _log_messages(sup.run.root)
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
    sup, state = _run(root, {"planning": _plan,
                             "worker": _steps(_claim_done, _claim_done,
                                              _claim_done, _give_up),
                             "verify": _verify_miss,
                             "review": _review_rejects})
    status = sup.run.read_status()
    assert (state, status["verdict"]) == ("failed", "unverified"), status
    assert "review" in _phases_recorded(root)
    assert "review rejected approach 1" in _log_messages(root)


def test_a_legacy_failed_record_also_routes_to_review(tmp_path):
    """MIGRATION, end to end: a plan written before `failureKind` existed --
    `failed` with three recorded rounds and no label -- reads as exhausted and
    routes exactly the same way. The engine does not rewrite it."""
    def legacy_plan(sup: LoopSupervisor, prompt: str = "") -> IterationResult:
        _write_plan(sup, [_task(status="failed", validationAttempts=3,
                                validationNotes="round 3: criterion 2 unmet")])
        return _ok("planned (legacy record)")

    root = tmp_path / "run"
    sup, state = _run(root, {"planning": legacy_plan, "worker": _give_up,
                             "verify": _verify_miss,
                             "review": _review_verified})
    status = sup.run.read_status()
    assert (state, status["verdict"]) == ("succeeded", "verified"), status
    assert f"{TASK_ID} ({TASK_FAILURE_VALIDATION_EXHAUSTED})" in \
        _log_messages(root)
    task = sup.run.read_tasks()["tasks"][0]
    assert "failureKind" not in task, \
        "an old record is read, not retroactively rewritten"


def test_a_plan_with_work_left_is_never_routed_to_review(tmp_path):
    """The route must not fire while a worker iteration still has something to
    pick up -- otherwise one failed task would cut a plan's remaining work
    short. Here the second task stays `pending` and the worker keeps ignoring
    it, so the run ends on the stagnation guard, as before."""
    def plan_two(sup: LoopSupervisor, prompt: str = "") -> IterationResult:
        _write_plan(sup, [_task(status="failed", validationAttempts=3),
                          _task(id="002", status="pending")])
        return _ok("planned two tasks")

    root = tmp_path / "run"
    sup, state = _run(root, {"planning": plan_two, "worker": _give_up,
                             "verify": _verify_miss, "review": _review_verified})
    msgs = _log_messages(root)
    assert "routing to review" not in msgs, msgs
    assert "3 iterations with no task progress" in msgs, msgs
    assert "review" not in _phases_recorded(root)
    assert (state, sup.run.read_status()["verdict"]) == ("failed", "unverified")


def test_a_finished_plan_still_waits_for_the_workers_complete(tmp_path):
    """R5 is untouched where it applies: with every task `completed` and no
    `failed` one, entry to review is still the worker's COMPLETE. A worker that
    finishes the work and forgets to signal gets the stagnation guard, exactly
    as before this task."""
    def finish_quietly(sup: LoopSupervisor, prompt: str = "") -> IterationResult:
        _set_status(sup, "completed")
        return _ok("done, no signal")

    root = tmp_path / "run"
    sup, state = _run(root, {"planning": _plan,
                             "worker": _steps(finish_quietly, _give_up),
                             "verify": _verify_pass,
                             "review": _review_verified})
    msgs = _log_messages(root)
    assert "routing to review" not in msgs, msgs
    assert "3 iterations with no task progress" in msgs, msgs
    assert (state, sup.run.read_status()["verdict"]) == ("failed", "unverified")


def test_a_worker_that_carves_out_a_follow_up_task_is_not_pre_empted(tmp_path):
    """The better outcome stays reachable: the iteration after the task fails
    belongs to the worker, so `worker.md`'s first resolution (a NEW task for the
    residual gap) still happens and the run reaches review the ordinary way,
    through COMPLETE."""
    def carve_follow_up(sup: LoopSupervisor, prompt: str = "") -> IterationResult:
        doc = sup.run.read_tasks()
        doc["tasks"].append(_task(id="002", status="completed",
                                  title="close the residual gap of 001"))
        atomic_write_json(sup.run.tasks_file, doc)
        return _ok("carved 002 for the residual gap")

    def signal_complete(sup: LoopSupervisor, prompt: str = "") -> IterationResult:
        return _ok(f"every task resolved {COMPLETE}")

    root = tmp_path / "run"
    sup, state = _run(root, {"planning": _plan,
                             "worker": _steps(_claim_done, _claim_done,
                                              _claim_done, carve_follow_up,
                                              signal_complete),
                             "verify": _steps(_verify_miss, _verify_miss,
                                              _verify_miss, _verify_pass),
                             "review": _review_verified})
    assert (state, sup.run.read_status()["verdict"]) == ("succeeded", "verified")
    msgs = _log_messages(root)
    assert "routing to review" not in msgs, msgs
    assert [ev for ev in _events(root)
            if ev.get("type") == "signal" and ev.get("signal") == "COMPLETE"]


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
