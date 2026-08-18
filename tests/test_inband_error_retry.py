"""Task 005 (#11): an exit-0, in-band provider error is retried and refunded.

The live shape this pins down: pi reaches the gateway, the gateway fails, and
pi reports it *in band* -- one `message_end` with `stopReason: "error"`, an
infra-shaped `errorMessage` ("Connection error."), zero token usage, no
sentinel -- and then shuts down cleanly with exit code 0. Before task 001 the
engine keyed "did this iteration fail?" off the exit code alone, so such an
iteration was scored a success: no retry, no refund, and one iteration of the
budget spent on an iteration that never ran.

Black-box only (stub-pi + the real engine): the assertions read events.jsonl,
status.json and the iteration meta.json the same way an operator would.

The stub's in-band error is delayed past LoopSupervisor's 5s instant-failure
window on purpose -- an *instant* no-traffic failure is still handled by the
pre-existing broken-environment carve-out (task 010 changes that), and this
test is about the retry path. Everything else runs on a compressed backoff
schedule (RALPHD_INFRA_RETRY_BACKOFF_S), never a real minute-long sleep.
"""

from __future__ import annotations

import json

from test_e2e import engine_factory

__all__ = ["engine_factory"]

# > INSTANT_FAILURE_MAX_DURATION_S (5.0), small enough to keep the suite fast.
INBAND_DELAY_S = "6"


def _events(run_dir, type_):
    return [ev for ev in (json.loads(line) for line in
                          (run_dir / "events.jsonl").read_text().splitlines())
            if ev.get("type") == type_]


def _metas(run_dir):
    return [json.loads((d / "meta.json").read_text())
            for d in sorted((run_dir / "iterations").iterdir())
            if (d / "meta.json").exists()]


def test_inband_exit0_error_is_retried_refunded_and_job_completes(engine_factory):
    # Charged budget is exactly the happy path (planning + 1 worker + review
    # == 3). One worker invocation fails in band with exit 0 before the
    # healthy one; if that attempt were charged, review would never run and
    # the job could not reach a verified terminal state.
    e = engine_factory(
        job={"on_complete": "exit", "iterations": 3, "max_approaches": 1},
        stub_env={
            "STUB_TASKS": "1",
            "STUB_INBAND_ERROR_SKIP": "1",   # invocation 1 (planning) is healthy
            "STUB_INBAND_ERROR_COUNT": "1",  # invocation 2 (1st worker) errors
            "STUB_INBAND_ERROR_DELAY_S": INBAND_DELAY_S,
            "RALPHD_INFRA_RETRY_MAX": "3",
            "RALPHD_INFRA_RETRY_BACKOFF_S": "0.1,0.1,0.1",
        })
    assert e.proc.wait(timeout=60) == 0

    status = json.loads((e.run_dir / "status.json").read_text())
    assert status["state"] == "succeeded"
    assert status["verdict"] == "verified"

    # The failed attempt was retried as an infra fault...
    infra = _events(e.run_dir, "infra_retry")
    assert len(infra) == 1, infra
    assert infra[0]["phase"] == "worker"
    assert infra[0]["attempt"] == 1
    assert "Connection error" in infra[0]["error"]
    assert infra[0]["noTrafficTimeout"] is False

    # ... and refunded: 4 attempts ran, only the 3 real iterations are charged.
    metas = _metas(e.run_dir)
    assert len(metas) == 4, [(m["number"], m["phase"]) for m in metas]
    assert status["iterationsUsed"] == 3

    # The in-band failure is on record as an infra fault despite exit 0 and is
    # the only classified fault in the run.
    faulted = [m for m in metas if m["faultClass"] is not None]
    assert len(faulted) == 1, [(m["number"], m["phase"], m["faultClass"])
                              for m in metas]
    bad = faulted[0]
    assert (bad["phase"], bad["faultClass"], bad["exitCode"]) == ("worker", "infra", 0)
    assert "Connection error" in bad["error"]
    assert bad["usage"]["totalTokens"] == 0


def test_inband_errors_exhausting_retries_end_terminal_without_burning_budget(
        engine_factory):
    # Every worker attempt fails in band with exit 0: the run must end in a
    # terminal state naming the infra fault, without switching approaches and
    # without the refunded attempts eating the iteration budget.
    e = engine_factory(
        job={"on_complete": "exit", "iterations": 12, "max_approaches": 3},
        stub_env={
            "STUB_TASKS": "1",
            "STUB_INBAND_ERROR_SKIP": "1",
            "STUB_INBAND_ERROR_COUNT": "10",
            "STUB_INBAND_ERROR_DELAY_S": INBAND_DELAY_S,
            "RALPHD_INFRA_RETRY_MAX": "2",
            "RALPHD_INFRA_RETRY_BACKOFF_S": "0.1,0.1",
        })
    assert e.proc.wait(timeout=90) == 1

    status = json.loads((e.run_dir / "status.json").read_text())
    assert status["state"] == "aborted"
    assert "infra fault" in status["reason"].lower()
    assert "connection error" in status["reason"].lower()
    assert status["approach"] == 1

    infra = _events(e.run_dir, "infra_retry")
    assert [ev["attempt"] for ev in infra] == [1, 2]
    # planning (charged) + 2 refunded worker attempts
    assert status["iterationsUsed"] == 1
