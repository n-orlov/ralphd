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
    RunDir,
    TASK_ORIGIN_WORKER,
    atomic_write_json,
    format_task_status,
    insert_worker_split_tasks,
    is_worker_origin,
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
    """`dependsOn` is scoped to the proposed batch itself -- a split cannot
    silently wire itself to unrelated plan work."""
    errors = plan_ingestion.lint_worker_split(
        [{"id": "021a", "title": "x", "successCriteria": "y",
          "dependsOn": ["999-not-in-batch"]}],
        assigned_task_id=TASK_1, existing_ids=set())
    assert any("unknown task" in e for e in errors)


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

def _sup(root: Path, **cfg_kw) -> LoopSupervisor:
    root.mkdir(parents=True, exist_ok=True)
    run = RunDir(root=root)
    run.update_status(state="running")
    kw = {"iterations": 15, "max_approaches": 1, "vigilant": False,
          "infra_retry_backoff_s": [0.0], "infra_retry_backoff_max_s": 0.0,
          **cfg_kw}
    sup = LoopSupervisor(JobConfig(run_id="v014-worker-split", **kw), run, root)
    atomic_write_json(run.tasks_file, {"version": 1, "tasks": [_task()]})
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
# worker.md: the superseded prose is gone, the new mechanism is described
# ---------------------------------------------------------------------------

def test_the_superseded_smallest_atomic_prose_is_gone():
    assert "smallest atomic first piece" not in WORKER_MD
    assert "describe the remainder precisely in the notes file" not in WORKER_MD


def test_worker_md_describes_the_proposed_tasks_mechanism():
    assert "proposed_tasks" in WORKER_MD
    assert "split" in WORKER_MD.lower()
