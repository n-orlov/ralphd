"""Task 015 (PRD requirement A.5, #156): "pinned at the in-process tier" --
the fifth and last of the five #156 stage tasks. Tasks 011-014 built the
claim, the corroboration, the budget and the surface; this task pins the
FOUR end-to-end behaviours requirement A.5's letter names, plus the one
incident that motivated the whole requirement, all driven through the same
production call site (`LoopSupervisor._step_worker`, a stubbed `sup.runner`
only) every sibling module in this family already uses:

1. a corroborated claim does not stagnate -- proven mid-streak: it resets
   the approach's own `stagnant` counter even after two witness-less
   iterations had already advanced it, so the SAME five-iteration shape
   that would trip the three-strike guard without the reset never does
   with it (`test_a_corroborated_claim_resets_the_stagnation_counter_mid_
   streak`);
2. an uncorroborated claim stagnates -- three consecutive `commit`-kind
   claims with no tree delta trip the guard exactly as they did before
   task 012 existed (`test_an_uncorroborated_claim_still_trips_the_three_
   strike_guard`);
3. two consecutive external-only claims stagnate -- the SECOND (and every
   claim after it, in a row) is uncorroborated, so four consecutive
   external-only claims on one task trip the guard on the fourth, never
   exempted twice (`test_only_the_first_of_several_consecutive_external_
   only_claims_is_corroborated_the_rest_stagnate`);
4. the budget counts per task and resets on task change -- one task's
   budget usage does not leak into the next task's own record the moment
   the scheduler moves on (`test_the_progress_budget_resets_the_moment_
   the_assigned_task_changes`);
5. the v0.19 #156 approach-1 incident, replayed against a stub engine --
   `docs/roadmap.md`'s own account of that approach: three consecutive
   `no_verdict` iterations pushed thirteen commits toward a target they
   never reached, and the OLD engine failed the approach's stagnation
   guard over it anyway. Replayed here with the same shape (three
   `no_verdict`s, thirteen commits split 5/4/4 across them, target never
   reached) against THIS engine, with task 013's budget/exemption already
   landed, and the task survives: still `in-progress`, never forced to
   verify, no stagnation-guard message
   (`test_the_v019_approach_1_incident_replayed_the_task_survives`).
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

TASK_A = "015a"
TASK_B = "015b"


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
    invokes `step(sup, call_index)`, which does whatever workspace side
    effect this scenario needs and returns the payload text for that
    call."""

    def __init__(self, sup: LoopSupervisor, step):
        self.sup = sup
        self.step = step
        self.calls = 0

    def interrupt(self) -> bool:
        return False

    async def run(self, prompt, transcript, **kw) -> IterationResult:
        text = self.step(self.sup, self.calls)
        self.calls += 1
        return _ok(text)


def _worker_sup(tmp_path: Path, name: str, tasks: list[dict],
                **cfg_kw) -> tuple[LoopSupervisor, RunDir]:
    # Same reasoning tasks 012/013's own `_worker_sup` already documented:
    # the run dir and the workspace are DIFFERENT directories, so the
    # fingerprint this family reads never sees the run dir's own
    # tasks.json/events.jsonl bookkeeping writes as tree "progress" no
    # worker made.
    root = tmp_path / name / "run"
    workspace = tmp_path / name / "workspace"
    root.mkdir(parents=True)
    workspace.mkdir(parents=True)
    run = RunDir(root=root)
    run.update_status(state="running")
    atomic_write_json(run.tasks_file, {"version": 1, "tasks": tasks})
    kw = {"max_approaches": 3, "vigilant": False,
          "on_complete": "exit", "infra_retry_backoff_s": [0.0],
          "infra_retry_backoff_max_s": 0.0, "infra_outage_budget_s": 1000.0,
          **cfg_kw}
    cfg = JobConfig(run_id=f"task015-{name}", **kw)
    sup = LoopSupervisor(cfg, run, workspace)
    sup._approach = 1
    # Task 013's own `_worker_sup` reasoning: the baseline write must land
    # BEFORE `_step_worker()` ever runs, or the first iteration's own
    # tasks.json diff (the baseline hash landing for the first time) reads
    # as a stagnation witness all by itself, silently consuming one of the
    # iterations these tests budget for stagnation bookkeeping.
    sup._ensure_criteria_baseline()
    return sup, run


