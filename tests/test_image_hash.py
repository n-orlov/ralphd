"""Content hashing of the job image's inputs (task 032, #20).

Requirement H tags the job image `ralphd:<hash>` and builds only on a cache
miss, so an operator cannot silently run an engine older than the bug they are
watching for (PRD fact 1). Everything downstream -- the build, the tag lookup,
`resume` reproducing a recorded image, `doctor`'s staleness line -- is only as
trustworthy as the hash, so what is under test here is the hash *as a
function*:

* the same tree hashes the same, from any checkout path, in any directory
  listing order, whatever the mtimes;
* editing anything the image installs changes it;
* editing something the image does not install (docs, tests, and above all
  `artifacts/`, which a *running job* writes) does not;
* the excluded directories are not merely absent from the result, they are
  never walked -- asserted through `HashedTree.dirs`, a scandir spy, and an
  unreadable directory inside `.git` that a traversal would trip over;
* the short form is a legal docker tag component.

Nothing here runs docker (task 033 owns that); a guard test keeps the module
that way.
"""

from __future__ import annotations

import ast
import hashlib
import os
import re
import shutil
import time
from pathlib import Path

import pytest

from ralphd.cli import image

REPO = Path(__file__).resolve().parent.parent

# docker reference-spec tag charset -- what `ralphd:<hash>` has to satisfy.
DOCKER_TAG_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9._-]{0,127}$")


def _tree(root: Path) -> Path:
    """A miniature ralphd checkout: the three image inputs plus the noise that
    must stay out of the hash."""
    (root / "container").mkdir(parents=True)
    (root / "container/Dockerfile").write_text("FROM python:3.12-slim\nRUN pip install .\n")
    entry = root / "container/entrypoint.sh"
    entry.write_text("#!/bin/bash\nexec ralphd-engine\n")
    entry.chmod(0o755)
    (root / "src/ralphd/engine").mkdir(parents=True)
    (root / "src/ralphd/cli").mkdir(parents=True)
    (root / "src/ralphd/__init__.py").write_text('__version__ = "0.6.0"\n')
    (root / "src/ralphd/engine/loop.py").write_text("def run_job():\n    pass\n")
    (root / "src/ralphd/cli/main.py").write_text("def main():\n    pass\n")
    (root / "pyproject.toml").write_text('[project]\nname = "ralphd"\n')

    # not inputs, in the ways they actually occur
    (root / ".git/objects/ab").mkdir(parents=True)
    (root / ".git/objects/ab/cdef").write_bytes(b"\x00packed\x00")
    (root / ".git/HEAD").write_text("ref: refs/heads/main\n")
    (root / "artifacts/reports").mkdir(parents=True)
    (root / "artifacts/reports/pricing-anomaly.md").write_text("# anomaly\n")
    (root / "src/ralphd/__pycache__").mkdir()
    (root / "src/ralphd/__pycache__/loop.cpython-312.pyc").write_bytes(b"\x00stale\x00")
    (root / "src/ralphd/engine/loop.pyc").write_bytes(b"\x00stale\x00")
    (root / "tests").mkdir()
    (root / "tests/test_loop.py").write_text("def test_it():\n    pass\n")
    (root / "docs").mkdir()
    (root / "docs/cli.md").write_text("# cli\n")
    (root / ".venv/lib").mkdir(parents=True)
    (root / ".venv/lib/pyvenv.cfg").write_text("home = /usr\n")
    (root / "container/node_modules/x").mkdir(parents=True)
    (root / "container/node_modules/x/index.js").write_text("module.exports = 1\n")
    (root / "container/.DS_Store").write_bytes(b"\x00")
    return root


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    return _tree(tmp_path / "repo")


# --- determinism ----------------------------------------------------------


def test_the_same_tree_hashes_the_same_twice(tree):
    assert image.hash_image_inputs(tree).digest == image.hash_image_inputs(tree).digest


def test_the_hash_does_not_depend_on_the_checkout_path(tmp_path, tree):
    other = tmp_path / "elsewhere" / "deeper" / "ralphd"
    other.parent.mkdir(parents=True)
    shutil.copytree(tree, other, symlinks=True)
    assert image.hash_image_inputs(other).hash == image.hash_image_inputs(tree).hash


