"""Flow 3 of the twelve (task 005/014, requirement C.3): **steer received
-> consumed -> settled**, including a `task-insert` and a `standing-rules`
write, over a real `ralphd-engine` subprocess with the stub `pi` binary on
PATH, `STUB_SLEEP=0`.

The path, end to end, with the engine doing every write:

1. the operator's `POST /steering` (`RunDir.add_steering`) lands a message
   in the run's own `steering/` directory and emits `steering.received` --
   this happens while the run's first (and only originally-planned) worker
   iteration is held open (`STUB_HOLD_FILE`/`STUB_HOLD_PHASE="worker"`), so
   the message is guaranteed to be PENDING, not merely delivered, before
   that worker iteration ever finishes;
2. that one task resolves the whole (one-task) plan, so `review` runs and
   verifies BEFORE the pending message is ever looked at -- `_step_worker`'s
   top-of-loop check (task 012, requirement I) only runs at the top of a
   fresh worker pass, never mid-verdict -- but a `verified` review with
   steering still pending takes `review -> worker` on
   `steering_pending_at_verified` (never ending the run) instead of the
   plain terminal edge, and the very next worker pass's top-of-loop check
   then routes `worker -> steer` at absolute top priority, above any
   pending/next task;
3. the steer hat's real subprocess issues two real tool calls through the
   cross-process spool (`STUB_STEER_TOOL_CALLS`, exactly as the generated
   `pi` extension's `callEngine()` would) -- one `task-insert` (landing a
   brand new task, `origin: "steer"`, `status: "pending"`, never what the
   call itself asked for) and one `standing-rules` write (replacing the
   run's pinned standing-rules block) -- then reports `settled: true`;
4. the engine marks the message consumed (`steering.consumed`) and records
   the hat's own settlement claim (`steer.settled`) as a harness-observable
   fact, separate from whether the inserted task will itself pass;
5. `worker` resumes, and picks up the freshly-inserted task like any other
   -- proving the insert reached the SAME scheduler every ordinary task
   goes through, not a side channel the harness treats differently -- and a
   second `review` verifies it, this time ending the run for good.

Asserted two ways, like every other flow module: the exact `events.jsonl`
step sequence (`flowlib.assert_step_sequence`, naming the diverging index
and step on any mismatch) and the observable record on disk -- the new
task's own fields in `tasks.json`, the run's `standing-rules.md` contents,
and the resulting `notes.md` -- the file a later iteration actually READS,
because the harness splices the pinned block back into it after every
iteration (`state.reinject_standing_rules`, task 107), so the steer's
replacement must have DISPLACED the planner's original block there while
leaving the agent's own prose in that file untouched.
"""

from __future__ import annotations

import json

from tests.flows.flowlib import (assert_step_sequence, events_of, flow_steps,
                                 status, tasks, wait_step_recorded)

INSERTED_TASK = {"id": "steer-inserted", "title": "landed by the steer hat",
                 "successCriteria": "criteria via steer",
                 "status": "completed", "origin": "attacker"}
STANDING_RULES_TEXT = "## Standing rules\n\n- steered rule (task 014)\n"
# The planner's own `standing_rules` contract field (stub `pi`) -- the block
# the steer hat's write must DISPLACE in notes.md, not merely sit beside.
PLANNED_RULES_LINE = "- stub-supplied convention"
# notes.md's own prose, written by the stub planner: every line outside the
# pinned block must survive the harness's splice verbatim.
PLANNED_NOTES_PROSE = "planned by stub"
# `state.STANDING_RULES_END_MARKER`, spelled out rather than imported: these
# flows read a finished run dir like any other host would, never internals.
END_MARKER = "<!-- /standing-rules: harness-pinned, task 107 -->"

STEER_TOOL_CALLS = [
    {"tool": "task-insert", "args": {"task": dict(INSERTED_TASK)}},
    {"tool": "standing-rules", "args": {"text": STANDING_RULES_TEXT}},
]

STEER_PATH_STEPS = [
    "state:running",
    "phase:planning@1",
    "iteration:planning",
    "iteration:worker",
    "steering:received:001-steer-flow.md",
    "signal:COMPLETE",
    "phase:review@1",
    "iteration:review",
    "verdict:review:verified@1",
    "phase:steer@1",
    "iteration:steer",
    "steer_tool:task-insert:True",
    "steer_write:standing-rules.md",
    "steer_tool:standing-rules:True",
    "steering:consumed:001-steer-flow.md",
    "steer:settled:True",
    "iteration:worker",
    "signal:COMPLETE",
    "phase:review@1",
    "iteration:review",
    "verdict:review:verified@1",
    "signal:VERIFIED",
    "state:succeeded",
]


