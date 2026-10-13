"""v0.36 requirement E: the input timeout.

A run parked in `awaiting_input` waits for `ralphctl answer`; if nobody
answers within the input timeout the engine acts as if the operator had
answered and records `input.timed_out` naming the outcome. Everything here is
driven with a fake clock (`_now_epoch` and the `_wait_pause_event` seam move
together) and fake judges, so no test sleeps out a real timeout.

1. the setting: `input_timeout_min` defaults to 60, `ralphctl start
   --input-timeout`, the registry key and a template layer onto it, and
   `ralphctl budget --input-timeout N|+N` changes it live and in job.yaml; `0`
   means never (`expiresAt: null`);
2. the clock: the run deadline is frozen for the whole park, `expiresAt` is
   absolute, and a fresh engine process over a parked run keeps the same
   `expiresAt` and stays `awaiting_input`;
3. the outcomes (E.3), one test per park row, for both judges where both can
   park there: reviewer escalation with rounds left -> cure now from the
   remaining allowance; cure rounds spent or incurable with an approach left
   -> the next approach; none left -> `exhausted` (approaches); a `secret`
   park never expires;
4. `operator_input_needed_pause: false` (D.3): the timeout fires at the moment
   of the park, with the park and the outcome still recorded.
"""

from __future__ import annotations

import asyncio
import json

import pytest
import yaml
from fastapi.testclient import TestClient
from test_cli_docker import Ctl, ctl, unix_sock  # noqa: F401  (fixtures)
from test_cli_resume import _seed_run

from ralphd.cli import main as cli_main
from ralphd.engine import state
from ralphd.engine.api import create_app
from ralphd.engine.config import JobConfig
from ralphd.engine.state import atomic_write_json, read_events
from tests.test_rejection_ladder import (
    APPROACH_LEFT_EDGE, CURE_EDGE, JUDGES, PARK_EDGE, _drive, _finding, _make,
    _status, _supervisor)

__all__ = ["ctl", "unix_sock"]

T0 = 1_800_000_000.0   # the fake clock's start, an exact minute boundary
MIN = 60.0


class FakeClock:
    """`now()` is the supervisor's `_now_epoch`; its `_wait_pause_event`
    advances the clock by the whole wait instead of sleeping it."""

    def __init__(self, start: float = T0):
        self.t = start
        self.waits: list[float] = []

    def install(self, sup) -> "FakeClock":
        sup._now_epoch = lambda: self.t   # type: ignore[method-assign]

        async def wait(timeout_s: float) -> bool:
            self.waits.append(timeout_s)
            self.t += timeout_s
            return sup._pause.is_set()
        sup._wait_pause_event = wait      # type: ignore[method-assign]
        return self


def _parked(tmp_path, judge="review", *, rounds_spent=None, incurable=False,
            escalate=False, **cfg):
    """A supervisor parked in `awaiting_input` under a fake clock: on spent
    cure rounds unless it is an incurable finding or a reviewer escalation."""
    clock = FakeClock()
    if rounds_spent is None:
        rounds_spent = not (incurable or escalate)
    if rounds_spent:
        cfg.setdefault("cure_pass_ceiling", 1)
    sup = _make(tmp_path, judge, **cfg)
    clock.install(sup)
    if rounds_spent:
        sup.run.update_status(curePasses={"1": 1})
    outcome = _drive(sup, judge, [_finding(not incurable, "requirement A (#1)")],
                     escalate=escalate)
    assert outcome == ("advance", "paused", PARK_EDGE[judge]), outcome
    assert _status(sup)["state"] == "awaiting_input"
    return sup, clock


def _expires(sup) -> str | None:
    return _status(sup)["inputQuestion"]["expiresAt"]


def _events(sup, kind: str) -> list[dict]:
    return [e for e in read_events(sup.run.root) if e.get("type") == kind]


def _run_paused(sup) -> tuple:
    """The parked step as the dispatch loop runs it: the edge it returns must
    be one the transition table declares from `paused`."""
    outcome = asyncio.run(asyncio.wait_for(sup._step_paused(), 30))
    if outcome[0] == "advance":
        assert sup._advance("paused", outcome[1], outcome[2]) == outcome[1]
    return outcome


# -- 1. the setting -------------------------------------------------------------

def test_the_default_input_timeout_is_sixty_minutes(tmp_path):
    assert JobConfig().input_timeout_min == 60
    sup, _clock = _parked(tmp_path)
    question = _status(sup)["inputQuestion"]
    assert question["expiresAt"] == state.utc_from_epoch(T0 + 60 * MIN)


