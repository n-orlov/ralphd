"""Shared pytest fixtures for black-box CLI tests that need a real,
directly-launched `ralphd-engine` (no Docker) behind `ralphctl` — the
"live test engine" pattern.
"""

from __future__ import annotations

import json
import os
import shutil
import site
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

REPO = Path(__file__).parent.parent
STUB_PI = REPO / "tests" / "stub-pi"

# --- the child's import path under an overridden HOME (#67) ----------------
# 19 modules launch the engine with HOME pointed at a tmp dir, deliberately:
# a virgin $HOME is the whole subject of the creds/skills/prompts/llm tests
# (where ~/.pi/agent/models.json lands, where credentials are placed, that
# nothing pre-existing is read). On a `pip install --user -e .` layout that
# override also un-installs the world for the child, because user
# site-packages is $HOME/.local/lib/pythonX.Y/site-packages and `site`
# derives it from HOME at interpreter start. The child dies with
# ModuleNotFoundError before it binds a port -- and the test never sees the
# traceback, because the engine's stdout is a PIPE nothing reads until the
# test is already failing. The symptom is `TimeoutError: engine API never
# came up`, 15 seconds later, in a fixture, with no mention of imports.
# Measured on tests/test_cli_creds_crud.py before this fix: 1 passed, 7
# errors in 105s, identical under an interpreter with the console scripts
# beside it -- so this is a separate defect from #65's script-location bug
# and no virtualenv hides it.
#
# TWO variables are needed, and the reason is worth stating because setting
# only the obvious one leaves the bug in place:
#
#   PYTHONUSERBASE is the load-bearing half. HOME does not just hide
#   `ralphd`; it hides everything installed with it. `--version` on this box
#   with HOME overridden and PYTHONPATH=<repo>/src set gets exactly as far as
#   `src/ralphd/engine/main.py:13` and dies on `import uvicorn` -- uvicorn,
#   fastapi and pyyaml all live in the same user site-packages. Pinning
#   PYTHONUSERBASE to the value this interpreter resolved *before* the
#   override (site.getuserbase(), which reads the real HOME) restores the
#   whole set at once, and is inert on any layout where user site is not in
#   play (a venv disables it outright).
#
#   PYTHONPATH=<repo>/src is not needed for importability once the above is
#   set, and is added for a different reason: it decides *which* build the
#   child imports. These are black-box tests of this tree, so the child
#   should load this checkout's `ralphd`, not whichever build happens to sit
#   in site-packages -- which under a non-editable install would be a stale
#   wheel, and the tests would silently pass or fail against code that is not
#   the code under review. Prepended, never assigned, because
#   scripts/host_like_pytest.sh runs the suite with a decoy `tests` package
#   on $PYTHONPATH and clobbering it would quietly disarm that scenario.
#
# Applied at every point the suite spawns this repo's code, not only the
# engine: `ralphctl` is the same package behind a second console script, so
# tests/test_cli_docker.py's `Ctl.run` (which lets callers pass a HOME, and
# test_cli_resume_llm_wiring.py does) needs it just as much.
REPO_SRC = REPO / "src"
# Resolved once, at import, from the pytest process's own (real) HOME --
# reading it later would be reading it through whatever override the caller
# just applied.
_USER_BASE = site.getuserbase()


def with_ralphd_importable(env: dict[str, str]) -> dict[str, str]:
    """`env` made safe for an engine child even when it overrides HOME (#67):
    user site-packages pinned by absolute path, and this checkout's `src/`
    ahead of it on $PYTHONPATH.

    The PYTHONPATH half is skipped when there is no `src/ralphd` beside these
    tests (a wheel-only layout), where there is no checkout to prefer and
    naming a non-existent directory would only mislead whoever debugs the next
    import failure. PYTHONUSERBASE is set unconditionally: it costs nothing
    where user site is disabled, and guessing wrong about the layout is the
    whole of this issue.
    """
    out = {**env, "PYTHONUSERBASE": _USER_BASE}
    if (REPO_SRC / "ralphd").is_dir():
        inherited = env.get("PYTHONPATH")
        out["PYTHONPATH"] = os.pathsep.join(
            [str(REPO_SRC), *([inherited] if inherited else [])])
    return out


