"""Building the default job image on a cache miss (task 033, #20 H1).

Task 032 decided *what* the image is a function of (`cli/image.py`: the
content hash of `container/`, `pyproject.toml` and `src/ralphd`). This module
covers the other half -- turning that hash into an image that exists:

* `start` with nobody selecting an image resolves `ralphd:<hash>`, where the
  hash is the one `image.hash_image_inputs()` computes over the source root;
* a missing tag is built (with `container/Dockerfile` and the source root as
  the build context), a present tag is not -- the second `start` is a pure tag
  lookup;
* an explicit selection (`--image`, `RALPHD_IMAGE`, the registry's `image`)
  pins exactly that reference and neither hashes nor builds;
* a failed build aborts `start` loudly **before** the run dir or config dir
  exists (requirement H4: no half-registered run);
* build output is visible as it arrives but bounded, and the retained tail is
  printed when -- and only when -- the live echo was cut short by a failure.

Two tiers, both fast: black-box `ralphctl` over the recording stub docker
(`tests/stub-docker/docker`, whose new `build`/`image inspect` knobs make the
image cache answer honestly), and in-process calls to
`main.resolve_job_image()` over a miniature source tree, which is the only way
to assert that a *source change* moves the tag without editing this checkout.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from ralphd.cli import image, main

REPO = Path(__file__).parent.parent
STUB_DOCKER = REPO / "tests" / "stub-docker" / "docker"
RALPHCTL = Path(sys.executable).parent / "ralphctl"


class Ctl:
    """ralphctl bound to a tmp registry + the recording stub docker, with the
    stub's image cache answering honestly (empty until something builds)."""

    def __init__(self, tmp: Path):
        self.tmp = tmp
        self.registry = tmp / "registry"
        self.registry.mkdir()
        self.log = tmp / "docker-argv.jsonl"
        self.images = tmp / "stub-images.txt"
        self.prd = tmp / "prd.md"
        self.prd.write_text("# Test PRD\n\nDo the thing.\n")

    def run(self, *argv: str, env: dict | None = None) -> subprocess.CompletedProcess:
        full_env = {k: v for k, v in os.environ.items() if k != "RALPHD_IMAGE"}
        full_env.update({
            "RALPHD_DOCKER": str(STUB_DOCKER),
            "RALPHD_REGISTRY": str(self.registry),
            "STUB_DOCKER_LOG": str(self.log),
            "STUB_DOCKER_IMAGES": "",
            "STUB_DOCKER_IMAGE_FILE": str(self.images),
            **(env or {}),
        })
        return subprocess.run([str(RALPHCTL), *argv], env=full_env,
                              capture_output=True, text=True, timeout=120)

    def start(self, run_id: str, *argv: str, env: dict | None = None):
        return self.run("start", "--prd", str(self.prd), "--llm", "none",
                        "--run-id", run_id, *argv, env=env)

    def recorded(self) -> list[list[str]]:
        if not self.log.exists():
            return []
        return [json.loads(line) for line in self.log.read_text().splitlines()]

    def of(self, verb: str) -> list[list[str]]:
        """Recorded invocations of one docker verb (`build`, `run`, ...)."""
        if verb == "image inspect":
            return [a for a in self.recorded() if a[:2] == ["image", "inspect"]]
        return [a for a in self.recorded() if a[:1] == [verb]]

    def host_json(self, run_id: str) -> dict:
        return json.loads((self.registry / "runs" / run_id / "host.json").read_text())


@pytest.fixture
def ctl(tmp_path):
    return Ctl(tmp_path)


def repo_tag() -> str:
    """The tag `ralphctl` (an editable install of this checkout) must resolve."""
    root = image.source_root()
    assert root == REPO, root
    return image.image_tag(image.hash_image_inputs(root).hash)


def build_lines(stderr: str) -> list[str]:
    return [ln for ln in stderr.splitlines() if ln.startswith(main.IMAGE_BUILD_PREFIX)]


# --- the tag, and the build on a cache miss --------------------------------


def test_start_builds_the_content_hashed_tag_when_it_is_missing(ctl):
    """H1: the tag is `ralphd:<content hash>`, the build uses
    `container/Dockerfile` with the source root as its context, and the
    container runs that exact tag."""
    tag = repo_tag()
    res = ctl.start("img-miss")
    assert res.returncode == 0, res.stderr

    assert ctl.of("image inspect") == [["image", "inspect", tag]]
    assert ctl.of("build") == [["build", "-t", tag, "-f",
                                str(REPO / "container/Dockerfile"), str(REPO)]]
    run = ctl.of("run")[0]
    assert run[-1] == tag
    assert ctl.host_json("img-miss")["image"] == tag
    # the operator is told, on stderr, that a build happened and which tag
    assert f"building job image {tag}" in res.stderr
    assert f"built job image {tag}" in res.stderr


