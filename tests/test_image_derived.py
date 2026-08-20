"""A user-supplied image is a BASE, and the job image is derived from it
(task 034, #20 H2).

Task 033 built the default image: nobody selected one, so `ralphd:<source
hash>` is hashed, looked up and built on a miss. This module covers the other
half of requirement H's promise -- "the user's image only has to carry the
toolchain their repo needs" -- which means ralphd must *never* run an
operator's image as the job image (it has no `ralphd-engine` in it) but layer
the engine and pi onto it and run the result:

* the derived tag `ralphd-derived:<hash>` moves when the base moves, when the
  engine source moves, and when the generated recipe itself moves -- all three
  change what the image contains, so all three have to change the cache key;
* the base is never run and never tagged: the only image `docker run` ever sees
  is the derived one;
* a derived tag that already exists is a pure tag lookup, exactly like the
  default image's;
* the generated Dockerfile really does install pi (at the version
  `container/Dockerfile` pins -- copied, never restated) and the engine from
  the source root, and hands over to `container/entrypoint.sh`;
* contradictory or unusable input is refused loudly (exit 2), and a failed
  derived build aborts `start` before any run state exists (H4).

Three tiers, all fast: unit calls into `cli/image.py` (which is docker-free by
construction -- see test_image_hash.py's AST guard), in-process
`main.resolve_job_image()` over a miniature source checkout, and black-box
`ralphctl start --base-image` over the recording stub docker.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from test_image_build import build_lines, ctl, repo_tag, stub_daemon

from ralphd.cli import image, main

__all__ = ["ctl", "stub_daemon"]

REPO = Path(__file__).parent.parent
BASE = "ubuntu:24.04"
OTHER_BASE = "ubuntu:22.04"


def repo_dockerfile() -> str:
    return (REPO / "container/Dockerfile").read_text()


def pinned_pi_version() -> str:
    """The pi version `container/Dockerfile` pins -- the repo's single source."""
    return image.arg_defaults(repo_dockerfile())["PI_VERSION"]


def derived_tag_for(root: Path, base: str) -> str:
    return image.derive(root, base).tag


@pytest.fixture
def checkout(tmp_path):
    """A miniature ralphd source root whose `container/Dockerfile` declares the
    pins the derived recipe copies out of it (test_image_build's `tree` is
    deliberately more minimal -- it only has to be hashable)."""
    root = tmp_path / "checkout"
    (root / "container").mkdir(parents=True)
    (root / "container/Dockerfile").write_text(
        "FROM python:3.12-slim-bookworm\n"
        "ARG PI_VERSION=0.84.1\n"
        "RUN curl -fsSL https://deb.nodesource.com/setup_22.x | bash -\n"
        "ARG DOCKER_VERSION=29.7.2\n")
    (root / "container/entrypoint.sh").write_text(
        "#!/bin/bash\nexec ralphd-engine\n")
    (root / "src/ralphd").mkdir(parents=True)
    (root / "src/ralphd/__init__.py").write_text('__version__ = "0.6.0"\n')
    (root / "pyproject.toml").write_text('[project]\nname = "ralphd"\n')
    return root


# --- the tag is a function of base, source and recipe ---------------------


def test_the_derived_tag_depends_on_the_base(checkout):
    """Two bases, one source: two images, because they contain different
    things. A shared tag would serve `ubuntu:22.04`'s image to a job that
    asked for `24.04`."""
    one = image.derive(checkout, BASE)
    two = image.derive(checkout, OTHER_BASE)
    assert one.hash != two.hash
    assert one.tag != two.tag
    assert (one.base, two.base) == (BASE, OTHER_BASE)
    # ... and the same base twice is the same tag (a cache key, not a nonce)
    assert image.derive(checkout, BASE).tag == one.tag


def test_the_derived_tag_depends_on_the_engine_source(checkout):
    """The point of hashing at all (#20): a job must not run a stale engine
    just because its base image did not change."""
    before = derived_tag_for(checkout, BASE)
    (checkout / "src/ralphd/__init__.py").write_text('__version__ = "0.6.1"\n')
    after = derived_tag_for(checkout, BASE)
    assert after != before
    # the source digest is carried on the result, so a caller can say *why*
    assert image.derive(checkout, BASE).digest == image.hash_image_inputs(checkout).digest


