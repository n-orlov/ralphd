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


def test_script_puts_the_userbase_bin_directory_on_path_for_the_import_check():
    """Task 068 (steer 005 defect 1): `pip install --user` writes its console
    scripts (ralphctl, ralphd-engine) into $DEV_USERBASE/bin, a directory on
    no process's PATH by default. The import-path check shares $DEV_HOME /
    $DEV_USERBASE with both pytest invocations below it, so it must resolve
    PATH the same way they do -- computed once and reused, not recomputed."""
    text = SCRIPT.read_text()
    assert 'DEV_USERBASE_BIN_PATH="$DEV_USERBASE/bin:$PATH"' in text, (
        "the userbase bin directory must be prepended onto PATH, computed"
        " once from $DEV_USERBASE"
    )
    computed_idx = text.index('DEV_USERBASE_BIN_PATH="$DEV_USERBASE/bin:$PATH"')
    import_idx = text.index("IMPORT_PATH=")
    assert computed_idx < import_idx, (
        "DEV_USERBASE_BIN_PATH must be computed before the import-path check"
        " uses it"
    )
    stmt_end = text.index(')"', import_idx)
    import_stmt = text[import_idx:stmt_end]
    assert 'PATH="$DEV_USERBASE_BIN_PATH"' in import_stmt, (
        "the import-path check must hand the pytest/install PATH override to"
        f" its own subprocess too, got: {import_stmt!r}"
    )


def test_script_puts_the_userbase_bin_directory_first_on_path_for_both_pytest_modes():
    """Both PYTHONUSERBASE_MODE branches (exported and unset) run pytest
    against the SAME $DEV_USERBASE/bin console scripts -- the mode only
    decides whether PYTHONUSERBASE itself reaches the pytest subprocess's
    *import* path, a separate question from where the console scripts this
    suite shells out to live. Both branches must therefore export PATH."""
    text = SCRIPT.read_text()
    branches_text = text[text.index('if [ "$PYTHONUSERBASE_MODE" = "unset" ]; then'):]
    assert branches_text.count('PATH="$DEV_USERBASE_BIN_PATH"') >= 2, (
        "both the 'unset' and 'exported' pytest invocations must put"
        " $DEV_USERBASE/bin on PATH"
    )
    unset_branch = branches_text[: branches_text.index("else")]
    assert "env -u PYTHONUSERBASE" in unset_branch, (
        "the 'unset' branch must still strip PYTHONUSERBASE from the pytest"
        " subprocess's environment -- putting the console-script bin"
        " directory on PATH must not reintroduce PYTHONUSERBASE"
    )


@pytest.mark.parametrize(
    "mode_env",
    [
        pytest.param(None, id="exported-default-mode"),
        pytest.param(
            {"HOST_LIKE_DEV_LAYOUT_PYTHONUSERBASE_MODE": "unset"}, id="unset-mode"
        ),
    ],
)
def test_ralphctl_console_script_resolves_into_the_userbase_bin_directory(
    tmp_path, mode_env
):
    """Task 068 (steer 005 defect 1), driven end to end: in EACH
    PYTHONUSERBASE_MODE, the pytest subprocess's own PATH must resolve
    `ralphctl` (a console script `pip install --user` wrote into
    $DEV_USERBASE/bin) to a path under this invocation's own $HOME/userbase --
    never to this container's system-wide copy at /usr/local/bin/ralphctl
    (container/Dockerfile:42), which is exactly the copy that has silently
    masked this defect on every prior green run of this script (steer 005)."""
    probe_dir = tmp_path / "probe"
    probe_dir.mkdir()
    probe = probe_dir / "test_ralphctl_on_path_probe.py"
    probe.write_text(
        "import os\n"
        "import shutil\n"
        "\n"
        "\n"
        "def test_ralphctl_resolves_under_the_dev_userbase_not_the_system_copy():\n"
        "    home = os.environ.get('HOME')\n"
        "    assert home, 'HOME was not exported to the pytest subprocess'\n"
        "    resolved = shutil.which('ralphctl')\n"
        "    assert resolved is not None, (\n"
        "        'ralphctl is not on PATH inside the pytest subprocess -- '\n"
        "        f'PATH={os.environ.get(\"PATH\")!r}'\n"
        "    )\n"
        "    assert resolved.startswith(home), (\n"
        "        f'ralphctl resolved to {resolved!r}, not under $HOME '\n"
        "        f'({home!r}) -- PATH did not put $DEV_USERBASE/bin first, '\n"
        "        'so this resolved the system-wide copy instead'\n"
        "    )\n"
    )
    result = _run([str(probe)], extra_env=mode_env)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "1 passed" in result.stdout, result.stdout
