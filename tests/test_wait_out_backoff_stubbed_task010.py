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

Since the task's second run this module also pins B.4's FIRST clause -- the
stub-`pi` (subprocess) side of the same "no real sleeps" requirement:

* the shared launcher (`tests/conftest.py`'s `LiveRun`, behind the `live` and
  `engine_factory` fixtures) gives EVERY live engine an all-zero infra-retry
  backoff schedule unless the calling test names one itself
  (`_with_zero_backoff`, exercised below), so a stub-`pi` module that merely
  happens to hit one infra fault can no longer pay `JobConfig`'s shipped
  2 s..300 s ladder;
* and NO module outside the three exempt ones names a non-zero schedule of its
  own any more: the five that used to (`test_status_infra_retry_note.py`,
  `test_status_health_infra_wait.py`, `test_infra_deadline_extension.py`,
  `test_cli_fault_explanation.py`, `test_reflection.py`) were converted on this
  task's third pass -- two of them observe the wait-only status window from
  *inside* a stubbed `_wait_out_backoff` (in-process, deterministic, no poll
  race), one bounds its retry episode with an attempt cap instead of a spent
  outage budget, and two assert their subject against the engine's own
  zero-cost ladder. `NONZERO_BACKOFF_ALLOWLIST` below is therefore down to the
  single module that names the knobs without launching anything at all, and
  `test_the_allowlist_has_no_stale_entries` keeps it from growing back
  silently.
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


# ---------------------------------------------------------------------------
# B.4's first clause: the stub-`pi` (subprocess) side.

# Every module that names a NON-ZERO `RALPHD_INFRA_RETRY_BACKOFF_S` /
# `RALPHD_INFRA_RETRY_BACKOFF_MAX_S` -- the only two knobs a spawned engine
# reads, and therefore the only ones that can buy a real sleep in a stub-`pi`
# test -- with the reason it cannot be zero. Exactly one module qualifies, and
# it launches no engine at all. An in-process module configuring a non-zero
# schedule is NOT on this list and does not need to be: its `_wait_out_backoff`
# is stubbed (the clause above), so the numbers are reported, never waited.
NONZERO_BACKOFF_ALLOWLIST = {
    "test_job_config_defaults.py":
        "no engine is launched at all: monkeypatched env, read back through "
        "in-process `JobConfig.load` -- the parsing of the two knobs is the "
        "subject",
}

_ENV_BACKOFF_RE = re.compile(
    r"RALPHD_INFRA_RETRY_BACKOFF_(?:MAX_)?S\"\s*[:,]\s*\"([^\"]*)\"")
_NUMBER_RE = re.compile(r"\d+(?:\.\d+)?")


def nonzero_env_backoffs(text: str) -> list[str]:
    """The non-zero `RALPHD_INFRA_RETRY_BACKOFF*` values `text` names."""
    found = []
    for m in _ENV_BACKOFF_RE.finditer(text):
        value = m.group(1)
        if any(float(n) != 0.0 for n in _NUMBER_RE.findall(value)):
            found.append(value)
    return found


def _scanned_files() -> list[Path]:
    files = sorted(TESTS_DIR.glob("test_*.py")) + sorted(FLOWS_DIR.glob("test_*.py"))
    return [f for f in files
            if f.name not in EXEMPT_MODULES and f.name != Path(__file__).name]


def test_no_stub_pi_module_buys_a_real_backoff_sleep_outside_the_allowlist():
    unlisted = {}
    for path in _scanned_files():
        found = nonzero_env_backoffs(path.read_text())
        if found and path.name not in NONZERO_BACKOFF_ALLOWLIST:
            unlisted[path.name] = found
    assert not unlisted, (
        "these modules configure a non-zero infra-retry backoff for a spawned "
        "engine, i.e. a real sleep (B.4). Drop the knob so the shared "
        "launcher's all-zero schedule applies, or add the module to "
        "NONZERO_BACKOFF_ALLOWLIST in this file with the reason its subject "
        f"needs a real wait: {unlisted}")


def test_the_allowlist_has_no_stale_entries():
    """Non-vacuity in the other direction: an allowlisted module that no
    longer names a non-zero schedule must lose its entry, so the list can
    never quietly grow into a blanket permission."""
    stale = []
    for name, reason in NONZERO_BACKOFF_ALLOWLIST.items():
        assert reason.strip(), f"{name} is allowlisted with no reason"
        path = TESTS_DIR / name
        if not path.exists() or not nonzero_env_backoffs(path.read_text()):
            stale.append(name)
    assert not stale, (
        "these modules are on NONZERO_BACKOFF_ALLOWLIST but no longer "
        f"configure a non-zero backoff -- remove the entries: {stale}")


def test_the_shared_launcher_zeroes_the_backoff_unless_the_test_names_one():
    """The other half of the first clause, behaviourally: `LiveRun` (the one
    launcher behind `live`/`engine_factory`) writes an all-zero schedule into
    every job.yaml it generates, and yields the WHOLE setting -- both knobs --
    to a test that names either half of it, in the job dict or in `stub_env`
    (a spawned engine's env wins over its job.yaml, and a `max_s` of 0.0
    would cap an env-named schedule back to zero)."""
    from tests.conftest import _with_zero_backoff

    assert _with_zero_backoff({"run_id": "x"}, None) == {
        "run_id": "x", "infra_retry_backoff_s": [0.0],
        "infra_retry_backoff_max_s": 0.0}
    # An explicit job-dict schedule survives untouched, cap included.
    assert _with_zero_backoff({"infra_retry_backoff_s": [2.0]}, None) == {
        "infra_retry_backoff_s": [2.0]}
    # ... and so does an env-named one, from either half of the setting.
    assert _with_zero_backoff({}, {"RALPHD_INFRA_RETRY_BACKOFF_S": "1,1"}) == {}
    assert _with_zero_backoff({}, {"RALPHD_INFRA_RETRY_BACKOFF_MAX_S": "1"}) == {}


@pytest.mark.parametrize("snippet,expected", [
    ('"RALPHD_INFRA_RETRY_BACKOFF_S": "1,1,1",', ["1,1,1"]),
    ('"RALPHD_INFRA_RETRY_BACKOFF_MAX_S": "0.5",', ["0.5"]),
    ('"RALPHD_INFRA_RETRY_BACKOFF_S": "0",', []),
    ('"RALPHD_INFRA_RETRY_BACKOFF_S": "0.0,0",', []),
    ('setenv("RALPHD_INFRA_RETRY_BACKOFF_S", "0.1,0.2")', ["0.1,0.2"]),
    ('infra_retry_backoff_s=[1.0]  # in-process: stubbed, not waited', []),
])
def test_the_env_backoff_scanner_reads_the_shapes_it_claims_to(snippet, expected):
    assert nonzero_env_backoffs(snippet) == expected
