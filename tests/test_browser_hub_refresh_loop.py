"""Task 016 (#27): browser-tier proof that `cli/web/app.js`'s router leaves
exactly one live refresh loop after two navigations, and never applies a
response from a render `router()` has already superseded.

Both defects only reproduce under a real race between a pending `fetch` and
a navigation that happens before it resolves -- so each test here installs a
one-shot delay on a specific `fetch` call (via `window.fetch` override,
evaluated in the real page) to force the race deterministically, rather than
hoping a fast local server happens to be slow enough on its own.

Marked `@pytest.mark.browser` for the same reason as `test_browser_hub.py`
(shells out to `playwright-cli`, skips cleanly if it is absent). Task 017
(#27) adds further coverage driven by actual click-based navigation; these
two tests exist to pin the exact mechanism task 016 introduced
(`renderGeneration` + `startRefreshLoop`/`stopRefreshLoop` + the `loadSeq`
tag) and are written to fail if either half of the fix is reverted -- see
each test's docstring for which half it targets.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
from test_browser_hub import ARTIFACTS_DIR, PLAYWRIGHT_CLI, Pw, _skip_reason, _wait_for, pw  # noqa: F401
from test_cli_ui import UiServer, _write_dead_run

SCREENSHOTS_DIR = ARTIFACTS_DIR / "screenshots" / "hub"

pytestmark = [pytest.mark.browser, pytest.mark.skipif(_skip_reason() is not None, reason=_skip_reason() or "")]


def test_two_fast_navigations_leave_exactly_one_refresh_loop(tmp_path, pw):
    """Targets the fix in `router()`/`renderRunList` that only installs the
    refresh timer for the render that is still current when its initial
    `load()` resolves (`if (myGen === renderGeneration) refreshTimer =
    startRefreshLoop(load);`). Before task 016, `router()` cleared
    `refreshTimer` unconditionally on every navigation but a render only
    *assigned* `refreshTimer` after its own `await load()` -- so a second
    navigation landing before the first's initial load resolved cleared a
    variable that was still `null`, and the first render's later,
    now-superseded `await load()` went on to unconditionally overwrite
    `refreshTimer` with a brand new interval, orphaning whatever the second
    render had already installed. Reverting just that one `if` guard back to
    an unconditional assignment makes this test observe `2` live loops
    instead of `1` (the orphaned one plus the new one), while every other
    part of the task 016 diff stays in place.
    """
    registry = tmp_path / "registry"
    _write_dead_run(registry, "run-only", state="succeeded", verdict="verified")

    server = UiServer(registry)
    server.wait_ready()
    try:
        pw.open(server.base)
        _wait_for(pw, "document.body.innerText", "run-only")

        # The initial page load already installed its own refresh loop with
        # the real `REFRESH_MS` (4s) delay. Each `eval_js` round-trip to
        # `playwright-cli` costs on the order of a couple of real seconds
        # (subprocess + CDP overhead), so the handful of setup calls below
        # can by themselves eat past that 4s window -- letting the *original*
        # tick consume the one-shot delay this test installs next, well
        # before the deliberate `router(); router();` race even runs.
        # Cancel that already-scheduled tick right away (via the app's own
        # `stopRefreshLoop`, reachable from `eval` because it is a top-level
        # function declaration) so the rest of this test's real-time budget
        # is spent on the race it means to create, not raced by REFRESH_MS.
        pw.eval_js("(function(){ stopRefreshLoop(refreshTimer); refreshTimer = null; return 'ok'; })()")

        # One-shot delay on the *first* call to the list endpoint made after
        # this point -- gives a second, fast navigation a deterministic
        # window to land inside the first navigation's still-pending
        # `await load()`.
        pw.eval_js(
            "(function(){"
            "  const orig = window.fetch;"
            "  let delayed = false;"
            "  window.fetch = function(path, opts){"
            "    if (!delayed && path === '/api/runs') {"
            "      delayed = true;"
            "      return new Promise(res => setTimeout(() => res(orig(path, opts)), 1500));"
            "    }"
            "    return orig(path, opts);"
            "  };"
            "  return 'installed';"
            "})()"
        )

        # Two navigations back-to-back, calling `router()` directly (a global
        # function declaration, so reachable from `eval`) rather than via a
        # hashchange, since the hash does not need to change for the race to
        # matter here -- only that two renders of the same view are started
        # while the first's `load()` is still in flight.
        pw.eval_js("(function(){ router(); router(); return 'done'; })()")

        # The second navigation's own (fast, undelayed) load has settled.
        time.sleep(0.5)
        n_mid = int(pw.eval_js("window.__hubRefreshLoopCount()"))

        # Wait past the first navigation's artificial delay so its
        # now-superseded `load()` gets a chance to (mis)behave.
        time.sleep(1.5)
        n_after = int(pw.eval_js("window.__hubRefreshLoopCount()"))

        assert n_mid == 1, f"expected exactly one live loop before the delayed response lands, got {n_mid}"
        assert n_after == 1, (
            f"expected exactly one live loop after the superseded render's delayed "
            f"response resolved, got {n_after} (an orphaned timer would show as 2)"
        )
        pw.screenshot(SCREENSHOTS_DIR / "task016-single-refresh-loop.png")
    finally:
        server.stop()


def test_a_superseded_detail_renders_response_is_discarded_not_applied(tmp_path, pw):
    """Targets the `myGen !== renderGeneration` / `seq !== loadSeq` guards
    inside `renderRunDetail`'s `load()`. Several of the sub-renders that
    `load()` drives (`renderTasks(document.getElementById("task-box"), ...)`
    among them) look their target element up fresh by `id` rather than via
    a closure -- and every `renderRunDetail` call reuses the same ids. That
    means a *stale* response landing after a later navigation has already
    replaced the DOM does not write into a harmlessly-detached node; it
    writes into the *live* page's element with the same id, because
    `getElementById` finds whatever currently has that id. Only the
    generation/sequence guards at the top of `load()` stop that stale write
    from ever running. Reverting either guard (or removing `loadSeq`
    entirely) makes the run-a marker task reappear in run-b's live task
    list once the delayed response for run-a lands.
    """
    registry = tmp_path / "registry"
    dir_a = _write_dead_run(registry, "run-a", state="aborted", verdict="unverified")
    dir_b = _write_dead_run(registry, "run-b", state="succeeded", verdict="verified")
    (dir_a / "tasks.json").write_text(json.dumps({"tasks": [
        {"id": "1", "title": "STALE-TASK-A-MARKER", "status": "completed"},
    ]}))
    (dir_b / "tasks.json").write_text(json.dumps({"tasks": [
        {"id": "1", "title": "run-b's own task", "status": "completed"},
    ]}))

    server = UiServer(registry)
    server.wait_ready()
    try:
        pw.open(server.base)
        _wait_for(pw, "document.body.innerText", "run-a")

        # Delay exactly the one HTTP call run-a's detail render makes for its
        # own status ("/api/runs/run-a") -- everything else (run-b's own
        # fetch, the list poll) stays untouched.
        pw.eval_js(
            "(function(){"
            "  const orig = window.fetch;"
            "  let delayed = false;"
            "  window.fetch = function(path, opts){"
            "    if (!delayed && path === '/api/runs/run-a') {"
            "      delayed = true;"
            "      return new Promise(res => setTimeout(() => res(orig(path, opts)), 1500));"
            "    }"
            "    return orig(path, opts);"
            "  };"
            "  return 'installed';"
            "})()"
        )

        # Two navigations to two *different* runIds, back to back with no
        # yield to the event loop in between -- deliberately not two
        # `location.hash` assignments in sequence, because `router()` (like
        # any hashchange listener) reads `location.hash` fresh at the time it
        # actually runs rather than from the event that scheduled it: two
        # hash assignments made synchronously, before either of the two
        # resulting hashchange tasks gets a chance to run, both end up
        # dispatching to the *same*, final hash. Calling the router's own
        # generation-minting sequence directly (mirroring exactly what
        # `router()` does internally) is the only way to give the two
        # navigations distinct runIds while keeping them synchronous.
        pw.eval_js(
            "(function(){"
            "  renderGeneration += 1; const gA = renderGeneration;"
            "  stopRefreshLoop(refreshTimer); refreshTimer = null;"
            "  renderRunDetail('run-a', gA);"
            "  renderGeneration += 1; const gB = renderGeneration;"
            "  stopRefreshLoop(refreshTimer); refreshTimer = null;"
            "  renderRunDetail('run-b', gB);"
            "  return 'ok';"
            "})()"
        )

        # run-b's own (fast, undelayed) fetch has settled well within
        # run-a's artificial 1.5s delay.
        body_text = _wait_for(pw, "document.body.innerText", "run-b's own task")
        assert "STALE-TASK-A-MARKER" not in body_text, body_text

        # Only run-b's render ever got to install a refresh loop: run-a's
        # `load()` was still awaiting its delayed fetch when this is checked,
        # so it has not reached its own (generation-guarded) install line yet
        # either way -- this pins the same single-loop property the previous
        # test targets, using the second view (`renderRunDetail`) instead of
        # the first (`renderRunList`).
        n_loops = int(pw.eval_js("window.__hubRefreshLoopCount()"))
        assert n_loops == 1, f"expected exactly one live loop, got {n_loops}"

        # `renderRunDetail`'s own legitimate periodic refresh for run-b would,
        # once it has run at least once for real data, re-fetch and repaint
        # `task-box` with run-b's correct data on its own -- which would mask
        # a leak that happened and was later overwritten, rather than one
        # that never happened. Neutralize it once run-b's initial (real,
        # undelayed) load has settled, so the check below can only observe
        # what run-a's stale response actually did, not what a subsequent
        # legitimate poll cleaned up after it.
        pw.eval_js("(function(){ stopRefreshLoop(refreshTimer); refreshTimer = null; return 'ok'; })()")

        # Now let run-a's delayed, superseded response land.
        time.sleep(2.0)
        body_text = pw.eval_js("document.body.innerText")
        assert "run-b's own task" in body_text, body_text
        assert "STALE-TASK-A-MARKER" not in body_text, (
            "a stale response for a superseded render (run-a) leaked into "
            "the live view (run-b) via a shared element id: " + body_text
        )
        pw.screenshot(SCREENSHOTS_DIR / "task016-stale-response-discarded.png")
    finally:
        server.stop()
