"""Unit tests for `wait_engine_exit` (task 016, requirement F.1/F.3, #216):
the reusable barrier every flow uses to wait for the engine subprocess to
exit, and its dump on a `Popen.wait` timeout.

`tests/flows/test_flow_cure_pass.py::test_the_second_curable_rejection_
finds_the_cure_pass_spent_and_replans` was reported timing out on a bare
`run.proc.wait(timeout=60)` under CI load, with two shapes fitting the
symptom and neither visible in the failure message: a single hung child
process eating the whole 60 s floor, or a run parked in
`_park_until_resumed` (which ignores SIGTERM, per the real engine
harness's own `stop()` docstring) on a paused status. This module drives one synthetic hang per
shape against a bare fake run (a real, deliberately-sleeping child process
plus a hand-written run dir -- no real `ralphd-engine`, no real cure-pass
node) and asserts the dump names the correct shape and carries all four
sections F.1 asks for.

**F.3's acceptance boundary, restated exactly as `flowlib.wait_engine_exit`
itself restates it**: this requirement is satisfied by the helper existing,
a synthetic hang exercising it, and the helper wired into the real node's
wait (task 017) -- never by reproducing the CI flake. #216's hunt ran
~100+ executions, including on the real CI host, without reproducing it,
and that is not grounds to reject this diagnosis; closing #216 for good
plausibly needs a soak in the reviewer's own environment that no test here
can manufacture. Nothing in this module attempts to reproduce the flake.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from tests.flows.flowlib import wait_engine_exit


class _FakeRun:
    """The only two things `wait_engine_exit` needs off a `run`: `.proc`
    (a real `Popen`) and `.run_dir` (a bare directory, no registry, no
    live engine)."""

    def __init__(self, proc: subprocess.Popen, run_dir: Path):
        self.proc = proc
        self.run_dir = run_dir


def _sleeper() -> subprocess.Popen:
    """A real, deliberately long-sleeping child -- the synthetic hang.
    Piped stdout/stderr, `text=False`, matching the real engine harness's
    own Popen shape closely enough to exercise the pending-pipe-bytes
    section."""
    return subprocess.Popen(
        [sys.executable, "-c",
         "import sys, time; sys.stdout.write('hello from the hung child\\n'); "
         "sys.stdout.flush(); time.sleep(30)"],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT)


def _write_status(run_dir: Path, **fields) -> None:
    (run_dir / "status.json").write_text(json.dumps(fields))


def _write_events(run_dir: Path, *events: dict) -> None:
    (run_dir / "events.jsonl").write_text(
        "".join(json.dumps(ev) + "\n" for ev in events))


@pytest.fixture
def sleeping_child():
    proc = _sleeper()
    yield proc
    proc.kill()
    proc.wait(timeout=5)


def _assert_four_sections(message: str) -> None:
    for section in ("process tree", "pending pipe bytes", "status.json",
                    "events.jsonl tail"):
        assert section in message, (
            f"wait_engine_exit dump is missing its {section!r} section:\n"
            f"{message}")


def test_a_hung_child_is_named_and_dumped_on_timeout(tmp_path, sleeping_child):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    # Ordinary, non-paused status: nothing parked this run -- the whole
    # difference between the two shapes.
    _write_status(run_dir, state="running", approach=1)
    _write_events(run_dir, {"type": "phase", "phase": "worker", "approach": 1},
                  {"type": "iteration.start", "phase": "worker"})
    run = _FakeRun(sleeping_child, run_dir)

    with pytest.raises(AssertionError) as excinfo:
        wait_engine_exit(run, timeout=0.3)
    message = str(excinfo.value)

    assert "hung child" in message, (
        f"a non-paused status must be named as a hung child, got:\n{message}")
    assert "parked run" not in message, (
        f"a non-paused status must not also be named a parked run:\n{message}")
    _assert_four_sections(message)
    assert str(sleeping_child.pid) in message, (
        f"the dump must name the actual pid it inspected:\n{message}")


def test_a_parked_run_is_named_and_dumped_on_timeout(tmp_path, sleeping_child):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    # A run parked on `rejected_last_approach` -- state: "paused" is the
    # observable a real `_park_until_resumed` leaves behind, regardless of
    # whether the underlying child process is itself hung.
    _write_status(run_dir, state="paused", pauseReason="rejected_last_approach",
                  approach=2)
    _write_events(run_dir, {"type": "phase", "phase": "review", "approach": 2},
                  {"type": "signal", "signal": "rejected_last_approach"})
    run = _FakeRun(sleeping_child, run_dir)

    with pytest.raises(AssertionError) as excinfo:
        wait_engine_exit(run, timeout=0.3)
    message = str(excinfo.value)

    assert "parked run" in message, (
        f"a paused status must be named as a parked run, got:\n{message}")
    assert "rejected_last_approach" in message, (
        f"the dump must carry the recorded pauseReason:\n{message}")
    _assert_four_sections(message)


def test_wait_engine_exit_returns_the_real_exit_code_without_raising(tmp_path):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    _write_status(run_dir, state="succeeded", approach=1)
    proc = subprocess.Popen([sys.executable, "-c", "raise SystemExit(0)"],
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    run = _FakeRun(proc, run_dir)

    assert wait_engine_exit(run, timeout=10) == 0
