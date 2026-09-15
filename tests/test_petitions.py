"""D.5 -- the petition/adjudication hat's own subject matter, folded into
one module (task 026, requirement D.5). Absorbs, whole, the seven
`test_*_task*.py` modules whose subject is petition filing/discovery,
mid-run adjudication, or an adjudication's downstream effect on the run
record or `tasks.json`:

* `tests/test_adjudicated_petitions_history_task014.py` (task 014,
  requirement F.1, #144): a petition present in
  `petition-adjudications.json` is rendered to review as HISTORY, never as
  open or re-filed, and a `midRunAdjudication: true` review episode may
  answer `verdict: "adjudicated"` (or omit `verdict`) without producing a
  `contract.agent_fault`, while the ordinary run-ending review's own
  schema is unchanged.
* `tests/test_petitioned_hold_not_starve_task014.py` (task 014,
  requirement C.2, #149): a `petitioned` dependency HOLDS the worker loop
  (the stagnation counter is exempted, never incremented or reset while
  the actionable remainder is blocked on a `petitioned` ancestor) instead
  of STARVING it, and the same bounded, off-budget mid-run adjudication
  episode `test_midrun_adjudication_episode.py` already pins for the
  `pending_verify` branch also fires proactively the moment the hold is
  detected. This section imports its scaffolding
  (`_make_petitions_file`/`_ok`/`_ScriptedAgent`/`_worker_supervisor`, kept
  under an `_episode_`-prefixed alias below to avoid shadowing this
  module's OWN, differently-shaped section-1 helpers of the same bare
  names) from that still-separate module rather than a second copy.
* `tests/test_petition_confirmed_closes_task_task015.py` (task 015, PRD
  requirement C.3, #149): once review CONFIRMS the obligation a
  `petitioned` task filed, the task moves to `skipped` with the
  adjudication on its own `notes`, and every OTHER task's `dependsOn` is
  re-pointed away from it -- to nothing by default, or to the named
  `replacement_task_id` when it names a real task in the current plan.
* `tests/test_refuted_petitions_rendered_task002.py` (task 002, approach
  02, cure for finding 2 of the approach-01 review, requirement B.1): a
  REFUTED petition adjudication -- and its remedy text -- must appear in
  the run record, not just CONFIRMED ones, driven through the SAME pair of
  calls (`record_confirmed_adjudications` + `record_refuted_adjudications`)
  the engine actually makes at review-verdict time.
* `tests/test_refuted_adjudications_persisted_task016.py` (task 016,
  approach 02, cure for task 002; review finding 2, B.1): the SAME claim,
  narrowed to the review's own reproduction -- a REFUTED entry passed to
  `record_confirmed_adjudications` ALONE (no second, caller-side witness
  write) must still reach `run-record.json`/`.md`, while the CONFIRMED-only
  exemption store and its tamper/MAC behaviour stay unchanged.
* `tests/test_petitions_scoped_to_wave_task018.py` (task 018, requirement
  E.1/E.3, #150): petition discovery and exhaustion filing are scoped to
  the CURRENT wave's file only -- never an older wave's, never the
  newest-older-wave `files[-1]` fallback -- and an obligation naming no
  task in the current plan and no requirement of the contract is filtered
  out as history even within the current wave's own file.
* `tests/test_petition_writeback_task019.py` (task 019, PRD requirement
  E.2, #150): when an adjudication is recorded, the engine appends
  `**Adjudication**: CONFIRMED|REFUTED -- <remedy>` to that petition's own
  section in the tracked wave file, committing it with the run's git
  identity when the workspace is clean and deferring to the next task
  boundary when it is dirty.

Renamed to avoid collisions across the seven originals, all mechanical
(same body, new name -- see `docs/test-suite-audit.md` § "Subsumed" for
the row naming each rename): the four modules' own `OBLIGATION` constants
became `HISTORY_OBLIGATION`/`CLOSES_OBLIGATION`/
`REFUTED_RENDERED_OBLIGATION`/`REFUTED_PERSISTED_OBLIGATION`; the two
`REMEDY` constants became `REFUTED_RENDERED_REMEDY`/
`REFUTED_PERSISTED_REMEDY`; task 018's own `_task(tid, title)` became
`_wave_task` (task 015's own `_task(id_, **over)` keeps the bare name);
each section's own `_supervisor` gained a section suffix
(`_supervisor_history`/`_supervisor_closes_task`/`_supervisor_writeback`);
and one test name -- `test_run_record_markdown_renders_the_refuted_
obligation_and_remedy`, pinned once in each of the two REFUTED-rendering
sections with a genuinely different scenario -- gained a suffix naming
that scenario in each (`..._with_witness_log` for task 002's two-call
pipeline, `..._confirmed_store_only` for task 016's single-call one). The
byte-identical `_run(tmp_path) -> RunDir` helper both of the REFUTED
sections defined is now shared, defined once.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path

from ralphd.engine import contracts, petitions, run_record, transitions
from ralphd.engine.config import JobConfig
from ralphd.engine.loop import LoopSupervisor
from ralphd.engine.petitions import (
    read_adjudications,
    read_confirmed_adjudications,
    record_confirmed_adjudications,
    record_refuted_adjudications,
)
from ralphd.engine.runner import IterationResult
from ralphd.engine.state import RunDir, atomic_write_json, read_json
from tests.conftest import make_supervisor, supervisor_factory

from test_midrun_adjudication_episode import (
    _make_petitions_file as _episode_petitions_file,
    _ok as _episode_ok,
    _ScriptedAgent as _EpisodeScriptedAgent,
    _worker_supervisor as _episode_worker_supervisor,
)

HISTORY_OBLIGATION = "Task 1: awaiting an adjudication episode"


def _ok(text: str) -> IterationResult:
    r = IterationResult(exit_code=0)
    r.final_text = text
    r.duration_s = 1.0
    r.usage = {"input": 1, "output": 1, "totalTokens": 2}
    return r


# The shared harness (tests/conftest.py, G.1): this section's run dir and
# workspace sit side by side under the test's own tmp_path.
_supervisor_history = supervisor_factory(run_id="task014-history", run_root="run",
                                         workspace="ws")


def test_empty_when_nothing_confirmed(tmp_path):
    sup = _supervisor_history(tmp_path)
    assert petitions.adjudicated_petitions_review_context(sup.run) == ""
    assert sup._adjudicated_petitions_review_context() == ""


def test_confirmed_obligation_renders_as_settled_history(tmp_path):
    sup = _supervisor_history(tmp_path)
    petitions.record_confirmed_adjudications(sup.run, [
        {"obligation": HISTORY_OBLIGATION, "adjudication": "CONFIRMED"},
    ])
    ctx = petitions.adjudicated_petitions_review_context(sup.run)
    assert "Adjudicated petitions" in ctx
    assert "history" in ctx.lower()
    assert HISTORY_OBLIGATION in ctx
    assert "CONFIRMED" in ctx
    # never rendered as something still open for a hearing
    assert "MUST" not in ctx.split("CONFIRMED")[0] or "not open" in ctx.lower()

    method_ctx = sup._adjudicated_petitions_review_context()
    assert HISTORY_OBLIGATION in method_ctx


def test_history_context_joins_review_extra_context(tmp_path):
    sup = _supervisor_history(tmp_path)
    petitions.record_confirmed_adjudications(sup.run, [
        {"obligation": HISTORY_OBLIGATION, "adjudication": "CONFIRMED"},
    ])
    ctx = sup._review_extra_context()
    assert HISTORY_OBLIGATION in ctx
    assert "Adjudicated petitions" in ctx


def test_tampered_record_is_treated_as_empty_history(tmp_path, monkeypatch):
    sup = _supervisor_history(tmp_path)
    petitions.record_confirmed_adjudications(sup.run, [
        {"obligation": HISTORY_OBLIGATION, "adjudication": "CONFIRMED"},
    ])
    monkeypatch.setattr(petitions, "read_confirmed_adjudications",
                        lambda run: ([], True))
    assert petitions.adjudicated_petitions_review_context(sup.run) == ""


def test_confirmed_obligation_is_not_also_rendered_as_open(tmp_path):
    """The obligation appearing in the history block must not simultaneously
    appear in the OPEN petitions list -- `open_unconfirmed_petitions`
    already excludes it (task 001/#112); this pins the combination holds
    for a filing that also exists under the workspace."""
    reports = tmp_path / "ws" / "artifacts" / "reports"
    reports.mkdir(parents=True)
    (reports / "v0.16-petitions.md").write_text(
        f"# v0.16 petitions\n\n## Petition\n\n"
        f"**Obligation**: {HISTORY_OBLIGATION}\n"
        "**Type**: environmental\n"
        "**Evidence**: some evidence.\n")
    sup = _supervisor_history(tmp_path)
    petitions.record_confirmed_adjudications(sup.run, [
        {"obligation": HISTORY_OBLIGATION, "adjudication": "CONFIRMED"},
    ])
    open_ctx = sup._petitions_review_context()
    history_ctx = sup._adjudicated_petitions_review_context()
    assert "Open impossibility petitions" not in open_ctx
    assert HISTORY_OBLIGATION not in open_ctx
    assert HISTORY_OBLIGATION in history_ctx


# ---------------------------------------------------------------------------
# 2. Schema: MIDRUN_REVIEW_SCHEMA vs the ordinary review schema.
# ---------------------------------------------------------------------------

def test_midrun_schema_accepts_adjudicated_verdict():
    payload = {"verdict": "adjudicated",
              "petition_adjudications": [
                  {"obligation": HISTORY_OBLIGATION, "adjudication": "CONFIRMED"}]}
    assert contracts.validate_against(transitions.MIDRUN_REVIEW_SCHEMA, payload) == []


def test_midrun_schema_accepts_missing_verdict():
    payload = {"petition_adjudications": [
        {"obligation": HISTORY_OBLIGATION, "adjudication": "REFUTED", "remedy": "do X"}]}
    assert contracts.validate_against(transitions.MIDRUN_REVIEW_SCHEMA, payload) == []


def test_midrun_schema_still_rejects_an_illegal_verdict_value():
    payload = {"verdict": "banana"}
    assert contracts.validate_against(transitions.MIDRUN_REVIEW_SCHEMA, payload)


def test_ordinary_review_schema_unchanged_still_requires_verdict():
    assert contracts.validate_payload("review", {}) != []


def test_ordinary_review_schema_unchanged_still_rejects_adjudicated():
    errors = contracts.validate_payload("review", {"verdict": "adjudicated"})
    assert errors


# ---------------------------------------------------------------------------
# 3. `_review_contract_ladder`/`_review_verdict`'s `mid_run` flag.
# ---------------------------------------------------------------------------

def test_ladder_accepts_adjudicated_verdict_when_mid_run_and_skips_the_ladder(tmp_path):
    sup = _supervisor_history(tmp_path)
    result = _ok(json.dumps({
        "verdict": "adjudicated",
        "petition_adjudications": [
            {"obligation": HISTORY_OBLIGATION, "adjudication": "CONFIRMED"}],
    }))
    out = asyncio.run(sup._review_contract_ladder(result, mid_run=True))
    # accepted on the first attempt -- the SAME IterationResult object,
    # never routed through enforce_contract's re-ask/agent-fault path.
    assert out is result

    verified, payload, errors = sup._review_verdict(result, mid_run=True)
    assert errors == []
    assert payload["verdict"] == "adjudicated"


def test_ladder_accepts_missing_verdict_when_mid_run(tmp_path):
    sup = _supervisor_history(tmp_path)
    result = _ok(json.dumps({
        "petition_adjudications": [
            {"obligation": HISTORY_OBLIGATION, "adjudication": "CONFIRMED"}],
    }))
    out = asyncio.run(sup._review_contract_ladder(result, mid_run=True))
    assert out is result
    verified, payload, errors = sup._review_verdict(result, mid_run=True)
    assert errors == []


def test_the_same_reply_is_schema_invalid_for_an_ordinary_non_midrun_call(tmp_path):
    """Proves the differentiation is real: the identical payload that
    `mid_run=True` accepts outright is still schema-invalid under the
    ordinary (unchanged) review contract -- pinned directly against
    `contracts.validate_payload`, the one call `_review_verdict(...,
    mid_run=False)` makes when computing `contract_errors`."""
    sup = _supervisor_history(tmp_path)
    result = _ok(json.dumps({"verdict": "adjudicated"}))
    _verified, _payload, errors = sup._review_verdict(result, mid_run=False)
    assert errors  # not accepted by the ordinary, unchanged review schema


# ---------------------------------------------------------------------------
# 4. End-to-end through `_maybe_run_midrun_adjudication_episode`.
# ---------------------------------------------------------------------------

def _make_petitions_file(workspace: Path, obligation: str,
                         wave: str = "v0.18") -> Path:
    reports = workspace / "artifacts" / "reports"
    reports.mkdir(parents=True)
    path = reports / f"{wave}-petitions.md"
    path.write_text(
        f"# {wave} petitions\n\n"
        f"## Petition: {obligation}\n\n"
        f"**Obligation**: {obligation}\n"
        "**Type**: self-referential\n"
        "**Evidence**: fixture evidence for task 014's own test.\n")
    return path


class _ScriptedAgent:
    """Same shape `tests/test_midrun_adjudication_episode.py` already
    uses: one script per phase, keyed off the CURRENT `status.json`
    phase."""

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


def _worker_supervisor(tmp_path: Path) -> tuple[LoopSupervisor, RunDir, Path]:
    root = tmp_path / "run"
    root.mkdir(parents=True)
    run = RunDir(root=root)
    run.update_status(state="running")
    atomic_write_json(run.tasks_file, {"version": 1, "tasks": [
        {"id": "1", "title": "awaiting an adjudication episode",
         "status": "awaiting-validation", "successCriteria": "n/a"},
    ]})
    cfg = JobConfig(run_id="task014-episode", iterations=10, max_approaches=3,
                    vigilant=True, on_complete="exit",
                    infra_retry_backoff_s=[0.0], infra_retry_backoff_max_s=0.0,
                    infra_outage_budget_s=1000.0)
    sup = LoopSupervisor(cfg, run, root)
    sup._approach = 1
    return sup, run, root


def _adjudicated_review(sup, prompt=""):
    return _ok(json.dumps({
        "verdict": "adjudicated",
        "petition_adjudications": [
            {"obligation": HISTORY_OBLIGATION, "adjudication": "CONFIRMED"}],
    }))


def _verified(sup, prompt=""):
    return _ok(json.dumps({"task_id": "1", "verdict": "verified"}))


def test_episode_answering_adjudicated_settles_with_no_agent_fault(tmp_path):
    sup, run, workspace = _worker_supervisor(tmp_path)
    _make_petitions_file(workspace, HISTORY_OBLIGATION)

    agent = _ScriptedAgent({
        "worker": [lambda s, p: _ok(json.dumps({"verdict": "no_verdict"}))],
        "review": [_adjudicated_review],
        "verify": [_verified],
    })
    agent.sup = sup  # type: ignore[attr-defined]
    sup.runner = agent  # type: ignore[assignment]

    asyncio.run(sup._step_worker())

    # exactly one "review" call -- the episode settled on its first
    # attempt, never re-asked.
    assert agent.calls.get("review") == 1
    assert agent.calls.get("verify") == 1

    events = [json.loads(x) for x in
              (run.root / "events.jsonl").read_text().splitlines() if x.strip()]
    assert not any(e.get("type") == "contract.agent_fault" for e in events)
    assert not any(e.get("type") == "contract.violation" and e.get("hat") == "review"
                   for e in events)

    confirmed, tampered = petitions.confirmed_obligations(sup.run)
    assert not tampered
    assert any(HISTORY_OBLIGATION.lower() in c or c in HISTORY_OBLIGATION.lower()
              for c in confirmed)

    task = sup.run.read_tasks()["tasks"][0]
    assert task["status"] == "validated"


# ---------------------------------------------------------------------------
# 5. A `petitioned` dependency HOLDS the worker loop, never STARVES it
#    (task 014, requirement C.2, #149). Scaffolding (`_episode_*`) is
#    imported from `test_midrun_adjudication_episode.py`, still a
#    separate module, under an alias to avoid shadowing section 4's own
#    differently-shaped `_make_petitions_file`/`_ok`/`_ScriptedAgent`/
#    `_worker_supervisor` above.
# ---------------------------------------------------------------------------

# The held task's own filed obligation. It names task `A` of `_held_plan`
# below because that is what a real filing names: task 018 (requirement
# E.1, #150) treats a petition whose obligation names no task in the
# current `tasks.json` and no requirement of the contract as history, and
# history is never surfaced to review at all -- so an obligation invented
# out of thin air would make this scenario un-adjudicable rather than held.
HELD_OBLIGATION = "Task A: the criterion names its own not-yet-existing sha"


def _confirming_review(sup, prompt=""):
    """The review hat adjudicating `HELD_OBLIGATION` CONFIRMED -- the same
    shape `test_midrun_adjudication_episode._confirming_review` has, over
    this module's own held obligation."""
    return _episode_ok("adjudicating the open petition\n" + json.dumps({
        "verdict": "rejected",
        "petition_adjudications": [
            {"obligation": HELD_OBLIGATION, "adjudication": "CONFIRMED"}],
    }))


def _held_plan() -> list[dict]:
    """`A` is `petitioned` (held, not dead) with a matching filed
    obligation; `B` depends on it directly and is the plan's only other
    task -- so the entire actionable remainder (just `B`) is blocked on
    the petitioned ancestor and `scheduler.select_next_task` returns
    `None` every single pass, exactly the scenario this task's fix
    targets."""
    return [
        {"id": "A", "title": "the petitioned task", "status": "petitioned",
         "successCriteria": "n/a"},
        {"id": "B", "title": "depends on A", "status": "pending",
         "dependsOn": ["A"], "successCriteria": "n/a"},
    ]


def _no_verdict(sup, prompt: str = "") -> IterationResult:
    return _episode_ok(json.dumps({"verdict": "no_verdict"}))


def test_a_held_pass_runs_the_episode_and_still_dispatches_the_worker(tmp_path):
    """Node id: test_a_held_pass_runs_the_episode_and_still_dispatches_the_worker
    -- the immediate-adjudication half of requirement C.2."""
    sup, run, workspace = _episode_worker_supervisor(tmp_path, iterations=1)
    _episode_petitions_file(workspace, HELD_OBLIGATION)
    atomic_write_json(sup.run.tasks_file,
                      {"version": 1, "tasks": _held_plan()})

    agent = _EpisodeScriptedAgent({
        "worker": [_no_verdict],
        "review": [_confirming_review],
    })
    agent.sup = sup  # type: ignore[attr-defined]
    sup.runner = agent  # type: ignore[assignment]

    asyncio.run(sup._step_worker())

    # The mid-run adjudication episode ran, on the review-hat's model,
    # BEFORE this pass's ordinary worker dispatch -- proactively, at the
    # point the hold was detected, not merely as a side effect of some
    # other branch.
    assert agent.calls.get("review") == 1
    assert agent.model_calls["review"][0] == sup.cfg.model_for("review")
    assert "review" in agent.order and "worker" in agent.order
    assert agent.order.index("review") < agent.order.index("worker")

    # The worker was NOT skipped ("instead of idling" replaces the
    # episode's own dispatch point, not the worker's ordinary one) --
    # a held plan still gets its normal chance to reply.
    assert agent.calls.get("worker") == 1

    # Off-budget: the episode never competed with the run's own iteration
    # budget (iterations=1 covered exactly the one worker dispatch).
    assert sup.run.read_status().get(
        "iterationsRefunded", {}).get("grace", 0) >= 1
    charged = (sup.iterations_used - sup._infra_refunded
              - sup._grace_refunded - sup._steer_refunded)
    assert charged == 1

    # tasks.json is untouched by the hold itself -- A is still `petitioned`
    # (closing it on a CONFIRMED adjudication is a later task's scope).
    tasks = {t["id"]: t["status"] for t in sup.run.read_tasks()["tasks"]}
    assert tasks == {"A": "petitioned", "B": "pending"}


def test_repeated_held_passes_never_trip_the_stagnation_counter(tmp_path):
    """Node id:
    test_repeated_held_passes_never_trip_the_stagnation_counter -- the
    stagnation-exemption half of requirement C.2. Three held passes in a
    row (the run's own iteration budget here, chosen to land exactly on
    task 011's own `LoopSupervisor.PETITION_HELD_MAX_NO_TASK_ITERATIONS`
    bound, requirement C.4, #187 -- so this scenario ends on the run's
    ordinary budget exhaustion, never on that SEPARATE bound), each with
    `tasks.json` unchanged, no commit, no contract progress and no tool
    activity -- exactly the shape that trips `stagnant >= 3` (guard 10,
    `stagnation_exhausted`) for an ordinary worker loop -- must NOT fail
    or replan this approach; the run simply runs its ordinary iteration
    budget out and tails off. A run with MORE budget than the bound is
    task 011's own `tests/test_petition_hold_bound.py`'s scope, not this
    one's -- that module is what pins the bound itself actually cutting a
    longer hold off."""
    sup, run, workspace = _episode_worker_supervisor(
        tmp_path, iterations=3, max_approaches=2)
    _episode_petitions_file(workspace, HELD_OBLIGATION)
    atomic_write_json(sup.run.tasks_file,
                      {"version": 1, "tasks": _held_plan()})

    agent = _EpisodeScriptedAgent({
        "worker": [_no_verdict],
        "review": [_confirming_review],
    })
    agent.sup = sup  # type: ignore[attr-defined]
    sup.runner = agent  # type: ignore[assignment]

    outcome = asyncio.run(sup._step_worker())

    # Every one of the 3 charged iterations dispatched the ordinary
    # worker -- none of them was skipped -- and NONE of them tripped
    # guard 10: the outcome is the ordinary budget-exhaustion tail, never
    # an approach failure/replan over a hold that was always going to
    # resolve on its own. Landing exactly on task 011's own bound means
    # the run's OWN `iterations` budget is what ends this pass, one beat
    # before that bound's own "already spent N held passes" check would
    # otherwise have fired on a hypothetical 4th pass.
    assert agent.calls.get("worker") == 3
    assert outcome == ("tail",)

    # The bounded episode ran exactly ONCE for this obligation -- calling
    # it again on every later held pass is a no-op, never a repeat LLM
    # call (`self._petition_adjudication_seen` dedup, unchanged by this
    # task).
    assert agent.calls.get("review") == 1

    # A held run never gets silently failed/replanned: `self._approach`
    # is exactly where it started (a `stagnation_exhausted` outcome would
    # have returned "advance"/"replan" above instead), and tasks.json is
    # exactly where it started too.
    assert sup._approach == 1
    tasks = {t["id"]: t["status"] for t in sup.run.read_tasks()["tasks"]}
    assert tasks == {"A": "petitioned", "B": "pending"}


# ---------------------------------------------------------------------------
# 6. CONFIRMED closes a `petitioned` task, dependents re-pointed (task
#    015, PRD requirement C.3, #149).
# ---------------------------------------------------------------------------

TASK_ID = "001"
DEPENDENT_ID = "002"
REPLACEMENT_ID = "003"
CLOSES_OBLIGATION = "Task 001: make the impossible thing work"


def _task(id_=TASK_ID, **over) -> dict:
    task = {"id": id_, "title": "make the impossible thing work",
            "status": "petitioned", "successCriteria": "criterion 2 holds",
            "validationNotes": "criterion 2 cannot be verified as written"}
    task.update(over)
    return task


_supervisor_closes_task = lambda root, workspace=None, **cfg: make_supervisor(
    root, run_id="task015-confirmed-closes-task", cfg_defaults=False,
    workspace=workspace or root.parent / 'ws', iterations=5, max_approaches=1,
    vigilant=False, on_complete="exit", **cfg)


def _write_plan(sup: LoopSupervisor, tasks: list[dict]) -> None:
    atomic_write_json(sup.run.tasks_file, {"version": 1, "tasks": tasks})


def test_apply_confirmed_petition_adjudications_moves_the_task_to_skipped():
    tasks_doc = {"version": 1, "tasks": [_task()]}
    changed = petitions.apply_confirmed_petition_adjudications(tasks_doc, [
        {"obligation": CLOSES_OBLIGATION, "adjudication": "CONFIRMED"},
    ])

    assert changed == [TASK_ID]
    task = tasks_doc["tasks"][0]
    assert task["status"] == "skipped"
    assert "CONFIRMED" in task["notes"]
    assert CLOSES_OBLIGATION in task["notes"]


def test_apply_confirmed_petition_adjudications_appends_to_existing_notes():
    tasks_doc = {"version": 1, "tasks": [_task(notes="an earlier note")]}
    petitions.apply_confirmed_petition_adjudications(tasks_doc, [
        {"obligation": CLOSES_OBLIGATION, "adjudication": "CONFIRMED"},
    ])

    notes = tasks_doc["tasks"][0]["notes"]
    assert notes.startswith("an earlier note\n")
    assert "CONFIRMED" in notes


def test_apply_confirmed_petition_adjudications_leaves_non_matching_tasks_alone():
    tasks_doc = {"version": 1,
                 "tasks": [_task(), _task(id_=DEPENDENT_ID, title="unrelated")]}
    petitions.apply_confirmed_petition_adjudications(tasks_doc, [
        {"obligation": CLOSES_OBLIGATION, "adjudication": "CONFIRMED"},
    ])

    other = next(t for t in tasks_doc["tasks"] if t["id"] == DEPENDENT_ID)
    assert other["status"] == "petitioned"


def test_apply_confirmed_petition_adjudications_ignores_refuted_entries():
    tasks_doc = {"version": 1, "tasks": [_task()]}
    changed = petitions.apply_confirmed_petition_adjudications(tasks_doc, [
        {"obligation": CLOSES_OBLIGATION, "adjudication": "REFUTED",
         "remedy": "do something else"},
    ])

    assert changed == []
    assert tasks_doc["tasks"][0]["status"] == "petitioned"


def test_apply_confirmed_petition_adjudications_is_a_noop_with_no_adjudications():
    tasks_doc = {"version": 1, "tasks": [_task()]}
    assert petitions.apply_confirmed_petition_adjudications(tasks_doc, None) == []
    assert petitions.apply_confirmed_petition_adjudications(tasks_doc, []) == []
    assert tasks_doc["tasks"][0]["status"] == "petitioned"


def test_a_dependents_dependson_loses_the_confirmed_task_with_no_replacement():
    tasks_doc = {"version": 1, "tasks": [
        _task(),
        {"id": DEPENDENT_ID, "title": "depends on it",
         "status": "pending", "successCriteria": "x",
         "dependsOn": [TASK_ID]},
    ]}
    petitions.apply_confirmed_petition_adjudications(tasks_doc, [
        {"obligation": CLOSES_OBLIGATION, "adjudication": "CONFIRMED"},
    ])

    dependent = next(t for t in tasks_doc["tasks"] if t["id"] == DEPENDENT_ID)
    assert dependent["dependsOn"] == []


def test_a_dependents_dependson_gains_the_named_replacement_task():
    tasks_doc = {"version": 1, "tasks": [
        _task(),
        {"id": REPLACEMENT_ID, "title": "the replacement",
         "status": "pending", "successCriteria": "y"},
        {"id": DEPENDENT_ID, "title": "depends on it",
         "status": "pending", "successCriteria": "x",
         "dependsOn": [TASK_ID]},
    ]}
    petitions.apply_confirmed_petition_adjudications(tasks_doc, [
        {"obligation": CLOSES_OBLIGATION, "adjudication": "CONFIRMED",
         "replacement_task_id": REPLACEMENT_ID},
    ])

    dependent = next(t for t in tasks_doc["tasks"] if t["id"] == DEPENDENT_ID)
    assert dependent["dependsOn"] == [REPLACEMENT_ID]


def test_a_replacement_task_id_naming_no_real_task_repoints_to_nothing():
    tasks_doc = {"version": 1, "tasks": [
        _task(),
        {"id": DEPENDENT_ID, "title": "depends on it",
         "status": "pending", "successCriteria": "x",
         "dependsOn": [TASK_ID]},
    ]}
    petitions.apply_confirmed_petition_adjudications(tasks_doc, [
        {"obligation": CLOSES_OBLIGATION, "adjudication": "CONFIRMED",
         "replacement_task_id": "does-not-exist"},
    ])

    dependent = next(t for t in tasks_doc["tasks"] if t["id"] == DEPENDENT_ID)
    assert dependent["dependsOn"] == []


def test_a_dependent_naming_both_the_task_and_its_replacement_has_no_duplicate():
    tasks_doc = {"version": 1, "tasks": [
        _task(),
        {"id": REPLACEMENT_ID, "title": "the replacement",
         "status": "pending", "successCriteria": "y"},
        {"id": DEPENDENT_ID, "title": "depends on both",
         "status": "pending", "successCriteria": "x",
         "dependsOn": [TASK_ID, REPLACEMENT_ID]},
    ]}
    petitions.apply_confirmed_petition_adjudications(tasks_doc, [
        {"obligation": CLOSES_OBLIGATION, "adjudication": "CONFIRMED",
         "replacement_task_id": REPLACEMENT_ID},
    ])

    dependent = next(t for t in tasks_doc["tasks"] if t["id"] == DEPENDENT_ID)
    assert dependent["dependsOn"] == [REPLACEMENT_ID]


def test_a_dependent_with_no_dependson_at_all_is_left_alone():
    tasks_doc = {"version": 1, "tasks": [
        _task(),
        {"id": DEPENDENT_ID, "title": "no deps", "status": "pending",
         "successCriteria": "x"},
    ]}
    petitions.apply_confirmed_petition_adjudications(tasks_doc, [
        {"obligation": CLOSES_OBLIGATION, "adjudication": "CONFIRMED"},
    ])

    dependent = next(t for t in tasks_doc["tasks"] if t["id"] == DEPENDENT_ID)
    assert "dependsOn" not in dependent


def test_supervisor_write_back_persists_the_confirmed_transition(tmp_path):
    sup = _supervisor_closes_task(tmp_path / "run")
    _write_plan(sup, [
        _task(),
        {"id": DEPENDENT_ID, "title": "depends on it", "status": "pending",
         "successCriteria": "x", "dependsOn": [TASK_ID]},
    ])

    changed = sup._apply_confirmed_petition_adjudications([
        {"obligation": CLOSES_OBLIGATION, "adjudication": "CONFIRMED"},
    ])

    assert changed == [TASK_ID]
    on_disk = {t["id"]: t for t in sup.run.read_tasks()["tasks"]}
    assert on_disk[TASK_ID]["status"] == "skipped"
    assert "CONFIRMED" in on_disk[TASK_ID]["notes"]
    assert on_disk[DEPENDENT_ID]["dependsOn"] == []


def test_supervisor_write_back_is_a_noop_with_no_adjudications(tmp_path):
    sup = _supervisor_closes_task(tmp_path / "run")
    _write_plan(sup, [_task()])
    before = sup.run.tasks_file.stat().st_mtime_ns

    assert sup._apply_confirmed_petition_adjudications(None) == []
    assert sup._apply_confirmed_petition_adjudications([]) == []

    assert sup.run.tasks_file.stat().st_mtime_ns == before
    assert sup.run.read_tasks()["tasks"][0]["status"] == "petitioned"


# ---------------------------------------------------------------------------
# 7. REFUTED adjudications must reach the run record too, not just
#    CONFIRMED ones (task 002, requirement B.1, and its cure task 016).
# ---------------------------------------------------------------------------

def _run(tmp_path: Path) -> RunDir:
    return RunDir(root=tmp_path / "run")


REFUTED_RENDERED_OBLIGATION = "Requirement Q -- task 002's own fixture obligation"
REFUTED_RENDERED_REMEDY = "Run the missing sweep at the post-fix sha and record it."


def _refute(run: RunDir, obligation: str = REFUTED_RENDERED_OBLIGATION,
            remedy: str = REFUTED_RENDERED_REMEDY) -> None:
    adjudications = [
        {"obligation": obligation, "adjudication": "REFUTED", "remedy": remedy},
    ]
    # The exact pair the engine calls at review-verdict time
    # (`LoopSupervisor._review_verdict` / `_apply_refuted_petition_
    # adjudications`): the CONFIRMED-only store drops it (unchanged
    # behaviour -- pinned below), the witness log keeps it.
    record_confirmed_adjudications(run, adjudications)
    record_refuted_adjudications(run, adjudications)


def test_confirmed_store_still_drops_the_refuted_entry(tmp_path):
    run = _run(tmp_path)
    _refute(run)
    entries, tampered = read_confirmed_adjudications(run)
    assert entries == []
    assert tampered is False


def test_petitions_section_includes_the_refuted_obligation_and_remedy(tmp_path):
    run = _run(tmp_path)
    _refute(run)
    section = run_record.build_petitions_section(run)
    assert section["tampered"] is False
    assert section["entries"] == [
        {"obligation": REFUTED_RENDERED_OBLIGATION, "adjudication": "REFUTED",
         "remedy": REFUTED_RENDERED_REMEDY}]


def test_run_record_json_carries_the_refuted_entry(tmp_path, monkeypatch):
    run = _run(tmp_path)
    _refute(run)
    monkeypatch.setattr(
        run_record.state, "amendment_entries", lambda config_root: [])
    record = run_record.build_run_record(
        run, run_id="task002-refuted", max_approaches=3,
        config_root=tmp_path / "config")
    entries = record["petitions"]["entries"]
    assert any(e["obligation"] == REFUTED_RENDERED_OBLIGATION
               and e["adjudication"] == "REFUTED"
               and e["remedy"] == REFUTED_RENDERED_REMEDY for e in entries)


def test_run_record_markdown_renders_the_refuted_obligation_and_remedy_with_witness_log(
        tmp_path, monkeypatch):
    run = _run(tmp_path)
    _refute(run)
    monkeypatch.setattr(
        run_record.state, "amendment_entries", lambda config_root: [])
    record = run_record.build_run_record(
        run, run_id="task002-refuted", max_approaches=3,
        config_root=tmp_path / "config")
    md = run_record.render_run_record_markdown(record)
    assert REFUTED_RENDERED_OBLIGATION in md
    assert "REFUTED" in md
    assert REFUTED_RENDERED_REMEDY in md


def test_confirmed_entry_still_renders_and_is_never_duplicated(tmp_path, monkeypatch):
    """A CONFIRMED entry must keep working exactly as before, and if the
    same obligation is ALSO witnessed as REFUTED (e.g. a duplicate/stale
    event), the CONFIRMED entry wins -- the obligation is rendered once,
    not twice."""
    run = _run(tmp_path)
    record_confirmed_adjudications(run, [
        {"obligation": REFUTED_RENDERED_OBLIGATION, "adjudication": "CONFIRMED"}])
    record_refuted_adjudications(run, [
        {"obligation": REFUTED_RENDERED_OBLIGATION, "adjudication": "REFUTED",
         "remedy": REFUTED_RENDERED_REMEDY}])
    section = run_record.build_petitions_section(run)
    assert section["entries"] == [
        {"obligation": REFUTED_RENDERED_OBLIGATION, "adjudication": "CONFIRMED"}]

    monkeypatch.setattr(
        run_record.state, "amendment_entries", lambda config_root: [])
    record = run_record.build_run_record(
        run, run_id="task002-both", max_approaches=3,
        config_root=tmp_path / "config")
    md = run_record.render_run_record_markdown(record)
    assert md.count(REFUTED_RENDERED_OBLIGATION) == 1


def test_no_petitions_still_renders_none(tmp_path):
    run = _run(tmp_path)
    section = run_record.build_petitions_section(run)
    assert section == {"entries": [], "tampered": False}


# ---------------------------------------------------------------------------
# 8. The SAME claim, narrowed to the review's own reproduction: a REFUTED
#    entry passed through `record_confirmed_adjudications` ALONE (no
#    second, caller-side write) must still reach the run record (task 016,
#    cure for task 002).
# ---------------------------------------------------------------------------

REFUTED_PERSISTED_OBLIGATION = "Requirement Z.9 -- task 016's own fixture obligation"
REFUTED_PERSISTED_REMEDY = ("Re-run the docker lane at the post-fix sha and record its exit "
                            "code in the lane record.")
CONFIRMED_OBLIGATION = "Requirement Y.1 -- a genuinely confirmed obligation"


def _refuted(obligation: str = REFUTED_PERSISTED_OBLIGATION,
            remedy: str = REFUTED_PERSISTED_REMEDY) -> list[dict]:
    return [{"obligation": obligation, "adjudication": "REFUTED",
             "remedy": remedy}]


def _record(run: RunDir, tmp_path: Path, monkeypatch) -> dict:
    monkeypatch.setattr(
        run_record.state, "amendment_entries", lambda config_root: [])
    return run_record.build_run_record(
        run, run_id="task016-refuted", max_approaches=3,
        config_root=tmp_path / "config")


def test_refuted_entry_is_persisted_by_record_confirmed_adjudications(tmp_path):
    run = _run(tmp_path)
    record_confirmed_adjudications(run, _refuted())
    assert run.petition_adjudications_file.exists()
    entries, tampered = read_adjudications(run)
    assert tampered is False
    assert entries == [{"obligation": REFUTED_PERSISTED_OBLIGATION,
                        "adjudication": "REFUTED", "remedy": REFUTED_PERSISTED_REMEDY}]
    # One store, one keyed MAC -- the same write path CONFIRMED entries use.
    doc = read_json(run.petition_adjudications_file)
    assert isinstance(doc.get("mac"), str) and doc["mac"]
    assert doc["digest"] == petitions._adjudications_digest(doc["entries"])


def test_petitions_section_carries_the_refuted_adjudication_and_remedy(tmp_path):
    run = _run(tmp_path)
    record_confirmed_adjudications(run, _refuted())
    section = run_record.build_petitions_section(run)
    assert section["tampered"] is False
    assert section["entries"] == [
        {"obligation": REFUTED_PERSISTED_OBLIGATION, "adjudication": "REFUTED",
         "remedy": REFUTED_PERSISTED_REMEDY}]


def test_run_record_json_carries_the_refuted_obligation_and_remedy(
        tmp_path, monkeypatch):
    run = _run(tmp_path)
    record_confirmed_adjudications(run, _refuted())
    record = _record(run, tmp_path, monkeypatch)
    entries = record["petitions"]["entries"]
    assert any(e["obligation"] == REFUTED_PERSISTED_OBLIGATION
               and e["adjudication"] == "REFUTED"
               and e["remedy"] == REFUTED_PERSISTED_REMEDY for e in entries), entries
    # ... and it survives the JSON round trip `run-record.json` itself is.
    round_tripped = json.loads(json.dumps(record))
    assert round_tripped["petitions"]["entries"] == entries


def test_run_record_markdown_renders_the_refuted_obligation_and_remedy_confirmed_store_only(
        tmp_path, monkeypatch):
    run = _run(tmp_path)
    record_confirmed_adjudications(run, _refuted())
    md = run_record.render_run_record_markdown(
        _record(run, tmp_path, monkeypatch))
    assert REFUTED_PERSISTED_OBLIGATION in md
    assert f"{REFUTED_PERSISTED_OBLIGATION} -- REFUTED" in md
    assert REFUTED_PERSISTED_REMEDY in md


def test_confirmed_and_refuted_both_render_once_each(tmp_path, monkeypatch):
    run = _run(tmp_path)
    record_confirmed_adjudications(run, [
        {"obligation": CONFIRMED_OBLIGATION, "adjudication": "CONFIRMED"},
        *_refuted(),
    ])
    md = run_record.render_run_record_markdown(
        _record(run, tmp_path, monkeypatch))
    assert md.count(REFUTED_PERSISTED_OBLIGATION) == 1
    assert md.count(CONFIRMED_OBLIGATION) == 1
    assert f"{CONFIRMED_OBLIGATION} -- CONFIRMED" in md


def test_a_refuted_entry_in_the_store_never_exempts_an_obligation(tmp_path):
    run = _run(tmp_path)
    record_confirmed_adjudications(run, _refuted())
    entries, tampered = read_confirmed_adjudications(run)
    assert entries == []
    assert tampered is False
    obligations, tampered = petitions.confirmed_obligations(run)
    assert obligations == set()
    assert tampered is False
    # ... so a finding naming that obligation stays BLOCKING, and a
    # `petitioned` task naming it is not "accepted".
    finding = {"severity": "blocking", "curable": True,
               "clause": REFUTED_PERSISTED_OBLIGATION, "evidence": "fixture evidence"}
    out = petitions.downgrade_confirmed_findings([finding], obligations)
    assert out[0]["severity"] == "blocking"
    task = {"id": "016", "status": "petitioned",
            "title": REFUTED_PERSISTED_OBLIGATION,
            "successCriteria": REFUTED_PERSISTED_OBLIGATION}
    assert petitions.petitioned_task_accepted(task, obligations) is False


def test_confirmed_exemption_still_granted_alongside_a_refuted_entry(tmp_path):
    run = _run(tmp_path)
    record_confirmed_adjudications(run, [
        {"obligation": CONFIRMED_OBLIGATION, "adjudication": "CONFIRMED"},
        *_refuted(),
    ])
    entries, tampered = read_confirmed_adjudications(run)
    assert tampered is False
    assert entries == [{"obligation": CONFIRMED_OBLIGATION,
                        "adjudication": "CONFIRMED"}]
    obligations, _ = petitions.confirmed_obligations(run)
    assert obligations == {CONFIRMED_OBLIGATION.strip().lower()}


def test_a_later_refuted_entry_never_downgrades_a_confirmed_one(tmp_path):
    run = _run(tmp_path)
    record_confirmed_adjudications(run, [
        {"obligation": CONFIRMED_OBLIGATION, "adjudication": "CONFIRMED"}])
    record_confirmed_adjudications(
        run, _refuted(obligation=CONFIRMED_OBLIGATION, remedy="ignored"))
    entries, tampered = read_adjudications(run)
    assert tampered is False
    assert entries == [{"obligation": CONFIRMED_OBLIGATION,
                        "adjudication": "CONFIRMED"}]
    obligations, _ = petitions.confirmed_obligations(run)
    assert obligations == {CONFIRMED_OBLIGATION.strip().lower()}


def test_editing_the_refuted_entry_is_detected_as_tampering(tmp_path):
    run = _run(tmp_path)
    record_confirmed_adjudications(run, _refuted())
    doc = read_json(run.petition_adjudications_file)
    doc["entries"][0]["adjudication"] = "CONFIRMED"
    doc["digest"] = petitions._adjudications_digest(doc["entries"])
    run.petition_adjudications_file.write_text(json.dumps(doc))
    entries, tampered = read_adjudications(run)
    assert tampered is True
    assert entries == []
    # The exemption read fails closed the same way, and the detection is
    # sticky for the rest of the run.
    obligations, tampered = petitions.confirmed_obligations(run)
    assert obligations == set()
    assert tampered is True
    section = run_record.build_petitions_section(run)
    assert section["tampered"] is True
    assert section["entries"] == []


def test_nothing_adjudicated_writes_nothing_and_renders_none(tmp_path):
    run = _run(tmp_path)
    record_confirmed_adjudications(run, [])
    record_confirmed_adjudications(run, [{"obligation": "  ",
                                          "adjudication": "REFUTED"}])
    record_confirmed_adjudications(run, [{"obligation": "x",
                                          "adjudication": "PENDING"}])
    assert not run.petition_adjudications_file.exists()
    assert run_record.build_petitions_section(run) == {
        "entries": [], "tampered": False}


# ---------------------------------------------------------------------------
# 9. Petition discovery and exhaustion filing are scoped to the CURRENT
#    wave (task 018, requirement E.1/E.3, #150).
# ---------------------------------------------------------------------------

CURRENT_CONTRACT = """# PRD v0.19 -- a note is not a rejection

## E -- petitions belong to their wave, adjudications survive (#150, #152)

1. Scoped discovery.
"""


def _write(workspace: Path, name: str, text: str) -> Path:
    reports = workspace / "artifacts" / "reports"
    reports.mkdir(parents=True, exist_ok=True)
    path = reports / name
    path.write_text(text)
    return path


def _section(obligation: str, ptype: str = "self-referential",
             evidence: str = "some evidence") -> str:
    return petitions.format_petition_section(obligation, ptype, evidence)


def test_a_foreign_older_waves_open_petition_is_not_surfaced(tmp_path):
    """v0.16/v0.17/v0.18 each carry an OPEN petition; this run's own
    contract names v0.19. None of the older waves' petitions comes back,
    even though every file is discovered on disk."""
    ws = tmp_path / "ws"
    for wave in ("v0.16", "v0.17", "v0.18"):
        _write(ws, f"{wave}-petitions.md",
               f"# {wave} petitions\n" + _section(f"{wave}'s own obligation"))
    _write(ws, "v0.19-petitions.md",
           "# v0.19 petitions\n" + _section("this run's own obligation"))

    found = petitions.open_petitions_in_workspace(ws, CURRENT_CONTRACT)

    assert len(found) == 1
    assert found[0]["obligation"] == "this run's own obligation"
    assert petitions.discover_petition_files(ws) and \
        len(petitions.discover_petition_files(ws)) == 4


def test_only_older_waves_exist_nothing_is_current(tmp_path):
    """No `v0.19-petitions.md` exists at all -- only v0.16-v0.18's history.
    Reading must never resurrect the newest OLD one as if it were current
    (the `files[-1]` bug this task fixes): the result is empty."""
    ws = tmp_path / "ws"
    for wave in ("v0.16", "v0.17", "v0.18"):
        _write(ws, f"{wave}-petitions.md",
               f"# {wave} petitions\n" + _section(f"{wave}'s own obligation"))

    found = petitions.open_petitions_in_workspace(ws, CURRENT_CONTRACT)

    assert found == []


def test_a_lone_older_waves_file_is_not_the_current_wave(tmp_path):
    """The direct case: EXACTLY ONE petitions file exists, it belongs to an
    OLDER wave (`v0.18`), and the contract names `v0.19`. One older file is
    no more current than three: nothing is surfaced, because a contract
    that names its own wave has already said which file is current."""
    ws = tmp_path / "ws"
    _write(ws, "v0.18-petitions.md",
           "# v0.18 petitions\n" + _section("v0.18's own obligation"))

    found = petitions.open_petitions_in_workspace(ws, CURRENT_CONTRACT)

    assert found == []
    # and the derivation itself agrees: no on-disk file counts as current,
    # while the path it WOULD live at is the contract's own wave.
    default_path, existing = petitions._current_wave_petitions_path(
        ws, CURRENT_CONTRACT)
    assert existing is None
    assert default_path.name == "v0.19-petitions.md"


def test_the_single_file_backcompat_fallback_still_applies(tmp_path):
    """Exactly one petitions file on disk is still treated as the current
    wave's when the contract names NO wave at all -- the common
    single-wave-repo shape every pre-existing test in this suite already
    assumes, where there is no wave token to disagree with."""
    ws = tmp_path / "ws"
    _write(ws, "run-petitions.md",
           "# run petitions\n" + _section("the lone obligation"))

    found = petitions.open_petitions_in_workspace(ws, "")

    assert len(found) == 1
    assert found[0]["obligation"] == "the lone obligation"


def test_an_obligation_naming_a_task_no_longer_in_the_plan_is_history(tmp_path):
    ws = tmp_path / "ws"
    _write(ws, "v0.19-petitions.md",
           "# v0.19 petitions\n" +
           _section("Task 205: a task this plan no longer carries"))
    tasks = [{"id": "018", "title": "still in the plan"}]

    found = petitions.open_petitions_in_workspace(
        ws, CURRENT_CONTRACT, tasks=tasks)

    assert found == []


def test_an_obligation_naming_a_task_still_in_the_plan_stays_open(tmp_path):
    ws = tmp_path / "ws"
    _write(ws, "v0.19-petitions.md",
           "# v0.19 petitions\n" +
           _section("Task 018: scope petition discovery"))
    tasks = [{"id": "018", "title": "scope petition discovery"}]

    found = petitions.open_petitions_in_workspace(
        ws, CURRENT_CONTRACT, tasks=tasks)

    assert len(found) == 1
    assert found[0]["obligation"] == "Task 018: scope petition discovery"


def test_an_obligation_naming_a_task_with_a_possessive_stays_open(tmp_path):
    """Task 002 (#165): the task-anchor extraction used to capture `039's`
    verbatim (possessive apostrophe and all) instead of stopping at the id,
    so an obligation phrased as "Task 039's ..." -- the possessive shape a
    plain-English filing takes -- never resolved against `task_ids` even
    when 039 is squarely in the current plan, and was wrongly dropped as
    history. Stripping the possessive (and any trailing punctuation) before
    the membership check fixes that: the obligation names a task this plan
    still carries, so it is adjudicated, not history."""
    ws = tmp_path / "ws"
    _write(ws, "v0.19-petitions.md",
           "# v0.19 petitions\n" +
           _section("Task 039's obligation cannot be met as written"))
    tasks = [{"id": "039", "title": "still in the plan"}]

    found = petitions.open_petitions_in_workspace(
        ws, CURRENT_CONTRACT, tasks=tasks)

    assert len(found) == 1
    assert found[0]["obligation"] == (
        "Task 039's obligation cannot be met as written")


def test_an_obligation_naming_a_possessive_task_absent_from_the_plan_is_history(
        tmp_path):
    """Companion to the test above: the possessive-stripping fix must not
    turn every possessive task reference into an anchor -- only one whose
    id is actually in `task_ids`. `039` is not in this plan, so the
    obligation is still classified as history."""
    ws = tmp_path / "ws"
    _write(ws, "v0.19-petitions.md",
           "# v0.19 petitions\n" +
           _section("Task 039's obligation cannot be met as written"))
    tasks = [{"id": "018", "title": "still in the plan"}]

    found = petitions.open_petitions_in_workspace(
        ws, CURRENT_CONTRACT, tasks=tasks)

    assert found == []


def test_an_obligation_naming_a_requirement_the_contract_declares_stays_open(
        tmp_path):
    ws = tmp_path / "ws"
    _write(ws, "v0.19-petitions.md",
           "# v0.19 petitions\n" +
           _section("Requirement E, DoD predicate 1 cannot be met"))

    found = petitions.open_petitions_in_workspace(
        ws, CURRENT_CONTRACT, tasks=[{"id": "1", "title": "x"}])

    assert len(found) == 1


def test_an_obligation_naming_a_requirement_the_contract_never_declares_is_history(
        tmp_path):
    ws = tmp_path / "ws"
    _write(ws, "v0.19-petitions.md",
           "# v0.19 petitions\n" +
           _section("Requirement Z, DoD predicate 1 cannot be met"))

    found = petitions.open_petitions_in_workspace(
        ws, CURRENT_CONTRACT, tasks=[{"id": "1", "title": "x"}])

    assert found == []


def test_freeform_obligation_naming_neither_a_task_nor_a_requirement_is_history(
        tmp_path):
    """An obligation that anchors to nothing this run carries -- no task id
    from the current `tasks.json`, no requirement the contract declares -- is
    history, exactly as this task's criterion of record states ("a petition
    whose obligation names no task in the current tasks.json and no
    requirement of the contract"). Free text such as a bare criterion clause
    names neither, so nothing shows it belongs to this run's own plan or
    PRD."""
    ws = tmp_path / "ws"
    _write(ws, "v0.19-petitions.md",
           "# v0.19 petitions\n" +
           _section("the criterion can only be verified after it is met"))

    found = petitions.open_petitions_in_workspace(
        ws, CURRENT_CONTRACT, tasks=[{"id": "1", "title": "x"}])

    assert found == []


def test_an_unanchored_obligation_beside_an_anchored_one_is_the_only_one_dropped(
        tmp_path):
    """The filter is per-petition, not per-file: the current wave's file
    carries one obligation anchored to a live task and one anchored to
    nothing, and only the unanchored one is left out."""
    ws = tmp_path / "ws"
    _write(ws, "v0.19-petitions.md",
           "# v0.19 petitions\n" +
           _section("Task 018: scope petition discovery") +
           _section("no anchor of any kind in this text"))
    tasks = [{"id": "018", "title": "scope petition discovery"}]

    found = petitions.open_petitions_in_workspace(
        ws, CURRENT_CONTRACT, tasks=tasks)

    assert [p["obligation"] for p in found] == [
        "Task 018: scope petition discovery"]


def test_with_no_contract_an_unresolvable_requirement_ref_gets_the_doubt(
        tmp_path):
    """With NO contract text in hand there is nothing to resolve a
    `requirement <letter>` reference against, so that reference is left open
    -- unresolvABLE is not the same fact as unresolved. An obligation naming
    no anchor at all is still history, contract or no contract, and a live
    task reference is still an anchor."""
    ws = tmp_path / "ws"
    _write(ws, "run-petitions.md",
           "# run petitions\n" +
           _section("Requirement E, DoD predicate 1 cannot be met") +
           _section("Task 018: scope petition discovery") +
           _section("nothing here anchors to anything at all"))
    tasks = [{"id": "018", "title": "scope petition discovery"}]

    found = petitions.open_petitions_in_workspace(ws, "", tasks=tasks)

    assert [p["obligation"] for p in found] == [
        "Requirement E, DoD predicate 1 cannot be met",
        "Task 018: scope petition discovery"]


def test_an_obligation_quoting_a_live_tasks_own_criterion_clause_stays_open(
        tmp_path):
    """The third obligation form the worker contract names -- "your own
    task's criterion clause" -- names its task by quoting it, so a clause
    that is still in a current task's `successCriteria` is an anchor and
    the petition stays open, while the same shape of free text that matches
    no current criterion is history (the test above)."""
    ws = tmp_path / "ws"
    _write(ws, "v0.19-petitions.md",
           "# v0.19 petitions\n" +
           _section("the completion names the test node ids covering the "
                    "scoped discovery"))
    tasks = [{"id": "018", "title": "scope petition discovery",
              "successCriteria": (
                  "`open_petitions_in_workspace` reads only the current "
                  "wave's petitions file; the completion names the test "
                  "node ids covering the scoped discovery and the scoped "
                  "filing and they pass.")}]

    found = petitions.open_petitions_in_workspace(
        ws, CURRENT_CONTRACT, tasks=tasks)

    assert len(found) == 1


def test_a_two_word_overlap_with_some_criterion_is_not_an_anchor(tmp_path):
    """The clause anchor is bounded: an obligation too short to be a real
    clause never anchors itself to a criterion that happens to contain it."""
    ws = tmp_path / "ws"
    _write(ws, "v0.19-petitions.md", "# v0.19 petitions\n" +
           _section("they pass"))
    tasks = [{"id": "018", "title": "x",
              "successCriteria": "the node ids are named and they pass."}]

    found = petitions.open_petitions_in_workspace(
        ws, CURRENT_CONTRACT, tasks=tasks)

    assert found == []


def test_tasks_none_keeps_history_filtering_off_for_backward_compat(tmp_path):
    """`tasks=None` (every pre-existing caller) never applies the history
    filter at all, regardless of what the obligation names."""
    ws = tmp_path / "ws"
    _write(ws, "v0.19-petitions.md",
           "# v0.19 petitions\n" +
           _section("Task 205: a task this plan no longer carries"))

    found = petitions.open_petitions_in_workspace(ws, CURRENT_CONTRACT)

    assert len(found) == 1


def _wave_task(tid: str = "018", title: str = "scope petition discovery") -> dict:
    return {"id": tid, "title": title, "validationNotes": "cannot be met"}


def test_exhaustion_without_a_run_handle_refuses_and_touches_no_wave_file(
        tmp_path):
    """Task 008 (requirement H): the wave-scoping logic this test used to
    exercise (`ensure_petitions_file`/`_current_wave_petitions_path`, task
    018) only ever ran on the workspace-fallback branch of
    `file_exhaustion_petition`, taken when the caller has no run handle --
    exactly the branch requirement H retires outright. Every REAL caller
    (`LoopSupervisor._verify_task`) always has a run handle
    (`tests/test_petitioned_status_routing.py` covers that path); a
    caller with none now gets a refusal and creates NOTHING under
    `artifacts/reports/` -- no new current-wave file, and every pre-
    existing older-wave file byte-for-byte unchanged."""
    ws = tmp_path / "ws"
    olders = {}
    for wave in ("v0.16", "v0.17", "v0.18"):
        path = _write(ws, f"{wave}-petitions.md", f"# {wave} petitions\n")
        olders[path] = path.read_text()

    errors = petitions.file_exhaustion_petition(ws, _wave_task(), CURRENT_CONTRACT)

    assert errors
    assert not (ws / "artifacts" / "reports" / "v0.19-petitions.md").exists()
    for path, before in olders.items():
        assert path.read_text() == before


def test_exhaustion_without_a_run_handle_never_touches_an_existing_wave_file(
        tmp_path):
    ws = tmp_path / "ws"
    older = _write(ws, "v0.18-petitions.md", "# v0.18 petitions\n")
    current = _write(ws, "v0.19-petitions.md", "# v0.19 petitions\n")
    before_older, before_current = older.read_text(), current.read_text()

    errors = petitions.file_exhaustion_petition(ws, _wave_task(), CURRENT_CONTRACT)

    assert errors
    assert older.read_text() == before_older
    assert current.read_text() == before_current


def test_exhaustion_without_a_run_handle_never_creates_beside_a_lone_older_one(
        tmp_path):
    """The direct case for the (now retired) writer fallback: EXACTLY ONE
    petitions file exists and it is an OLDER wave's (`v0.18`). Without a
    run handle the filing is refused outright -- no new `v0.19-
    petitions.md` is created, and v0.18's bytes are unchanged."""
    ws = tmp_path / "ws"
    older = _write(ws, "v0.18-petitions.md",
                   "# v0.18 petitions\n" + _section("v0.18's own obligation"))
    before = older.read_text()

    errors = petitions.file_exhaustion_petition(ws, _wave_task(), CURRENT_CONTRACT)

    assert errors
    assert older.read_text() == before
    assert not (ws / "artifacts" / "reports" / "v0.19-petitions.md").exists()


def test_exhaustion_still_refuses_a_duplicate_obligation_from_any_wave(
        tmp_path):
    """The one-petition-per-obligation rule is unaffected by wave-scoping:
    a duplicate filed under an OLDER wave's file still refuses a new
    filing for the same (normalised) obligation."""
    ws = tmp_path / "ws"
    task = _wave_task()
    obligation = petitions.task_petition_obligation(task)
    _write(ws, "v0.18-petitions.md",
           "# v0.18 petitions\n" +
           _section(obligation, evidence="filed some other way first"))

    errors = petitions.file_exhaustion_petition(ws, task, CURRENT_CONTRACT)

    assert errors
    assert "duplicate" in errors[0]
    # no v0.19 file was created for a refused filing.
    assert not (ws / "artifacts" / "reports" / "v0.19-petitions.md").exists()


# ---------------------------------------------------------------------------
# 10. Write-back of the adjudication into the tracked wave file itself
#     (task 019, PRD requirement E.2, #150).
# ---------------------------------------------------------------------------

WAVE_FILE_TEXT = """# v0.19 impossibility petitions

## Petition: Requirement E.2 cannot be met

**Obligation**: Requirement E.2 cannot be met
**Type**: environmental
**Evidence**: no network route

## Petition: Task 5: a second, unrelated obligation

**Obligation**: Task 5: a second, unrelated obligation
**Type**: self-referential
**Evidence**: circular
"""


def _wave_file(workspace: Path) -> Path:
    reports = workspace / "artifacts" / "reports"
    reports.mkdir(parents=True, exist_ok=True)
    path = reports / "v0.19-petitions.md"
    path.write_text(WAVE_FILE_TEXT)
    return path


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=cwd, check=True,
                           capture_output=True, text=True)


