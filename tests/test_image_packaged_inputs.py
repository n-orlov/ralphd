"""A wheel/pipx install builds its job image from package data (task 038, #20 H4).

Requirement H4 asks for one packaging decision, taken and written down: a
`pipx install ralphd` has no `container/` directory next to it, so before this
task `start` could hash nothing, build nothing, and fell back to the legacy
`ralphd:dev` tag -- the very "am I running the engine I think I am?" ignorance
requirement H exists to end. The PRD offered two fixes: ship the Dockerfile as
package data, or fall back to a pinned *published* tag. v0.6 publishes no image
(an explicit non-goal), so a pinned tag would name a reference nobody can pull.
The decision is therefore package data, and this module is what makes the
decision real rather than prose in `docs/architecture.md`:

* the wheel ships `PACKAGED_FILES` under `ralphd/_image/` -- asserted against
  `pyproject.toml`'s own `force-include` mapping, *and* against a real wheel
  built by the real backend, so the mapping cannot rot;
* an install with package data and no checkout is `INPUTS_PACKAGED`, a checkout
  wins over package data, and an install with neither is `INPUTS_NONE` --
  absence stays an answer;
* the staged context is laid out exactly like a checkout, so it hashes to **the
  same `ralphd:<hash>`** a checkout of that version builds: a pipx install and a
  checkout share the image cache instead of each keeping its own;
* the packaged path is never silent (`IMAGE_PACKAGED_INPUTS_NOTICE` on stderr
  from `start`, `inputs` + a line in `doctor`), and the "neither" fallback still
  announces itself.

The `doctor` tier runs a *real* wheel-shaped install: `ralphd` copied into a
throwaway `site-packages` with its package data, imported over `PYTHONPATH`, so
`source_root()` genuinely finds no checkout and nothing is monkeypatched.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tomllib
import zipfile
from pathlib import Path

import pytest

from ralphd.cli import image
from ralphd.cli import main as cli

REPO = Path(__file__).parent.parent
STUB_DOCKER = REPO / "tests" / "stub-docker" / "docker"

# What this checkout's inputs hash to -- computed, never transcribed, so the
# central claim ("the staged wheel context hashes the same") cannot rot.
SOURCE_HASH = image.hash_image_inputs(REPO).hash
SOURCE_TAG = image.image_tag(SOURCE_HASH)


def _force_include() -> dict[str, str]:
    """`pyproject.toml`'s wheel `force-include` mapping (the packaging half)."""
    data = tomllib.loads((REPO / "pyproject.toml").read_text())
    wheel = data["tool"]["hatch"]["build"]["targets"]["wheel"]
    return wheel["force-include"]


def _wheel_install(dest: Path, *, packaged: bool = True) -> Path:
    """A wheel install of THIS checkout under `dest`, returning the package dir.

    Built with `shutil.copytree` and `pyproject.toml`'s own mapping -- not with
    `image`'s staging code, which is what is under test -- so "the staged
    context equals a checkout" is an assertion about two independent copies.
    `packaged=False` is the pre-038 wheel: an install with no package data.
    """
    pkg = dest / "site-packages" / "ralphd"
    shutil.copytree(REPO / "src/ralphd", pkg,
                    ignore=shutil.ignore_patterns("__pycache__"))
    if not packaged:
        return pkg
    for src, target in _force_include().items():
        assert target.startswith("ralphd/"), target
        out = pkg.parent / target
        out.parent.mkdir(parents=True, exist_ok=True)
        if (REPO / src).is_dir():
            shutil.copytree(REPO / src, out)
        else:
            shutil.copy2(REPO / src, out)
    return pkg


@pytest.fixture
def wheel_pkg(tmp_path):
    return _wheel_install(tmp_path / "install")


