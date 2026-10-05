"""#294 (E.1-E.4): the paged, contents-free artifact listing, the capped text
view and the download route.

  * `GET /api/runs/<id>/artifacts?offset=&limit=&q=&dir=` is a page --
    `{total, offset, limit, entries}` -- with `limit` defaulting to 200 and
    clamped to 1000; `q` is a case-insensitive path filter; `dir` lists one
    level with subdirectories as `dir` entries carrying file counts;
  * the listing stats files and never opens one;
  * the text view reads at most `ARTIFACT_PREVIEW_MAX_BYTES`;
  * `GET /api/runs/<id>/artifacts-download/<path...>` returns the full file as
    an attachment;
  * every one of the three routes refuses `..`, `%2e%2e`, a leading `/` and NUL
    through `state.artifact_relpath` alone.
"""

from __future__ import annotations

import builtins
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from ralphd.cli.ui_server import artifact_list, artifact_view
from ralphd.engine import state
from ralphd.engine.state import (
    ARTIFACT_INLINE_MAX_BYTES,
    ARTIFACT_KIND_DIR,
    ARTIFACT_KIND_IMAGE,
    ARTIFACT_PREVIEW_MAX_BYTES,
    artifact_listing,
)

sys.path.insert(0, str(Path(__file__).parent))
from test_cli_ui import UiServer, ui  # noqa: E402

__all__ = ["UiServer", "ui"]

RUN = "page-run"
MIB = 1024 * 1024


def _seed(tmp_path: Path, files: dict[str, bytes] | None = None) -> Path:
    registry = tmp_path / "registry"
    rdir = registry / "runs" / RUN
    adir = rdir / "artifacts"
    adir.mkdir(parents=True)
    (rdir / "status.json").write_text(json.dumps({"runId": RUN,
                                                   "state": "succeeded"}))
    (rdir / "notes.md").write_text("SECRET-NOTES-BODY\n")
    for rel, data in (files or {}).items():
        path = adir / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    return registry


def _many(tmp_path: Path, n: int = 1500) -> Path:
    """`n` small files spread over 3 top-level dirs plus a few loose ones."""
    registry = _seed(tmp_path)
    adir = registry / "runs" / RUN / "artifacts"
    for i in range(n):
        d = adir / ("logs", "reports", "shots")[i % 3]
        d.mkdir(exist_ok=True)
        (d / f"f{i:04d}.txt").write_text("x")
    (adir / "README.MD").write_text("# top\n")
    return registry


def _fetch(server, path):
    try:
        with urllib.request.urlopen(f"{server.base}{path}", timeout=10) as r:
            return r.status, r.headers, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.headers, e.read()


# ----------------------------------------------------------------- listing


def test_listing_pages_a_1500_file_fixture_with_total_clamp_and_last_page(
        tmp_path, ui):
    registry = _many(tmp_path)
    server = ui(registry)

    code, body = server.get(f"/api/runs/{RUN}/artifacts")
    assert code == 200
    assert body["total"] == 1501
    assert body["offset"] == 0 and body["limit"] == 200
    assert len(body["entries"]) == 200
    first = body["entries"][0]
    assert set(first) >= {"path", "size", "mtime", "kind"}

    # clamped to 1000, not honoured
    code, body = server.get(f"/api/runs/{RUN}/artifacts?limit=999999")
    assert body["limit"] == 1000 and len(body["entries"]) == 1000

    # the last page holds the remainder and nothing overlaps the previous one
    code, last = server.get(f"/api/runs/{RUN}/artifacts?offset=1000&limit=1000")
    assert last["total"] == 1501 and last["offset"] == 1000
    assert len(last["entries"]) == 501
    code, prev = server.get(f"/api/runs/{RUN}/artifacts?offset=0&limit=1000")
    assert not {e["path"] for e in prev["entries"]} & \
        {e["path"] for e in last["entries"]}
    assert len({e["path"] for e in prev["entries"]}
               | {e["path"] for e in last["entries"]}) == 1501

    # past the end: an empty page, the same total
    code, past = server.get(f"/api/runs/{RUN}/artifacts?offset=5000")
    assert past["entries"] == [] and past["total"] == 1501


