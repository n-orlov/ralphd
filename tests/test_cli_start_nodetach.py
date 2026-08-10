"""Black-box test for `ralphctl start --no-detach` surviving the API dying
right at job completion (PRD req 28).

Uses the STUB_DOCKER_LIVE_ENGINE knob (tests/stub-docker/docker +
live_engine_supervisor.py): `docker run` actually launches a real
`ralphd-engine` wired to the mounted run/config dirs and mapped port, and a
supervisor SIGKILLs it the instant status.json reaches a terminal state —
deterministically reproducing "the connection resets right as the container
exits" instead of depending on a timing race.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest
from test_cli_docker import Ctl  # reuse the recording-stub-docker harness

REPO = Path(__file__).parent.parent
STUB_PI = REPO / "tests" / "stub-pi"


@pytest.fixture
def ctl(tmp_path):
    return Ctl(tmp_path)


def _live_env() -> dict:
    return {
        "STUB_DOCKER_LIVE_ENGINE": "1",
        "PATH": f"{STUB_PI}:{os.environ['PATH']}",
    }


def _wait_for_supervisor(run_dir: Path, timeout: float = 60) -> dict:
    marker = run_dir / ".stub-supervisor-done"
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if marker.exists():
            try:
                return json.loads(marker.read_text())
            except json.JSONDecodeError:
                pass
        time.sleep(0.05)
    raise TimeoutError(f"supervisor never finished (log: {run_dir / '.stub-live-engine.log'})")


def test_no_detach_survives_api_dying_at_container_exit(ctl):
    run_id = "nodetach-verified"
    res = ctl.run("start", "--prd", str(ctl.prd), "--run-id", run_id,
                  "--llm", "none", "--on-complete", "exit", "--no-detach",
                  "--iterations", "10",
                  env={**_live_env(), "STUB_TASKS": "1", "STUB_SLEEP": "0.2"})
    run_dir = ctl.registry / "runs" / run_id
    sup = _wait_for_supervisor(run_dir)
    # the point of this test: the supervisor really did SIGKILL the engine
    # right at terminal state (proving the fallback path, not a lucky race).
    assert sup["killed"] is True
    log_tail = (run_dir / ".stub-live-engine.log").read_text()[-4000:]
    assert "ConnectionResetError" not in res.stderr, res.stderr
    assert "Traceback" not in res.stderr, (res.stderr, log_tail)
    status = json.loads((run_dir / "status.json").read_text())
    assert status["verdict"] == "verified", status
    assert res.returncode == 0, (res.returncode, res.stdout, res.stderr)


def test_no_detach_exits_nonzero_when_stub_never_verifies(ctl):
    run_id = "nodetach-notverified"
    res = ctl.run("start", "--prd", str(ctl.prd), "--run-id", run_id,
                  "--llm", "none", "--on-complete", "exit", "--no-detach",
                  "--iterations", "2", "--max-approaches", "1",
                  # STUB_REVIEW_FAILS is set alongside STUB_VERIFY_FAILS (task
                  # 002): budget exhausts exactly as the sole task completes,
                  # which since task 002 now grants a single off-budget grace
                  # review (Reviewer role) rather than going straight to a
                  # terminal unverified state -- this test's "stub never
                  # verifies" premise must hold across that pathway too, or
                  # the always-satisfied stub Reviewer would flip this run to
                  # verified despite the deliberately-failing Verifier.
                  env={**_live_env(), "STUB_TASKS": "1", "STUB_VERIFY_FAILS": "99",
                       "STUB_REVIEW_FAILS": "99", "STUB_SLEEP": "0.6"})
    run_dir = ctl.registry / "runs" / run_id
    _wait_for_supervisor(run_dir)
    status = json.loads((run_dir / "status.json").read_text())
    assert status["verdict"] != "verified", status
    assert res.returncode == 1, (res.returncode, res.stdout, res.stderr)
