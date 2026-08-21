"""Task 012 (#45): an interrupted or timed-out verify renders no verdict.

`_verify_task` used to ask "was this attempt a verdict?" with
`result.error_message` alone -- pi's own in-band error. Every other way an
attempt can end without a verifier ever finishing (the engine's SIGINT
handling -> `interrupted`, the full `iteration_timeout_s` -> `timed_out`,
the startup-window watchdog -> `no_traffic_timeout`) records no
`error_message` at all, so it fell through to the verdict-miss bookkeeping:
`status` forced to `validation-failed`, a validation attempt burned, and
`validationNotes` reading "Verifier did not emit the task-verified
sentinel." -- true of the bytes on disk and thoroughly misleading, since no
verifier ever read the criteria. Three of those and the task is `failed`
forever because of the engine's own timeouts.

The rule under test: absence of a verdict is not a negative verdict.
Whatever ended the attempt, `status`, `validationAttempts` and
`validationNotes` come out byte-for-byte as they went in, and any note
written on such a path says the verifier never reached a verdict rather than
quoting the missing sentinel.

Mutation case (recorded in the commit message): restoring the
`error_message`-only condition in loop.py -- i.e. `no_verdict =
"errored out" if result.error_message else ""` in `_verify_task`, or
dropping the `timed_out`/`interrupted`/`no_traffic_timeout` branches of
`_verify_no_verdict` -- makes
test_an_engine_ended_verify_leaves_the_task_byte_for_byte_unchanged,
test_an_engine_ended_verify_writes_no_misleading_sentinel_note and
test_an_engine_ended_verify_attempt_is_recorded_as_error_not_fail and
test_an_engine_ended_verify_is_retried_without_consuming_an_attempt fail.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ralphd.engine.config import JobConfig
from ralphd.engine.loop import LoopSupervisor
from ralphd.engine.runner import IterationResult
from ralphd.engine.state import RunDir, atomic_write_json


# -- scaffolding -----------------------------------------------------------

TASK = {
    "id": "007",
    "title": "the task under verification",
    "status": "completed",
    "successCriteria": "the thing works",
    "validationAttempts": 1,
    "validationNotes": "first round: the CLI flag was missing (worker's note)",
}


class _FixedRunner:
    """Stands in for PiRunner: every iteration returns the same shape."""

    def __init__(self, make_result):
        self.make_result = make_result
        self.calls = 0
        self.running = False

    def interrupt(self) -> bool:
        return self.running

    async def run(self, prompt, transcript, **kw) -> IterationResult:
        self.calls += 1
        return self.make_result()


def _supervisor(tmp_path: Path, make_result, **cfg_kw) -> LoopSupervisor:
    run = RunDir(root=tmp_path)
    kw = {"iterations": 8, "max_approaches": 1, "vigilant": True,
          "on_complete": "exit", "infra_retry_backoff_s": [0.0],
          "infra_retry_backoff_max_s": 0.0, "infra_outage_budget_s": 0.0,
          **cfg_kw}
    sup = LoopSupervisor(JobConfig(run_id="unit", **kw), run, tmp_path)
    sup.runner = _FixedRunner(make_result)      # type: ignore[assignment]

    async def no_backoff(seconds):
        return seconds, False

    sup._wait_out_backoff = no_backoff          # type: ignore[method-assign]
    atomic_write_json(run.tasks_file, {"version": 1, "tasks": [dict(TASK)]})
    return sup


def _events(run_dir: Path) -> list[dict]:
    p = run_dir / "events.jsonl"
    if not p.exists():
        return []
    return [json.loads(x) for x in p.read_text().splitlines() if x.strip()]


def _messages(run_dir: Path) -> str:
    return "\n".join(ev.get("message") or "" for ev in _events(run_dir)
                     if ev.get("type") == "log")


def _verify_metas(run_dir: Path) -> list[dict]:
    return [json.loads((d / "meta.json").read_text())
            for d in sorted((run_dir / "iterations").iterdir())
            if (d / "meta.json").exists()
            and json.loads((d / "meta.json").read_text()).get("phase") == "verify"]


# The four ways one verify attempt can end without a verifier ever reaching
# a verdict. `traffic` mirrors reality: an engine-side kill of an agent that
# HAD been talking to the model (so it classifies "work", not "infra", and
# comes straight back to _verify_task) vs. the watchdog case, which is
# infra by construction.
def _interrupted() -> IterationResult:
    r = IterationResult(exit_code=-2, interrupted=True, duration_s=30.0)
    r.final_text = "reading the criteria now"
    r.usage = {"input": 10, "output": 5, "totalTokens": 15}
    return r


def _timed_out() -> IterationResult:
    r = IterationResult(exit_code=None, timed_out=True, duration_s=2700.0)
    r.final_text = "still checking the second criterion"
    r.usage = {"input": 10, "output": 5, "totalTokens": 15}
    return r


def _no_traffic() -> IterationResult:
    return IterationResult(exit_code=-9, no_traffic_timeout=True,
                           duration_s=60.0)


def _errored() -> IterationResult:
    return IterationResult(exit_code=0, duration_s=12.0,
                           error_message="Connection error.")


NO_VERDICT_SHAPES = {
    "interrupted": _interrupted,
    "timed-out": _timed_out,
    "startup-watchdog": _no_traffic,
    "in-band-error": _errored,
}


# -- the predicate itself --------------------------------------------------


def test_a_finished_verifier_reached_a_verdict():
    """The pass and the genuine-miss cases must NOT be read as "no verdict"
    -- otherwise a real negative verdict would stop being recorded."""
    ok = IterationResult(exit_code=0, duration_s=42.0)
    ok.final_text = "<task-verified>007</task-verified>"
    assert LoopSupervisor._verify_no_verdict(ok) == ""

    miss = IterationResult(exit_code=0, duration_s=42.0)
    miss.final_text = "criterion 2 does not hold: no test asserts it"
    assert LoopSupervisor._verify_no_verdict(miss) == ""


@pytest.mark.parametrize("shape", list(NO_VERDICT_SHAPES),
                         ids=list(NO_VERDICT_SHAPES))
def test_every_engine_ended_shape_is_no_verdict(shape):
    why = LoopSupervisor._verify_no_verdict(NO_VERDICT_SHAPES[shape]())
    assert why, f"{shape} must be recognised as producing no verdict"
    assert "sentinel" not in why.lower(), \
        "a no-verdict reason must not be phrased as a missing sentinel"


# -- _verify_task on a no-verdict attempt ----------------------------------


@pytest.fixture(params=list(NO_VERDICT_SHAPES), ids=list(NO_VERDICT_SHAPES))
def no_verdict_verify(request, tmp_path):
    """One `_verify_task` call whose every attempt ends without a verdict,
    plus the exact bytes of tasks.json from before it ran."""
    import asyncio

    sup = _supervisor(tmp_path, NO_VERDICT_SHAPES[request.param])
    before = sup.run.tasks_file.read_bytes()
    verified = asyncio.run(sup._verify_task(dict(TASK)))
    return sup, before, verified, request.param


def test_an_engine_ended_verify_leaves_the_task_byte_for_byte_unchanged(
        no_verdict_verify):
    sup, before, verified, shape = no_verdict_verify

    assert verified is False, "no verdict is not a pass"
    after = sup.run.tasks_file.read_bytes()
    assert after == before, f"{shape} rewrote tasks.json"

    t = json.loads(after)["tasks"][0]
    assert t["status"] == TASK["status"]
    assert t["validationAttempts"] == TASK["validationAttempts"]
    assert t["validationNotes"] == TASK["validationNotes"]


def test_an_engine_ended_verify_does_not_mark_the_task_verified(
        no_verdict_verify):
    sup, _before, _verified, _shape = no_verdict_verify
    record = sup.run.root / "vigilant-verified.json"
    verified_ids = json.loads(record.read_text()) if record.exists() else []
    assert "007" not in json.dumps(verified_ids)
    assert not [ev for ev in _events(sup.run.root)
                if ev.get("signal") == "taskVerified"]


def test_an_engine_ended_verify_writes_no_misleading_sentinel_note(
        no_verdict_verify):
    """The record must distinguish "the verifier reached a negative verdict"
    from "the verifier never reached a verdict" -- so the sentinel wording
    belongs only to the former."""
    sup, _before, _verified, shape = no_verdict_verify
    msgs = _messages(sup.run.root)

    assert "did not emit the task-verified sentinel" not in msgs, shape
    assert "not a validation failure" in msgs, shape
    assert ("never reached a verdict" in msgs
            or "kept erroring" in msgs), msgs
    assert ("validationNotes" in msgs and "unchanged" in msgs), \
        "the log says which fields were deliberately left alone"


def test_an_engine_ended_verify_attempt_is_recorded_as_error_not_fail(
        no_verdict_verify):
    sup, _before, _verified, shape = no_verdict_verify
    metas = _verify_metas(sup.run.root)
    assert metas, "at least one verify iteration was recorded"
    for meta in metas:
        assert meta.get("verifiedTask") == "007"
        assert meta.get("verifyOutcome") == "error", \
            f"{shape}: no verdict must not be recorded as a `fail` verdict"


@pytest.mark.parametrize("shape", ["interrupted", "timed-out"])
def test_an_engine_ended_verify_is_retried_without_consuming_an_attempt(
        tmp_path, shape):
    """A no-verdict attempt gets the same bounded retry an in-band error gets
    -- otherwise one engine-side timeout silently abandons verification of a
    completed task (in vigilant mode, blocking the run's verdict) instead of
    trying again."""
    import asyncio

    sup = _supervisor(tmp_path, NO_VERDICT_SHAPES[shape])
    assert asyncio.run(sup._verify_task(dict(TASK))) is False

    assert sup.runner.calls == 1 + LoopSupervisor.MAX_VERIFY_ERROR_RETRIES, \
        "the attempt is retried, bounded by MAX_VERIFY_ERROR_RETRIES"
    msgs = _messages(sup.run.root)
    assert (f"retrying verification "
            f"(1/{LoopSupervisor.MAX_VERIFY_ERROR_RETRIES})") in msgs, msgs
    assert "without consuming a validation attempt" in msgs
    t = json.loads(sup.run.tasks_file.read_text())["tasks"][0]
    assert t["validationAttempts"] == TASK["validationAttempts"]


# -- the other side of the line: a real verdict still counts ---------------


def test_a_real_verdict_miss_still_records_a_validation_failure(tmp_path):
    import asyncio

    def miss() -> IterationResult:
        r = IterationResult(exit_code=0, duration_s=42.0)
        r.final_text = "criterion 2 does not hold: no test asserts it"
        r.usage = {"input": 10, "output": 5, "totalTokens": 15}
        return r

    sup = _supervisor(tmp_path, miss)
    task = dict(TASK)
    task["validationNotes"] = ""
    atomic_write_json(sup.run.tasks_file, {"version": 1, "tasks": [task]})

    assert asyncio.run(sup._verify_task(dict(task))) is False

    t = json.loads(sup.run.tasks_file.read_text())["tasks"][0]
    assert t["status"] == "validation-failed"
    assert t["validationAttempts"] == TASK["validationAttempts"] + 1
    notes = t["validationNotes"]
    assert "sentinel" in notes
    assert "verdict" in notes.lower(), \
        "the default note says a verifier ran and reached a verdict"
    assert _verify_metas(sup.run.root)[-1]["verifyOutcome"] == "fail"


def test_no_verify_iteration_at_all_records_nothing(tmp_path):
    """Budget gone before the first attempt: nothing was observed about the
    task, so nothing is recorded against it (and no verdict is faked)."""
    import asyncio

    sup = _supervisor(tmp_path, _timed_out, iterations=0)
    before = sup.run.tasks_file.read_bytes()

    assert asyncio.run(sup._verify_task(dict(TASK))) is False

    assert sup.run.tasks_file.read_bytes() == before
    assert not (sup.run.root / "iterations").exists() or not _verify_metas(
        sup.run.root)
    msgs = _messages(sup.run.root)
    assert "no verify iteration ran" in msgs
    assert "not a validation failure" in msgs


# -- the documented claim --------------------------------------------------

ROOT = Path(__file__).resolve().parent.parent


def _section(text: str, start: str, stop: str) -> str:
    i = text.index(start)
    return text[i:text.index(stop, i)]


def test_spec_documents_no_verdict_rather_than_errored_out():
    """SPEC §8's task-failure bookkeeping table is what an operator reads to
    know whether a timeout can cost a task a validation attempt."""
    spec = _section((ROOT / "SPEC.md").read_text(),
                    "Task-failure bookkeeping",
                    "Independently of vigilant mode")
    assert "no verdict at all" in spec
    for field in ("timed_out", "no_traffic_timeout", "interrupted",
                  "error_message"):
        assert f"`{field}`" in spec, f"SPEC omits the {field} shape"
    assert "byte-for-byte" in spec
    assert "validationNotes" in spec
    assert "no verify iteration ran at all" in spec


def test_architecture_documents_the_generalisation_and_the_two_outcomes():
    arch = _section((ROOT / "docs" / "architecture.md").read_text(),
                    "A verify iteration that errors out mid-stream",
                    "### Criteria fingerprinting")
    assert "_verify_no_verdict" in arch, \
        "the doc names the one place the decision is made"
    for field in ("timed_out", "no_traffic_timeout", "interrupted"):
        assert f"`{field}`" in arch, f"architecture.md omits the {field} shape"
    assert "byte-for-byte" in arch
    # The distinction the note wording has to carry, documented as such.
    assert "never reached a verdict" in arch
    assert "ran to completion" in arch
