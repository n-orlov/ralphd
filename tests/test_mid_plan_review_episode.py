"""Task 022 (PRD requirement K.2, #169): a mid-plan review episode,
default OFF, behind `JobConfig.mid_plan_review`/`ralphctl start
--mid-plan-review`. Once per approach, once the declared fraction
(`loop.MID_PLAN_REVIEW_FRACTION`) of the plan has already reached
terminal-success, `LoopSupervisor._maybe_run_mid_plan_review_episode`
(called from `_step_worker`, the same seam
`_maybe_run_midrun_adjudication_episode` already uses) runs one review
iteration scoped to the already-validated requirements, and opens a cure
task per BLOCKING finding it reports through the SAME `state.insert_cure_
tasks` path an ordinary run-ending review's own cure pass already takes.

Three things pinned here, driving the real `LoopSupervisor._step_worker`
path with a phase-dispatching scripted agent (never a test-local shim),
mirroring `tests/test_midrun_adjudication_episode.py`'s own shape:

1. flag ON, fraction reached -- the episode's "review" call happens
   strictly before the "verify" call, the prompt that dispatch actually
   carries is SCOPED to the already-validated requirements (each one named,
   the not-yet-validated one absent), and its findings open a cure task;
2. flag OFF (the default) -- no "review" call happens at all and today's
   iteration sequence (worker -> verify) is unchanged;
3. flag ON but the episode is INTERRUPTED mid-dispatch -- the
   one-per-approach marker is already on disk at dispatch (read back from
   inside the review dispatch itself), it survives the interruption, and a
   fresh supervisor over the same run directory dispatches no second
   episode for the same approach.
"""

from __future__ import annotations

import asyncio
import inspect
import json
from pathlib import Path

import pytest

from ralphd.engine import findings
from ralphd.engine.config import JobConfig
from ralphd.engine.loop import LoopSupervisor
from ralphd.engine.runner import IterationResult
from ralphd.engine.state import RunDir, atomic_write_json


def _ok(text: str) -> IterationResult:
    r = IterationResult(exit_code=0)
    r.final_text = text
    r.duration_s = 1.0
    r.usage = {"input": 1, "output": 1, "totalTokens": 2}
    return r


class _ScriptedAgent:
    """Phase-dispatching scripted agent -- one script per phase, keyed off
    the CURRENT `status.json` phase, the last step of each repeating
    forever. Records call order and count per phase."""

    def __init__(self, handlers: dict):
        self.handlers = handlers
        self.calls: dict[str, int] = {}
        self.order: list[str] = []
        self.prompts: dict[str, list[str]] = {}
        self.running = False

    def interrupt(self) -> bool:
        return self.running

    async def run(self, prompt, transcript, **kw) -> IterationResult:
        phase = self.sup.run.read_status().get("phase")
        n = self.calls.get(phase, 0)
        self.calls[phase] = n + 1
        self.order.append(phase)
        self.prompts.setdefault(phase, []).append(str(prompt))
        steps = self.handlers[phase]
        self.running = True
        try:
            out = steps[min(n, len(steps) - 1)](self.sup, prompt)
            if inspect.isawaitable(out):
                out = await out
            return out
        finally:
            self.running = False


def _supervisor_over(run: RunDir, root: Path, **cfg_kw) -> LoopSupervisor:
    """A supervisor over an EXISTING run directory -- used both for the
    first process and, in the interrupted-episode probe, for the fresh
    process that resumes the same run dir after an episode was killed
    mid-dispatch."""
    kw = {"iterations": 10, "max_approaches": 3, "vigilant": True,
          "on_complete": "exit", "infra_retry_backoff_s": [0.0],
          "infra_retry_backoff_max_s": 0.0, "infra_outage_budget_s": 1000.0,
          **cfg_kw}
    cfg = JobConfig(run_id="task022-mid-plan-review", **kw)
    sup = LoopSupervisor(cfg, run, root)
    sup._approach = 1
    return sup


def _worker_supervisor(tmp_path: Path, **cfg_kw) -> tuple[LoopSupervisor, RunDir, Path]:
    root = tmp_path / "run"
    root.mkdir(parents=True)
    run = RunDir(root=root)
    run.update_status(state="running")
    # Task 3 is the only one `_step_worker` will pick up as `pending_
    # verify`; tasks 1/2 are already at this run's own terminal-success
    # status, so 2/3 (0.667) is already above the declared 0.5 fraction.
    atomic_write_json(run.tasks_file, {"version": 1, "tasks": [
        {"id": "1", "title": "requirement A: already validated",
         "status": "validated", "successCriteria": "n/a"},
        {"id": "2", "title": "requirement B: already validated",
         "status": "validated", "successCriteria": "n/a"},
        {"id": "3", "title": "requirement C: pending verification",
         "status": "awaiting-validation", "successCriteria": "n/a"},
    ]})
    return _supervisor_over(run, root, **cfg_kw), run, root


