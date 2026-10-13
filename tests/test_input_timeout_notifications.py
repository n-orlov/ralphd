"""v0.36 requirement E.4: the three notifications of an `awaiting_input` park.

A fake notifier at `notifier_url` (the engine's `notifier` seam, as the
escalation tests drive it) and a fake clock (`_now_epoch` and the
`_wait_pause_event` seam move together), so no test sleeps out a timeout:

1. the park sends exactly one message: run id, judge, trigger, each finding's
   clause with a one-line summary, `expiresAt`, the timeout outcome and the
   exact `ralphctl answer` command lines;
2. one reminder 10 minutes before `expiresAt` (at half the timeout when the
   timeout is under 20 minutes), and one message when the timeout fires that
   names the outcome taken;
3. a notifier that errors, or none at all, never holds or fails the run.
"""

from __future__ import annotations

import pytest

from ralphd.engine import state
from tests.test_input_timeout import (
    MIN, T0, FakeClock, _events, _run_paused)
from tests.test_rejection_ladder import (
    PARK_EDGE, _drive, _finding, _make, _status)

JUDGE_NAMES = ["review", "verdict_gate"]


class Notifier:
    """Records `(fake clock time, text)` for every message."""

    def __init__(self, clock: FakeClock | None = None):
        self.clock = clock
        self.sent: list[tuple[float, str]] = []

    def __call__(self, text: str, max_attempts: int = 1):
        self.sent.append((self.clock.t if self.clock else 0.0, text))
        return {"ok": True}

    @property
    def texts(self) -> list[str]:
        return [t for _, t in self.sent]


class RaisingNotifier:
    def __init__(self):
        self.calls = 0

    def __call__(self, text: str, max_attempts: int = 1):
        self.calls += 1
        raise RuntimeError("the notifier is down")


class FailingNotifier(RaisingNotifier):
    def __call__(self, text: str, max_attempts: int = 1):
        self.calls += 1
        return {"ok": False, "error": "refused"}


def _park(tmp_path, judge="review", notifier="fake", *, items=None,
          escalate=False, **cfg):
    clock = FakeClock()
    cfg.setdefault("cure_pass_ceiling", 3 if escalate else 1)
    sup = _make(tmp_path, judge, **cfg)
    clock.install(sup)
    if notifier == "fake":
        notifier = Notifier(clock)
    sup.notifier = notifier
    if not escalate:
        sup.run.update_status(curePasses={"1": 1})
    items = items or [_finding(True, "requirement A (#1)")]
    outcome = _drive(sup, judge, items, escalate=escalate)
    assert outcome == ("advance", "paused", PARK_EDGE[judge]), outcome
    assert _status(sup)["state"] == "awaiting_input"
    return sup, clock, notifier


# -- 1. the park message ---------------------------------------------------------

@pytest.mark.parametrize("judge", JUDGE_NAMES)
def test_the_park_sends_exactly_one_message_with_everything_to_act_on(
        tmp_path, judge):
    items = [_finding(True, "requirement A (#1)", "line one\nline two"),
             _finding(False, "requirement B (#2)")]
    sup, _clock, notifier = _park(tmp_path, judge, items=items)

    assert len(notifier.texts) == 1, notifier.texts
    text = notifier.texts[0]
    question = _status(sup)["inputQuestion"]
    run_id = sup.cfg.run_id
    assert run_id in text
    assert f"judge: {judge}" in text
    assert f"trigger: {question['trigger']}" in text
    assert f"expiresAt: {question['expiresAt']}" in text
    assert question["expiresAt"] == state.utc_from_epoch(T0 + 60 * MIN)
    # each finding: its clause and a one-line summary on the same line
    line_a = next(ln for ln in text.splitlines() if "requirement A (#1)" in ln)
    line_b = next(ln for ln in text.splitlines() if "requirement B (#2)" in ln)
    assert line_a.startswith("F1:") and "curable" in line_a
    assert line_b.startswith("F2:") and "not curable" in line_b
    # the timeout outcome
    assert f"if unanswered: {question['onTimeout']}" in text
    # the exact `ralphctl answer` command lines
    lines = text.splitlines()
    assert f"answer: ralphctl answer {run_id}" in lines
    assert f"answer: ralphctl answer {run_id} --waive F1" in lines
    assert f"answer: ralphctl answer {run_id} --cure-rounds 0" in lines


def test_the_park_is_announced_once_and_recorded_once(tmp_path):
    sup, _clock, notifier = _park(tmp_path)
    assert len(notifier.texts) == 1
    assert len(_events(sup, "input.parked")) == 1


