"""PRD v0.37 requirement E and decision 5: the last phase has no gate, so its
completion goes straight to the whole-PRD final review (and, with the peer
gate on, the final peer gate); an intermediate phase's pass never ends the
run; the final judges read the earlier phase reviews as earlier verdicts, not
evidence; archiving moves a phased approach's records and the next approach
plans afresh; the run record carries the plan phases.

The loop is driven with fake hats over a git workspace, as
`tests/test_phase_review_dispatch.py` does (its helpers are reused); nothing
sleeps."""

from __future__ import annotations

import asyncio
import json

import pytest

from ralphd.engine import findings, run_record
from ralphd.engine.state import RunDir, atomic_write_json

from tests.conftest import scripted_supervisor
from tests.test_phase_review_dispatch import (
    _events, _git, _init_workspace, _ok, _plan, _worker)

_supervisor = scripted_supervisor(
    run_id="phase-gate-final", run_root="run", workspace="workspace",
    iterations=40, max_approaches=1, vigilant=False, on_complete="exit",
    infra_outage_budget_s=1000.0, operator_input_needed_pause=False)

PEER = pytest.mark.parametrize("peer_on", [False, True],
                               ids=["peer_gate_off", "peer_gate_on"])


class _Fakes:
    """Review, peer and worker fakes that record, per dispatch, the prompt,
    the iteration meta and the run state they saw."""

    def __init__(self):
        self.calls: list[dict] = []

    def _note(self, hat, sup, prompt):
        meta = json.loads((sup.run.iteration_dir(sup.iterations_used)
                           / "meta.json").read_text())
        status = sup.run.read_status()
        self.calls.append({
            "hat": hat, "prompt": prompt, "meta": meta,
            "state": status.get("state"), "verdict": status.get("verdict"),
            "outcomes": len(_events(sup, "approach.verdict_recorded")),
            "claimed": [t["id"] for t in sup.run.read_tasks()["tasks"]
                        if t["status"] == "in-progress"]})

    def review(self, sup, prompt=""):
        self._note("review", sup, prompt)
        return _ok(json.dumps({"verdict": "verified", "reason": "fine"}))

    def peer(self, sup, prompt=""):
        self._note("verdict_gate", sup, prompt)
        return _ok(json.dumps({"verdict": "verified", "reason": "agreed"}))

    def worker(self, sup, prompt=""):
        self._note("worker", sup, prompt)
        return _worker(sup, prompt)


def _run(tmp_path, fakes, *, peer_on, **cfg):
    _init_workspace(tmp_path / "workspace")
    RunDir(root=tmp_path / "run").update_status(state="starting")
    sup = _supervisor(
        tmp_path, {"planning": [_plan], "worker": [fakes.worker],
                   "review": [fakes.review], "verdict_gate": [fakes.peer]},
        peer_verdict_gate=peer_on, **cfg)
    final = asyncio.run(asyncio.wait_for(sup._run_job_core(), 60))
    return sup, final


def _is_phase_scoped(call) -> bool:
    return (call["meta"].get("scope") == "phase"
            or "planPhase" in call["meta"]
            or "## Phase review" in call["prompt"])


# -- decision 5: the last phase has no gate -----------------------------------

@PEER
def test_last_phase_completion_dispatches_final_review(tmp_path, peer_on):
    fakes = _Fakes()
    sup, final = _run(tmp_path, fakes, peer_on=peer_on)
    assert final == "succeeded", sup.run.read_status()
    expected = ["planning", "worker", "review"]
    if peer_on:
        expected.append("verdict_gate")
    expected.append("worker")          # the last phase's one task
    expected.append("review")          # straight to the final review
    if peer_on:
        expected.append("verdict_gate")  # and the final peer gate after it
    assert sup.runner.phases_seen == expected
    # the judges after the last phase-2 task: no phase review, no
    # phase-scoped peer gate for phase two in between.
    judges = [c for c in fakes.calls if c["hat"] != "worker"]
    phase_one, final_judges = judges[:2 if peer_on else 1], \
        judges[2 if peer_on else 1:]
    assert all(_is_phase_scoped(c) and c["meta"]["planPhase"] == "one"
               for c in phase_one)
    assert [c["hat"] for c in final_judges] == (
        ["review", "verdict_gate"] if peer_on else ["review"])
    for c in final_judges:
        assert not _is_phase_scoped(c)
        assert "scope" not in c["meta"] and "planPhase" not in c["meta"]
        assert "## Phase review" not in c["prompt"]
    data = sup.run.read_tasks()
    assert [p["gate"] for p in data["phases"]] == ["passed", "closed"]
    assert data["phases"][1]["records"] == []
    assert [e["planPhase"] for e in _events(sup, "phase_gate.due")] == ["one"]
    assert not list((sup.run.artifacts_dir / "phase-reviews").glob("two-*"))


