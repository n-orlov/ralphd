"""Task 048 (#4): absolute local-time timestamps in the hub timeline,
`ralphctl logs` and `ralphctl status`.

A relative duration ("took 4m 12s", "elapsed 3h 12m") cannot be correlated
with anything outside the run -- an upstream outage window, a host reboot,
another run's log. Every surface that shows *when* something happened now
renders it through ONE shared formatter,
`ralphd.engine.state.format_local_time`, and the raw ISO wire value is
left untouched in the payload for machine consumers and sorting.

Three tiers, no engine and no container:
- unit tests over the formatter itself;
- black-box `ralphctl status` / `ralphctl logs` over on-disk run-dir
  fixtures (reusing test_cli_status_degraded's stub-docker `Ctl` seeding
  and test_cli_logs_dead_run's dead-run fixture);
- an endpoint test over the hub's run-detail payload (the *Local fields
  the timeline renders, plus the untouched ISO fields).

The browser-tier assertion that the timeline cell actually shows the
string lives in tests/test_browser_hub.py.
"""

from __future__ import annotations

import json
import time

from test_cli_docker import Ctl, ctl, unix_sock
from test_cli_logs_dead_run import _ctl, _dead_run
from test_cli_status_degraded import _seed_status
from test_cli_ui import UiServer, _write_dead_run, ui

from ralphd.engine.state import format_local_time, parse_utc, utc_from_epoch

__all__ = ["ctl", "ui", "unix_sock"]

# The fixture instants used by the run-dir fixtures below, pre-rendered
# through the shared formatter: the tests assert the CLI/hub print exactly
# what the formatter produces for that instant, without hardcoding a
# timezone (CI and this host need not agree on TZ).
ITER_1_START = "2026-01-01T00:01:00Z"
ITER_1_END = "2026-01-01T00:01:30Z"
STATUS_START = "2024-01-01T00:00:00Z"
STATUS_END = "2024-01-01T01:02:03Z"


# --------------------------------------------------------------------------
# formatter unit tests
# --------------------------------------------------------------------------

def test_format_local_time_renders_the_instant_in_local_time_with_offset():
    ts = "2026-01-01T00:01:30Z"
    got = format_local_time(ts)
    expected = time.strftime("%Y-%m-%d %H:%M:%S %z",
                             time.localtime(parse_utc(ts)))
    assert got == expected
    # absolute, not relative: a date, a wall clock and a UTC offset
    assert got.startswith(("2025-12-31", "2026-01-01", "2026-01-02"))
    assert len(got.split()) == 3
    offset = got.split()[-1]
    assert offset[0] in "+-" and offset[1:].isdigit()


def test_format_local_time_round_trips_the_encoded_instant():
    """Whatever the host timezone, the formatted wall clock must describe
    the SAME instant as the ISO input (no naive-UTC-as-local bug)."""
    ts = utc_from_epoch(1767225690)  # 2026-01-01T00:01:30Z
    got = format_local_time(ts)
    parsed = time.mktime(time.strptime(got[:19], "%Y-%m-%d %H:%M:%S"))
    # mktime interprets the struct as local time -> back to epoch
    assert abs(parsed - parse_utc(ts)) < 1.5


def test_format_local_time_degrades_instead_of_raising():
    assert format_local_time(None) == "n/a"
    assert format_local_time("") == "n/a"
    # unparseable value renders itself rather than exploding a status line
    assert format_local_time("whenever") == "whenever"


# --------------------------------------------------------------------------
# `ralphctl status`
# --------------------------------------------------------------------------

def _status_lines(res) -> dict[str, str]:
    out = {}
    for line in res.stdout.splitlines():
        if ":" in line:
            key, _, rest = line.partition(":")
            out.setdefault(key.strip(), rest.strip())
    return out


def test_status_prints_absolute_started_and_ended_alongside_the_duration(ctl: Ctl):
    _seed_status(ctl, "tst-abs-ts", state="failed",
                 startedAt=STATUS_START, endedAt=STATUS_END)
    res = ctl.run("status", "tst-abs-ts")
    assert res.returncode == 0, res.stderr
    lines = _status_lines(res)
    assert lines["started"] == format_local_time(STATUS_START), res.stdout
    assert lines["ended"] == format_local_time(STATUS_END), res.stdout
    # the relative duration line is kept alongside, not replaced
    assert "duration" in lines and "(total)" in res.stdout


