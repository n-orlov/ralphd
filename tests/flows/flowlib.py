"""Reading helpers shared by the stub-`pi` flow modules (task 005, G.5).

Every module under `tests/flows/` drives the REAL engine subprocess with the
stub `pi` binary on PATH (`engine_factory`, `tests/conftest.py`) and observes
one loop path strictly through the run's own recorded state: `events.jsonl`,
`status.json`, `tasks.json`, `petition-adjudications.json` and the workspace's
own files. Nothing here imports engine internals -- these flows are black-box
doubles of a host reading a finished run dir.

The two things this file exists to keep identical across the four flows:

1. **The step vocabulary** (`flow_steps`): `events.jsonl` reduced to one short
   string per event that marks a *step of the path* -- the phase entered, the
   iteration spawned, the verdict recorded, the signal raised, the escalation
   emitted, the adjudication recorded. A raw event list is unreadable in a
   failure message and drags in ids/timestamps/usage that are not the path; a
   reduced step list is what a reviewer can actually diff.
2. **A failure message that names the failing step** (`assert_step_sequence`,
   `assert_steps_include`): the audit's own risk note for merging 44 modules
   into ~12 flows is "loss of diff-shaped failure messages -- a merged flow
   that fails must print which step". So both helpers report the INDEX and the
   expected-vs-actual step at the first divergence, plus the whole observed
   sequence, and never a bare `assert a == b`.
"""

from __future__ import annotations

import fcntl
import json
import os
import select
import subprocess
import time
from pathlib import Path

TERMINAL_STATES = ("succeeded", "failed", "aborted")

# --- reading the run's own record -----------------------------------------

def events(run) -> list[dict]:
    """Every event line of `events.jsonl`, in order (empty list when the run
    never wrote one)."""
    log = run.run_dir / "events.jsonl"
    if not log.exists():
        return []
    out = []
    for line in log.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:  # a partial trailing write mid-run
            continue
    return out


def events_of(run, type_: str) -> list[dict]:
    return [ev for ev in events(run) if ev.get("type") == type_]


def wait_terminal_recorded(run, timeout: float = 60, interval: float = 0.05) -> dict:
    """Bounded barrier (task 006, #169): block until `events.jsonl` itself
    carries the run's terminal `state` event, then return it.

    Every flow used to read the terminal state through `status.json`
    (`LiveRun.wait_terminal()`, which polls status.json) or a bare
    `proc.wait()`, and only then reach for `flow_steps()`/`events_of(run,
    'state')` to assert the step sequence -- trusting that by the time
    status.json read terminal, the matching event was already on disk too.
    That trust is now a documented engine invariant (SPEC.md's events
    table, task 006/#169: the event is appended strictly BEFORE the write
    that makes status.json terminal), so polling status.json first and
    events.jsonl second happens to work, but it is the wrong barrier to
    poll on directly in a *test* -- it duplicates the polling loop in every
    module that needs it, each with its own timeout and its own way of
    rendering a timeout failure. This is the one barrier: it polls
    `events.jsonl` directly for the terminal `state` event (never
    status.json), and raises with the events actually seen so far -- never a
    bare `TimeoutError()` -- when the deadline passes first.
    """
    deadline = time.monotonic() + timeout
    while True:
        for ev in events(run):
            if ev.get("type") == "state" and ev.get("state") in TERMINAL_STATES:
                return ev
        if time.monotonic() >= deadline:
            seen = [ev.get("type") for ev in events(run)]
            raise TimeoutError(
                f"wait_terminal_recorded: no terminal `state` event landed in "
                f"events.jsonl within {timeout}s; event types seen so far: "
                f"{seen!r}")
        time.sleep(interval)


