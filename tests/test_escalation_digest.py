"""Task 001 (PRD requirement A, #269): every `self.notifier(...)` call site
in `engine/loop.py` is bounded by `MAX_NOTIFY_CHARS`, and the notification
posted for an `operator_input_needed`/`rejected_repeated_signature`
escalation is a digest -- run id, `pauseReason`, `approach n of m`,
blocking/residual counts, the A.7 preview numbers, one line per blocking
finding (id + `clause` only), the hub URL when configured, and the exact
command that answers the pause -- never the full per-finding evidence the
`operator_escalation` event itself still carries in full.

The #269 incident: a real escalation body reached 45,876 characters and
was refused `422` by the notifier's own 16,000-character cap, silently,
because `self.notifier(text)` (0.25.2) posted the full, un-digested
escalation text with no bound of its own.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ralphd.engine import findings
from ralphd.engine.loop import MAX_NOTIFY_CHARS
from tests.conftest import supervisor_factory


def _finding(clause: str, evidence_len: int = 5000) -> dict:
    return {
        "severity": "blocking",
        "curable": True,
        "clause": clause,
        "evidence": "E" * evidence_len,
        "cured_when": f"{clause} is discharged",
    }


def _residual(clause: str) -> dict:
    return {
        "severity": "residual",
        "curable": False,
        "clause": clause,
        "evidence": "residual evidence, still non-blocking",
    }


class _StubNotifier:
    def __init__(self) -> None:
        self.messages: list[str] = []

    def __call__(self, text: str) -> None:
        self.messages.append(text)


def _events(root: Path, type_: str) -> list[dict]:
    log = root / "events.jsonl"
    lines = log.read_text().splitlines() if log.exists() else []
    return [ev for ev in (json.loads(x) for x in lines) if ev.get("type") == type_]


_supervisor = supervisor_factory(
    run_id="task001-escalation-digest", cfg_defaults=False,
    prd="# task 001 fixture PRD\n\nDo the thing.\n",
    status={"state": "running"}, operator_input_needed_pause=False)


def _many_findings(n: int, evidence_len: int = 5000) -> list[dict]:
    blocking = [_finding(f"clause {i}", evidence_len) for i in range(n)]
    return blocking + [_residual("a disclosed residual, never counted as blocking")]


# ===========================================================================
# 1. `operator_input_needed`: the notifier payload is bounded, names the
#    run, the reason and the counts -- fails against 0.25.2's
#    `self.notifier(text)`, which posted the full, unbounded escalation
#    text (well over MAX_NOTIFY_CHARS for 100 findings x 5,000 chars each).
# ===========================================================================


def test_operator_input_needed_notification_is_bounded_and_names_run_reason_counts(
        tmp_path):
    root = tmp_path / "run"
    sup = _supervisor(root, max_approaches=3)
    sup._approach = 1
    notifier = _StubNotifier()
    sup.notifier = notifier

    findings_list = _many_findings(100)

    sup._pause_and_escalate_operator_input_needed(
        1, findings_list, replan_available=True)

    assert len(notifier.messages) == 1
    text = notifier.messages[0]
    assert len(text) <= MAX_NOTIFY_CHARS
    assert sup.cfg.run_id in text
    assert "operator_input_needed" in text
    assert "blocking: 100" in text
    assert "residual: 1" in text
    assert "approach 1 of 3" in text


# ===========================================================================
# 2. `rejected_repeated_signature`: same bound, same named fields.
# ===========================================================================


def test_repeated_signature_notification_is_bounded_and_names_run_reason_counts(
        tmp_path):
    root = tmp_path / "run"
    sup = _supervisor(root, max_approaches=3)
    sup._approach = 2
    notifier = _StubNotifier()
    sup.notifier = notifier

    findings_list = _many_findings(100)
    findings.write_findings_json(sup.run.findings_json_file, findings_list)

    sup._pause_and_escalate_repeated_rejection_signature(2, "sig-abc")

    assert len(notifier.messages) == 1
    text = notifier.messages[0]
    assert len(text) <= MAX_NOTIFY_CHARS
    assert sup.cfg.run_id in text
    assert "rejected_repeated_signature" in text
    assert "blocking: 100" in text
    assert "residual: 1" in text
    assert "approach 2 of 3" in text


# ===========================================================================
# 3. The event keeps every field it has at 0.25.2, full evidence included,
#    while the notification carries none of that evidence.
# ===========================================================================


def test_event_keeps_full_findings_while_notification_carries_no_evidence(
        tmp_path):
    root = tmp_path / "run"
    sup = _supervisor(root, max_approaches=3)
    sup._approach = 1
    notifier = _StubNotifier()
    sup.notifier = notifier

    findings_list = _many_findings(100)

    sup._pause_and_escalate_operator_input_needed(
        1, findings_list, replan_available=True)

    escalations = _events(root, "operator_escalation")
    assert len(escalations) == 1
    ev = escalations[0]
    # 0.25.2's own fields, unedited.
    assert ev["reason"] == "operator_input_needed"
    assert ev["approach"] == 1
    assert ev["replanAvailable"] is True
    assert "escalation" in ev
    # every blocking finding's full evidence survives on the event.
    blocking_findings = ev["findings"]
    assert len(blocking_findings) == 100
    for f in blocking_findings:
        assert len(f["evidence"]) == 5000
        assert f["evidence"] == "E" * 5000
    # the full escalation text (event side) still carries every finding's
    # evidence verbatim too -- only the POSTED notification is digested.
    assert blocking_findings[0]["evidence"] in ev["escalation"]

    text = notifier.messages[0]
    for f in blocking_findings:
        assert f["evidence"] not in text


def test_repeated_signature_event_keeps_full_findings_too(tmp_path):
    root = tmp_path / "run"
    sup = _supervisor(root, max_approaches=3)
    sup._approach = 2
    notifier = _StubNotifier()
    sup.notifier = notifier

    findings_list = _many_findings(100)
    findings.write_findings_json(sup.run.findings_json_file, findings_list)

    sup._pause_and_escalate_repeated_rejection_signature(2, "sig-xyz")

    escalations = _events(root, "operator_escalation")
    assert len(escalations) == 1
    ev = escalations[0]
    blocking_findings = ev["findings"]
    assert len(blocking_findings) == 100
    for f in blocking_findings:
        assert f["evidence"] == "E" * 5000

    text = notifier.messages[0]
    for f in blocking_findings:
        assert f["evidence"] not in text


# ===========================================================================
# 4. Small escalations still list each finding by id + clause, and the hub
#    URL/answering command are present exactly when expected.
# ===========================================================================


def test_small_escalation_lists_finding_ids_and_clauses_and_the_answer_command(
        tmp_path):
    root = tmp_path / "run"
    sup = _supervisor(root, max_approaches=2, hub_base_url="http://hub.example:9")
    sup._approach = 1
    notifier = _StubNotifier()
    sup.notifier = notifier

    findings_list = [_finding("PRD requirement A: some clause", 200)]

    sup._pause_and_escalate_operator_input_needed(
        1, findings_list, replan_available=True)

    text = notifier.messages[0]
    assert "F1: PRD requirement A: some clause" in text
    assert "hub.example" in text
    assert sup.cfg.run_id in text
    assert "ralphctl" in text


def test_per_finding_lines_overflow_to_a_count_and_hub_pointer(tmp_path):
    root = tmp_path / "run"
    sup = _supervisor(root, max_approaches=3)
    sup._approach = 1
    notifier = _StubNotifier()
    sup.notifier = notifier

    # Long clauses (not evidence -- the digest never carries evidence at
    # all) so 100 per-finding lines alone would blow past MAX_NOTIFY_CHARS.
    findings_list = [
        _finding("a long PRD clause text repeated to force overflow " * 3
                 + f"#{i}", evidence_len=50)
        for i in range(100)]

    sup._pause_and_escalate_operator_input_needed(
        1, findings_list, replan_available=True)

    text = notifier.messages[0]
    assert len(text) <= MAX_NOTIFY_CHARS
    assert "blocking: 100" in text
    assert sup.cfg.run_id in text
    assert "operator_input_needed" in text
    assert " more findings in the hub" in text
    # the counts are never dropped to make room for a partial finding list.
    assert "blocking: 100" in text and "residual:" in text


def test_no_hub_line_when_hub_base_url_unconfigured(tmp_path):
    root = tmp_path / "run"
    sup = _supervisor(root, max_approaches=2)
    sup._approach = 1
    notifier = _StubNotifier()
    sup.notifier = notifier

    sup._pause_and_escalate_operator_input_needed(
        1, [_finding("clause X", 100)], replan_available=True)

    text = notifier.messages[0]
    assert "hub:" not in text


# ===========================================================================
# 5. Every other `self.notifier(...)` call site delivers through the same
#    bounding path: an oversized ad-hoc text is still capped by
#    `MAX_NOTIFY_CHARS` when posted.
# ===========================================================================


def test_last_approach_rejection_notification_is_bounded_too(tmp_path):
    root = tmp_path / "run"
    sup = _supervisor(root, max_approaches=2)
    sup._approach = 1
    notifier = _StubNotifier()
    sup.notifier = notifier

    huge_findings = _many_findings(200, evidence_len=5000)
    findings.write_findings_json(sup.run.findings_json_file, huge_findings)

    sup._pause_and_escalate_last_approach_rejection(1)

    assert len(notifier.messages) == 1
    assert len(notifier.messages[0]) <= MAX_NOTIFY_CHARS


def test_max_notify_chars_is_a_small_int_constant():
    assert isinstance(MAX_NOTIFY_CHARS, int)
    assert 0 < MAX_NOTIFY_CHARS < 16000


# ===========================================================================
# 6. Task 002 (#269 follow-on): the FULL escalation text (event side, never
#    digested) already prints every blocking finding's evidence once on its
#    own `evidence:` line -- the `resolve with:` line right below it used to
#    repeat that same evidence verbatim inside the `--waive` argument
#    (`_amend_invocation_for_finding`). It now names the finding by id
#    (`F<n>`, matching the `Finding <n> (...)` header above it) instead, so
#    no finding's evidence string appears more than once anywhere in the
#    text any of the four escalation-text builders produce.
# ===========================================================================


def _distinct_evidence_findings(n: int) -> list[dict]:
    return [
        {
            "severity": "blocking",
            "curable": True,
            "clause": f"clause {i}",
            "evidence": f"distinct evidence text number {i}",
            "cured_when": f"clause {i} is discharged",
        }
        for i in range(n)
    ]


def test_no_builder_repeats_a_findings_evidence_in_its_own_text(tmp_path):
    root = tmp_path / "run"
    sup = _supervisor(root, max_approaches=3)
    sup._approach = 2

    blocking = _distinct_evidence_findings(3)

    texts = {
        "last_approach": sup._last_approach_rejection_escalation_text(
            2, blocking),
        "repeated_signature": sup._repeated_rejection_signature_escalation_text(
            2, blocking, "sig-1"),
        "operator_input_needed": sup._operator_input_needed_escalation_text(
            2, blocking, replan_available=True),
        "incurable": sup._incurable_rejection_escalation_text(2, blocking),
    }

    for name, text in texts.items():
        for f in blocking:
            assert text.count(f["evidence"]) <= 1, (
                f"{name} builder repeated finding evidence "
                f"{f['evidence']!r} -- it should appear at most once "
                "(on the finding's own `evidence:` line), never again "
                "inside the `resolve with:` line")
        resolve_lines = [line for line in text.splitlines()
                        if line.strip().startswith("resolve with:")]
        assert len(resolve_lines) == len(blocking), name
        for index, (line, finding) in enumerate(
                zip(resolve_lines, blocking), start=1):
            assert finding["evidence"] not in line, name
            # named by id -- the same `F<index>` the `Finding <index>`
            # header above it uses.
            assert f"F{index}" in line, name


# ===========================================================================
# 7. Cure task cure-01-02 (PRD requirement A.1/A.2/A.6, D.1, #269, review
#    findings F(last_approach)/F(incurable)/F(repeated-signature-command)):
#    `rejected_last_approach` and `rejected_incurable` are ruling-answerable
#    escalations whose blocking findings carry a real `evidence` clause --
#    exactly the same shape of risk `operator_input_needed`/`rejected_
#    repeated_signature` already digest -- so they now go through the SAME
#    evidence-free digest, never the full per-finding text. And every
#    digest this helper renders -- `rejected_repeated_signature` included --
#    names BOTH live-run levers (`ralphctl rule ...`/`ralphctl unpause
#    ...`), since every reason it covers waits on an operator ruling
#    (SPEC.md \u00a74.6).
# ===========================================================================


def test_last_approach_notification_is_an_evidence_free_digest(tmp_path):
    root = tmp_path / "run"
    sup = _supervisor(root, max_approaches=2)
    sup._approach = 1
    notifier = _StubNotifier()
    sup.notifier = notifier

    data = [_finding("requirement C.1 (#141)", evidence_len=200)]
    findings.write_findings_json(sup.run.findings_json_file, data)

    sup._pause_and_escalate_last_approach_rejection(1)

    escalations = _events(root, "operator_escalation")
    assert len(escalations) == 1
    # the event's own `escalation` field keeps the FULL text, evidence
    # included -- only the POSTED notification is digested.
    assert data[0]["evidence"] in escalations[0]["escalation"]

    assert len(notifier.messages) == 1
    text = notifier.messages[0]
    assert data[0]["evidence"] not in text
    assert "requirement C.1 (#141)" in text
    assert "blocking: 1" in text and "residual: 0" in text
    assert "resolve with:" not in text
    assert (f"ralphctl rule {sup.cfg.run_id} --file <ruling.md> "
            "--name <slug>") in text
    assert f"ralphctl unpause {sup.cfg.run_id}" in text


def test_incurable_notification_is_an_evidence_free_digest(tmp_path):
    root = tmp_path / "run"
    sup = _supervisor(root, max_approaches=2)
    sup._approach = 1
    notifier = _StubNotifier()
    sup.notifier = notifier

    data = [{
        "severity": "blocking", "curable": False,
        "clause": "requirement B (#111)",
        "evidence": "the endpoint has no docker.sock in this container",
    }]
    findings.write_findings_json(sup.run.findings_json_file, data)

    sup._pause_and_escalate_incurable_rejection(1)

    escalations = _events(root, "operator_escalation")
    assert len(escalations) == 1
    assert data[0]["evidence"] in escalations[0]["escalation"]

    assert len(notifier.messages) == 1
    text = notifier.messages[0]
    assert data[0]["evidence"] not in text
    assert "requirement B (#111)" in text
    assert "blocking: 1" in text and "residual: 0" in text
    assert (f"ralphctl rule {sup.cfg.run_id} --file <ruling.md> "
            "--name <slug>") in text
    assert f"ralphctl unpause {sup.cfg.run_id}" in text


def test_repeated_signature_digest_names_the_ruling_command_too(tmp_path):
    root = tmp_path / "run"
    sup = _supervisor(root, max_approaches=2)
    text = sup._operator_escalation_digest_text(
        "rejected_repeated_signature", 1, [_finding("clause Z", 50)])
    assert (f"ralphctl rule {sup.cfg.run_id} --file <ruling.md> "
            "--name <slug>") in text
    assert f"ralphctl unpause {sup.cfg.run_id}" in text


# ===========================================================================
# 8. Cure task cure-01-02, retry (PRD requirement A.1/A.7, #269): the A.7
#    preview numbers a POSTED digest carries agree with the `operator_
#    escalation` event it announces -- for a caller-passed `cure_preview`
#    AND for a pause handed none (the event then builds its A.7 fields off
#    `_effective_cure_preview`'s own walk, and so must the digest) -- and
#    the incurable path's digest is bounded at `MAX_NOTIFY_CHARS` too.
# ===========================================================================


def _preview_line(event: dict) -> str:
    return (f"tasks reopened: {len(event['taskIdsValidatedToPending'])}  "
            f"tasks inserted: {event['insertCount']}")


def test_operator_input_needed_digest_carries_the_passed_preview_numbers(
        tmp_path):
    root = tmp_path / "run"
    sup = _supervisor(root, max_approaches=3)
    sup._approach = 1
    notifier = _StubNotifier()
    sup.notifier = notifier

    preview = {"reopenedTaskIds": ["t-a", "t-b"], "insertCount": 3}
    sup._pause_and_escalate_operator_input_needed(
        1, [_finding("clause P", 100)], replan_available=True,
        cure_preview=preview)

    ev = _events(root, "operator_escalation")[-1]
    assert ev["taskIdsValidatedToPending"] == ["t-a", "t-b"]
    assert ev["insertCount"] == 3
    text = notifier.messages[0]
    assert "tasks reopened: 2  tasks inserted: 3" in text
    assert _preview_line(ev) in text


def test_repeated_signature_digest_carries_the_passed_preview_numbers(
        tmp_path):
    root = tmp_path / "run"
    sup = _supervisor(root, max_approaches=3)
    sup._approach = 2
    notifier = _StubNotifier()
    sup.notifier = notifier

    findings.write_findings_json(
        sup.run.findings_json_file, [_finding("clause Q", 100)])
    preview = {"reopenedTaskIds": ["t-x"], "insertCount": 4}
    sup._pause_and_escalate_repeated_rejection_signature(
        2, "sig-preview", cure_preview=preview)

    ev = _events(root, "operator_escalation")[-1]
    assert ev["taskIdsValidatedToPending"] == ["t-x"]
    assert ev["insertCount"] == 4
    text = notifier.messages[0]
    assert "tasks reopened: 1  tasks inserted: 4" in text
    assert _preview_line(ev) in text


@pytest.mark.parametrize(
    "reason", ["operator_input_needed", "rejected_repeated_signature"])
def test_digest_preview_agrees_with_event_when_no_preview_is_passed(
        tmp_path, reason):
    root = tmp_path / "run"
    sup = _supervisor(root, max_approaches=3)
    sup._approach = 1
    notifier = _StubNotifier()
    sup.notifier = notifier

    data = [_finding("clause R", 100)]
    findings.write_findings_json(sup.run.findings_json_file, data)
    if reason == "operator_input_needed":
        sup._pause_and_escalate_operator_input_needed(
            1, data, replan_available=True)
    else:
        sup._pause_and_escalate_repeated_rejection_signature(1, "sig-none")

    ev = _events(root, "operator_escalation")[-1]
    # one curable blocking finding and no task in the plan: the event's own
    # effective preview predicts one inserted cure task.
    assert ev["insertCount"] == 1
    text = notifier.messages[0]
    assert "tasks inserted: 1" in text
    assert _preview_line(ev) in text


@pytest.mark.parametrize("kind", ["last_approach", "incurable"])
def test_non_cure_pause_digest_previews_no_cure_pass(tmp_path, kind):
    """Neither `rejected_last_approach` nor `rejected_incurable` previews a
    cure pass that will run, so the digest's A.7 numbers read zero."""
    root = tmp_path / "run"
    sup = _supervisor(root, max_approaches=2)
    sup._approach = 1
    notifier = _StubNotifier()
    sup.notifier = notifier

    data = [_finding("clause S", 100)]
    if kind == "incurable":
        data[0]["curable"] = False
    findings.write_findings_json(sup.run.findings_json_file, data)
    if kind == "last_approach":
        sup._pause_and_escalate_last_approach_rejection(1)
    else:
        sup._pause_and_escalate_incurable_rejection(1)

    text = notifier.messages[0]
    assert "tasks reopened: 0  tasks inserted: 0" in text
    assert "spent this approach: unavailable (unavailable tokens)" in text


