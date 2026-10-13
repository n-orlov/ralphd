"""cure-01-04 (PRD v0.27 requirement B.4/B.5, DoD 2): on a CONFIRMATION
review, reopening every target of the previous granted pass is not
progress -- irrespective of whether that previous pass was granted by
review's own cure-pass edge or by the peer verdict gate's separate one.

Before this task, `LoopSupervisor._review_rejection_curable_ignoring_
ceiling` read `_cure_passes_for_approach` -- review's OWN `curePasses`
counter -- and returned `True` (the unconditional first-pass shortcut)
whenever that counter read `0`, BEFORE ever checking `_is_confirmation_
review()`. A gate-origin cure pass increments `gateCurePasses`, never
`curePasses`, so an approach whose only granted pass came from the peer
gate still reads `curePasses[approach] == 0` on review's very next look
-- even though `_is_confirmation_review()` is correctly `True` (it reads
`cure-lineage.json`, which both channels append to via `state.insert_
cure_tasks`). The old order let that first-pass shortcut fire ahead of
the confirmation check, granting a second, no-progress pass even when
review reopens the gate's sole targeted finding, unchanged in substance
and merely reworded.

This module drives the real `LoopSupervisor._step_review()` through
that exact sequence: a gate-origin cure pass, then a review rejection
that reopens the gate pass's sole target with a different clause. Every
node here is red without this task's reordering fix -- the old code
grants `rejected_curable_pass_available` and a second `curePasses` entry
for a rejection that reopens the entire previous targeted set.
"""

from __future__ import annotations

import asyncio
import json

from ralphd.engine import findings
from ralphd.engine.runner import IterationResult
from ralphd.engine.state import atomic_write_json

from tests.conftest import supervisor_factory

_supervisor = supervisor_factory(
    run_id="cure01-04-gate-confirmation-identity", cfg_defaults=False,
    status={"state": "running"},
    prd="# cure-01-04 fixture PRD\n\nDo the thing.\n", max_approaches=3,
    operator_input_needed_pause=False)


def _finding(clause: str, evidence: str, **links) -> dict:
    payload = {"severity": "blocking", "curable": True, "clause": clause,
              "evidence": evidence}
    payload.update(links)
    payload["cured_when"] = f"{clause}: {evidence} is fixed"
    return payload


def _result(text: str) -> IterationResult:
    r = IterationResult(exit_code=0)
    r.final_text = text
    r.duration_s = 5.0
    r.usage = {"input": 10, "output": 5, "totalTokens": 15}
    return r


def _rejects_with(findings_list: list[dict]):
    async def _review(prompt, transcript, **kw):
        findings.write_findings_json(
            _review.sup.run.findings_json_file, findings_list)
        return _result(json.dumps({"verdict": "rejected", "reason": "no"}))
    return _review


def _bounded(coro, timeout: float = 30.0):
    """`tests/test_loop_driving_await_guard.py` fails any new module that
    drives `_step_review()` with a bare, unbounded `asyncio.run` -- the
    same discipline `tests/test_cure_progress_identity.py`'s own
    `_bounded` helper already exists to satisfy.
    """
    return asyncio.run(asyncio.wait_for(coro, timeout))


