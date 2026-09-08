"""Black-box tests: skills runtime CRUD API (PRD req 10).

GET /config/skills (list), GET/PUT/DELETE /config/skills/{name}. PUT bodies
are `application/x-tar` and must contain a top-level SKILL.md; mutations take
effect immediately (proven by the stub-pi marker file, which records what's
visible under ~/.pi/agent/skills on every invocation) -- so "effective next
iteration" trivially holds.
"""

from __future__ import annotations

import io
import tarfile
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest
from tests.conftest import LiveRun

from ralphd.engine import skills


@pytest.fixture
def skills_engine(tmp_path):
    procs: list[LiveRun] = []

    def make(job: dict | None = None, stub_env: dict | None = None) -> LiveRun:
        defaults = {"run_id": "skills-e2e", "iterations": 3,
                    "max_approaches": 1, "on_complete": "idle"}
        e = LiveRun(tmp_path, {**defaults, **(job or {})}, stub_env)
        procs.append(e)
        return e

    yield make
    for e in procs:
        e.stop()


def _write_mounted_skill(config_dir: Path, name: str, extra_file: str = "notes.txt") -> None:
    d = config_dir / "skills" / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(f"# {name}\n\nA mounted test skill.\n")
    (d / extra_file).write_text("supporting content\n")


def _make_tar(files: dict[str, str]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tf:
        for name, content in files.items():
            data = content.encode()
            info = tarfile.TarInfo(name=name)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def _put(port: int, path: str, body: bytes) -> tuple[int, bytes]:
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}", method="PUT", data=body,
        headers={"Content-Type": "application/x-tar"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def _get(port: int, path: str) -> tuple[int, bytes]:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=10) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def _delete(port: int, path: str) -> int:
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", method="DELETE")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status
    except urllib.error.HTTPError as exc:
        return exc.code


def test_skills_crud_and_visibility(skills_engine, tmp_path):
    fake_home = tmp_path / "fakehome"
    fake_home.mkdir()
    _write_mounted_skill(tmp_path / "config", "greet")
    # Task cure-01-01 (requirement I): the last step of this node PUTs skill
    # `late` and then waits for a *subsequent* stub invocation to record it in
    # the `.stub-skills` marker (that marker is written at the top of every
    # invocation, before any role work). `wait_api()` returns as soon as
    # /healthz answers, and the job behind it is a `STUB_SLEEP=0` stub over
    # `STUB_TASKS=5` tasks -- on a loaded machine the whole plan can be worked
    # and the run parked in `on_complete: idle` before this test even reaches
    # that PUT, and then no later iteration exists to observe `late` at all.
    # That is exactly the red the dev-layout lanes hit at tree `4baf046`
    # ("'late' skill never observed by an iteration; last seen: ['greet']"),
    # reached through this module's nested subset in
    # tests/test_skills_discoverability_survives_suite.py.
    #
    # Cured with requirement B.1's bounded hold primitive rather than a longer
    # deadline: the stub parks the run's FIRST worker invocation until this
    # test releases it (after the `late` PUT), so further invocations -- and
    # therefore a marker write strictly after the PUT -- provably still lie
    # ahead. The bound is the stub's own `STUB_HOLD_TIMEOUT_S`, so a release
    # this test fails to send is a fast red, never a hang.
    release_worker = tmp_path / "release-worker-hold"
    e = skills_engine(job={"iterations": 20},
                      stub_env={"HOME": str(fake_home), "STUB_SLEEP": "0",
                                "STUB_TASKS": "5",
                                "STUB_HOLD_FILE": str(release_worker),
                                "STUB_HOLD_PHASE": "worker",
                                "STUB_HOLD_TIMEOUT_S": "90"})
    e.wait_api()

    # -- mounted skill visible from the start ------------------------------
    status, body = e.api("GET", "/config/skills")
    assert status == 200
    assert body == [{"name": "greet", "origin": "mounted", "fileCount": 2}]
    assert (fake_home / ".pi" / "agent" / "skills" / "greet" / "SKILL.md").is_file()

    # -- PUT without SKILL.md is rejected -----------------------------------
    bad_tar = _make_tar({"README.md": "no skill file here\n"})
    st, _ = _put(e.port, "/config/skills/broken", bad_tar)
    assert 400 <= st < 500
    status, body = e.api("GET", "/config/skills")
    assert all(s["name"] != "broken" for s in body)

    # -- PUT a valid skill (api origin) -------------------------------------
    good_files = {"SKILL.md": "# fetcher\n\nFetches things.\n",
                  "helper.py": "print('hi')\n"}
    good_tar = _make_tar(good_files)
    st, _ = _put(e.port, "/config/skills/fetcher", good_tar)
    assert st == 204

    status, body = e.api("GET", "/config/skills")
    entry = next(s for s in body if s["name"] == "fetcher")
    assert entry["origin"] == "api"
    assert entry["fileCount"] == 2

    # visible immediately under ~/.pi/agent/skills (so trivially "next
    # iteration" too) -- the actual symlink target, not a stale copy.
    placed = fake_home / ".pi" / "agent" / "skills" / "fetcher"
    assert placed.is_symlink()
    assert (placed / "SKILL.md").read_text() == good_files["SKILL.md"]

    # -- GET round-trips the tar ---------------------------------------------
    st, tar_bytes = _get(e.port, "/config/skills/fetcher")
    assert st == 200
    with tarfile.open(fileobj=io.BytesIO(tar_bytes)) as tf:
        names = sorted(tf.getnames())
        assert names == sorted(good_files)
        for n in names:
            assert tf.extractfile(n).read().decode() == good_files[n]

    # -- DELETE then 404 on repeat --------------------------------------------
    assert _delete(e.port, "/config/skills/fetcher") == 204
    assert not placed.exists()
    assert _delete(e.port, "/config/skills/fetcher") == 404
    st, _ = _get(e.port, "/config/skills/fetcher")
    assert st == 404

    # -- deleting a mounted skill tombstones it (doesn't resurrect) ----------
    assert _delete(e.port, "/config/skills/greet") == 204
    status, body = e.api("GET", "/config/skills")
    assert all(s["name"] != "greet" for s in body)
    assert not (fake_home / ".pi" / "agent" / "skills" / "greet").exists()

    # -- stub-observed visibility: the very next worker iteration sees the
    # current skill set (proves no restart is needed for CRUD to apply).
    st, _ = _put(e.port, "/config/skills/late", _make_tar({"SKILL.md": "# late\n"}))
    assert st == 204
    # The PUT is answered, so every invocation from here on must see `late`:
    # release the held worker iteration (and the iterations behind it).
    release_worker.touch()
    skills_marker = e.run_dir / ".stub-skills"
    deadline = time.time() + 20
    seen: list[str] = []
    while time.time() < deadline:
        if skills_marker.exists():
            seen = skills_marker.read_text().splitlines()
            if "late" in seen:
                break
        time.sleep(0.3)
    assert "late" in seen, f"'late' skill never observed by an iteration; last seen: {seen}"
    assert "greet" not in seen


