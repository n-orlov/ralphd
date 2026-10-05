"""#293 (PRD D): each routed iteration links to the router call that picked
its task.

The `task_selection` `router.decision` event carries the OPTIONAL `iteration`
-- the worker iteration whose task it selected -- `state.task_selection_call_
entry` exposes it, `ui_server.run_detail` sets `routerCallIndex` on that
iteration's row only (and `iteration_view` on the dialog payload), and
`web/app.js` renders a "router call" link that opens the router panel at the
entry's own element id `router-call-<index>`. A run whose events lack the
field links nothing.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

from ralphd.cli.ui_server import iteration_view, run_detail
from ralphd.engine import state as state_mod

sys.path.insert(0, str(Path(__file__).parent))
from test_cli_ui import _write_dead_run
from test_hub_iteration_dialog import _meta, _write_iteration
from test_router_task_selection import (
    TWO_TASKS,
    _make_sup,
    _ok,
    _router_decision_events,
    _RoutingAgent,
    _ts_event,
)

pytestmark = pytest.mark.fast

STATIC_APP_JS = (Path(__file__).resolve().parents[1] / "src" / "ralphd"
                 / "cli" / "web" / "app.js")


def _event(run_dir: Path, ts: str, **fields) -> None:
    with open(run_dir / "events.jsonl", "a") as f:
        f.write(json.dumps({"ts": ts, "type": "router.decision",
                            **_ts_event("002", **fields)}) + "\n")


def _fixture(tmp_path: Path, *, with_iteration_field: bool) -> tuple[Path, Path]:
    registry = tmp_path / "registry"
    run_dir = _write_dead_run(registry, "run-link", state="failed",
                              verdict="unverified")
    for n in (1, 2, 3):
        _write_iteration(run_dir, n, _meta(n=n), texts=[f"iteration {n}"])
    extra = {"iteration": 2} if with_iteration_field else {}
    _event(run_dir, "2026-09-03T10:00:00Z", **extra)
    return registry, run_dir


# ------------------------------------------------------------- criterion 1

def test_routed_dispatch_event_carries_the_dispatched_iteration_number(tmp_path):
    sup, run = _make_sup(tmp_path, [dict(t) for t in TWO_TASKS],
                         route_task_selection=True)
    sup.runner = _RoutingAgent(run, router_answers=[
        _ok(json.dumps({"candidate_id": "002", "reason": "picked"}))])  # type: ignore[assignment]

    asyncio.run(sup._step_worker())

    (event,) = _router_decision_events(run)
    dispatched = sup.iterations_used
    assert dispatched >= 1
    assert event["iteration"] == dispatched
    meta = json.loads((run.root / "iterations" / f"{dispatched:04d}"
                       / "meta.json").read_text())
    assert meta["number"] == dispatched
    assert event["choice"] == meta["assignedTaskId"]
    (entry,) = state_mod.router_call_entries(run.root)
    assert entry["iteration"] == dispatched


def test_entry_omits_iteration_when_the_event_lacks_it_or_it_is_junk():
    base = _ts_event("002")
    assert "iteration" not in state_mod.task_selection_call_entry(base, 1)
    for junk in (None, "2", True, 0, -1, 1.5):
        entry = state_mod.task_selection_call_entry({**base, "iteration": junk}, 1)
        assert "iteration" not in entry, junk
    assert state_mod.task_selection_call_entry({**base, "iteration": 4}, 1)["iteration"] == 4


# ------------------------------------------------------------- criterion 2

def test_run_detail_marks_only_the_routed_iteration_row(tmp_path):
    registry, _ = _fixture(tmp_path, with_iteration_field=True)
    detail = run_detail(registry, "run-link")
    rows = {r["number"]: r for r in detail["iterations"]}
    assert rows[2]["routerCallIndex"] == 1
    assert "routerCallIndex" not in rows[1]
    assert "routerCallIndex" not in rows[3]
    # the dialog payload agrees
    assert iteration_view(registry, "run-link", 2)["routerCallIndex"] == 1
    assert "routerCallIndex" not in iteration_view(registry, "run-link", 1)
    # a repeat request (cached rows) is still marked on that row only
    again = {r["number"]: r for r in run_detail(registry, "run-link")["iterations"]}
    assert again[2]["routerCallIndex"] == 1 and "routerCallIndex" not in again[1]


def test_fixture_without_the_iteration_field_links_no_row(tmp_path):
    registry, _ = _fixture(tmp_path, with_iteration_field=False)
    detail = run_detail(registry, "run-link")
    assert len(detail["iterations"]) == 3
    assert all("routerCallIndex" not in r for r in detail["iterations"])
    assert all("routerCallIndex" not in iteration_view(registry, "run-link", n)
               for n in (1, 2, 3))


# ------------------------------------------------------------- criterion 3

def test_app_js_gives_entries_an_id_and_renders_the_link():
    src = STATIC_APP_JS.read_text()
    # the entry id the link scrolls to
    assert 'return "router-call-" + String(index);' in src
    assert "id: routerCallElementId(c.index)" in src
    # the link: only for a numeric routerCallIndex, on the row and in the dialog
    assert "it.routerCallIndex" in src
    assert '["router call"]' in src
    assert "routerCallLink(it)," in src          # timeline row
    assert "routerCallLink(body)," in src        # iteration dialog
    # opens the panel, scrolls to the entry, highlights it
    assert "details.open = true" in src
    assert "scrollIntoView" in src
    assert 'classList.add("router-call-highlight")' in src
    # a click on the row's link never also opens the row's own dialog
    assert "ev.stopPropagation(); openRouterCall(index)" in src
    # wording and data go through text nodes / h(), never innerHTML with data
    link_src = src[src.index("function routerCallLink"):src.index("function renderRouterCalls")]
    assert "innerHTML" not in link_src
    # criterion 4: the run-wide panel is still rendered
    assert 'class: "router-call-list-details"' in src
    assert 'id: "router-call-list"' in src
