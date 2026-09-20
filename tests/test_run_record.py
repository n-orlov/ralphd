"""Task 006 (PRD requirement B.1, #140): `run-record.md`/`.json`.

Two layers, both in-process (no docker, no real `pi`):

  * `engine.run_record` unit tests -- build the record straight off a hand
    built run dir (archived approaches with findings sidecars, a confirmed
    petition adjudication, a consumed steer, an operator ruling under a
    monkeypatched `CONFIG_DIR`, and `status.json` usage/iteration figures),
    then assert on the JSON shape and that the markdown renders the same
    facts.
  * `LoopSupervisor` tests -- `_finish()` writes both files at entry to
    `finishing` (reflect on and off), and a later terminal transition
    (`_promote_finishing_state()`, `_promote_recorded_finishing_state()`)
    rewrites them, exactly as PRD requirement B.1 asks.

Task 028 (requirement D.7) folded `test_run_record_approach_metadata_task035.py` into this module in
whole -- every test body, assertion and docstring verbatim, only the module
they live in changed; one `docs/test-suite-audit.md` § "Subsumed" row per
folded node names this module's surviving node.
"""

from __future__ import annotations

import json
from pathlib import Path

from ralphd.engine import loop as loop_mod
from ralphd.engine import run_record
from ralphd.engine.config import JobConfig
from ralphd.engine.findings import write_findings_json
from ralphd.engine.loop import LoopSupervisor
from ralphd.engine.petitions import record_confirmed_adjudications
from ralphd.engine.state import (
    RunDir,
    VERDICT_GATE_FINDINGS_JSON_FILE,
    amendment_entries,
    atomic_write_json,
    write_amendment,
)
from tests.conftest import make_supervisor


def _run(tmp_path: Path) -> RunDir:
    return RunDir(root=tmp_path / "run")


_supervisor = lambda tmp_path, run=None, **cfg: make_supervisor(
    tmp_path, **{"run_id": "unit-run", "cfg_defaults": False,
                 "run_root": (run or _run(tmp_path)).root, "workspace": "ws",
                 "max_approaches": 3, **cfg})


# ---------------------------------------------------------------------------
# engine.run_record: approach history
# ---------------------------------------------------------------------------

def test_approach_history_reads_archived_approach_with_blocking_and_residual(tmp_path):
    run = _run(tmp_path)
    adir = run.root / "approaches" / "01"
    adir.mkdir(parents=True)
    atomic_write_json(adir / "tasks.json", {"tasks": [
        {"id": "001", "status": "completed"},
        {"id": "002", "status": "pending"},
    ]})
    write_findings_json(adir / "review-findings.json", [
        {"severity": "blocking", "curable": True, "clause": "req A",
         "evidence": "missing X"},
        {"severity": "residual", "curable": False, "clause": "req B",
         "evidence": "cosmetic gap"},
    ])
    run.update_status(approach=2)  # approach 1 archived, 2 is the live one
    history = run_record.build_approach_history(run)
    assert [h["approach"] for h in history] == [1, 2]
    a1 = history[0]
    assert a1["archived"] is True
    assert a1["archivedDir"] == "approaches/01"
    assert a1["taskTally"]["total"] == 2
    assert a1["review"]["verdict"] == "rejected"
    assert [f["clause"] for f in a1["review"]["blocking"]] == ["req A"]
    assert [f["clause"] for f in a1["review"]["residual"]] == ["req B"]
    assert a1["verdictGate"] is None  # never written for this approach
    a2 = history[1]
    assert a2["archived"] is False
    assert a2["archivedDir"] is None
    assert a2["review"] is None  # nothing written at the run root either


def test_approach_history_reads_live_channel_off_the_run_root_when_never_archived(tmp_path):
    run = _run(tmp_path)
    write_findings_json(run.findings_json_file, [
        {"severity": "residual", "curable": True, "clause": "req C",
         "evidence": "noted, not blocking"},
    ])
    run.write_tasks({"tasks": [{"id": "001", "status": "validated"}]})
    history = run_record.build_approach_history(run)
    assert len(history) == 1
    live = history[0]
    assert live["approach"] == 1
    assert live["archived"] is False
    assert live["taskTally"] == {"total": 1, "validated": 1}
    assert live["review"]["verdict"] == "verified"


