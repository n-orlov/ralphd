"""Task 015 (#56): `ralphctl status` must not print an `ended:` line (or
label the duration `(total)`) while an in-progress iteration is recorded.

Task 012's `_finish()` writes `endedAt` into status.json the instant the
job's verdict settles, *before* the post-terminal `reflect` iteration (the
"finishing" window, #56) has actually run -- so a run whose engine is still
doing agent work (a live `currentIteration` dict, `state: finishing`) had
`ralphctl status` claim the job had already ended. This is a pure
black-box test over the CLI's on-disk-status-json fallback path (the same
harness `test_cli_status_reflect.py` uses), no engine, no real container.
"""

from __future__ import annotations

import json

from test_cli_docker import Ctl, ctl, unix_sock
from test_cli_resume import _seed_run

from ralphd.cli.main import job_container_name

__all__ = ["ctl", "unix_sock"]


_FINISHING_STATUS = {
    "state": "finishing",
    "verdict": "verified",
    "pendingTerminalState": "succeeded",
    "phase": "reflect",
    "approach": 1,
    "iterationsUsed": 9,
    "iterationsBudget": 250,
    "startedAt": "2024-01-01T00:00:00Z",
    "endedAt": "2024-01-01T01:02:03Z",
    "schemaVersion": 1,
    "tasks": {"total": 3, "completed": 3, "pending": 0},
    "usage": {"costUSD": 0.5, "totalTokens": 12000},
    "currentIteration": {"number": 10, "phase": "reflect", "model": "stub",
                         "startedAt": "2024-01-01T01:02:05Z"},
}


def _seed_status(ctl: Ctl, run_id: str, **status_over) -> None:
    rdir, _cdir = _seed_run(ctl, run_id)
    doc = {**_FINISHING_STATUS, "runId": run_id, **status_over}
    (rdir / "status.json").write_text(json.dumps(doc))


# The recorded `state` is non-terminal ("finishing") for every fixture in
# this module -- without telling the stub docker daemon the container is
# still running, `_dangling_run_entry`'s zombie check fires first and the
# CLI takes the "since last update" staleness branch instead of the one
# this task actually means to exercise. Keeps the container alive so the
# fixture reads as "engine still up, doing the reflect iteration" -- what
# a real `finishing` run looks like.
def _alive_env(run_id: str) -> dict:
    name = job_container_name(run_id)
    return {"STUB_DOCKER_CONTAINERS": name, "STUB_DOCKER_RUNNING": name}


def test_status_prints_no_ended_line_while_an_iteration_is_in_progress(ctl: Ctl):
    run_id = "tst-finishing-reflecting"
    _seed_status(ctl, run_id)
    res = ctl.run("status", run_id, env=_alive_env(run_id))
    assert res.returncode == 0, res.stderr
    assert "ended:" not in res.stdout, res.stdout
    # the run really is doing an iteration right now -- that line is
    # unaffected by this fix, and its presence is what makes "ended:"
    # misleading in the first place.
    assert "iteration elapsed:" in res.stdout, res.stdout
    assert "state:     finishing" in res.stdout, res.stdout
    # `--json` is untouched: `endedAt` is exactly what `_finish()` wrote,
    # readable immediately (task 012's own contract).
    res_json = ctl.run("--json", "status", run_id, env=_alive_env(run_id))
    assert json.loads(res_json.stdout)["endedAt"] == "2024-01-01T01:02:03Z"


def test_status_duration_reads_elapsed_not_total_while_reflecting(ctl: Ctl):
    run_id = "tst-finishing-duration"
    _seed_status(ctl, run_id)
    res = ctl.run("status", run_id, env=_alive_env(run_id))
    assert res.returncode == 0, res.stderr
    lines = [ln for ln in res.stdout.splitlines() if ln.startswith("duration:")]
    assert len(lines) == 1, res.stdout
    assert "(elapsed)" in lines[0], lines[0]
    assert "(total)" not in lines[0], lines[0]


