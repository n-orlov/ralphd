"""Task 003 (PRD v0.21.1 requirement C, #-- "a planning death does not
spend a product approach") rewrites this module in place.

Task 007 (v0.21, PRD requirement B.3, #135) made the two-strike ingestion-
termination mechanism's terminal reason legible (name the lint, quote the
clause) but explicitly kept the mechanism's CHARGING behaviour exactly as
it was -- including the fact that a zero-deliverable planning death (an
ingestion episode that never produced a single usable task) spent the SAME
`max_approaches` slot a real, worker-driven approach spends. v0.21.1's
requirement C observed the resulting bug at `6e3ec40`: with
`max_approaches=1`, an invalid first plan and a perfectly legal retry
available, the run ended `failed` after exactly ONE ingestion failure
(`runner.calls=1`) -- never even reaching the worker -- while this module's
OWN shipped assertions (`status["approach"] == 2`, and the string "both
approaches count normally against max_approaches" in the terminal reason)
pinned exactly that charging behaviour as correct.

This is the required retirement/rewrite of those two assertions (C.4);
their surviving cover is this SAME module, in place, under the SAME test
name (`test_a_repeated_planning_death_names_the_lexical_lint_and_quotes_
the_clause`) -- nothing about "the two-strike rule still fires and still
names the lint" was deleted, only the "both approaches count normally"
half, which requirement C reverses. `test_c1_...` below is the new
companion proving the actual fix (C.1/C.3): a single zero-deliverable
planning death no longer spends the run's one permitted product approach.

`_step_planning`'s new mechanism (`src/ralphd/engine/loop.py`): a plan-
ingestion failure now tracks its own two-strike signature on a dedicated,
non-approach-keyed pair of status.json fields
(`planningDeathSignature`/`planningDeathDetail`) instead of the
`blockerSignatures`/`blockerSignatureOrigins` maps worker stagnation (#61)
owns -- a zero-deliverable retry deliberately never advances
`self._approach`, so "approach N vs approach N-1" is not a question those
maps can answer for it any longer. Unless that dedicated comparison fires,
`_step_planning` still reaches `replan` via the `empty_plan` guard exactly
as before; `_step_replan` (reading `self._replan_guard`, the guard
`_advance` recorded on the way in) retries planning on the SAME approach
number, uncharged, instead of running the ordinary
`budget_and_approach_left`/`budget_or_approach_exhausted` check.

Scaffolding mirrors `tests/test_plan_ingestion_rejection.py`'s
`_ScriptedRunner` (stands in for `PiRunner`, no real subprocess) --
deliberately not shared via import, exactly as every other
`_run_job_core`-driving test module in this tree keeps its own copy.
"""

from __future__ import annotations

import asyncio
import json

from ralphd.engine.loop import LoopSupervisor
from ralphd.engine.runner import IterationResult
from ralphd.engine.state import atomic_write_json
from tests.conftest import supervisor_with_runner


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
    r.duration_s = 30.0  # not an "instant" failure
    r.usage = {"input": 10, "output": 5, "totalTokens": 15}
    return r


def _pointer_line(path: str = "plan-proposal.json", summary: str = "planned") -> str:
    return json.dumps({"proposal_path": path, "summary": summary})


# The SAME unenumerated-quantifier clause every planning attempt resubmits --
# `plan_ingestion.lint_proposal` refuses this on lexical grounds
# ("quantifies over an unenumerated set") every single time, so the two
# consecutive planning ATTEMPTS' normalised signatures compare equal and
# the retained two-strike guard fires on the second one.
_OFFENDING_CLAUSE = "Each of the shipped features works correctly"


def _bad_quantifier_plan(sup: LoopSupervisor) -> IterationResult:
    atomic_write_json(sup.run.root / "plan-proposal.json", {"version": 1, "tasks": [
        {"id": "001", "title": "t1",
         "successCriteria": f"{_OFFENDING_CLAUSE}."},
    ]})
    return _ok("planned\n" + _pointer_line())


_GOOD_PLAN_TASK_ID = "001"


def _good_plan(sup: LoopSupervisor) -> IterationResult:
    """A perfectly legal retry -- no unenumerated quantifier, one clause,
    well under the checkable-clause cap."""
    atomic_write_json(sup.run.root / "plan-proposal-good.json", {"version": 1, "tasks": [
        {"id": _GOOD_PLAN_TASK_ID, "title": "do the one thing",
         "successCriteria": "the one thing named by `docs/the-one-thing.md` is done"},
    ]})
    return _ok("planned\n" + _pointer_line("plan-proposal-good.json"))


