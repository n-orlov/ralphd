"""PRD v0.37 requirements C.2, C.4 (the pass preconditions) and C.5: the
phase-scoped peer verdict gate, what keeps a passing phase review from
opening its gate, and the resume of a phase review that crashed.

The loop is driven with fake hats over a git workspace, as
`tests/test_phase_review_dispatch.py` does (its helpers are reused); nothing
sleeps."""

from __future__ import annotations

import asyncio
import json

import pytest

from ralphd.engine import petitions, state
from ralphd.engine.state import RunDir, atomic_write_json

from tests.conftest import scripted_supervisor
from tests.test_phase_review_dispatch import (
    _events, _git, _init_workspace, _ok, _plan, _worker)

_supervisor = scripted_supervisor(
    run_id="phase-gate-peer", run_root="run", workspace="workspace",
    iterations=40, max_approaches=1, vigilant=False, on_complete="exit",
    infra_outage_budget_s=1000.0, operator_input_needed_pause=False)


class _Judges:
    """Review and peer fakes: every prompt and the gate state each one saw."""

    def __init__(self, on_review=None):
        self.reviews: list[dict] = []
        self.peers: list[dict] = []
        self.on_review = on_review

    def review(self, sup, prompt=""):
        scoped = "## Phase review" in prompt
        self.reviews.append({"scoped": scoped, "prompt": prompt})
        if scoped and self.on_review:
            self.on_review(sup, len([r for r in self.reviews if r["scoped"]]))
        return _ok(json.dumps({"verdict": "verified", "reason": "fine"}))

    def peer(self, sup, prompt=""):
        data = sup.run.read_tasks()
        self.peers.append({
            "scoped": "## Phase review" in prompt, "prompt": prompt,
            "gates": [p["gate"] for p in data["phases"]]})
        return _ok(json.dumps({"verdict": "verified", "reason": "agreed"}))


def _run(tmp_path, judges, extra_hats=None, **cfg):
    root = tmp_path / "run"
    ws = tmp_path / "workspace"
    _init_workspace(ws)
    RunDir(root=root).update_status(state="starting")
    hats = {"planning": [_plan], "worker": [_worker],
            "review": [judges.review], "verdict_gate": [judges.peer],
            **(extra_hats or {})}
    sup = _supervisor(tmp_path, hats, **cfg)
    final = asyncio.run(asyncio.wait_for(sup._run_job_core(), 60))
    return sup, final


# -- C.2: the peer step -------------------------------------------------------

def test_with_the_peer_gate_on_a_passing_phase_review_is_followed_by_the_phase_scoped_peer_gate(tmp_path):
    judges = _Judges()
    sup, final = _run(tmp_path, judges, peer_verdict_gate=True)
    assert final == "succeeded", sup.run.read_status()
    # plan, worker 001, phase review, PEER, worker 002, final review, final peer.
    assert sup.runner.phases_seen == [
        "planning", "worker", "review", "verdict_gate", "worker", "review",
        "verdict_gate"]
    phase_peer, final_peer = judges.peers
    # the peer's prompt carries the same `## Phase review` section ...
    assert phase_peer["scoped"]
    assert "Phase id: `one`" in phase_peer["prompt"]
    assert "Review focus: f1" in phase_peer["prompt"]
    assert "criterion one" in phase_peer["prompt"]
    assert "criterion two" not in phase_peer["prompt"]
    # ... and the gate is still `reviewing` while it runs
    assert phase_peer["gates"] == ["reviewing", "closed"]
    # the final peer gate on the same plan carries no such section.
    assert not final_peer["scoped"]
    assert [r["scoped"] for r in judges.reviews] == [True, False]
    # both verdicts are recorded under artifacts/phase-reviews/ and listed.
    rec = sup.run.artifacts_dir / "phase-reviews"
    assert sorted(p.name for p in rec.iterdir()) == [
        "one-r1-review.json", "one-r1-review.md",
        "one-r1-verdict_gate.json", "one-r1-verdict_gate.md"]
    peer_record = json.loads((rec / "one-r1-verdict_gate.json").read_text())
    assert peer_record["verdict"] == "verified"
    assert peer_record["reason"] == "agreed"
    assert peer_record["planPhase"] == "one"
    assert peer_record["startHead"]
    assert peer_record["headReviewed"]
    assert (rec / "one-r1-verdict_gate.md").read_text().strip()
    one, two = sup.run.read_tasks()["phases"]
    assert one["records"] == [
        "artifacts/phase-reviews/one-r1-review.json",
        "artifacts/phase-reviews/one-r1-review.md",
        "artifacts/phase-reviews/one-r1-verdict_gate.json",
        "artifacts/phase-reviews/one-r1-verdict_gate.md"]
    assert one["gate"] == "passed" and two["records"] == []
    passed = _events(sup, "phase_gate.passed")
    assert [e["planPhase"] for e in passed] == ["one"]
    assert len(passed[0]["records"]) == 4


