"""Task 008 (v0.12 requirement D, #84): `scripts/host_like_dev_layout.sh` is
the third host-shape layout -- a user-base editable install plus a
populated, hostile `$HOME` -- sibling to `scripts/host_like_pytest.sh` (see
that script's header for why sibling, not mode). This test proves the
mechanism the same way `tests/test_host_like_pytest_script.py` proves
`host_like_pytest.sh`'s: by writing a throwaway probe module the script
actually execs and asserting the environment properties from *inside* that
subprocess, plus asserting the script's own post-run skill-survival report.
"""

from __future__ import annotations

import os
import subprocess
import sys
import venv
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts" / "host_like_dev_layout.sh"

# One cache dir reused across every invocation in this module -- each
# invocation's own $HOME is a fresh scratch dir (so pip's own default
# `$HOME/.cache/pip` is never reused), and this module makes several.
_PIP_CACHE_ENV = {
    "HOST_LIKE_DEV_LAYOUT_PIP_CACHE_DIR": "/tmp/host-like-dev-layout-pip-cache-test",
}


def _run(args, extra_env=None, timeout=180):
    env = dict(os.environ)
    env.update(_PIP_CACHE_ENV)
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        ["bash", str(SCRIPT), *args],
        cwd=str(REPO),
        capture_output=True,
        text=True,
        env=env,
        timeout=timeout,
    )


def test_script_exists_and_is_executable():
    assert SCRIPT.is_file(), SCRIPT
    assert os.access(SCRIPT, os.X_OK), f"{SCRIPT} is not executable"


def test_script_refuses_a_venv_interpreter(tmp_path):
    """A virtualenv interpreter has user-site packages disabled outright
    (verified live: `.venv/bin/python -c "import site; print(site.
    ENABLE_USER_SITE)"` prints `False` in this repo's own checkout), which
    would make PYTHONUSERBASE inert and the whole layout's claim -- the
    import path moves with $HOME -- vacuous. The script must detect that
    and refuse rather than silently proving nothing."""
    venv_dir = tmp_path / "throwaway-venv"
    venv.EnvBuilder(with_pip=False).create(venv_dir)
    venv_python = venv_dir / "bin" / "python"
    assert venv_python.is_file()

    result = _run(["--collect-only"], extra_env={"PYTHON": str(venv_python)}, timeout=30)
    assert result.returncode == 2, result.stdout + result.stderr
    combined = result.stdout + result.stderr
    assert "venv" in combined or "user-site" in combined


def test_script_populates_a_hostile_home_and_wires_pythonuserbase_before_install():
    """Static check: the models.json/skill population happens BEFORE the
    `pip install --user` call, and PYTHONUSERBASE is computed from the new
    $HOME (not left to each platform's own default) and threaded through
    both the install and the run."""
    full_text = SCRIPT.read_text()
    # Skip past the header comment block (which discusses both pieces in
    # prose, in whichever order reads best) to the executable body, so this
    # check is about what the script DOES, not how its own header talks
    # about it.
    text = full_text[full_text.index("set -euo pipefail"):]
    populate_idx = text.index('.pi/agent/models.json')
    install_idx = text.index("pip install --user")
    assert populate_idx < install_idx, (
        "the hostile $HOME must be populated before the install, not after"
    )
    assert "site.getuserbase()" in text
    assert text.count("PYTHONUSERBASE=") >= 2, (
        "PYTHONUSERBASE must be threaded through more than one invocation"
        " (install AND run), not set once and dropped"
    )
    assert ".pi/agent/skills/" in text


def test_provider_name_the_script_declares_is_used_by_no_test_fixture():
    """The models.json this script writes must declare a provider no
    fixture in this repo actually uses, per the task's own success
    criteria -- otherwise a coincidental match could make a route check
    pass for the wrong reason."""
    text = SCRIPT.read_text()
    start = text.index('"providers": {') 
    end = text.index("}", text.index('"providers": {', start) + 1)
    snippet = text[start:end]
    # extract the provider key between the outer quotes on the next line
    provider_name = snippet.split('"providers": {', 1)[1].strip().splitlines()[0]
    provider_name = provider_name.strip().split('"')[1]
    assert provider_name, "could not extract the declared provider name"

    hits = []
    for path in (REPO / "tests").rglob("*.py"):
        if path == Path(__file__):
            continue
        if provider_name in path.read_text(errors="ignore"):
            hits.append(str(path))
    assert hits == [], (
        f"provider name {provider_name!r} is used by a fixture: {hits} --"
        " it must be one no fixture uses"
    )


def test_script_installs_ralphd_so_it_imports_from_this_checkout_via_userbase(tmp_path):
    """The whole point of (a): with a fresh, populated $HOME and no system
    install to fall back on for THIS check, `ralphd` must import from this
    checkout's own src/ tree (the editable install target), proving the
    import path really did move with $HOME through PYTHONUSERBASE."""
    probe_dir = tmp_path / "probe"
    probe_dir.mkdir()
    probe = probe_dir / "test_import_path_probe.py"
    probe.write_text(
        "import ralphd\n"
        "\n"
        "\n"
        "def test_ralphd_imports_from_this_checkout():\n"
        f"    assert ralphd.__file__.startswith({str(REPO)!r}), ralphd.__file__\n"
    )
    result = _run([str(probe)])
    assert result.returncode == 0, result.stdout + result.stderr
    assert "1 passed" in result.stdout, result.stdout


def test_script_reports_the_placed_skills_survival_after_the_run():
    """(c): after the pytest run, the script re-checks
    ~/.pi/agent/skills/<name>/SKILL.md and reports the outcome on stdout --
    a fast, no-op test target must still get the SURVIVED report, since
    nothing in this invocation ever touches that $HOME's skills dir."""
    result = _run(
        ["tests/test_host_like_pytest_script.py::test_script_exists_and_is_executable"]
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "SURVIVED the run" in result.stdout, result.stdout
    assert "DID NOT SURVIVE" not in result.stdout


def test_script_exits_with_pytests_own_status_on_a_failing_subset(tmp_path):
    """Exit code is pytest's own, not swallowed by the post-run skill
    report."""
    probe_dir = tmp_path / "probe"
    probe_dir.mkdir()
    probe = probe_dir / "test_deliberate_failure_probe.py"
    probe.write_text("def test_fails():\n    assert False\n")
    result = _run([str(probe)])
    assert result.returncode == 1, result.stdout + result.stderr
    assert "SURVIVED the run" in result.stdout, result.stdout
