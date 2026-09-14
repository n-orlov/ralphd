"""Directory-scoped import setup for `tests/records/` (task 032, requirement
E.1).

Originally: every records-lint module living directly under `tests/`
imported its shared helper module (`report_claims`, and later the
conformance-ledger/roadmap support modules E.2/E.3 add) by bare name --
`import report_claims` -- which resolves because pytest's default import
mode inserts a collected module's *own directory* onto `sys.path` when that
directory has no `__init__.py`. For a module directly under `tests/`, that
inserted directory IS `tests/`, so the bare import found `tests/report_claims.py`
right there.

A module one level down, under `tests/records/`, gets `tests/records`
inserted instead -- a directory that does not contain those helpers. Rather
than change every records module's import style, this conftest adds the
parent `tests/` directory to `sys.path` once, at collection time, so bare
imports the records modules copy from their `tests/` originals keep working
unchanged.

Both bare-name importers this was written for are gone now --
`tests/sweep_sha_guard.py` (requirement A.1, v0.20.2) and `tests/report_claims.py`
plus its one caller `tests/test_issue_traceability.py` (requirement N.2, task
060) -- and nothing left under `tests/records/` does a bare `import <helper>`
(checked directly: no `test_*.py` module here imports anything but stdlib
and `pytest`). The `sys.path` insertion is kept anyway, dormant rather than
deleted, since removing it is no part of N.2's own job (stopping a read of
the report corpus) and a future records module reusing this same bare-import
style would need it back unchanged.
"""

from __future__ import annotations

import sys
from pathlib import Path

_TESTS_DIR = Path(__file__).resolve().parent.parent
if str(_TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(_TESTS_DIR))
