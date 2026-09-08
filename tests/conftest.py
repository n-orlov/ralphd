"""Shared pytest fixtures for black-box CLI tests that need a real,
directly-launched `ralphd-engine` (no Docker) behind `ralphctl` — the
"live test engine" pattern.
"""

from __future__ import annotations

import asyncio
import errno
import functools
import json
import os
import re
import shutil
import site
import socket
import subprocess
import sys
import threading
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


def call_ralphctl_in_process(argv: list[str], *, env: dict | None,
                              capsys, cwd: Path | None = None,
                              ) -> subprocess.CompletedProcess:
    """Drive `ralphctl` through `cli.main.main()` in this same process (task
    036/037, requirement F.1/F.2) instead of spawning the real console
    script -- for every black-box CLI test whose subject is the CLI's own
    argument parsing / rendering, never the process boundary itself.

    Swaps `sys.argv` and the *whole* of `os.environ` for the one call (most
    callers built `env` as a full replacement dict the way `subprocess.run`
    wanted it, never a delta), restores both in a `finally`, and returns a
    real `subprocess.CompletedProcess` so every existing
    `res.returncode`/`res.stdout`/`res.stderr` assertion keeps reading it
    exactly as it read the spawned process's. `main()` never returns a code
    on success (`sys.exit` is only called for a nonzero/explicit exit); a
    bare return -- or any exception other than `SystemExit` -- is exit 0/an
    uncaught error respectively, matched to the same shape a real process
    exit would have taken.
    """
    from ralphd.cli import main as cli_main

    # `DOCKER`/`IMAGE_ENV` are read from `os.environ` ONCE, at module import
    # time (main.py's two module-level `RALPHD_DOCKER`/`RALPHD_IMAGE` reads) --
    # never per-call. A real subprocess re-imports the module fresh every
    # time, so this never mattered there; in-process it means an `env=`
    # override is silently ignored and the REAL `docker` binary runs instead
    # of a test's recording stub (caught the hard way: task 036 first spawned
    # five real sibling containers on the host daemon before this patch was
    # added -- cleaned up by exact container id, never by label/pattern).
    # Patched here, not just `os.environ`, and restored in the same `finally`.
    old_argv = sys.argv
    old_environ = dict(os.environ)
    old_docker = cli_main.DOCKER
    old_image_env = cli_main.IMAGE_ENV
    old_cwd = Path.cwd() if cwd is not None else None
    try:
        if env is not None:
            os.environ.clear()
            os.environ.update(env)
        cli_main.DOCKER = os.environ.get("RALPHD_DOCKER", "docker")
        cli_main.IMAGE_ENV = os.environ.get("RALPHD_IMAGE") or None
        sys.argv = ["ralphctl", *argv]
        if cwd is not None:
            os.chdir(cwd)
        code = 0
        try:
            cli_main.main()
        except SystemExit as exc:
            if exc.code is None:
                code = 0
            elif isinstance(exc.code, int):
                code = exc.code
            else:
                code = 1
        out = capsys.readouterr()
        return subprocess.CompletedProcess(list(sys.argv), code, out.out, out.err)
    finally:
        sys.argv = old_argv
        os.environ.clear()
        os.environ.update(old_environ)
        cli_main.DOCKER = old_docker
        cli_main.IMAGE_ENV = old_image_env
        if old_cwd is not None:
            os.chdir(old_cwd)


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
    # Task 040 (steer 002, #83-shaped): the one opt-in from the autouse
    # RALPHD_NOTIFIER_URL scrub below (`_scrub_notifier_env_in_process`) --
    # a test that genuinely wants to see the ambient notifier URL in its
    # own os.environ (in-process JobConfig.load() call) marks itself with
    # this and nothing else, so there stays exactly one greppable spelling
    # for the opt-in.
    config.addinivalue_line(
        "markers",
        "NOTIFIER_ENV: opt this test out of the autouse RALPHD_NOTIFIER_URL "
        "scrub, so its own os.environ (and anything it calls in-process, "
        "e.g. JobConfig.load()) sees the ambient notifier URL",
    )
    # Task 048 (B.5 follow-on): moves an individual node (never a whole
    # module -- `real_backoff` already marks those three modules, and this
    # is deliberately narrower) out of `fast`/`smoke` into the nightly lane,
    # for a node that is inherently slow rather than merely unoptimised.
    # `pytest_collection_modifyitems` below honours it exactly like
    # `docker`/`browser`.
    config.addinivalue_line(
        "markers",
        "nightly: this individual node belongs in the nightly lane, not "
        "fast/smoke -- applied directly to a node (task 048), never "
        "derived, unlike docker/browser/dev-layout",
    )


