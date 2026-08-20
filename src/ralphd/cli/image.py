"""Job-image inputs: what goes into the image, and its content hash (task 032, #20).

`container/Dockerfile` has always existed and nothing ever built it: `--image`
/ `RALPHD_IMAGE` / registry `config.yaml`'s `image` only ever *selected* a tag
somebody built by hand. The consequence is on the record -- two runs of this
project executed a ten-day-old engine and reported `costUSD: 0` in the pre-#10
shape, and no surface could say so.

The fix (requirement H) is to tag the image by the *content of its inputs*, so
"the image matches the source" is structural rather than something an operator
remembers. This module owns the *declarative* half of that: **which files are
inputs, what their content hashes to, and -- for a bring-your-own base image
(requirement H2) -- the text of the derived Dockerfile and the tag it hashes
to.** Running a build, looking a tag up, precedence between supply points and
staleness reporting are elsewhere -- nothing here runs `docker`.

Deriving from a base image (H2)
-------------------------------
A user-supplied image is a *base*, never a finished job image: `derive()`
renders a Dockerfile that layers the engine and pi on top of it and tags the
result `ralphd-derived:<hash>`, where the hash covers **both** the base
reference and the source digest (plus the rendered Dockerfile itself, so a
change to the recipe below is a new tag too). That is what makes "the base only
has to carry the toolchain your repo needs" true: the operator never replicates
the engine's install contract, and the derived image is cached and invalidated
by exactly the same rules as the default one.

The version pins (pi, the docker client) are **copied verbatim** out of
`container/Dockerfile`'s own `ARG` lines rather than restated here, so there is
still exactly one place in the repo that says which pi version ralphd runs.

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

A wheel install, and where the inputs live (task 038, requirement H4)
--------------------------------------------------------------------
`pipx install ralphd` has no checkout next to it, so there is no `container/`
directory on disk to hash. The decision (documented with its reasoning in
`docs/architecture.md`) is to **ship the image inputs as package data**: the
wheel carries `PACKAGED_FILES` under `<installed ralphd>/_image/`, and the
engine source the recipe installs is the installed package itself. `find_inputs`
answers *where* the inputs are (`INPUTS_CHECKOUT` / `INPUTS_PACKAGED` /
`INPUTS_NONE` -- absence is still an answer, never a guess), and `stage_inputs`
assembles the packaged half into a build context laid out exactly like a
checkout, so the same tree hashes to the same `ralphd:<hash>` a checkout of the
same version builds. The alternative the PRD offered -- falling back to a
pinned *published* tag -- would mean pinning a reference nobody publishes yet
(v0.6 ships no registry image), i.e. a name that cannot be pulled.

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

# The image repository the content hash tags into: `ralphd:<hash>` (task 033,
# requirement H1). Spelled once here so the builder, the cache lookup and
# `doctor`'s staleness check cannot disagree about what the default job image
# is called.
IMAGE_REPO = "ralphd"

# The repository derived images (task 034, requirement H2) are tagged into. A
# *separate* repository from IMAGE_REPO on purpose: `ralphd:<hash>` means "the
# default image, whose hash is exactly the source hash", and a surface that
# compares a tag against the current source hash (`doctor`, task 037) would
# call every derived image stale if the two shared a namespace. A derived hash
# is only meaningful next to the base it was derived from.
DERIVED_REPO = "ralphd-derived"

# Hash-stream format for a derived tag, versioned like HASH_FORMAT.
DERIVED_FORMAT = "ralphd-derived-image v1"

# The repository a base image built from an operator's *own* Dockerfile (task
# 035, requirement H3: `start --dockerfile <path>`, or the `dockerfile:` key in
# a template's job.yaml / the registry config) is tagged into. A third
# repository, for the same reason DERIVED_REPO is separate from IMAGE_REPO:
# this hash is over somebody else's build context, so comparing it against a
# ralphd source hash would be meaningless. The job image is then *derived* from
# this tag -- a `--dockerfile` supplies a base, exactly like `--base-image`,
# and never the finished job image.
BASE_REPO = "ralphd-base"

# Hash-stream format for a base tag, versioned like HASH_FORMAT.
BASE_FORMAT = "ralphd-base-image v1"

# The one instruction a build context's Dockerfile cannot do without, so that
# `--dockerfile <some text file>` is refused *here*, naming the file, instead
# of thirty seconds into a build. (Case-insensitive: `from` is legal.)
FROM_RE = re.compile(r"^[ \t]*FROM[ \t]+\S", re.IGNORECASE | re.MULTILINE)

# What a base image reference may look like. Deliberately strict: the reference
# is interpolated into a generated `FROM` line, so anything with whitespace or
# a newline in it must be refused rather than rendered (see _check_base).
BASE_REF_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@-]*$")

# `ARG NAME=default` in a Dockerfile, and the node major version inside
# `container/Dockerfile`'s nodesource setup URL -- the two things the derived
# recipe inherits instead of restating.
ARG_LINE_RE = re.compile(r"^ARG\s+([A-Za-z_][A-Za-z0-9_]*)=(\S*)[ \t]*$", re.MULTILINE)
NODE_MAJOR_RE = re.compile(r"setup_(\d+)\.x")

# The one pin the derived recipe cannot do without: pi's version. (An
# unpinned `npm install -g @earendil-works/pi-coding-agent` silently resolves
# whatever npm feels like -- the failure mode container/Dockerfile's own
# comment warns about.)
PI_PIN = "PI_VERSION"

# The file that marks a directory as a ralphd source root worth hashing --
# i.e. a checkout, or (task 038) the packaged copy of the same inputs inside a
# wheel install, which is laid out identically on purpose.
SOURCE_MARKER = "container/Dockerfile"

# Where the image inputs live inside an installed wheel: `ralphd/_image/`
# (task 038, requirement H4's packaging interaction). Underscored so it cannot
# collide with an importable submodule name, and *inside* the package because
# that is the only place a wheel can put data that survives `pipx install`.
PACKAGED_DIR_NAME = "_image"

# What that directory has to carry for the staged context to be an installable
# recipe: the image inputs proper minus the engine source (which is the
# installed package itself), plus the two metadata files `pyproject.toml`
# points at -- `pip install <context>` fails outright without the readme it
# declares, so "the wheel ships the Dockerfile" is not enough on its own.
# `pyproject.toml`'s `force-include` mapping must cover exactly these (a test
# reads the mapping and compares, so packaging and code cannot drift apart).
PACKAGED_FILES = ("container", "pyproject.toml", "README.md", "LICENSE")

# The two of them without which there is nothing to build at all -- checked
# before calling a directory a packaged supply.
PACKAGED_REQUIRED = (SOURCE_MARKER, "pyproject.toml")

# Where the image inputs were found. Vocabulary, so no surface invents its own
# word for "this install has no checkout but ships its own inputs".
INPUTS_CHECKOUT = "checkout"  # a source tree next to the install
INPUTS_PACKAGED = "packaged"  # package data inside a wheel/pipx install
INPUTS_NONE = "none"          # neither -- staleness unknowable, nothing to build

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


def image_tag(short_hash: str) -> str:
    """`ralphd:<hash>` -- the one spelling of the default job image's tag.

    Takes the short hash (`HashedTree.hash`) rather than a HashedTree so a
    caller holding only a tag component (`doctor` comparing the tag in use
    against the current source hash, task 037) uses the same formatting.
    """
    return f"{IMAGE_REPO}:{short_hash}"


def tag_hash(ref: str) -> str | None:
    """The content hash inside a `ralphd:<hash>` reference, or None.

    None means "this reference was not produced by hashing these inputs" --
    an operator's own pin, a registry ref, a `ralphd:dev` built by hand, or a
    *derived* `ralphd-derived:<hash>` (whose hash also covers a base image, so
    it is not comparable to a source hash -- `derived_tag_hash` reads those).
    Absence is an answer: a caller must not treat an unrecognized reference as
    stale *or* as fresh (task 037 reports it as neither).
    """
    repo, sep, tag = ref.partition(":")
    if not sep or repo != IMAGE_REPO or not HASH_RE.match(tag):
        return None
    return tag


def derived_tag(short_hash: str) -> str:
    """`ralphd-derived:<hash>` -- the one spelling of a derived image's tag."""
    return f"{DERIVED_REPO}:{short_hash}"


