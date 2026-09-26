"""Task 011 (v0.26, #269): browser-tier coverage for the parked panel
(tasks 008/009/010) and the two answer-textarea Enter-key contracts
(`#steer-message`, `#parked-rule-text`).

cure-01-05 (adopted #270 C.12/D.14): a live browser probe on the shipped
tree found the panel had ZERO textarea/name inputs of its own and reached
into the separate Rule section's `#rule-text` instead -- a chosen ruling
name was silently dropped on submit as a result. This module now drives
the panel's OWN `#parked-rule-text`/`#parked-rule-name` throughout, and
adds coverage for a named, multi-line ruling actually reaching `/rule`
byte-identical, name included.

Same harness as `tests/test_browser_hub.py` (playwright-cli, real Chromium,
skips the whole module cleanly when the binary is absent) -- reuses that
module's `Pw` wrapper, `pw` fixture and `_wait_for`/`_wait_for_count_ge`
helpers rather than a second copy of any of them.

At 9b72b700 `app.js` has no `renderParkedPanel`/`parkedFindingItem` at all
(tasks 007-010 add the whole feature), `#steer-message` is a one-line
`<input>` (Enter submits it, task 010 makes it an 8-row textarea) and
`#rule-text` is a 4-row textarea with no panel above it -- every node below
fails against that checkout: the panel/checkbox/action selectors do not
exist, and the steer-message Enter behaviour is the opposite of what this
module pins.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from test_browser_hub import (
    PLAYWRIGHT_CLI,
    SCREENSHOTS_DIR,
    Pw,
    _wait_for,
    _wait_for_count_ge,
    pw,
)
from test_cli_ui import (
    StubEngineApi,
    UiServer,
    _write_dead_run,
    _write_run_with_api,
)

from ralphd.engine.state import RunDir

__all__ = ["pw"]


def _skip_reason():
    if PLAYWRIGHT_CLI is None:
        return "playwright-cli not on PATH"
    return None


pytestmark = [pytest.mark.browser,
             pytest.mark.skipif(_skip_reason() is not None, reason=_skip_reason() or "")]


# ---------------------------------------------------------------------------
# fixture helpers: a real `operator_escalation` event on disk (task 007's
# own writer, `RunDir.emit`), never a hand-shaped JSON line -- so
# `parked_question` (`ui_server.py`) reads exactly the event shape the
# engine's own `_pause_and_escalate_operator_input_needed`/`_pause_and_
# escalate_repeated_rejection_signature` emit.
# ---------------------------------------------------------------------------

_BLOCKING_FINDINGS = [
    {"severity": "blocking", "clause": "clause one (#111)",
     "evidence": "evidence one", "cured_when": "clause one is discharged",
     "invalidates": []},
    {"severity": "blocking", "clause": "clause two (#112)",
     "evidence": "evidence two", "cured_when": None, "invalidates": []},
]


def _emit_parked_escalation(run_dir, reason, *, approach=1, findings=None,
                            replan_available=True, next_step="cure_pass"):
    findings = _BLOCKING_FINDINGS if findings is None else findings
    kwargs = {"approach": approach, "findings": findings,
              "escalation": "escalation text\n",
              "nextStepIfUnanswered": next_step}
    if reason == "operator_input_needed":
        kwargs["replanAvailable"] = replan_available
    RunDir(root=run_dir).emit("operator_escalation", reason=reason, **kwargs)


def _write_dead_parked_run(registry, run_id, reason, **status_extra):
    """A dead (on-disk-only) run, `pauseReason` set to `reason` (`None`
    for the plain-pause fixture, which persists no `pauseReason` at all --
    `parked_question`'s own documented rule). An `operator_escalation`
    event is written only for the two reasons a ruling can answer; the
    other two (plain pause, `stagnation`) get none, matching what the real
    engine would actually leave on disk for them."""
    status_fields = dict(state="paused", approach=1, maxApproaches=2, **status_extra)
    if reason is not None:
        status_fields["pauseReason"] = reason
    run_dir = _write_dead_run(registry, run_id, **status_fields)
    if reason in ("operator_input_needed", "rejected_repeated_signature"):
        _emit_parked_escalation(run_dir, reason)
    return run_dir


def _write_live_parked_run(registry, run_id, reason="operator_input_needed",
                           engine_kwargs=None, findings=None, **status_extra):
    """A LIVE run (`StubEngineApi` behind `host.json`, task 017's stand-in
    for the run's own container API) parked on `reason`, so a ruling
    action posted through the hub's proxy really reaches something that
    records it (`engine.requests`/`engine.bodies`), the same way it would
    reach the real engine's `POST /rule` / `POST /unpause`."""
    status = {"runId": run_id, "state": "paused", "pauseReason": reason,
              "verdict": None, "phase": "review", "approach": 1,
              "maxApproaches": 2, "iterationsUsed": 5, "iterationsBudget": 25,
              "startedAt": "2026-01-01T00:00:00Z", **status_extra}
    engine = StubEngineApi(status=status, **(engine_kwargs or {}))
    run_dir = _write_run_with_api(registry, run_id, engine, **status)
    _emit_parked_escalation(run_dir, reason, findings=findings)
    return run_dir, engine


# ---------------------------------------------------------------------------
# small keyboard/value helpers on top of `Pw` -- real keystrokes (never
# `fill`'s direct value assignment) for the Enter-key nodes below, and a
# JSON round trip for reading a string value back byte-exact (playwright-
# cli's own `--raw eval` prints the JSON encoding of the JS result --
# confirmed empirically: `eval 'JSON.stringify("a\"b\\c\nd")'` and
# `eval '"a\"b\\c\nd"'` both print the identical JSON text -- so
# `json.loads` is the one correct way to recover an embedded newline,
# never `.strip('"')`, which leaves a literal backslash-n).
# ---------------------------------------------------------------------------


def _type(pw: Pw, text: str) -> None:
    r = pw._run("type", text)
    assert r.returncode == 0, r.stdout + r.stderr


def _press(pw: Pw, key: str) -> None:
    r = pw._run("press", key)
    assert r.returncode == 0, r.stdout + r.stderr


def _uncheck(pw: Pw, target: str) -> None:
    r = pw._run("uncheck", target)
    assert r.returncode == 0, r.stdout + r.stderr


def _val(pw: Pw, expr: str):
    return json.loads(pw.eval_js(expr))


def _posts(engine: StubEngineApi):
    return [(m, p) for m, p, _t in engine.requests if m == "POST"]


def _last_body_for(engine: StubEngineApi, method: str, path: str) -> bytes:
    """The body of the LAST request matching `(method, path)` -- never
    `engine.bodies[-1]`, since the run-detail page's own background
    auto-refresh poll (`GET /status`/`/tasks`/`/logs`/`/steering`, every
    `REFRESH_MS`) can land a fresh GET on this same stand-in engine after
    the POST under test, making the plain last entry the wrong one."""
    for i in range(len(engine.requests) - 1, -1, -1):
        m, p, _t = engine.requests[i]
        if m == method and p == path:
            return engine.bodies[i]
    raise AssertionError(f"no {method} {path} request recorded; requests={engine.requests}")


# ---------------------------------------------------------------------------
# criterion (1): the panel appears only for the two ruling-answerable pause
# reasons, and every finding expands/collapses via its own <details>.
# ---------------------------------------------------------------------------


def test_panel_appears_for_ruling_answerable_reasons_and_absent_otherwise(tmp_path, pw):
    registry = tmp_path / "registry"
    _write_dead_parked_run(registry, "run-input-needed", "operator_input_needed")
    _write_dead_parked_run(registry, "run-repeated-sig", "rejected_repeated_signature")
    _write_dead_parked_run(registry, "run-plain-pause", None)
    _write_dead_parked_run(registry, "run-stagnation", "stagnation")

    server = UiServer(registry)
    server.wait_ready()
    try:
        for run_id, expect_present in [
            ("run-input-needed", True),
            ("run-repeated-sig", True),
            ("run-plain-pause", False),
            ("run-stagnation", False),
        ]:
            pw.open(f"{server.base}/#/run/{run_id}")
            # `renderParkedPanel` runs (and decides) inside the SAME `load()`
            # tick that produces the state pill -- waiting for "paused" to
            # appear guarantees the panel decision for this fixture has
            # already been made before the count below is read.
            _wait_for(pw, "document.body.innerText", "paused")
            n_panel = int(pw.eval_js("document.querySelectorAll('#parked-panel').length"))
            assert n_panel == (1 if expect_present else 0), (run_id, n_panel)
            n_box_hidden = pw.eval_js(
                "document.getElementById('parked-panel-box').style.display")
            if expect_present:
                assert n_box_hidden.strip('"') != "none", run_id
            else:
                assert n_box_hidden.strip('"') == "none", run_id
        pw.screenshot(SCREENSHOTS_DIR / "parked-01-presence.png")
    finally:
        server.stop()


def test_a_finding_expands_and_collapses_via_its_own_summary(tmp_path, pw):
    """`.parked-finding` is a native `<details>` -- clicking its `<summary>`
    toggles `.open`, and (the real behavioural proof, not just the
    attribute) the browser's own default stylesheet hides the collapsed
    body from `innerText` (which respects rendering) while `textContent`
    would not have -- so "evidence one" is absent from the rendered page
    text until the finding is expanded, and gone again once collapsed."""
    registry = tmp_path / "registry"
    _write_dead_parked_run(registry, "run-expand", "operator_input_needed")

    server = UiServer(registry)
    server.wait_ready()
    try:
        pw.open(f"{server.base}/#/run/run-expand")
        _wait_for_count_ge(pw, "document.querySelectorAll('.parked-finding').length", 1)

        body_before = pw.eval_js("document.body.innerText")
        assert "evidence one" not in body_before, body_before
        assert _val(pw, "document.querySelectorAll('.parked-finding')[0].open") is False

        # `.parked-finding summary` matches 2 elements (one per finding) --
        # `>> nth=0` (playwright's own locator chaining, distinct from a CSS
        # `:nth-of-type`) picks the first unambiguously rather than hitting
        # a strict-mode violation.
        pw.click(".parked-finding summary >> nth=0")
        _wait_for(pw, "document.body.innerText", "evidence one")
        assert _val(pw, "document.querySelectorAll('.parked-finding')[0].open") is True
        pw.screenshot(SCREENSHOTS_DIR / "parked-02-finding-expanded.png")

        pw.click(".parked-finding summary >> nth=0")
        assert _val(pw, "document.querySelectorAll('.parked-finding')[0].open") is False
        body_collapsed = pw.eval_js("document.body.innerText")
        assert "evidence one" not in body_collapsed, body_collapsed
    finally:
        server.stop()


# ---------------------------------------------------------------------------
# criterion (2): waive checkboxes compose text only (no request), and the
# two stated-effect actions each reach the engine.
# ---------------------------------------------------------------------------


def test_checking_and_unchecking_waive_boxes_composes_text_with_no_request(tmp_path, pw):
    registry = tmp_path / "registry"
    _run_dir, engine = _write_live_parked_run(registry, "run-waive")
    try:
        server = UiServer(registry)
        server.wait_ready()
        try:
            pw.open(f"{server.base}/#/run/run-waive")
            _wait_for_count_ge(pw, "document.querySelectorAll('.waive-checkbox').length", 2)
            # each waive checkbox lives inside its own (initially closed)
            # `<details class="parked-finding">` -- expand both findings
            # first, via their own summaries, exactly as an operator would.
            pw.click(".parked-finding summary >> nth=0")
            pw.click(".parked-finding summary >> nth=1")

            pw.check("#waive-F1")
            value = _val(pw, "document.getElementById('parked-rule-text').value")
            assert value == "waive F1 (clause one (#111))\n"
            assert _posts(engine) == []

            pw.check("#waive-F2")
            value = _val(pw, "document.getElementById('parked-rule-text').value")
            assert value == ("waive F1 (clause one (#111))\n"
                             "waive F2 (clause two (#112))\n")
            assert _posts(engine) == []

            _uncheck(pw, "#waive-F1")
            value = _val(pw, "document.getElementById('parked-rule-text').value")
            assert value == "waive F2 (clause two (#112))\n"
            assert _posts(engine) == []
            pw.screenshot(SCREENSHOTS_DIR / "parked-03-waive-checkboxes.png")
        finally:
            server.stop()
    finally:
        engine.close()


def test_submit_ruling_and_resume_reaches_the_engines_rule_route(tmp_path, pw):
    registry = tmp_path / "registry"
    _run_dir, engine = _write_live_parked_run(registry, "run-submit-ruling")
    try:
        server = UiServer(registry)
        server.wait_ready()
        try:
            pw.open(f"{server.base}/#/run/run-submit-ruling")
            _wait_for(pw, "document.body.innerText", "Submit ruling")
            # cure-01-05 (adopted #270 D.14): the panel's OWN name field and
            # ruling textarea -- an operator-chosen name rides along on the
            # request the engine actually receives, never dropped.
            pw.fill("#parked-rule-name", "operator-chosen-name")
            pw.fill("#parked-rule-text", "an operator ruling")

            pw.click("#parked-submit-ruling")
            _wait_for(pw, "document.getElementById('parked-action-status').textContent",
                     "ruling recorded")

            assert _posts(engine) == [("POST", "/rule")], engine.requests
            body = json.loads(_last_body_for(engine, "POST", "/rule"))
            assert body == {"text": "an operator ruling", "name": "operator-chosen-name"}
            pw.screenshot(SCREENSHOTS_DIR / "parked-04-submit-ruling.png")
        finally:
            server.stop()
    finally:
        engine.close()


def test_resume_without_a_ruling_reaches_the_engines_unpause_route(tmp_path, pw):
    registry = tmp_path / "registry"
    _run_dir, engine = _write_live_parked_run(registry, "run-resume-no-ruling")
    try:
        server = UiServer(registry)
        server.wait_ready()
        try:
            pw.open(f"{server.base}/#/run/run-resume-no-ruling")
            _wait_for(pw, "document.body.innerText", "Resume without a ruling")

            pw.click("#parked-resume-no-ruling")
            _wait_for(pw, "document.getElementById('parked-action-status').textContent",
                     "resumed with no ruling")

            assert _posts(engine) == [("POST", "/resume")], engine.requests
            pw.screenshot(SCREENSHOTS_DIR / "parked-05-resume-no-ruling.png")
        finally:
            server.stop()
    finally:
        engine.close()


# ---------------------------------------------------------------------------
# criterion (3): Enter inserts a newline (never a submit) in
# `#steer-message`, and in the panel's OWN `#parked-rule-text` -- and each
# control's own submit then delivers the composed multi-line text
# byte-identical at the request the engine actually receives.
# ---------------------------------------------------------------------------


def test_enter_in_steer_message_inserts_a_newline_and_submits_it_byte_identical(
        tmp_path, pw):
    registry = tmp_path / "registry"
    engine = StubEngineApi(status={"state": "running"})
    try:
        _write_run_with_api(registry, "run-steer-enter", engine, state="running")
        server = UiServer(registry)
        server.wait_ready()
        try:
            pw.open(f"{server.base}/#/run/run-steer-enter")
            _wait_for(pw, "document.body.innerText", "Steer")

            pw.click("#steer-message")
            _type(pw, "first steer line")
            _press(pw, "Enter")
            _type(pw, "second steer line")

            value = _val(pw, "document.getElementById('steer-message').value")
            assert value == "first steer line\nsecond steer line"
            # Enter inserted a newline natively -- it never submitted the form.
            assert _posts(engine) == []

            pw.click("#steer-form button[type=submit]")
            _wait_for(pw, "document.getElementById('steer-status').textContent", "sent")

            assert _posts(engine) == [("POST", "/steering")], engine.requests
            body = json.loads(_last_body_for(engine, "POST", "/steering"))
            assert body["message"] == "first steer line\nsecond steer line"
            pw.screenshot(SCREENSHOTS_DIR / "parked-06-steer-enter-multiline.png")
        finally:
            server.stop()
    finally:
        engine.close()


def test_enter_in_the_panels_own_rule_text_submits_via_the_panel_byte_identical(
        tmp_path, pw):
    """cure-01-05 (adopted #270 D.14): `#parked-rule-text` is the panel's
    OWN control, separate from the Rule section's own `#rule-text` -- a
    plain `<textarea>`, so Enter-inserts-a-newline is a browser default no
    app code overrides there either. Driven through `#parked-submit-
    ruling`, the panel's own action."""
    registry = tmp_path / "registry"
    _run_dir, engine = _write_live_parked_run(registry, "run-panel-rule-enter")
    try:
        server = UiServer(registry)
        server.wait_ready()
        try:
            pw.open(f"{server.base}/#/run/run-panel-rule-enter")
            _wait_for(pw, "document.body.innerText", "Submit ruling")

            pw.click("#parked-rule-text")
            _type(pw, "first panel ruling line")
            _press(pw, "Enter")
            _type(pw, "second panel ruling line")

            value = _val(pw, "document.getElementById('parked-rule-text').value")
            assert value == "first panel ruling line\nsecond panel ruling line"
            assert _posts(engine) == []

            pw.click("#parked-submit-ruling")
            _wait_for(pw, "document.getElementById('parked-action-status').textContent",
                     "ruling recorded")

            assert _posts(engine) == [("POST", "/rule")], engine.requests
            body = json.loads(_last_body_for(engine, "POST", "/rule"))
            assert body["text"] == "first panel ruling line\nsecond panel ruling line"
            pw.screenshot(SCREENSHOTS_DIR / "parked-07-panel-rule-enter-multiline.png")
        finally:
            server.stop()
    finally:
        engine.close()

# ---------------------------------------------------------------------------
# criterion (4): a waive toggle round trip preserves unrelated operator
# prose exactly, an unterminated last line included -- the exact defect a
# prior review probe found in a real browser
# (`test_waive_roundtrip_preserves_unterminated_operator_prose`).
# ---------------------------------------------------------------------------


def test_waive_toggle_round_trip_preserves_an_unterminated_operator_line(tmp_path, pw):
    registry = tmp_path / "registry"
    _run_dir, engine = _write_live_parked_run(registry, "run-waive-unterminated")
    try:
        server = UiServer(registry)
        server.wait_ready()
        try:
            pw.open(f"{server.base}/#/run/run-waive-unterminated")
            _wait_for_count_ge(pw, "document.querySelectorAll('.waive-checkbox').length", 2)
            pw.click(".parked-finding summary >> nth=0")

            pw.click("#parked-rule-text")
            _type(pw, "Keep this exact unterminated line")

            pw.check("#waive-F1")
            _uncheck(pw, "#waive-F1")

            value = _val(pw, "document.getElementById('parked-rule-text').value")
            assert value == "Keep this exact unterminated line"
            assert _posts(engine) == []
            pw.screenshot(SCREENSHOTS_DIR / "parked-08-waive-roundtrip-unterminated.png")
        finally:
            server.stop()
    finally:
        engine.close()
