"""#67: the engine subprocess must still be able to `import ralphd` when the
test overrides HOME -- and must still not see the operator's real one.

The defect. 19 modules launch the engine with `stub_env={"HOME": <tmp>}`,
deliberately: a virgin $HOME *is* the subject of the creds / skills / prompts
/ llm-config tests (where `~/.pi/agent/models.json` lands, where credentials
are placed, that nothing pre-existing is read). On a `pip install --user -e .`
layout that override also un-installs the package for the child, because
`ralphd` is reachable only through `$HOME/.local/lib/pythonX.Y/site-packages`
and `site` derives that path from HOME at interpreter start. The child died
with `ModuleNotFoundError: No module named 'ralphd'` before binding a port.

What made it expensive is that the message was invisible: the engine's
stdout is a PIPE nothing reads until the test is already failing, so the
symptom was `TimeoutError: engine API never came up` 15 seconds later, in a
fixture, with no mention of imports. Measured on tests/test_cli_creds_crud.py
before the fix: `1 passed, 7 errors in 105s`, identical under an interpreter
with the console scripts beside it -- so this is a distinct defect from #65's
script-location bug, and no virtualenv hides it.

The fix lives once, in `tests/conftest.py:with_ralphd_importable()`, and every
place that spawns this repo's code routes its env through it: the two engine
launchers (`LiveRun` there -- the one launcher since task 021 folded
test_e2e.py's `EngineProc` into it -- and test_config_overlay.py's fixture,
which bypasses `LiveRun.__init__` and so
has to repeat it) and the two shared `ralphctl` runners (`LiveRun.ralphctl`,
and `Ctl.run` in tests/test_cli_docker.py -- `ralphctl` is the same package
behind a second console script, so a HOME handed to it hides `ralphd` just the
same).

It sets two variables, and the tests below pin *both*, because setting only
the obvious one leaves the defect standing: HOME hides not just `ralphd` but
everything installed alongside it, so `PYTHONPATH=<repo>/src` on its own
merely moves the failure from `import ralphd` to `import uvicorn` in
engine/main.py.

Every test here also asserts the tmp HOME survived, not only that the import
worked, because a fix that restored the import by leaking the real HOME back
in would trade this defect for a worse one: all 19 modules would start reading
the operator's own `~/.pi`, passing or failing according to what is in it.
Importability and isolation are checked as one pair, never separately.
"""

from __future__ import annotations

import json
import os
import site
import subprocess
import sys
from pathlib import Path

from tests.conftest import REPO, REPO_SRC, with_ralphd_importable

# The stub `pi` dumps its whole environment here on every invocation (see
# tests/stub-pi/pi and tests/test_stub_env_marker_atomic.py, which pins that
# it is written atomically). It is the only black-box view of the environment
# the engine actually ran with -- the engine passes its own env down to `pi`,
# so HOME/PYTHONPATH observed here are the ones the engine child received.
ENV_MARKER = ".stub-env.json"


def _on_user_site() -> bool:
    """True when this interpreter reaches ralphd's dependencies through *user*
    site-packages -- the `pip install --user -e .` layout #67 is about, and the
    only one where dropping the fix is observable.

    Guards the mutation halves below. Inside a venv an unfixed spawn succeeds
    regardless, because the venv's site-packages does not move with HOME, so
    asserting a failure there would pin nothing and would fail the suite for
    the wrong reason. Decided from where uvicorn actually is rather than from
    `sys.prefix != sys.base_prefix`, because it is uvicorn's location -- not
    the flavour of the interpreter -- that the mutation depends on.
    """
    import uvicorn
    return Path(uvicorn.__file__).is_relative_to(site.getusersitepackages())


def _wait_for_env_marker(path: Path, timeout: float = 20) -> dict:
    """Poll until the marker holds a parsable document; {} if the deadline
    passes, so the caller's own assertion reports the failure."""
    import time
    deadline = time.time() + timeout
    while True:
        try:
            return json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            if time.time() >= deadline:
                return {}
            time.sleep(0.2)


