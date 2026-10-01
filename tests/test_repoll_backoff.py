"""PRD v0.29 requirement A.1-A.4 (#279): "a long wait costs wall clock, not
iterations." Today `tool_activity` (stagnation witness (d)) is true for an
assigned iteration with at least `STAGNATION_TOOL_ACTIVITY_MIN_CALLS` tool
calls, even when the SAME iteration's own progress claim was measured
uncorroborated with a zero workspace delta -- a worker that keeps polling a
long-running wait (a dispatched CI lane, say) and reporting honestly that
nothing moved yet never trips the three-strike stagnation guard, because its
tool calls alone keep resetting it. That is what selfdev-v028's own #279
measured: 71 one-minute iterations re-checking a CI lane, none of them ever
counted as stagnant.

Call such an iteration a **re-poll**: a worker iteration on an assigned task
that ends `no_verdict` with a `progress_claim` event reading
`corroborated: false` and `deltaSize: 0` (`LoopSupervisor._apply_worker_
progress_report`, task 012/013's own corroboration -- see `tests/test_
progress_claim_corroboration.py`).

Four behaviours, matching PRD A.6 one-for-one:

(i)   a stub worker that re-polls the SAME task four times in a row: the
      FIRST re-poll is counted by the ordinary stagnation formula (A.3: "the
      first re-poll of a run of them is counted ... as A.1 leaves it"), and
      the second through fourth each emit a `task.repoll_backoff` event
      (600s, 1200s, 1800s) and defer the task instead of ever touching the
      approach's own stagnation counter -- so four re-polls alone never
      fail the approach, but (continuing the SAME run two more iterations
      with an ordinary, claim-less no-progress reply) exactly two more
      do, proving the first re-poll really did spend one of the three
      strikes rather than being a freebie;
(ii)  a re-poll followed by a CORROBORATED claim on the same task resets the
      consecutive count, so the very next re-poll backs off as if it were
      the first again (no `task.repoll_backoff` event, no deferral);
(iii) an assigned iteration with tool calls and a *corroborated* claim is
      untouched by any of this: `tool_activity` still fires exactly as
      before this task;
(iv)  a re-poll with 3+ tool calls in its own transcript still does not
      reset the stagnation counter -- `tool_activity` is gated on
      `is_repoll`, never merely on `assigned_id`, so tool calls spent
      re-polling buy nothing.

Each driven through the REAL production call site (`LoopSupervisor.
_step_worker`, a stubbed `sup.runner` only), the same style `tests/test_
progress_claim_corroboration.py` already uses for the claim itself.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from ralphd.engine import state
from ralphd.engine.loop import LoopSupervisor
from ralphd.engine.runner import IterationResult
from ralphd.engine.state import RunDir
from tests.conftest import make_supervisor

TASK_ID = "001t"


def _ok(text: str) -> IterationResult:
    r = IterationResult(exit_code=0)
    r.final_text = text
    r.duration_s = 1.0
    r.usage = {"input": 1, "output": 1, "totalTokens": 2}
    return r


class _ScriptedWorker:
    """Same advance-through-a-script shape every sibling module in this
    suite uses (`tests/test_progress_claim_corroboration.py`'s own
    `_ScriptedWorker`) -- the last entry repeats forever -- extended with
    an optional `tool_calls` count per step: when set, that many
    `tool_execution_start` events are appended to THIS iteration's own
    transcript (`state.iteration_output_path`) before the step's reply is
    returned, the exact shape `tests/test_worker_hat.py`'s `_tool_heavy`
    helper already uses for witness (d). `sup.iterations_used` is already
    the current iteration's number by the time a hat's runner is invoked
    (`LoopSupervisor._run_iteration_once` increments it, and creates the
    iteration directory, strictly before dispatching to `self.runner.run`).
    """

    def __init__(self, sup: LoopSupervisor, script: list[dict]):
        self.sup = sup
        self.script = script
        self.calls = 0

    def interrupt(self) -> bool:
        return False

    async def run(self, prompt, transcript, **kw) -> IterationResult:
        step = self.script[min(self.calls, len(self.script) - 1)]
        self.calls += 1
        tool_calls = step.get("tool_calls", 0)
        if tool_calls:
            path = state.iteration_output_path(
                self.sup.run.root, self.sup.iterations_used)
            with open(path, "a") as f:
                f.writelines(json.dumps(
                        {"type": "tool_execution_start", "toolName": "bash",
                         "args": {"command": "true"}}) + "\n" for _ in range(tool_calls))
        return _ok(step["text"])


def _worker_sup(tmp_path: Path, name: str, **cfg_kw) -> tuple[LoopSupervisor, RunDir]:
    # Same split run-dir/workspace shape task 012's own
    # `test_progress_claim_corroboration.py::_worker_sup` uses: the
    # fingerprint this task's re-poll detector depends on
    # (`_apply_worker_progress_report`'s `fp_before`/`fp_after`) reads
    # `self.workspace` specifically, never the run dir's own bookkeeping.
    #
    # Built through `tests.conftest.make_supervisor` rather than a direct
    # `LoopSupervisor(...)` construction (contrast `test_progress_claim_
    # corroboration.py`'s own helper) -- this module's own scenarios drive
    # the engine's REAL deferral/wake path (`_apply_engine_repoll_backoff`
    # -> `_sleep_until_earliest_deferred`), which calls a real `asyncio.
    # sleep` for the backoff duration; only `make_supervisor` patches that
    # process-global to be instant for the life of the test (`real_sleep=
    # False`, the default) -- a bare construction leaves it unpatched and
    # a 600s+ wait really elapses.
    root = tmp_path / name
    kw = {"iterations": 4, "max_approaches": 3, "vigilant": False,
          "on_complete": "exit", "infra_retry_backoff_s": [0.0],
          "infra_retry_backoff_max_s": 0.0, "infra_outage_budget_s": 1000.0,
          "run_root": "run", "workspace": "workspace",
          "status": {"state": "running"}, "approach": 1,
          "tasks": [
              {"id": TASK_ID, "title": "wait on something that is not done yet",
               "status": "in-progress", "successCriteria": "n/a"},
          ],
          **cfg_kw}
    sup = make_supervisor(root, run_id=f"repoll-{name}", **kw)
    return sup, sup.run


def _events_of_type(run: RunDir, type_: str) -> list[dict]:
    events = [json.loads(x) for x in
              (run.root / "events.jsonl").read_text().splitlines() if x.strip()]
    return [e for e in events if e.get("type") == type_]


def _repoll_step(summary: str = "polled the lane, nothing new yet") -> dict:
    """A re-poll step: `verdict: "no_verdict"` with a `commit`-kind
    `progress` claim and NO matching tree delta -- uncorroborated,
    `deltaSize: 0` (`tests/test_progress_claim_corroboration.py`'s own
    `test_commit_evidence_with_no_tree_delta_is_not_corroborated` pins the
    corroboration half of this shape already; this module only adds what
    `_step_worker` does with the RESULT)."""
    payload = {"verdict": "no_verdict",
               "progress": {"summary": summary,
                            "evidence": [{"kind": "commit", "ref": "abc123"}]}}
    return {"text": json.dumps(payload)}


def _plain_no_verdict_step(tool_calls: int = 0) -> dict:
    """An ordinary `no_verdict` reply carrying no `progress` block at all
    -- NOT a re-poll (no `progress_claim` event is even recorded this
    pass), so it must fall straight through to the UNCHANGED four-witness
    formula. `tool_calls` (default 0) lets a caller isolate witness (d)
    (`tool_activity`) on its own, with no claim in the picture at all."""
    step = {"text": json.dumps({"verdict": "no_verdict"})}
    if tool_calls:
        step["tool_calls"] = tool_calls
    return step


def _warmup_step() -> dict:
    """A plain, claim-less, tool-call-less `no_verdict` meant to be the
    FIRST iteration this task is ever dispatched on in a given test.

    `_step_worker`'s own pre-dispatch bookkeeping stamps a brand-new
    `criteriaFingerprint`/`criteriaOfRecord` pair onto a task the very
    first time it is read (`state.tasks_doc_excluding_new_entries`'s own
    caller site, unconditional, nothing to do with this task's own
    changes) -- which makes `before != after` true for that one iteration
    ALONE, independent of anything a worker replies. Every test below that
    counts an EXACT number of iterations to a three-strike stagnation
    guard spends one dispatch on this first, so that one-time stamp
    witness lands on an iteration nobody is measuring, not on the first
    re-poll/tool-call/claim the test actually cares about."""
    return _plain_no_verdict_step()


def _failing_approach_logged(run: RunDir) -> bool:
    for ev in _events_of_type(run, "log"):
        if ev.get("level") == "error" and "failing approach" in str(ev.get("message", "")):
            return True
    return False


# ---------------------------------------------------------------------------
# (i) four re-polls: the first counts, the second-to-fourth back off
# ---------------------------------------------------------------------------


def test_four_repolls_the_first_counts_the_rest_back_off_without_failing(tmp_path):
    sup, run = _worker_sup(
        tmp_path, "four-repolls",
        # One warm-up dispatch (absorbs the one-time criteria-fingerprint
        # stamp witness, see `_warmup_step`), then four re-polls, then two
        # ordinary claim-less no-progress replies that only reach
        # stagnant == 3 if the FIRST re-poll already spent one of the
        # three strikes. `iterations` has one spare slot of headroom past
        # the seven real dispatches so the stagnation guard's own
        # post-loop bookkeeping (budget_left()) is not what ends this run
        # instead of the guard itself.
        iterations=8)
    sup.runner = _ScriptedWorker(sup, [  # type: ignore[assignment]
        _warmup_step(),
        _repoll_step(), _repoll_step(), _repoll_step(), _repoll_step(),
        _plain_no_verdict_step(), _plain_no_verdict_step(),
    ])

    outcome = asyncio.run(sup._step_worker())

    backoffs = _events_of_type(run, "task.repoll_backoff")
    assert len(backoffs) == 3, backoffs
    assert [b["consecutiveCount"] for b in backoffs] == [2, 3, 4]
    assert [b["backoffSeconds"] for b in backoffs] == [600, 1200, 1800]
    for b in backoffs:
        assert b["taskId"] == TASK_ID
        assert b["deferredUntil"]

    # Four re-polls alone (the first of them already counted toward
    # stagnation) never fail the approach on their own.
    claims = _events_of_type(run, "progress_claim")
    assert len(claims) == 4
    assert [c["corroborated"] for c in claims] == [False, False, False, False]
    assert [c["deltaSize"] for c in claims] == [0, 0, 0, 0]

    # ... but it DID count: two more ordinary no-progress iterations are
    # all it takes to reach the three-strike guard, not three more --
    # proving the first re-poll was not a free pass.
    assert _failing_approach_logged(run)
    assert outcome[1] in ("replan", "paused")

    task = run.read_tasks()["tasks"][0]
    assert task["id"] == TASK_ID


def test_the_backed_off_task_is_pending_with_a_deferred_until_and_reason_naming_the_count(
        tmp_path):
    sup, run = _worker_sup(tmp_path, "backoff-record", iterations=4)
    sup.runner = _ScriptedWorker(sup, [  # type: ignore[assignment]
        _repoll_step(), _repoll_step(), _repoll_step(), _repoll_step(),
    ])

    asyncio.run(sup._step_worker())

    task = run.read_tasks()["tasks"][0]
    assert task["status"] == "pending"
    assert task.get("deferredUntil")
    reason = task.get("deferredReason", "")
    assert "re-poll" in reason
    assert "4" in reason

    # Does not fail across exactly these four re-polls.
    assert not _failing_approach_logged(run)


# ---------------------------------------------------------------------------
# (ii) a corroborated claim resets the consecutive count
# ---------------------------------------------------------------------------


def test_a_corroborated_claim_resets_the_count_so_the_next_repoll_does_not_back_off(
        tmp_path):
    sup, run = _worker_sup(tmp_path, "resets-count", iterations=4)

    class _ResettingWorker(_ScriptedWorker):
        """Step 3 (index 2) writes a real file into the workspace before
        returning its (otherwise ordinary) `progress` claim -- a genuine
        tree delta, corroborated, exactly `tests/test_progress_claim_
        corroboration.py`'s own `_FileWritingWorker`."""

        async def run(self, prompt, transcript, **kw) -> IterationResult:
            if self.calls == 2:
                (self.sup.workspace / "evidence.txt").write_text("touched\n")
            return await super().run(prompt, transcript, **kw)

    sup.runner = _ResettingWorker(sup, [  # type: ignore[assignment]
        _repoll_step(),            # 1: re-poll, count 1 -- ordinary
        _repoll_step(),            # 2: re-poll, count 2 -- backs off (600s)
        _repoll_step("made real progress this time"),  # 3: corroborated (real delta)
        _repoll_step(),            # 4: re-poll again -- count must be back at 1
    ])

    asyncio.run(sup._step_worker())

    backoffs = _events_of_type(run, "task.repoll_backoff")
    # Exactly one backoff (the second iteration) -- the corroborated claim
    # on the third iteration resets the ladder, so the fourth re-poll is
    # treated as the FIRST of a new run and does not back off again.
    assert len(backoffs) == 1, backoffs
    assert backoffs[0]["consecutiveCount"] == 2
    assert backoffs[0]["backoffSeconds"] == 600

    claims = _events_of_type(run, "progress_claim")
    assert [c["corroborated"] for c in claims] == [False, False, True, False]


