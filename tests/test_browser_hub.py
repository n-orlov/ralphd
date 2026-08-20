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

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
from test_cli_ui import StubEngineApi, UiServer, _write_dead_run, _write_run_with_api

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


def test_dead_nonterminal_run_gets_the_warning_treatment(tmp_path, pw):
    """Task 024 (#8): a run whose status.json still records `running` while
    its container is gone must NOT render like a healthy running run -- the
    engine was killed before it could write a terminal state, so `state`
    alone lies. Assert the card's existing warning treatment
    (`.card.warning`, one CSS rule shared with `.card.degraded`) plus the
    `container appears gone` line on the detail view and a row marker in the
    run list, and their complete absence for a live running run."""
    registry = tmp_path / "registry"
    engine = StubEngineApi(status={
        "runId": "run-alive", "state": "running", "verdict": None,
        "phase": "worker", "approach": 1, "iterationsUsed": 2,
        "iterationsBudget": 25, "startedAt": "2026-01-01T00:00:00Z",
    })
    _write_dead_run(registry, "run-zombie", state="running", verdict=None)
    _write_run_with_api(registry, "run-alive", engine, state="running", verdict=None)

    server = UiServer(registry)
    server.wait_ready()
    try:
        # run list: the zombie's row is marked, the live run's is not
        pw.open(server.base)
        _wait_for(pw, "document.body.innerText", "run-zombie")
        _wait_for_count_ge(pw, "document.querySelectorAll('tr.row-warning').length", 1)
        marked = pw.eval_js(
            "[...document.querySelectorAll('tr.row-warning')]"
            ".map(r => r.querySelector('a').textContent).join(',')").strip('"')
        assert marked == "run-zombie", marked
        assert "container gone" in pw.eval_js("document.body.innerText")
        n_marker = int(pw.eval_js(
            "document.querySelectorAll('.container-gone-marker').length"))
        assert n_marker == 1
        pw.screenshot(SCREENSHOTS_DIR / "08-run-list-container-gone.png")

        # detail: warning card + the explanation naming `ralphctl repair`
        pw.open(f"{server.base}/#/run/run-zombie")
        body_text = _wait_for(pw, "document.body.innerText", "container appears gone")
        assert "ralphctl repair run-zombie" in body_text, body_text
        assert "records state" in body_text and "running" in body_text, body_text
        n_warning = int(pw.eval_js("document.querySelectorAll('.card.warning').length"))
        n_gone = int(pw.eval_js("document.querySelectorAll('.container-gone').length"))
        assert n_warning == 1
        assert n_gone == 1
        pw.screenshot(SCREENSHOTS_DIR / "09-detail-container-gone.png")

        # a genuinely live running run: no warning treatment anywhere
        pw.open(f"{server.base}/#/run/run-alive")
        body_text = _wait_for(pw, "document.body.innerText", "running")
        assert "container appears gone" not in body_text, body_text
        n_warning = int(pw.eval_js("document.querySelectorAll('.card.warning').length"))
        n_gone = int(pw.eval_js("document.querySelectorAll('.container-gone').length"))
        assert n_warning == 0
        assert n_gone == 0
    finally:
        server.stop()
        engine.close()


def test_run_detail_shows_unconsumed_steering_warning(tmp_path, pw):
    """Task 006: the hub run-detail view must loudly surface a terminal
    run's unconsumedSteering field (not silently omit it the way a plain
    key/value dump of status.json's other fields would if a caller forgot
    to look)."""
    registry = tmp_path / "registry"
    _write_dead_run(registry, "run-stranded", state="aborted", verdict="unverified",
                    unconsumedSteering=["001-stranded.md"])
    _write_dead_run(registry, "run-clean", state="succeeded", verdict="verified",
                    unconsumedSteering=[])

    server = UiServer(registry)
    server.wait_ready()
    try:
        pw.open(f"{server.base}/#/run/run-stranded")
        body_text = _wait_for(pw, "document.body.innerText", "UNCONSUMED STEERING")
        assert "001-stranded.md" in body_text
        n_warn = int(pw.eval_js("document.querySelectorAll('.steering-warning').length"))
        assert n_warn == 1

        pw.open(f"{server.base}/#/run/run-clean")
        body_text = _wait_for(pw, "document.body.innerText", "succeeded")
        assert "UNCONSUMED STEERING" not in body_text
        n_warn = int(pw.eval_js("document.querySelectorAll('.steering-warning').length"))
        assert n_warn == 0
    finally:
        server.stop()


def test_run_detail_shows_infra_retry_note(tmp_path, pw):
    """Task 001a criterion 4: while an infra-fault retry is backing off,
    `currentIteration.note` must be rendered on the hub run-detail card
    (not just present in the raw JSON the page fetches), so an operator
    watching a live job via the hub sees the same 'retrying after infra
    fault...' message `ralphctl status` shows."""
    registry = tmp_path / "registry"
    _write_dead_run(registry, "run-retrying", state="running", verdict="unverified",
                    currentIteration={
                        "phase": "worker",
                        "note": ("retrying after infra fault (attempt 1/3, "
                                 "next in 60s): getaddrinfo ENOTFOUND"),
                    })
    _write_dead_run(registry, "run-clean", state="succeeded", verdict="verified")

    server = UiServer(registry)
    server.wait_ready()
    try:
        pw.open(f"{server.base}/#/run/run-retrying")
        body_text = _wait_for(pw, "document.body.innerText", "retrying after infra fault")
        assert "ENOTFOUND" in body_text
        n_note = int(pw.eval_js("document.querySelectorAll('.infra-retry-note').length"))
        assert n_note == 1

        pw.open(f"{server.base}/#/run/run-clean")
        body_text = _wait_for(pw, "document.body.innerText", "succeeded")
        assert "retrying after infra fault" not in body_text
        n_note = int(pw.eval_js("document.querySelectorAll('.infra-retry-note').length"))
        assert n_note == 0
    finally:
        server.stop()


def test_run_detail_renders_degraded_infra_wait_distinctly(tmp_path, pw):
    """Task 014 (#5): a run sitting out an infra outage keeps
    `state: running` on purpose (docs/api.md: there is no `degraded` state
    value), so without a dedicated treatment the hub renders it EXACTLY
    like a healthy running run and the operator sees a job that merely
    looks stuck. Assert the degraded card treatment (`.card.degraded` +
    `.infra-wait`) appears for a degraded fixture, carries the error and
    the countdown to `nextAttemptAt`, and is entirely absent for a healthy
    running run."""
    registry = tmp_path / "registry"
    next_attempt = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + 150))
    _write_dead_run(registry, "run-degraded", state="running", verdict=None,
                    health="degraded",
                    infraWait={
                        "since": "2026-01-01T00:00:00Z", "attempt": 4,
                        "error": "getaddrinfo EAI_AGAIN aigw.example.invalid",
                        "phase": "worker", "nextAttemptAt": next_attempt,
                        "waitedS": 52, "budgetS": 14400, "remainingS": 14348,
                    })
    _write_dead_run(registry, "run-healthy", state="running", verdict=None,
                    health="ok", infraWait=None)

    server = UiServer(registry)
    server.wait_ready()
    try:
        pw.open(f"{server.base}/#/run/run-degraded")
        body_text = _wait_for(pw, "document.body.innerText", "degraded")
        assert "EAI_AGAIN" in body_text, body_text
        assert "attempt 4" in body_text, body_text
        assert "worker" in body_text, body_text
        # countdown to nextAttemptAt, not a bare timestamp
        assert "next attempt in " in body_text, body_text
        assert "outage budget" in body_text, body_text
        n_degraded = int(pw.eval_js("document.querySelectorAll('.card.degraded').length"))
        n_wait = int(pw.eval_js("document.querySelectorAll('.infra-wait').length"))
        assert n_degraded == 1
        assert n_wait == 1
        pw.screenshot(SCREENSHOTS_DIR / "05-degraded-infra-wait.png")

        pw.open(f"{server.base}/#/run/run-healthy")
        body_text = _wait_for(pw, "document.body.innerText", "running")
        assert "degraded" not in body_text, body_text
        assert "EAI_AGAIN" not in body_text
        n_degraded = int(pw.eval_js("document.querySelectorAll('.card.degraded').length"))
        n_wait = int(pw.eval_js("document.querySelectorAll('.infra-wait').length"))
        assert n_degraded == 0
        assert n_wait == 0
    finally:
        server.stop()


def test_degraded_card_offers_a_retry_now_button_with_a_ticking_countdown(tmp_path, pw):
    """Task 017 (#5): while a run is degraded the run-detail card must show
    a countdown to `nextAttemptAt` and a "retry now" button that POSTs
    through the hub's proxy (`POST /api/runs/<id>/retry`, ui_server.py) to
    the run's own `POST /retry` -- the browser equivalent of `ralphctl retry
    <run-id>`. The button must NOT exist on a healthy run's card (there is
    no wait to wake, `/retry` would only 409), and a dead run's card stays
    READ-ONLY (on-disk snapshot -- a button whose proxy can only ever answer
    503 would be a lie)."""
    registry = tmp_path / "registry"
    next_attempt = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + 180))
    infra_wait = {
        "since": "2026-01-01T00:00:00Z", "attempt": 3,
        "error": "socket hang up", "phase": "worker",
        "nextAttemptAt": next_attempt,
        "waitedS": 20, "budgetS": 14400, "remainingS": 14380,
    }
    degraded_status = {
        "runId": "run-degraded-live", "state": "running", "verdict": None,
        "phase": "worker", "approach": 1, "iterationsUsed": 5,
        "iterationsBudget": 25, "startedAt": "2026-01-01T00:00:00Z",
        "health": "degraded", "infraWait": infra_wait,
    }
    healthy_status = {**degraded_status, "runId": "run-healthy-live",
                      "health": "ok", "infraWait": None}

    degraded_engine = StubEngineApi(status=degraded_status)
    healthy_engine = StubEngineApi(status=healthy_status)
    _write_run_with_api(registry, "run-degraded-live", degraded_engine,
                        token="hub-token", **degraded_status)
    _write_run_with_api(registry, "run-healthy-live", healthy_engine,
                        **healthy_status)
    # dead + degraded: last known state says degraded but the API is gone
    _write_dead_run(registry, "run-degraded-dead", state="running", verdict=None,
                    health="degraded", infraWait=infra_wait)

    server = UiServer(registry)
    server.wait_ready()
    try:
        pw.open(f"{server.base}/#/run/run-degraded-live")
        _wait_for(pw, "document.body.innerText", "retry now")
        n_button = int(pw.eval_js("document.querySelectorAll('button.retry-now').length"))
        assert n_button == 1
        # the countdown is live-ticking, not a value frozen until the 4s
        # full-page rebuild
        first = pw.eval_js("document.querySelector('.infra-countdown').textContent")
        assert "next attempt in " in first, first
        time.sleep(1.6)
        second = pw.eval_js("document.querySelector('.infra-countdown').textContent")
        assert second != first, (first, second)

        pw.click("button.retry-now")
        deadline = time.time() + 15
        posts = []
        while time.time() < deadline:
            posts = [(m, p) for m, p, _t in degraded_engine.requests if m == "POST"]
            if posts:
                break
            time.sleep(0.3)
        assert posts == [("POST", "/retry")], degraded_engine.requests
        pw.screenshot(SCREENSHOTS_DIR / "06-retry-now-button.png")

        # healthy run: no button at all
        pw.open(f"{server.base}/#/run/run-healthy-live")
        body_text = _wait_for(pw, "document.body.innerText", "running")
        assert "retry now" not in body_text, body_text
        n_button = int(pw.eval_js("document.querySelectorAll('button.retry-now').length"))
        assert n_button == 0
        assert [(m, p) for m, p, _t in healthy_engine.requests if m == "POST"] == []

        # dead run recorded degraded: read-only snapshot, still no button
        pw.open(f"{server.base}/#/run/run-degraded-dead")
        body_text = _wait_for(pw, "document.body.innerText", "degraded")
        assert "read-only on-disk snapshot" in body_text, body_text
        n_button = int(pw.eval_js("document.querySelectorAll('button.retry-now').length"))
        assert n_button == 0
    finally:
        server.stop()
        degraded_engine.close()
        healthy_engine.close()


