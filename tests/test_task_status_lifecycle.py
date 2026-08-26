"""Task 019 (requirement F, #60 pillar 3): task status becomes a mini-FSM
the HARNESS advances at transitions, not a report card an agent writes.

`awaiting-validation` (the vigilant worker's claim state) and `validated`
(the vigilant verifier-granted terminal) land beside the pre-existing
vocabulary; `completed` stays the non-vigilant terminal unchanged. Every
reader named in the PRD's fact 8 moves with them -- most for free, because
none of them hardcodes a status enumeration (they all key off
`engine.state`'s vocabulary/mapping constants), which this module also
proves directly rather than trusting the design note.

Two acceptance tests own the harder half of the requirement:

1. A status transition the declared task-status table forbids, proposed in
   a hat's output, is a contract violation -- re-prompted, logged, and
   eventually scored an agent-fault, through the EXACT SAME retry loop
   (`contract_enforcement.enforce_contract`) task 010 built for a
   malformed/absent/wrong-shape payload. Not a second mechanism: a second
   REASON the same one can refuse an attempt.
2. A long-running hat is driven for real through `LoopSupervisor`, and the
   task record is sampled from a second thread throughout -- the status on
   disk must not move until after the hat's own subprocess has already
   returned, matching the PRD's operator ruling ("task state changes only
   at step transitions ... in-step there is no task-state change at all").
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from pathlib import Path

import pytest

import ralphd.cli.main as cli_main
from ralphd.engine import contract_enforcement as ce
from ralphd.engine import state
from ralphd.engine import transitions as tr
from ralphd.engine.config import JobConfig
from ralphd.engine.loop import LoopSupervisor
from ralphd.engine.runner import IterationResult
from ralphd.engine.state import RunDir, atomic_write_json


# ---------------------------------------------------------------------------
# 1. the declared task-status table -- enumerable successors, both variants
# ---------------------------------------------------------------------------

def test_every_task_status_has_enumerable_successors_both_variants():
    for status in tr.TASK_STATUSES:
        assert isinstance(tr.legal_next_task_statuses(status, True), tuple)
        assert isinstance(tr.legal_next_task_statuses(status, False), tuple)


def test_the_two_variants_differ_only_by_the_declared_delta():
    non_vigilant = tr.task_status_table_for(False)
    vigilant = tr.task_status_table_for(True)
    differing = {k for k in non_vigilant if non_vigilant[k] != vigilant.get(k)}
    differing |= {k for k in vigilant if k not in non_vigilant}
    assert differing == set(tr.TASK_STATUS_VIGILANT_DELTA)


def test_vigilant_chain_pending_to_validated():
    assert tr.is_legal_task_status_transition("pending", "in-progress", True)
    assert tr.is_legal_task_status_transition("in-progress", "awaiting-validation", True)
    assert tr.is_legal_task_status_transition("awaiting-validation", "validated", True)
    # awaiting-validation replaces completed as the vigilant claim state --
    # a vigilant worker can never claim `completed` directly.
    assert not tr.is_legal_task_status_transition("in-progress", "completed", True)


def test_non_vigilant_chain_pending_to_completed():
    assert tr.is_legal_task_status_transition("pending", "in-progress", False)
    assert tr.is_legal_task_status_transition("in-progress", "completed", False)
    # the vigilant-only statuses are unreachable without the delta.
    assert not tr.is_legal_task_status_transition("in-progress", "awaiting-validation", False)
    assert not tr.is_legal_task_status_transition("awaiting-validation", "validated", False)


def test_verify_rejection_loops_back_for_a_retry():
    assert tr.is_legal_task_status_transition("awaiting-validation", "validation-failed", True)
    assert tr.is_legal_task_status_transition("validation-failed", "in-progress", True)


def test_exhausted_validation_budget_can_force_failed_from_awaiting_validation():
    # `_verify_task`'s own sequencing (engine/loop.py): validation-failed is
    # set in memory first, then overwritten to `failed` before the single
    # disk write lands once VALIDATION_ATTEMPT_LIMIT is reached -- so the
    # transition actually OBSERVED on disk is awaiting-validation -> failed
    # directly, and the table must accept it.
    assert tr.is_legal_task_status_transition("awaiting-validation", "failed", True)


def test_a_no_op_report_is_always_legal():
    for status in tr.TASK_STATUSES:
        assert tr.is_legal_task_status_transition(status, status, True)
        assert tr.is_legal_task_status_transition(status, status, False)


def test_terminal_statuses_have_no_successors_either_variant():
    for status in ("completed", "failed", "skipped", "validated"):
        assert tr.legal_next_task_statuses(status, True) == ()
        assert tr.legal_next_task_statuses(status, False) == ()


def test_blocked_is_deliberately_not_a_status_in_this_table():
    # task 021's harness-derived label: never a transition TARGET, only a
    # read-time annotation over a dependency's own status.
    assert "blocked" not in tr.TASK_STATUSES
    assert "blocked" not in tr.TASK_STATUS_TABLE
    assert "blocked" not in tr.TASK_STATUS_VIGILANT_DELTA


# ---------------------------------------------------------------------------
# 2. every reader in fact 8 moved with the new vocabulary
# ---------------------------------------------------------------------------

def test_task_completion_statuses_include_validated_but_not_awaiting_validation():
    assert "validated" in state.TASK_COMPLETION_STATUSES
    assert "completed" in state.TASK_COMPLETION_STATUSES
    assert "awaiting-validation" not in state.TASK_COMPLETION_STATUSES


def test_task_actionable_statuses_excludes_both_new_statuses():
    # neither is something a worker iteration picks up -- awaiting-validation
    # is waiting on verify, validated is done.
    assert "awaiting-validation" not in state.TASK_ACTIONABLE_STATUSES
    assert "validated" not in state.TASK_ACTIONABLE_STATUSES


def test_task_counts_camel_cases_awaiting_validation_and_passes_validated_through():
    tasks = [
        {"id": "1", "status": "awaiting-validation"},
        {"id": "2", "status": "validated"},
        {"id": "3", "status": "pending"},
    ]
    counts = state.task_counts(tasks)
    assert counts["awaitingValidation"] == 1
    assert counts["validated"] == 1
    assert counts["pending"] == 1
    assert counts["total"] == 3


def test_task_status_labels_render_awaiting_validation_and_validated():
    assert state.TASK_STATUS_LABELS["awaitingValidation"] == "awaiting-validation"
    counts = {"total": 2, "completed": 0, "awaitingValidation": 1, "validated": 1}
    rendered = state.format_task_counts(counts)
    assert "1 awaiting-validation" in rendered
    # Task 018 (#77/#78): `validated` is one of `TASK_COMPLETION_STATUSES`,
    # so it is folded into the leading `x/2 completed` numerator rather than
    # said again in the parenthesised remainder -- printing both would make
    # the parenthesised counts stop adding up to `total`.
    assert rendered == "1/2 completed (1 awaiting-validation)"
    assert "1 validated" not in rendered


def test_format_task_status_renders_the_two_new_statuses_bare():
    assert state.format_task_status({"status": "awaiting-validation"}) == "awaiting-validation"
    assert state.format_task_status({"status": "validated"}) == "validated"


def test_ralphctl_doctor_recognizes_both_new_statuses_in_tasks_json(tmp_path):
    rdir = tmp_path / "run"
    rdir.mkdir()
    atomic_write_json(rdir / "tasks.json", {"version": 1, "tasks": [
        {"id": "1", "title": "a", "status": "awaiting-validation"},
        {"id": "2", "title": "b", "status": "validated"},
    ]})
    issues = cli_main._diagnose_tasks_json(rdir)
    assert not any("unrecognized status" in i for i in issues), issues


def test_ralphctl_cli_status_tuple_matches_the_declared_engine_table():
    """`_TASK_STATUSES` (cli/main.py, host-side, deliberately not importing
    the engine package -- see the comment beside `PROMPT_NAMES`) is a
    hand-kept-in-sync duplicate of `transitions.TASK_STATUSES`; this is the
    drift check that keeps the two matching."""
    assert set(cli_main._TASK_STATUSES) == set(tr.TASK_STATUSES)


def test_ralphctl_tasks_status_column_renders_the_new_statuses():
    tasks = [{"id": "1", "title": "x", "status": "awaiting-validation"},
             {"id": "2", "title": "y", "status": "validated"}]
    width = cli_main._task_status_width(tasks)
    assert cli_main._task_status_cell(tasks[0], width).strip() == "awaiting-validation"
    assert cli_main._task_status_cell(tasks[1], width).strip() == "validated"


def test_hub_app_js_renders_task_status_generically_not_via_a_hardcoded_enum():
    """The hub's task pill/dialog (`app.js`) formats whatever `t.status`
    says (`taskStatusText`) and slugifies it into a CSS class (`pill()`) --
    neither hardcodes a status enumeration, so the two new statuses render
    for free. This is the code-derived check for that design claim."""
    import ralphd.cli.ui_server as ui_mod
    app_js = (Path(ui_mod.__file__).parent / "web" / "app.js").read_text()
    block = app_js.split("function taskStatusText(")[1].split("\n}")[0]
    assert "t.status" in block
    # not a status-name literal switch/if-ladder
    assert "awaiting-validation" not in app_js
    assert "validated" not in app_js
    pill_block = app_js.split("function pill(")[1].split("\n}")[0] if "function pill(" in app_js else app_js
    assert "toLowerCase" in app_js  # the generic slugifier exists at all


# ---------------------------------------------------------------------------
# 3. an illegal task-status move proposed in a hat's output is a contract
#    violation, handled through task 010's own retry loop
# ---------------------------------------------------------------------------

def test_illegal_task_status_move_is_reprompted_then_scored_agent_fault():
    events: list[dict] = []

    class _Sink:
        def emit(self, type_, **data):
            events.append({"type": type_, **data})
            return events[-1]

    # A hat that keeps proposing an illegal move (awaiting-validation ->
    # completed does not exist in the vigilant table -- validated is the
    # only vigilant terminal) every attempt.
    attempts_seen = []

    def respond(attempt, prior_errors):
        attempts_seen.append((attempt, prior_errors))
        return json.dumps({"task_id": "001", "task_status": "completed"})

    def extra_validate(payload):
        return tr.task_status_report_errors(payload, "awaiting-validation", True)

    # register a throwaway hat schema so validate_payload has something to
    # check the shape against (task_id/task_status only).
    from ralphd.engine import transitions as tr_mod
    HAT = "task019_status_hat_fixture"
    tr_mod.HAT_SCHEMAS[HAT] = {
        "type": "object", "required": ["task_id", "task_status"],
        "properties": {"task_id": {"type": "string"},
                       "task_status": {"type": "string"}},
        "additionalProperties": False,
    }
    try:
        result = ce.enforce_contract(
            HAT, respond, max_retries=2, state=_Sink(),
            extra_validate=extra_validate)
    finally:
        tr_mod.HAT_SCHEMAS.pop(HAT, None)

    assert result.ok is False
    assert result.agent_fault is True
    assert result.attempts == 3  # first attempt + 2 retries
    assert len(attempts_seen) == 3
    # the SECOND and THIRD calls carried the previous attempt's specific
    # error back, exactly as any other contract violation would.
    assert attempts_seen[1][1] and "task_status" in attempts_seen[1][1][0]
    violation_events = [e for e in events if e["type"] == ce.EVENT_VIOLATION]
    fault_events = [e for e in events if e["type"] == ce.EVENT_AGENT_FAULT]
    assert len(violation_events) == 3
    assert len(fault_events) == 1
    assert "task_status" in violation_events[0]["errors"][0]


def test_a_legal_task_status_move_is_accepted_by_the_same_loop():
    def respond(attempt, prior_errors):
        return json.dumps({"task_id": "001", "task_status": "validated"})

    def extra_validate(payload):
        return tr.task_status_report_errors(payload, "awaiting-validation", True)

    from ralphd.engine import transitions as tr_mod
    HAT = "task019_status_hat_fixture_ok"
    tr_mod.HAT_SCHEMAS[HAT] = {
        "type": "object", "required": ["task_id", "task_status"],
        "properties": {"task_id": {"type": "string"},
                       "task_status": {"type": "string"}},
        "additionalProperties": False,
    }
    try:
        result = ce.enforce_contract(
            HAT, respond, max_retries=2, extra_validate=extra_validate)
    finally:
        tr_mod.HAT_SCHEMAS.pop(HAT, None)
    assert result.ok is True
    assert result.payload == {"task_id": "001", "task_status": "validated"}
    assert result.attempts == 1


# ---------------------------------------------------------------------------
# 4. no task-status change while a hat executes -- the harness applies it
#    strictly AFTER the hat's own subprocess has returned
# ---------------------------------------------------------------------------

STATUS_STATE = "task019_status_live_state"
STATUS_HAT = "task019_status_live_hat"


@pytest.fixture
def registered_status_hat(monkeypatch, tmp_path):
    prompts_dir = tmp_path / "config" / "prompts"
    prompts_dir.mkdir(parents=True)
    (prompts_dir / f"{STATUS_HAT}.md").write_text(
        "You are a task019 status-reporting fixture hat. Reply with "
        "{\"task_id\": \"001\", \"task_status\": \"awaiting-validation\"} "
        "as the last line of your final message.\n")
    from ralphd.engine import config as config_mod
    monkeypatch.setattr(config_mod, "CONFIG_DIR", tmp_path / "config")

    assert STATUS_STATE not in tr.TABLE
    tr.TABLE[STATUS_STATE] = (tr.T("planning", "task019_status_reported"),)
    tr.STATE_HAT[STATUS_STATE] = STATUS_HAT
    tr.HAT_SCHEMAS[STATUS_HAT] = {
        "type": "object", "required": ["task_id", "task_status"],
        "properties": {"task_id": {"type": "string"},
                       "task_status": {"type": "string"}},
        "additionalProperties": False,
    }
    try:
        yield
    finally:
        tr.TABLE.pop(STATUS_STATE, None)
        tr.STATE_HAT.pop(STATUS_STATE, None)
        tr.HAT_SCHEMAS.pop(STATUS_HAT, None)


def test_no_status_change_while_a_long_running_hat_executes(tmp_path, registered_status_hat):
    root = tmp_path / "run"
    root.mkdir(parents=True)
    run = RunDir(root=root)
    run.update_status(state="running")
    atomic_write_json(run.tasks_file, {"version": 1, "tasks": [
        {"id": "001", "title": "do the thing", "status": "in-progress",
         "successCriteria": "the thing is done"},
    ]})
    cfg = JobConfig(run_id="task019-status-live", iterations=5,
                    max_approaches=1, vigilant=True, on_complete="exit",
                    infra_retry_backoff_s=[0.0], infra_retry_backoff_max_s=0.0,
                    infra_outage_budget_s=1000.0)
    sup = LoopSupervisor(cfg, run, root)

    handler_started = threading.Event()
    handler_finished = threading.Event()
    SLEEP_S = 0.35

    class _SlowAgent:
        def interrupt(self):
            return False

        async def run(self, prompt, transcript, **kw):
            handler_started.set()
            time.sleep(SLEEP_S)
            r = IterationResult(exit_code=0)
            r.final_text = ('reporting\n' +
                            json.dumps({"task_id": "001",
                                        "task_status": "awaiting-validation"}))
            r.duration_s = SLEEP_S
            r.usage = {"input": 1, "output": 1, "totalTokens": 2}
            handler_finished.set()
            return r

    sup.runner = _SlowAgent()  # type: ignore[assignment]

    samples: list[str] = []

    def sample_loop():
        if not handler_started.wait(2.0):
            return
        while not handler_finished.is_set():
            doc = run.read_tasks()
            samples.append(doc["tasks"][0]["status"])
            time.sleep(0.01)

    sampler = threading.Thread(target=sample_loop)
    sampler.start()

    edges = (tr.T("planning", "task019_status_reported"),)
    outcome = asyncio.run(sup._step_generic_hat(STATUS_STATE, edges))
    sampler.join(2.0)

    assert samples, "sampler never observed the hat in flight -- test did not overlap"
    assert all(s == "in-progress" for s in samples), samples
    assert outcome == ("advance", "planning", "task019_status_reported")
    assert run.read_tasks()["tasks"][0]["status"] == "awaiting-validation"
    events = state.read_events(run.root)
    task_events = [e for e in events if e.get("type") == "task"]
    assert any(e.get("taskId") == "001" and e.get("newStatus") == "awaiting-validation"
              for e in task_events)


def test_an_illegal_reported_status_ends_the_run_as_a_contract_violation(
        tmp_path, registered_status_hat):
    root = tmp_path / "run"
    root.mkdir(parents=True)
    run = RunDir(root=root)
    run.update_status(state="running")
    # this task is already `validated` -- vigilant terminal, no successor at
    # all, so the fixture hat's `awaiting-validation` proposal is illegal.
    atomic_write_json(run.tasks_file, {"version": 1, "tasks": [
        {"id": "001", "title": "do the thing", "status": "validated",
         "successCriteria": "the thing is done"},
    ]})
    cfg = JobConfig(run_id="task019-status-illegal", iterations=5,
                    max_approaches=1, vigilant=True, on_complete="exit",
                    infra_retry_backoff_s=[0.0], infra_retry_backoff_max_s=0.0,
                    infra_outage_budget_s=1000.0)
    sup = LoopSupervisor(cfg, run, root)

    class _FixedAgent:
        def interrupt(self):
            return False

        async def run(self, prompt, transcript, **kw):
            r = IterationResult(exit_code=0)
            r.final_text = json.dumps({"task_id": "001",
                                      "task_status": "awaiting-validation"})
            r.duration_s = 0.01
            r.usage = {"input": 1, "output": 1, "totalTokens": 2}
            return r

    sup.runner = _FixedAgent()  # type: ignore[assignment]
    edges = (tr.T("planning", "task019_status_reported"),)
    outcome = asyncio.run(sup._step_generic_hat(STATUS_STATE, edges))
    assert outcome == ("tail",)
    assert sup._abort_reason and "contract violation" in sup._abort_reason
    # the task record itself is untouched -- an illegal move is refused,
    # never applied "just in case".
    assert run.read_tasks()["tasks"][0]["status"] == "validated"
