"""Task 048 (#22): `docs/prds/README.md`'s index table, re-read against code.

That table is the only place a wave's *outcome* is written down in the repo, and
it is pure prose: nothing reads it, every number in it was typed by hand from a
run's `status.json`, and each row outlives the run dir it was copied from. Three
ways it can lie, all checked here rather than by eye:

* a row points at a PRD that is not in the directory (or a recovered PRD sits
  there with no row), so the index and the directory disagree;
* a cost cell states money for a run whose recorded counters the shipped
  classifier calls *unknown* -- exactly the #14 defect this wave fixed. The
  v0.6 row's cell is not compared against a string typed twice: it is compared
  against what `engine.state.format_cost` renders for that run's own recorded
  usage payload (kept here as a fixture), so if the classification of an
  implausible zero quote ever changes, this test makes the row change with it;
* the token total drifts from those same counters, or the row silently drops
  the marker that says the numbers are a snapshot the run took of itself while
  still running (a run cannot count the iterations that document it);
* the outcome cell's task tally is not the plan's. A wave's row says how many
  of its planned tasks were verified, and that pair of numbers is the easiest
  thing in the table to mistype or to carry over from a neighbouring run's
  numbering -- the first draft of the v0.6 row said `58 of 59` for a 55-task
  plan. The tally is kept here as a fixture beside `V06_USAGE`, the row and the
  prose note are both compared against it, and the two numbers must add up with
  the failure count.

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

# This run's own recorded usage rollup, copied verbatim from
# `<run-dir>/status.json` at the iteration that wrote the v0.6 row: an
# implausible zero quote (`costUSD: 0` with `costPriced: true`) beside 310M
# billed tokens. The row's cost and token cells are derived from it below.
V06_RUN_ID = "selfdev-v06-release"
V06_USAGE = {
    "input": 9536,
    "output": 2603118,
    "cacheRead": 289908196,
    "cacheWrite": 17637400,
    "totalTokens": 310158250,
    "costUSD": 0,
    "costPriced": True,
}
# This run's own plan, counted from `<run-dir>/tasks.json` (the loop's source of
# truth) at the iteration that wrote the v0.6 row: 55 tasks, of which 54 reached
# `completed` and one (043d, the whole-SPEC rewrite) is terminally `failed`.
# Unlike the iteration and token cells this is NOT a floor -- no task is added
# after the plan is finished -- so the tally carries no snapshot marker.
V06_TASKS = {"total": 55, "completed": 54, "failed": 1}

# The numbers are a floor: the run was still running when it wrote its own row.
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
    expected = f"{total:.1f}M{SNAPSHOT_MARKER}"
    problems = []
    if tokens != expected:
        problems.append(f"token cell {tokens!r} is not the recorded {expected!r}")
    if not iterations.rstrip(SNAPSHOT_MARKER).isdigit():
        problems.append(f"iteration cell {iterations!r} is not a count")
    if not iterations.endswith(SNAPSHOT_MARKER):
        problems.append(f"iteration cell {iterations!r} drops the snapshot marker")
    return problems


def test_the_v06_row_counts_match_the_recorded_usage():
    assert _count_problems(_index_text()) == []


def test_a_row_presenting_the_snapshot_as_final_is_caught():
    cells = _v06_cells(_index_text())
    text = _index_text().replace(
        f"| {cells[2]} | {cells[3]} |",
        f"| {cells[2].rstrip(SNAPSHOT_MARKER)} | {cells[3].rstrip(SNAPSHOT_MARKER)} |")
    problems = _count_problems(text)
    assert any("drops the snapshot marker" in p for p in problems)
    assert any("is not the recorded" in p for p in problems)


TALLY = re.compile(
    r"(?P<completed>\d+) of (?:the )?(?P<total>\d+) (?:planned )?tasks")
FAILED = re.compile(r"(?P<failed>\d+) failed")


def _tally_problems(text: str) -> list[str]:
    """Every place the v0.6 row states a task tally, against the plan itself."""
    problems: list[str] = []
    total = V06_TASKS["total"]
    completed = V06_TASKS["completed"]
    failed = V06_TASKS["failed"]
    if completed + failed != total:
        problems.append(
            f"fixture is inconsistent: {completed} + {failed} != {total}")

    outcome = _v06_cells(text)[1]
    m = TALLY.search(outcome)
    if not m:
        problems.append(f"outcome cell {outcome!r} states no task tally")
    elif (int(m.group("completed")), int(m.group("total"))) != (completed, total):
        problems.append(
            f"outcome cell says {m.group('completed')} of {m.group('total')} "
            f"tasks, the plan holds {completed} of {total}")
    f = FAILED.search(outcome)
    if not f:
        problems.append(f"outcome cell {outcome!r} hides the failed task")
    elif int(f.group("failed")) != failed:
        problems.append(
            f"outcome cell counts {f.group('failed')} failed, the plan {failed}")

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
        f"{V06_TASKS['completed']} of {V06_TASKS['total']} tasks verified",
        "58 of 59 tasks verified")
    assert _tally_problems(text) == [
        "outcome cell says 58 of 59 tasks, the plan holds 54 of 55"]


def test_a_note_disagreeing_with_the_row_is_caught():
    text = _index_text().replace(
        f"{V06_TASKS['completed']} of the {V06_TASKS['total']} planned tasks",
        "58 of the 59 planned tasks")
    assert _tally_problems(text) == [
        "the note says 58 of 59 tasks, the plan holds 54 of 55"]


def test_a_row_hiding_the_failed_task_is_caught():
    cells = _v06_cells(_index_text())
    text = _index_text().replace(
        f"| {cells[1]} |", f"| succeeded / all {V06_TASKS['total']} tasks verified |")
    assert _tally_problems(text) == [
        "outcome cell 'succeeded / all 55 tasks verified' states no task tally",
        "outcome cell 'succeeded / all 55 tasks verified' hides the failed task",
    ]


def _v06_note(text: str) -> str:
    para = text[text.index("v0.6-first-release.md`'s row"):]
    return para[:para.index("\n- ") if "\n- " in para else len(para)]


def test_the_note_explains_why_the_counts_are_a_floor():
    lowered = " ".join(_v06_note(_index_text()).lower().split())
    for phrase in ("snapshot", "cannot count the iterations",
                   "closed on github", "still running"):
        assert phrase in lowered, f"the v0.6 note no longer says {phrase!r}"
    # The derived estimate must be marked as one, never as what was billed.
    assert "estimate" in lowered
