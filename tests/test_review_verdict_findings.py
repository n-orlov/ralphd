"""Task 002 (requirement A.2, #110): the review verdict is computed from
blocking findings only, enforced ENGINE-side.

`LoopSupervisor._review_verdict` now reads the structured findings sidecar
(`engine/findings.py`, task 001) beside review's own contract payload and:

* recomputes the true verdict from the findings' severities alone
  (`findings.compute_verdict`) whenever a `rejected` payload's findings are
  ALL residual -- an overcautious rejection is corrected to `verified`, with
  every residual finding still recorded/surfaced (DoD predicate 3);
* trusts a `rejected` payload whose findings are a MIX of blocking and
  residual -- the verdict is computed from the blocking set only, and the
  mix stays `rejected` (DoD predicate 1);
* refuses (never silently rewrites) a `verified` payload that contradicts
  its own findings -- at least one `severity: "blocking"` entry present --
  through the SAME re-ask ladder `_reflect_contract_ladder` already rides
  (`contract_enforcement.enforce_contract`), via the new
  `_review_contract_ladder`.

Two fixtures anchor the two DoD predicates; a third and fourth pin the
contradiction-refusal path (a persistent contradiction never earns
`verified`; a corrected re-ask does).
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


def _finding(severity: str, **overrides) -> dict:
    payload = {
        "severity": severity,
        "curable": True,
        "clause": "requirement A (#110)",
        "evidence": "fixture evidence",
    }
    payload.update(overrides)
    return payload


def _result(text: str) -> IterationResult:
    r = IterationResult(exit_code=0)
    r.final_text = text
    return r


def _supervisor(root: Path) -> LoopSupervisor:
    root.mkdir(parents=True, exist_ok=True)
    run = RunDir(root=root)
    run.update_status(state="starting")
    cfg = JobConfig(run_id="task002-review-findings")
    return LoopSupervisor(cfg, run, root)


def _write_findings(sup: LoopSupervisor, findings_list: list[dict]) -> None:
    findings.write_findings_json(sup.run.findings_json_file, findings_list)


# ---------------------------------------------------------------------------
# DoD predicate 1: mixed blocking+residual -- verdict computed from the
# blocking set only, stays "rejected".
# ---------------------------------------------------------------------------

def test_a_mixed_rejection_computes_its_verdict_from_the_blocking_set_only(
        tmp_path):
    sup = _supervisor(tmp_path)
    mixed = [
        _finding("blocking", clause="requirement A", evidence="observed X"),
        _finding("residual", curable=False, clause="requirement D",
                  evidence="observed Y"),
    ]
    _write_findings(sup, mixed)
    result = _result(json.dumps({
        "verdict": "rejected",
        "reason": "requirement A is unmet",
        "findings_path": "review-findings.md",
    }))

    verified, payload, errors = sup._review_verdict(result)

    assert errors == []
    assert verified is False
    assert payload["verdict"] == "rejected"
    # the blocking finding alone is what earned the rejection -- an
    # all-residual set (below) verifies instead, proving the blocking
    # subset is what actually drives the computation, not the mere
    # presence of ANY finding.
    assert findings.compute_verdict(mixed) == "rejected"


def test_transitions_computes_rejected_from_the_blocking_set_directly():
    """Pure unit pin on `findings.compute_verdict` itself, isolated from
    `_review_verdict`'s I/O: a mixed list is "rejected" because of the
    blocking entry alone, regardless of how many residual ones sit beside
    it."""
    mixed = [_finding("residual"), _finding("residual"),
              _finding("blocking")]
    assert findings.compute_verdict(mixed) == "rejected"


# ---------------------------------------------------------------------------
# DoD predicate 3: an all-residual finding set verifies, with every
# residual still recorded and surfaced (emitted to events.jsonl).
# ---------------------------------------------------------------------------

def test_an_all_residual_rejection_is_recomputed_to_verified_with_residuals_recorded(
        tmp_path):
    sup = _supervisor(tmp_path)
    all_residual = [
        _finding("residual", curable=False, clause="requirement D",
                  evidence="observed Y"),
        _finding("residual", curable=True, clause="requirement E",
                  evidence="observed Z"),
    ]
    _write_findings(sup, all_residual)
    result = _result(json.dumps({
        "verdict": "rejected",
        "reason": "overcautious, no blocking finding actually present",
        "findings_path": "review-findings.md",
    }))

    verified, payload, errors = sup._review_verdict(result)

    assert errors == []
    assert verified is True
    # the payload's OWN verdict field is never rewritten -- the caller
    # (this test, and _step_review in production) reads the RETURNED
    # `verified` bool, not a mutated payload["verdict"].
    assert payload["verdict"] == "rejected"

    events = [json.loads(line) for line in
               (tmp_path / "events.jsonl").read_text().splitlines()]
    recomputed = [e for e in events if e["type"] == "review.verdict_recomputed"]
    assert len(recomputed) == 1
    assert recomputed[0]["claimed_verdict"] == "rejected"
    assert recomputed[0]["computed_verdict"] == "verified"
    assert recomputed[0]["residual_count"] == 2
    # every residual finding is present verbatim -- surfaced, not dropped.
    assert recomputed[0]["residual_findings"] == all_residual


def test_an_empty_findings_list_also_verifies():
    """The edge of DoD predicate 3: no findings at all is the same as "all
    residual" for `compute_verdict` -- an empty list computes "verified"."""
    assert findings.compute_verdict([]) == "verified"


