"""Task 016 (PRD requirement F.3, #146): a deadline that lands mid-review
ends the run `verdict: "unreviewed"`, `reason: "deadline during review"` --
never an ordinary rejection -- for BOTH review paths that can be cut short by
the engine:

  * the ordinary self-loop (`_run_review_iteration` retrying in place while
    budget/wall clock remain, exhausting into `_review_unjudged_at_budget_end`
    -- the same v0.9 iteration 183 shape `tests/test_review_interrupted_not_
    rejection.py` already pins, now checked for the NEW verdict/reason this
    task adds on top of that fix), and
  * the off-budget grace review (`_maybe_grace_review`, granted once every
    task is already complete and the ordinary budget is spent) itself being
    interrupted by the engine, with no budget left to ask again either.

Neither path may log `review rejected approach N`, archive the approach, or
advance `status.json`'s `approach` counter -- review never actually rendered
a judgement this approach, so there is nothing to have rejected. A resume
with a fresh iteration/wall-clock window must land straight back on `review`
for the SAME approach (`LoopSupervisor._unreviewed_awaiting_resume`,
`_resume_point`'s new sixth case) -- never a throwaway `worker` pass and
never a fresh approach via `replan`.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from ralphd.engine.config import JobConfig
from ralphd.engine.loop import LoopSupervisor
from ralphd.engine.runner import IterationResult
from ralphd.engine.state import RunDir, atomic_write_json, read_events
from tests.conftest import make_supervisor


def _ok(text: str = "working") -> IterationResult:
    r = IterationResult(exit_code=0)
    r.final_text = text
    r.duration_s = 30.0
    r.usage = {"input": 10, "output": 5, "totalTokens": 15}
    return r


def _interrupted(sup: LoopSupervisor | None = None, _prompt: str = "") -> IterationResult:
    """The engine's own cutoff -- see `tests/test_review_interrupted_not_
    rejection.py`'s `_interrupted` for why `final_text`/`usage` are non-empty
    (a no-traffic interrupt is scored `infra` and silently retried before
    `_step_review`/`_maybe_grace_review` ever see it)."""
    r = IterationResult(exit_code=None)
    r.interrupted = True
    r.duration_s = 5.0
    r.final_text = "partial output before the engine cut this attempt short"
    r.usage = {"input": 3, "output": 1, "totalTokens": 4}
    return r


class _ScriptedAgent:
    def __init__(self, sup: LoopSupervisor, handlers: dict):
        self.sup = sup
        self.handlers = handlers
        self.calls = 0
        self.running = False

    def interrupt(self) -> bool:
        return self.running

    async def run(self, prompt, transcript, **kw) -> IterationResult:
        self.calls += 1
        phase = self.sup.run.read_status().get("phase")
        self.running = True
        try:
            queue = self.handlers.get(phase)
            if queue is None:
                raise AssertionError(
                    f"no scripted handler registered for phase {phase!r} "
                    f"(call {self.calls})")
            if isinstance(queue, list):
                if not queue:
                    raise AssertionError(
                        f"{phase} asked for a {self.calls}th time; script "
                        f"had no entries left for phase {phase!r}")
                step = queue.pop(0)
            else:
                step = queue
            return step(self.sup, prompt)
        finally:
            self.running = False


def _plan(sup: LoopSupervisor, _prompt: str = "") -> IterationResult:
    atomic_write_json(sup.run.root / "plan-proposal.json", {"version": 1, "tasks": [
        {"id": "001", "title": "do the thing",
         "successCriteria": "the thing is done"},
    ]})
    return _ok("planned\n" +
              json.dumps({"proposal_path": "plan-proposal.json", "summary": "planned"}))


def _complete_task(sup: LoopSupervisor, _prompt: str = "") -> IterationResult:
    doc = sup.run.read_tasks()
    task_id = doc["tasks"][0]["id"]
    return _ok("done\n" + json.dumps({"verdict": "complete", "task_id": task_id}))


def _verified(sup: LoopSupervisor, _prompt: str = "") -> IterationResult:
    return _ok("looks right\n" + json.dumps({"verdict": "verified"}))


_supervisor = lambda root, handlers, run_id='task016-review-deadline', **cfg: make_supervisor(
    root, run_id=run_id, status={"state": "starting"}, prd="# PRD\n\nDo the thing.\n",
    max_approaches=2, vigilant=False, on_complete="exit", infra_retry_backoff_s=[0.0],
    infra_retry_backoff_max_s=0.0, infra_outage_budget_s=1000.0,
    runner_factory=lambda sup: _ScriptedAgent(sup, handlers), **cfg)


def _rejected_events(run: RunDir) -> list[dict]:
    return [e for e in read_events(run.root, types=("log",))
            if "review rejected approach" in (e.get("message") or "")]


def _unreviewed_events(run: RunDir) -> list[dict]:
    return read_events(run.root, types=("review_deadline_unreviewed",))


# ---------------------------------------------------------------------------
# 1. Ordinary path: self-loop exhaustion ends `unreviewed`, not `rejected`.
# ---------------------------------------------------------------------------

def test_ordinary_path_interrupted_review_at_budget_end_is_unreviewed(tmp_path):
    handlers = {
        "planning": [_plan],
        "worker": [_complete_task],
        "review": [_interrupted],
    }
    # 3 iterations exactly: plan, worker(COMPLETE), review(interrupted) --
    # the ordinary budget is gone the instant that third attempt returns.
    sup = _supervisor(tmp_path / "run", handlers, iterations=3)
    state = asyncio.run(sup._run_job_core())

    assert state == "failed"
    status = sup.run.read_status()
    assert status["verdict"] == "unreviewed"
    assert status["reason"] == "deadline during review"
    assert status["approach"] == 1
    assert not (sup.run.root / "approaches" / "01").exists()
    assert _rejected_events(sup.run) == []
    assert len(_unreviewed_events(sup.run)) == 1
    assert _unreviewed_events(sup.run)[0]["approach"] == 1


# ---------------------------------------------------------------------------
# 2. The off-budget grace review itself interrupted: same outcome.
# ---------------------------------------------------------------------------

def test_grace_review_interrupted_is_also_unreviewed_not_rejected(tmp_path):
    handlers = {
        "planning": [_plan],
        "worker": [_complete_task],
        "review": [_interrupted],
    }
    # 2 iterations: plan, worker(COMPLETE) -- the ordinary budget is spent
    # the instant the task completes, with review never having started;
    # `_maybe_grace_review` grants one OFF-budget attempt, which is itself
    # interrupted.
    sup = _supervisor(tmp_path / "run", handlers, iterations=2)
    state = asyncio.run(sup._run_job_core())

    assert state == "failed"
    status = sup.run.read_status()
    assert status["verdict"] == "unreviewed"
    assert status["reason"] == "deadline during review"
    assert status["approach"] == 1
    assert not (sup.run.root / "approaches" / "01").exists()
    assert _rejected_events(sup.run) == []
    assert len(_unreviewed_events(sup.run)) == 1
    assert _unreviewed_events(sup.run)[0]["approach"] == 1
    # the grace review really was granted and really did run (off-budget,
    # so it does not appear in status.json's iterationsUsed count).
    grace_logs = [e for e in read_events(sup.run.root, types=("log",))
                  if "grace review" in (e.get("message") or "")]
    assert grace_logs


# ---------------------------------------------------------------------------
# 3. Resume with a fresh window re-reviews the SAME approach.
# ---------------------------------------------------------------------------

def test_resume_after_unreviewed_lands_on_review_same_approach_and_succeeds(tmp_path):
    root = tmp_path / "run"
    handlers1 = {
        "planning": [_plan],
        "worker": [_complete_task],
        "review": [_interrupted],
    }
    sup1 = _supervisor(root, handlers1, iterations=3)
    state1 = asyncio.run(sup1._run_job_core())
    assert state1 == "failed"
    assert sup1.run.read_status()["verdict"] == "unreviewed"

    # Episode 2: a FRESH LoopSupervisor over the SAME run dir -- what
    # `ralphctl resume` leaves behind (`self._pause`/`self._grace_review_
    # granted` etc. are in-memory only; nothing here carries over).
    handlers2 = {"review": [_verified]}
    sup2 = _supervisor(root, handlers2, iterations=6)  # fresh, bigger window

    assert sup2._unreviewed_awaiting_resume(1) is True
    start_approach, skip_planning, step = sup2._resume_point()
    assert (start_approach, step) == (1, "review")
    assert step != "worker"
    assert step != "replan"

    state2 = asyncio.run(sup2._run_job_core())

    assert state2 == "succeeded"
    status2 = sup2.run.read_status()
    assert status2["verdict"] == "verified"
    # the SAME approach was re-reviewed -- never a fresh one.
    assert status2["approach"] == 1
    assert not (sup2.run.root / "approaches" / "01").exists()
    # exactly one iteration ran this episode: the review resume routed to
    # directly, no throwaway worker pass first.
    assert sup2.runner.calls == 1


