"""Task 013 (#21): the hub run list carries per-row task progress.

The run list showed state/phase/iterations but nothing about the plan the
agent is actually working through, so "is this run nearly done?" required
opening every run's detail page. The fix is deliberately *local*: one
hardened `read_tasks_doc` per row (task 002's reader) plus the engine's
`task_counts`, rendered server-side by the same formatters `ralphctl status`
uses -- no `GET /tasks` proxy call, so listing N runs still costs no round
trips and a finished run whose container is long gone shows its fraction just
like a live one.

Tiers: unit tests on the shared formatters (including that `ralphctl status`
now delegates to them, so the wording cannot drift), in-process probes of
`run_list` for the "exactly one local read per row" contract, and black-box
HTTP against a real `ralphctl ui` server for the payload -- with a live stub
engine API that must record ZERO requests while the list is rendered.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from test_cli_ui import StubEngineApi, UiServer, _write_run_with_api, ui

from ralphd.cli import main as cli_main
from ralphd.cli import ui_server
from ralphd.engine.state import (
    NO_TASKS,
    TASK_STATUS_LABELS,
    TASKS_LAST_GOOD_NAME,
    format_task_counts,
    format_task_fraction,
    format_task_trouble,
    task_counts,
)

__all__ = ["UiServer", "ui"]


# 7 tasks, 5 done, one in-progress, one validation-failed: the shape the PRD
# names (`5/7` with trouble to flag).
MID_PLAN = {
    "version": 1,
    "goal": "ship it",
    "tasks": [
        {"id": "001", "status": "completed"},
        {"id": "002", "status": "completed"},
        {"id": "003", "status": "completed"},
        {"id": "004", "status": "completed"},
        {"id": "005", "status": "completed"},
        {"id": "006", "status": "in-progress"},
        {"id": "007", "status": "validation-failed"},
    ],
}
TRUNCATED = '{"version": 1, "goal": "ship it", "tasks": [{"id": "001", "sta'


def _seed_hub_run(registry, run_id: str, *, tasks_text: str | None = None,
                  last_good: dict | None = None, state: str = "succeeded"):
    run_dir = registry / "runs" / run_id
    run_dir.mkdir(parents=True)
    (run_dir / "status.json").write_text(json.dumps(
        {"runId": run_id, "state": state, "phase": "worker", "approach": 1,
         "iterationsUsed": 3, "startedAt": "2026-01-01T00:00:00Z"}))
    if tasks_text is not None:
        (run_dir / "tasks.json").write_text(tasks_text)
    if last_good is not None:
        (run_dir / TASKS_LAST_GOOD_NAME).write_text(json.dumps(last_good))
    return run_dir


def _row(rows: list[dict], run_id: str) -> dict:
    return next(r for r in rows if r["runId"] == run_id)


# --------------------------------------------------------------------------
# the shared formatters (one vocabulary for CLI and hub)
# --------------------------------------------------------------------------

def test_fraction_renders_progress_mid_plan():
    assert format_task_fraction(task_counts(MID_PLAN["tasks"])) == "5/7"
    assert format_task_fraction({"total": 7, "completed": 7}) == "7/7"


@pytest.mark.parametrize("counts", [
    {},                                  # nothing read at all
    {"total": 0},                        # no plan / empty plan
    {"total": 0, "completed": 0},
    {"total": None},                     # junk
    {"total": "seven", "completed": 5},
])
def test_a_run_with_no_plan_gets_no_fraction_never_zero_over_zero(counts):
    """The whole point of a fraction is a denominator somebody stated. `0/0`
    reads as a plan of zero tasks, which is a claim about a plan that does not
    exist -- same discipline as `format_approach`'s empty answer."""
    assert format_task_fraction(counts) == ""


def test_trouble_flags_are_worded_exactly_as_the_status_summary_words_them():
    counts = task_counts(MID_PLAN["tasks"])
    trouble = format_task_trouble(counts)
    assert trouble == ["1 validation-failed", "1 in-progress"]
    summary = format_task_counts(counts)
    for flag in trouble:
        assert flag in summary, (flag, summary)


