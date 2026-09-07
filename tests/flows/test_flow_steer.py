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

from tests.flows.flowlib import assert_step_sequence, flow_steps, status, tasks

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
