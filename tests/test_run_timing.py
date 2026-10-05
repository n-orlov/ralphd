"""#291 (PRD B.4-B.5): the ONE shared header/status timing helper.

`engine/state.run_timing` computes the original start, `wall` and `active`;
the hub's run header (`timingLines` on the run-detail status) and `ralphctl
status` both render what it returns. In-process, no engine, no container.
"""

from __future__ import annotations

import json

import pytest

import test_cli_logs_dead_run as _dead_run_module
from test_cli_logs_dead_run import _ctl
from test_cli_ui import UiServer, _write_dead_run, ui

from ralphd.engine.state import (
    first_event_ts, format_local_time, format_run_timing_lines, run_timing,
)

__all__ = ["ui"]

T0 = "2026-01-01T00:00:00Z"
T0_END = "2026-01-01T01:00:00Z"      # episode 1: 3600 s
T1 = "2026-01-01T02:00:00Z"          # resume after a 1 h gap
T1_END = "2026-01-01T03:00:00Z"      # episode 2: 3600 s


def _two_episode_status(**over) -> dict:
    return {"state": "succeeded", "startedAt": T1, "endedAt": T1_END,
            "firstStartedAt": T0, "pausedTotalS": 600.0,
            "previousEndings": [{"startedAt": T0, "endedAt": T0_END,
                                 "reason": "stopped", "verdict": None}],
            **over}


def _lines(timing: dict) -> dict[str, str]:
    return dict(format_run_timing_lines(timing))


# ------------------------------------------------------------- the helper
def test_active_is_summed_episode_wall_clock_minus_paused_total(tmp_path):
    t = run_timing(_two_episode_status(), tmp_path, end_ts=T1_END)
    assert t["startedAt"] == T0 and t["resumedAt"] == T1
    assert t["episodes"] == 2
    assert t["wallSeconds"] == 3 * 3600          # original start .. endedAt
    assert t["activeSeconds"] == 2 * 3600 - 600  # two episodes, one pause


def test_active_is_omitted_when_one_episode_start_is_missing(tmp_path):
    status = _two_episode_status()
    del status["previousEndings"][0]["startedAt"]
    t = run_timing(status, tmp_path, end_ts=T1_END)
    assert "activeSeconds" not in t
    assert t["wallSeconds"] == 3 * 3600
    assert "active" not in _lines(t)
    assert "wall" in _lines(t)


def test_original_start_falls_back_to_the_first_events_line_only(tmp_path):
    first = {"id": 1, "ts": "2025-12-31T23:00:00Z", "type": "budget_seeded"}
    later = {"id": 2, "ts": "2026-01-01T00:00:00Z", "type": "x"}
    (tmp_path / "events.jsonl").write_text(
        json.dumps(first) + "\n" + json.dumps(later) + "\n" + "{not json\n")
    assert first_event_ts(tmp_path) == "2025-12-31T23:00:00Z"
    status = {"state": "succeeded", "startedAt": T0, "endedAt": T0_END}
    t = run_timing(status, tmp_path, end_ts=T0_END)       # pre-0.32: no firstStartedAt
    assert t["startedAt"] == "2025-12-31T23:00:00Z"
    assert t["wallSeconds"] == 2 * 3600
    # a recorded firstStartedAt wins over the events file
    t = run_timing({**status, "firstStartedAt": T0}, tmp_path, end_ts=T0_END)
    assert t["startedAt"] == T0


def test_events_fallback_reads_a_single_line_never_the_whole_file(tmp_path):
    # a second line that is not even valid UTF-8 / JSON must never be read
    (tmp_path / "events.jsonl").write_bytes(
        json.dumps({"ts": T0}).encode() + b"\n" + b"\xff\xfe" * 1_000_000)
    assert first_event_ts(tmp_path) == T0


def test_single_episode_run_has_no_resumed_line(tmp_path):
    status = {"state": "succeeded", "startedAt": T0, "firstStartedAt": T0,
              "endedAt": T0_END, "previousEndings": []}
    t = run_timing(status, tmp_path, end_ts=T0_END)
    assert t["episodes"] == 1 and "resumedAt" not in t
    assert [k for k, _ in format_run_timing_lines(t)] == ["started", "wall", "active"]
    assert t["activeSeconds"] == t["wallSeconds"] == 3600


def test_run_without_any_start_yields_no_lines(tmp_path):
    assert format_run_timing_lines(run_timing({}, tmp_path)) == []


# --------------------------------------------------------------- the hub
def _detail_lines(ui_factory, tmp_path, run_id, **status):
    registry = tmp_path / "registry"
    _write_dead_run(registry, run_id, **status)
    server: UiServer = ui_factory(registry)
    code, detail = server.get(f"/api/runs/{run_id}")
    assert code == 200, detail
    return detail["status"]


def test_hub_header_shows_resumed_only_for_a_multi_episode_run(tmp_path, ui):
    single = _detail_lines(ui, tmp_path, "single", state="succeeded",
                           startedAt=T0, firstStartedAt=T0, endedAt=T0_END)
    rows = dict(single["timingLines"])
    assert "resumed" not in rows
    assert rows["started"] == format_local_time(T0)
    assert rows["wall"] == "1h" and rows["active"] == "1h"

    multi = _detail_lines(ui, tmp_path, "multi", **_two_episode_status())
    rows = dict(multi["timingLines"])
    assert rows["started"] == format_local_time(T0)
    assert rows["resumed"] == format_local_time(T1)
    assert rows["wall"] == "3h"
    assert rows["active"] == "1h 50m"
    # raw wire fields stay untouched beside the server-rendered rows
    assert multi["startedAt"] == T1 and multi["firstStartedAt"] == T0


# ------------------------------------------------------------------ the CLI
@pytest.fixture(autouse=True)
def _capture_cli_output(capsys):
    _dead_run_module._CAPSYS = capsys
    yield
    _dead_run_module._CAPSYS = None


def _status_out(tmp_path, run_id, **status) -> str:
    registry = tmp_path / "registry"
    _write_dead_run(registry, run_id, **status)
    res = _ctl(registry, "status", run_id)
    assert res.returncode == 0, (res.stdout, res.stderr)
    return res.stdout


def test_status_prints_started_resumed_wall_and_active(tmp_path):
    out = _status_out(tmp_path, "cli-multi", **_two_episode_status())
    lines = {ln.partition(":")[0].strip(): ln.partition(":")[2].strip()
             for ln in out.splitlines() if ":" in ln}
    assert lines["started"] == format_local_time(T0), out
    assert lines["resumed"] == format_local_time(T1), out
    assert lines["wall"] == "3h", out
    assert lines["active"] == "1h 50m", out


def test_status_of_a_single_episode_run_prints_no_resumed_line(tmp_path):
    out = _status_out(tmp_path, "cli-single", state="succeeded",
                      startedAt=T0, firstStartedAt=T0, endedAt=T0_END)
    keys = [ln.partition(":")[0].strip() for ln in out.splitlines()]
    assert "started" in keys and "wall" in keys and "active" in keys
    assert "resumed" not in keys, out


def test_runs_columns_are_unchanged(tmp_path):
    registry = tmp_path / "registry"
    _write_dead_run(registry, "cols", **_two_episode_status())
    res = _ctl(registry, "runs")
    assert res.returncode == 0, (res.stdout, res.stderr)
    header = res.stdout.splitlines()[0].split()
    assert header[-1] == "STARTED"
    for word in ("WALL", "ACTIVE", "RESUMED"):
        assert word not in header
