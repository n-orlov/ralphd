"""Task 014 (requirement E.7, #212): pin the decision to decline making
`LoopSupervisor._verify_task` aware of an open, unadjudicated petition filing,
recorded in `docs/petition-format.md`'s "Verify's own awareness of an open
filing" section and echoed in `_verify_task`'s own docstring.

No test module referenced this section at the moment it was added, so a
later edit that keeps the heading but guts the content, or a docstring that
drifts from it, would go unnoticed. Each test greps a specific claim, not
just the heading.
"""

from __future__ import annotations

import inspect
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
PETITION_FORMAT = REPO_ROOT / "docs" / "petition-format.md"

HEADING = "Verify's own awareness of an open filing (task 014, requirement E.7, #212)"


def _section(text: str, heading: str) -> str:
    pattern = re.compile(
        rf"^##\s+{re.escape(heading)}\s*$(.*?)(?=^##\s+|\Z)",
        re.MULTILINE | re.DOTALL,
    )
    match = pattern.search(text)
    assert match is not None, f"no '## {heading}' section found"
    return match.group(1)


def _petition_format_text() -> str:
    return PETITION_FORMAT.read_text(encoding="utf-8")


def _verify_task_doc() -> str:
    from ralphd.engine.loop import LoopSupervisor
    doc = inspect.getdoc(LoopSupervisor._verify_task)
    assert doc is not None
    return doc


def test_decision_section_exists():
    section = _section(_petition_format_text(), HEADING)
    assert section.strip()


def test_decision_is_named_decline():
    section = _section(_petition_format_text(), HEADING)
    assert "**Decision: decline.**" in section


def test_decision_cites_212_measured_cost():
    section = _section(_petition_format_text(), HEADING)
    assert "four re-rejections" in section
    assert "48-hour run" in section
    assert "unverified" in section


def test_decision_names_the_ordering_guarantee():
    section = _section(_petition_format_text(), HEADING)
    assert "_maybe_run_midrun_adjudication_episode" in section
    assert "strictly" in section and "BEFORE" in section
    assert "test_midrun_adjudication_episode.py" in section


def test_verify_task_docstring_echoes_the_decision():
    doc = _verify_task_doc()
    assert "requirement E.7, #212" in doc
    assert "deliberately carries" in doc
    assert "_maybe_run_midrun_adjudication_episode" in doc


def test_verify_task_docstring_cites_the_measured_cost():
    doc = _verify_task_doc()
    assert "four re-rejections" in doc
    assert "48-hour run ending unverified" in doc
