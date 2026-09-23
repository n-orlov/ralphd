"""Task 010 (#28): an iteration that exceeds its timeout fails the ITERATION.

The defect: `PiRunner.run`'s timeout path did

    try:
        await asyncio.wait_for(pump_task, timeout=timeout_s)
    except TimeoutError:
        ...
        await asyncio.wait_for(pump_task, timeout=30)   # already cancelled!

`asyncio.wait_for` cancels the future it timed out on before raising, so the
second await re-awaited an *already cancelled* task -- which re-raises
`CancelledError`. That is a `BaseException`, so it slipped past every
`except Exception` between the runner and the loop's per-iteration guard: a
single iteration blowing its timeout took the whole engine down instead of
costing one failed iteration.

Two levels of proof, both fast (compressed timeouts, no long sleeps):

* the runner in isolation returns an `IterationResult(timed_out=True)`
  instead of raising, even when the agent ignores the SIGINT that follows;
* black box, the real engine: the timed-out iteration is recorded
  (`meta.json` with `endedAt`, `timedOut`, a fault class), the engine
  survives it, starts the next iteration, and the run does not go terminal
  because of it.

Mutation case (recorded in the commit message): restoring the two
`asyncio.wait_for(pump_task, ...)` calls makes
test_runner_returns_a_timed_out_result_instead_of_raising and
test_a_timed_out_iteration_costs_one_iteration_not_the_engine fail.
"""

from __future__ import annotations

import asyncio
import json
import os
import stat

import pytest

from ralphd.engine import runner as runner_mod
from ralphd.engine.runner import PiRunner
from tests.flows.flowlib import wait_engine_exit


__all__ = ["engine_factory"]


# -- the runner in isolation ----------------------------------------------

def _fake_pi(tmp_path, *, obey_sigint: bool):
    """A stand-in agent that emits one NDJSON event and then hangs forever.

    `obey_sigint=False` additionally ignores SIGINT, which is the worse half
    of the shape: the runner must still come back with a verdict rather than
    raise, and must not wait out its whole shutdown grace before doing so.

    Task 001 (v0.20-a6, B-001): this used to be a `#!{sys.executable}`
    script -- a full CPython cold start (import `json`/`signal`/`sys`/`time`,
    parse and compile this literal) sitting entirely *inside* the 0.2 s
    `timeout_s` window `test_runner_returns_a_timed_out_result_instead_of_raising`
    races against, before the process has even read stdin or printed its one
    event. Measured locally (`taskset -c 0`, six competing busy loops on the
    same core -- see `b001-cure.md`): the python-shebang version's spawn to
    first-byte latency ran ~80 ms under that contention, leaving only ~2.5x
    headroom against the 200 ms window; a `/bin/sh` script with no
    interpreter/import cost measured ~10-20 ms, an order of magnitude more
    headroom for the same window. `/bin/sh` has no `signal.signal` call to
    reach for, so the deaf variant instead `trap`s SIGINT to be ignored and
    then `exec`s into the sleep -- a POSIX-ignored disposition survives
    `exec` (unlike a caught one), so the replacement process (`sleep`, now
    running as this same pid) stays exactly as deaf as the python version's
    `SIG_IGN` did, and the runner's SIGKILL fallback is still what ends it.
    """
    script = tmp_path / ("fake-pi-deaf" if not obey_sigint else "fake-pi")
    ignore = "trap '' INT\n" if not obey_sigint else ""
    script.write_text(
        "#!/bin/sh\n"
        f"{ignore}"
        "cat >/dev/null\n"
        "echo '{\"type\": \"message_end\", \"message\": {\"role\": \"assistant\","
        " \"content\": [{\"type\": \"text\", \"text\": \"hi\"}],"
        " \"usage\": {\"input\": 1, \"output\": 1, \"totalTokens\": 2}}}'\n"
        "exec sleep 9999\n")
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return script


@pytest.mark.parametrize("obey_sigint", [True, False],
                         ids=["agent-obeys-sigint", "agent-ignores-sigint"])
def test_runner_returns_a_timed_out_result_instead_of_raising(
        tmp_path, monkeypatch, obey_sigint):
    # Compressed grace so the deaf-agent case finishes in well under 1s, not
    # ~30s (task 048, B.5 follow-on): the deaf-agent path waits this grace
    # out TWICE in `PiRunner.run` (once for the pump, once for the process
    # itself), so shrinking it here shaves both legs at once without
    # touching the real 30s production default or the behaviour under test.
    monkeypatch.setattr(runner_mod, "SHUTDOWN_GRACE_S", 0.2)
    pi = _fake_pi(tmp_path, obey_sigint=obey_sigint)
    r = PiRunner(tmp_path, pi_bin=str(pi))

    async def go():
        return await r.run("prompt", tmp_path / "output.jsonl", timeout_s=0.2)

    result = asyncio.run(go())          # must NOT raise CancelledError
    assert result.timed_out is True
    assert result.no_traffic_timeout is False   # traffic WAS observed
    assert result.duration_s is not None
    # The transcript the pump wrote before the timeout is still on disk.
    assert "message_end" in (tmp_path / "output.jsonl").read_text()
    # No agent process is left behind by the timeout path.
    assert r.running is False


