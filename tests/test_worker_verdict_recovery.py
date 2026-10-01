"""Task 005 (PRD v0.29 requirement C, #281): a worker reply whose last
non-blank line is not a verdict is repaired, never silently dropped.

Today (before this task) `_step_worker` reads `last_nonblank_line(result.
final_text)` and parses it opportunistically: a last line that is not JSON,
or a JSON object with no `verdict` key, falls through as no verdict with no
event at all. The repair channel (`_ask_contract_fix`, which already tries
`_extract_validated_fenced_payload` before any model call) was reachable
only from ladders this path never entered.

Three shapes, in this order:

1. **Fenced recovery first, no model call.** The iteration-22 shape: prose,
   then a fenced ```json block carrying a valid `{"verdict": ...}` payload.
   `_extract_validated_fenced_payload` finds and validates it; it is used
   exactly as if it had been the last line, and `contract.recovered` is
   emitted with `hat="worker"`, `source="fenced"`.
2. **Then the ladder, for verdict-shaped JSON.** The iteration-17 shape: a
   JSON object (not prose) that carries no valid `verdict` at all, e.g.
   `{"task": ..., "status": "complete"}`. No fenced payload validates, so
   the reply goes through the existing worker contract ladder
   (`contract_enforcement.enforce_contract` with `_ask_contract_fix` as
   `respond`), bounded by `contract_retry_max`.
3. **Prose stays prose.** Anything that is neither of those -- including
   the retired `<promise>COMPLETE</promise>` whole-plan marker -- makes no
   model call, never enters the ladder, and emits exactly one
   `worker.verdict_absent` event naming the task id, with no reply text.

Direct `_step_worker()` calls against a hand-built `LoopSupervisor`, the
style `tests/test_contract_events.py`'s `_worker_run` helper already uses --
no job-core drive, no review phase needed, since every case here resolves
(or fails to) within the worker step alone.
"""

from __future__ import annotations

import asyncio
import json
import tempfile
from pathlib import Path

import pytest

from ralphd.engine.config import JobConfig
from ralphd.engine.loop import LoopSupervisor
from ralphd.engine.runner import COMPLETE, IterationResult
from ralphd.engine.state import RunDir, atomic_write_json, read_events
from tests.conftest import supervisor_factory

TASK_1 = "001"


def _ok(text: str) -> IterationResult:
    r = IterationResult(exit_code=0, duration_s=1.0)
    r.final_text = text
    r.usage = {"input": 10, "output": 5, "totalTokens": 15}
    return r


class _ScriptedWorker:
    """Replays one `IterationResult` per `run()` call, in order (the last
    one repeats forever once the script runs out)."""

    def __init__(self, script: list[IterationResult]):
        self.script = script
        self.calls = 0

    def interrupt(self) -> bool:
        return False

    async def run(self, prompt, transcript, **kw) -> IterationResult:
        step = self.script[min(self.calls, len(self.script) - 1)]
        self.calls += 1
        return step


def _worker_run(tmp_path: Path, name: str, *, iterations: int = 1,
                vigilant: bool = True) -> tuple[LoopSupervisor, RunDir]:
    root = tmp_path / name
    root.mkdir(parents=True)
    run = RunDir(root=root)
    run.update_status(state="running")
    atomic_write_json(run.tasks_file, {"version": 1, "tasks": [
        {"id": TASK_1, "title": "do the thing", "status": "in-progress",
         "successCriteria": "the thing works"},
    ]})
    cfg = JobConfig(run_id=f"task005-{name}", iterations=iterations,
                    max_approaches=1, vigilant=vigilant, on_complete="exit",
                    infra_retry_backoff_s=[0.0], infra_retry_backoff_max_s=0.0,
                    infra_outage_budget_s=1000.0)
    sup = LoopSupervisor(cfg, run, root)
    sup._approach = 1
    return sup, run


def _events(run: RunDir, type_: str) -> list[dict]:
    return read_events(run.root, types=[type_])


