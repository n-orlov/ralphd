"""ralphctl — operate ralphd job containers. See docs/cli.md.

Deliberately stdlib-only (argparse + urllib) so `pipx install ralphd` is light.
"""

from __future__ import annotations

import argparse
import atexit
import contextlib
import functools
import hashlib
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
from ..engine.config import (
    MODEL_HATS,
    MUTATING_METHODS,
    PRICE_STRATEGIES,
    JobConfig,
)
from ..engine import findings
from ..engine import petitions
from ..engine.tiers import TIERS
from ..engine.transitions import TASK_STATUSES
from ..engine.state import (
    CURRENT_SCHEMA_VERSION,
    IMAGE_RECORD_KEYS,
    NO_ARTIFACTS,
    NO_ROUTER_CALLS,
    NONTERMINAL_STATES,
    RUN_DOCUMENT_ABSENT,
    AMENDMENT_MODE_STATUS,
    TASK_BLOCKED_FIELD,
    TASK_FAILURE_KINDS,
    TASK_STATUS_LABELS,
    TERMINAL_STATES,
    TERMINATION_CLASS_SELF,
    amendment_entries,
    archived_approach_dir,
    archived_approach_numbers,
    archived_approaches_dir,
    artifact,
    artifact_entries,
    artifact_names,
    artifact_relpath,
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
    format_image,
    format_image_id,
    format_last_tool_call,
    format_local_time,
    format_provisioning_lines,
    format_router_calls_listing,
    format_run_document_listing,
    format_task_counts,
    format_task_status,
    image_record,
    image_record_from,
    is_operator_termination,
    iteration_detail,
    iteration_detail_text,
    parse_utc,
    read_iteration_prompt,
    read_operator_termination,
    read_tasks_doc,
    record_operator_termination,
    router_call_entries,
    router_call_summary_lines,
    run_document,
    run_document_keys,
    run_document_text,
    run_documents,
    tasks_read_notice,
    utc_from_epoch,
    utcnow,
    write_amendment,
)
from ..log_merge import NO_TRANSCRIPT, iteration_numbers
from ..log_merge import iteration_lines as _iteration_lines
from ..log_merge import merged_lines as _merged_lines
from . import image, llm_profiles, patch, ui_server
from .log_render import FULL_BACKLOG_TAIL as _FULL_BACKLOG_TAIL
from .log_render import _ansi
from .log_render import new_render_state as _new_render_state
from .log_render import render_log_line as _render_log_line
from .log_render import render_to_lines as _render_to_lines

DOCKER = os.environ.get("RALPHD_DOCKER", "docker")

# `RALPHD_IMAGE` is a *selection*, like `--image`: set it and ralphctl pins
# exactly that reference, hashing nothing and building nothing (task 033,
# requirement H1). Unset (`IMAGE_ENV is None`) means nobody chose an image, and
# `start` then resolves the content-hashed default `ralphd:<hash>` and builds it
# on a cache miss. DEFAULT_IMAGE stays the hand-built legacy tag: the default
# for `resume`/`doctor`/`llm test`'s own `--image` flags, and the fallback when
# the image inputs are not on disk to hash (a wheel/pipx install -- task 038
# owns that decision).
IMAGE_ENV = os.environ.get("RALPHD_IMAGE") or None
DEFAULT_IMAGE = IMAGE_ENV or "ralphd:dev"

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


# Task 020 (E.4, #150/#152): the host directory `start`/`resume` bind at
# `petitions.DEFAULT_RUN_SECRETS_DIR` (`/var/lib/ralphd/run-secrets`) inside
# the job container, so a per-run MAC key an earlier engine EPISODE minted
# (see `petitions.key_store_dir`) survives a `resume`'s fresh container
# instead of vanishing with the exited one. Lives under the run's own
# CONFIG dir (`~/.ralphd/configs/<run>/run-secrets/`), not the run dir --
# the run dir is agent-owned end to end (see `petitions.key_store_dir`'s
# docstring), and this directory only has to survive a `resume`, not be
# read by anything at the agent uid. Created/mode-fixed on both `start` and
# `resume` (never assumed already 0700) so a run dir predating this task
# self-heals the first time it resumes.
def run_secrets_dir(run_id: str) -> Path:
    return config_root(run_id) / "run-secrets"


def _bind_run_secrets_mount(run_id: str, mounts: list) -> None:
    d = run_secrets_dir(run_id)
    d.mkdir(parents=True, exist_ok=True)
    os.chmod(d, 0o700)
    mounts += ["-v", f"{d}:{petitions.DEFAULT_RUN_SECRETS_DIR}"]


# The container path every `docker run` in this module binds the run dir at
# (`-v {rdir}:/run/ralphd`), which is therefore the engine's own
# `run.root` -- and so the identity `petitions.stored_key_filename` names
# this run's key file by.
CONTAINER_RUN_DIR = "/run/ralphd"

# What `status` reports in place of a key when there is none: the check an
# operator can still run, spelled out rather than left to inference.
RUN_SECRETS_FALLBACK_CHECK = (
    "the events.jsonl petition_adjudications_recorded MAC witness")


def run_secrets_key_files(run_id: str) -> list[Path]:
    """The non-empty MAC-key files that actually exist in this run's
    run-secrets directory: the one `petitions.stored_key_filename` names
    for the engine's own run root first, then any other `*.key` an
    operator's `$RALPHD_RUN_SECRETS_DIR` override or an older engine build
    left behind.

    An EMPTY list is the no-key condition `status` must name (task 020's
    rejection): the directory existing says only that `start`/`resume`
    bound it, never that a key survived into it -- with no uid boundary
    `petitions.key_store_dir()` returns None and the mount stays empty for
    the whole run, which is exactly when the `events.jsonl` fallback check
    is the operator's only handle.
    """
    d = run_secrets_dir(run_id)
    candidates = [d / petitions.stored_key_filename(CONTAINER_RUN_DIR)]
    try:
        candidates += sorted(d.glob("*.key"))
    except OSError:
        pass
    found: list[Path] = []
    for p in candidates:
        if p in found:
            continue
        try:
            if p.is_file() and p.stat().st_size > 0:
                found.append(p)
        except OSError:
            continue
    return found


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
    # Task 004 (#62 candidate 1, requirement A): the mutating-only gate.
    # `.control-token` is provisioned unconditionally by `start`/`resume`
    # (unlike `.api-token`, which only exists when the operator opted into
    # `--api-token`), so this is the one place ralphctl needs to change for
    # every mutating command (abort/pause/resume/retry/shutdown/budget/the
    # config CRUD) to keep working over the moved surface -- they all share
    # this one `api()` helper. Read fresh per call, not cached, so a
    # `resume` that rotates the file mid-process is picked up.
    if method in MUTATING_METHODS:
        control_file = run_root(run_id) / ".control-token"
        if control_file.exists():
            req.add_header("X-Ralphd-Control-Token",
                          control_file.read_text().strip())
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
# existed, `doctor --fix`) goes through this constant, which is why flipping
# the default was this one line.
#
# v0.7 (requirement O) flipped it from False to True: the two rules that
# make self-recovery safe have now been validated on real runs -- the
# crash-loop guard (AUTO_RESUME_MAX_ATTEMPTS below, progress-keyed on
# iterationsUsed) and "never resurrect a run the operator killed"
# (`is_operator_termination()`, which v0.7 narrowed so an accidental
# SELF-inflicted signal is still resumable). An unattended run whose host
# rebooted or whose container was OOM-killed is worth more resumed than
# sitting dangling until someone notices; an operator who disagrees opts a
# run out with `--no-auto-resume`, or the whole registry with
# `ralphctl config set auto_resume false`. Nothing else about the sweep
# changed: it still only ever acts on the dangling condition, and only
# under `doctor --fix`.
AUTO_RESUME_DEFAULT = True


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


# ------------------------------------------------------- job image (#20 H1)
# Producing the job image, as opposed to hashing its inputs (cli/image.py) or
# selecting somebody else's tag. `container/Dockerfile` has existed since v0.1
# and nothing ever built it: two runs of this project executed a ten-day-old
# engine and no surface could say so. Tagging by content hash makes "the image
# matches the source" structural -- a source change produces a new tag, and a
# missing tag is a build.

# How the image `start` is about to run was arrived at. Recorded here as
# vocabulary (task 036 puts it in run state, task 037 compares it against the
# current source hash) so no surface invents its own word for it.
IMAGE_SOURCE_PINNED = "pinned"        # --image / RALPHD_IMAGE / template / registry
IMAGE_SOURCE_CACHED = "cached"        # the hashed tag already present -- tag lookup only
IMAGE_SOURCE_BUILT = "built"          # the hashed tag was missing and got built
IMAGE_SOURCE_UNHASHABLE = "unhashable"  # no source tree to hash (wheel/pipx)
# Task 036 (#20 H4): `resume` reproducing the image THIS RUN started on, from
# the record in its own run state -- neither hashed nor built, just reused.
IMAGE_SOURCE_RECORDED = "recorded"
# ... and the last resort: a run whose state records no image and whose job.yaml
# holds no recipe (a pre-v0.6 run dir), resumed on DEFAULT_IMAGE. A word of its
# own because it is the one case where nothing at all is known about the image
# -- neither content hash nor operator intent (task 037 must not read it as a
# deliberate pin).
IMAGE_SOURCE_DEFAULT = "default"

# `cached`/`built` describe *both* hashed repositories, and `imageBase` is what
# tells them apart: None for the default `ralphd:<hash>`, the operator's base
# reference for a derived `ralphd-derived:<hash>` (task 034, requirement H2).
# One vocabulary rather than two more words, because the *act* is the same one
# -- hash the inputs, look the tag up, build only on a miss -- and only the
# ingredients differ.
IMAGE_NO_SOURCE_TO_DERIVE = (
    "cannot derive a job image from base {base}: no ralphd source to layer on "
    f"top of it -- no checkout next to this install (no {image.SOURCE_MARKER}) "
    f"and no packaged image inputs inside it (no "
    f"{image.PACKAGED_DIR_NAME}/{image.SOURCE_MARKER}). Install ralphd from a "
    "checkout or a wheel that ships its image inputs, or pin a finished image "
    "with --image (which is run as-is, not used as a base).")
IMAGE_BASE_AND_PIN_NOTICE = (
    "--image and --base-image/--dockerfile are different things: --image (or "
    "RALPHD_IMAGE, or a template's or the registry's image) pins a finished "
    "job image and runs it as-is, while --base-image and --dockerfile supply a "
    "base to layer the engine and pi onto. Pass one or the other, not both.")
IMAGE_BASE_AND_DOCKERFILE_NOTICE = (
    "--base-image and --dockerfile are two ways to supply the same thing -- "
    "the base the job image is derived from: one names an image that already "
    "exists, the other a recipe to build one. Pass one or the other, not both.")
IMAGE_DOCKERFILE_UNUSABLE = "cannot build a base image from {what}: {why}"

# Task 036 (#20 H4): what `resume` says when the image a run started on cannot
# be reproduced exactly. Every one of these is a warning, never a refusal: a
# resume that will not start is worse than a resume the operator was told about.
IMAGE_RECORDED_MOVED_NOTICE = (
    "run state records this run's image as {ref} (id {short}), but {ref} now "
    "names a different image -- running the recorded id instead, so this "
    "resume continues on the image the run started on.")
IMAGE_RECORDED_ID_GONE_NOTICE = (
    "run state records this run's image as {ref} (id {short}), but {ref} now "
    "names a different image and the recorded id is no longer on this daemon "
    "-- running {ref} as it is today. This resume may run a different engine "
    "than the run started with.")
IMAGE_RECORDED_GONE_NOTICE = (
    "the image this run started on ({ref}) is no longer on this daemon -- "
    "resolving one again from this run's own configuration instead. This "
    "resume may run a different engine than the run started with.")

# The three keys that decide which image `start` runs, resolved as ONE unit
# (`_resolve_image_supply`, task 035 / requirement H3) rather than field by
# field: they are three answers to the same question, so the most specific
# *level* that answers it wins whole, instead of a `--dockerfile` on the
# command line being silently combined with a registry-wide `image:` pin from
# `config.yaml`.
IMAGE_SUPPLY_KEYS = ("image", "base_image", "dockerfile")
IMAGE_SUPPLY_FLAGS = {"image": "--image", "base_image": "--base-image",
                      "dockerfile": "--dockerfile"}
IMAGE_SUPPLY_LEVEL_FLAG = "the command line"
IMAGE_SUPPLY_LEVEL_TEMPLATE = "the template's job.yaml"
IMAGE_SUPPLY_LEVEL_REGISTRY = "the registry's config.yaml"
IMAGE_SUPPLY_CLASH_NOTICE = (
    "{where} supplies both {keys}, which contradict each other: --image / "
    "image: pins a finished job image and runs it as-is, while --base-image / "
    "base_image: and --dockerfile / dockerfile: supply a base to layer the "
    "engine and pi onto. Set exactly one of the three per level.")

# Bounded build output (requirement H4: "visible enough to debug a failing
# build without drowning the start path"). Every line is echoed, prefixed and
# clipped, until ECHO_LINES; past that only the last TAIL_LINES are kept, and
# they are printed if -- and only if -- the build then fails.
IMAGE_BUILD_ECHO_LINES = 200
IMAGE_BUILD_TAIL_LINES = 40
IMAGE_BUILD_LINE_WIDTH = 200
IMAGE_BUILD_PREFIX = "  build: "
IMAGE_BUILD_ELIDED_NOTICE = (
    f"... more than {IMAGE_BUILD_ECHO_LINES} lines of build output; "
    f"showing the last {IMAGE_BUILD_TAIL_LINES} only if the build fails")
IMAGE_NO_SOURCE_NOTICE = (
    "cannot hash the job image inputs: no ralphd checkout next to this install "
    f"(no {image.SOURCE_MARKER}) and no packaged image inputs inside it (no "
    f"{image.PACKAGED_DIR_NAME}/{image.SOURCE_MARKER}), so nothing can be "
    f"built from content -- falling back to {DEFAULT_IMAGE}. Pin one with "
    "--image, or see docs/architecture.md on where the image inputs come from.")
# Task 038 (#20 H4), the packaging interaction, decided: a wheel/pipx install
# ships the image inputs as package data and builds from a staged copy of them.
# Said out loud on every such build, because it is a different provenance for
# the same tag -- the inputs are the ones frozen at install time, not a working
# tree the operator can edit -- and requirement H4 asks for the packaging
# fallback to be observable rather than a silent behaviour change.
IMAGE_PACKAGED_INPUTS_NOTICE = (
    "job image inputs come from this install's own package data ({where}): "
    f"there is no ralphd checkout next to it (no {image.SOURCE_MARKER}), so "
    "container/ and the install recipe are taken from the wheel and the "
    "installed engine is staged into a build context. This produces the same "
    "content-hashed tag a checkout of this version builds.")


def image_exists(ref: str) -> bool:
    """Is this image reference present on the local daemon? (The cache check.)"""
    return sh([DOCKER, "image", "inspect", ref]).returncode == 0


def inspect_image(ref: str) -> tuple[bool, str | None]:
    """`(present, image id)` for a reference on the local daemon (task 036).

    Two questions in one call because they must be answered about the same
    instant: `resume` has to know both whether the image it recorded is still
    here *and* whether that reference still names the same image (a mutable pin
    like `ralphd:dev` moves under a run). `(False, None)` for an absent
    reference; `(True, None)` when the daemon has it but would not say which id
    (never inferred as absence -- unknown is not gone).
    """
    res = sh([DOCKER, "image", "inspect", "--format", "{{.Id}}", ref])
    if res.returncode != 0:
        return False, None
    return True, res.stdout.strip() or None


def container_image_id(container: str) -> str | None:
    """The content id of the image a container is actually running, or None.

    Asked of the container rather than of the reference on purpose: it is the
    only answer that stays true for a reference the daemon resolved itself (a
    pinned tag `docker run` pulled, or a tag that moves later). Never fatal --
    an unanswerable id is recorded as absent, not as an error.
    """
    res = sh([DOCKER, "inspect", "--format", "{{.Image}}", container])
    if res.returncode != 0:
        return None
    return res.stdout.strip() or None


def _image_build_env() -> dict:
    """Environment for the build.

    Defaults to the legacy builder because the case ralphd itself has to work
    in is a build from *inside* a job container, which ships the static docker
    client only -- no buildx plugin, so a BuildKit build there fails with a
    confusing "buildx not found". An operator who has BuildKit and wants it
    sets `DOCKER_BUILDKIT=1` themselves; we never override that choice.
    """
    env = dict(os.environ)
    env.setdefault("DOCKER_BUILDKIT", "0")
    return env


def _build_image(tag: str, root: Path, dockerfile: Path,
                 label: str = "job image") -> tuple[int, list[str], bool]:
    """Build `tag` from `root` with `dockerfile`; return (code, tail, elided).

    `label` names *what* is being built, for the operator watching stderr: the
    job image itself, or (task 035) the base image an operator's own
    `--dockerfile` produces before the engine is layered onto it.

    Output is streamed as it arrives (a 20-minute image build must not look
    hung) but bounded: clipped per line, capped at IMAGE_BUILD_ECHO_LINES, and
    the last IMAGE_BUILD_TAIL_LINES retained for the caller to print on
    failure. On stderr, because stdout of `start` is the run's own report.
    """
    cmd = [DOCKER, "build", "-t", tag, "-f", str(dockerfile), str(root)]
    print(f"ralphctl: building {label} {tag} from {root}", file=sys.stderr)
    tail: list[str] = []
    echoed = 0
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True,
                                bufsize=1, env=_image_build_env())
    except OSError as e:
        return 127, [str(e)], False
    assert proc.stdout is not None
    for raw in proc.stdout:
        line = raw.rstrip("\n")[:IMAGE_BUILD_LINE_WIDTH]
        tail.append(line)
        del tail[:-IMAGE_BUILD_TAIL_LINES]
        if echoed < IMAGE_BUILD_ECHO_LINES:
            print(f"{IMAGE_BUILD_PREFIX}{line}", file=sys.stderr)
        elif echoed == IMAGE_BUILD_ECHO_LINES:
            print(f"{IMAGE_BUILD_PREFIX}{IMAGE_BUILD_ELIDED_NOTICE}",
                  file=sys.stderr)
        echoed += 1
    return proc.wait(), tail, echoed > IMAGE_BUILD_ECHO_LINES


def _report_build_failure(code: int, tail: list[str], elided: bool,
                          what: str) -> None:
    """Die (exit 1) on a failed build, showing the tail only if it was elided.

    Re-printing the tail when the live echo was complete would repeat lines
    the operator already has above: noise, not context.
    """
    if elided:
        print(f"{IMAGE_BUILD_PREFIX}last {len(tail)} lines:", file=sys.stderr)
        for line in tail:
            print(f"{IMAGE_BUILD_PREFIX}{line}", file=sys.stderr)
    die(1, f"job image build failed (exit {code}): {what}. No run state was "
           f"created -- fix the build, or pin an existing image with --image.")


def _dockerfile_base_image(spec: str) -> image.DockerfileBase:
    """Build (or reuse) the operator's own Dockerfile as a *base* image.

    Requirement H3's third supply point: `--dockerfile <path>` (or a
    `dockerfile:` key in a template's job.yaml / the registry config) is built
    with the Dockerfile's own directory as the build context and tagged
    `ralphd-base:<hash>` over that context -- and the result is a **base**, not
    the job image: the engine and pi are layered onto it by `_derived_job_image`
    exactly as for `--base-image`. Two builds, two cache keys, one rule: hash
    the inputs, look the tag up, build only on a miss.
    """
    try:
        df = image.dockerfile_base(spec)
    except image.ImageInputError as e:
        die(2, IMAGE_DOCKERFILE_UNUSABLE.format(what=f"--dockerfile {spec}",
                                                why=e))
    if image_exists(df.tag):
        return df
    code, tail, elided = _build_image(df.tag, df.context, df.dockerfile,
                                     label="base image")
    if code != 0:
        _report_build_failure(code, tail, elided,
                              f"{df.tag} from {df.dockerfile} with "
                              f"{df.context} as the context")
    print(f"ralphctl: built base image {df.tag}", file=sys.stderr)
    return df


