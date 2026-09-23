"""Task 017 (F.2, #216): every wait for the *ralphd-engine* subprocess to
exit, anywhere under `tests/`, goes through `tests/flows/flowlib.
wait_engine_exit` (task 016, F.1) rather than a bare `proc.wait(timeout=...)`
-- so a hang there gets the process-tree/pipe/status.json/events.jsonl dump
instead of a bare `TimeoutExpired`.

The set this guard polices is defined exactly the way the task names it:
the live output of `grep -rn 'proc.wait(timeout' tests/`. Wiring a call site
through the helper changes its text to `wait_engine_exit(<run>, timeout=...)`,
which no longer contains the substring `proc.wait(timeout` at all -- so a
routed site simply drops out of the grep's own output. What is LEFT in that
output, after task 017's own wiring commit, is exactly nine files' worth of
sites that are not engine-exit waits at all (a CLI subprocess wrapper, a
teardown escalation that must keep swallowing its own timeout, a standalone
supervisor script, the helper's own implementation and unit tests) plus ONE
single deliberate exception: F.4/non-negotiable 2 forbids adding a barrier
ahead of, or wiring the helper into, the one `proc.wait(timeout=60)` in
`tests/flows/test_flow_cure_pass.py::
test_the_second_curable_rejection_finds_the_cure_pass_spent_and_replans` (the
node task 002/A.2 measured). This module fails the moment the grep's output
contains anything else: a new bare `<engine>.proc.wait(timeout=...)` call
that is not one of the ten known, explained exceptions below.

Each exception is keyed to the EXACT file and, for the ambiguous files (ones
that mix an engine wait with a non-engine one, or that carry a mix of code
and prose), to a content pattern -- never a bare per-file allow so that a
NEW bare engine wait added to an already-exempt file (e.g. a fresh
`e.proc.wait(timeout=...)` line dropped into `tests/conftest.py` outside
`LiveRun.stop`) still gets caught."""

from __future__ import annotations

import ast
import re
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
TESTS_ROOT = REPO_ROOT / "tests"

CURE_PASS_REL = "flows/test_flow_cure_pass.py"
CURE_PASS_NODE = "test_the_second_curable_rejection_finds_the_cure_pass_spent_and_replans"