def test_a_job_yaml_value_sets_the_timeout_and_zero_means_never(tmp_path):
    job = tmp_path / "job.yaml"
    job.write_text("input_timeout_min: 15\n")
    assert JobConfig.load(job).input_timeout_min == 15
    sup, _clock = _parked(tmp_path / "a", input_timeout_min=15)
    assert _expires(sup) == state.utc_from_epoch(T0 + 15 * MIN)

    never, _clock = _parked(tmp_path / "b", input_timeout_min=0)
    assert _expires(never) is None
    assert _status(never)["inputQuestion"]["onTimeout"] == "next_approach"


def _job_yaml(ctl: Ctl, run_id: str) -> dict:
    return yaml.safe_load(
        (ctl.registry / "configs" / run_id / "job.yaml").read_text())


def _start(ctl: Ctl, run_id: str, *extra: str):
    res = ctl.run("start", "--prd", str(ctl.prd), "--llm", "none",
                  "--run-id", run_id, *extra)
    assert res.returncode == 0, res.stderr
    return _job_yaml(ctl, run_id)


def test_start_writes_the_default_timeout_to_job_yaml(ctl: Ctl):
    assert _start(ctl, "tst-it-default")["input_timeout_min"] == 60


def test_start_flag_beats_template_beats_registry_beats_default(ctl: Ctl):
    assert ctl.run("config", "set", "input_timeout_min", "45").returncode == 0
    assert _start(ctl, "tst-it-registry")["input_timeout_min"] == 45

    tdir = ctl.registry / "templates" / "slow"
    tdir.mkdir(parents=True)
    (tdir / "job.yaml").write_text(yaml.safe_dump({"input_timeout": 90}))
    (tdir / "prd.md").write_text("# T\n\nDo it.\n")
    assert _start(ctl, "tst-it-template",
                  "--template", "slow")["input_timeout_min"] == 90

    job = _start(ctl, "tst-it-flag", "--template", "slow",
                 "--input-timeout", "5")
    assert job["input_timeout_min"] == 5
    assert JobConfig.load(
        ctl.registry / "configs" / "tst-it-flag" / "job.yaml"
    ).input_timeout_min == 5


def test_start_input_timeout_zero_is_kept_and_a_negative_one_is_refused(ctl: Ctl):
    assert _start(ctl, "tst-it-zero", "--input-timeout", "0"
                  )["input_timeout_min"] == 0
    res = ctl.run("start", "--prd", str(ctl.prd), "--llm", "none",
                  "--run-id", "tst-it-neg", "--input-timeout", "-3")
    assert res.returncode == 2


def _client(sup):
    return TestClient(create_app(sup.cfg, sup.run, sup))


@pytest.mark.parametrize("spec,minutes", [("+30", 90), ("20", 20), ("0", 0)])
def test_budget_changes_the_timeout_live_and_moves_a_parked_expiry(
        tmp_path, spec, minutes):
    sup, _clock = _parked(tmp_path)
    parked_at = state.parse_utc(_status(sup)["inputQuestion"]["parkedAt"])
    with _client(sup) as client:
        resp = client.patch("/config/budget", json={"inputTimeoutMin": spec})

    assert resp.status_code == 200, resp.text
    assert resp.json()["inputTimeoutMin"] == minutes
    assert resp.json()["previousInputTimeoutMin"] == 60
    assert sup.cfg.input_timeout_min == minutes
    assert _expires(sup) == (
        state.utc_from_epoch(parked_at + minutes * MIN) if minutes else None)
    assert [e["field"] for e in _events(sup, "budget_changed")] == [
        "inputTimeoutMin"]


def test_budget_input_timeout_is_persisted_to_the_runs_job_yaml(ctl: Ctl):
    rdir, cdir = _seed_run(ctl, "tst-it-budget")
    (rdir / "status.json").write_text(json.dumps({"state": "running"}))
    seen = []

    def api(run_id, method, path, body=None, **kw):
        seen.append((method, path, body))
        return {"inputTimeoutMin": 90, "previousInputTimeoutMin": 60}

    cli_main_api, cli_main.api = cli_main.api, api
    try:
        res = ctl.run("budget", "tst-it-budget", "--input-timeout", "+30")
    finally:
        cli_main.api = cli_main_api
    assert res.returncode == 0, res.stderr
    assert seen == [("PATCH", "/config/budget", {"inputTimeoutMin": "+30"})]
    assert _job_yaml(ctl, "tst-it-budget")["input_timeout_min"] == 90
    assert "60 min -> 90 min" in res.stdout


# -- 2. the clock ---------------------------------------------------------------

