"""v0.25 task 028a (PRD requirement K.1/K.2, #267): the deferred wait races
its one timer against a control-plane wake instead of ending in a bare
`await asyncio.sleep(wait_s)` with no wake condition -- the defect measured
on `deck-phase4c` on 23 September 2026 (`docs/prds/v0.25...md`'s section K):
a worker deferred correctly, the operator's steer to stop deferring landed in
the enqueue but sat unreachable for the whole bound, and only `POST /abort`
eventually cut it short, 3m42s after it was sent -- exactly the remainder the
sleep had left.

Site drift, named per this task's own criteria: the PRD (and steer
001-requirement-k-267.md) cites `_sleep_until_earliest_deferred` at
`loop.py:5436-5509`; as measured on 23 Sep 2026 (this task's own tip, before
this commit's edit) it is at `loop.py:5628`, called from
`_run_job_core`'s dispatch pass at `loop.py:8477` (now `loop.py:8531` after
this same edit grew the docstring) -- a ~200-line drift from four earlier
tasks (025/026/027) landing ahead of it in the file, not from a rename or a
move.

Five things pinned here, none of them through a poll loop -- `asyncio.wait`
tasks are inspected for exactly one call to the (possibly stubbed) timer
`asyncio.sleep`, whatever the outcome:

* nothing but the real timer fires: the ORIGINAL two outcomes
  (`test_deferred_wait.py`'s own subject) still hold, but this module also
  pins that `waitedS` is the REAL measured elapsed time (not the planned
  bound, which is what the pre-K.1 code reported even under a fully mocked,
  instant `asyncio.sleep`) while `sleptUntil` still names the bound this
  wait was aimed AT.
* the bell rings first (`RunDir.add_steering` or `LoopSupervisor.abort`,
  raced via `asyncio.wait(..., return_when=FIRST_COMPLETED)`): `boundedBy`
  becomes `"steering"`/`"abort"`, `waitedS` reports the short real elapsed
  time to the wake (not the full bound), `sleptUntil` still names the
  original target, the deferred task's own `deferredUntil`/`deferredReason`
  are left completely untouched, and the caller is told to `continue` the
  dispatch loop -- which the last section proves actually reaches the
  existing top-of-pass `pending_steering()` check and routes to the `steer`
  hat, never touching the deferral itself.

Mutation cases (recorded in the commit message): reverting to the bare
`await asyncio.sleep(wait_s)` would make every "woken" test below hang for
the full bound instead of returning promptly (caught by the real-time
tolerance in `_run_woken`, well under the bound); reporting the PLANNED
`wait_s` as `waitedS` instead of the real elapsed time (the pre-K.1 shape)
would fail
`test_a_wait_that_elapses_reports_the_real_near_zero_elapsed_time_under_a_mocked_sleep`
directly, since a mocked sleep never advances the clock; clearing
`deferredUntil`/`deferredReason` on a steering/abort wake (treating a wake as
an elapsed wait, the thing K.2 forbids) would fail
`test_a_steering_wake_leaves_the_deferred_tasks_record_completely_untouched`
and the snapshot assertion in the dispatch-pass integration test; slicing the
timer into short polls instead of one `asyncio.wait(...,
return_when=FIRST_COMPLETED)` would fail every `sleep_calls == [...]`
assertion below (more than one recorded call, or one shorter than the
planned bound).
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import pytest

from ralphd.engine.loop import LoopSupervisor
from ralphd.engine.runner import IterationResult
from ralphd.engine.state import RunDir, atomic_write_json, utc_from_epoch
from tests.conftest import scripted_supervisor

TASK_1 = "001"


def _ok(text: str) -> IterationResult:
    r = IterationResult(exit_code=0)
    r.final_text = text
    r.duration_s = 5.0
    r.usage = {"input": 10, "output": 5, "totalTokens": 15}
    return r


def _complete(task_id: str) -> IterationResult:
    payload = json.dumps({"verdict": "complete", "task_id": task_id})
    return _ok(f"task {task_id} done\n{payload}")


def _steer_settles(sup, prompt=""):
    return _ok(json.dumps({"settled": True}))


def _review_verifies(sup, prompt=""):
    return _ok("the PRD holds\n" + json.dumps({"verdict": "verified"}))


def _task(**over) -> dict:
    task = {"id": TASK_1, "title": "wait on a gated window", "status": "pending",
            "successCriteria": "the window opened"}
    task.update(over)
    return task


_supervisor = scripted_supervisor(run_id="v25-deferred-wake", iterations=15,
                                  max_approaches=1, vigilant=False,
                                  on_complete="exit",
                                  infra_outage_budget_s=1000.0)


def _build(root: Path, handlers: dict, tasks: list[dict],
          **cfg_kw) -> LoopSupervisor:
    """Builds the supervisor WITHOUT driving it -- unlike `test_deferred_
    wait.py`'s `_run_resumed`, split apart here so a test can install its
    own sleep-counting monkeypatch (or spawn its own concurrent waker) in
    between construction and the drive."""
    RunDir(root=root).update_status(state="starting")
    sup = _supervisor(root, handlers, **cfg_kw)
    atomic_write_json(sup.run.tasks_file, {"version": 1, "tasks": tasks})
    sup.iterations_used = 1
    return sup


def _events(root: Path) -> list[dict]:
    p = root / "events.jsonl"
    if not p.exists():
        return []
    return [json.loads(x) for x in p.read_text().splitlines() if x.strip()]


def _deferred_waits(root: Path) -> list[dict]:
    return [ev for ev in _events(root) if ev.get("type") == "deferred_wait"]


def _install_sleep_counter(monkeypatch) -> list[float]:
    """Wraps whatever `asyncio.sleep` currently is (the real one under
    `real_sleep=True`, the globally instant stand-in otherwise) with a
    counting layer, restored automatically by `monkeypatch` at teardown.
    Installed AFTER the supervisor exists so it sees whichever `asyncio.
    sleep` `make_supervisor` itself left in place."""
    calls: list[float] = []
    original = asyncio.sleep

    async def counting(delay, result=None):
        calls.append(delay)
        return await original(delay, result)

    monkeypatch.setattr(asyncio, "sleep", counting)
    return calls


# ---------------------------------------------------------------------------
# 1. The timer alone still fires -- the ORIGINAL two outcomes, now also
#    pinning `waitedS` as the REAL elapsed time (never the planned bound).
# ---------------------------------------------------------------------------


def test_the_earliest_until_wakes_the_task_with_one_timer_sleep_for_the_whole_bound(
        tmp_path, monkeypatch):
    root = tmp_path / "run"
    until = utc_from_epoch(time.time() + 50)
    tasks = [_task(deferredUntil=until, deferredReason="waiting on a "
                   "gated window to open")]
    sup = _build(root, {"worker": [], "review": []}, tasks,
                job_timeout_s=3600)
    sleep_calls = _install_sleep_counter(monkeypatch)

    woken = asyncio.run(sup._sleep_until_earliest_deferred(
        sup.run.read_tasks()))

    assert woken is True
    # exactly one timer sleep, for the WHOLE planned bound -- never sliced.
    assert len(sleep_calls) == 1, sleep_calls
    assert sleep_calls[0] == pytest.approx(50, abs=1.0), sleep_calls

    task = sup.run.read_tasks()["tasks"][0]
    assert "deferredUntil" not in task
    assert "deferredReason" not in task

    waits = _deferred_waits(root)
    assert len(waits) == 1, waits
    ev = waits[0]
    assert ev["boundedBy"] == "until"
    assert ev["taskId"] == TASK_1
    assert ev["until"] == until
    assert ev["sleptUntil"] == until   # the target this wait was aimed at
    # a mocked `asyncio.sleep` never advances the clock -- the REAL elapsed
    # time this call actually spent must be near zero, never the ~50s
    # planned bound the pre-K.1 code used to report here.
    assert ev["waitedS"] < 1.0, ev


def test_a_deadline_bound_still_leaves_the_task_untouched(tmp_path, monkeypatch):
    root = tmp_path / "run"
    far_until = "2099-01-01T00:00:00Z"
    tasks = [_task(deferredUntil=far_until, deferredReason="waiting on a "
                   "window that outlives this job")]
    sup = _build(root, {"worker": [], "review": []}, tasks, job_timeout_s=1)
    sleep_calls = _install_sleep_counter(monkeypatch)

    woken = asyncio.run(sup._sleep_until_earliest_deferred(
        sup.run.read_tasks()))

    assert woken is False
    assert len(sleep_calls) == 1, sleep_calls

    task = sup.run.read_tasks()["tasks"][0]
    assert task["deferredUntil"] == far_until
    assert task["deferredReason"] == (
        "waiting on a window that outlives this job")

    waits = _deferred_waits(root)
    assert len(waits) == 1, waits
    ev = waits[0]
    assert ev["boundedBy"] == "deadlineAt"
    assert ev["taskId"] == TASK_1
    assert ev["until"] == far_until
    assert ev["waitedS"] < 1.0, ev


# ---------------------------------------------------------------------------
# 2. The bell rings first -- `boundedBy` names the wake cause, the real
#    elapsed time to the wake is reported, the target survives untouched,
#    and the deferral itself is left alone (K.2).
# ---------------------------------------------------------------------------


async def _wake_after(sleep_fn, delay: float, action) -> None:
    await sleep_fn(delay)
    action()


async def _run_woken(sleep_fn, sup, tasks_doc: dict, delay: float, action):
    waker = asyncio.ensure_future(_wake_after(sleep_fn, delay, action))
    result = await sup._sleep_until_earliest_deferred(tasks_doc)
    await waker
    return result


def _woken_case(tmp_path, monkeypatch, action_name: str, action_of):
    root = tmp_path / "run"
    bound_s = 3.0
    until = utc_from_epoch(time.time() + bound_s)
    tasks = [_task(deferredUntil=until, deferredReason="waiting on a "
                   "gated window to open")]
    sup = _build(root, {"worker": [], "review": []}, tasks,
                job_timeout_s=3600, real_sleep=True)
    real_sleep_fn = asyncio.sleep    # captured BEFORE the counter wraps it
    sleep_calls = _install_sleep_counter(monkeypatch)

    wake_delay = 0.15
    started = time.time()
    expected_wait_s = max(0.0, sup._parse_iso_epoch(until) - started)
    tasks_doc = sup.run.read_tasks()
    woken = asyncio.run(_run_woken(real_sleep_fn, sup, tasks_doc, wake_delay,
                                   action_of(sup)))
    real_elapsed = time.time() - started

    # continue the dispatch loop -- exactly like the "until elapsed" case.
    assert woken is True
    # one real timer sleep, requested for the WHOLE planned bound (~3s,
    # modulo the test's own setup time before the call), never sliced
    # into short polls even though the wake wins first.
    assert len(sleep_calls) == 1, sleep_calls
    assert sleep_calls[0] == pytest.approx(expected_wait_s, abs=0.5), sleep_calls
    # the wait actually returned promptly (near the wake delay), not after
    # the whole planned bound -- proves the wake, not the timer, won.
    assert real_elapsed < bound_s - 1.0, real_elapsed

    waits = _deferred_waits(root)
    assert len(waits) == 1, waits
    ev = waits[0]
    assert ev["boundedBy"] == action_name
    assert ev["taskId"] == TASK_1
    assert ev["sleptUntil"] == until, (
        "the TARGET this wait was aimed at must survive an early wake "
        "unchanged")
    # the real elapsed time to the wake, not the planned ~3s bound.
    assert ev["waitedS"] == pytest.approx(wake_delay, abs=0.4), ev
    assert ev["waitedS"] < bound_s - 1.0, ev
    return sup


def test_a_steering_wake_leaves_the_deferred_tasks_record_completely_untouched(
        tmp_path, monkeypatch):
    def action_of(sup):
        return lambda: sup.run.add_steering(
            "do not defer -- proceed", "opK")
    sup = _woken_case(tmp_path, monkeypatch, "steering", action_of)
    task = sup.run.read_tasks()["tasks"][0]
    # K.2: a wake is NOT an elapsed wait -- the deferral is untouched.
    assert task.get("deferredUntil"), task
    assert task.get("deferredReason"), task
    assert sup.run.pending_steering(), "the enqueued steer must still be there"


def test_an_abort_wake_is_named_distinctly_from_a_steering_wake(
        tmp_path, monkeypatch):
    def action_of(sup):
        return lambda: sup.abort("operator asked to stop deferring")
    sup = _woken_case(tmp_path, monkeypatch, "abort", action_of)
    task = sup.run.read_tasks()["tasks"][0]
    assert task.get("deferredUntil"), task
    assert task.get("deferredReason"), task
    assert sup._abort_reason is not None


# ---------------------------------------------------------------------------
# 3. The integration proof: a steering wake really does re-enter the
#    dispatch pass, and the existing top-of-pass `pending_steering()` check
#    routes it to the `steer` hat -- never to the deferral itself.
# ---------------------------------------------------------------------------


async def _drive_with_waker(sleep_fn, sup, delay: float, action):
    waker = asyncio.ensure_future(_wake_after(sleep_fn, delay, action))
    result = await sup._run_job_core()
    await waker
    return result


def test_a_steering_wake_routes_the_very_next_pass_to_the_steer_hat(tmp_path):
    root = tmp_path / "run"
    until = utc_from_epoch(time.time() + 2.0)
    tasks = [_task(deferredUntil=until,
                  deferredReason="waiting on a gated window to open")]

    snapshots: list[dict] = []

    def steer_and_snapshot(sup, prompt=""):
        # captured at the EXACT moment the steer hat runs -- immediately
        # after the wake this test triggers, before anything else has a
        # chance to touch the deferral.
        snapshots.append(sup.run.read_tasks()["tasks"][0])
        return _ok(json.dumps({"settled": True}))

    sup = _build(root, {
        "steer": [steer_and_snapshot],
        "worker": [lambda s, p: _complete(TASK_1)],
        "review": [_review_verifies],
    }, tasks, job_timeout_s=3600, real_sleep=True)

    real_sleep_fn = asyncio.sleep
    final_state = asyncio.run(_drive_with_waker(
        real_sleep_fn, sup, 0.1, lambda: sup.run.add_steering(
            "do not defer -- proceed", "opK")))

    agent = sup.runner
    assert "steer" in agent.phases_seen, agent.phases_seen
    steer_index = agent.phases_seen.index("steer")
    # the steer pass came from the wake -- not from a task ever being
    # dispatched to `worker` first.
    assert "worker" not in agent.phases_seen[:steer_index], agent.phases_seen

    assert snapshots, "the steer hat handler never ran"
    snapshot = snapshots[0]
    assert snapshot.get("deferredUntil") == until, (
        "the deferred task's OWN record must still hold its original "
        "until/reason the moment the steer hat runs -- a wake must not "
        "have cleared it (K.2)")
    assert snapshot.get("deferredReason")

    waits = _deferred_waits(root)
    assert waits, "no deferred_wait event recorded at all"
    assert waits[0]["boundedBy"] == "steering"
    # the task's own `until` still elapses normally afterwards (this run
    # never told the engine to cancel the deferral, only to look at the
    # operator's words) and the very next dispatch completes it.
    assert any(w["boundedBy"] == "until" for w in waits[1:]), waits

    task = sup.run.read_tasks()["tasks"][0]
    assert task["status"] == "completed", task
    assert "deferredUntil" not in task
    assert "deferredReason" not in task
    assert final_state == "succeeded", sup.run.read_status()