def _derived_job_image(base: str, src: Path,
                       dockerfile: Path | None = None) -> dict:
    """Resolve (and on a cache miss build) the image derived from `base`.

    Requirement H2: a user-supplied base image is **not** the job image. The
    engine and pi are layered on top of it by a generated Dockerfile
    (`image.derive`), the result is tagged `ralphd-derived:<hash>` where the
    hash covers the base reference *and* the source *and* the recipe, and it is
    cached and invalidated by exactly the same rules as the default image: one
    `image inspect`, a build only on a miss. The base itself is never run -- it
    has no `ralphd-engine` in it.

    `dockerfile` is the operator's own recipe when the base came from one (task
    035), recorded on the result so a later surface can say where the base came
    from rather than only that it was `ralphd-base:<hash>`.
    """
    try:
        derived = image.derive(src, base)
    except image.ImageInputError as e:
        # An unusable base, or a `container/Dockerfile` that no longer declares
        # what the recipe copies out of it: refuse, never build something else.
        die(2, f"cannot derive a job image from base {base}: {e}")
    if not derived.tree.complete:
        print(f"ralphctl: warning: image inputs missing from {src}: "
              f"{', '.join(derived.tree.missing)}", file=sys.stderr)
    fields = {"imageHash": derived.hash, "imageBase": derived.base,
              "imageDockerfile": str(dockerfile) if dockerfile else None}
    if image_exists(derived.tag):
        return {"image": derived.tag, "imageSource": IMAGE_SOURCE_CACHED,
                **fields}
    print(f"ralphctl: deriving job image {derived.tag} from base "
          f"{derived.base}", file=sys.stderr)
    # The generated Dockerfile lives outside the build context on purpose: it
    # is a build input, not a source file, and writing it into the checkout
    # would leave litter behind a failed build (and race two concurrent
    # `start`s over the same path).
    with tempfile.TemporaryDirectory(prefix="ralphd-derived-") as td:
        dockerfile = Path(td) / "Dockerfile"
        dockerfile.write_text(derived.dockerfile)
        code, tail, elided = _build_image(derived.tag, derived.root, dockerfile)
    if code != 0:
        _report_build_failure(code, tail, elided,
                              f"{derived.tag} derived from base "
                              f"{derived.base} with {src} as the context")
    print(f"ralphctl: built job image {derived.tag}", file=sys.stderr)
    return {"image": derived.tag, "imageSource": IMAGE_SOURCE_BUILT, **fields}


@contextlib.contextmanager
def _image_inputs_tree(supply: image.InputsSupply):
    """Yield a build context for `supply` -- staging package data if that is
    where this install's image inputs live (task 038, requirement H4).

    A checkout yields its own tree and nothing is copied. A wheel/pipx install
    yields a temporary context assembled by `image.stage_inputs`, which lives
    exactly as long as the build that needs it: the context is derived data, so
    leaving it behind (in the install directory, or anywhere else) would be
    litter that the next build has to distrust.

    Never silent: the staged case announces itself on stderr, since the same tag
    is then built from inputs frozen at install time rather than from a tree the
    operator can edit.
    """
    if supply.kind == image.INPUTS_CHECKOUT:
        yield supply.root
        return
    print("ralphctl: " + IMAGE_PACKAGED_INPUTS_NOTICE.format(
        where=supply.packaged), file=sys.stderr)
    with tempfile.TemporaryDirectory(prefix="ralphd-image-inputs-") as td:
        yield image.stage_inputs(supply, Path(td))


def resolve_job_image(selected: str | None, *, base: str | None = None,
                      dockerfile: str | None = None,
                      root: Path | None = None) -> dict:
    """Resolve the image `start` will run, building it if it does not exist.

    The one place image *production* happens. Returns
    `{image, imageSource, imageHash, imageBase, imageDockerfile}` --
    `imageHash` is None whenever the reference was not derived from content, so
    a later surface can tell "pinned, staleness unknowable" from "built from
    this exact source"; `imageBase` is non-None exactly when the reference is a
    *derived* image, whose hash is a function of that base as well as of the
    source; `imageDockerfile` is non-None exactly when that base was itself
    built from an operator's own recipe.

    `selected` not None (an `--image` flag, `RALPHD_IMAGE`, a template's or the
    registry's `image`) pins it: **no hash is computed and no build runs**, so
    deliberately running an old engine stays possible and cheap.

    `base` not None (requirement H2) makes that reference a *base layer*: the
    engine and pi are layered on top of it and the derived image is what runs.
    `dockerfile` not None (requirement H3) builds that recipe into a base first
    -- with its own directory as the build context -- and then derives from it,
    so the two are alternative spellings of the same ingredient and passing
    both is a usage error.

    The build context is this install's image inputs: its checkout when there is
    one, otherwise (task 038) a staged copy of the inputs the wheel ships, which
    hashes to the same tag. Only an install with neither falls back to
    DEFAULT_IMAGE, and it says so.

    Dies (exit 1) on a failed build, exit 2 on contradictory input. Callers
    must call this *before* creating any run state -- requirement H4: no
    half-registered run.
    """
    if selected and (base or dockerfile):
        die(2, IMAGE_BASE_AND_PIN_NOTICE)
    if base and dockerfile:
        die(2, IMAGE_BASE_AND_DOCKERFILE_NOTICE)
    if selected:
        return {"image": selected, "imageSource": IMAGE_SOURCE_PINNED,
                "imageHash": None, "imageBase": None, "imageDockerfile": None}
    supply = image.find_inputs(root)
    if not supply.buildable:
        if base or dockerfile:
            # Deriving needs the engine source to install: there is no
            # fallback that could honour the operator's base, so this is an
            # error rather than the observable default below.
            die(1, IMAGE_NO_SOURCE_TO_DERIVE.format(
                base=base or f"the image built from {dockerfile}"))
        print(f"ralphctl: warning: {IMAGE_NO_SOURCE_NOTICE}", file=sys.stderr)
        return {"image": DEFAULT_IMAGE, "imageSource": IMAGE_SOURCE_UNHASHABLE,
                "imageHash": None, "imageBase": None, "imageDockerfile": None}
    with _image_inputs_tree(supply) as src:
        recipe = None
        if dockerfile:
            # The operator's own recipe becomes a hash-tagged base image; the
            # job image is then derived from that tag by the H2 path below.
            recipe = _dockerfile_base_image(dockerfile)
            base = recipe.tag
        if base:
            return _derived_job_image(
                base, src, dockerfile=recipe.dockerfile if recipe else None)
        tree = image.hash_image_inputs(src)
        tag = image.image_tag(tree.hash)
        if not tree.complete:
            # A hash over a partial input set is still a real hash of what is
            # there, but the build is likely to fail on the missing piece -- say
            # so now rather than letting `pip install /opt/ralphd` explain it.
            print(f"ralphctl: warning: image inputs missing from {src}: "
                  f"{', '.join(tree.missing)}", file=sys.stderr)
        if image_exists(tag):
            return {"image": tag, "imageSource": IMAGE_SOURCE_CACHED,
                    "imageHash": tree.hash, "imageBase": None,
                    "imageDockerfile": None}
        code, tail, elided = _build_image(tag, src, src / image.SOURCE_MARKER)
        if code != 0:
            _report_build_failure(code, tail, elided,
                                  f"{tag} from {src} ({image.SOURCE_MARKER})")
        print(f"ralphctl: built job image {tag}", file=sys.stderr)
        return {"image": tag, "imageSource": IMAGE_SOURCE_BUILT,
                "imageHash": tree.hash, "imageBase": None,
                "imageDockerfile": None}


def _image_record_fields(resolved: dict, container: str) -> dict:
    """The job-image record `start`/`resume` write into host.json beside
    `image` (task 036, requirement H4).

    Two halves: the *provenance* `resolve_job_image`/`_resume_image` reported
    (how the reference was arrived at, the content hash it was tagged by, the
    base and the operator Dockerfile behind it), and the *identity* of the image
    the container actually got -- observed from the container itself, so a
    pinned reference the daemon pulled, or one that moves tomorrow, is still
    recorded as the image this run runs.

    Keys nothing is known about are omitted rather than written as nulls, which
    is host.json's existing convention (`network`/`workspace`); readers get the
    explicit-null contract from `engine.state.image_record` instead. `image`
    itself is left to the caller, which already writes it.
    """
    fields = {k: v for k, v in resolved.items()
              if k in IMAGE_RECORD_KEYS and k != "image" and v is not None}
    observed = container_image_id(container)
    if observed:
        fields["imageId"] = observed
    return fields


# ------------------------------------------- job-image staleness (#20 H4)
# Requirement H4: `doctor` reports image **staleness** -- the source hash
# against the tag in use -- not merely that some image exists. "Exists" was
# the pre-v0.6 check, and it is exactly the check that let two runs of this
# project execute a ten-day-old engine with nothing to say so.
#
# The whole difficulty is that a tag is only comparable to a source hash when
# it was *produced* by hashing that source. There are three hash namespaces
# (`ralphd:<hash>`, `ralphd-derived:<hash>`, `ralphd-base:<hash>`, task 032-035)
# and only the first is a function of ralphd's source alone; a pinned or
# hand-built reference is a function of nothing this host can recompute. So
# there are FOUR answers, not two, and `unknowable` is a first-class one:
# reporting a pin as "up to date" would be the same lie in the other direction.
IMAGE_STALENESS_FRESH = "fresh"          # built from exactly these inputs
IMAGE_STALENESS_STALE = "stale"          # hashed from some other source tree
IMAGE_STALENESS_MISSING = "missing"      # not on this daemon at all
IMAGE_STALENESS_UNKNOWABLE = "unknowable"  # not comparable to a source hash
IMAGE_STALENESS_VERDICTS = (IMAGE_STALENESS_FRESH, IMAGE_STALENESS_STALE,
                            IMAGE_STALENESS_MISSING,
                            IMAGE_STALENESS_UNKNOWABLE)

# Which hash namespace a reference lives in -- read with cli/image.py's own
# readers (`tag_hash`/`derived_tag_hash`/`base_tag_hash`), never by matching
# the string here, so "what is a ralphd tag" stays defined in one module.
IMAGE_TAG_DEFAULT = "default"    # ralphd:<hash>         -- comparable
IMAGE_TAG_DERIVED = "derived"    # ralphd-derived:<hash> -- covers a base too
IMAGE_TAG_BASE = "base"          # ralphd-base:<hash>    -- covers a context
IMAGE_TAG_UNHASHED = "unhashed"  # a pin, a registry ref, a hand-built tag
IMAGE_TAG_NONE = "none"          # nothing recorded at all

IMAGE_FRESH_NOTE = (
    "{ref} was built from exactly this source tree's image inputs (source "
    "hash {source})")
IMAGE_STALE_NOTE = (
    "{ref} was built from different image inputs than this source tree, whose "
    "job image is {expected} -- a run on {ref} executes an engine that is not "
    "this source")
IMAGE_MISSING_BUILDABLE_NOTE = (
    "{ref} is this source tree's job image but is not on this daemon -- the "
    "next `ralphctl start` builds it")
IMAGE_MISSING_NOTE = (
    "{ref} is not on this daemon and is not this source tree's job image "
    "({expected}), so nothing here builds it: a run on {ref} cannot start")
IMAGE_NO_SOURCE_HASH_NOTE = (
    "{ref} cannot be compared against a source hash: this install has neither "
    "a ralphd checkout next to it (no {marker}) nor packaged image inputs "
    "inside it to hash")
IMAGE_DERIVED_NOTE = (
    "{ref} is a derived image, whose hash covers the base image it was layered "
    "onto as well as ralphd's source, so it is not comparable to a bare source "
    "hash -- this source tree's own default job image is {expected}")
IMAGE_BASE_TAG_NOTE = (
    "{ref} is a base image built from an operator's own Dockerfile, whose hash "
    "covers that build context rather than ralphd's source, so it is not "
    "comparable to a source hash -- this source tree's own default job image "
    "is {expected}")
IMAGE_PIN_NOTE = (
    "{ref} was not produced by hashing ralphd's source, so whether it matches "
    "this source tree cannot be established -- this source tree's own job "
    "image is {expected}")
IMAGE_NO_REFERENCE_NOTE = (
    "no image reference is recorded for this run, so which engine it runs "
    "cannot be established (a run dir written before v0.6 records none)")
# Appended to an `unknowable` verdict for a run: the record's own word for how
# the reference was chosen (IMAGE_SOURCE_*), which is the only thing that can
# explain the ignorance -- `pinned` and `default` mean staleness is unknowable
# by construction, not that the image is current.
IMAGE_RECORDED_SOURCE_CLAUSE = " (run state records it as `{source}`)"
IMAGE_NO_SOURCE_EXPECTED = "unknown (nothing here to hash)"


def image_tag_kind(ref: str | None) -> tuple[str, str | None]:
    """`(namespace, content hash)` for an image reference (task 037).

    The hash is read by cli/image.py's own three readers rather than by
    pattern-matching a tag here: they are what define the namespaces, and a
    second implementation is how `ralphd-derived:<hash>` would eventually get
    compared against a source hash it has nothing to do with.
    """
    ref = (ref or "").strip()
    if not ref:
        return IMAGE_TAG_NONE, None
    for kind, reader in ((IMAGE_TAG_DEFAULT, image.tag_hash),
                         (IMAGE_TAG_DERIVED, image.derived_tag_hash),
                         (IMAGE_TAG_BASE, image.base_tag_hash)):
        found = reader(ref)
        if found:
            return kind, found
    return IMAGE_TAG_UNHASHED, None


def current_source_hash(root: Path | None = None) -> tuple[Path | None, str | None]:
    """`(where the image inputs are, content hash of them)`, or `(None, None)`.

    `(None, None)` means this install has neither a checkout nor packaged image
    inputs, and it means staleness is unknowable, never "fresh": there is
    nothing to compare a tag against. For a wheel/pipx install (task 038) the
    inputs *are* there -- shipped as package data -- so they are staged into a
    throwaway context and hashed, which yields the same hash the checkout of
    that version yields; the directory reported is the package data itself,
    since the staged copy is gone by the time this returns.

    One hash per `doctor` invocation (cheap by construction, task 032: excluded
    directories are pruned, not walked).
    """
    supply = image.find_inputs(root)
    if supply.kind == image.INPUTS_CHECKOUT and supply.root is not None:
        return supply.root, image.hash_image_inputs(supply.root).hash
    if supply.kind == image.INPUTS_PACKAGED:
        with tempfile.TemporaryDirectory(prefix="ralphd-image-inputs-") as td:
            staged = image.stage_inputs(supply, Path(td))
            return supply.packaged, image.hash_image_inputs(staged).hash
    return None, None


def _comparable_image_hash(kind: str, tag_hash: str | None,
                           rec: dict | None) -> str | None:
    """The hash of an image that a ralphd *source* hash may be compared to.

    None means "not comparable" -- which is an answer, not a failure. A
    derived or base tag's hash covers ingredients that are not this source, so
    it is refused here even though it is a real content hash.

    A run's own record (task 036) answers when the reference cannot: a resume
    that had to fall back to the recorded image *id* runs a reference with no
    tag to read, while `host.json` still says which source hash produced it.
    Only for `cached`/`built`/`recorded` with no base -- `pinned`, `unhashable`
    and `default` mean nobody hashed anything.
    """
    rec = rec or {}
    if rec.get("imageBase") or rec.get("imageDockerfile"):
        return None
    if kind == IMAGE_TAG_DEFAULT:
        return tag_hash
    recorded = rec.get("imageHash")
    if (isinstance(recorded, str) and recorded.strip()
            and rec.get("imageSource") in (IMAGE_SOURCE_CACHED,
                                           IMAGE_SOURCE_BUILT,
                                           IMAGE_SOURCE_RECORDED)):
        return recorded.strip()
    return None


def image_staleness(ref: str | None, *, source_hash: str | None,
                    present: bool | None = None,
                    rec: dict | None = None) -> dict:
    """Is `ref` the image this source tree builds? (requirement H4's question.)

    Pure: every docker fact it uses is passed in (`present`, `None` when nobody
    asked the daemon -- which is never read as absence), so the verdict is
    testable without a daemon and so one implementation can answer both about
    the host's own job image and about the image a run recorded (task 036).

    Returns `{image, imageKind, imageHash, imageSource, sourceHash,
    sourceImage, present, staleness, note}` -- `staleness` one of
    IMAGE_STALENESS_VERDICTS, `note` the operator-facing sentence (worded once,
    here, so the text report and `--json` cannot disagree).
    """
    ref = (ref or "").strip()
    rec = rec or {}
    kind, tag_hash = image_tag_kind(ref)
    comparable = _comparable_image_hash(kind, tag_hash, rec)
    expected = image.image_tag(source_hash) if source_hash else None
    matches = bool(comparable and source_hash and comparable == source_hash)
    fmt = {"ref": ref, "source": source_hash,
           "expected": expected or IMAGE_NO_SOURCE_EXPECTED,
           "marker": image.SOURCE_MARKER}
    if not ref:
        verdict, note = IMAGE_STALENESS_UNKNOWABLE, IMAGE_NO_REFERENCE_NOTE
    elif present is False:
        verdict = IMAGE_STALENESS_MISSING
        note = (IMAGE_MISSING_BUILDABLE_NOTE if matches
                else IMAGE_MISSING_NOTE).format(**fmt)
    elif not source_hash:
        verdict = IMAGE_STALENESS_UNKNOWABLE
        note = IMAGE_NO_SOURCE_HASH_NOTE.format(**fmt)
    elif comparable:
        verdict = IMAGE_STALENESS_FRESH if matches else IMAGE_STALENESS_STALE
        note = (IMAGE_FRESH_NOTE if matches else IMAGE_STALE_NOTE).format(**fmt)
    else:
        verdict = IMAGE_STALENESS_UNKNOWABLE
        note = {IMAGE_TAG_DERIVED: IMAGE_DERIVED_NOTE,
                IMAGE_TAG_BASE: IMAGE_BASE_TAG_NOTE}.get(
                    kind, IMAGE_PIN_NOTE).format(**fmt)
    recorded_source = rec.get("imageSource")
    if verdict == IMAGE_STALENESS_UNKNOWABLE and recorded_source and ref:
        note += IMAGE_RECORDED_SOURCE_CLAUSE.format(source=recorded_source)
    return {"image": ref or None, "imageKind": kind, "imageHash": comparable,
            "imageSource": recorded_source, "sourceHash": source_hash,
            "sourceImage": expected, "present": present,
            "staleness": verdict, "note": note}