# ---------------------------------------------------------------------------
# (i) the iteration-22 shape: prose, then a fenced valid verdict payload.
# ---------------------------------------------------------------------------

def test_fenced_recovery_reaches_awaiting_validation_with_no_model_call(tmp_path):
    sup, run = _worker_run(tmp_path, "fenced-recovery", vigilant=True)
    corrected = json.dumps({"verdict": "complete", "task_id": TASK_1})
    reply = (
        "I looked at the task and finished the work.\n"
        "Let me wrap this up with the verdict.\n\n"
        "```json\n" + corrected + "\n```\n"
    )
    agent = _ScriptedWorker([_ok(reply)])
    sup.runner = agent  # type: ignore[assignment]

    asyncio.run(sup._step_worker())

    tasks = run.read_tasks()["tasks"]
    assert tasks[0]["status"] == "awaiting-validation", tasks[0]

    # no model call beyond the one real (charged) worker iteration itself --
    # fenced-payload extraction is a pure regex + schema scan.
    assert agent.calls == 1

    recovered = _events(run, "contract.recovered")
    assert len(recovered) == 1, recovered
    assert recovered[0]["hat"] == "worker"
    assert recovered[0]["source"] == "fenced"
    assert recovered[0]["taskId"] == TASK_1

    assert _events(run, "worker.verdict_absent") == []
    assert _events(run, "contract.violation") == []
    assert _events(run, "contract.agent_fault") == []


# ---------------------------------------------------------------------------
# (ii) the iteration-17 shape: a verdict-less JSON object enters the ladder.
# ---------------------------------------------------------------------------

def test_iteration_17_shape_enters_the_ladder_and_a_stubbed_payload_is_applied(
        tmp_path):
    sup, run = _worker_run(tmp_path, "ladder-shape", vigilant=False)
    verdictless = json.dumps({"task": TASK_1, "status": "complete"})
    corrected = json.dumps({"verdict": "complete", "task_id": TASK_1})
    agent = _ScriptedWorker([_ok(verdictless), _ok(corrected)])
    sup.runner = agent  # type: ignore[assignment]

    asyncio.run(sup._step_worker())

    tasks = run.read_tasks()["tasks"]
    assert tasks[0]["status"] == "completed", tasks[0]

    # exactly the ladder's shape: the original attempt plus one re-ask.
    assert agent.calls == 2

    violations = _events(run, "contract.violation")
    assert len(violations) == 1, violations
    assert violations[0]["hat"] == "worker"

    assert _events(run, "contract.agent_fault") == []
    assert _events(run, "contract.recovered") == []
    assert _events(run, "worker.verdict_absent") == []


def test_an_unrepaired_iteration_17_shape_exhausts_the_ladder_safely(tmp_path):
    """Ladder exhaustion is read exactly like every other worker verdict
    site: nothing is applied, no abort, `contract.agent_fault` fires once --
    never silent."""
    sup, run = _worker_run(tmp_path, "ladder-exhausted", vigilant=False)
    verdictless = json.dumps({"task": TASK_1, "status": "complete"})
    agent = _ScriptedWorker([_ok(verdictless)])  # every call replays this
    sup.runner = agent  # type: ignore[assignment]

    asyncio.run(sup._step_worker())

    tasks = run.read_tasks()["tasks"]
    assert tasks[0]["status"] == "in-progress", tasks[0]

    assert agent.calls == 1 + sup.cfg.contract_retry_max
    assert len(_events(run, "contract.agent_fault")) == 1
    assert sup._abort_reason is None


# ---------------------------------------------------------------------------
# (ii.a) cure-01-02 (PRD v0.29 C.1-C.3/F.1): the entry condition for ALL
# THREE shapes above is "the last non-blank line is not a JSON object
# carrying a `verdict` key" -- never "does not parse as a schema-valid
# verdict payload". A last line that IS such an object, with a PRESENT but
# schema-invalid `verdict` value, skips shapes 1-3 entirely -- including
# shape 1's own fenced-recovery scan, even when a valid fenced block sits
# right beside it in the same reply -- and falls to the pre-existing
# generic schema-repair ladder instead: `contract.violation`/`contract.
# agent_fault`, never `contract.recovered`.
# ---------------------------------------------------------------------------

