"""Task 009 (v0.26, #269): the parked panel's answer controls -- a `waive`
checkbox per finding that only composes editable ruling text, and the two
stated-effect actions (`Submit ruling & resume`, `Resume without a ruling`).

cure-01-05 (adopted #270 C.12/D.14): the panel now carries its OWN visible
`#parked-rule-name`/`#parked-rule-text` answer box, right alongside its
findings, instead of reaching into the SEPARATE Rule section's `#rule-text`
further down the page -- a live browser probe found zero textarea/name
inputs inside `#parked-panel`, and a chosen name was silently dropped on
submit. This module now drives the panel's OWN fields throughout.

At 9b72b700 `app.js` has no `renderParkedPanel`/`parkedFindingItem` at all
(task 008 adds those), and even once task 008/010 land there is still no
waive checkbox, no `parkedWaiveLineText`/`applyWaiveLine`, no
`#parked-rule-name`/`#parked-rule-text`, no
`#parked-submit-ruling`/`#parked-resume-no-ruling` button and no
`#parked-action-status` -- every node below fails against both checkouts.

Browser-free, the same node-eval technique `tests/test_hub_parked_panel.py`
already uses (the REAL functions are extracted verbatim out of the served
`app.js` and evaluated under Node), extended with a `document` stub that
carries just `getElementById` (backing the panel's own answer fields) and a
recording `fetch` stand-in -- so the REAL onchange/onclick closures app.js
builds are invoked directly and their effect (or absence of one) observed,
never a Python reimplementation of what they do.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")

sys.path.insert(0, str(Path(__file__).parent))

from ralphd.cli import ui_server  # noqa: E402

APP_JS = (Path(ui_server.__file__).parent / "web" / "app.js")

# ---------------------------------------------------------------------------
# node-eval harness
# ---------------------------------------------------------------------------

_ACTIONS_JS = r"""
const fs = require('fs');
const src = fs.readFileSync(process.argv[1], 'utf8');
function grab(name) {
  const start = src.indexOf('function ' + name + '(');
  if (start < 0) throw Error('missing function ' + name);
  const end = src.indexOf('\n}\n', start);
  return src.slice(start, end + 3);
}
function findAll(node, tag, out) {
  out = out || [];
  if (node == null || typeof node === 'string') return out;
  if (node.tag === tag) out.push(node);
  for (const c of node.children || []) findAll(c, tag, out);
  return out;
}
function find(node, pred) {
  if (node == null || typeof node === 'string') return null;
  if (pred(node)) return node;
  for (const c of node.children || []) {
    const f = find(c, pred);
    if (f) return f;
  }
  return null;
}
function text(node) {
  if (node == null) return '';
  if (typeof node === 'string') return node;
  return (node.children || []).map(text).join('');
}
const h = (tag, attrs, children) => ({ tag, attrs: attrs || {}, children: children || [] });
// `hubURL` is stubbed to identity -- the real implementation resolves a
// mount prefix off `document.currentScript`, irrelevant to what this
// harness checks (which route each button posts to, and with what body).
function hubURL(path) { return String(path); }
const calls = [];
function fetch(url, opts) {
  calls.push({ url, opts: opts || {} });
  const queued = (globalThis.__nextResponses || []).shift();
  const resp = queued || { ok: true, status: 200, json: async () => ({}) };
  return Promise.resolve(resp);
}
const document = {
  __els: {},
  getElementById(id) { return this.__els[id]; },
};
eval(grab('parkedReasonText'));
eval(grab('parkedNextStepOutcomeText'));
eval(grab('parkedConsequenceLines'));
eval(grab('parkedWaiveLineText'));
eval(grab('applyWaiveLine'));
eval(grab('parkedFindingItem'));
eval(grab('fmtDuration'));
eval(grab('isoToEpoch'));
eval(grab('durationBetween'));
eval(grab('innermostReason'));
eval(grab('renderParkedPanel'));

function makeBox() {
  return { children: [], style: {}, innerHTML: '',
    appendChild(n) { this.children.push(n); } };
}

