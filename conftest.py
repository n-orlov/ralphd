"""Root-level conftest, loaded by pytest before it collects (and therefore
before it imports) anything under `tests/`.

Its whole job is to make the name `tests` mean *this* repository's `tests/`
directory. **The mechanism, the measurements and the reasoning all live in
`tests_package_pin.py`** -- read that file, not this one. It is a separate
module rather than code here because a conftest is only loaded for invocations
that collect something under this repository, and the guarantee has to hold for
the ones that do not (`scripts/host_like_pytest.sh` runs an out-of-tree probe;
it loads that module with `-p tests_package_pin` instead). Keeping the rule
conftest-only was the original bug's own shape one level out: a guarantee that
held for the invocation it was written for.

Task 005 (#50), mechanism corrected in #64. Tested by
tests/test_tests_conftest_resolution.py.
"""

from __future__ import annotations

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent

# Keep this repository's root first on `sys.path`. This is NOT the `tests`
# guarantee -- see tests_package_pin.py: a regular `tests` package anywhere on
# `sys.path` beats a namespace portion at position 0, so ordering was never the
# lever. It stays for other repo-rooted absolute imports (and so that
# `import tests_package_pin` below works however pytest was invoked),
# unconditional so the invariant does not depend on pytest's own prepend-mode
# bookkeeping.
_root_str = str(_REPO_ROOT)
if _root_str in sys.path:
    sys.path.remove(_root_str)
sys.path.insert(0, _root_str)

from tests_package_pin import pin_tests_package  # noqa: E402  (needs sys.path above)

# Called explicitly rather than left to the module's own import-time call, so
# that this file states the guarantee it is responsible for instead of relying
# on an import side effect -- and so it still holds if the module was already
# imported (as a `-p` plugin) before this conftest loaded.
pin_tests_package()
