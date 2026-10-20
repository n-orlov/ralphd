"""Task 054 (K.3, #188/#189/#193/#179): prove `scripts/run_bounded.sh`
distinguishes its own inner-bound kill from an outside cancel, on the
surfaces a judging hat reads (the step's own log text, the optional step
summary file and the `*.expired` evidence file) -- see the script's own
header comment for the full mechanism.

The discriminator is a FACT one cause produces and the other cannot forge: a
wrapped step whose own inner bound expires writes an evidence file; an
outside cancel, however late, leaves none. On Jenkins the CI summary lists
the evidence files a build left (docs/ci.md).

Every test runs the real script as a subprocess against a real `sleep`.
"""

from __future__ import annotations

import os
import re
import signal
import subprocess
import time
from pathlib import Path

import pytest

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
    # ...and the durable fact the CI summary reports.
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
    # No evidence file either, which is what keeps the CI summary from
    # calling this ending a timeout however late it arrives.
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
    marker nor evidence: this script's own bound never got a chance to
    fire, so nothing may read the KILL-shaped ending as a timeout."""
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
