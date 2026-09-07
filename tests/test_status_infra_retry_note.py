"""Task 001a criterion 4 (surface it): while an infra-fault retry is backing
off, `currentIteration.note` carries a human-readable "retrying after infra
fault..." message. This must be visible via plain `ralphctl status` (not just
`--json`), and the hub run-detail page must render `currentIteration.note` too
(checked via the JSON the hub consumes, since app.js itself isn't exercised by
the plain pytest tier).

Task 010 (B.4): the note exists ONLY between the write that publishes it and
the next attempt's iteration-start status write -- i.e. for exactly as long as
the backoff wait lasts (`loop.py` writes it immediately before
`_wait_out_backoff`). This module used to buy that window with a real
`RALPHD_INFRA_RETRY_BACKOFF_S=1,1,1` (3 s of engine sleep) and then race a
0.3 s poll loop against it. It is now observed from *inside* the wait instead:
`_wait_out_backoff` is stubbed with a sampler that reads status.json and runs
`ralphctl status` -- in-process, over the run dir the supervisor is writing --
at the one instant the note is live. Deterministic, no sleeping, and it asks
BOTH surfaces the same question the poll loop used to ask:

* the `--json` document (what the hub consumes) carries `currentIteration.note`;
* plain `ralphctl status` renders it on its own `note:` line.
"""

from __future__ import annotations

import json

import pytest
from test_infra_retry_schedule import _infra_result, _ok_result

from ralphd.cli import main as cli_main
from tests.conftest import supervisor_factory

RUN_ID = "infra-retry-note"

# `no_backoff` is left at the harness default (True), so the supervisor is
# built with `_wait_out_backoff` already stubbed and the sampling stub below
# merely replaces it: nothing in this module can wait, whatever schedule the
# JobConfig names. The schedule IS named (1 s, cap 1 s, 3 attempts) because the
# note's text quotes it -- "next in 1s" is what the operator reads.
_supervisor = supervisor_factory(run_id=RUN_ID, cfg_defaults=False)


class _StatusArgs:
    """The two attributes `cmd_status` reads off its argparse namespace."""

    def __init__(self, run_id: str, as_json: bool):
        self.run_id = run_id
        self.json = as_json


def _ralphctl_status(capsys, *, as_json: bool) -> str:
    cli_main.cmd_status(_StatusArgs(RUN_ID, as_json))
    return capsys.readouterr().out


@pytest.mark.asyncio
async def test_ralphctl_status_shows_infra_retry_note_during_backoff(
        tmp_path, monkeypatch, capsys):
    registry = tmp_path / "registry"
    monkeypatch.setenv("RALPHD_REGISTRY", str(registry))
    # No host.json -> `api()` exits 4 and `cmd_status` falls back to the run
    # dir on disk, which is what an operator asking a live run gets served from
    # anyway (the engine's own GET /status reads the same file). The dangling-
    # container probe is the one thing in that fallback that would reach for
    # the docker daemon; it has nothing to do with the note, so it is answered
    # "not dangling" here instead of being asked.
    monkeypatch.setattr(cli_main, "_dangling_run_entry", lambda run_id: None)

    sup = _supervisor(tmp_path, run_root=f"registry/runs/{RUN_ID}",
                      infra_retry_backoff_s=[1.0], infra_retry_backoff_max_s=1.0,
                      infra_retry_max=3, infra_outage_budget_s=100.0)
    sup.run.update_status(runId=RUN_ID, state="running", iteration=1,
                          iterationsUsed=1, iterationsBudget=6)

    results = [_infra_result(), _ok_result()]
    calls: list[str] = []
    notes: list[str] = []
    human: list[str] = []
    machine: list[str] = []

    async def fake_once(phase, extra="", prompt_name=None, tier=None):
        calls.append(phase)
        return results[min(len(calls) - 1, len(results) - 1)]

    async def sampling_backoff(seconds):
        # Called exactly where the engine's real backoff wait happens: the
        # note is live on disk right now and nothing else is running.
        status = json.loads((sup.run.root / "status.json").read_text())
        notes.append((status.get("currentIteration") or {}).get("note") or "")
        machine.append(_ralphctl_status(capsys, as_json=True))
        human.append(_ralphctl_status(capsys, as_json=False))
        return seconds, False

    sup._run_iteration_once = fake_once            # type: ignore[method-assign]
    sup._wait_out_backoff = sampling_backoff       # type: ignore[method-assign]

    await sup.run_iteration("worker")

    assert calls == ["worker", "worker"], "one infra fault, then a healthy attempt"
    # -- the note itself, quoting attempt/cap/next-attempt and the error ----
    assert len(notes) == 1
    assert notes[0] == ("retrying after infra fault (attempt 1/3, next in 1s): "
                        "Connection error.")
    # -- `--json`: the document the hub's run-detail page consumes -----------
    doc = json.loads(machine[0])
    assert doc["currentIteration"]["note"] == notes[0]
    assert doc["state"] == "running", "no new `state` value while retrying"
    # -- plain `ralphctl status`: its own `note:` line, not just --json ------
    assert "retrying after infra fault" in human[0]
    assert "note:" in human[0]
    assert notes[0] in human[0]

    # ... and the successful attempt leaves nothing retry-shaped behind.
    final = json.loads((sup.run.root / "status.json").read_text())
    assert final["health"] == "ok"
    assert final["infraWait"] is None
