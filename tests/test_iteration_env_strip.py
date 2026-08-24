"""Task 003 (#62 candidate 2, requirement A): an iteration never inherits the
coordinates that locate or authenticate to the engine's own control-plane
HTTP API.

Fact 5 (docs/prds/v0.9-declarative-loop.md): `PiRunner.run` used to build the
child's environment as `{**os.environ, **(extra_env or {})}`, so a spawned
`pi` inherited whatever `RALPHD_PORT`/`RALPHD_API_TOKEN` the engine process
itself was started with (`docker run -e ...`) -- a probe agent that did
nothing but read its own environment reached every mutating route on the
API, `auth=True` and all. This is defence in depth, NOT the boundary itself
(candidate 1 / task 004 is): removing the coordinate removes the cheapest
route to the API, not the capability of a port scan finding it anyway (see
`artifacts/reports/requirement-a-boundary-design.md`).

Two tiers:

- unit: `PiRunner.run`'s env-construction path, driven directly against a
  tiny env-dumping stand-in for `pi` (no engine, no HTTP).
- black-box: the real engine + `tests/stub-pi/pi`, reading the stub's own
  `.stub-env.json` marker (dumped on every invocation already, for the LLM
  env-rotation tests) to see the iteration's environment from the child's
  own point of view.
"""

from __future__ import annotations

import asyncio
import json
import os
import stat

import pytest
from test_e2e import engine_factory
from test_llm_api import wait_for_env_marker

from ralphd.engine.config import CONTROL_PLANE_ENV_VARS
from ralphd.engine.runner import PiRunner

__all__ = ["engine_factory"]

# --------------------------------------------------------------------------
# unit tier: the env-construction path
# --------------------------------------------------------------------------

_ENV_DUMPER = """\
#!/usr/bin/env python3
import json, os, sys
sys.stdin.read()
with open(os.environ["ENV_DUMP_PATH"], "w") as f:
    json.dump(dict(os.environ), f)
print(json.dumps({"type": "message_end", "content": [], "usage": {}}))
"""


@pytest.fixture
def env_dumper(tmp_path):
    p = tmp_path / "fake-pi"
    p.write_text(_ENV_DUMPER)
    p.chmod(p.stat().st_mode | stat.S_IEXEC)
    return p


def test_control_plane_vars_named():
    # Pinned so a future PRD/doc change to which coordinates "locate the
    # control API" has to touch this test, not just the implementation.
    assert CONTROL_PLANE_ENV_VARS == {"RALPHD_PORT", "RALPHD_BIND", "RALPHD_API_TOKEN"}


def test_runner_strips_control_plane_vars_from_base_environ(env_dumper, tmp_path, monkeypatch):
    dump_path = tmp_path / "env.json"
    monkeypatch.setenv("RALPHD_PORT", "38123")
    monkeypatch.setenv("RALPHD_BIND", "0.0.0.0")
    monkeypatch.setenv("RALPHD_API_TOKEN", "sekret-token")
    monkeypatch.setenv("ENV_DUMP_PATH", str(dump_path))
    # A var that is NOT a control-plane coordinate must survive -- proves
    # the strip is a denylist, not "iterations get no environment at all".
    monkeypatch.setenv("RALPHD_RUN_ID", "e2e-unit")

    runner = PiRunner(tmp_path, pi_bin=str(env_dumper))
    asyncio.run(runner.run("hi", tmp_path / "transcript.jsonl", timeout_s=10))

    seen = json.loads(dump_path.read_text())
    for name in CONTROL_PLANE_ENV_VARS:
        assert name not in seen, f"{name} leaked into the iteration's environment"
    assert seen.get("RALPHD_RUN_ID") == "e2e-unit"
    assert seen.get("ENV_DUMP_PATH") == str(dump_path)


def test_runner_strips_control_plane_vars_reintroduced_by_extra_env(
        env_dumper, tmp_path, monkeypatch):
    # extra_env is the LLM-rotation override path (engine/llm.py's
    # current_env()); a future rotation reintroducing one of these names
    # must not defeat the strip either.
    dump_path = tmp_path / "env.json"
    monkeypatch.delenv("RALPHD_PORT", raising=False)
    monkeypatch.setenv("ENV_DUMP_PATH", str(dump_path))

    runner = PiRunner(tmp_path, pi_bin=str(env_dumper))
    asyncio.run(runner.run(
        "hi", tmp_path / "transcript.jsonl", timeout_s=10,
        extra_env={"RALPHD_PORT": "9999", "SOME_OTHER_VAR": "kept"}))

    seen = json.loads(dump_path.read_text())
    assert "RALPHD_PORT" not in seen
    assert seen.get("SOME_OTHER_VAR") == "kept"


# --------------------------------------------------------------------------
# black-box tier: the real engine + stub pi
# --------------------------------------------------------------------------

def test_iteration_environment_has_no_control_plane_coords(engine_factory):
    os.environ["RALPHD_API_TOKEN"] = "sekret-e2e-tok"
    try:
        e = engine_factory(job={"on_complete": "idle"})
        marker = e.run_dir / ".stub-env.json"
        seen = wait_for_env_marker(marker, predicate=lambda d: bool(d))
        assert seen, "stub never dumped its environment"
        for name in ("RALPHD_PORT", "RALPHD_API_TOKEN", "RALPHD_BIND"):
            assert name not in seen, (
                f"{name} present in the iteration's own environment: "
                "the control-plane coordinate leaked")
        # Sanity: the stub really did see ITS environment, not an empty one --
        # a coordinate the iteration legitimately needs must still be there.
        assert "RALPHD_RUN_DIR" in seen
    finally:
        del os.environ["RALPHD_API_TOKEN"]
