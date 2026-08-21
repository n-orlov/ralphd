"""Task 048 (#22): `docs/prds/README.md`'s index table, re-read against code.

That table is the only place a wave's *outcome* is written down in the repo, and
it is pure prose: nothing reads it, every number in it was typed by hand from a
run's `status.json`, and each row outlives the run dir it was copied from. Four
ways it can lie, all checked here rather than by eye:

* a row points at a PRD that is not in the directory (or a recovered PRD sits
  there with no row), so the index and the directory disagree;
* a cost cell states money for a run whose recorded counters the shipped
  classifier calls *unknown* -- exactly the #14 defect this wave fixed. The
  v0.6 row's cell is not compared against a string typed twice: it is compared
  against what `engine.state.format_cost` renders for that run's own recorded
  usage payload (kept here as a fixture), so if the classification of an
  implausible zero quote ever changes, this test makes the row change with it;
* the token total drifts from those same counters, or a count cell still wears
  the `+` snapshot marker the row carried while the run was writing it. That
  run has finished; the cells now hold final figures read from a settled
  `status.json`, so the marker must be *absent* -- kept on, it would present a
  closed record as a floor forever;
* the outcome cell's task tally is not the plan's. A wave's row says how many
  of its planned tasks were completed, and that pair of numbers is the easiest
  thing in the table to mistype or to carry over from a neighbouring run's
  numbering -- the first draft of the v0.6 row said `58 of 59` for a 55-task
  plan. The tally is kept here as a fixture beside `V06_USAGE`, the row and the
  prose note are both compared against it, and the two numbers must add up with
  the count of tasks that never got there.

Each check is paired with a mutation of the real text into the wording it
replaced, so a check that stops discriminating fails.
"""

from __future__ import annotations

import re
from pathlib import Path

from ralphd.engine import state

REPO_ROOT = Path(__file__).resolve().parents[1]
PRD_DIR = REPO_ROOT / "docs" / "prds"
INDEX = PRD_DIR / "README.md"

# This run's own recorded usage rollup, copied verbatim from the
# `<run-dir>/status.json` the run left behind when it ended: an implausible zero
# quote (`costUSD: 0` -- and no `costPriced` marker at all, so the
# classification rests on the zero beside billable tokens) next to 332M billed
# tokens. The row's cost and token cells are derived from it below.
V06_RUN_ID = "selfdev-v06-release"
V06_USAGE = {
    "input": 10094,
    "output": 2732870,
    "cacheRead": 310853886,
    "cacheWrite": 18551959,
    "totalTokens": 332148809,
    "costUSD": 0,
    "costPriced": None,
}
# This run's own plan, counted from the `<run-dir>/tasks.json` it ended with
# (the loop's source of truth): 55 tasks, of which 54 reached `completed` and one
# (043d, the whole-SPEC rewrite) is `skipped` -- relabelled from `failed` by
# operator instruction so the run could leave its task loop and reach review,
# with its three consumed `validationAttempts` left in place as the record.
# `skipped` is not a claim that the task met its criteria; it did not.
V06_TASKS = {"total": 55, "completed": 54, "skipped": 1}

# The marker the row wore while the run that wrote it was still running: the
# counts were then a floor, because a run cannot count the iterations that
# document and verify it. The run has since finished and the cells hold its
# final figures, so this marker must now be ABSENT from both of them.
SNAPSHOT_MARKER = "+"

ROW = re.compile(r"^\|\s*`(?P<run>[^`]+)`\s*\|(?P<rest>.*)\|\s*$")
LINK = re.compile(r"^\[(?P<label>[^\]]+)\]\((?P<target>[^)]+)\)$")


def _rows(text: str) -> dict[str, list[str]]:
    """Run id -> the row's remaining cells, for every table row in `text`."""
    rows: dict[str, list[str]] = {}
    for line in text.splitlines():
        m = ROW.match(line)
        if m:
            cells = [c.strip() for c in m.group("rest").split("|")]
            rows[m.group("run")] = cells
    return rows


def _index_text() -> str:
    return INDEX.read_text()


def _prd_files() -> set[str]:
    return {p.name for p in PRD_DIR.glob("*.md") if p.name != "README.md"}


# --------------------------------------------------------------------------
# The index and the directory agree.
# --------------------------------------------------------------------------