def test_with_the_peer_gate_off_a_passing_phase_review_opens_the_gate_directly(tmp_path):
    judges = _Judges()
    sup, final = _run(tmp_path, judges, peer_verdict_gate=False)
    assert final == "succeeded", sup.run.read_status()
    assert sup.runner.phases_seen == [
        "planning", "worker", "review", "worker", "review"]
    assert judges.peers == []
    rec = sup.run.artifacts_dir / "phase-reviews"
    assert sorted(p.name for p in rec.iterdir()) == [
        "one-r1-review.json", "one-r1-review.md"]
    assert sup.run.read_tasks()["phases"][0]["gate"] == "passed"


def test_a_peer_rejection_leaves_the_gate_closed_and_records_the_rejection(tmp_path):
    """The rungs a phase peer rejection climbs belong to the ladder; what
    this node pins is that a rejecting peer never opens the gate."""
    judges = _Judges()
    judges.peer = lambda sup, prompt="": _ok(json.dumps(
        {"verdict": "rejected", "reason": "not convinced"}))
    root = tmp_path / "run"
    ws = tmp_path / "workspace"
    _init_workspace(ws)
    RunDir(root=root).update_status(state="starting")
    (root / "prd.md").write_text("# PRD\n")
    sup = _supervisor(tmp_path, {
        "planning": [_plan], "worker": [_worker, _plan],
        "review": [judges.review], "verdict_gate": [judges.peer]},
        peer_verdict_gate=True)
    sup._approach = 1
    _seed(sup, [("001", "one", "completed"), ("002", "two", "pending")])
    asyncio.run(sup._run_phase_review("one"))
    data = sup.run.read_tasks()
    assert data["phases"][0]["gate"] == "closed"
    record = json.loads(
        (sup.run.artifacts_dir / "phase-reviews"
         / "one-r1-verdict_gate.json").read_text())
    assert record["verdict"] == "rejected" and record["reason"] == "not convinced"
    assert _events(sup, "phase_gate.passed") == []


# -- C.4: what a pass waits for ----------------------------------------------

def _seed(sup, tasks, gate="closed", **extra_task):
    ws = sup.workspace
    head = _git(ws, "rev-parse", "HEAD")
    doc = {
        "version": 1,
        "phases": [
            {"id": "one", "title": "Phase one", "reviewFocus": "f1",
             "gate": gate, "rounds": 0, "startHead": head, "records": []},
            {"id": "two", "title": "Phase two", "reviewFocus": "f2",
             "gate": "closed", "rounds": 0, "startHead": head,
             "records": []}],
        "tasks": []}
    for tid, phase, status, *origin in tasks:
        t = {"id": tid, "title": f"thing {tid}", "phase": phase, "status": status,
             "successCriteria": f"criterion {tid}", "dependsOn": []}
        if origin:
            t["origin"] = origin[0]
        doc["tasks"].append(t)
    sup.run.write_tasks(doc)


def _seeded_sup(tmp_path, hats, tasks, **cfg):
    ws = tmp_path / "workspace"
    _init_workspace(ws)
    RunDir(root=tmp_path / "run").update_status(state="starting")
    sup = _supervisor(tmp_path, hats, **cfg)
    sup._approach = 1
    _seed(sup, tasks)
    return sup


def _steer_settles(sup, prompt=""):
    return _ok(json.dumps({"settled": True}))


