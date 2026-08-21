"""The planner, worker and verifier prompts must budget test runs against the
iteration wall-clock cap (#38).

An iteration has a hard wall-clock cap with no grace period, and the suite of a
mature repo can eat most of one. Every wave that rediscovered this per iteration
paid for it twice: once in a killed iteration that lost its uncommitted work,
once in the verifier recording that kill as a validation failure. So:

  planning.md must measure the test surface ONCE (command, wall-clock runtime,
  tiers/markers and their cost, fastest targeted invocation), compare it with
  the iteration cap, record the numbers in the notes, and size tasks so the
  work AND its verification fit one iteration.

  worker.md and task-verify.md must each state: targeted modules while working,
  the whole suite at most once per iteration, background it rather than block on
  it, never chain two whole-suite runs, and that an iteration killed at the cap
  loses its uncommitted work.

Each rule gets its own test (parametrized per file for the two shell prompts),
so deleting any single rule from any of the three files fails a *named* test.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

PROMPTS_DIR = Path(__file__).resolve().parents[1] / "src" / "ralphd" / "prompts"

# prompts whose agent runs tests inside an iteration's wall-clock cap
SHELL_PROMPTS = ["worker", "task-verify"]


def _prompt_text(name: str) -> str:
    path = PROMPTS_DIR / f"{name}.md"
    assert path.is_file(), f"missing prompt file {path}"
    return path.read_text()


def _assert_all(name: str, rule: str, patterns: list[str]) -> None:
    text = _prompt_text(name)
    missing = [p for p in patterns
               if not re.search(p, text, re.IGNORECASE | re.DOTALL)]
    assert not missing, (
        f"{name}.md is missing the '{rule}' test-budget rule; "
        f"no match for: {missing}")


# ---- planning.md: measure once, write it down, size tasks to fit ------------

def test_planning_measures_the_test_surface_once():
    _assert_all("planning", "measure the test surface once", [
        r"Measure the test surface once",
        r"command that runs the suite",
        r"wall-clock runtime",
        r"tiers or markers\s*\n?\s*exist and what each costs",
        r"fastest targeted invocation",
    ])


def test_planning_writes_the_numbers_into_the_notes():
    _assert_all("planning", "write the numbers into the notes", [
        r"write the numbers into the notes",
        r"record what fraction of one iteration a full run costs",
    ])


def test_planning_compares_the_suite_runtime_with_the_iteration_cap():
    _assert_all("planning", "compare against the iteration cap", [
        r"Compare that runtime against the iteration wall-clock cap",
    ])


def test_planning_sizes_tasks_so_work_plus_verification_fit_one_iteration():
    _assert_all("planning", "size tasks to one iteration", [
        r"Size each task so the work AND its verification fit one iteration's\s*\n?\s*wall-clock\s*\n?\s*cap",
        r"whole-suite run in the same iteration is already too",
        r"full sweep its own late task",
    ])


# ---- worker.md and task-verify.md: the five in-iteration rules --------------

@pytest.mark.parametrize("name", SHELL_PROMPTS)
def test_prompt_budgets_test_runs_against_the_iteration_clock(name):
    _assert_all(name, "budget rule exists", [
        r"Budget your test runs against the iteration clock",
    ])


@pytest.mark.parametrize("name", SHELL_PROMPTS)
def test_prompt_runs_targeted_modules_while_working(name):
    _assert_all(name, "targeted modules while working", [
        r"run only the targeted test modules that cover",
        r"seconds, not minutes",
    ])


@pytest.mark.parametrize("name", SHELL_PROMPTS)
def test_prompt_runs_the_whole_suite_at_most_once_per_iteration(name):
    _assert_all(name, "whole suite at most once per iteration", [
        r"whole suite at most ONCE per iteration",
    ])


@pytest.mark.parametrize("name", SHELL_PROMPTS)
def test_prompt_never_chains_two_whole_suite_runs(name):
    _assert_all(name, "never chain two whole-suite runs", [
        r"never\s*\n?\s*chain two whole-suite runs",
    ])


@pytest.mark.parametrize("name", SHELL_PROMPTS)
def test_prompt_backgrounds_a_long_suite_run_instead_of_blocking(name):
    _assert_all(name, "background it, do not block", [
        r"background (a long|such a) run",
        r"polling it instead of\s*\n?\s*blocking on it",
    ])


@pytest.mark.parametrize("name", SHELL_PROMPTS)
def test_prompt_says_an_iteration_killed_at_the_cap_loses_uncommitted_work(name):
    _assert_all(name, "a kill at the cap loses uncommitted work", [
        r"cap is hard and has\s*\n?\s*no grace period",
        r"killed at the cap loses its uncommitted work",
    ])
