"""Task 004 (PRD requirement A.4, #192): `ralphctl status` renders distinct
wording for each of the three endings a peer verdict gate rejection can
leave the terminal record in -- status.json's `gateRejectionEnding`
(`state.GATE_REJECTION_ENDING_*`, written by `LoopSupervisor.
_step_verdict_gate`'s `_gate_rejection_ending`, task 004):

* rejected on the merits (no curable path existed at all -- A.3's own
  mixed/incurable case)
* rejected, cure path taken (A.1's cure-pass edge)
* rejected, cure path unavailable (curable, but A.2's run-wide cure-pass
  ceiling was already spent)

A.1-A.3 (tasks 001-003) already gave the engine the cure mechanism itself;
this only covers the terminal-record field and its rendering, so these
tests seed status.json by hand (the same pattern
`tests/test_cli_status_finishing.py`'s `deadlineBeatVerdict` tests use for
the sibling marker task 007 added) rather than driving the full engine --
no docker, no live container, seconds not minutes.

Fourth test: a terminal run dir with no `gateRejectionEnding` key at all
(every run before this task, or a run whose gate never rejected) still
renders cleanly, with no `gate:` line and no crash.
"""

from __future__ import annotations

import json

from test_cli_docker import Ctl, ctl, unix_sock
from test_cli_resume import _seed_run

__all__ = ["ctl", "unix_sock"]


_BASE_STATUS = {
    "state": "failed",
    "verdict": "unverified",
    "reason": "peer verdict gate rejected approach 1",
    "phase": None,
    "approach": 1,
    "maxApproaches": 1,
    "iterationsUsed": 9,
    "iterationsBudget": 250,
    "startedAt": "2024-01-01T00:00:00Z",
    "endedAt": "2024-01-01T01:02:03Z",
    "currentIteration": None,
    "pendingTerminalState": None,
    "schemaVersion": 1,
    "tasks": {"total": 3, "completed": 1, "failed": 1},
}


def _seed_status(ctl: Ctl, run_id: str, **status_over) -> None:
    rdir, _cdir = _seed_run(ctl, run_id)
    doc = {**_BASE_STATUS, "runId": run_id, **status_over}
    (rdir / "status.json").write_text(json.dumps(doc))


def test_gate_rejection_ending_on_the_merits_renders_distinct_wording(
        ctl: Ctl):
    run_id = "tst-gate-ending-merits"
    _seed_status(ctl, run_id, gateRejectionEnding="rejected_on_the_merits")
    res = ctl.run("status", run_id)
    assert res.returncode == 0, res.stderr
    assert "gate:      rejected on the merits" in res.stdout, res.stdout
    assert "cure path" not in res.stdout, res.stdout
    res_json = ctl.run("--json", "status", run_id)
    assert (json.loads(res_json.stdout)["gateRejectionEnding"]
            == "rejected_on_the_merits")


def test_gate_rejection_ending_cure_taken_renders_distinct_wording(
        ctl: Ctl):
    run_id = "tst-gate-ending-cured"
    _seed_status(ctl, run_id, gateRejectionEnding="rejected_cure_taken")
    res = ctl.run("status", run_id)
    assert res.returncode == 0, res.stderr
    assert "gate:      rejected, cure path taken" in res.stdout, res.stdout
    assert "unavailable" not in res.stdout, res.stdout
    assert "merits" not in res.stdout, res.stdout


def test_gate_rejection_ending_cure_unavailable_renders_distinct_wording(
        ctl: Ctl):
    run_id = "tst-gate-ending-budget"
    _seed_status(ctl, run_id, gateRejectionEnding="rejected_cure_unavailable")
    res = ctl.run("status", run_id)
    assert res.returncode == 0, res.stderr
    assert ("gate:      rejected, cure path unavailable (cure-pass budget "
            "exhausted)") in res.stdout, res.stdout
    assert "merits" not in res.stdout, res.stdout

    # All three renderings differ from one another -- a human reading any
    # two side by side can tell which ending this run is, not just that
    # status.json holds *some* value.
    merits_id = "tst-gate-ending-merits-2"
    _seed_status(ctl, merits_id, gateRejectionEnding="rejected_on_the_merits")
    cured_id = "tst-gate-ending-cured-2"
    _seed_status(ctl, cured_id, gateRejectionEnding="rejected_cure_taken")
    merits_line = next(ln for ln in ctl.run("status", merits_id).stdout
                       .splitlines() if ln.startswith("gate:"))
    cured_line = next(ln for ln in ctl.run("status", cured_id).stdout
                      .splitlines() if ln.startswith("gate:"))
    budget_line = next(ln for ln in res.stdout.splitlines()
                       if ln.startswith("gate:"))
    assert len({merits_line, cured_line, budget_line}) == 3


def test_gate_rejection_ending_absent_still_renders(ctl: Ctl):
    """The counterpart every one of the three tests above needs: a terminal
    run dir written before this task existed (no `gateRejectionEnding` key
    on status.json at all) must still render -- no `gate:` line, no
    crash -- exactly like every other terminal run that never touched the
    peer verdict gate."""
    run_id = "tst-gate-ending-absent"
    _seed_status(ctl, run_id)
    res = ctl.run("status", run_id)
    assert res.returncode == 0, res.stderr
    assert "gate:" not in res.stdout, res.stdout
    # the rest of the ordinary terminal render is unaffected.
    assert "state:     failed" in res.stdout, res.stdout
    assert "verdict:   unverified" in res.stdout, res.stdout
    assert "ended:     " in res.stdout, res.stdout

    res_json = ctl.run("--json", "status", run_id)
    assert "gateRejectionEnding" not in json.loads(res_json.stdout)