def test_run_detail_shows_a_failed_reflection(tmp_path, pw):
    """Task 020 (#5): a run whose post-terminal `reflect` iteration failed
    keeps its terminal state/verdict/reason untouched (docs/api.md's
    `reflect`), so without a dedicated line the hub renders a run that lost
    its post-mortem exactly like one that never enabled reflect. Assert the
    `.reflect-failed` line appears for a failed-reflection fixture naming the
    error, and is absent both for a successful reflection and for a run that
    never ran one."""
    registry = tmp_path / "registry"
    _write_dead_run(registry, "run-reflect-failed", state="failed", verdict=None,
                    reflect={"ok": False, "error": "Connection error.",
                             "endedAt": "2026-01-01T01:00:00Z"})
    _write_dead_run(registry, "run-reflect-ok", state="succeeded", verdict="verified",
                    reflect={"ok": True, "error": None,
                             "endedAt": "2026-01-01T01:00:00Z"})
    _write_dead_run(registry, "run-reflect-off", state="succeeded", verdict="verified")

    server = UiServer(registry)
    server.wait_ready()
    try:
        pw.open(f"{server.base}/#/run/run-reflect-failed")
        body_text = _wait_for(pw, "document.body.innerText", "reflection: failed")
        assert "Connection error." in body_text, body_text
        n_reflect = int(pw.eval_js("document.querySelectorAll('.reflect-failed').length"))
        assert n_reflect == 1
        pw.screenshot(SCREENSHOTS_DIR / "07-reflection-failed.png")

        for run_id in ("run-reflect-ok", "run-reflect-off"):
            pw.open(f"{server.base}/#/run/{run_id}")
            body_text = _wait_for(pw, "document.body.innerText", "succeeded")
            assert "reflection: failed" not in body_text, (run_id, body_text)
            n_reflect = int(pw.eval_js(
                "document.querySelectorAll('.reflect-failed').length"))
            assert n_reflect == 0, run_id
    finally:
        server.stop()


def test_run_detail_shows_reason_for_terminal_failed_run(tmp_path, pw):
    """Task 004: the engine's high-quality `reason` string (e.g. the
    no-progress fail-fast explanation) must be prominently visible on the
    hub run-detail page for terminal failed/aborted runs, not buried in
    raw JSON the page fetches but never displays."""
    registry = tmp_path / "registry"
    _write_dead_run(registry, "run-failed-reason", state="failed", verdict="unverified",
                    reason="no-progress guard tripped: 3 consecutive instant exits "
                           "(No API key found for amazon-bedrock)")
    _write_dead_run(registry, "run-clean", state="succeeded", verdict="verified")

    server = UiServer(registry)
    server.wait_ready()
    try:
        pw.open(f"{server.base}/#/run/run-failed-reason")
        body_text = _wait_for(pw, "document.body.innerText", "no-progress guard tripped")
        assert "amazon-bedrock" in body_text
        n_reason = int(pw.eval_js("document.querySelectorAll('.run-reason').length"))
        assert n_reason == 1

        pw.open(f"{server.base}/#/run/run-clean")
        body_text = _wait_for(pw, "document.body.innerText", "succeeded")
        assert "no-progress guard tripped" not in body_text
        n_reason = int(pw.eval_js("document.querySelectorAll('.run-reason').length"))
        assert n_reason == 0
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


def test_run_detail_log_tail_collapses_many_delta_thinking_block(tmp_path, pw, live):
    """Task 014: the hub run-detail log tail is server-rendered through
    the shared `log_render` module `ralphctl logs` uses, rather than
    `app.js` reimplementing event-to-HTML rendering client-side. The
    pre-fix `app.js` (see git history: the `renderLogText` function this
    task deleted) appended a fresh `.lg-thinking` element for EVERY
    `thinking_start`/`thinking_delta` event with no `thinking_seen` guard
    -- so a thinking block streamed across N deltas (`STUB_RICH_EVENTS`'s
    `emit_rich_preamble` chunks its thinking text into multiple deltas,
    see tests/stub-pi/pi) flooded the tail with N '[thinking…]'
    elements. This negative-proof is documented here rather than kept
    runnable against the old code: reverting `app.js`'s `loadLogs`/
    `renderLogLines` to the deleted `renderLogText` implementation (and
    pointing it at the old `body.text` field) reproduces a per-delta line
    count for this same fixture (many more than the 2 rich iterations
    below), which this test's exact-count assertion would fail on.

    The fix collapses each rich iteration's thinking burst to EXACTLY ONE
    line server-side (the same `thinking_seen` guard
    `_render_message_update`/`_render_message_end` in `log_render.py`
    already applied for the CLI), and `app.js` merely displays the lines
    the server decided on."""
    run = live(run_id="browser-thinking",
               job={"iterations": 12, "max_approaches": 3, "on_complete": "idle"},
               stub_env={"STUB_RICH_EVENTS": "1", "STUB_TASKS": "1", "STUB_SLEEP": "1"})
    run.wait_api()

    server = UiServer(run.registry)
    server.wait_ready()
    try:
        pw.open(f"{server.base}/#/run/{run.run_id}")
        _wait_for_count_ge(
            pw,
            "Array.from(document.querySelectorAll('#logbox .lg-line'))"
            ".filter(e => e.textContent.includes('[thinking')).length",
            1, timeout=30)

        n_thinking = int(pw.eval_js(
            "Array.from(document.querySelectorAll('#logbox .lg-line'))"
            ".filter(e => e.textContent.includes('[thinking')).length"))
        n_boundaries_with_thinking = int(pw.eval_js(
            "Array.from(document.querySelectorAll('#logbox .lg-line'))"
            ".filter(e => e.textContent.includes('── iteration')).length - 1"))
        # This stub run's worker + review iterations each emit ONE rich
        # thinking block (`STUB_RICH_EVENTS`, see tests/stub-pi/pi) chunked
        # across MULTIPLE `thinking_delta` events (`_chunks` in stub-pi) --
        # the planning iteration emits none. So the correctly-collapsed
        # count is exactly one '[thinking…]' line per rich iteration (2
        # here: worker, review), NOT one per delta (which `_chunks`' chunk
        # count for these thinking strings would make considerably higher
        # than 2 if the pre-fix per-delta-append bug were still present).
        body_text = pw.eval_js("document.body.innerText")
        assert n_thinking == 2, (
            f"expected exactly one collapsed '[thinking…]' line per rich "
            f"iteration (2: worker + review), got {n_thinking}; "
            f"body={body_text!r}")
        assert n_boundaries_with_thinking >= 0  # sanity: didn't miscount

        pw.screenshot(SCREENSHOTS_DIR / "04-thinking-collapse.png")
    finally:
        run.wait_terminal(timeout=60)
        server.stop()


def test_dead_run_log_tail_shows_the_on_disk_snapshot_label(tmp_path, pw):
    """Task 039 (#6): the hub log tail falls back to the shared on-disk
    merge when a run's API is unreachable, so a dead run's transcript is
    still readable -- but it MUST be labelled as a snapshot rather than
    passed off as a live tail (the wording style the detail card's `live`
    row already uses: "no (on-disk snapshot)"). Asserts the
    `.lg-snapshot` line plus real transcript lines for a dead fixture, and
    that a live run gets neither the label nor a lost tail."""
    registry = tmp_path / "registry"
    dead = _write_dead_run(registry, "run-dead-logs", state="running", verdict=None)
    for n, phase, text in ((1, "planning", "browser snapshot planning line"),
                            (2, "worker", "browser snapshot worker line")):
        d = dead / "iterations" / f"{n:04d}"
        d.mkdir(parents=True)
        (d / "meta.json").write_text(json.dumps(
            {"number": n, "phase": phase, "model": "stub-model", "approach": 1,
             "startedAt": f"2026-01-01T00:0{n}:00Z", "exitCode": 0, "error": None,
             "endedAt": f"2026-01-01T00:0{n}:30Z", "usage": {"totalTokens": 10 * n}}))
        (d / "output.jsonl").write_text(json.dumps(
            {"type": "message_end",
             "message": {"content": [{"type": "text", "text": text}]}}) + "\n")

    live_engine = StubEngineApi(status={"runId": "run-live-logs", "state": "running"})
    _write_run_with_api(registry, "run-live-logs", live_engine, state="running")

    server = UiServer(registry)
    server.wait_ready()
    try:
        pw.open(f"{server.base}/#/run/run-dead-logs")
        _wait_for(pw, "document.getElementById('logbox').textContent",
                  "on-disk snapshot")
        assert int(pw.eval_js(
            "document.querySelectorAll('#logbox .lg-snapshot').length")) == 1
        logbox = pw.eval_js("document.getElementById('logbox').textContent")
        assert "browser snapshot planning line" in logbox, logbox
        assert "browser snapshot worker line" in logbox, logbox
        pw.screenshot(SCREENSHOTS_DIR / "08-dead-run-log-snapshot.png")

        pw.open(f"{server.base}/#/run/run-live-logs")
        _wait_for(pw, "document.body.innerText", "running")
        assert int(pw.eval_js(
            "document.querySelectorAll('#logbox .lg-snapshot').length")) == 0
    finally:
        server.stop()
        live_engine.close()


def test_timeline_and_summary_show_absolute_local_timestamps(tmp_path, pw):
    """Task 048 (#4): the iteration timeline anchors every row in wall-clock
    time, and the summary card shows the absolute start/end instants next to
    the relative duration. The strings are produced server-side by the one
    shared Python formatter (`engine/state.format_local_time`, delivered as
    `startedAtLocal`/`endedAtLocal`), so this asserts the rendered cell text
    equals exactly what that formatter produces -- while the payload the
    page fetched still carries the raw ISO values."""
    from ralphd.engine.state import format_local_time

    registry = tmp_path / "registry"
    started, ended = "2026-01-01T00:01:00Z", "2026-01-01T00:41:30Z"
    run_dir = _write_dead_run(registry, "run-timestamps", state="succeeded",
                              startedAt=started, endedAt=ended)
    it_start, it_end = "2026-01-01T00:01:00Z", "2026-01-01T00:01:30Z"
    d = run_dir / "iterations" / "0001"
    d.mkdir(parents=True)
    (d / "meta.json").write_text(json.dumps(
        {"number": 1, "phase": "planning", "model": "stub-model", "approach": 1,
         "startedAt": it_start, "endedAt": it_end, "exitCode": 0,
         "error": None, "usage": {"totalTokens": 10}}))

    server = UiServer(registry)
    server.wait_ready()
    try:
        pw.open(f"{server.base}/#/run/run-timestamps")
        _wait_for_count_ge(pw, "document.querySelectorAll('.timeline-item').length", 1)
        cell = pw.eval_js(
            "document.querySelector('.timeline-item .at').textContent").strip('"')
        assert cell == format_local_time(it_start), cell

        body_text = _wait_for(pw, "document.body.innerText", "started")
        assert format_local_time(started) in body_text, body_text
        assert format_local_time(ended) in body_text, body_text
        # relative duration is kept alongside, not replaced
        assert "total " in body_text, body_text
        pw.screenshot(SCREENSHOTS_DIR / "10-absolute-timestamps.png")

        # the payload the page consumed still carries the raw ISO values
        code, detail = server.get("/api/runs/run-timestamps")
        assert code == 200, detail
        assert detail["iterations"][0]["startedAt"] == it_start
        assert detail["status"]["startedAt"] == started
    finally:
        server.stop()


