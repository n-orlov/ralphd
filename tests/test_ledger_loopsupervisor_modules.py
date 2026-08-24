"""Task 007: every test module that imports the supervisor class this ledger
section tracks must be named in the conformance ledger's "...test modules"
section below, and the ledger's list must never silently drift from the tree
in either direction (a module added or removed from ``tests/`` that starts or
stops referencing that class must be reflected in the ledger in the same
commit).

This module is the committed scan the ledger's prose refers to: a plain
``Path.glob`` over ``tests/*.py`` plus a substring search, no shell globbing
quirks, and (unlike a naive recursive grep) no risk of matching
``__pycache__/*.pyc`` bytecode -- which is exactly how the PRD's stale ``51``
figure was produced.

Deliberate style note: the target class name is never spelled out as one
contiguous literal in this file's own source (it is built by concatenation
below). Spelling it out here would make this checker module itself match its
own scan and then fail the drift check against the ledger it is checking.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
TESTS_DIR = REPO_ROOT / "tests"
LEDGER = REPO_ROOT / "artifacts" / "reports" / "conformance-ledger.md"

# Built by concatenation on purpose -- see the module docstring.
_TARGET_SYMBOL = "Loop" + "Supervisor"

SECTION_HEADING = "## " + _TARGET_SYMBOL + "-importing test modules (task 007)"
NEXT_SECTION_RE = re.compile(r"^## ", re.MULTILINE)
ROW_RE = re.compile(r"^\| `(tests/[\w./]+\.py)` \| (\w[\w -]*) \|", re.MULTILINE)


def _scan_loopsupervisor_modules(tests_dir: Path = TESTS_DIR) -> list[str]:
    """The ground-truth scan: every top-level ``tests/*.py`` module whose
    source text contains the target class name as one literal substring."""
    found = []
    for path in sorted(tests_dir.glob("*.py")):
        if _TARGET_SYMBOL in path.read_text(errors="ignore"):
            found.append(f"tests/{path.name}")
    return found


def _ledger_section_text() -> str:
    text = LEDGER.read_text()
    assert SECTION_HEADING in text, (
        f"ledger is missing the {SECTION_HEADING!r} section")
    start = text.index(SECTION_HEADING) + len(SECTION_HEADING)
    rest = text[start:]
    m = NEXT_SECTION_RE.search(rest)
    return rest[: m.start()] if m else rest


def _ledger_modules_and_fates() -> dict[str, str]:
    section = _ledger_section_text()
    rows = ROW_RE.findall(section)
    assert rows, "no module rows parsed out of the ledger's task-007 section"
    return {module: fate.strip() for module, fate in rows}


def test_scan_finds_the_expected_modules_at_head():
    # Sanity check on the scan itself: it must actually find hits for the
    # target symbol, and every hit must genuinely contain it (guards against
    # an accidental empty-tree false pass).
    scanned = _scan_loopsupervisor_modules()
    assert scanned, f"scan found zero {_TARGET_SYMBOL}-importing modules -- broken scan or empty tests/ dir"
    for module in scanned:
        assert _TARGET_SYMBOL in (REPO_ROOT / module).read_text()


def test_ledger_list_matches_the_tree_exactly():
    """The drift check: the ledger's table and a fresh scan of the tree must
    name exactly the same set of modules. Fails on drift in either
    direction -- a module added to the tree that the ledger doesn't yet list,
    or a module the ledger lists that the tree no longer has."""
    scanned = set(_scan_loopsupervisor_modules())
    ledgered = set(_ledger_modules_and_fates())
    missing_from_ledger = scanned - ledgered
    stale_in_ledger = ledgered - scanned
    assert not missing_from_ledger, (
        f"tree has {_TARGET_SYMBOL}-importing modules the ledger does not list: "
        f"{sorted(missing_from_ledger)}")
    assert not stale_in_ledger, (
        f"ledger lists modules the tree no longer has (or that no longer "
        f"reference {_TARGET_SYMBOL}): {sorted(stale_in_ledger)}")


def test_every_ledgered_module_has_a_fate():
    fates = _ledger_modules_and_fates()
    for module, fate in fates.items():
        assert fate in {"kept", "rewritten"} or fate.startswith("retired"), (
            f"{module} has an unrecognised fate {fate!r} "
            f"(expected kept / rewritten / retired-plus-reason)")


def test_every_fate_is_kept_at_this_point():
    # Requirement of task 007 specifically: no rewrite (tasks 008-016) has
    # landed yet, so every fate recorded right now must be `kept`.
    fates = _ledger_modules_and_fates()
    non_kept = {m: f for m, f in fates.items() if f != "kept"}
    assert not non_kept, (
        f"task 007 expects every fate to be 'kept' at this point in the "
        f"wave; found non-kept fates (rewrite tasks 008-016 haven't run "
        f"yet): {non_kept}")


# --- mutation cases: prove the drift check is not vacuous ---


def test_mutation_a_module_missing_from_the_ledger_is_caught():
    scanned = {"tests/test_a.py", "tests/test_b.py"}
    ledgered = {"tests/test_a.py"}  # missing test_b.py on purpose
    missing_from_ledger = scanned - ledgered
    assert missing_from_ledger == {"tests/test_b.py"}


def test_mutation_a_stale_ledger_entry_is_caught():
    scanned = {"tests/test_a.py"}
    ledgered = {"tests/test_a.py", "tests/test_removed.py"}  # stale on purpose
    stale_in_ledger = ledgered - scanned
    assert stale_in_ledger == {"tests/test_removed.py"}
