"""Playwright-cli browser e2e tests for the hub (PRD req 23a).

Marked `@pytest.mark.browser`. Drives a *real* Chromium via the
`playwright-cli` binary (a stdlib-free external tool, shelled out to --
never imported), against a real `ralphctl ui` server (tasks 033/034).
Assertions are made by evaluating small JS snippets in the page
(`playwright-cli eval ...`) rather than by parsing markdown snapshots.

Skips cleanly (whole module) if the `playwright-cli` binary is not on
PATH. Screenshots of each view are written under
`artifacts/screenshots/hub/` (the job's artifacts dir, taken from
`RALPHD_ARTIFACTS_DIR`, defaulting to `/run/ralphd/artifacts` to match
this job's layout).
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
from test_cli_ui import UiServer, _write_dead_run

PLAYWRIGHT_CLI = shutil.which("playwright-cli")

ARTIFACTS_DIR = Path(os.environ.get("RALPHD_ARTIFACTS_DIR", "/run/ralphd/artifacts"))
SCREENSHOTS_DIR = ARTIFACTS_DIR / "screenshots" / "hub"


def _skip_reason():
    if PLAYWRIGHT_CLI is None:
        return "playwright-cli not on PATH"
    return None


pytestmark = [pytest.mark.browser, pytest.mark.skipif(_skip_reason() is not None, reason=_skip_reason() or "")]


class Pw:
    """Thin wrapper shelling out to `playwright-cli -s=<session> ...`."""

    def __init__(self, session: str):
        self.session = session

    def run(self, *args: str, timeout: int = 30) -> subprocess.CompletedProcess:
        cmd = ["playwright-cli", f"-s={self.session}", *args]
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)

    def open(self, url: str):
        r = self.run("open", url, timeout=60)
        assert r.returncode == 0, r.stdout + r.stderr
        return r

    def eval_js(self, expr: str) -> str:
        """Evaluate a JS expression in the page, return the raw (unwrapped)
        result via --raw so string quoting doesn't leak into assertions."""
        r = self.run("--raw", "eval", expr)
        assert r.returncode == 0, r.stdout + r.stderr
        return r.stdout.strip()

    def fill(self, target: str, text: str):
        r = self.run("fill", target, text)
        assert r.returncode == 0, r.stdout + r.stderr

    def click(self, target: str):
        r = self.run("click", target)
        assert r.returncode == 0, r.stdout + r.stderr

    def check(self, target: str):
        r = self.run("check", target)
        assert r.returncode == 0, r.stdout + r.stderr

    def screenshot(self, filename: Path):
        filename.parent.mkdir(parents=True, exist_ok=True)
        r = self.run("screenshot", f"--filename={filename}")
        assert r.returncode == 0, r.stdout + r.stderr
        assert filename.is_file() and filename.stat().st_size > 0

    def close(self):
        self.run("close", timeout=15)


@pytest.fixture
def pw():
    """A uniquely-named playwright-cli session, force-closed at teardown."""
    name = f"hub-{os.getpid()}-{time.time_ns()}"
    session = Pw(name)
    yield session
    session.close()


def _wait_for(session: Pw, expr: str, expected_substr: str, timeout=15):
    deadline = time.time() + timeout
    last = ""
    while time.time() < deadline:
        last = session.eval_js(expr)
        if expected_substr in last:
            return last
        time.sleep(0.3)
    raise AssertionError(f"timed out waiting for {expected_substr!r} in eval({expr!r}); last={last!r}")


def _wait_for_count_ge(session: Pw, expr: str, minimum: int, timeout=15):
    deadline = time.time() + timeout
    last = 0
    while time.time() < deadline:
        raw = session.eval_js(expr)
        try:
            last = int(raw)
        except ValueError:
            last = 0
        if last >= minimum:
            return last
        time.sleep(0.3)
    raise AssertionError(f"timed out waiting for {expr!r} >= {minimum}; last={last!r}")


def test_run_list_renders_fixture_runs(tmp_path, pw):
    registry = tmp_path / "registry"
    _write_dead_run(registry, "run-alpha", state="succeeded", verdict="verified")
    _write_dead_run(registry, "run-beta", state="failed", verdict=None)

    server = UiServer(registry)
    server.wait_ready()
    try:
        pw.open(server.base)
        body_text = _wait_for(pw, "document.body.innerText", "run-alpha")
        assert "run-beta" in body_text
        assert "succeeded" in body_text
        assert "failed" in body_text
        pw.screenshot(SCREENSHOTS_DIR / "01-run-list.png")
    finally:
        server.stop()


def test_run_detail_shows_task_table_and_iteration_timeline(tmp_path, pw, live):
    run = live(run_id="browser-detail",
               job={"iterations": 12, "max_approaches": 3, "on_complete": "idle"},
               stub_env={"STUB_TASKS": "2", "STUB_SLEEP": "1"})
    run.wait_api()

    server = UiServer(run.registry)
    server.wait_ready()
    try:
        pw.open(f"{server.base}/#/run/{run.run_id}")
        # wait for the task table to render at least one task id.
        body_text = _wait_for(pw, "document.body.innerText", "Tasks")
        _wait_for_count_ge(pw, "document.querySelectorAll('#task-box tbody tr').length", 1)

        n_task_rows = int(pw.eval_js("document.querySelectorAll('#task-box tbody tr').length"))
        assert n_task_rows >= 1, body_text

        # iteration timeline should show at least one iteration row once the
        # engine has run past planning.
        n_timeline_rows = _wait_for_count_ge(pw, "document.querySelectorAll('.timeline-item').length", 1, timeout=30)
        assert n_timeline_rows >= 1

        pw.screenshot(SCREENSHOTS_DIR / "02-run-detail.png")
    finally:
        run.wait_terminal(timeout=60)
        server.stop()


def test_steering_form_submit_creates_steering_file(tmp_path, pw, live):
    run = live(run_id="browser-steer",
               job={"iterations": 12, "max_approaches": 3, "on_complete": "idle"},
               stub_env={"STUB_TASKS": "1", "STUB_SLEEP": "3"})
    run.wait_api()

    server = UiServer(run.registry)
    server.wait_ready()
    try:
        pw.open(f"{server.base}/#/run/{run.run_id}")
        _wait_for(pw, "document.body.innerText", "Steer")

        pw.fill("#steer-message", "browser e2e steering message")
        pw.fill("#steer-name", "browser-e2e")
        pw.click("#steer-form button[type=submit]")

        status_text = _wait_for(pw, "document.getElementById('steer-status').textContent", "sent")
        assert "sent" in status_text

        pw.screenshot(SCREENSHOTS_DIR / "03-steering-sent.png")
    finally:
        run.wait_terminal(timeout=60)
        server.stop()

    steering_files = list((run.run_dir / "steering").glob("*.md"))
    assert steering_files, "steering form submit via the browser never wrote a file"
    assert any("browser e2e steering message" in f.read_text() for f in steering_files)


def test_module_reports_playwright_cli_present():
    """Sanity check the environment actually has the tool (this suite's
    success criteria requires the tests be run for real, not skipped)."""
    assert PLAYWRIGHT_CLI is not None, "playwright-cli must be on PATH for this run"
