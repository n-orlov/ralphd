"""Task 020 (PRD requirement D.5, #125 A): a peek past `verdict_gate`'s
blindness is DETECTED and RECORDED, never adjudicated.

`prompts/verdict_gate.md` STATES the blindness contract in words (task 020
of the previous wave, PRD requirement L, #101, `tests/
test_peer_verdict_gate.py`); nothing in the engine stops the agent
subprocess from ignoring the prompt and running `bash cat run/events.jsonl`
anyway -- the sandbox has no technical wall there, only the honor system.
This module pins the post-hoc check this task adds on top of that honor
system: `state.detect_verdict_gate_peek` scans ONE gate iteration's own
transcript (`iterations/NNNN/output.jsonl`) for a tool call whose rendered
arguments mention `iterations/`, `events.jsonl` or `output.jsonl` -- the
three paths a genuinely blind peer must never read -- and
`LoopSupervisor._record_verdict_gate_peek` turns a flagged call into one
`verdict_gate_peek` event plus one entry on the run's own
`verdictGatePeeks` status list (surfaced by `GET /status` for free, since
that endpoint is just `read_status()` plus a few `setdefault`s).

Part 1 unit-tests the detector directly against a hand-written transcript
file (clean, flagged-bash, flagged-read, multiple patterns in one call,
absent file). Part 2 drives the detector through the real dispatch path
(`LoopSupervisor._step_verdict_gate` and its off-budget sibling
`_grace_verdict_gate`, both reached through
`tests/test_peer_verdict_gate.py`'s own scripted-agent harness) and pins
the two properties requirement D.5 actually asks for: a flagged read is
recorded (event + status surface) and NEVER changes the gate's own
verdict -- an APPROVED reply with a planted peek still reaches terminal
success, exactly as an approved reply with a clean transcript does; a
clean gate iteration leaves `verdictGatePeeks` unset/empty.
"""

from __future__ import annotations

import asyncio
import json

from ralphd.engine import state
from ralphd.engine.state import RunDir, atomic_write_json

from test_peer_verdict_gate import (  # noqa: F401 (re-exported fixtures)
    TASK_1,
    _ok,
    _plan,
    _run_job,
    _seed_all_completed,
    _supervisor,
    _review_verifies,
    _worker_completes,
    _verdict_gate_approves,
    _verdict_gate_rejects,
)


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
    _write_transcript(root, 1, _tool_event("cat run/events.jsonl"))
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
        "args": {"path": "/run/ralphd/iterations/0003/output.jsonl"}})
    flagged = state.detect_verdict_gate_peek(root, 1)
    assert len(flagged) == 1
    assert flagged[0]["tool"] == "read"
    assert set(flagged[0]["matched"]) == {"iterations/", "output.jsonl"}


def test_a_clean_call_amid_flagged_ones_is_not_itself_flagged(tmp_path):
    root = tmp_path / "run"
    _write_transcript(
        root, 1,
        _tool_event("ls -la"),
        _tool_event("cat iterations/0001/output.jsonl"),
        _tool_event("git status"))
    flagged = state.detect_verdict_gate_peek(root, 1)
    assert len(flagged) == 1
    assert set(flagged[0]["matched"]) == {"iterations/", "output.jsonl"}


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
    _write_transcript(root, 1, _tool_event("cat run/events.jsonl"))
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
                        _tool_event("cat run/events.jsonl"))

    final_state = asyncio.run(sup._run_job_core())

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
                        _tool_event("cat iterations/0003/output.jsonl"))

    final_state = asyncio.run(sup._run_job_core())

    assert final_state == "failed", sup.run.read_status()
    status = sup.run.read_status()
    peeks = status.get("verdictGatePeeks")
    assert peeks and len(peeks) == 1
    assert set(peeks[0]["calls"][0]["matched"]) == {"iterations/", "output.jsonl"}


def test_the_off_budget_grace_gate_is_checked_too(tmp_path):
    """`_grace_verdict_gate` (the second call site) is covered by the same
    `_record_verdict_gate_peek` helper -- a peek there is recorded and the
    grace path's own approval still stands."""
    root = tmp_path / "run"
    _seed_all_completed(root)
    sup = _supervisor(
        root,
        {"review": [_review_verifies()],
         "verdict_gate": [_verdict_gate_approves]},
        peer_verdict_gate=True)
    sup._approach = 1
    _inject_after_phase(sup.runner, "verdict_gate",
                        _tool_event("cat run/events.jsonl"))

    assert asyncio.run(sup._maybe_grace_review(1)) is True
    status = sup.run.read_status()
    assert status.get("state") == "succeeded"
    peeks = status.get("verdictGatePeeks")
    assert peeks and len(peeks) == 1
