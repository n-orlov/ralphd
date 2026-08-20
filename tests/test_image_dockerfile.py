"""`--dockerfile`, and where the image supply points come from (task 035, #20 H3).

Task 033 built the default image and task 034 made a user-supplied image a
*base*. This module covers the third supply point and the rule that ranks all
three:

* `ralphctl start --dockerfile <path>` builds **that** Dockerfile, with the
  Dockerfile's own directory as the build context, into a hash-tagged base
  image (`ralphd-base:<hash>`) and then derives the job image from it -- an
  operator's recipe supplies an ingredient, never the finished job image (it
  has no `ralphd-engine` in it);
* the recipe is cached by exactly the rules the ralphd source is cached by: a
  second `start` from an unchanged context is two tag lookups and no build,
  while any change inside the context is a new base tag *and* a new derived
  tag;
* the three keys that answer "which image does this job run" -- `image`,
  `base_image`, `dockerfile` -- are settled as one unit at the most specific
  *level* that answers at all (command line > the template's `job.yaml` >
  the registry's `config.yaml` > build from source), so a `--dockerfile` on
  the command line beats a standing `image:` pin instead of colliding with
  it, and two of the three within one level is a usage error;
* the ingredients are persisted into the run's own `job.yaml`, so `resume`
  replays the recipe the run started with rather than whatever `ralphd:dev`
  happens to be today.

Three tiers, all fast: unit calls into `cli/image.py` (docker-free by
construction -- see test_image_hash.py's AST guard), in-process
`main.resolve_job_image()` over a miniature source checkout, and black-box
`ralphctl start/resume` over the recording stub docker.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from test_image_build import build_lines, ctl, repo_tag, stub_daemon
from test_image_derived import BASE, checkout, derived_tag_for

from ralphd.cli import image, main

__all__ = ["checkout", "ctl", "stub_daemon"]

REPO = Path(__file__).parent.parent
RECIPE = "FROM ubuntu:24.04\nRUN apt-get install -y openjdk-21-jdk\n"


@pytest.fixture
def recipe(tmp_path):
    """An operator's own build context: a Dockerfile plus a file it copies."""
    ctx = tmp_path / "ci"
    ctx.mkdir()
    (ctx / "Dockerfile").write_text(RECIPE)
    (ctx / "settings.xml").write_text("<settings/>\n")
    return ctx / "Dockerfile"


def base_tag_for(spec: Path) -> str:
    return image.dockerfile_base(spec).tag


def derived_from_recipe(root: Path, spec: Path) -> str:
    """The job image a `--dockerfile <spec>` run must end up running."""
    return derived_tag_for(root, base_tag_for(spec))


# --- the base tag is a function of the whole build context ----------------


def test_the_context_is_the_dockerfiles_own_directory(recipe):
    """The only reading under which a `COPY settings.xml` line in somebody's
    recipe means what its author meant."""
    df = image.dockerfile_base(recipe)
    assert df.dockerfile == recipe.resolve()
    assert df.context == recipe.parent.resolve()
    assert df.rel == "Dockerfile"
    assert "settings.xml" in df.tree.files and "Dockerfile" in df.tree.files


def test_the_base_tag_is_its_own_repository_and_a_legal_docker_tag(recipe):
    """A third repository, for the same reason `ralphd-derived` is separate
    from `ralphd`: this hash covers an operator's build context, so nothing may
    compare it against a ralphd source hash (task 037 must not call it stale)."""
    df = image.dockerfile_base(recipe)
    repo, _, tag = df.tag.partition(":")
    assert repo == image.BASE_REPO == "ralphd-base"
    assert image.HASH_RE.match(tag)
    assert image.base_tag_hash(df.tag) == df.hash
    # the three tag vocabularies refuse each other's references
    assert image.tag_hash(df.tag) is None
    assert image.derived_tag_hash(df.tag) is None
    assert image.base_tag_hash(image.image_tag(df.hash)) is None
    assert image.base_tag_hash(image.derived_tag(df.hash)) is None


