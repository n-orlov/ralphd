"""#295 (PRD F): the hub header's hat -> model mapping is a table whose cells
are worded server-side (`ui_server.provisioning_table_cells`) and only placed
by app.js (`provisioningTableNode`, inside the remembered `<details>`)."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from ralphd.cli.ui_server import provisioning_table_cells
from ralphd.engine.config import MODEL_HATS, JobConfig

pytestmark = pytest.mark.fast

APP_JS = (Path(__file__).resolve().parents[1]
          / "src" / "ralphd" / "cli" / "web" / "app.js")
WORKER = "anthropic/claude-sonnet-5"
HARD = "anthropic/claude-opus-5"


def _tiered() -> JobConfig:
    return JobConfig(
        model_overrides={"worker": WORKER},
        harnesses={"worker": "claude-code"},
        model_tiers={"worker": {"hard": {"model": HARD, "thinking": "high"}}})


def test_tiered_worker_yields_tier_columns_with_rung_models():
    cells = provisioning_table_cells(_tiered())
    assert cells["columns"] == ["hat", "model", "thinking/effort", "harness",
                                "trivial", "normal", "hard"]
    row = next(r for r in cells["rows"] if r[0] == "worker")
    by_col = dict(zip(cells["columns"], row))
    assert by_col["model"] == WORKER
    assert by_col["trivial"] == WORKER
    assert by_col["normal"] == WORKER
    assert HARD in by_col["hard"]
    assert "thinking: high" in by_col["hard"]
    assert "thinking" not in by_col["normal"]
    other = next(r for r in cells["rows"] if r[0] == "planning")
    assert other[4:] == ["", "", ""]
    assert all(len(r) == len(cells["columns"]) for r in cells["rows"])


def test_untiered_config_yields_no_tier_columns():
    cells = provisioning_table_cells(JobConfig(model_overrides={"worker": WORKER}))
    assert cells["columns"] == ["hat", "model", "thinking/effort", "harness"]
    assert [r[0] for r in cells["rows"]] == list(MODEL_HATS)
    assert all(len(r) == 4 for r in cells["rows"])
    assert next(r for r in cells["rows"] if r[0] == "worker")[1] == WORKER


@pytest.mark.parametrize("cfg", [_tiered(), JobConfig(
    harnesses={"review": "claude-code"})])
def test_harness_column_equals_harness_for_every_row(cfg):
    cells = provisioning_table_cells(cfg)
    assert [r[0] for r in cells["rows"]] == list(MODEL_HATS)
    for row in cells["rows"]:
        assert row[3] == cfg.harness_for(row[0])


_HARNESS = r"""
const fs = require('fs');
const src = fs.readFileSync(process.argv[1], 'utf8');
const input = JSON.parse(process.argv[2]);
function grab(name) {
  const a = src.indexOf('function ' + name + '(');
  if (a < 0) throw Error(name + ' not found');
  const b = src.indexOf('\n}\n', a);
  return src.slice(a, b + 3);
}
class El {
  constructor(tag) { this.tag = tag; this.children = []; this.attributes = {};
    this.listeners = {}; this.className = ''; this.open = false; }
  setAttribute(k, v) { this.attributes[k] = String(v); if (k === 'open') this.open = true; }
  addEventListener(t, fn) { (this.listeners[t] = this.listeners[t] || []).push(fn); }
  appendChild(c) { this.children.push(c); return c; }
  set innerHTML(v) { this.children = []; }
}
const document = { createElement: (t) => new El(t), createTextNode: (t) => ({ text: String(t) }) };
function h(tag, attrs, children) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (k === 'class') el.className = v; else el.setAttribute(k, v);
  }
  for (const c of children || []) {
    if (c == null) continue;
    el.appendChild(typeof c === 'string' ? document.createTextNode(c) : c);
  }
  return el;
}
const window = { localStorage: { getItem: () => input.pref } };
const COLLAPSE_STORAGE_PREFIX = 'ralphd.collapse.';
const noop = () => null;
const stub = () => document.createElement('span');
const code = grab('readCollapsePref') + grab('wireCollapsePersistence') +
  grab('provisioningTableNode') + grab('renderSummary') +
  '; return renderSummary;';
const renderSummary = new Function('h', 'window', 'document', 'COLLAPSE_STORAGE_PREFIX',
  'setTimeout', 'approachText', 'durationBetween', 'fmtDuration', 'pill', 'stateCell',
  'renderContainerGone', 'renderInfraWait', 'renderProvisionalVerdict',
  'renderRateLimitWait', 'renderUnpause', 'renderReflect', code)(
  h, window, document, COLLAPSE_STORAGE_PREFIX, noop, () => '', () => '', () => '',
  stub, stub, noop, noop, noop, noop, noop, noop);
const el = new El('div');
renderSummary(el, { status: input.status || {}, runId: 'r' });
function walk(n, out) { out.push(n); for (const c of n.children || []) walk(c, out); return out; }
function text(n) { return n.text !== undefined ? n.text : (n.children || []).map(text).join(''); }
const all = walk(el, []).filter((n) => n.tag);
const details = all.find((n) => n.className === 'provisioning');
const out = { hasDetails: !!details, open: details ? details.open : null,
  tables: details ? walk(details, []).filter((n) => n.tag === 'table').length : 0,
  ths: details ? walk(details, []).filter((n) => n.tag === 'th').map(text) : [],
  rows: details ? walk(details, []).filter((n) => n.tag === 'tr').slice(1)
    .map((tr) => tr.children.map(text)) : [] };
console.log(JSON.stringify(out));
"""


def _render(status: dict, pref=None) -> dict:
    res = subprocess.run(
        ["node", "-e", _HARNESS, str(APP_JS),
         json.dumps({"status": status, "pref": pref})],
        capture_output=True, text=True, timeout=30)
    assert res.returncode == 0, res.stderr
    return json.loads(res.stdout.strip().splitlines()[-1])


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_app_js_renders_a_table_inside_the_provisioning_details():
    cells = provisioning_table_cells(_tiered())
    out = _render({"provisioningTable": cells})
    assert out["hasDetails"] and out["tables"] == 1
    assert out["ths"] == cells["columns"]
    assert out["rows"] == cells["rows"]
    assert out["open"] is False


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_app_js_provisioning_table_keeps_the_remembered_open_state():
    cells = provisioning_table_cells(_tiered())
    assert _render({"provisioningTable": cells}, pref="open")["open"] is True
    assert _render({"provisioningTable": cells}, pref="closed")["open"] is False


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_app_js_places_no_provisioning_details_without_rows():
    out = _render({"provisioningTable": {"columns": [], "rows": []}})
    assert out["hasDetails"] is False
