"""Task 033 (#13): the stale-terminal audit of `ralphctl logs -f` / `GET /logs`.

Finding (see the task-033 commit message): the premature close fixed in
task 031 for `ralphctl watch` is **not** shared by the logs path. The two
followers end their stream on different signals:

* `watch` streams `events.jsonl`, which is append-only across resumes, and
  used to close on the FIRST replayed `state: succeeded|failed|aborted`
  line -- i.e. on a *historical marker written by a previous episode*.
* `logs -f` streams the merged iteration transcripts; the CLI never
  inspects events at all (`_stream_logs` just renders whatever arrives and
  ends when the server closes the body), and the engine's
  `GET /logs?follow=true` follower ends only when `finished()` -- a FRESH
  read of `status.json`'s current `state` on every poll -- is true and all
  iteration dirs are consumed. A resuming engine rewrites `status.json` to
  `state: starting` *before* its API starts serving (engine/main.py), so a
  follower can never observe the previous episode's terminal state. That
  is the same liveness reconciliation task 031 added to `watch`, only here
  it was correct by construction, so there is nothing to fix.

Because "correct by construction" is exactly the kind of claim that rots,
these tests pin it down from the outside on the real thing: two real
`ralphd-engine` processes over one run dir (episode 1 dies with a terminal
marker mid-log and terminal `status.json` on disk; episode 2 resumes) with
the real `ralphctl logs --follow` attached across the resume.
"""

from __future__ import annotations

import json
import os
import subprocess
import threading
import time

import pytest

from tests.conftest import RALPHCTL, LiveRun

TERMINAL = ("succeeded", "failed", "aborted")


@pytest.fixture
def episodes(tmp_path):
    """Factory for successive engine processes over ONE run dir/registry --
    i.e. what `ralphctl resume` does: same run dir, same config dir, a
    fresh port recorded in host.json, a bumped budget."""
    runs: list[LiveRun] = []

    def make(run_id: str, job: dict, stub_env: dict | None = None) -> LiveRun:
        r = LiveRun(tmp_path, run_id, {"run_id": run_id, **job}, stub_env)
        runs.append(r)
        return r

    yield make
    for r in runs:
        r.stop()


