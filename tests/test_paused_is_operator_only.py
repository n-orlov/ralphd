"""v0.36 requirement A: `paused` is the operator's alone.

A.1  The engine has no call path that sets `state: paused`. Every former
     engine pause reason (the 14 named in `FORMER_REASONS`) is driven to its
     trigger by the real engine (fake hats, a fake clock where time matters)
     and must land on the state the PRD names -- `awaiting_input`, a
     self-heal, `exhausted` or `failed` -- never `paused`. Every status.json
     write of every driven run is recorded, so a transient `paused` between
     two writes fails as well.
A.2  The reasonless in-memory pause is gone: every engine stop is persisted
     to status.json before the engine waits on it. Each driven run records
     the persisted state at every park entry (`_park_until_resumed`), and an
     operator pause is persisted too.
A.3  `pause` records `pausedBy: "operator"` and the operator's note;
     `unpause` of an `awaiting_input` run is refused naming `answer`, of an
     `exhausted` run naming `resume --<budget>`.

Covering nodes of the retired pause paths this module replaced: the
review-contract and findings-channel faults land in
`tests/test_stop_routing_faults.py` (the digest nodes of
`tests/test_other_escalations_digest.py` pinned a pause method that no longer
exists).
"""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path

import pytest

from ralphd.cli import main as cli_main
from ralphd.engine import state
from ralphd.engine import state as state_mod
from ralphd.engine import loop as loop_mod
from ralphd.engine.loop import LoopSupervisor
from ralphd.engine.state import RunDir
from tests import test_repeated_rejection_signature as sigmod
from tests import test_rejection_ladder as ladder
from tests import test_self_heal_stagnation_graph as heal
from tests import test_stop_routing_faults as stops
from tests.conftest import supervisor_factory

ROOT = Path(__file__).resolve().parents[1]

# PRD A.1, verbatim and in order.
FORMER_REASONS = [
    "operator_input_needed",
    "verdict_gate_operator_input_needed",
    "rejected_last_approach",
    "rejected_incurable",
    "rejected_repeated_signature",
    "stagnation",
    "unschedulable_dependency_cycle",
    "unresolvable_dependency_edge",
    "review_contract_unrepaired",
    "review_findings_unusable",
    "judge_no_verdict",
    "rate_limit_ceiling",
    "provider_refused",
    "commit_secret",
]

# What a run may rest in while the engine waits on it, as persisted.
PERSISTED_STOPS = {"paused", "awaiting_input", "exhausted", "finishing",
                   *state.TERMINAL_STATES}


@pytest.fixture
def recorder(monkeypatch):
    """Every `state` any status.json write carries, and the persisted state
    at every moment the engine parks (`_park_until_resumed`)."""
    writes: list[str] = []
    parks: list[str | None] = []
    real_write = state_mod.atomic_write_json

    def recording_write(path, obj):
        if Path(path).name == "status.json" and isinstance(obj, dict):
            writes.append(obj.get("state"))
        return real_write(path, obj)

    monkeypatch.setattr(state_mod, "atomic_write_json", recording_write)
    if hasattr(loop_mod, "atomic_write_json"):
        monkeypatch.setattr(loop_mod, "atomic_write_json", recording_write)

    real_park = LoopSupervisor._park_until_resumed

    async def recording_park(self):
        parks.append(self.run.read_status().get("state"))
        return await real_park(self)

    monkeypatch.setattr(LoopSupervisor, "_park_until_resumed", recording_park)
    return writes, parks


# ---------------------------------------------------------------------------
# the 14 triggers: each returns the state the run rests in
# ---------------------------------------------------------------------------

def _park(tmp_path, judge, items, *, trigger, **cfg) -> str:
    sup = ladder._make(tmp_path, judge, **cfg)
    outcome = ladder._drive(sup, judge, items)
    assert outcome == ("advance", "paused", ladder.PARK_EDGE[judge])
    status = sup.run.read_status()
    assert status["inputQuestion"]["trigger"] == trigger
    assert status["inputQuestion"]["judge"] == judge
    assert status["pauseReason"] == ladder.PARK_EDGE[judge]
    assert sup._pause.is_set() is False, "the engine waits on a persisted state"
    return status["state"]


