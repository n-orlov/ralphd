"""Task 019 (PRD requirement E.2, #150): "When `petition_adjudications_
recorded` fires, the engine appends `**Adjudication**: CONFIRMED|REFUTED --
<remedy>` to that petition's own section in the tracked wave file and
commits it with the run's git identity when the workspace is clean,
deferring to the next task boundary when the workspace is dirty."

Two layers, mirroring the split every other petitions writer in this tree
already keeps between "decide/render" (`engine.petitions`) and "wire it into
the review step" (`LoopSupervisor`):

1. `petitions.write_back_petition_adjudication` -- the pure file-edit half:
   finds the one section in a discovered `<wave>-petitions.md` whose
   `Obligation` field matches, appends the `**Adjudication**` line inside
   THAT section (never at the end of the file), and is a no-op when nothing
   matches or the section already carries one.
2. `LoopSupervisor._write_back_petition_adjudications` (called from
   `_review_verdict` in the same step as `petitions.record_confirmed_
   adjudications`) -- decides, from a `git status --porcelain` snapshot
   taken BEFORE any file is touched, whether the edit may be committed
   immediately (a clean tree) or must be left uncommitted for whatever
   commits at the next task boundary (a dirty tree).
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from ralphd.engine import petitions
from ralphd.engine.config import JobConfig
from ralphd.engine.loop import LoopSupervisor
from ralphd.engine.runner import IterationResult
from ralphd.engine.state import RunDir, atomic_write_json
from tests.conftest import supervisor_factory

WAVE_FILE_TEXT = """# v0.19 impossibility petitions

## Petition: Requirement E.2 cannot be met

**Obligation**: Requirement E.2 cannot be met
**Type**: environmental
**Evidence**: no network route

## Petition: Task 5: a second, unrelated obligation

