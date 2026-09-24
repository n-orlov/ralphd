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

The criterion's no-push clause is observed of the RUN, not merely of the
engine: each hook-path test wires a bare remote THIS TEST owns as `origin`
(a local directory -- no network, no credential), configures the engine to
own a push of its own (`workspace_push_remote`/`workspace_push_branch`), and
uses a worker that does what a worker of this repository does -- `git
commit` without `--no-verify`, then `git push origin main` from its own
shell, inside the iteration. On a hit the remote's `main` must not move, the
engine's single push entry point (`commit_scan.push_if_clean`) must never be
reached, and the recorded `commit_secret_scan` event must say `pushed` is
False with no `sha` (no commit was ever created) and no matched value
anywhere in it.
`test_a_clean_commit_through_the_same_hooks_still_commits_and_pushes` is the
non-vacuity twin of exactly that wiring: same hooks, same remote, same
worker, a value that is token-SHAPED but not in the map -- the commit lands,
the worker's push really advances the remote, and the engine's own push path
is reached once.
"""

from __future__ import annotations

import asyncio
import json
import subprocess

import test_commit_scan_engine_boundary as cb

from ralphd.engine import commit_scan, redact

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


class _LeakingCommitThenPushWorker(cb._CommittingWorker):
    """The production shape of a worker iteration under the shipped config
    (`workspace_push_remote is None`: the WORKER pushes its own commit from
    its own shell): stage, `git commit` WITHOUT `--no-verify`, then
    immediately `git push origin main` -- so if the hooks let the leak
    through, the irrevocable sink is reached inside this very iteration,
    long before the engine's task boundary reads `head_after`.

    Subclasses differ only in which part of the commit carries the
    synthetic value, and therefore in WHICH hook refuses.
    """

    filename = "unused"
    file_content = "unused"
    commit_message = "unused"
    verdict: dict = {"verdict": "no_verdict"}

    def __init__(self, *args, **kw):
        super().__init__(*args, **kw)
        self.commit_rc: int | None = None
        self.commit_stderr = ""
        self.push_rc: int | None = None
        self.push_stderr = ""

    async def run(self, prompt, transcript, **kw):
        (self.workspace / self.filename).write_text(self.file_content)
        cb._git(self.workspace, "add", self.filename)
        attempt = subprocess.run(
            ["git", "-C", str(self.workspace), "commit", "-m",
             self.commit_message],
            capture_output=True, text=True)
        self.commit_rc = attempt.returncode
        self.commit_stderr = attempt.stderr
        # Production order, whatever the commit did: the worker's next act
        # is its own push (standing rule: one commit per task, pushed
        # immediately). On a refused commit there is nothing new to send,
        # which is exactly the observable these tests assert -- the remote
        # ref does not move.
        pushed = subprocess.run(
            ["git", "-C", str(self.workspace), "push", "origin", "main"],
            capture_output=True, text=True)
        self.push_rc = pushed.returncode
        self.push_stderr = pushed.stderr
        self.calls += 1
        return cb._ok(json.dumps(self.verdict))


class _CommitMsgLeakWorker(_LeakingCommitThenPushWorker):
    """Commits (no `--no-verify`) with the synthetic value in the MESSAGE
    only -- the staged diff is clean, so `pre-commit` (which git runs
    first) passes and `commit-msg` is the hook that refuses."""

    filename = "safe.txt"
    file_content = "safe\n"
    commit_message = "review synthetic " + SYNTHETIC_VALUE
    verdict = {"verdict": "no_verdict"}


class _PreCommitLeakWorker(_LeakingCommitThenPushWorker):
    """Commits (no `--no-verify`) with the synthetic value in the STAGED
    DIFF only -- the message is clean, so `pre-commit` (which git runs
    before `commit-msg`) is the hook that refuses."""

    filename = "diagnostics.log"
    file_content = f"connection ok, token={SYNTHETIC_VALUE}\n"
    commit_message = "diagnostics: capture what the retry saw"
    verdict = {"verdict": "complete", "task_id": "029t"}


class _CleanCommitThenPushWorker(_LeakingCommitThenPushWorker):
    """Non-vacuity twin of the two above: the same hooks, the same remote,
    the same commit-then-push order -- with no map value anywhere. The
    commit is created and the push really does reach the remote, which is
    what makes "the remote ref did not move" a real observation in the two
    leak tests rather than a property of a broken fixture."""

    filename = "diagnostics.log"
    file_content = f"connection ok, token shaped like {cb.TOKEN_SHAPED_BUT_UNKNOWN}\n"
    commit_message = "diagnostics: name the variable, not its value"
    verdict = {"verdict": "no_verdict"}


def _sup_with_remote(tmp_path, name, worker_cls):
    """A supervisor whose workspace has a bare remote THIS TEST owns (a
    local directory -- no network, no credential) wired as `origin` and
    already holding the pre-iteration `main`, AND an engine configured to
    own a push of its own (`workspace_push_remote`/`workspace_push_branch`)
    so that a push subprocess is something the engine WOULD start on a
    clean iteration. Both push sinks -- the worker's own shell and the
    engine's task boundary -- are therefore live and observable."""
    sup, run, workspace = cb._sup(tmp_path, name,
                                  workspace_push_remote="origin",
                                  workspace_push_branch="main")
    base = cb._init_repo(workspace)
    remote = cb._bare_remote(workspace, tmp_path / name / "remote.git")
    assert cb._remote_main(remote) == base
    sup.runner = worker_cls(  # type: ignore[assignment]
        workspace, "unused", "unused", "unused", "unused")
    return sup, run, workspace, remote, base


