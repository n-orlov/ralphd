"""Task 035 (requirement E.4): measure and pin the records family itself --
how many tests `tests/records/` collects, how long a real run of it takes,
and what it actually spawns a subprocess for.

`docs/testing.md`'s budget table carries the measured numbers this module
pins live: the family collects tests against a **300** budget and runs in
a few seconds against a **15 s** budget -- both well inside target, so
neither is disclosed as a miss.

v0.20.1 tasks 044/045/046 retired the sweep-sha and conformance-ledger
guard coverage and lowered this ceiling to 150 (operator ruling
`001-039.md` point 3); task cure-02-02 restores that coverage (review
finding: the ruling is not binding for approach 2), so the ceiling is
restored to its pre-retirement value of 300, matching the family's
restored collected count.

**The subprocess rule.** A real run of the whole family spawns exactly TWO
real subprocesses -- one `git ls-files` and one `git rev-list HEAD` -- and
`test_the_records_family_spawns_only_one_git_ls_files` below pins
that live. Getting to one (from ~220) took three fixes in task 035; task 009
(#176) then added the second, deliberately, so a report-cited sha that only
exists as a dangling object (present in `.git` but not an ancestor of
`HEAD`) is a claim problem instead of a silent pass:

1. `tests/report_claims.py`'s `commit_problems()` used to answer "is this
   sha a commit object" with a `git cat-file --batch-check` subprocess, once
   PER REPORT that carried a sha claim -- about 218 of the ~220 subprocess
   calls a full `tests/records/` run made (measured on this task's own tree,
   before the fix). It now reads the object type straight out of `.git`
   in-process (`_ObjectDatabase`: loose objects' zlib header, packed
   objects' pack-entry type bits, following a delta chain to its base), so
   the sha check spends no subprocess at all. `commit_problems()` also
   requires that resolved commit to be reachable from `HEAD` (task 009,
   #176), checked against one `git rev-list HEAD` snapshot
   (`_reachable_commits()`), memoised the same way `_tracked_paths()`
   memoises its own `git ls-files` snapshot.
2. `test_conformance_ledger.py`'s `_collect_node_ids()` set
   `RALPHD_SKIP_DOCKER_PROBE=1` around its nested in-process collection
   (new gate in `tests/test_docker_sibling_e2e.py::_docker_available()`),
   which removed a real `docker version` probe that collection pass spent
   without ever looking at the answer. That module is itself retired now
   (v0.20.1 task 045, ruling `001-039.md` point 3), so this particular fix
   is history -- the gate it added stays live for any other in-process
   collection.
3. `tests/records/test_report_claims.py`'s whitebox memoisation test fakes
   `subprocess.run`'s return instead of calling real git, so proving the
   caching logic no longer spends a second real `git ls-files`.

The reader that replaced the `git cat-file` call is checked against real
git's own answers by `tests/test_report_claims_git_objects.py`, which lives
OUTSIDE this family precisely because that comparison has to spawn the very
subprocess this family is pinned not to spawn.

**The single exemption.** Task 010 (#176) added
`test_records_family_fresh_clone.py`, whose one node deliberately spawns a
real `git clone` plus a nested `pytest` subprocess to prove the family
passes against a genuine fresh clone, not merely this checkout's own
working tree. That node is marked `@pytest.mark.nightly` directly (so it
stays out of `smoke`/`fast`) but still lives inside this directory and
still runs whenever `tests/records/` is selected by directory, so the
nested run below excludes it by name alongside this file itself -- the one
deliberate exemption from the two-subprocess count, never a second real
`git ls-files` or `git rev-list HEAD` slipping in uncounted.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
RECORDS_DIR = REPO_ROOT / "tests" / "records"

# docs/testing.md's own ceiling for this lane (task 032's E.1, restated here
# so this module's own assertion and the doc's row cite the same number).
# Ratcheted 300 -> 150 by v0.20.1 task 045, then restored to 300 by task
# cure-02-02 when the conformance-ledger checker's guard coverage was
# restored (review finding: operator ruling 001-039.md is not binding for
# approach 2), bringing the family back to its 204-collected count.
COLLECTED_BUDGET = 300


class _CollectPlugin:
    """Records every item pytest's own collector visits, in-process."""

    def __init__(self) -> None:
        self.node_ids: list[str] = []

    def pytest_itemcollected(self, item) -> None:
        self.node_ids.append(item.nodeid)


