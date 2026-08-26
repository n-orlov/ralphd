"""Requirement A (#72): an interrupted review iteration is not a rejection.

v0.9's live incident, run `selfdev-v09-fsm-loop` iteration 183: `_step_review`
read an `IterationResult` the engine itself cut short (`interrupted=True`, no
parseable payload at all) through the exact same code path as an ordinary
`{"verdict": "rejected"}` reply -- `_review_verdict` saw no parseable payload,
called it "not verified", and `_step_review` archived the approach, logged
`review rejected approach N`, wrote a composite PRD for a successor approach,
and advanced `status.json`'s `approach` counter. The clock cutting an
iteration short was scored as review's own judgement.

This module pins the fix (`LoopSupervisor._review_interrupted`,
`_run_review_iteration`, `_review_unjudged_at_budget_end`, and the two new
`transitions.TABLE["review"]` edges `review_interrupted` /
`review_unjudged_at_budget_end` task 002 declared):

1. `test_interrupted_review_with_budget_left_reasks_in_place_and_does_not_...`
   -- budget/wall clock still available: review stays in `review`, asks
   again, the approach is not touched.
2. `test_v09_iteration_183_regression_interrupted_review_at_budget_end...`
   -- the named regression: honoured COMPLETE, then review, then the engine
   interrupts review with nothing left to retry with. Cites the incident.
3. `test_mutation_stops_discriminating_interrupt_is_read_as_ordinary_verdict`
   -- the paired mutation: patch `_review_interrupted` to always return
   `False` (i.e. undo the fix) and show the SAME scenario now DOES archive
   the approach, DOES log `review rejected`, and DOES advance the approach
   counter -- proving test 2 actually exercises the discriminating logic
   rather than passing by construction.
4. `test_review_verdict_unchanged_for_a_completed_iteration_with_bad_payload`
   -- pins `_review_verdict`'s existing contract semantics: for an iteration
   that ran to completion, an absent/malformed payload is still an ordinary
   non-verified verdict, and evaluating it never raises.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from ralphd.engine.config import JobConfig
from ralphd.engine.loop import LoopSupervisor
from ralphd.engine.runner import COMPLETE, IterationResult
from ralphd.engine.state import RunDir, atomic_write_json, read_events


def _ok(text: str = "working") -> IterationResult:
    r = IterationResult(exit_code=0)
    r.final_text = text
    r.duration_s = 30.0
    r.usage = {"input": 10, "output": 5, "totalTokens": 15}
    return r


def _interrupted(sup: LoopSupervisor | None = None, _prompt: str = "") -> IterationResult:
    """The engine's own SIGINT-shaped cutoff -- fact 9, `IterationResult`'s
    own fields.

    Signature mirrors `_plan`/`_complete_task` on purpose: `_ScriptedAgent.run`
    invokes every handler as `step(self.sup, prompt)` -- two positional args.
    An earlier draft of this handler took none, so every call raised
    `TypeError` inside the runner, which the engine classifies as an `infra`
    fault (refunded, zero backoff in this fixture) and retried forever
    without ever reaching the code this module exists to test -- see
    steer 002 for the full incident. Keep the shape aligned with its
    siblings.

    Carries a non-empty `final_text`/`usage` on purpose: `_classify_result`
    (`loop.py:1002`) computes `produced_traffic = bool(final_text) or
    bool(usage)`, and `classify_fault` only reaches the `interrupted`
    branch it scores `FAULT_CLASS_SIGNAL` (neither `infra` nor `work`) when
    traffic WAS produced -- a no-traffic interrupt is unconditionally
    `infra` and gets silently retried/refunded by
    `_run_iteration_with_infra_retry` BEFORE `_step_review` ever sees it,
    which would make this module test the pre-existing generic infra
    retry instead of the review-specific fix task 003 actually adds.
    """
    r = IterationResult(exit_code=None)
    r.interrupted = True
    r.duration_s = 5.0
    r.final_text = "partial output before the engine cut this attempt short"
    r.usage = {"input": 3, "output": 1, "totalTokens": 4}
    return r


class _ScriptedAgent:
    def __init__(self, sup: LoopSupervisor, handlers: dict):
        self.sup = sup
        self.handlers = handlers
        self.calls = 0
        self.running = False

    def interrupt(self) -> bool:
        return self.running

    async def run(self, prompt, transcript, **kw) -> IterationResult:
        self.calls += 1
        phase = self.sup.run.read_status().get("phase")
        self.running = True
        try:
            queue = self.handlers.get(phase)
            if queue is None:
                raise AssertionError(
                    f"no scripted handler registered for phase {phase!r} "
                    f"(call {self.calls})")
            if isinstance(queue, list):
                # Steer 002: an exhausted queue must fail loudly rather than
                # raise IndexError, which the engine would otherwise
                # classify as an `infra` fault (refunded, unbounded retry)
                # indistinguishable from a real provider outage.
                if not queue:
                    raise AssertionError(
                        f"{phase} asked for a {self.calls}th time; script "
                        f"had no entries left for phase {phase!r}")
                step = queue.pop(0)
            else:
                step = queue
            return step(self.sup, prompt)
        finally:
            self.running = False


def _plan(sup: LoopSupervisor, _prompt: str = "") -> IterationResult:
    atomic_write_json(sup.run.root / "plan-proposal.json", {"version": 1, "tasks": [
        {"id": "001", "title": "do the thing",
         "successCriteria": "the thing is done"},
    ]})
    return _ok("planned\n" +
              json.dumps({"proposal_path": "plan-proposal.json", "summary": "planned"}))


def _complete_task(sup: LoopSupervisor, _prompt: str = "") -> IterationResult:
    doc = sup.run.read_tasks()
    doc["tasks"][0]["status"] = "completed"
    atomic_write_json(sup.run.tasks_file, doc)
    # `IterationResult.saw_complete` is a read-only property derived from
    # `final_text`'s own last line (task 046, #55) -- the marker has to
    # actually be the message's last line.
    return _ok(f"done\n{COMPLETE}")


def _supervisor(root: Path, handlers: dict, **cfg_kw) -> LoopSupervisor:
    root.mkdir(parents=True, exist_ok=True)
    run = RunDir(root=root)
    run.update_status(state="starting")
    # The mutation test (test 3) undoes the fix and falls all the way
    # through to the ordinary rejected-verdict path, which calls
    # `_write_composite_prd` -- that reads `run.prd_file` unconditionally,
    # so every scenario in this module needs one on disk even though the
    # fixed tests never reach that call.
    run.prd_file.write_text("# PRD\n\nDo the thing.\n")
    kw = {"max_approaches": 2, "vigilant": False,
          "on_complete": "exit", "infra_retry_backoff_s": [0.0],
          "infra_retry_backoff_max_s": 0.0, "infra_outage_budget_s": 1000.0,
          **cfg_kw}
    sup = LoopSupervisor(JobConfig(run_id="task003-review-interrupt", **kw),
                         run, root)
    sup.runner = _ScriptedAgent(sup, handlers)  # type: ignore[assignment]

    async def no_backoff(seconds):
        # Steer 002: report the requested `seconds` as WAITED (matching
        # `_wait_out_backoff`'s real contract) while actually sleeping
        # none, so a misbehaving stub still bounds a hot retry loop via the
        # outage budget instead of spinning forever with 0s ever accruing.
        return seconds, False

    sup._wait_out_backoff = no_backoff  # type: ignore[method-assign]
    return sup


def _rejected_events(run: RunDir) -> list[dict]:
    return [e for e in read_events(run.root, types=("log",))
            if "review rejected approach" in (e.get("message") or "")]


# ---------------------------------------------------------------------------
# 1. budget/wall clock left: self-loop, no cost to the approach
# ---------------------------------------------------------------------------

def test_interrupted_review_with_budget_left_reasks_in_place_and_costs_nothing(tmp_path):
    handlers = {
        "planning": [_plan],
        "worker": [_complete_task],
        # first review attempt is cut short; plenty of budget remains, so
        # the second one is asked, and it verifies.
        "review": [_interrupted,
                  lambda sup, prompt: _ok("looks right\n" +
                                          json.dumps({"verdict": "verified"}))],
    }
    sup = _supervisor(tmp_path / "run", handlers, iterations=10)
    state = asyncio.run(sup._run_job_core())

    assert state == "succeeded"
    assert sup.runner.calls == 4  # plan, worker, review(interrupted), review(verified)
    # `RunDir.__post_init__` always creates an empty `approaches/` container
    # (state.py:3506); what must NOT exist is the archived approach itself.
    assert not (sup.run.root / "approaches" / "01").exists()
    assert _rejected_events(sup.run) == []
    assert sup.run.read_status()["approach"] == 1


# ---------------------------------------------------------------------------
# 2. the named regression: v0.9's exact sequence, iteration 183
# ---------------------------------------------------------------------------

def test_v09_iteration_183_regression_interrupted_review_at_budget_end_is_not_a_rejection(tmp_path):
    """Reproduces `selfdev-v09-fsm-loop` iteration 183 exactly: a worker
    honours `<promise>COMPLETE</promise>` with every task genuinely
    completed, review is entered, and the review iteration itself is
    interrupted by the engine with NO budget left to ask again (the run's
    `iterations` budget is exhausted by this exact attempt). Before this
    task's fix, `_step_review` read the empty/absent payload as an ordinary
    rejected verdict: it archived the approach, emitted `review rejected
    approach 1`, wrote a composite PRD, and (approach 1 < max_approaches)
    advanced `status.json`'s `approach` to 2. None of that may happen now.
    """
    handlers = {
        "planning": [_plan],
        "worker": [_complete_task],
        "review": [_interrupted],
    }
    # exactly 3 iterations: plan, worker(COMPLETE), review(interrupted) --
    # the budget is gone the instant that third attempt returns, so there
    # is nothing left to re-ask with.
    sup = _supervisor(tmp_path / "run", handlers, iterations=3)
    state = asyncio.run(sup._run_job_core())

    assert state == "failed"  # no verdict was ever scored; not "succeeded"
    assert sup.runner.calls == 3
    # the approach was NOT archived (see the note on the empty container in
    # test 1 above -- `approaches/01` specifically must not exist)
    assert not (sup.run.root / "approaches" / "01").exists()
    assert not sup.run.composite_prd_file.exists()
    # no `review rejected approach N` event was ever emitted
    assert _rejected_events(sup.run) == []
    # the approach counter did NOT advance
    status = sup.run.read_status()
    assert status["approach"] == 1
    assert status["verdict"] != "verified"
    assert "review" in (status.get("reason") or "").lower()


# ---------------------------------------------------------------------------
# 3. the paired mutation: stop discriminating, and the same scenario now
#    DOES read the interrupt as an ordinary rejection
# ---------------------------------------------------------------------------

def test_mutation_stops_discriminating_interrupt_is_read_as_ordinary_verdict(
        tmp_path, monkeypatch):
    handlers = {
        "planning": [_plan],
        "worker": [_complete_task],
        # A second entry: once the mutation removes the discrimination,
        # the ordinary rejected-verdict path replans onto approach 2, whose
        # `_step_planning` immediately finds the iteration budget already
        # exhausted -- and since approach 1's worker already marked every
        # task completed (tasks.json persists across the replan; nothing
        # in this scripted scenario resets it), `_maybe_grace_review` (task
        # 002) grants ONE off-budget review call before the job ends. An
        # exhausted queue there would raise (steer 002's own trap, one
        # level up: an unbounded infra-fault retry against a 0.0 backoff
        # schedule that never accrues outage budget either) -- so the
        # grace call needs an entry of its own, not because the test
        # cares what it says.
        "review": [_interrupted,
                  lambda sup, prompt: _ok("grace review: still not enough\n" +
                                          json.dumps({"verdict": "rejected",
                                                     "reason": "insufficient evidence"}))],
    }
    sup = _supervisor(tmp_path / "run", handlers, iterations=3)
    # Undo the fix: an interrupted iteration is no longer distinguished from
    # one that ran to completion and merely replied with nothing parseable.
    monkeypatch.setattr(
        LoopSupervisor, "_review_interrupted", staticmethod(lambda result: False))

    state = asyncio.run(sup._run_job_core())

    # With the discrimination removed, the same interrupted iteration is
    # scored as an ordinary rejected verdict: the approach IS archived, the
    # rejection event IS emitted, and (approach 1 < max_approaches=2) the
    # run replans onto approach 2 rather than going straight to `finishing`.
    assert (sup.run.root / "approaches" / "01").exists()
    assert _rejected_events(sup.run) != []
    status = sup.run.read_status()
    assert status["approach"] == 2
    # this asserts the MUTATED behaviour differs from the fixed test above --
    # a real assertion that would FAIL against the fix, proving test 2 is
    # not vacuous.
    assert state != "succeeded"


# ---------------------------------------------------------------------------
# 4. `_review_verdict`'s unchanged semantics for a completed iteration
# ---------------------------------------------------------------------------

def test_review_verdict_unchanged_for_a_completed_iteration_with_bad_payload(tmp_path):
    sup = _supervisor(tmp_path / "run", {}, iterations=5)

    # absent payload (plain chatter, no JSON at all)
    verified, payload, errors = sup._review_verdict(_ok("just some prose"))
    assert verified is False
    assert payload is None
    assert errors

    # malformed payload (JSON, but not an object -- e.g. a bare list)
    verified, payload, errors = sup._review_verdict(_ok("[1, 2, 3]"))
    assert verified is False
    assert payload is None
    assert errors

    # schema-violating payload (object, but no recognised verdict field)
    verified, payload, errors = sup._review_verdict(
        _ok(json.dumps({"not_a_verdict": "nope"})))
    assert verified is False
    assert errors

    # a `rejected` verdict with no reason (transitions.review_report_errors)
    verified, payload, errors = sup._review_verdict(
        _ok(json.dumps({"verdict": "rejected"})))
    assert verified is False
    assert errors

    # none of the above crashed -- that is the whole point of fact 7.
