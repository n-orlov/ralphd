"""Task 004 (PRD requirement A.4, #269): "the escalation path reads the send
result. A failed send is retried with bounded backoff (at most 3 attempts,
within a few minutes in total). If the last attempt fails, the engine emits
a distinct event, `notification.escalation_undelivered`. The honest
`notification.sent`/`notification.failed` pair (#197) is unchanged."

`LoopSupervisor._notify_escalation_delivery` (`engine/loop.py`) is the new
seam: every existing `self.pause(reason=...)` + `self.notifier` escalation
site now calls it instead of `self._notify(text)` directly. It retries the
SAME `self._notify`/`self.notifier` path -- never a second HTTP client --
up to `_ESCALATION_DELIVERY_MAX_ATTEMPTS` (3) OUTER attempts (each still a
whole call to `hat_tools.send_notification`, which itself already retries
ONCE, immediately, with no backoff -- task 017/requirement A.7, unchanged
by this task and orthogonal to it), waiting `_ESCALATION_DELIVERY_BACKOFF_S`
(60s, then 120s -- 180s total, inside the PRD's "a few minutes" and its own
300s ceiling) between a failed OUTER attempt and the next, through
`_sleep_before_escalation_retry` (stubbed at the class level for the whole
suite, `tests/conftest.py`, so no other test pays this wait either -- this
module's own nodes below stub the INSTANCE attribute to observe the calls,
which shadows that class-level stub).

At 9b72b700 there is no `_notify_escalation_delivery`, no
`_ESCALATION_DELIVERY_MAX_ATTEMPTS`/`_ESCALATION_DELIVERY_BACKOFF_S`, and no
`notification.escalation_undelivered` event -- every one of this module's
nodes fails against that checkout (`AttributeError` on the missing
method/constants, or the undelivered event simply never appears).
"""

from __future__ import annotations

import json
from pathlib import Path

from ralphd.engine import hat_tools, run_record
from tests.conftest import supervisor_factory

_supervisor = supervisor_factory(
    run_id="task004-escalation-delivery", cfg_defaults=False,
    prd="# task 004 fixture PRD\n\nDo the thing.\n",
    status={"state": "running"}, max_approaches=1,
    operator_input_needed_pause=False)


def _events(root: Path, type_: str) -> list[dict]:
    log = root / "events.jsonl"
    lines = log.read_text().splitlines() if log.exists() else []
    return [ev for ev in (json.loads(x) for x in lines) if ev.get("type") == type_]


def _status(root: Path) -> dict:
    """A HOST-style read straight off disk, never through the supervisor's
    own in-memory state -- exactly what an operator polling `ralphctl
    status` would see."""
    return json.loads((root / "status.json").read_text())


def _trigger(sup):
    """Drive the REAL `rejected_last_approach` pause + escalation path
    (`_pause_and_escalate_last_approach_rejection`) -- the same production
    method `tests/test_last_approach.py`'s own notifier-wiring nodes drive,
    just called directly rather than through a full `_step_review()`."""
    sup._approach = 1
    sup._pause_and_escalate_last_approach_rejection(1)


def _count_send_notification_calls(monkeypatch) -> list[int]:
    """Wraps the REAL `hat_tools.send_notification` to count OUTER
    attempts -- one per call, whatever it does internally (including its
    own task 017/A.7 retry-once) -- without changing its behaviour at
    all."""
    calls: list[int] = []
    real_send = hat_tools.send_notification

    def counting_send(run, text, url, **kw):
        calls.append(1)
        return real_send(run, text, url, **kw)

    monkeypatch.setattr(hat_tools, "send_notification", counting_send)
    return calls


def test_notifier_failing_twice_then_succeeding_delivers_once(
        tmp_path, monkeypatch):
    outer_calls = _count_send_notification_calls(monkeypatch)
    sleeps = []
    poster_calls = []

    def stub_poster(url, text, timeout_s):
        poster_calls.append(1)
        # The first two OUTER attempts must each fail (each may itself
        # retry once internally, task 017/A.7); the third succeeds on its
        # very first poster call.
        if len(poster_calls) <= 4:
            return 500, None
        return 200, None

    monkeypatch.setattr(hat_tools, "_http_post_text", stub_poster)

    root = tmp_path / "run"
    sup = _supervisor(root, notifier_url="http://notifier.example/notify")
    sup._sleep_before_escalation_retry = sleeps.append  # backoff sleep stubbed

    _trigger(sup)

    # Exactly 3 OUTER attempts: 2 failures, then the third delivers.
    assert len(outer_calls) == 3
    assert sleeps == [60.0, 120.0]

    sent = _events(root, run_record.NOTIFICATION_SENT_EVENT)
    failed = _events(root, run_record.NOTIFICATION_FAILED_EVENT)
    assert len(sent) == 1
    assert len(failed) == 2

    # Delivered -- no undelivered record.
    undelivered = _events(root, run_record.NOTIFICATION_ESCALATION_UNDELIVERED_EVENT)
    assert undelivered == []

    status = _status(root)
    assert status["state"] == "paused"
    assert status["pauseReason"] == "rejected_last_approach"


