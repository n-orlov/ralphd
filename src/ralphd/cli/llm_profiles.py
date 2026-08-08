"""Named LLM profile loading + host-side resolution (PRD req 13).

See docs/llm-profiles.md for the format. `ralphctl start --llm <name>`
resolves a profile file *once*, on the host, before the container starts;
this module implements exactly that resolution (the two built-ins, `host`
and `none`, are handled directly in `main.py` and never touch this module).

Profiles live at `<registry>/llm-profiles/<name>.yaml` (default registry:
`~/.ralphd`, overridable via `RALPHD_REGISTRY` like everything else in this
CLI).
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import yaml

_REF_RE = re.compile(r"^\$\{(env|file|cmd):(.*)\}$", re.DOTALL)

# Placeholder printed by `ralphctl llm show` in place of any value that came
# from the `env:` block or from a `${env:}`/`${file:}`/`${cmd:}` reference
# inside `pi:` -- never the literal secret.
MASK = "***REDACTED***"


class ProfileError(Exception):
    """Missing profile file, malformed YAML, or an unresolvable
    ``${env:}``/``${file:}``/``${cmd:}`` reference. Always carries a
    human-readable diagnostic naming the profile and the offending key."""


def profiles_dir(reg: Path) -> Path:
    return reg / "llm-profiles"


def profile_path(reg: Path, name: str) -> Path:
    return profiles_dir(reg) / f"{name}.yaml"


def list_profile_names(reg: Path) -> list[str]:
    d = profiles_dir(reg)
    if not d.is_dir():
        return []
    return sorted(p.stem for p in d.glob("*.yaml"))


def load_profile_doc(reg: Path, name: str) -> dict:
    path = profile_path(reg, name)
    if not path.is_file():
        raise ProfileError(
            f"llm profile '{name}' not found (looked for {path}; "
            f"built-in profiles are 'host' and 'none')")
    try:
        doc = yaml.safe_load(path.read_text()) or {}
    except yaml.YAMLError as e:
        raise ProfileError(f"llm profile '{name}': invalid YAML in {path}: {e}") from e
    if not isinstance(doc, dict):
        raise ProfileError(f"llm profile '{name}': {path} must be a YAML mapping")
    return doc


def _resolve_ref(ref: str, combined_env: dict, *, name: str, where: str) -> str:
    """Resolve one whole-string ``${env:...}``/``${file:...}``/``${cmd:...}``
    reference. `combined_env` is host env layered with any of the
    profile's own `env:` entries already resolved earlier."""
    body = ref[2:-1]  # strip leading "${" and trailing "}"
    kind, _, arg = body.partition(":")
    if kind == "env":
        if arg not in combined_env:
            raise ProfileError(
                f"llm profile '{name}': {where} references ${{env:{arg}}} but "
                f"env var '{arg}' is not set (checked the host env and the "
                f"profile's own already-resolved `env:` block)")
        return combined_env[arg]
    if kind == "file":
        p = Path(arg).expanduser()
        try:
            return p.read_text().strip()
        except OSError as e:
            raise ProfileError(
                f"llm profile '{name}': {where} references ${{file:{arg}}} but "
                f"the file could not be read: {e}") from e
    if kind == "cmd":
        res = subprocess.run(["bash", "-lc", arg], capture_output=True, text=True)
        if res.returncode != 0:
            raise ProfileError(
                f"llm profile '{name}': {where} references ${{cmd:{arg}}} which "
                f"exited {res.returncode}: {res.stderr.strip()[:300]}")
        return res.stdout.strip()
    raise ProfileError(
        f"llm profile '{name}': {where} has an unsupported reference form "
        f"'${{{kind}:...}}' (supported: env, file, cmd)")