def _operator_input_needed(tmp_path):
    # review, the approach's cure rounds are spent
    sup = ladder._make(tmp_path, "review", cure_pass_ceiling=1)
    sup.run.update_status(curePasses={"1": 1})
    ladder._drive(sup, "review", [ladder._finding(True, "requirement A")])
    assert sup.run.read_status()["inputQuestion"]["trigger"] == "cure_rounds_spent"
    return sup.run.read_status()["state"]


def _verdict_gate_operator_input_needed(tmp_path):
    return _park(tmp_path, "verdict_gate",
                 [ladder._finding(False, "requirement B (#111)")],
                 trigger="incurable")


def _rejected_last_approach(tmp_path):
    # the last approach, curable findings, no cure round left
    sup = ladder._make(tmp_path, "review", cure_pass_ceiling=0, max_approaches=1)
    ladder._drive(sup, "review", [ladder._finding(True, "requirement D.1")])
    question = sup.run.read_status()["inputQuestion"]
    assert question["onTimeout"] == state.INPUT_ON_TIMEOUT_EXHAUSTED
    return sup.run.read_status()["state"]


def _rejected_incurable(tmp_path):
    # the 0.35 hold was in memory only (status.json said `running`)
    return _park(tmp_path, "review",
                 [ladder._finding(False, "requirement B (#111)")],
                 trigger="incurable")


def _rejected_repeated_signature(tmp_path):
    # the same blocking-clause set again is one more FAILED cure round with
    # rounds left: no park, no archive, no pause.
    sup = ladder._make(tmp_path, "review", cure_pass_ceiling=3)
    clause = "requirement E.5 (#190): tests still fail"
    sup.run.update_status(rejectionSignatures={
        "1": sigmod._sig(sup, clause)})
    outcome = ladder._drive(sup, "review", [ladder._finding(True, clause)])
    assert outcome == ("advance", "cure", ladder.CURE_EDGE["review"])
    assert sup._cure_rounds_used(1) == 1
    assert not sup.run.composite_prd_file.exists()
    return sup.run.read_status().get("state") or "running"


def _stagnation(tmp_path):
    root = tmp_path / "run"
    sup = heal._supervisor(
        root, {"planning": [lambda s, p: heal._propose(s, [heal._task()])],
               "worker": [heal._QuietWorker()]},
        max_approaches=2, workspace="ws")
    RunDir(root=root).update_status(state="starting")
    asyncio.run(asyncio.wait_for(sup._run_job_core(), 120))
    status = sup.run.read_status()
    assert status["exhaustedBudget"] == "approaches"
    assert sorted(d.name for d in (root / "approaches").iterdir()) == ["01", "02"]
    return status["state"]


def _graph(kind):
    def drive(tmp_path):
        sup, _script, _root = heal._graph_run(tmp_path, kind, rebreak=True)
        asyncio.run(asyncio.wait_for(sup._run_job_core(), 120))
        status = sup.run.read_status()
        assert status["ending"] == "unschedulable_task_graph"
        return status["state"]
    return drive


def _judge_failed(case):
    def drive(tmp_path):
        sup, final, seen = stops._run(
            tmp_path / "run", infra_outage_budget_s=0.0, **case)
        assert seen == []
        status = sup.run.read_status()
        assert status["verdict"] == "unreviewed", status
        return status["state"]
    return drive


def _faulted(step, ending, **cfg):
    def drive(tmp_path):
        sup, _final, seen = stops._faulted_worker_run(tmp_path, step, **cfg)
        assert seen == []
        status = sup.run.read_status()
        assert status["ending"] == ending, status
        return status["state"]
    return drive


def _commit_secret(tmp_path):
    # the live-secret redaction fixture is applied by the test below
    sup, _ws, _base, _head = stops._parked_on_a_secret(tmp_path)
    status = sup.run.read_status()
    assert status["inputQuestion"]["kind"] == "secret"
    assert sup._pause.is_set() is False
    return status["state"]