# --------------------------------------------------------------------------
# The mechanism, in isolation and without a layout assumption.
# --------------------------------------------------------------------------
_PROBE = ("import json, os, ralphd, sys;"
          " print(json.dumps({'file': ralphd.__file__, 'home': os.environ['HOME'],"
          " 'version': ralphd.__version__}))")


def _probe(env: dict[str, str]) -> subprocess.CompletedProcess:
    """Run the probe under `-S`, i.e. with no site-packages of any kind.

    `-S` is what makes the pair below decide the same way on every layout.
    Without it the negative case is untestable here: in a virtualenv the venv's
    own site-packages is on sys.path regardless of HOME, so a child that
    ignores PYTHONPATH entirely would still import `ralphd` and the mutation
    would pass vacuously. Suppressing site reproduces, on any interpreter, the
    exact condition a HOME override creates on a `--user` install: the package
    is reachable through PYTHONPATH or not at all.
    """
    return subprocess.run([sys.executable, "-S", "-c", _PROBE], env=env,
                          capture_output=True, text=True, timeout=60)


def test_pythonpath_makes_the_child_import_this_checkout_under_a_new_home(tmp_path):
    """The PYTHONPATH half, isolated: which `ralphd` the child loads, and that
    it loads one at all with site suppressed."""
    fake_home = tmp_path / "fakehome"
    fake_home.mkdir()
    base = {"PATH": os.environ["PATH"], "HOME": str(fake_home)}

    res = _probe(with_ralphd_importable(base))
    assert res.returncode == 0, res.stdout + res.stderr
    seen = json.loads(res.stdout)
    # imported, and imported from *this checkout* rather than from whatever
    # build happens to be installed -- the reason PYTHONPATH is set at all
    # once PYTHONUSERBASE has restored importability (see the helper's
    # comment in tests/conftest.py).
    assert Path(seen["file"]) == REPO_SRC / "ralphd" / "__init__.py"
    # ...and the fresh-$HOME property the 19 modules exist for is intact.
    assert seen["home"] == str(fake_home)

    # Mutation pair: the same spawn with the helper omitted cannot import it,
    # so the assertions above are about the helper and not about `ralphd`
    # being importable no matter what.
    res = _probe(base)
    assert res.returncode != 0
    assert "No module named 'ralphd'" in res.stderr


def test_pythonuserbase_is_what_restores_the_engines_dependencies(tmp_path):
    """The half that is easy to miss, and without which #67 is not fixed.

    HOME does not merely hide `ralphd`; it hides user site-packages entirely,
    and uvicorn / fastapi / pyyaml live there too. So a fix that only put
    `<repo>/src` on the path would move the failure from `import ralphd` in
    the console-script wrapper to `import uvicorn` at engine/main.py:13 --
    same silent 15-second fixture timeout, one line further in.

    Asserted through `site` rather than by importing uvicorn, so this states
    the mechanism (which directory the child's user site resolves to) and not
    an incidental fact about which packages happen to be installed where.
    """
    fake_home = tmp_path / "fakehome"
    fake_home.mkdir()
    base = {"PATH": os.environ["PATH"], "HOME": str(fake_home)}
    probe = ("import json, os, site;"
             " print(json.dumps({'usersite': site.getusersitepackages(),"
             " 'home': os.environ['HOME']}))")

    def run(env):
        res = subprocess.run([sys.executable, "-c", probe], env=env,
                             capture_output=True, text=True, timeout=60)
        assert res.returncode == 0, res.stdout + res.stderr
        return json.loads(res.stdout)

    fixed = run(with_ralphd_importable(base))
    assert fixed["usersite"] == site.getusersitepackages(), \
        "the child's user site must be the one this interpreter really uses"
    assert not fixed["usersite"].startswith(str(fake_home)), \
        "user site still follows the overridden HOME -- deps stay invisible"
    assert fixed["home"] == str(fake_home)  # and HOME is still the tmp one

    # Mutation pair: without the helper the child's user site follows the
    # override into an empty tmp dir, which is the defect verbatim.
    broken = run(base)
    assert broken["usersite"].startswith(str(fake_home))
    assert broken["usersite"] != fixed["usersite"]


