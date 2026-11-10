"""PRD v0.37 requirement D.4 (and C.4's remedy bullet): a curable phase
rejection is cured INSIDE its phase, with phase-scoped cure lineage, a
confirmation re-review once the cure (or a petition remedy) is terminal, and
one cure allowance shared with the final review and the final peer gate.

The loop is driven with fake hats over a git workspace, as
`tests/test_phase_review_dispatch.py` does (its helpers are reused); nothing
sleeps."""

from __future__ import annotations

import asyncio
import json

from ralphd.engine import findings, petitions, state
from ralphd.engine.state import RunDir, atomic_write_json

from tests.conftest import scripted_supervisor
from tests.test_phase_review_dispatch import (
    _events, _git, _init_workspace, _ok, _plan, _worker)

_supervisor = scripted_supervisor(
    run_id="phase-gate-cure", run_root="run", workspace="workspace",
    iterations=60, max_approaches=1, vigilant=False, on_complete="exit",
    infra_outage_budget_s=1000.0, operator_input_needed_pause=False)


def _finding(clause: str, **extra) -> dict:
    return {"severity": "blocking", "curable": True, "clause": clause,
            "evidence": f"{clause} is not met",
            "cured_when": f"{clause} is met", **extra}


def _two_findings() -> list[dict]:
    return [_finding("requirement A"), _finding("requirement B")]


def _rejecting(sup, items=None, *, gate=False):
    path = (sup.run.verdict_gate_findings_json_file if gate
            else sup.run.findings_json_file)
    findings.write_findings_json(path, items or _two_findings())
    return _ok(json.dumps({"verdict": "rejected", "reason": "not yet"}))


def _verified(sup, prompt=""):
    return _ok(json.dumps({"verdict": "verified", "reason": "fine"}))


def _seed(sup, tasks, gate="closed"):
    head = _git(sup.workspace, "rev-parse", "HEAD")
    doc = {
        "version": 1,
        "phases": [
            {"id": "one", "title": "Phase one", "reviewFocus": "f1",
             "gate": gate, "rounds": 0, "startHead": head, "records": []},
            {"id": "two", "title": "Phase two", "reviewFocus": "f2",
             "gate": "closed", "rounds": 0, "startHead": head,
             "records": []}],
        "tasks": [
            {"id": tid, "title": f"thing {tid}", "phase": phase,
             "status": status, "successCriteria": f"criterion {tid}",
             "dependsOn": []} for tid, phase, status in tasks]}
    sup.run.write_tasks(doc)


def _seeded_sup(tmp_path, hats, tasks=None, **cfg):
    _init_workspace(tmp_path / "workspace")
    RunDir(root=tmp_path / "run").update_status(state="starting")
    sup = _supervisor(tmp_path, hats, **cfg)
    sup._approach = 1
    _seed(sup, tasks or [("001", "one", "completed"), ("002", "two", "pending")])
    return sup


def _cure_ids(sup, phase=None):
    return [t["id"] for t in sup.run.read_tasks()["tasks"]
            if t.get("origin") == "review-cure"
            and (phase is None or t.get("phase") == phase)]


# -- (1) the cure itself -------------------------------------------------------

def _assert_cured_inside_phase(sup, counter):
    data = sup.run.read_tasks()
    cures = [t for t in data["tasks"] if t.get("origin") == "review-cure"]
    assert len(cures) == 2
    assert {t["phase"] for t in cures} == {"one"}
    assert all(t["status"] == "pending" for t in cures)
    lineage = state.read_cure_lineage(sup.run.root)["passes"]
    assert len(lineage) == 1
    entry = lineage[0]
    assert entry["planPhase"] == "one" and entry["kind"] == "granted"
    assert entry["approach"] == 1
    assert sorted(entry["findings"]) == sorted(t["id"] for t in cures)
    one, two = data["phases"]
    assert one["rounds"] == 1 and two["rounds"] == 0
    assert one["gate"] == "closed"
    (rejected,) = _events(sup, "phase_gate.rejected")
    assert rejected["planPhase"] == "one" and rejected["approach"] == 1
    assert rejected["blockingFindings"] == 2 and rejected["round"] == 1
    assert _events(sup, "phase_gate.passed") == []
    assert sup.run.read_status()[counter] == {"1": 1}
    # the phase is not parked, and not due while its cure tasks are open
    assert sup._phase_review_unresolved == set()
    assert sup._due_phase_for_review() is None
    assert not sup.run.findings_json_file.exists()
    assert not sup.run.verdict_gate_findings_json_file.exists()


def test_a_curable_phase_review_rejection_inserts_cure_tasks_in_that_phase(tmp_path):
    sup = _seeded_sup(tmp_path, {"review": [lambda s, p="": _rejecting(s)]})
    asyncio.run(sup._run_phase_review("one"))
    _assert_cured_inside_phase(sup, "curePasses")
    assert _events(sup, "phase_gate.rejected")[0]["judge"] == "review"