# ------------------------------------------------ the packaging declaration
def test_the_wheel_mapping_ships_exactly_the_packaged_files():
    """`pyproject.toml` and `cli/image.py` must agree about what a wheel
    carries: the code stages `PACKAGED_FILES`, and only the mapping puts them
    in the wheel, so a rename on either side has to fail here."""
    mapping = _force_include()
    assert mapping == {name: f"ralphd/{image.PACKAGED_DIR_NAME}/{name}"
                       for name in image.PACKAGED_FILES}
    # everything insisted on at runtime is actually shipped, and is a real file
    for rel in image.PACKAGED_REQUIRED:
        top = rel.split("/")[0]
        assert top in image.PACKAGED_FILES, rel
        assert (REPO / rel).is_file(), rel


def test_the_packaged_files_are_the_recipe_and_what_pip_reads():
    """The Dockerfile alone is not enough: the default image's build context IS
    the install recipe (`COPY . /opt/ralphd` + `pip install /opt/ralphd`), so the
    wheel has to carry `pyproject.toml` and the metadata files it points at."""
    project = tomllib.loads((REPO / "pyproject.toml").read_text())["project"]
    assert project["readme"] in image.PACKAGED_FILES
    assert "pyproject.toml" in image.PACKAGED_FILES
    assert image.SOURCE_MARKER.split("/")[0] in image.PACKAGED_FILES


# ------------------------------------------------------- where the inputs are
def test_a_checkout_wins_over_package_data(wheel_pkg):
    """In an editable/checkout install the tree is the live source; hashing
    package data instead would tag the files as they were at install time."""
    supply = image.find_inputs(pkg=wheel_pkg)
    assert supply.kind == image.INPUTS_CHECKOUT
    assert supply.root == REPO and supply.where == REPO
    assert supply.buildable and supply.packaged is None


def test_a_wheel_install_finds_its_own_packaged_inputs(tmp_path, wheel_pkg):
    supply = image.find_inputs(root=tmp_path / "not-a-checkout", pkg=wheel_pkg)
    assert supply.kind == image.INPUTS_PACKAGED
    assert supply.packaged == wheel_pkg / image.PACKAGED_DIR_NAME
    assert supply.package == wheel_pkg and supply.root is None
    assert supply.buildable and supply.where == supply.packaged
    # the inputs are there under the layout the marker names
    assert (supply.packaged / image.SOURCE_MARKER).is_file()


def test_an_install_with_neither_is_the_honest_none(tmp_path):
    """The pre-038 wheel: no checkout, no package data. Not a guessed hash."""
    pkg = _wheel_install(tmp_path / "install", packaged=False)
    assert image.packaged_inputs(pkg) is None
    supply = image.find_inputs(root=tmp_path / "not-a-checkout", pkg=pkg)
    assert supply.kind == image.INPUTS_NONE
    assert not supply.buildable
    assert supply.root is None and supply.packaged is None
    assert supply.where is None


@pytest.mark.parametrize("victim", image.PACKAGED_REQUIRED)
def test_package_data_missing_the_recipe_is_not_a_packaged_supply(tmp_path, victim):
    """Half a recipe is not a recipe: a wheel that lost `container/Dockerfile`
    or `pyproject.toml` is described by what it has, not by what it should."""
    pkg = _wheel_install(tmp_path / "install")
    (pkg / image.PACKAGED_DIR_NAME / victim).unlink()
    assert image.packaged_inputs(pkg) is None
    assert image.find_inputs(root=tmp_path / "nope", pkg=pkg).kind \
        == image.INPUTS_NONE


def test_a_named_tree_is_never_silently_replaced_by_package_data(tmp_path,
                                                                monkeypatch):
    """`root=` means "this tree or nothing": a caller naming a tree is not
    asking to be handed some other install's inputs. Only the default path
    (nobody named one) consults this install's package data."""
    pkg = _wheel_install(tmp_path / "install")
    monkeypatch.setattr(image, "package_dir", lambda: pkg)
    assert image.find_inputs(root=tmp_path / "not-a-checkout").kind \
        == image.INPUTS_NONE
    assert image.find_inputs(root=REPO).kind == image.INPUTS_CHECKOUT
    # ... and with nothing named, this process is still a checkout install, so
    # the package data is not reached for that reason either
    assert image.find_inputs().kind == image.INPUTS_CHECKOUT


def test_package_dir_is_this_installed_package():
    assert image.package_dir() == Path(image.__file__).resolve().parent.parent
    assert image.package_dir().name == "ralphd"


