"""#293 (PRD D): browser-tier proof of the iteration's "router call" link.

A finished run whose iteration 2 was routed shows a "router call" link on
that timeline row only; clicking it opens the collapsed router panel,
highlights the entry `router-call-1` and does NOT open the row's own
iteration dialog. The same link sits in the iteration dialog. A run whose
events lack `iteration` shows no link anywhere, and the run-wide router panel
still lists its call.

Marked `@pytest.mark.browser` for the same reason as `test_browser_hub.py`.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
from test_browser_hub import _skip_reason, _wait_for, pw  # noqa: F401
from test_cli_ui import UiServer, _write_dead_run
from test_hub_iteration_dialog import _meta, _write_iteration
from test_router_task_selection import _ts_event

pytestmark = [pytest.mark.browser, pytest.mark.skipif(_skip_reason() is not None, reason=_skip_reason() or "")]


def _js(pw, expr: str) -> str:
    return pw.eval_js(expr).strip('"')


def _run(tmp_path: Path, run_id: str, **event_extra) -> Path:
    registry = tmp_path / "registry"
    run_dir = _write_dead_run(registry, run_id, state="failed", verdict="unverified")
    for n in (1, 2, 3):
        _write_iteration(run_dir, n, _meta(n=n), texts=[f"iteration {n}"])
    with open(run_dir / "events.jsonl", "a") as f:
        f.write(json.dumps({"ts": "2026-09-03T10:00:00Z", "type": "router.decision",
                            **_ts_event("002", **event_extra)}) + "\n")
    return registry


def test_routed_iteration_links_to_its_router_call(tmp_path, pw):
    registry = _run(tmp_path, "run-rl", iteration=2)
    server = UiServer(registry)
    server.wait_ready()
    try:
        pw.open(f"{server.base}/#/run/run-rl")
        _wait_for(pw, "document.getElementById('router-calls-box').innerText", "router calls (1)")
        # the panel entry has the element id the link targets
        assert _js(pw, "String(!!document.getElementById('router-call-1'))") == "true"
        # one link, on iteration 2's row only
        assert _js(pw, 
            "[...document.querySelectorAll('#timeline-box .router-call-link')]"
            ".map((b) => b.closest('.timeline-item').dataset.iteration).join(',')") == "2"
        assert _js(pw, "String(document.querySelector('.router-call-list-details').open)") == "false"

        pw.click("#timeline-box .router-call-link")

        assert _js(pw, "String(document.querySelector('.router-call-list-details').open)") == "true"
        assert _js(pw, 
            "document.getElementById('router-call-1').classList.contains('router-call-highlight')"
            ".toString()") == "true"
        assert _js(pw, "String(document.querySelectorAll('dialog[open]').length)") == "0"

        # the dialog of the routed iteration carries the link too; the others do not
        pw.eval_js("document.querySelector('.timeline-item[data-iteration=\"1\"]').click(), 1")
        _wait_for(pw, "document.querySelector('.iteration-dialog') ? 'open' : ''", "open")
        assert _js(pw, "String(document.querySelectorAll('.iteration-dialog .router-call-link').length)") == "0"
        pw.eval_js("document.querySelector('.timeline-item[data-iteration=\"2\"]').click(), 1")
        _wait_for(pw, "String(document.querySelectorAll('.iteration-dialog .router-call-link').length)", "1")
    finally:
        server.stop()


def test_events_without_the_iteration_field_link_nothing(tmp_path, pw):
    registry = _run(tmp_path, "run-rl-none")
    server = UiServer(registry)
    server.wait_ready()
    try:
        pw.open(f"{server.base}/#/run/run-rl-none")
        _wait_for(pw, "document.getElementById('router-calls-box').innerText", "router calls (1)")
        _wait_for(pw, "String(document.querySelectorAll('#timeline-box .timeline-item').length)", "3")
        assert _js(pw, "String(document.querySelectorAll('.router-call-link').length)") == "0"
        assert _js(pw, "String(!!document.getElementById('router-call-1'))") == "true"
    finally:
        server.stop()