def test_schema_invalid_verdict_beside_valid_fence_skips_recovery(tmp_path):
    """The exact probe cure-01-02 cites: prose + a validating fenced
    ```json block + a last line that IS a JSON object but carries a
    schema-invalid `verdict` value (`"done"`, not in the worker contract's
    enum). Because `verdict` is PRESENT (however invalid), shape 1's fenced
    recovery never runs and `contract.recovered` never fires -- the reply
    instead goes through the pre-existing generic schema ladder, which may
    still land on the SAME fenced block through its own `_ask_contract_fix`
    zero-call scan, but records that recovery as a `contract.violation`,
    never as `contract.recovered`."""
    sup, run = _worker_run(tmp_path, "invalid-verdict-beside-fence",
                           vigilant=False)
    fenced = json.dumps({"verdict": "complete", "task_id": TASK_1})
    reply = (
        "I looked at the task and finished the work.\n\n"
        "```json\n" + fenced + "\n```\n\n"
        '{"verdict": "done"}'
    )
    agent = _ScriptedWorker([_ok(reply)])
    sup.runner = agent  # type: ignore[assignment]

    asyncio.run(sup._step_worker())

    # no `contract.recovered` -- shape 1 never ran, verdict was present.
    assert _events(run, "contract.recovered") == []
    # the generic schema ladder recorded (at least) one violation for the
    # invalid `"done"` verdict before it recovered.
    violations = _events(run, "contract.violation")
    assert len(violations) >= 1, violations
    assert violations[0]["hat"] == "worker"
    # never routed through shape 3 either.
    assert _events(run, "worker.verdict_absent") == []
    # the generic ladder's own zero-call fenced scan (inside `_ask_contract_
    # fix`) still recovers the valid payload -- no EXTRA model call needed
    # beyond the one real (charged) worker iteration.
    assert agent.calls == 1
    tasks = run.read_tasks()["tasks"]
    assert tasks[0]["status"] == "completed", tasks[0]


@pytest.mark.parametrize("present_value", [None, 42, "", ["complete"]],
                         ids=["null", "int", "empty-string", "list"])
def test_any_present_verdict_value_beside_valid_fence_skips_recovery(
        tmp_path, present_value):
    """cure-01-02 retry: the rule is KEY PRESENCE, not the key's value. The
    verifier's own probe was `{"verdict": null}` -- a present `null` used to
    be read as "no verdict" (`if verdict is None`) and took shape 1's fenced
    recovery, emitting `contract.recovered`. Every present value -- `null`,
    a number, an empty string, a list -- is a JSON object CARRYING `verdict`
    and so must skip shapes 1-3 and take the generic schema ladder exactly
    like `"done"` above: a `contract.violation`, never `contract.recovered`,
    never `worker.verdict_absent`."""
    sup, run = _worker_run(tmp_path, f"present-{type(present_value).__name__}",
                           vigilant=False)
    fenced = json.dumps({"verdict": "complete", "task_id": TASK_1})
    reply = (
        "I looked at the task and finished the work.\n\n"
        "```json\n" + fenced + "\n```\n\n"
        + json.dumps({"verdict": present_value})
    )
    agent = _ScriptedWorker([_ok(reply)])
    sup.runner = agent  # type: ignore[assignment]

    asyncio.run(sup._step_worker())

    assert _events(run, "contract.recovered") == []
    violations = _events(run, "contract.violation")
    assert len(violations) >= 1, violations
    assert violations[0]["hat"] == "worker"
    assert _events(run, "worker.verdict_absent") == []


