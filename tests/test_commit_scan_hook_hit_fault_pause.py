"""Task cure-01-03 (PRD requirement I.3, DoD 9, #210): every matcher hit --
not only the authoritative task-boundary layer's own (`_scan_task_boundary_
commits`, task 030) but the earlier, local `commit-msg`/`pre-commit` hooks'
too (task 031, requirement I.1) -- refuses, faults the assigned task, and
raises the run's `operator_input_needed` pause before another attempt.

The gap this closes: a `commit-msg`/`pre-commit` refusal happens BEFORE the
commit is ever created, so no commit lands between a task boundary's
`head_before` and `head_after` for `_scan_task_boundary_commits` to find at
all -- without a separate channel, I.3's fault-and-pause consequence would
only ever fire for the task-boundary layer, never for the hook layer, even
though a hook refusal is exactly as much a hit. `commit_scan.hook_main`
conveys the hit to the engine via a marker file (variable NAME and matched
PART only -- never the value, never a sha, since no commit exists);
`LoopSupervisor._check_hook_hit_marker` reads and clears it once per
iteration and feeds it into the very same fault/pause path a task-boundary
hit already takes.

Everything here uses a SYNTHETIC secret value only (PRD requirement I.9) --
reached the way production does, via an env var with a secret-shaped NAME
plus `redact.refresh_redaction_map(home=<empty fake home>)`; no real
credential file is ever read.
"""

from __future__ import annotations

import asyncio
import json
import subprocess

import test_commit_scan_engine_boundary as cb

from ralphd.engine import redact

SYNTHETIC_VALUE = "sk-synthetic-hookfault-0123456789abcdefSYN"
SYNTHETIC_NAME = "SYNTHETIC_HOOKFAULT_API_TOKEN"


def _live_synthetic_map_for_subprocesses(monkeypatch, tmp_path):
    """`cb.live_synthetic_map_for_subprocesses`, inlined with this module's
    own synthetic name/value so it never collides with another test's live
    map, and `HOME` pointed at an empty scratch home so the hook's own
    subprocess rebuild (`redact.build_redaction_map`) reads no real
    `~/.creds/*.env`, `~/.git-credentials` or `~/.netrc` (PRD requirement
    I.9)."""
    fake_home = tmp_path / "hook-home"
    fake_home.mkdir()
    monkeypatch.setenv(SYNTHETIC_NAME, SYNTHETIC_VALUE)
    monkeypatch.setenv("HOME", str(fake_home))
    redact.refresh_redaction_map(home=fake_home)
    assert redact.live_redaction_map().get(SYNTHETIC_VALUE) == f"env:{SYNTHETIC_NAME}"


class _CommitMsgLeakWorker(cb._CommittingWorker):
    """Commits (no `--no-verify`) with the synthetic value in the MESSAGE
    only -- the staged diff is clean, so `pre-commit` (which git runs
    first) passes and `commit-msg` is the hook that refuses."""

    async def run(self, prompt, transcript, **kw):
        (self.workspace / "safe.txt").write_text("safe\n")
        cb._git(self.workspace, "add", "safe.txt")
        attempt = subprocess.run(
            ["git", "-C", str(self.workspace), "commit", "-m",
             "review synthetic " + SYNTHETIC_VALUE],
            capture_output=True, text=True)
        self.commit_rc = attempt.returncode
        self.commit_stderr = attempt.stderr
        self.calls += 1
        return cb._ok(json.dumps({"verdict": "no_verdict"}))


class _PreCommitLeakWorker(cb._CommittingWorker):
    """Commits (no `--no-verify`) with the synthetic value in the STAGED
    DIFF only -- the message is clean, so `pre-commit` (which git runs
    before `commit-msg`) is the hook that refuses."""

    async def run(self, prompt, transcript, **kw):
        (self.workspace / "diagnostics.log").write_text(
            f"connection ok, token={SYNTHETIC_VALUE}\n")
        cb._git(self.workspace, "add", "diagnostics.log")
        attempt = subprocess.run(
            ["git", "-C", str(self.workspace), "commit", "-m",
             "diagnostics: capture what the retry saw"],
            capture_output=True, text=True)
        self.commit_rc = attempt.returncode
        self.commit_stderr = attempt.stderr
        self.calls += 1
        return cb._ok(json.dumps({"verdict": "complete", "task_id": "029t"}))


