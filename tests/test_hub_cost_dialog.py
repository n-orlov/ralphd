"""Task 028 (#18.5): the hub's cost-breakdown dialog -- server side.

The run-detail usage card showed ONE money string and, under it, per-phase and
per-approach tables of raw token counts with no word on how much of that money
is known: a `$0.5000` sitting next to tokens nothing priced looked exactly like
a complete figure. `GET /api/runs/<id>/cost` now answers "what did this run
spend, per phase and per approach, and which parts of that are quoted, derived
or unavailable", and `web/app.js` opens it in THE single text dialog from the
cost cell itself.

What is pinned here:

  * ONE shaping and ONE wording: the payload IS `state.cost_breakdown` (task
    027's dict, `summaryLines` and all) and its `text` is
    `state.cost_breakdown_text` -- asserted byte-for-byte against `ralphctl cost
    <run> --json` and line-for-line against what `ralphctl cost <run>` prints,
    so the hub cannot label the same money differently from the CLI;
  * the headline stays the card's own `costDisplay`: the dialog's `costDisplay`
    IS `status.usage.costDisplay` as the detail payload renders it, so opening
    the breakdown can never contradict the number that was clicked;
  * every kind of money is labelled in that text (provider-quoted, `~… derived`,
    a partial subtotal, `unavailable`) and the per-phase/per-approach buckets
    are in it;
  * unknown is not zero (#10's rule): task 049's implausible zero quote renders
    `unavailable` with the anomaly named, never `$0.00`;
  * a run with no usage at all is not an error: `hasUsage: false` and
    `state.COST_NO_USAGE` as the text, so the cell is never a lie about having
    a breakdown to show;
  * purely on-disk BY DESIGN, like the iteration/document/artifact/fault views:
    no live branch, no `live` key, no snapshot notice -- proven by a live
    StubEngineApi that records ZERO requests while the endpoint answers;
  * app.js words nothing itself and renders text nodes only;
  * the browser side lives in tests/test_browser_hub.py
    (`test_run_detail_opens_the_cost_dialog_from_the_cost_cell`).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from ralphd.cli.ui_server import cost_view, run_detail
from ralphd.engine.state import (
    COST_BREAKDOWN_LEGEND,
    COST_NO_USAGE,
    COST_SOURCE_DERIVED,
    COST_SOURCE_PARTIAL,
    COST_SOURCE_UNAVAILABLE,
    COST_UNAVAILABLE,
    COST_ZERO_QUOTE_NOTICE,
    cost_breakdown,
    cost_breakdown_text,
)
from tests.conftest import RALPHCTL

sys.path.insert(0, str(Path(__file__).parent))
from test_cli_cost_breakdown import MIXED_USAGE, ZERO_QUOTE
from test_cli_ui import (
    StubEngineApi,
    UiServer,
    _write_dead_run,
    _write_run_with_api,
    ui,
)

# re-exported so the imported `ui` fixture is not flagged as unused
__all__ = ["StubEngineApi", "UiServer", "ui"]

STATIC_APP_JS = (Path(__file__).resolve().parents[1] / "src" / "ralphd"
                 / "cli" / "web" / "app.js")


def _ctl(registry: Path, *argv: str, timeout: int = 60):
    env = {**os.environ, "RALPHD_REGISTRY": str(registry)}
    return subprocess.run([str(RALPHCTL), *argv], env=env,
                          capture_output=True, text=True, timeout=timeout)


def _seed_spender(registry: Path, run_id: str = "hub-cost", *,
                  usage=..., live=None, **status) -> Path:
    """A registry holding one run dir that spent money. `usage` defaults to the
    mixed fixture (one quoted phase, one derived, one nothing priced); `live` is
    a StubEngineApi when the test wants a reachable container."""
    if usage is ...:
        usage = MIXED_USAGE
    fields = {"state": "succeeded", "verdict": "verified", **status}
    if usage is not None:
        fields["usage"] = usage
    if live is None:
        return _write_dead_run(registry, run_id, **fields)
    return _write_run_with_api(registry, run_id, live, **fields)


# ------------------------------------------------------------ one shaping


def test_cost_view_is_the_shared_breakdown_plus_its_text(tmp_path):
    registry = tmp_path / "registry"
    _seed_spender(registry)

    view = cost_view(registry, "hub-cost")
    bd = cost_breakdown(registry / "runs" / "hub-cost")
    assert view == {"runId": "hub-cost", **bd, "text": cost_breakdown_text(bd)}
    # the shaping already carries the lines; the hub does not re-word them
    assert view["summaryLines"] == bd["summaryLines"]
    assert view["text"] == "\n".join(view["summaryLines"])


def test_cost_view_payload_is_what_ralphctl_cost_json_prints(tmp_path):
    """Byte-for-byte the same document, so `--json` and the dialog's endpoint
    can never drift into two shapes of the same answer."""
    registry = tmp_path / "registry"
    _seed_spender(registry)

    r = _ctl(registry, "--json", "cost", "hub-cost")
    assert r.returncode == 0, r.stderr
    assert json.loads(r.stdout) == cost_view(registry, "hub-cost")


def test_cost_view_text_is_what_ralphctl_cost_prints(tmp_path):
    """One rendering: the dialog body is the CLI's own block, line for line.
    `cmd_cost` prints its own `run:` line first, then exactly this text."""
    registry = tmp_path / "registry"
    _seed_spender(registry)

    r = _ctl(registry, "cost", "hub-cost")
    assert r.returncode == 0, r.stderr
    printed = r.stdout.split("\n", 1)[1]
    assert printed.rstrip("\n") == cost_view(registry, "hub-cost")["text"]


def test_cost_view_text_carries_the_buckets_and_labels_18_5_asked_for(tmp_path):
    registry = tmp_path / "registry"
    _seed_spender(registry)

    view = cost_view(registry, "hub-cost")
    text = view["text"]
    # per-phase and per-approach groups, with the engine's own bucket keys
    assert "by phase:" in text and "by approach:" in text, text
    for phase in ("planning", "worker", "verify"):
        assert phase in text, text
    # approaches sort numerically, not lexically ("10" after "2")
    assert text.index("\n  2 ") < text.index("\n  10 "), text
    # every kind of money is labelled
    assert COST_SOURCE_DERIVED in text, text
    assert COST_UNAVAILABLE in text, text
    assert COST_BREAKDOWN_LEGEND in text, text
    assert set(view["sources"]) >= {COST_SOURCE_DERIVED, COST_SOURCE_PARTIAL,
                                    COST_SOURCE_UNAVAILABLE}
    assert [b["key"] for b in view["byApproach"]] == ["2", "10"]


def test_cost_view_headline_is_the_string_the_card_already_shows(tmp_path, ui):
    """The cell that opens the dialog and the dialog itself must agree: both are
    `format_cost` over the same bucket, applied server-side."""
    registry = tmp_path / "registry"
    _seed_spender(registry)
    server = ui(registry)

    _, detail = server.get("/api/runs/hub-cost")
    card = detail["status"]["usage"]["costDisplay"]
    view = cost_view(registry, "hub-cost")
    assert view["costDisplay"] == card
    assert view["total"]["costDisplay"] == card
    assert f"cost:      {card}" in view["text"], view["text"]


def test_cost_view_says_so_when_a_run_recorded_no_usage(tmp_path):
    """"Nothing was recorded" is an answer (the `NO_FAULT`/absent-document
    discipline) -- so the cell that opens this dialog never has to lie, and a
    run with no usage does not get a table of zeros."""
    registry = tmp_path / "registry"
    _seed_spender(registry, "hub-quiet", usage=None)

    view = cost_view(registry, "hub-quiet")
    assert view["hasUsage"] is False
    assert view["text"] == COST_NO_USAGE
    assert "$0.00" not in view["text"]


def test_cost_view_renders_the_implausible_zero_quote_as_unavailable(tmp_path):
    """Task 049 / #10's rule reaches the dialog: half a million billed tokens
    quoted at $0 is unknown money, and the anomaly is named."""
    registry = tmp_path / "registry"
    _seed_spender(registry, "hub-zero", usage=ZERO_QUOTE)

    view = cost_view(registry, "hub-zero")
    assert view["costDisplay"] == COST_UNAVAILABLE
    assert "$0.00" not in view["text"]
    assert COST_ZERO_QUOTE_NOTICE in view["text"]
    assert "505,628 total" in view["text"]


def test_cost_view_recomputes_a_forged_display_string(tmp_path):
    """A hand-edited status.json cannot make the dialog claim a number its own
    counters do not support (`cost_bucket`'s rule, asserted through the hub)."""
    registry = tmp_path / "registry"
    _seed_spender(registry, "hub-forged",
                  usage={**ZERO_QUOTE, "costDisplay": "$99.9999"})

    view = cost_view(registry, "hub-forged")
    assert "99.9999" not in json.dumps(view)
    assert view["costDisplay"] == COST_UNAVAILABLE


# ------------------------------------------------------------ HTTP surface


def test_hub_endpoint_serves_the_cost_breakdown(tmp_path, ui):
    registry = tmp_path / "registry"
    _seed_spender(registry)
    server = ui(registry)

    code, body = server.get("/api/runs/hub-cost/cost")
    assert code == 200
    assert body == cost_view(registry, "hub-cost")
    assert "by phase:" in body["text"]


def test_hub_cost_endpoint_404s_for_an_unknown_run(tmp_path, ui):
    registry = tmp_path / "registry"
    _seed_spender(registry)
    server = ui(registry)

    code, body = server.get("/api/runs/nope/cost")
    assert code == 404 and "not found" in body["error"]


def test_hub_cost_endpoint_answers_for_a_run_with_no_usage_too(tmp_path, ui):
    registry = tmp_path / "registry"
    _seed_spender(registry, "hub-quiet", usage=None)
    server = ui(registry)

    code, body = server.get("/api/runs/hub-quiet/cost")
    assert code == 200 and body["text"] == COST_NO_USAGE


def test_hub_cost_endpoint_never_touches_the_live_api(tmp_path, ui):
    """On-disk BY DESIGN (the `iteration_view`/`document_list`/`artifact_list`/
    `fault_view` contract): status.json is the engine's own atomic write, so a
    reachable container must not be consulted."""
    engine = StubEngineApi()
    try:
        registry = tmp_path / "registry"
        _seed_spender(registry, live=engine)
        server = ui(registry)

        code, body = server.get("/api/runs/hub-cost/cost")
        assert code == 200
        assert engine.requests == []
        # no live/snapshot vocabulary in the payload at all
        assert "live" not in body
        blob = json.dumps(body)
        assert "snapshot" not in blob and "unreachable" not in blob
        # control: a route that DOES proxy records requests on the same stub
        server.get("/api/runs/hub-cost")
        assert engine.requests != []
    finally:
        engine.close()


def test_two_runs_do_not_borrow_each_others_spend(tmp_path, ui):
    registry = tmp_path / "registry"
    _seed_spender(registry, "run-a")
    _seed_spender(registry, "run-b", usage={
        "totalTokens": 7, "costUSD": 0.25,
        "byPhase": {"reflect": {"totalTokens": 7, "costUSD": 0.25}}})
    server = ui(registry)

    _, a = server.get("/api/runs/run-a/cost")
    _, b = server.get("/api/runs/run-b/cost")
    assert "planning" in a["text"] and "reflect" not in b["text"].split("\n")[0]
    assert [x["key"] for x in b["byPhase"]] == ["reflect"]
    assert b["total"]["tokens"] == 7 and a["total"]["tokens"] == 40000
    assert COST_UNAVAILABLE in a["text"] and COST_UNAVAILABLE not in b["text"]


def test_hub_detail_still_carries_the_run_id_the_cost_cell_needs(tmp_path, ui):
    """app.js only offers the cell when the payload names a run (the
    `faultBadge` rule), so the detail view must keep saying which run it is."""
    registry = tmp_path / "registry"
    _seed_spender(registry)
    server = ui(registry)

    _, detail = server.get("/api/runs/hub-cost")
    assert detail["runId"] == "hub-cost"
    assert run_detail(registry, "hub-cost")["runId"] == "hub-cost"


# ------------------------------------------------------------ app.js guards


def test_app_js_opens_the_cost_dialog_from_the_cost_cell():
    src = STATIC_APP_JS.read_text()
    # the endpoint, and THE single shared dialog with the server's own `text`
    assert "/cost`" in src
    assert "openCostDialog" in src
    assert "openTextDialog(costTitle(runId), text, null)" in src
    # the affordance wraps the card's own value, it does not replace it
    assert 'statCard("cost", costText, (v) => costCell(runId, v))' in src
    assert '"data-cost-cell": "total"' in src


def test_app_js_words_no_cost_fact_of_its_own():
    """Every string the dialog shows was formatted in Python (task 027's
    wording): app.js must not respell any of it.

    The single English words `derived`/`unavailable` are deliberately NOT
    forbidden -- the cell's hover promise says what the dialog will explain, the
    way the fault badge's title names its four facts -- but no rendered VERDICT
    (`provider-priced`, `declared free`, `no traffic`), notice, legend or label
    may be spelled here, and the dialog's own region may not format money.
    """
    from ralphd.engine import state as st

    src = STATIC_APP_JS.read_text()
    for wording in (COST_NO_USAGE, COST_BREAKDOWN_LEGEND, COST_ZERO_QUOTE_NOTICE,
                    st.COST_SOURCE_PROVIDER, st.COST_SOURCE_FREE,
                    st.COST_SOURCE_NO_TRAFFIC):
        assert wording not in src, wording
    for label in ("by phase:", "by approach:", "tokens:", "source:"):
        assert label not in src, label
    region = src[src.index("const COST_LOAD_FAILED"):src.index("function costCell")]
    assert "toFixed" not in region


def test_app_js_renders_the_cost_dialog_as_text_nodes_only():
    src = STATIC_APP_JS.read_text()
    region = src[src.index("const COST_LOAD_FAILED"):
                 src.index("// --------------------------------------------------- "
                           "steering history (#17)")]
    assert "innerHTML" not in region
    assert "html" not in region.replace("openCostDialog", "")
