"""Playwright-cli browser e2e tests for the hub (PRD req 23a).

Marked `@pytest.mark.browser`. Drives a *real* Chromium via the
`playwright-cli` binary (a stdlib-free external tool, shelled out to --
never imported), against a real `ralphctl ui` server (tasks 033/034).
Assertions are made by evaluating small JS snippets in the page
(`playwright-cli eval ...`) rather than by parsing markdown snapshots.

Skips cleanly (whole module) if the `playwright-cli` binary is not on
PATH. Screenshots of each view are written under
`artifacts/screenshots/hub/` (the job's artifacts dir). The base directory
is `RALPHD_ARTIFACTS_DIR` when set (this job's layout: `/run/ralphd/artifacts`,
a path only writable because it is this container's run-dir mount); when
unset it falls back to a location any ordinary user can create --
`<tempfile.gettempdir()>/ralphd-artifacts` -- rather than guessing at
`/run/ralphd`, which is root-owned on a developer host. See
`_default_artifacts_dir()` below for the exact rule.
"""

from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
from test_cli_ui import StubEngineApi, UiServer, _write_dead_run, _write_run_with_api

from ralphd.log_merge import iteration_output_path

PLAYWRIGHT_CLI = shutil.which("playwright-cli")


def _default_artifacts_dir() -> Path:
    """Where screenshots go when `RALPHD_ARTIFACTS_DIR` is unset.

    Inside a job container, `/run/ralphd` is the run-dir mount and this
    default used to hardcode `/run/ralphd/artifacts` -- a path that is only
    writable there *because* it is a mount, not because `/run` is generally
    writable. On a developer host `/run` is root-owned, so that default made
    the whole browser tier fail there (PRD #57 fact 4). The rule now is:
    honour the env var if it is set (this keeps the in-container behaviour
    working exactly as before, including setting it explicitly to
    `/run/ralphd/artifacts`); otherwise fall back to a directory under the
    system temp dir, which `mkdir(parents=True)` can always create for an
    ordinary user.
    """
    env = os.environ.get("RALPHD_ARTIFACTS_DIR")
    if env:
        return Path(env)
    return Path(tempfile.gettempdir()) / "ralphd-artifacts"


ARTIFACTS_DIR = _default_artifacts_dir()
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


def test_run_detail_header_says_nothing_run_ended_for_a_live_run(tmp_path, pw):
    """Task 001 (#164): the run-detail header's steering warning reads
    literally 'run ended without acting on' -- if `unconsumedSteering` ever
    goes stale (carried forward across a resume, or left uncleared right
    after a live consume) a run that is genuinely alive and that DID act on
    the note would render that wording anyway. A genuinely live run
    (`StubEngineApi`, no container-gone treatment) whose `unconsumedSteering`
    is correctly empty must render neither the warning card nor the wording.
    """
    registry = tmp_path / "registry"
    engine = StubEngineApi(status={
        "runId": "run-alive-steered", "state": "running", "verdict": None,
        "phase": "worker", "approach": 1, "iterationsUsed": 2,
        "iterationsBudget": 25, "startedAt": "2026-01-01T00:00:00Z",
        "unconsumedSteering": [],
    })
    _write_run_with_api(registry, "run-alive-steered", engine, state="running",
                        verdict=None, unconsumedSteering=[])

    server = UiServer(registry)
    server.wait_ready()
    try:
        pw.open(f"{server.base}/#/run/run-alive-steered")
        body_text = _wait_for(pw, "document.body.innerText", "running")
        assert "UNCONSUMED STEERING" not in body_text, body_text
        assert "run ended" not in body_text, body_text
        n_warn = int(pw.eval_js("document.querySelectorAll('.steering-warning').length"))
        assert n_warn == 0
    finally:
        server.stop()
        engine.close()


def test_run_detail_hides_the_terminal_record_during_the_finishing_window(tmp_path, pw):
    """Task 015 (#56): task 012's `finishing` state settles `verdict`/`endedAt`
    into status.json before the post-terminal reflect iteration is actually
    over -- so a run whose `currentIteration` is still a live dict (the
    reflect iteration itself, still running) must not have the run-detail
    card show a colored, terminal-looking verdict pill or an `ended:` row: an
    operator reading the card must not conclude the run is over while the
    timeline right below it is still showing that very iteration as
    'running...'. Neither field is hidden -- the verdict TEXT is still shown,
    just not styled as the same pill a genuinely terminal run gets -- and both
    reappear once the window closes (state promotes, currentIteration clears)."""
    registry = tmp_path / "registry"
    run_dir = _write_dead_run(
        registry, "run-reflecting", state="finishing", verdict="verified",
        pendingTerminalState="succeeded", phase="reflect",
        startedAt="2026-01-01T00:00:00Z", endedAt="2026-01-01T00:41:30Z",
        currentIteration={"number": 4, "phase": "reflect",
                          "startedAt": "2026-01-01T00:41:31Z"})
    (run_dir / "iterations" / "0004").mkdir(parents=True)
    (run_dir / "iterations" / "0004" / "meta.json").write_text(json.dumps(
        {"number": 4, "phase": "reflect", "model": "stub-model", "approach": 1,
         "startedAt": "2026-01-01T00:41:31Z", "endedAt": None, "exitCode": None,
         "error": None, "usage": {}}))
    _write_dead_run(registry, "run-done", state="succeeded", verdict="verified",
                    startedAt="2026-01-01T00:00:00Z", endedAt="2026-01-01T00:41:30Z")

    server = UiServer(registry)
    server.wait_ready()
    try:
        pw.open(f"{server.base}/#/run/run-reflecting")
        body_text = _wait_for(pw, "document.body.innerText", "finishing")
        assert "ended:" not in body_text, body_text
        assert "running" in body_text, body_text  # the timeline row below
        assert "verified" in body_text, body_text  # the verdict text is still there
        n_verdict_pill = int(pw.eval_js(
            "document.querySelectorAll('.pill-verified').length"))
        assert n_verdict_pill == 0, "verdict rendered as a terminal-looking pill"
        pw.screenshot(SCREENSHOTS_DIR / "finishing-no-terminal-record.png")

        # the counterpart: a genuinely terminal run keeps today's rendering.
        pw.open(f"{server.base}/#/run/run-done")
        body_text = _wait_for(pw, "document.body.innerText", "succeeded")
        assert "ended:" in body_text, body_text
        n_verdict_pill = int(pw.eval_js(
            "document.querySelectorAll('.pill-verified').length"))
        assert n_verdict_pill == 1, body_text
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


def test_reason_extraction_walks_a_nested_problem_body_to_the_innermost_string(tmp_path, pw):
    """Task 069 (#155, requirement G.7): both the steer form's error branch
    and `renderRetryNow`'s fallback used to stop at the first TRUTHY value
    in their own `||` chain -- which, for a body nested one `detail` deeper
    than either chain expected, is an OBJECT, and `String()` on an object
    renders the literal text `[object Object]`. This is exactly the shape
    the hub's own proxy produces when the run's live API answers a mutating
    request with a problem document that itself carries a `detail` key
    (`ui_server.py`'s `do_POST`: `{"error": ..., "detail": <engine body>}`,
    and the engine's own body here is `{"detail": {"title": ..., "detail":
    "steering has no effect"}}`) -- e.g. a run that finishes between the
    browser's submit and the proxy's forward (#179's family).

    Confirmed this node genuinely pins the defect: reverted `app.js` to the
    pre-069 `||` chains (`git show HEAD:src/ralphd/cli/web/app.js` before
    this commit) and re-ran this node solo -- both of the first two
    assertions failed with the rendered text containing the literal
    `[object Object]` instead of `steering has no effect`. Restored the fix
    before this commit landed.

    The rest of the node then re-drives the RETRY surface (the two share
    one helper, already proven identical above by the nested-body checks)
    against the three shapes that worked before this task and must keep
    working: a flat `{error: ...}`, the 401 control-token body task 007
    introduced (`{detail: {title, status, detail: "mutating routes require
    the control token"}}`), and a body with no string anywhere, which must
    still fall back to the numeric status. The retry proxy passes the
    engine's body through verbatim with no added wrapper key (unlike the
    steer proxy, which always adds its own top-level `error` string), so
    driving these three through retry tests the helper against exactly the
    named shape with nothing else in play.
    """
    nested_body = {
        "error": "run's API unreachable or rejected the request",
        "detail": {"detail": {"title": "job finished", "status": 409,
                              "detail": "steering has no effect"}},
    }
    next_attempt = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + 180))
    infra_wait = {
        "since": "2026-01-01T00:00:00Z", "attempt": 3,
        "error": "socket hang up", "phase": "worker",
        "nextAttemptAt": next_attempt,
        "waitedS": 20, "budgetS": 14400, "remainingS": 14380,
    }
    degraded_status = {
        "runId": "run-nested-reason", "state": "running", "verdict": None,
        "phase": "worker", "approach": 1, "iterationsUsed": 5,
        "iterationsBudget": 25, "startedAt": "2026-01-01T00:00:00Z",
        "health": "degraded", "infraWait": infra_wait,
    }
    # Both surfaces are fed the SAME body shape the task names -- the retry
    # proxy passes a dict response through verbatim (`ui_server.py`'s
    # `do_POST`), so setting the stub's own retry body to the already-
    # hub-wrapped shape reproduces exactly that `j` at the browser for the
    # retry button too, not just the steer form.
    engine = StubEngineApi(status=degraded_status, steer_status=409,
                           steer_body={"detail": {"title": "job finished",
                                                  "status": 409,
                                                  "detail": "steering has no effect"}},
                           retry_status=409, retry_body=dict(nested_body))
    registry = tmp_path / "registry"
    try:
        _write_run_with_api(registry, "run-nested-reason", engine, **degraded_status)
        server = UiServer(registry)
        server.wait_ready()
        try:
            pw.open(f"{server.base}/#/run/run-nested-reason")
            _wait_for(pw, "document.body.innerText", "Steer")

            pw.fill("#steer-message", "trigger the nested rejection")
            pw.click("#steer-form button[type=submit]")
            steer_text = _wait_for(pw, "document.getElementById('steer-status').textContent", "failed")
            assert "steering has no effect" in steer_text, steer_text
            assert "[object Object]" not in steer_text, steer_text

            _wait_for(pw, "document.body.innerText", "retry now")
            pw.click("button.retry-now")
            retry_text = _wait_for(
                pw, "document.querySelector('.retry-now-status').textContent", "failed")
            assert "steering has no effect" in retry_text, retry_text
            assert "[object Object]" not in retry_text, retry_text

            pw.screenshot(SCREENSHOTS_DIR / "07-nested-reason.png")

            # -- previously working shapes keep working, on the RETRY
            # surface -- it passes the stub's body through with no added
            # wrapper key, so each shape below is exactly what the task
            # names, nothing else in play. Each wait targets the shape's OWN
            # distinguishing text, never the generic "failed" prefix already
            # on screen from the previous click, so a stale render cannot
            # pass for a fresh one.
            engine.retry_body = {"error": "flat failure, no detail key at all"}
            pw.click("button.retry-now")
            flat_text = _wait_for(pw, "document.querySelector('.retry-now-status').textContent",
                                  "flat failure")
            assert "[object Object]" not in flat_text, flat_text

            engine.retry_body = {"detail": {"title": "unauthorized", "status": 401,
                                           "detail": "mutating routes require the control token"}}
            pw.click("button.retry-now")
            control_text = _wait_for(pw, "document.querySelector('.retry-now-status').textContent",
                                     "mutating routes require the control token")
            assert "[object Object]" not in control_text, control_text

            engine.retry_body = {"noStringAnywhere": 1}
            pw.click("button.retry-now")
            fallback_text = _wait_for(pw, "document.querySelector('.retry-now-status').textContent",
                                      "failed: 409")
            assert "[object Object]" not in fallback_text, fallback_text
        finally:
            server.stop()
    finally:
        engine.close()


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
               stub_env={"STUB_TASKS": "2", "STUB_SLEEP": "0.05"})
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
    # task 009 (B.3): STUB_HOLD_FILE/STUB_HOLD_PHASE="worker" holds the
    # single task's worker iteration open (after its own in-progress write)
    # deterministically long enough for the browser to open the page, fill
    # and submit the steer form before the job could reach a terminal
    # state (which refuses steering with 409) -- instead of racing a fixed
    # STUB_SLEEP window against real Playwright interaction latency.
    hold_file = tmp_path / "hold-browser-steer"
    run = live(run_id="browser-steer",
               job={"iterations": 12, "max_approaches": 3, "on_complete": "idle"},
               stub_env={"STUB_TASKS": "1", "STUB_HOLD_FILE": str(hold_file),
                        "STUB_HOLD_PHASE": "worker", "STUB_HOLD_TIMEOUT_S": "30"})
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

        hold_file.touch()  # release: let the held worker iteration finish

        pw.screenshot(SCREENSHOTS_DIR / "03-steering-sent.png")
    finally:
        run.wait_terminal(timeout=60)
        server.stop()

    steering_files = list((run.run_dir / "steering").glob("*.md"))
    assert steering_files, "steering form submit via the browser never wrote a file"
    assert any("browser e2e steering message" in f.read_text() for f in steering_files)