def test_approach_history_never_duplicates_an_archived_approach_as_live(tmp_path):
    """The current approach in status.json IS the one just archived (the
    ordinary rejection path archives before advancing `approach`) -- must
    not appear twice."""
    run = _run(tmp_path)
    adir = run.root / "approaches" / "01"
    adir.mkdir(parents=True)
    atomic_write_json(adir / "tasks.json", {"tasks": []})
    run.update_status(approach=1)
    history = run_record.build_approach_history(run)
    assert len(history) == 1
    assert history[0]["archived"] is True


def test_approach_history_verdict_gate_channel_attributed_separately(tmp_path):
    run = _run(tmp_path)
    adir = run.root / "approaches" / "01"
    adir.mkdir(parents=True)
    atomic_write_json(adir / "tasks.json", {"tasks": []})
    write_findings_json(adir / VERDICT_GATE_FINDINGS_JSON_FILE, [
        {"severity": "blocking", "curable": False, "clause": "gate req",
         "evidence": "peer disagreed"},
    ])
    run.update_status(approach=2)
    history = run_record.build_approach_history(run)
    a1 = history[0]
    assert a1["review"] is None
    assert a1["verdictGate"]["verdict"] == "rejected"
    assert a1["verdictGate"]["blocking"][0]["clause"] == "gate req"


# ---------------------------------------------------------------------------
# engine.run_record: petitions / steers / operator rulings / notifications
# ---------------------------------------------------------------------------

def test_petitions_section_reads_confirmed_adjudications_file(tmp_path):
    run = _run(tmp_path)
    record_confirmed_adjudications(
        run, [{"obligation": "Cannot reach the network here.",
              "adjudication": "CONFIRMED"}])
    section = run_record.build_petitions_section(run)
    assert section["tampered"] is False
    assert section["entries"] == [
        {"obligation": "Cannot reach the network here.",
         "adjudication": "CONFIRMED"}]


def test_petitions_section_empty_when_nothing_ever_confirmed(tmp_path):
    run = _run(tmp_path)
    section = run_record.build_petitions_section(run)
    assert section == {"entries": [], "tampered": False}


def test_steers_section_only_lists_consumed_files(tmp_path):
    run = _run(tmp_path)
    f1 = run.add_steering("do the thing", name="one")
    run.add_steering("do the other thing", name="two")
    run.consume_steering([run.steering_dir / f1], iteration=1)
    steers = run_record.build_steers_section(run)
    assert [s["file"] for s in steers] == [f1]
    assert steers[0]["consumed"] is True


def test_operator_rulings_section_reads_config_root_amendments(tmp_path):
    cdir = tmp_path / "config"
    write_amendment(cdir, "003", "waived for the run.", waive=True)
    rulings = run_record.build_operator_rulings_section(cdir)
    assert [r["taskId"] for r in rulings] == ["003"]
    assert rulings == amendment_entries(cdir)


def test_notifications_section_empty_until_a_notification_sent_event_exists(tmp_path):
    run = _run(tmp_path)
    assert run_record.build_notifications_section(run) == []
    run.emit(run_record.NOTIFICATION_SENT_EVENT, endpoint="http://x",
             textSha256="abc", httpStatus=200)
    notifications = run_record.build_notifications_section(run)
    assert len(notifications) == 1
    assert notifications[0]["httpStatus"] == 200


# ---------------------------------------------------------------------------
# engine.run_record: the whole record + markdown rendering
# ---------------------------------------------------------------------------