def test_an_unsettled_steer_is_applied_before_a_passing_phase_review_is_honoured(tmp_path):
    def steer_arrives(sup, round_):
        if round_ == 1:
            sup.run.add_steering("also mention the thing", "late-note")

    judges = _Judges(on_review=steer_arrives)
    sup, final = _run(tmp_path, judges, extra_hats={"steer": [_steer_settles]},
                      peer_verdict_gate=False)
    assert final == "succeeded", sup.run.read_status()
    # the first phase review passed, but the steer ran before the gate opened;
    # the phase was then reviewed again (round 2) before phase two began.
    assert sup.runner.phases_seen == [
        "planning", "worker", "review", "steer", "review", "worker", "review"]
    assert [r["scoped"] for r in judges.reviews] == [True, True, False]
    passed = _events(sup, "phase_gate.passed")
    assert [(e["planPhase"], e["round"]) for e in passed] == [("one", 2)]
    log = [e for e in _events(sup, "log")
           if "not honoured" in str(e.get("message"))]
    assert len(log) == 1 and "late-note" in log[0]["message"]
    one = sup.run.read_tasks()["phases"][0]
    assert one["gate"] == "passed"
    assert [r for r in one["records"] if r.endswith(".json")] == [
        "artifacts/phase-reviews/one-r1-review.json",
        "artifacts/phase-reviews/one-r2-review.json"]


@pytest.mark.parametrize("origin", ["steer", "review-cure"])
def test_only_this_phases_steering_and_review_cure_tasks_hold_the_gate(tmp_path, origin):
    judges = _Judges()
    # an open task of this origin in the LATER phase does not hold phase one
    sup = _seeded_sup(
        tmp_path, {"review": [judges.review]},
        [("001", "one", "validated"), ("002", "two", "pending", origin)])
    assert sup._phase_pass_blockers("one") == []
    asyncio.run(sup._run_phase_review("one"))
    assert sup.run.read_tasks()["phases"][0]["gate"] == "passed"
    assert [e["planPhase"] for e in _events(sup, "phase_gate.passed")] == ["one"]
    # ... while the same task in this phase is named as a blocker
    sup2 = _seeded_sup(
        tmp_path / "again", {"review": [judges.review]},
        [("001", "one", "validated"), ("002", "one", "pending", origin),
         ("003", "two", "pending")])
    blockers = sup2._phase_pass_blockers("one")
    assert len(blockers) == 1 and "002" in blockers[0]


def test_a_passing_review_whose_petition_remedy_was_inserted_leaves_the_gate_closed(tmp_path):
    obligation = "Task 001: thing 001"

    def review(sup, prompt=""):
        return _ok(json.dumps({
            "verdict": "verified", "reason": "fine",
            "petition_adjudications": [{
                "obligation": obligation, "adjudication": "REFUTED",
                "remedy": "Fix the fixture. Done when: the fixture renders."}]}))

    sup = _seeded_sup(
        tmp_path, {"review": [review]},
        [("001", "one", "failed"), ("002", "two", "pending")])
    assert petitions.file_worker_petition(
        sup.workspace, obligation, "environmental", "measured in the sandbox",
        run=sup.run) == []
    asyncio.run(sup._run_phase_review("one"))
    data = sup.run.read_tasks()
    assert data["phases"][0]["gate"] == "closed"
    assert _events(sup, "phase_gate.passed") == []
    (remedy,) = [t for t in data["tasks"] if t.get("remedyFor") == obligation]
    assert remedy["phase"] == "one" and remedy["status"] == "pending"
    # the verdict is recorded; the phase is NOT parked for this process ...
    assert data["phases"][0]["records"][0].endswith("one-r1-review.json")
    assert sup._phase_review_unresolved == set()
    # ... and is not due while the remedy is open, but is due once terminal.
    assert sup._due_phase_for_review() is None
    for t in data["tasks"]:
        if t["id"] == remedy["id"]:
            t["status"] = "validated"
    sup.run.write_tasks(data)
    assert sup._due_phase_for_review() == "one"


# -- C.5: resume ---------------------------------------------------------------

