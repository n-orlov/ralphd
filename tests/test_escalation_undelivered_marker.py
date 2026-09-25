"""Task 005 (PRD requirement A.5, #269): "an undelivered escalation is
visible without reading `events.jsonl`. `ralphctl status` shows it next to
the pause marker, and so do the hub's run row and run page." **Test:** each
surface shows the marker for a run whose last escalation went undelivered,
and none of them shows it after the pause is answered.

At 9b72b700 there is no `state.escalation_undelivered`/`state.format_
escalation_undelivered_marker`, `ralphctl status` never renders a pause
marker at all (only `ralphctl runs`/the hub run list did, task 017), and
neither `ui_server.run_list`/`run_detail` nor `app.js` carry/render an
`escalationUndelivered(Marker)` field -- every node below fails against
that checkout.

Three surfaces, one shared predicate (`state.escalation_undelivered`,
read straight off `events.jsonl` + `status.json`, no live engine needed):

* `ralphctl status` (`cli/main.py::cmd_status`) -- human output, on-disk
  fallback path (no `host.json`, so `api()` fails and the CLI reads the
  run dir directly, exactly like an operator polling a dead run would);
* the hub's runs-list row (`ui_server.run_list`);
* the hub's run-detail page's own status doc (`ui_server.run_detail`,
  `_with_pause_markers`).

`app.js` is checked structurally (it reads/renders the two new fields) --
the discipline every other "cheap app.js guard" module in this suite
already uses (`tests/test_steering_origin_marker.py` etc.): a real
Chromium render is the browser tier's job, not this one's.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from test_cli_ui import _write_dead_run

from ralphd.cli import main as cli_main
from ralphd.cli import ui_server
from ralphd.engine import hat_tools, run_record
from ralphd.engine.state import RunDir, escalation_undelivered, format_escalation_undelivered_marker
from tests.conftest import supervisor_factory

RUN_ID = "task005-escalation-undelivered-marker"

_supervisor = supervisor_factory(run_id=RUN_ID, cfg_defaults=False)

APP_JS = (Path(ui_server.__file__).parent / "web" / "app.js").read_text()


def _always_failing(monkeypatch):
    def failing_poster(url, text, timeout_s):
        return 500, None
    monkeypatch.setattr(hat_tools, "_http_post_text", failing_poster)


def _trigger_undelivered_escalation(sup):
    """Drives the real `rejected_last_approach` pause + escalation path with
    a permanently-failing notifier -- leaving `status.json` at `state:
    "paused"`, `pauseReason: "rejected_last_approach"`, and exactly one
    `notification.escalation_undelivered` event on `events.jsonl` (see
    `tests/test_escalation_delivery.py`, task 004, for the delivery path
    itself -- this module only needs the OUTCOME as a fixture)."""
    sup._sleep_before_escalation_retry = lambda seconds: None
    sup._approach = 1
    sup._pause_and_escalate_last_approach_rejection(1)


# --------------------------------------------------------------------------
# the shared predicate itself
# --------------------------------------------------------------------------
def test_escalation_undelivered_is_true_while_paused_on_the_undelivered_escalation(
        tmp_path, monkeypatch):
    _always_failing(monkeypatch)
    root = tmp_path / "run"
    sup = _supervisor(root, notifier_url="http://notifier.example/notify")

    _trigger_undelivered_escalation(sup)

    status = sup.run.read_status()
    assert status["state"] == "paused"
    assert escalation_undelivered(root, status) is True
    assert format_escalation_undelivered_marker(root, status) != ""


def test_escalation_undelivered_is_false_the_instant_the_pause_is_answered(
        tmp_path, monkeypatch):
    _always_failing(monkeypatch)
    root = tmp_path / "run"
    sup = _supervisor(root, notifier_url="http://notifier.example/notify")

    _trigger_undelivered_escalation(sup)
    assert escalation_undelivered(root, sup.run.read_status()) is True

    sup.resume()  # `ralphctl unpause` / POST /resume

    status = sup.run.read_status()
    assert status["state"] != "paused"
    assert escalation_undelivered(root, status) is False
    assert format_escalation_undelivered_marker(root, status) == ""


def test_escalation_undelivered_is_false_when_delivery_actually_succeeded(
        tmp_path, monkeypatch):
    """Never fires for the ordinary pause -- only for one whose delivery
    was exhausted."""
    def ok_poster(url, text, timeout_s):
        return 200, None
    monkeypatch.setattr(hat_tools, "_http_post_text", ok_poster)

    root = tmp_path / "run"
    sup = _supervisor(root, notifier_url="http://notifier.example/notify")
    sup._sleep_before_escalation_retry = lambda seconds: None
    sup._approach = 1
    sup._pause_and_escalate_last_approach_rejection(1)

    status = sup.run.read_status()
    assert status["state"] == "paused"
    assert escalation_undelivered(root, status) is False


def test_escalation_undelivered_never_crashes_on_the_in_memory_only_pause(
        tmp_path, monkeypatch):
    """`rejected_incurable` pauses in-memory only (`LoopSupervisor.pause()`
    called with no reason, see `_pause_and_escalate_incurable_rejection`) --
    `status.json`'s `state` never becomes `"paused"` for it, the same gap
    `state.format_pause_marker` already lives with for this one reason
    (docs note: gotcha for task 005). The predicate must read that
    gracefully (`False`, never an exception), not claim a marker it cannot
    actually place."""
    _always_failing(monkeypatch)
    root = tmp_path / "run"
    sup = _supervisor(root, notifier_url="http://notifier.example/notify")
    sup._sleep_before_escalation_retry = lambda seconds: None
    sup._review_findings = lambda: []

    sup._pause_and_escalate_incurable_rejection(1)

    status = sup.run.read_status()
    assert status.get("pauseReason") is None
    assert escalation_undelivered(root, status) is False


# --------------------------------------------------------------------------
# surface 1: `ralphctl status`
# --------------------------------------------------------------------------
class _StatusArgs:
    def __init__(self, run_id: str, as_json: bool = False):
        self.run_id = run_id
        self.json = as_json


def _ralphctl_status(capsys) -> str:
    cli_main.cmd_status(_StatusArgs(RUN_ID))
    return capsys.readouterr().out


def test_ralphctl_status_shows_the_marker_next_to_the_pause_marker(
        tmp_path, monkeypatch, capsys):
    registry = tmp_path / "registry"
    monkeypatch.setenv("RALPHD_REGISTRY", str(registry))
    monkeypatch.setattr(cli_main, "_dangling_run_entry", lambda run_id: None)

    _always_failing(monkeypatch)
    sup = _supervisor(tmp_path, run_root=f"registry/runs/{RUN_ID}",
                      notifier_url="http://notifier.example/notify")
    _trigger_undelivered_escalation(sup)

    out = _ralphctl_status(capsys)
    state_line = next(l for l in out.splitlines() if l.startswith("state:"))
    assert "paused" in state_line
    assert "escalation undelivered" in state_line


def test_ralphctl_status_shows_no_marker_once_the_pause_is_answered(
        tmp_path, monkeypatch, capsys):
    registry = tmp_path / "registry"
    monkeypatch.setenv("RALPHD_REGISTRY", str(registry))
    monkeypatch.setattr(cli_main, "_dangling_run_entry", lambda run_id: None)

    _always_failing(monkeypatch)
    sup = _supervisor(tmp_path, run_root=f"registry/runs/{RUN_ID}",
                      notifier_url="http://notifier.example/notify")
    _trigger_undelivered_escalation(sup)
    sup.resume()

    out = _ralphctl_status(capsys)
    state_line = next(l for l in out.splitlines() if l.startswith("state:"))
    assert "escalation undelivered" not in state_line
    assert "escalation undelivered" not in out


# --------------------------------------------------------------------------
# surface 2: the hub's runs-list row
# --------------------------------------------------------------------------
def _write_undelivered_run(registry: Path, run_id: str, **status_fields) -> Path:
    run_dir = _write_dead_run(registry, run_id, state="paused",
                              pauseReason="rejected_last_approach",
                              **status_fields)
    rd = RunDir(root=run_dir)
    rd.emit("operator_escalation", approach=1, reason="rejected_last_approach",
           findings=[], escalation="parked")
    rd.emit(run_record.NOTIFICATION_ESCALATION_UNDELIVERED_EVENT,
           reason="rejected_last_approach")
    return run_dir


def test_hub_run_list_row_carries_the_marker(tmp_path):
    registry = tmp_path / "registry"
    _write_undelivered_run(registry, "row-undelivered")
    _write_dead_run(registry, "row-ordinary", state="running")

    rows = {r["runId"]: r for r in ui_server.run_list(registry)}

    assert rows["row-undelivered"]["escalationUndelivered"] is True
    assert rows["row-undelivered"]["escalationUndeliveredMarker"] != ""
    assert rows["row-ordinary"]["escalationUndelivered"] is False
    assert rows["row-ordinary"]["escalationUndeliveredMarker"] == ""


def test_hub_run_list_row_drops_the_marker_once_answered(tmp_path):
    registry = tmp_path / "registry"
    run_dir = _write_undelivered_run(registry, "row-answered")
    # `ralphctl unpause`/a ruling releasing the pause: state and pauseReason
    # go back exactly the way `LoopSupervisor._clear_persisted_pause` leaves
    # them.
    status = json.loads((run_dir / "status.json").read_text())
    status["state"] = "running"
    status["pauseReason"] = None
    (run_dir / "status.json").write_text(json.dumps(status))

    rows = {r["runId"]: r for r in ui_server.run_list(registry)}

    assert rows["row-answered"]["escalationUndelivered"] is False
    assert rows["row-answered"]["escalationUndeliveredMarker"] == ""


# --------------------------------------------------------------------------
# surface 3: the hub's run-detail page
# --------------------------------------------------------------------------
def test_hub_run_detail_carries_the_marker(tmp_path):
    registry = tmp_path / "registry"
    _write_undelivered_run(registry, "det-undelivered")

    detail = ui_server.run_detail(registry, "det-undelivered")

    assert detail["status"]["escalationUndelivered"] is True
    assert detail["status"]["escalationUndeliveredMarker"] != ""
    # the pause marker rides alongside it now too (task 005 adds this to
    # the run-detail page, which never carried it before this task).
    assert "pauseMarker" in detail["status"]


def test_hub_run_detail_drops_the_marker_once_answered(tmp_path):
    registry = tmp_path / "registry"
    run_dir = _write_undelivered_run(registry, "det-answered")
    status = json.loads((run_dir / "status.json").read_text())
    status["state"] = "running"
    status["pauseReason"] = None
    (run_dir / "status.json").write_text(json.dumps(status))

    detail = ui_server.run_detail(registry, "det-answered")

    assert detail["status"]["escalationUndelivered"] is False
    assert detail["status"]["escalationUndeliveredMarker"] == ""


# --------------------------------------------------------------------------
# app.js: structural guard (the browser tier renders it for real)
# --------------------------------------------------------------------------
def test_app_js_renders_the_marker_on_the_runs_list_row():
    block = APP_JS.split("function stateMarkerSpans(")[1].split("\n}")[0]
    assert "escalation-undelivered-marker" in block
    row_block = APP_JS.split("for (const r of sortRuns(runs))")[1][:2000]
    assert "stateMarkerSpans(r.pauseMarker, r.escalationUndeliveredMarker)" in row_block


def test_app_js_renders_the_marker_on_the_run_detail_page():
    block = APP_JS.split("function stateCell(")[1].split("\n}")[0]
    assert "stateMarkerSpans(s.pauseMarker, s.escalationUndeliveredMarker)" in block