# reason -> (driver, the state the PRD names for it)
DRIVERS = {
    "operator_input_needed": (_operator_input_needed, "awaiting_input"),
    "verdict_gate_operator_input_needed": (
        _verdict_gate_operator_input_needed, "awaiting_input"),
    "rejected_last_approach": (_rejected_last_approach, "awaiting_input"),
    "rejected_incurable": (_rejected_incurable, "awaiting_input"),
    "rejected_repeated_signature": (_rejected_repeated_signature, "running"),
    "stagnation": (_stagnation, "exhausted"),
    "unschedulable_dependency_cycle": (_graph("cycle"), "failed"),
    "unresolvable_dependency_edge": (_graph("edge"), "failed"),
    "review_contract_unrepaired": (
        _judge_failed(dict(review=[stops._review_prose])), "failed"),
    "review_findings_unusable": (
        _judge_failed(dict(review=[stops._findings_unusable])), "failed"),
    "judge_no_verdict": (
        _judge_failed(dict(gate=[stops._gate_truncated])), "failed"),
    "rate_limit_ceiling": (
        _faulted(stops._rate_limited, "rate_limit_ceiling",
                 rate_limit_max_wait_s=300), "failed"),
    "provider_refused": (
        _faulted(stops._refused, "provider_refused"), "failed"),
    "commit_secret": (_commit_secret, "awaiting_input"),
}


def test_the_guard_lists_exactly_the_fourteen_former_reasons_of_the_prd():
    assert len(FORMER_REASONS) == 14 and len(set(FORMER_REASONS)) == 14
    assert list(DRIVERS) == FORMER_REASONS


@pytest.mark.parametrize("reason", FORMER_REASONS)
def test_a_former_pause_reason_never_lands_on_paused(
        reason, tmp_path, recorder, live_secret):
    writes, parks = recorder
    driver, expected = DRIVERS[reason]

    resting = driver(tmp_path)

    assert resting == expected, (reason, resting)
    assert resting != "paused"
    assert "paused" not in writes, (
        f"{reason}: status.json passed through `paused`: {writes}")
    # A.2: whenever the engine waited, the stop was already on disk.
    assert all(p in PERSISTED_STOPS for p in parks), (reason, parks)


live_secret = stops.live_secret  # the fixture the commit-secret trigger needs


# ---------------------------------------------------------------------------
# A.1 (static): `paused` has exactly one writer, the operator's pause
# ---------------------------------------------------------------------------

def test_the_only_code_that_writes_state_paused_is_the_operator_pause():
    source = (ROOT / "src/ralphd/engine/loop.py").read_text()
    writers = [m.start() for m in re.finditer(r"state=PAUSED_STATE", source)]
    assert len(writers) == 1
    enclosing = source.rfind("    def ", 0, writers[0])
    assert source[enclosing:].startswith("    def _persist_operator_pause(")
    # no other engine module writes the literal either
    for path in (ROOT / "src/ralphd/engine").glob("*.py"):
        text = path.read_text()
        assert not re.search(r"state\s*=\s*[\"']paused[\"']", text), path.name


def test_pause_takes_no_engine_reason():
    import inspect
    params = list(inspect.signature(LoopSupervisor.pause).parameters)
    assert params == ["self", "note"]
    assert not hasattr(LoopSupervisor, "_persist_pause")


# ---------------------------------------------------------------------------
# A.2 / A.3: the operator's pause is persisted, carries pausedBy and the note
# ---------------------------------------------------------------------------

_supervisor = supervisor_factory(run_id="paused-operator-only",
                                 status={"state": "running"})


def test_an_operator_pause_is_persisted_before_the_engine_waits_on_it(
        tmp_path, recorder):
    _writes, parks = recorder
    sup = _supervisor(tmp_path / "run")

    sup.pause("holding for the migration")
    status = sup.run.read_status()
    assert status["state"] == "paused"
    assert status["pausedBy"] == "operator"
    assert status["pauseNote"] == "holding for the migration"
    assert status.get("pauseReason") is None

    async def hold_then_release():
        waiting = asyncio.ensure_future(sup._gate())
        await asyncio.sleep(0.01)
        assert not waiting.done()
        sup.resume()
        await asyncio.wait_for(waiting, 5)

    asyncio.run(hold_then_release())
    assert parks == ["paused"], "the state was on disk when the engine waited"
    released = sup.run.read_status()
    assert released["state"] == "running"
    assert released["pausedBy"] is None and released["pauseNote"] is None