def test_build_run_record_carries_usage_and_iterations_from_status_json(tmp_path):
    run = _run(tmp_path)
    run.update_status(state="succeeded", verdict="verified", approach=1,
                      iterationsUsed=12,
                      usage={"totalTokens": 500, "costUSD": 0.5})
    record = run_record.build_run_record(
        run, run_id="unit-run", max_approaches=3, config_root=tmp_path / "cfg")
    assert record["runId"] == "unit-run"
    assert record["state"] == "succeeded"
    assert record["verdict"] == "verified"
    assert record["iterationsUsed"] == 12
    assert record["usage"] == {"totalTokens": 500, "costUSD": 0.5}
    assert record["maxApproaches"] == 3
    assert record["notifications"] == []
    assert record["petitions"] == {"entries": [], "tampered": False}
    assert record["steersConsumed"] == []
    assert record["operatorRulings"] == []
    assert isinstance(record["approaches"], list) and record["approaches"]


def test_markdown_renders_the_same_record_for_a_human(tmp_path):
    run = _run(tmp_path)
    write_findings_json(run.findings_json_file, [
        {"severity": "blocking", "curable": True, "clause": "req D",
         "evidence": "not done yet"},
    ])
    run.update_status(state="failed", verdict="unverified", approach=1,
                      iterationsUsed=3, usage={"totalTokens": 10})
    record_confirmed_adjudications(
        run, [{"obligation": "no network route here",
              "adjudication": "CONFIRMED"}])
    f1 = run.add_steering("focus on X", name="focus")
    run.consume_steering([run.steering_dir / f1], iteration=1)
    record = run_record.build_run_record(
        run, run_id="unit-run", max_approaches=1, config_root=tmp_path / "cfg")
    md = run_record.render_run_record_markdown(record)
    assert "unit-run" in md
    assert "`failed`" in md
    assert "req D" in md and "not done yet" in md
    assert "no network route here" in md
    assert f1 in md
    assert "## Petitions" in md and "## Steers consumed" in md
    assert "## Notifications" in md and "## Approach history" in md


# ---------------------------------------------------------------------------
# LoopSupervisor: write at entry to finishing, rewrite on later promotion
# ---------------------------------------------------------------------------

def _json_record(run: RunDir) -> dict:
    return json.loads((run.artifacts_dir / "run-record.json").read_text())


def test_finish_writes_both_files_with_reflect_false(tmp_path, monkeypatch):
    monkeypatch.setattr(loop_mod, "CONFIG_DIR", tmp_path / "no-config")
    run = _run(tmp_path)
    sup = _supervisor(tmp_path, run, reflect=False)
    sup._finish("succeeded", "verified")
    assert (run.artifacts_dir / "run-record.json").is_file()
    assert (run.artifacts_dir / "run-record.md").is_file()
    record = _json_record(run)
    assert record["state"] == "succeeded"
    assert record["verdict"] == "verified"
    assert record["runId"] == "unit-run"


def test_finish_writes_both_files_with_reflect_true_state_still_finishing(tmp_path, monkeypatch):
    monkeypatch.setattr(loop_mod, "CONFIG_DIR", tmp_path / "no-config")
    run = _run(tmp_path)
    sup = _supervisor(tmp_path, run, reflect=True)
    sup._finish("succeeded", "verified")
    record = _json_record(run)
    # `_finish()` with reflect:true leaves status.json's own `state` at the
    # non-terminal `finishing` -- the run record reads that same status.json,
    # so it must say the same thing until `_promote_finishing_state()` runs.
    assert run.read_status()["state"] == "finishing"
    assert record["state"] == "finishing"
    assert record["verdict"] == "verified"


def test_promote_finishing_state_rewrites_the_record_to_the_terminal_state(tmp_path, monkeypatch):
    monkeypatch.setattr(loop_mod, "CONFIG_DIR", tmp_path / "no-config")
    run = _run(tmp_path)
    sup = _supervisor(tmp_path, run, reflect=True)
    sup._finish("succeeded", "verified")
    assert _json_record(run)["state"] == "finishing"
    # Something happens in the reflect window that the rewritten record must
    # pick up (a steer consumed just before the run ended).
    f1 = run.add_steering("late note", name="late")
    run.consume_steering([run.steering_dir / f1], iteration=99)
    sup._promote_finishing_state()
    record = _json_record(run)
    assert record["state"] == "succeeded"
    assert [s["file"] for s in record["steersConsumed"]] == [f1]