def test_present_null_verdict_without_fence_is_never_silently_dropped(tmp_path):
    """Same rule, no fence to recover from: `{"verdict": null}` is a JSON
    object carrying `verdict`, so it is neither shape 3 (no
    `worker.verdict_absent`) nor a silent no-progress fall-through -- the
    generic schema ladder re-asks over it, and on exhaustion records
    `contract.agent_fault`, leaving the task where it was."""
    sup, run = _worker_run(tmp_path, "present-null-no-fence", vigilant=False)
    agent = _ScriptedWorker([_ok('{"verdict": null}')])  # replays forever
    sup.runner = agent  # type: ignore[assignment]

    asyncio.run(sup._step_worker())

    assert agent.calls == 1 + sup.cfg.contract_retry_max
    assert len(_events(run, "contract.violation")) >= 1
    assert len(_events(run, "contract.agent_fault")) == 1
    assert _events(run, "contract.recovered") == []
    assert _events(run, "worker.verdict_absent") == []
    assert run.read_tasks()["tasks"][0]["status"] == "in-progress"
    assert sup._abort_reason is None


def test_present_null_verdict_repaired_by_the_ladder_is_applied(tmp_path):
    """Same rule, the ladder's success side: a present `null` verdict
    re-asked once and corrected is applied by the branch owning the
    corrected verdict -- the generic schema ladder's one bounded
    re-dispatch, never shape 1/2's `contract.recovered`."""
    sup, run = _worker_run(tmp_path, "present-null-repaired", vigilant=False)
    corrected = json.dumps({"verdict": "complete", "task_id": TASK_1})
    agent = _ScriptedWorker([_ok('{"verdict": null}'), _ok(corrected)])
    sup.runner = agent  # type: ignore[assignment]

    asyncio.run(sup._step_worker())

    assert agent.calls == 2
    assert run.read_tasks()["tasks"][0]["status"] == "completed"
    assert len(_events(run, "contract.violation")) == 1
    assert _events(run, "contract.recovered") == []
    assert _events(run, "worker.verdict_absent") == []


def test_ladder_exhaustion_never_emits_verdict_absent(tmp_path):
    """Same rule, a DIFFERENT case than cure-01-02's own second probe: a
    verdict-less JSON object (shape 2's own entry condition) that exhausts
    the ladder unrepaired emits `contract.agent_fault` for the record, but
    NEVER `worker.verdict_absent` -- that event is shape 3's alone, which
    this reply never reaches because its last line IS a JSON object."""
    sup, run = _worker_run(tmp_path, "ladder-exhausted-no-absent",
                           vigilant=False)
    verdictless = json.dumps({"task": TASK_1, "status": "complete"})
    agent = _ScriptedWorker([_ok(verdictless)])  # every call replays this
    sup.runner = agent  # type: ignore[assignment]

    asyncio.run(sup._step_worker())

    tasks = run.read_tasks()["tasks"]
    assert tasks[0]["status"] == "in-progress", tasks[0]
    assert len(_events(run, "contract.agent_fault")) == 1
    # the documented behaviour cure-01-02 pins: ladder exhaustion is never
    # read as a `worker.verdict_absent` -- that event fires ONLY for a last
    # line that is not a JSON object at all.
    assert _events(run, "worker.verdict_absent") == []


def test_non_dict_json_last_line_is_verdict_absent_not_recovered(tmp_path):
    """Same rule again, a THIRD case: the last non-blank line parses as
    valid JSON but is not an object at all (a bare list) -- shape 3's own
    entry condition is "not a JSON object", not "not JSON", so this reply
    takes shape 3 (one `worker.verdict_absent`, no model call, no ladder)
    exactly like plain prose does, never shape 1 or 2."""
    sup, run = _worker_run(tmp_path, "non-dict-json-last-line")
    reply = "some prose explaining the attempt\n\n[1, 2, 3]"
    agent = _ScriptedWorker([_ok(reply)])
    sup.runner = agent  # type: ignore[assignment]

    asyncio.run(sup._step_worker())

    assert agent.calls == 1  # no model call beyond the real iteration
    tasks = run.read_tasks()["tasks"]
    assert tasks[0]["status"] == "in-progress"  # nothing applied

    absent = _events(run, "worker.verdict_absent")
    assert len(absent) == 1, absent
    assert absent[0]["taskId"] == TASK_1
    assert _events(run, "contract.recovered") == []
    assert _events(run, "contract.violation") == []
    assert _events(run, "contract.agent_fault") == []


