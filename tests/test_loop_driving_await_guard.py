"""Task 073 (steer 001 §2): a fast-tier guard against a NEW test module
introducing a bare `asyncio.run(...)` over one of the three loop-driving
calls the two known forever-await traps sit behind: `run_job`,
`_run_job_core` and `_step_review`. Both traps are named in the run's own
standing rules and in task 072's commit messages (`3b936b8`, `d4a0658`):
`_step_paused`'s `await self._gate()`, cleared only by `POST /pause`'s
`loop.pause()`, and a `_ScriptedRunner` script whose repeating last step
never reaches a terminal phase. A test that drives one of the three named
calls unbound can park on either trap forever; task 072 bounded the five
modules that were reachable from requirements A-C (three written directly,
plus two imported helper modules those three modules pulled the driver
functions from), but nothing stopped a SIXTH module from reintroducing the
exact same shape tomorrow -- this guard is that stop.

The suite already carries pre-existing bare call sites over these same
three names, spread across dozens of modules that predate this guard --
retrofitting every one of them is out of THIS task's own successCriteria,
which asks only that a NEW unbounded occurrence goes red, not that every
historical one already be bound. Each pre-existing file is grandfathered
below in `LOOP_DRIVING_AWAIT_ALLOWLIST` by (file, count) pair, sharing one
reason string, so the guard fires exactly when:

* a file with NO recorded baseline (a brand-new module, task 072's own two
  written-then-bounded modules, or any module bound from the start)
  acquires one -- the "new module" shape steer 001 §2 names directly, and
* a file that DOES have a recorded baseline acquires MORE bare calls than
  its own baseline count -- so bumping an already-grandfathered module is
  caught too, not only a brand-new file -- and
* an allowlist entry's recorded count no longer matches reality (lower,
  because someone bound some of that file's calls without touching this
  guard) -- a stale entry, same direction `tests/test_ambient_home_
  allowlist_guard.py` already checks for its own allowlist.

Two mutation tests below prove the guard fires in both directions, entirely
against a `tmp_path` scratch tree, never against the real `tests/` tree.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
TESTS_ROOT = REPO_ROOT / "tests"
THIS_MODULE_NAME = Path(__file__).name

# The two named forever-await traps, and the three loop-driving calls that
# reach them (task 072's commit messages, the run's own standing rules).
LOOP_DRIVING_NAMES = ("run_job", "_run_job_core", "_step_review")

# A bare `asyncio.run(...)` directly over one of the three names, optionally
# through a `sup.`/`sup1.`/`sup2.`/etc. attribute prefix, and NOT already
# wrapped in `asyncio.wait_for(...)` or a per-module `_bounded(...)` helper
# -- neither of those text shapes can appear directly inside the
# `asyncio.run(` parens this regex requires immediately after the literal.
BARE_ASYNCIO_RUN_RE = re.compile(
    r"asyncio\.run\(\s*(?:\w+\.)?(?:" + "|".join(LOOP_DRIVING_NAMES) + r")\(\)\s*\)"
)


def _is_bare_call_line(text: str) -> bool:
    """`text` is one raw source line. A line whose *stripped* form opens
    with a backtick or a `#` is prose/comment quoting the shape (this
    module's own docstring above does exactly that), never a real call
    site, so it never counts."""
    stripped = text.strip()
    if stripped.startswith("`") or stripped.startswith("#"):
        return False
    return bool(BARE_ASYNCIO_RUN_RE.search(text))


def find_bare_loop_driving_awaits(root: Path) -> dict[str, list[int]]:
    """Every `*.py` file directly under `root`, recursively, that contains a
    bare loop-driving `asyncio.run(...)` -- `{filename: [1-based line
    numbers]}`, keyed by filename relative to `root` itself (never the repo
    root, so a `tmp_path` scratch tree and the real `tests/` tree use the
    exact same key shape). This guard's own module is always skipped: it is
    the scanner, not a call site.
    """
    out: dict[str, list[int]] = {}
    for path in sorted(root.rglob("*.py")):
        if path.name == THIS_MODULE_NAME:
            continue
        lines = [
            lineno
            for lineno, text in enumerate(path.read_text().splitlines(), start=1)
            if _is_bare_call_line(text)
        ]
        if lines:
            out[str(path.relative_to(root).as_posix())] = lines
    return out


# Every pre-existing bare call site over the three named loop-driving calls,
# as measured against this task's own tip (`.venv/bin/python -m pytest`,
# `find_bare_loop_driving_awaits(TESTS_ROOT)` run directly): 37 files, 128
# call sites total, none of them task 072's own five modules (those are
# already bounded through `asyncio.wait_for`/a `_bounded()` helper, so they
# carry zero and need no entry here). Retrofitting these is a future task's
# job, not this guard's -- see the module docstring above.
_GRANDFATHER_REASON = (
    "pre-existing at task 073's own tip (steer 001 \u00a72): grandfathered "
    "as this guard's baseline rather than retrofitted here -- task 073's "
    "own successCriteria asks only that a NEW unbounded occurrence goes "
    "red, not that every historical one already be bound. Bounding this "
    "file's calls (asyncio.wait_for(coro, timeout), or a per-module "
    "_bounded() helper as tests/test_peer_verdict_gate.py added for task "
    "072) and lowering or removing this entry is a future task, not this "
    "one."
)

LOOP_DRIVING_AWAIT_ALLOWLIST: list[dict[str, object]] = [
    {"path": "test_amendment_status_load_time.py", "count": 1, "reason": _GRANDFATHER_REASON},
    {"path": "test_confirmed_petition_incurable_remainder.py", "count": 2, "reason": _GRANDFATHER_REASON},
    {"path": "test_contract_events.py", "count": 3, "reason": _GRANDFATHER_REASON},
    {"path": "test_contract_ladder.py", "count": 4, "reason": _GRANDFATHER_REASON},
    {"path": "test_criteria_amendment_authority.py", "count": 4, "reason": _GRANDFATHER_REASON},
    {"path": "test_cure_invalidates.py", "count": 1, "reason": _GRANDFATHER_REASON},
    {"path": "test_cure_pass.py", "count": 15, "reason": _GRANDFATHER_REASON},
    {"path": "test_deadline.py", "count": 9, "reason": _GRANDFATHER_REASON},
    {"path": "test_deferred_pin_task024.py", "count": 1, "reason": _GRANDFATHER_REASON},
    {"path": "test_deferred_tasks_surface.py", "count": 1, "reason": _GRANDFATHER_REASON},
    {"path": "test_deferred_verdict.py", "count": 1, "reason": _GRANDFATHER_REASON},
    {"path": "test_deferred_wait.py", "count": 1, "reason": _GRANDFATHER_REASON},
    {"path": "test_engine_finishing_state.py", "count": 6, "reason": _GRANDFATHER_REASON},
    {"path": "test_episode_scoped_ending.py", "count": 1, "reason": _GRANDFATHER_REASON},
    {"path": "test_hat_extensibility.py", "count": 1, "reason": _GRANDFATHER_REASON},
    {"path": "test_iteration_cancellation_contained.py", "count": 1, "reason": _GRANDFATHER_REASON},
    {"path": "test_last_approach.py", "count": 13, "reason": _GRANDFATHER_REASON},
    {"path": "test_operator_ruling_review_verdict.py", "count": 3, "reason": _GRANDFATHER_REASON},
    {"path": "test_plan_ingestion_rejection.py", "count": 2, "reason": _GRANDFATHER_REASON},
    {"path": "test_promise_marker_last_line.py", "count": 3, "reason": _GRANDFATHER_REASON},
    {"path": "test_reflect_deadline_boundary.py", "count": 1, "reason": _GRANDFATHER_REASON},
    {"path": "test_reflect_tail_budget.py", "count": 1, "reason": _GRANDFATHER_REASON},
    {"path": "test_repeated_rejection_signature.py", "count": 10, "reason": _GRANDFATHER_REASON},
    {"path": "test_resume_after_incurable_rejection.py", "count": 3, "reason": _GRANDFATHER_REASON},
    {"path": "test_review_interrupted_not_rejection.py", "count": 3, "reason": _GRANDFATHER_REASON},
    {"path": "test_router_calls_surface.py", "count": 2, "reason": _GRANDFATHER_REASON},
    {"path": "test_router_engine.py", "count": 2, "reason": _GRANDFATHER_REASON},
    {"path": "test_run_record.py", "count": 4, "reason": _GRANDFATHER_REASON},
    {"path": "test_stagnation_excludes_nonwork_faults.py", "count": 1, "reason": _GRANDFATHER_REASON},
    {"path": "test_standing_rules_pin.py", "count": 1, "reason": _GRANDFATHER_REASON},
    {"path": "test_steer.py", "count": 1, "reason": _GRANDFATHER_REASON},
    {"path": "test_transitions.py", "count": 10, "reason": _GRANDFATHER_REASON},
    {"path": "test_unconsumed_steering_live.py", "count": 1, "reason": _GRANDFATHER_REASON},
    {"path": "test_unsatisfiable_criteria.py", "count": 2, "reason": _GRANDFATHER_REASON},
    {"path": "test_worker_hat.py", "count": 11, "reason": _GRANDFATHER_REASON},
    {"path": "test_worker_split_proposal.py", "count": 1, "reason": _GRANDFATHER_REASON},
]

_REMEDY = (
    "bound the await instead: asyncio.run(asyncio.wait_for(coro, timeout)) "
    "or a per-module _bounded(coro, timeout=30.0) helper (see the one task "
    "072 added to tests/test_peer_verdict_gate.py and "
    "tests/test_peer_plan_review_gate.py) -- never a bare asyncio.run(...) "
    "over run_job/_run_job_core/_step_review, which can wait forever on "
    "either the paused-state Event or a repeating scripted-runner step."
)


def bare_await_violations(
    found: dict[str, list[int]],
    allowlist: list[dict[str, object]],
) -> list[str]:
    """Diff an ACTUAL scan (`find_bare_loop_driving_awaits()`'s return
    value) against an allowlist's claims about it. Three kinds of mismatch:

    * a file with more bare calls than its allowlist entry claims (or no
      entry at all, i.e. claiming zero) -- a NEW unbounded occurrence;
    * an allowlist entry whose claimed count is HIGHER than what is
      actually there -- a stale entry (someone already bound some of that
      file's calls without correcting this list);
    * an allowlist entry with a blank `reason` -- malformed regardless of
      the count matching.
    """
    allow_by_path = {str(e["path"]): int(e["count"]) for e in allowlist}
    violations: list[str] = []

    for entry in allowlist:
        if not str(entry.get("reason", "")).strip():
            violations.append(
                f"{entry['path']}: allowlist entry carries an empty reason")

    for path, lines in found.items():
        allowed = allow_by_path.get(path, 0)
        if len(lines) > allowed:
            kind = ("a NEW unlisted occurrence" if allowed == 0 else
                     f"more than the {allowed} grandfathered in "
                     f"LOOP_DRIVING_AWAIT_ALLOWLIST")
            violations.append(
                f"{path}: {len(lines)} bare asyncio.run(...) call(s) over "
                f"one of {LOOP_DRIVING_NAMES} (lines {lines}) -- {kind}. "
                f"{_REMEDY}")

    for path, allowed in allow_by_path.items():
        actual = len(found.get(path, []))
        if actual < allowed:
            violations.append(
                f"{path}: LOOP_DRIVING_AWAIT_ALLOWLIST claims {allowed} "
                f"bare call(s) but only {actual} remain now -- lower the "
                f"recorded count (stale entry).")

    return violations


def test_no_new_unbounded_loop_driving_await():
    """The real check: a NEW bare asyncio.run(...) over run_job/
    _run_job_core/_step_review, in any module under tests/, over what
    LOOP_DRIVING_AWAIT_ALLOWLIST already grandfathers, fails here."""
    found = find_bare_loop_driving_awaits(TESTS_ROOT)
    assert found, (
        "scan found zero bare loop-driving asyncio.run(...) call sites at "
        "all -- the regex or TESTS_ROOT drifted, since dozens are known to "
        "predate this guard")
    violations = bare_await_violations(found, LOOP_DRIVING_AWAIT_ALLOWLIST)
    assert not violations, "\n".join(violations)


def test_mutation_a_new_bare_call_in_a_brand_new_module_is_caught(tmp_path):
    """Proves the "new module" direction fires: a scratch file with one
    bare call and NO allowlist entry is reported by file and line."""
    planted = tmp_path / "test_fake_new_loop_driver.py"
    planted.write_text(
        "import asyncio\n\n"
        "def test_x(sup):\n"
        "    outcome = asyncio.run(sup._step_review())\n"
    )
    found = find_bare_loop_driving_awaits(tmp_path)
    assert found == {"test_fake_new_loop_driver.py": [4]}
    # An empty scratch allowlist, never the real LOOP_DRIVING_AWAIT_ALLOWLIST
    # -- that one is keyed to the real tests/ tree and would report every
    # one of its own entries as stale against this unrelated tmp_path tree.
    violations = bare_await_violations(found, [])
    assert len(violations) == 1
    assert "test_fake_new_loop_driver.py" in violations[0]
    assert "NEW unlisted occurrence" in violations[0]
    assert "bound the await" in violations[0]


def test_mutation_one_more_call_over_a_grandfathered_baseline_is_caught(tmp_path):
    """Proves the "bumped an already-grandfathered module" direction
    fires: a scratch file claims a baseline of 1 in a throwaway allowlist
    but actually carries 2."""
    planted = tmp_path / "test_fake_already_grandfathered.py"
    planted.write_text(
        "import asyncio\n\n"
        "def test_a(sup):\n"
        "    asyncio.run(sup._run_job_core())\n\n"
        "def test_b(sup2):\n"
        "    asyncio.run(sup2._run_job_core())\n"
    )
    scratch_allowlist = [
        {"path": "test_fake_already_grandfathered.py", "count": 1,
         "reason": "throwaway baseline for this mutation test"},
    ]
    found = find_bare_loop_driving_awaits(tmp_path)
    assert found == {"test_fake_already_grandfathered.py": [4, 7]}
    violations = bare_await_violations(found, scratch_allowlist)
    assert len(violations) == 1
    assert "more than the 1 grandfathered" in violations[0]


def test_mutation_a_stale_allowlist_entry_is_caught(tmp_path):
    """The mirror-image check `tests/test_ambient_home_allowlist_guard.py`
    already applies to its own allowlist: an entry claiming more bare
    calls than a file actually has anymore is stale, not silently
    accepted."""
    planted = tmp_path / "test_fake_partly_bound.py"
    planted.write_text(
        "import asyncio\n\n"
        "def test_a(sup):\n"
        "    asyncio.run(sup._run_job_core())\n"
    )
    scratch_allowlist = [
        {"path": "test_fake_partly_bound.py", "count": 2,
         "reason": "throwaway stale baseline for this mutation test"},
    ]
    found = find_bare_loop_driving_awaits(tmp_path)
    violations = bare_await_violations(found, scratch_allowlist)
    assert len(violations) == 1
    assert "stale entry" in violations[0]


def test_a_bounded_call_never_matches_the_regex():
    """A `_bounded()`/`asyncio.wait_for(...)`-wrapped call is exactly what
    task 072 landed, and must never re-trip this guard."""
    bounded_lines = [
        "    outcome = asyncio.run(asyncio.wait_for(sup._step_review(), 10))",
        "    return sup, _bounded(sup._run_job_core())",
        "    final_state = _bounded(sup.run_job())",
    ]
    for line in bounded_lines:
        assert not _is_bare_call_line(line), line


def test_every_allowlist_entry_carries_a_reason():
    for entry in LOOP_DRIVING_AWAIT_ALLOWLIST:
        assert str(entry["reason"]).strip(), entry["path"]