def _assert_content_advanced(before: str, after: str, what: str) -> None:
    """Task 064: the one assertion
    `test_run_detail_keeps_updating_through_a_long_worker_iteration` relies
    on to prove the run-detail page is still live -- `after` must differ
    from `before`. Named and factored out (rather than inlined as a bare
    `assert before != after`) so the test can demonstrate, in the same
    module, that this check genuinely fails on a page that stopped
    updating: see the inline self-check right before its real use below.
    """
    assert before != after, (
        f"{what} did not advance between the two observations -- the page "
        f"looks stalled rather than still refreshing (before={before!r} "
        f"after={after!r})")


def test_run_detail_keeps_updating_through_a_long_worker_iteration(tmp_path, pw, live):
    """Task 022 (requirement F): before this task, the engine ran a
    0.25s background poller (`_poll_task_changes`) purely so a task-status
    transition written mid-iteration surfaced as a live `task` event; that
    poller is retired. This is the black-box proof that retiring it did
    not cost the hub's run-detail view its in-step liveness during a long
    worker iteration.

    Task 064 reshaped the proof itself: the previous version sampled
    `#logbox`/`#task-box` on a fixed real-time loop and asserted it managed
    `>= 4` samples inside an 8s window -- a host-throughput floor, not a
    liveness proof (a fast host could always clear it without the page ever
    actually refreshing with new content, and a slow host could fail it
    while the page was working perfectly). This version instead CAUSES a
    change mid-iteration -- appending a distinguishing event straight to
    the currently-running iteration's own transcript file, the same file
    `#logbox` is server-rendered from on every poll (`log_merge`/
    `log_render`, never cached) -- and asserts the page's rendered content
    genuinely advances to reflect it (`_assert_content_advanced`), which is
    the actual property this test exists for. The task table (`#task-box`)
    is still checked never to blank out or revert to its empty-state
    placeholder across the same window.
    """
    # A hold (task 008/B.2's `STUB_HOLD_FILE`/`STUB_HOLD_PHASE`), not a
    # fixed `STUB_SLEEP`, is what actually keeps the worker iteration open
    # here: planning and review run at their normal (near-instant) speed,
    # so the task table appears the moment planning hands off -- no window
    # where the hub's run-detail poll sees `tasks.json` missing for two
    # polls running and widens itself to `BACKOFF_POLL_MS` (task 005, A.3)
    # before there is even anything to show. The worker iteration alone
    # stays open until this test explicitly releases it (`hold_file.touch()`
    # below), which is exactly the bounded real-time window this test
    # needs and no more -- `STUB_HOLD_TIMEOUT_S` is a safety net, not the
    # thing driving the test's own timing.
    hold_file = tmp_path / "hold-browser-liveness"
    run = live(run_id="browser-liveness",
               job={"iterations": 12, "max_approaches": 3, "on_complete": "idle"},
               stub_env={"STUB_TASKS": "1", "STUB_HOLD_FILE": str(hold_file),
                        "STUB_HOLD_PHASE": "worker", "STUB_HOLD_TIMEOUT_S": "30"})
    run.wait_api()

    server = UiServer(run.registry)
    server.wait_ready()
    try:
        pw.open(f"{server.base}/#/run/{run.run_id}")

        # Wait for the worker iteration itself (iteration 2 -- iteration 1
        # is planning) to actually start AND for the hub's own run-detail
        # view to have caught up to it: the task table has at least one row
        # only once planning has finished and the held worker iteration has
        # begun.
        _wait_for_count_ge(
            pw, "document.querySelectorAll('#task-box tbody tr').length", 1, timeout=30)

        # The exact worker iteration number, read directly off the run's own
        # `status.json` -- reliable now that the task row above proves the
        # harness has already assigned it -- rather than inferred from a
        # directory listing that could itself be mid-write.
        status = json.loads((run.run_dir / "status.json").read_text())
        cur = status.get("currentIteration") or {}
        iter_no = cur.get("number")
        assert cur.get("phase") == "worker" and iter_no, (
            f"expected the worker iteration to still be current; got {cur!r}")

        # Companion self-check (task 064): prove `_assert_content_advanced`
        # is not vacuously true -- it must raise when nothing changed --
        # right here, inline, every time this test runs, rather than
        # trusted on faith. This is the "still fails if the page stops
        # updating" guarantee the real assertion below depends on.
        try:
            _assert_content_advanced("same text", "same text", "self-check")
        except AssertionError:
            pass
        else:
            raise AssertionError(
                "_assert_content_advanced failed to raise on identical "
                "before/after content -- it would not catch a genuinely "
                "stalled page either, so the real assertion below would be "
                "meaningless")

        log_text_before = pw.eval_js("document.getElementById('logbox').innerText")
        task_text_before = pw.eval_js("document.getElementById('task-box').innerText")
        assert log_text_before.strip(), "log tail went blank mid-iteration"
        assert task_text_before.strip(), "task table went blank mid-iteration"
        assert "(no tasks)" not in task_text_before, (
            f"task table reverted to the empty placeholder mid-iteration: "
            f"{task_text_before!r}")

        # Cause the change ourselves: append a distinguishing event to the
        # in-progress worker iteration's own output.jsonl. merge_entries
        # (log_merge.py) re-reads that file from disk on every /logs
        # request -- never cached -- so this is a change the hub's server
        # side has no way to see except by genuinely re-polling.
        marker = f"LIVENESS-MARKER-{os.getpid()}-{time.time_ns()}"
        out_path = iteration_output_path(run.run_dir, iter_no)
        with open(out_path, "a") as f:
            f.write(json.dumps({
                "type": "message_update",
                "assistantMessageEvent": {"type": "text_delta", "delta": marker + "\n"},
            }) + "\n")

        log_text_after = _wait_for(
            pw, "document.getElementById('logbox').innerText", marker, timeout=20)
        _assert_content_advanced(log_text_before, log_text_after, "logbox")

        task_text_after = pw.eval_js("document.getElementById('task-box').innerText")
        assert task_text_after.strip(), "task table went blank mid-iteration"
        assert "(no tasks)" not in task_text_after, (
            f"task table reverted to the empty placeholder mid-iteration: "
            f"{task_text_after!r}")

        pw.screenshot(SCREENSHOTS_DIR / "05-run-detail-liveness.png")
    finally:
        hold_file.touch()  # release: let the held worker iteration finish
        run.wait_terminal(timeout=60)
        server.stop()


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
    # task 009 (B.3): STUB_SLEEP reduced -- this test has no immediate-state
    # race (every assertion below is reached through _wait_for_count_ge's
    # own generous 30s poll, not a fixed-window sample), so it needs no
    # more real delay than it takes both rich iterations' events to stream.
    run = live(run_id="browser-thinking",
               job={"iterations": 12, "max_approaches": 3, "on_complete": "idle"},
               stub_env={"STUB_RICH_EVENTS": "1", "STUB_TASKS": "1", "STUB_SLEEP": "0.1"})
    run.wait_api()

    server = UiServer(run.registry)
    server.wait_ready()
    try:
        pw.open(f"{server.base}/#/run/{run.run_id}")
        # Wait for the state the assertion below actually demands: BOTH
        # rich iterations' collapsed '[thinking\u2026]' lines (worker + review),
        # not just the first one to land -- a wait threshold of 1 races the
        # review iteration's slower-to-render line (#97).
        _wait_for_count_ge(
            pw,
            "Array.from(document.querySelectorAll('#logbox .lg-line'))"
            ".filter(e => e.textContent.includes('[thinking')).length",
            2, timeout=30)

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
# task 014 (#156, requirement A.4): the in-progress task's own progress
# budget, on the task row
# --------------------------------------------------------------------------

