"""Task 004 (#94 (c)+(d), operator-added scope): `--approaches`,
`--timeout` and `--iteration-timeout` on both `ralphctl budget` and
`ralphctl resume`, plus the resume-persistence decision for the three
(process-only -- a plain resume REVERTS them, unlike `iterations`, which
persists -- see engine/api.py's `patch_budget` docstring and docs/cli.md's
budget table).

- `test_cli_budget.py` already proves the positional `iterations` spec
  round-trips byte for byte; this module pins that the bare positional STILL
  means the iteration spec after the three new flags land (the exact
  regression success criteria calls for), then covers the three new flags
  independently, combined with the positional, and their malformed-spec /
  empty-request failure modes -- all against the same stub-engine harness
  `test_cli_budget.py` uses.
- On the `resume` side: each flag lands in job.yaml's `max_approaches`/
  `job_timeout_s`/`iteration_timeout_s`, using job.yaml's own current value
  (never a live one) as the `+N` base, and a plain resume with none of the
  three flags leaves all three keys exactly as job.yaml already had them
  (the revert direction, proven by absence of a change -- there is no live
  process for a resume to revert *from* in this harness, which is the point:
  the "revert" is really "resume never reads a live value for these three in
  the first place").
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from test_cli_budget import _seed, engine  # noqa: F401 (fixture)
from test_cli_docker import Ctl, ctl, unix_sock
from test_cli_resume import _seed_run

__all__ = ["ctl", "unix_sock", "engine"]


# --------------------------------------------------------------------------
# `ralphctl budget`
# --------------------------------------------------------------------------

def test_budget_bare_positional_still_means_the_iteration_spec(ctl: Ctl, engine):
    """The exact regression this task's successCriteria pins: an existing
    `ralphctl budget <run> +5` invocation parses and reaches the engine
    exactly as it always did, unaccompanied by any of the three new flags."""
    eng = engine()
    _seed(ctl, "tst-94-bare", eng)
    res = ctl.run("budget", "tst-94-bare", "+5")
    assert res.returncode == 0, res.stderr
    assert eng.requests[-1]["body"] == {"iterations": "+5"}


def test_budget_approaches_flag_alone(ctl: Ctl, engine):
    eng = engine(body={"maxApproaches": 4, "previousMaxApproaches": 3})
    _seed(ctl, "tst-94-approaches", eng)
    res = ctl.run("budget", "tst-94-approaches", "--approaches", "+1")
    assert res.returncode == 0, res.stderr
    assert eng.requests[-1]["body"] == {"maxApproaches": "+1"}
    assert "3 -> 4" in res.stdout


def test_budget_timeout_flag_alone(ctl: Ctl, engine):
    eng = engine(body={"jobDeadlineEpochS": 999999, "previousJobDeadlineEpochS": 900000,
                       "deadlineAt": "2001-01-01T00:00:00Z"})
    _seed(ctl, "tst-94-timeout", eng)
    res = ctl.run("budget", "tst-94-timeout", "--timeout", "+3600")
    assert res.returncode == 0, res.stderr
    assert eng.requests[-1]["body"] == {"jobDeadlineEpochS": "+3600"}
    assert "900000 -> 999999" in res.stdout


def test_budget_iteration_timeout_flag_alone(ctl: Ctl, engine):
    eng = engine(body={"iterationTimeoutS": 900, "previousIterationTimeoutS": 600})
    _seed(ctl, "tst-94-itertimeout", eng)
    res = ctl.run("budget", "tst-94-itertimeout", "--iteration-timeout", "900")
    assert res.returncode == 0, res.stderr
    assert eng.requests[-1]["body"] == {"iterationTimeoutS": "900"}
    assert "600s -> 900s" in res.stdout


def test_budget_combines_positional_and_flags_in_one_request(ctl: Ctl, engine):
    eng = engine(body={"iterations": 30, "previous": 25, "iterationsUsed": 5,
                       "maxApproaches": 4, "previousMaxApproaches": 3})
    _seed(ctl, "tst-94-combo", eng)
    res = ctl.run("budget", "tst-94-combo", "+5", "--approaches", "+1")
    assert res.returncode == 0, res.stderr
    assert eng.requests[-1]["body"] == {"iterations": "+5", "maxApproaches": "+1"}


def test_budget_malformed_approaches_exits_2_without_calling_the_api(ctl: Ctl, engine):
    eng = engine()
    _seed(ctl, "tst-94-bad-approaches", eng)
    res = ctl.run("budget", "tst-94-bad-approaches", "--approaches", "one")
    assert res.returncode == 2, (res.returncode, res.stderr)
    assert "--approaches" in res.stderr and "invalid value" in res.stderr
    assert eng.requests == []


def test_budget_no_knob_at_all_exits_2(ctl: Ctl, engine):
    eng = engine()
    _seed(ctl, "tst-94-empty", eng)
    res = ctl.run("budget", "tst-94-empty")
    assert res.returncode == 2, (res.returncode, res.stderr)
    assert "nothing to change" in res.stderr
    assert eng.requests == []


def test_budget_flags_documented_in_cli_docs(ctl: Ctl):
    doc = (Path(__file__).resolve().parents[1] / "docs" / "cli.md").read_text()
    heading = "### `ralphctl budget <run-id> <+N|N>`"
    assert heading in doc
    section = doc.split(heading, 1)[1].split("\n### ", 1)[0]
    for flag in ("--approaches", "--timeout", "--iteration-timeout"):
        assert flag in section, f"{flag} not documented in the budget section"
    # the per-knob persistence table: 4 knob rows, one saying "live", three
    # saying "reverts"
    assert section.count("**live**") == 1
    assert section.count("**reverts**") == 3


# --------------------------------------------------------------------------
# `ralphctl resume`
# --------------------------------------------------------------------------

def _job_yaml_dict(cdir: Path) -> dict:
    text = (cdir / "job.yaml").read_text()
    return {k: json.loads(v) for k, v in
            (line.split(": ", 1) for line in text.splitlines() if line)}


def test_resume_approaches_topup_bumps_job_yaml(ctl):
    _rdir, cdir = _seed_run(ctl, "tst-94r-approaches")
    # _seed_run's job.yaml already carries max_approaches: 1
    res = ctl.run("resume", "tst-94r-approaches", "--approaches", "+2")
    assert res.returncode == 0, res.stderr
    assert _job_yaml_dict(cdir)["max_approaches"] == 3


def test_resume_timeout_topup_bumps_job_yaml(ctl):
    _rdir, cdir = _seed_run(ctl, "tst-94r-timeout")
    res = ctl.run("resume", "tst-94r-timeout", "--timeout", "3600")
    assert res.returncode == 0, res.stderr
    assert _job_yaml_dict(cdir)["job_timeout_s"] == 3600


def test_resume_iteration_timeout_topup_bumps_job_yaml(ctl):
    _rdir, cdir = _seed_run(ctl, "tst-94r-itertimeout")
    res = ctl.run("resume", "tst-94r-itertimeout", "--iteration-timeout", "+300")
    assert res.returncode == 0, res.stderr
    from ralphd.engine.config import JobConfig
    expected = JobConfig().iteration_timeout_s + 300
    assert _job_yaml_dict(cdir)["iteration_timeout_s"] == expected


def test_resume_all_three_together_next_to_iterations(ctl):
    _rdir, cdir = _seed_run(ctl, "tst-94r-combo", iterations=5)
    res = ctl.run("resume", "tst-94r-combo", "--iterations", "+10",
                 "--approaches", "+1", "--timeout", "+100",
                 "--iteration-timeout", "+50")
    assert res.returncode == 0, res.stderr
    job = _job_yaml_dict(cdir)
    assert job["iterations"] == 15
    assert job["max_approaches"] == 2  # seeded job.yaml had max_approaches: 1
    from ralphd.engine.config import JobConfig
    d = JobConfig()
    assert job["job_timeout_s"] == d.job_timeout_s + 100
    assert job["iteration_timeout_s"] == d.iteration_timeout_s + 50


def test_resume_bad_approaches_value_exits_2(ctl):
    _seed_run(ctl, "tst-94r-bad")
    res = ctl.run("resume", "tst-94r-bad", "--approaches", "banana")
    assert res.returncode == 2, res.stderr
    assert "--approaches" in res.stderr and "invalid value" in res.stderr
    assert not any(a[:2] == ["run", "-d"] for a in ctl.recorded())


def test_resume_with_no_new_flags_reverts_to_job_yamls_own_value(ctl):
    """The #94 (d) decision, proven at the CLI-black-box level: the three
    new knobs are process-only. This harness never HAS a live process to
    revert away from (there is no engine running against `_seed_run`'s run
    dir) -- which is exactly the point of the decision: a plain resume
    passes the three knobs from job.yaml's own recorded values, never from
    anything a live container might have set. So job.yaml's own
    `max_approaches`/`job_timeout_s`/`iteration_timeout_s` are left
    completely untouched when the resume passes none of the three flags,
    unlike `iterations`, whose value CAN come from status.json (task
    010/#51) -- see test_cli_resume.py's
    `test_resume_iterations_topup_adds_to_status_json_not_job_yaml_when_higher`
    for the contrasting case that pins `iterations` on the other side of
    this same decision."""
    rdir, cdir = _seed_run(ctl, "tst-94r-noflags")
    before = _job_yaml_dict(cdir)
    res = ctl.run("resume", "tst-94r-noflags")
    assert res.returncode == 0, res.stderr
    after = _job_yaml_dict(cdir)
    assert after.get("max_approaches") == before.get("max_approaches")
    assert "job_timeout_s" not in after
    assert "iteration_timeout_s" not in after
