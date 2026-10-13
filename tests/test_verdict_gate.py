"""The peer verdict gate's own surfaces, folded from five per-task pin
modules (D.7 sweep, task 028) that each pinned one facet of `peer_verdict_gate`
/`peer_plan_review` and the review-verdict labelling next to it:

- task 019 (D.4, #125): the gate's OWN findings path
  (`verdict-gate-findings.md`/`.json`, distinct from review's own) plus
  the engine-attributed `author` field on every structured finding.
- task 020 (D.5, #125 A): `state.detect_verdict_gate_peek` -- a post-hoc
  check that a gate iteration's transcript never read the paths a
  genuinely blind peer must not see, recorded but never adjudicated.
- task 017 (D.3, #117 side finding): `ralphctl start --peer-plan-review`/
  `--peer-verdict-gate` and their `JobConfig.effective()['flags']` surface.
- task 016 (D.2, #117 half B): `plan_review`'s undecidable branch (no
  usable verdict) is a declared deterministic policy, never a live
  `engine.router.decide()` call.
- task 012 (D.3): a review that VERIFIES over a residual finding records
  `verdict: "verified-with-residuals"`, never plain `"verified"` and never
  `"unknown"` on the CLI/hub read side.

Two source modules for the scripted-agent harness are reused throughout
and imported under module-specific aliases where a bare name would
otherwise collide across sections: `test_peer_verdict_gate` (sections 1-2,
used bare) and `test_peer_plan_review_gate` (section 4, aliased `_pr_*`
-- a different fixture set, same names, bound to `plan_review`'s own
`_ScriptedAgent`/`_supervisor` rather than the verdict gate's).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
from pathlib import Path

import pytest
import yaml

from ralphd.engine import findings, state, transitions
from ralphd.engine.config import JobConfig
from ralphd.engine.loop import LoopSupervisor
from ralphd.engine.runner import IterationResult
from ralphd.engine.state import RunDir, atomic_write_json
from tests.conftest import supervisor_factory

from test_cli_docker import Ctl
from test_peer_plan_review_gate import (  # noqa: F401 (re-exported fixtures)
    _ok as _pr_ok,
    _plan as _pr_plan,
    _plan_review_approves,
    _review_verifies as _pr_review_verifies,
    _router_decisions,
    _run_job as _pr_run_job,
    _supervisor as _pr_supervisor,
    _worker_completes as _pr_worker_completes,
)
from test_peer_verdict_gate import (  # noqa: F401 (re-exported fixtures)
    TASK_1,
    _bounded,
    _drive_until_parked,
    _ok,
    _plan,
    _run_job,
    _seed_all_completed,
    _supervisor,
    _review_verifies,
    _verdict_gate_approves,
    _verdict_gate_rejects,
    _worker_completes,
)


# Task 072 (steer 001 §2): every bare `asyncio.run(sup.<loop-driving call>)`
# in this module is wrapped through here instead. The forever-await this
# guards is `_step_paused`'s `await self._gate()`, parked on the
# `asyncio.Event` only `POST /pause`'s `loop.pause()` clears.
#
# Correcting this comment's first version (task 072, first attempt), which
# claimed the `verdict_gate` edges never target `paused` and inferred from
# that this module could not reach the Event at all: the inference was
# wrong, because `_run_job`/`_pr_run_job` -- imported from
# `test_peer_verdict_gate`/`test_peer_plan_review_gate` and used at five
# call sites here -- drive the WHOLE loop, `review`'s own edges included. A
# throwaway probe over `_run_job` with a review scripted to reject with an
# all-curable findings set on the FINAL approach reached `state: "paused"`,
# `pauseReason: "rejected_last_approach"` and then hung: `timeout 25` exited
# 124 with no summary line. Those two imported helpers carry the bound in
# their own modules now (a bound written here cannot reach an `asyncio.run`
# inside an imported helper), which is what actually closes this module's
# five loop-driving call sites. The second known forever-await -- a
# `_ScriptedRunner` script whose repeating last step never reaches a
# terminal phase -- does not reproduce here: this module's scripted
# handlers all reach a deterministic terminal verdict.
def _bounded(coro, timeout: float = 30.0):
    return asyncio.run(asyncio.wait_for(coro, timeout))


# ===========================================================================
# Section 1 (task 019, D.4, #125): the gate's own findings path + authorship
# ===========================================================================

def test_state_declares_a_distinct_fixed_path_for_the_gate():
    assert state.VERDICT_GATE_FINDINGS_FILE == "verdict-gate-findings.md"
    assert state.VERDICT_GATE_FINDINGS_JSON_FILE == "verdict-gate-findings.json"
    assert state.VERDICT_GATE_FINDINGS_FILE != state.FINDINGS_FILE
    assert state.VERDICT_GATE_FINDINGS_JSON_FILE != state.FINDINGS_JSON_FILE


def test_rundir_exposes_the_gate_s_own_findings_paths(tmp_path):
    run = state.RunDir(root=tmp_path)
    assert run.verdict_gate_findings_file == tmp_path / "verdict-gate-findings.md"
    assert run.verdict_gate_findings_json_file == tmp_path / "verdict-gate-findings.json"


# ---------------------------------------------------------------------------
# Unit: findings.attribute_authorship
# ---------------------------------------------------------------------------


def _finding(**overrides) -> dict:
    payload = {"severity": "blocking", "curable": True,
              "clause": "requirement Q", "evidence": "observed"}
    payload.update(overrides)
    return payload


def test_attribute_authorship_sets_the_field_when_absent():
    out = findings.attribute_authorship([_finding()], "verdict_gate")
    assert out[0]["author"] == "verdict_gate"


def test_attribute_authorship_overwrites_whatever_was_already_there():
    """The engine's own attribution is unconditional -- it never trusts a
    hat's own (possibly wrong) `author` claim over the fixed channel the
    finding was actually read from."""
    out = findings.attribute_authorship([_finding(author="somebody-else")],
                                        "review")
    assert out[0]["author"] == "review"


def test_attribute_authorship_never_mutates_its_input():
    original = [_finding()]
    findings.attribute_authorship(original, "review")
    assert "author" not in original[0]


def test_author_is_a_validating_optional_finding_field():
    """Adding `author` to `FINDING_SCHEMA` must not turn additional-
    properties enforcement into a hard rejection -- a genuinely unknown
    field is tolerated (task 014, requirement E.1, #190: named by a
    warning, never itself a validation error), and a finding lacking
    `author` altogether (every fixture in `tests/test_structured_
    findings.py`) still validates unchanged."""
    assert findings.validate_finding(_finding(author="review")) == []
    assert findings.validate_finding(_finding()) == []
    assert findings.validate_finding(_finding(bogus_field="x")) == []
    assert findings.finding_warnings(_finding(bogus_field="x")) != []


# ---------------------------------------------------------------------------
# End to end: review verifies with a RESIDUAL finding on record, the gate
# then rejects with its OWN blocking finding -- the two artifacts must be
# distinguishable by name and by author once archived and folded.
# ---------------------------------------------------------------------------


def _review_verifies_with_a_residual_finding(sup, prompt=""):
    findings.write_findings_json(sup.run.findings_json_file, [_finding(
        severity="residual", curable=True, clause="requirement Z (residual)",
        evidence="review's own residual observation")])
    sup.run.findings_file.write_text(
        "### Residual\nrequirement Z could use polish, but is not blocking.\n")
    return _ok(json.dumps({"verdict": "verified"}))


def _verdict_gate_rejects_with_a_blocking_finding(reason: str):
    def handler(sup, prompt=""):
        # Task 001 (PRD requirement A.1, #192): `curable=False` here, not
        # this module's own `_finding` default (`True`) -- since that task
        # a curable-throughout rejection takes the cure-pass edge
        # (`verdict_gate` -> `cure`) instead of ending the run, and this
        # test's own point is the archived-artifact distinguishability a
        # terminal (`replan`/`finishing`) rejection produces, not the cure
        # pass -- `tests/test_peer_verdict_gate.py`'s own new node covers
        # the curable case end to end.
        findings.write_findings_json(sup.run.verdict_gate_findings_json_file, [
            _finding(clause="requirement Q (blocking)",
                    evidence="the peer's own independent observation",
                    curable=False)])
        sup.run.verdict_gate_findings_file.write_text(
            "### Blocking\nrequirement Q is unmet, independently re-checked.\n")
        return _ok(json.dumps({"verdict": "rejected", "reason": reason}))
    return handler


def test_review_verified_with_residuals_then_gate_rejects_yields_two_distinguishable_artifacts(tmp_path):
    """Retired-in-place (task cure-01-01-3, PRD requirement A.3's
    removal-specific review rule): before this task, a last-approach
    verdict-gate rejection carrying a non-curable blocking finding still
    ran the ordinary `_archive_approach`/`_write_composite_prd` machinery,
    so this test could pin the two findings' distinguishability against
    the ARCHIVED copies under `approaches/01/` and the composite PRD the
    (never-taken) next approach would have read. Requirement A.3 removes
    that fall-through unconditionally -- last approach or not, a MERITS
    rejection with a readable gate-findings channel now ends the run
    RIGHT HERE, with no archive and no composite PRD at all (exactly the
    shape `tests/test_peer_verdict_gate.py::test_a_mix_of_curable_and_
    incurable_gate_findings_still_ends_the_run` and `..._ends_the_run_
    with_an_approach_left` already assert for the mixed-findings case),
    so an archive-shaped assertion here can never pass again.

    This node's own point -- review's and the peer gate's findings stay
    distinguishable by filename and by severity even when BOTH channels
    fire in the same approach -- still holds and is reasserted below,
    just read from the RUN-SCOPE paths (`RunDir.findings_file`/
    `.findings_json_file`/`.verdict_gate_findings_file`/
    `.verdict_gate_findings_json_file`) the two fixture handlers write
    directly and that this rejection leaves untouched, rather than from
    an archive this rejection no longer creates. The `author` distinction
    is dropped from this rewrite rather than reasserted at run scope:
    per `LoopSupervisor._archive_findings_json`'s own docstring, `author`
    is an ARCHIVING-time enrichment (`findings.attribute_authorship`)
    stamped only on the copy written into `approaches/NN/` -- with no
    archive written for this scenario any more, the run-scope sidecars
    stay exactly as each fixture handler wrote them (no `author` key at
    all), so asserting one here would test the fixture's own literal,
    not engine behaviour. `attribute_authorship` itself keeps its own
    unit cover (`test_attribute_authorship_sets_the_field_when_absent`,
    `test_attribute_authorship_overwrites_whatever_was_already_there`,
    both still in this module), and its call site
    (`_archive_findings_json`) keeps running end to end on every
    rejection that DOES still archive, e.g. `tests/test_peer_verdict_
    gate.py::test_gate_on_rejection_with_an_approach_left_starts_a_
    fresh_approach`. Its surviving cover for the no-archive/no-
    composite/no-cure-task shape itself is `tests/test_peer_verdict_
    gate.py::test_a_mix_of_curable_and_incurable_gate_findings_still_
    ends_the_run` (last approach) and `..._ends_the_run_with_an_
    approach_left` (approach left)."""
    root = tmp_path / "run"
    root.mkdir(parents=True, exist_ok=True)
    (root / "prd.md").write_text("# test PRD\n\nDo the thing.\n")

    RunDir(root=root).update_status(state="starting")
    sup = _supervisor(
        root,
        {"planning": [_plan()],
         "worker": [_worker_completes(TASK_1)],
         "review": [_review_verifies_with_a_residual_finding],
         "verdict_gate": [_verdict_gate_rejects_with_a_blocking_finding(
             "the peer independently found requirement Q unmet")]},
        peer_verdict_gate=True, max_approaches=1,
        operator_input_needed_pause=True)
    _bounded(_drive_until_parked(sup))

    # v0.36 requirement B: a peer rejection carrying a non-curable finding
    # parks in `awaiting_input` (it is never terminal on the spot).
    status = sup.run.read_status()
    assert status["state"] == "awaiting_input", status
    assert status["inputQuestion"]["judge"] == "verdict_gate"

    # the park never archives and never writes a composite PRD.
    assert not (root / "approaches" / "01").exists()
    assert not (root / "composite-prd.md").exists()

    review_md = sup.run.findings_file
    gate_md = sup.run.verdict_gate_findings_file
    review_json_path = sup.run.findings_json_file
    gate_json_path = sup.run.verdict_gate_findings_json_file

    # distinguishable by NAME: two different filenames, both still on
    # disk at RUN scope (never archived, but never deleted either).
    assert review_md.exists()
    assert gate_md.exists()
    assert review_md.name != gate_md.name
    assert "requirement Z" in review_md.read_text()
    assert "requirement Q" in gate_md.read_text()

    # distinguishable by SEVERITY: no archiving-time attribution needed
    # for this -- each hat's own reply already carried its own severity.
    review_items = findings.read_findings_json(review_json_path)
    gate_items = findings.read_findings_json(gate_json_path)
    assert review_items[0]["severity"] == "residual"
    assert gate_items[0]["severity"] == "blocking"


# ===========================================================================
# Section 2 (task 020, D.5, #125 A): the verdict-gate-peek detector
# ===========================================================================

def _tool_event(command: str, tool: str = "bash") -> dict:
    return {"type": "tool_execution_start", "toolCallId": "call_1",
            "toolName": tool, "args": {"command": command}}


def _write_transcript(root, number: int, *lines: dict) -> None:
    itdir = root / "iterations" / f"{number:04d}"
    itdir.mkdir(parents=True, exist_ok=True)
    with open(itdir / "output.jsonl", "a") as fh:
        for line in lines:
            fh.write(json.dumps(line) + "\n")
    atomic_write_json(itdir / "meta.json", {"endedAt": "2024-01-01T00:00:00Z"})


# ---------------------------------------------------------------------------
# Part 1: the detector itself, unit-level
# ---------------------------------------------------------------------------


def test_a_clean_transcript_produces_no_flag(tmp_path):
    root = tmp_path / "run"
    _write_transcript(root, 1, _tool_event("ls -la src/ralphd"))
    assert state.detect_verdict_gate_peek(root, 1) == []


def test_a_bash_read_of_events_jsonl_is_flagged(tmp_path):
    root = tmp_path / "run"
    _write_transcript(root, 1, _tool_event(f"cat {root}/events.jsonl"))
    flagged = state.detect_verdict_gate_peek(root, 1)
    assert len(flagged) == 1
    assert flagged[0]["tool"] == "bash"
    assert flagged[0]["matched"] == ["events.jsonl"]
    assert "events.jsonl" in flagged[0]["args"]


def test_a_read_tool_path_into_iterations_is_flagged(tmp_path):
    root = tmp_path / "run"
    _write_transcript(root, 1, {
        "type": "tool_execution_start", "toolCallId": "call_1",
        "toolName": "read",
        "args": {"path": f"{root}/iterations/0003/output.jsonl"}})
    flagged = state.detect_verdict_gate_peek(root, 1)
    assert len(flagged) == 1
    assert flagged[0]["tool"] == "read"
    assert set(flagged[0]["matched"]) == {"iterations/", "output.jsonl"}


def test_a_clean_call_amid_flagged_ones_is_not_itself_flagged(tmp_path):
    root = tmp_path / "run"
    _write_transcript(
        root, 1,
        _tool_event("ls -la"),
        _tool_event(f"cat {root}/iterations/0001/output.jsonl"),
        _tool_event("git status"))
    flagged = state.detect_verdict_gate_peek(root, 1)
    assert len(flagged) == 1
    assert set(flagged[0]["matched"]) == {"iterations/", "output.jsonl"}


def test_a_workspace_targeted_read_of_events_jsonl_produces_no_peek_record(
        tmp_path):
    """Task 051 (J.2, J.3): a `verdict_gate` iteration legitimately
    grepping `src/` for the literal `events.jsonl` -- which this repository
    spells throughout, including in this very module's own docstrings --
    never mentions the run-state directory's own path, so it must not be
    flagged just because the bare word appears."""
    root = tmp_path / "run"
    _write_transcript(
        root, 1,
        _tool_event("grep -rn events.jsonl src/ralphd/engine/state.py"))
    assert state.detect_verdict_gate_peek(root, 1) == []


def test_a_read_of_the_run_dirs_own_events_jsonl_is_still_flagged(tmp_path):
    """The other half of J.3: a call that actually reaches into the run
    dir's own `events.jsonl` -- not merely a workspace path containing the
    same literal -- is still flagged."""
    root = tmp_path / "run"
    _write_transcript(root, 1, _tool_event(f"cat {root}/events.jsonl"))
    flagged = state.detect_verdict_gate_peek(root, 1)
    assert len(flagged) == 1
    assert flagged[0]["matched"] == ["events.jsonl"]


def test_a_missing_transcript_produces_no_flag(tmp_path):
    root = tmp_path / "run"
    root.mkdir(parents=True)
    assert state.detect_verdict_gate_peek(root, 7) == []


def test_a_malformed_line_is_skipped_not_raised(tmp_path):
    root = tmp_path / "run"
    itdir = root / "iterations" / "0001"
    itdir.mkdir(parents=True)
    (itdir / "output.jsonl").write_text(
        'not json at all, but mentions events.jsonl anyway\n'
        + json.dumps(_tool_event("ls")) + "\n")
    assert state.detect_verdict_gate_peek(root, 1) == []


def test_only_the_named_iteration_number_is_scanned(tmp_path):
    """The detector is scoped to ONE iteration -- a peek recorded against a
    different iteration number (e.g. an earlier `worker` call that
    legitimately reads its own prior output) is never mistaken for this
    gate call's own."""
    root = tmp_path / "run"
    _write_transcript(root, 1, _tool_event(f"cat {root}/events.jsonl"))
    _write_transcript(root, 2, _tool_event("ls -la"))
    assert state.detect_verdict_gate_peek(root, 1) != []
    assert state.detect_verdict_gate_peek(root, 2) == []


