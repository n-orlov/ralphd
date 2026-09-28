"""Task 006: SPEC.md and the docs describe SHIPPED behaviour in the present
tense, with no task or release chronology ("Before task N did X", "now
schedules Y") -- that framing belongs in a commit message or a task
description, never in the spec/docs, which describe only what the system
does today.

A regex-only guard: it does not understand English, it only refuses the
handful of phrasings this wave has actually used to smuggle chronology into
tracked docs. A future doc author finding a new phrasing that means the same
thing should extend the pattern here, not work around it.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

DOC_FILES = [
    REPO_ROOT / "SPEC.md",
    REPO_ROOT / "docs" / "cli.md",
    REPO_ROOT / "docs" / "api.md",
    REPO_ROOT / "docs" / "architecture.md",
]

RELEASE_NARRATIVE_RE = re.compile(
    r"Before task [0-9]+|Before this( task)?[ ,]|now schedules|now records"
)


def test_docs_have_no_release_or_task_chronology():
    offenders = []
    for path in DOC_FILES:
        text = path.read_text(encoding="utf-8")
        for lineno, line in enumerate(text.splitlines(), start=1):
            if RELEASE_NARRATIVE_RE.search(line):
                offenders.append(f"{path.relative_to(REPO_ROOT)}:{lineno}: {line.strip()}")
    assert not offenders, (
        "SPEC.md/docs describe shipped behaviour in the present tense with no "
        "task/release chronology; rewrite these lines to state the current "
        "fact only, dropping the before/after framing:\n" + "\n".join(offenders)
    )
