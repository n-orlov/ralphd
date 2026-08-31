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
# socket", "a usable docker socket will always be present in this
# environment", "each run includes a usable docker socket" or "without
# fail, this environment supplies a usable docker socket" is exactly as
# false as the sentence that actually shipped, so all of them must fail.
# The detector therefore works clause by clause (and, so a comma cannot
# hide the quantifier from its own claim, comma-part by comma-part inside
# each clause) and is insensitive to word order: a part is an offender when
# it mentions a socket, asserts its presence/provision/possession by ANY of
# a broad family of verbs, quantifies that presence universally by ANY of a
# broad family of quantifiers, and carries no negation or hedge that turns
# the universal into a denial or a qualification. A double negative that
# spells the same universal ("never runs without a usable socket", "no
# container lacks a socket") is caught before the negation escape hatch.
# `.` only ends a clause when whitespace/end follows it, so a path like
# /var/run/docker.sock cannot split a sentence and hide its own subject.
_CLAUSE_SPLIT = re.compile(r"(?:[.;:!?]+(?=\s|$))|\s--+\s")
_SOCKET_MENTION = re.compile(r"\bsockets?\b")
# Anaphora: "The docker socket? It is there every time." makes the claim in a
# clause that never says "socket" -- a pronoun referring back to one does.
_ANAPHOR = re.compile(r"\b(it|one|they|these|those|that|this)\b")
# Presence / provision / possession / obtainability -- "is present", "is
# mounted", "includes", "comes with", "accompanies", "you get", "you can
# count on finding", "supplies", "exposes" ... all assert the same thing.
_PRESENCE_WORD = re.compile(
    r"\b(present|presence|there is|there are|there's|is there|are there"
    r"|be there|sits there|sit there|available|availability|mounted"
    r"|bind-mounted|mounts?|exists?|in place|around|have|has|had|having"
    r"|attached|provided|provides?|supplied|supplies|supply|reachable"
    r"|usable|includes?|included|including|comes? with|came with"
    r"|ships? with|shipped with|accompanies|accompany|accompanied"
    r"|offers?|grants?|hands?|exposes?|exposed|wired|equipped|carries|carry"
    r"|finds?|finding|found|gets?|getting|receives?|access to|count on"
    r"|counted on|rely on|relies on|depend on|depends on|guaranteed"
    r"|guarantees?|standard|default|furnished|furnishes|set up|live"
    r"|working|bundled|baked in|built in|built-in|comes as)\b"
)
# Universal quantification -- adverbs ("always", "invariably", "without
# fail"), frequency phrases ("every time", "at all times", "100% of the
# time") and any of every/each/all/any/both applied to an occasion or scope
# noun ("each run", "every invocation", "all containers", "any job").
_UNIVERSAL_QUANTIFIER = re.compile(
    r"\b(always|invariably|universally|unconditionally|unfailingly"
    r"|guaranteed|guarantees?|without fail|without exception|no exceptions?"
    r"|at all times|all the time|every time|each time|any time|anytime"
    r"|whenever|in every case|in all cases|in each case|100% of the time"
    r"|each and every|by default|as standard|out of the box"
    r"|(?:every|each|all|any|both)(?:\s+\w+){0,3}?\s+"
    r"(?:run|runs|time|times|invocation|invocations|case|cases|container"
    r"|containers|job|jobs|environment|environments|instance|instances"
    r"|session|sessions|execution|executions|host|hosts|machine|machines"
    r"|box|boxes|sandbox|sandboxes|script|scripts|call|calls|start|starts"
    r"|startup|launch|launches|checkout|checkouts|one|ones))\b"
)
_HEDGE_OR_NEGATION = re.compile(
    r"\b(not|never|n't|no|nor|none|nothing|cannot|lacks?|lacking|lacked"
    r"|absent|missing|unavailable|rarely|seldom|hardly|may or may not"
    r"|may|might|maybe|perhaps|possibly|some|sometimes|often|usually"
    r"|typically|generally|frequently|occasionally|most|many|several"
    r"|varies|vary|likely|probably|potentially|whether|unless)\b"
)
# A universal presence claim spelled as a double negative -- "this script
# never runs without a usable docker socket", "no ralphd job container
# lacks a usable socket", "the socket is never missing here". The negation
# escape hatch above would otherwise wave these through.
_DOUBLE_NEGATIVE_UNIVERSAL = re.compile(
    r"\b(never|not|no|nowhere)\b[^,]*?\b(without|lack\w*|missing|absent"
    r"|unavailable|optional|short of|devoid of)\b"
)
# Honest branch wording makes the socket's presence the *condition* of the
# sentence, not its claim: "whenever this script detects a usable socket,
# it filters the tier out", "when no usable socket is present ...". Those
# must keep passing, or the only way to satisfy the guard would be to
# delete the header's description of both branches.
_CONDITION_ON_DETECTION = re.compile(
    r"\b(when|whenever|if|once|unless)\b(?:\s+\w+){0,4}?\s+"
    r"(detects?|detected|detecting|finds?|found|sees?|discovers?|probes?"
    r"|checks?|tests? for)\b"
)
_CONDITION_ON_SOCKET_SUBJECT = re.compile(
    r"\b(when|whenever|if|unless|once)\b\s+(?:a|an|the|no|any|some)?\s*"
    r"(?:(?:usable|working|real|live|docker|bind-mounted)\s+)*sockets?\b"
)
# Describing the probe is not claiming its outcome: "every invocation probes
# for a usable socket before deciding", "the script checks whether a socket
# is there". Narrow on purpose -- the probe verb must be followed by
# for/whether/if, so a bare "every run has a usable socket" is untouched.
_DETECTION_DESCRIPTION = re.compile(
    r"\b(probes?|probing|probed|checks?|checking|checked|tests?|testing"
    r"|looks?|looking|looked|detects?|detecting|decides?|deciding"
    r"|discovers?|searches|searching|inspects?)\b(?:\s+\w+){0,3}?\s*"
    r"\b(for|whether|if)\b"
)