def test_the_base_tag_moves_when_anything_in_the_context_moves(recipe):
    before = base_tag_for(recipe)
    assert base_tag_for(recipe) == before          # a cache key, not a nonce

    recipe.write_text(RECIPE + "RUN apt-get install -y maven\n")
    edited = base_tag_for(recipe)
    assert edited != before

    (recipe.parent / "settings.xml").write_text("<settings><new/></settings>\n")
    copied = base_tag_for(recipe)
    assert copied not in (before, edited)


def test_two_recipes_in_one_context_are_two_base_images(recipe):
    """The Dockerfile's *name* is in the hash stream: a context carrying
    `Dockerfile` and `Dockerfile.ci` must not serve one under the other's tag,
    even though both hash the same context."""
    other = recipe.parent / "Dockerfile.ci"
    other.write_text("FROM ubuntu:22.04\n")
    one, two = image.dockerfile_base(recipe), image.dockerfile_base(other)
    assert one.digest == two.digest              # same context ...
    assert one.hash != two.hash                  # ... different recipe
    assert one.tag != two.tag


def test_a_relative_path_is_resolved_against_the_cwd(recipe, monkeypatch):
    monkeypatch.chdir(recipe.parent.parent)
    df = image.dockerfile_base(Path("ci") / "Dockerfile")
    assert df.dockerfile == recipe.resolve()
    assert df.tag == base_tag_for(recipe)


# --- unusable input is refused here, naming the file ---------------------


def test_a_missing_path_is_refused(tmp_path):
    with pytest.raises(image.ImageInputError) as e:
        image.dockerfile_base(tmp_path / "nope" / "Dockerfile")
    assert "no such file" in str(e.value) and "Dockerfile" in str(e.value)


def test_a_directory_is_refused_and_says_what_to_name_instead(recipe):
    with pytest.raises(image.ImageInputError) as e:
        image.dockerfile_base(recipe.parent)
    msg = str(e.value)
    assert "directory" in msg and str(recipe.parent / "Dockerfile") in msg


def test_a_file_with_no_from_instruction_is_not_a_dockerfile(tmp_path):
    """Refused *here*, naming the file, rather than thirty seconds into a
    build whose error message is the builder's."""
    notes = tmp_path / "notes.md"
    notes.write_text("# not a Dockerfile\n\nRUN nothing\n")
    with pytest.raises(image.ImageInputError) as e:
        image.dockerfile_base(notes)
    assert "FROM" in str(e.value) and "notes.md" in str(e.value)


@pytest.mark.parametrize("text", [
    "from ubuntu:24.04\n", "  FROM ubuntu:24.04\n",
    "# comment\nARG X=1\nFROM ubuntu:24.04 AS build\n",
])
def test_a_legal_from_line_in_any_of_its_spellings_is_accepted(tmp_path, text):
    p = tmp_path / "Dockerfile"
    p.write_text(text)
    assert image.dockerfile_base(p).tag.startswith(image.BASE_REPO + ":")


def test_an_undecodable_file_is_refused_rather_than_hashed(tmp_path):
    p = tmp_path / "Dockerfile"
    p.write_bytes(b"\xff\xfe\x00binary\n")
    with pytest.raises(image.ImageInputError) as e:
        image.dockerfile_base(p)
    assert "cannot read" in str(e.value)


# --- in-process: build the base, then derive the job image ---------------


def test_a_dockerfile_builds_a_base_and_the_job_image_is_derived_from_it(
        checkout, recipe, stub_daemon):
    res = main.resolve_job_image(None, dockerfile=str(recipe), root=checkout)
    base = base_tag_for(recipe)
    assert res == {"image": derived_tag_for(checkout, base),
                   "imageSource": main.IMAGE_SOURCE_BUILT,
                   "imageHash": image.derive(checkout, base).hash,
                   "imageBase": base,
                   "imageDockerfile": str(recipe.resolve())}
    # two builds, in order: the operator's recipe with ITS directory as the
    # context, then the derived image with the source root as the context
    first, second = stub_daemon("build")
    assert first == ["build", "-t", base, "-f", str(recipe.resolve()),
                     str(recipe.parent.resolve())]
    assert second[:3] == ["build", "-t", res["image"]]
    assert second[5] == str(checkout)
    assert Path(second[4]).name == "Dockerfile"
    assert not Path(second[4]).exists()          # the generated recipe is temp
    assert res["image"] != base                  # the base is not the job image


