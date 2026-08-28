"""v0.13 task 008 (requirement B): the re-ask channel proved end to end on
the REAL worker path (`_step_worker`), not just against `enforce_contract`
in isolation (`tests/test_contract_enforcement.py`) or `_ask_contract_fix`
in isolation (`tests/test_contract_reask_channel_task002.py`).

Drives `_step_worker` (via `_run_job_core`, exactly like
`tests/test_worker_verdict_schema_error_task007.py` and
`tests/test_unsatisfiable_criteria.py` already do) with a stubbed
`sup.runner` whose FIRST final payload for the one worker iteration is a
`verdict: "complete"` report missing the required `task_id`
(`transitions.worker_completion_report_errors`'s own semantic check --
`transitions.HAT_SCHEMAS["worker"]` itself does not require `task_id`, so
this is deliberately the SEMANTIC half of site 4's (task 006) two-layer
check, not the schema half site 5/007 already covers) -- and whose re-ask
answer supplies the missing `task_id`, correctly naming the one task the
scheduler assigned.

Asserts all four of requirement B's acceptance points directly:

(a) the task moved EXACTLY as the corrected payload said -- task 001, and
    only task 001, ends `"completed"`;
(b) `iterations_used` advanced by exactly ONE across the whole episode --
    the re-ask never opens a second `iterations/NNNN/` slot, confirmed both
    by the counter and by the directory listing;
(c) the re-ask prompt (captured directly from the stub's second call)
    contains the verbatim validation error string
    (`"verdict 'complete' requires a non-empty task_id"`) and does NOT
    contain the worker hat's own task-prompt body (a recognisable fragment
    of `src/ralphd/prompts/worker.md`, contrasted against the FIRST call's
    prompt, which DOES carry that fragment -- proving the fragment is
    genuinely present in the ordinary case and specifically absent from the
    re-ask);
(d) a re-ask transcript exists on disk under the run's
    `artifacts/contract/0001.jsonl`.

A one-line revert of task 006's cutover (putting back the inline
`self._abort_reason = message; return ("tail",)` pair for the `"complete"`
branch) was checked by hand to make this module's main test fail outright:
the task never moves to `"completed"` at all (the FIRST, malformed payload
aborts the run before the corrected second one is ever asked for), so
assertion (a) fails immediately -- there is no re-ask to inspect for (b)/(c)
and no transcript for (d) either.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from ralphd.engine.config import JobConfig
from ralphd.engine.loop import LoopSupervisor
from ralphd.engine.runner import IterationResult
from ralphd.engine.state import RunDir, atomic_write_json

TASK_1 = "001"

# The exact semantic-check error text `transitions.worker_completion_report_
# errors` raises for a `verdict: "complete"` report with no `task_id` --
# the FIRST payload's own violation, which the re-ask must state verbatim.
VALIDATION_ERROR = "verdict 'complete' requires a non-empty task_id"

# A recognisable fragment of the worker hat's own task prompt body (from
# `src/ralphd/prompts/worker.md`) -- must be present in the FIRST call's
# prompt (the ordinary work turn) and absent from the re-ask's.
WORKER_PROMPT_FRAGMENT = "THE ONE RULE: exactly one task, then stop"


def _ok(text: str) -> IterationResult:
    r = IterationResult(exit_code=0)
    r.final_text = text
    r.duration_s = 5.0
    r.usage = {"input": 10, "output": 5, "totalTokens": 15}
    return r


def _review_verified(sup, prompt="") -> IterationResult:
    # Task 119 (#60 pillar 3, fact 9): review reports through the `review`
    # JSON contract -- a correctly-completed task 001 is genuinely done, so
    # this run has nothing left for review to reject.
    return _ok("the PRD holds for what remains\n"
               + json.dumps({"verdict": "verified"}))


def _task(**over) -> dict:
    task = {"id": TASK_1, "title": "do the thing",
            "status": "in-progress", "successCriteria": "the thing works"}
    task.update(over)
    return task


class _RecordingScriptedAgent:
    """Same dispatch shape as `test_worker_verdict_schema_error_task007.py`'s
    `_ScriptedAgent` (one script per phase, keyed by the engine's own
    recorded phase, the last step repeating forever) -- but also records
    every call's `prompt` argument, in order, so a re-ask's prompt can be
    inspected directly rather than inferred from its effects alone."""

    def __init__(self, handlers: dict):
        self.handlers = handlers
        self.calls: dict[str, int] = {}
        self.prompts: list[str] = []
        self.running = False

    def interrupt(self) -> bool:
        return self.running

    async def run(self, prompt, transcript, **kw) -> IterationResult:
        self.prompts.append(prompt)
        # Mirror the real `PiRunner.run`: the transcript file is a side
        # effect of running, not something the caller creates itself --
        # `_ask_contract_fix` (and `_run_iteration_once`) both just pass a
        # `Path` and expect it to exist afterwards.
        Path(transcript).parent.mkdir(parents=True, exist_ok=True)
        Path(transcript).write_text('{"type": "assistant", "text": "stub"}\n')
        phase = self.sup.run.read_status().get("phase")
        n = self.calls.get(phase, 0)
        self.calls[phase] = n + 1
        steps = self.handlers[phase]
        self.running = True
        try:
            return steps[min(n, len(steps) - 1)](self.sup, prompt)
        finally:
            self.running = False


def _supervisor(root: Path, handlers: dict, **cfg_kw) -> LoopSupervisor:
    root.mkdir(parents=True, exist_ok=True)
    run = RunDir(root=root)
    kw = {"iterations": 2, "max_approaches": 1, "vigilant": False,
          "on_complete": "exit", "infra_retry_backoff_s": [0.0],
          "infra_retry_backoff_max_s": 0.0, "infra_outage_budget_s": 1000.0,
          **cfg_kw}
    sup = LoopSupervisor(JobConfig(run_id="v013-task008-worker-reask-e2e",
                                    **kw), run, root)
    agent = _RecordingScriptedAgent(handlers)
    agent.sup = sup                      # type: ignore[attr-defined]
    sup.runner = agent                    # type: ignore[assignment]

    async def no_backoff(seconds):
        return seconds, False

    sup._wait_out_backoff = no_backoff    # type: ignore[method-assign]
    return sup


def _run_resumed(root: Path, handlers: dict, tasks: list[dict],
                 **cfg_kw) -> tuple[LoopSupervisor, str]:
    """Mirrors `test_unsatisfiable_criteria.py`/`test_worker_verdict_schema_
    error_task007.py`'s helper of the same name: seed tasks.json exactly
    like a resumed run, skip planning entirely (`handlers` must have no
    `"planning"` key)."""
    RunDir(root=root).update_status(state="starting")
    sup = _supervisor(root, handlers, **cfg_kw)
    atomic_write_json(sup.run.tasks_file, {"version": 1, "tasks": tasks})
    sup.iterations_used = 1
    return sup, asyncio.run(sup._run_job_core())


def test_a_malformed_complete_report_is_repaired_by_one_uncharged_re_ask(
        tmp_path):
    root = tmp_path / "run"
    tasks = [_task(status="in-progress")]

    malformed = json.dumps({"verdict": "complete"})           # no task_id
    corrected = json.dumps({"verdict": "complete", "task_id": TASK_1})

    sup, state = _run_resumed(root, {
        "worker": [lambda s, p: _ok(malformed), lambda s, p: _ok(corrected)],
        "review": [_review_verified],
    }, tasks, iterations=3)  # _run_resumed seeds iterations_used=1 already;
    # room for the worker's own charged call PLUS the run-ending review's --
    # kept apart from a grace review so "charged" accounting stays simple.

    # -- (a) the task moved EXACTLY as the corrected payload said --------
    final_tasks = sup.run.read_tasks()["tasks"]
    assert len(final_tasks) == 1
    assert final_tasks[0]["id"] == TASK_1
    assert final_tasks[0]["status"] == "completed", final_tasks[0]

    # the ladder took exactly two attempts: attempt 0 (the malformed
    # payload, reused from the already-completed charged iteration -- no
    # extra hat call) and attempt 1 (the re-ask, corrected).
    assert sup.runner.calls["worker"] == 2

    # -- (b) `iterations_used` advanced by exactly ONE across the WHOLE ---
    #        worker episode -- the re-ask never opens a second iteration
    #        slot despite the ladder's two attempts. The run then goes on
    #        to a SEPARATE, ordinary charged review iteration (expected --
    #        every run needs one), which this assertion tells apart from
    #        the re-ask by checking each iteration DIRECTORY's own recorded
    #        `phase`, not merely counting directories.
    assert sup.iterations_used == 3   # seeded 1, +1 worker charge, +1 review charge
    iterations_root = root / "iterations"
    iteration_dirs = sorted(p.name for p in iterations_root.iterdir() if p.is_dir())
    assert iteration_dirs == ["0002", "0003"], iteration_dirs
    phases = {d: json.loads((iterations_root / d / "meta.json").read_text())["phase"]
              for d in iteration_dirs}
    assert phases == {"0002": "worker", "0003": "review"}, phases

    # -- (c) the re-ask prompt states the verbatim error and excludes the -
    #        worker task prompt; contrasted against the FIRST call's prompt,
    #        which genuinely does carry the task-prompt fragment. A THIRD
    #        prompt follows for the separate, ordinary review iteration --
    #        irrelevant to requirement B, so only the first two are checked.
    assert len(sup.runner.prompts) == 3
    first_prompt, reask_prompt, _review_prompt = sup.runner.prompts
    assert WORKER_PROMPT_FRAGMENT in first_prompt, \
        "sanity: the ordinary work turn's prompt must carry worker.md"
    assert VALIDATION_ERROR in reask_prompt
    assert WORKER_PROMPT_FRAGMENT not in reask_prompt
    assert "do the thing" not in reask_prompt.lower()

    # -- (d) a re-ask transcript exists on disk under the run's artifacts -
    transcript = root / "artifacts" / "contract" / "0001.jsonl"
    assert transcript.exists(), transcript

    # every attempt was recorded, and the ladder resolved cleanly -- no
    # agent-fault, and the run's own contract-violation event for the
    # first (malformed) attempt is on the record either way (requirement G).
    events = [json.loads(x) for x in
              (root / "events.jsonl").read_text().splitlines() if x.strip()]
    violation_events = [e for e in events if e.get("type") == "contract.violation"]
    fault_events = [e for e in events if e.get("type") == "contract.agent_fault"]
    assert len(violation_events) == 1
    assert violation_events[0]["errors"] == [VALIDATION_ERROR]
    assert fault_events == []

    assert sup._abort_reason is None
    assert state == "succeeded", sup.run.read_status()