def test_always_failing_notifier_is_recorded_undelivered_after_three_attempts(
        tmp_path, monkeypatch):
    outer_calls = _count_send_notification_calls(monkeypatch)
    sleeps = []

    def failing_poster(url, text, timeout_s):
        return 500, None

    monkeypatch.setattr(hat_tools, "_http_post_text", failing_poster)

    root = tmp_path / "run"
    sup = _supervisor(root, notifier_url="http://notifier.example/notify")
    sup._sleep_before_escalation_retry = sleeps.append  # backoff sleep stubbed

    _trigger(sup)

    # At most 3 OUTER attempts, at most 300s of total backoff.
    assert len(outer_calls) <= 3
    assert sum(sleeps) <= 300.0

    failed = _events(root, run_record.NOTIFICATION_FAILED_EVENT)
    assert len(failed) == len(outer_calls)
    assert _events(root, run_record.NOTIFICATION_SENT_EVENT) == []

    undelivered = _events(root, run_record.NOTIFICATION_ESCALATION_UNDELIVERED_EVENT)
    assert len(undelivered) == 1
    assert undelivered[0]["reason"] == "rejected_last_approach"


class _RecordingPoster:
    """A poster that fails its first two calls and then answers
    `final_status` on the third -- and, on that very FIRST call, snapshots
    `status.json` off disk before returning, so the assertion below reads
    exactly what an operator polling `ralphctl status` would have seen at
    that instant."""

    def __init__(self, root: Path, final_status: int) -> None:
        self.root = root
        self.final_status = final_status
        self.calls = 0
        self.seen_at_first_attempt: dict | None = None

    def __call__(self, url, text, timeout_s):
        self.calls += 1
        if self.calls == 1:
            self.seen_at_first_attempt = dict(_status(self.root))
        if self.calls < 3:
            return 500, None
        return self.final_status, None


def test_pause_takes_effect_regardless_of_delivery_retries(tmp_path, monkeypatch):
    """Neither fixture above ever blocks the pause from taking effect --
    `status.json` already reads `state: "paused"` with its `pauseReason`
    the very first time a poster is ever called, well before the retry
    loop even considers a second attempt."""
    for final_status, expect_undelivered in ((200, False), (500, True)):
        root = tmp_path / f"run-{final_status}"
        poster = _RecordingPoster(root, final_status)

        monkeypatch.setattr(hat_tools, "_http_post_text", poster)
        sup = _supervisor(root, notifier_url="http://notifier.example/notify")
        sup._sleep_before_escalation_retry = lambda seconds: None

        _trigger(sup)

        assert poster.seen_at_first_attempt is not None
        assert poster.seen_at_first_attempt["state"] == "paused"
        assert poster.seen_at_first_attempt["pauseReason"] == "rejected_last_approach"

        undelivered = _events(root, run_record.NOTIFICATION_ESCALATION_UNDELIVERED_EVENT)
        assert (len(undelivered) == 1) == expect_undelivered


def test_incurable_rejection_pause_is_in_memory_and_escalated_before_delivery(
        tmp_path, monkeypatch):
    """`rejected_incurable` is the ONE escalation site whose pause carries no
    reason (`_pause_and_escalate_incurable_rejection` calls the bare
    `self.pause()`, the same in-memory hold `POST /pause` takes), so SPEC
    §4.6 promises it exactly this, and no more: the in-memory pause is
    already in force and the `operator_escalation` event is already on
    disk when the first delivery attempt runs, while status.json is not
    rewritten to `paused` (`pauseReason` stays null). Its delivery is still
    retried and still recorded undelivered, like every other site."""
    from ralphd.engine import findings

    root = tmp_path / "run"
    sup = _supervisor(root, notifier_url="http://notifier.example/notify")
    sup._sleep_before_escalation_retry = lambda seconds: None
    sup._approach = 1
    findings.write_findings_json(sup.run.findings_json_file, [
        {"severity": "blocking", "curable": False, "clause": "C.1",
         "evidence": "needs hardware this sandbox lacks"}])

    seen: list[dict] = []

    def failing_poster(url, text, timeout_s):
        if not seen:
            seen.append({
                "gate_open": sup._pause.is_set(),
                "escalations": _events(root, "operator_escalation"),
                "status": dict(_status(root))})
        return 500, None

    monkeypatch.setattr(hat_tools, "_http_post_text", failing_poster)

    sup._pause_and_escalate_incurable_rejection(1)

    assert seen, "the notifier was never called"
    first = seen[0]
    assert first["gate_open"] is False  # the in-memory pause is already held
    assert [e["reason"] for e in first["escalations"]] == ["rejected_incurable"]
    assert first["status"].get("state") != "paused"
    assert first["status"].get("pauseReason") is None

    undelivered = _events(root, run_record.NOTIFICATION_ESCALATION_UNDELIVERED_EVENT)
    assert [e["reason"] for e in undelivered] == ["rejected_incurable"]
