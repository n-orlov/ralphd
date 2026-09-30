"""Task 001 (#277): browser-tier proof of the hub's "apply now" checkbox --
ticking it, submitting the steer form, and asserting (a) the request the
page itself sent to the hub carries `now: true` (recorded off the page's
own `window.fetch`) and the hub forwarded it to `/interrupt`, and (b)
the page renders the SAME wording `ralphctl steer --now`/`ralphctl
interrupt` print (`_interrupt_report`, cli/main.py; `web/interrupt-
wording.json`, the one place either phrase is spelled out) rather than the
ordinary "sent (...)" line the unchecked form shows.

Marked `@pytest.mark.browser` for the same reason as `test_browser_hub.py`
(shells out to `playwright-cli`, skips cleanly if it is absent).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
from test_browser_hub import (  # noqa: F401
    ARTIFACTS_DIR,
    PLAYWRIGHT_CLI,
    Pw,
    _skip_reason,
    _wait_for,
    pw,
)
from test_cli_ui import StubEngineApi, UiServer, _write_run_with_api

SCREENSHOTS_DIR = ARTIFACTS_DIR / "screenshots" / "hub"

pytestmark = [pytest.mark.browser, pytest.mark.skipif(_skip_reason() is not None, reason=_skip_reason() or "")]


def _last_post(engine):
    """The LAST recorded POST's (path, parsed-json-body) off a
    `StubEngineApi`, or (None, None) if it never saw one."""
    last_path, last_body = None, None
    for (method, path, _auth), body in zip(engine.requests, engine.bodies):
        if method == "POST":
            last_path = path
            last_body = json.loads(body) if body else {}
    return last_path, last_body


def _record_steer_request_bodies(pw):
    """Wrap the page's `window.fetch` so every body it POSTs to the hub's
    `/steer` route is recorded in `window.__steerBodies` -- the browser-to-
    hub request itself, before the hub has routed or reshaped anything."""
    pw.eval_js(
        "(function(){"
        "  window.__steerBodies = [];"
        "  const orig = window.fetch;"
        "  window.fetch = function(url, opts) {"
        "    if (String(url).includes('/steer') && opts && opts.method === 'POST') {"
        "      window.__steerBodies.push(String(opts.body));"
        "    }"
        "    return orig.apply(this, arguments);"
        "  };"
        "  return 'wrapped';"
        "})()")


def _steer_request_bodies(pw):
    """The recorded browser-to-hub `/steer` bodies, parsed in the page and
    returned as a JSON array (`--raw eval` prints an object/array result
    as JSON)."""
    raw = pw.eval_js("(window.__steerBodies || []).map((b) => JSON.parse(b))")
    return json.loads(raw)


def test_apply_now_checkbox_sends_now_true_and_shows_the_shared_wording(tmp_path, pw):
    engine = StubEngineApi(
        status={"runId": "run-apply-now", "state": "running"},
        interrupt_status=200,
        interrupt_body={"interrupted": True, "target": "iteration",
                        "signalled": True, "woken": False})
    registry = tmp_path / "registry"
    try:
        _write_run_with_api(registry, "run-apply-now", engine, state="running")
        server = UiServer(registry)
        server.wait_ready()
        try:
            pw.open(f"{server.base}/#/run/run-apply-now")
            _wait_for(pw, "document.body.innerText", "Steer")

            _record_steer_request_bodies(pw)
            pw.fill("#steer-message", "steer now via the checkbox")
            pw.click("#steer-now")
            pw.click("#steer-form button[type=submit]")

            steer_text = _wait_for(
                pw, "document.getElementById('steer-status').textContent",
                "a running iteration was signalled")
            assert "a running iteration was signalled" in steer_text, steer_text

            pw.screenshot(SCREENSHOTS_DIR / "apply-now-interrupt-wording.png")

            # The request the PAGE itself sent to the hub: the checkbox
            # must put `now: true` in the browser-to-hub body.
            page_bodies = _steer_request_bodies(pw)
            assert len(page_bodies) == 1, page_bodies
            assert page_bodies[0].get("now") is True, page_bodies
            assert page_bodies[0].get("message") == "steer now via the checkbox", page_bodies

            # And what the hub forwarded on to the engine: `/interrupt`,
            # never `/steering`, with `now` stripped (a hub-side routing
            # decision, not part of `/interrupt`'s own body contract).
            last_path, last_body = _last_post(engine)
            assert last_path == "/interrupt", engine.requests
            assert last_body.get("now") is None, (
                "the `now` flag is a hub-side routing decision, not part of "
                f"the forwarded /interrupt body: {last_body}")
            assert last_body.get("message") == "steer now via the checkbox", last_body
        finally:
            server.stop()
    finally:
        engine.close()


def test_apply_now_left_unchecked_still_shows_the_ordinary_sent_line(tmp_path, pw):
    """The negative half, at the browser layer: leaving the checkbox
    unticked must NOT show the interrupt wording, and must reach
    `/steering` (the stub's default 202 `{"file": ...}` body renders "sent
    (001-x.md)")."""
    engine = StubEngineApi(status={"runId": "run-no-now", "state": "running"})
    registry = tmp_path / "registry"
    try:
        _write_run_with_api(registry, "run-no-now", engine, state="running")
        server = UiServer(registry)
        server.wait_ready()
        try:
            pw.open(f"{server.base}/#/run/run-no-now")
            _wait_for(pw, "document.body.innerText", "Steer")

            _record_steer_request_bodies(pw)
            pw.fill("#steer-message", "an ordinary queued steer")
            pw.click("#steer-form button[type=submit]")

            steer_text = _wait_for(
                pw, "document.getElementById('steer-status').textContent", "sent")
            assert "a running iteration was signalled" not in steer_text, steer_text
            assert "an idle loop was woken" not in steer_text, steer_text

            page_bodies = _steer_request_bodies(pw)
            assert len(page_bodies) == 1, page_bodies
            assert "now" not in page_bodies[0], page_bodies

            last_path, _last_body = _last_post(engine)
            assert last_path == "/steering", engine.requests
        finally:
            server.stop()
    finally:
        engine.close()