def test_a_curable_phase_peer_rejection_inserts_cure_tasks_in_that_phase(tmp_path):
    sup = _seeded_sup(
        tmp_path, {"review": [_verified],
                   "verdict_gate": [lambda s, p="": _rejecting(s, gate=True)]},
        peer_verdict_gate=True)
    asyncio.run(sup._run_phase_review("one"))
    _assert_cured_inside_phase(sup, "gateCurePasses")
    assert _events(sup, "phase_gate.rejected")[0]["judge"] == "verdict_gate"


# -- (2) the confirmation re-review ----------------------------------------------

class _Judges:
    """Phase reviews reject on the listed rounds (per phase), else verify;
    every review prompt is kept with the phase it was scoped to."""

    def __init__(self, reject=()):
        self.reject = set(reject)  # {(phase, nth review of that phase)}
        self.seen: list[dict] = []
        self.count: dict[str, int] = {}

    def review(self, sup, prompt=""):
        scope = None
        for pid in ("one", "two", "three"):
            if f"Phase id: `{pid}`" in prompt:
                scope = pid
        n = self.count[scope] = self.count.get(scope, 0) + 1
        self.seen.append({"phase": scope, "n": n, "prompt": prompt})
        if (scope, n) in self.reject:
            return _rejecting(sup)
        return _verified(sup)

    def prompts(self, phase, n=None):
        return [s["prompt"] for s in self.seen
                if s["phase"] == phase and (n is None or s["n"] == n)]


def _run(tmp_path, judges, plan=_plan, **cfg):
    _init_workspace(tmp_path / "workspace")
    RunDir(root=tmp_path / "run").update_status(state="starting")
    sup = _supervisor(tmp_path, {"planning": [plan], "worker": [_worker],
                                 "review": [judges.review]}, **cfg)
    final = asyncio.run(asyncio.wait_for(sup._run_job_core(), 90))
    return sup, final


def test_after_a_phase_cure_the_phase_is_due_again_with_a_phase_scoped_confirmation_review(tmp_path):
    judges = _Judges(reject={("one", 1)})
    sup, final = _run(tmp_path, judges)
    assert final == "succeeded", sup.run.read_status()
    first, second = judges.prompts("one")
    assert "## Confirmation review" not in first
    # the cure tasks ran (worker iterations) before the phase was re-reviewed
    cure_ids = _cure_ids(sup, "one")
    assert len(cure_ids) == 2
    assert sup.runner.phases_seen[:3] == ["planning", "worker", "review"]
    assert sup.runner.phases_seen[3:5] == ["worker", "worker"]
    assert sup.runner.phases_seen[5] == "review"
    # the confirmation section is built from this phase's latest granted pass
    assert "## Confirmation review" in second
    assert "scoped to plan phase `one`" in second
    lineage = state.read_cure_lineage(sup.run.root)["passes"]
    (granted,) = [p for p in lineage if p.get("planPhase") == "one"]
    for cid in granted["findings"]:
        assert f"`{cid}`" in second
    # the diff range starts at the HEAD the pass was taken at
    assert f"`{granted['preCureHead']}.." in second
    # the second review passed, so the gate opened and phase two followed
    assert [e["round"] for e in _events(sup, "phase_gate.due")] == [1, 2]
    assert [e["round"] for e in _events(sup, "phase_gate.passed")] == [2]


def test_after_a_petition_remedy_the_phase_is_due_again_with_a_confirmation_review_naming_the_remedy(tmp_path):
    obligation = "Task 001: thing 001"
    seen: list[str] = []

    def review(sup, prompt=""):
        seen.append(prompt)
        if len(seen) == 1:
            return _ok(json.dumps({
                "verdict": "verified", "reason": "fine",
                "petition_adjudications": [{
                    "obligation": obligation, "adjudication": "REFUTED",
                    "remedy": "Fix the fixture. Done when: it renders."}]}))
        return _verified(sup)

    sup = _seeded_sup(
        tmp_path, {"review": [review]},
        [("001", "one", "failed"), ("002", "two", "pending")])
    assert petitions.file_worker_petition(
        sup.workspace, obligation, "environmental", "measured in the sandbox",
        run=sup.run) == []
    asyncio.run(sup._run_phase_review("one"))
    data = sup.run.read_tasks()
    (remedy,) = [t for t in data["tasks"] if t.get("remedyFor") == obligation]
    assert remedy["phase"] == "one" and data["phases"][0]["gate"] == "closed"
    assert "## Confirmation review" not in seen[0]
    lineage = state.read_cure_lineage(sup.run.root)["passes"]
    assert [(p["kind"], p["planPhase"]) for p in lineage] == [("remedy", "one")]
    # the remedy becomes terminal: the phase is due again ...
    assert sup._due_phase_for_review() is None
    for t in data["tasks"]:
        if t["id"] == remedy["id"]:
            t["status"] = "completed"
    sup.run.write_tasks(data)
    assert sup._due_phase_for_review() == "one"
    asyncio.run(sup._run_phase_review("one"))
    # ... and its review is a confirmation review that names the remedy task
    assert "## Confirmation review" in seen[1]
    assert f"`{remedy['id']}`" in seen[1]
    assert "Petition remedy task(s)" in seen[1]
    assert sup.run.read_tasks()["phases"][0]["gate"] == "passed"
    assert [e["round"] for e in _events(sup, "phase_gate.passed")] == [2]