def test_the_hash_does_not_depend_on_mtimes(tree):
    before = image.hash_image_inputs(tree).digest
    old = time.time() - 90_000
    for path in (tree / "container/Dockerfile", tree / "src/ralphd/engine/loop.py"):
        os.utime(path, (old, old))
    assert image.hash_image_inputs(tree).digest == before


def test_the_hash_does_not_depend_on_directory_listing_order(monkeypatch, tree):
    """Real filesystems hand entries back in arbitrary order; two identical
    checkouts must still agree, so the stream is sorted rather than walked."""
    before = image.hash_image_inputs(tree).digest
    real = os.scandir

    class _Reversed:
        def __init__(self, path):
            with real(path) as it:
                self._entries = list(reversed(list(it)))

        def __enter__(self):
            return iter(self._entries)

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(image.os, "scandir", _Reversed)
    assert image.hash_image_inputs(tree).digest == before


def test_the_hash_stream_carries_a_format_version(monkeypatch, tree):
    before = image.hash_image_inputs(tree).digest
    monkeypatch.setattr(image, "HASH_FORMAT", "ralphd-image-inputs v2")
    assert image.hash_image_inputs(tree).digest != before


# --- what changes the hash ------------------------------------------------


@pytest.mark.parametrize("rel", [
    "container/Dockerfile",
    "container/entrypoint.sh",
    "src/ralphd/__init__.py",
    "src/ralphd/engine/loop.py",
    "src/ralphd/cli/main.py",
    "pyproject.toml",
])
def test_editing_a_hashed_input_changes_the_hash(tree, rel):
    before = image.hash_image_inputs(tree).hash
    path = tree / rel
    path.write_text(path.read_text() + "# changed\n")
    assert image.hash_image_inputs(tree).hash != before


def test_a_new_source_file_changes_the_hash(tree):
    before = image.hash_image_inputs(tree).digest
    (tree / "src/ralphd/engine/pricing_aws.py").write_text("RATES = {}\n")
    assert image.hash_image_inputs(tree).digest != before


def test_renaming_a_source_file_changes_the_hash(tree):
    """Paths are hashed, not just bytes: moving a module is an image change."""
    before = image.hash_image_inputs(tree).digest
    (tree / "src/ralphd/engine/loop.py").rename(tree / "src/ralphd/engine/loop2.py")
    assert image.hash_image_inputs(tree).digest != before


def test_the_executable_bit_is_part_of_the_hash(tree):
    """container/entrypoint.sh is the ENTRYPOINT; a lost +x is a broken image."""
    before = image.hash_image_inputs(tree).digest
    (tree / "container/entrypoint.sh").chmod(0o644)
    assert image.hash_image_inputs(tree).digest != before


def test_a_symlink_is_hashed_as_its_target_and_never_followed(tree):
    link = tree / "container/shortcut"
    link.symlink_to("../src/ralphd/engine/loop.py")
    with_link = image.hash_image_inputs(tree).digest

    # the target's content is already hashed under its own path; retargeting
    # the LINK still has to change the hash, and following it must not be how
    (tree / "container/shortcut").unlink()
    link.symlink_to("../pyproject.toml")
    assert image.hash_image_inputs(tree).digest != with_link

    # a link into an excluded tree drags nothing in
    (tree / "container/shortcut").unlink()
    link.symlink_to("../.git")
    pointing_at_git = image.hash_image_inputs(tree).digest
    (tree / ".git/HEAD").write_text("ref: refs/heads/other\n")
    assert image.hash_image_inputs(tree).digest == pointing_at_git


def test_a_missing_input_is_recorded_and_hashed(tree):
    full = image.hash_image_inputs(tree)
    assert full.missing == () and full.complete

    (tree / "pyproject.toml").unlink()
    gone = image.hash_image_inputs(tree)
    assert gone.missing == ("pyproject.toml",)
    assert gone.present == ("container", "src/ralphd")
    assert not gone.complete
    assert gone.digest != full.digest

    # "absent" and "present but empty" are different images, so different hashes
    (tree / "pyproject.toml").write_text("")
    empty = image.hash_image_inputs(tree)
    assert empty.complete and empty.digest not in (gone.digest, full.digest)


# --- what does not change the hash ---------------------------------------