# ------------------------------------------------------------ staging the copy
def test_staging_reproduces_a_checkout_and_therefore_its_tag(tmp_path, wheel_pkg):
    """The whole point of the decision: a pipx install builds the SAME tag."""
    supply = image.find_inputs(root=tmp_path / "nope", pkg=wheel_pkg)
    staged = image.stage_inputs(supply, tmp_path / "context")
    tree = image.hash_image_inputs(staged)
    checkout = image.hash_image_inputs(REPO)
    assert tree.hash == checkout.hash == SOURCE_HASH
    assert tree.digest == checkout.digest
    assert tree.files == checkout.files and tree.complete
    assert image.image_tag(tree.hash) == SOURCE_TAG


def test_the_staged_context_is_an_installable_recipe(tmp_path, wheel_pkg):
    """`pip install <context>` inside the image reads more than the inputs: the
    readme and license `pyproject.toml` declares have to be there too, or the
    build fails after the image is otherwise complete."""
    supply = image.find_inputs(root=tmp_path / "nope", pkg=wheel_pkg)
    staged = image.stage_inputs(supply, tmp_path / "context")
    for rel in image.PACKAGED_FILES:
        assert (staged / rel).exists(), rel
    assert (staged / "src/ralphd/engine/main.py").is_file()
    assert (staged / "container/entrypoint.sh").stat().st_mode & 0o111
    # the recipe the container runs, and the metadata `pip install` insists on
    assert (staged / image.SOURCE_MARKER).is_file()
    project = tomllib.loads((staged / "pyproject.toml").read_text())["project"]
    assert (staged / project["readme"]).is_file()


def test_staging_leaves_the_package_data_marker_out_of_the_source(tmp_path,
                                                                 wheel_pkg):
    """A checkout has no `_image/` inside `src/ralphd`; copying one in would
    change the hash and nest the inputs inside themselves."""
    supply = image.find_inputs(root=tmp_path / "nope", pkg=wheel_pkg)
    staged = image.stage_inputs(supply, tmp_path / "context")
    assert (wheel_pkg / image.PACKAGED_DIR_NAME).is_dir()
    assert not (staged / "src/ralphd" / image.PACKAGED_DIR_NAME).exists()
    assert not any(image.PACKAGED_DIR_NAME in f
                   for f in image.hash_image_inputs(staged).files)


def test_staging_carries_only_what_is_hashed(tmp_path, wheel_pkg):
    """The copy walks by the rules that decide what is hashed: caches pruned,
    symlinks recreated rather than followed."""
    (wheel_pkg / "__pycache__").mkdir(exist_ok=True)
    (wheel_pkg / "__pycache__/state.pyc").write_bytes(b"\x00")
    (wheel_pkg / "engine/link.py").symlink_to("state.py")
    supply = image.find_inputs(root=tmp_path / "nope", pkg=wheel_pkg)
    staged = image.stage_inputs(supply, tmp_path / "context")
    assert not (staged / "src/ralphd/__pycache__").exists()
    link = staged / "src/ralphd/engine/link.py"
    assert link.is_symlink() and os.readlink(link) == "state.py"


@pytest.mark.parametrize("kind", [image.INPUTS_CHECKOUT, image.INPUTS_NONE])
def test_staging_refuses_a_supply_that_needs_no_staging(tmp_path, kind):
    supply = image.InputsSupply(kind, root=REPO if kind ==
                                image.INPUTS_CHECKOUT else None)
    with pytest.raises(image.ImageInputError) as e:
        image.stage_inputs(supply, tmp_path / "context")
    assert kind in str(e.value) and image.INPUTS_PACKAGED in str(e.value)


