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
