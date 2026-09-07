"""Pins the stub `pi`'s bounded, env-driven hold primitive (task 007, B.1).

`STUB_HOLD_FILE`/`STUB_HOLD_TIMEOUT_S` let a test open a real race window
inside a stub `pi` invocation instead of guessing a fixed `STUB_SLEEP`
duration. This module invokes `tests/stub-pi/pi` directly (no engine
involved) and checks both directions:

  * released promptly -> the invocation returns quickly, well under the
    fallback bound;
  * never released -> the invocation still returns, on the bound, not by
    hanging forever -- the whole point of a BOUNDED fallback.

Both cases assert the stub's own recorded duration
(`<hold file>.stub-hold-duration`) stays within the bound plus a stated
margin, not just that the subprocess eventually exited.
"""
import os
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
STUB_PI = REPO / "tests" / "stub-pi" / "pi"

# Stated margin for every "duration must be within the bound" assertion in
# this module -- generous enough to absorb interpreter startup and process
# scheduling jitter on a loaded CI host, small enough that a regression
# back to something sleep-shaped (e.g. `time.sleep(9999)`) still fails it.
MARGIN_S = 2.0


def _run_stub(run_dir: Path, env_extra: dict) -> tuple[float, subprocess.CompletedProcess]:
    env = {**os.environ, "STUB_RUN_DIR": str(run_dir), **env_extra}
    start = time.monotonic()
    proc = subprocess.run(
        [sys.executable, str(STUB_PI)],
        input="unrecognized phase, no Role marker here\n",
        capture_output=True, text=True, env=env, timeout=30,
    )
    wall = time.monotonic() - start
    return wall, proc


def test_hold_releases_promptly_when_the_test_touches_the_file(tmp_path):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    hold_file = tmp_path / "hold-release"

    # Release the hold in a background thread shortly after launch, mirroring
    # how a real test would release it once it has observed whatever it was
    # waiting to observe mid-iteration.
    import threading

    def _release():
        time.sleep(0.2)
        hold_file.touch()

    threading.Thread(target=_release, daemon=True).start()

    wall, proc = _run_stub(run_dir, {
        "STUB_HOLD_FILE": str(hold_file),
        "STUB_HOLD_TIMEOUT_S": "5",
    })
    assert proc.returncode == 0, proc.stderr

    duration_file = Path(f"{hold_file}.stub-hold-duration")
    assert duration_file.exists()
    duration = float(duration_file.read_text())

    # Released around 0.2s in -- nowhere near the 5s fallback bound.
    assert duration < 1.0, f"released hold should return promptly, got {duration}s"
    assert wall < 1.0 + MARGIN_S


def test_hold_never_released_fails_on_the_bound_not_by_hanging(tmp_path):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    hold_file = tmp_path / "hold-never-released"
    bound = 1.5

    wall, proc = _run_stub(run_dir, {
        "STUB_HOLD_FILE": str(hold_file),
        "STUB_HOLD_TIMEOUT_S": str(bound),
    })
    assert proc.returncode == 0, proc.stderr
    # The invocation still completed (emitted its final message) rather
    # than hanging past the bound.
    assert '"type": "message_end"' in proc.stdout

    duration_file = Path(f"{hold_file}.stub-hold-duration")
    assert duration_file.exists()
    duration = float(duration_file.read_text())

    assert duration >= bound, "must have actually waited out the fallback"
    assert duration <= bound + MARGIN_S, (
        f"a never-released hold must fail fast on the bound ({bound}s) plus "
        f"a stated margin ({MARGIN_S}s), took {duration}s")
    assert wall <= bound + MARGIN_S


def test_hold_is_a_noop_when_unset(tmp_path):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    wall, proc = _run_stub(run_dir, {})
    assert proc.returncode == 0, proc.stderr
    assert wall < MARGIN_S


def test_help_text_documents_the_env_var_and_the_fallback_bound():
    source = STUB_PI.read_text()
    assert "STUB_HOLD_FILE" in source
    assert "STUB_HOLD_TIMEOUT_S" in source
    # The docstring itself (this stub's own help/usage text) names both the
    # release mechanism and the numeric default fallback bound.
    doc = source.split('"""', 2)[1]
    assert "STUB_HOLD_FILE" in doc
    assert "STUB_HOLD_TIMEOUT_S" in doc
    assert "5.0" in doc
