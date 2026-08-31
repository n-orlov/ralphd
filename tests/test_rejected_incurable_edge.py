"""Task 006 (PRD requirement B.2, #111): a rejection whose blocking findings
are ALL incurable takes `rejected_incurable` instead of the ordinary
`rejected_approach_left` replan edge.

`transitions.TABLE["review"]` declares the new edge (self-loop, `review ->
review`); `LoopSupervisor._step_review` takes it -- ahead of the ordinary
archive-and-replan branch, but only once `_review_incurable_rejection()`
confirms both halves of requirement B.2 (`findings.compute_verdict` says
"rejected" AND `findings.all_blocking_findings_incurable` says every
blocking finding is incurable) -- by calling the EXISTING pause machinery
(`self.pause()`, the same `asyncio.Event` `POST /pause` clears; no second
pause implementation) and producing an operator escalation containing every
blocking finding plus the exact `ralphctl amend` invocation that would
resolve it.

This module drives a real `LoopSupervisor` (constructed exactly like
`tests/test_settled_vs_done_task013.py`/`tests/test_review_verdict_findings.
py` already do: a real `RunDir`, `JobConfig`, real `_step_review`, no
`_run_iteration_once` subprocess seam) straight to that edge by seeding
review's structured findings channel with two all-incurable blocking
findings and a stub `review` iteration reply, and asserts:

1. the declared edge exists and is legal (`transitions` state-machine pin);
2. `_step_review()` returns the `rejected_incurable` edge, not
   `rejected_approach_left`;
3. the run is PAUSED through the existing machinery (`sup._pause.is_set()`
   is `False`, exactly what `POST /pause`'s own `loop.pause()` sets);
4. a stub notifier attached to `sup.notifier` receives escalation text
   containing every finding's clause/evidence AND the exact `ralphctl
   amend` invocation for each one;
5. requirement B.3's other half -- a CURABLE blocking finding still takes
   `rejected_approach_left`, and `rejected_no_approach_left` still wins once
   the approach budget is exhausted, even when every finding is incurable.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from ralphd.engine import findings, transitions
from ralphd.engine.config import JobConfig
from ralphd.engine.loop import LoopSupervisor
from ralphd.engine.runner import IterationResult
from ralphd.engine.state import RunDir


def _finding(severity: str, curable: bool, clause: str, evidence: str) -> dict:
    return {"severity": severity, "curable": curable, "clause": clause,
            "evidence": evidence}


def _result(text: str) -> IterationResult:
    r = IterationResult(exit_code=0)
    r.final_text = text
    r.duration_s = 5.0
    r.usage = {"input": 10, "output": 5, "totalTokens": 15}
    return r


def _supervisor(root: Path, **cfg_kw) -> LoopSupervisor:
    root.mkdir(parents=True, exist_ok=True)
    run = RunDir(root=root)
    run.update_status(state="running")
    cfg = JobConfig(run_id="task006-rejected-incurable", **cfg_kw)
    return LoopSupervisor(cfg, run, root)


class _StubNotifier:
    def __init__(self) -> None:
        self.messages: list[str] = []

    def __call__(self, text: str) -> None:
        self.messages.append(text)


# ---------------------------------------------------------------------------
# 1. The declared edge itself.
# ---------------------------------------------------------------------------

def test_the_edge_is_declared_in_both_variants():
    for vigilant in (True, False):
        assert transitions.is_legal("review", "review", vigilant)
        guards = transitions.guard_names("review", vigilant)
        assert "rejected_incurable" in guards
        by_guard = {e.guard: e.target for e in
                   transitions.successors("review", vigilant)}
        assert by_guard["rejected_incurable"] == "review"


def test_the_edge_is_also_declared_under_the_peer_verdict_gate_delta():
    edges = transitions.successors("review", vigilant=False,
                                   peer_verdict_gate=True)
    by_guard = {e.guard: e.target for e in edges}
    assert by_guard["rejected_incurable"] == "review"


# ---------------------------------------------------------------------------
# 2-4. Driving a real LoopSupervisor to the edge.
# ---------------------------------------------------------------------------

def test_all_incurable_blocking_findings_pause_and_escalate(tmp_path):
    root = tmp_path / "run"
    sup = _supervisor(root, max_approaches=3)
    sup._approach = 1
    notifier = _StubNotifier()
    sup.notifier = notifier

    all_incurable = [
        _finding("blocking", False, "requirement B (#111)",
                "the endpoint has no docker.sock in this container"),
        _finding("blocking", False, "requirement D (#99)",
                "the deadline already passed before this approach started"),
    ]
    findings.write_findings_json(sup.run.findings_json_file, all_incurable)

    async def _review_rejects(prompt, transcript, **kw):
        return _result(json.dumps({
            "verdict": "rejected",
            "reason": "both blocking findings are environment/temporal "
                      "facts a fresh approach cannot change",
            "findings_path": "review-findings.md",
        }))

    sup.runner.run = _review_rejects  # type: ignore[method-assign]

    assert sup._pause.is_set() is True  # not paused yet

    outcome = asyncio.run(sup._step_review())

    assert outcome == ("advance", "review", "rejected_incurable")
    # the run is paused through the EXISTING pause machinery -- the same
    # asyncio.Event POST /pause's loop.pause() clears.
    assert sup._pause.is_set() is False

    # the stub notifier received exactly one escalation, and it names both
    # findings plus the exact ralphctl amend invocation for each.
    assert len(notifier.messages) == 1
    text = notifier.messages[0]
    for f in all_incurable:
        assert f["clause"] in text
        assert f["evidence"] in text
    assert text.count("ralphctl amend task006-rejected-incurable") == 2
    assert "--waive" in text

    # never archived, never composited into a fresh approach's PRD -- the
    # SAME approach is what review is asked about again on resume.
    assert not (root / "approaches" / "01").exists()
    assert sup._approach == 1


def test_the_escalation_is_also_emitted_to_events_jsonl_with_no_notifier(tmp_path):
    """Even with no notifier wired in (production's default), the
    escalation is never silently lost -- it lands in events.jsonl too."""
    root = tmp_path / "run"
    sup = _supervisor(root, max_approaches=3)
    sup._approach = 1
    assert sup.notifier is None

    incurable = [_finding("blocking", False, "requirement B (#111)",
                          "temporal miss")]
    findings.write_findings_json(sup.run.findings_json_file, incurable)

    async def _review_rejects(prompt, transcript, **kw):
        return _result(json.dumps({"verdict": "rejected", "reason": "no"}))

    sup.runner.run = _review_rejects  # type: ignore[method-assign]

    outcome = asyncio.run(sup._step_review())
    assert outcome == ("advance", "review", "rejected_incurable")
    assert sup._pause.is_set() is False

    events = [json.loads(l) for l in
             (root / "events.jsonl").read_text().splitlines()]
    escalations = [e for e in events if e.get("type") == "operator_escalation"]
    assert len(escalations) == 1
    assert escalations[0]["reason"] == "rejected_incurable"
    assert escalations[0]["findings"][0]["clause"] == "requirement B (#111)"


# ---------------------------------------------------------------------------
# 5. A curable blocking finding still replans; approach exhaustion still
#    wins over an all-incurable rejection.
# ---------------------------------------------------------------------------

def test_a_curable_blocking_finding_still_takes_the_ordinary_replan_edge(tmp_path):
    root = tmp_path / "run"
    root.mkdir(parents=True, exist_ok=True)
    (root / "prd.md").write_text("# test PRD\n\nDo the thing.\n")
    sup = _supervisor(root, max_approaches=3)
    sup._approach = 1
    sup.notifier = _StubNotifier()

    mixed_curability = [
        _finding("blocking", False, "requirement B (#111)", "incurable one"),
        _finding("blocking", True, "requirement D (#99)", "curable one"),
    ]
    findings.write_findings_json(sup.run.findings_json_file, mixed_curability)

    async def _review_rejects(prompt, transcript, **kw):
        return _result(json.dumps({"verdict": "rejected", "reason": "mixed"}))

    sup.runner.run = _review_rejects  # type: ignore[method-assign]

    outcome = asyncio.run(sup._step_review())

    assert outcome == ("advance", "replan", "rejected_approach_left")
    assert sup._pause.is_set() is True  # never paused
    assert sup.notifier.messages == []  # never escalated
    assert (root / "approaches" / "01").exists()  # archived as usual


def test_rejected_no_approach_left_still_wins_when_approaches_are_exhausted(tmp_path):
    root = tmp_path / "run"
    root.mkdir(parents=True, exist_ok=True)
    (root / "prd.md").write_text("# test PRD\n\nDo the thing.\n")
    sup = _supervisor(root, max_approaches=1)  # nothing left after this one
    sup._approach = 1
    sup.notifier = _StubNotifier()

    all_incurable = [_finding("blocking", False, "requirement B (#111)",
                              "incurable finding")]
    findings.write_findings_json(sup.run.findings_json_file, all_incurable)

    async def _review_rejects(prompt, transcript, **kw):
        return _result(json.dumps({"verdict": "rejected", "reason": "no"}))

    sup.runner.run = _review_rejects  # type: ignore[method-assign]

    outcome = asyncio.run(sup._step_review())

    # the approach budget is spent -- `rejected_no_approach_left` wins over
    # `rejected_incurable` even though every blocking finding is incurable.
    assert outcome == ("tail",)
    assert sup._pause.is_set() is True  # never paused
    assert sup.notifier.messages == []  # never escalated
    status = sup.run.read_status()
    assert status.get("state") != "succeeded"