# --- the smoke and fast tiers (task 004, requirement G.8) ------------------
# docs/test-suite-audit.md § 5(d) "Tiers" names three lanes by CONTENTS, not
# by module list:
#
#   smoke   in-process engine flows (LoopSupervisor + scripted attempts,
#           backoffs stubbed), CLI in-process, records lint, hub in-process
#   fast    smoke + the stub-pi flows + process-boundary CLI tests
#   nightly the docker tier, the browser tier, the two dev-layout modes
#
# So membership is DERIVED here, exactly as `scripts/final_sha_guards.sh`
# derives its guard subset -- never enumerated. A hand-typed module list is
# the v0.13 failure mode this repo already paid for once (see that script's
# header): the list omits a module, the omission is invisible, and a new
# module added tomorrow silently belongs to no tier. With a derivation, a
# module written after this comment lands in the right tier the first time
# it is collected.
#
# The rules, in order:
#   - an item carrying `docker` or `browser` is nightly: neither `smoke` nor
#     `fast` (the two markers this suite already had, unchanged);
#   - a `tests/test_host_like_dev_layout_script.py` item is nightly too --
#     that is the dev-layout mode the tier table puts in the nightly lane;
#   - everything else is `fast`;
#   - a `fast` item is ALSO `smoke` unless its module spawns a real engine
#     or a real `ralphctl`/console-script process, i.e. unless its own
#     source names one of the process-boundary helpers below (the `live`
#     fixture is named by its CALL, `live(`, because a fixture arrives as a
#     bare parameter name that a docstring could also mention). Those spawns
#     are the whole difference between the two tiers in the audit's table
#     ("smoke + the stub-pi flows + process-boundary CLI tests"), and they
#     are what the 981 s serial fast tier spends ~75 % of its wall clock on
#     (audit § 2). Nested `pytest --collect-only` (the records lint's own
#     shape, tests/test_conformance_ledger.py) is deliberately NOT in this
#     list: the tier table puts records lint in `smoke`.
_PROCESS_BOUNDARY_RE = re.compile(
    r"""
      \bLiveRun\b          # the shared real-engine harness (task 021 folded EngineProc in)
    | \bEngineProc\b       # its pre-task-021 spelling, so an old module still sorts right
    | \bengine_factory\b   # the registry-less LiveRun factory fixture
    | \blive\(            # the registry LiveRun factory fixture, called
    | \bRALPHCTL\b         # a real `ralphctl` subprocess
    | \bRALPHD_ENGINE\b    # a real `ralphd-engine` subprocess
    | host_like_           # the bash `scripts/host_like_*.sh` runs
    """,
    re.VERBOSE,
)

_NIGHTLY_MODULES = ("test_host_like_dev_layout_script.py",)


@functools.lru_cache(maxsize=None)
def _module_is_in_process(path: str) -> bool:
    """True when this test module never spawns a real engine/CLI process, so
    its tests belong to the `smoke` tier as well as to `fast`."""
    try:
        source = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    return _PROCESS_BOUNDARY_RE.search(source) is None


@pytest.hookimpl(tryfirst=True)
def pytest_collection_modifyitems(config, items):
    """Stamp every collected item with its tier (`smoke`/`fast`), before the
    mark plugin's own `-m` deselection runs (hence `tryfirst`).

    An item carrying `nightly` directly (task 048, B.5 follow-on) opts out
    of `fast`/`smoke` the same way `docker`/`browser` do -- for a handful
    of individual nodes, not a whole module, that are inherently slow (a
    real backoff/outage schedule under test, or a real nested-pytest-
    subset black-box run) rather than merely unoptimised: moving just the
    node, not its whole module, keeps every genuinely fast sibling test in
    the same file inside the fast tier.
    """
    for item in items:
        keywords = item.keywords
        if "docker" in keywords or "browser" in keywords or "nightly" in keywords:
            continue
        path = str(getattr(item, "fspath", "") or "")
        if any(path.endswith(name) for name in _NIGHTLY_MODULES):
            continue
        item.add_marker(pytest.mark.fast)
        if _module_is_in_process(path):
            item.add_marker(pytest.mark.smoke)


def free_port() -> int:
    """An ephemeral TCP port, currently free on 127.0.0.1.

    `bind(("127.0.0.1", 0))` asks the kernel for a free port and releases it
    the instant the probe socket above closes -- a TOCTOU race under a
    loaded parallel run (`-n 8`, task 003/G.3): many workers racing for
    ephemeral ports at once can have the OS hand the same just-released
    number to a second bind before the first caller ever gets to use it.
    Re-verify the candidate with a second, immediate bind right before
    returning it, and retry with a fresh candidate on `EADDRINUSE` instead
    of handing back a port that already lost the race -- this does not
    close the window entirely (the port is released again after this
    second bind too), but it shrinks it from "since the probe socket
    closed" to "since this line ran".
    """
    for _ in range(10):
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        try:
            with socket.socket() as s2:
                s2.bind(("127.0.0.1", port))
        except OSError as e:
            if e.errno != errno.EADDRINUSE:
                raise
            continue
        return port
    raise RuntimeError("free_port: no free port after 10 EADDRINUSE retries")