def _bounded(coro, timeout: float = 30.0):
    """Every `run_job()` in this module is bounded -- `_step_paused`'s
    `await self._gate()` and a repeating scripted-runner step can both
    wait forever otherwise (tests/test_loop_driving_await_guard.py)."""
    return asyncio.run(asyncio.wait_for(coro, timeout))


def _no_progress(sup: LoopSupervisor) -> IterationResult:
    """An ordinary worker attempt that reports no verdict at all -- proves
    the run genuinely reached and ran the worker hat."""
    return _ok("still working, no verdict yet")


def _finish_task(sup: LoopSupervisor) -> IterationResult:
    task_id = sup.run.read_tasks()["tasks"][0]["id"]
    return _ok("done\n" + json.dumps({"verdict": "complete", "task_id": task_id}))


def _review_ok(sup: LoopSupervisor) -> IterationResult:
    return _ok("looks good\n" + json.dumps({"verdict": "verified"}))


_supervisor = supervisor_with_runner(
    lambda sup, script: _ScriptedRunner(sup, script),
    run_id="v021-planning-death", iterations=20, max_approaches=2, vigilant=False,
    on_complete="exit", infra_retry_backoff_s=[0.0], infra_retry_backoff_max_s=0.0,
    infra_outage_budget_s=1000.0, contract_retry_max=0)


def test_a_repeated_planning_death_names_the_lexical_lint_and_quotes_the_clause(
        tmp_path):
    root = tmp_path / "run"
    sup = _supervisor(root, [_bad_quantifier_plan])

    final_state = _bounded(sup.run_job())

    assert final_state == "failed"
    status = sup.run.read_status()
    reason = status.get("reason") or ""

    # -- the terminal reason names the lexical lint and quotes the clause --
    assert "lexical lint" in reason
    assert _OFFENDING_CLAUSE in reason
    assert "quantifies over an unenumerated set" in reason

    # -- the two-strike termination is KEPT AS-IS (#61, #124, #135): two
    # consecutive planning attempts hitting the SAME lexical lint still end
    # the run. Requirement C reverses the OLD "both approaches count
    # normally" half: NEITHER zero-deliverable attempt ever spent the
    # product-approach budget -- the run ended at approach 1 throughout,
    # never having bumped to a second approach at all.
    assert status["approach"] == 1
    assert "neither zero-deliverable attempt spent" in reason
    assert "both approaches count normally" not in reason
    # The health-degraded signal from a repeated ingestion-origin blocker
    # is unchanged by this task.
    assert status.get("health") == "degraded"

    # tasks.json was never overwritten with the rejected proposal's tasks.
    tasks_doc = sup.run.read_tasks() or {}
    assert (tasks_doc.get("tasks") or []) == []


def test_c1_a_failed_ingestion_episode_followed_by_a_legal_plan_proceeds_into_worker_without_exhausting_the_one_permitted_approach(
        tmp_path):
    """Requirement C.1/C.3: with ONE permitted product approach, a first
    plan proposal refused at ingestion, followed by a legal retry,
    proceeds into worker work -- `self._approach` never leaves 1, and the
    run's one product approach is still available to spend on real work.
    """
    root = tmp_path / "run"
    sup = supervisor_with_runner(
        lambda s, script: _ScriptedRunner(s, script),
        run_id="v021-planning-death-c1", iterations=8, max_approaches=1,
        vigilant=False, on_complete="exit", infra_retry_backoff_s=[0.0],
        infra_retry_backoff_max_s=0.0, infra_outage_budget_s=1000.0,
        contract_retry_max=0,
    )(root, [_bad_quantifier_plan, _good_plan, _finish_task, _review_ok])

    final_state = _bounded(sup.run_job())

    status = sup.run.read_status()
    # The one permitted product approach was never charged by the
    # zero-deliverable first plan proposal -- it is still approach 1 that
    # reached, ran inside, and finished worker/review.
    assert status["approach"] == 1
    assert final_state == "succeeded"

    iters = sorted((root / "iterations").iterdir())
    metas = [json.loads((d / "meta.json").read_text()) for d in iters]
    assert sum(1 for m in metas if m["phase"] == "planning") == 2
    worker_metas = [m for m in metas if m["phase"] == "worker"]
    assert worker_metas, (
        "the run never reached worker -- the zero-deliverable planning "
        "death still spent the one permitted approach")
    assert all(m.get("approach") == 1 for m in worker_metas)

    # tasks.json carries the LEGAL plan's task, never the bad proposal's,
    # and it is validated.
    tasks_doc = sup.run.read_tasks() or {}
    tasks = tasks_doc.get("tasks") or []
    assert [t.get("id") for t in tasks] == [_GOOD_PLAN_TASK_ID]
    assert tasks[0].get("status") == "completed"