def test_the_derived_tag_depends_on_the_recipe(checkout):
    """A change to the generated Dockerfile changes the bytes of the image, so
    it has to change the tag too -- otherwise an old derived image is reused
    under the new ralphd."""
    derived = image.derive(checkout, BASE)
    edited = image.derived_hash(BASE, derived.digest,
                               derived.dockerfile + "\nRUN echo later\n")
    assert edited != derived.hash
    assert image.derived_hash(BASE, derived.digest, derived.dockerfile) \
        == derived.hash


def test_an_excluded_path_does_not_move_the_derived_tag(checkout):
    """Same exclusions as the default image: `artifacts/` is written by a
    *running job*, so hashing it would make the tag depend on the run's own
    output."""
    before = derived_tag_for(checkout, BASE)
    (checkout / "artifacts").mkdir()
    (checkout / "artifacts/report.md").write_text("# written by the job\n")
    (checkout / "docs").mkdir()
    (checkout / "docs/cli.md").write_text("# typo fixed\n")
    assert derived_tag_for(checkout, BASE) == before


def test_the_derived_tag_is_its_own_repository_and_a_legal_docker_tag(checkout):
    """`ralphd:<hash>` means "the default image, hash == source hash"; a
    derived hash is not comparable to a source hash, so it lives elsewhere and
    `tag_hash`/`derived_tag_hash` refuse each other's references (task 037
    must not call every derived image stale)."""
    derived = image.derive(checkout, BASE)
    repo, _, tag = derived.tag.partition(":")
    assert repo == image.DERIVED_REPO == "ralphd-derived"
    assert image.HASH_RE.match(tag)
    assert image.derived_tag_hash(derived.tag) == derived.hash
    assert image.tag_hash(derived.tag) is None
    assert image.derived_tag_hash(image.image_tag(derived.hash)) is None
    assert derived.hash != image.hash_image_inputs(checkout).hash


# --- the recipe: engine + pi on top of the base ---------------------------


def test_the_generated_dockerfile_layers_the_engine_and_pi_onto_the_base(checkout):
    text = image.derive(checkout, BASE).dockerfile
    pinned = image.arg_defaults((checkout / "container/Dockerfile").read_text())
    assert f"FROM {BASE}\n" in text
    # pi, at the version the base Dockerfile pins
    assert "@earendil-works/pi-coding-agent@${PI_VERSION}" in text
    assert f"ARG PI_VERSION={pinned['PI_VERSION']}" in text
    # the engine, installed out of THIS build context (the source root)
    assert "COPY . /opt/ralphd" in text
    assert "pip install --no-cache-dir /opt/ralphd" in text
    # and the run contract every ralphd image owes the CLI
    assert 'ENTRYPOINT ["/opt/ralphd/container/entrypoint.sh"]' in text
    assert "USER 1000" in text
    assert "RALPHD_RUN_DIR=/run/ralphd" in text
    assert "EXPOSE 7777" in text


def test_the_recipe_copies_the_version_pins_instead_of_restating_them(tmp_path):
    """One place in the repo says which pi version ralphd runs. A bump in
    `container/Dockerfile` must reach the derived image by itself."""
    root = tmp_path / "checkout"
    (root / "container").mkdir(parents=True)
    (root / "container/Dockerfile").write_text(
        "FROM python:3.12-slim\n"
        "ARG PI_VERSION=9.9.9\n"
        "ARG DOCKER_VERSION=1.2.3\n"
        "RUN curl -fsSL https://deb.nodesource.com/setup_44.x | bash -\n")
    (root / "pyproject.toml").write_text('[project]\nname = "ralphd"\n')
    text = image.derive(root, BASE).dockerfile
    assert "ARG PI_VERSION=9.9.9" in text
    assert "ARG DOCKER_VERSION=1.2.3" in text
    assert "RALPHD_NODE_MAJOR=44" in text
    # nothing in the module hard-codes a version of its own
    assert "9.9.9" not in (REPO / "src/ralphd/cli/image.py").read_text()
    assert pinned_pi_version() not in (REPO / "src/ralphd/cli/image.py").read_text()


