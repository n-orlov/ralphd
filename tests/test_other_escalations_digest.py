"""Cure task cure-01-05-2 (PRD requirement A.1, D.1; DoD 1; cure-01-02
criterion 1): `_operator_escalation_digest_text` (cure-01-02) only ever
digested the four finding-bearing rejection reasons
(`operator_input_needed`, `rejected_repeated_signature`,
`rejected_last_approach`, `rejected_incurable`). Every OTHER real
operator-escalation pause this engine takes -- `review_contract_
unrepaired` and `review_findings_unusable` (v0.36 requirement F retired the
`stagnation`, `unschedulable_dependency_cycle` and
`unresolvable_dependency_edge` pauses: those self-heal and are covered by
`tests/test_self_heal_stagnation_graph.py`) -- still
posted the raw, un-digested escalation text through the generic
`_notify`/`MAX_NOTIFY_CHARS` truncator: no run id/`pauseReason` header, no
hub URL even when `hub_base_url` was configured, no honest "unavailable"
disclosure for the blocking/residual/reopen/insert fields the digest
contract now promises for every pause, and (for the two review-contract
faults) the raw validation-error sentinel reaching the 3500-char
truncation cap on anything but a short list.

This module drives each of the real pause paths end to end
(`_pause_and_escalate_*`) and asserts the notifier payload is the bounded
digest `_pause_escalation_digest_text` builds, while the `operator_
escalation` EVENT keeps every original field and the full diagnostic text
intact on disk.
"""

from __future__ import annotations

import json
from pathlib import Path

from ralphd.engine.loop import MAX_NOTIFY_CHARS
from tests.conftest import supervisor_factory


def _events(root: Path, type_: str) -> list[dict]:
    log = root / "events.jsonl"
    lines = log.read_text().splitlines() if log.exists() else []
    return [ev for ev in (json.loads(x) for x in lines) if ev.get("type") == type_]


class _StubNotifier:
    def __init__(self) -> None:
        self.messages: list[str] = []

    def __call__(self, text: str) -> None:
        self.messages.append(text)


_supervisor = supervisor_factory(
    run_id="cure-01-05-2-other-escalations", cfg_defaults=False,
    prd="# cure-01-05-2 fixture PRD\n\nDo the thing.\n",
    status={"state": "running"}, operator_input_needed_pause=False)


def _sup(root: Path, **cfg_kw):
    sup = _supervisor(root, max_approaches=3, **cfg_kw)
    sup._approach = 2
    notifier = _StubNotifier()
    sup.notifier = notifier
    return sup, notifier


def _assert_generic_digest_header(text: str, sup, reason: str) -> None:
    assert len(text) <= MAX_NOTIFY_CHARS
    assert sup.cfg.run_id in text
    assert f"pauseReason: {reason}" in text
    assert "approach 2 of 3" in text
    assert "blocking: unavailable  residual: unavailable" in text
    assert "tasks reopened: unavailable  tasks inserted: unavailable" in text
    assert "ralphctl" in text


# ===========================================================================
# 1. `review_contract_unrepaired` -- no verdict was ever rendered; the
#    contract errors are the whole diagnosis and used to reach the
#    notifier verbatim, unbounded.
# ===========================================================================


def test_review_contract_unrepaired_notification_is_a_bounded_digest(
        tmp_path):
    root = tmp_path / "run"
    sup, notifier = _sup(root, hub_base_url="http://hub.example:9")

    errors = [f"$.verdict: schema violation number {i}" for i in range(50)]
    sup._pause_and_escalate_review_contract_unrepaired(2, errors)

    assert len(notifier.messages) == 1
    text = notifier.messages[0]
    _assert_generic_digest_header(text, sup, "review_contract_unrepaired")
    assert "hub.example" in text
    assert f"ralphctl unpause {sup.cfg.run_id}" in text

    ev = _events(root, "operator_escalation")[-1]
    assert ev["reason"] == "review_contract_unrepaired"
    assert ev["contractErrors"] == errors
    assert all(e in ev["escalation"] for e in errors)


# ===========================================================================
# 2. `review_findings_unusable` -- sibling pause, same treatment.
# ===========================================================================


def test_review_findings_unusable_notification_is_a_bounded_digest(tmp_path):
    root = tmp_path / "run"
    sup, notifier = _sup(root, hub_base_url="http://hub.example:9")

    errors = [f"findings sidecar error {i}: stray field 'x{i}'"
              for i in range(50)]
    sup._pause_and_escalate_review_findings_unusable(2, errors)

    assert len(notifier.messages) == 1
    text = notifier.messages[0]
    _assert_generic_digest_header(text, sup, "review_findings_unusable")
    assert f"ralphctl unpause {sup.cfg.run_id}" in text

    ev = _events(root, "operator_escalation")[-1]
    assert ev["reason"] == "review_findings_unusable"
    assert ev["contractErrors"] == errors
    assert all(e in ev["escalation"] for e in errors)


# ===========================================================================
# 6. No hub line when `hub_base_url` is unconfigured -- same rule the four
#    finding-bearing digests already follow.
# ===========================================================================


def test_no_hub_line_when_unconfigured_for_the_five_other_pauses(tmp_path):
    root = tmp_path / "run"
    sup, notifier = _sup(root)

    sup._pause_and_escalate_review_contract_unrepaired(2, ["one error"])
    text = notifier.messages[-1]
    assert "hub:" not in text


# ===========================================================================
# 7. However huge the underlying fault (an unbounded contract-error list,
#    an unbounded stagnation outstanding-task list), the digest never
#    exceeds MAX_NOTIFY_CHARS and drops to a bounded count instead.
# ===========================================================================


def test_huge_contract_error_list_still_bounds_the_digest(tmp_path):
    root = tmp_path / "run"
    sup, notifier = _sup(root)

    errors = [f"error number {i}: " + ("X" * 200) for i in range(500)]
    sup._pause_and_escalate_review_contract_unrepaired(2, errors)

    text = notifier.messages[-1]
    assert len(text) <= MAX_NOTIFY_CHARS
    assert "more in the hub/events.jsonl" in text

    ev = _events(root, "operator_escalation")[-1]
    assert len(ev["contractErrors"]) == 500
    assert len(ev["escalation"]) > MAX_NOTIFY_CHARS
