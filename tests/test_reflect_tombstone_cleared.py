"""Task 017 (#43): a successful reflect removes the stale FAILED.md tombstone
and, with it, the `reflect-failed` artifact alias.

`_record_reflect_outcome()` writes `artifacts/reflection/FAILED.md` when the
reflect phase produced no report, and used to only *log* on success -- so
nothing ever removed the file. The tombstone is written per attempt, but the run
dir outlives the attempt: `selfdev-v06-release` finished `succeeded / verified`
with a report on disk AND a FAILED.md beside it claiming `terminal state:
aborted (verdict unverified)`, false in every particular. It is surfaced too,
not merely present: `reflect-failed` is a documented alias in
`ARTIFACT_ALIASES`, described as "why the reflect phase left no report", so
`ralphctl artifacts <run> ls` offered it next to the report it contradicts.

A successful attempt now deletes it (`_clear_reflect_tombstone()`), emitting one
`reflect_tombstone_cleared` event. Deleting the file is the whole fix on the
artifacts surface as well: `artifact_entries()` derives its rows from the files
on disk, so there is no second piece of alias state to keep in sync.

Three tiers:

* `LoopSupervisor._record_reflect_outcome()` / `_clear_reflect_tombstone()`
  over a real run dir with a fake `run_iteration` (fast): the removal, the
  event, the no-tombstone and unremovable-file cases, and the two paths that
  must NOT remove one (a failure, and task 016's no-verdict signal path);
* the fail -> resume -> succeed sequence over one run dir, black-box through
  `ralphctl artifacts ... ls` / `show reflect-failed` with the container gone;
* the documents that promise it.
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import subprocess
from pathlib import Path

import pytest

from ralphd.engine.config import JobConfig
from ralphd.engine.loop import LoopSupervisor
from ralphd.engine.runner import IterationResult
from ralphd.engine.state import RunDir, artifact_entries
from tests.conftest import RALPHCTL

REPO = Path(__file__).resolve().parents[1]
STALE = ("# Reflection failed\n\n- error: Connection error.\n"
         "- terminal state: aborted (verdict unverified)\n")


# --------------------------------------------------------------------------
# the engine, in process
# --------------------------------------------------------------------------

def _supervisor(root: Path, run_id: str = "unit") -> LoopSupervisor:
    root.mkdir(parents=True, exist_ok=True)
    return LoopSupervisor(JobConfig(run_id=run_id, reflect=True),
                          RunDir(root=root), root)


def _reflect_dir(sup: LoopSupervisor) -> Path:
    outdir = sup.run.artifacts_dir / "reflection"
    outdir.mkdir(parents=True, exist_ok=True)
    return outdir


def _write_report(sup: LoopSupervisor) -> Path:
    report = _reflect_dir(sup) / "report.md"
    report.write_text("# stub report\n")
    return report


def _write_tombstone(sup: LoopSupervisor) -> Path:
    stale = _reflect_dir(sup) / "FAILED.md"
    stale.write_text(STALE)
    return stale


def _events(root: Path, type_: str) -> list[dict]:
    log = root / "events.jsonl"
    lines = log.read_text().splitlines() if log.exists() else []
    return [ev for ev in (json.loads(x) for x in lines)
            if ev.get("type") == type_]


class _FakeReflect:
    """Stands in for run_iteration('reflect') -- writes a report or not."""

    def __init__(self, sup: LoopSupervisor, write_report: bool = True):
        self.sup = sup
        self.write_report = write_report
        self.calls: list[str] = []

    async def __call__(self, phase: str, **kw) -> IterationResult:
        self.calls.append(phase)
        if self.write_report:
            _write_report(self.sup)
        return IterationResult(exit_code=0)


def test_a_successful_reflect_removes_an_earlier_attempts_tombstone(tmp_path):
    sup = _supervisor(tmp_path / "cleared")
    stale = _write_tombstone(sup)
    _write_report(sup)

    sup._record_reflect_outcome(IterationResult(exit_code=0))

    assert not stale.exists(), \
        "a report on disk beside a file saying there is none"
    reflect = sup.run.read_status()["reflect"]
    assert reflect["ok"] is True and reflect["error"] is None
    # ... and the removal is in the event stream, once, naming the file
    cleared = _events(sup.run.root, "reflect_tombstone_cleared")
    assert [ev["path"] for ev in cleared] == ["reflection/FAILED.md"]
    assert [ev["ok"] for ev in _events(sup.run.root, "reflect_done")] == [True]


def test_the_reflect_failed_alias_disappears_with_the_file(tmp_path):
    """The artifacts surface needs no separate fix: its rows ARE the files."""
    sup = _supervisor(tmp_path / "alias")
    _write_tombstone(sup)
    _write_report(sup)
    before = {e["key"] for e in artifact_entries(sup.run.root)}
    assert "reflect-failed" in before, before

    sup._record_reflect_outcome(IterationResult(exit_code=0))

    after = {e["key"] for e in artifact_entries(sup.run.root)}
    assert "reflect-failed" not in after, after
    assert "report" in after


def test_a_failed_reflect_still_writes_the_tombstone(tmp_path):
    """The mutation guard in the other direction: the removal must not make a
    real failure invisible."""
    sup = _supervisor(tmp_path / "failed")
    fake = _FakeReflect(sup, write_report=False)
    sup.run_iteration = fake

    asyncio.run(sup._run_reflection())

    stale = _reflect_dir(sup) / "FAILED.md"
    assert stale.exists(), "a reflect failure left no trace on disk"
    assert "wrote no artifacts/reflection/report.md" in stale.read_text()
    assert sup.run.read_status()["reflect"]["ok"] is False
    assert not _events(sup.run.root, "reflect_tombstone_cleared")


def test_a_signal_skipped_reflection_leaves_an_earlier_tombstone_alone(tmp_path):
    """Task 016's no-verdict path (#47) writes no tombstone -- and must not
    remove one either: a signal stopping THIS engine does not make an earlier
    attempt's failure untrue, and there is no report to contradict it."""
    sup = _supervisor(tmp_path / "signalled")
    stale = _write_tombstone(sup)
    sup.run_iteration = _FakeReflect(sup)
    sup.abort_on_signal(signal.SIGTERM)

    asyncio.run(sup._run_reflection())

    assert stale.exists() and stale.read_text() == STALE
    assert sup.run.read_status()["reflect"]["ok"] is None
    assert not _events(sup.run.root, "reflect_tombstone_cleared")