def test_no_trouble_flags_for_a_clean_plan():
    assert format_task_trouble({"total": 3, "completed": 3}) == []
    assert format_task_trouble({"total": 3, "completed": 2, "pending": 1}) == []
    assert format_task_trouble({"total": 1, "inProgress": 0}) == []
    assert format_task_trouble(None) == []


def test_ralphctl_status_delegates_to_the_shared_renderer():
    """`_summarize_tasks` is now a thin alias: one renderer, so the hub's
    TASKS column cannot word the same counts differently."""
    assert cli_main._TASK_STATUS_LABELS is TASK_STATUS_LABELS
    for counts in ({}, {"total": 7, "completed": 7},
                   task_counts(MID_PLAN["tasks"]),
                   {"total": 2, "completed": 1, "weird-status": 1}):
        assert cli_main._summarize_tasks(counts) == format_task_counts(counts)
    assert cli_main._summarize_tasks({}) == NO_TASKS == "(none)"
    assert format_task_counts(task_counts(MID_PLAN["tasks"])) == (
        "5/7 completed (1 in-progress, 1 validation-failed)")


# --------------------------------------------------------------------------
# run_list: one local hardened read per row, no live calls
# --------------------------------------------------------------------------

def test_run_list_reads_each_plan_exactly_once_through_the_hardened_reader(
        tmp_path, monkeypatch):
    reg = tmp_path / "reg"
    for run_id in ("run-a", "run-b", "run-c"):
        _seed_hub_run(reg, run_id, tasks_text=json.dumps(MID_PLAN))

    calls: list[tuple[str, bool]] = []
    real = ui_server.read_tasks_doc

    def spy(run_root, **kw):
        calls.append((str(run_root), kw.get("persist")))
        return real(run_root, **kw)

    monkeypatch.setattr(ui_server, "read_tasks_doc", spy)
    rows = ui_server.run_list(reg)

    assert [r["tasksDisplay"] for r in rows] == ["5/7", "5/7", "5/7"]
    assert len(calls) == 3, calls                      # exactly one per row
    assert len({c[0] for c in calls}) == 3             # ... and a different dir each
    # `persist=False`: the hub is a read-only viewer of somebody else's run
    # dir and must never leave a last-good cache behind.
    assert {c[1] for c in calls} == {False}


def test_run_list_writes_nothing_into_the_run_dir(tmp_path):
    reg = tmp_path / "reg"
    run_dir = _seed_hub_run(reg, "run-mid", tasks_text=TRUNCATED)
    (run_dir / TASKS_LAST_GOOD_NAME).write_text(json.dumps(MID_PLAN))
    before = {p.name: p.stat().st_mtime_ns for p in run_dir.iterdir()}
    rows = ui_server.run_list(reg)
    assert _row(rows, "run-mid")["tasksDisplay"] == "5/7"
    assert {p.name: p.stat().st_mtime_ns for p in run_dir.iterdir()} == before


def test_run_list_makes_no_http_call_while_rendering(tmp_path, ui):
    """The list's contract: local reads only. A run whose `host.json` points
    at a live API must be listed WITHOUT a single request reaching it (the
    only permitted contact is task 024's bare TCP liveness probe, which the
    stub never sees as a request) -- otherwise listing N runs costs N round
    trips. Asserted against a real stub API rather than by reading the code.
    """
    reg = tmp_path / "reg"
    engine = StubEngineApi(status={"runId": "run-live", "state": "running"},
                           tasks={"tasks": [], "total": 0})
    try:
        run_dir = _write_run_with_api(reg, "run-live", engine, state="running",
                                      verdict=None)
        (run_dir / "tasks.json").write_text(json.dumps(MID_PLAN))
        server = ui(reg)
        code, body = server.get("/api/runs")
        assert code == 200, body
        row = _row(body["runs"], "run-live")
        # the fraction came off disk, not from the live engine's (empty) plan
        assert row["tasksDisplay"] == "5/7"
        assert engine.requests == []
        # control: the DETAIL view does proxy, so the assertion above is about
        # the list specifically and not about a stub that records nothing.
        code, detail = server.get("/api/runs/run-live")
        assert code == 200, detail
        assert detail["live"] is True
        assert [p for _m, p, _a in engine.requests] != []
    finally:
        engine.close()