# ---------------------------------------------------------------------------
# (iii) a corroborated claim with tool calls: tool_activity unchanged
# ---------------------------------------------------------------------------


def test_a_corroborated_claim_with_tool_calls_keeps_tool_activity_as_today(tmp_path):
    sup, run = _worker_sup(tmp_path, "corroborated-tool-activity", iterations=7)

    class _FileWritingToolWorker(_ScriptedWorker):
        async def run(self, prompt, transcript, **kw) -> IterationResult:
            if self.calls == 1:
                (self.sup.workspace / "evidence.txt").write_text("touched\n")
            return await super().run(prompt, transcript, **kw)

    corroborated_step = _repoll_step("made real progress, with tool calls")
    corroborated_step["tool_calls"] = 3

    sup.runner = _FileWritingToolWorker(sup, [  # type: ignore[assignment]
        _warmup_step(),
        corroborated_step,
        # A PLAIN no_verdict carrying no progress claim at all, but with
        # 3 tool calls -- isolates witness (d) (`tool_activity`) on its
        # own: `is_repoll` requires a claim (`progress_claim_this_pass`
        # a dict), so an iteration with no claim at all is never a
        # re-poll by construction, and this is exactly the "ordinary
        # tool-heavy no-claim iteration" shape `tool_activity` has always
        # covered.
        _plain_no_verdict_step(tool_calls=3),
        _plain_no_verdict_step(),
        _plain_no_verdict_step(),
        _plain_no_verdict_step(),
    ])

    outcome = asyncio.run(sup._step_worker())

    claims = _events_of_type(run, "progress_claim")
    assert len(claims) == 1
    assert claims[0]["corroborated"] is True
    assert claims[0]["deltaSize"] >= 1

    # No `task.repoll_backoff` -- a corroborated claim is never a re-poll.
    assert _events_of_type(run, "task.repoll_backoff") == []

    # The corroborated claim (iteration 2) and the claim-less tool-heavy
    # iteration (iteration 3) both reset stagnant to 0 exactly as before
    # this task; three further plain, tool-less, claim-less iterations
    # (4, 5, 6) are exactly enough to stagnate -- proving `tool_activity`
    # on iteration 3 (no claim at all, so `is_repoll` cannot have gated
    # it) still fired, the same as it always has.
    assert _failing_approach_logged(run)
    assert outcome[1] in ("replan", "paused")


