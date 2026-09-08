"""`tests/report_claims.py`'s in-process object reader, checked against real
git (task 035, requirement E.4).

`commit_problems()` answers "is this backticked hex token a commit object in
this repo" without spawning anything, because requirement E.4 pins the
records family (`tests/records/`) to exactly one subprocess for a whole run
-- a single `git ls-files` -- and a sha claim is not a membership question
that snapshot can answer. The reader that replaced the old
`git cat-file --batch-check` call parses `.git` itself: loose objects' zlib
type header, packed objects' pack-entry type bits (v2 `.idx` fanout/sha/
offset tables, following an OFS_DELTA/REF_DELTA chain to its base, since a
delta never changes the object type).

That is real plumbing, so it is checked the only way worth checking: against
git's own answers, for every sha this repo's reports actually cite plus a
broad sample of the object store, including deliberately ambiguous prefixes
and shas that resolve to nothing. This module lives OUTSIDE `tests/records/`
precisely because the comparison has to spawn the very `git cat-file`
subprocess the family is pinned not to spawn -- keeping it here means the
reader stays honest without the family paying for it.
"""

from __future__ import annotations

import collections
import random
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "tests"))

import report_claims as report_claims_module
from report_claims import parse_claims, report_files


def _git_answers(tokens: list[str]) -> list[str]:
    """`git cat-file --batch-check`'s own kind word for each token: the type
    for a resolved object, `missing`, or `ambiguous`."""
    proc = subprocess.run(
        ["git", "cat-file", "--batch-check"],
        cwd=REPO_ROOT,
        input="\n".join(tokens) + "\n",
        capture_output=True,
        text=True,
        check=True,
    )
    lines = proc.stdout.splitlines()
    assert len(lines) == len(tokens), (
        f"git answered {len(lines)} of {len(tokens)} asked tokens")
    answers = []
    for line in lines:
        fields = line.split(" ")
        answers.append(fields[1] if len(fields) > 1 else fields[0])
    return answers


def _mismatches(tokens: list[str]) -> list[str]:
    database = report_claims_module._objects()
    wrong = []
    for token, expected in zip(tokens, _git_answers(tokens), strict=True):
        got = database.kind(token)
        if got != expected:
            wrong.append(f"{token}: git says {expected!r}, reader says {got!r}")
    return wrong


def test_every_sha_the_reports_cite_gets_gits_own_answer() -> None:
    """The population that actually matters: the shas `commit_problems()` is
    asked about on a real run of the records family."""
    cited = sorted({
        claim.value
        for report in report_files()
        for claim in parse_claims(report).shas
    })
    assert len(cited) > 100, (
        f"only {len(cited)} sha claims found under artifacts/reports/ -- the "
        f"parser or the reports moved, this check is no longer meaningful")
    wrong = _mismatches(cited)
    assert not wrong, "in-process object reader disagrees with git: " + "; ".join(
        wrong[:10])


def test_a_broad_sample_of_the_object_store_gets_gits_own_answer() -> None:
    """Full shas and 7-char abbreviations drawn from the store itself, so
    trees, blobs and delta-encoded entries are covered too -- not just the
    commits the reports happen to cite."""
    database = report_claims_module._objects()
    every_sha = database._all
    assert len(every_sha) > 1000, (
        f"object store scan found only {len(every_sha)} objects")
    picker = random.Random(20260908)
    full = picker.sample(every_sha, 400)
    abbreviated = [sha[:7] for sha in picker.sample(every_sha, 200)]
    wrong = _mismatches(full + abbreviated)
    assert not wrong, "in-process object reader disagrees with git: " + "; ".join(
        wrong[:10])
    kinds = collections.Counter(_git_answers(full))
    assert kinds["commit"] and kinds["tree"] and kinds["blob"], (
        f"sample did not cover all three ordinary object types: {dict(kinds)}")


def test_missing_and_ambiguous_prefixes_get_gits_own_answer() -> None:
    """The two non-type answers `commit_problems()` reports verbatim: a
    prefix nothing matches is `missing`, one several objects match is
    `ambiguous`."""
    database = report_claims_module._objects()
    shared = collections.Counter(sha[:4] for sha in database._all)
    ambiguous = [prefix for prefix, seen in sorted(shared.items()) if seen > 1][:5]
    assert ambiguous, "no 4-char prefix in this store is shared, cannot check"
    absent = ["0" * 40, "f" * 40, "deadbee"]
    tokens = ambiguous + absent
    wrong = _mismatches(tokens)
    assert not wrong, "in-process object reader disagrees with git: " + "; ".join(
        wrong[:10])
    assert [database.kind(prefix) for prefix in ambiguous] == [
        "ambiguous"] * len(ambiguous)
    assert [database.kind(token) for token in absent] == ["missing"] * len(absent)


def test_report_claims_spawns_git_for_nothing_but_ls_files() -> None:
    """The rule this reader exists to make true, asserted against the
    implementation module's own source: `tests/report_claims.py` runs exactly
    one kind of command, `git ls-files`."""
    source = (REPO_ROOT / "tests" / "report_claims.py").read_text()
    spawn_sites = [
        line.strip() for line in source.splitlines()
        if "subprocess.run(" in line
    ]
    assert len(spawn_sites) == 1, (
        f"tests/report_claims.py has {len(spawn_sites)} subprocess.run call "
        f"sites, expected exactly one (git ls-files): {spawn_sites}")
    assert '"git", "ls-files"' in source, (
        "the one subprocess tests/report_claims.py may spawn is git ls-files")
    assert '"cat-file"' not in source, (
        "no git cat-file argv may return to tests/report_claims.py -- "
        "requirement E.4 pins the records family to one git ls-files "
        "(prose about the call it replaced is fine, an argv is not)")