def test_incurable_rejection_notification_is_bounded_too(tmp_path):
    root = tmp_path / "run"
    sup = _supervisor(root, max_approaches=2)
    sup._approach = 1
    notifier = _StubNotifier()
    sup.notifier = notifier

    huge = [{
        "severity": "blocking", "curable": False,
        "clause": f"incurable clause {i}",
        "evidence": f"INCURABLE-EVIDENCE-{i}-" + "E" * 5000,
    } for i in range(200)]
    huge.append(_residual("a disclosed residual"))
    findings.write_findings_json(sup.run.findings_json_file, huge)

    sup._pause_and_escalate_incurable_rejection(1)

    ev = _events(root, "operator_escalation")[-1]
    assert ev["reason"] == "rejected_incurable"
    # the event keeps every finding's full evidence.
    assert huge[0]["evidence"] in ev["escalation"]
    assert len(ev["escalation"]) > MAX_NOTIFY_CHARS

    assert len(notifier.messages) == 1
    text = notifier.messages[0]
    assert len(text) <= MAX_NOTIFY_CHARS
    assert "INCURABLE-EVIDENCE-" not in text
    assert "blocking: 200  residual: 1" in text
    assert " more findings in the hub" in text
    assert f"ralphctl unpause {sup.cfg.run_id}" in text


