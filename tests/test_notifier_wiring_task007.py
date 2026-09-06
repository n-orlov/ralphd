"""Task 007 (PRD requirement D.1, finding 5): the last-approach pause
escalation (task 011/036) reaches the CONFIGURED notifier through B.3's own
helper (`hat_tools.send_notification`), rather than the plain `self.notifier
= None` the escalation code had before this task -- production never wired
`cfg.notifier_url` into it at all.

This module drives a real `LoopSupervisor._step_review()` over a real run
dir, the same shape `tests/test_last_approach_pause_task011.py` and
`tests/test_last_approach_pause_persisted_task036.py` already use, and pins:

1. `JobConfig(notifier_url=...)` set: the escalation reaches that URL
   through `hat_tools.send_notification` -- a `notification.sent` event
   with the escalation text's `textSha256` lands in `events.jsonl` and in
   the run record's notifications section;
2. `notifier_url` unset: the escalation still emits `operator_escalation`
   and still pauses, but writes NO `notification.sent` event at all;
3. a poster that raises, and one that returns a non-2xx status, both still
   leave the pause outcome and the `operator_escalation` event unchanged
   (a broken/failing notifier is never a reason to lose the pause).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path

from ralphd.engine import findings, hat_tools, run_record
from ralphd.engine.config import JobConfig
from ralphd.engine.loop import LoopSupervisor
from ralphd.engine.runner import IterationResult
from ralphd.engine.state import RunDir
from tests.conftest import supervisor_factory


def _finding(severity: str, curable: bool, clause: str, evidence: str) -> dict:
    return {"severity": severity, "curable": curable, "clause": clause,
            "evidence": evidence}


def _result(text: str) -> IterationResult:
    r = IterationResult(exit_code=0)
    r.final_text = text
    r.duration_s = 5.0
    r.usage = {"input": 10, "output": 5, "totalTokens": 15}
    return r


_supervisor = supervisor_factory(run_id="task007-notifier-wiring", cfg_defaults=False,
                                 prd="# task 007 fixture PRD\n\nDo the thing.\n",
                                 status={"state": "running"}, max_approaches=1)


def _events(root: Path, type_: str) -> list[dict]:
    log = root / "events.jsonl"
    lines = log.read_text().splitlines() if log.exists() else []
    return [ev for ev in (json.loads(x) for x in lines)
            if ev.get("type") == type_]


def _trigger_last_approach_pause(sup: LoopSupervisor):
    sup._approach = 1
    sup.run.update_status(curePasses={"1": 1})
    still_curable = [_finding("blocking", True, "requirement D.1 (#142)",
                              "the notifier is still not wired in")]
    findings.write_findings_json(sup.run.findings_json_file, still_curable)

    async def _review_rejects(prompt, transcript, **kw):
        return _result(json.dumps({"verdict": "rejected", "reason": "no"}))

    sup.runner.run = _review_rejects  # type: ignore[method-assign]
    return asyncio.run(sup._step_review())


# ---------------------------------------------------------------------------
# 1. notifier_url SET -- reaches it through hat_tools.send_notification,
#    a notification.sent event lands in events.jsonl and in the run
#    record's notifications section.
# ---------------------------------------------------------------------------


def test_notifier_url_set_reaches_send_notification_and_is_recorded(
        tmp_path, monkeypatch):
    calls = []

    def stub_poster(url, text, timeout_s):
        calls.append((url, text, timeout_s))
        return 200, None

    monkeypatch.setattr(hat_tools, "_http_post_text", stub_poster)

    root = tmp_path / "run"
    sup = _supervisor(root, max_approaches=1,
                      notifier_url="http://notifier.example/notify")
    assert sup.notifier is not None

    outcome = _trigger_last_approach_pause(sup)
    assert outcome == ("advance", "paused", "rejected_last_approach")

    # reached through B.3's own helper, not a second HTTP client.
    assert len(calls) == 1
    posted_url, posted_text, _ = calls[0]
    assert posted_url == "http://notifier.example/notify"
    expected_sha = hashlib.sha256(posted_text.encode()).hexdigest()

    events = _events(root, hat_tools.NOTIFICATION_SENT_EVENT)
    assert len(events) == 1
    assert events[0]["textSha256"] == expected_sha
    assert events[0]["endpoint"] == "http://notifier.example/notify"
    assert events[0]["httpStatus"] == 200

    escalations = _events(root, "operator_escalation")
    assert len(escalations) == 1
    assert escalations[0]["reason"] == "rejected_last_approach"

    section = run_record.build_notifications_section(sup.run)
    assert len(section) == 1
    assert section[0]["textSha256"] == expected_sha


# ---------------------------------------------------------------------------
# 2. notifier_url UNSET -- still escalates and pauses, writes no
#    notification.sent event at all.
# ---------------------------------------------------------------------------


def test_notifier_url_unset_still_escalates_but_writes_no_event(tmp_path):
    root = tmp_path / "run"
    sup = _supervisor(root, max_approaches=1)
    assert sup.cfg.notifier_url is None
    assert sup.notifier is None

    outcome = _trigger_last_approach_pause(sup)
    assert outcome == ("advance", "paused", "rejected_last_approach")
    assert sup._pause.is_set() is False  # still pauses

    escalations = _events(root, "operator_escalation")
    assert len(escalations) == 1
    assert escalations[0]["reason"] == "rejected_last_approach"

    events = _events(root, hat_tools.NOTIFICATION_SENT_EVENT)
    assert events == []
    assert run_record.build_notifications_section(sup.run) == []


# ---------------------------------------------------------------------------
# 3. A raising poster, and a poster that returns a non-2xx status, both
#    leave the pause outcome and the operator_escalation event unchanged.
# ---------------------------------------------------------------------------


def test_a_raising_poster_still_leaves_the_pause_outcome_unchanged(
        tmp_path, monkeypatch):
    def raising_poster(url, text, timeout_s):
        raise RuntimeError("boom -- unexpected poster failure")

    monkeypatch.setattr(hat_tools, "_http_post_text", raising_poster)

    root = tmp_path / "run"
    sup = _supervisor(root, max_approaches=1,
                      notifier_url="http://notifier.example/notify")

    outcome = _trigger_last_approach_pause(sup)

    assert outcome == ("advance", "paused", "rejected_last_approach")
    assert sup._pause.is_set() is False

    escalations = _events(root, "operator_escalation")
    assert len(escalations) == 1
    assert escalations[0]["reason"] == "rejected_last_approach"

    status = sup.run.read_status()
    assert status.get("state") != "succeeded"
    assert status.get("state") != "failed"


def test_a_non_2xx_status_still_leaves_the_pause_outcome_unchanged(
        tmp_path, monkeypatch):
    def failing_poster(url, text, timeout_s):
        return 500, None

    monkeypatch.setattr(hat_tools, "_http_post_text", failing_poster)

    root = tmp_path / "run"
    sup = _supervisor(root, max_approaches=1,
                      notifier_url="http://notifier.example/notify")

    outcome = _trigger_last_approach_pause(sup)

    assert outcome == ("advance", "paused", "rejected_last_approach")
    assert sup._pause.is_set() is False

    escalations = _events(root, "operator_escalation")
    assert len(escalations) == 1
    assert escalations[0]["reason"] == "rejected_last_approach"

    # the failed send is still recorded, distinguishably, on
    # events.jsonl -- a non-2xx is a recorded outcome, never a reason to
    # silently lose the escalation.
    events = _events(root, hat_tools.NOTIFICATION_SENT_EVENT)
    assert len(events) == 1
    assert events[0]["httpStatus"] == 500
