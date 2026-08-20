"""Job-image inputs: what goes into the image, and its content hash (task 032, #20).

`container/Dockerfile` has always existed and nothing ever built it: `--image`
/ `RALPHD_IMAGE` / registry `config.yaml`'s `image` only ever *selected* a tag
somebody built by hand. The consequence is on the record -- two runs of this
project executed a ten-day-old engine and reported `costUSD: 0` in the pre-#10
shape, and no surface could say so.

The fix (requirement H) is to tag the image by the *content of its inputs*, so
"the image matches the source" is structural rather than something an operator
remembers. This module owns only the first half of that: **which files are
inputs, and what their content hashes to.** Building, tag lookup, base-image
derivation, precedence between supply points and staleness reporting are later
tasks -- nothing here runs `docker`.

What is an input, and what is deliberately not
----------------------------------------------
`IMAGE_INPUTS` is `container/` (the Dockerfile and its entrypoint), the
installed engine source `src/ralphd/`, and `pyproject.toml` (the install
recipe, including the pinned dependencies and console-script names). Those
three, and nothing else, decide what the built image *does*.

`tests/`, `docs/`, `examples/`, `SPEC.md` and `artifacts/` are all outside the
hash even though a `COPY . /opt/ralphd` sweeps some of them into the build
context: a docs typo must not invalidate every cached image, and (the reason
the PRD calls it out) `artifacts/` is a directory a *running job* writes into,
so hashing it would make the tag depend on the run's own output. The rule of
thumb: if changing the file cannot change what `ralphd-engine` does inside the
container, it is not an input.

Determinism, and what "cheap" means
-----------------------------------
The digest must be reproducible on another machine and in another checkout
path, so the hashed stream contains only: a format version, the requested
input names (including the ones that were *missing* -- an image built without
`pyproject.toml` is a different image), each file's path **relative to the
source root**, its executable bit, its size and its content digest, in
`sorted()` path order. It never contains an absolute path, an mtime, a uid, an
inode or a directory-listing order -- the four things that make naive tree
hashes differ between two identical checkouts.

Cheap means: excluded directory names are *pruned*, never walked
(`EXCLUDED_DIR_NAMES`, e.g. `.git`, `artifacts`, `__pycache__`, `.venv`), and
symlinks are hashed as their target string rather than followed -- both so a
link or a stray build tree cannot drag an unbounded subtree into the hash.
`HashedTree.dirs` reports every directory actually traversed, which is how the
tests assert the pruning instead of trusting this docstring.
"""

from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass
from pathlib import Path

# Hash-stream format. Bump when the framing below changes in a way that would
# otherwise silently reuse an image built by an older ralphd under a tag whose
# meaning has changed (every tag changes when this changes -- that is the point).
HASH_FORMAT = "ralphd-image-inputs v1"

# Length of the short hash used as a docker tag component. 12 hex chars = 48
# bits; git's abbreviated-sha convention, and long enough that an accidental
# collision between two source trees is not a thing that happens.
HASH_LENGTH = 12

# What the short hash is allowed to look like, so a caller building
# `ralphd:<hash>` never has to wonder whether it is a legal docker tag
# (reference-spec tag charset: [A-Za-z0-9_][A-Za-z0-9._-]{0,127}).
HASH_RE = re.compile(rf"^[0-9a-f]{{{HASH_LENGTH}}}$")

# The image inputs, relative to the source root. Order is irrelevant (the
# stream is sorted); this tuple is the *definition* of "the image inputs" that
# docs and later tasks refer to.
IMAGE_INPUTS = ("container", "pyproject.toml", "src/ralphd")

# The file that marks a directory as a ralphd source root worth hashing --
# i.e. a checkout, as opposed to a `pipx`/wheel install that has no
# `container/` at all (the packaging interaction task 038 decides).
SOURCE_MARKER = "container/Dockerfile"

# Directory names pruned outright, never traversed. Two kinds: version
# control and caches (nothing about them reaches the image), and directories a
# *run* writes (`artifacts`, `.ralphd`) which must never feed back into the tag.
EXCLUDED_DIR_NAMES = frozenset({
    ".git", ".hg", ".svn",
    "__pycache__", ".pytest_cache", ".ruff_cache", ".mypy_cache",
    ".venv", "venv", "node_modules", ".playwright-cli",
    "artifacts", ".ralphd",
    "dist", "build", ".eggs",
})

# File names/suffixes ignored inside an input tree: build droppings and editor
# leftovers, none of which the install step reads.
EXCLUDED_SUFFIXES = (".pyc", ".pyo", ".pyd", ".orig", ".rej", ".swp", "~")
EXCLUDED_FILE_NAMES = frozenset({".DS_Store"})

_CHUNK = 1 << 20


def is_excluded_dir(name: str) -> bool:
    """Prune this directory name? (`.egg-info` matched by suffix.)"""
    return name in EXCLUDED_DIR_NAMES or name.endswith(".egg-info")


def is_excluded_file(name: str) -> bool:
    return name in EXCLUDED_FILE_NAMES or name.endswith(EXCLUDED_SUFFIXES)


