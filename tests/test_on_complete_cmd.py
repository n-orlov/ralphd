"""Black-box tests: `on_complete_cmd` completion hook (PRD req 26).

A shell command run by the engine (in-container) exactly once on reaching a
terminal state, receiving RALPHD_RUN_ID/RALPHD_STATE/RALPHD_VERDICT env vars.
Failures are logged, never affect the job's verdict or the engine's exit
code.
"""

from __future__ import annotations

import json
import time

import pytest
from tests.conftest import LiveRun
from tests.flows.flowlib import wait_engine_exit


@pytest.fixture
def engine_factory(tmp_path):
    procs: list[LiveRun] = []

    def make(job: dict | None = None, stub_env: dict | None = None) -> LiveRun:
        defaults = {"run_id": "hook-e2e", "iterations": 12,
                    "max_approaches": 1, "on_complete": "exit"}
        e = LiveRun(tmp_path, {**defaults, **(job or {})}, stub_env)
        procs.append(e)
        return e

    yield make
    for e in procs:
        e.stop()


def test_hook_runs_once_with_correct_env_on_success(engine_factory, tmp_path):
    marker = tmp_path / "hook-ran.json"
    cmd = (
        f'python3 -c "import json,os; '
        f"json.dump({{'run_id': os.environ.get('RALPHD_RUN_ID'), "
        f"'state': os.environ.get('RALPHD_STATE'), "
        f"'verdict': os.environ.get('RALPHD_VERDICT')}}, "
        f"open('{marker}', 'a')); "
        f"open('{marker}', 'a').write(chr(10))\""
    )
    e = engine_factory(job={"on_complete_cmd": cmd, "run_id": "hook-run-1"})
    assert wait_engine_exit(e, timeout=60) == 0
    status = json.loads((e.run_dir / "status.json").read_text())
    assert status["state"] == "succeeded"
    assert status["verdict"] == "verified"

    lines = marker.read_text().strip().splitlines()
    assert len(lines) == 1, f"hook ran {len(lines)} time(s), expected exactly 1"
    payload = json.loads(lines[0])
    assert payload == {"run_id": "hook-run-1", "state": "succeeded",
                       "verdict": "verified"}

    events = (e.run_dir / "events.jsonl").read_text().splitlines()
    parsed = [json.loads(ln) for ln in events]
    assert any("on_complete_cmd finished" in (ev.get("message") or "")
               for ev in parsed)


def test_hook_nonzero_exit_logged_but_state_verdict_and_exit_code_unaffected(
        engine_factory, tmp_path):
    # Task 011/cure-01-02 (v0.24 requirement B.3/C.1, #169): this node used
    # to force the job itself to fail (STUB_TASKS=5 with iterations=2), but
    # that plain out-of-budget ending now HOLDS (`exhausted`/verdict null)
    # rather than reaching a real terminal state -- and B.3/operator ruling
    # 3 forbid running `on_complete_cmd` at all on a hold (see
    # `test_hold_does_not_run_completion_hook` below). So this node now
    # drives an ordinary, genuinely-terminal `succeeded` job instead: the
    # subject it pins is purely "a nonzero-exit hook is logged but never
    # changes the job's own state/verdict/exit code", which is just as
    # true (and faster to prove) on a real terminal outcome as it was on
    # the old accidental-hold one.
    marker = tmp_path / "hook-ran-fail"
    cmd = f"touch {marker} && exit 7"
    e = engine_factory(job={"on_complete_cmd": cmd})
    assert wait_engine_exit(e, timeout=60) == 0
    status = json.loads((e.run_dir / "status.json").read_text())
    assert status["state"] == "succeeded"
    assert status["verdict"] == "verified"

    deadline = time.time() + 10
    while not marker.exists() and time.time() < deadline:
        time.sleep(0.1)
    assert marker.exists(), "hook never ran"

    events = [json.loads(ln) for ln in
             (e.run_dir / "events.jsonl").read_text().splitlines()]
    error_events = [ev for ev in events
                    if ev.get("level") == "error"
                    and "on_complete_cmd exited 7" in (ev.get("message") or "")]
    assert error_events, f"no on_complete_cmd failure event found: {events}"


def test_hold_does_not_run_completion_hook(engine_factory, tmp_path):
    """B.3/operator ruling 3 (v0.24, #169, cure-01-02): a real engine that
    enters `exhausted` (the hold) must skip `on_complete_cmd` entirely --
    running the operator's completion hook on a hold would falsely tell it
    the job is over -- and must exit through its ordinary shutdown cleanup
    (release the protected dirs, close the lock, return) even with
    `on_complete: idle`, never idle forever waiting for a signal that a
    genuinely-finished run would have been waiting for. Forces the hold via
    the same plain-budget-exhaustion shape `test_budget_exhaustion_fails_job`
    (tests/test_e2e.py) and the superseded node above used to use."""
    marker = tmp_path / "forbidden-wrapup"
    e = engine_factory(job={"on_complete_cmd": f"touch {marker}",
                            "on_complete": "idle",
                            "max_approaches": 1, "iterations": 2},
                       stub_env={"STUB_TASKS": "5"})
    status = e.wait_state(("exhausted",), timeout=30)
    assert status["state"] == "exhausted"
    assert status["verdict"] is None

    # `on_complete: idle` would ordinarily keep the process alive waiting on
    # a signal/`/shutdown` -- a hold must exit through the ordinary shutdown
    # funnel regardless, so the process must still end on its own.
    assert wait_engine_exit(e, timeout=15) == 1

    assert not marker.exists(), (
        "B.3 permits only frozen status/events/run-record on a hold, not "
        "the operator's completion hook")

    events = [json.loads(ln) for ln in
             (e.run_dir / "events.jsonl").read_text().splitlines()]
    assert not any("on_complete_cmd" in (ev.get("message") or "") for ev in events)


def test_no_hook_configured_runs_nothing(engine_factory):
    e = engine_factory()
    assert wait_engine_exit(e, timeout=60) == 0
    events = [json.loads(ln) for ln in
             (e.run_dir / "events.jsonl").read_text().splitlines()]
    assert not any("on_complete_cmd" in (ev.get("message") or "") for ev in events)