def test_the_helper_prepends_and_never_clobbers_an_inherited_pythonpath():
    """scripts/host_like_pytest.sh runs the suite with a decoy `tests` package
    on $PYTHONPATH; assigning rather than prepending would silently disarm
    that scenario. Prepending also matters on its own account: this checkout
    must win over any other `ralphd` a caller put on the path."""
    decoy = "/tmp/decoy-pythonpath-entry"
    out = with_ralphd_importable({"PYTHONPATH": decoy})["PYTHONPATH"]
    assert out.split(os.pathsep) == [str(REPO_SRC), decoy]

    # ...and with nothing inherited it does not leave an empty entry behind
    # (an empty PYTHONPATH component means the cwd, which would put the repo
    # root on the child's sys.path and make `tests` importable there -- the
    # very ambiguity the root conftest.py exists to remove).
    assert with_ralphd_importable({})["PYTHONPATH"] == str(REPO_SRC)


def test_no_pythonpath_is_added_without_a_checkout_beside_the_tests(monkeypatch):
    """On a layout with no `src/ralphd` next to tests/ (a wheel install) there
    is no checkout to prefer, and naming a directory that does not exist would
    only mislead whoever debugs the next import failure. PYTHONUSERBASE is
    still set, because that half is about the layout being unknown."""
    import tests.conftest as conftest
    monkeypatch.setattr(conftest, "REPO_SRC", Path("/nonexistent/src"))
    out = conftest.with_ralphd_importable({"HOME": "/tmp"})
    assert "PYTHONPATH" not in out
    assert out["PYTHONUSERBASE"] == site.getuserbase()


# --------------------------------------------------------------------------
# The real thing: the engine launchers both tests use, with HOME overridden.
# --------------------------------------------------------------------------
def test_live_run_engine_starts_and_keeps_the_tmp_home(live, tmp_path):
    """`LiveRun` (tests/conftest.py) is the launcher behind the `live` fixture
    that test_cli_creds_crud.py and friends use. wait_api() returning at all
    is the proof the child imported `ralphd`: it only answers /healthz after
    the module is loaded and uvicorn is bound."""
    fake_home = tmp_path / "engine-home"
    fake_home.mkdir()
    run = live(run_id="import-path", job={"iterations": 6, "max_approaches": 1},
               stub_env={"HOME": str(fake_home)})
    run.wait_api()

    seen = _wait_for_env_marker(run.run_dir / ENV_MARKER)
    assert seen, "the stub never ran an iteration, so the engine never got going"
    assert seen["HOME"] == str(fake_home)
    assert seen["HOME"] != os.environ.get("HOME"), \
        "the fresh-$HOME property #67's fix must not trade away"
    assert seen["PYTHONPATH"].split(os.pathsep)[0] == str(REPO_SRC)
    assert seen["PYTHONUSERBASE"] == site.getuserbase()


