"""Task 018 (#34): a steering note is consumed on iteration SUCCESS, not on
delivery.

Until this task the engine appended a steering file's name to
`steering/.consumed.json` at the *start* of the iteration whose prompt carried
it (`_run_iteration_once`, right after the first `meta.json` write). That is
at-most-once *delivery*: if the iteration then failed, was interrupted, timed
out or died with the engine, the note was already recorded as `applied` --
`GET /steering`, `ralphctl steer --list` and the hub all said the loop had
actioned an instruction that nothing ever read, and no later iteration would
ever be handed it again.

The rule is now at-least-once delivery, at-most-once application: the marker is
appended only once the iteration finished cleanly (`faultClass is None`), and
the ordering that implements it is build the prompt -> run the agent -> record
the outcome -> append to `.consumed.json`. Re-delivery is safe because
`RunDir.consume_steering` skips names already in the marker, so neither the
`applied` state nor the `steering.consumed` event can be doubled.

Tiers here:

* `LoopSupervisor._run_iteration_once` over a real run dir with a scripted
  runner (no subprocess, milliseconds): the clean case consumes exactly once,
  each dying shape (work error / interrupted / timed out / a cancellation
  escaping the runner) leaves the note pending, and the following iteration
  applies it;
* `RunDir.consume_steering` itself, for the idempotence the guarantee rests on;
* the two operator surfaces the state is read through -- the engine's live
  `GET /steering` over real ASGI, and `ralphctl steer --list` against the same
  run dir with no container at all (the on-disk snapshot path).
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
import pytest
from test_cli_docker import Ctl, ctl  # noqa: F401  (ctl is a fixture)
from test_cli_resume import _seed_run

from ralphd.engine.api import create_app
from ralphd.engine.config import JobConfig
from ralphd.engine.loop import LoopSupervisor
from ralphd.engine.runner import IterationResult
from ralphd.engine.state import (STEERING_APPLIED, STEERING_CONSUMED_FILE,
                                 STEERING_PENDING, RunDir, steering_entries)

NOTE = "Stop rewriting the tests; fix the runner instead.\n"


# -- scaffolding -----------------------------------------------------------


class _ScriptedRunner:
    """Stands in for PiRunner at the seam `_run_iteration_once` awaits.

    Each entry of `script` is an IterationResult factory; a callable that
    raises stands in for an explosion inside the runner's own plumbing. The
    last entry repeats forever.
    """

    def __init__(self, script: list):
        self.script = script
        self.calls = 0
        self.running = False

    def interrupt(self) -> bool:
        return self.running

    async def run(self, prompt, transcript, **kw) -> IterationResult:
        self.calls += 1
        self.prompts = getattr(self, "prompts", [])
        self.prompts.append(prompt)
        self.running = True
        try:
            return self.script[min(self.calls - 1, len(self.script) - 1)]()
        finally:
            self.running = False


def _result(**kw) -> IterationResult:
    """An attempt that reached the model (traffic + a plausible duration), so
    every shape below differs only in how it ENDED."""
    r = IterationResult(exit_code=kw.pop("exit_code", 0))
    r.final_text = "did some work"
    r.duration_s = 300.0
    r.usage = {"input": 10, "output": 5, "totalTokens": 15}
    for k, v in kw.items():
        setattr(r, k, v)
    return r


def _ok() -> IterationResult:
    return _result()


def _work_error() -> IterationResult:
    return _result(exit_code=1, error_message="the agent reported an error")


def _interrupted() -> IterationResult:
    return _result(exit_code=-2, interrupted=True)


def _timed_out() -> IterationResult:
    return _result(exit_code=-9, timed_out=True, interrupted=True)


def _exploded() -> IterationResult:
    raise asyncio.CancelledError("stray cancellation from the agent plumbing")


DYING = {"work error": _work_error, "interrupted": _interrupted,
         "timed out": _timed_out, "engine explosion": _exploded}


def _supervisor(root: Path, script: list) -> LoopSupervisor:
    root.mkdir(parents=True, exist_ok=True)
    run = RunDir(root=root)
    sup = LoopSupervisor(
        JobConfig(run_id="unit", iterations=8, max_approaches=1,
                  vigilant=False, on_complete="exit"),
        run, root)
    sup.runner = _ScriptedRunner(script)   # type: ignore[assignment]
    return sup


def _consumed_marker(root: Path) -> list:
    p = root / "steering" / STEERING_CONSUMED_FILE
    return json.loads(p.read_text()) if p.exists() else []


def _events(root: Path, type_: str) -> list[dict]:
    log = root / "events.jsonl"
    lines = log.read_text().splitlines() if log.exists() else []
    return [ev for ev in (json.loads(x) for x in lines) if ev.get("type") == type_]


def _metas(root: Path) -> list[dict]:
    return [json.loads((d / "meta.json").read_text())
            for d in sorted((root / "iterations").iterdir())
            if (d / "meta.json").exists()]


def _states(root: Path) -> dict[str, str]:
    return {e["file"]: e["state"] for e in steering_entries(root, bodies=False)}


# -- the clean case: consumed exactly once ---------------------------------


def test_a_clean_worker_iteration_consumes_its_note_exactly_once(tmp_path):
    sup = _supervisor(tmp_path, [_ok])
    fname = sup.run.add_steering(NOTE, "focus")

    asyncio.run(sup._run_iteration_once("worker"))

    assert _consumed_marker(tmp_path) == [fname]
    assert _states(tmp_path) == {fname: STEERING_APPLIED}
    assert [(ev["file"], ev["iteration"])
            for ev in _events(tmp_path, "steering.consumed")] == [(fname, 1)]
    meta = _metas(tmp_path)[-1]
    assert meta["steeringDelivered"] == [fname]
    assert meta["steeringConsumed"] == [fname]
    # ... and it really was handed to the agent, not just book-kept
    assert NOTE.strip() in sup.runner.prompts[-1]        # type: ignore[attr-defined]


# -- every dying shape leaves the note pending -----------------------------


@pytest.mark.parametrize("shape", sorted(DYING))
def test_a_dying_iteration_leaves_its_note_pending(tmp_path, shape):
    """THE defect: an iteration that never finished must not bank the note."""
    sup = _supervisor(tmp_path / shape.replace(" ", "-"), [DYING[shape]])
    root = sup.run.root
    fname = sup.run.add_steering(NOTE, "focus")

    asyncio.run(sup._run_iteration_once("worker"))

    assert _consumed_marker(root) == [], f"{shape}: the note was banked anyway"
    assert _states(root) == {fname: STEERING_PENDING}
    assert _events(root, "steering.consumed") == []
    assert sup.run.pending_steering() == [root / "steering" / fname]
    meta = _metas(root)[-1]
    assert meta["faultClass"] is not None, "this shape must be a failure"
    assert meta["steeringDelivered"] == [fname], "it WAS delivered"
    assert meta["steeringConsumed"] == [], "but it was not earned"


@pytest.mark.parametrize("shape", sorted(DYING))
def test_the_following_iteration_is_handed_the_note_again_and_applies_it(
        tmp_path, shape):
    sup = _supervisor(tmp_path / shape.replace(" ", "-"), [DYING[shape], _ok])
    root = sup.run.root
    fname = sup.run.add_steering(NOTE, "focus")

    asyncio.run(sup._run_iteration_once("worker"))
    asyncio.run(sup._run_iteration_once("worker"))

    prompts = sup.runner.prompts                        # type: ignore[attr-defined]
    if shape != "engine explosion":                     # the explosion ate its own
        assert NOTE.strip() in prompts[0]
    assert NOTE.strip() in prompts[-1], "redelivered to the next iteration"
    assert _consumed_marker(root) == [fname]
    assert _states(root) == {fname: STEERING_APPLIED}
    # exactly once, and credited to the iteration that earned it
    assert [(ev["file"], ev["iteration"])
            for ev in _events(root, "steering.consumed")] == [(fname, 2)]
    first, second = _metas(root)
    assert (first["steeringDelivered"], first["steeringConsumed"]) == ([fname], [])
    assert (second["steeringDelivered"], second["steeringConsumed"]) \
        == ([fname], [fname])


def test_a_note_that_arrives_mid_iteration_is_not_consumed_by_it(tmp_path):
    """Only what the prompt actually CARRIED can be earned: a note posted
    while the agent was already running was never delivered to it, so a clean
    finish must leave it pending for the next iteration (POST /steering is
    accepted at any moment, `202`)."""
    sup = _supervisor(tmp_path, [])
    early = sup.run.add_steering(NOTE, "early")
    late: list[str] = []

    def mid_flight() -> IterationResult:
        late.append(sup.run.add_steering("and one more thing\n", "late"))
        return _ok()

    sup.runner.script = [mid_flight]                    # type: ignore[attr-defined]

    asyncio.run(sup._run_iteration_once("worker"))

    (later,) = late
    assert _consumed_marker(tmp_path) == [early]
    assert _states(tmp_path) == {early: STEERING_APPLIED, later: STEERING_PENDING}
    meta = _metas(tmp_path)[-1]
    assert (meta["steeringDelivered"], meta["steeringConsumed"]) \
        == ([early], [early])
    assert [p.name for p in sup.run.pending_steering()] == [later]


def test_the_in_flight_record_never_claims_a_note_is_already_applied(tmp_path):
    """The crash-safety half of the ordering: while the agent runs, the
    iteration's own meta.json must agree with `.consumed.json` -- delivered,
    not applied -- so an engine killed mid-iteration cannot leave a record
    claiming a note was actioned."""
    sup = _supervisor(tmp_path, [])
    fname = sup.run.add_steering(NOTE, "focus")
    seen: list[dict] = []

    def mid_flight() -> IterationResult:
        seen.append(json.loads(
            (sup.run.iteration_dir(1) / "meta.json").read_text()))
        return _ok()

    sup.runner.script = [mid_flight]                    # type: ignore[attr-defined]

    asyncio.run(sup._run_iteration_once("worker"))

    (in_flight,) = seen
    assert in_flight["steeringDelivered"] == [fname]
    assert in_flight["steeringConsumed"] == [], \
        "the record claimed the note was applied before the agent finished"
    assert _consumed_marker(tmp_path) == [fname], "and it IS applied at the end"


def test_a_pending_note_is_reported_as_such_while_it_waits(tmp_path):
    """The operator is told, at the moment it happens, that the note survived
    its iteration -- not left to diff .consumed.json by hand."""
    sup = _supervisor(tmp_path, [_work_error])
    fname = sup.run.add_steering(NOTE, "focus")

    asyncio.run(sup._run_iteration_once("worker"))

    said = [ev["message"] for ev in _events(tmp_path, "log")
            if fname in ev.get("message", "") and "pending" in ev["message"]]
    assert said, [ev.get("message") for ev in _events(tmp_path, "log")]


# -- the two non-changes ---------------------------------------------------


def test_a_non_actionable_phase_still_never_consumes_even_when_it_succeeds(
        tmp_path):
    """The older rule (STEERING_ACTIONABLE_PHASES) is untouched: a clean
    `review` iteration is a success and still must not bank the note."""
    sup = _supervisor(tmp_path, [_ok])
    fname = sup.run.add_steering(NOTE, "focus")

    asyncio.run(sup._run_iteration_once("review"))

    assert _consumed_marker(tmp_path) == []
    assert _states(tmp_path) == {fname: STEERING_PENDING}
    meta = _metas(tmp_path)[-1]
    assert meta["faultClass"] is None, "a clean review iteration"
    assert (meta["steeringDelivered"], meta["steeringConsumed"]) == ([], [])
    assert NOTE.strip() not in sup.runner.prompts[-1]   # type: ignore[attr-defined]


def test_an_iteration_with_no_steering_records_both_halves_empty(tmp_path):
    sup = _supervisor(tmp_path, [_ok])

    asyncio.run(sup._run_iteration_once("worker"))

    meta = _metas(tmp_path)[-1]
    assert (meta["steeringDelivered"], meta["steeringConsumed"]) == ([], [])
    assert _events(tmp_path, "steering.consumed") == []


# -- the idempotence the at-most-once half rests on ------------------------


def test_consume_steering_can_never_apply_a_name_twice(tmp_path):
    run = RunDir(root=tmp_path)
    fname = run.add_steering(NOTE, "focus")
    path = tmp_path / "steering" / fname

    run.consume_steering([path], 1)
    run.consume_steering([path], 2)          # a redelivery that raced
    run.consume_steering([path, path], 3)    # ... and a doubled list

    assert _consumed_marker(tmp_path) == [fname]
    assert [ev["iteration"] for ev in _events(tmp_path, "steering.consumed")] == [1]
    assert run.pending_steering() == []


def test_consume_steering_applies_a_second_note_beside_the_first(tmp_path):
    """The dedupe must not make the marker write-once."""
    run = RunDir(root=tmp_path)
    one = run.add_steering(NOTE, "one")
    two = run.add_steering("and another thing\n", "two")
    sdir = tmp_path / "steering"

    run.consume_steering([sdir / one], 1)
    run.consume_steering([sdir / one, sdir / two], 2)

    assert _consumed_marker(tmp_path) == [one, two]
    assert [(ev["file"], ev["iteration"])
            for ev in _events(tmp_path, "steering.consumed")] == [(one, 1), (two, 2)]


# -- the operator surfaces: live API, and the container-gone snapshot ------


def _api_states(sup: LoopSupervisor) -> tuple:
    app = create_app(sup.cfg, sup.run, sup)

    async def get():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="http://engine") as c:
            r = await c.get("/steering")
            assert r.status_code == 200, r.text
            s = await c.get("/status")
            assert s.status_code == 200, s.text
            return r.json(), s.json()

    entries, status = asyncio.run(get())
    return ({e["file"]: e["state"] for e in entries},
            {e["file"]: e["consumed"] for e in entries},
            status.get("steering"))


def test_the_live_api_calls_the_note_pending_until_an_iteration_earns_it(tmp_path):
    sup = _supervisor(tmp_path, [_work_error, _ok])
    sup.run.update_status(state="running")
    fname = sup.run.add_steering(NOTE, "focus")

    asyncio.run(sup._run_iteration_once("worker"))
    states, consumed, counts = _api_states(sup)
    assert states == {fname: STEERING_PENDING}
    assert consumed == {fname: False}
    assert counts == {"pending": 1, "consumed": 0}

    asyncio.run(sup._run_iteration_once("worker"))
    states, consumed, counts = _api_states(sup)
    assert states == {fname: STEERING_APPLIED}
    assert consumed == {fname: True}
    assert counts == {"pending": 0, "consumed": 1}


def _steer_list(registry: Path, run_id: str) -> list[str]:
    import os
    import subprocess
    import sys
    ralphctl = Path(sys.executable).parent / "ralphctl"
    res = subprocess.run([str(ralphctl), "steer", run_id, "--list"],
                         env={**os.environ, "RALPHD_REGISTRY": str(registry)},
                         capture_output=True, text=True, timeout=60)
    assert res.returncode == 0, (res.stdout, res.stderr)
    return res.stdout.splitlines()


def test_ralphctl_steer_list_reads_the_same_verdict_off_disk(ctl: Ctl):  # noqa: F811
    """Container gone: the on-disk snapshot must show the note still pending
    after the iteration that carried it died, and applied after the next one
    finished -- the state a run dir is triaged from days later."""
    rdir, _ = _seed_run(ctl, "st-earned")
    sup = _supervisor(rdir, [_work_error, _ok])
    fname = sup.run.add_steering(NOTE, "focus")

    asyncio.run(sup._run_iteration_once("worker"))
    rows = [ln for ln in _steer_list(ctl.registry, "st-earned") if "focus" in ln]
    assert rows and STEERING_PENDING in rows[0], rows

    asyncio.run(sup._run_iteration_once("worker"))
    rows = [ln for ln in _steer_list(ctl.registry, "st-earned") if "focus" in ln]
    assert rows and STEERING_APPLIED in rows[0], rows
    assert _consumed_marker(rdir) == [fname]


# -- the record a human reads -----------------------------------------------


def test_iteration_detail_says_delivered_not_consumed_for_the_dead_attempt(
        tmp_path):
    from ralphd.engine.state import iteration_detail, iteration_summary_lines

    sup = _supervisor(tmp_path, [_work_error, _ok])
    fname = sup.run.add_steering(NOTE, "focus")
    asyncio.run(sup._run_iteration_once("worker"))
    asyncio.run(sup._run_iteration_once("worker"))

    dead = iteration_summary_lines(iteration_detail(tmp_path, 1))
    earned = iteration_summary_lines(iteration_detail(tmp_path, 2))
    line = [ln for ln in dead if ln.startswith("steering:")]
    assert line == [f"steering:  {fname}  (delivered, not consumed -- still pending)"]
    assert [ln for ln in earned if ln.startswith("steering:")] \
        == [f"steering:  {fname}"]


# -- the documented claims -------------------------------------------------


def _doc(name: str) -> str:
    return (Path(__file__).resolve().parents[1] / name).read_text()


def test_spec_documents_consumption_on_a_finished_iteration():
    spec = _doc("SPEC.md")
    assert "marked consumed once **that\n  iteration has finished cleanly**" in spec
    assert "at-least-once and application at-most-once" in spec
    assert "| `steeringDelivered` |" in spec


def test_api_docs_document_both_halves_of_the_iteration_record():
    api = _doc("docs/api.md")
    assert "`steeringDelivered` names the steering files this iteration's prompt carried" in api
    assert "Delivery is at-least-once, application at-most-once" in api


def test_api_docs_no_longer_promise_consumption_at_the_next_iteration_start():
    api = _doc("docs/api.md")
    assert "Consumed at the next iteration start" not in api
    assert ("marked consumed once that iteration finishes cleanly" in api), api


def test_architecture_documents_the_ordering_that_implements_the_choice():
    arch = _doc("docs/architecture.md")
    assert "**Delivery is at-least-once; application is at-most-once (issue #34).**" in arch
    assert "record the outcome -> append to" in arch


def test_cli_docs_document_the_delivered_not_consumed_line():
    cli = _doc("docs/cli.md")
    assert "(delivered, not consumed -- still\n  pending)" in cli
