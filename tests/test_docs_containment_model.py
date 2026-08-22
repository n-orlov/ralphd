"""The containment model, as documented (task 032's sweep of the #28 debt).

Tasks 010 and 011 fixed the defect that a worker iteration blowing its
`iteration_timeout_s` ended the whole engine, and they changed **no docs**: the
guarantee an operator actually cares about -- *a failed iteration costs one
iteration, never the run* -- was true in the code and stated nowhere. This
module is the check that keeps the prose and the code the same story.

Every assertion here is derived from the source, never from a second copy of
the claim: the boundary's exception class and its two deliberate escapes are
read out of `engine/loop.py`'s AST, the timeout idiom out of
`engine/runner.py`, and the two operator-visible strings out of
`engine/state.py`, so a later refactor that moves the boundary, narrows it back
to `except Exception`, or renames the exit reason makes a named test here fail
rather than leaving the docs quietly wrong.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

from ralphd.engine import runner as runner_mod
from ralphd.engine import state as state_mod
from ralphd.engine.state import format_exit_reason

REPO_ROOT = Path(__file__).resolve().parents[1]
SPEC = REPO_ROOT / "SPEC.md"
ARCH = REPO_ROOT / "docs" / "architecture.md"
API = REPO_ROOT / "docs" / "api.md"
CLI = REPO_ROOT / "docs" / "cli.md"
LOOP_PY = REPO_ROOT / "src" / "ralphd" / "engine" / "loop.py"
RUNNER_PY = REPO_ROOT / "src" / "ralphd" / "engine" / "runner.py"

# The one sentence this whole module exists to keep honest.
GUARANTEE = "costs one iteration, never the run"
ENGINE_FAILURE_PREFIX = "engine iteration failure: "

# What each doc must say about containment. Keyed by file so a dropped claim
# names the file it was dropped from.
DOC_CLAIMS: dict[Path, tuple[str, ...]] = {
    SPEC: (
        GUARANTEE,
        "iteration containment boundary",
        "`BaseException`",
        ENGINE_FAILURE_PREFIX,
        "asyncio.wait({pump_task}, timeout=...)",
        "`KeyboardInterrupt`/`SystemExit`",
        "cancelling() > 0",
        "SHUTDOWN_GRACE_S",
    ),
    ARCH: (
        "one iteration, never the run",
        "The iteration containment boundary",
        "except BaseException",
        ENGINE_FAILURE_PREFIX,
        "asyncio.wait({pump_task}, timeout=...)",
        "cancelling() > 0",
        "`_run_job_core`'s, which stays `except Exception` on purpose",
    ),
    API: (
        "engine iteration failure:",
        "`exitCode: null`",
        "costs one iteration and never the run",
    ),
    CLI: (
        "exit: error: engine iteration failure: ...",
        "exit: iteration timeout",
        "one iteration, never the run",
    ),
}


def _missing(path: Path) -> list[str]:
    text = path.read_text()
    return [claim for claim in DOC_CLAIMS[path] if claim not in text]


@pytest.mark.parametrize("path", list(DOC_CLAIMS),
                         ids=lambda p: p.name)
def test_the_docs_state_the_containment_model(path: Path):
    """SPEC §8.1, architecture §9, api.md's iteration record and cli.md's
    iteration detail each say what one iteration can and cannot do to its
    run."""
    assert not _missing(path), (
        f"{path.relative_to(REPO_ROOT)} no longer states: {_missing(path)}")


@pytest.mark.parametrize("path,claim",
                         [(p, c) for p, cs in DOC_CLAIMS.items() for c in cs],
                         ids=lambda v: v.name if isinstance(v, Path) else v)
def test_the_doc_check_catches_every_dropped_claim(path: Path, claim: str):
    """The mutation case for the check above: each claim is load-bearing on its
    own, so deleting any single one is caught by name."""
    text = path.read_text().replace(claim, "")
    missing = [c for c in DOC_CLAIMS[path] if c not in text]
    assert claim in missing, (
        f"dropping {claim!r} from {path.name} would go unnoticed")


# --------------------------------------------------------------------------
# the code the docs describe
# --------------------------------------------------------------------------

def _boundary_handler() -> ast.ExceptHandler:
    """The `try` whose own body is the single `await self.runner.run(...)`."""
    tree = ast.parse(LOOP_PY.read_text())
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Try):
            continue
        # the innermost such try: a body holding another `try` is an outer one
        if any(isinstance(inner, ast.Try) for stmt in node.body
               for inner in ast.walk(stmt)):
            continue
        src = ast.unparse(node.body)
        if "self.runner.run(" in src and "await" in src:
            found.append(node)
    assert len(found) == 1, (
        f"expected exactly one try/except around the agent await, got {len(found)}")
    handlers = found[0].handlers
    assert len(handlers) == 1, "the boundary grew a second handler"
    return handlers[0]


def test_the_boundary_catches_baseexception_at_the_agent_await():
    """SPEC §8.1 / architecture §9 claim the guard around the one
    `await self.runner.run(...)` is `except BaseException` -- because the
    leak that ended the v0.6 engine was a `CancelledError`, which is not an
    `Exception`."""
    handler = _boundary_handler()
    assert isinstance(handler.type, ast.Name), ast.unparse(handler.type)
    assert handler.type.id == "BaseException", (
        "the containment boundary was narrowed; every doc that promises "
        f"'{GUARANTEE}' is now wrong")


def test_the_boundary_records_one_failed_iteration_with_no_exit_code():
    """api.md's shape: `exitCode: null` plus an `error` starting
    `engine iteration failure:`, i.e. an ordinary recorded iteration."""
    body = ast.unparse(_boundary_handler().body)
    assert "IterationResult(exit_code=None)" in body, body
    assert ENGINE_FAILURE_PREFIX.strip() in body, body
    assert "raise" in body, "the two deliberate escapes are gone"


@pytest.mark.parametrize("escape", ["KeyboardInterrupt", "SystemExit",
                                    "cancelling()"])
def test_the_two_deliberate_escapes_are_re_raised(escape: str):
    """Both docs promise the boundary does NOT swallow a shutdown or a
    cancellation genuinely requested on this task."""
    body = ast.unparse(_boundary_handler().body)
    assert escape in body, (
        f"{escape} is no longer re-raised, contradicting the docs' "
        "'deliberately not contained'")


def test_the_timeout_path_never_re_awaits_a_cancelled_task():
    """The defect itself: `wait_for` cancels the future it timed out on, so
    re-awaiting that task re-raises `CancelledError`. Both docs name
    `asyncio.wait({pump_task}, timeout=...)` as the idiom that replaced it."""
    src = ast.unparse(ast.parse(RUNNER_PY.read_text()))  # comments stripped
    assert "asyncio.wait({pump_task}, timeout=timeout_s)" in src
    assert "wait_for(pump_task" not in src, (
        "the wait_for idiom is back on the pump task: an iteration that blows "
        "its timeout can end the engine again")


def test_the_drain_grace_is_the_constant_the_docs_name():
    """Both docs call the post-SIGINT drain window
    `runner.SHUTDOWN_GRACE_S`, so it has to be a module constant and not an
    inline literal."""
    assert isinstance(runner_mod.SHUTDOWN_GRACE_S, (int, float))
    assert runner_mod.SHUTDOWN_GRACE_S > 0
    assert "SHUTDOWN_GRACE_S" in RUNNER_PY.read_text()


def test_the_outer_guard_stays_exception_on_purpose():
    """architecture §9 contrasts the boundary with `_run_job_core`'s guard,
    which stays `except Exception` so a genuine shutdown still unwinds."""
    tree = ast.parse(LOOP_PY.read_text())
    fns = [n for n in ast.walk(tree)
           if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
           and n.name == "_run_job_core"]
    assert len(fns) == 1
    handlers = [h.type.id for n in ast.walk(fns[0])
                if isinstance(n, ast.Try)
                for h in n.handlers if isinstance(h.type, ast.Name)]
    assert "Exception" in handlers and "BaseException" not in handlers, handlers


# --------------------------------------------------------------------------
# the two operator-visible strings cli.md quotes
# --------------------------------------------------------------------------

def test_cli_md_quotes_the_real_engine_failure_line():
    """`exit: error: engine iteration failure: ...` is what
    `format_exit_reason` actually renders for the boundary's record, fault
    verdict appended -- not a hand-written approximation."""
    reason = format_exit_reason({"endedAt": "x", "exitCode": None,
                                 "error": ENGINE_FAILURE_PREFIX + "RuntimeError()",
                                 "faultClass": "work"})
    assert reason.startswith("error: " + ENGINE_FAILURE_PREFIX)
    assert reason.endswith("[work fault]")
    assert "exit: error: engine iteration failure: ..." in CLI.read_text()


def test_cli_md_quotes_the_real_timeout_exit_reason():
    """`exit: iteration timeout` is `state.EXIT_REASON_TIMEOUT`, so renaming
    the reason breaks the doc claim instead of silently outdating it."""
    assert state_mod.EXIT_REASON_TIMEOUT == "iteration timeout"
    assert format_exit_reason({"endedAt": "x", "exitCode": None,
                               "timedOut": True}) == "iteration timeout"
    assert f"exit: {state_mod.EXIT_REASON_TIMEOUT}" in CLI.read_text()


def test_the_behavioural_proof_of_the_guarantee_still_exists():
    """The prose points at #28; the *behaviour* is proven by tasks 010/011's
    black-box modules. If those disappear, this doc claim is unbacked."""
    for name in ("test_iteration_timeout_contained.py",
                 "test_iteration_cancellation_contained.py"):
        assert (REPO_ROOT / "tests" / name).is_file(), name
