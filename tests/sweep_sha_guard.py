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

Task 005 (v0.14.1): the terminating fixed point for the final-sha rule.
Every sweep record names its *parent* sha (a commit cannot truthfully name
its own resulting sha -- see the `sweep-sha` docs below), so the commit that
ADDS a record always leaves that record's own declared sha exactly one
commit behind `HEAD` the instant it lands, with no possible `discharge-sha`
entry any commit could add yet, because the sha the discharge would need to
name (its own resulting HEAD) does not exist until after it is computed.
Demanding an explicit discharge for that gap would make the guard subset
permanently red the moment any record's own commit landed, for every record,
forever -- not a corner case, the *typical* case.

The mechanism chosen to close that gap is **parent-of-HEAD acceptance**: a
sha declaration is also accepted, with no `discharge-sha` block at all, when
`HEAD` is *exactly* the commit that ADDED the declaring record (git status
`A`, not `M`), that commit's diff touches the record's own path and nothing
else, and the declared sha is precisely that commit's single parent
(`_is_self_anchoring_add_commit`, used from `_reaches_head`/`check_reports`
alongside the discharge chain). This is narrow by construction: it names the
record's own path and only that path, it is refused outright the moment the
tip commit touches any other path, and it is refused for a second commit
stacked on top of the first (an edit to an already-existing record shows
git status `M`, not `A`, so the "added the record" test alone -- not the
resolver's cycle/visited-set logic used for a multi-hop discharge chain --
is what keeps this specific mechanism from ever iterating: it is checked
once, against literal `HEAD`, never walked). Report and docs commits are
never exempted as a *class* -- only the one commit that adds the one record
naming its own literal parent gets this, exactly as a hand-written
`discharge-sha` would have to be added for any other commit shape.

**The invariant this preserves:** the guard subset
(`scripts/final_sha_guards.sh`) ran green over every line of content that
ships, and a record-only commit -- one that does nothing but add a record
naming its own true parent sha -- can never change what that subset would
report if run again right now. Accepting such a commit's declaration
without a discharge block does not widen what counts as "the suite was
green at the sha the wave shipped" (`docs/sweep-records.md`'s own framing):
the commit contributed no `src/`/`tests/`/behaviour-affecting change of its
own, so the guard subset's own verdict is unchanged by its landing, and the
record's claim about its parent sha remains exactly as true after the
add-commit as before it.
"""

from __future__ import annotations

import re
import subprocess
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

# This run's launch sha -- the sha `docs/prds/README.md` records this run
# shipped from. The `archived-sha` form (below) is accepted for a record
# only when its declared sha is an ANCESTOR of this one: that is what makes
# "archived" mean something older than the current run, rather than a second
# unchecked way for a current-wave record to dodge the sweep-sha/
# discharge-sha chain.
#
# Task 003 (v0.14.1, #105/#106 backfill): this constant was still v0.14's
# OWN launch sha (`8fc9ee33d...`) after v0.14.1 had already launched from a
# later one -- a stale value inherited across the run boundary, not a fresh
# choice. It is updated here to v0.14.1's real launch sha
# (`7c962c6ea64933f664ab71b8eddcab18ae236cd1`, one commit past `f73e94f`,
# the tree `docs/prds/README.md`'s own v0.14.1 row and `notes.md` both name
# as this run's launch). The old value (`8fc9ee33d...`) is a genuine
# ancestor of the new one -- v0.14.1 shipped strictly after v0.14 -- so this
# widens which pre-launch shas the ancestor check accepts; it never narrows
# it, and no record that passed the check before stops passing now. This is
# the fix `v0.14-a2-closing-report.md` (a v0.14 record whose real sha is
# newer than v0.14's own LAUNCH_SHA but older than v0.14.1's) needed to take
# `archived-sha` at all without relaxing the ancestor check itself.
LAUNCH_SHA = "7c962c6ea64933f664ab71b8eddcab18ae236cd1"

SHA40_RE = re.compile(r"^[0-9a-f]{40}$")

# A fenced block tagged `sweep-sha`, `discharge-sha` or `archived-sha`, whose
# body is exactly one (sweep-sha/archived-sha) or two (discharge-sha)
# 40-hex-char shas per line (leading/trailing blank lines tolerated).
_FENCE_RE = re.compile(r"```(sweep-sha|discharge-sha|archived-sha)\s*\n(.*?)```", re.S)

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
    documented alternative to `sweep-sha`/`discharge-sha` for a record whose
    declared sha predates this run's launch sha (`LAUNCH_SHA`) and therefore
    has no chain to walk to current `HEAD` at all. See `check_reports` for
    the ancestor check that makes this form refuse a current-wave record.
    """
    return _fenced_shas(text, "archived-sha")


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