class InProcessHub:
    """The hub's own HTTP server, served on a daemon thread in THIS process
    (task 003, requirement F) instead of a spawned `ralphctl ui` subprocess.

    `ralphd.cli.ui_server.make_server` is the exact object `ralphctl ui`
    builds and serves, so every request still travels over real HTTP to the
    real handler over a real registry directory -- what disappears is only
    the process boundary, which for a test whose subject is a payload the
    hub renders (`approachDisplay`, a 404 for a route that must not exist)
    is not part of the subject at all. A test whose subject IS the boundary
    (the CLI's own startup, a signal, a pty) keeps `test_cli_ui.UiServer`.

    The API is `UiServer`'s -- `base`, `port`, `get`, `post`, `delete`,
    `wait_ready`, `stop` -- so a converted call site reads unchanged.
    """

    def __init__(self, registry: Path):
        from ralphd.cli import ui_server

        self.registry = Path(registry)
        self.registry.mkdir(parents=True, exist_ok=True)
        last: OSError | None = None
        server = None
        for _ in range(5):
            port = free_port()
            try:
                server = ui_server.make_server(self.registry, "127.0.0.1", port)
            except OSError as exc:  # lost the free_port race, try another
                if exc.errno != errno.EADDRINUSE:
                    raise
                last = exc
                continue
            break
        if server is None:
            raise RuntimeError(f"InProcessHub: no bindable port ({last})")
        self._server = server
        self.port = self._server.server_address[1]
        self.base = f"http://127.0.0.1:{self.port}"
        self._thread = threading.Thread(target=self._server.serve_forever,
                                        name="in-process-hub", daemon=True)
        self._thread.start()

    def wait_ready(self, timeout=15):
        """Already listening: the socket is bound before `serve_forever`
        starts, so a connection made now queues rather than being refused.
        Kept for API parity with `UiServer.wait_ready`."""
        return None

    def get(self, path: str) -> tuple[int, dict]:
        try:
            with urllib.request.urlopen(f"{self.base}{path}", timeout=5) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def post(self, path: str, body: dict) -> tuple[int, dict]:
        data = json.dumps(body).encode()
        req = urllib.request.Request(f"{self.base}{path}", method="POST",
                                     data=data)
        req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def delete(self, path: str) -> tuple[int, dict]:
        req = urllib.request.Request(f"{self.base}{path}", method="DELETE")
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def stop(self):
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=10)


@pytest.fixture
def hub_in_process():
    """Factory fixture: `hub_in_process(registry)` -> a live `InProcessHub`,
    shut down at teardown. Drop-in for `tests/test_cli_ui.py`'s `ui` fixture
    for the hub tests whose subject is a payload, not the process boundary.

    `env=` overrides are applied to this process's own `os.environ` for the
    server's lifetime and restored at teardown (a subprocess got them in its
    child env; in-process there is only one environment).
    """
    servers: list[InProcessHub] = []
    saved: dict | None = None

    def make(registry, env: dict | None = None) -> InProcessHub:
        nonlocal saved
        if env:
            if saved is None:
                saved = dict(os.environ)
            os.environ.update(env)
        server = InProcessHub(registry)
        servers.append(server)
        return server

    yield make
    for server in servers:
        server.stop()
    if saved is not None:
        os.environ.clear()
        os.environ.update(saved)


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
#
# task 018: the same fixture also pins PYTHONUSERBASE, to the exact
# `_USER_BASE` value `with_ralphd_importable()` above already derived from
# the real ambient HOME at import time -- one derivation, not a second one
# computed here that could drift from it. Without this, an in-process test
# that overrides HOME (this fixture, autouse) on a `pip install --user -e .`
# layout un-installs the world for anything imported AFTER the override took
# effect, the same #67 shape `with_ralphd_importable()` fixes for a spawned
# child, just in-process instead. Skipped together with the HOME override,
# under the same `AMBIENT_HOME` opt-out, for the same reason: a test that
# asked to see the real ambient HOME gets the real ambient PYTHONUSERBASE
# (i.e. none pinned here) too, not a second, different opt-out spelling.
@pytest.fixture(autouse=True)
def _isolate_ambient_home_in_process(request, monkeypatch, tmp_path_factory):
    if request.node.get_closest_marker("AMBIENT_HOME"):
        return
    isolated = tmp_path_factory.mktemp("ambient-home-in-process")
    monkeypatch.setenv("HOME", str(isolated))
    monkeypatch.setenv("PYTHONUSERBASE", _USER_BASE)


# --- keep the operator's real notifier URL out of the suite (steer 002, ---
# --- task 040) ---------------------------------------------------------
# This container exports `RALPHD_NOTIFIER_URL` (the operator's own
# notifier endpoint) into every process it starts, including whatever
# spawns pytest. `LiveRun`/`ralphctl` below (and `EngineProc` in
# tests/test_e2e.py, which mirrored the same construction until task 021
# folded it into `LiveRun`) used to build a
# spawned child's env as `{**os.environ, ...}`, so the ambient notifier URL
# rode along into every test engine/ralphctl child unless a test happened
# to override it. `JobConfig.load()` reads the same variable straight out
# of `os.environ` (config.py), so any IN-PROCESS call (no subprocess at
# all) saw it too. Task 012's last-approach stagnation pause escalates
# through `self.notifier` whenever `cfg.notifier_url` is set -- so a test
# engine that reached that pause POSTed the escalation text to the
# operator's real Telegram.
#
# `NOTIFIER_ENV_VAR` is the one name both halves of the fix key off of:
# `_without_notifier_env()` drops it from a spawned child's env dict
# unless the caller's own `stub_env` names it explicitly (stub_env is
# always merged in AFTER this, so an explicit stub_env entry still wins);
# the autouse fixture below drops it from the pytest PROCESS's own
# `os.environ`, with the `NOTIFIER_ENV` marker (registered in
# `pytest_configure` above) as the one opt-in spelling.
NOTIFIER_ENV_VAR = "RALPHD_NOTIFIER_URL"


def _without_notifier_env(env: dict) -> dict:
    """`env` (typically `os.environ`) with `RALPHD_NOTIFIER_URL` dropped.

    Callers merge a `stub_env` dict AFTER this, last, in every call site
    that has one -- so a test that deliberately wants a spawned child to
    see the notifier URL still gets it, by naming
    `RALPHD_NOTIFIER_URL` in its own `stub_env` explicitly; this function
    never has to know about that case itself.
    """
    return {k: v for k, v in env.items() if k != NOTIFIER_ENV_VAR}