def test_run_detail_renders_unknown_cost_as_unavailable(tmp_path, pw):
    """Task 051 (#10): the usage panel must never claim `$0.0000` for a cost
    the provider never quoted. The string is produced server-side by the one
    shared formatter (`engine/state.format_cost`, shipped as
    `usage.costDisplay` -- the `startedAtLocal` pattern), so this asserts the
    rendered panel text for an unpriced run, the partial lower-bound wording
    for a mixed byPhase bucket, and that a fully-priced run still renders the
    plain number."""
    registry = tmp_path / "registry"
    _write_dead_run(registry, "run-unpriced", state="succeeded", usage={
        "costStatus": "unknown", "totalTokens": 900,
        "byPhase": {"worker": {"costStatus": "unknown", "totalTokens": 900}},
    })
    _write_dead_run(registry, "run-mixed", state="succeeded", usage={
        "costUSD": 0.25, "costStatus": "partial", "totalTokens": 900,
        "byPhase": {"worker": {"costUSD": 0.25, "costStatus": "partial"},
                    "review": {"costUSD": 1.6, "totalTokens": 100}},
    })
    _write_dead_run(registry, "run-priced", state="succeeded",
                    usage={"costUSD": 14.2, "totalTokens": 900,
                           "byPhase": {"worker": {"costUSD": 14.2}}})

    server = UiServer(registry)
    server.wait_ready()
    try:
        pw.open(f"{server.base}/#/run/run-unpriced")
        panel = _wait_for(pw, "document.getElementById('usage-box').innerText",
                          "unavailable")
        assert "$0.0000" not in panel, panel
        assert "$" not in panel, panel
        n_marked = int(pw.eval_js(
            "document.querySelectorAll('.stat.cost-unknown').length"))
        assert n_marked == 1
        pw.screenshot(SCREENSHOTS_DIR / "11-usage-cost-unavailable.png")

        pw.open(f"{server.base}/#/run/run-mixed")
        panel = _wait_for(pw, "document.getElementById('usage-box').innerText",
                          "partial")
        assert "$0.2500+ (partial, rest unavailable)" in panel, panel
        assert "$1.6000" in panel, panel
        n_marked = int(pw.eval_js(
            "document.querySelectorAll('.stat.cost-partial').length"))
        assert n_marked == 1

        pw.open(f"{server.base}/#/run/run-priced")
        panel = _wait_for(pw, "document.getElementById('usage-box').innerText",
                          "$14.2000")
        assert "unavailable" not in panel, panel
        assert "partial" not in panel, panel
        n_marked = int(pw.eval_js(
            "document.querySelectorAll('.stat.cost-unknown, .stat.cost-partial').length"))
        assert n_marked == 0
    finally:
        server.stop()


def _run_list_order(session: Pw) -> list[str]:
    """The run ids currently rendered in the run-list table, top to bottom."""
    raw = session.eval_js(
        "[...document.querySelectorAll('table.run-list tbody tr td:first-child a')]"
        ".map(a => a.textContent).join(',')").strip('"')
    return [x for x in raw.split(",") if x]


def _sort_header_text(session: Pw, key: str) -> str:
    return session.eval_js(
        f"document.querySelector('th[data-sort-key=\\\"{key}\\\"]').textContent"
    ).strip('"')


def test_run_list_is_sortable_and_defaults_to_newest_first(tmp_path, pw):
    """Task 054 (#9): every run-list column sorts on click, reversibly, with
    an indicator, and the default order is STARTED descending -- NOT the
    run-id alphabetical order the registry directory listing yields.

    The fixtures are built so that a naive implementation fails visibly:

    * `ccc-offset` / `ddd-mid` are ordered differently by ISO *string* than
      by the instants those strings denote (one carries a +05:00 offset), so
      sorting the rendered/raw text instead of the parsed epoch flips them;
    * the ITERATIONS cells ("9/10", "17/250", "3/25", "100/250") sort
      completely differently as text than `iterationsUsed` does as a number;
    * STATE/VERDICT lifecycle order (running -> succeeded -> failed ->
      aborted) is not alphabetical order.

    Finally the chosen sort must survive the 4s `load()` rebuild, which is
    why `app.js` keeps it in a module-level variable rather than in the DOM.
    """
    registry = tmp_path / "registry"
    # alphabetical run-id order is aaa, bbb, ccc, ddd -- deliberately not
    # any of the orders asserted below except the explicit RUN sort.
    _write_dead_run(registry, "aaa-newest", state="running", verdict=None,
                    phase="worker", approach=2, iterationsUsed=9,
                    iterationsBudget=10, startedAt="2026-01-05T00:00:00Z")
    _write_dead_run(registry, "bbb-oldest", state="succeeded", verdict="verified",
                    phase="review", approach=1, iterationsUsed=17,
                    iterationsBudget=250, startedAt="2026-01-01T00:00:00Z")
    # 2026-01-04T02:00:00+05:00 == 2026-01-03T21:00:00Z: EARLIER than
    # ddd-mid's 23:00Z although its ISO text sorts later.
    _write_dead_run(registry, "ccc-offset", state="failed", verdict="unverified",
                    phase="worker", approach=1, iterationsUsed=3,
                    iterationsBudget=25, startedAt="2026-01-04T02:00:00+05:00")
    _write_dead_run(registry, "ddd-mid", state="aborted", verdict="unverified",
                    phase="verify", approach=3, iterationsUsed=100,
                    iterationsBudget=250, startedAt="2026-01-03T23:00:00Z")

    server = UiServer(registry)
    server.wait_ready()
    try:
        pw.open(server.base)
        _wait_for(pw, "document.body.innerText", "ddd-mid")

        # default: STARTED descending, computed from the parsed instants
        newest_first = ["aaa-newest", "ddd-mid", "ccc-offset", "bbb-oldest"]
        assert _run_list_order(pw) == newest_first, _run_list_order(pw)
        assert "\u25BC" in _sort_header_text(pw, "startedAt")
        assert pw.eval_js(
            "document.querySelector('th[data-sort-key=\\\"startedAt\\\"]')"
            ".getAttribute('aria-sort')").strip('"') == "descending"
        pw.screenshot(SCREENSHOTS_DIR / "12-run-list-sorted.png")

        # reversible: same column, other direction
        pw.click('th[data-sort-key="startedAt"]')
        _wait_for(pw, "document.body.innerText", "\u25B2")
        assert _run_list_order(pw) == list(reversed(newest_first)), _run_list_order(pw)

        # ITERATIONS sorts numerically on iterationsUsed, not on "17/250"
        pw.click('th[data-sort-key="iterationsUsed"]')
        _wait_for(pw, "document.body.innerText", "\u25BC")
        assert _run_list_order(pw) == ["ddd-mid", "bbb-oldest", "aaa-newest",
                                       "ccc-offset"], _run_list_order(pw)
        pw.click('th[data-sort-key="iterationsUsed"]')
        _wait_for(pw, "document.body.innerText", "\u25B2")
        assert _run_list_order(pw) == ["ccc-offset", "aaa-newest", "bbb-oldest",
                                       "ddd-mid"], _run_list_order(pw)

        # STATE / VERDICT sort in lifecycle order, not alphabetically
        pw.click('th[data-sort-key="state"]')
        lifecycle = ["aaa-newest", "bbb-oldest", "ccc-offset", "ddd-mid"]
        deadline = time.time() + 10
        while time.time() < deadline and _run_list_order(pw) != lifecycle:
            time.sleep(0.2)
        assert _run_list_order(pw) == lifecycle, _run_list_order(pw)
        assert "\u25B2" in _sort_header_text(pw, "state")

        pw.click('th[data-sort-key="verdict"]')
        verdict_order = ["aaa-newest", "ccc-offset", "ddd-mid", "bbb-oldest"]
        deadline = time.time() + 10
        while time.time() < deadline and _run_list_order(pw) != verdict_order:
            time.sleep(0.2)
        assert _run_list_order(pw) == verdict_order, _run_list_order(pw)

        # RUN column: plain alphabetical, reversible
        pw.click('th[data-sort-key="runId"]')
        alphabetical = ["aaa-newest", "bbb-oldest", "ccc-offset", "ddd-mid"]
        deadline = time.time() + 10
        while time.time() < deadline and _run_list_order(pw) != alphabetical:
            time.sleep(0.2)
        assert _run_list_order(pw) == alphabetical, _run_list_order(pw)
        pw.click('th[data-sort-key="runId"]')
        deadline = time.time() + 10
        while time.time() < deadline and _run_list_order(pw) != list(reversed(alphabetical)):
            time.sleep(0.2)
        assert _run_list_order(pw) == list(reversed(alphabetical)), _run_list_order(pw)

        # ...and the choice survives the periodic full-table rebuild
        # (REFRESH_MS = 4000 in app.js): sort state lives outside the DOM.
        time.sleep(5.5)
        assert _run_list_order(pw) == list(reversed(alphabetical)), _run_list_order(pw)
        assert "\u25BC" in _sort_header_text(pw, "runId")
        assert "\u25B2" not in _sort_header_text(pw, "startedAt")
    finally:
        server.stop()


def test_run_detail_opens_the_prd_in_a_dialog(tmp_path, pw):
    """Task 056 (#1): the run's PRD is one click away from its detail page,
    for a LIVE run (proxied `GET /prd`) and for a DEAD one (the on-disk
    fallback, consistent with the log tail's snapshot behaviour in task 039).

    Also pins the rendering discipline in the browser rather than by grep:
    the PRD text contains markup and an inline `<script>`, so if the dialog
    ever went through `innerHTML` the literal characters would vanish from
    `textContent` and a `script` element would appear inside the dialog.
    """
    registry = tmp_path / "registry"
    dead_prd = ("# Dead run PRD\n\n"
                "Ship <b>the thing</b> & do not <script>alert(1)</script>.\n")
    live_prd = "# Live run PRD\n\nKeep the endpoint honest.\n"

    dead = _write_dead_run(registry, "run-dead-prd", state="failed",
                           verdict="unverified")
    (dead / "prd.md").write_text(dead_prd)

    engine = StubEngineApi(
        status={"runId": "run-live-prd", "state": "running", "phase": "worker",
                "approach": 1, "iterationsUsed": 2, "iterationsBudget": 25,
                "startedAt": "2026-01-01T00:00:00Z"},
        prd=live_prd)
    live = _write_run_with_api(registry, "run-live-prd", engine, state="running")
    # a stale on-disk copy must NOT be what the live run's dialog shows
    (live / "prd.md").write_text("stale on-disk copy of the live run's PRD\n")

    server = UiServer(registry)
    server.wait_ready()
    try:
        # -- dead run: on-disk fallback, labelled as a snapshot ------------
        pw.open(f"{server.base}/#/run/run-dead-prd")
        _wait_for(pw, "document.body.innerText", "view PRD")
        assert int(pw.eval_js("document.querySelectorAll('dialog.text-dialog').length")) == 0
        pw.click("button.open-prd")
        body = _wait_for(pw, "document.querySelector('.text-dialog').textContent",
                         "Dead run PRD")
        assert "Ship <b>the thing</b> & do not <script>alert(1)</script>." in body, body
        assert "on-disk snapshot" in body, body
        # textContent-only discipline: nothing was parsed as markup
        assert int(pw.eval_js(
            "document.querySelectorAll('.text-dialog script, .text-dialog b').length")) == 0
        assert pw.eval_js("document.querySelector('dialog.text-dialog').open") == "true"
        pw.screenshot(SCREENSHOTS_DIR / "12-prd-dialog-dead-run.png")

        # closing removes it (no stale copies piling up behind the 4s refresh)
        pw.click(".text-dialog .dialog-close button")
        deadline = time.time() + 10
        while time.time() < deadline:
            if pw.eval_js("document.querySelectorAll('dialog.text-dialog').length") == "0":
                break
            time.sleep(0.2)
        assert pw.eval_js("document.querySelectorAll('dialog.text-dialog').length") == "0"

        # -- live run: proxied from the container, no snapshot label -------
        pw.open(f"{server.base}/#/run/run-live-prd")
        _wait_for(pw, "document.body.innerText", "view PRD")
        pw.click("button.open-prd")
        body = _wait_for(pw, "document.querySelector('.text-dialog').textContent",
                         "Live run PRD")
        assert "Keep the endpoint honest." in body, body
        assert "stale on-disk copy" not in body, body
        assert "on-disk snapshot" not in body, body
        pw.screenshot(SCREENSHOTS_DIR / "13-prd-dialog-live-run.png")
    finally:
        server.stop()
        engine.close()


