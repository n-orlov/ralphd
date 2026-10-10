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
import fcntl
import hashlib
import http.client
import io
import json
import os
import re
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.request
from calendar import timegm
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Callable

POOL_LABEL = "ci-pool"
CONTAINER_PREFIX = "ci-runner-"
RUNNER_NAME_PREFIX = "ci-"
RUNNER_GROUP_ID = 1
DEFAULT_CONFIG_PATH = "~/.config/ralphd-ci/scaler.yaml"
API_TIMEOUT_S = 30
BACKOFF_CAP_S = 60
RATE_LIMIT_MAX_WAIT_S = 3600
TRUSTED_EVENTS = ("push", "workflow_dispatch", "schedule", "pull_request")

DEFAULTS: dict[str, Any] = {
    "api_url": "https://api.github.com",
    "poll_interval_s": 5,
    "max_concurrency": 4,
    "start_deadline_s": 120,
    "job_deadline_s": 7800,
    "image": "ralphd-ci-runner:latest",
    "runner_dir": str(Path(__file__).resolve().parent / "runner"),
    "lock_file": str(Path(tempfile.gettempdir()) / f"ralphd-ci-image-build-{os.getuid()}.lock"),
    "docker": "docker",
    "host_cpus": 12,
    "profiles": {},
}
# The default profile (B.3). A ``profiles: default:`` entry overrides any field.
DEFAULT_PROFILE: dict[str, Any] = {"cpus": 4, "memory": "12g", "pids_limit": 4096}
PROFILE_KEYS = tuple(DEFAULT_PROFILE)
RESERVED_CORES = 1
REQUIRED = ("repo", "token_file")
INT_KEYS = ("poll_interval_s", "max_concurrency", "start_deadline_s",
            "job_deadline_s", "host_cpus")


class ScalerError(Exception):
    """A configuration, API or docker failure the CLI reports and exits 1/2 on."""


class ConfigError(ScalerError):
    """The config file is unreadable or invalid."""


