"""Task 054 (K.3, #188/#189/#193/#179): prove the two scripts actually
distinguish a `timeout-minutes`-shaped kill from a human cancel /
`cancel-in-progress` cancellation, on the two surfaces a judging hat reads
(the step's own log text and `$GITHUB_STEP_SUMMARY`) -- see each script's
own header comment for the full mechanism.

The discriminator is a FACT one cause produces and the other cannot forge: a
wrapped step whose own inner bound expires writes an evidence file, and the
job-level classifier reports a timeout only when such a file exists. It is
deliberately NOT a comparison of elapsed time against the declared bound --
that earlier shape called a human cancel 44 minutes into a 45-minute bound a
timeout, and `test_a_cancel_one_minute_before_the_bound_is_not_called_a_timeout`
below pins that exact probe.

Every test runs the real scripts as subprocesses against a real `sleep`; no
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
CANCEL_MARKER = "RALPHD_CANCEL_NOT_TIMEOUT:"


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

    evidence_dir = tmp_path / "evidence"

    result = _run(
        ["1", "demo label", "--", "sleep", "5"],
        GITHUB_STEP_SUMMARY=str(summary_path),
        RALPHD_TIMEOUT_EVIDENCE_DIR=str(evidence_dir),
    )

    assert result.returncode == 1, result.stdout
    assert f"{MARKER} demo label exceeded its internal bound of 1s" in result.stdout
    assert (
        f"{MARKER} demo label exceeded its internal bound of 1s"
        in summary_path.read_text(encoding="utf-8")
    )
    # ...and the durable fact the job-level classifier reads.
    written = sorted(evidence_dir.glob("*.expired"))
    assert len(written) == 1, [p.name for p in written]
    body = written[0].read_text(encoding="utf-8")
    assert "label=demo label" in body
    assert "effective_bound_s=1" in body


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

    evidence_dir = tmp_path / "evidence"
    evidence_dir.mkdir()
    env["RALPHD_TIMEOUT_EVIDENCE_DIR"] = str(evidence_dir)

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
    # No evidence file either, which is what keeps the job-level classifier
    # from calling this ending a timeout however late it arrives.
    assert list(evidence_dir.glob("*.expired")) == []
    # Killed by SIGTERM, well before the 10s inner bound and nowhere near
    # GNU timeout's own self-inflicted 124.
    assert proc.returncode != 124
    assert proc.returncode < 0 or proc.returncode >= 128


@pytest.mark.timeout(30)
def test_a_term_ignoring_child_still_yields_the_marker_evidence_and_failure_status(tmp_path):
    """Task 004 (K.3): the expiry has to be recorded BEFORE the escalation
    from TERM to KILL, not inferred from whichever raw exit status the
    escalation happens to leave behind. A child that traps and ignores
    TERM forces the kill-after grace period, and used to leave the run
    with a bare 137 and no marker at all -- this pins that it now reports
    exactly like a TERM-obeying child: `failure`, the marker on both
    surfaces, and the evidence file."""
    ignorer = tmp_path / "ignore_term.sh"
    ignorer.write_text(
        "#!/usr/bin/env bash\ntrap '' TERM\nsleep 30\n", encoding="utf-8"
    )
    ignorer.chmod(0o755)

    summary_path = tmp_path / "step-summary.txt"
    summary_path.write_text("", encoding="utf-8")
    evidence_dir = tmp_path / "evidence"

    started = time.time()
    result = _run(
        ["1", "stubborn label", "--", str(ignorer)],
        GITHUB_STEP_SUMMARY=str(summary_path),
        RALPHD_TIMEOUT_EVIDENCE_DIR=str(evidence_dir),
    )
    took = time.time() - started

    # Documented failure status -- never the raw 137-ish status a TERM that
    # needed escalating to KILL would otherwise leave behind.
    assert result.returncode == 1, result.stdout
    expected = f"{MARKER} stubborn label exceeded its internal bound of 1s"
    assert expected in result.stdout
    assert expected in summary_path.read_text(encoding="utf-8")
    written = sorted(evidence_dir.glob("*.expired"))
    assert len(written) == 1, [p.name for p in written]
    body = written[0].read_text(encoding="utf-8")
    assert "label=stubborn label" in body
    assert "effective_bound_s=1" in body
    # The child really did need the kill-after escalation (1s bound + up to
    # 5s of grace before KILL), not a fluke of the TERM landing early.
    assert 1 <= took < 20, took


@pytest.mark.timeout(30)
def test_a_kill_requiring_cancel_before_the_bound_leaves_neither_marker_nor_evidence(
    tmp_path,
):
    """An outside cancel that itself needs to escalate from TERM to KILL
    (because the wrapped child ignores TERM too) must still leave neither
    marker nor evidence, and the job-level classifier must not read
    whatever raw, KILL-shaped exit status results as a self-inflicted
    timeout: this script's own bound never got a chance to fire."""
    ignorer = tmp_path / "ignore_term.sh"
    ignorer.write_text(
        "#!/usr/bin/env bash\ntrap '' TERM\nsleep 30\n", encoding="utf-8"
    )
    ignorer.chmod(0o755)

    summary_path = tmp_path / "step-summary.txt"
    summary_path.write_text("", encoding="utf-8")

    env = dict(os.environ)
    env["GITHUB_STEP_SUMMARY"] = str(summary_path)

    evidence_dir = tmp_path / "evidence"
    evidence_dir.mkdir()
    env["RALPHD_TIMEOUT_EVIDENCE_DIR"] = str(evidence_dir)

    proc = subprocess.Popen(
        [str(SCRIPT), "10", "demo label", "--", str(ignorer)],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        env=env,
        preexec_fn=os.setsid,
    )
    time.sleep(0.3)
    # The outside canceller's own TERM...
    os.killpg(proc.pid, signal.SIGTERM)
    time.sleep(0.5)
    # ...escalated by the outside canceller to its own KILL, well inside
    # the 10s inner bound, because the wrapped child ignored that outside
    # TERM too.
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    out, _ = proc.communicate(timeout=10)

    assert MARKER not in out
    assert summary_path.read_text(encoding="utf-8") == ""
    assert list(evidence_dir.glob("*.expired")) == []
    assert proc.returncode != 124
    assert proc.returncode != 1
    assert proc.returncode < 0 or proc.returncode >= 128

    # And the job-level classifier, fed this exact ending, must call it a
    # cancel -- not read the KILL-shaped ending as a self-inflicted timeout.
    classifier = _run_classifier(
        ["45", "gate fast tier", "cancelled"],
        RALPHD_JOB_STARTED_AT=str(int(time.time()) - 30),
        RALPHD_TIMEOUT_EVIDENCE_DIR=str(evidence_dir),
        GITHUB_STEP_SUMMARY=str(summary_path),
    )
    assert classifier.returncode == 0, classifier.stdout
    assert MARKER not in classifier.stdout
    assert CANCEL_MARKER in classifier.stdout


