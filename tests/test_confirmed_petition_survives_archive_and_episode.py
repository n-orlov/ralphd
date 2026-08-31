"""Task 002 (requirement C.4, #112): pin that the CONFIRMED-petition
exclusion `petitions_review_context`/`LoopSupervisor._petitions_review_
context` applies (task 001, `tests/test_confirmed_petition_not_
resurfaced.py`) survives past the one review pass that confirmed it --
the exact "for the rest of the run" half of requirement C.4 task 001's
own suite does not pin:

1. `LoopSupervisor._archive_approach` running over the run root (the
   ordinary rejection path between one approach and the next) does not
   resurrect the obligation -- and does not move/copy `petition_
   adjudications_file` anywhere, which is the structural fact that makes
   survival possible at all (it is deliberately not one of the three
   paths `_archive_approach` touches).
2. A genuinely fresh engine EPISODE -- a brand new `RunDir`/
   `LoopSupervisor` object built over the SAME run root, with the
   in-process MAC authority explicitly dropped
   (`petitions.forget_run_authority`, exactly what a process restart
   leaves behind) and a key STORE outside the run dir
   (`$RALPHD_RUN_SECRETS_DIR`) standing in for the uid-boundary default
   so the earlier episode's write still authenticates -- still excludes
   the obligation: approach N+1 cannot re-litigate what approach N's
   review already confirmed.

Both directions also pin that the WORKSPACE petitions markdown itself is
never touched by any of this: `open_petitions_in_workspace` only reads
it, `record_confirmed_adjudications`/`_archive_approach` never write to
it, so it stays textually OPEN (the raw file review originally read)
forever -- only the RENDERED review context, consulting the run-scope
record, drops the obligation.
"""

from __future__ import annotations

import secrets
from pathlib import Path

from ralphd.engine import petitions
from ralphd.engine.config import JobConfig
from ralphd.engine.loop import LoopSupervisor
from ralphd.engine.state import RunDir

TWO_OPEN_PETITIONS_MD = """# v0.15 petitions

## Requirement B, DoD predicate 9 cannot be met

**Obligation**: Requirement B, DoD predicate 9 (verbatim: "every finding is
included in the escalation").
**Type**: contradictory-clauses
**Evidence**: requirement A's own residual-finding carve-out means a
residual finding never appears in a BLOCKING escalation; the two clauses
cannot both hold.

## A second, unrelated obligation

**Obligation**: A second, wholly unrelated obligation nobody has ruled on.
**Type**: environmental
**Evidence**: unrelated evidence for the unrelated obligation.
"""

CONFIRMED_OBLIGATION = "Requirement B, DoD predicate 9"


def _write_two_open_petitions(workspace: Path) -> Path:
    reports = workspace / "artifacts" / "reports"
    reports.mkdir(parents=True, exist_ok=True)
    petitions_file = reports / "v0.15-petitions.md"
    petitions_file.write_text(TWO_OPEN_PETITIONS_MD)
    return petitions_file


def _supervisor(run_root: Path, workspace: Path) -> LoopSupervisor:
    run = RunDir(root=run_root)
    cfg = JobConfig(run_id="task002-confirmed-survives-archive-and-episode")
    return LoopSupervisor(cfg, run, workspace)


def _populate_the_three_archived_files(sup: LoopSupervisor) -> None:
    """The three paths `_archive_approach` DOES copy -- populated so the
    assertion that `petition_adjudications_file` is NOT among them has
    something to contrast against."""
    sup.run.tasks_file.write_text('{"tasks": []}')
    sup.run.notes_file.write_text("approach 1 notes\n")
    sup.run.findings_file.write_text("approach 1 findings\n")


def _assert_workspace_petitions_still_textually_open(
        petitions_file: Path) -> None:
    assert petitions_file.read_text() == TWO_OPEN_PETITIONS_MD


# ---------------------------------------------------------------------------
# Direction 1: survives `_archive_approach`.
# ---------------------------------------------------------------------------

