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

from ralphd.engine.state import RunDir, build_input_question

# v0.36: a findings park is `awaiting_input` (with its `inputQuestion`); every
# other reason is still an engine/operator `paused` hold with no panel.
_FINDINGS_PARK_REASONS = ("operator_input_needed", "rejected_repeated_signature")


def _park_status(reason) -> dict:
    if reason not in _FINDINGS_PARK_REASONS:
        return {"state": "paused"}
    return {"state": "awaiting_input", "inputQuestion": build_input_question(
        judge="review", trigger="cure_rounds_spent",
        blocking=[{"severity": "blocking", "curable": True,
                   "clause": "clause one (#111)", "evidence": "evidence one"}],
        cure_rounds_used=3, cure_rounds_allowed=3, approach=1,
        max_approaches=2, parked_at_epoch=1767225600.0)}

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
    status_fields = dict(approach=1, maxApproaches=2, **_park_status(reason),
                         **status_extra)
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
    status = {"runId": run_id, **_park_status(reason), "pauseReason": reason,
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
            _wait_for(pw, "document.body.innerText",
                      "awaiting_input" if expect_present else "paused")
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


def test_no_stale_parked_panel_after_a_commit_scan_hold_following_an_answered_gate(tmp_path, pw):
    """Cure task cure-01-02-4 (PRD B.1, D.1; DoD 2/4): a task-boundary
    commit-secret-scan hold reuses the SAME `operator_input_needed` reason
    string an already-answered review gate left behind, but emits no fresh
    `operator_escalation` event of its own
    (`LoopSupervisor._pause_for_commit_secret_hit`). The panel must not
    resurrect that answered gate's own stale question/finding list -- and a
    genuinely new review escalation raised once that hold is answered still
    appears, with its OWN findings and consequences, not the stale ones."""
    registry = tmp_path / "registry"
    run_dir = _write_dead_run(
        registry, "run-stale-commit-scan", state="paused",
        pauseReason="operator_input_needed", approach=1, maxApproaches=2)
    rd = RunDir(root=run_dir)
    # The review gate's own escalation -- later answered by a resume.
    rd.emit("operator_escalation", reason="operator_input_needed", approach=1,
            findings=_BLOCKING_FINDINGS, escalation="escalation text\n",
            replanAvailable=True, nextStepIfUnanswered="cure_pass",
            taskIdsValidatedToPending=["stale-reopened-task"], insertCount=0)
    rd.emit("log", message="resumed")
    # A cure worker runs, then a task-boundary commit-secret-scan hit raises
    # the SAME reason string with no fresh `operator_escalation` behind it.
    rd.emit("commit_secret_scan", sha="0" * 40, part="blob",
            variable="REVIEW_TEST_VARIABLE", pushed=False)

    server = UiServer(registry)
    server.wait_ready()
    try:
        pw.open(f"{server.base}/#/run/run-stale-commit-scan")
        _wait_for(pw, "document.body.innerText", "paused")
        n_panel = int(pw.eval_js("document.querySelectorAll('#parked-panel').length"))
        assert n_panel == 0
        box_hidden = pw.eval_js(
            "document.getElementById('parked-panel-box').style.display")
        assert box_hidden.strip('"') == "none"
        pw.screenshot(SCREENSHOTS_DIR / "parked-00-no-stale-after-commit-scan.png")

        # The operator answers the commit-scan hold; a later review then
        # raises a genuinely new escalation (same reason string) over a
        # DIFFERENT finding with its own cure-pass preview.
        rd.emit("log", message="resumed")
        rd.emit("operator_escalation", reason="operator_input_needed", approach=1,
                findings=[{"severity": "blocking",
                           "clause": "fresh review clause (#321)",
                           "evidence": "fresh evidence", "cured_when": None,
                           "invalidates": ["fresh-reopened-task"]}],
                escalation="fresh escalation text\n", replanAvailable=True,
                nextStepIfUnanswered="cure_pass",
                taskIdsValidatedToPending=["fresh-reopened-task"], insertCount=1)
        # v0.36: the later review park is an `awaiting_input` question.
        rd.update_status(**_park_status("operator_input_needed"))
        pw.open(f"{server.base}/#/run/run-stale-commit-scan")
        _wait_for(pw, "document.body.innerText", "fresh review clause (#321)")
        n_panel = int(pw.eval_js("document.querySelectorAll('#parked-panel').length"))
        assert n_panel == 1
        box_hidden = pw.eval_js(
            "document.getElementById('parked-panel-box').style.display")
        assert box_hidden.strip('"') != "none"
        panel_text = _val(
            pw, "document.getElementById('parked-panel').innerText")
        # Its own consequences, read off the rendered consequence list.
        consequence_text = _val(
            pw, "document.querySelector('#parked-panel .parked-consequence').innerText")
        assert "if left unanswered: a cure pass over this approach's blocking findings" \
            in consequence_text
        assert "tasks reopened to pending: fresh-reopened-task" in consequence_text
        assert "tasks inserted: 1" in consequence_text
        assert "blocking findings: 1" in panel_text
        # None of the answered gate's stale question or preview leaks in.
        assert "clause one (#111)" not in panel_text
        assert "clause two (#112)" not in panel_text
        assert "stale-reopened-task" not in panel_text
        assert "tasks inserted: 0" not in consequence_text
        pw.screenshot(SCREENSHOTS_DIR / "parked-00b-fresh-escalation-after-commit-scan.png")
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


def test_enter_in_the_panels_own_rule_text_submits_named_multiline_byte_identical(
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

            # criterion (2): a NAMED multi-line ruling -- the panel's own
            # name field and the Enter-composed body travel together.
            pw.fill("#parked-rule-name", "named-multiline-ruling")
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
            assert body == {"text": "first panel ruling line\nsecond panel ruling line",
                            "name": "named-multiline-ruling"}
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


def test_waive_toggle_round_trip_preserves_a_single_trailing_newline_in_browser(
        tmp_path, pw):
    """cure-01-01-5 (B.3-B.5): the review's own live browser probe found
    that checking then unchecking a waive box on operator prose already
    ending in exactly one real newline silently dropped that byte -- the
    node-eval regression above (`test_hub_parked_actions.py`) pins the same
    contract without a browser; this pins it through the real DOM checkbox
    and a real click, never a hand-built event."""
    registry = tmp_path / "registry"
    _run_dir, engine = _write_live_parked_run(registry, "run-waive-one-newline")
    try:
        server = UiServer(registry)
        server.wait_ready()
        try:
            pw.open(f"{server.base}/#/run/run-waive-one-newline")
            _wait_for_count_ge(pw, "document.querySelectorAll('.waive-checkbox').length", 2)
            pw.click(".parked-finding summary >> nth=0")

            typed = "Keep this exact paragraph.\n"
            pw.fill("#parked-rule-text", typed)

            pw.check("#waive-F1")
            composed = _val(pw, "document.getElementById('parked-rule-text').value")
            assert composed == typed + "waive F1 (clause one (#111))\n"

            _uncheck(pw, "#waive-F1")
            actual = _val(pw, "document.getElementById('parked-rule-text').value")
            assert actual == typed
            assert _posts(engine) == []
            pw.screenshot(SCREENSHOTS_DIR / "parked-09-waive-roundtrip-one-newline.png")
        finally:
            server.stop()
    finally:
        engine.close()


def test_uncheck_does_not_remove_a_substring_from_operator_sentence_in_browser(
        tmp_path, pw):
    """cure-01-01-6 (B.3-B.4, D.1, DoD 2): the review's own live probe
    found that unchecking a waive box removed a bare `indexOf(line +
    "\n")` substring match wherever it happened to occur -- including
    INSIDE an unrelated operator sentence that merely happens to contain
    the waive line's own text ('Do not waive F1 (clause one (#111))\n'
    contains 'waive F1 (clause one (#111))\n' as a substring, but not as
    its own line). Checking F1 then F2, then unchecking F1, must remove
    only the LINE `applyWaiveLine` itself composed and appended, never
    the matching text embedded in the operator's own prose above it --
    pinned here through the real DOM checkbox and a real click."""
    registry = tmp_path / "registry"
    _run_dir, engine = _write_live_parked_run(registry, "run-waive-substring")
    try:
        server = UiServer(registry)
        server.wait_ready()
        try:
            pw.open(f"{server.base}/#/run/run-waive-substring")
            _wait_for_count_ge(pw, "document.querySelectorAll('.waive-checkbox').length", 2)
            pw.click(".parked-finding summary >> nth=0")
            pw.click(".parked-finding summary >> nth=1")

            body = "Do not waive F1 (clause one (#111))\nKeep this paragraph\n"
            pw.fill("#parked-rule-text", body)

            pw.check("#waive-F1")
            pw.check("#waive-F2")
            _uncheck(pw, "#waive-F1")

            actual = _val(pw, "document.getElementById('parked-rule-text').value")
            expected = body + "waive F2 (clause two (#112))\n"
            assert actual == expected
            assert _posts(engine) == []
            pw.screenshot(SCREENSHOTS_DIR / "parked-11-waive-substring.png")
        finally:
            server.stop()
    finally:
        engine.close()


def test_edit_then_check_and_uncheck_second_finding_preserves_body_in_browser(
        tmp_path, pw):
    """cure-01-01-6 (B.3-B.4, D.1, DoD 2): the companion bug -- separator
    insertion was tracked only at the instant the composed region grew
    from zero active lines to one, so a hand edit typed directly into the
    textarea BETWEEN two checks (leaving it unterminated again with one
    waive line already active) never got its own leading separator
    recorded. Checking F1, typing an unterminated further explanation,
    then checking and unchecking F2 must leave the edited prose exactly
    as typed, with F2's own composed line and its own separator fully
    removed -- pinned through real keystrokes and real checkbox clicks."""
    registry = tmp_path / "registry"
    _run_dir, engine = _write_live_parked_run(registry, "run-waive-edit-between")
    try:
        server = UiServer(registry)
        server.wait_ready()
        try:
            pw.open(f"{server.base}/#/run/run-waive-edit-between")
            _wait_for_count_ge(pw, "document.querySelectorAll('.waive-checkbox').length", 2)
            pw.click(".parked-finding summary >> nth=0")
            pw.click(".parked-finding summary >> nth=1")

            pw.fill("#parked-rule-text", "original\n")
            pw.check("#waive-F1")

            after_f1 = _val(pw, "document.getElementById('parked-rule-text').value")
            before = after_f1 + "My further explanation"
            pw.fill("#parked-rule-text", before)

            pw.check("#waive-F2")
            _uncheck(pw, "#waive-F2")

            actual = _val(pw, "document.getElementById('parked-rule-text').value")
            assert actual == before
            assert _posts(engine) == []
            pw.screenshot(SCREENSHOTS_DIR / "parked-12-waive-edit-between.png")
        finally:
            server.stop()
    finally:
        engine.close()


def test_unchecking_the_earlier_of_two_composed_waivers_keeps_the_later_in_browser(
        tmp_path, pw):
    """cure-01-01-7 (B.3-B.4, D.1, DoD 2; cure-01-01-6): checking F1 into
    unterminated prose (so F1's own insertion adds a leading separator)
    then F2, then unchecking F1 -- the EARLIER of the two, not the last
    line -- must strip only F1's own composed line, never the separator
    its insertion added: F2's still-active line now sits right where
    F1's used to and still needs that exact byte to stay off the
    operator's own prose. Pinned through real checkbox clicks."""
    registry = tmp_path / "registry"
    _run_dir, engine = _write_live_parked_run(registry, "run-waive-earlier-first")
    try:
        server = UiServer(registry)
        server.wait_ready()
        try:
            pw.open(f"{server.base}/#/run/run-waive-earlier-first")
            _wait_for_count_ge(pw, "document.querySelectorAll('.waive-checkbox').length", 2)
            pw.click(".parked-finding summary >> nth=0")
            pw.click(".parked-finding summary >> nth=1")

            pw.fill("#parked-rule-text", "orig")
            pw.check("#waive-F1")
            pw.check("#waive-F2")
            _uncheck(pw, "#waive-F1")

            actual = _val(pw, "document.getElementById('parked-rule-text').value")
            assert actual == "orig\nwaive F2 (clause two (#112))\n"
            assert _posts(engine) == []
            pw.screenshot(SCREENSHOTS_DIR / "parked-13-waive-earlier-first.png")
        finally:
            server.stop()
    finally:
        engine.close()


@pytest.mark.parametrize("order", [
    ["#waive-F1", "#waive-F2"],
    ["#waive-F2", "#waive-F1"],
], ids=["remove-earlier-first", "remove-later-first"])
def test_removing_every_composed_waiver_in_either_order_restores_the_original_in_browser(
        tmp_path, pw, order):
    """cure-01-01-7 (B.3-B.4, D.1, DoD 2; cure-01-01-6): a separator a
    removed line correctly declines to strip -- because another composed
    line still needs it -- is not lost, it is inherited by that
    remaining line, so removing EVERY composed waiver, in EITHER order,
    always ends back at the exact original, unterminated bytes, never a
    leftover synthetic newline and never a merged byte. Pinned through
    real checkbox clicks in both removal orders."""
    registry = tmp_path / "registry"
    _run_dir, engine = _write_live_parked_run(registry, "run-waive-remove-all")
    try:
        server = UiServer(registry)
        server.wait_ready()
        try:
            pw.open(f"{server.base}/#/run/run-waive-remove-all")
            _wait_for_count_ge(pw, "document.querySelectorAll('.waive-checkbox').length", 2)
            pw.click(".parked-finding summary >> nth=0")
            pw.click(".parked-finding summary >> nth=1")

            pw.fill("#parked-rule-text", "orig")
            pw.check("#waive-F1")
            pw.check("#waive-F2")
            _uncheck(pw, order[0])
            _uncheck(pw, order[1])

            actual = _val(pw, "document.getElementById('parked-rule-text').value")
            assert actual == "orig"
            assert _posts(engine) == []
            pw.screenshot(SCREENSHOTS_DIR / "parked-14-waive-remove-all.png")
        finally:
            server.stop()
    finally:
        engine.close()


# ---------------------------------------------------------------------------
# criterion (1): findings occupy the LEFT column, the answer box and its
# two actions occupy the RIGHT column, at desktop widths.
# ---------------------------------------------------------------------------


def test_wide_parked_panel_places_answer_to_right_of_findings(tmp_path, pw):
    """cure-01-01-5 (B.3-B.5, adopted #270 D.14): the review's own live
    probe at viewport width 1280 found both `.parked-findings` and
    `.parked-answer` full-width and STACKED, the answer strictly below
    the findings rather than beside them. Pinned here on the real
    rendered geometry -- `.parked-panel-columns`'s own `min-width: 900px`
    breakpoint (`style.css`) -- never only on the source-level DOM
    structure the fast-tier regression above already pins."""
    registry = tmp_path / "registry"
    _run_dir, engine = _write_live_parked_run(registry, "run-wide-columns")
    try:
        server = UiServer(registry)
        server.wait_ready()
        try:
            pw.open(f"{server.base}/#/run/run-wide-columns")
            _wait_for(pw, "document.body.innerText", "Submit ruling")
            got = json.loads(pw.eval_js(
                "(() => { const rect = s => { const r = "
                "document.querySelector(s).getBoundingClientRect(); "
                "return {left: r.left, right: r.right, top: r.top, "
                "bottom: r.bottom}; }; return {width: innerWidth, "
                "findings: rect('.parked-findings'), "
                "answer: rect('.parked-answer')}; })()"))
            assert got["width"] >= 900, "probe needs a desktop viewport"
            pw.screenshot(SCREENSHOTS_DIR / "parked-10-wide-columns.png")
            # the answer box begins at or after the findings column ends
            # (side by side, never one strictly below the other).
            assert got["answer"]["left"] >= got["findings"]["right"] - 1
            assert got["answer"]["top"] < got["findings"]["bottom"]
        finally:
            server.stop()
    finally:
        engine.close()


# ---------------------------------------------------------------------------
# criterion (3): an unrelated detail refresh preserves an in-progress
# draft; a genuinely CHANGED question and an ANSWERED pause each get
# deliberate, different handling.
# ---------------------------------------------------------------------------


def test_an_unrelated_detail_refresh_preserves_the_in_progress_draft(tmp_path, pw):
    """cure-01-01-5 (B.3-B.5, adopted #270 D.14): the review's own live
    probe found that ANY fresh `events.jsonl` entry -- even one with
    nothing to do with the parked question, `notification.failed` here --
    moved the run-detail `ETag` and re-rendered the panel from scratch,
    silently wiping the operator's own in-progress draft name/text. Pinned
    through the REAL background poll (never a synthetic `renderParkedPanel`
    call): the draft is typed, an unrelated event is appended on disk, and
    the poll is given real time to observe the moved `ETag` before the
    draft is asserted still exactly what was typed."""
    registry = tmp_path / "registry"
    run_dir, engine = _write_live_parked_run(registry, "run-unrelated-refresh")
    try:
        server = UiServer(registry)
        server.wait_ready()
        try:
            pw.open(f"{server.base}/#/run/run-unrelated-refresh")
            _wait_for(pw, "document.body.innerText", "Submit ruling")

            typed = "Draft answer\n\nNot submitted yet.\n"
            pw.fill("#parked-rule-text", typed)
            pw.fill("#parked-rule-name", "draft-name")
            assert _val(pw, "(() => { window.__cureOldAnswer = "
                       "document.getElementById('parked-rule-text'); "
                       "return !!window.__cureOldAnswer; })()") is True

            gets_before = sum(1 for m, p, _t in engine.requests
                              if m == "GET" and p == "/status")
            RunDir(root=run_dir).emit(
                "notification.failed", endpoint="http://notifier.example",
                textSha256="no-secret-here", httpStatus=503)

            # confirm the real background poll actually observed the moved
            # ETag and re-fetched (never assumed from a fixed sleep): the
            # paused run's own back-off condition (`lastKnownState !==
            # "running"`) widens the poll to `BACKOFF_POLL_MS` (20s) after
            # the very first load, so the wait below spans well over one
            # full backed-off interval rather than the plain `REFRESH_MS`
            # (4s) a running run would use.
            import time
            deadline = time.monotonic() + 50
            while time.monotonic() < deadline:
                gets_now = sum(1 for m, p, _t in engine.requests
                               if m == "GET" and p == "/status")
                if gets_now >= gets_before + 1:
                    break
                time.sleep(0.5)
            else:
                raise AssertionError("no poll cycle observed after the unrelated event")

            actual = _val(pw, "({text: document.getElementById('parked-rule-text').value, "
                         "name: document.getElementById('parked-rule-name').value})")
            assert actual == {"text": typed, "name": "draft-name"}
            pw.screenshot(SCREENSHOTS_DIR / "parked-11-unrelated-refresh-preserved.png")
        finally:
            server.stop()
    finally:
        engine.close()


def test_a_genuinely_changed_question_replaces_the_panel_with_a_fresh_draft(
        tmp_path, pw):
    """The companion case to the preservation above: a fresh escalation
    over DIFFERENT findings (a re-park, never merely an unrelated event)
    is a genuinely new question, so the panel rebuilds with its OWN new
    findings and an EMPTY draft -- the old, unsubmitted text does not
    leak into a question it was never written for."""
    registry = tmp_path / "registry"
    run_dir, engine = _write_live_parked_run(registry, "run-changed-question")
    try:
        server = UiServer(registry)
        server.wait_ready()
        try:
            pw.open(f"{server.base}/#/run/run-changed-question")
            _wait_for(pw, "document.body.innerText", "Submit ruling")

            pw.fill("#parked-rule-text", "stale draft for the old question")
            pw.fill("#parked-rule-name", "stale-name")

            _emit_parked_escalation(
                run_dir, "operator_input_needed",
                findings=[{"severity": "blocking", "clause": "a brand new clause",
                          "evidence": "fresh evidence", "cured_when": None,
                          "invalidates": []}])

            import time
            deadline = time.monotonic() + 50
            seen_fresh = False
            while time.monotonic() < deadline:
                text = pw.eval_js("document.body.innerText")
                if "a brand new clause" in text:
                    seen_fresh = True
                    break
                time.sleep(0.5)
            assert seen_fresh, "the changed question was never observed"

            actual = _val(pw, "document.getElementById('parked-rule-text').value")
            assert actual == ""
            name = _val(pw, "document.getElementById('parked-rule-name').value")
            assert name == ""
            pw.screenshot(SCREENSHOTS_DIR / "parked-12-changed-question-fresh-draft.png")
        finally:
            server.stop()
    finally:
        engine.close()


def test_an_answered_pause_hides_the_panel_on_the_next_refresh(tmp_path, pw):
    """The panel disappears once the pause it answers is actually resolved
    -- a live `pauseReason`/`state` transition, never only the initial
    fixture-based absence `test_panel_appears_for_ruling_answerable_
    reasons_and_absent_otherwise` already pins."""
    registry = tmp_path / "registry"
    run_dir, engine = _write_live_parked_run(registry, "run-answered-pause")
    try:
        server = UiServer(registry)
        server.wait_ready()
        try:
            pw.open(f"{server.base}/#/run/run-answered-pause")
            _wait_for(pw, "document.body.innerText", "Submit ruling")

            status = json.loads((run_dir / "status.json").read_text())
            status["pauseReason"] = None
            status["state"] = "running"
            (run_dir / "status.json").write_text(json.dumps(status))
            engine.status_body["pauseReason"] = None
            engine.status_body["state"] = "running"

            import time
            deadline = time.monotonic() + 50
            hidden = False
            while time.monotonic() < deadline:
                display = pw.eval_js(
                    "document.getElementById('parked-panel-box').style.display")
                if display.strip('"') == "none":
                    hidden = True
                    break
                time.sleep(0.5)
            assert hidden, "the panel never hid after the pause was answered"
            pw.screenshot(SCREENSHOTS_DIR / "parked-13-answered-pause-hidden.png")
        finally:
            server.stop()
    finally:
        engine.close()
