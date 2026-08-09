"""Task 006: a terminal run (failed/aborted/succeeded) that still has
unconsumed steering must surface that loudly -- not just in raw --json
status.json, but in the human `ralphctl status` output too, since a
terminal run never reads pending steering again and the operator's
`--json` habit may not be there the one time it matters.
"""

from __future__ import annotations

import json


def test_ralphctl_status_warns_about_unconsumed_steering(live):
    run = live(run_id="unconsumed-steer", job={"iterations": 12},
               stub_env={"STUB_TASKS": "10", "STUB_SLEEP": "5"})
    run.wait_api()

    steer = run.ralphctl("steer", run.run_id, "never gets acted on",
                         "--name", "stranded")
    assert steer.returncode == 0, (steer.stdout, steer.stderr)

    abort = run.ralphctl("abort", run.run_id, "--reason", "test abort mid-steering")
    assert abort.returncode == 0, (abort.stdout, abort.stderr)

    run.wait_terminal(timeout=30)

    jres = run.ralphctl("--json", "status", run.run_id)
    assert jres.returncode == 0, (jres.stdout, jres.stderr)
    status = json.loads(jres.stdout)
    assert status["unconsumedSteering"] == ["001-stranded.md"]

    human = run.ralphctl("status", run.run_id)
    assert human.returncode == 0, (human.stdout, human.stderr)
    assert "UNCONSUMED STEERING" in human.stdout
    assert "001-stranded.md" in human.stdout


def test_ralphctl_status_no_warning_when_steering_fully_consumed(live):
    """Negative case: a run that ends with no pending steering (the common
    case) must not print the warning line at all."""
    run = live(run_id="no-unconsumed-steer", job={"iterations": 6},
               stub_env={"STUB_TASKS": "1", "STUB_SLEEP": "1"})
    run.wait_terminal(timeout=60)

    jres = run.ralphctl("--json", "status", run.run_id)
    status = json.loads(jres.stdout)
    assert status.get("unconsumedSteering") == []

    human = run.ralphctl("status", run.run_id)
    assert "UNCONSUMED STEERING" not in human.stdout
