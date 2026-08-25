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

Three layers, tested here in that order:

1. **String.** The marker must be the last non-blank line, stripped, equal --
   `runner.last_nonblank_line()`. Deliberately not an "own line anywhere"
   (`MULTILINE ^...$`) match, which a fenced block or an echoed notes.md
   section defeats. A marker seen elsewhere is absent AND logged, so failing
   closed does not look like an unexplained non-conclusion.
2. **Plan.** A COMPLETE is honoured only after the freshly re-read plan is
   checked: anything not `completed`/`skipped` refuses it, names the
   outstanding count in a log event, and the worker loop keeps working. This
   layer does not care how the marker was formatted, so it stops the whole
   class rather than one sentence.
3. **Stagnation.** The counter consults the HONOURED completion. Reading the
   raw marker there would trade the false conclusion for an infinite loop: an
   agent emitting COMPLETE every iteration while changing nothing would hold
   `stagnant` at 0 forever. test_an_agent_that_emits_complete_forever_still_
   stagnates is that test.

Plus the prompt half: an agent has to be able to NAME the signal to explain
withholding it, and if the only name it has is the literal line it will use the
literal line. So `worker.md` supplies "the completion marker" and `review.md`
"the verification marker", and both forbid reproducing the line anywhere but at
the very end.

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


def _supervisor(root: Path, handlers: dict | None = None, **cfg_kw) -> LoopSupervisor:
    root.mkdir(parents=True, exist_ok=True)
    run = RunDir(root=root)
    kw = {"iterations": 14, "max_approaches": 1, "vigilant": False,
          "on_complete": "exit", "infra_retry_backoff_s": [0.0],
          "infra_retry_backoff_max_s": 0.0, "infra_outage_budget_s": 1000.0,
          **cfg_kw}
    sup = LoopSupervisor(JobConfig(run_id="v07-marker", **kw), run, root)
    if handlers is not None:
        sup.runner = _ScriptedAgent(sup, handlers)   # type: ignore[assignment]

    async def no_backoff(seconds):
        return seconds, False

    sup._wait_out_backoff = no_backoff              # type: ignore[method-assign]
    return sup


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
    atomic_write_json(sup.run.tasks_file,
                      _plan_doc(("001", "pending"), ("002", "pending")))
    return _ok("planned two tasks")


def _finish_one(sup: LoopSupervisor) -> IterationResult:
    doc = sup.run.read_tasks()
    for task in doc["tasks"]:
        if task["status"] != "completed":
            task["status"] = "completed"
            atomic_write_json(sup.run.tasks_file, doc)
            return _ok(f"finished {task['id']}")
    return _ok("nothing left")


def _signal_done(sup: LoopSupervisor) -> IterationResult:
    return _ok(f"every task is terminal\n{COMPLETE}")


def _review_verified(sup: LoopSupervisor) -> IterationResult:
    return _ok(f"the PRD holds\n{VERIFIED}")


def _run(root: Path, worker, **cfg_kw) -> tuple[LoopSupervisor, str]:
    RunDir(root=root).update_status(state="starting")
    sup = _supervisor(root, {"planning": _plan_two, "worker": worker,
                             "review": _review_verified}, **cfg_kw)
    return sup, asyncio.run(sup._run_job_core())


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
    withheld = ("one requirement is unverifiable, so I am not emitting "
                f"`{VERIFIED}`")
    RunDir(root=root).update_status(state="starting")
    sup = _supervisor(root, {"planning": _plan_two,
                             "worker": _steps(_finish_one, _finish_one,
                                              _signal_done),
                             "review": _steps(lambda s: _ok(withheld))})
    outcome = asyncio.run(sup._run_job_core())
    assert sup.runner.phases.count("review") == 1
    assert "mentioned the verification line but did not end with it" \
        in _log_messages(root)
    assert "VERIFIED" not in _signals(root)
    assert (outcome, sup.run.read_status()["verdict"]) == ("failed", "unverified")


def test_an_unearned_complete_is_refused_and_the_loop_keeps_working(tmp_path):
    """Layer 2, and the layer that does not depend on formatting: the marker is
    in the only place that counts, and the plan still says otherwise."""
    root = tmp_path / "run"
    sup, outcome = _run(root, _steps(_signal_done, _finish_one, _finish_one,
                                     _signal_done))
    msgs = _log_messages(root)
    assert "signalled COMPLETE with 2 task(s) not completed or skipped" in msgs
    # Task 116: the harness itself claims task 001 `in-progress` (the
    # scheduler's own pick, persisted strictly before this iteration's
    # subprocess was spawned) before the scripted handler ever runs --
    # so the very first refused-COMPLETE snapshot already shows it moved
    # off `pending`, unlike task 002 which the scheduler never touched.
    assert "001 (in-progress), 002 (pending)" in msgs
    assert "refusing the signal and continuing the worker loop (task 046, #55)" in msgs
    assert sup.runner.phases.count("worker") == 4, sup.runner.phases
    assert _signals(root).count("COMPLETE") == 1, "one earned signal, at the end"
    assert (outcome, sup.run.read_status()["verdict"]) == ("succeeded", "verified")