def wait_status_terminal(run, timeout: float = 60, interval: float = 0.05) -> dict:
    """Bounded barrier (task 002, requirement M): block until `status.json`
    itself reports a finished run (`state` in `TERMINAL_STATES`), then
    return it, and raise with the last status observed when the deadline
    passes first.

    SPEC.md's ordering invariant (§5.4, "A run's terminal `state` event is
    appended before the status.json write that makes `state` itself read
    as terminal") only orders the two writes -- it never says they land in
    the same instant. `wait_terminal_recorded` blocking on `events.jsonl`
    and then a caller reading `status.json` straight off disk is racing the
    SECOND of those two writes, the one the invariant only promises comes
    after the first: the terminal `state` event can be on disk for as long
    as that second write takes to land, during which `status.json` still
    reads a pre-terminal state (observed on nightly, run 34921161300:
    `status.json` read `running` immediately after the events barrier
    returned). This is the second, narrower barrier for exactly that gap:
    it polls `status.json` directly (never `events.jsonl`) and is the one a
    caller should read a finished run's status through once the events
    barrier has already returned.
    """
    deadline = time.monotonic() + timeout
    last: dict = {}
    while True:
        last = status(run)
        if last.get("state") in TERMINAL_STATES:
            return last
        if time.monotonic() >= deadline:
            raise TimeoutError(
                f"wait_status_terminal: status.json never reported a "
                f"terminal state within {timeout}s; last status observed: "
                f"{last!r}")
        time.sleep(interval)


def wait_step_recorded(run, step: str, timeout: float = 30,
                       interval: float = 0.05) -> list[str]:
    """Bounded barrier (task 063, #179): block until `step` (exactly as
    `flow_steps()` renders it, e.g. `"iteration:worker"`) has actually
    landed in `events.jsonl`, then return the full reduced sequence seen at
    that moment.

    `tests/flows/test_flow_steer.py` used to POST to `/steering` the
    instant `wait_api()` returned -- proving only that the HTTP server was
    up, never that the engine had gotten as far as dispatching the worker
    iteration whose `iteration.start` event the flow's asserted sequence
    expects to precede `steering.received`. On a slow or loaded host the
    POST's own event could land in `events.jsonl` first, reversing the two
    steps and failing `assert_step_sequence` at that index (observed on
    GitHub-hosted 2-core runners: run 34350174011 attempt 1, and the push
    of d2ec329, run 34361695755). This barrier polls the same reduced
    sequence `assert_step_sequence` itself checks and returns the moment
    the named step is actually there -- never a fixed sleep, never a wider
    or order-insensitive assertion -- so a caller can order its own next
    action strictly after that step without guessing how long the engine
    takes to reach it."""
    deadline = time.monotonic() + timeout
    while True:
        seen = flow_steps(run)
        if step in seen:
            return seen
        if time.monotonic() >= deadline:
            raise TimeoutError(
                f"wait_step_recorded: step {step!r} never landed in "
                f"events.jsonl within {timeout}s; steps seen so far: {seen!r}")
        time.sleep(interval)


# --- waiting for the engine subprocess to exit, with a real diagnosis on a
# timeout (task 016, F.1/F.5, #216) -----------------------------------------

def wait_engine_exit(run, timeout: float = 60, *, tail: int = 20) -> int:
    """The one barrier every flow uses to wait for the engine subprocess to
    exit: `run.proc.wait(timeout=timeout)`, unchanged. The only thing this
    adds is what happens on a `TimeoutExpired` -- instead of that bare,
    diagnosis-free exception, the failure carries the engine's own process
    tree (state + wchan for the engine pid and every live descendant),
    whatever bytes are sitting unread in its stdout pipe, `status.json`, and
    an `events.jsonl` tail, and it NAMES which of the two shapes #216's hunt
    found it is looking at -- a hung child process still alive past this
    wait's own budget, or a parked run sitting in `_park_until_resumed`
    (which ignores SIGTERM, per `LiveRun.stop`'s own docstring) -- rather
    than leaving the reader to infer it from the dump.

    **F.3's acceptance boundary, restated here because it travels with the
    code, not only with the PRD**: this helper's obligation is the DUMP --
    the helper existing, a synthetic hang exercising it and asserting the
    dump's content, and the helper wired into the node's wait (task 017,
    F.2/F.5) -- never reproducing the CI flake itself. #216's hunt ran
    ~100+ executions, including on the real CI host, and did not reproduce
    it; that is not grounds to reject this requirement, and closing #216 for
    good plausibly needs a soak in the reviewer's own environment (memory
    pressure was the only lever that ever moved it) that no test in this
    repo can manufacture. F.4 (also restated): no bound is raised and no
    barrier sits ahead of the caller's own `timeout` -- this function is a
    thin wrapper around the SAME single `proc.wait(timeout=timeout)`, and
    the dump is built only in the `except` branch, strictly after that one
    wait already expired.
    """
    try:
        return run.proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        raise AssertionError(_engine_exit_timeout_dump(run, timeout, tail=tail)) from None


