"""v0.30 F.2: the hub shows a rate-limit wait and can wake it.

A run whose status.json carries `rateLimitWait` shows the line `waiting: rate
limit (<type>) until <local time>` (the same words `ralphctl status` prints,
formatted server-side) with the countdown and the "retry now" button, which
POSTs the run's own `/retry`. A run with no such wait shows neither.

Browser tier, like `test_browser_hub_backoff.py`: shells out to
`playwright-cli` and skips cleanly without it.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
from test_browser_hub import (  # noqa: F401
    ARTIFACTS_DIR,
    _skip_reason,
    _wait_for,
    pw,
)
from test_cli_ui import StubEngineApi, UiServer, _write_run_with_api

from ralphd.engine.state import format_local_time

SCREENSHOTS_DIR = ARTIFACTS_DIR / "screenshots" / "hub"

pytestmark = [pytest.mark.browser,
              pytest.mark.skipif(_skip_reason() is not None, reason=_skip_reason() or "")]


def test_run_detail_shows_the_rate_limit_wait_and_its_retry_button(tmp_path, pw):
    registry = tmp_path / "registry"
    resets_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + 1800))
    waiting_status = {
        "runId": "run-rl-live", "state": "running", "verdict": None,
        "phase": "worker", "approach": 1, "iterationsUsed": 2,
        "iterationsBudget": 25, "startedAt": "2026-01-01T00:00:00Z",
        "health": "ok", "infraWait": None,
        "rateLimitWait": {"type": "five_hour", "resetsAt": resets_at,
                          "since": "2026-01-01T00:10:00Z"},
    }
    plain_status = {**waiting_status, "runId": "run-plain-live", "rateLimitWait": None}
    waiting_engine = StubEngineApi(status=waiting_status)
    plain_engine = StubEngineApi(status=plain_status)
    _write_run_with_api(registry, "run-rl-live", waiting_engine,
                        token="hub-token", **waiting_status)
    _write_run_with_api(registry, "run-plain-live", plain_engine, **plain_status)

    server = UiServer(registry)
    server.wait_ready()
    try:
        pw.open(f"{server.base}/#/run/run-rl-live")
        body = _wait_for(pw, "document.body.innerText", "waiting: rate limit")
        assert (f"waiting: rate limit (five_hour) until "
                f"{format_local_time(resets_at)}") in body, body
        assert int(pw.eval_js("document.querySelectorAll('.rate-limit-wait').length")) == 1
        # not an outage: no degraded treatment
        assert int(pw.eval_js("document.querySelectorAll('.card.degraded').length")) == 0
        countdown = pw.eval_js(
            "document.querySelector('.rate-limit-wait .infra-countdown').textContent")
        assert "next attempt in " in countdown, countdown
        pw.click("button.retry-now")
        deadline = time.time() + 15
        posts = []
        while time.time() < deadline:
            posts = [(m, p) for m, p, _t in waiting_engine.requests if m == "POST"]
            if posts:
                break
            time.sleep(0.3)
        assert posts == [("POST", "/retry")], waiting_engine.requests
        _wait_for(pw, "document.querySelector('.retry-now-status').textContent",
                  "rate-limit wait woken")
        pw.screenshot(SCREENSHOTS_DIR / "v030-rate-limit-wait.png")

        pw.open(f"{server.base}/#/run/run-plain-live")
        body = _wait_for(pw, "document.body.innerText", "running")
        assert "rate limit" not in body, body
        assert int(pw.eval_js("document.querySelectorAll('button.retry-now').length")) == 0
    finally:
        server.stop()
        waiting_engine.close()
        plain_engine.close()