def _header_comment_text(script_text: str) -> str:
    """The script's `#`-comment lines with their markers stripped, so the
    checks below cannot be defeated by rewrapping a sentence across lines."""
    return " ".join(
        line[1:] for line in script_text.splitlines() if line.startswith("#")
    )


def _part_claims_always_a_socket(part: str, socket_in_context: bool = False) -> bool:
    """Does this clause (or comma-part of one) assert that a usable docker
    socket is universally present here? ``socket_in_context`` says the
    preceding clause named a socket, so a pronoun here can carry it.

    Ambiguity is resolved toward flagging (a subordinate clause can put a
    universal and a socket in one sentence innocently); the guard's job is to
    reject the false claim, and the header's honest branch wording -- which
    hedges, denies or conditions -- is protected by the tables below."""
    if not _SOCKET_MENTION.search(part):
        if not (socket_in_context and _ANAPHOR.search(part)):
            return False
    if _DOUBLE_NEGATIVE_UNIVERSAL.search(part):
        # "never runs without a socket" == "always has a socket".
        return True
    if not _PRESENCE_WORD.search(part):
        return False
    if not _UNIVERSAL_QUANTIFIER.search(part):
        return False
    if _CONDITION_ON_DETECTION.search(part) or _CONDITION_ON_SOCKET_SUBJECT.search(part):
        # The socket's presence is the sentence's condition, not its claim.
        return False
    if _DETECTION_DESCRIPTION.search(part):
        # Describes the probe ("probes for a socket"), not its outcome.
        return False
    if _HEDGE_OR_NEGATION.search(part):
        # "not always", "some containers do", "may or may not" ... --
        # a qualification or denial, not an always-present assertion.
        return False
    return True


def _always_a_socket_claims(header_text: str) -> list[str]:
    """Every clause of ``header_text`` that claims a usable docker socket is
    always (or essentially always) present here. Empty list == no such
    claim."""
    normalized = " ".join(header_text.replace("*", "").split()).lower()
    offenders = []
    socket_in_context = False
    for clause in _CLAUSE_SPLIT.split(normalized):
        clause = clause.strip()
        if not clause:
            continue
        # The whole clause first (so "each run, without exception, includes a
        # usable socket" is caught), then each comma-part (so "the socket is
        # always present, so the tier is filtered out" is caught even though
        # the trailing part drags in unrelated words).
        parts = [clause]
        if "," in clause:
            parts.extend(p.strip() for p in clause.split(",") if p.strip())
        if any(
            _part_claims_always_a_socket(part, socket_in_context) for part in parts
        ):
            offenders.append(clause)
        socket_in_context = bool(_SOCKET_MENTION.search(clause))
    return offenders