def _engine_exit_shape(run) -> str:
    """Which of #216's two shapes this timeout looks like, in words a
    failure message can quote directly: a PARKED RUN (status.json reads
    `state: "paused"` -- `_park_until_resumed`, which ignores SIGTERM) or a
    HUNG CHILD (the process is simply still alive, nothing paused it)."""
    st = status(run)
    if st.get("state") == "paused":
        return (f"a parked run -- status.json reads state='paused', "
                f"pauseReason={st.get('pauseReason')!r} (_park_until_resumed "
                f"ignores SIGTERM, per LiveRun.stop's own docstring)")
    return (f"a hung child process -- pid {run.proc.pid} is still alive past "
            f"this wait's own budget and status.json does not read "
            f"state='paused'")


def _pid_children(pid: int) -> list[int]:
    """Every direct child of `pid`, read off `/proc/<pid>/task/*/children`
    (best-effort: a pid that has already exited, or that this process lacks
    permission to inspect, simply contributes no children)."""
    out: list[int] = []
    task_dir = Path(f"/proc/{pid}/task")
    try:
        tid_dirs = list(task_dir.iterdir())
    except OSError:
        return out
    for tid_dir in tid_dirs:
        try:
            text = (tid_dir / "children").read_text().strip()
        except OSError:
            continue
        if text:
            out.extend(int(x) for x in text.split())
    return out


def _process_tree(root_pid: int) -> list[int]:
    """`root_pid` and every live descendant, breadth-first, each pid listed
    exactly once."""
    seen = [root_pid]
    frontier = [root_pid]
    while frontier:
        nxt: list[int] = []
        for pid in frontier:
            for child in _pid_children(pid):
                if child not in seen:
                    seen.append(child)
                    nxt.append(child)
        frontier = nxt
    return seen


def _proc_state(pid: int) -> str:
    """`/proc/<pid>/status`'s own `State:` line (e.g. `"S (sleeping)"`), or
    `"<exited>"` for a pid that is gone by the time this reads it."""
    try:
        text = Path(f"/proc/{pid}/status").read_text()
    except OSError:
        return "<exited>"
    for line in text.splitlines():
        if line.startswith("State:"):
            return line.split(":", 1)[1].strip()
    return "<unknown>"


def _proc_wchan(pid: int) -> str:
    """`/proc/<pid>/wchan`'s contents (the kernel function the process is
    blocked in, empty when running), or `"<unavailable>"` for a pid this
    process cannot read it for (already exited, or no permission)."""
    try:
        text = Path(f"/proc/{pid}/wchan").read_text().strip()
    except OSError:
        return "<unavailable>"
    return text or "<running, no wchan>"


