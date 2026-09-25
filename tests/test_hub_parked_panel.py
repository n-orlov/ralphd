"""Task 008 (v0.26, #269): render task 007's `parkedQuestion` payload as a
full-width panel at the TOP of the hub run page.

At 9b72b700 `app.js` has no `renderParkedPanel`, `parkedFindingItem`,
`parkedConsequenceLines` or `parkedReasonText` at all (task 007's own
`parkedQuestion` payload does not exist there either -- see
tests/test_hub_parked_payload.py), and `stateMarkerSpans` takes only two
arguments with no `runId`/link-wrapping -- every node below fails against
that checkout, either because the named function is missing (a node-eval
`grab()` throws) or because the marker never becomes a link.

Browser-free, same technique `tests/test_escalation_undelivered_marker.py`
and `tests/test_hub_infra_wait_timed_out.py` already use: the REAL functions
are extracted verbatim out of the served `app.js` (never a Python
reimplementation of the rendering rule) and evaluated under Node against a
minimal object-tree stand-in for the DOM (`h(tag, attrs, children) =>
{tag, attrs, children}`), the same stand-in those two modules already use.
`app.js` keeps every helper below self-contained (no shared top-level
`const`), on purpose: a `const` declared inside a direct `eval()` call is
scoped to that call and would never reach the enclosing script the way
`var`/`function` do (the ECMAScript direct-eval rules), and this module
plus `test_escalation_undelivered_marker.py` each grab only the ONE
function they need.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")

sys.path.insert(0, str(Path(__file__).parent))
from test_cli_ui import StubEngineApi, UiServer, _get_raw, _write_run_with_api, ui  # noqa: E402
from test_hub_parked_payload import (  # noqa: E402
    _CURE_REOPEN_FINDING,
    _RESIDUAL,
    _supervisor,
)

from ralphd.cli import ui_server  # noqa: E402
from ralphd.engine.state import parked_question  # noqa: E402

__all__ = ["UiServer", "ui"]

APP_JS = (Path(ui_server.__file__).parent / "web" / "app.js")
STYLE_CSS = (Path(ui_server.__file__).parent / "web" / "style.css")


# ---------------------------------------------------------------------------
# node-eval harness
# ---------------------------------------------------------------------------

_PANEL_JS = r"""
const fs = require('fs');
const src = fs.readFileSync(process.argv[1], 'utf8');
function grab(name) {
  const start = src.indexOf('function ' + name + '(');
  if (start < 0) throw Error('missing function ' + name);
  const end = src.indexOf('\n}\n', start);
  return src.slice(start, end + 3);
}
function text(node) {
  if (node == null) return '';
  if (typeof node === 'string') return node;
  return (node.children || []).map(text).join('');
}
function classes(node, out) {
  if (node == null || typeof node === 'string') return out;
  if (node.attrs && node.attrs.class) out.push(node.attrs.class);
  for (const c of node.children || []) classes(c, out);
  return out;
}
function findAll(node, tag, out) {
  if (node == null || typeof node === 'string') return out;
  if (node.tag === tag) out.push(node);
  for (const c of node.children || []) findAll(c, tag, out);
  return out;
}
const h = (tag, attrs, children) => ({ tag, attrs: attrs || {}, children: children || [] });
eval(grab('parkedReasonText'));
eval(grab('parkedNextStepOutcomeText'));
eval(grab('parkedConsequenceLines'));
eval(grab('parkedWaiveLineText'));
eval(grab('applyWaiveLine'));
eval(grab('parkedFindingItem'));
eval(grab('fmtDuration'));
eval(grab('isoToEpoch'));
eval(grab('durationBetween'));
eval(grab('renderParkedPanel'));

function makeBox() {
  return { children: [], style: {}, innerHTML: '',
    appendChild(n) { this.children.push(n); } };
}
const input = JSON.parse(process.argv[2]);
const box = makeBox();
renderParkedPanel(box, input.runId, input.pq);
const wholeText = box.children.map(text).join('');
const evidencePres = box.children
  .map((c) => findAll(c, 'pre', [])).flat();