# --- console-script resolution (#65) --------------------------------------
# Every shell-out in this suite needs the absolute path of a console script
# installed from this repo (`ralphctl`, `ralphd-engine`). What used to stand
# here was the one-liner `Path(sys.executable).parent / "ralphctl"`, which is
# only correct when the interpreter and the script share a `bin/` -- true
# inside a virtualenv, false for `pip install --user -e .` against a system
# interpreter: there the interpreter is /usr/bin/python3 while the script is
# ~/.local/bin/ralphctl, so the guess became /usr/bin/ralphctl and every one
# of the ~45 modules that shells out died on FileNotFoundError.
#
# It died *slowly*, which is the part worth engineering against: the argv
# never reaches an exec, so LiveRun.wait_api()/wait_terminal() below just
# poll a status.json nobody will ever write until their own timeouts expire.
# A wrong layout therefore costs hours of wall clock before it reports
# anything, and reports it as a timeout rather than as "the script is not
# where I looked". Hence _startup_error()/pytest_configure() below: one
# clear failure at session start, naming every location searched.
#
# The order in _script_candidates() is load-bearing:
#   1. $RALPHD_TEST_<SCRIPT> -- an explicit operator override outranks any
#      guess (the same escape hatch PYTHON= gives scripts/host_like_pytest.sh)
#      and, when set, is *authoritative*: a typo must fail loudly naming
#      itself rather than silently fall through to the copy the operator was
#      trying to bypass.
#   2. beside the interpreter -- this must beat $PATH, not the other way
#      round: inside a venv the venv's own script is the build under test,
#      while a stale ~/.local/bin copy can easily sit earlier on $PATH. A
#      bare shutil.which() would quietly test the wrong build, so it is only
#      ever consulted as step 3.
#   3. on $PATH -- the `--user` layout, where the script exists nowhere near
#      the interpreter and $PATH is the only thing that knows about it.
SCRIPTS = ("ralphctl", "ralphd-engine")


def _override_var(name: str) -> str:
    """The override environment variable for a console script, *derived* from
    the script name (ralphctl -> RALPHD_TEST_RALPHCTL) so the two can never
    drift apart as scripts are added."""
    return "RALPHD_TEST_" + name.upper().replace("-", "_")


def _script_candidates(name: str, interpreter_dir: Path,
                       environ) -> list[tuple[str, Path | None]]:
    """Where `name` is looked for, in order, as (description, path) pairs.

    Single source of truth for both the search and the error message, so a
    candidate cannot be searched without also being reported (or reported
    without being searched). `path` is None when that source says nothing --
    e.g. `ralphctl` is not on $PATH at all.
    """
    var = _override_var(name)
    override = environ.get(var)
    if override:
        return [(f"${var}", Path(override))]  # authoritative -- see above
    search_path = environ.get("PATH")
    on_path = shutil.which(name, path=search_path) if search_path else None
    return [
        ("beside the interpreter", interpreter_dir / name),
        ("on $PATH", Path(on_path) if on_path else None),
    ]


def _resolve_script(name: str, interpreter_dir: Path, environ) -> Path | None:
    """First candidate that actually exists, or None if none of them do."""
    for _description, candidate in _script_candidates(name, interpreter_dir, environ):
        if candidate is not None and candidate.exists():
            return candidate
    return None


def _resolve_ralphctl(interpreter_dir: Path, environ) -> Path | None:
    return _resolve_script("ralphctl", interpreter_dir, environ)


def _resolve_engine(interpreter_dir: Path, environ) -> Path | None:
    return _resolve_script("ralphd-engine", interpreter_dir, environ)


