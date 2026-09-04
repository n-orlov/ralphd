"""Task 023 (requirement C.3, #141): the black-box cure-pass end-to-end --
a real `ralphd-engine` subprocess, a stub `pi` on PATH, the SAME `live`
fixture tier `tests/test_e2e_peer_gates_task029.py`/
`tests/test_ladder_exhaustion_e2e_task033.py` already use for the rest of
the engine's black-box surface. No engine internals are imported; every
assertion below reads the run's OWN recorded state -- `status.json`,
`tasks.json`, `events.jsonl` and the run dir's own filesystem shape -- never
only the stub's own input.

The unit-tier coverage for the cure pass itself
(`tests/test_cure_pass_task009.py`, `tests/test_bounded_cure_pass_task010.py`)
drives a real `LoopSupervisor` with a hand-scripted `sup.runner` standing in
for the model call; this module instead drives the real subprocess boundary
those tests never touch -- job.yaml parsing, the real `runner.py` spawn of a
`pi`-shaped process, and the real NDJSON parse of that process's own stdout
-- for exactly the scenario requirement C.3 names: a review that rejects
ONCE with all-curable blocking findings, then verifies.

New stub-`pi` knob added alongside this module (`tests/stub-pi/pi`), mirrors
`STUB_VERDICT_GATE_REJECT_COUNT`'s own shape:

  STUB_REVIEW_CURABLE_REJECT_COUNT   how many `review` invocations reject
                      with an all-curable BLOCKING findings channel
                      (`review-findings.md` + `review-findings.json`)
                      before the next one verifies (default 0).

This module skips cleanly (rather than erroring) when the stub `pi`
entrypoint this whole tier depends on is missing or not executable --
`pytest_configure` in `tests/conftest.py` already turns a missing
`ralphd-engine`/`ralphctl` console script into a session-wide collection
error for every OTHER module in this tier, but the stub `pi` entrypoint
itself (`tests/stub-pi/pi`, placed first on `PATH` by `tests.conftest.live`)
is a plain repo file, not a resolved console script, so it gets its own,
narrower check here.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

REPO = Path(__file__).parent.parent
STUB_PI_ENTRY = REPO / "tests" / "stub-pi" / "pi"

pytestmark = pytest.mark.skipif(
    not STUB_PI_ENTRY.exists() or not os.access(STUB_PI_ENTRY, os.X_OK),
    reason="stub pi entrypoint (tests/stub-pi/pi) is absent or not executable",
)


def _status(run) -> dict:
    f = run.run_dir / "status.json"
    if not f.exists():
        return {}
    try:
        return json.loads(f.read_text())
    except json.JSONDecodeError:  # mid-write
        return {}


def _phases(run) -> list[str]:
    iters = sorted((run.run_dir / "iterations").iterdir())
    metas = [json.loads((d / "meta.json").read_text()) for d in iters]
    return [m["phase"] for m in metas]


def _events(run, type_: str) -> list[dict]:
    log = run.run_dir / "events.jsonl"
    lines = log.read_text().splitlines() if log.exists() else []
    return [ev for ev in (json.loads(x) for x in lines) if ev.get("type") == type_]


def _tasks(run) -> list[dict]:
    return json.loads((run.run_dir / "tasks.json").read_text())["tasks"]


def test_cure_pass_appends_tasks_verifies_and_never_replans(live):
    run = live(run_id="c3-cure-pass-e2e",
               job={"on_complete": "exit"},
               stub_env={"STUB_REVIEW_CURABLE_REJECT_COUNT": "1"})
    assert run.proc.wait(timeout=60) == 0

    status = _status(run)
    # 1. The run ends verified -- never a rejection, never a replan-driven
    #    failure. `_review_success_verdict_label` names either "verified"
    #    or "verified-with-residuals"; both are the same real success edge.
    assert status.get("state") == "succeeded", status
    assert status.get("verdict") in ("verified", "verified-with-residuals"), status

    # 2. The cure pass was taken exactly once against approach 1, and the
    #    run never advanced past approach 1 -- the disk-persisted proof
    #    that `rejected_curable_pass_available` was the edge taken, not
    #    `rejected_approach_left`.
    assert status.get("approach") == 1, status
    assert status.get("curePasses") == {"1": 1}, status

    # 3. No replan ever happened: the FSM's own explicit `replan` state
    #    never ran, and no approach was ever ARCHIVED into `approaches/`
    #    (that archive only happens on the ordinary replan-or-fail path,
    #    never on a cure pass -- `_step_review`'s own docstring). The
    #    directory itself may exist (created unconditionally elsewhere,
    #    e.g. by the run-record build), but it must hold no archived
    #    approach files when no replan ever ran.
    phases = _phases(run)
    assert "replan" not in phases, phases
    approaches_dir = run.run_dir / "approaches"
    assert not any(approaches_dir.rglob("*")) if approaches_dir.exists() else True

    # 4. The cure task was appended to THIS SAME approach's tasks.json
    #    (never a fresh approach's file) and reached completed -- the one
    #    real consequence `_take_cure_pass` has on disk.
    tasks = _tasks(run)
    cure_tasks = [t for t in tasks if t.get("origin") == "review-cure"]
    assert cure_tasks, tasks
    assert all(t["status"] == "completed" for t in cure_tasks), cure_tasks
    assert all(t["id"].startswith("cure-01-") for t in cure_tasks), cure_tasks

    # 5. The engine's own `approach.cure_pass` event was emitted, naming
    #    the approach, the new count and the inserted task ids -- proof
    #    the cure-pass EDGE (not merely a coincidentally-shaped tasks.json)
    #    is what produced this run.
    cure_events = _events(run, "approach.cure_pass")
    assert len(cure_events) == 1, cure_events
    assert cure_events[0].get("approach") == 1, cure_events[0]
    assert cure_events[0].get("curePasses") == 1, cure_events[0]
    assert set(cure_events[0].get("taskIds") or []) == {t["id"] for t in cure_tasks}

    # 6. Every task -- the planner's originals AND the appended cure
    #    task(s) -- is completed; the plan is genuinely, fully resolved.
    assert tasks and all(t["status"] == "completed" for t in tasks), tasks