# ---------------------------------------------------------------------------
# Part 2: wired into the real dispatch path
# ---------------------------------------------------------------------------


def _inject_after_phase(agent, phase: str, *lines: dict):
    """Wraps `agent.run` so that, once the scripted reply for `phase` has
    been produced, `lines` are appended to that SAME call's transcript file
    before `run()` returns -- simulating a hat that answered the contract
    correctly (the scripted handler's normal reply) while ALSO having made
    an out-of-band tool call the honor system was supposed to forbid.
    `_ScriptedAgent.run` (test_peer_verdict_gate.py) never itself writes to
    the transcript path it is handed, so nothing here fights that class;
    it only adds what a real `pi` subprocess would have appended."""
    original_run = agent.run

    async def wrapped(prompt, transcript, **kw):
        result = await original_run(prompt, transcript, **kw)
        if agent.phases_seen and agent.phases_seen[-1] == phase:
            with open(transcript, "a") as fh:
                for line in lines:
                    fh.write(json.dumps(line) + "\n")
        return result

    agent.run = wrapped


def test_a_flagged_peek_on_the_ordinary_gate_never_flips_an_approval(tmp_path):
    root = tmp_path / "run"
    root.mkdir(parents=True, exist_ok=True)
    RunDir(root=root).update_status(state="starting")
    sup = _supervisor(
        root,
        {"planning": [_plan()], "worker": [_worker_completes(TASK_1)],
         "review": [_review_verifies()],
         "verdict_gate": [_verdict_gate_approves]},
        peer_verdict_gate=True)
    _inject_after_phase(sup.runner, "verdict_gate",
                        _tool_event(f"cat {root}/events.jsonl"))

    final_state = _bounded(sup._run_job_core())

    assert sup.runner.phases_seen == ["planning", "worker", "review",
                                      "verdict_gate"]
    # the verdict itself is UNCHANGED by the peek: an approving reply
    # still reaches terminal success.
    assert final_state == "succeeded", sup.run.read_status()
    status = sup.run.read_status()
    assert status.get("state") == "succeeded"
    peeks = status.get("verdictGatePeeks")
    assert peeks and len(peeks) == 1
    entry = peeks[0]
    assert entry["approach"] == 1
    assert entry["calls"][0]["matched"] == ["events.jsonl"]

    events = [json.loads(line) for line in
              (root / "events.jsonl").read_text().splitlines()]
    peek_events = [e for e in events if e.get("type") == "verdict_gate_peek"]
    assert len(peek_events) == 1
    assert peek_events[0]["approach"] == 1
    assert peek_events[0]["calls"][0]["matched"] == ["events.jsonl"]