console.log(JSON.stringify({
  displayNone: box.style.display === 'none',
  childCount: box.children.length,
  text: wholeText,
  classes: box.children.map((c) => classes(c, [])).flat(),
  detailsCount: box.children.map((c) => findAll(c, 'details', [])).flat().length,
  evidenceTexts: evidencePres.map(text),
}));
"""


def _render_panel(pq, run_id: str = "run-x") -> dict:
    result = subprocess.run(
        ["node", "-e", _PANEL_JS, str(APP_JS), json.dumps({"runId": run_id, "pq": pq})],
        capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


_MARKER_JS = r"""
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
function findAll(node, tag, out) {
  if (typeof node === 'string') return out;
  if (node.tag === tag) out.push(node);
  for (const c of node.children || []) findAll(c, tag, out);
  return out;
}
const h = (tag, attrs, children) => ({ tag, attrs: attrs || {}, children: children || [] });
const FAULT_BADGE_STATES = new Set(["failed", "aborted"]);
const faultBadge = () => null;
eval(grab('pill') + grab('stateMarkerSpans') + grab('runRowStateChildren') + grab('stateCell'));
const input = JSON.parse(process.argv[2]);
const node = input.surface === 'row'
  ? h('td', {}, runRowStateChildren(input.doc))
  : stateCell({ runId: input.doc.runId || 'x' }, input.doc);
