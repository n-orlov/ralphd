"""Task 017 (PRD requirement A.7, #224): `ralphctl runs` and the hub run
list both render a distinct marker for a row whose `pauseReason` is
`operator_input_needed` -- the ONE pause reason an operator cannot resolve
by waiting or editing `tasks.json` alone; every other `paused` reason
either resolves on its own re-ask (`stagnation`, the dependency-edge
reasons) or is a different pauseReason this task never touches.

One shared spelling (`state.format_pause_marker`/`PARKED_FOR_INPUT_
MARKER`) feeds both surfaces, so this module pins a CLI node
(`cmd_runs`) and a hub node (`ui_server.run_list`) against the SAME
on-disk fixture and checks they agree, plus that neither surface renders
the marker for an ordinary `paused` run with some other reason (or none).
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from ralphd.cli import main as cli_main
from ralphd.cli import ui_server
from ralphd.engine.state import PARKED_FOR_INPUT_MARKER


def _seed_run_dir(registry: Path, run_id: str, **status_over) -> Path:
    run_dir = registry / "runs" / run_id
    run_dir.mkdir(parents=True)
    status = {"runId": run_id, "state": "paused", "verdict": None,
              "phase": "review", "approach": 1, "maxApproaches": 3,
              "iterationsUsed": 3, "iterationsBudget": 25,
              "startedAt": "2026-01-01T00:00:00Z", **status_over}
    (run_dir / "status.json").write_text(json.dumps(status))
    return run_dir


def _run_cmd_runs(monkeypatch, capsys, reg: Path) -> list[dict]:
    monkeypatch.setenv("RALPHD_REGISTRY", str(reg))
    args = argparse.Namespace(state=None, json=True, sort=None, reverse=False)
    cli_main.cmd_runs(args)
    return json.loads(capsys.readouterr().out)


def test_the_cli_json_row_carries_the_pause_reason_and_marker(
        tmp_path, monkeypatch, capsys):
    reg = tmp_path / "reg"
    _seed_run_dir(reg, "parked", pauseReason="operator_input_needed")
    _seed_run_dir(reg, "ordinary-pause", pauseReason="stagnation")
    _seed_run_dir(reg, "not-paused", state="running", pauseReason=None)

    rows = {r["runId"]: r for r in _run_cmd_runs(monkeypatch, capsys, reg)}

    assert rows["parked"]["pauseReason"] == "operator_input_needed"
    assert rows["parked"]["pauseMarker"] == PARKED_FOR_INPUT_MARKER
    assert rows["ordinary-pause"]["pauseMarker"] == ""
    assert rows["not-paused"]["pauseMarker"] == ""


def test_the_cli_table_text_shows_the_marker_only_for_the_parked_row(
        tmp_path, monkeypatch, capsys):
    reg = tmp_path / "reg"
    _seed_run_dir(reg, "parked", pauseReason="operator_input_needed")
    _seed_run_dir(reg, "ordinary-pause", pauseReason="stagnation")

    monkeypatch.setenv("RALPHD_REGISTRY", str(reg))
    args = argparse.Namespace(state=None, json=False, sort=None, reverse=False)
    cli_main.cmd_runs(args)
    out = capsys.readouterr().out
    lines = {line.split()[0]: line for line in out.splitlines()[1:]}

    assert PARKED_FOR_INPUT_MARKER in lines["parked"]
    assert PARKED_FOR_INPUT_MARKER not in lines["ordinary-pause"]


def test_the_hub_run_list_carries_the_same_field_and_marker(tmp_path):
    reg = tmp_path / "reg"
    _seed_run_dir(reg, "parked", pauseReason="operator_input_needed")
    _seed_run_dir(reg, "ordinary-pause", pauseReason="stagnation")
    _seed_run_dir(reg, "not-paused", state="running", pauseReason=None)

    rows = {r["runId"]: r for r in ui_server.run_list(reg)}

    assert rows["parked"]["pauseReason"] == "operator_input_needed"
    assert rows["parked"]["pauseMarker"] == PARKED_FOR_INPUT_MARKER
    assert rows["ordinary-pause"]["pauseMarker"] == ""
    assert rows["not-paused"]["pauseMarker"] == ""


def test_the_cli_and_hub_markers_agree_key_for_key(tmp_path):
    reg = tmp_path / "reg"
    _seed_run_dir(reg, "parked", pauseReason="operator_input_needed")

    hub_row = ui_server.run_list(reg)[0]

    import argparse as _argparse
    import io
    import contextlib
    from ralphd.cli import main as _cli_main
    import os
    os.environ["RALPHD_REGISTRY"] = str(reg)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        _cli_main.cmd_runs(_argparse.Namespace(state=None, json=True,
                                               sort=None, reverse=False))
    cli_row = json.loads(buf.getvalue())[0]

    assert cli_row["pauseReason"] == hub_row["pauseReason"]
    assert cli_row["pauseMarker"] == hub_row["pauseMarker"]
