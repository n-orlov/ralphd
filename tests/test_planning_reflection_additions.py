"""Task 040 (#170, #175, PRD requirement I.2): land the v0.19.1
reflection's `suggestions.diff` (#175) additions to `planning.md` --
step 2c (read the approach ordinal from Job context) and step 3a
(self-lint the draft against the three refusal shapes) -- with a pin that
the practice-count sentence agrees with the numbered list beneath it, so a
tenth practice added later without updating the count sentence fails here
rather than being read as nine forever.

Task 019 (PRD requirement G, #169) also pins that the very "Job context"
section step 2c points the planner at (`Approach N of M`) now also carries
the `Deadline` and `Remaining wall clock` lines, for the planning phase
like every other hat.
"""

from __future__ import annotations

import re
import tempfile
from pathlib import Path

from tests.conftest import supervisor_factory

REPO = Path(__file__).resolve().parents[1]
PLANNING_MD = (REPO / "src" / "ralphd" / "prompts" / "planning.md").read_text()

_supervisor = supervisor_factory(run_id="unit", run_root="run", workspace="workspace")

_NUMBER_WORDS = {
    1: "one", 2: "two", 3: "three", 4: "four", 5: "five", 6: "six",
    7: "seven", 8: "eight", 9: "nine", 10: "ten", 11: "eleven",
    12: "twelve",
}


def _practices_section() -> str:
    start = PLANNING_MD.index("## Plan-building practices")
    end = PLANNING_MD.index("## Rules", start)
    return PLANNING_MD[start:end]


def test_the_practice_count_sentence_agrees_with_the_numbered_list():
    """The intro sentence says "Follow all N below"; N must equal the count
    of top-level numbered items (`1.` .. `k.`) in the section, so the two
    can never silently drift apart the way the criteria this task fixes
    already had (eight practices existed, four other additions were owed)."""
    section = _practices_section()
    match = re.search(r"Follow all (\w+) below", section)
    assert match, "practice-count sentence not found"
    word = match.group(1)
    numbers_by_word = {v: k for k, v in _NUMBER_WORDS.items()}
    assert word in numbers_by_word, f"unrecognised count word {word!r}"
    claimed = numbers_by_word[word]

    item_numbers = [int(n) for n in re.findall(r"(?m)^(\d+)\.\s", section)]
    assert item_numbers == list(range(1, len(item_numbers) + 1)), (
        f"numbered practice items are not a clean 1..N sequence: {item_numbers}"
    )
    assert claimed == len(item_numbers), (
        f"the sentence claims {claimed} practices but {len(item_numbers)} "
        f"numbered items exist: {item_numbers}"
    )


def test_step_2c_reads_the_approach_ordinal_from_job_context():
    assert "2c." in PLANNING_MD
    idx = PLANNING_MD.index("2c.")
    step = " ".join(PLANNING_MD[idx:idx + 900].split())
    assert "approach ordinal" in step
    assert "Job context" in step
    assert "Approach N of M" in step
    # PRD fact 5's own two prohibitions, carried into the planner's copy.
    assert "task-id prefix" in step
    assert "git history" in step


def test_step_3a_self_lints_against_the_three_refusal_shapes():
    assert "3a." in PLANNING_MD
    idx = PLANNING_MD.index("3a.")
    step = PLANNING_MD[idx:idx + 900]
    assert "self-lint" in step.lower()
    assert "estimatedIterations" in step
    assert "baseline" in step
    assert "terminal" in step
    # exactly three enumerated shapes, marked (i)/(ii)/(iii)
    for marker in ("(i)", "(ii)", "(iii)"):
        assert marker in step, f"missing refusal-shape marker {marker}"


def test_step_ordering_is_2b_then_2c_then_3_then_3a_then_4():
    positions = {
        label: PLANNING_MD.index(label)
        for label in ("2b.", "2c.", "\n3. Draft your plan", "3a.", "4. Report")
    }
    ordered = sorted(positions, key=positions.get)
    assert ordered == ["2b.", "2c.", "\n3. Draft your plan", "3a.", "4. Report"]


def test_planning_md_still_carries_exactly_one_fence():
    """Neither addition introduced a new fenced code block -- the one
    surviving fence stays the plan-proposal.json file schema, per
    `tests/test_estimated_iterations.py`'s own pin."""
    fences = re.findall(r"```[^\n]*\n(.*?)```", PLANNING_MD, re.DOTALL)
    assert len(fences) == 1
    assert '"estimatedIterations"' not in fences[0]


def test_the_job_context_section_2c_points_at_also_carries_the_deadline():
    """Task 019 (PRD requirement G, #169): step 2c sends the planner to the
    'Job context' section below for `Approach N of M`; that same rendered
    section now also carries `Deadline` and `Remaining wall clock`, for the
    planning phase like every other hat."""
    with tempfile.TemporaryDirectory() as td:
        sup = _supervisor(Path(td))
        prompt = sup.build_prompt("planning")
        assert "## Job context" in prompt
        assert "- Deadline: " in prompt
        assert "- Remaining wall clock: " in prompt
