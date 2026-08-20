"""ralphctl — operate ralphd job containers. See docs/cli.md.

Deliberately stdlib-only (argparse + urllib) so `pipx install ralphd` is light.
"""

from __future__ import annotations

import argparse
import atexit
import contextlib
import functools
import io
import json
import os
import random
import re
import secrets
import shutil
import signal
import socket
import stat as stat_mod
import subprocess
import sys
import tarfile
import tempfile
import termios
import textwrap
import threading
import time
import tty as tty_module
import urllib.error
import urllib.request
from pathlib import Path
from typing import Self

import yaml

from .. import __version__
from ..engine.config import PRICE_STRATEGIES
from ..engine.state import (
    CURRENT_SCHEMA_VERSION,
    NO_ARTIFACTS,
    NONTERMINAL_STATES,
    RUN_DOCUMENT_ABSENT,
    TASK_STATUS_LABELS,
    TERMINAL_STATES,
    artifact,
    artifact_entries,
    artifact_names,
    artifact_text,
    cost_breakdown,
    cost_breakdown_text,
    elapsed_seconds,
    fault_explanation,
    fault_text,
    format_approach,
    format_artifact_listing,
    format_cost,
    format_duration,
    format_iteration_log_header,
    format_local_time,
    format_run_document_listing,
    format_task_counts,
    iteration_detail,
    iteration_summary_lines,
    parse_utc,
    read_operator_termination,
    read_tasks_doc,
    record_operator_termination,
    run_document,
    run_document_keys,
    run_document_text,
    run_documents,
    tasks_read_notice,
    utc_from_epoch,
    utcnow,
)
from ..log_merge import NO_TRANSCRIPT, iteration_numbers
from ..log_merge import iteration_lines as _iteration_lines
from ..log_merge import merged_lines as _merged_lines
from . import llm_profiles, ui_server
from .log_render import FULL_BACKLOG_TAIL as _FULL_BACKLOG_TAIL
from .log_render import _ansi
from .log_render import new_render_state as _new_render_state
from .log_render import render_log_line as _render_log_line
from .log_render import render_to_lines as _render_to_lines

DOCKER = os.environ.get("RALPHD_DOCKER", "docker")
DEFAULT_IMAGE = os.environ.get("RALPHD_IMAGE", "ralphd:dev")

# `ralphctl logs -f`/`logsf` Ctrl+C exit code (task 002, docs/cli.md): the
# shell convention for "terminated by signal N" is 128+N, so SIGINT (2)
# is 130. Chosen (over 0) so a script piping `logs -f` into something else
# can still tell a genuine user interrupt apart from a clean end-of-stream
# exit -- and documented once, here, so nothing else has to guess.
_SIGINT_EXIT_CODE = 130

ADJECTIVES = ("brisk", "calm", "deft", "eager", "fond", "glad", "keen",
              "merry", "nimble", "proud", "quick", "spry", "vivid", "warm")
ANIMALS = ("otter", "lynx", "heron", "badger", "finch", "marten", "newt",
           "osprey", "pika", "stoat", "swift", "tern", "vole", "wren")

# env vars forwarded into the container by `--llm host` when set on the host
# Standard, vendor-documented credential vars forwarded by `--llm host` when
# set. Anything beyond these (endpoint overrides, gateway tokens, exotic SDK
# knobs) is the operator's business: pass `--forward-env NAME` or
# `--forward-env 'PREFIX_*'` explicitly.
HOST_LLM_ENV = ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GEMINI_API_KEY",
                "GOOGLE_API_KEY", "AWS_REGION", "AWS_DEFAULT_REGION",
                "AWS_PROFILE", "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY",
                "AWS_SESSION_TOKEN")
# host pi config files copied into the container's pi config by `--llm host`
HOST_PI_FILES = ("settings.json", "models.json", "auth.json")


def registry() -> Path:
    return Path(os.environ.get("RALPHD_REGISTRY", str(Path.home() / ".ralphd")))


def run_root(run_id: str) -> Path:
    return registry() / "runs" / run_id


def config_root(run_id: str) -> Path:
    return registry() / "configs" / run_id


def gen_run_id() -> str:
    return (f"{random.choice(ADJECTIVES)}-{random.choice(ANIMALS)}-"
            f"{time.strftime('%H%M')}")


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _network_args(network: str | None, api_bind: str, port: int,
                  docker_args: list[str], env_args: list[str]) -> list[str]:
    """Docker networking wiring shared by start/resume.

    Default (no --network): bridge network, publish the engine's :7777 on
    the host via `-p`. `--network host`: the container shares the host's
    network namespace -- port publishing is meaningless there (docker
    ignores -p with a warning), so instead the engine itself is told to
    listen on the chosen host port (RALPHD_PORT) and, because 0.0.0.0 on
    the host netns would expose the API on every interface, to bind to the
    requested --api-bind address (RALPHD_BIND). Any other value is passed
    through as a named docker network with normal port publishing.
    """
    if network == "host":
        docker_args += ["--network", "host"]
        env_args += ["-e", f"RALPHD_PORT={port}",
                     "-e", f"RALPHD_BIND={api_bind}"]
        return []
    if network:
        docker_args += ["--network", network]
    return ["-p", f"{api_bind}:{port}:7777"]


def host_meta(run_id: str) -> dict:
    return _read_json(run_root(run_id) / "host.json", {})


def _read_json(path: Path, default=None):
    """Read one small JSON document off disk, defaulting when absent or
    mid-write.

    Deliberately NOT for `tasks.json` (task 004, #15): that file is rewritten
    non-atomically by the agent, so `JSONDecodeError -> default` silently
    turns a plan being rewritten into no plan. Every task read goes through
    `engine.state.read_tasks_doc()`, which distinguishes absent from
    mid-write and serves the last plan that parsed. Enforced rather than
    documented so it cannot regress quietly.
    """
    if path.name == "tasks.json":
        raise ValueError(
            "read tasks.json through engine.state.read_tasks_doc(persist=False), "
            "not _read_json: an unparseable plan must not become an empty one")
    try:
        return json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def api(run_id: str, method: str, path: str, body: dict | None = None,
        data: bytes | None = None, content_type: str | None = None,
        raw: bool = False, binary: bool = False, timeout: int = 30):
    meta = host_meta(run_id)
    if not meta.get("apiUrl"):
        die(4, f"no API endpoint recorded for run {run_id}")
    req = urllib.request.Request(meta["apiUrl"] + path, method=method)
    token_file = run_root(run_id) / ".api-token"
    if token_file.exists():
        req.add_header("Authorization", f"Bearer {token_file.read_text().strip()}")
    if data is not None:
        req.add_header("Content-Type", content_type or "application/octet-stream")
        req.data = data
    elif body is not None:
        req.add_header("Content-Type", "application/json")
        req.data = json.dumps(body).encode()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            resp_data = resp.read()
            if binary:
                return resp_data
            return resp_data.decode() if raw else (json.loads(resp_data) if resp_data else {})
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")[:500]
        die(5 if e.code == 409 else 1, f"API {e.code}: {detail}")
    except (urllib.error.URLError, TimeoutError) as e:
        die(4, f"API unreachable: {e}")


def _require_run(run_id: str) -> None:
    """Exit 3 (documented 'run not found') before ever touching the API,
    distinct from exit 4 ('container/API unreachable') for a run that
    exists but whose container/API is currently down."""
    if not run_root(run_id).exists():
        die(3, f"run {run_id} not found")


