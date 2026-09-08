"""Make the name `tests` mean *this* repository's `tests/` directory, and
nothing else, for the whole life of the process.

Why this is a module and not just `conftest.py` (#64, second half). The rule
below has to run before anything imports `tests`, and there are two entirely
different moments that can be:

  * **A normal run** (`pytest`, `pytest tests/...`, `pytest -k foo`) collects
    something under this repository, so pytest loads the repo-root
    `conftest.py`, which calls `pin_tests_package()`. That covers every way a
    developer or CI invokes the suite.
  * **A run pointed somewhere else entirely.** pytest only loads the conftests
    on the path to the arguments it was given, so `pytest /tmp/probe/test_x.py`
    -- even with the repository as the working directory -- never loads this
    repository's `conftest.py` at all. `scripts/host_like_pytest.sh` does
    exactly that, and any wrapper that runs an out-of-tree probe against this
    suite will too. Those invocations load this module as a plugin instead
    (`-p tests_package_pin`), which pytest imports and executes *before* any
    conftest.

Both paths end in the same call, and it is idempotent, so belt and braces is
free. Keeping the rule in a conftest alone was the shape of the original bug
one level out: a guarantee that only held for the invocation it was written
for.

--------------------------------------------------------------------------
The rule itself, and why it is a `sys.modules` pre-bind rather than a
`sys.path` reorder.

`tests/` here has no `__init__.py` (deliberately -- see the last section), so
it is a PEP 420 *namespace* package: its `__path__` is assembled by
`PathFinder` at the moment `tests` is first imported, by walking `sys.path` in
order. That walk does not simply concatenate every `tests/` directory it
meets. For each `sys.path` entry, `PathFinder._get_spec` asks the entry's
finder for `tests` and then:

  * a directory holding `tests/__init__.py` (a *regular* package), or a plain
    `tests.py` module, yields a spec **with a loader** -- the walk stops there,
    that spec wins, and every namespace portion accumulated so far is
    **discarded**;
  * a directory holding no `__init__.py` yields only a loader-less "portion",
    which is merely *appended* to a running list. The list becomes a namespace
    package only if the walk finishes without ever finding a loader.

So namespace portions accumulate, but a regular package pre-empts. Being at
`sys.path[0]` buys this repository nothing against a regular `tests` package
sitting at `sys.path[-1]`, because this repository's `tests/` is not a package
at all -- it is a portion, and portions lose to loaders regardless of
position. Demonstrable in three lines: put a plain `tests/conftest.py` in dir
A and a `tests/__init__.py` + `tests/conftest.py` in dir B, then `sys.path` =
[A, B] -- `import tests.conftest` resolves to B.

Measured impact before the fix: `az_assertpy_extensions` and `az_s3_url_helper`
each install a top-level `tests/` *with* `__init__.py` into user
site-packages. With either present, `python -m pytest -q` in this repository
died with 42 collection errors and ran zero tests (40 x `ImportError: cannot
import name 'RALPHCTL' from 'tests.conftest'`, 2 x `ModuleNotFoundError: No
module named 'tests.records.test_roadmap'`) -- the site-packages `tests` package
had won outright and this repository's modules simply were not in it. A decoy
that happens to *define* the names a module imports is worse still: collection
succeeds and the wrong values are used silently.

What actually closes it: win on **precedence**, not on ordering. Below we
pre-bind `sys.modules["tests"]` to a namespace-style module whose `__path__` is
exactly `[<repo>/tests]`. `import tests.conftest` then finds `tests` already in
`sys.modules` (the very first step of the import protocol, ahead of
`sys.meta_path` and therefore ahead of `PathFinder` entirely) and resolves the
`conftest` submodule against that single `__path__` entry. Nothing on
`sys.path` -- regular package, namespace portion or plain module -- gets a say.

Two cases are handled:

  1. Nothing has imported `tests` yet: just bind. This is the common case.
  2. Something already has -- an early-loading plugin (`-p foo`, or a
     `pytest11` entry point) whose own top-level code ran before any conftest,
     or another `-p` module ahead of this one. `sys.modules["tests"]` is then
     the wrong object and cached `tests.*` entries point at the wrong files, so
     we replace the parent and purge the stale children. Anything still holding
     a reference to the displaced decoy keeps working; it just stops being what
     the name `tests` resolves to.

--------------------------------------------------------------------------
Deliberately NOT fixed by adding `tests/__init__.py`: that would make `tests/`
a *regular* package, which changes which base directory pytest's own "prepend"
import mode puts on `sys.path` for every module collected under `tests/` -- it
would insert the repo root instead of `tests/` -- and 102 of this suite's test
modules rely on bare top-level imports (`from test_cli_docker import ...`
rather than `from tests.test_cli_docker import ...`), which need `tests/`
itself, not its parent, directly on `sys.path`. Measured (not assumed):
dropping an `__init__.py` into `tests/` makes collection die with
`ModuleNotFoundError: No module named 'test_cli_docker'` on the first such
module collected (`test_absolute_timestamps.py`) and exit 2 with zero tests
run. Only *one* error is reported, because 18 other modules do
`sys.path.insert(0, str(Path(__file__).parent))` at import time and
accidentally repair `sys.path` for everything collected after them -- an
accident, not a design, and not one to rely on. Keeping `tests/` a namespace
package makes the whole question moot. The pre-bind is compatible with it: it
only decides what the *name* `tests` means, and leaves pytest's per-module
`sys.path` insertion untouched.

Tested by tests/test_tests_conftest_resolution.py, which plants decoy
top-level `tests` packages ahead of this repo -- as a namespace portion and as
a regular package, via `PYTHONPATH` and via an early-loading pytest plugin, and
both at once -- asserts collection still resolves `tests.conftest` here through
both load paths above, and pins that the bare top-level import style keeps
working.
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent
TESTS_DIR = REPO_ROOT / "tests"


def is_this_repos_tests(module: object) -> bool:
    """True only if `module` is a package whose submodule search path is
    exactly this repository's `tests/` -- i.e. no other directory can
    contribute a `tests.*` submodule. A decoy is either not a package at all
    (no `__path__`, e.g. a `tests.py` module), or has a `__path__` naming
    somewhere else, or is a namespace package that merged our directory
    together with others (still ambiguous: a submodule we do not ship could be
    served from the other portion)."""
    search = list(getattr(module, "__path__", None) or ())
    return len(search) == 1 and Path(search[0]) == TESTS_DIR


def pin_tests_package() -> None:
    """Install `sys.modules["tests"]` as a namespace-style package pinned to
    `<repo>/tests`, so `tests.*` never reaches `PathFinder`/`sys.path`.

    Idempotent: safe to call from both load paths described above, in either
    order, any number of times.
    """
    existing = sys.modules.get("tests")
    if existing is not None and is_this_repos_tests(existing):
        return  # already ours -- leave it alone

    if existing is not None:
        # Something imported a foreign `tests` before we got here. Its cached
        # `tests.*` children were resolved against the wrong `__path__`, so
        # they must go too, or `import tests.conftest` would be served from
        # `sys.modules` without ever consulting the `__path__` we are about to
        # install.
        for name in [n for n in sys.modules if n == "tests" or n.startswith("tests.")]:
            del sys.modules[name]

    # A loader-less spec with `submodule_search_locations` is exactly the shape
    # CPython gives a PEP 420 namespace package; `module_from_spec` copies
    # those locations onto `__path__`. Built by hand rather than by importing,
    # precisely so no `sys.path` search happens.
    spec = importlib.machinery.ModuleSpec("tests", loader=None, is_package=True)
    spec.submodule_search_locations = [str(TESTS_DIR)]
    sys.modules["tests"] = importlib.util.module_from_spec(spec)


# Loaded as a pytest plugin (`-p tests_package_pin`), this is the whole point:
# it runs during plugin import, before any conftest.py. Loaded as a plain
# import from conftest.py, the call below has already happened by the time
# `pin_tests_package` is looked up -- which is why conftest.py calls it anyway
# rather than relying on import side effects being enough.
pin_tests_package()