@pytest.fixture(autouse=True)
def _scrub_notifier_env_in_process(request, monkeypatch):
    if request.node.get_closest_marker("NOTIFIER_ENV"):
        return
    monkeypatch.delenv(NOTIFIER_ENV_VAR, raising=False)


# `_STRIPPED_CHILD_ENV_VARS`: variables a spawned engine must never inherit
# from whatever process is running pytest. The three docker-siblings ones may
# be set in the ambient environment (e.g. this very suite running inside a
# docker-enabled ralphd job), so a test that never asked for them would
# otherwise see the host's values; `NOTIFIER_ENV_VAR` (steer 002, task 040) is
# stripped so a child that reaches a notifier escalation cannot POST to the
# operator's real endpoint. `stub_env` (merged in last, through
# `_resolved_home_env`) stays the one way to opt a specific test back in.
_STRIPPED_CHILD_ENV_VARS = (
    "RALPHD_HOST_WORKSPACE", "RALPHD_HOST_RUN_DIR", "RALPHD_RUN_ID",
    NOTIFIER_ENV_VAR,
)


def _child_base_env() -> dict:
    return {k: v for k, v in os.environ.items()
            if k not in _STRIPPED_CHILD_ENV_VARS}


# Task 010 (B.4): the all-zero infra-retry backoff schedule every stub-`pi`
# launch gets by default. `JobConfig`'s shipped default is
# [2, 5, 15, 30, 60, 120, 300] with a 300 s per-wait cap, so a live run that
# hits ONE infra fault (a killed startup watchdog, a refused connection while
# a port comes up) used to pay a real two-second sleep at best and a real
# five-minute one at worst -- inside a test whose subject is never the retry
# schedule. A test that IS about the schedule keeps its own values: naming
# either knob in the `job` dict, or either `RALPHD_INFRA_RETRY_BACKOFF_S`/
# `RALPHD_INFRA_RETRY_BACKOFF_MAX_S` in `stub_env` (env wins over job.yaml in
# `JobConfig.load`, so an env-named schedule would win regardless) suppresses
# the default for BOTH knobs -- they are one setting, and filling in only the
# unnamed half of it would silently rewrite the other (a `max_s` of 0.0 caps
# any schedule to zero: `backoff = min(backoff, infra_retry_backoff_max_s,
# ...)` in `LoopSupervisor._run_iteration_with_infra_retry`).
_ZERO_BACKOFF_JOB_KEYS = {
    "infra_retry_backoff_s": [0.0],
    "infra_retry_backoff_max_s": 0.0,
}
_ZERO_BACKOFF_ENV_KEYS = {
    "infra_retry_backoff_s": "RALPHD_INFRA_RETRY_BACKOFF_S",
    "infra_retry_backoff_max_s": "RALPHD_INFRA_RETRY_BACKOFF_MAX_S",
}


def _with_zero_backoff(job: dict, stub_env: dict | None) -> dict:
    """`job` with the zero backoff schedule filled in, unless this call named
    either half of it itself (see `_ZERO_BACKOFF_JOB_KEYS`)."""
    out = dict(job)
    env = stub_env or {}
    named = any(key in out or _ZERO_BACKOFF_ENV_KEYS[key] in env
                for key in _ZERO_BACKOFF_JOB_KEYS)
    if named:
        return out
    for key, value in _ZERO_BACKOFF_JOB_KEYS.items():
        out[key] = value
    return out