# --- wording-independent detector for the mirror-image false claim: an
# UNCONDITIONAL, universal claim that a usable docker socket is *absent* ---
#
# The header genuinely branches (present -> filter with `-m "not docker"`;
# absent -> inject nothing, let the docker-marked modules self-skip), and
# both branches are true, conditional, per-invocation statements -- neither
# is a claim about every invocation. A rewrite that flattened either branch
# into a universal ("this environment always/never has a usable docker
# socket") would be just as false as the original always-present claim, so
# this side needs its own detector rather than a blanket string match that
# only happens to pass by phrasing luck (task 002, review finding 1).
_UNIVERSAL_ABSENCE_QUANTIFIER = re.compile(
    r"\b(never|at no time|not once|not ever|under no circumstances"
    r"|no matter when)\b"
)
_HARD_NEGATIONS = {
    "not", "never", "no", "nor", "none", "nothing", "cannot",
    "lacks", "lacking", "lacked", "absent", "missing", "unavailable",
}


def _part_is_conditioned_or_describes_the_probe(part: str) -> bool:
    return bool(
        _CONDITION_ON_DETECTION.search(part)
        or _CONDITION_ON_SOCKET_SUBJECT.search(part)
        or _DETECTION_DESCRIPTION.search(part)
    )


def _part_claims_never_a_socket(part: str, socket_in_context: bool = False) -> bool:
    """Does this clause (or comma-part) assert that a usable docker socket
    is UNCONDITIONALLY absent here -- the mirror image of
    ``_part_claims_always_a_socket`` above. Honest, branch-conditioned
    wording ("when no usable socket is present ...", "this container has
    none right now") must not trip this: the condition/probe-description
    exclusions and the requirement for an explicit universal (never/at no
    time/always+lacks/...) rather than a bare, unquantified "no" keep
    those passing."""
    if not _SOCKET_MENTION.search(part):
        if not (socket_in_context and _ANAPHOR.search(part)):
            return False
    if not _PRESENCE_WORD.search(part):
        return False
    if _UNIVERSAL_ABSENCE_QUANTIFIER.search(part):
        return not _part_is_conditioned_or_describes_the_probe(part)
    if _UNIVERSAL_QUANTIFIER.search(part):
        negation = _HEDGE_OR_NEGATION.search(part)
        if negation and negation.group(0) in _HARD_NEGATIONS:
            return not _part_is_conditioned_or_describes_the_probe(part)
    return False


def _always_absent_socket_claims(header_text: str) -> list[str]:
    """Every clause of ``header_text`` that claims a usable docker socket is
    unconditionally (always, never, at no time, ...) absent here. Empty
    list == no such claim. Sibling to ``_always_a_socket_claims``: together
    the two reject a header that flattens either branch of the genuine
    present/absent split into a universal."""
    normalized = " ".join(header_text.replace("*", "").split()).lower()
    offenders = []
    socket_in_context = False
    for clause in _CLAUSE_SPLIT.split(normalized):
        clause = clause.strip()
        if not clause:
            continue
        parts = [clause]
        if "," in clause:
            parts.extend(p.strip() for p in clause.split(",") if p.strip())
        if any(
            _part_claims_never_a_socket(part, socket_in_context) for part in parts
        ):
            offenders.append(clause)
        socket_in_context = bool(_SOCKET_MENTION.search(clause))
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
    # Validation attempt 2 of task 002 broke the clause-level detector with
    # these four: presence asserted by "includes"/"accompanies"/"count on
    # finding"/"supplies" and quantified by "each run"/"every
    # invocation"/"each invocation"/"without fail" -- families the earlier
    # word lists did not cover. They are the regression cases now.
    "Each run includes a usable Docker socket in this environment.",
    "A usable Docker socket accompanies every invocation in this environment.",
    "You can count on finding a usable Docker socket on each invocation here.",
    "Without fail, this environment supplies a usable Docker socket.",
    # Same claim, further afield: other presence verbs, other quantifiers,
    # and the double negatives that spell the universal backwards.
    "Every job container that runs this script exposes a working docker socket.",
    "A usable docker socket comes with each ralphd job container.",
    "This environment ships with a usable docker socket at all times.",
    "You get a usable docker socket 100% of the time here.",
    "A usable docker socket is available by default in this environment.",
    "The docker socket is mounted in on every single run of this script.",
    "Each and every invocation here has a usable docker socket attached.",
    "This script never runs without a usable docker socket.",
    "No ralphd job container lacks a usable docker socket.",
    "The docker socket is never missing in this environment.",
    "A usable socket is unfailingly reachable from this container.",
    "Each run, without exception, includes a usable docker socket.",
    "In every case a usable docker socket is bind-mounted into this container.",
    # The claim made across a clause boundary by a pronoun, and one whose
    # subject is a socket path (so a naive split on every "." would lose it).
    "The docker socket? It is there each and every time.",
    "The socket at /var/run/docker.sock is available in each job container.",
]