def _remote_commit_count(remote: str) -> int:
    out = subprocess.run(
        ["git", "-C", remote, "rev-list", "--all", "--count"],
        capture_output=True, text=True, check=True).stdout.strip()
    return int(out or 0)


def _engine_push_spy(monkeypatch):
    """Record every call to `commit_scan.push_if_clean` -- the ONE place in
    the engine that may start a `git push` subprocess at all (see
    `LoopSupervisor._scan_task_boundary_commits`'s own docstring) -- while
    delegating to the real implementation, so nothing about the engine's
    behaviour changes.

    Deliberately NOT `mock.patch("ralphd.engine.commit_scan.subprocess.run")`,
    the idiom `tests/test_commit_scan_engine_boundary.py` uses for its
    direct-call nodes: `commit_scan.subprocess` is the stdlib module object
    itself, so patching its `run` attribute patches `subprocess.run`
    process-wide -- including the push the worker under test makes from its
    own shell, which would leave "the remote ref never moved" true because
    the push was faked rather than because the gate refused it.
    """
    calls: list[tuple] = []
    real = commit_scan.push_if_clean

    def _spy(*args, **kw):
        calls.append((args, kw))
        return real(*args, **kw)

    monkeypatch.setattr(commit_scan, "push_if_clean", _spy)
    return calls


def _assert_refused_faulted_paused(sup, run, workspace, base, remote,
                                   engine_pushes, expected_part):
    """Every observable the criterion names, together, for either hook
    path: the commit refused (non-zero rc, HEAD unchanged), the hit
    conveyed to the engine WITHOUT the matched value, the assigned task
    faulted straight to `failed`, the run's `operator_input_needed` pause
    raised, and -- critically -- NO PUSH and NO RETRY: the worker's own
    `git push origin main` (attempted in that same iteration, from its own
    shell, against a live remote this test owns) moved no remote ref and
    transferred no commit, the engine -- itself configured to own a push --
    started no `git push` subprocess either, the recorded
    `commit_secret_scan` event says `pushed` is False, and only ONE worker
    iteration ran despite a `"complete"`/`"no_verdict"` verdict reported on
    that same iteration.
    """
    status = run.read_status()
    task = run.read_tasks()["tasks"][0]
    assert sup.runner.commit_rc != 0, "the hook did not refuse the commit"
    assert cb._head(workspace) == base, "HEAD moved despite the hook refusal"

    # --- no push, of the RUN: neither sink moved anything ---------------
    assert cb._remote_main(remote) == base, (
        "the refused commit reached the remote anyway")
    assert _remote_commit_count(remote) == 1, (
        "the remote gained an object set the hook had just refused")
    assert not engine_pushes, (
        "the engine reached its own push path (commit_scan.push_if_clean) "
        f"despite the hook hit: {engine_pushes}")

    # --- the hit as the engine recorded it, naming no value -------------
    hits = cb._events(run, cb.LoopSupervisor.COMMIT_SECRET_SCAN_EVENT)
    assert len(hits) == 1, (
        "the hook hit was not conveyed to the engine as exactly one "
        f"commit_secret_scan event: {hits}")
    assert hits[0]["pushed"] is False, (
        "the recorded hit does not state that nothing was pushed")
    assert hits[0]["variable"] == SYNTHETIC_NAME
    assert hits[0]["part"] == expected_part
    # No commit was ever created, so there is no sha to name -- and the
    # value itself appears nowhere in the record.
    assert hits[0]["sha"] is None
    assert SYNTHETIC_VALUE not in json.dumps(hits[0])
    recorded = status.get(cb.LoopSupervisor.COMMIT_SECRET_SCAN_STATUS_KEY)
    assert recorded is not None and recorded["pushed"] is False

    # --- faulted, paused, never retried --------------------------------
    assert task["status"] == "failed"
    assert status.get("state") == "paused"
    assert status.get("pauseReason") == "operator_input_needed"
    assert sup.runner.calls == 1, "a retry was dispatched after the hit"
    assert SYNTHETIC_VALUE not in json.dumps(status)
    assert SYNTHETIC_VALUE not in (sup.runner.commit_stderr or "")
    assert SYNTHETIC_VALUE not in (sup.runner.push_stderr or "")
    assert SYNTHETIC_NAME in (sup.runner.commit_stderr or "")