class ApiError(ScalerError):
    """A failed GitHub API call. ``status`` is the HTTP status, or None when the
    call never got a reply (connection error, timeout, unreadable body).
    ``retry_after`` and ``ratelimit_reset`` carry the reply's rate-limit headers.
    """

    def __init__(self, message: str, status: int | None = None,
                 retry_after: str | None = None,
                 ratelimit_reset: str | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.retry_after = retry_after
        self.ratelimit_reset = ratelimit_reset

    @property
    def retryable(self) -> bool:
        """Outage-shaped: no reply, a 5xx, a 403 or a 429. The scaler backs off."""
        return self.status is None or self.status >= 500 or self.status in (403, 429)


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
    _check_profiles(merged)
    merged["token_file"] = str(Path(str(merged["token_file"])).expanduser())
    return merged


def _check_profiles(cfg: dict[str, Any]) -> None:
    """Validate every profile, then refuse a cap that oversubscribes the host."""
    for label, entry in cfg["profiles"].items():
        if not isinstance(entry, dict):
            raise ConfigError(f"profile {label!r} must be a map of "
                              f"{', '.join(PROFILE_KEYS)}")
        for key, value in entry.items():
            if key not in PROFILE_KEYS:
                raise ConfigError(f"profile {label!r}: unknown key {key!r} "
                                  f"(allowed: {', '.join(PROFILE_KEYS)})")
            if key == "cpus":
                ok = (isinstance(value, (int, float)) and not isinstance(value, bool)
                      and value > 0)
            elif key == "memory":
                ok = isinstance(value, str) and re.fullmatch(
                    r"[1-9]\d*[bkmgBKMG]", value) is not None
            else:
                ok = isinstance(value, int) and not isinstance(value, bool) and value > 0
            if not ok:
                raise ConfigError(f"profile {label!r}: bad value for {key!r}: "
                                  f"{value!r}")
    largest = max(float(profile["cpus"]) for profile in all_profiles(cfg))
    total = int(cfg["max_concurrency"]) * largest
    usable = int(cfg["host_cpus"]) - RESERVED_CORES
    if total > usable:
        raise ConfigError(
            f"max_concurrency {cfg['max_concurrency']} x largest profile "
            f"cpus {largest:g} = {total:g} CPUs exceeds {usable} usable CPUs "
            f"(host_cpus {cfg['host_cpus']} minus {RESERVED_CORES} reserved core)")


def default_profile(cfg: dict[str, Any]) -> dict[str, Any]:
    """The default profile: the built-in one with ``profiles: default:`` on top."""
    return {**DEFAULT_PROFILE, **(cfg["profiles"].get("default") or {})}


def all_profiles(cfg: dict[str, Any]) -> list[dict[str, Any]]:
    """The default profile and every labelled profile, each fully resolved."""
    base = default_profile(cfg)
    return [base] + [{**base, **entry} for label, entry in cfg["profiles"].items()
                     if label != "default"]


def resolve_profile(cfg: dict[str, Any], labels: list[str]) -> dict[str, Any]:
    """The profile for a job: the first of its labels (in the job's own label
    order) that keys a ``profiles`` entry, else the default. A labelled profile
    inherits any field it omits from the default profile."""
    base = default_profile(cfg)
    for label in labels:
        if label != "default" and label in cfg["profiles"]:
            return {**base, **cfg["profiles"][label]}
    return base


# --------------------------------------------------------------------------
# GitHub API (polling, never webhooks)
# --------------------------------------------------------------------------

class GitHubApi:
    def __init__(self, cfg: dict[str, Any]) -> None:
        self.base = str(cfg["api_url"]).rstrip("/")
        self.repo = str(cfg["repo"])
        self.token_file = str(cfg["token_file"])
        # The latest retryable failure; ``run_loop`` clears it before a pass and
        # reads it after, to decide the next wait.
        self.last_failure: ApiError | None = None

    def _token(self) -> str:
        try:
            return Path(self.token_file).read_text().strip()
        except OSError as exc:
            raise ScalerError(f"cannot read token file: {exc.strerror or exc}")

    def request(self, method: str, path: str, body: Any = None) -> Any:
        try:
            return self._request(method, path, body)
        except ApiError as exc:
            if exc.retryable:
                self.last_failure = exc
            raise

    def _request(self, method: str, path: str, body: Any) -> Any:
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
            raise ApiError(f"GitHub API {method} {path}: HTTP {exc.code}",
                           status=exc.code,
                           retry_after=exc.headers.get("Retry-After"),
                           ratelimit_reset=exc.headers.get("x-ratelimit-reset"))
        except (urllib.error.URLError, OSError, http.client.HTTPException) as exc:
            raise ApiError(f"GitHub API {method} {path}: {exc}")
        try:
            return json.loads(payload) if payload else None
        except ValueError as exc:
            raise ApiError(f"GitHub API {method} {path}: unreadable reply: {exc}")

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

# The container's command (B.4): the image's one-shot wrapper, which starts the
# runner once with the JIT config read from this file. There is no registration
# step (no ``config.sh``); the file's delivery into the container is separate.
RUN_ONCE_COMMAND = ("/home/ubuntu/run-once.sh", "/run/ci-runner/jitconfig")
JIT_PARENT_DIR = "/run"  # `docker cp -` extracts the tar below here
JIT_RUNNER_UID = 1000


def jit_tar(jit_config: str) -> bytes:
    """A tar holding ``ci-runner/jitconfig`` (the JIT config), owned by the
    runner's uid 1000, readable by it alone (dir 0500, file 0400)."""
    data = jit_config.encode()
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        for path, mode, size in (("ci-runner", 0o500, 0),
                                 ("ci-runner/jitconfig", 0o400, len(data))):
            info = tarfile.TarInfo(path)
            info.type = tarfile.REGTYPE if path.endswith("jitconfig") else tarfile.DIRTYPE
            info.mode, info.size, info.mtime = mode, size, 0
            info.uid = info.gid = JIT_RUNNER_UID
            tar.addfile(info, io.BytesIO(data) if info.isreg() else None)
    return buf.getvalue()


# Shared caches and the docker socket (B.5). The pip volume is writable and is
# pip's cache dir for the runner's user; Playwright's browsers are the host's
# existing cache, read-only, only for a ``browser-runner`` job; the host docker
# socket is mounted only for a ``docker-host`` job.
PIP_CACHE_VOLUME = "ci-pip-cache"
PIP_CACHE_DIR = "/home/ubuntu/.cache/pip"
PLAYWRIGHT_DIR = "/opt/ms-playwright"
DOCKER_SOCKET = "/var/run/docker.sock"
BROWSER_LABEL = "browser-runner"
DOCKER_LABEL = "docker-host"
BUILD_TIMEOUT_S = 3600


def mounts_for(labels: tuple[str, ...] | list[str]) -> list[str]:
    """The ``-v`` options of a container for a job with ``labels``."""
    mounts = ["-v", f"{PIP_CACHE_VOLUME}:{PIP_CACHE_DIR}"]
    if BROWSER_LABEL in labels:
        mounts += ["-v", f"{PLAYWRIGHT_DIR}:{PLAYWRIGHT_DIR}:ro"]
    if DOCKER_LABEL in labels:
        mounts += ["-v", f"{DOCKER_SOCKET}:{DOCKER_SOCKET}"]
    return mounts


class Docker:
    def __init__(self, cfg: dict[str, Any]) -> None:
        self.binary = str(cfg["docker"])

    def run(self, *args: str, stdin: bytes | None = None,
            timeout: float = 120) -> str:
        import subprocess
        try:
            proc = subprocess.run([self.binary, *args], capture_output=True,
                                  input=stdin,
                                  timeout=timeout)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ScalerError(f"docker {args[0] if args else ''}: {exc}")
        stdout = proc.stdout.decode(errors="replace")
        if proc.returncode != 0:
            stderr = proc.stderr.decode(errors="replace")
            raise ScalerError(f"docker {args[0] if args else ''} failed: "
                              f"{stderr.strip() or stdout.strip()}")
        return stdout

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

    def has_image(self, ref: str) -> bool:
        """Whether the exact ``name:tag`` is on the host."""
        try:
            self.run("image", "inspect", "--format", "{{.Id}}", ref)
        except ScalerError:
            return False
        return True

    def build(self, ref: str, context_dir: str) -> None:
        """``docker build -t <ref> <context_dir>``; a build outlives the 120 s
        every other call gets."""
        self.run("build", "-t", ref, str(context_dir), timeout=BUILD_TIMEOUT_S)

    def ensure_volume(self, name: str) -> None:
        """Create the named volume if the host does not have it (by exact name)."""
        try:
            self.run("volume", "inspect", name)
        except ScalerError:
            self.run("volume", "create", name)

    def start(self, name: str, image: str, jit_config: str,
              profile: dict[str, Any], labels: tuple[str, ...] | list[str] = ()) -> None:
        """Create the container with its limits, deliver the JIT config, start it.

        ``docker create`` carries ``--cpus``, ``--memory`` and ``--pids-limit``
        from the job's profile (B.3). The JIT config is a credential (B.2): it
        reaches the container as the file ``/run/ci-runner/jitconfig`` through a
        tar stream on ``docker cp -`` stdin, so it is in no argv element, no
        ``--env``/``--env-file`` value (which ``docker inspect`` shows), no log
        line and no host-side file. If delivery or the start fails the container
        is removed by its exact name, so the next pass starts clean.

        The shared caches and the docker socket (B.5) come from the job's
        ``labels``: see ``mounts_for``. A named volume the host lacks is
        created first.
        """
        mounts = mounts_for(labels)
        self.ensure_volume(PIP_CACHE_VOLUME)
        self.run("create", "--name", name,
                 "--cpus", f"{float(profile['cpus']):g}",
                 "--memory", str(profile["memory"]),
                 "--pids-limit", str(profile["pids_limit"]),
                 *mounts,
                 image, *RUN_ONCE_COMMAND)
        try:
            self.run("cp", "-", f"{name}:{JIT_PARENT_DIR}",
                     stdin=jit_tar(jit_config))
            self.run("start", name)
        except ScalerError:
            try:
                self.remove(name)
            except ScalerError as exc:
                log(f"cannot remove {name} after a failed start: {exc}")
            raise

    def remove(self, name: str) -> None:
        """Remove ONE container by its exact name; never by filter or prune."""
        if job_id_of(name) is None:
            raise ScalerError(f"refusing to remove {name!r}: not a scaler container")
        self.run("rm", "--force", name)


_CREATED_RE = re.compile(
    r"(\d{4})-(\d\d)-(\d\d) (\d\d):(\d\d):(\d\d)(?:\.\d+)? ([+-])(\d\d)(\d\d)")


# --------------------------------------------------------------------------
# The runner image: one build per recipe, under a file lock (B.6)
# --------------------------------------------------------------------------

IMAGE_HASH_LEN = 12
RECIPE_FILES = ("Dockerfile", "run-once.sh")


def recipe_hash(runner_dir: str | Path) -> str:
    """Content hash of the image recipe: ``Dockerfile`` and, as it is copied
    into the image, ``run-once.sh``. Changing either changes the tag."""
    digest = hashlib.sha256()
    for name in RECIPE_FILES:
        path = Path(runner_dir) / name
        if not path.is_file():
            if name == "Dockerfile":
                raise ScalerError(f"no runner image recipe at {path}")
            continue
        data = path.read_bytes()
        digest.update(f"{name}\0{len(data)}\0".encode() + data)
    return digest.hexdigest()[:IMAGE_HASH_LEN]


def image_ref(cfg: dict[str, Any]) -> str:
    """``<repository>:<recipe hash>``: the config's ``image`` names the
    repository (a tag on it is replaced by the hash)."""
    image = str(cfg["image"])
    head, sep, tail = image.rpartition("/")
    repo = (head + sep) + tail.split(":", 1)[0]
    return f"{repo}:{recipe_hash(cfg['runner_dir'])}"


def ensure_image(cfg: dict[str, Any], docker: Docker, *,
                 on_wait: Callable[[], None] | None = None) -> str:
    """The tag every start in this pass uses, built if the host lacks it.

    The tag is derived from the recipe's content hash, so a changed recipe is a
    new tag and is built here, before the start that needs it. A missing tag is
    built once: the build runs under an exclusive ``flock`` on ``lock_file``, and
    a pass (or process) that finds the lock held waits for it, then looks again
    and finds the tag built. ``on_wait`` runs when the lock is already held.
    """
    ref = image_ref(cfg)
    if docker.has_image(ref):
        return ref
    lock_path = Path(str(cfg["lock_file"])).expanduser()
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_path, "a+") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            log(f"waiting for another build of {ref}")
            if on_wait is not None:
                on_wait()
            fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            if not docker.has_image(ref):
                log(f"building {ref}")
                docker.build(ref, str(cfg["runner_dir"]))
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)
    return ref


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


