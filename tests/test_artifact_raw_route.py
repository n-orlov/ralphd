"""Task 023 (#54): the raw artifact byte route.

`engine.state.artifact_raw` (consumed by the hub's `GET
/api/runs/<id>/artifacts-raw/<name>`) resolves `name` through
`state.artifact_relpath` -- the SAME single resolver `artifact()`/
`artifact_view()` already use -- and never guesses a `Content-Type` from the
URL: only a file whose SNIFFED kind is `ARTIFACT_KIND_IMAGE` (task 022's
sniff) and whose size is at or under `ARTIFACT_INLINE_MAX_BYTES` is ever
answered with its real bytes and a `Content-Type` drawn from
`ARTIFACT_IMAGE_MIME_TYPES`. Everything else -- text, opaque binary, absent,
an image over the cap, or a name `artifact_relpath` refuses outright --
answers with today's `pull` wording (`ARTIFACT_BINARY`) rather than a stream
of bytes, or `None` (caller -> 404) for a name that cannot address an
artifact at all.
"""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

from ralphd.engine.state import (
    ARTIFACT_BINARY,
    ARTIFACT_IMAGE_MIME_TYPES,
    ARTIFACT_INLINE_MAX_BYTES,
    artifact_raw,
)

sys.path.insert(0, str(Path(__file__).parent))
from test_cli_ui import UiServer, _get_raw, ui  # noqa: E402

__all__ = ["UiServer", "ui"]

PNG_HEADER = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00"
TEXT_BYTES = b"just some notes\n"
TAR_GZ_BYTES = b"\x1f\x8b\x08\x00\x00\x00\x00\x00\x00\x03" + bytes(range(200))


def _seed(tmp_path: Path, files: dict[str, bytes], run_id="raw-run") -> Path:
    registry = tmp_path / "registry"
    rdir = registry / "runs" / run_id
    adir = rdir / "artifacts"
    adir.mkdir(parents=True)
    (rdir / "status.json").write_text(json.dumps({"runId": run_id,
                                                   "state": "succeeded"}))
    for rel, data in files.items():
        path = adir / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    return registry


def _png(size: int) -> bytes:
    """A real PNG signature padded out to exactly `size` bytes -- the sniff
    only looks at the head, so the padding does not change the kind, only
    the size the cap check compares against."""
    assert size >= len(PNG_HEADER)
    return PNG_HEADER + bytes(size - len(PNG_HEADER))


# ---------------------------------------------------------------- function-level


def test_artifact_raw_serves_a_previewable_image_with_its_sniffed_mime(tmp_path):
    registry = _seed(tmp_path, {"shot.png": PNG_HEADER + b"rest of the file"})
    root = registry / "runs" / "raw-run"
    raw = artifact_raw(root, "shot.png")
    assert raw["contentType"] == "image/png"
    assert raw["contentType"] in ARTIFACT_IMAGE_MIME_TYPES
    assert raw["body"] == PNG_HEADER + b"rest of the file"


def test_artifact_raw_refuses_a_non_image_kind_with_pull_wording(tmp_path):
    registry = _seed(tmp_path, {"notes.txt": TEXT_BYTES,
                                 "dump.tar.gz": TAR_GZ_BYTES})
    root = registry / "runs" / "raw-run"
    for name in ("notes.txt", "dump.tar.gz"):
        raw = artifact_raw(root, name)
        assert raw["contentType"] is None
        assert raw["body"] == ARTIFACT_BINARY.encode()


def test_artifact_raw_a_bin_named_file_holding_png_bytes_is_still_served(tmp_path):
    """The kind is decided from magic bytes, never the extension -- the same
    fact task 022 pinned for the JSON view holds for the raw route too."""
    registry = _seed(tmp_path, {"payload.bin": PNG_HEADER + b"more"})
    root = registry / "runs" / "raw-run"
    raw = artifact_raw(root, "payload.bin")
    assert raw["contentType"] == "image/png"


def test_artifact_raw_never_written_gets_pull_wording_not_a_crash(tmp_path):
    registry = _seed(tmp_path, {})
    root = registry / "runs" / "raw-run"
    raw = artifact_raw(root, "never/written.png")
    assert raw is not None
    assert raw["contentType"] is None
    assert raw["body"] == ARTIFACT_BINARY.encode()


def test_artifact_raw_refuses_traversal_and_absolute_paths(tmp_path):
    registry = _seed(tmp_path, {"ok.png": PNG_HEADER})
    root = registry / "runs" / "raw-run"
    assert artifact_raw(root, "../../etc/passwd") is None
    assert artifact_raw(root, "/etc/passwd") is None
    assert artifact_raw(root, "") is None