def test_an_operator_pause_without_a_note_records_pausedby_alone(tmp_path):
    sup = _supervisor(tmp_path / "run")
    sup.pause()
    status = sup.run.read_status()
    assert status["state"] == "paused" and status["pausedBy"] == "operator"
    assert status["pauseNote"] is None


def test_an_operator_pause_never_overwrites_a_question_or_a_hold(tmp_path):
    for held in ("awaiting_input", "exhausted", "failed"):
        sup = _supervisor(tmp_path / held)
        sup.run.update_status(state=held)
        sup.pause("note")
        status = sup.run.read_status()
        assert status["state"] == held
        assert status.get("pausedBy") is None


# -- unpause of a run that is not paused -------------------------------------

def test_unpause_of_an_awaiting_input_run_is_refused_naming_answer():
    text = state.unpause_refusal(
        "r1", {"state": "awaiting_input", "inputQuestion": {"kind": "findings"}})
    assert "ralphctl answer r1" in text


@pytest.mark.parametrize("budget,flag", [
    ("iterations", "--iterations"), ("time", "--timeout"),
    ("approaches", "--approaches")])
def test_unpause_of_an_exhausted_run_is_refused_naming_resume_budget(budget, flag):
    text = state.unpause_refusal(
        "r1", {"state": "exhausted", "exhaustedBudget": budget})
    assert f"ralphctl resume r1 {flag}" in text


def test_unpause_of_a_paused_or_running_run_is_not_refused():
    assert state.unpause_refusal("r1", {"state": "paused"}) is None
    assert state.unpause_refusal("r1", {"state": "running"}) is None


def test_the_engine_resume_route_refuses_a_run_that_is_not_paused(tmp_path):
    import httpx
    from ralphd.engine.api import create_app

    def post_resume(status_state, **extra):
        sup = _supervisor(tmp_path / status_state)
        sup.run.update_status(state=status_state, **extra)

        async def go():
            app = create_app(sup.cfg, sup.run, sup)
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport,
                                         base_url="http://engine") as client:
                return await client.post("/resume")
        return asyncio.run(go())

    awaiting = post_resume("awaiting_input", inputQuestion={"kind": "findings"})
    assert awaiting.status_code == 409
    assert "ralphctl answer" in awaiting.text
    exhausted = post_resume("exhausted", exhaustedBudget="time")
    assert exhausted.status_code == 409
    assert "ralphctl resume" in exhausted.text and "--timeout" in exhausted.text
    paused = post_resume("paused", pausedBy="operator")
    assert paused.status_code == 200 and paused.json() == {"resumed": True}


def test_the_pause_route_records_the_note(tmp_path):
    import httpx
    from ralphd.engine.api import create_app
    sup = _supervisor(tmp_path / "run")

    async def go():
        app = create_app(sup.cfg, sup.run, sup)
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport,
                                     base_url="http://engine") as client:
            return await client.post("/pause", json={"note": "deploy window"})
    resp = asyncio.run(go())
    assert resp.status_code == 200
    status = sup.run.read_status()
    assert status["pausedBy"] == "operator"
    assert status["pauseNote"] == "deploy window"


def test_ralphctl_unpause_refuses_before_any_http_call(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("RALPHD_REGISTRY", str(tmp_path))
    run_id = "r-unpause-refused"
    root = cli_main.run_root(run_id)
    root.mkdir(parents=True)
    (root / "status.json").write_text(json.dumps(
        {"state": "exhausted", "exhaustedBudget": "iterations"}))
    called = []
    monkeypatch.setattr(cli_main, "api", lambda *a, **k: called.append(a))
    args = cli_main.build_parser().parse_args(["unpause", run_id])
    with pytest.raises(SystemExit) as exit_info:
        args.func(args)
    assert exit_info.value.code == 5
    assert called == []
    assert f"ralphctl resume {run_id} --iterations" in capsys.readouterr().err


def test_ralphctl_pause_carries_the_note():
    args = cli_main.build_parser().parse_args(
        ["pause", "some-run", "--note", "back soon"])
    assert args.note == "back soon"
