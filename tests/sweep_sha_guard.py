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

# A pytest-style result summary line -- "N passed", "N failed", "N skipped",
# "N error"/"N errors" -- the signature every existing sweep record's
# verbatim-quoted summary line carries (e.g. "588 passed in 21.92s",
# "2227 passed, 1 skipped, 42 deselected in 164.09s").
PYTEST_SUMMARY_RE = re.compile(r"\b\d+\s+(?:passed|failed|skipped|errors?)\b")


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


def discharge_shas(text: str) -> list[str]:
    """Every sha this record explicitly retires (`discharge-sha` blocks)."""
    return _fenced_shas(text, "discharge-sha")


def is_wave_sweep_record(path: Path, text: str | None = None) -> bool:
    """This wave's own report, and a report *of a sweep* (names a pytest
    result line) -- the two things that jointly make declaring a
    `sweep-sha` block mandatory rather than optional."""
    if not path.name.startswith(WAVE_PREFIX):
        return False
    if text is None:
        text = path.read_text()
    return bool(PYTEST_SUMMARY_RE.search(text))


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


def _discharge_message(path: Path, sha: str, head: str) -> str:
    rel = path
    try:
        rel = path.relative_to(REPO_ROOT)
    except ValueError:
        pass
    return (
        f"{rel} declares sweep-sha {sha}, but HEAD is {head} and {sha} is "
        f"neither HEAD nor discharged -- re-take this sweep at {head} and "
        f"update the record, or, if the commits since {sha} are the "
        f"report/roadmap/index-only tail requirement B exists for, run "
        f"that subset (scripts/final_sha_guards.sh, or "
        f"tests/test_docs_consistency.py tests/test_docs_prd_index.py) at "
        f"{head} and add a `discharge-sha` block naming {sha}."
    )


def check_reports(paths: list[Path], head: str) -> list[str]:
    """Every declared sweep-sha must be current HEAD or discharged.

    Discharge is resolved transitively: a sha discharged by *any* report in
    `paths` clears every declaration of that same sha, and a discharged sha
    that is itself neither HEAD nor discharged is not re-flagged here (the
    discharge chain's own currency is a separate, human judgement call --
    see docs/sweep-records.md's "discharge rule" section -- not something
    this function re-derives).
    """
    texts = {p: p.read_text() for p in paths}
    discharged: set[str] = set()
    for text in texts.values():
        discharged.update(discharge_shas(text))

    problems: list[str] = []
    for path, text in texts.items():
        for sha in sweep_shas(text):
            if sha == head or sha in discharged:
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