def test_a_second_resolve_from_the_same_context_builds_nothing(
        checkout, recipe, stub_daemon):
    first = main.resolve_job_image(None, dockerfile=str(recipe), root=checkout)
    again = main.resolve_job_image(None, dockerfile=str(recipe), root=checkout)
    assert again == {**first, "imageSource": main.IMAGE_SOURCE_CACHED}
    assert len(stub_daemon("build")) == 2        # from the first resolve only
    assert [a[-1] for a in stub_daemon("image inspect")] == [
        base_tag_for(recipe), first["image"]] * 2


def test_a_change_in_the_context_rebuilds_the_base_and_the_job_image(
        checkout, recipe, stub_daemon):
    first = main.resolve_job_image(None, dockerfile=str(recipe), root=checkout)
    recipe.write_text(RECIPE + "RUN apt-get install -y maven\n")
    after = main.resolve_job_image(None, dockerfile=str(recipe), root=checkout)
    assert after["imageBase"] != first["imageBase"]
    assert after["image"] != first["image"]
    assert after["imageSource"] == main.IMAGE_SOURCE_BUILT
    assert [a[2] for a in stub_daemon("build")] == [
        first["imageBase"], first["image"], after["imageBase"], after["image"]]
    # ... and an engine change moves only the derived half
    (checkout / "src/ralphd/__init__.py").write_text('__version__ = "0.6.1"\n')
    newer = main.resolve_job_image(None, dockerfile=str(recipe), root=checkout)
    assert newer["imageBase"] == after["imageBase"]
    assert newer["image"] != after["image"]
    assert len(stub_daemon("build")) == 5


def test_the_recipe_docker_gets_for_the_base_is_the_operators_own(
        checkout, recipe, stub_daemon, monkeypatch, tmp_path):
    """ralphd builds the operator's file verbatim -- it does not rewrite it,
    and it is not the generated derive recipe (which is built second)."""
    seen = tmp_path / "seen.Dockerfile"
    monkeypatch.setenv("STUB_DOCKER_BUILD_DOCKERFILE", str(seen))
    main.resolve_job_image(None, dockerfile=str(recipe), root=checkout)
    # the last build's -f content is the generated one; the base's was the
    # operator's, which the recorded argv above pins by path
    assert seen.read_text().startswith("# Generated by ralphd")
    assert recipe.read_text() == RECIPE


def test_a_base_image_and_a_dockerfile_are_mutually_exclusive(
        checkout, recipe, stub_daemon, capsys):
    with pytest.raises(SystemExit) as e:
        main.resolve_job_image(None, base=BASE, dockerfile=str(recipe),
                               root=checkout)
    assert e.value.code == 2
    assert main.IMAGE_BASE_AND_DOCKERFILE_NOTICE in capsys.readouterr().err
    assert stub_daemon("build") == [] and stub_daemon("image inspect") == []


def test_a_pinned_image_and_a_dockerfile_are_mutually_exclusive(
        checkout, recipe, stub_daemon, capsys):
    with pytest.raises(SystemExit) as e:
        main.resolve_job_image("pinned:9", dockerfile=str(recipe), root=checkout)
    assert e.value.code == 2
    err = capsys.readouterr().err
    assert main.IMAGE_BASE_AND_PIN_NOTICE in err and "--dockerfile" in err
    assert stub_daemon("build") == []


def test_an_unusable_dockerfile_is_refused_before_any_build(
        checkout, tmp_path, stub_daemon, capsys):
    with pytest.raises(SystemExit) as e:
        main.resolve_job_image(None, dockerfile=str(tmp_path / "nope"),
                               root=checkout)
    assert e.value.code == 2
    err = capsys.readouterr().err
    assert "cannot build a base image from" in err and "nope" in err
    assert stub_daemon("build") == [] and stub_daemon("image inspect") == []


