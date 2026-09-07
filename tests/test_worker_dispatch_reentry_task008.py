"""v0.17 task 008 (requirement B.2, #128): a repaired worker verdict is
applied by RE-ENTERING the verdict dispatch, at most once per iteration.

Task 008's own module, kept SEPARATE from the contract-ladder module's own
`test_a_malformed_complete_report_is_repaired_by_one_uncharged_re_ask`
(`tests/test_contract_ladder.py`, folded there by task 024/D.3) -- which shares the
`task008` filename suffix only because an unrelated, long-finished v0.13
task also carried the number 008, and whose one test exercises a DIFFERENT
ladder (the `elif verdict == "complete":` branch's own semantic re-ask, a
v0.13 task 006 mechanism). The three tests here drive the generic schema
site (`elif schema_errors:` in `LoopSupervisor._step_worker`) that
requirement B.2 changed:

(a) `test_generic_schema_site_repair_counts_the_re_entry_once` -- after
    `enforcement.ok` at that site the dispatch is re-entered with the
    repaired payload EXACTLY once, counted directly on the engine's own
    `_last_worker_dispatch_reentries`;
(b) `test_three_consecutive_repaired_completes_never_trip_the_3_strike_guard`
    -- a repaired `complete` moves the task through the branch that owns it
    and does NOT register as a no-progress iteration for the 3-strike
    stagnation breaker;
(c) `test_schema_site_ladder_exhaustion_still_falls_through_to_stagnation`
    -- exhaustion at this site still falls through to ordinary stagnation
    bookkeeping (`("advance", "replan", "stagnation_exhausted")`), because
    the re-dispatch fires only on `enforcement.ok`.

The scenario helpers (`_ok`, `_task`, `_review_verified`, `_run_resumed`)
are imported from that sibling module rather than copied, the same way
~20 other modules in this suite reuse a sibling's fixtures
(`tests/test_absolute_timestamps.py`, `tests/test_browser_hub_refresh_loop.py`,
...), so a single change to the scenario shape cannot leave the two modules
disagreeing.
"""

from __future__ import annotations

import asyncio
import json

from test_contract_ladder import (
    TASK_1,
    _ok,
    _review_verified,
    _task,
)
from test_contract_ladder import _run_resumed_worker_e2e as _run_resumed

from ralphd.engine.config import JobConfig
from ralphd.engine.loop import LoopSupervisor
from ralphd.engine.runner import IterationResult
from ralphd.engine.state import RunDir, atomic_write_json


def test_generic_schema_site_repair_counts_the_re_entry_once(tmp_path):
    """Requirement B.2 (#128), the letter of the criterion: after
    `enforcement.ok` at the GENERIC schema site (`elif schema_errors:`,
    reached only when `verdict` matches none of the known enum branches --
    unlike the sibling test above, which never leaves the `"complete"`
    branch's own pre-existing ladder), the dispatch is re-entered with the
    repaired payload EXACTLY once, counted directly on `sup._last_worker_
    dispatch_reentries` -- never merely inferred from the task having
    moved (that inference is `test_a_malformed_complete_report_is_repaired_
    by_one_uncharged_re_ask`'s job, and it exercises a different ladder).
    The first payload's `verdict: "done"` is not a member of the enum at
    all, so it fails schema validation and matches none of `"unsatisfiable"`/
    `"complete"`/`"plan_complete"` -- landing in `elif schema_errors:`,
    task 008's own site -- where the re-ask ladder repairs it into a valid
    `"complete"` report, re-dispatched once into the branch that owns it."""
    root = tmp_path / "run"
    tasks = [_task(status="in-progress")]

    malformed = json.dumps({"verdict": "done"})               # invalid enum
    corrected = json.dumps({"verdict": "complete", "task_id": TASK_1})

    sup, state = _run_resumed(root, {
        "worker": [lambda s, p: _ok(malformed), lambda s, p: _ok(corrected)],
        "review": [_review_verified],
    }, tasks, iterations=3)

    final_tasks = sup.run.read_tasks()["tasks"]
    assert final_tasks[0]["status"] == "completed", final_tasks[0]
    assert sup._last_worker_dispatch_reentries == 1
    assert sup._abort_reason is None
    assert state == "succeeded", sup.run.read_status()


