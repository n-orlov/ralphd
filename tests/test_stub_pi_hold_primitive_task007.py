"""Pins the stub `pi`'s bounded, env-driven hold primitive (task 007, B.1).

`STUB_HOLD_FILE`/`STUB_HOLD_TIMEOUT_S` let a test open a real race window
inside a stub `pi` invocation instead of guessing a fixed `STUB_SLEEP`
duration. This module invokes `tests/stub-pi/pi` directly (no engine
involved) and checks both directions:

  * released promptly -> the invocation returns quickly, well under the
    fallback bound, and emits its ordinary role payload;
  * never released -> the invocation FAILS on the bound (nonzero exit,
    stderr naming the bound, no role payload), rather than hanging
    forever or quietly continuing on a window that never opened -- the
    whole point of a BOUNDED fallback that is also a real failure. A
    nested pytest run pins the end-to-end consequence: a test that forgets
    to release the hold goes RED on the bound.

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

# The stub's own documented exit code for "the hold was never released".
HOLD_TIMEOUT_EXIT_CODE = 3

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
    assert '"type": "message_end"' in proc.stdout

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
    # Reaching the bound is a FAILURE, not a silent continue: nonzero exit,
    # a stderr line naming the bound, and no role payload emitted at all.
    assert proc.returncode == HOLD_TIMEOUT_EXIT_CODE, (proc.returncode, proc.stderr)
    assert "never released" in proc.stderr
    assert "STUB_HOLD_TIMEOUT_S" in proc.stderr
    assert str(bound) in proc.stderr
    assert '"type": "message_end"' not in proc.stdout

    duration_file = Path(f"{hold_file}.stub-hold-duration")
    assert duration_file.exists()
    duration = float(duration_file.read_text())

    assert duration >= bound, "must have actually waited out the fallback"
    assert duration <= bound + MARGIN_S, (
        f"a never-released hold must fail fast on the bound ({bound}s) plus "
        f"a stated margin ({MARGIN_S}s), took {duration}s")
    assert wall <= bound + MARGIN_S


INNER_TEST = '''
import os, subprocess, sys, time
from pathlib import Path


def test_forgets_to_release_the_hold(tmp_path):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    hold_file = tmp_path / "never-touched"
    env = {**os.environ, "STUB_RUN_DIR": str(run_dir),
           "STUB_HOLD_FILE": str(hold_file),
           "STUB_HOLD_TIMEOUT_S": "1.0"}
    proc = subprocess.run(
        [sys.executable, %(stub)r],
        input="unrecognized phase\\n", capture_output=True, text=True,
        env=env, timeout=30,
    )
    proc.check_returncode()   # red on the bound, never a hang
'''


def test_a_test_that_never_releases_the_hold_goes_red_on_the_bound(tmp_path):
    """End-to-end: forgetting the release makes the TEST fail, on the bound.

    The inner pytest run (its own process, its own tmp dir) contains a test
    that opens a hold and never releases it. It must come back FAILED --
    not passed, not still running -- and the whole nested run must finish
    inside the hold's own bound plus this module's stated margin plus a
    small allowance for pytest's own start-up.
    """
    inner = tmp_path / "test_inner_forgotten_hold.py"
    inner.write_text(INNER_TEST % {"stub": str(STUB_PI)})

    start = time.monotonic()
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
         "-p", "no:randomly", str(inner)],
        capture_output=True, text=True, cwd=str(tmp_path), timeout=120,
        env={**os.environ, "PYTEST_ADDOPTS": ""},
    )
    wall = time.monotonic() - start

    assert proc.returncode != 0, f"inner run should have failed:\n{proc.stdout}"
    assert "1 failed" in proc.stdout, proc.stdout
    assert "never released" in proc.stdout, proc.stdout
    # Bound (1.0s) + this module's stated margin + a pytest start-up
    # allowance: a regression to a sleep-shaped or unbounded hold blows this.
    assert wall <= 1.0 + MARGIN_S + 30.0, f"nested run took {wall}s"


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
    # The doc must state that reaching the bound FAILS the invocation, not
    # that it continues quietly.
    assert "EXITS NONZERO" in doc
    assert "exit code 3" in doc
