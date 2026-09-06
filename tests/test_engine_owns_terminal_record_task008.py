"""Task 008 (requirement B.2, #140): review.md and the review contract
context (`LoopSupervisor._terminal_record_review_context`, joined at the
`_review_extra_context` seam every other review-prompt source uses) both
state that petition/adjudication accounting, steer and ruling history,
approach history, terminal tallies and notification accounting are the
engine's record, that a closing report is never faulted for omitting or
predating any of them, and that a closing report's obligations are exactly:
what shipped per requirement, its evidence (commit, test node ids), and the
residuals it knows of, at the code sha it names.

This module pins the PROMPT TEXT itself (both the static `review.md` file
and the dynamic per-run context the engine always appends to a review
prompt) -- the same lexical-pin discipline
`tests/test_review_findings_prompt_shape.py` (task 004) and
`tests/test_materiality_rubric.py` (task 003) already use for their own
review-prompt hand-offs. `run_record.py`'s own docstring already carries
the engine-side half of this guarantee (task 006/007, requirement B.1);
this task's job is telling REVIEW the same thing, in both places review
actually reads it from.
"""

from __future__ import annotations

import re
from pathlib import Path

from ralphd.engine.config import JobConfig
from ralphd.engine.loop import LoopSupervisor
from ralphd.engine.state import RunDir
from tests.conftest import supervisor_factory

REPO_ROOT = Path(__file__).resolve().parents[1]
REVIEW_MD = (REPO_ROOT / "src" / "ralphd" / "prompts" / "review.md").read_text()


def _flatten(text: str) -> str:
    """Collapse markdown line-wrapping whitespace to single spaces so a
    sentence pinned here matches regardless of where prose happens to
    wrap in the source file."""
    return re.sub(r"\s+", " ", text.lower())


ENGINE_RECORD_SENTENCE = (
    "petition/adjudication accounting, steer and ruling history, "
    "approach history, terminal tallies and notification accounting are "
    "the engine's record"
)
NEVER_FAULTED_SENTENCE = (
    "a closing report is never faulted for omitting or predating them"
)
OBLIGATIONS_SENTENCE = (
    "a closing report's obligations are exactly: what shipped per "
    "requirement, its evidence (commit, test node ids), and the residuals "
    "it knows of, at the code sha it names"
)


# The shared harness (tests/conftest.py, G.1); `status` is the resumed-run
# shape: the state is on disk before the supervisor exists.
_supervisor = supervisor_factory(run_id="task008-terminal-record",
                                status={"state": "starting"})



def test_review_md_names_the_engine_owned_history_categories():
    assert ENGINE_RECORD_SENTENCE in _flatten(REVIEW_MD)


def test_review_md_states_a_closing_report_is_never_faulted_for_these():
    assert NEVER_FAULTED_SENTENCE in _flatten(REVIEW_MD)


def test_review_md_states_the_closing_reports_exact_obligations():
    assert OBLIGATIONS_SENTENCE in _flatten(REVIEW_MD)


def test_review_md_names_commit_and_test_node_ids_as_evidence():
    assert "commit, test node ids" in _flatten(REVIEW_MD)


# ---------------------------------------------------------------------------
# The review contract context: `LoopSupervisor._terminal_record_review_
# context`, always present (unconditional, unlike the petition/operator-
# ruling contexts which return '' in the common case), and joined into
# `_review_extra_context` so every real review prompt carries it.
# ---------------------------------------------------------------------------

def test_terminal_record_review_context_names_the_engine_owned_categories(
        tmp_path):
    sup = _supervisor(tmp_path)
    context = sup._terminal_record_review_context()
    assert ENGINE_RECORD_SENTENCE in _flatten(context)


def test_terminal_record_review_context_states_never_faulted(tmp_path):
    sup = _supervisor(tmp_path)
    context = sup._terminal_record_review_context()
    assert NEVER_FAULTED_SENTENCE in _flatten(context)


def test_terminal_record_review_context_states_exact_obligations(tmp_path):
    sup = _supervisor(tmp_path)
    context = sup._terminal_record_review_context()
    assert OBLIGATIONS_SENTENCE in _flatten(context)


def test_terminal_record_review_context_is_unconditional(tmp_path):
    """Unlike the petitions/operator-ruling contexts, this one carries no
    'common case: empty' -- it must be present on a bare run with no
    petitions, no ruling and no steering at all."""
    sup = _supervisor(tmp_path)
    context = sup._terminal_record_review_context()
    assert context.strip()


def test_review_extra_context_always_includes_the_terminal_record_context(
        tmp_path):
    sup = _supervisor(tmp_path)
    extra = sup._review_extra_context()
    assert ENGINE_RECORD_SENTENCE in _flatten(extra)
    assert NEVER_FAULTED_SENTENCE in _flatten(extra)
    assert OBLIGATIONS_SENTENCE in _flatten(extra)
