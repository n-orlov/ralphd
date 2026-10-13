"""cure-01-01 (PRD v0.27 requirement B.1/B.3/B.4, review verdict-gate
VG-01): the confirmation section, its listed cure task ids and the no-
progress root comparison are all keyed on the most recent GRANTED cure
pass for an approach -- never on `cure-lineage.json`'s literal last
entry, which a REFUTED-petition remedy insertion (`LoopSupervisor.
_insert_refuted_remedy_tasks`) can also be, since it appends to the SAME
`passes` list through the SAME `state.insert_cure_tasks` write path a
granted pass uses.

Before this task, `_is_confirmation_review`, `_confirmation_review_
listed_cure_task_ids`, `_confirmation_review_context` and `_cure_pass_
targeted_roots` all read `passes[-1]` directly. A remedy insertion
landing after a granted pass silently became that last entry: the
confirmation section narrowed down to the remedy's own single target,
the diff range's start moved from the granted pass's own `preCureHead`
to the workspace HEAD at remedy-insertion time (already past whatever
partial fix had landed), and a finding reopening one of the granted
pass's OTHER targets lost its listed id to link `reopens` against, so it
was silently downgraded to residual instead of staying blocking.

At 32352c15 every node below is red: the listed ids are only
`["cure-01-01-2"]` (the remedy's own collision-resolved cure task, not
the granted pass's `cure-01-01`/`cure-01-02`), the diff range starts at
the post-fix HEAD rather than the granted pass's own `preCureHead`, and
`cure-01-02`'s own reopen is downgraded to residual.

Every node drives the REAL production path: `LoopSupervisor._take_cure_
pass`, `state.insert_cure_tasks`, `petitions.file_worker_petition` and
`LoopSupervisor._insert_refuted_remedy_tasks` -- no lineage document is
ever hand-assembled.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from ralphd.engine import findings
from ralphd.engine import petitions
from ralphd.engine.state import atomic_write_json

from tests.conftest import supervisor_factory

_supervisor = supervisor_factory(run_id="cure01-01-confirmation-scope",
                                 run_root="run", workspace="workspace")

SECTION_HEADING = "## Confirmation review"


def _git(workspace: Path, *args) -> None:
    subprocess.run(["git", "-C", str(workspace), *args], check=True,
                   capture_output=True)


def _head(workspace: Path) -> str:
    return subprocess.run(
        ["git", "-C", str(workspace), "rev-parse", "HEAD"],
        check=True, capture_output=True, text=True).stdout.strip()


def _init_git_repo(workspace: Path) -> str:
    _git(workspace, "init", "-q")
    _git(workspace, "config", "user.email", "test@example.com")
    _git(workspace, "config", "user.name", "test")
    (workspace / "a.txt").write_text("one\n")
    _git(workspace, "add", "a.txt")
    _git(workspace, "commit", "-q", "-m", "first")
    return _head(workspace)


def _commit(workspace: Path, name: str) -> str:
    (workspace / name).write_text("more\n")
    _git(workspace, "add", name)
    _git(workspace, "commit", "-q", "-m", name)
    return _head(workspace)


def _finding(clause: str, evidence: str, cured_when: str, **overrides) -> dict:
    payload = {"severity": "blocking", "curable": True, "clause": clause,
               "evidence": evidence, "cured_when": cured_when}
    payload.update(overrides)
    return payload


def _file_remedy_petition(sup, obligation: str, remedy: str) -> None:
    """File an OPEN petition for `obligation` into this run's own
    petitions file (`petitions.file_worker_petition`'s run-scope write
    path, task 065/#150), then drive `_insert_refuted_remedy_tasks` with a
    REFUTED adjudication naming it -- the exact call `_review_verdict`
    makes right after `_apply_refuted_petition_adjudications`."""
    errors = petitions.file_worker_petition(
        sup.workspace, obligation, "environmental",
        "measured in the sandbox", run=sup.run)
    assert errors == [], errors
    inserted = sup._insert_refuted_remedy_tasks([
        {"obligation": obligation, "adjudication": "REFUTED",
         "remedy": remedy},
    ])
    assert inserted, "the remedy must actually become a cure task"
    return inserted


def _setup_granted_pass_then_remedy(tmp_path, name: str):
    """Common scaffold: a granted pass over two blocking findings whose
    cure task ids resolve to `cure-01-01`/`cure-01-02`, a partial-fix
    commit, then one REFUTED remedy insertion against `cure-01-01`.
    Returns `(sup, pre_cure_head, post_fix_head, remedy_task_ids)`."""
    root = tmp_path / "run"
    sup = _supervisor(root, approach=1)
    atomic_write_json(sup.run.tasks_file, {"version": 1, "tasks": []})
    pre_cure_head = _init_git_repo(sup.workspace)

    blocking = [
        _finding("requirement C.1 (#141)", "evidence one", "cure one"),
        _finding("requirement C.2 (#141)", "evidence two", "cure two"),
    ]
    findings.write_findings_json(sup.run.findings_json_file, blocking)
    granted_ids = sup._take_cure_pass(1)
    assert granted_ids == ["cure-01-01", "cure-01-02"]

    # A partial fix lands -- the workspace moves on past the granted
    # pass's own `preCureHead`.
    post_fix_head = _commit(sup.workspace, "partial-fix.txt")

    remedy_ids = _file_remedy_petition(
        sup, "requirement C.1 (#141) remedy obligation",
        "rewrite the cure task's own criteria to name the missing case. "
        "Done when: the missing case is named in the criteria.")

    return sup, pre_cure_head, post_fix_head, remedy_ids


# ---------------------------------------------------------------------------
# (1) One granted pass, one remedy insertion, driven live.
# ---------------------------------------------------------------------------


def test_listed_ids_and_diff_range_survive_a_remedy_insertion(tmp_path):
    sup, pre_cure_head, post_fix_head, remedy_ids = \
        _setup_granted_pass_then_remedy(tmp_path, "listed-ids")

    # The remedy's own cure task collision-resolves to `cure-01-01-2`
    # (`cure-01-01` is already taken by the granted pass): this is what
    # the buggy `passes[-1]` read narrowed the whole section down to.
    assert remedy_ids == ["cure-01-01-2"]

    listed = sup._confirmation_review_listed_cure_task_ids()
    assert listed == {"cure-01-01", "cure-01-02"}
    assert "cure-01-01-2" not in listed

    prompt = sup.build_prompt(
        "review", extra=sup._confirmation_review_context())
    assert SECTION_HEADING in prompt
    assert "cure-01-01" in prompt
    assert "cure-01-02" in prompt

    current_head = sup._workspace_head_sha()
    assert f"{pre_cure_head}..{current_head}" in prompt
    # never the remedy's own (the workspace HEAD at remedy-insertion
    # time, already past the partial fix) as the range's START -- the
    # buggy `passes[-1]` read renders `{post_fix_head}..{current_head}`
    # instead, since `post_fix_head` is what `_insert_refuted_remedy_
    # tasks` passed as `pre_cure_head` for its own lineage entry.
    assert pre_cure_head != post_fix_head
    assert (f"{post_fix_head}..{current_head}" in prompt) is False


def test_a_finding_reopening_the_other_granted_target_stays_blocking(
        tmp_path):
    sup, _pre, _post, remedy_ids = _setup_granted_pass_then_remedy(
        tmp_path, "reopen-stays-blocking")

    # A fresh review rejection reopens `cure-01-02` -- the granted pass's
    # OTHER target, never named by the remedy insertion at all -- worded
    # differently from either original clause.
    reopen = [_finding("the second gap is still present",
                       "reworded, but the same underlying gap", "",
                       reopens="cure-01-02")]
    findings.write_findings_json(sup.run.findings_json_file, reopen)

    findings_after = sup._review_findings()
    assert len(findings_after) == 1
    assert findings_after[0]["severity"] == "blocking"


# ---------------------------------------------------------------------------
# (2) Fresh LoopSupervisor over the same run dir (disk reload) sees the
#     identical section, listed ids and non-downgrade.
# ---------------------------------------------------------------------------


def test_a_fresh_supervisor_over_the_same_run_dir_reads_it_identically(
        tmp_path):
    sup1, pre_cure_head, _post, _remedy_ids = _setup_granted_pass_then_remedy(
        tmp_path, "fresh-supervisor")

    before_listed = sup1._confirmation_review_listed_cure_task_ids()
    before_context = sup1._confirmation_review_context()
    before_is_confirmation = sup1._is_confirmation_review()
    assert before_listed == {"cure-01-01", "cure-01-02"}
    assert before_is_confirmation is True

    # Episode 2: a brand-new `LoopSupervisor`, constructed over the SAME
    # on-disk run dir, nothing from `sup1` carried over in memory.
    sup2 = _supervisor(tmp_path / "run", approach=1)

    assert sup2._confirmation_review_listed_cure_task_ids() == before_listed
    assert sup2._confirmation_review_context() == before_context
    assert sup2._is_confirmation_review() == before_is_confirmation

    # The review prompt the reloaded supervisor builds carries the same
    # section: both granted targets listed, the range starting at the
    # granted pass's own `preCureHead`.
    prompt = sup2.build_prompt(
        "review", extra=sup2._confirmation_review_context())
    assert SECTION_HEADING in prompt
    assert "cure-01-01" in prompt and "cure-01-02" in prompt
    assert f"{pre_cure_head}..{sup2._workspace_head_sha()}" in prompt

    # Non-downgrade from the reloaded supervisor: a reworded finding
    # reopening `cure-01-02` (the granted pass's OTHER target, which the
    # remedy never named) stays blocking, while an unlinked blocking
    # finding beside it is downgraded -- proving the confirmation rule
    # really runs on this reload rather than being skipped.
    findings.write_findings_json(sup2.run.findings_json_file, [
        _finding("the second gap is still present",
                 "reworded, but the same underlying gap", "",
                 reopens="cure-01-02"),
        _finding("an unrelated nit outside the cure pass's targets",
                 "not linked to any listed cure task", ""),
    ])
    severities = {f["clause"]: f["severity"]
                  for f in sup2._review_findings()}
    assert severities == {
        "the second gap is still present": "blocking",
        "an unrelated nit outside the cure pass's targets": "residual",
    }


def test_two_successive_remedy_insertions_never_narrow_the_section(
        tmp_path):
    """Sibling case the gate did not list: TWO successive remedy
    insertions after one granted pass. The section must still list only
    the granted pass's own two targets, never either remedy's own cure
    task."""
    sup, pre_cure_head, _post, remedy_ids_1 = \
        _setup_granted_pass_then_remedy(tmp_path, "two-remedies")
    assert remedy_ids_1 == ["cure-01-01-2"]

    _commit(sup.workspace, "second-partial-fix.txt")
    remedy_ids_2 = _file_remedy_petition(
        sup, "requirement C.2 (#141) remedy obligation",
        "rewrite the second cure task's own criteria similarly. "
        "Done when: the second cure task's criteria are rewritten.")
    assert len(remedy_ids_2) == 1
    assert remedy_ids_2[0] not in ("cure-01-01", "cure-01-02")

    listed = sup._confirmation_review_listed_cure_task_ids()
    assert listed == {"cure-01-01", "cure-01-02"}
    assert remedy_ids_1[0] not in listed
    assert remedy_ids_2[0] not in listed

    prompt = sup.build_prompt(
        "review", extra=sup._confirmation_review_context())
    current_head = sup._workspace_head_sha()
    assert f"{pre_cure_head}..{current_head}" in prompt

    roots = sup._cure_pass_targeted_roots(1)
    assert roots == {"cure-01-01", "cure-01-02"}


def test_b4_still_refuses_a_pass_reopening_both_granted_targets_after_a_remedy(
        tmp_path):
    """B.4's `_review_cure_pass_available` must still refuse a cure pass
    when a rejection reopens BOTH of the previous granted pass's own
    targets -- reworded, never verbatim -- even though a remedy insertion
    landed in between. If the remedy insertion had narrowed `_cure_pass_
    targeted_roots` down to its own single target, reopening only the
    OTHER original target would have read as a strict subset and wrongly
    earned another pass."""
    sup, _pre, _post, _remedy_ids = _setup_granted_pass_then_remedy(
        tmp_path, "b4-no-progress")

    assert sup._cure_pass_targeted_roots(1) == {"cure-01-01", "cure-01-02"}
    # v0.36 C.2: the refusal is the approach's round count -- one granted
    # round against an allowance of one; a no-progress rejection with a
    # round left cures instead (tests/test_cure_rounds_per_approach.py).
    sup.cfg.cure_pass_ceiling = 1

    reopen_both = [
        _finding("the first gap is still present, reworded",
                 "same underlying gap as before", "", reopens="cure-01-01"),
        _finding("the second gap is still present, reworded",
                 "same underlying gap as before", "", reopens="cure-01-02"),
    ]
    findings.write_findings_json(sup.run.findings_json_file, reopen_both)

    assert sup._review_cure_pass_available(1) is False
