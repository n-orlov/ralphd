"""v0.36 requirement F.1 / F.2: stops that are not about findings heal
themselves or fail -- stagnation and dependency-graph faults.

F.1  A stalled approach, on ANY approach (silent or progressed, and the
     exhausted-forced-split-child case), is archived and replanned on
     approach + 1 over the same workspace tree. With no approach left the
     run holds `exhausted` with `exhaustedBudget: approaches`. 0.35 parked
     all of these as `paused` / `stagnation_last_approach` and waited for an
     operator.

F.2  A `dependsOn` cycle or an edge to an id that is not in the plan
     replans the SAME approach, uncharged (as `empty_plan` does), with the
     offending edges named in the planning prompt. A second graph fault on
     the same approach ends the run `failed` with
     `ending: unschedulable_task_graph`. 0.35 parked both as `paused` /
     `unschedulable_dependency_cycle` / `unresolvable_dependency_edge`.

Every node drives the real engine with fake hats (a per-phase script) and
reads the outcome off status.json / events.jsonl; none sleeps.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path

import pytest

from ralphd.engine import transitions
from ralphd.engine.runner import IterationResult
from ralphd.engine.state import RunDir, atomic_write_json
from tests.conftest import make_supervisor, scripted_supervisor

STAGNATION_LOG = "3 iterations with no task progress"
PARKED_STATES = {"paused", "awaiting_input"}
RETIRED_REASONS = ("stagnation", "unschedulable_dependency_cycle",
                   "unresolvable_dependency_edge")


# -- scaffolding ------------------------------------------------------------

def _ok(text: str) -> IterationResult:
    r = IterationResult(exit_code=0)
    r.final_text = text
    r.duration_s = 5.0
    r.usage = {"input": 10, "output": 5, "totalTokens": 15}
    return r


def _events(root: Path) -> list[dict]:
    p = root / "events.jsonl"
    if not p.exists():
        return []
    return [json.loads(x) for x in p.read_text().splitlines() if x.strip()]


def _states_seen(root: Path) -> list[str]:
    return [ev.get("state") for ev in _events(root) if ev.get("type") == "state"]


def _assert_never_parked(root: Path) -> None:
    states = set(_states_seen(root))
    assert not states & PARKED_STATES, states
    status = json.loads((root / "status.json").read_text())
    assert status.get("pauseReason") is None, status
    reasons = [ev.get("reason") for ev in _events(root)
               if ev.get("type") == "operator_escalation"]
    assert not [r for r in reasons if r in RETIRED_REASONS], reasons


def _task(tid: str = "001", **over) -> dict:
    task = {"id": tid, "title": f"task {tid}", "status": "pending",
            "successCriteria": f"task {tid} works"}
    task.update(over)
    return task


def _propose(sup, tasks: list[dict]) -> IterationResult:
    keep = ("id", "title", "successCriteria", "dependsOn")
    atomic_write_json(sup.run.root / "plan-proposal.json", {
        "version": 1,
        "tasks": [{k: v for k, v in t.items() if k in keep} for t in tasks]})
    return _ok("planned\n" + json.dumps(
        {"proposal_path": "plan-proposal.json", "summary": "planned"}))


_supervisor = scripted_supervisor(
    run_id="v036-self-heal", iterations=40, vigilant=False,
    on_complete="exit", infra_outage_budget_s=1000.0)


# ===========================================================================
# F.1  Stagnation self-heals on every approach.
# ===========================================================================


class _QuietWorker:
    """A worker that writes one file on its first call (the workspace tree
    the next approach must keep) and afterwards reports prose only, naming
    the wall differently on every approach so the repeated-blocker guard is
    not what ends the run."""

    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, sup, prompt=""):
        self.calls += 1
        if self.calls == 1:
            (sup.workspace / "approach-one-work.txt").write_text("kept")
        return _ok(f"wall number {sup._approach}: I cannot get past it")


def test_stagnation_archives_and_replans_then_holds_exhausted_with_no_approach_left(
        tmp_path):
    """Approach 1 of 2 stalls silently: it is archived and the run plans
    approach 2 over the same workspace tree. Approach 2 stalls too, with no
    approach left: the run holds `exhausted` on `approaches` (0.35 parked on
    `stagnation_last_approach` and waited for an operator)."""
    root = tmp_path / "run"
    sup = _supervisor(
        root, {"planning": [lambda s, p: _propose(s, [_task()])],
               "worker": [_QuietWorker()]},
        max_approaches=2, workspace="ws")
    RunDir(root=root).update_status(state="starting")

    final = asyncio.run(asyncio.wait_for(sup._run_job_core(), 120))

    status = json.loads((root / "status.json").read_text())
    assert final == "exhausted", status
    assert status["state"] == "exhausted"
    assert status["exhaustedBudget"] == "approaches"
    assert status["approach"] == 2
    assert status.get("verdict") is None
    # approach 1 was archived and approach 2 was planned afresh; the last
    # approach is archived too before the run holds.
    assert sorted(d.name for d in (root / "approaches").iterdir()) == ["01", "02"]
    assert sup.runner.calls["planning"] == 2
    # the workspace tree the first approach left is kept for the second.
    assert (sup.workspace / "approach-one-work.txt").read_text() == "kept"
    _assert_never_parked(root)
    # "stagnation" is no longer a pause reason the run ever records.
    logs = [ev for ev in _events(root) if ev.get("type") == "log"
            and STAGNATION_LOG in (ev.get("message") or "")]
    assert len(logs) == 2


def test_stagnation_on_the_last_approach_with_the_same_wall_holds_exhausted_not_failed(
        tmp_path):
    """The same-structural-wall guard (#61) ends a run `failed` only while
    a further approach would have been spent on that wall; on the last
    approach the run holds `exhausted`, which an operator can top up."""
    root = tmp_path / "run"
    sup = _supervisor(
        root, {"planning": [lambda s, p: _propose(s, [_task()])],
               "worker": [lambda s, p: _ok("blocked by the very same wall")]},
        max_approaches=2)
    RunDir(root=root).update_status(state="starting")

    final = asyncio.run(asyncio.wait_for(sup._run_job_core(), 120))

    status = json.loads((root / "status.json").read_text())
    assert final == "exhausted", status
    assert status["exhaustedBudget"] == "approaches"
    signatures = status.get("blockerSignatures") or {}
    assert signatures.get("1") == signatures.get("2")
    _assert_never_parked(root)


def _init_git(workspace: Path) -> None:
    workspace.mkdir(parents=True, exist_ok=True)
    for args in (("init", "-q"), ("config", "user.email", "t@example.com"),
                 ("config", "user.name", "t")):
        subprocess.run(["git", *args], cwd=workspace, check=True,
                       capture_output=True)
    (workspace / "seed.txt").write_text("seed")
    subprocess.run(["git", "add", "-A"], cwd=workspace, check=True,
                   capture_output=True)
    subprocess.run(["git", "commit", "-q", "-m", "seed"], cwd=workspace,
                   check=True, capture_output=True)


def test_a_progressed_approach_that_stalls_replans_like_a_silent_one(tmp_path):
    """0.35 parked an approach that gained a workspace commit before it
    stalled. Now it is archived and replanned; the stagnation event records
    that it progressed, and the commits stay in the tree."""
    workspace = tmp_path / "ws"
    _init_git(workspace)
    calls = {"n": 0}

    def worker(sup, prompt=""):
        calls["n"] += 1
        if calls["n"] == 1:
            (workspace / "work.txt").write_text("real work")
            subprocess.run(["git", "add", "-A"], cwd=workspace, check=True,
                           capture_output=True)
            subprocess.run(["git", "commit", "-q", "-m", "work"], cwd=workspace,
                           check=True, capture_output=True)
        return _ok(f"iteration {calls['n']}: nothing further to report")

    sup = make_supervisor(
        tmp_path / "run", run_id="v036-progressed", workspace=workspace,
        iterations=30, max_approaches=3, vigilant=False, on_complete="exit",
        tasks=[_task()], status={"state": "running"},
        runner_factory=lambda s: None)
    sup.runner = type("R", (), {
        "interrupt": lambda self: False,
        "run": lambda self, prompt, transcript, **kw: _coro(worker(sup, prompt)),
    })()
    sup._approach = 1

    outcome = asyncio.run(sup._step_worker())

    assert outcome == ("advance", "replan", "stagnation_exhausted")
    root = sup.run.root
    assert (root / "approaches" / "01").is_dir()
    assert sup._pause.is_set() is True
    _assert_never_parked(root)
    stagnation = [ev for ev in _events(root) if ev.get("type") == "log"
                  and STAGNATION_LOG in (ev.get("message") or "")]
    assert [ev["approachProgressed"] for ev in stagnation] == [True]
    assert sup._step_replan() == ("advance", "planning", "budget_and_approach_left")
    assert sup._approach == 2
    assert (workspace / "work.txt").read_text() == "real work"


async def _coro(value):
    return value


def _spent_split_child_supervisor(tmp_path: Path, **cfg):
    child = {"id": "c1", "title": "split child", "status": "pending",
             "successCriteria": "the child's slice works",
             "origin": "worker", "splitFrom": "p1"}
    parent = {"id": "p1", "title": "split parent", "status": "superseded",
              "successCriteria": "the whole thing works"}

    class _MustNotRun:
        calls = 0

        def interrupt(self):
            return False

        async def run(self, *a, **kw):
            type(self).calls += 1
            return _ok("a forced-split iteration was dispatched")

    return make_supervisor(
        tmp_path / "run", run_id="v036-spent-child", cfg_defaults=False,
        prd=True, vigilant=False, iterations=10, contract_retry_max=1,
        tasks=[parent, child],
        status={"state": "running",
                "progressBudget": {"taskId": "c1", "used": 3, "budget": 3}},
        runner=_MustNotRun(), **{"max_approaches": 3, **cfg})


def test_an_exhausted_forced_split_child_archives_and_replans(tmp_path):
    """0.35 parked the exhausted-split-child case as `stagnation_last_
    approach` on a non-last approach; it now takes the same archive-and-
    replan edge as any other stall, and never dispatches the doomed second
    split."""
    sup = _spent_split_child_supervisor(tmp_path)
    sup._approach = 1

    outcome = asyncio.run(sup._step_worker())

    assert outcome == ("advance", "replan", "stagnation_exhausted")
    assert type(sup.runner).calls == 0
    root = sup.run.root
    assert (root / "approaches" / "01").is_dir()
    _assert_never_parked(root)
    ids = {t["id"]: t for t in sup.run.read_tasks()["tasks"]}
    assert set(ids) == {"p1", "c1"}            # no grandchild
    assert sup._step_replan() == ("advance", "planning", "budget_and_approach_left")
    assert sup._approach == 2


def test_an_exhausted_forced_split_child_on_the_last_approach_holds_exhausted(
        tmp_path):
    sup = _spent_split_child_supervisor(tmp_path, max_approaches=1)
    sup._approach = 1

    assert asyncio.run(sup._step_worker()) == (
        "advance", "replan", "stagnation_exhausted")
    assert sup._step_replan() == ("tail",)
    assert sup._finish_tail_unverified() == "exhausted"
    status = sup.run.read_status()
    assert status["state"] == "exhausted"
    assert status["exhaustedBudget"] == "approaches"
    _assert_never_parked(sup.run.root)


def test_the_stagnation_pause_edges_are_gone_from_the_transition_table():
    for vigilant in (True, False):
        by_guard = {e.guard: e.target for e in
                    transitions.successors("worker", vigilant)}
        assert by_guard["stagnation_exhausted"] == "replan"
        for retired in ("stagnation_last_approach",
                        "unschedulable_dependency_cycle",
                        "unresolvable_dependency_edge"):
            assert retired not in by_guard
        assert by_guard["unschedulable_task_graph"] in {"replan", "finishing"}


# ===========================================================================
# F.2  A graph fault replans the same approach, uncharged; a second one on
#      that approach ends the run `failed`.
# ===========================================================================

GRAPH_CASES = {
    # kind: (broken dependsOn on 001, expected edge text)
    "cycle": (["001"], "001 -> 001"),
    "edge": (["ghost"], "001 -> ghost"),
}


class _GraphScript:
    """Fake planning and worker hats for a graph-fault run. Planning always
    proposes a valid two-task plan (001, 002 depending on 001). The first
    worker iteration finishes 001 and then re-breaks the graph (a writer
    the planner's lint cannot see: a post-ingestion edit), so the same
    approach faults a SECOND time."""

    def __init__(self, broken: list[str], *, rebreak: bool) -> None:
        self.broken = broken
        self.rebreak = rebreak
        self.planning_prompts: list[str] = []

    def plan(self, sup, prompt=""):
        self.planning_prompts.append(prompt)
        return _propose(sup, [_task("001"), _task("002", dependsOn=["001"])])

    def work(self, sup, prompt=""):
        doc = sup.run.read_tasks()
        by_id = {t["id"]: t for t in doc["tasks"]}
        by_id["001"]["status"] = "completed"
        if self.rebreak:
            by_id["002"]["dependsOn"] = (
                ["002"] if self.broken == ["001"] else ["ghost"])
        sup.run.write_tasks(doc)
        return _ok("finished 001")


def _graph_run(tmp_path, kind: str, *, rebreak: bool):
    broken, _edge = GRAPH_CASES[kind]
    script = _GraphScript(broken, rebreak=rebreak)
    root = tmp_path / "run"
    sup = _supervisor(
        root, {"planning": [script.plan], "worker": [script.work]},
        max_approaches=3, tasks=[
            _task("001", dependsOn=broken),
            _task("002", dependsOn=["001"])],
        status={"state": "running"})
    sup.iterations_used = 1            # a resumed run: skip the first planning
    sup._approach = 1
    return sup, script, root


@pytest.mark.parametrize("kind", sorted(GRAPH_CASES))
def test_a_first_graph_fault_replans_the_same_approach_and_names_the_edges(
        tmp_path, kind):
    sup, script, root = _graph_run(tmp_path, kind, rebreak=False)
    edge_text = GRAPH_CASES[kind][1]

    outcome = asyncio.run(sup._step_worker())

    assert outcome == ("advance", "replan", "unschedulable_task_graph")
    assert sup._approach == 1
    assert sup._pause.is_set() is True
    status = sup.run.read_status()
    assert status.get("health") == "degraded"
    assert status.get("graphFaultApproaches") == [1]
    assert not (root / "approaches" / "01").exists()   # same approach: no archive
    assert "worker" not in (sup.runner.calls)       # nothing was dispatched
    _assert_never_parked(root)

    # replan keeps the approach number and goes back to planning
    assert sup._step_replan() == (
        "advance", "planning", "graph_fault_excluded_from_charge")
    assert sup._approach == 1
    # the planning prompt names the offending edge, once
    asyncio.run(sup._step_planning(skip_planning=False))
    assert len(script.planning_prompts) == 1
    prompt = script.planning_prompts[0]
    assert "cannot be scheduled" in prompt
    assert edge_text in prompt
    assert "002" in prompt                           # the blocked dependent
    assert sup._pending_graph_fault_note is None     # consumed
    # and the new plan is clean
    doc = sup.run.read_tasks()
    assert [t["id"] for t in doc["tasks"]] == ["001", "002"]
    assert all(t["status"] == "pending" for t in doc["tasks"])


@pytest.mark.parametrize("kind", sorted(GRAPH_CASES))
def test_a_second_graph_fault_on_the_same_approach_ends_failed(tmp_path, kind):
    sup, script, root = _graph_run(tmp_path, kind, rebreak=True)
    edge_text = GRAPH_CASES[kind][1]

    final = asyncio.run(asyncio.wait_for(sup._run_job_core(), 120))

    status = json.loads((root / "status.json").read_text())
    assert final == "failed", status
    assert status["state"] == "failed"
    assert status["ending"] == "unschedulable_task_graph"
    assert status.get("verdict") is None
    assert status["approach"] == 1                   # never charged an approach
    assert status["graphFaultApproaches"] == [1]
    assert "twice" in (status.get("reason") or "")
    assert "002" in (status.get("reason") or "")
    assert sup.runner.calls["planning"] == 1         # exactly one replan
    assert len(script.planning_prompts) == 1
    assert edge_text in script.planning_prompts[0]
    assert not (root / "approaches" / "01").exists()
    _assert_never_parked(root)
    faults = [ev for ev in _events(root) if ev.get("type") == "graph_fault"]
    assert [ev["repeated"] for ev in faults] == [False, True]
    assert all(ev["approach"] == 1 for ev in faults)


def test_a_graph_fault_message_never_points_at_the_retired_unpause_route(tmp_path):
    sup, script, root = _graph_run(tmp_path, "cycle", rebreak=False)
    asyncio.run(sup._step_worker())
    note = sup._pending_graph_fault_note
    assert note and "001 -> 001" in note
    assert "unpause" not in note and "ralphctl resume" not in note


def test_the_failed_graph_fault_run_resumes_at_the_worker_after_a_repair(tmp_path):
    """`failed` with `ending: unschedulable_task_graph` is resumable through
    the existing resume point: a fresh engine over the run dir re-enters the
    approach (planning skipped) and the stale `ending` is cleared when the
    new episode starts."""
    sup, script, root = _graph_run(tmp_path, "cycle", rebreak=True)
    asyncio.run(asyncio.wait_for(sup._run_job_core(), 120))
    assert json.loads((root / "status.json").read_text())["ending"] == \
        "unschedulable_task_graph"

    # the operator repairs the graph, then resumes with a fresh process
    doc = sup.run.read_tasks()
    for t in doc["tasks"]:
        t["dependsOn"] = [] if t["id"] == "002" else t.get("dependsOn", [])
        t["status"] = "pending"
    sup.run.write_tasks(doc)
    fresh = _supervisor(
        root, {"planning": [script.plan], "worker": [script.work]},
        max_approaches=3, status=None)
    approach, skip_planning, step = fresh._resume_point()
    assert approach == 1
    assert skip_planning is True
    assert step == "planning"
