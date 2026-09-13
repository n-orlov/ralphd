"""Task 022 (PRD requirement B.3, #140): the engine-owned `notify` hat tool.
Task 009 (PRD requirement B.1, #197) splits the event NAME `send_notification`
writes by outcome -- see 3a/4a below.

`hat_tools.send_notification` is the one place the actual POST happens and
the one place the `notification.sent`/`notification.failed` events are
written -- both the steer hat's broker (`hat_tools.STEER_WIRED_TOOLS`,
unchanged authority) and the worker hat's own much narrower one
(`hat_tools.WORKER_WIRED_TOOLS`, just this one tool -- the supervisor's own
`_run_worker_iteration` wires it) call through it. This module pins, over a
fixture run directory (`tmp_path / "run"`, task 009/requirement B.5 --
never this run's own events):

3. a stubbed successful send: a `notification.sent` event carries exactly
   {endpoint, textSha256, httpStatus} as its own data (plus the base
   id/ts/type every event gets) and the tool answers ok=True;
3a. `test_a_failed_send_emits_the_distinct_failure_event_and_never_sent`
    below and `test_a_successful_send_still_emits_notification_sent` below
    are this split's own named tests (requirement B.1);
4. a stubbed failed send (connection error, and a non-2xx status): a
   distinct `notification.failed` event is recorded instead -- never
   `notification.sent` -- with its outcome reflected in `httpStatus`, and
   the tool answers ok=False with a non-empty `error` -- never raises;
5. no notifier URL configured behaves like any other failed send (also
   `notification.failed`, never `notification.sent`);
6. the tool is wired for BOTH the steer and the worker broker, and the
   worker broker refuses every OTHER steer-only tool;
7. the run record's notifications section is exactly the notification.sent
   events, in order (a failed send never appears there);
8. `examples/skills/telegram-notify/SKILL.md` exists, vendored alongside
   `toolchain-sibling/SKILL.md`, and tells the agent to prefer the `notify`
   tool when present and fall back to the direct HTTP push otherwise.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from ralphd.engine import hat_tools, run_record
from ralphd.engine.state import RunDir, atomic_write_json

REPO_ROOT = Path(__file__).resolve().parents[1]


def _run(tmp_path: Path) -> RunDir:
    run = RunDir(root=tmp_path / "run")
    atomic_write_json(run.tasks_file, {"version": 1, "tasks": []})
    return run


def _events(run: RunDir, type_: str) -> list[dict]:
    log = run.root / "events.jsonl"
    lines = log.read_text().splitlines() if log.exists() else []
    return [ev for ev in (json.loads(x) for x in lines)
            if ev.get("type") == type_]


# ---------------------------------------------------------------------------
# 1/2/3. send_notification itself -- a stubbed success, a stubbed failure,
# and the no-url-configured case.
# ---------------------------------------------------------------------------

def test_a_stubbed_successful_send_records_exactly_the_named_fields(tmp_path):
    run = _run(tmp_path)
    calls = []

    def stub_poster(url, text, timeout_s):
        calls.append((url, text, timeout_s))
        return 200, None

    result = hat_tools.send_notification(
        run, "hello operator", "http://notifier.example/notify",
        poster=stub_poster)

    assert result["ok"] is True
    assert result["error"] is None
    assert calls == [("http://notifier.example/notify", "hello operator",
                      15.0)]

    events = _events(run, hat_tools.NOTIFICATION_SENT_EVENT)
    assert len(events) == 1
    event = events[0]
    # every event carries id/ts/type -- the requirement's own field set is
    # everything ELSE on the event, and it is EXACTLY these three fields,
    # never an "ok"/"error" leaking onto the persisted record.
    data_fields = set(event) - {"id", "ts", "type"}
    assert data_fields == {"endpoint", "textSha256", "httpStatus"}
    assert event["endpoint"] == "http://notifier.example/notify"
    assert event["httpStatus"] == 200
    assert event["textSha256"] == hashlib.sha256(
        b"hello operator").hexdigest()
    assert "ts" in event


def test_a_stubbed_connection_failure_still_records_the_event(tmp_path):
    run = _run(tmp_path)

    def stub_poster(url, text, timeout_s):
        return None, "Connection refused"

    result = hat_tools.send_notification(
        run, "hello operator", "http://notifier.example/notify",
        poster=stub_poster)

    assert result["ok"] is False
    assert result["error"] == "Connection refused"

    events = _events(run, hat_tools.NOTIFICATION_FAILED_EVENT)
    assert len(events) == 1
    event = events[0]
    assert set(event) - {"id", "ts", "type"} == {
        "endpoint", "textSha256", "httpStatus"}
    assert event["httpStatus"] is None
    assert event["endpoint"] == "http://notifier.example/notify"
    assert _events(run, hat_tools.NOTIFICATION_SENT_EVENT) == []


def test_a_stubbed_non_2xx_status_is_also_a_failure_but_still_recorded(
        tmp_path):
    run = _run(tmp_path)

    def stub_poster(url, text, timeout_s):
        return 500, None

    result = hat_tools.send_notification(
        run, "hi", "http://notifier.example/notify", poster=stub_poster)

    assert result["ok"] is False
    assert result["error"]
    events = _events(run, hat_tools.NOTIFICATION_FAILED_EVENT)
    assert events[0]["httpStatus"] == 500
    assert _events(run, hat_tools.NOTIFICATION_SENT_EVENT) == []


def test_no_notifier_url_configured_is_a_failure_that_never_raises(
        tmp_path):
    run = _run(tmp_path)
    result = hat_tools.send_notification(run, "hi", None)
    assert result["ok"] is False
    assert "no notifier" in result["error"]
    events = _events(run, hat_tools.NOTIFICATION_FAILED_EVENT)
    assert len(events) == 1
    assert events[0]["endpoint"] is None
    assert events[0]["httpStatus"] is None
    assert _events(run, hat_tools.NOTIFICATION_SENT_EVENT) == []


def test_a_failed_send_emits_the_distinct_failure_event_and_never_sent(
        tmp_path):
    """Task 009 (PRD requirement B.1, #197): over the fixture run
    directory `_run(tmp_path)` builds (`tmp_path / "run"`), a failed send
    -- here a stubbed non-2xx status, the same shape a real notifier
    outage takes -- writes `notification.failed` into the run record and
    leaves NO `notification.sent` entry there at all: a reader grepping
    the `sent` name for "was the operator told" never gets a false
    positive from a send that never happened."""
    run = _run(tmp_path)

    def stub_poster(url, text, timeout_s):
        return 503, None

    result = hat_tools.send_notification(
        run, "hello operator", "http://notifier.example/notify",
        poster=stub_poster)

    assert result["ok"] is False
    failed = _events(run, hat_tools.NOTIFICATION_FAILED_EVENT)
    assert len(failed) == 1
    assert failed[0]["httpStatus"] == 503
    assert _events(run, hat_tools.NOTIFICATION_SENT_EVENT) == []


def test_a_successful_send_still_emits_notification_sent(tmp_path):
    """Task 009 (PRD requirement B.1, #197): the success path is
    unchanged by the split -- over the same fixture run directory shape,
    a successful send still writes `notification.sent`, and never the
    failure event."""
    run = _run(tmp_path)

    def stub_poster(url, text, timeout_s):
        return 200, None

    result = hat_tools.send_notification(
        run, "hello operator", "http://notifier.example/notify",
        poster=stub_poster)

    assert result["ok"] is True
    sent = _events(run, hat_tools.NOTIFICATION_SENT_EVENT)
    assert len(sent) == 1
    assert sent[0]["httpStatus"] == 200
    assert _events(run, hat_tools.NOTIFICATION_FAILED_EVENT) == []


# ---------------------------------------------------------------------------
# 4. Wired for both the steer and the worker broker; the worker broker
# refuses every OTHER (steer-only) tool.
# ---------------------------------------------------------------------------

def test_notify_is_wired_for_both_the_steer_and_worker_tool_sets():
    assert hat_tools.NOTIFY_TOOL in hat_tools.STEER_WIRED_TOOLS
    assert hat_tools.NOTIFY_TOOL in hat_tools.WORKER_WIRED_TOOLS
    assert set(hat_tools.WORKER_WIRED_TOOLS) == {hat_tools.NOTIFY_TOOL}


def test_the_worker_broker_serves_notify_but_refuses_task_insert(
        tmp_path, monkeypatch):
    run = _run(tmp_path)
    monkeypatch.setattr(hat_tools, "_http_post_text",
                        lambda *a, **k: (200, None))
    broker = hat_tools.SteerToolBroker(
        run, tools=frozenset(hat_tools.WORKER_WIRED_TOOLS),
        notifier_url="http://notifier.example/notify", source="worker")

    response = broker.execute("notify", {"text": "worker says hi"})
    assert response["ok"] is True

    refused = broker.execute("task-insert",
                             {"task": {"id": "999", "title": "t"}})
    assert refused["ok"] is False
    assert "unknown tool" in refused["error"]
    # never mutated: the refusal above did not sneak a task in
    assert run.read_tasks()["tasks"] == []


def test_the_worker_broker_generated_extension_registers_only_notify(
        tmp_path):
    broker = hat_tools.SteerToolBroker(
        run=_run(tmp_path), tools=frozenset(hat_tools.WORKER_WIRED_TOOLS))
    path = broker.place()
    source = path.read_text()
    names = set(__import__("re").findall(r'name:\s*"([a-z-]+)"', source))
    assert names == {"notify"}


def test_the_steer_broker_still_serves_the_full_task_table_tools(
        tmp_path, monkeypatch):
    """Adding `notify` to the steer hat's set does not narrow anything it
    already had."""
    run = _run(tmp_path)
    monkeypatch.setattr(hat_tools, "_http_post_text",
                        lambda *a, **k: (200, None))
    broker = hat_tools.SteerToolBroker(
        run, notifier_url="http://notifier.example/notify")
    inserted = broker.execute(
        "task-insert", {"task": {"id": "042", "title": "t"}})
    assert inserted["ok"] is True
    sent = broker.execute("notify", {"text": "steer says hi"})
    assert sent["ok"] is True


def test_a_missing_or_blank_text_is_refused_without_a_send(tmp_path):
    run = _run(tmp_path)
    calls = []
    broker = hat_tools.SteerToolBroker(
        run, tools=frozenset(hat_tools.WORKER_WIRED_TOOLS),
        notifier_url="http://notifier.example/notify")
    import ralphd.engine.hat_tools as ht
    orig = ht.send_notification
    ht.send_notification = lambda *a, **k: calls.append(1)  # pragma: no cover
    try:
        response = broker.execute("notify", {"text": "   "})
    finally:
        ht.send_notification = orig
    assert response["ok"] is False
    assert not calls


# ---------------------------------------------------------------------------
# 5. The run record's notifications section.
# ---------------------------------------------------------------------------

def test_the_run_records_notifications_section_carries_the_sent_events(
        tmp_path):
    run = _run(tmp_path)
    hat_tools.send_notification(run, "one", "http://n.example/notify",
                                poster=lambda *a, **k: (200, None))
    hat_tools.send_notification(run, "two", "http://n.example/notify",
                                poster=lambda *a, **k: (200, None))
    # a failed send (task 009, requirement B.1: `notification.failed`,
    # never `notification.sent`) never lands in this section.
    hat_tools.send_notification(run, "three", None)
    section = run_record.build_notifications_section(run)
    assert len(section) == 2
    assert section[0]["httpStatus"] == 200
    assert section[1]["httpStatus"] == 200


# ---------------------------------------------------------------------------
# 6. examples/skills/telegram-notify/SKILL.md is vendored and says to
# prefer the tool.
# ---------------------------------------------------------------------------

def test_the_telegram_notify_skill_is_vendored_alongside_toolchain_sibling():
    sibling = REPO_ROOT / "examples" / "skills" / "toolchain-sibling" / "SKILL.md"
    vendored = REPO_ROOT / "examples" / "skills" / "telegram-notify" / "SKILL.md"
    assert sibling.exists()
    assert vendored.exists()
    text = vendored.read_text()
    assert text.startswith("---\nname: telegram-notify\n")
    assert "notify" in text
    # states the preference and the fallback in the same breath a later
    # reader can't miss.
    assert "notify" in text.lower()
    assert "prefer" in text.lower() and "notify" in text.lower()
    assert "fall back" in text.lower() or "fallback" in text.lower()
    assert "direct http push" in text.lower() or "http push" in text.lower()