def _git_status(cwd: Path) -> str:
    return subprocess.run(["git", "status", "--porcelain"], cwd=cwd,
                           check=True, capture_output=True,
                           text=True).stdout


def _git_log(cwd: Path) -> str:
    return subprocess.run(["git", "log", "--oneline"], cwd=cwd, check=True,
                           capture_output=True, text=True).stdout


def _init_repo(workspace: Path) -> None:
    _git(workspace, "init", "-q")
    _git(workspace, "config", "user.email", "t@example.com")
    _git(workspace, "config", "user.name", "t")
    _git(workspace, "add", "-A")
    _git(workspace, "commit", "-q", "-m", "initial")


def _result(payload: dict) -> IterationResult:
    r = IterationResult(exit_code=0)
    r.final_text = json.dumps(payload)
    return r


_supervisor_writeback = supervisor_factory(run_id="task019-petition-writeback", cfg_defaults=False,
                                           run_root="run", workspace="workspace", tasks=[])


def _run_wave_file(tmp_path: Path) -> tuple[RunDir, Path]:
    """The run-dir counterpart of `_wave_file`: a real `RunDir` whose OWN
    `petitions_file` (never a workspace path) carries `WAVE_FILE_TEXT` --
    the fixture every run-dir-scoped rewrite of this section's tests uses
    (task 008, requirement H) so the matching-section/remedy/no-overwrite/
    invalid-value logic `_append_adjudication_to_section` implements stays
    covered, just against the ONLY place a run handle ever writes now.
    """
    run = RunDir(root=tmp_path / "run-writeback")
    path = run.petitions_file
    path.write_text(WAVE_FILE_TEXT)
    return run, path