def test_listing_limit_and_offset_garbage_fall_back_to_the_defaults(tmp_path):
    registry = _many(tmp_path, 10)
    page = artifact_listing(registry / "runs" / RUN, offset="zz", limit="no")
    assert page["offset"] == 0 and page["limit"] == 200
    page = artifact_listing(registry / "runs" / RUN, offset=-5, limit=0)
    assert page["offset"] == 0 and page["limit"] == 1


def test_q_filters_by_relative_path_case_insensitively(tmp_path, ui):
    registry = _many(tmp_path)
    server = ui(registry)

    code, body = server.get(f"/api/runs/{RUN}/artifacts?q=readme")
    assert [e["path"] for e in body["entries"]] == ["README.MD"]
    assert body["total"] == 1

    # the match is on the RELATIVE path, so a directory name selects its files
    code, body = server.get(f"/api/runs/{RUN}/artifacts?q=SHOTS/&limit=1000")
    assert body["total"] == 500
    assert all(e["path"].startswith("shots/") for e in body["entries"])

    code, body = server.get(f"/api/runs/{RUN}/artifacts?q=no-such-thing")
    assert body["total"] == 0 and body["entries"] == []


def test_dir_lists_one_level_with_directory_file_counts(tmp_path, ui):
    registry = _many(tmp_path, 30)
    adir = registry / "runs" / RUN / "artifacts"
    (adir / "logs" / "deep").mkdir()
    for i in range(4):
        (adir / "logs" / "deep" / f"d{i}.log").write_text("y")
    server = ui(registry)

    code, top = server.get(f"/api/runs/{RUN}/artifacts?dir=")
    assert code == 200
    by = {e["path"]: e for e in top["entries"]}
    # the flat (no dir) listing is every file; `dir=` is not a one-level view
    assert top["total"] == 31 + 4

    code, body = server.get(f"/api/runs/{RUN}/artifacts?dir=logs")
    assert code == 200
    kinds = {e["path"]: e for e in body["entries"]}
    deep = kinds["logs/deep"]
    assert deep["kind"] == ARTIFACT_KIND_DIR and deep["fileCount"] == 4
    assert deep["name"] == "deep"
    # direct children only: 10 files directly in logs/ plus the one subdir
    assert body["total"] == 11
    assert all("/" not in e["path"][len("logs/"):] for e in body["entries"])
    # directories come first
    assert body["entries"][0]["kind"] == ARTIFACT_KIND_DIR
    assert by  # the unscoped listing is non-empty


def test_dir_entries_exist_for_the_root_view_only_via_dir_children(tmp_path):
    """The one-level view of a top directory carries counts of everything
    beneath each subdirectory, not just its direct files."""
    registry = _seed(tmp_path, {"a/b/c/one.txt": b"1", "a/b/two.txt": b"2",
                                "a/three.txt": b"3"})
    page = artifact_listing(registry / "runs" / RUN, dir="a")
    assert [(e["path"], e["kind"]) for e in page["entries"]] == \
        [("a/b", ARTIFACT_KIND_DIR), ("a/three.txt", "text")]
    assert page["entries"][0]["fileCount"] == 2


def test_kind_comes_from_the_extension_alone(tmp_path):
    registry = _seed(tmp_path, {
        "n.md": b"x", "d.json": b"{}", "p.html": b"<p>", "t.txt": b"x",
        "i.PNG": b"not really a png", "z.bin": b"\x00\x01", "noext": b"x"})
    kinds = {e["path"]: e["kind"] for e in
             artifact_listing(registry / "runs" / RUN)["entries"]}
    assert kinds == {"n.md": "markdown", "d.json": "json", "p.html": "html",
                     "t.txt": "text", "i.PNG": ARTIFACT_KIND_IMAGE,
                     "z.bin": "binary", "noext": "binary"}