def test_the_records_family_collects_at_most_the_budgeted_count() -> None:
    """`--collect-only` over `tests/records/` alone, in-process (task 033's
    own `pytest.main()` mechanism -- never a subprocess): the count this
    directory holds right now, checked against the budget."""
    plugin = _CollectPlugin()
    args = ["-q", "--collect-only", "-p", "no:cacheprovider", str(RECORDS_DIR)]
    ret = pytest.main(args, plugins=[plugin])
    assert ret == pytest.ExitCode.OK, (
        f"in-process collection of tests/records/ failed (exit code {ret})")
    assert plugin.node_ids, "tests/records/ collects nothing"
    assert len(plugin.node_ids) <= COLLECTED_BUDGET, (
        f"tests/records/ collects {len(plugin.node_ids)} tests, over the "
        f"{COLLECTED_BUDGET} budget docs/testing.md declares")


def test_the_records_family_spawns_only_one_git_ls_files(monkeypatch) -> None:
    """A real run of the WHOLE family (not just collecting it) spawns exactly
    two real subprocesses -- one `git ls-files` (`path_problems()`'s tracked-
    path snapshot) and one `git rev-list HEAD` (`commit_problems()`'s
    reachable-commit snapshot, task 009/#176) -- and nothing else: no second
    call of either however many reports or claims exist, and no other
    command of any kind.

    This file excludes itself from the nested run it launches (it would
    otherwise recurse into calling `pytest.main()` on itself, forever), and
    also excludes `test_records_family_fresh_clone.py` -- task 010's (#176)
    one deliberate exemption: that module's own node spawns a real `git
    clone` plus a nested `pytest` subprocess on purpose, which would
    otherwise show up here as an uncounted extra call. The other 110+ nodes
    in the directory are exactly "the rest of the family" this test
    measures.
    """
    import report_claims as report_claims_module

    # Force a cold cache so this genuinely exercises the fetch path rather
    # than a no-op reuse of whatever an earlier test already warmed --
    # deliberately NOT reverted afterward (no monkeypatch here): a real
    # fresh process warms these once and stays warm for its own remaining
    # life, and this test's nested run should behave the same way for
    # whatever runs after it in this same process.
    report_claims_module._TRACKED_PATHS = None
    report_claims_module._COMMIT_KINDS = None
    report_claims_module._COMMIT_FULL_SHAS = None
    report_claims_module._REACHABLE_COMMITS = None
    report_claims_module._OBJECTS = None

    calls: list[list[str]] = []
    real_run = subprocess.run

    def counting_run(*args, **kwargs):
        argv = list(args[0]) if args else list(kwargs.get("args", []))
        calls.append(argv)
        return real_run(*args, **kwargs)

    monkeypatch.setattr(subprocess, "run", counting_run)

    this_file = str(Path(__file__).resolve())
    fresh_clone_file = str(
        Path(__file__).resolve().parent / "test_records_family_fresh_clone.py")
    args = [
        "-q", "-p", "no:cacheprovider",
        "--ignore", this_file, "--ignore", fresh_clone_file,
        str(RECORDS_DIR),
    ]
    ret = pytest.main(args)
    assert ret == pytest.ExitCode.OK, (
        f"a real run of tests/records/ (minus this file) failed (exit "
        f"code {ret}) -- rerun without the subprocess patch to see why")

    expected = {("git", "ls-files"), ("git", "rev-list", "HEAD")}
    others = [c for c in calls if tuple(c) not in expected]
    assert not others, (
        f"tests/records/ spawned a subprocess other than `git ls-files` or "
        f"`git rev-list HEAD`: {others}")
    ls_files_calls = [c for c in calls if tuple(c) == ("git", "ls-files")]
    rev_list_calls = [c for c in calls if tuple(c) == ("git", "rev-list", "HEAD")]
    assert len(ls_files_calls) == 1, (
        f"expected exactly one `git ls-files`, got {len(ls_files_calls)}: {calls}")
    assert len(rev_list_calls) == 1, (
        f"expected exactly one `git rev-list HEAD`, got {len(rev_list_calls)}: "
        f"{calls}")
    assert len(calls) == 2, (
        f"tests/records/ spawned {len(calls)} subprocess calls from a cold "
        f"cache, expected exactly two: {calls}")