def _events_of_type(run: RunDir, type_: str) -> list[dict]:
    events = [json.loads(x) for x in
              (run.root / "events.jsonl").read_text().splitlines() if x.strip()]
    return [e for e in events if e.get("type") == type_]


def _log_messages(run: RunDir) -> str:
    return "\n".join(e.get("message", "") for e in _events_of_type(run, "log"))


STAGNATION_MESSAGE = "3 iterations with no task progress"


# ---------------------------------------------------------------------------
# 1: a corroborated claim does not stagnate -- proven mid-streak, not just
#    "never reaches 3 because there were only a few iterations".
# ---------------------------------------------------------------------------


def test_a_corroborated_claim_resets_the_stagnation_counter_mid_streak(tmp_path):
    """Five iterations on one task, no other witness available (no real
    commits, no tasks.json edit, no tool activity): iterations 1 and 2
    claim `commit` evidence with NO tree delta (uncorroborated, advancing
    the counter to 2 -- one short of the guard); iteration 3 claims `file`
    evidence WITH a real tree delta (corroborated, and this is the fact
    under test: it must reset the counter to 0, not merely fail to add to
    it); iterations 4 and 5 repeat the uncorroborated pair. Without the
    reset, the running total across these five passes (2 before + 1 for
    the corroborated pass itself if it did not reset + 2 after) would trip
    the guard on the third or fifth pass; with it, it never does."""
    tasks = [{"id": TASK_A, "title": "t", "status": "in-progress",
              "successCriteria": "n/a"}]

    def step(sup, i):
        if i == 2:  # the third call (0-indexed) -- the corroborated one
            (sup.workspace / "midstreak.txt").write_text("real delta")
            evidence = {"kind": "file", "ref": "midstreak.txt"}
        else:
            evidence = {"kind": "commit", "ref": f"deadbeef{i}"}
        return json.dumps({"verdict": "no_verdict", "progress": {
            "summary": f"pass {i}", "evidence": [evidence]}})

    sup, run = _worker_sup(tmp_path, "corroborated-midstreak", tasks,
                            iterations=5)
    sup.runner = _ScriptedWorker(sup, step)  # type: ignore[assignment]

    asyncio.run(sup._step_worker())

    claims = _events_of_type(run, "progress_claim")
    assert len(claims) == 5
    assert [c["corroborated"] for c in claims] == [False, False, True, False, False]
    assert STAGNATION_MESSAGE not in _log_messages(run)
    task = next(t for t in sup.run.read_tasks()["tasks"] if t["id"] == TASK_A)
    assert task["status"] == "in-progress", task


# ---------------------------------------------------------------------------
# 2: an uncorroborated claim stagnates
# ---------------------------------------------------------------------------


def test_an_uncorroborated_claim_still_trips_the_three_strike_guard(tmp_path):
    """Three consecutive `file`-kind claims, none backed by a real tree
    delta -- uncorroborated every time (task 012's own pinned behaviour),
    so requirement A.5's letter ("an uncorroborated [claim] does") holds:
    the pre-existing three-strike guard still fires exactly as it did
    before this wave touched anything."""
    tasks = [{"id": TASK_A, "title": "t", "status": "in-progress",
              "successCriteria": "n/a"}]
    payload = json.dumps({"verdict": "no_verdict", "progress": {
        "summary": "claimed a file touch that never happened",
        "evidence": [{"kind": "file", "ref": "nope.txt"}]}})

    sup, run = _worker_sup(tmp_path, "uncorroborated-stagnates", tasks,
                            iterations=3, max_approaches=1)
    sup.runner = _ScriptedWorker(sup, lambda s, i: payload)  # type: ignore[assignment]

    asyncio.run(sup._step_worker())

    claims = _events_of_type(run, "progress_claim")
    assert len(claims) == 3
    assert all(c["corroborated"] is False for c in claims), claims
    assert STAGNATION_MESSAGE in _log_messages(run)


