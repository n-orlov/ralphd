"""Task 036 (#20 H4): the resolved job image is recorded in run state, and
`resume` reproduces it.

Requirement H4's constraint is blunt: "`resume` must reproduce the image the run
started with, not the current hash of possibly-changed sources; otherwise a
resume silently swaps the engine mid-run." Task 035 got half way there by
replaying the *recipe* (`base_image`/`dockerfile` out of the run's job.yaml),
which is the same image only while nothing changed. This module covers the
other half:

* `start` records the resolved reference **and the content id of the image the
  container actually got** (observed from the container, not assumed from the
  reference) in the run dir's `host.json`, together with the provenance
  `resolve_job_image` reported;
* `resume` prefers that record over any hashing or building at all -- by
  reference while the reference still names that image, by the recorded id once
  a mutable tag has moved -- and falls back to the recipe replay only when the
  image is genuinely gone from the daemon, saying so;
* the record is readable back through ONE reader (`engine.state.image_record`),
  so `GET /status` (the engine cannot see its own image; it reads the host's
  record from its own run dir) and `ralphctl status` answer identically.

Three tiers, all fast: unit tests on the reader/formatters, black-box
`ralphctl start`/`resume`/`status` over the recording stub docker (whose new
`--format {{.Id}}` / `{{.Image}}` answers let a test stage a moved tag), and the
engine's `/status` over real ASGI.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path

import httpx
import pytest
from test_image_build import Ctl, ctl, repo_tag

from ralphd.cli import image, main
from ralphd.engine.api import create_app
from ralphd.engine.config import JobConfig
from ralphd.engine.loop import LoopSupervisor
from ralphd.engine.state import (
    IMAGE_RECORD_KEYS,
    RunDir,
    format_image,
    format_image_id,
    image_record,
    image_record_from,
)

__all__ = ["ctl"]

REPO = Path(__file__).parent.parent
RECORDED_TAG = "ralphd-derived:0123456789ab"
RECORDED_ID = "sha256:" + "a" * 64
OTHER_ID = "sha256:" + "b" * 64


def stub_id(ref: str) -> str:
    """The id the stub daemon reports for an image it has (see stub-docker)."""
    return "sha256:" + hashlib.sha256(ref.encode()).hexdigest()


def seed(c: Ctl, run_id: str, *, host_extra: dict | None = None,
         job_extra: dict | None = None) -> tuple[Path, Path]:
    """A terminal run dir a `resume` can be pointed at, with whatever image
    record (or none) the test wants in its host.json."""
    rdir = c.registry / "runs" / run_id
    cdir = c.registry / "configs" / run_id
    rdir.mkdir(parents=True)
    cdir.mkdir(parents=True)
    job = {"run_id": run_id, "iterations": 5, "max_approaches": 1,
           **(job_extra or {})}
    (cdir / "job.yaml").write_text(
        "".join(f"{k}: {json.dumps(v)}\n" for k, v in job.items()))
    host = {"runId": run_id, "container": "f" * 12, "port": 1234,
            "apiUrl": "http://127.0.0.1:1234",
            "startedAt": "2024-01-01T00:00:00Z", **(host_extra or {})}
    (rdir / "host.json").write_text(json.dumps(host))
    (rdir / "status.json").write_text(json.dumps(
        {"runId": run_id, "state": "failed", "verdict": "unverified"}))
    return rdir, cdir


def image_inspects(c: Ctl) -> list[str]:
    """The references `docker image inspect` was asked about, in order."""
    return [a[-1] for a in c.of("image inspect")]


# --- the reader and the formatters ----------------------------------------


def test_the_record_is_read_back_key_for_key(tmp_path):
    (tmp_path / "host.json").write_text(json.dumps({
        "runId": "r", "image": RECORDED_TAG, "imageId": RECORDED_ID,
        "imageSource": main.IMAGE_SOURCE_BUILT, "imageHash": "0123456789ab",
        "imageBase": "ubuntu:24.04", "imageDockerfile": "/ci/Dockerfile",
    }))
    assert image_record(tmp_path) == {
        "image": RECORDED_TAG, "imageId": RECORDED_ID,
        "imageSource": main.IMAGE_SOURCE_BUILT, "imageHash": "0123456789ab",
        "imageBase": "ubuntu:24.04", "imageDockerfile": "/ci/Dockerfile"}


@pytest.mark.parametrize("written", [
    None,                                   # no host.json at all
    "{not json",                            # unreadable
    '["a list"]',                           # not an object
    '{"runId": "r", "image": "ralphd:dev"}',  # pre-v0.6: reference only
    '{"image": 7, "imageId": {"a": 1}, "imageHash": null}',   # junk types
    '{"image": "   "}',                     # blank
])
def test_absence_is_never_a_third_case(tmp_path, written):
    """Every key is always present -- a consumer never has to tell a missing
    key from an unknown value (the `maxApproaches`/`model` discipline)."""
    if written is not None:
        (tmp_path / "host.json").write_text(written)
    rec = image_record(tmp_path)
    assert set(rec) == set(IMAGE_RECORD_KEYS)
    assert rec["imageId"] is None
    if written == '{"runId": "r", "image": "ralphd:dev"}':
        assert rec["image"] == "ralphd:dev"
    else:
        assert rec["image"] is None


def test_the_two_readers_agree(tmp_path):
    doc = {"image": RECORDED_TAG, "imageId": RECORDED_ID}
    (tmp_path / "host.json").write_text(json.dumps(doc))
    assert image_record(tmp_path) == image_record_from(doc)
    assert image_record_from(None) == image_record(tmp_path / "nope")


@pytest.mark.parametrize("value,expected", [
    (RECORDED_ID, "a" * 12),
    ("0123456789abcdef", "0123456789ab"),
    ("", ""),
    (None, ""),
    (17, ""),
])
def test_short_image_ids(value, expected):
    assert format_image_id(value) == expected


def test_the_image_line_only_says_what_it_knows():
    assert format_image({"image": RECORDED_TAG, "imageId": RECORDED_ID}) \
        == f"{RECORDED_TAG}  (id {'a' * 12})"
    # no id observed: the reference alone, never `(id None)`
    assert format_image({"image": RECORDED_TAG, "imageId": None}) == RECORDED_TAG
    # nothing recorded at all: nothing to render (the caller omits the line)
    assert format_image({}) == ""
    assert format_image(None) == ""
    assert format_image({"imageId": RECORDED_ID}) == ""
    # a reference that IS the id needs no parenthetical repeat of itself
    assert format_image({"image": RECORDED_ID, "imageId": RECORDED_ID}) \
        == RECORDED_ID


# --- start records it -----------------------------------------------------


def test_start_records_the_resolved_reference_and_the_observed_id(ctl):
    tag = repo_tag()
    res = ctl.start("rec-start")
    assert res.returncode == 0, res.stderr
    meta = ctl.host_json("rec-start")
    assert meta["image"] == tag
    assert meta["imageId"] == stub_id(tag)
    assert meta["imageSource"] == main.IMAGE_SOURCE_BUILT
    assert meta["imageHash"] == image.tag_hash(tag)
    # the id is *observed from the container*, not assumed from the reference:
    # the only answer that stays true for a reference the daemon resolved
    # itself (a pinned tag it pulled, or a tag that moves tomorrow).
    rec = ctl.recorded()
    run_at = next(i for i, a in enumerate(rec) if a[:2] == ["run", "-d"])
    container = "f" * 64
    assert rec[run_at + 1] == ["inspect", "--format", "{{.Image}}", container]


def test_a_second_start_records_the_cached_source(ctl):
    assert ctl.start("rec-first").returncode == 0
    assert ctl.start("rec-second").returncode == 0
    assert ctl.host_json("rec-second")["imageSource"] == main.IMAGE_SOURCE_CACHED
    assert ctl.host_json("rec-second")["imageId"] == ctl.host_json("rec-first")["imageId"]


def test_a_pinned_start_records_the_pin_with_no_hash(ctl):
    """A pinned reference has no content hash (staleness is unknowable), but
    the image the container got is still identified."""
    res = ctl.start("rec-pin", "--image", "pinned/elsewhere:9",
                    env={"STUB_DOCKER_IMAGES": "pinned/elsewhere:9"})
    assert res.returncode == 0, res.stderr
    meta = ctl.host_json("rec-pin")
    assert meta["image"] == "pinned/elsewhere:9"
    assert meta["imageSource"] == main.IMAGE_SOURCE_PINNED
    assert "imageHash" not in meta, "nothing was hashed"
    assert meta["imageId"] == stub_id("pinned/elsewhere:9")
    assert image_record(ctl.registry / "runs" / "rec-pin")["imageHash"] is None


def test_the_ambient_image_pin_is_not_passed_into_the_container(ctl):
    """`RALPHD_IMAGE` is a host-side ambient pin; leaking it into the job
    container would silently pin a nested `ralphctl start` too."""
    assert ctl.start("rec-env").returncode == 0
    run = next(a for a in ctl.recorded() if a[:2] == ["run", "-d"])
    assert not any(v.startswith("RALPHD_IMAGE=") for v in run)


# --- resume reproduces it -------------------------------------------------


def test_resume_reuses_the_recorded_image_and_neither_hashes_nor_builds(ctl):
    """The H4 promise. The recorded reference is deliberately NOT what hashing
    this checkout produces (that is exactly the "sources changed" case), and
    the run's job.yaml carries a recipe whose replay would build something --
    neither happens.
    """
    seed(ctl, "rec-resume",
         host_extra={"image": RECORDED_TAG, "imageId": RECORDED_ID,
                     "imageSource": main.IMAGE_SOURCE_BUILT,
                     "imageHash": "0123456789ab",
                     "imageBase": "ubuntu:24.04"},
         job_extra={"base_image": "ubuntu:24.04"})
    res = ctl.run("resume", "rec-resume",
                  env={"STUB_DOCKER_IMAGES": f"{RECORDED_TAG},{RECORDED_ID}",
                       "STUB_DOCKER_IMAGE_IDS": json.dumps({RECORDED_TAG: RECORDED_ID})})
    assert res.returncode == 0, res.stderr
    assert ctl.of("run")[0][-1] == RECORDED_TAG
    assert ctl.of("build") == [], "a resume must not build"
    # nothing hashed: the tag a fresh hash-and-derive would have produced is
    # never even asked about
    asked = image_inspects(ctl)
    assert asked == [RECORDED_TAG]
    assert repo_tag() not in asked
    # the record survives the resume, provenance and all
    meta = ctl.host_json("rec-resume")
    assert meta["image"] == RECORDED_TAG
    assert meta["imageSource"] == main.IMAGE_SOURCE_RECORDED
    assert meta["imageHash"] == "0123456789ab"
    assert meta["imageBase"] == "ubuntu:24.04"
    assert meta["imageId"] == RECORDED_ID


def test_a_pre_v06_record_with_no_id_is_still_reused(ctl):
    """A run dir written before the id was recorded: the reference is all there
    is, and it is enough -- no comparison to make, nothing to rebuild."""
    seed(ctl, "rec-noid", host_extra={"image": RECORDED_TAG})
    res = ctl.run("resume", "rec-noid",
                  env={"STUB_DOCKER_IMAGES": RECORDED_TAG})
    assert res.returncode == 0, res.stderr
    assert ctl.of("run")[0][-1] == RECORDED_TAG
    assert ctl.of("build") == []
    assert ctl.host_json("rec-noid")["imageSource"] == main.IMAGE_SOURCE_RECORDED


def test_a_moved_reference_resumes_on_the_recorded_id(ctl):
    """A mutable pin (`ralphd:dev`) now names a different image: the recorded
    *id* is what the run started on, so that is what runs."""
    seed(ctl, "rec-moved",
         host_extra={"image": "ralphd:dev", "imageId": RECORDED_ID})
    res = ctl.run("resume", "rec-moved",
                  env={"STUB_DOCKER_IMAGES": f"ralphd:dev,{RECORDED_ID}",
                       "STUB_DOCKER_IMAGE_IDS": json.dumps({"ralphd:dev": OTHER_ID})})
    assert res.returncode == 0, res.stderr
    assert ctl.of("run")[0][-1] == RECORDED_ID
    assert ctl.of("build") == []
    assert "names a different image" in res.stderr
    assert "a" * 12 in res.stderr, "the operator is told which id"
    meta = ctl.host_json("rec-moved")
    assert meta["image"] == RECORDED_ID
    assert meta["imageSource"] == main.IMAGE_SOURCE_RECORDED


def test_a_moved_reference_whose_recorded_id_is_gone_says_so(ctl):
    """Nothing can reproduce the image any more; running the reference as it is
    today is the best available answer, and the operator hears about it."""
    seed(ctl, "rec-moved-gone",
         host_extra={"image": "ralphd:dev", "imageId": RECORDED_ID})
    res = ctl.run("resume", "rec-moved-gone",
                  env={"STUB_DOCKER_IMAGES": "ralphd:dev",
                       "STUB_DOCKER_IMAGE_IDS": json.dumps({"ralphd:dev": OTHER_ID})})
    assert res.returncode == 0, res.stderr
    assert ctl.of("run")[0][-1] == "ralphd:dev"
    assert "no longer on this daemon" in res.stderr
    assert "may run a different engine" in res.stderr


def test_a_pruned_recorded_image_falls_back_to_the_recipe(ctl):
    """Only when the recorded image is genuinely gone does task 035's recipe
    replay get a say -- and it is announced, never silent."""
    seed(ctl, "rec-pruned",
         host_extra={"image": RECORDED_TAG, "imageId": RECORDED_ID},
         job_extra={"base_image": "ubuntu:24.04"})
    res = ctl.run("resume", "rec-pruned")   # stub daemon has nothing
    assert res.returncode == 0, res.stderr
    assert "no longer on this daemon" in res.stderr
    derived = ctl.of("run")[0][-1]
    assert derived.startswith(f"{image.DERIVED_REPO}:")
    assert [a[2] for a in ctl.of("build")] == [derived]
    meta = ctl.host_json("rec-pruned")
    assert meta["imageSource"] == main.IMAGE_SOURCE_BUILT
    assert meta["imageBase"] == "ubuntu:24.04"


def test_a_run_dir_with_no_record_and_no_recipe_still_resumes(ctl):
    """A pre-v0.6 run dir: nothing recorded, nothing to replay. The pre-035
    default, under a word of its own so no surface reads it as a pin."""
    seed(ctl, "rec-nothing")
    res = ctl.run("resume", "rec-nothing")
    assert res.returncode == 0, res.stderr
    assert ctl.of("run")[0][-1] == main.DEFAULT_IMAGE
    assert ctl.of("build") == []
    assert ctl.host_json("rec-nothing")["imageSource"] == main.IMAGE_SOURCE_DEFAULT


def test_an_image_flag_on_resume_still_pins_and_asks_nothing(ctl):
    seed(ctl, "rec-pinned-resume",
         host_extra={"image": RECORDED_TAG, "imageId": RECORDED_ID})
    res = ctl.run("resume", "rec-pinned-resume", "--image", "pin:9",
                  env={"STUB_DOCKER_IMAGES": f"pin:9,{RECORDED_TAG}"})
    assert res.returncode == 0, res.stderr
    assert ctl.of("run")[0][-1] == "pin:9"
    assert image_inspects(ctl) == [], "a pin settles it without the daemon"
    meta = ctl.host_json("rec-pinned-resume")
    assert meta["imageSource"] == main.IMAGE_SOURCE_PINNED
    assert meta["imageId"] == stub_id("pin:9")


def test_start_then_resume_stays_on_the_same_image(ctl):
    """End to end over the real resolver: whatever `start` resolved, `resume`
    runs -- one image, one id, no second build."""
    assert ctl.start("rec-round").returncode == 0
    started = ctl.host_json("rec-round")
    assert ctl.run("resume", "rec-round").returncode == 0
    resumed = ctl.host_json("rec-round")
    assert (resumed["image"], resumed["imageId"]) \
        == (started["image"], started["imageId"])
    assert len(ctl.of("build")) == 1


# --- the surfaces that report it -------------------------------------------


def _status(app) -> dict:
    async def go():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="http://engine") as c:
            r = await c.get("/status")
            assert r.status_code == 200, r.text
            return r.json()
    return asyncio.run(go())


@pytest.fixture
def engine_app(tmp_path):
    run = RunDir(root=tmp_path)
    sup = LoopSupervisor(JobConfig(run_id="unit"), run, tmp_path)
    run.update_status(state="running")
    return create_app(sup.cfg, run, sup)


def test_status_serves_the_recorded_image(engine_app, tmp_path):
    """The engine cannot see its own image, so it reads the host's record from
    its own run dir -- which is how `GET /status` can answer "which engine is
    this run running?" for a caller who has only the API."""
    (tmp_path / "host.json").write_text(json.dumps({
        "runId": "unit", "image": RECORDED_TAG, "imageId": RECORDED_ID,
        "imageSource": main.IMAGE_SOURCE_BUILT}))
    s = _status(engine_app)
    assert s["image"] == RECORDED_TAG
    assert s["imageId"] == RECORDED_ID
    assert s["imageSource"] == main.IMAGE_SOURCE_BUILT
    assert s["imageHash"] is None