def test_run_detail_opens_a_task_in_a_dialog(tmp_path, pw):
    """Task 057 (#2): the plan rows in the run-detail view are clickable and
    open that task's detail -- status, successCriteria, dependsOn, priority --
    so an operator can read the criteria a task is being judged against
    without opening the run dir's tasks.json by hand.

    Also pins the rendering discipline the same way task 056's PRD dialog
    does: the criteria text contains markup and an inline `<script>`, so if
    the dialog ever went through `innerHTML` those literal characters would
    vanish from `textContent` and real elements would appear inside it.
    """
    registry = tmp_path / "registry"
    criteria_two = ("`pytest -q tests/test_two.py` green and <b>no</b> "
                    "<script>alert(1)</script> regressions.")
    tasks = {"tasks": [
        {"id": "001", "title": "First task", "status": "completed",
         "successCriteria": "The first thing is shipped and covered by a test."},
        {"id": "002", "title": "Second task", "status": "in-progress",
         "priority": 7, "dependsOn": ["001"], "successCriteria": criteria_two},
    ]}

    dead = _write_dead_run(registry, "run-tasks", state="running", verdict=None)
    (dead / "tasks.json").write_text(json.dumps(tasks))

    server = UiServer(registry)
    server.wait_ready()
    try:
        pw.open(f"{server.base}/#/run/run-tasks")
        _wait_for(pw, "document.body.innerText", "Second task")
        assert int(pw.eval_js("document.querySelectorAll('tr.task-row').length")) == 2
        assert int(pw.eval_js("document.querySelectorAll('dialog.text-dialog').length")) == 0

        # -- the second task: criteria plus its scheduling fields ----------
        pw.click('tr.task-row[data-task-id="002"]')
        body = _wait_for(pw, "document.querySelector('.text-dialog').textContent",
                         "Second task")
        assert criteria_two in body, body
        assert "status: in-progress" in body, body
        assert "priority: 7" in body, body
        assert "dependsOn: 001" in body, body
        # ...and only that task's criteria
        assert "The first thing is shipped" not in body, body
        assert int(pw.eval_js(
            "document.querySelectorAll('.text-dialog script, .text-dialog b').length")) == 0
        assert pw.eval_js("document.querySelector('dialog.text-dialog').open") == "true"
        pw.screenshot(SCREENSHOTS_DIR / "14-task-dialog.png")

        pw.click(".text-dialog .dialog-close button")
        deadline = time.time() + 10
        while time.time() < deadline:
            if pw.eval_js("document.querySelectorAll('dialog.text-dialog').length") == "0":
                break
            time.sleep(0.2)
        assert pw.eval_js("document.querySelectorAll('dialog.text-dialog').length") == "0"

        # -- a second task opens its OWN detail (one dialog at a time) -----
        pw.click('tr.task-row[data-task-id="001"]')
        body = _wait_for(pw, "document.querySelector('.text-dialog').textContent",
                         "First task")
        assert "The first thing is shipped and covered by a test." in body, body
        assert "status: completed" in body, body
        assert "priority:" not in body, body
        assert int(pw.eval_js("document.querySelectorAll('dialog.text-dialog').length")) == 1
    finally:
        server.stop()


# --------------------------------------------------------------------------
# task 005 (#15): a stale task read is labelled, and the table never blinks
# --------------------------------------------------------------------------

STALE_TASKS_PLAN = {
    "version": 1,
    "goal": "ship it",
    "tasks": [
        {"id": "001", "title": "first task", "status": "completed"},
        {"id": "002", "title": "second task", "status": "in-progress"},
        {"id": "003", "title": "third task", "status": "pending"},
    ],
}
# A real mid-write snapshot: pi has truncated tasks.json and not yet finished
# writing the new plan (same fixture text as tests/test_tasks_stale_cli.py).
TRUNCATED_TASKS = '{"version": 1, "goal": "ship it", "tasks": [{"id": "001", "sta'

_STALE_ROWS = "document.querySelectorAll('#task-box tbody tr').length"
_STALE_LABEL_COUNT = "document.querySelectorAll('#tasks-stale').length"
_STALE_LABEL_TEXT = (
    "(document.getElementById('tasks-stale') || {}).textContent || '(none)'")
_STALE_LABEL_SOURCE = (
    "(document.getElementById('tasks-stale') || {dataset: {}})"
    ".dataset.tasksSource || '(none)'")


def test_run_detail_labels_a_stale_task_read_and_never_blinks_empty(tmp_path, pw):
    """Task 005 (#15): the browser half of the hardened task read.

    `tasks.json` is written by *pi*, non-atomically, while the hub polls every
    4 s -- so the pre-#15 run-detail view rendered `(no tasks)` for a whole
    cycle whenever a poll landed inside a rewrite, and (worse) said nothing
    about it. Tasks 002-004 made the reader serve the last plan that parsed,
    flagged `tasksStale`/`tasksSource`; this asserts the browser both KEEPS
    showing that plan and LABELS it, so an operator is never shown stale rows
    as if they were current, and never shown an empty plan that isn't one.

    Drives the real failure mode: an agent-style truncate+rewrite loop across
    several poll cycles, then a file left truncated so a poll is guaranteed to
    land on it, then a healthy rewrite which must clear the label again.
    """
    from ralphd.engine.state import TASKS_STALE_LABEL, TASKS_STALE_NOTICE

    registry = tmp_path / "registry"
    run_dir = _write_dead_run(registry, "run-stale-tasks", state="running", verdict=None)
    tasks_path = run_dir / "tasks.json"
    tasks_path.write_text(json.dumps(STALE_TASKS_PLAN))

    server = UiServer(registry)
    server.wait_ready()
    try:
        pw.open(f"{server.base}/#/run/run-stale-tasks")
        _wait_for(pw, "document.body.innerText", "third task")
        assert int(pw.eval_js(_STALE_ROWS)) == 3
        # a healthy read is not labelled: the badge must mean something.
        assert int(pw.eval_js(_STALE_LABEL_COUNT)) == 0

        # -- phase 1: the rewrite loop, sampled far faster than the poll ---
        row_counts = set()
        deadline = time.time() + 10
        flip = 0
        while time.time() < deadline:
            tasks_path.write_text(
                TRUNCATED_TASKS if flip % 2 else json.dumps(STALE_TASKS_PLAN))
            flip += 1
            row_counts.add(int(pw.eval_js(_STALE_ROWS)))
            time.sleep(0.25)
        assert row_counts == {3}, row_counts

        # -- phase 2: left truncated, so staleness is certain, not lucky ---
        tasks_path.write_text(TRUNCATED_TASKS)
        text = _wait_for(pw, _STALE_LABEL_TEXT, TASKS_STALE_LABEL, timeout=20)
        assert TASKS_STALE_NOTICE in text, text
        assert "last-good" in pw.eval_js(_STALE_LABEL_SOURCE)
        assert int(pw.eval_js("document.querySelectorAll('#tasks-stale .pill-stale').length")) == 1
        # the point of the label: the rows are still there, still all three.
        assert int(pw.eval_js(_STALE_ROWS)) == 3
        # text nodes only, like every other payload the hub renders.
        assert int(pw.eval_js(
            "document.querySelectorAll('#tasks-stale script, #tasks-stale b').length")) == 0
        pw.screenshot(SCREENSHOTS_DIR / "15-stale-tasks-label.png")

        # -- phase 3: a finished write clears the label -------------------
        tasks_path.write_text(json.dumps(STALE_TASKS_PLAN))
        cleared = False
        deadline = time.time() + 20
        while time.time() < deadline:
            assert int(pw.eval_js(_STALE_ROWS)) == 3
            if int(pw.eval_js(_STALE_LABEL_COUNT)) == 0:
                cleared = True
                break
            time.sleep(0.3)
        assert cleared, "stale label never cleared after tasks.json parsed again"
    finally:
        server.stop()


_APPROACH_CELLS = (
    "[...document.querySelectorAll('table.run-list tbody tr')]"
    ".map(tr => tr.children[0].textContent + '=' + tr.children[4].textContent)"
    ".join(',')")

_DETAIL_APPROACH_LINE = (
    "[...document.querySelectorAll('div')].filter(d => d.firstChild"
    " && d.firstChild.tagName === 'B'"
    " && d.firstChild.textContent === 'approach: ')"
    ".map(d => d.textContent).join('|')")


def _approach_cells(session: Pw) -> dict:
    raw = session.eval_js(_APPROACH_CELLS).strip('"')
    return dict(pair.split("=", 1) for pair in raw.split(",") if "=" in pair)


def test_run_list_and_detail_render_the_approach_denominator(tmp_path, pw):
    """Task 008 (#16): the browser half of the approach counter.

    #16's complaint is that `approach 2` alone says nothing about how much of
    the review ladder is left, so both hub surfaces must show `n/m` -- the
    run-list APPROACH cell and the run-detail summary row -- rendered from the
    string the server formatted with the one shared formatter
    (`engine.state.format_approach`, via `ui_server._with_approach_display`),
    never a second JS spelling of it.

    All three honest renderings are asserted from an ON-DISK snapshot with no
    container at all (the state in which an operator most often reads these
    pages): `10/12`, a bare `2` where the run dir records no `maxApproaches`,
    and an empty cell for a run that never entered the ladder -- never `/3`.

    The column must also still sort on the raw numerator: the fixtures are
    picked so a string sort of the rendered cells ("10/12" < "2/3") puts
    approach 10 in the wrong place.
    """
    registry = tmp_path / "registry"
    _write_dead_run(registry, "aaa-ten", state="running", verdict=None,
                    phase="worker", approach=10, maxApproaches=12,
                    startedAt="2026-01-04T00:00:00Z")
    _write_dead_run(registry, "bbb-two", state="failed", verdict="unverified",
                    phase="worker", approach=2, maxApproaches=3,
                    startedAt="2026-01-03T00:00:00Z")
    # pre-v0.6 run dir: approach recorded, limit never was
    _write_dead_run(registry, "ccc-bare", state="succeeded", verdict="verified",
                    phase="review", approach=2,
                    startedAt="2026-01-02T00:00:00Z")
    _write_dead_run(registry, "ddd-none", state="failed", verdict="unverified",
                    phase="planning", approach=None, maxApproaches=3,
                    startedAt="2026-01-01T00:00:00Z")

    server = UiServer(registry)
    server.wait_ready()
    try:
        pw.open(server.base)
        _wait_for(pw, "document.body.innerText", "ddd-none")

        cells = _approach_cells(pw)
        assert cells == {"aaa-ten": "10/12", "bbb-two": "2/3",
                         "ccc-bare": "2", "ddd-none": ""}, cells
        assert "/" not in cells["ccc-bare"]
        assert "None" not in pw.eval_js("document.body.innerText")
        pw.screenshot(SCREENSHOTS_DIR / "16-approach-denominator.png")

        # the APPROACH column still sorts on the raw numerator: descending is
        # 10 before 2, which a sort of the cell TEXT would get wrong.
        pw.click('th[data-sort-key="approach"]')
        desc = ["ddd-none", "aaa-ten", "bbb-two", "ccc-bare"]
        deadline = time.time() + 10
        while time.time() < deadline and _run_list_order(pw) != desc:
            time.sleep(0.2)
        assert _run_list_order(pw) == desc, _run_list_order(pw)
        assert "\u25BC" in _sort_header_text(pw, "approach")
        # ...and reversibly, with the run that never entered the ladder last
        pw.click('th[data-sort-key="approach"]')
        asc = ["bbb-two", "ccc-bare", "aaa-ten", "ddd-none"]
        deadline = time.time() + 10
        while time.time() < deadline and _run_list_order(pw) != asc:
            time.sleep(0.2)
        assert _run_list_order(pw) == asc, _run_list_order(pw)
        # the cells survived the re-render unchanged
        assert _approach_cells(pw)["aaa-ten"] == "10/12"

        # -- run detail: the same three renderings -------------------------
        for run_id, expected in [("aaa-ten", "10/12"), ("ccc-bare", "2"),
                                 ("ddd-none", "")]:
            pw.open(f"{server.base}/#/run/{run_id}")
            _wait_for(pw, "document.body.innerText", "on-disk snapshot")
            line = _wait_for(pw, _DETAIL_APPROACH_LINE, "approach: ")
            assert line.strip('"') == "approach: " + expected, (run_id, line)
        pw.screenshot(SCREENSHOTS_DIR / "17-approach-detail.png")
    finally:
        server.stop()


# --------------------------------------------------------------------------
# task 014 (#21): the run list's TASKS column
# --------------------------------------------------------------------------

_TASKS_CELLS = (
    "[...document.querySelectorAll('table.run-list tbody tr')]"
    ".map(tr => tr.children[0].textContent + '=' +"
    " tr.querySelector('td.tasks-cell').textContent).join('|')")


def _tasks_cells(session: Pw) -> dict:
    raw = session.eval_js(_TASKS_CELLS).strip('"')
    return dict(pair.split("=", 1) for pair in raw.split("|") if "=" in pair)


def _plan(statuses) -> str:
    return json.dumps({
        "version": 1,
        "goal": "ship it",
        "tasks": [{"id": f"{i:03d}", "title": f"task {i}", "status": s}
                  for i, s in enumerate(statuses, start=1)],
    })


