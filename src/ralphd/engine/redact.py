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

import functools
import json
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

# What a masked value reads as, in ONE place: `ralphctl llm show`'s masked
# profile env (cli/llm_profiles.MASK is this constant) and the redacted
# `job.yaml` a run-document surface prints (task 021, #18.2) say the same word,
# so an operator learns one spelling of "there is a secret here".
MASK = "***REDACTED***"

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


def live_redaction_map() -> dict[str, str]:
    """A COPY of the live in-memory `value -> label` map (task 029, PRD
    requirement I.2, #210). `build_redaction_map` recomputes from the
    environment and the placed creds files; this hands back what
    `refresh_redaction_map` actually made live in THIS engine process --
    what `scrub_text` itself scrubs against -- so the task-boundary commit
    scan (`commit_scan.scan_commits_for_secrets`, reached from
    `LoopSupervisor._scan_task_boundary_commits`) keys on exactly the same
    set of values the rest of the engine already treats as secret, rather
    than a second, independently rebuilt set that could disagree with it.
    A copy, never the live dict: no caller can mutate the map through it.
    """
    with _lock:
        return dict(_map)


def redaction_map_size() -> int:
    """Count only (for logging/diagnostics) -- never the values themselves."""
    with _lock:
        return len(_map)


def scrub_text(text: str, extra: dict[str, str] | None = None) -> str:
    """Replace every occurrence of any known secret value in `text` with
    `[REDACTED:<label>]`. Longest values first, so one secret value that
    happens to be a substring of another isn't left partially exposed.
    Cheap no-op when the redaction set is empty (the common case in tests
    with no creds/LLM secrets configured).

    `extra` adds values known only to THIS caller, merged over the live map.
    The live map is an *engine-process* thing (built by `refresh_redaction_map`
    at startup); a host-side reader like `ralphctl docs` (task 021, #18.2) has
    no live map at all and passes the set it computed itself
    (`build_redaction_map` + `config_dir_secrets`), so one scrubber serves both
    sides instead of a second implementation growing on the host.
    """
    with _lock:
        merged = dict(_map)
    if extra:
        merged.update(extra)
    if not merged:
        return text
    return mask_values(text, {value: f"[REDACTED:{label}]"
                              for value, label in merged.items() if value})


# A run of JSON string-body text holding at least one valid escape: what
# `mask_values` decodes to look inside. It needs no enclosing quotes, so a
# quoted literal's body, a bare escaped body in prose and a body nested inside
# another body are all one shape. Linear: a run only starts where a plain
# character does not precede it (it is maximal, so nothing is lost), and an
# escape always starts with the one character the plain class excludes.
_ESCAPED_RUN_RE = re.compile(
    r'(?<![^"\\\x00-\x1f])(?:[^"\\\x00-\x1f]*\\(?:["\\/bfnrt]|u[0-9a-fA-F]{4}))+[^"\\\x00-\x1f]*')


def mask_values(text: str, replacements: dict[str, str]) -> str:
    """`text` with every value in `replacements` (value -> what replaces it)
    masked however JSON escaping spells it, so the value cannot be recovered by
    decoding the result, at any nesting depth. The one masking routine:
    `scrub_text` and the `codex` wrapper both call it.

    Two layers, longest value first in each:

    1. every spelling of the value that `json.dumps` produces when the value is
       escaped any number of times, `ensure_ascii` on or off at each level
       (`secret_spellings`, bounded by the length of `text`), replaced in place;
    2. every run of JSON string-body text that holds a valid escape -- the body
       of a quoted literal, or a bare body with no quotes around it, in a JSON
       line or in prose -- is decoded as a JSON string body and masked
       recursively (so a document carried as a string inside a document, at
       any depth, and escapes `json.dumps` never writes -- `\\u0022`, a `\\uXXXX`
       for every character, upper-case hex, `\\/`, surrogate pairs -- are seen
       decoded), and is re-encoded only when the decoded text changed, so a
       JSON line stays valid JSON.
    """
    values = {v: r for v, r in replacements.items() if v}
    if not values or not text:
        return text
    # Built once for the whole text: a decoded run is never longer.
    pairs = sorted(((spelling, repl) for value, repl in values.items()
                    for spelling in secret_spellings(value, len(text))),
                   key=lambda kv: -len(kv[0]))
    return _mask(text, pairs)


