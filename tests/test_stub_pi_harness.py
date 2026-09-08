"""Feature module for the subject below (task 028, requirement D.7): the
per-task pin modules listed here are folded in whole -- every test body,
assertion and docstring verbatim, only the module they live in changed.
Sources absorbed (each was `tests/<name>`, deleted in the same commit, with
one `docs/test-suite-audit.md` § "Subsumed" row per node):

- `test_stub_pi_hold_primitive_task007.py` -- Pins the stub `pi`'s bounded, env-driven hold primitive (task 007, B.1). `STUB_HOLD_FILE`/`STUB_HOLD_TIMEOUT_S` let a test open a real race window inside a stub `pi` invocation instead of guessing a fixed `STUB_SLEEP` duration. This module invokes `tests/stub-pi/pi` directly (no engine involved) and
- `test_wait_out_backoff_stubbed_task010.py` -- Task 010 (B.4), cured by task 047 (operator steer 004): no in-process test's `LoopSupervisor._wait_out_backoff` is reachable as a real, waiting coroutine, and no `LiveRun.__init__`-bypassing `job.yaml` writer buys a spawned engine a real backoff/STUB_SLEEP sleep either. Task 010's own pass accepted 

Renamed to resolve cross-source collisions of module-private names (no test
function was renamed):
- none: no module-level name was defined by two sources.
"""

from __future__ import annotations


# ----------------------------------------------------------------------
# Folded from tests/test_stub_pi_hold_primitive_task007.py (task 028, requirement D.7).
# ----------------------------------------------------------------------

import os
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
STUB_PI = REPO / "tests" / "stub-pi" / "pi"

# The stub's own documented exit code for "the hold was never released".
HOLD_TIMEOUT_EXIT_CODE = 3

# Stated margin for every "duration must be within the bound" assertion in
# this module -- generous enough to absorb interpreter startup and process
# scheduling jitter on a loaded CI host, small enough that a regression
# back to something sleep-shaped (e.g. `time.sleep(9999)`) still fails it.
MARGIN_S = 2.0


def _run_stub(run_dir: Path, env_extra: dict) -> tuple[float, subprocess.CompletedProcess]:
    env = {**os.environ, "STUB_RUN_DIR": str(run_dir), **env_extra}
    start = time.monotonic()
    proc = subprocess.run(
        [sys.executable, str(STUB_PI)],
        input="unrecognized phase, no Role marker here\n",
        capture_output=True, text=True, env=env, timeout=30,
    )
    wall = time.monotonic() - start
    return wall, proc


def test_hold_releases_promptly_when_the_test_touches_the_file(tmp_path):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    hold_file = tmp_path / "hold-release"

    # Release the hold in a background thread shortly after launch, mirroring
    # how a real test would release it once it has observed whatever it was
    # waiting to observe mid-iteration.
    import threading

    def _release():
        time.sleep(0.2)
        hold_file.touch()

    threading.Thread(target=_release, daemon=True).start()

    wall, proc = _run_stub(run_dir, {
        "STUB_HOLD_FILE": str(hold_file),
        "STUB_HOLD_TIMEOUT_S": "5",
    })
    assert proc.returncode == 0, proc.stderr
    assert '"type": "message_end"' in proc.stdout

    duration_file = Path(f"{hold_file}.stub-hold-duration")
    assert duration_file.exists()
    duration = float(duration_file.read_text())

    # Released around 0.2s in -- nowhere near the 5s fallback bound.
    assert duration < 1.0, f"released hold should return promptly, got {duration}s"
    assert wall < 1.0 + MARGIN_S


def test_hold_never_released_fails_on_the_bound_not_by_hanging(tmp_path):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    hold_file = tmp_path / "hold-never-released"
    bound = 1.5

    wall, proc = _run_stub(run_dir, {
        "STUB_HOLD_FILE": str(hold_file),
        "STUB_HOLD_TIMEOUT_S": str(bound),
    })
    # Reaching the bound is a FAILURE, not a silent continue: nonzero exit,
    # a stderr line naming the bound, and no role payload emitted at all.
    assert proc.returncode == HOLD_TIMEOUT_EXIT_CODE, (proc.returncode, proc.stderr)
    assert "never released" in proc.stderr
    assert "STUB_HOLD_TIMEOUT_S" in proc.stderr
    assert str(bound) in proc.stderr
    assert '"type": "message_end"' not in proc.stdout

    duration_file = Path(f"{hold_file}.stub-hold-duration")
    assert duration_file.exists()
    duration = float(duration_file.read_text())

    assert duration >= bound, "must have actually waited out the fallback"
    assert duration <= bound + MARGIN_S, (
        f"a never-released hold must fail fast on the bound ({bound}s) plus "
        f"a stated margin ({MARGIN_S}s), took {duration}s")
    assert wall <= bound + MARGIN_S