class LiveRun:
    """A real ralphd-engine with its run/config dirs and API helpers.

    The one shared launcher for a real child engine (task 021, G.1): before
    this wave `tests/test_e2e.py` carried a second, near-identical `EngineProc`
    that ~47 modules imported, so every change to the spawn contract (task
    001's HOME isolation, task 040's notifier scrub) had to be made twice or
    be silently missing from half the suite. Both layouts live here now,
    chosen by `registry=`:

      * `registry=False` (the default, and the old `EngineProc` shape):
        `tmp/run`, `tmp/config`, `tmp/ws`, `tmp/home`, no registry and no
        `host.json` -- for black-box tests that observe the engine strictly
        through its own HTTP API and run-dir files.
      * `registry=True`: `tmp/registry/runs/<run_id>` and
        `tmp/registry/configs/<run_id>` plus a `host.json`, so `ralphctl`
        pointed at `registry` can talk to the run as if a container had been
        started normally (the `live` fixture's shape).
    """

    def __init__(self, tmp: Path, job: dict, stub_env: dict | None = None,
                 *, run_id: str | None = None, registry: bool = False):
        self.run_id = run_id or job.get("run_id")
        self.registry = tmp / "registry"
        if registry:
            self.run_dir = self.registry / "runs" / self.run_id
            self.config_dir = self.registry / "configs" / self.run_id
            self.workspace = tmp / "ws" / self.run_id
            self.home = tmp / "home" / self.run_id
        else:
            self.run_dir = tmp / "run"
            self.config_dir = tmp / "config"
            self.workspace = tmp / "ws"
            self.home = tmp / "home"
        for d in (self.run_dir, self.config_dir, self.workspace):
            d.mkdir(parents=True, exist_ok=True)
        (self.config_dir / "prd.md").write_text("# E2E test PRD\n\nDo the thing.\n")
        (self.config_dir / "job.yaml").write_text(
            "".join(f"{k}: {json.dumps(v)}\n"
                    for k, v in _with_zero_backoff(job, stub_env).items()))
        self.port = free_port()
        # The engine's PATH: STUB_PI stays *first*, because tests depend on the
        # stub `pi` shadowing any real one the operator has installed. After it
        # come the two directories that can hold this repo's own scripts -- the
        # resolved engine's (#65: on a `--user` layout that is ~/.local/bin,
        # not the interpreter's bin) and the interpreter's, which is needed on
        # its own account because tests/stub-pi/pi is `#!/usr/bin/env python3`
        # and must run under the interpreter running pytest -- and only then
        # the ambient PATH. Before #65 the engine's own directory was absent here
        # and `ralphd-engine` was spawned by bare name, so the spawn worked
        # only when the ambient PATH happened to carry ~/.local/bin.
        # with_ralphd_importable() wraps the *merged* dict, after stub_env:
        # stub_env is where the HOME override arrives (#67), so the two
        # variables that have to survive it are applied last rather than
        # buried in the defaults where a caller could shadow them by accident.
        env = with_ralphd_importable({
            **_child_base_env(),
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
        if registry:
            (self.run_dir / "host.json").write_text(json.dumps({
                "runId": self.run_id, "container": "f" * 12, "port": self.port,
                "apiUrl": f"http://127.0.0.1:{self.port}", "image": "n/a",
            }))

    # -- black-box surface -------------------------------------------------
    def api(self, method: str, path: str, body: dict | None = None,
            expect_error: bool = False):
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}", method=method)
        if body is not None:
            req.add_header("Content-Type", "application/json")
            req.data = json.dumps(body).encode()
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = resp.read()
                return resp.status, (json.loads(data) if data else None)
        except urllib.error.HTTPError as e:
            if not expect_error:
                raise
            return e.code, json.loads(e.read() or b"{}")

    def api_raw(self, path: str) -> str:
        """GET a non-JSON (e.g. NDJSON) route and return the decoded body."""
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}")
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.read().decode()

    def wait_state(self, states: tuple[str, ...], timeout=60) -> dict:
        deadline = time.time() + timeout
        status: dict = {}
        while time.time() < deadline:
            status = json.loads((self.run_dir / "status.json").read_text()) \
                if (self.run_dir / "status.json").exists() else {}
            if status.get("state") in states:
                return status
            time.sleep(0.2)
        raise TimeoutError(f"never reached {states}; last: {status}")

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
            {**_without_notifier_env(os.environ), "RALPHD_REGISTRY": str(self.registry)})
        return subprocess.run([str(RALPHCTL), *argv], env=env,
                              capture_output=True, text=True, timeout=60)

    def wait_api(self, timeout=15):
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                self.api("GET", "/healthz")
                return
            except (urllib.error.URLError, ConnectionError, TimeoutError):
                time.sleep(0.2)
        raise TimeoutError("engine API never came up")

    def stop(self):
        if self.proc.poll() is None:
            # Task 048 (B.5 follow-on): a run parked in `state: "paused"`
            # sits forever in `await self._pause.wait()`
            # (loop.py's `_park_until_resumed`) -- an `asyncio.Event`
            # nothing wires a signal handler to. `abort_on_signal` records
            # the abort and interrupts the CHILD process if one happens to
            # be running, but it never sets that Event, so main.py's own
            # `await job_task` never returns and the process does not react
            # to SIGTERM at all while paused. `terminate()` + a 10s wait
            # therefore always times out and falls through to `kill()`
            # anyway -- four suite-budget nodes were each paying that full
            # 10.00s teardown for a grace the engine cannot honour. Detect
            # the state up front and go straight to a bounded kill instead.
            try:
                paused = json.loads(
                    (self.run_dir / "status.json").read_text()
                ).get("state") == "paused"
            except (OSError, json.JSONDecodeError):
                paused = False
            if paused:
                self.proc.kill()
                try:
                    self.proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    pass
                return
            self.proc.terminate()
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.proc.kill()