def test_b4_gate_origin_pass_also_counts_for_confirmation_progress(
        tmp_path):
    root = tmp_path / "run"
    sup = _supervisor(root, max_approaches=3)
    sup._approach = 1
    atomic_write_json(sup.run.tasks_file, {"version": 1, "tasks": []})

    # Step 1: the peer verdict gate grants ONE cure pass over a single
    # blocking finding -- never review's own channel.
    gate_finding = _finding("requirement B.4 (#274)",
                            "the gate found the sole gap review missed")
    findings.write_findings_json(sup.run.verdict_gate_findings_json_file,
                                 [gate_finding])
    sup._take_gate_cure_pass(1)
    cure_tasks = [t for t in sup.run.read_tasks()["tasks"]
                 if t.get("origin") == "review-cure"]
    assert len(cure_tasks) == 1
    cure_task_id = cure_tasks[0]["id"]

    status = sup.run.read_status()
    assert status.get("gateCurePasses") == {"1": 1}
    assert not status.get("curePasses")  # review's own counter untouched

    # Its cure task lands and is worked to completion.
    tasks_doc = sup.run.read_tasks()
    for t in tasks_doc["tasks"]:
        if t["id"] == cure_task_id:
            t["status"] = "validated"
    atomic_write_json(sup.run.tasks_file, tasks_doc)

    # This IS a confirmation review: some channel (the gate) already
    # granted a pass for this approach.
    assert sup._is_confirmation_review() is True

    # Step 2: review rejects, reopening the gate pass's SOLE target --
    # worded completely differently from the gate's own clause -- and
    # names nothing else. Reopening the entire previous targeted set is
    # no progress, whichever channel granted that previous pass.
    reopen = [_finding("the gap the gate found is still present",
                       "reworded, but the same underlying gap",
                       reopens=cure_task_id)]
    reviewer = _rejects_with(reopen)
    reviewer.sup = sup  # type: ignore[attr-defined]
    sup.runner.run = reviewer  # type: ignore[method-assign]

    # v0.36 C.2: the gate's one granted round is the approach's whole
    # allowance, so the round count refuses the next pass (a no-progress
    # rejection with a round left cures: tests/test_cure_rounds_per_approach.py).
    sup.cfg.cure_pass_ceiling = 1
    outcome = _bounded(sup._step_review())

    # Refused: NOT another cure pass. The old, unfixed order granted
    # `("advance", "cure", "rejected_curable_pass_available")` here
    # because review's own `curePasses[1]` still read `0`.
    assert outcome != ("advance", "cure", "rejected_curable_pass_available")
    assert outcome == ("advance", "replan", "rejected_approach_left")

    # Neither cure-pass counter grew, and no new cure task was appended
    # for this refused rejection.
    status_after = sup.run.read_status()
    assert not status_after.get("curePasses")
    assert status_after.get("gateCurePasses") == {"1": 1}
    cure_tasks_after = [t for t in sup.run.read_tasks()["tasks"]
                        if t.get("origin") == "review-cure"]
    assert len(cure_tasks_after) == 1
    assert sup.run.composite_prd_file.exists()  # the replan side effect ran


def test_strict_subset_reopen_after_a_gate_pass_still_earns_a_pass(
        tmp_path):
    """Control: a confirmation review after a gate-origin pass that
    reopens a STRICT SUBSET (here, nothing at all -- a fresh, unrelated
    problem with no `reopens`) is still granted, exactly as the identity
    rule intends. Proves the fix did not turn every post-gate-pass review
    into a refusal outright -- only a superset reopen is refused.
    """
    root = tmp_path / "run"
    sup = _supervisor(root, max_approaches=3)
    sup._approach = 1
    atomic_write_json(sup.run.tasks_file, {"version": 1, "tasks": []})

    gate_finding = _finding("requirement B.4 (#274)",
                            "the gate found the sole gap review missed")
    findings.write_findings_json(sup.run.verdict_gate_findings_json_file,
                                 [gate_finding])
    sup._take_gate_cure_pass(1)
    cure_tasks = [t for t in sup.run.read_tasks()["tasks"]
                 if t.get("origin") == "review-cure"]
    cure_task_id = cure_tasks[0]["id"]
    tasks_doc = sup.run.read_tasks()
    for t in tasks_doc["tasks"]:
        if t["id"] == cure_task_id:
            t["status"] = "validated"
    atomic_write_json(sup.run.tasks_file, tasks_doc)
    assert sup._is_confirmation_review() is True

    # A fresh, unrelated finding with `regression: true` -- no `reopens`
    # at all, so it contributes no root to the identity comparison: the
    # reopened-root set is empty, a strict subset of the gate pass's own
    # `{root(cure_task_id)}`.
    fresh = [_finding("a brand-new problem introduced since the cure",
                      "unrelated to the gate's own finding",
                      regression=True)]
    reviewer = _rejects_with(fresh)
    reviewer.sup = sup  # type: ignore[attr-defined]
    sup.runner.run = reviewer  # type: ignore[method-assign]

    outcome = _bounded(sup._step_review())

    assert outcome == ("advance", "cure", "rejected_curable_pass_available")
    status_after = sup.run.read_status()
    assert status_after.get("curePasses") == {"1": 1}
    assert status_after.get("gateCurePasses") == {"1": 1}
