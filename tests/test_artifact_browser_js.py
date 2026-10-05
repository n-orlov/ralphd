"""#294 (E.5-E.6): the run page's artifacts section is a summary plus a browse
modal, and the poll no longer fetches the artifact list.

`app.js`'s artifacts region is evaluated under Node (the same technique
`tests/test_hub_parked_panel.py` uses) against a minimal DOM stand-in whose
`getJSON` records every fetch, so the "one fetch on open" and "the poll never
lists" rules are proven on the REAL functions, not on source text alone.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(shutil.which("node") is None,
                                reason="node not installed")

APP_JS = (Path(__file__).resolve().parents[1]
          / "src" / "ralphd" / "cli" / "web" / "app.js")

_HARNESS = r"""
const fs = require('fs');
const src = fs.readFileSync(process.argv[1], 'utf8');
const input = JSON.parse(process.argv[2]);
const a = src.indexOf('const ARTIFACT_LOAD_FAILED');
const b = src.indexOf('// ------------------------------------------------------ router calls');
if (a < 0 || b < 0) throw Error('artifacts region not found');
class El {
  constructor(tag) { this.tag = tag; this.children = []; this.attributes = {};
    this.listeners = {}; this.dataset = {}; this.className = ''; this.value = '';
    this.disabled = false; }
  setAttribute(k, v) { this.attributes[k] = String(v); }
  addEventListener(t, fn) { (this.listeners[t] = this.listeners[t] || []).push(fn); }
  appendChild(c) { this.children.push(c); return c; }
  set innerHTML(v) { this.children = []; }
  get innerHTML() { return ''; }
  click() { for (const f of this.listeners.click || []) f({}); }
  submit() { for (const f of this.listeners.submit || []) f({ preventDefault() {} }); }
}
const document = {
  createElement: (t) => new El(t),
  createTextNode: (t) => ({ text: String(t) }),
  getElementById: (id) => (input.boxes || {})[id] || null,
};
function h(tag, attrs, children) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (k === 'class') el.className = v;
    else if (k.startsWith('on') && typeof v === 'function') el.addEventListener(k.slice(2), v);
    else el.setAttribute(k, v);
  }
  for (const c of children || []) {
    if (c == null) continue;
    el.appendChild(typeof c === 'string' ? document.createTextNode(c) : c);
  }
  return el;
}
const calls = [];
const getJSON = async (path) => {
  calls.push(path);
  const r = input.responses.find((x) => path.includes(x.match));
  return r ? { ok: true, status: 200, body: r.body } : { ok: false, status: 404, body: {} };
};
const hubURL = (p) => p;
const opened = [];
const showDialog = (d) => { opened.push(d); return d; };
const openTextDialog = (t, text) => { opened.push({ text }); };
const api = new Function('h', 'getJSON', 'hubURL', 'showDialog', 'openTextDialog', 'document',
  src.slice(a, b) +
  '; return { renderArtifacts, loadArtifacts, openArtifactBrowser, artifactPreviewNodes };')(
  h, getJSON, hubURL, showDialog, openTextDialog, document);
