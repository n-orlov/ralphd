"""#161: a resume must not re-enter `plan_review` over an approved (or
already worked) plan, and the gate must never spend a status-wiping
revision once work is recorded.

Observed 2026-09-07 on `selfdev-v0191-a-gate-is-not-a-wall`: `ralphctl
resume` took `_step_planning(skip_planning=True)` -> `plan_review`; a
`ralphctl steer --now` interrupted that iteration; the interrupted reply was
read as undecidable, a revision was spent, and the planning iteration that
followed would have ingested a fresh proposal with every task `pending` --
over 8 validated tasks. The operator stopped the container first.

Four facts pinned here, against the scripted-agent fixtures
`tests/test_peer_plan_review_gate.py` already uses:

1. resume over a plan with recorded work (or with the `planReviewApproved`
   marker) starts at `worker`, never `plan_review`, and every status
   survives;
2. resume over an all-pending plan with no marker still runs `plan_review`
   (a run killed between planning and its review is reviewed on resume);
3. an interrupted `plan_review` iteration is re-asked in place, not read
   as an objection -- no revision spent, no planning re-run;
4. an objection over a plan with recorded work is refused
   (`plan_review_revision_refused_work_recorded` -> worker) and
   `tasks.json` is untouched.

Plus the disk marker itself: approval and bound-reached both write
`planReviewApproved[str(approach)] = True`.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from ralphd.engine import transitions as tr
from ralphd.engine.runner import IterationResult
from ralphd.engine.state import RunDir, atomic_write_json, read_events
from tests.conftest import make_supervisor

TASK_1 = "001"
TASK_2 = "002"


# ---------------------------------------------------------------------------
# scripted-agent fixtures (same shape as tests/test_peer_plan_review_gate.py)
# ---------------------------------------------------------------------------


def _ok(text: str) -> IterationResult:
    r = IterationResult(exit_code=0)
    r.final_text = text
    r.duration_s = 5.0
    r.usage = {"input": 10, "output": 5, "totalTokens": 15}
    return r


def _interrupted() -> IterationResult:
    r = IterationResult(exit_code=-2)
    r.interrupted = True
    r.final_text = ""
    r.duration_s = 1.0
    r.usage = {"input": 1, "output": 0, "totalTokens": 1}
    return r


def _plan(*task_ids: str):
    ids = task_ids or (TASK_1,)

    def handler(sup, prompt=""):
        atomic_write_json(sup.run.root / "plan-proposal.json", {
            "version": 1,
            "tasks": [{"id": t, "title": f"t{t}", "successCriteria": "c"}
                      for t in ids],
        })
        return _ok(json.dumps(
            {"proposal_path": "plan-proposal.json", "summary": "planned"}))
    return handler


def _plan_review_approves(sup, prompt=""):
    return _ok(json.dumps({"verdict": "approved"}))


def _plan_review_interrupted(sup, prompt=""):
    return _interrupted()


def _plan_review_objects(reason: str):
    def handler(sup, prompt=""):
        return _ok(json.dumps({"verdict": "objected", "reason": reason}))
    return handler


def _worker_completes(task_id: str):
    def handler(sup, prompt=""):
        return _ok(json.dumps({"verdict": "complete", "task_id": task_id}))
    return handler


def _worker_completes_assigned(sup, prompt=""):
    """Complete the task the harness hands out (first `in-progress`, else
    first `pending`, in list order) -- the same order `_step_worker`
    assigns by, so the reported id never trips the assigned-vs-reported
    mismatch guard."""
    tasks = sup.run.read_tasks()["tasks"]
    for wanted in ("in-progress", "pending"):
        for t in tasks:
            if t.get("status") == wanted:
                return _ok(json.dumps({"verdict": "complete",
                                       "task_id": t["id"]}))
    raise AssertionError("worker asked to run with nothing assignable")


def _review_verifies(sup, prompt=""):
    return _ok(json.dumps({"verdict": "verified"}))


class _ScriptedAgent:
    def __init__(self, handlers: dict):
        self.handlers = handlers
        self.calls: dict[str, int] = {}
        self.phases_seen: list[str] = []
        self.running = False

    def interrupt(self) -> bool:
        return self.running

    async def run(self, prompt, transcript, **kw) -> IterationResult:
        phase = self.sup.run.read_status().get("phase")
        self.phases_seen.append(phase)
        n = self.calls.get(phase, 0)
        self.calls[phase] = n + 1
        steps = self.handlers[phase]   # KeyError == "this phase must not run"
        self.running = True
        try:
            return steps[min(n, len(steps) - 1)](self.sup, prompt)
        finally:
            self.running = False


def _supervisor(root: Path, handlers: dict, **cfg):
    return make_supervisor(
        root, runner_factory=lambda sup: _ScriptedAgent(handlers),
        **{"run_id": "issue161-plan-review-resume", "iterations": 15,
           "max_approaches": 1, "vigilant": False, "on_complete": "exit",
           "infra_retry_backoff_s": [0.0], "infra_retry_backoff_max_s": 0.0,
           "infra_outage_budget_s": 1000.0, "peer_plan_review": True, **cfg})


def _run_fresh(root: Path, handlers: dict, **cfg):
    RunDir(root=root).update_status(state="starting")
    sup = _supervisor(root, handlers, **cfg)
    return sup, asyncio.run(sup._run_job_core())


def _seed_resumable_run_dir(root: Path, *, task1_status: str,
                            approved_marker: bool) -> RunDir:
    """A run dir a prior episode left behind: a two-task plan, one
    completed iteration on disk (so `_resuming_existing_run_dir()` is
    true), status `aborted` at `worker`."""
    run = RunDir(root=root)
    root.mkdir(parents=True, exist_ok=True)
    run.write_tasks({
        "version": 1, "goal": "g", "scope": {}, "repositories": [],
        "tasks": [
            {"id": TASK_1, "title": "t001", "successCriteria": "c",
             "status": task1_status},
            {"id": TASK_2, "title": "t002", "successCriteria": "c",
             "status": "pending"},
        ],
        "discovered": {},
    })
    it = root / "iterations" / "0001"
    it.mkdir(parents=True)
    atomic_write_json(it / "meta.json", {
        "number": 1, "phase": "planning", "approach": 1,
        "startedAt": "2026-09-07T07:00:00Z", "endedAt": "2026-09-07T07:05:00Z",
        "exitCode": 0})
    status = {"state": "aborted", "approach": 1, "phase": "worker"}
    if approved_marker:
        status["planReviewApproved"] = {"1": True}
    run.update_status(**status)
    return run


def _statuses(run: RunDir) -> dict[str, str]:
    return {t["id"]: t["status"] for t in run.read_tasks()["tasks"]}


# ---------------------------------------------------------------------------
# the table
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("vigilant", [True, False])
def test_the_two_161_edges_exist_only_with_the_gate_on(vigilant):
    on = tr.TransitionExecutor(vigilant, peer_plan_review=True)
    assert on.advance("planning", "worker",
                      "planning_skipped_plan_already_reviewed") == "worker"
    assert on.advance("plan_review", "worker",
                      "plan_review_revision_refused_work_recorded") == "worker"
    off_guards = {e.guard for e in tr.successors("planning", vigilant,
                                                  peer_plan_review=False)}
    assert "planning_skipped_plan_already_reviewed" not in off_guards
    assert tr.successors("plan_review", vigilant, peer_plan_review=False) == ()


# ---------------------------------------------------------------------------
# 1. resume over a worked / approved plan goes straight to worker
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("task1_status,approved_marker", [
    ("validated", True),    # the ordinary post-fix case
    ("validated", False),   # a run dir from before the marker existed
    ("in-progress", False),
    ("pending", True),      # approved, killed before any work: still no re-ask
])
def test_resume_never_reenters_plan_review_over_a_settled_plan(
        tmp_path, task1_status, approved_marker):
    root = tmp_path / "run"
    run = _seed_resumable_run_dir(root, task1_status=task1_status,
                                  approved_marker=approved_marker)
    before = _statuses(run)
    handlers = {"worker": [_worker_completes_assigned],
                "review": [_review_verifies]}
    # NO "planning" and NO "plan_review" handler: reaching either phase
    # raises KeyError inside the scripted agent and fails the test.
    sup = _supervisor(root, handlers)
    final_state = asyncio.run(sup._run_job_core())

    agent = sup.runner
    assert "plan_review" not in agent.phases_seen
    assert "planning" not in agent.phases_seen
    assert agent.phases_seen[0] == "worker"
    assert final_state == "succeeded", sup.run.read_status()

    after = _statuses(run)
    if task1_status == "validated":
        assert after[TASK_1] == "validated", (before, after)
    assert after[TASK_2] != "pending"
    logs = [e for e in read_events(sup.run.root)
            if e.get("type") == "log" and "#161" in (e.get("message") or "")]
    assert logs, "the skip is logged so an operator can see why the peer was not re-asked"


# ---------------------------------------------------------------------------
# 2. resume over an all-pending, never-reviewed plan still runs the gate
# ---------------------------------------------------------------------------


def test_resume_over_an_unreviewed_all_pending_plan_still_runs_plan_review(
        tmp_path):
    root = tmp_path / "run"
    _seed_resumable_run_dir(root, task1_status="pending", approved_marker=False)
    handlers = {"plan_review": [_plan_review_approves],
                "worker": [_worker_completes_assigned],
                "review": [_review_verifies]}
    sup = _supervisor(root, handlers)
    final_state = asyncio.run(sup._run_job_core())

    agent = sup.runner
    assert agent.phases_seen[0] == "plan_review", agent.phases_seen
    assert "planning" not in agent.phases_seen
    assert final_state == "succeeded", sup.run.read_status()
    assert sup.run.read_status().get("planReviewApproved") == {"1": True}


# ---------------------------------------------------------------------------
# 3. an interrupted plan_review is re-asked, not read as an objection
# ---------------------------------------------------------------------------


def test_an_interrupted_plan_review_is_reasked_in_place_and_spends_no_revision(
        tmp_path):
    root = tmp_path / "run"
    sup, final_state = _run_fresh(
        root,
        {"planning": [_plan(TASK_1)],
         "plan_review": [_plan_review_interrupted, _plan_review_approves],
         "worker": [_worker_completes(TASK_1)],
         "review": [_review_verifies]})

    agent = sup.runner
    assert agent.phases_seen == [
        "planning", "plan_review", "plan_review", "worker", "review"]
    assert agent.calls["planning"] == 1, "no revision re-ran planning"
    assert final_state == "succeeded", sup.run.read_status()
    status = sup.run.read_status()
    assert status.get("planReviewRevisions") is None
    assert status.get("planReviewApproved") == {"1": True}


def test_a_twice_interrupted_plan_review_over_a_fresh_plan_falls_to_the_old_policy(
        tmp_path):
    """Two interruptions in a row on a plan with NO recorded work: the
    existing undecidable policy still applies (spend a revision, re-plan)
    -- #161 changes nothing there, since nothing can be wiped."""
    root = tmp_path / "run"
    sup, final_state = _run_fresh(
        root,
        {"planning": [_plan(TASK_1), _plan(TASK_1)],
         "plan_review": [_plan_review_interrupted, _plan_review_interrupted,
                         _plan_review_approves],
         "worker": [_worker_completes(TASK_1)],
         "review": [_review_verifies]})

    agent = sup.runner
    assert agent.calls["planning"] == 2
    assert agent.calls["plan_review"] == 3
    assert final_state == "succeeded", sup.run.read_status()
    assert sup.run.read_status().get("planReviewRevisions") == {"1": 1}


