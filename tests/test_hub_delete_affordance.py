"""Task 031 (#19): the hub's delete affordance -- the payload half.

Task 030 gave the hub `DELETE /api/runs/<id>` behind a gate that only opens
for a run whose recorded state is terminal. A button in a browser now has to
agree with that gate *before* it is clicked: `run_list` rows and `run_detail`
carry `deletable` plus, when it is false, the server's own refusal sentence
(`deleteRefusal`), so app.js never decides for itself which states count as
finished and never words a refusal of its own.

Tiers here:

* in-process, on `ui_server.deletion_fields` and the two views that splice it
  in (including that a *live* proxied status cannot talk the detail page into
  offering a deletion the endpoint would refuse);
* black-box hub, over the real `ralphctl ui` subprocess with the recording
  stub docker: for every state, `deletable` PREDICTS what
  `DELETE /api/runs/<id>` actually answers, and `deleteRefusal` is byte-equal
  to the error it returns;
* cheap `app.js` guards on the contract the browser test
  (tests/test_browser_hub.py::test_run_list_and_detail_delete_a_run_behind_a_
  confirm_dialog) depends on.

The rendering itself is asserted in a real Chromium there, not here.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from test_cli_ui import StubEngineApi, UiServer, _write_dead_run, _write_run_with_api, ui

from ralphd.cli import ui_server
from ralphd.engine.state import NONTERMINAL_STATES, TERMINAL_STATES

sys.path.insert(0, str(Path(__file__).parent))
from test_hub_delete import HAS_CONTAINER, Hub

__all__ = ["UiServer", "ui"]

APP_JS = (Path(ui_server.__file__).parent / "web" / "app.js").read_text()


# --------------------------------------------------------------------------
# unit tier: the fields, from the gate itself
# --------------------------------------------------------------------------
@pytest.mark.parametrize("state", sorted(TERMINAL_STATES))
def test_a_terminal_run_is_offered_deletion_with_no_reason_to_show(state):
    assert ui_server.deletion_fields({"state": state}) == {
        "deletable": True, "deleteRefusal": None}


@pytest.mark.parametrize("state", sorted(NONTERMINAL_STATES))
def test_an_active_run_is_not_deletable_and_carries_the_reason(state):
    fields = ui_server.deletion_fields({"state": state})
    assert fields["deletable"] is False
    # the server's OWN sentence, not a second wording for the browser
    assert fields["deleteRefusal"] == ui_server.DELETE_REFUSED_ACTIVE.format(
        state=state)


@pytest.mark.parametrize("status", [{}, {"state": None}, {"state": ""},
                                    {"state": "wat"}, None, "not a dict"])
def test_a_state_we_cannot_read_is_never_offered_deletion(status):
    """`unknown is not permission` (task 030's gate) reaches the button too:
    the control is disabled and the reason is the unknown-state sentence."""
    fields = ui_server.deletion_fields(status)
    assert fields["deletable"] is False
    assert fields["deleteRefusal"] == ui_server.DELETE_REFUSED_UNKNOWN.format(
        state=(status.get("state") if isinstance(status, dict) else None)
        or ui_server.UNKNOWN_STATE)


def test_the_fields_are_exactly_the_gate_for_every_status_shape():
    """One decision, two spellings of it -- and they may never disagree."""
    for status in [{"state": s} for s in sorted({*TERMINAL_STATES, *NONTERMINAL_STATES})] \
            + [{}, {"state": "wat"}, {"state": None}]:
        reason = ui_server.deletion_refusal(status)
        fields = ui_server.deletion_fields(status)
        assert fields == {"deletable": reason is None, "deleteRefusal": reason}


# --------------------------------------------------------------------------
# in-process tier: the two views
# --------------------------------------------------------------------------
def test_run_list_rows_carry_the_delete_fields(tmp_path):
    registry = tmp_path / "registry"
    _write_dead_run(registry, "row-done", state="succeeded")
    _write_dead_run(registry, "row-live", state="running")

    rows = {r["runId"]: r for r in ui_server.run_list(registry)}

    assert rows["row-done"]["deletable"] is True
    assert rows["row-done"]["deleteRefusal"] is None
    assert rows["row-live"]["deletable"] is False
    assert "running" in rows["row-live"]["deleteRefusal"]


def test_a_forged_deletable_in_status_json_cannot_offer_a_deletion(tmp_path):
    """The row is built from the gate, not from keys somebody's status.json
    happens to contain (the `approachDisplay` discipline)."""
    registry = tmp_path / "registry"
    _write_dead_run(registry, "row-forged", state="running", deletable=True,
                    deleteRefusal=None)

    row = ui_server.run_list(registry)[0]

    assert row["deletable"] is False
    assert row["deleteRefusal"] == ui_server.DELETE_REFUSED_ACTIVE.format(
        state="running")


def test_run_detail_carries_the_delete_fields_at_top_level(tmp_path):
    registry = tmp_path / "registry"
    _write_dead_run(registry, "det-done", state="aborted")

    detail = ui_server.run_detail(registry, "det-done")

    assert detail["deletable"] is True
    assert detail["deleteRefusal"] is None
    # a fact about the run, not a rendering of the status doc
    assert "deletable" not in detail["status"]


def test_run_detail_of_an_active_run_states_the_refusal(tmp_path):
    registry = tmp_path / "registry"
    _write_dead_run(registry, "det-live", state="starting")

    detail = ui_server.run_detail(registry, "det-live")

    assert detail["deletable"] is False
    assert detail["deleteRefusal"] == ui_server.DELETE_REFUSED_ACTIVE.format(
        state="starting")


def test_a_live_status_claiming_it_finished_does_not_unlock_the_button(tmp_path):
    """The delete endpoint gates on what the run dir RECORDS. The detail card
    may be showing a live proxied status, but the control must predict the
    endpoint -- so it keeps reading status.json, and a live answer (or a
    forged one) cannot promise a deletion that would be refused."""
    registry = tmp_path / "registry"
    engine = StubEngineApi(status={"runId": "det-mixed", "state": "succeeded",
                                   "deletable": True, "deleteRefusal": None})
    try:
        _write_run_with_api(registry, "det-mixed", engine, state="running")

        detail = ui_server.run_detail(registry, "det-mixed")

        assert detail["live"] is True
        assert detail["status"]["state"] == "succeeded"     # live answer shown
        assert detail["deletable"] is False                # recorded state rules
        assert detail["deleteRefusal"] == \
            ui_server.DELETE_REFUSED_ACTIVE.format(state="running")
    finally:
        engine.close()


# --------------------------------------------------------------------------
# hub tier: the promise matches the act
# --------------------------------------------------------------------------
@pytest.mark.parametrize("state", sorted({*TERMINAL_STATES, *NONTERMINAL_STATES}))
def test_deletable_predicts_what_the_endpoint_answers(ui, tmp_path, state):
    hub = Hub(ui, tmp_path, **HAS_CONTAINER)
    hub.seed(f"pred-{state}", state=state)

    _, body = hub.server.get("/api/runs")
    row = next(r for r in body["runs"] if r["runId"] == f"pred-{state}")
    _, detail = hub.server.get(f"/api/runs/pred-{state}")
    code, answer = hub.delete(f"pred-{state}")

    assert (row["deletable"], detail["deletable"]) == (code == 200, code == 200)
    if code == 200:
        assert row["deleteRefusal"] is None
        assert answer == {"removed": f"pred-{state}", "stoppedContainer": True}
    else:
        assert code == 409
        # byte-equal: the disabled button shows the endpoint's own words
        assert row["deleteRefusal"] == answer["error"]
        assert detail["deleteRefusal"] == answer["error"]


@pytest.mark.parametrize("kwargs", [
    {"state": None},                                # no status.json at all
    {"status": "{ this is not json"},               # unreadable
    {"status": json.dumps({"state": "wat"})},       # unrecognized state
])
def test_an_unestablishable_run_is_disabled_in_the_payload_too(ui, tmp_path, kwargs):
    hub = Hub(ui, tmp_path, **HAS_CONTAINER)
    hub.seed("pred-mystery", **kwargs)

    _, body = hub.server.get("/api/runs")
    row = body["runs"][0]
    code, answer = hub.delete("pred-mystery")

    assert row["deletable"] is False
    assert code == 409
    assert row["deleteRefusal"] == answer["error"]
    assert hub.run_dir("pred-mystery").exists()


def test_the_row_stops_advertising_deletion_once_the_run_is_gone(ui, tmp_path):
    hub = Hub(ui, tmp_path)
    hub.seed("pred-gone", state="succeeded")
    assert hub.server.get("/api/runs")[1]["runs"][0]["deletable"] is True

    assert hub.delete("pred-gone")[0] == 200

    assert hub.server.get("/api/runs")[1]["runs"] == []
    assert hub.server.get("/api/runs/pred-gone")[0] == 404


# --------------------------------------------------------------------------
# app.js guards (the rendering itself is asserted in a real Chromium)
# --------------------------------------------------------------------------
def test_app_js_asks_the_server_whether_a_run_may_be_deleted():
    body = APP_JS.split("function deleteControl(")[1].split("\n}")[0]
    code = "\n".join(ln for ln in body.splitlines()
                     if not ln.strip().startswith("//"))
    assert "o.deletable === true" in code
    assert "o.deleteRefusal" in code
    # no second opinion about which states are finished, and no refusal
    # sentence of its own
    for word in ("succeeded", "failed", "aborted", "running", "starting",
                 "TERMINAL", "still active"):
        assert word not in code, (word, code)


def test_app_js_deletes_with_the_delete_method_on_the_run_endpoint():
    body = APP_JS.split("async function requestRunDeletion(")[1].split("\n}")[0]
    assert 'method: "DELETE"' in body
    assert "/api/runs/${encodeURIComponent(runId)}`" in body


def test_the_confirmation_names_the_run_id_and_is_irreversible():
    body = APP_JS.split("function deleteConfirmText(")[1].split("\n}")[0]
    assert "String(runId)" in body
    assert "cannot be undone" in body


def test_the_delete_column_is_not_a_sortable_run_column():
    """The action is an affordance, not a value: adding it to `RUN_COLUMNS`
    would invent a sort key the CLI has no mirror for
    (tests/test_cli_runs_sort.py::test_sort_keys_mirror_the_hub_columns)."""
    block = APP_JS.split("const RUN_COLUMNS = [")[1].split("];")[0]
    assert "delete" not in block.lower(), block


def test_every_dialog_goes_through_the_one_show_dialog_invariant():
    """Exactly one dialog at a time, whatever kind -- the delete confirmation
    must not be able to stack on top of an open text dialog (or on a 4s
    `load()`'s rebuild)."""
    show = APP_JS.split("function showDialog(")[1].split("\n}")[0]
    assert 'querySelectorAll("dialog")' in show and "previous.remove()" in show
    # both dialog builders hand off to it, and neither shows itself
    for builder in ("function openTextDialog(", "function openDeleteDialog("):
        body = APP_JS.split(builder)[1].split("\nfunction ")[0]
        assert "showDialog(dlg)" in body, builder
        assert "showModal" not in body, builder
    # ...and `showModal` is called in exactly one place in the whole bundle
    assert APP_JS.count("showModal") == show.count("showModal")


def test_both_surfaces_render_the_same_delete_control():
    """One affordance, two places -- not two implementations."""
    assert APP_JS.count("function deleteControl(") == 1
    list_body = APP_JS.split("async function renderRunList(")[1].split("\n}")[0]
    detail_body = APP_JS.split("function renderDelete(")[1].split("\n}")[0]
    assert "deleteControl(r.runId, r," in list_body
    assert "deleteControl(runId, detail," in detail_body
