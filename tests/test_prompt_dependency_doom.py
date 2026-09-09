"""worker.md must state that failed-task resolution never clears dependency doom (task 004).

`state.TASK_DEPENDENCY_SATISFIED_STATUSES` is `("completed", "validated")`
(src/ralphd/engine/state.py) and the scheduler
(src/ralphd/engine/scheduler.py) only unblocks a dependent whose every
`dependsOn` entry has reached one of those two statuses. Neither of the two
failed-task resolutions worker.md offers in `## Completion signal` ever puts
the ORIGINAL failed task into either status:

  - carving the residual gap into a NEW task and completing that new task
    leaves the old task's own status untouched at `failed`;
  - relabelling the old task `skipped` moves it to a THIRD status, still
    outside the satisfied pair.

So any other task whose `dependsOn` names that id stays `blocked` forever
after either resolution, and a worker that just picks whichever resolution
looks tidiest and calls the plan fixed has not actually unblocked anything.
worker.md must say this explicitly and tell the worker to report the doomed
set and stop, rather than silently declaring victory. Each element gets its
own named test, so deleting any one of them fails a test that says which rule
went missing rather than a catch-all.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
WORKER = REPO_ROOT / "src" / "ralphd" / "prompts" / "worker.md"
STATE = REPO_ROOT / "src" / "ralphd" / "engine" / "state.py"


def _worker_text() -> str:
    assert WORKER.is_file(), f"missing prompt file {WORKER}"
    return WORKER.read_text()


def _completion_signal_section() -> str:
    """The text of worker.md's `## Completion signal` section."""
    text = _worker_text()
    m = re.search(r"^##\s+Completion signal\s*$(.*?)(?=^##\s|\Z)",
                  text, re.MULTILINE | re.DOTALL)
    assert m, "worker.md has no '## Completion signal' section"
    return m.group(1)


def _assert_all(rule: str, patterns: list[str]) -> None:
    section = _completion_signal_section()
    missing = [p for p in patterns
               if not re.search(p, section, re.IGNORECASE | re.DOTALL)]
    assert not missing, (
        "worker.md's completion-signal section is missing the "
        f"'{rule}' rule about dependency doom; no match for: {missing}")


def test_dependency_satisfied_statuses_constant_matches_the_prose_claim():
    """Sanity-pin the engine constant the prose relies on, so a change to it
    is caught here rather than silently making the prose wrong."""
    text = STATE.read_text()
    m = re.search(r'TASK_DEPENDENCY_SATISFIED_STATUSES\s*=\s*\(([^)]*)\)', text)
    assert m, "state.py no longer defines TASK_DEPENDENCY_SATISFIED_STATUSES"
    values = {v.strip().strip('"\'') for v in m.group(1).split(',') if v.strip()}
    # Task 016 (PRD requirement B.1, #156) added `superseded` -- the status
    # a forced split leaves on the task it just split -- to this tuple, so
    # a dependent of a superseded task is not blocked forever waiting for a
    # status the parent will never reach again. That addition is orthogonal
    # to worker.md's own `## Completion signal` prose this module pins,
    # which is entirely about `failed`/`skipped` staying OUTSIDE the pair --
    # both stay excluded here too, so this check keeps a real bite.
    assert values == {"completed", "validated", "superseded"}, (
        f"TASK_DEPENDENCY_SATISFIED_STATUSES changed to {values}; "
        "the worker.md prose asserted by this module must be updated too"
    )
    assert "failed" not in values and "skipped" not in values


def test_completion_signal_names_the_dependency_satisfied_statuses():
    _assert_all("names the satisfied-status pair", [
        r"TASK_DEPENDENCY_SATISFIED_STATUSES",
        r"`completed`",
        r"`validated`",
    ])


def test_carving_a_residual_task_is_stated_not_to_clear_doom():
    _assert_all("carving does not clear doom", [
        r"[Cc]arving the residual gap[\s\S]*?leaves the original `failed` task's own status",
        r"never `completed` or `validated`",
    ])


def test_relabelling_skipped_is_stated_not_to_clear_doom_either():
    _assert_all("skipped does not clear doom either", [
        r"relabelling it `skipped` does not change that\s*\n?\s*either",
        r"`skipped` is not in `TASK_DEPENDENCY_SATISFIED_STATUSES`",
    ])


def test_dependents_are_stated_to_stay_blocked_forever_either_way():
    _assert_all("dependents stay blocked forever either way", [
        r"stays `blocked` forever after either resolution",
        r"whichever resolution you picked",
    ])


def test_worker_is_told_not_to_pick_whichever_resolution_looks_tidiest():
    _assert_all("do not pick whichever looks tidiest", [
        r"[Dd]o not pick whichever of the two\s*\n?\s*resolutions looks tidiest",
    ])


def test_worker_is_told_to_report_the_doomed_set_and_stop():
    _assert_all("report the doomed set and stop", [
        r"report that doomed set in the\s*\n?\s*notes file and stop",
        r"taskBlockedBy",
    ])
