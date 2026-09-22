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

from ralphd.engine import findings, petitions, state, transitions
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
    # `tests/test_worker_hat.py`'s own
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
    used), the run ends `timed_out`/`verdict: null` (task 011, v0.24
    requirement C.1, #169 -- budget genuinely exhausted, the plain shape)
    via the ordinary tail, never `"failed"`/`"aborted"`.
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
    # `_abort_reason`, no "contract violation" log message. Task 011
    # (v0.24 requirement C.1, #169): the run ends `timed_out`/`verdict:
    # null` (budget genuinely exhausted, the plain shape) through the
    # same `_finish_timed_out()` path task 010 established, never
    # `"failed"`/`"aborted"`.
    assert sup._abort_reason is None
    msgs = _log_messages(root)
    assert "contract violation" not in msgs
    assert state == "timed_out", sup.run.read_status()
    assert sup.run.read_status()["verdict"] is None


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
    generic "no valid contract payload" note.

    GH #209 defect 2 changed review's answer to this same question and
    deliberately did NOT change verify's: an exhausted `_verify_contract_
    ladder` still produces a substantive negative verdict (`validation-
    failed` plus one `validationAttempts`), while an exhausted
    `_review_contract_ladder` that never got a readable verdict claim now
    holds for an operator (see `test_ladder_exhaustion_holds_for_an_
    operator_and_never_archives` below, in this same module). The
    asymmetry is a recorded decision, not an oversight: verify's exhaustion
    burns a `validationAttempts` slot on ONE task rather than an approach --
    a different currency, with its own three-strike ladder behind it -- so
    it is a different fix and #209 does not attempt it. Nothing in this
    node's assertions changes."""
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


# -- 2. exhaustion holds for an operator; it never archives ----------------


def test_ladder_exhaustion_holds_for_an_operator_and_never_archives(
        tmp_path):
    """The hat never produces a valid `review` payload across every
    attempt the ladder grants, and the ladder made its full
    `1 + contract_retry_max` attempts (1 reused + `contract_retry_max`
    uncharged re-asks) before giving up.

    Under its previous name -- `test_ladder_exhaustion_still_archives_and_
    writes_the_composite_prd` -- this node deliberately pinned the OPPOSITE
    outcome, "exactly the pre-task-002 outcome for an unparseable reply:
    the approach is archived, a composite PRD is written for the next one".
    GH #209 defect 2 is that this was the defect: a reviewer that never
    produced a parseable verdict at all was scored exactly like a reviewer
    that read the work and refused it, so a pure schema slip
    (`$.verdict: 'fail' is not one of ['verified', 'rejected']`) cost the
    reference run an approach, archived it and wrote a composite PRD for a
    successor that need not exist. A hat that never returned a verdict has
    not returned one, so the run now HOLDS for an operator on the existing
    pause machinery over the same unarchived approach.

    The two assertions the old outcome did not itself dictate survive
    verbatim: the full attempt count, and the `contract.agent_fault` that
    witnesses the exhaustion (`enforce_contract` already emitted it -- #209
    invents no event for this). Everything else is the new outcome. This
    node's own unbounded drive of the review step is kept exactly as it
    was, so `tests/test_loop_driving_await_guard.py`'s pinned count of 4
    grandfathered loop-driving calls for this file stays exact -- every
    drive #209 ADDS below is wait_for-bounded.
    """
    prose = "still not sure, let me look again"

    sup = _supervisor_review_ladder(tmp_path, [_ok(prose)])   # every call replays `prose`
    root = sup.run.root

    outcome = asyncio.run(sup._step_review())

    assert outcome == ("advance", "paused", "review_contract_unrepaired")
    assert sup.runner.calls == 1 + sup.cfg.contract_retry_max

    # neither consequence of a rejection fires: nothing is archived, no
    # composite PRD is written, and the approach counter never moves.
    assert not (root / "approaches" / "01").exists()
    assert not sup.run.composite_prd_file.exists()
    status = sup.run.read_status()
    assert status["state"] == "paused"
    assert status["pauseReason"] == "review_contract_unrepaired"
    assert sup._approach == 1          # the approach counter never moved

    events = _events(root)
    faults = [e for e in events if e.get("type") == "contract.agent_fault"]
    assert len(faults) == 1 and faults[0]["hat"] == "review", faults
    escalations = [e for e in events if e.get("type") == "operator_escalation"]
    assert len(escalations) == 1, escalations
    assert escalations[0]["reason"] == "review_contract_unrepaired"
    # the validation errors reach the operator verbatim -- the whole
    # diagnosis, and unreachable otherwise without reading events.jsonl.
    assert escalations[0]["contractErrors"], escalations[0]
    assert any("contract payload" in err
               for err in escalations[0]["contractErrors"])
    assert "review" in escalations[0]["escalation"]
    # the judge never spoke, so the recorded approach verdict says so --
    # never `rejected`, and no `review rejected approach` log line at all.
    verdicts = [e for e in events if e.get("type") == "approach.verdict_recorded"]
    assert [(e["channel"], e["verdict"]) for e in verdicts] == \
        [("review", "unreviewed")], verdicts
    assert not [e for e in events if e.get("type") == "log"
                and "review rejected approach" in (e.get("message") or "")]


def test_a_legible_rejected_verdict_with_a_schema_slip_elsewhere_now_pauses(
        tmp_path):
    """Task 012 (PRD requirement C.3, #219): this node, inverted.

    Under its previous name -- `test_a_legible_rejected_verdict_with_a_
    schema_slip_elsewhere_still_rejects` -- it pinned GH #209 defect 2's
    own boundary: `HAT_SCHEMAS["review"]` is `additionalProperties:
    False`, so one stray inline key (`confidence` here -- among the most
    common slips a hat makes against a closed object) is a schema error,
    and a hat stubborn enough to repeat its own shape on every re-ask
    exhausts the ladder over it; because the judge nevertheless said
    `rejected` for a stated reason, #209 scored that an ordinary,
    substantive rejection -- the approach archived, a composite PRD
    written for a successor that need not exist.

    Requirement C supersedes exactly that outcome, and inverting this pin
    is the contract's own decision rather than this test's: #219 names "a
    stray inline key" as the fourth of the four shapes whose exhausted
    review ladder "would still cost an approach today", alongside the
    truncated sidecar, the `verified` claim contradicting a blocking
    finding and the curable finding with no `cured_when`. The reasoning
    #209 used for its own half still holds and is what carries the
    inversion: a legible verdict means the judge DID speak, so the run
    still records `rejected` and never `unreviewed` -- but nothing inside
    the run can synthesize a usable review CONTRACT out of a payload the
    ladder could not repair, so the only remaining edge is a hold for an
    operator rather than spending an approach on a reviewer whose channel
    is broken structurally. The outcome therefore flips to the eighth
    `pauseReason`, `review_findings_unusable`; `tests/test_run_record.py::
    test_review_verdict_now_pauses_when_the_sidecar_does_not_validate` is
    this node's inverted sibling, for the sidecar half of the same ruling.

    What does NOT move, and is asserted below so the narrowing #209
    measured stays pinned: `_review_contract_unrepaired` stays False (this
    verdict claim was legible -- an unreadable one keeps its own distinct
    `review_contract_unrepaired` hold, recorded `unreviewed`, see
    `test_ladder_exhaustion_holds_for_an_operator_and_never_archives`
    above), the recorded approach verdict stays `rejected` with the hat's
    own words, and the hold still fires only on an EXHAUSTED ladder rather
    than on "the payload failed its schema" per se -- a slip the ladder
    repairs on a re-ask reaches the ordinary outcome untouched
    (`test_a_prose_final_line_is_repaired_on_reask_and_never_archives_or_
    composites` above).

    The two deliberate-asymmetry pins of this module stay as they are,
    requirement C being review-only: verify's `test_ladder_exhaustion_
    still_produces_the_verdict_miss_validation_failed_outcome` and the
    verdict gate's `test_ladder_exhaustion_on_a_prose_reply_keeps_todays_
    safe_default`."""
    reply = json.dumps({"verdict": "rejected",
                        "reason": "the deck never renders",
                        "confidence": 0.9})

    sup = _supervisor_review_ladder(tmp_path, [_ok(reply)])
    root = sup.run.root

    outcome = asyncio.run(asyncio.wait_for(sup._step_review(), 30.0))

    # the ladder genuinely exhausted -- the full attempt count and the
    # `contract.agent_fault` are both here -- and the outcome is now the
    # discriminating hold, with every rejection consequence withheld.
    assert sup.runner.calls == 1 + sup.cfg.contract_retry_max
    events = _events(root)
    faults = [e for e in events if e.get("type") == "contract.agent_fault"]
    assert len(faults) == 1 and faults[0]["hat"] == "review", faults
    assert outcome == ("advance", "paused", "review_findings_unusable")
    status = sup.run.read_status()
    assert status["state"] == "paused"
    assert status["pauseReason"] == "review_findings_unusable"
    # the approach survives: nothing archived, no composite PRD written
    # for a successor, and the counter never moved.
    assert not (root / "approaches" / "01").exists()
    assert not sup.run.composite_prd_file.exists()
    assert sup._approach == 1
    # which flag fired, and which deliberately did not: this verdict claim
    # was legible, so the `review_contract_unrepaired` hold -- whose own
    # record says the judge never spoke -- is NOT the edge taken here.
    assert sup._review_findings_unusable is True
    assert sup._review_contract_unrepaired is False
    escalations = [e for e in events if e.get("type") == "operator_escalation"]
    assert len(escalations) == 1, escalations
    assert escalations[0]["reason"] == "review_findings_unusable"
    # the judge DID speak, so the recorded approach verdict is still its
    # own `rejected` -- never `unreviewed`, which is the half of #209's
    # narrowing requirement C does not touch.
    verdicts = [e for e in events if e.get("type") == "approach.verdict_recorded"]
    assert [(e["channel"], e["verdict"]) for e in verdicts] == \
        [("review", "rejected")], verdicts
    # and the hat's own words are still what the approach's metadata carries.
    assert state.approach_metadata_channel(root, 1, "review")["reason"] == \
        "the deck never renders"


def test_the_second_ladder_flag_separates_unusable_findings_from_an_unreadable_verdict(
        tmp_path):
    """Task 010 (requirement C.1, #219): `_review_contract_ladder` now
    computes a SECOND flag (`self._review_findings_unusable`) alongside
    `self._review_contract_unrepaired` -- one for a legible verdict claim
    whose FINDINGS CHANNEL is what's unusable, the other for a verdict
    claim that was never legible at all -- and `_review_verdict` reads
    both (the new invariant assertions right where it already read the
    first one).

    Task 011 (requirement C.2, #219) gave the first flag its own pause
    edge (`review_findings_unusable`), so scenario 1 below now lands on
    the SAME kind of hold `review_contract_unrepaired` always has, rather
    than the ordinary rejection ladder it rode under task 010 alone --
    see `test_review_findings_unusable_pauses_and_never_archives_or_cures`
    below for the full pause-shape assertions (state, pauseReason, no
    approaches dir, approach counter, no cure pass) across the three
    named unusable-findings shapes. This node keeps its original job: pin
    that the two flags stay mutually exclusive and each fires only on its
    own scenario -- an unreadable verdict claim still holds on
    `review_contract_unrepaired`
    (`test_ladder_exhaustion_holds_for_an_operator_and_never_archives`
    above), never on the sibling edge.
    """
    legible_rejected = json.dumps({"verdict": "rejected",
                                   "reason": "the deck never renders"})

    # -- scenario 1: a legible verdict, an unusable (present-but-invalid)
    # findings sidecar -- one of requirement C's three named shapes ("a
    # truncated sidecar"), the same fixture `tests/test_run_record.py::
    # test_review_verdict_is_persisted_when_the_sidecar_does_not_validate`
    # uses for the identical channel state.
    sup = _supervisor_review_ladder(tmp_path / "findings-unusable",
                                    [_ok(legible_rejected)])
    root = sup.run.root
    sup.run.findings_json_file.write_text('[{"severity": "blocking"}]')

    outcome = asyncio.run(asyncio.wait_for(sup._step_review(), 30.0))

    # task 011: now the discriminating pause, not the ordinary rejection.
    assert outcome == ("advance", "paused", "review_findings_unusable")
    assert not (root / "approaches" / "01").exists()
    assert not sup.run.composite_prd_file.exists()
    # what's new (task 010): the discriminating flag is set, its sibling is not.
    assert sup._review_findings_unusable is True
    assert sup._review_contract_unrepaired is False

    # -- scenario 2: the mirror case -- an unreadable verdict claim sets
    # the ORIGINAL flag and leaves the new one False. Same prose reply
    # `test_ladder_exhaustion_holds_for_an_operator_and_never_archives`
    # drives, in a supervisor of its own so the two scenarios never share
    # a run directory.
    prose = "still not sure, let me look again"
    sup2 = _supervisor_review_ladder(tmp_path / "verdict-unreadable",
                                     [_ok(prose)])

    outcome2 = asyncio.run(asyncio.wait_for(sup2._step_review(), 30.0))

    assert outcome2 == ("advance", "paused", "review_contract_unrepaired")
    assert sup2._review_contract_unrepaired is True
    assert sup2._review_findings_unusable is False


def test_review_findings_unusable_pauses_and_never_archives_or_cures(tmp_path):
    """Task 011 (PRD requirement C.2, #219): the pause edge this task adds
    (`review -> paused` on `review_findings_unusable`), driven through a
    real `_step_review()` over each of the THREE named shapes requirement
    C draws the flag from (`LoopSupervisor._review_contract_ladder`'s own
    `findings_channel_unusable_reply`): a present-but-invalid (truncated)
    sidecar, a `verified` claim contradicting a blocking finding, and a
    curable finding with no `cured_when`. Every shape must leave the SAME
    fingerprint: `state: "paused"` with `pauseReason: "review_findings_
    unusable"`, no `approaches/NN/` directory written, no composite PRD,
    the approach counter unchanged, and no cure pass opened (neither
    `curePasses` on status.json nor an `approach.cure_pass` event) --
    exactly the consequences `test_ladder_exhaustion_holds_for_an_operator_
    and_never_archives` above already pins for the sibling edge.
    """
    legible_rejected = json.dumps({"verdict": "rejected",
                                   "reason": "the deck never renders"})

    def _assert_paused_with_no_side_effects(sup, root):
        outcome = asyncio.run(asyncio.wait_for(sup._step_review(), 30.0))
        assert outcome == ("advance", "paused", "review_findings_unusable")
        assert sup._review_findings_unusable is True
        assert sup._review_contract_unrepaired is False
        assert not (root / "approaches" / "01").exists()
        assert not sup.run.composite_prd_file.exists()
        status = sup.run.read_status()
        assert status["state"] == "paused"
        assert status["pauseReason"] == "review_findings_unusable"
        assert sup._approach == 1          # the approach counter never moved
        assert not status.get("curePasses")
        events = _events(root)
        assert not [e for e in events if e.get("type") == "approach.cure_pass"]
        escalations = [e for e in events if e.get("type") == "operator_escalation"]
        assert len(escalations) == 1, escalations
        assert escalations[0]["reason"] == "review_findings_unusable"
        assert "review" in escalations[0]["escalation"]
        verdicts = [e for e in events if e.get("type") == "approach.verdict_recorded"]
        assert [(e["channel"], e["verdict"]) for e in verdicts] == \
            [("review", "rejected")], verdicts
        return outcome

    # -- shape 1: a present-but-invalid (truncated) sidecar.
    sup1 = _supervisor_review_ladder(tmp_path / "shape-truncated-sidecar",
                                     [_ok(legible_rejected)])
    sup1.run.findings_json_file.write_text('[{"severity": "blocking"}]')
    _assert_paused_with_no_side_effects(sup1, sup1.run.root)

    # -- shape 2: a `verified` claim contradicting a blocking finding.
    legible_verified = json.dumps({"verdict": "verified"})
    sup2 = _supervisor_review_ladder(tmp_path / "shape-verified-contradiction",
                                     [_ok(legible_verified)])
    findings.write_findings_json(sup2.run.findings_json_file, [
        {"severity": "blocking", "curable": False,
         "clause": "requirement C", "evidence": "still broken"}])
    outcome2 = asyncio.run(asyncio.wait_for(sup2._step_review(), 30.0))
    assert outcome2 == ("advance", "paused", "review_findings_unusable")
    assert sup2._review_findings_unusable is True
    assert sup2._review_contract_unrepaired is False
    root2 = sup2.run.root
    assert not (root2 / "approaches" / "01").exists()
    assert not sup2.run.composite_prd_file.exists()
    status2 = sup2.run.read_status()
    assert status2["state"] == "paused"
    assert status2["pauseReason"] == "review_findings_unusable"
    assert sup2._approach == 1
    assert not status2.get("curePasses")
    events2 = _events(root2)
    assert not [e for e in events2 if e.get("type") == "approach.cure_pass"]
    # the claimed verdict was `verified`, but `_review_verdict`'s own
    # ternary only reads `_review_contract_unrepaired` (False here -- the
    # claim itself was legible) to pick `unreviewed`, so the recorded
    # approach verdict is still `rejected`, exactly like the other two
    # shapes -- never `verified` (the invariant assertion in `_review_
    # verdict` refuses that combination outright).
    verdicts2 = [e for e in events2 if e.get("type") == "approach.verdict_recorded"]
    assert [(e["channel"], e["verdict"]) for e in verdicts2] == \
        [("review", "rejected")], verdicts2

    # -- shape 3: a curable blocking finding with no `cured_when`.
    sup3 = _supervisor_review_ladder(tmp_path / "shape-missing-cured-when",
                                     [_ok(legible_rejected)])
    findings.write_findings_json(sup3.run.findings_json_file, [
        {"severity": "blocking", "curable": True,
         "clause": "requirement C", "evidence": "still broken"}])
    _assert_paused_with_no_side_effects(sup3, sup3.run.root)


# -- 3. Half B: the negative-verdict synonym, and its one-directionality ---


def test_a_fail_verdict_is_normalised_to_rejected_without_a_single_reask(
        tmp_path):
    """GH #209 defect 2 (Half B). The reference run's own reply. `fail` is
    not in `HAT_SCHEMAS["review"]`'s two-word enum, so it used to cost
    three attempts, three `contract.violation`s, one `contract.agent_fault`
    and -- once Half A landed -- would have cost a PAUSE, which is strictly
    worse than the rejection it plainly meant. It is now normalised ahead
    of the ladder's own early-exit check, so it never enters
    `enforce_contract` at all and becomes a real, fully-furnished
    rejection in ONE runner call."""
    reply = json.dumps({"verdict": "fail", "reason": "the deck never renders"})

    sup = _supervisor_review_ladder(tmp_path, [_ok(reply)])
    root = sup.run.root

    outcome = asyncio.run(asyncio.wait_for(sup._step_review(), 30.0))

    assert outcome == ("advance", "replan", "rejected_approach_left")
    assert sup.runner.calls == 1          # no re-ask at all
    events = _events(root)
    assert not [e for e in events if e.get("type") == "contract.violation"]
    assert not [e for e in events if e.get("type") == "contract.agent_fault"]
    # the audit trail keeps what the hat LITERALLY said.
    normalised = [e for e in events
                  if e.get("type") == "review.verdict_synonym_normalised"]
    assert len(normalised) == 1, normalised
    assert normalised[0]["claimed_verdict"] == "fail"
    assert normalised[0]["normalised_verdict"] == "rejected"
    assert normalised[0]["approach"] == 1
    # a real rejection, not a hold: archive, composite PRD, and the hat's
    # own words survive into the approach's own metadata.
    verdicts = [e for e in events if e.get("type") == "approach.verdict_recorded"]
    assert [e["verdict"] for e in verdicts] == ["rejected"], verdicts
    assert (root / "approaches" / "01").exists()
    assert sup.run.composite_prd_file.exists()
    entry = state.approach_metadata_channel(root, 1, "review")
    assert entry["reason"] == "the deck never renders"


def test_a_pass_verdict_is_never_normalised_toward_success(tmp_path):
    """GH #209 defect 2 (Half B) safety property: the synonym map is
    one-directional by construction -- no entry maps to `verified`, so a
    wrong guess can only ever land on the NEGATIVE word, never straight on
    the one `_step_review` turns into `_finish("succeeded", ...)`. `pass` is
    therefore a coercion-proof violation -- which is also the second test
    #92's requirement F2 attaches to any commit that does add a coercion:
    it rides the full ladder to exhaustion and lands on the hold, never on
    `("done", "succeeded")`. What the negative word then INHERITS is the
    sibling node below (`test_a_normalised_fail_inherits_the_all_residual_
    recomputation`), which is where the boundary of this property actually
    lies."""
    reply = json.dumps({"verdict": "pass", "reason": "looks good"})

    sup = _supervisor_review_ladder(tmp_path, [_ok(reply)])
    root = sup.run.root

    outcome = asyncio.run(asyncio.wait_for(sup._step_review(), 30.0))

    assert outcome == ("advance", "paused", "review_contract_unrepaired")
    assert sup.runner.calls == 1 + sup.cfg.contract_retry_max
    events = _events(root)
    assert not [e for e in events
                if e.get("type") == "review.verdict_synonym_normalised"]
    faults = [e for e in events if e.get("type") == "contract.agent_fault"]
    assert len(faults) == 1 and faults[0]["hat"] == "review"
    assert "VERIFIED" not in [e.get("signal") for e in events
                              if e.get("type") == "signal"]
    assert sup.run.read_status().get("verdict") is None
    # and a future entry cannot quietly reopen the hole.
    assert "verified" not in transitions.NEGATIVE_VERDICT_SYNONYMS.values()


def test_a_normalised_fail_inherits_the_all_residual_recomputation(tmp_path):
    """GH #209 defect 2 (Half B): the exact boundary of the node above, and
    a stated decision rather than a surprise. "One-directional" is a
    property of the MAP, not a promise about the run's outcome: a rewritten
    payload then receives precisely the treatment a LITERAL `rejected`
    receives, and that includes `_review_verdict`'s recorded correction of
    an over-cautious refusal -- a `rejected` claim whose structured findings
    are ALL `severity: "residual"` is recomputed to `verified` (task 002's
    DoD predicate 3, witnessed by `review.verdict_recomputed`). So a hat
    that writes `fail` while filing only residual findings does reach
    `succeeded`, by that route, on its own findings, exactly as it would
    have with the compliant word.

    Pinned deliberately, in both directions: making `fail` BINDING where
    `rejected` is correctable would give the contract a second, undeclared
    verdict vocabulary in which the illegal word outranks the legal one, and
    silently dropping the recomputation for a normalised payload would be
    that change made by accident."""
    reply = json.dumps({"verdict": "fail", "reason": "the deck never renders"})

    sup = _supervisor_review_ladder(tmp_path, [_ok(reply)])
    root = sup.run.root
    findings.write_findings_json(sup.run.findings_json_file, [
        {"severity": "residual", "clause": "requirement C",
         "evidence": "a cosmetic gap remains", "curable": False}])

    outcome = asyncio.run(asyncio.wait_for(sup._step_review(), 30.0))

    assert outcome == ("done", "succeeded")
    events = _events(root)
    # both rewrites are on the audit trail, in order: what the hat said,
    # then what its own findings said about what it said.
    normalised = [e for e in events
                  if e.get("type") == "review.verdict_synonym_normalised"]
    assert [(e["claimed_verdict"], e["normalised_verdict"])
            for e in normalised] == [("fail", "rejected")], normalised
    recomputed = [e for e in events
                  if e.get("type") == "review.verdict_recomputed"]
    assert [(e["claimed_verdict"], e["computed_verdict"])
            for e in recomputed] == [("rejected", "verified")], recomputed
    assert recomputed[0]["residual_count"] == 1
    verdicts = [e for e in events if e.get("type") == "approach.verdict_recorded"]
    assert [(e["channel"], e["verdict"]) for e in verdicts] == \
        [("review", "verified")], verdicts
    status = sup.run.read_status()
    assert status["state"] == "succeeded"
    # the residuals are named in the terminal verdict, never dropped.
    assert status["verdict"] == "verified-with-residuals"


def test_a_repaired_reask_clears_the_unrepaired_flag_for_the_next_pass(
        tmp_path):
    """GH #209 defect 2: the mutation-facing half of the flag's lifetime.
    `_review_contract_ladder` resets `_review_contract_unrepaired` at every
    ordinary entry, so a run whose reviewer has since started answering
    correctly leaves the hold instead of being stranded in it -- one
    pass's fault can never leak into the next."""
    prose = "still not sure, let me look again"
    good = json.dumps({"verdict": "rejected", "reason": "requirement B unmet"})

    sup = _supervisor_review_ladder(tmp_path, [_ok(prose)])
    root = sup.run.root

    first = asyncio.run(asyncio.wait_for(sup._step_review(), 30.0))
    assert first == ("advance", "paused", "review_contract_unrepaired")
    assert sup._review_contract_unrepaired is True

    # a working reviewer on the next pass, released the way an operator
    # releases it (`POST /resume`): the flag clears and the ordinary
    # rejection path runs, archive and composite PRD included.
    sup.runner.results = [_ok(good)]
    sup.resume()
    second = asyncio.run(asyncio.wait_for(sup._step_review(), 30.0))
    assert second == ("advance", "replan", "rejected_approach_left")
    assert sup._review_contract_unrepaired is False
    assert (root / "approaches" / "01").exists()
    verdicts = [e["verdict"] for e in _events(root)
                if e.get("type") == "approach.verdict_recorded"]
    assert verdicts == ["unreviewed", "rejected"], verdicts


def test_a_petition_adjudication_in_an_unrepairable_payload_is_still_recorded(
        tmp_path):
    """GH #209 defect 2: the hold branch sits AFTER `_review_verdict`, not
    in front of it, so task 011's own recorded decision survives -- a
    payload that is malformed as a VERDICT can still carry a perfectly
    valid CONFIRMED adjudication, and that adjudication must not be
    swallowed by the verdict's unusability. This is the node that goes red
    if the hold is ever "simplified" into an early return out of
    `_review_verdict`."""
    reply = json.dumps({
        "verdict": "fail-ish",          # un-normalisable: the ladder exhausts
        "reason": "cannot say",
        "petition_adjudications": [
            {"obligation": "task 001 must ship the widget",
             "adjudication": "CONFIRMED",
             "remedy": "the widget is genuinely impossible here"}]})

    sup = _supervisor_review_ladder(tmp_path, [_ok(reply)])
    root = sup.run.root

    outcome = asyncio.run(asyncio.wait_for(sup._step_review(), 30.0))

    assert outcome == ("advance", "paused", "review_contract_unrepaired")
    entries, tampered = petitions.read_confirmed_adjudications(sup.run)
    assert tampered is False
    assert [e.get("obligation") for e in entries] == \
        ["task 001 must ship the widget"], entries
    assert [e.get("type") for e in _events(root)].count(
        petitions.ADJUDICATIONS_RECORDED_EVENT) == 1


def test_a_grace_review_with_an_unrepaired_contract_is_unreviewed_not_rejected(
        tmp_path):
    """RETIRED (task 015, requirement E.1, #169): this pinned GH #209 defect
    2's OFF-budget half, driven directly through the now-removed
    `_maybe_grace_review`. The grace review (and its off-budget verdict
    path) no longer exists -- requirement A's contract-ladder fix and
    requirement D's own budget/deadline fixes already covered the root
    causes it existed to patch over. The ON-budget half this was a sibling
    of (`_run_review_iteration`'s own "unreviewed, not rejected" write for
    an unrepaired contract, exercised by the tests around line 807-926
    above) is unaffected and still covers the surviving mechanism; the
    `tests/test_deadline.py` sibling this docstring used to name is
    likewise retired.
    """
    assert not hasattr(LoopSupervisor, "_maybe_grace_review")


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
    uncharged re-asks) before giving up.

    GH #209 defect 2 changed review's answer to this same question and
    deliberately did NOT change the verdict gate's (see
    `test_ladder_exhaustion_holds_for_an_operator_and_never_archives`
    above, in this same module): `_verdict_gate_verdict` still returns its
    own `(False, None)` safe default and `_step_verdict_gate` still
    archives the approach and writes a composite PRD over it. This is a
    recorded decision, not an oversight -- and it is the higher-value
    follow-up of the two siblings, because the gate archives an approach on
    exhaustion exactly as review's did AND it is the terminal-success gate
    every self-dev run enables. It is a near-mechanical mirror of review's
    fix (one `T("paused", "verdict_gate_contract_unrepaired")` edge in
    `PEER_VERDICT_GATE_DELTA["verdict_gate"]`, a third piece of state out of
    `_verdict_gate_verdict`, one branch in `_step_verdict_gate`) and is out
    of #209's scope. Nothing in this node's assertions changes."""
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
