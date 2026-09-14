"""Task 054 (K.3, #188/#189/#193/#179): prove `scripts/run_bounded.sh`
actually distinguishes a `timeout-minutes`-shaped kill from a human cancel /
`cancel-in-progress` cancellation, on the two surfaces a judging hat reads
(the step's own log text and `$GITHUB_STEP_SUMMARY`) -- see that script's
own header comment for the full mechanism and why the two cases produce
different exit statuses from GNU `timeout(1)`.

Both tests run the real script as a subprocess against a real `sleep`; no
live GitHub Actions dispatch is involved or needed -- proving the negative
half (a genuine job-level `timeout-minutes` kill never fires early) would
need a real wait past a real timeout-minutes bound, which is exactly what
this mechanism exists to make unnecessary to prove per-run.
"""

from __future__ import annotations

import copy
import os
import re
import signal
import subprocess
import time
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "run_bounded.sh"

MARKER = "RALPHD_TIMEOUT_KILL:"


def _run(args: list[str], **env_overrides: str) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env.update(env_overrides)
    return subprocess.run(
        [str(SCRIPT), *args],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        env=env,
        timeout=20,
    )


@pytest.mark.timeout(30)
def test_timeout_kill_marker_is_present_when_the_inner_bound_fires(tmp_path):
    """The command overruns the script's own inner bound: this is the
    shape of a job that would otherwise die as an ambiguous job-level
    `timeout-minutes` `cancelled` -- the script kills it first and reports
    a `failure` with the marker on both surfaces instead."""
    summary_path = tmp_path / "step-summary.txt"
    summary_path.write_text("", encoding="utf-8")

    result = _run(
        ["1", "demo label", "--", "sleep", "5"],
        GITHUB_STEP_SUMMARY=str(summary_path),
    )

    assert result.returncode == 1, result.stdout
    assert f"{MARKER} demo label exceeded its internal bound of 1s" in result.stdout
    assert (
        f"{MARKER} demo label exceeded its internal bound of 1s"
        in summary_path.read_text(encoding="utf-8")
    )


