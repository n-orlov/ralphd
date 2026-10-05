"""#296 (PRD G): the run list's COST column. `ui_server.run_list` rows carry
`costDisplay` (the run detail header's own total string) and numeric `costUSD`
from status.json alone; `RUN_COLUMNS`/`sortRuns` in app.js and `ralphctl runs`
(`--sort cost`) sort on the number with unknown costs last."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from ralphd.cli.main import RUN_SORT_KEYS, sort_run_rows
from ralphd.cli.ui_server import run_detail, run_list

pytestmark = pytest.mark.fast

APP_JS = (Path(__file__).resolve().parents[1]
          / "src" / "ralphd" / "cli" / "web" / "app.js")

PRICED = {"totalTokens": 100, "costUSD": 0.56}
PARTIAL = {"totalTokens": 100, "costUSD": 0.25, "costStatus": "partial"}
UNKNOWN = {"totalTokens": 100, "costStatus": "unknown"}


def _write_run(reg: Path, run_id: str, usage: dict | None) -> None:
    d = reg / "runs" / run_id
    d.mkdir(parents=True)
    doc = {"runId": run_id, "state": "succeeded", "verdict": "verified",
           "phase": "reflect", "approach": 1, "iterationsUsed": 1,
           "iterationsBudget": 10, "schemaVersion": 1,
           "startedAt": "2024-01-01T00:00:00Z"}
    if usage is not None:
        doc["usage"] = usage
    (d / "status.json").write_text(json.dumps(doc))


def _header_total(reg: Path, run_id: str) -> str:
    """The run detail header's cost string: `status.usage.costDisplay`, which
    app.js `renderUsage` places verbatim (absent -> no cost card)."""
    usage = (run_detail(reg, run_id)["status"].get("usage") or {})
    return usage.get("costDisplay") or ""


@pytest.mark.parametrize("name,usage,shown", [
    ("priced", PRICED, "$0.5600"),
    ("partial", PARTIAL, "$0.2500+ (partial, rest unavailable)"),
    ("unknown", UNKNOWN, "unavailable"),
    ("no-usage", None, ""),
])
def test_run_list_row_cost_equals_the_run_detail_header_total(tmp_path, name, usage, shown):
    _write_run(tmp_path, name, usage)
    row = next(r for r in run_list(tmp_path) if r["runId"] == name)
    assert row["costDisplay"] == _header_total(tmp_path, name) == shown
    if name in ("priced", "partial"):
        assert isinstance(row["costUSD"], float)
    else:
        assert row["costUSD"] is None
    assert row["costUSD"] == {"priced": 0.56, "partial": 0.25}.get(name)


def test_run_list_row_cost_reads_nothing_beyond_status_json(tmp_path):
    """Files beside status.json that carry other money (a usage ledger, an
    events log naming a different cost) cannot move the row's cost."""
    _write_run(tmp_path, "r1", PRICED)
    d = tmp_path / "runs" / "r1"
    (d / "usage.json").write_text(json.dumps({"costUSD": 99.0}))
    (d / "events.jsonl").write_text(json.dumps(
        {"type": "iteration.completed", "usage": {"costUSD": 77.0}}) + "\n")
    row = run_list(tmp_path)[0]
    assert (row["costDisplay"], row["costUSD"]) == ("$0.5600", 0.56)


# ------------------------------------------------------------------ app.js
_HARNESS = r"""
const fs = require('fs');
const src = fs.readFileSync(process.argv[1], 'utf8');
const runs = JSON.parse(process.argv[2]);
const dir = Number(process.argv[3]);
function grab(name) {
  const a = src.indexOf('function ' + name + '(');
  if (a < 0) throw Error(name + ' not found');
  return src.slice(a, src.indexOf('\n}\n', a) + 3);
}
const cols = src.slice(src.indexOf('const RUN_COLUMNS = ['),
                       src.indexOf('\n];', src.indexOf('const RUN_COLUMNS = [')) + 3);
const body = ['numOrNull', 'runColumn', 'cmpValues', 'sortRuns'].map(grab).join('\n');
const f = new Function('runSort', 'lifecycleRank', 'isoToEpoch', 'taskRatio',
  'RUN_STATE_ORDER', 'RUN_VERDICT_ORDER',
  body + '\n' + cols + '\nreturn {sortRuns, RUN_COLUMNS};');
const noop = () => null;
const api = f({key: 'cost', dir}, noop, noop, noop, [], []);
console.log(JSON.stringify({
  labels: api.RUN_COLUMNS.map(c => c.label),
  order: api.sortRuns(runs).map(r => r.runId)}));
"""

RUNS = [
    {"runId": "unknown-a", "costUSD": None},
    {"runId": "dear", "costUSD": 12.5},
    {"runId": "cheap", "costUSD": 0.05},
    {"runId": "free", "costUSD": 0},
    {"runId": "unknown-b"},
]


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_app_js_cost_column_sorts_on_costusd_with_unknown_last():
    out = subprocess.run(
        ["node", "-e", _HARNESS, str(APP_JS), json.dumps(RUNS), "1"],
        capture_output=True, text=True, check=True, timeout=30)
    got = json.loads(out.stdout)
    assert got["labels"][-1] == "COST"
    assert got["order"] == ["free", "cheap", "dear", "unknown-a", "unknown-b"]


def test_app_js_cost_cell_places_the_server_string():
    text = APP_JS.read_text()
    assert "r.costDisplay" in text
    block = text.split("const RUN_COLUMNS = [")[1].split("\n];")[0]
    line = next(ln for ln in block.splitlines() if '"COST"' in ln)
    assert 'key: "cost"' in line and "r.costUSD" in line and "costDisplay" not in line


# --------------------------------------------------------------------- CLI
def test_cost_is_a_run_sort_key_with_unknown_last():
    assert "cost" in RUN_SORT_KEYS
    got = [r["runId"] for r in sort_run_rows(RUNS, "cost")]
    assert got == ["free", "cheap", "dear", "unknown-a", "unknown-b"]


def _seed_cli(reg: Path) -> None:
    _write_run(reg, "r-dear", {"totalTokens": 1, "costUSD": 12.5})
    _write_run(reg, "r-cheap", PRICED)
    _write_run(reg, "r-none", None)
    _write_run(reg, "r-unknown", UNKNOWN)


def test_ralphctl_runs_prints_a_cost_column_and_sorts_by_it(tmp_path, monkeypatch, capsys):
    import argparse

    from ralphd.cli import main as cli_main
    _seed_cli(tmp_path)
    monkeypatch.setattr(cli_main, "registry", lambda: tmp_path)

    def run(sort, **kw):
        capsys.readouterr()
        cli_main.cmd_runs(argparse.Namespace(
            state=None, sort=sort, reverse=False, json=kw.get("json", False)))
        return capsys.readouterr().out

    lines = run("cost").splitlines()
    assert lines[0].split()[-1] == "COST"
    assert [ln.split()[0] for ln in lines[1:]] == [
        "r-cheap", "r-dear", "r-none", "r-unknown"]
    assert lines[1].endswith("$0.5600") and lines[2].endswith("$12.5000")
    assert lines[-1].endswith("unavailable")
    rows = json.loads(run("cost", json=True))
    assert [r["runId"] for r in rows][:2] == ["r-cheap", "r-dear"]
    assert {r["runId"] for r in rows[2:]} == {"r-none", "r-unknown"}
    assert [r["costUSD"] for r in rows] == [0.56, 12.5, None, None]
