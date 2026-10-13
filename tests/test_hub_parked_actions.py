"""Task 009 (v0.26, #269), reworked for v0.36 C.2/D.2: the parked panel's
answer controls -- a `waive` checkbox per finding that composes editable
note text and names the finding to waive, and the stated-effect answer
actions (`Answer: cure now`, `Answer: next approach`, `Answer: grant
approaches and replan`), all posting to `POST /api/runs/<id>/answer`. There
is no `Resume without a ruling` button (D.2) and a ruling alone releases
nothing.

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
  document.__els['parked-cure-rounds'] = { value: input.cureRounds || '1' };
  document.__els['parked-add-approaches'] = { value: input.addApproaches || '1' };
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

  // cure-01-01-6: an `append` action writes straight to the textarea's
  // own value, exactly as a hand-typed keystroke would -- never through
  // `onWaive`/`applyWaiveLine` -- so a test can put the panel back into
  // an unterminated state BETWEEN two checkbox toggles, the exact
  // interleaving that exposed the separator-tracking bug.
  for (const w of (input.waive || [])) {
    if (w.append !== undefined) {
      document.__els['parked-rule-text'].value += w.append;
      continue;
    }
    const cb = checkboxes.find((c) => c.attrs.id === 'waive-' + w.id);
    if (!cb) throw Error('no such waive checkbox: ' + w.id);
    cb.attrs.onchange({ target: { checked: w.checked } });
  }

  const buttons = card ? findAll(card, 'button') : [];
  const submitBtn = buttons.find((b) => b.attrs.id === 'parked-submit-answer');
  const nextBtn = buttons.find((b) => b.attrs.id === 'parked-next-approach');
  const grantBtn = buttons.find((b) => b.attrs.id === 'parked-grant-approaches');
  const resumeBtn = buttons.find((b) => b.attrs.id === 'parked-resume-no-ruling'
    || text(b).includes('Resume without a ruling'));
  const oldSubmitBtn = buttons.find((b) => b.attrs.id === 'parked-submit-ruling');
  if (input.click === 'submit') await submitBtn.attrs.onclick();
  if (input.click === 'next') await nextBtn.attrs.onclick();
  if (input.click === 'grant') await grantBtn.attrs.onclick();

  const statusEl = card ? find(card, (n) => n.attrs && n.attrs.id === 'parked-action-status') : null;

  console.log(JSON.stringify({
    textareaValue: document.__els['parked-rule-text'].value,
    nameValue: document.__els['parked-rule-name'].value,
    calls,
    statusText: statusEl ? statusEl.textContent || null : null,
    cardText: card ? text(card) : '',
    submitButtonPresent: !!submitBtn,
    nextButtonPresent: !!nextBtn,
    nextDisabled: nextBtn ? !!nextBtn.attrs.disabled : null,
    grantButtonPresent: !!grantBtn,
    resumeButtonPresent: !!resumeBtn,
    oldSubmitButtonPresent: !!oldSubmitBtn,
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


@pytest.mark.parametrize("typed", [
    "operator note\n",
    "\n",
    "first paragraph\n\nsecond paragraph\n",
], ids=["single-line", "newline-only", "multi-paragraph"])
def test_waive_toggle_round_trip_preserves_a_single_trailing_newline(typed):
    """cure-01-01-5 (B.3-B.5): the companion bug to the unterminated-line
    fix above -- operator prose that ALREADY ends in exactly one real
    newline is byte-identical to what `applyWaiveLine` produces right
    after inserting its own separator for UNTERMINATED prose, so a naive
    fix that always strips a lone trailing newline on uncheck instead
    silently eats the operator's own byte. The review's own probe found
    exactly this: checking then unchecking F1 turned "operator note\n"
    into "operator note" with zero requests. Exercised with multiple
    toggles (check, uncheck, check, uncheck again) since a stateful fix
    could get the FIRST round trip right and a later one wrong."""
    out = _run(_PQ, textareaValue=typed,
               waive=[{"id": "F1", "checked": True},
                      {"id": "F1", "checked": False},
                      {"id": "F1", "checked": True},
                      {"id": "F1", "checked": False}])
    assert out["textareaValue"] == typed


def test_editable_prose_still_supported_after_the_single_newline_fix():
    """The fix above must not turn the textarea read-only or otherwise
    special-case its content beyond the waive-line compose/remove
    contract: hand-typed prose typed AFTER a waive line is still exactly
    what `_run`'s stand-in wrote, and a second finding still composes
    correctly on top of it."""
    out = _run(_PQ, textareaValue="operator note\n",
               waive=[{"id": "F1", "checked": True},
                      {"id": "F2", "checked": True}])
    assert out["textareaValue"] == (
        "operator note\nwaive F1 (clause one (#111))\n"
        "waive F2 (clause two (#112))\n")


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


def test_uncheck_removes_own_line_not_a_matching_substring_in_prose():
    """cure-01-01-6 (B.3-B.4, D.1, DoD 2): the review's own probe found
    that unchecking fell back to a bare `indexOf(line + "\\n")` for a
    line that is NOT the textarea's last line -- a plain substring
    search with no line-boundary requirement, so a waive line's own text
    embedded inside an unrelated operator sentence ('Do not waive F1
    (clause one (#111))\\n' contains 'waive F1 (clause one (#111))\\n' as
    a bare substring, but never as its own line) got spliced out of that
    prose instead of the real composed line further down. Checking F1
    then F2, then unchecking F1, must remove only the LINE `applyWaiveLine`
    itself appended -- the operator's own sentence stays byte-identical."""
    body = "Do not waive F1 (clause one (#111))\nKeep this paragraph\n"
    out = _run(_PQ, textareaValue=body,
               waive=[{"id": "F1", "checked": True},
                      {"id": "F2", "checked": True},
                      {"id": "F1", "checked": False}])
    assert out["textareaValue"] == body + "waive F2 (clause two (#112))\n"
    assert out["calls"] == []


def test_second_waiver_after_an_operator_edit_is_separate_and_removable():
    """cure-01-01-6 (B.3-B.4, D.1, DoD 2): the companion bug -- separator
    insertion was tracked only at the instant the composed region grew
    from zero active lines to one, so a hand edit appended directly to
    the textarea BETWEEN two checks (leaving it unterminated again with
    one waive line already active) never got its own leading separator
    recorded, and a later check ran its composed line straight into the
    operator's own last word. Checking F1, appending an unterminated
    further explanation, then checking and unchecking F2 must leave the
    edited prose exactly as typed."""
    out = _run(_PQ, textareaValue="original\n",
               waive=[{"id": "F1", "checked": True},
                      {"append": "My further explanation"},
                      {"id": "F2", "checked": True},
                      {"id": "F2", "checked": False}])
    assert out["textareaValue"] == (
        "original\nwaive F1 (clause one (#111))\nMy further explanation")
    assert out["calls"] == []


def test_unchecking_the_earlier_of_two_composed_lines_keeps_the_later_removable():
    """cure-01-01-7 (B.3-B.4, D.1, DoD 2; cure-01-01-6): checking F1 (into
    unterminated prose, so F1's own insertion adds a leading separator)
    then F2, then unchecking F1 -- the EARLIER of the two -- must strip
    only F1's own composed line, never the separator its insertion added,
    because F2's still-active line now sits right where F1's used to and
    still needs that exact byte to stay off the operator's own prose. The
    old fix always stripped it, merging 'orig' straight into 'waive F2
    ...' with no separating byte at all."""
    out = _run(_PQ, textareaValue="orig",
               waive=[{"id": "F1", "checked": True},
                      {"id": "F2", "checked": True},
                      {"id": "F1", "checked": False}])
    assert out["textareaValue"] == "orig\nwaive F2 (clause two (#112))\n"
    assert out["calls"] == []


@pytest.mark.parametrize("order", [
    ["F1", "F2"],
    ["F2", "F1"],
], ids=["remove-earlier-first", "remove-later-first"])
def test_removing_every_composed_waiver_in_either_order_restores_the_original_bytes(order):
    """cure-01-01-7 (B.3-B.4, D.1, DoD 2; cure-01-01-6): the whole point
    of the fix above -- a separator a removed line correctly declines to
    strip (because another composed line still needs it) is not lost, it
    is inherited by that remaining line, so removing EVERY composed
    waiver -- in EITHER order -- always ends up back at the exact
    original, unterminated bytes, never a leftover synthetic newline and
    never a merged byte."""
    out = _run(_PQ, textareaValue="orig",
               waive=[{"id": "F1", "checked": True},
                      {"id": "F2", "checked": True},
                      {"id": order[0], "checked": False},
                      {"id": order[1], "checked": False}])
    assert out["textareaValue"] == "orig"
    assert out["calls"] == []


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
# v0.36 C.2: the answer actions, all through `POST /api/runs/<id>/answer`
# ---------------------------------------------------------------------------

_ANSWER = "/api/runs/run-x/answer"

_PQ_LAST = dict(_PQ, inputQuestion={
    "kind": "findings", "trigger": "cure_rounds_spent", "judge": "review",
    "approach": 2, "maxApproaches": 2, "cureRoundsUsed": 3,
    "cureRoundsAllowed": 3, "expiresAt": None, "onTimeout": "exhausted_approaches"})

_PQ_MID = dict(_PQ, inputQuestion=dict(_PQ_LAST["inputQuestion"], approach=1))


def _posted(out, index=0):
    call = out["calls"][index]
    assert call["opts"]["method"] == "POST"
    return call["url"], json.loads(call["opts"]["body"])


def test_answer_posts_the_note_unchanged_to_answer_with_default_cure_rounds():
    out = _run(_PQ_MID, textareaValue="  a hand-typed note  \n",
               click="submit", responses=[{"ok": True, "body": {"outcome": "cure"}}])
    assert len(out["calls"]) == 1
    url, body = _posted(out)
    assert url == _ANSWER
    # byte-for-byte: no trim, no reflow -- the note reaches `/answer` as typed.
    assert body == {"cureRounds": 1, "note": "  a hand-typed note  \n"}


def test_answer_with_no_note_posts_no_note_key():
    out = _run(_PQ_MID, click="submit",
               responses=[{"ok": True, "body": {"outcome": "cure"}}])
    assert _posted(out)[1] == {"cureRounds": 1}


def test_answer_posts_the_cure_rounds_the_operator_chose():
    out = _run(_PQ_MID, cureRounds="3", click="submit",
               responses=[{"ok": True, "body": {"outcome": "cure"}}])
    assert _posted(out)[1] == {"cureRounds": 3}


def test_ticked_waive_boxes_name_their_findings_in_the_answer():
    out = _run(_PQ_MID, textareaValue="first paragraph\n\nsecond paragraph\n",
               waive=[{"id": "F1", "checked": True}, {"id": "F2", "checked": True},
                      {"id": "F2", "checked": False}], click="submit",
               responses=[{"ok": True, "body": {"outcome": "cure"}}])
    assert _posted(out)[1] == {
        "cureRounds": 1, "waive": ["F1"],
        "note": "first paragraph\n\nsecond paragraph\nwaive F1 (clause one (#111))\n"}


def test_answer_includes_the_entered_name_only_with_a_note():
    out = _run(_PQ_MID, textareaValue="chosen note", nameValue="operator-chosen-name",
               click="submit", responses=[{"ok": True, "body": {}}])
    assert _posted(out)[1] == {
        "cureRounds": 1, "note": "chosen note", "name": "operator-chosen-name"}
    blank = _run(_PQ_MID, textareaValue="chosen note", click="submit",
                 responses=[{"ok": True, "body": {}}])
    assert "name" not in _posted(blank)[1]


def test_answer_clears_the_panels_own_fields_on_success_only():
    ok = _run(_PQ_MID, textareaValue="n", nameValue="a-name", click="submit",
              responses=[{"ok": True, "body": {"outcome": "cure"}}])
    assert ok["nameValue"] == "" and ok["textareaValue"] == ""
    assert ok["statusText"] == "answered: cure"
    failed = _run(_PQ_MID, textareaValue="a note\n", click="submit",
                  responses=[{"ok": False, "status": 409,
                              "body": {"detail": "no approach is left"}}])
    assert failed["statusText"] == "failed: no approach is left"
    assert failed["textareaValue"] == "a note\n"  # never cleared on failure


def test_next_approach_posts_cure_rounds_zero():
    out = _run(_PQ_MID, click="next",
               responses=[{"ok": True, "body": {"outcome": "next_approach"}}])
    url, body = _posted(out)
    assert url == _ANSWER and body == {"cureRounds": 0}


def test_next_approach_is_greyed_out_when_no_approach_is_left():
    mid = _run(_PQ_MID)
    last = _run(_PQ_LAST)
    assert mid["nextButtonPresent"] and mid["nextDisabled"] is False
    assert last["nextButtonPresent"] and last["nextDisabled"] is True
    assert "no approach is left" in last["cardText"]


def test_the_grant_approaches_option_appears_only_when_none_is_left():
    assert _run(_PQ_MID)["grantButtonPresent"] is False
    assert _run(_PQ_LAST)["grantButtonPresent"] is True


def test_grant_approaches_posts_cure_rounds_zero_and_the_count():
    out = _run(_PQ_LAST, addApproaches="2", click="grant",
               responses=[{"ok": True, "body": {"outcome": "next_approach"}}])
    url, body = _posted(out)
    assert url == _ANSWER and body == {"cureRounds": 0, "approaches": 2}


def test_a_replan_unavailable_payload_without_an_input_question_greys_next_approach():
    out = _run(dict(_PQ, replanAvailable=False, approach=None, maxApproaches=None))
    assert out["nextDisabled"] is True


def test_there_is_no_resume_without_a_ruling_button_and_no_unpause_call():
    out = _run(_PQ_MID, click="submit",
               responses=[{"ok": True, "body": {"outcome": "cure"}}])
    assert out["resumeButtonPresent"] is False
    assert out["oldSubmitButtonPresent"] is False
    assert "Resume without a ruling" not in out["cardText"]
    assert all("/unpause" not in c["url"] and not c["url"].endswith("/rule")
               for c in out["calls"])
    src = APP_JS.read_text()
    assert "Resume without a ruling" not in src
    assert "parked-resume-no-ruling" not in src


def test_each_action_shows_its_effect_in_visible_text_before_any_click():
    out = _run(_PQ_MID)
    assert out["cardText"].count("effect:") == 2
    assert "adds this many cure rounds to this approach" in out["cardText"]
    assert "archives this approach and replans the next one" in out["cardText"]
    last = _run(_PQ_LAST)
    assert last["cardText"].count("effect:") == 3
    assert "grants that many approaches" in last["cardText"]


def test_the_consequence_list_still_names_what_an_unanswered_park_does():
    for next_step, wording in (
            ("cure_pass", "if left unanswered: a cure pass over this approach's blocking findings"),
            ("archive_and_replan", "if left unanswered: this approach is archived and the run replans"),
            ("finishing", "if left unanswered: this approach is archived and the run ends (no approach left)")):
        pq = json.loads(json.dumps(_PQ_MID))
        pq["consequence"]["nextStepIfUnanswered"] = next_step
        out = _run(pq)
        assert wording in out["cardText"]
        assert "unknown (" not in out["cardText"]


def test_a_failed_answer_reports_the_engines_own_reason():
    out = _run(_PQ_MID, click="submit",
               responses=[{"ok": False, "status": 503,
                           "body": {"error": "run's API unreachable or rejected the request"}}])
    assert "run's API unreachable" in out["statusText"]


def test_a_residual_finding_carries_no_waive_checkbox():
    pq = json.loads(json.dumps(_PQ_MID))
    pq["findings"].append({"id": "R1", "severity": "residual", "clause": "nit",
                           "evidence": "e", "cured_when": None, "invalidates": []})
    out = _run(pq)
    assert out["checkboxIds"] == ["waive-F1", "waive-F2"]
