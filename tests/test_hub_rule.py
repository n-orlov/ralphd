"""Task 013 (v0.23 PRD requirement A.2, #224): the hub's `POST
/api/runs/<id>/rule` proxy -- the run page's operator-ruling textarea's
landing route -- and the textarea ITSELF.

Same shape as `test_steering_post_proxies_and_creates_steering_file` in
test_cli_ui.py: a real (no-Docker) engine via the `live` fixture, so the
proxy is proven against production code on both sides, not a stub.

The proxy nodes alone were the gap this module's first cut left (a POST
aimed straight at `/api/runs/<id>/rule` passes even if the run page has no
ruling textarea at all, or wires it to some other route): the last two
nodes therefore drive the run page's own Rule form -- one in a real
Chromium through the same `playwright-cli` harness
`tests/test_browser_hub.py::test_steering_form_submit_creates_steering_file`
uses, and one browser-free over the served bundle, so the wiring stays
pinned on a host with no browser too.
"""

from __future__ import annotations

import re
import shutil
from pathlib import Path

import pytest

from test_browser_hub import SCREENSHOTS_DIR, _wait_for, pw  # noqa: F401
from test_cli_ui import (  # noqa: F401
    StubEngineApi,
    UiServer,
    _get_raw,
    _write_run_with_api,
    ui,
)

__all__ = ["ui", "pw"]

APP_JS = (Path(__file__).resolve().parents[1] / "src" / "ralphd" / "cli"
          / "web" / "app.js")


def test_rule_post_proxies_and_creates_ruling_file(tmp_path, live, ui):
    hold_file = tmp_path / "hold-hubrule"
    run = live(run_id="hubrule", job={"iterations": 12, "max_approaches": 3,
                                       "on_complete": "idle"},
               stub_env={"STUB_TASKS": "1", "STUB_HOLD_FILE": str(hold_file),
                        "STUB_HOLD_PHASE": "worker", "STUB_HOLD_TIMEOUT_S": "30"})
    run.wait_api()

    server = ui(run.registry)
    code, resp = server.post(f"/api/runs/{run.run_id}/rule",
                              {"text": "hub ruling text", "name": "hubtest"})
    assert code == 202, resp
    assert "file" in resp

    ruling_files = list((run.run_dir / "rulings").glob("*.md"))
    assert ruling_files, "ruling POST via the hub never wrote a file"
    assert any("hub ruling text" in f.read_text() for f in ruling_files)

    # Deliberately never lands in the steering record -- see A.2's own
    # "distinct command" requirement.
    steering_files = list((run.run_dir / "steering").glob("*.md")) \
        if (run.run_dir / "steering").exists() else []
    assert not any("hub ruling text" in f.read_text() for f in steering_files)

    hold_file.touch()
    run.wait_terminal()


def test_rule_post_unknown_run_404(tmp_path, ui):
    registry = tmp_path / "registry"
    server = ui(registry)
    code, _resp = server.post("/api/runs/nope/rule", {"text": "x"})
    assert code == 404


@pytest.mark.browser
@pytest.mark.skipif(shutil.which("playwright-cli") is None,
                    reason="playwright-cli not on PATH")
