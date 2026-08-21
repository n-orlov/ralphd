"""Task 014 (#49 part 2): a bare in-band `aborted` that ended well inside its
own timeout, with no operator abort recorded, is `infra` even when traffic
preceded it -- and SPEC's open question 1 is closed with the evidence that
settled it.

The shape: `selfdev-v06-release`'s iteration 145 recorded `error: aborted` with
`faultClass: "work"` 39 seconds into a 45-minute `iteration_timeout_s`, as the
leading edge of a DNS outage whose next five iterations matched an infra
signature and were retried and refunded. Same outage, opposite treatment,
decided only by whether a token had been emitted before the stream died.

What must NOT move: a genuinely operator-initiated abort is never `infra` (the
carve-out), a signal-killed iteration stays `signal` (task 013), and a bare
`aborted` past the threshold stays `work`.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ralphd.engine.config import JobConfig
from ralphd.engine.faults import (
    ABORTED_STREAM_MAX_DURATION_S,
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
EARLY = 39.0        # the observed instant: iteration 145 of selfdev-v06-release
LATE = 1800.0       # half an hour in: an iteration that really did work

# ------------------------------------------------------------------ the table
# The four corners of (traffic / no traffic) x (abort recorded / not) for the
# bare `aborted`, at the observed 39s -- plus the boundary either side of the
# threshold, the unknown-duration case, and the shapes that must not move.
# id -> (kwargs, expected class, expected reason)
CORNERS: dict[str, tuple[dict, str | None, str | None]] = {
    # ---- the case #49 part 2 moves ------------------------------------
    "traffic-no-abort-early": (
        {"error_text": "aborted", "exit_code": 0, "produced_traffic": True,
         "duration_s": EARLY},
        FAULT_CLASS_INFRA, FAULT_REASON_ABORTED_STREAM),
    # ---- the other three corners -------------------------------------
    "traffic-abort-recorded-early": (
        {"error_text": "aborted", "exit_code": 0, "produced_traffic": True,
         "duration_s": EARLY, "operator_abort": True},
        FAULT_CLASS_WORK, FAULT_REASON_ABORT_RECORDED),
    "no-traffic-no-abort-early": (
        {"error_text": "aborted", "exit_code": 0, "produced_traffic": False,
         "duration_s": EARLY},
        FAULT_CLASS_INFRA, FAULT_REASON_NO_TRAFFIC_UNCLASSIFIED),
    "no-traffic-abort-recorded-early": (
        {"error_text": "aborted", "exit_code": 0, "produced_traffic": False,
         "duration_s": EARLY, "operator_abort": True},
        FAULT_CLASS_WORK, FAULT_REASON_ABORT_RECORDED),
    # ---- the duration boundary, either side ---------------------------
    "boundary-just-inside": (
        {"error_text": "aborted", "exit_code": 0, "produced_traffic": True,
         "duration_s": ABORTED_STREAM_MAX_DURATION_S - 0.001},
        FAULT_CLASS_INFRA, FAULT_REASON_ABORTED_STREAM),
    "boundary-exactly-at": (
        {"error_text": "aborted", "exit_code": 0, "produced_traffic": True,
         "duration_s": ABORTED_STREAM_MAX_DURATION_S},
        FAULT_CLASS_INFRA, FAULT_REASON_ABORTED_STREAM),
    "boundary-just-outside": (
        {"error_text": "aborted", "exit_code": 0, "produced_traffic": True,
         "duration_s": ABORTED_STREAM_MAX_DURATION_S + 0.001},
        FAULT_CLASS_WORK, FAULT_REASON_WORK),
    "traffic-no-abort-late": (
        {"error_text": "aborted", "exit_code": 0, "produced_traffic": True,
         "duration_s": LATE},
        FAULT_CLASS_WORK, FAULT_REASON_WORK),
    # duration unknown: nothing is known, so nothing is reclassified
    "traffic-no-abort-duration-unknown": (
        {"error_text": "aborted", "exit_code": 0, "produced_traffic": True,
         "duration_s": None},
        FAULT_CLASS_WORK, FAULT_REASON_WORK),
    # ---- shapes that must NOT move ------------------------------------
    "signal-beats-the-new-branch": (
        {"error_text": "aborted", "exit_code": 130, "interrupted": True,
         "produced_traffic": True, "duration_s": EARLY},
        FAULT_CLASS_SIGNAL, FAULT_REASON_INTERRUPTED),
    "premature-close-is-a-signature": (
        {"error_text": "Error: aborted: Premature close", "exit_code": 0,
         "produced_traffic": True, "duration_s": EARLY},
        FAULT_CLASS_INFRA, FAULT_REASON_SIGNATURE),
    "not-a-bare-aborted": (
        {"error_text": "the task was aborted by the test harness",
         "exit_code": 1, "produced_traffic": True, "duration_s": EARLY},
        FAULT_CLASS_WORK, FAULT_REASON_WORK),
    "early-work-failure-is-still-work": (
        {"error_text": "pytest failed: 3 tests", "exit_code": 1,
         "produced_traffic": True, "duration_s": EARLY},
        FAULT_CLASS_WORK, FAULT_REASON_WORK),
    "clean-exit-early": (
        {"exit_code": 0, "produced_traffic": True, "duration_s": EARLY},
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
    iteration 145's exact shape."""
    verdict = classify_fault(error_text="aborted", exit_code=0,
                             produced_traffic=True, duration_s=EARLY)
    assert verdict != FAULT_CLASS_WORK, \
        "a provider-side stream abort must not cost an approach"
    assert verdict == FAULT_CLASS_INFRA