@pytest.mark.parametrize("rel", [
    ".git/HEAD",
    ".git/objects/ab/cdef",
    "artifacts/reports/pricing-anomaly.md",
    "src/ralphd/__pycache__/loop.cpython-312.pyc",
    "src/ralphd/engine/loop.pyc",
    "tests/test_loop.py",
    "docs/cli.md",
    ".venv/lib/pyvenv.cfg",
    "container/node_modules/x/index.js",
    "container/.DS_Store",
])
def test_editing_an_excluded_path_does_not_change_the_hash(tree, rel):
    before = image.hash_image_inputs(tree).digest
    (tree / rel).write_bytes(b"completely different content\n")
    assert image.hash_image_inputs(tree).digest == before


def test_a_new_artifact_or_test_file_does_not_change_the_hash(tree):
    """The job under observation writes artifacts/ while it runs; if that moved
    the tag, every run would invalidate its own image."""
    before = image.hash_image_inputs(tree).digest
    (tree / "artifacts/screenshots").mkdir()
    (tree / "artifacts/screenshots/hub.png").write_bytes(b"\x89PNG\r\n")
    (tree / "tests/test_new.py").write_text("def test_new():\n    pass\n")
    assert image.hash_image_inputs(tree).digest == before


def test_only_the_declared_inputs_are_hashed(tree):
    res = image.hash_image_inputs(tree)
    assert image.IMAGE_INPUTS == ("container", "pyproject.toml", "src/ralphd")
    assert set(res.files) == {
        "container/Dockerfile", "container/entrypoint.sh", "pyproject.toml",
        "src/ralphd/__init__.py", "src/ralphd/cli/main.py",
        "src/ralphd/engine/loop.py",
    }
    assert res.file_count == 6
    assert res.bytes == sum((tree / rel).stat().st_size for rel in res.files)


# --- pruning, not filtering ----------------------------------------------


def test_excluded_directories_are_never_traversed(tree):
    res = image.hash_image_inputs(tree)
    walked = set(res.dirs)
    assert "container" in walked and "src/ralphd/engine" in walked
    for pruned in (".git", ".git/objects", "artifacts", "artifacts/reports",
                   "src/ralphd/__pycache__", ".venv",
                   "container/node_modules", "container/node_modules/x"):
        assert pruned not in walked
    # `src` itself is not an input, so nothing above src/ralphd is walked either
    assert "src" not in walked and "." not in walked
    assert res.dirs == tuple(sorted(res.dirs))


def test_scandir_is_never_called_inside_an_excluded_directory(monkeypatch, tree):
    seen: list[str] = []
    real = os.scandir

    def spy(path):
        seen.append(str(path))
        return real(path)

    monkeypatch.setattr(image.os, "scandir", spy)
    image.hash_image_inputs(tree)
    assert seen, "the walker did not scan anything"
    for path in seen:
        rel = Path(path).relative_to(tree).as_posix()
        assert not any(part in image.EXCLUDED_DIR_NAMES for part in rel.split("/")), path


def test_an_unreadable_excluded_directory_does_not_break_hashing(tree):
    """The proof that pruning happens *before* the syscall: a directory the
    process cannot open at all, inside every excluded tree."""
    locked = []
    for parent in (".git", "artifacts", "src/ralphd/__pycache__"):
        d = tree / parent / "locked"
        d.mkdir()
        (d / "x").write_text("x\n")
        d.chmod(0o000)
        locked.append(d)
    try:
        res = image.hash_image_inputs(tree)
        assert res.file_count == 6
        assert image.HASH_RE.match(res.hash)
    finally:
        for d in locked:
            d.chmod(0o755)


def test_exclusion_predicates_are_the_one_place_the_rules_live(tree):
    assert image.is_excluded_dir(".git") and image.is_excluded_dir("__pycache__")
    assert image.is_excluded_dir("artifacts") and image.is_excluded_dir("ralphd.egg-info")
    assert not image.is_excluded_dir("container") and not image.is_excluded_dir("engine")
    assert image.is_excluded_file("loop.pyc") and image.is_excluded_file("main.py~")
    assert image.is_excluded_file(".DS_Store") and not image.is_excluded_file("loop.py")


# --- the short form ------------------------------------------------------


