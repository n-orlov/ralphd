"""F.5 -- the engine version survives the container: `run-record.md` (and
`run-record.json` where it is written) carries the engine version, so a
removed container does not take the answer with it (task 031, PRD
requirement F.5, #159/#194).

`_record_engine_version()` (F.4, task 030) already writes `status.json`'s
`engineVersion` unconditionally at construction; this proves `_finish()`'s
run-record write (the same path B.1/task 006 wires) carries that same value
into both `run-record.json` and `run-record.md` on a terminal run dir --
never a mock of `_record_engine_version` or of `write_run_record` itself.
"""

from __future__ import annotations

import json
from pathlib import Path

from ralphd import __version__ as ENGINE_VERSION
from ralphd.engine import loop as loop_mod
from ralphd.engine.state import RunDir
from tests.conftest import make_supervisor


def _run(tmp_path: Path) -> RunDir:
    return RunDir(root=tmp_path / "run")


def test_terminal_run_dir_carries_the_engine_version_in_both_record_files(
        tmp_path, monkeypatch):
    monkeypatch.setattr(loop_mod, "CONFIG_DIR", tmp_path / "no-config")
    run = _run(tmp_path)
    sup = make_supervisor(tmp_path, run_id="unit-run", cfg_defaults=False,
                          run_root=run.root, workspace="ws",
                          max_approaches=2, reflect=False)
    sup._finish("succeeded", "verified")

    record = json.loads((run.artifacts_dir / "run-record.json").read_text())
    assert record["engineVersion"] == ENGINE_VERSION

    md = (run.artifacts_dir / "run-record.md").read_text()
    assert f"- engineVersion: {ENGINE_VERSION}" in md