def test_a_success_with_no_tombstone_changes_nothing_and_says_nothing(tmp_path):
    """The common case: no file, no event, no mkdir side effects."""
    sup = _supervisor(tmp_path / "clean")
    sup.run_iteration = _FakeReflect(sup)

    asyncio.run(sup._run_reflection())

    assert sup._clear_reflect_tombstone() is False
    assert not (_reflect_dir(sup) / "FAILED.md").exists()
    assert not _events(sup.run.root, "reflect_tombstone_cleared")
    assert sup.run.read_status()["reflect"]["ok"] is True


def test_the_helper_reports_whether_there_was_a_tombstone(tmp_path):
    sup = _supervisor(tmp_path / "helper")
    _write_tombstone(sup)
    assert sup._clear_reflect_tombstone() is True
    assert sup._clear_reflect_tombstone() is False


def test_an_unremovable_tombstone_does_not_break_a_good_post_mortem(tmp_path):
    """Best effort: reflect runs when the job is already terminal, so a
    read-only artifacts dir must not turn a successful post-mortem into a
    crash (it degrades to a warning and a stale file)."""
    sup = _supervisor(tmp_path / "readonly")
    outdir = _reflect_dir(sup)
    stale = _write_tombstone(sup)
    _write_report(sup)
    os.chmod(outdir, 0o500)
    try:
        sup._record_reflect_outcome(IterationResult(exit_code=0))
    finally:
        os.chmod(outdir, 0o700)

    assert stale.exists(), "the fixture did not actually block the unlink"
    assert sup.run.read_status()["reflect"]["ok"] is True
    assert [ev["ok"] for ev in _events(sup.run.root, "reflect_done")] == [True]
    assert not _events(sup.run.root, "reflect_tombstone_cleared")


