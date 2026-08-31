"""Task 005 (requirement B, #111): classify a rejection's blocking findings
by curability, engine-side.

`engine.findings.all_blocking_findings_incurable(findings)` answers "are
this rejection's blocking findings ALL incurable?" from the structured
findings list alone -- the same list `engine.findings.compute_verdict`
already reads (task 002). Curability is asserted by REVIEW ONLY (PRD B.4):
the function takes no argument beyond the findings list itself -- no run
directory, no workspace path -- so nothing on the run side, including a
file the WORKER can write into its own writable workspace, has any path to
the answer. The value comes from review's own structured findings channel
(`review-findings.json`, written by `findings.write_findings_json`) and
nowhere else.
"""

from __future__ import annotations

import inspect

from ralphd.engine import findings
from ralphd.engine.config import JobConfig
from ralphd.engine.loop import LoopSupervisor
from ralphd.engine.state import RunDir


def _finding(severity: str, curable: bool, **overrides) -> dict:
    payload = {
        "severity": severity,
        "curable": curable,
        "clause": "requirement B (#111)",
        "evidence": "fixture evidence",
    }
    payload.update(overrides)
    return payload


# ---------------------------------------------------------------------------
# All-incurable blocking findings.
# ---------------------------------------------------------------------------

def test_all_incurable_blocking_findings_answer_true():
    all_incurable = [
        _finding("blocking", False, clause="requirement A"),
        _finding("blocking", False, clause="requirement D"),
    ]
    assert findings.all_blocking_findings_incurable(all_incurable) is True


# ---------------------------------------------------------------------------
# At least one curable blocking finding.
# ---------------------------------------------------------------------------

def test_at_least_one_curable_blocking_finding_answers_false():
    mixed = [
        _finding("blocking", False, clause="requirement A"),
        _finding("blocking", True, clause="requirement D"),
    ]
    assert findings.all_blocking_findings_incurable(mixed) is False


def test_all_curable_blocking_findings_answer_false_too():
    all_curable = [_finding("blocking", True)]
    assert findings.all_blocking_findings_incurable(all_curable) is False


# ---------------------------------------------------------------------------
# An empty blocking set (either no findings at all, or findings that are
# all residual) is vacuously "all incurable" -- callers must check
# `compute_verdict(...) == "rejected"` first before routing on this alone.
# ---------------------------------------------------------------------------

def test_an_empty_findings_list_is_vacuously_all_incurable():
    assert findings.all_blocking_findings_incurable([]) is True


def test_an_all_residual_findings_list_is_also_vacuously_all_incurable():
    all_residual = [
        _finding("residual", True, clause="requirement D"),
        _finding("residual", False, clause="requirement E"),
    ]
    assert findings.all_blocking_findings_incurable(all_residual) is True


# ---------------------------------------------------------------------------
# Residual findings' curability is irrelevant -- only the blocking subset
# is inspected, regardless of what sits beside it.
# ---------------------------------------------------------------------------

def test_a_curable_residual_finding_beside_incurable_blocking_ones_is_ignored():
    findings_list = [
        _finding("blocking", False, clause="requirement A"),
        _finding("residual", True, clause="requirement D"),
    ]
    assert findings.all_blocking_findings_incurable(findings_list) is True


def test_an_incurable_residual_finding_beside_a_curable_blocking_one_is_ignored():
    findings_list = [
        _finding("blocking", True, clause="requirement A"),
        _finding("residual", False, clause="requirement D"),
    ]
    # the residual's curable=False does not flip the answer -- the
    # blocking finding's own curable=True is what decides it.
    assert findings.all_blocking_findings_incurable(findings_list) is False


# ---------------------------------------------------------------------------
# Curability is asserted by review only -- no run-side input path (in
# particular, no file the WORKER can write into its own writable
# workspace) reaches this function's answer.
# ---------------------------------------------------------------------------

def test_the_helper_takes_no_argument_besides_the_findings_list():
    """The function's own signature is the first proof: there is no
    second parameter (a run dir, a workspace path, a file path) through
    which anything besides the findings list handed to it could reach the
    answer."""
    params = list(inspect.signature(
        findings.all_blocking_findings_incurable).parameters)
    assert params == ["findings"]


def test_a_worker_writable_workspace_file_cannot_supply_the_answer(tmp_path):
    """Construct the two directories a real run actually has -- an
    engine-owned run dir (`RunDir.root`, where review's own
    `review-findings.json` lives) and a separate, WORKER-writable
    workspace (`LoopSupervisor.workspace`, the directory mounted into the
    worker's own container). Write a file into the worker-writable
    workspace that -- if anything in the answer path read it -- would flip
    an all-incurable rejection to "curable" (and vice versa for a second
    case). The answer must be identical whether or not that file exists,
    because `all_blocking_findings_incurable` never opens any file at all;
    it only ever sees the `findings` argument its caller passes it."""
    run_root = tmp_path / "run"
    workspace = tmp_path / "workspace"
    run_root.mkdir()
    workspace.mkdir()
    run = RunDir(root=run_root)
    run.update_status(state="starting")
    cfg = JobConfig(run_id="task005-curability")
    sup = LoopSupervisor(cfg, run, workspace)
    assert sup.workspace == workspace  # the worker-writable directory

    all_incurable = [_finding("blocking", False, clause="requirement A")]
    before = findings.all_blocking_findings_incurable(all_incurable)

    # A worker with full write access to `sup.workspace` (never to
    # `sup.run.root`) plants a file that claims the opposite answer, at
    # every plausible name/shape a naive implementation might have read.
    (workspace / "review-findings.json").write_text(
        '[{"severity": "blocking", "curable": true, '
        '"clause": "requirement A", "evidence": "forged"}]',
        encoding="utf-8")
    (workspace / "curability_override.json").write_text(
        '{"all_incurable": false}', encoding="utf-8")

    after = findings.all_blocking_findings_incurable(all_incurable)

    assert before is True
    assert after is True
    assert before == after
    # and review's own channel, in the run-owned directory the worker
    # cannot write into, was never touched by this function either.
    assert not run.findings_json_file.exists()
