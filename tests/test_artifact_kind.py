"""Task 022 (#54): a server-decided artifact `kind`, sniffed from magic bytes.

`engine.state.artifact_entry` already sniffs text-vs-binary (task 023's
predecessor field, `isText`). This adds the one further decision the hub's
image preview (#54, tasks 023-025) needs: text / previewable image / opaque
binary, decided from the file's OWN LEADING BYTES and never from its name --
so a `.png` that is secretly a ZIP is not previewable, and a `.bin` that is
secretly a PNG is.

What is pinned here:

  * `kind` lives in ONE place (`engine.state`) and both `ralphctl artifacts
    ls` and the hub's `GET /api/runs/<id>/artifacts` read it off the exact
    same `artifact_entries`/`artifact_entry` dicts -- there is no second
    resolver to disagree with it;
  * the decision is magic-byte, not extension: renaming a file cannot change
    its kind;
  * SVG is specifically excluded from the image allow-list (stored XSS), with
    a comment in `engine.state` explaining why, so a later "just add svg"
    patch fails this test instead of shipping the vulnerability;
  * a `.tar.gz` (an opaque binary already covered by `ARTIFACT_BINARY`) keeps
    today's exact wording unchanged.
"""

from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path

from ralphd.engine.state import (
    ARTIFACT_BINARY,
    ARTIFACT_IMAGE_MIME_TYPES,
    ARTIFACT_KIND_BINARY,
    ARTIFACT_KIND_IMAGE,
    ARTIFACT_KIND_TEXT,
    artifact,
    artifact_body,
    artifact_entries,
)
from ralphd.cli.ui_server import artifact_list

# A real PNG signature (the same eight magic bytes every PNG starts with).
PNG_BYTES = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00"

# A real, valid ZIP -- genuinely non-text, not just "renamed .png".
_zip_buf = io.BytesIO()
with zipfile.ZipFile(_zip_buf, "w") as _zf:
    _zf.writestr("a.txt", "not an image, whatever the name says" * 4)
ZIP_BYTES = _zip_buf.getvalue()

# A real gzip signature followed by binary payload -- today's ARTIFACT_BINARY
# case, which must not move.
TAR_GZ_BYTES = b"\x1f\x8b\x08\x00\x00\x00\x00\x00\x00\x03" + bytes(range(200))

SVG_BYTES = (b'<?xml version="1.0" encoding="UTF-8"?>\n'
             b'<svg xmlns="http://www.w3.org/2000/svg" onload="alert(1)">'
             b'</svg>\n')


def _seed(tmp_path: Path, files: dict[str, bytes]) -> Path:
    registry = tmp_path / "registry"
    rdir = registry / "runs" / "kind-run"
    adir = rdir / "artifacts"
    adir.mkdir(parents=True)
    (rdir / "status.json").write_text(json.dumps({"runId": "kind-run",
                                                   "state": "succeeded"}))
    for rel, data in files.items():
        path = adir / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    return registry


def test_png_named_file_holding_zip_bytes_is_not_previewable(tmp_path):
    root = _seed(tmp_path, {"fake.png": ZIP_BYTES}) / "runs" / "kind-run"
    entry = artifact(root, "fake.png")
    assert entry["kind"] != ARTIFACT_KIND_IMAGE
    assert entry["kind"] == ARTIFACT_KIND_BINARY
    assert entry["imageMime"] is None


def test_bin_named_file_holding_png_bytes_is_previewable(tmp_path):
    root = _seed(tmp_path, {"payload.bin": PNG_BYTES}) / "runs" / "kind-run"
    entry = artifact(root, "payload.bin")
    assert entry["kind"] == ARTIFACT_KIND_IMAGE
    assert entry["imageMime"] == "image/png"
    assert entry["imageMime"] in ARTIFACT_IMAGE_MIME_TYPES


def test_tar_gz_keeps_todays_exact_artifact_binary_wording(tmp_path):
    root = _seed(tmp_path, {"dump.tar.gz": TAR_GZ_BYTES}) / "runs" / "kind-run"
    entry = artifact(root, "dump.tar.gz")
    assert entry["kind"] == ARTIFACT_KIND_BINARY
    assert artifact_body(entry) == ARTIFACT_BINARY
    assert "pull" in ARTIFACT_BINARY


def test_svg_is_specifically_not_previewable(tmp_path):
    root = _seed(tmp_path, {"logo.svg": SVG_BYTES}) / "runs" / "kind-run"
    entry = artifact(root, "logo.svg")
    assert entry["kind"] != ARTIFACT_KIND_IMAGE
    assert entry["imageMime"] is None
    # SVG's bytes are plain XML text, so it reads as ARTIFACT_KIND_TEXT --
    # correct: the exclusion is "never previewable as an image", not "must be
    # described as opaque binary".
    assert entry["kind"] == ARTIFACT_KIND_TEXT


def test_the_svg_exclusion_is_explained_in_source_as_an_xss_precaution():
    import ralphd.engine.state as state_mod
    src = Path(state_mod.__file__).read_text()
    # the reasoning must live next to the sniff itself, or a later
    # well-meaning "just add svg" patch has nothing warning it off
    idx = src.index("_sniff_image_mime")
    window = src[idx:idx + 2000]
    assert "SVG" in window
    assert "XSS" in window


def test_svg_is_never_added_to_the_image_allow_list():
    assert "image/svg+xml" not in ARTIFACT_IMAGE_MIME_TYPES
    assert not any("svg" in m for m in ARTIFACT_IMAGE_MIME_TYPES)


def test_cli_sniffs_kinds_while_the_hub_listing_derives_them_from_the_extension(
        tmp_path):
    """#294 changes the hub listing on purpose: it never opens a file, so its
    `kind` is the extension's. The CLI listing and the hub's one-file view still
    sniff magic bytes (so a ZIP named `.png` is not previewed as an image)."""
    registry = _seed(tmp_path, {
        "fake.png": ZIP_BYTES,
        "payload.bin": PNG_BYTES,
        "dump.tar.gz": TAR_GZ_BYTES,
        "logo.svg": SVG_BYTES,
        "notes.txt": b"plain text notes\n",
    })
    root = registry / "runs" / "kind-run"
    cli_entries = {e["path"]: e["kind"] for e in artifact_entries(root)}
    assert cli_entries == {
        "fake.png": ARTIFACT_KIND_BINARY,
        "payload.bin": ARTIFACT_KIND_IMAGE,
        "dump.tar.gz": ARTIFACT_KIND_BINARY,
        "logo.svg": ARTIFACT_KIND_TEXT,
        "notes.txt": ARTIFACT_KIND_TEXT,
    }
    hub_payload = artifact_list(registry, "kind-run")
    hub_entries = {e["path"]: e["kind"] for e in hub_payload["entries"]}
    assert hub_entries == {
        "fake.png": ARTIFACT_KIND_IMAGE,
        "payload.bin": ARTIFACT_KIND_BINARY,
        "dump.tar.gz": ARTIFACT_KIND_BINARY,
        "logo.svg": ARTIFACT_KIND_BINARY,
        "notes.txt": ARTIFACT_KIND_TEXT,
    }
    from ralphd.cli.ui_server import artifact_view
    for name, kind in cli_entries.items():
        assert artifact_view(registry, "kind-run", name)["kind"] == kind
