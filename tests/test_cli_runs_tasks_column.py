"""Task 015 (#21): `ralphctl runs` gains the hub's TASKS column, from the
same counts.

Task 013/014 gave the hub run list a `5/7` TASKS column with trouble flags;
the CLI list still showed state/verdict/phase/approach/iterations only, so an
operator on a terminal had to open every run to learn how far its plan got --
and the two surfaces could have drifted the moment they each rendered their
own counts.

They cannot: the row itself is built ONCE, by `TasksRead.row_fields`
(engine/state.py, beside task 002's hardened reader and the shared
formatters), and both `cli/ui_server.py:_row_tasks` and `cli/main.py:cmd_runs`
just spread it into their row. The terminal column is that row flattened to
one string by `format_task_column` -- fraction, `\u26a0` when a task is
validation-failed/in-progress, `stale` when the fraction came from the
last-good payload -- with the flag SENTENCES kept in `--json`'s `tasksTrouble`
and in `ralphctl status`' summary rather than abbreviated into a private
wording here.

Tiers: unit on the flattening formatter and on the shared row builder;
black-box `ralphctl` over on-disk run dirs with no container (the case #21 is
about); an in-process probe of `cmd_runs` for "one hardened read per LISTED
row, `persist=False`, nothing written"; and two agreement tests -- hub row vs
`ralphctl runs` vs `ralphctl status` -- one on a snapshot with the container
gone, one against a real live engine.
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

import pytest
from test_cli_docker import Ctl, ctl, unix_sock
from test_cli_ui import StubEngineApi, UiServer, ui

from ralphd.cli import main as cli_main
from ralphd.cli import ui_server
from ralphd.engine.state import (
    TASK_TROUBLE_MARKER,
    TASKS_LAST_GOOD_NAME,
    TASKS_STALE_LABEL,
    format_task_column,
    format_task_counts,
    format_task_fraction,
    read_tasks_doc,
    task_counts,
)

__all__ = ["StubEngineApi", "UiServer", "ctl", "ui", "unix_sock"]

APP_JS = Path(ui_server.STATIC_DIR) / "app.js"

# 7 tasks, 5 done, one in-progress, one validation-failed -- the shape the PRD
# names (`5/7` with trouble to flag), same fixture as the hub's tests.
MID_PLAN = {"version": 1, "goal": "ship it", "tasks": (
    [{"id": f"{i:03d}", "status": "completed"} for i in range(5)]
    + [{"id": "005", "status": "in-progress"},
       {"id": "006", "status": "validation-failed"}])}
CLEAN_PLAN = {"version": 1, "tasks": [{"id": "001", "status": "completed"},
                                      {"id": "002", "status": "completed"}]}
BIG_PLAN = {"version": 1, "tasks": (
    [{"id": f"{i:03d}", "status": "completed"} for i in range(100)]
    + [{"id": f"p{i:03d}", "status": "pending"} for i in range(150)])}
TRUNCATED = '{"version": 1, "goal": "ship it", "tasks": [{"id": "001", "sta'


# --------------------------------------------------------------------------
# unit tier: the flattened column string
# --------------------------------------------------------------------------

@pytest.mark.parametrize("counts,stale,expected", [
    (task_counts(MID_PLAN["tasks"]), False, "5/7 \u26a0"),
    (task_counts(CLEAN_PLAN["tasks"]), False, "2/2"),
    ({"total": 7, "completed": 7}, False, "7/7"),
    ({"total": 4, "completed": 4}, True, "4/4 " + TASKS_STALE_LABEL),
    (task_counts(MID_PLAN["tasks"]), True, "5/7 \u26a0 " + TASKS_STALE_LABEL),
    # no plan -> no cell at all, and therefore no marker to misread
    ({}, False, ""),
    ({"total": 0}, False, ""),
    ({}, True, ""),
    ({"total": 0, "inProgress": 0}, False, ""),
    ({"total": "seven"}, False, ""),                      # junk degrades
    (None, False, ""),
])
def test_format_task_column_renderings(counts, stale, expected):
    assert format_task_column(counts, stale=stale) == expected


def test_the_column_never_renders_zero_over_zero():
    """`format_task_fraction`'s rule, inherited: a run whose agent has not
    written a plan yet has no denominator, and an unreadable `tasks.json` is
    ignorance rather than a plan of zero tasks."""
    for counts in ({}, {"total": 0, "completed": 0}, {"total": None}):
        for stale in (False, True):
            assert format_task_column(counts, stale=stale) == ""


def test_the_column_starts_with_the_shared_fraction():
    """One fraction formatter for `ralphctl runs`, `ralphctl status` and the
    hub -- the flattening only ever APPENDS markers."""
    counts = task_counts(MID_PLAN["tasks"])
    fraction = format_task_fraction(counts)
    assert format_task_column(counts).split(" ")[0] == fraction == "5/7"
    assert fraction in format_task_counts(counts)


def test_the_trouble_marker_is_the_glyph_the_hub_cell_appends():
    """`app.js taskCell` marks a troubled plan with `\\u26A0`; the CLI column
    marks it with `TASK_TROUBLE_MARKER`. One vocabulary, so this test fails if
    either side starts using its own glyph."""
    assert TASK_TROUBLE_MARKER == "\u26a0"
    js = APP_JS.read_text()
    assert re.search(r"\\u26A0", js, re.IGNORECASE), (
        "app.js no longer appends the shared trouble marker")


def test_the_marker_is_only_for_the_two_trouble_statuses():
    """Pending work is not trouble: a plan with 5 pending tasks is a plan in
    progress, and marking it would make the marker meaningless."""
    assert format_task_column({"total": 7, "completed": 2, "pending": 5}) == "2/7"
    assert format_task_column({"total": 7, "completed": 6,
                               "inProgress": 1}) == "6/7 \u26a0"
    assert format_task_column({"total": 7, "completed": 6,
                               "validationFailed": 1}) == "6/7 \u26a0"


# --------------------------------------------------------------------------
# unit tier: ONE row builder behind both list surfaces
# --------------------------------------------------------------------------

def _seed_run_dir(registry: Path, run_id: str, *, tasks_text: str | None = None,
                  last_good: dict | None = None, **status_over) -> Path:
    run_dir = registry / "runs" / run_id
    run_dir.mkdir(parents=True)
    status = {"runId": run_id, "state": "succeeded", "verdict": "verified",
              "phase": "worker", "approach": 1, "maxApproaches": 3,
              "iterationsUsed": 3, "iterationsBudget": 25,
              "startedAt": "2026-01-01T00:00:00Z", **status_over}
    (run_dir / "status.json").write_text(json.dumps(status))
    if tasks_text is not None:
        (run_dir / "tasks.json").write_text(tasks_text)
    if last_good is not None:
        (run_dir / TASKS_LAST_GOOD_NAME).write_text(json.dumps(last_good))
    return run_dir


def test_hub_row_fields_come_from_the_shared_builder(tmp_path):
    """`ui_server._row_tasks` is now the shared builder plus a read: whatever
    the hub row says about a plan, `TasksRead.row_fields` said."""
    run_dir = _seed_run_dir(tmp_path / "reg", "shared",
                            tasks_text=json.dumps(MID_PLAN))
    assert ui_server._row_tasks(run_dir) == read_tasks_doc(
        run_dir, persist=False).row_fields


def test_the_builder_carries_raw_counts_rendered_strings_and_the_contract(tmp_path):
    run_dir = _seed_run_dir(tmp_path / "reg", "fields",
                            tasks_text=json.dumps(MID_PLAN))
    fields = read_tasks_doc(run_dir, persist=False).row_fields
    assert fields == {
        "tasksTotal": 7, "tasksCompleted": 5, "tasksInProgress": 1,
        "tasksValidationFailed": 1,
        # Task 025 (#33) RETARGETED this exact-dict assertion: a row now also
        # carries the terminal-failure counts and both meanings of them, so a
        # consumer reading the row alone can tell a plan that finished from one
        # that gave up (and on which of the two grounds). Zeros here, because
        # MID_PLAN has no failed task -- every rendered string below is
        # byte-identical to before.
        "tasksFailed": 0,
        "tasksFailedValidationExhausted": 0,
        "tasksFailedRequirementUnmet": 0,
        "tasksDisplay": "5/7",
        "tasksSummary": "5/7 completed (1 in-progress, 1 validation-failed)",
        "tasksTrouble": ["1 validation-failed", "1 in-progress"],
        "tasksColumn": "5/7 \u26a0",
        "tasksStale": False, "tasksSource": "file",
    }


def test_cli_and_hub_rows_carry_the_identical_task_fields(tmp_path):
    """The two list surfaces read the same run dir independently; every
    `tasks*` field must match key for key (this is what makes the black-box
    agreement tests below a check on the rendering, not on the data)."""
    reg = tmp_path / "reg"
    for run_id, tasks_text, last_good in (
            ("mid", json.dumps(MID_PLAN), None),
            ("clean", json.dumps(CLEAN_PLAN), None),
            ("planless", None, None),
            ("stale", TRUNCATED, CLEAN_PLAN),
            ("unreadable", TRUNCATED, None)):
        run_dir = _seed_run_dir(reg, run_id, tasks_text=tasks_text,
                                last_good=last_good)
        hub_row = ui_server._row_tasks(run_dir)
        cli_row = read_tasks_doc(run_dir, persist=False).row_fields
        assert hub_row == cli_row, run_id


# --------------------------------------------------------------------------
# in-process: one hardened read per LISTED row, persist=False, no writes
# --------------------------------------------------------------------------

def _run_cmd_runs(monkeypatch, capsys, reg: Path, **args_over) -> list[dict]:
    monkeypatch.setenv("RALPHD_REGISTRY", str(reg))
    defaults = {"state": None, "json": True, "sort": None, "reverse": False}
    args = argparse.Namespace(**{**defaults, **args_over})
    cli_main.cmd_runs(args)
    return json.loads(capsys.readouterr().out)


def test_cmd_runs_reads_each_plan_once_through_the_hardened_reader(
        tmp_path, monkeypatch, capsys):
    reg = tmp_path / "reg"
    for run_id in ("run-a", "run-b", "run-c"):
        _seed_run_dir(reg, run_id, tasks_text=json.dumps(MID_PLAN))

    calls: list[tuple[str, bool]] = []
    real = cli_main.read_tasks_doc

    def spy(run_root, **kw):
        calls.append((str(run_root), kw.get("persist")))
        return real(run_root, **kw)

    monkeypatch.setattr(cli_main, "read_tasks_doc", spy)
    rows = _run_cmd_runs(monkeypatch, capsys, reg)

    assert [r["tasksDisplay"] for r in rows] == ["5/7", "5/7", "5/7"]
    assert len(calls) == 3, calls                    # exactly one per row
    assert len({c[0] for c in calls}) == 3           # a different dir each
    # `persist=False`: `ralphctl runs` is a viewer of somebody else's run dir
    # (possibly a LIVE run's) and must never leave a last-good cache behind.
    assert {c[1] for c in calls} == {False}


def test_cmd_runs_does_not_read_the_plan_of_a_filtered_out_run(
        tmp_path, monkeypatch, capsys):
    """`--state` filters before the read, so listing one running run out of a
    hundred finished ones costs one read, not a hundred."""
    reg = tmp_path / "reg"
    _seed_run_dir(reg, "kept", state="running", tasks_text=json.dumps(MID_PLAN))
    _seed_run_dir(reg, "dropped", state="succeeded",
                  tasks_text=json.dumps(MID_PLAN))

    calls: list[str] = []
    real = cli_main.read_tasks_doc
    monkeypatch.setattr(cli_main, "read_tasks_doc",
                        lambda root, **kw: (calls.append(str(root)),
                                            real(root, **kw))[1])
    rows = _run_cmd_runs(monkeypatch, capsys, reg, state="running")
    assert [r["runId"] for r in rows] == ["kept"]
    assert [Path(c).name for c in calls] == ["kept"]


def test_cmd_runs_writes_nothing_into_the_run_dir(tmp_path, monkeypatch, capsys):
    reg = tmp_path / "reg"
    run_dir = _seed_run_dir(reg, "mid-write", tasks_text=TRUNCATED,
                            last_good=CLEAN_PLAN)
    before = {p.name: p.stat().st_mtime_ns for p in run_dir.iterdir()}
    rows = _run_cmd_runs(monkeypatch, capsys, reg)
    assert rows[0]["tasksColumn"] == "2/2 " + TASKS_STALE_LABEL
    assert {p.name: p.stat().st_mtime_ns for p in run_dir.iterdir()} == before


# --------------------------------------------------------------------------
# black-box tier: the rendered column, no container anywhere
# --------------------------------------------------------------------------

# runId              tasks.json               last-good      expected cell
_RUNS_FIXTURE = [
    ("aaa-mid", json.dumps(MID_PLAN), None, "5/7 \u26a0"),
    ("bbb-clean", json.dumps(CLEAN_PLAN), None, "2/2"),
    ("ccc-planless", None, None, ""),
    ("ddd-stale", TRUNCATED, CLEAN_PLAN, "2/2 " + TASKS_STALE_LABEL),
    ("eee-unreadable", TRUNCATED, None, ""),
]


def _seed_ctl_runs(ctl: Ctl) -> None:
    for run_id, tasks_text, last_good, _cell in _RUNS_FIXTURE:
        _seed_run_dir(ctl.registry, run_id, tasks_text=tasks_text,
                      last_good=last_good)


def _cells(stdout: str, column: str, next_column: str) -> dict:
    lines = [ln for ln in stdout.splitlines() if ln.strip()]
    header = lines[0]
    start, end = header.index(column), header.index(next_column)
    return {ln.split()[0]: ln[start:end].strip() for ln in lines[1:]}


def test_runs_header_has_a_tasks_column_between_approach_and_iterations(ctl):
    """The hub's column order (task 014), so the two lists read the same."""
    _seed_ctl_runs(ctl)
    res = ctl.run("runs")
    assert res.returncode == 0, res.stderr
    header = res.stdout.splitlines()[0]
    assert header.index("APPROACH") < header.index("TASKS") < header.index("ITER")


