"""Task 015 (requirement F.2, #145): the worker's retry prompt must carry
the verifier's own rejection forward, not just the task's unchanged
`successCriteria`.

A task re-dispatched at `validationAttempts > 0` is a RETRY: the worker's
prompt (`LoopSupervisor._worker_assignment_extra`) must gain a
"## Why the previous attempt was rejected" block naming the attempt count
and carrying `validationNotes` VERBATIM (never summarised or truncated --
the worker is the one that judges whether the notes are addressed or
petitioned). At `validationAttempts == 0` (a first dispatch, or a task
dict missing the key) that heading must be entirely absent -- its mere
presence is itself the retry signal. `worker.md` must also tell the
worker, in its own words, to address those notes or petition instead of
re-attempting the same thing unchanged.

Two in-process cases (attempts > 0 / attempts == 0) plus a lexical check
of `worker.md`, each its own named test.
"""

from __future__ import annotations

import re
from pathlib import Path

from ralphd.engine.config import JobConfig
from ralphd.engine.loop import LoopSupervisor
from ralphd.engine.state import RunDir

REPO_ROOT = Path(__file__).resolve().parents[1]
WORKER = REPO_ROOT / "src" / "ralphd" / "prompts" / "worker.md"

HEADING = "## Why the previous attempt was rejected"
NOTES = ("Missing evidence for criterion 2 -- the log file cited was never "
         "actually produced by the command run.")


def _supervisor(tmp_path: Path) -> LoopSupervisor:
    run = RunDir(root=tmp_path / "run")
    cfg = JobConfig(run_id="task015-retry-rejection")
    return LoopSupervisor(cfg, run, tmp_path / "ws")


def _task(**overrides) -> dict:
    task = {
        "id": "042",
        "title": "Some task",
        "status": "in-progress",
        "successCriteria": "Some criteria.",
    }
    task.update(overrides)
    return task


# ---------------------------------------------------------------------------
# In-process cases.
# ---------------------------------------------------------------------------

def test_retry_dispatch_carries_attempts_and_notes_verbatim(tmp_path):
    sup = _supervisor(tmp_path)
    task = _task(validationAttempts=2, validationNotes=NOTES)
    extra = sup._worker_assignment_extra(task)
    assert HEADING in extra
    assert "2" in extra.split(HEADING, 1)[1]
    # verbatim: the exact notes string appears unmangled in the block
    assert NOTES in extra


def test_first_dispatch_has_no_rejection_heading_zero_attempts(tmp_path):
    sup = _supervisor(tmp_path)
    task = _task(validationAttempts=0, validationNotes=NOTES)
    extra = sup._worker_assignment_extra(task)
    assert HEADING not in extra


def test_first_dispatch_has_no_rejection_heading_missing_key(tmp_path):
    sup = _supervisor(tmp_path)
    task = _task()
    assert "validationAttempts" not in task
    extra = sup._worker_assignment_extra(task)
    assert HEADING not in extra


def test_no_task_selectable_has_no_rejection_heading(tmp_path):
    sup = _supervisor(tmp_path)
    extra = sup._worker_assignment_extra(None)
    assert HEADING not in extra
    assert "No task is currently selectable" in extra


# ---------------------------------------------------------------------------
# Lexical check of worker.md.
# ---------------------------------------------------------------------------

def _worker_text() -> str:
    assert WORKER.is_file(), f"missing prompt file {WORKER}"
    return WORKER.read_text()


def test_worker_md_tells_the_worker_to_address_notes_or_petition():
    text = _worker_text()
    assert re.search(r"Why the previous attempt was rejected", text), (
        "worker.md does not mention the "
        "'## Why the previous attempt was rejected' block by name")
    assert re.search(r"validationNotes", text), (
        "worker.md does not reference validationNotes")
    assert re.search(r"petition|unsatisfiable", text, re.IGNORECASE), (
        "worker.md does not tell the worker it may petition or report "
        "unsatisfiable instead of blindly re-attempting")