def _pending_pipe_bytes(proc) -> tuple[int, str]:
    """Bytes sitting unread in `proc.stdout` right now, read non-blocking so
    this never itself hangs the diagnosis. Draining them is fine here --
    this only runs once the caller has already decided to fail."""
    stream = getattr(proc, "stdout", None)
    if stream is None:
        return 0, "<no stdout pipe>"
    try:
        fd = stream.fileno()
    except (OSError, ValueError):
        return 0, "<stdout pipe closed>"
    chunks: list[bytes] = []
    try:
        flags = fcntl.fcntl(fd, fcntl.F_GETFL)
        fcntl.fcntl(fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)
        try:
            while True:
                ready, _, _ = select.select([fd], [], [], 0)
                if not ready:
                    break
                chunk = os.read(fd, 65536)
                if not chunk:
                    break
                chunks.append(chunk)
        finally:
            fcntl.fcntl(fd, fcntl.F_SETFL, flags)
    except OSError:
        return 0, "<pipe read failed>"
    data = b"".join(chunks)
    return len(data), data.decode("utf-8", errors="replace")


def _engine_exit_timeout_dump(run, timeout: float, *, tail: int = 20) -> str:
    """The four-section dump F.1 asks for, plus the shape name F.2 asks for
    named up front -- a hung child vs. a parked run, never left for the
    reader to infer."""
    shape = _engine_exit_shape(run)
    pid = run.proc.pid
    tree_pids = _process_tree(pid)
    tree_lines = "\n".join(
        f"  pid={p} state={_proc_state(p)!r} wchan={_proc_wchan(p)!r}"
        for p in tree_pids) or "  (no live pid found)"
    pipe_len, pipe_text = _pending_pipe_bytes(run.proc)
    status_text = json.dumps(status(run), indent=2, sort_keys=True)
    log = run.run_dir / "events.jsonl"
    lines = log.read_text().splitlines() if log.exists() else []
    tail_lines = lines[-tail:]
    events_tail = ("\n".join(f"  {line}" for line in tail_lines)
                  if tail_lines else "  (events.jsonl empty or absent)")
    return (
        f"wait_engine_exit: the engine did not exit within {timeout}s -- "
        f"observed shape: {shape}\n\n"
        f"process tree (pid {pid} and its live descendants, state + wchan):\n"
        f"{tree_lines}\n\n"
        f"pending pipe bytes ({pipe_len} bytes read from stdout without "
        f"blocking):\n  {pipe_text!r}\n\n"
        f"status.json:\n{status_text}\n\n"
        f"events.jsonl tail (last {tail} lines):\n{events_tail}\n\n"
        f"F.3 (#216): this dump is the obligation; reproducing the CI flake "
        f"is not -- ~100+ hunt attempts including on the real CI host never "
        f"reproduced it, and that is not grounds to reject this diagnosis.")


def status(run) -> dict:
    f = run.run_dir / "status.json"
    if not f.exists():
        return {}
    try:
        return json.loads(f.read_text())
    except json.JSONDecodeError:  # mid-write
        return {}


def tasks(run) -> list[dict]:
    f = run.run_dir / "tasks.json"
    if not f.exists():
        return []
    return json.loads(f.read_text()).get("tasks", [])