def test_artifact_raw_size_cap_boundary(tmp_path):
    """Exactly at the cap is still served; one byte over answers with the
    `pull` wording instead -- the boundary the acceptance criteria asks for."""
    at_cap = _png(ARTIFACT_INLINE_MAX_BYTES)
    over_cap = _png(ARTIFACT_INLINE_MAX_BYTES + 1)
    registry = _seed(tmp_path, {"at.png": at_cap, "over.png": over_cap})
    root = registry / "runs" / "raw-run"

    raw_at = artifact_raw(root, "at.png")
    assert raw_at["contentType"] == "image/png"
    assert raw_at["body"] == at_cap

    raw_over = artifact_raw(root, "over.png")
    assert raw_over["contentType"] is None
    assert raw_over["body"] == ARTIFACT_BINARY.encode()


def test_artifact_raw_uses_artifact_relpath_as_its_only_resolver(tmp_path, monkeypatch):
    """If a second, ad-hoc path resolver were introduced on this route, this
    test would fail: `artifact_raw`'s resolution is proven to depend
    ENTIRELY on `state.artifact_relpath`'s answer, by making that answer
    disagree with the name actually passed in and checking the disagreement
    wins."""
    import ralphd.engine.state as state_mod

    registry = _seed(tmp_path, {"decoy.png": PNG_HEADER,
                                 "elsewhere/real.png": PNG_HEADER + b"REAL"})
    root = registry / "runs" / "raw-run"

    calls = []

    def fake_relpath(name):
        calls.append(name)
        return "elsewhere/real.png"

    monkeypatch.setattr(state_mod, "artifact_relpath", fake_relpath)
    raw = state_mod.artifact_raw(root, "totally/unrelated/name.png")
    assert calls == ["totally/unrelated/name.png"]
    # The bytes served are the PATCHED resolver's file, not a second lookup
    # keyed off the raw, unpatched name -- proving there is no other
    # resolver on this path.
    assert raw["body"] == PNG_HEADER + b"REAL"

    # And when the resolver refuses outright, artifact_raw refuses too --
    # there is no fallback that would still find "decoy.png".
    monkeypatch.setattr(state_mod, "artifact_relpath", lambda name: None)
    assert state_mod.artifact_raw(root, "decoy.png") is None


# -------------------------------------------------------------------- HTTP-level


def test_hub_raw_route_serves_image_bytes_with_nosniff(tmp_path, ui):
    registry = _seed(tmp_path, {"shot.png": PNG_HEADER + b"tail"})
    server = ui(registry)
    code, ctype, body = _get_raw(server, "/api/runs/raw-run/artifacts-raw/shot.png")
    assert code == 200
    assert ctype == "image/png"
    assert body == PNG_HEADER + b"tail"


def test_hub_raw_route_content_type_is_never_guessed_from_the_extension(tmp_path, ui):
    """A `.txt`-named file whose bytes are actually a PNG still comes back
    as `image/png` -- proving the route never calls `mimetypes.guess_type`
    on the URL/filename."""
    registry = _seed(tmp_path, {"weird.txt": PNG_HEADER + b"tail"})
    server = ui(registry)
    code, ctype, body = _get_raw(server, "/api/runs/raw-run/artifacts-raw/weird.txt")
    assert code == 200
    assert ctype == "image/png"


def test_hub_raw_route_non_image_answers_pull_wording_as_text(tmp_path, ui):
    registry = _seed(tmp_path, {"dump.tar.gz": TAR_GZ_BYTES})
    server = ui(registry)
    code, ctype, body = _get_raw(
        server, "/api/runs/raw-run/artifacts-raw/dump.tar.gz")
    assert code == 200
    assert "text/plain" in ctype
    assert body.decode() == ARTIFACT_BINARY
    assert "pull" in body.decode()


def test_hub_raw_route_404s_for_unknown_run_and_traversal(tmp_path, ui):
    registry = _seed(tmp_path, {"ok.png": PNG_HEADER})
    server = ui(registry)
    code, _ctype, _body = _get_raw(
        server, "/api/runs/nope/artifacts-raw/ok.png")
    assert code == 404
    code, _ctype, _body = _get_raw(
        server, "/api/runs/raw-run/artifacts-raw/..%2F..%2Fetc%2Fpasswd")
    assert code == 404


def test_hub_raw_route_always_sends_nosniff_header(tmp_path, ui):
    registry = _seed(tmp_path, {"shot.png": PNG_HEADER,
                                 "dump.tar.gz": TAR_GZ_BYTES})
    server = ui(registry)
    for path in ("/api/runs/raw-run/artifacts-raw/shot.png",
                 "/api/runs/raw-run/artifacts-raw/dump.tar.gz",
                 "/api/runs/nope/artifacts-raw/ok.png"):
        try:
            with urllib.request.urlopen(f"{server.base}{path}", timeout=5) as resp:
                headers = resp.headers
        except urllib.error.HTTPError as e:
            headers = e.headers
        assert headers.get("X-Content-Type-Options") == "nosniff", path