def test_a_clean_gate_iteration_records_no_flag(tmp_path):
    root = tmp_path / "run"
    sup, final_state = _run_job(
        root,
        {"planning": [_plan()], "worker": [_worker_completes(TASK_1)],
         "review": [_review_verifies()],
         "verdict_gate": [_verdict_gate_approves]},
        peer_verdict_gate=True)

    assert final_state == "succeeded", sup.run.read_status()
    status = sup.run.read_status()
    assert not status.get("verdictGatePeeks")
    events = [json.loads(line) for line in
              (root / "events.jsonl").read_text().splitlines()]
    assert not [e for e in events if e.get("type") == "verdict_gate_peek"]


def test_a_flagged_peek_on_a_rejection_is_still_just_recorded(tmp_path):
    """The flag is recorded on a rejecting reply too -- it is an
    observation about the CALL, independent of which verdict the hat
    happened to return; the run's own terminal outcome here is driven
    entirely by the `rejected` verdict, not by the peek."""
    root = tmp_path / "run"
    root.mkdir(parents=True, exist_ok=True)
    RunDir(root=root).update_status(state="starting")
    sup = _supervisor(
        root,
        {"planning": [_plan()], "worker": [_worker_completes(TASK_1)],
         "review": [_review_verifies()],
         "verdict_gate": [_verdict_gate_rejects("no thanks")]},
        peer_verdict_gate=True, max_approaches=1)
    _inject_after_phase(sup.runner, "verdict_gate",
                        _tool_event(f"cat {root}/iterations/0003/output.jsonl"))

    final_state = _bounded(sup._run_job_core())

    assert final_state == "failed", sup.run.read_status()
    status = sup.run.read_status()
    peeks = status.get("verdictGatePeeks")
    assert peeks and len(peeks) == 1
    assert set(peeks[0]["calls"][0]["matched"]) == {"iterations/", "output.jsonl"}