@PEER
def test_intermediate_phase_pass_never_ends_run(tmp_path, peer_on):
    fakes = _Fakes()
    sup, final = _run(tmp_path, fakes, peer_on=peer_on)
    assert final == "succeeded", sup.run.read_status()
    hats = [c["hat"] for c in fakes.calls]
    # the next dispatch after phase one's judges is a phase-two worker
    last_phase_one_judge = max(
        i for i, c in enumerate(fakes.calls)
        if c["meta"].get("planPhase") == "one")
    nxt = fakes.calls[last_phase_one_judge + 1]
    assert nxt["hat"] == "worker" and nxt["claimed"] == ["002"]
    assert hats[last_phase_one_judge] == (
        "verdict_gate" if peer_on else "review")
    # while that worker ran the run was still running, with no outcome
    assert nxt["state"] == "running"
    assert nxt["verdict"] is None
    assert nxt["outcomes"] == 0
    # the only recorded outcome is the final judges' own
    assert [e["channel"] for e in _events(sup, "approach.verdict_recorded")
            ] == (["review", "verdictGate"] if peer_on else ["review"])
    assert sup.run.read_status()["verdict"] == "verified"


# -- E.1: the earlier phase reviews reach the final judges ---------------------

HEADING = "## Earlier phase reviews"
RULES = (
    "these are earlier verdicts, not evidence",
    "you may re-check anything they cover",
    "do not reject a requirement only because the evidence a passed gate "
    "saw was later destroyed by a planned step",
)


def _records_fixture(sup):
    """Two phase records on disk and listed in `phases[]`: phase one's
    passing review (round 1) and phase two's rejected peer (round 2) with a
    blocking finding."""
    head = _git(sup.workspace, "rev-parse", "HEAD")
    rdir = sup.run.artifacts_dir / "phase-reviews"
    rdir.mkdir(parents=True)
    rec_one = {"planPhase": "one", "round": 1, "judge": "review",
               "verdict": "verified", "findings": [], "reason": "all good",
               "iteration": 1, "startHead": head, "headReviewed": head}
    rec_two = {"planPhase": "two", "round": 2, "judge": "verdict_gate",
               "verdict": "rejected", "reason": "second look",
               "findings": [{"severity": "blocking", "curable": True,
                             "clause": "requirement Q",
                             "evidence": "Q is not met in module m",
                             "cured_when": "Q is met"}],
               "iteration": 4, "startHead": head, "headReviewed": head}
    atomic_write_json(rdir / "one-r1-review.json", rec_one)
    atomic_write_json(rdir / "two-r2-verdict_gate.json", rec_two)
    base = "artifacts/phase-reviews/"
    sup.run.write_tasks({
        "version": 1,
        "phases": [
            {"id": "one", "title": "P1", "reviewFocus": "", "gate": "passed",
             "rounds": 1, "startHead": head,
             "records": [base + "one-r1-review.json"]},
            {"id": "two", "title": "P2", "reviewFocus": "", "gate": "passed",
             "rounds": 2, "startHead": head,
             "records": [base + "two-r2-verdict_gate.json"]},
            {"id": "three", "title": "P3", "reviewFocus": "",
             "gate": "closed", "rounds": 0, "startHead": head,
             "records": []}],
        "tasks": [
            {"id": "001", "title": "a", "phase": "one",
             "status": "completed", "successCriteria": "c"},
            {"id": "002", "title": "b", "phase": "two",
             "status": "completed", "successCriteria": "c"},
            {"id": "003", "title": "c", "phase": "three",
             "status": "completed", "successCriteria": "c"}]})


def _assert_section(prompt: str) -> None:
    assert HEADING in prompt
    section = prompt[prompt.index(HEADING):].lower()
    for needle in ("phase `one`", "round 1", "judge `review`",
                   "verdict: verified", "reason: all good", "findings: (none)",
                   "phase `two`", "round 2", "judge `verdict_gate`",
                   "verdict: rejected", "reason: second look",
                   "[blocking]", "requirement q", "q is not met in module m"):
        assert needle in section, needle
    for rule in RULES:
        assert rule in section, rule
    # phase one is listed before phase two
    assert section.index("phase `one`") < section.index("phase `two`")


