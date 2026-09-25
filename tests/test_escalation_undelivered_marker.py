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

`app.js`'s own row and run-detail state-cell renderers
(`runRowStateChildren`, `stateCell`) are executed in node against a minimal
DOM stand-in, fed the payloads `ui_server` produced -- for the runs-list row
both with and without `containerGone`, since a paused dead run renders
through the gone branch.
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


def _ralphctl_status_json(capsys) -> dict:
    cli_main.cmd_status(_StatusArgs(RUN_ID, as_json=True))
    return json.loads(capsys.readouterr().out)


def test_ralphctl_status_json_carries_the_marker_fields(
        tmp_path, monkeypatch, capsys):
    registry = tmp_path / "registry"
    monkeypatch.setenv("RALPHD_REGISTRY", str(registry))
    monkeypatch.setattr(cli_main, "_dangling_run_entry", lambda run_id: None)

    _always_failing(monkeypatch)
    sup = _supervisor(tmp_path, run_root=f"registry/runs/{RUN_ID}",
                      notifier_url="http://notifier.example/notify")
    _trigger_undelivered_escalation(sup)

    doc = _ralphctl_status_json(capsys)
    assert doc["escalationUndelivered"] is True
    assert "escalation undelivered" in doc["escalationUndeliveredMarker"]
    assert "pauseMarker" in doc


def test_ralphctl_status_json_drops_the_marker_once_the_pause_is_answered(
        tmp_path, monkeypatch, capsys):
    registry = tmp_path / "registry"
    monkeypatch.setenv("RALPHD_REGISTRY", str(registry))
    monkeypatch.setattr(cli_main, "_dangling_run_entry", lambda run_id: None)

    _always_failing(monkeypatch)
    sup = _supervisor(tmp_path, run_root=f"registry/runs/{RUN_ID}",
                      notifier_url="http://notifier.example/notify")
    _trigger_undelivered_escalation(sup)
    sup.resume()

    doc = _ralphctl_status_json(capsys)
    assert doc["escalationUndelivered"] is False
    assert doc["escalationUndeliveredMarker"] == ""


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
# app.js: the REAL row / detail-cell renderers, executed in node against a
# minimal DOM stand-in, fed the payloads `ui_server` itself produced above
# --------------------------------------------------------------------------
APP_JS_PATH = Path(ui_server.__file__).parent / "web" / "app.js"

_NODE_RENDER = r"""
const fs = require('fs');
const src = fs.readFileSync(process.argv[1], 'utf8');
function grab(name) {
  const start = src.indexOf('function ' + name + '(');
  if (start < 0) throw Error('missing function ' + name);
  const end = src.indexOf('\n}\n', start);
  return src.slice(start, end + 3);
}
function text(node) {
  if (typeof node === 'string') return node;
  return (node.children || []).map(text).join('');
}
function classes(node, out) {
  if (typeof node === 'string') return out;
  if (node.attrs && node.attrs.class) out.push(node.attrs.class);
  for (const c of node.children || []) classes(c, out);
  return out;
}
const h = (tag, attrs, children) => ({ tag, attrs: attrs || {}, children: children || [] });
const FAULT_BADGE_STATES = new Set(["failed", "aborted"]);
const faultBadge = () => null;
eval(grab('pill') + grab('stateMarkerSpans') + grab('runRowStateChildren') + grab('stateCell'));
const input = JSON.parse(process.argv[2]);
const node = input.surface === 'row'
  ? h('td', {}, runRowStateChildren(input.doc))
  : stateCell({ runId: 'x' }, input.doc);
console.log(JSON.stringify({ text: text(node), classes: classes(node, []) }));
"""


def _node_render(surface: str, doc: dict) -> dict:
    import shutil
    import subprocess
    if shutil.which("node") is None:
        pytest.skip("node not installed")
    result = subprocess.run(
        ["node", "-e", _NODE_RENDER, str(APP_JS_PATH),
         json.dumps({"surface": surface, "doc": doc})],
        capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def _mark_answered(run_dir: Path) -> None:
    status = json.loads((run_dir / "status.json").read_text())
    status["state"] = "running"
    status["pauseReason"] = None
    (run_dir / "status.json").write_text(json.dumps(status))


@pytest.mark.parametrize("container_gone", [False, True])
def test_app_js_renders_the_marker_on_the_runs_list_row(tmp_path, container_gone):
    registry = tmp_path / "registry"
    _write_undelivered_run(registry, "js-row")
    row = next(r for r in ui_server.run_list(registry) if r["runId"] == "js-row")
    # A dead run (the on-disk fixture has no container) is the case the
    # gone branch renders; the live branch is the same payload without it.
    row["containerGone"] = container_gone

    rendered = _node_render("row", row)

    assert "escalation-undelivered-marker" in rendered["classes"]
    assert row["escalationUndeliveredMarker"] in rendered["text"]
    assert ("container-gone-marker" in rendered["classes"]) is container_gone


@pytest.mark.parametrize("container_gone", [False, True])
def test_app_js_drops_the_marker_on_the_runs_list_row_once_answered(
        tmp_path, container_gone):
    registry = tmp_path / "registry"
    run_dir = _write_undelivered_run(registry, "js-row-answered")
    _mark_answered(run_dir)
    row = next(r for r in ui_server.run_list(registry)
               if r["runId"] == "js-row-answered")
    row["containerGone"] = container_gone

    rendered = _node_render("row", row)

    assert "escalation-undelivered-marker" not in rendered["classes"]
    assert "escalation undelivered" not in rendered["text"]


def test_app_js_renders_the_marker_on_the_run_detail_page(tmp_path):
    registry = tmp_path / "registry"
    _write_undelivered_run(registry, "js-det")
    detail = ui_server.run_detail(registry, "js-det")

    rendered = _node_render("detail", detail["status"])

    assert "escalation-undelivered-marker" in rendered["classes"]
    assert detail["status"]["escalationUndeliveredMarker"] in rendered["text"]


def test_app_js_drops_the_marker_on_the_run_detail_page_once_answered(tmp_path):
    registry = tmp_path / "registry"
    run_dir = _write_undelivered_run(registry, "js-det-answered")
    _mark_answered(run_dir)
    detail = ui_server.run_detail(registry, "js-det-answered")

    rendered = _node_render("detail", detail["status"])

    assert "escalation-undelivered-marker" not in rendered["classes"]
    assert "escalation undelivered" not in rendered["text"]