def _mask(text: str, pairs: list[tuple[str, str]]) -> str:
    for spelling, repl in pairs:
        if spelling in text:
            text = text.replace(spelling, repl)
    if "\\" in text:
        text = _ESCAPED_RUN_RE.sub(lambda m: _mask_run(m.group(0), pairs), text)
    return text


def _mask_run(run: str, pairs: list[tuple[str, str]]) -> str:
    try:
        decoded = json.loads('"' + run + '"')
    except ValueError:
        return run
    # Every escape shortens the text, so the recursion ends.
    masked = _mask(decoded, pairs)
    if masked == decoded:
        return run
    if run.isascii():
        return json.dumps(masked)[1:-1]
    out = json.dumps(masked, ensure_ascii=False)[1:-1]
    if _SURROGATE_RE.search(run):
        return out  # already raw in the text: keep it so
    # A lone surrogate the run spelled as an escape stays one, never raw.
    return _SURROGATE_RE.sub(lambda m: f"\\u{ord(m.group(0)):04x}", out)


_SURROGATE_RE = re.compile("[\ud800-\udfff]")


def secret_spellings(value: str, max_len: int | None = None) -> tuple[str, ...]:
    """`value` plus every way `json.dumps` spells it inside a JSON string when
    it is escaped once, twice or any number of times (a JSON document carried
    as a string inside another, at any depth), `ensure_ascii` off or on at each
    level. Escaping is per character, so each of these appears verbatim in a
    document that nests the value that deep.

    Every spelling that differs from its parent holds a backslash, and the next
    level doubles it, so spellings grow without bound: `max_len` (the length of
    the text being masked, since a longer spelling cannot occur in it) bounds
    the expansion, and None means `len(value)` times 64. A value with no quote,
    backslash, control or non-ASCII character has only the one spelling."""
    bound = max(max_len if max_len is not None else 64 * len(value), len(value), 1)
    return _spellings_upto(value, 1 << (bound - 1).bit_length())


@functools.lru_cache(maxsize=1024)
def _spellings_upto(value: str, bound: int) -> tuple[str, ...]:
    seen = [value]
    known = {value}
    frontier = [value]
    while frontier:
        nxt = []
        for v in frontier:
            for ascii_only in (False, True):
                esc = json.dumps(v, ensure_ascii=ascii_only)[1:-1]
                if esc not in known and len(esc) <= bound:
                    known.add(esc)
                    seen.append(esc)
                    nxt.append(esc)
        frontier = nxt
    return tuple(seen)


# ---------------------------------------------------------------------------
# Task 021 (#18.2): printing a run's *effective* `job.yaml` back to an
# operator, redacted.
#
# Two independent bounds, because either one alone leaks:
#
#   1. by NAME -- any config key whose name is secret-shaped (`api_token`, a
#      hand-written `AWS_SECRET_ACCESS_KEY` inside a nested map) is masked
#      whatever its value is, so a secret this host never knew about (a job.yaml
#      copied in from elsewhere) is still masked;
#   2. by VALUE -- every known secret value is scrubbed out of the rendered
#      text, so a secret smuggled into an innocently-named key
#      (`on_complete_cmd: "curl -H 'Authorization: Bearer ...'"`) is caught
#      too. The value set is this host's own (`build_redaction_map`) plus the
#      run's own config dir (`config_dir_secrets`).
#
# Both are mechanical, for the same reason the rest of this module is (see the
# module doc string): a rule that depends on somebody remembering not to print
# a field has already failed twice in this project.


def is_secret_name(name: str) -> bool:
    """Is `name` a secret-shaped config/env key name? The ONE predicate --
    the same `_NAME_PATTERN`/`KNOWN_LLM_ENV_NAMES` pair that decides which env
    vars are redaction candidates, so "secret-shaped" means one thing here."""
    return bool(_NAME_PATTERN.search(str(name))) or str(name) in KNOWN_LLM_ENV_NAMES