def _startup_error(interpreter_dir: Path, environ) -> str | None:
    """None when every script in SCRIPTS resolves; otherwise the whole
    complaint as one string, naming each location searched for each missing
    script plus the override that would fix it."""
    problems = []
    for name in SCRIPTS:
        if _resolve_script(name, interpreter_dir, environ) is not None:
            continue
        var = _override_var(name)
        lines = [f"cannot find the `{name}` console script this suite shells out to."
                 " Searched, in order:"]
        for description, candidate in _script_candidates(name, interpreter_dir, environ):
            lines.append(f"  - {description}: {candidate if candidate else '(nothing)'}")
        lines.append(f"Install this repo (`pip install -e .`) into the interpreter"
                     f" running pytest ({sys.executable}), or set"
                     f" {var}=/abs/path/to/{name} to point the suite at a"
                     f" specific build. See #65.")
        problems.append("\n".join(lines))
    return "\n\n".join(problems) if problems else None


_INTERPRETER_DIR = Path(sys.executable).parent

# Deliberately still a plain `Path`, never None: ~25 modules do
# `from tests.conftest import RALPHCTL` and `str(RALPHCTL)` straight into an
# argv, and tests/test_tests_conftest_resolution.py asserts on its value, so
# the unresolved case must not change this name's type. The fallback below is
# only ever reached when pytest_configure() is about to abort the session
# anyway, where it serves purely as the "beside the interpreter" label.
RALPHCTL = _resolve_ralphctl(_INTERPRETER_DIR, os.environ) or _INTERPRETER_DIR / "ralphctl"
RALPHD_ENGINE = _resolve_engine(_INTERPRETER_DIR, os.environ) or _INTERPRETER_DIR / "ralphd-engine"


def pytest_configure(config):
    """Turn a missing console script into one immediate, self-explaining
    session error (#65) instead of ~45 modules' worth of subprocess
    FileNotFoundErrors that only surface hours later as fixture timeouts.

    Deliberately NOT accompanied by a warning about the interpreter-adjacent
    guess being wrong. An earlier revision emitted a PytestConfigWarning
    whenever `ralphctl` resolved away from the interpreter's own bin/, because
    ~19 modules still computed `Path(sys.executable).parent / "ralphctl"` for
    themselves and no conftest can reach a constant another module evaluates
    at its own import time. Those modules now all import RALPHCTL from here,
    so the condition it fired on -- a `pip install --user -e .` layout -- is
    simply a supported layout, and a warning on every session of a supported
    layout is noise that trains operators to ignore warnings."""
    problem = _startup_error(_INTERPRETER_DIR, os.environ)
    if problem:
        raise pytest.UsageError(problem)
    # Task 003 (#83 family): the one opt-out from the autouse in-process HOME
    # isolation fixture below (`_isolate_ambient_home_in_process`) -- reuses
    # the exact `AMBIENT_HOME` spelling as a marker name so there is still
    # only one greppable token for "inherit the real ambient $HOME", not a
    # second one under a different name.
    config.addinivalue_line(
        "markers",
        "AMBIENT_HOME: opt this test out of the autouse in-process HOME "
        "isolation fixture, so it (and anything it calls in-process, e.g. "
        "amain()) sees the real ambient $HOME -- the same opt-in spelling "
        "as the AMBIENT_HOME sentinel used for spawned-child stub_env",
    )


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# --- default HOME isolation for spawned engines (#83) ---------------------
# `place_skills()` (engine/skills.py) and `check_hat_routes()` (llm.py) both
# read/rebuild state under `$HOME` unconditionally when no `home=` override
# reaches them, and a child engine's env used to default to the ambient
# `$HOME` of whatever process is running pytest -- so a test that forgot to
# pass a HOME override in `stub_env` could clobber a human's real
# `~/.pi/agent/skills/` (tests/test_skills_placement_ambient_home_loss.py).
#
# The fix is symmetric with the run/config/workspace dirs already built here:
# a per-instance directory inside the test's own tmp tree, used unless the
# caller opts in to the real ambient HOME. There is exactly ONE opt-in
# spelling, `AMBIENT_HOME`, passed as `stub_env={"HOME": AMBIENT_HOME}` --
# never a second one (e.g. copying `os.environ["HOME"]` by hand), so every
# call site that genuinely needs the operator's own HOME is greppable by
# that one name and nothing else silently reproduces the old default.
AMBIENT_HOME = "<<ralphd-tests: inherit the real ambient $HOME (opt-in)>>"


