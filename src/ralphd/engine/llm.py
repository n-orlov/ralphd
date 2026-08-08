"""Runtime LLM env + pi config fragment CRUD (PRD req 10, `PUT /config/llm`).

`ralphctl start --llm <profile>` resolves a profile *once*, on the host, at
container start (docs/llm-profiles.md); this module is the *mid-run* path:
`PUT /config/llm` (wrapped by `ralphctl llm set`) lets the operator rotate an
expired key / switch endpoints without restarting the container.

Two pieces, both container-local -- never the run dir, never events.jsonl,
never job.json (mirrors creds.py's secrecy discipline):

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
