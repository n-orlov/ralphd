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

from .config import CONFIG_DIR, OVERLAY_DIR

log = logging.getLogger("ralphd.skills")


class InvalidSkillTar(ValueError):
    """Raised when a PUT tar body doesn't contain SKILL.md."""


# Task 003 (finding 3, PRD B.3): the live `telegram-notify` skill pi actually
# reads is `$HOME/.pi/agent/skills/telegram-notify/SKILL.md`, placed by
# `place_skills()` from a host-supplied, untracked mounted directory --
# `examples/skills/telegram-notify/SKILL.md` in THIS repo is a vendored copy
# that is never itself live-mounted anywhere, so hand-editing it can never
# reach the copy pi actually loads. The only repo-owned lever is engine code
# that runs AT placement time, so `place_skills()` injects this instruction
# into any placed `telegram-notify/SKILL.md` that doesn't already carry it,
# leaving every other byte -- and every other skill entirely -- untouched.
NOTIFY_PREFERENCE_MARKER = "Prefer the engine's own `notify` tool when it is present"

NOTIFY_PREFERENCE_INSTRUCTION = (
    "## Prefer the engine's own `notify` tool when it is present\n"
    "\n"
    "If a live `notify` tool is registered for this iteration (the\n"
    "`hat_tools` spool's own send, wired for the worker and steer hats --\n"
    "see `engine/hat_tools.py`), **call that tool instead of POSTing\n"
    "directly**:\n"
    "\n"
    "```json\n"
    '{"text": "...your message..."}\n'
    "```\n"
    "\n"
    "The engine performs the POST itself from its own process, records a\n"
    "`notification.sent` event (`ts`, `endpoint`, `textSha256`,\n"
    "`httpStatus`) and the same entry in `run-record.json`'s notifications\n"
    "section, and answers a failed send with an error rather than raising --\n"
    "the calling task is never failed by a bad or missing notifier. That\n"
    "accounting is the whole reason to prefer it: a direct HTTP push from\n"
    "inside the agent leaves no such record behind.\n"
    "\n"
    "**Fall back to the direct HTTP push below only when no `notify` tool\n"
    "is registered** (a phase this run does not wire it for, or a `pi`\n"
    "invocation outside this harness entirely).\n"
    "\n"
    "_(This section was added by the engine's `skills.place_skills()` at\n"
    "placement time -- task 003, finding 3 -- because it is not present in\n"
    "the mounted source and no tracked file can edit that source.)_\n"
)


def _inject_notify_preference(text: str) -> str:
    """Return `text` with `NOTIFY_PREFERENCE_INSTRUCTION` appended, unless
    it already contains the marker -- in which case `text` is returned
    completely unchanged. Never touches anything before the append point:
    the whole point is that the rest of the placed file stays byte-for-byte
    identical to its source."""
    if NOTIFY_PREFERENCE_MARKER in text:
        return text
    return text.rstrip("\n") + "\n\n" + NOTIFY_PREFERENCE_INSTRUCTION


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


def ambient_home() -> Path:
    """The engine process's own real home (from $HOME, falling back to
    `os.path.expanduser`). Exists so call sites of `place_skills()` resolve
    the home they mean to pass *explicitly*, at the call site, instead of
    relying on an implicit fallback inside `place_skills()` itself (#83,
    task 002) -- the value is identical to the old default, but now every
    caller states in its own source that this is the home it intends."""
    return _home()


def api_skills_dir() -> Path:
    return OVERLAY_DIR / "skills"


def deleted_dir() -> Path:
    return OVERLAY_DIR / "skills-deleted"


def _pi_skills_dir(home: Path | None = None) -> Path:
    return (home or _home()) / ".pi" / "agent" / "skills"


def _notify_preference_staging_dir(home: Path) -> Path:
    """Scratch area (inside `home`, alongside the placement target itself,
    never inside it) where the materialized `telegram-notify` copy lives.
    `place_skills()` symlinks the placed name AT this directory instead of
    at the mounted/api source directly, so `verify_placement()`'s
    'is a symlink to a readable directory' check keeps working unchanged."""
    return home / ".pi" / "agent" / ".skills-notify-preference"


def _materialize_telegram_notify(src: Path, home: Path) -> Path:
    """Rebuild a real (non-symlink) copy of `src` under `home`'s staging
    dir, with `SKILL.md` carrying `NOTIFY_PREFERENCE_INSTRUCTION` if it
    didn't already. Every other file -- and every byte of `SKILL.md`
    that existed in `src` -- is copied unchanged. Returns the staging
    directory `place_skills()` should symlink the placed name at."""
    staging = _notify_preference_staging_dir(home)
    if staging.exists():
        shutil.rmtree(staging)
    shutil.copytree(src, staging)
    skill_md = staging / "SKILL.md"
    if skill_md.is_file():
        skill_md.write_text(_inject_notify_preference(skill_md.read_text()))
    return staging


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