def derived_tag_hash(ref: str) -> str | None:
    """The content hash inside a `ralphd-derived:<hash>` reference, or None.

    Deliberately separate from `tag_hash`: a derived hash is a function of the
    base *and* the source, so a caller that compares it against a bare source
    hash would be wrong. `tag_hash` returns None for a derived reference for
    the same reason.
    """
    repo, sep, tag = ref.partition(":")
    if not sep or repo != DERIVED_REPO or not HASH_RE.match(tag):
        return None
    return tag


def base_tag(short_hash: str) -> str:
    """`ralphd-base:<hash>` -- the one spelling of a Dockerfile-built base tag."""
    return f"{BASE_REPO}:{short_hash}"


def base_tag_hash(ref: str) -> str | None:
    """The content hash inside a `ralphd-base:<hash>` reference, or None.

    Separate from `tag_hash`/`derived_tag_hash` for the same reason those two
    are separate from each other: this hash covers an operator's build context,
    not ralphd's source, so nothing may compare it against a source hash.
    """
    repo, sep, tag = ref.partition(":")
    if not sep or repo != BASE_REPO or not HASH_RE.match(tag):
        return None
    return tag


class ImageInputError(Exception):
    """The image inputs cannot produce a derived Dockerfile.

    Raised -- never swallowed, never worked around with a guessed default --
    when the base reference is unusable or `container/Dockerfile` no longer
    declares something the derived recipe copies out of it.
    """


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


