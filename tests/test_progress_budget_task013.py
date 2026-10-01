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
  three-strike witness rule exactly as task 010 built it, unchanged (this
  task's own letter, true at the time; PRD v0.29 #279 later layers a
  separate re-poll-backoff ladder over the SAME repeated-identical-claim
  shape and intercepts before three such claims ever reach that counter
  -- see `test_an_uncorroborated_no_verdict_now_backs_off_instead_of_
  tripping_the_three_strike_guard`'s own docstring, task 014's sweep cure);
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
from tests.conftest import _patch_sleep_for_this_test

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
    # PRD v0.29 #279 cure (task 014's sweep): this module's scenarios script
    # TWO consecutive uncorroborated no_verdict passes on `TASK_ID`
    # deliberately (the three-strike guard under test), which is also
    # EXACTLY `_apply_engine_repoll_backoff`'s own trigger
    # (`tests/test_repoll_backoff.py`'s own `_worker_sup` docstring: "a bare
    # construction leaves [asyncio.sleep] unpatched and a 600s+ wait really
    # elapses"). This module predates task 009/#279 and built `sup` directly
    # rather than through `tests.conftest.make_supervisor` (which patches
    # `asyncio.sleep` to be instant by default), so the SECOND consecutive
    # re-poll's real 600s backoff wait (`_sleep_until_earliest_deferred`)
    # elapsed for real the moment #279 landed, exceeding pytest-timeout's
    # 120s and crashing the xdist worker it ran under (measured: task 014's
    # own sweep, fast tier, `node down: Not properly terminated` on this
    # module's `test_an_uncorroborated_no_verdict_still_trips_the_three_
    # strike_guard`). Patching it here, the same way `make_supervisor`
    # already does, keeps the backoff wait inert without touching the
    # three-strike assertions this module exists to pin.
    _patch_sleep_for_this_test()
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
# 2: an uncorroborated no_verdict now backs off instead of following
#    today's three-strike rule (PRD v0.29 #279 cure, task 014's sweep)
# ---------------------------------------------------------------------------


def test_an_uncorroborated_no_verdict_now_backs_off_instead_of_tripping_the_three_strike_guard(tmp_path):
    """Three consecutive iterations, each claiming `commit` evidence but
    touching NOTHING in the workspace -- `_apply_worker_progress_report`
    reads this as uncorroborated every time (task 012's own pinned
    behaviour, `test_commit_evidence_with_no_tree_delta_is_not_corroborated`).

    This test used to prove requirement A.3's letter ("keeps today's
    three-strike witness rule") held unchanged. PRD v0.29 #279 (task 014's
    sweep cure) supersedes it for exactly this shape: #279's own
    `_apply_engine_repoll_backoff` trigger is defined over the SAME signal
    (`verdict == "no_verdict"`, `corroborated: false`, `deltaSize: 0`, two
    consecutive times on one task), and it intercepts BEFORE a third such
    pass can ever reach the three-strike guard's own counter -- pass 2 is
    the first ordinary witness (old counter -> 1) and simultaneously the
    first of what becomes two consecutive re-polls, so pass 3 backs the
    task off for 600s instead of becoming the guard's third witness
    (requirement A.3's own "a re-poll that triggers a backoff neither
    increments nor resets the approach's stagnation counter"). task 014's
    own sweep crashed an xdist worker on this exact node before this cure
    -- this module's `_worker_sup` built `sup` directly rather than through
    `tests.conftest.make_supervisor`, leaving the backoff's real
    `asyncio.sleep(600)` unpatched (`_patch_sleep_for_this_test()` now
    closes that, matching `tests/test_repoll_backoff.py`'s own documented
    reasoning for the same gap).

    What is left to pin, and what this rewrite asserts instead: the claims
    are still recorded and still uncorroborated every time (task 012's
    letter, unaffected by #279); the OLD three-strike message never
    appears (superseded, not merely absent by accident); #279's own
    backoff fires twice (`consecutiveCount` 2 then 3, since the third
    scripted call is ALSO a re-poll on the already-once-backed-off task);
    and the task ends up deferred (`pending`, a `re-poll backoff`
    `deferredReason`)."""
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
    assert "3 iterations with no task progress" not in _log_messages(run)
    backoffs = _events_of_type(run, "task.repoll_backoff")
    assert [b["consecutiveCount"] for b in backoffs] == [2, 3], backoffs
    task = next(t for t in run.read_tasks()["tasks"] if t["id"] == TASK_ID)
    assert task["status"] == "pending", task
    assert task.get("deferredReason", "").startswith("re-poll backoff"), task


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
