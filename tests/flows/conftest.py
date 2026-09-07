"""Directory-scoped fixtures for the stub-`pi` flow modules (task 005,
requirement G.5).

`tests/conftest.py` (one level up) supplies everything these flows actually
drive the engine with -- `engine_factory`/`live`, the notifier scrub, the
ambient-HOME isolation, the tier stamping. The only thing that belongs HERE is
the one precondition the parent's session-wide script check cannot cover: the
stub `pi` entrypoint is a plain repo file, not a resolved console script, so a
tree where it is missing or not executable must SKIP this directory rather
than error out of a fixture 15 s later (the narrower check
`tests/test_cure_pass_e2e_task023.py` already carries for the same reason).

The reading/assertion helpers live in `tests/flows/flowlib.py`, not here: a
conftest is imported under two module identities in an ordinary run (see
`tests/conftest.py`'s own note), and helpers a test module imports by name
should have exactly one.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

STUB_PI_ENTRY = Path(__file__).parent.parent / "stub-pi" / "pi"
STUB_PI_MISSING = "stub pi entrypoint (tests/stub-pi/pi) is absent or not executable"


@pytest.fixture(autouse=True)
def _require_stub_pi():
    if not STUB_PI_ENTRY.exists() or not os.access(STUB_PI_ENTRY, os.X_OK):
        pytest.skip(STUB_PI_MISSING)
