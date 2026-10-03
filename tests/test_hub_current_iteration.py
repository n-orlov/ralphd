"""#288: the run page's iteration timeline shows the 3 newest rows when
collapsed, and a "Current iteration" panel sits beside the live log tail.

The panel's fields are built server-side (`ui_server.current_iteration_panel`,
delivered as `run_detail(...)["currentIteration"]`) from `status.json`'s
`currentIteration`, the in-flight iteration's own `meta.json` row and the task
it was dispatched against. The unit tests below pin that payload; the browser
tests (skipped cleanly without `playwright-cli`) pin the rendering.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
from test_browser_hub import (  # noqa: F401
    ARTIFACTS_DIR,
    _skip_reason,
    _wait_for,
    _wait_for_count_ge,
    pw,
)
from test_cli_ui import UiServer, _write_dead_run

from ralphd.cli.ui_server import current_iteration_panel, run_detail
from ralphd.engine.state import JOB_CONFIG_FILE, utc_from_epoch

SCREENSHOTS_DIR = ARTIFACTS_DIR / "screenshots" / "hub"

def browser(fn):
    """Browser tier, like `test_browser_hub_rate_limit.py`: skips cleanly
    without `playwright-cli`."""
    fn = pytest.mark.skipif(_skip_reason() is not None, reason=_skip_reason() or "")(fn)
    return pytest.mark.browser(fn)


def _iso(offset_s: float) -> str:
    return utc_from_epoch(time.time() + offset_s)


def _write_iterations(run_dir: Path, n_finished: int, *, in_flight: dict | None = None):
    for n in range(1, n_finished + 1):
        d = run_dir / "iterations" / f"{n:04d}"
        d.mkdir(parents=True)
        (d / "meta.json").write_text(json.dumps(
            {"number": n, "phase": "worker", "model": "stub-model", "approach": 1,
             "startedAt": f"2026-01-01T00:{n:02d}:00Z",
             "endedAt": f"2026-01-01T00:{n:02d}:30Z", "exitCode": 0, "error": None,
             "assignedTaskId": "002", "effectiveTier": "standard",
             "usage": {"totalTokens": 10}}))
    if in_flight is not None:
        d = run_dir / "iterations" / f"{in_flight['number']:04d}"
        d.mkdir(parents=True)
        (d / "meta.json").write_text(json.dumps(in_flight))


def _write_job_yaml(registry: Path, run_id: str, timeout_s: int = 1800):
    cdir = registry / "configs" / run_id
    cdir.mkdir(parents=True)
    (cdir / JOB_CONFIG_FILE).write_text(
        f'run_id: "{run_id}"\niterations: 25\niteration_timeout_s: {timeout_s}\n')


def _running_run(registry: Path, run_id: str) -> Path:
    started = _iso(-600)
    in_flight = {"number": 4, "phase": "worker", "model": "anthropic/claude-x",
                 "harness": "claude-code", "approach": 1, "startedAt": started,
                 "assignedTaskId": "002", "effectiveTier": "hard"}
    run_dir = _write_dead_run(
        registry, run_id, state="running", verdict=None, phase="worker",
        currentIteration={"number": 4, "phase": "worker",
                          "model": "anthropic/claude-x", "startedAt": started},
        progressBudget={"taskId": "002", "used": 2, "budget": 3})
    (run_dir / "tasks.json").write_text(json.dumps({"tasks": [
        {"id": "001", "title": "first", "status": "completed"},
        {"id": "002", "title": "wire the panel", "status": "in-progress"},
        {"id": "003", "title": "later", "status": "pending",
         "deferredUntil": _iso(900), "deferredReason": "re-poll backoff: #2"},
    ]}))
    _write_iterations(run_dir, 3, in_flight=in_flight)
    _write_job_yaml(registry, run_id)
    return run_dir


# ---------------------------------------------------------------------------
# server payload
# ---------------------------------------------------------------------------

def test_running_payload_carries_the_in_flight_iteration(tmp_path):
    registry = tmp_path / "registry"
    _running_run(registry, "run-cur")
    cur = run_detail(registry, "run-cur")["currentIteration"]
    assert cur["active"] is True
    assert cur["number"] == 4 and cur["phase"] == "worker"
    assert cur["model"] == "anthropic/claude-x" and cur["harness"] == "claude-code"
    assert cur["effectiveTier"] == "hard"
    assert cur["assignedTaskId"] == "002" and cur["taskTitle"] == "wire the panel"
    # iterations 1-3 plus this one, all against task 002 in approach 1
    assert cur["taskIterations"] == 4
    assert cur["progressBudget"] == {"used": 2, "budget": 3}
    assert cur["timeoutSeconds"] == 1800
    assert cur["startedAtLocal"]
    # no usage in meta.json mid-iteration: nothing claimed
    assert "tokensDisplay" not in cur and "costDisplay" not in cur
    assert [d["taskId"] for d in cur["deferred"]] == ["003"]
    assert "idleText" not in cur


def test_terminal_payload_is_idle_and_says_why(tmp_path):
    registry = tmp_path / "registry"
    run_dir = _write_dead_run(registry, "run-done", state="succeeded")
    _write_iterations(run_dir, 2)
    cur = run_detail(registry, "run-done")["currentIteration"]
    assert cur["active"] is False
    assert cur["idleText"] == "no iteration running — the run is over (succeeded)"


def test_idle_waiting_on_deferral_names_the_earliest_release():
    until_late, until_soon = _iso(1200), _iso(300)
    tasks = {"tasks": [
        {"id": "001", "status": "completed"},
        {"id": "002", "status": "pending", "deferredUntil": until_late},
        {"id": "003", "status": "pending", "deferredUntil": until_soon},
    ]}
    cur = current_iteration_panel({"state": "running", "currentIteration": None},
                                  [], tasks, Path("/nonexistent/job.yaml"))
    assert cur["active"] is False
    assert [d["taskId"] for d in cur["deferred"]] == ["003", "002"]
    assert "waiting on deferral: task 003" in cur["idleText"]


def test_idle_with_runnable_work_is_between_iterations():
    tasks = {"tasks": [
        {"id": "001", "status": "pending"},
        {"id": "002", "status": "pending", "deferredUntil": _iso(300)},
        # an expired deferral no longer holds anything back
        {"id": "003", "status": "pending", "deferredUntil": _iso(-300)},
    ]}
    cur = current_iteration_panel({"state": "running"}, [], tasks,
                                  Path("/nonexistent/job.yaml"))
    assert cur["idleText"] == "idle — between iterations (running)"
    assert [d["taskId"] for d in cur["deferred"]] == ["002"]


def test_infra_retry_note_reaches_the_panel_without_a_number():
    rows = [{"number": 7, "phase": "review", "startedAt": "2026-01-01T00:00:00Z",
             "harness": "pi", "assignedTaskId": None}]
    status = {"state": "running", "currentIteration": {
        "phase": "review", "note": "retrying after infra fault (attempt 2, next in 30s): x"}}
    cur = current_iteration_panel(status, rows, {"tasks": []},
                                  Path("/nonexistent/job.yaml"))
    assert cur["active"] is True and cur["number"] == 7
    assert cur["note"].startswith("retrying after infra fault")
    assert cur["timeoutSeconds"] is None


# ---------------------------------------------------------------------------
# browser
# ---------------------------------------------------------------------------

_RECENT = "document.querySelectorAll('.timeline-recent .timeline-item')"


@browser
def test_collapsed_timeline_shows_exactly_the_3_newest_and_expands_to_all(tmp_path, pw):
    registry = tmp_path / "registry"
    run_dir = _write_dead_run(registry, "run-last3", state="succeeded")
    _write_iterations(run_dir, 9)
    _write_job_yaml(registry, "run-last3")

    server = UiServer(registry)
    server.wait_ready()
    try:
        pw.open(f"{server.base}/#/run/run-last3")
        _wait_for_count_ge(pw, "document.querySelectorAll('.timeline-item').length", 9)
        # -- collapsed (default): exactly the 3 newest, newest first --------
        assert pw.eval_js("document.querySelector('.timeline-details').open") == "false"
        recent = pw.eval_js(
            f"Array.from({_RECENT}).map(e => e.dataset.iteration).join(',')").strip('"')
        assert recent == "9,8,7", recent
        summary = pw.eval_js("document.querySelector('.timeline-details summary').textContent")
        assert "show all 9" in summary, summary
        visible = pw.eval_js(
            "Array.from(document.querySelectorAll('.timeline-item'))"
            ".filter(e => e.checkVisibility()).length")
        assert int(visible) == 3
        pw.screenshot(SCREENSHOTS_DIR / "40-timeline-last3-collapsed-288.png")

        # -- a recent row still opens the iteration dialog -----------------
        pw.click('.timeline-item[data-iteration="8"]')
        _wait_for(pw, "document.querySelector('.text-dialog').textContent", "iteration: 8")
        pw.run("press", "Escape")

        # -- expand: all 9 visible, still newest first ----------------------
        pw.click(".timeline-details summary")
        _wait_for(pw, "document.querySelector('.timeline-details').open", "true")
        visible = pw.eval_js(
            "Array.from(document.querySelectorAll('.timeline-item'))"
            ".filter(e => e.checkVisibility()).map(e => e.dataset.iteration).join(',')")
        assert visible.strip('"') == "9,8,7,6,5,4,3,2,1", visible
    finally:
        server.stop()


@browser
def test_current_iteration_panel_running_and_idle(tmp_path, pw):
    registry = tmp_path / "registry"
    _running_run(registry, "run-cur-live")
    done_dir = _write_dead_run(registry, "run-cur-done", state="succeeded")
    _write_iterations(done_dir, 2)

    server = UiServer(registry)
    server.wait_ready()
    try:
        pw.open(f"{server.base}/#/run/run-cur-live")
        text = _wait_for(pw, "document.getElementById('current-iteration-box').innerText",
                         "#4")
        assert "worker" in text, text
        assert "anthropic/claude-x · claude-code" in text, text
        assert "002 — wire the panel" in text, text
        assert "iteration 4 on this task, budget 2/3" in text, text
        assert "of 30m 0s" in text, text
        assert "deferred: task 003" in text, text
        assert int(pw.eval_js(
            "document.querySelectorAll('#current-iteration-box .ci-bar-fill').length")) == 1
        # side by side with the log on a wide viewport
        same_row = pw.eval_js(
            "(() => { const a = document.querySelector('.current-iteration-sec')"
            ".getBoundingClientRect(); const b = document.querySelector('.log-sec')"
            ".getBoundingClientRect(); return window.innerWidth >= 1000"
            " ? String(Math.abs(a.top - b.top) < 2 && a.right <= b.left) : 'narrow'; })()")
        assert same_row.strip('"') in ("true", "narrow"), same_row
        pw.screenshot(SCREENSHOTS_DIR / "41-current-iteration-running-288.png")

        pw.goto(f"{server.base}/#/run/run-cur-done")
        text = _wait_for(pw, "document.getElementById('current-iteration-box').innerText",
                         "the run is over")
        assert "succeeded" in text, text
        assert int(pw.eval_js(
            "document.querySelectorAll('#current-iteration-box .ci-row').length")) == 0
        pw.screenshot(SCREENSHOTS_DIR / "42-current-iteration-idle-288.png")
    finally:
        server.stop()