# relpath (relative to the scanned root, POSIX-style) -> one or more
# content patterns that are known NOT to be an engine-exit wait, with why.
EXEMPT: dict[str, list[tuple[re.Pattern, str]]] = {
    "stub-docker/live_engine_supervisor.py": [
        (re.compile(r"^\s*proc\.wait\(timeout=30\)\s*$"),
         "a standalone supervisor SCRIPT (the STUB_DOCKER_LIVE_ENGINE "
         "knob's container-entrypoint stand-in), not a pytest test module; "
         "it deliberately swallows TimeoutExpired on one branch and is "
         "never wired to tests/flows/flowlib"),
    ],
    "test_cli_watch_resumed_run.py": [
        (re.compile(r"self\.proc\.wait\(timeout="),
         "Watcher.proc is the `ralphctl watch` CLI subprocess it drives, "
         "never the engine"),
    ],
    "test_cli_ui.py": [
        (re.compile(r"self\.proc\.wait\(timeout="),
         "UiServer.proc is the ui-server script subprocess, never the "
         "engine"),
    ],
    "test_cli_logs_resumed_run.py": [
        (re.compile(r"self\.proc\.wait\(timeout="),
         "Follower.proc is the `ralphctl logs -f` CLI subprocess, never "
         "the engine (this module's genuine engine wait, `e1.proc.wait`, "
         "is routed through wait_engine_exit and no longer matches this "
         "grep at all)"),
    ],
    "test_cli_logs_pty_termios.py": [
        (re.compile(r"(?<![.\w])proc\.wait\(timeout="),
         "bare `proc` is the PTY-driven `ralphctl logs` CLI subprocess "
         "under test, never the engine"),
    ],
    "test_cli_logs_signal.py": [
        (re.compile(r"(?<![.\w])proc\.wait\(timeout="),
         "bare `proc` is the CLI subprocess under a signal-delivery test, "
         "never the engine"),
    ],
    "test_cli_logs_follow_liveness.py": [
        (re.compile(r"(?<![.\w])proc\.wait\(timeout="),
         "bare `proc` is the `ralphctl logs --follow` CLI subprocess under "
         "test, never the engine"),
    ],
    "conftest.py": [
        (re.compile(r"self\.proc\.wait\(timeout=(2|10)\)\s*$"),
         "LiveRun.stop()'s own teardown escalation (terminate() then a "
         "bounded wait before kill()) -- it already swallows "
         "TimeoutExpired itself on purpose and must keep doing so; wiring "
         "the assertion-raising helper in here would break clean teardown "
         "of a hung child"),
    ],
    "flows/flowlib.py": [
        (re.compile(r".*"),
         "the helper's own implementation: the bottom `run.proc.wait("
         "timeout=timeout)` this whole task wires every genuine call site "
         "THROUGH, plus two docstring mentions of that same shape -- not "
         "a call site to route"),
    ],
    "flows/test_flowlib_engine_exit_wait.py": [
        (re.compile(r".*"),
         "the helper's own unit tests (task 016): a module docstring "
         "mention, and `sleeping_child`'s fixture teardown of a bare "
         "synthetic subprocess.Popen() built to fake a hang -- never a "
         "real ralphd-engine"),
    ],
    "test_engine_exit_wait_wiring.py": [
        (re.compile(r".*"),
         "this guard module itself: its docstring, comments and source "
         "quote/build the literal grep pattern and synthetic mutation "
         "fixtures repeatedly, none of which is a real engine-exit wait"),
    ],
    CURE_PASS_REL: [
        (re.compile(r"^\s*#.*proc\.wait\(timeout=60\)"),
         "the comment documenting the exception directly below it"),
        (re.compile(r"^\s*assert run\.proc\.wait\(timeout=60\) == 0, \($"),
         "F.4/non-negotiable 2's single named exception -- verified in "
         "shape by test_the_cure_pass_exemption_keeps_its_bare_shape below, "
         "not merely allow-listed here"),
    ],
}


def find_unrouted_engine_exit_waits(root: Path) -> list[str]:
    """Every line `grep -rn 'proc.wait(timeout' <root>` finds that is
    neither already routed through `wait_engine_exit` (which drops the call
    out of this grep's own pattern) nor covered by `EXEMPT` above.
    Returns human-readable violation messages, one per offending line."""
    proc = subprocess.run(
        ["grep", "-rn", "proc.wait(timeout", str(root)],
        capture_output=True, text=True)
    # grep's own exit status is 1 for "no matches", not an error here.
    if proc.returncode not in (0, 1):
        raise RuntimeError(
            f"grep -rn 'proc.wait(timeout' {root} failed: {proc.stderr}")

    violations: list[str] = []
    for line in proc.stdout.splitlines():
        if not line.strip():
            continue
        path_str, lineno_str, content = line.split(":", 2)
        rel = Path(path_str).resolve().relative_to(root.resolve()).as_posix()
        patterns = EXEMPT.get(rel, [])
        if any(pat.search(content) for pat, _reason in patterns):
            continue
        violations.append(
            f"{rel}:{lineno_str}: engine-exit wait not routed through "
            f"wait_engine_exit and not on the known-exempt list: {content!r}")
    return violations


def test_no_unrouted_engine_exit_waits_under_tests():
    """The real check, over the real `tests/` tree."""
    violations = find_unrouted_engine_exit_waits(TESTS_ROOT)
    assert not violations, "\n".join(violations)


