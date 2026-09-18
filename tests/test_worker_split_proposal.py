"""Task 021 (PRD requirement N, #102): a worker can propose a SPLIT of the
task it was actually assigned, through its own contract, ingested by
`plan_ingestion`'s EXISTING lint (no second one written), budgeted per
approach, marked worker-origin on every created task and on the surfaces
that already render `origin`, with `dependsOn` coherence checked -- and,
critically, proved NOT to be an escape hatch from the pre-existing
3-iteration stagnation guard (the same hazard v0.9 requirement H had to pin
for `verdict: "unsatisfiable"`, pinned here the same way).

Five acceptance halves:

* `transitions.HAT_SCHEMAS["worker"]` gains an optional `proposed_tasks`
  array field; the schema only checks its TYPE (`contracts.py`'s engine has
  no `items` sub-schema by design) -- the real lint is reused, not
  re-derived.
* `plan_ingestion.lint_worker_split` reuses `validate_proposal_shape` /
  `lint_proposal` / `lint_own_terminal_state` verbatim, adding only the two
  rules that make this a bounded SPLIT: an assigned task is required, and a
  proposed id may not collide with one already live in the plan.
* `state.insert_worker_split_tasks` always stamps `origin: "worker"` and
  `splitFrom`, the same "always stamp, never trust the caller" discipline
  `steer_task_insert` already applies; `format_task_status` marks the
  result ` (worker-origin)`, mirroring ` (steering-origin)`.
* `LoopSupervisor._apply_worker_proposed_tasks` enforces the per-approach
  budget (`JobConfig.worker_split_budget_per_approach`) and refuses (never
  raises) an unlintable or over-budget proposal.
* A worker whose EVERY iteration proposes a split and reports no other
  verdict still exhausts the pre-existing `stagnation_exhausted` guard
  after three iterations -- proposing is excluded from what counts as
  progress against that guard (`state.tasks_doc_excluding_new_entries`).

The superseded `worker.md` prose ("do only the smallest atomic first piece
... describe the remainder ... in the notes file") is asserted gone.

#209 defect 7 adds the worker's SECOND plan-shaping side channel to this
module: `proposed_task_updates`, a bounded `dependsOn` re-point of a task
`state.task_blocked_by` itself derives as doomed. It belongs here rather
than in a module of its own -- same contract field family, the same
per-approach budget shape, the same refusal-reaches-the-next-prompt feedback
path (literally the same status key and prompt heading), and the same "is
this an escape hatch from the stagnation guard" question the five halves
above already exist to answer. Its own six halves:

* `HAT_SCHEMAS["worker"]` gains an optional `proposed_task_updates` array,
  TYPE-checked only, exactly like `proposed_tasks`.
* `plan_ingestion.lint_worker_task_updates` grants standing from the
  HARNESS's derived doom over a `pending` task, drops only `failed`/
  `skipped` edges, adds none, requires a `reason`, and reuses
  `validate_dependency_edges` -- refusing the WHOLE array on any rejected
  edge instead of dropping it, because a silently dropped edge is the exact
  outcome the drop-only-dead guard forbids.
* `state.apply_worker_dependency_repoints` writes the replacement list,
  appends the `reason` to the task's own `notes`, and emits one
  `task.dependency_repointed`.
* `LoopSupervisor._apply_worker_task_updates` enforces
  `worker_repoint_budget_per_approach` (`status.json`'s
  `workerRepointUpdates`) and refuses whole, never partially.
* The call site runs AFTER the split block in the same reply, so a re-point
  onto a child the same reply carved off resolves.
* An accepted re-point cannot be repeated on the same task -- it clears the
  very standing a second one would need -- which is what bounds the
  stagnation reset a `dependsOn` edit does buy (an in-place row edit is not
  filtered out of the progress diff the way an inserted split task is).
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from ralphd.engine import contracts, plan_ingestion, transitions
from ralphd.engine.config import JobConfig
from ralphd.engine.loop import LoopSupervisor
from ralphd.engine.runner import IterationResult
from ralphd.engine.state import (
    DEPENDENCY_REPOINTED_EVENT,
    RunDir,
    TASK_DEPENDENCY_DEAD_STATUSES,
    TASK_ORIGIN_WORKER,
    apply_worker_dependency_repoints,
    atomic_write_json,
    format_task_status,
    insert_worker_split_tasks,
    is_worker_origin,
    task_blocked_by,
    tasks_doc_excluding_new_entries,
)

# test_worker_hat.py absorbed test_failed_task_vocabulary.py (task 028,
# requirement D.7); the aliases restore the names this module already used.
from test_worker_hat import (
    _drive_until_paused,
)

REPO = Path(__file__).resolve().parents[1]
WORKER_MD = (REPO / "src" / "ralphd" / "prompts" / "worker.md").read_text()

TASK_1 = "021"


def _ok(text: str) -> IterationResult:
    r = IterationResult(exit_code=0)
    r.final_text = text
    r.duration_s = 5.0
    r.usage = {"input": 10, "output": 5, "totalTokens": 15}
    return r


def _task(**over) -> dict:
    task = {"id": TASK_1, "title": "a task big enough to split",
            "status": "in-progress",
            "successCriteria": "the whole thing works end to end"}
    task.update(over)
    return task


# ---------------------------------------------------------------------------
# the schema field
# ---------------------------------------------------------------------------

def test_the_worker_schema_declares_an_optional_proposed_tasks_array():
    schema = transitions.HAT_SCHEMAS["worker"]
    assert schema["properties"]["proposed_tasks"] == {"type": "array"}
    assert "proposed_tasks" not in schema["required"]


def test_a_worker_payload_with_proposed_tasks_validates_clean():
    errors = contracts.validate_payload("worker", {
        "verdict": "no_verdict",
        "proposed_tasks": [{"id": "021a", "title": "part a",
                           "successCriteria": "part a works"}],
    })
    assert errors == []


def test_a_non_array_proposed_tasks_is_a_schema_violation():
    errors = contracts.validate_payload(
        "worker", {"verdict": "no_verdict", "proposed_tasks": "not a list"})
    assert errors


# ---------------------------------------------------------------------------
# plan_ingestion.lint_worker_split -- the reused lint
# ---------------------------------------------------------------------------

def test_no_assigned_task_refuses_any_proposal():
    errors = plan_ingestion.lint_worker_split(
        [{"id": "021a", "title": "x", "successCriteria": "y"}],
        assigned_task_id=None, existing_ids=set())
    assert errors and "assigned" in errors[0]


def test_a_well_formed_split_lints_clean():
    errors = plan_ingestion.lint_worker_split(
        [{"id": "021a", "title": "part a", "successCriteria": "a works"},
         {"id": "021b", "title": "part b", "successCriteria": "b works",
          "dependsOn": ["021a"]}],
        assigned_task_id=TASK_1, existing_ids={TASK_1})
    assert errors == []


def test_reuses_lint_proposal_for_duplicate_ids():
    """No second lint: the duplicate-id message is `lint_proposal`'s own
    wording, byte for byte."""
    errors = plan_ingestion.lint_worker_split(
        [{"id": "021a", "title": "x", "successCriteria": "y"},
         {"id": "021a", "title": "x2", "successCriteria": "y2"}],
        assigned_task_id=TASK_1, existing_ids=set())
    assert any("duplicate id" in e for e in errors)


def test_reuses_lint_proposal_for_dependson_coherence():
    """`dependsOn` is resolved against the union of the proposed batch and
    the caller's `existing_ids` (task 017, #162) -- an id in neither is
    still refused."""
    errors = plan_ingestion.lint_worker_split(
        [{"id": "021a", "title": "x", "successCriteria": "y",
          "dependsOn": ["999-not-in-batch"]}],
        assigned_task_id=TASK_1, existing_ids=set())
    assert any("unknown task" in e for e in errors)


def test_dependson_may_resolve_against_the_live_plan_including_the_parent():
    """Task 017 (PRD requirement B.2, #162): a child's `dependsOn` may
    name an id already live in `tasks.json` -- including the very task
    being split (`assigned_task_id`, always a member of `existing_ids`
    since it is itself a task in the live plan) -- and not only a sibling
    in the same proposed batch."""
    errors = plan_ingestion.lint_worker_split(
        [{"id": "021a", "title": "part a", "successCriteria": "a works",
          "dependsOn": [TASK_1, "017-neighbour"]}],
        assigned_task_id=TASK_1, existing_ids={TASK_1, "017-neighbour"})
    assert errors == []


def test_dependson_within_the_batch_still_resolves_cleanly():
    """Acceptance WITHIN the proposed batch itself, unaffected by widening
    resolution to also cover the live plan (task 017, #162)."""
    errors = plan_ingestion.lint_worker_split(
        [{"id": "021a", "title": "part a", "successCriteria": "a works"},
         {"id": "021b", "title": "part b", "successCriteria": "b works",
          "dependsOn": ["021a"]}],
        assigned_task_id=TASK_1, existing_ids={TASK_1})
    assert errors == []


def test_dependson_in_neither_the_batch_nor_the_plan_names_both_sets_in_the_hint():
    """An id resolving against NEITHER set is still refused, and the
    refusal's own text names both sets that were checked (task 017,
    #162) -- not just the offending id."""
    errors = plan_ingestion.lint_worker_split(
        [{"id": "021a", "title": "part a", "successCriteria": "a works",
          "dependsOn": ["totally-unknown"]}],
        assigned_task_id=TASK_1, existing_ids={TASK_1, "017-neighbour"})
    assert len(errors) == 1
    assert "unknown task" in errors[0] and "totally-unknown" in errors[0]
    assert "021a" in errors[0]              # the proposed batch's own id
    assert TASK_1 in errors[0]               # the live plan's id set
    assert "017-neighbour" in errors[0]      # ... including a neighbour


def test_reuses_lint_proposal_for_cycles():
    errors = plan_ingestion.lint_worker_split(
        [{"id": "021a", "title": "x", "successCriteria": "y",
          "dependsOn": ["021b"]},
         {"id": "021b", "title": "x2", "successCriteria": "y2",
          "dependsOn": ["021a"]}],
        assigned_task_id=TASK_1, existing_ids=set())
    assert any("cycle" in e for e in errors)


def test_reuses_lint_proposal_for_boilerplate_and_status():
    boilerplate = plan_ingestion.lint_worker_split(
        [{"id": "021a", "title": "x",
          "successCriteria": plan_ingestion.BOILERPLATE_SUCCESS_CRITERIA}],
        assigned_task_id=TASK_1, existing_ids=set())
    assert any("boilerplate" in e for e in boilerplate)

    smuggled_status = plan_ingestion.lint_worker_split(
        [{"id": "021a", "title": "x", "successCriteria": "y",
          "status": "completed"}],
        assigned_task_id=TASK_1, existing_ids=set())
    assert any("'status' field" in e for e in smuggled_status)


def test_reuses_lint_own_terminal_state():
    errors = plan_ingestion.lint_worker_split(
        [{"id": "021a", "title": "x",
          "successCriteria": "cannot complete until the run is terminal"}],
        assigned_task_id=TASK_1, existing_ids=set())
    assert any("own terminal state" in e for e in errors)


def test_own_id_collision_with_the_live_plan_is_refused():
    errors = plan_ingestion.lint_worker_split(
        [{"id": TASK_1, "title": "x", "successCriteria": "y"}],
        assigned_task_id=TASK_1, existing_ids={TASK_1})
    assert any("already exist" in e for e in errors)


# ---------------------------------------------------------------------------
# state.py: origin marking + the stagnation-diff helper
# ---------------------------------------------------------------------------

def test_is_worker_origin_predicate():
    assert is_worker_origin({"id": "1", "origin": "worker"}) is True
    assert is_worker_origin({"id": "1"}) is False
    assert is_worker_origin({"id": "1", "origin": "steer"}) is False
    assert is_worker_origin("not a dict") is False


def test_format_task_status_marks_a_worker_origin_task():
    task = {"id": "021a", "status": "pending", "origin": TASK_ORIGIN_WORKER}
    assert format_task_status(task) == "pending (worker-origin)"


def test_insert_worker_split_tasks_stamps_status_origin_and_split_from(tmp_path):
    run = RunDir(root=tmp_path / "run")
    run.tasks_file.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(run.tasks_file, {"version": 1, "tasks": [_task()]})
    inserted = insert_worker_split_tasks(
        run,
        [{"id": "021a", "title": "part a", "successCriteria": "a works",
          "status": "completed", "origin": "planner"}],
        TASK_1,
    )
    assert inserted == ["021a"]
    data = run.read_tasks()
    new_task = next(t for t in data["tasks"] if t["id"] == "021a")
    assert new_task["status"] == "pending"          # never trusts the caller
    assert new_task["origin"] == TASK_ORIGIN_WORKER  # never trusts the caller
    assert new_task["splitFrom"] == TASK_1


def test_insert_worker_split_tasks_skips_an_id_collision(tmp_path):
    run = RunDir(root=tmp_path / "run")
    run.tasks_file.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(run.tasks_file, {"version": 1, "tasks": [_task()]})
    inserted = insert_worker_split_tasks(
        run, [{"id": TASK_1, "title": "x", "successCriteria": "y"}], TASK_1)
    assert inserted == []
    assert len(run.read_tasks()["tasks"]) == 1


def test_tasks_doc_excluding_new_entries_drops_only_brand_new_ones():
    before_ids = {"001"}
    doc = {"tasks": [
        {"id": "001", "status": "completed", "origin": "worker"},
        {"id": "002", "status": "pending", "origin": "worker"},
        {"id": "003", "status": "pending"},
    ]}
    view = tasks_doc_excluding_new_entries(doc, before_ids, origin="worker")
    ids = {t["id"] for t in view["tasks"]}
    assert ids == {"001", "003"}   # 002 is new-this-pass worker-origin, dropped


# ---------------------------------------------------------------------------
# LoopSupervisor._apply_worker_proposed_tasks
# ---------------------------------------------------------------------------

def _sup(root: Path, tasks: list | None = None, **cfg_kw) -> LoopSupervisor:
    root.mkdir(parents=True, exist_ok=True)
    run = RunDir(root=root)
    run.update_status(state="running")
    kw = {"iterations": 15, "max_approaches": 1, "vigilant": False,
          "infra_retry_backoff_s": [0.0], "infra_retry_backoff_max_s": 0.0,
          **cfg_kw}
    sup = LoopSupervisor(JobConfig(run_id="v014-worker-split", **kw), run, root)
    # `tasks` defaults to the single splittable task every pre-existing node
    # here uses; #209 defect 7's re-point nodes below need a doomed graph
    # instead, so the seed is a parameter rather than a literal.
    atomic_write_json(run.tasks_file,
                      {"version": 1, "tasks": list(tasks or [_task()])})
    return sup


def test_apply_proposed_tasks_inserts_and_records_budget_usage(tmp_path):
    sup = _sup(tmp_path / "run", worker_split_budget_per_approach=5)
    sup._apply_worker_proposed_tasks(
        [{"id": "021a", "title": "part a", "successCriteria": "a works"}],
        TASK_1, 1)
    data = sup.run.read_tasks()
    new_task = next(t for t in data["tasks"] if t["id"] == "021a")
    assert new_task["origin"] == TASK_ORIGIN_WORKER
    assert new_task["splitFrom"] == TASK_1
    usage = sup.run.read_status().get("workerSplitProposals")
    assert usage == {"1": 1}


def test_apply_proposed_tasks_refuses_without_an_assigned_task(tmp_path):
    sup = _sup(tmp_path / "run")
    sup._apply_worker_proposed_tasks(
        [{"id": "021a", "title": "part a", "successCriteria": "a works"}],
        None, 1)
    data = sup.run.read_tasks()
    assert not any(t.get("id") == "021a" for t in data["tasks"])
    events = [json.loads(x) for x in
              (tmp_path / "run" / "events.jsonl").read_text().splitlines()]
    assert any(e.get("type") == "contract.violation" for e in events)


def test_apply_proposed_tasks_enforces_the_per_approach_budget(tmp_path):
    sup = _sup(tmp_path / "run", worker_split_budget_per_approach=1)
    sup._apply_worker_proposed_tasks(
        [{"id": "021a", "title": "part a", "successCriteria": "a works"}],
        TASK_1, 1)
    # A second proposal in the SAME approach would push the total to 2,
    # over the budget of 1 -- refused whole, nothing partially inserted.
    sup._apply_worker_proposed_tasks(
        [{"id": "021b", "title": "part b", "successCriteria": "b works"}],
        TASK_1, 1)
    data = sup.run.read_tasks()
    ids = {t["id"] for t in data["tasks"]}
    assert "021a" in ids
    assert "021b" not in ids
    assert sup.run.read_status()["workerSplitProposals"] == {"1": 1}


def test_apply_proposed_tasks_refuses_an_unlintable_batch(tmp_path):
    sup = _sup(tmp_path / "run")
    sup._apply_worker_proposed_tasks(
        [{"id": "021a", "title": "x", "successCriteria": "y"},
         {"id": "021a", "title": "x2", "successCriteria": "y2"}],
        TASK_1, 1)
    data = sup.run.read_tasks()
    assert not any(t.get("id") == "021a" for t in data["tasks"])
    assert "workerSplitProposals" not in sup.run.read_status()


# ---------------------------------------------------------------------------
# the whole run: proposing alone is not a stagnation escape hatch
# ---------------------------------------------------------------------------

class _ScriptedAgent:
    """Same shape as `tests/test_unsatisfiable_criteria.py`'s scripted
    agent: dispatches on the phase the engine recorded, one script per
    phase, the last step of each repeating forever."""

    def __init__(self, handlers: dict):
        self.handlers = handlers
        self.calls: dict[str, int] = {}
        self.running = False

    def interrupt(self) -> bool:
        return self.running

    async def run(self, prompt, transcript, **kw) -> IterationResult:
        phase = self.sup.run.read_status().get("phase")
        n = self.calls.get(phase, 0)
        self.calls[phase] = n + 1
        steps = self.handlers[phase]
        self.running = True
        try:
            return steps[min(n, len(steps) - 1)](self.sup, prompt)
        finally:
            self.running = False


def _run_resumed(root: Path, handlers: dict, tasks: list[dict],
                 **cfg_kw) -> tuple[LoopSupervisor, str]:
    RunDir(root=root).update_status(state="starting")
    root.mkdir(parents=True, exist_ok=True)
    run = RunDir(root=root)
    kw = {"iterations": 15, "max_approaches": 1, "vigilant": False,
          "on_complete": "exit", "infra_retry_backoff_s": [0.0],
          "infra_retry_backoff_max_s": 0.0, "infra_outage_budget_s": 1000.0,
          "worker_split_budget_per_approach": 100,
          **cfg_kw}
    sup = LoopSupervisor(JobConfig(run_id="v014-worker-split-run", **kw), run, root)
    agent = _ScriptedAgent(handlers)
    agent.sup = sup                      # type: ignore[attr-defined]
    sup.runner = agent                    # type: ignore[assignment]

    async def no_backoff(seconds):
        return seconds, False

    sup._wait_out_backoff = no_backoff    # type: ignore[method-assign]
    atomic_write_json(sup.run.tasks_file, {"version": 1, "tasks": tasks})
    sup.iterations_used = 1
    return sup, asyncio.run(sup._run_job_core())


def _run_resumed_until_paused(root: Path, handlers: dict, tasks: list[dict],
                              **cfg_kw) -> LoopSupervisor:
    """Task 042 (steer 004): same setup as `_run_resumed` above, but for a
    worker that never yields a verdict on the (default) LAST approach --
    since task 012 (`abdc914`) the stagnation guard there pauses and
    escalates instead of ending the run `failed`, then parks at `_gate()`
    forever waiting for an operator `resume()` that no scripted test here
    ever sends. Drives the run with the bounded-wait `_drive_until_paused`
    helper (`tests/test_worker_hat.py`) instead of letting
    `asyncio.run(sup._run_job_core())` block forever, and returns the
    supervisor for the caller to read `status.json` off of directly.
    """
    RunDir(root=root).update_status(state="starting")
    root.mkdir(parents=True, exist_ok=True)
    run = RunDir(root=root)
    kw = {"iterations": 15, "max_approaches": 1, "vigilant": False,
          "on_complete": "exit", "infra_retry_backoff_s": [0.0],
          "infra_retry_backoff_max_s": 0.0, "infra_outage_budget_s": 1000.0,
          "worker_split_budget_per_approach": 100,
          **cfg_kw}
    sup = LoopSupervisor(JobConfig(run_id="v014-worker-split-run", **kw), run, root)
    agent = _ScriptedAgent(handlers)
    agent.sup = sup                      # type: ignore[attr-defined]
    sup.runner = agent                    # type: ignore[assignment]

    async def no_backoff(seconds):
        return seconds, False

    sup._wait_out_backoff = no_backoff    # type: ignore[method-assign]
    atomic_write_json(sup.run.tasks_file, {"version": 1, "tasks": tasks})
    sup.iterations_used = 1
    asyncio.run(_drive_until_paused(sup))
    return sup


def _propose_only(n: int):
    def _step(sup, prompt=""):
        payload = {"verdict": "no_verdict",
                  "proposed_tasks": [{"id": f"021-split-{n}", "title": "x",
                                     "successCriteria": "y works"}]}
        return _ok(f"proposing a split, attempt {n}\n" + json.dumps(payload))
    return _step


def _review_verified(sup, prompt=""):
    return _ok("verified\n" + json.dumps({"verdict": "verified"}))


def test_a_worker_that_only_ever_proposes_splits_still_fails_the_approach(
        tmp_path):
    """The pin requirement N asks for: proposing a split every iteration,
    and reporting nothing else, must not reset the 3-iteration stagnation
    guard -- exactly as a genuinely silent hat would not
    (`tests/test_unsatisfiable_criteria.py`'s sibling pin for
    `unsatisfiable`), even though `tasks.json` visibly grows on every pass.
    Since task 012 (`abdc914`) the guard on the (default) LAST approach
    pauses and escalates instead of ending the run `failed` -- asserted
    here via `_run_resumed_until_paused` (task 042, steer 004)."""
    root = tmp_path / "run"
    tasks = [_task(status="in-progress")]

    sup = _run_resumed_until_paused(root, {
        "worker": [_propose_only(1), _propose_only(2), _propose_only(3)],
        "review": [_review_verified],
    }, tasks)

    status = sup.run.read_status()
    assert status["state"] == "paused", status
    assert status["pauseReason"] == "stagnation", status
    assert sup.runner.calls["worker"] == 3          # type: ignore[attr-defined]
    data = sup.run.read_tasks()
    # The proposals DID land -- this is not a dead field -- they simply
    # never counted as progress.
    split_ids = {t["id"] for t in data["tasks"] if is_worker_origin(t)}
    assert split_ids == {"021-split-1", "021-split-2", "021-split-3"}
    assigned = next(t for t in data["tasks"] if t["id"] == TASK_1)
    assert assigned["status"] == "in-progress"
    assert "review" not in [
        json.loads(x).get("phase") for x in
        (root / "events.jsonl").read_text().splitlines()
        if json.loads(x).get("type") == "phase"
    ]


# ---------------------------------------------------------------------------
# #209 defect 7: `proposed_task_updates`, the worker's OTHER plan-shaping
# side channel -- a bounded `dependsOn` re-point of a task the harness
# itself derives as doomed. Folded into this module rather than given one of
# its own: same contract field family, same per-approach budget shape, same
# refusal-reaches-the-next-prompt feedback path, and the same "is it a
# stagnation escape hatch" question this module already exists to answer.
# ---------------------------------------------------------------------------

DEAD_1 = "005"          # a `failed` ancestor
DOOMED_1 = "009"        # `pending`, dependsOn DEAD_1 -- doomed
LIVE_1 = "004"          # `validated`: the sibling that carries the work


def _row(tid: str, *, status: str = "pending", depends=None, **over) -> dict:
    row = {"id": tid, "title": f"task {tid}", "successCriteria": "n/a",
           "status": status, "origin": "planner",
           "dependsOn": list(depends or [])}
    row.update(over)
    return row


def _doomed_graph() -> list[dict]:
    """The observed incident's shape, as data: a `failed` task, a `pending`
    dependent the harness therefore derives as doomed, an already-`validated`
    sibling that actually carries the dead task's work, and the task the
    worker itself is holding this iteration."""
    return [_task(), _row(LIVE_1, status="validated"),
            _row(DEAD_1, status="failed"),
            _row(DOOMED_1, depends=[DEAD_1])]


def _update(task_id: str = DOOMED_1, depends=(LIVE_1,),
            reason: str = "005 failed; 004 already delivered its scope") -> dict:
    return {"task_id": task_id, "dependsOn": list(depends), "reason": reason}


def _lint(updates, *, tasks=None, assigned=TASK_1, doomed=None) -> list[str]:
    tasks = _doomed_graph() if tasks is None else tasks
    doomed = set(task_blocked_by(tasks)) if doomed is None else doomed
    return plan_ingestion.lint_worker_task_updates(
        updates, assigned_task_id=assigned, tasks=tasks, doomed_ids=doomed)


def test_the_worker_schema_declares_an_optional_proposed_task_updates_array():
    """The declaration, an accepted payload and a TYPE refusal in one node:
    `contracts.py`'s schema engine has no `items` sub-schema by design, so
    the outer type is the whole of what this schema can say -- the real lint
    is `plan_ingestion.lint_worker_task_updates`, pinned below."""
    schema = transitions.HAT_SCHEMAS["worker"]
    assert schema["properties"]["proposed_task_updates"] == {"type": "array"}
    assert "proposed_task_updates" not in schema["required"]
    assert contracts.validate_payload("worker", {
        "verdict": "no_verdict", "proposed_task_updates": [_update()],
    }) == []
    assert contracts.validate_payload("worker", {
        "verdict": "no_verdict", "proposed_task_updates": "not a list"})


def test_the_repointable_dead_statuses_copy_matches_the_state_constant():
    """`plan_ingestion._REPOINTABLE_DEAD_STATUSES` is a deliberate copy of
    `state.TASK_DEPENDENCY_DEAD_STATUSES` (this module must not import
    `state`; the dependency runs the other way). Pinned equal so the copy
    cannot drift -- the same sanity-pin shape
    `tests/test_prompt_dependency_doom.py` uses for the satisfied-status
    tuple its own prose relies on."""
    assert (plan_ingestion._REPOINTABLE_DEAD_STATUSES
            == TASK_DEPENDENCY_DEAD_STATUSES)


def test_repoint_lint_refuses_without_an_assigned_task_or_an_empty_batch():
    """The same standing rule `lint_worker_split`'s first branch applies: a
    re-point is a repair made in passing while holding real work, never a
    plan-editing mandate handed to an unassigned iteration."""
    errors = _lint([_update()], assigned=None)
    assert errors and "no task was assigned" in errors[0]
    assert _lint([])
    assert _lint("not a list")


def test_repoint_lint_refuses_a_target_the_harness_does_not_call_blocked():
    """Derived doom, not the worker's opinion, is the standing -- refused
    even though every edge in the replacement would be legal on its own."""
    tasks = [_task(), _row(LIVE_1, status="validated"), _row(DOOMED_1)]
    errors = _lint([_update(depends=[LIVE_1])], tasks=tasks)
    assert len(errors) == 1
    assert "does not derive" in errors[0] and "taskBlockedBy" in errors[0]


def test_repoint_lint_refuses_a_target_that_is_not_pending():
    """`in-progress`/`validation-failed` carry the #184 hazard
    `state._depends_on_claimed_refusal` names -- never rewrite the graph a
    live attempt is executing against -- and a terminal target has nothing
    left to unblock. Narrower than `TASK_CLAIMED_STATUSES` on purpose."""
    for status in ("in-progress", "validation-failed", "completed"):
        tasks = [_task(), _row(LIVE_1, status="validated"),
                 _row(DEAD_1, status="failed"),
                 _row(DOOMED_1, status=status, depends=[DEAD_1])]
        # `doomed` is supplied directly: `task_blocked_by` drops a
        # `completed` task as a sink, and the point here is the status
        # refusal itself, reached before any doom question.
        errors = _lint([_update()], tasks=tasks, doomed={DOOMED_1})
        assert len(errors) == 1, (status, errors)
        assert f"is {status!r}, not 'pending'" in errors[0]


def test_repoint_lint_refuses_dropping_a_live_dependency():
    """The anti-abuse guard: only an edge onto a `failed`/`skipped` task may
    be dropped, so a worker cannot shed a dependency it merely does not want
    to wait for. The target is genuinely doomed here and the replacement is
    otherwise legal -- the live edge alone refuses the batch."""
    tasks = [_task(), _row(LIVE_1, status="validated"),
             _row(DEAD_1, status="failed"), _row("006"),
             _row(DOOMED_1, depends=[DEAD_1, "006"])]
    errors = _lint([_update(depends=[LIVE_1])], tasks=tasks)
    assert len(errors) == 1
    assert "'006'" in errors[0]
    assert "do not want to wait" in errors[0]


def test_repoint_lint_refuses_any_dead_edge_in_the_replacement():
    """"A failed dependency never satisfies" -- a replacement naming a
    `failed` or `skipped` id is a deadlock, not a repair. Stated over the
    WHOLE replacement list, not only over the ids it adds: an entry that
    KEPT the dead edge it already had (`DEAD_1` below) would otherwise be
    accepted while leaving the target exactly as doomed as it found it -- a
    re-point that buys nothing but a free stagnation reset."""
    tasks = _doomed_graph() + [_row("007", status="skipped")]
    for dead in (DEAD_1, "007"):
        errors = _lint([_update(depends=[LIVE_1, dead])], tasks=tasks)
        assert len(errors) == 1, (dead, errors)
        assert repr(dead) in errors[0]
        assert "never" in errors[0] and "satisfies" in errors[0]


def test_repoint_lint_reuses_the_shared_validator_and_refuses_the_whole_batch():
    """A self-loop, an unknown id and a cycle all come back as
    `validate_dependency_edges`' OWN reason strings -- the single cycle
    detector every other `dependsOn` writer already shares, not a second
    copy. For this one caller a rejected edge refuses the WHOLE batch
    instead of being dropped and the remainder written: dropping it silently
    would produce exactly the vanished-edge outcome the drop-only-dead guard
    above forbids.

    The last case is the working-graph fold: two entries that are each fine
    alone but jointly close a cycle, caught because entry 2 is validated
    against entry 1's already-accepted edges."""
    self_loop = _lint([_update(depends=[DOOMED_1])])
    assert len(self_loop) == 1 and "self-loop" in self_loop[0]

    unknown = _lint([_update(depends=["999-nowhere"])])
    assert len(unknown) == 1 and "unknown task" in unknown[0]

    # 009 -> 010 -> 009: legal edge by edge, a cycle together.
    tasks = _doomed_graph() + [_row("010", depends=[DEAD_1])]
    both = _lint([_update(DOOMED_1, depends=["010"]),
                  _update("010", depends=[DOOMED_1])], tasks=tasks)
    assert len(both) == 1 and "cycle" in both[0]


def test_repoint_lint_refuses_a_bad_shape_a_blank_reason_and_a_duplicate_id():
    """All three fields mandatory, `dependsOn` a full replacement list of
    non-blank strings, one entry per task so the batch has exactly one
    meaning for each."""
    blank_reason = _lint([_update(reason="  ")])
    assert any("'reason'" in e for e in blank_reason)

    bad_shape = _lint([{"task_id": DOOMED_1, "dependsOn": "004",
                        "reason": "x"}])
    assert any("'dependsOn' must be an array" in e for e in bad_shape)

    assert any("blank 'task_id'" in e
               for e in _lint([{"dependsOn": [LIVE_1], "reason": "x"}]))
    assert any("expected an object" in e for e in _lint(["not a dict"]))

    duplicate = _lint([_update(), _update()])
    assert any("duplicate task_id" in e for e in duplicate)


def test_repoint_lint_accepts_the_observed_incident_shape():
    """The one incident this residual lever exists for, as data: a `pending`
    task re-pointed off a `failed` ancestor onto an already-`validated`
    sibling, where no cure task existed to be re-pointed onto by
    `state._repoint_dependents_onto_cure_task`. Also accepted: a second
    entry whose replacement KEEPS an edge onto a still-doomed sibling --
    clearing one dependent's doom is not conditioned on clearing every
    dependent's."""
    tasks = _doomed_graph() + [_row("010", depends=[DEAD_1, DOOMED_1])]
    assert _lint([_update(),
                  _update("010", depends=[LIVE_1, DOOMED_1])],
                 tasks=tasks) == []


def test_a_doom_clearing_repoint_cannot_be_repeated_on_the_same_task():
    """A re-point that actually clears the doom removes exactly the standing
    a second re-point of the same task would need: the task is no longer a
    key of `state.task_blocked_by`'s derived map, so asking again is refused.
    Putting the dead edge back is refused twice over (no standing, and a dead
    edge in the replacement).

    This is the honest bound on the stagnation risk. A re-point edits an
    EXISTING row, so unlike an inserted split task it is NOT filtered out of
    the pass's progress diff by `state.tasks_doc_excluding_new_entries`
    (pinned above) and it DOES read as progress. What keeps that from being
    an unbounded escape from the 3-iteration no-progress guard is: an entry
    must drop a real dead edge or leave the target still doomed for some
    OTHER reason, a doom-clearing entry cannot be re-sent, and everything
    else is capped at `worker_repoint_budget_per_approach` per approach
    (pinned in the budget node below) -- at most three free resets, never a
    treadmill."""
    tasks = _doomed_graph()
    assert _lint([_update()], tasks=tasks) == []
    repointed = [_row(t["id"], status=t["status"],
                      depends=([LIVE_1] if t["id"] == DOOMED_1
                               else t.get("dependsOn")))
                  for t in tasks]
    assert task_blocked_by(repointed) == {}
    again = _lint([_update(depends=[LIVE_1])], tasks=repointed)
    assert len(again) == 1 and "does not derive" in again[0]
    back = _lint([_update(depends=[DEAD_1])], tasks=repointed)
    assert back and "does not derive" in back[0]


def test_apply_repoints_writes_the_graph_the_notes_trace_and_the_audit_event(
        tmp_path):
    """`state.apply_worker_dependency_repoints`: the replacement list lands,
    the entry's mandatory `reason` is appended to the task's own `notes`
    through the existing `_JUSTIFICATION_SEPARATOR` convention (so the
    re-point is legible from the task record, not only from
    `events.jsonl`), and one `task.dependency_repointed` event per update
    carries `removed`/`added`/`dependsOn`/`reason`/`source`."""
    run = RunDir(root=tmp_path / "run")
    run.tasks_file.parent.mkdir(parents=True, exist_ok=True)
    tasks = _doomed_graph()
    next(t for t in tasks if t["id"] == DOOMED_1)["notes"] = "an earlier note"
    atomic_write_json(run.tasks_file, {"version": 1, "tasks": tasks})

    assert apply_worker_dependency_repoints(run, [_update()]) == [DOOMED_1]

    row = next(t for t in run.read_tasks()["tasks"] if t["id"] == DOOMED_1)
    assert row["dependsOn"] == [LIVE_1]
    assert row["notes"].startswith("an earlier note")
    assert row["notes"].endswith("005 failed; 004 already delivered its scope")
    events = [json.loads(x) for x in
              (tmp_path / "run" / "events.jsonl").read_text().splitlines()]
    repoints = [e for e in events if e["type"] == DEPENDENCY_REPOINTED_EVENT]
    assert len(repoints) == 1
    assert repoints[0]["taskId"] == DOOMED_1
    assert repoints[0]["removed"] == [DEAD_1]
    assert repoints[0]["added"] == [LIVE_1]
    assert repoints[0]["dependsOn"] == [LIVE_1]
    assert repoints[0]["source"] == "worker-repoint"
    assert repoints[0]["reason"]
    # A vanished id is skipped, never inserted -- the same belt-and-
    # suspenders `insert_worker_split_tasks`' collision skip applies.
    assert apply_worker_dependency_repoints(
        run, [_update("no-such-task")]) == []


def test_apply_task_updates_records_budget_usage_and_refuses_whole_when_over(
        tmp_path):
    """`_apply_worker_task_updates`: `status.json`'s `workerRepointUpdates`
    is the per-approach counter, and a batch that would push the running
    total past `worker_repoint_budget_per_approach` changes NOTHING (no
    partial re-point) and emits `contract.violation`. Its own knob, not
    `worker_split_budget_per_approach`: that counter is documented as the
    tasks a split INSERTED, and a re-point inserts none."""
    tasks = _doomed_graph() + [_row("010", depends=[DEAD_1])]
    sup = _sup(tmp_path / "run", tasks, worker_repoint_budget_per_approach=1)
    sup._apply_worker_task_updates([_update()], TASK_1, 1)
    assert sup.run.read_status()["workerRepointUpdates"] == {"1": 1}

    sup._apply_worker_task_updates([_update("010", depends=[LIVE_1])],
                                   TASK_1, 1)
    row = next(t for t in sup.run.read_tasks()["tasks"] if t["id"] == "010")
    assert row["dependsOn"] == [DEAD_1]          # nothing partially applied
    assert sup.run.read_status()["workerRepointUpdates"] == {"1": 1}
    events = [json.loads(x) for x in
              (tmp_path / "run" / "events.jsonl").read_text().splitlines()]
    assert any(e.get("type") == "contract.violation"
               and "re-point budget" in (e.get("message") or "")
               for e in events)


def test_a_refused_repoint_reaches_the_next_worker_prompt(tmp_path):
    """The reused feedback path, asserted so the reuse is deliberate rather
    than incidental: a refused re-point lands under the SAME
    `REFUSED_WORKER_SPLIT_STATUS_KEY` a refused split uses, and
    `_worker_assignment_extra` renders its errors verbatim under the same
    heading. No second status key and no second heading -- both are refused
    optional plan-shaping proposals from one channel with no re-ask ladder,
    and the errors already self-identify through their
    `proposed_task_updates:` prefix.

    The batch here is two entries, the first of them perfectly legal, so it
    also pins the refuse-WHOLE property on the LINT branch (the budget
    branch's own is pinned above): one bad entry leaves the good one's graph
    exactly as it stood."""
    tasks = _doomed_graph() + [_row("010", depends=[DEAD_1])]
    sup = _sup(tmp_path / "run", tasks)
    sup._apply_worker_task_updates(
        [_update(), _update("010", depends=["999-nowhere"])], TASK_1, 1)
    row = next(t for t in sup.run.read_tasks()["tasks"] if t["id"] == DOOMED_1)
    assert row["dependsOn"] == [DEAD_1]         # the legal entry did not land
    refused = sup.run.read_status()[sup.REFUSED_WORKER_SPLIT_STATUS_KEY]
    assert refused["taskId"] == TASK_1
    assert any("proposed_task_updates" in e for e in refused["errors"])
    extra = sup._worker_assignment_extra(_task(), refused_split=refused)
    assert "## Your last proposed task split was refused" in extra
    assert "999-nowhere" in extra
    assert "workerRepointUpdates" not in sup.run.read_status()


def test_the_repoint_rides_the_real_dispatch_after_the_split(tmp_path):
    """End to end through `_step_worker`, and the ordering guarantee of the
    call site: one reply carrying BOTH `proposed_tasks` and
    `proposed_task_updates` has the split inserted FIRST, so the re-point
    onto the brand-new child resolves instead of being refused as an
    unresolved id -- and `state.task_blocked_by` goes from naming the doomed
    task to empty."""
    root = tmp_path / "run"
    sup = _sup(root, _doomed_graph(), iterations=1)
    sup._approach = 1
    assert task_blocked_by(sup.run.read_tasks()["tasks"]) == {DOOMED_1: [DEAD_1]}
    payload = {"verdict": "no_verdict",
               "proposed_tasks": [{"id": "021a", "title": "the residual gap",
                                   "successCriteria": "the gap is closed"}],
               "proposed_task_updates": [
                   _update(depends=["021a"],
                           reason="005 failed; 021a carves its residual gap")]}
    sup.runner = _ScriptedAgent({"worker": [lambda s, p="": _ok(
        "working\n" + json.dumps(payload))]})
    sup.runner.sup = sup                 # type: ignore[attr-defined]

    asyncio.run(asyncio.wait_for(sup._step_worker(), 30.0))

    tasks = sup.run.read_tasks()["tasks"]
    assert any(t["id"] == "021a" and is_worker_origin(t) for t in tasks)
    assert next(t for t in tasks if t["id"] == DOOMED_1)["dependsOn"] == ["021a"]
    assert task_blocked_by(tasks) == {}
    assert sup.run.read_status()["workerRepointUpdates"] == {"1": 1}


# ---------------------------------------------------------------------------
# worker.md: the superseded prose is gone, the new mechanism is described
# ---------------------------------------------------------------------------

def test_the_superseded_smallest_atomic_prose_is_gone():
    assert "smallest atomic first piece" not in WORKER_MD
    assert "describe the remainder precisely in the notes file" not in WORKER_MD


def test_worker_md_describes_the_proposed_tasks_mechanism():
    assert "proposed_tasks" in WORKER_MD
    assert "split" in WORKER_MD.lower()


def test_worker_md_documents_the_repoint_field_and_every_guard_it_enforces():
    """#209 defect 7: the prompt IS the contract surface here, so every
    guard `plan_ingestion.lint_worker_task_updates` enforces must be stated
    to the hat that has to satisfy it -- otherwise the shipped prompt drifts
    from the lint and the worker learns the rules by being refused."""
    assert "proposed_task_updates" in WORKER_MD
    # Matched against a whitespace-normalised copy so a reflow of the prompt's
    # own line breaks cannot fail a pin that is about the RULE, not the
    # wrapping -- the same tolerance `tests/test_prompt_dependency_doom.py`
    # buys with its `\s*\n?\s*` regexes.
    flat = " ".join(WORKER_MD.split())
    for clause in (
        '"task_id"', '"dependsOn"', '"reason"',          # all three mandatory
        "All three fields are required",
        "refuses the WHOLE array",
        "`pending` task the harness itself already marks blocked",
        "`taskBlockedBy`",
        "drops only edges naming a `failed` or `skipped` task",
        "names no `failed` or `skipped` task itself",
        "names no id the plan does not hold",
        "closes no cycle",
        "do not want to wait for is alive",
        "never changes a status, a title or a `successCriteria`",
        "refused outright when no task was assigned to you this iteration",
        "its own per-approach budget",
    ):
        assert clause in flat, clause
