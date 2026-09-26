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

# The ratcheting ceilings for the live collected-tier check below: a code
# constant, so it cannot go stale silently. Each value sits on the count
# `.venv/bin/python -m pytest -q -m <tier> --collect-only` measured when it
# last moved. Move a value only with that measurement named in the commit
# message that moves it -- never as history in this comment. A value may be
# lowered to the measured count freely; raising it needs the tests that
# earned the room named in the same message.
TIER_CEILINGS: dict[str, dict[str, int]] = {
    # Task 008: +18 tests / +1 module (tests/test_hub_parked_panel.py),
    # measured `.venv/bin/python -m pytest -q -m fast --collect-only` ->
    # 6232/428, `-m smoke` -> 4553/308.
    # Task 010: +4 tests / +3 modules (tests/test_hub_steer_roundtrip.py,
    # tests/test_hub_rule_roundtrip.py, tests/test_hub_steer_textarea_and_
    # rule_rows.py), measured the same way -> fast 6236/431, smoke 4555/309.
    # Task 010 retry: +1 test (test_hub_steer_textarea_and_rule_rows.py::
    # test_steer_apply_now_is_labelled_with_its_cost) -> fast 6237, smoke 4556.
    # Task 009: +18 tests / +1 module (tests/test_hub_parked_actions.py),
    # measured `.venv/bin/python -m pytest -q -m fast --collect-only` ->
    # 6255/432, `-m smoke` -> 4574/310.
    # Task 009 retry: +7 tests in tests/test_hub_parked_actions.py (byte-
    # for-byte submit body, blank-line-preserving waive compose, Enter in
    # #rule-text), measured the same way -> fast 6262, smoke 4581.
    # Task 012: +5 tests / +1 module (tests/test_render_index.py) plus the
    # ingestion-lint suite's own automatic +1 parametrization for the new
    # src/ralphd/render_index.py source file, measured
    # `.venv/bin/python -m pytest -q -m fast --collect-only` -> fast
    # 6268/433, `-m smoke` -> smoke 4587/311.
    # Task 013 retry: +2 tests in tests/test_hub_real_render_cap_whole_call.py
    # (lagging live index, streamed text/tool calls at every index lag) ->
    # fast 6270, smoke 4589.
    # Task 014: -1 test (tests/test_hub_real_render_cap_whole_call.py::
    # test_absent_index_residual_costs_measured_through_the_whole_client_
    # facing_call retired outright -- its own premise, a cheap-but-wrong
    # absent-index render, is cured; the rewritten tests/test_log_merge_
    # bounded_reads.py::test_silent_prelude_case_renders_the_full_
    # renderers_last_two_lines_on_both_real_paths covers the cure without
    # adding a node) -> fast 6269, smoke 4588.
    # Task 014 retry: +6 tests (tests/test_log_merge_bounded_reads.py::
    # test_cold_no_index_legacy_shapes_render_the_exact_full_tail_on_both_
    # hub_surfaces, 3 shapes x 2 hidden-setter variants), measured
    # `.venv/bin/python -m pytest -q -m fast --collect-only` -> fast 6275,
    # `-m smoke` -> smoke 4594.
    # Task 015: +3 tests / +1 module (tests/test_hub_render_index_cache.py:
    # run-dir-untouched control, the absent-index cache's own C.4 bound at
    # both silent-prelude depths, and the cache-key-changes-on-append
    # control), measured `.venv/bin/python -m pytest -q -m fast
    # --collect-only` -> fast 6278/434, `-m smoke` -> smoke 4597/312.
    # cure-01-01-2: +10 tests / +1 module (the new dev-layout supplemental-
    # run test module: the default no-argument invocation's primary +
    # supplemental pass, its explicit-tier/targeted/collect-only opt-outs,
    # either-run-nonzero, the SUPPLEMENTAL_TIER_MARKER/header pins, and the
    # fast+docker+browser+leftover collection partition), measured
    # `.venv/bin/python -m pytest -q -m fast --collect-only` -> fast
    # 6288/435, `-m smoke` -> smoke 4597/312 (unchanged: every new test
    # shells out to a subprocess, so none is `smoke`).
    # cure-01-02: +3 tests, no new module (tests/test_escalation_digest.py:
    # last_approach/incurable digests are evidence-free, repeated-signature
    # digest names the ruling command too), measured `.venv/bin/python -m
    # pytest -q -m fast --collect-only` -> fast 6292/435, `-m smoke` ->
    # smoke 4600/312 (every new node runs in-process, so all three count
    # in both tiers).
    # cure-01-02 retry: +7 tests, no new module (tests/test_escalation_
    # digest.py: posted digests' A.7 preview numbers agree with their
    # event, and the incurable digest is bounded), measured `-m fast
    # --collect-only` -> fast 6299/435, `-m smoke` -> smoke 4607/312.
    # cure-01-02 second retry: +3 tests, no new module (tests/test_
    # escalation_digest.py: the last-approach/incurable full escalation
    # text names each finding's cured_when), measured `-m fast
    # --collect-only` -> fast 6302/435, `-m smoke` -> smoke 4610/312.
    # cure-01-03: +6 tests, no new module (tests/test_hub_real_render_cap_
    # whole_call.py: same-size leading-checkpoint rewrite, missing/wrong-
    # typed checkpoint state x4, one append-only-lagging regression guard),
    # measured `-m fast --collect-only` -> fast 6308/435, `-m smoke` ->
    # smoke 4616/312.
    # cure-01-03 retry: +8 tests, no new module (same module: malformed
    # NESTED state in a later checkpoint x7, a passed-in index whose state
    # cannot hydrate), measured `-m fast --collect-only` -> fast 6316/435,
    # `-m smoke` -> smoke 4624/312.
    # cure-01-03 second retry: +13 tests, no new module (same module: a
    # same-size rewrite of a later covered line x4, missing/wrong-typed/
    # inconsistent checkpoint fields x7, a checksum-free version-1 index,
    # the bounded verification window on a large prefix), measured `-m fast
    # --collect-only` -> fast 6329/435, `-m smoke` -> smoke 4637/312.
    # cure-01-04 second retry: +1 test, no new module (tests/test_hub_real_
    # render_cap_whole_call.py: SPEC.md §11.4 and docs/cli.md describe the
    # indexed bounded tail read), measured `-m fast --collect-only` -> fast
    # 6333/435, `-m smoke` -> smoke 4641/312.
    # cure-01-05: +8 tests, no new module (tests/test_hub_parked_actions.py:
    # the panel's own name-field-preservation/no-request-toggle tests replace
    # the retired opposite pin plus 4 more; tests/test_hub_steer_textarea_
    # and_rule_rows.py: the apply-now plan-review-revision disclosure),
    # measured `-m fast --collect-only` -> fast 6341/435, `-m smoke` ->
    # smoke 4649/312.
    # cure-01-05 retry: +1 test, no new module (tests/test_hub_parked_
    # actions.py: the named multi-line parked submission), measured
    # `-m fast --collect-only` -> fast 6342/435, `-m smoke` -> smoke 4650/312.
    # Task cure-01-01-4 (petition remedy for cure-01-03 criterion (1)):
    # +6 tests in tests/test_hub_real_render_cap_whole_call.py (out-of-
    # window same-size rewrite/replacement, complete and lagging coverage,
    # preservation across a later append, safe append/index publication),
    # measured `.venv/bin/python -m pytest -q -m fast --collect-only` ->
    # 6348/435, `-m smoke` -> 4656/312.
    # Task cure-01-01-4 retry: +5 tests, no new module (tests/test_hub_real_
    # render_cap_whole_call.py: a non-leading out-of-window rewrite refused
    # across 20 later appends through both writer protocols, a refused
    # hub-cached index, and the two read-snapshot consistency cases),
    # measured -> fast 6353/435, smoke 4661/312.
    # cure-01-01-5: +5 tests, no new module (tests/test_hub_parked_actions.py:
    # the single-trailing-newline waive round trip across 3 prose shapes plus
    # an editable-prose-after-the-fix control; tests/test_hub_parked_panel.py:
    # findings live in the left column, the answer box and actions in the
    # right column), measured `.venv/bin/python -m pytest -q -m fast
    # --collect-only` -> fast 6358/435, `-m smoke --collect-only` -> smoke
    # 4666/312.
    # cure-01-02-2: +1 test, no new module (tests/test_hub_real_render_cap_
    # whole_call.py: a 200-line tail spanning two absent-index silent-
    # prelude iterations at depths 1,000/1,700 stays bounded on the
    # SECOND call), measured `.venv/bin/python -m pytest -q -m fast
    # --collect-only` -> fast 6359/435, `-m smoke --collect-only` -> smoke
    # 4667/312.
    "fast": {"tests": 6359, "modules": 435},
    "smoke": {"tests": 4667, "modules": 312},
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
    # The verify prompt is generic across projects, so it names no marker of
    # this repository's own: it selects the project's per-task tier from the
    # measured test commands the Standing rules block records.
    verify = " ".join(VERIFY_PROMPT.read_text().split())
    assert re.search(r"cheapest per-task tier -- the measured test commands "
                     r"the notes' `## Standing rules` block records", verify), (
        "the verify prompt does not select the project's per-task tier for "
        "its per-task run")


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