def _resolved_home_env(default_home: Path, stub_env: dict | None) -> dict:
    """`stub_env` with `HOME` resolved for a spawned engine/ralphctl child.

    - `stub_env["HOME"] == AMBIENT_HOME` (the one opt-in): swapped for the
      real, ambient `$HOME` of the process running pytest.
    - `stub_env` already names an explicit `HOME` (any other value, e.g. the
      ~19 modules that isolate their own fixture home for creds/skills/
      prompts/llm coverage): left exactly as given.
    - neither: `default_home` is created (parents ok, idempotent) and used --
      the new default, isolating this instance from the ambient `$HOME`
      without the caller having to ask.
    """
    stub_env = dict(stub_env or {})
    if stub_env.get("HOME") == AMBIENT_HOME:
        stub_env["HOME"] = os.environ["HOME"]
    elif "HOME" not in stub_env:
        default_home.mkdir(parents=True, exist_ok=True)
        stub_env["HOME"] = str(default_home)
    return stub_env


# --- default HOME isolation for the pytest PROCESS itself (task 003) ------
# `_resolved_home_env()` above only isolates a *spawned* child's HOME. It
# does nothing for code that runs in-process during a test -- most notably
# `engine/main.py`'s `amain()`, which several unit tests
# (tests/test_engine_budget_seed.py, tests/test_status_max_approaches.py)
# call directly, with no subprocess involved at all. `amain()` calls
# `place_skills(CONFIG_DIR, home=ambient_home())`, and `ambient_home()`
# reads `os.environ["HOME"]` -- the pytest PROCESS's own $HOME -- whenever
# a test doesn't monkeypatch it away first. Before this fixture, a test with
# no `monkeypatch.setenv("HOME", ...)` of its own ran `amain()` against
# whatever real $HOME the operator running pytest actually has, and
# `place_skills`'s clear-then-fill wiped that real `~/.pi/agent/skills/` --
# the same #83 loss task 001/002 closed for spawned children, left open here
# for in-process calls. Task 002's attribution named the culprits: all four
# `tests/test_engine_budget_seed.py` nodes and
# `tests/test_status_max_approaches.py::test_the_first_status_write_already_carries_the_denominator`.
#
# The fix is the same shape as `_resolved_home_env` above, applied to the
# pytest process's own environment instead of a child's: isolate `HOME` to a
# throwaway directory by default, for every test in this suite (autouse),
# with exactly one opt-out -- reusing the `AMBIENT_HOME` name, this time as
# a marker (registered in `pytest_configure` above) rather than a sentinel
# value, so there is still only one greppable spelling of "inherit the real
# ambient $HOME" and not a second one under a different name. A test that
# overrides `HOME` itself afterwards (`monkeypatch.setenv("HOME", ...)`, as
# tests/test_skills_placement_ambient_home_loss.py and
# tests/test_skills_placement_requires_home.py already do) is unaffected
# either way: its own later call still wins, because this fixture only ever
# runs before the test body does.
@pytest.fixture(autouse=True)
def _isolate_ambient_home_in_process(request, monkeypatch, tmp_path_factory):
    if request.node.get_closest_marker("AMBIENT_HOME"):
        return
    isolated = tmp_path_factory.mktemp("ambient-home-in-process")
    monkeypatch.setenv("HOME", str(isolated))


