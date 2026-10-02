"""v0.30 F.2: `ralphctl status` and `ralphctl fault` show a rate-limit wait.

While the loop waits for a subscription window to reset, status.json carries
`rateLimitWait = {type, resetsAt, since}`. Both commands render it as one
line, `waiting:   rate limit (<type>) until <local time>`, aligned with their
other labelled lines. Driven in-process over a hand-written run dir.
"""

from __future__ import annotations

import json

import pytest

from ralphd.cli import main as cli_main
from ralphd.engine.faults import FAULT_CLASS_RATE_LIMIT, FAULT_REASON_RATE_LIMIT
from ralphd.engine.state import (
    FAULT_VERDICT_DIVERGED_NOTICE,
    fault_explanation,
    format_local_time,
    format_rate_limit_wait_line,
    iteration_detail,
)

RUN_ID = "rl-wait"
RESETS_AT = "2026-10-02T15:00:00Z"
WAIT = {"type": "five_hour", "resetsAt": RESETS_AT, "since": "2026-10-02T14:50:00Z"}
LINE = f"waiting:   rate limit (five_hour) until {format_local_time(RESETS_AT)}"


class _Args:
    def __init__(self, run_id: str, as_json: bool = False):
        self.run_id = run_id
        self.json = as_json


@pytest.fixture
def run_dir(tmp_path, monkeypatch):
    registry = tmp_path / "registry"
    monkeypatch.setenv("RALPHD_REGISTRY", str(registry))
    # No host.json: `cmd_status` reads the run dir on disk. The dangling
    # container probe would ask docker; it is answered "not dangling".
    monkeypatch.setattr(cli_main, "_dangling_run_entry", lambda run_id: None)
    root = registry / "runs" / RUN_ID
    root.mkdir(parents=True)
    status = {"runId": RUN_ID, "state": "running", "health": "ok",
              "infraWait": None, "rateLimitWait": WAIT, "iteration": 1,
              "iterationsUsed": 0, "iterationsBudget": 6,
              "startedAt": "2026-10-02T14:00:00Z"}
    (root / "status.json").write_text(json.dumps(status))
    it = root / "iterations" / "0001"
    it.mkdir(parents=True)
    rate_limit = {"status": "rejected", "resetsAt": 1790953200,
                  "rateLimitType": "five_hour",
                  "unifiedWindows": {"five_hour": {"resetsAt": 1790953200}}}
    (it / "meta.json").write_text(json.dumps({
        "number": 1, "phase": "worker", "startedAt": "2026-10-02T14:49:58Z",
        "endedAt": "2026-10-02T14:50:00Z", "exitCode": 1,
        "error": "429 rate_limit: API Error: 429 rate_limit_error",
        "faultClass": FAULT_CLASS_RATE_LIMIT, "rateLimit": rate_limit,
        "rateLimitFresh": True, "httpStatus": 429, "errorType": "rate_limit",
        "usage": {}}))
    return root


def test_the_line_has_one_wording():
    assert format_rate_limit_wait_line(WAIT) == LINE
    assert format_rate_limit_wait_line(None) is None
    assert format_rate_limit_wait_line({"resetsAt": RESETS_AT}).startswith(
        "waiting:   rate limit (unknown type) until ")


def test_ralphctl_status_renders_the_waiting_line(run_dir, capsys):
    cli_main.cmd_status(_Args(RUN_ID))
    out = capsys.readouterr().out
    assert LINE in out.splitlines(), out
    # the label lines up with its neighbours (11 columns)
    started = next(ln for ln in out.splitlines() if ln.startswith("started:"))
    assert started.index(started.split()[1]) == LINE.index("rate")


def test_ralphctl_status_json_carries_the_wait(run_dir, capsys):
    cli_main.cmd_status(_Args(RUN_ID, as_json=True))
    doc = json.loads(capsys.readouterr().out)
    assert doc["rateLimitWait"] == WAIT


def test_ralphctl_status_has_no_line_without_a_wait(run_dir, capsys):
    status = json.loads((run_dir / "status.json").read_text())
    status["rateLimitWait"] = None
    (run_dir / "status.json").write_text(json.dumps(status))
    cli_main.cmd_status(_Args(RUN_ID))
    assert "rate limit" not in capsys.readouterr().out


def test_ralphctl_fault_renders_the_waiting_line_and_the_class(run_dir, capsys):
    cli_main.cmd_fault(_Args(RUN_ID))
    lines = capsys.readouterr().out.splitlines()
    assert LINE in lines, lines
    assert lines[1].startswith(f"fault:     {FAULT_CLASS_RATE_LIMIT} (iteration 1")
    assert f"because:   {FAULT_REASON_RATE_LIMIT}" in lines
    assert FAULT_VERDICT_DIVERGED_NOTICE not in lines


def test_the_reread_class_matches_the_recorded_one(run_dir):
    """meta.json carries the structured evidence, so the re-derivation the
    readers make agrees with the engine's verdict."""
    detail = iteration_detail(run_dir, 1)
    assert detail["reason"] == FAULT_REASON_RATE_LIMIT
    assert detail["notices"] == []
    exp = fault_explanation(run_dir)
    assert exp["rateLimitWait"] == WAIT
    assert exp["faultClass"] == FAULT_CLASS_RATE_LIMIT


def test_a_wait_with_no_recorded_iteration_still_explains_itself(run_dir):
    for meta in run_dir.glob("iterations/*/meta.json"):
        meta.unlink()
    exp = fault_explanation(run_dir)
    assert exp["hasFault"] is True
    assert exp["faultClass"] == FAULT_CLASS_RATE_LIMIT
    assert LINE in exp["summaryLines"]