def _link_problems(text: str) -> list[str]:
    problems: list[str] = []
    linked: list[str] = []
    for run, cells in _rows(text).items():
        m = LINK.match(cells[0])
        if not m:
            problems.append(f"{run}: row cell is not a markdown link: {cells[0]!r}")
            continue
        target = m.group("target")
        linked.append(target)
        if not (PRD_DIR / target).is_file():
            problems.append(f"{run}: links to {target}, which is not in docs/prds/")
        if m.group("label") != target:
            problems.append(f"{run}: link label {m.group('label')!r} is not the file")
    for extra in sorted(_prd_files() - set(linked)):
        problems.append(f"{extra} sits in docs/prds/ with no row in the index")
    for dupe in sorted({t for t in linked if linked.count(t) > 1}):
        problems.append(f"{dupe} has more than one row")
    return problems


def test_every_prd_has_exactly_one_row_and_every_row_a_file():
    assert _link_problems(_index_text()) == [], (
        "docs/prds/README.md's table disagrees with the directory it indexes"
    )


def test_a_row_for_a_missing_prd_is_caught():
    text = _index_text() + (
        "| `selfdev-ghost` | [selfdev-ghost.md](selfdev-ghost.md) | "
        "succeeded / verified | 1 | 1.0M | $1.00 |\n"
    )
    assert any("not in docs/prds/" in p for p in _link_problems(text))


def test_a_prd_with_no_row_is_caught():
    text = "\n".join(line for line in _index_text().splitlines()
                     if "v0.6-first-release.md)" not in line)
    assert any("no row in the index" in p for p in _link_problems(text))


# --------------------------------------------------------------------------
# This run's own row states what the shipped code says about this run.
# --------------------------------------------------------------------------


def _v06_cells(text: str) -> list[str]:
    rows = _rows(text)
    assert V06_RUN_ID in rows, f"docs/prds/README.md has no row for {V06_RUN_ID}"
    return rows[V06_RUN_ID]


def _cost_problems(text: str) -> list[str]:
    cell = _v06_cells(text)[4]
    expected = state.format_cost(V06_USAGE, decimals=4)
    problems = []
    if cell != expected:
        problems.append(f"cost cell {cell!r} is not format_cost's {expected!r}")
    if "$" in cell:
        problems.append(f"cost cell {cell!r} states money for an unknown cost")
    return problems


def test_the_v06_row_reports_the_cost_the_classifier_reports():
    # The premise: the recorded payload really is the implausible zero, so the
    # expected wording is earned rather than asserted into existence.
    assert state.cost_status(V06_USAGE) == "unknown"
    assert _cost_problems(_index_text()) == []


def test_a_row_pricing_the_implausible_zero_is_caught():
    cells = _v06_cells(_index_text())
    text = _index_text().replace(f"| {cells[4]} |", "| $0.00 |")
    assert _cost_problems(text) == [
        "cost cell '$0.00' is not format_cost's 'unavailable'",
        "cost cell '$0.00' states money for an unknown cost",
    ]


def _count_problems(text: str) -> list[str]:
    iterations, tokens = _v06_cells(text)[2:4]
    total = V06_USAGE["totalTokens"] / 1_000_000
    expected = f"{total:.1f}M"
    problems = []
    if tokens != expected:
        problems.append(f"token cell {tokens!r} is not the recorded {expected!r}")
    if not iterations.isdigit():
        problems.append(f"iteration cell {iterations!r} is not a final count")
    for name, cell in (("iteration", iterations), ("token", tokens)):
        if SNAPSHOT_MARKER in cell:
            problems.append(
                f"{name} cell {cell!r} still marks a finished run's count "
                f"as a snapshot floor")
    return problems


def test_the_v06_row_counts_match_the_recorded_usage():
    assert _count_problems(_index_text()) == []


def test_a_row_presenting_a_finished_runs_counts_as_a_snapshot_is_caught():
    # The wording this row shipped with while the run was still writing it.
    cells = _v06_cells(_index_text())
    marked = [f"{c}{SNAPSHOT_MARKER}" for c in cells[2:4]]
    text = _index_text().replace(
        f"| {cells[2]} | {cells[3]} |", f"| {marked[0]} | {marked[1]} |")
    assert _count_problems(text) == [
        f"token cell {marked[1]!r} is not the recorded {cells[3]!r}",
        f"iteration cell {marked[0]!r} is not a final count",
        (f"iteration cell {marked[0]!r} still marks a finished run's count "
         f"as a snapshot floor"),
        (f"token cell {marked[1]!r} still marks a finished run's count "
         f"as a snapshot floor"),
    ]


