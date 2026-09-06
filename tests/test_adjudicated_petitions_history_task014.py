"""Task 014 (requirement F.1, #144): a petition present in
`petition-adjudications.json` is rendered to review as HISTORY, never as
open or re-filed, and `review.md`'s re-file prohibition is scoped to a
*new* filing against an already-adjudicated obligation; a
`midRunAdjudication: true` review episode may answer `verdict:
"adjudicated"` (or omit `verdict`) without producing a
`contract.agent_fault`, while the ordinary run-ending review's own schema
(and its safe default at ladder exhaustion) is unchanged.

Four things pinned:

1. `engine.petitions.adjudicated_petitions_review_context` (and the
   `LoopSupervisor` method/join point that renders it) -- empty in the
   common case, renders every run-scope CONFIRMED obligation as settled
   history when present, and treats a tampered record as empty (fail
   towards re-adjudication, never towards trusting a manufactured
   exemption) -- the same posture every sibling petitions context in this
   module already takes.
2. `transitions.MIDRUN_REVIEW_SCHEMA` accepts `verdict: "adjudicated"` and
   an absent `verdict` field; the ordinary `HAT_SCHEMAS["review"]` schema
   still rejects both, unchanged.
3. `LoopSupervisor._review_contract_ladder(..., mid_run=True)` /
   `_review_verdict(..., mid_run=True)` accept such a reply with zero
   contract errors and never reach `contract_enforcement.enforce_contract`
   at all for a compliant first attempt -- so no `contract.agent_fault` is
   ever emitted for it -- while `mid_run=False` (the ordinary, unchanged
   default) still finds the exact same reply schema-invalid.
4. End-to-end through `_maybe_run_midrun_adjudication_episode`
   (`_step_worker`): a review reply spelling `"verdict": "adjudicated"`
   settles the episode, confirms its petition at run scope, and never
   emits `contract.agent_fault` -- while the worker's own task still
   reaches an ordinary `verified` outcome afterwards, proving the episode
   never substituted for it.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from ralphd.engine import contracts
from ralphd.engine import petitions
from ralphd.engine import transitions
from ralphd.engine.config import JobConfig
from ralphd.engine.loop import LoopSupervisor
from ralphd.engine.runner import IterationResult
from ralphd.engine.state import RunDir, atomic_write_json
from tests.conftest import supervisor_factory

OBLIGATION = "Task 1: awaiting an adjudication episode"


def _ok(text: str) -> IterationResult:
    r = IterationResult(exit_code=0)
    r.final_text = text
    r.duration_s = 1.0
    r.usage = {"input": 1, "output": 1, "totalTokens": 2}
    return r


# The shared harness (tests/conftest.py, G.1): this module's run dir and
# workspace sit side by side under the test's own tmp_path.
_supervisor = supervisor_factory(run_id="task014-history", run_root="run",
                                workspace="ws")



def test_empty_when_nothing_confirmed(tmp_path):
    sup = _supervisor(tmp_path)
    assert petitions.adjudicated_petitions_review_context(sup.run) == ""
    assert sup._adjudicated_petitions_review_context() == ""


def test_confirmed_obligation_renders_as_settled_history(tmp_path):
    sup = _supervisor(tmp_path)
    petitions.record_confirmed_adjudications(sup.run, [
        {"obligation": OBLIGATION, "adjudication": "CONFIRMED"},
    ])
    ctx = petitions.adjudicated_petitions_review_context(sup.run)
    assert "Adjudicated petitions" in ctx
    assert "history" in ctx.lower()
    assert OBLIGATION in ctx
    assert "CONFIRMED" in ctx
    # never rendered as something still open for a hearing
    assert "MUST" not in ctx.split("CONFIRMED")[0] or "not open" in ctx.lower()

    method_ctx = sup._adjudicated_petitions_review_context()
    assert OBLIGATION in method_ctx


def test_history_context_joins_review_extra_context(tmp_path):
    sup = _supervisor(tmp_path)
    petitions.record_confirmed_adjudications(sup.run, [
        {"obligation": OBLIGATION, "adjudication": "CONFIRMED"},
    ])
    ctx = sup._review_extra_context()
    assert OBLIGATION in ctx
    assert "Adjudicated petitions" in ctx


def test_tampered_record_is_treated_as_empty_history(tmp_path, monkeypatch):
    sup = _supervisor(tmp_path)
    petitions.record_confirmed_adjudications(sup.run, [
        {"obligation": OBLIGATION, "adjudication": "CONFIRMED"},
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
        f"**Obligation**: {OBLIGATION}\n"
        "**Type**: environmental\n"
        "**Evidence**: some evidence.\n")
    sup = _supervisor(tmp_path)
    petitions.record_confirmed_adjudications(sup.run, [
        {"obligation": OBLIGATION, "adjudication": "CONFIRMED"},
    ])
    open_ctx = sup._petitions_review_context()
    history_ctx = sup._adjudicated_petitions_review_context()
    assert "Open impossibility petitions" not in open_ctx
    assert OBLIGATION not in open_ctx
    assert OBLIGATION in history_ctx


# ---------------------------------------------------------------------------
# 2. Schema: MIDRUN_REVIEW_SCHEMA vs the ordinary review schema.
# ---------------------------------------------------------------------------

def test_midrun_schema_accepts_adjudicated_verdict():
    payload = {"verdict": "adjudicated",
              "petition_adjudications": [
                  {"obligation": OBLIGATION, "adjudication": "CONFIRMED"}]}
    assert contracts.validate_against(transitions.MIDRUN_REVIEW_SCHEMA, payload) == []


def test_midrun_schema_accepts_missing_verdict():
    payload = {"petition_adjudications": [
        {"obligation": OBLIGATION, "adjudication": "REFUTED", "remedy": "do X"}]}
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
    sup = _supervisor(tmp_path)
    result = _ok(json.dumps({
        "verdict": "adjudicated",
        "petition_adjudications": [
            {"obligation": OBLIGATION, "adjudication": "CONFIRMED"}],
    }))
    out = asyncio.run(sup._review_contract_ladder(result, mid_run=True))
    # accepted on the first attempt -- the SAME IterationResult object,
    # never routed through enforce_contract's re-ask/agent-fault path.
    assert out is result

    verified, payload, errors = sup._review_verdict(result, mid_run=True)
    assert errors == []
    assert payload["verdict"] == "adjudicated"


def test_ladder_accepts_missing_verdict_when_mid_run(tmp_path):
    sup = _supervisor(tmp_path)
    result = _ok(json.dumps({
        "petition_adjudications": [
            {"obligation": OBLIGATION, "adjudication": "CONFIRMED"}],
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
    sup = _supervisor(tmp_path)
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
            {"obligation": OBLIGATION, "adjudication": "CONFIRMED"}],
    }))


def _verified(sup, prompt=""):
    return _ok(json.dumps({"task_id": "1", "verdict": "verified"}))


def test_episode_answering_adjudicated_settles_with_no_agent_fault(tmp_path):
    sup, run, workspace = _worker_supervisor(tmp_path)
    _make_petitions_file(workspace, OBLIGATION)

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
    assert any(OBLIGATION.lower() in c or c in OBLIGATION.lower()
              for c in confirmed)

    task = sup.run.read_tasks()["tasks"][0]
    assert task["status"] == "validated"