# ---------------------------------------------------------------- start
# job-config fields a template's job.yaml may default; explicit CLI flags
# (checked via `is not None`/falsy, since every one of these argparse
# options defaults to None/empty when omitted -- see the `start` subparser)
# always win over the template.
_TEMPLATE_SCALAR_FIELDS = {
    "iterations": 25, "max_approaches": 3, "vigilant": False,
    "reflect": False, "on_complete": "exit", "on_complete_cmd": None, "timeout": 480,
    "iteration_timeout": 45, "llm": "host",
    # Task 006 (#60 pillar 4 cleanup): `model_strategy`/`fast_model` are
    # RETIRED (see `cmd_config`'s rejection for the config-key half, and
    # `engine/config._migrate_legacy_model_config` for the job.yaml half) --
    # neither name is a `start` flag/template scalar any more.
    "model": None, "thinking": None,
    # Task 010 (#14): None here means "nobody on the host decided" -- the
    # profile may then supply one, and if nothing does the key is omitted
    # from job.yaml so the engine's own default (`none`) applies. The default
    # lives in engine/config.DEFAULT_PRICE_STRATEGY alone.
    "price_strategy": None,
    "network": None,
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
#
# The three image supply keys (`image`, `base_image`, `dockerfile`) are NOT
# here: they contradict each other, so they are settled as one unit by
# `_resolve_image_supply` below -- under the same key names, at the same three
# levels -- rather than filled in field by field.
#
# Task 043 (#52): `model` and `thinking` are here too. A named LLM profile can
# already carry `model`, but a profile answers *where do I route and with what
# credentials*, which is a routing fact rather than a registry-wide default,
# so a registry-wide value still beats the profile's. Task 006 retired
# `fast_model`/`model_strategy` at this level too (see `cmd_config`'s
# rejection below) -- neither is a registry key any more.
_REGISTRY_CONFIG_FIELD_KEYS = {"on_complete": "on_complete",
                               "llm": "default_llm_profile", "network": "network",
                               "auto_resume": "auto_resume",
                               "price_strategy": "price_strategy",
                               "model": "model",
                               "thinking": "thinking"}
# Task 008 (#60 pillar 4, requirement A): the per-hat `models`/
# `model_overrides` table is the table-aware equivalent of the two maps
# above -- it cannot be one more entry in either because a table MERGES
# across levels (registry < template < explicit `--model-for` flags) where
# every field above REPLACES. See `_resolve_model_table`.


def _table_from(d: dict) -> dict:
    """Merge a dict's own `models`/`model_overrides` keys the same way
    `JobConfig.__post_init__` does: `model_overrides` wins on any key both
    name. `d` is a raw parsed YAML mapping (a template's `job.yaml`, or
    `<registry>/config.yaml`), never a JobConfig."""
    merged = dict(d.get("models") or {})
    merged.update(d.get("model_overrides") or {})
    return merged


def _tier_table_from(d: dict) -> dict:
    """Task 004: a dict's own `model_tiers` key (JobConfig's single field of
    that name -- unlike `models`/`model_overrides` there is no second name
    to merge here), shallow-copied at both the hat and tier level so the
    caller's own merge (`_merge_tier_tables`) never mutates the source
    dict it was read from."""
    raw = d.get("model_tiers") or {}
    if not isinstance(raw, dict):
        return {}
    return {hat: dict(tiers) for hat, tiers in raw.items() if isinstance(tiers, dict)}


def _merge_tier_tables(base: dict, overlay: dict) -> dict:
    """Task 004: merge two hat -> tier -> leaf tables key by key at BOTH
    levels -- `overlay` wins per (hat, tier) rung, but a rung `overlay`
    never mentions (for a hat it does mention, or a hat it never mentions
    at all) survives from `base` untouched. This is the tier-table
    equivalent of `_table_from`'s per-hat merge above, one level deeper."""
    merged = {hat: dict(tiers) for hat, tiers in base.items()}
    for hat, tiers in overlay.items():
        if not isinstance(tiers, dict):
            continue
        existing = dict(merged.get(hat) or {})
        existing.update(tiers)
        merged[hat] = existing
    return merged


def _resolve_model_tiers_table(args, cfg: dict, reg_cfg: dict) -> dict:
    """Task 004 (#68): the hat x tier `model_tiers` table (task 002's
    JobConfig field), reachable from `ralphctl start` the same shape task
    008 gave the flat per-hat `models`/`model_overrides` table
    (`_resolve_model_table` above), except every level's merge happens at
    BOTH the hat key and the tier key (`_merge_tier_tables`), never
    replace-whole at either.

    Order, weakest to strongest:

    1. `<registry>/config.yaml`'s own `model_tiers:` key.
    2. the `--template`'s `job.yaml` `model_tiers:` key.
    3. one or more explicit `--model-for-tier HAT:TIER=REF` flags on this
       command, applied last (so they win per (hat, tier) rung over both
       levels above) and in argv order (last one for a repeated rung
       wins).

    A named LLM profile has no tier table of its own yet either (same gap
    `_resolve_model_table` already documents for the flat table) -- that
    rung is presently always empty for this table too.
    """
    table = _tier_table_from(reg_cfg)
    table = _merge_tier_tables(table, _tier_table_from(cfg))
    flag_table: dict = {}
    for spec in args.model_for_tier or []:
        hat_tier, sep, ref = spec.partition("=")
        hat, csep, tier = hat_tier.partition(":")
        if not sep or not csep or not hat.strip() or not tier.strip() or not ref.strip():
            die(2, f"--model-for-tier {spec!r} must be HAT:TIER=REF, e.g. "
                    f"worker:hard=prov/model-id (known hats: {', '.join(MODEL_HATS)}; "
                    f"known tiers: {', '.join(TIERS)}; an unrecognised hat or tier "
                    "name still resolves via the anti-downgrade rule, see docs/cli.md)")
        flag_table.setdefault(hat.strip(), {})[tier.strip()] = ref.strip()
    return _merge_tier_tables(table, flag_table)


def _resolve_model_table(args, cfg: dict, reg_cfg: dict) -> dict:
    """Task 008 (#60 pillar 4, requirement A): the per-hat `models`/
    `model_overrides` table, reachable from the same three levels the
    scalar `model`/`thinking` knobs already are (`_TEMPLATE_SCALAR_FIELDS`/
    `_REGISTRY_CONFIG_FIELD_KEYS`), except a *table* has to MERGE key by
    key rather than replace-whole: setting `--model-for worker=...` must
    not blank out a `verify` entry a template or the registry already
    supplied for a different hat.

    Order, weakest to strongest:

    1. `<registry>/config.yaml`'s own `models:`/`model_overrides:` key -- a
       nested mapping, read directly off the parsed YAML like `pricing:`
       is (there is no scalar `ralphctl config set` for a table).
    2. the `--template`'s `job.yaml` `models:`/`model_overrides:` key.
    3. one or more explicit `--model-for HAT=REF` flags on this command,
       applied last (so they win per-hat over both levels above) and in
       argv order (last one for a repeated hat wins).

    A named LLM profile has no per-hat table of its own yet (only the
    single `model:` scalar, docs/llm-profiles.md) -- so that rung, which
    would sit between level 1 and the hardcoded default (absent = engine's
    own per-hat default, `model_for`), is presently always empty for this
    table rather than skipped; a future profile schema could add one at
    that exact position without another resolver.
    """
    table = _table_from(reg_cfg)
    table.update(_table_from(cfg))
    for spec in args.model_for or []:
        hat, sep, ref = spec.partition("=")
        if not sep or not hat.strip() or not ref.strip():
            die(2, f"--model-for {spec!r} must be HAT=REF, e.g. worker=prov/model-id "
                    f"(known hats: {', '.join(MODEL_HATS)}; an unrecognised name still "
                    f"resolves via the anti-downgrade rule, see docs/cli.md)")
        table[hat.strip()] = ref.strip()
    return table


def _resolve_image_supply(args, cfg: dict, reg_cfg: dict) -> None:
    """Settle `image` / `base_image` / `dockerfile` as one unit (task 035, H3).

    Requirement H3's "most specific winning", read the only way that does not
    quietly combine two contradictory answers: the three keys are three answers
    to *one* question -- which image does this job run -- so the most specific
    **level** that answers it at all wins whole, and lower levels are not
    consulted for the other two keys. `--dockerfile ci/Dockerfile` on the
    command line therefore beats a standing `image:` pin in the registry's
    `config.yaml` instead of colliding with it; two of the three *within* one
    level is a usage error (exit 2), because there is no way to rank them.

    `RALPHD_IMAGE` is deliberately not a level: it is ambient, so rather than
    being silently dropped by a `--dockerfile` (or silently winning over one) it
    is carried into `resolve_job_image`, which refuses the combination by name
    (task 034's IMAGE_BASE_AND_PIN_NOTICE). Nobody answering anywhere leaves
    all three unset, which is "build `ralphd:<hash>` from source" (task 033).

    Mutates `args` in place; called from `_apply_template` before the
    field-by-field loop, which no longer knows about these three keys.
    """
    levels = ((IMAGE_SUPPLY_LEVEL_FLAG,
               {k: getattr(args, k, None) for k in IMAGE_SUPPLY_KEYS}),
              (IMAGE_SUPPLY_LEVEL_TEMPLATE, cfg),
              (IMAGE_SUPPLY_LEVEL_REGISTRY, reg_cfg))
    chosen: tuple[str, str] | None = None
    for where, level in levels:
        picked = [(k, str(level.get(k)).strip()) for k in IMAGE_SUPPLY_KEYS
                  if level.get(k) not in (None, "")]
        if not picked:
            continue
        if len(picked) > 1:
            die(2, IMAGE_SUPPLY_CLASH_NOTICE.format(
                where=where,
                keys=" and ".join(IMAGE_SUPPLY_FLAGS[k] for k, _ in picked)))
        chosen = picked[0]
        break
    for key in IMAGE_SUPPLY_KEYS:
        setattr(args, key, chosen[1] if chosen and chosen[0] == key else None)
    if chosen is None or chosen[0] != "image":
        # ambient RALPHD_IMAGE: the bottom of the ladder when nothing else
        # answered, and a refusal (never a silent drop) when something did.
        args.image = IMAGE_ENV


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
    # the three image supply points first, as one unit (task 035): the loop
    # below fills fields independently, which is exactly wrong for three keys
    # that contradict each other.
    _resolve_image_supply(args, cfg, reg_cfg)
    # Task 008 (#60 pillar 4, requirement A): the per-hat `models`/
    # `model_overrides` table -- a fourth thing `_TEMPLATE_SCALAR_FIELDS`'s
    # loop below cannot handle field-by-field because it MERGES across
    # levels instead of one level replacing the last. `args.model_overrides`
    # is not an argparse field; `cmd_start` reads it from here.
    args.model_overrides = _resolve_model_table(args, cfg, reg_cfg)
    # Task 004 (#68): the hat x tier `model_tiers:` table -- same reason
    # this cannot be one more `_TEMPLATE_SCALAR_FIELDS` entry as
    # `args.model_overrides` above: it merges across levels at two key
    # levels (hat AND tier) rather than one level replacing the last.
    # `args.model_tiers` is not an argparse field; `cmd_start` reads it
    # from here.
    args.model_tiers = _resolve_model_tiers_table(args, cfg, reg_cfg)
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


def _print_rubric_status(prd_text: str, workspace_root: Path) -> None:
    """Print which materiality rubric applies to `prd_text` (task 005,
    requirement A.3): exactly one line -- `rubric: inline`, `rubric: by
    reference (<path>)` or `rubric: engine default` -- mirroring
    `findings.resolve_materiality_rubric` (the same resolver
    `LoopSupervisor._review_materiality_rubric_context` uses at review
    time, task 003), so a PRD reports the same rubric before and after the
    run starts. Warns, once per unresolved reference, using that
    resolver's own `log_events` (a referenced rubric path that does not
    resolve to a readable file, or that resolves but carries no rubric
    section of its own).

    Everything this function emits -- the rubric line as well as the
    warnings -- goes to **stderr** (task 041): `ralphctl start`'s stdout is
    a machine-readable contract (`out()`'s single JSON object under
    `--json`, parsed by every docker e2e via `json.loads(res.stdout)` /
    `_first_json_object`, and by operator scripts), and task 005 shipped
    this line on stdout, which broke that contract in 0.18.0. The line is
    still printed exactly once per invocation, so requirement A.3 holds;
    only the stream changed.
    """
    resolution = findings.resolve_materiality_rubric(prd_text, workspace_root)
    if resolution.rubric is None:
        line = "rubric: engine default"
    elif resolution.source_path is not None:
        line = f"rubric: by reference ({resolution.source_path})"
    else:
        line = "rubric: inline"
    print(line, file=sys.stderr)
    for event in resolution.log_events:
        print(f"ralphctl: warning: {event['message']}", file=sys.stderr)


def cmd_start(args):
    _apply_template(args)
    # Task 006 (#60 pillar 4 cleanup): `--fast-model`/`--model-strategy` are
    # retired -- refuse explicitly, naming the replacement, rather than
    # silently dropping the flag's value (a no-op) or letting it land
    # unexplained. `_apply_template` never fills these from a template/
    # registry value any more (dropped from `_TEMPLATE_SCALAR_FIELDS`/
    # `_REGISTRY_CONFIG_FIELD_KEYS`), so only an explicit CLI flag reaches
    # this check.
    if args.fast_model is not None or args.model_strategy is not None:
        flag = "--fast-model" if args.fast_model is not None else "--model-strategy"
        die(2, f"{flag} is removed (task 006): per-hat models are now a "
                "job.yaml `models:` (or `model_overrides:`) table, e.g.\n"
                "  models:\n    worker: prov/fast-model\n    verify: prov/fast-model\n"
                "naming any of: planning, worker, verify, review, reflect, router "
                "-- an unnamed hat falls back to `model`.")
    if args.prd is None:
        die(2, "--prd is required (or use --template with a prd.md skeleton)")
    run_id = args.run_id or gen_run_id()
    rdir = run_root(run_id)
    if rdir.exists() and any(rdir.iterdir()):
        die(2, f"run {run_id} already exists")
    cdir = config_root(run_id)
    # Requirement H1/H4 (#20): resolve -- and, on a cache miss, BUILD -- the
    # job image *before* any run state exists, so a failed build aborts here
    # instead of leaving a half-registered run dir behind. An explicit
    # selection (flag/env/template/registry) pins and skips both steps; a base
    # or a `--dockerfile` recipe (task 035) is layered into a derived image.
    resolved_image = resolve_job_image(args.image, base=args.base_image,
                                       dockerfile=args.dockerfile)
    args.image = resolved_image["image"]
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

    # Task 004 (#62 candidate 1, requirement A): the mutating-only gate's
    # secret. Provisioned unconditionally -- NOT gated on `--api-token` --
    # so a real run closes `/abort`, `/pause`, `/resume`, `/retry`,
    # `/shutdown`, `PATCH /config/budget` and every `PUT`/`DELETE` under
    # `/config` to an iteration by default, whether or not the operator
    # ever opted into `--api-token`. `.control-token` lives beside
    # `.api-token` in the run dir for the same reason `.api-token` does
    # (task 003 strips `RALPHD_CONTROL_TOKEN` from a spawned iteration's
    # own environment; the file's readability from inside the container is
    # requirement D's job, not this one's).
    control_token = secrets.token_urlsafe(24)
    ctf = rdir / ".control-token"
    ctf.write_text(control_token)
    os.chmod(ctf, 0o600)

    # Named LLM profile (docs/llm-profiles.md): resolved once, here, before
    # anything else touches it -- job.yaml's `model` default and
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
        # Task 017 (D.3, #117 side finding): forwarded exactly like
        # `vigilant`/`reflect` above -- omitted from job.yaml entirely when
        # the flag is absent (the `if v is not None` filter a few lines
        # below this dict), so the engine's own `JobConfig` default (False)
        # applies unconditionally-off, same as the two flags it sits beside.
        "peer_plan_review": args.peer_plan_review,
        "peer_verdict_gate": args.peer_verdict_gate,
        "model": args.model or (llm_profile.get("model") if llm_profile else None),
        # Task 006 (#60 pillar 4 cleanup): `fast_model`/`model_strategy` are
        # retired job.yaml keys -- a named LLM profile may still declare its
        # own `fast_model` (docs/llm-profiles.md's schema, unchanged), but
        # `start` no longer forwards it into job.yaml; per-hat overrides live
        # in the `models:`/`model_overrides:` job.yaml table, reachable from
        # `start` via the repeatable `--model-for HAT=REF` flag (task 008,
        # see `_resolve_model_table` below) as well as by hand-editing
        # job.yaml directly.
        # Task 008: the resolved per-hat table (explicit `--model-for`
        # flags merged over the template's and the registry's own
        # `models:`/`model_overrides:` key, see `_resolve_model_table`) --
        # omitted entirely when empty so an unconfigured run's job.yaml
        # carries no table at all, exactly like `model`/`thinking` above.
        "models": args.model_overrides or None,
        # Task 004 (#68): the resolved hat x tier table (explicit
        # `--model-for-tier` flags merged over the template's and the
        # registry's own `model_tiers:` key, see
        # `_resolve_model_tiers_table`) -- omitted entirely when empty,
        # exactly like `models` above.
        "model_tiers": args.model_tiers or None,
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
        # Task 035 (#20 H3): the *recipe* this run's image was derived from,
        # persisted so `ralphctl resume` replays it instead of falling back to
        # some default image (see `_resume_job_image`). Only the ingredients an
        # operator supplied are recorded -- the resolved tag itself is run
        # state, not job config, and lives in host.json.
        "base_image": args.base_image,
        "dockerfile": resolved_image["imageDockerfile"],
    }
    (cdir / "job.yaml").write_text(
        "".join(f"{k}: {json.dumps(v)}\n" for k, v in job.items() if v is not None))

    port = args.port or free_port()
    env_args: list[str] = []
    mounts = [
        "-v", f"{rdir}:/run/ralphd",
        "-v", f"{cdir}:/config:ro",
    ]
    # Task 020 (E.4): the per-run MAC-key host directory, mode 0700, bound
    # at the fixed container path `petitions.DEFAULT_RUN_SECRETS_DIR` --
    # see `_bind_run_secrets_mount`'s docstring above.
    _bind_run_secrets_mount(run_id, mounts)
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
    # Task 005 (A.3): which rubric this run's PRD resolves to, printed
    # exactly once -- resolved against the workspace that will actually be
    # mounted at /workspace (the single unnamed one, else the first named
    # one, else the CLI's own cwd when no --workspace was given at all, so
    # a rubric-by-reference PRD with no workspace still gets an honest
    # warning instead of a silent skip).
    _print_rubric_status(prd, ws if ws is not None else
                          (next(iter(ws_named.values())) if ws_named else Path.cwd()))

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
    env_args += ["-e", f"RALPHD_CONTROL_TOKEN={control_token}"]

    cmd = [DOCKER, "run", "-d", "--name", job_container_name(run_id),
           "--init", "--ulimit", "core=0", *port_args,
           *docker_args, *mounts, *env_args, args.image]
    res = sh(cmd)
    if res.returncode != 0:
        shutil.rmtree(rdir, ignore_errors=True)
        shutil.rmtree(cdir, ignore_errors=True)
        die(1, f"docker run failed: {res.stderr.strip()}")
    container = res.stdout.strip()
    meta = {"runId": run_id, "container": container, "port": port,
            "apiUrl": f"http://{args.api_bind}:{port}",
            "image": args.image,
            **_image_record_fields(resolved_image, container),
            "startedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ",
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


def _effective_budget_base(job: dict, rdir: Path) -> int:
    """Task 011 (#51): the base that `resume --iterations +N` tops up.

    This has to be the same number the engine's own `_seed_iteration_budget`
    (task 010, `engine/main.py`) would pick at the *next* startup --
    `max(status.json's recorded iterationsBudget, job.yaml's iterations)` --
    never job.yaml's alone. Before this, a live top-up (`ralphctl budget
    <run-id> +N`) that already raised status.json above job.yaml's value was
    silently discarded by a `+N` resume: `+N` was added to the *smaller*
    job.yaml figure, so an operator watching `status`/`GET /config` for
    `iterationsBudget` saw a number go backwards. job.yaml is still allowed to
    win when an operator raised *it* directly between episodes (the ordinary
    case, and #51's second acceptance direction) -- this is `max()`, not
    "status.json always wins".
    """
    job_budget = int(job.get("iterations", 25))
    status = _read_json(rdir / "status.json", {}) or {}
    status_budget = status.get("iterationsBudget")
    if isinstance(status_budget, int) and status_budget > job_budget:
        return status_budget
    return job_budget


def _resolve_topup_spec(flag: str, spec: str, base: int) -> int:
    """Resolve a `+N|N` CLI spec against `base` -- the ONE grammar shared by
    every resume-time budget knob: `--iterations` (task 011) and, task
    004/#94 (c), `--approaches`/`--timeout`/`--iteration-timeout`. `+N` bumps
    `base` by N; a bare integer sets the result absolutely (documented as
    `+N` but an absolute value is harmless and occasionally convenient).
    `flag` only names the flag in the error message."""
    spec = spec.strip()
    try:
        if spec.startswith("+"):
            return base + int(spec[1:])
        return int(spec)
    except ValueError:
        die(2, f"{flag}: invalid value {spec!r} (expected e.g. +10 or 30)")


def _apply_iterations_topup(job: dict, spec: str, base: int) -> None:
    """`--iterations +N` bumps `base` (the *effective* budget -- see
    `_effective_budget_base`, task 011) by N; a bare integer sets the
    resulting job.yaml value absolutely."""
    job["iterations"] = _resolve_topup_spec("--iterations", spec, base)


# Task 004 (#94 (c)+(d)): the three process-only knobs (a2's PATCH
# /config/budget docstring and docs/cli.md's budget table state the same
# decision) -- unlike `iterations`, a resume with none of these flags never
# seeds job.yaml from a live-engine value that might have drifted (there is
# no status.json field for them to seed from, on purpose): the base a `+N`
# tops up is always job.yaml's OWN current value, falling back to the
# engine's own JobConfig default when job.yaml has never carried the key.
def _apply_approaches_topup(job: dict, spec: str) -> None:
    current = job.get("max_approaches")
    base = int(current) if current is not None else JobConfig().max_approaches
    job["max_approaches"] = _resolve_topup_spec("--approaches", spec, base)


def _apply_job_timeout_topup(job: dict, spec: str) -> None:
    current = job.get("job_timeout_s")
    base = int(current) if current is not None else JobConfig().job_timeout_s
    job["job_timeout_s"] = _resolve_topup_spec("--timeout", spec, base)


def _apply_iteration_timeout_topup(job: dict, spec: str) -> None:
    current = job.get("iteration_timeout_s")
    base = int(current) if current is not None else JobConfig().iteration_timeout_s
    job["iteration_timeout_s"] = _resolve_topup_spec(
        "--iteration-timeout", spec, base)


def _resume_job_image(job: dict) -> dict:
    """The image recipe a `resume` replays when run state records none (task
    035, H3) -- one `resolve_job_image` record, same shape as `start`'s.

    A run started from a `--base-image` or a `--dockerfile` recorded those
    ingredients in its own `job.yaml` (see cmd_start), so a resume replays the
    *recipe* the run started with rather than dropping back to `ralphd:dev` --
    which is a different engine, and (for a job whose repo needs a JDK) an
    image without the toolchain the PRD was written against. On unchanged
    sources the replay is a pure tag lookup: same base, same source, same tag.

    This is the *fallback*, not the first answer: task 036 records the resolved
    image itself in run state and `_resume_image` prefers it, because replaying
    a recipe over changed sources produces a different image, and requirement
    H4 asks for the same one. A run with no recipe and no record keeps the
    pre-035 behaviour exactly (DEFAULT_IMAGE).
    """
    base = job.get("base_image") or None
    dockerfile = job.get("dockerfile") or None
    if not (base or dockerfile):
        return {"image": DEFAULT_IMAGE, "imageSource": IMAGE_SOURCE_DEFAULT,
                "imageHash": None, "imageBase": None, "imageDockerfile": None}
    return resolve_job_image(job.get("image") or None, base=base,
                            dockerfile=dockerfile)


def _resume_image(pinned: str | None, job: dict, prev_meta: dict) -> dict:
    """Which image a `resume` runs -- requirement H4's core promise (task 036).

    Ranked, most-specific-first:

    1. `--image` pins, as everywhere else.
    2. **the image this run started on**, as recorded in its own run state
       (`host.json`: the reference plus the daemon-observed content id). It is
       reproduced exactly -- by reference while that reference still names the
       recorded image, by the recorded *id* once the reference has moved -- and
       on this path **no hash is computed and no build runs**: a resume after
       the sources changed continues on the same engine instead of hashing the
       checkout again and swapping the engine mid-run.
    3. the *recipe* the run started with (task 035), when the recorded image is
       no longer on this daemon at all (pruned, or a different machine).
    4. DEFAULT_IMAGE, for a pre-v0.6 run dir that records neither.

    Returns a `resolve_job_image`-shaped record (so `cmd_resume` writes the
    same host.json fields `cmd_start` does); provenance recorded at `start`
    (hash/base/dockerfile) is carried forward on path 2, because it is
    literally the same image. Every degradation is announced on stderr.
    """
    if pinned:
        return {"image": pinned, "imageSource": IMAGE_SOURCE_PINNED,
                "imageHash": None, "imageBase": None, "imageDockerfile": None}
    rec = image_record_from(prev_meta)
    ref, rec_id = rec["image"], rec["imageId"]

    def recorded(reference: str) -> dict:
        return {"image": reference, "imageSource": IMAGE_SOURCE_RECORDED,
                "imageHash": rec["imageHash"], "imageBase": rec["imageBase"],
                "imageDockerfile": rec["imageDockerfile"]}

    def warn(notice: str) -> None:
        print(f"ralphctl: warning: {notice}", file=sys.stderr)

    if ref:
        present, current_id = inspect_image(ref)
        if present and (not rec_id or not current_id or current_id == rec_id):
            # the recorded reference still names the recorded image (or nobody
            # can say otherwise) -- the whole promise, for one docker call.
            return recorded(ref)
        short = format_image_id(rec_id)
        if rec_id and inspect_image(rec_id)[0]:
            warn(IMAGE_RECORDED_MOVED_NOTICE.format(ref=ref, short=short))
            return recorded(rec_id)
        if present:
            warn(IMAGE_RECORDED_ID_GONE_NOTICE.format(ref=ref, short=short))
            return recorded(ref)
        warn(IMAGE_RECORDED_GONE_NOTICE.format(ref=ref))
    return _resume_job_image(job)


# Task 021 (#31): liveness has THREE states, not two. `docker inspect` can
# answer "no such container", "exists and is running" and "exists and has
# exited", and the third one is the shape every engine death leaves behind
# (`docker run` without `--rm`, a killed engine, an OOM). The old two-valued
# reading (`_container_running` -> bool | None) carried all three, but the
# name invited `is not None` to mean "something is alive there", and
# `_dangling_run_entry` read it exactly that way -- so an exited-but-present
# container was invisible to status, doctor, repair AND auto-resume through
# one line. The vocabulary is named here, once, so the distinction cannot be
# lost again at a call site.
CONTAINER_ABSENT = "absent"     # no container by that name exists
CONTAINER_RUNNING = "running"  # exists, and something is running in it
CONTAINER_EXITED = "exited"    # exists, but nothing is running in it
CONTAINER_LIVENESS_STATES = (CONTAINER_ABSENT, CONTAINER_RUNNING,
                             CONTAINER_EXITED)


def _container_liveness(name: str) -> str:
    """THE liveness question, in its three-valued form (task 021, #31): one
    of `CONTAINER_ABSENT` / `CONTAINER_RUNNING` / `CONTAINER_EXITED`.

    One docker call, same as before -- what changed is that the third state
    has a name, so a caller that means "a live engine holds this run dir"
    cannot accidentally spell it "a container record exists"."""
    res = sh([DOCKER, "inspect", "--format", "{{.State.Running}}", name])
    if res.returncode != 0:
        return CONTAINER_ABSENT
    return (CONTAINER_RUNNING if res.stdout.strip() == "true"
            else CONTAINER_EXITED)


def _container_running(name: str) -> bool | None:
    """True/False if the container exists (running or not); None if no
    container by that name exists at all.

    Kept as the boolean *view* of `_container_liveness` for the callers that
    only ever ask "is a live engine holding this run dir?" (`resume`,
    `repair`) -- and because the docker-tier tests import it. Derived, never
    a second docker call or a second reading of the format string."""
    live = _container_liveness(name)
    if live == CONTAINER_ABSENT:
        return None
    return live == CONTAINER_RUNNING


# Task 021 (#31): the two dangling shapes read differently to an operator --
# "your container is gone" vs "your container is still sitting there, dead" --
# and the second one is also where `docker logs` still works. Both stories are
# worded ONCE here, keyed by liveness, and every surface that reports the
# condition (status's warning, repair's diagnosis and `--set-state` reason,
# doctor's sweep) renders from this table instead of inventing its own text.
_DANGLING_PHRASE = {
    CONTAINER_ABSENT: {
        "status": "appears gone (no such container)",
        "state": "no longer exists",
        "why": "the container died or was removed outside ralphctl",
    },
    CONTAINER_EXITED: {
        "status": "still exists but has exited (nothing is running in it)",
        "state": "exists but has exited",
        "why": "the container exited without the engine recording a "
               "terminal state",
    },
}


def _dangling_phrase(entry: dict, key: str) -> str:
    """One wording for a `_dangling_run_entry` shape (task 021). Unknown
    liveness degrades to the absent wording -- the pre-#31 text -- rather
    than raising in a diagnostic path."""
    return _DANGLING_PHRASE.get(entry.get("liveness"),
                                _DANGLING_PHRASE[CONTAINER_ABSENT])[key]


def cmd_resume(args):
    """PRD req 16: start a fresh container over an *existing* run dir. The
    engine side (task 028, src/ralphd/engine/loop.py:_resume_point) detects
    pre-existing `tasks.json`/iterations and continues instead of
    re-planning; this only has to reproduce cmd_start's docker-run wiring
    for the same mounts (run dir, config dir, workspace-if-any) plus an
    optional `--iterations +N` budget top-up written into the existing
    job.yaml before the container starts.

    Task 004 (#94 (c)+(d)): `--approaches`/`--timeout`/`--iteration-timeout`
    take the same `+N|N` spec and land in job.yaml's `max_approaches`/
    `job_timeout_s`/`iteration_timeout_s`, right next to `--iterations`'s
    `iterations` write, above. The PERSISTENCE DECISION for all three
    -- deliberately the opposite of `iterations` -- is: they are
    PROCESS-ONLY while a container is live (`PATCH /config/budget`'s
    `maxApproaches`/`iterationTimeoutS`/`jobDeadlineEpochS` are never seeded
    from status.json the way `iterations` is, see engine/api.py's
    `patch_budget` docstring), so a plain `resume` with none of these three
    flags REVERTS them to whatever job.yaml already said (this run's
    `start`-time value, or an earlier `resume --approaches`/`--timeout`/
    `--iteration-timeout` that DID pass a flag and so wrote job.yaml) --
    never to whatever a live `ralphctl budget --approaches`/`--timeout`/
    `--iteration-timeout` last set in the container that just exited. Only
    passing the flag again here carries a live change forward. See
    docs/cli.md's budget table for the same statement in operator-facing
    form, one row per knob."""
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
        _apply_iterations_topup(job, args.iterations,
                                 _effective_budget_base(job, rdir))
    if args.approaches:
        _apply_approaches_topup(job, args.approaches)
    if args.timeout:
        _apply_job_timeout_topup(job, args.timeout)
    if args.iteration_timeout:
        _apply_iteration_timeout_topup(job, args.iteration_timeout)
    if args.iterations or args.approaches or args.timeout or args.iteration_timeout:
        _write_job_yaml(job_path, job)
    prev_meta = host_meta(run_id)
    # `--image` on resume pins as always; with no flag, reproduce the image this
    # run started on from its own run state (task 036, H4), falling back to the
    # recipe it started with (task 035) only when that image is gone.
    resolved_image = _resume_image(args.image, job, prev_meta)
    args.image = resolved_image["image"]

    ws = prev_meta.get("workspace")
    ws_named: dict[str, str] = prev_meta.get("workspaces") or {}
    port = args.port or free_port()
    mounts = ["-v", f"{rdir}:/run/ralphd", "-v", f"{cdir}:/config:ro"]
    # Task 020 (E.4): reproduce the same bind on every `resume` -- a run dir
    # created before this task self-heals (the directory is created/mode-
    # fixed unconditionally by `_bind_run_secrets_mount`, never assumed to
    # already exist or be 0700).
    _bind_run_secrets_mount(run_id, mounts)
    if ws:
        mounts += ["-v", f"{ws}:/workspace"]
    for name, path in ws_named.items():
        mounts += ["-v", f"{path}:/workspace/{name}"]

    env_args: list[str] = []
    token_file = rdir / ".api-token"
    if token_file.exists():
        env_args += ["-e", f"RALPHD_API_TOKEN={token_file.read_text().strip()}"]
    # Task 004: `.control-token` was provisioned unconditionally at `start`
    # (or, for a run dir predating task 004, is simply absent -- an older
    # run resumes with the mutating gate open, exactly as it always was;
    # `resume` never retrofits a boundary the run did not start with).
    control_file = rdir / ".control-token"
    if control_file.exists():
        env_args += ["-e", f"RALPHD_CONTROL_TOKEN={control_file.read_text().strip()}"]

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

    cmd = [DOCKER, "run", "-d", "--name", name, "--init", "--ulimit", "core=0", *port_args,
           *docker_args, *mounts, *env_args, args.image]
    res = sh(cmd)
    if res.returncode != 0:
        die(1, f"docker run failed: {res.stderr.strip()}")
    container = res.stdout.strip()
    meta = {"runId": run_id, "container": container, "port": port,
            "apiUrl": f"http://{args.api_bind}:{port}",
            "image": args.image,
            **_image_record_fields(resolved_image, container),
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
RUN_STATE_ORDER = ["starting", "running", "paused", "finishing", "succeeded",
                  "failed", "aborted"]
RUN_VERDICT_ORDER = ["", "unverified", "verified", "verified-with-residuals"]
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


def _format_deadline_beat_verdict_lines(deadline_beat_verdict) -> list[str]:
    """Task 007 (#72): the `deadline:` line for the one terminal write task
    006's `_deadline_beat_verdict_patch()` produces -- every task in the
    record was completed, the worker's own `COMPLETE` was already honoured,
    and the deadline took the last review attempt before it could ever
    render a verdict. `state`/`verdict` above still print the ordinary
    `failed` / `unverified` (that ruling stays the review hat's alone to
    grant -- this task does not touch it), so without a line naming the
    marker this run reads on screen exactly like any other failure. This is
    the line that tells a human reader apart: it names the deadline, the
    moment `COMPLETE` was honoured, and says in so many words that the
    ending is not a judgement on the work.

    Nothing at all unless status.json actually carries the
    `deadlineBeatVerdict` object -- absent for every other terminal shape,
    including an ordinary failed run and a genuinely rejected approach that
    happens to look similar on disk (task 006's own docstring names why that
    one is deliberately excluded upstream).
    """
    if not deadline_beat_verdict or not isinstance(deadline_beat_verdict, dict):
        return []
    deadline = deadline_beat_verdict.get("deadline")
    honoured = deadline_beat_verdict.get("completeHonouredAt")
    text = (
        "the clock beat the verdict, not the work (#72): every task was "
        f"completed and COMPLETE was honoured at {honoured}, but the "
        f"deadline ({deadline}) took the last review attempt before it "
        "could ever render one -- this is not a failure judgement."
    )
    wrapped = textwrap.wrap(text, width=76) or [text]
    lines = [f"deadline:  {wrapped[0]}"]
    lines.extend(f"           {extra}" for extra in wrapped[1:])
    return lines


def _format_termination_lines(termination) -> list[str]:
    """Task 015 (#46): the `termination:` line(s) for a run a signal ended with
    no operator behind it -- the class, and the last tool call before the
    signal as evidence.

    Nothing at all for every other run, so their human output is byte-identical
    to before: an *operator* abort needs no line here (its `reason:` already
    says `aborted by operator` and the operator is the one who asked), and a
    run with no `termination` record was never told to stop. The one case this
    exists for is the one `reason: signal 15` used to answer with nothing.
    """
    if not isinstance(termination, dict):
        return []
    if termination.get("class") != TERMINATION_CLASS_SELF:
        return []
    sig = termination.get("signal")
    head = (f"self-inflicted (signal {sig})" if sig else "self-inflicted")
    lines = [(f"termination: {head} — no operator abort was recorded; "
              "eligible for auto-resume")]
    detail = format_last_tool_call(termination.get("evidence"))
    if detail:
        wrapped = textwrap.wrap(detail, width=64) or [detail]
        lines.extend(f"             {part}" for part in wrapped)
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

    Task 016 (#47) adds the third shape: `ok: null` with a `skipped` reason --
    the engine was signalled, so the phase was never attempted (or was cut
    short by the teardown) and no failure was manufactured for it. That prints
    `reflection: not attempted (<why>)` / `not completed (<why>)`; `ok: false`
    still prints `failed (...)`, and `ok: true` / null-without-a-reason still
    print nothing at all.
    """
    if not isinstance(reflect, dict):
        return []
    # Task 016 (#47): the engine was signalled, so there is no verdict and the
    # reflection is not to blame -- `ok: null` with a `skipped` reason. Without
    # a line of its own this run looks exactly like `reflect: false` (the
    # failure mode the failed-reflection line above exists to fix), because
    # every other trace of the phase is a FAILED.md the engine correctly did
    # not write.
    if reflect.get("ok") is None and str(reflect.get("skipped") or "").strip():
        head = "not completed" if reflect.get("attempted") else "not attempted"
        return _reflect_wrapped(f"{head} ({str(reflect['skipped']).strip()})")
    if reflect.get("ok") is not False:
        return []
    error = str(reflect.get("error") or "").strip() or "reason not recorded"
    return _reflect_wrapped(f"failed ({error})")


def _reflect_wrapped(text: str) -> list[str]:
    """`reflection: <text>`, wrapped and hanging-indented (task 020, #5)."""
    wrapped = textwrap.wrap(text, width=76) or [text]
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
    non-terminal but which has no running container -- the zombie condition
    `_dangling_run_entry` owns (the same one doctor and repair report).

    Before this line existed the operator had to join two facts printed lines
    apart -- `state: running` and `(live api: False)` -- and know that the
    combination means "this run died without ever recording a terminal
    state", which reads exactly like a briefly-unreachable healthy run. Said
    once, explicitly, naming the container and the command that diagnoses it
    (`repair` names both the `--set-state` and the `resume` remedy, so status
    does not fork that story).

    Task 021 (#31): the first line words the two dangling shapes apart (gone
    vs exited-but-present, via `_dangling_phrase`) -- an exited container is
    one `docker logs` away from the reason it died, so calling it "gone" would
    send the operator looking for something that is still right there.
    """
    state = status.get("state")
    return [_ansi(tty, "1;31",
                  f"container: {entry['container']} "
                  f"{_dangling_phrase(entry, 'status')} -- status.json still"),
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


def _format_progress_budget_lines(progress_budget) -> list[str]:
    """Task 014 (#156, requirement A.4): `ralphctl status`'s own rendering
    of status.json's `progressBudget` key (`{taskId, used, budget}`, task
    013) -- a pure formatting helper in the same shape as
    `_format_reason_lines` above, so it is unit-testable without a live
    engine or a run dir.

    Nothing at all for a run that has never recorded a corroborated
    progress claim (the key absent, or not a dict) -- the same "absent,
    not a crash" discipline every other optional status.json field in
    this command follows."""
    if not isinstance(progress_budget, dict) or not progress_budget.get("taskId"):
        return []
    return [f"progress budget: task {progress_budget.get('taskId')}  "
            f"{progress_budget.get('used')}/{progress_budget.get('budget')} "
            "iterations used"]


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

    # Task 010 (#79, PRD requirement O): the resolved hat -> model mapping,
    # computed from THIS run's own job.yaml on disk -- available whether or
    # not the run is live, and before any iteration has run, unlike the
    # per-iteration `model`/`modelRaw` fields above which only exist once
    # something has actually dispatched. Same `JobConfig.provisioning_table`
    # + `state.format_provisioning_lines` pair the hub's `run_detail` calls
    # (`ui_server._with_provisioning`), so the two surfaces cannot drift.
    _cfg_path = config_root(args.run_id) / "job.yaml"
    _provisioning = (JobConfig.load(_cfg_path).provisioning_table()
                     if _cfg_path.is_file() else [])
    status["provisioning"] = _provisioning
    status["provisioningLines"] = format_provisioning_lines(_provisioning)

    # Task 020 (E.4): whether this run's per-run MAC-key host directory is
    # bound (created by `start`/`resume`, see `run_secrets_dir`) -- known
    # from the host alone, live or not, unlike `model`/`provisioning`
    # above.
    #
    # `bound` is about the MOUNT (did `start`/`resume` create and bind the
    # directory); `keyAvailable` is the separate, weaker condition that
    # decides whether cross-episode authentication can work at all -- is
    # there a key FILE in there (`run_secrets_key_files`). A bound but
    # empty directory is the ordinary single-uid shape: `key_store_dir()`
    # returns None, no key is ever written, and the run's only handle is
    # the `events.jsonl` `petition_adjudications_recorded` MAC witness.
    # So the fallback check is reported whenever no key is available, not
    # merely when the directory is missing (the first attempt at this task
    # used directory-exists as a proxy for key-exists and was rejected for
    # exactly that).
    _secrets_dir = run_secrets_dir(args.run_id)
    _secrets_keys = run_secrets_key_files(args.run_id)
    status["runSecretsMount"] = {
        "path": str(_secrets_dir),
        "target": str(petitions.DEFAULT_RUN_SECRETS_DIR),
        "bound": _secrets_dir.is_dir(),
        "keyAvailable": bool(_secrets_keys),
        "keyFiles": [p.name for p in _secrets_keys],
        "fallbackCheck": (None if _secrets_keys
                          else RUN_SECRETS_FALLBACK_CHECK),
    }

    # Task 036 (#20 H4): which image this run is running. A live `GET /status`
    # already answers it (the engine reads the same host.json), so this only
    # fills keys the answer did not carry -- the on-disk path, and a live run
    # started by a pre-v0.6 ralphctl/engine -- rather than overwriting what the
    # run's own API said.
    for key, value in image_record(run_root(args.run_id)).items():
        if status.get(key) is None:
            status[key] = value

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
    #
    # Task 021 (#31): the condition now also matches an exited-but-present
    # container, so `containerGone` -- documented as the *vanished*-container
    # case -- can no longer carry it alone. `dangling` is the condition
    # (either shape: what repair/doctor/auto-resume act on),
    # `containerLiveness` says which shape it is, and `containerGone` keeps
    # its exact pre-v0.7 meaning rather than quietly widening under a name
    # that would then be a lie for half the runs it covers.
    container_gone = None if live else _dangling_run_entry(args.run_id)
    status["dangling"] = container_gone is not None
    status["containerLiveness"] = (container_gone or {}).get("liveness")
    status["containerGone"] = (
        container_gone is not None
        and container_gone["liveness"] == CONTAINER_ABSENT)

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

    # Task 015 (#56): the `finishing` window (task 012) settles `endedAt`
    # before the reflect iteration that still has to run is over -- so an
    # `endedAt` sitting alongside a `currentIteration` dict is not yet a
    # real end; it is the record `_finish()` wrote ahead of time. Treat that
    # combination as "not ended yet" for every HUMAN-readable rendering
    # below (never for `--json`, which passes `status` through untouched --
    # `endedAt`/`durationSeconds` there are exactly what `_finish()` wrote,
    # unaffected by this).
    iteration_in_progress = isinstance(cur_it, dict)
    ended_at_for_display = None if iteration_in_progress else status.get("endedAt")

    if stale_since:
        shown_s, duration_label = since_update_s, "since last update"
    elif iteration_in_progress and status.get("endedAt"):
        # Recomputed against "now" instead of the frozen `endedAt` `_finish()`
        # already wrote -- otherwise the number would say "elapsed" but sit
        # perfectly still while the reflect iteration keeps running.
        shown_s = elapsed_seconds(status.get("startedAt"))
        duration_label = "elapsed"
    else:
        shown_s = duration_s
        duration_label = "total" if ended_at_for_display else "elapsed"
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
    if ended_at_for_display:
        lines.append(f"ended:     {format_local_time(ended_at_for_display)}")
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
    # Task 010 (#79, PRD requirement O): the resolved hat -> model mapping
    # (`provisioningLines`, computed above from job.yaml through the ONE
    # shared `state.format_provisioning_lines` formatter) -- printed
    # unconditionally, unlike the per-iteration `model:` line above, because
    # it is known from job.yaml alone and reads the same before iteration 1
    # as it does at iteration 100.
    if status.get("provisioningLines"):
        lines.append("provisioning:")
        for prov_line in status["provisioningLines"]:
            lines.append(f"  {prov_line}")
    # Task 036 (#20 H4): and which job image it is running it on -- the
    # reference plus the daemon's short id for the image the container actually
    # got, so "is this the engine I think it is?" and "what will a resume run?"
    # are answerable without `docker inspect`. Omitted entirely when run state
    # records no image (a pre-v0.6 run dir), never `image: None`.
    image_line = format_image(status)
    if image_line:
        lines.append(f"image:     {image_line}")
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
    # Task 007 (#72): ... and when the reason the record reads failed is that
    # the deadline beat the verdict rather than the work having failed, a
    # `deadline:` line names the marker task 006 added -- otherwise this run
    # is indistinguishable on screen from an ordinary failure.
    lines.extend(_format_deadline_beat_verdict_lines(
        status.get("deadlineBeatVerdict")))
    # Task 015 (#46): ... and when a signal nobody claimed ended the run, the
    # class and the evidence, because `reason: signal 15` on its own told an
    # operator nothing they could act on.
    lines.extend(_format_termination_lines(status.get("termination")))
    lines.append(f"tasks:     {_summarize_tasks(status.get('tasks') or {})}")
    # Task 014 (#156, requirement A.4): the per-task progress budget task
    # 013 persists on status.json -- named here so a corroborated
    # no_verdict's own usage is visible without `--json`.
    lines.extend(_format_progress_budget_lines(status.get("progressBudget")))
    lines.append(f"usage:     {_summarize_usage(status.get('usage') or {})}")
    # Task 020 (E.4): name the run-secrets MAC-key mount, and name the
    # documented fallback check in the same line whenever NO KEY is
    # available -- whether because the directory is not bound at all (a
    # pre-task-020 run dir that has not resumed yet, or a registry whose
    # `configs/<run>/` was removed out from under a live run) or because
    # it is bound and empty (no uid boundary, so `key_store_dir()`
    # returned None and nothing was ever persisted). Reporting the mount
    # alone would tell the operator the key survived when it never
    # existed.
    _rsm = status.get("runSecretsMount") or {}
    _mount = (f"{_rsm.get('path')} -> {_rsm.get('target')}")
    _state = "bound" if _rsm.get("bound") else "not bound"
    if _rsm.get("keyAvailable"):
        lines.append(f"run-secrets: {_state} ({_mount}); MAC key present: "
                     f"{', '.join(_rsm.get('keyFiles') or [])}")
    else:
        lines.append(
            f"run-secrets: {_state} ({_mount}); no MAC key available "
            "(cross-episode authentication fails closed); fallback check: "
            + (_rsm.get("fallbackCheck") or RUN_SECRETS_FALLBACK_CHECK))
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


def _progress_budget_doc(run_id: str) -> dict | None:
    """`ralphctl tasks`'s cross-reference into status.json for task 014
    (#156, requirement A.4): `progressBudget` (`{taskId, used, budget}`,
    task 013) lives on status.json, not tasks.json, so annotating a task
    row with it means one more read alongside `_tasks_doc`'s own -- same
    live-with-on-disk-fallback shape as `cmd_status` uses for the exact
    same key. Best-effort: a run dir this run cannot read at all yields
    `None` (no suffix printed) rather than raising -- absent/unreadable
    budget data must never make `ralphctl tasks` itself fail.
    """
    err = io.StringIO()
    try:
        with contextlib.redirect_stderr(err):
            status = api(run_id, "GET", "/status")
        if isinstance(status, dict):
            return status.get("progressBudget")
    except SystemExit as e:
        if e.code != 4:
            return None
    status = _read_json(run_root(run_id) / "status.json") or {}
    return status.get("progressBudget")


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
    # Task 025 (#33): `TasksRead.payload`, the same shaping `GET /tasks` serves
    # -- so the on-disk answer carries the derived `taskFailureKinds` too and
    # `--json` reads the same either way.
    return False, res.payload


# Task 025 (#33): a `ralphctl tasks` status cell is as wide as its widest
# entry, never narrower than this. `failed (validation-exhausted)` does not fit
# the historical 17 columns, and a plan with no failed task must still print
# byte-identically to before -- so the width is measured per plan rather than
# frozen at either number.
_TASK_STATUS_MIN_WIDTH = 17


def _task_status_width(tasks: list, blocked_by: dict | None = None,
                       progress_budget: dict | None = None) -> int:
    widths = []
    for t in tasks:
        if not isinstance(t, dict):
            continue
        tid = str(t.get("id")) if t.get("id") is not None else None
        by = (blocked_by or {}).get(tid) if tid else None
        widths.append(len(format_task_status(t, by, progress_budget)))
    return max([_TASK_STATUS_MIN_WIDTH, *widths])


def _task_status_cell(task: dict, width: int, blocked_by: list | None = None,
                      progress_budget: dict | None = None) -> str:
    """The status column of one `ralphctl tasks` line: the shared
    `format_task_status` wording (so a failed task says WHICH kind of failed it
    is, derived for a plan written before task 024; task 021 (#60 pillar 3)
    adds the ` (blocked by ...)` suffix when `blocked_by` -- this task's own
    entry in the harness-derived `taskBlockedBy` map -- is non-empty;
    task 014 (#156, requirement A.4) adds the ` (progress budget N/M)`
    suffix for the in-progress task named by `progress_budget`), padded
    to `width`."""
    return f"{format_task_status(task, blocked_by, progress_budget):<{width}}"


def _archived_approaches_dir(run_id: str) -> Path:
    return archived_approaches_dir(run_root(run_id))


def _archived_approach_dir(run_id: str, approach: int) -> Path:
    """Where `LoopSupervisor._archive_approach()` (`engine/loop.py`) copies a
    finished approach's `tasks.json`/`notes.md`/`review-findings.md`: task
    019 (#24) reads through the SAME `NN` zero-padding, never a second
    resolver -- `engine.state.archived_approach_dir`, moved there by task
    020 (#24) so the hub's approach routes read through this identical
    resolver."""
    return archived_approach_dir(run_root(run_id), approach)


def _archived_approach_numbers(run_id: str) -> list[int]:
    """Every archived approach for a run, sorted numerically -- delegates to
    `engine.state.archived_approach_numbers` (moved there by task 020, #24)
    so the CLI and the hub skip the same not-a-plain-integer directory names
    the same way, through the same resolver."""
    return archived_approach_numbers(run_root(run_id))


def _print_task_lines(tasks: list, blocked_by: dict | None = None,
                      progress_budget: dict | None = None) -> None:
    """Task 021 (#60 pillar 3): `blocked_by` is the harness-derived
    `{task id: [dead/doomed ancestor ids]}` map (`taskBlockedBy` on the wire,
    `TasksRead.blocked_by` on disk) -- optional, so a caller with no such map
    handy (or an older engine's payload with no such key) prints exactly as
    it always did.

    Task 014 (#156, requirement A.4): `progress_budget`, when given, is
    this run's `progressBudget` status.json record (`{taskId, used,
    budget}`, task 013) -- optional the same way, so a caller with no
    budget handy (an archived approach's own task table, which has none)
    prints exactly as before."""
    width = _task_status_width(tasks, blocked_by, progress_budget)
    for t in tasks:
        tid = str(t.get("id")) if t.get("id") is not None else None
        by = (blocked_by or {}).get(tid) if tid else None
        print(f"[{_task_status_cell(t, width, by, progress_budget)}] "
              f"{t.get('id')} {t.get('title')}")


def _cmd_tasks_approach(args, approach: int) -> None:
    """`ralphctl tasks <run> --approach N` (task 019, #24): that approach's
    OWN task table, read from `approaches/NN/tasks.json` through the same
    hardened reader every other surface uses -- never recomputed from the
    live plan, which may be several approaches further on by the time this
    is read."""
    _require_run(args.run_id)
    adir = _archived_approach_dir(args.run_id, approach)
    if not (adir / "tasks.json").exists():
        die(1, f"approach {approach} was never archived for run "
               f"{args.run_id} (no {adir / 'tasks.json'})")
    res = read_tasks_doc(adir, persist=False)
    payload = {**res.payload, "approach": approach}
    if args.json:
        print(json.dumps(payload, indent=2))
        return
    notice = res.notice
    if notice:
        print(f"!! {notice}", file=sys.stderr)
    print(f"approach {approach} (archived): "
          f"{format_task_counts(res.counts)}")
    _print_task_lines(res.tasks, res.blocked_by)


def _cmd_tasks_all(args) -> None:
    """`ralphctl tasks <run> --all` (task 019, #24): every archived approach
    with its OWN completed/total counts, each taken from that approach's own
    archived `tasks.json` -- never the live plan's counts repeated under a
    different label."""
    _require_run(args.run_id)
    numbers = _archived_approach_numbers(args.run_id)
    rows = []
    for n in numbers:
        res = read_tasks_doc(_archived_approach_dir(args.run_id, n),
                              persist=False)
        counts = res.counts
        rows.append({"approach": n, "counts": counts,
                     "summary": format_task_counts(counts),
                     **res.contract})
    if args.json:
        print(json.dumps({"approaches": rows}, indent=2))
        return
    if not rows:
        print(f"no archived approaches for run {args.run_id} yet",
              file=sys.stderr)
        return
    for row in rows:
        print(f"approach {row['approach']:02d}: {row['summary']}")


def cmd_tasks(args):
    approach = getattr(args, "approach", None)
    if approach is not None:
        _cmd_tasks_approach(args, approach)
        return
    if getattr(args, "all", False):
        _cmd_tasks_all(args)
        return
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
    # Task 014 (#156, requirement A.4): the in-progress task's own progress
    # budget (status.json's `progressBudget`, task 013), fetched the same
    # live-with-on-disk-fallback way as everything else in this command.
    budget = _progress_budget_doc(args.run_id)
    _print_task_lines(tasks.get("tasks", []), tasks.get(TASK_BLOCKED_FIELD) or {}, budget)


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
    """One iteration's own story, in three LABELLED regions (task 020, #81):
    outcome (`meta.json`) FIRST -- phase, model, timestamps, duration, exit
    reason, tokens and cost -- then input (`prompt.md`) then logs (the full
    transcript). The hub's iteration dialog renders
    the very same `state.iteration_detail_text` block (task 020, #18.1/#81).

    Purely on-disk: `iterations/NNNN/meta.json`, `prompt.md` and that
    iteration's transcript are written by the engine into the run dir, so
    this works identically for a running job and for one whose container is
    long gone -- there is nothing to fall back from, hence no `--live`-style
    notice (see `state.iteration_detail`).
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
    prompt_text = read_iteration_prompt(root, args.number)

    if args.json:
        doc = {"runId": args.run_id, **detail, "promptText": prompt_text}
        # `log` is absent (not empty) with --no-log: an empty list would claim
        # the iteration produced no transcript.
        if log_lines is not None:
            doc["log"] = log_lines
        print(json.dumps(doc, indent=2))
        return

    it_line = f"run:       {args.run_id}"
    # Task 020/#81: the labelled three-region block is worded ONCE, in
    # `state.iteration_detail_text`, and shown verbatim by the hub's
    # iteration dialog too -- only this `run:` line (the id the operator
    # typed) belongs to the CLI.
    lines = [it_line] + iteration_detail_text(
        detail, prompt_text=prompt_text, log_lines=log_lines)
    print("\n".join(lines))


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
#
# `ralphctl skills install` (task 022, requirement R, #88) is the ONE action
# under this verb that takes no run id -- it installs the shipped operator
# skill (`skills/ralphd/SKILL.md`, this repo's own guide for a coding agent
# to *drive* ralphctl) onto the local machine, rather than managing a run's
# skill inventory over the API. `run_id` is `nargs="?"` on the parser for
# exactly this reason: `ralphctl skills install ...` and `ralphctl skills
# <run-id> ls|get|add|rm` share one subparsers tree and argparse resolves
# which positional the single leading token fills correctly on its own (a
# bare `install` satisfies the required subparsers action, leaving run_id
# unset) -- see the parser construction below for the two-line proof.

_SKILL_INSTALL_NAME = "ralphd"
_SKILL_INSTALL_STAMP = ".ralphd-skill-stamp.json"


def _skill_discoverable(path: Path) -> bool:
    """The agent's own discoverability rule (#83's lesson, restated for R):
    a directory containing `SKILL.md`. `install` calls this AFTER placing
    the copy and reports what it found, rather than reporting success
    because the copy call returned; `doctor` (task 023) calls the same
    function so the two surfaces can never disagree about what
    'discoverable' means."""
    return path.is_dir() and (path / "SKILL.md").is_file()


def _skills_install_source_dir() -> Path:
    """Locate this install's copy of `skills/ralphd` on disk.

    A checkout wins (same precedence `find_inputs` uses for the image
    inputs): if this is a checkout, its `skills/ralphd` is the live source.
    Otherwise (task 023) fall back to the packaged copy the wheel ships at
    `ralphd/_image/skills/ralphd` (`image.packaged_skill_dir`) -- so a real
    `pipx install ralphd` now has a real source instead of the refusal this
    used to be the only wheel behaviour.
    """
    root = image.source_root()
    if root is not None:
        src = root / "skills" / _SKILL_INSTALL_NAME
        if not (src / "SKILL.md").is_file():
            die(2, f"skills install: {src} has no SKILL.md")
        return src
    packaged = image.packaged_skill_dir(_SKILL_INSTALL_NAME)
    if packaged is not None:
        return packaged
    die(2, "skills install: no source checkout and no packaged skills/ralphd "
           "found in this install -- reinstall ralphd to get its skill data")


def _auto_detect_skills_install_target(home: Path) -> Path:
    """Pick an install target when `--target` is not given.

    Preference order: an agent's skills/ dir that already has content wins
    (don't create a second home for the same agent); then an agent whose
    config dir exists but has no skills/ yet; and only when NEITHER agent
    has ever been set up on this machine does this default to
    `~/.pi/agent/skills` -- ralphd's own skill
    (`skills/ralphd/SKILL.md`) is written for a pi-driven agent first, so
    that is the sane default when there is nothing on disk to detect at
    all. pi is checked before claude at every tier so the two agents never
    race for a default; a caller who wants the other one uses `--target`.
    """
    pi_skills = home / ".pi" / "agent" / "skills"
    claude_skills = home / ".claude" / "skills"
    if pi_skills.is_dir():
        return pi_skills
    if claude_skills.is_dir():
        return claude_skills
    if (home / ".pi" / "agent").is_dir():
        return pi_skills
    if (home / ".claude").is_dir():
        return claude_skills
    return pi_skills


def _skill_tree_hash(d: Path, *, exclude: str | None = None) -> str:
    """A deterministic content hash of every file under `d`, in sorted
    relative-path order, skipping a top-level file named `exclude` (the
    stamp file itself so hashing the destination doesn't hash its own
    record of the last hash). Used both to stamp what `install` placed and
    to tell, on a later `install`, whether the target still matches that
    stamp -- i.e. whether it was locally modified since."""
    h = hashlib.sha256()
    for p in sorted(d.rglob("*")):
        if not p.is_file():
            continue
        rel = p.relative_to(d).as_posix()
        if exclude is not None and rel == exclude:
            continue
        h.update(rel.encode())
        h.update(b"\0")
        h.update(p.read_bytes())
    return h.hexdigest()


def _read_skill_install_stamp(dest: Path) -> dict | None:
    p = dest / _SKILL_INSTALL_STAMP
    if not p.is_file():
        return None
    try:
        return json.loads(p.read_text())
    except (json.JSONDecodeError, OSError):
        return None


# The three states `ralphctl doctor` reports for the operator skill (task
# 023, requirement R). Vocabulary, so doctor and its test agree on the exact
# words rather than each spelling "not installed" its own way.
OPERATOR_SKILL_INSTALLED = "installed"
OPERATOR_SKILL_MISSING = "missing"
OPERATOR_SKILL_STALE = "stale"


def _operator_skill_status(home: Path) -> dict:
    """installed / missing / stale for `ralphctl doctor`'s operator-skill
    line (task 023, requirement R).

    Checks the same two agent layouts `install` auto-detects, pi first --
    the same order, so doctor and install can never disagree about where the
    skill lives -- and reports on whichever one is actually discoverable
    (`_skill_discoverable`, the same rule `install` verifies its own copy
    against) rather than only the one `install` would default to. `missing`
    when neither is discoverable; otherwise `stale` when the install stamp's
    recorded version differs from this `ralphctl`'s own `__version__`
    (`installedVersion` names it, for the "(built from 0.x.y)" line), and
    `installed` when it matches -- or when there is no stamp at all (a skill
    placed some other way than `ralphctl skills install`; there is no
    version to call stale against, so this does not guess one).
    """
    for dest in (home / ".pi" / "agent" / "skills" / _SKILL_INSTALL_NAME,
                home / ".claude" / "skills" / _SKILL_INSTALL_NAME):
        if _skill_discoverable(dest):
            stamp = _read_skill_install_stamp(dest)
            installed_version = stamp.get("version") if stamp else None
            if installed_version is not None and installed_version != __version__:
                status = OPERATOR_SKILL_STALE
            else:
                status = OPERATOR_SKILL_INSTALLED
            return {"status": status, "path": str(dest),
                    "installedVersion": installed_version}
    return {"status": OPERATOR_SKILL_MISSING, "path": None,
            "installedVersion": None}


def cmd_skills_install(args):
    """Copy `skills/ralphd` to a local agent skills dir; refuse a local
    edit unless `--force`; verify discoverability rather than trusting the
    copy returned (task 022, requirement R)."""
    home = Path(os.environ.get("HOME") or os.path.expanduser("~"))
    src = _skills_install_source_dir()
    target_dir = (Path(args.target).expanduser().resolve() if args.target
                  else _auto_detect_skills_install_target(home))
    dest = target_dir / _SKILL_INSTALL_NAME

    source_hash = _skill_tree_hash(src)
    if dest.exists():
        stamp = _read_skill_install_stamp(dest)
        installed_hash = _skill_tree_hash(dest, exclude=_SKILL_INSTALL_STAMP)
        if (stamp is None or stamp.get("sourceHash") != installed_hash) \
                and not args.force:
            die(2, f"skills install: {dest} differs from what was last "
                   f"installed by ralphctl (local modification) -- rerun "
                   f"with --force to overwrite")

    target_dir.mkdir(parents=True, exist_ok=True)
    staging = dest.with_name(dest.name + ".installing")
    if staging.exists():
        shutil.rmtree(staging)
    shutil.copytree(src, staging)
    if dest.exists():
        shutil.rmtree(dest)
    staging.rename(dest)
    (dest / _SKILL_INSTALL_STAMP).write_text(
        json.dumps({"version": __version__, "sourceHash": source_hash},
                   indent=2) + "\n")

    verified = _skill_discoverable(dest)
    if not verified:
        die(1, f"skills install: placed at {dest} but it is not "
               f"discoverable (no SKILL.md there) -- the copy did not land "
               f"cleanly")
    out(args, {"target": str(dest), "verified": True},
        f"installed '{_SKILL_INSTALL_NAME}' to {dest} -- verified "
        f"discoverable (directory contains SKILL.md)")


def cmd_skills(args):
    if args.action == "install":
        cmd_skills_install(args)
        return
    if not args.run_id:
        die(2, "skills: a run id is required for ls|get|add|rm "
               "(only 'install' takes none)")
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


_AMEND_SNAPSHOT_NOTICE = (
    "on-disk read: `ralphctl amend` writes host-side into this run's "
    "read-only-mounted /config directory and never touches the API, so "
    "--list always reads the run dir directly, live container or not")


def cmd_amend_list(args) -> None:
    """`ralphctl amend <run> --list` (task 028, #26): what operator rulings
    have been recorded for this run.

    Unlike `steer --list`, there is no live/API half to this at all --
    `ralphctl amend` never goes through the engine, so there is nothing for
    a running container to answer differently than the file on disk. Always
    reads `config_root(run_id)/amendments/` directly through the ONE shared
    reader (`engine.state.amendment_entries`), the same reader
    `_operator_amendment_review_context()` uses from inside the container --
    so "what `ralphctl amend --list` shows" and "what review was actually
    handed" can never disagree about which rulings exist.
    """
    _require_run(args.run_id)
    entries = amendment_entries(config_root(args.run_id))
    if args.json:
        print(json.dumps({"entries": entries}, indent=2))
        return
    print(_AMEND_SNAPSHOT_NOTICE, file=sys.stderr)
    if not entries:
        print("(no operator rulings recorded)")
        return
    print(f"{'SEQ':>3}  {'MODE':<7}  {'RECORDED':<25}  {'TASK':<8}  RULING")
    for e in entries:
        if e.get("mode") == AMENDMENT_MODE_STATUS:
            mode = "status"
            preview = f"-> {e.get('status')}"
            reason = " ".join(str(e.get("text") or "").split())
            if reason:
                preview += f" ({reason})"
        else:
            mode = "waive" if e.get("waive") else "replace"
            preview = " ".join(str(e.get("text") or "").split())
        if len(preview) > 60:
            preview = preview[:59] + "\u2026"
        seq = e.get("seq")
        print(f"{(str(seq) if isinstance(seq, int) else '-'):>3}  {mode:<7}  "
              f"{str(e.get('ts') or '-'):<25}  {str(e.get('taskId') or '-'):<8}  "
              f"{preview}")


def cmd_amend(args):
    """`ralphctl amend <run> <task-id> [text]` (task 028, #26): record an
    operator ruling review must honour -- a waiver or a replacement of one
    task's `successCriteria` -- for a PRD requirement that turned out to be
    impossible, or a scope call only the operator can make.

    Deliberately HOST-ONLY: this never opens a socket, never calls the
    engine's HTTP API, and never touches the run dir
    (`registry()/runs/<id>`) at all -- it writes directly into
    `config_root(run_id)` (`registry()/configs/<id>`), the SAME directory
    `ralphctl start`/`resume` bind-mount `:ro` as `/config` into the job
    container. That is what makes the ruling unforgeable from inside: no
    process the container ever runs -- worker shell, the API's config-CRUD
    routes, the writable config overlay -- can write there. See
    artifacts/reports/task-027-operator-ruling-design.md for the full
    reasoning and docs/cli.md's `ralphctl amend` section for the surface.

    `--list` shows what has been recorded instead of writing a new one (see
    `cmd_amend_list`); combining it with a task id / text / `--file` /
    `--waive` is a usage error, mirroring `steer --list`.

    `--status <status>` (task 006/029, #26 requirement E) records a THIRD
    ruling kind instead of a criteria waiver/replacement: a typed status
    ruling the engine itself applies to that task's `status` field at load
    time (`LoopSupervisor._apply_operator_status_rulings` ->
    `engine.state.apply_amendment_status_rulings`), never a claim about
    successCriteria. Goes through the SAME `write_amendment` writer as
    every other ruling (`status=` kwarg instead of `waive=`) -- there is no
    second writer. `<status>` must be one of `engine.transitions.
    TASK_STATUSES`; anything else is rejected here, on the host, before a
    file is ever written, so a typo never reaches the run dir as an inert
    file the engine silently ignores. The trailing text/`--file`/stdin
    argument becomes the operator's OPTIONAL reason (never required, never
    read from stdin when omitted) -- combining `--status` with `--waive`
    is a usage error.
    """
    if args.list:
        if args.task_id or args.text or args.file or args.waive or args.status:
            die(2, "--list only shows this run's recorded operator rulings; "
                   "drop the task id/text/--file/--waive/--status")
        cmd_amend_list(args)
        return
    _require_run(args.run_id)
    if not args.task_id:
        die(2, "a task id is required (or --list to show recorded rulings)")
    cdir = config_root(args.run_id)
    cdir.mkdir(parents=True, exist_ok=True)
    if args.status:
        if args.waive:
            die(2, "--status and --waive are two different ruling kinds; "
                   "drop one")
        if args.status not in TASK_STATUSES:
            die(2, f"unknown status {args.status!r}; must be one of "
                   f"{', '.join(TASK_STATUSES)}")
        reason = args.text or (Path(args.file).read_text() if args.file else "")
        fname = write_amendment(cdir, args.task_id, reason,
                                status=args.status)
        out(args, {"file": fname, "taskId": args.task_id,
                   "status": args.status},
            f"operator status ruling recorded: {fname} (task {args.task_id} "
            f"-> {args.status}, applied by the engine at load time)")
        return
    text = args.text or (Path(args.file).read_text() if args.file
                         else sys.stdin.read())
    if not text.strip():
        die(2, "empty ruling text")
    fname = write_amendment(cdir, args.task_id, text, waive=args.waive)
    kind = "waiver" if args.waive else "replacement"
    out(args, {"file": fname, "taskId": args.task_id, "waive": args.waive},
        f"operator ruling recorded ({kind}): {fname} "
        f"(applies to task {args.task_id} at review time)")


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
    """Task 046 (#3): change a *running* job's iteration budget in flight;
    task 004 (#94 (c)+(d)) extends this to the other three `PATCH
    /config/budget` knobs task 003 added: `--approaches`
    (`maxApproaches`), `--timeout` (`jobDeadlineEpochS`) and
    `--iteration-timeout` (`iterationTimeoutS`).

    Thin operator front-end for `PATCH /config/budget` (task 045/003), taking
    the same spec syntax for all four knobs as `resume --iterations`/
    `--approaches`/`--timeout`/`--iteration-timeout`: `+N` tops up by N, a
    bare `N` sets the knob absolutely (so lowering is explicit -- a bare
    `-5` is an absolute negative value and is rejected, never read as a
    decrement). The positional `iterations` and the three flags are each
    independent and optional; any non-empty subset reaches the engine in
    ONE request (task 003's `patch_budget` accepts all four in one body).
    The bare positional keeps meaning the iteration spec exactly as before
    task 004 -- `ralphctl budget <run> +5` is unchanged.

    Only the shape is checked here; every semantic decision (below
    `iterationsUsed`/the current approach, job already finished, a deadline
    in the past) belongs to the engine, which owns the live counters. Exit
    codes come from the shared api() helper, so they match
    pause/unpause/retry/abort: 0 applied, 5 on the engine's 409 refusals, 1
    on its 422 (invalid value), 3 unknown run, 4 API unreachable, 2 for a
    locally malformed spec or an empty request (usage errors, same as
    `resume --iterations`).

    The change is live-engine only: `/config/job.yaml` is a read-only
    mount, so the engine cannot persist any of the four knobs there itself.
    `iterations` still PERSISTS across a plain resume (task 010/#51's
    startup seeding). Task 004/#94 (d)'s decision for the other three is the
    opposite: they are PROCESS-ONLY and a plain `resume` with no
    `--approaches`/`--timeout`/`--iteration-timeout` flag of its own
    REVERTS them to job.yaml's own value -- see `cmd_resume`'s docstring and
    docs/cli.md's budget table for the same statement in operator-facing
    form.
    """
    _require_run(args.run_id)
    body: dict[str, str] = {}
    _FLAG_TO_API_KEY = {
        "approaches": "maxApproaches",
        "timeout": "jobDeadlineEpochS",
        "iteration_timeout": "iterationTimeoutS",
    }
    if args.iterations is not None:
        spec = args.iterations.strip()
        if not re.fullmatch(r"\+?-?\d+", spec):
            die(2, f"budget: invalid value {spec!r} (expected e.g. +10 or 30)")
        body["iterations"] = spec
    for attr, api_key in _FLAG_TO_API_KEY.items():
        value = getattr(args, attr)
        if value is None:
            continue
        flag = "--" + attr.replace("_", "-")
        spec = value.strip()
        if not re.fullmatch(r"\+?-?\d+", spec):
            die(2, f"{flag}: invalid value {spec!r} (expected e.g. +10 or 30)")
        body[api_key] = spec
    if not body:
        die(2, "budget: nothing to change -- pass an iteration spec, "
               "--approaches, --timeout or --iteration-timeout")
    res = api(args.run_id, "PATCH", "/config/budget", body)
    lines = []
    if "iterations" in res:
        lines.append(f"iteration budget: {res.get('previous')} -> "
                     f"{res.get('iterations')} ({res.get('iterationsUsed')} used)")
    if "maxApproaches" in res:
        lines.append(f"approaches budget: {res.get('previousMaxApproaches')} "
                     f"-> {res.get('maxApproaches')}")
    if "iterationTimeoutS" in res:
        lines.append(f"iteration timeout: {res.get('previousIterationTimeoutS')}s "
                     f"-> {res.get('iterationTimeoutS')}s")
    if "jobDeadlineEpochS" in res:
        lines.append(f"job deadline: {res.get('previousJobDeadlineEpochS')} -> "
                     f"{res.get('jobDeadlineEpochS')} ({res.get('deadlineAt')})")
    out(args, res, "\n".join(lines) if lines else json.dumps(res))


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

    Task 014 (#56): the reflect unwind's `finishing` state (engine/state.py's
    `NONTERMINAL_STATES`, task 012) is neither of those two things -- the
    verdict is already settled, nothing is "still running" that `abort`
    could stop, but the terminal state is not yet on disk either. Plain `rm`
    refuses it with a reason naming the unwind (checked BEFORE the generic
    container-exists refusal below, and regardless of whether a container
    record exists, so a crashed-mid-unwind run dir with no container left
    still gets the accurate reason rather than falling through to a plain
    delete). `--force`'s terminal-state guard is widened here to accept
    `finishing` too -- deliberately, matching `ui_server.deletion_refusal`'s
    reasoning for the same state (task 013): a settled-but-not-yet-terminal
    verdict is exactly the case `--force` exists to let an operator past.
    """
    run_id = args.run_id
    if not run_root(run_id).exists():
        die(3, f"run {run_id} not found")
    state = _read_json(run_root(run_id) / "status.json", {}).get("state")
    if state == "finishing" and not args.force:
        die(5, "run is finishing (reflect unwind in progress) — wait for it "
               "to reach a terminal state, or use `rm --force`")
    container_exists = container_record_exists(run_id)
    if container_exists and not args.force:
        die(5, "container still exists — `stop` first (or `rm --force`)")
    if (container_exists and state not in TERMINAL_STATES
            and state != "finishing"):
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


_TASK_STATUSES = ("pending", "in-progress", "awaiting-validation", "validated",
                  "completed", "validation-failed", "failed", "skipped",
                  "petitioned")
# Task 019 (requirement F, #60 pillar 3): now SEVEN statuses plus `pending`
# -- `awaiting-validation` (the vigilant worker's claim state) and
# `validated` (the vigilant verifier-granted terminal) joined the five
# task 025 counted, mirroring `engine.transitions.TASK_STATUSES` exactly.
# The two meanings of `failed` are told apart by the `failureKind` label
# (engine/state.py's `TASK_FAILURE_KINDS`), never by a sixth status, which
# is what keeps every surface that counts statuses adding up.
# `_diagnose_tasks_json` below validates both halves together.
_STATUS_STATES = ("starting", "running", "paused", "finishing", "succeeded",
                  "failed", "aborted")
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
        # Task 025 (#33): `failureKind` is the label that tells the two
        # meanings of `failed` apart (engine/state.py owns the vocabulary).
        # Readers never crash on a bad one -- `task_failure_kind()` derives the
        # kind from `validationAttempts` instead -- so a wrong label is exactly
        # the kind of silent misreading `repair` exists to name. Reported for
        # the value AND for the placement: on anything but a `failed` task the
        # label describes nothing.
        kind = t.get("failureKind")
        if kind is not None and kind not in TASK_FAILURE_KINDS:
            issues.append(f"tasks.json: task {label} has unrecognized "
                          f"failureKind {kind!r} (expected one of "
                          f"{', '.join(TASK_FAILURE_KINDS)})")
        elif kind is not None and status != "failed":
            issues.append(f"tasks.json: task {label} has failureKind "
                          f"{kind!r} but status {status!r}, not 'failed'")
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
    requirement E): a run recorded non-terminal with no running container.
    Reuses `_dangling_run_entry` -- doctor's check -- rather than a second
    implementation, and names the fix (via `_dangling_remedy`, the same
    text doctor prints -- task 025) so `repair` stops reporting a zombie
    run as 'no issues found'.

    Task 021 (#31) widened the condition to the exited-but-present container;
    the issue text says which of the two shapes it is (`_dangling_phrase`),
    since the remedy is the same but where to look for the cause is not."""
    entry = _dangling_run_entry(run_id)
    if entry is None:
        return []
    state = _read_json(run_root(run_id) / "status.json", {}).get("state")
    issue = (f"container: {entry['container']} "
             f"{_dangling_phrase(entry, 'state')}, but "
             f"status.json still records state {state!r} -- "
             f"{_dangling_phrase(entry, 'why')}; "
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
        # task 021: a zombie run (recorded non-terminal, no running
        # container) gets a reason explaining what happened to the
        # container -- the same field the engine writes on its own terminal
        # transitions. #31: worded per liveness, so an operator reading
        # status.json months later is not told a container vanished when it
        # is still there, exited.
        reason = None
        if dangling is not None:
            reason = (f"container {dangling['container']} "
                      f"{_dangling_phrase(dangling, 'state')} "
                      f"({_dangling_phrase(dangling, 'why')}); state "
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


def cmd_router(args):
    """This run's router calls (task 022, P, #80): `ls` lists every
    decision the router made -- most runs have none, since the only
    router-ambiguous state shipped today is the opt-in peer plan-review
    gate's `plan_review` (`transitions.ROUTER_FALLBACK`'s one real entry,
    task 019) and every other hat decides its own edge without ever
    reaching `engine.router` -- `show <index>` prints one decision's full
    header block.

    Purely on-disk, like `ralphctl artifacts`: `state.router_call_entries`
    reads `artifacts/router/decisions.jsonl`, the engine's own append-only
    write, so a live run and one whose container is long gone read
    identically. Every entry says explicitly that it consumed no iteration
    and no approach (`state.ROUTER_CALL_NOTE`) -- never presented as a work
    iteration, and it needs no `iterations/NNNN/` directory of its own.
    """
    _require_run(args.run_id)
    root = run_root(args.run_id)
    entries = router_call_entries(root)
    if args.action == "show":
        try:
            index = int(args.index)
        except (TypeError, ValueError):
            die(2, "router show needs a numeric index (see `router ls`)")
        entry = next((e for e in entries if e["index"] == index), None)
        if entry is None:
            die(1, f"run {args.run_id} has no router call #{index} "
                   f"(recorded: {len(entries)})")
        if args.json:
            print(json.dumps({"runId": args.run_id, **entry}, indent=2))
            return
        print(f"run:       {args.run_id}")
        sys.stdout.write("\n".join(router_call_summary_lines(entry)) + "\n")
        return
    out(args, {"runId": args.run_id, "routerCalls": entries},
        "\n".join([f"run:       {args.run_id}",
                   *(format_router_calls_listing(entries) if entries
                     else [NO_ROUTER_CALLS])]))


# ------------------------------------------- the review-and-apply verb (041)
# `artifacts/reflection/suggestions.diff` is what a finished run proposes about
# the loop's own material, and until now nothing could apply it: the reflect
# prompt forbids the agent from doing so (SPEC §16), and no host-side surface
# could either, so v0.6's diff became eight hand-typed issues. This verb is the
# operator's half of that loop -- and its whole safety property is that
# **nothing is written unseen**: every invocation prints the diff and the plan
# first, and only `apply --yes` goes on to write. The matcher, the plan and the
# single writer live in `cli/patch.py`; what is here is the run dir, the target
# tree, the ordering and the exit codes.

# The section headers of the verb's output. Spelled once, because the tests
# assert the ORDER of these markers -- "was the diff shown before anything was
# written" is exactly the property that cannot be checked against retyped
# copies of the words.
SUGGESTIONS_DIFF_HEADER = "--- the diff as proposed ---"
SUGGESTIONS_PLAN_HEADER = "--- what it would touch ---"
SUGGESTIONS_WROTE_HEADER = "--- what was written ---"

# `apply` with no explicit act. Shown *after* the plan, so the run that stops
# here is also the run that showed the operator what would happen.
SUGGESTIONS_NEEDS_YES = (
    "nothing written -- re-run the same command with `--yes` to apply this "
    "plan")

# A well-formed diff that does not fit the target tree. The per-hunk detail
# (path, `@@` header, reason) comes from `patch.HunkFailure`, never from here.
SUGGESTIONS_REFUSED = "refusing to apply a diff that does not fit the target"

# ------------------------------------------- the two refusals of P part 2 (029)
# "Never mid-flight", made checkable. Two things can tie the tree a diff is
# about to be written into to a job that is running *right now*, and both of
# them are the requirement's case:
#
#   * the run is executing an image built from exactly this tree -- host.json's
#     `imageHash` is the content hash `start` tagged that image by, so the
#     prompt files about to be rewritten are the ones it is running. Nothing
#     reaches the live container (the prompts are baked in), which is precisely
#     the damage: from that moment the checkout no longer says what the run
#     under observation is doing, and SPEC's "every run stays reproducible
#     against known prompt material" stops being true for it;
#   * the tree is (or contains, or sits inside) the run's mounted workspace --
#     then the write lands in a directory a live iteration is editing, on top of
#     whatever it has half-finished there.
#
# Deliberately no override flag: applying to a copy is always available
# (`--target`), and a diff reaches a job only through the *next* `start`, so
# waiting costs nothing. The recorded state alone is not enough to refuse on --
# a run recorded `running` whose container is long gone would block every apply
# forever (`doctor` owns that condition) -- so a tie is confirmed with the same
# three-valued liveness question `resume`/`repair` ask.
SUGGESTIONS_MIDFLIGHT_REFUSED = (
    "refusing to write the material a live run is executing under")
SUGGESTIONS_MIDFLIGHT_WORKFLOW = (
    "apply to a copy of the tree instead (`--target DIR`), or let the run "
    "finish first -- a diff reaches a job only through the next `start`, never "
    "mid-flight")
SUGGESTIONS_MIDFLIGHT_HEADER = "--- live runs tied to this tree ---"
MIDFLIGHT_REASON_IMAGE = ("it is executing the image built from this tree "
                          "(imageHash {hash})")
MIDFLIGHT_REASON_WORKSPACE = "this tree is its mounted workspace ({path})"

# SPEC §16's conflict question: what happens when two runs propose edits to the
# same prompt. Answer: the second one is never taken silently, because the verb
# refuses to apply either while both still apply, and names both diffs. The
# matcher is context-exact, so applying both in sequence does not corrupt a file
# -- it produces a *third* version of it that neither run proposed and nobody
# reviewed, and the operator would never be told that is what happened. Only a
# sibling diff that would still be written counts as a conflict: one whose own
# plan no longer applies (stale, or already landed) can take nothing from this
# one. `--ignore-conflicts` is the operator's explicit "this run's suggestions
# win" -- a second act, like `--yes`, never a default.
SUGGESTIONS_CONFLICT_REFUSED = (
    "refusing to apply one of two diffs that both propose edits to the same "
    "file")
SUGGESTIONS_CONFLICT_WORKFLOW = (
    "decide which run's suggestions win: re-run with `--ignore-conflicts` to "
    "apply this one (the other diff is then re-planned against what was "
    "written, and refused if it no longer fits), or apply the other run's diff "
    "instead")
SUGGESTIONS_CONFLICT_HEADER = "--- other runs proposing edits to the same files ---"

# Requirement P part 3 (task 030): taking SOME of a diff's suggestions. Six of
# eight is a real operator need -- v0.6's diff became eight issues and two of
# them were wrong -- and the answer is a selection made before the plan exists
# (`patch.select_patches`), by whole file, not a half-finished write: what is
# selected is planned, shown and written all-or-nothing like any other plan, and
# what is left out is printed beside it so the record of a partial apply says it
# was partial. Hunk-level selection is refused, and the reason is that a `@@`
# header is not a stable name to select by -- the same file's hunks renumber the
# moment one of them applies -- while a path is exactly what the plan prints.
SUGGESTIONS_ONLY_HEADER = "--- left out by --only ---"
SUGGESTIONS_ONLY_UNKNOWN = "--only names a path this diff does not touch"
SUGGESTIONS_ONLY_WORKFLOW = (
    "select whole files, by the paths the plan lists; hunks are not separately "
    "selectable -- to take part of one file, take the whole file and edit the "
    "rest back out, or apply to a scratch copy (`--target DIR`) and carry "
    "across what you wanted")


def _suggestions_tree_ties(rdir: Path, root: Path, source_hash: str) -> list[str]:
    """Why one run dir is tied to the target tree, in the wording shown -- an
    empty list when it is not tied to it at all.

    Containment counts in *both* directions for the workspace tie: a diff path
    under a workspace mounted inside the tree is as much a live iteration's file
    as the other way round, and this is a refusal, so the generous reading is
    the safe one.
    """
    reasons = []
    rec = image_record(rdir)
    if rec.get("imageHash") and rec["imageHash"] == source_hash:
        reasons.append(MIDFLIGHT_REASON_IMAGE.format(hash=source_hash))
    meta = _read_json(rdir / "host.json", {}) or {}
    spelled = [meta.get("workspace")]
    if isinstance(meta.get("workspaces"), dict):
        spelled += list(meta["workspaces"].values())
    for value in spelled:
        if not isinstance(value, str) or not value.strip():
            continue
        try:
            ws = Path(value).expanduser().resolve()
        except OSError:                                   # pragma: no cover
            continue
        if ws == root or ws in root.parents or root in ws.parents:
            reasons.append(MIDFLIGHT_REASON_WORKSPACE.format(path=ws))
    return reasons


def _suggestions_midflight(root: Path, source_hash: str) -> list[dict]:
    """Every run that is running *right now* and tied to the target tree.

    One status.json read per run, the tie computed from files, and the docker
    liveness question asked only for a run that both claims to be non-terminal
    and is tied to this tree -- so a registry full of finished runs costs no
    docker calls at all.
    """
    runs_dir = registry() / "runs"
    if not runs_dir.is_dir():
        return []
    live = []
    for d in sorted(runs_dir.iterdir()):
        status = _read_json(d / "status.json", {})
        if not isinstance(status, dict) or \
                status.get("state") not in NONTERMINAL_STATES:
            continue
        reasons = _suggestions_tree_ties(d, root, source_hash)
        if not reasons:
            continue
        if _container_liveness(job_container_name(d.name)) != CONTAINER_RUNNING:
            continue
        live.append({"runId": d.name, "state": status.get("state"),
                     "reasons": reasons})
    return live


def _suggestions_conflicts(root: Path, run_id: str,
                          plan: patch.PatchPlan) -> list[dict]:
    """Other runs whose own `suggestions.diff` still applies to a file this plan
    would write (SPEC §16's two-runs question).

    A sibling diff is planned against the same tree, in memory, exactly the way
    this one was: unreadable or non-applying siblings are not conflicts, because
    they can never be written and so can never take anything from this diff.
    """
    runs_dir = registry() / "runs"
    mine = {c.path for c in plan.changes}
    if not mine or not runs_dir.is_dir():
        return []
    found = []
    for d in sorted(runs_dir.iterdir()):
        if d.name == run_id:
            continue
        rel = artifact_relpath("suggestions")
        diff = d / "artifacts" / rel
        try:
            other = patch.plan_text(root, diff.read_text())
        except (OSError, UnicodeDecodeError, patch.PatchError):
            continue
        if not other.ok:
            continue
        shared = sorted(mine & {c.path for c in other.changes})
        if shared:
            found.append({"runId": d.name, "diff": f"artifacts/{rel}",
                          "paths": shared})
    return found


def _suggestions_target(args) -> Path:
    """The tree a reflection diff is applied to.

    Default: the ralphd checkout this `ralphctl` runs out of -- the tree whose
    `src/ralphd/prompts/` the diff was written against and whose content hash
    decides which image the *next* `start` builds and runs. A wheel/pipx
    install has no such checkout (`image.source_root()` is None, and absence is
    an answer there too), so it is a usage error rather than a guess.
    """
    if getattr(args, "target", None):
        root = Path(args.target).expanduser()
        if not root.is_dir():
            die(2, f"--target is not a directory: {root}")
        return root.resolve()
    root = image.source_root()
    if root is None:
        die(2, "this ralphd install has no source checkout to apply a diff to "
               "(no container/Dockerfile beside it -- the wheel/pipx case); "
               "name the tree with --target")
    return root


def _suggestions_diff(run_id: str) -> dict:
    """The run's `suggestions.diff` artifact entry, or exit 1 saying so."""
    entry = artifact(run_root(run_id), "suggestions")
    if entry is None or not entry["exists"]:
        present = [e["path"] for e in artifact_entries(run_root(run_id))]
        die(1, f"run {run_id} has no artifacts/{artifact_relpath('suggestions')}"
               f" ({RUN_DOCUMENT_ABSENT}; on disk: "
               f"{', '.join(present) if present else 'nothing'})")
    return entry


def cmd_suggestions(args):
    """Show -- and, on an explicit act, apply -- a run's reflection diff
    (task 041, requirement P).

    `show` (the default) prints the diff and the exact list of files it would
    touch, and writes nothing at all. `apply --yes` prints the same two things
    *first* and then writes, all-or-nothing. A diff that does not fit the target
    tree is refused with the file and the `@@` header of every hunk that failed,
    exit 1 -- from `show` too, so the check is scriptable without a write path.

    Applying edits the target *tree*, never a live run: a job runs the prompts
    baked into its image, so the diff is picked up by the next `start`, which
    builds a new image because the content hash moved.

    Two more refusals, both from requirement P part 2 (task 029), both reported
    by `show` and only refused for `apply`, because `show` writes nothing:
    a tree a run is executing under right now is never written
    (`SUGGESTIONS_MIDFLIGHT_REFUSED`), and two runs whose diffs both apply to
    the same file are refused with both named rather than merged into a version
    neither proposed (`SUGGESTIONS_CONFLICT_REFUSED`, overridable by an explicit
    `--ignore-conflicts`).

    `--only PATH` (repeatable, requirement P part 3, task 030) takes a subset:
    the named files are selected out of the parsed diff *before* the plan is
    built, so everything downstream -- the plan, both guards, the writer's
    all-or-nothing rule -- sees only what was selected, and the files left out
    are listed under `SUGGESTIONS_ONLY_HEADER`. A name that matches no path in
    the diff is a usage error (exit 2), never a quiet apply of the rest.
    """
    _require_run(args.run_id)
    entry = _suggestions_diff(args.run_id)
    root = _suggestions_target(args)
    text = (run_root(args.run_id) / "artifacts"
            / artifact_relpath("suggestions")).read_text()
    try:
        patches = patch.parse_patch(text)
    except patch.PatchError as e:
        die(1, f"unreadable diff: {e}")
    only = list(getattr(args, "only", None) or [])
    selected, unknown = patch.select_patches(patches, only)
    if unknown:
        # Usage error, before anything is shown or planned: the operator asked
        # for something this diff does not contain, and the answer is the list
        # of what it does contain -- not a plan of whatever else was named.
        die(2, f"{SUGGESTIONS_ONLY_UNKNOWN}: {', '.join(unknown)} "
               f"(this diff touches: "
               f"{', '.join(p.path for p in patches)}) "
               f"-- {SUGGESTIONS_ONLY_WORKFLOW}")
    chosen = {p.path for p in selected}
    left_out = [p for p in patches if p.path not in chosen]
    plan = patch.plan_patch(root, selected)
    before = image.hash_image_inputs(root)
    apply_it = args.action == "apply" and args.yes
    asking_to_write = args.action == "apply"
    midflight = _suggestions_midflight(root, before.hash)
    conflicts = _suggestions_conflicts(root, args.run_id, plan)

    lines = [f"run:       {args.run_id}",
             f"diff:      artifacts/{entry['path']} "
             f"({entry['bytes']} bytes)",
             f"target:    {root}",
             f"image:     {before.hash}"]
    if only:
        # Printed in the header block, not buried below the plan: a partial
        # apply has to announce itself as one wherever its output is read.
        lines.append(f"selected:  {len(selected)} of {len(patches)} files "
                     f"(--only)")
    lines += [SUGGESTIONS_DIFF_HEADER,
              text if text.endswith("\n") else text + "\n",
              SUGGESTIONS_PLAN_HEADER,
              *patch.format_plan(plan)]
    if left_out:
        lines += [SUGGESTIONS_ONLY_HEADER,
                  *(f"  skip  {p.path}  "
                    f"({patch.hunk_count_label(len(p.hunks))})"
                    for p in left_out)]
    # Both conditions are *shown* beside the plan whatever the action is: an
    # operator running `show` on a host that also hosts runs needs to know the
    # apply would be refused before typing it, and the refusal message below is
    # then the same information on stderr.
    if midflight:
        lines += [SUGGESTIONS_MIDFLIGHT_HEADER,
                  *(f"run {r['runId']} ({r['state']}): {reason}"
                    for r in midflight for reason in r["reasons"])]
    if conflicts:
        lines += [SUGGESTIONS_CONFLICT_HEADER,
                  *(f"run {c['runId']}: {c['diff']} also rewrites "
                    f"{', '.join(c['paths'])}" for c in conflicts)]
    body = "\n".join(lines)
    payload = {"runId": args.run_id, "diff": f"artifacts/{entry['path']}",
               "target": str(root), "paths": list(plan.paths),
               "applies": plan.ok,
               "failures": [str(f) for f in plan.failures],
               "imageHash": before.hash, "written": [], "applied": False,
               "only": only, "skipped": [p.path for p in left_out],
               "liveRuns": midflight, "conflicts": conflicts}

    if not plan.ok:
        # Shown in full first (the operator needs the diff to understand the
        # rejection), then refused on stderr with every failing hunk named.
        out(args, payload, body)
        die(1, SUGGESTIONS_REFUSED + ": "
               + "; ".join(str(f) for f in plan.failures))
    if asking_to_write and midflight:
        # Shown first, like every other outcome, then refused: exit 1 (a
        # refusal), not 2 (a usage error) -- the command was well formed and the
        # answer is "not while that run is running".
        out(args, payload, body)
        die(1, f"{SUGGESTIONS_MIDFLIGHT_REFUSED}: "
               + "; ".join(f"run {r['runId']}: " + " and ".join(r["reasons"])
                           for r in midflight)
               + f" -- {SUGGESTIONS_MIDFLIGHT_WORKFLOW}")
    if asking_to_write and conflicts and not args.ignore_conflicts:
        out(args, payload, body)
        die(1, f"{SUGGESTIONS_CONFLICT_REFUSED}: "
               + "; ".join(f"run {c['runId']}'s {c['diff']} also rewrites "
                           f"{', '.join(c['paths'])}" for c in conflicts)
               + f" -- {SUGGESTIONS_CONFLICT_WORKFLOW}")
    if not apply_it:
        asked = args.action == "apply"
        if asked:
            payload["reason"] = SUGGESTIONS_NEEDS_YES
        out(args, payload,
            body + (f"\n{SUGGESTIONS_NEEDS_YES}" if asked else ""))
        if asked:
            # An `apply` that wrote nothing is not a success: the operator
            # asked for a write and did not get one. Exit 2 (usage), after
            # having shown exactly what the `--yes` would have done.
            sys.exit(2)
        return
    # Shown first, written second: this ORDER is the verb's safety property,
    # not a layout choice, so the diff and the plan go out (and are flushed)
    # before the writer is called. `--json` is a single document by
    # construction, so its plan necessarily lands after the write -- the way to
    # inspect a plan beforehand there is `show` / `apply` without `--yes`.
    if not args.json:
        print(body, flush=True)
    written = patch.apply_plan(plan)
    after = image.hash_image_inputs(root)
    payload.update(written=list(written), applied=True,
                   imageHash=after.hash, imageHashBefore=before.hash)
    if args.json:
        print(json.dumps(payload, indent=2))
        return
    print("\n".join([SUGGESTIONS_WROTE_HEADER,
                     *(f"wrote   {p}" for p in written),
                     f"image:     {before.hash} -> {after.hash}  "
                     "(the next `start` builds and runs it)"]))


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
                "base_image": None, "dockerfile": None,
                "default_llm_profile": None, "network": None,
                "auto_resume": ("true", "false"),
                "price_strategy": PRICE_STRATEGIES,
                # Task 043 (#52): `model`/`thinking` are free-form
                # provider/pi strings, like `image`. Task 006 (#60 pillar 4
                # cleanup) retired the other two of the original four keys
                # (`fast_model`, `model_strategy`) -- see
                # `_RETIRED_CONFIG_KEYS` below, checked first so both name
                # the replacement instead of the generic unknown-key message.
                "model": None, "thinking": None}
# keys stored as real booleans in `<registry>/config.yaml` (task 026)
_CONFIG_BOOL_KEYS = {"auto_resume"}

_RETIRED_CONFIG_KEYS = {
    "model_strategy": (
        "model_strategy is removed (task 006): per-hat models are now a "
        "job.yaml models: (or model_overrides:) table naming any of "
        "planning, worker, verify, review, reflect, router -- set that key "
        "directly in job.yaml or a --template's job.yaml."),
    "fast_model": (
        "fast_model is removed (task 006): per-hat models are now a "
        "job.yaml models: (or model_overrides:) table naming any of "
        "planning, worker, verify, review, reflect, router -- set that key "
        "directly in job.yaml or a --template's job.yaml."),
}


def cmd_config(args):
    """Registry-wide defaults at `<registry>/config.yaml` (PRD req 25):
    `image`, `base_image`, `dockerfile` (the three image supply points, ranked
    as one unit by `_resolve_image_supply`), `on_complete`,
    `default_llm_profile`, `network`, `auto_resume`, `price_strategy`,
    `model`, `thinking`. `start`
    layers these in between a `--template`'s value and the hardcoded fallback
    (see `_apply_template`); `doctor` reads `default_llm_profile` directly."""
    if args.key in _RETIRED_CONFIG_KEYS:
        die(2, _RETIRED_CONFIG_KEYS[args.key])
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

    Task 015 (#46) narrows the first of those refusals to what it always
    meant: `is_operator_termination` (not "a marker exists"), so a
    *self-inflicted* termination -- a signal that reached the engine with no
    operator abort behind it, e.g. the agent killing its own supervisor -- is
    resumed like any other crashed container instead of being stranded
    forever. It is not a separate bucket: from here on it IS an ordinary
    zombie, and the crash-loop guard below is what stops it looping.

    Task 021 (#31) does not change a line here, and that is the point: this
    sweep acts on whatever `_dangling_run_entry` matches, so widening that
    condition to the exited-but-present container made an engine that died
    inside its container auto-resume-eligible for free. `cmd_resume` already
    removes an exited container occupying the name before it starts a fresh
    one, so the resume path needed nothing either.

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
        if is_operator_termination(term):
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
            # no longer a zombie: it finished on its own, or a container is
            # running for it again (a slow `resume`, an operator who got there
            # first). THE condition, re-asked -- never a second
            # implementation of it.
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
            run_id=run_id, iterations=None, approaches=None, timeout=None,
            iteration_timeout=None, image=args.image, port=None,
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


# Where the reference `doctor` reports on came from -- the same ranking `start`
# uses to *select* an image (task 035's levels), said in words an operator can
# match to their own configuration. `doctor` cannot know a per-job
# `--dockerfile`/`base_image`, so it never claims to: it reports the host-wide
# answer and says which level produced it.
IMAGE_IN_USE_FLAG = "the --image flag"
IMAGE_IN_USE_ENV = "RALPHD_IMAGE"
IMAGE_IN_USE_REGISTRY = "the registry's config.yaml (image:)"
IMAGE_IN_USE_SOURCE = "this source tree's image inputs"
IMAGE_IN_USE_DERIVED = "the registry's config.yaml ({key}:)"
IMAGE_IN_USE_FALLBACK = "the built-in fallback"
# The registry configures a *base*, so the job image is a derived tag that only
# exists once it has been derived: doctor names the base rather than inventing
# a tag, and reports the default tag it can still speak for.
IMAGE_DERIVED_SUPPLY_NOTE = (
    "the job image is derived from {base} ({where}), so which tag runs is only "
    "settled when `ralphctl start` derives it; this source tree's own default "
    "job image is {expected}")
IMAGE_STALE_RUNS_HEADING = (
    "runs recorded non-terminal whose own job image is not this source tree's:")


def _doctor_image_reference(args, reg_cfg: dict) -> tuple[str | None, str, str | None]:
    """`(reference, where it came from, base it is derived from)` for `doctor`.

    Ranked exactly like `start`'s image *selection*: an explicit `--image`,
    then the ambient `RALPHD_IMAGE`, then the registry's `image:` pin, then --
    when nothing pins anything -- the content-hashed default this source tree
    builds, and finally the legacy fallback for an install with no source tree.

    A registry that supplies `base_image:`/`dockerfile:` instead of `image:`
    yields `(None, where, base)`: the job image is a *derived* tag, and naming
    one here would mean deriving it (a second resolver, and a build). Absence
    is an answer -- the caller reports the base instead of guessing a tag.
    """
    pinned = getattr(args, "image", None)
    if pinned:
        return pinned, IMAGE_IN_USE_FLAG, None
    if IMAGE_ENV:
        return IMAGE_ENV, IMAGE_IN_USE_ENV, None
    for key in ("image", "base_image", "dockerfile"):
        val = reg_cfg.get(key)
        if isinstance(val, str) and val.strip():
            if key == "image":
                return val.strip(), IMAGE_IN_USE_REGISTRY, None
            return (None, IMAGE_IN_USE_DERIVED.format(key=key), val.strip())
    _, source_hash = current_source_hash()
    if source_hash:
        return image.image_tag(source_hash), IMAGE_IN_USE_SOURCE, None
    return DEFAULT_IMAGE, IMAGE_IN_USE_FALLBACK, None


def _doctor_image_staleness(args, reg_cfg: dict,
                            source_hash: str | None) -> tuple[dict, bool]:
    """`doctor`'s job-image verdict, and whether the check passes (task 037).

    The check passes when the job image is **available**: present on this
    daemon, or something ralphd can produce from this source tree. That is a
    deliberate change from the pre-v0.6 "present" check, which failed a fresh
    checkout for an image `start` would have built anyway -- and, in the other
    direction, passed for a pinned reference that is years older than the
    source. Staleness is reported separately and is *report-only*: running an
    old engine on purpose is a supported thing to do (`--image`), being unable
    to tell is not.
    """
    ref, where, base = _doctor_image_reference(args, reg_cfg)
    expected = image.image_tag(source_hash) if source_hash else None
    if ref is None:
        # a derived supply: no single tag to inspect, so nothing is claimed
        # about presence -- ralphd can produce it iff there is source to layer.
        verdict = image_staleness(None, source_hash=source_hash)
        verdict["note"] = IMAGE_DERIVED_SUPPLY_NOTE.format(
            base=base, where=where,
            expected=expected or IMAGE_NO_SOURCE_EXPECTED)
        verdict["imageBase"] = base
        verdict["where"] = where
        return verdict, source_hash is not None
    present = image_exists(ref)
    verdict = image_staleness(ref, source_hash=source_hash, present=present)
    verdict["imageBase"] = None
    verdict["where"] = where
    return verdict, bool(present or (expected and ref == expected))


def _run_image_staleness(source_hash: str | None) -> list[dict]:
    """The job-image verdict for every run recorded non-terminal (task 037).

    The literal reading of "the tag in use": a live run's image is the one its
    own `host.json` records (task 036), not whatever the host would resolve
    today -- and it is the case fact 1 of the PRD is about, a run executing an
    engine that predates the fix it is watching for.

    Hash comparison only: no `docker image inspect` per run, so this costs one
    file read each and never claims an image is gone. Terminal runs are left
    out -- an image that was current while the run happened is not "stale"
    afterwards, and the run is over either way.
    """
    runs_dir = registry() / "runs"
    if not runs_dir.is_dir():
        return []
    entries = []
    for d in sorted(runs_dir.iterdir()):
        status = _read_json(d / "status.json", {})
        if not isinstance(status, dict) or status.get("state") not in NONTERMINAL_STATES:
            continue
        rec = image_record(d)
        entries.append({"runId": d.name,
                        **image_staleness(rec["image"], source_hash=source_hash,
                                          rec=rec)})
    return entries


def cmd_doctor(args):
    checks = {}
    # Task 005 (A.3): report-only, before the ordinary checks -- absent
    # entirely when --prd is omitted or unreadable (the criterion only
    # covers "a --prd file it can read"); resolved against the current
    # directory, mirroring `start`'s own workspace-root resolution above.
    if getattr(args, "prd", None):
        try:
            prd_text = Path(args.prd).read_text()
        except OSError:
            prd_text = None
        if prd_text is not None:
            _print_rubric_status(prd_text, Path.cwd())
    checks["docker"] = sh([DOCKER, "version", "--format", "{{.Server.Version}}"]) \
        .returncode == 0
    reg = registry()
    reg.mkdir(parents=True, exist_ok=True)
    reg_cfg = _registry_config(reg)
    # Requirement H4: source hash vs the tag in use, not mere existence.
    inputs = image.find_inputs()
    src_root, source_hash = current_source_hash()
    image_verdict, image_ok = _doctor_image_staleness(args, reg_cfg, source_hash)
    checks["image"] = image_ok
    run_images = _run_image_staleness(source_hash)
    stale_runs = [e for e in run_images
                  if e["staleness"] == IMAGE_STALENESS_STALE]
    checks["registry"] = os.access(reg, os.W_OK)
    checks["pi_host_config"] = (Path.home() / ".pi" / "agent" / "settings.json").exists()
    # Task 023, requirement R: report-only, like image staleness -- an
    # operator who never installed the skill is not broken, so this never
    # feeds `checks`/`ok`.
    operator_skill = _operator_skill_status(Path.home())

    default_profile_name = reg_cfg.get("default_llm_profile", "host")
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
    image_verdict["sourceRoot"] = str(src_root) if src_root else None
    # Task 038: *where* those inputs came from -- a checkout, this install's own
    # package data (a wheel/pipx install), or nowhere. Reported as a field, and
    # as a line below for the packaged case, so the packaging fallback is
    # observable rather than a silent difference in what gets built.
    image_verdict["inputs"] = inputs.kind
    # host-network jobs share the host's network namespace, so docker's
    # normal port-publish isolation (`-p host:container`) doesn't apply --
    # the API binds `--api-bind` directly on the host. Report-only, never
    # affects the verdict (mirrors strays/dangling below).
    configured_network = reg_cfg.get("network")
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
    # The staleness line is report-only, so it is marked `!` exactly when it is
    # news the operator has to act on (a stale or unrunnable image) and left
    # unmarked when it is not (fresh, or honestly unknowable).
    stale_mark = image_verdict["staleness"] in (IMAGE_STALENESS_STALE,
                                                IMAGE_STALENESS_MISSING)
    report += (f"\n{'!' if stale_mark else ' '} job image "
               f"({image_verdict['staleness']}, from "
               f"{image_verdict['where']}): {image_verdict['note']}")
    if inputs.kind == image.INPUTS_PACKAGED:
        report += "\n  " + IMAGE_PACKAGED_INPUTS_NOTICE.format(
            where=inputs.packaged)
    # Task 023, requirement R: installed / missing / stale, driven by the
    # install stamp's recorded version -- marked `!` only for `stale`
    # (something to act on), same rule the job-image line above uses.
    skill_line = f"operator skill: {operator_skill['status']}"
    if operator_skill["status"] == OPERATOR_SKILL_STALE:
        skill_line += f" (built from {operator_skill['installedVersion']})"
    report += (f"\n{'!' if operator_skill['status'] == OPERATOR_SKILL_STALE else ' '} "
               f"{skill_line}")
    if stale_runs:
        report += f"\n! {IMAGE_STALE_RUNS_HEADING}"
        for e in stale_runs:
            report += f"\n    {e['runId']}  {e['note']}"
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
        report += "\n! registry entries recorded running with no live container:"
        for d in dangling:
            report += (f"\n    {d['runId']}  container={d['container']} "
                       f"({d.get('liveness')})")
            if auto_resume is not None and d["runId"] in auto_resume["resumed"]:
                # already restarted by this very sweep -- printing the manual
                # remedy here would be stale advice
                report += (f"\n      {_dangling_phrase(d, 'why')}; "
                           f"auto-resumed (auto_resume enabled)")
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
                                   "a container is running for it again)")
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
                # #31: and the same per-liveness explanation of what happened
                # to the container, so doctor and repair cannot tell an
                # operator two different things about one run.
                report += (f"\n      {_dangling_phrase(d, 'why')}; "
                           f"{_dangling_remedy(d['runId'])}")
    if auto_resume is not None:
        report += (f"\n! --fix: auto-resumed {len(auto_resume['resumed'])}, "
                   f"left {len(auto_resume['skipped'])} opted out, "
                   f"{len(auto_resume['operatorTerminated'])} operator-"
                   f"terminated, "
                   f"{len(auto_resume['waiting'])} in crash-loop backoff, "
                   f"{len(auto_resume['gaveUp'])} given up on, "
                   f"{len(auto_resume['failed'])} failed")
    # Task 020 (E.4): named unconditionally (registry-wide, not tied to any
    # one run) so an operator reading `doctor` alone still learns the
    # mount and its fallback -- `status` (per run) is where whether a
    # SPECIFIC run's key is actually THERE is answered
    # (`runSecretsMount.keyAvailable`).
    report += (
        f"\n  run-secrets: `start`/`resume` bind a mode-0700 host "
        f"directory `<config>/<run>/run-secrets/` at "
        f"`{petitions.DEFAULT_RUN_SECRETS_DIR}` for the petition-"
        f"adjudication MAC key (E.4); with no key persisted there, the "
        f"`events.jsonl` `petition_adjudications_recorded` MAC witness is "
        f"the documented fallback authentication check.")
    out(args, {"ok": ok, "checks": checks, "strayContainers": strays,
               "imageStaleness": image_verdict, "runImageStaleness": run_images,
               "danglingRegistryEntries": dangling, "registryIssues": registry_issues,
               "defaultLlmProfile": default_profile_name,
               "defaultLlmProfileError": default_llm_profile_error,
               "hostNetworkApiBindNote": host_network_note,
               "operatorSkill": operator_skill,
               "runSecretsMountTarget": str(petitions.DEFAULT_RUN_SECRETS_DIR),
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
    whose status.json records a non-terminal state while **no container is
    running** for it -- either none exists at all (crashed/removed outside
    ralphctl, e.g. `docker rm -f` by hand) or one exists that has exited.
    The reverse of `_stray_sibling_containers`.

    Task 021 (#31) added the second half. Before it, the condition read `if
    _container_running(name) is not None: return None`, i.e. "a container
    record exists, so this run is fine" -- which is precisely what an engine
    death leaves behind, so an exited-but-present container was reported as
    healthy by all four callers (`cmd_status`, `_diagnose_dangling_container`,
    `cmd_repair` and `_auto_resume_dangling`). Liveness comes from docker's
    three states (`_container_liveness`), never from a field the dead engine
    wrote; and the fix lives HERE rather than in the four callers, so they
    cannot drift apart about what a zombie is.

    Returns `{runId, container, liveness}` -- `liveness` is
    `CONTAINER_ABSENT` or `CONTAINER_EXITED`, which is what lets each surface
    word the two shapes differently without re-asking docker -- or None (not
    dangling: the recorded state is terminal, or a container is running)."""
    status = _read_json(run_root(run_id) / "status.json", {})
    if status.get("state") not in _NONTERMINAL_STATUS_STATES:
        return None
    name = job_container_name(run_id)
    liveness = _container_liveness(name)
    if liveness == CONTAINER_RUNNING:
        return None
    return {"runId": run_id, "container": name, "liveness": liveness}

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


# #182: on some argparse patch levels (the operator measured Python 3.12.3;
# this container runs 3.12.14, where the bug does not reproduce -- every
# form below already parses correctly here, so the nodes this fix targets
# pass VACUOUSLY in this container and are not the evidence the fix works)
# an optional positional (`nargs="?"`) that follows a flag on the same
# command line is dropped or misrouted instead of being assigned, and the
# leftover token either surfaces as argparse's own generic "unrecognized
# arguments" error or silently vanishes. `nargs="*"` was tried by the
# operator and does not fix it. `_LEFTOVER_POSITIONAL_FIELDS` names, for each
# affected subcommand, the ordered `nargs="?"` destinations argparse itself
# still declares (so `--help`/docs/cli.md are untouched) that a leftover
# token may be folded into when `main()`'s own `parse_known_args()` sees
# argparse leave it unconsumed.
_LEFTOVER_POSITIONAL_FIELDS: dict[str, tuple[str, ...]] = {
    "amend": ("task_id", "text"),
    "steer": ("message",),
}


def _place_leftover_positional(command: str, leftovers: list[str],
                               namespace: argparse.Namespace,
                               positional_fields: tuple[str, ...]) -> None:
    """Fold ONE leftover token `main()`'s `parser.parse_known_args()` did
    not consume into the first still-empty `nargs="?"` positional of
    `command`, independent of whatever argparse version produced that
    leftover (#182).

    This is the one place that decides where such a token goes -- called
    from `main()` with whatever `parse_known_args()` left over, and called
    directly by tests with a hand-built `namespace`/`leftovers` pair so the
    placement logic is exercised without depending on any particular
    argparse behaviour (never by shelling out to the CLI, never relying on
    the ambient interpreter's own argparse quirks).

    Raises the CLI's own clear error (via `die`, exit 2) -- naming the
    subcommand and the leftover tokens -- when there is more than one
    leftover, or when every named positional field is already filled and
    there is nowhere left to put the one leftover token.
    """
    if not leftovers:
        return
    if len(leftovers) > 1:
        die(2, f"{command}: too many extra arguments "
               f"({', '.join(repr(t) for t in leftovers)}); only one "
               "positional slot is still open")
    unfilled = [f for f in positional_fields
                if getattr(namespace, f, None) is None]
    if not unfilled:
        die(2, f"{command}: extra argument {leftovers[0]!r} does not fit "
               "any positional slot (all are already filled)")
    setattr(namespace, unfilled[0], leftovers[0])


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


def build_parser() -> argparse.ArgumentParser:
    """The whole CLI surface, as one inspectable object.

    Split out of `main()` so the flag set is readable *without* running the
    CLI: `tests/test_docs_consistency.py` walks this parser tree (including
    every sub-subparser) to check that every flag docs/cli.md documents really
    exists. Parsing `--help` text instead would only see what argparse chose
    to print.
    """
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
    # Task 017 (D.3, #117 side finding): the opt-in peer gates
    # (`JobConfig.peer_plan_review`/`peer_verdict_gate`, both OFF by
    # default) become launchable from the CLI, not just hand-edited into
    # job.yaml -- same `action="store_true", default=None` shape as
    # `--vigilant`/`--reflect` above so an unset flag stays out of job.yaml
    # entirely (the `if v is not None` filter at the write below) and the
    # engine's own `JobConfig` default applies. `resume` has no equivalent
    # flag for the same reason `--vigilant`/`--reflect` don't: neither is a
    # live-override knob on an existing job.yaml, only a `start`-time
    # choice that then persists via the value `resume` replays unchanged.
    s.add_argument("--peer-plan-review", action="store_true", default=None,
                   help="opt-in peer plan-review gate (PRD requirement K, "
                        "#101): a peer model approves or objects to the "
                        "plan before work starts")
    s.add_argument("--peer-verdict-gate", action="store_true", default=None,
                   help="opt-in, genuinely blind peer verdict gate (PRD "
                        "requirement L, #101): a second peer model "
                        "re-verifies completed work before terminal success")
    s.add_argument("--model", help="pi model ref, e.g. provider/model-id")
    s.add_argument("--model-for", action="append", metavar="HAT=REF",
                   help="per-hat model override (repeatable), e.g. "
                        "--model-for worker=amazon-bedrock/eu.anthropic.claude-sonnet-5 "
                        f"(known hats: {', '.join(MODEL_HATS)}); merges into the "
                        "job's models: table over the --template's and the "
                        "registry's own models:/model_overrides: key -- see docs/cli.md")
    s.add_argument("--model-for-tier", action="append", metavar="HAT:TIER=REF",
                   help="per-hat-per-tier model override (repeatable), e.g. "
                        "--model-for-tier worker:hard=amazon-bedrock/eu.anthropic.claude-opus-5 "
                        f"(known hats: {', '.join(MODEL_HATS)}; known tiers: "
                        f"{', '.join(TIERS)}); merges into the job's model_tiers: "
                        "table over the --template's and the registry's own "
                        "model_tiers: key -- see docs/cli.md")
    # Task 006 (#60 pillar 4 cleanup): `--fast-model`/`--model-strategy` are
    # RETIRED -- the phase->tier strategy table they drove (`STRATEGY_TIERS`)
    # is gone (task 005's per-hat `models:`/`model_overrides:` job.yaml table
    # replaced it). Kept as accepted-but-suppressed flags ONLY so `cmd_start`
    # can refuse them with a message naming the replacement (see below),
    # rather than argparse's generic "unrecognized arguments" or -- worse --
    # silently accepting and doing nothing with them.
    s.add_argument("--fast-model", default=None, help=argparse.SUPPRESS)
    s.add_argument("--model-strategy", default=None, help=argparse.SUPPRESS)
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
    s.add_argument("--base-image", default=None, metavar="REF",
                   help="build the job image ON TOP of this image (#20 H2): it "
                        "only has to carry the toolchain your repo needs -- "
                        "ralphd layers the engine and pi onto it and runs the "
                        "derived, hash-tagged image. Mutually exclusive with "
                        "--image, which pins a finished image and runs it as-is")
    s.add_argument("--dockerfile", default=None, metavar="PATH",
                   help="build THIS Dockerfile (with its own directory as the "
                        "build context) into that base image (#20 H3), instead "
                        "of naming a base that already exists; persisted into "
                        "the run's job.yaml and replayed by resume")
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
        if name == "tasks":
            # Task 019 (#24): read an archived approach's OWN tasks.json
            # instead of the live plan -- `--approach N` picks one, `--all`
            # lists every archived approach with its own counts. Mutually
            # exclusive: each names a different document to read.
            g = s.add_mutually_exclusive_group()
            g.add_argument("--approach", type=int, default=None,
                           help="print approaches/NN/tasks.json's own task "
                                "table instead of the live plan")
            g.add_argument("--all", action="store_true",
                           help="list every archived approach with its own "
                                "completed/total counts")
        s.set_defaults(func=fn)

    s = sub.add_parser("resume", help="start a fresh container over an "
                       "existing run dir (crash recovery / budget top-up)")
    s.add_argument("run_id")
    s.add_argument("--iterations", metavar="+N",
                   help="budget top-up, e.g. +10 (adds to the existing "
                        "budget); a bare integer sets it absolutely")
    s.add_argument("--approaches", metavar="+N|N", default=None,
                   help="max-approaches top-up (+N) or absolute (N), "
                        "written to job.yaml's `max_approaches` next to "
                        "--iterations (#94 (c)); with no flag a resume "
                        "REVERTS a live `budget --approaches` change back "
                        "to job.yaml's own value (#94 (d), docs/cli.md's "
                        "budget table)")
    s.add_argument("--timeout", metavar="+N|N", default=None,
                   help="job wall-clock timeout top-up/absolute in "
                        "seconds, written to job.yaml's `job_timeout_s` "
                        "next to --iterations (#94 (c)); with no flag a "
                        "resume REVERTS a live `budget --timeout` change "
                        "back to job.yaml's own value (#94 (d), "
                        "docs/cli.md's budget table)")
    s.add_argument("--iteration-timeout", metavar="+N|N", default=None,
                   help="per-iteration timeout top-up/absolute in "
                        "seconds, written to job.yaml's "
                        "`iteration_timeout_s` next to --iterations "
                        "(#94 (c)); with no flag a resume REVERTS a live "
                        "`budget --iteration-timeout` change back to "
                        "job.yaml's own value (#94 (d), docs/cli.md's "
                        "budget table)")
    s.add_argument("--image", default=None,
                   help="pin the image this resume runs; defaults to replaying "
                        "the image recipe the run started with (its "
                        "base_image/dockerfile), or "
                        f"{DEFAULT_IMAGE} for a run that recorded none")
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

    s = sub.add_parser("skills", help="inspect/hot-swap a running job's skills, "
                       "or install the shipped operator skill locally")
    # `run_id` is optional here (unlike every other run-scoped verb) so that
    # `ralphctl skills install` -- the one action with no run -- and
    # `ralphctl skills <run-id> ls|get|add|rm` can share this one parser:
    # argparse resolves a bare `install` to the required subparsers action
    # with run_id left None, and `<run-id> ls` to the other order, on its
    # own (task 022, requirement R).
    s.add_argument("run_id", nargs="?")
    sksub = s.add_subparsers(dest="action", required=True)
    sksub.add_parser("ls", help="list skills with origin")
    g = sksub.add_parser("get", help="download a skill directory")
    g.add_argument("name")
    g.add_argument("dest")
    a = sksub.add_parser("add", help="tar + upload a skill directory")
    a.add_argument("dir")
    r = sksub.add_parser("rm", help="delete a skill")
    r.add_argument("name")
    inst = sksub.add_parser("install", help="install the shipped operator "
                            "skill to a local agent skills dir (no run id)")
    inst.add_argument("--target", metavar="DIR",
                      help="override auto-detection of ~/.claude/skills or "
                           "~/.pi/agent/skills")
    inst.add_argument("--force", action="store_true",
                      help="overwrite even if the target was locally modified")
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

    s = sub.add_parser("amend", help="record an operator ruling review must "
                       "honour (waiver/replacement of a task's criteria), or "
                       "--list what's recorded (#26)")
    s.add_argument("run_id")
    s.add_argument("task_id", nargs="?", help="the task id the ruling applies to")
    s.add_argument("text", nargs="?", help="the ruling text; or --file, or stdin")
    s.add_argument("--file")
    s.add_argument("--waive", action="store_true",
                   help="a full waiver (the task's successCriteria is "
                        "satisfied by operator decision) rather than a "
                        "replacement of its criteria text")
    s.add_argument("--status", metavar="STATUS",
                   help="record a status ruling instead (must be one of "
                        "engine.transitions.TASK_STATUSES); the trailing "
                        "text/--file/stdin argument becomes an optional "
                        "reason, never required")
    s.add_argument("--list", action="store_true",
                   help="list this run's recorded operator rulings instead "
                        "of recording one; always reads the run's /config "
                        "directory directly, container or no container")
    s.set_defaults(func=cmd_amend)

    s = sub.add_parser("retry", help="wake a degraded run's infra backoff "
                       "wait immediately (resets the outage-budget clock)")
    s.add_argument("run_id")
    s.set_defaults(func=cmd_retry)

    s = sub.add_parser("budget", help="change a running job's iteration "
                       "budget (and, task 004/#94 (c), its approaches/"
                       "timeout knobs) in flight (+N tops up, N sets "
                       "absolutely)")
    s.add_argument("run_id")
    s.add_argument("iterations", metavar="+N|N", nargs="?", default=None,
                   help="budget top-up (+10) or absolute new budget (40); "
                        "persists across a resume -- "
                        "`resume --iterations +N` only to add more. May be "
                        "omitted if one of --approaches/--timeout/"
                        "--iteration-timeout is given instead (any non-empty "
                        "subset of the four is one request)")
    s.add_argument("--approaches", metavar="+N|N", default=None,
                   help="max-approaches budget top-up/absolute (#94 (c)); "
                        "process-only -- reverts on a plain `resume` "
                        "unless that resume itself passes `--approaches` "
                        "(docs/cli.md's budget table)")
    s.add_argument("--timeout", metavar="+N|N", default=None,
                   help="job wall-clock deadline top-up in seconds (+N) or "
                        "an absolute Unix epoch second (#94 (c)); "
                        "process-only -- reverts on a plain `resume` "
                        "unless that resume itself passes `--timeout` "
                        "(docs/cli.md's budget table)")
    s.add_argument("--iteration-timeout", metavar="+N|N", default=None,
                   help="per-iteration timeout top-up/absolute in seconds "
                        "(#94 (c)); process-only -- reverts on a plain "
                        "`resume` unless that resume itself passes "
                        "`--iteration-timeout` (docs/cli.md's budget table)")
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

    s = sub.add_parser("router", help="this run's router calls: which "
                       "legal edge the router picked for an ambiguous "
                       "state, and its own cost/tokens -- never a work "
                       "iteration")
    s.add_argument("run_id")
    s.add_argument("action", choices=["ls", "show"], nargs="?", default="ls")
    s.add_argument("index", nargs="?", metavar="N",
                   help="with `show`: the router call's index (see `ls`)")
    s.set_defaults(func=cmd_router)

    s = sub.add_parser("suggestions", help="show a run's reflection diff and "
                       "what it would touch, and apply it to a checkout on "
                       "an explicit --yes")
    s.add_argument("run_id")
    s.add_argument("action", choices=["show", "apply"], nargs="?",
                   default="show")
    s.add_argument("--target", default=None, metavar="DIR",
                   help="the tree to plan/apply against (default: the ralphd "
                        "checkout this ralphctl runs out of, whose content "
                        "hash the next `start` builds from)")
    s.add_argument("--yes", action="store_true",
                   help="with `apply`: the explicit act that writes; without "
                        "it `apply` shows the plan and exits 2 having written "
                        "nothing")
    s.add_argument("--only", action="append", default=None, metavar="PATH",
                   help="take just this file out of the diff (repeatable; "
                        "selected before the plan is built, so what is "
                        "selected is planned, shown and written "
                        "all-or-nothing). A path the diff does not touch is a "
                        "usage error, and hunks are not separately selectable")
    s.add_argument("--ignore-conflicts", action="store_true",
                   help="with `apply --yes`: write even though another run's "
                        "suggestions.diff also applies to one of these files "
                        "(that diff is then re-planned against what was "
                        "written, and refused if it no longer fits). Never "
                        "overrides the live-run refusal")
    s.set_defaults(func=cmd_suggestions)

    s = sub.add_parser("doctor", help="preflight checks")
    s.add_argument("--prd", default=None, metavar="PATH",
                   help="also print which materiality rubric this PRD file "
                        "resolves to (task 005, A.3): inline, by reference, "
                        "or the engine default; warns on a referenced rubric "
                        "path that does not resolve, relative to the current "
                        "directory")
    s.add_argument("--image", default=None, metavar="REF",
                   help="report on this image reference instead of the one "
                        "this host would run (RALPHD_IMAGE, the registry's "
                        "image:, or the content-hashed default this source "
                        "tree builds); with --fix it also pins the image "
                        "auto-resumed runs restart on, which by default is "
                        "the one each run recorded at start time")
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

    return p


def main() -> None:
    parser = build_parser()
    args, extras = parser.parse_known_args(_preprocess_logs_argv(sys.argv[1:]))
    if extras:
        # #182: an affected subcommand gets one chance to fold a leftover
        # token back into its own still-unfilled nargs="?" positional
        # (_place_leftover_positional) instead of failing here; every other
        # command gets argparse's own generic error, unchanged.
        fields = _LEFTOVER_POSITIONAL_FIELDS.get(args.command)
        if fields is None:
            parser.error("unrecognized arguments: %s" % " ".join(extras))
        _place_leftover_positional(args.command, extras, args, fields)
    args.func(args)


if __name__ == "__main__":
    main()