def test_a_steer_message_is_received_consumed_and_settled_with_a_task_insert_and_a_standing_rules_write(
        engine_factory, tmp_path):
    hold_file = tmp_path / "hold-steer-flow"
    run = engine_factory(
        job={"run_id": "flow-steer", "on_complete": "exit",
             "max_approaches": 1, "iterations": 30},
        stub_env={"STUB_TASKS": "1",
                 "STUB_HOLD_FILE": str(hold_file),
                 "STUB_HOLD_PHASE": "worker",
                 "STUB_HOLD_TIMEOUT_S": "30",
                 "STUB_STEER_TOOL_CALLS": json.dumps(STEER_TOOL_CALLS)})
    run.wait_api()

    # Task 063 (#179): wait for the worker iteration's own `iteration.start`
    # event to actually land in events.jsonl before POSTing, instead of
    # POSTing straight after `wait_api()` -- the latter only proves the API
    # is reachable, not that the engine has dispatched the worker iteration
    # this flow's asserted sequence expects `steering:received` to follow.
    # Racing that (see wait_step_recorded's own docstring for the two
    # observed hosted-runner failures) could land `steering.received`
    # BEFORE `iteration.start`, reversing steps [3]/[4] below. This barrier
    # is bounded and returns the moment the worker phase has engaged --
    # never a fixed sleep, and the expected sequence itself is unchanged.
    wait_step_recorded(run, "iteration:worker", timeout=30)

    # Land the message while the first worker iteration is held open, so
    # `pending_steering()` is guaranteed non-empty the instant that
    # iteration finishes -- never a race against how fast a one-task run
    # would otherwise reach `review`.
    code, res = run.api("POST", "/steering",
                        {"message": "insert a task and update the standing "
                                    "rules", "name": "steer-flow"})
    assert code == 202 and res["file"] == "001-steer-flow.md", (
        f"flow steer: POST /steering did not land the expected file, got "
        f"{code} {res}")

    hold_file.touch()  # release: let the held worker iteration finish task 1
    assert run.proc.wait(timeout=60) == 0, (
        "flow steer: the engine did not exit 0")

    assert_step_sequence(flow_steps(run), STEER_PATH_STEPS, flow="flow steer")

    all_tasks = tasks(run)
    inserted = next((t for t in all_tasks if t["id"] == "steer-inserted"), None)
    assert inserted is not None, (
        f"flow steer: the steer hat's task-insert never reached tasks.json, "
        f"got {[t.get('id') for t in all_tasks]}")
    # The applier's own forced guarantees, not the tool call's own request --
    # proof the ENGINE performed this write (task 011, requirement I, #98).
    assert inserted["origin"] == "steer", inserted
    assert inserted["status"] in ("pending", "completed"), inserted
    assert inserted["title"] == INSERTED_TASK["title"], inserted

    rules_file = run.run_dir / "standing-rules.md"
    assert rules_file.exists(), (
        "flow steer: the standing-rules write never landed standing-rules.md")
    assert rules_file.read_text() == STANDING_RULES_TEXT, (
        f"flow steer: standing-rules.md must be the steer hat's exact "
        f"replacement text, got {rules_file.read_text()!r}")

    # The resulting notes state: notes.md is where the pinned block is
    # actually read from by every later iteration, so a steered
    # standing-rules write that never reached it would be inert.
    notes_file = run.run_dir / "notes.md"
    assert notes_file.exists(), (
        "flow steer: the run never wrote notes.md at all")
    notes_text = notes_file.read_text()
    assert STANDING_RULES_TEXT.rstrip("\n") + "\n" + END_MARKER in notes_text, (
        f"flow steer: notes.md must carry the steer hat's own standing-rules "
        f"block, re-injected verbatim and closed by the harness marker, got "
        f"{notes_text!r}")
    assert PLANNED_RULES_LINE not in notes_text, (
        f"flow steer: the planner's original standing rule "
        f"{PLANNED_RULES_LINE!r} must be GONE from notes.md -- the steer's "
        f"write replaces the pinned block, it does not append to it; got "
        f"{notes_text!r}")
    assert PLANNED_NOTES_PROSE in notes_text, (
        f"flow steer: notes.md's own prose outside the pinned block must "
        f"survive the harness splice, got {notes_text!r}")

    st = status(run)
    assert st.get("state") == "succeeded", (
        f"flow steer: the run must end succeeded, got {st.get('state')!r}")
    settled = st.get("steerSettled") or {}
    assert settled.get("settled") is True, (
        f"flow steer: status.json's own steerSettled snapshot must record "
        f"the hat's settled:true claim, got {settled!r}")