def test_status_shows_ended_once_the_finishing_window_is_over(ctl: Ctl):
    """The counterpart case: once `currentIteration` is cleared (the reflect
    iteration -- or the whole job, for a plain terminal run -- is over),
    `ended:` is exactly as before this task."""
    run_id = "tst-finished-clean"
    _seed_status(ctl, run_id, state="succeeded",
                 currentIteration=None, pendingTerminalState=None)
    res = ctl.run("status", run_id)
    assert res.returncode == 0, res.stderr
    assert "ended:     " in res.stdout, res.stdout
    lines = [ln for ln in res.stdout.splitlines() if ln.startswith("duration:")]
    assert "(total)" in lines[0], lines[0]


# --------------------------------------------------------------------------
# task 007 (#72): a human reading `ralphctl status` for a run where the
# clock beat the verdict must be able to tell it apart from an ordinary
# failed run -- both share the exact same `state: failed` / `verdict:
# unverified` cells, so the distinction has to live somewhere else on
# screen. This drives the full CLI, not just the formatting helper, so it
# also proves the field reaches the human view from a real status.json.
# --------------------------------------------------------------------------

_DEADLINE_BEAT_VERDICT_MARKER = {
    "deadline": "2026-08-26T06:53:21Z",
    "completeHonouredAt": "2026-08-26T06:53:15Z",
}


def test_status_names_the_deadline_beat_verdict_marker_for_a_human_reader(
        ctl: Ctl):
    run_id = "tst-deadline-beat-verdict"
    _seed_status(ctl, run_id, state="failed", verdict="unverified",
                 reason="review was repeatedly interrupted with no budget "
                        "or wall clock left to ask again",
                 currentIteration=None, pendingTerminalState=None,
                 deadlineBeatVerdict=_DEADLINE_BEAT_VERDICT_MARKER)
    res = ctl.run("status", run_id)
    assert res.returncode == 0, res.stderr
    assert "state:     failed" in res.stdout, res.stdout
    assert "verdict:   unverified" in res.stdout, res.stdout
    assert "deadline:  " in res.stdout, res.stdout
    assert "2026-08-26T06:53:21Z" in res.stdout
    assert "2026-08-26T06:53:15Z" in res.stdout
    assert "not a failure judgement" in res.stdout.lower()
    # --json carries the marker through untouched, for a machine consumer.
    res_json = ctl.run("--json", "status", run_id)
    assert (json.loads(res_json.stdout)["deadlineBeatVerdict"]
            == _DEADLINE_BEAT_VERDICT_MARKER)


def test_status_ordinary_failed_run_prints_no_deadline_line(ctl: Ctl):
    """The counterpart: a run that failed for an ordinary reason (no
    `deadlineBeatVerdict` in its status.json at all) prints nothing new --
    the two runs' human output must actually differ, not merely differ in
    theory."""
    ordinary_id = "tst-ordinary-failed"
    _seed_status(ctl, ordinary_id, state="failed", verdict="unverified",
                 reason="budget exhausted with tasks still pending",
                 currentIteration=None, pendingTerminalState=None)
    ordinary = ctl.run("status", ordinary_id)
    assert ordinary.returncode == 0, ordinary.stderr
    assert "deadline:" not in ordinary.stdout, ordinary.stdout

    marked_id = "tst-deadline-beat-verdict-2"
    _seed_status(ctl, marked_id, state="failed", verdict="unverified",
                 reason="budget exhausted with tasks still pending",
                 currentIteration=None, pendingTerminalState=None,
                 deadlineBeatVerdict=_DEADLINE_BEAT_VERDICT_MARKER)
    marked = ctl.run("status", marked_id)
    assert marked.returncode == 0, marked.stderr
    assert "deadline:" in marked.stdout, marked.stdout
    # Same state/verdict/reason cells, different output overall -- exactly
    # the distinction a human reader needs and the plain cells cannot give.
    assert ordinary.stdout != marked.stdout
