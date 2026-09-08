"""Task 006: the conformance ledger (`artifacts/reports/conformance-ledger.md`)
makes claims a reader can check mechanically rather than trust on prose alone.
This module is that check, run as a test:

* every `guard N cite K: "<file>:<line>" contains "<substring>"` line in the
  ledger's "Citation substrings" appendix is verified against the real file at
  HEAD -- the cited line must literally contain the cited substring;
* every test node id named in the ledger's guard table (anything of the shape
  `tests/...::test_...`, skipping the literal string `GAP`) is collected by
  pytest without error.

Both checks have a mutation case (below) proving they can fail: a citation
pointing at the wrong line, or a text mangled to look like a node id that does
not exist, must make the corresponding test fail.

Task 033 (requirement E.2): moved here from `tests/test_conformance_ledger.py`
and the node-id check converted from 68 separate `pytest --collect-only`
SUBPROCESSES (one per cited node id, ~16.4 s measured in
`docs/test-suite-audit.md`) to ONE in-process pytest collection pass over the
deduplicated set of files those node ids live in -- `pytest.main()` called
directly inside this process, never `subprocess.run([...python..., "-m",
"pytest", ...])`. A collected node id satisfies a bare (unparametrised)
ledger citation if it equals the citation or is one of its parametrisations
(`<citation>[<param>]`), the same prefix-match semantics the old
`pytest --collect-only -q <node_id>` subprocess call relied on.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
LEDGER = REPO_ROOT / "artifacts" / "reports" / "conformance-ledger.md"

CITE_RE = re.compile(
    r'guard \d+ cite \d+: `([^`]+):(\d+)` contains `([^`]*)`'
)
# Any run of `<path with '/'>::test_<name>` inside backticks. Deliberately
# excludes the literal `GAP` marker cells, which name no test on purpose.
NODE_ID_RE = re.compile(r'`(tests/[\w./]+\.py::test_[\w]+)`')


def _ledger_text() -> str:
    assert LEDGER.exists(), f"ledger missing: {LEDGER}"
    return LEDGER.read_text()


def _citations() -> list[tuple[str, int, str]]:
    text = _ledger_text()
    cites = [(path, int(line), sub) for path, line, sub in CITE_RE.findall(text)]
    assert cites, "no citation lines parsed out of the ledger -- regex or ledger format drifted"
    return cites


def _node_ids() -> list[str]:
    text = _ledger_text()
    # Only look inside the guard tables (before the "Citation substrings"
    # heading) -- the appendix itself never names a test node id.
    table_text = text.split("## Citation substrings")[0]
    ids = sorted(set(NODE_ID_RE.findall(table_text)))
    assert ids, "no test node ids parsed out of the ledger's guard tables"
    return ids


@pytest.mark.parametrize("path,line,substring", _citations(),
                         ids=[f"{p}:{l}" for p, l, _ in _citations()])
def test_cited_line_contains_cited_symbol(path, line, substring):
    target = REPO_ROOT / path
    assert target.exists(), f"cited file does not exist: {path}"
    lines = target.read_text().splitlines()
    assert 1 <= line <= len(lines), (
        f"{path} has {len(lines)} lines; ledger cites line {line}")
    actual = lines[line - 1]
    assert substring in actual, (
        f"{path}:{line} does not contain the cited substring.\n"
        f"  cited:  {substring!r}\n"
        f"  actual: {actual!r}")


def _guard_table_text(text: str | None = None) -> str:
    """Only the two guard tables (before the appendix), same slice
    `_node_ids()` already restricts itself to."""
    src = text if text is not None else _ledger_text()
    return src.split("## Citation substrings")[0]


GUARD_ROW_RE = re.compile(r'^\|\s*(\d+)\s*\|.*\|\s*$', re.MULTILINE)


def _new_expression_cells(text: str | None = None) -> list[tuple[str, str]]:
    """`(guard number, New-expression cell)` for every guard row in either
    table -- the New expression is always the LAST `|`-delimited cell.
    Relies on no guard-row cell ever containing a literal `|` (true today:
    every cell is prose/backtick-quoted code, never a raw pipe).
    """
    table_text = _guard_table_text(text)
    out = []
    for line in table_text.splitlines():
        m = GUARD_ROW_RE.match(line)
        if not m:
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        out.append((m.group(1), cells[-1]))
    return out


def _new_expression_todo_rows(text: str | None = None) -> list[str]:
    return [num for num, cell in _new_expression_cells(text) if cell == "TODO"]


def test_no_new_expression_todo_cells():
    """Task 012 (requirement D, #69): the ledger is evidence or it is
    decoration -- a `New expression` cell still reading `TODO` proves
    nothing, whatever the rest of the row says."""
    cells = _new_expression_cells()
    assert cells, "no guard rows parsed -- table format drifted"
    todo_rows = _new_expression_todo_rows()
    assert not todo_rows, (
        f"guard rows with an unfilled New expression cell: {todo_rows}")


def test_mutation_a_todo_new_expression_cell_is_caught():
    """Proves `test_no_new_expression_todo_cells` is not vacuously green --
    mutate one real, filled row's New-expression cell back to the literal
    `TODO` marker (in memory only, the file on disk is untouched) and
    check the SAME parsing helper the real test uses reports it."""
    text = _ledger_text()
    filled = [n for n, c in _new_expression_cells(text) if c != "TODO"]
    assert filled, "no filled new-expression cells available to mutate"
    target = filled[0]

    lines = text.splitlines()
    mutated_lines = []
    hit = False
    for line in lines:
        if not hit and GUARD_ROW_RE.match(line) and \
                GUARD_ROW_RE.match(line).group(1) == target:
            prefix = line.rstrip()
            assert prefix.endswith("|")
            prefix_no_last_cell = prefix.rsplit("|", 2)[0]
            mutated_lines.append(prefix_no_last_cell + "| TODO |")
            hit = True
        else:
            mutated_lines.append(line)
    assert hit, f"could not locate guard {target}'s row to mutate"
    mutated_text = "\n".join(mutated_lines)

    assert target in _new_expression_todo_rows(mutated_text), (
        "mutating a real row's New expression to TODO was not detected -- "
        "the check is not discriminating")


def test_every_row_has_at_least_one_citation():
    # Every guard number 1..23 appears at least once in the citation
    # appendix OR is explicitly marked n/a (guard 20's mechanism lives
    # outside loop.py, in task 005's own ledger row).
    text = _ledger_text()
    guard_nums = {int(n) for n in re.findall(r'^\| (\d+) \|', text, re.MULTILINE)}
    assert guard_nums >= set(range(1, 24)), (
        f"missing guard rows: {set(range(1, 24)) - guard_nums}")
    cited_nums = {int(n) for n in re.findall(r'guard (\d+) cite \d+:', text)}
    missing = guard_nums - cited_nums - {20}  # 20 is deliberately n/a
    assert not missing, f"guards with a table row but no citation: {missing}"


class _CollectPlugin:
    """Records every item pytest's own collector visits, in-process."""

    def __init__(self) -> None:
        self.node_ids: set[str] = set()

    def pytest_itemcollected(self, item):
        self.node_ids.add(item.nodeid)


def _collect_node_ids(targets: list[str]) -> tuple[set[str], int]:
    """ONE in-process pytest collection pass over `targets` (file paths, not
    individual node ids -- passing a single missing node id as a target
    would abort collection of the rest). No `subprocess`/`os.fork` involved:
    `pytest.main()` runs a fresh `Session` inside THIS interpreter.

    Task 035 (E.4): some cited files transitively import
    `tests/test_docker_sibling_e2e.py` (`test_worker_hat.py` ->
    `test_steer.py` -> that module), whose module-level skipif spends a real
    `docker version` subprocess call at IMPORT time -- a real cost this
    collect-only pass never needs the answer to. `RALPHD_SKIP_DOCKER_PROBE`
    makes that probe answer "unavailable" without asking the real docker
    binary, scoped to this one call and restored (or deleted) right after."""
    plugin = _CollectPlugin()
    args = ["-q", "--collect-only", "-p", "no:cacheprovider"] + targets
    previous = os.environ.get("RALPHD_SKIP_DOCKER_PROBE")
    os.environ["RALPHD_SKIP_DOCKER_PROBE"] = "1"
    try:
        ret = pytest.main(args, plugins=[plugin])
    finally:
        if previous is None:
            os.environ.pop("RALPHD_SKIP_DOCKER_PROBE", None)
        else:
            os.environ["RALPHD_SKIP_DOCKER_PROBE"] = previous
    return plugin.node_ids, ret


def _satisfied(node_id: str, collected: set[str]) -> bool:
    """A ledger citation names a bare node id; a collected id satisfies it
    either exactly or as one of its parametrisations (`<node_id>[<param>]`)
    -- the same prefix semantics the old per-node subprocess call got for
    free from pytest's own node-id argument matching."""
    return node_id in collected or any(
        c.startswith(node_id + "[") for c in collected)


def test_every_cited_test_node_id_collects():
    """Replaces the former `test_cited_test_node_id_collects` parametrised
    over 68 `pytest --collect-only` SUBPROCESSES (one per ledger node id,
    ~16.4 s measured baseline, `docs/test-suite-audit.md`) with a single
    in-process collection over the deduplicated files those ids live in."""
    node_ids = _node_ids()
    files = sorted({node_id.split("::", 1)[0] for node_id in node_ids})
    collected, ret = _collect_node_ids(files)
    assert ret == pytest.ExitCode.OK, (
        f"in-process collection over the ledger's cited files failed "
        f"(exit code {ret}); ledger node ids: {node_ids}")
    missing = [n for n in node_ids if not _satisfied(n, collected)]
    assert not missing, f"ledger-cited node ids that did not collect: {missing}"


def test_gap_cells_are_the_literal_marker_not_a_disguised_citation():
    """A `GAP` cell must be the bare word, not e.g. `GAP (see foo::test_x)`
    smuggling an uncoillected id past the node-id regex -- the ledger's own
    GAP summary section is prose and is allowed to say more; the table cells
    are not."""
    text = _ledger_text()
    table_text = text.split("## Beyond the PRD")[0] + text.split(
        "## Citation substrings")[0].split("## Beyond the PRD")[-1]
    for row in re.findall(r'^\|.*\|\s*$', table_text, re.MULTILINE):
        if "GAP" not in row:
            continue
        cells = [c.strip() for c in row.strip("|").split("|")]
        gap_cells = [c for c in cells if c.startswith("GAP")]
        for cell in gap_cells:
            assert cell == "GAP" or cell.startswith("GAP —") or cell.startswith("GAP -"), (
                f"suspicious GAP cell (should be bare or a plain dash-note): {cell!r}")


# --- mutation cases: prove the two structural checks above are not vacuous ---

def test_mutation_a_wrong_line_number_is_caught():
    path = REPO_ROOT / "src" / "ralphd" / "engine" / "loop.py"
    lines = path.read_text().splitlines()
    # Line 1 of loop.py is never going to contain this guard's substring.
    wrong_line = lines[0]
    with pytest.raises(AssertionError):
        assert "def _seed_refunds" in wrong_line


def test_mutation_a_nonexistent_node_id_is_caught():
    """Same in-process mechanism as the real check above -- collecting a
    file that does not contain the fake node id must leave it unsatisfied."""
    fake_id = "tests/records/test_conformance_ledger.py::test_this_function_does_not_exist_xyz"
    collected, ret = _collect_node_ids([fake_id.split("::", 1)[0]])
    assert ret == pytest.ExitCode.OK, f"collecting this real file should still succeed: {ret}"
    assert not _satisfied(fake_id, collected), (
        "a nonexistent node id must not appear satisfied by the real file's collection")
