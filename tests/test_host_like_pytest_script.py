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
import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts" / "host_like_pytest.sh"

# The sha of the commit immediately before task 001's header fix -- the
# header at this sha literally claims "in this environment a usable socket
# is present essentially every time this script runs here", so it is the
# mutation target the always-a-socket guard below must reject.
PRE_FIX_SHA = "7f153298de9f5f495e05ab669dc8c31b67cb302f"

# --- wording-independent detector for the false always-a-socket claim ---
#
# F2 (#74). The point is to reject the *claim*, not one phrasing of it: a
# header that says "in this environment there is always a usable docker
# socket" or "a usable docker socket will always be present in this
# environment" is exactly as false as the sentence that actually shipped,
# so all of them must fail. The detector therefore works clause by clause
# and is insensitive to word order inside a clause: a clause is an offender
# when it mentions a socket, asserts its presence, quantifies that presence
# universally, and carries no negation or hedge that would turn the
# universal into a denial or a qualification.
_CLAUSE_SPLIT = re.compile(r"[.;:!?]+|\s--+\s")
_SOCKET_MENTION = re.compile(r"\bsockets?\b")
_PRESENCE_WORD = re.compile(
    r"\b(present|presence|there is|there are|there's|is there|are there"
    r"|be there|sits there|available|mounted|bind-mounted|exists|exist"
    r"|in place|around|have|has|had|attached|provided|supplied|reachable"
    r"|usable)\b"
)
_UNIVERSAL_QUANTIFIER = re.compile(
    r"\b(always|invariably|universally|unconditionally|guaranteed|guarantees"
    r"|every time|each time|any time|anytime|without exception|in every case"
    r"|in all cases|in every (?:ralphd )?(?:job )?container"
    r"|every (?:ralphd )?(?:job )?container"
    r"|all (?:ralphd )?(?:job )?containers"
    r"|any (?:ralphd )?(?:job )?container)\b"
)
_HEDGE_OR_NEGATION = re.compile(
    r"\b(not|never|n't|no|nor|rarely|seldom|hardly|may or may not|some"
    r"|sometimes|often|usually|most|might|occasionally|whether|unless)\b"
)


def _header_comment_text(script_text: str) -> str:
    """The script's `#`-comment lines with their markers stripped, so the
    checks below cannot be defeated by rewrapping a sentence across lines."""
    return " ".join(
        line[1:] for line in script_text.splitlines() if line.startswith("#")
    )


def _always_a_socket_claims(header_text: str) -> list[str]:
    """Every clause of ``header_text`` that claims a usable docker socket is
    always (or essentially always) present here. Empty list == no such
    claim."""
    normalized = " ".join(header_text.replace("*", "").split()).lower()
    offenders = []
    for clause in _CLAUSE_SPLIT.split(normalized):
        clause = clause.strip()
        if not clause or not _SOCKET_MENTION.search(clause):
            continue
        if not _PRESENCE_WORD.search(clause):
            continue
        if not _UNIVERSAL_QUANTIFIER.search(clause):
            continue
        if _HEDGE_OR_NEGATION.search(clause):
            # "not always", "some containers do", "may or may not" ... --
            # a qualification or denial, not an always-present assertion.
            continue
        offenders.append(clause)
    return offenders


# Paraphrases of the false claim. Every one of these must be rejected --
# including the two that slipped through an earlier, wording-bound version
# of this guard (the first two entries).
FORBIDDEN_ALWAYS_A_SOCKET_PARAPHRASES = [
    "In this environment there is always a usable docker socket.",
    "A usable docker socket will always be present in this environment.",
    "In this environment a usable socket is present essentially every time"
    " this script runs here.",
    "Every ralphd job container has a usable docker socket bind-mounted in.",
    "All ralphd job containers ship with a usable docker socket.",
    "The docker socket is invariably available inside a ralphd job container.",
    "A working socket is guaranteed to be mounted here.",
    "Here, the docker socket is there every time.",
    "The socket is always present, so the tier is filtered out.",
    "Because a usable docker socket is universally available here, the"
    " docker tier runs nested.",
    "When this script runs here, a usable docker socket is always present.",
]

# Honest, branch-describing wording that must keep passing, so the guard
# cannot be satisfied by deleting the header's docker-tier discussion.
ALLOWED_SOCKET_WORDINGS = [
    "A ralphd job container may or may not have a usable docker socket"
    " bind-mounted in.",
    "Some ralphd job containers do have a usable socket at invocation time,"
    " but not every one does.",
    "When a usable socket is present, the script filters the docker tier"
    " out with -m 'not docker'.",
    "When no usable socket is present the script injects nothing extra for"
    " the docker tier.",
    "This container has no usable docker socket right now.",
    "The docker socket is not always present in this environment.",
    "Whenever this script detects a usable socket it filters the docker"
    " tier out instead of running it nested.",
    "Every RALPHD_* environment variable is unset before the run.",
]


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


