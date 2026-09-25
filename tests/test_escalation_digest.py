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