# --------------------------------------------------------------------------
# the payload a real `ralphctl ui` serves
# --------------------------------------------------------------------------

def test_run_list_row_carries_the_fraction_counts_and_summary(tmp_path, ui):
    reg = tmp_path / "reg"
    _seed_hub_run(reg, "run-mid", tasks_text=json.dumps(MID_PLAN))
    code, body = ui(reg).get("/api/runs")
    assert code == 200, body
    row = _row(body["runs"], "run-mid")
    assert row["tasksDisplay"] == "5/7"
    assert (row["tasksCompleted"], row["tasksTotal"]) == (5, 7)
    assert (row["tasksInProgress"], row["tasksValidationFailed"]) == (1, 1)
    assert row["tasksTrouble"] == ["1 validation-failed", "1 in-progress"]
    # the same sentence `ralphctl status` prints for the same file
    assert row["tasksSummary"] == cli_main._summarize_tasks(
        task_counts(MID_PLAN["tasks"]))
    assert row["tasksStale"] is False and row["tasksSource"] == "file"


def test_a_validation_failed_plan_is_flagged_even_with_nothing_in_progress(
        tmp_path, ui):
    reg = tmp_path / "reg"
    _seed_hub_run(reg, "run-stuck", tasks_text=json.dumps({"tasks": [
        {"id": "001", "status": "completed"},
        {"id": "002", "status": "validation-failed"},
        {"id": "003", "status": "pending"},
    ]}))
    code, body = ui(reg).get("/api/runs")
    assert code == 200, body
    row = _row(body["runs"], "run-stuck")
    assert row["tasksDisplay"] == "1/3"
    assert row["tasksValidationFailed"] == 1
    assert row["tasksInProgress"] == 0
    assert row["tasksTrouble"] == ["1 validation-failed"]
    assert "1 validation-failed" in row["tasksSummary"]


@pytest.mark.parametrize("tasks_text,source", [
    (None, "absent"),                      # agent has not planned yet
    (json.dumps({"tasks": []}), "file"),   # ... or planned an empty plan
    (json.dumps({"version": 1}), "file"),  # ... or a doc with no task list
])
def test_a_plan_less_run_gets_a_blank_column_not_zero_over_zero(
        tmp_path, ui, tasks_text, source):
    reg = tmp_path / "reg"
    _seed_hub_run(reg, "run-blank", tasks_text=tasks_text)
    code, body = ui(reg).get("/api/runs")
    assert code == 200, body
    row = _row(body["runs"], "run-blank")
    assert row["tasksDisplay"] == ""
    assert row["tasksSummary"] == ""       # not `0/0 completed`, not `(none)`
    assert row["tasksTotal"] == 0
    assert row["tasksTrouble"] == []
    assert row["tasksSource"] == source
    assert "0/0" not in json.dumps(row)


def test_a_mid_write_plan_keeps_its_fraction_flagged_stale(tmp_path, ui):
    """The reader's whole purpose, applied to the list: an agent rewriting
    `tasks.json` must not make the column blink blank for a poll cycle."""
    reg = tmp_path / "reg"
    _seed_hub_run(reg, "run-rewriting", tasks_text=TRUNCATED, last_good=MID_PLAN)
    code, body = ui(reg).get("/api/runs")
    assert code == 200, body
    row = _row(body["runs"], "run-rewriting")
    assert row["tasksDisplay"] == "5/7"
    assert row["tasksStale"] is True
    assert row["tasksSource"] == "last-good"


def test_an_unreadable_plan_with_no_last_good_shows_ignorance_not_zero(
        tmp_path, ui):
    reg = tmp_path / "reg"
    _seed_hub_run(reg, "run-lost", tasks_text=TRUNCATED)
    code, body = ui(reg).get("/api/runs")
    assert code == 200, body
    row = _row(body["runs"], "run-lost")
    assert row["tasksDisplay"] == ""
    assert row["tasksSummary"] == ""
    assert row["tasksStale"] is True and row["tasksSource"] == "unreadable"