def test_the_tag_is_the_hash_of_the_inputs_and_nothing_else(ctl):
    """A guard against a tag that merely *looks* content-derived: the hash in
    the tag is exactly `hash_image_inputs`' short hash, and it is a legal tag."""
    res = ctl.start("img-tag")
    assert res.returncode == 0, res.stderr
    ref = ctl.host_json("img-tag")["image"]
    repo, _, tag = ref.partition(":")
    assert repo == image.IMAGE_REPO == "ralphd"
    assert tag == image.hash_image_inputs(REPO).hash
    assert image.HASH_RE.match(tag)
    assert image.tag_hash(ref) == tag


def test_a_second_start_is_a_tag_lookup_with_no_build(ctl):
    """The whole point of hashing: repeat runs cost one `image inspect`."""
    tag = repo_tag()
    first = ctl.start("img-one")
    second = ctl.start("img-two")
    assert (first.returncode, second.returncode) == (0, 0), second.stderr

    assert len(ctl.of("build")) == 1, ctl.recorded()
    assert len(ctl.of("image inspect")) == 2
    assert [a[-1] for a in ctl.of("run")] == [tag, tag]
    assert "building job image" not in second.stderr
    assert ctl.host_json("img-two")["image"] == tag


def test_a_tag_already_present_is_never_built(ctl):
    """Seeded cache hit: not even the first `start` builds."""
    tag = repo_tag()
    res = ctl.start("img-hit", env={"STUB_DOCKER_IMAGES": tag})
    assert res.returncode == 0, res.stderr
    assert ctl.of("build") == []
    assert ctl.of("image inspect") == [["image", "inspect", tag]]
    assert ctl.of("run")[0][-1] == tag


# --- an explicit selection pins ------------------------------------------


@pytest.mark.parametrize("how", ["flag", "env", "registry"])
def test_an_explicit_image_neither_hashes_nor_builds(ctl, how):
    """`--image`, `RALPHD_IMAGE` and the registry's `image` are *selections*:
    deliberately running an old engine stays possible, cheap and silent."""
    pin = f"pinned-{how}:9"
    argv, env = (), None
    if how == "flag":
        argv = ("--image", pin)
    elif how == "env":
        env = {"RALPHD_IMAGE": pin}
    else:
        assert ctl.run("config", "set", "image", pin).returncode == 0
    res = ctl.start(f"img-pin-{how}", *argv, env=env)
    assert res.returncode == 0, res.stderr

    assert ctl.of("build") == []
    assert ctl.of("image inspect") == []          # no cache probe at all
    assert ctl.of("run")[0][-1] == pin
    assert ctl.host_json(f"img-pin-{how}")["image"] == pin
    assert "job image" not in res.stderr


def test_pinning_does_not_hash_even_in_process(tmp_path, monkeypatch):
    """The black-box test above can only observe that no docker ran. Prove the
    stronger claim -- a pinned image is resolved without touching the source
    tree at all -- by making both halves explode."""
    def boom(*a, **kw):
        raise AssertionError("hashed a tree for a pinned image")

    monkeypatch.setattr(image, "hash_image_inputs", boom)
    monkeypatch.setattr(image, "source_root", boom)
    monkeypatch.setattr(main, "image_exists", boom)
    assert main.resolve_job_image("some/where:tag") == {
        "image": "some/where:tag", "imageSource": main.IMAGE_SOURCE_PINNED,
        "imageHash": None, "imageBase": None}


# --- a failed build aborts before any run state ---------------------------


def test_a_failed_build_aborts_start_before_any_run_state_exists(ctl):
    """H4: no half-registered run. The exit is loud (names the tag, the
    context and the fact that nothing was created) and no container starts."""
    tag = repo_tag()
    res = ctl.start("img-fail", env={"STUB_DOCKER_BUILD_FAIL": "1"})
    assert res.returncode == 1, res.stdout + res.stderr
    assert tag in res.stderr and "build failed" in res.stderr
    assert "No run state was created" in res.stderr
    assert "--image" in res.stderr          # the escape hatch is named

    assert ctl.of("run") == [], ctl.recorded()
    assert not (ctl.registry / "runs" / "img-fail").exists()
    assert not (ctl.registry / "configs" / "img-fail").exists()
    # ... and the run is not registered in any other sense either
    assert json.loads(ctl.run("--json", "runs").stdout) == []


