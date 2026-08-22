"""Refund counters survive a resume (task 023, #32).

`budget_left()` charges `iterations_used - _infra_refunded - _grace_refunded`,
but both refund counters used to start at 0 in every engine process while
`iterations_used` was seeded from the *raw* `max_iteration_number()`. So every
`ralphctl resume` -- and every auto-resume after a crash -- silently re-charged
the refunds earned before it: `selfdev-v06-release` earned 27 infra refunds and
kept 10, and its permanent record says 145 iterations used where the guarantee
implies 128.

The fix persists the counters (`status.json`'s `iterationsRefunded`,
`LoopSupervisor._refund_iteration()`) and seeds them back (`_seed_refunds()`),
deliberately rather than seeding `iterations_used` from a charged count: that
counter is also the number of the next iteration directory, and reusing the
number of a *finished* iteration would make `begin_iteration_dir()` (task 019,
#44) archive a completed record as a crashed attempt.

Covered here: the write as a refund is earned (both kinds), the seed, and
`budget_left()` itself -- credited after a resume, still enforced, unbreakable
by a corrupt record -- the combination with task 019's reused slot, a real
engine killed after earning a refund and resumed, and the doc claims.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import signal
import time
from pathlib import Path

import pytest
from test_e2e import engine_factory

from ralphd.engine.config import JobConfig
from ralphd.engine.loop import LoopSupervisor
from ralphd.engine.runner import IterationResult
from ralphd.engine.state import RunDir, atomic_write_json
from ralphd.log_merge import iteration_attempt_dirs, iteration_numbers

__all__ = ["engine_factory"]

REPO = Path(__file__).resolve().parent.parent
INFRA_ERROR = "Connection error."


# -- helpers ---------------------------------------------------------------
def _seed_run(tmp_path, finished: int = 0, refunded: dict | None = None,
              crashed: int | None = None, extra: dict | None = None) -> RunDir:
    """A run dir as an earlier engine process left it: `finished` iterations
    with an `endedAt`, optionally one `crashed` slot without one, and whatever
    `iterationsRefunded` record the run had earned."""
    run = RunDir(root=tmp_path / "run")
    atomic_write_json(run.tasks_file, {"version": 1, "tasks": [
        {"id": "001", "title": "t", "status": "completed",
         "successCriteria": "c"}]})
    for n in range(1, finished + 1):
        atomic_write_json(run.iteration_dir(n) / "meta.json",
                          {"number": n, "phase": "worker",
                           "startedAt": "2026-01-01T00:00:00Z",
                           "endedAt": "2026-01-01T00:01:00Z"})
    if crashed is not None:
        atomic_write_json(run.iteration_dir(crashed) / "meta.json",
                          {"number": crashed, "phase": "worker",
                           "startedAt": "2026-01-01T00:02:00Z"})
    status = {"runId": "unit", "state": "running"}
    if refunded is not None:
        status["iterationsRefunded"] = refunded
    status.update(extra or {})
    atomic_write_json(run.status_file, status)
    return run


def _supervisor(run: RunDir, iterations: int = 100) -> LoopSupervisor:
    cfg = JobConfig(run_id="unit", iterations=iterations,
                    infra_retry_backoff_s=[0.0], infra_retry_backoff_max_s=0.0,
                    infra_outage_budget_s=1000.0)
    return LoopSupervisor(cfg, run, run.root.parent)


def _infra_result() -> IterationResult:
    r = IterationResult(exit_code=0)
    r.error_message = INFRA_ERROR
    r.duration_s = 30.0  # not "instant": the retry-and-refund path
    return r


def _clean_result() -> IterationResult:
    r = IterationResult(exit_code=0)
    r.duration_s = 30.0
    r.final_text = "done"
    return r


def _stub_attempts(sup: LoopSupervisor, results: list[IterationResult]):
    """Feed the infra-retry wrapper one result per attempt (the last repeats),
    with the backoff wait replaced so nothing sleeps."""
    calls: list[str] = []

    async def fake_once(phase, extra="", prompt_name=None):
        calls.append(phase)
        # what the real _run_iteration_once does: take the next number and
        # publish the CHARGED count (loop.py's `n - self._infra_refunded`)
        sup.iterations_used += 1
        sup.run.update_status(
            iterationsUsed=sup.iterations_used - sup._infra_refunded)
        return results[min(len(calls) - 1, len(results) - 1)]

    async def fake_backoff(seconds):
        return seconds, False

    sup._run_iteration_once = fake_once  # type: ignore[method-assign]
    sup._wait_out_backoff = fake_backoff  # type: ignore[method-assign]
    return calls


def _refund_record(run: RunDir) -> dict:
    return json.loads(run.status_file.read_text()).get("iterationsRefunded")


# -- the write: a refund reaches disk as it is earned ----------------------
@pytest.mark.asyncio
async def test_an_infra_refund_is_recorded_in_status_as_it_is_earned(tmp_path):
    run = _seed_run(tmp_path)
    sup = _supervisor(run)
    _stub_attempts(sup, [_infra_result()] * 3 + [_clean_result()])

    result = await sup._run_iteration_with_infra_retry("worker", "", None)

    assert result.final_text == "done", "the wrapper rode the outage out"
    assert sup._infra_refunded == 3
    assert _refund_record(run) == {"infra": 3, "grace": 0}


@pytest.mark.asyncio
async def test_a_grace_review_refund_is_recorded_in_status(tmp_path):
    run = _seed_run(tmp_path, finished=3)
    sup = _supervisor(run, iterations=3)

    async def fake_review(phase, extra="", prompt_name=None):
        assert phase == "review"
        sup.iterations_used += 1
        r = IterationResult(exit_code=0)
        r.duration_s = 5.0
        r.final_text = "not verified"  # so the job still ends failed
        return r

    sup.run_iteration = fake_review  # type: ignore[method-assign]
    verified = await sup._maybe_grace_review(1)

    assert verified is False
    assert sup._grace_refunded == 1
    assert _refund_record(run) == {"infra": 0, "grace": 1}


@pytest.mark.asyncio
async def test_a_live_runs_published_count_matches_the_charged_count(tmp_path):
    """The live half of the criteria: whatever the resume does, `status.json`'s
    `iterationsUsed` still publishes exactly `iterations_used_charged`."""
    run = _seed_run(tmp_path)
    sup = _supervisor(run)
    _stub_attempts(sup, [_infra_result()] * 2 + [_clean_result()])

    await sup._run_iteration_with_infra_retry("worker", "", None)

    status = json.loads(run.status_file.read_text())
    assert sup.iterations_used == 3
    assert status["iterationsUsed"] == sup.iterations_used_charged == 1


def test_each_kind_bumps_only_its_own_counter(tmp_path):
    sup = _supervisor(_seed_run(tmp_path, finished=4))
    assert LoopSupervisor.REFUND_KINDS == ("infra", "grace")
    sup._refund_iteration("infra")
    assert (sup._infra_refunded, sup._grace_refunded) == (1, 0)
    sup._refund_iteration("grace")
    assert (sup._infra_refunded, sup._grace_refunded) == (1, 1)
    assert _refund_record(sup.run) == {"infra": 1, "grace": 1}
    with pytest.raises(ValueError):
        sup._refund_iteration("approach")


# -- the seed: a new engine process over the same run dir ------------------
def test_a_resumed_engine_seeds_both_counters_from_the_run_dir(tmp_path):
    run = _seed_run(tmp_path, finished=6, refunded={"infra": 2, "grace": 1})
    sup = _supervisor(run)
    assert sup.iterations_used == 6, "numbering still continues from the raw max"
    assert (sup._infra_refunded, sup._grace_refunded) == (2, 1)
    assert sup.iterations_used_charged == 4


def test_a_pre_v07_run_dir_has_no_refunds_to_seed(tmp_path):
    """Back-compat: a run dir written before this field existed reads as zero
    refunds -- exactly the numbers it recorded, not a crash and not a credit."""
    run = _seed_run(tmp_path, finished=5)
    assert "iterationsRefunded" not in json.loads(run.status_file.read_text())
    sup = _supervisor(run)
    assert (sup._infra_refunded, sup._grace_refunded) == (0, 0)
    assert sup.iterations_used_charged == 5


def test_the_published_count_does_not_jump_back_up_after_a_resume(tmp_path):
    """What an operator sees across the resume: the dead engine published 4 of
    6 raw attempts as charged, and the next process's own publish expression
    (`iterations_used - _infra_refunded`, i.e. iterations_used_charged) has to
    produce that same 4 -- re-charging the refunds would silently walk the
    number an operator already read back up to 6."""
    run = _seed_run(tmp_path, finished=6, refunded={"infra": 2, "grace": 0},
                    extra={"iterationsUsed": 4})
    published = json.loads(run.status_file.read_text())["iterationsUsed"]
    assert _supervisor(run).iterations_used_charged == published == 4


# -- budget_left(), not just the counter -----------------------------------
def test_budget_left_credits_the_refunds_earned_before_the_resume(tmp_path):
    """THE defect, at the gate that decides whether the run may keep working:
    6 raw attempts of which 2 were refunded is 4 charged, so a 5-iteration job
    has budget left. Re-charging the refunds ends the run instead."""
    run = _seed_run(tmp_path, finished=6, refunded={"infra": 2, "grace": 0})
    assert _supervisor(run, iterations=5).budget_left() is True


def test_without_the_persisted_record_the_same_run_dir_is_out_of_budget(tmp_path):
    """The same run dir minus the persisted record -- i.e. what every resume
    saw before this task: the two refunds are re-charged and the job is over."""
    run = _seed_run(tmp_path, finished=6)
    assert _supervisor(run, iterations=5).budget_left() is False


def test_a_grace_refund_is_credited_after_a_resume_too(tmp_path):
    run = _seed_run(tmp_path, finished=4, refunded={"infra": 0, "grace": 1})
    assert _supervisor(run, iterations=4).budget_left() is True
    assert _supervisor(run, iterations=3).budget_left() is False


def test_the_budget_is_still_enforced_after_a_resume(tmp_path):
    """A credited refund is a discount, not an exemption: 5 raw minus 1 refund
    is 4 charged, which exhausts a 4-iteration budget."""
    run = _seed_run(tmp_path, finished=5, refunded={"infra": 1, "grace": 0})
    assert _supervisor(run, iterations=4).budget_left() is False


@pytest.mark.parametrize("record", [
    None,                                   # field absent
    "lots",                                 # not an object
    [1, 2],                                 # not an object either
    {},                                     # object, no counters
    {"infra": -3, "grace": -4},             # negative: never a credit
    {"infra": "two", "grace": None},        # non-numeric
    {"infra": 999, "grace": 999},           # more refunds than attempts
    {"infra": 3, "grace": 3},               # sum over the attempts on disk
])
def test_a_corrupt_refund_record_cannot_inflate_the_budget(tmp_path, record):
    run = _seed_run(tmp_path, finished=4,
                    refunded=record if record is not None else None)
    sup = _supervisor(run, iterations=4)
    assert sup._infra_refunded >= 0 and sup._grace_refunded >= 0
    assert sup._infra_refunded + sup._grace_refunded <= sup.iterations_used
    assert sup.iterations_used_charged >= 0
    # 4 attempts against a 4-iteration budget: no refund record, however
    # broken, may hand this run more work.
    assert (sup.budget_left() is False
            or sup._infra_refunded + sup._grace_refunded > 0
            and record not in (None, "lots", [1, 2], {}))


# -- the combination with task 019's reused iteration slot -----------------
def test_a_crashed_slot_and_its_archive_are_not_refunds_or_iterations(tmp_path):
    """Task 019 (#44) keeps a crashed attempt's files under
    `iterations/NNNN/attempts/NN/` and hands its NUMBER to the next attempt.
    Neither the unfinished slot nor the archive may change what this task
    seeds: 4 finished attempts, 1 refunded, slot 5 crashed."""
    run = _seed_run(tmp_path, finished=4, crashed=5,
                    refunded={"infra": 1, "grace": 0})
    # the resumed engine reuses slot 5, archiving what the dead one left
    run.begin_iteration_dir(5)
    assert len(iteration_attempt_dirs(run.root, 5)) == 1

    sup = _supervisor(run, iterations=4)
    assert sup.iterations_used == 4, "the unfinished slot is not a finished one"
    assert sup._infra_refunded == 1
    assert sup.iterations_used_charged == 3
    assert sup.budget_left() is True, "the refund still buys the 4th iteration"
    assert iteration_numbers(run.root) == [1, 2, 3, 4, 5], \
        "the archive is a record of an iteration, never an extra one"


def test_the_clamp_uses_the_finished_attempts_not_the_archive(tmp_path):
    """A run dir with one finished attempt and three archived ones may not
    seed more refunds than the finished count -- otherwise a resumed run's
    charged count could go negative."""
    run = _seed_run(tmp_path, finished=1, crashed=2,
                    refunded={"infra": 9, "grace": 0})
    for _ in range(3):
        atomic_write_json(run.iteration_dir(2) / "meta.json", {"number": 2})
        run.begin_iteration_dir(2)
    sup = _supervisor(run, iterations=1)
    assert sup.iterations_used == 1
    assert sup._infra_refunded == 1
    assert sup.iterations_used_charged == 0


# -- a real engine, killed after earning a refund, then resumed ------------
def _wait_for(predicate, timeout=40, interval=0.02):
    deadline = time.time() + timeout
    while time.time() < deadline:
        result = predicate()
        if result:
            return result
        time.sleep(interval)
    raise TimeoutError("condition never became true")


def _metas(run_dir):
    return [json.loads((d / "meta.json").read_text())
            for d in sorted((run_dir / "iterations").iterdir())
            if (d / "meta.json").exists()]


def test_a_refund_earned_before_a_crash_is_still_credited_after_the_resume(
        engine_factory):
    """Black box, end to end: a real engine earns one infra refund (a hung
    invocation killed by the startup watchdog, retried), is SIGKILLed, and the
    resumed engine finishes the run. The refund must still be credited in the
    figure the operator reads -- `iterationsUsed` one below the raw slot
    count, not equal to it."""
    e1 = engine_factory(
        job={"on_complete": "idle", "iterations": 20, "max_approaches": 1},
        stub_env={
            "STUB_TASKS": "3",
            # traffic FIRST (the rich preamble is flushed immediately), then a
            # 3s sleep inside the tool call: a real window to kill the engine
            # in that the 1s startup watchdog below cannot mistake for a hang.
            "STUB_RICH_EVENTS": "1", "STUB_TOOL_SLEEP": "3",
            "STUB_INFRA_HANG_SKIP": "1",   # invocation 1 (planning) is healthy
            "STUB_INFRA_HANG_COUNT": "1",  # invocation 2 (1st worker) hangs
            "RALPHD_INFRA_STARTUP_TIMEOUT": "1",
            "RALPHD_INFRA_RETRY_BACKOFF_S": "0.1,0.1,0.1",
        })
    e1.wait_api()

    # wait until the infra fault is recorded (that is the refund) and the
    # retry after it is under way, then kill this engine mid-iteration
    infra = _wait_for(lambda: [m for m in _metas(e1.run_dir)
                               if m.get("faultClass") == "infra"])
    assert len(infra) == 1 and infra[0]["phase"] == "worker"
    _wait_for(lambda: len(_metas(e1.run_dir)) > max(m["number"] for m in infra))
    os.kill(e1.proc.pid, signal.SIGKILL)  # this pid, never a pattern
    e1.proc.wait(timeout=10)

    e2 = engine_factory(  # what `ralphctl resume` does: same run dir
        job={"on_complete": "exit", "iterations": 20, "max_approaches": 1},
        stub_env={"STUB_TASKS": "3", "STUB_SLEEP": "0"})
    assert e2.run_dir == e1.run_dir
    assert e2.proc.wait(timeout=90) == 0

    status = json.loads((e2.run_dir / "status.json").read_text())
    assert status["state"] == "succeeded"
    raw = max(iteration_numbers(e2.run_dir))
    assert status["iterationsRefunded"] == {"infra": 1, "grace": 0}
    assert status["iterationsUsed"] == raw - 1, (
        "the refund earned before the crash was re-charged by the resume")


# -- the documented claims ------------------------------------------------
def _spec_section(heading: str) -> str:
    lines = (REPO / "SPEC.md").read_text().splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith(heading))
    end = len(lines)
    for i in range(start + 1, len(lines)):
        if lines[i].startswith("#") and not lines[i].startswith("####"):
            end = i
            break
    return "\n".join(lines[start:end])


def test_the_status_field_is_in_the_spec_table():
    rows = [line for line in _spec_section("### 5.2").splitlines()
            if line.startswith("| `iterationsRefunded`")]
    assert len(rows) == 1, rows
    assert "infra" in rows[0] and "grace" in rows[0]
    assert "resume" in rows[0]


@pytest.mark.parametrize("claim", ["iterationsRefunded", "_seed_refunds",
                                   "begin_iteration_dir", "#32"])
def test_the_retry_section_explains_the_persistence_and_the_decision(claim):
    assert claim in _spec_section("### 8.4"), claim


def test_the_retry_section_promises_the_refunds_survive_a_resume():
    """Not just *that* the field exists: 8.4 has to state the guarantee, since
    that is what an operator reading the spec is owed."""
    para = [p for p in _spec_section("### 8.4").split("\n\n")
            if "iterationsRefunded" in p]
    assert len(para) == 1, para
    assert "survive a resume" in para[0]
    assert "seeds the counters back" in para[0]


def test_the_budget_section_says_neither_counter_is_lost_at_a_resume():
    """4.5 is where `budget_left()` and the two refunds are spelled out; the
    charged formula there is wrong if the counters restart at zero."""
    para = [p for p in _spec_section("### 4.5").split("\n\n")
            if "_grace_refunded" in p or "grace\nreview" in p]
    survives = [p for p in para
                if "iterationsRefunded" in p and "resume" in p]
    assert survives, para


def test_the_spec_records_the_unpersisted_grace_grant_as_a_limitation():
    section = _spec_section("### 8.4")
    para = [p for p in section.split("\n\n") if "_grace_review_granted" in p]
    assert len(para) == 1, para
    assert "per-process" in para[0] and "resumed" in para[0]


def test_the_api_doc_gives_both_fields_their_semantics():
    doc = (REPO / "docs" / "api.md").read_text()
    rows = {name: [line for line in doc.splitlines()
                   if line.startswith(f"| `{name}` |")]
            for name in ("iterationsUsed", "iterationsRefunded")}
    assert all(len(r) == 1 for r in rows.values()), rows
    assert "charged" in rows["iterationsUsed"][0]
    refunded = rows["iterationsRefunded"][0]
    assert "#32" in refunded and "resume" in refunded
    assert "{infra, grace}" in refunded


def _blocks(text: str) -> list[str]:
    """Paragraphs, with list items split apart, so a claim has to live in the
    same block as the thing it is about rather than anywhere in the file."""
    out = []
    for para in text.split("\n\n"):
        out.extend(re.split(r"\n(?=[-*] )", para))
    return out


def test_every_architecture_block_explaining_a_refund_says_it_is_persisted():
    """docs/architecture.md explains the refund counters in three places (the
    infra-retry narrative, the grace review, the fault-model summary). Each has
    to say the counter is persisted, or one of them still describes the defect
    as the mechanism."""
    blocks = [b for b in _blocks((REPO / "docs" / "architecture.md").read_text())
              if "_infra_refunded" in b or "_grace_refunded" in b]
    assert len(blocks) == 3, [b[:60] for b in blocks]
    missing = [b[:60] for b in blocks if "iterationsRefunded" not in b]
    assert not missing, missing
    assert any("resume cannot re-charge" in b or "seeded back on resume" in b
               for b in blocks)


def test_the_prd_index_calls_the_v06_tally_inflated():
    """The permanent-record consequence: the note where the 145 is explained
    says the figure is inflated by this defect and names the issue -- and the
    row itself still reports 145."""
    text = (REPO / "docs" / "prds" / "README.md").read_text()
    row = [line for line in text.splitlines()
           if line.startswith("| `selfdev-v06-release` |")]
    assert len(row) == 1 and "| 145 |" in row[0], row
    note = [p for p in text.split("\n- ") if "The iteration cell is the 145" in p]
    assert len(note) == 1, note
    assert "inflated" in note[0] and "#32" in note[0]
    assert "128" in note[0], "the note says what the guarantee implies instead"
