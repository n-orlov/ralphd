"""Task 013 (#5): `ralphctl status` on a degraded run (one sitting out an
infra outage) prints a `degraded:` line with the countdown to the next
attempt, the attempt number and the error -- while a healthy run's human
output stays byte-identical to what it was before that line existed, and
`--json` passes `health`/`infraWait` straight through.

Two tiers, no engine and no real container:
- unit tests over the pure formatter `_format_degraded_lines`;
- black-box `ralphctl status` runs over on-disk run-dir fixtures (the CLI's
  status.json fallback path), reusing test_cli_docker.py's stub-docker `Ctl`
  harness and test_cli_resume.py's `_seed_run`.
"""

from __future__ import annotations

import json
import time

from test_cli_docker import Ctl, ctl, unix_sock
from test_cli_resume import _seed_run

from ralphd.cli.main import _format_degraded_lines
from ralphd.engine.state import utc_from_epoch

__all__ = ["ctl", "unix_sock"]


def _wait(**over) -> dict:
    wait = {
        "since": utc_from_epoch(time.time() - 52),
        "attempt": 4,
        "error": "getaddrinfo EAI_AGAIN aigw.example.internal",
        "phase": "worker",
        "nextAttemptAt": utc_from_epoch(time.time() + 58),
        "waitedS": 52,
        "budgetS": 14400,
        "remainingS": 14348,
    }
    wait.update(over)
    return wait


# --------------------------------------------------------------------------
# formatter unit tests
# --------------------------------------------------------------------------

def test_healthy_status_renders_no_degraded_line():
    assert _format_degraded_lines({"health": "ok", "infraWait": None}) == []
    # a pre-0.5 status.json that carries neither field is healthy too
    assert _format_degraded_lines({"state": "running"}) == []


def test_degraded_line_carries_countdown_attempt_and_error():
    lines = _format_degraded_lines({"health": "degraded", "infraWait": _wait()})
    assert lines[0].startswith("degraded:  ")
    head = lines[0]
    assert "attempt 4" in head
    assert "phase worker" in head
    # countdown to nextAttemptAt (58s away), not just the raw timestamp
    assert "next attempt in " in head
    assert "s (at " in head
    assert "waited 52s of 4h outage budget" in head
    assert any("getaddrinfo EAI_AGAIN aigw.example.internal" in line
               for line in lines[1:]), lines


def test_degraded_line_wraps_a_long_error_without_losing_text():
    error = "Connection error. " + ("x" * 200)
    lines = _format_degraded_lines({"health": "degraded",
                                    "infraWait": _wait(error=error)})
    assert len(lines) > 2
    for extra in lines[1:]:
        assert extra.startswith("           ")
    rejoined = " ".join(
        line.removeprefix("           error: ").removeprefix("           ")
        for line in lines[1:]
    )
    assert rejoined.replace(" ", "") == error.replace(" ", "")


def test_degraded_with_next_attempt_in_the_past_says_due_now():
    lines = _format_degraded_lines({
        "health": "degraded",
        "infraWait": _wait(nextAttemptAt=utc_from_epoch(time.time() - 5)),
    })
    assert "next attempt due now" in lines[0]


def test_degraded_between_waits_reports_the_episode_without_a_countdown():
    """`health` stays degraded while the retry attempt itself runs and
    `infraWait` is back to null (docs/api.md) -- still reported, since the
    outage episode is not over."""
    lines = _format_degraded_lines({"health": "degraded", "infraWait": None})
    assert len(lines) == 1
    assert lines[0].startswith("degraded:  infra outage episode in progress")


def test_malformed_next_attempt_timestamp_does_not_crash():
    lines = _format_degraded_lines({"health": "degraded",
                                    "infraWait": _wait(nextAttemptAt="whenever")})
    assert "next attempt at whenever" in lines[0]


# --------------------------------------------------------------------------
# black-box `ralphctl status` over run-dir fixtures
# --------------------------------------------------------------------------

_BASE_STATUS = {
    "runId": "tst-degraded",
    "state": "running",
    "verdict": None,
    "phase": "worker",
    "approach": 1,
    "iterationsUsed": 7,
    "iterationsBudget": 250,
    "startedAt": "2024-01-01T00:00:00Z",
    "endedAt": "2024-01-01T01:02:03Z",
    "schemaVersion": 1,
    "tasks": {"total": 3, "completed": 1, "pending": 2},
    "usage": {"costUSD": 0.5, "totalTokens": 12000},
}


def _seed_status(ctl: Ctl, run_id: str, **status_over) -> None:
    rdir, _cdir = _seed_run(ctl, run_id)
    doc = {**_BASE_STATUS, "runId": run_id, **status_over}
    (rdir / "status.json").write_text(json.dumps(doc))


def test_status_prints_degraded_line_for_a_degraded_run(ctl: Ctl):
    _seed_status(ctl, "tst-degraded", health="degraded", infraWait=_wait())
    res = ctl.run("status", "tst-degraded")
    assert res.returncode == 0, res.stderr
    degraded = [ln for ln in res.stdout.splitlines() if ln.startswith("degraded:")]
    assert len(degraded) == 1, res.stdout
    assert "attempt 4" in degraded[0]
    assert "next attempt in " in degraded[0]
    assert "getaddrinfo EAI_AGAIN aigw.example.internal" in res.stdout


def test_status_json_passes_health_and_infra_wait_through(ctl: Ctl):
    wait = _wait()
    _seed_status(ctl, "tst-degraded-json", health="degraded", infraWait=wait)
    res = ctl.run("--json", "status", "tst-degraded-json")
    assert res.returncode == 0, res.stderr
    doc = json.loads(res.stdout)
    assert doc["health"] == "degraded"
    assert doc["infraWait"] == wait


def test_healthy_run_output_is_byte_identical_to_pre_task_013(ctl: Ctl):
    """The `degraded:` line must not perturb a healthy run's output: a
    status.json carrying health ok / infraWait null renders exactly the same
    bytes as one that has neither field at all (the shape this command
    printed before task 013 existed). Terminal fixtures (endedAt set) so the
    duration lines are deterministic across the two invocations."""
    _seed_status(ctl, "tst-healthy-fields", state="failed",
                 health="ok", infraWait=None)
    _seed_status(ctl, "tst-healthy-nofields", state="failed")

    with_fields = ctl.run("status", "tst-healthy-fields")
    without = ctl.run("status", "tst-healthy-nofields")
    assert with_fields.returncode == 0 and without.returncode == 0
    def normalise(text: str, rid: str) -> str:
        return text.replace(rid, "RUNID")
    assert (normalise(with_fields.stdout, "tst-healthy-fields")
            == normalise(without.stdout, "tst-healthy-nofields"))
    assert "degraded" not in with_fields.stdout


def test_healthy_json_still_carries_the_contract_fields(ctl: Ctl):
    """Even for an on-disk fallback of a pre-0.5 run dir, --json publishes
    the same health/infraWait contract GET /status guarantees."""
    _seed_status(ctl, "tst-healthy-json", state="failed")
    res = ctl.run("--json", "status", "tst-healthy-json")
    assert res.returncode == 0, res.stderr
    doc = json.loads(res.stdout)
    assert doc["health"] == "ok"
    assert doc["infraWait"] is None