def test_confirmed_is_appended_to_the_matching_section_only(tmp_path):
    """Task 008 (requirement H, PRD finding B8): calling the shipped
    `write_back_petition_adjudication` WITHOUT a run handle used to append
    `**Adjudication**: CONFIRMED` straight into this workspace file -- the
    exact counterexample finding B8 named, and the reason this test (and
    its REFUTED counterpart right below) is rewritten to assert TREE
    IMMUTABILITY instead of a tree write: no run handle means a
    documented no-op, `artifacts/reports/` left byte-identical, full
    stop. The matching-section-only/remedy/no-overwrite/invalid-value
    behaviour this test used to pin for the tree survives instead at RUN
    scope -- `test_confirmed_is_appended_to_the_run_dir_matching_section_only`
    and its neighbours below (parser cover: `_append_adjudication_to_
    section`/`parse_petitions`, exercised identically either way) -- plus
    the pre-existing supervisor-level
    `test_review_verdict_writes_the_adjudication_into_the_run_dir_never_the_workspace_tree`.
    """
    path = _wave_file(tmp_path)
    before = path.read_text()
    written = petitions.write_back_petition_adjudication(
        tmp_path, "Requirement E.2 cannot be met", "CONFIRMED")
    assert written is None
    assert path.read_text() == before
    assert sorted((tmp_path / "artifacts" / "reports").iterdir()) == [path]