def verify_placement(config_dir: Path, home: Path) -> tuple[list[str], list[str]]:
    """Read-only check of what `place_skills()` last claimed to have done
    (task 003, #83 detection): recompute the same expected name set
    `place_skills()` would place right now (mounted + api, minus
    tombstones) and check each one still resolves at
    `_pi_skills_dir(home)/<name>` -- a symlink that exists and whose
    target is a readable directory.

    Returns `(verified, lost)`, both sorted name lists. `lost` is exactly
    the names whose placement did NOT resolve -- deleted out from under
    the engine, replaced with something that is not a symlink, or
    pointing at a source that no longer exists or isn't readable. Never
    raises for an ordinary missing/broken link; a genuinely surprising
    OSError while `stat`-ing one entry counts that one entry as lost
    rather than aborting the whole check, so one bad name never hides
    the rest of the picture.

    Deliberately independent of `place_skills()`'s own accounting (no
    shared "last placed" state) so a caller can call this as often as it
    likes, from a fresh process even, and get the same answer -- "lost"
    means the filesystem disagrees with what the sources say should be
    there right now, not "disagrees with some remembered snapshot".
    """
    mounted, api = _sources(config_dir)
    deleted = _deleted_names()
    expected = sorted((set(mounted) | set(api)) - deleted)
    target = _pi_skills_dir(home)
    verified: list[str] = []
    lost: list[str] = []
    for name in expected:
        link = target / name
        try:
            ok = link.is_symlink() and link.is_dir()
        except OSError:
            ok = False
        (verified if ok else lost).append(name)
    return verified, lost


def verify_placement_and_report(run, config_dir: Path, home: Path) -> None:
    """`verify_placement()` plus the run-event half of #83's detection
    requirement (task 003): a placement that resolves gets a
    `skills.placement.verified` run EVENT (not just an INFO log line) so
    "the check ran and saw nothing wrong" is itself a fact in
    events.jsonl, not merely the absence of a complaint; a placement that
    doesn't resolve gets a `skills.placement.lost` run event, loud enough
    to grep out of events.jsonl instead of scrolling a log file for it.

    `run` is anything with an `.emit(type_, **data)` method shaped like
    `state.RunDir.emit` -- callers pass the real RunDir; tests can pass a
    stand-in that just records calls.

    Never raises: called both right after `place_skills()` and again at
    every iteration boundary (`LoopSupervisor._run_iteration_once`), so a
    bug in the check itself must not cost an iteration or take down the
    loop -- that would just be a second way to lose the same signal #83
    already showed can go unnoticed.
    """
    try:
        verified, lost = verify_placement(config_dir, home)
    except Exception as exc:  # pragma: no cover -- defensive, see docstring
        log.error("skills placement verification itself failed: %r", exc)
        return
    if verified:
        run.emit("skills.placement.verified", names=verified)
    if lost:
        log.error("skills placement lost (expected but not resolving): %s",
                  ", ".join(lost))
        run.emit("skills.placement.lost", names=lost)


def place_skills(config_dir: Path, *, home: Path) -> list[str]:
    """Rebuild `~/.pi/agent/skills/` from the mounted + api sources (api
    wins on name collision; tombstoned names are omitted). Returns the
    placed names, for logging by the caller (skill *names* only, never
    contents -- skills aren't secret, but keep the same discipline).

    Task 002 (#83 class closure): this function used to default a missing
    `home` to `_home()` (real ambient $HOME) and never checked `config_dir`
    at all, so a caller that forgot `home=` -- as engine/api.py's two PUT/
    DELETE handlers did -- silently rebuilt the *real* $HOME's
    `~/.pi/agent/skills/` from whatever (usually empty, in a throwaway test
    config_dir) sources that caller's `config_dir` happened to have,
    wiping out anything a human or another process had placed there (#83).
    `home` is now a *required* keyword-only argument -- a caller that omits
    it gets a loud `TypeError` before any filesystem write happens, never a
    silent fallback to ambient $HOME. `config_dir` is also checked against
    the engine's own `CONFIG_DIR`: a caller that (by bug or by a future
    call site added elsewhere) passes some other directory gets a loud
    `ValueError` instead of a placement built from the wrong source tree.
    Both checks run before any write to `home`, so refusal never touches
    the caller's real $HOME.

    Task 003 (finding 3, PRD B.3): `telegram-notify` gets one extra step
    before it is symlinked -- `_materialize_telegram_notify()` rebuilds a
    real (non-symlink) copy of its source with `SKILL.md` carrying
    `NOTIFY_PREFERENCE_INSTRUCTION` (unless already present), and
    `target/telegram-notify` is symlinked at THAT copy instead of at the
    mounted/api source directly. Every other name is still symlinked
    straight at its source, unchanged.
    """
    if config_dir != CONFIG_DIR:
        raise ValueError(
            f"place_skills: config_dir must be the engine's own CONFIG_DIR "
            f"({CONFIG_DIR!r}), got {config_dir!r}")
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
        link_target = src
        if name == "telegram-notify":
            link_target = _materialize_telegram_notify(src, home)
        (target / name).symlink_to(link_target, target_is_directory=True)
        placed.append(name)
    if placed:
        log.info("placed skills: %s", ", ".join(placed))
    return placed
