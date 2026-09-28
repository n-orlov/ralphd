"""Task 005 (v0.27, PRD requirement B.1/B.2/B.3, #274, prompt side): once
the approach under review has taken at least one GRANTED cure pass,
review's own prompt gains a `## Confirmation review` section built
entirely from disk (`state.read_cure_lineage`, task 004's own
`cure-lineage.json`):

* every blocking finding of the previous granted pass, named by the id of
  the cure task `insert_cure_tasks` built for it, with its clause and
  `cured_when`;
* the diff range to check: that pass's own `pre_cure_head` to the
  workspace's CURRENT HEAD;
* the three checks, in order (rule-level closure of the listed findings,
  no regression in the diff range, one green run of the PRD's own tier
  suite) plus the "do not re-audit an untouched requirement" rule;
* review's own linking obligation for this confirmation review: every
  blocking finding must carry `reopens` (one of the listed cure task ids)
  or `regression: true`; neither field downgrades the finding to
  residual; an exposed secret and an absent requirement's behaviour stay
  blocking with neither field regardless (the two B.3 exceptions).

A first review of an approach that has taken no cure pass carries none of
this. `LoopSupervisor._confirmation_review_context` builds the text, and
`_review_extra_context` folds it into every real review prompt; this
module drives `build_prompt("review", extra=sup._confirmation_review_
context())` directly, the shape `tests/test_cure_worker_prompt.py`
already uses for this same PRD wave's worker-side section.

Every node here is red without this task's change: before it,
`LoopSupervisor` has no `_confirmation_review_context` method at all
(`AttributeError` on the very first line of each test).
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from ralphd.engine import findings
from ralphd.engine.state import atomic_write_json

from tests.conftest import supervisor_factory

_supervisor = supervisor_factory(run_id="confirmation-review-prompt",
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


def test_after_one_pass_the_section_lists_findings_and_the_right_range(
        tmp_path):
    root = tmp_path / "run"
    sup = _supervisor(root, approach=1)
    atomic_write_json(sup.run.tasks_file, {"version": 1, "tasks": []})
    pre_cure_head = _init_git_repo(sup.workspace)

    blocking = [
        _finding("requirement C.1 (#141)", "evidence one", "cure one"),
        _finding("requirement C.2 (#141)", "evidence two", "cure two"),
    ]
    findings.write_findings_json(sup.run.findings_json_file, blocking)
    inserted = sup._take_cure_pass(1)
    assert len(inserted) == 2

    current_head = _commit(sup.workspace, "fix.txt")

    prompt = sup.build_prompt(
        "review", extra=sup._confirmation_review_context())

    assert SECTION_HEADING in prompt
    for cure_task_id, finding_payload in zip(inserted, blocking):
        assert cure_task_id in prompt
        assert finding_payload["clause"] in prompt
        assert finding_payload["cured_when"] in prompt

    assert f"{pre_cure_head}..{current_head}" in prompt

    idx_rule = prompt.index("closed at the level of its rule")
    idx_diff = prompt.index("breaks no requirement it touches")
    idx_tier = prompt.index("tier suite named by the PRD is green")
    assert idx_rule < idx_diff < idx_tier
    assert "not re-audit" in prompt

    assert "`reopens`" in prompt
    assert "regression: true" in prompt
    assert "downgraded to residual" in prompt
    assert "exposed secret" in prompt
    assert "absent altogether" in prompt


def test_a_resumed_fresh_supervisor_builds_the_identical_section(tmp_path):
    root = tmp_path / "run"
    sup1 = _supervisor(root, approach=1)
    atomic_write_json(sup1.run.tasks_file, {"version": 1, "tasks": []})
    _init_git_repo(sup1.workspace)

    blocking = [_finding("requirement C.1 (#141)", "evidence", "cure spec")]
    findings.write_findings_json(sup1.run.findings_json_file, blocking)
    sup1._take_cure_pass(1)
    _commit(sup1.workspace, "fix.txt")

    before = sup1._confirmation_review_context()
    assert before, "must actually build a section"

    # Episode 2: a brand-new `LoopSupervisor` over the SAME on-disk run
    # dir, nothing from `sup1` carried over in memory -- what a killed-
    # and-restarted process leaves behind.
    sup2 = _supervisor(root, approach=1)
    after = sup2._confirmation_review_context()
    assert after == before


def test_a_first_review_prompt_carries_none_of_the_section(tmp_path):
    root = tmp_path / "run"
    sup = _supervisor(root, approach=1)
    atomic_write_json(sup.run.tasks_file, {"version": 1, "tasks": []})
    _init_git_repo(sup.workspace)

    extra = sup._confirmation_review_context()
    assert extra == ""
    prompt = sup.build_prompt("review", extra=extra)

    assert SECTION_HEADING not in prompt
    assert "`reopens`" not in prompt
    assert "regression: true" not in prompt


def test_no_approach_yet_also_carries_none_of_the_section(tmp_path):
    root = tmp_path / "run"
    sup = _supervisor(root)
    assert sup._approach is None
    atomic_write_json(sup.run.tasks_file, {"version": 1, "tasks": []})

    extra = sup._confirmation_review_context()
    assert extra == ""


def test_section_removed_would_fail_the_pinning_assertions(tmp_path):
    """A control that pins the section against a hand-reverted copy of the
    extra text -- if `_confirmation_review_context` is ever deleted (or
    stops firing), this catches it exactly like `tests/test_cure_worker_
    prompt.py`'s own control does for the worker-side section."""
    root = tmp_path / "run"
    sup = _supervisor(root, approach=1)
    atomic_write_json(sup.run.tasks_file, {"version": 1, "tasks": []})
    _init_git_repo(sup.workspace)

    blocking = [_finding("requirement C.1 (#141)", "evidence", "cure spec")]
    findings.write_findings_json(sup.run.findings_json_file, blocking)
    sup._take_cure_pass(1)

    extra = sup._confirmation_review_context()
    reverted = extra.replace(SECTION_HEADING, "## Not confirmation (reverted)")
    prompt = sup.build_prompt("review", extra=reverted)
    assert SECTION_HEADING not in prompt

    real_prompt = sup.build_prompt(
        "review", extra=sup._confirmation_review_context())
    assert SECTION_HEADING in real_prompt
