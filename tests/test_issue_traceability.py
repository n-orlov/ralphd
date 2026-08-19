"""Keeps `artifacts/reports/issue-traceability.md` (task 058) honest.

The PRD forbids closing the GitHub issues from inside the run: the operator
closes #1-#11 and #13 from that report instead. A report that names a commit
that never landed, or a test that no longer exists, is worse than no report,
so this module re-reads it and asserts:

* every issue in scope has a section,
* every 7-hex commit sha it lists exists in `git log`,
* every `tests/...` path it lists exists, and every `::node_id` after such a
  path corresponds to a `def`/`async def` in that file,
* no `gh issue close` anywhere in the tree (the report claims this).
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
REPORT = REPO_ROOT / "artifacts" / "reports" / "issue-traceability.md"

ISSUES_IN_SCOPE = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 13]

SHA_RE = re.compile(r"`([0-9a-f]{7,40})`")
# `tests/foo.py::test_a`, `::test_b` continuing the same path
PATH_RE = re.compile(r"`((?:tests|src|docs|examples|artifacts)/[\w./-]+?)(::[\w:]+)?`")
NODE_ONLY_RE = re.compile(r"`(::[\w:]+)`")


def _report_text() -> str:
    assert REPORT.exists(), f"{REPORT} is missing"
    return REPORT.read_text()


def test_report_covers_every_issue_in_scope() -> None:
    text = _report_text()
    for issue in ISSUES_IN_SCOPE:
        assert re.search(rf"^## #{issue}\b", text, re.MULTILINE), f"no section for #{issue}"


def test_every_listed_commit_sha_exists_in_git_log() -> None:
    text = _report_text()
    shas = sorted({m.group(1) for m in SHA_RE.finditer(text)})
    assert len(shas) >= 40, f"suspiciously few commits listed: {shas}"
    for sha in shas:
        proc = subprocess.run(
            ["git", "cat-file", "-t", sha],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
        )
        assert proc.returncode == 0 and proc.stdout.strip() == "commit", (
            f"{sha} is not a commit in this repo ({proc.stdout.strip()}"
            f"{proc.stderr.strip()})"
        )


def test_every_listed_path_and_test_node_exists() -> None:
    text = _report_text()
    paths: list[str] = []
    nodes: list[tuple[str, str]] = []
    for line in text.splitlines():
        current: str | None = None
        for token in re.finditer(r"`[^`]+`", line):
            chunk = token.group(0)
            hit = PATH_RE.fullmatch(chunk)
            if hit:
                current = hit.group(1)
                paths.append(current)
                if hit.group(2):
                    nodes.append((current, hit.group(2).lstrip(":")))
                continue
            node = NODE_ONLY_RE.fullmatch(chunk)
            if node:
                assert current is not None, f"bare node id with no path: {line}"
                nodes.append((current, node.group(1).lstrip(":")))

    assert len(paths) >= 40, f"suspiciously few paths listed: {paths}"
    assert len(nodes) >= 80, f"suspiciously few test node ids listed: {len(nodes)}"

    for rel in sorted(set(paths)):
        assert (REPO_ROOT / rel).exists(), f"listed path does not exist: {rel}"

    for rel, node in nodes:
        src = (REPO_ROOT / rel).read_text()
        assert re.search(rf"^(async )?def {re.escape(node)}\(", src, re.MULTILINE), (
            f"{rel} has no test named {node}"
        )


def test_no_issue_was_closed_from_inside_the_run() -> None:
    # An actual invocation always names an issue number, which is what this
    # looks for -- the report and this module both *quote* the forbidden
    # command without a number, and must not trip the check.
    invocation = r"gh issue close +#?[0-9]"
    proc = subprocess.run(
        ["git", "grep", "-rIn", "-E", invocation],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    assert proc.stdout.strip() == "", f"found an issue-closing command: {proc.stdout}"
    log = subprocess.run(
        ["git", "log", "--format=%B"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    assert not re.search(invocation, log.stdout), "history closes a GitHub issue"
