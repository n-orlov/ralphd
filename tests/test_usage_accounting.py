"""Black-box tests for per-phase/per-approach usage accounting (PRD req 19).

Reuses tests/test_e2e.py's `engine_factory`/`EngineProc` harness: launches a
real `ralphd-engine` with the stub `pi`, then observes strictly from the
outside (HTTP API + run-dir files).
"""

from __future__ import annotations

import json

from test_e2e import engine_factory

from ralphd.engine.loop import LoopSupervisor

__all__ = ["engine_factory"]


def test_status_usage_by_phase_and_by_approach_sum_to_total(engine_factory):
    """Vigilant job -> planning, worker, verify, worker, verify, review (2
    tasks, single approach). Every iteration's usage must be reflected in
    both usage.byPhase[phase] and usage.byApproach[str(approach)], and the
    sums of either breakdown must equal the top-level totals exactly."""
    e = engine_factory(job={"on_complete": "idle", "vigilant": True})
    e.wait_api()
    e.wait_state(("succeeded",), timeout=90)

    status = json.loads((e.run_dir / "status.json").read_text())
    usage = status["usage"]
    by_phase = usage["byPhase"]
    by_approach = usage["byApproach"]

    # Expected phase sequence per test_vigilant_happy_path: planning, worker,
    # verify (x2 tasks), review -- i.e. planning:1, worker:2, verify:2, review:1
    assert set(by_phase.keys()) == {"planning", "worker", "verify", "review"}
    assert set(by_approach.keys()) == {"1"}

    token_fields = ["input", "output", "cacheRead", "cacheWrite", "totalTokens"]
    for field in token_fields + ["costUSD"]:
        total = usage.get(field, 0)
        phase_sum = sum(v.get(field, 0) for v in by_phase.values())
        approach_sum = sum(v.get(field, 0) for v in by_approach.values())
        if isinstance(total, float):
            assert round(phase_sum, 6) == round(total, 6), field
            assert round(approach_sum, 6) == round(total, 6), field
        else:
            assert phase_sum == total, field
            assert approach_sum == total, field

    # Non-trivial: real usage was actually recorded (stub pi always reports
    # some tokens/cost per iteration), not just empty/zero buckets.
    assert usage["totalTokens"] > 0
    assert usage["costUSD"] > 0
    assert by_phase["worker"]["totalTokens"] == 2 * by_phase["planning"]["totalTokens"]
    assert by_phase["verify"]["totalTokens"] == by_phase["planning"]["totalTokens"] * 2

    # API surfaces the identical structure (GET /status)
    _, api_status = e.api("GET", "/status")
    assert api_status["usage"] == usage


def test_status_usage_by_approach_breaks_down_across_a_rejected_approach(engine_factory):
    """A reviewer rejection starts approach 2; usage recorded during approach
    1's iterations must stay attributed to byApproach["1"] and NOT bleed into
    approach 2's bucket, while the top-level total keeps accumulating both."""
    e = engine_factory(
        job={"on_complete": "exit", "max_approaches": 2, "iterations": 20},
        stub_env={"STUB_REVIEW_FAILS": "1"},
    )
    e.wait_api()
    e.wait_state(("succeeded", "failed"), timeout=90)

    status = json.loads((e.run_dir / "status.json").read_text())
    usage = status["usage"]
    by_approach = usage["byApproach"]

    assert set(by_approach.keys()) >= {"1", "2"}
    for field in ("input", "output", "totalTokens"):
        assert sum(v.get(field, 0) for v in by_approach.values()) == usage.get(field, 0)
        # Each approach that ran at least one iteration recorded > 0 usage.
        for v in by_approach.values():
            assert v.get(field, 0) >= 0
    assert by_approach["1"]["totalTokens"] > 0
    assert by_approach["2"]["totalTokens"] > 0


# --- task 050 (#10): priced/unpriced mixes in a bucket -------------------
#
# Unit level over the one shared merge helper (every bucket -- total, byPhase,
# byApproach -- goes through it), then black-box over the real engine.

PRICED = {"input": 100, "output": 10, "totalTokens": 110,
          "costUSD": 0.01, "costPriced": True}
UNPRICED = {"input": 100, "output": 10, "totalTokens": 110, "costPriced": False}
NO_TRAFFIC = {"input": 0, "output": 0, "totalTokens": 0, "costUSD": 0}


def _merge(*usages: dict) -> dict:
    bucket: dict = {}
    for u in usages:
        LoopSupervisor._merge_usage(bucket, dict(u))
    return bucket


def test_fully_priced_bucket_carries_no_cost_status_marker():
    bucket = _merge(PRICED, PRICED)
    assert bucket["costUSD"] == 0.02
    assert "costStatus" not in bucket, "a fully priced bucket is unchanged"
    assert "costPriced" not in bucket, "the per-iteration marker is not a counter"