def test_the_real_repo_derives_a_recipe_pinning_the_real_pi_version():
    text = image.derive(REPO, BASE).dockerfile
    assert f"ARG PI_VERSION={pinned_pi_version()}" in text
    assert image.derive(REPO, BASE).root == REPO


# --- unusable input is refused, never guessed around ---------------------


@pytest.mark.parametrize("bad", [
    "", "   ", "ubuntu:24.04 && rm -rf /", "ubuntu:24.04\nRUN evil",
    "-ubuntu", "ubuntu;24", "$(evil)", "ubu ntu",
])
def test_an_unusable_base_reference_is_refused(checkout, bad):
    """The reference is interpolated into a generated `FROM` line: anything
    that could turn into a second instruction has to be refused, not
    rendered."""
    with pytest.raises(image.ImageInputError):
        image.derive(checkout, bad)


def test_surrounding_whitespace_is_stripped_not_refused(checkout):
    """A trailing newline out of a config file is a typo, not an attack."""
    derived = image.derive(checkout, f"  {BASE}\n")
    assert derived.base == BASE
    assert derived.tag == derived_tag_for(checkout, BASE)


def test_a_source_root_that_cannot_answer_is_refused(tmp_path):
    """No `container/Dockerfile` -> no pins to copy, and (the case below) a
    Dockerfile that no longer declares them is the same problem: refuse rather
    than install an unpinned pi, which npm resolves to whatever it likes."""
    root = tmp_path / "checkout"
    root.mkdir()
    with pytest.raises(image.ImageInputError) as e:
        image.derive(root, BASE)
    assert image.SOURCE_MARKER in str(e.value)

    (root / "container").mkdir()
    (root / "container/Dockerfile").write_text("FROM python:3.12-slim\n")
    with pytest.raises(image.ImageInputError) as e:
        image.derive(root, BASE)
    assert "PI_VERSION" in str(e.value)

    (root / "container/Dockerfile").write_text(
        "FROM python:3.12-slim\nARG PI_VERSION=0.84.1\n")
    with pytest.raises(image.ImageInputError) as e:
        image.derive(root, BASE)
    assert "node" in str(e.value)


def test_a_lost_template_marker_is_an_assertion_not_a_silent_hole():
    with pytest.raises(image.ImageInputError):
        image._fill("FROM @BASE@\n", {"@NOPE@": "x"})


# --- in-process: cache hit, cache miss, and the base is never run ---------


def test_a_derived_tag_is_built_once_and_looked_up_afterwards(checkout, stub_daemon):
    first = main.resolve_job_image(None, base=BASE, root=checkout)
    assert first == {"image": derived_tag_for(checkout, BASE),
                     "imageSource": main.IMAGE_SOURCE_BUILT,
                     "imageHash": image.derive(checkout, BASE).hash,
                     "imageBase": BASE, "imageDockerfile": None}
    again = main.resolve_job_image(None, base=BASE, root=checkout)
    assert again == {**first, "imageSource": main.IMAGE_SOURCE_CACHED}
    assert len(stub_daemon("build")) == 1
    assert [a[-1] for a in stub_daemon("image inspect")] == [first["image"]] * 2

    # a different base is a different image, so it is a miss and a build
    other = main.resolve_job_image(None, base=OTHER_BASE, root=checkout)
    assert other["imageSource"] == main.IMAGE_SOURCE_BUILT
    assert [a[2] for a in stub_daemon("build")] == [first["image"], other["image"]]


def test_a_source_change_is_a_new_derived_tag_and_a_new_build(checkout, stub_daemon):
    """Both halves of the cache key, through the *build* path: the same base
    with a changed engine is a different image, and it is not in the cache."""
    first = main.resolve_job_image(None, base=BASE, root=checkout)
    (checkout / "src/ralphd/__init__.py").write_text('__version__ = "0.6.1"\n')
    after = main.resolve_job_image(None, base=BASE, root=checkout)
    assert after["image"] != first["image"]
    assert after["imageBase"] == first["imageBase"] == BASE
    assert after["imageSource"] == main.IMAGE_SOURCE_BUILT
    assert [a[2] for a in stub_daemon("build")] == [first["image"], after["image"]]
    # ... and the changed engine is now the cached one
    assert main.resolve_job_image(None, base=BASE, root=checkout) == {
        **after, "imageSource": main.IMAGE_SOURCE_CACHED}
    assert len(stub_daemon("build")) == 2