# --------------------------------------------------- resolving and building
@pytest.fixture
def stub_daemon(tmp_path, monkeypatch):
    """`main`'s docker pointed at the recording stub, cache empty (nothing
    exists until something builds it) -- test_image_build.py's fixture."""
    log = tmp_path / "argv.jsonl"
    monkeypatch.setattr(cli, "DOCKER", str(STUB_DOCKER))
    monkeypatch.setenv("STUB_DOCKER_LOG", str(log))
    monkeypatch.setenv("STUB_DOCKER_IMAGES", "")
    monkeypatch.setenv("STUB_DOCKER_IMAGE_FILE", str(tmp_path / "images.txt"))
    monkeypatch.delenv("STUB_DOCKER_BUILD_FAIL", raising=False)

    def recorded(verb: str) -> list[list[str]]:
        if not log.exists():
            return []
        argvs = [json.loads(ln) for ln in log.read_text().splitlines()]
        if verb == "image inspect":
            return [a for a in argvs if a[:2] == ["image", "inspect"]]
        return [a for a in argvs if a[:1] == [verb]]

    return recorded


@pytest.fixture
def wheel_install(tmp_path, monkeypatch, wheel_pkg):
    """This process, pretending to BE a wheel install: no checkout next to it,
    package data inside it. Two monkeypatches and no more -- the black-box
    `doctor` tier below runs the same layout for real, over PYTHONPATH."""
    monkeypatch.setattr(image, "package_dir", lambda: wheel_pkg)
    monkeypatch.setattr(image, "source_root", lambda start=None: None)
    return wheel_pkg


def test_a_wheel_install_builds_the_checkouts_tag_from_a_staged_context(
        wheel_install, stub_daemon, capsys):
    res = cli.resolve_job_image(None)
    assert res == {"image": SOURCE_TAG, "imageSource": cli.IMAGE_SOURCE_BUILT,
                   "imageHash": SOURCE_HASH, "imageBase": None,
                   "imageDockerfile": None}
    (argv,) = stub_daemon("build")
    assert argv[:3] == ["build", "-t", SOURCE_TAG]
    context = Path(argv[5])
    assert argv[4] == str(context / image.SOURCE_MARKER)
    # the context was staged, not the install directory, and it is gone again:
    # derived data, never litter for the next build to distrust
    assert context != REPO and not str(context).startswith(str(wheel_install))
    assert not context.exists()
    err = capsys.readouterr().err
    assert cli.IMAGE_PACKAGED_INPUTS_NOTICE.format(
        where=wheel_install / image.PACKAGED_DIR_NAME) in err
    assert SOURCE_TAG in err


def test_the_second_start_on_a_wheel_install_is_a_tag_lookup(
        wheel_install, stub_daemon):
    assert cli.resolve_job_image(None)["imageSource"] == cli.IMAGE_SOURCE_BUILT
    res = cli.resolve_job_image(None)
    assert res["imageSource"] == cli.IMAGE_SOURCE_CACHED
    assert res["image"] == SOURCE_TAG and res["imageHash"] == SOURCE_HASH
    assert len(stub_daemon("build")) == 1


def test_a_pin_on_a_wheel_install_still_hashes_and_builds_nothing(
        wheel_install, stub_daemon, capsys):
    res = cli.resolve_job_image("ghcr.io/x/ralphd:v1")
    assert res["imageSource"] == cli.IMAGE_SOURCE_PINNED
    assert stub_daemon("build") == [] and stub_daemon("image inspect") == []
    assert "package data" not in capsys.readouterr().err


def test_a_wheel_install_can_derive_from_a_base_image(wheel_install, stub_daemon):
    """Requirement H2 off package data: the derived tag is the one a checkout
    would produce, because the staged context is the same inputs."""
    expected = image.derive(REPO, "ubuntu:24.04").tag
    res = cli.resolve_job_image(None, base="ubuntu:24.04")
    assert res["image"] == expected
    assert res["imageSource"] == cli.IMAGE_SOURCE_BUILT
    assert res["imageBase"] == "ubuntu:24.04"
    (argv,) = stub_daemon("build")
    assert argv[:3] == ["build", "-t", expected]


