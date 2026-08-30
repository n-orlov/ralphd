"""Task 009 (#57, #50, F1/#73): `scripts/host_like_pytest.sh` is the
reproducible mechanism behind the host-like suite run recorded in
`artifacts/reports/task-009-suite-comparison.md`. This test proves the
script actually assembles a host-like environment (every `RALPHD_*` env var
unset *except* `RALPHD_RUN_ID`, which is preserved or minted fresh -- see
F1/#73 below -- a decoy top-level `tests` package ahead of the repo on
`PYTHONPATH`, a fresh `$HOME`) without running the whole suite here -- that
would make the fast tier's own collection ~11 minutes long twice over. The
one full run lives in the report, not in CI; this test just proves the
mechanism.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts" / "host_like_pytest.sh"


def test_script_exists_and_is_executable():
    assert SCRIPT.is_file(), SCRIPT
    assert os.access(SCRIPT, os.X_OK), f"{SCRIPT} is not executable"


def test_script_unsets_ralphd_vars_uses_a_fresh_home_and_shadows_tests_package(tmp_path):
    """Write a throwaway test module that asserts, from inside the
    subprocess the script actually execs, all three host-like properties
    at once: no ``RALPHD_*`` var survives, ``$HOME`` is not this test's real
    one, and ``tests.conftest`` (imported the same way the real suite's
    modules do) still resolves to *this* repo's copy even with a decoy
    planted ahead of it on ``PYTHONPATH`` -- proving the decoy the script
    plants does not, in fact, win (the #50 regression this whole mechanism
    exists to keep catching)."""
    probe_dir = tmp_path / "probe"
    probe_dir.mkdir()
    real_home = os.environ.get("HOME", "")
    probe = probe_dir / "test_host_like_probe.py"
    probe.write_text(
        "import os\n"
        "import tests.conftest as real_conftest\n"
        "\n"
        "\n"
        "def test_no_ralphd_env_vars_survive_except_run_id():\n"
        "    # RALPHD_RUN_ID is deliberately preserved/minted (F1, #73) --\n"
        "    # every OTHER RALPHD_* var must still be gone.\n"
        "    leaked = sorted(\n"
        "        k for k in os.environ\n"
        "        if k.startswith('RALPHD_') and k != 'RALPHD_RUN_ID'\n"
        "    )\n"
        "    assert leaked == []\n"
        "\n"
        "\n"
        "def test_home_is_not_the_real_one():\n"
        f"    assert os.environ.get('HOME') != {real_home!r}\n"
        "\n"
        "\n"
        "def test_tests_conftest_is_this_repos_copy_despite_the_decoy():\n"
        "    assert real_conftest.RALPHCTL != 'not-the-real-ralphctl'\n"
    )

    env = dict(os.environ)
    # Drive the script with an explicit interpreter override (PYTHON=...)
    # instead of relying on this container's own .venv living at the
    # hard-coded path the script used to require -- a developer host's venv
    # can live anywhere, and this is the only way to actually exercise the
    # override branch rather than the .venv fallback (task 043).
    env["PYTHON"] = sys.executable
    result = subprocess.run(
        ["bash", str(SCRIPT), str(probe)],
        cwd=str(REPO),
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "3 passed" in result.stdout, result.stdout


def test_script_preserves_or_mints_run_id_so_the_docker_tag_is_never_shared(tmp_path):
    """F1 (#73): scrubbing RALPHD_RUN_ID away collapses the docker tier's
    per-run tag isolation (``ralphd:test-<RALPHD_RUN_ID>``, see
    tests/test_docker_sibling_e2e.py) to the shared fallback
    ``ralphd:test-local-dev`` that module falls back to when the variable is
    absent -- exactly the collision the per-run tag exists to prevent. The
    script must preserve an inherited RALPHD_RUN_ID unchanged, or mint a
    fresh, unique one when none is inherited; either way the tag the docker
    tier would build from it must never be the shared fallback, and two runs
    with no inherited id must not mint the same one. A mutation of the
    script back to unconditionally scrubbing RALPHD_RUN_ID makes this test
    fail (the sentinel file stays empty)."""

    def run_and_capture_run_id(extra_env=None, sentinel_name="run_id.txt"):
        probe_dir = tmp_path / sentinel_name.replace(".", "_")
        probe_dir.mkdir()
        sentinel = probe_dir / sentinel_name
        probe = probe_dir / "test_run_id_probe.py"
        probe.write_text(
            "import os\n"
            "\n"
            "\n"
            "def test_write_run_id():\n"
            f"    with open({str(sentinel)!r}, 'w') as f:\n"
            "        f.write(os.environ.get('RALPHD_RUN_ID', ''))\n"
        )
        env = dict(os.environ)
        env["PYTHON"] = sys.executable
        if extra_env:
            env.update(extra_env)
        else:
            env.pop("RALPHD_RUN_ID", None)
        result = subprocess.run(
            ["bash", str(SCRIPT), str(probe)],
            cwd=str(REPO),
            capture_output=True,
            text=True,
            env=env,
            timeout=60,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        assert sentinel.is_file(), "probe never ran"
        return sentinel.read_text()

    run_id_a = run_and_capture_run_id(sentinel_name="run_id_a.txt")
    run_id_b = run_and_capture_run_id(sentinel_name="run_id_b.txt")
    inherited_run_id = run_and_capture_run_id(
        extra_env={"RALPHD_RUN_ID": "operator-supplied-id"},
        sentinel_name="run_id_c.txt",
    )

    for run_id in (run_id_a, run_id_b, inherited_run_id):
        assert run_id, "RALPHD_RUN_ID must not be scrubbed away (F1, #73)"
        tag = f"ralphd:test-{run_id}"
        assert tag != "ralphd:test-local-dev", tag

    assert run_id_a != run_id_b, (
        "two invocations with no inherited RALPHD_RUN_ID must not mint the"
        " same one, or they would still collide on the shared docker tag"
    )
    assert inherited_run_id == "operator-supplied-id", (
        "an inherited RALPHD_RUN_ID must survive unchanged, not be replaced"
    )


def test_script_discovers_ralphd_vars_dynamically_not_a_hardcoded_list():
    """A rewrite that hardcodes a fixed list of names would silently stop
    covering a `RALPHD_*` variable added anywhere later; the script must
    keep discovering them from the live environment at run time."""
    text = SCRIPT.read_text()
    assert "RALPHD_*" in text
    assert "unset_args" in text and '"-u"' in text
    assert "RALPHD_BIND" not in text and "RALPHD_RUN_DIR" not in text, (
        "must not degrade to a hardcoded list of variable names"
    )


def test_script_places_decoy_ahead_of_repo_on_pythonpath():
    text = SCRIPT.read_text()
    assert "DECOY_CONFTEST" in text
    assert 'PYTHONPATH="$DECOY_ROOT' in text, "decoy must be prepended, not appended"


def test_the_decoy_is_a_regular_package_not_a_namespace_portion():
    """#64: a decoy without `__init__.py` is a namespace *portion*, which loses
    to plain `sys.path` ordering -- so planting one proved almost nothing. The
    shape that ships in the wild (and that broke this suite: two installed
    wheels with a top-level `tests/`) is a regular package, which pre-empts this
    repo's portion from anywhere on `sys.path`. Assert the script plants that
    one, or its host-like claim is weaker than the environment it models."""
    text = SCRIPT.read_text()
    assert '"$DECOY_ROOT/tests/__init__.py"' in text, (
        "the decoy must be a regular package (see #64) -- a namespace portion"
        " decoy is beaten by sys.path order alone"
    )


def test_header_makes_no_self_skip_claim_about_the_docker_or_browser_tiers():
    """F2 (#74): an earlier header claimed the docker (and browser) tiers
    self-skip cleanly here for want of a socket/browser -- false, since this
    script always runs inside a job container that has a real docker socket
    mounted in. The header must describe what actually happens (the tier is
    detected and filtered out, not left to self-skip) and must cite #74, not
    repeat the false premise."""
    text = SCRIPT.read_text()
    assert "#74" in text
    lowered = text.lower()
    assert "self-skip" not in lowered or "false" in lowered, (
        "a stray 'self-skip' claim must be framed as the false premise being"
        " corrected, not restated as fact"
    )
    assert "no docker socket" not in lowered, (
        "the header must not claim this environment lacks a docker socket"
    )


def test_script_filters_the_docker_tier_out_when_a_usable_socket_is_present(tmp_path):
    """F2 (#74): rather than let the docker-marked modules attempt to run
    nested (and time out against a bridge-gateway port this container
    cannot reach), the script must detect a usable docker socket and
    exclude the tier with ``-m "not docker"`` -- proven here by collecting
    a real docker-marked module through the script and asserting nothing
    from it is collected.

    Made hermetic (#109): this must hold regardless of whether *this*
    container happens to have a real docker socket mounted in, so it
    fabricates its own usable-socket precondition instead of relying on
    the ambient environment -- a fake ``docker`` executable on ``PATH``
    that exits 0 for ``docker version`` (matching the script's own probe,
    ``command -v docker`` + ``-S "${DOCKER_SOCK_PATH:-/var/run/docker.sock}"``
    + a ``docker version`` round-trip), plus a real bound unix socket at a
    path handed to the script via ``DOCKER_SOCK_PATH`` so the ``-S`` test
    passes without touching ``/var/run/docker.sock``."""
    import socket as socket_module

    fake_bin_dir = tmp_path / "fake_bin"
    fake_bin_dir.mkdir()
    fake_docker = fake_bin_dir / "docker"
    fake_docker.write_text("#!/usr/bin/env bash\nexit 0\n")
    fake_docker.chmod(0o755)

    sock_path = tmp_path / "fake-docker.sock"
    fake_socket = socket_module.socket(socket_module.AF_UNIX, socket_module.SOCK_STREAM)
    fake_socket.bind(str(sock_path))
    fake_socket.listen(1)
    try:
        assert sock_path.is_socket()

        env = dict(os.environ)
        env["PYTHON"] = sys.executable
        env["PATH"] = f"{fake_bin_dir}{os.pathsep}{env.get('PATH', '')}"
        env["DOCKER_SOCK_PATH"] = str(sock_path)
        result = subprocess.run(
            ["bash", str(SCRIPT), "tests/test_docker_sibling_e2e.py", "--collect-only"],
            cwd=str(REPO),
            capture_output=True,
            text=True,
            env=env,
            timeout=60,
        )
    finally:
        fake_socket.close()
    # exit code 5 is pytest's own "no tests collected" -- expected here since
    # every test in the module is deselected, not an error in the script.
    assert result.returncode == 5, result.stdout + result.stderr
    assert "no tests collected" in (result.stdout + result.stderr), (
        result.stdout + result.stderr
    )
    assert "deselected" in (result.stdout + result.stderr)


def test_script_lets_an_explicit_marker_argument_override_the_default(tmp_path):
    """An explicit ``-m`` after the script's own filename must win over the
    default docker-exclusion the script injects -- proven by asking for only
    the docker tier explicitly and getting it collected (not deselected to
    zero)."""
    env = dict(os.environ)
    env["PYTHON"] = sys.executable
    result = subprocess.run(
        [
            "bash",
            str(SCRIPT),
            "tests/test_docker_sibling_e2e.py",
            "--collect-only",
            "-m",
            "docker",
        ],
        cwd=str(REPO),
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    combined = result.stdout + result.stderr
    assert "no tests collected" not in combined, combined


def test_script_loads_the_tests_package_pin_as_a_plugin():
    """The script's targets can be outside the repository (the probe above is),
    and pytest only loads the conftests on the path to its arguments -- so the
    repo-root conftest.py, and with it the `tests` pin, may never load at all.
    `-p tests_package_pin` is what makes the guarantee hold for this
    invocation; without it the regular-package decoy the script itself plants
    wins. `tests/test_tests_conftest_resolution.py` proves that by mutation."""
    text = SCRIPT.read_text()
    assert "-p tests_package_pin" in text, (
        "out-of-tree targets never load the repo-root conftest, so the pin has"
        " to arrive as a plugin (#64)"
    )
    assert (REPO / "tests_package_pin.py").is_file()