class LiveRun:
    """A real ralphd-engine, wired so `ralphctl` (pointed at `registry`)
    can talk to it as if a container had been started normally."""

    def __init__(self, tmp: Path, run_id: str, job: dict, stub_env: dict | None = None):
        self.run_id = run_id
        self.registry = tmp / "registry"
        self.run_dir = self.registry / "runs" / run_id
        self.config_dir = self.registry / "configs" / run_id
        self.workspace = tmp / "ws" / run_id
        self.home = tmp / "home" / run_id
        for d in (self.run_dir, self.config_dir, self.workspace):
            d.mkdir(parents=True, exist_ok=True)
        (self.config_dir / "prd.md").write_text("# CLI logs test PRD\n\nDo the thing.\n")
        (self.config_dir / "job.yaml").write_text(
            "".join(f"{k}: {json.dumps(v)}\n" for k, v in job.items()))
        self.port = free_port()
        # The engine's PATH: STUB_PI stays *first*, because tests depend on the
        # stub `pi` shadowing any real one the operator has installed. After it
        # come the two directories that can hold this repo's own scripts -- the
        # resolved engine's (#65: on a `--user` layout that is ~/.local/bin,
        # not the interpreter's bin) and the interpreter's -- and only then the
        # ambient PATH. Before #65 the engine's own directory was absent here
        # and `ralphd-engine` was spawned by bare name, so the spawn worked
        # only when the ambient PATH happened to carry ~/.local/bin.
        # with_ralphd_importable() wraps the *merged* dict, after stub_env:
        # stub_env is where the HOME override arrives (#67), so the two
        # variables that have to survive it are applied last rather than
        # buried in the defaults where a caller could shadow them by accident.
        env = with_ralphd_importable({
            **os.environ,
            "PATH": ":".join([str(STUB_PI), str(RALPHD_ENGINE.parent),
                              str(_INTERPRETER_DIR), os.environ["PATH"]]),
            "STUB_RUN_DIR": str(self.run_dir),
            "RALPHD_RUN_DIR": str(self.run_dir),
            "RALPHD_CONFIG_DIR": str(self.config_dir),
            "RALPHD_WORKSPACE_DIR": str(self.workspace),
            "RALPHD_PORT": str(self.port),
            **_resolved_home_env(self.home, stub_env),
        })
        self.proc = subprocess.Popen([str(RALPHD_ENGINE)], env=env,
                                     stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                     text=True)
        (self.run_dir / "host.json").write_text(json.dumps({
            "runId": run_id, "container": "f" * 12, "port": self.port,
            "apiUrl": f"http://127.0.0.1:{self.port}", "image": "n/a",
        }))

    def wait_terminal(self, timeout=60) -> dict:
        deadline = time.time() + timeout
        status = {}
        while time.time() < deadline:
            sf = self.run_dir / "status.json"
            if sf.exists():
                try:
                    status = json.loads(sf.read_text())
                except json.JSONDecodeError:
                    status = {}
            if status.get("state") in ("succeeded", "failed", "aborted"):
                return status
            time.sleep(0.2)
        raise TimeoutError(f"job never finished; last status: {status}")

    def ralphctl(self, *argv: str) -> subprocess.CompletedProcess:
        # Wrapped too, not only the engine: `ralphctl` is the same package
        # behind a different console script, so any test that hands it a
        # different HOME hides `ralphd` from it in exactly the same way (#67).
        env = with_ralphd_importable(
            {**os.environ, "RALPHD_REGISTRY": str(self.registry)})
        return subprocess.run([str(RALPHCTL), *argv], env=env,
                              capture_output=True, text=True, timeout=60)

    def wait_api(self, timeout=15):
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                req = urllib.request.Request(
                    f"http://127.0.0.1:{self.port}/healthz")
                with urllib.request.urlopen(req, timeout=2):
                    return
            except (urllib.error.URLError, ConnectionError, TimeoutError):
                time.sleep(0.2)
        raise TimeoutError("engine API never came up")

    def stop(self):
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.proc.kill()


@pytest.fixture
def live(tmp_path):
    """Factory fixture: `live(run_id=..., job=..., stub_env=...)` -> LiveRun.
    All runs created are stopped at teardown."""
    runs = []

    def make(run_id="logtest", job=None, stub_env=None):
        defaults = {"run_id": run_id, "iterations": 12, "max_approaches": 3,
                    "on_complete": "idle"}
        r = LiveRun(tmp_path, run_id, {**defaults, **(job or {})}, stub_env)
        runs.append(r)
        return r

    yield make
    for r in runs:
        r.stop()