def test_commit_msg_hook_hit_refuses_faults_and_pauses_before_retry(
        tmp_path, monkeypatch):
    _live_synthetic_map_for_subprocesses(monkeypatch, tmp_path)
    sup, run, workspace, remote, base = _sup_with_remote(
        tmp_path, "hook-fault-commit-msg", _CommitMsgLeakWorker)
    engine_pushes = _engine_push_spy(monkeypatch)

    asyncio.run(sup._step_worker())

    _assert_refused_faulted_paused(sup, run, workspace, base, remote,
                                   engine_pushes, "message")


def test_pre_commit_hook_hit_refuses_faults_and_pauses_before_retry(
        tmp_path, monkeypatch):
    _live_synthetic_map_for_subprocesses(monkeypatch, tmp_path)
    sup, run, workspace, remote, base = _sup_with_remote(
        tmp_path, "hook-fault-pre-commit", _PreCommitLeakWorker)
    engine_pushes = _engine_push_spy(monkeypatch)

    asyncio.run(sup._step_worker())

    _assert_refused_faulted_paused(sup, run, workspace, base, remote,
                                   engine_pushes, "diff")


def test_a_clean_commit_through_the_same_hooks_still_commits_and_pushes(
        tmp_path, monkeypatch):
    """Non-vacuity for both tests above: identical wiring -- same hooks,
    same bare remote, same commit-then-push worker -- with a
    token-SHAPED-but-unknown value that is not in the map (PRD requirement
    I.4). The commit is created, the worker's push really does advance the
    remote's `main`, nothing is faulted or paused, and no
    `commit_secret_scan` event is recorded at all. So "the remote ref did
    not move" above is a property of the refusal, not of the fixture."""
    _live_synthetic_map_for_subprocesses(monkeypatch, tmp_path)
    sup, run, workspace, remote, base = _sup_with_remote(
        tmp_path, "hook-clean-pushes", _CleanCommitThenPushWorker)
    engine_pushes = _engine_push_spy(monkeypatch)

    asyncio.run(sup._step_worker())

    assert sup.runner.commit_rc == 0, sup.runner.commit_stderr
    assert sup.runner.push_rc == 0, sup.runner.push_stderr
    head = cb._head(workspace)
    assert head != base
    assert cb._remote_main(remote) == head, (
        "the clean commit never reached the remote: the no-push assertions "
        "in this module's leak tests would then be vacuous")
    assert len(engine_pushes) == 1, (
        "the engine never reached its own push path on a clean iteration, so "
        "its absence on a hit above would prove nothing: "
        f"{engine_pushes}")
    assert cb._events(run, cb.LoopSupervisor.COMMIT_SECRET_SCAN_EVENT) == []
    assert run.read_status().get(
        cb.LoopSupervisor.COMMIT_SECRET_SCAN_STATUS_KEY) is None
    assert run.read_status().get("state") != "paused"
    assert run.read_tasks()["tasks"][0]["status"] != "failed"
    assert commit_scan.read_and_clear_hook_hit_marker(run.root) is None


def test_a_hook_hit_is_read_once_and_never_replayed_into_a_later_iteration(
        tmp_path, monkeypatch):
    """`_check_hook_hit_marker` clears the marker it reads -- a later,
    independent call over a clean iteration must never see a stale hit
    from an earlier one."""
    _live_synthetic_map_for_subprocesses(monkeypatch, tmp_path)
    sup, run, workspace, remote, base = _sup_with_remote(
        tmp_path, "hook-fault-read-once", _CommitMsgLeakWorker)
    engine_pushes = _engine_push_spy(monkeypatch)
    asyncio.run(sup._step_worker())
    _assert_refused_faulted_paused(sup, run, workspace, base, remote,
                                   engine_pushes, "message")

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
