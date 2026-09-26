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


def test_steer_apply_now_is_labelled_with_its_cost(tmp_path, ui):
    """PRD requirement 4 (#161): the steer keeps `apply now`, labelled with
    its cost. At 9b72b700 the label was the bare text `apply now`."""
    app_js = _served_app_js(tmp_path, ui)
    start = app_js.index('id: "steer-now"')
    label_open = app_js.rindex('h("label"', 0, start)
    # the label's children end at the `send` submit button that follows it
    label_src = app_js[label_open:app_js.index('h("button"', start)]
    assert '"apply now"' in label_src, label_src
    # the cost itself: `--now` SIGINTs the running iteration, whose
    # uncommitted work is lost; an idle loop is only woken.
    assert "cost:" in label_src, label_src
    assert "interrupts the running iteration" in label_src, label_src
    assert "uncommitted work is lost" in label_src, label_src
    assert "idle loop is only woken" in label_src, label_src


def test_steer_apply_now_discloses_the_plan_review_revision_risk(tmp_path, ui):
    """cure-01-05 (adopted #270 C.12): the label also discloses #161's
    OTHER cost -- interrupting a running PLAN-REVIEW iteration spends a
    revision and re-ingests every task `pending`
    (`test_steer_now_twice_against_a_running_plan_review_still_spends_a_
    revision`, `tests/test_transitions.py`) -- unlike a single interrupt of
    `planning`/`worker`/`review`, which just retries in place with no extra
    spend (`test_steer_now_against_a_running_planning_iteration_keeps_
    todays_retry_in_place`, same module). Disclosure only: interrupt and
    rejection semantics are unchanged by this label."""
    app_js = _served_app_js(tmp_path, ui)
    start = app_js.index('id: "steer-now"')
    label_open = app_js.rindex('h("label"', 0, start)
    label_src = app_js[label_open:app_js.index('h("button"', start)]
    assert "revision" in label_src, label_src
    assert "re-ingest" in label_src, label_src
    # the disclosed risk is conditional on the phase actually interrupted
    # (plan review), never phrased as an unconditional cost of every
    # `--now` click.
    assert "plan review" in label_src or "plan-review" in label_src, label_src
