"""Reading helpers shared by the stub-`pi` flow modules (task 005, G.5).

Every module under `tests/flows/` drives the REAL engine subprocess with the
stub `pi` binary on PATH (`engine_factory`, `tests/conftest.py`) and observes
one loop path strictly through the run's own recorded state: `events.jsonl`,
`status.json`, `tasks.json`, `petition-adjudications.json` and the workspace's
own files. Nothing here imports engine internals -- these flows are black-box
doubles of a host reading a finished run dir.

The two things this file exists to keep identical across the four flows:

1. **The step vocabulary** (`flow_steps`): `events.jsonl` reduced to one short
   string per event that marks a *step of the path* -- the phase entered, the
   iteration spawned, the verdict recorded, the signal raised, the escalation
   emitted, the adjudication recorded. A raw event list is unreadable in a
   failure message and drags in ids/timestamps/usage that are not the path; a
   reduced step list is what a reviewer can actually diff.
2. **A failure message that names the failing step** (`assert_step_sequence`,
   `assert_steps_include`): the audit's own risk note for merging 44 modules
   into ~12 flows is "loss of diff-shaped failure messages -- a merged flow
   that fails must print which step". So both helpers report the INDEX and the
   expected-vs-actual step at the first divergence, plus the whole observed
   sequence, and never a bare `assert a == b`.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

TERMINAL_STATES = ("succeeded", "failed", "aborted")

# --- reading the run's own record -----------------------------------------

def events(run) -> list[dict]:
    """Every event line of `events.jsonl`, in order (empty list when the run
    never wrote one)."""
    log = run.run_dir / "events.jsonl"
    if not log.exists():
        return []
    out = []
    for line in log.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:  # a partial trailing write mid-run
            continue
    return out


def events_of(run, type_: str) -> list[dict]:
    return [ev for ev in events(run) if ev.get("type") == type_]


def wait_terminal_recorded(run, timeout: float = 60, interval: float = 0.05) -> dict:
    """Bounded barrier (task 006, #169): block until `events.jsonl` itself
    carries the run's terminal `state` event, then return it.

    Every flow used to read the terminal state through `status.json`
    (`LiveRun.wait_terminal()`, which polls status.json) or a bare
    `proc.wait()`, and only then reach for `flow_steps()`/`events_of(run,
    'state')` to assert the step sequence -- trusting that by the time
    status.json read terminal, the matching event was already on disk too.
    That trust is now a documented engine invariant (SPEC.md's events
    table, task 006/#169: the event is appended strictly BEFORE the write
    that makes status.json terminal), so polling status.json first and
    events.jsonl second happens to work, but it is the wrong barrier to
    poll on directly in a *test* -- it duplicates the polling loop in every
    module that needs it, each with its own timeout and its own way of
    rendering a timeout failure. This is the one barrier: it polls
    `events.jsonl` directly for the terminal `state` event (never
    status.json), and raises with the events actually seen so far -- never a
    bare `TimeoutError()` -- when the deadline passes first.
    """
    deadline = time.monotonic() + timeout
    while True:
        for ev in events(run):
            if ev.get("type") == "state" and ev.get("state") in TERMINAL_STATES:
                return ev
        if time.monotonic() >= deadline:
            seen = [ev.get("type") for ev in events(run)]
            raise TimeoutError(
                f"wait_terminal_recorded: no terminal `state` event landed in "
                f"events.jsonl within {timeout}s; event types seen so far: "
                f"{seen!r}")
        time.sleep(interval)


def status(run) -> dict:
    f = run.run_dir / "status.json"
    if not f.exists():
        return {}
    try:
        return json.loads(f.read_text())
    except json.JSONDecodeError:  # mid-write
        return {}


def tasks(run) -> list[dict]:
    f = run.run_dir / "tasks.json"
    if not f.exists():
        return []
    return json.loads(f.read_text()).get("tasks", [])


def flow_steps(run) -> list[str]:
    """`events.jsonl` reduced to the path's own steps, in order.

    One string per step-marking event; every other event type (budget,
    token usage, task-status churn, notifications) is dropped -- those are
    asserted directly off `status.json`/`tasks.json` by whichever flow cares,
    and carrying them here would make every sequence assertion brittle
    against an unrelated new event type.
    """
    steps: list[str] = []
    for ev in events(run):
        t = ev.get("type")
        if t == "state":
            steps.append(f"state:{ev.get('state')}")
        elif t == "phase":
            step = f"phase:{ev.get('phase')}@{ev.get('approach')}"
            if ev.get("midRunAdjudication"):
                step += ":midrun"
            steps.append(step)
        elif t == "iteration.start":
            steps.append(f"iteration:{ev.get('phase')}")
        elif t == "approach.verdict_recorded":
            steps.append(
                f"verdict:{ev.get('channel')}:{ev.get('verdict')}@{ev.get('approach')}")
        elif t == "signal":
            steps.append(f"signal:{ev.get('signal')}")
        elif t == "operator_escalation":
            steps.append(f"escalation:{ev.get('reason')}@{ev.get('approach')}")
        elif t == "petition_adjudications_recorded":
            steps.append("adjudications_recorded")
        elif t == "approach.cure_pass":
            steps.append(
                f"cure_pass:{ev.get('curePasses')}@{ev.get('approach')}")
        # Task 014 (C.3): the steer path's own five step-marking event
        # types -- received/consumed (state.py's `RunDir.add_steering`/
        # `consume_steering`), the hat's own settlement claim
        # (`LoopSupervisor._step_generic_hat`'s `steer.settled`), and the
        # two tool-surface audit events (`hat_tools.EVENT_TOOL_CALL`/
        # `EVENT_CONTEXT_WRITE`) a `task-insert`/`standing-rules` call
        # emits. No other flow drives any of these, so adding them here
        # cannot perturb an existing flow's reduced sequence.
        elif t == "steering.received":
            steps.append(f"steering:received:{ev.get('file')}")
        elif t == "steering.consumed":
            steps.append(f"steering:consumed:{ev.get('file')}")
        elif t == "steer.settled":
            steps.append(f"steer:settled:{ev.get('settled')}")
        elif t == "steer.tool_call":
            steps.append(f"steer_tool:{ev.get('tool')}:{ev.get('ok')}")
        elif t == "steer.context_write":
            steps.append(f"steer_write:{ev.get('file')}")
        # Task 015 (C.4): the infra-retry wrapper's own three step-marking
        # event types (`loop.py`'s `_run_iteration_with_infra_retry` /
        # `_begin_infra_wait` / `_reset_infra_episode`) -- one attempt
        # (`infra_retry`), one backoff wait beginning (`infra_wait`), one
        # episode ending because an iteration finally reached the model
        # again (`infra_recovered`). No other flow drives a hung stub-`pi`
        # invocation, so adding these cannot perturb an existing flow's
        # reduced sequence.
        elif t == "infra_retry":
            steps.append(f"infra_retry:{ev.get('phase')}:{ev.get('attempt')}")
        elif t == "infra_wait":
            steps.append(f"infra_wait:{ev.get('phase')}:{ev.get('attempt')}")
        elif t == "infra_recovered":
            steps.append("infra_recovered")
    return steps


# --- sequence assertions that name the failing step ------------------------

def _rendered(actual: list[str]) -> str:
    return "\n".join(f"  [{i}] {s}" for i, s in enumerate(actual)) or "  (no steps)"


def assert_step_sequence(actual: list[str], expected: list[str], *,
                         flow: str) -> None:
    """`actual` IS `expected`, step for step -- and on any difference, say
    which step index diverged and what stood there instead."""
    for i, want in enumerate(expected):
        got = actual[i] if i < len(actual) else "<nothing: sequence ended>"
        assert got == want, (
            f"{flow}: flow step [{i}] should be {want!r} but was {got!r}\n"
            f"observed sequence:\n{_rendered(actual)}")
    assert len(actual) == len(expected), (
        f"{flow}: flow step [{len(expected)}] should not exist, but the run "
        f"recorded {actual[len(expected)]!r} after the expected sequence "
        f"ended\nobserved sequence:\n{_rendered(actual)}")


def assert_steps_include(actual: list[str], expected: list[str], *,
                         flow: str) -> None:
    """`expected` appears in `actual` IN ORDER (other steps may sit between)
    -- and on a miss, say which expected step was never reached and how far
    the match got."""
    i = 0
    for want in expected:
        while i < len(actual) and actual[i] != want:
            i += 1
        assert i < len(actual), (
            f"{flow}: flow step {want!r} never appears (in order) in the run's "
            f"own record; matched {expected[:expected.index(want)]!r} first\n"
            f"observed sequence:\n{_rendered(actual)}")
        i += 1