def test_three_consecutive_repaired_completes_never_trip_the_3_strike_guard(
        tmp_path):
    """Requirement B.2 (#128): a repaired `"complete"` must be applied by
    the branch that owns it -- and that application resets the no-progress
    counter the SAME way an ordinary, first-try-valid `"complete"` already
    does (the `stagnant = 0` inside that branch, task008's own re-dispatch
    changes nothing about it). Proven by driving exactly THREE consecutive
    worker passes, each one a malformed-then-repaired `"complete"` for a
    DIFFERENT task -- the same count `stagnant >= 3` would fail the
    approach on if a re-dispatched repair were silently read as three
    passes of silence instead of three passes of real progress."""
    root = tmp_path / "run"
    tasks = [_task(id=tid, status="pending") for tid in ("001", "002", "003")]

    def completed(tid):
        return json.dumps({"verdict": "complete", "task_id": tid})

    # genuinely schema-invalid (not merely missing the optional `task_id`)
    # so every pass lands in the generic `elif schema_errors:` site --
    # task 008's own reentry -- rather than the `"complete"` branch's
    # pre-existing (task 006) semantic ladder.
    malformed = json.dumps({"verdict": "done"})

    sup, state = _run_resumed(root, {
        "worker": [
            lambda s, p: _ok(malformed), lambda s, p: _ok(completed("001")),
            lambda s, p: _ok(malformed), lambda s, p: _ok(completed("002")),
            lambda s, p: _ok(malformed), lambda s, p: _ok(completed("003")),
        ],
        "review": [_review_verified],
    }, tasks, iterations=6)

    final_tasks = {t["id"]: t["status"] for t in sup.run.read_tasks()["tasks"]}
    assert final_tasks == {"001": "completed", "002": "completed",
                            "003": "completed"}, final_tasks

    # the run reached an ordinary review verdict, NOT
    # `stagnation_exhausted`/a replanned approach -- the events log carries
    # no "no task progress" stagnation message anywhere.
    events = [json.loads(x) for x in
              (root / "events.jsonl").read_text().splitlines() if x.strip()]
    assert not any("no task progress" in e.get("message", "") for e in events)
    assert sup._approach == 1, "a stagnation-triggered replan bumps the approach"
    assert sup._last_worker_dispatch_reentries == 1
    assert sup._abort_reason is None
    assert state == "succeeded", sup.run.read_status()


class _AlwaysBogusVerdictAgent:
    """Requirement B.2 (#128)'s other half: a payload the schema rejects
    on EVERY attempt (the re-ask ladder never repairs it), so the generic
    schema site's own `enforce_contract` call genuinely exhausts. Same
    unconditional-repeat shape as
    `test_contract_exhaustion_never_solely_ends_the_run_task009.py`'s
    `_RepeatedlyBrokenAgent`, but `"bogus"` is not a member of the
    `verdict` enum at all -- unlike that module's fixture (`verdict:
    "complete"`, invalid `task_id` type), which is caught by the
    `elif verdict == "complete":` branch, not the `elif schema_errors:`
    branch task 008 changed."""

    def __init__(self):
        self.calls = 0

    def interrupt(self) -> bool:
        return False

    async def run(self, prompt, transcript, **kw):
        self.calls += 1
        r = IterationResult(exit_code=0)
        r.final_text = json.dumps({"verdict": "bogus"})
        r.duration_s = 5.0
        r.usage = {"input": 10, "output": 5, "totalTokens": 15}
        return r


def test_schema_site_ladder_exhaustion_still_falls_through_to_stagnation(
        tmp_path):
    """Requirement B.2 (#128): ladder EXHAUSTION at the generic schema
    site must still fall through to the SAME ordinary `stagnation_
    exhausted` bookkeeping every other exhausted site already takes --
    task 008's re-dispatch only fires on `enforcement.ok`, never changing
    what happens when the ladder never repairs. Mirrors `test_contract_
    exhaustion_never_solely_ends_the_run_task009.py`'s own invariant test
    (`sup._step_worker()` driven directly, once), at task 008's own site."""
    root = tmp_path / "run"
    root.mkdir(parents=True)
    run = RunDir(root=root)
    run.update_status(state="running")
    atomic_write_json(run.tasks_file, {"version": 1, "tasks": [_task()]})
    cfg = JobConfig(run_id="v013-task008-schema-site-exhaustion",
                    iterations=900, max_approaches=12, vigilant=False,
                    on_complete="exit", infra_retry_backoff_s=[0.0],
                    infra_retry_backoff_max_s=0.0, infra_outage_budget_s=1000.0)
    sup = LoopSupervisor(cfg, run, root)
    sup._approach = 1
    agent = _AlwaysBogusVerdictAgent()
    sup.runner = agent  # type: ignore[assignment]

    outcome = asyncio.run(sup._step_worker())

    assert sup._abort_reason is None, (
        "an exhausted contract ladder must never be the SOLE reason the "
        "run ends while budget and approach remain")
    assert run.read_status().get("state") == "running"
    assert outcome == ("advance", "replan", "stagnation_exhausted")
    # never repaired -- the re-entry counter for the LAST pass stays at 0.
    assert sup._last_worker_dispatch_reentries == 0
