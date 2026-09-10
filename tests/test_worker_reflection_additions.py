"""Task 041 (#170, #175, PRD requirement I.3): land the v0.19.1
reflection's `suggestions.diff` (#175) additions to `worker.md` -- four
statements the prompt did not previously make: evidence you invalidate is
yours to re-take, a lane is re-measured in the same environment, the two
refused split shapes (forced vs voluntary), and a refused `plan_complete`
is work, not formatting.
"""

from __future__ import annotations

from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
WORKER_MD = (REPO / "src" / "ralphd" / "prompts" / "worker.md").read_text()


def test_evidence_you_invalidate_is_yours_to_retake():
    assert "Evidence you invalidate is yours to re-take" in WORKER_MD
    idx = WORKER_MD.index("Evidence you invalidate is yours to re-take")
    para = " ".join(WORKER_MD[idx:idx + 700].split())
    assert "re-taking it is" in para
    assert "your job" in para
    assert "exit-status file" in para
    assert "never erased by" in para


def test_a_lane_is_re_measured_in_the_same_environment():
    assert "re-measured\nin the same environment" in WORKER_MD or \
        "re-measured in the same environment" in " ".join(WORKER_MD.split())
    idx = WORKER_MD.index("re-measured")
    para = " ".join(WORKER_MD[idx:idx + 400].split())
    assert "same checkout, same venv, same layout" in para
    assert "git worktree" in para


def test_the_two_refused_split_shapes_are_named_and_distinguished():
    assert "two refused split shapes are different" in WORKER_MD
    idx = WORKER_MD.index("two refused split shapes are different")
    section = WORKER_MD[idx:idx + 1200]
    assert "**forced**" in section
    assert "**voluntary**" in section
    # the forced shape is a contract-ladder re-ask; the voluntary shape is
    # a silent refusal-and-log with no retry -- the asymmetry is the point.
    assert "is not optional" in section
    assert "asked again through the contract\n   ladder" in section or \
        "asked again through the contract" in " ".join(section.split())
    assert "refused and logged\n   silently" in section or \
        "refused and logged silently" in " ".join(section.split())


def test_a_refused_plan_complete_is_work_not_formatting():
    assert "A refused `plan_complete` is work, not formatting" in WORKER_MD
    idx = WORKER_MD.index("A refused `plan_complete` is work")
    para = " ".join(WORKER_MD[idx:idx + 500].split())
    assert "completed" in para and "validated" in para and "skipped" in para
    assert "task_status" in para
    assert "claim `plan_complete` again next iteration" in para


def test_worker_md_additions_introduced_no_new_fence():
    """Task 039's fence-conformance pin (#170) must not regress: all four
    additions above are plain prose/bold text, never a fenced code block,
    so the file's total fence count stays balanced (opened/closed pairs).
    `tests/test_prompt_fence_conformance.py` covers the contract-key scan
    itself; this is only a cheap same-file sanity check.
    """
    assert WORKER_MD.count("```") % 2 == 0
