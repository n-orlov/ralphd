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

from ..engine.config import PRICE_STRATEGIES, normalize_price_strategy
from ..engine.redact import MASK

_REF_RE = re.compile(r"^\$\{(env|file|cmd):(.*)\}$", re.DOTALL)

# Placeholder printed by `ralphctl llm show` in place of any value that came
# from the `env:` block or from a `${env:}`/`${file:}`/`${cmd:}` reference
# inside `pi:` -- never the literal secret. Re-exported from
# `engine.redact.MASK` (task 021, #18.2): the redacted `job.yaml` a run-document
# surface prints masks values with the same word, and one spelling of "there is
# a secret here" is the point.


class ProfileError(Exception):
    """Missing profile file, malformed YAML, or an unresolvable
    ``${env:}``/``${file:}``/``${cmd:}`` reference. Always carries a
    human-readable diagnostic naming the profile and the offending key."""


class HeaderError(Exception):
    """A malformed ``--llm-header`` spec (no ``Name: value``/``Name=value``
    separator found, or an empty name) or an unresolvable
    ``${env:}``/``${file:}``/``${cmd:}`` reference inside a header's value
    (#177). Always carries a human-readable diagnostic naming the header."""


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

    Returns ``{"description", "model", "fast_model", "price_strategy",
    "env", "mounts", "pi"}`` where `env` is a `str -> str` dict of
    fully-resolved values,
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

    A top-level `headers:` map (#177) is merged into the `headers:` map of
    EVERY provider under the resolved `pi:` fragment's `providers` --
    weaker than that provider's own `headers:` map (a same-named header
    there wins; any other top-level header is still added). Resolves the
    same three reference forms as `env:`, and a header value that came from
    a reference is masked under `redact=True` exactly like a `pi:` field is.
    """
    doc = load_profile_doc(reg, name)
    price_strategy = doc.get("price_strategy")
    if price_strategy is not None and \
            normalize_price_strategy(price_strategy) != str(price_strategy).strip().lower():
        raise ProfileError(
            f"llm profile '{name}': price_strategy: {price_strategy!r} is not one "
            f"of {'|'.join(PRICE_STRATEGIES)}")
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

    # Top-level `headers:` shorthand (#177): a profile-wide map applied to
    # EVERY provider under this profile's own `pi.providers`, weaker than
    # each provider's own `headers:` map (see the merge below -- the
    # provider's own map wins a same name, any other top-level header is
    # still added). Resolved the same way `env:` is, with its own ref-path
    # tracking so `redact=True` masks exactly the header values that came
    # from a reference.
    headers_section = doc.get("headers") or {}
    if not isinstance(headers_section, dict):
        raise ProfileError(f"llm profile '{name}': `headers:` must be a mapping")
    header_ref_paths: set[str] = set()
    top_level_headers = {
        k: str(_resolve_value(v, combined, name=name, where=f"headers.{k}",
                              ref_paths=header_ref_paths))
        for k, v in headers_section.items()
    }

    pi_fragment = doc.get("pi")
    if pi_fragment is not None:
        if not isinstance(pi_fragment, dict):
            raise ProfileError(f"llm profile '{name}': `pi:` must be a mapping")
        ref_paths: set[str] = set()
        pi_fragment = _resolve_value(pi_fragment, combined, name=name, where="pi",
                                     ref_paths=ref_paths)
        if top_level_headers:
            providers = pi_fragment.get("providers")
            if isinstance(providers, dict):
                for pname, pcfg in providers.items():
                    if not isinstance(pcfg, dict):
                        continue
                    existing = pcfg.get("headers") or {}
                    pcfg["headers"] = {**top_level_headers, **existing}
                    for hname in top_level_headers:
                        if hname not in existing and f"headers.{hname}" in header_ref_paths:
                            ref_paths.add(f"pi.providers.{pname}.headers.{hname}")
        if redact:
            pi_fragment = _mask_ref_paths(pi_fragment, ref_paths, "pi")

    return {
        "description": doc.get("description"),
        "model": doc.get("model"),
        "fast_model": doc.get("fast_model"),
        # Task 010 (#14): a gateway profile knows which rate table can price
        # its routes, so the profile is where that belongs -- `ralphctl start`
        # inlines it into job.yaml unless an explicit --price-strategy (or a
        # template/registry default) already decided.
        "price_strategy": price_strategy,
        "env": resolved_env,
        "mounts": mounts,
        "pi": pi_fragment,
    }


# ---------------------------------------------------------- --llm-header (#177)
#
# `ralphctl start --llm-header 'Name: value'` (repeatable; `Name=value` also
# accepted) sets that header on every provider of the run's resolved pi
# config -- both a named profile's `pi.providers` and the `--llm host`
# copied `models.json` -- merged over any header the provider already
# carries so the flag wins on a same-named header and other headers are
# kept. Values accept the same `${env:}`/`${file:}`/`${cmd:}` resolution
# syntax as a profile's own `env:`/`pi:` values, resolved on the host.

def parse_header_spec(spec: str) -> tuple[str, str]:
    """Parse one `--llm-header` argument into `(name, raw_value)`.

    Accepts `Name: value` (a single leading space after the colon is
    stripped, matching ordinary HTTP header notation) and `Name=value`.
    The separator is whichever of `:`/`=` occurs FIRST in the string, so a
    `Name=http://example.com` value (which itself contains a `:`) still
    splits on the intended `=`. Raises `HeaderError` when neither
    separator is present or the name is empty -- the caller's malformed-
    value refusal.
    """
    idx_colon = spec.find(":")
    idx_eq = spec.find("=")
    candidates = [i for i in (idx_colon, idx_eq) if i != -1]
    if not candidates:
        raise HeaderError(
            f"--llm-header {spec!r} is not 'Name: value' or 'Name=value' "
            f"(no ':' or '=' found)")
    idx = min(candidates)
    name, value = spec[:idx], spec[idx + 1:]
    if idx == idx_colon:
        value = value[1:] if value.startswith(" ") else value
    name = name.strip()
    if not name:
        raise HeaderError(f"--llm-header {spec!r}: empty header name")
    return name, value


def _resolve_header_ref(ref: str, combined_env: dict, *, header_name: str) -> str:
    """Resolve one whole-string ``${env:...}``/``${file:...}``/``${cmd:...}``
    reference for a `--llm-header` value -- the same three kinds
    `resolve_profile` supports, with a diagnostic naming the header rather
    than a profile (a header is not scoped to one)."""
    body = ref[2:-1]
    kind, _, arg = body.partition(":")
    if kind == "env":
        if arg not in combined_env:
            raise HeaderError(
                f"--llm-header '{header_name}': references ${{env:{arg}}} but "
                f"env var '{arg}' is not set")
        return combined_env[arg]
    if kind == "file":
        p = Path(arg).expanduser()
        try:
            return p.read_text().strip()
        except OSError as e:
            raise HeaderError(
                f"--llm-header '{header_name}': references ${{file:{arg}}} but "
                f"the file could not be read: {e}") from e
    if kind == "cmd":
        res = subprocess.run(["bash", "-lc", arg], capture_output=True, text=True)
        if res.returncode != 0:
            raise HeaderError(
                f"--llm-header '{header_name}': references ${{cmd:{arg}}} which "
                f"exited {res.returncode}: {res.stderr.strip()[:300]}")
        return res.stdout.strip()
    raise HeaderError(
        f"--llm-header '{header_name}': has an unsupported reference form "
        f"'${{{kind}:...}}' (supported: env, file, cmd)")


def resolve_header_specs(specs: list[str], host_env: dict | None = None) -> dict[str, str]:
    """Parse + resolve a list of `--llm-header` specs into a `name ->
    value` dict, in argument order (a later spec for the same name wins,
    the same last-one-wins rule the flag's own repeatability implies).
    Raises `HeaderError` on a malformed spec or an unresolvable reference.
    """
    combined = dict(host_env if host_env is not None else os.environ)
    out: dict[str, str] = {}
    for spec in specs:
        name, raw_value = parse_header_spec(spec)
        m = _REF_RE.match(raw_value)
        out[name] = (_resolve_header_ref(raw_value, combined, header_name=name)
                     if m else raw_value)
    return out


def merge_headers_into_pi_config(pi_doc: dict | None, headers: dict[str, str]) -> dict | None:
    """Merge `headers` into the `headers:` map of EVERY provider under
    `pi_doc["providers"]`, in place, and return `pi_doc` -- the flag wins on
    a same-named header, any other header the provider already carries is
    kept. A no-op (returns `pi_doc` unchanged) when `headers` is empty or
    `pi_doc` carries no `providers` map (nothing to merge into: `--llm
    none`, or a profile/host models.json with no providers).
    """
    if not headers or not isinstance(pi_doc, dict):
        return pi_doc
    providers = pi_doc.get("providers")
    if not isinstance(providers, dict):
        return pi_doc
    for _pname, pcfg in providers.items():
        if not isinstance(pcfg, dict):
            continue
        existing = pcfg.get("headers") or {}
        pcfg["headers"] = {**existing, **headers}
    return pi_doc