def test_run_detail_shows_the_progress_budget_pill_on_the_in_progress_task_row(
        tmp_path, pw):
    """Task 013's `progressBudget` status.json key (`{taskId, used, budget}`)
    flows through `ui_server.run_detail` untouched (a plain dict merge, no
    field allowlist) into `body.status.progressBudget`; this pins that the
    hub actually RENDERS it -- a `budget 2/3` pill on the NAMED task's own
    row, and on no other row, matching `ralphctl tasks`'s
    ` (progress budget 2/3)` suffix in spirit (same numbers, same task).
    """
    registry = tmp_path / "registry"
    tasks = {"tasks": [
        {"id": "001", "title": "already done", "status": "completed"},
        {"id": "002", "title": "spending its budget", "status": "in-progress"},
        {"id": "003", "title": "not started", "status": "pending"},
    ]}
    dead = _write_dead_run(registry, "run-budget", state="running", verdict=None,
                           progressBudget={"taskId": "002", "used": 2, "budget": 3})
    (dead / "tasks.json").write_text(json.dumps(tasks))

    server = UiServer(registry)
    server.wait_ready()
    try:
        pw.open(f"{server.base}/#/run/run-budget")
        _wait_for(pw, "document.body.innerText", "spending its budget")
        rows = ("[...document.querySelectorAll('tr.task-row')]"
               ".map(tr => tr.dataset.taskId + '=' + tr.textContent).join('|')")
        text = pw.eval_js(rows).strip('"')
        by_id = dict(pair.split("=", 1) for pair in text.split("|") if "=" in pair)
        assert "budget 2/3" not in by_id["001"], by_id
        assert "budget 2/3" in by_id["002"], by_id
        assert "budget 2/3" not in by_id["003"], by_id
        pw.screenshot(SCREENSHOTS_DIR / "18-progress-budget.png")
    finally:
        server.stop()


# --------------------------------------------------------------------------
# task 021 (#60 pillar 3): dead dependencies are harness-derived, and the
# dead subtree is visible in the hub -- not just `ralphctl tasks`
# --------------------------------------------------------------------------

def test_run_detail_marks_a_dead_dependencys_subtree_blocked(tmp_path, pw):
    """A dependency that reaches `failed`/`skipped` dooms its dependents
    WITHOUT the harness ever changing their own `status` -- and the hub
    shows it, not just `ralphctl tasks`, per the PRD's own wording ("Dead
    subtrees are visible in `ralphctl tasks` and the hub").

    Task 004 is dead outright; 005 depends on it directly (blocked); 006
    depends on 005, which is itself only blocked, not dead -- proving the
    derivation is transitive, not one hop. Task 007 depends on 001
    (completed, a SATISFIED dependency) and is NOT blocked.
    """
    registry = tmp_path / "registry"
    tasks = {"tasks": [
        {"id": "001", "title": "unrelated done task", "status": "completed"},
        {"id": "004", "title": "the one that died", "status": "failed",
         "failureKind": "requirement-unmet"},
        {"id": "005", "title": "direct dependent", "status": "pending",
         "dependsOn": ["004"]},
        {"id": "006", "title": "transitive dependent", "status": "pending",
         "dependsOn": ["005"]},
        {"id": "007", "title": "unaffected dependent", "status": "pending",
         "dependsOn": ["001"]},
    ]}
    dead = _write_dead_run(registry, "run-blocked", state="running", verdict=None)
    (dead / "tasks.json").write_text(json.dumps(tasks))

    server = UiServer(registry)
    server.wait_ready()
    try:
        pw.open(f"{server.base}/#/run/run-blocked")
        _wait_for(pw, "document.body.innerText", "transitive dependent")
        assert int(pw.eval_js("document.querySelectorAll('tr.task-row').length")) == 5

        # -- rows, selected by POSITION (avoids CSS attribute-value quoting
        # noise): 001, 004, 005, 006, 007 in that order. 005/006 flagged
        # blocked by their own direct dead/doomed dependency; 007 (a
        # satisfied dependency) is not.
        rows = "Array.from(document.querySelectorAll('tr.task-row'))"
        blocked_005 = pw.eval_js(f"{rows}[2].dataset.blocked").strip('"')
        blocked_006 = pw.eval_js(f"{rows}[3].dataset.blocked").strip('"')
        blocked_007 = pw.eval_js(f"{rows}[4].dataset.blocked").strip('"')
        assert blocked_005 == "004", blocked_005
        assert blocked_006 == "005", blocked_006
        assert blocked_007 == "", blocked_007
        assert int(pw.eval_js(
            "document.querySelectorAll('tr.task-row-blocked').length")) == 2
        row_005_text = pw.eval_js(f"{rows}[2].textContent")
        assert "blocked by 004" in row_005_text, row_005_text

        # -- the dialog states the same fact in words, same wording as
        # `ralphctl tasks`/`engine.state.format_task_status` -------------
        pw.eval_js(f"{rows}[2].click()")
        body = _wait_for(pw, "document.querySelector('.text-dialog').textContent",
                         "direct dependent")
        assert "status: pending (blocked by 004)" in body, body
        pw.screenshot(SCREENSHOTS_DIR / "15-task-blocked.png")
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


# The pills under test. `#task-box`'s status column is children[1] (id /
# status / title), so `in-progress` and `pending` share one column and one
# font: whatever height a `pending` pill has is the one-line height here.
# NB: height, not `getClientRects().length` -- an inline-block is one atomic
# box however many lines its *content* wraps onto, so counting rects reports 1
# for a visibly split pill and would pass vacuously.
_PILL = "document.querySelector('#task-box .pill-in-progress')"
_REF_PILL = "document.querySelector('#task-box .pill-pending')"
_PILL_CELL = f"{_PILL}.closest('td')"
_PILL_HEIGHT = f"{_PILL}.getBoundingClientRect().height"
_PILL_WIDTH = f"{_PILL}.getBoundingClientRect().width"
_REF_HEIGHT = f"{_REF_PILL}.getBoundingClientRect().height"
_PILL_CELL_WIDTH = f"{_PILL_CELL}.getBoundingClientRect().width"
# Squeeze the status column far below the pill's natural width, the way a real
# narrow window does. `table-layout: fixed` makes the header widths binding.
# Injected as a stylesheet, not as inline styles on the table: the hub repolls
# every 4 s and `renderTasks` rebuilds the whole table, so inline styles are
# silently dropped mid-test (they were, and the squeeze became a no-op).
#
# Task 024 (#54) added a `Content-Security-Policy: style-src 'self'` header
# to every hub response, which blocks a freshly-created `<style>` element's
# `textContent` (an inline stylesheet) exactly as it would block an inline
# `style=""` attribute -- so the squeeze rules are inserted at runtime into
# `style.css`'s OWN, already same-origin-permitted `CSSStyleSheet` object via
# `insertRule()` instead of appending a new inline `<style>` node. CSP
# governs how a stylesheet resource is *loaded*, not runtime CSSOM mutation
# of a sheet the page was already allowed to load -- confirmed against this
# hub's real CSP header, not merely asserted.
_SQUEEZE = """(() => {
  const sheet = Array.from(document.styleSheets).find(
    s => s.href && s.href.includes('style.css'));
  const rules = [
    '#task-box table{table-layout:fixed;width:90px}',
    '#task-box th:nth-child(1){width:20px}',
    '#task-box th:nth-child(2){width:24px}',
    '#task-box th:nth-child(3){width:46px}',
  ];
  for (const r of rules) sheet.insertRule(r, sheet.cssRules.length);
  return document.querySelectorAll('#task-box thead th').length;
})()"""


