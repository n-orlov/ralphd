"""Task 014 (#49 part 2): a bare in-band `aborted` with a clean exit is
`infra` even when traffic preceded it -- and SPEC's open question 1 is closed
with the evidence that settled it.

Task 117 (steering 005, run `selfdev-v09-fsm-loop`) removed the duration bound
task 014 first decided this with (`faults.ABORTED_STREAM_MAX_DURATION_S`,
120s, derived from a SAMPLE OF ONE: iteration 145 of `selfdev-v06-release`,
39s). That run's own 15 bare-`aborted`-after-traffic incidents were every one
a genuine provider-side abort; 14 of the 15 landed ABOVE 120s, and three in a
row (iterations 94/100/101) tripped the stagnation guard and failed an
approach for a plan that was not actually stuck. A provider can hang up on a
stream at any point, so the population of genuine abort durations has no
upper bound a clock could be calibrated against -- raising the bound would
only move where the next failure landed. The discriminator is decided on
SHAPE instead: a bare `aborted`, a clean `exit_code == 0`, traffic present, no
infra signature, not `interrupted`, no abort recorded for the run. `work`
this is, whatever the duration.

What must NOT move: a genuinely operator-initiated abort is never `infra`
(the carve-out), a signal-killed iteration stays `signal` (task 013), a bare
`aborted` with a NONZERO exit stays `work` (the clean-exit half of the new
conjunction), and a message that merely mentions "aborted" stays `work`.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ralphd.engine.config import JobConfig
from ralphd.engine.faults import (
    FAULT_CLASS_INFRA,
    FAULT_CLASS_SIGNAL,
    FAULT_CLASS_WORK,
    FAULT_REASON_ABORTED_STREAM,
    FAULT_REASON_ABORT_RECORDED,
    FAULT_REASON_INTERRUPTED,
    FAULT_REASON_NO_TRAFFIC_UNCLASSIFIED,
    FAULT_REASON_SIGNATURE,
    FAULT_REASON_WORK,
    classify_fault,
    explain_fault,
    is_bare_aborted,
)
from ralphd.engine.loop import LoopSupervisor
from ralphd.engine.runner import IterationResult
from ralphd.engine.state import RunDir, fault_explanation

REPO = Path(__file__).resolve().parents[1]
EARLY = 39.0         # the observed instant: iteration 145 of selfdev-v06-release
LATE = 1800.0        # half an hour in: an iteration that, before task 117, was
                     # (wrongly) read as having really done work
REALLY_LATE = 2542.0  # iteration 59 of selfdev-v09-fsm-loop's real number --
                      # 42 minutes in, the same fault as one at 39s

# ------------------------------------------------------------------ the table
# The four corners of (traffic / no traffic) x (abort recorded / not) for the
# bare `aborted`, plus duration having NO effect any more, plus the shapes
# that must not move.
# id -> (kwargs, expected class, expected reason)
CORNERS: dict[str, tuple[dict, str | None, str | None]] = {
    # ---- the case #49 part 2 / task 117 moves --------------------------
    "traffic-no-abort-early": (
        {"error_text": "aborted", "exit_code": 0, "produced_traffic": True},
        FAULT_CLASS_INFRA, FAULT_REASON_ABORTED_STREAM),
    # ---- the other three corners -------------------------------------
    "traffic-abort-recorded-early": (
        {"error_text": "aborted", "exit_code": 0, "produced_traffic": True,
         "operator_abort": True},
        FAULT_CLASS_WORK, FAULT_REASON_ABORT_RECORDED),
    "no-traffic-no-abort-early": (
        {"error_text": "aborted", "exit_code": 0, "produced_traffic": False},
        FAULT_CLASS_INFRA, FAULT_REASON_NO_TRAFFIC_UNCLASSIFIED),
    "no-traffic-abort-recorded-early": (
        {"error_text": "aborted", "exit_code": 0, "produced_traffic": False,
         "operator_abort": True},
        FAULT_CLASS_WORK, FAULT_REASON_ABORT_RECORDED),
    # ---- duration has NO effect any more (task 117) -------------------
    "traffic-no-abort-late": (
        {"error_text": "aborted", "exit_code": 0, "produced_traffic": True},
        FAULT_CLASS_INFRA, FAULT_REASON_ABORTED_STREAM),
    "traffic-no-abort-really-late": (
        # iteration 59's real number -- 2542s, 42 minutes in. Same fault as
        # one at 39s: the old 120s bound would have called this `work`.
        {"error_text": "aborted", "exit_code": 0, "produced_traffic": True},
        FAULT_CLASS_INFRA, FAULT_REASON_ABORTED_STREAM),
    # ---- the clean-exit half of the conjunction (new in task 117) -----
    "nonzero-exit-bare-aborted": (
        {"error_text": "aborted", "exit_code": 1, "produced_traffic": True},
        FAULT_CLASS_WORK, FAULT_REASON_WORK),
    # ---- shapes that must NOT move ------------------------------------
    "signal-beats-the-new-branch": (
        {"error_text": "aborted", "exit_code": 130, "interrupted": True,
         "produced_traffic": True},
        FAULT_CLASS_SIGNAL, FAULT_REASON_INTERRUPTED),
    "premature-close-is-a-signature": (
        {"error_text": "Error: aborted: Premature close", "exit_code": 0,
         "produced_traffic": True},
        FAULT_CLASS_INFRA, FAULT_REASON_SIGNATURE),
    "not-a-bare-aborted": (
        {"error_text": "the task was aborted by the test harness",
         "exit_code": 1, "produced_traffic": True},
        FAULT_CLASS_WORK, FAULT_REASON_WORK),
    "early-work-failure-is-still-work": (
        {"error_text": "pytest failed: 3 tests", "exit_code": 1,
         "produced_traffic": True},
        FAULT_CLASS_WORK, FAULT_REASON_WORK),
    "clean-exit-early": (
        {"exit_code": 0, "produced_traffic": True},
        None, None),
}


@pytest.mark.parametrize("case", sorted(CORNERS))
def test_every_corner_of_the_bare_aborted_table(case):
    kwargs, expected, reason = CORNERS[case]
    assert classify_fault(**kwargs) == expected
    exp = explain_fault(**kwargs)
    assert exp["faultClass"] == expected, exp
    assert exp["reason"] == reason if reason is not None else True


@pytest.mark.parametrize("case", sorted(CORNERS))
def test_the_reason_matches_the_verdict_in_every_corner(case):
    kwargs, expected, reason = CORNERS[case]
    if reason is None:
        return
    assert explain_fault(**kwargs)["reason"] == reason


def test_a_bare_aborted_after_traffic_inside_the_threshold_is_not_work():
    """The defect, stated on its own so a revert cannot hide inside a table:
    iteration 145's exact shape (name kept for the ledger -- the "threshold"
    it once named is gone; the assertion it makes is not)."""
    verdict = classify_fault(error_text="aborted", exit_code=0,
                             produced_traffic=True)
    assert verdict != FAULT_CLASS_WORK, \
        "a provider-side stream abort must not cost an approach"
    assert verdict == FAULT_CLASS_INFRA


def test_a_bare_aborted_well_past_the_old_120s_bound_is_still_infra():
    """The regression task 117 fixes, stated on its own: the exact shape that
    failed approach 2 of selfdev-v09-fsm-loop (94/100/101, all >120s) must
    resolve `infra`, not `work`, regardless of how long the iteration ran."""
    for duration in (121.0, LATE, REALLY_LATE):
        r = IterationResult(exit_code=0, interrupted=False)
        r.final_text = "partial answer"
        r.error_message = "aborted"
        r.duration_s = duration
        assert classify_fault(
            error_text=r.error_message, exit_code=r.exit_code,
            interrupted=r.interrupted, produced_traffic=True,
        ) == FAULT_CLASS_INFRA, duration


def test_an_operator_initiated_abort_is_still_never_infra():
    """The carve-out is intact: whatever the traffic, an abort on the record
    is not retried as an outage."""
    for produced_traffic in (True, False):
        for recorded in (True, False):
            verdict = classify_fault(
                error_text="aborted", exit_code=0,
                produced_traffic=produced_traffic,
                operator_abort=True, operator_abort_recorded=recorded)
            assert verdict != FAULT_CLASS_INFRA, (produced_traffic, recorded)
            assert verdict == FAULT_CLASS_WORK


def test_bare_aborted_recognizes_only_the_bare_word():
    assert is_bare_aborted("aborted")
    assert is_bare_aborted("  aborted\n")
    assert is_bare_aborted("Aborted.")
    assert not is_bare_aborted("")
    assert not is_bare_aborted(None)
    assert not is_bare_aborted("aborted: Premature close")
    assert not is_bare_aborted("run aborted after 3 retries")


def test_classify_fault_takes_no_duration_parameter_any_more():
    """The regression this locks: `classify_fault`/`explain_fault` must not
    accept `duration_s` at all -- a caller passing it should fail loudly (a
    TypeError), not silently ignore it, so a reintroduced duration gate can't
    hide behind an accepted-but-unused kwarg."""
    with pytest.raises(TypeError):
        classify_fault(error_text="aborted", exit_code=0,
                       produced_traffic=True, duration_s=39.0)  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        explain_fault(error_text="aborted", exit_code=0,
                      produced_traffic=True, duration_s=39.0)  # type: ignore[call-arg]


def test_classify_fault_still_delegates_to_the_one_ladder():
    for kwargs, _, _ in CORNERS.values():
        assert classify_fault(**kwargs) == explain_fault(**kwargs)["faultClass"]


# --------------------------------------------------- the engine's own verdict
def _supervisor(tmp_path: Path) -> LoopSupervisor:
    cfg = JobConfig(run_id="unit", infra_retry_max=3,
                    infra_retry_backoff_s=[0.01])
    return LoopSupervisor(cfg, RunDir(root=tmp_path), tmp_path)


def _aborted_after_traffic(duration_s: float) -> IterationResult:
    r = IterationResult(exit_code=0, interrupted=False)
    r.final_text = "partial answer"   # traffic: it reached the model
    r.error_message = "aborted"
    r.duration_s = duration_s
    return r


def test_the_loop_records_infra_for_an_early_and_a_late_aborted_after_traffic(tmp_path):
    """Task 117: duration no longer decides this at the engine layer either --
    `_classify_result` never reads `result.duration_s` for this shape."""
    sup = _supervisor(tmp_path)
    assert sup._classify_result(_aborted_after_traffic(EARLY)) \
        == FAULT_CLASS_INFRA
    assert sup._classify_result(_aborted_after_traffic(LATE)) \
        == FAULT_CLASS_INFRA
    assert sup._classify_result(_aborted_after_traffic(REALLY_LATE)) \
        == FAULT_CLASS_INFRA


@pytest.mark.asyncio
async def test_the_wrapper_retries_and_refunds_the_early_aborted(tmp_path):
    """Being `infra` is not cosmetic: the wrapper retries it in place and
    refunds the iteration, which is what iteration 145 was denied."""
    sup = _supervisor(tmp_path)
    calls: list[str] = []

    async def fake_once(phase, extra="", prompt_name=None, tier=None):
        calls.append(phase)
        return _aborted_after_traffic(EARLY)

    sup._run_iteration_once = fake_once  # type: ignore[method-assign]
    await sup.run_iteration("worker")
    assert len(calls) == 3, calls
    log = sup.run.root / "events.jsonl"
    lines = log.read_text().splitlines() if log.exists() else []
    retries = [ev for ev in (json.loads(x) for x in lines)
               if ev.get("type") == "infra_retry"]
    assert [ev["attempt"] for ev in retries] == [1, 2, 3]
    assert sup._infra_refunded > 0


@pytest.mark.asyncio
async def test_a_late_aborted_after_traffic_is_now_also_retried(tmp_path):
    """The behaviour task 117 changes, at the wrapper: this used to assert
    `calls == ["worker"]` (not retried, because 1800s > the old 120s bound).
    That was the defect -- a genuine stream abort at half an hour in is the
    same fault as one at 39s, so it is retried and refunded exactly like the
    early case now."""
    sup = _supervisor(tmp_path)
    calls: list[str] = []

    async def fake_once(phase, extra="", prompt_name=None, tier=None):
        calls.append(phase)
        return _aborted_after_traffic(LATE)

    sup._run_iteration_once = fake_once  # type: ignore[method-assign]
    await sup.run_iteration("worker")
    assert len(calls) == 3, calls
    assert sup._infra_refunded > 0


# ------------------------------------------- the on-disk re-derivation agrees
def _run_with_iteration(tmp_path: Path, **over) -> Path:
    run_dir = tmp_path / "registry" / "runs" / "faulty"
    (run_dir / "iterations" / "0145").mkdir(parents=True)
    (run_dir / "status.json").write_text(json.dumps(
        {"runId": "faulty", "state": "failed", "iterationsUsed": 145}))
    meta = {"number": 145, "phase": "worker", "approach": 1,
            "startedAt": "2026-09-04T10:00:00Z",
            "endedAt": "2026-09-04T10:00:39Z",  # 39s, the observed instant
            "exitCode": 0, "interrupted": False, "timedOut": False,
            "noTrafficTimeout": False, "sawComplete": True,
            "error": "aborted", "faultClass": FAULT_CLASS_INFRA,
            "usage": {"totalTokens": 4200}}
    meta.update(over)
    (run_dir / "iterations" / "0145" / "meta.json").write_text(json.dumps(meta))
    return run_dir


def test_the_explanation_re_derives_the_same_verdict_from_the_record(tmp_path):
    """`state.fault_explanation` re-derives the classifier's reasoning from
    meta.json alone -- it has to reach the recorded verdict, or every run of
    this shape carries a spurious divergence notice. Also proves the
    re-derivation no longer reads `durationS` at all: a record with a huge
    `durationS` (task 117's own regression) still re-derives `infra`."""
    exp = fault_explanation(_run_with_iteration(tmp_path))
    assert exp["faultClass"] == FAULT_CLASS_INFRA
    assert exp["reason"] == FAULT_REASON_ABORTED_STREAM
    assert exp["notices"] == [], exp["notices"]

    exp_late = fault_explanation(_run_with_iteration(
        tmp_path / "late", durationS=REALLY_LATE,
        endedAt="2026-09-04T10:42:22Z"))
    assert exp_late["faultClass"] == FAULT_CLASS_INFRA
    assert exp_late["reason"] == FAULT_REASON_ABORTED_STREAM
    assert exp_late["notices"] == [], exp_late["notices"]


def test_a_pre_fix_record_of_this_shape_is_flagged_as_diverging(tmp_path):
    """The other direction: an iteration recorded `work` before this task (145
    itself) now re-derives as `infra`, and the surface says so rather than
    quietly preferring either verdict."""
    from ralphd.engine.state import FAULT_VERDICT_DIVERGED_NOTICE
    exp = fault_explanation(_run_with_iteration(
        tmp_path, faultClass=FAULT_CLASS_WORK))
    assert exp["faultClass"] == FAULT_CLASS_WORK, "the record is reported"
    assert exp["reason"] == FAULT_REASON_ABORTED_STREAM
    assert exp["notices"] == [FAULT_VERDICT_DIVERGED_NOTICE]


# --------------------------------------------------------------- the documents
@pytest.mark.parametrize("path, needles", [
    ("SPEC.md", ["task 014, #49 part 2",
                 "return `\"work\"` \u2014 **unless** the",
                 "What is left, deliberately",
                 "## 17. Answered questions",
                 "**39 seconds**",
                 "Task 117 retired `ABORTED_STREAM_MAX_DURATION_S`"]),
    ("docs/api.md", ["a bare `aborted` with a clean exit and no abort "
                     "recorded for the run, at any duration"]),
    ("docs/architecture.md", ["is the provider hanging up, at any\n  duration",
                              "the duration bound retired by task 117"]),
    ("docs/cli.md", ["the provider hung up mid-stream"]),
])
def test_the_docs_state_the_new_rule(path, needles):
    text = (REPO / path).read_text()
    for needle in needles:
        assert needle in text, f"{path} is missing: {needle}"


@pytest.mark.parametrize("path", ["SPEC.md", "docs/architecture.md",
                                  "docs/api.md", "docs/cli.md"])
def test_the_retired_constant_is_gone_from_the_docs_rule_itself(path):
    """The constant may still be named as HISTORY (task 014 introduced it,
    task 117 retired it) but none of these surfaces may still state the rule
    itself in terms of it (a live "within Ns" / "inside two minutes" claim)."""
    text = (REPO / path).read_text()
    assert "within two minutes" not in text
    assert "inside two minutes" not in text


def test_section_8_2_no_longer_carries_the_duration_residual():
    """The ladder's own section must not read as if a bare `aborted` past a
    duration bound still misclassifies -- that residual is gone along with
    the bound that created it."""
    text = (REPO / "SPEC.md").read_text()
    section = text.split("### 8.2 Infra versus work")[1].split("### 8.3")[0]
    assert "**What is left, deliberately.**" in section
    assert "at any\n   duration" in section
    assert ("ABORTED_STREAM_MAX_DURATION_S" in section
            and "retired" in section), \
        "the section must still name the retired constant as history"
    assert "still classifies\nas \"work\" (step 6)" not in section, \
        "the old duration residual must not still be claimed"


def test_the_open_question_moved_out_of_section_16_rather_than_being_edited():
    """R's rule: a resolved open question is MOVED, with its answer and
    evidence, not rewritten in place inside §16."""
    text = (REPO / "SPEC.md").read_text()
    questions = text.split("## 16. Open questions")[1]
    questions, answered = questions.split("## 17. Answered questions")
    assert "told from an operator's" not in questions, \
        "the answered question is still sitting in §16"
    assert "Can a provider-side stream abort mid-iteration be told from an " \
        "operator's\n   abort?" in answered
    assert "§16 question 1 through v0.6" in answered
    assert "answered by task 014, #49 part 2" in answered
    assert "task 117" in answered, \
        "the answer's own correction is not recorded next to it"
    # the answer's own evidence, not just a verdict
    for needle in ("selfdev-v06-release", "iteration 145", "**39 seconds**",
                   "45-minute", "120s", "selfdev-v09-fsm-loop"):
        assert needle in answered, f"the answer omits its evidence: {needle}"
    # and §16 still holds the questions that are genuinely open
    assert "Does the outage budget belong per-episode or per-job?" in questions
    # ("Should the reflection diff ever be applied automatically?" stood here
    # until task 029 moved it out with its own answer -- §17 entry 2, checked by
    # tests/test_cli_suggestions_guards.py. What this assertion is for is that
    # §16 still carries a question nothing has answered yet.)
    assert ("How much autonomy should the agent have over its own container?"
            in questions)