def _assert_refused_faulted_paused(sup, run, workspace, base):
    """The four observables together, for either hook path: the commit
    refused (non-zero, HEAD unchanged), the assigned task faulted straight
    to `failed`, the run's `operator_input_needed` pause raised, and --
    critically -- no push and no retry: only ONE worker iteration ran,
    despite a `"complete"`/`"no_verdict"` verdict reported on that same
    iteration.
    """
    status = run.read_status()
    task = run.read_tasks()["tasks"][0]
    assert sup.runner.commit_rc != 0, "the hook did not refuse the commit"
    assert cb._head(workspace) == base, "HEAD moved despite the hook refusal"
    assert task["status"] == "failed"
    assert status.get("state") == "paused"
    assert status.get("pauseReason") == "operator_input_needed"
    assert sup.runner.calls == 1, "a retry was dispatched after the hit"
    assert SYNTHETIC_VALUE not in json.dumps(status)
    assert SYNTHETIC_VALUE not in (sup.runner.commit_stderr or "")
    assert SYNTHETIC_NAME in (sup.runner.commit_stderr or "")


def test_commit_msg_hook_hit_refuses_faults_and_pauses_before_retry(
        tmp_path, monkeypatch):
    _live_synthetic_map_for_subprocesses(monkeypatch, tmp_path)
    sup, run, workspace = cb._sup(tmp_path, "hook-fault-commit-msg")
    base = cb._init_repo(workspace)
    sup.runner = _CommitMsgLeakWorker(  # type: ignore[assignment]
        workspace, "unused", "unused", "unused", "unused")

    asyncio.run(sup._step_worker())

    _assert_refused_faulted_paused(sup, run, workspace, base)


def test_pre_commit_hook_hit_refuses_faults_and_pauses_before_retry(
        tmp_path, monkeypatch):
    _live_synthetic_map_for_subprocesses(monkeypatch, tmp_path)
    sup, run, workspace = cb._sup(tmp_path, "hook-fault-pre-commit")
    base = cb._init_repo(workspace)
    sup.runner = _PreCommitLeakWorker(  # type: ignore[assignment]
        workspace, "unused", "unused", "unused", "unused")

    asyncio.run(sup._step_worker())

    _assert_refused_faulted_paused(sup, run, workspace, base)


def test_a_hook_hit_is_read_once_and_never_replayed_into_a_later_iteration(
        tmp_path, monkeypatch):
    """`_check_hook_hit_marker` clears the marker it reads -- a later,
    independent call over a clean iteration must never see a stale hit
    from an earlier one."""
    _live_synthetic_map_for_subprocesses(monkeypatch, tmp_path)
    sup, run, workspace = cb._sup(tmp_path, "hook-fault-read-once")
    base = cb._init_repo(workspace)
    sup.runner = _CommitMsgLeakWorker(  # type: ignore[assignment]
        workspace, "unused", "unused", "unused", "unused")
    asyncio.run(sup._step_worker())
    _assert_refused_faulted_paused(sup, run, workspace, base)

    assert sup._check_hook_hit_marker() is None, (
        "a consumed hook-hit marker was replayed into a second read")


def test_the_authoritative_boundary_hit_behavior_remains_green(
        tmp_path, monkeypatch):
    """Non-vacuity/regression: the pre-existing task-boundary (I.2) hit
    behaviour -- a commit made with `--no-verify` (bypassing the hooks
    entirely) is still caught, faulted and paused by the authoritative
    layer, exactly as task 030 already pinned. This module's own hook-hit
    marker channel must never interfere with that path when no marker was
    ever written."""
    _live_synthetic_map_for_subprocesses(monkeypatch, tmp_path)
    sup, run, workspace = cb._sup(tmp_path, "boundary-still-green")
    cb._init_repo(workspace)
    sup.runner = cb._CommittingWorker(  # type: ignore[assignment]
        workspace, "diagnostics.log",
        f"connection ok, token={SYNTHETIC_VALUE}\n",
        "diagnostics: capture what the retry saw",
        json.dumps({"verdict": "complete", "task_id": "029t"}))

    asyncio.run(sup._step_worker())

    hits = cb._events(run, cb.LoopSupervisor.COMMIT_SECRET_SCAN_EVENT)
    assert len(hits) == 1
    assert hits[0]["pushed"] is False
    assert hits[0]["sha"] == sup.runner.sha
    task = run.read_tasks()["tasks"][0]
    assert task["status"] == "failed"
    status = run.read_status()
    assert status.get("state") == "paused"
    assert status.get("pauseReason") == "operator_input_needed"
    assert sup.runner.calls == 1
    assert SYNTHETIC_VALUE not in json.dumps(status)
