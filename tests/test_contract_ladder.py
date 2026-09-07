"""D.3 -- the contract-violation re-ask ladder, folded into one module
(task 024, requirement D.3). Absorbs, whole, the seven `test_*_task*.py`
modules whose subject is `contract_enforcement.enforce_contract`'s uncharged
re-ask channel and its cutover across every hat that consults it:

* `tests/test_contract_reask_channel_task002.py` (task 002, #60 pillar 3,
  requirement B): `LoopSupervisor._ask_contract_fix` in isolation -- the
  channel itself, wired to nothing yet -- never charges an iteration, opens
  no `iterations/` directory, records `usage.byPhase["contract"]` and a
  transcript under `artifacts/contract/NNNN.jsonl`, states the validation
  error verbatim in its prompt while excluding the worker task prompt's own
  body, and counts separately from the ordinary `_ask_router` counter.
* `tests/test_contract_reask_model_task004.py` (task 004, steer 002 item 3,
  requirement B model decision): which model the re-ask resolves -- the
  OFFENDING HAT's own model (`self.cfg.model_for(hat)`), never a fixed or
  borrowed entry.
* `tests/test_worker_contract_reask_end_to_end_task008.py` (task 008,
  requirement B): the channel proved end to end on the real worker path
  (`_step_worker`) -- a malformed `verdict: "complete"` report missing its
  `task_id` is repaired by one uncharged re-ask, the task moves exactly as
  the corrected payload said, and `iterations_used` advances by exactly one
  for the whole episode.
* `tests/test_worker_verdict_schema_error_task007.py` (task 007, #92 site 5,
  the last of the five-site cutover map): the remaining `elif
  schema_errors:` branch -- an unrecognised `verdict` enum value -- rides
  the same ladder instead of a single validate-or-abort check; ladder
  exhaustion is an agent-fault continuation, never an abort.
* `tests/test_verify_contract_ladder_task001.py` (task 001, requirement
  A.1, #119): `_verify_task` parses its reply through
  `contract_enforcement.enforce_contract` instead of a manual parse-and-
  record block that burned a validation attempt on the FIRST bad reply.
* `tests/test_review_contract_ladder_task002.py` (task 002, requirement
  A.2, #119): the widened `_review_contract_ladder` entry condition -- any
  unparseable or schema-failing review reply now also rides the ladder,
  through a real `_step_review()`.
* `tests/test_verdict_gate_contract_ladder_task003.py` (task 003,
  requirement A.3, #119): the same ladder pushed INSIDE
  `_verdict_gate_verdict` itself, through a real `_step_verdict_gate()`.

No two absorbed modules collided on a node name; every node below keeps its
original function name. Helpers that were byte-identical across two or more
of the seven source modules (`_ok`, `_events`, `_SequencedRunner`,
`_StubRunner`, `_task`, `TASK_1`, `WORKER_PROMPT_FRAGMENT`) are declared
once here and shared; helpers that differed even slightly keep a
section-specific suffix so no section's behaviour changed by merging.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from ralphd.engine.config import JobConfig
from ralphd.engine.loop import LoopSupervisor
from ralphd.engine.runner import IterationResult
from ralphd.engine.state import RunDir, atomic_write_json
from tests.conftest import scripted_supervisor, supervisor_factory, supervisor_with_runner

# A recognisable fragment of the worker hat's own task prompt body (from
# `src/ralphd/prompts/worker.md`) -- must never leak into a contract re-ask.
WORKER_PROMPT_FRAGMENT = "THE ONE RULE: exactly one task, then stop"

TASK_1 = "001"


def _ok(text: str) -> IterationResult:
    r = IterationResult(exit_code=0, duration_s=5.0)
    r.final_text = text
    r.usage = {"input": 10, "output": 5, "totalTokens": 15}
    return r


def _events(root: Path) -> list[dict]:
    p = root / "events.jsonl"
    if not p.exists():
        return []
    return [json.loads(x) for x in p.read_text().splitlines() if x.strip()]


def _task(**over) -> dict:
    task = {"id": TASK_1, "title": "do the thing",
            "status": "in-progress", "successCriteria": "the thing works"}
    task.update(over)
    return task


class _StubRunner:
    """Records every call's prompt/transcript/model and returns a scripted,
    successful `IterationResult` -- these tests never spawn a subprocess.
    Shared by the reask-channel and reask-model sections (task 002/004),
    which drive `_ask_contract_fix` directly rather than through a
    supervisor state method."""

    def __init__(self):
        self.calls: list[dict] = []

    async def run(self, prompt, transcript, **kw):
        self.calls.append({"prompt": prompt, "transcript": Path(transcript), **kw})
        # Mirror the real `PiRunner.run`: the transcript file is created (and
        # appended to) as a side effect of running, not by the caller.
        Path(transcript).parent.mkdir(parents=True, exist_ok=True)
        Path(transcript).write_text('{"type": "assistant", "text": "stub"}\n')
        r = IterationResult(exit_code=0)
        r.final_text = '{"task_id": "042", "verdict": "complete"}'
        r.duration_s = 0.01
        r.usage = {"input": 12, "output": 6, "totalTokens": 18}
        return r


class _SequencedRunner:
    """Replays one `IterationResult` per call, in order (the last one
    repeats forever once the sequence runs out) -- shared by the three
    A.1/A.2/A.3 ladder sections (verify/review/verdict_gate), which each
    need the charged call and the uncharged re-ask(s) that follow it to
    answer differently."""

    def __init__(self, results):
        self.results = list(results)
        self.calls = 0
        self.prompts: list[str] = []
        self.running = False

    def interrupt(self) -> bool:
        return self.running

    async def run(self, prompt, transcript, **kw) -> IterationResult:
        self.prompts.append(prompt)
        idx = min(self.calls, len(self.results) - 1)
        self.calls += 1
        return self.results[idx]


# ===========================================================================
# task 002 (#60 pillar 3, requirement B): `_ask_contract_fix` in isolation.
# ===========================================================================

# The exact schema-violation shape task 002's own acceptance text names.
VALIDATION_ERROR = "$.task_id: expected string, got int"

_supervisor_reask_channel = supervisor_factory(
    run_id="task002-contract-reask", cfg_defaults=False)


async def test_ask_contract_fix_never_charges_an_iteration(tmp_path):
    sup = _supervisor_reask_channel(tmp_path)
    stub = _StubRunner()
    sup.runner = stub  # never sup._run_iteration_once
    iterations_before = sup.iterations_used

    text, usage = await sup._ask_contract_fix(
        "worker", [VALIDATION_ERROR])

    assert sup.iterations_used == iterations_before
    assert not (tmp_path / "iterations").exists() or \
        list((tmp_path / "iterations").iterdir()) == []
    assert usage  # a non-empty usage dict was handed back


async def test_ask_contract_fix_records_usage_and_transcript_under_own_bucket(tmp_path):
    sup = _supervisor_reask_channel(tmp_path)
    stub = _StubRunner()
    sup.runner = stub

    await sup._ask_contract_fix("worker", [VALIDATION_ERROR])

    status = sup.run.read_status()
    by_phase = status.get("usage", {}).get("byPhase", {})
    assert by_phase.get("contract"), "usage.byPhase['contract'] must be non-empty"

    assert len(stub.calls) == 1
    transcript = stub.calls[0]["transcript"]
    assert transcript.parent == tmp_path / "artifacts" / "contract"
    assert transcript.name == "0001.jsonl"
    assert transcript.exists()


async def test_ask_contract_fix_prompt_states_the_error_verbatim_and_nothing_else(tmp_path):
    sup = _supervisor_reask_channel(tmp_path)
    stub = _StubRunner()
    sup.runner = stub

    await sup._ask_contract_fix("worker", [VALIDATION_ERROR])

    prompt = stub.calls[0]["prompt"]
    assert VALIDATION_ERROR in prompt
    assert WORKER_PROMPT_FRAGMENT not in prompt
    assert "do the thing" not in prompt.lower()
    for invite in ("do the work", "continue working", "keep working",
                   "make more changes", "implement the task"):
        assert invite not in prompt.lower()


async def test_ask_contract_fix_uses_its_own_counter_across_calls(tmp_path):
    sup = _supervisor_reask_channel(tmp_path)
    stub = _StubRunner()
    sup.runner = stub

    await sup._ask_contract_fix("worker", [VALIDATION_ERROR])
    await sup._ask_contract_fix("worker", ["a second, different error"])

    names = sorted(c["transcript"].name for c in stub.calls)
    assert names == ["0001.jsonl", "0002.jsonl"]
    # the router's own counter must stay untouched by contract calls
    assert sup._router_calls == 0
    assert sup._contract_calls == 2


# ===========================================================================
# task 004 (steer 002 item 3, requirement B model decision): which model
# `_ask_contract_fix` resolves for its re-ask.
# ===========================================================================

PLANNING_MODEL = "amazon-bedrock/eu.anthropic.claude-opus-5"
WORKER_MODEL = "amazon-bedrock/eu.anthropic.claude-sonnet-5"

_supervisor_reask_model = supervisor_factory(
    run_id="task004-contract-reask-model", cfg_defaults=False,
    model_overrides={'planning': PLANNING_MODEL, 'worker': WORKER_MODEL})


async def test_ask_contract_fix_uses_the_offending_hats_own_model(tmp_path):
    """A worker-hat re-ask must resolve WORKER's own model, not planning's
    (the fallback `model_for("contract")` used to borrow) and not `None`
    (pi's own default, what a literal but unconfigured "contract" hat
    would resolve to)."""
    sup = _supervisor_reask_model(tmp_path)
    stub = _StubRunner()
    sup.runner = stub

    await sup._ask_contract_fix("worker", [VALIDATION_ERROR])

    assert stub.calls[0]["model"] == WORKER_MODEL
    assert stub.calls[0]["model"] != PLANNING_MODEL


async def test_ask_contract_fix_model_matches_model_for_the_hat(tmp_path):
    """The re-ask's model must always equal `cfg.model_for(hat)` for
    whichever hat is being repaired -- pinned for a second hat (`planning`
    itself) too, so this is not just "always worker"."""
    sup = _supervisor_reask_model(tmp_path)
    stub = _StubRunner()
    sup.runner = stub

    await sup._ask_contract_fix("planning", [VALIDATION_ERROR])

    assert stub.calls[0]["model"] == sup.cfg.model_for("planning") == PLANNING_MODEL


# ===========================================================================
# task 008 (requirement B): the re-ask channel proved end to end on the
# real worker path (`_step_worker`).
# ===========================================================================

# The exact semantic-check error text `transitions.worker_completion_report_
# errors` raises for a `verdict: "complete"` report with no `task_id` --
# the FIRST payload's own violation, which the re-ask must state verbatim.
VALIDATION_ERROR_MISSING_TASK_ID = "verdict 'complete' requires a non-empty task_id"


class _RecordingScriptedAgent:
    """Same dispatch shape as the schema-error section's `_ScriptedAgent`
    (one script per phase, keyed by the engine's own recorded phase, the
    last step repeating forever) -- but also records every call's `prompt`
    argument, in order, so a re-ask's prompt can be inspected directly
    rather than inferred from its effects alone."""

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


def _review_verified(sup, prompt="") -> IterationResult:
    # Task 119 (#60 pillar 3, fact 9): review reports through the `review`
    # JSON contract -- a correctly-completed task 001 is genuinely done, so
    # this run has nothing left for review to reject.
    return _ok("the PRD holds for what remains\n"
               + json.dumps({"verdict": "verified"}))


_supervisor_worker_e2e = supervisor_with_runner(
    lambda sup, handlers: _RecordingScriptedAgent(handlers),
    run_id="v013-task008-worker-reask-e2e", iterations=2, max_approaches=1, vigilant=False,
    on_complete="exit", infra_retry_backoff_s=[0.0], infra_retry_backoff_max_s=0.0,
    infra_outage_budget_s=1000.0)


def _run_resumed_worker_e2e(root: Path, handlers: dict, tasks: list[dict],
                            **cfg_kw) -> tuple[LoopSupervisor, str]:
    """Mirrors the schema-error section's helper of the same name: seed
    tasks.json exactly like a resumed run, skip planning entirely
    (`handlers` must have no `"planning"` key)."""
    RunDir(root=root).update_status(state="starting")
    sup = _supervisor_worker_e2e(root, handlers, **cfg_kw)
    atomic_write_json(sup.run.tasks_file, {"version": 1, "tasks": tasks})
    sup.iterations_used = 1
    return sup, asyncio.run(sup._run_job_core())


def test_a_malformed_complete_report_is_repaired_by_one_uncharged_re_ask(
        tmp_path):
    root = tmp_path / "run"
    tasks = [_task(status="in-progress")]

    malformed = json.dumps({"verdict": "complete"})           # no task_id
    corrected = json.dumps({"verdict": "complete", "task_id": TASK_1})

    sup, state = _run_resumed_worker_e2e(root, {
        "worker": [lambda s, p: _ok(malformed), lambda s, p: _ok(corrected)],
        "review": [_review_verified],
    }, tasks, iterations=3)  # _run_resumed_worker_e2e seeds iterations_used=1
    # already; room for the worker's own charged call PLUS the run-ending
    # review's -- kept apart from a grace review so "charged" accounting
    # stays simple.

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
    assert VALIDATION_ERROR_MISSING_TASK_ID in reask_prompt
    assert WORKER_PROMPT_FRAGMENT not in reask_prompt
    assert "do the thing" not in reask_prompt.lower()

    # -- (d) a re-ask transcript exists on disk under the run's artifacts -
    transcript = root / "artifacts" / "contract" / "0001.jsonl"
    assert transcript.exists(), transcript

    # every attempt was recorded, and the ladder resolved cleanly -- no
    # agent-fault, and the run's own contract-violation event for the
    # first (malformed) attempt is on the record either way (requirement G).
    events = _events(root)
    violation_events = [e for e in events if e.get("type") == "contract.violation"]
    fault_events = [e for e in events if e.get("type") == "contract.agent_fault"]
    assert len(violation_events) == 1
    assert violation_events[0]["errors"] == [VALIDATION_ERROR_MISSING_TASK_ID]
    assert fault_events == []

    assert sup._abort_reason is None
    assert state == "succeeded", sup.run.read_status()

    # Requirement B.2 (#128)'s own generic re-dispatch site is NOT the
    # ladder this scenario exercises: `{"verdict": "complete"}` with no
    # `task_id` is already schema-VALID (`task_id` is optional in the
    # schema itself -- only `transitions.worker_completion_report_errors`'s
    # SEMANTIC check rejects it), so it matches `elif verdict == "complete":`
    # directly on the very first pass and is repaired by THAT branch's own
    # pre-existing (task 006) ladder, never touching the generic
    # `elif schema_errors:` site at all -- the counter stays at 0.
    # `tests/test_worker_dispatch_reentry_task008.py`'s own
    # `test_generic_schema_site_repair_counts_the_re_entry_once` pins the
    # v0.17 task 008 site with a genuinely schema-invalid first payload.
    assert sup._last_worker_dispatch_reentries == 0


# ===========================================================================
# task 007 (#92 site 5, the last of the five-site cutover map): the
# remaining `elif schema_errors:` branch in `_step_worker`.
# ===========================================================================


def _bad_verdict() -> IterationResult:
    """A worker final line whose `verdict` the schema's closed enum
    (`["complete", "no_verdict", "unsatisfiable"]`) rejects outright --
    valid JSON, but not any recognised verdict at all."""
    payload = json.dumps({"verdict": "give_up"})
    return _ok(f"I am stuck on task {TASK_1}.\n{payload}")


_supervisor_schema_error = scripted_supervisor(
    run_id="v013-task007-schema-verdict", iterations=2, max_approaches=1,
    vigilant=False, on_complete="exit", infra_outage_budget_s=1000.0)


def _run_resumed_schema_error(root: Path, handlers: dict, tasks: list[dict],
                              **cfg_kw) -> tuple[LoopSupervisor, str]:
    """Mirrors the worker-e2e section's helper of the same name: seed
    tasks.json exactly like a resumed run, skip planning entirely
    (`handlers` must have no `"planning"` key)."""
    RunDir(root=root).update_status(state="starting")
    sup = _supervisor_schema_error(root, handlers, **cfg_kw)
    atomic_write_json(sup.run.tasks_file, {"version": 1, "tasks": tasks})
    sup.iterations_used = 1
    return sup, asyncio.run(sup._run_job_core())


def _log_messages(root: Path) -> str:
    return "\n".join(ev.get("message") or "" for ev in _events(root)
                     if ev.get("type") == "log")


def test_the_schema_still_rejects_an_unrecognised_verdict_value():
    from ralphd.engine import contracts

    assert contracts.validate_payload("worker", {"verdict": "complete"}) == []
    assert contracts.validate_payload("worker", {"verdict": "give_up"}) != []


def test_an_unrecognised_verdict_value_exhausts_the_ladder_and_still_refuses_the_move(
        tmp_path):
    """Site 5's own version of tasks 005/006's exhaustion test: a worker
    payload whose `verdict` the schema rejects outright (never
    `"unsatisfiable"` or `"complete"`, so neither of those two branches
    above this one ever fires) is carried back through
    `contract_enforcement.enforce_contract`'s SAME re-ask ladder those
    sites already use, not a single validate-or-abort check.

    The move is refused (the task record never moves off `in-progress` --
    there was nothing legal to apply in the first place, and nothing was);
    the ladder ran the FULL `contract_retry_max + 1` attempts (every call,
    the one charged `run_iteration` plus every uncharged `_ask_contract_fix`
    re-ask, lands on the SAME `ScriptedRunner.run` (tests/conftest.py), so
    this call count IS the ladder's own attempt count directly);
    `sup._abort_reason` stays `None` and no "contract violation" log
    message is emitted; and the run continues per the table (`T("worker",
    "no_verdict_yet")`, the undeclared self-loop -- no `_advance` call at
    all, exactly like an ordinary `"no_verdict"` reply) rather than
    aborting outright. With the run's own iteration budget already spent
    by the one charged attempt below (`iterations=2`, one already seeded
    used), the run ends `"failed"` (unverified, no abort) via the ordinary
    tail, never `"aborted"`.
    """
    root = tmp_path / "run"
    tasks = [_task(status="in-progress")]

    sup, state = _run_resumed_schema_error(root, {
        "worker": [lambda s, p: _bad_verdict()],
    }, tasks)

    # the move is still refused -- the task record is untouched.
    task = sup.run.read_tasks()["tasks"][0]
    assert task["status"] == "in-progress", \
        "a contract violation must not silently take the move anyway"

    # the ladder ran the FULL contract_retry_max + 1 attempts.
    assert sup.runner.calls["worker"] == sup.cfg.contract_retry_max + 1  # type: ignore[attr-defined]

    events = _events(root)
    violation_events = [ev for ev in events if ev.get("type") == "contract.violation"]
    fault_events = [ev for ev in events if ev.get("type") == "contract.agent_fault"]
    assert len(violation_events) == sup.cfg.contract_retry_max + 1
    assert len(fault_events) == 1, "the ladder must be exhausted exactly once"

    # agent-fault continuation, NEVER a contract-violation abort: no
    # `_abort_reason`, no "contract violation" log message, and the run
    # ends `"failed"` (budget genuinely exhausted), not `"aborted"`.
    assert sup._abort_reason is None
    msgs = _log_messages(root)
    assert "contract violation" not in msgs
    assert state == "failed", sup.run.read_status()


# ===========================================================================
# task 001 (#119, requirement A.1): `_verify_task`'s own ladder.
# ===========================================================================

TASK = {
    "id": "007",
    "title": "the task under verification",
    "status": "completed",
    "successCriteria": "the thing works",
    "validationAttempts": 1,
    "validationNotes": "first round: the CLI flag was missing (worker's note)",
}


_supervisor_verify_ladder = supervisor_with_runner(
    lambda sup, results: _SequencedRunner(results),
    run_id="task001-verify-ladder", iterations=8, max_approaches=1, vigilant=True,
    on_complete="exit", infra_retry_backoff_s=[0.0], infra_retry_backoff_max_s=0.0,
    infra_outage_budget_s=0.0, tasks=[dict(TASK)])


# -- 1. a prose final line is repaired, no attempt spent -------------------


def test_a_prose_final_line_is_repaired_on_reask_leaving_attempts_unchanged(
        tmp_path):
    prose = "let me check the criteria... yes, this looks complete to me"
    corrected = json.dumps({"task_id": TASK["id"], "verdict": "verified"})

    sup = _supervisor_verify_ladder(tmp_path, [_ok(prose), _ok(corrected)])

    verified = asyncio.run(sup._verify_task(dict(TASK)))

    assert verified is True
    t = json.loads(sup.run.tasks_file.read_text())["tasks"][0]
    assert t["status"] == "validated"
    # never touched -- the re-ask fixed the FIRST attempt's own reply, so
    # no validation-failed round was ever recorded against this task.
    assert t["validationAttempts"] == TASK["validationAttempts"]
    assert t["validationNotes"] == TASK["validationNotes"]

    # the ladder's re-ask prompt states the specific parse failure, not a
    # generic complaint -- mirrors the worker path's own re-ask pinning.
    assert sup.runner.calls == 2
    reask_prompt = sup.runner.prompts[1]
    assert "malformed JSON" in reask_prompt


def test_a_task_id_naming_the_wrong_task_is_repaired_on_reask(tmp_path):
    wrong_task = json.dumps({"task_id": "999", "verdict": "verified"})
    corrected = json.dumps({"task_id": TASK["id"], "verdict": "verified"})

    sup = _supervisor_verify_ladder(tmp_path, [_ok(wrong_task), _ok(corrected)])

    verified = asyncio.run(sup._verify_task(dict(TASK)))

    assert verified is True
    t = json.loads(sup.run.tasks_file.read_text())["tasks"][0]
    assert t["status"] == "validated"
    assert t["validationAttempts"] == TASK["validationAttempts"]

    reask_prompt = sup.runner.prompts[1]
    assert "999" in reask_prompt
    assert "the one under verification" in reask_prompt


# -- 2. the re-ask is uncharged and lands under byPhase.contract -----------


def test_the_reask_charges_no_iteration_and_is_recorded_under_contract_phase(
        tmp_path):
    prose = "still reading the diff"
    corrected = json.dumps({"task_id": TASK["id"], "verdict": "verified"})

    sup = _supervisor_verify_ladder(tmp_path, [_ok(prose), _ok(corrected)])

    iterations_before = sup.iterations_used
    verified = asyncio.run(sup._verify_task(dict(TASK)))
    assert verified is True

    # exactly one CHARGED iteration for the whole episode -- the re-ask
    # never opens a second iteration slot.
    assert sup.iterations_used == iterations_before + 1
    iteration_dirs = sorted(p.name for p in (sup.run.root / "iterations").iterdir())
    assert len(iteration_dirs) == 1

    status = sup.run.read_status()
    by_phase = status.get("usage", {}).get("byPhase", {})
    assert "contract" in by_phase, by_phase
    assert by_phase["contract"].get("totalTokens", 0) > 0

    # exactly one contract-violation event recorded for the repaired attempt
    events = _events(sup.run.root)
    violations = [e for e in events if e.get("type") == "contract.violation"]
    faults = [e for e in events if e.get("type") == "contract.agent_fault"]
    assert len(violations) == 1
    assert faults == []


# -- 3. ladder exhaustion still produces today's outcome --------------------


def test_ladder_exhaustion_still_produces_the_verdict_miss_validation_failed_outcome(
        tmp_path):
    """The hat never produces a valid payload, across every attempt the
    ladder grants (`contract_retry_max + 1`, default 3) -- exactly the
    pre-task-001 outcome: one validation-failed round recorded, with the
    generic "no valid contract payload" note."""
    prose = "still not sure, let me look again"

    sup = _supervisor_verify_ladder(tmp_path, [_ok(prose)])   # every call replays `prose`

    task = dict(TASK)
    task["validationNotes"] = ""
    atomic_write_json(sup.run.tasks_file, {"version": 1, "tasks": [task]})

    verified = asyncio.run(sup._verify_task(dict(task)))

    assert verified is False
    t = json.loads(sup.run.tasks_file.read_text())["tasks"][0]
    assert t["status"] == "validation-failed"
    assert t["validationAttempts"] == TASK["validationAttempts"] + 1
    notes = t["validationNotes"]
    assert "contract payload" in notes
    assert "verdict" in notes.lower()

    # the ladder made its full 1 + contract_retry_max attempts (1 reused +
    # `contract_retry_max` uncharged re-asks) before giving up -- still only
    # ONE charged iteration.
    assert sup.runner.calls == 1 + sup.cfg.contract_retry_max
    assert sup.iterations_used == 1

    events = _events(sup.run.root)
    faults = [e for e in events if e.get("type") == "contract.agent_fault"]
    assert len(faults) == 1
    assert faults[0]["hat"] == "verify"


# ===========================================================================
# task 002 (requirement A.2, #119): the widened `_review_contract_ladder`
# entry condition, through a real `_step_review()`.
# ===========================================================================

_supervisor_review_ladder = supervisor_with_runner(
    lambda sup, results: _SequencedRunner(results),
    run_id="task002-review-ladder", run_root="run", workspace="run",
    prd="# test PRD\n\nDo the thing.\n", status={"state": "running"}, approach=1,
    max_approaches=3, vigilant=True, on_complete="exit", infra_retry_backoff_s=[0.0],
    infra_retry_backoff_max_s=0.0, infra_outage_budget_s=0.0)


# -- 1. a prose final line is repaired, no archive/composite PRD -----------


def test_a_prose_final_line_is_repaired_on_reask_and_never_archives_or_composites(
        tmp_path):
    prose = "looks solid to me, I think we're genuinely done here"
    corrected = json.dumps({"verdict": "verified"})

    sup = _supervisor_review_ladder(tmp_path, [_ok(prose), _ok(corrected)])
    root = sup.run.root

    outcome = asyncio.run(sup._step_review())

    # the repaired reply reaches the ordinary VERIFIED terminal exit --
    # never a rejection.
    assert outcome == ("done", "succeeded")
    # exactly one charged review iteration plus one uncharged re-ask.
    assert sup.runner.calls == 2
    reask_prompt = sup.runner.prompts[1]
    assert "review" in reask_prompt.lower()

    # neither consequence of a rejection ever fires for a repaired verdict.
    assert not (root / "approaches" / "01").exists()
    assert not sup.run.composite_prd_file.exists()


# -- 2. exhaustion still reaches the ordinary archive/replan path ----------


def test_ladder_exhaustion_still_archives_and_writes_the_composite_prd(
        tmp_path):
    """The hat never produces a valid `review` payload across every
    attempt the ladder grants -- exactly the pre-task-002 outcome for an
    unparseable reply: the approach is archived, a composite PRD is
    written for the next one, and the ladder made its full
    `1 + contract_retry_max` attempts (1 reused + `contract_retry_max`
    uncharged re-asks) before giving up."""
    prose = "still not sure, let me look again"

    sup = _supervisor_review_ladder(tmp_path, [_ok(prose)])   # every call replays `prose`
    root = sup.run.root

    outcome = asyncio.run(sup._step_review())

    assert outcome == ("advance", "replan", "rejected_approach_left")
    assert sup.runner.calls == 1 + sup.cfg.contract_retry_max

    # today's ordinary archive-with-safe-default consequences, unchanged.
    assert (root / "approaches" / "01").exists()
    assert sup.run.composite_prd_file.exists()


# ===========================================================================
# task 003 (requirement A.3, #119): the same ladder pushed INSIDE
# `_verdict_gate_verdict` itself, through a real `_step_verdict_gate()`.
# ===========================================================================

_supervisor_verdict_gate_ladder = supervisor_with_runner(
    lambda sup, results: _SequencedRunner(results),
    run_id="task003-verdict-gate-ladder", run_root="run", workspace="run",
    prd="# test PRD\n\nDo the thing.\n", status={"state": "running"}, approach=1,
    iterations=10, max_approaches=2, vigilant=False, on_complete="exit",
    infra_retry_backoff_s=[0.0], infra_retry_backoff_max_s=0.0, infra_outage_budget_s=0.0,
    peer_verdict_gate=True)


# -- 1. a prose final line is repaired, no archive/composite PRD -----------


def test_a_prose_verdict_gate_final_line_is_repaired_on_reask_and_spends_no_approach(
        tmp_path):
    prose = "yep, this genuinely looks fully verified to me"
    corrected = json.dumps({"verdict": "verified"})

    sup = _supervisor_verdict_gate_ladder(tmp_path, [_ok(prose), _ok(corrected)])
    root = sup.run.root

    outcome = asyncio.run(sup._step_verdict_gate())

    # the repaired reply reaches the ordinary approved terminal exit.
    assert outcome == ("done", "succeeded")
    # exactly one charged verdict_gate iteration plus one uncharged re-ask.
    assert sup.runner.calls == 2
    reask_prompt = sup.runner.prompts[1]
    assert "verdict_gate" in reask_prompt.lower()

    # neither consequence of a rejection ever fires for a repaired verdict.
    assert not (root / "approaches" / "01").exists()
    assert not sup.run.composite_prd_file.exists()
    assert sup.run.read_status().get("verdict") == "verified"


# -- 2. exhaustion still keeps today's safe default ------------------------


def test_ladder_exhaustion_on_a_prose_reply_keeps_todays_safe_default(tmp_path):
    """The hat never produces a valid `verdict_gate` payload across every
    attempt the ladder grants -- exactly the pre-task-003 outcome for an
    unparseable reply: the approach is archived, a composite PRD is
    written (no approach left to try again), and the ladder made its full
    `1 + contract_retry_max` attempts (1 reused + `contract_retry_max`
    uncharged re-asks) before giving up."""
    prose = "hard to say, would need another look at this"

    sup = _supervisor_verdict_gate_ladder(tmp_path, [_ok(prose)], max_approaches=1)
    root = sup.run.root

    outcome = asyncio.run(sup._step_verdict_gate())

    # no approach left -> fail-open to finishing, never a deadlocked run.
    assert outcome == ("tail",)
    assert sup.runner.calls == 1 + sup.cfg.contract_retry_max

    # today's ordinary archive-with-safe-default consequences, unchanged.
    assert (root / "approaches" / "01").exists()
    assert sup.run.composite_prd_file.exists()
