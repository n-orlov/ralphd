"""Task 014 (C.4, #129): guard against a `return` statement immediately
following another `return` in the same block anywhere under `src/ralphd/`.

That shape is dead code by construction -- the first `return` already
exits the enclosing function, so a second `return` statement at the same
block level can never execute. This is exactly what iteration N found in
`Engine._apply_worker_completion_report` (`src/ralphd/engine/loop.py`): a
stray `return []` sitting right after
`return self._apply_reported_task_status(...)`, unreachable and removed
by this same task.

The codebase's stated-unreachable convention (see
`src/ralphd/engine/scheduler.py`'s `return None  # unreachable: ...`) is a
single `return` carrying a trailing `# unreachable: ...` comment to
document a branch the type-checker/reader needs spelled out -- it is not
two consecutive `return` statements, so it is untouched by this guard and
does not need an exemption clause here.
"""

import ast
import pathlib

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src" / "ralphd"


def _find_double_returns(source: str, filename: str = "<test>"):
    """Return a list of (first_lineno, second_lineno) pairs where a
    `return` statement is immediately followed by another `return`
    statement in the same statement block (function body, if-branch,
    for-body, etc.) anywhere in `source`.
    """
    tree = ast.parse(source, filename=filename)
    hits = []
    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if not isinstance(body, list):
            continue
        for prev, cur in zip(body, body[1:]):
            if isinstance(prev, ast.Return) and isinstance(cur, ast.Return):
                hits.append((prev.lineno, cur.lineno))
        # `orelse`/`finalbody` are separate blocks too (e.g. an `else:`
        # clause on an `if`/`for`, or a `try`'s `finally:`); walk() already
        # visits their statements as part of `ast.walk`, but `orelse`/
        # `finalbody` themselves are plain lists on the same node, not
        # separate AST nodes with their own `body` -- so check them here.
        for extra_block in (getattr(node, "orelse", None),
                             getattr(node, "finalbody", None)):
            if not isinstance(extra_block, list):
                continue
            for prev, cur in zip(extra_block, extra_block[1:]):
                if isinstance(prev, ast.Return) and isinstance(cur, ast.Return):
                    hits.append((prev.lineno, cur.lineno))
    return hits


def test_detector_fires_on_an_injected_double_return_sample():
    """Self-test (required by the task's success criteria): the detector
    itself must catch a deliberately unreachable second `return` in a
    same-block sample, so a silently-broken detector can't pass this
    module by finding nothing anywhere.
    """
    sample = """
def f(x):
    if x:
        return 1
        return 2  # unreachable, dead
    return 3
"""
    hits = _find_double_returns(sample)
    assert hits == [(4, 5)]


def test_detector_does_not_flag_a_single_stated_unreachable_return():
    """The codebase's own convention -- one `return` with a trailing
    `# unreachable: ...` comment -- is a single statement, not two, and
    must not be flagged.
    """
    sample = """
def f(candidates):
    for c in candidates:
        return c
    return None  # unreachable: candidates is non-empty
"""
    assert _find_double_returns(sample) == []


def test_detector_does_not_flag_returns_in_different_blocks():
    sample = """
def f(x):
    if x:
        return 1
    else:
        return 2
"""
    assert _find_double_returns(sample) == []


@pytest.mark.parametrize(
    "path",
    sorted(SRC_ROOT.rglob("*.py")),
    ids=lambda p: str(p.relative_to(REPO_ROOT)),
)
def test_no_source_file_has_a_dead_return_after_return(path):
    source = path.read_text()
    hits = _find_double_returns(source, filename=str(path))
    assert hits == [], (
        f"{path.relative_to(REPO_ROOT)} has an unreachable `return` "
        f"immediately following another `return` in the same block at "
        f"line(s) {hits} -- remove the dead statement or, if it genuinely "
        f"documents an unreachable branch, replace it with a single "
        f"`return ...  # unreachable: <reason>` per the codebase's stated "
        f"convention (see src/ralphd/engine/scheduler.py)."
    )