def test_runs_renders_every_task_cell(ctl):
    _seed_ctl_runs(ctl)
    res = ctl.run("runs")
    assert res.returncode == 0, res.stderr
    assert _cells(res.stdout, "TASKS", "ITER") == {
        run_id: cell for run_id, _t, _lg, cell in _RUNS_FIXTURE}
    # never a fabricated denominator, and the iterations column is untouched
    assert "0/0" not in res.stdout
    assert _cells(res.stdout, "ITER", "STARTED")["aaa-mid"] == "3/25"


def test_runs_json_carries_the_raw_counts_and_the_flag_wording(ctl):
    _seed_ctl_runs(ctl)
    rows = {r["runId"]: r for r in json.loads(ctl.run("--json", "runs").stdout)}
    mid = rows["aaa-mid"]
    assert (mid["tasksTotal"], mid["tasksCompleted"]) == (7, 5)
    assert (mid["tasksInProgress"], mid["tasksValidationFailed"]) == (1, 1)
    # the sentences the column cannot fit, verbatim from the shared formatter
    assert mid["tasksTrouble"] == ["1 validation-failed", "1 in-progress"]
    assert mid["tasksSummary"] == format_task_counts(task_counts(MID_PLAN["tasks"]))
    assert (mid["tasksStale"], mid["tasksSource"]) == (False, "file")
    # task 002's contract travels with the row, so a consumer can tell a
    # last-good fraction from a fresh one without parsing the cell
    stale = rows["ddd-stale"]
    assert (stale["tasksStale"], stale["tasksSource"]) == (True, "last-good")
    assert (stale["tasksTotal"], stale["tasksCompleted"]) == (2, 2)
    # a plan-less run: explicit zeros with empty display strings, never `0/0`
    blank = rows["ccc-planless"]
    assert (blank["tasksTotal"], blank["tasksDisplay"],
            blank["tasksSummary"], blank["tasksColumn"]) == (0, "", "", "")
    assert blank["tasksSource"] == "absent"
    # ... and an unreadable plan is ignorance, not an empty plan
    assert rows["eee-unreadable"]["tasksSource"] == "unreadable"
    assert rows["eee-unreadable"]["tasksColumn"] == ""