def mask_secret_names(value, name: str = ""):
    """Recursively replace every node reached through a secret-shaped key name
    with `MASK`, leaving everything else untouched (the shape of the document
    is preserved -- an operator still sees THAT a token is configured, exactly
    like `ralphctl llm show` does for a profile's env).

    A `None` is left alone: "not set" is not a secret, and masking it would
    claim a value exists.
    """
    if is_secret_name(name) and value is not None:
        return MASK
    if isinstance(value, dict):
        return {k: mask_secret_names(v, str(k)) for k, v in value.items()}
    if isinstance(value, list):
        return [mask_secret_names(v, name) for v in value]
    return value


def config_dir_secrets(config_dir: Path) -> dict[str, str]:
    """Secret values a run's OWN job config dir holds, host-side (never
    returned to anybody -- only fed to `scrub_text` as an `extra` set).

    Sources, all of them files `ralphctl start` writes for one run:
    `creds/*.env` plus the recognized extras (`git-credentials`, `netrc`),
    `llm-wiring.json`'s resolved env, `env-wiring.json`'s `name=value` pairs,
    and any `apiKey` in the staged `pi/models.json`. Best-effort throughout:
    an unreadable or unexpected file contributes nothing rather than raising,
    because this is a defense-in-depth layer over the name-based masking, not
    a parser contract.
    """
    out: dict[str, str] = {}
    cdir = Path(config_dir)
    creds = cdir / "creds"
    if creds.is_dir():
        for p in sorted(creds.glob("*.env")):
            _from_env_file(p, out)
        _from_git_credentials(creds / "git-credentials", out)
        _from_netrc(creds / "netrc", out)
    wiring = _read_json_dict(cdir / "llm-wiring.json")
    env = wiring.get("env")
    if isinstance(env, dict):
        for name, value in env.items():
            if isinstance(value, str) and len(value) >= MIN_SECRET_LEN:
                out.setdefault(value, f"llm-wiring:{name}")
    extra_env = _read_json_dict(cdir / "env-wiring.json").get("extra_env")
    if isinstance(extra_env, list):
        for pair in extra_env:
            name, _, value = str(pair).partition("=")
            if value and len(value) >= MIN_SECRET_LEN:
                out.setdefault(value, f"env-wiring:{name}")
    _from_pi_models(cdir / "pi" / "models.json", out)
    return out


def _read_json_dict(path: Path) -> dict:
    try:
        doc = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    return doc if isinstance(doc, dict) else {}


def _from_pi_models(path: Path, out: dict[str, str]) -> None:
    """Every `apiKey`-ish string anywhere in a staged pi `models.json`."""
    def walk(node) -> None:
        if isinstance(node, dict):
            for k, v in node.items():
                if isinstance(v, str) and is_secret_name(k) and len(v) >= MIN_SECRET_LEN:
                    out.setdefault(v, f"models.json:{k}")
                else:
                    walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    doc = _read_json_dict(path)
    walk(doc)


def job_yaml_secrets(config_dir: Path | None = None,
                     home: Path | None = None) -> dict[str, str]:
    """The value set used to scrub a rendered `job.yaml`: this host's own
    secrets (`build_redaction_map`) plus the run's config dir, if given."""
    out = dict(build_redaction_map(home))
    if config_dir is not None:
        out.update(config_dir_secrets(config_dir))
    return out


def redact_job_yaml(text: str, *, config_dir: Path | None = None,
                    home: Path | None = None,
                    secrets: dict[str, str] | None = None) -> str:
    """A run's persisted `job.yaml` rendered safe to print (task 021, #18.2).

    `job.yaml` is the `key: <json>` per line format `ralphctl start` writes
    (see `cli/main.py:_read_job_yaml` and `engine/config.py`), so each line is
    re-emitted with its value passed through `mask_secret_names` -- keeping the
    file's own shape and key order, which is the point of showing it at all.
    A line that does not parse is passed through untouched (a hand-edited
    file must still be readable), and the whole result is then value-scrubbed.
    """
    values = secrets if secrets is not None else job_yaml_secrets(config_dir, home)
    lines = []
    for line in text.splitlines():
        key, sep, raw = line.partition(": ")
        if sep and key and not key[:1].isspace():
            try:
                value = json.loads(raw)
            except ValueError:
                lines.append(line)
                continue
            lines.append(f"{key}: {json.dumps(mask_secret_names(value, key))}")
        else:
            lines.append(line)
    out = "\n".join(lines)
    if text.endswith("\n"):
        out += "\n"
    return scrub_text(out, values)
