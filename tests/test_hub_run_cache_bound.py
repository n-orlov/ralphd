"""Task 047 (H.5): the hub's three per-run derivation caches are bounded.

`_ITERATION_ROW_CACHE`, `_TASKS_BLOCK_CACHE` and `_STATUS_BLOCK_CACHE`
(`src/ralphd/cli/ui_server.py`, task 004/A.2) are keyed per run id, at
MODULE scope, with entries dropped only when `delete_run` removes that run
-- a long-lived `ralphctl ui` that polls many distinct run ids over its life
(every run it was ever pointed at, never explicitly deleted) would otherwise
grow one entry per run id forever. `_bound_run_cache` (task 047) caps each
cache at `_RUN_CACHE_CAP` (200) distinct run ids, evicting the
least-recently-used one once a new run id would push it over.

This module drives the real per-run helper functions -- never the bound
helper in isolation -- for more distinct run ids than the cap, and asserts
the cap holds for all three caches afterward.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ralphd.cli import ui_server

N_RUNS = ui_server._RUN_CACHE_CAP + 50


def _seed_minimal_run(reg: Path, run_id: str) -> Path:
    run_dir = reg / "runs" / run_id
    run_dir.mkdir(parents=True)
    (run_dir / "tasks.json").write_text(json.dumps({"tasks": []}))
    idir = run_dir / "iterations" / "0001"
    idir.mkdir(parents=True)
    (idir / "meta.json").write_text(json.dumps({
        "number": 1, "startedAt": "2026-01-01T00:00:00Z",
        "endedAt": "2026-01-01T00:01:00Z", "exitCode": 0}))
    (idir / "output.jsonl").write_text(
        json.dumps({"type": "text", "text": "hi"}) + "\n")
    return run_dir


@pytest.fixture(autouse=True)
def _clear_caches():
    """The three caches live at module scope -- start and end this test
    module cold so its own run ids never leak into (or are contaminated by)
    another test module sharing the same process."""
    ui_server._ITERATION_ROW_CACHE.clear()
    ui_server._TASKS_BLOCK_CACHE.clear()
    ui_server._STATUS_BLOCK_CACHE.clear()
    yield
    ui_server._ITERATION_ROW_CACHE.clear()
    ui_server._TASKS_BLOCK_CACHE.clear()
    ui_server._STATUS_BLOCK_CACHE.clear()


def test_more_runs_than_the_cap_leaves_each_cache_at_or_under_it(tmp_path):
    reg = tmp_path / "registry"
    (reg / "runs").mkdir(parents=True)
    recorded_status = {
        "runId": "placeholder", "state": "succeeded", "verdict": "verified",
        "phase": "review", "approach": 1, "iterationsUsed": 1,
        "iterationsBudget": 1, "startedAt": "2026-01-01T00:00:00Z"}

    for i in range(N_RUNS):
        run_id = f"cache-bound-run-{i:04d}"
        run_dir = _seed_minimal_run(reg, run_id)
        ui_server._cached_iteration_row(run_dir, run_id, 1)
        ui_server._cached_disk_tasks_block(run_dir, run_id)
        ui_server._cached_disk_status_block(
            reg, run_dir, run_id, {**recorded_status, "runId": run_id})

    assert N_RUNS > ui_server._RUN_CACHE_CAP
    assert len(ui_server._ITERATION_ROW_CACHE) <= ui_server._RUN_CACHE_CAP
    assert len(ui_server._TASKS_BLOCK_CACHE) <= ui_server._RUN_CACHE_CAP
    assert len(ui_server._STATUS_BLOCK_CACHE) <= ui_server._RUN_CACHE_CAP

    # The most-recently-touched run ids are the ones still resident -- the
    # very first run id seeded, touched only once and never again, is the
    # one an LRU policy evicts first.
    assert "cache-bound-run-0000" not in ui_server._ITERATION_ROW_CACHE
    assert "cache-bound-run-0000" not in ui_server._TASKS_BLOCK_CACHE
    assert "cache-bound-run-0000" not in ui_server._STATUS_BLOCK_CACHE
    last_run_id = f"cache-bound-run-{N_RUNS - 1:04d}"
    assert last_run_id in ui_server._ITERATION_ROW_CACHE
    assert last_run_id in ui_server._TASKS_BLOCK_CACHE
    assert last_run_id in ui_server._STATUS_BLOCK_CACHE