def test_put_empty_body_rejected(skills_engine, tmp_path):
    e = skills_engine(stub_env={"HOME": str(tmp_path / "fakehome2")})
    e.wait_api()
    st, _ = _put(e.port, "/config/skills/empty", b"")
    assert st == 422


def test_home_isolation_is_an_enforced_invariant_not_a_convention(tmp_path):
    """Every test above passes `stub_env={"HOME": ...}` to isolate the
    child engine's `~/.pi/agent/skills/` from the real ambient $HOME --
    but until task 002 that was purely a *convention* this test module
    followed: nothing in `place_skills()` itself would have complained if
    a call site (this one's own `engine/api.py`, or a future one) forgot
    it and fell back to ambient $HOME (#83). This test asserts the
    invariant directly, at the function boundary skills_engine's helpers
    only exercise indirectly through the API: `place_skills()` refuses to
    run at all without an explicit `home=`, and refuses a `config_dir`
    that isn't the engine's own CONFIG_DIR -- in both cases before
    touching any filesystem, proven by a decoy symlink under a fake home
    that survives the refusal untouched. See
    tests/test_skills_placement_requires_home.py for the full coverage;
    this is the pointer from this module to that enforced invariant."""
    fake_home = tmp_path / "fakehome-isolation-check"
    skills_dir = fake_home / ".pi" / "agent" / "skills"
    skills_dir.mkdir(parents=True)
    source = fake_home / "decoy-source"
    source.mkdir()
    (source / "SKILL.md").write_text("# decoy\n")
    decoy = skills_dir / "decoy"
    decoy.symlink_to(source, target_is_directory=True)

    config_dir = tmp_path / "config"
    config_dir.mkdir()

    with pytest.raises(TypeError):
        skills.place_skills(config_dir)  # no home= -- must not fall back

    foreign_config_dir = tmp_path / "foreign-config"
    foreign_config_dir.mkdir()
    with pytest.raises(ValueError, match="CONFIG_DIR"):
        skills.place_skills(foreign_config_dir, home=fake_home)

    assert decoy.is_symlink() and decoy.exists(), (
        "place_skills() must refuse both bad calls before touching the "
        "fake home at all -- the decoy symlink must survive untouched, "
        "proving this suite's stub_env HOME overrides are backed by an "
        "enforced invariant, not merely a followed convention"
    )