def test_a_hyphenated_status_pill_never_breaks_across_two_lines(tmp_path, pw):
    """Operator report (21 Aug 2026): the `in-progress` pill rendered split
    over two lines, border and all, in the running hub.

    Cause: `.pill` was `display: inline-block` with no `white-space` rule, so
    the hyphen in `in-progress` (and in `validation-failed`) is a soft-wrap
    opportunity. Squeeze the column and the badge comes apart -- measured on
    the live hub, the same pill was 17.6px tall with `nowrap` and 31.6px
    without.

    This drives the real geometry rather than asserting on the CSS text: the
    pill is measured in a column deliberately narrower than the pill's own
    text, and must stay exactly as tall as the un-hyphenated `pending` pill
    beside it. The overflow assertion guards against a vacuous pass -- if the
    squeeze did not constrain the pill, its width would not exceed the cell.
    """
    registry = tmp_path / "registry"
    run_dir = _write_dead_run(registry, "run-pill-wrap", state="running", verdict=None)
    (run_dir / "tasks.json").write_text(json.dumps(STALE_TASKS_PLAN))

    server = UiServer(registry)
    server.wait_ready()
    try:
        pw.open(f"{server.base}/#/run/run-pill-wrap")
        _wait_for(pw, "document.body.innerText", "second task")
        assert "in-progress" in pw.eval_js(f"{_PILL}.textContent")
        one_line = float(pw.eval_js(_REF_HEIGHT))
        assert one_line > 0
        # unsqueezed, it is one line -- the baseline the operator should see.
        assert abs(float(pw.eval_js(_PILL_HEIGHT)) - one_line) < 0.5

        # Task 036 (G.4) added the time/cost columns after title -- 5
        # headers now, not 3 -- but the squeeze rules above only target the
        # first three (id/status/title), which is all this test's pill-wrap
        # property needs.
        assert int(pw.eval_js(_SQUEEZE)) == 5
        width = float(pw.eval_js(_PILL_WIDTH))
        height = float(pw.eval_js(_PILL_HEIGHT))
        cell = float(pw.eval_js(_PILL_CELL_WIDTH))
        assert width > cell, (
            f"squeeze did not constrain the pill (pill {width}px <= cell"
            f" {cell}px); the wrap assertion below would pass vacuously")
        # `pending` shares the squeezed column, so it re-measures the one-line
        # height under exactly the constraint the pill under test now faces.
        one_line = float(pw.eval_js(_REF_HEIGHT))
        assert abs(height - one_line) < 0.5, (
            f"the in-progress pill grew to {height}px in a {cell}px column"
            f" while the pending pill beside it stayed {one_line}px -- it broke at"
            " the hyphen; .pill needs white-space: nowrap")
        pw.screenshot(SCREENSHOTS_DIR / "pill-nowrap-squeezed.png")
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

    Task 070 (steer 005 defect 2): the run's FIRST worker iteration is held
    open (`STUB_HOLD_FILE`/`STUB_HOLD_PHASE="worker"`, the same bounded
    barrier task 064 used for the liveness proof) from before the browser
    even opens the page, and released only once `#steer-status` has shown
    "sent" -- i.e. the POST /steering has already been accepted -- so the
    engine can never answer 409 "job finished / steering has no effect"
    because the loop raced ahead to a terminal state while Playwright was
    still filling the form. Before this fix the hold did not exist: a
    fast loop (STUB_SLEEP="1" across 8 iterations, no barrier) could finish
    the whole run inside the real wall-clock time Playwright needs to open
    the page and submit, which is exactly the failure this bounded hold
    rules out rather than papering over with a longer sleep or more
    iterations.
    """
    hold_file = tmp_path / "hold-browser-steering-entry"
    run = live(run_id="steer-hub-ui",
               job={"iterations": 8, "on_complete": "idle"},
               stub_env={"STUB_SLEEP": "1", "STUB_TASKS": "4",
                        "STUB_HOLD_FILE": str(hold_file),
                        "STUB_HOLD_PHASE": "worker", "STUB_HOLD_TIMEOUT_S": "30"})
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

        hold_file.touch()  # release: let the held worker iteration finish

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
        hold_file.touch()  # safety: release even if an assertion above raised
        run.wait_terminal(timeout=120)
        server.stop()


_TIMELINE_ITEMS = "document.querySelectorAll('.timeline-item').length"
_TIMELINE_CLICKABLE = "document.querySelectorAll('.timeline-clickable[role=\"button\"]').length"


def test_timeline_row_opens_the_iteration_dialog(tmp_path, pw):
    """Task 020 (#18.1, restructured into labelled regions by #81): clicking a
    timeline row opens THAT iteration's own story as three distinctly
    LABELLED regions matching the on-disk layout -- outcome (`meta.json`)
    FIRST, then input (`prompt.md`), then logs (`output.jsonl`) -- so an
    iteration that ended with a non-zero exit is readable without scrolling
    past the transcript.

    Everything asserted here is a string Python formatted (`ui_server.
    iteration_view` -> `state.iteration_summary_lines`/`read_iteration_prompt`
    + the shared `log_render`), so the hub cannot word an exit reason or a
    cost differently from `ralphctl iteration`; the fixture has no container
    at all, since the endpoint is on-disk by design. Also pins the rendering
    discipline (a transcript containing markup stays TEXT) and that the
    dialog does not stack across the 4s `load()` rebuild behind it.
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
    prompts = {1: "# planning prompt\ndo the planning",
              2: "# worker prompt\ndo the work"}
    for n, meta in metas.items():
        d = run_dir / "iterations" / f"{n:04d}"
        d.mkdir(parents=True)
        (d / "meta.json").write_text(json.dumps(meta))
        (d / "prompt.md").write_text(prompts[n])
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
        assert "do the work" in body, body       # the input region's prompt.md
        # ...and only THIS iteration's log/prompt
        assert "planning transcript line" not in body, body
        assert "do the planning" not in body, body

        # -- three distinctly LABELLED regions, outcome FIRST --------------
        region_ids = pw.eval_js(
            "[...document.querySelectorAll('.text-dialog .iteration-region')]"
            ".map(e => e.id).join(',')")
        assert region_ids.strip('"') == (
            "iteration-region-outcome,iteration-region-input,"
            "iteration-region-logs")
        labels = pw.eval_js(
            "[...document.querySelectorAll('.text-dialog .iteration-region-label')]"
            ".map(e => e.textContent).join('|')")
        assert labels.strip('"') == (
            "Outcome (meta.json)|Input (prompt.md)|Logs (output.jsonl)")
        # the outcome fact is readable at the TOP, before the transcript --
        # its region comes first in document order, not just in the string
        outcome_before_logs = pw.eval_js(
            "document.querySelector('#iteration-region-outcome')"
            ".compareDocumentPosition(document.querySelector('#iteration-region-logs'))"
            " & Node.DOCUMENT_POSITION_FOLLOWING")
        assert int(outcome_before_logs) != 0

        # every region's own text came from the server's own field, verbatim
        view = iteration_view(registry, "run-iter-dialog", 2)
        outcome_text = pw.eval_js(
            "document.querySelector('#iteration-region-outcome .dialog-body')"
            ".textContent")
        input_text = pw.eval_js(
            "document.querySelector('#iteration-region-input .dialog-body')"
            ".textContent")
        logs_text = pw.eval_js(
            "document.querySelector('#iteration-region-logs .dialog-body')"
            ".textContent")
        assert (json.loads(outcome_text) if outcome_text.startswith('"')
               else outcome_text) == view["outcomeText"]
        assert (json.loads(input_text) if input_text.startswith('"')
               else input_text) == view["promptText"]
        assert (json.loads(logs_text) if logs_text.startswith('"')
               else logs_text) == view["logsText"]
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
        # Task 008 (#57): the same race the job.yaml document dialog above
        # retries through -- the 4s `load()` poll's `renderTimeline` rebuild
        # can replace this row (clearing its focus) between the `.focus()`
        # eval below and the separate `press Enter` invocation, two
        # independent playwright-cli subprocess calls, not one atomic
        # action, through no fault of the row. Proven with a scratch,
        # much-faster poll copy of app.js in
        # artifacts/reports/task-008-timeline-dialog-diagnosis.md: today's
        # single-attempt version failed 10/10 there; this retry loop passed
        # 10/10. Retry the whole focus+press cycle until the dialog opens.
        focus_js = (
            "(() => { const el = document.querySelector('.timeline-item"
            "[data-iteration=\"1\"]'); if (!el) return 'missing';"
            " el.focus(); return document.activeElement === el ? 'focused' : 'lost'; })()")
        deadline = time.time() + 40
        body = ""
        while time.time() < deadline and "iteration: 1" not in body:
            if pw.eval_js(focus_js).strip('"') != "focused":
                continue
            assert pw.run("press", "Enter").returncode == 0
            for _ in range(10):
                body = pw.eval_js(
                    "document.querySelector('.text-dialog')"
                    " ? document.querySelector('.text-dialog').textContent : ''")
                if "iteration: 1" in body:
                    break
                time.sleep(0.2)
        assert "iteration: 1" in body, body
        assert "phase planning" in body, body
        assert EXIT_REASON_CLEAN in body, body
        assert "planning transcript line" in body, body
        assert "worker said" not in body, body
        assert int(pw.eval_js(_DIALOGS)) == 1
    finally:
        server.stop()


def test_timeline_row_and_dialog_show_the_same_cause_for_a_faulted_iteration(tmp_path, pw):
    """Task 015 (#89, requirement I, hub half): a faulted iteration's cause
    sentence -- `<faultClass> -- <reason>`, `engine.state.iteration_detail`'s
    `causeText` -- is visible on the TIMELINE ROW itself, not only after
    opening the dialog, and the two read the exact same string.

    The fixture is task 013/014's own two-fault fixture (#89) -- iteration 2's
    infra/DNS fault and iteration 5's later, unrelated work fault -- read
    through the SAME `_base_meta` helper `tests/test_cli_iteration_detail.py`'s
    `test_iteration_prints_its_own_cause_task014` asserts against via
    `ralphctl iteration`, so the CLI assertion and this browser assertion agree
    by construction rather than by two hand-written fixtures happening to
    match. Iteration 1 (no fault) proves the row shows NO cause span at all --
    the absence is itself part of the contract (`causeText: None`).
    """
    from ralphd.cli.ui_server import run_detail
    from ralphd.engine.faults import FAULT_REASON_SIGNATURE, FAULT_REASON_WORK

    sys.path.insert(0, str(Path(__file__).parent))
    from test_cli_iteration_detail import DNS_ERROR, WORK_ERROR, _base_meta

    registry = tmp_path / "registry"
    run_dir = _write_dead_run(registry, "run-cause-parity", state="failed",
                              verdict="unverified")
    metas = {
        1: _base_meta(1, error=None, exitCode=0, faultClass=None,
                     usage={"totalTokens": 30, "input": 10, "output": 20,
                            "costUSD": 0.0125, "costPriced": True}),
        2: _base_meta(2, error=DNS_ERROR, exitCode=0, faultClass="infra",
                     usage=None),
        5: _base_meta(5, error=WORK_ERROR, exitCode=1, faultClass="work",
                     usage={"totalTokens": 4000, "input": 3000, "output": 1000}),
    }
    for n, meta in metas.items():
        d = run_dir / "iterations" / f"{n:04d}"
        d.mkdir(parents=True)
        (d / "meta.json").write_text(json.dumps(meta))

    server = UiServer(registry)
    server.wait_ready()
    try:
        pw.open(f"{server.base}/#/run/run-cause-parity")
        _wait_for_count_ge(pw, _TIMELINE_ITEMS, 3)

        # -- the server's own payload: the timeline row and the dialog it
        # opens are both built from THIS -- `run_detail`'s `iterations` list
        # is `engine.state.iteration_detail` per number (task 015).
        detail = run_detail(registry, "run-cause-parity")
        by_number = {it["number"]: it for it in detail["iterations"]}
        assert by_number[2]["causeText"] == f"infra -- {FAULT_REASON_SIGNATURE}"
        assert by_number[5]["causeText"] == f"work -- {FAULT_REASON_WORK}"
        assert by_number[1]["causeText"] is None

        # -- the TIMELINE ROW itself names the cause, unopened -------------
        row2 = pw.eval_js(
            "document.querySelector('.timeline-item[data-iteration=\"2\"]')"
            ".textContent")
        assert f"infra -- {FAULT_REASON_SIGNATURE}" in row2, row2
        row5 = pw.eval_js(
            "document.querySelector('.timeline-item[data-iteration=\"5\"]')"
            ".textContent")
        assert f"work -- {FAULT_REASON_WORK}" in row5, row5
        # the unfaulted iteration's row names no cause at all
        row1 = pw.eval_js(
            "document.querySelector('.timeline-item[data-iteration=\"1\"]')"
            ".querySelectorAll('.cause').length")
        assert int(row1) == 0
        pw.screenshot(SCREENSHOTS_DIR / "24-timeline-cause.png")

        # -- the DIALOG the row opens says the very same thing -------------
        pw.click('.timeline-item[data-iteration="2"]')
        body = _wait_for(pw, "document.querySelector('.text-dialog').textContent",
                         "iteration: 2")
        assert f"cause:     infra -- {FAULT_REASON_SIGNATURE}" in body, body
    finally:
        server.stop()


def test_timeline_row_shows_task_id_tier_and_cost(tmp_path, pw):
    """Task 034 (G.2): a hub iteration row names the task it was dispatched
    against, the effective tier it ran at, and its own cost -- beside what
    the row already showed (phase/model/duration/etc).

    `assignedTaskId`/`effectiveTier` are task 027 (F.1)'s dispatch-write
    fields, passed through `engine.state.iteration_detail` verbatim (never
    re-derived -- neither name is in `ITERATION_DERIVED_KEYS`); the cost is
    that SAME iteration's own `costDisplay` (`format_cost(usage,
    decimals=4)`), never recomputed here. Built off the same `_base_meta`
    fixture helper the cause-parity test above uses, so the usage dict this
    asserts against is the one `format_cost` already has coverage for.
    """
    from ralphd.cli.ui_server import run_detail

    sys.path.insert(0, str(Path(__file__).parent))
    from test_cli_iteration_detail import _base_meta

    registry = tmp_path / "registry"
    run_dir = _write_dead_run(registry, "run-task-tier-cost", state="failed",
                              verdict="unverified")
    meta = _base_meta(1, assignedTaskId="041", effectiveTier="hard",
                      usage={"input": 10, "output": 20, "totalTokens": 30,
                             "costUSD": 0.0125, "costPriced": True})
    d = run_dir / "iterations" / "0001"
    d.mkdir(parents=True)
    (d / "meta.json").write_text(json.dumps(meta))

    server = UiServer(registry)
    server.wait_ready()
    try:
        pw.open(f"{server.base}/#/run/run-task-tier-cost")
        _wait_for_count_ge(pw, _TIMELINE_ITEMS, 1)

        # -- the server's own payload, the same one the row is built from --
        detail = run_detail(registry, "run-task-tier-cost")
        row_data = detail["iterations"][0]
        assert row_data["assignedTaskId"] == "041"
        assert row_data["effectiveTier"] == "hard"
        cost_display = row_data["costDisplay"]
        assert cost_display and cost_display != "unavailable"

        # -- the TIMELINE ROW itself names all three, unopened -------------
        row_text = pw.eval_js(
            "document.querySelector('.timeline-item[data-iteration=\"1\"]')"
            ".textContent")
        assert "task: 041" in row_text, row_text
        assert "tier: hard" in row_text, row_text
        assert cost_display in row_text, row_text
        pw.screenshot(SCREENSHOTS_DIR / "25-timeline-task-tier-cost.png")
    finally:
        server.stop()


def test_task_table_shows_summed_time_and_cost_across_two_iterations(tmp_path, pw):
    """Task 036 (G.4): the hub tasks table's new time/cost columns are
    AGGREGATED per task, not the last iteration's own figures -- a task that
    took two attempts (a validation-failed re-entry dispatching a second
    iteration against the SAME task id) must show the SUM across both, never
    just the second attempt's numbers.

    Two iterations both carry `assignedTaskId: "030"` -- iteration 1 (the
    first attempt, ended in `validation-failed`) and iteration 2 (the
    re-claim that finished it) -- each with its own duration and its own
    fully-priced `usage`, built off the same `_base_meta` fixture helper task
    034's test above uses. `engine.state.task_totals` sums `durationS` and
    rolls the two `usage` dicts together (`_accumulate_task_usage`, the same
    algorithm `LoopSupervisor._merge_usage` uses for status.json's own
    buckets), so the expected total is computed here from the SAME two
    numbers the fixture writes, never a hand-typed literal that could drift
    from the fixture.
    """
    from ralphd.cli.ui_server import run_detail

    sys.path.insert(0, str(Path(__file__).parent))
    from test_cli_iteration_detail import _base_meta

    registry = tmp_path / "registry"
    run_dir = _write_dead_run(registry, "run-task-totals", state="succeeded",
                              verdict="verified")
    (run_dir / "tasks.json").write_text(json.dumps({
        "version": 1, "goal": "ship it",
        "tasks": [{"id": "030", "title": "add the totals column",
                   "status": "completed", "validationAttempts": 1}],
    }))

    meta1 = _base_meta(1, assignedTaskId="030",
                       startedAt="2026-09-02T10:00:00Z",
                       endedAt="2026-09-02T10:01:00Z",
                       usage={"input": 10, "output": 20, "totalTokens": 30,
                              "costUSD": 0.01, "costPriced": True})
    meta2 = _base_meta(2, assignedTaskId="030",
                       startedAt="2026-09-02T11:00:00Z",
                       endedAt="2026-09-02T11:02:00Z",
                       usage={"input": 15, "output": 25, "totalTokens": 40,
                              "costUSD": 0.02, "costPriced": True})
    for number, meta in ((1, meta1), (2, meta2)):
        d = run_dir / "iterations" / f"{number:04d}"
        d.mkdir(parents=True)
        (d / "meta.json").write_text(json.dumps(meta))

    server = UiServer(registry)
    server.wait_ready()
    try:
        pw.open(f"{server.base}/#/run/run-task-totals")
        _wait_for_count_ge(pw, "document.querySelectorAll('#task-box tbody tr').length", 1)

        # -- the server's own payload -- the expected sum, derived from the
        # SAME two iterations, never a hand-typed literal --------------------
        detail = run_detail(registry, "run-task-totals")
        task_row = detail["tasks"]["tasks"][0]
        assert task_row["id"] == "030"
        expected_time = task_row["timeDisplay"]
        expected_cost = task_row["costDisplay"]
        assert expected_time == "3m"  # 60s + 120s
        assert expected_cost == "$0.0300"  # 0.01 + 0.02

        # -- the TASK TABLE ROW itself shows the summed figure, unopened -----
        row_text = pw.eval_js(
            "document.querySelector('tr.task-row[data-task-id=\"030\"]')"
            ".textContent")
        assert expected_time in row_text, row_text
        assert expected_cost in row_text, row_text
        pw.screenshot(SCREENSHOTS_DIR / "26-task-table-summed-time-cost.png")
    finally:
        server.stop()


def test_task_dialog_lists_the_iterations_behind_its_total(tmp_path, pw):
    """Task 037 (G.5): the task dialog opened from the tasks table lists the
    INDIVIDUAL iterations the row's own timeDisplay/costDisplay total (task
    036, G.4) was summed from -- each with its tier and its own per-
    iteration cost -- bounded to exactly the fixture's own two iterations
    (never a third, never a different task's).

    Same two-iteration fixture shape as
    `test_task_table_shows_summed_time_and_cost_across_two_iterations`
    above (task id `030`, one `validation-failed` re-entry) plus a THIRD
    iteration dispatched against a DIFFERENT task (`999`), so the breakdown
    proves it is bounded to task `030`'s own two, not every iteration in the
    run.
    """
    from ralphd.cli.ui_server import run_detail

    sys.path.insert(0, str(Path(__file__).parent))
    from test_cli_iteration_detail import _base_meta

    registry = tmp_path / "registry"
    run_dir = _write_dead_run(registry, "run-task-breakdown", state="succeeded",
                              verdict="verified")
    (run_dir / "tasks.json").write_text(json.dumps({
        "version": 1, "goal": "ship it",
        "tasks": [{"id": "030", "title": "add the totals column",
                   "status": "completed", "validationAttempts": 1},
                  {"id": "999", "title": "an unrelated task",
                   "status": "completed"}],
    }))

    meta1 = _base_meta(1, assignedTaskId="030", effectiveTier="normal",
                       startedAt="2026-09-02T10:00:00Z",
                       endedAt="2026-09-02T10:01:00Z",
                       usage={"input": 10, "output": 20, "totalTokens": 30,
                              "costUSD": 0.01, "costPriced": True})
    meta2 = _base_meta(2, assignedTaskId="030", effectiveTier="hard",
                       startedAt="2026-09-02T11:00:00Z",
                       endedAt="2026-09-02T11:02:00Z",
                       usage={"input": 15, "output": 25, "totalTokens": 40,
                              "costUSD": 0.02, "costPriced": True})
    meta3 = _base_meta(3, assignedTaskId="999", effectiveTier="normal",
                       startedAt="2026-09-02T12:00:00Z",
                       endedAt="2026-09-02T12:01:00Z",
                       usage={"input": 1, "output": 1, "totalTokens": 2,
                              "costUSD": 0.05, "costPriced": True})
    for number, meta in ((1, meta1), (2, meta2), (3, meta3)):
        d = run_dir / "iterations" / f"{number:04d}"
        d.mkdir(parents=True)
        (d / "meta.json").write_text(json.dumps(meta))

    # -- the server's own payload: what the breakdown must reproduce --------
    detail = run_detail(registry, "run-task-breakdown")
    task_row = next(t for t in detail["tasks"]["tasks"] if t["id"] == "030")
    breakdown = task_row["iterationsBreakdown"]
    assert [b["number"] for b in breakdown] == [1, 2]
    cost1, cost2 = breakdown[0]["costDisplay"], breakdown[1]["costDisplay"]

    server = UiServer(registry)
    server.wait_ready()
    try:
        pw.open(f"{server.base}/#/run/run-task-breakdown")
        _wait_for_count_ge(pw, "document.querySelectorAll('#task-box tbody tr').length", 1)

        pw.click('tr.task-row[data-task-id="030"]')
        body = _wait_for(pw, "document.querySelector('.text-dialog').textContent",
                         "iterations behind this total")
        # -- exactly the two iterations task 030's own total was computed
        # from, each with its own tier and cost, and NOT iteration 3 (a
        # different task) -----------------------------------------------
        assert "#1" in body and "tier: normal" in body, body
        assert "#2" in body and "tier: hard" in body, body
        assert cost1 in body, body
        assert cost2 in body, body
        assert "#3" not in body, body
        pw.screenshot(SCREENSHOTS_DIR / "27-task-dialog-iteration-breakdown.png")
    finally:
        server.stop()


def test_pre_dispatch_record_run_dir_still_renders(tmp_path, pw):
    """Task 038 (G.6): a run dir written BEFORE the dispatch-record change
    (task 027, F.1's `assignedTaskId`/`effectiveTier` on each iteration's own
    `meta.json`, and task 030, F.4's `engineVersion` on `status.json`) must
    still render -- as blank or an em dash where those fields would have
    gone -- rather than raising anywhere along the chain (`engine.state.
    iteration_detail`/`task_totals`, `ui_server.run_detail`, `app.js`'s
    `renderTimeline`/`renderTasks`/`renderSummary`).

    The fixture is exactly that pre-change shape: `_base_meta()` with none
    of its `over` kwargs used, so the written `meta.json` carries no
    `assignedTaskId`/`effectiveTier` key at all (not merely a null value --
    absent, the way a real pre-027 iteration dir would be), and
    `_write_dead_run`'s default `status.json` (no `engineVersion` kwarg),
    the same default `test_run_page_shows_the_engine_version_it_is_executing`
    (task 035) opts OUT of by passing `engineVersion="0.21.0"` explicitly --
    here it is left out on purpose.
    """
    from ralphd.cli.ui_server import run_detail

    sys.path.insert(0, str(Path(__file__).parent))
    from test_cli_iteration_detail import _base_meta

    registry = tmp_path / "registry"
    run_dir = _write_dead_run(registry, "run-pre-dispatch-record",
                              state="succeeded", verdict="verified")
    (run_dir / "tasks.json").write_text(json.dumps({
        "version": 1, "goal": "ship it",
        "tasks": [{"id": "030", "title": "a task from before the change",
                   "status": "completed"}],
    }))
    meta = _base_meta(1)
    assert "assignedTaskId" not in meta and "effectiveTier" not in meta
    d = run_dir / "iterations" / "0001"
    d.mkdir(parents=True)
    (d / "meta.json").write_text(json.dumps(meta))

    # -- the server's own payload never raises building this ----------------
    detail = run_detail(registry, "run-pre-dispatch-record")
    assert detail["iterations"][0].get("assignedTaskId") is None
    assert detail["iterations"][0].get("effectiveTier") is None
    task_row = next(t for t in detail["tasks"]["tasks"] if t["id"] == "030")
    assert "timeDisplay" not in task_row and "costDisplay" not in task_row

    server = UiServer(registry)
    server.wait_ready()
    try:
        pw.open(f"{server.base}/#/run/run-pre-dispatch-record")
        _wait_for_count_ge(pw, _TIMELINE_ITEMS, 1)

        # -- the iteration row: em dash for task and tier ---------------------
        row_text = pw.eval_js(
            "document.querySelector('.timeline-item[data-iteration=\"1\"]')"
            ".textContent")
        assert "task: \u2014" in row_text, row_text
        assert "tier: \u2014" in row_text, row_text

        # -- the tasks table: em dash for time and cost -----------------------
        _wait_for_count_ge(pw, "document.querySelectorAll('#task-box tbody tr').length", 1)
        task_text = pw.eval_js(
            "document.querySelector('tr.task-row[data-task-id=\"030\"]')"
            ".textContent")
        assert "\u2014" in task_text, task_text

        # -- the run page itself: no exception, no "engine: " row at all -----
        body_text = pw.eval_js("document.body.innerText")
        assert "engine: " not in body_text, body_text
        pw.screenshot(SCREENSHOTS_DIR / "28-pre-dispatch-record-run-dir.png")
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
    from ralphd.engine.state import (
        JOB_CONFIG_FILE, RUN_DOCUMENT_ABSENT, RUN_DOCUMENTS,
    )

    # this fixture writes exactly these three documents; every other key in
    # `state.RUN_DOCUMENTS` is absent for a run that never reached
    # `finishing` -- derived here so a new key (like task 006's `debt` row)
    # shifts this count automatically instead of going stale.
    written_keys = {"notes", "findings", "job"}
    expected_button_count = len(written_keys)
    expected_absent_count = len(RUN_DOCUMENTS) - len(written_keys)

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
        assert int(pw.eval_js(_DOCUMENT_BUTTONS)) == expected_button_count
        # not 1: task 007 added two unconditional `run-record` rows to
        # `state.RUN_DOCUMENTS`, and task 006 (B.1) added `debt`, so a run
        # that never reached `finishing` is missing composite-prd.md,
        # debt.md, run-record.md and run-record.json.
        assert int(pw.eval_js(
            "document.querySelectorAll('[data-document-absent]').length")
            ) == expected_absent_count
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
        # one row per `RUN_DOCUMENTS` key: notes, findings, job + every
        # absent row (composite-prd, debt, run-record, run-record-json).
        assert int(pw.eval_js(_DOCUMENT_ITEMS)) == len(RUN_DOCUMENTS)  # re-rendered
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

        # -- task 021 (#82): collapsed by default behind a native <details>,
        # summary carries the COUNT so collapsing never hides whether the run
        # left anything behind
        assert pw.eval_js(
            "document.querySelector('.artifact-list-details').open") == "false"
        summary_text = pw.eval_js(
            "document.querySelector('.artifact-list-details summary').textContent")
        assert "3" in summary_text, summary_text
        pw.click(".artifact-list-details summary")
        assert pw.eval_js(
            "document.querySelector('.artifact-list-details').open") == "true"

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
        # poll legitimately replaces the panel's nodes (collapsing the
        # `<details>` back to its default, task 021/#82), so a focus taken
        # just before one lands is lost through no fault of the button:
        # re-open the details and retry the whole focus+Enter cycle (never a
        # mouse click here).
        focus_js = (
            "(() => { const d = document.querySelector('.artifact-list-details');"
            " if (d && !d.open) d.open = true;"
            " const el = document.querySelector('button.artifact-item"
            "[data-artifact-key=\"suggestions\"]'); if (!el) return 'missing';"
            " el.focus();"
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


# A real 1x1 PNG (not just the eight magic-byte header): a browser can
# actually decode it, so the test below can assert on `naturalWidth`, not
# merely on the presence of a src attribute.
_PNG_1X1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY"
    "42YAAAAASUVORK5CYII=")


def test_run_detail_artifact_dialog_branches_on_server_kind(tmp_path, pw):
    """Task 025 (#54): the artifact dialog's SHAPE is decided from the
    server's own `kind` field (task 022's magic-byte sniff), never from a
    client-side extension list. A previewable image gets an `<img>` pointed
    at the raw byte route (task 023) and actually renders it (`naturalWidth`
    > 0, not just a src attribute); a tarball -- opaque binary -- still gets
    today's `pull` wording in the plain text dialog, unchanged.

    Also proves the file's NAME does not decide the shape: `renamed.bin` is a
    real PNG under a non-image extension and still gets the image dialog,
    because `kind` is sniffed from the bytes, never the name.
    """
    from ralphd.engine.state import ARTIFACT_BINARY

    registry = tmp_path / "registry"
    run_dir = _write_dead_run(registry, "run-arts-kinds", state="succeeded",
                              verdict="verified")
    artifacts = run_dir / "artifacts"
    artifacts.mkdir(parents=True, exist_ok=True)
    (artifacts / "screenshot.png").write_bytes(_PNG_1X1)
    (artifacts / "renamed.bin").write_bytes(_PNG_1X1)
    (artifacts / "archive.tar.gz").write_bytes(
        b"\x1f\x8b\x08\x00\x00\x00\x00\x00\x00\x03" + bytes(range(200)))

    server = UiServer(registry)
    server.wait_ready()
    try:
        pw.open(f"{server.base}/#/run/run-arts-kinds")
        _wait_for_count_ge(pw, _ARTIFACT_ITEMS, 3)
        assert int(pw.eval_js(_DIALOGS)) == 0
        # task 021 (#82): expand the collapsed-by-default artifact list
        # before clicking any row inside it.
        pw.eval_js("(document.querySelector('.artifact-list-details').open = true)")

        # -- a previewable image: <img>, not <pre> -------------------------
        pw.click('button.artifact-item[data-artifact="screenshot.png"]')
        _wait_for(pw, "document.querySelectorAll('.text-dialog img').length",
                 "1")
        assert int(pw.eval_js(_DIALOGS)) == 1
        assert int(pw.eval_js(
            "document.querySelectorAll('.text-dialog pre').length")) == 0
        src = pw.eval_js(
            "document.querySelector('.text-dialog img').getAttribute('src')")
        assert ("/api/runs/run-arts-kinds/artifacts-raw/screenshot.png"
                in src), src
        width = _wait_for(
            pw, "document.querySelector('.text-dialog img').naturalWidth",
            "1")
        assert int(width) > 0, width  # the browser actually decoded real bytes
        pw.screenshot(SCREENSHOTS_DIR / "25-artifact-image-dialog.png")
        pw.click(".text-dialog .dialog-close button")
        deadline = time.time() + 10
        while time.time() < deadline and pw.eval_js(_DIALOGS) != "0":
            time.sleep(0.2)
        assert int(pw.eval_js(_DIALOGS)) == 0

        # -- a real PNG under a non-image extension: still the image dialog
        pw.click('button.artifact-item[data-artifact="renamed.bin"]')
        _wait_for(pw, "document.querySelectorAll('.text-dialog img').length",
                 "1")
        src2 = pw.eval_js(
            "document.querySelector('.text-dialog img').getAttribute('src')")
        assert ("/api/runs/run-arts-kinds/artifacts-raw/renamed.bin"
                in src2), src2
        pw.click(".text-dialog .dialog-close button")
        deadline = time.time() + 10
        while time.time() < deadline and pw.eval_js(_DIALOGS) != "0":
            time.sleep(0.2)
        assert int(pw.eval_js(_DIALOGS)) == 0

        # -- a tarball: today's `pull` wording, plain text, no <img> -------
        pw.click('button.artifact-item[data-artifact="archive.tar.gz"]')
        body = _wait_for(pw, "document.querySelector('.text-dialog').textContent",
                         "archive.tar.gz")
        assert ARTIFACT_BINARY in body, body
        assert int(pw.eval_js(
            "document.querySelectorAll('.text-dialog img').length")) == 0
        assert int(pw.eval_js(
            "document.querySelectorAll('.text-dialog pre').length")) == 1
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


# ---------------------------------------- approach history panel (#24) ----

_APPROACH_ENTRIES = "document.querySelectorAll('.approach-entry').length"
_APPROACH_SEC_VISIBLE = (
    "getComputedStyle(document.getElementById('approach-history-sec'))"
    ".display !== 'none'")


def _seed_approach_history_run(registry, run_id):
    """A dead run with two archived approaches (task 020's fixture files),
    each with its OWN task list and its OWN notes.md/review-findings.md,
    deliberately different from both each other and the live plan -- so a
    row that shows the wrong approach's data is caught."""
    run_dir = _write_dead_run(registry, run_id, state="failed",
                              verdict="unverified", approach=3)
    (run_dir / "tasks.json").write_text(json.dumps(
        {"tasks": [{"id": "901", "title": "live plan, approach 3",
                    "status": "pending"}]}))
    a1 = run_dir / "approaches" / "01"
    a1.mkdir(parents=True)
    (a1 / "tasks.json").write_text(json.dumps(
        {"tasks": [{"id": "001", "title": "approach one, task one",
                    "status": "completed"},
                   {"id": "002", "title": "approach one, task two",
                    "status": "failed",
                    "notes": "gave up: requirement C unmet"}]}))
    (a1 / "notes.md").write_text("# Notes (approach 1)\nran out of budget\n")
    (a1 / "review-findings.md").write_text(
        "# Findings (approach 1)\nmissed requirement C entirely\n")
    a2 = run_dir / "approaches" / "02"
    a2.mkdir(parents=True)
    (a2 / "tasks.json").write_text(json.dumps(
        {"tasks": [{"id": "001", "title": "approach two, task one",
                    "status": "completed"}]}))
    # deliberately no notes.md/review-findings.md for approach 2 -- an
    # archived approach can have nothing to say and must still answer cleanly
    return run_dir


def test_run_detail_approach_history_expands_a_previous_approach(tmp_path, pw):
    """Task 021 (#24): every previous approach `_archive_approach` froze into
    `approaches/NN/` is one collapsed row away, most recent last, expanding to
    that approach's OWN task table and its OWN notes.md/review-findings.md --
    never the live plan's, and never another approach's. A run still on its
    first approach (nothing archived) shows no history panel at all.

    Drives the real accordion (`<details>`/`<summary>`) with a real click, and
    proves the panel consumes only the server's `GET /api/runs/<id>/approaches`
    and `GET /api/runs/<id>/approaches/<n>` -- no client-side counting.
    """
    from ralphd.cli.ui_server import approach_view
    from ralphd.engine.state import RUN_DOCUMENT_ABSENT

    registry = tmp_path / "registry"
    _seed_approach_history_run(registry, "run-approach-hist")
    _write_dead_run(registry, "run-approach-none", state="succeeded",
                    verdict="verified", approach=1)

    server = UiServer(registry)
    server.wait_ready()
    try:
        # -- a run still on its first approach: no panel at all -------------
        pw.open(f"{server.base}/#/run/run-approach-none")
        _wait_for(pw, "document.body.innerText", "succeeded")
        time.sleep(1.0)   # give the async approaches fetch a beat to land
        assert pw.eval_js(_APPROACH_SEC_VISIBLE) == "false"
        assert int(pw.eval_js(_APPROACH_ENTRIES)) == 0

        # -- a run with two archived approaches ------------------------------
        pw.open(f"{server.base}/#/run/run-approach-hist")
        _wait_for_count_ge(pw, _APPROACH_ENTRIES, 2)
        assert pw.eval_js(_APPROACH_SEC_VISIBLE) == "true"
        # most recent last -- approach 1's row precedes approach 2's
        order = pw.eval_js(
            "[...document.querySelectorAll('.approach-entry')]"
            ".map(e => e.dataset.approach).join(',')")
        assert order.strip('"') == "1,2", order
        # collapsed by default, no fetch made yet: the live task table above
        # is untouched by this panel's data
        assert pw.eval_js(
            "document.querySelector('.approach-entry[data-approach=\"1\"]').open"
        ) == "false"
        live_rows = pw.eval_js(
            "[...document.querySelectorAll('#task-box tr[data-task-id]')]"
            ".map(e => e.dataset.taskId).join(',')")
        assert live_rows.strip('"') == "901", live_rows

        # -- expand approach 1's row: its OWN task table shows up -----------
        pw.click('.approach-entry[data-approach="1"] summary')
        rows = _wait_for(
            pw,
            "[...document.querySelectorAll('.approach-entry[data-approach=\"1\"]"
            " tr[data-task-id]')].map(e => e.dataset.taskId).join(',')",
            "001")
        assert "002" in rows, rows
        # the live task table above is untouched -- still the live plan's row
        live_rows = pw.eval_js(
            "[...document.querySelectorAll('#task-box tr[data-task-id]')]"
            ".map(e => e.dataset.taskId).join(',')")
        assert live_rows.strip('"') == "901", live_rows
        pw.screenshot(SCREENSHOTS_DIR / "33-approach-history-expanded.png")

        # -- and its OWN notes.md/review-findings.md, not another approach's --
        assert int(pw.eval_js(
            "document.querySelectorAll('.approach-entry[data-approach=\"1\"]"
            " .document-item').length")) == 2
        assert int(pw.eval_js(_DIALOGS)) == 0
        pw.click('.approach-entry[data-approach="1"]'
                 ' button.document-item[data-document="findings"]')
        body = _wait_for(pw, "document.querySelector('.text-dialog').textContent",
                         "missed requirement C entirely")
        assert "approach 1" in body.lower() or "1" in body, body
        raw = pw.eval_js("document.querySelector('.text-dialog .dialog-body')"
                         ".textContent")
        dialog_body = json.loads(raw) if raw.startswith('"') else raw
        assert dialog_body == approach_view(
            registry, "run-approach-hist", 1)["findings"]["text"]
        pw.click(".text-dialog .dialog-close button")
        deadline = time.time() + 10
        while time.time() < deadline and pw.eval_js(_DIALOGS) != "0":
            time.sleep(0.2)

        # -- approach 2's row: no notes/findings ever written, still clean ---
        assert pw.eval_js(
            "document.querySelector('.approach-entry[data-approach=\"2\"]').open"
        ) == "false"
        pw.click('.approach-entry[data-approach="2"] summary')
        _wait_for(
            pw,
            "[...document.querySelectorAll('.approach-entry[data-approach=\"2\"]"
            " tr[data-task-id]')].map(e => e.dataset.taskId).join(',')",
            "001")
        text = pw.eval_js(
            "document.querySelector('.approach-entry[data-approach=\"2\"]')"
            ".textContent")
        assert RUN_DOCUMENT_ABSENT in text, text
        assert int(pw.eval_js(
            "document.querySelectorAll('.approach-entry[data-approach=\"2\"]"
            " [data-document-absent]').length")) == 2
    finally:
        server.stop()


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


# --------------------------------------------------------------------------
# task 031 (#19): the delete affordance
# --------------------------------------------------------------------------

# The recording stub `docker` (tests/stub-docker/docker): the hub's delete goes
# through the CLI's real removal sequence, and this test is about the browser
# half -- it must not talk to a real daemon to prove that a row disappears.
STUB_DOCKER = Path(__file__).parent / "stub-docker" / "docker"

_DIALOG_COUNT = "document.querySelectorAll('dialog').length"
_DELETE_BUTTONS = "document.querySelectorAll('button.delete-run').length"
_ROW_IDS = ("[...document.querySelectorAll('table.run-list tbody tr a')]"
            ".map(a => a.textContent).join(',')")


def _delete_button(run_id: str) -> str:
    return f"document.querySelector('button.delete-run[data-delete-run=\"{run_id}\"]')"


def _row_run_ids(session: Pw) -> list[str]:
    raw = session.eval_js(_ROW_IDS).strip('"')
    return [x for x in raw.split(",") if x]


def _wait_until_gone(session: Pw, run_id: str, timeout=20):
    deadline = time.time() + timeout
    ids: list[str] = []
    while time.time() < deadline:
        ids = _row_run_ids(session)
        if run_id not in ids:
            return ids
        time.sleep(0.3)
    raise AssertionError(f"{run_id} still listed after {timeout}s: {ids}")


def test_run_list_and_detail_delete_a_run_behind_a_confirm_dialog(tmp_path, pw):
    """Task 031 (#19): deleting a finished run used to mean leaving the hub for
    `ralphctl stop` + `ralphctl rm`, and the hub had no delete affordance at
    all.

    Drives the real thing in a real Chromium: the run list and the run detail
    page both offer a delete button for a TERMINAL run only, behind a
    confirmation naming the run id; the active run's button is disabled with
    the server's own refusal sentence shown next to it; cancelling leaves the
    run on disk; confirming really removes it (row gone from the table, run
    dir gone from the registry) while the other runs are untouched; and the
    single-dialog invariant holds across the 4s `load()` rebuild behind it.
    """
    from ralphd.cli import ui_server
    registry = tmp_path / "registry"
    _write_dead_run(registry, "del-done", state="succeeded", verdict="verified")
    _write_dead_run(registry, "del-detail", state="failed", verdict=None)
    engine = StubEngineApi(status={
        "runId": "del-live", "state": "running", "verdict": None,
        "phase": "worker", "approach": 1, "iterationsUsed": 2,
        "iterationsBudget": 25, "startedAt": "2026-01-01T00:00:00Z",
    })
    _write_run_with_api(registry, "del-live", engine, state="running", verdict=None)

    server = UiServer(registry, env={
        "RALPHD_DOCKER": str(STUB_DOCKER),
        "STUB_DOCKER_LOG": str(tmp_path / "docker-argv.jsonl"),
    })
    server.wait_ready()
    try:
        # -- the run list: one control per row, enabled only where it works --
        pw.open(server.base)
        _wait_for(pw, "document.body.innerText", "del-done")
        _wait_for_count_ge(pw, _DELETE_BUTTONS, 3)
        assert pw.eval_js(_delete_button("del-done") + ".disabled") == "false"
        assert pw.eval_js(_delete_button("del-detail") + ".disabled") == "false"
        # ...and the live run's is disabled, with the reason SHOWN (the
        # server's `deleteRefusal`, ui_server.DELETE_REFUSED_ACTIVE)
        assert pw.eval_js(_delete_button("del-live") + ".disabled") == "true"
        refusals = pw.eval_js(
            "[...document.querySelectorAll('.delete-refusal')]"
            ".map(e => e.textContent).join('|')").strip('"')
        assert refusals == ui_server.DELETE_REFUSED_ACTIVE.format(state="running"), \
            refusals
        assert "still active" in refusals and "running" in refusals
        assert int(pw.eval_js(_DIALOG_COUNT)) == 0
        pw.screenshot(SCREENSHOTS_DIR / "29-delete-disabled.png")

        # -- cancelling destroys nothing -------------------------------------
        pw.click('button.delete-run[data-delete-run="del-done"]')
        body = _wait_for(pw, "document.querySelector('#delete-dialog').textContent",
                         "del-done")
        assert "cannot be undone" in body, body
        assert int(pw.eval_js(_DIALOG_COUNT)) == 1
        pw.screenshot(SCREENSHOTS_DIR / "30-delete-confirm.png")
        pw.click("#delete-cancel")
        deadline = time.time() + 10
        while time.time() < deadline and pw.eval_js(_DIALOG_COUNT) != "0":
            time.sleep(0.2)
        assert int(pw.eval_js(_DIALOG_COUNT)) == 0
        assert (registry / "runs" / "del-done").is_dir()
        assert "del-done" in _row_run_ids(pw)

        # -- confirming really deletes it, and does not stack a dialog -------
        pw.click('button.delete-run[data-delete-run="del-done"]')
        _wait_for(pw, "document.querySelector('#delete-dialog').textContent",
                  "del-done")
        time.sleep(5.0)                       # a full 4s poll rebuilds the table
        assert int(pw.eval_js(_DELETE_BUTTONS)) == 3      # ...behind
        assert int(pw.eval_js(_DIALOG_COUNT)) == 1        # ...exactly ONE dialog
        pw.click("#delete-confirm")
        ids = _wait_until_gone(pw, "del-done")
        assert sorted(ids) == ["del-detail", "del-live"], ids
        assert not (registry / "runs" / "del-done").exists()
        assert (registry / "runs" / "del-detail").is_dir()
        assert (registry / "runs" / "del-live").is_dir()
        assert int(pw.eval_js(_DIALOG_COUNT)) == 0        # closed on success

        # -- run detail: the same control, same gate -------------------------
        pw.open(f"{server.base}/#/run/del-live")
        _wait_for(pw, "document.body.innerText", "running")
        _wait_for_count_ge(pw, "document.querySelectorAll('#delete-box button').length", 1)
        assert pw.eval_js(_delete_button("del-live") + ".disabled") == "true"
        detail_text = pw.eval_js("document.body.innerText")
        assert ui_server.DELETE_REFUSED_ACTIVE.format(state="running") in detail_text

        pw.open(f"{server.base}/#/run/del-detail")
        _wait_for(pw, "document.body.innerText", "del-detail")
        _wait_for_count_ge(pw, _DELETE_BUTTONS, 1)
        assert pw.eval_js(_delete_button("del-detail") + ".disabled") == "false"
        pw.click('button.delete-run[data-delete-run="del-detail"]')
        _wait_for(pw, "document.querySelector('#delete-dialog').textContent",
                  "del-detail")
        pw.click("#delete-confirm")
        # nothing left to show: the hub goes back to the list
        _wait_for(pw, "location.hash", "#/")
        ids = _wait_until_gone(pw, "del-detail")
        assert ids == ["del-live"], ids
        assert not (registry / "runs" / "del-detail").exists()
    finally:
        server.stop()
        engine.close()


def test_task_dialog_shows_more_than_the_current_status_task039(tmp_path, pw):
    """Task 039 (PRD I.1): a task detail dialog shows the STATES a task
    passed through, with timestamps and (where recorded) a cause -- not
    only its current status.

    `engine.state.task_transition_history` reads this fact straight off
    the run's own `events.jsonl` "task" events (`LoopSupervisor.
    _emit_task_status`/`_emit_task_changes`, the only writers of that
    event type) -- named here and in the commit message, per this task's
    own success criteria.

    Task `030` bounces pending -> in-progress -> pending (a worker
    deferral, carrying `deferredReason`) -> in-progress -> completed, four
    transitions recorded on `events.jsonl`, while `tasks.json` itself only
    ever shows the LAST of those (`completed`) -- so the dialog listing
    more than one state proves it is reading the transition log, not
    re-deriving anything from the task's current status alone.
    """
    from ralphd.cli.ui_server import run_detail

    registry = tmp_path / "registry"
    run_dir = _write_dead_run(registry, "run-task-transitions",
                              state="succeeded", verdict="verified")
    (run_dir / "tasks.json").write_text(json.dumps({
        "version": 1, "goal": "ship it",
        "tasks": [{"id": "030", "title": "a task that bounced states",
                   "status": "completed", "validationAttempts": 1}],
    }))
    events = [
        {"id": 1, "ts": "2026-09-05T09:00:00Z", "type": "task",
         "taskId": "030", "oldStatus": None, "newStatus": "pending"},
        {"id": 2, "ts": "2026-09-05T09:05:00Z", "type": "task",
         "taskId": "030", "oldStatus": "pending", "newStatus": "in-progress"},
        {"id": 3, "ts": "2026-09-05T09:10:00Z", "type": "task",
         "taskId": "030", "oldStatus": "in-progress", "newStatus": "pending",
         "deferredUntil": "2026-09-06T00:00:00Z",
         "deferredReason": "waiting on an upstream migration"},
        {"id": 4, "ts": "2026-09-06T01:00:00Z", "type": "task",
         "taskId": "030", "oldStatus": "pending", "newStatus": "in-progress"},
        {"id": 5, "ts": "2026-09-06T01:20:00Z", "type": "task",
         "taskId": "030", "oldStatus": "in-progress", "newStatus": "completed"},
    ]
    (run_dir / "events.jsonl").write_text(
        "".join(json.dumps(ev) + "\n" for ev in events))

    # -- the server's own payload: more than the current status -------------
    detail = run_detail(registry, "run-task-transitions")
    task_row = next(t for t in detail["tasks"]["tasks"] if t["id"] == "030")
    history = task_row["transitionHistory"]
    assert [h["newStatus"] for h in history] == [
        "pending", "in-progress", "pending", "in-progress", "completed"]
    assert history[2]["cause"] and "deferred" in history[2]["cause"], history

    server = UiServer(registry)
    server.wait_ready()
    try:
        pw.open(f"{server.base}/#/run/run-task-transitions")
        _wait_for_count_ge(pw, "document.querySelectorAll('#task-box tbody tr').length", 1)

        pw.click('tr.task-row[data-task-id="030"]')
        body = _wait_for(pw, "document.querySelector('.text-dialog').textContent",
                         "state history")
        # -- more than just "status: completed": every intermediate state
        # the task actually passed through is visible, with the recorded
        # deferral cause ------------------------------------------------
        assert "status: completed" in body, body
        assert body.count("in-progress") >= 2, body
        assert "waiting on an upstream migration" in body, body
        assert "2026-09-05" in body or "09/05" in body or "2026" in body, body
        pw.screenshot(SCREENSHOTS_DIR / "29-task-dialog-state-history.png")
    finally:
        server.stop()


def test_iteration_history_and_provisioning_collapse_by_default_task040(tmp_path, pw):
    """Task 040 (PRD I.2): the iteration history renders collapsed by
    default once it holds 15 or more records, and stays expanded below
    that count; the run-detail "provisioning (resolved hat -> model)"
    block (this PRD's "model config header") renders collapsed by default
    regardless of iteration count.

    `renderTimeline` (`app.js`) wraps the timeline rows in a native
    `<details class="timeline-details">`, `open` set only when the
    iteration count is under `TIMELINE_COLLAPSE_THRESHOLD` (15); the
    provisioning block (`renderSummary`) no longer carries `open` at all.
    Two fixtures -- 14 iterations and 15 -- so the threshold's own boundary
    is exercised, not just "some" vs "many".
    """
    from ralphd.engine.state import JOB_CONFIG_FILE

    registry = tmp_path / "registry"

    def _make_run(run_id: str, n_iterations: int):
        run_dir = _write_dead_run(registry, run_id, state="succeeded",
                                   verdict="verified")
        for n in range(1, n_iterations + 1):
            d = run_dir / "iterations" / f"{n:04d}"
            d.mkdir(parents=True)
            (d / "meta.json").write_text(json.dumps(
                {"number": n, "phase": "worker", "model": "stub-model",
                 "approach": 1, "startedAt": f"2026-01-01T00:{n:02d}:00Z",
                 "exitCode": 0, "error": None,
                 "endedAt": f"2026-01-01T00:{n:02d}:30Z",
                 "usage": {"totalTokens": 10}}))
        cdir = registry / "configs" / run_id
        cdir.mkdir(parents=True)
        # A minimal job.yaml is enough: `provisioning_table()` resolves
        # every `MODEL_HATS` entry to a default even with no `models:`
        # knob configured, so `provisioningLines` is never empty here.
        (cdir / JOB_CONFIG_FILE).write_text(f'run_id: "{run_id}"\niterations: 25\n')

    _make_run("run-few-iters-040", 14)
    _make_run("run-many-iters-040", 15)

    server = UiServer(registry)
    server.wait_ready()
    try:
        # -- below the threshold: expanded by default -----------------------
        pw.open(f"{server.base}/#/run/run-few-iters-040")
        _wait_for_count_ge(pw, "document.querySelectorAll('.timeline-item').length", 14)
        assert int(pw.eval_js("document.querySelectorAll('.timeline-item').length")) == 14
        assert pw.eval_js("document.querySelector('.timeline-details').open") == "true"
        summary = pw.eval_js("document.querySelector('.timeline-details summary').textContent")
        assert "14" in summary, summary
        # the model config header (provisioning block) is collapsed
        # regardless of the iteration count on this same page
        assert pw.eval_js("document.querySelector('.provisioning').open") == "false"
        pw.screenshot(SCREENSHOTS_DIR / "30-iteration-history-expanded-task040.png")

        # -- at the threshold: collapsed by default --------------------------
        pw.open(f"{server.base}/#/run/run-many-iters-040")
        _wait_for_count_ge(pw, "document.querySelectorAll('.timeline-item').length", 15)
        assert int(pw.eval_js("document.querySelectorAll('.timeline-item').length")) == 15
        assert pw.eval_js("document.querySelector('.timeline-details').open") == "false"
        summary2 = pw.eval_js("document.querySelector('.timeline-details summary').textContent")
        assert "15" in summary2, summary2
        assert pw.eval_js("document.querySelector('.provisioning').open") == "false"
        pw.screenshot(SCREENSHOTS_DIR / "31-iteration-history-collapsed-task040.png")
    finally:
        server.stop()
