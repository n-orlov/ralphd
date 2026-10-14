"""v0.36 requirement H.1: a run directory written by 0.35 loads on 0.36.

Each case loads a committed fixture run directory (`tests/fixtures/
compat_035/<case>/`, a status.json exactly as 0.35 left it) through the one
load-time migration, `RunDir.migrate_legacy_stop_state` (what `ralphd-engine`
runs at startup, before its own first status write), and asserts the 0.36
reading:

1. `timed_out` reads as `exhausted`, `exhaustedBudget` `iterations` or `time`
   taken from the recorded reason;
2. a `paused` run on a reason PRD A.1 retired reads as `awaiting_input` with
   `inputQuestion.trigger == "cure_rounds_spent"` and `expiresAt` = load time
   plus the input timeout (never when the timeout is 0);
3. run-wide `curePasses` / `gateCurePasses` totals are kept for display and
   the current approach's count comes from its own entry.

The mapping is compatibility only: the engine writes none of the 0.35 shapes.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from ralphd.engine import state
from ralphd.engine.state import RunDir, utc_from_epoch
from tests.conftest import make_supervisor

FIXTURES = Path(__file__).parent / "fixtures" / "compat_035"
NOW = 1_790_000_000.0
TIMEOUT_MIN = 45

# The retired reasons 0.35 persisted as `paused` (the PRD A.1 reasons that
# were `pauseDetail` shapes under `operator_input_needed` have their own fixture).
PERSISTED_REASONS = [
    "operator_input_needed",
    "verdict_gate_operator_input_needed",
    "rejected_last_approach",
    "rejected_repeated_signature",
    "stagnation",
    "unschedulable_dependency_cycle",
    "unresolvable_dependency_edge",
    "review_contract_unrepaired",
    "review_findings_unusable",
]


def _load(case: str, tmp_path: Path, *, timeout_min=TIMEOUT_MIN) -> RunDir:
    """Copy the fixture run dir out of the tree and load it the way the
    engine does at startup."""
    root = tmp_path / case
    shutil.copytree(FIXTURES / case, root)
    run = RunDir(root)
    run.migrate_legacy_stop_state(now_epoch=NOW, timeout_min=timeout_min,
                                  cure_pass_ceiling=3)
    return run


def _fixture_status(case: str) -> dict:
    return json.loads((FIXTURES / case / "status.json").read_text())


def test_the_fixtures_are_0_35_run_dirs_that_v0_36_never_writes():
    assert _fixture_status("timed_out_time")["state"] == "timed_out"
    for reason in PERSISTED_REASONS:
        status = _fixture_status(f"paused_{reason}")
        assert status["state"] == "paused" and status["pauseReason"] == reason
        assert "pausedBy" not in status and "inputQuestion" not in status


@pytest.mark.parametrize("case,budget", [("timed_out_iterations", "iterations"),
                                         ("timed_out_time", "time")])
def test_a_timed_out_run_reads_as_exhausted_with_the_recorded_budget(
        case, budget, tmp_path):
    assert _fixture_status(case)["state"] == "timed_out"
    status = _load(case, tmp_path).read_status()
    assert status["state"] == "exhausted"
    assert status["exhaustedBudget"] == budget
    assert status["verdict"] is None
    assert state.format_state_display(status) == f"exhausted ({budget})"
    # the hold's own note survives the load
    assert status["reason"] == _fixture_status(case)["reason"]


@pytest.mark.parametrize("reason", PERSISTED_REASONS)
def test_a_retired_reason_paused_run_reads_as_awaiting_input(reason, tmp_path):
    status = _load(f"paused_{reason}", tmp_path).read_status()
    assert status["state"] == "awaiting_input"
    question = status["inputQuestion"]
    assert question["kind"] == "findings"
    assert question["trigger"] == "cure_rounds_spent"
    assert question["judge"] == (
        "verdict_gate" if reason == "verdict_gate_operator_input_needed"
        else "review")
    assert question["parkedAt"] == utc_from_epoch(NOW)
    assert question["expiresAt"] == utc_from_epoch(NOW + TIMEOUT_MIN * 60)
    # the resume routing still reads the reason the 0.35 engine recorded
    assert status["pauseReason"] == reason


def test_a_pause_detail_shape_under_operator_input_needed_reads_the_same(
        tmp_path):
    status = _load("paused_rate_limit_ceiling", tmp_path).read_status()
    assert status["state"] == "awaiting_input"
    assert status["inputQuestion"]["trigger"] == "cure_rounds_spent"
    assert status["pauseDetail"]["reason"] == "rate_limit_ceiling"


def test_expires_at_is_load_time_plus_the_timeout_and_never_for_zero(tmp_path):
    soon = _load("paused_stagnation", tmp_path / "a", timeout_min=5)
    assert soon.read_status()["inputQuestion"]["expiresAt"] == \
        utc_from_epoch(NOW + 5 * 60)
    never = _load("paused_stagnation", tmp_path / "b", timeout_min=0)
    assert never.read_status()["inputQuestion"]["expiresAt"] is None
    default = _load("paused_stagnation", tmp_path / "c", timeout_min=None)
    assert default.read_status()["inputQuestion"]["expiresAt"] == utc_from_epoch(
        NOW + state.DEFAULT_INPUT_TIMEOUT_MIN * 60)


def test_a_second_load_keeps_the_first_loads_expiry(tmp_path):
    run = _load("paused_rejected_last_approach", tmp_path)
    first = run.read_status()["inputQuestion"]
    assert run.migrate_legacy_stop_state(now_epoch=NOW + 9999,
                                         timeout_min=TIMEOUT_MIN) is None
    assert run.read_status()["inputQuestion"] == first
    exhausted = _load("timed_out_time", tmp_path)
    assert exhausted.migrate_legacy_stop_state(now_epoch=NOW) is None


def test_an_operator_pause_and_a_running_run_are_not_migrated(tmp_path):
    root = tmp_path / "operator"
    root.mkdir()
    run = RunDir(root)
    run.update_status(state="paused", pausedBy="operator", pauseNote="lunch")
    assert run.migrate_legacy_stop_state(now_epoch=NOW) is None
    assert run.read_status()["state"] == "paused"
    # a 0.35 operator pause carried no reason at all
    run.update_status(pausedBy=state.REMOVE_STATUS_KEY, pauseNote=None)
    assert run.migrate_legacy_stop_state(now_epoch=NOW) is None
    assert run.read_status()["state"] == "paused"
    run.update_status(state="running", pauseReason="stagnation")
    assert run.migrate_legacy_stop_state(now_epoch=NOW) is None
    assert run.read_status()["state"] == "running"


def test_cure_pass_totals_are_kept_and_the_current_approach_counts_its_own(
        tmp_path):
    fixture = _fixture_status("cure_passes_totals")
    assert fixture["approach"] == 2
    run = _load("cure_passes_totals", tmp_path)
    status = run.read_status()
    # the run-wide counters are untouched, still readable for display
    assert status["curePasses"] == {"1": 3, "2": 1}
    assert status["gateCurePasses"] == {"1": 1, "2": 1}
    # the question shows approach 2's own rounds (1 review + 1 gate), not the
    # run-wide total of 6, against approach 2's own allowance (ceiling 3; the
    # top-up on approach 1 does not carry over)
    question = status["inputQuestion"]
    assert question["approach"] == 2
    assert question["cureRoundsUsed"] == 2
    assert question["cureRoundsAllowed"] == 3

    sup = make_supervisor(tmp_path / "sup", status=status, approach=2,
                          cure_pass_ceiling=3)
    assert sup._cure_passes_for_run() == 6
    assert sup._cure_rounds_used(2) == 2
    assert sup._cure_rounds_used(1) == 4
    assert sup._cure_rounds_allowed(2) == 3
    assert sup._cure_rounds_allowed(1) == 5
    assert sup.cure_passes_used == 2