def test_a_failed_build_leaves_the_run_id_free(ctl):
    """The consequence an operator cares about: fix the build and re-run the
    same command, rather than picking a new run id around a corpse."""
    failed = ctl.start("img-retry", env={"STUB_DOCKER_BUILD_FAIL": "1"})
    assert failed.returncode == 1
    ok = ctl.start("img-retry")
    assert ok.returncode == 0, ok.stderr
    assert ctl.host_json("img-retry")["image"] == repo_tag()


# --- bounded, visible output ---------------------------------------------


def test_build_output_is_visible_as_it_arrives_but_bounded(ctl):
    tag = repo_tag()
    res = ctl.start("img-loud", env={"STUB_DOCKER_BUILD_LINES": "1000"})
    assert res.returncode == 0, res.stderr
    lines = build_lines(res.stderr)
    # every echoed line is prefixed and the echo stops at the cap, plus the
    # one notice saying so -- not 1000 lines of somebody else's build
    assert len(lines) == main.IMAGE_BUILD_ECHO_LINES + 1, len(lines)
    assert main.IMAGE_BUILD_ELIDED_NOTICE in res.stderr
    assert any("Step 1/1000" in ln for ln in lines)        # the start is visible
    assert not any("Step 999/1000" in ln for ln in lines)  # the middle is not
    assert f"built job image {tag}" in res.stderr


def test_a_failing_build_prints_the_retained_tail_once(ctl):
    """The tail exists for exactly one purpose: debugging a failure whose
    interesting lines were past the echo cap."""
    res = ctl.start("img-loud-fail", env={"STUB_DOCKER_BUILD_LINES": "1000",
                                          "STUB_DOCKER_BUILD_FAIL": "1"})
    assert res.returncode == 1
    assert f"last {main.IMAGE_BUILD_TAIL_LINES} lines:" in res.stderr
    lines = build_lines(res.stderr)
    # echo cap + notice + tail header + tail
    assert len(lines) == main.IMAGE_BUILD_ECHO_LINES + 2 + main.IMAGE_BUILD_TAIL_LINES
    assert any("Step 1000/1000" in ln for ln in lines)   # the end is now visible
    assert sum("Step 1/1000" in ln for ln in lines) == 1  # nothing printed twice


def test_a_short_failing_build_does_not_repeat_its_output(ctl):
    res = ctl.start("img-short-fail", env={"STUB_DOCKER_BUILD_LINES": "3",
                                           "STUB_DOCKER_BUILD_FAIL": "1"})
    assert res.returncode == 1
    assert f"last {main.IMAGE_BUILD_TAIL_LINES} lines:" not in res.stderr
    assert main.IMAGE_BUILD_ELIDED_NOTICE not in res.stderr
    assert sum("Step 1/3" in ln for ln in build_lines(res.stderr)) == 1


# --- in-process: a source change moves the tag ---------------------------


@pytest.fixture
def tree(tmp_path):
    """A miniature ralphd source root: enough for `source_root()` to accept it
    and for the hash to have something to hash."""
    root = tmp_path / "checkout"
    (root / "container").mkdir(parents=True)
    (root / "container/Dockerfile").write_text("FROM python:3.12-slim\n")
    (root / "container/entrypoint.sh").write_text("#!/bin/bash\nexec ralphd-engine\n")
    (root / "src/ralphd").mkdir(parents=True)
    (root / "src/ralphd/__init__.py").write_text('__version__ = "0.6.0"\n')
    (root / "pyproject.toml").write_text('[project]\nname = "ralphd"\n')
    return root


@pytest.fixture
def stub_daemon(tmp_path, monkeypatch):
    """Point `main`'s docker at the recording stub, with an honest image cache."""
    log = tmp_path / "in-process-argv.jsonl"
    images = tmp_path / "in-process-images.txt"
    monkeypatch.setattr(main, "DOCKER", str(STUB_DOCKER))
    monkeypatch.setenv("STUB_DOCKER_LOG", str(log))
    monkeypatch.setenv("STUB_DOCKER_IMAGES", "")
    monkeypatch.setenv("STUB_DOCKER_IMAGE_FILE", str(images))
    monkeypatch.delenv("STUB_DOCKER_BUILD_FAIL", raising=False)

    def recorded(verb: str) -> list[list[str]]:
        if not log.exists():
            return []
        argvs = [json.loads(ln) for ln in log.read_text().splitlines()]
        if verb == "image inspect":
            return [a for a in argvs if a[:2] == ["image", "inspect"]]
        return [a for a in argvs if a[:1] == [verb]]

    return recorded


