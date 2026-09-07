"""Task 006 (requirement G.9): `docs/test-suite-audit.md`'s "Subsumed" table
claims one row per test node deleted since the v0.19 launch sha (`4c003a5`),
naming the surviving node that now covers what the deleted one pinned. That
claim is only worth anything if a reader can check it mechanically -- this
module is that check.

Git-derived list this table's rows were built from (task 006): diff every
`-def test_`/`+def test_` line out of `git diff 4c003a5 HEAD -- tests/` and
keep the removed names that never reappear as an added name anywhere in the
same diff (a same-name signature change, e.g. gaining a fixture argument, is
NOT a removal -- `test_api_token_auth` and
`test_iteration_environment_has_no_control_plane_coords` both gained a
`monkeypatch` parameter and are excluded on exactly that basis). That leaves
exactly two genuinely deleted nodes as of this table's construction:
`test_a_distinct_blocker_signature_keeps_the_remaining_approaches` (was in
`tests/test_stagnation_signature.py`) and
`test_three_ordinary_work_noop_iterations_still_fail_the_approach` (was in
`tests/test_stagnation_excludes_nonwork_faults.py`).

The check itself only resolves the SURVIVING node cited in each row against
the currently-collected test set (whether every genuinely deleted node has
a row at all is a per-task discipline the standing rules enforce, not
something this module can derive on its own without re-running that same
git diff at every invocation). The mutation case below proves the resolve
step is not vacuously green.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
AUDIT = REPO_ROOT / "docs" / "test-suite-audit.md"

# One row: "| `deleted node` | `surviving node` (prose...) | landed in |"
ROW_RE = re.compile(
    r'^\|\s*`([^`]+)`\s*\|\s*`([^`]+)`.*\|\s*[^|]+\|\s*$',
    re.MULTILINE,
)


def _audit_text() -> str:
    assert AUDIT.exists(), f"audit file missing: {AUDIT}"
    return AUDIT.read_text()


def _subsumed_table_text(text: str | None = None) -> str:
    src = text if text is not None else _audit_text()
    assert "## Subsumed" in src, "docs/test-suite-audit.md has no ## Subsumed section"
    return src.split("## Subsumed", 1)[1]


def _rows(text: str | None = None) -> list[tuple[str, str]]:
    """`(deleted node, surviving node)` for every data row in the Subsumed
    table (the `|---|---|---|` separator line never matches ROW_RE, since
    its cells contain no backticks)."""
    table_text = _subsumed_table_text(text)
    rows = ROW_RE.findall(table_text)
    return rows


def test_subsumed_table_has_rows():
    rows = _rows()
    assert rows, "docs/test-suite-audit.md's Subsumed table has no parsed rows"


@pytest.mark.parametrize("deleted,surviving", _rows(),
                         ids=[d for d, _ in _rows()])
def test_surviving_node_collects(deleted, surviving):
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q", surviving],
        cwd=REPO_ROOT, capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0, (
        f"Subsumed row for {deleted!r} cites a surviving node that does not "
        f"collect: {surviving!r}\nSTDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}")


def test_mutation_a_nonexistent_surviving_node_is_caught():
    """Proves `test_surviving_node_collects` is not vacuously green: a
    surviving-node cell rewritten to name a test that does not exist must
    fail to collect."""
    fake_id = "tests/test_subsumed_table.py::test_this_function_does_not_exist_xyz"
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q", fake_id],
        cwd=REPO_ROOT, capture_output=True, text=True, timeout=60,
    )
    assert result.returncode != 0, "collecting a nonexistent node id must fail"


def test_mutation_a_row_pointed_at_a_deleted_node_itself_is_caught():
    """A second flavour of the same failure mode: the surviving-node cell
    mutated to cite the DELETED node from its own row (the thing that no
    longer exists, by construction) must also fail to collect -- proving
    the check would catch a row whose two cells got swapped or a row that
    forgot to update its surviving node after a later rename."""
    rows = _rows()
    assert rows, "no rows available to build the mutation case from"
    deleted, _surviving = rows[0]
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q", deleted],
        cwd=REPO_ROOT, capture_output=True, text=True, timeout=60,
    )
    assert result.returncode != 0, (
        f"the deleted node {deleted!r} unexpectedly still collects -- "
        "either it was never actually removed, or a same-named replacement "
        "landed elsewhere; either way this mutation case needs a different "
        "row to stay meaningful")
