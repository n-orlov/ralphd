"""Task 035 (PRD requirement B.1, cure for task 006): the run record's
approach history comes from ENGINE-OWNED metadata, written at the moment each
judge's verdict is settled, and `run-record.md` renders every section of
`run-record.json`.

Task 006 built the record but derived each approach's verdict from the two
OPTIONAL structured findings sidecars (`review-findings.json`,
`verdict-gate-findings.json`). Those are the judge's own deliverable: they can
be absent (a hat that wrote only prose or approved without findings),
present-but-unreadable, or simply never written for an approach that was
archived long ago -- and in every one of those cases the record lost the one
fact it must never lose, WHAT EACH JUDGE DECIDED. So:

  * `LoopSupervisor._review_verdict` / `_verdict_gate_verdict` now persist each
    settled verdict, with EXPLICIT `blocking`/`residual` arrays, into
    `RunDir.approach_metadata_file` (`state.record_approach_verdict`) as the
    judge runs -- independently of any sidecar;
  * `run_record.build_run_record` reads that metadata for BOTH the live and the
    archived approach history, falling back to a sidecar only for an approach
    that has no metadata entry at all;
  * `run_record.render_run_record_markdown` renders every top-level section of
    the record (`run_record.RECORD_SECTIONS`), including `usage`'s per-direction
    token fields and its `byPhase`/`byApproach`/`byTier` breakdowns.

Everything here is in-process: real `LoopSupervisor` instances driving real
`_step_review()`/`_step_verdict_gate()` passes with a stubbed runner, no docker
and no real `pi`. The two runs the task asks for are covered end to end:
`test_succeeded_run_records_its_verified_verdict_...` (a succeeded run) and
`test_archived_rejected_approach_...` (a run with an archived rejected
approach).
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from ralphd.engine import findings as findings_mod
from ralphd.engine import loop as loop_mod
from ralphd.engine import run_record
from ralphd.engine import state as state_mod
from ralphd.engine.config import JobConfig
from ralphd.engine.loop import LoopSupervisor
from ralphd.engine.runner import IterationResult
from ralphd.engine.state import RunDir


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------

def _result(text: str) -> IterationResult:
    r = IterationResult(exit_code=0)
    r.final_text = text
    r.duration_s = 3.0
    r.usage = {"input": 12, "output": 7, "totalTokens": 19, "costUSD": 0.02}
    return r


def _supervisor(root: Path, monkeypatch, **cfg_kw) -> LoopSupervisor:
    root.mkdir(parents=True, exist_ok=True)
    (root / "prd.md").write_text("# task 035 fixture PRD\n\nDo the thing.\n")
    monkeypatch.setattr(loop_mod, "CONFIG_DIR", root / "no-config")
    run = RunDir(root=root)
    run.update_status(state="running")
    run.write_tasks({"version": 1, "tasks": [
        {"id": "001", "title": "t", "successCriteria": "c",
         "status": "completed"},
    ]})
    kw = {"max_approaches": 1, "reflect": False}
    kw.update(cfg_kw)
    cfg = JobConfig(run_id="task035-approach-metadata", **kw)
    return LoopSupervisor(cfg, run, root)


def _stub_runner(sup: LoopSupervisor, payload: dict) -> None:
    async def _run(prompt, transcript, **kw):
        return _result(json.dumps(payload))
    sup.runner.run = _run  # type: ignore[method-assign]


def _record(run: RunDir) -> dict:
    return json.loads((run.artifacts_dir / "run-record.json").read_text())


def _markdown(run: RunDir) -> str:
    return (run.artifacts_dir / "run-record.md").read_text()


def _events(root: Path, type_: str) -> list[dict]:
    log = root / "events.jsonl"
    lines = log.read_text().splitlines() if log.exists() else []
    return [ev for ev in (json.loads(x) for x in lines) if ev.get("type") == type_]


# ---------------------------------------------------------------------------
# 1. state: the engine-owned metadata itself
# ---------------------------------------------------------------------------

def test_metadata_records_explicit_arrays_even_with_no_findings_list(tmp_path):
    run = RunDir(root=tmp_path / "run")
    entry = run.record_approach_verdict(1, "review", verdict="rejected",
                                        reason="not done", findings=None)
    assert entry["verdict"] == "rejected"
    assert entry["blocking"] == [] and entry["residual"] == []
    assert entry["findingsChannel"] is False
    assert entry["recordedAt"]
    assert run.approach_metadata_file.is_file()
    assert state_mod.approach_metadata_channel(run.root, 1, "review") == entry
    # a channel no judge ever settled stays absent -- never a fabricated
    # verified verdict.
    assert state_mod.approach_metadata_channel(run.root, 1, "verdictGate") is None
    assert state_mod.approach_metadata_channel(run.root, 2, "review") is None


def test_metadata_splits_blocking_and_residual_and_is_per_channel_idempotent(tmp_path):
    run = RunDir(root=tmp_path / "run")
    items = [
        {"severity": "blocking", "curable": True, "clause": "req X",
         "evidence": "missing"},
        {"severity": "residual", "curable": False, "clause": "req Y",
         "evidence": "cosmetic"},
    ]
    run.record_approach_verdict(1, "review", verdict="rejected", findings=items)
    run.record_approach_verdict(1, "verdictGate", verdict="verified",
                                findings=[])
    run.record_approach_verdict(2, "review", verdict="verified", findings=[])
    review = state_mod.approach_metadata_channel(run.root, 1, "review")
    assert [f["clause"] for f in review["blocking"]] == ["req X"]
    assert [f["clause"] for f in review["residual"]] == ["req Y"]
    assert review["findingsChannel"] is True
    # re-recording ONE channel leaves the other channel and other approaches
    # untouched (a resumed process re-reviewing the same approach).
    run.record_approach_verdict(1, "review", verdict="verified", findings=[])
    assert state_mod.approach_metadata_channel(
        run.root, 1, "review")["verdict"] == "verified"
    assert state_mod.approach_metadata_channel(
        run.root, 1, "verdictGate")["verdict"] == "verified"
    assert state_mod.approach_metadata_channel(
        run.root, 2, "review")["verdict"] == "verified"
    assert sorted(state_mod.read_approach_metadata(run.root)["approaches"]) == \
        ["1", "2"]


def test_metadata_is_not_archived_with_the_approach(tmp_path, monkeypatch):
    """It lives at the run root, keyed by approach, exactly like
    `petition-adjudications.json` -- so one file answers for every approach."""
    root = tmp_path / "run"
    sup = _supervisor(root, monkeypatch, max_approaches=2)
    sup.run.record_approach_verdict(1, "review", verdict="rejected",
                                    findings=None)
    sup._archive_approach(1)
    assert not (root / "approaches" / "01" /
                state_mod.APPROACH_METADATA_FILE).exists()
    assert state_mod.approach_metadata_channel(root, 1, "review")["verdict"] == \
        "rejected"


# ---------------------------------------------------------------------------
# 2. LoopSupervisor: the verdict is persisted as the judge runs, with no
#    findings sidecar anywhere
# ---------------------------------------------------------------------------

def test_review_verdict_is_persisted_without_any_findings_sidecar(tmp_path, monkeypatch):
    root = tmp_path / "run"
    sup = _supervisor(root, monkeypatch, max_approaches=1)
    sup._approach = 1
    _stub_runner(sup, {"verdict": "rejected", "reason": "requirement B.1 unmet"})
    assert not sup.run.findings_json_file.exists()

    outcome = asyncio.run(sup._step_review())
    assert outcome == ("tail",)  # rejected, no approach left

    entry = state_mod.approach_metadata_channel(root, 1, "review")
    assert entry["verdict"] == "rejected"
    assert entry["reason"] == "requirement B.1 unmet"
    assert entry["findingsChannel"] is False
    assert entry["blocking"] == [] and entry["residual"] == []
    recorded = _events(root, "approach.verdict_recorded")
    assert [(e["channel"], e["verdict"]) for e in recorded] == \
        [("review", "rejected")]


def test_review_verdict_is_persisted_when_the_sidecar_does_not_validate(
        tmp_path, monkeypatch):
    """A present-but-unreadable sidecar refuses the verdict (task 002) -- and
    the refusal itself is still recorded as this approach's verdict, with
    `findingsChannel: false` saying why the arrays are empty."""
    root = tmp_path / "run"
    sup = _supervisor(root, monkeypatch, max_approaches=1)
    sup._approach = 1
    sup.run.findings_json_file.write_text('[{"severity": "blocking"}]')
    _stub_runner(sup, {"verdict": "verified", "reason": "all good"})

    asyncio.run(sup._step_review())

    entry = state_mod.approach_metadata_channel(root, 1, "review")
    assert entry["verdict"] == "rejected"
    assert entry["findingsChannel"] is False


def test_midrun_adjudication_episode_records_no_approach_verdict(tmp_path, monkeypatch):
    """A mid-run adjudication reply (task 014, requirement F.1) scores no
    approach verdict -- it must never be recorded as a rejection."""
    root = tmp_path / "run"
    sup = _supervisor(root, monkeypatch, max_approaches=1)
    sup._approach = 1
    sup._review_verdict(_result(json.dumps({"verdict": "adjudicated"})),
                        mid_run=True)
    assert state_mod.approach_metadata_channel(root, 1, "review") is None


# ---------------------------------------------------------------------------
# 3. The two runs requirement B.1's cure names: a succeeded run, and a run
#    with an archived rejected approach.
# ---------------------------------------------------------------------------

def test_succeeded_run_records_its_verified_verdict_in_the_written_record(
        tmp_path, monkeypatch):
    root = tmp_path / "run"
    sup = _supervisor(root, monkeypatch, max_approaches=1)
    sup._approach = 1
    findings_mod.write_findings_json(sup.run.findings_json_file, [
        {"severity": "residual", "curable": False, "clause": "req R",
         "evidence": "noted, not blocking"},
    ])
    _stub_runner(sup, {"verdict": "verified", "reason": "criteria hold",
                       "findings_path": "review-findings.md"})

    outcome = asyncio.run(sup._step_review())
    assert outcome == ("done", "succeeded")

    status = sup.run.read_status()
    assert status["state"] == "succeeded"
    record = _record(sup.run)
    assert record["state"] == "succeeded"
    assert len(record["approaches"]) == 1
    live = record["approaches"][0]
    assert live["approach"] == 1 and live["archived"] is False
    assert live["review"]["source"] == "engine-metadata"
    assert live["review"]["verdict"] == "verified"
    assert live["review"]["reason"] == "criteria hold"
    assert [f["clause"] for f in live["review"]["residual"]] == ["req R"]
    assert live["review"]["blocking"] == []
    # the peer gate never ran on this run: absent, never a fabricated verdict.
    assert live["verdictGate"] is None

    md = _markdown(sup.run)
    assert "verdict `verified`" in md
    assert "source: engine-metadata" in md
    assert "req R" in md and "noted, not blocking" in md
    assert "**verdict gate**: no verdict recorded" in md
    # the usage the review iteration actually accumulated, all three
    # breakdowns rendered (the gap task 006 left).
    assert "- input: 12" in md and "- output: 7" in md
    assert "- byPhase:" in md and "`review`" in md
    assert "- byApproach:" in md and "- byTier:" in md


def test_archived_rejected_approach_history_survives_a_missing_sidecar(
        tmp_path, monkeypatch):
    """A run whose first approach was REJECTED and archived, and whose second
    approach is live: the archived approach's verdict is read from the engine's
    metadata, so it is still in the record after the archived sidecar is gone
    (here: never written at all, the case task 006 rendered as `null`)."""
    root = tmp_path / "run"
    sup = _supervisor(root, monkeypatch, max_approaches=2)
    sup._approach = 1
    _stub_runner(sup, {"verdict": "rejected", "reason": "requirement D unmet"})

    outcome = asyncio.run(sup._step_review())
    assert outcome == ("advance", "replan", "rejected_approach_left")
    assert (root / "approaches" / "01" / "tasks.json").is_file()
    assert not (root / "approaches" / "01" / "review-findings.json").exists()

    # the run then goes terminal on the second approach's budget end.
    sup._approach = 2
    sup.run.update_status(approach=2)
    sup._finish("failed", "unverified")

    record = _record(sup.run)
    assert [a["approach"] for a in record["approaches"]] == [1, 2]
    archived = record["approaches"][0]
    assert archived["archived"] is True
    assert archived["archivedDir"] == "approaches/01"
    assert archived["review"]["source"] == "engine-metadata"
    assert archived["review"]["verdict"] == "rejected"
    assert archived["review"]["reason"] == "requirement D unmet"
    assert archived["review"]["findingsChannel"] is False
    assert archived["taskTally"]["total"] == 1
    live = record["approaches"][1]
    assert live["archived"] is False and live["review"] is None

    md = _markdown(sup.run)
    assert "### Approach 1 (archived)" in md
    assert "requirement D unmet" in md
    assert "### Approach 2 (live)" in md


def test_verdict_gate_rejection_is_recorded_as_its_own_channel(tmp_path, monkeypatch):
    root = tmp_path / "run"
    sup = _supervisor(root, monkeypatch, max_approaches=2,
                      peer_verdict_gate=True)
    sup._approach = 1
    findings_mod.write_findings_json(sup.run.verdict_gate_findings_json_file, [
        {"severity": "blocking", "curable": False, "clause": "gate req",
         "evidence": "peer disagreed"},
    ])
    _stub_runner(sup, {"verdict": "rejected", "reason": "gate says no"})

    outcome = asyncio.run(sup._step_verdict_gate())
    assert outcome == ("advance", "replan", "verdict_gate_rejected_approach_left")

    entry = state_mod.approach_metadata_channel(root, 1, "verdictGate")
    assert entry["verdict"] == "rejected"
    assert entry["reason"] == "gate says no"
    assert entry["findingsChannel"] is True
    assert [f["clause"] for f in entry["blocking"]] == ["gate req"]
    assert state_mod.approach_metadata_channel(root, 1, "review") is None

    sup._finish("failed", "unverified")
    archived = _record(sup.run)["approaches"][0]
    assert archived["verdictGate"]["source"] == "engine-metadata"
    assert archived["verdictGate"]["verdict"] == "rejected"
    assert archived["review"] is None


# ---------------------------------------------------------------------------
# 4. Fallback + markdown completeness
# ---------------------------------------------------------------------------

def test_an_approach_with_no_metadata_still_falls_back_to_its_sidecar(tmp_path):
    """Runs judged before this task have no metadata at all -- their archived
    sidecars are still read, and labelled as such."""
    run = RunDir(root=tmp_path / "run")
    adir = run.root / "approaches" / "01"
    adir.mkdir(parents=True)
    state_mod.atomic_write_json(adir / "tasks.json", {"tasks": []})
    findings_mod.write_findings_json(adir / "review-findings.json", [
        {"severity": "blocking", "curable": True, "clause": "old req",
         "evidence": "from before task 035"},
    ])
    run.update_status(approach=2)
    history = run_record.build_approach_history(run)
    assert history[0]["review"]["source"] == "findings-sidecar"
    assert history[0]["review"]["verdict"] == "rejected"
    assert [f["clause"] for f in history[0]["review"]["blocking"]] == ["old req"]


def test_markdown_renders_every_top_level_section_of_the_json(tmp_path):
    run = RunDir(root=tmp_path / "run")
    run.update_status(state="succeeded", verdict="verified", approach=1,
                      iterationsUsed=7,
                      usage={"input": 100, "output": 20, "cacheRead": 3,
                             "cacheWrite": 1, "totalTokens": 124,
                             "costUSD": 0.5,
                             "byPhase": {"review": {"totalTokens": 24}},
                             "byApproach": {"1": {"totalTokens": 124}},
                             "byTier": {"hard": {"totalTokens": 124,
                                                  "iterations": 2}}})
    record = run_record.build_run_record(
        run, run_id="unit-run", max_approaches=2, config_root=tmp_path / "cfg")
    # every field of the record is a declared, rendered section -- a new field
    # with no renderer fails here rather than going silently unrendered.
    assert set(record) == set(run_record.RECORD_SECTIONS)
    md = run_record.render_run_record_markdown(record)
    for section in sorted(set(run_record.RECORD_SECTIONS.values()) - {"header"}):
        assert f"## {section}" in md
    for line in ("- input: 100", "- output: 20", "- cacheRead: 3",
                 "- cacheWrite: 1", "- totalTokens: 124", "- costUSD: 0.5",
                 "- byPhase:", "- byApproach:", "- byTier:",
                 "- iterationsUsed: 7"):
        assert line in md, line
    assert "`review`" in md and "`hard`" in md