def test_run_list_tasks_column_renders_flags_and_sorts_on_progress(tmp_path, pw):
    """Task 014 (#21): the hub run list has a TASKS column.

    #21's complaint is that the list says a run is `running` without saying
    how far through its plan it is, so the operator has to open each run. The
    column shows the fraction the server rendered (`tasksDisplay`, task 013 ->
    `engine.state.format_task_fraction`) plus the trouble flags in
    `format_task_counts`' exact wording -- never a second JS spelling of
    either.

    Asserted from an ON-DISK snapshot with no container (the fraction must be
    just as available for a finished run as for a live one), for the four
    honest renderings: mid-plan with something in flight, a plan stuck on a
    failed validation, a finished plan, and a run with no plan at all -- which
    is BLANK, never `0/0`, and sorts last ascending because "no plan" is not
    "0% done".

    The fixtures are chosen so the sort can only pass on the completion
    RATIO: sorting the rendered cell text ("1/4" < "100/250" < "2/2" < "5/7")
    or the bare numerator (1, 5, 2, 100) each yields a different, plausible-
    looking order.
    """
    registry = tmp_path / "registry"
    # 5/7 completed, one worker iteration in flight -> flagged in-progress
    run = _write_dead_run(registry, "aaa-mid", state="running", verdict=None,
                          phase="worker", startedAt="2026-01-05T00:00:00Z")
    (run / "tasks.json").write_text(_plan(
        ["completed"] * 5 + ["in-progress", "pending"]))
    # 1/4, and the plan is stuck: a validation-failed task is trouble
    run = _write_dead_run(registry, "bbb-stuck", state="running", verdict=None,
                          phase="verify", startedAt="2026-01-04T00:00:00Z")
    (run / "tasks.json").write_text(_plan(
        ["completed", "validation-failed", "pending", "pending"]))
    # a finished plan: no flags at all
    run = _write_dead_run(registry, "ccc-done", state="succeeded", verdict="verified",
                          phase="review", startedAt="2026-01-03T00:00:00Z")
    (run / "tasks.json").write_text(_plan(["completed", "completed"]))
    # no plan on disk at all (the agent never got to write one)
    run = _write_dead_run(registry, "ddd-planless", state="failed",
                          verdict="unverified", phase="planning",
                          startedAt="2026-01-02T00:00:00Z")
    (run / "tasks.json").unlink()
    # a big plan, 40% done: sorts between 1/4 and 5/7 on ratio only
    run = _write_dead_run(registry, "eee-big", state="running", verdict=None,
                          phase="worker", startedAt="2026-01-01T00:00:00Z")
    (run / "tasks.json").write_text(_plan(["completed"] * 100 + ["pending"] * 150))

    server = UiServer(registry)
    server.wait_ready()
    try:
        pw.open(server.base)
        _wait_for(pw, "document.body.innerText", "eee-big")
        assert "TASKS" in _sort_header_text(pw, "tasks")

        cells = _tasks_cells(pw)
        assert cells == {
            "aaa-mid": "5/7 \u26a0 1 in-progress",
            "bbb-stuck": "1/4 \u26a0 1 validation-failed",
            "ccc-done": "2/2",
            "ddd-planless": "",
            "eee-big": "100/250",
        }, cells
        # never `0/0` for the run with no plan, and no invented denominator
        assert "0/0" not in pw.eval_js("document.body.innerText")
        # text nodes only, like every other payload the hub renders
        assert int(pw.eval_js(
            "document.querySelectorAll('td.tasks-cell script, td.tasks-cell b')"
            ".length")) == 0
        pw.screenshot(SCREENSHOTS_DIR / "18-tasks-column.png")

        # first click = ascending: least-complete first, plan-less LAST
        pw.click('th[data-sort-key="tasks"]')
        asc = ["bbb-stuck", "eee-big", "aaa-mid", "ccc-done", "ddd-planless"]
        deadline = time.time() + 10
        while time.time() < deadline and _run_list_order(pw) != asc:
            time.sleep(0.2)
        assert _run_list_order(pw) == asc, _run_list_order(pw)
        assert "\u25B2" in _sort_header_text(pw, "tasks")

        # ...and reversibly
        pw.click('th[data-sort-key="tasks"]')
        desc = ["ddd-planless", "ccc-done", "aaa-mid", "eee-big", "bbb-stuck"]
        deadline = time.time() + 10
        while time.time() < deadline and _run_list_order(pw) != desc:
            time.sleep(0.2)
        assert _run_list_order(pw) == desc, _run_list_order(pw)
        assert "\u25BC" in _sort_header_text(pw, "tasks")
        # the cells survived the re-render unchanged
        assert _tasks_cells(pw)["eee-big"] == "100/250"

        # the flags are the summary sentence's own wording: the full sentence
        # is on the cell (hover), and the flag text appears verbatim inside it
        summary = pw.eval_js(
            "[...document.querySelectorAll('table.run-list tbody tr')]"
            ".filter(tr => tr.children[0].textContent === 'aaa-mid')"
            ".map(tr => tr.querySelector('td.tasks-cell').getAttribute('title'))"
            ".join('')").strip('"')
        assert summary == "5/7 completed (1 in-progress, 1 pending)", summary
        assert "1 in-progress" in summary
    finally:
        server.stop()


# --------------------------------------------------------------------------
# task 017 (#17): the run-detail steering history panel
# --------------------------------------------------------------------------

_STEERING_ITEMS = "document.querySelectorAll('.steering-item').length"
_STEERING_ROW_TEXT = (
    "[...document.querySelectorAll('.steering-item')]"
    ".map(e => e.dataset.steeringState + ' ' + e.textContent).join(' | ')")
_STEERING_STATES = (
    "[...document.querySelectorAll('.steering-item')]"
    ".map(e => e.dataset.steeringState).join(',')")
_DIALOGS = "document.querySelectorAll('dialog.text-dialog').length"


def test_run_detail_lists_steering_history_from_the_on_disk_snapshot(tmp_path, pw):
    """Task 017 (#17): the hub's steering panel, for a run whose container is
    gone -- the case that made #17 painful, since a finished run's steering
    history was unreachable from every surface.

    Pins the three things the panel exists to state (name, arrival time,
    pending-vs-applied), the rendering discipline (a message body containing
    markup and an inline `<script>` must survive as TEXT: the same reason the
    PRD dialog never touches `innerHTML`), and that the shared single dialog
    does not stack across the 4s `load()` rebuild behind it.
    """
    from ralphd.engine.state import STEERING_CONSUMED_FILE, format_local_time, steering_entries

    registry = tmp_path / "registry"
    applied_body = ("Stop deriving cost from <b>zero</b> quotes & "
                    "do not <script>alert(1)</script>.\n")
    pending_body = "Also: prefix every commit with `task NNN:`.\n"

    run_dir = _write_dead_run(registry, "run-steer-hist", state="succeeded",
                              verdict="verified")
    sdir = run_dir / "steering"
    sdir.mkdir(parents=True, exist_ok=True)
    (sdir / "001-cost-zero-quote.md").write_text(applied_body)
    (sdir / "002-commit-prefix.md").write_text(pending_body)
    (sdir / STEERING_CONSUMED_FILE).write_text(json.dumps(["001-cost-zero-quote.md"]))
    # a run nobody ever steered, for the notice
    _write_dead_run(registry, "run-unsteered", state="failed", verdict="unverified")

    arrived = format_local_time(steering_entries(run_dir)[0]["ts"])

    server = UiServer(registry)
    server.wait_ready()
    try:
        pw.open(f"{server.base}/#/run/run-steer-hist")
        _wait_for(pw, "document.body.innerText", "Steering history")
        _wait_for_count_ge(pw, _STEERING_ITEMS, 2)

        rows = pw.eval_js(_STEERING_ROW_TEXT)
        # oldest first, as the engine numbered them; each row carries the
        # state, the operator's own name, the server-formatted arrival time
        # and the file name
        assert rows.index("applied") < rows.index("pending"), rows
        assert "cost-zero-quote" in rows and "commit-prefix" in rows, rows
        assert "001-cost-zero-quote.md" in rows, rows
        assert arrived in rows, (arrived, rows)
        assert pw.eval_js(_STEERING_STATES).strip('"') == "applied,pending"
        assert int(pw.eval_js(_DIALOGS)) == 0
        pw.screenshot(SCREENSHOTS_DIR / "19-steering-history.png")

        # -- the body opens in THE shared dialog, as text ------------------
        pw.click('.steering-item[data-steering-file="001-cost-zero-quote.md"]')
        body = _wait_for(pw, "document.querySelector('.text-dialog').textContent",
                         "Stop deriving cost")
        assert "Stop deriving cost from <b>zero</b> quotes & " \
               "do not <script>alert(1)</script>." in body, body
        assert "state: applied" in body, body
        assert arrived in body, body
        assert "on-disk snapshot" in body, body
        # ...and only that message
        assert "commit-prefix" not in body and "task NNN" not in body, body
        # text nodes only: nothing was parsed as markup
        assert int(pw.eval_js(
            "document.querySelectorAll('.text-dialog script, .text-dialog b').length")) == 0
        assert pw.eval_js("document.querySelector('dialog.text-dialog').open") == "true"
        pw.screenshot(SCREENSHOTS_DIR / "20-steering-dialog.png")

        # -- it survives a full 4s poll without stacking -------------------
        time.sleep(5.0)
        assert int(pw.eval_js(_STEERING_ITEMS)) == 2       # the panel re-rendered
        assert int(pw.eval_js(_DIALOGS)) == 1              # ...behind ONE dialog
        assert pw.eval_js("document.querySelector('dialog.text-dialog').open") == "true"

        pw.click(".text-dialog .dialog-close button")
        deadline = time.time() + 10
        while time.time() < deadline and pw.eval_js(_DIALOGS) != "0":
            time.sleep(0.2)
        assert int(pw.eval_js(_DIALOGS)) == 0

        # the second message opens its OWN body (one dialog at a time)
        pw.click('.steering-item[data-steering-file="002-commit-prefix.md"]')
        body = _wait_for(pw, "document.querySelector('.text-dialog').textContent",
                         "task NNN")
        assert "state: pending" in body, body
        assert "Stop deriving cost" not in body, body
        assert int(pw.eval_js(_DIALOGS)) == 1

        # -- a run nobody steered says so, in the server's wording ---------
        from ralphd.cli.ui_server import NO_STEERING
        pw.open(f"{server.base}/#/run/run-unsteered")
        notice = _wait_for(pw,
                           "(document.getElementById('steering-notice') || {})"
                           ".textContent || ''", NO_STEERING)
        assert NO_STEERING in notice
        assert int(pw.eval_js(_STEERING_ITEMS)) == 0
    finally:
        server.stop()


def test_steering_entry_appears_pending_then_flips_to_applied(tmp_path, pw, live):
    """Task 017 (#17) against a REAL engine: the panel's whole point is that a
    queued message is visibly *pending* until the loop reads it at an
    iteration boundary, and *applied* afterwards -- so an operator can tell
    "the agent has not seen this yet" from "the agent has it".

    Steered through the hub's own form (the write surface #17 says was the
    only surface), then watched through the same page.
    """
    run = live(run_id="steer-hub-ui",
               job={"iterations": 8, "on_complete": "idle"},
               stub_env={"STUB_SLEEP": "1", "STUB_TASKS": "4"})
    run.wait_api()

    server = UiServer(run.registry)
    server.wait_ready()
    try:
        pw.open(f"{server.base}/#/run/{run.run_id}")
        _wait_for(pw, "document.body.innerText", "Steering history")

        pw.fill("#steer-message", "Rendered pending, then applied.")
        pw.fill("#steer-name", "from-browser")
        pw.click("#steer-form button[type=submit]")
        _wait_for(pw, "document.getElementById('steer-status').textContent", "sent")

        # appears within one poll cycle, flagged pending
        _wait_for(pw, _STEERING_STATES, "pending", timeout=20)
        rows = pw.eval_js(_STEERING_ROW_TEXT)
        assert "from-browser" in rows, rows
        pw.screenshot(SCREENSHOTS_DIR / "21-steering-pending.png")

        # ...and flips to applied once the loop consumes it at a boundary
        _wait_for(pw, _STEERING_STATES, "applied", timeout=90)
        assert int(pw.eval_js(_STEERING_ITEMS)) == 1
        pw.screenshot(SCREENSHOTS_DIR / "22-steering-applied.png")

        # the body is the message the operator typed, from the live run
        pw.click(".steering-item")
        body = _wait_for(pw, "document.querySelector('.text-dialog').textContent",
                         "Rendered pending")
        assert "state: applied" in body, body
        assert "on-disk snapshot" not in body, body      # this run is live
    finally:
        run.wait_terminal(timeout=120)
        server.stop()


_TIMELINE_ITEMS = "document.querySelectorAll('.timeline-item').length"
_TIMELINE_CLICKABLE = "document.querySelectorAll('.timeline-clickable[role=\"button\"]').length"


