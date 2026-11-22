"""v0.38 A.5, B.2/B.3: the answer, event and notification contracts of the two
parks that carry no findings (`petition_hold` and `verified_unchanged`).

In-process probes -- the real `LoopSupervisor` park writers, the real
`POST /answer` route over `fastapi.testclient.TestClient`, a recording notifier
that always fails -- confirm what SPEC.md, docs/api.md and docs/cli.md say:

* values that refuse the release (`409`, nothing written, still parked) versus
  fields the park ignores (`200`), and the exact successful response;
* the `input.parked` / `input.answered` payloads and their order, with none of
  the findings-only side effects (`operator_escalation`, waivers, rulings,
  `inputAnswer`);
* ONE best-effort notification attempt (`max_attempts=1`), never the retried
  operator-escalation path.

Nothing sleeps: the park writers are called directly and the notifier is a
local recorder.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ralphd.engine.api import create_app
from ralphd.engine.state import read_events
from tests.conftest import make_supervisor

ROOT = Path(__file__).resolve().parents[1]
HEAD = "a" * 40
KINDS = ("petition_hold", "verified_unchanged", "verified_unchanged_gate")

PARKED_COMMON = {
    "kind", "trigger", "judge", "findings", "cureRoundsUsed",
    "cureRoundsAllowed", "approach", "maxApproaches", "expiresAt",
    "onTimeout", "heldTasks", "text"}
FINDINGS_ONLY_EVENTS = {"operator_escalation",
                        "notification.escalation_undelivered"}


def _park(tmp_path, which):
    """Park a run on `which` with a notifier that always fails; returns the
    supervisor and the recorded notifier calls."""
    sup = make_supervisor(
        tmp_path / "run", run_id="park-contracts", cfg_defaults=False,
        max_approaches=1, vigilant=False, on_complete="exit", iterations=30,
        approach=1, peer_verdict_gate=which.endswith("_gate"), tasks=[
            {"id": "001", "title": "held", "status": "petitioned",
             "successCriteria": "c1"},
            {"id": "002", "title": "waits", "status": "pending",
             "dependsOn": ["001"], "successCriteria": "c2"}])
    calls: list[tuple] = []

    def failing(text, **kw):
        calls.append((text, kw))
        raise RuntimeError("the notifier is down")

    sup.notifier = failing
    sup._now_epoch = lambda: 1_800_000_000.0
    if which == "petition_hold":
        sup._park_for_petition_hold(
            sup.run.read_tasks(), {"002": ["001"]}, ["001"])
    else:
        sup._workspace_head_sha = lambda: HEAD
        key = {"head": HEAD, "tasks": [["001", "petitioned", False]]}
        judge = "verdict_gate" if which.endswith("_gate") else "review"
        sup._park_for_verified_unchanged(1, key, judge)
    assert sup.run.read_status()["state"] == "awaiting_input"
    return sup, calls


def _types(sup):
    return [e["type"] for e in read_events(sup.run.root)]


def _answered(sup):
    return [e for e in read_events(sup.run.root)
            if e["type"] == "input.answered"]


def _rulings(sup):
    rdir = sup.run.root / "rulings"
    return sorted(rdir.glob("*")) if rdir.is_dir() else []


# -- POST /answer: the values that refuse the release ------------------------

REFUSING = [
    {"waive": ["F1"]}, {"accept": ["F1"]}, {"waive": "F1"},
    {"cureRounds": 0}, {"cureRounds": 2},
    {"approaches": 0}, {"approaches": 1},
    {"secretHandled": True},
    {"waive": ["F1"], "note": "with a note"},
]


@pytest.mark.parametrize("which", KINDS)
@pytest.mark.parametrize("body", REFUSING, ids=lambda b: ",".join(sorted(b)) + repr(list(b.values())))
def test_values_that_refuse_the_release_leave_the_park_untouched(
        tmp_path, which, body):
    sup, _ = _park(tmp_path, which)
    before = _types(sup)
    with TestClient(create_app(sup.cfg, sup.run, sup)) as client:
        response = client.post("/answer", json=body)

    assert response.status_code == 409, response.text
    status = sup.run.read_status()
    assert status["state"] == "awaiting_input"
    assert status["inputQuestion"]["kind"] == which.replace("_gate", "")
    assert _types(sup) == before, "a refused answer emits nothing"
    assert not _answered(sup) and not _rulings(sup)
    assert not (sup.run.root / "waivers").exists() or not list(
        (sup.run.root / "waivers").glob("*"))


# -- POST /answer: the fields the park ignores, and the response -------------

IGNORED = [
    {}, {"note": "an ignored note"}, {"note": "n", "name": "slug"},
    {"name": "slug"}, {"unknown": 1},
    {"secretHandled": False}, {"waive": [], "accept": []},
    {"waive": None, "cureRounds": None, "approaches": None},
]


@pytest.mark.parametrize("which", KINDS)
@pytest.mark.parametrize("body", IGNORED, ids=lambda b: ",".join(sorted(b)) or "empty")
def test_ignored_fields_still_release_with_exactly_the_answered_payload(
        tmp_path, which, body):
    sup, _ = _park(tmp_path, which)
    with TestClient(create_app(sup.cfg, sup.run, sup)) as client:
        response = client.post("/answer", json=body)

    assert response.status_code == 200, response.text
    expected = {
        "outcome": ("petition_hold_released" if which == "petition_hold"
                    else "verified_unchanged_released"),
        "kind": which.replace("_gate", ""), "approach": 1}
    if which == "petition_hold":
        expected["heldTaskIds"] = ["001"]
    assert response.json() == expected
    answered = _answered(sup)
    assert len(answered) == 1
    assert {k: v for k, v in answered[0].items()
            if k not in ("id", "ts", "type")} == expected
    # none of the findings-only side effects, whatever the body carried
    assert not _rulings(sup), "a note is not recorded as a ruling here"
    status = sup.run.read_status()
    assert status.get("inputAnswer") is None
    assert status["state"] == "running" and status.get("inputQuestion") is None
    assert not FINDINGS_ONLY_EVENTS & set(_types(sup))
    # released: input.answered, then the park is cleared
    types = _types(sup)
    assert types.index("input.answered") < types.index("run.unpaused")


# -- the events and their order ----------------------------------------------

@pytest.mark.parametrize("which", KINDS)
def test_input_parked_payload_and_order(tmp_path, which):
    sup, _ = _park(tmp_path, which)
    events = read_events(sup.run.root)
    types = [e["type"] for e in events]

    assert not FINDINGS_ONLY_EVENTS & set(types)
    assert types.count("input.parked") == 1
    assert types.index("run.paused") < types.index("input.parked")
    paused = next(e for e in events if e["type"] == "run.paused")
    assert paused["reason"] == which.replace("_gate", "")
    parked = next(e for e in events if e["type"] == "input.parked")
    question = sup.run.read_status()["inputQuestion"]
    # the payload is the whole question except `parkedAt`
    assert {k: v for k, v in parked.items()
            if k not in ("id", "ts", "type")} == {
        k: v for k, v in question.items() if k != "parkedAt"}
    expected_keys = set(PARKED_COMMON)
    if which != "petition_hold":
        expected_keys.add("reviewStateKey")
    assert set(question) - {"parkedAt"} == expected_keys
    assert parked["findings"] == [] and parked["expiresAt"] is None
    assert parked["onTimeout"] == "never"
    assert "planPhase" not in parked and "timeoutAtPark" not in parked
    assert parked["judge"] == {"petition_hold": None,
                               "verified_unchanged": "review",
                               "verified_unchanged_gate": "verdict_gate"
                               }[which]


# -- notifications -----------------------------------------------------------

@pytest.mark.parametrize("which", KINDS)
def test_one_best_effort_notification_and_no_escalation_retry(tmp_path, which):
    sup, calls = _park(tmp_path, which)

    # the failing notifier was called once, with a single attempt, and the
    # failure left the park in place
    assert len(calls) == 1
    text, kwargs = calls[0]
    assert kwargs == {"max_attempts": 1}
    question = sup.run.read_status()["inputQuestion"]
    assert text.startswith("run park-contracts is parked in awaiting_input\n")
    assert question["text"][:200] in text
    assert sup.run.read_status()["state"] == "awaiting_input"
    types = _types(sup)
    assert "notification.escalation_undelivered" not in types
    assert "operator_escalation" not in types


# -- the documentation says the same -----------------------------------------

def _section(text: str, start: str, end: str) -> str:
    a = text.index(start)
    return text[a:text.index(end, a)]


def test_api_reference_names_refusing_and_ignored_values_and_responses():
    section = _section((ROOT / "docs/api.md").read_text(),
                       "### `POST /answer`", "### `POST /retry`")
    for needle in (
            "Values that refuse the release", "Fields ignored on these two parks",
            '{"cureRounds": 0}', '{"approaches": 0}', "`secretHandled: true`",
            "`note` and `name`", "NOT recorded as a ruling",
            '"outcome": "petition_hold_released"', "`heldTaskIds`",
            '"outcome": "verified_unchanged_released"',
            "`inputAnswer`", "ONE best-effort notification",
            "`max_attempts=1`", "`notification.escalation_undelivered` is never"):
        assert needle in section, needle


@pytest.mark.parametrize("doc", ["SPEC.md", "docs/api.md"])
def test_event_tables_describe_both_parks_payloads_and_order(doc):
    text = (ROOT / doc).read_text()
    parked = next(line for line in text.splitlines()
                  if line.startswith("| `input.parked` |"))
    answered = next(line for line in text.splitlines()
                    if line.startswith("| `input.answered` |"))
    for needle in ("`petition_hold`", "`verified_unchanged`",
                   "NO `operator_escalation` event", "`run.paused`",
                   "`heldTasks`", "`reviewStateKey`", "`findings: []`"):
        assert needle in parked, (doc, needle)
    for needle in ("petition_hold_released", "verified_unchanged_released",
                   "`heldTaskIds`", "no `inputAnswer`", "`run.unpaused`"):
        assert needle in answered, (doc, needle)


@pytest.mark.parametrize("doc", ["SPEC.md", "docs/api.md"])
def test_notification_docs_separate_best_effort_from_the_retried_path(doc):
    text = (ROOT / doc).read_text()
    for needle in ("`max_attempts=1`", "best-effort",
                   "notification.escalation_undelivered"):
        assert needle in text, (doc, needle)
    assert "never `notification.escalation_undelivered`" in text or (
        "`notification.escalation_undelivered` is never" in text)