def test_the_run_deadline_is_frozen_for_the_whole_park(tmp_path):
    sup, clock = _parked(tmp_path)
    deadline, epoch = sup.deadline, sup._deadline_epoch

    async def operator():
        # the operator answers 25 minutes into the park
        while clock.t < T0 + 25 * MIN:
            await asyncio.sleep(0)
        sup.answer(cure_rounds=1)

    async def run():
        task = asyncio.create_task(operator())
        outcome = await sup._step_paused()
        await task
        return outcome

    async def wait(timeout_s):
        clock.t += 5 * MIN
        await asyncio.sleep(0)
        return sup._pause.is_set()
    sup._wait_pause_event = wait   # type: ignore[method-assign]
    outcome = asyncio.run(asyncio.wait_for(run(), 30))

    assert outcome[:2] == ("advance", "cure")
    parked_s = clock.t - T0
    assert parked_s >= 25 * MIN
    assert sup._deadline_epoch == pytest.approx(epoch + parked_s)
    assert sup.deadline == pytest.approx(deadline + parked_s)
    assert _status(sup)["pausedTotalS"] == pytest.approx(parked_s)


def test_expiry_is_absolute_so_a_fresh_engine_process_keeps_it(tmp_path):
    first, clock = _parked(tmp_path)
    expires = _expires(first)
    saved_question = dict(_status(first)["inputQuestion"])
    # the first process dies; its run dir is all a fresh process has
    atomic_write_json(first.run.root / "iterations" / "0001" / "meta.json",
                      {"phase": "worker", "endedAt": "2026-01-01T00:00:00Z"})
    atomic_write_json(first.run.tasks_file, {"version": 1, "tasks": [
        {"id": "001", "title": "t", "status": "validated",
         "successCriteria": ["c"]}]})

    second = _supervisor(tmp_path / "run", status=None, peer_verdict_gate=False,
                         cure_pass_ceiling=2)
    clock.t = T0 + 20 * MIN            # the restart happens 20 minutes in
    clock.install(second)
    seen: dict = {}

    async def wait(timeout_s: float) -> bool:
        if not seen:
            status = second.run.read_status()
            seen.update(state=status["state"],
                        question=dict(status["inputQuestion"]),
                        reason=status["pauseReason"],
                        slice_s=timeout_s)
            second.answer(waive=["F1"])          # the operator answers
        return second._pause.is_set()
    second._wait_pause_event = wait   # type: ignore[method-assign]

    final = asyncio.run(asyncio.wait_for(second._run_job_core(), 60))

    assert seen["state"] == "awaiting_input"
    assert seen["question"] == saved_question
    assert seen["question"]["expiresAt"] == expires
    assert seen["reason"] == PARK_EDGE["review"]
    # 40 of the ORIGINAL 60 minutes are left, not a fresh 60
    assert state.input_expiry_epoch(seen["question"]) - (T0 + 20 * MIN) == 40 * MIN
    assert seen["slice_s"] <= 40 * MIN
    assert final == "succeeded"
    assert not _events(second, "input.timed_out")


def test_a_fresh_process_over_an_expired_park_takes_the_timeout_outcome(
        tmp_path):
    first, clock = _parked(tmp_path)
    atomic_write_json(first.run.root / "iterations" / "0001" / "meta.json",
                      {"phase": "worker", "endedAt": "2026-01-01T00:00:00Z"})
    atomic_write_json(first.run.tasks_file, {"version": 1, "tasks": [
        {"id": "001", "title": "t", "status": "validated",
         "successCriteria": ["c"]}]})
    second = _supervisor(tmp_path / "run", status=None, peer_verdict_gate=False,
                         cure_pass_ceiling=2)
    clock.t = T0 + 61 * MIN            # the container was down past expiry
    clock.install(second)

    async def go():
        # open the episode exactly as `_run_job_core` does, then stop at the
        # first thing it would run after the timeout fired
        second._approach = 1
        second._opening_parked_question = dict(
            second.run.read_status()["inputQuestion"])
        second._pause.clear()
        return await second._step_paused()
    outcome = asyncio.run(asyncio.wait_for(go(), 30))

    assert outcome[:2] == ("advance", "replan")
    (event,) = _events(second, "input.timed_out")
    assert event["outcome"] == "next_approach"


# -- 3. the outcomes (E.3) --------------------------------------------------------

def _expire(sup, clock) -> tuple:
    clock.t = T0 + 60 * MIN
    return _run_paused(sup)