# --------------------------------------------------------------------------
# fail -> resume -> succeed, then the CLI with the container gone
# --------------------------------------------------------------------------

def _ctl(registry: Path, *argv: str, timeout: int = 60):
    env = {**os.environ, "RALPHD_REGISTRY": str(registry)}
    return subprocess.run([str(RALPHCTL), *argv], env=env,
                          capture_output=True, text=True, timeout=timeout)


def test_fail_then_resume_then_succeed_leaves_no_tombstone_or_alias(tmp_path):
    """Episode 1's reflect fails; the run is resumed and episode 2's reflect
    succeeds over the same run dir. What an operator then sees must be one
    report and nothing claiming there is none."""
    registry = tmp_path / "registry"
    root = registry / "runs" / "resumed"
    run_id = "resumed"

    # episode 1: the endpoint is dead, no report
    first = _supervisor(root, run_id)
    first.run_iteration = _FakeReflect(first, write_report=False)
    asyncio.run(first._run_reflection())
    tombstone = root / "artifacts" / "reflection" / "FAILED.md"
    assert tombstone.exists(), "episode 1 did not fail the way this test needs"
    assert _ctl(registry, "artifacts", run_id, "show",
                "reflect-failed").returncode == 0, \
        "the stale alias must be printable before the fix takes effect"

    # episode 2: a fresh engine over the same run dir, and it works
    second = _supervisor(root, run_id)
    second.run_iteration = _FakeReflect(second)
    asyncio.run(second._run_reflection())

    assert not tombstone.exists(), \
        "the resumed run advertises a reflection failure it does not have"
    listing = _ctl(registry, "artifacts", run_id, "ls")
    assert listing.returncode == 0, (listing.stdout, listing.stderr)
    assert "reflection/report.md" in listing.stdout
    assert "reflect-failed" not in listing.stdout, listing.stdout
    assert "FAILED.md" not in listing.stdout, listing.stdout
    shown = _ctl(registry, "artifacts", run_id, "show", "reflect-failed")
    assert shown.returncode == 1, (shown.stdout, shown.stderr)
    assert "FAILED.md" in shown.stderr
    # the report itself is still there and printable
    report = _ctl(registry, "artifacts", run_id, "show", "report")
    assert report.returncode == 0 and "stub report" in report.stdout


# --------------------------------------------------------------------------
# the documents
# --------------------------------------------------------------------------

@pytest.mark.parametrize("path, needles", [
    ("SPEC.md",
     ["**The tombstone is an assertion, so it only exists while it is true.**",
      "`_clear_reflect_tombstone()`, one `reflect_tombstone_cleared` event",
      "Only success falsifies a tombstone",
      "| `reflect_tombstone_cleared` | `path` |",
      "removed again as soon as an attempt produces one"]),
    ("docs/api.md",
     ["A successful attempt **removes** any `FAILED.md` an earlier attempt",
      "fail \u2192 resume \u2192 succeed run never advertises a reflection failure it "
      "does not\nhave",
      "| `reflect_tombstone_cleared` | a successful `reflect` attempt removed "
      "a stale `reflection/FAILED.md`"]),
    ("docs/cli.md",
     ["`reflect-failed` is listed only while the file is on disk, and the "
      "engine\nremoves it the moment a reflect attempt succeeds (task 017, "
      "issue #43)",
      "`show reflect-failed` exits non-zero"]),
])
def test_the_docs_state_the_new_rule(path, needles):
    text = (REPO / path).read_text()
    for needle in needles:
        assert needle in text, f"{path} is missing: {needle}"


def test_spec_12_2_explains_why_the_tombstone_is_removed_on_success():
    """The reasoning, not just the behaviour: the file is an assertion, and the
    alias listing is derived from it rather than kept separately."""
    spec = (REPO / "SPEC.md").read_text()
    section = spec.split("### 12.2 Reflection")[1].split("### 12.3 Reports")[0]
    assert "run dir\noutlives the attempt" in section
    assert "terminal state: aborted (verdict unverified)" in section
    assert "retires the `reflect-failed` alias" in section
    assert "derives its rows from the files on disk" in section
    assert "best\neffort" in section