@dataclass(frozen=True)
class HashedTree:
    """The result of hashing a set of input paths under one source root.

    `hash` is the short tag component; `digest` the full sha256 it abbreviates.
    `present`/`missing` name the requested inputs that did and did not exist --
    `missing` is part of the hashed stream, so a checkout that lost an input
    cannot masquerade as one that has it. `dirs` is every directory actually
    traversed (relative, sorted, source root as `.`), published so that "the
    excluded directories were not walked" is an assertion a test can make
    rather than a claim this module makes about itself.
    """

    root: Path
    digest: str
    hash: str
    files: tuple[str, ...]
    bytes: int
    present: tuple[str, ...]
    missing: tuple[str, ...]
    dirs: tuple[str, ...]

    @property
    def complete(self) -> bool:
        """True when every requested input existed. A hash over a partial
        input set is still a real hash of what is there -- but a caller about
        to *build* from it wants to know it is not looking at a full checkout."""
        return not self.missing

    @property
    def file_count(self) -> int:
        return len(self.files)


def source_root(start: Path | str | None = None) -> Path | None:
    """The ralphd source root whose image inputs can be hashed, or None.

    Resolved from *this file's* location, not the process cwd: for an editable
    or checkout install `src/ralphd/cli/image.py` sits three levels under the
    root, which is exactly the tree whose `container/` was meant to be built.
    Returns None when there is no `container/Dockerfile` there -- the wheel /
    `pipx` case, where the inputs simply are not on disk. Absence is an answer;
    callers must not substitute a guess for it.
    """
    base = Path(start) if start is not None else Path(__file__).resolve().parents[3]
    base = Path(base).resolve()
    return base if (base / SOURCE_MARKER).is_file() else None


def _entry_digest(path: Path) -> tuple[str, str, int, str]:
    """(kind, mode, size, sha256) for one file or symlink.

    A symlink is hashed as its *target string* and never followed: following
    would let a link into `.git` (or out of the tree entirely) smuggle an
    unbounded, non-reproducible subtree into the tag. Only the executable bit
    of the mode is recorded -- it is the one permission bit `docker build`
    and `git` both preserve, and `container/entrypoint.sh` depends on it.
    """
    st = path.lstat()
    if os.path.islink(path):
        target = os.readlink(path).encode()
        return "link", "-", len(target), hashlib.sha256(target).hexdigest()
    h = hashlib.sha256()
    size = 0
    with open(path, "rb") as fh:
        while chunk := fh.read(_CHUNK):
            size += len(chunk)
            h.update(chunk)
    mode = "x" if st.st_mode & 0o111 else "-"
    return "file", mode, size, h.hexdigest()


def _collect(root: Path, rel: str, files: dict[str, Path], dirs: list[str]) -> None:
    """Walk one input path, pruning excluded names before touching them."""
    path = root / rel if rel != "." else root
    if os.path.islink(path) or not path.is_dir():
        files[rel] = path
        return
    dirs.append(rel)
    stack = [rel]
    while stack:
        cur = stack.pop()
        with os.scandir(root / cur) as it:
            for entry in it:
                child = f"{cur}/{entry.name}" if cur != "." else entry.name
                if entry.is_dir(follow_symlinks=False):
                    if is_excluded_dir(entry.name):
                        continue
                    dirs.append(child)
                    stack.append(child)
                elif not is_excluded_file(entry.name):
                    files[child] = root / child


def hash_tree(root: Path | str, inputs: tuple[str, ...] = IMAGE_INPUTS) -> HashedTree:
    """Hash `inputs` (paths relative to `root`) into a HashedTree.

    The generic form, so a user-supplied build context (requirement H2/H3) can
    be hashed by the same rules rather than by a second implementation.
    """
    root = Path(root).resolve()
    files: dict[str, Path] = {}
    dirs: list[str] = []
    present, missing = [], []
    for rel in sorted(inputs):
        target = root / rel
        if not (target.exists() or os.path.islink(target)):
            missing.append(rel)
            continue
        present.append(rel)
        _collect(root, rel, files, dirs)

    outer = hashlib.sha256()
    outer.update(f"{HASH_FORMAT}\n".encode())
    for rel in present:
        outer.update(f"input {rel}\n".encode())
    for rel in missing:
        outer.update(f"missing {rel}\n".encode())
    total = 0
    for rel in sorted(files):
        kind, mode, size, digest = _entry_digest(files[rel])
        total += size
        outer.update(f"{kind} {mode} {size} {digest} {rel}\n".encode())
    digest = outer.hexdigest()
    return HashedTree(root=root, digest=digest, hash=digest[:HASH_LENGTH],
                      files=tuple(sorted(files)), bytes=total,
                      present=tuple(present), missing=tuple(missing),
                      dirs=tuple(sorted(dirs)))


def hash_image_inputs(root: Path | str) -> HashedTree:
    """Hash the job image's inputs (`IMAGE_INPUTS`) under a source root."""
    return hash_tree(root, IMAGE_INPUTS)
