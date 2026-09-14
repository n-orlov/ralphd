"""F.6 -- `ralphctl status`'s existing `image:` line grows the engine
version beside the content-hash tag, tag spelling unchanged (task 032).

`format_image` (`engine/state.py`) already renders `<tag>  (id <short>)`
when the record it is handed carries an `imageId`; task 030 put
`engineVersion` on `status.json` and task 036/`GET /status` already returns
the whole status document, so the version is *already sitting next to* the
image-record keys on the dict `cmd_status` hands to `format_image` -- no new
line, no new field plumbed through. This module proves the rendering, unit
first (`format_image` itself, with and without an id alongside), then
black-box through `ralphctl status` over a dead run's `status.json`/
`host.json` pair, the same fixtures `tests/test_image_recorded.py` uses.
"""

from __future__ import annotations

import json

from test_image_build import Ctl, ctl  # noqa: F401
from test_image_recorded import RECORDED_ID, RECORDED_TAG, seed

from ralphd.engine.state import format_image

ENGINE_VERSION = "0.20.2"

__all__ = ["ctl"]


def test_format_image_renders_the_version_beside_the_tag_alone():
    assert format_image({"image": RECORDED_TAG, "engineVersion": ENGINE_VERSION}) \
        == f"{RECORDED_TAG}  (engine {ENGINE_VERSION})"


def test_format_image_renders_the_version_and_the_id_together():
    assert format_image({
        "image": RECORDED_TAG, "imageId": RECORDED_ID,
        "engineVersion": ENGINE_VERSION,
    }) == f"{RECORDED_TAG}  (engine {ENGINE_VERSION}, id {'a' * 12})"


def test_format_image_omits_the_version_parenthetical_when_absent():
    """A bare `IMAGE_RECORD_KEYS`-only dict (no `engineVersion` key at all,
    what every other `format_image` caller still passes) is unchanged."""
    assert format_image({"image": RECORDED_TAG, "imageId": RECORDED_ID}) \
        == f"{RECORDED_TAG}  (id {'a' * 12})"


def test_ralphctl_status_shows_the_engine_version_beside_the_tag_on_one_line(ctl):
    rdir, _cdir = seed(ctl, "rec-status-version",
                        host_extra={"image": RECORDED_TAG,
                                    "imageId": RECORDED_ID})
    status_doc = json.loads((rdir / "status.json").read_text())
    status_doc["engineVersion"] = ENGINE_VERSION
    (rdir / "status.json").write_text(json.dumps(status_doc))

    res = ctl.run("status", "rec-status-version")
    assert res.returncode == 0, res.stderr
    expected = (f"image:     {RECORDED_TAG}  "
                f"(engine {ENGINE_VERSION}, id {'a' * 12})")
    assert expected in res.stdout
    # the tag spelling is byte-identical to the recorded reference -- only
    # the parenthetical grew, never the tag itself
    assert RECORDED_TAG in res.stdout
    lines = [ln for ln in res.stdout.splitlines() if ln.startswith("image:")]
    assert len(lines) == 1, "the version and the tag are on ONE line"
    assert ENGINE_VERSION in lines[0] and RECORDED_TAG in lines[0]
