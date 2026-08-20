"""`ralphctl doctor` reports job-image STALENESS, not mere existence (task 037,
#20 requirement H4).

The pre-v0.6 check was `docker image inspect <ref>` -- exactly the check that
let two runs of this project execute a ten-day-old engine with nothing to say
so (PRD fact 1). What replaces it has to answer a harder question honestly:
*is the image this host runs jobs on the one this source tree builds?* There
are four answers, and `unknowable` is a real one -- there are three
non-comparable hash namespaces (`ralphd:<hash>`, `ralphd-derived:<hash>`,
`ralphd-base:<hash>`) and a pinned reference is a function of nothing this host
can recompute, so calling a pin "up to date" would be the same lie as calling
it stale.

Two tiers:

* a unit tier over `main.image_staleness` (pure: every docker fact is passed
  in), covering fresh / stale / missing / each flavour of unknowable, and
* a black-box tier running the real `ralphctl doctor` against the recording
  stub docker (`tests/stub-docker/docker`), covering the text line, the
  `--json` field, the `checks["image"]` verdict in both directions, and the
  per-run report over run state written by task 036.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from ralphd.cli import image
from ralphd.cli import main as cli

REPO = Path(__file__).parent.parent
STUB_DOCKER = REPO / "tests" / "stub-docker" / "docker"
RALPHCTL = Path(sys.executable).parent / "ralphctl"

# The hash this checkout's image inputs produce -- computed by the same unit
# ralphctl uses, never transcribed, so this file cannot rot when a source file
# changes (which is the entire point of the tag).
SOURCE_HASH = image.hash_image_inputs(REPO).hash
SOURCE_TAG = image.image_tag(SOURCE_HASH)
OLD_TAG = image.image_tag("aaaaaaaaaaaa")


# --------------------------------------------------------------- unit tier
def test_image_tag_kind_reads_the_three_namespaces_and_nothing_else():
    assert cli.image_tag_kind(SOURCE_TAG) == (cli.IMAGE_TAG_DEFAULT, SOURCE_HASH)
    assert cli.image_tag_kind(image.derived_tag("0123456789ab")) == (
        cli.IMAGE_TAG_DERIVED, "0123456789ab")
    assert cli.image_tag_kind(image.base_tag("0123456789ab")) == (
        cli.IMAGE_TAG_BASE, "0123456789ab")
    for ref in ("ralphd:dev", "ralphd", "ghcr.io/x/ralphd:latest",
                "ralphd:not-a-hash", "sha256:" + "f" * 64):
        assert cli.image_tag_kind(ref) == (cli.IMAGE_TAG_UNHASHED, None), ref
    assert cli.image_tag_kind("") == (cli.IMAGE_TAG_NONE, None)
    assert cli.image_tag_kind(None) == (cli.IMAGE_TAG_NONE, None)


def test_current_source_hash_is_this_checkout_and_absent_without_one(tmp_path):
    root, h = cli.current_source_hash()
    assert root == REPO and h == SOURCE_HASH
    # a directory with no container/Dockerfile is the wheel/pipx case: absence,
    # never a guessed hash
    assert cli.current_source_hash(tmp_path) == (None, None)


def test_the_current_tag_is_fresh():
    v = cli.image_staleness(SOURCE_TAG, source_hash=SOURCE_HASH, present=True)
    assert v["staleness"] == cli.IMAGE_STALENESS_FRESH
    assert v["imageHash"] == SOURCE_HASH == v["sourceHash"]
    assert SOURCE_TAG in v["note"] and SOURCE_HASH in v["note"]


def test_a_hashed_tag_from_another_source_tree_is_stale():
    v = cli.image_staleness(OLD_TAG, source_hash=SOURCE_HASH, present=True)
    assert v["staleness"] == cli.IMAGE_STALENESS_STALE
    assert v["sourceImage"] == SOURCE_TAG
    # the note has to name BOTH: what is running and what this source builds
    assert OLD_TAG in v["note"] and SOURCE_TAG in v["note"]


def test_a_missing_current_tag_says_the_next_start_builds_it():
    v = cli.image_staleness(SOURCE_TAG, source_hash=SOURCE_HASH, present=False)
    assert v["staleness"] == cli.IMAGE_STALENESS_MISSING
    assert "builds it" in v["note"]


def test_a_missing_pin_says_nothing_will_build_it():
    v = cli.image_staleness("ralphd:dev", source_hash=SOURCE_HASH, present=False)
    assert v["staleness"] == cli.IMAGE_STALENESS_MISSING
    assert "cannot start" in v["note"]


def test_unasked_presence_is_never_read_as_absence():
    """`present=None` means nobody asked the daemon -- the hash comparison
    still stands, and `missing` must not be inferred from ignorance."""
    v = cli.image_staleness(OLD_TAG, source_hash=SOURCE_HASH)
    assert v["present"] is None
    assert v["staleness"] == cli.IMAGE_STALENESS_STALE


@pytest.mark.parametrize("ref", ["ralphd:dev", "ghcr.io/x/ralphd:latest",
                                 "sha256:" + "f" * 64])
def test_a_pin_is_unknowable_not_fresh(ref):
    v = cli.image_staleness(ref, source_hash=SOURCE_HASH, present=True)
    assert v["staleness"] == cli.IMAGE_STALENESS_UNKNOWABLE
    assert v["imageHash"] is None
    assert "cannot be established" in v["note"] and SOURCE_TAG in v["note"]


def test_a_derived_or_base_tag_is_never_compared_to_a_source_hash():
    """Both hashes cover ingredients that are not this source tree (a base
    image, an operator's build context), so a comparison would be meaningless
    -- even when the hash happens to equal the source hash."""
    for ref, word in ((image.derived_tag(SOURCE_HASH), "base image"),
                      (image.base_tag(SOURCE_HASH), "build context")):
        v = cli.image_staleness(ref, source_hash=SOURCE_HASH, present=True)
        assert v["staleness"] == cli.IMAGE_STALENESS_UNKNOWABLE, ref
        assert v["imageHash"] is None
        assert word in v["note"]


def test_no_source_tree_makes_every_verdict_unknowable():
    v = cli.image_staleness(SOURCE_TAG, source_hash=None, present=True)
    assert v["staleness"] == cli.IMAGE_STALENESS_UNKNOWABLE
    assert image.SOURCE_MARKER in v["note"]
    assert v["sourceImage"] is None


def test_no_recorded_reference_at_all_is_unknowable():
    v = cli.image_staleness(None, source_hash=SOURCE_HASH)
    assert v["staleness"] == cli.IMAGE_STALENESS_UNKNOWABLE
    assert v["image"] is None and v["imageKind"] == cli.IMAGE_TAG_NONE
    assert v["note"] == cli.IMAGE_NO_REFERENCE_NOTE


def test_the_record_answers_when_the_reference_cannot():
    """Task 036's record: a resume that fell back to the recorded image *id*
    runs a reference with no tag to read, while host.json still says which
    source hash produced it."""
    rec = {"image": "sha256:" + "f" * 64, "imageSource": cli.IMAGE_SOURCE_RECORDED,
           "imageHash": SOURCE_HASH, "imageBase": None, "imageDockerfile": None}
    v = cli.image_staleness(rec["image"], source_hash=SOURCE_HASH, rec=rec)
    assert v["staleness"] == cli.IMAGE_STALENESS_FRESH
    assert v["imageHash"] == SOURCE_HASH
    stale = cli.image_staleness(rec["image"], source_hash="ffffffffffff", rec=rec)
    assert stale["staleness"] == cli.IMAGE_STALENESS_STALE


def test_a_records_derived_hash_is_never_comparable():
    rec = {"image": image.derived_tag(SOURCE_HASH), "imageHash": SOURCE_HASH,
           "imageSource": cli.IMAGE_SOURCE_BUILT, "imageBase": "ubuntu:24.04",
           "imageDockerfile": None}
    v = cli.image_staleness(rec["image"], source_hash=SOURCE_HASH, rec=rec)
    assert v["staleness"] == cli.IMAGE_STALENESS_UNKNOWABLE


@pytest.mark.parametrize("source", [cli.IMAGE_SOURCE_PINNED,
                                    cli.IMAGE_SOURCE_UNHASHABLE,
                                    cli.IMAGE_SOURCE_DEFAULT])
def test_pinned_unhashable_and_default_records_stay_unknowable(source):
    """`pinned`/`unhashable`/`default` mean nobody hashed anything, so a hash
    in the record (there is none) could not make the answer knowable -- and the
    record's own word for it is quoted, so the ignorance is explained."""
    rec = {"image": "ralphd:dev", "imageSource": source, "imageHash": None,
           "imageBase": None, "imageDockerfile": None}
    v = cli.image_staleness(rec["image"], source_hash=SOURCE_HASH, rec=rec)
    assert v["staleness"] == cli.IMAGE_STALENESS_UNKNOWABLE
    assert v["imageSource"] == source
    assert f"`{source}`" in v["note"]


def test_every_verdict_is_one_of_the_declared_words():
    cases = [
        cli.image_staleness(SOURCE_TAG, source_hash=SOURCE_HASH, present=True),
        cli.image_staleness(OLD_TAG, source_hash=SOURCE_HASH, present=True),
        cli.image_staleness(OLD_TAG, source_hash=SOURCE_HASH, present=False),
        cli.image_staleness("x:1", source_hash=None),
        cli.image_staleness(None, source_hash=None),
    ]
    for v in cases:
        assert v["staleness"] in cli.IMAGE_STALENESS_VERDICTS
        assert v["note"] and not v["note"].endswith("None")


# --------------------------------------------------------- black-box tier
class Ctl:
    def __init__(self, tmp: Path):
        self.tmp = tmp
        self.registry = tmp / "registry"
        (self.registry / "runs").mkdir(parents=True)
        self.log = tmp / "docker-argv.jsonl"

    def run(self, *argv: str, env: dict | None = None) -> subprocess.CompletedProcess:
        full_env = {k: v for k, v in os.environ.items() if k != "RALPHD_IMAGE"}
        full_env.update({
            "RALPHD_DOCKER": str(STUB_DOCKER),
            "RALPHD_REGISTRY": str(self.registry),
            "STUB_DOCKER_LOG": str(self.log),
            **(env or {}),
        })
        return subprocess.run([str(RALPHCTL), *argv], env=full_env,
                              capture_output=True, text=True, timeout=90)

    def doctor(self, *argv: str, env: dict | None = None) -> dict:
        res = self.run("--json", "doctor", *argv, env=env)
        assert res.stdout, res.stderr
        return json.loads(res.stdout)

    def argv(self) -> list[list[str]]:
        if not self.log.exists():
            return []
        return [json.loads(x) for x in self.log.read_text().splitlines() if x]

    def seed_run(self, run_id: str, state: str = "running",
                 host: dict | None = None) -> Path:
        """A run dir as `start` leaves it: status.json plus (task 036) the
        host-side record of which image its container was created from."""
        d = self.registry / "runs" / run_id
        d.mkdir(parents=True, exist_ok=True)
        (d / "status.json").write_text(json.dumps({"state": state,
                                                   "schemaVersion": 1}))
        if host is not None:
            (d / "host.json").write_text(json.dumps({"runId": run_id, **host}))
        return d


@pytest.fixture
def ctl(tmp_path):
    return Ctl(tmp_path)


def _images(*refs: str) -> dict:
    """Exactly these image refs exist on the stub daemon (an empty string set
    means the daemon has nothing -- the stub's unset default is 'everything
    exists', which is what every pre-037 doctor test relies on)."""
    return {"STUB_DOCKER_IMAGES": ",".join(refs)}


def test_doctor_reports_the_current_source_tag_as_fresh(ctl):
    doc = ctl.doctor(env=_images(SOURCE_TAG))
    v = doc["imageStaleness"]
    assert v["staleness"] == "fresh"
    assert v["image"] == SOURCE_TAG and v["sourceHash"] == SOURCE_HASH
    assert v["where"] == cli.IMAGE_IN_USE_SOURCE
    assert v["sourceRoot"] == str(REPO)
    assert doc["checks"]["image"] is True


def test_doctor_reports_a_pinned_older_hash_as_stale_in_text_and_json(ctl):
    env = {**_images(OLD_TAG, SOURCE_TAG), "RALPHD_IMAGE": OLD_TAG}
    doc = ctl.doctor(env=env)
    v = doc["imageStaleness"]
    assert v["staleness"] == "stale"
    assert v["image"] == OLD_TAG and v["sourceImage"] == SOURCE_TAG
    assert v["where"] == cli.IMAGE_IN_USE_ENV
    # a stale image is report-only: running an old engine on purpose is
    # supported (`--image`), being unable to tell is not
    assert doc["checks"]["image"] is True
    res = ctl.run("doctor", env=env)
    line = [ln for ln in res.stdout.splitlines() if "job image" in ln]
    assert len(line) == 1, res.stdout
    assert line[0].startswith("! ")
    assert "stale" in line[0] and OLD_TAG in line[0] and SOURCE_TAG in line[0]
    assert v["note"] in line[0]


def test_doctor_text_line_is_unmarked_when_the_image_is_fresh(ctl):
    res = ctl.run("doctor", env=_images(SOURCE_TAG))
    line = next(ln for ln in res.stdout.splitlines() if "job image" in ln)
    assert not line.startswith("!") and "fresh" in line


def test_a_missing_buildable_default_tag_still_passes_the_image_check(ctl):
    """The pre-v0.6 check failed a fresh checkout for an image `start` would
    have built anyway. Absence of the content-hashed default is not a
    problem -- it is a build."""
    doc = ctl.doctor(env=_images())
    v = doc["imageStaleness"]
    assert v["staleness"] == "missing" and v["present"] is False
    assert "builds it" in v["note"]
    assert doc["checks"]["image"] is True
    assert doc["ok"] == all(doc["checks"].values())


def test_a_missing_pin_fails_the_image_check(ctl):
    """The other direction: a pinned reference is run as-is, so if it is not
    here the run cannot start -- that is a failing check, as before."""
    doc = ctl.doctor(env={**_images(SOURCE_TAG), "RALPHD_IMAGE": "ralphd:dev"})
    assert doc["imageStaleness"]["staleness"] == "missing"
    assert doc["checks"]["image"] is False
    assert doc["ok"] is False


def test_an_explicit_image_flag_is_the_reference_reported_on(ctl):
    doc = ctl.doctor("--image", OLD_TAG, env=_images(OLD_TAG, SOURCE_TAG))
    assert doc["imageStaleness"]["image"] == OLD_TAG
    assert doc["imageStaleness"]["where"] == cli.IMAGE_IN_USE_FLAG
    assert doc["imageStaleness"]["staleness"] == "stale"


def test_the_registry_image_pin_is_the_reference_reported_on(ctl):
    (ctl.registry / "config.yaml").write_text(f"image: {OLD_TAG}\n")
    doc = ctl.doctor(env=_images(OLD_TAG, SOURCE_TAG))
    assert doc["imageStaleness"]["image"] == OLD_TAG
    assert doc["imageStaleness"]["where"] == cli.IMAGE_IN_USE_REGISTRY
    assert doc["imageStaleness"]["staleness"] == "stale"


@pytest.mark.parametrize("key,val", [("base_image", "ubuntu:24.04"),
                                     ("dockerfile", "ci/Dockerfile")])
def test_a_registry_configured_base_is_named_rather_than_guessed_at(ctl, key, val):
    """A registry that supplies a *base* means the job image is a derived tag
    that only exists once it has been derived. Naming one here would mean a
    second resolver (and a build), so doctor reports the base instead."""
    (ctl.registry / "config.yaml").write_text(f"{key}: {val}\n")
    doc = ctl.doctor(env=_images(SOURCE_TAG))
    v = doc["imageStaleness"]
    assert v["staleness"] == "unknowable"
    assert v["image"] is None and v["imageBase"] == val
    assert val in v["note"] and key in v["where"]
    assert doc["checks"]["image"] is True


def test_the_image_check_is_the_only_docker_image_inspect(ctl):
    """One `docker image inspect`, however many runs the registry holds: the
    per-run report is a hash comparison over run state, not a daemon query."""
    for i in range(3):
        ctl.seed_run(f"run-{i}", host={"image": OLD_TAG, "imageHash": "aaaaaaaaaaaa",
                                       "imageSource": "built"})
    doc = ctl.doctor(env={**_images(SOURCE_TAG),
                          "STUB_DOCKER_CONTAINERS": "ralphd-run-0,ralphd-run-1,"
                                                    "ralphd-run-2",
                          "STUB_DOCKER_RUNNING": "ralphd-run-0,ralphd-run-1,"
                                                 "ralphd-run-2"})
    assert len(doc["runImageStaleness"]) == 3
    inspects = [a for a in ctl.argv() if a[:2] == ["image", "inspect"]]
    assert len(inspects) == 1, inspects


def test_a_live_run_on_an_older_engine_is_reported_per_run(ctl):
    """PRD fact 1, made visible: a run whose own state records an image built
    from a different source tree than this checkout."""
    ctl.seed_run("stale-run", host={"image": OLD_TAG, "imageHash": "aaaaaaaaaaaa",
                                    "imageSource": "built",
                                    "imageId": "sha256:" + "a" * 64})
    env = {**_images(SOURCE_TAG), "STUB_DOCKER_CONTAINERS": "ralphd-stale-run",
           "STUB_DOCKER_RUNNING": "ralphd-stale-run"}
    doc = ctl.doctor(env=env)
    entry = doc["runImageStaleness"][0]
    assert entry["runId"] == "stale-run" and entry["staleness"] == "stale"
    assert entry["imageSource"] == "built"
    res = ctl.run("doctor", env=env)
    assert cli.IMAGE_STALE_RUNS_HEADING in res.stdout
    assert "stale-run" in res.stdout and OLD_TAG in res.stdout
    # report-only, exactly like the stray/dangling reports
    assert doc["ok"] == all(doc["checks"].values())


def test_a_live_run_on_the_current_engine_is_fresh_and_quiet(ctl):
    ctl.seed_run("fresh-run", host={"image": SOURCE_TAG, "imageHash": SOURCE_HASH,
                                    "imageSource": "cached"})
    env = {**_images(SOURCE_TAG), "STUB_DOCKER_CONTAINERS": "ralphd-fresh-run",
           "STUB_DOCKER_RUNNING": "ralphd-fresh-run"}
    doc = ctl.doctor(env=env)
    assert [e["staleness"] for e in doc["runImageStaleness"]] == ["fresh"]
    res = ctl.run("doctor", env=env)
    assert cli.IMAGE_STALE_RUNS_HEADING not in res.stdout


def test_a_pinned_or_unrecorded_run_image_is_reported_as_unknowable(ctl):
    """Two ways of not knowing: an operator pin (`pinned`, staleness
    unknowable by construction) and a pre-v0.6 run dir with no record at all.
    Neither is printed as trouble, and neither is silently called current."""
    ctl.seed_run("pinned-run", host={"image": "ralphd:dev",
                                     "imageSource": "pinned"})
    ctl.seed_run("old-run")  # no host.json at all
    env = {**_images(SOURCE_TAG),
           "STUB_DOCKER_CONTAINERS": "ralphd-pinned-run,ralphd-old-run",
           "STUB_DOCKER_RUNNING": "ralphd-pinned-run,ralphd-old-run"}
    doc = ctl.doctor(env=env)
    by_run = {e["runId"]: e for e in doc["runImageStaleness"]}
    assert by_run["pinned-run"]["staleness"] == "unknowable"
    assert "`pinned`" in by_run["pinned-run"]["note"]
    assert by_run["old-run"]["staleness"] == "unknowable"
    assert by_run["old-run"]["image"] is None
    res = ctl.run("doctor", env=env)
    assert cli.IMAGE_STALE_RUNS_HEADING not in res.stdout


def test_terminal_runs_are_left_out_of_the_per_run_report(ctl):
    """An image that was current while the run happened is not stale
    afterwards, and the run is over either way."""
    for state in ("succeeded", "failed", "aborted"):
        ctl.seed_run(f"done-{state}", state=state,
                     host={"image": OLD_TAG, "imageHash": "aaaaaaaaaaaa",
                           "imageSource": "built"})
    doc = ctl.doctor(env=_images(SOURCE_TAG))
    assert doc["runImageStaleness"] == []


def test_doctor_json_shape_carries_both_new_fields(ctl):
    doc = ctl.doctor(env=_images(SOURCE_TAG))
    assert "imageStaleness" in doc and "runImageStaleness" in doc
    for key in ("image", "imageKind", "imageHash", "imageSource", "sourceHash",
                "sourceImage", "present", "staleness", "note", "where",
                "imageBase", "sourceRoot"):
        assert key in doc["imageStaleness"], key
