"""Task 001 (PRD A.1/A.2/A.3/A.4, #217/#216): `RunDir.acquire_lock` used to
take the run-dir `flock` non-blocking, exactly once -- a predecessor engine
in the last milliseconds of its own exit tail (#216 measured that
terminal-state->process-exit lag at 0.16-0.32 s in every condition tested,
including under CPU starvation) made a successor fail permanently with
`RunDirLocked`/`EXIT_RUN_DIR_LOCKED` instead of a brief wait.

Four things pinned here, each naming the requirement it proves:

* A.3 -- the happy path (lock free on the first attempt) returns with no
  sleep at all: measured elapsed time on a real call.
* A.1/A.2 -- the bound is a named module constant
  (`ACQUIRE_LOCK_TIMEOUT_S`), justified in its own comment against #216's
  measured figures.
* A.4 (direction 1) -- a predecessor that releases the lock *inside* the
  window lets the successor acquire it and proceed.
* A.4 (direction 2) -- a predecessor that holds the lock *past* the window
  still raises `RunDirLocked`, which the unchanged `main.amain()` call site
  (`src/ralphd/engine/main.py:169-173`) turns into `EXIT_RUN_DIR_LOCKED`;
  proven both at the `acquire_lock` level and end-to-end through a real
  `amain()` call, the same in-process pattern
  `tests/test_engine_budget_seed.py` uses.
"""

from __future__ import annotations

import fcntl
import json
import socket
import threading
import time

import pytest

from ralphd.engine import state as state_mod
from ralphd.engine.state import RunDir, RunDirLocked


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_acquire_lock_bound_is_a_named_constant_justified_against_216(tmp_path):
    """A.2: the bound is a module-level named constant, not a magic
    number, and its own comment names #216's measured 0.16-0.32 s figure."""
    import inspect

    assert state_mod.ACQUIRE_LOCK_TIMEOUT_S > 0
    # comfortably above the measured tail, comfortably below "a human
    # notices this as a hang" -- low single-digit seconds, per A.2.
    assert 1.0 <= state_mod.ACQUIRE_LOCK_TIMEOUT_S <= 5.0
    src = inspect.getsource(state_mod)
    idx = src.index("ACQUIRE_LOCK_TIMEOUT_S = ")
    comment = src[:idx]
    assert "#216" in comment
    assert "0.16" in comment and "0.32" in comment


def test_lock_free_on_first_attempt_returns_without_sleeping(tmp_path):
    """A.3: the happy path never sleeps."""
    run = RunDir(tmp_path / "run")
    started = time.monotonic()
    fh = run.acquire_lock()
    elapsed = time.monotonic() - started
    try:
        # Well under even one poll interval -- a genuine no-sleep return,
        # not merely "faster than the full timeout".
        assert elapsed < state_mod.ACQUIRE_LOCK_POLL_INTERVAL_S
    finally:
        fh.close()