def flow_steps(run) -> list[str]:
    """`events.jsonl` reduced to the path's own steps, in order.

    One string per step-marking event; every other event type (budget,
    token usage, task-status churn, notifications) is dropped -- those are
    asserted directly off `status.json`/`tasks.json` by whichever flow cares,
    and carrying them here would make every sequence assertion brittle
    against an unrelated new event type.
    """
    steps: list[str] = []
    for ev in events(run):
        t = ev.get("type")
        if t == "state":
            steps.append(f"state:{ev.get('state')}")
        elif t == "phase":
            step = f"phase:{ev.get('phase')}@{ev.get('approach')}"
            if ev.get("midRunAdjudication"):
                step += ":midrun"
            steps.append(step)
        elif t == "iteration.start":
            steps.append(f"iteration:{ev.get('phase')}")
        elif t == "approach.verdict_recorded":
            steps.append(
                f"verdict:{ev.get('channel')}:{ev.get('verdict')}@{ev.get('approach')}")
        elif t == "signal":
            steps.append(f"signal:{ev.get('signal')}")
        elif t == "operator_escalation":
            steps.append(f"escalation:{ev.get('reason')}@{ev.get('approach')}")
        elif t == "petition_adjudications_recorded":
            steps.append("adjudications_recorded")
        elif t == "approach.cure_pass":
            steps.append(
                f"cure_pass:{ev.get('curePasses')}@{ev.get('approach')}")
        # Task 014 (C.3): the steer path's own five step-marking event
        # types -- received/consumed (state.py's `RunDir.add_steering`/
        # `consume_steering`), the hat's own settlement claim
        # (`LoopSupervisor._step_generic_hat`'s `steer.settled`), and the
        # two tool-surface audit events (`hat_tools.EVENT_TOOL_CALL`/
        # `EVENT_CONTEXT_WRITE`) a `task-insert`/`standing-rules` call
        # emits. No other flow drives any of these, so adding them here
        # cannot perturb an existing flow's reduced sequence.
        elif t == "steering.received":
            steps.append(f"steering:received:{ev.get('file')}")
        elif t == "steering.consumed":
            steps.append(f"steering:consumed:{ev.get('file')}")
        elif t == "steer.settled":
            steps.append(f"steer:settled:{ev.get('settled')}")
        elif t == "steer.tool_call":
            steps.append(f"steer_tool:{ev.get('tool')}:{ev.get('ok')}")
        elif t == "steer.context_write":
            steps.append(f"steer_write:{ev.get('file')}")
        # Task 015 (C.4): the infra-retry wrapper's own three step-marking
        # event types (`loop.py`'s `_run_iteration_with_infra_retry` /
        # `_begin_infra_wait` / `_reset_infra_episode`) -- one attempt
        # (`infra_retry`), one backoff wait beginning (`infra_wait`), one
        # episode ending because an iteration finally reached the model
        # again (`infra_recovered`). No other flow drives a hung stub-`pi`
        # invocation, so adding these cannot perturb an existing flow's
        # reduced sequence.
        elif t == "infra_retry":
            steps.append(f"infra_retry:{ev.get('phase')}:{ev.get('attempt')}")
        elif t == "infra_wait":
            steps.append(f"infra_wait:{ev.get('phase')}:{ev.get('attempt')}")
        elif t == "infra_recovered":
            steps.append("infra_recovered")
    return steps


# --- sequence assertions that name the failing step ------------------------

def _rendered(actual: list[str]) -> str:
    return "\n".join(f"  [{i}] {s}" for i, s in enumerate(actual)) or "  (no steps)"


def assert_step_sequence(actual: list[str], expected: list[str], *,
                         flow: str) -> None:
    """`actual` IS `expected`, step for step -- and on any difference, say
    which step index diverged and what stood there instead."""
    for i, want in enumerate(expected):
        got = actual[i] if i < len(actual) else "<nothing: sequence ended>"
        assert got == want, (
            f"{flow}: flow step [{i}] should be {want!r} but was {got!r}\n"
            f"observed sequence:\n{_rendered(actual)}")
    assert len(actual) == len(expected), (
        f"{flow}: flow step [{len(expected)}] should not exist, but the run "
        f"recorded {actual[len(expected)]!r} after the expected sequence "
        f"ended\nobserved sequence:\n{_rendered(actual)}")


def assert_steps_include(actual: list[str], expected: list[str], *,
                         flow: str) -> None:
    """`expected` appears in `actual` IN ORDER (other steps may sit between)
    -- and on a miss, say which expected step was never reached and how far
    the match got."""
    i = 0
    for want in expected:
        while i < len(actual) and actual[i] != want:
            i += 1
        assert i < len(actual), (
            f"{flow}: flow step {want!r} never appears (in order) in the run's "
            f"own record; matched {expected[:expected.index(want)]!r} first\n"
            f"observed sequence:\n{_rendered(actual)}")
        i += 1