def test_runs_sorts_on_the_completion_fraction_with_plan_less_runs_last(ctl):
    """The hub's dialect (task 014): `5/7` (0.71) outranks `100/250` (0.4),
    and a run with no plan has no fraction at all -- it sorts last ascending
    rather than pretending to be 0% done."""
    _seed_run_dir(ctl.registry, "aaa-mid", tasks_text=json.dumps(MID_PLAN))
    _seed_run_dir(ctl.registry, "bbb-big", tasks_text=json.dumps(BIG_PLAN))
    _seed_run_dir(ctl.registry, "ccc-clean", tasks_text=json.dumps(CLEAN_PLAN))
    _seed_run_dir(ctl.registry, "ddd-planless")

    res = ctl.run("runs", "--sort", "tasks")
    assert res.returncode == 0, res.stderr
    order = [ln.split()[0] for ln in res.stdout.splitlines()[1:] if ln.strip()]
    assert order == ["bbb-big", "aaa-mid", "ccc-clean", "ddd-planless"]

    rev = ctl.run("runs", "--sort", "tasks", "--reverse")
    assert rev.returncode == 0, rev.stderr
    rev_order = [ln.split()[0] for ln in rev.stdout.splitlines()[1:] if ln.strip()]
    # Descending flips the WHOLE comparison, missing values included -- the
    # hub's `sortRuns` does exactly the same (`cmpValues(...) * dir`), so the
    # plan-less run leads here rather than trailing. Asserted rather than
    # "fixed": the two surfaces agreeing is the property that matters.
    assert rev_order == ["ddd-planless", "ccc-clean", "aaa-mid", "bbb-big"]