INNER_TEST = '''
import os, subprocess, sys, time
from pathlib import Path


def test_forgets_to_release_the_hold(tmp_path):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    hold_file = tmp_path / "never-touched"
    env = {**os.environ, "STUB_RUN_DIR": str(run_dir),
           "STUB_HOLD_FILE": str(hold_file),
           "STUB_HOLD_TIMEOUT_S": "1.0"}
    proc = subprocess.run(
        [sys.executable, %(stub)r],
        input="unrecognized phase\\n", capture_output=True, text=True,
        env=env, timeout=30,
    )
    proc.check_returncode()   # red on the bound, never a hang
'''


def test_a_test_that_never_releases_the_hold_goes_red_on_the_bound(tmp_path):
    """End-to-end: forgetting the release makes the TEST fail, on the bound.

    The inner pytest run (its own process, its own tmp dir) contains a test
    that opens a hold and never releases it. It must come back FAILED --
    not passed, not still running -- and the whole nested run must finish
    inside the hold's own bound plus this module's stated margin plus a
    small allowance for pytest's own start-up.
    """
    inner = tmp_path / "test_inner_forgotten_hold.py"
    inner.write_text(INNER_TEST % {"stub": str(STUB_PI)})

    start = time.monotonic()
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
         "-p", "no:randomly", str(inner)],
        capture_output=True, text=True, cwd=str(tmp_path), timeout=120,
        env={**os.environ, "PYTEST_ADDOPTS": ""},
    )
    wall = time.monotonic() - start

    assert proc.returncode != 0, f"inner run should have failed:\n{proc.stdout}"
    assert "1 failed" in proc.stdout, proc.stdout
    assert "never released" in proc.stdout, proc.stdout
    # Bound (1.0s) + this module's stated margin + a pytest start-up
    # allowance: a regression to a sleep-shaped or unbounded hold blows this.
    assert wall <= 1.0 + MARGIN_S + 30.0, f"nested run took {wall}s"


def test_hold_is_a_noop_when_unset(tmp_path):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    wall, proc = _run_stub(run_dir, {})
    assert proc.returncode == 0, proc.stderr
    assert wall < MARGIN_S


def test_help_text_documents_the_env_var_and_the_fallback_bound():
    source = STUB_PI.read_text()
    assert "STUB_HOLD_FILE" in source
    assert "STUB_HOLD_TIMEOUT_S" in source
    # The docstring itself (this stub's own help/usage text) names both the
    # release mechanism and the numeric default fallback bound.
    doc = source.split('"""', 2)[1]
    assert "STUB_HOLD_FILE" in doc
    assert "STUB_HOLD_TIMEOUT_S" in doc
    assert "5.0" in doc
    # The doc must state that reaching the bound FAILS the invocation, not
    # that it continues quietly.
    assert "EXITS NONZERO" in doc
    assert "exit code 3" in doc


# ----------------------------------------------------------------------
# Folded from tests/test_wait_out_backoff_stubbed_task010.py (task 028, requirement D.7).
# ----------------------------------------------------------------------

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
# test -- with the reason it cannot be zero. An in-process module configuring
# a non-zero schedule is NOT on this list and does not need to be: its
# `_wait_out_backoff` is stubbed at the class level (the fixture above), so
# the numbers are reported, never waited. A module IS on this list only when
# its subject is the retry-backoff ladder itself, spent across a real process
# boundary the in-process stub cannot reach, and the value is small (well
# under a second per node): the two named flow modules keep every LARGER
# schedule under `@pytest.mark.nightly` already (task 048's B.5 follow-on) --
# this list is exactly the small remainder that stayed in the fast tier.
NONZERO_BACKOFF_ALLOWLIST = {
    "test_job_config_defaults.py":
        "no engine is launched at all: monkeypatched env, read back through "
        "in-process `JobConfig.load` -- the parsing of the two knobs is the "
        "subject",
    "test_flow_infra_ladder.py":
        "the flow's whole subject (task 015, requirement C.4) is the infra-"
        "retry ladder's booked-wait arithmetic (`infraWaitTotalS` == the "
        "backoffs actually spent, e.g. 0.05+0.1=0.15) over a real `ralphd-"
        "engine` subprocess -- the in-process class-level `_wait_out_backoff` "
        "stub cannot reach a spawned process, so proving the number booked "
        "matches the schedule configured needs a real, small wait; every "
        "schedule that would run longer is already `@pytest.mark.nightly`, "
        "out of this fast-tier scan's own -m fast selection",
    "test_flow_crash_resume_refunds.py":
        "the same infra-retry-ladder subject as test_flow_infra_ladder.py "
        "(task 016, requirement C.5), here combined with the crash/resume "
        "refund accounting: one hung attempt must be booked and refunded "
        "before the crash, which needs a real (0.05s) backoff wait over the "
        "same real subprocess boundary",
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
    by_name = {p.name: p for p in _all_files()}
    for name, reason in NONZERO_BACKOFF_ALLOWLIST.items():
        assert reason.strip(), f"{name} is allowlisted with no reason"
        path = by_name.get(name)
        if path is None or not nonzero_env_backoffs(path.read_text()):
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
