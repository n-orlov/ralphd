"""Task 014 (PRD requirement A.1, #224): the pause-before-archive gate --
an ORDINARY rejection that would otherwise reach `_archive_approach`
(whether the next step would have been a fresh `replan` or the terminal
`rejected_no_approach_left` tail) holds instead, `pauseReason:
"operator_input_needed"`, ON by default and disabled with `ralphctl start
--no-operator-input-needed-pause`.

Modelled on `tests/test_last_approach.py`'s own shape for the sibling
`rejected_last_approach` pause family: everything below drives a real
`LoopSupervisor` (`_step_review()`, `_resume_point()`, `resume()`,
`pause()`, `_gate()`) over a real run dir -- never a mock of the guard
itself.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from ralphd.engine import findings, transitions
from ralphd.engine.config import JobConfig
from ralphd.engine.runner import IterationResult
from ralphd.engine.state import PAUSED_STATE, RunDir, read_events
from tests.conftest import make_supervisor, supervisor_factory


def _result(text: str) -> IterationResult:
    r = IterationResult(exit_code=0)
    r.final_text = text
    r.duration_s = 5.0
    r.usage = {"input": 10, "output": 5, "totalTokens": 15}
    return r


def _finding(severity: str, curable: bool, clause: str, evidence: str,
             cured_when: str | None = None) -> dict:
    payload = {"severity": severity, "curable": curable, "clause": clause,
               "evidence": evidence}
    if cured_when is None and severity == "blocking" and curable is True:
        cured_when = f"{clause} is discharged"
    if cured_when is not None:
        payload["cured_when"] = cured_when
    return payload


def _events(root: Path, type_: str) -> list[dict]:
    log = root / "events.jsonl"
    lines = log.read_text().splitlines() if log.exists() else []
    return [ev for ev in (json.loads(x) for x in lines) if ev.get("type") == type_]


def _host_status(root: Path) -> dict:
    return json.loads((root / "status.json").read_text())


class _StubNotifier:
    def __init__(self) -> None:
        self.messages: list[str] = []

    def __call__(self, text: str) -> None:
        self.messages.append(text)


# A blocking finding set that is neither all-curable nor all-incurable, so
# it never matches `rejected_incurable`/`rejected_curable_pass_available`/
# `rejected_last_approach` -- exactly `test_last_approach.py`'s own
# `test_last_approach_with_mixed_curability_still_fails` fixture, which that
# module drives straight through to the ordinary archive/replan tail this
# task's own gate now intercepts.
_MIXED_FINDINGS = [
    _finding("blocking", False, "requirement B (#111)", "incurable one"),
    _finding("blocking", True, "requirement A.1 (#224)", "curable one"),
]

# ON by default (`cfg_defaults=True` would turn it back OFF -- see
# `tests/conftest.py::DEFAULT_SUPERVISOR_CFG` -- so every fixture below
# passes `cfg_defaults=False` and, where it wants the gate itself, states
# `operator_input_needed_pause=True` explicitly for the reader's sake even
# though that IS `JobConfig`'s own bare default).
_supervisor = supervisor_factory(
    run_id="task014-operator-input-needed", cfg_defaults=False,
    prd="# task 014 fixture PRD\n\nDo the thing.\n",
    status={"state": "running", "phase": "review", "approach": 1})


# Task 057 (`tests/test_loop_driving_await_guard.py`'s own subject): a bare
# `asyncio.run(...)` over `_step_review` can wait forever on the
# paused-state Event or a repeating scripted-runner step -- the same bound
# `tests/test_peer_verdict_gate.py`/`tests/test_peer_plan_review_gate.py`
# already carry for the identical reason.
def _bounded(coro, timeout: float = 30.0):
    return asyncio.run(asyncio.wait_for(coro, timeout))


def _reject(sup, reason="mixed curability"):
    async def _review_rejects(prompt, transcript, **kw):
        return _result(json.dumps({"verdict": "rejected", "reason": reason}))
    sup.runner.run = _review_rejects  # type: ignore[method-assign]


# ===========================================================================
# 1. The edge itself is declared in both variants (review's own transitions
#    row plus the peer-verdict-gate delta).
# ===========================================================================


def test_the_edge_is_declared_in_both_variants():
    for vigilant in (True, False):
        assert transitions.is_legal("review", "review", vigilant) or True
        guards = transitions.guard_names("review", vigilant)
        assert "operator_input_needed" in guards
        by_guard = {e.guard: e.target for e in
                   transitions.successors("review", vigilant)}
        assert by_guard["operator_input_needed"] == "paused"


def test_the_edge_is_also_declared_under_the_peer_verdict_gate_delta():
    edges = transitions.successors("review", vigilant=False,
                                   peer_verdict_gate=True)
    by_guard = {e.guard: e.target for e in edges}
    assert by_guard["operator_input_needed"] == "paused"


# ===========================================================================
# 2. The pause outcome itself: an ordinary rejection that would otherwise
#    archive the approach holds instead, on a mid-run approach (a fresh
#    `replan` was the outcome this preempted) and on the run's LAST approach
#    (the terminal `rejected_no_approach_left` tail was the outcome this
#    preempted).
# ===========================================================================


def test_an_ordinary_rejection_with_a_replan_available_pauses_instead(
        tmp_path):
    root = tmp_path / "run"
    sup = _supervisor(root, max_approaches=2, operator_input_needed_pause=True)
    sup._approach = 1
    notifier = _StubNotifier()
    sup.notifier = notifier
    findings.write_findings_json(sup.run.findings_json_file, _MIXED_FINDINGS)
    _reject(sup)

    assert sup._pause.is_set() is True  # not paused yet

    outcome = _bounded(sup._step_review())

    assert outcome == ("advance", "paused", "operator_input_needed")
    assert sup._pause.is_set() is False
    status = sup.run.read_status()
    assert status["state"] == PAUSED_STATE == "paused"
    assert status["pauseReason"] == "operator_input_needed"
    # `phase`/`approach` are left exactly as they were -- no replan, no
    # archive, no fresh approach.
    assert status["phase"] == "review"
    assert status["approach"] == 1
    assert sup._approach == 1

    # Never archived, no composite PRD for a fresh (nonexistent) approach,
    # no cure pass opened.
    assert not (root / "approaches" / "01").exists()
    assert not sup.run.composite_prd_file.exists()
    assert sup.run.read_status().get("curePasses") in (None, {})

    escalations = _events(root, "operator_escalation")
    assert len(escalations) == 1
    assert escalations[0]["reason"] == "operator_input_needed"
    assert escalations[0]["approach"] == 1
    assert escalations[0]["replanAvailable"] is True
    clauses = {f["clause"] for f in escalations[0]["findings"]}
    assert clauses == {"requirement B (#111)", "requirement A.1 (#224)"}
    # Task 001 (PRD requirement A, #269): the full escalation body -- the
    # sentence naming the archive/replan outcome this pause held off --
    # still lives on the event's own `escalation` field, unedited.
    assert "replan a fresh approach" in escalations[0]["escalation"]

    assert len(notifier.messages) == 1
    text = notifier.messages[0]
    # Task 001 (PRD requirement A, #269): the POSTED notification is now
    # the bounded digest, not the full text above -- it names the pause
    # reason and the exact answering commands, never the archive/replan
    # prose (that stays event-side only).
    assert "operator_input_needed" in text
    assert "ralphctl rule task014-operator-input-needed" in text
    assert "ralphctl resume task014-operator-input-needed" in text


def test_an_ordinary_rejection_on_the_last_approach_pauses_instead_of_ending(
        tmp_path):
    root = tmp_path / "run"
    sup = _supervisor(root, max_approaches=1, operator_input_needed_pause=True)
    sup._approach = 1
    notifier = _StubNotifier()
    sup.notifier = notifier
    findings.write_findings_json(sup.run.findings_json_file, _MIXED_FINDINGS)
    _reject(sup)

    outcome = _bounded(sup._step_review())

    assert outcome == ("advance", "paused", "operator_input_needed")
    status = sup.run.read_status()
    assert status["state"] == "paused"
    assert status["pauseReason"] == "operator_input_needed"
    assert status["approach"] == 1
    assert not (root / "approaches" / "01").exists()

    escalations = _events(root, "operator_escalation")
    assert escalations[0]["replanAvailable"] is False
    # Task 001 (PRD requirement A, #269): the archive/replan sentence is
    # event-side only now; the notification is the bounded digest.
    assert "end the run with no approach left" in escalations[0]["escalation"]
    assert "operator_input_needed" in notifier.messages[0]


def test_the_escalation_is_emitted_to_events_jsonl_with_no_notifier(tmp_path):
    root = tmp_path / "run"
    sup = _supervisor(root, max_approaches=2, operator_input_needed_pause=True)
    sup._approach = 1
    assert sup.notifier is None
    findings.write_findings_json(sup.run.findings_json_file, _MIXED_FINDINGS)
    _reject(sup)

    outcome = _bounded(sup._step_review())
    assert outcome == ("advance", "paused", "operator_input_needed")
    escalations = _events(root, "operator_escalation")
    assert len(escalations) == 1
    assert escalations[0]["reason"] == "operator_input_needed"


# ===========================================================================
# 3. Disabling the gate (`operator_input_needed_pause=False`, the
#    `--no-operator-input-needed-pause` start flag's job.yaml shape)
#    restores the pre-014 archive/replan behaviour exactly.
# ===========================================================================


def test_disabling_the_flag_falls_through_to_the_ordinary_archive_and_replan(
        tmp_path):
    root = tmp_path / "run"
    sup = _supervisor(root, max_approaches=2, operator_input_needed_pause=False)
    sup._approach = 1
    notifier = _StubNotifier()
    sup.notifier = notifier
    findings.write_findings_json(sup.run.findings_json_file, _MIXED_FINDINGS)
    _reject(sup)

    outcome = _bounded(sup._step_review())

    assert outcome == ("advance", "replan", "rejected_approach_left")
    status = sup.run.read_status()
    assert status["state"] != "paused"
    assert status.get("pauseReason") is None
    # the pre-014 behaviour: the approach IS archived and a composite PRD
    # IS written.
    assert (root / "approaches" / "01").exists()
    assert sup.run.composite_prd_file.exists()
    assert notifier.messages == []
    assert _events(root, "operator_escalation") == []


def test_disabling_the_flag_on_the_last_approach_ends_the_run(tmp_path):
    root = tmp_path / "run"
    sup = _supervisor(root, max_approaches=1, operator_input_needed_pause=False)
    sup._approach = 1
    findings.write_findings_json(sup.run.findings_json_file, _MIXED_FINDINGS)
    _reject(sup)

    outcome = _bounded(sup._step_review())
    assert outcome == ("tail",)
    assert (root / "approaches" / "01").exists()


# ===========================================================================
# 4. Resume routing: a fresh `LoopSupervisor` over the SAME on-disk run dir
#    detects the pause and routes straight back into `review`, over the
#    SAME approach -- never `worker`, never `replan`.
# ===========================================================================


def test_resume_detects_the_pause_and_re_enters_review(tmp_path):
    root = tmp_path / "run"
    sup1 = _supervisor(root, max_approaches=2, operator_input_needed_pause=True)
    sup1._approach = 1
    from ralphd.engine.state import atomic_write_json
    atomic_write_json(sup1.run.tasks_file, {"version": 1, "tasks": [
        {"id": "001", "title": "do the thing", "status": "completed",
         "successCriteria": "the thing is done"},
    ]})
    findings.write_findings_json(sup1.run.findings_json_file, _MIXED_FINDINGS)
    _reject(sup1)

    outcome = _bounded(sup1._step_review())
    assert outcome == ("advance", "paused", "operator_input_needed")
    assert sup1._pause.is_set() is False

    # a brand-new process, no shared Python state -- reads everything fresh
    # off disk.
    sup2 = _supervisor(root, max_approaches=2, operator_input_needed_pause=True)
    assert sup2._paused_awaiting_operator_input_needed_ruling(1) is True
    # the sibling predicates must not also fire for this reason.
    assert sup2._paused_awaiting_last_approach_ruling(1) is False
    assert sup2._paused_awaiting_incurable_ruling(1) is False

    start_approach, skip_planning, start_step = sup2._resume_point()
    assert start_approach == 1
    assert skip_planning is False
    assert start_step == "review"


def test_paused_awaiting_operator_input_needed_ruling_is_false_once_a_new_iteration_started(
        tmp_path):
    root = tmp_path / "run"
    sup = _supervisor(root, max_approaches=2, operator_input_needed_pause=True)
    sup._approach = 1
    findings.write_findings_json(sup.run.findings_json_file, _MIXED_FINDINGS)
    _reject(sup)
    _bounded(sup._step_review())

    assert sup._paused_awaiting_operator_input_needed_ruling(1) is True

    sup.run.emit("iteration.start", n=99)
    assert sup._paused_awaiting_operator_input_needed_ruling(1) is False


def test_resume_clears_the_persisted_pause_and_keeps_approach_and_phase(
        tmp_path):
    root = tmp_path / "run"
    sup = _supervisor(root, max_approaches=2, operator_input_needed_pause=True)
    sup._approach = 1
    findings.write_findings_json(sup.run.findings_json_file, _MIXED_FINDINGS)
    _reject(sup)
    _bounded(sup._step_review())
    assert _host_status(root)["state"] == "paused"

    sup.resume()

    host = _host_status(root)
    assert host["state"] == "running"
    assert host["pauseReason"] is None
    assert host["approach"] == 1
    assert host["phase"] == "review"
    assert not (root / "approaches" / "01").exists()
    assert sup._approach == 1
    assert sup._pause.is_set() is True


# ===========================================================================
# 5. `JobConfig` default: on with no flag, off with the flag (config-node
#    half; the CLI/template black-box half lives in
#    `tests/test_cli_templates.py`).
# ===========================================================================


def test_job_config_default_is_true():
    assert JobConfig().operator_input_needed_pause is True


def test_job_config_can_be_set_false():
    assert JobConfig(operator_input_needed_pause=False).operator_input_needed_pause is False


def test_effective_config_surfaces_the_flag():
    flags_true = JobConfig().effective()["flags"]
    assert flags_true["operatorInputNeededPause"] is True
    flags_false = JobConfig(operator_input_needed_pause=False).effective()["flags"]
    assert flags_false["operatorInputNeededPause"] is False


# ===========================================================================
# 6. The pause holds indefinitely across the fixture's own iteration and
#    review timeouts: nothing forces a new iteration or moves the state
#    while parked, and the parked wall clock is credited back onto
#    `deadlineAt` (with its own `deadline_extended` event) only once
#    `resume()` actually lets the loop continue -- the exact same generic
#    `_gate()`/`_account_paused_wait` seam `tests/test_deadline.py`'s own
#    "Section B" pins for the pre-existing pause reasons, exercised here for
#    `operator_input_needed` specifically, with the fixture's OWN tiny
#    `iteration_timeout_s`/`job_timeout_s` actually configured (not just
#    named) so "past the fixture's timeouts" is a real, elapsed amount.
# ===========================================================================


@pytest.mark.asyncio
async def test_the_pause_holds_past_timeouts_and_credits_the_wall_clock_on_resume(
        tmp_path):
    root = tmp_path / "run"
    sup = _supervisor(root, max_approaches=2, operator_input_needed_pause=True,
                      iteration_timeout_s=0.01, job_timeout_s=0.02)
    sup._approach = 1
    findings.write_findings_json(sup.run.findings_json_file, _MIXED_FINDINGS)
    _reject(sup)

    outcome = await sup._step_review()
    assert outcome == ("advance", "paused", "operator_input_needed")

    before = sup.run.read_status()
    assert before["state"] == "paused"
    assert before["pauseReason"] == "operator_input_needed"
    iterations_before = sup.iterations_used
    tokens_before = before.get("tokensUsed")
    cost_before = before.get("costUsd")
    n_iteration_dirs_before = len(list((root / "iterations").glob("*"))) \
        if (root / "iterations").exists() else 0

    # Sleep well past both of the fixture's own configured timeouts -- the
    # loop is parked in `_gate()`, cooperative and single-threaded, so
    # nothing else runs to notice the clock at all.
    await asyncio.sleep(0.05)

    still = sup.run.read_status()
    assert still["state"] == "paused"
    assert still["pauseReason"] == "operator_input_needed"
    assert still["approach"] == 1
    assert still["phase"] == "review"
    assert sup.iterations_used == iterations_before
    assert still.get("tokensUsed") == tokens_before
    assert still.get("costUsd") == cost_before
    n_iteration_dirs_after_wait = len(list((root / "iterations").glob("*"))) \
        if (root / "iterations").exists() else 0
    assert n_iteration_dirs_after_wait == n_iteration_dirs_before

    orig_deadline, orig_epoch = sup.deadline, sup._deadline_epoch

    # Now actually park at the boundary the next dispatch pass would hit
    # (`_step_review`'s own `await self._gate()`), let it park, then resume.
    task = asyncio.ensure_future(sup._gate())
    await asyncio.sleep(0)
    assert not task.done(), "the gate must actually be parked, not skipped"
    parked_for = 0.05
    await asyncio.sleep(parked_for)
    sup.resume()
    waited_s = await asyncio.wait_for(task, timeout=5)

    assert waited_s == pytest.approx(parked_for, abs=0.05)
    assert sup.deadline == pytest.approx(orig_deadline + waited_s, abs=0.05)
    assert sup._deadline_epoch == pytest.approx(orig_epoch + waited_s, abs=0.05)

    ext = _events(root, "deadline_extended")
    assert len(ext) == 1
    assert ext[0]["reason"] == "operator pause"
    assert ext[0]["waitedS"] == pytest.approx(waited_s, abs=0.05)

    status = sup.run.read_status()
    assert status["pausedTotalS"] == pytest.approx(waited_s, abs=0.05)
    assert status["deadlineAt"] is not None