def _resolve_value(value, combined_env: dict, *, name: str, where: str,
                   ref_paths: set[str] | None = None):
    """Resolve one value (recursively for dicts/lists). When `ref_paths` is
    given, records every dotted `where` path whose value came from a
    ``${env:}``/``${file:}``/``${cmd:}`` reference (as opposed to a literal
    in the YAML) -- used by `resolve_profile(..., redact=True)` to mask only
    genuinely-resolved values, leaving literals like `baseUrl` visible."""
    if isinstance(value, str):
        m = _REF_RE.match(value)
        if m:
            if ref_paths is not None:
                ref_paths.add(where)
            return _resolve_ref(value, combined_env, name=name, where=where)
        return value
    if isinstance(value, dict):
        return {k: _resolve_value(v, combined_env, name=name, where=f"{where}.{k}",
                                  ref_paths=ref_paths)
                for k, v in value.items()}
    if isinstance(value, list):
        return [_resolve_value(v, combined_env, name=name, where=f"{where}[{i}]",
                               ref_paths=ref_paths)
                for i, v in enumerate(value)]
    return value


def _mask_ref_paths(value, ref_paths: set[str], where: str):
    """Walk an already-resolved structure, replacing any node whose dotted
    path is in `ref_paths` with `MASK`; everything else (literals) passes
    through unchanged."""
    if where in ref_paths:
        return MASK
    if isinstance(value, dict):
        return {k: _mask_ref_paths(v, ref_paths, f"{where}.{k}") for k, v in value.items()}
    if isinstance(value, list):
        return [_mask_ref_paths(v, ref_paths, f"{where}[{i}]") for i, v in enumerate(value)]
    return value


def expand_mount_host_path(spec: str) -> str:
    """`~`-expand the host side of a `host:container[:ro]` mount spec."""
    parts = spec.split(":", 1)
    if len(parts) != 2:
        return spec
    host, rest = parts
    return f"{Path(host).expanduser()}:{rest}"


def resolve_profile(name: str, reg: Path, host_env: dict | None = None, *,
                    redact: bool = False) -> dict:
    """Load and fully resolve a named profile.

    Returns ``{"description", "model", "fast_model", "env", "mounts",
    "pi"}`` where `env` is a `str -> str` dict of fully-resolved values,
    `mounts` is a list of `host:container[:ro]` strings (host side
    `~`-expanded), and `pi` is the resolved `pi:` fragment (or `None`).

    Raises `ProfileError` (with a diagnostic naming the profile and the
    exact key/reference) on a missing file, malformed YAML, or any
    unresolvable `${env:}`/`${file:}`/`${cmd:}` reference. Resolves each
    reference exactly once, including when `redact=True` -- resolution
    still happens in full (so a broken reference is still caught and
    reported, and later refs can still depend on earlier `env:` entries),
    only the *returned* values are replaced with `MASK`.

    `redact=True` (used by `ralphctl llm show`) masks every `env` value
    unconditionally (env vars are assumed sensitive) and masks only the
    `pi:` fields that were actually filled in from a
    `${env:}`/`${file:}`/`${cmd:}` reference -- literal `pi:` fields such as
    `baseUrl` or a model id stay visible so the resolved shape is still
    useful for diagnosis. `mounts` (host paths) are never masked.
    """
    doc = load_profile_doc(reg, name)
    combined = dict(host_env if host_env is not None else os.environ)
    resolved_env: dict[str, str] = {}
    env_section = doc.get("env") or {}
    if not isinstance(env_section, dict):
        raise ProfileError(f"llm profile '{name}': `env:` must be a mapping")
    for k, v in env_section.items():
        val = _resolve_value(v, combined, name=name, where=f"env.{k}")
        resolved_env[k] = MASK if redact else str(val)
        combined[k] = str(val)  # later env/pi/mounts entries may reference it

    mounts: list[str] = []
    for i, m in enumerate(doc.get("mounts") or []):
        resolved = _resolve_value(m, combined, name=name, where=f"mounts[{i}]")
        mounts.append(expand_mount_host_path(str(resolved)))

    pi_fragment = doc.get("pi")
    if pi_fragment is not None:
        if not isinstance(pi_fragment, dict):
            raise ProfileError(f"llm profile '{name}': `pi:` must be a mapping")
        ref_paths: set[str] = set()
        pi_fragment = _resolve_value(pi_fragment, combined, name=name, where="pi",
                                     ref_paths=ref_paths)
        if redact:
            pi_fragment = _mask_ref_paths(pi_fragment, ref_paths, "pi")

    return {
        "description": doc.get("description"),
        "model": doc.get("model"),
        "fast_model": doc.get("fast_model"),
        "env": resolved_env,
        "mounts": mounts,
        "pi": pi_fragment,
    }