def test_resume_point_over_a_reviewing_gate_redispatches_that_phase_review_not_the_final_review(tmp_path):
    judges = _Judges()
    ws = tmp_path / "workspace"
    _init_workspace(ws)
    run = RunDir(root=tmp_path / "run")
    run.update_status(state="running", phase="review", approach=1)
    # iteration 1 finished; iteration 2 is the phase review the process died in
    for n, ended in ((1, True), (2, False)):
        meta = {"phase": "worker" if n == 1 else "review", "iteration": n,
                "startedAt": "2026-01-01T00:00:00Z"}
        if ended:
            meta["endedAt"] = "2026-01-01T00:01:00Z"
        (run.iteration_dir(n) / "meta.json").write_text(json.dumps(meta))
    sup = _supervisor(tmp_path, {"review": [judges.review]})
    sup._approach = 1
    _seed(sup, [("001", "one", "completed"), ("002", "two", "pending")],
          gate="reviewing")
    # the dead attempt's checkpoint must not leak into the re-dispatch
    sup.run.review_early_verdict_file.write_text('{"verdict": "verified"}')
    fresh = _supervisor(tmp_path, {"review": [judges.review]})
    assert fresh.iterations_used == 1
    assert fresh._crashed_mid_review()  # a bare resume would run the FINAL review
    approach, skip_planning, step = fresh._resume_point()
    assert (approach, skip_planning) == (1, True)
    assert step == "worker" and step != "review"
    data = fresh.run.read_tasks()
    # the gate stays `reviewing` on disk until the re-dispatch (a second crash
    # before it must resume the same way); the dispatch is marked in memory.
    assert data["phases"][0]["gate"] == "reviewing"
    assert not fresh.run.review_early_verdict_file.exists()
    # the worker pass re-dispatches the PHASE review as a fresh first attempt
    fresh._approach = 1
    assert fresh._due_phase_for_review() == "one"
    asyncio.run(fresh._run_phase_review("one"))
    assert [r["scoped"] for r in judges.reviews] == [True]
    assert fresh.run.read_tasks()["phases"][0]["gate"] == "passed"
    assert [e["round"] for e in _events(fresh, "phase_gate.due")] == [1]


# -- C.5 with the B.7 worker-exit backstops: nonterminal tasks ---------------

def _crashed_phase_review(tmp_path, tasks, *, edge=None, scoped=True,
                          hats=None, **cfg):
    """A run dir whose last process died inside a phase review (iteration 2,
    never ended) with the first gate persisted `reviewing`; returns a fresh
    supervisor over it, before `_resume_point`."""
    _init_workspace(tmp_path / "workspace")
    run = RunDir(root=tmp_path / "run")
    run.update_status(state="running", phase="review", approach=1)
    for n, ended in ((1, True), (2, False)):
        meta = {"phase": "worker" if n == 1 else "review", "iteration": n,
                "startedAt": "2026-01-01T00:00:00Z"}
        if ended:
            meta["endedAt"] = "2026-01-01T00:01:00Z"
        elif scoped:
            meta.update(planPhase="one", scope="phase")
            if edge:
                meta["edge"] = edge
        (run.iteration_dir(n) / "meta.json").write_text(json.dumps(meta))
    first = _supervisor(tmp_path, hats or {})
    first._approach = 1
    _seed(first, tasks, gate="reviewing")
    first.run.review_early_verdict_file.write_text('{"verdict": "verified"}')
    fresh = _supervisor(tmp_path, hats or {}, **cfg)
    fresh._approach = 1
    return fresh


def _stop_after_first_worker(sup, prompt=""):
    sup.abort("end observation after the first worker")
    return _ok(json.dumps({"verdict": "no_verdict"}))


@pytest.mark.parametrize("tasks", [
    [("001", "one", "pending"), ("002", "two", "pending")],
    [("001", "one", "completed"), ("002", "one", "in-progress"),
     ("003", "two", "pending")],
    [("001", "one", "validation-failed"), ("002", "two", "pending")],
    [("001", "one", "completed"), ("002", "one", "pending", "review-cure"),
     ("003", "two", "pending")],
])
def test_a_reviewing_gate_over_nonterminal_tasks_is_re_reviewed_before_any_worker(tmp_path, tasks):
    judges = _Judges()
    fresh = _crashed_phase_review(
        tmp_path, tasks, edge="verdict_ready",
        hats={"review": [judges.review], "worker": [_stop_after_first_worker]})
    assert fresh._resume_point() == (1, True, "worker")
    # not due by the ordinary terminal-task predicate ...
    assert not state.phase_is_due(fresh.run.read_tasks(), "one")
    # ... but marked for re-dispatch by the resume
    assert fresh._due_phase_for_review() == "one"
    asyncio.run(fresh._step_worker())
    assert fresh.runner.phases_seen[:2] == ["review", "worker"], \
        fresh.runner.phases_seen
    assert [r["scoped"] for r in judges.reviews] == [True]  # never the final
    due = _events(fresh, "phase_gate.due")
    assert [(e["planPhase"], e["round"], e.get("edge")) for e in due] == [
        ("one", 1, "verdict_ready")]
    assert not fresh.run.review_early_verdict_file.exists()


