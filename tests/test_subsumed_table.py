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

import os
import re
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


class _CollectPlugin:
    """Records every item pytest's own collector visits, in-process."""

    def __init__(self) -> None:
        self.node_ids: set[str] = set()

    def pytest_itemcollected(self, item):
        self.node_ids.add(item.nodeid)


_COLLECTED: tuple[set[str], int] | None = None


def _collected() -> tuple[set[str], int]:
    """Every node id the `tests/` tree collects, from ONE in-process pytest
    collection pass, memoised for the process.

    Task 050 (requirement G.2): this replaces one
    `python -m pytest --collect-only <node>` SUBPROCESS PER SUBSUMED ROW.
    That shape was affordable when the table had a handful of rows and
    unaffordable by the time D.1-D.7 and E had folded 343 nodes into feature
    modules: measured at `778738b`, this module alone was **302.73 s of the
    797.25 s serial `fast` lane** across 849 nodes (~0.36 s of interpreter
    startup each), and under `-n 8 --dist loadfile` -- which keeps a module
    on ONE worker -- that made it the parallel lane's whole critical path
    (322.38 s wall, with no single node over 11.29 s). Every later fold adds
    rows, so the cost was growing with the very work the audit records.

    Same technique, and the same reasoning, as `tests/records/
    test_conformance_ledger.py::_collect_node_ids` (task 035, E.4) which
    already retired the identical per-citation-subprocess shape:
    `RALPHD_SKIP_DOCKER_PROBE` keeps a transitively-imported module's
    import-time `docker version` probe out of a collect-only pass that never
    reads its answer, scoped to this call and restored after.
    """
    global _COLLECTED
    if _COLLECTED is None:
        plugin = _CollectPlugin()
        previous = os.environ.get("RALPHD_SKIP_DOCKER_PROBE")
        os.environ["RALPHD_SKIP_DOCKER_PROBE"] = "1"
        try:
            ret = pytest.main(
                ["-q", "--collect-only", "-p", "no:cacheprovider",
                 "-p", "no:randomly", str(REPO_ROOT / "tests")],
                plugins=[plugin])
        finally:
            if previous is None:
                os.environ.pop("RALPHD_SKIP_DOCKER_PROBE", None)
            else:
                os.environ["RALPHD_SKIP_DOCKER_PROBE"] = previous
        _COLLECTED = (plugin.node_ids, int(ret))
    return _COLLECTED


def _collects(node_id: str) -> bool:
    """Does `node_id` name something the `tests/` tree actually collects?

    A cited id is satisfied exactly, or by any of its parametrisations
    (`<node_id>[<param>]`) -- the same prefix semantics the retired per-node
    subprocess got for free from pytest's own node-id argument matching
    (`_satisfied` in `tests/records/test_conformance_ledger.py`).
    """
    collected, _ret = _collected()
    return node_id in collected or any(
        c.startswith(node_id + "[") for c in collected)


def test_subsumed_table_has_rows():
    rows = _rows()
    assert rows, "docs/test-suite-audit.md's Subsumed table has no parsed rows"


def test_the_collection_this_module_checks_against_succeeded():
    """The load-bearing precondition of the two checks below: a collection
    that ERRORED would make every row look uncollectable (or, if it aborted
    early, silently shrink the set every row is checked against), so the
    exit code is asserted on its own rather than folded into a row failure.
    """
    collected, ret = _collected()
    assert ret == int(pytest.ExitCode.OK), (
        f"in-process collection of the tests/ tree failed (exit code {ret})")
    assert len(collected) > 1000, (
        f"collection returned only {len(collected)} node ids -- too few for "
        "this tree, so the membership check below would be vacuous")


def test_every_surviving_node_collects():
    """One assertion over every row (was: one parametrised node, and one
    `--collect-only` subprocess, per row -- see `_collected`). Every row that
    fails is named, rather than the first one only."""
    rows = _rows()
    assert rows, "docs/test-suite-audit.md's Subsumed table has no parsed rows"
    broken = [(deleted, surviving) for deleted, surviving in rows
              if not _collects(surviving)]
    assert not broken, (
        "Subsumed rows citing a surviving node that does not collect:\n"
        + "\n".join(f"  row for {d!r} cites {s!r}" for d, s in broken))


def test_mutation_a_nonexistent_surviving_node_is_caught():
    """Proves `test_every_surviving_node_collects` is not vacuously green: a
    surviving-node cell rewritten to name a test that does not exist must
    fail the SAME predicate the real check uses."""
    fake_id = "tests/test_subsumed_table.py::test_this_function_does_not_exist_xyz"
    assert not _collects(fake_id), \
        "a nonexistent node id must not count as collected"


def test_mutation_a_row_pointed_at_a_deleted_node_itself_is_caught():
    """A second flavour of the same failure mode: the surviving-node cell
    mutated to cite the DELETED node from its own row (the thing that no
    longer exists, by construction) must also fail to collect -- proving
    the check would catch a row whose two cells got swapped or a row that
    forgot to update its surviving node after a later rename."""
    rows = _rows()
    assert rows, "no rows available to build the mutation case from"
    deleted, _surviving = rows[0]
    assert not _collects(deleted), (
        f"the deleted node {deleted!r} unexpectedly still collects -- "
        "either it was never actually removed, or a same-named replacement "
        "landed elsewhere; either way this mutation case needs a different "
        "row to stay meaningful")