# ------------------------------------- a wheel install's own inputs (038, H4)
# `pipx install ralphd` leaves no checkout behind, so `source_root()` is None
# and -- before this task -- nothing could be hashed or built: `start` fell
# back to the legacy `ralphd:dev` tag with a warning. The decision is to ship
# the inputs *inside* the wheel and assemble a build context from them, so a
# wheel install builds by content exactly like a checkout does. Two halves,
# both here because both are "which files are the inputs": where they are
# (`find_inputs`) and how the packaged half becomes a context (`stage_inputs`).


def package_dir() -> Path:
    """The installed `ralphd` package directory (`.../site-packages/ralphd`).

    Read from this file's location for the same reason `source_root` is: it is
    the one thing that is true whether ralphd was installed as a wheel, with
    `pipx`, or editable out of a checkout.
    """
    return Path(__file__).resolve().parents[1]


def packaged_inputs(pkg: Path | str | None = None) -> Path | None:
    """The packaged image inputs directory shipped in the wheel, or None.

    None means this install carries no package data to build from -- which,
    together with no checkout, is the honest `INPUTS_NONE`. Only
    `PACKAGED_REQUIRED` is insisted on here: the readme and license are needed
    by `pip install` and are asserted present by a packaging test, but a wheel
    that lost one of them is still better described by what it *does* have.
    """
    base = Path(pkg) if pkg is not None else package_dir()
    d = Path(base).resolve() / PACKAGED_DIR_NAME
    if all((d / rel).is_file() for rel in PACKAGED_REQUIRED):
        return d
    return None


@dataclass(frozen=True)
class InputsSupply:
    """Where this install's image inputs are, and what has to happen to use them.

    `kind` is one of INPUTS_CHECKOUT / INPUTS_PACKAGED / INPUTS_NONE -- the
    observable answer surfaces (`start`'s notice, `doctor`'s report) quote it
    rather than re-deriving it. `root` is a ready-to-build context and is set
    for a checkout only; `packaged` is the package-data directory and `package`
    the installed package that `stage_inputs` assembles a context out of.
    """

    kind: str
    root: Path | None = None
    packaged: Path | None = None
    package: Path | None = None

    @property
    def buildable(self) -> bool:
        """Can a job image be produced from this install at all?"""
        return self.kind != INPUTS_NONE

    @property
    def where(self) -> Path | None:
        """The directory to *name* when reporting where the inputs came from."""
        return self.root if self.kind == INPUTS_CHECKOUT else self.packaged