# ---------------------------------------------------------------------------
# 3: two consecutive external-only claims stagnate -- and so does every
#    external-only claim after the first, in the same streak.
# ---------------------------------------------------------------------------


def test_only_the_first_of_several_consecutive_external_only_claims_is_corroborated_the_rest_stagnate(
        tmp_path):
    """Four consecutive `external`-only claims on one task, no other
    witness: the first is honoured at face value (corroborated); the
    second is the letter's own case ("two consecutive external-only
    claims" -- the second does not get the grace the first did, so it
    stagnates); the third and fourth stagnate too, for the same reason,
    and by the fourth pass three consecutive stagnating passes (2, 3, 4)
    have accumulated and the guard fires -- proving the second claim's
    "stagnates" is not merely a flag on the event but a real contribution
    to the counter that eventually trips the guard."""
    tasks = [{"id": TASK_A, "title": "t", "status": "in-progress",
              "successCriteria": "n/a"}]
    payload = json.dumps({"verdict": "no_verdict", "progress": {
        "summary": "watched the dashboard, nothing to commit yet",
        "evidence": [{"kind": "external", "ref": "watched the pipeline run"}]}})

    sup, run = _worker_sup(tmp_path, "external-streak-stagnates", tasks,
                            iterations=4, max_approaches=1)
    sup.runner = _ScriptedWorker(sup, lambda s, i: payload)  # type: ignore[assignment]

    asyncio.run(sup._step_worker())

    claims = _events_of_type(run, "progress_claim")
    assert len(claims) == 4
    assert [c["corroborated"] for c in claims] == [True, False, False, False]
    assert STAGNATION_MESSAGE in _log_messages(run)


# ---------------------------------------------------------------------------
# 4: the budget counts per task and resets on task change
# ---------------------------------------------------------------------------


def test_the_progress_budget_resets_the_moment_the_assigned_task_changes(tmp_path):
    """Task A is `in-progress`, task B is `pending`. Two corroborated
    `file`-evidence passes spend task A's own budget up to 2/3; the third
    pass reports `verdict: "complete"` for A (no progress block, plain
    completion) -- the scheduler now assigns B; the fourth pass reports a
    corroborated `file`-evidence claim on B. `_record_progress_budget_
    usage` resets `used` to 1 the moment `assigned_id` no longer matches
    the persisted record's own `taskId` -- proven here by reading
    `status.json`'s `progressBudget` after the fourth pass and finding
    `{"taskId": TASK_B, "used": 1, ...}`, not `{"taskId": TASK_B, "used":
    3, ...}` (which is what a budget that failed to reset would show, by
    carrying A's 2 used passes plus B's own one forward)."""
    tasks = [
        {"id": TASK_A, "title": "t-a", "status": "in-progress",
         "successCriteria": "n/a"},
        {"id": TASK_B, "title": "t-b", "status": "pending",
         "successCriteria": "n/a"},
    ]

    def step(sup, i):
        if i in (0, 1):
            (sup.workspace / f"a{i}.txt").write_text("x")
            return json.dumps({"verdict": "no_verdict", "progress": {
                "summary": f"touched a{i}.txt",
                "evidence": [{"kind": "file", "ref": f"a{i}.txt"}]}})
        if i == 2:
            return json.dumps({"verdict": "complete", "task_id": TASK_A})
        (sup.workspace / "b0.txt").write_text("y")
        return json.dumps({"verdict": "no_verdict", "progress": {
            "summary": "touched b0.txt",
            "evidence": [{"kind": "file", "ref": "b0.txt"}]}})

    sup, run = _worker_sup(tmp_path, "budget-resets-on-task-change", tasks,
                            iterations=4)
    sup.runner = _ScriptedWorker(sup, step)  # type: ignore[assignment]

    asyncio.run(sup._step_worker())

    claims = _events_of_type(run, "progress_claim")
    assert [c["taskId"] for c in claims] == [TASK_A, TASK_A, TASK_B]
    assert all(c["corroborated"] for c in claims), claims

    final_tasks = {t["id"]: t for t in sup.run.read_tasks()["tasks"]}
    assert final_tasks[TASK_A]["status"] == "completed", final_tasks[TASK_A]
    assert final_tasks[TASK_B]["status"] == "in-progress", final_tasks[TASK_B]

    budget_record = sup.run.read_status().get("progressBudget")
    assert budget_record == {"taskId": TASK_B, "used": 1, "budget": 3}, budget_record