def test_there_is_only_one_verdict_gate_call_site_now(tmp_path):
    """Task 015 (requirement E.1, #169) retired `_maybe_grace_review` and its
    off-budget `_grace_verdict_gate` twin, the second call site the retired
    `test_the_off_budget_grace_gate_is_checked_too` used to cover here.
    `_step_verdict_gate` is the only one left; this pins that both retired
    names are actually gone rather than merely unused, so nothing could
    quietly resurrect a second, unaccounted-for peer-gate path."""
    assert not hasattr(LoopSupervisor, "_maybe_grace_review")
    assert not hasattr(LoopSupervisor, "_grace_verdict_gate")


# ===========================================================================
# Section 3 (task 017, D.3, #117 side finding): --peer-plan-review/
# --peer-verdict-gate CLI flags and their JobConfig/effective() surface
# ===========================================================================

@pytest.fixture
def ctl(tmp_path, capsys):
    return Ctl(tmp_path, capsys)


def _job_yaml_path(ctl: Ctl, run_id: str):
    matches = list(ctl.tmp.glob(f"**/configs/{run_id}/job.yaml"))
    assert len(matches) == 1, f"expected one job.yaml for {run_id}, found {matches}"
    return matches[0]


def test_start_accepts_both_peer_gate_flags(ctl):
    res = ctl.run("start", "--prd", str(ctl.prd), "--llm", "none",
                  "--run-id", "peer-gates-on",
                  "--peer-plan-review", "--peer-verdict-gate")
    assert res.returncode == 0, res.stderr
    job = yaml.safe_load(_job_yaml_path(ctl, "peer-gates-on").read_text())
    assert job["peer_plan_review"] is True
    assert job["peer_verdict_gate"] is True