def find_inputs(root: Path | str | None = None,
                pkg: Path | str | None = None) -> InputsSupply:
    """Where the image inputs are for this install: checkout, package data, or
    nowhere.

    A checkout wins: in an editable or checkout install its tree is the live
    source, and hashing package data instead would tag an image built from the
    files as they were at install time. `root` given explicitly means "this
    tree or nothing" -- a caller naming a tree is not asking to be given
    somebody else's inputs -- so the packaged fallback is only consulted when
    nobody named one.
    """
    src = source_root(root)
    if src is not None:
        return InputsSupply(INPUTS_CHECKOUT, root=src)
    if root is None or pkg is not None:
        found = packaged_inputs(pkg)
        if found is not None:
            return InputsSupply(INPUTS_PACKAGED, packaged=found,
                                package=found.parent)
    return InputsSupply(INPUTS_NONE)


def stage_inputs(supply: InputsSupply, dest: Path | str) -> Path:
    """Assemble a packaged supply into a build context under `dest`; return it.

    The context is laid out exactly like a checkout -- `container/`,
    `pyproject.toml` and the metadata files from the package data, plus the
    installed package copied to `src/ralphd` -- because that is what makes the
    resulting tag *the same tag*: `hash_image_inputs` sees the same relative
    paths, modes, sizes and contents it would see in a checkout of the same
    version, so a `pipx` install and a checkout agree on `ralphd:<hash>` instead
    of each maintaining its own image.

    The copy walks with `_collect`, i.e. by exactly the rules that decide what
    is hashed (excluded dirs pruned, symlinks recreated rather than followed),
    so "the context contains what the hash covers" is one definition rather than
    two that can drift. `PACKAGED_DIR_NAME` is left out of the staged
    `src/ralphd`: a checkout has no such directory, and copying it in would
    change the hash and nest the inputs inside themselves.
    """
    if supply.kind != INPUTS_PACKAGED or not supply.packaged or not supply.package:
        raise ImageInputError(
            f"nothing to stage: image inputs are {supply.kind!r}, and only "
            f"{INPUTS_PACKAGED!r} inputs have to be assembled into a context")
    dest = Path(dest).resolve()
    for rel in PACKAGED_FILES:
        if (supply.packaged / rel).exists():
            _copy_into(supply.packaged, rel, dest, rel)
    _copy_into(supply.package.parent, supply.package.name, dest, "src/ralphd",
               prune={PACKAGED_DIR_NAME})
    return dest


def _copy_into(src_root: Path, rel: str, dest_root: Path, dest_rel: str,
               prune: frozenset[str] | set[str] = frozenset()) -> None:
    """Copy one input path into the staged context, preserving what is hashed.

    "What is hashed" is the executable bit and the bytes (see `_entry_digest`),
    so those two are what this reproduces; mtimes and ownership are deliberately
    not carried over, since nothing about the tag or the build depends on them.
    """
    files: dict[str, Path] = {}
    _collect(src_root, rel, files, [])
    prefix = f"{rel}/"
    for path_rel, path in files.items():
        tail = path_rel[len(prefix):] if path_rel.startswith(prefix) else ""
        target = dest_root / dest_rel / tail if tail else dest_root / dest_rel
        if tail and any(part in prune for part in tail.split("/")):
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        if os.path.islink(path):
            os.symlink(os.readlink(path), target)
            continue
        with open(path, "rb") as fh, open(target, "wb") as out:
            while chunk := fh.read(_CHUNK):
                out.write(chunk)
        if path.stat().st_mode & 0o111:
            target.chmod(target.stat().st_mode | 0o111)


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


