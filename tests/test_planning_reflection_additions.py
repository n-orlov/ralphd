"""Task 040 (#170, #175, PRD requirement I.2): land the v0.19.1
reflection's `suggestions.diff` (#175) additions to `planning.md` --
practice 9 (the tail is a freeze; the plan names the last code-touching
task), step 2c (read the approach ordinal from Job context) and step 3a
(self-lint the draft against the three refusal shapes) -- with a pin that
the practice-count sentence agrees with the numbered list beneath it, so a
tenth practice added later without updating the count sentence fails here
rather than being read as nine forever.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
PLANNING_MD = (REPO / "src" / "ralphd" / "prompts" / "planning.md").read_text()

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


def test_practice_nine_names_the_freeze_and_the_last_code_touching_task():
    section = _practices_section()
    assert re.search(r"9\.\s+\*\*The tail is a FREEZE", section), (
        "practice 9 (the freeze) is missing or not the ninth numbered item"
    )
    assert "last code-touching task" in section
    assert "docs/roadmap.md" in section and "docs/prds/README.md" in section


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
