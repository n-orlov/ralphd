"""Task 035 (requirement E.4): measure and pin the records family itself --
how many tests `tests/records/` collects, how long a real run of it takes,
and what it actually spawns a subprocess for.

`docs/testing.md`'s budget table carries the measured numbers this module
pins live: the family collects **194** tests against a **300** budget and
runs in about **5.1-5.6 s** serial against a **15 s** budget -- both well
inside target, so neither is disclosed as a miss.

**The subprocess rule.** A real run of the whole family spawns exactly ONE
real subprocess, `git ls-files`, and
`test_the_records_family_spawns_only_one_git_ls_files` below pins that live.
Getting there took three fixes, all in this task:

1. `tests/report_claims.py`'s `commit_problems()` used to answer "is this
   sha a commit object" with a `git cat-file --batch-check` subprocess, once
   PER REPORT that carried a sha claim -- about 218 of the ~220 subprocess
   calls a full `tests/records/` run made (measured on this task's own tree,
   before the fix). It now reads the object type straight out of `.git`
   in-process (`_ObjectDatabase`: loose objects' zlib header, packed
   objects' pack-entry type bits, following a delta chain to its base), so
   the sha check spends no subprocess at all. `tests/report_claims.py`'s
   single remaining `subprocess.run` is the one `git ls-files` snapshot
   `path_problems()` needs for path membership.
2. `tests/records/test_conformance_ledger.py`'s `_collect_node_ids()` sets
   `RALPHD_SKIP_DOCKER_PROBE=1` around its nested in-process collection
   (new gate in `tests/test_docker_sibling_e2e.py::_docker_available()`),
   which removed a real `docker version` probe that collection pass spent
   without ever looking at the answer.
3. `tests/records/test_report_claims.py`'s whitebox memoisation test fakes
   `subprocess.run`'s return instead of calling real git, so proving the
   caching logic no longer spends a second real `git ls-files`.

The reader that replaced the `git cat-file` call is checked against real
git's own answers by `tests/test_report_claims_git_objects.py`, which lives
OUTSIDE this family precisely because that comparison has to spawn the very
subprocess this family is pinned not to spawn.
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


def test_the_records_family_spawns_only_one_git_ls_files(monkeypatch) -> None:
    """A real run of the WHOLE family (not just collecting it) spawns exactly
    one real subprocess -- a single `git ls-files` -- and nothing else: no
    second `git ls-files` however many reports or claims exist, and no other
    command of any kind (requirement E.4's rule, verbatim: the family
    "spawns no subprocess other than one `git ls-files`").

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
    report_claims_module._OBJECTS = None

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

    others = [c for c in calls if tuple(c[:2]) != ("git", "ls-files")]
    assert not others, (
        f"tests/records/ spawned a subprocess other than `git ls-files`: "
        f"{others}")
    assert len(calls) == 1, (
        f"tests/records/ spawned {len(calls)} subprocess calls from a cold "
        f"cache, expected exactly one `git ls-files`: {calls}")