# ---------------------------------------------------------------------------
# (iv) a re-poll with tool calls does not reset the stagnation counter
# ---------------------------------------------------------------------------


def test_a_repoll_with_tool_calls_does_not_reset_the_stagnation_counter(tmp_path):
    sup, run = _worker_sup(tmp_path, "repoll-tool-calls", iterations=5)

    repoll_with_tools = _repoll_step("polled the lane and read a lot of files")
    repoll_with_tools["tool_calls"] = 5

    # A warm-up, then one re-poll (with 5 tool calls, well above
    # STAGNATION_TOOL_ACTIVITY_MIN_CALLS) followed by two ordinary
    # claim-less no-progress iterations. If the re-poll's own tool calls
    # still counted as `tool_activity` (the pre-this-task behaviour,
    # #279's own regression), the counter would reset right there and
    # three total post-warm-up iterations would never stagnate.
    sup.runner = _ScriptedWorker(sup, [  # type: ignore[assignment]
        _warmup_step(),
        repoll_with_tools,
        _plain_no_verdict_step(),
        _plain_no_verdict_step(),
    ])

    outcome = asyncio.run(sup._step_worker())

    # Never backed off (this is the ladder's first and only re-poll of the
    # run), but its own tool calls bought it nothing either: the re-poll
    # plus two plain iterations is exactly enough to stagnate.
    assert _events_of_type(run, "task.repoll_backoff") == []
    assert _failing_approach_logged(run)
    assert outcome[1] in ("replan", "paused")