def _restricted_bin_dir(tmp_path: Path, tools: list[str]) -> Path:
    """A PATH containing only `tools`, resolved from the real environment,
    with no `pgrep` -- used to prove the descendant walk needs no external
    binary, and (task 009) that a genuinely absent `pgrep` is reported
    rather than silently read as "no children"."""
    import shutil as _shutil

    bin_dir = tmp_path / "restricted-bin"
    bin_dir.mkdir()
    for tool in tools:
        found = _shutil.which(tool)
        assert found, f"{tool} not found on this host, cannot build a restricted PATH"
        os.symlink(found, bin_dir / tool)
    assert _shutil.which("pgrep", path=str(bin_dir)) is None
    return bin_dir


@pytest.mark.timeout(30)
def test_run_bounded_reaches_a_grandchild_with_pgrep_unreachable_through_path(
    tmp_path,
):
    """Task 009 (M.3, operator steer 008): the descendant walk must reach a
    two-level-deep descendant (the wrapped command's own child's child) with
    `pgrep` nowhere on `PATH` at all -- proving the walk goes through
    `/proc/<pid>/task/<pid>/children`, which needs no external binary, and
    not through the old `pgrep -P` that silently found nothing on the
    `procps`-less nightly images (steer 008). If `children_of()` is ever
    reverted to `pgrep`-only, `pgrep` is unreachable here too, so the
    grandchild below is never signalled, stays alive holding the pipe open,
    and this test times out on `communicate()` instead of passing -- the
    same shape the nightly failure had before commit 545213e."""
    bin_dir = _restricted_bin_dir(
        tmp_path, ["bash", "sh", "sleep", "cat", "mkdir", "date", "tr", "env"]
    )

    grandchild_pid_file = tmp_path / "grandchild.pid"
    parent = tmp_path / "spawns_grandchild.sh"
    parent.write_text(
        "#!/usr/bin/env bash\n"
        "trap '' TERM\n"
        "sleep 30 &\n"
        "gc=$!\n"
        f"echo $gc >'{grandchild_pid_file}'\n"
        "wait $gc\n",
        encoding="utf-8",
    )
    parent.chmod(0o755)

    summary_path = tmp_path / "step-summary.txt"
    summary_path.write_text("", encoding="utf-8")
    evidence_dir = tmp_path / "evidence"

    env = dict(os.environ)
    env["PATH"] = str(bin_dir)
    env["GITHUB_STEP_SUMMARY"] = str(summary_path)
    env["RALPHD_TIMEOUT_EVIDENCE_DIR"] = str(evidence_dir)

    started = time.time()
    result = subprocess.run(
        [str(SCRIPT), "1", "grandchild label", "--", str(parent)],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        env=env,
        timeout=20,
    )
    took = time.time() - started

    assert result.returncode == 1, result.stdout
    expected = f"{MARKER} grandchild label exceeded its internal bound of 1s"
    assert expected in result.stdout
    assert expected in summary_path.read_text(encoding="utf-8")

    grandchild_pid = int(grandchild_pid_file.read_text(encoding="utf-8").strip())
    with pytest.raises(ProcessLookupError):
        os.kill(grandchild_pid, 0)
    # 1s bound + up to 5s of TERM-ignoring grace, comfortably inside the 20s
    # harness timeout, and named in the commit message per task 009.
    assert 1 <= took < 20, took