# ---------------------------------------------------------------------------
# Task 021 (requirement C.10): five nodes folded in from two deleted
# real-engine modules (each with its own § "Subsumed" row in the same
# commit), both of them this flow's own subject -- the steer path -- observed
# at two ends the flow above does not reach:
#
#   * `tests/test_status_unconsumed_steering.py` (2 nodes): steering that is
#     NEVER consumed because the run goes terminal first, and how `ralphctl
#     status` says so (plus the negative case: a fully-consumed run must not
#     print the warning at all);
#   * `tests/test_steer_tool_spool_two_uid_task030.py` (3 nodes): the same
#     `task-insert` the flow above drives in-band, but asserted at the SPOOL
#     level -- the hat's own `pi -m ralphd.engine.hat_tools` child writes a
#     request file and the ENGINE performs every `tasks.json` write, so the
#     applier's forced `origin: "steer"` (never the `origin: "attacker"` the
#     call asked for) is the proof of which process wrote it -- plus a
#     malformed spool request refused without ending the run, and an illegal
#     status jump refused by the real transition validator.
#
# Moved verbatim: same fixtures (`live` where the real `ralphctl` executable
# must address the run by id, `engine_factory` for the spool nodes), same
# STUB_HOLD determinism, same assertions; only the module and its local
# readers changed (`flowlib.tasks`/`flowlib.events_of` replace the deleted
# modules' own `_tasks`/`_tool_call_events` copies). The docker-tier
# ownership half of the spool (`tests/
# test_steer_tool_spool_two_uid_docker_task030.py`) is untouched and still
# names the uid boundary this tier structurally cannot show.
# ---------------------------------------------------------------------------


def test_ralphctl_status_warns_about_unconsumed_steering(live, tmp_path):
    # task 010 (B.4): STUB_HOLD_FILE/STUB_HOLD_PHASE="worker" holds the
    # first worker iteration open instead of racing a real STUB_SLEEP
    # window to steer+abort while the job is still running -- the abort
    # below interrupts the held pi process directly, so the hold never
    # needs an explicit release.
    hold_file = tmp_path / "hold-unconsumed"
    run = live(run_id="unconsumed-steer", job={"iterations": 12},
               stub_env={"STUB_TASKS": "10", "STUB_HOLD_FILE": str(hold_file),
                        "STUB_HOLD_PHASE": "worker", "STUB_HOLD_TIMEOUT_S": "30"})
    run.wait_api()
    # Task 058 (K.7): wait for the worker iteration to actually be dispatched
    # before steering -- wait_api() only proves the HTTP server is up. This
    # node asserts no step order, but posting this early races the same
    # engine-startup window the primary flow node's fix (task 063, #179)
    # names; synchronising here too keeps the whole module off that race.
    wait_step_recorded(run, "iteration:worker", timeout=30)

    steer = run.ralphctl("steer", run.run_id, "never gets acted on",
                         "--name", "stranded")
    assert steer.returncode == 0, (steer.stdout, steer.stderr)

    abort = run.ralphctl("abort", run.run_id, "--reason", "test abort mid-steering")
    assert abort.returncode == 0, (abort.stdout, abort.stderr)

    run.wait_terminal(timeout=30)

    jres = run.ralphctl("--json", "status", run.run_id)
    assert jres.returncode == 0, (jres.stdout, jres.stderr)
    st = json.loads(jres.stdout)
    assert st["unconsumedSteering"] == ["001-stranded.md"]

    human = run.ralphctl("status", run.run_id)
    assert human.returncode == 0, (human.stdout, human.stderr)
    assert "UNCONSUMED STEERING" in human.stdout
    assert "001-stranded.md" in human.stdout


def test_ralphctl_status_no_warning_when_steering_fully_consumed(live):
    """Negative case: a run that ends with no pending steering (the common
    case) must not print the warning line at all."""
    run = live(run_id="no-unconsumed-steer", job={"iterations": 6},
               stub_env={"STUB_TASKS": "1", "STUB_SLEEP": "0"})
    run.wait_terminal(timeout=60)

    jres = run.ralphctl("--json", "status", run.run_id)
    st = json.loads(jres.stdout)
    assert st.get("unconsumedSteering") == []

    human = run.ralphctl("status", run.run_id)
    assert "UNCONSUMED STEERING" not in human.stdout


