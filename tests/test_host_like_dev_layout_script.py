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
import tempfile
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
    # nightly.yml sets the results directory for the whole cell; a nested run
    # of the script under test must not write its own results into it.
    env.pop("HOST_LIKE_DEV_LAYOUT_ALLURE_DIR", None)
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
        "import pytest\n"
        "import ralphd\n"
        "\n"
        "\n"
        # Task 076: the script's own default pytest selection is now `-m
        # fast` (see scripts/host_like_dev_layout.sh's header) -- this probe
        # lives outside the repo's own tests/ tree, so tests/conftest.py's
        # derivation never marks it; it must mark itself `fast` to stay
        # selected under that default, the same way a real host-repo test
        # would arrive already marked by that derivation.
        "@pytest.mark.fast\n"
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
    # Marked `fast` for the same reason as the import-path probe above --
    # this probe must survive the script's default `-m fast` selection.
    probe.write_text(
        "import pytest\n"
        "\n"
        "\n"
        "@pytest.mark.fast\n"
        "def test_fails():\n"
        "    assert False\n"
    )
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
    # Marked `fast` for the same reason as the other probes in this module --
    # this probe must survive the script's default `-m fast` selection
    # (task 076).
    probe.write_text(
        "import os\n"
        "import shutil\n"
        "\n"
        "import pytest\n"
        "\n"
        "\n"
        "@pytest.mark.fast\n"
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


# --- (a) the PEP 668 / EXTERNALLY-MANAGED branch (task 066) ------------------
# Task 076 (steer 008) re-pins task 066's behaviour with NAMED nodes: the
# container matrix's `ubuntu:24.04` cell is externally managed, so this branch
# is the difference between that cell installing and that cell failing 20
# seconds in -- it must not be possible to break it silently.
#
# The behavioural nodes below drive the branch through a STUB interpreter
# rather than a real one, because this container's own `python3` decides the
# question by itself (its stdlib either carries the marker or does not) and
# task 066 could not reach the marker-present branch here at all. The stub
# answers each probe the script makes of its interpreter (the user-site
# check, the marker path, `site.getuserbase()`, the post-install import path)
# and records every argv and environment it is handed, so "exactly when the
# marker is present" is observed in BOTH directions from the script's own
# recorded `pip install` invocation -- no real install, no lane, no
# interpreter this container does not have. What that does NOT prove is that
# pip itself accepts the flag on a genuinely externally-managed interpreter:
# that is the real host's to confirm (the operator's nightly did, in steer
# 005's report) and it is exactly what the `ubuntu:24.04` cell of task 077's
# matrix exercises on every run.

_STUB_INTERPRETER = r"""#!/usr/bin/env bash
# Test-only stub interpreter, written by
# tests/test_host_like_dev_layout_script.py. Records what it was asked and
# answers each of the script's own interpreter probes.
printf 'HOME=%s\tPYTHONUSERBASE=%s\tARGS=%s\n' \
  "${HOME:-}" "${PYTHONUSERBASE:-<unset>}" "$*" >> '__LOG__'
case "$*" in
  *ENABLE_USER_SITE*)   exit 0 ;;
  *EXTERNALLY-MANAGED*) printf '%s\n' '__MARKER__'; exit 0 ;;
  *getuserbase*)        printf '%s\n' "${HOME:-}/.local"; exit 0 ;;
  *"import ralphd"*)    printf '%s\n' '__REPO__/src/ralphd/__init__.py'; exit 0 ;;
  *) exit 0 ;;
esac
"""


def _stub_interpreter(tmp_path, marker_path):
    """Write the stub interpreter; return (its path, its log path)."""
    log = tmp_path / "stub-interpreter.log"
    stub = tmp_path / "stub-python3"
    stub.write_text(
        _STUB_INTERPRETER.replace("__LOG__", str(log))
        .replace("__MARKER__", str(marker_path))
        .replace("__REPO__", str(REPO))
    )
    stub.chmod(0o755)
    return stub, log


def _recorded_pip_invocation(log):
    """The one recorded invocation that is the `pip install` step."""
    text = log.read_text()
    for line in text.splitlines():
        fields = dict(field.split("=", 1) for field in line.split("\t"))
        if "-m pip install" in fields.get("ARGS", ""):
            return fields
    raise AssertionError(f"no `-m pip install` invocation recorded in {log}:\n{text}")


def _synthetic_stdlib_marker(tmp_path, present):
    stdlib = tmp_path / "stdlib"
    stdlib.mkdir()
    marker = stdlib / "EXTERNALLY-MANAGED"
    if present:
        marker.write_text("[externally-managed]\nError=synthetic PEP 668 marker\n")
    else:
        assert not marker.exists()
    return marker


@pytest.mark.parametrize(
    "marker_present", [True, False], ids=["marker-present", "marker-absent"]
)
def test_break_system_packages_is_passed_exactly_when_the_marker_is_present(
    tmp_path, marker_present
):
    """Task 066, re-pinned by task 076 (a): the script probes the SELECTED
    interpreter for PEP 668's `EXTERNALLY-MANAGED` marker and adds
    `--break-system-packages` to its `pip install --user` if and only if that
    marker exists -- an interpreter with no marker is invoked with no extra
    flag at all, exactly as before task 066."""
    marker = _synthetic_stdlib_marker(tmp_path, marker_present)
    stub, log = _stub_interpreter(tmp_path, marker)

    result = _run(
        ["--collect-only", "-q"], extra_env={"PYTHON": str(stub)}, timeout=60
    )
    assert result.returncode == 0, result.stdout + result.stderr

    pip = _recorded_pip_invocation(log)
    assert "pip install --user" in pip["ARGS"], pip["ARGS"]
    if marker_present:
        assert "--break-system-packages" in pip["ARGS"], pip["ARGS"]
        assert "is externally managed" in result.stdout, result.stdout
        assert (
            "--break-system-packages into the throwaway user base"
            in " ".join(result.stdout.split())
        ), result.stdout
    else:
        assert "--break-system-packages" not in pip["ARGS"], pip["ARGS"]
        assert "externally managed" not in result.stdout, result.stdout
        assert "--break-system-packages" not in result.stdout, result.stdout


@pytest.mark.parametrize(
    "marker_present", [True, False], ids=["marker-present", "marker-absent"]
)
def test_the_pep668_install_targets_only_the_throwaway_user_base(
    tmp_path, marker_present
):
    """The other half of (a), in BOTH marker states: whatever the flag
    decision, the install is pointed at the user base derived from the
    script's own `mktemp -d` scratch $HOME -- never the system site-packages,
    and never the invoking user's real $HOME. That is what makes
    `--break-system-packages` a lifted blanket refusal here rather than a
    licence to write into /usr (see the script's own comment on the flag)."""
    marker = _synthetic_stdlib_marker(tmp_path, marker_present)
    stub, log = _stub_interpreter(tmp_path, marker)
    real_home = os.environ["HOME"]

    result = _run(
        ["--collect-only", "-q"], extra_env={"PYTHON": str(stub)}, timeout=60
    )
    assert result.returncode == 0, result.stdout + result.stderr

    pip = _recorded_pip_invocation(log)
    userbase = pip["PYTHONUSERBASE"]
    assert userbase not in ("", "<unset>"), pip
    # The stub derives its answer from whatever $HOME the SCRIPT handed it,
    # the way `site.getuserbase()` does -- so this asserts on the home the
    # script actually built, not on a value this test chose.
    assert userbase.endswith("/home/.local"), userbase
    assert userbase.startswith(tempfile.gettempdir() + os.sep), userbase
    assert not userbase.startswith("/usr"), userbase
    assert not userbase.startswith(real_home + os.sep), (
        f"the install targeted the invoking user's real $HOME: {userbase}"
    )
    dev_home = userbase[: -len("/.local")]
    assert pip["HOME"] == dev_home, pip
    # ...and that scratch tree is gone once the script's own EXIT trap fired.
    assert not Path(dev_home).exists(), (
        f"{dev_home} outlived the script -- the install target must be a"
        " throwaway the script removes"
    )


def test_the_marker_probe_reads_the_selected_interpreters_own_stdlib():
    """Structural companion to the two nodes above: the marker path is
    computed from `$PY`'s own `sysconfig.get_path("stdlib")` (so it follows a
    PYTHON= override and every matrix image's own layout) rather than
    hardcoded to one distro's path, and `--break-system-packages` appears
    nowhere in the script's body outside the branch guarded by that marker's
    existence."""
    text = SCRIPT.read_text()
    body = text[text.index("set -euo pipefail"):]
    probe_idx = body.index("EXTERNALLY_MANAGED_MARKER=")
    probe = body[probe_idx: body.index("PIP_EXTRA_ARGS=()", probe_idx)]
    assert '"$PY" -c' in probe, probe
    assert 'sysconfig.get_path("stdlib")' in probe, probe

    guard = 'if [ -f "$EXTERNALLY_MANAGED_MARKER" ]; then'
    guard_idx = body.index(guard)
    branch_end = body.index("\nfi\n", guard_idx)
    branch = body[guard_idx:branch_end]
    assert "PIP_EXTRA_ARGS+=(--break-system-packages)" in branch, branch
    assert branch.count("--break-system-packages") >= 2, (
        "the marker branch must both append the flag and report that it did"
    )
    # Only executable lines: the surrounding prose explains the flag at
    # length (and must be free to), so this is about what the script DOES.
    outside = [
        line
        for line in (body[:guard_idx] + body[branch_end:]).splitlines()
        if "--break-system-packages" in line and not line.lstrip().startswith("#")
    ]
    assert outside == [], (
        "--break-system-packages must be added only inside the branch guarded"
        f" by the EXTERNALLY-MANAGED marker's existence, found: {outside}"
    )


# --- (e) tier selection (task 076, steer 008) -------------------------------
# Observed from the script's own reported/echoed invocation, never by running
# a lane: each case below uses `--collect-only`, which never executes a test,
# so these stay seconds even though the install step they share with every
# other test in this module is not.


def test_the_header_states_both_reasons_the_fast_tier_is_the_default():
    """Task 076: the default is not self-evident, so the script's own header
    must carry BOTH reasons for it -- (i) what `-m fast` drops relative to
    unfiltered (the `docker` and `browser` tiers) is already gate-blocking in
    heavy.yml and does not vary with the interpreter axis a cell varies, and
    (ii) `-m fast` excludes `-m browser`, so no matrix cell ever drives a
    browser and heavy.yml's browser tier stays the only browser-bearing job
    on the self-hosted box -- which is what makes the single `browser-runner`
    label (steer 009) enough to serialise browser work across workflows."""
    header = SCRIPT.read_text()
    header = header[: header.index("set -euo pipefail")]
    # Read it as prose: drop each comment line's own `#` and collapse the
    # line wrapping, so these assertions are about what the header SAYS and
    # not about where its lines happen to break.
    prose = " ".join(
        line.lstrip().lstrip("#").strip() for line in header.splitlines()
    )
    prose = " ".join(prose.split())

    # (i)
    assert "heavy.yml" in prose
    assert "does not vary with the interpreter" in prose, prose[-2000:]
    # (ii)
    assert "-m fast` EXCLUDES `-m browser" in prose, prose[-2000:]
    assert "browser-runner" in prose, prose[-2000:]
    assert "across *workflows*" in prose, prose[-2000:]

    # And the exclusion the second reason rests on is real, not just claimed:
    # tests/conftest.py never stamps a `browser`-marked node with `fast`.
    conftest = (REPO / "tests" / "conftest.py").read_text()
    stamp = conftest[conftest.index("def pytest_collection_modifyitems"):]
    stamp = stamp[: stamp.index("item.add_marker(pytest.mark.fast)")]
    assert '"browser" in keywords' in stamp, stamp
    assert "continue" in stamp, stamp


def test_with_no_tier_named_the_invocation_carries_the_fast_selection():
    """Default: no HOST_LIKE_DEV_LAYOUT_TIER override at all."""
    result = _run(["--collect-only", "-q"])
    assert result.returncode == 0, result.stdout + result.stderr
    assert (
        "host_like_dev_layout.sh: pytest tier selection: -m fast" in result.stdout
    ), result.stdout
    assert "unfiltered" not in result.stdout


def test_an_explicit_tier_is_carried_instead_of_the_default():
    """A caller-named tier (here `smoke`) replaces the `fast` default."""
    result = _run(
        ["--collect-only", "-q"],
        extra_env={"HOST_LIKE_DEV_LAYOUT_TIER": "smoke"},
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert (
        "host_like_dev_layout.sh: pytest tier selection: -m smoke" in result.stdout
    ), result.stdout
    assert (
        "host_like_dev_layout.sh: pytest tier selection: -m fast" not in result.stdout
    ), result.stdout


def test_a_compound_tier_expression_is_carried_verbatim():
    """The header promises any marker expression pytest's own `-m` accepts is
    honoured verbatim, not just a single marker name."""
    result = _run(
        ["--collect-only", "-q"],
        extra_env={"HOST_LIKE_DEV_LAYOUT_TIER": "fast and not docker"},
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert (
        "host_like_dev_layout.sh: pytest tier selection: -m fast and not docker"
        in result.stdout
    ), result.stdout


@pytest.mark.parametrize(
    "tier, reason",
    [
        ("(", "its parentheses are unbalanced"),
        ("fast and (", "its parentheses are unbalanced"),
        ("   ", "it is blank"),
        ("fast;rm", "it contains a character no pytest marker expression uses"),
    ],
    ids=["open-paren", "trailing-open-paren", "blank", "stray-character"],
)
def test_a_tier_that_is_not_marker_expression_shaped_exits_2_before_the_install(
    tier, reason
):
    """The header's exit-code paragraph claims exit 2 for a
    HOST_LIKE_DEV_LAYOUT_TIER that is not even marker-expression shaped, so
    the script must actually check that -- and check it BEFORE the slow
    install step, which is the whole reason the check is worth having (a typo
    in a matrix cell's env otherwise surfaces as a pytest usage error minutes
    later). Observed from the script's own reported invocation and status; no
    lane is launched."""
    result = _run(["--collect-only", "-q"], extra_env={"HOST_LIKE_DEV_LAYOUT_TIER": tier})
    assert result.returncode == 2, (result.returncode, result.stdout, result.stderr)
    # Raw stderr, not whitespace-collapsed: the blank case's own value is
    # whitespace, and the message must quote it as given.
    assert f"HOST_LIKE_DEV_LAYOUT_TIER '{tier}'" in result.stderr.replace("\n", " "), (
        result.stderr
    )
    assert reason in " ".join(result.stderr.split()), result.stderr
    # Refused before the install: neither the tier echo nor any post-install
    # report of this script's was reached.
    assert "pytest tier selection" not in result.stdout, result.stdout
    assert "SURVIVED the run" not in result.stdout, result.stdout


def test_a_shape_valid_tier_pytest_rejects_is_pytests_own_usage_error():
    """The other half of the same claim, and the boundary the header states
    explicitly: this shell judges SHAPE only -- it cannot judge pytest's `-m`
    grammar, which lives in the interpreter being set up -- so an expression
    that is shaped like a marker expression but that pytest itself rejects
    reaches pytest and surfaces as pytest's own usage error (exit 4), with
    the script's tier echo already printed."""
    result = _run(
        ["--collect-only", "-q"],
        extra_env={"HOST_LIKE_DEV_LAYOUT_TIER": "fast and"},
    )
    assert result.returncode == 4, (result.returncode, result.stdout, result.stderr)
    assert (
        "host_like_dev_layout.sh: pytest tier selection: -m fast and"
        in result.stdout
    ), result.stdout


def test_the_header_documents_the_tier_shape_check_it_actually_performs():
    """Rejection of attempt 2 was a FALSE header claim (exit 2 promised for an
    invalid tier, no validation in the script). This node pins the pair: the
    exit-code paragraph names the shapes that exit 2, and the
    HOST_LIKE_DEV_LAYOUT_TIER entry discloses that meaning is pytest's to
    judge, with exit 4 named."""
    text = SCRIPT.read_text()
    header = text[: text.index("set -euo pipefail")]
    prose = " ".join(
        line.lstrip().lstrip("#").strip() for line in header.splitlines()
    )
    prose = " ".join(prose.split())
    assert "not even marker-expression SHAPED" in prose, prose[-2000:]
    assert "unbalanced parentheses" in prose, prose[-2000:]
    assert "exit 2 and a message naming the value" in prose, prose[-2000:]
    assert "does NOT judge the expression's meaning" in prose, prose[-2000:]
    assert "pytest's OWN usage error, exit 4" in prose, prose[-2000:]

    body = text[text.index("set -euo pipefail"):]
    assert 'TIER_INVALID="it is blank"' in body, body
    assert 'TIER_INVALID="its parentheses are unbalanced"' in body, body


def test_the_explicit_no_filter_form_adds_no_marker_flag():
    """`unfiltered` is the one documented "no filter" form -- the retired
    whole-suite invocation every version of this script ran before task 076,
    still reachable on request."""
    result = _run(
        ["--collect-only", "-q"],
        extra_env={"HOST_LIKE_DEV_LAYOUT_TIER": "unfiltered"},
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert (
        "host_like_dev_layout.sh: pytest tier selection: no marker filter"
        " (unfiltered)" in result.stdout
    ), result.stdout
    assert "-m fast" not in result.stdout
    assert "-m unfiltered" not in result.stdout