def test_status_reports_explicit_nulls_for_a_pre_v06_run_dir(engine_app):
    s = _status(engine_app)
    for key in IMAGE_RECORD_KEYS:
        assert key in s, "absence must never be a third case"
        assert s[key] is None


def test_a_resume_is_visible_through_status(engine_app, tmp_path):
    """The record is re-read per request, so the image a `resume` recorded
    replaces the one the previous episode ran (not cached at startup)."""
    (tmp_path / "host.json").write_text(json.dumps({"image": "ralphd:dev"}))
    assert _status(engine_app)["image"] == "ralphd:dev"
    (tmp_path / "host.json").write_text(json.dumps(
        {"image": RECORDED_TAG, "imageId": RECORDED_ID}))
    assert _status(engine_app)["image"] == RECORDED_TAG


def test_ralphctl_status_prints_the_image_for_a_dead_run(ctl):
    seed(ctl, "rec-status",
         host_extra={"image": RECORDED_TAG, "imageId": RECORDED_ID})
    res = ctl.run("status", "rec-status")
    assert res.returncode == 0, res.stderr
    assert f"image:     {RECORDED_TAG}  (id {'a' * 12})" in res.stdout
    doc = json.loads(ctl.run("--json", "status", "rec-status").stdout)
    assert doc["image"] == RECORDED_TAG
    assert doc["imageId"] == RECORDED_ID