def test_engine_proc_starts_and_keeps_the_tmp_home(tmp_path):
    """The same assertions for a direct `LiveRun` construction on the
    registry-less layout (what `EngineProc` was before task 021) -- used by
    test_llm_api.py, test_creds_api.py, test_skills_api.py
    and the rest of the fresh-$HOME tier. Two entry points into the launcher
    means two places to check, so it is asserted twice rather than assumed to
    generalise.
    (test_config_overlay.py is a third, and covers itself: its own tests
    override HOME and could not start the engine at all without this.)
    """
    from tests.conftest import LiveRun

    fake_home = tmp_path / "engine-home"
    fake_home.mkdir()
    e = LiveRun(tmp_path, {"run_id": "import-path-e2e", "iterations": 6,
                          "max_approaches": 1, "on_complete": "idle"},
                stub_env={"HOME": str(fake_home)})
    try:
        e.wait_api()
        seen = _wait_for_env_marker(e.run_dir / ENV_MARKER)
        assert seen, "the stub never ran an iteration"
        assert seen["HOME"] == str(fake_home)
        assert seen["HOME"] != os.environ.get("HOME")
        assert seen["PYTHONPATH"].split(os.pathsep)[0] == str(REPO_SRC)
        assert seen["PYTHONUSERBASE"] == site.getuserbase()
    finally:
        e.stop()


def test_the_engine_script_itself_runs_under_an_overridden_home():
    """One rung more black-box than the probe above: the real `ralphd-engine`
    console script, not a `-c` string -- so the shebang, the entry-point
    wrapper and the package import are all in the picture. `--version` exits
    before any RALPHD_* env is read (PRD req 29a), which makes it the cheapest
    argv that still proves the import happened."""
    import ralphd

    from tests.conftest import RALPHD_ENGINE

    # task 018: the autouse in-process HOME isolation fixture
    # (tests/conftest.py::_isolate_ambient_home_in_process) now pins
    # PYTHONUSERBASE on os.environ for every test, including this one -- so
    # `{**os.environ, "HOME": ...}` alone would already carry a working
    # PYTHONUSERBASE and the mutation half below ("the same spawn without
    # the helper must fail") would no longer fail: it would pass by
    # accident, pinning nothing. PYTHONUSERBASE is stripped back out here so
    # `fresh_home` is what an unfixed caller's env dict actually looked like
    # before #67's fix existed, and the mutation pair stays armed.
    fresh_home = {**os.environ, "HOME": "/nonexistent-home-for-67"}
    fresh_home.pop("PYTHONUSERBASE", None)

    def version(env):
        return subprocess.run([str(RALPHD_ENGINE), "--version"], env=env,
                              capture_output=True, text=True, timeout=30)

    res = version(with_ralphd_importable(fresh_home))
    assert res.returncode == 0, res.stdout + res.stderr
    assert "ralphd-engine" in res.stdout
    # ...and it is *this tree's* version, not an installed build's -- the
    # PYTHONPATH half's job. `ralphd` imported here resolves to REPO/src (the
    # root conftest.py and test_ralphctl_resolution.py both hold that).
    assert Path(ralphd.__file__).is_relative_to(REPO)
    assert ralphd.__version__ in res.stdout

    # Mutation pair, asserted only where it can mean anything (see
    # _on_user_site): the same spawn without the helper must fail, and fail on
    # an import.
    if _on_user_site():
        broken = version(fresh_home)
        assert broken.returncode != 0
        assert "ModuleNotFoundError" in broken.stderr, broken.stderr


def test_the_shared_ralphctl_runner_keeps_the_tmp_home(tmp_path):
    """`ralphctl` is the same package behind a second console script, and
    tests/test_cli_docker.py's `Ctl.run` -- the runner ~10 modules get from the
    `ctl` fixture -- lets callers pass a HOME through. One of them,
    test_cli_resume_llm_wiring.py, asserts on the ~/.aws mount ralphctl derives
    from that HOME, so it genuinely needs the override; before the helper was
    applied there it failed with `ModuleNotFoundError: No module named
    'ralphd'` from ~/.local/bin/ralphctl line 3. Same defect, other script."""
    from test_cli_docker import Ctl

    import ralphd

    fake_home = tmp_path / "ctl-home"
    fake_home.mkdir()
    ctl = Ctl(tmp_path)
    res = ctl.run("--version", env={"HOME": str(fake_home)})
    assert res.returncode == 0, res.stdout + res.stderr
    assert ralphd.__version__ in res.stdout
