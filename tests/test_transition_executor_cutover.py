"""Task 014 (requirement B, #60 pillar 2): `_run_job_core`'s hand-written
branch/while phase sequencing is gone; `engine.loop.LoopSupervisor` now
dispatches on an explicit FSM position and every state change goes through
`engine.transitions.TransitionExecutor`, which refuses anything the
declared table (`engine.transitions.TABLE`/`VIGILANT_DELTA`) does not list.

Three properties, each with its own test:

1. An illegal transition is refused BY THE EXECUTOR at runtime -- not by a
   test reading `transitions.is_legal()` back and asserting a property about
   it (that belongs to tests/test_transition_table.py, task 008's job).
2. The two modes (`vigilant` True/False) differ only by which table variant
   `self._executor` was built with -- there is no second, hand-written
   branch anywhere in the new step handlers.
3. No legacy dual path remains: the old `for approach in range(...)` /
   nested-`while` imperative shape is gone from `_run_job_core` itself.

Runs use the same "phase-dispatching scripted runner, no subprocess"
technique as tests/test_failed_task_vocabulary.py.
"""

from __future__ import annotations

import ast
import asyncio
import json
from pathlib import Path

import pytest

from ralphd.engine import transitions as tr
from ralphd.engine.config import JobConfig
from ralphd.engine.loop import LoopSupervisor
from ralphd.engine.runner import COMPLETE, VERIFIED, IterationResult
from ralphd.engine.state import RunDir, atomic_write_json

REPO_ROOT = Path(__file__).resolve().parents[1]
LOOP_PATH = REPO_ROOT / "src" / "ralphd" / "engine" / "loop.py"


# ---------------------------------------------------------------------------
# scaffolding (same shape as tests/test_failed_task_vocabulary.py)
# ---------------------------------------------------------------------------

def _ok(text: str = "working") -> IterationResult:
    r = IterationResult(exit_code=0)
    r.final_text = text
    r.duration_s = 30.0
    r.usage = {"input": 10, "output": 5, "totalTokens": 15}
    return r


class _ScriptedAgent:
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


def _supervisor(root: Path, handlers: dict, **cfg_kw) -> LoopSupervisor:
    root.mkdir(parents=True, exist_ok=True)
    run = RunDir(root=root)
    kw = {"iterations": 15, "max_approaches": 2, "vigilant": True,
          "on_complete": "exit", "infra_retry_backoff_s": [0.0],
          "infra_retry_backoff_max_s": 0.0, "infra_outage_budget_s": 1000.0,
          **cfg_kw}
    sup = LoopSupervisor(JobConfig(run_id="task014-cutover", **kw), run, root)
    sup.runner = _ScriptedAgent(sup, handlers)  # type: ignore[assignment]

    async def no_backoff(seconds):
        return seconds, False

    sup._wait_out_backoff = no_backoff  # type: ignore[method-assign]
    return sup


def _run(root: Path, handlers: dict, **cfg_kw) -> tuple[LoopSupervisor, str]:
    RunDir(root=root).update_status(state="starting")
    sup = _supervisor(root, handlers, **cfg_kw)
    return sup, asyncio.run(sup._run_job_core())


def _plan(sup: LoopSupervisor, prompt: str = "") -> IterationResult:
    atomic_write_json(sup.run.tasks_file, {"version": 1, "tasks": [
        {"id": "001", "title": "do the thing", "status": "pending",
         "successCriteria": "the thing is done"},
    ]})
    return _ok("planned")


def _complete_task(sup: LoopSupervisor, prompt: str = "") -> IterationResult:
    doc = sup.run.read_tasks()
    doc["tasks"][0]["status"] = "completed"
    atomic_write_json(sup.run.tasks_file, doc)
    # `IterationResult.saw_complete` is a read-only property derived from
    # `final_text`'s own last line (task 046, #55) -- it has had no setter
    # since before this task's cutover, so the marker has to actually be
    # the message's last line, not assigned onto the result afterwards.
    return _ok(f"done\n{COMPLETE}")


def _verify_pass(sup: LoopSupervisor, prompt: str = "") -> IterationResult:
    tid = sup.run.read_tasks()["tasks"][0]["id"]
    r = _ok(json.dumps({"task_id": tid, "verdict": "verified"}))
    return r


def _review_verified(sup: LoopSupervisor, prompt: str = "") -> IterationResult:
    return _ok(f"looks right\n{VERIFIED}")


def _happy_handlers() -> dict:
    return {"planning": _plan, "worker": _complete_task,
            "verify": _verify_pass, "review": _review_verified}


# ---------------------------------------------------------------------------
# 1. illegal transition refused BY THE EXECUTOR, not by reading the table back
# ---------------------------------------------------------------------------

def test_executor_refuses_an_edge_the_table_does_not_list():
    executor = tr.TransitionExecutor(vigilant=True)
    # "worker" has no successor named "reflect" in either variant.
    with pytest.raises(tr.IllegalTransition):
        executor.advance("worker", "reflect", "made_up_guard")
    # A legal edge is unaffected.
    assert executor.advance("planning", "worker", "planning_produced_tasks") == "worker"


