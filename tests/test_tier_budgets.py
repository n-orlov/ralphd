"""Task 004 (requirement G.8): the `smoke` and `fast` tiers have declared
budgets, and a tier that outgrows its budget fails here.

Two halves, because the two facts have very different costs to check:

* **Counts (live).** The tier membership is derived at collection time
  (tests/conftest.py stamps `smoke`/`fast` onto every collected item), so this
  module can ask pytest itself, in a `--collect-only` child, how many tests and
  how many modules each tier currently holds -- about a second and a half per
  tier -- and compare that against the ratcheting ceiling `TIER_CEILINGS`
  declares, in this module, as a code constant (v0.21 task 062, requirement
  N.4: "the ratcheting ceilings ... are code, they cannot go stale silently").
  Add enough tests to push a tier past its ceiling and this goes red, which is
  the ratchet requirement G.8 asks for.
* **Wall clock (recorded).** Measuring a tier's wall clock means *running* the
  tier, which no per-task records test can afford (the fast tier is minutes).
  So the wall clock is a recorded measurement in `docs/testing.md`'s budget
  table, and what is checked here is that every recorded number sits inside the
  budget declared beside it, and that a number which misses the PRD's target is
  disclosed as missed *with* that measured number rather than quietly rounded
  into compliance.

The wall-clock half still reads the doc, since that half checks a *recorded*
measurement against a *recorded* budget -- both numbers already live nowhere
else. The live count/module ratchet no longer does: its ceiling is a code
constant below, not a doc cell, so it cannot drift when `docs/testing.md`'s
count rows are edited or deleted (v0.21 task 063).
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
TESTING_DOC = REPO_ROOT / "docs" / "testing.md"
PYPROJECT = REPO_ROOT / "pyproject.toml"
GUARD_SCRIPT = REPO_ROOT / "scripts" / "final_sha_guards.sh"
VERIFY_PROMPT = REPO_ROOT / "src" / "ralphd" / "prompts" / "task-verify.md"

# The PRD's own G.8 targets (docs/prds/v0.19-a-note-is-not-a-rejection.md,
# requirement G.8): the contract this suite is measured against. Wall-clock
# targets are per lane and live in the table's own "PRD target" column; the
# tier-wide test/module counts used to be checked here too, by
# `test_the_tier_wide_count_targets_are_met_or_disclosed`, which v0.21 task
# 043 retired -- see that retirement's note below.

TIER_MARKERS = ("smoke", "fast")

# The ratcheting ceilings for the live collected-tier check below (v0.21 task
# 062, requirement N.4: "the ratcheting ceilings ... stay -- they are code,
# they cannot go stale silently"). Moved out of docs/testing.md's budget
# table's "budget tests"/"budget modules" columns, which this module no
# longer reads for this purpose. Raise a value only with the measurement
# that earned it named in the commit message that raises it; task 006
# (PRD F) raised `fast`'s test ceiling 5800 -> 5801: three new regression
# tests in `tests/test_log_render_bounded_tail.py` (a bounded-tail/
# live-tail/dead-run cut mid-iteration must carry `text_seen`/
# `thinking_seen`/`toolcall_seen` collapse state across the restart) push
# the fast tier's live collected count to 5801 (`pytest -m fast
# --collect-only`). `modules`/`smoke` are unchanged (390 fast modules /
# 270 smoke modules, 4300 smoke tests).
#
# Task 007 (PRD G) raised `fast`'s test ceiling 5801 -> 5803: two new
# regression tests in `tests/test_hub_log_tail_cache.py` (the
# append-and-revalidation scenario, and the freshly-issued-validator
# revalidates-without-rerendering scenario) push the fast tier's live
# collected count to 5803 (`pytest -m fast --collect-only`).
# `modules`/`smoke` are unchanged.
TIER_CEILINGS: dict[str, dict[str, int]] = {
    "fast": {"tests": 5803, "modules": 390},
    "smoke": {"tests": 4300, "modules": 270},
}

# The budget table: one row per *lane* (a tier plus the recipe that runs it).
# No `tests`/`modules`/`budget tests`/`budget modules` columns -- those moved
# out of this doc entirely (v0.21 task 062/063, requirement N.4); this table
# now carries only the wall-clock half, which is a recorded measurement
# against a recorded budget, not a live collected count.
BUDGET_COLUMNS = ("lane", "marker", "recipe", "wall clock",
                  "budget wall clock", "PRD target", "target met?")

_NUMBER_RE = re.compile(r"\d+(?:\.\d+)?")
# A cell that deliberately states "no target of this kind for this lane".
_NO_TARGET = {"-", "--", "\u2014", "n/a", "none"}


def _cells(line: str) -> list[str]:
    return [c.strip() for c in line.strip().strip("|").split("|")]


def _is_separator(cells: list[str]) -> bool:
    return all(set(c) <= set("-: ") and c for c in cells)


def parse_budget_table(text: str) -> list[dict[str, str]]:
    """Every row of the budget table, as `column name -> raw cell text`.

    The table is found by its header, not by position, so prose may move
    around it freely.
    """
    rows: list[dict[str, str]] = []
    lines = text.splitlines()
    for index, line in enumerate(lines):
        if not line.lstrip().startswith("|"):
            continue
        header = _cells(line)
        if [c.lower() for c in header] != [c.lower() for c in BUDGET_COLUMNS]:
            continue
        for following in lines[index + 1:]:
            if not following.lstrip().startswith("|"):
                break
            cells = _cells(following)
            if _is_separator(cells):
                continue
            if len(cells) != len(BUDGET_COLUMNS):
                pytest.fail(f"budget table row has {len(cells)} cells, "
                            f"expected {len(BUDGET_COLUMNS)}: {following!r}")
            rows.append(dict(zip(BUDGET_COLUMNS, cells)))
        break
    return rows


def _number(cell: str) -> float | None:
    match = _NUMBER_RE.search(cell.replace(",", ""))
    return float(match.group(0)) if match else None


def budget_violations(text: str) -> list[str]:
    """Every lane whose recorded measurement exceeds its declared budget.

    The whole discriminator of this module, kept as a plain function over the
    doc's text so a mutated table can be checked without touching the file.
    """
    problems: list[str] = []
    for row in parse_budget_table(text):
        lane = row["lane"]
        for measured_col, budget_col in (("wall clock", "budget wall clock"),):
            measured, budget = _number(row[measured_col]), _number(row[budget_col])
            if measured is None or budget is None:
                problems.append(f"{lane}: {measured_col}/{budget_col} carries no "
                                f"number ({row[measured_col]!r} / {row[budget_col]!r})")
                continue
            if measured > budget:
                problems.append(
                    f"{lane}: measured {measured_col} {row[measured_col]!r} exceeds "
                    f"the declared budget {row[budget_col]!r}")
    return problems


def _collect(marker: str) -> list[str]:
    """The node ids `-m <marker>` selects right now, from pytest itself."""
    env = {k: v for k, v in os.environ.items() if k != "RALPHD_NOTIFIER_URL"}
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "--collect-only", "-m", marker],
        cwd=REPO_ROOT, capture_output=True, text=True, env=env, timeout=600)
    assert proc.returncode == 0, (
        f"collect-only for -m {marker} failed ({proc.returncode}):\n"
        f"{proc.stdout[-3000:]}\n{proc.stderr[-2000:]}")
    return [line.strip() for line in proc.stdout.splitlines() if "::" in line]


_COLLECTED: dict[str, list[str]] = {}


def collected(marker: str) -> list[str]:
    if marker not in _COLLECTED:
        _COLLECTED[marker] = _collect(marker)
    return _COLLECTED[marker]


def _modules(node_ids: list[str]) -> set[str]:
    return {node.split("::", 1)[0] for node in node_ids}


# --------------------------------------------------------------------------
# the tiers exist and are wired where the requirement says
# --------------------------------------------------------------------------

def test_both_tier_markers_are_registered_in_pyproject() -> None:
    text = PYPROJECT.read_text()
    for marker in TIER_MARKERS:
        assert re.search(rf'^\s*"{marker}: ', text, re.MULTILINE), (
            f"pyproject.toml does not register the `{marker}` marker; an "
            "unregistered marker is a warning pytest can be configured to "
            "ignore, not a declared tier")


def test_the_recorded_tier_doc_exists_and_names_both_recipes() -> None:
    text = TESTING_DOC.read_text()
    for marker in TIER_MARKERS:
        assert f"-m {marker}" in text, (
            f"docs/testing.md never shows a recipe selecting `-m {marker}`")


def test_the_per_task_run_selects_the_smoke_tier() -> None:
    assert re.search(r"-m\s+'?smoke'?", GUARD_SCRIPT.read_text()), (
        "scripts/final_sha_guards.sh does not select the smoke tier")
    assert re.search(r"-m\s+smoke", VERIFY_PROMPT.read_text()), (
        "the verify prompt does not select the smoke tier for its per-task run")


# --------------------------------------------------------------------------
# the budget guard
# --------------------------------------------------------------------------

def test_the_budget_table_covers_both_tiers() -> None:
    rows = parse_budget_table(TESTING_DOC.read_text())
    assert rows, ("docs/testing.md has no budget table with the columns "
                  f"{BUDGET_COLUMNS}")
    markers = {r["marker"].strip("`") for r in rows}
    assert set(TIER_MARKERS) <= markers, (
        f"the budget table covers {sorted(markers)}, not both tiers")


def test_no_lane_exceeds_its_declared_budget() -> None:
    problems = budget_violations(TESTING_DOC.read_text())
    assert not problems, (
        "docs/testing.md records a tier over its own declared budget:\n"
        + "\n".join(problems))


def test_the_budget_guard_discriminates() -> None:
    """The mutation this guard exists to catch: a measurement pushed past the
    budget beside it. Applied to a copy of the real table, so the check is
    proven on the real column layout rather than on a hand-built fixture."""
    text = TESTING_DOC.read_text()
    assert not budget_violations(text)
    rows = parse_budget_table(text)
    row = rows[0]
    blown = text.replace(
        f"| {row['recipe']} | {row['wall clock']} |",
        f"| {row['recipe']} | 99999 s |", 1)
    assert blown != text, "could not mutate the first budget row"
    problems = budget_violations(blown)
    assert any("wall clock" in p for p in problems), (
        f"a 99999 s wall clock inside a smaller budget went unreported: {problems}")


def test_a_missed_prd_target_is_disclosed_with_its_measured_number() -> None:
    """A target the suite does not meet is stated as missed, with the number.

    The PRD's rule for requirement G: "a missed target is disclosed with its
    number, not hidden".
    """
    text = TESTING_DOC.read_text()
    for row in parse_budget_table(text):
        target = _number(row["PRD target"])
        if target is None:
            assert row["PRD target"].lower() in _NO_TARGET, (
                f"{row['lane']}: PRD target cell {row['PRD target']!r} carries "
                "neither a number nor an explicit 'no target' marker")
            continue
        measured = _number(row["wall clock"])
        verdict = row["target met?"]
        if measured is not None and measured > target:
            assert "MISSED" in verdict.upper(), (
                f"{row['lane']}: measured {row['wall clock']} against a "
                f"{row['PRD target']} target, but the verdict cell says "
                f"{verdict!r}")
            assert _NUMBER_RE.search(verdict), (
                f"{row['lane']}: the miss is declared without its measured "
                f"number: {verdict!r}")
        else:
            assert "MISSED" not in verdict.upper(), (
                f"{row['lane']}: measured {row['wall clock']} is inside its "
                f"{row['PRD target']} target, but the verdict says {verdict!r}")


# `test_the_tier_wide_count_targets_are_met_or_disclosed` stood here until
# v0.21 task 043 retired it as the cure for the fast lane's own red at
# `b0361e1` (gate run 34885356133: "the fast tier collects 5756 tests against
# the PRD's target of 3000, and docs/testing.md never states that measured
# number"). It was satisfiable ONLY by writing this wave's own measured test
# count into `docs/testing.md` as prose -- which v0.21's PRD prohibits ("No
# committed record whose subject is this wave's own work ... not a count
# disclosure") and requirement N.4 deletes outright, with N.9 failing a test
# if such a row comes back. So every test-adding task in this wave would
# otherwise have to break one rule to satisfy the other; the same node forced
# a docs-only cure commit in v0.19.1 and a petition in v0.20.
#
# Surviving cover for what it actually bounded (the collected counts):
#   - `test_the_collected_tier_is_inside_its_declared_budget[fast]` and
#     `[smoke]` below still refuse a tier that grows past the ratcheting
#     ceilings, now `TIER_CEILINGS` above (5800/4300 tests, 390/270 modules;
#     moved out of the doc's "budget tests"/"budget modules" columns by
#     v0.21 task 062, requirement N.4).
#   - `test_a_missed_prd_target_is_disclosed_with_its_measured_number` above
#     still requires a missed PRD target to be stated with its number for
#     every row of the budget table.


# --------------------------------------------------------------------------
# the live half: what the tiers actually collect
# --------------------------------------------------------------------------

@pytest.mark.parametrize("marker", TIER_MARKERS)
def test_the_collected_tier_is_inside_its_declared_budget(marker: str) -> None:
    node_ids = collected(marker)
    assert node_ids, f"-m {marker} selects nothing at all"
    ceiling = TIER_CEILINGS[marker]
    test_budget = ceiling["tests"]
    module_budget = ceiling["modules"]
    assert len(node_ids) <= test_budget, (
        f"the {marker} tier collects {len(node_ids)} tests, over the "
        f"{test_budget} ceiling in TIER_CEILINGS")
    modules = _modules(node_ids)
    assert len(modules) <= module_budget, (
        f"the {marker} tier collects {len(modules)} modules, over the "
        f"{module_budget} ceiling in TIER_CEILINGS")


def test_the_smoke_tier_is_a_subset_of_the_fast_tier() -> None:
    """The tier table's own definition: fast is "smoke + ...", so a smoke test
    the fast tier does not run would make the final-sha lane weaker than the
    per-task one."""
    extra = sorted(set(collected("smoke")) - set(collected("fast")))
    assert not extra, (
        f"{len(extra)} smoke tests are outside the fast tier, e.g. {extra[:5]}")


def test_the_smoke_tier_leaves_out_the_process_boundary_modules() -> None:
    """What makes smoke cheaper than fast: no module that spawns a real
    engine or `ralphctl` process is in it (a real engine, real sleep or
    subprocess spawn attributes ~75 % of the serial wall clock)."""
    smoke_modules = _modules(collected("smoke"))
    assert smoke_modules, "the smoke tier collects no modules"
    offenders = []
    for module in sorted(smoke_modules):
        source = (REPO_ROOT / module).read_text(errors="replace")
        if re.search(r"\bLiveRun\b|\bengine_factory\b|\blive\(|\bRALPHCTL\b", source):
            offenders.append(module)
    assert not offenders, (
        f"smoke-tier modules spawn real engine/CLI processes: {offenders}")