def test_the_cure_pass_exemption_keeps_its_bare_shape():
    """F.4/non-negotiable 2, asserted by THIS guard (task 017's own
    requirement): the named cure-pass node's single `proc.wait(timeout=60)`
    is unrouted, unraised, and has no barrier freshly inserted ahead of it
    -- the statement immediately before the wait, in the function body, is
    still the bare `run = engine_factory(...)` assignment task 002 measured
    against, nothing new in between."""
    path = TESTS_ROOT / "flows" / "test_flow_cure_pass.py"
    text = path.read_text()
    tree = ast.parse(text, filename=str(path))

    func = None
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == CURE_PASS_NODE:
            func = node
            break
    assert func is not None, (
        f"could not find {CURE_PASS_NODE} in {path} at all -- has it been "
        f"renamed or removed?")

    body = func.body
    wait_idx = None
    for i, stmt in enumerate(body):
        if (isinstance(stmt, ast.Assert)
                and isinstance(stmt.test, ast.Compare)
                and isinstance(stmt.test.left, ast.Call)):
            call = stmt.test.left
            if (isinstance(call.func, ast.Attribute) and call.func.attr == "wait"
                    and isinstance(call.func.value, ast.Attribute)
                    and call.func.value.attr == "proc"):
                wait_idx = i
                break
    assert wait_idx is not None, (
        f"{CURE_PASS_NODE} no longer contains a bare `<x>.proc.wait(...)` "
        f"assert at all -- either it was routed through wait_engine_exit "
        f"(forbidden by F.4) or the shape changed enough that this scan "
        f"can no longer find it; re-audit by hand.")

    wait_stmt = body[wait_idx]
    src = ast.get_source_segment(text, wait_stmt)
    assert "wait_engine_exit" not in src, (
        f"{CURE_PASS_NODE}: F.4 forbids routing this exact node's wait "
        f"through the helper -- found it wired anyway:\n{src}")
    assert re.search(r"\.proc\.wait\(timeout=60\)", src), (
        f"{CURE_PASS_NODE}: the bound must stay exactly timeout=60 (no "
        f"bound raised) -- got:\n{src}")

    assert wait_idx > 0, f"{CURE_PASS_NODE}: the wait is the function's first statement?"
    prev = body[wait_idx - 1]
    assert (isinstance(prev, ast.Assign) and len(prev.targets) == 1
            and isinstance(prev.targets[0], ast.Name)
            and prev.targets[0].id == "run"
            and isinstance(prev.value, ast.Call)), (
        f"{CURE_PASS_NODE}: a new statement sits between `run = "
        f"engine_factory(...)` and the wait -- that is a barrier ahead of "
        f"the wait, forbidden by F.4/non-negotiable 2. Found instead:\n"
        f"{ast.get_source_segment(text, prev)}")


def test_mutation_a_new_unwired_engine_wait_is_caught(tmp_path):
    """A synthetic module (never under the real `tests/` tree) with a plain
    `e.proc.wait(timeout=45)` -- no exemption anywhere for this made-up
    relpath -- must be reported."""
    planted = tmp_path / "test_planted_bad_wait.py"
    planted.write_text(
        "def test_bad(engine_factory):\n"
        "    e = engine_factory(job={})\n"
        "    assert e.proc.wait(timeout=45) == 0\n")
    violations = find_unrouted_engine_exit_waits(tmp_path)
    assert len(violations) == 1
    assert "test_planted_bad_wait.py:3" in violations[0]


def test_mutation_b_a_wired_wait_is_not_flagged(tmp_path):
    """The mirror image: the same shape, routed through `wait_engine_exit`,
    must not be reported -- it no longer even matches the grep pattern."""
    planted = tmp_path / "test_planted_good_wait.py"
    planted.write_text(
        "from tests.flows.flowlib import wait_engine_exit\n\n\n"
        "def test_good(engine_factory):\n"
        "    e = engine_factory(job={})\n"
        "    assert wait_engine_exit(e, timeout=45) == 0\n")
    violations = find_unrouted_engine_exit_waits(tmp_path)
    assert violations == []