def _responses(e) -> list[dict]:
    path = e.run_dir / ".stub-steer-tool-responses.json"
    assert path.exists(), "the steer hat's subprocess never wrote its answers"
    return json.loads(path.read_text())


def test_a_real_steer_iteration_applies_a_task_insert_through_the_spool(
        engine_factory, tmp_path):
    """The hat's subprocess asks for `status: "completed"` and `origin:
    "attacker"` on the new task -- both illegal for a steer-hat insert
    (`state.STEER_HAT_TOOLS["task-insert"]` always lands `origin=steer`/
    `status=pending`, task 010, requirement H, #98). Getting back the
    APPLIER's guarantee rather than the raw payload the client sent is the
    proof that the write happened on the ENGINE's side of the spool -- a
    hat that could write `tasks.json` directly would just have whatever it
    asked for, unmodified."""
    calls = [{"tool": "task-insert",
             "args": {"task": {"id": "spooled-1", "title": "via the spool",
                               "status": "completed", "origin": "attacker"}}}]
    # task 009 (B.3): STUB_HOLD_FILE/STUB_HOLD_PHASE="worker" holds the
    # first worker iteration (after it has flipped the task to in-progress,
    # `tests/stub-pi/pi`'s own `_apply_hold("worker")` position) instead of
    # racing a fixed STUB_SLEEP window against how fast the single-task run
    # reaches a terminal state -- the steering POST below is guaranteed to
    # land before the hold is released, deterministically, not probably.
    hold_file = tmp_path / "hold-spool-insert"
    e = engine_factory(job={"on_complete": "exit", "iterations": 12},
                       stub_env={"STUB_TASKS": "1", "STUB_HOLD_FILE": str(hold_file),
                                "STUB_HOLD_PHASE": "worker", "STUB_HOLD_TIMEOUT_S": "30",
                                "STUB_STEER_TOOL_CALLS": json.dumps(calls)})
    e.wait_api()
    # Task 058 (K.7): same barrier as the primary flow node, applied here too.
    wait_step_recorded(e, "iteration:worker", timeout=30)
    code, res = e.api("POST", "/steering",
                      {"message": "insert a task", "name": "spool"})
    assert code == 202 and res["file"] == "001-spool.md"
    hold_file.touch()  # release: let the held worker iteration proceed
    assert e.proc.wait(timeout=90) == 0

    responses = _responses(e)
    assert len(responses) == 1
    assert responses[0]["ok"] is True, responses[0]
    assert responses[0]["tool"] == "task-insert"

    all_tasks = tasks(e)
    inserted = next((t for t in all_tasks if t["id"] == "spooled-1"), None)
    assert inserted is not None, all_tasks
    # The applier's own guarantee, not the hat-side client's request payload:
    # `origin` is always stamped `steer` and can never be requested away --
    # this is what proves the ENGINE performed the write, through
    # `STEER_HAT_TOOLS["task-insert"]`, and the hat's own process never
    # touched tasks.json at all. `status` is NOT asserted here to `pending`
    # (the applier's other forced field): by the time the run reaches its
    # own terminal state, the ordinary worker lifecycle has already picked
    # this newly-inserted task up and completed it like any other, which is
    # the correct, expected outcome -- not evidence against the applier.
    assert inserted["origin"] == "steer", inserted

    # And the engine's own event log -- written only by the engine's process
    # -- recorded exactly this call as served, naming the steering entry
    # that caused it.
    tool_events = events_of(e, "steer.tool_call")
    assert any(ev["tool"] == "task-insert" and ev["ok"] is True
              and "001-spool.md" in ev.get("steering", [])
              for ev in tool_events), tool_events

    # The request was consumed: nothing is left outstanding in the spool.
    assert list((e.run_dir / "hat-tools" / "requests").glob("*.json")) == []


