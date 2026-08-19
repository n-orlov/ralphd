"""Task 020 (#5): `ralphctl status` surfaces a *failed* post-terminal
reflection as `reflection: failed (<error>)`, while a successful (or
disabled, or not-yet-finished) reflection adds nothing at all.

A failed reflection deliberately never touches the run's state / verdict /
reason (the job is already over when `reflect` runs, see docs/api.md's
`reflect`), so without this line the operator cannot tell "reflect ran and
died into a dead endpoint" from "reflect was never enabled" -- exactly the
shape incident 2 in the v0.5 PRD had.

Two tiers, no engine and no real container:
- unit tests over the pure formatter `_format_reflect_lines`;
- black-box `ralphctl status` runs over on-disk run-dir fixtures (the CLI's
  status.json fallback path), reusing test_cli_docker.py's stub-docker `Ctl`
  harness and test_cli_resume.py's `_seed_run`.
"""

from __future__ import annotations

import json

from test_cli_docker import Ctl, ctl, unix_sock
from test_cli_resume import _seed_run

from ralphd.cli.main import _format_reflect_lines

__all__ = ["ctl", "unix_sock"]


# --------------------------------------------------------------------------
# formatter unit tests
# --------------------------------------------------------------------------

def test_failed_reflection_renders_one_line_naming_the_error():
    lines = _format_reflect_lines({"ok": False, "error": "Connection error.",
                                   "endedAt": "2026-08-18T09:31:20Z"})
    assert lines == ["reflection: failed (Connection error.)"]


def test_successful_reflection_renders_nothing():
    assert _format_reflect_lines({"ok": True, "error": None,
                                  "endedAt": "2026-08-18T09:31:20Z"}) == []


def test_absent_reflect_verdict_renders_nothing():
    # reflect disabled, or the phase has not ended yet (docs/api.md: null)
    assert _format_reflect_lines(None) == []
    assert _format_reflect_lines({}) == []
    assert _format_reflect_lines("nonsense") == []


def test_failed_reflection_without_an_error_string_still_says_failed():
    lines = _format_reflect_lines({"ok": False, "error": None})
    assert lines == ["reflection: failed (reason not recorded)"]


def test_long_reflect_error_wraps_without_losing_text():
    error = "Connection error. " + ("x" * 200)
    lines = _format_reflect_lines({"ok": False, "error": error})
    assert len(lines) > 1
    assert lines[0].startswith("reflection: failed (")
    for extra in lines[1:]:
        assert extra.startswith("            ")
    rejoined = " ".join(line.removeprefix("reflection: ").removeprefix("            ")
                        for line in lines)
    assert rejoined.replace(" ", "") == f"failed({error})".replace(" ", "")


# --------------------------------------------------------------------------
# black-box `ralphctl status` over run-dir fixtures
# --------------------------------------------------------------------------

_BASE_STATUS = {
    "runId": "tst-reflect",
    "state": "failed",
    "verdict": None,
    "phase": None,
    "approach": 1,
    "iterationsUsed": 9,
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


def test_status_reports_a_failed_reflection(ctl: Ctl):
    _seed_status(ctl, "tst-reflect-failed",
                 reflect={"ok": False, "error": "Connection error.",
                          "endedAt": "2024-01-01T01:03:00Z"})
    res = ctl.run("status", "tst-reflect-failed")
    assert res.returncode == 0, res.stderr
    lines = [ln for ln in res.stdout.splitlines() if ln.startswith("reflection:")]
    assert lines == ["reflection: failed (Connection error.)"], res.stdout
    # the job's own terminal state is untouched by the reflection failure
    assert "state:     failed" in res.stdout


def test_status_output_unchanged_when_reflection_succeeded_or_was_disabled(ctl: Ctl):
    """A successful reflection (its report.md is the signal) and a run that
    never enabled reflect both render exactly the bytes this command printed
    before task 020 existed."""
    _seed_status(ctl, "tst-reflect-ok",
                 reflect={"ok": True, "error": None,
                          "endedAt": "2024-01-01T01:03:00Z"})
    _seed_status(ctl, "tst-reflect-off")

    ok = ctl.run("status", "tst-reflect-ok")
    off = ctl.run("status", "tst-reflect-off")
    assert ok.returncode == 0 and off.returncode == 0

    def normalise(text: str, rid: str) -> str:
        return text.replace(rid, "RUNID")
    assert (normalise(ok.stdout, "tst-reflect-ok")
            == normalise(off.stdout, "tst-reflect-off"))
    assert "reflection:" not in ok.stdout


def test_status_json_passes_the_reflect_verdict_through(ctl: Ctl):
    verdict = {"ok": False, "error": "Connection error.",
               "endedAt": "2024-01-01T01:03:00Z"}
    _seed_status(ctl, "tst-reflect-json", reflect=verdict)
    res = ctl.run("--json", "status", "tst-reflect-json")
    assert res.returncode == 0, res.stderr
    assert json.loads(res.stdout)["reflect"] == verdict


def test_status_json_defaults_the_reflect_field_for_a_pre_0_5_run_dir(ctl: Ctl):
    _seed_status(ctl, "tst-reflect-json-null")
    res = ctl.run("--json", "status", "tst-reflect-json-null")
    assert res.returncode == 0, res.stderr
    assert json.loads(res.stdout)["reflect"] is None