def test_a_dockerfile_without_a_source_tree_is_an_error_not_a_fallback(
        recipe, tmp_path, stub_daemon, capsys):
    """Same rule as `--base-image`: there is no fallback that could honour the
    operator's recipe, and running their image directly would be worse."""
    with pytest.raises(SystemExit) as e:
        main.resolve_job_image(None, dockerfile=str(recipe),
                               root=tmp_path / "not-a-checkout")
    assert e.value.code == 1
    err = capsys.readouterr().err
    assert str(recipe) in err and image.SOURCE_MARKER in err and "--image" in err
    assert stub_daemon("build") == []


def test_a_failed_base_build_stops_before_deriving(checkout, recipe,
                                                   stub_daemon, monkeypatch,
                                                   capsys):
    monkeypatch.setenv("STUB_DOCKER_BUILD_FAIL", "1")
    with pytest.raises(SystemExit) as e:
        main.resolve_job_image(None, dockerfile=str(recipe), root=checkout)
    assert e.value.code == 1
    err = capsys.readouterr().err
    assert "build failed" in err and str(recipe.resolve()) in err
    assert [a[2] for a in stub_daemon("build")] == [base_tag_for(recipe)]
    # the failed base was not recorded as existing, so a retry rebuilds it
    monkeypatch.delenv("STUB_DOCKER_BUILD_FAIL")
    assert main.resolve_job_image(None, dockerfile=str(recipe),
                                  root=checkout)["imageSource"] \
        == main.IMAGE_SOURCE_BUILT


# --- black-box: `ralphctl start --dockerfile` ----------------------------


def test_start_builds_the_recipe_derives_and_runs_the_derived_image(ctl, recipe):
    base = base_tag_for(recipe)
    tag = derived_tag_for(REPO, base)
    res = ctl.start("df-miss", "--dockerfile", str(recipe))
    assert res.returncode == 0, res.stderr

    assert [a[2] for a in ctl.of("build")] == [base, tag]
    assert [a[-1] for a in ctl.of("image inspect")] == [base, tag]
    run = ctl.of("run")[0]
    assert run[-1] == tag and base not in run
    assert ctl.host_json("df-miss")["image"] == tag
    assert f"building base image {base}" in res.stderr
    assert f"built base image {base}" in res.stderr
    assert f"deriving job image {tag} from base {base}" in res.stderr
    assert build_lines(res.stderr)


def test_start_records_the_recipe_in_the_runs_job_yaml(ctl, recipe):
    assert ctl.start("df-record", "--dockerfile", str(recipe)).returncode == 0
    job = (ctl.registry / "configs/df-record/job.yaml").read_text()
    assert f'dockerfile: "{recipe.resolve()}"' in job
    assert "base_image:" not in job              # only what was supplied

    assert ctl.start("df-record-base", "--base-image", BASE).returncode == 0
    job = (ctl.registry / "configs/df-record-base/job.yaml").read_text()
    assert f'base_image: "{BASE}"' in job
    assert "dockerfile:" not in job


def test_a_second_start_from_the_same_recipe_builds_nothing(ctl, recipe):
    first = ctl.start("df-one", "--dockerfile", str(recipe))
    second = ctl.start("df-two", "--dockerfile", str(recipe))
    assert (first.returncode, second.returncode) == (0, 0), second.stderr
    assert len(ctl.of("build")) == 2, ctl.recorded()
    assert "building base image" not in second.stderr
    assert "deriving job image" not in second.stderr
    tag = derived_from_recipe(REPO, recipe)
    assert [a[-1] for a in ctl.of("run")] == [tag, tag]


def test_a_failed_recipe_build_aborts_start_before_any_run_state(ctl, recipe):
    """H4, for the third supply point too: no half-registered run."""
    res = ctl.start("df-fail", "--dockerfile", str(recipe),
                    env={"STUB_DOCKER_BUILD_FAIL": "1"})
    assert res.returncode == 1, res.stdout + res.stderr
    assert base_tag_for(recipe) in res.stderr and "build failed" in res.stderr
    assert "No run state was created" in res.stderr
    assert ctl.of("run") == []
    assert not (ctl.registry / "runs" / "df-fail").exists()
    assert not (ctl.registry / "configs" / "df-fail").exists()
    assert json.loads(ctl.run("--json", "runs").stdout) == []


