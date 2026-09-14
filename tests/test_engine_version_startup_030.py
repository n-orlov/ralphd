"""F.4 -- the engine records its own `ralphd.__version__` into `status.json`
at start and emits one startup event naming it, unconditionally, on EVERY
run -- never only on a workspace-version mismatch the way E.3's
`workspace_version_mismatch` is (task 030, PRD requirement F.4, #159/#194).

Drives a real `LoopSupervisor(...)` constructor over a real run dir, the
same `make_supervisor` harness `tests/test_workspace_version_drift.py` uses
for E.3 -- never a mock of `_record_engine_version` itself. The workspace
here carries NO `src/ralphd/__init__.py` at all, so `_check_workspace_
version_drift` stays silent (no readable literal to compare): this module
proves F.4 fires anyway, on a run with no drift.
"""

from __future__ import annotations

import json
from pathlib import Path

from ralphd import __version__ as ENGINE_VERSION
from ralphd.engine import run_record
from tests.conftest import make_supervisor


def _events(root: Path, type_: str) -> list[dict]:
    log = root / "events.jsonl"
    lines = log.read_text().splitlines() if log.exists() else []
    return [ev for ev in (json.loads(x) for x in lines) if ev.get("type") == type_]


def test_status_json_and_one_startup_event_carry_the_engine_version_on_a_drift_free_run(
        tmp_path):
    root = tmp_path / "run"
    root.mkdir()
    ws = root / "ws"
    ws.mkdir()  # no src/ralphd/__init__.py at all -- no workspace version
                # literal to compare, so E.3's drift check stays silent.

    make_supervisor(root, workspace=ws, cfg_defaults=False)

    status = json.loads((root / "status.json").read_text())
    assert status["engineVersion"] == ENGINE_VERSION

    started = _events(root, run_record.ENGINE_STARTED_EVENT)
    assert len(started) == 1
    assert started[0]["engineVersion"] == ENGINE_VERSION

    # And, on this drift-free run, E.3's own event stays exactly what it
    # already promised: silent.
    assert _events(root, "workspace_version_mismatch") == []
