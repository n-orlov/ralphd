"""Credential placement: read /config/creds and place files under
$HOME/.creds (and conventional extras), entirely inside the engine.

This replaces the shell placement that used to live in
container/entrypoint.sh -- doing it here means it happens under the same
process that already promises never to leak secret *values* into the run
dir, events, stdout, or the job config: we only ever log file *names*, never
contents, and we never call RunDir.emit()/print() with file contents.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
from pathlib import Path

from .config import OVERLAY_DIR

log = logging.getLogger("ralphd.creds")

EXTRA_NAMES = ("gitconfig", "git-credentials", "netrc")


def _home() -> Path:
    return Path(os.environ.get("HOME") or os.path.expanduser("~"))


def api_creds_dir() -> Path:
    """Container-local writable overlay location for runtime `PUT
    /config/creds/{name}` bodies -- mirrors skills.api_skills_dir(). Never
    under the read-only-mounted /config, never under the run dir."""
    return OVERLAY_DIR / "creds"


def creds_deleted_dir() -> Path:
    return OVERLAY_DIR / "creds-deleted"


def clear_creds_tombstone(name: str) -> None:
    """Remove a delete-tombstone for env-cred `name`, if any (called by PUT
    so a re-added cred isn't immediately suppressed again)."""
    marker = creds_deleted_dir() / f"{name}.env"
    if marker.exists():
        marker.unlink()


def _deleted_env_names() -> set[str]:
    ddir = creds_deleted_dir()
    if not ddir.is_dir():
        return set()
    return {p.stem for p in ddir.iterdir() if p.name.endswith(".env")}


def _env_sources(config_dir: Path) -> tuple[dict[str, Path], dict[str, Path]]:
    """Return (mounted, api) env-cred-name -> `<name>.env` path maps."""
    mounted: dict[str, Path] = {}
    mdir = config_dir / "creds"
    if mdir.is_dir():
        for p in sorted(mdir.glob("*.env")):
            mounted[p.stem] = p
    api: dict[str, Path] = {}
    adir = api_creds_dir()
    if adir.is_dir():
        for p in sorted(adir.glob("*.env")):
            api[p.stem] = p
    return mounted, api


def effective_env_source(config_dir: Path, name: str) -> tuple[Path, str] | None:
    """The `<name>.env` file + origin ("api"/"mounted") that is/would be
    placed at `~/.creds/<name>.env`, or None if not visible (deleted or
    never existed)."""
    if name in _deleted_env_names():
        return None
    mounted, api = _env_sources(config_dir)
    if name in api:
        return api[name], "api"
    if name in mounted:
        return mounted[name], "mounted"
    return None


def list_creds(config_dir: Path) -> list[dict]:
    """Effective credential inventory for `GET /config/creds`: name, size,
    mtime -- never values. Deterministic (sorted by name)."""
    mounted, api = _env_sources(config_dir)
    deleted = _deleted_env_names()
    names = sorted((set(mounted) | set(api)) - deleted)
    out = []
    for name in names:
        src, _origin = effective_env_source(config_dir, name)  # type: ignore[misc]
        st = src.stat()
        out.append({"name": name, "size": st.st_size, "mtime": st.st_mtime})
    return out


def _extra_source(config_dir: Path, name: str) -> Path | None:
    """An overlay (api) override wins over the mounted extra file, mirroring
    the env-cred precedence."""
    api_path = api_creds_dir() / name
    if api_path.is_file():
        return api_path
    mounted_path = config_dir / "creds" / name
    if mounted_path.is_file():
        return mounted_path
    return None


def place_creds(config_dir: Path, home: Path | None = None) -> list[str]:
    """Place effective `*.env` creds at `$HOME/.creds/<name>.env` (0600)
    plus recognized extras (gitconfig, git-credentials, netrc, ssh/,
    setup.sh). Mounted (`<config_dir>/creds`) and api-added (runtime `PUT
    /config/creds/{name}`, in the writable overlay) sources are merged --
    api wins on name collision, tombstoned names (`DELETE`) are omitted.
    `~/.creds/*.env` is fully rebuilt on every call (so a DELETE actually
    removes the file, not just stops re-adding it) -- safe/cheap, mirrors
    skills.place_skills(). Returns the list of placed cred *names* (never
    values) for logging by the caller.
    """
    home = home or _home()
    placed: list[str] = []

    dest_creds = home / ".creds"
    mounted, api = _env_sources(config_dir)
    deleted = _deleted_env_names()
    names = sorted((set(mounted) | set(api)) - deleted)
    if names or dest_creds.is_dir():
        dest_creds.mkdir(parents=True, exist_ok=True)
        desired_files = {f"{n}.env" for n in names}
        for existing in dest_creds.glob("*.env"):
            if existing.name not in desired_files:
                existing.unlink()
        for name in names:
            src = api.get(name) or mounted[name]
            dest = dest_creds / f"{name}.env"
            dest.write_bytes(src.read_bytes())
            dest.chmod(0o600)
            placed.append(f"{name}.env")

    creds_dir = config_dir / "creds"

    gitconfig = _extra_source(config_dir, "gitconfig")
    if gitconfig:
        shutil.copy(gitconfig, home / ".gitconfig")
        placed.append("gitconfig")

    git_credentials = _extra_source(config_dir, "git-credentials")
    if git_credentials:
        dest = home / ".git-credentials"
        shutil.copy(git_credentials, dest)
        dest.chmod(0o600)
        subprocess.run(
            ["git", "config", "--global", "credential.helper", "store"],
            check=False, env={**os.environ, "HOME": str(home)},
        )
        placed.append("git-credentials")

    netrc = _extra_source(config_dir, "netrc")
    if netrc:
        dest = home / ".netrc"
        shutil.copy(netrc, dest)
        dest.chmod(0o600)
        placed.append("netrc")

    ssh_src = creds_dir / "ssh"
    if ssh_src.is_dir():
        ssh_dest = home / ".ssh"
        ssh_dest.mkdir(parents=True, exist_ok=True)
        for item in ssh_src.iterdir():
            target = ssh_dest / item.name
            if item.is_dir():
                shutil.copytree(item, target, dirs_exist_ok=True)
            else:
                shutil.copy(item, target)
        os.chmod(ssh_dest, 0o700)
        for root, dirs, files in os.walk(ssh_dest):
            for d in dirs:
                os.chmod(os.path.join(root, d), 0o700)
            for f in files:
                os.chmod(os.path.join(root, f), 0o600)
        placed.append("ssh/")

    setup = creds_dir / "setup.sh"
    if setup.is_file() and os.access(setup, os.X_OK):
        subprocess.run(
            [str(setup)], check=False, cwd=str(home),
            env={**os.environ, "HOME": str(home)},
        )
        placed.append("setup.sh")

    if placed:
        log.info("placed creds: %s", ", ".join(placed))
    return placed