def _no_verdict(sup, prompt=""):
    return _ok(json.dumps({"verdict": "no_verdict"}))


def _adjudicated_review(sup, prompt=""):
    return _ok("mid-plan review episode\n" + json.dumps({
        "verdict": "adjudicated"}))


def _verified(sup, prompt=""):
    return _ok(json.dumps({"task_id": "3", "verdict": "verified"}))


_BLOCKING_FINDING = {
    "severity": "blocking", "curable": True,
    "clause": "requirement A (task 022 fixture)",
    "evidence": "the mid-plan review episode found a defect",
    "cured_when": "the defect above is fixed",
}


def test_flag_on_runs_one_scoped_episode_before_verify_and_opens_a_cure_task(
        tmp_path):
    sup, run, workspace = _worker_supervisor(tmp_path, mid_plan_review=True)
    # The review hat's own structured findings sidecar -- pre-seeded on
    # disk exactly like `tests/test_cure_pass.py`'s own cure-pass fixtures
    # do, since `_review_findings_channel` reads this fixed path, never
    # the payload's own `findings_path` string.
    findings.write_findings_json(run.findings_json_file, [_BLOCKING_FINDING])

    agent = _ScriptedAgent({
        "worker": [_no_verdict],
        "review": [_adjudicated_review],
        "verify": [_verified],
    })
    agent.sup = sup  # type: ignore[attr-defined]
    sup.runner = agent  # type: ignore[assignment]

    asyncio.run(sup._step_worker())

    # 1. ordering: the episode's own "review" call happened strictly
    # before the "verify" call.
    assert "review" in agent.order and "verify" in agent.order
    assert agent.order.index("review") < agent.order.index("verify")
    assert agent.calls.get("review") == 1
    assert agent.calls.get("verify") == 1

    # 1b. SCOPING: the prompt that dispatch actually carried names every
    # requirement whose task has already reached terminal-success, and
    # none of the ones that have not -- the criterion's "scoped review
    # episode running over the requirements whose tasks are `validated`".
    review_prompt = agent.prompts["review"][0]
    assert "SCOPED review episode" in review_prompt
    head, _, scoped = review_prompt.partition(
        "### Already-validated requirements in scope")
    assert scoped, "the review dispatch carried no scoped-requirements block"
    for tid, title in (("1", "requirement A: already validated"),
                       ("2", "requirement B: already validated")):
        assert f"**{tid}**" in scoped
        assert title in scoped
    # The task still awaiting validation is NOT in the episode's scope,
    # anywhere in the prompt.
    assert "requirement C: pending verification" not in review_prompt
    assert "**3**" not in scoped

    # 2. the episode is recorded once per approach.
    assert sup.run.read_status().get("midPlanReviewEpisodes") == {"1": True}

    # 3. the episode's own "review" phase event is tagged distinctly.
    events = [json.loads(x) for x in
             (run.root / "events.jsonl").read_text().splitlines() if x.strip()]
    phase_events = [e for e in events if e.get("type") == "phase"
                    and e.get("phase") == "review"]
    assert any(e.get("midPlanReview") is True for e in phase_events)

    # 4. its blocking finding opened a cure task through the EXISTING
    # cure machinery (`state.insert_cure_tasks`, same as an ordinary
    # review's own cure pass) -- never a new mechanism.
    tasks = sup.run.read_tasks()["tasks"]
    cure_tasks = [t for t in tasks if t.get("origin") == "review-cure"]
    assert len(cure_tasks) == 1
    cure_events = [e for e in events if e.get("type") == "approach.mid_plan_review_cure"]
    assert len(cure_events) == 1
    assert cure_events[0]["taskIds"] == [cure_tasks[0]["id"]]

    # The episode never replaced or blocked the ordinary verify step.
    task_3 = next(t for t in tasks if t["id"] == "3")
    assert task_3["status"] == "validated"

    # The episode's own reply never counted as a terminal review verdict.
    assert not any(e.get("type") == "signal" and e.get("signal") == "VERIFIED"
                  for e in events)


