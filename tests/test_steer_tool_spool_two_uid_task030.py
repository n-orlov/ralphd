"""Task 030 (requirement G.2, #130): the steer tool spool at the two-uid
subprocess tier.

The in-process tier -- `tests/test_steer_hat_tools_live.py` -- already pins
`SteerToolBroker`'s applier guarantees and (with `_ThreadedAgent`) a
same-process worker-thread stand-in for the hat's own subprocess. What that
tier structurally CANNOT show is the one property the whole spool exists
for (`hat_tools.py`'s own module docstring): the hat's process, a real
`fork`+`exec` child the engine drops to the agent uid in production
(`privsep.agent_child_kwargs()`), never writes `tasks.json` (root-owned
under the #60 boundary) itself -- it writes a *request* file and reads an
*answer* file, and the ENGINE performs every write, from ITS OWN process.

This module drives that for real: a real `ralphd-engine` subprocess (the
`engine_factory` fixture (tests/conftest.py) `tests/test_e2e.py` already
exercises the rest of
the black-box surface with) launches a real `Role: Steer` invocation of the
stub `pi` as ITS OWN subprocess, and the stub -- new knobs
`STUB_STEER_TOOL_CALLS`/`STUB_STEER_MALFORMED_REQUEST`, see `tests/stub-pi/
pi`'s own header -- issues each tool call through a THIRD real process: `pi
-m ralphd.engine.hat_tools <tool>` (the hat-side client's `main()`), exactly
mirroring what the generated `pi` extension's `callEngine()` spawns in
production. That process writes the request file and blocks reading the
response; only the engine's own `SteerToolBroker.serve()` (an asyncio task
inside the engine's process) ever answers it or touches `tasks.json`.

Both engine and hat-side client processes run under the same uid here (this
container's own test suite has no root to separate from -- `tests/
test_engine_uid_boundary.py`'s documented limit), so the property THIS
module tests is the PROCESS boundary and the spool protocol crossing it:
"the agent uid" and "the owner of tasks.json" name the SAME uid here,
exactly as they do in every fast-lane test in this suite. What this tier
adds over the in-process one is that the call gets there via TWO REAL OS
PROCESS BOUNDARIES (steer-hat subprocess -> hat-tools-client subprocess ->
spool files -> engine's own process) rather than a same-process broker
call or a worker thread.

The ownership half -- the hat's uid genuinely UNABLE to write `tasks.json`,
the engine performing the write as that file's root owner -- needs a real
container, and is its own module at the docker tier:
`tests/test_steer_tool_spool_two_uid_docker_task030.py`, which runs this
same spool inside the shipped `container/Dockerfile` (engine started as
root, `pi` dropped to uid 1000, `tasks.json` root-owned per task 105 / #60
requirement D) and asserts the EACCES the hat gets when it tries the write
itself. Read the two together: neither tier alone covers requirement G.2.
"""

from __future__ import annotations

import json



def _tasks(e) -> list[dict]:
    return json.loads((e.run_dir / "tasks.json").read_text())["tasks"]


def _responses(e) -> list[dict]:
    path = e.run_dir / ".stub-steer-tool-responses.json"
    assert path.exists(), "the steer hat's subprocess never wrote its answers"
    return json.loads(path.read_text())


def _tool_call_events(e) -> list[dict]:
    log = e.run_dir / "events.jsonl"
    events = [json.loads(line) for line in log.read_text().splitlines()]
    return [ev for ev in events if ev.get("type") == "steer.tool_call"]


def test_a_real_steer_iteration_applies_a_task_insert_through_the_spool(engine_factory):
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
    e = engine_factory(job={"on_complete": "exit", "iterations": 12},
                       stub_env={"STUB_TASKS": "1", "STUB_SLEEP": "1",
                                "STUB_STEER_TOOL_CALLS": json.dumps(calls)})
    e.wait_api()
    code, res = e.api("POST", "/steering",
                      {"message": "insert a task", "name": "spool"})
    assert code == 202 and res["file"] == "001-spool.md"
    assert e.proc.wait(timeout=90) == 0

    responses = _responses(e)
    assert len(responses) == 1
    assert responses[0]["ok"] is True, responses[0]
    assert responses[0]["tool"] == "task-insert"

    tasks = _tasks(e)
    inserted = next((t for t in tasks if t["id"] == "spooled-1"), None)
    assert inserted is not None, tasks
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
    tool_events = _tool_call_events(e)
    assert any(ev["tool"] == "task-insert" and ev["ok"] is True
              and "001-spool.md" in ev.get("steering", [])
              for ev in tool_events), tool_events

    # The request was consumed: nothing is left outstanding in the spool.
    assert list((e.run_dir / "hat-tools" / "requests").glob("*.json")) == []


def test_a_malformed_spool_request_is_refused_without_ending_the_run(engine_factory):
    """A hand-written, unparseable request file dropped straight into the
    spool (never through `hat_tools.call()`, which always writes valid
    JSON) must be answered `ok: false` -- `SteerToolBroker.serve_once()`'s
    `except (OSError, json.JSONDecodeError)` branch -- and must not crash
    the broker's asyncio task, hang the steer iteration, or fail the run:
    the run still has to reach its ordinary terminal, verified state."""
    e = engine_factory(job={"on_complete": "exit", "iterations": 12},
                       stub_env={"STUB_TASKS": "1", "STUB_SLEEP": "1",
                                "STUB_STEER_MALFORMED_REQUEST": "1"})
    e.wait_api()
    code, res = e.api("POST", "/steering",
                      {"message": "drop a bad request", "name": "bad"})
    assert code == 202 and res["file"] == "001-bad.md"
    assert e.proc.wait(timeout=90) == 0

    status = json.loads((e.run_dir / "status.json").read_text())
    assert status.get("state") == "succeeded", status
    assert status.get("verdict") == "verified", status

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


def test_a_task_update_status_move_still_goes_through_the_transition_validator(engine_factory):
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
    e = engine_factory(job={"on_complete": "exit", "iterations": 12},
                       stub_env={"STUB_TASKS": "1", "STUB_SLEEP": "1",
                                "STUB_STEER_TOOL_CALLS": json.dumps(calls)})
    e.wait_api()
    code, res = e.api("POST", "/steering",
                      {"message": "insert then illegally jump", "name": "jump"})
    assert code == 202 and res["file"] == "001-jump.md"
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
    tasks = _tasks(e)
    moved = next((t for t in tasks if t["id"] == "spooled-2"), None)
    assert moved is not None, tasks
    assert moved["status"] != "validated", moved