def test_the_cli_flag_drives_through_to_the_config_object(ctl):
    """Not just job.yaml -- the flag reaches a real `JobConfig.load()`
    read of the file `start` wrote, the same object the engine itself
    loads at boot."""
    res = ctl.run("start", "--prd", str(ctl.prd), "--llm", "none",
                  "--run-id", "peer-gates-load",
                  "--peer-plan-review", "--peer-verdict-gate")
    assert res.returncode == 0, res.stderr
    path = _job_yaml_path(ctl, "peer-gates-load")
    cfg = JobConfig.load(path)
    assert cfg.peer_plan_review is True
    assert cfg.peer_verdict_gate is True


def test_omitting_the_flags_keeps_both_gates_off(ctl):
    """Unset -- like `--vigilant`/`--reflect` -- stays out of job.yaml
    entirely, so the engine's own `JobConfig` default (False) applies."""
    res = ctl.run("start", "--prd", str(ctl.prd), "--llm", "none",
                  "--run-id", "peer-gates-off")
    assert res.returncode == 0, res.stderr
    job = yaml.safe_load(_job_yaml_path(ctl, "peer-gates-off").read_text())
    assert "peer_plan_review" not in job
    assert "peer_verdict_gate" not in job
    cfg = JobConfig.load(_job_yaml_path(ctl, "peer-gates-off"))
    assert cfg.peer_plan_review is False
    assert cfg.peer_verdict_gate is False


def test_both_peer_gate_keys_appear_in_effective_flags():
    flags = JobConfig(run_id="t").effective()["flags"]
    assert "peerPlanReview" in flags and flags["peerPlanReview"] is False
    assert "peerVerdictGate" in flags and flags["peerVerdictGate"] is False


def test_both_peer_gate_keys_reflect_true_when_the_gates_are_on():
    flags = JobConfig(run_id="t", peer_plan_review=True,
                       peer_verdict_gate=True).effective()["flags"]
    assert flags["peerPlanReview"] is True
    assert flags["peerVerdictGate"] is True


# ===========================================================================
# Section 4 (task 016, D.2, #117 half B): plan_review's undecidable branch
# is a declared deterministic policy, never a live router.decide() call
# ===========================================================================