def test_exclusion_survives_archive_approach(tmp_path):
    workspace = tmp_path / "ws"
    petitions_file = _write_two_open_petitions(workspace)
    sup = _supervisor(tmp_path / "run", workspace)
    petitions.record_confirmed_adjudications(sup.run, [
        {"obligation": CONFIRMED_OBLIGATION, "adjudication": "CONFIRMED"},
    ])
    _populate_the_three_archived_files(sup)

    sup._archive_approach(1)

    archived = sup.run.root / "approaches" / "01"
    # the three paths archival DOES move/copy:
    assert (archived / "tasks.json").exists()
    assert (archived / "notes.md").exists()
    assert (archived / "review-findings.md").exists()
    # `petition_adjudications_file` is not among them, anywhere under the
    # archive directory, by name or content.
    assert not (archived / sup.run.petition_adjudications_file.name).exists()
    assert not list(archived.rglob("petition-adjudications.json"))
    assert sup.run.petition_adjudications_file.exists()  # still at run root
    assert sup.run.petition_adjudications_file.parent == sup.run.root

    ctx = sup._petitions_review_context()
    assert "DoD predicate 9" not in ctx
    assert "wholly unrelated obligation" in ctx
    _assert_workspace_petitions_still_textually_open(petitions_file)


def test_a_second_unconfirmed_petition_is_unaffected_by_the_same_archive(
        tmp_path):
    """Companion check pinned in the same test: archival dropping ONLY the
    confirmed obligation, not collapsing the review context to nothing
    when a second, unrelated OPEN petition is still genuinely open."""
    workspace = tmp_path / "ws"
    _write_two_open_petitions(workspace)
    sup = _supervisor(tmp_path / "run", workspace)
    petitions.record_confirmed_adjudications(sup.run, [
        {"obligation": CONFIRMED_OBLIGATION, "adjudication": "CONFIRMED"},
    ])
    _populate_the_three_archived_files(sup)
    sup._archive_approach(1)

    ctx = sup._petitions_review_context()
    assert "MUST" in ctx  # the surviving open-petition instruction is present
    assert "wholly unrelated obligation" in ctx


# ---------------------------------------------------------------------------
# Direction 2: survives a genuinely fresh engine episode over the same run
# root -- approach N+1 cannot re-litigate it.
# ---------------------------------------------------------------------------

def test_exclusion_survives_a_fresh_engine_episode(tmp_path, monkeypatch):
    workspace = tmp_path / "ws"
    petitions_file = _write_two_open_petitions(workspace)
    run_root = tmp_path / "run"

    # A key store OUTSIDE the run dir stands in for the uid-boundary
    # default (`petitions.key_store_dir`) so a later episode's freshly
    # constructed authority can still authenticate approach 1's write --
    # exactly `tests/test_petition_adjudications_run_scope.py::
    # test_a_later_episode_with_a_key_store_still_authenticates`'s setup.
    store = tmp_path / "secrets"
    monkeypatch.setenv(petitions.RUN_SECRETS_DIR_ENV, str(store))

    # Episode 1 (approach 1): review confirms the obligation; the
    # rejection path archives the approach exactly as production does.
    sup1 = _supervisor(run_root, workspace)
    petitions.record_confirmed_adjudications(sup1.run, [
        {"obligation": CONFIRMED_OBLIGATION, "adjudication": "CONFIRMED"},
    ])
    _populate_the_three_archived_files(sup1)
    sup1._archive_approach(1)

    ctx1 = sup1._petitions_review_context()
    assert "DoD predicate 9" not in ctx1
    assert "wholly unrelated obligation" in ctx1

    # The restart: drop the in-process authority (nothing else can reach
    # it, and nothing else survives a real process boundary either), then
    # build a BRAND NEW RunDir/LoopSupervisor over the identical run root
    # -- a fresh engine episode driving approach 2, with no reference to
    # `sup1` or its objects at all.
    petitions.forget_run_authority(sup1.run)
    del sup1
    sup2 = _supervisor(run_root, workspace)

    ctx2 = sup2._petitions_review_context()
    assert "DoD predicate 9" not in ctx2  # approach 2 cannot re-litigate it
    assert "wholly unrelated obligation" in ctx2
    assert "MUST" in ctx2

    # and the workspace's own petitions markdown was never touched by any
    # of the above -- it is still the exact text approach 1's review read.
    _assert_workspace_petitions_still_textually_open(petitions_file)

    # the record itself lived at the run root the whole time, never
    # inside an `approaches/NN/` directory.
    assert sup2.run.petition_adjudications_file.parent == run_root
    assert not list((run_root / "approaches").rglob(
        "petition-adjudications.json"))


