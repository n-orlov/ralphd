"""Task 101 (#61): the blocker-signature guard on the `replan` transition.

A job whose worker stagnates (3 iterations with no `tasks.json` change) on a
STRUCTURAL wall must not spend every remaining approach re-planning into the
identical wall. This module drives the real engine binary (same shape as
`tests/test_e2e.py`) with the stub `pi`'s `STUB_WORKER_STALLS_BY_APPROACH`
knob (already wired for exactly this, task 026's WIP) and asserts both
directions the PRD names:

* a SECOND approach that stagnates on the SAME blocker signature as its
  predecessor ends the run terminally right there, with the blocker text in
  the terminal `reason`, WITHOUT ever starting a third approach even though
  the job's `max_approaches` would allow one;
* a SECOND approach that stagnates on a DIFFERENT blocker signature is not
  short-circuited: the run keeps its remaining approaches and goes on to
  actually spend the third one.

The pre-existing infra-outage degraded path (ledger guard 17, `health:
"degraded"` while retrying a transient fault) is a completely different
mechanism -- it never touches `blockerSignatures` -- and
`tests/test_engine_infra_retry.py` (unchanged by this task) is what proves it
still works; nothing here duplicates that coverage.
"""

from __future__ import annotations

import json

import pytest

from tests.conftest import LiveRun
from tests.flows.flowlib import wait_engine_exit


@pytest.fixture
def engine_factory(tmp_path):
    procs: list[LiveRun] = []

    def make(job: dict | None = None, stub_env: dict | None = None) -> LiveRun:
        defaults = {"run_id": "stagnation-sig", "iterations": 40,
                    "max_approaches": 3, "on_complete": "idle"}
        e = LiveRun(tmp_path, {**defaults, **(job or {})}, stub_env)
        procs.append(e)
        return e

    yield make
    for e in procs:
        e.stop()


def test_a_repeated_blocker_signature_ends_the_run_without_a_third_approach(
        engine_factory):
    e = engine_factory(
        job={"on_complete": "exit", "max_approaches": 3},
        stub_env={"STUB_WORKER_STALLS": "1",
                  "STUB_WORKER_STALLS_BY_APPROACH":
                      "the same structural wall,the same structural wall"})
    assert wait_engine_exit(e, timeout=90) == 1
    status = json.loads((e.run_dir / "status.json").read_text())
    assert status["state"] == "failed"
    assert status["verdict"] == "unverified"
    # Ended AT approach 2 -- a third approach was available
    # (max_approaches=3) but never entered, because the guard fired the
    # moment approach 2's own stagnation signature matched approach 1's.
    assert status["approach"] == 2
    assert "structural wall" in (status.get("reason") or "")
    assert "#61" in (status.get("reason") or "")
    signatures = status.get("blockerSignatures") or {}
    assert signatures.get("1") == signatures.get("2")
    assert signatures["1"] is not None
    # No third approach's planning iteration ever ran -- both approaches 1
    # and 2 got archived (stagnation archives on its way to `replan`
    # regardless of what `replan` decides next), but only two `planning`
    # iterations exist on disk.
    approach_dirs = sorted((e.run_dir / "approaches").iterdir())
    assert [d.name for d in approach_dirs] == ["01", "02"]
    iters = sorted((e.run_dir / "iterations").iterdir())
    metas = [json.loads((d / "meta.json").read_text()) for d in iters]
    assert sum(1 for m in metas if m["phase"] == "planning") == 2


def test_a_distinct_blocker_signature_holds_exhausted_on_the_last_approach(
        engine_factory):
    """The mismatched signature between approach 1 and approach 2 never
    short-circuits the replan, so all three approaches get spent. Approach
    3 is the last: its own stagnation (even on the same wall as approach
    2) archives it and, with no approach left, holds `exhausted` on
    `approaches` (v0.36 requirement F.1) rather than failing on the
    same-wall guard or parking. This module's OWN concern -- the
    blocker-signature guard on the `replan` transition -- stays proven by
    the fact that a real THIRD approach ran at all (never short-circuited
    the way the sibling test above is)."""
    e = engine_factory(
        job={"on_complete": "exit", "max_approaches": 3},
        stub_env={"STUB_WORKER_STALLS": "1",
                  "STUB_WORKER_STALLS_BY_APPROACH":
                      "wall A entirely unrelated to wall B,"
                      "wall B entirely unrelated to wall A,"
                      "wall B entirely unrelated to wall A"})
    status = e.wait_state(("exhausted",), timeout=90)
    assert status["exhaustedBudget"] == "approaches"
    assert status.get("pauseReason") is None
    assert status.get("verdict") is None
    # All three approaches were spent -- the mismatched signature between
    # approach 1 and approach 2 never short-circuited the replan.
    assert status["approach"] == 3
    signatures = status.get("blockerSignatures") or {}
    assert signatures.get("1") != signatures.get("2")
    assert signatures.get("2") == signatures.get("3")
    # Every approach was archived, the last one included.
    approach_dirs = sorted((e.run_dir / "approaches").iterdir())
    assert [d.name for d in approach_dirs] == ["01", "02", "03"]
    iters = sorted((e.run_dir / "iterations").iterdir())
    metas = [json.loads((d / "meta.json").read_text()) for d in iters]
    assert sum(1 for m in metas if m["phase"] == "planning") == 3