def _garbage(sup, prompt=""):
    return _pr_ok("not valid json at all, no verdict anywhere in sight")


def test_an_undecidable_reply_spends_the_revision_directly_no_router_asked(tmp_path):
    """`payload is None and revision_available` -> the revision is spent
    right here, in `_step_plan_review` itself -- no call into
    `_route_with_router`/`engine.router.decide()` at all. A test that
    declared no `router_answer=...` would fail loudly on any attempted
    router call (`_supervisor`'s `fake_ask_router` asserts `answers` is
    non-empty before it ever answers), so a passing run here IS the
    zero-call proof, reinforced by the two direct assertions below."""
    root = tmp_path / "run"
    sup, final_state = _pr_run_job(
        root,
        {"planning": [_pr_plan(), _pr_plan()],
         "plan_review": [_garbage, _plan_review_approves],
         "worker": [_pr_worker_completes(TASK_1)], "review": [_pr_review_verifies]},
        peer_plan_review=True)

    agent = sup.runner
    assert agent.phases_seen == [
        "planning", "plan_review", "planning", "plan_review", "worker",
        "review"]
    assert final_state == "succeeded", sup.run.read_status()

    # the revision was spent exactly as an `objected`-with-no-reason
    # verdict already would have -- same accounting as before task 016.
    revisions = sup.run.read_status().get("planReviewRevisions")
    assert revisions == {"1": 1}

    # no router call happened for this branch: neither the model channel
    # nor the decisions ledger saw anything.
    assert sup.router_prompts == []
    assert _router_decisions(root) == []


def test_the_revision_reprompt_carries_the_raw_reply_and_the_parse_errors(tmp_path):
    """The next `planning` attempt's prompt must contain the peer's own
    raw reply text and a concrete parse-error message -- not the old
    boilerplate ("the router routed the plan back for one revision")."""
    raw_garbage = "not valid json at all, no verdict anywhere in sight"

    def _garbage_reply(sup, prompt=""):
        return _pr_ok(raw_garbage)

    root = tmp_path / "run"
    sup, final_state = _pr_run_job(
        root,
        {"planning": [_pr_plan(), _pr_plan()],
         "plan_review": [_garbage_reply, _plan_review_approves],
         "worker": [_pr_worker_completes(TASK_1)], "review": [_pr_review_verifies]},
        peer_plan_review=True)

    assert final_state == "succeeded", sup.run.read_status()
    agent = sup.runner
    second_planning_prompt = agent.prompts[agent.phases_seen.index(
        "planning", 1)]

    # the peer's raw reply text reaches the next planning attempt verbatim.
    assert raw_garbage in second_planning_prompt
    # a concrete parse error accompanies it, not vague boilerplate.
    assert "could not parse a JSON object" in second_planning_prompt
    assert "the router routed the plan back" not in second_planning_prompt


def test_a_schema_invalid_reply_reprompt_carries_its_own_contract_errors(tmp_path):
    """A reply that DOES parse as JSON but fails `plan_review`'s own
    contract (schema violation, or `objected` with no/blank reason) is
    just as undecidable -- its re-prompt carries those contract errors,
    not a generic parse-failure message, since the JSON parsed fine."""
    def _schema_invalid(sup, prompt=""):
        return _pr_ok('{"verdict": "objected"}')  # no "reason" -> semantic error

    root = tmp_path / "run"
    sup, final_state = _pr_run_job(
        root,
        {"planning": [_pr_plan(), _pr_plan()],
         "plan_review": [_schema_invalid, _plan_review_approves],
         "worker": [_pr_worker_completes(TASK_1)], "review": [_pr_review_verifies]},
        peer_plan_review=True)

    assert final_state == "succeeded", sup.run.read_status()
    agent = sup.runner
    second_planning_prompt = agent.prompts[agent.phases_seen.index(
        "planning", 1)]
    assert '{"verdict": "objected"}' in second_planning_prompt
    assert sup.router_prompts == []
    assert _router_decisions(root) == []


def test_the_revision_bound_reached_fallback_still_bounds_the_loop(tmp_path):
    """With the revision bound already spent, an undecidable reply still
    falls through to `plan_review_revision_bound_reached` -> `worker` --
    fail OPEN on a bounded gate, never a deadlock, and (task 016) never a
    router call either: `planning` is not a legal move here regardless of
    what the reply says, decidable or not."""
    root = tmp_path / "run"
    RunDir(root=root).update_status(state="starting",
                                    planReviewRevisions={"1": 1})
    sup = _pr_supervisor(
        root,
        {"planning": [_pr_plan()], "plan_review": [_garbage],
         "worker": [_pr_worker_completes(TASK_1)], "review": [_pr_review_verifies]},
        peer_plan_review=True, peer_plan_review_max_revisions=1)
    final_state = _bounded(sup._run_job_core())

    assert sup.runner.phases_seen == ["planning", "plan_review", "worker",
                                      "review"]
    assert final_state == "succeeded", sup.run.read_status()
    # the loop proceeded to worker rather than looping or faulting.
    assert sup.run.read_status().get("planReviewRevisions") == {"1": 1}
    # bounded and deterministic: no router call, no decision recorded.
    assert sup.router_prompts == []
    assert _router_decisions(root) == []


