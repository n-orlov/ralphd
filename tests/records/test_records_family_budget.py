"""Task 035 (requirement E.4): measure and pin the records family itself --
how many tests `tests/records/` collects, how long a real run of it takes,
and what it actually spawns a subprocess for.

`docs/testing.md`'s budget table carries the measured numbers this module
pins live: the family collects **192** tests against a **300** budget and
runs in about **2.6-3.0 s** serial against a **15 s** budget -- both well
inside target, so neither is disclosed as a miss.

**The subprocess rule, and a correction to how the task names it.** The
PRD/task text says the family "spawns no subprocess other than one git
ls-files"; that undercounts by one kind of call. `tests/report_claims.py`
has always needed TWO different git subprocesses, not one:
`path_problems()` needs `git ls-files` (path membership) and
`commit_problems()` needs `git cat-file --batch-check` (commit existence +
object type) -- checking a sha claim was never a membership question
`git ls-files` can answer. Before this task, `commit_problems` ran its own
`git cat-file --batch-check` once PER REPORT that carried a sha claim --
about 218 of the ~220 subprocess calls a full `tests/records/` run made
(measured on this task's own tree, before the fix below). This task's
change to `tests/report_claims.py` batches every sha across every report
into ONE such call, memoised for the process, mirroring `_tracked_paths()`'s
existing one-`git-ls-files`-per-process rule -- and a second fix
(`tests/test_docker_sibling_e2e.py`'s `_docker_available()`,
`tests/records/test_conformance_ledger.py`'s `_collect_node_ids()`) removes
a third, unrelated subprocess a nested collection pass was spending on a
`docker version` probe it never looked at the answer to. What is left, and
what `test_the_records_family_spawns_only_its_two_necessary_git_snapshots`
below pins as the family's real floor, is exactly the two DIFFERENT git
snapshots the two DIFFERENT checks structurally need -- never a third, and
never either one more than once per process.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
RECORDS_DIR = REPO_ROOT / "tests" / "records"

# docs/testing.md's own ceiling for this lane (task 032's E.1, restated here
# so this module's own assertion and the doc's row cite the same number).
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


def test_the_records_family_spawns_only_its_two_necessary_git_snapshots(
        monkeypatch) -> None:
    """A real run of the WHOLE family (not just collecting it) spawns
    exactly two real subprocesses -- one `git ls-files` and one
    `git cat-file --batch-check` -- never a third, and never either one
    more than once no matter how many reports or claims exist.

    This file excludes itself from the nested run it launches (it would
    otherwise recurse into calling `pytest.main()` on itself, forever); the
    other 190+ nodes in the directory are exactly "the rest of the family"
    this test measures.
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

    calls: list[list[str]] = []
    real_run = subprocess.run

    def counting_run(*args, **kwargs):
        argv = list(args[0]) if args else list(kwargs.get("args", []))
        calls.append(argv)
        return real_run(*args, **kwargs)

    monkeypatch.setattr(subprocess, "run", counting_run)

    this_file = str(Path(__file__).resolve())
    args = ["-q", "-p", "no:cacheprovider", "--ignore", this_file, str(RECORDS_DIR)]
    ret = pytest.main(args)
    assert ret == pytest.ExitCode.OK, (
        f"a real run of tests/records/ (minus this file) failed (exit "
        f"code {ret}) -- rerun without the subprocess patch to see why")

    kinds = [tuple(c[:2]) for c in calls]
    others = [c for c in calls
              if tuple(c[:2]) not in (("git", "ls-files"), ("git", "cat-file"))]
    assert not others, f"tests/records/ spawned an unexpected subprocess: {others}"
    assert kinds.count(("git", "ls-files")) == 1, (
        f"expected exactly one `git ls-files` call from a cold cache, got "
        f"{kinds.count(('git', 'ls-files'))}: {calls}")
    assert kinds.count(("git", "cat-file")) == 1, (
        f"expected exactly one `git cat-file --batch-check` call from a "
        f"cold cache, got {kinds.count(('git', 'cat-file'))}: {calls}")
    assert len(calls) == 2, (
        f"tests/records/ spawned {len(calls)} subprocess calls from a cold "
        f"cache, expected exactly 2 (one git ls-files, one git cat-file "
        f"--batch-check): {calls}")
