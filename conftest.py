"""Root-level conftest, loaded by pytest before it collects (and therefore
before it imports) anything under `tests/`.

Purpose (task 005, #50): guarantee that `import tests.conftest` (and the
sibling absolute imports several test modules use, e.g. `from
tests.test_roadmap_v07 import closed_issues`) always resolve to *this*
repository's `tests` package, no matter what else on `sys.path` also
happens to be importable as `tests` (another project's test suite, a
decoy package placed ahead of this repo by `PYTHONPATH`, a pytest plugin
that pushes its own directory onto `sys.path` during early setup, etc.).

Mechanism: `tests/` has no `__init__.py` (deliberately -- see below), so
it is a PEP 420 *namespace* package. A namespace package's `__path__` is
the list of every `sys.path` entry that contains a `tests/` directory,
in the order those entries are found *at the moment `tests` is first
imported* -- and submodule lookups (`tests.conftest`) search that
`__path__` in order, using the first hit. So whichever `tests/` directory
sits earliest in `sys.path`, at the time of the first `import tests`,
wins every subsequent `tests.*` import for the rest of the process.

This conftest.py is the earliest hook pytest gives us before that first
import can happen (it loads before collecting/importing anything under
`tests/`, including `tests/conftest.py` itself, and after any early
plugin/`-p` module has already run its own top-level code -- so it can
undo an early plugin's `sys.path` manipulation too). Inserting this
repository's root at `sys.path[0]` here, unconditionally, makes this
repository's `tests/` the first portion in the merge, regardless of what
was already on `sys.path` when this file started loading.

Deliberately NOT fixed by adding `tests/__init__.py`: that would make
`tests/` a *regular* package, which changes which base directory pytest's
own "prepend" import mode inserts on `sys.path` for every module
collected under `tests/` -- and ~40 of this suite's test modules already
rely on bare top-level imports (`from test_cli_docker import ...` rather
than `from tests.test_cli_docker import ...`), which depend on `tests/`
itself (not its parent) being directly on `sys.path`. Making `tests/` a
namespace package (the status quo) preserves that; a regular package
would break all of them at once. `sys.path.insert` below only reorders
`sys.path`, so it does not touch that dependency either way.

Tested by tests/test_tests_conftest_resolution.py, which plants a decoy
top-level `tests` package (both a plain `PYTHONPATH` entry and one pushed
by an early-loading pytest plugin) ahead of this repo on `sys.path` and
asserts collection still resolves `tests.conftest` to this repo's copy.
"""

from __future__ import annotations

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent
_root_str = str(_REPO_ROOT)
if _root_str in sys.path:
    sys.path.remove(_root_str)
sys.path.insert(0, _root_str)