def test_predecessor_releasing_inside_the_window_lets_the_successor_acquire_and_run(
    tmp_path, monkeypatch
):
    """A.1/A.4 (direction 1): a predecessor holding the lock for less than
    the timeout releases before the deadline, and the successor's retried
    `acquire_lock` call acquires the lock and returns -- runs -- rather
    than raising."""
    monkeypatch.setattr(state_mod, "ACQUIRE_LOCK_TIMEOUT_S", 1.0)
    monkeypatch.setattr(state_mod, "ACQUIRE_LOCK_POLL_INTERVAL_S", 0.02)

    run = RunDir(tmp_path / "run")
    predecessor_fh = open(run.lock_file, "a+")
    fcntl.flock(predecessor_fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    release_at = time.monotonic() + 0.2  # well inside the 1.0 s window

    def _release_predecessor():
        time.sleep(max(0.0, release_at - time.monotonic()))
        fcntl.flock(predecessor_fh.fileno(), fcntl.LOCK_UN)
        predecessor_fh.close()

    releaser = threading.Thread(target=_release_predecessor)
    releaser.start()
    try:
        started = time.monotonic()
        successor_fh = run.acquire_lock()  # must retry, then succeed
        elapsed = time.monotonic() - started
    finally:
        releaser.join(timeout=5)

    try:
        # Acquired only after the predecessor actually let go, and well
        # before the 1.0 s deadline would have expired.
        assert elapsed >= 0.15
        assert elapsed < 1.0
        # "and run": the successor genuinely holds the lock now -- it can
        # write through it exactly as amain() does after acquiring it.
        successor_fh.seek(0)
        successor_fh.truncate()
        successor_fh.write("12345\n")
        successor_fh.flush()
        assert run.lock_file.read_text() == "12345\n"
    finally:
        successor_fh.close()


def test_predecessor_holding_past_the_window_still_raises_run_dir_locked(
    tmp_path, monkeypatch
):
    """A.1/A.4 (direction 2): a predecessor that never releases the lock
    within the window still causes `acquire_lock` to raise
    `RunDirLocked` once the deadline passes -- the guard is not dropped."""
    monkeypatch.setattr(state_mod, "ACQUIRE_LOCK_TIMEOUT_S", 0.2)
    monkeypatch.setattr(state_mod, "ACQUIRE_LOCK_POLL_INTERVAL_S", 0.02)

    run = RunDir(tmp_path / "run")
    predecessor_fh = open(run.lock_file, "a+")
    fcntl.flock(predecessor_fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        started = time.monotonic()
        with pytest.raises(RunDirLocked):
            run.acquire_lock()
        elapsed = time.monotonic() - started
        # It waited out (most of) the window rather than failing instantly.
        assert elapsed >= 0.15
    finally:
        fcntl.flock(predecessor_fh.fileno(), fcntl.LOCK_UN)
        predecessor_fh.close()


async def _run_amain_over_locked_dir(tmp_path, monkeypatch, *, timeout_s):
    """The same in-process `amain()` pattern
    `tests/test_engine_budget_seed.py::_run_amain_capturing_cfg` uses,
    trimmed to just what a locked run dir needs: no `run_job`/`serve`
    stubbing is required because a still-held lock never gets that far."""
    from ralphd.engine import config as config_mod
    from ralphd.engine import main as main_mod
    from ralphd.engine import skills as skills_mod

    cfg_dir, run_dir, ws = tmp_path / "cfg", tmp_path / "run", tmp_path / "ws"
    for d in (cfg_dir, run_dir, ws):
        d.mkdir()
    (cfg_dir / "prd.md").write_text("# PRD\n")
    (cfg_dir / "job.yaml").write_text(
        "run_id: lock-retry\niterations: 1\non_complete: exit\n")

    monkeypatch.setattr(config_mod, "CONFIG_DIR", cfg_dir)
    monkeypatch.setattr(main_mod, "CONFIG_DIR", cfg_dir)
    monkeypatch.setattr(skills_mod, "CONFIG_DIR", cfg_dir)
    monkeypatch.setenv("RALPHD_RUN_DIR", str(run_dir))
    monkeypatch.setenv("RALPHD_WORKSPACE_DIR", str(ws))
    monkeypatch.setenv("RALPHD_PORT", str(_free_port()))
    monkeypatch.setattr(state_mod, "ACQUIRE_LOCK_TIMEOUT_S", timeout_s)
    monkeypatch.setattr(state_mod, "ACQUIRE_LOCK_POLL_INTERVAL_S", 0.02)

    run_dir_lock_file = run_dir / ".lock"
    predecessor_fh = open(run_dir_lock_file, "a+")
    fcntl.flock(predecessor_fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        rc = await main_mod.amain()
    finally:
        fcntl.flock(predecessor_fh.fileno(), fcntl.LOCK_UN)
        predecessor_fh.close()
    return rc


async def test_amain_exits_run_dir_locked_when_the_window_is_exceeded(
    tmp_path, monkeypatch
):
    """A.4 (direction 2), end to end: the unchanged `main.amain()` call
    site (`src/ralphd/engine/main.py:169-173`) still turns a `RunDirLocked`
    that survives the whole retry window into `EXIT_RUN_DIR_LOCKED`."""
    from ralphd.engine.main import EXIT_RUN_DIR_LOCKED

    rc = await _run_amain_over_locked_dir(tmp_path, monkeypatch, timeout_s=0.15)
    assert rc == EXIT_RUN_DIR_LOCKED
