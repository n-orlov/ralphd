"""Task 007 (#16): `ralphctl status` and `ralphctl runs` render the approach
counter as `n/m`.

#16's complaint: both surfaces printed a bare approach number, so `approach 2`
told the operator nothing about how much of the review ladder is left -- and
for a run that had not entered the ladder at all they printed the literal
string `None`. The denominator is `maxApproaches` from status.json (task 006),
which means three renderings, tested here on all three:

- approach 2 + maxApproaches 3            -> `2/3`
- approach 2, no maxApproaches (pre-v0.6) -> `2` bare, never `2/?`, and never
  the *live* config's limit guessed in
- no approach at all                      -> empty, never `/3`

Tiers: a unit tier on the one shared formatter
(`ralphd.engine.state.format_approach`, which the hub also renders through)
plus a black-box tier running the real `ralphctl` executable over on-disk
run-dir fixtures with no container at all (stub-docker `Ctl` harness), because
the rendering an operator sees on a dead run is exactly what #16 is about.
"""

from __future__ import annotations

import json

import pytest
from test_cli_docker import Ctl, ctl, unix_sock
from test_cli_resume import _seed_run
from test_cli_ui import StubEngineApi, _write_dead_run, _write_run_with_api, ui

from ralphd.cli.main import RUN_SORT_KEYS, sort_run_rows
from ralphd.engine.state import format_approach

__all__ = ["ctl", "ui", "unix_sock"]


# --------------------------------------------------------------------------
# unit tier: the one shared formatter
# --------------------------------------------------------------------------

@pytest.mark.parametrize("approach,max_approaches,expected", [
    (2, 3, "2/3"),
    (1, 1, "1/1"),
    (2, None, "2"),
    (2, "", "2"),
    (None, 3, ""),
    ("", 3, ""),
    (None, None, ""),
])
def test_format_approach_renderings(approach, max_approaches, expected):
    assert format_approach(approach, max_approaches) == expected


def test_format_approach_never_invents_a_denominator():
    """The formatter takes the limit as an argument: there is no code path in
    which it can read a live config and present it as this run's limit."""
    assert "/" not in format_approach(2, None)


def test_format_approach_does_not_raise_on_junk():
    assert format_approach("two", "three") == "two/three"


# --------------------------------------------------------------------------
# black-box tier: ralphctl status
# --------------------------------------------------------------------------

_BASE_STATUS = {
    "state": "failed",
    "verdict": "unverified",
    "phase": "worker",
    "iterationsUsed": 7,
    "iterationsBudget": 250,
    "startedAt": "2024-01-01T00:00:00Z",
    "schemaVersion": 1,
}


def _seed_status(ctl: Ctl, run_id: str, **status_over) -> None:
    """A run dir with NO container record, so `ralphctl` takes its on-disk
    status.json path (no live API, nothing to inspect)."""
    rdir, _cdir = _seed_run(ctl, run_id)
    (rdir / "host.json").unlink()
    doc = {**_BASE_STATUS, "runId": run_id, **status_over}
    (rdir / "status.json").write_text(json.dumps(doc))


def _phase_line(stdout: str) -> str:
    for line in stdout.splitlines():
        if line.startswith("phase:"):
            return line
    raise AssertionError(f"no phase line in output:\n{stdout}")


def test_status_renders_approach_over_max(ctl):
    _seed_status(ctl, "with-max", approach=2, maxApproaches=3)
    res = ctl.run("status", "with-max")
    assert res.returncode == 0, res.stderr
    assert _phase_line(res.stdout).split() == ["phase:", "worker",
                                              "approach", "2/3"]


def test_status_renders_bare_approach_without_max(ctl):
    """A pre-v0.6 run dir: the limit is genuinely unknown, so `2` -- not
    `2/1` borrowed from the seeded job.yaml's `max_approaches: 1`."""
    _seed_status(ctl, "no-max", approach=2)
    res = ctl.run("status", "no-max")
    assert res.returncode == 0, res.stderr
    line = _phase_line(res.stdout)
    assert line.split() == ["phase:", "worker", "approach", "2"]
    assert "/" not in line


def test_status_omits_the_approach_segment_with_no_approach(ctl):
    _seed_status(ctl, "no-approach", approach=None, maxApproaches=3,
                 phase="planning")
    res = ctl.run("status", "no-approach")
    assert res.returncode == 0, res.stderr
    line = _phase_line(res.stdout)
    assert line.split() == ["phase:", "planning"]
    assert "/3" not in line
    assert "None" not in line


