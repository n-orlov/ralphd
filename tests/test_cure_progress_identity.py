"""Task 007 (PRD v0.27 requirement B.4, #274): `_review_cure_pass_
available`'s second-or-later-rejection no-progress comparison, on a
CONFIRMATION review, is by FINDING IDENTITY, never by `clause` wording.

Before this task, EVERY rejection past the first pass compared
`findings.blocking_finding_clauses` -- the literal `clause` text set --
against the immediately preceding granted pass's own recorded set
(`cureBlocking`). That comparison walks straight past a review that
re-files the SAME still-open finding with a reworded `clause` between
passes: the text differs, so the set is "not a superset", and a cure
pass a genuinely unmoved wall does not deserve is granted anyway.

A confirmation review's own linking obligation (task 006, B.3) already
gives every reopened finding a STABLE identity that survives a
rewording: the `reopens` cure task id it names, followed back through
`tasks[].reopensCureTaskId` (`state.root_cure_task_id`) to the root cure
task the whole chain descends from. This module drives a real
`LoopSupervisor._step_review()` through that identity comparison and
proves the ordinary (non-confirmation, `passes_taken == 0`) grant path
is untouched.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from ralphd.engine import findings
from ralphd.engine.runner import IterationResult
from ralphd.engine.state import atomic_write_json

from tests.conftest import supervisor_factory

_supervisor = supervisor_factory(
    run_id="task007-cure-progress-identity", cfg_defaults=False,
    status={"state": "running"},
    prd="# task 007 fixture PRD\n\nDo the thing.\n", max_approaches=3,
    operator_input_needed_pause=False)


def _finding(clause: str, evidence: str, cured_when: str | None = None,
            **links) -> dict:
    payload = {"severity": "blocking", "curable": True, "clause": clause,
              "evidence": evidence}
    payload.update(links)
    if cured_when is None:
        cured_when = f"{clause}: {evidence} is fixed"
    payload["cured_when"] = cured_when
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


def _cure_tasks(run) -> list[dict]:
    return [t for t in run.read_tasks()["tasks"]
           if t.get("origin") == "review-cure"]


def _bounded(coro, timeout: float = 30.0):
    """Every drive of `_step_review()` below goes through this, not a bare
    `asyncio.run(sup._step_review())` -- `tests/test_loop_driving_await_
    guard.py` (task 073, steer 001 §2) fails any NEW module that
    introduces an unbounded loop-driving await, exactly the shape
    `tests/test_peer_verdict_gate.py`'s own `_bounded` helper (task 072)
    already exists to avoid.
    """
    return asyncio.run(asyncio.wait_for(coro, timeout))


def _take_first_pass(root: Path) -> tuple:
    """Pass 1 (an ordinary, non-confirmation review -- the approach's
    very first rejection) targets TWO independent findings, A and B, so a
    later confirmation review has a genuine strict subset available to
    reopen. Returns `(sup, cure_a_id, cure_b_id)`."""
    sup = _supervisor(root, max_approaches=3)
    sup._approach = 1
    atomic_write_json(sup.run.tasks_file, {"version": 1, "tasks": []})

    first = [
        _finding("requirement C.1 (#141)", "first counterexample for A"),
        _finding("requirement D (#167)", "first counterexample for B"),
    ]
    reviewer = _rejects_with(first)
    reviewer.sup = sup  # type: ignore[attr-defined]
    sup.runner.run = reviewer  # type: ignore[method-assign]

    outcome = _bounded(sup._step_review())
    assert outcome == ("advance", "cure", "rejected_curable_pass_available")
    assert sup.run.read_status().get("curePasses") == {"1": 1}

    cure_tasks = _cure_tasks(sup.run)
    assert len(cure_tasks) == 2
    cure_a = next(t["id"] for t in cure_tasks
                 if t["title"] == "requirement C.1 (#141)")
    cure_b = next(t["id"] for t in cure_tasks
                 if t["title"] == "requirement D (#167)")
    return sup, cure_a, cure_b


# ---------------------------------------------------------------------------
# 1. A rejection reopening a strict subset of the previous pass's own
#    targeted set is granted another pass.
# ---------------------------------------------------------------------------


def test_a_rejection_reopening_a_strict_subset_is_granted_another_pass(
        tmp_path):
    root = tmp_path / "run"
    sup, cure_a, cure_b = _take_first_pass(root)

    # Confirmation review: only A is claimed still open, worded quite
    # differently from pass 1's own clause for it -- `{root(A)}` is a
    # STRICT SUBSET of pass 1's own `{root(A), root(B)}` (B is not
    # reopened at all), so this earns pass 2 regardless of wording.
    reopen_a = [_finding("the counterexample for C.1 persists",
                        "still not fixed, worded differently",
                        reopens=cure_a)]
    reviewer = _rejects_with(reopen_a)
    reviewer.sup = sup  # type: ignore[attr-defined]
    sup.runner.run = reviewer  # type: ignore[method-assign]

    outcome = _bounded(sup._step_review())

    assert outcome == ("advance", "cure", "rejected_curable_pass_available")
    assert sup.run.read_status().get("curePasses") == {"1": 2}
    cure_tasks = _cure_tasks(sup.run)
    assert len(cure_tasks) == 3
    new_task = next(t for t in cure_tasks if t["id"] not in (cure_a, cure_b))
    assert new_task["reopensCureTaskId"] == cure_a


# ---------------------------------------------------------------------------
# 2. One finding, reopened twice with reworded clauses, is refused a
#    third pass -- the identity is the same both times, whatever the
#    clause says.
# ---------------------------------------------------------------------------


def test_one_finding_reopened_twice_with_reworded_clauses_is_refused_a_third_pass(
        tmp_path):
    root = tmp_path / "run"
    sup, cure_a, cure_b = _take_first_pass(root)

    reopen_a_1 = [_finding("the counterexample for C.1 persists",
                          "still not fixed, worded differently",
                          reopens=cure_a)]
    reviewer1 = _rejects_with(reopen_a_1)
    reviewer1.sup = sup  # type: ignore[attr-defined]
    sup.runner.run = reviewer1  # type: ignore[method-assign]

    outcome1 = _bounded(sup._step_review())
    assert outcome1 == ("advance", "cure", "rejected_curable_pass_available")
    assert sup.run.read_status().get("curePasses") == {"1": 2}
    # v0.36 C.2: two rounds taken, two allowed -- the third is refused by
    # the approach's round count (a no-progress rejection with a round left
    # cures: tests/test_cure_rounds_per_approach.py).
    sup.cfg.cure_pass_ceiling = 2

    cure_a_2 = next(t["id"] for t in _cure_tasks(sup.run)
                    if t.get("reopensCureTaskId") == cure_a)

    # A THIRD rejection reopens the SAME finding again, worded a THIRD
    # way -- neither rewording matches the previous pass's own clause
    # text (`blocking_finding_clauses` would see a disjoint set and, on
    # the old text comparison, wrongly grant a third pass), but the
    # identity -- `root_cure_task_id` resolves `cure_a_2` back to
    # `cure_a`, exactly the previous pass's own sole target -- is
    # unchanged, so this is refused.
    reopen_a_2 = [_finding("yet another way of saying C.1 is still broken",
                          "still not fixed, worded a third time",
                          reopens=cure_a_2)]
    reviewer2 = _rejects_with(reopen_a_2)
    reviewer2.sup = sup  # type: ignore[attr-defined]
    sup.runner.run = reviewer2  # type: ignore[method-assign]

    outcome2 = _bounded(sup._step_review())

    assert outcome2 == ("advance", "replan", "rejected_approach_left")
    assert sup.run.read_status().get("curePasses") == {"1": 2}  # unchanged
    # 3 cure tasks total after passes 1 (A, B) and 2 (A's reopen) --
    # no FOURTH cure task appended for this refused third pass.
    assert len(_cure_tasks(sup.run)) == 3
    assert sup.run.composite_prd_file.exists()  # the replan side effect ran


# ---------------------------------------------------------------------------
# 3. First-review (clause superset) behaviour is unchanged: the very
#    first curable rejection of a fresh approach is still granted
#    unconditionally -- `passes_taken == 0` short-circuits before either
#    comparison (text or identity) ever runs.
# ---------------------------------------------------------------------------


def test_first_review_of_an_approach_is_still_granted_unconditionally(
        tmp_path):
    root = tmp_path / "run"
    sup = _supervisor(root, max_approaches=3)
    sup._approach = 1
    atomic_write_json(sup.run.tasks_file, {"version": 1, "tasks": []})

    first = [_finding("requirement C.1 (#141)", "first counterexample")]
    reviewer = _rejects_with(first)
    reviewer.sup = sup  # type: ignore[attr-defined]
    sup.runner.run = reviewer  # type: ignore[method-assign]

    outcome = _bounded(sup._step_review())

    assert outcome == ("advance", "cure", "rejected_curable_pass_available")
    assert sup.run.read_status().get("curePasses") == {"1": 1}
    assert len(_cure_tasks(sup.run)) == 1
