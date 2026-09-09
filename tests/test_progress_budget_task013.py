"""Task 013 (PRD requirement A.3, #156): "the budget" -- the third of the
five #156 stages, replacing the absorption-forced verify (v0.19 B.2) with a
per-task progress budget.

Task 011 accepted the worker contract's `progress` claim shape; task 012
made the claim MEAN something by corroborating it against a workspace
fingerprint. This task is the first to make `corroborated` change what the
engine actually DOES with a `no_verdict`:

- a corroborated `no_verdict` on task T spends T's own budget
  (`estimatedIterations + 2`, default 1 when the field is absent --
  `plan_ingestion` defaulting a missing estimate is requirement D.1, not
  yet built, so every task in this run reads the same default budget of 3
  today) and is exempted from the approach's own three-strike stagnation
  counter (`LoopSupervisor._progress_budget_for`/`_record_progress_budget_
  usage`, `PROGRESS_BUDGET_STATUS_KEY`);
- an uncorroborated `no_verdict` gets none of that -- it keeps today's
  three-strike witness rule exactly as task 010 built it, unchanged;
- `taskAbsorptionStreak`'s forced verify (v0.19 B.2) no longer fires for a
  task that is under its own budget AND whose current iteration's claim
  was corroborated -- `_record_task_absorption_streak`'s new
  `budget_exempt` parameter.

All three driven through the REAL production call site
(`LoopSupervisor._step_worker`, a stubbed `sup.runner` only), the same
style `tests/test_progress_claim_corroboration.py` and
`tests/test_worker_hat.py`'s folded `test_task_absorption_handoff_task011.py`
section already use.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path

from ralphd.engine.config import JobConfig
from ralphd.engine.loop import LoopSupervisor
from ralphd.engine.runner import IterationResult
from ralphd.engine.state import RunDir, atomic_write_json

TASK_ID = "013t"


def _ok(text: str) -> IterationResult:
    r = IterationResult(exit_code=0)
    r.final_text = text
    r.duration_s = 1.0
    r.usage = {"input": 1, "output": 1, "totalTokens": 2}
    return r


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def _init_git_workspace(workspace: Path) -> None:
    workspace.mkdir(parents=True, exist_ok=True)
    _git(workspace, "init", "-q")
    _git(workspace, "config", "user.email", "t@example.com")
    _git(workspace, "config", "user.name", "t")
    (workspace / "seed.txt").write_text("seed")
    _git(workspace, "add", "-A")
    _git(workspace, "commit", "-q", "-m", "seed")


class _ScriptedWorker:
    """Same shape every sibling module in this family uses: every call
    invokes `step(sup)`, which does whatever workspace side effect this
    test needs and returns the payload text for this call."""

    def __init__(self, step):
        self.step = step
        self.calls = 0

    def interrupt(self) -> bool:
        return False

    async def run(self, prompt, transcript, **kw) -> IterationResult:
        text = self.step(self)
        self.calls += 1
        return _ok(text)


def _worker_sup(tmp_path: Path, name: str, **cfg_kw) -> tuple[LoopSupervisor, RunDir]:
    # Task 012's own `_worker_sup` docstring reasoning applies unchanged:
    # the run dir and the workspace are DIFFERENT directories, so the
    # fingerprint this task's budget bookkeeping reads never sees the run
    # dir's own tasks.json/events.jsonl bookkeeping writes as tree
    # "progress" a worker never made.
    root = tmp_path / name / "run"
    workspace = tmp_path / name / "workspace"
    root.mkdir(parents=True)
    workspace.mkdir(parents=True)
    run = RunDir(root=root)
    run.update_status(state="running")
    atomic_write_json(run.tasks_file, {"version": 1, "tasks": [
        {"id": TASK_ID, "title": "the task this worker iteration was assigned",
         "status": "in-progress", "successCriteria": "n/a"},
    ]})
    kw = {"max_approaches": 3, "vigilant": False,
          "on_complete": "exit", "infra_retry_backoff_s": [0.0],
          "infra_retry_backoff_max_s": 0.0, "infra_outage_budget_s": 1000.0,
          **cfg_kw}
    cfg = JobConfig(run_id=f"task013-{name}", **kw)
    sup = LoopSupervisor(cfg, run, workspace)
    sup._approach = 1
    # Task 008's own baseline write (normally taken during planning) must
    # land BEFORE `_step_worker()` ever runs, or the FIRST worker iteration's
    # own `before != after` tasks.json diff (the baseline hash landing for
    # the first time) reads as a stagnation witness all by itself, silently
    # consuming one of the iterations these tests budget for stagnation
    # bookkeeping (`tests/test_worker_hat.py`'s own `_ensure_criteria_
    # baseline()` call, same reasoning, right before its own `_step_worker`).
    sup._ensure_criteria_baseline()
    return sup, run


def _events_of_type(run: RunDir, type_: str) -> list[dict]:
    events = [json.loads(x) for x in
              (run.root / "events.jsonl").read_text().splitlines() if x.strip()]
    return [e for e in events if e.get("type") == type_]


def _log_messages(run: RunDir) -> str:
    return "\n".join(e.get("message", "") for e in _events_of_type(run, "log"))


# ---------------------------------------------------------------------------
# 1: a corroborated no_verdict spends the task's own budget, never the
#    approach's stagnation counter
# ---------------------------------------------------------------------------


def test_a_corroborated_no_verdict_spends_the_tasks_own_budget_not_the_approach_counter(
        tmp_path):
    """Five consecutive worker iterations, each touching a NEW, never-
    committed file (so `commits_gained`, witness (b), never fires -- this
    isolates the new progress-claim witness from the pre-existing one) and
    claiming `file` evidence for it -- corroborated every time, since the
    workspace fingerprint really does change. `estimatedIterations` is
    absent from the seeded task, so the default budget is `1 + 2 == 3`;
    five corroborated claims still runs `used` past that budget (this
    task's own job stops at recording it honestly -- forcing a split once
    it is spent is task 016's), and the approach-wide three-strike counter
    -- which an unwitnessed run of 5 iterations like this would otherwise
    trip on the 3rd -- never fires at all, across all 5."""
    n = 5

    def step(worker):
        (worker._sup.workspace / f"evidence{worker.calls}.txt").write_text("x")
        payload = {"verdict": "no_verdict", "progress": {
            "summary": f"touched file {worker.calls}",
            "evidence": [{"kind": "file", "ref": f"evidence{worker.calls}.txt"}]}}
        return json.dumps(payload)

    sup, run = _worker_sup(tmp_path, "budget-spend", iterations=n)
    worker = _ScriptedWorker(step)
    worker._sup = sup
    sup.runner = worker  # type: ignore[assignment]

    asyncio.run(sup._step_worker())

    claims = _events_of_type(run, "progress_claim")
    assert len(claims) == n
    assert all(c["corroborated"] for c in claims), claims
    assert all(c["deltaSize"] >= 1 for c in claims), claims

    budget_record = sup.run.read_status().get("progressBudget")
    assert budget_record == {"taskId": TASK_ID, "used": n, "budget": 3}, budget_record

    assert "3 iterations with no task progress" not in _log_messages(run)


# ---------------------------------------------------------------------------
# 2: an uncorroborated no_verdict still follows today's three-strike rule
# ---------------------------------------------------------------------------


def test_an_uncorroborated_no_verdict_still_trips_the_three_strike_guard(tmp_path):
    """Three consecutive iterations, each claiming `commit` evidence but
    touching NOTHING in the workspace -- `_apply_worker_progress_report`
    reads this as uncorroborated every time (task 012's own pinned
    behaviour, `test_commit_evidence_with_no_tree_delta_is_not_corroborated`).
    Requirement A.3's letter is explicit that this case "keeps today's
    three-strike witness rule" -- so the pre-existing guard must still fire
    exactly as it did before this task touched anything."""
    payload = json.dumps({"verdict": "no_verdict", "progress": {
        "summary": "checked, nothing to touch yet",
        "evidence": [{"kind": "commit", "ref": "deadbeef"}]}})

    def step(worker):
        return payload

    sup, run = _worker_sup(tmp_path, "uncorroborated-stagnates",
                           iterations=3, max_approaches=1)
    worker = _ScriptedWorker(step)
    worker._sup = sup
    sup.runner = worker  # type: ignore[assignment]

    asyncio.run(sup._step_worker())

    claims = _events_of_type(run, "progress_claim")
    assert len(claims) == 3
    assert all(c["corroborated"] is False for c in claims), claims
    assert "3 iterations with no task progress" in _log_messages(run)


# ---------------------------------------------------------------------------
# 3: taskAbsorptionStreak's forced verify no longer fires for a task under
#    budget with corroborated progress
# ---------------------------------------------------------------------------


def test_absorption_streak_does_not_force_verify_under_budget_with_corroborated_progress(
        tmp_path):
    """The v0.19 B.2 hand-off (`test_three_consecutive_commit_only_
    iterations_hand_the_task_to_verify` in `tests/test_worker_hat.py`) used
    to force ANY 3 consecutive commit-only iterations on one task to
    `awaiting-validation` -- commits alone were enough, with no notion of
    a budget at all. Here the SAME three consecutive commit-only
    iterations each ALSO carry a corroborated `commit` progress claim, and
    the seeded task's default budget (`estimatedIterations` absent, so
    `1 + 2 == 3`) covers all three (`used` reaches exactly 3, never past
    it) -- so `budget_exempt` holds for every one of them, and the
    hand-off this task retires must never fire: the task stays
    `in-progress`, and `taskAbsorptionStreak` on `status.json` never
    accumulates past `{}`."""
    def step(worker):
        n = worker.calls
        (worker._sup.workspace / f"f{n}.txt").write_text("x")
        _git(worker._sup.workspace, "add", "-A")
        _git(worker._sup.workspace, "commit", "-q", "-m", f"progress {n}")
        payload = {"verdict": "no_verdict", "progress": {
            "summary": f"committed progress {n}",
            "evidence": [{"kind": "commit", "ref": f"progress {n}"}]}}
        return json.dumps(payload)

    sup, run = _worker_sup(tmp_path, "absorption-exempt",
                           iterations=3, vigilant=True,
                           task_absorption_max_iterations=3)
    _init_git_workspace(sup.workspace)
    worker = _ScriptedWorker(step)
    worker._sup = sup
    sup.runner = worker  # type: ignore[assignment]

    asyncio.run(sup._step_worker())

    claims = _events_of_type(run, "progress_claim")
    assert len(claims) == 3
    assert all(c["corroborated"] for c in claims), claims

    budget_record = sup.run.read_status().get("progressBudget")
    assert budget_record == {"taskId": TASK_ID, "used": 3, "budget": 3}, budget_record

    task = next(t for t in sup.run.read_tasks()["tasks"] if t["id"] == TASK_ID)
    assert task["status"] == "in-progress", task
    assert "task 011" not in (task.get("notes") or "")
    assert "absorbed 3 consecutive worker iterations" not in _log_messages(run)
    assert sup.run.read_status().get("taskAbsorptionStreak") in (None, {})
