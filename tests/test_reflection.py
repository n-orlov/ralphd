"""Black-box tests: self-reflection phase (PRD req 24).

`reflect: true` runs one extra 'reflect' iteration after the job reaches a
terminal state, writing a report + suggested diff to `artifacts/reflection/`
and touching nothing else (workspace, tasks.json, status verdict). With
`reflect` absent, no extra iteration runs at all.
"""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path

import pytest
from test_e2e import EngineProc


@pytest.fixture
def engine_factory(tmp_path):
    procs: list[EngineProc] = []

    def make(job: dict | None = None, stub_env: dict | None = None) -> EngineProc:
        defaults = {"run_id": "reflect-e2e", "iterations": 12,
                    "max_approaches": 1, "on_complete": "idle"}
        e = EngineProc(tmp_path, {**defaults, **(job or {})}, stub_env)
        procs.append(e)
        return e

    yield make
    for e in procs:
        e.stop()


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_reflect_runs_after_terminal_state_leaves_run_state_untouched(engine_factory):
    e = engine_factory(job={"reflect": True, "iterations": 6, "on_complete": "exit"},
                       stub_env={"STUB_TASKS": "1", "STUB_SLEEP": "1.5"})
    e.wait_api()

    # STUB_SLEEP delays every non-worker phase (incl. reflect) so we get a
    # real window between "job reached terminal state" and "reflect finished
    # writing its report" to snapshot run-state/workspace before vs. after.
    status = e.wait_state(("succeeded", "failed", "aborted"), timeout=30)
    assert status["state"] == "succeeded"
    assert status["verdict"] == "verified"

    tasks_before = e.run_dir / "tasks.json"
    ws_file = e.workspace / "probe.txt"
    ws_file.write_text("workspace probe content\n")
    before_tasks_sha = _sha(tasks_before)
    before_ws_sha = _sha(ws_file)
    before_verdict = status["verdict"]

    report = e.run_dir / "artifacts" / "reflection" / "report.md"
    diff = e.run_dir / "artifacts" / "reflection" / "suggestions.diff"
    deadline = time.time() + 15
    while time.time() < deadline and not report.exists():
        time.sleep(0.1)
    assert report.exists(), "reflect iteration never wrote artifacts/reflection/report.md"
    assert diff.exists()

    # the engine process (and thus run_job(), including the reflect
    # iteration) has fully finished by the time --on-complete=exit tears
    # the server down
    assert e.proc.wait(timeout=30) == 0

    # -- run state / workspace untouched by the reflect iteration ----------
    assert _sha(tasks_before) == before_tasks_sha
    assert _sha(ws_file) == before_ws_sha
    final_status = json.loads((e.run_dir / "status.json").read_text())
    assert final_status["verdict"] == before_verdict
    assert final_status["state"] == "succeeded"
    assert final_status["phase"] is None  # restored after the reflect iteration

    # -- the reflect iteration itself: own prompt, own phase, model per
    # strategy (default quality-first -> the (only) configured model tier)
    iters = sorted((e.run_dir / "iterations").iterdir())
    metas = [json.loads((d / "meta.json").read_text()) for d in iters]
    assert metas[-1]["phase"] == "reflect"
    assert metas[-1]["endedAt"]
    prompt = (iters[-1] / "prompt.md").read_text()
    assert "Role: Reflector" in prompt


def test_reflect_absent_runs_no_extra_iteration(engine_factory):
    e = engine_factory(job={"iterations": 6, "on_complete": "exit"},
                       stub_env={"STUB_TASKS": "1"})
    assert e.proc.wait(timeout=30) == 0
    status = json.loads((e.run_dir / "status.json").read_text())
    assert status["state"] == "succeeded"
    iters = sorted((e.run_dir / "iterations").iterdir())
    metas = [json.loads((d / "meta.json").read_text()) for d in iters]
    assert "reflect" not in {m["phase"] for m in metas}
    assert not (e.run_dir / "artifacts" / "reflection").exists()


def test_reflect_model_resolved_per_strategy(engine_factory):
    e = engine_factory(
        job={"reflect": True, "iterations": 6, "on_complete": "exit",
            "model": "strong-model-x", "fast_model": "fast-model-y",
            "model_strategy": "cost-optimized"},
        stub_env={"STUB_TASKS": "1"})
    assert e.proc.wait(timeout=30) == 0
    iters = sorted((e.run_dir / "iterations").iterdir())
    metas = [json.loads((d / "meta.json").read_text()) for d in iters]
    reflect_metas = [m for m in metas if m["phase"] == "reflect"]
    assert len(reflect_metas) == 1
    # cost-optimized maps reflect -> the "fast" tier, mirroring review/verify/worker
    assert reflect_metas[0]["model"] == "fast-model-y"
