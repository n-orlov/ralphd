"""Shared re-reading of the evidence reports under `artifacts/reports/`.

Task 041 (#22): the sha/path/test-node checks that task 058 wrote for
`artifacts/reports/issue-traceability.md` are the ones every report in that
directory needs -- a report that names a commit that never landed, a file that
has been renamed away, or a test that no longer exists is worse than no report.
So the extraction and the checks live here once and
`tests/test_report_claims.py` applies them to every report the directory
holds (discovered by glob, so a new report is covered the day it lands);
`tests/test_issue_traceability.py` keeps only what is specific to its report
and re-uses this parser for its density assertions.

The claim vocabulary the reports use, and how it is read:

* A backticked 7-40 char hex token is a commit sha, and must resolve to a
  commit object in this repo.
* A backticked path under one of `REPO_DIRS` is a repo-relative path and must
  exist. Anything else in backticks is prose: `ci/Dockerfile` in the
  sibling-toolchain recipe belongs to a *target* repo, and a run-dir artifact
  is written `<run-dir>/artifacts/...` precisely so this parser does not read
  it as a claim about the checkout.
* `path::node` names a test; a following ``::node`` or ``\u2026::node`` (the
  report style for "another test in the same file") continues the most
  recent path that was *itself* named with a node id, across lines, since the
  reports continue one test file down the rows of a table while naming
  unrelated files (`container/Dockerfile`) in the other column.
* A trailing `*` (`::test_retry_proxy_*`, standing for a family of tests) is
  a prefix: at least one test in the file must match it.
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
    rf"^`((?:{'|'.join(REPO_DIRS)})/[\w./-]+?)(::[\w:*]+)?`$"
)
# `::test_b` / `\u2026::test_b` -- a node id continuing the last named path.
NODE_ONLY_RE = re.compile(r"^`(?:\u2026|\.\.\.)?(::[\w:*]+)`$")


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
class ReportClaims:
    report: Path
    shas: list[Claim]
    paths: list[Claim]
    nodes: list[NodeClaim]

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
                paths.append(Claim(hit.group(1), lineno))
                if hit.group(2):
                    # Only a path named *with* a node id becomes the one that
                    # later bare node ids continue.
                    current = hit.group(1)
                    nodes.append(NodeClaim(current, hit.group(2).lstrip(":"), lineno))
                continue
            node = NODE_ONLY_RE.match(chunk)
            if node:
                nodes.append(NodeClaim(current, node.group(1).lstrip(":"), lineno))
    return ReportClaims(report=report, shas=shas, paths=paths, nodes=nodes)


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


def path_problems(claims: ReportClaims) -> list[str]:
    """Every listed repo path must exist."""
    return [
        f"{claims.where(claim.line)}: listed path does not exist: {claim.value}"
        for claim in claims.paths
        if not (REPO_ROOT / claim.value).exists()
    ]


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
