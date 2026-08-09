"""Mechanical secret-value redaction (operator steering 019 / task 060).

Prompt-only guidance (task 049) already failed twice in this run: task 053
(a worker `cat`-ed `~/.git-credentials`, leaking the GitHub token verbatim
into `output.jsonl`) and task 041/iteration 120 (`docker inspect` on the
production engine container dumped real `AWS_BEARER_TOKEN_BEDROCK` and
friends into the transcript). Both happened *after* task 049's prompt
guidance existed, so the bound has to be mechanical, not advisory -- the
same principle Jenkins credential masking uses: the engine already knows
every secret value it forwards or places, so scrub known values from
everything it persists or serves.

The redaction *set* (the actual secret values) lives in this process's
memory ONLY:
- it is (re)built fresh at engine startup and after any mutation that could
  change forwarded env or placed creds (`PUT`/`DELETE /config/creds/{name}`,
  `PUT /config/llm`);
- it is never written to disk anywhere, and no API route ever returns it
  (only file/var *names*, exactly like the existing creds/llm routes).

Scrub points (defense-in-depth, several independent layers):
1. `runner.py` -- every line of a `pi` subprocess's stdout is scrubbed
   before being appended to that iteration's `output.jsonl`.
2. `state.py` -- every event is scrubbed (as its serialized JSON text)
   before being appended to `events.jsonl`.
3. `api.py` -- `GET /logs` (both tail and follow modes) scrubs again as it
   serves content, using whatever the redaction set currently is (catches
   values that were only *recognized* as secrets after the transcript line
   was originally written, e.g. a cred added mid-run).

Only values at least `MIN_SECRET_LEN` characters long are ever candidates,
so short/common substrings (region codes, single words) are never mangled.
"""

from __future__ import annotations

import os
import re
import threading
from pathlib import Path

from .llm import current_env

# Secret-shaped env var *names* (case-insensitive) that are candidates for
# redaction regardless of source (process env or a `PUT /config/llm`
# override) -- these are the classes of variable that leaked in both real
# incidents (AWS_BEARER_TOKEN_BEDROCK matches TOKEN; a leaked
# AWS_SECRET_ACCESS_KEY would match both SECRET and KEY).
_NAME_PATTERN = re.compile(r"(TOKEN|KEY|SECRET|PASSWORD)", re.IGNORECASE)

# Known LLM provider env var names (mirrors cli/main.py's HOST_LLM_ENV) that
# should be treated as candidates even on a naming convention _NAME_PATTERN
# doesn't happen to catch.
KNOWN_LLM_ENV_NAMES = (
    "ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY",
    "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN",
    "AWS_BEARER_TOKEN_BEDROCK",
)

# Values shorter than this are never redacted -- avoids mangling trivial
# values (region codes, single words, empty strings) that happen to share a
# secret-shaped variable name.
MIN_SECRET_LEN = 8

_lock = threading.Lock()
_map: dict[str, str] = {}  # secret value -> redaction label, memory-only


def _home() -> Path:
    return Path(os.environ.get("HOME") or os.path.expanduser("~"))


def _candidate_names(env: dict) -> set[str]:
    return {k for k in env if _NAME_PATTERN.search(k) or k in KNOWN_LLM_ENV_NAMES}


def _from_env(env: dict, label_prefix: str, out: dict[str, str]) -> None:
    for name in _candidate_names(env):
        value = env.get(name)
        if value and len(value) >= MIN_SECRET_LEN:
            out.setdefault(value, f"{label_prefix}{name}")


def _from_env_file(path: Path, out: dict[str, str]) -> None:
    """Best-effort `KEY=value` parse of a placed `*.env` cred file.
    Unparseable/blank/comment lines are simply skipped -- this is a
    defense-in-depth aid, not a strict parser."""
    try:
        lines = path.read_text().splitlines()
    except OSError:
        return
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if key.startswith("export "):
            key = key[len("export "):].strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if value and len(value) >= MIN_SECRET_LEN:
            out.setdefault(value, f"{path.name}:{key}")


_GIT_CRED_URL_RE = re.compile(r"://[^:/@\s]*:([^@\s]+)@")
_NETRC_PASSWORD_RE = re.compile(r"\bpassword\s+(\S+)", re.IGNORECASE)


def _from_git_credentials(path: Path, out: dict[str, str]) -> None:
    try:
        text = path.read_text()
    except OSError:
        return
    for line in text.splitlines():
        m = _GIT_CRED_URL_RE.search(line)
        if m and len(m.group(1)) >= MIN_SECRET_LEN:
            out.setdefault(m.group(1), "git-credentials")


def _from_netrc(path: Path, out: dict[str, str]) -> None:
    try:
        text = path.read_text()
    except OSError:
        return
    for m in _NETRC_PASSWORD_RE.finditer(text):
        if len(m.group(1)) >= MIN_SECRET_LEN:
            out.setdefault(m.group(1), "netrc")


def build_redaction_map(home: Path | None = None) -> dict[str, str]:
    """Compute a fresh secret-value -> label map. Never touches disk itself
    (callers decide whether/when to make the result the live map via
    `refresh_redaction_map`). Sources:

    - this process's own environment (what every `pi` subprocess inherits)
      plus any `PUT /config/llm` env overrides (`llm.current_env()`),
      filtered to secret-shaped names;
    - values parsed best-effort from placed creds files under
      `<home>/.creds/*.env` and the conventional extras
      (`~/.git-credentials`, `~/.netrc`).
    """
    home = home or _home()
    out: dict[str, str] = {}
    _from_env(dict(os.environ), "env:", out)
    _from_env(current_env(), "env:", out)
    creds_dir = home / ".creds"
    if creds_dir.is_dir():
        for p in sorted(creds_dir.glob("*.env")):
            _from_env_file(p, out)
    git_creds = home / ".git-credentials"
    if git_creds.is_file():
        _from_git_credentials(git_creds, out)
    netrc = home / ".netrc"
    if netrc.is_file():
        _from_netrc(netrc, out)
    return out


def refresh_redaction_map(home: Path | None = None) -> None:
    """(Re)build the in-memory redaction set and make it live. Call at
    engine startup (after creds/skills placement) and after any mutation
    that could change forwarded env or placed creds."""
    new_map = build_redaction_map(home)
    with _lock:
        _map.clear()
        _map.update(new_map)


def redaction_map_size() -> int:
    """Count only (for logging/diagnostics) -- never the values themselves."""
    with _lock:
        return len(_map)


def scrub_text(text: str) -> str:
    """Replace every occurrence of any known secret value in `text` with
    `[REDACTED:<label>]`. Longest values first, so one secret value that
    happens to be a substring of another isn't left partially exposed.
    Cheap no-op when the redaction set is empty (the common case in tests
    with no creds/LLM secrets configured)."""
    with _lock:
        items = sorted(_map.items(), key=lambda kv: -len(kv[0]))
    if not items:
        return text
    for value, label in items:
        if value and value in text:
            text = text.replace(value, f"[REDACTED:{label}]")
    return text
