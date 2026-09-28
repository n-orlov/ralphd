"""Task 006 (v0.27, PRD requirement B.3, #274): what may block a
CONFIRMATION review is bounded, and the engine enforces it.

On a confirmation review (the approach under review has taken at least
one GRANTED cure pass -- `LoopSupervisor._is_confirmation_review`), every
blocking finding review files must carry one of two optional fields to
stay blocking:

* `reopens` -- the cure task id of a listed finding that is still open;
* `regression: true` -- the failure is caused by a change inside the
  diff range.

A blocking finding carrying NEITHER field is downgraded to `"residual"`
before the verdict is computed (`findings.apply_confirmation_review_
downgrade`, wired into `LoopSupervisor._review_verdict`), with a
`review.finding_downgraded` event naming it. Two things always stay
blocking with neither field: an exposed secret, and a requirement whose
behaviour is absent altogether (`findings.confirmation_review_exempt`).

Every node here is red without this task's change: before it,
`_review_verdict` never downgrades an unlinked blocking finding on a
confirmation review at all -- an unlinked finding simply stays blocking,
and no `review.finding_downgraded` event is ever emitted.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from ralphd.engine import findings
from ralphd.engine.runner import IterationResult
from ralphd.engine.state import atomic_write_json

from tests.conftest import supervisor_factory

_supervisor = supervisor_factory(run_id="confirmation-downgrade",
                                 run_root="run", workspace="workspace")


def _git(workspace: Path, *args) -> None:
    subprocess.run(["git", "-C", str(workspace), *args], check=True,
                   capture_output=True)


def _init_git_repo(workspace: Path) -> None:
    _git(workspace, "init", "-q")
    _git(workspace, "config", "user.email", "test@example.com")
    _git(workspace, "config", "user.name", "test")
    (workspace / "a.txt").write_text("one\n")
    _git(workspace, "add", "a.txt")
    _git(workspace, "commit", "-q", "-m", "first")


def _finding(clause: str, evidence: str, **overrides) -> dict:
    payload = {"severity": "blocking", "curable": True, "clause": clause,
               "evidence": evidence, "cured_when": "the fixture cure lands"}
    payload.update(overrides)
    return payload


def _result(text: str) -> IterationResult:
    r = IterationResult(exit_code=0)
    r.final_text = text
    return r


def _rejected() -> IterationResult:
    return _result(json.dumps({
        "verdict": "rejected",
        "reason": "a blocking finding is present",
        "findings_path": "review-findings.md",
    }))


def _make_confirmation_review(tmp_path, prior_finding_clause="prior C.1"):
    """Build a supervisor whose approach 1 has already taken one GRANTED
    cure pass -- `_is_confirmation_review()` is True for it from this
    point on. Returns the supervisor."""
    root = tmp_path / "run"
    sup = _supervisor(root, approach=1)
    atomic_write_json(sup.run.tasks_file, {"version": 1, "tasks": []})
    _init_git_repo(sup.workspace)
    findings.write_findings_json(
        sup.run.findings_json_file,
        [_finding(prior_finding_clause, "prior evidence")])
    inserted = sup._take_cure_pass(1)
    assert len(inserted) == 1
    assert sup._is_confirmation_review() is True
    return sup, inserted[0]


def _events(root: Path) -> list[dict]:
    return [json.loads(line) for line in
            (root / "events.jsonl").read_text().splitlines()]


def test_an_unlinked_blocking_finding_is_downgraded_and_the_event_emitted(
        tmp_path):
    sup, _cure_task_id = _make_confirmation_review(tmp_path)
    unlinked = _finding("requirement D.9 (unlinked)", "still observed")
    findings.write_findings_json(sup.run.findings_json_file, [unlinked])

    verified, payload, errors = sup._review_verdict(_rejected())

    assert errors == []
    # the only blocking finding was downgraded to residual -- the
    # rejection is recomputed to verified (findings.compute_verdict),
    # exactly as an all-residual set already does today.
    assert verified is True
    assert payload["verdict"] == "rejected"  # the claim is never rewritten

    downgraded_events = [e for e in _events(sup.run.root)
                         if e["type"] == "review.finding_downgraded"]
    assert len(downgraded_events) == 1
    assert downgraded_events[0]["clause"] == "requirement D.9 (unlinked)"
    assert downgraded_events[0]["evidence"] == "still observed"


def test_a_reopens_finding_stays_blocking(tmp_path):
    sup, cure_task_id = _make_confirmation_review(tmp_path)
    reopened = _finding("prior C.1", "a new example of the same rule",
                         reopens=cure_task_id)
    findings.write_findings_json(sup.run.findings_json_file, [reopened])

    verified, payload, errors = sup._review_verdict(_rejected())

    assert errors == []
    assert verified is False
    downgraded_events = [e for e in _events(sup.run.root)
                         if e["type"] == "review.finding_downgraded"]
    assert downgraded_events == []


def test_a_regression_finding_stays_blocking(tmp_path):
    sup, _cure_task_id = _make_confirmation_review(tmp_path)
    regressed = _finding("requirement D.10", "broken by the diff",
                          regression=True)
    findings.write_findings_json(sup.run.findings_json_file, [regressed])

    verified, payload, errors = sup._review_verdict(_rejected())

    assert errors == []
    assert verified is False
    downgraded_events = [e for e in _events(sup.run.root)
                         if e["type"] == "review.finding_downgraded"]
    assert downgraded_events == []


def test_the_same_unlinked_finding_stays_blocking_on_a_first_review(
        tmp_path):
    """No cure pass has ever been taken for this approach -- this is a
    FIRST review, not a confirmation review -- so the identical unlinked
    finding that the first test downgrades stays blocking here."""
    root = tmp_path / "run"
    sup = _supervisor(root, approach=1)
    atomic_write_json(sup.run.tasks_file, {"version": 1, "tasks": []})
    _init_git_repo(sup.workspace)
    assert sup._is_confirmation_review() is False
    unlinked = _finding("requirement D.9 (unlinked)", "still observed")
    findings.write_findings_json(sup.run.findings_json_file, [unlinked])

    verified, payload, errors = sup._review_verdict(_rejected())

    assert errors == []
    assert verified is False
    downgraded_events = [e for e in _events(sup.run.root)
                         if e["type"] == "review.finding_downgraded"]
    assert downgraded_events == []


def test_an_exposed_secret_finding_stays_blocking_with_neither_field(
        tmp_path):
    sup, _cure_task_id = _make_confirmation_review(tmp_path)
    secret_finding = _finding(
        "requirement F.1", "a live API secret is printed in the log")
    findings.write_findings_json(sup.run.findings_json_file, [secret_finding])

    verified, payload, errors = sup._review_verdict(_rejected())

    assert errors == []
    assert verified is False
    downgraded_events = [e for e in _events(sup.run.root)
                         if e["type"] == "review.finding_downgraded"]
    assert downgraded_events == []


def test_an_absent_behaviour_finding_stays_blocking_with_neither_field(
        tmp_path):
    sup, _cure_task_id = _make_confirmation_review(tmp_path)
    absent_finding = _finding(
        "requirement F.2", "the named behaviour is absent from the code")
    findings.write_findings_json(sup.run.findings_json_file, [absent_finding])

    verified, payload, errors = sup._review_verdict(_rejected())

    assert errors == []
    assert verified is False
    downgraded_events = [e for e in _events(sup.run.root)
                         if e["type"] == "review.finding_downgraded"]
    assert downgraded_events == []


def test_confirmation_review_downgrade_candidates_is_a_pure_unit(tmp_path):
    """Pure unit pin on `findings.apply_confirmation_review_downgrade`
    itself, isolated from `_review_verdict`'s I/O."""
    unlinked = _finding("requirement Z", "observed")
    linked = _finding("requirement Y", "observed", reopens="cure-01-01")
    new_list, downgraded = findings.apply_confirmation_review_downgrade(
        [unlinked, linked])
    assert new_list[0]["severity"] == "residual"
    assert new_list[1]["severity"] == "blocking"
    assert downgraded == [new_list[0]]
    # the input list and its dicts are never mutated.
    assert unlinked["severity"] == "blocking"