def test_an_install_with_neither_falls_back_observably(tmp_path, monkeypatch,
                                                       stub_daemon, capsys):
    """The task's other half: when there is nothing to hash *at all*, the
    fallback is loud. Nothing is built, and the reference says so."""
    pkg = _wheel_install(tmp_path / "install", packaged=False)
    monkeypatch.setattr(image, "package_dir", lambda: pkg)
    monkeypatch.setattr(image, "source_root", lambda start=None: None)
    res = cli.resolve_job_image(None)
    assert res == {"image": cli.DEFAULT_IMAGE,
                   "imageSource": cli.IMAGE_SOURCE_UNHASHABLE,
                   "imageHash": None, "imageBase": None,
                   "imageDockerfile": None}
    err = capsys.readouterr().err
    assert cli.IMAGE_NO_SOURCE_NOTICE in err
    assert image.PACKAGED_DIR_NAME in err and "--image" in err
    assert stub_daemon("build") == [] and stub_daemon("image inspect") == []


def test_deriving_with_nothing_to_hash_is_refused_not_defaulted(
        tmp_path, monkeypatch, stub_daemon):
    pkg = _wheel_install(tmp_path / "install", packaged=False)
    monkeypatch.setattr(image, "package_dir", lambda: pkg)
    monkeypatch.setattr(image, "source_root", lambda start=None: None)
    with pytest.raises(SystemExit) as e:
        cli.resolve_job_image(None, base="ubuntu:24.04")
    assert e.value.code == 1
    assert stub_daemon("build") == []


def test_current_source_hash_reports_the_package_data_and_the_same_hash(
        wheel_install):
    root, digest = cli.current_source_hash()
    assert root == wheel_install / image.PACKAGED_DIR_NAME
    assert digest == SOURCE_HASH
    # the staged copy is not left behind for a later reader to trip over
    assert not (wheel_install / image.PACKAGED_DIR_NAME / "src").exists()


# -------------------------------------------- black-box: a real wheel layout
def _wheel_ralphctl(pkg: Path, registry: Path, *argv: str,
                    env: dict | None = None) -> subprocess.CompletedProcess:
    """Run ralphctl *as* the wheel install at `pkg` (PYTHONPATH, no patching).

    `source_root()` resolves from the module's own path, so importing ralphd out
    of a throwaway `site-packages` is a faithful pipx install: there genuinely
    is no checkout above it.
    """
    full = {k: v for k, v in os.environ.items() if k != "RALPHD_IMAGE"}
    full.update({"PYTHONPATH": str(pkg.parent),
                 "RALPHD_DOCKER": str(STUB_DOCKER),
                 "RALPHD_REGISTRY": str(registry),
                 **(env or {})})
    return subprocess.run(
        [sys.executable, "-c",
         "import sys; from ralphd.cli.main import main; sys.exit(main())",
         *argv],
        env=full, capture_output=True, text=True, timeout=120)