def _tar_skill_dir(src: Path) -> bytes:
    """Tar a skill directory's *contents* at the archive root (no wrapping
    folder) for `PUT /config/skills/{name}` -- mirrors the engine's own
    tar_dir() so round-tripping via `get`/`add` is byte-for-byte shaped the
    same way."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tf:
        for p in sorted(src.rglob("*")):
            if p.is_file():
                tf.add(p, arcname=str(p.relative_to(src)))
    return buf.getvalue()


def die(code: int, msg: str):
    print(f"ralphctl: {msg}", file=sys.stderr)
    sys.exit(code)


def out(args, obj, human: str | None = None):
    if args.json:
        print(json.dumps(obj, indent=2))
    else:
        print(human if human is not None else json.dumps(obj, indent=2))


def sh(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


def docker_sock() -> str:
    """Host docker socket path (RALPHD_DOCKER_SOCK overrides for tests/podman)."""
    return os.environ.get("RALPHD_DOCKER_SOCK", "/var/run/docker.sock")


def job_container_name(run_id: str) -> str:
    """The one place the job container's docker name is spelled.

    Also the value exported as RALPHD_SELF_CONTAINER_ID: the real 64-hex id
    is not known until `docker run` has already returned, and docker accepts
    a name anywhere an id is accepted, so the name we chose ourselves is the
    identifier we can hand to the agent *before* the container exists.
    """
    return f"ralphd-{run_id}"


def _reap_siblings(run_id: str) -> None:
    """Best-effort removal of containers labeled ralphd.run=<run_id>.

    Deliberately filters on the run label ALONE -- ralphctl reaps the job
    container together with its siblings (both carry ralphd.run=<run_id>;
    only the job container also carries ralphd.role=job). That is safe here
    and only here: `stop`/`rm` mean "take this whole run down". Guidance
    given to the agent inside the container must add
    --filter label=ralphd.role=sibling, since the same query run from there
    would destroy the container it is running in (task 035, #7).

    Never fails the caller.
    """
    res = sh([DOCKER, "ps", "-aq", "--filter", f"label=ralphd.run={run_id}"])
    if res.returncode != 0:
        return
    for cid in res.stdout.split():
        sh([DOCKER, "rm", "-f", cid])


def _resolve_pi_apikeys(models_json: Path) -> None:
    """pi supports `apiKey: "!command args"` (shell-out per request). Such
    helpers exist on the host, not in the container — resolve them here and
    forward the literal value instead."""
    doc = _read_json(models_json)
    if not doc:
        return
    changed = False
    for name, provider in (doc.get("providers") or {}).items():
        key = provider.get("apiKey", "")
        if isinstance(key, str) and key.startswith("!"):
            res = sh(["bash", "-lc", key[1:]])
            if res.returncode == 0 and res.stdout.strip():
                provider["apiKey"] = res.stdout.strip()
                changed = True
            else:
                print(f"ralphctl: warning: could not resolve apiKey command for "
                      f"provider '{name}': {res.stderr.strip()[:200]}",
                      file=sys.stderr)
    if changed:
        models_json.write_text(json.dumps(doc, indent=1))
        os.chmod(models_json, 0o600)


def _llm_wiring_path(cdir: Path) -> Path:
    return cdir / "llm-wiring.json"


def _write_llm_wiring(cdir: Path, mode: str, env: dict[str, str],
                      mounts: list[str]) -> None:
    """Persist the *resolved* env vars + extra mounts contributed by
    `--llm` wiring at `start` time (task 058, operator steering 018) so
    `ralphctl resume` can reproduce the exact same wiring on a fresh
    container later, regardless of the operator's current shell env at
    resume time (which may lack the credentials entirely, or have
    different ones). `mode` is `"host"`, a named profile, or `"none"`
    -- informational only, not read back by resume.

    These values are genuinely secret (the same class of value already
    written unencrypted-but-mode-0600 to `<cdir>/pi/models.json` for
    `--llm host`'s resolved `apiKey`, or to `<run-dir>/.api-token`) -- this
    reuses that same at-rest pattern (private file under the job's own
    config dir, 0600, never under the run dir proper, never returned by
    any HTTP route, mounted read-only into the container like everything
    else under `<cdir>`) rather than inventing a new secret-at-rest
    mechanism.
    """
    path = _llm_wiring_path(cdir)
    if not env and not mounts:
        # nothing --llm-wiring-specific to remember (e.g. `--llm none`, or
        # a profile/host env that set nothing) -- leave no file rather
        # than writing an empty one.
        path.unlink(missing_ok=True)
        return
    path.write_text(json.dumps({"mode": mode, "env": env, "mounts": mounts}, indent=2))
    os.chmod(path, 0o600)


def _read_llm_wiring(cdir: Path) -> dict:
    """The `{"env", "mounts"}` `--llm` wiring persisted by `_write_llm_wiring`
    at `start` time, or empty defaults for a run started before task 058 (no
    file at all) or with nothing to reproduce."""
    doc = _read_json(_llm_wiring_path(cdir), {}) or {}
    return {"env": doc.get("env") or {}, "mounts": doc.get("mounts") or []}


def _extra_env_wiring_path(cdir: Path) -> Path:
    return cdir / "env-wiring.json"


# THE default for the per-run `auto_resume` setting (task 026, issue #8,
# PRD req F) -- the *only* place this default value is written down. Every
# other reader (`start`'s flag layering via `_TEMPLATE_SCALAR_FIELDS`, the
# `_read_auto_resume_setting()` fallback for runs started before this
# existed, `doctor --fix`) goes through this constant, and the tests are
# parameterised over it, so flipping the default to ON in a later version
# (docs/roadmap.md's deferred list) is this one line.
AUTO_RESUME_DEFAULT = False


def _auto_resume_path(cdir: Path) -> Path:
    return cdir / "auto-resume.json"


def _as_bool(v) -> bool:
    """Coerce a `true`/`false` scalar from a YAML/JSON/CLI source (which may
    hand us a real bool or its string spelling, e.g. a hand-edited
    `<registry>/config.yaml`) into a bool -- never `bool("false") is True`."""
    if isinstance(v, str):
        return v.strip().lower() in ("1", "true", "yes", "on")
    return bool(v)


def _write_auto_resume_setting(cdir: Path, enabled: bool) -> None:
    """Persist the run's `auto_resume` opt-in alongside its other
    start-time wiring (`llm-wiring.json`, `env-wiring.json`) in the job's
    config dir, so it survives the container it was started with: a
    `ralphctl resume` (whether operator-typed or, per task 027, issued by
    `doctor --fix`) replaces the container but never the config dir, so
    the opt-in a run was started with is still the opt-in that applies.

    Not a secret (unlike its two sibling wiring files) -- a plain readable
    file, since `doctor --fix` has to sweep it for every run in the
    registry.
    """
    _auto_resume_path(cdir).write_text(
        json.dumps({"auto_resume": bool(enabled)}, indent=2) + "\n")


def _read_auto_resume_setting(run_id: str) -> bool:
    """The run's persisted `auto_resume` opt-in, falling back to
    `AUTO_RESUME_DEFAULT` for a run started before task 026 (no file at
    all) or one whose config dir is gone."""
    doc = _read_json(_auto_resume_path(config_root(run_id)), {}) or {}
    if "auto_resume" not in doc:
        return AUTO_RESUME_DEFAULT
    return _as_bool(doc["auto_resume"])


# Task 028 (#8, PRD req F): the auto-resume crash-loop guard. Self-recovery
# that never gives up is a crash loop: a run whose container dies within
# seconds of every resume (broken image, missing credential, corrupt run
# dir) would otherwise be resurrected by every `doctor --fix` tick forever,
# burning the operator's LLM budget and hiding the real defect behind an
# endless stream of fresh containers. So consecutive auto-resume attempts
# are spaced by an escalating backoff and stop entirely after
# AUTO_RESUME_MAX_ATTEMPTS, leaving a readable reason behind.
#
# Both constants live here alone (the tests read them rather than repeating
# the numbers). The schedule's shape mirrors the engine's infra backoff:
# quick first retry (a genuinely transient host hiccup recovers in seconds),
# escalating fast so a hard failure costs at most a handful of containers.
AUTO_RESUME_MAX_ATTEMPTS = 5
AUTO_RESUME_BACKOFF_S = [30, 120, 600, 1800, 3600]


def _auto_resume_state_path(run_id: str) -> Path:
    """The guard's bookkeeping, in the *run* dir (not the config dir, which
    holds the immutable start-time `auto_resume` opt-in): this is mutable
    per-run history, written by whoever runs `doctor --fix` on the host."""
    return run_root(run_id) / "auto-resume.json"


def _read_auto_resume_state(run_id: str) -> dict:
    """The run's `autoResume` record -- {attempts, lastAt, maxAttempts} plus
    the give-up verdict -- normalised so a missing/hand-mangled file reads as
    "no attempts yet" instead of crashing the sweep.

    `iterationsUsed` is the run's iteration count as of the last attempt: it
    is how the guard tells a crash loop (nothing ever progresses) from a run
    that resumed, did real work, and later died again -- the latter resets
    the counter, so a long-lived job is not eventually refused recovery
    because it was recovered four times over its lifetime.
    """
    doc = _read_json(_auto_resume_state_path(run_id), {}) or {}
    try:
        attempts = int(doc.get("attempts") or 0)
    except (TypeError, ValueError):
        attempts = 0
    try:
        max_attempts = int(doc.get("maxAttempts") or AUTO_RESUME_MAX_ATTEMPTS)
    except (TypeError, ValueError):
        max_attempts = AUTO_RESUME_MAX_ATTEMPTS
    used = doc.get("iterationsUsed")
    return {"attempts": max(0, attempts),
            "lastAt": doc.get("lastAt"),
            "maxAttempts": max(1, max_attempts),
            "iterationsUsed": used if isinstance(used, int) else None,
            "gaveUp": bool(doc.get("gaveUp")),
            "reason": doc.get("reason")}


def _write_auto_resume_state(run_id: str, state: dict) -> None:
    path = _auto_resume_state_path(run_id)
    if not path.parent.is_dir():
        return
    path.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n")


def _auto_resume_backoff_s(attempts: int) -> int:
    """Seconds that must pass after `attempts` consecutive failed
    auto-resumes before the next one is allowed (0 before the first)."""
    if attempts <= 0:
        return 0
    return AUTO_RESUME_BACKOFF_S[min(attempts, len(AUTO_RESUME_BACKOFF_S)) - 1]


def _auto_resume_give_up_reason(state: dict) -> str:
    return (f"auto-resume gave up after {state['attempts']} attempts "
            f"(max {state['maxAttempts']}, last attempt "
            f"{state['lastAt'] or 'unknown'}): the run's container keeps "
            f"dying without the run making progress, which is a crash loop, "
            f"not a recoverable outage -- investigate, then delete "
            f"auto-resume.json in the run dir to re-arm auto-recovery "
            f"(or resume manually with `ralphctl resume <run-id>`)")


def _auto_resume_decision(run_id: str) -> tuple[str, dict]:
    """Should `doctor --fix` auto-resume this dangling run right now?

    Returns (verdict, state) with verdict one of:
      "go"      -- resume it (and record the attempt),
      "waiting" -- inside the crash-loop backoff, try again later,
      "gave-up" -- AUTO_RESUME_MAX_ATTEMPTS consecutive attempts made no
                   progress; auto-recovery is off for this run until an
                   operator clears the record.
    The returned state is already progress-reset where applicable, so the
    caller can write it back as-is.
    """
    state = _read_auto_resume_state(run_id)
    status = _read_json(run_root(run_id) / "status.json", {}) or {}
    used = status.get("iterationsUsed")
    if (state["attempts"] and isinstance(used, int)
            and state["iterationsUsed"] is not None
            and used > state["iterationsUsed"]):
        # the run got further after the last auto-resume: whatever killed it
        # this time is a new incident, not the same crash loop
        state = {"attempts": 0, "lastAt": None,
                 "maxAttempts": state["maxAttempts"], "iterationsUsed": None,
                 "gaveUp": False, "reason": None}
    if state["attempts"] >= state["maxAttempts"]:
        return "gave-up", state
    wait_s = _auto_resume_backoff_s(state["attempts"])
    since = elapsed_seconds(state["lastAt"]) if state["lastAt"] else None
    if since is not None and since < wait_s:
        return "waiting", state
    return "go", state


def _auto_resume_next_attempt_at(state: dict) -> str | None:
    if not state["lastAt"]:
        return None
    try:
        return utc_from_epoch(parse_utc(str(state["lastAt"]))
                              + _auto_resume_backoff_s(state["attempts"]))
    except (ValueError, TypeError):
        return None


def _write_extra_env_wiring(cdir: Path, pairs: list[str]) -> None:
    """Persist the *resolved* `name=value` pairs contributed by
    `--forward-env`, `--llm-env`, and `--env` at `start` time (task 001,
    same defect class as `_write_llm_wiring`/task 058) so `ralphctl resume`
    can reproduce them byte-for-byte later regardless of the resuming
    shell's own environment. `pairs` is kept in the exact order the flags
    were applied at `start` (forward-env, then llm-env, then env) so a
    later duplicate name wins on replay exactly as it did the first time
    (docker `-e NAME=A -e NAME=B` -- last one wins).

    Same at-rest pattern as `llm-wiring.json`: private file under the job's
    own config dir, 0600, never under the run dir proper, never returned by
    any HTTP route.
    """
    path = _extra_env_wiring_path(cdir)
    if not pairs:
        path.unlink(missing_ok=True)
        return
    path.write_text(json.dumps({"extra_env": pairs}, indent=2))
    os.chmod(path, 0o600)


def _read_extra_env_wiring(cdir: Path) -> list[str]:
    """The ordered `name=value` list persisted by `_write_extra_env_wiring`,
    or an empty list for a run started before task 001 (no file at all)."""
    doc = _read_json(_extra_env_wiring_path(cdir), {}) or {}
    return list(doc.get("extra_env") or [])


# recognized creds extras copied verbatim alongside *.env files
_CREDS_EXTRA_FILES = ("gitconfig", "git-credentials", "netrc", "setup.sh")


def _copy_creds(src: Path, cdir: Path) -> None:
    """Copy `*.env` plus recognized extras (gitconfig, git-credentials,
    netrc, ssh/, setup.sh) from `src` into the job config dir's `creds/`.
    Anything else in `src` is ignored. The engine (not this CLI) places
    these under $HOME/.creds etc. at container startup -- this only stages
    the config-dir copy that gets mounted read-only at /config.
    """
    src = src.expanduser().resolve()
    if not src.is_dir():
        die(2, f"--creds: {src} is not a directory")
    dest = cdir / "creds"
    dest.mkdir(exist_ok=True)
    for env_file in sorted(src.glob("*.env")):
        shutil.copy2(env_file, dest / env_file.name)
    for name in _CREDS_EXTRA_FILES:
        f = src / name
        if f.is_file():
            shutil.copy2(f, dest / name)
            if name == "setup.sh":
                (dest / name).chmod((dest / name).stat().st_mode | 0o100)
    ssh_src = src / "ssh"
    if ssh_src.is_dir():
        shutil.copytree(ssh_src, dest / "ssh", dirs_exist_ok=True)


def _copy_skills(sdir: str, cdir: Path) -> None:
    """Validate and copy one --skills argument into the job config dir's
    `skills/`. `src` must either:
      - contain a `SKILL.md` itself (one skill), or
      - be a directory whose every immediate subdirectory contains a
        `SKILL.md` (a directory-of-skills, expanded to one copy per child).
    Anything else is a usage error (exit 2), naming the offending path.
    """
    src = Path(sdir).expanduser().resolve()
    if not src.is_dir():
        die(2, f"--skills: {src} is not a directory")
    (cdir / "skills").mkdir(exist_ok=True)
    if (src / "SKILL.md").is_file():
        shutil.copytree(src, cdir / "skills" / src.name, dirs_exist_ok=True)
        return
    children = sorted(p for p in src.iterdir() if p.is_dir())
    if children and all((c / "SKILL.md").is_file() for c in children):
        for c in children:
            shutil.copytree(c, cdir / "skills" / c.name, dirs_exist_ok=True)
        return
    die(2, f"--skills: {src} is neither a skill (no SKILL.md) nor a directory "
           f"of skills (not every immediate child has a SKILL.md)")


_WORKSPACE_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]+$")


def _parse_workspace_specs(specs: list[str]) -> list[tuple[str | None, Path]]:
    """Parse repeatable `--workspace DIR[:NAME]` values into (name-or-None,
    resolved host path) pairs, validating each dir exists. A bare `DIR` (no
    `:NAME`) is unnamed -- when it is the *only* entry it mounts at
    /workspace exactly as before; two or more entries each require a name
    (enforced by the caller, since a single named entry is legal too).
    """
    out: list[tuple[str | None, Path]] = []
    for spec in specs:
        raw, name = spec, None
        if ":" in spec:
            head, _, tail = spec.rpartition(":")
            if head and _WORKSPACE_NAME_RE.match(tail):
                raw, name = head, tail
        ws = Path(raw).expanduser().resolve()
        if not ws.is_dir():
            die(2, f"workspace {ws} is not a directory")
        out.append((name, ws))
    return out


# ---------------------------------------------------------------- start
# job-config fields a template's job.yaml may default; explicit CLI flags
# (checked via `is not None`/falsy, since every one of these argparse
# options defaults to None/empty when omitted -- see the `start` subparser)
# always win over the template.
_TEMPLATE_SCALAR_FIELDS = {
    "iterations": 25, "max_approaches": 3, "vigilant": False,
    "reflect": False, "on_complete": "exit", "on_complete_cmd": None, "timeout": 480,
    "iteration_timeout": 45, "model_strategy": "quality-first", "llm": "host",
    "model": None, "fast_model": None, "thinking": None,
    # Task 010 (#14): None here means "nobody on the host decided" -- the
    # profile may then supply one, and if nothing does the key is omitted
    # from job.yaml so the engine's own default (`none`) applies. The default
    # lives in engine/config.DEFAULT_PRICE_STRATEGY alone.
    "price_strategy": None,
    "image": DEFAULT_IMAGE, "network": None,
    # default lives in AUTO_RESUME_DEFAULT alone (task 026) -- never a
    # second literal here
    "auto_resume": AUTO_RESUME_DEFAULT,
}

# For these `start` scalar fields, `ralphctl config set <regkey> ...` (task
# 038) supplies a registry-wide fallback that sits BETWEEN a template's
# value and the hardcoded fallback above: explicit CLI flag > template >
# `<registry>/config.yaml` > hardcoded default. `llm`'s registry key is
# named `default_llm_profile` (matches doctor's existing read of it);
# the others share their field name.
_REGISTRY_CONFIG_FIELD_KEYS = {"image": "image", "on_complete": "on_complete",
                               "llm": "default_llm_profile", "network": "network",
                               "auto_resume": "auto_resume",
                               "price_strategy": "price_strategy"}


def _apply_template(args) -> Path | None:
    """Load `<registry>/templates/<name>/` (PRD req 25) and fill in any
    `start` flag the caller left at its argparse default with the
    template's value, then any registry-wide default (`ralphctl config`,
    task 038), then the hardcoded fallback -- run unconditionally (even
    with no `--template`) since these flags now default to None in the
    argparse `start` subparser, so this is the single place that fills in
    their real defaults. Mutates `args` in place; returns the template dir
    (for `--prd`/`--skills`/`--creds` defaulting below) or None if no
    `--template` was given.
    """
    tdir = None
    cfg = {}
    if args.template:
        tdir = registry() / "templates" / args.template
        if not tdir.is_dir():
            die(3, f"unknown template: {args.template} (expected {tdir})")
        cfg_file = tdir / "job.yaml"
        if cfg_file.is_file():
            cfg = yaml.safe_load(cfg_file.read_text()) or {}
            if not isinstance(cfg, dict):
                die(2, f"template {args.template}: job.yaml must be a mapping")
    reg_cfg = _registry_config(registry())
    for key, hard_default in _TEMPLATE_SCALAR_FIELDS.items():
        if getattr(args, key) is None:
            reg_key = _REGISTRY_CONFIG_FIELD_KEYS.get(key)
            fallback = reg_cfg.get(reg_key, hard_default) if reg_key else hard_default
            setattr(args, key, cfg.get(key, fallback))
    # the one non-string scalar that can arrive from a template/registry
    # YAML as either a bool or its string spelling
    args.auto_resume = _as_bool(args.auto_resume)
    if not args.skills:
        skill_names = cfg.get("skills") or []
        args.skills = [str(tdir / s) for s in skill_names] or None
    if not args.creds and cfg.get("creds"):
        args.creds = str(tdir / cfg["creds"])
    if args.prd is None and tdir is not None:
        prd_name = cfg.get("prd", "prd.md")
        prd_path = tdir / prd_name
        if prd_path.is_file():
            args.prd = str(prd_path)
    return tdir


def cmd_start(args):
    _apply_template(args)
    if args.prd is None:
        die(2, "--prd is required (or use --template with a prd.md skeleton)")
    run_id = args.run_id or gen_run_id()
    rdir = run_root(run_id)
    if rdir.exists() and any(rdir.iterdir()):
        die(2, f"run {run_id} already exists")
    cdir = config_root(run_id)
    rdir.mkdir(parents=True, exist_ok=True)
    cdir.mkdir(parents=True, exist_ok=True)
    os.chmod(cdir, 0o700)

    prd = sys.stdin.read() if args.prd == "-" else Path(args.prd).read_text()
    (cdir / "prd.md").write_text(prd)

    token = None
    if args.api_token:
        token = secrets.token_urlsafe(24) if args.api_token == "auto" else args.api_token
        tf = rdir / ".api-token"
        tf.write_text(token)
        os.chmod(tf, 0o600)

    # Named LLM profile (docs/llm-profiles.md): resolved once, here, before
    # anything else touches it -- job.yaml's model/fast_model defaults and
    # the later env/mounts/pi wiring both read from this single resolution
    # so a `${cmd:...}` reference is never shelled out twice.
    llm_profile = None
    if args.llm not in ("host", "none"):
        try:
            llm_profile = llm_profiles.resolve_profile(args.llm, registry())
        except llm_profiles.ProfileError as e:
            die(2, str(e))

    job = {
        "run_id": run_id,
        "iterations": args.iterations,
        "max_approaches": args.max_approaches,
        "vigilant": args.vigilant,
        "on_complete": args.on_complete,
        "on_complete_cmd": args.on_complete_cmd,
        "reflect": args.reflect,
        "model": args.model or (llm_profile.get("model") if llm_profile else None),
        "fast_model": args.fast_model or (llm_profile.get("fast_model") if llm_profile else None),
        "model_strategy": args.model_strategy,
        "thinking": args.thinking,
        "iteration_timeout_s": args.iteration_timeout * 60,
        "job_timeout_s": args.timeout * 60,
        # omitted when the flag is absent (the `if v is not None` filter
        # below), so the engine's JobConfig default applies -- the default
        # lives in engine/config.py alone, never duplicated here.
        "infra_outage_budget_s": args.infra_outage_budget,
        # Task 052 (#10): the host-side pricing map (`pricing:` in
        # `<registry>/config.yaml`) is inlined here, at start, so the engine
        # inside the container sees it and a `ralphctl resume` keeps using the
        # exact rates the run started with. Absent key -> no map -> an
        # unpriced iteration stays *unknown* (never a made-up $0).
        "pricing": _registry_config(registry()).get("pricing") or None,
        # Task 010 (#14): which built-in rate table may derive a cost for an
        # unpriced route. Explicit flag > template > registry config.yaml >
        # the profile's own `price_strategy:` (a gateway profile knows what
        # bills it) > omitted, i.e. the engine default `none`. Persisted here
        # so `ralphctl resume` (which re-runs this same job.yaml) keeps the
        # strategy the run started with.
        "price_strategy": args.price_strategy or (
            llm_profile.get("price_strategy") if llm_profile else None),
    }
    (cdir / "job.yaml").write_text(
        "".join(f"{k}: {json.dumps(v)}\n" for k, v in job.items() if v is not None))

    port = args.port or free_port()
    env_args: list[str] = []
    mounts = [
        "-v", f"{rdir}:/run/ralphd",
        "-v", f"{cdir}:/config:ro",
    ]
    ws: Path | None = None          # single-unnamed workspace (legacy path)
    ws_named: dict[str, Path] = {}  # name -> host path (multi-workspace)
    ws_specs = _parse_workspace_specs(args.workspace or [])
    if len(ws_specs) == 1 and ws_specs[0][0] is None:
        ws = ws_specs[0][1]
        mounts += ["-v", f"{ws}:/workspace"]
    else:
        for name, path in ws_specs:
            if name is None:
                die(2, "when more than one --workspace is given, every one "
                       "needs a name: --workspace DIR:NAME")
            mounts += ["-v", f"{path}:/workspace/{name}"]
            ws_named[name] = path
        if ws_named:
            env_args += ["-e", f"RALPHD_WORKSPACES={','.join(ws_named)}"]

    # ralphd.run reaps the whole run; ralphd.role distinguishes THIS
    # container from siblings the job starts, so cleanup guidance handed to
    # the agent can exclude it (task 034, #7).
    docker_args: list[str] = ["--label", f"ralphd.run={run_id}",
                              "--label", "ralphd.role=job"]
    env_args += ["-e", f"RALPHD_SELF_CONTAINER_ID={job_container_name(run_id)}"]
    port_args = _network_args(args.network, args.api_bind, port,
                              docker_args, env_args)
    if args.allow_docker:
        sock = docker_sock()
        try:
            st = os.stat(sock)
        except OSError:
            die(2, f"--allow-docker: docker socket {sock} not found")
        if not stat_mod.S_ISSOCK(st.st_mode):
            die(2, f"--allow-docker: {sock} is not a socket")
        docker_args += ["-v", f"{sock}:/var/run/docker.sock",
                        "--group-add", str(st.st_gid)]
        if ws is not None:
            env_args += ["-e", f"RALPHD_HOST_WORKSPACE={ws}"]
        elif ws_named:
            hostwss = json.dumps({n: str(p) for n, p in ws_named.items()})
            env_args += ["-e", f"RALPHD_HOST_WORKSPACES={hostwss}"]
        env_args += ["-e", f"RALPHD_HOST_RUN_DIR={rdir}",
                     "-e", f"RALPHD_RUN_ID={run_id}"]
        print("ralphctl: WARNING: --allow-docker mounts the host docker socket "
              "into the job container. The docker socket is ROOT-EQUIVALENT "
              "access to this host — the job can mount any host path and run "
              "privileged containers. Only use with PRDs you trust.",
              file=sys.stderr)
    for sdir in args.skills or []:
        _copy_skills(sdir, cdir)

    if args.creds:
        _copy_creds(Path(args.creds), cdir)

    # LLM wiring
    if args.llm == "host":
        pi_dir = Path.home() / ".pi" / "agent"
        dest = cdir / "pi"
        dest.mkdir(exist_ok=True)
        for name in HOST_PI_FILES:
            f = pi_dir / name
            if f.exists():
                shutil.copy2(f, dest / name)
        _resolve_pi_apikeys(dest / "models.json")
        host_env = {}
        for var in HOST_LLM_ENV:
            if os.environ.get(var):
                env_args += ["-e", f"{var}={os.environ[var]}"]
                host_env[var] = os.environ[var]
        aws = Path.home() / ".aws"
        host_mounts = []
        if aws.is_dir():
            mounts += ["-v", f"{aws}:/home/agent/.aws:ro"]
            host_mounts.append(f"{aws}:/home/agent/.aws:ro")
        _write_llm_wiring(cdir, "host", host_env, host_mounts)
    elif args.llm != "none":
        assert llm_profile is not None  # resolved (or died) above
        for k, v in llm_profile["env"].items():
            env_args += ["-e", f"{k}={v}"]
        for m in llm_profile["mounts"]:
            mounts += ["-v", m]
        if llm_profile["pi"]:
            dest = cdir / "pi"
            dest.mkdir(exist_ok=True)
            (dest / "models.json").write_text(json.dumps(llm_profile["pi"], indent=1))
            os.chmod(dest / "models.json", 0o600)
        _write_llm_wiring(cdir, args.llm, llm_profile["env"], llm_profile["mounts"])
    else:
        _write_llm_wiring(cdir, "none", {}, [])
    # Resolved `name=value` pairs from --forward-env/--llm-env/--env, in
    # the exact order applied below -- persisted so `resume` can reproduce
    # them byte-for-byte later regardless of the resuming shell's own
    # environment (task 001, same defect class as the --llm wiring above).
    extra_env: list[str] = []
    for pattern in args.forward_env or []:
        if pattern.endswith("*"):
            names = [k for k in os.environ if k.startswith(pattern[:-1])]
        else:
            names = [pattern] if os.environ.get(pattern) else []
            if not names:
                print(f"ralphctl: warning: --forward-env {pattern} not set on host",
                      file=sys.stderr)
        for name in names:
            entry = f"{name}={os.environ[name]}"
            env_args += ["-e", entry]
            extra_env.append(entry)
    for kv in args.llm_env or []:
        env_args += ["-e", kv]
        extra_env.append(kv)
    for kv in args.env or []:
        env_args += ["-e", kv]
        extra_env.append(kv)
    _write_extra_env_wiring(cdir, extra_env)
    # Host-side setting: the engine never resumes itself, so this is only
    # ever read back by `ralphctl` (task 027's `doctor --fix`); it is
    # deliberately not passed into the container.
    _write_auto_resume_setting(cdir, args.auto_resume)
    if token:
        env_args += ["-e", f"RALPHD_API_TOKEN={token}"]

    cmd = [DOCKER, "run", "-d", "--name", job_container_name(run_id),
           "--init", *port_args,
           *docker_args, *mounts, *env_args, args.image]
    res = sh(cmd)
    if res.returncode != 0:
        shutil.rmtree(rdir, ignore_errors=True)
        shutil.rmtree(cdir, ignore_errors=True)
        die(1, f"docker run failed: {res.stderr.strip()}")
    container = res.stdout.strip()
    meta = {"runId": run_id, "container": container, "port": port,
            "apiUrl": f"http://{args.api_bind}:{port}",
            "image": args.image, "startedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                                            time.gmtime())}
    if args.network:
        meta["network"] = args.network
    if ws is not None:
        # host path only -- never mounted into the container -- so `resume`
        # can remount the same workspace over a fresh container later.
        meta["workspace"] = str(ws)
    elif ws_named:
        meta["workspaces"] = {n: str(p) for n, p in ws_named.items()}
    (rdir / "host.json").write_text(json.dumps(meta, indent=2))
    out(args, {**meta, "authenticated": bool(token)},
        f"{run_id}\n  container: {container[:12]}\n  api: {meta['apiUrl']}")
    if not args.detach:
        # fatal=False: if the container/API dies right at job completion
        # (on_complete=exit tears the server down immediately after emitting
        # the final event), a request racing that shutdown can see the
        # stream cut off before the terminal event is fully delivered. Don't
        # let that crash or hang the CLI — fall through to the status.json
        # fallback below instead of a hard failure.
        _follow_events(args, run_id, fatal=False)
        # The final /status poll can hit that very same race (a fresh
        # connection right as the server is torn down): fall back to the
        # run dir's status.json, which the engine writes before emitting the
        # terminal event, rather than raising/crashing on a reset/refused
        # connection.
        try:
            status = api(run_id, "GET", "/status")
        except SystemExit:
            status = _read_json(rdir / "status.json", {})
        except (ConnectionError, OSError, TimeoutError):
            status = _read_json(rdir / "status.json", {})
        sys.exit(0 if status.get("verdict") == "verified" else 1)


# Recorded states that mean the job is over, so `stop`/`rm --force` may take
# its containers and directories away. Defined ONCE in engine/state.py beside
# `NONTERMINAL_STATES` (task 030) because the hub's delete endpoint gates on
# exactly the same tuple; re-exported here under its historical name.


def _has_later_state_event(run_id: str, ev_id) -> bool:
    """True when the run dir's `events.jsonl` holds a `state` event with a
    higher id than `ev_id` -- i.e. the log itself already proves the run
    moved on after that marker. False when the log cannot be read or the id
    is unusable, in which case the caller falls back to the liveness probe."""
    if not isinstance(ev_id, int):
        return False
    try:
        with open(run_root(run_id) / "events.jsonl") as f:
            for line in f:
                try:
                    ev = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if (ev.get("type") == "state" and isinstance(ev.get("id"), int)
                        and ev["id"] > ev_id):
                    return True
    except OSError:
        return False
    return False


def _engine_is_live(run_id: str) -> bool:
    """True only when the run's API answers *and* reports a non-terminal
    state, i.e. an engine is still working on this run right now.

    An unreachable API (container gone, or torn down moments after the
    terminal event) and an API that reports a terminal state both count as
    not live. Deliberately does not go through api(): a failed probe here is
    an expected outcome, not a fatal CLI error, and must not print to stderr
    in the middle of a --json event stream."""
    url = host_meta(run_id).get("apiUrl")
    if not url:
        return False
    req = urllib.request.Request(url + "/status")
    token_file = run_root(run_id) / ".api-token"
    if token_file.exists():
        req.add_header("Authorization", f"Bearer {token_file.read_text().strip()}")
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            status = json.loads(resp.read() or b"{}")
    except (urllib.error.URLError, TimeoutError, ConnectionError, OSError,
            json.JSONDecodeError):
        return False
    return isinstance(status, dict) and status.get("state") in NONTERMINAL_STATES


def _terminal_event_ends_stream(run_id: str, ev: dict) -> bool:
    """Task 031 (#13): decide whether a `state` event naming a terminal
    state is really the end of the stream.

    A run dir's `events.jsonl` is append-only *across resumes*, and the
    follower replays it from id 0 -- so the first terminal state event it
    sees may be a historical marker from an earlier episode that
    `ralphctl resume` has since continued past. Closing on it makes
    `ralphctl watch` (and the completion-wait idiom in docs/cli.md) report a
    resumed job as finished while it is still working.

    The stream therefore ends only when BOTH hold: nothing in the log
    supersedes the marker (no later `state` event -- the marker is the log's
    last word on the run's state), and the engine is not live (reconciled
    against the live /status, never against the replayed marker). An idling
    finished run -- API up, state terminal -- still ends the stream, so this
    never turns a completed job into a hang.

    Only later *state* events count as superseding: the engine emits
    `on_complete_cmd` log events strictly after the terminal state event
    (engine/main.py), and those must not hold the stream open past a real
    completion."""
    if _has_later_state_event(run_id, ev.get("id")):
        return False
    return not _engine_is_live(run_id)


def _follow_events(args, run_id: str, fatal: bool = True):
    meta = host_meta(run_id)
    url = meta["apiUrl"] + "/events?since=0"
    req = urllib.request.Request(url)
    token_file = run_root(run_id) / ".api-token"
    if token_file.exists():
        req.add_header("Authorization", f"Bearer {token_file.read_text().strip()}")
    connected = False
    post_connect_failures = 0
    # fatal=False callers (the --no-detach post-completion fall-through in
    # cmd_start/cmd_resume) always have a status.json fallback right after
    # this call, so a never-connected retry there only needs to cover a
    # normal container/API startup window, not the generous backoff a truly
    # fatal `ralphctl watch`/`logs -f` needs while waiting on an operator's
    # possibly-slow image pull. Without this bound, a job that finishes (and
    # tears its API down) before the CLI's very first connection attempt —
    # e.g. a near-instant stub job, observed for real against a docker
    # sibling — burns tens of seconds retrying a connection that will never
    # succeed, instead of falling straight through to the already-correct
    # status.json fallback.
    max_attempts = 30 if fatal else 12
    for attempt in range(max_attempts):
        try:
            with urllib.request.urlopen(req, timeout=3600) as resp:
                connected = True
                for line in resp:
                    line = line.decode().strip()
                    if not line.startswith("data: "):
                        continue
                    ev = json.loads(line[6:])
                    if args.json:
                        print(json.dumps(ev), flush=True)
                    else:
                        detail = {k: v for k, v in ev.items()
                                  if k not in ("id", "ts", "type")}
                        print(f"[{ev['ts']}] {ev['type']} "
                              f"{json.dumps(detail) if detail else ''}", flush=True)
                    if (ev["type"] == "state"
                            and ev.get("state") in TERMINAL_STATES
                            and _terminal_event_ends_stream(run_id, ev)):
                        return
            return
        except (urllib.error.URLError, TimeoutError, ConnectionError):
            if connected:
                # We were already receiving events — the API almost
                # certainly died right at job completion, most likely after
                # emitting (but not fully delivering) the terminal event.
                # A couple of quick retries is enough; no need for the full
                # container-startup backoff below.
                post_connect_failures += 1
                if post_connect_failures >= 3:
                    break
                time.sleep(0.2)
            elif fatal:
                time.sleep(1 + attempt * 0.5)
            else:
                time.sleep(0.3)
    if fatal:
        die(4, "could not connect to event stream")


def _read_job_yaml(path: Path) -> dict:
    """Parse the `key: json.dumps(value)`-per-line format `job.yaml` is
    written in (see cmd_start) -- not real YAML, just JSON-per-line, kept
    simple since the engine's own loader treats it the same way."""
    job: dict = {}
    for line in path.read_text().splitlines():
        if not line.strip():
            continue
        key, _, val = line.partition(": ")
        job[key] = json.loads(val)
    return job


def _write_job_yaml(path: Path, job: dict) -> None:
    path.write_text(
        "".join(f"{k}: {json.dumps(v)}\n" for k, v in job.items() if v is not None))


def _apply_iterations_topup(job: dict, spec: str) -> None:
    """`--iterations +N` bumps the existing budget by N; a bare integer sets
    it absolutely (documented as `+N` but an absolute value is harmless and
    occasionally convenient)."""
    spec = spec.strip()
    try:
        if spec.startswith("+"):
            job["iterations"] = int(job.get("iterations", 25)) + int(spec[1:])
        else:
            job["iterations"] = int(spec)
    except ValueError:
        die(2, f"--iterations: invalid value {spec!r} (expected e.g. +10 or 30)")


def _container_running(name: str) -> bool | None:
    """True/False if the container exists (running or not); None if no
    container by that name exists at all."""
    res = sh([DOCKER, "inspect", "--format", "{{.State.Running}}", name])
    if res.returncode != 0:
        return None
    return res.stdout.strip() == "true"


def cmd_resume(args):
    """PRD req 16: start a fresh container over an *existing* run dir. The
    engine side (task 028, src/ralphd/engine/loop.py:_resume_point) detects
    pre-existing `tasks.json`/iterations and continues instead of
    re-planning; this only has to reproduce cmd_start's docker-run wiring
    for the same mounts (run dir, config dir, workspace-if-any) plus an
    optional `--iterations +N` budget top-up written into the existing
    job.yaml before the container starts."""
    run_id = args.run_id
    _require_run(run_id)
    rdir = run_root(run_id)
    cdir = config_root(run_id)
    name = job_container_name(run_id)

    running = _container_running(name)
    if running:
        die(5, f"container {name} is still running -- a live engine already "
               f"holds this run dir's lock; `abort` or `stop` it first")
    if running is False:
        # exited/stopped container occupying the name -- `docker run --name`
        # refuses to reuse a name already in use by any container.
        sh([DOCKER, "rm", "-f", name])

    job_path = cdir / "job.yaml"
    job = _read_job_yaml(job_path)
    if args.iterations:
        _apply_iterations_topup(job, args.iterations)
        _write_job_yaml(job_path, job)

    prev_meta = host_meta(run_id)
    ws = prev_meta.get("workspace")
    ws_named: dict[str, str] = prev_meta.get("workspaces") or {}
    port = args.port or free_port()
    mounts = ["-v", f"{rdir}:/run/ralphd", "-v", f"{cdir}:/config:ro"]
    if ws:
        mounts += ["-v", f"{ws}:/workspace"]
    for name, path in ws_named.items():
        mounts += ["-v", f"{path}:/workspace/{name}"]

    env_args: list[str] = []
    token_file = rdir / ".api-token"
    if token_file.exists():
        env_args += ["-e", f"RALPHD_API_TOKEN={token_file.read_text().strip()}"]

    # Reproduce the exact `--llm` wiring (env vars + extra mounts) resolved
    # at `start` time (task 058, operator steering 018) -- never re-derived
    # from the resuming shell's own (possibly absent/different) environment.
    wiring = _read_llm_wiring(cdir)
    for k, v in wiring["env"].items():
        env_args += ["-e", f"{k}={v}"]
    for m in wiring["mounts"]:
        mounts += ["-v", m]

    # Reproduce the resolved --forward-env/--llm-env/--env pairs from
    # `start` time (task 001) in the same order/precedence, never re-read
    # from this resuming shell's own (possibly absent/different) env.
    for entry in _read_extra_env_wiring(cdir):
        env_args += ["-e", entry]

    docker_args = ["--label", f"ralphd.run={run_id}",
                   "--label", "ralphd.role=job"]
    env_args += ["-e", f"RALPHD_SELF_CONTAINER_ID={name}"]
    # --network on resume overrides; otherwise reuse what start recorded so a
    # resumed job keeps the same connectivity its PRD was written against.
    network = args.network or prev_meta.get("network")
    port_args = _network_args(network, args.api_bind, port,
                              docker_args, env_args)
    if args.allow_docker:
        sock = docker_sock()
        try:
            st = os.stat(sock)
        except OSError:
            die(2, f"--allow-docker: docker socket {sock} not found")
        if not stat_mod.S_ISSOCK(st.st_mode):
            die(2, f"--allow-docker: {sock} is not a socket")
        docker_args += ["-v", f"{sock}:/var/run/docker.sock",
                        "--group-add", str(st.st_gid)]
        if ws:
            env_args += ["-e", f"RALPHD_HOST_WORKSPACE={ws}"]
        elif ws_named:
            env_args += ["-e", f"RALPHD_HOST_WORKSPACES={json.dumps(ws_named)}"]
        env_args += ["-e", f"RALPHD_HOST_RUN_DIR={rdir}",
                     "-e", f"RALPHD_RUN_ID={run_id}"]
        print("ralphctl: WARNING: --allow-docker mounts the host docker socket "
              "into the job container. The docker socket is ROOT-EQUIVALENT "
              "access to this host. Only use with PRDs you trust.",
              file=sys.stderr)
    if ws_named:
        env_args += ["-e", f"RALPHD_WORKSPACES={','.join(ws_named)}"]

    cmd = [DOCKER, "run", "-d", "--name", name, "--init", *port_args,
           *docker_args, *mounts, *env_args, args.image]
    res = sh(cmd)
    if res.returncode != 0:
        die(1, f"docker run failed: {res.stderr.strip()}")
    container = res.stdout.strip()
    meta = {"runId": run_id, "container": container, "port": port,
            "apiUrl": f"http://{args.api_bind}:{port}",
            "image": args.image,
            "startedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    if network:
        meta["network"] = network
    if ws:
        meta["workspace"] = ws
    elif ws_named:
        meta["workspaces"] = ws_named
    (rdir / "host.json").write_text(json.dumps(meta, indent=2))
    out(args, meta,
        f"{run_id} resumed\n  container: {container[:12]}\n  api: {meta['apiUrl']}")
    if not args.detach:
        _follow_events(args, run_id, fatal=False)
        try:
            status = api(run_id, "GET", "/status")
        except SystemExit:
            status = _read_json(rdir / "status.json", {})
        except (ConnectionError, OSError, TimeoutError):
            status = _read_json(rdir / "status.json", {})
        sys.exit(0 if status.get("verdict") == "verified" else 1)


# ---------------------------------------------------------------- observation
# Task 055 (#9): `ralphctl runs` sorting, kept deliberately in lockstep with
# the hub run list's `RUN_COLUMNS` / `sortRuns` (cli/web/app.js, task 054) --
# same key names, same lifecycle orders, same missing-value and tie-break
# rules -- so an operator who sorts the table in the browser and then types
# `ralphctl runs --sort <key>` gets the same order, not a second dialect.
RUN_STATE_ORDER = ["starting", "running", "succeeded", "failed", "aborted"]
RUN_VERDICT_ORDER = ["", "unverified", "verified"]
# Keys whose *natural* first direction is descending (biggest/newest first),
# mirroring app.js `toggleRunSort`'s first-click rule. `--reverse` flips
# whichever direction the key starts with.
RUN_SORT_DESC_KEYS = ("startedAt", "iterationsUsed", "approach")


def _lifecycle_rank(order: list[str], value) -> int:
    """Position of a state/verdict in lifecycle order; an unrecognised value
    sorts after every known one instead of scrambling the order (mirrors
    app.js `lifecycleRank`)."""
    v = "" if value is None else str(value).lower()
    return order.index(v) if v in order else len(order)


def _num_or_none(v):
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _epoch_or_none(ts):
    if not ts:
        return None
    try:
        return parse_utc(str(ts))
    except (ValueError, TypeError):
        return None


def _task_ratio(row: dict):
    """Task 014 (#21): a run row's task progress as a fraction of one
    (`tasksCompleted / tasksTotal`), or None when there is no plan to have
    progress through -- mirrors app.js `taskRatio`.

    Sorting on the ratio (not on the rendered `5/7`, not on the bare
    numerator) is what makes `5/7` outrank `100/250`; `None` means "no plan",
    which `_cmp_run_values` puts after every run that has one instead of
    treating it as 0% done.
    """
    total = _num_or_none(row.get("tasksTotal"))
    completed = _num_or_none(row.get("tasksCompleted"))
    if total is None or total <= 0:
        return None
    return (0.0 if completed is None else completed) / total


# key -> sort value extracted from the RAW row values, never the rendered
# cell text (`iterationsUsed`, not the "17/250" string; the epoch instant,
# not the ISO characters).
RUN_SORT_KEYS: dict = {
    "runId": lambda r: str(r.get("runId") or ""),
    "state": lambda r: _lifecycle_rank(RUN_STATE_ORDER, r.get("state")),
    "verdict": lambda r: _lifecycle_rank(RUN_VERDICT_ORDER, r.get("verdict")),
    "phase": lambda r: str(r.get("phase") or ""),
    "approach": lambda r: _num_or_none(r.get("approach")),
    # Task 014 (#21): the hub's TASKS column sorts on the completion RATIO
    # (`5/7` outranks `100/250`; a plan-less run has no ratio at all, so it
    # sorts last ascending) -- and the sort dialect is shared, so the key
    # exists here too, computed the same way. `cmd_runs` starts carrying the
    # raw counts in task 015; until then every row's value is None, which the
    # missing-value rule below already handles.
    "tasks": lambda r: _task_ratio(r),
    "iterationsUsed": lambda r: _num_or_none(r.get("iterationsUsed")),
    "startedAt": lambda r: _epoch_or_none(r.get("startedAt")),
}
RUN_SORT_DEFAULT = "startedAt"


def _cmp_run_values(a, b) -> int:
    """app.js `cmpValues`: missing values (no startedAt, no approach yet)
    compare *after* present ones rather than pretending to be 0/""."""
    a_missing = a is None or a == ""
    b_missing = b is None or b == ""
    if a_missing or b_missing:
        return 0 if a_missing and b_missing else (1 if a_missing else -1)
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return -1 if a < b else (1 if a > b else 0)
    a, b = str(a), str(b)
    return -1 if a < b else (1 if a > b else 0)


def sort_run_rows(rows: list[dict], key: str | None = None,
                  reverse: bool = False) -> list[dict]:
    """Order run rows exactly the way the hub table does for the same key.

    Default `startedAt` descending -- newest first, which is what an operator
    coming back to a machine wants, not run-id alphabetical.
    """
    key = key or RUN_SORT_DEFAULT
    value = RUN_SORT_KEYS.get(key, RUN_SORT_KEYS[RUN_SORT_DEFAULT])
    direction = -1 if key in RUN_SORT_DESC_KEYS else 1
    if reverse:
        direction = -direction

    def compare(x, y):
        r = _cmp_run_values(value(x), value(y))
        if r:
            return r * direction
        # Deterministic tie-break (app.js does the same) so repeated calls
        # never reshuffle equal rows.
        return _cmp_run_values(str(x.get("runId") or ""), str(y.get("runId") or ""))

    return sorted(rows, key=functools.cmp_to_key(compare))


def cmd_runs(args):
    rows = []
    runs_dir = registry() / "runs"
    if runs_dir.is_dir():
        for d in sorted(runs_dir.iterdir()):
            status = _read_json(d / "status.json", {})
            if args.state and status.get("state") != args.state:
                continue
            used = status.get("iterationsUsed", 0)
            rows.append({"runId": d.name, "state": status.get("state"),
                         "verdict": status.get("verdict"),
                         "phase": status.get("phase"),
                         "approach": status.get("approach"),
                         # Task 007 (#16): BOTH numbers in --json (raw, for
                         # sorting and machine consumers) next to the human
                         # `n/m` string; an absent field is an explicit null
                         # ("limit unknown"), never the live config guessed in.
                         "maxApproaches": status.get("maxApproaches"),
                         "approachDisplay": format_approach(
                             status.get("approach"),
                             status.get("maxApproaches")),
                         "iterationsUsed": used,
                         "iterationsBudget": status.get("iterationsBudget"),
                         "iterations": f"{used}"
                                       f"/{status.get('iterationsBudget', '?')}",
                         "startedAt": status.get("startedAt"),
                         # Task 015 (#21): the same TASKS fields the hub's run
                         # list carries, built by the same
                         # `TasksRead.row_fields` -- raw counts (what `--sort
                         # tasks` compares and what a machine consumer wants),
                         # `tasksDisplay`/`tasksColumn` rendered by the
                         # engine's formatters, `tasksTrouble` in `ralphctl
                         # status`' exact wording, plus task 002's
                         # `tasksStale`/`tasksSource`.
                         #
                         # ONE local hardened read per listed row -- after the
                         # `--state` filter, so a filtered-out run costs
                         # nothing -- with `persist=False`, because the CLI is
                         # a viewer and must not leave a last-good cache in
                         # somebody else's run dir (`ui_server._row_tasks`
                         # reads exactly the same way).
                         **read_tasks_doc(d, persist=False).row_fields})
    # One ordering for both surfaces: --json is the human table's rows in the
    # same sequence, so a script and a reader never disagree about "first".
    rows = sort_run_rows(rows, getattr(args, "sort", None),
                         bool(getattr(args, "reverse", False)))
    if args.json:
        print(json.dumps(rows, indent=2))
    else:
        fmt = ("{runId:<24} {state:<10} {verdict:<10} {phase:<9} {approach:<8} "
               "{tasks:<12} {iterations:<7} {startedAt}")
        print(fmt.format(runId="RUN", state="STATE", verdict="VERDICT",
                         phase="PHASE", approach="APPROACH", tasks="TASKS",
                         iterations="ITER", startedAt="STARTED"))
        for r in rows:
            print(fmt.format(
                runId=str(r["runId"]), state=str(r["state"]),
                verdict=str(r["verdict"]), phase=str(r["phase"]),
                # Task 007 (#16): the rendered `n/m` (blank for a run with no
                # approach yet) -- the raw number stays in --json and is what
                # `--sort approach` compares, exactly like `iterationsUsed`
                # vs the "7/250" string.
                approach=str(r["approachDisplay"]),
                # Task 015 (#21): `5/7`, blank for a run with no plan (never
                # `0/0`), plus the trouble marker when a task is
                # validation-failed/in-progress and `stale` when the fraction
                # came from the last-good payload -- the hub cell's text,
                # flattened by `format_task_column`. The flag SENTENCES do not
                # fit a column: they are in `--json`'s `tasksTrouble` and in
                # `ralphctl status <run>`'s summary, verbatim.
                tasks=str(r["tasksColumn"]),
                iterations=str(r["iterations"]),
                # Task 048 (#4)'s shared absolute formatter for the human
                # column; --json keeps the raw ISO value for sorting/consumers.
                startedAt=format_local_time(r["startedAt"])))


def _format_reason_lines(reason) -> list[str]:
    """Task 003: human `reason:` line(s) for status.json's `reason` field --
    wrapped readably across a few lines rather than one giant line, and
    simply omitted (empty list) when there is no reason to show (still
    running, or a terminal state that never set one)."""
    if not reason:
        return []
    text = str(reason)
    wrapped = textwrap.wrap(text, width=76) or [text]
    lines = [f"reason:    {wrapped[0]}"]
    lines.extend(f"           {extra}" for extra in wrapped[1:])
    return lines


def _format_reflect_lines(reflect) -> list[str]:
    """Task 020 (#5): the `reflection:` line(s) for a run whose post-terminal
    `reflect` iteration failed -- e.g.
    `reflection: failed (Connection error.)`.

    Returns an empty list unless status.json's `reflect` verdict (task 019,
    see docs/api.md) actually records a failure: a successful reflection is
    already visible as `artifacts/reflection/report.md`, and `reflect: null`
    (reflect disabled, or the phase has not ended yet) has nothing to say --
    so every run that is not a *failed* reflection keeps the exact output it
    had before this line existed.

    A failed reflection never changes the run's state/verdict/reason (the job
    is over by the time reflect runs), which is precisely why it needs its own
    line: otherwise the only trace is artifacts/reflection/FAILED.md, and from
    the outside the run dir looks like reflect had never been enabled.
    """
    if not isinstance(reflect, dict) or reflect.get("ok") is not False:
        return []
    error = str(reflect.get("error") or "").strip() or "reason not recorded"
    wrapped = textwrap.wrap(f"failed ({error})", width=76) or [error]
    lines = [f"reflection: {wrapped[0]}"]
    lines.extend(f"            {extra}" for extra in wrapped[1:])
    return lines


def _format_auto_resume_lines(auto_resume) -> list[str]:
    """Task 028 (#8): the `auto-resume:` line(s) for a run whose automatic
    recovery gave up -- a crash loop that `doctor --fix` has stopped trying
    to fix is otherwise completely invisible (the run just sits there
    recorded `running` with no container and nothing ever happens again).

    Nothing at all unless the guard actually gave up: a run with a couple of
    recorded attempts is still being recovered, and a run that never needed
    recovery has no record, so every other run's human output is unchanged.
    """
    if not isinstance(auto_resume, dict) or not auto_resume.get("gaveUp"):
        return []
    reason = str(auto_resume.get("reason") or "").strip() or (
        f"gave up after {auto_resume.get('attempts')} attempts")
    wrapped = textwrap.wrap(reason, width=64) or [reason]
    lines = [f"auto-resume: {wrapped[0]}"]
    lines.extend(f"             {extra}" for extra in wrapped[1:])
    return lines


def _countdown_to(ts) -> str:
    """Human countdown to a published wall-clock timestamp (task 013, #5):
    'in 58s (2026-08-18T09:15:02Z)'. Degrades gracefully -- 'due now' once
    the moment has passed (or when it is missing), and a bare 'at <ts>' for a
    timestamp that is not in the engine's utcnow() format -- a status line
    must never be the thing that crashes `ralphctl status`."""
    if not ts:
        return "due now"
    try:
        remaining = parse_utc(str(ts)) - time.time()
    except (ValueError, TypeError):
        return f"at {ts}"
    if remaining <= 0:
        return "due now"
    return f"in {format_duration(remaining)} (at {ts})"


def _format_degraded_lines(status: dict) -> list[str]:
    """Task 013 (#5): the `degraded:` line(s) for a run sitting out an infra
    outage, so `ralphctl status` says *why nothing is happening* instead of
    showing a run that merely looks stuck at `state: running`.

    Returns an empty list for a healthy run (`health` absent or "ok"), which
    keeps the human output of every non-degraded run byte-identical to what
    it was before this line existed.

    Two degraded shapes exist (see docs/api.md's `health`/`infraWait`):
    `infraWait` is populated only while a backoff wait is actually pending;
    between two waits the retry attempt itself is running, `infraWait` is
    back to `null` and `health` is still "degraded" (the outage episode is
    not over until an iteration reaches the model again). Both are reported.
    """
    if (status.get("health") or "ok") != "degraded":
        return []
    wait = status.get("infraWait")
    if not isinstance(wait, dict):
        return [("degraded:  infra outage episode in progress "
                 "(a retry attempt is running, no backoff wait pending)")]
    lines = [(f"degraded:  infra outage: attempt {wait.get('attempt')} "
              f"(phase {wait.get('phase') or '?'}), "
              f"next attempt {_countdown_to(wait.get('nextAttemptAt'))}, "
              f"waited {format_duration(wait.get('waitedS'))} of "
              f"{format_duration(wait.get('budgetS'))} outage budget")]
    error = str(wait.get("error") or "").strip()
    if error:
        wrapped = textwrap.wrap(error, width=69) or [error]
        lines.append(f"           error: {wrapped[0]}")
        lines.extend(f"           {extra}" for extra in wrapped[1:])
    return lines


def _format_container_gone_lines(run_id: str, status: dict, entry: dict,
                                 tty: bool) -> list[str]:
    """Task 022 (#8): the dedicated warning for a run whose recorded state is
    non-terminal but whose container is gone -- the zombie condition
    `_dangling_run_entry` owns (the same one doctor and repair report).

    Before this line existed the operator had to join two facts printed lines
    apart -- `state: running` and `(live api: False)` -- and know that the
    combination means "this run died without ever recording a terminal
    state", which reads exactly like a briefly-unreachable healthy run. Said
    once, explicitly, naming the container and the command that diagnoses it
    (`repair` names both the `--set-state` and the `resume` remedy, so status
    does not fork that story).
    """
    state = status.get("state")
    return [_ansi(tty, "1;31",
                  f"container: {entry['container']} appears gone (no such "
                  f"container) -- status.json still"),
            _ansi(tty, "1;31",
                  f"           records state {state!r}, so this run stopped "
                  f"without recording a terminal"),
            _ansi(tty, "1;31",
                  f"           state; diagnose with `ralphctl repair "
                  f"{run_id}`")]


_TASK_STATUS_LABELS = TASK_STATUS_LABELS  # task 013 (#21): the labels moved to engine/state.py


def _summarize_tasks(tasks: dict) -> str:
    """Task 003: render the /status `tasks` counts dict (e.g.
    {"total": 7, "completed": 7, "pending": 0, ...}, see api.py's
    GET /status) as a short human summary like '7/7 completed' or, when
    not everything is done, '5/7 completed (1 in-progress, 1 pending)' --
    instead of dumping the raw counts dict as JSON.

    Task 013 (#21) moved the renderer itself into `engine/state.py`
    (`format_task_counts`) so the hub's TASKS column and `ralphctl runs` word
    the same counts identically; this stays as the name `cmd_status` and its
    tests already use."""
    return format_task_counts(tasks)


def _format_token_count(n) -> str:
    n = n or 0
    if n >= 10_000:
        return f"{n / 1000:.0f}k tokens"
    if n >= 1_000:
        return f"{n / 1000:.1f}k tokens"
    return f"{n} tokens"


def _summarize_usage(usage: dict) -> str:
    """Task 003: render the /status `usage` dict (costUSD/totalTokens plus
    a byPhase breakdown, see loop.py's `_accumulate_usage`) as a short
    human summary like '$0.56, 625k tokens (planning $0.10 / worker $0.40
    / review $0.06)' -- instead of dumping the raw usage dict as JSON."""
    if not usage:
        return "(none)"
    # Task 051 (#10): rendered through the one shared `format_cost`, so an
    # unpriced/mixed bucket says `unavailable` instead of claiming `$0.00`.
    # `or "$0.00"` preserves the pre-0.5 rendering for a usage dict that
    # simply has no `costUSD` key (old run dirs) -- unknown-vs-free is a
    # distinction only the markers can make, and they win above.
    summary = (f"{format_cost(usage) or '$0.00'}, "
               f"{_format_token_count(usage.get('totalTokens'))}")
    by_phase = usage.get("byPhase") or {}
    phase_bits = [f"{phase} {format_cost(by_phase[phase]) or '$0.00'}"
                 for phase in ("planning", "worker", "review") if phase in by_phase]
    if phase_bits:
        summary += " (" + " / ".join(phase_bits) + ")"
    return summary


def cmd_status(args):
    live = True
    try:
        status = api(args.run_id, "GET", "/status")
    except SystemExit:
        live = False
        status = _read_json(run_root(args.run_id) / "status.json")
        if status is None:
            die(3, f"run {args.run_id} not found")
        # Task 013 (#5): the on-disk fallback publishes the same
        # health/infraWait contract GET /status guarantees (api.py setdefaults
        # them too), so `--json` passes both through even for a pre-0.5 run
        # dir whose status.json predates the fields.
        status.setdefault("health", "ok")
        status.setdefault("infraWait", None)
        # Task 020 (#5): same for the reflect verdict -- null means "no
        # reflect iteration has finished" (reflect off, or not there yet).
        status.setdefault("reflect", None)
        # Task 007 (#16): same for the approach denominator -- a pre-v0.6 run
        # dir has no `maxApproaches`, and `GET /status` publishes an explicit
        # null for it, so the on-disk fallback's `--json` says the same thing
        # (limit unknown) rather than omitting the key.
        status.setdefault("maxApproaches", None)
        # Task 012 (#14): same for the resolved model id and the raw gateway id
        # -- `GET /status` publishes explicit nulls for a run dir that never
        # observed one, so the on-disk fallback's `--json` says the same thing.
        status.setdefault("model", None)
        status.setdefault("modelRaw", None)
        # Task 023 (#8): status.json itself carries no task counts -- the
        # engine synthesises them in GET /status from tasks.json, so the
        # on-disk fallback used to print `tasks: (none)` for a run dir with
        # a perfectly readable plan (exactly the case an operator hits when
        # the container is gone and they want to know how far it got).
        # Computed here from the same tasks.json with the engine's own
        # `task_counts()`, so both surfaces agree key-for-key. Kept behind
        # `not status.get("tasks")` so a future engine that persists real
        # counts into status.json wins over this reconstruction.
        #
        # Task 004 (#15): read through the hardened reader, so a plan caught
        # mid-rewrite reconstructs from the last-good payload (and says so via
        # the same `tasksStale`/`tasksSource` fields a live GET /status
        # carries) instead of collapsing to `tasks: (none)`. `persist=False`:
        # the CLI is a viewer, it does not write caches into a run dir.
        plan = read_tasks_doc(run_root(args.run_id), persist=False)
        status.update(plan.contract)
        if not status.get("tasks") and plan.tasks:
            status["tasks"] = plan.counts
    status["live"] = live

    # Task 028 (#8): the auto-resume crash-loop guard's record lives on the
    # host (the engine inside the container knows nothing about it), so it is
    # merged in here for both the live and the on-disk path -- `null` for a
    # run that never needed recovery.
    status["autoResume"] = (
        _read_auto_resume_state(args.run_id)
        if _auto_resume_state_path(args.run_id).is_file() else None)

    # Task 022 (#8): an unreachable run whose recorded state is non-terminal
    # is almost certainly a zombie -- container died/removed outside
    # ralphctl. Only asked once the API is already known to be unreachable,
    # so a live run's output (and its `docker inspect` cost) is unchanged.
    container_gone = None if live else _dangling_run_entry(args.run_id)
    status["containerGone"] = container_gone is not None

    # Duration fields (PRD steering 051): a single `durationSeconds` covers
    # both "elapsed so far" (state still running, no endedAt yet) and "total
    # run time" (terminal state, endedAt set) -- same measurement, the only
    # thing that changes is whether the end bound is `endedAt` or now.
    # Additive: existing timestamp fields (startedAt/endedAt/...) are left
    # untouched, this only adds new numeric-seconds fields alongside them.
    #
    # Task 022 (#8): for a zombie run neither bound is meaningful -- there is
    # no `endedAt` (the engine never got to write one) and nothing is
    # elapsing, so measuring to *now* prints an ever-growing number that
    # reads like a live run making progress. The end bound becomes the last
    # status.json write (`updatedAt`), and what is displayed is the staleness
    # -- time since that write -- under a label saying exactly that.
    stale_since = None
    if container_gone is not None:
        stale_since = status.get("updatedAt") or status.get("startedAt")
    duration_s = elapsed_seconds(status.get("startedAt"),
                                 status.get("endedAt") or stale_since)
    status["durationSeconds"] = duration_s
    since_update_s = elapsed_seconds(stale_since) if stale_since else None
    if stale_since:
        status["sinceLastUpdateSeconds"] = since_update_s
    cur_it = status.get("currentIteration")
    it_duration_s = None
    if isinstance(cur_it, dict):
        it_duration_s = elapsed_seconds(cur_it.get("startedAt"), stale_since)
        cur_it["elapsedSeconds"] = it_duration_s

    if stale_since:
        shown_s, duration_label = since_update_s, "since last update"
    else:
        shown_s = duration_s
        duration_label = "total" if status.get("endedAt") else "elapsed"
    tty = sys.stdout.isatty()
    lines = [
        f"run:       {status.get('runId')}",
        f"state:     {status.get('state')}  (live api: {live})",
    ]
    if container_gone is not None:
        lines.extend(_format_container_gone_lines(
            args.run_id, status, container_gone, tty))
    lines += [
        f"verdict:   {status.get('verdict')}",
        f"duration:  {format_duration(shown_s)}  ({duration_label})",
        # Task 048 (#4): a relative duration alone cannot be correlated with
        # anything outside the run (an upstream outage window, another run's
        # log, a host reboot). The absolute local-time instants go alongside
        # -- not instead of -- the durations, through the one shared
        # `format_local_time` formatter; `--json` keeps the raw ISO
        # `startedAt`/`endedAt` fields untouched for machine consumers.
        f"started:   {format_local_time(status.get('startedAt'))}",
    ]
    if status.get("endedAt"):
        lines.append(f"ended:     {format_local_time(status.get('endedAt'))}")
    elif stale_since:
        lines.append(f"last update: {format_local_time(stale_since)}")
    approach_text = format_approach(status.get("approach"),
                                    status.get("maxApproaches"))
    phase_line = f"phase:     {status.get('phase')}"
    if approach_text:
        # Task 007 (#16): `approach 2/3` when the limit is known, `approach 2`
        # when it is not, and no approach segment at all for a run that has
        # not entered the ladder (it used to read `approach None`).
        phase_line += f"  approach {approach_text}"
    lines += [
        phase_line,
        f"iteration: {status.get('iterationsUsed')}/{status.get('iterationsBudget')}",
    ]
    # Task 012 (#14): name the model the run is actually talking to, as pi
    # resolved it -- omitted entirely (never `model: None`) when no iteration
    # has observed one yet, the same discipline as the approach segment above.
    # The raw gateway id is only shown when it differs from the resolved ref.
    if status.get("model"):
        model_line = f"model:     {status.get('model')}"
        if status.get("modelRaw"):
            model_line += f"  (gateway id: {status.get('modelRaw')})"
        lines.append(model_line)
    if isinstance(cur_it, dict):
        at_update = ", at last update" if stale_since else ""
        lines.append(f"iteration elapsed: {format_duration(it_duration_s)} "
                     f"(iteration {cur_it.get('number')}, "
                     f"phase={cur_it.get('phase')}{at_update})")
        # Task 001a criterion 4: while an infra-fault retry is backing off,
        # currentIteration.note carries a human-readable
        # "retrying after infra fault (attempt N/max, next in Xs): <error>"
        # message -- surface it here so plain `ralphctl status` (not just
        # --json) shows it, matching docs/architecture.md's claim.
        note = cur_it.get("note")
        if note:
            lines.append(f"           note: {note}")
    # Task 013 (#5): a degraded run (sitting out an infra outage) says so
    # here, with the countdown to the next attempt; nothing at all for a
    # healthy run, whose output stays byte-identical.
    lines.extend(_format_degraded_lines(status))
    # Task 003: a `reason:` line whenever status.json carries a non-empty
    # `reason` (set by the engine on terminal failed/aborted states, or the
    # engine-error path) -- previously only visible via --json.
    lines.extend(_format_reason_lines(status.get("reason")))
    lines.append(f"tasks:     {_summarize_tasks(status.get('tasks') or {})}")
    lines.append(f"usage:     {_summarize_usage(status.get('usage') or {})}")
    # Task 020 (#5): a failed post-terminal reflection is otherwise invisible
    # here -- it deliberately leaves state/verdict/reason untouched, so
    # without this line the operator cannot tell "reflect ran and died" from
    # "reflect was never enabled".
    lines.extend(_format_reflect_lines(status.get("reflect")))
    # Task 028 (#8): ... and a run automatic recovery has given up on says so,
    # naming the crash loop, instead of looking merely forgotten.
    lines.extend(_format_auto_resume_lines(status.get("autoResume")))
    # Task 006: a terminal run (failed/aborted/succeeded) that still has
    # unconsumed steering files is a silent-drop hazard -- a terminal run
    # never reads pending steering again, so this is the operator's only
    # remaining chance to notice and act (e.g. re-steer a resumed run).
    # Surfaced loudly (not buried in --json) rather than left to be spotted
    # by combing steering/.consumed.json by hand.
    unconsumed = status.get("unconsumedSteering") or []
    if unconsumed:
        names = ", ".join(unconsumed)
        lines.append(_ansi(tty, "1;31",
                            f"!! UNCONSUMED STEERING: {names} "
                            "(run ended without acting on this steering)"))
    out(args, status, "\n".join(lines))


# Task 004 (#15): what `ralphctl tasks` says on stderr when it read the plan
# from the run dir instead of the run's API -- the `tasks` twin of
# `_LOGS_SNAPSHOT_NOTICE`, worded the same way so an operator learns one
# phrase. On stderr, never stdout, so `--json` stays a clean document.
_TASKS_SNAPSHOT_NOTICE = (
    "on-disk snapshot: the run's API is not reachable, showing the plan "
    "recorded in the run dir")


def _tasks_doc(run_id: str) -> tuple[bool, dict]:
    """Fetch a run's task document live, falling back to the run dir on disk
    (task 004, #15).

    Two independent failure modes used to print NOTHING here:
      * the container is gone -- `api()` exited 4 on connection-refused, even
        though the plan is sitting in the run dir (the case an operator most
        needs it: how far did the run get before it died?);
      * `tasks.json` was caught mid-rewrite -- the reader defaulted to an
        empty plan, so the command printed zero task lines and looked like a
        run with no plan at all.

    Both are fixed by the same two moves: fall back like `cmd_logs` does, and
    read through the engine's hardened reader, which serves the last plan
    that parsed and labels it (`tasksStale`/`tasksSource`, the exact fields a
    live `GET /tasks` carries -- so `--json` has one shape either way).

    Returns `(live, doc)`. Exit 3 ("run not found") is still an error: no run
    dir means there is nothing to fall back to.
    """
    _require_run(run_id)
    # `api()` reports unreachability by printing to stderr and exiting 4;
    # here that is a fallback, not an error, so its message is buffered and
    # only replayed for failures we still propagate (e.g. a 404).
    err = io.StringIO()
    try:
        with contextlib.redirect_stderr(err):
            doc = api(run_id, "GET", "/tasks")
        if isinstance(doc, dict):
            return True, doc
    except SystemExit as e:
        if e.code != 4:
            sys.stderr.write(err.getvalue())
            raise
    res = read_tasks_doc(run_root(run_id), persist=False)
    return False, {**res.doc, **res.contract}


def cmd_tasks(args):
    live, tasks = _tasks_doc(args.run_id)
    if not live:
        print(_TASKS_SNAPSHOT_NOTICE, file=sys.stderr)
    # The stale/unreadable wording lives in `engine/state.py` next to the
    # reader (like `log_merge.NO_TRANSCRIPT`), so the live path -- where the
    # engine flagged the read -- and the on-disk path say the same sentence.
    notice = tasks_read_notice(tasks.get("tasksSource"), bool(tasks.get("tasksStale")))
    if notice:
        print(f"!! {notice}", file=sys.stderr)
    if args.json:
        print(json.dumps({**tasks, "live": live}, indent=2))
        return
    for t in tasks.get("tasks", []):
        print(f"[{t.get('status'):<17}] {t.get('id')} {t.get('title')}")


# Task 040 (#6): what `ralphctl logs` says on stderr when it served the
# transcript from the run dir instead of the run's API. On stderr, never
# stdout, so `--raw` keeps its 1:1 wire contract and a pipe into jq/tee is
# unaffected; the wording matches the hub's own snapshot label (app.js
# `.lg-snapshot`) so both surfaces call the same thing by the same name.
_LOGS_SNAPSHOT_NOTICE = (
    "on-disk snapshot: the run's API is not reachable, showing the "
    "transcript recorded in the run dir")
_LOGS_SNAPSHOT_FOLLOW_NOTICE = _LOGS_SNAPSHOT_NOTICE + " (nothing to follow)"


def _snapshot_raw_text(run_id: str, iteration: int | None, tail: int) -> str:
    """The raw NDJSON `GET /logs` (or `GET /iterations/{n}/output`) would
    have served for this run dir, read straight off disk via the shared
    merge module -- i.e. byte-identical to what the engine serves from the
    inside (task 038)."""
    if iteration is not None:
        return "".join(_iteration_lines(run_root(run_id), iteration, tail=tail))
    return "".join(_merged_lines(run_root(run_id), tail=tail))


def _pretty_log_lines(text: str, tty: bool, tail: int) -> list[str]:
    """Render a raw NDJSON transcript to the lines pretty-mode `ralphctl
    logs` prints, trimmed to the last `tail` RENDERED lines (task 057).

    Task 041 (#6): when the render is empty the operator gets the explicit
    `log_merge.NO_TRANSCRIPT` marker instead of zero bytes of output, which
    is indistinguishable from a broken command. That happens for a run
    whose `iterations/` dir is empty (just started, or died before its
    first iteration was recorded) -- the single most likely moment for
    someone to run `logs` on it. The wording lives in `log_merge` so this
    surface and the hub's log tail (`ui_server.rendered_log_lines`) say the
    exact same thing. `--raw` deliberately does NOT get this line: it is a
    1:1 wire-format contract for machines, and an empty transcript is
    honestly zero events.
    """
    lines = _render_to_lines(text, tty, _new_render_state())
    if tail:
        lines = lines[-tail:]
    return lines or [NO_TRANSCRIPT]


def _logs_text(args, path: str, tail: int) -> tuple[bool, str]:
    """Fetch a log snapshot for `cmd_logs`, falling back to the on-disk
    merge when the run's API is unreachable (task 040, #6).

    A dead run is the case an operator most needs the transcript for (the
    container crashed -- what did the agent do last?), and the bytes are
    all right there in the run dir, so `logs` no longer dies with exit 4 on
    connection-refused. Returns `(live, raw_text)`; `live=False` callers
    must print `_LOGS_SNAPSHOT_NOTICE` on stderr. Exit 3 ("run not found")
    is still an error: no run dir means nothing to fall back to.
    """
    _require_run(args.run_id)
    # `api()` reports unreachability by printing to stderr and exiting 4;
    # here that is not an error but a fallback, so its message is buffered
    # and only replayed for the failures we still propagate (e.g. a 404
    # from `--iteration` on a live run, which exits 1).
    err = io.StringIO()
    try:
        with contextlib.redirect_stderr(err):
            return True, api(args.run_id, "GET", path, raw=True, timeout=30)
    except SystemExit as e:
        if e.code != 4:
            sys.stderr.write(err.getvalue())
            raise
        return False, _snapshot_raw_text(args.run_id, args.iteration, tail)


def _api_reachable(run_id: str, timeout: int = 5) -> bool:
    """Cheap liveness probe for a run's container API (task 040, #6).

    Needed by the *follow* paths only: a follow cannot discover
    unreachability the way a snapshot fetch does (`_logs_text`), because
    `_stream_logs` would have to fail mid-stream. `GET /status` is the
    smallest reply the engine serves, so a live run pays one tiny extra
    request and keeps its behaviour byte-identical. An HTTP *error* still
    counts as reachable -- something answered, so the container is alive and
    the real follow should run and report that error itself.
    """
    meta = host_meta(run_id)
    if not meta.get("apiUrl"):
        return False
    req = urllib.request.Request(meta["apiUrl"] + "/status")
    token_file = run_root(run_id) / ".api-token"
    if token_file.exists():
        req.add_header("Authorization", f"Bearer {token_file.read_text().strip()}")
    try:
        with urllib.request.urlopen(req, timeout=timeout):
            return True
    except urllib.error.HTTPError:
        return True
    except (urllib.error.URLError, TimeoutError, OSError):
        return False


def _print_log_snapshot(args, tail: int, tty: bool, following: bool) -> None:
    """Print the on-disk transcript for an unreachable run and say so on
    stderr (task 040, #6). `--raw` gets the merge verbatim (1:1 with the
    wire format `GET /logs` would have served); pretty mode renders it and
    trims to `tail` RENDERED lines exactly like the live path does."""
    if args.raw:
        text = _snapshot_raw_text(args.run_id, args.iteration, tail or 0)
        sys.stdout.write(text)
        if text and not text.endswith("\n"):
            print()
    else:
        text = _snapshot_raw_text(args.run_id, args.iteration, 0)
        for line in _pretty_log_lines(text, tty, tail or 0):
            print(line)
    print(f"ralphctl: {_LOGS_SNAPSHOT_FOLLOW_NOTICE if following else _LOGS_SNAPSHOT_NOTICE}",
          file=sys.stderr)


def cmd_logs(args):
    tty = sys.stdout.isatty()
    _require_run(args.run_id)
    # `--tail` has no default: bare `logs <id>` (no --follow) falls back to
    # tail 50; bare `-f`/`--follow` with no explicit count follows the
    # unbounded log from now on (no fixed snapshot size).
    tail = args.tail
    if tail is None and not args.follow:
        tail = 50
    base_path = (f"/iterations/{args.iteration}/output" if args.iteration is not None
                 else "/logs")

    if args.raw:
        # --raw semantics are unchanged from before task 057: 1 raw NDJSON
        # event == 1 line, and `tail`/`follow` are applied engine-side to
        # raw events exactly as `GET /logs?tail=N` already does -- this is
        # the machine-facing contract and must stay 1:1 with the wire
        # format, so there is nothing to trim CLI-side here.
        qs = []
        if tail:
            qs.append(f"tail={tail}")
        if args.follow:
            qs.append("follow=true")
        query = ("?" + "&".join(qs)) if qs else ""
        path = base_path + query
        if args.follow:
            # Task 040 (#6): nothing to follow on a dead run -- print the
            # on-disk snapshot and exit 0 with a notice, rather than dying
            # on connection-refused (exit 4) or, worse, hanging.
            if not _api_reachable(args.run_id):
                _print_log_snapshot(args, tail or 0, tty, following=True)
                return
            # Task 002: Ctrl+C during a follow is a normal user-requested
            # stop, not a crash -- caught here (rather than left to
            # Python's default handler) so it exits with no traceback at
            # the single documented code (docs/cli.md), instead of a
            # traceback + implicit exit code 1.
            #
            # Task 016: `_TerminalModeGuard` owns termios save/restore for
            # the whole follow, in this (main) thread, so restoration
            # happens on every exit path -- including this very
            # `KeyboardInterrupt` -- not just the ones a background
            # thread's `finally` might have gotten to run before the
            # process died.
            try:
                with _TerminalModeGuard():
                    _stream_logs(args, path, tty)
            except KeyboardInterrupt:
                sys.exit(_SIGINT_EXIT_CODE)
            return
        live, text = _logs_text(args, path, tail or 0)
        sys.stdout.write(text)
        if text and not text.endswith("\n"):
            print()
        if not live:
            print(f"ralphctl: {_LOGS_SNAPSHOT_NOTICE}", file=sys.stderr)
        return

    # Pretty mode: `-N`/`--tail N` means N RENDERED lines -- what the
    # operator actually sees -- not N raw NDJSON events (task 057). The
    # renderer collapses/skips many raw event types (e.g. a burst of
    # `toolcall_delta` events becomes one one-liner), so trimming raw
    # events BEFORE rendering (as the engine's own `tail=` query param
    # does) yields a wildly variable, much-smaller-than-N visible line
    # count. Fix: always fetch the FULL raw transcript from the engine
    # (no tail param), render every line, THEN trim to exactly N rendered
    # lines. Iteration/phase boundary headers count toward N like any
    # other rendered line (see docs/cli.md).
    if not args.follow:
        # tail=0 for the on-disk fallback too: in pretty mode the trim is
        # applied to RENDERED lines below, never to raw events.
        live, full_text = _logs_text(args, base_path, 0)
        for line in _pretty_log_lines(full_text, tty, tail or 0):
            print(line)
        if not live:
            print(f"ralphctl: {_LOGS_SNAPSHOT_NOTICE}", file=sys.stderr)
        return

    if not _api_reachable(args.run_id):  # task 040 (#6), see the --raw branch
        _print_log_snapshot(args, tail or 0, tty, following=True)
        return

    try:
        with _TerminalModeGuard():
            _stream_logs_pretty_tailed(args, base_path, tty, tail)
    except KeyboardInterrupt:
        sys.exit(_SIGINT_EXIT_CODE)


# Task 019 (#18.1): what `ralphctl iteration` says when the asked-for
# iteration is not in the run dir. Exit 1 (generic error), the same code the
# live `logs --iteration <n>` path already exits with when the engine answers
# 404 for it -- "run not found" (3) is a different fact and stays reserved for
# a run dir that does not exist at all.
def _iteration_span(numbers: list[int]) -> str:
    if not numbers:
        return "none recorded yet"
    if len(numbers) == 1:
        return str(numbers[0])
    return f"1..{numbers[-1]}" if numbers == list(range(1, numbers[-1] + 1)) \
        else ", ".join(str(n) for n in numbers)


def cmd_iteration(args):
    """One iteration's own story: phase, model, timestamps, duration, exit
    reason, that iteration's tokens and cost, and its full transcript
    (task 019, #18.1 -- the hub's iteration dialog, task 020, renders the same
    shaped payload).

    Purely on-disk: `iterations/NNNN/meta.json` and that iteration's
    transcript are written
    by the engine into the run dir, so this works identically for a running job
    and for one whose container is long gone -- there is nothing to fall back
    from, hence no `--live`-style notice (see `state.iteration_detail`).
    """
    _require_run(args.run_id)
    root = run_root(args.run_id)
    detail = iteration_detail(root, args.number)
    if detail is None:
        die(1, f"run {args.run_id} has no iteration {args.number} "
               f"(iterations on disk: {_iteration_span(iteration_numbers(root))})")
    tty = sys.stdout.isatty()
    log_lines = None
    if not args.no_log:
        # The transcript goes through the same merge + renderer `ralphctl logs
        # --iteration N` uses, so the two commands cannot render the same
        # events differently; `--json` never gets ANSI (tty=False).
        raw = _snapshot_raw_text(args.run_id, args.number, 0)
        log_lines = _pretty_log_lines(raw, tty and not args.json, 0)

    if args.json:
        doc = {"runId": args.run_id, **detail}
        # `log` is absent (not empty) with --no-log: an empty list would claim
        # the iteration produced no transcript.
        if log_lines is not None:
            doc["log"] = log_lines
        print(json.dumps(doc, indent=2))
        return

    it_line = f"run:       {args.run_id}"
    # Task 020 (#18.1): the header block is worded ONCE, in
    # `state.iteration_summary_lines`, and shown verbatim by the hub's
    # iteration dialog too -- only this `run:` line (the id the operator
    # typed) belongs to the CLI.
    lines = [it_line] + iteration_summary_lines(detail)
    print("\n".join(lines))
    if log_lines is not None:
        print(format_iteration_log_header(len(log_lines)))
        for line in log_lines:
            print(line)


def cmd_fault(args):
    """Why this run is (or last was) in trouble (task 025, #18.4).

    `faultClass: "infra"` on its own is a verdict without an argument: it says
    the engine blamed the endpoint, not WHICH signature from
    `engine/faults.py`' table fired, how far up the retry ladder the run has
    climbed, or how much of the outage budget is already spent. Reading those
    three facts used to mean knowing the signature table by heart, grepping
    `events.jsonl` for `infra_retry` and doing the budget arithmetic by hand.

    The join and its wording live in `state.fault_explanation` /
    `state.fault_summary_lines`, so the hub's fault dialog (task 026) explains
    the same fault in the same words.

    Purely on-disk, like `ralphctl iteration`/`docs`/`artifacts`: status.json,
    events.jsonl and the iteration metas are the engine's own writes, so a live
    run and one whose container is long gone read identically -- there is
    nothing to fall back from, hence no snapshot notice.
    """
    _require_run(args.run_id)
    exp = fault_explanation(run_root(args.run_id))
    if args.json:
        # `text` is the same complete rendering the human output prints (and
        # the hub dialog shows), `summaryLines` its lines -- the `docs`/
        # `artifacts` shape.
        print(json.dumps({"runId": args.run_id, **exp,
                          "text": fault_text(exp)}, indent=2))
        return
    print("\n".join([f"run:       {args.run_id}", *exp["summaryLines"]]))


def cmd_cost(args):
    """What this run spent, per phase and per approach (task 027, #18.5).

    `ralphctl status` prints one headline number and (historically) a
    three-phase parenthetical; status.json's `usage` has carried `byPhase` and
    `byApproach` buckets all along, so "which phase burned the tokens" and
    "how much of this figure is actually known" only ever needed joining up and
    wording. Both live in `state.cost_breakdown` / `cost_breakdown_lines`, so
    task 028's hub dialog shows the same block in the same words, and the
    headline stays the very `costDisplay` string `status` and the hub's usage
    card print -- a breakdown can never disagree with the number beside it.

    Purely on-disk, like `ralphctl iteration`/`docs`/`artifacts`/`fault`:
    status.json is the engine's own atomic write, so a live run and one whose
    container is long gone read identically -- nothing to fall back from, hence
    no snapshot notice.
    """
    _require_run(args.run_id)
    bd = cost_breakdown(run_root(args.run_id))
    if args.json:
        # `text` is the same complete rendering the human output prints (and
        # the hub dialog shows), `summaryLines` its lines -- the `fault`/
        # `docs`/`artifacts` shape.
        print(json.dumps({"runId": args.run_id, **bd,
                          "text": cost_breakdown_text(bd)}, indent=2))
        return
    print("\n".join([f"run:       {args.run_id}", *bd["summaryLines"]]))

class _TerminalModeGuard:
    """Task 016: owns termios save/restore for `ralphctl logs -f` on a
    TTY, in the MAIN thread, for the entire duration of the follow loop.

    Before this existed, save/restore lived inside `_QuitWatcher`, a
    background daemon thread -- but a main-thread `KeyboardInterrupt` (or
    `SystemExit`) can unwind and terminate the process before that
    thread's own `finally` ever gets scheduled to run, stranding the
    terminal in cbreak/no-echo mode after Ctrl+C. Termios state is a
    property of the terminal, not of any one thread, so ownership belongs
    to whichever code path is guaranteed to run to completion around the
    whole follow -- that is the main thread's `with` block here, entered
    once before `_QuitWatcher` (or anything else) touches stdin and
    exited via `finally` semantics on ANY exit path: normal return,
    `KeyboardInterrupt`, `SystemExit`, or an arbitrary exception.

    SIGTERM (e.g. a plain `kill <pid>`) is also handled: a handler is
    installed for the guard's lifetime that raises `SystemExit` so the
    signal turns into an ordinary Python exception unwinding through this
    `with` block's `__exit__`, instead of the default SIGTERM action
    (immediate process termination with zero Python-level cleanup, which
    would strand the terminal exactly like the old thread-owned design).

    `restore()` is idempotent -- safe to call more than once (guarded by
    `_active`) -- because an `atexit` hook is ALSO registered as a
    belt-and-braces last resort in case some future exit path manages to
    skip `__exit__` entirely (e.g. `os._exit`); calling both `__exit__`
    and the atexit hook in the ordinary case must not double-apply or
    error.

    Never activates on a non-TTY stdin: `sys.stdin.isatty()` is checked
    once up front, matching `_QuitWatcher`'s own longstanding rule that a
    piped/redirected follow never touches stdin at all."""

    def __init__(self) -> None:
        self._fd: int | None = None
        self._old_attrs = None
        self._active = False
        self._prev_sigterm = None

    @property
    def active(self) -> bool:
        return self._active

    def __enter__(self) -> Self:
        if not sys.stdin.isatty():
            return self
        fd = sys.stdin.fileno()
        try:
            old_attrs = termios.tcgetattr(fd)
        except (termios.error, OSError):
            return self
        self._fd = fd
        self._old_attrs = old_attrs
        self._active = True
        atexit.register(self.restore)
        with contextlib.suppress(termios.error, OSError):
            tty_module.setcbreak(fd)
        with contextlib.suppress(ValueError):
            # ValueError: signal only works in the main thread -- if this
            # is ever entered off-thread, skip SIGTERM handling rather
            # than crash; termios restore below still applies.
            self._prev_sigterm = signal.signal(signal.SIGTERM, self._on_sigterm)
        return self

    @staticmethod
    def _on_sigterm(signum, frame) -> None:
        raise SystemExit(128 + signal.SIGTERM)

    def restore(self) -> None:
        """Idempotent: a second call (e.g. from the belt-and-braces
        `atexit` hook after `__exit__` already ran) is a safe no-op."""
        if not self._active:
            return
        self._active = False
        with contextlib.suppress(termios.error, OSError):
            termios.tcsetattr(self._fd, termios.TCSADRAIN, self._old_attrs)

    def __exit__(self, exc_type, exc, tb) -> bool:
        self.restore()
        if self._prev_sigterm is not None:
            with contextlib.suppress(ValueError):
                signal.signal(signal.SIGTERM, self._prev_sigterm)
            self._prev_sigterm = None
        return False


class _QuitWatcher:
    """Task 002: while `ralphctl logs -f` follows on a TTY, watch stdin in
    a background thread for a single 'q' keypress and, on seeing one,
    close the open HTTP response to unblock the main thread's blocking
    line-iteration loop -- a clean, user-requested stop, not an error.

    Deliberately never started on a non-TTY stdin (piped/redirected): the
    caller only constructs/starts this when `sys.stdin.isatty()`, so a
    piped `logs -f` never touches stdin and can never block waiting for a
    key that will never come.

    Task 016: this thread no longer owns or restores termios state at
    all -- cbreak mode is entered once, up front, by `_TerminalModeGuard`
    in the main thread (which also owns restoring it on every exit path),
    so this watcher's only job is reading keys. Leaving signal generation
    (Ctrl+C -> SIGINT) to the terminal driver exactly as normal, this
    watcher does not need to handle Ctrl+C itself -- the ordinary
    KeyboardInterrupt path (see `cmd_logs`) does."""

    def __init__(self, resp):
        self.resp = resp
        self.quit = False
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        try:
            while not self._stop.is_set():
                ch = sys.stdin.read(1)
                if not ch:
                    return
                if ch in ("q", "Q"):
                    self.quit = True
                    with contextlib.suppress(Exception):
                        self.resp.close()
                    return
        except Exception:
            return


def _stream_logs(args, path: str, tty: bool, state: dict | None = None,
                  skip_lines: int = 0) -> None:
    """Open `path` (a `/logs...follow=true...` or
    `/iterations/N/output...follow=true...` URL) and render/print each
    NDJSON line as it arrives off the open connection, instead of waiting
    for the response body to finish (which -- for follow=true -- only
    happens once the job itself terminates). Iterating the urllib response
    object line-by-line (rather than calling `.read()`) is what makes this
    genuinely live: each `yield` on the engine's StreamingResponse side is
    delivered as its own HTTP chunk, and `http.client`'s line iteration
    returns a line as soon as it has one, not only at EOF.

    `skip_lines`, if given, discards that many raw lines off the front of
    the stream before rendering/printing anything -- used by
    `_stream_logs_pretty_tailed` (task 057) to resume a live follow right
    after a backlog that was already fetched/rendered/printed separately,
    without re-showing (or losing) a single line: the transcript only
    ever grows, so a fresh snapshot's first `skip_lines` raw lines are
    always identical to the ones already consumed.

    Task 002: on a TTY, a background `_QuitWatcher` lets 'q' end the
    follow cleanly (return, no error) by closing `resp`; on a non-TTY
    stdin no watcher is ever started, so piped/redirected follows never
    touch stdin. Ctrl+C (KeyboardInterrupt) is deliberately NOT caught
    here -- it propagates to `cmd_logs`, which turns it into a clean,
    traceback-free exit at the documented `_SIGINT_EXIT_CODE`."""
    meta = host_meta(args.run_id)
    if not meta.get("apiUrl"):
        die(4, f"no API endpoint recorded for run {args.run_id}")
    req = urllib.request.Request(meta["apiUrl"] + path)
    token_file = run_root(args.run_id) / ".api-token"
    if token_file.exists():
        req.add_header("Authorization", f"Bearer {token_file.read_text().strip()}")
    if state is None:
        state = _new_render_state()
    skipped = 0
    watcher: _QuitWatcher | None = None
    try:
        with urllib.request.urlopen(req, timeout=3600) as resp:
            if sys.stdin.isatty():
                watcher = _QuitWatcher(resp)
                watcher.start()
            try:
                for raw_bytes in resp:
                    if skipped < skip_lines:
                        skipped += 1
                        continue
                    line = raw_bytes.decode().rstrip("\n")
                    if not line:
                        continue
                    if args.raw:
                        print(line, flush=True)
                    else:
                        _render_log_line(line, tty, state, live=True)
                        sys.stdout.flush()
            finally:
                if watcher is not None:
                    watcher.stop()
    except (urllib.error.URLError, TimeoutError, ConnectionError,
            OSError, ValueError, AttributeError) as e:
        if watcher is not None and watcher.quit:
            return  # 'q' pressed -- closing resp to unblock the loop
                    # above is expected to surface as exactly this kind
                    # of "connection/file closed" exception -- including,
                    # per Python's chunked-transfer decoder, a bare
                    # AttributeError (`self.fp` -- the underlying socket
                    # file object -- can already be None by the time the
                    # main thread's blocking `for raw_bytes in resp:`
                    # loop notices the close, if the watcher thread's
                    # `resp.close()` lands mid-chunk-read) -- not an
                    # error. AttributeError is only ever swallowed here
                    # when `watcher.quit` is set, i.e. genuinely caused by
                    # OUR OWN 'q'-triggered close; any other AttributeError
                    # still surfaces below via `die`.
        die(4, f"API unreachable: {e}")


def _stream_logs_pretty_tailed(args, base_path: str, tty: bool, tail: int | None) -> None:
    """Follow+pretty (task 057): show exactly `tail` RENDERED backlog lines
    -- computed by fetching the FULL raw transcript, rendering it, and
    trimming AFTER rendering, same as the bounded path -- then keep the
    SAME render state and continue live from a fresh follow=true
    connection, skipping the raw lines already consumed by the backlog
    fetch so nothing is double-rendered or dropped (see `_stream_logs`).

    The follow connection is opened with an explicit huge `tail=` value
    (`_FULL_BACKLOG_TAIL`) rather than no `tail` param at all: `GET /logs`
    replays its full untailed snapshot either way, but `GET
    /iterations/{n}/output` has the OPPOSITE default -- no `tail` param
    there means "seek straight to EOF, replay nothing" -- so a bare
    `follow=true` would silently skip straight to only-new-lines and the
    `skip_lines` accounting above would eat genuinely-new content. A tail
    value far larger than any real transcript forces both endpoints to
    replay their full current backlog uniformly before continuing live."""
    full_text = api(args.run_id, "GET", base_path, raw=True, timeout=30)
    already_raw_lines = len(full_text.splitlines()) if full_text else 0
    state = _new_render_state()
    backlog = _render_to_lines(full_text, tty, state)
    if tail:
        backlog = backlog[-tail:]
    for line in backlog:
        print(line)
    sys.stdout.flush()
    _stream_logs(args, f"{base_path}?follow=true&tail={_FULL_BACKLOG_TAIL}", tty,
                 state=state, skip_lines=already_raw_lines)


def cmd_watch(args):
    _follow_events(args, args.run_id)


# ---------------------------------------------------------------- skills
def cmd_skills(args):
    _require_run(args.run_id)
    if args.action == "ls":
        skills = api(args.run_id, "GET", "/config/skills")
        if args.json:
            print(json.dumps(skills, indent=2))
        else:
            for s in skills:
                print(f"{s.get('name'):<24} {s.get('origin'):<8} "
                     f"{s.get('fileCount', '?')} file(s)")
            if not skills:
                print("(no skills)")
    elif args.action == "get":
        body = api(args.run_id, "GET", f"/config/skills/{args.name}", binary=True)
        dest = Path(args.dest)
        dest.mkdir(parents=True, exist_ok=True)
        with tarfile.open(fileobj=io.BytesIO(body), mode="r:*") as tf:
            tf.extractall(dest)
        out(args, {"name": args.name, "dest": str(dest)},
            f"skill '{args.name}' written to {dest}")
    elif args.action == "add":
        src = Path(args.dir).expanduser().resolve()
        if not (src / "SKILL.md").is_file():
            die(2, f"skills add: {src} has no SKILL.md")
        body = _tar_skill_dir(src)
        api(args.run_id, "PUT", f"/config/skills/{src.name}", data=body,
           content_type="application/x-tar")
        out(args, {"added": src.name}, f"skill '{src.name}' uploaded")
    elif args.action == "rm":
        api(args.run_id, "DELETE", f"/config/skills/{args.name}")
        out(args, {"removed": args.name}, f"skill '{args.name}' removed")


# ---------------------------------------------------------------- creds
def cmd_creds(args):
    _require_run(args.run_id)
    if args.action == "ls":
        creds = api(args.run_id, "GET", "/config/creds")
        if args.json:
            print(json.dumps(creds, indent=2))
        else:
            for c in creds:
                print(f"{c.get('name'):<24} {c.get('size', '?')} byte(s)")
            if not creds:
                print("(no creds)")
    elif args.action == "get":
        body = api(args.run_id, "GET", f"/config/creds/{args.name}", raw=True)
        print(body, end="" if body.endswith("\n") else "\n")
    elif args.action == "add":
        src = Path(args.file).expanduser().resolve()
        if src.suffix != ".env":
            die(2, f"creds add: {src} is not a *.env file")
        if not src.is_file():
            die(2, f"creds add: {src} does not exist")
        name = src.stem
        api(args.run_id, "PUT", f"/config/creds/{name}", data=src.read_bytes(),
           content_type="text/plain")
        out(args, {"added": name}, f"credential '{name}' uploaded")
    elif args.action == "rm":
        api(args.run_id, "DELETE", f"/config/creds/{args.name}")
        out(args, {"removed": args.name}, f"credential '{args.name}' removed")


# ---------------------------------------------------------------- prompts
# Kept in sync with `PROMPT_NAMES` in `src/ralphd/engine/config.py` -- the
# CLI validates client-side *before* any HTTP call (exit 2, mirrors the
# skills/creds local-validation style), while the engine also enforces the
# same set server-side (422). Deliberately not importing the engine package
# here: ralphctl runs on the host, the engine runs inside the job container.
PROMPT_NAMES = ("planning", "worker", "review", "task-verify")


def cmd_prompts(args):
    _require_run(args.run_id)
    if args.action == "ls":
        prompts = api(args.run_id, "GET", "/config/prompts")
        if args.json:
            print(json.dumps(prompts, indent=2))
        else:
            for p in prompts:
                print(f"{p.get('name'):<12} {p.get('source')}")
    elif args.action == "set":
        if args.phase not in PROMPT_NAMES:
            die(2, f"prompts set: invalid phase '{args.phase}' -- must be one "
                   f"of: {', '.join(PROMPT_NAMES)}")
        src = Path(args.file).expanduser().resolve()
        if not src.is_file():
            die(2, f"prompts set: {src} does not exist")
        text = src.read_bytes()
        if not text.strip():
            die(2, f"prompts set: {src} is empty")
        api(args.run_id, "PUT", f"/config/prompts/{args.phase}", data=text,
           content_type="text/plain")
        out(args, {"phase": args.phase}, f"prompt '{args.phase}' overridden")


# ---------------------------------------------------------------- llm
def cmd_llm(args):
    if args.llm_action == "profiles":
        names = ["host", "none", *llm_profiles.list_profile_names(registry())]
        if args.json:
            print(json.dumps(
                [{"name": n, "builtin": n in ("host", "none")} for n in names],
                indent=2))
        else:
            for n in names:
                print(f"{n}{' (builtin)' if n in ('host', 'none') else ''}")
    elif args.llm_action == "show":
        if args.name in ("host", "none"):
            doc = {"name": args.name, "builtin": True}
            out(args, doc,
                f"'{args.name}' is a built-in profile (see docs/llm-profiles.md) "
                f"-- nothing to resolve.")
            return
        path = llm_profiles.profile_path(registry(), args.name)
        if not path.is_file():
            die(3, f"llm profile '{args.name}' not found (looked for {path})")
        try:
            resolved = llm_profiles.resolve_profile(args.name, registry(), redact=True)
        except llm_profiles.ProfileError as e:
            die(1, str(e))
        doc = {"name": args.name, "builtin": False, **resolved}
        if args.json:
            print(json.dumps(doc, indent=2))
        else:
            print(f"profile: {args.name}")
            print(f"description: {doc.get('description') or '(none)'}")
            print(f"model: {doc.get('model') or '(unset)'}")
            print(f"fast_model: {doc.get('fast_model') or '(unset)'}")
            print(f"price_strategy: {doc.get('price_strategy') or '(unset)'}")
            print("env:")
            for k, v in doc["env"].items():
                print(f"  {k} = {v}")
            if not doc["env"]:
                print("  (none)")
            print("mounts:")
            for m in doc["mounts"]:
                print(f"  {m}")
            if not doc["mounts"]:
                print("  (none)")
            print("pi:")
            print(json.dumps(doc["pi"], indent=2) if doc["pi"] else "  (none)")
    elif args.llm_action == "test":
        _cmd_llm_test(args)


def _llm_test_resolve(name: str):
    """Resolve profile `name` for `ralphctl llm test`, unredacted (this runs
    entirely on the host, same trust level as `start`). Returns
    ``(model, env, mounts, pi_fragment)``. Built-ins never fail to resolve
    (nothing to read); a named profile dies exactly like `start`/`show`
    (exit 3 unknown name, exit 1 unresolvable reference) with the same
    diagnostic."""
    if name == "none":
        return None, {}, [], None
    if name == "host":
        env = {var: os.environ[var] for var in HOST_LLM_ENV if os.environ.get(var)}
        mounts = []
        aws = Path.home() / ".aws"
        if aws.is_dir():
            mounts.append(f"{aws}:/home/agent/.aws:ro")
        pi_fragment = None
        models_json = Path.home() / ".pi" / "agent" / "models.json"
        if models_json.is_file():
            try:
                pi_fragment = json.loads(models_json.read_text())
            except (OSError, json.JSONDecodeError):
                pi_fragment = None
        return None, env, mounts, pi_fragment
    path = llm_profiles.profile_path(registry(), name)
    if not path.is_file():
        die(3, f"llm profile '{name}' not found (looked for {path})")
    try:
        resolved = llm_profiles.resolve_profile(name, registry())
    except llm_profiles.ProfileError as e:
        die(1, str(e))
    return resolved["model"], resolved["env"], resolved["mounts"], resolved["pi"]


def _cmd_llm_test(args):
    """`ralphctl llm test <profile>`: resolve on the host (dies with the
    exact diagnostic `start`/`show` would on a missing/unresolvable
    profile -- no docker needed for this part). If resolution succeeds and
    docker is reachable (and `--no-ping` wasn't given), follow up with a
    real one-token completion in a throwaway `--rm`, labeled container
    (entrypoint overridden straight to `pi`, bypassing ralphd-engine)."""
    model, env, mounts, pi_fragment = _llm_test_resolve(args.name)
    try:
        docker_ok = sh([DOCKER, "version", "--format", "{{.Server.Version}}"]) \
            .returncode == 0
    except OSError:
        docker_ok = False
    if args.no_ping or not docker_ok:
        reason = " (--no-ping)" if args.no_ping else \
            ("" if docker_ok else " (docker unavailable -- skipped ping)")
        out(args, {"profile": args.name, "resolved": True, "pinged": False},
            f"llm profile '{args.name}' resolves OK{reason}")
        return
    env_args: list[str] = []
    for k, v in env.items():
        env_args += ["-e", f"{k}={v}"]
    mount_args: list[str] = []
    for m in mounts:
        mount_args += ["-v", m]
    tmp_pi: Path | None = None
    if pi_fragment:
        tmp_pi = Path(tempfile.mkdtemp(prefix="ralphd-llm-test-"))
        pi_file = tmp_pi / "models.json"
        pi_file.write_text(json.dumps(pi_fragment))
        os.chmod(pi_file, 0o600)
        mount_args += ["-v", f"{pi_file}:/home/agent/.pi/agent/models.json:ro"]
    ping_model = args.model or model
    cmd = [DOCKER, "run", "--rm", "-i", "--entrypoint", "pi",
           "--label", f"ralphd.llm-test={args.name}",
           *env_args, *mount_args, args.image,
           "-p", "--mode", "json", "--no-session"]
    if ping_model:
        cmd += ["--model", ping_model]
    try:
        res = sh(cmd, input="Reply with exactly the single word: ok\n")
    finally:
        if tmp_pi is not None:
            shutil.rmtree(tmp_pi, ignore_errors=True)
    if res.returncode != 0:
        die(1, f"llm profile '{args.name}': ping container failed (exit "
               f"{res.returncode}): {(res.stderr or res.stdout).strip()[:400]}")
    out(args, {"profile": args.name, "resolved": True, "pinged": True},
        f"llm profile '{args.name}' resolves OK and the container ping succeeded")


# ---------------------------------------------------------------- control
# Task 018 (#17): what `ralphctl steer --list` says on stderr when it read the
# history from the run dir instead of the run's API -- the steering twin of
# `_LOGS_SNAPSHOT_NOTICE`/`_TASKS_SNAPSHOT_NOTICE`, worded the same way so an
# operator learns one phrase. On stderr, never stdout, so `--json` stays a
# clean document.
_STEER_SNAPSHOT_NOTICE = (
    "on-disk snapshot: the run's API is not reachable, showing the steering "
    "messages recorded in the run dir")

# Width of the MESSAGE preview column. A steering message is multi-line prose;
# the table is an index (which messages exist, which the loop already applied),
# so it shows the first words and `--json` carries every body in full -- the
# terminal equivalent of the hub's click-to-open dialog (task 017).
_STEER_PREVIEW_WIDTH = 48


def _steer_preview(body: object, width: int = _STEER_PREVIEW_WIDTH) -> str:
    """One line of a steering body for the table's MESSAGE column.

    Whitespace (including the newlines of a multi-paragraph message) is
    collapsed so an entry can never occupy more than its row, and an
    over-long message is truncated with an ellipsis rather than wrapped --
    the full text is one `--json` away.
    """
    text = " ".join(str(body or "").split())
    if len(text) <= width:
        return text
    return text[:width - 1] + "\u2026"


def cmd_steer_list(args) -> None:
    """`ralphctl steer <run> --list` (task 018, #17): what has been steered.

    Issue #17's complaint is that steering was write-only: an operator could
    post a message and then had no way to see what was queued, what the loop
    already applied, or what the text said. The hub grew that view in tasks
    016/017; this is the same view in a terminal, and deliberately the same
    CODE -- `ui_server.steering_list` -- rather than a second implementation,
    so "the CLI and the hub show the same entries for the same run" is true by
    construction instead of by test discipline alone. That helper is
    live-first (the running engine decides when an entry becomes *applied*)
    with an on-disk fallback through the ONE shared reader
    (`engine.state.steering_entries`), so a finished or killed run's history
    stays readable -- the `logs`/`tasks` pattern.

    Exit 3 ("run not found") is still an error: no run dir means there is
    nothing to fall back to. An unreachable API is not -- it is the snapshot
    path, flagged on stderr.
    """
    _require_run(args.run_id)
    live, entries = ui_server.steering_list(registry(), args.run_id)
    if not live:
        print(_STEER_SNAPSHOT_NOTICE, file=sys.stderr)
    if args.json:
        print(json.dumps({"live": live, "entries": entries}, indent=2))
        return
    if not entries:
        # The wording lives in `ui_server` next to the hub's panel, so both
        # surfaces say the same sentence (like `log_merge.NO_TRANSCRIPT`).
        print(ui_server.NO_STEERING)
        return
    print(f"{'SEQ':>3}  {'STATE':<7}  {'ARRIVED':<25}  {'NAME':<18}  MESSAGE")
    for e in entries:
        seq = e.get("seq")
        # `tsLocal` is absent for an entry the hub/CLI cannot see on disk
        # (a live answer naming a file from another host): print `-`, never a
        # made-up arrival time.
        cells = (str(seq) if isinstance(seq, int) else "-",
                 str(e.get("state") or "-"),
                 str(e.get("tsLocal") or "-"),
                 str(e.get("name") or e.get("file") or "-"),
                 _steer_preview(e.get("body")))
        print(f"{cells[0]:>3}  {cells[1]:<7}  {cells[2]:<25}  "
              f"{cells[3]:<18}  {cells[4]}".rstrip())


def cmd_steer(args):
    if args.list:
        # `--list` is a read: it must not consume stdin, and pairing it with
        # anything that would SEND a message is a mistake worth naming rather
        # than silently doing one of the two.
        if args.message or args.file or args.now or args.name:
            die(2, "--list only shows this run's steering messages; drop the "
                   "message/--file/--name/--now")
        cmd_steer_list(args)
        return
    message = args.message or (Path(args.file).read_text() if args.file
                               else sys.stdin.read())
    if not message.strip():
        die(2, "empty steering message")
    if args.now:
        res = api(args.run_id, "POST", "/interrupt",
                  {"message": message, "name": args.name})
    else:
        res = api(args.run_id, "POST", "/steering",
                  {"message": message, "name": args.name})
    out(args, res, f"steering accepted: {res}")


def cmd_interrupt(args):
    out(args, api(args.run_id, "POST", "/interrupt"), "interrupted")


def cmd_pause(args):
    out(args, api(args.run_id, "POST", "/pause"), "pausing at iteration boundary")


def cmd_unpause(args):
    out(args, api(args.run_id, "POST", "/resume"), "unpaused")


def cmd_retry(args):
    """Task 016 (#5): wake a degraded run's infra backoff wait *now*.

    Deliberately NOT `unpause`: a degraded run is not paused, it is sitting
    out an LLM-endpoint outage (`/status` health `degraded` + a populated
    `infraWait`). A manual retry also resets the outage-budget episode clock,
    so the accumulated wait no longer counts against
    `infra_outage_budget_s`. Exit codes follow pause/unpause/abort: the
    shared api() helper maps the engine's 409 ("not waiting on an infra
    fault" / "job finished") onto exit 5, and an unreachable container onto
    4; an unknown run id is rejected before any HTTP with the documented 3.
    """
    _require_run(args.run_id)
    out(args, api(args.run_id, "POST", "/retry"),
        "retrying now (infra backoff wait woken; outage budget clock reset)")


def cmd_budget(args):
    """Task 046 (#3): change a *running* job's iteration budget in flight.

    Thin operator front-end for `PATCH /config/budget` (task 045), taking the
    same spec syntax as `resume --iterations`: `+N` tops up by N, a bare `N`
    sets the budget absolutely (so lowering is explicit -- a bare `-5` is an
    absolute negative budget and is rejected, never read as a decrement).

    Only the shape is checked here; every semantic decision (below
    `iterationsUsed`, job already finished) belongs to the engine, which owns
    the live counters. Exit codes come from the shared api() helper, so they
    match pause/unpause/retry/abort: 0 applied, 5 on the engine's 409
    refusals, 1 on its 422 (invalid value), 3 unknown run, 4 API unreachable,
    2 for a locally malformed spec (usage error, same as
    `resume --iterations`).

    The change is live-engine only: `/config/job.yaml` is a read-only mount,
    so a *fresh container* needs `resume --iterations +N` instead.
    """
    _require_run(args.run_id)
    spec = args.iterations.strip()
    if not re.fullmatch(r"\+?-?\d+", spec):
        die(2, f"budget: invalid value {spec!r} (expected e.g. +10 or 30)")
    res = api(args.run_id, "PATCH", "/config/budget", {"iterations": spec})
    out(args, res,
        f"iteration budget: {res.get('previous')} -> {res.get('iterations')} "
        f"({res.get('iterationsUsed')} used)")


def cmd_abort(args):
    result = api(args.run_id, "POST", "/abort", {"reason": args.reason or ""})
    # Task 029 (#8): the engine records the same marker itself (loop.abort);
    # doing it host-side too covers the case where the container dies before
    # it gets that far. Either way `doctor --fix` must never auto-resume a
    # run the operator deliberately terminated.
    _record_operator_termination(args.run_id, "abort", args.reason or "")
    out(args, result, "aborting")


def _record_operator_termination(run_id: str, action: str, reason: str) -> None:
    """Host-side writer for the operator-termination marker (task 029, #8).
    Thin wrapper over engine/state.py's single implementation so the CLI and
    the engine can never disagree about the file's name or shape."""
    record_operator_termination(run_root(run_id), action, reason=reason,
                               source="cli")


def _teardown_container(run_id: str, reason: str) -> None:
    """Take a run's containers down: ask the engine to shut down, remove the
    job container, reap the run's siblings, record the operator termination.

    The ONE implementation of that sequence (task 029, #19): `stop` and
    `rm --force` must not be able to drift apart on the order of the steps,
    on the sibling reaping, or on the label discipline `_reap_siblings()`
    documents. The marker is written last, after the container is gone, so
    nothing races the engine's own writes -- `stop` is the sharpest case:
    `--force` removes the container while status.json may still say
    `running`, which is exactly the shape of a crashed run, and without the
    marker `doctor --fix` would restart it.
    """
    try:
        api(run_id, "POST", "/shutdown")
    except SystemExit:
        pass
    time.sleep(1)
    sh([DOCKER, "rm", "-f", job_container_name(run_id)])
    _reap_siblings(run_id)
    _record_operator_termination(run_id, "stop", reason)


def cmd_stop(args):
    status = _read_json(run_root(args.run_id) / "status.json", {})
    if status.get("state") not in TERMINAL_STATES:
        if not args.force:
            die(5, "job still running — use `abort` first or `stop --force`")
        try:
            api(args.run_id, "POST", "/abort", {"reason": "stop --force"})
            time.sleep(2)
        except SystemExit:
            pass
    _teardown_container(args.run_id, "stopped by operator (`ralphctl stop`)")
    out(args, {"stopped": args.run_id}, f"stopped {args.run_id} (run dir kept)")


def container_record_exists(run_id: str) -> bool:
    """Does docker still know a container by this run's job container name?

    The one spelling of that question (task 030, #19): `ralphctl rm` asks it
    to decide whether it must refuse or stop first, and the hub's delete
    endpoint asks it through this same function -- so the two surfaces cannot
    drift on the container NAME or on what "exists" means.
    """
    return sh([DOCKER, "inspect", job_container_name(run_id)]).returncode == 0


def remove_run_state(run_id: str, *, container_exists: bool,
                     reason: str) -> None:
    """Take a run's containers and both of its directories away.

    The ONE removal sequence (task 030, #19), shared by `ralphctl rm` and the
    hub's `DELETE /api/runs/<id>`: leftover job container through
    `_teardown_container` (i.e. exactly `stop`'s path), siblings reaped even
    when there was no job container left to stop, then the run dir and the
    job config dir.

    Deliberately gate-free: whether this run MAY be deleted is the caller's
    question, and the two callers ask it differently on purpose -- `rm`
    accepts a zombie dir whose container is already gone, the hub insists on
    a recorded terminal state (see `ui_server.deletion_refusal`). Mixing the
    gate in here would force one policy on both.
    """
    if container_exists:
        # Exactly `stop`'s path (shutdown, job container, siblings, marker),
        # so a forced removal cannot drift from `stop` -- see
        # _teardown_container.
        _teardown_container(run_id, reason)
    else:
        # No container record: nothing to stop, but siblings can outlive it.
        _reap_siblings(run_id)
    shutil.rmtree(run_root(run_id), ignore_errors=True)
    shutil.rmtree(config_root(run_id), ignore_errors=True)


def cmd_rm(args):
    """Delete a run's state. Plain `rm` refuses while a container record
    exists (the safe default, unchanged); `--force` stops that container
    first and then deletes, so getting rid of a finished run is ONE command
    (task 029, #19).

    `--force` is a shortcut past a *stale* container, never a way to kill
    live work: it refuses unless the run's recorded state is terminal, and
    an absent/unreadable status.json counts as not-terminal (we cannot
    establish the job is over, so we do not act). Killing a live job stays
    explicit -- `abort`, or `stop --force`.
    """
    run_id = args.run_id
    container_exists = container_record_exists(run_id)
    if container_exists and not args.force:
        die(5, "container still exists — `stop` first (or `rm --force`)")
    if not run_root(run_id).exists():
        die(3, f"run {run_id} not found")
    state = _read_json(run_root(run_id) / "status.json", {}).get("state")
    if container_exists and state not in TERMINAL_STATES:
        die(5, f"job still running (state: {state or 'unknown'}) — `abort` "
               "first, then `rm --force`")
    if not args.yes and sys.stdin.isatty():
        reply = input(f"delete all state for {run_id}? [y/N] ")
        if reply.lower() != "y":
            sys.exit(1)
    remove_run_state(run_id, container_exists=container_exists,
                     reason="stopped by operator (`ralphctl rm --force`)")
    out(args, {"removed": run_id, "stoppedContainer": container_exists},
        f"stopped and removed {run_id}" if container_exists
        else f"removed {run_id}")


def _append_run_event(rdir: Path, type_: str, **data) -> dict:
    """CLI-side sibling of RunDir.emit() (src/ralphd/engine/state.py) for
    tooling that must append to a run's events.jsonl without a live engine
    holding the run dir's lock -- `repair`'s audit trail (PRD requirement
    E) being the first user. Deliberately never passed a secret value by
    any caller (repair only ever records file/field/key *names*, task ids,
    and issue descriptions -- never credential contents), so unlike
    RunDir.emit() this does not need scrub_text().
    """
    events_path = rdir / "events.jsonl"
    last = 0
    if events_path.is_file():
        for line in events_path.read_text().splitlines():
            try:
                last = max(last, json.loads(line).get("id", 0))
            except json.JSONDecodeError:
                continue
    event = {"id": last + 1, "ts": utcnow(), "type": type_, **data}
    with open(events_path, "a") as f:
        f.write(json.dumps(event) + "\n")
    return event


_TASK_STATUSES = ("pending", "in-progress", "completed", "validation-failed",
                  "failed", "skipped")
_STATUS_STATES = ("starting", "running", "succeeded", "failed", "aborted")
# Recorded states that mean "a live engine still owns this run": if no
# container exists for one of these, the run is a zombie (task 021). Defined
# once in engine/state.py (task 024) -- the hub server needs the same set.
_NONTERMINAL_STATUS_STATES = NONTERMINAL_STATES


def _diagnose_status_json(rdir: Path) -> list[str]:
    """Schema issues in status.json (docs/architecture.md 'State model'):
    must parse as a JSON object, carry a recognized 'state', and a
    schemaVersion this build actually knows (mirrors the engine's own
    refusal / doctor's registry_schema check, task 027/020)."""
    p = rdir / "status.json"
    if not p.is_file():
        return ["status.json: missing"]
    try:
        doc = json.loads(p.read_text())
    except json.JSONDecodeError as e:
        return [f"status.json: malformed JSON ({e})"]
    if not isinstance(doc, dict):
        return ["status.json: expected a JSON object"]
    issues = []
    state = doc.get("state")
    if state is None:
        issues.append("status.json: missing 'state' field")
    elif state not in _STATUS_STATES:
        issues.append(f"status.json: unrecognized state {state!r}")
    sv = doc.get("schemaVersion")
    if sv is not None and not isinstance(sv, int):
        issues.append("status.json: 'schemaVersion' should be an integer")
    elif isinstance(sv, int) and sv > CURRENT_SCHEMA_VERSION:
        issues.append(f"status.json: schemaVersion {sv} is newer than this "
                      f"build knows ({CURRENT_SCHEMA_VERSION})")
    return issues


def _diagnose_tasks_json(rdir: Path) -> list[str]:
    """Schema issues in tasks.json (docs/architecture.md 'tasks.json schema
    (v1)'). Absent entirely is normal for a run that hasn't finished
    planning yet -- not an error."""
    p = rdir / "tasks.json"
    if not p.is_file():
        return []
    try:
        doc = json.loads(p.read_text())
    except json.JSONDecodeError as e:
        return [f"tasks.json: malformed JSON ({e})"]
    if not isinstance(doc, dict):
        return ["tasks.json: expected a JSON object"]
    tasks = doc.get("tasks")
    if tasks is None:
        return ["tasks.json: missing 'tasks' list"]
    if not isinstance(tasks, list):
        return ["tasks.json: 'tasks' should be a list"]
    issues = []
    seen_ids: set[str] = set()
    for i, t in enumerate(tasks):
        if not isinstance(t, dict):
            issues.append(f"tasks.json: tasks[{i}] is not an object")
            continue
        tid = t.get("id")
        label = tid if tid else f"tasks[{i}]"
        if not tid:
            issues.append(f"tasks.json: tasks[{i}] missing 'id'")
        elif tid in seen_ids:
            issues.append(f"tasks.json: duplicate task id {tid!r}")
        else:
            seen_ids.add(tid)
        if not t.get("title"):
            issues.append(f"tasks.json: task {label} missing 'title'")
        status = t.get("status")
        if status not in _TASK_STATUSES:
            issues.append(f"tasks.json: task {label} has unrecognized "
                          f"status {status!r}")
    return issues


def _diagnose_host_json(rdir: Path) -> list[str]:
    """Schema issues in host.json (cmd_start's/cmd_resume's meta dict).
    Absent entirely means the container was never successfully launched
    (or the file was deleted) -- worth reporting, not fatal to diagnose."""
    p = rdir / "host.json"
    if not p.is_file():
        return ["host.json: missing"]
    try:
        doc = json.loads(p.read_text())
    except json.JSONDecodeError as e:
        return [f"host.json: malformed JSON ({e})"]
    if not isinstance(doc, dict):
        return ["host.json: expected a JSON object"]
    issues = []
    for key in ("runId", "container", "port", "apiUrl", "image", "startedAt"):
        if key not in doc:
            issues.append(f"host.json: missing '{key}' field")
    return issues


def _dangling_remedy(run_id: str) -> str:
    """THE remedy text for the dangling-container condition (task 025,
    issue #8): `doctor`'s registry sweep and `repair`'s per-run diagnosis
    both render this one string, so the operator can never read two
    recommendations pointing in different directions for the same run.

    One story, **resume-first**: the container died mid-run but the run
    dir (plan, notes, artifacts, iteration transcripts) is intact, so
    continuing the job is the useful default; `repair --set-state
    aborted` is the way to declare it over instead. Opt-in per-run
    auto-resume (`ralphctl doctor --fix`, task 027) automates exactly
    this `resume` step -- named here only, so this
    function stays the single source of the remedy.
    """
    return (f"continue it with `ralphctl resume {run_id}`, or record it as "
            f"over with `ralphctl repair {run_id} --set-state aborted` "
            f"(writes a reason naming the vanished container)")


def _diagnose_dangling_container(run_id: str) -> list[str]:
    """The dangling-container condition as a diagnosis line (task 021,
    requirement E): a run recorded non-terminal whose container is gone.
    Reuses `_dangling_run_entry` -- doctor's check -- rather than a second
    implementation, and names the fix (via `_dangling_remedy`, the same
    text doctor prints -- task 025) so `repair` stops reporting a zombie
    run as 'no issues found'."""
    entry = _dangling_run_entry(run_id)
    if entry is None:
        return []
    state = _read_json(run_root(run_id) / "status.json", {}).get("state")
    issue = (f"container: {entry['container']} no longer exists, but "
             f"status.json still records state {state!r} -- the container "
             f"died or was removed outside ralphctl; "
             f"{_dangling_remedy(run_id)}")
    return [issue]


def cmd_repair(args):
    """PRD requirement E: non-interactive diagnosis (and, guarded, fixes)
    for a run dir left in an inconsistent shape by a crash outside the
    paths the engine's own crash-consistency handling already covers.
    Refuses to touch any run whose container is currently running -- a
    live engine already owns that run dir's on-disk state -- and appends
    a `type: repair` audit line to events.jsonl for every invocation,
    describing what was checked/changed (never any secret value).

    `--set-state <state>` (task 009) is a guarded escape hatch for a run
    whose container died without the engine ever writing a terminal
    state -- it overwrites status.json's 'state' field directly, bypassing
    diagnosis, after validating the requested value against the same
    `_STATUS_STATES` list diagnosis checks against. When the run was in
    fact a zombie (recorded non-terminal, container gone -- task 021's
    condition), it also writes a `reason` saying the container vanished,
    so the terminal state on disk is self-explaining rather than a bare
    state flip.

    `--env KEY=VAL` (task 010) adds/updates a recorded value in the
    persisted env wiring (`env-wiring.json`, task 001's
    `_write_extra_env_wiring`/`_read_extra_env_wiring`) -- the exact
    hand-edit the operator performed live before this feature existed,
    done safely: 0600 preserved, the value never echoed to stdout/stderr
    or written into the audit event (only the KEY name is recorded).
    """
    run_id = args.run_id
    _require_run(run_id)
    rdir = run_root(run_id)
    name = job_container_name(run_id)
    running = _container_running(name)
    if running:
        die(5, f"container {name} is running -- repair refuses to touch a "
               f"live run; `abort` or `stop` it first")

    set_state = getattr(args, "set_state", None)
    # the zombie condition, computed once for both the --set-state reason
    # and diagnosis (doctor's check, one implementation).
    dangling = _dangling_run_entry(run_id)
    if set_state is not None:
        if set_state not in _STATUS_STATES:
            die(2, f"--set-state: invalid state {set_state!r} (expected "
                   f"one of {', '.join(_STATUS_STATES)})")
        status_path = rdir / "status.json"
        try:
            doc = json.loads(status_path.read_text()) if status_path.is_file() else {}
        except json.JSONDecodeError as e:
            die(1, f"status.json: malformed JSON ({e}) -- fix it by hand "
                   f"before --set-state")
        if not isinstance(doc, dict):
            die(1, "status.json: expected a JSON object")
        old_state = doc.get("state")
        doc["state"] = set_state
        # task 021: a zombie run (recorded non-terminal, container gone)
        # gets a reason explaining the vanished container -- the same
        # field the engine writes on its own terminal transitions.
        reason = None
        if dangling is not None:
            reason = (f"container {dangling['container']} no longer exists "
                      f"(died or was removed outside ralphctl); state "
                      f"{old_state!r} -> {set_state!r} by `ralphctl repair "
                      f"--set-state`")
            doc["reason"] = reason
        status_path.write_text(json.dumps(doc, indent=2))
        try:
            status_path.chmod(0o600)
        except OSError:
            pass
        _append_run_event(rdir, "repair", action="set-state",
                          old=old_state, new=set_state, reason=reason)
        result = {"runId": run_id, "action": "set-state", "old": old_state,
                  "new": set_state, "reason": reason}
        human = f"{run_id}: state {old_state!r} -> {set_state!r}"
        if reason:
            human += f"\n  reason: {reason}"
        out(args, result, human)
        sys.exit(0)

    env_updates = getattr(args, "env", None)
    if env_updates:
        cdir = config_root(run_id)
        pairs = _read_extra_env_wiring(cdir)
        # ordered dict of name -> value so "add or update" replaces an
        # existing key in place (rather than appending a shadowing
        # duplicate) -- keeps env-wiring.json's list one entry per name.
        by_name: dict[str, str] = {}
        order: list[str] = []
        for kv in pairs:
            k, _, v = kv.partition("=")
            if k not in by_name:
                order.append(k)
            by_name[k] = v
        updated_keys: list[str] = []
        for kv in env_updates:
            k, sep, v = kv.partition("=")
            if not sep or not k:
                die(2, f"--env: expected KEY=VAL, got {kv!r}")
            if k not in by_name:
                order.append(k)
            by_name[k] = v
            updated_keys.append(k)
        new_pairs = [f"{k}={by_name[k]}" for k in order]
        _write_extra_env_wiring(cdir, new_pairs)
        _append_run_event(rdir, "repair", action="env", keys=updated_keys)
        result = {"runId": run_id, "action": "env", "keys": updated_keys}
        out(args, result, f"{run_id}: updated env wiring key(s): "
                          f"{', '.join(updated_keys)}")
        sys.exit(0)

    checked = ["status.json", "tasks.json", "host.json", "container"]
    issues = (_diagnose_status_json(rdir) + _diagnose_tasks_json(rdir)
              + _diagnose_host_json(rdir) + _diagnose_dangling_container(run_id))
    _append_run_event(rdir, "repair", action="diagnose", checked=checked,
                      issueCount=len(issues), issues=issues)
    result = {"runId": run_id, "checked": checked, "issues": issues,
              "ok": not issues, "dangling": dangling}
    if issues:
        human = (f"{run_id}: {len(issues)} issue(s) found\n"
                  + "\n".join(f"  - {i}" for i in issues))
    else:
        human = f"{run_id}: no issues found ({', '.join(checked)})"
    out(args, result, human)
    sys.exit(0 if not issues else 1)


def cmd_docs(args):
    """A run's state documents (task 021, #18.2): `notes.md`,
    `review-findings.md`, `composite-prd.md` and the effective `job.yaml`
    -- the prose an operator goes looking for when a run ended badly, and
    which until now was only reachable by knowing the registry layout and
    `cat`-ing files (which is also how `job.yaml`'s secrets got read out
    loud).

    With no document named: the LISTING -- every known document with its size
    or the fact that this run never wrote it (`state.RUN_DOCUMENT_ABSENT`),
    because *which* documents exist is itself part of the answer. With one
    named (key or file name, e.g. `notes` or `notes.md`): its header block
    plus the whole body.

    `job.yaml` is redacted mechanically -- masked by key name AND scrubbed by
    value, see `engine.redact.redact_job_yaml` -- so this command is a safe
    thing to paste into an issue, unlike the `cat` it replaces.

    Purely on-disk, like `ralphctl iteration`: every one of these files is
    written by the engine, the agent or `start` itself into directories the
    host holds, so a live run and one whose container is long gone read
    identically and there is no live API to fall back from.
    """
    _require_run(args.run_id)
    root = run_root(args.run_id)
    cdir = config_root(args.run_id)
    if not args.name:
        docs = run_documents(root, cdir, bodies=False)
        out(args, {"runId": args.run_id, "documents": docs},
            "\n".join([f"run:       {args.run_id}",
                       *format_run_document_listing(docs)]))
        return
    doc = run_document(root, args.name, cdir)
    if doc is None:
        die(2, f"unknown document: {args.name} (expected one of "
               f"{', '.join(run_document_keys())}, or a file name)")
    if not doc["exists"]:
        present = [d["key"] for d in run_documents(root, cdir, bodies=False)
                   if d["exists"]]
        die(1, f"run {args.run_id} has no {doc['name']} "
               f"({RUN_DOCUMENT_ABSENT}; on disk: "
               f"{', '.join(present) if present else 'none of them'})")
    if args.json:
        # `text` is the same complete rendering the human output prints (and
        # the hub dialog shows, task 022), `body` the document alone.
        print(json.dumps({"runId": args.run_id, **doc,
                          "text": run_document_text(doc)}, indent=2))
        return
    print(f"run:       {args.run_id}")
    text = run_document_text(doc)
    # A document body normally ends in its own newline; `print` would add a
    # second one and make the output not `cat`-like.
    sys.stdout.write(text if text.endswith("\n") else text + "\n")


# Where `ralphctl artifacts <run> pull` copies to when no destination is
# given -- spelled once, since the parser's help text quotes it.
_DEFAULT_ARTIFACTS_DEST = "./artifacts"


def cmd_artifacts(args):
    """What the job left behind in `artifacts/` (task 023, #18.3).

    `ls` lists the tree (well-known files labelled with the name they can be
    asked for); `show <name>` prints ONE artifact inline -- above all the
    reflect phase's `report`/`suggestions`, which were previously reachable
    only by knowing the registry layout and `cat`-ing files; `pull` copies the
    whole directory out, unchanged.

    Purely on-disk, like `ralphctl docs`/`iteration`: these files are written
    by the agent into a directory the host holds, so a live run and one whose
    container is long gone read identically and there is no live API to fall
    back from.
    """
    _require_run(args.run_id)
    root = run_root(args.run_id)
    if args.action == "pull":
        dest = Path(args.name or _DEFAULT_ARTIFACTS_DEST)
        dest.mkdir(parents=True, exist_ok=True)
        shutil.copytree(root / "artifacts", dest, dirs_exist_ok=True)
        out(args, {"pulled": str(dest)}, f"artifacts copied to {dest}")
        return
    if args.action == "show":
        if not args.name:
            die(2, "artifacts show needs a name (one of "
                   f"{', '.join(artifact_names())}, or a path under "
                   "artifacts/)")
        entry = artifact(root, args.name)
        if entry is None:
            die(2, f"not an artifact name: {args.name} (expected one of "
                   f"{', '.join(artifact_names())}, or a path under "
                   "artifacts/)")
        if not entry["exists"]:
            present = [e["path"] for e in artifact_entries(root)]
            die(1, f"run {args.run_id} has no artifacts/{entry['path']} "
                   f"({RUN_DOCUMENT_ABSENT}; on disk: "
                   f"{', '.join(present) if present else 'nothing'})")
        text = artifact_text(entry)
        if args.json:
            # `text` is the same complete rendering the human output prints
            # (and the hub dialog shows, task 024), `body` the artifact alone.
            print(json.dumps({"runId": args.run_id, **entry, "text": text},
                             indent=2))
            return
        print(f"run:       {args.run_id}")
        # An artifact normally ends in its own newline; `print` would add a
        # second one and make the output not `cat`-like.
        sys.stdout.write(text if text.endswith("\n") else text + "\n")
        return
    entries = artifact_entries(root)
    out(args, {"runId": args.run_id, "artifacts": entries},
        "\n".join([f"run:       {args.run_id}",
                   *(format_artifact_listing(entries) if entries
                     else [NO_ARTIFACTS])]))


def cmd_ui(args):
    """Local hub HTTP server (PRD reqs 21-22): JSON endpoints reading the
    registry's run dirs and proxying live container APIs, plus (once task
    034 populates it) the static bundle. Runs in the foreground until
    interrupted -- same shape as any other long-lived dev server."""
    reg = registry()
    port = args.port or free_port()
    server = ui_server.make_server(reg, args.bind, port)
    print(f"ralphctl: serving hub at http://{args.bind}:{port} "
          f"(registry: {reg})", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


# Keys `ralphctl config get/set` (task 038, PRD req 25) knows about, and any
# extra validation applied on `set`. Mirrors the registry-wide defaults
# `_apply_template()` layers between a template and the hardcoded fallback
# (see `_REGISTRY_CONFIG_FIELD_KEYS` above) plus `default_llm_profile`,
# which doctor already reads independently.
_CONFIG_KEYS = {"image": None, "on_complete": ("idle", "exit"),
                "default_llm_profile": None, "network": None,
                "auto_resume": ("true", "false"),
                "price_strategy": PRICE_STRATEGIES}
# keys stored as real booleans in `<registry>/config.yaml` (task 026)
_CONFIG_BOOL_KEYS = {"auto_resume"}


def cmd_config(args):
    """Registry-wide defaults at `<registry>/config.yaml` (PRD req 25):
    `image`, `on_complete`, `default_llm_profile`, `network`,
    `auto_resume`, `price_strategy`. `start` layers these in between a
    `--template`'s value and the hardcoded fallback (see `_apply_template`);
    `doctor` reads `default_llm_profile` directly."""
    if args.key not in _CONFIG_KEYS:
        die(2, f"unknown config key: {args.key} (expected one of "
                f"{', '.join(sorted(_CONFIG_KEYS))})")
    reg = registry()
    if args.action == "get":
        cfg = _registry_config(reg)
        val = cfg.get(args.key)
        out(args, {"key": args.key, "value": val},
            f"{args.key}: {val}" if val is not None else f"{args.key}: (unset)")
        return
    choices = _CONFIG_KEYS[args.key]
    if choices is not None and args.value not in choices:
        die(2, f"{args.key} must be one of {', '.join(choices)}")
    reg.mkdir(parents=True, exist_ok=True)
    cfg = _registry_config(reg)
    value = _as_bool(args.value) if args.key in _CONFIG_BOOL_KEYS else args.value
    cfg[args.key] = value
    (reg / "config.yaml").write_text(
        yaml.safe_dump(cfg, default_flow_style=False, sort_keys=True))
    out(args, {"key": args.key, "value": value},
        f"{args.key}: {value}")


def _auto_resume_dangling(args, dangling: list[dict]) -> dict:
    """`doctor --fix`'s self-recovery sweep (task 027, issue #8, PRD req F):
    resume every dangling run (recorded non-terminal, container gone) that
    is opted in to `auto_resume`, leave the opted-out ones alone.

    Deliberately *not* a daemon: `doctor --fix` is meant to be run from
    cron/systemd (see docs/cli.md), which keeps ralphd's process model at
    "one container per job, nothing long-lived on the host" -- the sweep is
    idempotent, so running it every minute costs one registry scan plus one
    `docker inspect` per run.

    The actual restart goes through `cmd_resume` -- the very code path an
    operator-typed `ralphctl resume` takes -- so an auto-resumed container
    reproduces the run's original wiring (run-dir/config-dir/workspace
    mounts, recorded --llm + --env wiring, `ralphd.run` label) by
    construction and can never drift from it. Its `out()`/`die()` calls are
    contained here: stdout is swallowed (doctor prints its own report) and
    SystemExit from a failed `docker run` is turned into a per-run failure
    entry, so one broken run can't abort the sweep.

    Task 028 (#8) adds the crash-loop guard: every attempt is recorded in the
    run dir's `autoResume` record *before* the resume is issued (so a sweep
    that dies mid-resume still counts the attempt), consecutive attempts are
    spaced by `AUTO_RESUME_BACKOFF_S`, and after `maxAttempts` attempts with
    no progress the run is left alone with a readable give-up reason.

    Task 029 (#8) adds the two refusals that protect the operator: a run
    whose termination was *operator-initiated* (`abort`/`stop`, recorded in
    the run dir by `record_operator_termination`) is never resurrected, and
    the dangling condition is re-checked immediately before each resume so a
    run that reached a terminal state (or whose container came back) between
    doctor's registry scan and this moment is left alone. Terminal runs never
    enter this function in the first place -- `_dangling_run_entry` only ever
    matches a non-terminal recorded state -- but the scan-to-resume window is
    real, and "resumed a job that had already succeeded" is unrecoverable.

    Returns `{resumed: [...], skipped: [...], failed: [{runId, error}],
    waiting: [{runId, attempts, nextAttemptAt}],
    gaveUp: [{runId, attempts, reason}],
    operatorTerminated: [{runId, action, at, reason}],
    recovered: [...]}` -- `skipped` stays exactly "opted out of
    auto_resume", the guard's two refusals and task 029's two are their own
    buckets so a cron consumer can tell "not now" from "never again" from
    "never asked" from "deliberately killed".
    """
    resumed: list[str] = []
    skipped: list[str] = []
    failed: list[dict] = []
    waiting: list[dict] = []
    gave_up: list[dict] = []
    operator_terminated: list[dict] = []
    recovered: list[str] = []
    for entry in dangling:
        run_id = entry["runId"]
        term = read_operator_termination(run_root(run_id))
        if term is not None:
            operator_terminated.append(
                {"runId": run_id, "action": term.get("action"),
                 "at": term.get("at"), "reason": term.get("reason") or ""})
            continue
        if not _read_auto_resume_setting(run_id):
            skipped.append(run_id)
            continue
        verdict, state = _auto_resume_decision(run_id)
        if verdict == "gave-up":
            if not state["gaveUp"] or not state["reason"]:
                state = {**state, "gaveUp": True,
                         "reason": _auto_resume_give_up_reason(state)}
                _write_auto_resume_state(run_id, state)
            gave_up.append({"runId": run_id, "attempts": state["attempts"],
                            "reason": state["reason"]})
            continue
        if verdict == "waiting":
            waiting.append({"runId": run_id, "attempts": state["attempts"],
                            "nextAttemptAt": _auto_resume_next_attempt_at(state)})
            continue
        if _dangling_run_entry(run_id) is None:
            # no longer a zombie: it finished on its own, or its container is
            # back (a slow `resume`, an operator who got there first). THE
            # condition, re-asked -- never a second implementation of it.
            recovered.append(run_id)
            continue
        status = _read_json(run_root(run_id) / "status.json", {}) or {}
        used = status.get("iterationsUsed")
        state = {**state, "attempts": state["attempts"] + 1,
                 "lastAt": utcnow(),
                 "iterationsUsed": used if isinstance(used, int) else None,
                 "gaveUp": False, "reason": None}
        _write_auto_resume_state(run_id, state)
        rargs = argparse.Namespace(
            run_id=run_id, iterations=None, image=args.image, port=None,
            api_bind="127.0.0.1", network=None, allow_docker=False,
            detach=True, json=False)
        buf = io.StringIO()
        try:
            with contextlib.redirect_stdout(buf):
                cmd_resume(rargs)
        except SystemExit as e:
            if e.code:
                failed.append({"runId": run_id,
                               "error": f"resume exited {e.code}"})
                continue
        except (OSError, ValueError) as e:
            failed.append({"runId": run_id, "error": str(e)})
            continue
        resumed.append(run_id)
    return {"resumed": resumed, "skipped": skipped, "failed": failed,
            "waiting": waiting, "gaveUp": gave_up,
            "operatorTerminated": operator_terminated,
            "recovered": recovered}


def cmd_doctor(args):
    checks = {}
    checks["docker"] = sh([DOCKER, "version", "--format", "{{.Server.Version}}"]) \
        .returncode == 0
    checks["image"] = sh([DOCKER, "image", "inspect", args.image]).returncode == 0
    reg = registry()
    reg.mkdir(parents=True, exist_ok=True)
    checks["registry"] = os.access(reg, os.W_OK)
    checks["pi_host_config"] = (Path.home() / ".pi" / "agent" / "settings.json").exists()

    default_profile_name = _registry_config(reg).get("default_llm_profile", "host")
    default_llm_profile_error = None
    if default_profile_name in ("host", "none"):
        checks["default_llm_profile"] = True
    else:
        try:
            llm_profiles.resolve_profile(default_profile_name, reg, redact=True)
            checks["default_llm_profile"] = True
        except llm_profiles.ProfileError as e:
            checks["default_llm_profile"] = False
            default_llm_profile_error = str(e)

    registry_issues = _registry_schema_issues(reg)
    checks["registry_schema"] = not registry_issues

    strays = _stray_sibling_containers()
    dangling = _dangling_registry_entries()
    # host-network jobs share the host's network namespace, so docker's
    # normal port-publish isolation (`-p host:container`) doesn't apply --
    # the API binds `--api-bind` directly on the host. Report-only, never
    # affects the verdict (mirrors strays/dangling below).
    configured_network = _registry_config(reg).get("network")
    host_network_note = None
    if configured_network == "host":
        host_network_note = (
            "registry default network is 'host': jobs share the host "
            "network namespace, so the API binds --api-bind directly on "
            "the host with no docker port-publish isolation"
        )
    auto_resume = None
    if getattr(args, "fix", False):
        auto_resume = _auto_resume_dangling(args, dangling)
    # strays/dangling registry entries are report-only, never affect the verdict
    ok = all(checks.values())
    report = "\n".join(f"{'✓' if v else '✗'} {k}" for k, v in checks.items())
    if default_llm_profile_error:
        report += f"\n    default LLM profile ({default_profile_name!r}): {default_llm_profile_error}"
    if host_network_note:
        report += f"\n! {host_network_note}"
    if registry_issues:
        report += "\n! registry schema issues:"
        for issue in registry_issues:
            report += f"\n    {issue}"
    if strays:
        report += "\n! stray ralphd.run containers (no matching run dir):"
        for s in strays:
            report += f"\n    {s['id'][:12]}  ralphd.run={s['runId']}"
        report += "\n  clean up with: docker rm -f <id>"
    if dangling:
        report += "\n! registry entries recorded running with no matching container:"
        for d in dangling:
            report += f"\n    {d['runId']}  container={d['container']}"
            if auto_resume is not None and d["runId"] in auto_resume["resumed"]:
                # already restarted by this very sweep -- printing the manual
                # remedy here would be stale advice
                report += ("\n      the container died or was removed outside "
                           "ralphctl; auto-resumed (auto_resume enabled)")
            else:
                if auto_resume is not None:
                    if d["runId"] in auto_resume["skipped"]:
                        report += ("\n      not auto-resumed: auto_resume is "
                                   "off for this run")
                    # Task 029: the operator killed this one on purpose.
                    term = next((t for t in auto_resume["operatorTerminated"]
                                 if t["runId"] == d["runId"]), None)
                    if term:
                        report += (f"\n      not auto-resumed: terminated by "
                                   f"the operator (`{term['action']}` at "
                                   f"{term['at']}) -- auto-recovery never "
                                   f"restarts a deliberately stopped run")
                    if d["runId"] in auto_resume["recovered"]:
                        report += ("\n      not auto-resumed: no longer "
                                   "dangling as of this sweep (finished, or "
                                   "its container is back)")
                    # Task 028: the crash-loop guard's two refusals, said out
                    # loud -- "nothing happened" must never be silent.
                    wait = next((w for w in auto_resume["waiting"]
                                 if w["runId"] == d["runId"]), None)
                    if wait:
                        report += (f"\n      not auto-resumed yet: crash-loop "
                                   f"backoff after {wait['attempts']} "
                                   f"attempt(s), next attempt "
                                   f"{_countdown_to(wait['nextAttemptAt'])}")
                    gave = next((g for g in auto_resume["gaveUp"]
                                 if g["runId"] == d["runId"]), None)
                    if gave:
                        report += f"\n      {gave['reason']}"
                    err = next((f["error"] for f in auto_resume["failed"]
                                if f["runId"] == d["runId"]), None)
                    if err:
                        report += f"\n      auto-resume FAILED: {err}"
                # same remedy text `repair` prints for this run (task 025):
                # one story, never two commands pointing different ways.
                report += (f"\n      the container died or was removed outside "
                           f"ralphctl; {_dangling_remedy(d['runId'])}")
    if auto_resume is not None:
        report += (f"\n! --fix: auto-resumed {len(auto_resume['resumed'])}, "
                   f"left {len(auto_resume['skipped'])} opted out, "
                   f"{len(auto_resume['operatorTerminated'])} operator-"
                   f"terminated, "
                   f"{len(auto_resume['waiting'])} in crash-loop backoff, "
                   f"{len(auto_resume['gaveUp'])} given up on, "
                   f"{len(auto_resume['failed'])} failed")
    out(args, {"ok": ok, "checks": checks, "strayContainers": strays,
               "danglingRegistryEntries": dangling, "registryIssues": registry_issues,
               "defaultLlmProfile": default_profile_name,
               "defaultLlmProfileError": default_llm_profile_error,
               "hostNetworkApiBindNote": host_network_note,
               "autoResume": auto_resume}, report)
    sys.exit(0 if ok else 1)


def _registry_config(reg: Path) -> dict:
    """Registry-wide defaults (`ralphctl config`, task 038) at
    `<registry>/config.yaml`. Missing/malformed -> {} (doctor's registry
    schema check reports malformed separately; this just degrades quietly
    since callers only ever read a single optional key with a fallback)."""
    p = reg / "config.yaml"
    if not p.is_file():
        return {}
    try:
        return yaml.safe_load(p.read_text()) or {}
    except yaml.YAMLError:
        return {}


def _registry_schema_issues(reg: Path) -> list[str]:
    """Malformed registry entries: llm-profile YAML that won't parse, a run's
    status.json that won't parse, or a run's recorded schemaVersion newer
    than this build knows (mirrors the engine's own refusal, task 027)."""
    issues = []
    pdir = llm_profiles.profiles_dir(reg)
    if pdir.is_dir():
        for p in sorted(pdir.glob("*.yaml")):
            try:
                yaml.safe_load(p.read_text())
            except yaml.YAMLError as e:
                issues.append(f"malformed llm profile {p.name}: {e}")
    runs_dir = reg / "runs"
    if runs_dir.is_dir():
        for d in sorted(runs_dir.iterdir()):
            sp = d / "status.json"
            if not sp.is_file():
                continue
            try:
                doc = json.loads(sp.read_text())
            except json.JSONDecodeError as e:
                issues.append(f"malformed status.json for run {d.name}: {e}")
                continue
            sv = doc.get("schemaVersion", 0)
            if sv > CURRENT_SCHEMA_VERSION:
                issues.append(f"run {d.name}: schemaVersion {sv} is newer than "
                              f"this build knows ({CURRENT_SCHEMA_VERSION})")
    return issues


def _stray_sibling_containers() -> list[dict]:
    """Containers labeled ralphd.run=<id> whose run id has no registry dir."""
    res = sh([DOCKER, "ps", "-aq", "--filter", "label=ralphd.run"])
    if res.returncode != 0 or not res.stdout.strip():
        return []
    strays = []
    for cid in res.stdout.split():
        insp = sh([DOCKER, "inspect", "--format",
                   '{{index .Config.Labels "ralphd.run"}}', cid])
        if insp.returncode != 0:
            continue
        rid = insp.stdout.strip()
        if rid and not run_root(rid).exists():
            strays.append({"id": cid, "runId": rid})
    return strays


def _dangling_run_entry(run_id: str) -> dict | None:
    """THE dangling-container condition, in its single-run form: a run dir
    whose status.json records a non-terminal state but whose container no
    longer exists at all (crashed/removed outside ralphctl, e.g. `docker
    rm -f` by hand). The reverse of `_stray_sibling_containers`.

    One implementation, shared by `doctor`'s global report-only sweep
    (`_dangling_registry_entries`) and `repair`'s per-run diagnosis (task
    021) -- the two must never be able to disagree about whether a given
    run is a zombie. Returns `{runId, container}` or None (not dangling:
    either the recorded state is terminal, or a container by that name
    still exists, running or exited)."""
    status = _read_json(run_root(run_id) / "status.json", {})
    if status.get("state") not in _NONTERMINAL_STATUS_STATES:
        return None
    name = job_container_name(run_id)
    if _container_running(name) is not None:
        return None
    return {"runId": run_id, "container": name}


def _dangling_registry_entries() -> list[dict]:
    """Every run in the registry matching `_dangling_run_entry`."""
    runs_dir = registry() / "runs"
    if not runs_dir.is_dir():
        return []
    dangling = []
    for d in sorted(runs_dir.iterdir()):
        entry = _dangling_run_entry(d.name)
        if entry is not None:
            dangling.append(entry)
    return dangling


# ---------------------------------------------------------------- parser
_TAIL_SYNTAX_RE = re.compile(r"^-(\d+)(f)?$")


def _preprocess_logs_argv(argv: list[str]) -> list[str]:
    """Rewrite `tail`-style `logs` syntax (`-N`, `-Nf`, `-f`) and the
    `logsf` alias into `--tail`/`--follow` before argparse sees them, since
    argparse cannot parse a bare `-100`-style token as a positional value.
    Anything that doesn't match a recognized form is left untouched, so
    argparse's own "unrecognized arguments" error (exit 2) handles it."""
    out = list(argv)
    idx = next((i for i, t in enumerate(out) if not t.startswith("-")), None)
    if idx is None:
        return out
    cmd = out[idx]
    force_follow = False
    if cmd == "logsf":
        out[idx] = "logs"
        force_follow = True
    elif cmd != "logs":
        return out
    result = out[:idx + 1]
    for tok in out[idx + 1:]:
        if tok == "-f":
            result.append("--follow")
            continue
        m = _TAIL_SYNTAX_RE.match(tok)
        if m:
            result.append("--tail")
            result.append(m.group(1))
            if m.group(2):
                result.append("--follow")
            continue
        result.append(tok)
    if force_follow:
        result.append("--follow")
    return result


def main() -> None:
    p = argparse.ArgumentParser(prog="ralphctl",
                                description="Operate ralphd autonomous coding jobs")
    p.add_argument("--version", action="version", version=__version__)
    p.add_argument("--json", action="store_true", help="machine-readable output")
    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser("start", help="launch a job container")
    s.add_argument("--prd", default=None,
                   help="PRD markdown file, or - for stdin (optional if "
                        "--template supplies a prd.md skeleton)")
    s.add_argument("--template", metavar="NAME",
                   help="load job defaults + optional prd.md/skills/creds from "
                        "<registry>/templates/<name>/ (docs/cli.md); explicit "
                        "flags on this command override the template's values")
    s.add_argument("--workspace", action="append", metavar="DIR[:NAME]",
                   help="host dir to mount at /workspace (repeatable; each "
                        "extra one needs :NAME, mounted at /workspace/NAME "
                        "-- a single unnamed --workspace mounts at /workspace "
                        "exactly as before)")
    s.add_argument("--run-id")
    s.add_argument("--iterations", type=int, default=None)
    s.add_argument("--max-approaches", type=int, default=None)
    s.add_argument("--vigilant", action="store_true", default=None)
    s.add_argument("--reflect", action="store_true", default=None,
                   help="run one extra 'reflect' iteration after the job "
                        "reaches a terminal state, proposing prompt/skill "
                        "improvements to artifacts/reflection/")
    s.add_argument("--model", help="pi model ref, e.g. provider/model-id")
    s.add_argument("--fast-model")
    s.add_argument("--model-strategy", default=None,
                   choices=["quality-first", "cost-optimized", "balanced"])
    s.add_argument("--thinking", help="pi thinking level")
    s.add_argument("--price-strategy", default=None, choices=list(PRICE_STRATEGIES),
                   help="derive a cost for routes the provider does not price "
                        "(or prices with an implausible zero): 'aws' uses the "
                        "built-in AWS Bedrock rate table, 'none' leaves such a "
                        "cost unknown [default: none, or the template/registry/"
                        "llm-profile value]")
    s.add_argument("--llm", default=None,
                   help="LLM profile: host|none, or a name from "
                        "<registry>/llm-profiles/<name>.yaml (docs/llm-profiles.md) "
                        "[default: host, or the template's value]")
    s.add_argument("--llm-env", action="append", metavar="KEY=VAL")
    s.add_argument("--forward-env", action="append", metavar="NAME|PREFIX_*",
                   help="forward host env var(s) into the container (repeatable)")
    s.add_argument("--env", action="append", metavar="KEY=VAL")
    s.add_argument("--skills", action="append", metavar="DIR")
    s.add_argument("--creds", metavar="DIR",
                   help="copy *.env + recognized extras from DIR into the job's"
                        " config dir creds/")
    s.add_argument("--allow-docker", action="store_true",
                   help="mount the host docker socket into the job container "
                        "(ROOT-EQUIVALENT host access — trusted PRDs only)")
    s.add_argument("--image", default=None)
    s.add_argument("--on-complete", default=None, choices=["idle", "exit"],
                   help="post-completion behavior (default: exit; idle is an "
                        "explicit debugging opt-in)")
    s.add_argument("--on-complete-cmd", default=None, metavar="CMD",
                   help="shell command run once by the engine (in-container) "
                        "on reaching a terminal state, with RALPHD_RUN_ID/"
                        "RALPHD_STATE/RALPHD_VERDICT set; failures are logged, "
                        "never affect the job's verdict")
    s.add_argument("--timeout", type=int, default=None, metavar="MINUTES")
    s.add_argument("--iteration-timeout", type=int, default=None, metavar="MINUTES")
    s.add_argument("--infra-outage-budget", type=int, default=None, metavar="SECONDS",
                   help="wall-clock budget for riding out one LLM-endpoint "
                        "outage: infra-classified faults keep being retried "
                        "while the accumulated wait of an outage episode is "
                        "under this many seconds (engine default 14400 = 4h). "
                        "Waiting costs no iterations and no approaches")
    s.add_argument("--auto-resume", dest="auto_resume", action="store_true",
                   default=None,
                   help="opt this run in to self-recovery: `ralphctl doctor "
                        "--fix` resumes it if its container vanishes while "
                        "the run is still non-terminal (default: off, or the "
                        "template's / `ralphctl config set auto_resume` value)")
    s.add_argument("--no-auto-resume", dest="auto_resume", action="store_false",
                   default=None,
                   help="opt this run out of self-recovery even when a "
                        "template or registry default enables it")
    s.add_argument("--port", type=int)
    s.add_argument("--api-bind", default="127.0.0.1")
    s.add_argument("--network", default=None, metavar="NET",
                   help="docker network for the job container (e.g. 'host' "
                        "to share the host network namespace -- lets the job "
                        "reach host-only/VPN/tailnet services; the API then "
                        "binds --api-bind directly instead of -p publishing)")
    s.add_argument("--api-token", help="token value, or 'auto'")
    s.add_argument("--no-detach", dest="detach", action="store_false")
    s.set_defaults(func=cmd_start, detach=True)

    s = sub.add_parser("runs", help="list runs (newest first)")
    s.add_argument("--state")
    s.add_argument("--sort", choices=sorted(RUN_SORT_KEYS),
                   default=RUN_SORT_DEFAULT,
                   help="sort key (same keys as the hub's run-list columns); "
                        f"default {RUN_SORT_DEFAULT} (newest first)")
    s.add_argument("--reverse", action="store_true",
                   help="flip the sort key's natural direction")
    s.set_defaults(func=cmd_runs)

    for name, fn, extra in [
        ("status", cmd_status, None), ("tasks", cmd_tasks, None),
        ("watch", cmd_watch, None), ("interrupt", cmd_interrupt, None),
        ("pause", cmd_pause, None), ("unpause", cmd_unpause, None),
    ]:
        s = sub.add_parser(name)
        s.add_argument("run_id")
        s.set_defaults(func=fn)

    s = sub.add_parser("resume", help="start a fresh container over an "
                       "existing run dir (crash recovery / budget top-up)")
    s.add_argument("run_id")
    s.add_argument("--iterations", metavar="+N",
                   help="budget top-up, e.g. +10 (adds to the existing "
                        "budget); a bare integer sets it absolutely")
    s.add_argument("--image", default=DEFAULT_IMAGE)
    s.add_argument("--port", type=int)
    s.add_argument("--api-bind", default="127.0.0.1")
    s.add_argument("--network", default=None, metavar="NET",
                   help="docker network override; defaults to the network "
                        "recorded at start time")
    s.add_argument("--allow-docker", action="store_true",
                   help="remount the host docker socket (ROOT-EQUIVALENT "
                        "host access -- trusted PRDs only)")
    s.add_argument("--no-detach", dest="detach", action="store_false")
    s.set_defaults(func=cmd_resume, detach=True)

    s = sub.add_parser("logs", help="agent transcript (tail-style: -N, -Nf, -f)")
    s.add_argument("run_id")
    s.add_argument("--iteration", type=int, help="restrict to a single iteration")
    s.add_argument("--tail", type=int, help="defaults to 50 unless --follow given alone")
    s.add_argument("--follow", action="store_true")
    s.add_argument("--raw", action="store_true",
                   help="raw NDJSON passthrough (no pretty rendering)")
    s.set_defaults(func=cmd_logs)

    s = sub.add_parser("iteration", help="one iteration's detail: phase, "
                       "timing, exit reason, tokens/cost and its full log")
    s.add_argument("run_id")
    s.add_argument("number", type=int, metavar="N",
                   help="iteration number, as shown by `status`/`logs`")
    s.add_argument("--no-log", action="store_true",
                   help="header only -- skip the transcript")
    s.set_defaults(func=cmd_iteration)

    s = sub.add_parser("fault", help="explain a run's current/last fault: "
                       "class, matched signature, retry ladder, outage budget")
    s.add_argument("run_id")
    s.set_defaults(func=cmd_fault)

    s = sub.add_parser("cost", help="what a run spent, per phase and per "
                       "approach, labelling priced/derived/unavailable money")
    s.add_argument("run_id")
    s.set_defaults(func=cmd_cost)

    s = sub.add_parser("docs", help="a run's state documents: notes, review "
                       "findings, composite PRD, effective job.yaml (redacted)")
    s.add_argument("run_id")
    s.add_argument("name", nargs="?",
                   help="document key or file name (default: list them all)")
    s.set_defaults(func=cmd_docs)

    # `logsf <id>` is a pure alias for `logs <id> -f`, rewritten in
    # _preprocess_logs_argv() before argparse ever sees "logsf".

    s = sub.add_parser("skills", help="inspect or hot-swap skills on a running job")
    s.add_argument("run_id")
    sksub = s.add_subparsers(dest="action", required=True)
    sksub.add_parser("ls", help="list skills with origin")
    g = sksub.add_parser("get", help="download a skill directory")
    g.add_argument("name")
    g.add_argument("dest")
    a = sksub.add_parser("add", help="tar + upload a skill directory")
    a.add_argument("dir")
    r = sksub.add_parser("rm", help="delete a skill")
    r.add_argument("name")
    s.set_defaults(func=cmd_skills)

    s = sub.add_parser("creds", help="inspect or hot-swap credentials on a running job")
    s.add_argument("run_id")
    crsub = s.add_subparsers(dest="action", required=True)
    crsub.add_parser("ls", help="list credential names (no values)")
    g = crsub.add_parser("get", help="print a credential file's contents")
    g.add_argument("name")
    a = crsub.add_parser("add", help="upload/replace a *.env credential file")
    a.add_argument("file")
    r = crsub.add_parser("rm", help="delete a credential")
    r.add_argument("name")
    s.set_defaults(func=cmd_creds)

    s = sub.add_parser("prompts", help="inspect or hot-swap phase prompts on a running job")
    s.add_argument("run_id")
    prsub = s.add_subparsers(dest="action", required=True)
    prsub.add_parser("ls", help="list every phase with its effective source")
    st = prsub.add_parser("set", help="override a phase prompt for the next iteration")
    st.add_argument("phase")
    st.add_argument("file")
    s.set_defaults(func=cmd_prompts)

    s = sub.add_parser("llm", help="inspect LLM profiles (~/.ralphd/llm-profiles)")
    llmsub = s.add_subparsers(dest="llm_action", required=True)
    llmsub.add_parser("profiles", help="list built-in + file profiles")
    sh_ = llmsub.add_parser("show", help="resolved profile, secret values masked")
    sh_.add_argument("name")
    te_ = llmsub.add_parser("test", help="validate a profile resolves; optional "
                            "1-token ping in a throwaway container")
    te_.add_argument("name")
    te_.add_argument("--image", default=DEFAULT_IMAGE)
    te_.add_argument("--model", help="override the model used for the ping")
    te_.add_argument("--no-ping", action="store_true",
                      help="only validate resolution; skip the container ping "
                           "even if docker is available")
    s.set_defaults(func=cmd_llm)

    s = sub.add_parser("steer", help="send steering guidance (or --list what "
                       "has been steered)")
    s.add_argument("run_id")
    s.add_argument("message", nargs="?")
    s.add_argument("--file")
    s.add_argument("--name")
    s.add_argument("--now", action="store_true",
                   help="also interrupt the current iteration")
    s.add_argument("--list", action="store_true",
                   help="list this run's steering messages (pending and "
                        "applied) instead of sending one; works after the "
                        "container is gone")
    s.set_defaults(func=cmd_steer)

    s = sub.add_parser("retry", help="wake a degraded run's infra backoff "
                       "wait immediately (resets the outage-budget clock)")
    s.add_argument("run_id")
    s.set_defaults(func=cmd_retry)

    s = sub.add_parser("budget", help="change a running job's iteration "
                       "budget in flight (+N tops up, N sets absolutely)")
    s.add_argument("run_id")
    s.add_argument("iterations", metavar="+N|N",
                   help="budget top-up (+10) or absolute new budget (40); "
                        "live-engine change only -- use "
                        "`resume --iterations +N` for a fresh container")
    s.set_defaults(func=cmd_budget)

    s = sub.add_parser("abort", help="terminate a job")
    s.add_argument("run_id")
    s.add_argument("--reason")
    s.set_defaults(func=cmd_abort)

    s = sub.add_parser("stop", help="shut down a finished container")
    s.add_argument("run_id")
    s.add_argument("--force", action="store_true")
    s.set_defaults(func=cmd_stop)

    s = sub.add_parser("rm", help="delete a run's state")
    s.add_argument("run_id")
    s.add_argument("--yes", action="store_true")
    s.add_argument("--force", action="store_true",
                   help="stop a finished run's leftover container first "
                        "instead of refusing; still refuses a job whose "
                        "recorded state is not terminal (use `abort`)")
    s.set_defaults(func=cmd_rm)

    s = sub.add_parser("repair", help="diagnose (and, guarded, fix) "
                       "inconsistent run-dir state")
    s.add_argument("run_id")
    s.add_argument("--set-state", metavar="STATE", help="guarded escape "
                   "hatch: overwrite status.json's 'state' for a run "
                   "whose container died without writing a terminal "
                   f"state (one of: {', '.join(_STATUS_STATES)})")
    s.add_argument("--env", action="append", metavar="KEY=VAL",
                   help="add/update a recorded value in the persisted env "
                        "wiring (repeatable); never echoed, audit event "
                        "records the KEY only")
    s.set_defaults(func=cmd_repair)

    s = sub.add_parser("artifacts", help="what the job left in artifacts/: "
                       "list it, print one file (the reflect report, its "
                       "suggested diff), or copy the tree out")
    s.add_argument("run_id")
    s.add_argument("action", choices=["ls", "show", "pull"], nargs="?",
                   default="ls")
    s.add_argument("name", nargs="?", metavar="ARTIFACT|DEST",
                   help=f"with `show`: {' / '.join(artifact_names())}, or a "
                        "path under artifacts/; with `pull`: the destination "
                        f"directory (default: {_DEFAULT_ARTIFACTS_DEST})")
    s.set_defaults(func=cmd_artifacts)

    s = sub.add_parser("doctor", help="preflight checks")
    s.add_argument("--image", default=DEFAULT_IMAGE)
    s.add_argument("--fix", action="store_true",
                   help="self-recovery sweep: resume every run recorded "
                        "non-terminal whose container has vanished and that "
                        "is opted in to auto_resume (`start --auto-resume` / "
                        "`config set auto_resume true`). Opted-out runs are "
                        "reported, never touched. Idempotent -- intended to "
                        "run from cron/systemd")
    s.set_defaults(func=cmd_doctor)

    s = sub.add_parser("config", help="registry-wide defaults "
                       "(image, on_complete, default_llm_profile, network, "
                       "auto_resume)")
    consub = s.add_subparsers(dest="action", required=True)
    g = consub.add_parser("get", help="print a registry default (or (unset))")
    g.add_argument("key")
    st = consub.add_parser("set", help="persist a registry default to "
                           "<registry>/config.yaml")
    st.add_argument("key")
    st.add_argument("value")
    s.set_defaults(func=cmd_config)

    s = sub.add_parser("ui", help="local web hub (run list, run detail, steering)")
    s.add_argument("--port", type=int, help="defaults to a free ephemeral port")
    s.add_argument("--bind", default="127.0.0.1")
    s.set_defaults(func=cmd_ui)

    args = p.parse_args(_preprocess_logs_argv(sys.argv[1:]))
    args.func(args)


if __name__ == "__main__":
    main()
