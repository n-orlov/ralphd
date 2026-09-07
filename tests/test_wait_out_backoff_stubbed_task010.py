"""Task 010 (B.4), cured by task 047 (operator steer 004): no in-process
test's `LoopSupervisor._wait_out_backoff` is reachable as a real, waiting
coroutine, and no `LiveRun.__init__`-bypassing `job.yaml` writer buys a
spawned engine a real backoff/STUB_SLEEP sleep either.

Task 010's own pass accepted an all-zero backoff SCHEDULE
(`infra_retry_backoff_s=[0.0]`, `infra_retry_backoff_max_s=0.0`) on a bare
`LoopSupervisor(...)` construction as equivalent to a stub -- but a zero
schedule makes the REAL `_wait_out_backoff` return instantly; it is not the
same thing as the PRD's B.4 "unstubbed" clause, and it left the ~43 modules
that construct a bare `LoopSupervisor` outside `make_supervisor()` to fend
for themselves. Task 047 closed that at the harness level instead:
`tests/conftest.py` now carries an AUTOUSE fixture that patches
`LoopSupervisor._wait_out_backoff` at the CLASS level for every collected
test, so no per-module scan for an unstubbed instance is needed any more --
this module pins that the fixture exists (below) rather than re-deriving the
old, now-obsolete, per-module scan.

What is still worth scanning the tree for, because the class-level fixture
cannot reach it:

1. the opt-out marker (`@pytest.mark.real_backoff` /
   `pytestmark = pytest.mark.real_backoff`) must be used ONLY by the three
   modules the PRD names as testing the backoff/retry schedule itself
   (`test_infra_retry_schedule.py`, `test_retry_now.py`,
   `test_reflect_infra_retry.py`) -- a fourth module carrying it would quietly
   reopen the real sleep the fixture exists to close;
2. a `job.yaml` writer under `tests/` that bypasses `LiveRun.__init__`
   (`LiveRun.__new__(LiveRun)`, the shape `tests/test_config_overlay.py`
   needs because it must chmod `/config` read-only *before* the engine
   starts) spawns a SEPARATE PROCESS the in-process fixture above cannot
   reach at all -- it must name the zero-backoff triple itself:
   `infra_retry_backoff_s: [0.0]`, `infra_retry_backoff_max_s: 0.0`, and
   `STUB_SLEEP=0`.

The second half of this module (unchanged since task 010's third pass) pins
B.4's other clause: no stub-`pi` module outside the one module that parses
the two env knobs without launching an engine at all names a non-zero
`RALPHD_INFRA_RETRY_BACKOFF_S`/`_MAX_S`.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
TESTS_DIR = REPO_ROOT / "tests"
FLOWS_DIR = TESTS_DIR / "flows"

# The PRD's own three exempt modules (B.4): these test the backoff/retry
# schedule itself and are allowed to leave `_wait_out_backoff` real (via the
# `real_backoff` opt-out marker on tests/conftest.py's autouse fixture).
EXEMPT_MODULES = {
    "test_infra_retry_schedule.py",
    "test_retry_now.py",
    "test_reflect_infra_retry.py",
}

_ZERO_BACKOFF_S_RE = re.compile(
    r"infra_retry_backoff_s[\"']?\s*[:=]\s*\[\s*0(?:\.0+)?\s*\]")
_ZERO_BACKOFF_MAX_RE = re.compile(
    r"infra_retry_backoff_max_s[\"']?\s*[:=]\s*0(?:\.0+)?\b")
_STUB_SLEEP_ZERO_RE = re.compile(
    r"STUB_SLEEP[\"']?\s*[:=]\s*[\"']?0(?:\.0+)?[\"']?\b")
_LIVERUN_BYPASS_RE = re.compile(r"LiveRun\.__new__\(")
_JOB_YAML_LITERAL_RE = re.compile(r"[\"']job\.yaml[\"']")


def _all_files() -> list[Path]:
    """Every `tests/test_*.py` and `tests/flows/test_*.py` module, this
    checker's own module excluded (its synthetic snippets below are literal
    violation SHAPES on purpose and would otherwise flag themselves)."""
    files = sorted(TESTS_DIR.glob("test_*.py")) + sorted(FLOWS_DIR.glob("test_*.py"))
    return [f for f in files if f.name != Path(__file__).name]


def _scanned_files() -> list[Path]:
    """`_all_files()` minus the three exempt modules -- the set every check
    in this module other than the exempt-module pins itself scans."""
    return [f for f in _all_files() if f.name not in EXEMPT_MODULES]


# ---------------------------------------------------------------------------
# 1. The class-level fixture itself: pinned present, not re-derived.

def test_conftest_carries_the_autouse_class_level_backoff_stub():
    text = (TESTS_DIR / "conftest.py").read_text()
    fixture_idx = text.find("def _stub_wait_out_backoff_at_class_level")
    assert fixture_idx != -1, (
        "tests/conftest.py must carry an autouse fixture named "
        "_stub_wait_out_backoff_at_class_level (task 047, B.4 cure) -- the "
        "class-level stub that replaced this guard's old per-module scan")
    preceding = text[max(0, fixture_idx - 300):fixture_idx]
    assert "@pytest.fixture(autouse=True)" in preceding, (
        "_stub_wait_out_backoff_at_class_level must be autouse, or it never "
        "runs for the ~43 modules this cure exists to cover")
    body = text[fixture_idx:fixture_idx + 1500]
    assert "LoopSupervisor._wait_out_backoff =" in body, (
        "the fixture must assign LoopSupervisor._wait_out_backoff at CLASS "
        "level (LoopSupervisor._wait_out_backoff = ...), not merely an "
        "instance's -- an instance-level assignment would not reach a bare "
        "LoopSupervisor(...) construction elsewhere")
    assert "REAL_BACKOFF_MARKER" in body, (
        "the fixture must consult the registered opt-out marker so the "
        "three exempt modules can still exercise the real backoff")


# ---------------------------------------------------------------------------
# 2. Exempt modules + the opt-out marker: present in exactly the three, in
#    exactly the three -- checked together because they are one contract
#    (the PRD's own named list IS the marker's whole authorized surface).

_REAL_BACKOFF_MARK_RE = re.compile(r"pytest\.mark\.real_backoff")


def test_exempt_modules_are_the_prds_named_three_and_carry_the_marker():
    names = {p.name for p in TESTS_DIR.glob("test_*.py")}
    for exempt in EXEMPT_MODULES:
        assert exempt in names, f"exempt module {exempt} no longer exists on disk"
    assert EXEMPT_MODULES == {
        "test_infra_retry_schedule.py", "test_retry_now.py",
        "test_reflect_infra_retry.py"}
    marked = {p.name for p in _all_files()
              if _REAL_BACKOFF_MARK_RE.search(p.read_text())}
    assert marked == EXEMPT_MODULES, (
        "@pytest.mark.real_backoff (tests/conftest.py's opt-out from the "
        "autouse class-level backoff stub) must be used by exactly the "
        f"three exempt modules: found it in {sorted(marked)}, wanted "
        f"{sorted(EXEMPT_MODULES)}")


# ---------------------------------------------------------------------------
# 3. LiveRun.__init__-bypassing job.yaml writers: the one shape the
#    in-process class-level fixture cannot reach (a separate process).

def job_yaml_writer_violations_in_text(text: str, label: str) -> list[str]:
    """The check's whole logic for this half, factored out so the
    non-vacuity proof below can feed it synthetic snippets without touching
    disk. A file that never bypasses `LiveRun.__init__` (the overwhelming
    majority of the suite, including every ordinary `LiveRun(...)` call) is
    not scanned at all -- the shared launcher's own `_with_zero_backoff`
    already covers that path."""
    if not (_LIVERUN_BYPASS_RE.search(text) and _JOB_YAML_LITERAL_RE.search(text)):
        return []
    # `_with_zero_backoff(job, stub_env)` (tests/conftest.py's own helper)
    # computes both knobs programmatically -- a writer that calls it never
    # spells either key out literally, so a call to it satisfies both.
    zeroed_by_helper = "_with_zero_backoff(" in text
    missing = []
    if not zeroed_by_helper and not _ZERO_BACKOFF_S_RE.search(text):
        missing.append("infra_retry_backoff_s: [0.0]")
    if not zeroed_by_helper and not _ZERO_BACKOFF_MAX_RE.search(text):
        missing.append("infra_retry_backoff_max_s: 0.0")
    if not _STUB_SLEEP_ZERO_RE.search(text):
        missing.append("STUB_SLEEP=0")
    if not missing:
        return []
    return [(f"{label}: LiveRun.__init__-bypassing job.yaml writer omits "
             f"{', '.join(missing)}")]


def test_no_livrun_bypassing_job_yaml_writer_omits_the_zero_backoff_triple():
    all_violations: list[str] = []
    for path in _scanned_files():
        all_violations.extend(
            job_yaml_writer_violations_in_text(path.read_text(), path.name))
    assert not all_violations, (
        "the following tests/*.py modules spawn a real engine through a "
        "job.yaml writer that bypasses LiveRun.__init__ without pinning the "
        "zero-backoff triple (B.4) -- name all three, or route the launch "
        "through LiveRun.__init__ / _with_zero_backoff instead:\n"
        + "\n".join(all_violations))


@pytest.mark.parametrize("snippet,expect_violation", [
    # Bypasses LiveRun.__init__, writes job.yaml, missing infra_retry_backoff_s.
    (('e = LiveRun.__new__(LiveRun)\n'
      '(config_dir / "job.yaml").write_text(text)\n'
      'env = {"STUB_SLEEP": "0", **stub_env}\n'
      'job = {"infra_retry_backoff_max_s": 0.0}\n'), True),
    # ... missing infra_retry_backoff_max_s.
    (('e = LiveRun.__new__(LiveRun)\n'
      '(config_dir / "job.yaml").write_text(text)\n'
      'env = {"STUB_SLEEP": "0", **stub_env}\n'
      'job = {"infra_retry_backoff_s": [0.0]}\n'), True),
    # ... missing STUB_SLEEP.
    (('e = LiveRun.__new__(LiveRun)\n'
      '(config_dir / "job.yaml").write_text(text)\n'
      'job = {"infra_retry_backoff_s": [0.0], "infra_retry_backoff_max_s": 0.0}\n'), True),
    # All three present: clean.
    (('e = LiveRun.__new__(LiveRun)\n'
      '(config_dir / "job.yaml").write_text(text)\n'
      'env = {"STUB_SLEEP": "0", **stub_env}\n'
      'job = {"infra_retry_backoff_s": [0.0], "infra_retry_backoff_max_s": 0.0}\n'), False),
])
def test_the_checker_actually_fires_on_the_shapes_it_claims_to_catch(
        snippet, expect_violation):
    """Proof the check is not vacuous: a planted violation of each shape is
    caught by feeding the checker a synthetic snippet (never written to
    disk, so there is nothing to remove afterwards), and the clean shape
    clears it."""
    got = job_yaml_writer_violations_in_text(snippet, "synthetic")
    assert bool(got) is expect_violation, got


# ---------------------------------------------------------------------------
# 4. B.4's first clause, unchanged since task 010's third pass: the
#    stub-`pi` (subprocess) side of "no real sleeps".

# Every module that names a NON-ZERO `RALPHD_INFRA_RETRY_BACKOFF_S` /
# `RALPHD_INFRA_RETRY_BACKOFF_MAX_S` -- the only two knobs a spawned engine
# reads, and therefore the only ones that can buy a real sleep in a stub-`pi`
# test -- with the reason it cannot be zero. Exactly one module qualifies, and
# it launches no engine at all. An in-process module configuring a non-zero
# schedule is NOT on this list and does not need to be: its `_wait_out_backoff`
# is stubbed at the class level (the fixture above), so the numbers are
# reported, never waited.
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
