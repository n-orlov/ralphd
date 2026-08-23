"""Task 017 (#27): browser-tier proof of the same two `cli/web/app.js`
router properties task 016 pinned (`renderGeneration`/`startRefreshLoop`/
`stopRefreshLoop` leaving exactly one live refresh loop, and the `loadSeq`
tag discarding a superseded render's response) -- but driven by *actual
click-based navigation* through the rendered `<a href="#/...">` links
(`pw.click(...)`), the way an operator's mouse actually exercises the hub,
rather than by calling `router()`/`renderRunDetail()` directly from `eval`
the way `tests/test_browser_hub_refresh_loop.py` does. That file's own
docstring says as much: "Task 017 (#27) adds further coverage driven by
actual click-based navigation".

Both tests here still need a deterministic race (an ordinary local
`playwright-cli` round-trip is on the order of one to two real seconds, well
within `REFRESH_MS`'s 4s window, so a *real* fetch usually completes long
before a second click could possibly land) -- so, exactly like task 016's
file, each test installs a one-shot delay on a specific `fetch` call before
issuing the clicks. What is different here is which surface is exercised:
here it is the actual `<a>` elements the list/detail views render
(`a[href="#/run/..."]` and `a[href="#/"]`), clicked via `pw.click(selector)`,
not `router(...)`/`renderRunDetail(...)` called from `eval`.

Marked `@pytest.mark.browser`, skips cleanly if `playwright-cli` is absent.
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


def _neutralize_running_loop(session: Pw):
    """Cancel whatever refresh loop the page most recently installed on its
    own, so the real-time cost of the clicks below (each `pw.click` is a
    subprocess+CDP round trip, like `eval_js`) does not let a genuine
    periodic tick consume a one-shot delayed-fetch flag before the
    deliberate race this test builds gets a chance to run -- see
    `tests/test_browser_hub_refresh_loop.py` for the same gotcha in the
    `eval_js`-driven version of these tests.
    """
    session.eval_js("(function(){ stopRefreshLoop(refreshTimer); refreshTimer = null; return 'ok'; })()")


def _delay_one_fetch(session: Pw, path: str, delay_ms: int):
    session.eval_js(
        "(function(){"
        "  const orig = window.fetch;"
        "  let delayed = false;"
        f"  const target = {json.dumps(path)};"
        f"  const ms = {delay_ms};"
        "  window.fetch = function(p, opts){"
        "    if (!delayed && p === target) {"
        "      delayed = true;"
        "      return new Promise(res => setTimeout(() => res(orig(p, opts)), ms));"
        "    }"
        "    return orig(p, opts);"
        "  };"
        "  return 'installed';"
        "})()"
    )


def test_clicking_from_the_list_into_a_run_and_back_leaves_one_refresh_loop(tmp_path, pw):
    """Click the run-list's own `<a href="#/run/...">` link into the detail
    view, then -- before its detail fetch resolves -- click the detail
    view's own `<a href="#/">` ("&larr; runs") link back to the list. This
    targets the exact guard task 016 added at the end of `renderRunDetail`
    (`if (myGen === renderGeneration) refreshTimer = startRefreshLoop(load);`):
    without it, the detail render's `load()` -- still in flight when the
    click back to the list fires -- unconditionally installs its own timer
    once its delayed fetch finally resolves, on top of the one the list view
    (correctly) installed for itself, orphaning it. Reverting just that one
    `if` guard (matching task 016's own revert-evidence pattern) makes this
    test observe `2` live loops instead of `1`.
    """
    registry = tmp_path / "registry"
    _write_dead_run(registry, "run-click", state="succeeded", verdict="verified")

    server = UiServer(registry)
    server.wait_ready()
    try:
        pw.open(server.base)
        _wait_for(pw, "document.body.innerText", "run-click")
        _neutralize_running_loop(pw)

        # Delay the detail view's own status fetch so the click back to the
        # list has a deterministic window to land before it resolves.
        _delay_one_fetch(pw, "/api/runs/run-click", 3000)

        pw.click('#app a[href="#/run/run-click"]')
        _wait_for(pw, "document.body.innerText", "Run run-click")
        # `load()` is still awaiting its delayed fetch here -- click back to
        # the list before it can resolve. `#app a[href="#/"]` (not the bare
        # attribute selector) because the static page header also has an
        # `<a href="#/">` ("ralphd hub"), outside `#app` -- `playwright-cli`
        # `click` errors on an ambiguous selector but, like `eval`, still
        # exits 0, so an unqualified selector here would silently no-op
        # rather than fail loudly.
        pw.click('#app a[href="#/"]')
        _wait_for(pw, "document.body.innerText", "run-click")

        # The list view's own (fast, undelayed) load has settled and
        # installed its own loop.
        n_mid = int(pw.eval_js("window.__hubRefreshLoopCount()"))

        # Wait past the detail view's artificial delay so its now-superseded
        # `load()` gets a chance to (mis)behave.
        time.sleep(3.5)
        n_after = int(pw.eval_js("window.__hubRefreshLoopCount()"))

        assert n_mid == 1, f"expected exactly one live loop right after navigating back, got {n_mid}"
        assert n_after == 1, (
            f"expected exactly one live loop after the superseded detail render's "
            f"delayed response resolved, got {n_after} (an orphaned timer would show as 2)"
        )
        pw.screenshot(SCREENSHOTS_DIR / "task017-click-nav-single-refresh-loop.png")
    finally:
        server.stop()


def test_clicking_list_detail_list_detail_via_real_links_discards_a_stale_response(tmp_path, pw):
    """Click list -> run-a's detail -> back to the list -> run-b's detail,
    entirely through the page's own rendered `<a>` links. Targets the
    `myGen !== renderGeneration || seq !== loadSeq` guard inside
    `renderRunDetail`'s `load()`: several of the sub-renders it drives
    (`renderTasks(document.getElementById("task-box"), ...)` among them)
    look their target element up fresh by id, and every `renderRunDetail`
    call reuses the same ids -- so a stale response for run-a landing after
    run-b's detail view has already replaced the DOM does not write into a
    detached node, it writes into *run-b's own live element*. Reverting
    either guard (or `loadSeq` itself) makes run-a's marker task reappear in
    run-b's live task list once run-a's delayed response lands, exactly as
    in `tests/test_browser_hub_refresh_loop.py`'s function-call-driven
    version of this same defect -- this test pins the identical mechanism
    reached by real navigation through the list in between, instead.
    """
    registry = tmp_path / "registry"
    dir_a = _write_dead_run(registry, "run-a", state="aborted", verdict="unverified")
    dir_b = _write_dead_run(registry, "run-b", state="succeeded", verdict="verified")
    (dir_a / "tasks.json").write_text(json.dumps({"tasks": [
        {"id": "1", "title": "STALE-TASK-A-CLICK-MARKER", "status": "completed"},
    ]}))
    (dir_b / "tasks.json").write_text(json.dumps({"tasks": [
        {"id": "1", "title": "run-b's own click-nav task", "status": "completed"},
    ]}))

    server = UiServer(registry)
    server.wait_ready()
    try:
        pw.open(server.base)
        _wait_for(pw, "document.body.innerText", "run-a")
        _neutralize_running_loop(pw)

        # Delay only run-a's own detail fetch. 4000ms, not 3000 --
        # `playwright-cli` round trips for the three clicks below already
        # cost a couple of real seconds; giving the delay a bit more margin
        # over that keeps `t_click_a` (below) a reliable anchor even when a
        # single click happens to be slow.
        delay_s = 4.0
        _delay_one_fetch(pw, "/api/runs/run-a", int(delay_s * 1000))

        t_click_a = time.time()
        pw.click('#app a[href="#/run/run-a"]')
        _wait_for(pw, "document.body.innerText", "Run run-a")
        # run-a's `load()` is still awaiting its delayed fetch -- navigate
        # away through the real links before it resolves: back to the list,
        # then into run-b's detail. `#app a[href="#/"]`, not the bare
        # attribute selector -- see the matching comment in the previous
        # test for why the unqualified selector silently no-ops here instead
        # of failing loudly (the static page header has its own `#/` link).
        pw.click('#app a[href="#/"]')
        _wait_for(pw, "document.body.innerText", "run-b")
        pw.click('#app a[href="#/run/run-b"]')
        body_text = _wait_for(pw, "document.body.innerText", "run-b's own click-nav task")
        assert "STALE-TASK-A-CLICK-MARKER" not in body_text, body_text

        # run-b's own periodic refresh, once it has run for real, would
        # re-fetch and repaint `task-box` with run-b's own correct data on
        # its own within a few seconds -- masking a leak that happened and
        # was later overwritten rather than one that never happened at all.
        # Neutralize it right away, immediately after run-b's own (fast,
        # undelayed) initial load has settled, so nothing else touches
        # `task-box` between now and the check below.
        _neutralize_running_loop(pw)

        # Land the check just after run-a's delayed response is due (relative
        # to the click that triggered it, not to now) -- with the guard in
        # place `load()` returns before touching the DOM at all, so the fixed
        # code passes this check regardless of exact timing; only the
        # reverted code's behaviour depends on catching it before some later
        # event could overwrite it, and nothing else is running to do that
        # now that both loops are neutralized.
        remaining = delay_s - (time.time() - t_click_a) + 0.5
        if remaining > 0:
            time.sleep(remaining)
        body_text = pw.eval_js("document.body.innerText")
        assert "run-b's own click-nav task" in body_text, body_text
        assert "STALE-TASK-A-CLICK-MARKER" not in body_text, (
            "a stale response for a superseded detail render (run-a) leaked into "
            "the live view (run-b) via a shared element id, reached this time through "
            "real list<->detail navigation rather than a direct render call: " + body_text
        )
        pw.screenshot(SCREENSHOTS_DIR / "task017-click-nav-stale-response-discarded.png")
    finally:
        server.stop()