# ===========================================================================
# 9. SPEC.md 4.6: the FULL `operator_escalation.escalation` text for the
#    last-approach and incurable reasons carries each blocking finding's
#    own `cured_when` verbatim (beside clause/evidence), like the other two
#    digested reasons -- while the POSTED digest carries neither the
#    evidence nor the cured_when. Fails at b7fb4dec, whose two builders
#    rendered clause/evidence/resolve-with only.
# ===========================================================================


@pytest.mark.parametrize("reason", ["last_approach", "incurable"])
def test_full_escalation_text_names_each_findings_cured_when(tmp_path, reason):
    root = tmp_path / "run"
    sup = _supervisor(root, max_approaches=2)
    sup._approach = 1
    notifier = _StubNotifier()
    sup.notifier = notifier

    cured = f"CURED-WHEN-SENTINEL-{reason}: the probe passes on a clean tree"
    data = [{
        "severity": "blocking", "curable": reason == "last_approach",
        "clause": "requirement Z.9 (#999)",
        "evidence": f"EVIDENCE-SENTINEL-{reason}",
        "cured_when": cured,
    }]
    findings.write_findings_json(sup.run.findings_json_file, data)

    if reason == "last_approach":
        sup._pause_and_escalate_last_approach_rejection(1)
    else:
        sup._pause_and_escalate_incurable_rejection(1)

    ev = _events(root, "operator_escalation")[-1]
    assert f"  cured_when: {cured}" in ev["escalation"]
    assert data[0]["evidence"] in ev["escalation"]

    assert len(notifier.messages) == 1
    text = notifier.messages[0]
    assert cured not in text
    assert data[0]["evidence"] not in text
    assert "requirement Z.9 (#999)" in text


def test_incurable_full_text_omits_cured_when_line_when_finding_has_none(
        tmp_path):
    root = tmp_path / "run"
    sup = _supervisor(root, max_approaches=2)
    sup._approach = 1
    data = [{
        "severity": "blocking", "curable": False,
        "clause": "requirement B (#111)",
        "evidence": "no docker.sock in this container",
    }]
    findings.write_findings_json(sup.run.findings_json_file, data)

    sup._pause_and_escalate_incurable_rejection(1)

    ev = _events(root, "operator_escalation")[-1]
    assert "cured_when:" not in ev["escalation"]
    assert data[0]["evidence"] in ev["escalation"]