def test_listing_opens_no_file(tmp_path, monkeypatch):
    registry = _many(tmp_path, 60)
    root = registry / "runs" / RUN

    def boom(*a, **k):
        raise AssertionError(f"the listing opened a file: {a!r}")

    real_open = builtins.open
    monkeypatch.setattr(builtins, "open", boom)
    monkeypatch.setattr(Path, "open", boom)
    monkeypatch.setattr(Path, "read_bytes", boom)
    monkeypatch.setattr(Path, "read_text", boom)
    try:
        page = artifact_list(registry, RUN)
        sub = artifact_list(registry, RUN, dir="logs", q="f00")
    finally:
        monkeypatch.setattr(builtins, "open", real_open)
    assert page["total"] == 61 and len(page["entries"]) == 61
    assert sub["total"] > 0
    assert artifact_listing(root)["total"] == 61


def test_a_run_with_nothing_says_so_and_a_filtered_miss_does_not(tmp_path):
    registry = _seed(tmp_path)
    assert artifact_list(registry, RUN)["notice"] == state.NO_ARTIFACTS
    registry = _many(tmp_path / "again", 3)
    assert artifact_list(registry, RUN, q="zzz")["notice"] == ""


# ------------------------------------------------------------- capped view


def test_text_view_reads_at_most_1_mib_and_says_it_was_truncated(tmp_path, ui):
    assert ARTIFACT_PREVIEW_MAX_BYTES == MIB
    registry = _seed(tmp_path, {"big.txt": b"abcdefgh" * (3 * MIB // 8)})
    server = ui(registry)

    code, view = server.get(f"/api/runs/{RUN}/artifacts/big.txt")
    assert code == 200
    assert view["truncated"] is True
    assert len(view["content"].encode()) == MIB
    assert view["bytes"] == 3 * MIB
    assert view["kind"] == "text"


def test_the_text_view_never_reads_past_the_cap(tmp_path, monkeypatch):
    # Every byte the view pulls off disk, summed over every open of every
    # file, is at most ARTIFACT_PREVIEW_MAX_BYTES -- the sniff included.
    registry = _seed(tmp_path, {"big.txt": b"z" * (3 * MIB)})
    seen: list[int] = []
    real = Path.open

    class Spy:
        def __init__(self, fh):
            self._fh = fh

        def read(self, n=-1):
            assert n is not None and n >= 0, "an unbounded read"
            data = self._fh.read(n)
            seen.append(len(data))
            return data

        def __getattr__(self, name):
            raise AssertionError(f"unexpected file call {name}")

        def __enter__(self):
            return self

        def __exit__(self, *a):
            self._fh.close()

    monkeypatch.setattr(Path, "open",
                        lambda self, *a, **k: Spy(real(self, *a, **k)))
    view = artifact_view(registry, RUN, "big.txt")
    assert ARTIFACT_PREVIEW_MAX_BYTES == MIB
    assert sum(seen) == MIB, seen
    assert view["truncated"] is True and view["bytes"] == 3 * MIB
    assert len(view["content"].encode()) == MIB


def test_a_file_under_the_cap_is_whole_and_not_truncated(tmp_path):
    registry = _seed(tmp_path, {"small.txt": b"hello\n"})
    view = artifact_view(registry, RUN, "small.txt")
    assert view["truncated"] is False
    assert view["content"] == "hello\n"
    assert view["bytes"] == 6


def test_a_multibyte_character_cut_by_the_cap_leaves_no_replacement_char(tmp_path):
    data = b"a" * (MIB - 1) + "é".encode() + b"tail"
    registry = _seed(tmp_path, {"u.txt": data})
    view = artifact_view(registry, RUN, "u.txt")
    assert view["truncated"] is True
    assert "�" not in view["content"]
    assert view["content"] == "a" * (MIB - 1)


def test_a_file_no_preview_handles_answers_kind_and_size_but_no_content(tmp_path):
    registry = _seed(tmp_path, {"blob.bin": b"\x00\x01\x02" * 100})
    view = artifact_view(registry, RUN, "blob.bin")
    assert view["kind"] == "binary" and view["bytes"] == 300
    assert "content" not in view


def test_images_keep_the_inline_cap():
    assert ARTIFACT_INLINE_MAX_BYTES == 5 * MIB


# ---------------------------------------------------------------- download


def test_download_returns_the_full_bytes_as_an_attachment(tmp_path, ui):
    payload = bytes(range(256)) * (3 * MIB // 256)
    registry = _seed(tmp_path, {"sub/big file.bin": payload,
                                "t.txt": b"plain\n"})
    server = ui(registry)

    code, headers, body = _fetch(
        server, f"/api/runs/{RUN}/artifacts-download/sub/big%20file.bin")
    assert code == 200
    assert body == payload and len(body) == 3 * MIB
    assert headers["Content-Length"] == str(3 * MIB)
    disp = headers["Content-Disposition"]
    assert disp.startswith("attachment;") and "big%20file.bin" in disp
    assert headers["X-Content-Type-Options"] == "nosniff"

    # a percent-encoded single segment works as well
    code, headers, body = _fetch(
        server, f"/api/runs/{RUN}/artifacts-download/sub%2Fbig%20file.bin")
    assert code == 200 and body == payload

    # any kind: text is an attachment too, never inline
    code, headers, body = _fetch(server, f"/api/runs/{RUN}/artifacts-download/t.txt")
    assert code == 200 and body == b"plain\n"
    assert headers["Content-Disposition"].startswith("attachment")


def test_download_of_a_missing_file_or_run_is_404(tmp_path, ui):
    server = ui(_seed(tmp_path, {"a.txt": b"x"}))
    assert _fetch(server, f"/api/runs/{RUN}/artifacts-download/nope.txt")[0] == 404
    assert _fetch(server, "/api/runs/nope/artifacts-download/a.txt")[0] == 404
    # a directory is not a downloadable file
    (tmp_path / "registry" / "runs" / RUN / "artifacts" / "d").mkdir()
    assert _fetch(server, f"/api/runs/{RUN}/artifacts-download/d")[0] == 404


# --------------------------------------------------------------- traversal

BAD_NAMES = {
    "dotdot": "..%2F..%2Fnotes.md",
    "dotdot-plain": "../notes.md",
    "dotdot-nested": "reflection/../../notes.md",
    "encoded-dots": "%2e%2e/%2e%2e/notes.md",
    "encoded-dots-one-segment": "%2e%2e%2f%2e%2e%2fnotes.md",
    "leading-slash": "%2Fetc%2Fpasswd",
    "nul": "a.txt%00.png",
}


@pytest.mark.parametrize("label", sorted(BAD_NAMES))
def test_download_refuses_every_traversal_spelling(tmp_path, ui, label):
    server = ui(_seed(tmp_path, {"a.txt": b"x"}))
    code, _h, body = _fetch(
        server, f"/api/runs/{RUN}/artifacts-download/{BAD_NAMES[label]}")
    assert code == 404, (label, code)
    assert b"SECRET-NOTES-BODY" not in body


@pytest.mark.parametrize("label", sorted(BAD_NAMES))
def test_view_refuses_every_traversal_spelling(tmp_path, ui, label):
    server = ui(_seed(tmp_path, {"a.txt": b"x"}))
    code, _h, body = _fetch(
        server, f"/api/runs/{RUN}/artifacts/{BAD_NAMES[label]}")
    assert code == 404, (label, code)
    assert b"SECRET-NOTES-BODY" not in body


@pytest.mark.parametrize("bad", ["..", "../..", "%2e%2e", "%2e%2e%2f%2e%2e",
                                 "%2Fetc", "a%00b", "sub/../.."])
def test_list_refuses_every_traversal_spelling_in_dir(tmp_path, ui, bad):
    server = ui(_seed(tmp_path, {"sub/a.txt": b"x"}))
    code, _h, body = _fetch(server, f"/api/runs/{RUN}/artifacts?dir={bad}")
    assert code == 404, (bad, code)
    assert b"SECRET-NOTES-BODY" not in body and b"notes.md" not in body


def test_the_new_routes_use_artifact_relpath_and_add_no_second_resolver(
        tmp_path, monkeypatch):
    """The only path guard on the three routes is `state.artifact_relpath`:
    replace it with a recorder and every route consults it."""
    registry = _seed(tmp_path, {"a.txt": b"x"})
    calls: list[str] = []
    real = state.artifact_relpath

    def spy(name):
        calls.append(str(name))
        return real(name)

    monkeypatch.setattr(state, "artifact_relpath", spy)
    root = registry / "runs" / RUN
    assert artifact_listing(root, dir="x")["entries"] == []
    assert state.artifact_download(root, "a.txt") is not None
    from ralphd.engine.state import artifact
    assert artifact(root, "a.txt") is not None
    assert calls == ["x", "a.txt", "a.txt"]
    src = (Path(state.__file__)).read_text()
    for fn in ("def artifact_listing", "def artifact_download"):
        body = src[src.index(fn):]
        body = body[:body.index("\n\n\ndef ")]
        assert "artifact_relpath(" in body
        assert "resolve()" not in body and "is_relative_to" not in body


# ------------------------------------------- the run page's summary (#294 E.6)


def test_the_root_top_view_lists_the_roots_direct_children_with_counts(tmp_path, ui):
    registry = _many(tmp_path, 30)
    server = ui(registry)
    code, body = server.get(f"/api/runs/{RUN}/artifacts?top=1")
    assert code == 200
    assert [(e["path"], e["kind"]) for e in body["entries"][:3]] == [
        ("logs", ARTIFACT_KIND_DIR), ("reports", ARTIFACT_KIND_DIR),
        ("shots", ARTIFACT_KIND_DIR)]
    assert body["entries"][0]["fileCount"] == 10
    assert body["entries"][-1]["path"] == "README.MD"
    assert body["total"] == 4


def test_the_summary_is_a_bounded_count_plus_the_aliases_that_exist(tmp_path, ui):
    registry = _seed(tmp_path, {"reflection/report.md": b"# r\n",
                                "reflection/suggestions.diff": b"d",
                                "other/x.txt": b"x"})
    server = ui(registry)
    code, body = server.get(f"/api/runs/{RUN}/artifacts-summary")
    assert code == 200
    assert body["count"] == 3 and body["capped"] is False and body["notice"] == ""
    assert [(a["key"], a["path"]) for a in body["aliases"]] == [
        ("report", "reflection/report.md"),
        ("suggestions", "reflection/suggestions.diff")]
    assert all(a["sizeDisplay"] and a["title"] for a in body["aliases"])
    assert "entries" not in body            # no page, no bodies
    code, _ = server.get("/api/runs/no-such-run/artifacts-summary")
    assert code == 404


def test_the_summary_of_a_run_with_nothing_says_so(tmp_path, ui):
    server = ui(_seed(tmp_path))
    _, body = server.get(f"/api/runs/{RUN}/artifacts-summary")
    assert body["count"] == 0 and body["aliases"] == []
    assert body["notice"] == state.NO_ARTIFACTS


def test_the_summary_sniffs_no_file_contents(tmp_path, monkeypatch):
    from ralphd.cli.ui_server import artifact_summary_view

    registry = _seed(tmp_path, {"reflection/report.md": b"# r\n",
                                "a/b.bin": b"\x00\x01", "c.txt": b"c"})

    def boom(*a, **k):
        raise AssertionError(f"the summary opened a file: {a!r}")

    real_open = builtins.open
    monkeypatch.setattr(builtins, "open", boom)
    monkeypatch.setattr(Path, "open", boom)
    monkeypatch.setattr(Path, "read_bytes", boom)
    monkeypatch.setattr(Path, "read_text", boom)
    try:
        body = artifact_summary_view(registry, RUN)
    finally:
        monkeypatch.setattr(builtins, "open", real_open)
    assert body["count"] == 3
    assert [a["key"] for a in body["aliases"]] == ["report"]


def test_the_summary_count_walk_stops_at_the_cap(tmp_path, monkeypatch):
    monkeypatch.setattr(state, "ARTIFACT_SUMMARY_COUNT_CAP", 25)
    registry = _many(tmp_path, 60)
    summary = state.artifact_summary(registry / "runs" / RUN)
    assert summary["count"] == 25 and summary["capped"] is True


def test_a_truncated_view_carries_a_server_worded_notice(tmp_path):
    registry = _seed(tmp_path, {"big.txt": b"a" * (2 * MIB),
                                "small.txt": b"hi"})
    big = artifact_view(registry, RUN, "big.txt")
    assert big["truncated"] is True
    assert "truncated" in big["truncationNotice"]
    assert "download" in big["truncationNotice"]
    assert "truncationNotice" not in artifact_view(registry, RUN, "small.txt")