def test_promote_recorded_finishing_state_rewrites_the_record(tmp_path, monkeypatch):
    """The resumed-process counterpart: a fresh `LoopSupervisor` that never
    called `_finish()` at all, reading a `finishing` status back off disk
    (task 012, #56) -- the record must still be (re)written at that point,
    not only when the same process called `_finish()` itself."""
    monkeypatch.setattr(loop_mod, "CONFIG_DIR", tmp_path / "no-config")
    run = _run(tmp_path)
    run.update_status(state="finishing", verdict="verified",
                      pendingTerminalState="succeeded", approach=1)
    sup = _supervisor(tmp_path, run, reflect=True)
    state = sup._promote_recorded_finishing_state(run.read_status())
    assert state == "succeeded"
    record = _json_record(run)
    assert record["state"] == "succeeded"
    assert record["verdict"] == "verified"


def test_promote_recorded_finishing_state_still_writes_record_on_corrupt_status(tmp_path, monkeypatch):
    monkeypatch.setattr(loop_mod, "CONFIG_DIR", tmp_path / "no-config")
    run = _run(tmp_path)
    run.update_status(state="finishing", pendingTerminalState="not-a-real-state")
    sup = _supervisor(tmp_path, run, reflect=True)
    state = sup._promote_recorded_finishing_state(run.read_status())
    assert state == "failed"
    record = _json_record(run)
    assert record["state"] == "failed"


def test_operator_rulings_flow_through_to_the_written_record(tmp_path, monkeypatch):
    cdir = tmp_path / "config"
    write_amendment(cdir, "007", "waived.", waive=True)
    monkeypatch.setattr(loop_mod, "CONFIG_DIR", cdir)
    run = _run(tmp_path)
    sup = _supervisor(tmp_path, run, reflect=False)
    sup._finish("failed", "unverified")
    record = _json_record(run)
    assert [r["taskId"] for r in record["operatorRulings"]] == ["007"]


# ----------------------------------------------------------------------
# Folded from tests/test_run_record_approach_metadata_task035.py (task 028, requirement D.7).
# ----------------------------------------------------------------------

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
from tests.conftest import make_supervisor


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------

def _result(text: str) -> IterationResult:
    r = IterationResult(exit_code=0)
    r.final_text = text
    r.duration_s = 3.0
    r.usage = {"input": 12, "output": 7, "totalTokens": 19, "costUSD": 0.02}
    return r


def _no_config_dir(monkeypatch, root: Path) -> Path:
    """Point the loop module's `CONFIG_DIR` at a directory that does not exist
    -- what the run record says without a config dir is this module's subject
    -- and hand `root` back so a binding can use it inline."""
    monkeypatch.setattr(loop_mod, "CONFIG_DIR", root / "no-config")
    return root


_rram035_supervisor = lambda root, monkeypatch, **cfg: make_supervisor(
    _no_config_dir(monkeypatch, root),
    **{"run_id": "task035-approach-metadata", "cfg_defaults": False,
       "prd": "# task 035 fixture PRD\n\nDo the thing.\n",
       "status": {"state": "running"},
       "tasks": [{"id": "001", "title": "t", "successCriteria": "c",
                  "status": "completed"}],
       "max_approaches": 1, "reflect": False,
       # Task 014 (PRD requirement A.1, #224): `cfg_defaults=False` means
       # the bare `JobConfig` default applies, and that default is now
       # `True` for `operator_input_needed_pause` -- explicitly off below,
       # so this module's own subject (the archived-approach-history
       # record, not task 014's own gate) still reaches its own
       # ordinary archive/replan/tail outcomes; task 014's own edge has
       # its own module (`tests/test_operator_input_needed_pause.py`).
       "operator_input_needed_pause": False, **cfg})


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
    sup = _rram035_supervisor(root, monkeypatch, max_approaches=2)
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
    sup = _rram035_supervisor(root, monkeypatch, max_approaches=1)
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