def test_the_short_hash_is_a_legal_docker_tag_component(tree):
    res = image.hash_image_inputs(tree)
    assert len(res.hash) == image.HASH_LENGTH == 12
    assert image.HASH_RE.match(res.hash)
    assert DOCKER_TAG_RE.match(res.hash)
    assert DOCKER_TAG_RE.match(f"0.6.0-{res.hash}")
    assert res.digest.startswith(res.hash)
    assert len(res.digest) == len(hashlib.sha256(b"").hexdigest())


# --- the real repo ------------------------------------------------------


def test_source_root_finds_this_checkout(tree, tmp_path):
    assert image.source_root() == REPO
    assert (REPO / image.SOURCE_MARKER).is_file()
    assert image.source_root(tree) == tree
    assert image.source_root(tmp_path / "nothing-here") is None
    # a wheel/pipx install has no container/ -- absence is the answer (task 038)
    assert image.source_root(REPO / "src") is None


def test_hashing_this_repo_is_cheap_and_stable():
    started = time.monotonic()
    res = image.hash_image_inputs(REPO)
    elapsed = time.monotonic() - started
    assert res.complete and image.HASH_RE.match(res.hash)
    assert res.digest == image.hash_image_inputs(REPO).digest
    assert elapsed < 5.0, f"hashing the repo took {elapsed:.1f}s"
    assert 20 < res.file_count < 500, res.file_count
    for rel in res.files:
        assert rel.split("/")[0] in ("container", "pyproject.toml", "src")
    assert not any(d.startswith((".git", "artifacts", "tests", "docs")) for d in res.dirs)
    assert "container/Dockerfile" in res.files
    assert "src/ralphd/engine/loop.py" in res.files


def test_hash_tree_can_hash_an_arbitrary_context(tree):
    """H2/H3 hash a user-supplied build context by these same rules; the
    generic entry point exists so there is never a second walker."""
    only_container = image.hash_tree(tree, ("container",))
    assert set(only_container.files) == {"container/Dockerfile", "container/entrypoint.sh"}
    assert only_container.digest != image.hash_image_inputs(tree).digest
    assert image.hash_tree(tree, ("container",)).digest == only_container.digest
    # requesting nothing is not the same as requesting something absent
    assert image.hash_tree(tree, ()).digest != image.hash_tree(tree, ("nope",)).digest


def test_a_whole_directory_context_prunes_version_control_and_run_output(tree):
    """H3 lets an operator point at a Dockerfile whose context directory may be
    a checkout root -- `.git` and `artifacts/` must be pruned there too, which
    is only visible when the root itself is the requested input."""
    whole = image.hash_tree(tree, (".",))
    assert "." in whole.dirs and "container" in whole.dirs and "src" in whole.dirs
    assert not any(d.split("/")[0] in (".git", "artifacts", ".venv") for d in whole.dirs)
    assert "tests/test_loop.py" in whole.files  # not an image input, but IS context
    assert not any(f.startswith((".git/", "artifacts/", ".venv/")) for f in whole.files)
    before = whole.digest
    (tree / ".git/HEAD").write_text("ref: refs/heads/other\n")
    (tree / "artifacts/reports/pricing-anomaly.md").write_text("# rewritten\n")
    assert image.hash_tree(tree, (".",)).digest == before


def test_the_hashing_module_does_not_touch_docker():
    """Deciding what to hash must stay separable from running a build: the
    module imports nothing that can spawn a process and names no command.
    (Prose may -- and does -- discuss docker; this reads the AST, not the
    docstring.)"""
    module = ast.parse((REPO / "src/ralphd/cli/image.py").read_text())
    imported = {
        alias.name.split(".")[0]
        for node in ast.walk(module)
        if isinstance(node, ast.Import)
        for alias in node.names
    } | {
        (node.module or "").split(".")[0]
        for node in ast.walk(module)
        if isinstance(node, ast.ImportFrom)
    }
    assert not imported & {"subprocess", "shutil", "asyncio", "docker"}, imported
    docstrings = {ast.get_docstring(n, clean=False) for n in ast.walk(module)
                  if isinstance(n, ast.Module | ast.FunctionDef | ast.ClassDef)}
    for node in ast.walk(module):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) \
                and node.value not in docstrings and "docker" in node.value.lower():
            assert "Dockerfile" in node.value, node.value