def test_the_bound_reached_fallback_is_a_real_legal_table_edge(tmp_path):
    """`plan_review_revision_bound_reached` is a declared edge in the
    table, not an ad hoc string -- guards against the fallback drifting
    out of sync with `transitions.py` while this module only exercises
    the loop-level behaviour above."""
    from ralphd.engine import transitions as tr

    assert tr.fallback_guard("plan_review") == \
        "plan_review_revision_bound_reached"
    edges = tr.successors("plan_review", vigilant=False,
                          peer_plan_review=True)
    assert any(e.guard == "plan_review_revision_bound_reached"
               and e.target == "worker" for e in edges)


# ===========================================================================
# Section 5 (task 012, D.3): a VERIFIED review over a residual finding
# records "verified-with-residuals", never plain "verified" or "unknown"
# ===========================================================================

def _residual_finding(severity: str, **overrides) -> dict:
    payload = {
        "severity": severity,
        "curable": True,
        "clause": "requirement D.3",
        "evidence": "fixture evidence",
    }
    payload.update(overrides)
    return payload


def _result(text: str) -> IterationResult:
    r = IterationResult(exit_code=0)
    r.final_text = text
    r.duration_s = 5.0
    r.usage = {"input": 10, "output": 5, "totalTokens": 15}
    return r


_residuals_supervisor = supervisor_factory(run_id="task012-verified-with-residuals",
                                 cfg_defaults=False,
                                 prd="# task 012 D.3 fixture PRD\n\nDo the thing.\n",
                                 status={"state": "running"})


# ---------------------------------------------------------------------------
# 1. Pure unit pin on the new findings-module predicate.
# ---------------------------------------------------------------------------

def test_has_residual_findings_is_true_only_with_a_residual_entry():
    assert findings.has_residual_findings([]) is False
    assert findings.has_residual_findings([_residual_finding("blocking")]) is False
    assert findings.has_residual_findings([_residual_finding("residual")]) is True
    assert findings.has_residual_findings(
        [_residual_finding("blocking"), _residual_finding("residual")]) is True


# ---------------------------------------------------------------------------
# 2. A clean findings channel still records the plain "verified" verdict,
#    state "succeeded".
# ---------------------------------------------------------------------------

def test_a_clean_findings_channel_still_verifies_plainly(tmp_path):
    sup = _residuals_supervisor(tmp_path / "run")
    sup._approach = 1
    findings.write_findings_json(sup.run.findings_json_file, [])

    async def _review_verifies(prompt, transcript, **kw):
        return _result(json.dumps({
            "verdict": "verified", "reason": "nothing outstanding",
            "findings_path": "review-findings.md",
        }))

    sup.runner.run = _review_verifies  # type: ignore[method-assign]

    outcome = _bounded(sup._step_review(), timeout=10.0)

    assert outcome == ("done", "succeeded")
    status = sup.run.read_status()
    assert status.get("state") == "succeeded"
    assert status.get("verdict") == "verified"


# ---------------------------------------------------------------------------
# 3. A VERIFIED review whose findings channel still carries a residual
#    finding records "verified-with-residuals", state still "succeeded".
# ---------------------------------------------------------------------------

def test_a_residual_carrying_verified_review_records_verified_with_residuals(
        tmp_path):
    sup = _residuals_supervisor(tmp_path / "run")
    sup._approach = 1
    residual_only = [
        _residual_finding("residual", curable=False, clause="requirement Z",
                  evidence="a stale snapshot label, disclosed"),
    ]
    findings.write_findings_json(sup.run.findings_json_file, residual_only)

    async def _review_verifies(prompt, transcript, **kw):
        return _result(json.dumps({
            "verdict": "verified", "reason": "only residual findings remain",
            "findings_path": "review-findings.md",
        }))

    sup.runner.run = _review_verifies  # type: ignore[method-assign]

    outcome = _bounded(sup._step_review(), timeout=10.0)

    assert outcome == ("done", "succeeded")
    status = sup.run.read_status()
    assert status.get("state") == "succeeded"
    assert status.get("verdict") == "verified-with-residuals"


def test_a_mixed_blocking_and_residual_rejection_never_reaches_this_label(
        tmp_path):
    """Sanity: a genuinely rejected outcome never gets NEAR the new label
    at all -- `_review_success_verdict_label` is only ever consulted once a
    verdict is already VERIFIED (requirement D.3's own scope: a residual
    finding beside a blocking one still rejects)."""
    sup = _residuals_supervisor(tmp_path / "run")
    sup._approach = 1
    mixed = [_residual_finding("blocking"), _residual_finding("residual")]
    findings.write_findings_json(sup.run.findings_json_file, mixed)

    async def _review_rejects(prompt, transcript, **kw):
        return _result(json.dumps({
            "verdict": "rejected", "reason": "requirement D.3 unmet",
            "findings_path": "review-findings.md",
        }))

    sup.runner.run = _review_rejects  # type: ignore[method-assign]

    outcome = _bounded(sup._step_review(), timeout=10.0)

    assert outcome != ("done", "succeeded")
    status = sup.run.read_status()
    assert status.get("verdict") != "verified-with-residuals"
    assert status.get("state") != "succeeded"


