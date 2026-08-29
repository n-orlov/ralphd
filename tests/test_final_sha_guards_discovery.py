"""Task 003 (requirement B, #103): `scripts/final_sha_guards.sh` selects the
docs-/report-/README-reading guard subset by grepping `tests/` at run time,
never from a list typed into the script. v0.13 approach 3 task 012 hand-typed
a module list for the same job, omitted one, and the task exhausted its
validation ladder ("failed") -- this module exists so that failure mode can
never come back silently.

Two things are checked:

* the script's own source contains no hand-maintained module list -- no
  literal `test_<name>.py` filename anywhere in it, only the wildcard glob
  (`tests/test_*.py`) the discovery grep uses to scan the directory fresh
  each run;
* discovery genuinely runs at invocation time: a brand-new test module,
  planted here and reading `docs/`, is picked up by `--list` with *no* edit
  to the script -- proving the selection is computed, not cached or typed.
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
    """Sanity: the two modules the PRD names as the trivial subset
    (`docs/prds/v0.14-every-hand-on-the-plan.md:218-225`) are already picked
    up today, with no help from this test."""
    selected = set(_list_selected())
    assert "tests/test_docs_consistency.py" in selected
    assert "tests/test_docs_prd_index.py" in selected


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