def test_status_json_carries_both_numbers(ctl):
    _seed_status(ctl, "json-both", approach=2, maxApproaches=3)
    doc = json.loads(ctl.run("--json", "status", "json-both").stdout)
    assert doc["approach"] == 2
    assert doc["maxApproaches"] == 3


def test_status_json_says_null_for_an_unknown_limit(ctl):
    """Absent is published as an explicit null (the api.py contract), so a
    consumer can tell "no limit recorded" from "key I forgot to read"."""
    _seed_status(ctl, "json-null", approach=2)
    doc = json.loads(ctl.run("--json", "status", "json-null").stdout)
    assert doc["approach"] == 2
    assert doc["maxApproaches"] is None


# --------------------------------------------------------------------------
# black-box tier: ralphctl runs
# --------------------------------------------------------------------------

# runId          approach  maxApproaches
_RUNS_FIXTURE = [
    ("aaa-with-max", 2, 3),
    ("bbb-no-max", 2, None),
    ("ccc-no-approach", None, 3),
]


def _seed_runs(ctl: Ctl) -> None:
    for run_id, approach, max_approaches in _RUNS_FIXTURE:
        over = {"approach": approach}
        if max_approaches is not None:
            over["maxApproaches"] = max_approaches
        _seed_status(ctl, run_id, **over)


def _approach_cells(stdout: str) -> dict:
    lines = [ln for ln in stdout.splitlines() if ln.strip()]
    header = lines[0]
    start = header.index("APPROACH")
    end = header.index("ITER")
    cells = {}
    for line in lines[1:]:
        cells[line.split()[0]] = line[start:end].strip()
    return cells


def test_runs_renders_all_three_approach_cells(ctl):
    _seed_runs(ctl)
    res = ctl.run("runs")
    assert res.returncode == 0, res.stderr
    assert _approach_cells(res.stdout) == {
        "aaa-with-max": "2/3",
        "bbb-no-max": "2",
        "ccc-no-approach": "",
    }
    assert "None" not in res.stdout


def test_runs_json_carries_both_numbers(ctl):
    _seed_runs(ctl)
    rows = {r["runId"]: r for r in json.loads(ctl.run("--json", "runs").stdout)}
    assert (rows["aaa-with-max"]["approach"],
            rows["aaa-with-max"]["maxApproaches"]) == (2, 3)
    assert (rows["bbb-no-max"]["approach"],
            rows["bbb-no-max"]["maxApproaches"]) == (2, None)
    assert (rows["ccc-no-approach"]["approach"],
            rows["ccc-no-approach"]["maxApproaches"]) == (None, 3)


def test_runs_sorts_on_the_raw_number_not_the_rendered_string(ctl):
    """`10/3` must not sort before `2/3`: the sort key stays the raw number,
    exactly like `iterationsUsed` vs the rendered "7/250" cell -- and the
    missing-value placement (task 055's `_cmp_run_values`) is unchanged by
    this task."""
    rows = [{"runId": "a", "approach": 2, "maxApproaches": 3},
            {"runId": "b", "approach": 10, "maxApproaches": 3},
            {"runId": "c", "approach": None, "maxApproaches": 3}]
    assert RUN_SORT_KEYS["approach"](rows[1]) == 10
    ordered = [r["runId"] for r in sort_run_rows(rows, "approach")]
    # biggest first (approach is a DESC key); a string sort of the rendered
    # cells would put "10/3" before "2/3".
    assert ordered.index("b") < ordered.index("a")
    assert ordered == ["c", "b", "a"]


def test_status_and_runs_agree_for_the_same_run(ctl):
    _seed_runs(ctl)
    cells = _approach_cells(ctl.run("runs").stdout)
    for run_id, approach, max_approaches in _RUNS_FIXTURE:
        doc = json.loads(ctl.run("--json", "status", run_id).stdout)
        assert cells[run_id] == format_approach(doc["approach"],
                                                doc["maxApproaches"])


