"""Skills placement + inventory: mirrors creds.py's pattern (PRD req 10).

Two sources feed the effective skill set that pi discovers at
`~/.pi/agent/skills/<name>`:
  - "mounted": `<config_dir>/skills/<name>/` (read-only, from `ralphctl
    start --skills`).
  - "api": `<overlay>/skills/<name>/` (runtime `PUT /config/skills/{name}`),
    which always wins over a same-named mounted skill.
A name can also be tombstoned (`<overlay>/skills-deleted/<name>`) by
`DELETE /config/skills/{name}` so a mounted skill of the same name stops
being resurrected on the next `place_skills()` call.

`place_skills()` rebuilds `~/.pi/agent/skills/` from scratch on every call
(symlinks only, cheap) so it is safe to call at engine startup and again
after every API mutation -- the new/removed skill becomes visible to the
very next iteration (this container never needs a restart for skill CRUD).
"""

from __future__ import annotations

import io
import logging
import os
import shutil
import tarfile
from pathlib import Path

from .config import OVERLAY_DIR

log = logging.getLogger("ralphd.skills")


class InvalidSkillTar(ValueError):
    """Raised when a PUT tar body doesn't contain SKILL.md."""


def tar_dir(src: Path) -> bytes:
    """Tar up a skill directory's *contents* at the archive root (no
    wrapping folder), for GET /config/skills/{name}."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tf:
        for p in sorted(src.rglob("*")):
            if p.is_file():
                tf.add(p, arcname=str(p.relative_to(src)))
    return buf.getvalue()


def extract_skill_tar(body: bytes, dest: Path) -> None:
    """Extract a PUT body (application/x-tar) into `dest`, requiring a
    top-level SKILL.md. Raises InvalidSkillTar otherwise; `dest` is left
    untouched on failure."""
    try:
        with tarfile.open(fileobj=io.BytesIO(body), mode="r:*") as tf:
            names = tf.getnames()
            if "SKILL.md" not in names:
                raise InvalidSkillTar("tar body has no top-level SKILL.md")
            staging = dest.with_name(dest.name + ".staging")
            if staging.exists():
                shutil.rmtree(staging)
            staging.mkdir(parents=True)
            for member in tf.getmembers():
                if member.isdir():
                    continue
                target = (staging / member.name).resolve()
                if not target.is_relative_to(staging.resolve()):
                    raise InvalidSkillTar(f"unsafe tar member path: {member.name}")
            tf.extractall(staging, filter="data")
    except tarfile.TarError as exc:
        raise InvalidSkillTar(f"not a valid tar archive: {exc}") from exc
    if dest.exists():
        shutil.rmtree(dest)
    staging.rename(dest)


def _home() -> Path:
    return Path(os.environ.get("HOME") or os.path.expanduser("~"))


def api_skills_dir() -> Path:
    return OVERLAY_DIR / "skills"


def deleted_dir() -> Path:
    return OVERLAY_DIR / "skills-deleted"


def _pi_skills_dir(home: Path | None = None) -> Path:
    return (home or _home()) / ".pi" / "agent" / "skills"


def clear_tombstone(name: str) -> None:
    """Remove a delete-tombstone for `name`, if any (called by PUT so a
    re-added skill isn't immediately suppressed again)."""
    marker = deleted_dir() / name
    if marker.exists():
        marker.unlink()


def _sources(config_dir: Path) -> tuple[dict[str, Path], dict[str, Path]]:
    """Return (mounted, api) name -> skill-dir maps."""
    mounted: dict[str, Path] = {}
    mdir = config_dir / "skills"
    if mdir.is_dir():
        for p in sorted(mdir.iterdir()):
            if p.is_dir():
                mounted[p.name] = p
    api: dict[str, Path] = {}
    adir = api_skills_dir()
    if adir.is_dir():
        for p in sorted(adir.iterdir()):
            if p.is_dir():
                api[p.name] = p
    return mounted, api


def _deleted_names() -> set[str]:
    ddir = deleted_dir()
    if not ddir.is_dir():
        return set()
    return {p.name for p in ddir.iterdir()}


def effective_source(config_dir: Path, name: str) -> tuple[Path, str] | None:
    """The directory + origin ("api"/"mounted") that would be visible for
    `name`, or None if it isn't visible (deleted or never existed)."""
    if name in _deleted_names():
        return None
    mounted, api = _sources(config_dir)
    if name in api:
        return api[name], "api"
    if name in mounted:
        return mounted[name], "mounted"
    return None


def list_skills(config_dir: Path) -> list[dict]:
    """Effective skill inventory: name, origin, fileCount -- for GET
    /config/skills. Deterministic (sorted by name)."""
    mounted, api = _sources(config_dir)
    deleted = _deleted_names()
    names = sorted((set(mounted) | set(api)) - deleted)
    out = []
    for name in names:
        src, origin = effective_source(config_dir, name)  # type: ignore[misc]
        count = sum(1 for _ in src.rglob("*") if _.is_file())
        out.append({"name": name, "origin": origin, "fileCount": count})
    return out


def place_skills(config_dir: Path, home: Path | None = None) -> list[str]:
    """Rebuild `~/.pi/agent/skills/` from the mounted + api sources (api
    wins on name collision; tombstoned names are omitted). Returns the
    placed names, for logging by the caller (skill *names* only, never
    contents -- skills aren't secret, but keep the same discipline)."""
    home = home or _home()
    target = _pi_skills_dir(home)
    if target.exists():
        for existing in target.iterdir():
            if existing.is_symlink() or existing.is_file():
                existing.unlink()
            else:
                shutil.rmtree(existing)
    else:
        target.mkdir(parents=True, exist_ok=True)

    mounted, api = _sources(config_dir)
    deleted = _deleted_names()
    names = sorted((set(mounted) | set(api)) - deleted)
    placed = []
    for name in names:
        src = api.get(name) or mounted[name]
        (target / name).symlink_to(src, target_is_directory=True)
        placed.append(name)
    if placed:
        log.info("placed skills: %s", ", ".join(placed))
    return placed