def test_runner_timeout_leaves_no_orphan_process(tmp_path, monkeypatch):
    # Task 048 (B.5 follow-on): same double-grace shrink as the test above.
    monkeypatch.setattr(runner_mod, "SHUTDOWN_GRACE_S", 0.2)
    pi = _fake_pi(tmp_path, obey_sigint=False)
    r = PiRunner(tmp_path, pi_bin=str(pi))
    pids: list[int] = []

    async def go():
        task = asyncio.ensure_future(
            r.run("prompt", tmp_path / "output.jsonl", timeout_s=0.2))
        while not pids:
            if r.running:
                pids.append(r._proc.pid)
            await asyncio.sleep(0.05)
        return await task

    assert asyncio.run(go()).timed_out is True
    with pytest.raises(OSError):
        os.kill(pids[0], 0)


# -- black box: the real engine over a real timeout ------------------------

def _metas(run_dir):
    return [json.loads((d / "meta.json").read_text())
            for d in sorted((run_dir / "iterations").iterdir())
            if (d / "meta.json").exists()]


@pytest.fixture
def timed_out_run(engine_factory):
    """One worker iteration (invocation 2, after planning) hangs past a
    3-second iteration timeout; everything else is healthy."""
    e = engine_factory(
        job={"on_complete": "exit", "iterations": 6, "max_approaches": 1,
             "iteration_timeout_s": 3},
        stub_env={"STUB_TASKS": "1",
                  "STUB_TIMEOUT_HANG_SKIP": "1",
                  "STUB_TIMEOUT_HANG_COUNT": "1"})
    exit_code = wait_engine_exit(e, timeout=90)
    return e, exit_code


def test_a_timed_out_iteration_costs_one_iteration_not_the_engine(timed_out_run):
    e, exit_code = timed_out_run
    metas = _metas(e.run_dir)
    timed = [m for m in metas if m.get("timedOut")]
    assert len(timed) == 1, [(m["number"], m["phase"], m.get("timedOut"))
                             for m in metas]
    hung = timed[0]

    # (a) the iteration itself is fully recorded as a finished, failed one
    assert hung["phase"] == "worker"
    assert hung["endedAt"]                      # reached the normal recording path
    assert hung["noTrafficTimeout"] is False    # it DID produce traffic
    assert hung["sawComplete"] is False
    assert hung["faultClass"] is not None       # classified, not a clean success

    # (b) the engine survived it and started the next iteration
    later = [m for m in metas if m["number"] > hung["number"]]
    assert later, [m["number"] for m in metas]
    assert later[0]["endedAt"]

    # (c) the run did not go terminal because of the timeout: it went on to
    #     finish its work normally.
    status = json.loads((e.run_dir / "status.json").read_text())
    assert status["state"] == "succeeded", status.get("reason")
    assert status["verdict"] == "verified"
    assert exit_code == 0
    tasks = json.loads((e.run_dir / "tasks.json").read_text())["tasks"]
    assert tasks and all(t["status"] == "completed" for t in tasks)


def test_the_timeout_never_surfaces_as_an_engine_crash(timed_out_run):
    e, _ = timed_out_run
    out = e.proc.stdout.read() if e.proc.stdout else ""
    assert "CancelledError" not in out
    assert "Traceback" not in out


def test_a_timed_out_iteration_is_charged_and_not_refunded(timed_out_run):
    # Budget bookkeeping still applies on the timeout path: a timed-out
    # iteration that reached the model is work, not an infra fault, so it is
    # charged (no refund) and no retry episode is opened for it.
    e, _ = timed_out_run
    status = json.loads((e.run_dir / "status.json").read_text())
    metas = _metas(e.run_dir)
    assert status["iterationsUsed"] == max(m["number"] for m in metas)
    events = [json.loads(x) for x in
              (e.run_dir / "events.jsonl").read_text().splitlines()]
    assert [ev for ev in events if ev.get("type") == "iteration.end"
            and ev["number"] == [m["number"] for m in metas
                                 if m.get("timedOut")][0]]
    assert not [ev for ev in events if ev.get("type") == "infra_retry"]