def test_the_derived_build_uses_the_source_root_as_its_context(checkout, stub_daemon):
    """The engine has to come from somewhere: the context is the source root,
    and the Dockerfile is the *generated* one, which is not a file in the
    checkout (a failed build must leave no litter behind)."""
    res = main.resolve_job_image(None, base=BASE, root=checkout)
    argv = stub_daemon("build")[0]
    assert argv[:3] == ["build", "-t", res["image"]]
    assert argv[3] == "-f" and argv[5] == str(checkout)
    assert not Path(argv[4]).exists()          # the temp recipe is cleaned up
    assert Path(argv[4]).name == "Dockerfile"
    assert not list(checkout.glob("*.Dockerfile")) and not (checkout / "Dockerfile").exists()


def test_the_base_is_never_probed_built_or_run(checkout, stub_daemon):
    """The whole H2 promise: the operator's image is an ingredient. Nothing
    ever tags it, and nothing ever runs it -- it has no engine in it."""
    res = main.resolve_job_image(None, base=BASE, root=checkout)
    for argv in stub_daemon("build") + stub_daemon("image inspect"):
        assert BASE not in argv, argv
    assert res["image"] != BASE


def test_deriving_without_a_source_tree_is_an_error_not_a_fallback(
        tmp_path, stub_daemon, capsys):
    """Task 038's `DEFAULT_IMAGE` fallback cannot honour a base, and running
    the base itself would be worse than failing."""
    with pytest.raises(SystemExit) as e:
        main.resolve_job_image(None, base=BASE, root=tmp_path / "not-a-checkout")
    assert e.value.code == 1
    err = capsys.readouterr().err
    assert BASE in err and image.SOURCE_MARKER in err and "--image" in err
    assert stub_daemon("build") == [] and stub_daemon("run") == []


def test_pinning_and_deriving_are_mutually_exclusive(checkout, stub_daemon, capsys):
    with pytest.raises(SystemExit) as e:
        main.resolve_job_image("pinned:9", base=BASE, root=checkout)
    assert e.value.code == 2
    assert main.IMAGE_BASE_AND_PIN_NOTICE in capsys.readouterr().err
    assert stub_daemon("build") == [] and stub_daemon("image inspect") == []


def test_a_failed_derived_build_dies_naming_the_base(checkout, stub_daemon,
                                                     monkeypatch, capsys):
    monkeypatch.setenv("STUB_DOCKER_BUILD_FAIL", "1")
    with pytest.raises(SystemExit) as e:
        main.resolve_job_image(None, base=BASE, root=checkout)
    assert e.value.code == 1
    err = capsys.readouterr().err
    assert "build failed" in err and BASE in err and str(checkout) in err
    # the failed tag was not recorded as existing, so a retry rebuilds
    monkeypatch.delenv("STUB_DOCKER_BUILD_FAIL")
    assert main.resolve_job_image(None, base=BASE, root=checkout)["imageSource"] \
        == main.IMAGE_SOURCE_BUILT


# --- black-box: `ralphctl start --base-image` -----------------------------


def test_start_runs_the_derived_image_and_never_the_base(ctl):
    tag = derived_tag_for(REPO, BASE)
    recipe = ctl.tmp / "built.Dockerfile"
    res = ctl.start("derive-miss", "--base-image", BASE,
                    env={"STUB_DOCKER_BUILD_DOCKERFILE": str(recipe)})
    assert res.returncode == 0, res.stderr

    assert ctl.of("image inspect") == [["image", "inspect", tag]]
    assert [a[2] for a in ctl.of("build")] == [tag]
    run = ctl.of("run")[0]
    assert run[-1] == tag and BASE not in run
    assert ctl.host_json("derive-miss")["image"] == tag
    assert f"deriving job image {tag} from base {BASE}" in res.stderr
    assert f"built job image {tag}" in res.stderr
    assert build_lines(res.stderr)              # the build output was visible

    # the recipe docker actually got is the generated one
    text = recipe.read_text()
    assert text.startswith("# Generated by ralphd")
    assert f"FROM {BASE}\n" in text
    assert f"ARG PI_VERSION={pinned_pi_version()}" in text
    assert "pip install --no-cache-dir /opt/ralphd" in text
    assert text == image.derive(REPO, BASE).dockerfile


