"""v0.36 requirement A.4: the hub shows an **Unpause** button on a `paused`
run, and the button posts to the existing `POST /api/runs/<id>/unpause`.

Browser-free, the same technique `tests/test_hub_parked_panel.py` uses: the
REAL `renderUnpause` is extracted verbatim out of the served `app.js` and
evaluated under Node against a minimal object-tree stand-in for the DOM, with
a recording `fetch`. The click handler the function registers is invoked and
the request it makes is read back.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from ralphd.cli import ui_server

pytestmark = pytest.mark.skipif(shutil.which("node") is None,
                                reason="node not installed")

APP_JS = Path(ui_server.__file__).parent / "web" / "app.js"

_JS = r"""
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
function findAll(node, tag, out) {
  if (node == null || typeof node === 'string') return out;
  if (node.tag === tag) out.push(node);
  for (const c of node.children || []) findAll(c, tag, out);
  return out;
}
const h = (tag, attrs, children) => {
  const node = { tag, attrs: attrs || {}, children: children || [], handlers: {},
    appendChild(c) { this.children.push(c); },
    addEventListener(ev, fn) { this.handlers[ev] = fn; } };
  return node;
};
const hubURL = (p) => p;
const innermostReason = (body, fallback) => (body && body.detail) || fallback;
const calls = [];
globalThis.fetch = async (url, opts) => {
  calls.push({ url, method: opts && opts.method });
  return { ok: true, status: 200, json: async () => ({ resumed: true }) };
};
eval(grab('renderUnpause'));

(async () => {
  const input = JSON.parse(process.argv[2]);
  const el = { children: [], appendChild(c) { this.children.push(c); } };
  renderUnpause(el, input.status, input.runId, input.live);
  const buttons = el.children.map((c) => findAll(c, 'button', [])).flat();
  let clicked = false;
  if (buttons.length && input.click) {
    await buttons[0].handlers.click();
    clicked = true;
  }
  console.log(JSON.stringify({
    buttons: buttons.map((b) => text(b)),
    text: el.children.map(text).join(''),
    calls, clicked,
  }));
})();
"""


def _render(status: dict, *, run_id: str = "run-x", live: bool = True,
            click: bool = False) -> dict:
    result = subprocess.run(
        ["node", "-e", _JS, str(APP_JS), json.dumps(
            {"status": status, "runId": run_id, "live": live, "click": click})],
        capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_a_paused_run_shows_an_unpause_button_that_posts_to_the_unpause_route():
    out = _render({"state": "paused", "pausedBy": "operator",
                   "pauseNote": "deploy window"}, run_id="run a/b", click=True)
    assert out["buttons"] == ["Unpause"]
    assert "paused by operator: deploy window" in out["text"]
    assert out["clicked"] is True
    assert out["calls"] == [
        {"url": "/api/runs/run%20a%2Fb/unpause", "method": "POST"}]


@pytest.mark.parametrize("run_state", [
    "running", "awaiting_input", "exhausted", "failed", "succeeded", "starting"])
def test_only_a_paused_run_gets_the_button(run_state):
    out = _render({"state": run_state})
    assert out["buttons"] == []
    assert out["text"] == ""


def test_a_paused_run_whose_api_is_unreachable_gets_no_button():
    out = _render({"state": "paused", "pausedBy": "operator"}, live=False)
    assert out["buttons"] == []
    assert "cannot unpause" in out["text"]


def test_the_hub_run_page_calls_render_unpause():
    source = APP_JS.read_text()
    assert "renderUnpause(el, s, detail.runId, detail.live !== false);" in source
