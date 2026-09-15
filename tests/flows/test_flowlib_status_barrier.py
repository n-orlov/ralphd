"""Unit tests for `wait_status_terminal` (task 002, requirement M): the
second, narrower barrier `flowlib` gained to close the nightly race in
`test_flow_happy_path.py::test_the_same_path_under_on_complete_idle_keeps_the_engine_serving`
(nightly run 34921161300).

SPEC.md's ordering invariant (section 5.4, "A run's terminal `state` event
is appended before the status.json write that makes `state` itself read as
terminal") only orders the two writes -- events.jsonl first, status.json
second -- never says they land in the same instant. `wait_terminal_recorded`
blocks on the FIRST write; a caller that then reads `status.json` straight
off disk is racing the SECOND, and the gap between them is exactly what
this module drives directly, against a bare fake run dir, with no real
engine subprocess involved.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest

from tests.flows.flowlib import wait_status_terminal, wait_terminal_recorded


class _FakeRun:
    """The only thing every `flowlib` reader needs off a `run`: a
    `run_dir`. A bare fake, no engine subprocess, no `LiveRun`."""

    def __init__(self, run_dir: Path):
        self.run_dir = run_dir


def _write_status(run_dir: Path, **fields) -> None:
    (run_dir / "status.json").write_text(json.dumps(fields))


def _write_terminal_event(run_dir: Path, state: str = "succeeded") -> None:
    (run_dir / "events.jsonl").write_text(
        json.dumps({"type": "state", "state": state}) + "\n")


def test_terminal_event_on_disk_with_status_json_still_running_needs_the_new_barrier(tmp_path):
    """Drives a run dir whose ending `state` event is already on disk while
    `status.json` still reads `running` -- exactly the ordering the events
    barrier alone permits, and exactly what the nightly run hit. Without
    `wait_status_terminal`, the only thing a caller could do after the
    events barrier returns is read `status.json` directly off disk, which
    this asserts still reads the pre-terminal state at that moment -- i.e.
    this scenario fails an "it must be succeeded now" assertion without the
    new barrier. `wait_status_terminal` then bridges the remaining gap and
    returns the terminal status once the second write actually lands.
    """
    run = _FakeRun(tmp_path)
    _write_terminal_event(tmp_path, "succeeded")
    _write_status(tmp_path, state="running")

    # The events barrier returns immediately: the terminal event is already
    # on disk (SPEC.md guarantees it landed no later than the status write).
    ev = wait_terminal_recorded(run, timeout=5)
    assert ev["state"] == "succeeded"

    # The race: status.json has NOT caught up yet. A caller reading it
    # straight off disk right here -- the only option before this task --
    # observes the wrong, pre-terminal state.
    from tests.flows.flowlib import status
    assert status(run).get("state") == "running", (
        "setup check: this test's whole point is that status.json has not "
        "caught up to the terminal event yet")

    # The status write lands a little later, same as the real engine's
    # second write following its first (task 006/#169's invariant).
    def _catch_up_status():
        time.sleep(0.15)
        _write_status(tmp_path, state="succeeded", verdict="verified")

    threading.Thread(target=_catch_up_status, daemon=True).start()

    st = wait_status_terminal(run, timeout=5, interval=0.02)
    assert st.get("state") == "succeeded"
    assert st.get("verdict") == "verified"


def test_wait_status_terminal_raises_with_what_it_observed_on_timeout(tmp_path):
    run = _FakeRun(tmp_path)
    _write_status(tmp_path, state="running")

    with pytest.raises(TimeoutError) as excinfo:
        wait_status_terminal(run, timeout=0.2, interval=0.02)

    assert "running" in str(excinfo.value), (
        f"wait_status_terminal's timeout message should name what it "
        f"observed, got: {excinfo.value}")


def test_wait_status_terminal_returns_immediately_when_already_terminal(tmp_path):
    run = _FakeRun(tmp_path)
    _write_status(tmp_path, state="failed", verdict="unverified")

    st = wait_status_terminal(run, timeout=1)
    assert st.get("state") == "failed"
    assert st.get("verdict") == "unverified"


def test_wait_status_terminal_ignores_a_missing_or_mid_write_status_json(tmp_path):
    """`flowlib.status()` returns `{}` for a missing file or a JSON decode
    error mid-write; the barrier must keep polling through either, not
    raise on the first empty read."""
    run = _FakeRun(tmp_path)
    # No status.json at all yet.

    def _write_it_late():
        time.sleep(0.1)
        _write_status(tmp_path, state="aborted", reason="operator")

    threading.Thread(target=_write_it_late, daemon=True).start()

    st = wait_status_terminal(run, timeout=2, interval=0.02)
    assert st.get("state") == "aborted"
