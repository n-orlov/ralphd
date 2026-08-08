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
from test_e2e import EngineProc


@pytest.fixture
def engine_factory(tmp_path):
    procs: list[EngineProc] = []

    def make(job: dict | None = None, stub_env: dict | None = None) -> EngineProc:
        defaults = {"run_id": "hook-e2e", "iterations": 12,
                    "max_approaches": 1, "on_complete": "exit"}
        e = EngineProc(tmp_path, {**defaults, **(job or {})}, stub_env)
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
    assert e.proc.wait(timeout=60) == 0
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
    marker = tmp_path / "hook-ran-fail"
    cmd = f"touch {marker} && exit 7"
    e = engine_factory(job={"on_complete_cmd": cmd,
                            "max_approaches": 1, "iterations": 2},
                       stub_env={"STUB_TASKS": "5"})  # forces a failed job
    assert e.proc.wait(timeout=60) == 1
    status = json.loads((e.run_dir / "status.json").read_text())
    assert status["state"] == "failed"
    assert status["verdict"] == "unverified"

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


def test_no_hook_configured_runs_nothing(engine_factory):
    e = engine_factory()
    assert e.proc.wait(timeout=60) == 0
    events = [json.loads(ln) for ln in
             (e.run_dir / "events.jsonl").read_text().splitlines()]
    assert not any("on_complete_cmd" in (ev.get("message") or "") for ev in events)
