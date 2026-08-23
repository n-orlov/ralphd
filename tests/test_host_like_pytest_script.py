"""Task 009 (#57, #50): `scripts/host_like_pytest.sh` is the reproducible
mechanism behind the host-like suite run recorded in
`artifacts/reports/task-009-suite-comparison.md`. This test proves the
script actually assembles a host-like environment (every `RALPHD_*` env var
unset, a decoy top-level `tests` package ahead of the repo on `PYTHONPATH`,
a fresh `$HOME`) without running the whole suite here -- that would make
the fast tier's own collection ~11 minutes long twice over. The one full
run lives in the report, not in CI; this test just proves the mechanism.
"""

from __future__ import annotations

import os
import subprocess
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
        "def test_no_ralphd_env_vars_survive():\n"
        "    leaked = sorted(k for k in os.environ if k.startswith('RALPHD_'))\n"
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
    import sys

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
