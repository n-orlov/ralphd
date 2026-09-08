"""Task 005 (PRD requirement F): pin the `ralphctl` process-boundary module
set BEHAVIOURALLY, off the test sources themselves, instead of a
`grep -c 'RALPHCTL' tests/` count.

A module is at the real process boundary if its OWN source (not a fixture
it merely consumes, and not a name it imports only for a type hint) contains
either

* an unoverridden `<obj>.ralphctl(...)` call site -- `LiveRun.ralphctl()`,
  the shared subprocess-spawning method every CLI fixture used to call
  directly before task 002/003/004's in-process conversions; a module that
  reassigns `run.ralphctl = <closure>` before calling it has overridden the
  method with an in-process stand-in and does not count, or
* a spawn argv naming the resolved `RALPHCTL` path -- `subprocess.run([str(
  RALPHCTL), ...])` and its kin.

`_spawns()` below is that rule as an AST walk over one module's own parsed
source; nothing here follows an import into another module's body. That is
deliberate: the known false-positive shape task 004/005 both hit by hand is
an explicit `from tests.conftest import LiveRun` (or `ui`, `hub_in_process`,
`ctl`) written for a type hint or an unused re-export -- conftest fixtures
resolve to every test module WITHOUT an import, so an import site proves
nothing about whether THIS module's own tests spawn anything, and a
transitive walk into the imported name's defining module (chasing `LiveRun`
into `conftest.py`'s constructor, say) flags every module that so much as
names the class. Restricting the walk to each module's own top-level source
avoids that trap entirely.

Two node ids apply the rule and pin its answer:

* `test_the_cli_scope_process_boundary_is_exactly_the_named_allowlist` --
  the `tests/test_cli*.py` family (the one requirement 7 converts to
  in-process `main()` + capsys) against `CLI_SCOPE_ALLOWLIST` below, at
  most 8 entries per the PRD's cap on requirement F.
* `test_the_tree_wide_process_boundary_is_exactly_the_named_allowlist` --
  every `tests/test_*.py` module against `TREE_WIDE_ALLOWLIST` below; every
  member of `CLI_SCOPE_ALLOWLIST` is also a member of this one, plus the
  modules elsewhere in the tree whose own subject is a real subprocess
  contract (`docs/test-suite-audit.md`'s requirement-7 section names each
  one's reason).

Two mutation tests prove both checks can fail: dropping one real member
from each frozen allowlist must turn the corresponding node red.
"""

from __future__ import annotations

import ast
import glob
import os
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
TESTS_DIR = REPO_ROOT / "tests"

# The four members ruled legitimate boundary modules by operator ruling
# (task 001, steer 009 section 4) plus the two task 004 left at the
# boundary on purpose: test_cli_logs_resumed_run.py (a real streaming
# follow across an engine resume) and test_cli_ui.py (hosts the `UiServer`
# class ~15 external modules construct directly for real subprocess
# coverage).
CLI_SCOPE_ALLOWLIST = frozenset({
    "test_cli_logs_pty_termios.py",
    "test_cli_logs_signal.py",
    "test_cli_watch_resumed_run.py",
    "test_cli_logs_follow_liveness.py",
    "test_cli_logs_resumed_run.py",
    "test_cli_ui.py",
})