# --- the shared in-process supervisor harness (G.1, task 021) -------------
# 87 modules at this wave's launch sha each defined their own module-local
# `_supervisor(...)` helper: build a `RunDir` under a tmp dir, build a
# `JobConfig` with the same handful of "don't wait for anything" knobs,
# construct a `LoopSupervisor`, hang a scripted stand-in off `sup.runner`,
# and stub `_wait_out_backoff` so a retry does not really sleep. They
# differed only in a run id, a couple of cfg values, and where the run root
# and workspace sat under `tmp_path` -- so a change to the harness contract
# (task 040's notifier scrub, task 042's bounded pause wait) had to be made
# 87 times or be silently missing from most of the suite.
#
# `make_supervisor()` below is that harness, once. The two names the tests
# use are `supervisor(tmp_path, **cfg)` (the fixture, a factory) and
# `scripted_attempts(sup, [...])` (attach a scripted runner), both with
# backoff AND sleep stubbed by default:
#
#   - backoff: `sup._wait_out_backoff` is replaced by an instant one that
#     reports the delay it was asked for and "not woken", the exact shape
#     every module's local `no_backoff` had.
#   - sleep: `asyncio.sleep` is patched to yield instead of waiting, for as
#     long as the test runs (undone by `_undo_instant_sleep_patches` below,
#     autouse, so a module-level `make_supervisor()` call inside a test body
#     cannot leak the patch into the next test). Ask for the real thing with
#     `real_sleep=True` when the test is about elapsed time itself.
#
#     The restore is an UNCONDITIONAL `asyncio.sleep = _real_asyncio_sleep`,
#     not a `unittest.mock.patch` stack popped by whoever pushed it (task
#     003, G.3): `tests/conftest.py` is loaded TWICE under two distinct
#     module identities in every ordinary invocation -- once as the bare
#     top-level `conftest` plugin pytest's own conftest-discovery imports
#     (no `tests/__init__.py`, so pytest's prepend import mode roots there),
#     and again as `tests.conftest` the first time any test module does
#     `from tests.conftest import ...` (resolved through the namespace
#     package `tests_package_pin.py` pins). Both copies define their own
#     `_patch_sleep_for_this_test`/`_undo_instant_sleep_patches` with their
#     own separate module-global state, but pytest only ever registers the
#     bare-`conftest` copy's fixtures as autouse -- so a `unittest.mock.patch`
#     stack, tracked in a list private to whichever copy's function actually
#     ran, is popped by a teardown that is watching the OTHER copy's
#     (always-empty) list and therefore never restores anything: the first
#     `make_supervisor(real_sleep=False)` call in an entire pytest session
#     patches the one real, process-global `asyncio.sleep` and it stays
#     patched for every later test, forever (caught by
#     `tests/test_router.py`'s and `tests/test_router_engine.py`'s
#     millisecond-timeout hanging-call tests, which fail because a
#     3600-second `asyncio.sleep` returns instantly). Setting `asyncio.sleep`
#     straight back to the module-level `_real_asyncio_sleep` captured at
#     import time sidesteps the whole double-module problem: it does not
#     matter which copy's function object did the patching, because there is
#     only ever one real `asyncio` module and one real `.sleep` attribute on
#     it, and every copy's `_real_asyncio_sleep` was captured, correctly,
#     before any test ever ran.
#
# Deliberately only the two BACKOFF knobs, not a second set of iteration /
# approach / vigilance defaults: a module that migrated its local harness over
# to `make_supervisor` must keep seeing `JobConfig`'s own defaults for
# everything its harness did not name itself, or the migration would quietly
# re-tune every run it drives. `infra_outage_budget_s` is left alone for the
# same reason -- the modules that care about it pass their own.
DEFAULT_SUPERVISOR_CFG = {
    "infra_retry_backoff_s": [0.0],
    "infra_retry_backoff_max_s": 0.0,
}

_real_asyncio_sleep = asyncio.sleep


async def _instant_sleep(delay, result=None):
    """`asyncio.sleep` with the waiting taken out: still a real await point
    (so a task that is meant to yield still yields, and cancellation still
    lands), just never a real delay."""
    return await _real_asyncio_sleep(0, result)


def _patch_sleep_for_this_test() -> None:
    asyncio.sleep = _instant_sleep    # type: ignore[assignment]


@pytest.fixture(autouse=True)
def _undo_instant_sleep_patches():
    yield
    asyncio.sleep = _real_asyncio_sleep    # type: ignore[assignment]



# --- B.4 cure (task 047, operator steer 004): a class-level backoff stub
# for every collected test -------------------------------------------------
# Task 010 zeroed the backoff SCHEDULE across the suite, and its own grep
# guard accepted that all-zero schedule as equivalent to a stub -- but a
# zero schedule makes the REAL `_wait_out_backoff` return instantly, which
# is not the same thing as the PRD's B.4 "unstubbed" clause, and it does
# nothing for the ~43 modules that construct a bare `LoopSupervisor(...)`
# without going through `make_supervisor()` or assigning their own instance
# stub. Patching the CLASS attribute here, once, autouse, for every test
# collected in the whole session, closes that gap structurally instead of
# hunting down every construction site: an instance that never assigns its
# own `_wait_out_backoff` now resolves this class-level stub through
# ordinary attribute lookup, and one that DOES assign its own
# (`make_supervisor()`'s `_no_backoff`, or a test's own `fake_backoff`/
# `wrapped`) shadows it exactly as before -- nothing already stubbed changes
# behaviour.
#
# Opt-out: `@pytest.mark.real_backoff` (module-level `pytestmark =
# pytest.mark.real_backoff` works too), registered in pyproject.toml and
# applied ONLY by the three modules the PRD names as testing the
# backoff/retry schedule itself -- `test_infra_retry_schedule.py`,
# `test_retry_now.py`, `test_reflect_infra_retry.py`.
# `tests/test_stub_pi_harness.py` pins both this fixture's
# presence and that no other module carries the marker.
REAL_BACKOFF_MARKER = "real_backoff"


async def _class_level_no_backoff(self, seconds):
    return seconds, False


@pytest.fixture(autouse=True)
def _stub_wait_out_backoff_at_class_level(request):
    from ralphd.engine.loop import LoopSupervisor
    if request.node.get_closest_marker(REAL_BACKOFF_MARKER) is not None:
        yield
        return
    original = LoopSupervisor._wait_out_backoff
    LoopSupervisor._wait_out_backoff = _class_level_no_backoff   # type: ignore[assignment]
    try:
        yield
    finally:
        LoopSupervisor._wait_out_backoff = original             # type: ignore[assignment]