def test_review_verdict_now_pauses_when_the_sidecar_does_not_validate(
        tmp_path, monkeypatch):
    """Task 012 (PRD requirement C.3, #219): the inverted sibling of this
    node's own former self.

    Under its previous name -- `test_review_verdict_is_persisted_when_the_
    sidecar_does_not_validate` -- this node pinned task 002's original
    decision for a present-but-unreadable sidecar: the refusal was scored
    an ordinary rejection, `_step_review` took `rejected_approach_left`,
    and the run moved straight on to archiving the approach. Requirement C
    (tasks 010-011, #219) supersedes exactly that run-level consequence --
    a present-but-invalid findings sidecar is one of the three named
    `review_findings_unusable` shapes, so the SAME channel state this node
    has always driven now lands on the discriminating pause instead: the
    approach stays intact, nothing is archived, no composite PRD is
    written, and an operator has to look at the sidecar before the run
    moves again.

    What task 002 actually decided survives underneath the pause,
    unchanged: `_review_verdict` still folds the unreadable channel into
    the recorded verdict itself, so the approach's own metadata still
    reads `verdict: "rejected"` with `findingsChannel: false` explaining
    why its `blocking`/`residual` arrays are empty, exactly as before --
    it is only the run's OWN next move (pause vs. spend-the-approach) that
    requirement C changes, not what gets written about this approach.
    """
    root = tmp_path / "run"
    sup = _rram035_supervisor(root, monkeypatch, max_approaches=1)
    sup._approach = 1
    sup.run.findings_json_file.write_text('[{"severity": "blocking"}]')
    _stub_runner(sup, {"verdict": "verified", "reason": "all good"})

    outcome = asyncio.run(sup._step_review())

    # the new, discriminating pause -- not the ordinary rejection ladder
    # this node used to pin.
    assert outcome == ("advance", "paused", "review_findings_unusable")
    status = sup.run.read_status()
    assert status["state"] == "paused"
    assert status["pauseReason"] == "review_findings_unusable"
    assert sup._approach == 1                     # never archived
    assert not (root / "approaches" / "01").exists()
    assert not sup.run.composite_prd_file.exists()

    # task 002's own recorded decision, still intact underneath the pause:
    # the unreadable channel still forces the recorded verdict itself.
    entry = state_mod.approach_metadata_channel(root, 1, "review")
    assert entry["verdict"] == "rejected"
    assert entry["findingsChannel"] is False


def test_midrun_adjudication_episode_records_no_approach_verdict(tmp_path, monkeypatch):
    """A mid-run adjudication reply (task 014, requirement F.1) scores no
    approach verdict -- it must never be recorded as a rejection."""
    root = tmp_path / "run"
    sup = _rram035_supervisor(root, monkeypatch, max_approaches=1)
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
    sup = _rram035_supervisor(root, monkeypatch, max_approaches=1)
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
    sup = _rram035_supervisor(root, monkeypatch, max_approaches=2)
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
    sup = _rram035_supervisor(root, monkeypatch, max_approaches=2,
                      peer_verdict_gate=True)
    sup._approach = 1
    findings_mod.write_findings_json(sup.run.verdict_gate_findings_json_file, [
        {"severity": "blocking", "curable": False, "clause": "gate req",
         "evidence": "peer disagreed"},
    ])
    _stub_runner(sup, {"verdict": "rejected", "reason": "gate says no"})

    outcome = asyncio.run(sup._step_verdict_gate())
    # v0.21.1 requirement A.1/A.2 (task 001): a non-curable blocking
    # finding ends the run right here, whatever approaches remain --
    # never a replan, even though `max_approaches=2` leaves one available.
    assert outcome == ("tail",)

    entry = state_mod.approach_metadata_channel(root, 1, "verdictGate")
    assert entry["verdict"] == "rejected"
    assert entry["reason"] == "gate says no"
    assert entry["findingsChannel"] is True
    assert [f["clause"] for f in entry["blocking"]] == ["gate req"]
    assert state_mod.approach_metadata_channel(root, 1, "review") is None

    sup._finish("failed", "unverified")
    archived = _record(sup.run)["approaches"][0]
    # A.2: never archived on this path -- the metadata channel above is
    # still recorded, but there is no `approaches/01` archive dir.
    assert archived["archived"] is False
    assert archived["archivedDir"] is None
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
