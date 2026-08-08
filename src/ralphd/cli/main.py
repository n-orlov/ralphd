"""ralphctl — operate ralphd job containers. See docs/cli.md.

Deliberately stdlib-only (argparse + urllib) so `pipx install ralphd` is light.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import secrets
import shutil
import socket
import stat as stat_mod
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

from .. import __version__

DOCKER = os.environ.get("RALPHD_DOCKER", "docker")
DEFAULT_IMAGE = os.environ.get("RALPHD_IMAGE", "ralphd:dev")

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
        raw: bool = False, timeout: int = 30):
    meta = host_meta(run_id)
    if not meta.get("apiUrl"):
        die(4, f"no API endpoint recorded for run {run_id}")
    req = urllib.request.Request(meta["apiUrl"] + path, method=method)
    token_file = run_root(run_id) / ".api-token"
    if token_file.exists():
        req.add_header("Authorization", f"Bearer {token_file.read_text().strip()}")
    if body is not None:
        req.add_header("Content-Type", "application/json")
        req.data = json.dumps(body).encode()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = resp.read()
            return data.decode() if raw else (json.loads(data) if data else {})
    except urllib.error.HTTPError as e:
        detail = e.read().decode()[:500]
        die(5 if e.code == 409 else 1, f"API {e.code}: {detail}")
    except (urllib.error.URLError, TimeoutError) as e:
        die(4, f"API unreachable: {e}")


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


# ---------------------------------------------------------------- start
def cmd_start(args):
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

    job = {
        "run_id": run_id,
        "iterations": args.iterations,
        "max_approaches": args.max_approaches,
        "vigilant": args.vigilant,
        "on_complete": args.on_complete,
        "model": args.model,
        "fast_model": args.fast_model,
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
    ws: Path | None = None
    if args.workspace:
        ws = Path(args.workspace).expanduser().resolve()
        if not ws.is_dir():
            die(2, f"workspace {ws} is not a directory")
        mounts += ["-v", f"{ws}:/workspace"]

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
        env_args += ["-e", f"RALPHD_HOST_RUN_DIR={rdir}",
                     "-e", f"RALPHD_RUN_ID={run_id}"]
        print("ralphctl: WARNING: --allow-docker mounts the host docker socket "
              "into the job container. The docker socket is ROOT-EQUIVALENT "
              "access to this host — the job can mount any host path and run "
              "privileged containers. Only use with PRDs you trust.",
              file=sys.stderr)
    for sdir in args.skills or []:
        (cdir / "skills").mkdir(exist_ok=True)
        src = Path(sdir).expanduser().resolve()
        shutil.copytree(src, cdir / "skills" / src.name, dirs_exist_ok=True)

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
        for var in HOST_LLM_ENV:
            if os.environ.get(var):
                env_args += ["-e", f"{var}={os.environ[var]}"]
        aws = Path.home() / ".aws"
        if aws.is_dir():
            mounts += ["-v", f"{aws}:/home/agent/.aws:ro"]
    elif args.llm != "none":
        die(2, f"unknown LLM profile '{args.llm}' (v0.1 supports: host, none)")
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
    (rdir / "host.json").write_text(json.dumps(meta, indent=2))
    out(args, {**meta, "authenticated": bool(token)},
        f"{run_id}\n  container: {container[:12]}\n  api: {meta['apiUrl']}")
    if not args.detach:
        _follow_events(args, run_id)
        status = api(run_id, "GET", "/status")
        sys.exit(0 if status.get("verdict") == "verified" else 1)


def _follow_events(args, run_id: str):
    meta = host_meta(run_id)
    url = meta["apiUrl"] + "/events?since=0"
    req = urllib.request.Request(url)
    token_file = run_root(run_id) / ".api-token"
    if token_file.exists():
        req.add_header("Authorization", f"Bearer {token_file.read_text().strip()}")
    for attempt in range(30):
        try:
            with urllib.request.urlopen(req, timeout=3600) as resp:
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
            time.sleep(1 + attempt * 0.5)
    die(4, "could not connect to event stream")


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
    out(args, status,
        f"run:       {status.get('runId')}\n"
        f"state:     {status.get('state')}  (live api: {live})\n"
        f"verdict:   {status.get('verdict')}\n"
        f"phase:     {status.get('phase')}  approach {status.get('approach')}\n"
        f"iteration: {status.get('iterationsUsed')}/{status.get('iterationsBudget')}\n"
        f"tasks:     {json.dumps(status.get('tasks', {}))}\n"
        f"usage:     {json.dumps(status.get('usage', {}))}")


def cmd_tasks(args):
    tasks = api(args.run_id, "GET", "/tasks")
    if args.json:
        print(json.dumps(tasks, indent=2))
        return
    for t in tasks.get("tasks", []):
        print(f"[{t.get('status'):<17}] {t.get('id')} {t.get('title')}")


def cmd_logs(args):
    n = args.iteration
    if n is None:
        status = api(args.run_id, "GET", "/status")
        cur = status.get("currentIteration") or {}
        n = cur.get("number") or status.get("iterationsUsed") or 1
    qs = []
    if args.tail:
        qs.append(f"tail={args.tail}")
    if args.follow:
        qs.append("follow=true")
    text = api(args.run_id, "GET",
               f"/iterations/{n}/output" + ("?" + "&".join(qs) if qs else ""),
               raw=True, timeout=3600 if args.follow else 30)
    print(text)


def cmd_watch(args):
    _follow_events(args, args.run_id)


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


def cmd_resume(args):
    out(args, api(args.run_id, "POST", "/resume"), "resumed")


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


def cmd_doctor(args):
    checks = {}
    checks["docker"] = sh([DOCKER, "version", "--format", "{{.Server.Version}}"]) \
        .returncode == 0
    checks["image"] = sh([DOCKER, "image", "inspect", args.image]).returncode == 0
    reg = registry()
    reg.mkdir(parents=True, exist_ok=True)
    checks["registry"] = os.access(reg, os.W_OK)
    checks["pi_host_config"] = (Path.home() / ".pi" / "agent" / "settings.json").exists()
    strays = _stray_sibling_containers()
    ok = all(checks.values())  # strays are report-only, never affect the verdict
    report = "\n".join(f"{'✓' if v else '✗'} {k}" for k, v in checks.items())
    if strays:
        report += "\n! stray ralphd.run containers (no matching run dir):"
        for s in strays:
            report += f"\n    {s['id'][:12]}  ralphd.run={s['runId']}"
        report += "\n  clean up with: docker rm -f <id>"
    out(args, {"ok": ok, "checks": checks, "strayContainers": strays}, report)
    sys.exit(0 if ok else 1)


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


# ---------------------------------------------------------------- parser
def main() -> None:
    p = argparse.ArgumentParser(prog="ralphctl",
                                description="Operate ralphd autonomous coding jobs")
    p.add_argument("--version", action="version", version=__version__)
    p.add_argument("--json", action="store_true", help="machine-readable output")
    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser("start", help="launch a job container")
    s.add_argument("--prd", required=True, help="PRD markdown file, or - for stdin")
    s.add_argument("--workspace", help="host dir to mount at /workspace")
    s.add_argument("--run-id")
    s.add_argument("--iterations", type=int, default=25)
    s.add_argument("--max-approaches", type=int, default=3)
    s.add_argument("--vigilant", action="store_true")
    s.add_argument("--model", help="pi model ref, e.g. provider/model-id")
    s.add_argument("--fast-model")
    s.add_argument("--model-strategy", default="quality-first",
                   choices=["quality-first", "cost-optimized", "balanced"])
    s.add_argument("--thinking", help="pi thinking level")
    s.add_argument("--llm", default="host", help="LLM profile (v0.1: host|none)")
    s.add_argument("--llm-env", action="append", metavar="KEY=VAL")
    s.add_argument("--forward-env", action="append", metavar="NAME|PREFIX_*",
                   help="forward host env var(s) into the container (repeatable)")
    s.add_argument("--env", action="append", metavar="KEY=VAL")
    s.add_argument("--skills", action="append", metavar="DIR")
    s.add_argument("--allow-docker", action="store_true",
                   help="mount the host docker socket into the job container "
                        "(ROOT-EQUIVALENT host access — trusted PRDs only)")
    s.add_argument("--image", default=DEFAULT_IMAGE)
    s.add_argument("--on-complete", default="idle", choices=["idle", "exit"])
    s.add_argument("--timeout", type=int, default=480, metavar="MINUTES")
    s.add_argument("--iteration-timeout", type=int, default=45, metavar="MINUTES")
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
        ("pause", cmd_pause, None), ("resume", cmd_resume, None),
    ]:
        s = sub.add_parser(name)
        s.add_argument("run_id")
        s.set_defaults(func=fn)

    s = sub.add_parser("logs", help="agent transcript")
    s.add_argument("run_id")
    s.add_argument("--iteration", type=int)
    s.add_argument("--tail", type=int)
    s.add_argument("--follow", action="store_true")
    s.set_defaults(func=cmd_logs)

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

    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