def test_the_final_review_and_the_final_peer_gate_carry_the_earlier_phase_reviews(tmp_path):
    seen: dict = {}

    def peer(sup, prompt=""):
        seen["peer"] = prompt
        return _ok(json.dumps({"verdict": "verified", "reason": "ok"}))

    _init_workspace(tmp_path / "workspace")
    RunDir(root=tmp_path / "run").update_status(state="starting")
    sup = _supervisor(tmp_path, {"verdict_gate": [peer]},
                      peer_verdict_gate=True)
    sup._approach = 1
    _records_fixture(sup)
    review_prompt = sup.build_prompt("review", extra=sup._review_extra_context())
    _assert_section(review_prompt)
    assert "## Phase review" not in review_prompt
    asyncio.run(sup._dispatch_verdict_gate())
    _assert_section(seen["peer"])
    assert "## Phase review\n" not in seen["peer"]


@PEER
def test_the_final_judges_of_a_driven_phased_run_read_the_phase_one_records(tmp_path, peer_on):
    fakes = _Fakes()
    sup, final = _run(tmp_path, fakes, peer_on=peer_on)
    assert final == "succeeded", sup.run.read_status()
    finals = [c for c in fakes.calls
              if c["hat"] != "worker" and not _is_phase_scoped(c)]
    assert [c["hat"] for c in finals] == (
        ["review", "verdict_gate"] if peer_on else ["review"])
    for c in finals:
        assert HEADING in c["prompt"]
        section = c["prompt"][c["prompt"].index(HEADING):]
        assert "Phase `one`, round 1, judge `review`" in section
        assert ("Phase `one`, round 1, judge `verdict_gate`" in section
                ) is peer_on
        assert "Phase `two`" not in section
    # the phase-scoped judges themselves never carry it
    for c in fakes.calls:
        if _is_phase_scoped(c):
            assert HEADING not in c["prompt"]


def test_no_earlier_phase_reviews_section_on_a_plan_without_phases(tmp_path):
    seen: dict = {}

    def peer(sup, prompt=""):
        seen["peer"] = prompt
        return _ok(json.dumps({"verdict": "verified", "reason": "ok"}))

    _init_workspace(tmp_path / "workspace")
    RunDir(root=tmp_path / "run").update_status(state="starting")
    sup = _supervisor(tmp_path, {"verdict_gate": [peer]},
                      peer_verdict_gate=True)
    sup._approach = 1
    sup.run.write_tasks({"version": 1, "tasks": [
        {"id": "001", "title": "a", "status": "completed",
         "successCriteria": "c"}]})
    assert "phases" not in sup.run.read_tasks()
    assert HEADING not in sup.build_prompt(
        "review", extra=sup._review_extra_context())
    asyncio.run(sup._dispatch_verdict_gate())
    assert HEADING not in seen["peer"]
    assert "Earlier phase" not in seen["peer"]


# -- E.2: archiving and the next approach --------------------------------------

def _flat_plan(sup, prompt=""):
    atomic_write_json(sup.run.root / "plan-proposal.json", {
        "version": 1,
        "tasks": [{"id": "101", "title": "flat", "successCriteria": "crit"}]})
    return _ok(json.dumps({"proposal_path": "plan-proposal.json",
                           "summary": "planned afresh"}))


def test_archiving_copies_the_phase_records_and_the_next_approach_starts_empty(tmp_path):
    state_at_replan: dict = {}
    reviews = []

    def review(sup, prompt=""):
        phased = bool(sup.run.read_tasks().get("phases"))
        reviews.append((sup._approach, "## Phase review" in prompt))
        if phased and "## Phase review" not in prompt:
            findings.write_findings_json(sup.run.findings_json_file, [
                {"severity": "blocking", "curable": False,
                 "clause": "requirement Z", "evidence": "Z is not met",
                 "cured_when": "never"}])
            return _ok(json.dumps({"verdict": "rejected", "reason": "no"}))
        sup.run.findings_json_file.unlink(missing_ok=True)
        return _ok(json.dumps({"verdict": "verified", "reason": "fine"}))

    def plan(sup, prompt=""):
        if sup._approach == 1:
            return _plan(sup, prompt)
        # the archive is complete by the time approach two plans
        arch = sup.run.root / "approaches" / "01"
        state_at_replan["archived"] = sorted(
            p.name for p in (arch / "phase-reviews").iterdir())
        state_at_replan["archived_tasks"] = json.loads(
            (arch / "tasks.json").read_text())
        return _flat_plan(sup, prompt)

    _init_workspace(tmp_path / "workspace")
    RunDir(root=tmp_path / "run").update_status(state="starting")
    (tmp_path / "run" / "prd.md").write_text("# PRD\n")
    sup = _supervisor(
        tmp_path, {"planning": [plan, plan], "worker": [_worker, _worker, _worker],
                   "review": [review]},
        max_approaches=2, peer_verdict_gate=False, cure_pass_ceiling=0,
        iterations=60)
    final = asyncio.run(asyncio.wait_for(sup._run_job_core(), 60))
    assert final == "succeeded", sup.run.read_status()
    # approach one ran its phase review and a rejecting final review
    assert reviews[0] == (1, True) and reviews[1] == (1, False)
    # the archive holds approach one's records ...
    assert state_at_replan["archived"] == [
        "one-r1-review.json", "one-r1-review.md"]
    assert [p["id"] for p in state_at_replan["archived_tasks"]["phases"]
            ] == ["one", "two"]
    arch = sup.run.root / "approaches" / "01" / "phase-reviews"
    assert json.loads((arch / "one-r1-review.json").read_text()
                      )["verdict"] == "verified"
    # ... and approach two planned afresh: no phases, gates, rounds or
    # records, no phase-reviews file, no phase event for approach two.
    data = sup.run.read_tasks()
    assert "phases" not in data
    assert all("phase" not in t for t in data["tasks"])
    live = sup.run.artifacts_dir / "phase-reviews"
    assert not live.exists() or not list(live.iterdir())
    for e in _events(sup):
        if str(e.get("type", "")).startswith("phase_gate."):
            assert e.get("approach") == 1
    assert not (sup.run.root / "approaches" / "02" / "phase-reviews").exists()