def test_engine_petitions_function_itself_survives_the_fresh_episode(
        tmp_path, monkeypatch):
    """Same direction 2 proof, one layer down -- `petitions.
    petitions_review_context` called directly (no supervisor), pinning
    that the wiring lives in the function itself, not merely in whatever
    object happens to call it."""
    workspace = tmp_path / "ws"
    petitions_file = _write_two_open_petitions(workspace)
    run_root = tmp_path / "run"
    store = tmp_path / "secrets"
    monkeypatch.setenv(petitions.RUN_SECRETS_DIR_ENV, str(store))

    run1 = RunDir(root=run_root)
    petitions.record_confirmed_adjudications(run1, [
        {"obligation": CONFIRMED_OBLIGATION, "adjudication": "CONFIRMED"},
    ])
    petitions.forget_run_authority(run1)
    del run1

    run2 = RunDir(root=run_root)  # fresh object, same root -- the episode
    ctx = petitions.petitions_review_context(workspace, run2)
    assert "DoD predicate 9" not in ctx
    assert "wholly unrelated obligation" in ctx
    _assert_workspace_petitions_still_textually_open(petitions_file)


def test_the_fresh_episode_survives_a_whitespace_edged_minted_key(
        tmp_path, monkeypatch):
    """Regression (found by this task's own validation, which saw this
    module exit nonzero once): the exclusion has to survive the episode
    boundary for EVERY key `secrets.token_bytes(32)` can mint, not for 95%
    of them. The store used to hold the key as raw bytes and read it back
    through `.strip()`, so a key whose first or last byte was an ASCII
    whitespace byte came back short, the rebuilt authority's keyed MAC
    mismatched, and approach 1's own CONFIRMED obligation was read as
    tampered/empty -- i.e. re-litigated by approach 2, the exact failure
    requirement C.4 forbids. The key is hex encoded in the store now
    (`petitions._decode_key_material`); the minted key here is pinned to
    the worst case (leading `\\n`, trailing `\\t`) so the check is
    deterministic rather than a ~1-in-22 flake."""
    workspace = tmp_path / "ws"
    petitions_file = _write_two_open_petitions(workspace)
    run_root = tmp_path / "run"
    monkeypatch.setenv(petitions.RUN_SECRETS_DIR_ENV, str(tmp_path / "secrets"))

    worst_case_key = b"\n" + secrets.token_bytes(30) + b"\t"
    monkeypatch.setattr(secrets, "token_bytes", lambda n: worst_case_key)

    run1 = RunDir(root=run_root)
    petitions.record_confirmed_adjudications(run1, [
        {"obligation": CONFIRMED_OBLIGATION, "adjudication": "CONFIRMED"},
    ])
    assert petitions._authority(run1).key == worst_case_key
    petitions.forget_run_authority(run1)
    monkeypatch.undo()  # the later episode mints nothing; it reads the store
    monkeypatch.setenv(petitions.RUN_SECRETS_DIR_ENV, str(tmp_path / "secrets"))

    run2 = RunDir(root=run_root)
    assert petitions._read_stored_key(run2) == worst_case_key
    obligations, tampered = petitions.confirmed_obligations(run2)
    assert tampered is False
    assert obligations == {"requirement b, dod predicate 9"}

    sup2 = _supervisor(run_root, workspace)
    ctx = sup2._petitions_review_context()
    assert "DoD predicate 9" not in ctx
    assert "wholly unrelated obligation" in ctx
    _assert_workspace_petitions_still_textually_open(petitions_file)