# --------------------------------------------------------------------------
# hub tier (task 008, #16): the same three renderings in the hub payloads
# --------------------------------------------------------------------------
#
# `app.js` renders the *string* the server formatted (`approachDisplay`,
# ui_server._with_approach_display -> the `format_approach` above), which is
# why these black-box hub tests belong next to the CLI ones: they assert the
# two surfaces cannot drift. The browser tier
# (tests/test_browser_hub.py::test_run_list_and_detail_render_the_approach_
# denominator) asserts the rendered cells and that the column still sorts
# numerically.

def _hub_rows(server) -> dict:
    code, body = server.get("/api/runs")
    assert code == 200, body
    return {r["runId"]: r for r in body["runs"]}


def _seed_hub_runs(registry) -> None:
    for run_id, approach, max_approaches in _RUNS_FIXTURE:
        over = {"approach": approach}
        if max_approaches is not None:
            over["maxApproaches"] = max_approaches
        _write_dead_run(registry, run_id, **over)


def test_hub_run_list_rows_carry_the_rendered_counter(tmp_path, ui):
    registry = tmp_path / "registry"
    _seed_hub_runs(registry)
    rows = _hub_rows(ui(registry))
    assert {k: v["approachDisplay"] for k, v in rows.items()} == {
        "aaa-with-max": "2/3",
        "bbb-no-max": "2",
        "ccc-no-approach": "",
    }
    # the raw numbers travel untouched alongside the display string: the hub
    # sorts its APPROACH column on `approach`, not on "10/12"
    assert (rows["aaa-with-max"]["approach"],
            rows["aaa-with-max"]["maxApproaches"]) == (2, 3)
    assert rows["bbb-no-max"]["maxApproaches"] is None
    assert rows["ccc-no-approach"]["approach"] is None


def test_hub_run_list_never_invents_a_denominator(tmp_path, ui):
    registry = tmp_path / "registry"
    _seed_hub_runs(registry)
    rows = _hub_rows(ui(registry))
    assert "/" not in rows["bbb-no-max"]["approachDisplay"]
    assert "/" not in rows["ccc-no-approach"]["approachDisplay"]


def test_hub_run_detail_carries_the_rendered_counter(tmp_path, ui):
    registry = tmp_path / "registry"
    _seed_hub_runs(registry)
    server = ui(registry)
    for run_id, approach, max_approaches in _RUNS_FIXTURE:
        code, body = server.get(f"/api/runs/{run_id}")
        assert code == 200, body
        assert body["live"] is False, "on-disk snapshot, container gone"
        assert body["status"]["approachDisplay"] == format_approach(
            approach, max_approaches)


def test_hub_and_cli_render_the_same_string_for_the_same_run(tmp_path, ui):
    """One formatter, two surfaces: whatever `ralphctl runs`/`status` print,
    the hub payload says -- computed from the run's own recorded numbers."""
    registry = tmp_path / "registry"
    _seed_hub_runs(registry)
    rows = _hub_rows(ui(registry))
    for row in rows.values():
        assert row["approachDisplay"] == format_approach(row["approach"],
                                                        row["maxApproaches"])


def test_hub_run_detail_recomputes_a_forged_display(tmp_path, ui):
    """A status doc that carries its own `approachDisplay` cannot claim a
    ladder position its counter fields do not support -- the field is always
    recomputed from `approach`/`maxApproaches` (the discipline task 005 used
    for `tasksLabel`/`tasksNotice`)."""
    registry = tmp_path / "registry"
    _write_dead_run(registry, "forged", approach=2, maxApproaches=3,
                    approachDisplay="99/99")
    code, body = ui(registry).get("/api/runs/forged")
    assert code == 200, body
    assert body["status"]["approachDisplay"] == "2/3"


def test_hub_does_not_guess_a_limit_for_a_pre_v06_live_engine(tmp_path, ui):
    """A live `GET /status` from an engine that predates `maxApproaches`
    (task 006) is proxied verbatim; the hub renders `2` bare rather than
    borrowing a denominator from anywhere."""
    engine = StubEngineApi(status={"state": "running", "phase": "worker",
                                   "approach": 2})
    registry = tmp_path / "registry"
    try:
        _write_run_with_api(registry, "live-old", engine)
        code, body = ui(registry).get("/api/runs/live-old")
    finally:
        engine.close()
    assert code == 200, body
    assert body["live"] is True
    assert body["status"]["approachDisplay"] == "2"
    assert "/" not in body["status"]["approachDisplay"]