def test_no_traffic_bucket_carries_no_cost_status_marker():
    bucket = _merge(NO_TRAFFIC, NO_TRAFFIC)
    assert bucket == {"input": 0, "output": 0, "totalTokens": 0, "costUSD": 0}


def test_fully_unpriced_bucket_is_unknown_and_has_no_cost():
    bucket = _merge(UNPRICED, UNPRICED)
    assert bucket["costStatus"] == "unknown"
    assert "costUSD" not in bucket, "unknown cost must not read as $0"
    assert bucket["totalTokens"] == 220


def test_unpriced_iterations_plus_no_traffic_zeros_stay_unknown():
    """A truthful $0 from a no-traffic iteration does not make the bucket's
    unknown cost knowable."""
    bucket = _merge(NO_TRAFFIC, UNPRICED)
    assert bucket["costStatus"] == "unknown"
    assert bucket["costUSD"] == 0


def test_mixed_bucket_is_partial_and_keeps_the_priced_subtotal():
    for order in ((PRICED, UNPRICED), (UNPRICED, PRICED)):
        bucket = _merge(*order)
        assert bucket["costStatus"] == "partial", order
        assert bucket["costUSD"] == 0.01, "partial == priced subtotal only"
        assert bucket["totalTokens"] == 220


def test_a_partially_unpriced_iteration_makes_the_bucket_partial():
    """An iteration that itself mixed priced and unpriced messages carries
    costUSD together with costPriced false (task 049)."""
    bucket = _merge({"totalTokens": 110, "costUSD": 0.02, "costPriced": False})
    assert bucket["costStatus"] == "partial"
    assert bucket["costUSD"] == 0.02


def test_cost_status_is_monotone_once_something_is_unknown():
    bucket = _merge(UNPRICED, PRICED, PRICED)
    assert bucket["costStatus"] == "partial", "a later price cannot erase unknown"
    bucket = _merge(PRICED, UNPRICED, PRICED)
    assert bucket["costStatus"] == "partial"
    bucket = _merge(UNPRICED, NO_TRAFFIC)
    assert bucket["costStatus"] == "unknown"


def test_fully_priced_run_usage_carries_no_cost_status_anywhere(engine_factory):
    """Regression guard for the contract: nothing about a normal, fully priced
    run's usage changes."""
    e = engine_factory(job={"on_complete": "exit", "iterations": 8})
    assert e.proc.wait(timeout=60) == 0
    usage = json.loads((e.run_dir / "status.json").read_text())["usage"]
    assert usage["costUSD"] > 0
    assert "costStatus" not in usage
    for bucket in list(usage["byPhase"].values()) + list(usage["byApproach"].values()):
        assert "costStatus" not in bucket, bucket


def test_fully_unpriced_run_marks_total_and_buckets_unknown(engine_factory):
    e = engine_factory(job={"on_complete": "exit", "iterations": 8},
                       stub_env={"STUB_UNPRICED_COUNT": "99"})
    assert e.proc.wait(timeout=60) == 0
    usage = json.loads((e.run_dir / "status.json").read_text())["usage"]
    assert usage["totalTokens"] > 0
    assert usage["costStatus"] == "unknown"
    assert "costUSD" not in usage
    for bucket in list(usage["byPhase"].values()) + list(usage["byApproach"].values()):
        assert bucket["costStatus"] == "unknown", bucket
        assert "costUSD" not in bucket, bucket


def test_mixed_run_marks_the_total_partial(engine_factory):
    """Iteration 1 (planning) is priced, iteration 2 is not: the run total
    mixes both and must say so instead of publishing the priced subset as
    the cost."""
    e = engine_factory(job={"on_complete": "exit", "iterations": 8},
                       stub_env={"STUB_UNPRICED_SKIP": "1",
                                 "STUB_UNPRICED_COUNT": "1"})
    assert e.proc.wait(timeout=60) == 0
    usage = json.loads((e.run_dir / "status.json").read_text())["usage"]
    assert usage["costStatus"] == "partial"
    assert usage["costUSD"] > 0, "the priced subtotal is still reported"
    # The phase that ran the unpriced iteration is flagged; a phase whose
    # iterations were all priced keeps a clean, fully-known cost.
    flagged = {p for p, b in usage["byPhase"].items() if b.get("costStatus")}
    assert flagged, usage["byPhase"]
    assert all(usage["byPhase"][p]["costStatus"] in ("partial", "unknown")
               for p in flagged)
    assert usage["byPhase"]["planning"].get("costStatus") is None
    assert usage["byApproach"]["1"]["costStatus"] == "partial"