# ---------------------------------------------------------------------------
# 4. an objection over a worked plan is refused; tasks.json is untouched
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("reply", ["objected", "interrupted-twice", "garbage"])
def test_a_revision_over_a_worked_plan_is_refused_and_wipes_nothing(
        tmp_path, reply):
    root = tmp_path / "run"
    run = _seed_resumable_run_dir(root, task1_status="validated",
                                  approved_marker=False)
    before = _statuses(run)
    if reply == "objected":
        peer = [_plan_review_objects("re-cut the plan")]
    elif reply == "interrupted-twice":
        peer = [_plan_review_interrupted, _plan_review_interrupted]
    else:
        peer = [lambda sup, prompt="": _ok("not json at all")]
    sup = _supervisor(root, {"plan_review": peer})
    sup._approach = 1
    outcome = asyncio.run(sup._step_plan_review())

    assert outcome == ("advance", "worker",
                       "plan_review_revision_refused_work_recorded"), outcome
    assert _statuses(run) == before, "tasks.json must not change"
    status = sup.run.read_status()
    assert status.get("planReviewRevisions") is None, "no revision was spent"
    assert status.get("planReviewApproved") == {"1": True}
    warnings = [e for e in read_events(sup.run.root)
                if e.get("type") == "log" and "refused" in (e.get("message") or "")]
    assert warnings, "the refusal is logged with the peer's objection"
    assert sup._pending_plan_review_objection is None


# ---------------------------------------------------------------------------
# the marker: bound-reached also records that the plan proceeded
# ---------------------------------------------------------------------------


def test_bound_reached_records_the_plan_as_settled(tmp_path):
    root = tmp_path / "run"
    sup, final_state = _run_fresh(
        root,
        {"planning": [_plan(TASK_1), _plan(TASK_1)],
         "plan_review": [_plan_review_objects("first"),
                         _plan_review_objects("second, bound spent")],
         "worker": [_worker_completes(TASK_1)],
         "review": [_review_verifies]},
        peer_plan_review_max_revisions=1)
    assert final_state == "succeeded", sup.run.read_status()
    status = sup.run.read_status()
    assert status.get("planReviewRevisions") == {"1": 1}
    assert status.get("planReviewApproved") == {"1": True}