# ---------------------------------------------------------------------------
# 5: the v0.19 #156 approach-1 incident, replayed against a stub engine
# ---------------------------------------------------------------------------


def test_the_v019_approach_1_incident_replayed_the_task_survives(tmp_path):
    """`docs/roadmap.md`'s own account: v0.19 approach 1 "was failed by
    the stagnation guard ... after three `no_verdict` iterations that
    pushed thirteen commits toward requirement G.2's 120 s target
    (802.89 s -> 641.66 s) without reaching it." Replayed here with the
    same shape against a stub engine: three consecutive `no_verdict`
    iterations, thirteen real commits split 5/4/4 across them (each
    iteration's own commits are genuinely new tree content, so the
    fingerprint really moves and the `commit` evidence is corroborated
    every time), and the numeric target is never reached by any of the
    three -- the detail that made the OLD engine's `taskAbsorptionStreak`
    forced-verify (v0.19 B.2) fire on three consecutive commit-only
    passes regardless of whether real work was happening, and an
    unfinished forced verify count as a strike toward failing the task.

    Under THIS engine (task 013's budget-exemption already landed): the
    default budget (`estimatedIterations` absent, so `1 + 2 == 3`) exactly
    covers the three passes (`used` reaches exactly 3, never past it), so
    every one of them is `budget_exempt` and the forced-verify hand-off
    never fires; the approach's own three-strike counter never advances
    either, because a corroborated claim resets it every pass. The task
    survives: still `in-progress` at the end, never handed to verify,
    all thirteen commits present in the workspace's own git history."""
    tasks = [{"id": TASK_A, "title": "shave the suite toward the G.2 target",
              "status": "in-progress", "successCriteria": "n/a"}]
    commits_per_pass = [5, 4, 4]
    assert sum(commits_per_pass) == 13

    def step(sup, i):
        n = commits_per_pass[i]
        for k in range(n):
            fname = f"pass{i}_commit{k}.txt"
            (sup.workspace / fname).write_text(f"pass {i} commit {k}")
            _git(sup.workspace, "add", "-A")
            _git(sup.workspace, "commit", "-q", "-m", f"pass {i} commit {k}")
        return json.dumps({"verdict": "no_verdict", "progress": {
            "summary": f"pushed {n} commits toward the target, not there yet",
            "evidence": [{"kind": "commit", "ref": f"pass {i}, {n} commits"}]}})

    sup, run = _worker_sup(tmp_path, "v019-approach1-replay", tasks,
                            iterations=3, vigilant=True,
                            task_absorption_max_iterations=3)
    _init_git_workspace(sup.workspace)
    sup.runner = _ScriptedWorker(sup, step)  # type: ignore[assignment]

    asyncio.run(sup._step_worker())

    claims = _events_of_type(run, "progress_claim")
    assert len(claims) == 3
    assert all(c["corroborated"] for c in claims), claims

    budget_record = sup.run.read_status().get("progressBudget")
    assert budget_record == {"taskId": TASK_A, "used": 3, "budget": 3}, budget_record

    task = next(t for t in sup.run.read_tasks()["tasks"] if t["id"] == TASK_A)
    assert task["status"] == "in-progress", task
    assert "task 011" not in (task.get("notes") or "")  # the v0.19 B.2 hand-off marker
    assert STAGNATION_MESSAGE not in _log_messages(run)
    assert "absorbed 3 consecutive worker iterations" not in _log_messages(run)
    assert sup.run.read_status().get("taskAbsorptionStreak") in (None, {})

    log = subprocess.run(["git", "log", "--oneline"], cwd=sup.workspace,
                         check=True, capture_output=True, text=True).stdout
    # the seed commit `_init_git_workspace` makes, plus all thirteen.
    assert len(log.strip().splitlines()) == 1 + 13