class Follower:
    """`ralphctl logs <id> --follow --raw` as a subprocess with its stdout
    drained by a reader thread, so assertions can inspect what has arrived
    so far without blocking (and without racing the process's exit)."""

    def __init__(self, registry, run_id: str, *extra: str):
        env = {**os.environ, "RALPHD_REGISTRY": str(registry)}
        self.proc = subprocess.Popen(
            [str(RALPHCTL), "logs", run_id, "--follow", "--raw", *extra],
            env=env, text=True, bufsize=1,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.lines: list[str] = []
        self.thread = threading.Thread(target=self._drain, daemon=True)
        self.thread.start()

    def _drain(self):
        for line in self.proc.stdout:
            if line.strip():
                self.lines.append(line.strip())

    def objects(self) -> list[dict]:
        out = []
        for line in list(self.lines):
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return out

    def iteration_starts(self) -> list[int]:
        return [o["number"] for o in self.objects()
                if o.get("type") == "ralphd.iteration" and o.get("event") == "start"]

    def wait_until(self, predicate, timeout=45):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if predicate(self):
                return
            if self.proc.poll() is not None:
                raise AssertionError(
                    "ralphctl logs --follow exited early (rc="
                    f"{self.proc.returncode}); lines so far: {self.lines[-5:]}")
            time.sleep(0.1)
        raise AssertionError(f"condition never held; lines: {self.lines[-5:]}")

    def wait_quiescent(self, quiet=1.5, timeout=45) -> None:
        """Block until the follow has caught up completely -- the last line
        received is an iteration *end* boundary and nothing new arrives for
        `quiet` seconds -- which is exactly the engine-side state where the
        follower has consumed every iteration dir and is polling its
        'has the job finished?' check. That is the branch a stale-terminal
        bug closes in, so an implementation that trusted a replayed
        terminal marker would exit inside this window instead of
        returning."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            before = len(self.lines)
            time.sleep(quiet)
            objs = self.objects()
            if self.proc.poll() is not None:
                raise AssertionError(
                    "ralphctl logs --follow closed while the job was still "
                    f"alive (rc={self.proc.returncode})")
            if len(self.lines) == before and objs and objs[-1].get("event") == "end":
                return
        raise AssertionError(f"follow never went quiescent; lines: {self.lines[-5:]}")

    def wait_exit(self, timeout=60) -> int:
        rc = self.proc.wait(timeout=timeout)
        self.thread.join(timeout=10)
        return rc

    def kill(self):
        if self.proc.poll() is None:
            self.proc.kill()
            self.proc.wait(timeout=10)


def _state(run: LiveRun) -> str | None:
    sf = run.run_dir / "status.json"
    if not sf.exists():
        return None
    try:
        return json.loads(sf.read_text()).get("state")
    except json.JSONDecodeError:
        return None


def _state_events(run: LiveRun) -> list[dict]:
    path = run.run_dir / "events.jsonl"
    return [ev for ev in (json.loads(x) for x in path.read_text().splitlines())
            if ev.get("type") == "state"]


def _dead_first_episode(episodes, run_id: str) -> LiveRun:
    """Episode 1: a budget too small for the plan -> a real terminal
    (`failed`) state written to `status.json` AND a terminal `state` event
    appended to `events.jsonl`, with completed iteration dirs on disk. This
    is precisely the on-disk shape that made `watch` close early (#13)."""
    e1 = episodes(run_id, {"iterations": 2, "max_approaches": 1,
                           "on_complete": "exit"},
                  {"STUB_TASKS": "5"})
    assert e1.proc.wait(timeout=60) == 1
    assert _state(e1) == "failed"
    assert [ev["state"] for ev in _state_events(e1)] == ["running", "failed"]
    assert len(sorted((e1.run_dir / "iterations").iterdir())) == 2
    return e1


def test_logs_follow_streams_past_a_resumed_runs_stale_terminal_marker(episodes):
    """The #13 scenario applied to `logs -f`: attach to a run dir that
    already carries a mid-log terminal marker + finished iterations, while
    a resumed engine is working. The follow must replay the old episode's
    transcript, keep streaming into the NEW episode's iterations, and only
    end at the real terminus."""
    e1 = _dead_first_episode(episodes, "logsresume")

    # Episode 2 = the resume: same run dir, bumped budget, slow enough that
    # "it kept streaming" is a genuine multi-second observation.
    e2 = episodes("logsresume", {"iterations": 20, "max_approaches": 1,
                                 "on_complete": "idle"},
                  {"STUB_TASKS": "5", "STUB_SLEEP": "0.5"})
    assert e2.run_dir == e1.run_dir  # sanity: genuinely the same run dir
    e2.wait_api()
    # An operator pause parks the loop at an iteration boundary, which puts
    # the follower in its 'all iteration dirs consumed, is the job over?'
    # branch *while the run is very much alive* -- a deterministic window
    # for the assertion below (without it, the resumed engine creates the
    # next iteration dir so promptly that the branch is barely entered).
    assert e2.ralphctl("pause", "logsresume").returncode == 0

    f = Follower(e2.registry, "logsresume")
    try:
        # The stale terminal signals are all on disk (terminal marker in
        # events.jsonl, and status.json said `failed` until this engine
        # rewrote it) -- yet the follow streams the replayed backlog...
        f.wait_until(lambda f: set(f.iteration_starts()) >= {1, 2})
        # ... and does NOT close there: it sits in the has-it-finished poll
        # while the paused-but-live job holds still. Taken while the job is
        # non-terminal, so it cannot be satisfied by a
        # close-then-everything-flushed implementation.
        f.wait_quiescent()
        assert f.proc.poll() is None, \
            "logs --follow closed on a resumed run's stale terminal state"
        assert _state(e2) not in TERMINAL
        assert e2.ralphctl("unpause", "logsresume").returncode == 0

        # It carries on into iterations the resumed engine creates *after*
        # the stale marker (numbering continues past episode 1's last).
        f.wait_until(lambda f: any(n > 2 for n in f.iteration_starts()))
        assert f.proc.poll() is None
        assert _state(e2) not in TERMINAL

        # The real terminus -- and only that -- ends the stream.
        final = e2.wait_terminal(timeout=90)
        assert final["state"] in TERMINAL
        assert f.wait_exit() == 0, f.proc.stderr.read()

        starts = f.iteration_starts()
        assert starts == sorted(starts)
        assert {1, 2} <= set(starts) and max(starts) > 2, starts
        # every raw line is 1:1 NDJSON (the --raw wire contract)
        assert len(f.objects()) == len(f.lines)
    finally:
        f.kill()


def test_logs_follow_on_a_finished_resumed_run_still_exits_promptly(episodes):
    """The other direction (the failure mode a naive 'never close on a
    terminal marker' fix would introduce): a resumed run that has since
    finished must not hang -- the follow replays both episodes' transcripts
    and exits at once, because `finished()` reads the run's CURRENT state."""
    _dead_first_episode(episodes, "logsresume-done")

    e2 = episodes("logsresume-done", {"iterations": 20, "max_approaches": 1,
                                      "on_complete": "idle"},
                  {"STUB_TASKS": "5"})
    e2.wait_api()
    e2.wait_terminal(timeout=90)

    started = time.time()
    f = Follower(e2.registry, "logsresume-done")
    try:
        assert f.wait_exit(timeout=30) == 0, f.proc.stderr.read()
        assert time.time() - started < 30
        starts = f.iteration_starts()
        assert {1, 2} <= set(starts) and max(starts) > 2, starts
    finally:
        f.kill()