def test_a_second_start_from_the_same_base_is_a_tag_lookup(ctl):
    tag = derived_tag_for(REPO, BASE)
    first = ctl.start("derive-one", "--base-image", BASE)
    second = ctl.start("derive-two", "--base-image", BASE)
    assert (first.returncode, second.returncode) == (0, 0), second.stderr
    assert len(ctl.of("build")) == 1, ctl.recorded()
    assert len(ctl.of("image inspect")) == 2
    assert [a[-1] for a in ctl.of("run")] == [tag, tag]
    assert "deriving job image" not in second.stderr


def test_another_base_is_another_image(ctl):
    assert ctl.start("derive-a", "--base-image", BASE).returncode == 0
    assert ctl.start("derive-b", "--base-image", OTHER_BASE).returncode == 0
    tags = [a[2] for a in ctl.of("build")]
    assert tags == [derived_tag_for(REPO, BASE), derived_tag_for(REPO, OTHER_BASE)]
    assert len(set(tags)) == 2
    assert ctl.host_json("derive-b")["image"] == tags[1]


def test_a_seeded_derived_tag_is_never_built(ctl):
    tag = derived_tag_for(REPO, BASE)
    res = ctl.start("derive-hit", "--base-image", BASE,
                    env={"STUB_DOCKER_IMAGES": tag})
    assert res.returncode == 0, res.stderr
    assert ctl.of("build") == []
    assert ctl.of("run")[0][-1] == tag
    assert "deriving job image" not in res.stderr


def test_a_failed_derived_build_aborts_start_before_any_run_state(ctl):
    """H4, for the derived path too: no half-registered run."""
    tag = derived_tag_for(REPO, BASE)
    res = ctl.start("derive-fail", "--base-image", BASE,
                    env={"STUB_DOCKER_BUILD_FAIL": "1"})
    assert res.returncode == 1, res.stdout + res.stderr
    assert tag in res.stderr and BASE in res.stderr and "build failed" in res.stderr
    assert "No run state was created" in res.stderr
    assert ctl.of("run") == []
    assert not (ctl.registry / "runs" / "derive-fail").exists()
    assert not (ctl.registry / "configs" / "derive-fail").exists()
    assert json.loads(ctl.run("--json", "runs").stdout) == []


@pytest.mark.parametrize("how", ["flag", "env"])
def test_start_refuses_a_base_together_with_a_pinned_image(ctl, how):
    argv = ("--image", "pinned:9") if how == "flag" else ()
    env = {"RALPHD_IMAGE": "pinned:9"} if how == "env" else None
    res = ctl.start(f"derive-clash-{how}", "--base-image", BASE, *argv, env=env)
    assert res.returncode == 2, res.stdout + res.stderr
    assert "--base-image" in res.stderr and "--image" in res.stderr
    assert ctl.of("build") == [] and ctl.of("run") == []
    assert not (ctl.registry / "runs" / f"derive-clash-{how}").exists()


def test_start_refuses_an_unusable_base(ctl):
    res = ctl.start("derive-junk", "--base-image", "ubuntu:24.04 && whoami")
    assert res.returncode == 2, res.stdout + res.stderr
    assert "cannot derive a job image" in res.stderr
    assert ctl.of("build") == [] and ctl.of("run") == []
    assert not (ctl.registry / "runs" / "derive-junk").exists()


def test_the_flag_is_documented_in_the_cli_reference():
    doc = (REPO / "docs/cli.md").read_text()
    assert "--base-image" in doc
    assert image.DERIVED_REPO in doc


def test_the_default_path_is_untouched_by_the_base_plumbing(ctl):
    """A run that names no base still gets `ralphd:<source hash>` -- task 033's
    behaviour, byte for byte."""
    res = ctl.start("derive-none")
    assert res.returncode == 0, res.stderr
    assert ctl.host_json("derive-none")["image"] == repo_tag()
    assert image.DERIVED_REPO not in res.stderr