def test_a_real_wheel_layout_has_no_checkout_above_it(tmp_path, wheel_pkg):
    res = _wheel_ralphctl(wheel_pkg, tmp_path / "registry", "--version")
    assert res.returncode == 0, res.stderr
    probe = ("import json; from ralphd.cli import image; "
             "s = image.find_inputs(); "
             "print(json.dumps([s.kind, str(s.where), "
             "image.hash_image_inputs(image.stage_inputs(s, "
             "__import__('tempfile').mkdtemp())).hash]))")
    out = subprocess.run([sys.executable, "-c", probe],
                         env={**os.environ, "PYTHONPATH": str(wheel_pkg.parent)},
                         capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    kind, where, digest = json.loads(out.stdout)
    assert kind == image.INPUTS_PACKAGED
    assert where == str(wheel_pkg / image.PACKAGED_DIR_NAME)
    assert digest == SOURCE_HASH


def test_doctor_on_a_wheel_install_names_the_packaged_inputs(tmp_path, wheel_pkg):
    reg = tmp_path / "registry"
    (reg / "runs").mkdir(parents=True)
    res = _wheel_ralphctl(wheel_pkg, reg, "--json", "doctor",
                          env={"STUB_DOCKER_IMAGES": SOURCE_TAG})
    doc = json.loads(res.stdout)
    v = doc["imageStaleness"]
    assert v["inputs"] == image.INPUTS_PACKAGED
    assert v["sourceRoot"] == str(wheel_pkg / image.PACKAGED_DIR_NAME)
    # ... and because the staged context is a checkout's, the verdict is real:
    # a wheel install can say "fresh", where before v0.6 it could only shrug
    assert v["sourceHash"] == SOURCE_HASH and v["image"] == SOURCE_TAG
    assert v["staleness"] == cli.IMAGE_STALENESS_FRESH
    text = _wheel_ralphctl(wheel_pkg, reg, "doctor",
                           env={"STUB_DOCKER_IMAGES": SOURCE_TAG}).stdout
    assert cli.IMAGE_PACKAGED_INPUTS_NOTICE.format(
        where=wheel_pkg / image.PACKAGED_DIR_NAME) in text


def test_doctor_on_a_checkout_says_so(tmp_path):
    """The other value of the field: this repo's own install is a checkout."""
    reg = tmp_path / "registry"
    (reg / "runs").mkdir(parents=True)
    res = subprocess.run(
        [str(Path(sys.executable).parent / "ralphctl"), "--json", "doctor"],
        env={**{k: v for k, v in os.environ.items() if k != "RALPHD_IMAGE"},
             "RALPHD_DOCKER": str(STUB_DOCKER), "RALPHD_REGISTRY": str(reg),
             "STUB_DOCKER_IMAGES": SOURCE_TAG},
        capture_output=True, text=True, timeout=120)
    v = json.loads(res.stdout)["imageStaleness"]
    assert v["inputs"] == image.INPUTS_CHECKOUT
    assert v["sourceRoot"] == str(REPO)
    assert image.PACKAGED_DIR_NAME not in res.stdout


# ------------------------------------------------------- the real wheel build
def test_the_real_wheel_ships_the_image_inputs(tmp_path):
    """The decision is only true if the *built artefact* carries the inputs, so
    this builds the actual wheel with the actual backend and reads the zip.

    Skips -- loudly, saying which package is missing -- when the build backend
    is not installed, which is the one case where the claim cannot be checked
    here rather than a case where it holds.
    """
    for mod in ("hatchling", "build"):
        pytest.importorskip(
            mod, reason=f"{mod} is not installed, so the real wheel cannot be "
                        "built here to check its package data (it is in the "
                        "dev extra: pip install -e '.[dev]')")
    out = tmp_path / "dist"
    res = subprocess.run([sys.executable, "-m", "build", "--wheel",
                          "--no-isolation", "-o", str(out), str(REPO)],
                         capture_output=True, text=True, timeout=300)
    assert res.returncode == 0, res.stdout + res.stderr
    (wheel,) = out.glob("*.whl")
    with zipfile.ZipFile(wheel) as z:
        names = set(z.namelist())
        for name in image.PACKAGED_FILES:
            member = f"ralphd/{image.PACKAGED_DIR_NAME}/{name}"
            assert member in names or any(n.startswith(member + "/")
                                          for n in names), name
        entry = z.getinfo(f"ralphd/{image.PACKAGED_DIR_NAME}/container/entrypoint.sh")
        # the image's ENTRYPOINT has to stay executable through the wheel, and
        # the executable bit is part of the hash: losing it would both break the
        # container and move the tag
        assert entry.external_attr >> 16 & 0o111, oct(entry.external_attr >> 16)
        # `zipfile.extractall` drops permissions; `pip install` does not, so the
        # mode recorded in the archive is reapplied here -- otherwise this test
        # would measure zipfile's behaviour instead of the wheel's contents.
        target = tmp_path / "site-packages"
        for info in z.infolist():
            z.extract(info, target)
            mode = info.external_attr >> 16 & 0o777
            if mode and not info.is_dir():
                (target / info.filename).chmod(mode)
    pkg = tmp_path / "site-packages" / "ralphd"
    supply = image.find_inputs(root=tmp_path / "nope", pkg=pkg)
    assert supply.kind == image.INPUTS_PACKAGED
    for member in image.PACKAGED_FILES:
        (supply.packaged / member).exists()
    staged = image.stage_inputs(supply, tmp_path / "context")
    assert image.hash_image_inputs(staged).hash == SOURCE_HASH
