"""Task 020 (#24): the hub's per-approach archive data -- server side.

`ralphctl tasks <run> --approach N|--all` (task 019) reads
`approaches/NN/tasks.json` -- a previous approach's OWN task table, frozen
at the moment `LoopSupervisor._archive_approach()` (`engine/loop.py`)
superseded it. Until now the hub had no way to show that history at all.
`GET /api/runs/<id>/approaches` now lists every archived approach with its
own counts, and `GET /api/runs/<id>/approaches/<n>` serves one approach's
full task table plus its `notes.md`/`review-findings.md` text -- the
groundwork task 021 renders as the expandable approach history in the run
detail page.

What is pinned here:

  * one shaping, one resolver: the listing/view come from
    `engine.state.archived_approaches_list`/`archived_approach_view`, the
    SAME reader `ralphctl tasks --approach|--all` (task 019) uses -- an
    archived approach's counts and task list can never disagree between the
    CLI and the hub;
  * counts come from the archived `tasks.json` itself, never recomputed
    from the live plan -- proven by seeding a live plan that looks nothing
    like the archived one;
  * missing `notes.md`/`review-findings.md` are reported as absent
    (`RUN_DOCUMENT_ABSENT`) rather than raising -- an approach can be
    archived with no notes at all (a first approach with nothing to say)
    and still answer cleanly;
  * a never-archived approach number and a run with no archives at all are
    both clean 404s / empty lists, never a 500;
  * purely on-disk BY DESIGN, like the document/artifact dialogs (tasks
    022/024): no live branch, no `live` key, proven by a live StubEngineApi
    that records ZERO requests while the endpoints answer.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from ralphd.cli.ui_server import approach_view, approaches_list_view
from ralphd.engine.state import RUN_DOCUMENT_ABSENT

sys.path.insert(0, str(Path(__file__).parent))
from test_cli_ui import StubEngineApi, UiServer, _write_dead_run, ui  # noqa: E402

__all__ = ["StubEngineApi", "UiServer", "ui"]


LIVE_PLAN = {"tasks": [{"id": "901", "title": "live only", "status": "pending"}]}

APPROACH_1_PLAN = {
    "tasks": [
        {"id": "001", "title": "archived one", "status": "completed"},
        {"id": "002", "title": "archived two", "status": "failed",
         "notes": "gave up: requirement C unmet"},
    ]
}

APPROACH_2_PLAN = {
    "tasks": [
        {"id": "001", "title": "second attempt one", "status": "completed"},
        {"id": "002", "title": "second attempt two", "status": "completed"},
    ]
}


def _seed(registry: Path, run_id: str = "hub-approaches", *,
          live_plan: dict | None = LIVE_PLAN,
          archives: dict[int, dict] | None = None,
          notes: dict[int, str] | None = None,
          findings: dict[int, str] | None = None,
          live=None) -> Path:
    if live is None:
        rdir = _write_dead_run(registry, run_id, state="failed",
                               verdict="unverified")
    else:
        from test_cli_ui import _write_run_with_api
        rdir = _write_run_with_api(registry, run_id, live, state="running")
    if live_plan is not None:
        (rdir / "tasks.json").write_text(json.dumps(live_plan))
    for n, plan in (archives or {}).items():
        adir = rdir / "approaches" / f"{n:02d}"
        adir.mkdir(parents=True)
        (adir / "tasks.json").write_text(json.dumps(plan))
        if notes and n in notes:
            (adir / "notes.md").write_text(notes[n])
        if findings and n in findings:
            (adir / "review-findings.md").write_text(findings[n])
    return rdir


# --------------------------------------------------------------- function level


def test_approaches_list_reports_each_archives_own_counts(tmp_path):
    registry = tmp_path / "registry"
    _seed(registry, archives={1: APPROACH_1_PLAN, 2: APPROACH_2_PLAN})

    payload = approaches_list_view(registry, "hub-approaches")
    assert payload["runId"] == "hub-approaches"
    rows = {r["approach"]: r for r in payload["approaches"]}
    assert list(rows) == [1, 2]
    # approach 1 has one failed task -- distinct from approach 2 and from
    # the live plan (one pending task): the counts are the archive's own.
    assert rows[1]["counts"]["completed"] == 1
    assert rows[1]["counts"]["failed"] == 1
    assert rows[2]["counts"]["completed"] == 2
    assert rows[2]["counts"].get("failed", 0) == 0


def test_approaches_list_is_empty_for_a_run_with_no_archives(tmp_path):
    registry = tmp_path / "registry"
    _seed(registry)

    payload = approaches_list_view(registry, "hub-approaches")
    assert payload == {"runId": "hub-approaches", "approaches": []}


def test_approach_view_carries_the_archived_tasks_not_the_live_plan(tmp_path):
    registry = tmp_path / "registry"
    _seed(registry, archives={1: APPROACH_1_PLAN})

    view = approach_view(registry, "hub-approaches", 1)
    assert view["approach"] == 1
    ids = [t["id"] for t in view["tasks"]["tasks"]]
    assert ids == ["001", "002"]
    # the derived failure-kind map travels with the archived read too
    assert view["tasks"]["taskFailureKinds"] == {"002": "requirement-unmet"}


def test_approach_view_carries_notes_and_findings_text(tmp_path):
    registry = tmp_path / "registry"
    _seed(registry, archives={1: APPROACH_1_PLAN},
          notes={1: "# Notes\nfinished approach 1"},
          findings={1: "# Findings\nmissed requirement C"})

    view = approach_view(registry, "hub-approaches", 1)
    assert view["notes"]["exists"] is True
    assert "finished approach 1" in view["notes"]["text"]
    assert view["findings"]["exists"] is True
    assert "missed requirement C" in view["findings"]["text"]


def test_approach_view_reports_missing_notes_and_findings_as_absent(tmp_path):
    registry = tmp_path / "registry"
    # archived with a tasks.json but deliberately no notes.md/findings file
    _seed(registry, archives={1: APPROACH_1_PLAN})

    view = approach_view(registry, "hub-approaches", 1)
    assert view["notes"]["exists"] is False
    assert view["notes"]["sizeDisplay"] == RUN_DOCUMENT_ABSENT
    assert RUN_DOCUMENT_ABSENT in view["notes"]["text"]
    assert view["findings"]["exists"] is False
    assert RUN_DOCUMENT_ABSENT in view["findings"]["text"]


def test_approach_view_never_archived_is_none_not_an_error(tmp_path):
    registry = tmp_path / "registry"
    _seed(registry, archives={1: APPROACH_1_PLAN})

    assert approach_view(registry, "hub-approaches", 2) is None


# ------------------------------------------------------------------- HTTP level


def test_hub_endpoints_serve_the_listing_and_one_approach(tmp_path, ui):
    registry = tmp_path / "registry"
    _seed(registry, archives={1: APPROACH_1_PLAN, 2: APPROACH_2_PLAN},
          notes={1: "notes for approach one"},
          findings={1: "findings for approach one"})
    server = ui(registry)

    code, body = server.get("/api/runs/hub-approaches/approaches")
    assert code == 200
    assert body == approaches_list_view(registry, "hub-approaches")

    code, body = server.get("/api/runs/hub-approaches/approaches/1")
    assert code == 200
    assert body == approach_view(registry, "hub-approaches", 1)
    assert "notes for approach one" in body["notes"]["text"]
    assert "findings for approach one" in body["findings"]["text"]


def test_hub_approach_endpoints_404_cleanly(tmp_path, ui):
    registry = tmp_path / "registry"
    _seed(registry, archives={1: APPROACH_1_PLAN})
    server = ui(registry)

    code, body = server.get("/api/runs/nope/approaches")
    assert code == 404 and "not found" in body["error"]
    code, body = server.get("/api/runs/nope/approaches/1")
    assert code == 404 and "not found" in body["error"]
    code, body = server.get("/api/runs/hub-approaches/approaches/7")
    assert code == 404 and "no archived approach" in body["error"]
    code, body = server.get("/api/runs/hub-approaches/approaches/not-a-number")
    assert code == 404 and "bad approach number" in body["error"]


def test_hub_approaches_listing_is_empty_not_an_error_with_no_archives(tmp_path, ui):
    registry = tmp_path / "registry"
    _seed(registry)
    server = ui(registry)

    code, body = server.get("/api/runs/hub-approaches/approaches")
    assert code == 200
    assert body == {"runId": "hub-approaches", "approaches": []}


def test_hub_approach_endpoints_never_touch_the_live_api(tmp_path, ui):
    """On-disk BY DESIGN: `_archive_approach` writes these files into the
    run dir itself, so a reachable container must not be consulted."""
    engine = StubEngineApi()
    try:
        registry = tmp_path / "registry"
        _seed(registry, archives={1: APPROACH_1_PLAN}, live=engine)
        server = ui(registry)

        code, listing = server.get("/api/runs/hub-approaches/approaches")
        assert code == 200
        code, view = server.get("/api/runs/hub-approaches/approaches/1")
        assert code == 200
        assert engine.requests == []
        assert "live" not in listing and "live" not in view
        # control: a route that DOES proxy records requests on the same stub
        server.get("/api/runs/hub-approaches")
        assert engine.requests != []
    finally:
        engine.close()