def test_a_malformed_spool_request_is_refused_without_ending_the_run(
        engine_factory, tmp_path):
    """A hand-written, unparseable request file dropped straight into the
    spool (never through `hat_tools.call()`, which always writes valid
    JSON) must be answered `ok: false` -- `SteerToolBroker.serve_once()`'s
    `except (OSError, json.JSONDecodeError)` branch -- and must not crash
    the broker's asyncio task, hang the steer iteration, or fail the run:
    the run still has to reach its ordinary terminal, verified state."""
    # task 009 (B.3): see the hold rationale on the sibling test above.
    hold_file = tmp_path / "hold-spool-malformed"
    e = engine_factory(job={"on_complete": "exit", "iterations": 12},
                       stub_env={"STUB_TASKS": "1", "STUB_HOLD_FILE": str(hold_file),
                                "STUB_HOLD_PHASE": "worker", "STUB_HOLD_TIMEOUT_S": "30",
                                "STUB_STEER_MALFORMED_REQUEST": "1"})
    e.wait_api()
    # Task 058 (K.7): same barrier as the primary flow node, applied here too.
    wait_step_recorded(e, "iteration:worker", timeout=30)
    code, res = e.api("POST", "/steering",
                      {"message": "drop a bad request", "name": "bad"})
    assert code == 202 and res["file"] == "001-bad.md"
    hold_file.touch()  # release: let the held worker iteration proceed
    assert e.proc.wait(timeout=90) == 0

    st = status(e)
    assert st.get("state") == "succeeded", st
    assert st.get("verdict") == "verified", st

    answer_path = e.run_dir / "hat-tools" / "responses" / "stub-malformed.json"
    assert answer_path.exists(), "the malformed request was never answered"
    answer = json.loads(answer_path.read_text())
    assert answer["ok"] is False, answer
    assert "unreadable" in answer["error"], answer

    # And, still, the steering that arrived alongside it was actually
    # settled -- the malformed request is a refusal of ONE call, never of
    # the whole steer iteration.
    consumed = json.loads((e.run_dir / "steering" / ".consumed.json").read_text())
    assert "001-bad.md" in consumed, consumed

    # No malformed leftovers: the broker unlinks every request it served,
    # readable or not.
    assert list((e.run_dir / "hat-tools" / "requests").glob("*.json")) == []


def test_a_task_update_status_move_still_goes_through_the_transition_validator(
        engine_factory, tmp_path):
    """A second call, in the SAME spooled batch: an illegal status jump
    (`pending` -> `validated` is not a legal transition table entry for a
    non-vigilant run) must come back refused with the applier's own
    diagnostic, never silently applied -- proving the router between the
    spool and `apply_task_status_change` is the real one, not a stub-local
    substitute."""
    calls = [
        {"tool": "task-insert",
         "args": {"task": {"id": "spooled-2", "title": "second task"}}},
        {"tool": "task-update",
         "args": {"taskId": "spooled-2", "changes": {"status": "validated"}}},
    ]
    # task 009 (B.3): see the hold rationale on the first spool node above.
    hold_file = tmp_path / "hold-spool-jump"
    e = engine_factory(job={"on_complete": "exit", "iterations": 12},
                       stub_env={"STUB_TASKS": "1", "STUB_HOLD_FILE": str(hold_file),
                                "STUB_HOLD_PHASE": "worker", "STUB_HOLD_TIMEOUT_S": "30",
                                "STUB_STEER_TOOL_CALLS": json.dumps(calls)})
    e.wait_api()
    # Task 058 (K.7): same barrier as the primary flow node, applied here too.
    wait_step_recorded(e, "iteration:worker", timeout=30)
    code, res = e.api("POST", "/steering",
                      {"message": "insert then illegally jump", "name": "jump"})
    assert code == 202 and res["file"] == "001-jump.md"
    hold_file.touch()  # release: let the held worker iteration proceed
    assert e.proc.wait(timeout=90) == 0

    responses = _responses(e)
    assert len(responses) == 2
    assert responses[0]["ok"] is True, responses[0]
    assert responses[1]["ok"] is False, responses[1]
    assert "status change refused" in responses[1]["error"], responses[1]

    # The illegal jump was refused AT THE TIME OF THE CALL (the response
    # assertions above), which is the property under test; the task's
    # FINAL status by the time the run reaches its own terminal state is
    # not asserted here, because the ordinary worker lifecycle picks up a
    # pending task exactly like any other and completes it through the
    # normal, LEGAL `pending -> completed` transition afterwards -- that is
    # the correct outcome, not evidence the illegal jump was ever applied.
    moved = next((t for t in tasks(e) if t["id"] == "spooled-2"), None)
    assert moved is not None, tasks(e)
    assert moved["status"] != "validated", moved