# Cross-product proof that the guard is claim-bound, not phrase-bound: every
# combination of a universal quantifier with a presence assertion about the
# socket is the same false claim, and all of them must be rejected.
ALWAYS_A_SOCKET_QUANTIFIERS = [
    "always",
    "invariably",
    "unconditionally",
    "without fail",
    "without exception",
    "at all times",
    "every time",
    "each time",
    "in every case",
    "100% of the time",
]
ALWAYS_A_SOCKET_TEMPLATES = [
    "In this environment there is {q} a usable docker socket.",
    "A usable docker socket is present here {q}.",
    "This environment provides a usable docker socket {q}.",
    "You get a usable docker socket here {q}.",
    "A usable docker socket is bind-mounted into this container {q}.",
    "This script {q} has a usable docker socket to talk to.",
    "{q}, a usable docker socket accompanies this script's invocation.",
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
    # Honest wording that names a universal *and* a socket in one breath but
    # denies or conditions the presence -- must stay unflagged too.
    "Not every ralphd job container has a usable docker socket.",
    "A usable docker socket is not guaranteed in this environment.",
    "Whenever no usable socket is found, the docker tier is left alone.",
    "Some invocations have a usable socket and some do not.",
    "This script may or may not find a usable docker socket at each"
    " invocation.",
    "Every invocation probes for a usable docker socket before deciding.",
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
    """F2 (#74): an earlier header claimed a usable docker socket is always
    present here, so the docker tier is always filtered out and self-skip
    is dismissed outright as a false premise -- itself false, since socket
    presence genuinely varies per invocation (some ralphd job containers
    have one, some -- including this one -- do not, and a bare developer
    host usually has none). The header's actual, current behaviour branches
    on that per-invocation probe: when a usable socket IS present it
    injects ``-m "not docker"`` to filter the tier out (never runs it
    nested); when the socket is ABSENT it injects nothing, and the
    docker-marked modules' own module-level ``skipif`` self-skips cleanly,
    the same as it would for a developer running the bare command by hand.
    The header must describe *both* branches, cite #74, and must not claim
    either branch -- present or absent -- as this environment's
    unconditional, universal behaviour."""
    text = SCRIPT.read_text()
    assert "#74" in text
    header = _header_comment_text(text)
    normalized = " ".join(header.split()).lower()
    # The header must describe both halves of the actual branch, not just
    # one of them.
    assert "not docker" in normalized, (
        "header must describe the socket-present branch (-m 'not docker')"
    )
    assert "self-skip" in normalized, (
        "header must describe the socket-absent branch (modules self-skip)"
    )
    # Neither branch may be asserted as this environment's unconditional,
    # universal behaviour -- in either direction. This replaces a former
    # blanket `"no docker socket" not in lowered` check, which only passed
    # by phrasing luck and would reject the header's truthful, conditioned
    # description of the absent branch just as readily as a false
    # universal-absence claim.
    presence_offenders = _always_a_socket_claims(header)
    assert presence_offenders == [], (
        "header must not claim a usable docker socket is always present"
        f" -- offending clauses: {presence_offenders}"
    )
    absence_offenders = _always_absent_socket_claims(header)
    assert absence_offenders == [], (
        "header must not claim a usable docker socket is always/never"
        f" absent -- offending clauses: {absence_offenders}"
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


@pytest.mark.parametrize("quantifier", ALWAYS_A_SOCKET_QUANTIFIERS)
@pytest.mark.parametrize("template", ALWAYS_A_SOCKET_TEMPLATES)
def test_the_always_a_socket_guard_rejects_every_quantifier_presence_combination(
    template, quantifier
):
    """F2 (#74): the false claim is "universal quantifier + socket presence",
    so the guard is exercised over the whole cross-product of both families
    rather than a hand-picked sentence list -- a detector that passes this
    cannot be satisfied by swapping in another synonym."""
    wording = template.format(q=quantifier)
    assert _always_a_socket_claims(wording) != [], (
        "the always-a-socket guard let a false claim through, so it is"
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
