"""ralphctl — operate ralphd job containers. See docs/cli.md.

Deliberately stdlib-only (argparse + urllib) so `pipx install ralphd` is light.
"""

from __future__ import annotations

import argparse
import atexit
import contextlib
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
import threading
import time
import tty as tty_module
import urllib.error
import urllib.request
from pathlib import Path
from typing import Self

import yaml

from .. import __version__
from ..engine.state import CURRENT_SCHEMA_VERSION, elapsed_seconds, format_duration
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


def host_meta(run_id: str) -> dict:
    return _read_json(run_root(run_id) / "host.json", {})


def _read_json(path: Path, default=None):
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


def _reap_siblings(run_id: str) -> None:
    """Best-effort removal of containers labeled ralphd.run=<run_id>.

    Sibling containers started by the job via the host docker socket carry
    this label (the job container always does too). Never fails the caller.
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
    "image": DEFAULT_IMAGE,
}

# For these `start` scalar fields, `ralphctl config set <regkey> ...` (task
# 038) supplies a registry-wide fallback that sits BETWEEN a template's
# value and the hardcoded fallback above: explicit CLI flag > template >
# `<registry>/config.yaml` > hardcoded default. `llm`'s registry key is
# named `default_llm_profile` (matches doctor's existing read of it);
# the others share their field name.
_REGISTRY_CONFIG_FIELD_KEYS = {"image": "image", "on_complete": "on_complete",
                               "llm": "default_llm_profile"}


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

    docker_args: list[str] = ["--label", f"ralphd.run={run_id}"]
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
    for pattern in args.forward_env or []:
        if pattern.endswith("*"):
            names = [k for k in os.environ if k.startswith(pattern[:-1])]
        else:
            names = [pattern] if os.environ.get(pattern) else []
            if not names:
                print(f"ralphctl: warning: --forward-env {pattern} not set on host",
                      file=sys.stderr)
        for name in names:
            env_args += ["-e", f"{name}={os.environ[name]}"]
    for kv in args.llm_env or []:
        env_args += ["-e", kv]
    for kv in args.env or []:
        env_args += ["-e", kv]
    if token:
        env_args += ["-e", f"RALPHD_API_TOKEN={token}"]

    cmd = [DOCKER, "run", "-d", "--name", f"ralphd-{run_id}",
           "--init",
           "-p", f"{args.api_bind}:{port}:7777",
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
                    if ev["type"] == "state" and ev.get("state") in (
                            "succeeded", "failed", "aborted"):
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
    name = f"ralphd-{run_id}"

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

    docker_args = ["--label", f"ralphd.run={run_id}"]
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

    cmd = [DOCKER, "run", "-d", "--name", name, "--init",
           "-p", f"{args.api_bind}:{port}:7777",
           *docker_args, *mounts, *env_args, args.image]
    res = sh(cmd)
    if res.returncode != 0:
        die(1, f"docker run failed: {res.stderr.strip()}")
    container = res.stdout.strip()
    meta = {"runId": run_id, "container": container, "port": port,
            "apiUrl": f"http://{args.api_bind}:{port}",
            "image": args.image,
            "startedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
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
def cmd_runs(args):
    rows = []
    runs_dir = registry() / "runs"
    if runs_dir.is_dir():
        for d in sorted(runs_dir.iterdir()):
            status = _read_json(d / "status.json", {})
            if args.state and status.get("state") != args.state:
                continue
            rows.append({"runId": d.name, "state": status.get("state"),
                         "verdict": status.get("verdict"),
                         "phase": status.get("phase"),
                         "iterations": f"{status.get('iterationsUsed', 0)}"
                                       f"/{status.get('iterationsBudget', '?')}",
                         "startedAt": status.get("startedAt")})
    if args.json:
        print(json.dumps(rows, indent=2))
    else:
        fmt = "{runId:<24} {state:<10} {verdict:<10} {phase:<9} {iterations:<7} {startedAt}"
        print(fmt.format(runId="RUN", state="STATE", verdict="VERDICT",
                         phase="PHASE", iterations="ITER", startedAt="STARTED"))
        for r in rows:
            print(fmt.format(**{k: str(v) for k, v in r.items()}))


def cmd_status(args):
    live = True
    try:
        status = api(args.run_id, "GET", "/status")
    except SystemExit:
        live = False
        status = _read_json(run_root(args.run_id) / "status.json")
        if status is None:
            die(3, f"run {args.run_id} not found")
    status["live"] = live

    # Duration fields (PRD steering 051): a single `durationSeconds` covers
    # both "elapsed so far" (state still running, no endedAt yet) and "total
    # run time" (terminal state, endedAt set) -- same measurement, the only
    # thing that changes is whether the end bound is `endedAt` or now.
    # Additive: existing timestamp fields (startedAt/endedAt/...) are left
    # untouched, this only adds new numeric-seconds fields alongside them.
    duration_s = elapsed_seconds(status.get("startedAt"), status.get("endedAt"))
    status["durationSeconds"] = duration_s
    cur_it = status.get("currentIteration")
    it_duration_s = None
    if isinstance(cur_it, dict):
        it_duration_s = elapsed_seconds(cur_it.get("startedAt"))
        cur_it["elapsedSeconds"] = it_duration_s

    duration_label = "total" if status.get("endedAt") else "elapsed"
    lines = [
        f"run:       {status.get('runId')}",
        f"state:     {status.get('state')}  (live api: {live})",
        f"verdict:   {status.get('verdict')}",
        f"duration:  {format_duration(duration_s)}  ({duration_label})",
        f"phase:     {status.get('phase')}  approach {status.get('approach')}",
        f"iteration: {status.get('iterationsUsed')}/{status.get('iterationsBudget')}",
    ]
    if isinstance(cur_it, dict):
        lines.append(f"iteration elapsed: {format_duration(it_duration_s)} "
                     f"(iteration {cur_it.get('number')}, phase={cur_it.get('phase')})")
    lines.append(f"tasks:     {json.dumps(status.get('tasks', {}))}")
    lines.append(f"usage:     {json.dumps(status.get('usage', {}))}")
    # Task 006: a terminal run (failed/aborted/succeeded) that still has
    # unconsumed steering files is a silent-drop hazard -- a terminal run
    # never reads pending steering again, so this is the operator's only
    # remaining chance to notice and act (e.g. re-steer a resumed run).
    # Surfaced loudly (not buried in --json) rather than left to be spotted
    # by combing steering/.consumed.json by hand.
    unconsumed = status.get("unconsumedSteering") or []
    if unconsumed:
        tty = sys.stdout.isatty()
        names = ", ".join(unconsumed)
        lines.append(_ansi(tty, "1;31",
                            f"!! UNCONSUMED STEERING: {names} "
                            "(run ended without acting on this steering)"))
    out(args, status, "\n".join(lines))


def cmd_tasks(args):
    tasks = api(args.run_id, "GET", "/tasks")
    if args.json:
        print(json.dumps(tasks, indent=2))
        return
    for t in tasks.get("tasks", []):
        print(f"[{t.get('status'):<17}] {t.get('id')} {t.get('title')}")


def cmd_logs(args):
    tty = sys.stdout.isatty()
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
        text = api(args.run_id, "GET", path, raw=True, timeout=30)
        sys.stdout.write(text)
        if text and not text.endswith("\n"):
            print()
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
        full_text = api(args.run_id, "GET", base_path, raw=True, timeout=30)
        lines = _render_to_lines(full_text, tty, _new_render_state())
        if tail:
            lines = lines[-tail:]
        for line in lines:
            print(line)
        return

    try:
        with _TerminalModeGuard():
            _stream_logs_pretty_tailed(args, base_path, tty, tail)
    except KeyboardInterrupt:
        sys.exit(_SIGINT_EXIT_CODE)


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
def cmd_steer(args):
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


def cmd_abort(args):
    out(args, api(args.run_id, "POST", "/abort", {"reason": args.reason or ""}),
        "aborting")


def cmd_stop(args):
    status = _read_json(run_root(args.run_id) / "status.json", {})
    if status.get("state") not in ("succeeded", "failed", "aborted"):
        if not args.force:
            die(5, "job still running — use `abort` first or `stop --force`")
        try:
            api(args.run_id, "POST", "/abort", {"reason": "stop --force"})
            time.sleep(2)
        except SystemExit:
            pass
    try:
        api(args.run_id, "POST", "/shutdown")
    except SystemExit:
        pass
    name = f"ralphd-{args.run_id}"
    time.sleep(1)
    sh([DOCKER, "rm", "-f", name])
    _reap_siblings(args.run_id)
    out(args, {"stopped": args.run_id}, f"stopped {args.run_id} (run dir kept)")


def cmd_rm(args):
    name = f"ralphd-{args.run_id}"
    if sh([DOCKER, "inspect", name]).returncode == 0:
        die(5, "container still exists — `stop` first")
    if not run_root(args.run_id).exists():
        die(3, f"run {args.run_id} not found")
    if not args.yes and sys.stdin.isatty():
        reply = input(f"delete all state for {args.run_id}? [y/N] ")
        if reply.lower() != "y":
            sys.exit(1)
    _reap_siblings(args.run_id)
    shutil.rmtree(run_root(args.run_id), ignore_errors=True)
    shutil.rmtree(config_root(args.run_id), ignore_errors=True)
    out(args, {"removed": args.run_id}, f"removed {args.run_id}")


def cmd_artifacts(args):
    adir = run_root(args.run_id) / "artifacts"
    if args.action == "ls":
        files = [{"path": str(p.relative_to(adir)), "size": p.stat().st_size}
                 for p in sorted(adir.rglob("*")) if p.is_file()]
        out(args, files, "\n".join(f"{f['size']:>10}  {f['path']}" for f in files)
            or "(no artifacts)")
    else:
        dest = Path(args.dest)
        dest.mkdir(parents=True, exist_ok=True)
        shutil.copytree(adir, dest, dirs_exist_ok=True)
        out(args, {"pulled": str(dest)}, f"artifacts copied to {dest}")


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
                "default_llm_profile": None}


def cmd_config(args):
    """Registry-wide defaults at `<registry>/config.yaml` (PRD req 25):
    `image`, `on_complete`, `default_llm_profile`. `start` layers these in
    between a `--template`'s value and the hardcoded fallback (see
    `_apply_template`); `doctor` reads `default_llm_profile` directly."""
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
    cfg[args.key] = args.value
    (reg / "config.yaml").write_text(
        yaml.safe_dump(cfg, default_flow_style=False, sort_keys=True))
    out(args, {"key": args.key, "value": args.value},
        f"{args.key}: {args.value}")


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
    # strays/dangling registry entries are report-only, never affect the verdict
    ok = all(checks.values())
    report = "\n".join(f"{'✓' if v else '✗'} {k}" for k, v in checks.items())
    if default_llm_profile_error:
        report += f"\n    default LLM profile ({default_profile_name!r}): {default_llm_profile_error}"
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
        report += "\n  the container likely died/was removed outside ralphctl; " \
                  "try `ralphctl resume <run-id>`"
    out(args, {"ok": ok, "checks": checks, "strayContainers": strays,
               "danglingRegistryEntries": dangling, "registryIssues": registry_issues,
               "defaultLlmProfile": default_profile_name,
               "defaultLlmProfileError": default_llm_profile_error}, report)
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


def _dangling_registry_entries() -> list[dict]:
    """The reverse of `_stray_sibling_containers`: a run dir whose status.json
    says `state: running` but whose container no longer exists at all
    (crashed/removed outside ralphctl, e.g. `docker rm -f` by hand)."""
    runs_dir = registry() / "runs"
    if not runs_dir.is_dir():
        return []
    dangling = []
    for d in sorted(runs_dir.iterdir()):
        status = _read_json(d / "status.json", {})
        if status.get("state") != "running":
            continue
        name = f"ralphd-{d.name}"
        if _container_running(name) is None:
            dangling.append({"runId": d.name, "container": name})
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
    s.add_argument("--port", type=int)
    s.add_argument("--api-bind", default="127.0.0.1")
    s.add_argument("--api-token", help="token value, or 'auto'")
    s.add_argument("--no-detach", dest="detach", action="store_false")
    s.set_defaults(func=cmd_start, detach=True)

    s = sub.add_parser("runs", help="list runs")
    s.add_argument("--state")
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

    s = sub.add_parser("steer", help="send steering guidance")
    s.add_argument("run_id")
    s.add_argument("message", nargs="?")
    s.add_argument("--file")
    s.add_argument("--name")
    s.add_argument("--now", action="store_true",
                   help="also interrupt the current iteration")
    s.set_defaults(func=cmd_steer)

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
    s.set_defaults(func=cmd_rm)

    s = sub.add_parser("artifacts")
    s.add_argument("run_id")
    s.add_argument("action", choices=["ls", "pull"], nargs="?", default="ls")
    s.add_argument("dest", nargs="?", default="./artifacts")
    s.set_defaults(func=cmd_artifacts)

    s = sub.add_parser("doctor", help="preflight checks")
    s.add_argument("--image", default=DEFAULT_IMAGE)
    s.set_defaults(func=cmd_doctor)

    s = sub.add_parser("config", help="registry-wide defaults "
                       "(image, on_complete, default_llm_profile)")
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