def test_start_refuses_a_recipe_together_with_a_base_or_a_pin(ctl, recipe):
    both = ctl.start("df-clash-base", "--dockerfile", str(recipe),
                     "--base-image", BASE)
    assert both.returncode == 2, both.stdout + both.stderr
    assert "--base-image" in both.stderr and "--dockerfile" in both.stderr

    pinned = ctl.start("df-clash-pin", "--dockerfile", str(recipe),
                       env={"RALPHD_IMAGE": "pinned:9"})
    assert pinned.returncode == 2, pinned.stdout + pinned.stderr
    assert "--dockerfile" in pinned.stderr and "--image" in pinned.stderr

    assert ctl.of("build") == [] and ctl.of("run") == []
    assert not (ctl.registry / "runs" / "df-clash-base").exists()
    assert not (ctl.registry / "runs" / "df-clash-pin").exists()


def test_start_refuses_an_unusable_recipe(ctl, tmp_path):
    res = ctl.start("df-junk", "--dockerfile", str(tmp_path / "missing"))
    assert res.returncode == 2, res.stdout + res.stderr
    assert "cannot build a base image from" in res.stderr
    assert ctl.of("build") == [] and ctl.of("run") == []
    assert not (ctl.registry / "runs" / "df-junk").exists()


# --- precedence: three keys, one question, most specific level wins ------


def write_template(ctl, name: str, **cfg) -> None:
    tdir = ctl.registry / "templates" / name
    tdir.mkdir(parents=True)
    (tdir / "job.yaml").write_text(
        "".join(f"{k}: {json.dumps(v)}\n" for k, v in cfg.items()))
    (tdir / "prd.md").write_text("# Template PRD\n")


def test_nothing_anywhere_still_builds_the_default_image(ctl):
    """The bottom of the ladder is task 033's behaviour, unchanged."""
    res = ctl.start("sup-none")
    assert res.returncode == 0, res.stderr
    assert ctl.host_json("sup-none")["image"] == repo_tag()


def test_the_registry_config_supplies_any_of_the_three(ctl, recipe):
    assert ctl.run("config", "set", "dockerfile", str(recipe)).returncode == 0
    res = ctl.start("sup-reg-df")
    assert res.returncode == 0, res.stderr
    assert ctl.host_json("sup-reg-df")["image"] == derived_from_recipe(REPO, recipe)

    assert ctl.run("config", "set", "dockerfile", "").returncode == 0
    assert ctl.run("config", "set", "base_image", BASE).returncode == 0
    res = ctl.start("sup-reg-base")
    assert res.returncode == 0, res.stderr
    assert ctl.host_json("sup-reg-base")["image"] == derived_tag_for(REPO, BASE)


def test_a_template_beats_the_registry_config_for_the_whole_question(ctl, recipe):
    """A `dockerfile:` in the template does not merely add to a registry-wide
    `image:` pin -- it replaces the answer, so nothing is pinned any more."""
    assert ctl.run("config", "set", "image", "registry-pin:9").returncode == 0
    write_template(ctl, "df-tpl", dockerfile=str(recipe))
    res = ctl.run("start", "--template", "df-tpl", "--llm", "none",
                  "--run-id", "sup-tpl")
    assert res.returncode == 0, res.stderr
    assert ctl.host_json("sup-tpl")["image"] == derived_from_recipe(REPO, recipe)
    assert "registry-pin:9" not in res.stderr

    # ... and the other way round: a template `image:` beats a registry base
    assert ctl.run("config", "set", "image", "").returncode == 0
    assert ctl.run("config", "set", "base_image", BASE).returncode == 0
    write_template(ctl, "pin-tpl", image="tpl-pin:9")
    builds = len(ctl.of("build"))
    res = ctl.run("start", "--template", "pin-tpl", "--llm", "none",
                  "--run-id", "sup-tpl-pin")
    assert res.returncode == 0, res.stderr
    assert ctl.host_json("sup-tpl-pin")["image"] == "tpl-pin:9"
    assert len(ctl.of("build")) == builds       # a pin builds nothing