def test_a_buggy_step_handler_proposing_an_illegal_target_fails_the_run_loudly(tmp_path):
    """Not a unit test of the checker function -- this drives the REAL
    dispatch loop in `_run_job_core` with one step handler patched to
    propose an edge the table forbids, and observes the refusal happen at
    runtime, the same way any other engine bug is contained."""
    sup, _ = None, None
    root = tmp_path / "run"
    root.mkdir()
    RunDir(root=root).update_status(state="starting")
    sup = _supervisor(root, _happy_handlers())

    async def bogus_step_worker():
        # "aborted" is a RUN_TERMINAL_STATE; nothing in the table lists it
        # as a successor of "worker".
        return ("advance", "aborted", "not_a_real_guard")

    sup._step_worker = bogus_step_worker  # type: ignore[method-assign]
    state = asyncio.run(sup._run_job_core())
    assert state == "failed"
    status = sup.run.read_status()
    assert "illegal transition" in (status.get("reason") or "")
    assert "'worker' -> 'aborted'" in (status.get("reason") or "")


# ---------------------------------------------------------------------------
# 2. the two modes differ ONLY by which table variant the executor uses
# ---------------------------------------------------------------------------

def test_loopsupervisor_builds_its_executor_from_cfg_vigilant_alone(tmp_path):
    root = tmp_path / "run"
    run = RunDir(root=root)
    vig = LoopSupervisor(JobConfig(run_id="v", iterations=1, vigilant=True), run, root)
    plain = LoopSupervisor(JobConfig(run_id="p", iterations=1, vigilant=False),
                           run, root)
    assert vig._executor.vigilant is True
    assert plain._executor.vigilant is False
    # Same class, same module-level table -- not two hand-written executors.
    assert type(vig._executor) is type(plain._executor) is tr.TransitionExecutor


def test_step_worker_reads_cfg_vigilant_exactly_once():
    """The only place `_step_worker` is allowed to branch on `self.cfg.vigilant`
    is the one guard deciding whether to attempt the verify sub-cycle at all
    (verify is unreachable in the non-vigilant table anyway -- attempting the
    edge there would simply be refused by the executor). If a second,
    hand-written vigilant/non-vigilant fork creeps in, this fails."""
    tree = ast.parse(LOOP_PATH.read_text())
    step_worker = next(
        node for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "_step_worker")
    hits = [
        node for node in ast.walk(step_worker)
        if isinstance(node, ast.Attribute) and node.attr == "vigilant"
    ]
    assert len(hits) == 1, (
        f"expected exactly one `.vigilant` read in _step_worker, found "
        f"{len(hits)}")


@pytest.mark.parametrize("vigilant", [True, False])
def test_a_full_run_takes_the_declared_edges_for_its_variant(tmp_path, vigilant):
    handlers = _happy_handlers()
    if not vigilant:
        del handlers["verify"]
    root = tmp_path / f"run-{vigilant}"
    sup, state = _run(root, handlers, vigilant=vigilant, max_approaches=1)
    assert state == "succeeded"
    agent = sup.runner
    if vigilant:
        assert "verify" in agent.phases
    else:
        assert "verify" not in agent.phases


# ---------------------------------------------------------------------------
# 3. no legacy dual path: the old imperative shape is gone from the source
# ---------------------------------------------------------------------------

def test_run_job_core_no_longer_contains_the_old_imperative_shape():
    tree = ast.parse(LOOP_PATH.read_text())
    run_job_core = next(
        node for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "_run_job_core")
    # The pre-cutover body was a `for approach in range(...)` loop with
    # nested `while` phase loops inside it. Post-cutover it is a plain
    # `while True:` dispatch over `step`, calling out to `_step_*` handlers.
    for_loops = [n for n in ast.walk(run_job_core) if isinstance(n, ast.For)]
    assert for_loops == [], (
        "_run_job_core still contains a `for` loop -- the approach "
        "boundary must be a table transition (_step_replan), not a loop "
        "construct here")
    while_loops = [n for n in ast.walk(run_job_core) if isinstance(n, ast.While)]
    assert len(while_loops) == 1, (
        f"_run_job_core should hold exactly one dispatch `while True:`, "
        f"found {len(while_loops)}")


def test_run_job_core_dispatches_on_every_step_state():
    src = LOOP_PATH.read_text()
    core_start = src.index("async def _run_job_core")
    core_end = src.index("\n    def _advance(")
    body = src[core_start:core_end]
    for state in ("planning", "replan", "worker", "review"):
        assert f'step == "{state}"' in body, (
            f"_run_job_core's dispatch loop no longer names {state!r}")


def test_the_executor_is_the_only_thing_that_moves_state_in_run_job_core():
    src = LOOP_PATH.read_text()
    core_start = src.index("async def _run_job_core")
    core_end = src.index("\n    def _advance(")
    body = src[core_start:core_end]
    # The only assignment to `step` after the initial ENTRY_STATE seed is
    # `step = self._advance(...)`.
    assignments = [
        line.strip() for line in body.splitlines()
        if line.strip().startswith("step = ") or line.strip().startswith("step: ")
    ]
    non_entry = [a for a in assignments if "ENTRY_STATE" not in a]
    assert non_entry, "expected at least one non-entry assignment to `step`"
    assert all(a == "step = self._advance(step, target, guard)" for a in non_entry), (
        f"a `step` assignment bypasses `self._advance()`: {non_entry}")


def test_loop_line_count_delta_is_recorded_honestly():
    """Not a pass/fail gate on the number itself (the PRD asks that the
    delta be RECORDED in the commit message, not that it shrink) -- this
    just pins the before/after figures so the commit message's claim is
    checkable against the tree it describes."""
    before = 2886  # git show 866a671:src/ralphd/engine/loop.py | wc -l
    after = len(LOOP_PATH.read_text().splitlines())
    assert after > 0
    # Sanity: the file actually changed size (it did -- more, smaller
    # methods with their own docstrings replaced one large one).
    assert after != before