def test_rows_of_several_runs_do_not_borrow_each_others_counts(tmp_path, ui):
    reg = tmp_path / "reg"
    _seed_hub_run(reg, "run-1", tasks_text=json.dumps(MID_PLAN))
    _seed_hub_run(reg, "run-2", tasks_text=json.dumps({"tasks": [
        {"id": "001", "status": "completed"}, {"id": "002", "status": "completed"}]}))
    _seed_hub_run(reg, "run-3")
    code, body = ui(reg).get("/api/runs")
    assert code == 200, body
    assert {r["runId"]: r["tasksDisplay"] for r in body["runs"]} == {
        "run-1": "5/7", "run-2": "2/2", "run-3": ""}


# --------------------------------------------------------------------------
# task 014 (#21): the browser half of the TASKS column. The rendering itself
# is asserted in a real Chromium by
# tests/test_browser_hub.py::test_run_list_tasks_column_renders_flags_and_sorts_on_progress;
# these are the cheap fast-lane guards on the contract that test depends on.
# --------------------------------------------------------------------------

APP_JS = (Path(cli_main.__file__).parent / "web" / "app.js").read_text()


def test_app_js_has_a_tasks_column_between_approach_and_iterations():
    block = APP_JS.split("const RUN_COLUMNS = [")[1].split("];")[0]
    labels = re.findall(r'label:\s*"([^"]+)"', block)
    assert labels == ["RUN", "STATE", "VERDICT", "PHASE", "APPROACH", "TASKS",
                      "ITERATIONS", "STARTED"], labels
    # the CELL renders the string the SERVER formatted, and the trouble flags
    # the server worded -- never a second JS spelling of either.
    assert "r.tasksDisplay" in APP_JS
    assert "r.tasksTrouble" in APP_JS
    body = APP_JS.split("function taskCell(")[1].split("\n}")[0]
    code = "\n".join(ln for ln in body.splitlines()
                     if not ln.strip().startswith("//"))
    assert "completed" not in code, code


def test_the_tasks_column_sorts_on_the_ratio_not_the_rendered_text():
    block = APP_JS.split("const RUN_COLUMNS = [")[1].split("];")[0]
    tasks_col = next(ln for ln in block.splitlines() if '"TASKS"' in ln)
    assert "taskRatio(r)" in tasks_col, tasks_col
    assert "tasksDisplay" not in tasks_col, tasks_col
    # ascending-first, so the plan-less runs (no ratio) land last
    desc = APP_JS.split("const desc = ")[1].split(";")[0]
    assert '"tasks"' not in desc, desc


@pytest.mark.parametrize("row,expected", [
    ({"tasksTotal": 7, "tasksCompleted": 5}, 5 / 7),
    ({"tasksTotal": 250, "tasksCompleted": 100}, 0.4),
    ({"tasksTotal": 2, "tasksCompleted": 2}, 1.0),
    ({"tasksTotal": 4}, 0.0),
    # no plan is not 0% done: no ratio at all, so it sorts last ascending
    ({"tasksTotal": 0, "tasksCompleted": 0}, None),
    ({}, None),
    ({"tasksTotal": "seven", "tasksCompleted": 5}, None),
])
def test_task_ratio_is_the_shared_sort_value(row, expected):
    assert cli_main._task_ratio(row) == expected


def test_the_ratio_orders_five_sevenths_above_a_hundred_of_two_fifty():
    rows = [{"runId": "big", "tasksTotal": 250, "tasksCompleted": 100},
            {"runId": "mid", "tasksTotal": 7, "tasksCompleted": 5},
            {"runId": "none"}]
    order = [r["runId"] for r in cli_main.sort_run_rows(rows, "tasks")]
    # ascending: least complete first, the plan-less run LAST (not first,
    # which is what treating "no plan" as 0 would do)
    assert order == ["big", "mid", "none"], order
