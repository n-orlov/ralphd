"""Runtime LLM env + pi config fragment CRUD (PRD req 10, `PUT /config/llm`).

`ralphctl start --llm <profile>` resolves a profile *once*, on the host, at
container start (docs/llm-profiles.md); this module is the *mid-run* path:
`PUT /config/llm` (called directly -- there is no `ralphctl llm set` wrapper)
lets the operator rotate an expired key / switch endpoints without restarting
the container.

Two pieces, both container-local -- never the run dir, never events.jsonl,
never the job config (mirrors creds.py's secrecy discipline):

- `env`: fully replaces the env-override set, written to
  `<overlay>/llm/env.json`. `LoopSupervisor` reads it fresh via
  `current_env()` every iteration (same pattern as `_creds_note()`) and
  merges it into the next `pi` subprocess's environment.
- `pi`: a `models.json`-shaped fragment (`{"providers": {...}}`),
  deep-merged into `~/.pi/agent/models.json` *immediately* -- the same file
  `pi` itself reads -- so it's in effect for the very next invocation.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from .config import OVERLAY_DIR


def _home() -> Path:
    return Path(os.environ.get("HOME") or os.path.expanduser("~"))


def _env_path() -> Path:
    return OVERLAY_DIR / "llm" / "env.json"


def pi_agent_dir(home: Path | None = None) -> Path:
    return (home or _home()) / ".pi" / "agent"


class UnroutableHatModel(RuntimeError):
    """Raised by `check_hat_routes` (task 009, PRD requirement D, #60
    pillar 4) when a configured hat's model ref names a provider this
    profile's own `~/.pi/agent/models.json` does not declare. The message
    already names the hat, the offending ref, and the provider keys that
    ARE available -- callers (main.py's startup path) need no further
    formatting, just `str(exc)`.
    """


def provider_keys(*, home: Path) -> list[str]:
    """The provider keys this profile's own pi config declares --
    `~/.pi/agent/models.json`'s top-level `providers` object -- read
    locally from disk, never by calling a gateway (requirement D:
    "Reaching the gateway to enumerate models at startup is not
    required"). `[]` when the file is absent, unparsable, or carries no
    `providers` object -- `check_hat_routes` treats that as "nothing
    declared yet", never as "nothing is ever routable" (see its own
    docstring for why).

    `home` is required and keyword-only -- no ambient `$HOME` fallback --
    so a caller that omits it gets a `TypeError` before any filesystem
    read, the same refusal `place_skills()` already enforces (task 002,
    #83) for the same reason: silently rebuilding a caller's home from
    the process's own ambient environment is exactly the failure mode
    that lost a placed skill.
    """
    models_path = pi_agent_dir(home) / "models.json"
    if not models_path.exists():
        return []
    try:
        doc = json.loads(models_path.read_text())
    except (json.JSONDecodeError, OSError):
        return []
    providers = doc.get("providers") if isinstance(doc, dict) else None
    return sorted(providers) if isinstance(providers, dict) else []


def _provider_key(ref: str) -> str:
    """The provider half of a pi `provider/model` ref -- the text before
    the first `/`, or the whole ref when it carries none (an obviously
    unroutable shape either way, so still worth naming in the error)."""
    return ref.split("/", 1)[0]


def _tier_leaf_ref(leaf: object, fallback_ref: str | None) -> str | None:
    """Task 005 (#68): the model half of one `model_tiers[hat][tier]`
    leaf, mirroring `tiers._bundle_from_leaf`'s own model-field resolution
    (a plain string names the ref outright; a mapping may name `model`
    explicitly and otherwise inherits the hat's plain answer; anything
    else inherits unchanged) -- deliberately NOT calling into
    `tiers.default_host_map`/`resolve_bundle` themselves, so this
    read-only startup sanity check stays outside those two functions' own
    guarded "exactly one call site" invariant
    (`tests/test_tier_dispatch.py::test_resolver_has_exactly_one_call_site_
    in_src`), which is about the live DISPATCH pathway (`loop.py`), never
    this validation pass. Thinking/effort are irrelevant to routability so
    this helper, unlike `_bundle_from_leaf`, resolves the model ref alone.
    """
    if isinstance(leaf, str):
        return leaf
    if isinstance(leaf, dict):
        return leaf.get("model", fallback_ref)
    return fallback_ref


def check_hat_routes(cfg, *, home: Path) -> None:
    """Task 009 (PRD requirement D, #60 pillar 4), widened by task 005
    (#68) to every configured tier rung, not just each hat's plain
    `model_for` answer: before iteration 1, resolve every
    `config.MODEL_HATS` entry's plain ref (`cfg.model_for(hat)`, unchanged
    from task 009) AND every explicit `cfg.model_tiers[hat][tier]` rung a
    job.yaml/CLI actually configured, and check each ref's provider key
    against `provider_keys()` -- read from this profile's own
    `~/.pi/agent/models.json`, never a live gateway call, so this check
    adds no network dependency to `start`. Raises `UnroutableHatModel`
    naming the FIRST such rung (plain per-hat refs first in `MODEL_HATS`
    order, then each hat's own tier table in job.yaml's declared order)
    whose ref names an undeclared provider -- the hat, the tier (omitted
    for a plain per-hat ref, which has no tier of its own), the ref
    itself, and the provider keys that ARE available -- so a typo in one
    hat's `model_tiers` table surfaces at start, not three hours in on
    that hat's first dispatch at that tier.

    A `None` ref (`model_for`'s own documented "pi's own default model"
    sentinel) is never checked -- there is no explicit ref to resolve. An
    EMPTY `provider_keys()` (no `models.json`, or one with no `providers`
    object at all) is treated as "this profile hasn't declared anything
    yet" and skips the whole check, rather than failing every explicit ref
    in a dev container or test fixture that never placed a models.json at
    all -- a real `start` always places one (docs/llm-profiles.md's `host`
    profile at minimum copies the host's own), so a real per-hat table
    never hits this branch.

    Only rungs a job.yaml/CLI ACTUALLY named in `model_tiers` are walked
    here -- an unconfigured tier already repeats the exact same ref the
    plain per-hat loop below just checked, so re-deriving `tiers.
    default_host_map`'s full widened ladder (every hat's every tier,
    including ones nothing configured) would only recheck refs already
    covered, at the cost of a second call site into a function this
    module deliberately leaves to `loop.py` alone (see `_tier_leaf_ref`
    above).

    `home` is required and keyword-only -- no ambient `$HOME` fallback --
    same refusal as `provider_keys()` itself (a caller that omits it gets
    a `TypeError` before `provider_keys()` ever reads a filesystem path).
    """
    available = provider_keys(home=home)
    if not available:
        return
    from .config import MODEL_HATS
    for hat in MODEL_HATS:
        ref = cfg.model_for(hat)
        if not ref:
            continue
        key = _provider_key(ref)
        if key not in available:
            raise UnroutableHatModel(
                f"hat {hat!r} is configured with model {ref!r}, whose "
                f"provider {key!r} is not one of the providers this "
                f"profile declares ({', '.join(available)})")

    raw_table = cfg.model_tiers if isinstance(cfg.model_tiers, dict) else {}
    for hat in MODEL_HATS:
        tier_table = raw_table.get(hat)
        if not isinstance(tier_table, dict):
            continue
        fallback_ref = cfg.model_for(hat)
        for tier, leaf in tier_table.items():
            ref = _tier_leaf_ref(leaf, fallback_ref)
            if not ref:
                continue
            key = _provider_key(ref)
            if key not in available:
                raise UnroutableHatModel(
                    f"hat {hat!r} tier {tier!r} is configured with model "
                    f"{ref!r}, whose provider {key!r} is not one of the "
                    f"providers this profile declares "
                    f"({', '.join(available)})")


def current_env() -> dict[str, str]:
    """The env overrides from the last `PUT /config/llm`, if any (`{}`
    otherwise). Read fresh every call so a mid-run rotation is picked up by
    the very next iteration -- no container restart needed."""
    p = _env_path()
    if not p.exists():
        return {}
    try:
        doc = json.loads(p.read_text())
    except (json.JSONDecodeError, OSError):
        return {}
    return doc if isinstance(doc, dict) else {}


def _deep_merge(dest: dict, fragment: dict) -> dict:
    for k, v in fragment.items():
        if isinstance(v, dict) and isinstance(dest.get(k), dict):
            _deep_merge(dest[k], v)
        else:
            dest[k] = v
    return dest


def apply_llm(env: dict | None, pi_fragment: dict | None) -> None:
    """Apply a `PUT /config/llm` body.

    `env` (when not `None`) *replaces* the whole env-override set (matches
    the API's documented "replaces the LLM endpoint configuration"
    semantics). `pi_fragment` (when not `None`) is deep-merged into
    `~/.pi/agent/models.json` -- pi's own provider/model config file -- so
    an operator rotating one provider's key doesn't wipe out the rest of
    the config a profile placed there at container start.

    Values never land anywhere host-visible: `env.json` lives under the
    container-local overlay, and `models.json` lives under `$HOME`, neither
    of which is the run dir or `/config`.
    """
    if env is not None:
        env_path = _env_path()
        env_path.parent.mkdir(parents=True, exist_ok=True)
        env_path.write_text(json.dumps(env))
        os.chmod(env_path, 0o600)

    if pi_fragment:
        agent_dir = pi_agent_dir()
        agent_dir.mkdir(parents=True, exist_ok=True)
        models_path = agent_dir / "models.json"
        doc = {}
        if models_path.exists():
            try:
                doc = json.loads(models_path.read_text()) or {}
            except (json.JSONDecodeError, OSError):
                doc = {}
        _deep_merge(doc, pi_fragment)
        models_path.write_text(json.dumps(doc, indent=1))
        os.chmod(models_path, 0o600)