def test_flag_off_by_default_runs_no_episode_and_the_sequence_is_unchanged(
        tmp_path):
    """Default OFF: no `mid_plan_review` kwarg at all -- `JobConfig`'s own
    default (`False`) applies, and today's worker -> verify sequence is
    unchanged: no extra "review" call, no cure task, no recorded episode."""
    sup, run, workspace = _worker_supervisor(tmp_path)
    assert sup.cfg.mid_plan_review is False
    findings.write_findings_json(run.findings_json_file, [_BLOCKING_FINDING])

    agent = _ScriptedAgent({
        "worker": [_no_verdict],
        "review": [_adjudicated_review],
        "verify": [_verified],
    })
    agent.sup = sup  # type: ignore[attr-defined]
    sup.runner = agent  # type: ignore[assignment]

    asyncio.run(sup._step_worker())

    assert agent.calls.get("review", 0) == 0
    assert agent.calls.get("verify") == 1
    assert "review" not in agent.order
    assert agent.order[:2] == ["worker", "verify"]

    assert sup.run.read_status().get("midPlanReviewEpisodes") is None
    tasks = sup.run.read_tasks()["tasks"]
    cure_tasks = [t for t in tasks if t.get("origin") == "review-cure"]
    assert cure_tasks == []
    task_3 = next(t for t in tasks if t["id"] == "3")
    assert task_3["status"] == "validated"

    events = [json.loads(x) for x in
             (run.root / "events.jsonl").read_text().splitlines() if x.strip()]
    assert not any(e.get("type") == "approach.mid_plan_review_cure" for e in events)
    assert not any(e.get("type") == "phase" and e.get("midPlanReview")
                  for e in events)


def test_an_interrupted_episode_still_spends_the_approachs_one_episode(tmp_path):
    """The one-per-approach guarantee has to hold across an episode killed
    mid-dispatch (a crash, an operator interrupt, the job deadline
    cancelling the iteration task): the marker `midPlanReviewEpisodes` is
    written at DISPATCH -- what SPEC.md's and `docs/api.md`'s own rows claim
    -- so it is already readable from inside the review dispatch itself,
    survives a genuine external cancellation of the episode's task, and a
    FRESH supervisor over the same run directory dispatches no second
    episode for the same approach.
    """
    sup, run, root = _worker_supervisor(tmp_path, mid_plan_review=True)
    marker_at_dispatch: dict = {}

    async def _interrupted_mid_dispatch(s, prompt=""):
        # Read the marker from inside the dispatch itself, then hang until
        # something outside kills us -- exactly the shape of an iteration
        # in flight when the process dies.
        marker_at_dispatch["value"] = (
            s.run.read_status().get("midPlanReviewEpisodes"))
        await asyncio.sleep(3600)
        raise AssertionError("the dispatch was never cancelled")

    agent = _ScriptedAgent({
        "worker": [_no_verdict],
        "review": [_interrupted_mid_dispatch],
        "verify": [_verified],
    })
    agent.sup = sup  # type: ignore[attr-defined]
    sup.runner = agent  # type: ignore[assignment]

    async def _dispatch_then_cancel() -> None:
        episode = asyncio.ensure_future(
            sup._maybe_run_mid_plan_review_episode())
        for _ in range(2000):
            if "value" in marker_at_dispatch:
                break
            await asyncio.sleep(0.005)
        else:  # pragma: no cover - the dispatch never happened
            episode.cancel()
            raise AssertionError("the review dispatch never started")
        episode.cancel()
        with pytest.raises(asyncio.CancelledError):
            await episode

    asyncio.run(_dispatch_then_cancel())

    # The marker was on disk BEFORE the dispatch that got interrupted --
    # read back by the dispatch itself, not after the fact.
    assert marker_at_dispatch["value"] == {"1": True}
    # ... and it survived the cancellation.
    assert run.read_status().get("midPlanReviewEpisodes") == {"1": True}

    # A fresh supervisor over the SAME run directory (the resumed process)
    # spends no second episode on approach 1.
    sup2 = _supervisor_over(run, root, mid_plan_review=True)
    agent2 = _ScriptedAgent({
        "worker": [_no_verdict],
        "review": [_adjudicated_review],
        "verify": [_verified],
    })
    agent2.sup = sup2  # type: ignore[attr-defined]
    sup2.runner = agent2  # type: ignore[assignment]

    asyncio.run(sup2._maybe_run_mid_plan_review_episode())

    assert agent2.calls.get("review", 0) == 0
    assert agent2.order == []
    assert run.read_status().get("midPlanReviewEpisodes") == {"1": True}
    events = [json.loads(x) for x in
             (run.root / "events.jsonl").read_text().splitlines() if x.strip()]
    mid_plan_phase_events = [e for e in events if e.get("type") == "phase"
                             and e.get("midPlanReview") is True]
    assert len(mid_plan_phase_events) == 1
