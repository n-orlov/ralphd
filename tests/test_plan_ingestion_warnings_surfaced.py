"""Task 022 (PRD v0.23 requirement F.1, #222): `PlanIngestionResult.warnings`
is accumulated by `plan_ingestion.enforce_plan_ingestion`
(`normalize_task_tiers`'s degenerate-all-`hard` warning,
`normalize_task_estimated_iterations`'s defaulted-estimate warning) but had
NO runtime consumer at all before this task -- nothing in `loop.py` ever
read the field, so both warnings were invisible outside a unit test.
`_step_planning`'s accepted-proposal path now emits one `log` event
(`level: "warning"`) plus one dedicated `planning.warning` event per
warning string.

Scaffolding mirrors `tests/test_worker_hat.py`'s task-004-audit harness
(`supervisor_factory` + a stub of `_run_iteration_once` below the
infra-retry wrapper) -- deliberately not shared via import, exactly as
every other `_run_job_core`-driving test module in this tree keeps its own
copy. Unlike that harness, the stub here also writes the scripted
`plan-proposal.json` to the run root before returning the pointer line,
since `enforce_plan_ingestion` reads the proposal from disk
(`plan_ingestion.file_reader`).
"""

from __future__ import annotations

import asyncio
import copy
import json

from ralphd.engine.loop import LoopSupervisor
from ralphd.engine.runner import IterationResult
from ralphd.engine.state import atomic_write_json, read_events
from tests.conftest import supervisor_factory

_supervisor = supervisor_factory(
    run_id="task022-plan-warnings", infra_retry_backoff_s=[0.0],
    infra_retry_backoff_max_s=0.0, infra_outage_budget_s=1000.0,
    vigilant=False, max_approaches=2, on_complete="exit")


def _pointer_line(path: str = "plan-proposal.json", summary: str = "planned") -> str:
    return json.dumps({"proposal_path": path, "summary": summary})


def _ok(text: str) -> IterationResult:
    r = IterationResult(exit_code=0)
    r.final_text = text
    r.duration_s = 30.0
    r.usage = {"input": 10, "output": 5, "totalTokens": 15}
    return r


def _degenerate_tier_proposal(sup: LoopSupervisor) -> IterationResult:
    """Every task annotated tier 'hard' -- the pre-existing
    `normalize_task_tiers` degenerate-distribution warning."""
    # `estimatedIterations` supplied on every task so
    # `normalize_task_estimated_iterations` stays silent and the only
    # warning produced is the tier-distribution one under test.
    atomic_write_json(sup.run.root / "plan-proposal.json", {"version": 1, "tasks": [
        {"id": "001", "title": "t1", "successCriteria": "criterion one",
         "tier": "hard", "estimatedIterations": 2},
        {"id": "002", "title": "t2", "successCriteria": "criterion two",
         "tier": "hard", "estimatedIterations": 2},
    ]})
    return _ok("planned\n" + _pointer_line())


def _ordinary_proposal(sup: LoopSupervisor) -> IterationResult:
    """A plain, non-degenerate proposal with every optional annotation
    supplied explicitly: produces NO warnings at all (neither
    `normalize_task_tiers` nor `normalize_task_estimated_iterations`)."""
    atomic_write_json(sup.run.root / "plan-proposal.json", {"version": 1, "tasks": [
        {"id": "001", "title": "t1", "successCriteria": "criterion one",
         "tier": "normal", "estimatedIterations": 2},
        {"id": "002", "title": "t2", "successCriteria": "criterion two",
         "tier": "trivial", "estimatedIterations": 1},
    ]})
    return _ok("planned\n" + _pointer_line())


def _stub_attempts(sup: LoopSupervisor, build_result) -> list[str]:
    """Same technique as tests/test_worker_hat.py's `_stub_attempts`, but
    `build_result` is a callable `(sup) -> IterationResult` (so it can write
    the proposal file as a side effect) rather than a canned list."""
    calls: list[str] = []

    async def fake_once(phase, extra="", prompt_name=None, tier=None, task_id=None):
        calls.append(phase)
        sup.iterations_used += 1
        return copy.copy(build_result(sup))

    sup._run_iteration_once = fake_once  # type: ignore[method-assign]
    return calls


def _log_messages(sup: LoopSupervisor) -> list[str]:
    return [e.get("message") or ""
            for e in read_events(sup.run.root, types=("log",))]


def _planning_warning_events(sup: LoopSupervisor) -> list[dict]:
    return read_events(sup.run.root, types=("planning.warning",))


def test_a_degenerate_tier_distribution_warning_is_surfaced_at_ingestion(tmp_path):
    sup = _supervisor(tmp_path, iterations=5)
    sup._approach = 1
    _stub_attempts(sup, _degenerate_tier_proposal)

    outcome = asyncio.run(sup._step_planning(skip_planning=False))

    assert outcome[0] == "advance"
    events = _planning_warning_events(sup)
    assert len(events) == 1
    assert events[0]["approach"] == 1
    assert "degenerate" in events[0]["warning"]

    log_lines = _log_messages(sup)
    assert any("degenerate" in m and "plan ingestion warning" in m
                for m in log_lines)


def test_a_proposal_with_no_warnings_emits_no_planning_warning_event(tmp_path):
    sup = _supervisor(tmp_path, iterations=5)
    sup._approach = 1
    _stub_attempts(sup, _ordinary_proposal)

    outcome = asyncio.run(sup._step_planning(skip_planning=False))

    assert outcome[0] == "advance"
    assert _planning_warning_events(sup) == []
    assert not any("plan ingestion warning" in m for m in _log_messages(sup))


def _ordering_only_whole_set_proposal(sup: LoopSupervisor) -> IterationResult:
    """Task 023 (PRD requirement F.2, #222): task '002' names its WHOLE
    `dependsOn` set (just '001') as `orderingOnly` -- the over-declaration
    `lint_proposal` now warns on (accepted, not refused). `estimatedIterations`
    supplied explicitly on both tasks so no other warning fires alongside it."""
    atomic_write_json(sup.run.root / "plan-proposal.json", {"version": 1, "tasks": [
        {"id": "001", "title": "t1", "successCriteria": "criterion one",
         "tier": "normal", "estimatedIterations": 1},
        {"id": "002", "title": "t2", "successCriteria": "criterion two",
         "tier": "normal", "estimatedIterations": 1,
         "dependsOn": ["001"], "orderingOnly": ["001"]},
    ]})
    return _ok("planned\n" + _pointer_line())


def test_an_ordering_only_whole_set_over_declaration_warning_is_surfaced_at_ingestion(tmp_path):
    """Task 023 (PRD requirement F.2, #222): the warning reaches the run's
    own records through task 022's already-surfaced channel -- observed
    here in `events.jsonl` exactly like the pre-existing tier/estimate
    warnings above, naming the over-declaring task."""
    sup = _supervisor(tmp_path, iterations=5)
    sup._approach = 1
    _stub_attempts(sup, _ordering_only_whole_set_proposal)

    outcome = asyncio.run(sup._step_planning(skip_planning=False))

    assert outcome[0] == "advance"
    events = _planning_warning_events(sup)
    assert len(events) == 1
    assert events[0]["approach"] == 1
    assert "orderingOnly" in events[0]["warning"] and "002" in events[0]["warning"]

    log_lines = _log_messages(sup)
    assert any("orderingOnly" in m and "plan ingestion warning" in m
               for m in log_lines)