def test_refuted_is_appended_with_its_remedy(tmp_path):
    """The REFUTED counterpart of the test above (task 008, requirement H,
    H.3): same claim, a REFUTED call with a remedy is exactly as inert
    against the workspace tree as a CONFIRMED one -- see that test's own
    docstring for the run-dir/parser cover this retires in favour of.
    """
    path = _wave_file(tmp_path)
    before = path.read_text()
    written = petitions.write_back_petition_adjudication(
        tmp_path, "Task 5: a second, unrelated obligation", "REFUTED",
        "do X instead")
    assert written is None
    assert path.read_text() == before
    assert sorted((tmp_path / "artifacts" / "reports").iterdir()) == [path]


def test_confirmed_is_appended_to_the_run_dir_matching_section_only(tmp_path):
    """The run-dir survivor of `test_confirmed_is_appended_to_the_matching_
    section_only`'s original claim: WITH a run handle, the write lands
    (only) in `run.petitions_file`, in the matching section, leaving the
    other section untouched."""
    run, path = _run_wave_file(tmp_path)
    written = petitions.write_back_petition_adjudication(
        tmp_path, "Requirement E.2 cannot be met", "CONFIRMED", run=run)
    assert written == path
    parsed = {p["obligation"]: p for p in petitions.parse_petitions(
        path.read_text())}
    assert parsed["Requirement E.2 cannot be met"]["adjudication"] == "CONFIRMED"
    # the OTHER section is untouched.
    assert parsed["Task 5: a second, unrelated obligation"]["adjudication"] is None


