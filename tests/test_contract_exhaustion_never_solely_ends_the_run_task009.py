"""v0.13 task 009 (requirement C, #92): pin, directly and by name, the
invariant the whole cutover (tasks 003-008) exists to establish: *a
contract violation may never be the SOLE reason a run ends while an
approach and iteration budget remain.*

On `2026-08-28T06:24:03Z`, a run called `deck-phase3g` ended `aborted`,
`unverified`, at approach 2 of 12, iteration 135 of 900 -- with 765
iterations, 10 of 12 approaches and ~33 hours of remaining wall clock
thrown away, after 14h 36m and ~$174.52 (derived, 311755k tokens), because
the worker's final line was `{"verdict": "complete", "task_id": 104}` --
`104`, not `"104"` -- and the pre-cutover code aborted the run outright on
that single schema violation. This module drives that exact fixture shape
(approach 2 of 12, `iterations_used` seeded to 134 so the pass under test
is iteration 135 of a 900-iteration budget) with a worker hat that repeats
`deck-phase3g`'s own killing payload on EVERY attempt -- the first (real,
charged) call and every uncharged `_ask_contract_fix` re-ask alike -- so
`contract_enforcement.enforce_contract`'s ladder exhausts for real, FOUR
separate times, never once producing a valid payload: the fixture's task
has never been seen before, so its very first pass also records a
one-time `criteriaFingerprint` baseline (`_check_criteria_edits`, task
008) -- a genuine tasks.json write that keeps that pass from counting
toward the pre-existing `stagnation_exhausted` guard's own threshold
(three CONSECUTIVE no-progress passes) the same way an honoured claim
would. Passes two through four are the three genuinely silent ones the
guard is named for.

The invariant holds: `sup._abort_reason` stays `None` throughout,
status.json's `state` never moves off `"running"` (never `"aborted"`,
never any other terminal value -- this call never renders a verdict at
all), and `LoopSupervisor._step_worker` returns `("advance", "replan",
"stagnation_exhausted")` -- a transition `engine.transitions.TABLE["worker"]`
declares (`T("replan", "stagnation_exhausted")`), asserted here by name
through `transitions.is_legal`/`transitions.guard_names`, not merely by
matching the returned tuple. `stagnation_exhausted` is a PRE-EXISTING guard
(ledger guard 10, task 008/#60) that fires on repeated silence from the
worker for any reason -- this test's only claim is that an exhausted
contract ladder is read as exactly that ordinary silence (task 006's own
reasoning: "no `_advance` call, no `self._abort_reason`, no early
`return` -- this pass falls through to the SAME ordinary stagnation
bookkeeping"), not a special-cased abort. Both the approach (2 of 12) and
the iteration budget (138 of 900 charged when this returns) remain wide
open the moment the run continues past this point -- the run does not end
here at all, contract violation or not.

Hand-verified mutation: reverting task 006's cutover for the worker's
`"complete"` verdict site (putting back the inline `self._abort_reason =
message; return ("tail",)` pair instead of routing through
`contract_enforcement.enforce_contract`) makes
`test_an_exhausted_ladder_with_budget_and_approach_remaining_never_aborts`
fail immediately: `sup._abort_reason` is no longer `None` (it is set to
the `104`-is-not-a-string contract-violation message) and the returned
outcome is `("tail",)`, not `("advance", "replan", "stagnation_exhausted")`
-- the exact two assertions this module exists to make. Any one of the
five cutovers (tasks 003-007) reverted the same way breaks the same
invariant at its own site; this module drives the worker `"complete"`
site (004/006's shared branch) because it is `deck-phase3g`'s own site.
"""

from __future__ import annotations

import asyncio
import json

from ralphd.engine import state
from ralphd.engine import transitions
from ralphd.engine.config import JobConfig
from ralphd.engine.loop import LoopSupervisor
from ralphd.engine.runner import IterationResult
from ralphd.engine.state import RunDir, atomic_write_json