def _report_only_tail(sha: str, head: str, repo_root: Path) -> bool:
    """True when every path `git diff --name-only sha..head` names lives
    under `artifacts/reports/` -- the report/roadmap/index-only tail
    requirement B's discovered guard subset exists for. A commit that adds
    a `discharge-sha` block is itself exactly this shape: it can only name
    shas that already exist, so it always leaves HEAD one commit past the
    chain's own end, and a rule that demanded literal equality with HEAD
    could never be satisfied by any reachable repository state. False on
    any git failure (unknown/invalid sha, not a git repo, etc.) -- this
    allowance never masks a real problem, it only bridges a gap already
    known to be report-only.
    """
    if sha == head:
        return True
    try:
        out = subprocess.run(
            ["git", "diff", "--name-only", f"{sha}..{head}"],
            cwd=repo_root,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except OSError:
        return False
    if out.returncode != 0:
        return False
    changed = [p for p in out.stdout.splitlines() if p.strip()]
    if not changed:
        return True
    return all(p.startswith("artifacts/reports/") for p in changed)


def _reaches_head(
    sha: str,
    head: str,
    chain: dict[str, set[str]],
    repo_root: Path | None = None,
) -> bool:
    """Walk the discharge chain from `sha`, accepting when it ends at HEAD
    -- OR, for any sha reached by actually following at least one
    `discharge-sha` hop, when the remaining gap to HEAD is report-only.

    The bare declared `sha` never gets the report-only allowance on its own
    (that would let an undischarged stale sha pass just because nothing but
    reports happened to change since -- still a hard failure, per
    docs/sweep-records.md): the allowance only bridges the LAST leg of a
    chain that a real `discharge-sha` entry actually walked to. Cycles
    terminate as rejection, not recursion.
    """
    seen: set[str] = set()
    frontier: list[tuple[str, bool]] = [(sha, False)]
    while frontier:
        node, discharged = frontier.pop()
        if node == head:
            return True
        if discharged and repo_root is not None and _report_only_tail(node, head, repo_root):
            return True
        if node in seen:
            continue
        seen.add(node)
        frontier.extend((nxt, True) for nxt in chain.get(node, ()))
    return False


def _rel_to(path: Path, root: Path) -> str:
    try:
        return str(path.relative_to(root))
    except ValueError:
        return str(path)


def _is_self_anchoring_add_commit(
    path: Path, sha: str, head: str, repo_root: Path
) -> bool:
    """True when `head` is the git commit that ADDED `path` to the repo,
    `sha` is exactly that commit's single parent, and the commit's diff
    touches `path` and nothing else -- the terminating fixed point for a
    record's own sweep-sha (see the module docstring: "parent-of-HEAD
    acceptance"). Narrow by construction:

    * it names `path` itself and only `path` -- a diff that touches any
      other file (another record, a roadmap block, anything) is refused;
    * it fires only for the commit that ADDS the file (git diff status
      `A`), never one that edits an already-existing record -- a second
      record-only commit stacked on top of the first is an *edit*, so it
      shows status `M` and this refuses it; the ordinary discharge-sha
      chain is required for that case, exactly as for any other commit
      shape.

    False (never an exemption) on any git failure -- no parent (a root
    commit), more than one parent (a merge commit), or any other git error.
    """
    rel = _rel_to(path, repo_root)
    try:
        parents = subprocess.run(
            ["git", "rev-list", "--parents", "-n", "1", head],
            cwd=repo_root,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except OSError:
        return False
    if parents.returncode != 0:
        return False
    fields = parents.stdout.split()
    if len(fields) != 2:
        # fields[0] is `head` itself; anything but exactly one parent (a
        # root commit with zero, or a merge commit with two or more) is not
        # the single-parent shape this mechanism is anchored to.
        return False
    parent_sha = fields[1]
    if parent_sha != sha:
        return False
    try:
        diff = subprocess.run(
            ["git", "diff", "--name-status", parent_sha, head],
            cwd=repo_root,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except OSError:
        return False
    if diff.returncode != 0:
        return False
    changes = [line for line in diff.stdout.splitlines() if line.strip()]
    if len(changes) != 1:
        return False
    status, _, changed_path = changes[0].partition("\t")
    return status == "A" and changed_path == rel


def _is_ancestor(sha: str, descendant: str, repo_root: Path) -> bool:
    """True when `sha` is `descendant` itself or a genuine git ancestor of
    it (`git merge-base --is-ancestor`). False on any git failure (unknown
    sha, not a git repo, etc.) -- an archived-sha declaration this cannot
    positively confirm is treated as unproven, never as accepted by default.
    """
    if sha == descendant:
        return True
    try:
        out = subprocess.run(
            ["git", "merge-base", "--is-ancestor", sha, descendant],
            cwd=repo_root,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except OSError:
        return False
    return out.returncode == 0


def _archived_message(path: Path, sha: str) -> str:
    rel = _rel(path)
    return (
        f"{rel} declares archived-sha {sha}, but {sha} is not an ancestor "
        f"of this run's launch sha {LAUNCH_SHA} -- the archived-sha form is "
        f"reserved for a sha that predates this run entirely (see "
        f"docs/sweep-records.md). A record produced during this run cannot "
        f"label itself archived to dodge the sweep-sha/discharge-sha chain: "
        f"declare it as sweep-sha instead and either match HEAD or carry a "
        f"discharge chain that reaches it."
    )


def check_reports(
    paths: list[Path], head: str, repo_root: Path = REPO_ROOT
) -> list[str]:
    """Every declared sweep-sha must be current HEAD, discharged AT HEAD, or
    discharged at a sha whose remaining gap to HEAD is report-only. Every
    declared archived-sha must be an ancestor of `LAUNCH_SHA` (this run's
    own launch sha) -- the archived form's whole point is that it names a
    sha that predates the run, so it never needs a chain to HEAD at all;
    a sha that is NOT such an ancestor is refused outright, because that is
    precisely a current-wave record trying to label itself archived instead
    of carrying a real sweep-sha/discharge-sha chain.

    Discharge (for sweep-sha) is resolved transitively and is *anchored*:
    each `discharge-sha` line names both the stale sha it retires and the
    sha the discharge check was run at. That chain has to terminate at
    current `HEAD` -- OR at a sha for which `git diff --name-only sha..HEAD`
    (run in `repo_root`) touches only `artifacts/reports/**` -- to be
    accepted; a chain that reaches neither (and a discharge block that names
    no verified-at sha at all) leaves the stale declaration flagged. The
    report-only allowance is why this is satisfiable at all: the commit
    that ADDS a `discharge-sha` block can only name shas that already
    exist, so it always leaves `HEAD` one commit past the chain's own end,
    and that one commit is itself report-only.
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
            if _reaches_head(sha, head, chain, repo_root=repo_root):
                continue
            if _is_self_anchoring_add_commit(path, sha, head, repo_root):
                continue
            problems.append(_discharge_message(path, sha, head))
        for sha in archived_shas(text):
            if _is_ancestor(sha, LAUNCH_SHA, repo_root):
                continue
            problems.append(_archived_message(path, sha))
    return problems


def sha_missing_problems(paths: list[Path]) -> list[str]:
    """Every report that names a pytest result line owes a sha declaration
    -- `sweep-sha` (checked against HEAD/discharge by `check_reports`) or
    `archived-sha` (checked against `LAUNCH_SHA` ancestry, same place).
    Task 008 (#103, requirement A) removed the earlier `WAVE_PREFIX`
    filename gate that used to exempt every report not named `v0.14-*`; a
    record with a pytest result line and no declaration at all is now a
    hard failure regardless of its filename.
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
            f"sha the sweep ran at, or, if that sha predates this run's "
            f"launch sha {LAUNCH_SHA}, an `archived-sha` block naming it "
            f"(see docs/sweep-records.md)."
        )
    return problems


# Historical alias, kept for the same reason as `is_wave_sweep_record`
# above -- a real `def`, not a bare assignment, in case a future report ever
# cites this node id too.
def wave_sha_missing_problems(paths: list[Path]) -> list[str]:
    return sha_missing_problems(paths)