# --------------------------------------------------------------------------
# agreement: hub row == `ralphctl runs` == `ralphctl status`
# --------------------------------------------------------------------------

def _hub_rows(server) -> dict:
    code, body = server.get("/api/runs")
    assert code == 200, body
    return {r["runId"]: r for r in body["runs"]}


def _assert_agrees(hub_row: dict, cli_row: dict, status: dict) -> None:
    counts = status.get("tasks") or {}
    for key in ("tasksTotal", "tasksCompleted", "tasksInProgress",
                "tasksValidationFailed", "tasksDisplay", "tasksSummary",
                "tasksTrouble", "tasksColumn", "tasksStale", "tasksSource"):
        assert hub_row[key] == cli_row[key], key
    # ... and both agree with what `ralphctl status` counted for the same run
    assert cli_row["tasksTotal"] == counts.get("total", 0)
    assert cli_row["tasksCompleted"] == counts.get("completed", 0)
    assert cli_row["tasksDisplay"] == format_task_fraction(counts)
    assert cli_row["tasksColumn"] == format_task_column(
        counts, stale=bool(status.get("tasksStale")))
    if cli_row["tasksDisplay"]:
        assert cli_row["tasksSummary"] == format_task_counts(counts)


def test_hub_runs_and_status_agree_with_the_container_gone(ctl, ui):
    """#21's case: a finished run whose container is long gone. All three
    surfaces read the same `tasks.json` with the same hardened reader."""
    _seed_ctl_runs(ctl)
    hub = _hub_rows(ui(ctl.registry))
    cli = {r["runId"]: r for r in json.loads(ctl.run("--json", "runs").stdout)}
    for run_id, _t, _lg, cell in _RUNS_FIXTURE:
        status = json.loads(ctl.run("--json", "status", run_id).stdout)
        assert status["live"] is False, run_id
        _assert_agrees(hub[run_id], cli[run_id], status)
        assert cli[run_id]["tasksColumn"] == cell
    # the human surfaces say it too, not only --json
    assert _cells(ctl.run("runs").stdout, "TASKS", "ITER")["aaa-mid"] == "5/7 \u26a0"
    text = ctl.run("status", "aaa-mid").stdout
    assert "tasks:     5/7 completed (1 in-progress, 1 validation-failed)" in text