def test_header_rejects_the_false_always_a_socket_claim_about_this_environment():
    """F2 (#74) mutation guard, sibling to the self-skip-framing test above:
    the actual false premise an earlier header shipped was not just "it
    self-skips" -- the sentence preceding that claimed "in *this*
    environment a usable socket is present essentially every time this
    script runs here", which is also false: plenty of ralphd job
    containers, including this one, have no usable docker socket at all
    (see the docs/gotchas above and the header's own current wording). A
    fix that only rewords the self-skip sentence while leaving an
    always/essentially-always-present claim about the socket intact would
    still pass the older assertions above but must fail this one. Header
    lines are collapsed across the `#` comment markers and line wraps so
    the check is not defeated by rewrapping alone."""
    text = SCRIPT.read_text()
    header_lines = [
        line[1:] for line in text.splitlines() if line.startswith("#")
    ]
    normalized = " ".join(" ".join(header_lines).split()).lower()
    always_present_claim = re.compile(
        r"usable (?:docker )?socket is (?:present |there )?"
        r"(?:essentially )?(?:always|every time)"
    )
    assert not always_present_claim.search(normalized), (
        "header must not claim a usable docker socket is always, or"
        " essentially always, present in this environment (F2, #74) --"
        f" matched: {always_present_claim.search(normalized)}"
    )
    assert "essentially every time this script runs" not in normalized
    # Wording-independent form of the same rejection: the literal regex
    # above only catches sentences that open "usable socket is ...", which
    # a reworded-but-equally-false header would walk straight past.
    assert _always_a_socket_claims(_header_comment_text(text)) == [], (
        "header must not claim, in any wording, that a usable docker socket"
        " is always present in this environment (F2, #74) -- offending"
        f" clauses: {_always_a_socket_claims(_header_comment_text(text))}"
    )


@pytest.mark.parametrize("wording", FORBIDDEN_ALWAYS_A_SOCKET_PARAPHRASES)
def test_the_always_a_socket_guard_rejects_every_paraphrase_not_one_wording(
    wording,
):
    """F2 (#74): the guard above must reject the *claim*, however it is
    phrased. An earlier version of it matched only headers whose sentence
    opened "usable socket is ...", so a header saying "in this environment
    there is always a usable docker socket" -- just as false -- passed it.
    Each paraphrase here is fed through the detector as if it were the
    header, and must be flagged."""
    assert _always_a_socket_claims(wording) != [], (
        "the always-a-socket guard let a false paraphrase through, so it is"
        f" wording-bound rather than claim-bound: {wording!r}"
    )


@pytest.mark.parametrize("wording", ALLOWED_SOCKET_WORDINGS)
def test_the_always_a_socket_guard_leaves_honest_branch_wording_alone(wording):
    """The mirror of the test above: the detector must not be a blanket ban
    on the words "socket" and "always", or the only way to satisfy it would
    be to delete the header's honest description of both branches."""
    assert _always_a_socket_claims(wording) == [], (
        "honest, hedged branch wording must not be flagged as the false"
        f" always-a-socket claim: {wording!r} -> {_always_a_socket_claims(wording)}"
    )


def test_the_always_a_socket_guard_fails_on_the_pre_fix_header():
    """In-repo mutation proof for F2 (#74): applied to the header as it
    stood immediately before task 001's fix (``git show
    <PRE_FIX_SHA>:scripts/host_like_pytest.sh``), the detector must flag
    the claim -- otherwise the guard proves nothing about the regression it
    exists to catch."""
    completed = subprocess.run(
        ["git", "show", f"{PRE_FIX_SHA}:scripts/host_like_pytest.sh"],
        cwd=REPO,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        pytest.skip(
            "pre-fix blob unreachable (shallow clone or missing object):"
            f" {completed.stderr.strip()[:200]}"
        )
    offenders = _always_a_socket_claims(_header_comment_text(completed.stdout))
    assert offenders != [], (
        "the pre-fix header claims a usable socket is present essentially"
        " every time this script runs here -- the guard must reject it"
    )
    assert any("essentially every time" in clause for clause in offenders)


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


def test_script_injects_nothing_when_no_usable_docker_socket_is_present(tmp_path):
    """Pins the socket-absent direction (#109). Chosen policy: **injects
    nothing, docker-marked tests self-skip at runtime** -- when this
    script's own probe finds no usable docker socket (mirroring a bare
    developer host), it must NOT add ``-m "not docker"`` on the caller's
    behalf. The docker-marked module is left to its own module-level
    ``skipif`` (``tests/test_docker_sibling_e2e.py``'s
    ``_docker_available()``) to self-skip cleanly at runtime, the same way
    it would for a developer invoking the bare acceptance command by hand
    with no socket around. Proven here at collection time: with
    ``DOCKER_SOCK_PATH`` pointed at a path that does not exist (so the
    script's own ``-S`` probe fails regardless of any ambient docker socket
    this container happens to have), the docker-marked module's tests are
    still *collected* (no ``-m "not docker"`` deselection), unlike the
    usable-socket case above where they are deselected to zero."""
    absent_sock_path = tmp_path / "does-not-exist" / "docker.sock"
    assert not absent_sock_path.exists()

    env = dict(os.environ)
    env["PYTHON"] = sys.executable
    env["DOCKER_SOCK_PATH"] = str(absent_sock_path)
    result = subprocess.run(
        ["bash", str(SCRIPT), "tests/test_docker_sibling_e2e.py", "--collect-only"],
        cwd=str(REPO),
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
    )
    combined = result.stdout + result.stderr
    assert result.returncode == 0, combined
    assert "no tests collected" not in combined, combined
    assert "deselected" not in combined, combined


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