def test_an_operator_initiated_abort_is_still_never_infra():
    """The carve-out is intact: whatever the duration or the traffic, an abort
    on the record is not retried as an outage."""
    for produced_traffic in (True, False):
        for duration in (0.5, EARLY, LATE, None):
            for recorded in (True, False):
                verdict = classify_fault(
                    error_text="aborted", exit_code=0,
                    produced_traffic=produced_traffic, duration_s=duration,
                    operator_abort=True, operator_abort_recorded=recorded)
                assert verdict != FAULT_CLASS_INFRA, (produced_traffic,
                                                      duration, recorded)
                assert verdict == FAULT_CLASS_WORK


def test_bare_aborted_recognizes_only_the_bare_word():
    assert is_bare_aborted("aborted")
    assert is_bare_aborted("  aborted\n")
    assert is_bare_aborted("Aborted.")
    assert not is_bare_aborted("")
    assert not is_bare_aborted(None)
    assert not is_bare_aborted("aborted: Premature close")
    assert not is_bare_aborted("run aborted after 3 retries")


def test_the_threshold_is_a_named_constant_justified_from_the_evidence():
    """SPEC called this "a threshold nobody can derive from first principles";
    the constant therefore has to carry the run that derived it."""
    assert ABORTED_STREAM_MAX_DURATION_S == 120.0
    src = (REPO / "src" / "ralphd" / "engine" / "faults.py").read_text()
    block = src.split("ABORTED_STREAM_MAX_DURATION_S = ")[0]
    comment = block[block.index("# Task 014"):]
    for needle in ("selfdev-v06-release", "145", "39 seconds",
                   "45 minutes", "2700s"):
        assert needle in comment, f"the justification omits {needle!r}"
    assert "iteration_timeout_s" in comment, \
        "the comment must say why the threshold is absolute, not a fraction"


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


def test_the_loop_records_infra_for_an_early_aborted_after_traffic(tmp_path):
    sup = _supervisor(tmp_path)
    assert sup._classify_result(_aborted_after_traffic(EARLY)) \
        == FAULT_CLASS_INFRA
    assert sup._classify_result(_aborted_after_traffic(LATE)) \
        == FAULT_CLASS_WORK


@pytest.mark.asyncio
async def test_the_wrapper_retries_and_refunds_the_early_aborted(tmp_path):
    """Being `infra` is not cosmetic: the wrapper retries it in place and
    refunds the iteration, which is what iteration 145 was denied."""
    sup = _supervisor(tmp_path)
    calls: list[str] = []

    async def fake_once(phase, extra="", prompt_name=None):
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
async def test_a_late_aborted_after_traffic_is_not_retried(tmp_path):
    sup = _supervisor(tmp_path)
    calls: list[str] = []

    async def fake_once(phase, extra="", prompt_name=None):
        calls.append(phase)
        return _aborted_after_traffic(LATE)

    sup._run_iteration_once = fake_once  # type: ignore[method-assign]
    await sup.run_iteration("worker")
    assert calls == ["worker"], calls
    assert sup._infra_refunded == 0


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
    this shape carries a spurious divergence notice."""
    exp = fault_explanation(_run_with_iteration(tmp_path))
    assert exp["faultClass"] == FAULT_CLASS_INFRA
    assert exp["reason"] == FAULT_REASON_ABORTED_STREAM
    assert exp["notices"] == [], exp["notices"]


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
    ("SPEC.md", ["`faults.ABORTED_STREAM_MAX_DURATION_S` (120s)",
                 "task 014, #49 part 2",
                 "return `\"work\"` \u2014 **unless** the",
                 "What is left, deliberately",
                 "## 17. Answered questions",
                 "**39 seconds**"]),
    ("docs/api.md", ["a bare `aborted` that arrived within two minutes"]),
    ("docs/architecture.md", ["**A bare `aborted` inside two minutes is the "
                              "provider hanging up**",
                              "`faults.ABORTED_STREAM_MAX_DURATION_S` (120s)"]),
    ("docs/cli.md", ["the provider hung up mid-stream"]),
])
def test_the_docs_state_the_new_rule(path, needles):
    text = (REPO / path).read_text()
    for needle in needles:
        assert needle in text, f"{path} is missing: {needle}"


def test_section_8_2_keeps_the_residual_limitation_it_did_not_fix(tmp_path):
    """The ladder's own section has to say what is still misclassified -- a bare
    `aborted` past the threshold -- rather than reading as if #49 closed it."""
    text = (REPO / "SPEC.md").read_text()
    section = text.split("### 8.2 Infra versus work")[1].split("### 8.3")[0]
    assert "**What is left, deliberately.**" in section
    assert "still classifies\nas `\"work\"` (step 6)" in section
    assert "ABORTED_STREAM_MAX_DURATION_S" in section


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
    # the answer's own evidence, not just a verdict
    for needle in ("selfdev-v06-release", "iteration 145", "**39 seconds**",
                   "45-minute", "120s"):
        assert needle in answered, f"the answer omits its evidence: {needle}"
    # and §16 still holds the questions that are genuinely open
    assert "Does the outage budget belong per-episode or per-job?" in questions
    assert "Should the reflection diff ever be applied automatically?" in questions
