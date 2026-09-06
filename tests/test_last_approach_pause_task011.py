"""Task 011 (PRD requirement D.1/D.2, #142): "the last approach cannot end
`failed` on paperwork" -- on the FINAL approach (`approach ==
max_approaches`), a `rejected` review whose blocking findings are ALL
curable takes C's cure pass when one is still available (unchanged
behaviour, task 009), and otherwise a newly declared `review -> review`
self-loop edge (`rejected_last_approach`) that pauses the run through the
EXISTING pause machinery (`self.pause()`), emits `operator_escalation` with
`reason: rejected_last_approach`, and leaves the approach unarchived so a
`resume` (after an `amend`/`--waive`) re-reviews the SAME approach rather
than ending `failed` or reaching a nonexistent fresh approach. A `rejected`
review with any `curable: false` blocking finding on the last approach
still takes `rejected_no_approach_left` -> `failed` (requirement D.2,
already the pre-existing behaviour -- pinned here too since this module is
this task's own dedicated coverage).

Everything here drives a real `LoopSupervisor._step_review()` -- the same
shape `tests/test_rejected_incurable_edge.py` and
`tests/test_cure_pass_task009.py` already use -- never a mock of the guard
itself.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from ralphd.engine import findings, transitions
from ralphd.engine.config import JobConfig
from ralphd.engine.loop import LoopSupervisor
from ralphd.engine.runner import IterationResult
from ralphd.engine.state import RunDir
from tests.conftest import supervisor_factory


def _finding(severity: str, curable: bool, clause: str, evidence: str) -> dict:
    return {"severity": severity, "curable": curable, "clause": clause,
            "evidence": evidence}


def _result(text: str) -> IterationResult:
    r = IterationResult(exit_code=0)
    r.final_text = text
    r.duration_s = 5.0
    r.usage = {"input": 10, "output": 5, "totalTokens": 15}
    return r


_supervisor = supervisor_factory(run_id="task011-last-approach-pause", cfg_defaults=False,
                                 prd="# task 011 fixture PRD\n\nDo the thing.\n",
                                 status={"state": "running"}, max_approaches=1)


class _StubNotifier:
    def __init__(self) -> None:
        self.messages: list[str] = []

    def __call__(self, text: str) -> None:
        self.messages.append(text)


def _events(root: Path, type_: str) -> list[dict]:
    log = root / "events.jsonl"
    lines = log.read_text().splitlines() if log.exists() else []
    return [ev for ev in (json.loads(x) for x in lines) if ev.get("type") == type_]


# ---------------------------------------------------------------------------
# 1. The declared edge itself.
# ---------------------------------------------------------------------------


def test_the_edge_is_declared_in_both_variants():
    for vigilant in (True, False):
        assert transitions.is_legal("review", "review", vigilant)
        guards = transitions.guard_names("review", vigilant)
        assert "rejected_last_approach" in guards
        by_guard = {e.guard: e.target for e in
                   transitions.successors("review", vigilant)}
        assert by_guard["rejected_last_approach"] == "paused"


def test_the_edge_is_also_declared_under_the_peer_verdict_gate_delta():
    edges = transitions.successors("review", vigilant=False,
                                   peer_verdict_gate=True)
    by_guard = {e.guard: e.target for e in edges}
    assert by_guard["rejected_last_approach"] == "paused"


# ---------------------------------------------------------------------------
# 2. The pause outcome: last approach, all curable, cure pass already
#    spent -> pause and escalate, never `failed`, approach unarchived.
# ---------------------------------------------------------------------------


def test_last_approach_all_curable_with_cure_spent_pauses_and_escalates(
        tmp_path):
    root = tmp_path / "run"
    sup = _supervisor(root, max_approaches=1)
    sup._approach = 1
    notifier = _StubNotifier()
    sup.notifier = notifier
    # simulate the one cure pass this approach already spent (task 009).
    sup.run.update_status(curePasses={"1": 1})

    still_curable = [
        _finding("blocking", True, "requirement C.1 (#141)",
                "the notify tool still doesn't record the event"),
    ]
    findings.write_findings_json(sup.run.findings_json_file, still_curable)

    async def _review_rejects(prompt, transcript, **kw):
        return _result(json.dumps({
            "verdict": "rejected",
            "reason": "cure did not fully resolve the finding",
            "findings_path": "review-findings.md",
        }))

    sup.runner.run = _review_rejects  # type: ignore[method-assign]

    assert sup._pause.is_set() is True  # not paused yet

    outcome = asyncio.run(sup._step_review())

    assert outcome == ("advance", "paused", "rejected_last_approach")
    # paused through the EXISTING pause machinery -- the same asyncio.Event
    # POST /pause's loop.pause() clears.
    assert sup._pause.is_set() is False
    status = sup.run.read_status()
    assert status.get("state") != "succeeded"
    assert status.get("state") != "failed"  # never failed on paperwork

    # never archived, never a composite PRD for a fresh (nonexistent)
    # approach -- resume asks review again over the SAME approach.
    assert not (root / "approaches" / "01").exists()
    assert not sup.run.composite_prd_file.exists()
    assert sup._approach == 1

    escalations = _events(root, "operator_escalation")
    assert len(escalations) == 1
    assert escalations[0]["reason"] == "rejected_last_approach"
    assert escalations[0]["approach"] == 1
    assert escalations[0]["findings"][0]["clause"] == "requirement C.1 (#141)"

    assert len(notifier.messages) == 1
    text = notifier.messages[0]
    assert "requirement C.1 (#141)" in text
    assert "the notify tool still doesn't record the event" in text
    assert "ralphctl resume task011-last-approach-pause" in text
    assert "resolve with: ralphctl amend" in text


def test_the_escalation_is_emitted_to_events_jsonl_with_no_notifier(tmp_path):
    root = tmp_path / "run"
    sup = _supervisor(root, max_approaches=1)
    sup._approach = 1
    assert sup.notifier is None
    sup.run.update_status(curePasses={"1": 1})

    still_curable = [_finding("blocking", True, "requirement C.1 (#141)",
                              "still not fixed")]
    findings.write_findings_json(sup.run.findings_json_file, still_curable)

    async def _review_rejects(prompt, transcript, **kw):
        return _result(json.dumps({"verdict": "rejected", "reason": "no"}))

    sup.runner.run = _review_rejects  # type: ignore[method-assign]

    outcome = asyncio.run(sup._step_review())
    assert outcome == ("advance", "paused", "rejected_last_approach")
    assert sup._pause.is_set() is False

    escalations = _events(root, "operator_escalation")
    assert len(escalations) == 1
    assert escalations[0]["reason"] == "rejected_last_approach"


# ---------------------------------------------------------------------------
# 3. Requirement D.1's other half: a cure pass still available on the last
#    approach is taken instead of the pause edge.
# ---------------------------------------------------------------------------


def test_last_approach_first_rejection_still_takes_the_cure_pass(tmp_path):
    root = tmp_path / "run"
    sup = _supervisor(root, max_approaches=1)
    sup._approach = 1
    sup.notifier = _StubNotifier()
    from ralphd.engine.state import atomic_write_json
    atomic_write_json(sup.run.tasks_file, {"version": 1, "tasks": []})

    curable = [_finding("blocking", True, "requirement C.1 (#141)",
                        "the notify tool doesn't exist yet")]
    findings.write_findings_json(sup.run.findings_json_file, curable)

    async def _review_rejects(prompt, transcript, **kw):
        return _result(json.dumps({"verdict": "rejected", "reason": "no"}))

    sup.runner.run = _review_rejects  # type: ignore[method-assign]

    outcome = asyncio.run(sup._step_review())

    assert outcome == ("advance", "cure", "rejected_curable_pass_available")
    assert sup._pause.is_set() is True  # never paused -- cured instead
    assert sup.notifier.messages == []  # never escalated
    assert sup.run.read_status().get("curePasses") == {"1": 1}


# ---------------------------------------------------------------------------
# 4. Requirement D.2: any incurable blocking finding on the last approach
#    still takes `rejected_no_approach_left` -> `failed`, never the pause
#    edge, whether or not a cure pass has already been spent.
# ---------------------------------------------------------------------------


def test_last_approach_with_an_incurable_finding_still_fails(tmp_path):
    root = tmp_path / "run"
    sup = _supervisor(root, max_approaches=1)
    sup._approach = 1
    sup.notifier = _StubNotifier()

    all_incurable = [_finding("blocking", False, "requirement B (#111)",
                              "an environment fact a fresh approach cannot "
                              "change")]
    findings.write_findings_json(sup.run.findings_json_file, all_incurable)

    async def _review_rejects(prompt, transcript, **kw):
        return _result(json.dumps({"verdict": "rejected", "reason": "no"}))

    sup.runner.run = _review_rejects  # type: ignore[method-assign]

    outcome = asyncio.run(sup._step_review())

    # `rejected_incurable` isn't reachable at all once `approach ==
    # max_approaches` (task 006's own precedence: the incurable check is
    # gated on `approach < max_approaches`) -- so this lands on the
    # ordinary `rejected_no_approach_left` -> failed edge, never the pause
    # edge this task adds.
    assert outcome == ("tail",)
    assert sup._pause.is_set() is True  # never paused
    assert sup.notifier.messages == []  # never escalated
    escalations = _events(root, "operator_escalation")
    assert escalations == []
    status = sup.run.read_status()
    assert status.get("state") != "succeeded"


def test_last_approach_with_mixed_curability_still_fails(tmp_path):
    """A blocking finding set that is neither all-curable nor all-
    incurable (mixed) must not match either escalation edge -- it falls
    through to the ordinary `rejected_no_approach_left` -> failed edge on
    the last approach, exactly like it does mid-run (`rejected_approach_
    left`, `tests/test_rejected_incurable_edge.py`'s own mixed-curability
    case) -- one finding curable, one not."""
    root = tmp_path / "run"
    sup = _supervisor(root, max_approaches=1)
    sup._approach = 1
    sup.notifier = _StubNotifier()

    mixed = [
        _finding("blocking", False, "requirement B (#111)", "incurable one"),
        _finding("blocking", True, "requirement D (#99)", "curable one"),
    ]
    findings.write_findings_json(sup.run.findings_json_file, mixed)

    async def _review_rejects(prompt, transcript, **kw):
        return _result(json.dumps({"verdict": "rejected", "reason": "mixed"}))

    sup.runner.run = _review_rejects  # type: ignore[method-assign]

    outcome = asyncio.run(sup._step_review())

    assert outcome == ("tail",)
    assert sup._pause.is_set() is True  # never paused
    assert sup.notifier.messages == []  # never escalated


# ---------------------------------------------------------------------------
# 5. Resume: a fresh `LoopSupervisor` over the SAME on-disk run dir detects
#    the pause and routes straight back into `review`, over the SAME
#    approach -- never `worker`, never a fresh `replan`.
# ---------------------------------------------------------------------------


def test_resume_detects_the_last_approach_pause_and_re_enters_review(
        tmp_path):
    root = tmp_path / "run"
    sup1 = _supervisor(root, max_approaches=1)
    sup1._approach = 1
    sup1.run.update_status(curePasses={"1": 1})
    from ralphd.engine.state import atomic_write_json
    atomic_write_json(sup1.run.tasks_file, {"version": 1, "tasks": [
        {"id": "001", "title": "do the thing", "status": "completed",
         "successCriteria": "the thing is done"},
    ]})

    still_curable = [_finding("blocking", True, "requirement C.1 (#141)",
                              "still not fixed")]
    findings.write_findings_json(sup1.run.findings_json_file, still_curable)

    async def _review_rejects(prompt, transcript, **kw):
        return _result(json.dumps({"verdict": "rejected", "reason": "no"}))

    sup1.runner.run = _review_rejects  # type: ignore[method-assign]
    outcome = asyncio.run(sup1._step_review())
    assert outcome == ("advance", "paused", "rejected_last_approach")
    assert sup1._pause.is_set() is False

    # a brand-new process, no shared Python state at all -- reads
    # everything fresh off disk (`events.jsonl`, `status.json`).
    sup2 = _supervisor(root, max_approaches=1)
    assert sup2._paused_awaiting_last_approach_ruling(1) is True
    # the OTHER predicate (incurable) must not also fire for this reason.
    assert sup2._paused_awaiting_incurable_ruling(1) is False

    start_approach, skip_planning, start_step = sup2._resume_point()
    assert start_approach == 1
    assert skip_planning is False
    assert start_step == "review"


def test_paused_awaiting_last_approach_ruling_is_false_once_a_new_iteration_started(
        tmp_path):
    """Once a fresh iteration has actually started since the escalation
    (an in-process `resume()` that got all the way to running another
    review attempt), the predicate must not fire again for the now-stale
    escalation -- the same shape
    `tests/test_resume_after_incurable_rejection.py` pins for its own
    sibling predicate."""
    root = tmp_path / "run"
    sup = _supervisor(root, max_approaches=1)
    sup._approach = 1
    sup.run.update_status(curePasses={"1": 1})

    still_curable = [_finding("blocking", True, "requirement C.1 (#141)",
                              "still not fixed")]
    findings.write_findings_json(sup.run.findings_json_file, still_curable)

    async def _review_rejects(prompt, transcript, **kw):
        return _result(json.dumps({"verdict": "rejected", "reason": "no"}))

    sup.runner.run = _review_rejects  # type: ignore[method-assign]
    outcome = asyncio.run(sup._step_review())
    assert outcome == ("advance", "paused", "rejected_last_approach")
    assert sup._paused_awaiting_last_approach_ruling(1) is True

    sup.run.emit("iteration.start", phase="review", approach=1)
    assert sup._paused_awaiting_last_approach_ruling(1) is False