# ------------------------------------------------- deriving from a base (H2)
# The recipe. Kept as one template rather than assembled from pieces so that
# what gets built is readable *here*, in the order docker will run it, and so
# the module's docker-free guard (tests/test_image_hash.py) has one string to
# exempt instead of a dozen.
#
# Five layers, in dependency order, each a no-op when the base already
# provides what it installs -- an "ordinary dev image" is barely touched:
#
#   1. the interpreters and CLIs the engine shells out to (git, curl, jq, rg,
#      ps, python3 + venv), via the base's apt-get, and a loud failure if the
#      base has neither the tools nor apt-get;
#   2. node >= the major container/Dockerfile pins, then pi at its pinned
#      version;
#   3. the static docker client, but only when the base has none and the
#      inherited ARGs pin a version -- `--allow-docker`'s siblings need a
#      client inside the container;
#   4. the engine, from this build context (the ralphd source root), into its
#      own venv so the base's python installation is left exactly as it was;
#   5. the run contract every ralphd image owes the CLI: uid 1000 owning
#      /workspace, /run/ralphd and /config, and `ralphd-engine` as the
#      entrypoint via container/entrypoint.sh.
_DERIVED_TEMPLATE = """\
# Generated by ralphd -- do not edit. This Dockerfile is regenerated from the
# base image and the engine source on every build, and the derived image is
# tagged by the hash of both, so an edit here is thrown away by the next build.
FROM @BASE@

USER root
@ARGS@
ARG RALPHD_NODE_MAJOR=@NODE_MAJOR@

# 1. what the engine shells out to, installed only when the base lacks it
RUN set -eu; \\
    missing=''; \\
    for spec in git:git curl:curl jq:jq rg:ripgrep ps:procps python3:python3; do \\
        bin="${spec%%:*}"; pkg="${spec#*:}"; \\
        command -v "$bin" >/dev/null 2>&1 || missing="$missing $pkg"; \\
    done; \\
    python3 -c 'import venv' >/dev/null 2>&1 || missing="$missing python3-venv"; \\
    if [ -n "$missing" ]; then \\
        if command -v apt-get >/dev/null 2>&1; then \\
            apt-get update; \\
            DEBIAN_FRONTEND=noninteractive apt-get install -y \\
                --no-install-recommends ca-certificates$missing; \\
            rm -rf /var/lib/apt/lists/*; \\
        else \\
            echo "ralphd: this base image is missing$missing and has no" \\
                 "apt-get to install them -- add them to the base image" >&2; \\
            exit 1; \\
        fi; \\
    fi

# 2. node, then pi at the version container/Dockerfile pins
RUN set -eu; \\
    need_node=1; \\
    if command -v node >/dev/null 2>&1; then \\
        major=$(node -p 'process.versions.node.split(".")[0]' 2>/dev/null || echo 0); \\
        if [ "$major" -ge "$RALPHD_NODE_MAJOR" ]; then need_node=0; fi; \\
    fi; \\
    if [ "$need_node" = 1 ]; then \\
        if command -v apt-get >/dev/null 2>&1; then \\
            curl -fsSL "https://deb.nodesource.com/setup_${RALPHD_NODE_MAJOR}.x" | bash -; \\
            apt-get install -y --no-install-recommends nodejs; \\
            rm -rf /var/lib/apt/lists/*; \\
        else \\
            echo "ralphd: this base image has no node >= $RALPHD_NODE_MAJOR" \\
                 "and no apt-get to install one" >&2; \\
            exit 1; \\
        fi; \\
    fi; \\
    npm install -g --registry https://registry.npmjs.org \\
        "@earendil-works/pi-coding-agent@${PI_VERSION}"

# 3. the static docker client, only if the base has none (--allow-docker)
RUN set -eu; \\
    if ! command -v docker >/dev/null 2>&1 && [ -n "${DOCKER_VERSION:-}" ]; then \\
        curl -fsSL "https://download.docker.com/linux/static/stable/$(uname -m)/docker-${DOCKER_VERSION}.tgz" \\
          | tar -xz -C /usr/local/bin --strip-components=1 docker/docker; \\
        docker --version; \\
    fi

# 4. the engine itself, in its own venv (the base's python is left alone)
COPY . /opt/ralphd
RUN set -eu; \\
    python3 -m venv /opt/ralphd-venv; \\
    /opt/ralphd-venv/bin/pip install --no-cache-dir --upgrade pip; \\
    /opt/ralphd-venv/bin/pip install --no-cache-dir /opt/ralphd
ENV PATH=/opt/ralphd-venv/bin:$PATH

# 5. the run contract: uid 1000 owns the mounts, the engine is the entrypoint
RUN set -eu; \\
    if ! getent passwd 1000 >/dev/null 2>&1; then \\
        useradd -m -u 1000 -s /bin/bash agent; \\
    fi; \\
    home=$(getent passwd 1000 | cut -d: -f6 || true); \\
    : "${home:=/home/agent}"; \\
    mkdir -p /workspace /run/ralphd /config "$home"; \\
    chown -R 1000 /workspace /run/ralphd "$home"

USER 1000
WORKDIR /workspace
ENV RALPHD_RUN_DIR=/run/ralphd \\
    RALPHD_CONFIG_DIR=/config \\
    RALPHD_WORKSPACE_DIR=/workspace \\
    PI_OFFLINE=1

EXPOSE 7777
ENTRYPOINT ["/opt/ralphd/container/entrypoint.sh"]
"""