def test_hub_runs_and_status_agree_for_a_live_run(live, ui):
    """The same three surfaces against a REAL engine that is still answering:
    `ralphctl status` takes its live `GET /status` path (counts synthesised by
    the engine from `tasks.json`) while `ralphctl runs` and the hub list read
    the file directly -- and they still say the same thing."""
    run = live(run_id="tasks-column-live", job={"iterations": 6},
               stub_env={"STUB_TASKS": "3"})
    run.wait_api()
    run.wait_terminal(timeout=180)

    status = json.loads(run.ralphctl("--json", "status", run.run_id).stdout)
    assert status["live"] is True, "engine should still be answering (idle)"
    counts = status["tasks"]
    assert counts["total"] == 3

    cli = {r["runId"]: r
           for r in json.loads(run.ralphctl("--json", "runs").stdout)}
    hub = _hub_rows(ui(run.registry))
    _assert_agrees(hub[run.run_id], cli[run.run_id], status)
    assert cli[run.run_id]["tasksDisplay"] == f"{counts['completed']}/3"


def test_a_live_run_keeps_the_columns_source_local(tmp_path, ui, ctl):
    """`ralphctl runs` must not start proxying: the fraction for a run with a
    live API comes off disk, exactly like the hub list's (task 013). Asserted
    against a stub engine that records every request it receives."""
    engine = StubEngineApi(status={"runId": "run-live", "state": "running"},
                           tasks={"tasks": [], "total": 0})
    try:
        run_dir = _seed_run_dir(ctl.registry, "run-live", state="running",
                               tasks_text=json.dumps(MID_PLAN))
        (run_dir / "host.json").write_text(json.dumps(
            {"runId": "run-live", "port": engine.port,
             "apiUrl": f"http://127.0.0.1:{engine.port}"}))
        rows = json.loads(ctl.run("--json", "runs").stdout)
        assert rows[0]["tasksDisplay"] == "5/7"     # disk, not the empty live plan
        assert engine.requests == []
        # control: `status` DOES go live for the same run, so the assertion
        # above is about `runs` specifically, not about a silent stub
        doc = json.loads(ctl.run("--json", "status", "run-live").stdout)
        assert doc["live"] is True
        assert [p for _m, p, _a in engine.requests] != []
    finally:
        engine.close()
