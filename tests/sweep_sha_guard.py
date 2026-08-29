"""Shared parsing/checking for sweep records' machine-readable sha claim.

Task 002 (#103, requirement A): a sweep record in `artifacts/reports/` used
to state the sha it ran at only in prose, so nobody re-checked the sentence
once the wave's own tail commits (roadmap block, PRD index row, closing
report) moved `HEAD` past it -- the defect that cost the first approach of
every wave since v0.11. `docs/sweep-records.md` documents the two fenced
block forms this module reads (`sweep-sha`, `discharge-sha`) and the
discharge rule; this module implements the parser and the two problem-list
functions `tests/test_sweep_sha_guard.py` applies to every report the
directory holds, the same way `tests/report_claims.py` shares its checks.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
REPORTS_DIR = REPO_ROOT / "artifacts" / "reports"

# This wave's own filename prefix for new reports (already used by task 001's
# artifacts/reports/v0.14-operator-notifications.md).
WAVE_PREFIX = "v0.14-"

SHA40_RE = re.compile(r"^[0-9a-f]{40}$")

# A fenced block tagged `sweep-sha` or `discharge-sha`, whose body is exactly
# one 40-hex-char sha per line (leading/trailing blank lines tolerated).
_FENCE_RE = re.compile(r"```(sweep-sha|discharge-sha)\s*\n(.*?)```", re.S)

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
_OUTCOME = r"(?:passed|failed|skipped|errors?|deselected|warnings?|xfailed|xpassed)"
PYTEST_SUMMARY_RE = re.compile(
    r"^[ \t=]*"
    rf"\d+ +{_OUTCOME}"
    rf"(?:, *\d+ +{_OUTCOME})*"
    r" +in +\d+(?:\.\d+)?s"
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


def discharge_entries(text: str) -> list[tuple[str, str]]:
    """Every well-formed discharge this record states.

    A `discharge-sha` block's every line is a PAIR of 40-hex shas: the stale
    sha being retired, then the sha the discharge check was actually run at
    (`git rev-parse HEAD` at the moment of examination). The pair is what
    makes the documented chain checkable -- see `check_reports`.
    """
    entries: list[tuple[str, str]] = []
    for retired, verified_at in _discharge_lines(text):
        entries.append((retired, verified_at))
    return entries


def _discharge_lines(text: str) -> list[tuple[str, str]]:
    pairs: list[tuple[str, str]] = []
    for line in _raw_discharge_lines(text):
        fields = line.split()
        if len(fields) == 2 and all(SHA40_RE.match(f) for f in fields):
            pairs.append((fields[0], fields[1]))
    return pairs


def _raw_discharge_lines(text: str) -> list[str]:
    lines: list[str] = []
    for match in _FENCE_RE.finditer(text):
        if match.group(1) != "discharge-sha":
            continue
        for line in match.group(2).splitlines():
            line = line.strip()
            if line:
                lines.append(line)
    return lines


def malformed_discharge_lines(text: str) -> list[str]:
    """Discharge lines that are NOT a `<retired-sha> <verified-at-sha>` pair.

    A one-sha line -- the form a reader might guess -- is rejected rather than
    honoured: without the sha the discharge was verified at there is nothing
    to anchor the documented chain to `HEAD`, so accepting it would be a
    silent bypass of the whole rule.
    """
    well_formed = {" ".join(pair) for pair in _discharge_lines(text)}
    return [
        line
        for line in _raw_discharge_lines(text)
        if " ".join(line.split()) not in well_formed
    ]


def is_wave_sweep_record(path: Path, text: str | None = None) -> bool:
    """This wave's own report, and a report *of a sweep* (names a pytest
    result line) -- the two things that jointly make declaring a
    `sweep-sha` block mandatory rather than optional."""
    if not path.name.startswith(WAVE_PREFIX):
        return False
    if text is None:
        text = path.read_text()
    return contains_pytest_result_line(text)


def current_head(cwd: Path = REPO_ROOT) -> str:
    out = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert out.returncode == 0, out.stderr
    return out.stdout.strip()


def _rel(path: Path) -> Path:
    try:
        return path.relative_to(REPO_ROOT)
    except ValueError:
        return path


def _malformed_message(path: Path, line: str) -> str:
    return (
        f"{_rel(path)} has a malformed discharge-sha line {line!r}: every "
        f"line in a `discharge-sha` block must be exactly two 40-hex shas -- "
        f"the stale sha being retired, then the sha the discharge check was "
        f"run at (see docs/sweep-records.md). A one-sha line is rejected: "
        f"with no verified-at sha the discharge chain cannot be shown to "
        f"reach HEAD, which is the only thing that makes a discharge more "
        f"than an assertion."
    )


def _discharge_message(path: Path, sha: str, head: str) -> str:
    rel = _rel(path)
    return (
        f"{rel} declares sweep-sha {sha}, but HEAD is {head} and {sha} is "
        f"neither HEAD nor discharged -- re-take this sweep at {head} and "
        f"update the record, or, if the commits since {sha} are the "
        f"report/roadmap/index-only tail requirement B exists for, run "
        f"that subset (scripts/final_sha_guards.sh, which discovers its own "
        f"modules by grepping tests/) at "
        f"{head} and add a `discharge-sha` block whose line is `{sha} "
        f"{head}` (the stale sha, then the sha you verified at). A discharge "
        f"chain that never reaches HEAD does not clear this."
    )


def _reaches_head(sha: str, head: str, chain: dict[str, set[str]]) -> bool:
    """Walk the discharge chain from `sha`, accepting only if it ends at HEAD.

    Implements docs/sweep-records.md's discharge rule literally: "a chain of
    stale shas each pointing at a `discharge-sha` entry, ending at a sha that
    equals current HEAD, is accepted; a chain that never reaches HEAD is
    not." Cycles terminate as rejection, not recursion.
    """
    seen: set[str] = set()
    frontier = [sha]
    while frontier:
        node = frontier.pop()
        if node == head:
            return True
        if node in seen:
            continue
        seen.add(node)
        frontier.extend(chain.get(node, ()))
    return False


def check_reports(paths: list[Path], head: str) -> list[str]:
    """Every declared sweep-sha must be current HEAD or discharged AT HEAD.

    Discharge is resolved transitively and is *anchored*: each
    `discharge-sha` line names both the stale sha it retires and the sha the
    discharge check was run at, so the chain of retirements has to terminate
    at current `HEAD` to be accepted. A chain that never reaches `HEAD` (and
    a discharge block that names no verified-at sha at all) leaves the stale
    declaration flagged.
    """
    texts = {p: p.read_text() for p in paths}
    chain: dict[str, set[str]] = {}
    problems: list[str] = []
    for path, text in texts.items():
        for line in malformed_discharge_lines(text):
            problems.append(_malformed_message(path, line))
        for retired, verified_at in discharge_entries(text):
            chain.setdefault(retired, set()).add(verified_at)

    for path, text in texts.items():
        for sha in sweep_shas(text):
            if _reaches_head(sha, head, chain):
                continue
            problems.append(_discharge_message(path, sha, head))
    return problems


def wave_sha_missing_problems(paths: list[Path]) -> list[str]:
    """Companion assertion: every sweep record THIS WAVE writes declares the
    sweep-sha form. Pre-existing (pre-v0.14) records are out of scope by
    construction (they don't match WAVE_PREFIX)."""
    problems: list[str] = []
    for path in paths:
        text = path.read_text()
        if is_wave_sweep_record(path, text) and not sweep_shas(text):
            rel = path.relative_to(REPO_ROOT) if path.is_relative_to(REPO_ROOT) else path
            problems.append(
                f"{rel} is a v0.14 sweep record (name starts with "
                f"'{WAVE_PREFIX}', names a pytest result line) but declares "
                f"no sweep-sha block -- add one naming the sha the sweep "
                f"ran at (see docs/sweep-records.md)."
            )
    return problems