@dataclass(frozen=True)
class DerivedImage:
    """A derived job image: what to build, from where, and under which tag.

    `dockerfile` is the whole generated text (nothing on disk yet -- the
    builder writes it wherever it likes); `root` is the build context, which is
    the ralphd source root because layer 4 installs the engine out of it.
    `tree` is the hash of the image inputs under that root, kept so a caller
    can report incomplete inputs the same way the default path does.
    """

    base: str
    root: Path
    tree: HashedTree
    dockerfile: str
    digest: str
    hash: str

    @property
    def tag(self) -> str:
        return derived_tag(self.hash)


def arg_defaults(dockerfile_text: str) -> dict[str, str]:
    """The `ARG NAME=default` declarations in a Dockerfile, as a mapping."""
    return {m.group(1): m.group(2) for m in ARG_LINE_RE.finditer(dockerfile_text)}


def arg_lines(dockerfile_text: str) -> tuple[str, ...]:
    """Those same declarations, verbatim and in order.

    Copied rather than re-typed: the derived recipe then inherits every pin
    (pi, the docker client) from the one file in the repo that declares them,
    and a version bump there cannot leave a second copy behind.
    """
    return tuple(m.group(0).rstrip() for m in ARG_LINE_RE.finditer(dockerfile_text))


def _check_base(base: str) -> str:
    base = (base or "").strip()
    if not base:
        raise ImageInputError("no base image reference given")
    if not BASE_REF_RE.match(base):
        raise ImageInputError(
            f"unusable base image reference: {base!r} (a reference may only "
            "contain letters, digits and ._:/@- , and is interpolated into a "
            "generated FROM line)")
    return base


def render_derived_dockerfile(base: str, base_dockerfile: str) -> str:
    """Render the derived Dockerfile layering the engine and pi onto `base`.

    `base_dockerfile` is `container/Dockerfile`'s text, read for its `ARG`
    pins and its node major version only -- it is not otherwise used, since
    the default image's own layers (playwright, the full apt set) are the
    default image's business, not a derived one's.
    """
    base = _check_base(base)
    args = arg_defaults(base_dockerfile)
    if PI_PIN not in args or not args[PI_PIN]:
        raise ImageInputError(
            f"{SOURCE_MARKER} declares no ARG {PI_PIN}=<version>, so the "
            "derived image has no pi version to install (an unpinned install "
            "would silently resolve some other version)")
    node = NODE_MAJOR_RE.search(base_dockerfile)
    if not node:
        raise ImageInputError(
            f"{SOURCE_MARKER} names no node major version (expected a "
            "nodesource setup_NN.x URL), so the derived image cannot tell "
            "which node pi needs")
    return _fill(_DERIVED_TEMPLATE, {
        "@BASE@": base,
        "@ARGS@": "\n".join(arg_lines(base_dockerfile)),
        "@NODE_MAJOR@": node.group(1),
    })


def _fill(template: str, subs: dict[str, str]) -> str:
    """Replace each `@MARKER@`, insisting every one of them was actually there.

    A silently-unreplaced marker would produce a Dockerfile that builds
    something other than what was asked for, so this is an assertion, not a
    best effort.
    """
    for marker, value in subs.items():
        if marker not in template:
            raise ImageInputError(f"derived recipe lost its {marker} marker")
        template = template.replace(marker, value)
    return template


def derived_hash(base: str, source_digest: str, dockerfile: str) -> str:
    """The short hash of a derived image: base + source + recipe.

    All three, because all three change what the image contains: a new base
    (`ubuntu:22.04` -> `ubuntu:24.04`), a new engine source, or a new recipe
    (this module's template) each has to produce a tag that does not exist yet.
    A tag is a *cache key*; anything that changes the bytes must change it.
    """
    h = hashlib.sha256()
    h.update(f"{DERIVED_FORMAT}\n".encode())
    h.update(f"base {_check_base(base)}\n".encode())
    h.update(f"source {source_digest}\n".encode())
    h.update(f"recipe {hashlib.sha256(dockerfile.encode()).hexdigest()}\n".encode())
    return h.hexdigest()[:HASH_LENGTH]


