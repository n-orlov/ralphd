"""Directory-scoped import setup for `tests/records/` (task 032, requirement
E.1).

Every records-lint module living directly under `tests/` imports its shared
helper module (`report_claims`, and later the conformance-ledger/roadmap
support modules E.2/E.3 add) by bare name -- `import report_claims` -- which
resolves because pytest's default import mode inserts a collected module's
*own directory* onto `sys.path` when that directory has no `__init__.py`.
For a module directly under `tests/`, that inserted directory IS `tests/`,
so the bare import finds `tests/report_claims.py` right there.

A module one level down, under `tests/records/`, gets `tests/records`
inserted instead -- a directory that does not contain those helpers. Rather
than change every records module's import style (and diverge from how
`tests/test_issue_traceability.py` and `tests/sweep_sha_guard.py` still
import the very same module), this conftest adds the parent `tests/`
directory to `sys.path` once, at collection time, so the bare imports the
records modules copy from their `tests/` originals keep working unchanged.
"""

from __future__ import annotations

import sys
from pathlib import Path

_TESTS_DIR = Path(__file__).resolve().parent.parent
if str(_TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(_TESTS_DIR))