async function main() {
  const input = JSON.parse(process.argv[2]);
  // The panel's OWN answer box -- never `#rule-text`/`#rule-name`, the
  // separate Rule section's own fields.
  document.__els['parked-rule-text'] = { value: input.textareaValue || '' };
  document.__els['parked-rule-name'] = { value: input.nameValue || '' };
  globalThis.__nextResponses = (input.responses || []).map((r) => ({
    ok: r.ok !== false,
    status: r.status || (r.ok === false ? 400 : 200),
    json: async () => r.body || {},
  }));

  const box = makeBox();
  renderParkedPanel(box, input.runId, input.pq);
  const card = box.children[0];
  const checkboxes = card ? findAll(card, 'input').filter((n) => n.attrs.type === 'checkbox') : [];
  const textInputs = card ? findAll(card, 'input').filter((n) => n.attrs.type === 'text') : [];
  const textareas = card ? findAll(card, 'textarea') : [];

  for (const w of (input.waive || [])) {
    const cb = checkboxes.find((c) => c.attrs.id === 'waive-' + w.id);
    if (!cb) throw Error('no such waive checkbox: ' + w.id);
    cb.attrs.onchange({ target: { checked: w.checked } });
  }

  const buttons = card ? findAll(card, 'button') : [];
  const submitBtn = buttons.find((b) => b.attrs.id === 'parked-submit-ruling');
  const resumeBtn = buttons.find((b) => b.attrs.id === 'parked-resume-no-ruling');
  if (input.click === 'submit') await submitBtn.attrs.onclick();
  if (input.click === 'resume') await resumeBtn.attrs.onclick();

  const statusEl = card ? find(card, (n) => n.attrs && n.attrs.id === 'parked-action-status') : null;

  console.log(JSON.stringify({
    textareaValue: document.__els['parked-rule-text'].value,
    nameValue: document.__els['parked-rule-name'].value,
    calls,
    statusText: statusEl ? statusEl.textContent || null : null,
    cardText: card ? text(card) : '',
    submitButtonPresent: !!submitBtn,
    resumeButtonPresent: !!resumeBtn,
    checkboxIds: checkboxes.map((c) => c.attrs.id),
    panelTextareaIds: textareas.map((t) => t.attrs.id),
    panelTextInputIds: textInputs.map((t) => t.attrs.id),
  }));
}
main();
"""


def _run(pq, **kw) -> dict:
    payload = {"runId": kw.pop("run_id", "run-x"), "pq": pq, **kw}
    result = subprocess.run(
        ["node", "-e", _ACTIONS_JS, str(APP_JS), json.dumps(payload)],
        capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


_PQ = {
    "reason": "operator_input_needed",
    "approach": 1,
    "maxApproaches": 2,
    "approachOfMax": "approach 1 of 2",
    "parkedAt": "2020-01-01T00:00:00Z",
    "replanAvailable": True,
    "blockingCount": 2,
    "residualCount": 0,
    "findings": [
        {"id": "F1", "severity": "blocking", "clause": "clause one (#111)",
         "evidence": "evidence one", "cured_when": "clause one is discharged",
         "invalidates": []},
        {"id": "F2", "severity": "blocking", "clause": "clause two (#112)",
         "evidence": "evidence two", "cured_when": None, "invalidates": []},
    ],
    "consequence": {
        "taskIdsValidatedToPending": [],
        "insertCount": 0,
        "costSpentThisApproach": "$1.23",
        "tokensSpentThisApproach": 456,
        "nextStepIfUnanswered": "cure_pass",
    },
}


# ---------------------------------------------------------------------------
# criterion (1): a waive checkbox per finding, composing text only, never
# sending a request
# ---------------------------------------------------------------------------


def test_every_finding_carries_its_own_waive_checkbox():
    out = _run(_PQ)
    assert out["checkboxIds"] == ["waive-F1", "waive-F2"]


def test_checking_a_waive_box_composes_its_findings_line_into_parked_rule_text():
    out = _run(_PQ, waive=[{"id": "F1", "checked": True}])
    assert out["textareaValue"] == "waive F1 (clause one (#111))\n"
    assert out["calls"] == []  # no request sent


def test_checking_two_waive_boxes_composes_both_lines():
    out = _run(_PQ, waive=[{"id": "F1", "checked": True},
                            {"id": "F2", "checked": True}])
    assert out["textareaValue"] == (
        "waive F1 (clause one (#111))\nwaive F2 (clause two (#112))\n")
    assert out["calls"] == []


def test_unchecking_a_waive_box_removes_only_its_own_line():
    out = _run(_PQ, textareaValue="operator note\n",
               waive=[{"id": "F1", "checked": True},
                      {"id": "F2", "checked": True},
                      {"id": "F1", "checked": False}])
    assert out["textareaValue"] == "operator note\nwaive F2 (clause two (#112))\n"
    assert out["calls"] == []


def test_waived_text_stays_editable_hand_typed_text_survives_a_toggle():
    out = _run(_PQ, textareaValue="already typed by hand\n",
               waive=[{"id": "F1", "checked": True}])
    assert out["textareaValue"] == "already typed by hand\nwaive F1 (clause one (#111))\n"


def test_checking_a_waive_box_keeps_hand_typed_blank_lines():
    out = _run(_PQ, textareaValue="first paragraph\n\nsecond paragraph\n",
               waive=[{"id": "F1", "checked": True}])
    assert out["textareaValue"] == (
        "first paragraph\n\nsecond paragraph\nwaive F1 (clause one (#111))\n")


def test_checking_a_waive_box_starts_a_new_line_after_unterminated_text():
    out = _run(_PQ, textareaValue="no trailing newline",
               waive=[{"id": "F1", "checked": True}])
    assert out["textareaValue"] == "no trailing newline\nwaive F1 (clause one (#111))\n"


def test_waive_toggle_round_trip_leaves_hand_typed_text_byte_identical():
    """Check then uncheck, for every finding in `_PQ`: the textarea returns
    to exactly what was typed -- blank lines, indentation and trailing
    whitespace included -- never a normalised copy of it."""
    typed = "\n  indented line  \n\n\nafter two blank lines\n\n"
    for f in _PQ["findings"]:
        out = _run(_PQ, textareaValue=typed,
                   waive=[{"id": f["id"], "checked": True},
                          {"id": f["id"], "checked": False}])
        assert out["textareaValue"] == typed, f["id"]


def test_waive_toggle_round_trip_preserves_an_unterminated_last_line():
    """cure-01-05 (adopted #270 D.14, F04): a check-then-uncheck round trip
    over operator prose whose LAST line carries no trailing newline at all
    must restore that prose byte-identically -- SPEC.md 11.3's own
    byte-identical toggle promise, not a copy that gained a trailing
    newline the operator never typed. The smallest reproducer from the
    review's own probe (`test_waive_roundtrip_preserves_unterminated_
    operator_prose`)."""
    typed = "Keep this exact paragraph."
    out = _run(_PQ, textareaValue=typed,
               waive=[{"id": "F1", "checked": True},
                      {"id": "F1", "checked": False}])
    assert out["textareaValue"] == typed


def test_unchecking_the_last_of_two_composed_lines_keeps_the_others_own_newline():
    """The companion case to the unterminated fix above: when the line
    removed is the LAST one but ANOTHER finding's own composed line
    remains right before it, that other line's own trailing newline is
    real (added when IT was composed) and must never be eaten too."""
    out = _run(_PQ, textareaValue="operator note\n",
               waive=[{"id": "F1", "checked": True},
                      {"id": "F2", "checked": True},
                      {"id": "F2", "checked": False}])
    assert out["textareaValue"] == "operator note\nwaive F1 (clause one (#111))\n"


def test_unchecking_keeps_blank_lines_around_the_removed_line():
    out = _run(_PQ, textareaValue="para one\n\npara two\n",
               waive=[{"id": "F1", "checked": True},
                      {"id": "F2", "checked": True},
                      {"id": "F1", "checked": False}])
    assert out["textareaValue"] == "para one\n\npara two\nwaive F2 (clause two (#112))\n"


def test_checking_a_waive_box_never_sends_any_request_by_itself():
    """The whole point of criterion (1): one click never waives a finding
    on its own -- it only edits the textarea. Exercised for every finding
    in `_PQ` (the full set this clause quantifies over), each toggled on
    then off, asserting zero network calls throughout."""
    for f in _PQ["findings"]:
        out = _run(_PQ, waive=[{"id": f["id"], "checked": True},
                                {"id": f["id"], "checked": False}])
        assert out["calls"] == [], f["id"]


# ---------------------------------------------------------------------------
# criterion (2)/(1): the panel carries its OWN visible answer box -- a
# ruling textarea and a name field -- alongside its findings (adopted #270
# D.14). A live browser probe on the shipped tree found neither inside
# `#parked-panel`; this replaces the retired
# `test_the_panel_writes_into_rule_text_not_a_second_textarea` pin, which
# asserted exactly the OPPOSITE of this now-required layout.
# ---------------------------------------------------------------------------


def test_the_panel_carries_its_own_visible_ruling_textarea_and_name_field():
    out = _run(_PQ)
    assert "parked-rule-text" in out["panelTextareaIds"]
    assert "parked-rule-name" in out["panelTextInputIds"]
    # never reaching into the separate Rule section's own fields
    src = APP_JS.read_text()
    start = src.index("function renderParkedPanel(")
    end = src.index("\n}\n", start) + 3
    body = src[start:end]
    assert 'getElementById("rule-text")' not in body
    assert 'getElementById("rule-name")' not in body


def test_parked_rule_text_is_a_12_row_resizable_textarea():
    src = APP_JS.read_text()
    assert 'h("textarea", {\n    id: "parked-rule-text"' in src \
        or 'id: "parked-rule-text"' in src
    m_start = src.index('id: "parked-rule-text"')
    block_start = src.rindex('h("textarea"', 0, m_start)
    block = src[block_start:src.index("}", m_start) + 1]
    assert 'rows: "12"' in block
    css = (APP_JS.parent / "style.css").read_text()
    start = css.index(".parked-answer textarea {")
    block = css[start:css.index("}", start) + 1]
    assert "resize: vertical" in block


def test_rule_text_stays_a_12_row_resizable_textarea():
    """The separate Rule section's own textarea (task 010) is unaffected
    by the panel gaining its own answer box."""
    src = APP_JS.read_text()
    assert 'h("textarea", { id: "rule-text"' in src
    assert 'rows: "12"' in src
    css = (APP_JS.parent / "style.css").read_text()
    start = css.index("form.rule textarea {")
    block = css[start:css.index("}", start) + 1]
    assert "resize: vertical" in block


def test_enter_in_rule_text_inserts_a_newline_and_never_submits():
    """`Enter` in a `<textarea>` inserts a newline natively and never
    triggers implicit form submission (HTML's implicit submission applies
    to single-line inputs only). So the requirement holds exactly when
    nothing in app.js overrides it: no key handler on `#rule-text` itself,
    no key listener anywhere in the bundle that could intercept `Enter` to
    submit, and the Rule form submits only on its own `submit` event. The
    panel's own `#parked-rule-text` is likewise a bare `<textarea>` with no
    key handler at all -- its own submit is only ever the button click."""
    import re
    src = APP_JS.read_text()
    m = re.search(r'h\("textarea", \{ id: "rule-text"([^}]*)\}', src)
    assert m, "rule-text is not an h(\"textarea\") element"
    attrs = m.group(1)
    assert 'rows: "12"' in attrs
    assert "onkey" not in attrs and "onsubmit" not in attrs
    assert not re.search(r'addEventListener\(\s*["\']key', src)
    # every inline key handler in the bundle is a dialog opener bound to a
    # clickable cell, never a submitter
    for handler in re.finditer(r'onkeydown\s*[:=]\s*\(ev\)\s*=>\s*\{([^}]*)\}', src):
        assert "submit" not in handler.group(1)
    assert 'getElementById("rule-form").addEventListener("submit"' in src


def test_parked_rule_text_has_no_key_handler_either():
    src = APP_JS.read_text()
    start = src.index("function renderParkedPanel(")
    end = src.index("\n}\n", start) + 3
    body = src[start:end]
    assert "onkey" not in body


# ---------------------------------------------------------------------------
# criterion (3): the two stated-effect actions
# ---------------------------------------------------------------------------


def test_submit_ruling_and_resume_posts_the_textarea_body_unchanged_to_rule():
    out = _run(_PQ, textareaValue="  a hand-typed ruling  \n",
               click="submit", responses=[{"ok": True, "body": {"file": "r1"}}])
    assert len(out["calls"]) == 1
    call = out["calls"][0]
    assert call["url"] == "/api/runs/run-x/rule"
    assert call["opts"]["method"] == "POST"
    posted = json.loads(call["opts"]["body"])
    # byte-for-byte: no trim, no reflow -- leading/trailing whitespace and
    # the final newline all reach `/rule` exactly as typed.
    assert posted == {"text": "  a hand-typed ruling  \n"}


def test_submit_posts_a_multi_paragraph_body_with_its_blank_lines_intact():
    body = "\n  first paragraph\n\n\nsecond paragraph\t\n\n"
    out = _run(_PQ, textareaValue=body, click="submit",
               responses=[{"ok": True, "body": {"file": "r1"}}])
    assert json.loads(out["calls"][0]["opts"]["body"]) == {"text": body}


def test_submit_after_waiving_posts_exactly_the_composed_textarea_body():
    out = _run(_PQ, textareaValue="first paragraph\n\nsecond paragraph\n",
               waive=[{"id": "F1", "checked": True}], click="submit",
               responses=[{"ok": True, "body": {"file": "r1"}}])
    assert json.loads(out["calls"][0]["opts"]["body"]) == {
        "text": "first paragraph\n\nsecond paragraph\nwaive F1 (clause one (#111))\n"}


def test_submit_ruling_and_resume_never_calls_unpause():
    out = _run(_PQ, textareaValue="a ruling\n", click="submit",
               responses=[{"ok": True, "body": {"file": "r1"}}])
    assert all("/unpause" not in c["url"] for c in out["calls"])


def test_submit_includes_the_entered_name():
    """cure-01-05 (adopted #270 D.14): a name typed into the panel's own
    `#parked-rule-name` rides along on `POST /rule` exactly the way the
    Rule form's own `#rule-name` already does -- never dropped."""
    out = _run(_PQ, textareaValue="chosen ruling", nameValue="operator-chosen-name",
               click="submit", responses=[{"ok": True, "body": {"file": "r1"}}])
    posted = json.loads(out["calls"][0]["opts"]["body"])
    assert posted == {"text": "chosen ruling", "name": "operator-chosen-name"}


def test_submit_posts_a_named_multiline_body_byte_identical():
    """cure-01-05 (#270 D.14, criterion 2): name AND a multi-line body with
    blank lines, an indented line and an unterminated last line together --
    both reach `POST /rule` exactly as entered, neither dropped nor
    rewritten."""
    body = "first line\n\n  indented second paragraph\t\nunterminated last"
    out = _run(_PQ, textareaValue=body, nameValue="named-multiline-ruling",
               click="submit", responses=[{"ok": True, "body": {"file": "r1"}}])
    assert len(out["calls"]) == 1
    assert out["calls"][0]["url"] == "/api/runs/run-x/rule"
    posted = json.loads(out["calls"][0]["opts"]["body"])
    assert posted == {"text": body, "name": "named-multiline-ruling"}


def test_submit_omits_name_when_the_panels_name_field_is_blank():
    out = _run(_PQ, textareaValue="chosen ruling", click="submit",
               responses=[{"ok": True, "body": {"file": "r1"}}])
    posted = json.loads(out["calls"][0]["opts"]["body"])
    assert "name" not in posted


def test_submit_clears_the_panels_own_name_field_on_success():
    out = _run(_PQ, textareaValue="chosen ruling", nameValue="a-name",
               click="submit", responses=[{"ok": True, "body": {"file": "r1"}}])
    assert out["nameValue"] == ""


def test_resume_without_a_ruling_posts_to_unpause():
    out = _run(_PQ, click="resume", responses=[{"ok": True, "body": {"resumed": True}}])
    assert len(out["calls"]) == 1
    call = out["calls"][0]
    assert call["url"] == "/api/runs/run-x/unpause"
    assert call["opts"]["method"] == "POST"


def test_resume_without_a_ruling_never_posts_to_rule():
    out = _run(_PQ, click="resume", responses=[{"ok": True, "body": {}}])
    assert all(c["url"] != "/api/runs/run-x/rule" for c in out["calls"])


def test_each_action_shows_its_effect_in_visible_text_before_any_click():
    out = _run(_PQ)
    assert "effect:" in out["cardText"]
    assert out["cardText"].count("effect:") == 2
    assert "posts the ruling text above" in out["cardText"]
    assert "resumes with no ruling recorded" in out["cardText"]


def test_resume_without_a_ruling_is_labelled_with_the_cure_pass_outcome():
    pq = json.loads(json.dumps(_PQ))
    pq["consequence"]["nextStepIfUnanswered"] = "cure_pass"
    out = _run(pq)
    assert "predicted outcome: a cure pass over this approach's blocking findings" \
        in out["cardText"]


def test_resume_without_a_ruling_is_labelled_with_the_archive_and_replan_outcome():
    """Rerun against the OTHER `nextStepIfUnanswered` value -- the full set
    task 007 defines -- so the label is pinned for both, never just one."""
    pq = json.loads(json.dumps(_PQ))
    pq["consequence"]["nextStepIfUnanswered"] = "archive_and_replan"
    out = _run(pq)
    assert "predicted outcome: this approach is archived and the run replans" \
        in out["cardText"]


def test_a_failed_submit_reports_the_engines_own_reason_and_keeps_the_text():
    out = _run(_PQ, textareaValue="a ruling\n", click="submit",
               responses=[{"ok": False, "status": 409,
                           "body": {"detail": "job finished"}}])
    assert out["statusText"] == "failed: job finished"
    assert out["textareaValue"] == "a ruling\n"  # never cleared on failure


def test_a_failed_resume_reports_the_engines_own_reason():
    out = _run(_PQ, click="resume",
               responses=[{"ok": False, "status": 503,
                           "body": {"error": "run's API is unreachable — cannot unpause"}}])
    assert "run's API is unreachable" in out["statusText"]


def test_submit_with_empty_ruling_text_is_refused_client_side_with_no_request():
    out = _run(_PQ, textareaValue="   \n", click="submit")
    assert out["calls"] == []
    assert out["statusText"] == "failed: ruling text required"