def test_refuted_with_no_remedy_still_appends_a_line(tmp_path):
    run, path = _run_wave_file(tmp_path)
    petitions.write_back_petition_adjudication(
        tmp_path, "Requirement E.2 cannot be met", "REFUTED", "", run=run)
    parsed = {p["obligation"]: p for p in petitions.parse_petitions(
        path.read_text())}
    entry = parsed["Requirement E.2 cannot be met"]
    assert entry["adjudication"] == "REFUTED"
    assert entry["remedy"]  # never blank -- "no remedy stated" text


def test_no_matching_obligation_is_a_noop(tmp_path):
    run, _path = _run_wave_file(tmp_path)
    written = petitions.write_back_petition_adjudication(
        tmp_path, "an obligation nothing filed", "CONFIRMED", run=run)
    assert written is None


def test_an_already_adjudicated_section_is_never_overwritten(tmp_path):
    run, path = _run_wave_file(tmp_path)
    petitions.write_back_petition_adjudication(
        tmp_path, "Requirement E.2 cannot be met", "CONFIRMED", run=run)
    again = petitions.write_back_petition_adjudication(
        tmp_path, "Requirement E.2 cannot be met", "REFUTED", "a remedy",
        run=run)
    assert again is None
    parsed = {p["obligation"]: p for p in petitions.parse_petitions(
        path.read_text())}
    # still CONFIRMED -- the second call never flipped it to REFUTED.
    assert parsed["Requirement E.2 cannot be met"]["adjudication"] == "CONFIRMED"


