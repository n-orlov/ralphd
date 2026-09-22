"""Task 014 (#87): the boundary the incident actually happened at.

`selfdev-v011-model-per-hat`'s first window: the deadline fired mid-run, the
post-terminal `reflect` iteration returned no parseable `reflect` contract
payload, and the WINDOW'S TERMINAL RECORD came out `state: failed`,
`verdict: unverified` for a run whose work had 25/30 tasks validated and five
issues closed. The reflect *contract* failure became the *run's* outcome.

SUPERSEDED IN PART by task 016 (v0.18, PRD requirement F.3, #146): the work's
own terminal `verdict` this module drives via the `_interrupted` review
handler (`test_review_interrupted_not_rejection`'s own fixture) is, as of
that task, the dedicated `"unreviewed"` value instead of `"unverified"` --
see `tests/test_review_deadline_unreviewed_task016.py` for the new value's
own pinning, and `tests/test_deadline_beat_verdict_task006.py`'s own
SUPERSEDED note for the sibling fix this module never received until now.
The `state`/`reason`/`deadlineBeatVerdict` machinery and the reflect-failure
classification below are entirely unaffected -- only the three `"unverified"`
literals produced by the interrupted-review setup change to `"unreviewed"`.

This module drives that exact shape end to end, entirely through
`LoopSupervisor` with `sup.runner` stubbed (never `_run_iteration_once`,
per this run's standing rule):

1. `test_a_deadline_fired_garbage_reflect_leaves_the_works_outcome_intact` --
   budget/deadline exhaustion produces the work's own terminal record
   (`_run_job_core()`'s real `review_unjudged_at_budget_end` -> `("tail",)`
   path, requirement B/#72's `deadlineBeatVerdict` included), then the
   reflect iteration returns an unparseable contract payload (report.md
   exists, final_text is not JSON -- #87's own shape, not merely a missing
   report). Afterwards `status.json` still carries the work's
   `state`/`verdict`/`reason`/`deadlineBeatVerdict`, PLUS
   `reflect: {ok: false, error, errorClass: "degraded"}`, and
   `artifacts/reflection/FAILED.md` names the work's state without changing
   it.
2. `test_the_two_reflect_fault_causes_are_not_spelled_identically` -- #87's
   own suggested shape ("if a reflect contract failure should ever be able
   to fail a run, it needs its own distinguishable state or fault class"):
   `_reflect_failure()` returns a DIFFERENT class
   (`LoopSupervisor.REFLECT_FAULT_FATAL`) for a cause that is not "ran
   cleanly, bad content" (interrupted / timed out / never ran at all) than
   it does for #87's own DEGRADED cause, and the two spellings are literally
   different strings.
3. `test_the_mutation_a_flattened_class_would_have_missed` -- the paired
   mutation: collapsing both branches onto one spelling (what the code
   looked like before this task) makes test 2 fail, proving it is not
   vacuous.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from ralphd.engine.loop import LoopSupervisor
from ralphd.engine.runner import IterationResult
from test_review_interrupted_not_rejection import (
    _complete_task,
    _interrupted,
    _plan,
    _supervisor,
)


def _garbage_reflect_with_report(sup: LoopSupervisor, _prompt: str = "") -> IterationResult:
    """#87's own shape: the iteration ran to a clean exit and left a
    report.md on disk, but its final line is not a parseable `reflect`
    contract payload at all -- the exact incident text
    ("no parseable 'reflect' contract payload") this task's #87 quotes."""
    outdir = sup.run.artifacts_dir / "reflection"
    outdir.mkdir(parents=True, exist_ok=True)
    (outdir / "report.md").write_text("# stub post-mortem\n\nsomething useful.\n")
    r = IterationResult(exit_code=0)
    r.final_text = "wrote the report, forgot the contract payload"
    return r