TALLY = re.compile(
    r"(?P<completed>\d+) of (?:the )?(?P<total>\d+) (?:planned )?tasks")
SKIPPED = re.compile(r"(?P<skipped>\d+) skipped")


def _tally_problems(text: str) -> list[str]:
    """Every place the v0.6 row states a task tally, against the plan itself."""
    problems: list[str] = []
    total = V06_TASKS["total"]
    completed = V06_TASKS["completed"]
    skipped = V06_TASKS["skipped"]
    if completed + skipped != total:
        problems.append(
            f"fixture is inconsistent: {completed} + {skipped} != {total}")

    outcome = _v06_cells(text)[1]
    m = TALLY.search(outcome)
    if not m:
        problems.append(f"outcome cell {outcome!r} states no task tally")
    elif (int(m.group("completed")), int(m.group("total"))) != (completed, total):
        problems.append(
            f"outcome cell says {m.group('completed')} of {m.group('total')} "
            f"tasks, the plan holds {completed} of {total}")
    s = SKIPPED.search(outcome)
    if not s:
        problems.append(f"outcome cell {outcome!r} hides the skipped task")
    elif int(s.group("skipped")) != skipped:
        problems.append(
            f"outcome cell counts {s.group('skipped')} skipped, the plan {skipped}")

    note = _v06_note(text)
    n = TALLY.search(note)
    if not n:
        problems.append("the v0.6 note states no task tally")
    elif (int(n.group("completed")), int(n.group("total"))) != (completed, total):
        problems.append(
            f"the note says {n.group('completed')} of {n.group('total')} tasks, "
            f"the plan holds {completed} of {total}")
    return problems


def test_the_v06_row_states_the_plans_own_task_tally():
    assert _tally_problems(_index_text()) == []


def test_a_row_carrying_another_runs_task_numbering_is_caught():
    # The wording this row shipped with first: a 59-task plan that never existed.
    text = _index_text().replace(
        f"{V06_TASKS['completed']} of {V06_TASKS['total']} tasks completed",
        "58 of 59 tasks completed")
    assert _tally_problems(text) == [
        "outcome cell says 58 of 59 tasks, the plan holds 54 of 55"]


def test_a_note_disagreeing_with_the_row_is_caught():
    text = _index_text().replace(
        f"{V06_TASKS['completed']} of the {V06_TASKS['total']} planned tasks",
        "58 of the 59 planned tasks")
    assert _tally_problems(text) == [
        "the note says 58 of 59 tasks, the plan holds 54 of 55"]


def test_a_row_hiding_the_skipped_task_is_caught():
    cells = _v06_cells(_index_text())
    text = _index_text().replace(
        f"| {cells[1]} |", f"| succeeded / all {V06_TASKS['total']} tasks verified |")
    assert _tally_problems(text) == [
        "outcome cell 'succeeded / all 55 tasks verified' states no task tally",
        "outcome cell 'succeeded / all 55 tasks verified' hides the skipped task",
    ]


def _v06_note(text: str) -> str:
    para = text[text.index("v0.6-first-release.md`'s row"):]
    return para[:para.index("\n- ") if "\n- " in para else len(para)]


def test_the_note_records_what_the_finished_run_actually_did():
    lowered = " ".join(_v06_note(_index_text()).lower().split())
    for phrase in (
        # how it ended, and on which approach -- it never replanned
        "succeeded", "verified", "approach 1",
        # the 145/155 gap is explained, not left as an apparent contradiction
        "refunded",
        # 043d: the status, the attempts it burned, and the plain admission
        "skipped", "three validation attempts",
        "claim that it met its criteria", "it did not",
        # ...beside where its scope actually landed
        "b923af2", "168a041", "cc8c8a2",
        "closed on github",
    ):
        assert phrase in lowered, f"the v0.6 note no longer says {phrase!r}"
    # The derived figure must be marked an estimate, never as what was billed.
    assert "estimate" in lowered
    # The counts are final now: nothing may still call them a running snapshot.
    assert "still running" not in lowered
    # The token magnitude the note reasons about is the recorded counter's.
    billed = f"{V06_USAGE['totalTokens'] / 1_000_000:.0f}m billed tokens"
    assert billed in lowered, f"the v0.6 note no longer says {billed!r}"