def test_ralphctl_status_omits_the_line_when_nothing_is_recorded(ctl):
    seed(ctl, "rec-status-none")
    res = ctl.run("status", "rec-status-none")
    assert res.returncode == 0, res.stderr
    assert "image:" not in res.stdout
    doc = json.loads(ctl.run("--json", "status", "rec-status-none").stdout)
    assert doc["image"] is None and doc["imageId"] is None


def test_the_cli_and_the_api_render_the_same_record(ctl, engine_app, tmp_path):
    """One reader, two surfaces: `ralphctl status` and `GET /status` say the
    same thing about the same host.json."""
    host = {"runId": "same", "image": RECORDED_TAG, "imageId": RECORDED_ID,
            "imageSource": main.IMAGE_SOURCE_CACHED, "imageHash": "0123456789ab"}
    (tmp_path / "host.json").write_text(json.dumps(host))
    seed(ctl, "rec-same", host_extra=host)
    doc = json.loads(ctl.run("--json", "status", "rec-same").stdout)
    served = _status(engine_app)
    assert {k: doc[k] for k in IMAGE_RECORD_KEYS} \
        == {k: served[k] for k in IMAGE_RECORD_KEYS}


def test_a_live_engine_serves_the_record_from_its_own_run_dir(live):
    """The same thing against a real engine process: `ralphctl status` on a
    LIVE run gets the image out of `GET /status`, not from its own host.json
    read."""
    run = live(run_id="rec-live", job={"iterations": 1, "on_complete": "idle"})
    run.wait_api()
    (run.run_dir / "host.json").write_text(json.dumps({
        "runId": "rec-live", "container": "f" * 12, "port": run.port,
        "apiUrl": f"http://127.0.0.1:{run.port}",
        "image": RECORDED_TAG, "imageId": RECORDED_ID,
        "imageSource": main.IMAGE_SOURCE_BUILT}))
    doc = json.loads(run.ralphctl("--json", "status", "rec-live").stdout)
    assert doc["live"] is True
    assert doc["image"] == RECORDED_TAG
    assert doc["imageId"] == RECORDED_ID
    res = run.ralphctl("status", "rec-live")
    assert f"image:     {RECORDED_TAG}  (id {'a' * 12})" in res.stdout


# --- documented -----------------------------------------------------------


def test_the_record_is_documented_in_the_api_reference():
    doc = (REPO / "docs/api.md").read_text()
    assert "imageId" in doc
    assert "host.json" in doc


def test_the_resume_preference_is_documented_in_the_cli_reference():
    doc = (REPO / "docs/cli.md").read_text()
    assert "imageId" in doc
    for word in (main.IMAGE_SOURCE_RECORDED, main.IMAGE_SOURCE_DEFAULT):
        assert word in doc
