"""Task 116 (#60 pillar 3, fact 9): `_step_worker` -- not the agent -- runs
`engine.scheduler.select_next_task` and owns every task-status write. Three
things this module proves directly, driving `LoopSupervisor._step_worker`
for real (fake subprocess, real state machine):

1. The picked task is already `in-progress` ON DISK, with its matching
   `task` event already emitted, at the exact moment the worker subprocess
   is spawned -- not after. A spy planted on the fake agent's own `run()`
   entry point (called strictly once the subprocess would have been
   spawned for real) samples `tasks.json` before doing anything else.
2. No task-status change happens WHILE that subprocess is running (the same
   sampler-thread proof task 019's `test_no_status_change_while_a_long_
   running_hat_executes` uses for the generic-hat dispatcher, here for the
   hand-written worker step).
3. A worker payload that disagrees with the harness's own assignment -- it
   reports `verdict: "complete"` for a task_id it was never assigned --
   is refused as a contract violation, and the record that actually lands
   on disk is what the HARNESS decided (the assigned task claimed
   `in-progress`, nothing else touched), never the agent's word alone.
"""

from __future__ import annotations

import asyncio
import json
import threading
import time

from ralphd.engine import state
from ralphd.engine.config import JobConfig
from ralphd.engine.loop import LoopSupervisor
from ralphd.engine.runner import IterationResult
from ralphd.engine.state import RunDir, atomic_write_json


def _make_sup(tmp_path, tasks, vigilant=False):
    root = tmp_path / "run"
    root.mkdir(parents=True)
    run = RunDir(root=root)
    run.update_status(state="running")
    atomic_write_json(run.tasks_file, {"version": 1, "tasks": tasks})
    cfg = JobConfig(run_id="task116-worker-harness", iterations=5,
                    max_approaches=1, vigilant=vigilant, on_complete="exit",
                    infra_retry_backoff_s=[0.0], infra_retry_backoff_max_s=0.0,
                    infra_outage_budget_s=1000.0)
    sup = LoopSupervisor(cfg, run, root)
    return sup, run


def test_in_progress_lands_on_disk_before_the_worker_subprocess_is_spawned(tmp_path):
    sup, run = _make_sup(tmp_path, [
        {"id": "001", "title": "do the thing", "status": "pending",
         "successCriteria": "the thing is done"},
    ])

    observed_status_at_spawn: list[str] = []
    observed_event_at_spawn: list[bool] = []

    class _Agent:
        def interrupt(self):
            return False

        async def run(self, prompt, transcript, **kw):
            # The FIRST thing the subprocess would see, sampled before this
            # fake agent does anything else -- the harness's pre-spawn
            # claim must already be on disk and already emitted.
            doc = run.read_tasks()
            observed_status_at_spawn.append(doc["tasks"][0]["status"])
            events = state.read_events(run.root)
            observed_event_at_spawn.append(any(
                e.get("type") == "task" and e.get("taskId") == "001"
                and e.get("newStatus") == "in-progress" for e in events))
            r = IterationResult(exit_code=0)
            r.final_text = json.dumps({"verdict": "complete", "task_id": "001"})
            r.duration_s = 0.01
            r.usage = {"input": 1, "output": 1, "totalTokens": 2}
            return r

    sup.runner = _Agent()  # type: ignore[assignment]
    asyncio.run(sup._step_worker())

    assert observed_status_at_spawn == ["in-progress"]
    assert observed_event_at_spawn == [True]
    assert run.read_tasks()["tasks"][0]["status"] == "completed"


def test_no_task_status_change_while_worker_hat_executes(tmp_path):
    sup, run = _make_sup(tmp_path, [
        {"id": "001", "title": "do the thing", "status": "pending",
         "successCriteria": "the thing is done"},
    ])

    handler_started = threading.Event()
    handler_finished = threading.Event()
    SLEEP_S = 0.35

    class _SlowAgent:
        def interrupt(self):
            return False

        async def run(self, prompt, transcript, **kw):
            handler_started.set()
            time.sleep(SLEEP_S)
            r = IterationResult(exit_code=0)
            r.final_text = json.dumps({"verdict": "complete", "task_id": "001"})
            r.duration_s = SLEEP_S
            r.usage = {"input": 1, "output": 1, "totalTokens": 2}
            handler_finished.set()
            return r

    sup.runner = _SlowAgent()  # type: ignore[assignment]

    samples: list[str] = []

    def sample_loop():
        if not handler_started.wait(2.0):
            return
        while not handler_finished.is_set():
            doc = run.read_tasks()
            samples.append(doc["tasks"][0]["status"])
            time.sleep(0.01)

    sampler = threading.Thread(target=sample_loop)
    sampler.start()
    asyncio.run(sup._step_worker())
    sampler.join(2.0)

    assert samples, "sampler never observed the hat in flight -- test did not overlap"
    assert all(s == "in-progress" for s in samples), samples
    assert run.read_tasks()["tasks"][0]["status"] == "completed"