def test_nothing_happens_before_the_expiry(tmp_path):
    sup, clock = _parked(tmp_path)
    released = []

    async def wait(timeout_s: float) -> bool:
        clock.t += timeout_s
        if clock.t >= T0 + 59 * MIN:       # one minute short of the expiry
            released.append(clock.t)
            sup.answer(cure_rounds=1)
        return sup._pause.is_set()
    sup._wait_pause_event = wait   # type: ignore[method-assign]

    outcome = _run_paused(sup)

    assert released and released[0] < T0 + 60 * MIN
    assert outcome[:2] == ("advance", "cure")
    assert not _events(sup, "input.timed_out")
    assert _events(sup, "input.answered")


def test_reviewer_escalation_with_rounds_left_cures_now_from_the_allowance(
        tmp_path):
    sup, clock = _parked(tmp_path, "review", escalate=True,
                         cure_pass_ceiling=3)
    question = _status(sup)["inputQuestion"]
    assert question["trigger"] == "reviewer_escalation"
    assert question["onTimeout"] == "cure_now"
    allowed = sup._cure_rounds_allowed(1)

    outcome = _expire(sup, clock)

    assert outcome == ("advance", "cure", CURE_EDGE["review"])
    assert sup._cure_rounds_used(1) == 1
    assert sup._cure_rounds_allowed(1) == allowed, "no rounds were added"
    assert not sup.run.composite_prd_file.exists(), "no jump to a new approach"
    assert _status(sup)["state"] == "running"
    (event,) = _events(sup, "input.timed_out")
    assert event["outcome"] == "cure_now"
    assert event["trigger"] == "reviewer_escalation"
    assert event["approach"] == 1
    assert not sup.run.waiver_entries(), "accept all: nothing is waived"


def test_an_escalation_over_an_incurable_finding_cannot_cure_so_it_moves_on(
        tmp_path):
    """The timeout acts as an answer that accepts every finding, and cure
    rounds cannot fix a `curable: false` one -- the same refusal a manual
    `--cure-rounds 1` gets -- so the next-approach row applies."""
    sup, clock = _parked(tmp_path, "review", escalate=True, incurable=True,
                         cure_pass_ceiling=3)
    question = _status(sup)["inputQuestion"]
    assert question["trigger"] == "reviewer_escalation"
    assert question["onTimeout"] == "next_approach"

    outcome = _expire(sup, clock)

    assert outcome == ("advance", "replan", APPROACH_LEFT_EDGE["review"])
    (event,) = _events(sup, "input.timed_out")
    assert event["outcome"] == "next_approach"


@pytest.mark.parametrize("judge", JUDGES)
def test_cure_rounds_spent_with_an_approach_left_goes_to_the_next_approach(
        tmp_path, judge):
    sup, clock = _parked(tmp_path, judge, rounds_spent=True)
    question = _status(sup)["inputQuestion"]
    assert question["trigger"] == "cure_rounds_spent"
    assert question["onTimeout"] == "next_approach"

    outcome = _expire(sup, clock)

    assert outcome == ("advance", "replan", APPROACH_LEFT_EDGE[judge])
    assert sup.run.composite_prd_file.exists(), "the approach was archived"
    assert (sup.run.root / "approaches" / "01").is_dir()
    assert _status(sup)["state"] == "running"
    (event,) = _events(sup, "input.timed_out")
    assert event["outcome"] == "next_approach"
    assert event["judge"] == judge
    assert not sup.run.waiver_entries()


@pytest.mark.parametrize("judge", JUDGES)
def test_an_incurable_park_with_an_approach_left_goes_to_the_next_approach(
        tmp_path, judge):
    sup, clock = _parked(tmp_path, judge, incurable=True)
    assert _status(sup)["inputQuestion"]["trigger"] == "incurable"

    outcome = _expire(sup, clock)

    assert outcome == ("advance", "replan", APPROACH_LEFT_EDGE[judge])
    (event,) = _events(sup, "input.timed_out")
    assert (event["outcome"], event["trigger"]) == (
        "next_approach", "incurable")


@pytest.mark.parametrize("judge", JUDGES)
@pytest.mark.parametrize("how", ["rounds_spent", "incurable"])
def test_a_park_with_no_approach_left_holds_exhausted_on_approaches(
        tmp_path, judge, how):
    sup, clock = _parked(tmp_path, judge, max_approaches=1,
                         rounds_spent=how == "rounds_spent",
                         incurable=how == "incurable")
    assert _status(sup)["inputQuestion"]["onTimeout"] == "exhausted_approaches"

    outcome = _expire(sup, clock)

    assert outcome == ("tail",)
    assert sup._finish_tail_unverified() == "exhausted"
    status = _status(sup)
    assert status["state"] == "exhausted"
    assert status["exhaustedBudget"] == "approaches"
    assert status.get("verdict") is None
    (event,) = _events(sup, "input.timed_out")
    assert event["outcome"] == "exhausted_approaches"