# -- E.3: the run record -------------------------------------------------------

def _record_run(tmp_path, *, phased: bool):
    run = RunDir(root=tmp_path / "run")
    run.update_status(state="running", approach=1)
    base = "artifacts/phase-reviews/"
    if phased:
        run.write_tasks({
            "version": 1,
            "phases": [
                {"id": "one", "title": "Phase one title", "reviewFocus": "f",
                 "gate": "passed", "rounds": 2, "startHead": "abc",
                 "records": [base + "one-r1-review.json",
                             base + "one-r1-review.md",
                             base + "one-r2-review.json",
                             base + "one-r2-review.md"]},
                {"id": "two", "title": "Phase two title", "reviewFocus": "g",
                 "gate": "closed", "rounds": 0, "startHead": "abc",
                 "records": []}],
            "tasks": [
                {"id": "001", "title": "first | task", "phase": "one",
                 "status": "completed", "successCriteria": "c"},
                {"id": "002", "title": "second task", "phase": "two",
                 "status": "pending", "successCriteria": "c"}]})
    else:
        run.write_tasks({"version": 1, "tasks": [
            {"id": "001", "title": "first task", "status": "completed",
             "successCriteria": "c"}]})
    record = run_record.build_run_record(
        run, run_id="unit-run", max_approaches=2, config_root=tmp_path / "cfg")
    return record, run_record.render_run_record_markdown(record)


def test_the_run_record_of_a_phased_plan_has_plan_phases_and_a_phase_column(tmp_path):
    record, md = _record_run(tmp_path, phased=True)
    one, two = record["planPhases"]
    assert one == {"id": "one", "title": "Phase one title", "gate": "passed",
                   "rounds": 2,
                   "records": ["artifacts/phase-reviews/one-r1-review.json",
                               "artifacts/phase-reviews/one-r1-review.md",
                               "artifacts/phase-reviews/one-r2-review.json",
                               "artifacts/phase-reviews/one-r2-review.md"]}
    assert (two["id"], two["gate"], two["rounds"], two["records"]) == (
        "two", "closed", 0, [])
    assert [(t["id"], t["phase"]) for t in record["taskListing"]] == [
        ("001", "one"), ("002", "two")]
    assert "## Plan phases" in md
    section = md[md.index("## Plan phases"):md.index("## Tasks")]
    assert "`one` Phase one title: gate `passed`, rounds 2" in section
    assert "`artifacts/phase-reviews/one-r2-review.json`" in section
    assert "`two` Phase two title: gate `closed`, rounds 0" in section
    listing = md[md.index("## Tasks"):]
    assert "| id | phase | status | title |" in listing
    assert "| 001 | one | completed | first \\| task |" in listing
    assert "| 002 | two | pending | second task |" in listing
    # the sections the phased keys declare are exactly the ones rendered
    for key, heading in run_record.PHASED_RECORD_SECTIONS.items():
        assert key in record and f"## {heading}" in md


def test_the_run_record_of_a_plan_without_phases_has_neither(tmp_path):
    record, md = _record_run(tmp_path, phased=False)
    assert not set(run_record.PHASED_RECORD_SECTIONS) & set(record)
    assert set(record) == set(run_record.RECORD_SECTIONS)
    assert "## Plan phases" not in md
    assert "## Tasks" not in md
    assert "| phase |" not in md