def derive(root: Path | str, base: str) -> DerivedImage:
    """Everything needed to build the job image derived from `base`.

    Raises ImageInputError (never returns a half-answer) when the base
    reference is unusable or `container/Dockerfile` is missing/no longer
    declares what the recipe copies out of it.
    """
    root = Path(root).resolve()
    base = _check_base(base)
    marker = root / SOURCE_MARKER
    try:
        base_dockerfile = marker.read_text()
    except OSError as e:
        raise ImageInputError(f"cannot read {marker}: {e}") from e
    dockerfile = render_derived_dockerfile(base, base_dockerfile)
    tree = hash_image_inputs(root)
    short = derived_hash(base, tree.digest, dockerfile)
    return DerivedImage(base=base, root=root, tree=tree, dockerfile=dockerfile,
                        digest=tree.digest, hash=short)


# ------------------------------ a base built from the operator's Dockerfile (H3)
# The third supply point (`--dockerfile <path>`, `dockerfile:` in a template's
# job.yaml or the registry config): the operator's own build recipe. It is
# built with the Dockerfile's *own directory* as the context -- the natural
# reading of "this Dockerfile", and the only one under which a `COPY` line in
# it means what its author meant -- tagged `ralphd-base:<hash>`, and then used
# as a base, so the engine and pi are layered on top by `derive()` above. The
# job image is never the operator's build directly: it has no `ralphd-engine`.
#
# The tag hashes the context by exactly the rules the ralphd source is hashed
# by (`hash_tree`, one implementation), so an unchanged recipe is a cache hit
# and any change to it -- to the Dockerfile, or to a file it copies in -- is a
# new tag and a rebuild. The Dockerfile's own name is in the stream too, so a
# context carrying two recipes cannot serve one under the other's tag.


@dataclass(frozen=True)
class DockerfileBase:
    """An operator-supplied Dockerfile, its build context, and its tag.

    `dockerfile` and `context` are absolute (the context is the Dockerfile's
    directory); `rel` is the file's name inside that context; `tree` is the
    hash of the whole context, kept so a caller can report what it hashed.
    """

    dockerfile: Path
    context: Path
    rel: str
    tree: HashedTree
    digest: str
    hash: str

    @property
    def tag(self) -> str:
        return base_tag(self.hash)


def dockerfile_hash(name: str, context_digest: str) -> str:
    """The short hash of a Dockerfile-built base: its name + its context.

    Both, because both change what the built image contains: the recipe itself
    (whose text is part of the context digest, since it lives in the context)
    and everything the recipe can copy in.
    """
    h = hashlib.sha256()
    h.update(f"{BASE_FORMAT}\n".encode())
    h.update(f"recipe {name}\n".encode())
    h.update(f"context {context_digest}\n".encode())
    return h.hexdigest()[:HASH_LENGTH]


def dockerfile_base(spec: Path | str) -> DockerfileBase:
    """Everything needed to build `spec` into a base image, or ImageInputError.

    Refuses -- rather than guessing, and rather than letting a builder explain
    it later -- a path that does not exist, a directory (the flag names a file;
    the message says which one to name), an unreadable/undecodable file, and a
    file with no `FROM` instruction, which is not a build recipe at all.
    """
    given = Path(spec).expanduser()
    path = given if given.is_absolute() else Path.cwd() / given
    if path.is_dir():
        raise ImageInputError(
            f"{given} is a directory: name the Dockerfile itself, e.g. "
            f"{given / 'Dockerfile'} (its directory becomes the build context)")
    if not path.is_file():
        raise ImageInputError(f"no such file: {given}")
    try:
        text = path.read_text()
    except (OSError, UnicodeDecodeError) as e:
        raise ImageInputError(f"cannot read {given}: {e}") from e
    if not FROM_RE.search(text):
        raise ImageInputError(
            f"{given} declares no FROM instruction, so it is not a Dockerfile "
            "ralphd can build a base image from")
    path = path.resolve()
    context = path.parent
    tree = hash_tree(context, (".",))
    short = dockerfile_hash(path.name, tree.digest)
    return DockerfileBase(dockerfile=path, context=context, rel=path.name,
                          tree=tree, digest=tree.digest, hash=short)
