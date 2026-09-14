"""Task 054 (K.3, #188/#189/#193/#179): prove `scripts/run_bounded.sh`
actually distinguishes a `timeout-minutes`-shaped kill from a human cancel /
`cancel-in-progress` cancellation, on the two surfaces a judging hat reads
(the step's own log text and `$GITHUB_STEP_SUMMARY`) -- see that script's
own header comment for the full mechanism and why the two cases produce
different exit statuses from GNU `timeout(1)`.

Both tests run the real script as a subprocess against a real `sleep`; no
live GitHub Actions dispatch is involved or needed -- proving the negative
half (a genuine job-level `timeout-minutes` kill never fires early) would
need a real wait past a real timeout-minutes bound, which is exactly what
this mechanism exists to make unnecessary to prove per-run.
"""

from __future__ import annotations

import os
import signal
import subprocess
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "run_bounded.sh"

MARKER = "RALPHD_TIMEOUT_KILL:"


def _run(args: list[str], **env_overrides: str) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env.update(env_overrides)
    return subprocess.run(
        [str(SCRIPT), *args],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        env=env,
        timeout=20,
    )


@pytest.mark.timeout(30)
def test_timeout_kill_marker_is_present_when_the_inner_bound_fires(tmp_path):
    """The command overruns the script's own inner bound: this is the
    shape of a job that would otherwise die as an ambiguous job-level
    `timeout-minutes` `cancelled` -- the script kills it first and reports
    a `failure` with the marker on both surfaces instead."""
    summary_path = tmp_path / "step-summary.txt"
    summary_path.write_text("", encoding="utf-8")

    result = _run(
        ["1", "demo label", "--", "sleep", "5"],
        GITHUB_STEP_SUMMARY=str(summary_path),
    )

    assert result.returncode == 1, result.stdout
    assert f"{MARKER} demo label exceeded its internal bound of 1s" in result.stdout
    assert (
        f"{MARKER} demo label exceeded its internal bound of 1s"
        in summary_path.read_text(encoding="utf-8")
    )


@pytest.mark.timeout(30)
def test_timeout_kill_marker_is_absent_when_the_job_is_cancelled_by_signal(tmp_path):
    """The command is killed by a signal delivered from OUTSIDE the
    script well before its own inner bound could fire -- the shape of a
    human cancel or a `cancel-in-progress` supersession. No marker appears
    on either surface, so the ordinary `cancelled` conclusion is left
    exactly as informative (or uninformative) as it always was."""
    summary_path = tmp_path / "step-summary.txt"
    summary_path.write_text("", encoding="utf-8")

    env = dict(os.environ)
    env["GITHUB_STEP_SUMMARY"] = str(summary_path)

    proc = subprocess.Popen(
        [str(SCRIPT), "10", "demo label", "--", "sleep", "5"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        env=env,
        # A real job-level cancel kills the whole process tree, not just
        # the immediate child -- put this script in its own process group
        # so the signal below reaches `timeout(1)` and `sleep` the same way.
        preexec_fn=os.setsid,
    )
    time.sleep(0.3)
    os.killpg(proc.pid, signal.SIGTERM)
    out, _ = proc.communicate(timeout=10)

    assert MARKER not in out
    assert summary_path.read_text(encoding="utf-8") == ""
    # Killed by SIGTERM, well before the 10s inner bound and nowhere near
    # GNU timeout's own self-inflicted 124.
    assert proc.returncode != 124
    assert proc.returncode < 0 or proc.returncode >= 128