**Obligation**: Task 5: a second, unrelated obligation
**Type**: self-referential
**Evidence**: circular
"""


def _wave_file(workspace: Path) -> Path:
    reports = workspace / "artifacts" / "reports"
    reports.mkdir(parents=True, exist_ok=True)
    path = reports / "v0.19-petitions.md"
    path.write_text(WAVE_FILE_TEXT)
    return path


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=cwd, check=True,
                           capture_output=True, text=True)


def _git_status(cwd: Path) -> str:
    return subprocess.run(["git", "status", "--porcelain"], cwd=cwd,
                           check=True, capture_output=True,
                           text=True).stdout


def _git_log(cwd: Path) -> str:
    return subprocess.run(["git", "log", "--oneline"], cwd=cwd, check=True,
                           capture_output=True, text=True).stdout


def _init_repo(workspace: Path) -> None:
    _git(workspace, "init", "-q")
    _git(workspace, "config", "user.email", "t@example.com")
    _git(workspace, "config", "user.name", "t")
    _git(workspace, "add", "-A")
    _git(workspace, "commit", "-q", "-m", "initial")


def _result(payload: dict) -> IterationResult:
    r = IterationResult(exit_code=0)
    r.final_text = json.dumps(payload)
    return r


_supervisor = supervisor_factory(run_id="task019-petition-writeback", cfg_defaults=False,
                                 run_root="run", workspace="workspace", tasks=[])


# ---------------------------------------------------------------------------
# 1. `petitions.write_back_petition_adjudication` -- the pure file edit.
# ---------------------------------------------------------------------------

def test_confirmed_is_appended_to_the_matching_section_only(tmp_path):
    path = _wave_file(tmp_path)
    written = petitions.write_back_petition_adjudication(
        tmp_path, "Requirement E.2 cannot be met", "CONFIRMED")
    assert written == path
    parsed = {p["obligation"]: p for p in petitions.parse_petitions(
        path.read_text())}
    assert parsed["Requirement E.2 cannot be met"]["adjudication"] == "CONFIRMED"
    # the OTHER section is untouched.
    assert parsed["Task 5: a second, unrelated obligation"]["adjudication"] is None


def test_refuted_is_appended_with_its_remedy(tmp_path):
    path = _wave_file(tmp_path)
    petitions.write_back_petition_adjudication(
        tmp_path, "Task 5: a second, unrelated obligation", "REFUTED",
        "do X instead")
    parsed = {p["obligation"]: p for p in petitions.parse_petitions(
        path.read_text())}
    entry = parsed["Task 5: a second, unrelated obligation"]
    assert entry["adjudication"] == "REFUTED"
    assert entry["remedy"] == "do X instead"


def test_refuted_with_no_remedy_still_appends_a_line(tmp_path):
    path = _wave_file(tmp_path)
    petitions.write_back_petition_adjudication(
        tmp_path, "Requirement E.2 cannot be met", "REFUTED", "")
    parsed = {p["obligation"]: p for p in petitions.parse_petitions(
        path.read_text())}
    entry = parsed["Requirement E.2 cannot be met"]
    assert entry["adjudication"] == "REFUTED"
    assert entry["remedy"]  # never blank -- "no remedy stated" text


def test_no_matching_obligation_is_a_noop(tmp_path):
    _wave_file(tmp_path)
    written = petitions.write_back_petition_adjudication(
        tmp_path, "an obligation nothing filed", "CONFIRMED")
    assert written is None


def test_an_already_adjudicated_section_is_never_overwritten(tmp_path):
    path = _wave_file(tmp_path)
    petitions.write_back_petition_adjudication(
        tmp_path, "Requirement E.2 cannot be met", "CONFIRMED")
    again = petitions.write_back_petition_adjudication(
        tmp_path, "Requirement E.2 cannot be met", "REFUTED", "a remedy")
    assert again is None
    parsed = {p["obligation"]: p for p in petitions.parse_petitions(
        path.read_text())}
    # still CONFIRMED -- the second call never flipped it to REFUTED.
    assert parsed["Requirement E.2 cannot be met"]["adjudication"] == "CONFIRMED"


def test_an_invalid_adjudication_value_is_a_noop(tmp_path):
    _wave_file(tmp_path)
    assert petitions.write_back_petition_adjudication(
        tmp_path, "Requirement E.2 cannot be met", "MAYBE") is None


# ---------------------------------------------------------------------------
# 2. `LoopSupervisor._write_back_petition_adjudications` -- called from
#    `_review_verdict`, same step as `petitions.record_confirmed_
#    adjudications` (`petition_adjudications_recorded`'s own writer).
# ---------------------------------------------------------------------------

def test_review_verdict_writes_the_adjudication_into_the_tracked_file(tmp_path):
    sup = _supervisor(tmp_path)
    _wave_file(sup.workspace)
    _init_repo(sup.workspace)

    result = _result({
        "verdict": "verified",
        "petition_adjudications": [
            {"obligation": "Requirement E.2 cannot be met",
             "adjudication": "CONFIRMED"},
        ],
    })
    sup._review_verdict(result)

    path = sup.workspace / "artifacts" / "reports" / "v0.19-petitions.md"
    parsed = {p["obligation"]: p for p in petitions.parse_petitions(
        path.read_text())}
    assert parsed["Requirement E.2 cannot be met"]["adjudication"] == "CONFIRMED"


def test_a_clean_workspace_commits_the_writeback_with_the_run_identity(tmp_path):
    sup = _supervisor(tmp_path)
    _wave_file(sup.workspace)
    _init_repo(sup.workspace)
    assert _git_status(sup.workspace) == ""

    result = _result({
        "verdict": "verified",
        "petition_adjudications": [
            {"obligation": "Requirement E.2 cannot be met",
             "adjudication": "CONFIRMED"},
        ],
    })
    sup._review_verdict(result)

    # the tree is clean again -- the engine's own edit was committed.
    assert _git_status(sup.workspace) == ""
    log = _git_log(sup.workspace)
    assert log.count("\n") == 2  # initial + the writeback commit
    show = subprocess.run(
        ["git", "show", "--stat", "HEAD"], cwd=sup.workspace, check=True,
        capture_output=True, text=True).stdout
    assert "v0.19-petitions.md" in show
    author = subprocess.run(
        ["git", "log", "-1", "--format=%an <%ae>"], cwd=sup.workspace,
        check=True, capture_output=True, text=True).stdout.strip()
    assert author == "t <t@example.com>"


def test_a_dirty_workspace_defers_the_commit_to_the_next_task_boundary(tmp_path):
    sup = _supervisor(tmp_path)
    _wave_file(sup.workspace)
    _init_repo(sup.workspace)
    # a worker iteration's own edit, still uncommitted, in flight.
    (sup.workspace / "in-flight.txt").write_text("uncommitted work\n")
    assert _git_status(sup.workspace) != ""
    head_before = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=sup.workspace, check=True,
        capture_output=True, text=True).stdout.strip()

    result = _result({
        "verdict": "verified",
        "petition_adjudications": [
            {"obligation": "Requirement E.2 cannot be met",
             "adjudication": "CONFIRMED"},
        ],
    })
    sup._review_verdict(result)

    # the file WAS written --
    path = sup.workspace / "artifacts" / "reports" / "v0.19-petitions.md"
    parsed = {p["obligation"]: p for p in petitions.parse_petitions(
        path.read_text())}
    assert parsed["Requirement E.2 cannot be met"]["adjudication"] == "CONFIRMED"
    # -- but nothing was committed: HEAD did not move, and both the
    # in-flight file and the just-edited petitions file are still dirty.
    head_after = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=sup.workspace, check=True,
        capture_output=True, text=True).stdout.strip()
    assert head_after == head_before
    status = _git_status(sup.workspace)
    assert "in-flight.txt" in status
    assert "v0.19-petitions.md" in status


def test_a_non_git_workspace_still_gets_the_write(tmp_path):
    sup = _supervisor(tmp_path)  # workspace is a plain, non-git directory
    _wave_file(sup.workspace)

    result = _result({
        "verdict": "verified",
        "petition_adjudications": [
            {"obligation": "Requirement E.2 cannot be met",
             "adjudication": "CONFIRMED"},
        ],
    })
    sup._review_verdict(result)  # must not raise

    path = sup.workspace / "artifacts" / "reports" / "v0.19-petitions.md"
    parsed = {p["obligation"]: p for p in petitions.parse_petitions(
        path.read_text())}
    assert parsed["Requirement E.2 cannot be met"]["adjudication"] == "CONFIRMED"


def test_no_petition_adjudications_writes_nothing_and_touches_no_git_state(tmp_path):
    sup = _supervisor(tmp_path)
    _wave_file(sup.workspace)
    _init_repo(sup.workspace)

    result = _result({"verdict": "verified"})
    written = sup._write_back_petition_adjudications(
        json.loads(result.final_text).get("petition_adjudications"))
    assert written == []
    assert _git_status(sup.workspace) == ""