def test_timeline_row_opens_the_iteration_dialog(tmp_path, pw):
    """Task 020 (#18.1): clicking a timeline row opens THAT iteration's own
    story -- phase, timestamps, duration, exit reason, tokens, cost and its
    full log -- in the single shared text dialog.

    Everything asserted here is a string Python formatted (`ui_server.
    iteration_view` -> `state.iteration_summary_lines` + the shared
    `log_render`), so the hub cannot word an exit reason or a cost differently
    from `ralphctl iteration`; the fixture has no container at all, since the
    endpoint is on-disk by design. Also pins the rendering discipline (a
    transcript containing markup stays TEXT) and that the dialog does not
    stack across the 4s `load()` rebuild behind it.
    """
    from ralphd.cli.ui_server import iteration_view
    from ralphd.engine.state import EXIT_REASON_CLEAN, format_local_time

    registry = tmp_path / "registry"
    run_dir = _write_dead_run(registry, "run-iter-dialog", state="failed",
                              verdict="unverified")
    metas = {
        1: {"number": 1, "phase": "planning", "model": "stub-model", "approach": 1,
            "startedAt": "2026-09-03T09:00:00Z", "endedAt": "2026-09-03T09:04:10Z",
            "exitCode": 0, "error": None,
            "usage": {"input": 10, "output": 20, "totalTokens": 30,
                      "costUSD": 0.0125, "costPriced": True}},
        2: {"number": 2, "phase": "worker", "model": "stub-model", "approach": 2,
            "startedAt": "2026-09-03T09:05:00Z", "endedAt": "2026-09-03T09:22:51Z",
            "exitCode": 7, "error": "pi exited with a broken pipe",
            "usage": {"input": 18, "output": 2118, "totalTokens": 180661,
                      "costUSD": 0.4231, "costPriced": True}},
    }
    texts = {1: "planning transcript line",
             # markup in the transcript must survive as text, like a PRD body
             2: "worker said <b>done</b> and <script>alert(1)</script>"}
    for n, meta in metas.items():
        d = run_dir / "iterations" / f"{n:04d}"
        d.mkdir(parents=True)
        (d / "meta.json").write_text(json.dumps(meta))
        (d / "output.jsonl").write_text(json.dumps(
            {"type": "message_end",
             "message": {"content": [{"type": "text", "text": texts[n]}]}}) + "\n")

    server = UiServer(registry)
    server.wait_ready()
    try:
        pw.open(f"{server.base}/#/run/run-iter-dialog")
        _wait_for_count_ge(pw, _TIMELINE_ITEMS, 2)
        assert int(pw.eval_js(_TIMELINE_CLICKABLE)) == 2
        assert int(pw.eval_js(_DIALOGS)) == 0

        # -- the failed worker iteration -----------------------------------
        pw.click('.timeline-item[data-iteration="2"]')
        body = _wait_for(pw, "document.querySelector('.text-dialog').textContent",
                         "iteration: 2")
        title = pw.eval_js("document.querySelector('.text-dialog .dialog-title')"
                           ".textContent")
        assert "Iteration #2" in title and "run-iter-dialog" in title, title
        assert "phase worker" in body and "approach 2" in body, body
        assert format_local_time(metas[2]["startedAt"]) in body, body
        assert format_local_time(metas[2]["endedAt"]) in body, body
        assert "duration:  17m 51s  (total)" in body, body
        assert "error (exit 7): pi exited with a broken pipe" in body, body
        assert "180,661 total (in 18, out 2,118)" in body, body
        assert "$0.4231" in body, body
        assert "worker said <b>done</b> and <script>alert(1)</script>" in body, body
        # ...and only THIS iteration's log
        assert "planning transcript line" not in body, body
        # every line came from the server's own `text`, verbatim
        raw = pw.eval_js("document.querySelector('.text-dialog .dialog-body')"
                         ".textContent")
        dialog_body = json.loads(raw) if raw.startswith('"') else raw
        assert dialog_body == iteration_view(registry, "run-iter-dialog", 2)["text"]
        # text nodes only: nothing in the transcript was parsed as markup
        assert int(pw.eval_js(
            "document.querySelectorAll('.text-dialog script, .text-dialog b').length")) == 0
        pw.screenshot(SCREENSHOTS_DIR / "23-iteration-dialog.png")

        # -- it survives a full 4s poll without stacking -------------------
        time.sleep(5.0)
        assert int(pw.eval_js(_TIMELINE_ITEMS)) == 2       # timeline re-rendered
        assert int(pw.eval_js(_DIALOGS)) == 1              # ...behind ONE dialog
        assert pw.eval_js("document.querySelector('dialog.text-dialog').open") == "true"

        pw.click(".text-dialog .dialog-close button")
        deadline = time.time() + 10
        while time.time() < deadline and pw.eval_js(_DIALOGS) != "0":
            time.sleep(0.2)
        assert int(pw.eval_js(_DIALOGS)) == 0

        # -- the other row opens its OWN iteration (keyboard-reachable) ----
        pw.eval_js("document.querySelector('.timeline-item[data-iteration=\"1\"]')"
                   ".focus() || 'focused'")
        assert pw.eval_js(
            "document.activeElement.getAttribute('data-iteration')").strip('"') == "1"
        assert pw.run("press", "Enter").returncode == 0
        body = _wait_for(pw, "document.querySelector('.text-dialog').textContent",
                         "iteration: 1")
        assert "phase planning" in body, body
        assert EXIT_REASON_CLEAN in body, body
        assert "planning transcript line" in body, body
        assert "worker said" not in body, body
        assert int(pw.eval_js(_DIALOGS)) == 1
    finally:
        server.stop()


_DOCUMENT_ITEMS = "document.querySelectorAll('.document-item').length"
_DOCUMENT_BUTTONS = "document.querySelectorAll('button.document-item').length"
_DOCUMENT_ROW_TEXT = (
    "[...document.querySelectorAll('.document-item')]"
    ".map(e => e.dataset.document + ' ' + e.textContent).join(' | ')")


def test_run_detail_opens_the_state_document_dialogs(tmp_path, pw):
    """Task 022 (#18.2): the run's own prose -- the worker's handoff notes, the
    reviewer's findings, the composite PRD and the effective `job.yaml` -- is
    one click away on the run detail page, for a run whose container is gone
    (the case that made #18.2 painful: reading them meant knowing the registry
    layout and `cat`-ing files on the host).

    Pins what each dialog shows (the server's own `state.run_document_text`,
    i.e. exactly what `ralphctl docs` prints), that `job.yaml` arrives REDACTED
    (a staged secret value and a masked key name never reach the page), the
    rendering discipline (a notes body full of markup and an inline `<script>`
    survives as TEXT), that a document this run never wrote is stated rather
    than clickable, and that the single shared dialog does not stack across the
    4s `load()` rebuild behind it.
    """
    from ralphd.cli.llm_profiles import MASK
    from ralphd.cli.ui_server import document_view
    from ralphd.engine.state import JOB_CONFIG_FILE, RUN_DOCUMENT_ABSENT

    secret = "ghp_browserDocsSecret0123456789"
    registry = tmp_path / "registry"
    run_dir = _write_dead_run(registry, "run-docs-dialog", state="failed",
                              verdict="unverified")
    notes = ("# Handoff notes\n\nstate: <b>5/7</b> done & "
             "<script>alert(1)</script>\nnext: task 023\n")
    (run_dir / "notes.md").write_text(notes)
    (run_dir / "review-findings.md").write_text(
        "# Review findings\n\nApproach 1 missed requirement C.\n")
    # deliberately NOT written: composite-prd.md (this run never restarted)
    cdir = registry / "configs" / "run-docs-dialog"
    (cdir / "creds").mkdir(parents=True)
    (cdir / "creds" / "github.env").write_text(f"GITHUB_TOKEN={secret}\n")
    (cdir / JOB_CONFIG_FILE).write_text(
        'run_id: "run-docs-dialog"\niterations: 25\n'
        'api_token: "tok_abcdefgh12345678"\n'
        f'on_complete_cmd: "curl -H \'Authorization: Bearer {secret}\' https://ci"\n'
        'env: {"AWS_REGION": "eu-west-1"}\n')

    server = UiServer(registry)
    server.wait_ready()
    try:
        pw.open(f"{server.base}/#/run/run-docs-dialog")
        _wait_for_count_ge(pw, _DOCUMENT_ITEMS, 4)
        rows = pw.eval_js(_DOCUMENT_ROW_TEXT)
        assert "notes notes.md" in rows, rows
        assert "findings review-findings.md" in rows, rows
        assert "job job.yaml" in rows, rows
        # a never-written document is listed, in the server's own wording, and
        # is NOT a button (there is nothing to open)
        assert f"composite-prd composite-prd.md {RUN_DOCUMENT_ABSENT}" in rows, rows
        assert int(pw.eval_js(_DOCUMENT_BUTTONS)) == 3
        assert int(pw.eval_js(
            "document.querySelectorAll('[data-document-absent]').length")) == 1
        assert int(pw.eval_js(_DIALOGS)) == 0

        # -- the worker's notes --------------------------------------------
        pw.click('button.document-item[data-document="notes"]')
        body = _wait_for(pw, "document.querySelector('.text-dialog').textContent",
                         "document:  notes")
        title = pw.eval_js("document.querySelector('.text-dialog .dialog-title')"
                           ".textContent")
        assert "notes.md" in title and "run-docs-dialog" in title, title
        assert "next: task 023" in body, body
        # markup in an agent-authored document stays TEXT, like a PRD body
        assert "<b>5/7</b>" in body and "<script>alert(1)</script>" in body, body
        assert int(pw.eval_js(
            "document.querySelectorAll('.text-dialog script, .text-dialog b').length")) == 0
        # every line came from the server's own `text`, verbatim
        raw = pw.eval_js("document.querySelector('.text-dialog .dialog-body')"
                         ".textContent")
        dialog_body = json.loads(raw) if raw.startswith('"') else raw
        assert dialog_body == document_view(registry, "run-docs-dialog", "notes")["text"]
        assert "missed requirement C" not in body, body
        pw.screenshot(SCREENSHOTS_DIR / "24-document-dialog.png")

        # -- it survives a full 4s poll without stacking -------------------
        time.sleep(5.0)
        assert int(pw.eval_js(_DOCUMENT_ITEMS)) == 4      # panel re-rendered
        assert int(pw.eval_js(_DIALOGS)) == 1             # ...behind ONE dialog
        assert pw.eval_js("document.querySelector('dialog.text-dialog').open") == "true"

        pw.click(".text-dialog .dialog-close button")
        deadline = time.time() + 10
        while time.time() < deadline and pw.eval_js(_DIALOGS) != "0":
            time.sleep(0.2)
        assert int(pw.eval_js(_DIALOGS)) == 0

        # -- job.yaml, redacted, keyboard-reachable ------------------------
        # A <button> gets Enter/Space from the platform -- but the 4s `load()`
        # poll legitimately replaces the panel's nodes, so a focus taken just
        # before one lands is lost through no fault of the button. Hence: take
        # the focus and press Enter, retrying the whole cycle until the dialog
        # opens (what is asserted is that the button IS focusable and that
        # Enter alone opens it -- never a mouse click here).
        focus_js = (
            "(() => { const el = document.querySelector('button.document-item"
            "[data-document=\"job\"]'); el.focus();"
            " return document.activeElement === el ? 'focused' : 'lost'; })()")
        deadline = time.time() + 40
        body = ""
        while time.time() < deadline and "document:  job" not in body:
            if pw.eval_js(focus_js).strip('"') != "focused":
                continue
            assert pw.run("press", "Enter").returncode == 0
            for _ in range(10):
                body = pw.eval_js(
                    "document.querySelector('.text-dialog')"
                    " ? document.querySelector('.text-dialog').textContent : ''")
                if "document:  job" in body:
                    break
                time.sleep(0.2)
        assert "document:  job" in body, body
        assert MASK in body, body
        assert secret not in body, "the staged secret value reached the page"
        assert "tok_abcdefgh12345678" not in body, body
        # ...and the harmless config still readable
        assert "iterations: 25" in body and "eu-west-1" in body, body
        assert int(pw.eval_js(_DIALOGS)) == 1

        # -- the findings open their OWN document ---------------------------
        pw.click(".text-dialog .dialog-close button")
        pw.click('button.document-item[data-document="findings"]')
        body = _wait_for(pw, "document.querySelector('.text-dialog').textContent",
                         "document:  findings")
        assert "missed requirement C" in body, body
        assert "next: task 023" not in body, body
        assert int(pw.eval_js(_DIALOGS)) == 1
    finally:
        server.stop()


