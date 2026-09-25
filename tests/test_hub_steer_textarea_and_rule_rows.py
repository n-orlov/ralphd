"""Task 010 (v0.26 PRD requirement 4, #270): pins the shape of both the hub
steer input and the ruling textarea, browser-free, over the bundle the hub
actually SERVES.

Fails at 9b72b700: `#steer-message` was `h("input", { type: "text", ... })`
there (no `textarea` at all) and `#rule-text` carried `rows: "4"`.
"""

from __future__ import annotations

import re
from pathlib import Path

from test_cli_ui import StubEngineApi, _get_raw, _write_run_with_api, ui  # noqa: F401

__all__ = ["ui"]

APP_JS = (Path(__file__).resolve().parents[1] / "src" / "ralphd" / "cli"
          / "web" / "app.js")


def _served_app_js(tmp_path, ui):
    registry = tmp_path / "registry"
    engine = StubEngineApi()
    try:
        _write_run_with_api(registry, "run-textarea-rows", engine, state="running")
        server = ui(registry)
        code, _ctype, body = _get_raw(server, "/app.js")
    finally:
        engine.close()
    assert code == 200, code
    app_js = body.decode()
    assert app_js == APP_JS.read_text()
    return app_js


def test_steer_message_is_a_textarea_with_about_8_rows(tmp_path, ui):
    app_js = _served_app_js(tmp_path, ui)
    # never the old one-line `<input type="text">`
    assert not re.search(r'h\("input",\s*\{[^}]*id:\s*"steer-message"', app_js), \
        "the steer message is still a single-line <input>"
    m = re.search(r'h\("textarea",\s*\{([^}]*id:\s*"steer-message"[^}]*)\}', app_js)
    assert m, "#steer-message is not rendered as a textarea at all"
    attrs = m.group(1)
    rows_m = re.search(r'rows:\s*"(\d+)"', attrs)
    assert rows_m, f"#steer-message textarea has no rows attribute: {attrs}"
    rows = int(rows_m.group(1))
    assert 6 <= rows <= 10, f"#steer-message rows={rows}, expected about 8"
    # the `name` field (the operator's `--name`) survives the resize.
    assert 'id: "steer-name"' in app_js


def test_rule_text_is_a_textarea_with_at_least_12_rows(tmp_path, ui):
    app_js = _served_app_js(tmp_path, ui)
    m = re.search(r'h\("textarea",\s*\{([^}]*id:\s*"rule-text"[^}]*)\}', app_js)
    assert m, "#rule-text is not rendered as a textarea at all"
    attrs = m.group(1)
    rows_m = re.search(r'rows:\s*"(\d+)"', attrs)
    assert rows_m, f"#rule-text textarea has no rows attribute: {attrs}"
    rows = int(rows_m.group(1))
    assert rows >= 12, f"#rule-text rows={rows}, expected at least 12"
    assert 'id: "rule-name"' in app_js
