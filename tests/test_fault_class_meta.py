"""Task 004 (#11): the engine's fault verdict is recorded as `faultClass`.

Black-box (stub-pi) proof that the classification the infra-retry wrapper
acts on is also *persisted*, in both places an operator reads a finished or
in-flight run from: the iteration's `meta.json` on disk (served verbatim by
GET /iterations) and the `iteration.end` event in `events.jsonl`.

Scenario: one hung worker invocation (STUB_INFRA_HANG_COUNT, zero NDJSON
output -> killed by the startup-window watchdog) surrounded by healthy
iterations, so the same run carries both a null (clean) and an "infra"
verdict and neither can be produced by accident.
"""

from __future__ import annotations

import json

from test_e2e import engine_factory

__all__ = ["engine_factory"]


def _metas(run_dir):
    return [json.loads((d / "meta.json").read_text())
            for d in sorted((run_dir / "iterations").iterdir())
            if (d / "meta.json").exists()]


def _iteration_end_events(run_dir):
    lines = (run_dir / "events.jsonl").read_text().splitlines()
    return [ev for ev in (json.loads(x) for x in lines)
            if ev.get("type") == "iteration.end"]


def test_fault_class_null_on_clean_and_infra_on_hung_iteration(engine_factory):
    e = engine_factory(
        job={"on_complete": "exit", "iterations": 3, "max_approaches": 1},
        stub_env={
            "STUB_TASKS": "1",
            "STUB_INFRA_HANG_SKIP": "1",   # invocation 1 (planning) is healthy
            "STUB_INFRA_HANG_COUNT": "1",  # invocation 2 (1st worker) hangs
            "RALPHD_INFRA_STARTUP_TIMEOUT": "1",
            "RALPHD_INFRA_RETRY_MAX": "3",
            "RALPHD_INFRA_RETRY_BACKOFF_S": "0.1,0.1,0.1",
        })
    assert e.proc.wait(timeout=30) == 0
    status = json.loads((e.run_dir / "status.json").read_text())
    assert status["state"] == "succeeded"

    metas = _metas(e.run_dir)
    # Every finished iteration carries the field explicitly (never missing).
    assert all("faultClass" in m for m in metas), \
        [sorted(m) for m in metas if "faultClass" not in m]

    infra = [m for m in metas if m["faultClass"] == "infra"]
    assert len(infra) == 1, [(m["number"], m["phase"], m["faultClass"])
                             for m in metas]
    assert infra[0]["phase"] == "worker"
    assert infra[0]["noTrafficTimeout"] is True

    clean = [m for m in metas if m["number"] != infra[0]["number"]]
    assert clean, "expected healthy iterations alongside the hung one"
    assert all(m["faultClass"] is None for m in clean), \
        [(m["number"], m["phase"], m["faultClass"]) for m in clean]

    # ... and the event stream says exactly the same thing.
    events = _iteration_end_events(e.run_dir)
    assert {(m["number"], m["faultClass"]) for m in metas} == \
        {(ev["number"], ev["faultClass"]) for ev in events}
    assert [ev["number"] for ev in events if ev["faultClass"] == "infra"] == \
        [infra[0]["number"]]


def test_fault_class_null_for_a_fully_clean_run(engine_factory):
    # No injected fault at all: nothing in the run may be classified as a
    # fault (a false "infra" verdict would silently retry + refund healthy
    # iterations).
    e = engine_factory(
        job={"on_complete": "exit", "iterations": 5, "max_approaches": 1},
        stub_env={"STUB_TASKS": "1"})
    assert e.proc.wait(timeout=60) == 0
    metas = _metas(e.run_dir)
    assert metas
    assert all(m.get("faultClass", "missing") is None for m in metas), \
        [(m["number"], m["phase"], m.get("faultClass", "missing"))
         for m in metas]
    events = _iteration_end_events(e.run_dir)
    assert events
    assert all(ev["faultClass"] is None for ev in events)