def test_harness_decision_wins_over_a_disagreeing_worker_payload_claim(tmp_path):
    """The scheduler assigns "001" (first in list order, no priority/deps).
    The fake agent's payload instead claims completion of "002" -- a task
    it was never assigned. Task 116's ORIGINAL claim, pinned here before
    v0.13 task 006 (#92 site 4)'s cutover, was: `outcome == ("tail",)` and
    `sup._abort_reason` mentioning "contract violation" -- a single
    disagreeing claim was fatal on the FIRST offence, with no re-ask and no
    ladder at all.

    The NEW claim keeps the old one's real content -- the move is STILL
    refused, and the record that actually persists is the harness's OWN
    decision: "001" sits exactly where the harness itself put it
    (`in-progress`, claimed before spawn), "002" is untouched (still
    `pending`) -- and adds what the old assertion could not see: the
    disagreement is carried back to the SAME worker verbatim on every
    re-ask through `contract_enforcement.enforce_contract` (the EXACT SAME
    ladder `_step_generic_hat`'s sites 1/2 and worker's own site 3 already
    use), the ladder runs the FULL `contract_retry_max + 1` attempts each
    pass (never fewer, never more), and an exhausted ladder is an
    agent-fault continuation -- never an abort, no `_abort_reason`, no
    "contract violation" log message. Because the disagreement never
    resolves, tasks.json never moves on any pass, so it takes exactly one
    pass to record every task's fresh `criteriaFingerprint` baseline (task
    008's own first-sight bookkeeping, itself "progress" for the
    stagnation counter even though nothing about the disagreement changed)
    plus three more genuinely stagnant passes -- each still its own
    exhausted ladder -- to trip the PRE-EXISTING `stagnation_exhausted`
    guard, ending in `("advance", "replan", "stagnation_exhausted")`
    rather than `("tail",)`."""
    sup, run = _make_sup(tmp_path, [
        {"id": "001", "title": "first task", "status": "pending",
         "successCriteria": "first done"},
        {"id": "002", "title": "second task", "status": "pending",
         "successCriteria": "second done"},
    ])

    class _DisagreeingAgent:
        def __init__(self):
            self.calls = 0

        def interrupt(self):
            return False

        async def run(self, prompt, transcript, **kw):
            self.calls += 1
            r = IterationResult(exit_code=0)
            r.final_text = json.dumps({"verdict": "complete", "task_id": "002"})
            r.duration_s = 0.01
            r.usage = {"input": 1, "output": 1, "totalTokens": 2}
            return r

    agent = _DisagreeingAgent()
    sup.runner = agent  # type: ignore[assignment]
    sup._approach = 1  # a run's own approach bookkeeping, not this test's concern
    outcome = asyncio.run(sup._step_worker())

    assert outcome == ("advance", "replan", "stagnation_exhausted")
    assert sup._abort_reason is None
    tasks = {t["id"]: t["status"] for t in run.read_tasks()["tasks"]}
    # the harness's own pre-spawn claim on "001" stands...
    assert tasks["001"] == "in-progress"
    # ...and the disagreeing claim about "002" never touched it.
    assert tasks["002"] == "pending"

    events = state.read_events(run.root)
    violation_events = [e for e in events if e.get("type") == "contract.violation"]
    fault_events = [e for e in events if e.get("type") == "contract.agent_fault"]
    # the first pass's own criteria-fingerprint bookkeeping (task 008)
    # counts as progress and resets the counter once; the three
    # genuinely stagnant passes after it, each running its own full
    # ladder, are what trips the guard.
    assert len(fault_events) == 4, "the ladder must exhaust once per pass"
    assert len(violation_events) == 4 * (sup.cfg.contract_retry_max + 1)
    assert all("is the one this iteration was assigned" in e["errors"][0]
              for e in violation_events)
    assert agent.calls == 4 * (sup.cfg.contract_retry_max + 1)

    msgs = "\n".join(ev.get("message") or "" for ev in events
                     if ev.get("type") == "log")
    assert "contract violation" not in msgs