def test_a_secret_park_never_expires_whatever_the_setting():
    for minutes in (None, 60, 1, 0):
        question = state.build_input_question(
            judge="review", trigger="cure_rounds_spent", blocking=[],
            cure_rounds_used=0, cure_rounds_allowed=3, approach=1,
            max_approaches=3, parked_at_epoch=T0, timeout_min=minutes,
            kind="secret")
        assert question["expiresAt"] is None
        assert state.input_expiry_epoch(question) is None


def test_the_wait_on_a_secret_park_ends_only_with_an_answer(tmp_path):
    sup, clock = _parked(tmp_path)
    question = dict(_status(sup)["inputQuestion"], kind="secret", expiresAt=None)
    sup.run.update_status(inputQuestion=question)
    turns = []

    async def wait(timeout_s: float) -> bool:
        turns.append(timeout_s)
        clock.t += 30 * 24 * 3600           # a month goes by per turn
        if len(turns) == 3:
            sup._pause.set()
        return sup._pause.is_set()
    sup._wait_pause_event = wait   # type: ignore[method-assign]

    expired = asyncio.run(sup._wait_for_answer_or_expiry())

    assert expired is False and len(turns) == 3
    assert not _events(sup, "input.timed_out")


def test_a_live_timeout_extension_defers_the_outcome(tmp_path):
    sup, clock = _parked(tmp_path)
    sup.set_input_timeout_min(120)
    clock.t = T0 + 60 * MIN
    fired = []

    async def wait(timeout_s: float) -> bool:
        clock.t += timeout_s
        if not fired:
            fired.append(clock.t)
            sup.answer(cure_rounds=1)
        return sup._pause.is_set()
    sup._wait_pause_event = wait   # type: ignore[method-assign]

    outcome = _run_paused(sup)

    assert outcome[:2] == ("advance", "cure")
    assert not _events(sup, "input.timed_out"), "the old expiry no longer applies"


# -- 4. operator_input_needed_pause: false (D.3) ----------------------------------

def test_the_pause_flag_is_a_job_yaml_key(tmp_path):
    job = tmp_path / "job.yaml"
    job.write_text("operator_input_needed_pause: false\n")
    assert JobConfig.load(job).operator_input_needed_pause is False


@pytest.mark.parametrize("judge", JUDGES)
def test_without_the_pause_a_spent_park_fires_at_once_on_the_next_approach(
        tmp_path, judge):
    sup = _make(tmp_path, judge, operator_input_needed_pause=False,
                cure_pass_ceiling=1)
    sup.run.update_status(curePasses={"1": 1})

    outcome = _drive(sup, judge, [_finding(True, "requirement A (#1)")])

    assert outcome == ("advance", "replan", APPROACH_LEFT_EDGE[judge])
    assert _status(sup)["state"] != "awaiting_input"
    parked = _events(sup, "input.parked")
    fired = _events(sup, "input.timed_out")
    assert len(parked) == 1 and len(fired) == 1
    assert parked[0]["trigger"] == "cure_rounds_spent"
    assert parked[0]["judge"] == judge
    assert parked[0]["timeoutAtPark"] is True
    assert parked[0]["expiresAt"] is not None
    assert fired[0]["outcome"] == "next_approach"
    assert parked[0]["id"] < fired[0]["id"]


def test_without_the_pause_a_reviewer_escalation_cures_now(tmp_path):
    sup = _make(tmp_path, "review", operator_input_needed_pause=False,
                cure_pass_ceiling=3)

    outcome = _drive(sup, "review", [_finding(True, "requirement A (#1)")],
                     escalate=True)

    assert outcome == ("advance", "cure", CURE_EDGE["review"])
    (event,) = _events(sup, "input.timed_out")
    assert (event["outcome"], event["trigger"]) == (
        "cure_now", "reviewer_escalation")
    assert len(_events(sup, "input.parked")) == 1


@pytest.mark.parametrize("judge", JUDGES)
def test_without_the_pause_no_approach_left_holds_exhausted(tmp_path, judge):
    sup = _make(tmp_path, judge, operator_input_needed_pause=False,
                max_approaches=1)

    outcome = _drive(sup, judge, [_finding(False, "requirement A (#1)")])

    assert outcome == ("tail",)
    assert sup._finish_tail_unverified() == "exhausted"
    assert _status(sup)["exhaustedBudget"] == "approaches"
    (event,) = _events(sup, "input.timed_out")
    assert event["outcome"] == "exhausted_approaches"
