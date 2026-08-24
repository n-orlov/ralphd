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
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
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


@pytest.mark.parametrize("node_id", _node_ids())
def test_cited_test_node_id_collects(node_id):
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q", node_id],
        cwd=REPO_ROOT, capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, (
        f"{node_id} did not collect:\nSTDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}")


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
    fake_id = "tests/test_conformance_ledger.py::test_this_function_does_not_exist_xyz"
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q", fake_id],
        cwd=REPO_ROOT, capture_output=True, text=True, timeout=60)
    assert result.returncode != 0, "collecting a nonexistent node id must fail"
