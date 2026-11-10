"""PRD v0.37 requirement A.3: `planning.md` tells the planner when to use plan
phases and when not to, and the section reaches the rendered planning prompt.
"""

from __future__ import annotations

import re
import tempfile
from pathlib import Path

from tests.conftest import supervisor_factory

_supervisor = supervisor_factory(run_id="unit", run_root="run", workspace="workspace")


def _phases_section() -> str:
    with tempfile.TemporaryDirectory() as td:
        prompt = _supervisor(Path(td)).build_prompt("planning")
    assert "## Plan phases" in prompt, "the planning prompt has no phases section"
    start = prompt.index("## Plan phases")
    end = prompt.index("\n## ", start + 1)
    return " ".join(prompt[start:end].split())


def test_the_planning_prompt_carries_a_phases_section():
    section = _phases_section()
    # the shape the planner writes
    assert "`phases`" in section and "`phase`" in section
    for field in ("id", "title", "reviewFocus"):
        assert f'"{field}"' in section


def test_the_section_says_when_to_use_phases():
    section = _phases_section()
    use = section[section.index("Use phases when"):section.index("Do not use phases")]
    assert "PRD names phases or gates" in use
    assert "destroys or hides evidence" in use
    for example in ("tearing an environment down", "a merge that deletes a branch",
                    "a data migration"):
        assert example in use
    assert "cannot be undone" in use


def test_the_section_says_when_not_to_use_phases():
    section = _phases_section()
    avoid = section[section.index("Do not use phases"):]
    assert re.search(r"only to group work", avoid)
    assert "omit `phases`" in avoid