@pytest.mark.timeout(30)
def test_timeout_kill_marker_is_absent_when_the_job_is_cancelled_by_signal(tmp_path):
    """The command is killed by a signal delivered from OUTSIDE the
    script well before its own inner bound could fire -- the shape of a
    human cancel or a `cancel-in-progress` supersession. No marker appears
    on either surface, so the ordinary `cancelled` conclusion is left
    exactly as informative (or uninformative) as it always was."""
    summary_path = tmp_path / "step-summary.txt"
    summary_path.write_text("", encoding="utf-8")

    env = dict(os.environ)
    env["GITHUB_STEP_SUMMARY"] = str(summary_path)

    proc = subprocess.Popen(
        [str(SCRIPT), "10", "demo label", "--", "sleep", "5"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        env=env,
        # A real job-level cancel kills the whole process tree, not just
        # the immediate child -- put this script in its own process group
        # so the signal below reaches `timeout(1)` and `sleep` the same way.
        preexec_fn=os.setsid,
    )
    time.sleep(0.3)
    os.killpg(proc.pid, signal.SIGTERM)
    out, _ = proc.communicate(timeout=10)

    assert MARKER not in out
    assert summary_path.read_text(encoding="utf-8") == ""
    # Killed by SIGTERM, well before the 10s inner bound and nowhere near
    # GNU timeout's own self-inflicted 124.
    assert proc.returncode != 124
    assert proc.returncode < 0 or proc.returncode >= 128


# --- the job-level half: scripts/job_timeout_marker.sh ----------------------
#
# The step wrapper above prevents the ambiguous ending for the wall clock a
# wrapped step owns. It cannot cover the wall clock a job spends in steps no
# wrapper owns (`actions/checkout`, an environment report, a cleanup step),
# so every job also ends with an `if: always()` classifier step -- Actions
# runs those on a cancelled job too -- which reports whether the job was
# still alive as its declared `timeout-minutes` came due. See that script's
# header for why elapsed-versus-declared-bound is the discriminator and why
# it always exits 0.

MARKER_SCRIPT = REPO_ROOT / "scripts" / "job_timeout_marker.sh"


def _run_classifier(
    args: list[str], **env_overrides: str
) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env.update(env_overrides)
    return subprocess.run(
        [str(MARKER_SCRIPT), *args],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        env=env,
        timeout=20,
    )


@pytest.mark.timeout(30)
def test_job_timeout_marker_is_present_when_the_job_level_bound_came_due(tmp_path):
    """The job has been alive for its whole declared bound: the only cause
    that can kill it at that moment is `timeout-minutes` itself, so the
    marker goes onto both surfaces a judging hat reads."""
    summary_path = tmp_path / "step-summary.txt"
    summary_path.write_text("", encoding="utf-8")
    # A 45-minute bound (gate's own), with the job's start stamped 45
    # minutes ago -- the shape of the ending, at 1/2700th of the wait.
    started = int(time.time()) - 45 * 60

    result = _run_classifier(
        ["45", "gate fast tier"],
        RALPHD_JOB_STARTED_AT=str(started),
        GITHUB_STEP_SUMMARY=str(summary_path),
    )

    assert result.returncode == 0, result.stdout
    expected = f"{MARKER} gate fast tier reached its job-level bound of 45 minutes"
    assert expected in result.stdout
    assert expected in summary_path.read_text(encoding="utf-8")


@pytest.mark.timeout(30)
def test_job_timeout_marker_is_absent_when_the_job_is_cancelled_early(tmp_path):
    """A human cancel or a `cancel-in-progress` supersession lands whenever
    the human or the newer push arrives, which is nowhere near the declared
    bound: no marker, so `cancelled` keeps exactly the meaning it had."""
    summary_path = tmp_path / "step-summary.txt"
    summary_path.write_text("", encoding="utf-8")
    started = int(time.time()) - 4 * 60

    result = _run_classifier(
        ["45", "gate fast tier"],
        RALPHD_JOB_STARTED_AT=str(started),
        GITHUB_STEP_SUMMARY=str(summary_path),
    )

    assert result.returncode == 0, result.stdout
    assert MARKER not in result.stdout
    assert summary_path.read_text(encoding="utf-8") == ""


@pytest.mark.timeout(30)
def test_the_classifier_never_fails_a_job_over_a_missing_start_stamp(tmp_path):
    """A classifier that could turn a green job red would be a worse trade
    than the ambiguity it removes: with no usable stamp it says so on stderr
    and exits 0, printing no marker."""
    summary_path = tmp_path / "step-summary.txt"
    summary_path.write_text("", encoding="utf-8")

    env = {k: v for k, v in os.environ.items() if k != "RALPHD_JOB_STARTED_AT"}
    env["GITHUB_STEP_SUMMARY"] = str(summary_path)
    result = subprocess.run(
        [str(MARKER_SCRIPT), "45", "gate fast tier"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        env=env,
        timeout=20,
    )

    assert result.returncode == 0, result.stdout
    assert MARKER not in result.stdout
    assert "RALPHD_JOB_STARTED_AT is unset" in result.stdout
    assert summary_path.read_text(encoding="utf-8") == ""


# --- the coverage guard: no lane's job can die ambiguously -----------------
#
# The two scripts only remove the ambiguity for the jobs that actually use
# them. This half reads all three workflow files and holds every job in every
# lane to the same three properties, so a new job -- or a new long step in an
# existing one -- cannot reintroduce the ambiguous ending by omission:
#
#   1. the job's FIRST step stamps `RALPHD_JOB_STARTED_AT`, and its LAST step
#      is the `if: always()` classifier, declaring the same bound the job's
#      own `timeout-minutes` declares;
#   2. every `run:` step that can consume real wall clock is wrapped in
#      `scripts/run_bounded.sh` with a numeric inner bound -- the exempt
#      steps are named one by one below, with the reason each is instant;
#   3. the wrapped inner bounds sum to at most the job's `timeout-minutes`
#      minus a reserve, so the job-level bound cannot fire before some
#      wrapped step's own bound has already reported itself.
#
# Each check is exercised against the real files and against a mutated deep
# copy dumped under `tmp_path` (never under `.github/`), the same
# non-vacuity discipline tests/test_ci_dev_layout_matrix_guard.py uses.

WORKFLOW_DIR = REPO_ROOT / ".github" / "workflows"
WORKFLOW_NAMES = ("gate.yml", "heavy.yml", "nightly.yml")

START_STAMP_STEP = "Record the job start time"
CLASSIFIER_STEP = "Classify a job-level timeout kill"
STAMP_VAR = "RALPHD_JOB_STARTED_AT"

# Steps whose `run` is exempt from the inner bound, with the reason. Each is a
# handful of `echo`/`df`/`docker image rm`/`command -v` calls against local
# state: no network fetch, no test tier, no container payload. The reserve in
# JOB_RESERVE_S is what pays for them plus `actions/checkout`.
EXEMPT_RUN_STEPS = {
    ("gate.yml", "fast", "Record the job start time"),
    ("gate.yml", "fast", "Report the environment"),
    ("gate.yml", "fast", CLASSIFIER_STEP),
    ("heavy.yml", "docker-tier", "Record the job start time"),
    ("heavy.yml", "docker-tier", "Report the environment"),
    ("heavy.yml", "docker-tier", "Remove this run's test images"),
    ("heavy.yml", "docker-tier", CLASSIFIER_STEP),
    ("heavy.yml", "browser-tier", "Record the job start time"),
    ("heavy.yml", "browser-tier", "Assert the browser toolchain is present"),
    ("heavy.yml", "browser-tier", "Close stray browser sessions"),
    ("heavy.yml", "browser-tier", CLASSIFIER_STEP),
    ("nightly.yml", "dev-layout", "Record the job start time"),
    ("nightly.yml", "dev-layout", "Report the host environment"),
    ("nightly.yml", "dev-layout", CLASSIFIER_STEP),
}

# The wall clock left unwrapped in every job, in seconds: `actions/checkout`
# at `fetch-depth: 0` plus the exempt steps above. A job whose wrapped bounds
# sum past `timeout-minutes` minus this could still reach the job-level bound
# with no step-level marker printed.
JOB_RESERVE_S = 300

BOUNDED_CALL_RE = re.compile(r"scripts/run_bounded\.sh\s+(\d+)\b")
CLASSIFIER_CALL_RE = re.compile(r"scripts/job_timeout_marker\.sh\s+(\d+)\b")


def _load_workflow(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def check_timeout_marker_coverage(workflow_name: str, workflow: dict) -> list[str]:
    """Return human-readable problems with `workflow`'s timeout-kill
    disambiguation; an empty list means no job in it can die of its own
    `timeout-minutes` without saying so."""
    problems: list[str] = []

    for job_name, job in (workflow.get("jobs") or {}).items():
        where = f"{workflow_name}:{job_name}"
        steps = job.get("steps") or []
        if not steps:
            problems.append(f"{where} has no steps")
            continue

        bound_minutes = job.get("timeout-minutes")
        if not isinstance(bound_minutes, int):
            problems.append(
                f"{where} declares no integer timeout-minutes: {bound_minutes!r}"
            )
            continue

        first, last = steps[0], steps[-1]
        if first.get("name") != START_STAMP_STEP or STAMP_VAR not in (
            first.get("run") or ""
        ):
            problems.append(
                f"{where}'s first step does not stamp {STAMP_VAR}: "
                f"{first.get('name')!r}"
            )
        if last.get("name") != CLASSIFIER_STEP:
            problems.append(
                f"{where}'s last step is {last.get('name')!r}, not "
                f"{CLASSIFIER_STEP!r}"
            )
        else:
            if str(last.get("if") or "").strip() != "always()":
                problems.append(
                    f"{where}'s classifier step is not guarded by if: always() "
                    f"-- it would be skipped on the cancelled job it exists for"
                )
            declared = CLASSIFIER_CALL_RE.search(last.get("run") or "")
            if declared is None:
                problems.append(
                    f"{where}'s classifier step does not invoke "
                    f"scripts/job_timeout_marker.sh with a numeric bound"
                )
            elif int(declared.group(1)) != bound_minutes:
                problems.append(
                    f"{where}'s classifier declares {declared.group(1)} minutes "
                    f"but the job declares timeout-minutes: {bound_minutes}"
                )

        inner_total = 0
        for step in steps:
            run = step.get("run")
            if not run:
                continue
            bounds = [int(m) for m in BOUNDED_CALL_RE.findall(run)]
            inner_total += sum(bounds)
            if bounds:
                continue
            if (workflow_name, job_name, step.get("name")) in EXEMPT_RUN_STEPS:
                continue
            problems.append(
                f"{where} step {step.get('name')!r} runs unwrapped: neither "
                f"bounded by scripts/run_bounded.sh nor a named exempt step"
            )

        allowed = bound_minutes * 60 - JOB_RESERVE_S
        if inner_total > allowed:
            problems.append(
                f"{where}'s inner bounds sum to {inner_total}s, past the "
                f"{allowed}s its timeout-minutes leaves after the "
                f"{JOB_RESERVE_S}s reserve"
            )

    return problems


@pytest.mark.parametrize("workflow_name", WORKFLOW_NAMES)
def test_every_lane_job_can_report_its_own_timeout_kill(workflow_name):
    path = WORKFLOW_DIR / workflow_name
    problems = check_timeout_marker_coverage(workflow_name, _load_workflow(path))
    assert problems == [], problems


def test_the_coverage_guard_fires_when_a_long_step_is_left_unwrapped(tmp_path):
    broken = copy.deepcopy(_load_workflow(WORKFLOW_DIR / "heavy.yml"))
    step = next(
        s
        for s in broken["jobs"]["docker-tier"]["steps"]
        if s.get("name") == "Docker tier"
    )
    step["run"] = ".venv/bin/python -m pytest -q -m docker --durations=15"

    fixture = tmp_path / "broken-heavy-unwrapped.yml"
    fixture.write_text(yaml.safe_dump(broken, sort_keys=False), encoding="utf-8")

    problems = check_timeout_marker_coverage("heavy.yml", _load_workflow(fixture))
    assert any("runs unwrapped" in p for p in problems), problems


def test_the_coverage_guard_fires_when_the_classifier_step_is_dropped(tmp_path):
    broken = copy.deepcopy(_load_workflow(WORKFLOW_DIR / "gate.yml"))
    steps = broken["jobs"]["fast"]["steps"]
    assert steps[-1]["name"] == CLASSIFIER_STEP
    del steps[-1]

    fixture = tmp_path / "broken-gate-no-classifier.yml"
    fixture.write_text(yaml.safe_dump(broken, sort_keys=False), encoding="utf-8")

    problems = check_timeout_marker_coverage("gate.yml", _load_workflow(fixture))
    assert any("not 'Classify a job-level timeout kill'" in p for p in problems), (
        problems
    )


def test_the_coverage_guard_fires_when_the_classifier_is_skipped_on_cancel(tmp_path):
    broken = copy.deepcopy(_load_workflow(WORKFLOW_DIR / "nightly.yml"))
    broken["jobs"]["dev-layout"]["steps"][-1]["if"] = "success()"

    fixture = tmp_path / "broken-nightly-if.yml"
    fixture.write_text(yaml.safe_dump(broken, sort_keys=False), encoding="utf-8")

    problems = check_timeout_marker_coverage("nightly.yml", _load_workflow(fixture))
    assert any("if: always()" in p for p in problems), problems


def test_the_coverage_guard_fires_when_the_inner_bounds_outgrow_the_job(tmp_path):
    broken = copy.deepcopy(_load_workflow(WORKFLOW_DIR / "gate.yml"))
    step = next(
        s for s in broken["jobs"]["fast"]["steps"] if s.get("name") == "Fast tier"
    )
    step["run"] = BOUNDED_CALL_RE.sub("scripts/run_bounded.sh 2700", step["run"])

    fixture = tmp_path / "broken-gate-oversized.yml"
    fixture.write_text(yaml.safe_dump(broken, sort_keys=False), encoding="utf-8")

    problems = check_timeout_marker_coverage("gate.yml", _load_workflow(fixture))
    assert any("past the" in p for p in problems), problems


def test_the_coverage_guard_fires_when_the_start_stamp_step_is_dropped(tmp_path):
    broken = copy.deepcopy(_load_workflow(WORKFLOW_DIR / "heavy.yml"))
    steps = broken["jobs"]["browser-tier"]["steps"]
    assert steps[0]["name"] == START_STAMP_STEP
    del steps[0]

    fixture = tmp_path / "broken-heavy-no-stamp.yml"
    fixture.write_text(yaml.safe_dump(broken, sort_keys=False), encoding="utf-8")

    problems = check_timeout_marker_coverage("heavy.yml", _load_workflow(fixture))
    assert any("does not stamp" in p for p in problems), problems