def test_the_run_pages_rule_textarea_submit_creates_a_ruling_file(
        tmp_path, pw, live):
    """The hub half of A.2's "the hub run page's textarea submit reaches the
    same record": nothing is POSTed by this test at all -- a real browser
    opens the run page, types into the Rule textarea, presses the form's own
    submit button, and the ruling file appears under the live run's
    `rulings/`.

    Held open the same way the steering browser node is (STUB_HOLD_FILE /
    STUB_HOLD_PHASE="worker"), so the page is live while the form is
    submitted instead of racing a terminal state.
    """
    hold_file = tmp_path / "hold-browser-rule"
    run = live(run_id="browser-rule",
               job={"iterations": 12, "max_approaches": 3,
                    "on_complete": "idle"},
               stub_env={"STUB_TASKS": "1", "STUB_HOLD_FILE": str(hold_file),
                        "STUB_HOLD_PHASE": "worker",
                        "STUB_HOLD_TIMEOUT_S": "30"})
    run.wait_api()

    server = UiServer(run.registry)
    server.wait_ready()
    try:
        pw.open(f"{server.base}/#/run/{run.run_id}")
        _wait_for(pw, "document.body.innerText", "Rule")
        # The textarea is a textarea, not the one-line steering input: a
        # ruling is prose, and A.2 calls this surface a textarea by name.
        tag = _wait_for(pw, "document.getElementById('rule-text').tagName",
                        "TEXTAREA")
        assert "TEXTAREA" in tag, tag

        pw.fill("#rule-text", "browser e2e operator ruling")
        pw.fill("#rule-name", "browser-e2e")
        pw.click("#rule-form button[type=submit]")

        status_text = _wait_for(
            pw, "document.getElementById('rule-status').textContent",
            "recorded")
        assert "recorded" in status_text, status_text

        hold_file.touch()  # release: let the held worker iteration finish
        pw.screenshot(SCREENSHOTS_DIR / "28-operator-ruling-submitted.png")
    finally:
        run.wait_terminal(timeout=60)
        server.stop()

    ruling_files = list((run.run_dir / "rulings").glob("*.md"))
    assert ruling_files, \
        "the run page's Rule form submit never wrote a ruling file"
    assert any("browser e2e operator ruling" in f.read_text()
               for f in ruling_files)
    # ... and into the ruling record, never the steering one.
    steering = run.run_dir / "steering"
    assert not any("browser e2e operator ruling" in f.read_text()
                   for f in (steering.glob("*.md") if steering.exists() else []))


def test_the_served_bundle_wires_the_rule_textarea_to_the_rule_route(tmp_path, ui):
    """The same wiring, pinned without a browser, over the bundle the hub
    actually SERVES (not the file read off disk): the run page's Rule form
    carries the ids the submit handler binds, and that handler posts to this
    run's own `/rule` route -- never `/steer`, never anything else.

    Companion to the Chromium node above, which needs `playwright-cli`: this
    one keeps the textarea's landing route pinned on a host without one.
    """
    registry = tmp_path / "registry"
    engine = StubEngineApi()
    try:
        _write_run_with_api(registry, "run-rule-bundle", engine, state="running")
        server = ui(registry)
        code, _ctype, body = _get_raw(server, "/app.js")
    finally:
        engine.close()
    assert code == 200, code
    app_js = body.decode()
    # served == source: no second, stale copy of the wiring anywhere.
    assert app_js == APP_JS.read_text()

    # the form and its fields, as rendered by the run page
    assert re.search(r'h\("form", \{ class: "rule", id: "rule-form" \}', app_js), \
        "the run page renders no `rule-form`"
    assert re.search(r'h\("textarea", \{[^}]*id: "rule-text"', app_js), \
        "the run page's ruling field is not a textarea named `rule-text`"

    # the submit handler bound to that very form, and the route it posts to
    start = app_js.index('document.getElementById("rule-form").addEventListener')
    handler = app_js[start:app_js.index("\n  });", start)]
    assert 'document.getElementById("rule-text").value' in handler, handler
    assert re.search(
        r'fetch\(hubURL\(`/api/runs/\$\{encodeURIComponent\(runId\)\}/rule`\)',
        handler), handler
    assert '/steer' not in handler, \
        "a ruling must not land on the steering route -- review discounts " \
        "steering by design (A.2)"


def test_rule_proxy_forwards_the_control_token(tmp_path, ui):
    registry = tmp_path / "registry"
    token = "the-run-control-token"
    engine = StubEngineApi()
    try:
        _write_run_with_api(registry, "run-rule-ctl", engine, control_token=token,
                            state="running", health="degraded")
        server = ui(registry)
        code, body = server.post("/api/runs/run-rule-ctl/rule", {"text": "hi"})
        assert code == 202, body
        posts = [(p, t) for m, p, t in engine.requests if m == "POST"]
        assert [p for p, _t in posts] == ["/rule"], engine.requests
        assert engine.control_tokens == [token], engine.control_tokens
    finally:
        engine.close()