# Every CLI_SCOPE_ALLOWLIST member, plus the modules elsewhere in the tree
# whose own subject is a real ralphctl subprocess contract that the CLI
# in-process conversion (requirement 7) never targeted -- each one's reason
# is named in docs/test-suite-audit.md's requirement-7 section.
TREE_WIDE_ALLOWLIST = CLI_SCOPE_ALLOWLIST | frozenset({
    "test_debt_ledger.py",
    "test_docker_sibling_e2e.py",
    "test_docs_semantics.py",
    "test_durations.py",
    "test_engine_child_import_path.py",
    "test_hub_artifacts.py",
    "test_hub_cost_dialog.py",
    "test_hub_delete.py",
    "test_hub_fault_dialog.py",
    "test_hub_iteration_dialog.py",
    "test_hub_run_documents.py",
    "test_image_build.py",
    "test_image_packaged_inputs.py",
    "test_image_recorded.py",
    "test_no_transcript_message.py",
    "test_operator_ruling.py",
    "test_ralphctl_resolution.py",
    "test_reflect_tombstone_cleared.py",
    "test_rm_force_finishing_docs.py",
    "test_sibling_cleanup_job_safe.py",
    "test_spec_cli_surface.py",
    "test_status_health_infra_wait.py",
    "test_status_max_approaches.py",
    "test_steering_consumed_on_success.py",
    "test_tasks_stale_cli.py",
    "test_tier_cost_breakdown.py",
})


def _overrides_ralphctl(tree: ast.Module) -> bool:
    """True if the module reassigns `<obj>.ralphctl = ...` anywhere -- an
    in-process stand-in that makes a `.ralphctl(...)` call site in the same
    module a no-op subprocess-wise."""
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Attribute) and target.attr == "ralphctl":
                    return True
    return False


def _spawns(path: Path) -> list[str]:
    """Reasons `path`'s OWN source spawns a real ralphctl process, or []."""
    tree = ast.parse(path.read_text(), str(path))
    why: list[str] = []
    calls = sorted({
        node.lineno for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "ralphctl"
    })
    if calls and not _overrides_ralphctl(tree):
        why.append(f"unoverridden .ralphctl() call at line(s) {calls}")
    argv = sorted({
        node.lineno for node in ast.walk(tree)
        if isinstance(node, ast.Name) and node.id == "RALPHCTL"
    })
    if argv:
        why.append(f"RALPHCTL argv at line(s) {argv}")
    return why


def _boundary_set(pattern: str) -> dict[str, list[str]]:
    """{module basename: reasons} for every module matching `pattern` under
    tests/ whose own source spawns a real ralphctl process."""
    out: dict[str, list[str]] = {}
    for p in sorted(glob.glob(str(TESTS_DIR / pattern))):
        why = _spawns(Path(p))
        if why:
            out[os.path.basename(p)] = why
    return out


def test_the_cli_scope_process_boundary_is_exactly_the_named_allowlist() -> None:
    derived = _boundary_set("test_cli*.py")
    assert set(derived) == CLI_SCOPE_ALLOWLIST, (
        f"derived CLI-scope boundary set {sorted(derived)} != "
        f"allowlist {sorted(CLI_SCOPE_ALLOWLIST)}; reasons: {derived}"
    )
    assert len(CLI_SCOPE_ALLOWLIST) <= 8, "PRD requirement F's cap"


def test_mutating_the_cli_scope_allowlist_fails_the_check() -> None:
    mutated = frozenset(CLI_SCOPE_ALLOWLIST - {"test_cli_ui.py"})
    derived = set(_boundary_set("test_cli*.py"))
    assert derived != mutated, (
        "a locally shrunk copy of the CLI-scope allowlist must not still "
        "match the derived set"
    )


def test_the_tree_wide_process_boundary_is_exactly_the_named_allowlist() -> None:
    derived = _boundary_set("test_*.py")
    assert set(derived) == TREE_WIDE_ALLOWLIST, (
        f"derived tree-wide boundary set {sorted(derived)} != "
        f"allowlist {sorted(TREE_WIDE_ALLOWLIST)}; reasons: {derived}"
    )
    assert CLI_SCOPE_ALLOWLIST <= TREE_WIDE_ALLOWLIST


def test_mutating_the_tree_wide_allowlist_fails_the_check() -> None:
    mutated = frozenset(TREE_WIDE_ALLOWLIST - {"test_debt_ledger.py"})
    derived = set(_boundary_set("test_*.py"))
    assert derived != mutated, (
        "a locally shrunk copy of the tree-wide allowlist must not still "
        "match the derived set"
    )