function walk(n, out) { out.push(n); for (const c of n.children || []) walk(c, out); return out; }
function text(n) { return n.text !== undefined ? n.text : (n.children || []).map(text).join(''); }
const find = (root, pred) => walk(root, []).filter((n) => n.tag && pred(n));
const byClass = (root, cls) => find(root, (n) => n.className.split(' ').includes(cls));
module.exports = { api, calls, opened, find, byClass, text, El, document, h };
"""


def _run(scenario: str, **input_) -> dict:
    js = _HARNESS + "\n(async () => {\n" + scenario + "\n})().catch((e) => {" \
        " console.error(e); process.exit(1); });"
    # the harness is the script: `require` the helpers into the scenario
    js = js.replace("module.exports = { api, calls, opened, find, byClass, text, El, document, h };", "")
    result = subprocess.run(["node", "-e", js, str(APP_JS),
                             json.dumps(input_)],
                            capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


_PAGE = {"runId": "r", "total": 3, "offset": 0, "limit": 200, "notice": "",
         "entries": [
             {"path": "logs", "name": "logs", "kind": "dir", "size": 0,
              "fileCount": 2, "sizeDisplay": "2 files"},
             {"path": "a.md", "name": "a.md", "kind": "markdown", "size": 5,
              "sizeDisplay": "5 B", "key": None, "title": ""},
             {"path": "p.html", "name": "p.html", "kind": "html", "size": 9,
              "sizeDisplay": "9 B", "key": None, "title": ""}]}


def test_opening_the_modal_fetches_one_page_of_one_directory_and_nothing_else():
    out = _run("""
      const dlg = await api.openArtifactBrowser('r');
      console.log(JSON.stringify({calls, rows: byClass(dlg, 'artifact-row').length,
        downloads: byClass(dlg, 'artifact-download').map((a) => a.attributes.href)}));
    """, responses=[{"match": "/artifacts?", "body": _PAGE}])
    assert len(out["calls"]) == 1, out["calls"]
    call = out["calls"][0]
    assert call.startswith("/api/runs/r/artifacts?")
    assert "limit=200" in call and "offset=0" in call and "top=1" in call
    # a directory row and two file rows; every FILE row has a download link
    assert out["rows"] == 3
    assert out["downloads"] == [
        "/api/runs/r/artifacts-download/a.md",
        "/api/runs/r/artifacts-download/p.html"]


def test_the_modal_navigates_a_directory_searches_with_q_and_pages_past_limit():
    out = _run("""
      const dlg = await api.openArtifactBrowser('r');
      const seen = () => calls[calls.length - 1];
      byClass(dlg, 'artifact-row-open').find((b) => b.attributes['data-dir'] === 'logs').click();
      await new Promise((r) => setImmediate(r));
      const afterDir = seen();
      const crumbs = byClass(dlg, 'artifact-crumb').map((b) => b.attributes['data-crumb']);
      find(dlg, (n) => n.tag === 'input')[0].value = ' Rep ';
      find(dlg, (n) => n.tag === 'form' && n.className === 'artifact-search-form')[0].submit();
      await new Promise((r) => setImmediate(r));
      const afterSearch = seen();
      byClass(dlg, 'artifact-next')[0].click();
      await new Promise((r) => setImmediate(r));
      console.log(JSON.stringify({afterDir, crumbs, afterSearch, afterNext: seen(),
        nextDisabled: byClass(dlg, 'artifact-next')[0].disabled}));
    """, responses=[{"match": "/artifacts?",
                     "body": {**_PAGE, "total": 450, "limit": 200}}])
    assert "dir=logs" in out["afterDir"] and "top=1" not in out["afterDir"]
    assert out["crumbs"] == ["", "logs"]
    assert "q=Rep" in out["afterSearch"] and "dir=logs" in out["afterSearch"]
    assert "top=1" not in out["afterSearch"]
    assert "offset=200" in out["afterNext"]


def test_a_file_preview_is_one_fetch_of_that_file_with_text_and_a_truncation_notice():
    view = {"kind": "text", "content": "hello <b>x</b>", "truncated": True,
            "truncationNotice": "(preview truncated: the first 1.0 MiB of 3.0 MiB)",
            "text": "ignored"}
    out = _run("""
      const dlg = await api.openArtifactBrowser('r');
      const before = calls.length;
      byClass(dlg, 'artifact-row-open').find((b) => b.attributes['data-file'] === 'a.md').click();
      await new Promise((r) => setImmediate(r));
      const pre = byClass(dlg, 'artifact-preview-text')[0];
      console.log(JSON.stringify({fetches: calls.slice(before),
        pre: pre ? text(pre) : null,
        notice: byClass(dlg, 'artifact-truncated').map(text),
        iframes: find(dlg, (n) => n.tag === 'iframe').length}));
    """, responses=[{"match": "/artifacts/a.md", "body": view},
                    {"match": "/artifacts?", "body": _PAGE}])
    assert out["fetches"] == ["/api/runs/r/artifacts/a.md"]
    assert out["pre"] == "hello <b>x</b>"          # a text node, never markup
    assert out["notice"] == [view["truncationNotice"]]
    assert out["iframes"] == 0


def test_an_html_preview_is_a_sandboxed_iframe_without_scripts_or_same_origin():
    view = {"kind": "text", "content": "<script>alert(1)</script><p>hi</p>",
            "truncated": False, "text": "x"}
    out = _run("""
      const dlg = await api.openArtifactBrowser('r');
      byClass(dlg, 'artifact-row-open').find((b) => b.attributes['data-file'] === 'p.html').click();
      await new Promise((r) => setImmediate(r));
      const fr = find(dlg, (n) => n.tag === 'iframe');
      console.log(JSON.stringify({frames: fr.map((f) => f.attributes),
        pres: byClass(dlg, 'artifact-preview-text').length}));
    """, responses=[{"match": "/artifacts/p.html", "body": view},
                    {"match": "/artifacts?", "body": _PAGE}])
    assert len(out["frames"]) == 1 and out["pres"] == 0
    attrs = out["frames"][0]
    assert "sandbox" in attrs
    assert "allow-scripts" not in attrs["sandbox"]
    assert "allow-same-origin" not in attrs["sandbox"]
    assert attrs["srcdoc"] == view["content"]


def test_an_image_preview_is_inline_from_the_raw_route_and_binary_says_so():
    out = _run("""
      const nodes = (kind) => api.artifactPreviewNodes('r', {path: 'd/i.png', kind: 'image'},
        {kind, text: '(binary file)'});
      const img = nodes('image');
      const bin = nodes('binary');
      console.log(JSON.stringify({img: img.map((n) => [n.tag, n.attributes.src]),
        bin: bin.map((n) => [n.tag, text(n)])}));
    """, responses=[])
    assert out["img"] == [["img", "/api/runs/r/artifacts-raw/d%2Fi.png"]]
    assert out["bin"] == [["pre", "(binary file)"]]


def test_the_summary_section_shows_count_alias_links_and_a_browse_button():
    body = {"count": 7, "capped": False, "notice": "", "aliases": [
        {"key": "report", "path": "reflection/report.md", "title": "t",
         "size": 4, "sizeDisplay": "4 B"}]}
    out = _run("""
      const box = new El('div');
      api.renderArtifacts(box, true, input.body, 'r');
      const before = calls.length;
      byClass(box, 'browse-artifacts')[0].click();
      await new Promise((r) => setImmediate(r));
      console.log(JSON.stringify({count: text(byClass(box, 'artifact-count')[0]),
        links: byClass(box, 'artifact-item').map((b) => b.attributes['data-artifact-key']),
        browse: byClass(box, 'browse-artifacts').map(text),
        lists: calls.slice(before).length,
        inlineRows: byClass(box, 'artifact-row').length}));
    """, body=body, responses=[{"match": "/artifacts?", "body": _PAGE}])
    assert out["count"] == "artifacts (7)"
    assert out["links"] == ["report"]
    assert out["browse"] == ["Browse artifacts"]
    assert out["lists"] == 1 and out["inlineRows"] == 0


def test_the_run_detail_poll_fetches_the_summary_never_the_artifact_list():
    """Fails on main at 17707ecb, where `loadArtifacts` fetched
    `/api/runs/<id>/artifacts` (the whole walked-and-sniffed list) every poll."""
    out = _run("""
      const box = new El('div');
      input.boxes = {'artifacts-box': box};
      document.getElementById = (id) => id === 'artifacts-box' ? box : null;
      await api.loadArtifacts('r');
      await api.loadArtifacts('r');
      console.log(JSON.stringify({calls}));
    """, responses=[{"match": "/artifacts-summary",
                     "body": {"count": 0, "capped": False, "aliases": [],
                              "notice": "(no artifacts)"}}])
    assert out["calls"] == ["/api/runs/r/artifacts-summary"] * 2


def test_a_root_search_is_a_flat_search_of_the_whole_tree():
    out = _run("""
      const dlg = await api.openArtifactBrowser('r');
      find(dlg, (n) => n.tag === 'input')[0].value = 'zz';
      find(dlg, (n) => n.tag === 'form' && n.className === 'artifact-search-form')[0].submit();
      await new Promise((r) => setImmediate(r));
      console.log(JSON.stringify({last: calls[calls.length - 1]}));
    """, responses=[{"match": "/artifacts?", "body": _PAGE}])
    assert "q=zz" in out["last"] and "top=1" not in out["last"]
    assert "dir=" not in out["last"]