def test_a_deadline_fired_garbage_reflect_leaves_the_works_outcome_intact(tmp_path):
    # -- 1. budget/deadline exhaustion produces the work's own terminal
    #    record: honoured COMPLETE, then review cut short with nothing left
    #    to retry with (requirement A/#72's own regression shape, reused
    #    verbatim from test_deadline_beat_verdict_task006.py) -----------------
    handlers = {
        "planning": [_plan],
        "worker": [_complete_task],
        "review": [_interrupted],
    }
    sup = _supervisor(tmp_path / "run", handlers, iterations=3, reflect=True,
                      # Task 012 (v0.14, #56 follow-up): `_run_reflection`
                      # deliberately clears status.json's `phase` field to
                      # `None` BEFORE `_reflect_contract_ladder` ever runs
                      # (see `_MalformedReflectRunner`'s own docstring in
                      # test_reflect_tail_budget.py -- an established,
                      # separately-tested contract, not a bug this module
                      # gets to relitigate). `_ScriptedAgent.run` here
                      # routes by that same `phase` field, so it cannot
                      # tell a re-ask attempt apart from any other
                      # phase-less call. Pinning `contract_retry_max=0`
                      # keeps this module's own scripted `_complete_task`/
                      # `_garbage_reflect_with_report` queues exactly as
                      # sized (one entry each) and still exercises the
                      # real DEGRADED outcome this module is about: one
                      # malformed reflect payload, zero re-asks granted.
                      contract_retry_max=0)
    state = asyncio.run(sup._run_job_core())

    assert state == "failed"
    mid_status = sup.run.read_status()
    # reflect: true -- the outcome is settled but not yet the terminal
    # `state` on disk (task 012, #56): the reflect window is still ahead.
    assert mid_status["state"] == "finishing"
    assert mid_status["pendingTerminalState"] == "failed"
    # task 016, #146 SUPERSEDED (and task 009, v0.24 requirement B.2, #169
    # narrowed it further): this run's review is cut short by an
    # interrupted iteration, which writes a null verdict, not a string.
    assert mid_status["verdict"] is None
    marker = mid_status.get("deadlineBeatVerdict")
    assert isinstance(marker, dict) and marker.get("deadline"), (
        "the work's own terminal record must carry requirement B's marker "
        "before reflect ever runs")

    # -- 2. the reflect iteration returns garbage --------------------------
    handlers["reflect"] = [_garbage_reflect_with_report]
    asyncio.run(sup._run_reflection())
    sup._promote_finishing_state()

    # -- 3. the work's own outcome is untouched -----------------------------
    final_status = sup.run.read_status()
    assert final_status["state"] == "failed"
    assert final_status["verdict"] is None
    assert final_status.get("pendingTerminalState") is None
    assert final_status["deadlineBeatVerdict"] == marker
    assert final_status["reason"] == mid_status["reason"]

    # -- 4. AND the reflect failure is recorded, distinguishably -----------
    reflect = final_status["reflect"]
    assert reflect["ok"] is False
    assert "no parseable 'reflect' contract payload" in reflect["error"]
    assert reflect["errorClass"] == LoopSupervisor.REFLECT_FAULT_DEGRADED

    tombstone = (sup.run.artifacts_dir / "reflection" / "FAILED.md").read_text()
    assert "no parseable 'reflect' contract payload" in tombstone
    assert f"class: {LoopSupervisor.REFLECT_FAULT_DEGRADED}" in tombstone
    # names the work's state, does not (and structurally cannot) change it
    assert "terminal state: failed" in tombstone
    # task 009 (v0.24 requirement B.2, #169): the word this names is now
    # `None`, not the "unreviewed" string it used to be.
    assert "verdict None" in tombstone
    assert final_status["state"] == "failed"  # re-checked after reading FAILED.md


def test_the_two_reflect_fault_causes_are_not_spelled_identically(tmp_path):
    """#87's own suggested shape: a decision, implemented and named, that a
    reflection failure meant to be fatal is spelled differently from #87's
    own degraded/unparseable cause."""
    sup = _supervisor(tmp_path / "run", {}, reflect=True)

    assert LoopSupervisor.REFLECT_FAULT_DEGRADED != LoopSupervisor.REFLECT_FAULT_FATAL

    # #87's own cause: ran cleanly, left an unparseable/missing deliverable.
    degraded_cases = [
        IterationResult(exit_code=0),  # never writes report.md at all
    ]
    garbage = IterationResult(exit_code=0)
    garbage.final_text = "not a reflect contract payload"
    degraded_cases.append(garbage)
    for result in degraded_cases:
        error, cls = sup._reflect_failure(result)
        assert error is not None
        assert cls == LoopSupervisor.REFLECT_FAULT_DEGRADED, (result, cls)

    # A different cause: the iteration itself never produced a trustworthy
    # attempt -- never ran, was interrupted, timed out, errored, exited
    # nonzero. Every one of these must be spelled FATAL, never DEGRADED.
    never_ran = None
    interrupted = IterationResult(exit_code=None, interrupted=True)
    timed_out = IterationResult(exit_code=None, timed_out=True)
    errored = IterationResult(exit_code=0, error_message="Connection error.")
    nonzero = IterationResult(exit_code=17)
    for result in (never_ran, interrupted, timed_out, errored, nonzero):
        error, cls = sup._reflect_failure(result)
        assert error is not None
        assert cls == LoopSupervisor.REFLECT_FAULT_FATAL, (result, cls)

    # and a genuine success has neither class at all.
    outdir = sup.run.artifacts_dir / "reflection"
    outdir.mkdir(parents=True, exist_ok=True)
    (outdir / "report.md").write_text("# ok\n")
    ok_result = IterationResult(exit_code=0)
    ok_result.final_text = json.dumps({"report_written": True,
                                       "suggestions_written": False})
    error, cls = sup._reflect_failure(ok_result)
    assert error is None and cls is None


def test_the_mutation_a_flattened_class_would_have_missed(tmp_path):
    """Paired mutation: undo the split (collapse both branches onto the
    same spelling, the shape this code had before task 014) and show the
    previous test's own discriminating assertion then fails -- proving it
    is not vacuously true of any two-value return."""
    sup = _supervisor(tmp_path / "run", {}, reflect=True)
    real_failure = sup._reflect_failure

    def flattened(result):
        error, _cls = real_failure(result)
        if error is None:
            return None, None
        return error, LoopSupervisor.REFLECT_FAULT_DEGRADED  # always the same word

    sup._reflect_failure = flattened  # type: ignore[assignment]

    error, cls = sup._reflect_failure(IterationResult(exit_code=None, interrupted=True))
    assert error is not None
    with pytest.raises(AssertionError):
        assert cls == LoopSupervisor.REFLECT_FAULT_FATAL