_ARTIFACT_ITEMS = "document.querySelectorAll('.artifact-item').length"
_ARTIFACT_ROW_TEXT = (
    "[...document.querySelectorAll('.artifact-item')]"
    ".map(e => e.dataset.artifact + ' [' + e.dataset.artifactKey + '] '"
    " + e.textContent).join(' | ')")


def test_run_detail_browses_artifacts_and_opens_the_reflect_report(tmp_path, pw):
    """Task 024 (#18.3): what the job left behind in `artifacts/` -- above all
    the reflect phase's post-mortem report and the prompt/skill diff it proposes
    -- is browsable from the run detail page and opens in a dialog, for a run
    whose container is gone (the case that made #18.3 painful: reading the
    reflect output meant knowing the registry layout and `cat`-ing files).

    Pins the listing (every file under `artifacts/`, well-known ones labelled
    with the name `ralphctl artifacts show` takes), the dialog payload (the
    server's own `state.artifact_text`, i.e. exactly what `ralphctl artifacts
    <run> show` prints), the rendering discipline (a report full of markup and a
    diff full of `<`/`>` survive as TEXT), that the single shared dialog does not
    stack across the 4s `load()` rebuild behind it, and that a run which left
    nothing behind says so in the server's wording instead of showing an empty
    panel.
    """
    from ralphd.cli.ui_server import artifact_view
    from ralphd.engine.state import NO_ARTIFACTS

    registry = tmp_path / "registry"
    run_dir = _write_dead_run(registry, "run-arts-dialog", state="failed",
                              verdict="unverified")
    _write_dead_run(registry, "run-arts-none", state="succeeded",
                    verdict="verified")
    reflection = run_dir / "artifacts" / "reflection"
    reflection.mkdir(parents=True)
    report = ("# Reflection report\n\nApproach 1 died on <b>requirement C</b> & "
              "<script>alert(1)</script>\nnext: widen the browser tier\n")
    (reflection / "report.md").write_text(report)
    (reflection / "suggestions.diff").write_text(
        "--- a/prompts/worker.md\n+++ b/prompts/worker.md\n"
        "@@ -1,2 +1,3 @@\n Worker prompt\n+Run <every> tier before claiming done.\n")
    (run_dir / "artifacts" / "reports").mkdir()
    (run_dir / "artifacts" / "reports" / "pricing-anomaly.md").write_text(
        "# Pricing anomaly\n\nThe gateway quoted 0 for 505,628 tokens.\n")

    server = UiServer(registry)
    server.wait_ready()
    try:
        pw.open(f"{server.base}/#/run/run-arts-dialog")
        _wait_for_count_ge(pw, _ARTIFACT_ITEMS, 3)
        rows = pw.eval_js(_ARTIFACT_ROW_TEXT)
        # every file is listed by its path; the well-known two carry the name
        # `ralphctl artifacts show` takes, the third is keyless but present
        assert "reflection/report.md [report]" in rows, rows
        assert "reflection/suggestions.diff [suggestions]" in rows, rows
        assert "reports/pricing-anomaly.md []" in rows, rows
        assert int(pw.eval_js(_ARTIFACT_ITEMS)) == 3
        assert int(pw.eval_js(_DIALOGS)) == 0

        # -- the reflect report ---------------------------------------------
        pw.click('button.artifact-item[data-artifact-key="report"]')
        body = _wait_for(pw, "document.querySelector('.text-dialog').textContent",
                         "artifact:  reflection/report.md")
        title = pw.eval_js("document.querySelector('.text-dialog .dialog-title')"
                           ".textContent")
        assert "reflection/report.md" in title and "run-arts-dialog" in title, title
        assert "widen the browser tier" in body, body
        # markup in an agent-authored report stays TEXT, like a PRD body
        assert "<b>requirement C</b>" in body, body
        assert "<script>alert(1)</script>" in body, body
        assert int(pw.eval_js(
            "document.querySelectorAll('.text-dialog script, .text-dialog b').length")) == 0
        # every line came from the server's own `text`, verbatim
        raw = pw.eval_js("document.querySelector('.text-dialog .dialog-body')"
                         ".textContent")
        dialog_body = json.loads(raw) if raw.startswith('"') else raw
        assert dialog_body == artifact_view(registry, "run-arts-dialog",
                                            "report")["text"]
        assert "Pricing anomaly" not in body, body
        pw.screenshot(SCREENSHOTS_DIR / "25-artifact-dialog.png")

        # -- it survives a full 4s poll without stacking -------------------
        time.sleep(5.0)
        assert int(pw.eval_js(_ARTIFACT_ITEMS)) == 3      # panel re-rendered
        assert int(pw.eval_js(_DIALOGS)) == 1             # ...behind ONE dialog
        assert pw.eval_js("document.querySelector('dialog.text-dialog').open") == "true"

        pw.click(".text-dialog .dialog-close button")
        deadline = time.time() + 10
        while time.time() < deadline and pw.eval_js(_DIALOGS) != "0":
            time.sleep(0.2)
        assert int(pw.eval_js(_DIALOGS)) == 0

        # -- the suggested diff, keyboard-reachable -------------------------
        # A <button> gets Enter/Space from the platform -- but the 4s `load()`
        # poll legitimately replaces the panel's nodes, so a focus taken just
        # before one lands is lost through no fault of the button: retry the
        # whole focus+Enter cycle (never a mouse click here).
        focus_js = (
            "(() => { const el = document.querySelector('button.artifact-item"
            "[data-artifact-key=\"suggestions\"]'); el.focus();"
            " return document.activeElement === el ? 'focused' : 'lost'; })()")
        deadline = time.time() + 40
        body = ""
        while time.time() < deadline and "suggestions.diff" not in body:
            if pw.eval_js(focus_js).strip('"') != "focused":
                continue
            assert pw.run("press", "Enter").returncode == 0
            for _ in range(10):
                body = pw.eval_js(
                    "document.querySelector('.text-dialog')"
                    " ? document.querySelector('.text-dialog').textContent : ''")
                if "suggestions.diff" in body:
                    break
                time.sleep(0.2)
        assert "artifact:  reflection/suggestions.diff" in body, body
        # a diff is nothing but `<`/`>`/`+`/`-`: it must arrive verbatim
        assert "+Run <every> tier before claiming done." in body, body
        assert "--- a/prompts/worker.md" in body, body
        assert int(pw.eval_js(_DIALOGS)) == 1
        pw.click(".text-dialog .dialog-close button")

        # -- a run that left nothing behind says so ------------------------
        pw.open(f"{server.base}/#/run/run-arts-none")
        notice = _wait_for(pw, "document.querySelector('#artifacts-notice')"
                               " ? document.querySelector('#artifacts-notice')"
                               ".textContent : ''", NO_ARTIFACTS)
        assert NO_ARTIFACTS in notice, notice
        assert int(pw.eval_js(_ARTIFACT_ITEMS)) == 0
    finally:
        server.stop()


# --------------------------------------------------------------------------
# task 026 (#18.4): the fault dialog behind the failure / infra-wait badge
# --------------------------------------------------------------------------

_FAULT_BADGES = "document.querySelectorAll('button.fault-badge').length"
_FAULT_BADGE_KINDS = (
    "Array.from(document.querySelectorAll('button.fault-badge'))"
    ".map(b => b.dataset.faultBadge).join('|')")


def _seed_faulted_run(registry, run_id, *, pending_wait=True, **status):
    """A run dir that faulted on a DNS outage the engine retried -- written the
    way the engine writes it (status.json's degraded contract,
    `iterations/NNNN/meta.json`, `events.jsonl`), with no container at all.

    `pending_wait=False` is the shape a run that RAN OUT of outage budget has on
    disk: `loop._end_infra_wait` puts `infraWait` back to null while `health`
    stays degraded (it only returns to "ok" on recovery).
    """
    error = ("request to https://aigw.internal/v1 failed, reason: getaddrinfo "
             "EAI_AGAIN aigw.internal")
    wait = {"since": "2026-09-04T10:00:31Z", "attempt": 3, "error": error,
            "phase": "worker", "nextAttemptAt": "2026-09-04T10:02:31Z",
            "waitedS": 210.0, "budgetS": 14400.0, "remainingS": 14190.0}
    run_dir = _write_dead_run(registry, run_id, health="degraded",
                              infraWait=wait if pending_wait else None,
                              infraWaitTotalS=210.0, **status)
    it = run_dir / "iterations" / "0003"
    it.mkdir(parents=True)
    (it / "meta.json").write_text(json.dumps({
        "number": 3, "phase": "worker", "approach": 1,
        "startedAt": "2026-09-04T10:00:00Z", "endedAt": "2026-09-04T10:00:31Z",
        "exitCode": 0, "interrupted": False, "timedOut": False,
        "noTrafficTimeout": False, "sawComplete": False, "sawVerified": False,
        "error": error, "faultClass": "infra", "usage": {}}))
    (run_dir / "events.jsonl").write_text("".join(
        json.dumps({"id": n, "type": "infra_retry", "phase": "worker",
                    "attempt": n, "maxAttempts": None, "error": error,
                    "noTrafficTimeout": False, "instantFailure": False,
                    "backoffS": 30.0 * n, "waitedS": 30.0 * n,
                    "budgetS": 14400}) + "\n"
        for n in (1, 2, 3)))
    return run_dir


def _seed_work_faulted_run(registry, run_id, **status):
    """A run that died of a WORK fault: the model was reached, the worker exited
    non-zero, nothing was retried. No degraded card at all -- so the state pill
    is the only badge, which is the case the failure badge exists for."""
    run_dir = _write_dead_run(registry, run_id, state="failed",
                              verdict="unverified", health="ok",
                              infraWait=None, **status)
    it = run_dir / "iterations" / "0002"
    it.mkdir(parents=True)
    (it / "meta.json").write_text(json.dumps({
        "number": 2, "phase": "worker", "approach": 1,
        "startedAt": "2026-09-04T09:00:00Z", "endedAt": "2026-09-04T09:04:00Z",
        "exitCode": 1, "interrupted": False, "timedOut": False,
        "noTrafficTimeout": False, "sawComplete": True, "sawVerified": False,
        "error": "worker exited 1: pytest collection error",
        "faultClass": "work", "usage": {"totalTokens": 12345}}))
    return run_dir


