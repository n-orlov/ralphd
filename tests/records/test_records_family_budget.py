"""Task 035 (requirement E.4): measure and pin the records family itself --
how many tests `tests/records/` collects, how long a real run of it takes,
and what it actually spawns a subprocess for.

`docs/testing.md`'s budget table carries the measured numbers this module
pins live: the family collects tests against a **150** budget and runs in
a few seconds against a **15 s** budget -- both well inside target, so
neither is disclosed as a miss.

v0.20.1 tasks 044/045/046 retired the sweep-sha and conformance-ledger
guard coverage and lowered this ceiling to 150 (operator ruling
`001-039.md` point 3); task cure-02-02 restored that coverage (review
finding: the ruling is not binding for approach 2), raising the ceiling
back to 300 to match the family's then-restored 204-node count. v0.20.2's
requirement A retires that same coverage again, this time with the PRD
itself as authority (A.2, A.3 -- no operator ruling cited or needed), and
task 008 (requirement A.6) reconciles this ceiling downward once more, to
**150**, matching the family's current 116-node count with the same
modest headroom the pre-restoration value already used.

**The subprocess rule -- retired by task 059 (requirement N.2).** A real run
of the whole family used to spawn exactly two real subprocesses -- one
`git ls-files` and one `git rev-list HEAD` -- both spent by
`tests/records/test_report_claims.py`'s corpus-wide claim checks
(`path_problems()`'s tracked-path snapshot, `commit_problems()`'s
reachable-commit snapshot, task 009/#176). Task 059 retires that whole
module along with the corpus-wide read it drove (N.2: the check machinery
is retired ahead of N.1's deletion of the corpus itself), so nothing left
in this directory calls `report_claims.commit_problems()`/`path_problems()`
at all, and `test_the_records_family_spawns_no_subprocess` below now pins
the family at **zero** real subprocesses instead. Task 060 (N.2's second
half) then deletes `tests/report_claims.py` and `tests/test_issue_traceability.py`
entire: that single-report caller was the module's last remaining user, so
the parser and the checks no longer exist anywhere in the tree, not only
under `tests/records/`. See `tests/test_report_claims_retired.py` for the
live guard against either file coming back.

**The single exemption.** Task 010 (#176) added
`test_records_family_fresh_clone.py`, whose one node deliberately spawns a
real `git clone` plus a nested `pytest` subprocess to prove the family
passes against a genuine fresh clone, not merely this checkout's own
working tree. That node is marked `@pytest.mark.nightly` directly (so it
stays out of `smoke`/`fast`) but still lives inside this directory and
still runs whenever `tests/records/` is selected by directory, so the
nested run below excludes it by name alongside this file itself -- now the
one exemption from the **zero**-subprocess count, never a stray
`git ls-files`/`git rev-list HEAD`/anything else slipping in uncounted.
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
# v0.20.2 requirement A.2/A.3 retires that same coverage again (PRD itself
# is the authority this time, no operator ruling), dropping the family to
# 116 nodes; task 008 (requirement A.6) reconciles this ceiling downward
# once more, to 150, matching the 116-node count with modest headroom.
COLLECTED_BUDGET = 150


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


def test_the_records_family_spawns_no_subprocess(monkeypatch) -> None:
    """A real run of the WHOLE family (not just collecting it) now spawns NO
    real subprocess at all. Before task 059 (requirement N.2) this pinned
    exactly two -- one `git ls-files` (`path_problems()`'s tracked-path
    snapshot) and one `git rev-list HEAD` (`commit_problems()`'s
    reachable-commit snapshot, task 009/#176) -- both spent by
    `tests/records/test_report_claims.py`'s corpus-wide claim checks. That
    module (this directory's one caller of `report_claims.commit_problems()`/
    `path_problems()`) is retired by task 059 along with the corpus-wide read
    it drove, so nothing left under `tests/records/` spends either
    subprocess any more, or any other one.

    This file excludes itself from the nested run it launches (it would
    otherwise recurse into calling `pytest.main()` on itself, forever), and
    also excludes `test_records_family_fresh_clone.py` -- task 010's (#176)
    one deliberate exemption: that module's own node spawns a real `git
    clone` plus a nested `pytest` subprocess on purpose, which would
    otherwise show up here as an uncounted extra call. The other 100+ nodes
    in the directory are exactly "the rest of the family" this test
    measures.
    """
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

    assert not calls, (
        f"tests/records/ spawned {len(calls)} subprocess call(s), expected "
        f"none now that its one caller of report_claims's corpus-wide checks "
        f"is retired (task 059, requirement N.2): {calls}")
