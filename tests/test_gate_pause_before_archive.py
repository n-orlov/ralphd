"""Task 008 (v0.27 requirement C.1): the peer verdict gate's own mirror of
`TABLE["review"]`'s A.1 pause-before-archive edge (task 014, PRD
requirement A.1, #224) -- an ordinary gate rejection that would otherwise
reach `_archive_approach`/`_write_composite_prd` holds instead when it
would have earned the cure-pass edge (`verdict_gate_rejected_curable_pass_
available`) if the run-wide `cure_pass_ceiling` were not already spent
(`LoopSupervisor._gate_rejection_ending` reporting `state.GATE_REJECTION_
ENDING_CURE_UNAVAILABLE`), ON by default through the SAME `JobConfig.
operator_input_needed_pause` flag review's own edge reads.

Before this task `_step_verdict_gate` had no such edge: a rejection whose
findings were all curable but whose ceiling was spent fell straight
through to the ordinary archive/replan-or-finish edges, spending (and
losing) the approach on findings the run had no cure budget left to fix
faster than a fresh approach would either -- exactly the gap A.1 already
closed for review's own cure channel, left open for the gate's.

Drives `_step_verdict_gate` directly on a fresh `LoopSupervisor`/`RunDir`
fixture (never this run's own engine), mirroring `tests/test_peer_verdict_
gate.py::test_gate_rejection_all_curable_takes_a_cure_pass_not_a_replan`'s
own fixture shape.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from ralphd.engine import findings
from ralphd.engine.config import JobConfig
from ralphd.engine.loop import LoopSupervisor
from ralphd.engine.runner import IterationResult
from ralphd.engine.state import RunDir, atomic_write_json


def _bounded(coro, timeout: float = 30.0):
    return asyncio.run(asyncio.wait_for(coro, timeout))


def _all_curable_gate_findings() -> list[dict]:
    return [
        {"severity": "blocking", "curable": True,
         "clause": "requirement C.1 (v0.27)",
         "evidence": "the peer gate found an unmet requirement",
         "cured_when": "a cure task adds the missing behaviour"},
    ]


# Task 002 (this wave): the three named blocking-set shapes -- one curable
# plus one non-curable finding, two non-curable findings, and two curable
# findings (a two-finding sibling of `_all_curable_gate_findings` above,
# never reused for it: the existing tests above pin a SINGLE-finding all-
# curable rejection and must keep doing so unchanged).
def _mixed_1c_1i_gate_findings() -> list[dict]:
    return [
        {"severity": "blocking", "curable": True,
         "clause": "requirement C.1 mixed curable clause (v0.27)",
         "evidence": "the peer gate found a fixable gap",
         "cured_when": "a cure task adds the missing behaviour"},
        {"severity": "blocking", "curable": False,
         "clause": "requirement C.1 mixed non-curable clause (v0.27)",
         "evidence": "an environment fact no cure task or fresh approach "
                    "can change"},
    ]


def _all_incurable_2i_gate_findings() -> list[dict]:
    return [
        {"severity": "blocking", "curable": False,
         "clause": "requirement C.1 incurable clause one (v0.27)",
         "evidence": "an environment fact no cure task or fresh approach "
                    "can change"},
        {"severity": "blocking", "curable": False,
         "clause": "requirement C.1 incurable clause two (v0.27)",
         "evidence": "a second environment fact no cure task or fresh "
                    "approach can change"},
    ]


def _all_curable_2c_gate_findings() -> list[dict]:
    return [
        {"severity": "blocking", "curable": True,
         "clause": "requirement C.1 curable clause one (v0.27)",
         "evidence": "the peer gate found a fixable gap",
         "cured_when": "a cure task adds the missing behaviour"},
        {"severity": "blocking", "curable": True,
         "clause": "requirement C.1 curable clause two (v0.27)",
         "evidence": "the peer gate found a second fixable gap",
         "cured_when": "a cure task adds the other missing behaviour"},
    ]


_CEILING_SPENT_FIXTURES = {
    "mixed_1c_1i": _mixed_1c_1i_gate_findings,
    "all_incurable_2i": _all_incurable_2i_gate_findings,
    "all_curable_2c": _all_curable_2c_gate_findings,
}


def _gate_rejects(reason: str):
    async def handler(self, phase, extra="", *a, **kw):
        r = IterationResult(exit_code=0)
        r.final_text = json.dumps({"verdict": "rejected", "reason": reason})
        return r
    return handler


def _fixture(tmp_path: Path, *, cure_pass_ceiling: int,
            max_approaches: int = 2,
            gate_findings: list[dict] | None = None,
            operator_input_needed_pause: bool = True) -> LoopSupervisor:
    root = tmp_path / "run"
    root.mkdir(parents=True, exist_ok=True)
    run = RunDir(root=root)
    run.update_status(state="running")
    atomic_write_json(run.tasks_file, {"version": 1, "tasks": []})
    cfg = JobConfig(run_id="task008-gate-pause", iterations=5,
                    max_approaches=max_approaches, vigilant=False,
                    peer_verdict_gate=True, cure_pass_ceiling=cure_pass_ceiling,
                    operator_input_needed_pause=operator_input_needed_pause,
                    infra_retry_backoff_s=[0.0], infra_retry_backoff_max_s=0.0)
    sup = LoopSupervisor(cfg, run, root)
    sup._approach = 1
    findings.write_findings_json(run.verdict_gate_findings_json_file,
                                 gate_findings if gate_findings is not None
                                 else _all_curable_gate_findings())
    return sup


def _escalations(root: Path) -> list[dict]:
    log = root / "events.jsonl"
    lines = log.read_text().splitlines() if log.exists() else []
    events = [json.loads(line) for line in lines]
    return [ev for ev in events if ev.get("type") == "operator_escalation"]


def test_gate_rejection_with_ceiling_spent_parks_instead_of_archiving(
        tmp_path):
    """`cure_pass_ceiling=0`: every check `_gate_cure_pass_available` makes
    passes EXCEPT the run-wide ceiling comparison -- `_gate_rejection_
    ending` reports `GATE_REJECTION_ENDING_CURE_UNAVAILABLE`, and
    `operator_input_needed_pause` is on (the default), so the gate must
    hold instead of taking the ordinary archive/replan-or-finish edges.
    """
    sup = _fixture(tmp_path, cure_pass_ceiling=0, max_approaches=2)
    root = sup.run.root
    assert sup._pause.is_set() is True  # not paused yet

    orig = LoopSupervisor.run_iteration
    LoopSupervisor.run_iteration = _gate_rejects(
        "the one blocking finding is curable")
    try:
        outcome = _bounded(sup._step_verdict_gate())
    finally:
        LoopSupervisor.run_iteration = orig

    assert outcome == ("advance", "paused", "verdict_gate_operator_input_needed")
    # paused through the EXISTING pause machinery.
    assert sup._pause.is_set() is False
    status = sup.run.read_status()
    # v0.36: the park lands in `awaiting_input` (covering node: tests/test_awaiting_input_question.py)
    assert status.get("state") == "awaiting_input"
    assert status.get("pauseReason") == "verdict_gate_operator_input_needed"
    assert status.get("gateRejectionEnding") == "rejected_cure_unavailable"

    # never archived, never a composite PRD, never a fresh cure pass --
    # resume re-enters `verdict_gate` over the SAME approach.
    assert sup._approach == 1
    assert not (root / "approaches" / "01").exists()
    assert not sup.run.composite_prd_file.exists()
    assert status.get("gateCurePasses") is None

    escalations = _escalations(root)
    assert len(escalations) == 1
    assert escalations[0]["reason"] == "verdict_gate_operator_input_needed"
    assert escalations[0]["approach"] == 1
    assert escalations[0]["findings"][0]["clause"] == "requirement C.1 (v0.27)"
    assert "requirement C.1 (v0.27)" in escalations[0]["escalation"]


def test_gate_rejection_with_a_pass_left_still_takes_the_cure_edge(
        tmp_path):
    """The complement: with the run-wide ceiling NOT spent
    (`cure_pass_ceiling`'s default, 3, and no cure pass taken yet this
    run), the identical all-curable rejection still takes the ordinary
    cure-pass edge (`verdict_gate_rejected_curable_pass_available`) --
    this task's new pause edge must never preempt a cure pass that is
    actually available.
    """
    sup = _fixture(tmp_path, cure_pass_ceiling=3, max_approaches=2)
    root = sup.run.root

    orig = LoopSupervisor.run_iteration
    LoopSupervisor.run_iteration = _gate_rejects(
        "the one blocking finding is curable")
    try:
        outcome = _bounded(sup._step_verdict_gate())
    finally:
        LoopSupervisor.run_iteration = orig

    assert outcome == ("advance", "cure",
                       "verdict_gate_rejected_curable_pass_available")
    status = sup.run.read_status()
    assert status.get("gateRejectionEnding") == "rejected_cure_taken"
    assert status.get("gateCurePasses") == {"1": 1}
    assert status.get("state") != "paused"
    assert not (root / "approaches" / "01").exists()
    assert _escalations(root) == []


# ===========================================================================
# Task 002 (this wave): the spent-ceiling pause widens to EVERY blocking-
# set shape -- all-curable (already covered above, re-proven here with a
# two-finding sibling fixture), mixed, and all-incurable. At 32352c15 the
# mixed and all-incurable cases fell straight through to the ordinary
# merits tail (`("tail",)`) even with the ceiling spent, since the pause
# above checked `ending == GATE_REJECTION_ENDING_CURE_UNAVAILABLE` alone.
# ===========================================================================


@pytest.mark.parametrize("fixture_name", sorted(_CEILING_SPENT_FIXTURES))
def test_gate_rejection_with_ceiling_spent_parks(tmp_path, fixture_name):
    """`cure_pass_ceiling=0`, two approaches, for each of the three named
    blocking-set fixtures -- `_step_verdict_gate()` parks
    (`verdict_gate_operator_input_needed`) rather than taking the ordinary
    archive/replan-or-finish (or merits-tail) edges, with exactly one
    `operator_escalation` event and no `approaches/01` archive.
    """
    sup = _fixture(tmp_path, cure_pass_ceiling=0, max_approaches=2,
                   gate_findings=_CEILING_SPENT_FIXTURES[fixture_name]())
    root = sup.run.root

    orig = LoopSupervisor.run_iteration
    LoopSupervisor.run_iteration = _gate_rejects(
        "blocking findings present, cure-pass ceiling already spent")
    try:
        outcome = _bounded(sup._step_verdict_gate())
    finally:
        LoopSupervisor.run_iteration = orig

    assert outcome == ("advance", "paused", "verdict_gate_operator_input_needed")
    status = sup.run.read_status()
    # v0.36: the park lands in `awaiting_input` (covering node: tests/test_awaiting_input_question.py)
    assert status.get("state") == "awaiting_input"
    assert status.get("pauseReason") == "verdict_gate_operator_input_needed"

    assert sup._approach == 1
    assert not (root / "approaches" / "01").exists()
    assert not sup.run.composite_prd_file.exists()

    escalations = _escalations(root)
    assert len(escalations) == 1
    assert escalations[0]["reason"] == "verdict_gate_operator_input_needed"
    assert escalations[0]["approach"] == 1


def test_gate_rejection_mixed_with_ceiling_spent_on_last_approach_parks(
        tmp_path):
    """The mixed fixture on the run's own LAST approach (`max_approaches=
    1`) -- A.1/A.2's own removal-specific rule ends a non-curable
    rejection right there whether or not an approach is left, but this
    task's widened pause still holds for an operator FIRST, exactly like
    the approach-left case above; it never reads the lack of a further
    approach as a reason to skip straight to the terminal tail.
    """
    sup = _fixture(tmp_path, cure_pass_ceiling=0, max_approaches=1,
                   gate_findings=_mixed_1c_1i_gate_findings())
    root = sup.run.root

    orig = LoopSupervisor.run_iteration
    LoopSupervisor.run_iteration = _gate_rejects(
        "one blocking finding is curable, the other is not")
    try:
        outcome = _bounded(sup._step_verdict_gate())
    finally:
        LoopSupervisor.run_iteration = orig

    assert outcome == ("advance", "paused", "verdict_gate_operator_input_needed")
    status = sup.run.read_status()
    # v0.36: the park lands in `awaiting_input` (covering node: tests/test_awaiting_input_question.py)
    assert status.get("state") == "awaiting_input"
    assert not (root / "approaches" / "01").exists()
    assert not sup.run.composite_prd_file.exists()
    escalations = _escalations(root)
    assert len(escalations) == 1


def test_gate_rejection_mixed_with_ceiling_spent_disabled_pause_takes_32352c15_ending(
        tmp_path):
    """With `operator_input_needed_pause` disabled
    (`--no-operator-input-needed-pause`), the mixed fixture with the
    ceiling spent takes the EXACT ending it took at 32352c15 -- the
    ordinary merits tail (`("tail",)`), no pause, no escalation -- this
    task's widened pause is gated by the SAME flag review's own sibling
    edge is, never unconditional.
    """
    sup = _fixture(tmp_path, cure_pass_ceiling=0, max_approaches=2,
                   gate_findings=_mixed_1c_1i_gate_findings(),
                   operator_input_needed_pause=False)
    root = sup.run.root

    orig = LoopSupervisor.run_iteration
    LoopSupervisor.run_iteration = _gate_rejects(
        "one blocking finding is curable, the other is not")
    try:
        outcome = _bounded(sup._step_verdict_gate())
    finally:
        LoopSupervisor.run_iteration = orig

    assert outcome == ("tail",)
    status = sup.run.read_status()
    assert status.get("state") != "paused"
    assert status.get("gateRejectionEnding") == "rejected_on_the_merits"
    assert not (root / "approaches" / "01").exists()
    assert not sup.run.composite_prd_file.exists()
    assert _escalations(root) == []


_REPO = Path(__file__).resolve().parents[1]

# The pre-task-002 wording scoped the gate's pause to a rejection that
# "would have earned the gate's own cure-pass edge" -- i.e. all-curable
# only -- which contradicts `_step_verdict_gate` parking mixed and
# all-incurable blocking sets on a spent ceiling too.
_NARROW_SCOPE_PHRASES = (
    "rejection that would have earned the gate's own cure-pass edge if the "
    "run-wide `cure_pass_ceiling` were not already spent",
    "it fires only on a `verdict_gate` rejection that would have earned",
)


@pytest.mark.parametrize("doc, row_prefix", [
    ("SPEC.md", "| `operator_escalation` |"),
    ("SPEC.md", "| `pauseReason` |"),
    ("docs/api.md", "| `pauseReason` |"),
])
def test_spec_and_docs_gate_pause_rows_cover_every_blocking_set_shape(
        doc, row_prefix):
    """Every spec/doc row describing `verdict_gate_operator_input_needed`
    states it covers all-curable, mixed and all-incurable blocking sets,
    and none still scopes it to the all-curable (cure-pass-earning)
    rejection alone."""
    text = (_REPO / doc).read_text(encoding="utf-8")
    rows = [ln for ln in text.splitlines() if ln.startswith(row_prefix)]
    assert len(rows) == 1, (doc, row_prefix, len(rows))
    row = rows[0]
    for phrase in _NARROW_SCOPE_PHRASES:
        assert phrase not in row, (doc, phrase)
    # The description segment for this pause reason, from its own
    # introducing mention to the end of the row.
    idx = row.rfind('`"verdict_gate_operator_input_needed"` (v0.27')
    if idx < 0:
        idx = row.find('adds `"verdict_gate_operator_input_needed"`')
    assert idx >= 0, (doc, row_prefix)
    segment = row[idx:idx + 1200]
    assert "cure_pass_ceiling" in segment
    assert "mixed" in segment, (doc, segment[:300])
    assert "all-incurable" in segment, (doc, segment[:300])
    assert ("all-curable" in segment or "all curable" in segment), (
        doc, segment[:300])


def test_no_spec_or_doc_scopes_gate_pause_to_all_curable_only():
    for doc in ("SPEC.md", "docs/cli.md", "docs/api.md",
                "docs/architecture.md"):
        text = (_REPO / doc).read_text(encoding="utf-8")
        for phrase in _NARROW_SCOPE_PHRASES:
            assert phrase not in text, (doc, phrase)
