"""Task 031 (PRD requirement E.2, #171): structured findings gain
`cured_when` (required when `curable: true`) and optional `invalidates:
[<path>, ...]`. A curable blocking finding without `cured_when` is a
review contract violation on the ladder (`transitions.review_cured_
when_errors`, wired into both `_review_verdict` and `_review_contract_
ladder` exactly like `review_verdict_findings_errors` already is);
`state.insert_cure_tasks`/`cure_task_from_finding` use `cured_when`
verbatim as the cure task's `successCriteria` and `clause` + `evidence`
as its new `description`; `cured_when` itself is held to the plan's own
bounded-checkable-clause lint (`plan_ingestion.clause_count_overrun`,
exposed publicly by this task for exactly this cross-module reuse).

Four groups of nodes:

* the schema/business-rule layer (`findings.py`) -- `cured_when`/
  `invalidates` round-trip through the schema; a curable blocking
  finding missing `cured_when` is reported, one carrying a valid one is
  not; an overrun `cured_when` is reported with the same `(count,
  overflow_clause)` shape `plan_ingestion` itself returns;
* `transitions.review_cured_when_errors` -- the two refusal shapes
  (missing, overrun) and the two silent shapes (no channel, a
  compliant `cured_when`);
* the ladder itself -- a curable blocking finding missing `cured_when`
  is RE-ASKED over exactly like a `verified`/findings contradiction is,
  and recovers once the hat's re-ask answer repairs the finding
  (the "re-ask" pin);
* `state.cure_task_from_finding`/`insert_cure_tasks` -- the criteria
  SUBSTITUTION itself: `cured_when` becomes `successCriteria` verbatim,
  `clause`/`evidence` become `description`, and a finding with no
  `cured_when` still falls back to the pre-existing clause+evidence
  derivation (backward compatible with every fixture task 009 already
  pinned).
"""

from __future__ import annotations

import asyncio
import json

import pytest

from ralphd.engine import findings, plan_ingestion, transitions
from ralphd.engine.state import cure_task_from_finding, insert_cure_tasks

from tests.conftest import supervisor_factory

_supervisor = supervisor_factory(run_id="task031-cured-when",
                                 status={"state": "starting"})


def _curable_blocking(clause: str, evidence: str, **overrides) -> dict:
    payload = {"severity": "blocking", "curable": True, "clause": clause,
               "evidence": evidence}
    payload.update(overrides)
    return payload


def _write_findings(sup, findings_list: list[dict]) -> None:
    findings.write_findings_json(sup.run.findings_json_file, findings_list)


def _result(text: str):
    from ralphd.engine.runner import IterationResult
    r = IterationResult(exit_code=0)
    r.final_text = text
    return r


# ---------------------------------------------------------------------------
# The schema/business-rule layer.
# ---------------------------------------------------------------------------


def test_cured_when_and_invalidates_round_trip_through_the_schema():
    finding = _curable_blocking(
        "requirement E.2 (#171)", "concrete evidence",
        cured_when="the fix ships and the regression pins it",
        invalidates=["artifacts/reports/some-record.md"])
    assert findings.validate_finding(finding) == []


def test_a_curable_blocking_finding_missing_cured_when_is_reported():
    missing = _curable_blocking("requirement E.2 (#171)", "no cure spec")
    out = findings.curable_findings_missing_cured_when([missing])
    assert out == [missing]


def test_a_curable_blocking_finding_with_cured_when_is_not_reported():
    ok = _curable_blocking("requirement E.2 (#171)", "has a cure spec",
                           cured_when="ship the fix")
    assert findings.curable_findings_missing_cured_when([ok]) == []


def test_an_incurable_or_residual_finding_is_never_flagged_for_cured_when():
    incurable = _curable_blocking("requirement B (#111)", "env fact",
                                  curable=False)
    residual = {"severity": "residual", "curable": True,
                "clause": "cosmetic", "evidence": "typo"}
    assert findings.curable_findings_missing_cured_when(
        [incurable, residual]) == []


def test_a_cured_when_overrunning_the_bounded_clause_lint_is_reported():
    over_bound = ". ".join(f"clause {n}" for n in range(1, 8))  # 8 clauses
    finding = _curable_blocking("requirement E.2 (#171)", "evidence",
                                cured_when=over_bound)
    out = findings.curable_findings_with_overrun_cured_when([finding])
    assert len(out) == 1
    reported_finding, overrun = out[0]
    assert reported_finding is finding
    assert overrun == plan_ingestion.clause_count_overrun(over_bound)
    assert overrun[0] == 7


def test_a_cured_when_within_the_bound_is_not_reported():
    finding = _curable_blocking("requirement E.2 (#171)", "evidence",
                                cured_when="ship the fix. pin the regression")
    assert findings.curable_findings_with_overrun_cured_when([finding]) == []


# ---------------------------------------------------------------------------
# `transitions.review_cured_when_errors`.
# ---------------------------------------------------------------------------


def test_review_cured_when_errors_is_silent_with_no_findings_channel():
    assert transitions.review_cured_when_errors(None) == []


def test_review_cured_when_errors_is_silent_when_every_curable_finding_is_complete():
    ok = [_curable_blocking("requirement E.2 (#171)", "evidence",
                            cured_when="ship the fix")]
    assert transitions.review_cured_when_errors(ok) == []


def test_review_cured_when_errors_flags_a_missing_cured_when():
    missing = [_curable_blocking("requirement E.2 (#171)", "evidence")]
    errors = transitions.review_cured_when_errors(missing)
    assert len(errors) == 1
    assert "requirement E.2 (#171)" in errors[0]
    assert "cured_when" in errors[0]


