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
* a spawn argv naming the resolved `RALPHCTL` path *inside an actual
  process-spawning call* -- `subprocess.run([str(RALPHCTL), ...])`,
  `subprocess.Popen([str(RALPHCTL), ...])`, `check_output`/`check_call`/
  `call`, `asyncio.create_subprocess_exec`, `pty.spawn`, `os.spawnv*` and
  their kin. Merely *naming* `RALPHCTL` is not enough: the reference must
  sit in the argument subtree of one of those calls. That distinction is
  not hypothetical -- it is the false positive review caught in this
  module's first version: `tests/test_ralphctl_resolution.py` names
  `RALPHCTL` exactly once in executable code (`for script in (RALPHCTL,
  RALPHD_ENGINE): assert script.exists() and os.access(script, os.X_OK)`),
  imports no `subprocess` at all and spawns nothing, so it is NOT a
  process-boundary module however often the token appears in its prose.

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
from each frozen allowlist must turn the corresponding node red. A fifth
node is the classifier's own control pair: a synthetic module that only
asserts `RALPHCTL` exists and is executable is classified as NOT spawning,
while the same source with one `subprocess.run([str(RALPHCTL)])` added is.
"""

from __future__ import annotations

import ast
import glob
import os
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
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
# `test_ralphctl_resolution.py` is deliberately NOT a member: it names
# `RALPHCTL` only to assert the resolved path exists and is executable, and
# spawns no process at all (see the module docstring).
TREE_WIDE_ALLOWLIST = CLI_SCOPE_ALLOWLIST | frozenset({
    "test_debt_ledger.py",
    "test_deadline.py",
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


# Callables that actually start a child process. Matched on the attribute /
# function name only, so `subprocess.run(...)`, `sp.run(...)` and a bare
# `run(...)` imported from subprocess all match.
_SPAWN_ATTRS = frozenset({
    "run", "Popen", "call", "check_call", "check_output", "getoutput",
    "getstatusoutput", "spawn", "spawnv", "spawnve", "spawnvp", "spawnvpe",
    "spawnl", "spawnlp", "execv", "execve", "execvp", "execvpe",
    "create_subprocess_exec", "create_subprocess_shell", "fork_exec",
})
_SPAWN_NAMES = frozenset({"run", "Popen", "call", "check_call",
                          "check_output", "spawn"})


def _is_spawn_call(node: ast.AST) -> bool:
    """True if `node` is a call that starts a real child process."""
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    if isinstance(func, ast.Attribute):
        return func.attr in _SPAWN_ATTRS
    if isinstance(func, ast.Name):
        return func.id in _SPAWN_NAMES
    return False


def _ralphctl_argv_lines(tree: ast.Module) -> list[int]:
    """Lines where `RALPHCTL` appears in the argument subtree of a real
    process-spawning call -- never a bare mention (an `os.access`
    executability check, an import, a docstring) elsewhere in the module."""
    lines: set[int] = set()
    for node in ast.walk(tree):
        if not _is_spawn_call(node):
            continue
        for arg in [*node.args, *(kw.value for kw in node.keywords)]:
            for sub in ast.walk(arg):
                if isinstance(sub, ast.Name) and sub.id == "RALPHCTL":
                    lines.add(sub.lineno)
    return sorted(lines)


def _spawns(path: Path) -> list[str]:
    """Reasons `path`'s OWN source spawns a real ralphctl process, or []."""
    return _spawn_reasons(ast.parse(path.read_text(), str(path)))


def _spawn_reasons(tree: ast.Module) -> list[str]:
    """`_spawns()`'s rule applied to an already-parsed module."""
    why: list[str] = []
    calls = sorted({
        node.lineno for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "ralphctl"
    })
    if calls and not _overrides_ralphctl(tree):
        why.append(f"unoverridden .ralphctl() call at line(s) {calls}")
    argv = _ralphctl_argv_lines(tree)
    if argv:
        why.append(f"RALPHCTL spawn argv at line(s) {argv}")
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


def test_a_bare_RALPHCTL_mention_is_not_a_process_boundary() -> None:
    """The classifier's own control pair (the false positive review caught in
    this module's first version): asserting the resolved path exists is not
    spawning; passing it to `subprocess.run` is."""
    existence_check_only = (
        "import os\n"
        "from pathlib import Path\n"
        "from tests.conftest import RALPHCTL, RALPHD_ENGINE\n"
        "def test_this_sessions_scripts_actually_exist():\n"
        "    for script in (RALPHCTL, RALPHD_ENGINE):\n"
        "        assert isinstance(script, Path)\n"
        "        assert script.exists()\n"
        "        assert os.access(script, os.X_OK)\n"
    )
    assert _spawn_reasons(ast.parse(existence_check_only)) == [], (
        "a module that only checks the resolved path's existence and "
        "executability spawns nothing and must not be classified as a "
        "process boundary"
    )
    really_spawns = existence_check_only + (
        "def test_it_also_runs_it():\n"
        "    import subprocess\n"
        "    subprocess.run([str(RALPHCTL), '--version'], check=True)\n"
    )
    assert _spawn_reasons(ast.parse(really_spawns)), (
        "the same source with a real subprocess.run([str(RALPHCTL), ...]) "
        "must be classified as a process boundary"
    )
    # And the real module the false positive was about is classified the
    # way the control case says it should be.
    assert _spawns(TESTS_DIR / "test_ralphctl_resolution.py") == []
    assert "test_ralphctl_resolution.py" not in TREE_WIDE_ALLOWLIST


def test_mutating_the_tree_wide_allowlist_fails_the_check() -> None:
    mutated = frozenset(TREE_WIDE_ALLOWLIST - {"test_debt_ledger.py"})
    derived = set(_boundary_set("test_*.py"))
    assert derived != mutated, (
        "a locally shrunk copy of the tree-wide allowlist must not still "
        "match the derived set"
    )