# -- 2. the reminder and the timeout message -------------------------------------

def _wait_out(sup, clock) -> tuple:
    return _run_paused(sup)


@pytest.mark.parametrize("judge", JUDGE_NAMES)
def test_one_reminder_ten_minutes_before_expiry_then_one_timeout_message(
        tmp_path, judge):
    sup, clock, notifier = _park(tmp_path, judge)
    assert len(notifier.sent) == 1                       # the park message

    outcome = _wait_out(sup, clock)

    assert outcome[:2] == ("advance", "replan")
    assert len(notifier.sent) == 3, notifier.texts
    (_, reminder), (fired_at, fired) = notifier.sent[1], notifier.sent[2]
    remind_at = notifier.sent[1][0]
    assert remind_at == T0 + 50 * MIN, "10 minutes before expiresAt"
    run_id = sup.cfg.run_id
    assert run_id in reminder and "reminder" not in fired
    assert state.utc_from_epoch(T0 + 60 * MIN) in reminder
    assert f"answer: ralphctl answer {run_id}" in reminder
    # the timeout message names the outcome taken
    assert fired_at == T0 + 60 * MIN
    (event,) = _events(sup, "input.timed_out")
    assert event["outcome"] == "next_approach"
    assert f"outcome taken: {event['outcome']}" in fired
    assert run_id in fired and f"judge: {judge}" in fired


def test_a_timeout_under_twenty_minutes_reminds_at_half_of_it(tmp_path):
    sup, clock, notifier = _park(tmp_path, input_timeout_min=10)

    _wait_out(sup, clock)

    assert [t for t, _ in notifier.sent[1:]] == [T0 + 5 * MIN, T0 + 10 * MIN]


def test_the_reminder_is_recorded_on_the_question_so_it_is_sent_once(tmp_path):
    sup, clock, notifier = _park(tmp_path)

    async def answer_after_reminder(timeout_s: float) -> bool:
        clock.t += timeout_s
        if len(notifier.sent) >= 2:
            sup.answer(cure_rounds=1)
        return sup._pause.is_set()
    sup._wait_pause_event = answer_after_reminder   # type: ignore[method-assign]
    _run_paused(sup)
    assert len(notifier.sent) == 2, "the park message and the one reminder"
    assert notifier.sent[1][0] == T0 + 50 * MIN

    # a restarted engine reads the persisted question: a reminder already
    # recorded as sent is never due again
    again, clock2, notifier2 = _park(tmp_path / "again")
    question = _status(again)["inputQuestion"]
    assert again._input_reminder_epoch(question) == T0 + 50 * MIN
    again.run.update_status(inputQuestion={
        **question, "reminderSentAt": state.utc_from_epoch(T0 + 50 * MIN)})
    _wait_out(again, clock2)
    assert len(notifier2.sent) == 2, "park + timeout, no second reminder"


def test_no_reminder_when_the_park_never_expires(tmp_path):
    sup, clock, notifier = _park(tmp_path, input_timeout_min=0)
    assert _status(sup)["inputQuestion"]["expiresAt"] is None
    assert sup._input_reminder_epoch(_status(sup)["inputQuestion"]) is None
    assert len(notifier.sent) == 1


def test_a_reviewer_escalation_timeout_message_names_cure_now(tmp_path):
    sup, clock, notifier = _park(tmp_path, escalate=True)

    outcome = _wait_out(sup, clock)

    assert outcome[:2] == ("advance", "cure")
    assert "outcome taken: cure_now" in notifier.texts[-1]


# -- 3. a notifier that fails, or none, never holds the run ------------------------

@pytest.mark.parametrize("kind", ["raising", "failing", "none"])
@pytest.mark.parametrize("judge", JUDGE_NAMES)
def test_a_broken_or_missing_notifier_still_parks_and_still_times_out(
        tmp_path, judge, kind):
    notifier = {"raising": RaisingNotifier(), "failing": FailingNotifier(),
                "none": None}[kind]
    sup, clock, _ = _park(tmp_path, judge, notifier=notifier)
    assert _status(sup)["state"] == "awaiting_input"
    assert _events(sup, "input.parked")

    outcome = _wait_out(sup, clock)

    assert outcome[:2] == ("advance", "replan")
    (event,) = _events(sup, "input.timed_out")
    assert event["outcome"] == "next_approach"
    assert _status(sup)["state"] == "running"
    if notifier is not None:
        assert notifier.calls >= 2, "it was tried for the park and the timeout"
