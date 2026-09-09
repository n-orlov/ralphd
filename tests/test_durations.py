"""Black-box tests for human-readable durations (operator steering 009,
task 051): `ralphctl status` overall/iteration duration lines + --json
duration fields, and the `logs` pretty renderer's per-iteration duration.
"""

from __future__ import annotations

import calendar
import json
import time

from tests.conftest import wait_hold_entered


def _started_epoch(run, timeout: float = 30.0) -> int:
    """The run's own `startedAt`, as epoch seconds, once it exists.

    Task 019 follow-on: a duration the ENGINE measures is anchored to the
    engine's `startedAt`, not to the moment the `live` fixture's constructor
    returned -- engine boot sits between the two, and on a slow host it is
    seconds wide. The test therefore has to read that anchor rather than
    assume it.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        sf = run.run_dir / "status.json"
        if sf.exists():
            try:
                started = json.loads(sf.read_text()).get("startedAt")
            except json.JSONDecodeError:
                started = None
            if started:
                return calendar.timegm(time.strptime(started, "%Y-%m-%dT%H:%M:%SZ"))
        time.sleep(0.05)
    raise TimeoutError("the run never recorded a startedAt")


def test_status_shows_total_duration_and_json_fields_when_terminal(live, tmp_path):
    # task 010 (B.4): the whole point of this test is that `durationSeconds`
    # tracks REAL wall-clock elapsed time -- there is no stub-side knob to
    # fake that, so instead of STUB_SLEEP padding every phase, hold the
    # very first (planning) invocation open and let the TEST's own real
    # sleep (not a stub config knob) be what the run's duration measures.
    hold_file = tmp_path / "hold-durterm"
    run = live(run_id="dur-terminal", job={"iterations": 6},
               stub_env={"STUB_TASKS": "1", "STUB_HOLD_FILE": str(hold_file),
                        "STUB_HOLD_PHASE": "planning", "STUB_HOLD_TIMEOUT_S": "30"})
    # Task 019 follow-on -- CAUSE and OBSERVE the elapsed window instead of
    # assuming a sleep taken from the test's own timeline lands inside the
    # run's. A bare `time.sleep(3.5)` here began before the engine had even
    # recorded `startedAt`, so on the hosted gate at `7e685e2` the run's
    # measured span was only part of it: `durationSeconds` came back 2
    # against this test's own `>= 3` (check run 102649436814). Wait for the
    # planning child to be parked in its hold, read the engine's OWN
    # `startedAt`, and release the hold only once the run itself has
    # provably been alive long enough -- no bigger timeout, no looser
    # assertion, and the >=3 threshold below is untouched.
    wait_hold_entered(hold_file)
    release_at = _started_epoch(run) + 5.0
    while time.time() < release_at:
        time.sleep(0.05)
    hold_file.touch()
    run.wait_terminal()

    res = run.ralphctl("status", run.run_id)
    assert res.returncode == 0, (res.stdout, res.stderr)
    assert "duration:" in res.stdout
    assert "(total)" in res.stdout
    # no leftover currentIteration section on a terminal job
    assert "iteration elapsed:" not in res.stdout

    jres = run.ralphctl("--json", "status", run.run_id)
    assert jres.returncode == 0, (jres.stdout, jres.stderr)
    status = json.loads(jres.stdout)
    assert isinstance(status["durationSeconds"], (int, float))
    # the real >=5s window held open above (measured from the engine's own
    # `startedAt`, task 019 follow-on) guarantees a genuinely-measurable
    # (>=3s) duration -- tight enough that a broken/zeroed-out duration
    # computation cannot hide behind slack the way a near-instant job's
    # duration would.
    assert status["durationSeconds"] >= 3
    # existing timestamp fields are untouched, just augmented
    assert status["startedAt"] and status["endedAt"]
    # sanity: matches the timestamps within a couple seconds of slack
    def parse(ts):
        return calendar.timegm(time.strptime(ts, "%Y-%m-%dT%H:%M:%SZ"))
    expected = parse(status["endedAt"]) - parse(status["startedAt"])
    assert abs(status["durationSeconds"] - expected) <= 2


def test_status_shows_elapsed_so_far_and_current_iteration_elapsed_while_running(live, tmp_path):
    # task 010 (B.4): hold the first (planning) invocation open instead of
    # STUB_SLEEP-padding every phase -- gives a deterministic window in
    # which the run is guaranteed still "running" with an in-flight
    # currentIteration, with no real-time race against how fast the stub
    # would otherwise finish.
    hold_file = tmp_path / "hold-durrunning"
    run = live(run_id="dur-running", job={"iterations": 6},
               stub_env={"STUB_TASKS": "1", "STUB_HOLD_FILE": str(hold_file),
                        "STUB_HOLD_PHASE": "planning", "STUB_HOLD_TIMEOUT_S": "30"})
    # Poll until we observe an in-flight iteration (currentIteration set),
    # well before the held planning iteration is released.
    deadline = time.time() + 30
    status = None
    while time.time() < deadline:
        sf = run.run_dir / "status.json"
        if sf.exists():
            try:
                status = json.loads(sf.read_text())
            except json.JSONDecodeError:
                status = None
        if status and status.get("state") == "running" and status.get("currentIteration"):
            break
        time.sleep(0.2)
    assert status and status.get("currentIteration"), "job never observed running with an in-flight iteration"

    run.wait_api()
    res = run.ralphctl("status", run.run_id)
    assert res.returncode == 0, (res.stdout, res.stderr)
    assert "duration:" in res.stdout
    assert "(elapsed)" in res.stdout
    assert "iteration elapsed:" in res.stdout

    jres = run.ralphctl("--json", "status", run.run_id)
    assert jres.returncode == 0, (jres.stdout, jres.stderr)
    jstatus = json.loads(jres.stdout)
    assert isinstance(jstatus["durationSeconds"], (int, float))
    assert jstatus["durationSeconds"] >= 0
    assert jstatus.get("endedAt") is None
    cur = jstatus.get("currentIteration")
    assert cur is not None
    assert isinstance(cur["elapsedSeconds"], (int, float))
    assert cur["elapsedSeconds"] >= 0

    hold_file.touch()
    run.wait_terminal(timeout=90)


def test_logs_pretty_shows_per_iteration_duration_once_ended(live):
    run = live(run_id="dur-logs", stub_env={"STUB_RICH_EVENTS": "1", "STUB_TASKS": "1"})
    run.wait_terminal()

    res = run.ralphctl("logs", run.run_id, "--tail", "0")
    assert res.returncode == 0, (res.stdout, res.stderr)
    out = res.stdout
    assert "iteration 1 done" in out
    assert "took " in out
    # raw mode carries the raw startedAt/endedAt fields the duration is
    # computed from, unrendered (still 1 line == 1 event, unchanged by 051)
    raw = run.ralphctl("logs", run.run_id, "--raw", "--tail", "0")
    assert raw.returncode == 0
    parsed = []
    for line in raw.stdout.splitlines():
        if line.strip().startswith("{"):
            try:
                parsed.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    boundaries = [b for b in parsed if b.get("type") == "ralphd.iteration"]
    ends = [b for b in boundaries if b.get("event") == "end"]
    assert ends and all(b.get("startedAt") and b.get("endedAt") for b in ends)
