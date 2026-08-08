"""Black-box test: run-dir schemaVersion (PRD req 18).

Proves: a fresh run dir gets `schemaVersion` recorded in `status.json`; an
engine pointed at a run dir whose recorded `schemaVersion` is newer than
this engine build knows refuses to start (distinct nonzero exit, diagnostic
naming both versions, touches nothing else); a pre-schema run dir (no
`schemaVersion` field at all) is accepted and stamped/upgraded.
"""

from __future__ import annotations

import json

import pytest
from test_e2e import EngineProc

from ralphd.engine.state import CURRENT_SCHEMA_VERSION

# Mirrors ralphd.engine.main.EXIT_SCHEMA_TOO_NEW without importing engine
# internals into the test (this is a black-box suite) -- the value is part
# of the documented contract (docs/architecture.md).
EXIT_SCHEMA_TOO_NEW = 4


@pytest.fixture
def engine_factory(tmp_path):
    procs: list[EngineProc] = []

    def make(job: dict | None = None, stub_env: dict | None = None) -> EngineProc:
        defaults = {"run_id": "schema-e2e", "iterations": 12,
                    "max_approaches": 3, "on_complete": "exit"}
        e = EngineProc(tmp_path, {**defaults, **(job or {})}, stub_env)
        procs.append(e)
        return e

    yield make
    for e in procs:
        e.stop()


def test_fresh_run_dir_gets_schema_version_recorded(engine_factory):
    e = engine_factory()
    assert e.proc.wait(timeout=60) == 0
    status = json.loads((e.run_dir / "status.json").read_text())
    assert status["schemaVersion"] == CURRENT_SCHEMA_VERSION


def test_newer_schema_version_refused_and_touches_nothing(tmp_path, engine_factory):
    run_dir = tmp_path / "run"
    run_dir.mkdir(parents=True, exist_ok=True)
    newer = CURRENT_SCHEMA_VERSION + 1
    (run_dir / "status.json").write_text(json.dumps({
        "runId": "schema-e2e", "state": "succeeded", "schemaVersion": newer,
    }))

    e = engine_factory()
    rc = e.proc.wait(timeout=15)
    out = e.proc.stdout.read()

    assert rc == EXIT_SCHEMA_TOO_NEW, f"rc={rc}, out={out}"
    assert str(newer) in out
    assert str(CURRENT_SCHEMA_VERSION) in out
    assert "schemaVersion" in out

    # status.json itself is byte-for-byte untouched (still exactly what we
    # wrote, no schemaVersion stamp, no state/createdAt overwrite) -- the
    # refusal happens before the normal update_status()/prd-copy startup
    # writes. (The lock file and the always-created empty subdirs --
    # steering/, iterations/, approaches/, artifacts/ -- are unconditional
    # scaffolding created by every engine invocation, including the
    # pre-existing locked-run-dir refusal path; they carry no job state.)
    status = json.loads((run_dir / "status.json").read_text())
    assert status == {
        "runId": "schema-e2e", "state": "succeeded", "schemaVersion": newer,
    }
    assert not (run_dir / "tasks.json").exists()
    assert not (run_dir / "prd.md").exists()
    assert not any((run_dir / "iterations").iterdir())


def test_pre_schema_run_dir_accepted_and_upgraded(tmp_path, engine_factory):
    run_dir = tmp_path / "run"
    run_dir.mkdir(parents=True, exist_ok=True)
    # A run dir predating this feature: status.json exists with no
    # schemaVersion field at all.
    (run_dir / "status.json").write_text(json.dumps({
        "runId": "schema-e2e", "state": "succeeded",
    }))

    e = engine_factory()
    assert e.proc.wait(timeout=60) == 0
    status = json.loads((run_dir / "status.json").read_text())
    assert status["schemaVersion"] == CURRENT_SCHEMA_VERSION