def test_run_detail_opens_the_fault_dialog_from_the_badge(tmp_path, pw):
    """Task 026 (#18.4): `state: failed` / `⚠ degraded` told an operator that
    something went wrong and nothing more -- WHICH signature fired, how far up
    the retry ladder the run climbed and how much of the outage budget is gone
    were readable only by knowing `engine/faults.py`' table by heart and
    grepping `events.jsonl` by hand.

    Drives both ways in with a real browser, for runs whose container is gone
    (the on-disk case #18.4 exists for): the failed run's state badge and a
    degraded running run's infra-wait badge. Pins the dialog payload (the
    server's own `state.fault_text`, i.e. exactly what `ralphctl fault` prints,
    as TEXT NODES), that all four facts are in it, that the single shared dialog
    does not stack across the 4s `load()` rebuild behind it, that the badge is
    keyboard-reachable, and that a run which never faulted carries no badge at
    all (nothing to explain, so nothing to click).
    """
    from ralphd.cli.ui_server import fault_view

    registry = tmp_path / "registry"
    _seed_faulted_run(registry, "run-fault-dead", pending_wait=False,
                      state="failed", verdict="unverified",
                      reason="infra outage budget exhausted",
                      abortReason="infra outage budget exhausted")
    _seed_faulted_run(registry, "run-fault-degraded", state="running",
                      verdict=None)
    _seed_work_faulted_run(registry, "run-fault-work")
    _write_dead_run(registry, "run-fault-clean", state="succeeded",
                    verdict="verified")

    server = UiServer(registry)
    server.wait_ready()
    try:
        # -- the failure badge on a terminal failed run ---------------------
        pw.open(f"{server.base}/#/run/run-fault-dead")
        kinds = _wait_for(pw, _FAULT_BADGE_KINDS, "state").strip('"')
        # a run the outage KILLED keeps `health: degraded` on disk, so it
        # carries both ways in -- and both open the same explanation
        assert kinds == "state|infra-wait", kinds
        assert int(pw.eval_js(_DIALOGS)) == 0
        pw.click('button.fault-badge[data-fault-badge="state"]')
        body = _wait_for(pw, "document.querySelector('.text-dialog').textContent",
                         "fault:     infra")
        title = pw.eval_js("document.querySelector('.text-dialog .dialog-title')"
                           ".textContent")
        assert "run-fault-dead" in title, title
        # the four facts #18.4 asked for
        assert "fault:     infra (iteration 3, phase worker)" in body, body
        assert "signature: dns" in body, body
        assert "EAI_AGAIN" in body, body
        assert "ladder:    attempt 3" in body, body
        assert "budget:    " in body and "14400" not in body, body
        # ...and every line of it came from the server, verbatim
        raw = pw.eval_js("document.querySelector('.text-dialog .dialog-body')"
                         ".textContent")
        dialog_body = json.loads(raw) if raw.startswith('"') else raw
        assert dialog_body == fault_view(registry, "run-fault-dead")["text"]
        assert int(pw.eval_js(
            "document.querySelectorAll('.text-dialog script, .text-dialog b')"
            ".length")) == 0
        pw.screenshot(SCREENSHOTS_DIR / "26-fault-dialog.png")

        # -- it survives a full 4s poll without stacking --------------------
        time.sleep(5.0)
        assert int(pw.eval_js(_FAULT_BADGES)) == 2        # card re-rendered
        assert int(pw.eval_js(_DIALOGS)) == 1             # ...behind ONE dialog
        assert pw.eval_js("document.querySelector('dialog.text-dialog').open") == "true"
        pw.click(".text-dialog .dialog-close button")
        deadline = time.time() + 10
        while time.time() < deadline and pw.eval_js(_DIALOGS) != "0":
            time.sleep(0.2)
        assert int(pw.eval_js(_DIALOGS)) == 0

        # -- the infra-wait badge on a degraded RUNNING run, by keyboard ----
        # A <button> gets Enter/Space from the platform -- but the 4s `load()`
        # replaces the card's nodes, so a focus taken just before one lands is
        # lost through no fault of the button: retry the whole cycle.
        pw.open(f"{server.base}/#/run/run-fault-degraded")
        kinds = _wait_for(pw, _FAULT_BADGE_KINDS, "infra-wait").strip('"')
        assert kinds == "infra-wait", kinds  # running: the outage is the badge
        focus_js = (
            "(() => { const el = document.querySelector('button.fault-badge"
            "[data-fault-badge=\"infra-wait\"]'); el.focus();"
            " return document.activeElement === el ? 'focused' : 'lost'; })()")
        deadline = time.time() + 40
        body = ""
        while time.time() < deadline and "signature: dns" not in body:
            if pw.eval_js(focus_js).strip('"') != "focused":
                continue
            assert pw.run("press", "Enter").returncode == 0
            for _ in range(10):
                body = pw.eval_js(
                    "document.querySelector('.text-dialog')"
                    " ? document.querySelector('.text-dialog').textContent : ''")
                if "signature: dns" in body:
                    break
                time.sleep(0.2)
        assert "fault:     infra" in body, body
        assert "health:    degraded" in body, body
        assert "gave up" not in body, body     # still in the outage, not dead of it
        assert int(pw.eval_js(_DIALOGS)) == 1
        pw.click(".text-dialog .dialog-close button")

        # -- a WORK fault: the state pill is the only badge -----------------
        pw.open(f"{server.base}/#/run/run-fault-work")
        kinds = _wait_for(pw, _FAULT_BADGE_KINDS, "state").strip('"')
        assert kinds == "state", kinds
        assert int(pw.eval_js("document.querySelectorAll('.infra-wait').length")) == 0
        pw.click('button.fault-badge[data-fault-badge="state"]')
        body = _wait_for(pw, "document.querySelector('.text-dialog').textContent",
                         "fault:     work")
        assert "pytest collection error" in body, body
        # a work fault matches no infra signature -- said out loud, in the
        # server's own wording, not left blank
        assert "signature: " in body, body
        assert "dns" not in body, body
        raw = pw.eval_js("document.querySelector('.text-dialog .dialog-body')"
                         ".textContent")
        dialog_body = json.loads(raw) if raw.startswith('"') else raw
        assert dialog_body == fault_view(registry, "run-fault-work")["text"]
        pw.click(".text-dialog .dialog-close button")

        # -- a run that never faulted has nothing to click ------------------
        pw.open(f"{server.base}/#/run/run-fault-clean")
        _wait_for(pw, "document.body.innerText", "succeeded")
        assert int(pw.eval_js(_FAULT_BADGES)) == 0
        assert int(pw.eval_js("document.querySelectorAll('.infra-wait').length")) == 0
    finally:
        server.stop()


# ------------------------- cost-breakdown dialog (#18.5, task 028) --------
# --------------------------------------------------------------------------

_COST_CELLS = "document.querySelectorAll('button.cost-cell').length"
_COST_CELL_TEXT = (
    "document.querySelector('button.cost-cell')"
    " ? document.querySelector('button.cost-cell').textContent : ''")
_COST_CARD_TEXT = (
    "Array.from(document.querySelectorAll('.usage-grid .stat'))"
    ".filter(s => s.querySelector('.k').textContent === 'cost')"
    ".map(s => s.querySelector('.v').textContent).join('|')")

# The verbatim iteration-1 usage of this very self-development run: the
# AIGW/Bedrock route quoted $0 for half a million billed tokens (task 049), with
# the per-phase/per-approach buckets the engine accumulates around it.
_ZERO_QUOTE_USAGE = {
    "input": 32, "output": 18320, "cacheRead": 438945, "cacheWrite": 48331,
    "totalTokens": 505628, "costUSD": 0,
    "byPhase": {"worker": {"totalTokens": 505628, "costUSD": 0}},
    "byApproach": {"1": {"totalTokens": 505628, "costUSD": 0}},
}

# A run that mixes all three kinds of money: a provider-quoted phase, one whose
# cost was derived from the host-side rate table, one nothing priced at all.
_MIXED_USAGE = {
    "input": 1200, "output": 3400, "totalTokens": 40000,
    "costUSD": 0.5, "costDerivedUSD": 1.25, "costStatus": "partial",
    "byPhase": {
        "planning": {"input": 200, "output": 400, "totalTokens": 10000,
                     "costUSD": 0.5},
        "worker": {"input": 800, "output": 2600, "totalTokens": 20000,
                   "costDerivedUSD": 1.25, "costStatus": "derived"},
        "verify": {"input": 200, "output": 400, "totalTokens": 10000,
                   "costUSD": 0, "costPriced": False, "costStatus": "unknown"},
    },
    "byApproach": {
        "2": {"totalTokens": 10000, "costDerivedUSD": 1.25, "costStatus": "derived"},
        "10": {"totalTokens": 30000, "costUSD": 0.5, "costStatus": "partial"},
    },
}


def test_run_detail_opens_the_cost_dialog_from_the_cost_cell(tmp_path, pw):
    """Task 028 (#18.5): the usage card showed ONE money string over tables of
    raw token counts -- "which phase burned this, and is the figure quoted,
    derived or unavailable?" meant leaving the hub for `ralphctl cost` or reading
    status.json by hand.

    Drives the cell with a real browser, for runs whose container is gone (the
    on-disk case #18.5 exists for). Pins that the headline stays exactly the
    string the card already showed, that the dialog body is the server's own
    `state.cost_breakdown_text` (i.e. what `ralphctl cost` prints) as TEXT
    NODES, that the per-phase/per-approach buckets and the derived/unavailable
    labels are in it, that the single shared dialog does not stack across the 4s
    `load()` rebuild behind it, that the cell is keyboard-reachable, and that
    this run's own implausible zero quote (task 049) opens as `unavailable`
    rather than `$0.00`.
    """
    from ralphd.cli.ui_server import cost_view

    registry = tmp_path / "registry"
    _write_dead_run(registry, "run-cost-mixed", state="succeeded",
                    verdict="verified", usage=_MIXED_USAGE)
    _write_dead_run(registry, "run-cost-zero", state="failed", verdict=None,
                    usage=_ZERO_QUOTE_USAGE)
    _write_dead_run(registry, "run-cost-quiet", state="succeeded",
                    verdict="verified")

    server = UiServer(registry)
    server.wait_ready()
    try:
        # -- the mixed run: click the cost cell ------------------------------
        pw.open(f"{server.base}/#/run/run-cost-mixed")
        cell = _wait_for(pw, _COST_CELL_TEXT, "$").strip('"')
        # the headline is the card's own `costDisplay`, unchanged by becoming
        # clickable -- the value cell holds exactly the same text
        expected = cost_view(registry, "run-cost-mixed")["costDisplay"]
        assert cell == expected, (cell, expected)
        assert pw.eval_js(_COST_CARD_TEXT).strip('"') == expected
        assert int(pw.eval_js(_DIALOGS)) == 0

        pw.click("button.cost-cell")
        body = _wait_for(pw, "document.querySelector('.text-dialog').textContent",
                         "by phase:")
        title = pw.eval_js("document.querySelector('.text-dialog .dialog-title')"
                           ".textContent")
        assert "run-cost-mixed" in title, title
        # the buckets, and every kind of money labelled
        for phase in ("planning", "worker", "verify"):
            assert phase in body, body
        assert "by approach:" in body, body
        assert "derived" in body, body
        assert "unavailable" in body, body
        assert f"cost:      {expected}" in body, body
        # ...and every line of it came from the server, verbatim
        raw = pw.eval_js("document.querySelector('.text-dialog .dialog-body')"
                         ".textContent")
        dialog_body = json.loads(raw) if raw.startswith('"') else raw
        assert dialog_body == cost_view(registry, "run-cost-mixed")["text"]
        assert int(pw.eval_js(
            "document.querySelectorAll('.text-dialog script, .text-dialog b')"
            ".length")) == 0
        pw.screenshot(SCREENSHOTS_DIR / "28-cost-dialog.png")

        # -- it survives a full 4s poll without stacking ---------------------
        time.sleep(5.0)
        assert int(pw.eval_js(_COST_CELLS)) == 1          # card re-rendered
        assert int(pw.eval_js(_DIALOGS)) == 1             # ...behind ONE dialog
        assert pw.eval_js("document.querySelector('dialog.text-dialog').open") == "true"
        pw.click(".text-dialog .dialog-close button")
        deadline = time.time() + 10
        while time.time() < deadline and pw.eval_js(_DIALOGS) != "0":
            time.sleep(0.2)
        assert int(pw.eval_js(_DIALOGS)) == 0

        # -- this run's own zero quote, opened by keyboard -------------------
        # A <button> gets Enter/Space from the platform -- but the 4s `load()`
        # replaces the card's nodes, so a focus taken just before one lands is
        # lost through no fault of the button: retry the whole cycle.
        pw.open(f"{server.base}/#/run/run-cost-zero")
        cell = _wait_for(pw, _COST_CELL_TEXT, "unavailable").strip('"')
        assert cell == "unavailable", cell     # never $0.00 (#10/task 049)
        focus_js = (
            "(() => { const el = document.querySelector('button.cost-cell');"
            " el.focus();"
            " return document.activeElement === el ? 'focused' : 'lost'; })()")
        deadline = time.time() + 40
        body = ""
        while time.time() < deadline and "505,628" not in body:
            if pw.eval_js(focus_js).strip('"') != "focused":
                continue
            assert pw.run("press", "Enter").returncode == 0
            for _ in range(10):
                body = pw.eval_js(
                    "document.querySelector('.text-dialog')"
                    " ? document.querySelector('.text-dialog').textContent : ''")
                if "505,628" in body:
                    break
                time.sleep(0.2)
        assert "cost:      unavailable" in body, body
        assert "$0.00" not in body, body
        # the anomaly is NAMED rather than leaving a column of `unavailable`s
        assert "quoted" in body and "zero" in body, body
        assert int(pw.eval_js(_DIALOGS)) == 1
        pw.click(".text-dialog .dialog-close button")

        # -- a run that recorded nothing: the cell says so, and opens ---------
        pw.open(f"{server.base}/#/run/run-cost-quiet")
        _wait_for(pw, "document.body.innerText", "succeeded")
        # no usage at all -> the card has no cost cell to click (nothing was
        # ever reported), and the endpoint would still answer honestly
        assert int(pw.eval_js(_COST_CELLS)) == 0
        assert cost_view(registry, "run-cost-quiet")["hasUsage"] is False
    finally:
        server.stop()