def test_an_input_change_produces_a_new_tag_and_a_new_build(tree, stub_daemon):
    first = main.resolve_job_image(None, root=tree)
    assert first["imageSource"] == main.IMAGE_SOURCE_BUILT
    assert first["imageHash"] == image.hash_image_inputs(tree).hash
    assert first["image"] == image.image_tag(first["imageHash"])
    # nothing was derived from anything: this is the default image (task 034)
    assert first["imageBase"] is None

    # same tree -> cache hit, no second build
    again = main.resolve_job_image(None, root=tree)
    assert again == {**first, "imageSource": main.IMAGE_SOURCE_CACHED}
    assert len(stub_daemon("build")) == 1

    # an input changes -> a different tag, which does not exist yet
    (tree / "src/ralphd/__init__.py").write_text('__version__ = "0.6.1"\n')
    after = main.resolve_job_image(None, root=tree)
    assert after["imageHash"] != first["imageHash"]
    assert after["imageSource"] == main.IMAGE_SOURCE_BUILT
    assert [a[2] for a in stub_daemon("build")] == [first["image"], after["image"]]

    # ... while an excluded path (a running job's own output) does not
    (tree / "artifacts").mkdir()
    (tree / "artifacts/report.md").write_text("# written by the job\n")
    assert main.resolve_job_image(None, root=tree)["image"] == after["image"]
    assert len(stub_daemon("build")) == 2


def test_the_build_context_is_the_source_root_and_its_own_dockerfile(tree, stub_daemon):
    main.resolve_job_image(None, root=tree)
    argv = stub_daemon("build")[0]
    assert argv[3:] == ["-f", str(tree / "container/Dockerfile"), str(tree)]


def test_a_failed_build_dies_with_exit_1_and_no_tag(tree, stub_daemon, monkeypatch):
    monkeypatch.setenv("STUB_DOCKER_BUILD_FAIL", "1")
    with pytest.raises(SystemExit) as e:
        main.resolve_job_image(None, root=tree)
    assert e.value.code == 1
    # the failed tag was not recorded as existing, so a retry rebuilds
    monkeypatch.delenv("STUB_DOCKER_BUILD_FAIL")
    assert main.resolve_job_image(None, root=tree)["imageSource"] \
        == main.IMAGE_SOURCE_BUILT


def test_no_source_tree_falls_back_observably(tmp_path, stub_daemon, capsys):
    """Task 038 owns *which* fallback; task 033 owns that it is never silent.
    A wheel/pipx install has no `container/` to hash."""
    res = main.resolve_job_image(None, root=tmp_path / "not-a-checkout")
    assert res == {"image": main.DEFAULT_IMAGE,
                   "imageSource": main.IMAGE_SOURCE_UNHASHABLE,
                   "imageHash": None, "imageBase": None}
    err = capsys.readouterr().err
    assert main.IMAGE_NO_SOURCE_NOTICE in err
    assert main.DEFAULT_IMAGE in err and "--image" in err
    assert stub_daemon("build") == [] and stub_daemon("image inspect") == []


def test_missing_inputs_are_named_before_the_build(tree, stub_daemon, capsys):
    """A hash over a partial input set is a real hash, but the build will fail
    on the missing piece -- say which one now, not via `pip install`."""
    (tree / "pyproject.toml").unlink()
    res = main.resolve_job_image(None, root=tree)
    assert res["imageSource"] == main.IMAGE_SOURCE_BUILT
    err = capsys.readouterr().err
    assert "image inputs missing" in err and "pyproject.toml" in err


def test_the_build_defaults_to_the_legacy_builder_but_never_overrides_a_choice(
        monkeypatch):
    """A build from *inside* a job container has the static docker client only
    -- no buildx -- so BuildKit is not the default here (PRD fact 5). An
    operator who sets DOCKER_BUILDKIT keeps their choice."""
    monkeypatch.delenv("DOCKER_BUILDKIT", raising=False)
    assert main._image_build_env()["DOCKER_BUILDKIT"] == "0"
    monkeypatch.setenv("DOCKER_BUILDKIT", "1")
    assert main._image_build_env()["DOCKER_BUILDKIT"] == "1"


# --- the tag vocabulary -------------------------------------------------


def test_image_tag_and_tag_hash_round_trip():
    h = "0123456789ab"
    assert image.image_tag(h) == "ralphd:0123456789ab"
    assert image.tag_hash(image.image_tag(h)) == h
    # anything not produced by hashing these inputs is *not* a content tag:
    # neither stale nor fresh, and task 037 must not guess either way
    for ref in ("ralphd:dev", "ralphd", "ralphd:v05", "other:0123456789ab",
                "ralphd:0123456789AB", "ralphd:0123456789abc", ""):
        assert image.tag_hash(ref) is None, ref