@pytest.mark.timeout(30)
def test_run_bounded_reaches_a_grandchild_through_a_term_obeying_parent(tmp_path):
    """Task 010 (M.3, operator steer 008): the complementary case to task
    009's `test_run_bounded_reaches_a_grandchild_with_pgrep_unreachable_through_path`,
    which wraps a command that ITSELF ignores TERM. Here the wrapped command
    has no TERM trap at all -- it obeys the very first TERM it receives and
    dies immediately -- but it has already spawned a background grandchild
    (its own `sleep 30 &`) before that TERM arrives. `kill_tree` walks
    depth-first and signals every descendant it finds strictly BEFORE the
    pid it was called on, so the grandchild is (in this walk order) signalled
    ahead of the TERM-obeying parent -- but only if the walk can still find
    it. With `pgrep` unreachable and `children_of()` reverted to a
    pgrep-only walk, `children_of(cmd_pid)` finds nothing, so `kill_tree`
    sends TERM to the parent alone; the parent (obeying it) exits at once,
    but that leaves the grandchild orphaned and still running its own 30s
    sleep, holding this step's stdout pipe open long after the parent -- and
    this script's own report -- have already finished. A TERM-obeying parent
    proves only that this tree's ROOT was reaped, never that the rest of the
    tree was too; this pins the latter with a parent that dies on its own
    the instant it is signalled, so the only thing that can still be left
    running is a grandchild the walk failed to reach."""
    bin_dir = _restricted_bin_dir(
        tmp_path, ["bash", "sh", "sleep", "cat", "mkdir", "date", "tr", "env"]
    )

    grandchild_pid_file = tmp_path / "grandchild.pid"
    parent = tmp_path / "spawns_grandchild_obeys_term.sh"
    parent.write_text(
        "#!/usr/bin/env bash\n"
        "sleep 30 &\n"
        "gc=$!\n"
        f"echo $gc >'{grandchild_pid_file}'\n"
        "wait $gc\n",
        encoding="utf-8",
    )
    parent.chmod(0o755)

    summary_path = tmp_path / "step-summary.txt"
    summary_path.write_text("", encoding="utf-8")
    evidence_dir = tmp_path / "evidence"

    env = dict(os.environ)
    env["PATH"] = str(bin_dir)
    env["GITHUB_STEP_SUMMARY"] = str(summary_path)
    env["RALPHD_TIMEOUT_EVIDENCE_DIR"] = str(evidence_dir)

    started = time.time()
    result = subprocess.run(
        [str(SCRIPT), "1", "obedient parent label", "--", str(parent)],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        env=env,
        timeout=20,
    )
    took = time.time() - started

    assert result.returncode == 1, result.stdout
    expected = f"{MARKER} obedient parent label exceeded its internal bound of 1s"
    assert expected in result.stdout
    assert expected in summary_path.read_text(encoding="utf-8")
    written = sorted(evidence_dir.glob("*.expired"))
    assert len(written) == 1, [p.name for p in written]

    grandchild_pid = int(grandchild_pid_file.read_text(encoding="utf-8").strip())
    with pytest.raises(ProcessLookupError):
        os.kill(grandchild_pid, 0)
    # The parent obeys TERM on the spot -- no KILL-after-grace escalation
    # needed -- so a correctly-reaped grandchild must return this well
    # inside the 30s its own unreaped sleep would otherwise still be
    # running for, and comfortably inside the 20s harness timeout.
    assert 1 <= took < 20, took