def is_trusted(run: dict[str, Any]) -> bool:
    """The one trust decision: does the scaler serve this run's jobs?

    A run is served only when its head repository is the repository the run
    belongs to (the scaler's own repo) and its event is ``push``,
    ``workflow_dispatch``, ``schedule`` or ``pull_request``. A fork's pull
    request, ``pull_request_target`` and every other event are not, and a run
    missing either repository field is not (fail closed). Tightening the rule
    later is an edit to this function and nothing else.
    """
    if run.get("event") not in TRUSTED_EVENTS:
        return False
    head = (run.get("head_repository") or {}).get("full_name")
    own = (run.get("repository") or {}).get("full_name")
    return bool(head) and head == own


def queued_pool_jobs(api: GitHubApi) -> list[dict[str, Any]]:
    """Queued jobs of trusted runs whose labels include ``ci-pool``, in job-id
    order. A run that fails ``is_trusted`` is skipped and logged."""
    jobs = []
    for run in api.queued_runs():
        if not is_trusted(run):
            head = (run.get("head_repository") or {}).get("full_name")
            log(f"skipping run {run.get('id')}: not trusted "
                f"(event {run.get('event')!r}, head repository {head!r})")
            continue
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

    Before the first start of a pass the runner image is ensured (see
    ``ensure_image``): the tag is the recipe's content hash, built once under a
    file lock if the host lacks it. A failed build raises, and starts nothing.
    """
    jobs = queued_pool_jobs(api)
    existing = docker.containers()
    reaped = reap_containers(cfg, api, docker, existing, now=clock(),
                             dry_run=dry_run, out=out)
    names = {str(e["Names"]) for e in existing}
    free = int(cfg["max_concurrency"]) - len(
        live_containers([e for e in existing if str(e["Names"]) not in reaped]))
    started = 0
    image: str | None = None
    for job in jobs:
        name = container_name(int(job["id"]))
        if name in names:
            continue
        if free <= 0:
            break
        if dry_run:
            out(f"would start {name} for job {job['id']}")
        else:
            if image is None:
                image = ensure_image(cfg, docker)  # built before the next start
            try:
                jit_config = api.generate_jitconfig(job)
            except ScalerError as exc:
                # No container for this job; the next pass requests again.
                log(f"no runner config for job {job['id']}: {exc}")
                continue
            profile = resolve_profile(cfg, list(job.get("labels") or []))
            try:
                docker.start(name, image, jit_config, profile,
                             labels=list(job.get("labels") or []))
            except ScalerError as exc:
                log(f"cannot start {name}: {exc}")
                continue
            log(f"started {name}")
        names.add(name)
        free -= 1
        started += 1
    return started


def _header_wait(failure: ApiError, now: float) -> float | None:
    """Seconds a 403/429 reply asks us to wait: ``Retry-After`` (seconds or an
    HTTP date), else ``x-ratelimit-reset`` (epoch seconds). None when absent."""
    if failure.status not in (403, 429):
        return None
    if failure.retry_after:
        text = failure.retry_after.strip()
        try:
            return max(0.0, float(text))
        except ValueError:
            try:
                return max(0.0, parsedate_to_datetime(text).timestamp() - now)
            except (TypeError, ValueError):
                pass
    if failure.ratelimit_reset:
        try:
            return max(0.0, float(failure.ratelimit_reset.strip()) - now)
        except ValueError:
            pass
    return None


def next_wait(poll_interval_s: float, failures: int,
              failure: ApiError | None, now: float) -> float:
    """Seconds to sleep before the next pass.

    No failure: ``poll_interval_s``. After the ``failures``-th consecutive failed
    pass: ``poll_interval_s * 2**(failures - 1)``, capped at ``BACKOFF_CAP_S``
    (never below ``poll_interval_s``). A 403/429 that names a wait (``Retry-After``,
    else ``x-ratelimit-reset``) sets the wait to that, bounded by
    ``RATE_LIMIT_MAX_WAIT_S`` so a bad header cannot park the loop.
    """
    poll = float(poll_interval_s)
    if failure is None:
        return poll
    hinted = _header_wait(failure, now)
    if hinted is not None:
        return max(poll, min(hinted, float(RATE_LIMIT_MAX_WAIT_S)))
    backoff = min(float(BACKOFF_CAP_S), poll * 2 ** max(0, failures - 1))
    return max(poll, backoff)


def run_loop(cfg: dict[str, Any], api: GitHubApi, docker: Docker, *,
             dry_run: bool = False, passes: int | None = None,
             sleep: Callable[[float], None] = time.sleep,
             clock: Callable[[], float] = time.time) -> None:
    """Run passes forever (or ``passes`` times); no failure ends the loop.

    A pass whose GitHub call failed (no reply, timeout, 5xx, 403, 429) is
    skipped as far as that call goes and retried after a longer wait, see
    ``next_wait``. A running container is never removed because the API was
    unreachable: reaping needs a job read that succeeded.
    """
    done = 0
    failures = 0
    while passes is None or done < passes:
        api.last_failure = None
        try:
            run_pass(cfg, api, docker, dry_run=dry_run, clock=clock)
        except ScalerError as exc:
            log(f"pass failed: {exc}")
        except Exception as exc:  # a daemon outlives an unexpected pass error
            log(f"pass failed unexpectedly: {type(exc).__name__}: {exc}")
        failure = api.last_failure
        failures = failures + 1 if failure is not None else 0
        wait = next_wait(float(cfg["poll_interval_s"]), failures, failure, clock())
        if failure is not None:
            log(f"GitHub API unavailable ({failure}); retrying in {wait:g}s")
        done += 1
        if passes is None or done < passes:
            sleep(wait)


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
