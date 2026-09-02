"""Task 012 (requirement C.2, #124): the `empty_plan` boundary writes a
blocker signature the same way worker stagnation writes its own (task 026,
#61), reusing the SAME `blockerSignatures` map and the SAME `_step_replan`
guard -- not a second lint-specific mechanism.

Mirrors `tests/test_plan_ingestion_rejection.py`'s scaffolding (a
`LoopSupervisor` driven by a hand-scripted `_ScriptedRunner`, no real
subprocess -- deliberately not shared via import, as every other
`_run_job_core`-driving test module in this tree keeps its own copy) and
`tests/test_stagnation_signature.py`'s two-directions shape:

* two consecutive approaches whose plan proposals fail ingestion on the
  SAME normalized errors end the run right there, on the
  `repeated_blocker_signature` guard, without ever spending a third
  approach that budget would otherwise allow, with a `reason` naming the
  lint (the ingestion error text itself);
* two consecutive approaches whose proposals fail on DISTINCT errors are
  not short-circuited -- both approaches are consumed exactly as before
  this task, ending only on ordinary budget/approach exhaustion.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from ralphd.engine.config import JobConfig
from ralphd.engine.loop import LoopSupervisor
from ralphd.engine.runner import IterationResult
from ralphd.engine.state import RunDir, atomic_write_json


# -- scaffolding -- mirrors tests/test_plan_ingestion_rejection.py --------


class _ScriptedRunner:
    """Stands in for PiRunner. Each script entry takes the supervisor and
    returns an IterationResult; the last entry repeats forever."""

    def __init__(self, sup: LoopSupervisor, script: list):
        self.sup = sup
        self.script = script
        self.calls = 0
        self.running = False

    def interrupt(self) -> bool:
        return self.running

    async def run(self, prompt, transcript, **kw) -> IterationResult:
        self.calls += 1
        self.running = True
        try:
            step = self.script[min(self.calls - 1, len(self.script) - 1)]
            return step(self.sup)
        finally:
            self.running = False


def _ok(text: str) -> IterationResult:
    r = IterationResult(exit_code=0)
    r.final_text = text
    r.duration_s = 30.0          # not an "instant" failure
    r.usage = {"input": 10, "output": 5, "totalTokens": 15}
    return r


def _pointer_line(path: str = "plan-proposal.json", summary: str = "planned") -> str:
    return json.dumps({"proposal_path": path, "summary": summary})


def _bad_plan_step(dep_id: str):
    """A proposal whose single task depends on a nonexistent id -- the
    `plan_ingestion.lint_proposal` "unresolvable dependsOn" lint, worded by
    which id it names so two calls can be made to name either the SAME id
    (same normalized error) or DISTINCT ones (different error)."""

    def step(sup: LoopSupervisor) -> IterationResult:
        atomic_write_json(sup.run.root / "plan-proposal.json", {
            "version": 1, "tasks": [
                {"id": "001", "title": "t1", "successCriteria": "c1",
                 "dependsOn": [dep_id]},
            ]})
        return _ok("planned\n" + _pointer_line())

    return step


def _supervisor(root: Path, script: list, **cfg_kw) -> LoopSupervisor:
    root.mkdir(parents=True, exist_ok=True)
    run = RunDir(root=root)
    kw = {"iterations": 12, "max_approaches": 3, "vigilant": False,
          "on_complete": "exit", "infra_retry_backoff_s": [0.0],
          "infra_retry_backoff_max_s": 0.0, "infra_outage_budget_s": 1000.0,
          "contract_retry_max": 0,
          **cfg_kw}
    sup = LoopSupervisor(JobConfig(run_id="task012-ingestion-sig", **kw),
                         run, root)
    sup.runner = _ScriptedRunner(sup, script)   # type: ignore[assignment]

    async def no_backoff(seconds):
        return seconds, False

    sup._wait_out_backoff = no_backoff          # type: ignore[method-assign]
    return sup


PRIOR_TASKS_DOC = {
    "version": 1, "goal": "prior", "scope": {}, "repositories": [],
    "tasks": [{"id": "prior-1", "title": "already here",
               "successCriteria": "was here before the bad replan"}],
    "discovered": {},
}


def test_two_approaches_with_the_same_ingestion_errors_end_on_repeated_blocker_signature(
        tmp_path):
    root = tmp_path / "run"
    # max_approaches=3, but the guard must fire the moment approach 2's
    # own ingestion-error signature matches approach 1's -- a third
    # approach must never start even though budget would allow one.
    sup = _supervisor(root, [_bad_plan_step("999"), _bad_plan_step("999")])
    sup.run.write_tasks(dict(PRIOR_TASKS_DOC))

    seen_edges: list[tuple] = []
    real_advance = sup._advance

    def spy_advance(state, target, guard):
        seen_edges.append((state, target, guard))
        return real_advance(state, target, guard)

    sup._advance = spy_advance  # type: ignore[method-assign]

    final_state = asyncio.run(sup.run_job())

    assert final_state == "failed"
    status = sup.run.read_status()
    assert status["state"] == "failed"
    assert status["verdict"] == "unverified"
    # Ended AT approach 2 -- never entered a third planning attempt.
    assert status["approach"] == 2
    signatures = status.get("blockerSignatures") or {}
    assert signatures.get("1") == signatures.get("2")
    assert signatures["1"] is not None
    # The reason names the lint (the ingestion error text itself), not a
    # boilerplate placeholder.
    assert "dependson references unknown task" in (status.get("reason") or "")
    assert ("replan", "finishing", "repeated_blocker_signature") in seen_edges
    # Only two planning iterations ever ran.
    iters = sorted((root / "iterations").iterdir())
    metas = [json.loads((d / "meta.json").read_text()) for d in iters]
    assert sum(1 for m in metas if m["phase"] == "planning") == 2
    # tasks.json was never overwritten by either bad proposal.
    assert sup.run.read_tasks() == PRIOR_TASKS_DOC


def test_two_approaches_with_distinct_ingestion_errors_still_consume_both(
        tmp_path):
    root = tmp_path / "run"
    sup = _supervisor(
        root, [_bad_plan_step("alpha-task"), _bad_plan_step("beta-task"),
               _bad_plan_step("gamma-task")],
        max_approaches=3)
    sup.run.write_tasks(dict(PRIOR_TASKS_DOC))

    final_state = asyncio.run(sup.run_job())

    assert final_state == "failed"
    status = sup.run.read_status()
    assert status["state"] == "failed"
    assert status["verdict"] == "unverified"
    # All three approaches were spent -- every consecutive pair's own
    # ingestion-error signature is distinct, so the repeated-blocker-
    # signature guard never fires and the run ends only on ordinary
    # budget/approach exhaustion, exactly as before this task.
    assert status["approach"] == 3
    signatures = status.get("blockerSignatures") or {}
    assert signatures.get("1") != signatures.get("2")
    assert signatures.get("2") != signatures.get("3")
    assert signatures.get("1") != signatures.get("3")
    iters = sorted((root / "iterations").iterdir())
    metas = [json.loads((d / "meta.json").read_text()) for d in iters]
    assert sum(1 for m in metas if m["phase"] == "planning") == 3
