"""PRD v0.37 requirement B.5 / C.1 / C.3 / C.4 (verified pass) and decision 5:
a due phase before the last is reviewed by the `review` hat in phase-scoped
mode before any further worker iteration; the last phase has no gate.

The loop is driven with fake hats (`ScriptedRunner`), as the other loop
modules do; nothing sleeps."""

from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path

from ralphd.engine.runner import IterationResult
from ralphd.engine.state import RunDir, atomic_write_json

from tests.conftest import scripted_supervisor

_supervisor = scripted_supervisor(
    run_id="phase-review-dispatch", run_root="run", workspace="workspace",
    iterations=30, max_approaches=1, vigilant=False, on_complete="exit",
    infra_outage_budget_s=1000.0)


def _ok(text: str) -> IterationResult:
    r = IterationResult(exit_code=0)
    r.final_text = text
    r.duration_s = 5.0
    r.usage = {"input": 10, "output": 5, "totalTokens": 15}
    return r


def _git(ws: Path, *args) -> str:
    return subprocess.run(
        ["git", "-C", str(ws), "-c", "user.name=t", "-c", "user.email=t@e.x",
         *args], capture_output=True, text=True, check=True).stdout.strip()


def _init_workspace(ws: Path) -> str:
    ws.mkdir(parents=True, exist_ok=True)
    _git(ws, "init", "-q")
    (ws / "a.txt").write_text("one\n")
    _git(ws, "add", "a.txt")
    _git(ws, "commit", "-q", "-m", "first")
    return _git(ws, "rev-parse", "HEAD")


def _plan(sup, prompt=""):
    atomic_write_json(sup.run.root / "plan-proposal.json", {
        "version": 1,
        "phases": [{"id": "one", "title": "Phase one", "reviewFocus": "f1"},
                   {"id": "two", "title": "Phase two", "reviewFocus": "f2"}],
        "tasks": [
            {"id": "001", "title": "t1", "phase": "one",
             "successCriteria": "criterion one"},
            {"id": "002", "title": "t2", "phase": "two",
             "successCriteria": "criterion two"}]})
    return _ok(json.dumps({"proposal_path": "plan-proposal.json",
                           "summary": "planned"}))


def _worker(sup, prompt=""):
    """Completes whichever task the harness claimed, committing a file."""
    claimed = [t for t in sup.run.read_tasks()["tasks"]
               if t["status"] == "in-progress"]
    tid = claimed[0]["id"]
    (sup.workspace / f"{tid}.txt").write_text("work\n")
    _git(sup.workspace, "add", f"{tid}.txt")
    _git(sup.workspace, "commit", "-q", "-m", f"task {tid}")
    return _ok(json.dumps({"verdict": "complete", "task_id": tid}))


class _Hats:
    """Review fake: one answer for a phase review, one for the final one;
    records the gate and the iteration meta it saw while it ran."""

    def __init__(self, phase_verdict="verified"):
        self.phase_verdict = phase_verdict
        self.seen: list[dict] = []

    def review(self, sup, prompt=""):
        phase_scoped = "## Phase review" in prompt
        data = sup.run.read_tasks()
        n = sup.iterations_used
        meta = json.loads((sup.run.iteration_dir(n) / "meta.json").read_text())
        order = [t["id"] for t in data["tasks"] if t["status"] == "completed"]
        self.seen.append({
            "phase_scoped": phase_scoped, "meta": meta,
            "gates": [p["gate"] for p in data["phases"]],
            "completed": order})
        if phase_scoped and self.phase_verdict != "verified":
            return _ok(json.dumps({"verdict": "rejected", "reason": "no"}))
        return _ok(json.dumps({"verdict": "verified", "reason": "fine"}))


def _run(tmp_path, hats: _Hats, worker=_worker, **cfg):
    root = tmp_path / "run"
    ws = tmp_path / "workspace"
    head0 = _init_workspace(ws)
    RunDir(root=root).update_status(state="starting")
    sup = _supervisor(
        tmp_path, {"planning": [_plan], "worker": [worker],
                   "review": [hats.review]}, **cfg)
    final = asyncio.run(asyncio.wait_for(sup._run_job_core(), 60))
    return sup, final, head0


def _events(sup, kind=None):
    out = [json.loads(l) for l in
           (sup.run.root / "events.jsonl").read_text().splitlines()
           if l.strip()]
    return [e for e in out if kind is None or e.get("type") == kind
            or e.get("event") == kind]