class ScriptedRunner:
    """The scripted stand-in for `sup.runner`: a script per phase, the last
    step of each repeating forever, dispatched on the phase the engine itself
    recorded in `status.json` (never on a count the test keeps) -- so a
    contract re-ask, which stays on the same phase, keeps hitting the same
    step.

    A step is called `step(sup, prompt)` and returns an `IterationResult`.
    `calls` counts per phase; `phases_seen` and `prompts` record order flatly,
    for tests that pin the order of phases directly rather than depending on
    which internal call site happens to emit a `phase` event.
    """

    def __init__(self, handlers: dict | None = None):
        self.handlers: dict = dict(handlers or {})
        self.calls: dict[str, int] = {}
        self.phases_seen: list[str] = []
        self.prompts: list[str] = []
        self.running = False
        self.sup = None

    def interrupt(self) -> bool:
        return self.running

    async def run(self, prompt, transcript, **kw):
        phase = self.sup.run.read_status().get("phase")
        self.phases_seen.append(phase)
        self.prompts.append(prompt)
        n = self.calls.get(phase, 0)
        self.calls[phase] = n + 1
        steps = self.handlers[phase]
        self.running = True
        try:
            return steps[min(n, len(steps) - 1)](self.sup, prompt)
        finally:
            self.running = False


def _under(root: Path, where) -> Path:
    """`where` resolved against `root`: None -> root itself, a relative
    string/Path -> under it (`"ws"`, `"../ws"`), an absolute Path -> itself."""
    if where is None:
        return root
    p = Path(where)
    return p if p.is_absolute() else (root / p).resolve()


async def _no_backoff(seconds):
    return seconds, False


PRD_TEXT = "# test PRD\n\nDo the thing.\n"


def make_supervisor(root: Path, runner=None, *, run_id: str = "unit",
                    run_root=None, workspace=None, status: dict | None = None,
                    tasks: list[dict] | None = None, prd=None,
                    approach: int | None = None,
                    iterations_used: int | None = None,
                    runner_factory=None, no_backoff: bool = True,
                    cfg_defaults: bool = True,
                    real_sleep: bool = False, **cfg):
    """A `LoopSupervisor` on a real `RunDir` under `root`, wired for an
    in-process test: no real backoff, no real sleeping, and a scripted
    `runner` instead of `PiRunner` whenever one is given.

    `run_root`/`workspace` are resolved by `_under()` against `root` (both
    default to `root`); `status` is written through `RunDir.update_status`
    before the supervisor exists (the resumed-run shape); `tasks` seeds
    `tasks.json` directly (the shape a real plan-proposal contract can never
    produce, because it carries each task's own `status`). Everything else in
    `**cfg` goes to `JobConfig` over `DEFAULT_SUPERVISOR_CFG`.

    The four remaining knobs are the module-local pre/post steps that were
    the only reason a module could not use this harness as-is:

    - `prd=True` (or a string) writes `run.prd_file`, which every path that
      composes a prompt out of the PRD reads unconditionally;
    - `approach=`/`iterations_used=` set `sup._approach` / `sup.iterations_used`
      after construction, the two counters a test that starts mid-run seeds by
      hand;
    - `runner_factory=` is called with the finished supervisor
      (`runner_factory(sup)`) and its result attached, for the scripted
      stand-ins whose constructor takes the supervisor or its `RunDir`.

    `cfg_defaults=False` drops `DEFAULT_SUPERVISOR_CFG` from the `JobConfig`:
    for a module whose local harness never named a backoff knob, the retry
    schedule under test IS `JobConfig`'s own default, and seeding the instant
    one would silently rewrite what the test measures (`_wait_out_backoff` is
    still stubbed, so it reports those delays without waiting them).
    """
    # Imported here, not at module import: this conftest is loaded for every
    # session (including the black-box CLI modules that only shell out), and
    # `ralphd.engine.loop` pulls in the whole engine.
    from ralphd.engine.config import JobConfig
    from ralphd.engine.loop import LoopSupervisor
    from ralphd.engine.state import RunDir, atomic_write_json

    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    run = RunDir(root=_under(root, run_root))
    ws = _under(root, workspace)
    ws.mkdir(parents=True, exist_ok=True)
    if prd is not None and prd is not False:
        run.prd_file.write_text(PRD_TEXT if prd is True else prd)
    if status is not None:
        run.update_status(**status)
    sup = LoopSupervisor(JobConfig(run_id=run_id,
                                   **({**DEFAULT_SUPERVISOR_CFG, **cfg} if cfg_defaults
                                      else cfg)),
                         run, ws)
    if runner is not None:
        attach_runner(sup, runner)
    if runner_factory is not None:
        # A factory may return None, for the modules whose stand-in is optional
        # (`_supervisor(root)` with no handlers keeps the real `PiRunner`).
        built = runner_factory(sup)
        if built is not None:
            attach_runner(sup, built)
    if approach is not None:
        sup._approach = approach
    if iterations_used is not None:
        sup.iterations_used = iterations_used
    if no_backoff:
        sup._wait_out_backoff = _no_backoff    # type: ignore[method-assign]
    if not real_sleep:
        _patch_sleep_for_this_test()
    if tasks is not None:
        atomic_write_json(run.tasks_file,
                          {"version": 1, "tasks": [dict(t) for t in tasks]})
    return sup


def attach_runner(sup, runner):
    """Hang `runner` off `sup.runner`, with the back-reference every scripted
    stand-in needs to read the phase the engine recorded."""
    if getattr(runner, "sup", None) is None:
        runner.sup = sup                       # type: ignore[attr-defined]
    sup.runner = runner                        # type: ignore[assignment]
    return runner


def scripted_attempts(sup, script) -> ScriptedRunner:
    """Attach a `ScriptedRunner` to `sup` and return it.

    `script` is either a `{phase: [step, ...]}` mapping or a plain list of
    steps, which is taken as the script for the phase the run is in when the
    first attempt is made (`"worker"` unless the test says otherwise via a
    mapping). A step is `step(sup, prompt) -> IterationResult`.
    """
    handlers = script if isinstance(script, dict) else {"worker": list(script)}
    return attach_runner(sup, ScriptedRunner(handlers))