# ---------------------------------------------------------------------------
# (iii) a plain-prose reply: no ladder, no model call, one verdict_absent.
# ---------------------------------------------------------------------------

def test_plain_prose_makes_no_model_call_and_emits_one_verdict_absent(tmp_path):
    sup, run = _worker_run(tmp_path, "plain-prose")
    prose = ("I spent this iteration reading the diff and I am not yet "
             "sure where I left off.")
    agent = _ScriptedWorker([_ok(prose)])
    sup.runner = agent  # type: ignore[assignment]

    asyncio.run(sup._step_worker())

    assert agent.calls == 1  # no model call at all beyond the real iteration

    tasks = run.read_tasks()["tasks"]
    assert tasks[0]["status"] == "in-progress"  # nothing applied

    absent = _events(run, "worker.verdict_absent")
    assert len(absent) == 1, absent
    assert absent[0]["taskId"] == TASK_1
    # no reply text anywhere in the event payload.
    for key, value in absent[0].items():
        if isinstance(value, str):
            assert prose not in value

    assert _events(run, "contract.recovered") == []
    assert _events(run, "contract.violation") == []
    assert _events(run, "contract.agent_fault") == []


# ---------------------------------------------------------------------------
# (iv) the promise-marker line keeps today's behaviour.
# ---------------------------------------------------------------------------

def test_promise_marker_line_keeps_todays_behaviour(tmp_path):
    sup, run = _worker_run(tmp_path, "promise-marker")
    agent = _ScriptedWorker([_ok(COMPLETE)])
    sup.runner = agent  # type: ignore[assignment]

    asyncio.run(sup._step_worker())

    # no model call, and nothing applied to the plan -- exactly as today,
    # the retired marker drives no decision in `_step_worker` any more
    # than it did before this task.
    assert agent.calls == 1
    tasks = run.read_tasks()["tasks"]
    assert tasks[0]["status"] == "in-progress"

    # the operator-facing diagnostic (`IterationResult.saw_complete`) is
    # untouched by this task -- still a pure string match, independent of
    # the new recovery/ladder machinery.
    assert agent.script[0].saw_complete is True

    # the marker is prose from a JSON-parsing point of view, exactly the
    # same path (iii) exercises: one `worker.verdict_absent`, never the
    # ladder, never a fenced recovery.
    absent = _events(run, "worker.verdict_absent")
    assert len(absent) == 1, absent
    assert absent[0]["taskId"] == TASK_1
    assert _events(run, "contract.recovered") == []
    assert _events(run, "contract.violation") == []


# ---------------------------------------------------------------------------
# (v) the worker prompt carries C.4's rule at step 5.
# ---------------------------------------------------------------------------

_prompt_supervisor = supervisor_factory(run_id="task005-worker-prompt",
                                        run_root="run", workspace="workspace",
                                        max_approaches=5)


def _prompt() -> str:
    with tempfile.TemporaryDirectory() as td:
        sup = _prompt_supervisor(Path(td))
        return sup.build_prompt("worker")


def test_worker_prompt_step_5_says_the_verdict_is_written_bare():
    prompt = _prompt()
    idx = prompt.index("Report your outcome through the contract")
    section = prompt[idx:idx + 3000]
    assert "bare" in section
    assert "never inside" in section and "code fence" in section
    assert "closing fence" in section
    assert "last non-blank line" in section
    assert "is not a verdict" in section