@pytest.mark.timeout(15)
def test_children_of_reports_a_missing_pgrep_on_stderr_instead_of_no_children(
    tmp_path,
):
    """Task 009 (M.3): with `/proc/<pid>/task/<pid>/children` unreadable
    (a nonexistent pid) and `pgrep` absent from `PATH`, `children_of()` must
    say so on stderr, not silently emit nothing indistinguishable from "this
    pid really has no children" -- the exact missing-tool-reads-as-absent-
    result shape steer 008 diagnosed, one level down in the same function's
    own fallback."""
    script_text = SCRIPT.read_text(encoding="utf-8")
    match = re.search(r"^children_of\(\) \{.*?\n\}\n", script_text, re.S | re.M)
    assert match, "children_of() not found verbatim in run_bounded.sh"
    func_src = match.group(0)

    bin_dir = _restricted_bin_dir(tmp_path, ["bash", "cat"])

    env = dict(os.environ)
    env["PATH"] = str(bin_dir)
    result = subprocess.run(
        ["bash", "-c", func_src + "\nchildren_of 999999999"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
        timeout=10,
    )

    assert result.stdout == ""
    assert "pgrep is not installed" in result.stderr
    assert "999999999" in result.stderr


@pytest.mark.timeout(60)
def test_the_inner_bound_is_clamped_to_the_jobs_remaining_wall_clock(tmp_path):
    """A declared inner bound is an upper bound, not a promise: if earlier
    steps were slow, a step's own bound can outlive the job's
    `timeout-minutes` and the runner's ambiguous job-level kill lands first.
    With the job's start and bound exported, the wrapper clamps itself down
    to the budget that is actually left, so the expiry (and its evidence)
    happens from the inside, in time to be reported.

    The clamped figure is read out of the wrapper's own line rather than
    hardcoded, and then required to be consistent everywhere it appears.
    The nominal budget here is 3s (60s of job bound minus a 57s tail), but
    the exact integer is inherently either 3 or 2: this test can only export
    whole seconds (`int(time.time())`, truncated DOWN), while the wrapper
    reads its own `date +%s` a moment later, so whenever the process spawn
    crosses a second boundary the wrapper correctly computes one second
    less. Pinning the digit made this node flake on a loaded runner (gate
    35066139757 at 7d74c00f: "exceeded its internal bound of 2s"); pinning
    the *agreement* of all three reports of it, plus the clamp itself and
    the from-the-inside expiry, is what the wrapper actually owes."""
    evidence_dir = tmp_path / "evidence"

    started = time.time()
    result = _run(
        ["600", "late step", "--", "sleep", "120"],
        RALPHD_TIMEOUT_EVIDENCE_DIR=str(evidence_dir),
        RALPHD_JOB_STARTED_AT=str(int(started)),
        RALPHD_JOB_BOUND_MINUTES="1",
        # 60s of job bound minus this tail leaves a 3s budget.
        RALPHD_JOB_BOUND_TAIL_S="57",
    )
    took = time.time() - started

    assert result.returncode == 1, result.stdout
    # the clamp happened at all, and from the declared 600s ...
    clamp = re.search(r"clamping late step's inner bound from 600s to (\d+)s",
                      result.stdout)
    assert clamp, result.stdout
    effective = int(clamp.group(1))
    # ... down to the job's remaining budget: 3s nominally, 2s when the
    # spawn crossed a whole-second boundary (see the docstring); never the
    # declared bound, never zero, never negative.
    assert effective in (2, 3), result.stdout
    # ... and the expiry that followed reports that SAME clamped bound, in
    # both places the operator can read it: the greppable marker line ...
    assert (f"{MARKER} late step exceeded its internal bound of {effective}s"
            in result.stdout), result.stdout
    assert took < 20, took
    # ... and the recorded evidence file.
    body = (evidence_dir / "late-step.expired").read_text(encoding="utf-8")
    assert f"effective_bound_s={effective}" in body
    assert "declared_bound_s=600" in body


# --- the job-level half: scripts/job_timeout_marker.sh ----------------------
#
# The step wrapper above prevents the ambiguous ending for the wall clock a
# wrapped step owns, and (with the clamp) for the wall clock the job has left
# whichever wrapped step is running when it runs out. Every job also ends with
# an `if: always()` classifier step -- Actions runs those on a cancelled job
# too -- which turns the wrapper's recorded expiry, or its absence, into one of
# two disjoint greppable lines. It classifies from that recorded fact and from
# `${{ job.status }}`, never from elapsed time; see the script's header for why
# the elapsed-versus-declared-bound comparison it replaced was wrong.

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


def _expire_a_wrapped_step(evidence_dir: Path, label: str) -> None:
    """Produce a real expiry the way a real job does: run the real wrapper
    against a command that overruns a 1s inner bound, into `evidence_dir`."""
    evidence_dir.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    env["RALPHD_TIMEOUT_EVIDENCE_DIR"] = str(evidence_dir)
    done = subprocess.run(
        [str(SCRIPT), "1", label, "--", "sleep", "5"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        env=env,
        timeout=30,
    )
    assert done.returncode == 1, done.stdout


@pytest.mark.timeout(60)
def test_job_timeout_marker_is_present_when_a_bounded_step_ran_out_of_clock(tmp_path):
    """A wrapped step's own bound expired, so this job's wall clock genuinely
    ran out: the marker goes onto both surfaces a judging hat reads and names
    the step whose clock ran out. Note the job here is only seconds old -- the
    claim rests on the recorded expiry, not on being near the bound."""
    summary_path = tmp_path / "step-summary.txt"
    summary_path.write_text("", encoding="utf-8")
    evidence_dir = tmp_path / "evidence"
    _expire_a_wrapped_step(evidence_dir, "fast tier")

    result = _run_classifier(
        ["45", "gate fast tier", "cancelled"],
        RALPHD_JOB_STARTED_AT=str(int(time.time()) - 30),
        RALPHD_TIMEOUT_EVIDENCE_DIR=str(evidence_dir),
        GITHUB_STEP_SUMMARY=str(summary_path),
    )

    assert result.returncode == 0, result.stdout
    expected = f"{MARKER} gate fast tier ran out of wall clock in fast tier (bound 1s)"
    assert expected in result.stdout
    assert expected in summary_path.read_text(encoding="utf-8")
    assert CANCEL_MARKER not in result.stdout


@pytest.mark.timeout(60)
def test_a_cancel_one_minute_before_the_bound_is_not_called_a_timeout(tmp_path):
    """The probe that refuted the elapsed-versus-bound shape: a human cancel
    44 minutes into a 45-minute bound. No bounded step recorded an expiry, so
    this is a cancel and the classifier says so -- the timeout marker is
    absent no matter how close to the declared bound the cancel arrives."""
    summary_path = tmp_path / "step-summary.txt"
    summary_path.write_text("", encoding="utf-8")
    evidence_dir = tmp_path / "evidence"
    evidence_dir.mkdir()

    result = _run_classifier(
        ["45", "gate fast tier", "cancelled"],
        RALPHD_JOB_STARTED_AT=str(int(time.time()) - 44 * 60),
        RALPHD_TIMEOUT_EVIDENCE_DIR=str(evidence_dir),
        GITHUB_STEP_SUMMARY=str(summary_path),
    )

    assert result.returncode == 0, result.stdout
    assert MARKER not in result.stdout
    said = (
        f"{CANCEL_MARKER} gate fast tier was cancelled with no wall-clock "
        "bound expiry recorded"
    )
    assert said in result.stdout
    assert said in summary_path.read_text(encoding="utf-8")


@pytest.mark.timeout(30)
def test_job_timeout_marker_is_absent_when_the_job_is_cancelled_early(tmp_path):
    """A `cancel-in-progress` supersession four minutes in: the same absence
    of a recorded expiry, reported the same way, so `cancelled` never has to
    be read as a verdict in either direction."""
    summary_path = tmp_path / "step-summary.txt"
    summary_path.write_text("", encoding="utf-8")
    evidence_dir = tmp_path / "evidence"
    evidence_dir.mkdir()

    result = _run_classifier(
        ["45", "gate fast tier", "cancelled"],
        RALPHD_JOB_STARTED_AT=str(int(time.time()) - 4 * 60),
        RALPHD_TIMEOUT_EVIDENCE_DIR=str(evidence_dir),
        GITHUB_STEP_SUMMARY=str(summary_path),
    )

    assert result.returncode == 0, result.stdout
    assert MARKER not in result.stdout
    assert CANCEL_MARKER in result.stdout


@pytest.mark.timeout(30)
def test_a_job_that_ended_on_its_own_gets_neither_line(tmp_path):
    """Neither line is noise a passing or failing job has to carry: with no
    recorded expiry and a status that is not `cancelled`, there is nothing to
    disambiguate and the classifier stays silent."""
    summary_path = tmp_path / "step-summary.txt"
    summary_path.write_text("", encoding="utf-8")
    evidence_dir = tmp_path / "evidence"
    evidence_dir.mkdir()

    for status in ("success", "failure"):
        result = _run_classifier(
            ["45", "gate fast tier", status],
            RALPHD_JOB_STARTED_AT=str(int(time.time()) - 90),
            RALPHD_TIMEOUT_EVIDENCE_DIR=str(evidence_dir),
            GITHUB_STEP_SUMMARY=str(summary_path),
        )
        assert result.returncode == 0, result.stdout
        assert MARKER not in result.stdout
        assert CANCEL_MARKER not in result.stdout
        assert summary_path.read_text(encoding="utf-8") == ""


@pytest.mark.timeout(30)
def test_the_classifier_never_fails_a_job_over_a_missing_start_stamp(tmp_path):
    """A classifier that could turn a green job red would be a worse trade
    than the ambiguity it removes: with no usable stamp it still classifies
    from the recorded expiry, reports the elapsed figure as unknown, and
    exits 0."""
    summary_path = tmp_path / "step-summary.txt"
    summary_path.write_text("", encoding="utf-8")
    evidence_dir = tmp_path / "evidence"
    evidence_dir.mkdir()

    env = {k: v for k, v in os.environ.items() if k != "RALPHD_JOB_STARTED_AT"}
    env["GITHUB_STEP_SUMMARY"] = str(summary_path)
    env["RALPHD_TIMEOUT_EVIDENCE_DIR"] = str(evidence_dir)
    result = subprocess.run(
        [str(MARKER_SCRIPT), "45", "gate fast tier", "cancelled"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        env=env,
        timeout=20,
    )

    assert result.returncode == 0, result.stdout
    assert MARKER not in result.stdout
    assert "RALPHD_JOB_STARTED_AT is unset" in result.stdout
    assert "elapsed unknown" in result.stdout


@pytest.mark.timeout(30)
def test_the_classifier_claims_nothing_when_it_has_no_evidence_directory(tmp_path):
    """With no evidence directory exported it cannot tell the two causes
    apart, so it prints neither line and says why on stderr rather than
    defaulting to an answer."""
    summary_path = tmp_path / "step-summary.txt"
    summary_path.write_text("", encoding="utf-8")

    env = {k: v for k, v in os.environ.items() if k != "RALPHD_TIMEOUT_EVIDENCE_DIR"}
    env["GITHUB_STEP_SUMMARY"] = str(summary_path)
    env["RALPHD_JOB_STARTED_AT"] = str(int(time.time()) - 44 * 60)
    result = subprocess.run(
        [str(MARKER_SCRIPT), "45", "gate fast tier", "cancelled"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        env=env,
        timeout=20,
    )

    assert result.returncode == 0, result.stdout
    assert MARKER not in result.stdout
    assert CANCEL_MARKER not in result.stdout
    assert "RALPHD_TIMEOUT_EVIDENCE_DIR is unset" in result.stdout
    assert summary_path.read_text(encoding="utf-8") == ""


# --- the coverage guard: no lane's job can die ambiguously -----------------
#
# The two scripts only remove the ambiguity for the jobs that actually use
# them. This half reads all three workflow files and holds every job in every
# lane to the same three properties, so a new job -- or a new long step in an
# existing one -- cannot reintroduce the ambiguous ending by omission:
#
#   1. the job's FIRST step stamps `RALPHD_JOB_STARTED_AT`, the job's own
#      `timeout-minutes` as `RALPHD_JOB_BOUND_MINUTES`, and a FRESH
#      `RALPHD_TIMEOUT_EVIDENCE_DIR`; its LAST step is the `if: always()`
#      classifier, declaring the same bound and passing the job's status so a
#      cancellation can be named as one;
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
BOUND_VAR = "RALPHD_JOB_BOUND_MINUTES"
EVIDENCE_VAR = "RALPHD_TIMEOUT_EVIDENCE_DIR"
JOB_STATUS_EXPR = "job.status"

# Steps whose `run` is exempt from the inner bound, with the reason. Each is a
# handful of `echo`/`df`/`docker image rm`/`command -v` calls against local
# state: no network fetch, no test tier, no container payload. The reserve in
# JOB_RESERVE_S is what pays for them plus `actions/checkout`.
EXEMPT_RUN_STEPS = {
    ("gate.yml", "fast", "Record the job start time"),
    ("gate.yml", "fast", "Report the environment"),
    # Task 009 (requirement F): one `git config --global --add
    # safe.directory` write against this job's own checkout path -- local,
    # no network fetch, sub-second, same shape as the other exemptions here.
    ("gate.yml", "fast",
     "Trust the checked-out workspace (container root != host checkout owner)"),
    # Task 009 (requirement F): `useradd` plus one recursive `chown` of this
    # job's own checkout and evidence directory -- local, no network fetch,
    # a handful of seconds even over this tree.
    ("gate.yml", "fast", "Create the fast tier's non-root account"),
    ("gate.yml", "fast", CLASSIFIER_STEP),
    ("heavy.yml", "docker-tier", "Record the job start time"),
    # Task 004 (requirement B, #202): the pre-checkout litter reaper. It runs
    # BEFORE anything is checked out, so `scripts/run_bounded.sh` does not
    # exist yet to wrap it; its payload is a local `rm -rf` of at most a
    # handful of directories in a throwaway root container.
    ("heavy.yml", "docker-tier",
     "Reap root-owned test litter left in the runner workspace"),
    ("heavy.yml", "docker-tier", "Report the environment"),
    ("heavy.yml", "docker-tier", "Remove this run's test images"),
    ("heavy.yml", "docker-tier", CLASSIFIER_STEP),
    ("heavy.yml", "browser-tier", "Record the job start time"),
    ("heavy.yml", "browser-tier",
     "Reap root-owned test litter left in the runner workspace"),
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
STAMPED_BOUND_RE = re.compile(BOUND_VAR + r"=(\d+)")


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
        first_run = first.get("run") or ""
        if first.get("name") != START_STAMP_STEP or STAMP_VAR not in first_run:
            problems.append(
                f"{where}'s first step does not stamp {STAMP_VAR}: "
                f"{first.get('name')!r}"
            )
        else:
            stamped = STAMPED_BOUND_RE.search(first_run)
            if stamped is None:
                problems.append(
                    f"{where}'s first step does not stamp {BOUND_VAR}, so a "
                    f"bounded step cannot clamp itself to the job's budget"
                )
            elif int(stamped.group(1)) != bound_minutes:
                problems.append(
                    f"{where} stamps {BOUND_VAR}={stamped.group(1)} but declares "
                    f"timeout-minutes: {bound_minutes}"
                )
            if f"{EVIDENCE_VAR}=" not in first_run:
                problems.append(
                    f"{where}'s first step does not export {EVIDENCE_VAR}, so an "
                    f"expiry would leave no evidence for the classifier"
                )
            elif "rm -rf" not in first_run:
                problems.append(
                    f"{where}'s first step does not clear {EVIDENCE_VAR} before "
                    f"using it: a reused runner's stale evidence would be read "
                    f"as this job's own"
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
            classifier_run = last.get("run") or ""
            declared = CLASSIFIER_CALL_RE.search(classifier_run)
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
            if JOB_STATUS_EXPR not in classifier_run:
                problems.append(
                    f"{where}'s classifier step does not pass {JOB_STATUS_EXPR}, "
                    f"so a cancellation cannot be reported as a cancel"
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


def test_the_coverage_guard_fires_when_the_evidence_directory_is_not_exported(tmp_path):
    broken = copy.deepcopy(_load_workflow(WORKFLOW_DIR / "gate.yml"))
    step = broken["jobs"]["fast"]["steps"][0]
    assert step["name"] == START_STAMP_STEP
    step["run"] = 'echo "RALPHD_JOB_STARTED_AT=$(date +%s)" >> "$GITHUB_ENV"\n'

    fixture = tmp_path / "broken-gate-no-evidence-dir.yml"
    fixture.write_text(yaml.safe_dump(broken, sort_keys=False), encoding="utf-8")

    problems = check_timeout_marker_coverage("gate.yml", _load_workflow(fixture))
    assert any(f"does not export {EVIDENCE_VAR}" in p for p in problems), problems
    assert any(f"does not stamp {BOUND_VAR}" in p for p in problems), problems


def test_the_coverage_guard_fires_when_the_classifier_gets_no_job_status(tmp_path):
    broken = copy.deepcopy(_load_workflow(WORKFLOW_DIR / "nightly.yml"))
    last = broken["jobs"]["dev-layout"]["steps"][-1]
    assert last["name"] == CLASSIFIER_STEP
    last["run"] = 'scripts/job_timeout_marker.sh 120 "nightly dev-layout cell"'

    fixture = tmp_path / "broken-nightly-no-status.yml"
    fixture.write_text(yaml.safe_dump(broken, sort_keys=False), encoding="utf-8")

    problems = check_timeout_marker_coverage("nightly.yml", _load_workflow(fixture))
    assert any(f"does not pass {JOB_STATUS_EXPR}" in p for p in problems), problems


def test_the_coverage_guard_fires_when_the_stamped_bound_drifts_from_the_job(tmp_path):
    broken = copy.deepcopy(_load_workflow(WORKFLOW_DIR / "heavy.yml"))
    step = broken["jobs"]["docker-tier"]["steps"][0]
    step["run"] = STAMPED_BOUND_RE.sub(f"{BOUND_VAR}=999", step["run"])

    fixture = tmp_path / "broken-heavy-bound-drift.yml"
    fixture.write_text(yaml.safe_dump(broken, sort_keys=False), encoding="utf-8")

    problems = check_timeout_marker_coverage("heavy.yml", _load_workflow(fixture))
    assert any(f"stamps {BOUND_VAR}=999" in p for p in problems), problems
