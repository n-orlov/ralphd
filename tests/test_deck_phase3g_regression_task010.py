"""v0.13 task 010 (requirement F1/F2, #92): the `deck-phase3g` regression
test, by name, against the EXACT payload that killed that run.

**Cost this protects against:** on `2026-08-28T06:24:03Z`, run `deck-
phase3g` ended `aborted`, `unverified`, at approach 2 of 12, iteration 135
of 900 (765 iterations, 10 of 12 approaches, ~33h of wall clock and
~$174.52 thrown away, after 14h 36m and 311755k tokens) because the
worker's final line was the literal JSON

    {"verdict": "complete", "task_id": 104}

-- `104` a JSON number, not `"104"` a string -- and the pre-cutover code
took that single schema violation as sufficient reason to abort the whole
run outright. Tasks 003-009 already made an exhausted ladder survive
(pinned by `test_contract_exhaustion_never_solely_ends_the_run_task009.py`,
which replays this exact literal payload on every attempt so the ladder
never once recovers); THIS module is the other half of F1 -- proving that
when the SAME literal payload is corrected on a re-ask, the run not only
survives but genuinely finishes the job: task `104` moves exactly as the
corrected payload reports.

`KILLING_PAYLOAD` below is that literal string byte for byte
(`json.dumps({"verdict": "complete", "task_id": 104})` reproduces it
exactly -- both are asserted equal below so nobody can drift the fixture's
shape away from the real one over time).

The failure this payload trips is a SCHEMA violation, not the semantic
`worker_completion_report_errors` check task 008's end-to-end test drove
(that one supplied no `task_id` field at all): `transitions.HAT_SCHEMAS
["worker"]["properties"]["task_id"]` declares `{"type": "string"}`, so
`contracts.validate_payload` rejects the JSON number outright, before the
semantic layer ever runs, with the verbatim error
`"$.task_id: expected string, got int"`. That verbatim string is what a
real re-prompt states to the hat, and this test asserts it is present in
the re-ask prompt byte for byte -- "the error carried back" is not merely
"some retry happened".

**F2 decision, recorded here (not merely in the commit message, so the
next reader of this file has it too): no scalar coercion was added.**
`contracts.validate_payload`/`contract_enforcement.parse_response` still
reject `{"task_id": 104}` outright and rely on the re-ask ladder (already
proven survivable by task 009) to recover -- exactly what this module
demonstrates end to end. The PRD's own reasoning for declining coercion
holds: normalising one scalar field repairs one line, and the next
violation is a different field; the general fix is the ladder itself, not
a growing pile of type-specific normalisers. Because no coercion exists,
there is no second test required proving the ladder against a coercion-
proof violation (F2's own conditional) -- that obligation only attaches
to a commit that adds coercion, and this one does not.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from ralphd.engine.config import JobConfig
from ralphd.engine.loop import LoopSupervisor
from ralphd.engine.runner import IterationResult
from ralphd.engine.state import RunDir, atomic_write_json

TASK_104 = "104"

# The exact payload that killed `deck-phase3g` -- a JSON number where the
# worker contract's `task_id` property requires a string.
KILLING_PAYLOAD = json.dumps({"verdict": "complete", "task_id": 104})
assert KILLING_PAYLOAD == '{"verdict": "complete", "task_id": 104}', KILLING_PAYLOAD

# The verbatim schema-validation error `contracts.validate_payload` raises
# for that exact payload -- what a real re-prompt states to the hat.
VALIDATION_ERROR = "$.task_id: expected string, got int"

# A recognisable fragment of the worker hat's own task prompt body, used
# the same way task 008's end-to-end test uses it: present in the ordinary
# work turn's prompt, absent from the re-ask's.
WORKER_PROMPT_FRAGMENT = "THE ONE RULE: exactly one task, then stop"


def _ok(text: str) -> IterationResult:
    r = IterationResult(exit_code=0)
    r.final_text = text
    r.duration_s = 5.0
    r.usage = {"input": 10, "output": 5, "totalTokens": 15}
    return r


def _review_verified(sup, prompt="") -> IterationResult:
    return _ok("task 104's success criteria hold; nothing left for review "
               "to reject\n" + json.dumps({"verdict": "verified"}))


def _task(**over) -> dict:
    task = {"id": TASK_104,
            "title": "convert clientOpensCreateModalForAgent",
            "status": "in-progress",
            "successCriteria": "the targeted validation run exits 0"}
    task.update(over)
    return task


class _RecordingScriptedAgent:
    """Same dispatch shape as
    `test_worker_contract_reask_end_to_end_task008.py`'s
    `_RecordingScriptedAgent`: one script per phase, keyed by the engine's
    own recorded phase, the last step repeated forever, every call's
    prompt recorded in order."""

    def __init__(self, handlers: dict):
        self.handlers = handlers
        self.calls: dict[str, int] = {}
        self.prompts: list[str] = []
        self.running = False

    def interrupt(self) -> bool:
        return self.running

    async def run(self, prompt, transcript, **kw) -> IterationResult:
        self.prompts.append(prompt)
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
    kw = {"iterations": 3, "max_approaches": 12, "vigilant": False,
          "on_complete": "exit", "infra_retry_backoff_s": [0.0],
          "infra_retry_backoff_max_s": 0.0, "infra_outage_budget_s": 1000.0,
          **cfg_kw}
    sup = LoopSupervisor(JobConfig(run_id="deck-phase3g-regression-task010",
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
    """Mirrors task 008's helper of the same name: seed tasks.json exactly
    like a resumed run, skip planning entirely."""
    RunDir(root=root).update_status(state="starting")
    sup = _supervisor(root, handlers, **cfg_kw)
    atomic_write_json(sup.run.tasks_file, {"version": 1, "tasks": tasks})
    sup.iterations_used = 1
    return sup, asyncio.run(sup._run_job_core())


def test_deck_phase3g_killing_payload_is_refused_then_repaired_and_task104_moves(
        tmp_path):
    root = tmp_path / "run"
    tasks = [_task(status="in-progress")]

    corrected = json.dumps({"verdict": "complete", "task_id": TASK_104})

    sup, final_state = _run_resumed(root, {
        "worker": [lambda s, p: _ok(KILLING_PAYLOAD),
                   lambda s, p: _ok(corrected)],
        "review": [_review_verified],
    }, tasks, iterations=4)

    # -- the violation was genuinely refused, not silently taken ----------
    # (checked via the recorded events below, and via the fact that only
    # the SECOND, corrected attempt could have produced the move).
    events = [json.loads(x) for x in
              (root / "events.jsonl").read_text().splitlines() if x.strip()]
    violation_events = [e for e in events if e.get("type") == "contract.violation"]
    fault_events = [e for e in events if e.get("type") == "contract.agent_fault"]
    assert len(violation_events) == 1, violation_events
    # -- the error was carried back VERBATIM ------------------------------
    assert violation_events[0]["errors"] == [VALIDATION_ERROR]
    assert violation_events[0]["hat"] == "worker"
    assert fault_events == [], "the ladder recovered; there is no agent-fault"

    # -- on the corrected re-ask, task 104 moves exactly as reported -------
    final_tasks = sup.run.read_tasks()["tasks"]
    assert len(final_tasks) == 1
    assert final_tasks[0]["id"] == TASK_104
    assert final_tasks[0]["status"] == "completed", final_tasks[0]

    # the ladder took exactly two attempts for the one worker episode.
    assert sup.runner.calls["worker"] == 2

    # the re-ask never opened a second iteration slot.
    assert sup.iterations_used == 3   # seeded 1, +1 worker charge, +1 review charge
    iterations_root = root / "iterations"
    iteration_dirs = sorted(p.name for p in iterations_root.iterdir() if p.is_dir())
    assert iteration_dirs == ["0002", "0003"], iteration_dirs

    # the re-ask prompt states the verbatim error and excludes the worker
    # hat's own task-prompt body; contrasted against the FIRST call's
    # prompt, which genuinely does carry that fragment.
    assert len(sup.runner.prompts) == 3
    first_prompt, reask_prompt, _review_prompt = sup.runner.prompts
    assert WORKER_PROMPT_FRAGMENT in first_prompt, \
        "sanity: the ordinary work turn's prompt must carry worker.md"
    assert VALIDATION_ERROR in reask_prompt
    assert WORKER_PROMPT_FRAGMENT not in reask_prompt

    # a re-ask transcript exists on disk.
    transcript = root / "artifacts" / "contract" / "0001.jsonl"
    assert transcript.exists(), transcript

    # the run survived and finished -- the invariant this test exists for.
    assert sup._abort_reason is None
    assert final_state == "succeeded", sup.run.read_status()