# ---------------------------------------------------------------------------
# The contradiction direction: a "verified" claim alongside a blocking
# finding is refused through the contract re-ask ladder, never silently
# rewritten.
# ---------------------------------------------------------------------------

def test_review_verdict_findings_errors_flags_a_verified_claim_with_a_blocking_finding():
    blocking = [_finding("blocking")]
    errors = transitions.review_verdict_findings_errors(
        {"verdict": "verified"}, blocking)
    assert errors
    assert "blocking" in errors[0]


def test_review_verdict_findings_errors_is_silent_with_no_findings_channel():
    assert transitions.review_verdict_findings_errors(
        {"verdict": "verified"}, None) == []


def test_review_verdict_findings_errors_is_silent_for_a_rejected_claim():
    """A `rejected` verdict carries no such requirement -- the recomputation
    direction is `_review_verdict`'s own job, not this refusal check's."""
    blocking = [_finding("blocking")]
    assert transitions.review_verdict_findings_errors(
        {"verdict": "rejected", "reason": "x"}, blocking) == []


def test_a_persistent_contradiction_never_earns_verified(tmp_path):
    """The hat keeps claiming `verified` on every re-ask despite a blocking
    finding on disk; the ladder exhausts `contract_retry_max` extra
    `_ask_contract_fix` calls and gives up -- `_review_verdict` then reads
    the ORIGINAL contradictory payload, refuses it (contract_errors
    non-empty), and `verified` is `False`. Never silently rewritten to
    `"rejected"` by the ladder or by `_review_verdict` -- the payload's own
    `verdict` field is untouched throughout."""
    sup = _supervisor(tmp_path)
    cfg = sup.cfg
    cfg.contract_retry_max = 1
    _write_findings(sup, [_finding("blocking")])
    contradictory = json.dumps({"verdict": "verified"})
    result = _result(contradictory)

    calls = []

    async def fake_ask_contract_fix(hat, errors):
        calls.append((hat, list(errors)))
        return contradictory, {}

    sup._ask_contract_fix = fake_ask_contract_fix  # type: ignore[method-assign]

    laddered = asyncio.run(sup._review_contract_ladder(result))
    verified, payload, errors = sup._review_verdict(laddered)

    assert len(calls) == cfg.contract_retry_max
    assert calls[0][0] == "review"
    assert any("blocking" in e for e in calls[0][1])
    assert verified is False
    assert errors
    assert payload["verdict"] == "verified"  # untouched, never rewritten


def test_a_corrected_reask_recovers_and_the_second_attempt_verifies(tmp_path):
    """The other half: the FIRST attempt contradicts the findings, but the
    re-ask corrects it to `rejected` with a reason -- the ladder recovers
    the corrected payload, and `_review_verdict` reads it as an ordinary,
    consistent `rejected` verdict (never silently forcing `verified` either
    -- the correction came from the hat's own re-ask answer)."""
    sup = _supervisor(tmp_path)
    _write_findings(sup, [_finding("blocking")])
    first_attempt = json.dumps({"verdict": "verified"})
    corrected = json.dumps({"verdict": "rejected",
                             "reason": "a blocking finding is present"})
    result = _result(first_attempt)

    async def fake_ask_contract_fix(hat, errors):
        return corrected, {}

    sup._ask_contract_fix = fake_ask_contract_fix  # type: ignore[method-assign]

    laddered = asyncio.run(sup._review_contract_ladder(result))
    verified, payload, errors = sup._review_verdict(laddered)

    assert errors == []
    assert verified is False
    assert payload["verdict"] == "rejected"
    assert payload["reason"] == "a blocking finding is present"


def test_the_ladder_is_a_noop_with_no_findings_channel_at_all(tmp_path):
    """No `review-findings.json` was ever written this run (review.md has
    not been updated to always produce one -- task 004's scope): the ladder
    must never fire, and a "verified" payload with no findings channel is
    trusted exactly as it always was."""
    sup = _supervisor(tmp_path)
    result = _result(json.dumps({"verdict": "verified"}))

    laddered = asyncio.run(sup._review_contract_ladder(result))

    assert laddered is result
    verified, payload, errors = sup._review_verdict(laddered)
    assert verified is True
    assert errors == []