def test_an_earned_complete_is_still_honoured(tmp_path):
    """The control: nothing about the ordinary path changes."""
    root = tmp_path / "run"
    sup, outcome = _run(root, _steps(_finish_one, _finish_one, _signal_done))
    msgs = _log_messages(root)
    assert "refusing the signal" not in msgs and "mentioned the completion" not in msgs
    assert _signals(root) == ["COMPLETE", "VERIFIED"]
    assert sup.runner.phases == ["planning", "worker", "worker", "worker", "review"]
    assert (outcome, sup.run.read_status()["verdict"]) == ("succeeded", "verified")


def test_a_skipped_task_is_completion_but_a_failed_one_is_not(tmp_path):
    """`completed`/`skipped` is exactly what worker.md's completion signal
    recognises, so a justified skip must not block the run -- while `failed`
    must, which is what routes such a plan to review (#33) instead."""
    def skip_and_signal(sup: LoopSupervisor) -> IterationResult:
        doc = sup.run.read_tasks()
        doc["tasks"][0]["status"] = "completed"
        doc["tasks"][1]["status"] = "skipped"
        doc["tasks"][1]["notes"] = "out of scope; not a claim the criteria hold"
        atomic_write_json(sup.run.tasks_file, doc)
        return _ok(f"one done, one honestly skipped\n{COMPLETE}")

    root = tmp_path / "run"
    sup, outcome = _run(root, _steps(skip_and_signal))
    assert "refusing the signal" not in _log_messages(root)
    assert _signals(root) == ["COMPLETE", "VERIFIED"]
    assert (outcome, sup.run.read_status()["verdict"]) == ("succeeded", "verified")


def test_an_agent_that_emits_complete_forever_still_stagnates(tmp_path):
    """The second-order bug #55 warns about: once an unearned COMPLETE is
    ignored, the stagnation counter must consult the HONOURED completion, or an
    agent that signals every iteration while changing nothing holds `stagnant`
    at 0 and the run never ends. Three no-progress iterations, approach failed,
    review never entered."""
    root = tmp_path / "run"
    sup, outcome = _run(root, _steps(_signal_done))
    msgs = _log_messages(root)
    assert "3 iterations with no task progress" in msgs, msgs
    assert msgs.count("refusing the signal and continuing the worker loop"
                      " (task 046, #55)") == 3
    assert sup.runner.phases == ["planning", "worker", "worker", "worker"]
    assert "review" not in sup.runner.phases
    assert (outcome, sup.run.read_status()["verdict"]) == ("failed", "unverified")
    assert "COMPLETE" not in _signals(root)


def test_an_earned_complete_is_progress_even_when_the_plan_did_not_change(tmp_path):
    """The other half of "consult the HONOURED completion": an honoured COMPLETE
    must still reset the stagnation counter. The iteration that only signals
    changes nothing by definition, so on a plan already finished (a resumed run,
    or a worker that idled first) the guard would otherwise reach 3 and fail the
    approach on the very iteration that earned its review."""
    def plan_all_done(sup: LoopSupervisor) -> IterationResult:
        atomic_write_json(sup.run.tasks_file,
                          _plan_doc(("001", "completed"), ("002", "completed")))
        return _ok("planned; the work was already there")

    root = tmp_path / "run"
    RunDir(root=root).update_status(state="starting")
    sup = _supervisor(root, {"planning": plan_all_done,
                             "worker": _steps(lambda s: _ok("read the notes"),
                                              lambda s: _ok("re-read the notes"),
                                              _signal_done),
                             "review": _review_verified})
    outcome = asyncio.run(sup._run_job_core())
    msgs = _log_messages(root)
    assert "3 iterations with no task progress" not in msgs, msgs
    assert sup.runner.phases == ["planning", "worker", "worker", "worker", "review"]
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


@pytest.mark.parametrize("prompt, phrase", [("worker", "the completion marker"),
                                            ("review", "the verification marker")])
def test_each_prompt_supplies_the_prose_name_for_its_signal(prompt, phrase):
    """Prohibiting the literal is not enough on its own: the agent has to name
    the thing to explain withholding it, and if the only name it has is the
    literal it will use the literal."""
    text = _prompt(prompt)
    assert f'call it "{phrase}"' in text
    assert f'write "{phrase}"' in text


@pytest.mark.parametrize("prompt", ["worker", "review"])
def test_each_prompt_confines_the_literal_line_to_the_very_end(prompt):
    text = " ".join(_prompt(prompt).split())
    assert ("has exactly one legitimate place in your reply — the very end, as "
            "the final line, with nothing after it") in text
    assert "Anywhere else it is a formatting error" in text
    assert "do not reproduce the literal line" in text


@pytest.mark.parametrize("prompt", ["worker", "review"])
def test_each_prompt_names_the_quotation_shapes_that_do_not_count(prompt):
    text = " ".join(_prompt(prompt).split())
    assert "including inside quotes, a fenced block, a bullet" in text


def test_the_worker_prompt_shows_the_withholding_sentence_to_write_instead():
    assert ('write "tasks remain, so I am not emitting the completion marker"'
            in " ".join(_prompt("worker").split()))


def test_the_review_prompt_shows_the_withholding_sentence_to_write_instead():
    assert ("so I am not emitting the verification marker"
            in " ".join(_prompt("review").split()))


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