def test_review_cured_when_errors_flags_an_overrun_cured_when():
    over_bound = ". ".join(f"clause {n}" for n in range(1, 8))
    overrun = [_curable_blocking("requirement E.2 (#171)", "evidence",
                                 cured_when=over_bound)]
    errors = transitions.review_cured_when_errors(overrun)
    assert len(errors) == 1
    assert "checkable clauses" in errors[0]
    assert "5-clause bound" in errors[0]


# ---------------------------------------------------------------------------
# The ladder: a curable blocking finding missing cured_when re-asks, and
# recovers once the hat's re-ask answer repairs the finding.
# ---------------------------------------------------------------------------


def test_a_missing_cured_when_is_re_asked_over_and_never_verifies(tmp_path):
    sup = _supervisor(tmp_path)
    cfg = sup.cfg
    cfg.contract_retry_max = 1
    _write_findings(sup, [_curable_blocking(
        "requirement E.2 (#171)", "the worker never wrote the notify tool")])
    rejected = json.dumps({"verdict": "rejected", "reason": "curable defect"})
    result = _result(rejected)

    calls = []

    async def fake_ask_contract_fix(hat, errors, **_kw):
        calls.append((hat, list(errors)))
        return rejected, {}

    sup._ask_contract_fix = fake_ask_contract_fix  # type: ignore[method-assign]

    laddered = asyncio.run(sup._review_contract_ladder(result))
    verified, payload, errors = sup._review_verdict(laddered)

    assert len(calls) == cfg.contract_retry_max
    assert calls[0][0] == "review"
    assert any("cured_when" in e for e in calls[0][1])
    assert verified is False
    assert errors
    assert any("cured_when" in e for e in errors)


def test_a_repaired_cured_when_on_re_ask_recovers(tmp_path):
    sup = _supervisor(tmp_path)
    _write_findings(sup, [_curable_blocking(
        "requirement E.2 (#171)", "the worker never wrote the notify tool")])
    first_attempt = json.dumps({"verdict": "rejected", "reason": "curable defect"})
    result = _result(first_attempt)

    async def fake_ask_contract_fix(hat, errors, **_kw):
        # The hat's re-ask answer repairs the sidecar itself (review is
        # expected to rewrite `review-findings.json`, not just its final
        # text -- `extra_validate` re-reads the channel every attempt).
        _write_findings(sup, [_curable_blocking(
            "requirement E.2 (#171)",
            "the worker never wrote the notify tool",
            cured_when="the notify tool exists and is called on completion")])
        return first_attempt, {}

    sup._ask_contract_fix = fake_ask_contract_fix  # type: ignore[method-assign]

    laddered = asyncio.run(sup._review_contract_ladder(result))
    verified, payload, errors = sup._review_verdict(laddered)

    assert errors == []
    assert verified is False
    assert payload["verdict"] == "rejected"


# ---------------------------------------------------------------------------
# The criteria substitution itself: `cured_when` -> `successCriteria`,
# `clause`/`evidence` -> `description`.
# ---------------------------------------------------------------------------


def test_cure_task_from_finding_uses_cured_when_as_success_criteria():
    finding = _curable_blocking(
        "requirement E.2 (#171)", "the notify tool is missing",
        cured_when="the notify tool exists and is called on completion")
    task = cure_task_from_finding(1, 1, finding)
    assert task["successCriteria"] == (
        "the notify tool exists and is called on completion")
    assert task["description"] == (
        "requirement E.2 (#171) -- the notify tool is missing")


def test_cure_task_from_finding_falls_back_without_cured_when():
    finding = _curable_blocking("requirement E.2 (#171)", "concrete gap")
    task = cure_task_from_finding(1, 1, finding)
    assert "requirement E.2 (#171)" in task["successCriteria"]
    assert "concrete gap" in task["successCriteria"]
    assert task["description"] == "requirement E.2 (#171) -- concrete gap"


def test_insert_cure_tasks_persists_cured_when_as_success_criteria(tmp_path):
    from ralphd.engine.state import RunDir, atomic_write_json

    root = tmp_path / "run"
    root.mkdir()
    run = RunDir(root=root)
    atomic_write_json(run.tasks_file, {"version": 1, "tasks": []})

    finding = _curable_blocking(
        "requirement E.2 (#171)", "the notify tool is missing",
        cured_when="the notify tool exists and is called on completion")
    inserted = insert_cure_tasks(run, 1, [finding])

    assert inserted == ["cure-01-01"]
    tasks = run.read_tasks()["tasks"]
    task = next(t for t in tasks if t["id"] == "cure-01-01")
    assert task["successCriteria"] == (
        "the notify tool exists and is called on completion")
    assert task["description"] == (
        "requirement E.2 (#171) -- the notify tool is missing")


def test_a_cured_when_refused_by_the_bounded_lint_stays_a_criterion_bound_violation():
    """`cured_when` is checked by the SAME bounded-checkable-clause lint a
    planner's own `successCriteria` is (`plan_ingestion.lint_proposal`) --
    an oversized `cured_when` is refused by `review_cured_when_errors`
    with the identical message shape `lint_proposal` itself uses for an
    oversized task criterion (the '~N-clause bound' phrase), not a
    second, differently-worded rule.
    """
    over_bound = ". ".join(f"clause {n}" for n in range(1, 8))
    finding = _curable_blocking("requirement E.2 (#171)", "evidence",
                                cured_when=over_bound)
    errors = transitions.review_cured_when_errors([finding])
    assert errors
    assert f"~{plan_ingestion.MAX_CHECKABLE_CLAUSES}-clause bound" in errors[0]
