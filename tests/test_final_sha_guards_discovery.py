"""Task 003 (requirement B, #103): `scripts/final_sha_guards.sh` selects the
docs-/README-/SPEC-reading guard subset by grepping `tests/` at run time,
never from a list typed into the script. v0.13 approach 3 task 012 hand-typed
a module list for the same job, omitted one, and the task exhausted its
validation ladder ("failed") -- this module exists so that failure mode can
never come back silently.

Three things are checked:

* the script's own source contains no hand-maintained module list -- no
  literal `test_<name>.py` filename anywhere in it, only the wildcard
  (`test_*.py`) the discovery grep uses to scan the tree fresh each run;
* the selection equals what an *independent* reimplementation of the rule
  ("a test module anywhere under `tests/` that mentions `docs`, `README` or
  `SPEC` as a word") finds by scanning `tests/` here in Python -- a rule comparison, so this module
  does not type a module name either;
* discovery genuinely runs at invocation time: two brand-new test modules,
  planted here, are picked up by `--list` with *no* edit to the script --
  one that reads `docs/` by slash-form path, and one that reads
  `docs/architecture.md` by *split* path components (`REPO_ROOT / "docs" /
  "architecture.md"`, no literal `docs/` in its source), which is the shape
  the first version of this script silently missed.
"""

from __future__ import annotations

import re
import subprocess
import uuid
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "final_sha_guards.sh"

# Any bare `test_<name>.py` mention is a hand-typed module reference. The
# discovery glob itself (`test_*.py`) is the one permitted exception.
_MODULE_LITERAL_RE = re.compile(r"\btest_[A-Za-z0-9_]+\.py\b")


def test_script_exists_and_is_executable() -> None:
    assert SCRIPT.is_file(), f"{SCRIPT} is missing"
    assert SCRIPT.stat().st_mode & 0o111, f"{SCRIPT} is not executable"


def test_script_embeds_no_hand_maintained_module_list() -> None:
    text = SCRIPT.read_text()
    offenders = [
        m.group(0) for m in _MODULE_LITERAL_RE.finditer(text) if m.group(0) != "test_*.py"
    ]
    assert not offenders, (
        "scripts/final_sha_guards.sh names a specific test module literally "
        f"({offenders}) -- discovery must be the only mechanism"
    )


def _list_selected() -> list[str]:
    proc = subprocess.run(
        [str(SCRIPT), "--list"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    return [line for line in proc.stdout.splitlines() if line.strip()]


def test_known_docs_reading_modules_are_already_selected() -> None:
    """The selection equals an independent scan of `tests/` for the same rule.

    Comparing against the whole rule, recomputed here, cannot pass with an
    incomplete selection -- and it still types no module name. The scan is
    recursive, like the script's own grep, so a docs-reading module in a
    subdirectory (`tests/flows/`, for example) is a guard too.
    """
    rule = re.compile(r"\b(docs|README|SPEC)\b")
    expected = {
        path.relative_to(REPO_ROOT).as_posix()
        for path in sorted((REPO_ROOT / "tests").rglob("test_*.py"))
        if rule.search(path.read_text())
    }
    assert expected, "the independent scan found nothing -- the rule is broken"
    assert set(_list_selected()) == expected


def _plant(source: str) -> tuple[str, Path]:
    name = f"test_zzz_tmp_discovery_proof_{uuid.uuid4().hex[:8]}.py"
    path = REPO_ROOT / "tests" / name
    path.write_text(source)
    return name, path


def test_discovery_picks_up_a_module_that_reads_docs_via_split_path_components() -> None:
    """The gap the first version of the script had: a module whose source
    never contains the literal string `docs/`, only the path component
    `"docs"`, still reads repository docs and must be selected."""
    before = set(_list_selected())
    source = (
        '"""Throwaway module planted by '
        "test_discovery_picks_up_a_module_that_reads_docs_via_split_path_components; "
        'deleted at the end of that test."""\n'
        "\n"
        "from pathlib import Path\n"
        "\n"
        "REPO_ROOT = Path(__file__).resolve().parents[1]\n"
        'ARCH = REPO_ROOT / "docs" / "architecture.md"\n'
        "\n"
        "\n"
        "def test_the_architecture_doc_is_readable() -> None:\n"
        "    assert ARCH.read_text().strip()\n"
    )
    assert "docs/" not in source, "the fixture must not contain the slash form"
    new_name, new_path = _plant(source)
    try:
        after = set(_list_selected())
    finally:
        new_path.unlink()

    assert f"tests/{new_name}" in after, (
        "a brand-new module that reads docs/architecture.md via split path "
        "components was not picked up by scripts/final_sha_guards.sh --list"
    )
    assert after - before == {f"tests/{new_name}"}


def test_discovery_picks_up_a_brand_new_docs_reading_module_with_no_list_edit() -> None:
    """The acceptance fixture the PRD asks for: plant a new module that reads
    `docs/`, change nothing else, and the very next `--list` run must name it
    -- proving the set is computed by grepping tests/ at run time, not typed
    or cached anywhere."""
    before = set(_list_selected())
    new_name = f"test_zzz_tmp_discovery_proof_{uuid.uuid4().hex[:8]}.py"
    new_path = REPO_ROOT / "tests" / new_name
    assert new_name not in before

    new_path.write_text(
        '"""Throwaway module planted by '
        "test_discovery_picks_up_a_brand_new_docs_reading_module_with_no_list_edit "
        'to prove grep-time discovery; deleted at the end of that test. Reads '
        "docs/ off disk below.\"\"\"\n"
        "\n"
        "from pathlib import Path\n"
        "\n"
        "REPO_ROOT = Path(__file__).resolve().parents[1]\n"
        "\n"
        "\n"
        "def test_docs_directory_is_readable() -> None:\n"
        '    assert (REPO_ROOT / "docs/").is_dir()\n'
    )
    try:
        after = set(_list_selected())
    finally:
        new_path.unlink()

    assert f"tests/{new_name}" in after, (
        "a brand-new module that reads docs/ was not picked up by "
        "scripts/final_sha_guards.sh --list with no edit to any list"
    )
    assert after - before == {f"tests/{new_name}"}