def test_the_label_helper_reads_the_channel_independently_of_review_verdict(
        tmp_path):
    """`_review_success_verdict_label` re-reads the channel itself rather
    than depending on a value threaded through `_review_verdict`'s own
    return shape -- pinned directly, isolated from the async `_step_review`
    machinery above."""
    sup = _residuals_supervisor(tmp_path / "run")
    assert sup._review_success_verdict_label() == "verified"
    findings.write_findings_json(sup.run.findings_json_file,
                                 [_residual_finding("residual")])
    assert sup._review_success_verdict_label() == "verified-with-residuals"


# ---------------------------------------------------------------------------
# 4. CLI rendering: `ralphctl status`'s real `cmd_status` prints the new
#    value verbatim, never "unknown" -- registry rooted at a tmp dir
#    (`RALPHD_REGISTRY`), so no daemon/container is needed to exercise it.
# ---------------------------------------------------------------------------

def _seeded_run(cli_main, tmp_path, monkeypatch, run_id: str) -> RunDir:
    monkeypatch.setenv("RALPHD_REGISTRY", str(tmp_path / "registry"))
    root = cli_main.run_root(run_id)
    root.mkdir(parents=True)
    run = RunDir(root=root)
    run.update_status(state="succeeded", verdict="verified-with-residuals",
                      startedAt="2026-01-01T00:00:00Z",
                      endedAt="2026-01-01T00:05:00Z")
    return run


def test_cmd_status_renders_verified_with_residuals_verbatim_never_unknown(
        tmp_path, monkeypatch):
    from ralphd.cli import main as cli_main

    _seeded_run(cli_main, tmp_path, monkeypatch, "task012-cli-render")
    args = argparse.Namespace(run_id="task012-cli-render", json=False)
    printed: list[str] = []
    monkeypatch.setattr("builtins.print", lambda *a, **kw: printed.append(
        " ".join(str(x) for x in a)))

    cli_main.cmd_status(args)

    output = "\n".join(printed)
    assert "verdict:   verified-with-residuals" in output
    assert "unknown" not in output.lower()


def test_cmd_status_json_carries_the_new_verdict_verbatim(tmp_path, monkeypatch):
    from ralphd.cli import main as cli_main

    _seeded_run(cli_main, tmp_path, monkeypatch, "task012-cli-render-json")
    args = argparse.Namespace(run_id="task012-cli-render-json", json=True)
    printed: list[str] = []
    monkeypatch.setattr("builtins.print", lambda *a, **kw: printed.append(
        " ".join(str(x) for x in a)))

    cli_main.cmd_status(args)

    # `die(4, ...)` (the swallowed "no API endpoint recorded" fallback
    # notice) also goes through `print`, ahead of the JSON body -- find the
    # JSON object itself rather than assuming it is the only captured line.
    combined = "\n".join(printed)
    payload = json.loads(combined[combined.index("{"):])
    assert payload["verdict"] == "verified-with-residuals"


def test_cmd_status_lines_render_the_new_verdict_verbatim():
    """A narrower, dependency-free pin over the exact rendering line
    `cmd_status` builds for `verdict:` -- plus the sort/lifecycle
    vocabulary, which now knows the new value too (an unrecognised value
    would still sort after every known one rather than crash, but this
    value is now a KNOWN one, ranked strictly after "verified")."""
    from ralphd.cli import main as cli_main

    status = {"runId": "r1", "state": "succeeded",
              "verdict": "verified-with-residuals"}
    line = f"verdict:   {status.get('verdict')}"
    assert line == "verdict:   verified-with-residuals"
    assert "verified-with-residuals" in cli_main.RUN_VERDICT_ORDER
    assert (cli_main.RUN_VERDICT_ORDER.index("verified-with-residuals")
           > cli_main.RUN_VERDICT_ORDER.index("verified"))


# ---------------------------------------------------------------------------
# 5. Hub rendering: `pill()`/the summary row never fall back to "unknown"
#    for the new value, and the sort vocabulary knows it too.
# ---------------------------------------------------------------------------

def test_app_js_pill_never_special_cases_verdict_to_unknown():
    """`pill(text)` (cli/web/app.js) only substitutes "unknown" for a
    null/empty value -- never for a recognised-but-not-hardcoded string
    like "verified-with-residuals". Read the source rather than running a
    JS engine (no such runtime in this checkout) -- the same style
    `tests/test_peer_verdict_gate.py::_review_verdict_attributions`
    already uses for this same file."""
    app_js = (Path(__file__).resolve().parents[1] / "src" / "ralphd" /
             "cli" / "web" / "app.js").read_text()
    pill_fn = re.search(r"function pill\(text(?:, label)?\) \{.*?\n\}",
                        app_js, re.DOTALL)
    assert pill_fn, "pill() not found in app.js"
    body = pill_fn.group(0)
    # the ONLY places "unknown" appears inside pill() are the two
    # null-coalescing fallbacks (class name, text) for a missing/empty
    # value -- never a value-specific branch naming a real verdict string.
    assert body.count("unknown") == 2
    assert "verified-with-residuals" not in body
    # the lifecycle sort vocabulary was extended alongside the CLI's.
    assert ('const RUN_VERDICT_ORDER = ["", "unverified", "verified", '
           '"verified-with-residuals"];') in app_js


def test_style_css_gives_the_new_pill_a_real_colour_not_the_unknown_one():
    css = (Path(__file__).resolve().parents[1] / "src" / "ralphd" / "cli" /
          "web" / "style.css").read_text()
    assert ".pill-verified-with-residuals" in css
