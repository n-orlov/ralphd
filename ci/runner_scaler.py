#!/usr/bin/env python3
"""Elastic CI runner scaler: one ephemeral runner container per queued job.

Stdlib only, run as ``python3 ci/runner_scaler.py <command>``. It is not part
of the installed ``ralphd`` package. Commands:

* ``once``    one pass: list queued runs and their jobs, start a container for
              each queued job that carries the ``ci-pool`` label.
* ``run``     the same pass in a loop, one pass every ``poll_interval_s``.
* ``status``  print the scaler's containers (name, job id, age) as a JSON list.

``--dry-run`` (on ``once`` and ``run``) prints the starts a pass would make and
issues none. The scaler serves only jobs whose labels include ``ci-pool``.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
import urllib.error
import urllib.request
from calendar import timegm
from pathlib import Path
from typing import Any, Callable

POOL_LABEL = "ci-pool"
CONTAINER_PREFIX = "ci-runner-"
RUNNER_NAME_PREFIX = "ci-"
RUNNER_GROUP_ID = 1
DEFAULT_CONFIG_PATH = "~/.config/ralphd-ci/scaler.yaml"
API_TIMEOUT_S = 30

DEFAULTS: dict[str, Any] = {
    "api_url": "https://api.github.com",
    "poll_interval_s": 5,
    "max_concurrency": 4,
    "start_deadline_s": 120,
    "job_deadline_s": 7800,
    "image": "ralphd-ci-runner:latest",
    "docker": "docker",
    "host_cpus": 12,
    "profiles": {},
}
REQUIRED = ("repo", "token_file")
INT_KEYS = ("poll_interval_s", "max_concurrency", "start_deadline_s",
            "job_deadline_s", "host_cpus")


class ScalerError(Exception):
    """A configuration, API or docker failure the CLI reports and exits 1/2 on."""


class ConfigError(ScalerError):
    """The config file is unreadable or invalid."""


# --------------------------------------------------------------------------
# Config: a YAML subset (stdlib only, no yaml import).
#
# Supported: ``key: value`` scalars (strings, optionally quoted; integers;
# true/false), ``#`` comments, blank lines, and nested maps by indentation
# (used for ``profiles:``). Lists, anchors, flow style and multi-line scalars
# are not supported and are rejected with a line-numbered error.
# --------------------------------------------------------------------------

def _scalar(text: str, lineno: int) -> Any:
    text = text.strip()
    if text == "":
        return ""
    if text[0] in "\"'":
        quote = text[0]
        end = text.find(quote, 1)
        if end < 0:
            raise ConfigError(f"line {lineno}: unterminated quoted string")
        rest = text[end + 1:].strip()
        if rest and not rest.startswith("#"):
            raise ConfigError(f"line {lineno}: text after a quoted string")
        return text[1:end]
    text = re.sub(r"\s+#.*$", "", text)
    if text[0] in "[{&*|>!":
        raise ConfigError(f"line {lineno}: only plain scalars and nested maps "
                          "are supported")
    low = text.lower()
    if low in ("true", "false"):
        return low == "true"
    if re.fullmatch(r"-?\d+", text):
        return int(text)
    if re.fullmatch(r"-?\d+\.\d+", text):
        return float(text)
    return text


def parse_config_text(text: str) -> dict[str, Any]:
    """Parse the YAML subset into a (possibly nested) dict."""
    root: dict[str, Any] = {}
    stack: list[tuple[int, dict[str, Any]]] = [(-1, root)]
    for lineno, raw in enumerate(text.splitlines(), 1):
        if "\t" in raw[:len(raw) - len(raw.lstrip())]:
            raise ConfigError(f"line {lineno}: tabs are not allowed in indentation")
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        indent = len(raw) - len(raw.lstrip(" "))
        match = re.match(r"([A-Za-z0-9_.\-]+)\s*:(?:\s+(.*)|)$", stripped)
        if not match:
            raise ConfigError(f"line {lineno}: expected 'key: value'")
        key, value = match.group(1), match.group(2)
        while stack[-1][0] >= indent:
            stack.pop()
        parent = stack[-1][1]
        if key in parent:
            raise ConfigError(f"line {lineno}: duplicate key {key!r}")
        if value is None or value.strip() == "" or value.strip().startswith("#"):
            child: dict[str, Any] = {}
            parent[key] = child
            stack.append((indent, child))
        else:
            parent[key] = _scalar(value, lineno)
    return root


def load_config(path: str | Path) -> dict[str, Any]:
    """Read, parse and validate the scaler config at ``path``."""
    path = Path(path).expanduser()
    try:
        text = path.read_text()
    except OSError as exc:
        raise ConfigError(f"cannot read config {path}: {exc.strerror or exc}")
    cfg = parse_config_text(text)
    for key in cfg:
        if key == "token" or key.endswith("_token"):
            raise ConfigError(
                f"config key {key!r} holds an inline token; name a file in "
                "'token_file' instead")
    unknown = sorted(set(cfg) - set(DEFAULTS) - set(REQUIRED))
    if unknown:
        raise ConfigError(f"unknown config key(s): {', '.join(unknown)}")
    for key in REQUIRED:
        if not cfg.get(key) or isinstance(cfg.get(key), dict):
            raise ConfigError(f"config key {key!r} is required")
    merged = {**DEFAULTS, **cfg}
    if not re.fullmatch(r"[\w.\-]+/[\w.\-]+", str(merged["repo"])):
        raise ConfigError("config key 'repo' must look like 'owner/name'")
    for key in INT_KEYS:
        value = merged[key]
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise ConfigError(f"config key {key!r} must be a positive integer")
    if not isinstance(merged["profiles"], dict):
        raise ConfigError("config key 'profiles' must be a map")
    merged["token_file"] = str(Path(str(merged["token_file"])).expanduser())
    return merged


# --------------------------------------------------------------------------
# GitHub API (polling, never webhooks)
# --------------------------------------------------------------------------

class GitHubApi:
    def __init__(self, cfg: dict[str, Any]) -> None:
        self.base = str(cfg["api_url"]).rstrip("/")
        self.repo = str(cfg["repo"])
        self.token_file = str(cfg["token_file"])

    def _token(self) -> str:
        try:
            return Path(self.token_file).read_text().strip()
        except OSError as exc:
            raise ScalerError(f"cannot read token file: {exc.strerror or exc}")

    def request(self, method: str, path: str, body: Any = None) -> Any:
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(
            self.base + path, data=data, method=method, headers={
                "Authorization": f"Bearer {self._token()}",
                "Accept": "application/vnd.github+json",
                "Content-Type": "application/json",
                "User-Agent": "ralphd-ci-scaler",
            })
        try:
            with urllib.request.urlopen(req, timeout=API_TIMEOUT_S) as resp:
                payload = resp.read()
        except urllib.error.HTTPError as exc:
            raise ScalerError(f"GitHub API {method} {path}: HTTP {exc.code}")
        except (urllib.error.URLError, OSError) as exc:
            raise ScalerError(f"GitHub API {method} {path}: {exc}")
        return json.loads(payload) if payload else None

    def get(self, path: str) -> Any:
        return self.request("GET", path)

    def generate_jitconfig(self, job: dict[str, Any]) -> str:
        """Request a just-in-time runner config for one job (Rule 3).

        ``POST /repos/{repo}/actions/runners/generate-jitconfig`` registers a
        single-use runner named ``ci-<job id>`` carrying the job's own labels in
        runner group 1; the scaler never registers a standing runner. Returns
        the ``encoded_jit_config`` value, which is a secret.
        """
        reply = self.request(
            "POST", f"/repos/{self.repo}/actions/runners/generate-jitconfig",
            {"name": runner_name(int(job["id"])),
             "labels": list(job.get("labels") or []),
             "runner_group_id": RUNNER_GROUP_ID})
        encoded = (reply or {}).get("encoded_jit_config")
        if not isinstance(encoded, str) or not encoded:
            raise ScalerError("generate-jitconfig reply has no encoded_jit_config")
        return encoded

    def job_status(self, job_id: int) -> str:
        """The job's current ``status`` (``GET /repos/{repo}/actions/jobs/{id}``)."""
        reply = self.get(f"/repos/{self.repo}/actions/jobs/{int(job_id)}")
        return str((reply or {}).get("status", ""))

    def queued_runs(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        page = 1
        while True:
            reply = self.get(f"/repos/{self.repo}/actions/runs"
                             f"?status=queued&per_page=100&page={page}")
            runs = (reply or {}).get("workflow_runs", [])
            out.extend(runs)
            if len(runs) < 100:
                return out
            page += 1

    def run_jobs(self, run_id: int) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        page = 1
        while True:
            reply = self.get(f"/repos/{self.repo}/actions/runs/{run_id}/jobs"
                             f"?filter=latest&per_page=100&page={page}")
            jobs = (reply or {}).get("jobs", [])
            out.extend(jobs)
            if len(jobs) < 100:
                return out
            page += 1


# --------------------------------------------------------------------------
# Docker (the host CLI; tests put a fake first on PATH)
# --------------------------------------------------------------------------

class Docker:
    def __init__(self, cfg: dict[str, Any]) -> None:
        self.binary = str(cfg["docker"])

    def run(self, *args: str) -> str:
        import subprocess
        try:
            proc = subprocess.run([self.binary, *args], capture_output=True,
                                  text=True, timeout=120)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ScalerError(f"docker {args[0] if args else ''}: {exc}")
        if proc.returncode != 0:
            raise ScalerError(f"docker {args[0] if args else ''} failed: "
                              f"{proc.stderr.strip() or proc.stdout.strip()}")
        return proc.stdout

    def containers(self) -> list[dict[str, Any]]:
        """The scaler's own containers, filtered client-side by name prefix."""
        out = self.run("ps", "-a", "--no-trunc", "--format", "{{json .}}")
        found = []
        for line in out.splitlines():
            if not line.strip():
                continue
            entry = json.loads(line)
            if str(entry.get("Names", "")).startswith(CONTAINER_PREFIX):
                found.append(entry)
        return found

    def start(self, name: str, image: str) -> None:
        self.run("run", "-d", "--name", name, image)

    def remove(self, name: str) -> None:
        """Remove ONE container by its exact name; never by filter or prune."""
        if job_id_of(name) is None:
            raise ScalerError(f"refusing to remove {name!r}: not a scaler container")
        self.run("rm", "--force", name)


_CREATED_RE = re.compile(
    r"(\d{4})-(\d\d)-(\d\d) (\d\d):(\d\d):(\d\d)(?:\.\d+)? ([+-])(\d\d)(\d\d)")


def parse_created_at(text: str) -> float | None:
    """Epoch seconds from docker's ``CreatedAt`` ('2026-10-10 12:00:00 +0000 UTC')."""
    match = _CREATED_RE.match(str(text))
    if not match:
        return None
    y, mo, d, h, mi, s = (int(match.group(i)) for i in range(1, 7))
    offset = (int(match.group(8)) * 3600 + int(match.group(9)) * 60)
    if match.group(7) == "-":
        offset = -offset
    return float(timegm((y, mo, d, h, mi, s, 0, 0, 0)) - offset)


def container_name(job_id: int) -> str:
    return f"{CONTAINER_PREFIX}{job_id}"


def runner_name(job_id: int) -> str:
    """The GitHub-side name of the job's single-use runner."""
    return f"{RUNNER_NAME_PREFIX}{job_id}"


def job_id_of(name: str) -> int | None:
    tail = name[len(CONTAINER_PREFIX):]
    return int(tail) if name.startswith(CONTAINER_PREFIX) and tail.isdigit() else None


# --------------------------------------------------------------------------
# The pass
# --------------------------------------------------------------------------

def log(message: str) -> None:
    print(f"scaler: {message}", file=sys.stderr, flush=True)


def queued_pool_jobs(api: GitHubApi) -> list[dict[str, Any]]:
    """Queued jobs whose labels include the ``ci-pool`` label, in job-id order."""
    jobs = []
    for run in api.queued_runs():
        for job in api.run_jobs(int(run["id"])):
            if job.get("status") == "queued" and POOL_LABEL in (job.get("labels") or []):
                jobs.append(job)
    return sorted(jobs, key=lambda job: int(job["id"]))


def live_containers(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The scaler's containers that still occupy a slot (not exited or dead)."""
    return [e for e in entries if str(e.get("State", "")) not in ("exited", "dead")]


def reap_containers(cfg: dict[str, Any], api: GitHubApi, docker: Docker,
                    entries: list[dict[str, Any]], *, now: float,
                    dry_run: bool = False,
                    out: Callable[[str], None] = print) -> set[str]:
    """Remove, by exact name, the containers that are done or overdue.

    A container goes when (a) it is older than ``job_deadline_s``, (b) its job
    is ``completed``, or (c) its job is still ``queued`` after
    ``start_deadline_s`` (GitHub assigned the job to the runner and it never
    ran). Each removal is one ``docker rm --force ci-runner-<job id>``. A job
    lookup that fails leaves its container for the next pass. Returns the names
    removed (or, in a dry run, that would be).
    """
    reaped: set[str] = set()
    for entry in sorted(entries, key=lambda e: str(e["Names"])):
        name = str(entry["Names"])
        job_id = job_id_of(name)
        if job_id is None:
            continue
        created = parse_created_at(str(entry.get("CreatedAt", "")))
        age = None if created is None else now - created
        reason = None
        if age is not None and age > float(cfg["job_deadline_s"]):
            reason = "past job_deadline_s"
        else:
            try:
                status = api.job_status(job_id)
            except ScalerError as exc:
                log(f"cannot read job {job_id}, keeping {name}: {exc}")
                continue
            if status == "completed":
                reason = "job completed"
            elif (status == "queued" and age is not None
                  and age > float(cfg["start_deadline_s"])):
                reason = "job still queued past start_deadline_s"
        if reason is None:
            continue
        if dry_run:
            out(f"would remove {name}: {reason}")
        else:
            try:
                docker.remove(name)
            except ScalerError as exc:
                log(f"cannot remove {name}: {exc}")
                continue
            log(f"removed {name}: {reason}")
        reaped.add(name)
    return reaped


def run_pass(cfg: dict[str, Any], api: GitHubApi, docker: Docker, *,
             dry_run: bool = False, out: Callable[[str], None] = print,
             clock: Callable[[], float] = time.time) -> int:
    """One scaling pass. Returns the number of containers started (or that
    ``dry_run`` would have started).

    Queued ``ci-pool`` jobs are served in ascending job-id order. A job that
    already has a ``ci-runner-<job id>`` container (in any state) is skipped,
    so repeated passes never start a second container for it. Starting stops
    once the containers still alive reach ``max_concurrency``. Before each
    start the pass requests that job's JIT runner config; a failed request
    skips the job (no container, no slot used) and the next pass retries it.

    A pass first reaps (see ``reap_containers``). A job whose container was
    reaped is not restarted in the same pass: the next pass starts its new
    container.
    """
    jobs = queued_pool_jobs(api)
    existing = docker.containers()
    reaped = reap_containers(cfg, api, docker, existing, now=clock(),
                             dry_run=dry_run, out=out)
    names = {str(e["Names"]) for e in existing}
    free = int(cfg["max_concurrency"]) - len(
        live_containers([e for e in existing if str(e["Names"]) not in reaped]))
    started = 0
    for job in jobs:
        name = container_name(int(job["id"]))
        if name in names:
            continue
        if free <= 0:
            break
        if dry_run:
            out(f"would start {name} for job {job['id']}")
        else:
            try:
                api.generate_jitconfig(job)
            except ScalerError as exc:
                # No container for this job; the next pass requests again.
                log(f"no runner config for job {job['id']}: {exc}")
                continue
            docker.start(name, str(cfg["image"]))
            log(f"started {name}")
        names.add(name)
        free -= 1
        started += 1
    return started


def run_loop(cfg: dict[str, Any], api: GitHubApi, docker: Docker, *,
             dry_run: bool = False, passes: int | None = None,
             sleep: Callable[[float], None] = time.sleep) -> None:
    done = 0
    while passes is None or done < passes:
        try:
            run_pass(cfg, api, docker, dry_run=dry_run)
        except ScalerError as exc:
            log(f"pass failed: {exc}")
        done += 1
        if passes is None or done < passes:
            sleep(float(cfg["poll_interval_s"]))


def status_entries(docker: Docker, now: float | None = None) -> list[dict[str, Any]]:
    now = time.time() if now is None else now
    entries = []
    for entry in docker.containers():
        name = str(entry["Names"])
        created = parse_created_at(str(entry.get("CreatedAt", "")))
        entries.append({
            "container": name,
            "job_id": job_id_of(name),
            "age_s": None if created is None else max(0, int(now - created)),
            "state": entry.get("State"),
        })
    return sorted(entries, key=lambda e: (e["job_id"] is None, e["job_id"] or 0,
                                          e["container"]))


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="runner_scaler.py",
        description="Start one ephemeral runner container per queued "
                    f"'{POOL_LABEL}' job.")
    parser.add_argument("--config", default=DEFAULT_CONFIG_PATH,
                        help=f"scaler config (default {DEFAULT_CONFIG_PATH})")
    sub = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")
    for name, helptext in (("once", "run one pass and exit"),
                           ("run", "run passes in a loop every poll_interval_s")):
        cmd = sub.add_parser(name, help=helptext, description=helptext)
        cmd.add_argument("--dry-run", action="store_true",
                         help="print the starts a pass would make; issue none")
        if name == "run":
            cmd.add_argument("--passes", type=int, default=None,
                             help="stop after this many passes (default: forever)")
    sub.add_parser("status", help="print the scaler's containers as JSON",
                   description="print the scaler's containers, their job ids "
                               "and ages as a JSON list")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        cfg = load_config(args.config)
        docker = Docker(cfg)
        if args.command == "status":
            print(json.dumps(status_entries(docker), indent=2))
            return 0
        api = GitHubApi(cfg)
        if args.command == "once":
            run_pass(cfg, api, docker, dry_run=args.dry_run)
            return 0
        if args.command == "run":
            try:
                run_loop(cfg, api, docker, dry_run=args.dry_run, passes=args.passes)
            except KeyboardInterrupt:
                pass
            return 0
    except ConfigError as exc:
        print(f"scaler: config error: {exc}", file=sys.stderr)
        return 2
    except ScalerError as exc:
        print(f"scaler: {exc}", file=sys.stderr)
        return 1
    return 2  # unreachable: argparse requires a known command


if __name__ == "__main__":
    sys.exit(main())
