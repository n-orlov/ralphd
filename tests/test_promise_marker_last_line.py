"""Task 046 (#55): a promise marker counts only as the last line, and a
COMPLETE counts only when tasks.json agrees.

The near-miss this closes is real. On another run's worker iteration 63 the
agent obeyed the contract perfectly and ended its reply with

    Not all tasks are terminal (36 pending), so per protocol I will not emit
    `<promise>COMPLETE</promise>`.

`sawComplete` was recorded true, because the engine's test was the bare
substring `COMPLETE in final_text`: the agent's explanation of why it was NOT
signalling *was* read as the signal. The worker loop ended with 36 tasks
pending and went to review; only the reviewer rejecting the approach stopped
that run from going terminal `succeeded`/`verified` with three quarters of its
plan undone. `saw_verified` had the identical defect with nothing behind it.

Task 015 (requirement O, #100) retired the literal `<promise>COMPLETE</promise>`
marker itself as the mechanism that carries this claim: the worker's
whole-plan-is-done signal is now `{"verdict": "plan_complete"}`, a value in
the SAME contract enum as `complete`/`no_verdict`/`unsatisfiable`, valid ONLY
when no task was assigned this iteration. `IterationResult.saw_complete` (the
bare-marker string match this module's layer 1 still exercises) is now purely
the operator-facing "mentioned the completion line" diagnostic that
`saw_verified` already was before this task -- it does not drive
`_step_worker`'s decision any more than `saw_verified` ever drove review's.

Three layers, tested here in that order:

1. **String.** The retired marker must still be recognised as the last
   non-blank line, stripped, equal -- `runner.last_nonblank_line()` -- purely
   so the diagnostic can still name a near-miss sentence for the operator.
   Deliberately not an "own line anywhere" (`MULTILINE ^...$`) match, which a
   fenced block or an echoed notes.md section defeats.
2. **Plan.** A `plan_complete` claim is honoured only after the freshly
   re-read plan is checked, exactly as `honoured_complete` checked the
   marker before this task: a task assigned this iteration, or anything not
   `completed`/`skipped`, refuses it and routes it to
   `contract_enforcement.enforce_contract`'s retry ladder -- a malformed
   claim is a contract violation, never silent no-progress. This layer does
   not care how the JSON was formatted, so it stops the whole class rather
   than one shape.
3. **Stagnation.** The counter consults the HONOURED completion (now:
   whether `_step_worker` actually reached the `plan_complete` success
   branch this iteration), not the raw diagnostic. Reading the raw
   diagnostic there would trade the false conclusion for an infinite loop:
   an agent claiming `plan_complete` every iteration while changing nothing
   would hold `stagnant` at 0 forever.
   test_an_agent_that_claims_plan_complete_forever_still_stagnates is that
   test.

Plus the prompt half: `worker.md` no longer needs a prose name for a literal
line it no longer asks anyone to write -- `plan_complete` is JSON like every
other verdict here, with no substring-mention hazard for a naming convention
to defend against, exactly the property `review.md`'s own retired `VERIFIED`
sentinel already had (task 119, #60 pillar 3). The coverage below of
`saw_verified`/`VERIFIED` remains the low-level string mechanics
(`IterationResult`), kept for the "mentioned but did not end with it"
operator diagnostic (task 046, #55) that still runs for every iteration
regardless of phase.

Mutation matrix in the commit message / task notes.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from ralphd.engine import state as state_mod
from ralphd.engine.config import JobConfig
from ralphd.engine.loop import LoopSupervisor
from ralphd.engine.runner import (
    COMPLETE,
    VERIFIED,
    IterationResult,
    last_nonblank_line,
)
from ralphd.engine.state import RunDir, atomic_write_json, task_blocks_completion
from tests.conftest import supervisor_with_runner

REPO = Path(__file__).resolve().parents[1]

NEAR_MISS = (
    "Task 046 is in progress; 36 tasks are still pending.\n"
    f"So per protocol I will not emit `{COMPLETE}`."
)


# -- layer 1: the string ----------------------------------------------------


def _result(text: str) -> IterationResult:
    r = IterationResult(exit_code=0)
    r.final_text = text
    return r


@pytest.mark.parametrize("label, text, complete", [
    ("bare marker", COMPLETE, True),
    ("prose then marker", f"every task is done\n{COMPLETE}", True),
    ("trailing blank lines", f"done\n{COMPLETE}\n\n   \n", True),
    ("trailing spaces on the line", f"done\n  {COMPLETE}  \n", True),
    ("the near-miss sentence", NEAR_MISS, False),
    ("backticked as the last line", f"done\n`{COMPLETE}`", False),
    ("marker then one more sentence", f"{COMPLETE}\nOne more thought.", False),
    ("quoted in a fenced block", f"```\n{COMPLETE}\n```", False),
    ("echoed notes section", f"## Standing rules\n{COMPLETE}\nnotes end here", False),
    ("word inside a longer line", f"I would write {COMPLETE} if I could", False),
    ("no marker at all", "one task advanced, notes refreshed", False),
    ("empty message", "", False),
])
def test_only_the_last_non_blank_line_is_the_completion_marker(label, text, complete):
    assert _result(text).saw_complete is complete, label


@pytest.mark.parametrize("label, text, verified", [
    ("bare marker", VERIFIED, True),
    ("prose then marker", f"every requirement holds\n{VERIFIED}", True),
    ("the near-miss shape", f"two requirements are unmet, so not `{VERIFIED}`", False),
    ("marker then a caveat", f"{VERIFIED}\n(with reservations)", False),
    ("quoted in a fenced block", f"```\n{VERIFIED}\n```", False),
    ("no marker at all", "findings written", False),
])
def test_only_the_last_non_blank_line_is_the_verification_marker(label, text, verified):
    assert _result(text).saw_verified is verified, label


def test_the_two_markers_do_not_answer_for_each_other():
    assert _result(COMPLETE).saw_verified is False
    assert _result(VERIFIED).saw_complete is False


@pytest.mark.parametrize("text, expected", [
    ("", ""),
    ("   \n\n", ""),
    ("one\ntwo\n\n", "two"),
    ("  padded  ", "padded"),
    ("last\n   \n\t\n", "last"),
])
def test_last_nonblank_line_reads_the_end_of_the_message(text, expected):
    assert last_nonblank_line(text) == expected


def test_last_nonblank_line_tolerates_a_message_that_was_never_set():
    assert last_nonblank_line(None) == ""      # type: ignore[arg-type]
    assert IterationResult().last_line == ""


@pytest.mark.parametrize("text, complete_flag, verified_flag", [
    (NEAR_MISS, True, False),
    (f"done\n`{COMPLETE}`", True, False),
    (f"quoting the protocol: {VERIFIED}", False, True),
    (f"{COMPLETE}", False, False),            # honoured, so not "mentioned"
    (f"holds\n{VERIFIED}", False, False),
    ("nothing to see", False, False),
])
def test_a_marker_that_is_not_last_is_reported_as_mentioned(
        text, complete_flag, verified_flag):
    r = _result(text)
    assert r.mentioned_complete_not_last is complete_flag
    assert r.mentioned_verified_not_last is verified_flag


def test_the_match_is_not_a_multiline_own_line_regex():
    """An "on its own line anywhere" match is the wrong fix: a fenced block or
    an echoed notes.md section legitimately puts the marker on its own line.
    Pin the absence of that idiom in the module that owns the match."""
    src = (REPO / "src" / "ralphd" / "engine" / "runner.py").read_text()
    assert "import re" not in src, "the match is not a regex at all"
    assert "re.MULTILINE" not in src and "re.match" not in src
    assert "def last_nonblank_line" in src
    assert "return self.last_line == COMPLETE" in src
    assert "return self.last_line == VERIFIED" in src


# -- layer 2: the plan ------------------------------------------------------


def _ok(text: str = "working") -> IterationResult:
    r = IterationResult(exit_code=0)
    r.final_text = text
    r.duration_s = 30.0
    r.usage = {"input": 10, "output": 5, "totalTokens": 15}
    return r


class _ScriptedAgent:
    """Stands in for PiRunner, dispatching on the phase status.json records
    just before the agent is launched (same shape as
    tests/test_verify_gate_empty_warning.py's)."""

    def __init__(self, sup: LoopSupervisor, handlers: dict):
        self.sup = sup
        self.handlers = handlers
        self.phases: list[str] = []
        self.running = False

    def interrupt(self) -> bool:
        return self.running

    async def run(self, prompt, transcript, **kw) -> IterationResult:
        phase = self.sup.run.read_status().get("phase")
        self.phases.append(phase)
        self.running = True
        try:
            return self.handlers[phase](self.sup)
        finally:
            self.running = False


def _steps(*steps):
    calls = {"n": 0}

    def handler(sup: LoopSupervisor) -> IterationResult:
        calls["n"] += 1
        return steps[min(calls["n"] - 1, len(steps) - 1)](sup)

    return handler


def _plan_doc(*specs: tuple) -> dict:
    return {"version": 1,
            "tasks": [{"id": i, "title": f"task {i}", "status": s,
                       "successCriteria": f"{i} works"} for i, s in specs]}


_supervisor = supervisor_with_runner(
    lambda sup, handlers=None: None if handlers is None else _ScriptedAgent(sup, handlers),
    run_id="v07-marker", iterations=14, max_approaches=1, vigilant=False,
    on_complete="exit", infra_retry_backoff_s=[0.0], infra_retry_backoff_max_s=0.0,
    infra_outage_budget_s=1000.0)


def _events(root: Path) -> list[dict]:
    p = root / "events.jsonl"
    if not p.exists():
        return []
    return [json.loads(x) for x in p.read_text().splitlines() if x.strip()]


def _log_messages(root: Path) -> str:
    return "\n".join(ev.get("message") or "" for ev in _events(root)
                     if ev.get("type") == "log")


def _signals(root: Path) -> list[str]:
    return [ev.get("signal") for ev in _events(root) if ev.get("type") == "signal"]


def _plan_two(sup: LoopSupervisor) -> IterationResult:
    atomic_write_json(sup.run.root / "plan-proposal.json",
                      {"version": 1, "tasks": [
                          {"id": "001", "title": "task 001", "successCriteria": "001 works"},
                          {"id": "002", "title": "task 002", "successCriteria": "002 works"},
                      ]})
    return _ok("planned two tasks\n" +
              json.dumps({"proposal_path": "plan-proposal.json",
                         "summary": "planned two tasks"}))


def _finish_one(sup: LoopSupervisor) -> IterationResult:
    doc = sup.run.read_tasks()
    for task in doc["tasks"]:
        if task["status"] != "completed":
            task["status"] = "completed"
            sup.run.write_tasks(doc)
            return _ok(f"finished {task['id']}")
    return _ok("nothing left")


def _signal_done(sup: LoopSupervisor) -> IterationResult:
    # Task 015 (requirement O, #100): the retired `<promise>COMPLETE</promise>`
    # marker's replacement -- the same whole-plan claim, now the contract
    # value `verdict: "plan_complete"`.
    return _ok("every task is terminal\n" + json.dumps({"verdict": "plan_complete"}))


def _review_verified(sup: LoopSupervisor) -> IterationResult:
    # Task 119 (#60 pillar 3, fact 9): review reports through the `review`
    # JSON contract, not the retired `<promise>VERIFIED</promise>` sentinel.
    return _ok("the PRD holds\n" + json.dumps({"verdict": "verified"}))


def _run(root: Path, worker, **cfg_kw) -> tuple[LoopSupervisor, str]:
    RunDir(root=root).update_status(state="starting")
    sup = _supervisor(root, {"planning": _plan_two, "worker": worker,
                             "review": _review_verified}, **cfg_kw)
    return sup, asyncio.run(sup._run_job_core())


async def _drive_until_paused(sup: LoopSupervisor) -> None:
    """Task 012 (requirement B.3, #153): a `_run_job_core()` that hits the
    stagnation guard on the LAST approach now pauses and escalates instead
    of ending the run `failed` -- and stays parked at `_gate()` forever
    without an operator's `resume()`, which this scripted-runner test never
    sends. Starts `_run_job_core()` as a background task, polls
    `status.json` for `state == "paused"`, then cancels the still-pending
    task so the test's own event loop can close cleanly.
    """
    task = asyncio.ensure_future(sup._run_job_core())
    for _ in range(500):
        if sup.run.read_status().get("state") == "paused":
            break
        if task.done():
            break
        await asyncio.sleep(0.01)
    else:
        task.cancel()
        raise AssertionError("run never reached `paused`")
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


def test_the_near_miss_sentence_does_not_end_the_worker_loop(tmp_path):
    """The whole point, at the level the incident happened: a worker that says
    in prose why it is NOT signalling keeps working, and the operator gets a
    line saying the marker was mentioned rather than nothing at all."""
    root = tmp_path / "run"
    sup, outcome = _run(root, _steps(lambda s: _ok(NEAR_MISS), _finish_one,
                                     _finish_one, _signal_done))
    assert sup.runner.phases.count("worker") == 4, sup.runner.phases
    assert "mentioned the completion line but did not end with it" \
        in _log_messages(root)
    assert _signals(root).count("COMPLETE") == 1, "only the earned one"
    assert (outcome, sup.run.read_status()["verdict"]) == ("succeeded", "verified")
    first = json.loads((root / "iterations" / "0002" / "meta.json").read_text())
    assert first["sawComplete"] is False, "the near-miss iteration claimed nothing"


def test_the_not_last_warning_names_what_was_seen_instead(tmp_path):
    root = tmp_path / "run"
    _run(root, _steps(lambda s: _ok(NEAR_MISS), _finish_one, _finish_one,
                      _signal_done))
    line = [ev["message"] for ev in _events(root)
            if ev.get("type") == "log"
            and "mentioned the completion line" in (ev.get("message") or "")]
    assert len(line) == 1, line
    assert "does not count as the completion marker" in line[0]
    assert "I will not emit" in line[0], "the actual last line is quoted"
    assert "(task 046, #55)" in line[0]


def test_a_review_that_mentions_the_verification_line_is_not_a_verdict(tmp_path):
    """The worse half of the defect: `saw_verified` makes a run terminal with no
    second gate behind it, so a reviewer explaining why it is NOT verifying must
    not be able to verify the run by accident."""
    root = tmp_path / "run"
    # GH #209 defect 2: the near-miss prose is what this node is about, and
    # it is unchanged -- `VERIFIED` appears in the reply but is not its last
    # line, so `mentioned_verified_not_last` still fires. What is new is the
    # compliant `review` contract payload after it. Before #209 the reply
    # was prose ONLY, and it reached the terminal `failed`/`unverified`
    # write asserted below only because an exhausted review contract ladder
    # was SCORED as a rejection; it no longer is -- a reply whose verdict
    # claim was never readable now HOLDS for an operator
    # (`review_contract_unrepaired`), so a prose-only fixture would park
    # this whole `_run_job_core()` on `_step_paused`'s gate forever. This
    # node's subject is that a reviewer explaining why it is NOT verifying
    # cannot verify by accident, so the reviewer now says its verdict the
    # way the contract asks and every assertion below is unchanged except
    # the call count, which task 049's own comment already recorded as the
    # one incidental number here.
    withheld = ("one requirement is unverifiable, so I am not emitting "
                f"`{VERIFIED}`\n"
                + json.dumps({"verdict": "rejected",
                              "reason": "one requirement is unverifiable"}))
    RunDir(root=root).update_status(state="starting")
    sup = _supervisor(root, {"planning": _plan_two,
                             "worker": _steps(_finish_one, _finish_one,
                                              _signal_done),
                             "review": _steps(lambda s: _ok(withheld))})
    outcome = asyncio.run(sup._run_job_core())
    # Task 049 (#119): before the widened `_review_contract_ladder` entry
    # (task 002 part (a)), a prose-only reply -- unparseable as a contract
    # payload -- fell straight through to `_review_verdict`'s non-verified
    # default with exactly one review call; the widened ladder then made it
    # `1 + cfg.contract_retry_max` calls, because the scripted handler
    # repeats the identical text on every re-ask. GH #209 defect 2 brings
    # the number back to one: the reply now carries a legible verdict, so
    # the ladder's own early-exit check accepts it on the first attempt and
    # never re-asks at all.
    assert sup.runner.phases.count("review") == 1
    assert "mentioned the verification line but did not end with it" \
        in _log_messages(root)
    assert "VERIFIED" not in _signals(root)
    assert (outcome, sup.run.read_status()["verdict"]) == ("failed", "unverified")


def test_an_unearned_plan_complete_is_refused_and_reaches_the_ladder(tmp_path):
    """Task 015 (requirement O, #100): a malformed/unearned `plan_complete`
    claim is a CONTRACT VIOLATION now, carried back through
    `contract_enforcement.enforce_contract`'s retry ladder -- never read as
    silent no-progress the way the retired marker's `honoured_complete`
    refusal used to be (that refusal only ever logged a warning and fell
    straight through to the ordinary stagnation bookkeeping, no ladder, no
    `contract.violation` event)."""
    calls = {"n": 0}

    def worker(sup: LoopSupervisor) -> IterationResult:
        calls["n"] += 1
        n = calls["n"]
        # calls 1-3 are this ONE pass's whole ladder: call 1 is the real
        # worker dispatch (task 001 freshly claimed `in-progress` by the
        # scheduler before it even ran), calls 2-3 are the two
        # `_ask_contract_fix` re-asks `cfg.contract_retry_max`'s default (2)
        # grants -- all three see the SAME assigned task and are refused
        # STRUCTURALLY, so the ladder exhausts and this pass counts as
        # no-progress.
        if n <= 3:
            return _signal_done(sup)
        # calls 4-5: the worker's next two real iterations, finishing 001
        # then 002 directly -- ordinary progress, no verdict at all.
        if n <= 5:
            return _finish_one(sup)
        # call 6: nothing assigned, nothing outstanding -- earned.
        return _signal_done(sup)

    root = tmp_path / "run"
    sup, outcome = _run(root, worker)
    events = _events(root)
    violations = [ev for ev in events if ev.get("type") == "contract.violation"]
    assert violations, "the unearned claim must reach the ladder"
    all_errors = " ".join(" ".join(ev.get("errors", [])) for ev in violations)
    assert "only valid when no task was assigned" in all_errors, all_errors
    assert "'001' was assigned" in all_errors, all_errors
    faults = [ev for ev in events if ev.get("type") == "contract.agent_fault"]
    assert len(faults) == 1, "the ladder exhausts exactly once, for the one refused pass"
    assert sup.runner.phases.count("worker") == 6, sup.runner.phases
    assert _signals(root).count("COMPLETE") == 1, "one earned signal, at the end"
    assert (outcome, sup.run.read_status()["verdict"]) == ("succeeded", "verified")


def test_an_earned_plan_complete_is_still_honoured(tmp_path):
    """The control: nothing about the ordinary path changes."""
    root = tmp_path / "run"
    sup, outcome = _run(root, _steps(_finish_one, _finish_one, _signal_done))
    events = _events(root)
    assert not [ev for ev in events
                if ev.get("type") in ("contract.violation", "contract.agent_fault")]
    assert not [ev for ev in events if ev.get("type") == "log"
                and "mentioned the completion" in (ev.get("message") or "")]
    assert _signals(root) == ["COMPLETE", "VERIFIED"]
    assert sup.runner.phases == ["planning", "worker", "worker", "worker", "review"]
    assert (outcome, sup.run.read_status()["verdict"]) == ("succeeded", "verified")


def test_a_skipped_task_is_completion_but_a_failed_one_is_not(tmp_path):
    """`completed`/`skipped` is exactly what `_outstanding_for_completion`
    recognises, so a justified skip must not block the `plan_complete` claim
    -- while `failed` must, which is what routes such a plan to review (#33)
    instead. Two iterations, not one: `plan_complete` is structurally
    refused whenever a task was assigned THIS iteration (task 015), so the
    skip/complete pair and the whole-plan claim can no longer share the one
    reply the retired marker let them share."""
    def skip_both(sup: LoopSupervisor) -> IterationResult:
        doc = sup.run.read_tasks()
        doc["tasks"][0]["status"] = "completed"
        doc["tasks"][1]["status"] = "skipped"
        doc["tasks"][1]["notes"] = "out of scope; not a claim the criteria hold"
        sup.run.write_tasks(doc)
        return _ok("one done, one honestly skipped")

    root = tmp_path / "run"
    sup, outcome = _run(root, _steps(skip_both, _signal_done))
    events = _events(root)
    assert not [ev for ev in events
                if ev.get("type") in ("contract.violation", "contract.agent_fault")]
    assert _signals(root) == ["COMPLETE", "VERIFIED"]
    assert (outcome, sup.run.read_status()["verdict"]) == ("succeeded", "verified")


def test_an_agent_that_claims_plan_complete_forever_still_stagnates(tmp_path):
    """The second-order bug #55 warns about, now for the contract value: once
    an unearned `plan_complete` claim is refused, the stagnation counter must
    consult the HONOURED completion (whether the ladder actually succeeded),
    or an agent that claims it every iteration while changing nothing holds
    `stagnant` at 0 and the run never ends. Three no-progress PASSES (each
    its own exhausted ladder), approach paused and escalated (task 012,
    requirement B.3: this is the default, `max_approaches=1`, LAST approach,
    so the stagnation guard no longer fails the run outright), review never
    entered."""
    root = tmp_path / "run"
    RunDir(root=root).update_status(state="starting")
    sup = _supervisor(root, {"planning": _plan_two, "worker": _steps(_signal_done),
                             "review": _review_verified})
    asyncio.run(_drive_until_paused(sup))
    msgs = _log_messages(root)
    assert "3 iterations with no task progress" in msgs, msgs
    faults = [ev for ev in _events(root) if ev.get("type") == "contract.agent_fault"]
    assert len(faults) == 3, "one exhausted ladder per stagnant pass"
    assert sup.runner.phases.count("worker") == 9, sup.runner.phases
    assert "review" not in sup.runner.phases
    status = sup.run.read_status()
    assert status.get("state") == "paused"
    assert status.get("pauseReason") == "stagnation"
    assert "COMPLETE" not in _signals(root)


def test_an_earned_plan_complete_is_progress_even_when_the_plan_did_not_change(tmp_path):
    """The other half of "consult the HONOURED completion": an honoured
    `plan_complete` claim must still end the worker loop directly, without
    ever reaching the ordinary stagnation bookkeeping at all -- the iteration
    that only claims it changes nothing by definition, so on a plan already
    finished (a resumed run, or a worker that idled first) the guard would
    otherwise reach 3 and fail the approach on the very iteration that earned
    its review."""
    def plan_all_done(sup: LoopSupervisor) -> IterationResult:
        atomic_write_json(sup.run.tasks_file,
                          _plan_doc(("001", "completed"), ("002", "completed")))
        return _ok("planned; the work was already there")

    root = tmp_path / "run"
    RunDir(root=root).update_status(state="starting")
    # Task 120 (#60): a plan whose tasks are already `completed` at planning
    # time cannot come through the real plan-proposal.json contract (a
    # task's own `status` is rejected at ingestion) -- it is exactly what a
    # RESUMED run's tasks.json legitimately looks like, so seed disk state
    # that way and skip planning entirely, the same way `_resume_point()`
    # decides a real engine process should.
    sup = _supervisor(root, {"worker": _steps(lambda s: _ok("read the notes"),
                                             lambda s: _ok("re-read the notes"),
                                             _signal_done),
                             "review": _review_verified})
    plan_all_done(sup)
    sup.iterations_used = 1
    outcome = asyncio.run(sup._run_job_core())
    msgs = _log_messages(root)
    assert "3 iterations with no task progress" not in msgs, msgs
    assert sup.runner.phases == ["worker", "worker", "worker", "review"]
    assert _signals(root) == ["COMPLETE", "VERIFIED"]
    assert (outcome, sup.run.read_status()["verdict"]) == ("succeeded", "verified")


# -- the cross-check helper and its vocabulary ------------------------------


@pytest.mark.parametrize("status, blocks", [
    ("completed", False),
    ("skipped", False),
    ("pending", True),
    ("in-progress", True),
    ("validation-failed", True),
    ("failed", True),
    ("nonsense", True),
])
def test_task_blocks_completion_reads_one_record(status, blocks):
    assert task_blocks_completion({"id": "001", "status": status}) is blocks


def test_a_record_with_no_status_at_all_blocks_completion():
    assert task_blocks_completion({"id": "001"}) is True
    assert task_blocks_completion("not even a dict") is True   # type: ignore[arg-type]


def test_the_completion_statuses_are_the_two_worker_md_recognises():
    # Task 019 (requirement F): `TASK_COMPLETION_STATUSES` now also carries
    # `validated`, the vigilant mini-FSM's own terminal a worker never
    # writes itself -- only the harness applies it, off verify's verdict.
    # worker.md (the legacy, non-vigilant agent-writes-its-own-status path
    # task 019 deliberately leaves untouched) still recognises exactly the
    # original two; that subset relationship is what this guards now.
    assert set(("completed", "skipped")) <= set(state_mod.TASK_COMPLETION_STATUSES)
    assert "validated" in state_mod.TASK_COMPLETION_STATUSES


def test_the_outstanding_list_is_the_plan_order_of_everything_unfinished(tmp_path):
    sup = _supervisor(tmp_path / "run")
    doc = _plan_doc(("001", "completed"), ("002", "failed"),
                    ("003", "skipped"), ("004", "in-progress"))
    assert [t["id"] for t in sup._outstanding_for_completion(doc)] == ["002", "004"]
    assert sup._outstanding_for_completion(
        _plan_doc(("001", "completed"), ("002", "skipped"))) == []


def test_an_empty_or_unreadable_plan_is_not_refused(tmp_path):
    """Refusing here would strand such a run in the stagnation guard with no
    reviewer ever looking at it; review re-derives everything from the PRD and
    is the right gate."""
    sup = _supervisor(tmp_path / "run")
    assert sup._outstanding_for_completion({}) == []
    assert sup._outstanding_for_completion({"version": 1, "tasks": []}) == []


# -- the prompts ------------------------------------------------------------


def _prompt(name: str) -> str:
    return (REPO / "src" / "ralphd" / "prompts" / f"{name}.md").read_text()


def test_the_worker_prompt_no_longer_asks_for_a_literal_marker():
    """Task 015 (requirement O, #100): the literal `<promise>COMPLETE</promise>`
    line, and the naming convention that existed only to let an agent talk
    ABOUT it without reproducing it, are both gone -- there is no longer a
    substring-mention hazard for either to defend against."""
    text = _prompt("worker")
    assert "<promise>COMPLETE</promise>" not in text
    assert "the completion marker" not in text
    assert "Naming the signal in prose" not in text


def test_the_worker_prompt_documents_the_plan_complete_verdict():
    text = " ".join(_prompt("worker").split())
    assert '{"verdict": "plan_complete"}' in text
    assert ("nothing was selectable" in text
            and "ALREADY reached a terminal status" in text)
    assert "cross-checks the claim against" in text


def test_the_worker_prompt_still_warns_the_plan_complete_claim_is_refused_not_silent():
    text = " ".join(_prompt("worker").split())
    assert "is refused rather than silently accepted" in text


# -- the docs ---------------------------------------------------------------


@pytest.mark.parametrize("path, needles", [
    ("SPEC.md", [
        "**A promise counts only as the last line, and only when the plan agrees.**",
        "**Last non-blank line, stripped, equal to the marker**",
        "`runner.last_nonblank_line()`",
        "**absent**",
        "`MULTILINE ^…$`) match",
        "**A COMPLETE is cross-checked against `tasks.json` before it is honoured.**",
        "`_outstanding_for_completion()`",
        "`state.TASK_COMPLETION_STATUSES`, `task_blocks_completion()`",
        "The stagnation counter (§5.3)",
        "| `sawComplete` / `sawVerified` | bool | the marker was the final "
        "non-blank line of the final assistant text (§4.3) |",
    ]),
    ("docs/architecture.md", [
        "count **only as the final non-blank line**",
        'mentioning one anywhere else is logged as "mentioned but did not end '
        'with it".',
        "any task not `completed`/`skipped` refuses it and the worker loop "
        "keeps going (SPEC §4.3)",
    ]),
])
def test_the_docs_state_the_two_rules(path, needles):
    text = " ".join((REPO / path).read_text().split())
    for needle in needles:
        assert needle in text, f"{path}: {needle!r}"
