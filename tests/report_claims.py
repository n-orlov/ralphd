"""Shared re-reading of the evidence reports under `artifacts/reports/`.

Task 041 (#22): the sha/path/test-node checks that task 058 wrote for
`artifacts/reports/issue-traceability.md` are the ones every report in that
directory needs -- a report that names a commit that never landed, a file that
has been renamed away, or a test that no longer exists is worse than no report.
So the extraction and the checks live here once and
`tests/records/test_report_claims.py` applies them to every report the directory
holds (discovered by glob, so a new report is covered the day it lands);
`tests/test_issue_traceability.py` keeps only what is specific to its report
and re-uses this parser for its density assertions.

The claim vocabulary the reports use, and how it is read:

* A backticked 7-40 char hex token is a commit sha, and must resolve to a
  commit object in this repo.
* A backticked path under one of `REPO_DIRS` is a repo-relative path and must
  be tracked by git (task 012, #86) -- one `git ls-files` snapshot per
  process, not a subprocess per claim. Existence on disk is not membership:
  a directory the browser tier creates but never commits
  (`artifacts/screenshots/hub/`) used to pass this check just by being
  there; it must fail now, with a message that says *why* it fails --
  "generated, not committed" (still present but untracked) versus "deleted
  or renamed" (tracked once and gone now, or never named at all) are kept
  distinguishable so the fix is obvious from the message alone. Anything
  else in backticks is prose: `ci/Dockerfile` in the sibling-toolchain
  recipe belongs to a *target* repo, and a run-dir artifact is written
  `<run-dir>/artifacts/...` precisely so this parser does not read it as a
  claim about the checkout -- and stays that way, since `PATH_RE` only ever
  matches a path that starts with one of `REPO_DIRS`, never a leading `/`.
* `path::node` names a test; a following ``::node`` or ``\u2026::node`` (the
  report style for "another test in the same file") continues the most
  recent path that was *itself* named with a node id, across lines, since the
  reports continue one test file down the rows of a table while naming
  unrelated files (`container/Dockerfile`) in the other column. A bare path
  under `tests/` named with *no* node, in running prose rather than a table
  row, continues it too -- prose that moves on to a new test file usually
  names it before the node ids that belong to it -- but a table row's own
  bare mention of an unrelated file never does, since the next row's node
  still has to continue the table's own test-file column.
* A trailing `*` (`::test_retry_proxy_*`, standing for a family of tests) is
  a prefix: at least one test in the file must match it.
* A trailing `!retired` on any of the three forms above marks a
  *deliberately retired* citation -- a report is allowed to keep naming a
  path or test that a later task removed, as long as it says so, and the
  check runs in the STRONG direction: `` `tests/foo.py!retired` `` fails if
  `tests/foo.py` still exists on disk (it was not actually retired, so the
  marker is a lie), and `` `tests/foo.py::test_bar!retired` `` fails if
  `tests/foo.py` still exists AND still defines `test_bar` (the node was not
  actually retired). A `!retired` node whose file is gone entirely trivially
  passes -- it cannot collect, which is exactly the point. The marker
  attaches to one citation at a time; a continuation `` `::test_baz!retired`
  `` needs its own suffix even when the path it continues was itself marked
  retired.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
REPORTS_DIR = REPO_ROOT / "artifacts" / "reports"

# Top-level directories this repo owns. A path claim outside them is prose
# about somebody else's tree (see the module docstring).
REPO_DIRS = ("tests", "src", "docs", "examples", "artifacts", "tools", "container")

TOKEN_RE = re.compile(r"`[^`\n]+`")
SHA_RE = re.compile(r"^`([0-9a-f]{7,40})`$")
PATH_RE = re.compile(
    rf"^`((?:{'|'.join(REPO_DIRS)})/[\w./-]+?)(::[\w:*]+)?(!retired)?`$"
)
# `::test_b` / `\u2026::test_b` -- a node id continuing the last named path.
NODE_ONLY_RE = re.compile(r"^`(?:\u2026|\.\.\.)?(::[\w:*]+)(!retired)?`$")


@dataclass(frozen=True)
class Claim:
    """One backticked claim, with where it was made (for the failure text)."""

    value: str
    line: int


@dataclass(frozen=True)
class NodeClaim:
    path: str | None
    node: str
    line: int


@dataclass(frozen=True)
class RetiredClaim:
    """A `!retired`-marked citation: `path` is always set; `node` is set only
    for the `path::node!retired` / `::node!retired` forms."""

    path: str | None
    node: str | None
    line: int


@dataclass(frozen=True)
class ReportClaims:
    report: Path
    shas: list[Claim]
    paths: list[Claim]
    nodes: list[NodeClaim]
    retired: list[RetiredClaim]

    @property
    def name(self) -> str:
        return self.report.name

    def where(self, line: int) -> str:
        rel = self.report.relative_to(REPO_ROOT)
        return f"{rel}:{line}"


def report_files() -> list[Path]:
    """Every evidence report, discovered -- never enumerated by hand."""
    return sorted(REPORTS_DIR.glob("*.md"))


def parse_claims(report: Path) -> ReportClaims:
    text = report.read_text()
    shas: list[Claim] = []
    paths: list[Claim] = []
    nodes: list[NodeClaim] = []
    retired: list[RetiredClaim] = []
    current: str | None = None
    for lineno, line in enumerate(text.splitlines(), start=1):
        for token in TOKEN_RE.finditer(line):
            chunk = token.group(0)
            sha = SHA_RE.match(chunk)
            if sha:
                shas.append(Claim(sha.group(1), lineno))
                continue
            hit = PATH_RE.match(chunk)
            if hit:
                is_retired = bool(hit.group(3))
                if hit.group(2):
                    # Only a path named *with* a node id becomes the one that
                    # later bare node ids continue -- whether or not THIS
                    # citation is retired, so a retired first row can still
                    # be followed by `::other_node!retired` continuations.
                    current = hit.group(1)
                    node_name = hit.group(2).lstrip(":")
                    if is_retired:
                        retired.append(RetiredClaim(current, node_name, lineno))
                    else:
                        paths.append(Claim(current, lineno))
                        nodes.append(NodeClaim(current, node_name, lineno))
                elif is_retired:
                    retired.append(RetiredClaim(hit.group(1), None, lineno))
                else:
                    paths.append(Claim(hit.group(1), lineno))
                    # A bare path (no node) mentioned in running prose
                    # introduces a new "current" *test* file for any bare
                    # `::node` that follows, same as a path named *with* a
                    # node would -- but only when the bare path is itself
                    # under `tests/`: prose freely names an unrelated source
                    # file in passing (`src/ralphd/cli/image.py`) while a
                    # node id right after it still means the test file
                    # established earlier, and a table row's other column
                    # often names an unrelated file (`container/Dockerfile`)
                    # too -- neither should steal `current` away from the
                    # test file whose rows/paragraphs are still continuing.
                    if hit.group(1).startswith("tests/") and not line.lstrip().startswith("|"):
                        current = hit.group(1)
                continue
            node = NODE_ONLY_RE.match(chunk)
            if node:
                node_name = node.group(1).lstrip(":")
                if node.group(2):
                    retired.append(RetiredClaim(current, node_name, lineno))
                else:
                    nodes.append(NodeClaim(current, node_name, lineno))
    return ReportClaims(
        report=report, shas=shas, paths=paths, nodes=nodes, retired=retired
    )


def commit_problems(claims: ReportClaims) -> list[str]:
    """Every listed sha must be a commit object in this repo."""
    if not claims.shas:
        return []
    wanted = sorted({c.value for c in claims.shas})
    proc = subprocess.run(
        ["git", "cat-file", "--batch-check"],
        cwd=REPO_ROOT,
        input="\n".join(wanted) + "\n",
        capture_output=True,
        text=True,
        check=True,
    )
    kinds: dict[str, str] = {}
    for asked, answer in zip(wanted, proc.stdout.splitlines(), strict=True):
        kinds[asked] = answer.split(" ", 2)[1] if " " in answer else answer
    problems = []
    for claim in claims.shas:
        kind = kinds.get(claim.value, "unknown")
        if kind != "commit":
            problems.append(
                f"{claims.where(claim.line)}: {claim.value} is not a commit in "
                f"this repo ({kind})"
            )
    return problems


_TRACKED_PATHS: set[str] | None = None


def _tracked_paths() -> set[str]:
    """One `git ls-files` snapshot per process, memoised.

    A cited directory (`artifacts/screenshots/hub/`, with a trailing slash)
    is not itself a row of `git ls-files` -- only the files under it are --
    so membership is checked both as an exact path and as a prefix of some
    tracked file.
    """
    global _TRACKED_PATHS
    if _TRACKED_PATHS is None:
        proc = subprocess.run(
            ["git", "ls-files"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=True,
        )
        _TRACKED_PATHS = set(proc.stdout.splitlines())
    return _TRACKED_PATHS


def _is_tracked(value: str, tracked: set[str]) -> bool:
    if value in tracked:
        return True
    prefix = value if value.endswith("/") else value + "/"
    return any(p.startswith(prefix) for p in tracked)


def path_problems(claims: ReportClaims) -> list[str]:
    """Every listed repo path must be tracked by git.

    `.exists()` is not membership: a directory the browser tier creates
    (`artifacts/screenshots/hub/`) exists in the job's own workspace but is
    never committed, so it must fail here even though the filesystem check
    used to let it through. The two ways a path can be wrong are kept
    distinguishable: untracked (generated, never committed) versus tracked
    but missing from disk (deleted or renamed after the report was written).
    """
    tracked = _tracked_paths()
    problems = []
    for claim in claims.paths:
        if _is_tracked(claim.value, tracked):
            continue
        if (REPO_ROOT / claim.value).exists():
            problems.append(
                f"{claims.where(claim.line)}: listed path is generated, not "
                f"committed \u2014 cite it absolutely if it is a run-dir "
                f"path: {claim.value}"
            )
        else:
            problems.append(
                f"{claims.where(claim.line)}: listed path was deleted or "
                f"renamed: {claim.value}"
            )
    return problems


def defines_test(source: str, node: str) -> bool:
    if node.endswith("*"):
        pattern = rf"^(async )?def {re.escape(node[:-1])}[\w]*\("
    else:
        pattern = rf"^(async )?def {re.escape(node)}\("
    return bool(re.search(pattern, source, re.MULTILINE))


def node_problems(claims: ReportClaims) -> list[str]:
    """Every listed `::node` must be a test in the file it is attached to."""
    problems = []
    sources: dict[str, str] = {}
    for claim in claims.nodes:
        if claim.path is None:
            problems.append(
                f"{claims.where(claim.line)}: bare node id with no path: ::{claim.node}"
            )
            continue
        target = REPO_ROOT / claim.path
        if not target.exists():
            # path_problems() reports the missing file; nothing to read here.
            continue
        source = sources.setdefault(claim.path, target.read_text())
        if not defines_test(source, claim.node):
            problems.append(
                f"{claims.where(claim.line)}: {claim.path} has no test named "
                f"{claim.node}"
            )
    return problems


def retired_problems(claims: ReportClaims) -> list[str]:
    """The STRONG direction for `!retired`: a citation marked retired must
    NOT resolve. A retired path that still exists on disk, or a retired node
    that still collects (its file exists and still defines it), is wrong --
    either it was never actually retired, or the marker is stale and should
    be removed instead of carried forward.
    """
    problems = []
    sources: dict[str, str] = {}
    for claim in claims.retired:
        if claim.path is None:
            problems.append(
                f"{claims.where(claim.line)}: bare retired node id with no "
                f"path: ::{claim.node}!retired"
            )
            continue
        target = REPO_ROOT / claim.path
        if claim.node is None:
            if target.exists():
                problems.append(
                    f"{claims.where(claim.line)}: path marked !retired but "
                    f"still exists: {claim.path}"
                )
            continue
        if not target.exists():
            # The whole file is gone -- the node cannot collect either way.
            continue
        source = sources.setdefault(claim.path, target.read_text())
        if defines_test(source, claim.node):
            problems.append(
                f"{claims.where(claim.line)}: {claim.path}::{claim.node} is "
                f"marked !retired but still collects"
            )
    return problems
