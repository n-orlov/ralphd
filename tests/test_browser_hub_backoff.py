"""Task 005 (A.3): browser-tier proof of the two client-side behaviours the
in-process test in `tests/test_hub_client_backoff.py` cannot reach -- neither
is observable from a plain HTTP client, only from `app.js`'s own polling
loop running inside a real page:

1. **Back-off.** `renderRunDetail`'s poll widens from the normal 4s cadence
   to `BACKOFF_POLL_MS` (20000ms, inside the PRD's 15-30s band) once either
   back-off condition holds -- two identical (`304`) payloads in a row, or
   the run is no longer `running` -- and instrumentation
   (`window.__hubDetailPollIntervalMs()`, never read by the app itself)
   exposes the current target without a test having to wait out the full
   band to observe it.
2. **Hidden pause.** `document.hidden` stops the poll entirely -- proven by
   faking the Page Visibility API the way `visibilitychange` listeners
   actually observe it (`Object.defineProperty` on `document.hidden`'s
   getter, then dispatching the real event), the only way to drive this
   deterministically without a second real OS-level window/tab.

Marked `@pytest.mark.browser` for the same reason as `test_browser_hub.py`
(shells out to `playwright-cli`, skips cleanly if it is absent).
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
from test_browser_hub import ARTIFACTS_DIR, PLAYWRIGHT_CLI, Pw, _skip_reason, _wait_for, pw  # noqa: F401
from test_cli_ui import UiServer, _write_dead_run

SCREENSHOTS_DIR = ARTIFACTS_DIR / "screenshots" / "hub"

pytestmark = [pytest.mark.browser, pytest.mark.skipif(_skip_reason() is not None, reason=_skip_reason() or "")]

_POLL_MS = "Number(window.__hubDetailPollIntervalMs())"


def _wait_for_poll_ms(session: Pw, expected: int, timeout: float) -> int:
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        raw = session.eval_js(_POLL_MS)
        try:
            last = int(float(raw))
        except ValueError:
            last = None
        if last == expected:
            return last
        time.sleep(0.3)
    raise AssertionError(f"timed out waiting for poll interval == {expected}; last={last!r}")


def test_run_detail_backs_off_immediately_for_a_non_running_run(tmp_path, pw):
    """A run whose recorded `state` is not `running` backs off on the very
    first poll -- there is nothing to wait for two identical payloads to
    prove, the state itself already says the run is quiescent."""
    registry = tmp_path / "registry"
    _write_dead_run(registry, "run-done-backoff", state="succeeded", verdict="verified")

    server = UiServer(registry)
    server.wait_ready()
    try:
        pw.open(f"{server.base}/#/run/run-done-backoff")
        _wait_for(pw, "document.body.innerText", "succeeded")
        _wait_for_poll_ms(pw, 20000, timeout=10)
        pw.screenshot(SCREENSHOTS_DIR / "task005-backoff-not-running.png")
    finally:
        server.stop()


def test_run_detail_backs_off_after_two_identical_polls_of_a_running_run(tmp_path, pw):
    """Isolates the OTHER back-off trigger: a run recorded `running` whose
    on-disk snapshot never changes (no live container, no engine writing to
    it) gets a `304` on every poll after its first -- so the interval must
    still start at the normal 4s cadence (state alone does not trigger
    back-off here) and only widen once two of those `304`s have landed."""
    registry = tmp_path / "registry"
    _write_dead_run(registry, "run-static-running", state="running", verdict=None)

    server = UiServer(registry)
    server.wait_ready()
    try:
        pw.open(f"{server.base}/#/run/run-static-running")
        _wait_for(pw, "document.body.innerText", "running")

        # Sampled immediately: the "not running" trigger does not fire (the
        # state IS running), so the very first tick must still be the fast
        # cadence.
        first = int(float(pw.eval_js(_POLL_MS)))
        assert first == 4000, f"expected the fast 4s cadence before any back-off, got {first}"

        # Two more ticks land as 304s (nothing on disk moved) before the
        # loop widens -- comfortably inside a generous margin past 2*4s.
        _wait_for_poll_ms(pw, 20000, timeout=15)
        pw.screenshot(SCREENSHOTS_DIR / "task005-backoff-identical-payloads.png")
    finally:
        server.stop()


def test_run_detail_stops_polling_while_the_document_is_hidden(tmp_path, pw):
    """Fakes the Page Visibility API the same way a real backgrounded tab
    would notify a `visibilitychange` listener: overriding the `hidden`
    getter (the property `app.js` actually reads) and dispatching the real
    event, since there is no second OS-level window to background for
    real inside a headless CDP session. `__hubRefreshLoopCount()` (task 016)
    is the existing instrumentation for "how many live poll timers exist
    right now" -- 1 while visible, 0 the moment the tab is hidden, back to
    1 once it is shown again."""
    registry = tmp_path / "registry"
    _write_dead_run(registry, "run-hide-pause", state="running", verdict=None)

    server = UiServer(registry)
    server.wait_ready()
    try:
        pw.open(f"{server.base}/#/run/run-hide-pause")
        _wait_for(pw, "document.body.innerText", "running")
        assert int(pw.eval_js("window.__hubRefreshLoopCount()")) == 1

        pw.eval_js(
            "(function(){"
            "  Object.defineProperty(document, 'hidden', {"
            "    configurable: true, get: () => true });"
            "  document.dispatchEvent(new Event('visibilitychange'));"
            "  return 'hidden';"
            "})()"
        )
        assert int(pw.eval_js("window.__hubRefreshLoopCount()")) == 0, (
            "the poll kept running while document.hidden was true")
        # ...and it stays at 0 -- not merely momentarily dipping between two
        # back-to-back ticks.
        time.sleep(1.5)
        assert int(pw.eval_js("window.__hubRefreshLoopCount()")) == 0

        pw.eval_js(
            "(function(){"
            "  Object.defineProperty(document, 'hidden', {"
            "    configurable: true, get: () => false });"
            "  document.dispatchEvent(new Event('visibilitychange'));"
            "  return 'shown';"
            "})()"
        )
        assert int(pw.eval_js("window.__hubRefreshLoopCount()")) == 1, (
            "the poll never resumed after the tab became visible again")
        pw.screenshot(SCREENSHOTS_DIR / "task005-hidden-pause.png")
    finally:
        server.stop()