def stub_contract_reask(sup, reply: str = "") -> list[list[str]]:
    """Answer the engine's UNCOUNTED contract-repair channel
    (`LoopSupervisor._ask_contract_fix`) in-process, and record the errors it
    was asked about. Returns the list of `errors` lists, one per re-ask.

    Why this exists (task 050, requirement G.2): `_ask_contract_fix` is the
    one hat-facing call in the engine that does NOT go through
    `run_iteration`/`_run_iteration_once`, so a test that stubs *those* (the
    usual `_stub_attempts` technique) still reaches `self.runner.run(...)` --
    and a `make_supervisor` supervisor built with no scripted runner keeps the
    real `PiRunner`, which spawns the real `pi` binary and makes a REAL
    provider call, once per re-ask, bounded only by `router_timeout_s`
    (`JobConfig`'s default 60 s) and repeated `contract_retry_max` (2) times.
    In this container that is exactly 120 s of pure waiting per test whenever
    the ambient `pi` config can reach a provider at all -- two such nodes
    (`tests/test_refunds_survive_resume.py::test_a_grace_review_refund_is_
    recorded_in_status` and `tests/test_worker_hat.py::test_3_mutation_stops_
    discriminating_generic_hat_reads_interrupt_as_contract_violation`) each
    sat at 120.05 s / 114.00 s in the `-m fast -n 8 --dist loadfile` lane.

    `reply=""` is the FAITHFUL default, not a convenience: an empty answer is
    what a failed/timed-out `pi` re-ask actually hands back, so
    `enforce_contract` still runs its whole ladder and still exhausts it, and
    every assertion around the call is judged on the same outcome as before --
    only the real subprocess and its real provider call are gone. Pass a
    payload string when a test wants the repair to SUCCEED.
    """
    asked: list[list[str]] = []

    async def fake_ask_contract_fix(hat, errors, final_text=None):
        asked.append(list(errors))
        return reply, {}

    sup._ask_contract_fix = fake_ask_contract_fix    # type: ignore[method-assign]
    return asked


def supervisor_factory(runner_factory=None, **defaults):
    """A module's own `_supervisor`-shaped callable, built out of the shared
    harness: `(root, *runner_args, **cfg) -> LoopSupervisor`, with this
    module's run id and cfg defaults bound and a call's own `**cfg` winning.

    `runner_factory` (e.g. `ScriptedRunner`) is called with whatever
    positional arguments follow `root`, so a module whose call sites read
    `_supervisor(root, handlers)` keeps reading exactly that.
    """
    def build(root, *runner_args, **cfg):
        runner = runner_factory(*runner_args) if runner_factory is not None else None
        return make_supervisor(root, runner, **{**defaults, **cfg})
    return build


def scripted_supervisor(**defaults):
    """`supervisor_factory(ScriptedRunner, **defaults)`: the shape used by the
    modules that drive the whole loop through a per-phase script."""
    return supervisor_factory(ScriptedRunner, **defaults)


def supervisor_with_runner(build_runner, **defaults):
    """`supervisor_factory` for a module whose scripted stand-in needs the
    supervisor (or its `RunDir`) to exist first: `build_runner(sup,
    *runner_args)` is called with the finished supervisor and whatever
    positional arguments the call site passed after `root`, and its result is
    attached as `sup.runner`.

    A module keeps its own stand-in class and stops rebuilding the harness
    around it:

        _supervisor = supervisor_with_runner(
            lambda sup, handlers: _ScriptedAgent(sup, handlers),
            run_id="...", iterations=15)
    """
    def build(root, *runner_args, **cfg):
        return make_supervisor(
            root, runner_factory=lambda sup: build_runner(sup, *runner_args),
            **{**defaults, **cfg})
    return build


@pytest.fixture
def supervisor():
    """Factory fixture: `supervisor(tmp_path, **cfg)` -> LoopSupervisor, with
    backoff and sleep stubbed by default (see `make_supervisor`)."""
    return make_supervisor


@pytest.fixture
def live(tmp_path):
    """Factory fixture: `live(run_id=..., job=..., stub_env=...)` -> LiveRun.
    All runs created are stopped at teardown."""
    runs = []

    def make(run_id="logtest", job=None, stub_env=None):
        defaults = {"run_id": run_id, "iterations": 12, "max_approaches": 3,
                    "on_complete": "idle"}
        r = LiveRun(tmp_path, {**defaults, **(job or {})}, stub_env,
                    run_id=run_id, registry=True)
        runs.append(r)
        return r

    yield make
    for r in runs:
        r.stop()


@pytest.fixture
def engine_factory(tmp_path):
    """Factory fixture: `engine_factory(job=..., stub_env=...)` -> `LiveRun`
    on the registry-less layout (what `tests/test_e2e.py` owned before task
    021 folded `EngineProc` into `LiveRun`). All runs are stopped at
    teardown."""
    procs: list[LiveRun] = []

    def make(job: dict | None = None, stub_env: dict | None = None) -> LiveRun:
        defaults = {"run_id": "e2e", "iterations": 12,
                    "max_approaches": 3, "on_complete": "idle"}
        e = LiveRun(tmp_path, {**defaults, **(job or {})}, stub_env)
        procs.append(e)
        return e

    yield make
    for e in procs:
        e.stop()
