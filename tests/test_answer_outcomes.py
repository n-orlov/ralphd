"""v0.36 requirement C: `ralphctl answer`, `POST /answer` and the hub panel
are ONE path with ONE writer (`LoopSupervisor.answer`), and every outcome of
C.3 works for both judges (review and the peer verdict gate):

1. every blocking finding waived -> verified on that judge's verdict, and on
   to the peer gate or finish;
2. K >= 1 -> K rounds are added to the current approach's allowance and the
   accepted findings are cured now, without re-parking on the same findings;
3. K = 0 with an approach left -> archive and replan on approach + 1, over the
   current workspace tree (no reset);
4. K = 0 with none left -> refused, naming `--cure-rounds K>=1` and
   `--approaches +N` (which grants N approaches, then replans);
5. K >= 1 with an accepted `curable: false` finding -> refused.

Also pinned: an unnamed finding is accepted, K defaults to 1, a waiver goes to
the clause-bound store, the note is recorded as a ruling, `input.answered` is
emitted, the CLI and the hub route reach the same writer, and a ruling no
longer releases a park (D.1) nor does a bare unpause (D.2).
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ralphd.cli import main as cli_main
from ralphd.engine import answer as answering
from ralphd.engine.api import create_app
from ralphd.engine.state import read_events
from tests.test_rejection_ladder import (
    APPROACH_LEFT_EDGE, CURE_EDGE, JUDGES, _drive, _finding, _make, _status,
    _supervisor)

ROOT = Path(__file__).resolve().parents[1]


def _step_paused(sup) -> tuple:
    """Run the parked step and check the way the dispatch loop proceeds: it
    moves on from `paused` by the edge the step returns, so that edge must be
    declared (a live run failed with an illegal `paused -> cure` otherwise)."""
    outcome = asyncio.run(asyncio.wait_for(sup._step_paused(), 30))
    if outcome[0] == "advance":
        assert sup._advance("paused", outcome[1], outcome[2]) == outcome[1]
    return outcome


def _park_incurable(tmp_path, judge, **cfg):
    sup = _make(tmp_path, judge, **cfg)
    outcome = _drive(sup, judge, [
        _finding(False, "requirement A (#1)", "A is not there"),
        _finding(False, "requirement B (#2)", "B is not there")])
    assert outcome[:2] == ("advance", "paused")
    assert _status(sup)["state"] == "awaiting_input"
    return sup


def _park_rounds_spent(tmp_path, judge, **cfg):
    cfg.setdefault("cure_pass_ceiling", 1)
    sup = _make(tmp_path, judge, **cfg)
    sup.run.update_status(curePasses={"1": 1})
    outcome = _drive(sup, judge, [_finding(True, "requirement A (#1)")])
    assert outcome[:2] == ("advance", "paused")
    assert _status(sup)["inputQuestion"]["trigger"] == "cure_rounds_spent"
    return sup


def _answered(sup) -> list[dict]:
    return [e for e in read_events(sup.run.root) if e.get("type") == "input.answered"]


# -- C.3 outcome 1: every blocking finding waived -> verified -----------------

@pytest.mark.parametrize("judge", JUDGES)
def test_waiving_every_blocking_finding_is_verified_and_finishes(
        tmp_path, judge):
    sup = _park_incurable(tmp_path, judge)

    record = sup.answer(waive=["F1", "F2"], note="both are accepted risks")

    assert record["outcome"] == "verified"
    assert _step_paused(sup) == ("done", "succeeded")
    assert not sup.run.composite_prd_file.exists(), "nothing was archived"
    assert _status(sup)["state"] != "awaiting_input"


def test_waiving_every_blocking_finding_continues_to_the_peer_gate(tmp_path):
    sup = _supervisor(tmp_path / "run", peer_verdict_gate=True,
                      cure_pass_ceiling=2)
    sup._approach = 1
    from ralphd.engine.state import atomic_write_json
    atomic_write_json(sup.run.tasks_file, {"version": 1, "tasks": []})
    _drive(sup, "review", [_finding(False, "requirement A (#1)")])
    assert _status(sup)["state"] == "awaiting_input"

    sup.answer(waive=["F1"])

    assert _step_paused(sup) == (
        "advance", "verdict_gate", "verified_peer_verdict_gate_enabled")


@pytest.mark.parametrize("judge", JUDGES)
def test_each_waiver_is_stored_against_its_clause_with_the_note(
        tmp_path, judge):
    sup = _park_incurable(tmp_path, judge)

    sup.answer(waive=["F1"], accept=["F2"], cure_rounds=0,
               note="A is by design")

    waivers = sup.run.waiver_entries()
    assert len(waivers) == 1
    assert waivers[0]["clause"] == "requirement A (#1)"
    assert waivers[0]["finding"] == "A is not there"
    assert waivers[0]["judge"] == judge
    assert waivers[0]["note"] == "A is by design"


@pytest.mark.parametrize("judge", JUDGES)
def test_the_note_is_recorded_as_a_ruling_and_input_answered_is_emitted(
        tmp_path, judge):
    sup = _park_incurable(tmp_path, judge)

    record = sup.answer(waive=["F1"], cure_rounds=0, note="  see the spec \n",
                        note_name="why")

    rulings = sup.run.ruling_entries()
    assert [r["text"] for r in rulings] == ["see the spec"]
    assert rulings[0]["file"].endswith("-why.md")
    events = _answered(sup)
    assert len(events) == 1
    assert events[0]["outcome"] == "next_approach"
    assert events[0]["waived"] == ["F1"] and events[0]["accepted"] == ["F2"]
    assert events[0]["rulingFile"] == record["rulingFile"]


# -- C.3 outcome 2: K >= 1 cures now ------------------------------------------

@pytest.mark.parametrize("judge", JUDGES)
def test_an_unnamed_answer_accepts_everything_and_cures_one_round(
        tmp_path, judge):
    """No finding named -> all accepted; `--cure-rounds` defaults to 1."""
    sup = _park_rounds_spent(tmp_path, judge)
    assert sup._cure_rounds_allowed(1) == 1

    record = sup.answer()

    assert record["outcome"] == "cure" and record["cureRounds"] == 1
    assert record["waived"] == [] and record["accepted"] == ["F1"]
    assert sup._cure_rounds_allowed(1) == 2
    assert _step_paused(sup) == ("advance", "cure", CURE_EDGE[judge])
    assert sup._cure_rounds_used(1) == 2
    assert _status(sup)["state"] == "running"
    assert not sup.run.waiver_entries()


@pytest.mark.parametrize("judge", JUDGES)
def test_k_rounds_are_added_to_the_allowance_and_the_run_does_not_re_park(
        tmp_path, judge):
    sup = _park_rounds_spent(tmp_path, judge)
    assert sup._cure_pass_ceiling_spent(1)

    sup.answer(cure_rounds=2)

    assert sup._cure_rounds_allowed(1) == 3
    assert _step_paused(sup) == ("advance", "cure", CURE_EDGE[judge])
    # one of the K added rounds is spent by the cure the answer authorised;
    # the other is still there, so the run is not parked on the same findings.
    assert sup._cure_rounds_used(1) == 2
    assert not sup._cure_pass_ceiling_spent(1)
    assert _status(sup)["state"] == "running"
    assert "inputQuestion" not in _status(sup) or not _status(sup)["inputQuestion"]


@pytest.mark.parametrize("judge", JUDGES)
def test_the_added_rounds_belong_to_the_approach_that_was_answered(
        tmp_path, judge):
    sup = _park_rounds_spent(tmp_path, judge)
    sup.answer(cure_rounds=2)

    assert sup._cure_rounds_allowed(1) == 3
    assert sup._cure_rounds_allowed(2) == sup.cfg.cure_pass_ceiling


@pytest.mark.parametrize("judge", ["review"])
def test_the_answer_authorises_the_round_a_reviewer_escalation_asked_for(
        tmp_path, judge):
    sup = _make(tmp_path, judge, cure_pass_ceiling=3)
    _drive(sup, judge, [_finding(True, "requirement A (#1)")], escalate=True)
    assert _status(sup)["inputQuestion"]["trigger"] == "reviewer_escalation"

    sup.answer(cure_rounds=1)

    assert _step_paused(sup) == ("advance", "cure", CURE_EDGE[judge])
    assert _status(sup)["state"] == "running"


@pytest.mark.parametrize("judge", JUDGES)
def test_cure_tasks_target_the_accepted_findings_only(tmp_path, judge):
    sup = _make(tmp_path, judge, cure_pass_ceiling=1)
    sup.run.update_status(curePasses={"1": 1})
    _drive(sup, judge, [_finding(True, "requirement A (#1)", "A is broken"),
                        _finding(True, "requirement B (#2)", "B is broken")])

    sup.answer(waive=["F1"], cure_rounds=1)
    _step_paused(sup)

    tasks = sup.run.read_tasks()["tasks"]
    text = json.dumps(tasks)
    assert "B is broken" in text or "requirement B" in text
    assert "requirement A" not in text and "A is broken" not in text


# -- C.3 outcome 3: K = 0, approaches left -> archive and replan -------------

@pytest.mark.parametrize("judge", JUDGES)
def test_k_zero_with_approaches_left_archives_and_replans_on_the_next(
        tmp_path, judge):
    sup = _park_incurable(tmp_path, judge, max_approaches=3)
    marker = sup.workspace / "kept-by-the-new-approach.txt"
    marker.write_text("work of approach 1\n")

    sup.answer(cure_rounds=0)
    outcome = _step_paused(sup)

    assert outcome == ("advance", "replan", APPROACH_LEFT_EDGE[judge])
    assert (sup.run.root / "approaches" / "01").is_dir()
    assert sup.run.composite_prd_file.exists()
    assert marker.read_text() == "work of approach 1\n", "no workspace reset"
    assert sup._step_replan() == (
        "advance", "planning", "budget_and_approach_left")
    assert sup._approach == 2


# -- C.3 outcome 4: K = 0, none left ------------------------------------------

@pytest.mark.parametrize("judge", JUDGES)
def test_k_zero_with_no_approach_left_is_refused_naming_both_options(
        tmp_path, judge):
    sup = _park_incurable(tmp_path, judge, max_approaches=1)

    with pytest.raises(answering.AnswerRefused) as refused:
        sup.answer(cure_rounds=0, waive=["F1"], note="would be recorded")

    assert "--cure-rounds K" in refused.value.message
    assert "--approaches +N" in refused.value.message
    assert _status(sup)["state"] == "awaiting_input", "a refusal changes nothing"
    assert not sup.run.waiver_entries() and not sup.run.ruling_entries()
    assert not _answered(sup)


@pytest.mark.parametrize("judge", JUDGES)
def test_approaches_grants_n_approaches_and_then_replans(tmp_path, judge):
    sup = _park_incurable(tmp_path, judge, max_approaches=1)

    record = sup.answer(approaches=2)

    assert record["outcome"] == "next_approach"
    assert record["approachesGranted"] == 2 and sup.cfg.max_approaches == 3
    assert _status(sup)["maxApproaches"] == 3
    assert _step_paused(sup) == (
        "advance", "replan", APPROACH_LEFT_EDGE[judge])
    assert sup._step_replan()[:2] == ("advance", "planning")
    assert sup._approach == 2


# -- C.3 outcome 5: an accepted incurable finding ------------------------------

@pytest.mark.parametrize("judge", JUDGES)
def test_cure_rounds_are_refused_for_an_accepted_incurable_finding(
        tmp_path, judge):
    sup = _park_incurable(tmp_path, judge)

    with pytest.raises(answering.AnswerRefused) as refused:
        sup.answer(cure_rounds=1, waive=["F1"])    # F2 stays accepted

    assert "F2" in refused.value.message
    assert "--waive" in refused.value.message
    assert "--cure-rounds 0" in refused.value.message
    assert _status(sup)["state"] == "awaiting_input"
    assert not sup.run.waiver_entries()


@pytest.mark.parametrize("judge", JUDGES)
def test_waiving_the_incurable_finding_lets_cure_rounds_through(
        tmp_path, judge):
    sup = _make(tmp_path, judge, cure_pass_ceiling=3)
    _drive(sup, judge, [_finding(False, "requirement A (#1)"),
                        _finding(True, "requirement B (#2)")])
    assert _status(sup)["inputQuestion"]["trigger"] == "incurable"

    assert sup.answer(waive=["F1"], cure_rounds=1)["outcome"] == "cure"


# -- malformed answers ---------------------------------------------------------

@pytest.mark.parametrize("kwargs", [
    {"waive": ["F9"]}, {"accept": ["F9"]},
    {"waive": ["F1"], "accept": ["F1"]}, {"cure_rounds": -1},
    {"cure_rounds": "many"}, {"cure_rounds": 1, "approaches": 1}])
def test_a_malformed_answer_is_refused_and_writes_nothing(tmp_path, kwargs):
    sup = _park_incurable(tmp_path, "review")

    with pytest.raises(answering.AnswerRefused) as refused:
        sup.answer(note="x", **kwargs)

    assert refused.value.status == 422
    assert _status(sup)["state"] == "awaiting_input"
    assert not sup.run.waiver_entries() and not sup.run.ruling_entries()


def test_an_answer_to_a_run_that_is_not_awaiting_input_is_refused(tmp_path):
    sup = _make(tmp_path, "review")

    with pytest.raises(answering.AnswerRefused) as refused:
        sup.answer()

    assert "not `awaiting_input`" in refused.value.message


# -- one writer: the API route and the CLI --------------------------------------

def _client(sup):
    return TestClient(create_app(sup.cfg, sup.run, sup))


def test_post_answer_reaches_the_one_writer(tmp_path):
    sup = _park_incurable(tmp_path, "review")
    with _client(sup) as client:
        resp = client.post("/answer", json={
            "waive": ["F1"], "cureRounds": 0, "note": "by design"})

    assert resp.status_code == 200, resp.text
    assert resp.json()["outcome"] == "next_approach"
    assert [w["clause"] for w in sup.run.waiver_entries()] == [
        "requirement A (#1)"]
    assert [r["text"] for r in sup.run.ruling_entries()] == ["by design"]
    assert len(_answered(sup)) == 1


def test_post_answer_refusals_carry_the_engines_message(tmp_path):
    sup = _park_incurable(tmp_path, "review", max_approaches=1)
    with _client(sup) as client:
        refused = client.post("/answer", json={"cureRounds": 0})
        unknown = client.post("/answer", json={"waive": ["F7"]})

    assert refused.status_code == 409
    assert "--approaches +N" in refused.text
    assert unknown.status_code == 422


def test_the_cli_answer_command_posts_to_the_same_route(tmp_path, monkeypatch):
    sup = _park_incurable(tmp_path, "review")
    note = tmp_path / "note.md"
    note.write_text("waived on purpose\n")
    monkeypatch.setattr(cli_main, "_require_run", lambda run_id: None)
    seen = []
    with _client(sup) as client:
        def api(run_id, method, path, body=None, **kw):
            seen.append((method, path, body))
            resp = client.request(method, path, json=body)
            assert resp.status_code < 400, resp.text
            return resp.json()
        monkeypatch.setattr(cli_main, "api", api)
        args = cli_main.build_parser().parse_args([
            "answer", "rejection-ladder", "--waive", "F1", "F2",
            "--note-file", str(note)])
        args.func(args)

    assert seen == [("POST", "/answer", {
        "waive": ["F1", "F2"], "note": "waived on purpose\n"})]
    assert len(sup.run.waiver_entries()) == 2


def test_the_cli_parses_every_option_of_the_answer_command():
    args = cli_main.build_parser().parse_args([
        "answer", "r", "--waive", "F1", "--accept", "F2", "--cure-rounds", "0",
        "--approaches", "+2", "--note-file", "n.md"])
    assert (args.waive, args.accept, args.cure_rounds, args.approaches) == (
        [["F1"]], [["F2"]], 0, "+2")


# -- D.1 / D.2: a ruling and a bare unpause no longer release a park ------------

def test_a_ruling_is_recorded_but_does_not_release_the_park(tmp_path):
    sup = _park_incurable(tmp_path, "review")
    with _client(sup) as client:
        resp = client.post("/rule", json={"text": "I think F1 is fine"})

    assert resp.status_code == 202
    assert len(sup.run.ruling_entries()) == 1
    assert _status(sup)["state"] == "awaiting_input"
    assert not sup._pause.is_set()


def test_a_bare_unpause_of_a_parked_run_is_refused_naming_answer(tmp_path):
    sup = _park_incurable(tmp_path, "review")
    with _client(sup) as client:
        resp = client.post("/resume")

    assert resp.status_code == 409
    assert "ralphctl answer" in resp.text
    assert _status(sup)["state"] == "awaiting_input"
    assert "noRulingProceed" not in _status(sup)


def test_the_retired_release_paths_are_gone_from_the_engine_source():
    for path in (ROOT / "src" / "ralphd").rglob("*.py"):
        text = path.read_text()
        assert "resume_if_ruling_answers_pause" not in text, path
        assert "noRulingProceed" not in text, path


# -- the docs describe the answer path in the present tense ----------------------

def _section(text: str, heading: str, until: str) -> str:
    start = text.index(heading)
    return text[start:text.index(until, start + len(heading))]


def test_the_cli_api_and_spec_describe_the_answer_path():
    cli = (ROOT / "docs" / "cli.md").read_text()
    api = (ROOT / "docs" / "api.md").read_text()
    spec = (ROOT / "SPEC.md").read_text()

    answer_cli = _section(cli, "### `ralphctl answer ", "### `ralphctl amend")
    for needle in ("--waive", "--accept", "--cure-rounds", "--approaches +N",
                   "--note-file", "POST /answer", "input.answered",
                   "`curable: false`"):
        assert needle in answer_cli, needle

    answer_api = _section(api, "### `POST /answer`", "### `POST /tasks/")
    for needle in ("cureRounds", "waive", "\"verified\"", "\"cure\"",
                   "\"next_approach\"", "`--cure-rounds K>=1`",
                   "`--approaches +N`", "`409`", "`422`"):
        assert needle in answer_api, needle

    ten_four = _section(spec, "### 10.4 Steering and control", "### 10.5 ")
    for needle in ("| `answer <id>", "`POST /answer`", "`--cure-rounds K>=1`",
                   "`--approaches +N`", "write_ruling",
                   "releases nothing"):
        assert needle in ten_four, needle

    eleven_three = _section(spec, "### 11.3 Run detail", "### 11.4 ")
    for needle in ("Answer: cure now", "Answer: next approach",
                   "POST /api/runs/<id>/answer", "greyed out"):
        assert needle in eleven_three, needle
    assert "**`Resume without a ruling`**" not in eleven_three
    for text in (cli, api, spec):
        assert "resume_if_ruling_answers_pause" not in text
        assert "`noRulingProceed`" not in text
