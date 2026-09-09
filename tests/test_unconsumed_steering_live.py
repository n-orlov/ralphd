"""Task 001 (#164): `status.json`'s `unconsumedSteering` is a LIVE field, not
only a terminal-write one.

Before this task `_unconsumed_steering_patch()` (`LoopSupervisor`) was only
ever applied at the run's terminal `_finish()` write. Two consequences, both
observable on a live (non-terminal) run:

1. a steering note the engine had already consumed (`steering/
   .consumed.json` says so) kept showing as stranded in `status.json` for
   every iteration between the consume and whatever eventually happened to
   next write `status.json` -- which, for a run that never went terminal
   again in between, could be the entire rest of the run;
2. a resumed episode inherited whatever the PREVIOUS episode's status.json
   happened to say, rather than rebuilding the field from `steering/`'s own
   contents the way `_previous_endings` already refuses to trust
   `endedAt`/`reason` forward across a resume (task 022, #30).

Both feed the same visible symptom: `ralphctl status` and the hub run header
print "UNCONSUMED STEERING ... (run ended without acting on this steering)"
for a run that is very much alive and that DID act on the note.

Tiers: whole episodes through `LoopSupervisor._run_job_core` with a scripted
runner over one run dir (no subprocess, milliseconds) for the two data-layer
regressions, and `ralphctl status` through the real CLI (in-process,
`Ctl.run`) for the operator-facing rendering.
"""

from __future__ import annotations

import asyncio

from test_cli_docker import Ctl, ctl  # noqa: F401  (ctl is a fixture)
from test_cli_resume import _seed_run
from test_episode_scoped_ending import (
    _abort,
    _episode,
    _finish_task,
    _plan,
    _review_ok,
    _supervisor,
)

from ralphd.engine.state import RunDir

__all__ = ["ctl"]


# ---------------------------------------------------------------------------
# 1. cleared the moment the engine consumes it -- live, not at termination
# ---------------------------------------------------------------------------

def test_unconsumed_steering_is_cleared_live_the_moment_it_is_consumed(tmp_path):
    root = tmp_path / "run"
    seen: list[dict] = []

    def _snapshot_then_finish(sup):
        # By the time this (the SECOND scripted iteration, `worker`) runs,
        # the FIRST iteration (`planning`, an actionable phase per
        # `STEERING_ACTIONABLE_PHASES`) has already delivered and consumed
        # the pending note below, all inside `_run_iteration_once`'s own
        # teardown -- well before this run goes anywhere near terminal.
        seen.append(sup.run.read_status())
        return _finish_task(sup)

    sup = _supervisor(root, [_plan, _snapshot_then_finish, _review_ok])
    fname = sup.run.add_steering("please rename the widget", name="live-check")
    assert fname == "001-live-check.md", fname

    state = asyncio.run(sup._run_job_core())

    assert state == "succeeded", state
    assert len(seen) == 1, "the worker iteration ran exactly once"
    mid = seen[0]
    assert mid["state"] == "running", mid["state"]
    assert mid.get("unconsumedSteering") == [], (
        "status.json must clear unconsumedSteering the moment the note is "
        f"consumed, not only at the run's terminal write: {mid}")

    final = sup.run.read_status()
    assert final.get("unconsumedSteering") == []


# ---------------------------------------------------------------------------
# 2. rebuilt from steering/'s own contents on resume, not trusted forward
# ---------------------------------------------------------------------------

def test_unconsumed_steering_is_rebuilt_from_disk_on_resume(tmp_path):
    root = tmp_path / "run"
    sup1, state1 = _episode(root, [_plan, _abort])
    assert state1 == "aborted", state1

    # A note lands and is genuinely consumed on disk (`steering/
    # .consumed.json`, the ONE record `RunDir.pending_steering()` trusts) --
    # then status.json's OWN field is forced back to a stale value, standing
    # in for exactly what a resume that skipped the rebuild would inherit
    # from an earlier episode's write.
    run = RunDir(root=root)
    fname = run.add_steering("already acted on", name="acted")
    run.consume_steering([run.steering_dir / fname], iteration=1)
    run.update_status(unconsumedSteering=["999-ghost.md"])
    assert run.read_status()["unconsumedSteering"] == ["999-ghost.md"]

    seen: list[dict] = []

    def _snapshot_then_finish(sup):
        # The very first thing this episode's `_run_job_core` did, before
        # dispatching to ANY hat, was its `running` write -- so this
        # snapshot, taken inside the first iteration this episode actually
        # runs, already reflects whatever that write rebuilt.
        seen.append(sup.run.read_status())
        return _finish_task(sup)

    sup2, state2 = _episode(root, [_snapshot_then_finish, _review_ok])
    assert state2 == "succeeded", state2
    assert seen[0].get("unconsumedSteering") == [], (
        "resume must rebuild unconsumedSteering from steering/'s actual "
        f"contents, not the previous episode's status.json: {seen[0]}")


# ---------------------------------------------------------------------------
# 3. the operator surface: no "run ended" wording for a live run
# ---------------------------------------------------------------------------

def test_ralphctl_status_says_nothing_about_run_ended_for_a_live_run(ctl: Ctl):
    """The incident as it would be reported: a live, resumed run whose
    steering the engine already consumed, checked mid-episode with
    `ralphctl status` -- exactly the surface an operator watches."""
    run_id = "unconsumed-steer-live"
    rdir, _cdir = _seed_run(ctl, run_id)

    sup1, state1 = _episode(rdir, [_plan, _abort])
    assert state1 == "aborted", state1

    run = RunDir(root=rdir)
    fname = run.add_steering("acted on before this resume", name="acted")
    run.consume_steering([run.steering_dir / fname], iteration=1)
    run.update_status(unconsumedSteering=[fname])  # pre-fix stale shape

    captured: dict = {}

    def _check_ralphctl_then_finish(sup):
        proc = ctl.run("status", run_id)
        captured["out"] = proc.stdout + proc.stderr
        captured["returncode"] = proc.returncode
        captured["status"] = sup.run.read_status()
        return _finish_task(sup)

    sup2, state2 = _episode(rdir, [_check_ralphctl_then_finish, _review_ok])
    assert state2 == "succeeded", state2

    assert captured["returncode"] == 0, captured["out"]
    assert captured["status"]["state"] == "running"
    assert captured["status"].get("unconsumedSteering") == []
    assert "UNCONSUMED STEERING" not in captured["out"], captured["out"]
    assert "run ended" not in captured["out"], captured["out"]