def test_a_due_phase_before_the_last_is_reviewed_before_any_further_worker_iteration(tmp_path):
    hats = _Hats()
    sup, final, _head0 = _run(tmp_path, hats)
    assert final == "succeeded", sup.run.read_status()
    # the dispatch order: plan, worker (phase one), the PHASE review, then
    # the worker of phase two, then the final review.
    assert sup.runner.phases_seen == [
        "planning", "worker", "review", "worker", "review"]
    phase_review, final_review = hats.seen
    assert phase_review["phase_scoped"] and not final_review["phase_scoped"]
    # the gate reads `reviewing` during the dispatch; phase two's task had
    # not been touched when it ran.
    assert phase_review["gates"] == ["reviewing", "closed"]
    assert phase_review["completed"] == ["001"]
    # the iteration meta records the phase and the scope.
    assert phase_review["meta"]["planPhase"] == "one"
    assert phase_review["meta"]["scope"] == "phase"
    assert "planPhase" not in final_review["meta"]
    assert "scope" not in final_review["meta"]
    due = _events(sup, "phase_gate.due")
    assert [e["planPhase"] for e in due] == ["one"]


def test_last_phase_is_never_gate_dispatched(tmp_path):
    """Two-phase plan, phase one `passed`, both phase-two tasks terminal:
    the due check never dispatches phase two (decision 5)."""
    hats = _Hats()
    root = tmp_path / "run"
    ws = tmp_path / "workspace"
    head0 = _init_workspace(ws)
    RunDir(root=root).update_status(state="starting")
    def plan_complete(sup, prompt=""):
        return _ok(json.dumps({"verdict": "plan_complete"}))

    sup = _supervisor(tmp_path, {"planning": [_plan],
                                 "worker": [plan_complete],
                                 "review": [hats.review]})
    sup.run.write_tasks({
        "version": 1,
        "phases": [
            {"id": "one", "title": "P1", "reviewFocus": "", "gate": "passed",
             "rounds": 0, "startHead": head0, "records": []},
            {"id": "two", "title": "P2", "reviewFocus": "", "gate": "closed",
             "rounds": 0, "startHead": head0, "records": []}],
        "tasks": [
            {"id": "001", "title": "a", "phase": "one",
             "status": "completed", "successCriteria": "c"},
            {"id": "002", "title": "b", "phase": "two",
             "status": "completed", "successCriteria": "c"},
            {"id": "003", "title": "c", "phase": "two",
             "status": "completed", "successCriteria": "c"}]})
    data = sup.run.read_tasks()
    from ralphd.engine import state
    assert state.due_phase_id(data) == "two"      # due by B.4 ...
    assert sup._due_phase_for_review() is None    # ... but never dispatched
    sup._approach = 1
    asyncio.run(asyncio.wait_for(sup._step_worker(), 30))
    after = sup.run.read_tasks()
    assert [p["gate"] for p in after["phases"]] == ["passed", "closed"]
    for itdir in sorted(sup.run.root.glob("iterations/*/meta.json")):
        meta = json.loads(itdir.read_text())
        assert meta.get("scope") != "phase"
        assert meta.get("planPhase") != "two"
    names = {e.get("type") or e.get("event") for e in _events(sup)}
    assert "phase_gate.due" not in names and "phase_gate.passed" not in names
    assert not list((sup.run.artifacts_dir / "phase-reviews").glob("two-*")) \
        if (sup.run.artifacts_dir / "phase-reviews").exists() else True
    assert all(h["meta"].get("planPhase") != "two" for h in hats.seen)


def test_a_verified_phase_review_with_the_peer_gate_off_writes_records_and_opens_the_next_phase(tmp_path):
    hats = _Hats()
    sup, final, head0 = _run(tmp_path, hats, peer_verdict_gate=False)
    assert final == "succeeded", sup.run.read_status()
    data = sup.run.read_tasks()
    one, two = data["phases"]
    rec = sup.run.artifacts_dir / "phase-reviews"
    assert one["records"] == ["artifacts/phase-reviews/one-r1-review.json",
                              "artifacts/phase-reviews/one-r1-review.md"]
    record = json.loads((rec / "one-r1-review.json").read_text())
    assert record["verdict"] == "verified"
    assert record["reason"] == "fine"
    assert record["findings"] == []
    assert record["startHead"] == head0
    head_after_001 = _git(sup.workspace, "rev-list", "--max-count=2", "HEAD"
                          ).splitlines()[1]
    assert record["headReviewed"] == head_after_001
    assert isinstance(record["iteration"], int) and record["iteration"] >= 1
    assert (rec / "one-r1-review.md").read_text().strip()
    assert one["gate"] == "passed"
    # the next phase's startHead is the HEAD at the pass, not the planning HEAD
    assert two["startHead"] == head_after_001 != head0
    passed = _events(sup, "phase_gate.passed")
    assert [e["planPhase"] for e in passed] == ["one"]
    # and the next phase's task was then scheduled (it completed).
    assert {t["id"]: t["status"] for t in data["tasks"]}["002"] == "completed"
    assert two["records"] == []
    # the last phase has no gate record or event.
    assert not list(rec.glob("two-*"))
