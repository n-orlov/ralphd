"""Task 048 (B.5 follow-on, operator steer 005): the harness-level fix for
the four 10.00s teardown entries `artifacts/reports/v0.19.1-suite-budget.md`
names at task 011's measurement.

A run parked in `state: "paused"` sits forever in `await self._pause.wait()`
(`loop.py`'s `_park_until_resumed`) -- a plain `asyncio.Event` nothing wires
a signal handler to. `abort_on_signal` (`main.py`'s SIGTERM/SIGINT handler)
records the abort and interrupts the CHILD process if one happens to be
running, but never wakes that `Event`, so `main.py`'s own `await job_task`
never returns: the engine simply does not react to SIGTERM while paused.
`LiveRun.stop()` used to always `terminate()` then wait the ordinary 10s
grace before falling through to `kill()` -- a grace a paused engine can
never honour, so every one of the four named nodes paid the full 10s in
teardown for nothing. The cure (this same commit, `tests/conftest.py`) reads
`status.json` up front and, when the state is already `paused`, skips
straight to a bounded `kill()` instead of waiting out that grace.

This pins the cure directly against a real paused engine, rather than
relying on it merely showing up as a side effect in the four original
nodes' own teardown durations.
"""
import time


def test_stop_on_a_paused_engine_skips_the_sigterm_grace_it_cannot_honour(
        engine_factory):
    e = engine_factory(job={"on_complete": "exit", "max_approaches": 1},
                       stub_env={"STUB_WORKER_STALLS": "1"})
    status = e.wait_state(("paused",), timeout=60)
    assert status["pauseReason"] == "stagnation"
    assert e.proc.poll() is None, "still alive and parked, precondition"

    started = time.monotonic()
    e.stop()
    elapsed = time.monotonic() - started

    assert e.proc.poll() is not None, "stop() must have actually ended it"
    # The ordinary (non-paused) grace is 10s; a paused engine must never
    # wait it out -- bounded well under that, with headroom for a slow box.
    assert elapsed < 5.0, (
        f"stop() on a paused engine took {elapsed:.2f}s -- it must detect "
        "the paused state and go straight to a bounded kill() instead of "
        "waiting out the ordinary SIGTERM grace a paused engine ignores")

    # A second stop() call (what the fixture's own teardown does next) is a
    # no-op on an already-dead process, never a second wait.
    started = time.monotonic()
    e.stop()
    assert time.monotonic() - started < 1.0