def test_a_flag_beats_a_template_and_the_registry_config(ctl, recipe):
    assert ctl.run("config", "set", "image", "registry-pin:9").returncode == 0
    write_template(ctl, "base-tpl", base_image=BASE)
    res = ctl.run("start", "--template", "base-tpl", "--llm", "none",
                  "--run-id", "sup-flag", "--dockerfile", str(recipe))
    assert res.returncode == 0, res.stderr
    assert ctl.host_json("sup-flag")["image"] == derived_from_recipe(REPO, recipe)
    # neither lower-level answer was consulted
    assert BASE not in [a[2] for a in ctl.of("build")]
    assert "registry-pin:9" not in res.stderr

    # and an `--image` flag beats a template's dockerfile the same way
    write_template(ctl, "df-tpl2", dockerfile=str(recipe))
    res = ctl.run("start", "--template", "df-tpl2", "--llm", "none",
                  "--run-id", "sup-flag-pin", "--image", "flag-pin:9")
    assert res.returncode == 0, res.stderr
    assert ctl.host_json("sup-flag-pin")["image"] == "flag-pin:9"


@pytest.mark.parametrize("where", ["registry", "template"])
def test_two_supply_keys_in_one_level_is_a_usage_error(ctl, recipe, where):
    """Within one level there is nothing to rank them by, so ralphd refuses
    instead of picking one and being silently wrong."""
    if where == "registry":
        assert ctl.run("config", "set", "image", "pin:9").returncode == 0
        assert ctl.run("config", "set", "dockerfile", str(recipe)).returncode == 0
        res = ctl.start("sup-clash-reg")
    else:
        write_template(ctl, "clash-tpl", image="pin:9", base_image=BASE)
        res = ctl.run("start", "--template", "clash-tpl", "--llm", "none",
                      "--run-id", "sup-clash-tpl")
    assert res.returncode == 2, res.stdout + res.stderr
    assert "contradict each other" in res.stderr
    assert "--image" in res.stderr
    assert ctl.of("build") == [] and ctl.of("run") == []


def test_ralphd_image_is_the_bottom_of_the_ladder_not_a_level(ctl, recipe):
    """`RALPHD_IMAGE` is ambient, so it pins when nothing else answers and is
    refused by name (never silently dropped) when something does."""
    env = {"RALPHD_IMAGE": "ambient:9"}
    assert ctl.start("sup-env", env=env).returncode == 0
    assert ctl.host_json("sup-env")["image"] == "ambient:9"

    write_template(ctl, "env-tpl", dockerfile=str(recipe))
    res = ctl.run("start", "--template", "env-tpl", "--llm", "none",
                  "--run-id", "sup-env-clash", env=env)
    assert res.returncode == 2, res.stdout + res.stderr
    assert "--image" in res.stderr and "--dockerfile" in res.stderr


def test_the_supply_keys_are_settled_as_one_unit_in_process(ctl, monkeypatch):
    """The rule itself, without a build: whichever level answers first wins
    whole, and the other two keys are left unset rather than inherited."""
    monkeypatch.setenv("RALPHD_REGISTRY", str(ctl.registry))
    monkeypatch.delenv("RALPHD_IMAGE", raising=False)

    class Args:
        image = base_image = dockerfile = None

    args = Args()
    main._resolve_image_supply(args, {"base_image": "tpl-base:1"},
                              {"image": "reg-pin:1", "dockerfile": "/x/D"})
    assert (args.image, args.base_image, args.dockerfile) \
        == (None, "tpl-base:1", None)

    args = Args()
    args.dockerfile = "/ci/Dockerfile"
    main._resolve_image_supply(args, {"image": "tpl-pin:1"},
                              {"base_image": "reg-base:1"})
    assert (args.image, args.base_image, args.dockerfile) \
        == (None, None, "/ci/Dockerfile")

    args = Args()
    main._resolve_image_supply(args, {}, {})
    assert (args.image, args.base_image, args.dockerfile) == (None, None, None)


# --- resume reproduces the image the run started on (035 recipe, 036 record) --


