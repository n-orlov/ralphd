"""Shared parsing/checking for a sweep record's declared sha -- provenance
only, never currency.

Task 002 (#103, requirement A): a sweep record in `artifacts/reports/` used
to state the sha it ran at only in prose, so nobody re-checked the sentence
once the wave's own tail commits (roadmap block, PRD index row, closing
report) moved `HEAD` past it. `docs/sweep-records.md` documents the fenced
block form this module reads (`sweep-sha`, or `archived-sha` for a
pre-existing record); this module implements the parser and the two
problem-list functions `tests/test_sweep_sha_guard.py` applies to every
report the directory holds, the same way `tests/report_claims.py` shares its
checks.

Task 053 (v0.20, #113 step 4) retires everything this module used to do
*beyond* that: a HEAD-relative currency check (`check_reports`), a
hand-maintained discharge chain (`discharge_entries` and friends), the
self-anchoring/fixed-point acceptance walk
(`_self_anchoring_reaches_head`/`_is_self_anchoring_add_commit`) and the
per-run `LAUNCH_SHA` constant it and `archived-sha` both leaned on. None of
that machinery can outlive the defect it was built to chase: a GitHub check
run is bound to its own commit sha by GitHub itself and cannot go stale as
`HEAD` moves, so `GET /repos/.../commits/<sha>/check-runs` is now the
attestation review reads, and re-deriving "was this record's sha still good
as of the wave's own last commit" in this repo's own test suite is exactly
the self-authored ledger #113 exists to stop trusting. Every test node that
existed only to exercise that retired machinery is deleted in the same
commit (`tests/test_sweep_sha_guard.py`), and every historical report that
cited one of the now-gone functions or tests by node id is updated in the
same commit to mark that citation `!retired` (`tests/report_claims.py`'s
strong direction for a citation that must no longer resolve) -- the sha each
of those records names is untouched; only the drift-chasing citation is
retired.

What survives is the one lint that still earns its keep: **every report
under `artifacts/reports/` that shows a genuine pytest result line names the
sha it was taken at**, via a `sweep-sha` or `archived-sha` fenced block
(`sha_missing_problems`, and `wave_sha_missing_problems` as an established
alias -- see below). Neither block is checked against `HEAD`, a discharge
chain or `LAUNCH_SHA` any more; declaring *some* 40-hex sha is the whole
requirement. The two tag names are kept distinct only because hundreds of
existing records already use one or the other and their fenced blocks are
provenance, never rewritten (see docs/sweep-records.md) -- not because the
guard treats them differently; `sweep_shas()` and `archived_shas()` are
equally sufficient to satisfy the missing-declaration check.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
REPORTS_DIR = REPO_ROOT / "artifacts" / "reports"

# This wave's own filename prefix. Historically this gated which records
# owed a sha declaration at all (only files named with this prefix did);
# task 008 (#103, requirement A) removed that gate -- every report under
# artifacts/reports/ that contains a pytest result line owes a declaration
# now, this wave's own or not (see docs/sweep-records.md). The constant
# survives only as a label some historical records/messages still refer to.
WAVE_PREFIX = "v0.14-"

SHA40_RE = re.compile(r"^[0-9a-f]{40}$")

# A fenced block tagged `sweep-sha` or `archived-sha`, whose body is exactly
# one 40-hex-char sha per line (leading/trailing blank lines tolerated).
# `discharge-sha` blocks are no longer read at all (task 053, #113 step 4):
# the discharge chain they fed is retired outright, and existing records'
# `discharge-sha` blocks stand as untouched historical provenance rather
# than input to any check.
_FENCE_RE = re.compile(r"```(sweep-sha|archived-sha)\s*\n(.*?)```", re.S)

# A genuine pytest result summary line: a line whose WHOLE content is one or
# more comma-separated "N <outcome>" clauses followed by pytest's own
# trailing duration ("in <n>s"), optionally wrapped in the `=` padding
# pytest's non-quiet mode prints (e.g. "588 passed in 21.92s",
# "2227 passed, 1 skipped, 42 deselected in 164.09s",
# "==== 1 failed, 1 passed in 0.07s ====").
#
# Rule history, both steps recorded because each one was a real defect:
#
# 1. The original rule (task 002, #103) was `\b\d+\s+(?:passed|failed|
#    skipped|errors?)\b` -- any "N passed"/"N failed"/etc token ANYWHERE in
#    the text. That matched ordinary English sentences containing a number
#    next to one of those words -- "Attempt 1 failed validation because ..."
#    and "cannot relabel 016 failed->skipped itself" in
#    artifacts/reports/v0.14-operator-notifications.md -- misclassifying a
#    plain notification log as a sweep record owing a `sweep-sha` block.
# 2. Requiring the trailing "in <n>s" duration killed those two sentences
#    but was still an UNANCHORED search, so prose that quotes a real summary
#    inline -- "English says 4302 passed, 1 skipped in 856.12s yesterday."
#    -- still counted as a result line, and the detector could only ever
#    report that some fragment of a line looked like a summary.
#
# The rule below is stronger than both: the counts-plus-duration shape must
# be the entire line, so a match is a claim about a whole line of the report
# rather than about a substring of a sentence. Prose that mentions counts
# always has words around them, so it no longer matches, while every real
# pytest summary line -- alone on its line, indented inside a fenced block,
# or `=`-padded -- still does. Use `is_pytest_result_line` /
# `contains_pytest_result_line` rather than the regex directly. See
# docs/sweep-records.md's "companion rule" paragraph.
#
# Task 002 (#105, requirement A): pytest's own `-q` summary line grows a
# parenthesized `(H:MM:SS)` elapsed suffix once a run passes roughly 60s of
# wall time -- e.g. `4198 passed, 1 skipped, 62 deselected in 852.84s
# (0:14:12)`. The rule above, with no allowance for that suffix, rejected
# every one of those lines outright, which in practice meant it rejected
# every REAL sweep this project runs (the fast tier alone takes ~850s) while
# still matching every short, synthetic one -- the matcher saw fewer real
# records, never more. The trailing group below is OPTIONAL and adds
# nothing a real line didn't already have, so widening it can only make the
# matcher see MORE genuine summary lines, never fewer, and it changes
# nothing about the anchoring or the prose-rejection behaviour above.
_OUTCOME = r"(?:passed|failed|skipped|errors?|deselected|warnings?|xfailed|xpassed)"
PYTEST_SUMMARY_RE = re.compile(
    r"^[ \t=]*"
    rf"\d+ +{_OUTCOME}"
    rf"(?:, *\d+ +{_OUTCOME})*"
    r" +in +\d+(?:\.\d+)?s"
    r"(?: +\(\d+:\d{2}:\d{2}\))?"
    r"[ \t=]*$",
    re.MULTILINE,
)


def is_pytest_result_line(line: str) -> bool:
    """True only when the WHOLE line is a pytest result summary line.

    Anchored on purpose: a sentence that quotes counts inline ("the fast
    tier reported 4302 passed, 1 skipped in 856.12s yesterday") is prose
    about a sweep, not the sweep's own result line, and must not make the
    file holding it a sweep record.
    """
    return bool(PYTEST_SUMMARY_RE.fullmatch(line.strip("\r\n")))


def contains_pytest_result_line(text: str) -> bool:
    """True when any single line of `text` is a pytest result summary line."""
    return any(is_pytest_result_line(line) for line in text.splitlines())


def report_files() -> list[Path]:
    """Every report under artifacts/reports/, discovered -- never hand-listed."""
    return sorted(REPORTS_DIR.glob("*.md"))


def _fenced_shas(text: str, tag: str) -> list[str]:
    shas: list[str] = []
    for match in _FENCE_RE.finditer(text):
        if match.group(1) != tag:
            continue
        for line in match.group(2).splitlines():
            line = line.strip()
            if line and SHA40_RE.match(line):
                shas.append(line)
    return shas


def sweep_shas(text: str) -> list[str]:
    """Every sha this record declares it ran at (`sweep-sha` blocks)."""
    return _fenced_shas(text, "sweep-sha")


def archived_shas(text: str) -> list[str]:
    """Every sha this record declares via the `archived-sha` form -- the
    documented alternative to `sweep-sha` for a record produced before this
    guard existed or before the sha-declaration requirement reached it.
    Provenance only: neither form is checked against `HEAD`, a discharge
    chain, or a run's own launch sha any more (task 053, #113 step 4).
    """
    return _fenced_shas(text, "archived-sha")


def is_sweep_record(path: Path, text: str | None = None) -> bool:
    """A report *of a sweep* -- one that names a pytest result line -- and
    therefore owes a sha declaration (`sweep-sha` or `archived-sha`).

    Task 008 (#103, requirement A) removed the earlier `WAVE_PREFIX`
    filename gate: every report under `artifacts/reports/` that names a
    pytest result line owes a declaration now, this wave's own or not.
    `path` is accepted (and still named first) for call-site symmetry with
    the historical `is_wave_sweep_record` name and because callers already
    pass it; only `text` is examined.
    """
    if text is None:
        text = path.read_text()
    return contains_pytest_result_line(text)


# Historical alias: every real report and test in this repo has moved to
# `is_sweep_record`, but a v0.14-era report still cites
# `tests/sweep_sha_guard.py::is_wave_sweep_record` by node id
# (`tests/report_claims.py::node_problems` requires that exact `def` to
# exist in this file for that citation to keep resolving) -- kept as a real
# function, not a bare assignment, so that citation is never a stale one.
def is_wave_sweep_record(path: Path, text: str | None = None) -> bool:
    return is_sweep_record(path, text)


def sha_missing_problems(paths: list[Path]) -> list[str]:
    """Every report that names a pytest result line owes a sha declaration
    -- `sweep-sha` or `archived-sha`, either one, with no further check of
    what it names. Task 008 (#103, requirement A) removed the earlier
    `WAVE_PREFIX` filename gate that used to exempt every report not named
    `v0.14-*`; a record with a pytest result line and no declaration at all
    is a hard failure regardless of its filename. Task 053 (#113 step 4)
    removed the currency/discharge/self-anchoring machinery that used to
    check what a declared sha is worth once it exists -- this is now the
    ONLY check this module makes.
    """
    problems: list[str] = []
    for path in paths:
        text = path.read_text()
        if not is_sweep_record(path, text):
            continue
        if sweep_shas(text) or archived_shas(text):
            continue
        rel = path.relative_to(REPO_ROOT) if path.is_relative_to(REPO_ROOT) else path
        problems.append(
            f"{rel} names a pytest result line but declares no sweep-sha "
            f"or archived-sha block -- add a `sweep-sha` block naming the "
            f"sha the sweep ran at, or an `archived-sha` block if this "
            f"record predates the guard (see docs/sweep-records.md)."
        )
    return problems


# Historical alias, kept for the same reason as `is_wave_sweep_record`
# above -- a real `def`, not a bare assignment, in case a future report ever
# cites this node id too.
def wave_sha_missing_problems(paths: list[Path]) -> list[str]:
    return sha_missing_problems(paths)
