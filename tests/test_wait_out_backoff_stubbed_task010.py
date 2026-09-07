"""Task 010 (B.4): a grep-based check pinning that every in-process test
outside the three named exempt modules keeps `LoopSupervisor._wait_out_backoff`
harmless -- either genuinely stubbed (the default `make_supervisor()`/
`scripted_supervisor()`/`supervisor_with_runner()` harness gives every caller
that does not explicitly ask for the real thing) or backed by an explicit
all-zero backoff schedule (`infra_retry_backoff_s=[0.0]`,
`infra_retry_backoff_max_s=0.0`) so the real function returns instantly even
when it IS the real function (`tests/test_resume_after_incurable_rejection.py`
deliberately keeps the real `_wait_out_backoff` -- episode 2's dispatch is the
thing under test -- but pins its own cost to zero this same way).

The three modules the PRD names as genuinely testing the backoff/retry
schedule itself, and therefore exempt: `tests/test_infra_outage_budget.py`,
`tests/test_retry_now.py`, `tests/test_reflect_infra_retry.py`.

This is deliberately a TEXTUAL check (grep-shaped, not an AST/semantic one):
it looks at the literal source of every other `tests/*.py` and
`tests/flows/*.py` module for two violation shapes --

1. `no_backoff=False` (or the dict-literal `"no_backoff": False` a
   `make_supervisor(**kw)` call spreads in) with no all-zero backoff config
   anywhere in the same file;
2. a direct `LoopSupervisor(...)` construction (bypassing the
   `make_supervisor` family entirely, so its default stub never applies)
   whose own call span carries no all-zero backoff config, and that has no
   `_wait_out_backoff = ...` stub assignment shortly after it either.

A module that never constructs a supervisor at all (most of the suite) is
never inspected -- the check costs nothing on every module that doesn't
touch this contract.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
TESTS_DIR = REPO_ROOT / "tests"
FLOWS_DIR = TESTS_DIR / "flows"

# The PRD's own three exempt modules (B.4): these test the backoff/retry
# schedule itself and are allowed to leave `_wait_out_backoff` real.
EXEMPT_MODULES = {
    "test_infra_outage_budget.py",
    "test_retry_now.py",
    "test_reflect_infra_retry.py",
}

_ZERO = r"(?:0(?:\.0+)?|\[\s*0(?:\.0+)?\s*\])"
_ZERO_BACKOFF_S_RE = re.compile(
    r"infra_retry_backoff_s[\"']?\s*[:=]\s*\[\s*0(?:\.0+)?\s*\]")
_ZERO_BACKOFF_MAX_RE = re.compile(
    r"infra_retry_backoff_max_s[\"']?\s*[:=]\s*0(?:\.0+)?\b")
_NO_BACKOFF_FALSE_RE = re.compile(r"no_backoff[\"']?\s*[:=]\s*False\b")
_LOOP_SUPERVISOR_CALL_RE = re.compile(r"\bLoopSupervisor\(")
_WAIT_OUT_BACKOFF_STUB_RE = re.compile(r"_wait_out_backoff\s*=[^=]")


def _target_files() -> list[Path]:
    files = sorted(TESTS_DIR.glob("test_*.py")) + sorted(FLOWS_DIR.glob("test_*.py"))
    # The checker's own module is excluded: its synthetic snippets (the
    # parametrized proof below) are literal violation SHAPES on purpose and
    # would otherwise flag themselves.
    return [f for f in files
            if f.name not in EXEMPT_MODULES and f.name != Path(__file__).name]


def _has_zero_backoff_config(text: str) -> bool:
    return bool(_ZERO_BACKOFF_S_RE.search(text) and _ZERO_BACKOFF_MAX_RE.search(text))


def _balanced_call_span(text: str, open_paren_idx: int) -> str:
    """`text[open_paren_idx:]` up to (inclusive) the matching close paren for
    the '(' at `open_paren_idx`, tolerant of nesting."""
    depth = 0
    i = open_paren_idx
    n = len(text)
    while i < n:
        if text[i] == "(":
            depth += 1
        elif text[i] == ")":
            depth -= 1
            if depth == 0:
                return text[open_paren_idx:i + 1]
        i += 1
    return text[open_paren_idx:]


def violations_in_text(text: str, label: str) -> list[str]:
    """The check's whole logic, factored out so the negative-proof test
    below can feed it a synthetic snippet without touching disk."""
    out: list[str] = []

    if _NO_BACKOFF_FALSE_RE.search(text) and not _has_zero_backoff_config(text):
        out.append(f"{label}: no_backoff=False with no all-zero backoff "
                   f"config (infra_retry_backoff_s/infra_retry_backoff_max_s) "
                   f"anywhere in the file")

    for m in _LOOP_SUPERVISOR_CALL_RE.finditer(text):
        call_span = _balanced_call_span(text, m.end() - 1)
        # The config is often built a few lines earlier as `cfg = JobConfig(
        # ..., infra_retry_backoff_s=[0.0], ...)` and then just `cfg` is
        # passed in -- so a window around the call (not only the call's own
        # literal text) is what a `cfg =`/`**kw` forwarding shape needs.
        window_start = max(0, m.start() - 1200)
        window = text[window_start:m.end()] + call_span
        if _has_zero_backoff_config(window):
            continue
        # A stub assigned shortly (within ~800 chars, generously >10 lines)
        # after the construction covers the common
        # `sup = LoopSupervisor(...); ...; sup._wait_out_backoff = fn` shape.
        tail = text[m.end(): m.end() + 800]
        if _WAIT_OUT_BACKOFF_STUB_RE.search(tail):
            continue
        line_no = text.count("\n", 0, m.start()) + 1
        out.append(f"{label}: LoopSupervisor(...) at line {line_no} leaves "
                   f"_wait_out_backoff unstubbed with no all-zero backoff "
                   f"config (within ~30 lines before/at the call) and no "
                   f"nearby stub assignment")

    return out


def test_no_in_process_test_outside_the_exempt_modules_leaves_backoff_unstubbed():
    all_violations: list[str] = []
    for path in _target_files():
        all_violations.extend(violations_in_text(path.read_text(), path.name))
    assert not all_violations, (
        "the following in-process tests leave a real, un-zeroed "
        "_wait_out_backoff reachable (B.4) -- stub it via the default "
        "make_supervisor()/scripted_supervisor() harness, add "
        "infra_retry_backoff_s=[0.0]/infra_retry_backoff_max_s=0.0, or add "
        "the module to the PRD's named exempt list:\n" + "\n".join(all_violations))


def test_exempt_modules_are_the_prds_named_three_and_still_on_disk():
    names = {p.name for p in TESTS_DIR.glob("test_*.py")}
    for exempt in EXEMPT_MODULES:
        assert exempt in names, f"exempt module {exempt} no longer exists on disk"
    assert EXEMPT_MODULES == {
        "test_infra_outage_budget.py", "test_retry_now.py",
        "test_reflect_infra_retry.py"}


@pytest.mark.parametrize("snippet,expect_violation", [
    # A bare, un-stubbed, non-zero-config construction: caught.
    ('sup = LoopSupervisor(JobConfig(run_id="x"), run, root)\n', True),
    # Stubbed right after construction: clean.
    ('sup = LoopSupervisor(JobConfig(run_id="x"), run, root)\n'
     'sup._wait_out_backoff = _no_backoff\n', False),
    # All-zero config on the same call: clean.
    ('sup = LoopSupervisor(JobConfig(run_id="x", infra_retry_backoff_s=[0.0],\n'
     '                                infra_retry_backoff_max_s=0.0), run, root)\n', False),
    # no_backoff=False with no zero config anywhere: caught.
    ('sup = make_supervisor(root, no_backoff=False)\n', True),
    # no_backoff=False WITH zero config elsewhere in the file: clean (the
    # test_resume_after_incurable_rejection.py shape).
    ('sup = make_supervisor(root, no_backoff=False,\n'
     '                       infra_retry_backoff_s=[0.0],\n'
     '                       infra_retry_backoff_max_s=0.0)\n', False),
])
def test_the_checker_actually_fires_on_the_shapes_it_claims_to_catch(
        snippet, expect_violation):
    """Proof the check is not vacuous (the whole point of pinning it): a
    synthetic violation is caught, and each of its accepted escape hatches
    genuinely clears it."""
    got = violations_in_text(snippet, "synthetic")
    assert bool(got) is expect_violation, got