# The exact payload that killed `deck-phase3g`: a JSON scalar where the
# contract requires a string.
KILLING_PAYLOAD = json.dumps({"verdict": "complete", "task_id": 104})


class _RepeatedlyBrokenAgent:
    """Every call -- the real work iteration AND every uncharged
    `_ask_contract_fix` re-ask alike -- returns the SAME schema-violating
    payload. The ladder can never repair itself; every pass exhausts."""

    def __init__(self):
        self.calls = 0

    def interrupt(self) -> bool:
        return False

    async def run(self, prompt, transcript, **kw) -> IterationResult:
        self.calls += 1
        r = IterationResult(exit_code=0)
        r.final_text = ("Task 104's success criteria are met: all sites "
                        "converged, evidence quoted and committed.\n"
                        + KILLING_PAYLOAD)
        r.duration_s = 5.0
        r.usage = {"input": 10, "output": 5, "totalTokens": 15}
        return r


def _deck_phase3g_shaped_supervisor(tmp_path):
    """approach 2 of 12, iteration 135 of 900 -- `deck-phase3g`'s own
    shape at the moment its worker wrote the killing payload."""
    root = tmp_path / "run"
    root.mkdir(parents=True)
    run = RunDir(root=root)
    run.update_status(state="running")
    atomic_write_json(run.tasks_file, {"version": 1, "tasks": [
        {"id": "104", "title": "convert clientOpensCreateModalForAgent",
         "status": "in-progress",
         "successCriteria": "the targeted validation run exits 0"},
    ]})
    cfg = JobConfig(run_id="deck-phase3g-shaped", iterations=900,
                    max_approaches=12, vigilant=False, on_complete="exit",
                    infra_retry_backoff_s=[0.0], infra_retry_backoff_max_s=0.0,
                    infra_outage_budget_s=1000.0)
    sup = LoopSupervisor(cfg, run, root)
    sup._approach = 2
    sup.iterations_used = 134  # this pass is iteration 135
    agent = _RepeatedlyBrokenAgent()
    sup.runner = agent  # type: ignore[assignment]
    return sup, run, agent


def test_an_exhausted_ladder_with_budget_and_approach_remaining_never_aborts(
        tmp_path):
    sup, run, agent = _deck_phase3g_shaped_supervisor(tmp_path)

    outcome = asyncio.run(sup._step_worker())

    # -- the invariant itself --------------------------------------------
    assert sup._abort_reason is None, (
        "an exhausted contract ladder must never be the SOLE reason the "
        "run ends while budget and approach remain")
    assert run.read_status().get("state") == "running", (
        "the run must never be pushed to a terminal state by a contract "
        "violation alone")

    # -- the state machine advanced along a DECLARED edge -----------------
    assert outcome == ("advance", "replan", "stagnation_exhausted")
    assert transitions.is_legal("worker", "replan", vigilant=False)
    assert "stagnation_exhausted" in transitions.guard_names("worker", vigilant=False)

    # approach and iteration budget both remain wide open -- this is not
    # an ending, contract violation or not.
    assert sup._approach == 2
    assert sup.cfg.max_approaches == 12
    assert sup.iterations_used == 138  # 134 seeded + 4 charged passes (the
    # first records the one-time criteriaFingerprint baseline, task 008)
    assert sup.budget_left()

    # the illegal move was still genuinely refused throughout -- task 104
    # never silently completes just because the ladder gave up.
    task = run.read_tasks()["tasks"][0]
    assert task["status"] == "in-progress", \
        "a contract violation must not silently take the move anyway"

    # the ladder ran the FULL contract_retry_max + 1 attempts, four
    # separate times (once per charged pass) -- never fewer, never more.
    assert agent.calls == 4 * (sup.cfg.contract_retry_max + 1)

    events = state.read_events(run.root)
    violations = [e for e in events if e.get("type") == "contract.violation"]
    faults = [e for e in events if e.get("type") == "contract.agent_fault"]
    assert len(violations) == 4 * (sup.cfg.contract_retry_max + 1)
    assert len(faults) == 4
    assert all("task_id" in ev["errors"][0] for ev in violations)
