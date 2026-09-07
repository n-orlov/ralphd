"""Task 018 (#67/#83 families): `tests/conftest.py`'s autouse
`_isolate_ambient_home_in_process` now pins `PYTHONUSERBASE` alongside
`HOME`, to the same `_USER_BASE` `with_ralphd_importable()` already derives
from the real ambient HOME at import -- not a second, independent
derivation that could drift from it.

Without this, an in-process test that overrides `HOME` (the fixture is
autouse, so this is every test by default) moves *user* site-packages on a
`pip install --user -e .` layout, the in-process shape of #67's defect: any
import that happens to resolve through user site AFTER the override took
effect would fail, not because the code is wrong but because the test
harness quietly relocated the interpreter's own dependency shelf out from
under it.

Two directions, mirroring the one opt-out the fixture already has for HOME
itself (the `AMBIENT_HOME` marker):

  * an ordinary test (no marker): PYTHONUSERBASE is pinned to `_USER_BASE`;
  * an `AMBIENT_HOME`-marked test: PYTHONUSERBASE is left exactly as the
    real ambient environment had it (absent or present, whichever it was
    *before* pytest ever ran) -- captured at module import time, i.e.
    before any test body (and therefore before any fixture) has had a
    chance to mutate `os.environ` for this session, and restored between
    tests by `monkeypatch` regardless.
"""

from __future__ import annotations

import os

import pytest

from tests.conftest import _USER_BASE

# xdist_group (task 003, G.3): an AMBIENT_HOME-family module -- keeps this
# module's tests on one worker under a loaded parallel (`-n 8`) run rather
# than racing another AMBIENT_HOME/nested-pytest module over the real
# ambient $HOME on a different worker.
pytestmark = pytest.mark.xdist_group(name="ambient_home")

# Captured at collection time -- before any test function has run, so
# before this fixture (or any other) has touched os.environ this session.
# `monkeypatch` reverts its own edits after every test, so this is the same
# value an AMBIENT_HOME-marked test's fixture-skipped `os.environ` will show.
_AMBIENT_PYTHONUSERBASE_AT_COLLECTION = os.environ.get("PYTHONUSERBASE")


def test_ordinary_test_gets_pythonuserbase_pinned_by_the_fixture():
    assert os.environ.get("PYTHONUSERBASE") == _USER_BASE


@pytest.mark.AMBIENT_HOME
def test_ambient_home_marked_test_keeps_its_own_pythonuserbase_untouched():
    assert os.environ.get("PYTHONUSERBASE") == _AMBIENT_PYTHONUSERBASE_AT_COLLECTION, (
        "the AMBIENT_HOME marker opts a test out of the whole fixture "
        "(HOME *and* PYTHONUSERBASE alike) -- if this fails, the fixture "
        "pinned PYTHONUSERBASE even though the marker was present"
    )