def test_an_invalid_adjudication_value_is_a_noop(tmp_path):
    run, _path = _run_wave_file(tmp_path)
    assert petitions.write_back_petition_adjudication(
        tmp_path, "Requirement E.2 cannot be met", "MAYBE", run=run) is None


def test_review_verdict_writes_the_adjudication_into_the_run_dir_never_the_workspace_tree(
        tmp_path):
    """Task 065 (requirement N.6, #150): `_write_back_petition_adjudications`
    now threads `run=self.run` into `petitions.write_back_petition_
    adjudication`, so the CONFIRMED/REFUTED marker for an obligation this
    RUN itself filed (through the run-dir `file_worker_petition` path)
    lands in `sup.run.petitions_file` -- never a `*-petitions.md` file
    under the workspace tree, and never a git commit against the
    workspace (there is nothing there for `_commit_petition_writeback` to
    add). #150 (the historical multi-wave-glob duplicate-obligation bug)
    stays CLOSED -- unrelated to where this writes.

    Retires the four `_review_verdict`-driven tests this replaces
    (`test_review_verdict_writes_the_adjudication_into_the_tracked_file`,
    `test_a_clean_workspace_commits_the_writeback_with_the_run_identity`,
    `test_a_dirty_workspace_defers_the_commit_to_the_next_task_boundary`,
    `test_a_non_git_workspace_still_gets_the_write`): each pinned the
    write-back landing IN the workspace tree and being committed there,
    which requirement N.6 retires outright. Their surviving cover is this
    test (the run-dir write-back itself) plus `test_petition_adjudications_
    run_scope.py`'s existing suite (the JSON store this same run-dir
    write-back sits beside) and `test_a_workspace_filed_petitions_
    adjudication_write_back_is_a_noop_against_the_tree` below (a petition
    filed the OLD way, before this obligation's own filing moved).
    """
    sup = _supervisor_writeback(tmp_path)
    (sup.workspace / "README.md").write_text("an unrelated tracked file\n")
    _init_repo(sup.workspace)
    assert _git_status(sup.workspace) == ""
    obligation = "Requirement E.2 cannot be met"
    errors = petitions.file_worker_petition(
        sup.workspace, obligation, "self-referential", "circular",
        run=sup.run)
    assert errors == []

    result = _result({
        "verdict": "verified",
        "petition_adjudications": [
            {"obligation": obligation, "adjudication": "CONFIRMED"},
        ],
    })
    sup._review_verdict(result)

    parsed = {p["obligation"]: p for p in petitions.parse_petitions(
        sup.run.petitions_file.read_text())}
    assert parsed[obligation]["adjudication"] == "CONFIRMED"

    # nothing landed under the workspace tree, and nothing was committed.
    assert not (sup.workspace / "artifacts" / "reports").exists()
    assert _git_status(sup.workspace) == ""
    assert _git_log(sup.workspace).count("\n") == 1  # only the initial commit


