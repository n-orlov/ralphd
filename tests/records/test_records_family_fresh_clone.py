"""Task 010 (#176): pin that `tests/records/` passes against a genuine fresh
`git clone` of this repository, not merely against this checkout's own
working tree.

Every records module resolves its own `REPO_ROOT` off `Path(__file__)`
(the pattern `tests/report_claims.py` used before task 060 deleted it,
mirrored by this directory's own modules one level down) -- so the whole
family is, in principle, portable to any tree that has the same files at the
same relative layout. Task 009 (#176) pinned a narrower, whitebox version of
that claim: that a report-cited sha must be reachable from `HEAD`, checked
in-process against `git rev-list HEAD`'s own answer. This module pins the
broader, blackbox version directly: clone the repository for real (`git
clone --local --no-hardlinks`, no network, this tree's own `HEAD`) into a
throwaway
directory, then run `tests/records/` there for real, as its own subprocess,
and require it to come back green. If some records module ever grew a
hidden dependency on a stray file the clone would not carry (an untracked
fixture, a path reachable only via this checkout's own absolute location),
this is the node that would catch it -- `tests/records/` passing in THIS
checkout proves nothing about that.

**Marked `@pytest.mark.nightly` directly** (task 048's B.5 follow-on
precedent, e.g. `tests/test_skills_discoverability_survives_suite.py`'s own
nested-subprocess node): a real `git clone` plus a full nested `pytest`
subprocess run is inherently several real seconds by design, not merely
unoptimised, so it is pulled out of `smoke`/`fast` the same way that
precedent already establishes for "a real nested-pytest-subset run" --
moving just this one node, never thinning what it actually spawns. It still
runs whenever `tests/records/` is selected by directory (the records lane's
own recipe, `.venv/bin/python -m pytest -q tests/records/`), which selects
by directory rather than by marker and does not exclude `nightly` nodes.

**The single exemption in `test_records_family_budget.py`'s subprocess
budget.** `test_the_records_family_spawns_no_subprocess` there (task 059,
requirement N.2, renamed from `test_the_records_family_spawns_only_one_git_ls_files`
now that its own corpus-wide caller, `tests/records/test_report_claims.py`,
is retired) runs the rest of the family from a cold cache and fails on any
subprocess at all. This module's own `git clone` and nested `pytest`
subprocess are real subprocesses, so that budget test's nested run excludes
THIS file too (alongside itself) -- named there as the one deliberate
exemption, not a loosening of
what it still requires of everything else.
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

# `--no-hardlinks`: the clone destination (a tempdir, typically under a
# container's overlay `/tmp`) is often a different filesystem/device than
# `REPO_ROOT` itself (a mounted volume) -- git's default `--local` clone
# hardlinks objects when it can, which raises "Invalid cross-device link"
# the moment source and destination sit on different devices. Copying the
# objects instead is slightly slower but portable to that layout, and this
# test's whole point is portability.

# Generous but bounded: a local `git clone` of this tree plus a full nested
# `pytest -q tests/records/` run, each capped well under the iteration
# clock so a genuine hang surfaces as a timeout, never a silent stall.
CLONE_TIMEOUT_S = 120
NESTED_RUN_TIMEOUT_S = 120


@pytest.mark.nightly  # task 010 (#176): a real git clone + nested pytest
# subprocess run, inherently several real seconds by design -- see the
# module docstring for the precedent (task 048 B.5 follow-on) this follows.
def test_a_fresh_clone_of_the_repository_passes_the_records_family() -> None:
    with tempfile.TemporaryDirectory(prefix="ralphd-fresh-clone-") as tmp:
        clone_dir = Path(tmp) / "clone"
        clone = subprocess.run(
            ["git", "clone", "--quiet", "--local", "--no-hardlinks",
             str(REPO_ROOT), str(clone_dir)],
            capture_output=True, text=True, timeout=CLONE_TIMEOUT_S,
        )
        assert clone.returncode == 0, (
            f"`git clone --local --no-hardlinks {REPO_ROOT} {clone_dir}` "
            f"failed (exit code {clone.returncode}):\n{clone.stderr}"
        )
        assert (clone_dir / "tests" / "records" / "conftest.py").exists(), (
            f"fresh clone at {clone_dir} is missing tests/records/ -- the "
            f"clone did not carry the tree it was supposed to"
        )

        # Ignore this module's own path *inside the clone*: without this,
        # the nested run below would collect the clone's own copy of THIS
        # test, which would clone the clone and recurse without bound.
        self_in_clone = clone_dir / "tests" / "records" / Path(__file__).name
        run = subprocess.run(
            [sys.executable, "-m", "pytest", "-q",
             "--ignore", str(self_in_clone), "tests/records/"],
            cwd=clone_dir, capture_output=True, text=True,
            timeout=NESTED_RUN_TIMEOUT_S,
        )
        assert run.returncode == 0, (
            f"`{sys.executable} -m pytest -q --ignore {self_in_clone.name} "
            f"tests/records/` failed inside "
            f"the fresh clone at {clone_dir} (exit code {run.returncode}):\n"
            f"--- stdout ---\n{run.stdout}\n--- stderr ---\n{run.stderr}"
        )