def test_resume_after_grace_review_unreviewed_also_lands_on_review(tmp_path):
    root = tmp_path / "run"
    handlers1 = {
        "planning": [_plan],
        "worker": [_complete_task],
        "review": [_interrupted],
    }
    sup1 = _supervisor(root, handlers1, iterations=2)
    state1 = asyncio.run(sup1._run_job_core())
    assert state1 == "failed"
    assert sup1.run.read_status()["verdict"] == "unreviewed"

    handlers2 = {"review": [_verified]}
    sup2 = _supervisor(root, handlers2, iterations=6)

    assert sup2._unreviewed_awaiting_resume(1) is True
    start_approach, skip_planning, step = sup2._resume_point()
    assert (start_approach, step) == (1, "review")

    state2 = asyncio.run(sup2._run_job_core())
    assert state2 == "succeeded"
    assert sup2.run.read_status()["approach"] == 1


# ---------------------------------------------------------------------------
# 4. A completed iteration since the unreviewed ending stops the predicate
#    from firing on stale evidence (the same staleness guard every sibling
#    resume predicate already has -- `_paused_awaiting_incurable_ruling`'s
#    own pinned case).
# ---------------------------------------------------------------------------

def test_an_iteration_since_the_unreviewed_ending_stops_the_predicate(tmp_path):
    handlers = {
        "planning": [_plan],
        "worker": [_complete_task],
        "review": [_interrupted],
    }
    sup = _supervisor(tmp_path / "run", handlers, iterations=3)
    asyncio.run(sup._run_job_core())
    assert sup._unreviewed_awaiting_resume(1) is True

    sup.run.emit("iteration.start", number=99, phase="review")
    assert sup._unreviewed_awaiting_resume(1) is False