def test_resume_replays_the_recipe_and_rebuilds_nothing(ctl, recipe):
    tag = derived_from_recipe(REPO, recipe)
    assert ctl.start("df-resume", "--dockerfile", str(recipe)).returncode == 0
    assert ctl.of("run")[0][-1] == tag

    res = ctl.run("resume", "df-resume")
    assert res.returncode == 0, res.stderr
    assert ctl.of("run")[1][-1] == tag
    assert ctl.host_json("df-resume")["image"] == tag
    assert len(ctl.of("build")) == 2          # start's two, none from resume


def test_resume_replays_a_base_image_too(ctl):
    tag = derived_tag_for(REPO, BASE)
    assert ctl.start("base-resume", "--base-image", BASE).returncode == 0
    assert ctl.run("resume", "base-resume").returncode == 0
    assert ctl.of("run")[1][-1] == tag
    assert len(ctl.of("build")) == 1


def test_resume_of_a_run_with_no_recipe_reuses_the_image_it_started_on(ctl):
    """Task 036 (H4) took this case over: the *resolved* image is recorded in
    run state and preferred, so a run that recorded no ingredients no longer
    falls back to DEFAULT_IMAGE -- it resumes on the image it started on."""
    assert ctl.start("plain-resume").returncode == 0
    started = ctl.of("run")[0][-1]
    assert started != main.DEFAULT_IMAGE
    assert ctl.run("resume", "plain-resume").returncode == 0
    assert ctl.of("run")[1][-1] == started


def test_an_image_flag_on_resume_still_pins(ctl, recipe):
    assert ctl.start("df-resume-pin", "--dockerfile", str(recipe)).returncode == 0
    res = ctl.run("resume", "df-resume-pin", "--image", "pin:9")
    assert res.returncode == 0, res.stderr
    assert ctl.of("run")[1][-1] == "pin:9"
    assert len(ctl.of("build")) == 2          # nothing rebuilt for the pin


def test_a_changed_recipe_is_replayed_only_once_the_recorded_image_is_gone(ctl, recipe):
    """Task 036 (H4) narrowed this: while the image the run started on is still
    on the daemon, an edited Dockerfile changes nothing -- a resume must not
    swap the engine mid-run. The recipe replay is the *fallback*, and once that
    image is gone it is of the recipe (not of a tag), so the resume runs the
    image the edited recipe now means."""
    started = derived_from_recipe(REPO, recipe)
    assert ctl.start("df-resume-edit", "--dockerfile", str(recipe)).returncode == 0
    assert ctl.of("run")[0][-1] == started
    recipe.write_text(RECIPE + "RUN apt-get install -y maven\n")
    edited = derived_from_recipe(REPO, recipe)
    assert edited != started

    assert ctl.run("resume", "df-resume-edit").returncode == 0
    assert ctl.of("run")[1][-1] == started
    assert len(ctl.of("build")) == 2          # start's two, none from resume

    ctl.images.write_text("")                # the recorded image is pruned
    res = ctl.run("resume", "df-resume-edit")
    assert res.returncode == 0, res.stderr
    assert "no longer on this daemon" in res.stderr
    assert ctl.of("run")[2][-1] == edited
    assert len(ctl.of("build")) == 4


# --- the surfaces that have to say so ----------------------------------


def test_the_flag_and_the_keys_are_documented_in_the_cli_reference():
    doc = (REPO / "docs/cli.md").read_text()
    assert "--dockerfile" in doc
    assert image.BASE_REPO in doc
    # the precedence rule, and the config keys that participate in it
    assert "config set dockerfile" in doc and "config set base_image" in doc


def test_the_supply_points_are_documented_in_the_architecture_doc():
    doc = (REPO / "docs/architecture.md").read_text()
    assert "--dockerfile" in doc
    assert image.BASE_REPO in doc and image.DERIVED_REPO in doc


def test_config_set_accepts_the_two_new_keys_and_still_refuses_junk(ctl):
    assert ctl.run("config", "set", "base_image", BASE).returncode == 0
    assert json.loads(ctl.run("--json", "config", "get",
                              "base_image").stdout)["value"] == BASE
    assert ctl.run("config", "set", "dockerfile", "/ci/Dockerfile").returncode == 0
    bad = ctl.run("config", "set", "docker_file", "/ci/Dockerfile")
    assert bad.returncode == 2
    assert "unknown config key" in bad.stderr