def test_a_workspace_filed_petitions_adjudication_write_back_is_a_noop_against_the_tree(
        tmp_path):
    """The companion case: an obligation filed the OLD way -- straight into
    the workspace's tracked wave file, as every pre-065 engine build did --
    gets no write-back at all through the new, run-scoped path: `run=self.
    run` means only `run.petitions_file` is ever searched, so the
    workspace file is left completely untouched (byte-for-byte) and no
    commit is made. This is requirement N.6's own claim -- "no write to a
    tracked `*-petitions.md` file in the workspace" -- holding even for a
    petition this run itself never filed there.
    """
    sup = _supervisor_writeback(tmp_path)
    path = _wave_file(sup.workspace)
    _init_repo(sup.workspace)
    before = path.read_text()

    result = _result({
        "verdict": "verified",
        "petition_adjudications": [
            {"obligation": "Requirement E.2 cannot be met",
             "adjudication": "CONFIRMED"},
        ],
    })
    sup._review_verdict(result)  # must not raise

    assert path.read_text() == before
    assert _git_status(sup.workspace) == ""


def test_no_petition_adjudications_writes_nothing_and_touches_no_git_state(tmp_path):
    sup = _supervisor_writeback(tmp_path)
    _wave_file(sup.workspace)
    _init_repo(sup.workspace)

    result = _result({"verdict": "verified"})
    written = sup._write_back_petition_adjudications(
        json.loads(result.final_text).get("petition_adjudications"))
    assert written == []
    assert _git_status(sup.workspace) == ""