const anchors = findAll(node, 'a', []);
console.log(JSON.stringify({ text: text(node), anchors: anchors.map((a) => a.attrs) }));
"""


def _render_marker(surface: str, doc: dict) -> dict:
    result = subprocess.run(
        ["node", "-e", _MARKER_JS, str(APP_JS), json.dumps({"surface": surface, "doc": doc})],
        capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


# ---------------------------------------------------------------------------
# criterion (1): every field, every finding, expandable, evidence unclipped
# ---------------------------------------------------------------------------

_PQ = {
    "reason": "operator_input_needed",
    "approach": 2,
    "maxApproaches": 3,
    "approachOfMax": "approach 2 of 3",
    "parkedAt": "2020-01-01T00:00:00Z",
    "replanAvailable": True,
    "blockingCount": 2,
    "residualCount": 1,
    "findings": [
        {"id": "F1", "severity": "blocking", "clause": "clause one (#111)",
         "evidence": "evidence one", "cured_when": "clause one is discharged",
         "invalidates": ["docs/api.md"]},
        {"id": "F2", "severity": "blocking", "clause": "clause two (#112)",
         "evidence": "evidence two", "cured_when": None, "invalidates": []},
        {"id": "R1", "severity": "residual", "clause": "residual clause (#113)",
         "evidence": "residual evidence", "cured_when": None, "invalidates": []},
    ],
    "consequence": {
        "taskIdsValidatedToPending": ["009"],
        "insertCount": 1,
        "costSpentThisApproach": "$1.23",
        "tokensSpentThisApproach": 4567,
        "nextStepIfUnanswered": "cure_pass",
    },
}


def test_panel_shows_reason_approach_time_parked_replan_and_counts():
    out = _render_panel(_PQ)
    assert out["displayNone"] is False
    assert out["childCount"] == 1
    text = out["text"]
    assert "operator input needed" in text
    assert "approach 2 of 3" in text
    assert "2020-01-01T00:00:00Z" in text
    assert "yes" in text  # replanAvailable
    assert "2" in text and "blocking findings: 2" in text
    assert "residual findings: 1" in text


def test_panel_shows_the_consequence_of_not_answering():
    out = _render_panel(_PQ)
    text = out["text"]
    assert "cure pass" in text
    assert "009" in text  # taskIdsValidatedToPending
    assert "1" in text  # insertCount
    assert "$1.23" in text
    assert "4567" in text


def test_panel_shows_archive_and_replan_consequence_wording():
    pq = dict(_PQ, consequence=dict(_PQ["consequence"],
                                     nextStepIfUnanswered="archive_and_replan"))
    out = _render_panel(pq)
    assert "archive" in out["text"] and "replan" in out["text"]


def test_replan_available_renders_na_when_absent_for_repeated_signature():
    pq = dict(_PQ, reason="rejected_repeated_signature", replanAvailable=None)
    out = _render_panel(pq)
    assert "rejected" in out["text"] and "repeated" in out["text"]
    assert "n/a" in out["text"]


def test_every_finding_blocking_and_residual_is_listed_by_id_severity_clause():
    out = _render_panel(_PQ)
    assert out["detailsCount"] == 3  # one expandable <details> per finding
    for f in _PQ["findings"]:
        assert f["id"] in out["text"]
        assert f["severity"] in out["text"]
        assert f["clause"] in out["text"]


def test_each_finding_expands_to_its_evidence_cured_when_and_invalidates():
    out = _render_panel(_PQ)
    assert out["evidenceTexts"] == ["evidence one", "evidence two", "residual evidence"]
    assert "clause one is discharged" in out["text"]
    assert "docs/api.md" in out["text"]


def test_long_evidence_is_carried_whole_never_truncated():
    long_evidence = "x" * 50_000 + " END-OF-EVIDENCE"
    pq = json.loads(json.dumps(_PQ))
    pq["findings"][0]["evidence"] = long_evidence
    out = _render_panel(pq)
    assert out["evidenceTexts"][0] == long_evidence
    assert "END-OF-EVIDENCE" in out["text"]
    assert "\u2026" not in out["evidenceTexts"][0]  # no ellipsis clipping


def test_the_evidence_block_scrolls_in_css_rather_than_clipping():
    css = STYLE_CSS.read_text()
    start = css.index(".parked-finding-evidence {")
    block = css[start:css.index("}", start) + 1]
    assert "overflow" in block
    assert "max-height" in block
    assert "text-overflow" not in block
    assert "ellipsis" not in block


# ---------------------------------------------------------------------------
# criterion (2): absent when there is no parked question; Steer/Rule stay
# their own sections below it
# ---------------------------------------------------------------------------


def test_panel_is_not_rendered_when_there_is_no_parked_question():
    out = _render_panel(None)
    assert out["displayNone"] is True
    assert out["childCount"] == 0
    assert out["text"] == ""


def test_the_panel_sits_above_steer_and_rule_which_stay_their_own_sections():
    src = APP_JS.read_text()
    i_panel = src.index('id: "parked-panel-box"')
    i_steer_h2 = src.index('h("h2", {}, ["Steer"])')
    i_steer_form = src.index('id: "steer-form"')
    i_rule_h2 = src.index('h("h2", {}, ["Rule"])')
    i_rule_form = src.index('id: "rule-form"')
    assert i_panel < i_steer_h2 < i_steer_form
    assert i_steer_form < i_rule_h2 < i_rule_form
    # Both are still rendered as their OWN <section> elements, not folded
    # into the panel.
    assert re.search(r'h\("section", \{\}, \[\s*h\("h2", \{\}, \["Steer"\]\)',
                      src)
    assert re.search(r'h\("section", \{\}, \[\s*h\("h2", \{\}, \["Rule"\]\)',
                      src)


def test_load_wires_the_real_payloads_parkedQuestion_into_the_panel_box():
    """`renderRunDetail`'s own `load()` must feed `body.parkedQuestion` (task
    007's field) into `renderParkedPanel` against the SAME persistent box --
    pinned at the source level so a future edit that renders the panel from
    some other field, or against a throwaway element, is caught here."""
    src = APP_JS.read_text()
    start = src.index("async function renderRunDetail(")
    end = src.index("\n// Task 031 (#19): the run-detail page's delete control")
    body = src[start:end]
    assert 'const parkedBox = h("div", { id: "parked-panel-box"' in body
    assert re.search(
        r'renderParkedPanel\(document\.getElementById\("parked-panel-box"\),\s*'
        r'runId,\s*\n?\s*body\.parkedQuestion\)', body)


# ---------------------------------------------------------------------------
# criterion (3): the runs-list marker links to the panel
# ---------------------------------------------------------------------------


def test_the_runs_list_parked_marker_is_wrapped_in_a_link_to_the_run_page():
    row = {"runId": "run-abc", "state": "paused",
           "pauseMarker": "\u23F8 parked for input",
           "escalationUndeliveredMarker": ""}
    out = _render_marker("row", row)
    assert row["pauseMarker"] in out["text"]
    assert out["anchors"] == [
        {"href": "#/run/run-abc", "class": "parked-panel-link"}]


def test_the_run_detail_pages_own_marker_links_to_the_panel_too():
    doc = {"runId": "run-abc", "state": "paused",
           "pauseMarker": "\u23F8 parked for input",
           "escalationUndeliveredMarker": ""}
    out = _render_marker("detail", doc)
    assert out["anchors"] == [
        {"href": "#/run/run-abc", "class": "parked-panel-link"}]


def test_only_the_parked_for_input_marker_text_becomes_a_link():
    """The escalation-hold marker and the undelivered marker are plain
    text -- neither answers to a panel of its own, so wrapping either would
    be a link to nowhere."""
    held = {"runId": "run-abc", "state": "running",
            "pauseMarker": "\u23F8 held on rejected_incurable escalation",
            "escalationUndeliveredMarker": ""}
    out = _render_marker("row", held)
    assert held["pauseMarker"] in out["text"]
    assert out["anchors"] == []

    undelivered = {"runId": "run-abc", "state": "paused",
                   "pauseMarker": "\u23F8 parked for input",
                   "escalationUndeliveredMarker": "\u2757 escalation undelivered"}
    out2 = _render_marker("row", undelivered)
    assert undelivered["escalationUndeliveredMarker"] in out2["text"]
    assert out2["anchors"] == [
        {"href": "#/run/run-abc", "class": "parked-panel-link"}]


def test_the_real_server_shaped_row_gets_the_same_link(tmp_path):
    """Ties the two sides together: a run row shaped by the REAL
    `ui_server.run_list` (never a hand-built fixture) still gets its
    `pauseMarker` linked by the REAL `runRowStateChildren`."""
    registry = tmp_path / "reg"
    run_dir = registry / "runs" / "parked"
    run_dir.mkdir(parents=True)
    (run_dir / "status.json").write_text(json.dumps({
        "runId": "parked", "state": "paused", "verdict": None,
        "phase": "review", "approach": 1, "maxApproaches": 3,
        "iterationsUsed": 3, "iterationsBudget": 25,
        "startedAt": "2026-01-01T00:00:00Z",
        "pauseReason": "operator_input_needed"}))
    row = ui_server.run_list(registry)[0]
    out = _render_marker("row", row)
    assert out["anchors"] == [
        {"href": f"#/run/{row['runId']}", "class": "parked-panel-link"}]


# ---------------------------------------------------------------------------
# real backend round-trip: task 007's own `parked_question` output, fed
# straight into the real renderer, never a hand-typed stand-in for its shape
# ---------------------------------------------------------------------------


def test_a_real_parked_question_from_the_engine_renders_cleanly(tmp_path):
    root = tmp_path / "run"
    sup = _supervisor(root, max_approaches=2, operator_input_needed_pause=True,
                      status={"state": "running", "phase": "review",
                              "approach": 1, "maxApproaches": 2,
                              "usage": {"byApproach": {"1": {
                                  "costUSD": 0.42, "totalTokens": 321}}}})
    sup._approach = 1
    from ralphd.engine import findings as findings_mod
    findings_mod.write_findings_json(sup.run.findings_json_file,
                                     _CURE_REOPEN_FINDING + [_RESIDUAL])
    from ralphd.engine.state import cure_pass_preview
    preview = cure_pass_preview([], _CURE_REOPEN_FINDING)
    sup._pause_and_escalate_operator_input_needed(
        1, _CURE_REOPEN_FINDING, replan_available=True, cure_preview=preview)

    status = sup.run.read_status()
    pq = parked_question(root, status)
    assert pq is not None

    out = _render_panel(pq, run_id="real-run")
    assert out["displayNone"] is False
    assert out["detailsCount"] == len(pq["findings"]) == 2
    assert "approach 1 of 2" in out["text"]
    assert "F1" in out["text"] and "R1" in out["text"]
    assert pq["findings"][0]["evidence"] in out["evidenceTexts"]


# ---------------------------------------------------------------------------
# criterion (5): no new frontend dependency -- app.js stays on its own h()
# ---------------------------------------------------------------------------


def test_the_panel_uses_only_the_shared_h_helper_no_new_dependency():
    src = APP_JS.read_text()
    start = src.index("function renderParkedPanel(")
    end = src.index("\n}\n", start) + 3
    body = src[start:end]
    # every element the panel builds goes through `h(...)`; no template
    # library, no innerHTML string-building beyond the reset itself.
    assert body.count("h(") >= 5
    assert "innerHTML = ``" not in body
    assert "innerHTML = `<" not in body


def test_the_served_bundle_is_byte_identical_to_the_source(tmp_path, ui):
    """Same discipline `test_hub_rule.py` pins: the panel really ships in
    what the hub SERVES, not only in the source tree."""
    registry = tmp_path / "registry"
    engine = StubEngineApi()
    try:
        _write_run_with_api(registry, "run-parked-bundle", engine, state="running")
        server = ui(registry)
        code, _ctype, body = _get_raw(server, "/app.js")
    finally:
        engine.close()
    assert code == 200, code
    served = body.decode()
    assert served == APP_JS.read_text()
    assert "function renderParkedPanel(" in served
    assert "parked-panel-link" in served