def test_status_json_keeps_the_raw_iso_timestamps(ctl: Ctl):
    _seed_status(ctl, "tst-abs-ts-json", state="failed",
                 startedAt=STATUS_START, endedAt=STATUS_END)
    res = ctl.run("--json", "status", "tst-abs-ts-json")
    assert res.returncode == 0, res.stderr
    doc = json.loads(res.stdout)
    assert doc["startedAt"] == STATUS_START
    assert doc["endedAt"] == STATUS_END


def test_status_of_a_running_run_shows_started_but_no_ended(ctl: Ctl):
    _seed_status(ctl, "tst-abs-ts-live", state="running", endedAt=None,
                 startedAt=utc_from_epoch(time.time() - 90))
    res = ctl.run("status", "tst-abs-ts-live")
    assert res.returncode == 0, res.stderr
    lines = _status_lines(res)
    assert "started" in lines and lines["started"] != "n/a"
    assert "ended" not in lines, res.stdout


# --------------------------------------------------------------------------
# `ralphctl logs`
# --------------------------------------------------------------------------

def test_logs_boundary_lines_carry_absolute_timestamps(tmp_path):
    registry, _ = _dead_run(tmp_path, "abs-ts-logs")
    res = _ctl(registry, "logs", "abs-ts-logs")
    assert res.returncode == 0, (res.stdout, res.stderr)
    start_stamp = format_local_time(ITER_1_START)
    end_stamp = format_local_time(ITER_1_END)
    starts = [ln for ln in res.stdout.splitlines()
              if ln.startswith("── iteration 1 ")]
    assert len(starts) == 1, res.stdout
    assert f"started {start_stamp}" in starts[0]
    dones = [ln for ln in res.stdout.splitlines() if "iteration 1 done" in ln]
    assert len(dones) == 1, res.stdout
    assert f"at {end_stamp}" in dones[0]
    # the relative duration stays alongside the absolute instant
    assert "took " in dones[0]


def test_logs_raw_mode_keeps_the_iso_wire_values(tmp_path):
    """`--raw` is a 1:1 wire contract: the boundary lines it emits carry the
    raw ISO timestamps, never the human rendering."""
    registry, _ = _dead_run(tmp_path, "abs-ts-raw")
    res = _ctl(registry, "logs", "--raw", "abs-ts-raw")
    assert res.returncode == 0, (res.stdout, res.stderr)
    boundaries = [json.loads(ln) for ln in res.stdout.splitlines()
                  if ln.startswith("{") and "ralphd.iteration" in ln]
    assert boundaries, res.stdout
    assert any(b.get("startedAt") == ITER_1_START for b in boundaries)
    assert format_local_time(ITER_1_START) not in res.stdout


# --------------------------------------------------------------------------
# hub run-detail payload (what the timeline renders)
# --------------------------------------------------------------------------

def test_run_detail_payload_carries_local_strings_and_raw_iso(tmp_path, ui):
    registry = tmp_path / "registry"
    run_dir = _write_dead_run(registry, "abs-ts-hub", state="succeeded",
                              startedAt=STATUS_START, endedAt=STATUS_END)
    d = run_dir / "iterations" / "0001"
    d.mkdir(parents=True)
    (d / "meta.json").write_text(json.dumps(
        {"number": 1, "phase": "planning", "model": "stub-model", "approach": 1,
         "startedAt": ITER_1_START, "endedAt": ITER_1_END,
         "exitCode": 0, "error": None, "usage": {"totalTokens": 10}}))

    server: UiServer = ui(registry)
    code, detail = server.get("/api/runs/abs-ts-hub")
    assert code == 200, detail

    it = detail["iterations"][0]
    # formatted server-side by the one shared formatter ...
    assert it["startedAtLocal"] == format_local_time(ITER_1_START)
    assert it["endedAtLocal"] == format_local_time(ITER_1_END)
    # ... with the raw ISO values still in the payload (consumers, sorting)
    assert it["startedAt"] == ITER_1_START
    assert it["endedAt"] == ITER_1_END

    status = detail["status"]
    assert status["startedAtLocal"] == format_local_time(STATUS_START)
    assert status["endedAtLocal"] == format_local_time(STATUS_END)
    assert status["startedAt"] == STATUS_START and status["endedAt"] == STATUS_END