def test_the_resumed_dispatch_without_an_edge_in_the_crashed_meta_carries_none(tmp_path):
    judges = _Judges()
    fresh = _crashed_phase_review(
        tmp_path, [("001", "one", "pending"), ("002", "two", "pending")],
        scoped=False,
        hats={"review": [judges.review], "worker": [_stop_after_first_worker]})
    assert fresh._resume_point() == (1, True, "worker")
    asyncio.run(fresh._step_worker())
    assert fresh.runner.phases_seen[0] == "review"
    due = _events(fresh, "phase_gate.due")
    assert [(e["planPhase"], "edge" in e) for e in due] == [("one", False)]


def test_a_second_crash_before_the_re_dispatch_resumes_the_same_way(tmp_path):
    judges = _Judges()
    tasks = [("001", "one", "pending"), ("002", "two", "pending")]
    first = _crashed_phase_review(tmp_path, tasks, edge="blocked_with_failures",
                                  hats={"review": [judges.review]})
    assert first._resume_point() == (1, True, "worker")
    # the process dies again before the worker pass dispatches the review
    second = _supervisor(tmp_path, {"review": [judges.review]})
    second._approach = 1
    assert second.run.read_tasks()["phases"][0]["gate"] == "reviewing"
    assert second._resume_point() == (1, True, "worker")
    assert second._due_phase_for_review() == "one"


def test_a_peer_stage_crash_re_runs_the_phase_review_then_the_peer_as_a_fresh_first_attempt(tmp_path):
    judges = _Judges()
    fresh = _crashed_phase_review(
        tmp_path, [("001", "one", "completed"), ("002", "two", "pending")],
        hats={"review": [judges.review], "verdict_gate": [judges.peer],
              "worker": [_stop_after_first_worker]},
        peer_verdict_gate=True)
    assert fresh._resume_point() == (1, True, "worker")
    asyncio.run(fresh._step_worker())
    assert fresh.runner.phases_seen[:3] == ["review", "verdict_gate", "worker"]
    assert [r["scoped"] for r in judges.reviews] == [True]
    assert [p["scoped"] for p in judges.peers] == [True]
    assert fresh.run.read_tasks()["phases"][0]["gate"] == "passed"
    assert [e["round"] for e in _events(fresh, "phase_gate.due")] == [1]


def test_a_non_reviewing_gate_leaves_nothing_to_re_dispatch(tmp_path):
    fresh = _crashed_phase_review(
        tmp_path, [("001", "one", "pending"), ("002", "two", "pending")])
    data = fresh.run.read_tasks()
    state.set_phase_gate(data, "one", "closed")
    fresh.run.write_tasks(data)
    fresh._resume_point()
    assert fresh._phase_review_resume == {}
    assert fresh._due_phase_for_review() is None


def test_a_crash_after_a_remedy_insertion_re_reviews_the_phase_before_any_worker(tmp_path):
    obligation = "Task 001: thing 001"

    def review(sup, prompt=""):
        return _ok(json.dumps({
            "verdict": "verified",
            "petition_adjudications": [{
                "obligation": obligation, "adjudication": "REFUTED",
                "remedy": "Render the fixture. Done when: the fixture renders."}]}))

    class SimulatedCrash(BaseException):
        pass

    sup = _seeded_sup(tmp_path, {"review": [review]},
                      [("001", "one", "failed"), ("002", "two", "pending")])
    assert petitions.file_worker_petition(
        sup.workspace, obligation, "environmental", "fixture unavailable",
        run=sup.run) == []

    def crash(pid):
        raise SimulatedCrash()

    sup._phase_pass_blockers = crash
    with pytest.raises(SimulatedCrash):
        asyncio.run(sup._run_phase_review("one"))
    data = sup.run.read_tasks()
    assert data["phases"][0]["gate"] == "reviewing"
    assert any(t.get("remedyFor") == obligation and t["status"] == "pending"
               for t in data["tasks"])
    sup.run.update_status(state="running", phase="review", approach=1)
    judges = _Judges()
    fresh = _supervisor(tmp_path, {"review": [judges.review],
                                   "worker": [_stop_after_first_worker]})
    fresh._approach = 1
    assert fresh._resume_point() == (1, True, "worker")
    asyncio.run(fresh._step_worker())
    assert fresh.runner.phases_seen[0] == "review", fresh.runner.phases_seen
    assert [r["scoped"] for r in judges.reviews] == [True]
