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
from pathlib import Path

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
