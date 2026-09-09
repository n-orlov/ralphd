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
  commit object in this repo that is reachable from `HEAD` (task 009, #176)
  -- a sha that exists only as a dangling object (git keeps an amended,
  reset or rebased-away commit's pre-image on disk until `git gc` reclaims
  it) is not evidence a report can point at either.
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

import bisect
import re
import subprocess
import zlib
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


# --------------------------------------------------------------------------
# Object-type lookup WITHOUT spawning git (task 035, requirement E.4).
#
# A sha claim asks "is this hex token a commit object in this repo", which is
# not a question `git ls-files` can answer -- it used to be answered by a
# `git cat-file --batch-check` subprocess (one per report before this task,
# then one per process). E.4 pins the records family's object-type question
# at NO subprocess of its own, so the object-type answer is read straight
# out of `.git` in this process instead: loose objects give their type in
# their own zlib header, packed objects give it in the type bits of their
# pack entry header (following a delta chain to its base when the entry is
# an OFS_DELTA/REF_DELTA, exactly as git does). The vocabulary returned
# matches `git cat-file --batch-check`'s own words, so `commit_problems`'s
# messages are unchanged: `commit`/`blob`/`tree`/`tag` for a resolved
# object, `missing` for a prefix nothing matches, `ambiguous`
# for a prefix more than one object matches.
# --------------------------------------------------------------------------

_PACK_ENTRY_TYPES = {1: "commit", 2: "tree", 3: "blob", 4: "tag"}
_OFS_DELTA, _REF_DELTA = 6, 7


def _git_dir() -> Path:
    """This checkout's git directory, including the `gitdir:` indirection a
    worktree's `.git` *file* uses."""
    dot_git = REPO_ROOT / ".git"
    if dot_git.is_file():
        pointer = dot_git.read_text().strip()
        target = pointer.split(":", 1)[1].strip() if ":" in pointer else pointer
        return (REPO_ROOT / target).resolve()
    return dot_git


def _object_dirs(git_dir: Path) -> list[Path]:
    """`<git-dir>/objects` plus any alternates it borrows from."""
    dirs = [git_dir / "objects"]
    alternates = git_dir / "objects" / "info" / "alternates"
    if alternates.is_file():
        for line in alternates.read_text().splitlines():
            if line.strip():
                dirs.append((git_dir / "objects" / line.strip()).resolve())
    return dirs


def _pack_offsets(idx: Path) -> dict[str, int]:
    """Every object in one pack, as sha -> byte offset in the .pack file,
    read from the v2 index: magic+version (8 bytes), 256-entry fanout, the
    sha table, the CRC table, the 4-byte offset table (high bit set means
    "index into the 8-byte large-offset table that follows")."""
    data = idx.read_bytes()
    if data[:4] != b"\xfftOc" or int.from_bytes(data[4:8], "big") != 2:
        raise AssertionError(f"{idx}: not a version-2 pack index")
    fanout_end = 8 + 256 * 4
    count = int.from_bytes(data[fanout_end - 4:fanout_end], "big")
    shas_at = fanout_end
    crcs_at = shas_at + count * 20
    offsets_at = crcs_at + count * 4
    large_at = offsets_at + count * 4
    offsets: dict[str, int] = {}
    for i in range(count):
        sha = data[shas_at + i * 20:shas_at + i * 20 + 20].hex()
        raw = int.from_bytes(data[offsets_at + i * 4:offsets_at + i * 4 + 4], "big")
        if raw & 0x80000000:
            slot = raw & 0x7FFFFFFF
            raw = int.from_bytes(data[large_at + slot * 8:large_at + slot * 8 + 8],
                                 "big")
        offsets[sha] = raw
    return offsets


class _ObjectDatabase:
    """Read-only, in-process view of this repo's object store."""

    def __init__(self) -> None:
        git_dir = _git_dir()
        self._loose: dict[str, Path] = {}
        self._packed: dict[str, tuple[Path, int]] = {}
        for objects in _object_dirs(git_dir):
            for shard in sorted(objects.glob("[0-9a-f][0-9a-f]")):
                for obj in shard.iterdir():
                    if len(obj.name) == 38:
                        self._loose.setdefault(shard.name + obj.name, obj)
            for idx in sorted((objects / "pack").glob("pack-*.idx")):
                pack = idx.with_suffix(".pack")
                for sha, offset in _pack_offsets(idx).items():
                    self._packed.setdefault(sha, (pack, offset))
        self._all = sorted(set(self._loose) | set(self._packed))

    def _matches(self, prefix: str) -> list[str]:
        """Every full sha in the store starting with `prefix`, capped at two
        (an ambiguous prefix never needs a third to be reported as such)."""
        start = bisect.bisect_left(self._all, prefix)
        matches = []
        for sha in self._all[start:]:
            if not sha.startswith(prefix):
                break
            matches.append(sha)
            if len(matches) > 1:
                break
        return matches

    def kind(self, prefix: str) -> str:
        """`git cat-file --batch-check`'s answer for one (possibly
        abbreviated) sha."""
        matches = self._matches(prefix)
        if len(matches) > 1:
            return "ambiguous"
        if not matches:
            return "missing"
        return self._kind_of(matches[0])

    def resolve(self, prefix: str) -> str | None:
        """The one full 40-char sha `prefix` resolves to, or `None` when it
        is missing or ambiguous -- `kind()` already reports which. Needed
        because a reachability check (`_reachable_commits()`) only ever
        holds full shas, and a report's own citation may be abbreviated."""
        matches = self._matches(prefix)
        return matches[0] if len(matches) == 1 else None

    def _kind_of(self, sha: str) -> str:
        loose = self._loose.get(sha)
        if loose is not None:
            header = zlib.decompressobj().decompress(loose.read_bytes(), 64)
            return header.split(b" ", 1)[0].decode()
        pack, offset = self._packed[sha]
        with pack.open("rb") as handle:
            return self._packed_kind(handle, offset)

    def _packed_kind(self, handle, offset: int) -> str:
        """The type of one pack entry, resolving a delta to its base's type
        (a delta never changes the object type)."""
        seen = 0
        while True:
            seen += 1
            if seen > 64:
                raise AssertionError("pack delta chain too deep to be real")
            handle.seek(offset)
            byte = handle.read(1)[0]
            entry_type = (byte >> 4) & 7
            while byte & 0x80:
                byte = handle.read(1)[0]
            if entry_type in _PACK_ENTRY_TYPES:
                return _PACK_ENTRY_TYPES[entry_type]
            if entry_type == _OFS_DELTA:
                byte = handle.read(1)[0]
                back = byte & 0x7F
                while byte & 0x80:
                    byte = handle.read(1)[0]
                    back = ((back + 1) << 7) | (byte & 0x7F)
                offset = offset - back
                continue
            if entry_type == _REF_DELTA:
                base = handle.read(20).hex()
                if base in self._packed and self._packed[base][0] == Path(
                        handle.name):
                    offset = self._packed[base][1]
                    continue
                return self._kind_of(base)
            raise AssertionError(f"unknown pack entry type {entry_type}")


_OBJECTS: _ObjectDatabase | None = None
_COMMIT_KINDS: dict[str, str] | None = None
_COMMIT_FULL_SHAS: dict[str, str | None] | None = None
_REACHABLE_COMMITS: set[str] | None = None


def _objects() -> _ObjectDatabase:
    """One scan of `.git`'s object store per process, memoised."""
    global _OBJECTS
    if _OBJECTS is None:
        _OBJECTS = _ObjectDatabase()
    return _OBJECTS


def _cited_shas() -> list[str]:
    """Every sha cited anywhere under `artifacts/reports/`, the population
    both `_commit_kinds()` and `_commit_full_shas()` resolve once each."""
    return sorted({
        claim.value
        for report in report_files()
        for claim in parse_claims(report).shas
    })


def _commit_kinds() -> dict[str, str]:
    """Every cited sha's object type, resolved once for the life of the
    process and memoised -- with NO subprocess at all (see the block
    comment above). Before task 035 (requirement E.4) this ran a
    `git cat-file --batch-check` once PER REPORT it was called on, about 218
    of the ~220 subprocess calls a full `tests/records/` run made; the
    object-type answer is read out of `.git` in-process instead.
    """
    global _COMMIT_KINDS
    if _COMMIT_KINDS is None:
        wanted = _cited_shas()
        objects = _objects() if wanted else None
        _COMMIT_KINDS = {sha: objects.kind(sha) for sha in wanted} if wanted else {}
    return _COMMIT_KINDS


def _commit_full_shas() -> dict[str, str | None]:
    """Every cited (possibly abbreviated) sha's resolved full 40-char sha,
    memoised alongside `_commit_kinds()` -- needed to check a resolved
    commit against `_reachable_commits()`, which only ever holds full
    shas."""
    global _COMMIT_FULL_SHAS
    if _COMMIT_FULL_SHAS is None:
        wanted = _cited_shas()
        objects = _objects() if wanted else None
        _COMMIT_FULL_SHAS = (
            {sha: objects.resolve(sha) for sha in wanted} if wanted else {}
        )
    return _COMMIT_FULL_SHAS


def _reachable_commits() -> set[str]:
    """Every commit sha reachable from `HEAD`, one `git rev-list HEAD`
    snapshot per process, memoised the same way `_tracked_paths()` memoises
    its own `git ls-files` snapshot (task 009, #176).

    A sha can resolve to a real `commit` object in `_commit_kinds()` and
    still not be evidence of anything: `git` keeps a commit's pre-image on
    disk, as an ordinary loose or packed object, for as long as it takes
    `git gc` to reclaim it, even after an amend, a reset or a rebase drops
    every ref that used to point at it. Existing as an object is not the
    same claim as being part of this repo's history; only the second one is
    what a report's citation means.
    """
    global _REACHABLE_COMMITS
    if _REACHABLE_COMMITS is None:
        proc = subprocess.run(
            ["git", "rev-list", "HEAD"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=True,
        )
        _REACHABLE_COMMITS = set(proc.stdout.splitlines())
    return _REACHABLE_COMMITS


def commit_problems(claims: ReportClaims) -> list[str]:
    """Every listed sha must be a commit object in this repo AND reachable
    from `HEAD` -- a sha that only exists as a dangling object (task 009,
    #176) is not evidence a report can point at either."""
    if not claims.shas:
        return []
    kinds = _commit_kinds()
    fulls = _commit_full_shas()
    reachable = _reachable_commits()
    problems = []
    for claim in claims.shas:
        kind = kinds.get(claim.value, "unknown")
        if kind != "commit":
            problems.append(
                f"{claims.where(claim.line)}: {claim.value} is not a commit in "
                f"this repo ({kind})"
            )
            continue
        full = fulls.get(claim.value)
        if full not in reachable:
            problems.append(
                f"{claims.where(claim.line)}: {claim.value} is a commit but "
                f"not reachable from HEAD (dangling)"
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
