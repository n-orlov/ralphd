"""Task 013 (#49 part 1): a signal-killed iteration is not `faultClass: "work"`.

`work` is the class that burns approach and task-failure bookkeeping, so
folding "a signal ended it" into it charged the agent for a failure it never
committed -- and, per requirement D, *fed* requirement C's attempt-burning
path. The ladder now has a third verdict, `faults.FAULT_CLASS_SIGNAL`.

The table below is the whole contract, over every corner of
(traffic / no traffic) x (signal / no signal) x (abort recorded / not), plus
the shapes that must NOT move: the operator/abort carve-out (still `work`,
deliberately coarse -- that branch exists to say "never retry this"), the
startup watchdog, the signature table, and the no-traffic default.
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
    FAULT_CLASSES,
    FAULT_REASON_INTERRUPTED,
    FAULT_REASON_NO_TRAFFIC_TIMEOUT,
    FAULT_REASON_NO_TRAFFIC_UNCLASSIFIED,
    FAULT_REASON_SIGNATURE,
    FAULT_REASON_WORK,
    classify_fault,
    explain_fault,
)
from ralphd.engine.loop import LoopSupervisor
from ralphd.engine.runner import IterationResult
from ralphd.engine.state import RunDir, format_exit_reason

REPO = Path(__file__).resolve().parents[1]
DNS_ERROR = "getaddrinfo EAI_AGAIN gateway.internal"

# id -> (kwargs, expected class, expected reason)
LADDER: dict[str, tuple[dict, str | None, str | None]] = {
    # ---- the case #49 part 1 moves ------------------------------------
    "signal-with-traffic": (
        {"error_text": "", "exit_code": -15, "interrupted": True,
         "produced_traffic": True},
        FAULT_CLASS_SIGNAL, FAULT_REASON_INTERRUPTED),
    "signal-with-traffic-sigint-130": (
        {"error_text": "aborted", "exit_code": 130, "interrupted": True,
         "produced_traffic": True},
        FAULT_CLASS_SIGNAL, FAULT_REASON_INTERRUPTED),
    "signal-with-traffic-and-timeout": (
        {"error_text": "", "exit_code": -9, "interrupted": True,
         "timed_out": True, "produced_traffic": True},
        FAULT_CLASS_SIGNAL, FAULT_REASON_INTERRUPTED),
    # ---- shapes that must NOT move ------------------------------------
    "traffic-then-own-failure": (
        {"error_text": "pytest failed: 3 tests", "exit_code": 1,
         "produced_traffic": True},
        FAULT_CLASS_WORK, FAULT_REASON_WORK),
    "traffic-then-timeout-no-signal": (
        {"error_text": "", "timed_out": True, "produced_traffic": True},
        FAULT_CLASS_WORK, FAULT_REASON_WORK),
    "signal-without-traffic": (
        {"error_text": "aborted", "exit_code": None, "interrupted": True,
         "produced_traffic": False},
        FAULT_CLASS_INFRA, FAULT_REASON_NO_TRAFFIC_UNCLASSIFIED),
    "no-traffic-plain-failure": (
        {"error_text": "", "exit_code": 1, "produced_traffic": False},
        FAULT_CLASS_INFRA, FAULT_REASON_NO_TRAFFIC_UNCLASSIFIED),
    "signal-with-traffic-but-infra-text": (
        {"error_text": DNS_ERROR, "exit_code": -15, "interrupted": True,
         "produced_traffic": True},
        FAULT_CLASS_INFRA, FAULT_REASON_SIGNATURE),
    "startup-watchdog-kill": (
        {"error_text": "", "exit_code": None, "interrupted": True,
         "no_traffic_timeout": True, "produced_traffic": False},
        FAULT_CLASS_INFRA, FAULT_REASON_NO_TRAFFIC_TIMEOUT),
    # the carve-out: an abort is recorded for the run, so nothing here is
    # retried and the class stays `work` whatever ended the process
    "abort-recorded-signal-with-traffic": (
        {"error_text": "", "exit_code": -15, "interrupted": True,
         "produced_traffic": True, "operator_abort": True},
        FAULT_CLASS_WORK, None),
    "abort-recorded-signal-without-traffic": (
        {"error_text": "aborted", "exit_code": None, "interrupted": True,
         "produced_traffic": False, "operator_abort": True},
        FAULT_CLASS_WORK, None),
    "operator-abort-established": (
        {"error_text": "", "exit_code": -15, "interrupted": True,
         "produced_traffic": True, "operator_abort": True,
         "operator_abort_recorded": True},
        FAULT_CLASS_WORK, None),
    # not a failure at all
    "clean-exit": ({"exit_code": 0, "produced_traffic": True}, None, None),
}


@pytest.mark.parametrize("case", sorted(LADDER))
def test_the_ladder_classifies_every_corner(case):
    kwargs, expected, reason = LADDER[case]
    assert classify_fault(**kwargs) == expected
    exp = explain_fault(**kwargs)
    assert exp["faultClass"] == expected, exp
    if reason is not None:
        assert exp["reason"] == reason, exp


def test_a_signal_killed_iteration_is_not_work():
    """The defect, stated on its own so a revert cannot hide inside a table."""
    verdict = classify_fault(error_text="", exit_code=-15, interrupted=True,
                             produced_traffic=True)
    assert verdict != FAULT_CLASS_WORK
    assert verdict == FAULT_CLASS_SIGNAL


def test_the_reason_is_recorded_beside_the_new_class():
    """#49's own wording: the class must come with the reason it was chosen
    for, not just a bare label."""
    exp = explain_fault(error_text="", exit_code=-15, interrupted=True,
                        produced_traffic=True)
    assert exp["faultClass"] == FAULT_CLASS_SIGNAL
    assert exp["reason"] == FAULT_REASON_INTERRUPTED
    assert "signal ended this iteration" in exp["reason"]
    assert exp["reason"] != FAULT_REASON_WORK


def test_the_class_vocabulary_is_exactly_three_plus_none():
    assert FAULT_CLASSES == (FAULT_CLASS_INFRA, FAULT_CLASS_WORK,
                             FAULT_CLASS_SIGNAL)
    seen = {classify_fault(**kwargs) for kwargs, _, _ in LADDER.values()}
    assert seen - {None} <= set(FAULT_CLASSES), seen
    assert FAULT_CLASS_SIGNAL in seen, "the table must exercise the new class"


def test_the_exit_reason_line_names_the_new_class():
    """`format_exit_reason` appends the verdict to the raw signal it came
    from, so an operator reading a run document sees it too."""
    line = format_exit_reason({"endedAt": "2025-01-01T00:00:00Z",
                               "interrupted": True,
                               "faultClass": FAULT_CLASS_SIGNAL})
    assert line.endswith(f"[{FAULT_CLASS_SIGNAL} fault]"), line


# ----------------------------------------------------------- the loop's own
# verdict, and what the retry wrapper does with it.
def _supervisor(tmp_path: Path) -> LoopSupervisor:
    cfg = JobConfig(run_id="unit", infra_retry_max=3,
                    infra_retry_backoff_s=[0.01])
    return LoopSupervisor(cfg, RunDir(root=tmp_path), tmp_path)


def _signal_result() -> IterationResult:
    r = IterationResult(exit_code=-15, interrupted=True)
    r.final_text = "partial work"   # traffic: it reached the model
    r.duration_s = 120.0            # not an "instant" failure
    return r


def test_the_loop_records_signal_for_a_signal_killed_iteration(tmp_path):
    sup = _supervisor(tmp_path)
    assert sup._classify_result(_signal_result()) == FAULT_CLASS_SIGNAL


@pytest.mark.asyncio
async def test_a_signal_killed_iteration_is_not_retried_as_an_outage(tmp_path):
    """`signal` is not `infra`, so the retry wrapper hands it straight back --
    relaunching into whatever sent the signal is how a retry loop fights its
    operator (or its OOM killer)."""
    sup = _supervisor(tmp_path)
    calls: list[str] = []

    async def fake_once(phase, extra="", prompt_name=None):
        calls.append(phase)
        return _signal_result()

    sup._run_iteration_once = fake_once  # type: ignore[method-assign]
    result = await sup.run_iteration("worker")
    assert calls == ["worker"], calls
    assert result.interrupted is True
    assert sup._infra_refunded == 0
    log = sup.run.root / "events.jsonl"
    lines = log.read_text().splitlines() if log.exists() else []
    assert [ev for ev in (json.loads(x) for x in lines)
            if ev.get("type") == "infra_retry"] == []


# ------------------------------------------------- the record and the docs
def test_the_stale_issue_reference_is_gone_from_the_classifier():
    """The PRD's fact 8: `faults.py` cited #23 as the owner of this taxonomy,
    and #23 is closed; #49 owns it now. The old number may still appear as
    history, but only where the text says it is closed."""
    src = (REPO / "src" / "ralphd" / "engine" / "faults.py").read_text()
    assert "issue #23" not in src, "a closed issue is cited as the owner"
    for line in src.splitlines():
        if "#23" in line:
            assert "closed" in line, f"#23 named without saying so: {line}"
    assert "issue #49" in src


@pytest.mark.parametrize("path, needles", [
    ("docs/api.md", ['| `"signal"` |', "not** retried"]),
    ("SPEC.md", ['return\n   `"signal"`', "task 013, #49",
                 "falling through to step 7"]),
    ("docs/architecture.md", ["**A signal is its own class**",
                              '`faultClass: "signal"`']),
    ("docs/cli.md", ["is its own `signal` class"]),
])
def test_the_docs_state_the_new_class(path, needles):
    text = (REPO / path).read_text()
    for needle in needles:
        assert needle in text, f"{path} is missing: {needle}"