# -- (3) lineage is scoped by phase ------------------------------------------------

def test_a_phase_cure_followed_by_the_first_final_review_builds_no_confirmation_section(tmp_path):
    judges = _Judges(reject={("one", 1)})
    sup, final = _run(tmp_path, judges)
    assert final == "succeeded", sup.run.read_status()
    assert any(p.get("planPhase") == "one"
               for p in state.read_cure_lineage(sup.run.root)["passes"])
    final_prompts = [s for s in judges.seen if s["phase"] is None]
    assert len(final_prompts) == 1
    assert "## Confirmation review" not in final_prompts[0]["prompt"]
    assert sup._is_confirmation_review() is False
    assert sup._latest_granted_cure_pass(1) is None
    assert sup._latest_granted_cure_pass(1, "one")["planPhase"] == "one"


def _plan3(sup, prompt=""):
    atomic_write_json(sup.run.root / "plan-proposal.json", {
        "version": 1,
        "phases": [{"id": "one", "title": "P1", "reviewFocus": "f1"},
                   {"id": "two", "title": "P2", "reviewFocus": "f2"},
                   {"id": "three", "title": "P3", "reviewFocus": "f3"}],
        "tasks": [
            {"id": "001", "title": "t1", "phase": "one",
             "successCriteria": "criterion one"},
            {"id": "002", "title": "t2", "phase": "two",
             "successCriteria": "criterion two"},
            {"id": "003", "title": "t3", "phase": "three",
             "successCriteria": "criterion three"}]})
    return _ok(json.dumps({"proposal_path": "plan-proposal.json",
                           "summary": "planned"}))


def test_a_phase_two_re_review_never_cites_phase_one_cure_tasks(tmp_path):
    judges = _Judges(reject={("one", 1), ("two", 1)})
    sup, final = _run(tmp_path, judges, plan=_plan3)
    assert final == "succeeded", sup.run.read_status()
    one_cures = set(_cure_ids(sup, "one"))
    two_cures = set(_cure_ids(sup, "two"))
    assert len(one_cures) == len(two_cures) == 2 and not one_cures & two_cures
    # phase two's FIRST review is not a confirmation review: phase one's
    # granted pass is not its own
    (first,) = judges.prompts("two", 1)
    assert "## Confirmation review" not in first
    (again,) = judges.prompts("two", 2)
    assert "## Confirmation review" in again
    for cid in two_cures:
        assert f"`{cid}`" in again
    for cid in one_cures:
        assert f"`{cid}`" not in again
    # phase one's own re-review cites only phase one's tasks
    (p1_again,) = judges.prompts("one", 2)
    for cid in one_cures:
        assert f"`{cid}`" in p1_again
    for cid in two_cures:
        assert f"`{cid}`" not in p1_again
    passes = state.read_cure_lineage(sup.run.root)["passes"]
    assert [p["planPhase"] for p in passes] == ["one", "two"]
    assert sup._latest_granted_cure_pass(1, "two")["findings"].keys() == two_cures
    assert sup._latest_granted_cure_pass(1, "one")["findings"].keys() == one_cures


# -- (4) one cure allowance ---------------------------------------------------------

def test_a_phase_gate_cure_round_reduces_the_cure_rounds_left_to_the_final_review(tmp_path):
    def reject(sup, prompt=""):
        # a confirmation review keeps a finding blocking only when it
        # reopens a cure task the previous pass listed
        prior = _cure_ids(sup, "one")
        if not prior:
            return _rejecting(sup)
        return _rejecting(sup, [
            _finding("requirement A", reopens=prior[0]),
            _finding("requirement B", reopens=prior[1])])

    sup = _seeded_sup(tmp_path, {"review": [reject]},
                      cure_pass_ceiling=2, operator_input_needed_pause=True)
    assert sup._cure_rounds_allowed(1) - sup._cure_rounds_used(1) == 2
    asyncio.run(sup._run_phase_review("one"))
    assert sup._cure_rounds_allowed(1) - sup._cure_rounds_used(1) == 1
    assert not sup._cure_pass_ceiling_spent(1)
    # a second phase round spends the allowance the final review would use
    data = sup.run.read_tasks()
    for t in data["tasks"]:
        if t.get("origin") == "review-cure":
            t["status"] = "completed"
    sup.run.write_tasks(data)
    asyncio.run(sup._run_phase_review("one"))
    assert sup._cure_rounds_allowed(1) - sup._cure_rounds_used(1) == 0
    assert sup._cure_pass_ceiling_spent(1)
    # so the FINAL review's curable rejection now parks instead of curing
    findings.write_findings_json(sup.run.findings_json_file, _two_findings())
    outcome = sup._run_rejection_ladder(
        "review", 1, _two_findings(), signature=None)
    assert outcome == ("advance", "paused", "operator_input_needed")
    assert sup.run.read_status()["inputQuestion"]["trigger"] == "cure_rounds_spent"
    # and a third phase round is no longer taken as a cure either
    assert sup._phase_cure_candidate(
        "review", {"verdict": "rejected"}) is None
