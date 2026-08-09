"""Black-box tests for human-readable durations (operator steering 009,
task 051): `ralphctl status` overall/iteration duration lines + --json
duration fields, and the `logs` pretty renderer's per-iteration duration.
"""

from __future__ import annotations

import calendar
import json
import time


def test_status_shows_total_duration_and_json_fields_when_terminal(live):
    run = live(run_id="dur-terminal", job={"iterations": 6},
               stub_env={"STUB_TASKS": "1", "STUB_SLEEP": "3"})
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
    # STUB_SLEEP=3 across >=3 real iterations (planning/worker/review, at
    # least) guarantees a genuinely-measurable (>=3s) duration -- tight
    # enough that a broken/zeroed-out duration computation cannot hide
    # behind slack the way a near-instant job's duration would.
    assert status["durationSeconds"] >= 3
    # existing timestamp fields are untouched, just augmented
    assert status["startedAt"] and status["endedAt"]
    # sanity: matches the timestamps within a couple seconds of slack
    def parse(ts):
        return calendar.timegm(time.strptime(ts, "%Y-%m-%dT%H:%M:%SZ"))
    expected = parse(status["endedAt"]) - parse(status["startedAt"])
    assert abs(status["durationSeconds"] - expected) <= 2


def test_status_shows_elapsed_so_far_and_current_iteration_elapsed_while_running(live):
    run = live(run_id="dur-running", job={"iterations": 6},
               stub_env={"STUB_TASKS": "1", "STUB_SLEEP": "5"})
    # Poll until we observe an in-flight iteration (currentIteration set),
    # well before the job (which sleeps 5s per invocation across several
    # iterations) reaches a terminal state.
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
