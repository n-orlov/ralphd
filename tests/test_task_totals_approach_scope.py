"""Task 001 (PRD I.1, #207/#29): `engine.state.task_totals` buckets per
`(approach, taskId)`, never per bare `taskId` alone -- the same defect #29
already fixed once (the vigilant-verified sidecar) recurring in this new
place: every approach's own planning pass renumbers `tasks.json` from
"001", so a flat `taskId`-only bucket lets a later approach's task "001"
inherit an earlier approach's minutes and dollars.

Pure in-process: real `iteration_detail()` reads off written `meta.json`
fixtures (the same `_base_meta`/`_write_iteration` helpers
`test_cli_iteration_detail.py` and `test_browser_hub.py` already use), no
playwright, no subprocess -- so this stays in the smoke tier.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from test_cli_iteration_detail import _base_meta, _write_iteration

from ralphd.cli.ui_server import _with_task_totals
from ralphd.engine.state import iteration_detail, task_totals, task_totals_zero
from ralphd.log_merge import iteration_numbers


def _iterations(run_dir: Path) -> list:
    return [iteration_detail(run_dir, n) for n in iteration_numbers(run_dir)]


def _fixture(tmp_path: Path) -> Path:
    """Two approaches' iterations both naming task "001" -- approach 1's
    iteration 1 and approach 2's iteration 2 -- with DIFFERENT duration and
    cost, so a correct per-(approach, taskId) bucketing can never mix them,
    while the pre-fix flat `taskId`-only bucketing summed them into one
    figure.
    """
    run_dir = tmp_path / "run"
    (run_dir / "iterations").mkdir(parents=True)
    meta1 = _base_meta(1, approach=1, assignedTaskId="001",
                       startedAt="2026-09-02T10:00:00Z",
                       endedAt="2026-09-02T10:01:00Z",
                       usage={"input": 10, "output": 20, "totalTokens": 30,
                              "costUSD": 0.01, "costPriced": True})
    meta2 = _base_meta(2, approach=2, assignedTaskId="001",
                       startedAt="2026-09-02T11:00:00Z",
                       endedAt="2026-09-02T11:05:00Z",
                       usage={"input": 15, "output": 25, "totalTokens": 40,
                              "costUSD": 0.02, "costPriced": True})
    _write_iteration(run_dir, 1, meta1)
    _write_iteration(run_dir, 2, meta2)
    return run_dir


def test_task_totals_keys_are_approach_namespaced(tmp_path):
    """`task_totals()` buckets on `"<approach>:<taskId>"`, not on `taskId`
    alone -- reverting to the bare-id bucket would collapse `"1:001"` and
    `"2:001"` into a single `"001"` key whose duration/cost is the WRONG
    sum of both approaches' iterations, and this assertion would fail."""
    run_dir = _fixture(tmp_path)
    totals = task_totals(_iterations(run_dir))

    assert "001" not in totals, (
        "task_totals must never key on the bare taskId alone -- "
        f"got {sorted(totals)}")
    assert set(totals) == {"1:001", "2:001"}

    assert totals["1:001"]["durationDisplay"] == "1m"
    assert totals["1:001"]["costDisplay"] == "$0.0100"
    assert totals["1:001"]["iterationCount"] == 1

    assert totals["2:001"]["durationDisplay"] == "5m"
    assert totals["2:001"]["costDisplay"] == "$0.0200"
    assert totals["2:001"]["iterationCount"] == 1


def test_with_task_totals_scopes_to_the_rendered_approach(tmp_path):
    """`_with_task_totals` passes the approach it is rendering and returns
    ONLY that approach's own minutes/dollars/iteration numbers for a task
    id -- never another approach's, and a task the CURRENT approach has
    never dispatched reads ZERO (`0s`/`$0.0000`, PRD I.1's own
    definition-of-done 10: "a `pending` task in the current approach shows
    zero"), even though an earlier approach's same-numbered task has real
    spend on disk."""
    run_dir = _fixture(tmp_path)
    iterations = _iterations(run_dir)
    tasks = {"tasks": [{"id": "001", "title": "the shared task id"}]}

    approach1 = _with_task_totals(tasks, iterations, 1)
    row1 = approach1["tasks"][0]
    assert row1["costDisplay"] == "$0.0100"
    assert row1["timeDisplay"] == "1m"
    assert [b["number"] for b in row1["iterationsBreakdown"]] == [1]

    approach2 = _with_task_totals(tasks, iterations, 2)
    row2 = approach2["tasks"][0]
    assert row2["costDisplay"] == "$0.0200"
    assert row2["timeDisplay"] == "5m"
    assert [b["number"] for b in row2["iterationsBreakdown"]] == [2]

    # -- a THIRD approach never dispatched task "001" at all: a real zero,
    # not the bare-id union of both real approaches' figures ---------------
    approach3 = _with_task_totals(tasks, iterations, 3)
    row3 = approach3["tasks"][0]
    assert row3["timeDisplay"] == "0s"
    assert row3["costDisplay"] == "$0.0000"
    assert row3["timeDisplay"] == task_totals_zero()["durationDisplay"]
    assert row3["costDisplay"] == task_totals_zero()["costDisplay"]
    # nothing to break down behind a zero, and a forged breakdown on the
    # incoming task doc is dropped rather than carried through
    assert "iterationsBreakdown" not in row3
    forged = {"tasks": [{"id": "001", "title": "t",
                         "iterationsBreakdown": [{"number": 1}]}]}
    assert "iterationsBreakdown" not in _with_task_totals(forged, iterations, 3)["tasks"][0]


def test_a_run_recording_no_dispatch_attribution_still_reads_as_unknown(tmp_path):
    """The zero above is a MEASURED zero, so it is not claimed for a run
    that measures nothing: a pre-task-027 run dir, whose iterations carry no
    `assignedTaskId` at all, still gets NEITHER key (the em dash task 038 /
    G.6's `test_pre_dispatch_record_run_dir_still_renders` pins in the
    browser). "Never measured" and "measured zero" must stay distinguishable.
    """
    run_dir = tmp_path / "run-legacy"
    (run_dir / "iterations").mkdir(parents=True)
    meta = _base_meta(1)
    assert "assignedTaskId" not in meta
    _write_iteration(run_dir, 1, meta)

    tasks = {"tasks": [{"id": "001", "title": "a task from before the change"}]}
    row = _with_task_totals(tasks, _iterations(run_dir), 1)["tasks"][0]
    assert "timeDisplay" not in row and "costDisplay" not in row
