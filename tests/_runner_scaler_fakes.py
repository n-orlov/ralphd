"""Fixtures for the runner-scaler tests: a fake GitHub API and a fake `docker`.

Nothing here touches the network, a docker daemon or a real credential.

* `FakeGitHub` is a local HTTP server (127.0.0.1, ephemeral port) that serves
  queued workflow runs, per-run jobs and `generate-jitconfig`, and records
  every request's method, path, query and JSON body in `.requests`. It can
  also be told to fail the next N calls (`fail_next`). Every JIT value it
  returns is the obviously fake string `FAKE_JIT_VALUE_<runner name>`.
* `FakeDocker` writes an executable `docker` into a directory that a test puts
  first on `PATH`. It appends each argv as one JSON line to a log and keeps
  container/image/volume state in a JSON file, so a test can seed state and
  read back what the scaler did. `FAKE_DOCKER_BUILD_GATE=<path>` holds every
  `build` until that file exists, so a test can keep one build in flight. It
  refuses any filter, wildcard or prune, so a scaler that removes by pattern
  fails loudly.
"""

from __future__ import annotations

import json
import os
import stat
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

FAKE_TOKEN = "FAKE_TOKEN_NOT_A_SECRET"


class FakeGitHub:
    """A fake GitHub REST API for one repository."""

    def __init__(self, repo: str = "n-orlov/ralphd") -> None:
        self.repo = repo
        self.runs: list[dict[str, Any]] = []
        self.jobs: dict[int, list[dict[str, Any]]] = {}
        self.requests: list[dict[str, Any]] = []
        self._failures: list[dict[str, Any]] = []
        self._lock = threading.Lock()
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    # ---- setup helpers -------------------------------------------------
    def add_run(self, run_id: int, *, event: str = "push",
                head_repo: str | None = None, status: str = "queued") -> None:
        self.runs.append({
            "id": run_id, "status": status, "event": event,
            "repository": {"full_name": self.repo},
            "head_repository": {"full_name": head_repo or self.repo},
        })
        self.jobs.setdefault(run_id, [])

    def add_job(self, run_id: int, job_id: int, labels: list[str], *,
                status: str = "queued") -> None:
        self.jobs.setdefault(run_id, []).append({
            "id": job_id, "run_id": run_id, "status": status,
            "labels": list(labels), "name": f"job-{job_id}",
        })

    def set_job_status(self, job_id: int, status: str) -> None:
        for jobs in self.jobs.values():
            for job in jobs:
                if job["id"] == job_id:
                    job["status"] = status

    def fail_next(self, count: int, status: int = 503,
                  headers: dict[str, str] | None = None,
                  path_contains: str | None = None) -> None:
        """Answer the next `count` requests with HTTP `status`, with `headers`.

        With `path_contains`, only requests whose path contains it consume a
        failure; others are served normally.
        """
        with self._lock:
            self._failures.extend(
                {"status": status, "headers": headers or {},
                 "path_contains": path_contains} for _ in range(count))

    def requests_to(self, method: str, path_prefix: str = "") -> list[dict[str, Any]]:
        return [r for r in self.requests
                if r["method"] == method and r["path"].startswith(path_prefix)]

    # ---- server --------------------------------------------------------
    @property
    def url(self) -> str:
        assert self._server is not None, "FakeGitHub is not started"
        return f"http://127.0.0.1:{self._server.server_address[1]}"

    def start(self) -> "FakeGitHub":
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args: Any) -> None:  # silence
                pass

            def _reply(self, status: int, body: Any,
                       headers: dict[str, str] | None = None) -> None:
                data = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                for key, value in (headers or {}).items():
                    self.send_header(key, value)
                self.end_headers()
                self.wfile.write(data)

            def _handle(self, method: str) -> None:
                split = urlsplit(self.path)
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b""
                body = json.loads(raw) if raw else None
                query = {k: v[0] for k, v in parse_qs(split.query).items()}
                with fake._lock:
                    fake.requests.append({
                        "method": method, "path": split.path,
                        "query": query, "body": body,
                        "authorized": self.headers.get("Authorization", "")
                        == f"Bearer {FAKE_TOKEN}",
                    })
                    failure = None
                    for i, candidate in enumerate(fake._failures):
                        if candidate["path_contains"] in (None, ) or \
                                candidate["path_contains"] in split.path:
                            failure = fake._failures.pop(i)
                            break
                if failure:
                    self._reply(failure["status"], {"message": "fake failure"},
                                failure["headers"])
                    return
                status, payload = fake._route(method, split.path, query, body)
                self._reply(status, payload)

            def do_GET(self) -> None:
                self._handle("GET")

            def do_POST(self) -> None:
                self._handle("POST")

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever,
                                        daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None

    def __enter__(self) -> "FakeGitHub":
        return self.start()

    def __exit__(self, *exc: object) -> None:
        self.stop()

    # ---- routing -------------------------------------------------------
    def _page(self, items: list[Any], query: dict[str, str]) -> list[Any]:
        per_page = int(query.get("per_page", 30))
        page = int(query.get("page", 1))
        return items[(page - 1) * per_page: page * per_page]

    def _route(self, method: str, path: str, query: dict[str, str],
               body: Any) -> tuple[int, Any]:
        base = f"/repos/{self.repo}/actions"
        if method == "GET" and path == f"{base}/runs":
            wanted = query.get("status")
            runs = [r for r in self.runs if wanted in (None, r["status"])]
            return 200, {"total_count": len(runs),
                         "workflow_runs": self._page(runs, query)}
        parts = path.split("/")
        if (method == "GET" and path.startswith(f"{base}/runs/")
                and parts[-1] == "jobs"):
            jobs = self.jobs.get(int(parts[-2]), [])
            return 200, {"total_count": len(jobs), "jobs": self._page(jobs, query)}
        if (method == "GET" and parts[-2] == "jobs"
                and path.startswith(f"{base}/jobs/")):
            for jobs in self.jobs.values():
                for job in jobs:
                    if job["id"] == int(parts[-1]):
                        return 200, job
            return 404, {"message": "not found"}
        if method == "POST" and path == f"{base}/runners/generate-jitconfig":
            name = (body or {}).get("name", "unnamed")
            return 201, {"runner": {"id": 1, "name": name},
                         "encoded_jit_config": f"FAKE_JIT_VALUE_{name}"}
        return 404, {"message": "not found"}


_FAKE_DOCKER_SOURCE = r'''#!{python}
import fcntl, json, os, sys, time

root = os.environ["FAKE_DOCKER_DIR"]
state_path = os.path.join(root, "state.json")
argv = sys.argv[1:]
with open(os.path.join(root, "argv.jsonl"), "a") as fh:
    fcntl.flock(fh, fcntl.LOCK_EX)
    fh.write(json.dumps(argv) + "\n")


def load():
    with open(state_path) as fh:
        return json.load(fh)


def save(state):
    with open(state_path, "w") as fh:
        json.dump(state, fh)


def fail(message, code=1):
    print("fake docker: " + message, file=sys.stderr)
    sys.exit(code)


def image_id(tag):
    import hashlib
    return "sha256:" + hashlib.sha256(tag.encode()).hexdigest()


def stamp(epoch):
    return time.strftime("%Y-%m-%d %H:%M:%S +0000 UTC", time.gmtime(epoch))


for arg in argv:
    if arg == "--filter" or arg.startswith("--filter=") or arg == "-f" \
            or "*" in arg or arg == "prune":
        fail("filters, wildcards and prune are not supported: " + arg)

state = load()
now = state.get("clock") or time.time()


def emit(state, typ, action, actor_id, attrs=None):
    """Append one event to the daemon's bounded event log, stamped strictly
    after the previous one; the oldest are dropped past ``events_limit``."""
    ns = max(int(now * 1e9), state.get("last_event_ns", 0) + 1)
    state["last_event_ns"] = ns
    events = state.setdefault("events", [])
    events.append({"Type": typ, "Action": action,
                   "Actor": {"ID": actor_id, "Attributes": dict(attrs or {})},
                   "time": ns // 10**9, "timeNano": ns})
    del events[:max(0, len(events) - state.get("events_limit", 256))]


def daemon_ns(state):
    return max(int(now * 1e9), state.get("last_event_ns", 0))


def parse_ns(text):
    sec, _, frac = text.partition(".")
    return int(sec) * 10**9 + int((frac or "0").ljust(9, "0")[:9])
cmd = argv[0] if argv else ""
rest = argv[1:]
if cmd and cmd == os.environ.get("FAKE_DOCKER_FAIL"):
    fail("injected failure of docker " + cmd)

if cmd in ("run", "create"):
    name, image, i, binds = None, None, 0, []
    flags_with_value = {"--name", "--cpus", "--memory", "--pids-limit", "-v",
                        "--volume", "--env-file", "-e", "--env", "--user",
                        "--label", "--entrypoint", "--network", "--tmpfs",
                        "--mount", "-w", "--workdir"}
    while i < len(rest):
        a = rest[i]
        if a == "--name":
            name = rest[i + 1]; i += 2; continue
        if a.startswith("--name="):
            name = a.split("=", 1)[1]; i += 1; continue
        if a in ("-v", "--volume"):
            parts = rest[i + 1].split(":")
            binds.append({"src": parts[0], "dst": parts[1],
                          "ro": "ro" in parts[2:]})
            i += 2; continue
        if a in flags_with_value:
            i += 2; continue
        if a.startswith("-"):
            i += 1; continue
        image = a
        break
    if not name:
        fail("a container name is required")
    if name in state["containers"]:
        fail("Conflict. The container name /" + name + " is already in use", 125)
    state["containers"][name] = {
        "Names": name, "Image": image, "ID": "fake" + str(len(state["containers"])),
        "State": "running" if cmd == "run" else "created",
        "CreatedAt": stamp(now), "created_epoch": now, "binds": binds,
    }
    emit(state, "container", "create", name, {"image": image, "name": name})
    if cmd == "run":
        emit(state, "container", "start", name, {"image": image, "name": name})
    save(state)
    print(state["containers"][name]["ID"])
elif cmd == "start":
    for name in [a for a in rest if not a.startswith("-")]:
        if name not in state["containers"]:
            fail("No such container: " + name)
        state["containers"][name]["State"] = "running"
        emit(state, "container", "start", name,
             {"image": state["containers"][name]["Image"], "name": name})
        # A bind-mounted host FILE is held by its inode from this moment: the
        # container still reads it after the host path is unlinked. Snapshot what
        # the container will see, and the host-side owner and mode.
        for bind in state["containers"][name].get("binds", []):
            if os.path.isfile(bind["src"]):
                st = os.stat(bind["src"])
                with open(bind["src"], "rb") as fh:
                    content = fh.read().decode()
                state.setdefault("mounted", {}).setdefault(name, {})[bind["dst"]] = {
                    "content": content, "mode": st.st_mode & 0o7777,
                    "uid": st.st_uid, "gid": st.st_gid, "ro": bind["ro"],
                    "src": bind["src"],
                    "dir_mode": os.stat(os.path.dirname(bind["src"])).st_mode & 0o7777,
                }
    save(state)
elif cmd in ("stop", "kill"):
    for name in [a for a in rest if not a.startswith("-")]:
        if name in state["containers"]:
            state["containers"][name]["State"] = "exited"
            emit(state, "container", "die", name,
                 {"image": state["containers"][name]["Image"], "name": name})
    save(state)
elif cmd == "rm":
    names = [a for a in rest if not a.startswith("-")]
    for name in names:
        if name not in state["containers"]:
            fail("No such container: " + name)
        emit(state, "container", "destroy", name,
             {"image": state["containers"][name]["Image"], "name": name})
        del state["containers"][name]
    save(state)
    print("\n".join(names))
elif cmd == "cp":
    if rest and rest[0] == "-":
        payload = sys.stdin.buffer.read()
        dest = rest[1]
        with open(os.path.join(root, "cp-" + dest.split(":")[0] + ".tar"), "wb") as fh:
            fh.write(payload)
elif cmd == "ps":
    for entry in state["containers"].values():
        if "-a" in rest or entry["State"] == "running":
            print(json.dumps({k: v for k, v in entry.items()
                              if k not in ("created_epoch", "binds")}))
elif cmd == "images":
    for tag in state["images"]:
        repo, _, version = tag.partition(":")
        made = state.get("image_created", {}).get(tag)
        print(json.dumps({"Repository": repo, "Tag": version or "latest",
                          "ID": state.get("image_ids", {}).get(tag) or image_id(tag),
                          "CreatedAt": stamp(now if made is None else made)}))
elif cmd == "image" and rest[:1] == ["inspect"]:
    if rest[-1] not in state["images"]:
        fail("No such image: " + rest[-1])
    print(json.dumps([{"RepoTags": [rest[-1]]}]))
elif cmd == "build":
    gate = os.environ.get("FAKE_DOCKER_BUILD_GATE")
    deadline = time.time() + 60
    while gate and not os.path.exists(gate) and time.time() < deadline:
        time.sleep(0.01)  # a held build: the test releases it by creating the file
    state = load()
    tags = [rest[j + 1] for j, a in enumerate(rest[:-1]) if a in ("-t", "--tag")]
    for tag in tags:
        if tag not in state["images"]:
            state["images"].append(tag)
            state.setdefault("image_created", {})[tag] = now
    save(state)
elif cmd == "rmi":
    for tag in [a for a in rest if not a.startswith("-")]:
        if tag not in state["images"]:
            fail("No such image: " + tag)
        state["images"].remove(tag)
    save(state)
elif cmd == "volume" and rest[:1] == ["create"]:
    name = rest[-1]
    if name not in state["volumes"]:
        state["volumes"].append(name)
        emit(state, "volume", "create", name, {"driver": "local"})
    save(state)
    print(name)
elif cmd == "volume" and rest[:1] == ["rm"]:
    for name in [a for a in rest[1:] if not a.startswith("-")]:
        if name not in state["volumes"]:
            fail("No such volume: " + name)
        state["volumes"].remove(name)
        emit(state, "volume", "destroy", name, {"driver": "local"})
        print(name)
    save(state)
elif cmd == "events":
    opts = {rest[j]: rest[j + 1] for j in range(len(rest) - 1) if rest[j].startswith("--")}
    if "--until" not in opts:
        fail("the fake streams no events: --until is required")
    since = parse_ns(opts["--since"]) if "--since" in opts else 0
    until = parse_ns(opts["--until"])
    for event in state.get("events", []):
        if since <= event["timeNano"] <= until:
            print(json.dumps(event))
elif cmd == "info" and "{{json .SystemTime}}" in rest:
    ns = daemon_ns(state)
    print(json.dumps(time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(ns // 10**9))
                     + ".%09dZ" % (ns % 10**9)))
elif cmd == "volume" and rest[:1] == ["inspect"]:
    if rest[-1] not in state["volumes"]:
        fail("No such volume: " + rest[-1])
    print(json.dumps([{"Name": rest[-1]}]))
elif cmd in ("info", "version"):
    print("{}")
else:
    fail("unsupported command: " + cmd)
'''


class FakeDocker:
    """A fake `docker` executable plus the state and argv log it keeps."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.bin_dir = self.root / "bin"
        self.state_dir = self.root / "state"
        self.bin_dir.mkdir(parents=True, exist_ok=True)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        exe = self.bin_dir / "docker"
        exe.write_text(_FAKE_DOCKER_SOURCE.replace("{python}", sys.executable))
        exe.chmod(exe.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        (self.state_dir / "argv.jsonl").touch()
        self._write({"containers": {}, "images": [], "volumes": [], "clock": None})

    # ---- environment ---------------------------------------------------
    def env(self, base: dict[str, str] | None = None) -> dict[str, str]:
        """An environment with the fake first on PATH and its state dir set."""
        env = dict(os.environ if base is None else base)
        env["PATH"] = f"{self.bin_dir}{os.pathsep}{env.get('PATH', '')}"
        env["FAKE_DOCKER_DIR"] = str(self.state_dir)
        return env

    # ---- state ---------------------------------------------------------
    def _read(self) -> dict[str, Any]:
        return json.loads((self.state_dir / "state.json").read_text())

    def _write(self, state: dict[str, Any]) -> None:
        """Save ``state``. A container added, removed or moved to another state
        here (a sibling job's container, as the daemon would see it) is logged in
        the daemon's event log, as the real daemon logs every container event."""
        path = self.state_dir / "state.json"
        if path.exists():
            import time
            old = json.loads(path.read_text()).get("containers", {})
            new = state.get("containers", {})
            moves = [(name, "destroy", entry) for name, entry in old.items()
                     if name not in new]
            moves += [(name, "create", entry) for name, entry in new.items()
                      if name not in old]
            moves += [(name, "die" if entry.get("State") in ("exited", "dead") else "start",
                       entry) for name, entry in new.items()
                      if name in old and old[name].get("State") != entry.get("State")]
            now = state.get("clock") or time.time()
            for name, action, entry in moves:
                ns = max(int(now * 1e9), state.get("last_event_ns", 0) + 1)
                state["last_event_ns"] = ns
                events = state.setdefault("events", [])
                events.append({"Type": "container", "Action": action,
                               "Actor": {"ID": name, "Attributes": {
                                   "image": entry.get("Image", ""), "name": name}},
                               "time": ns // 10**9, "timeNano": ns})
                del events[:max(0, len(events) - state.get("events_limit", 256))]
        path.write_text(json.dumps(state))

    def set_clock(self, epoch: float | None) -> None:
        state = self._read()
        state["clock"] = epoch
        self._write(state)

    def add_container(self, name: str, *, created_epoch: float,
                      state: str = "running", image: str = "ci-runner:test") -> None:
        import time
        data = self._read()
        data["containers"][name] = {
            "Names": name, "Image": image, "ID": f"seed{len(data['containers'])}",
            "State": state, "created_epoch": created_epoch,
            "CreatedAt": time.strftime("%Y-%m-%d %H:%M:%S +0000 UTC",
                                       time.gmtime(created_epoch)),
        }
        self._write(data)

    def set_state(self, name: str, state: str) -> None:
        """Move an existing container to ``state`` (e.g. the runner exited)."""
        data = self._read()
        data["containers"][name]["State"] = state
        self._write(data)

    def add_image(self, tag: str, *, created_epoch: float | None = None) -> None:
        data = self._read()
        data["images"].append(tag)
        if created_epoch is not None:
            data.setdefault("image_created", {})[tag] = created_epoch
        self._write(data)

    def add_volume(self, name: str) -> None:
        data = self._read()
        data["volumes"].append(name)
        self._write(data)

    def remove_container(self, name: str) -> None:
        """A container that goes away outside the scaler (a sibling job's)."""
        data = self._read()
        del data["containers"][name]
        self._write(data)

    def set_events_limit(self, limit: int) -> None:
        """How many events the daemon's log holds (the oldest drop first)."""
        data = self._read()
        data["events_limit"] = limit
        del data.setdefault("events", [])[:max(0, len(data["events"]) - limit)]
        self._write(data)

    def restart_daemon(self) -> None:
        """A daemon restart: its in-memory event log starts empty."""
        data = self._read()
        data["events"] = []
        self._write(data)

    def set_image_id(self, tag: str, image_id: str) -> None:
        """Give ``tag`` the image id ``image_id`` (two tags of one image)."""
        data = self._read()
        data.setdefault("image_ids", {})[tag] = image_id
        self._write(data)

    def _image_event(self, data: dict[str, Any], action: str, image_id: str,
                     name: str) -> None:
        """Log an image event as the daemon does: ``tag`` carries the image id as
        Actor.ID and the new ``name:tag`` as its name; ``untag`` names only the id."""
        import time
        now = data.get("clock") or time.time()
        ns = max(int(now * 1e9), data.get("last_event_ns", 0) + 1)
        data["last_event_ns"] = ns
        events = data.setdefault("events", [])
        events.append({"Type": "image", "Action": action,
                       "Actor": {"ID": image_id, "Attributes": {"name": name}},
                       "time": ns // 10**9, "timeNano": ns})
        del events[:max(0, len(events) - data.get("events_limit", 256))]

    def image_id_of(self, tag: str) -> str:
        """The image id the fake reports for ``tag``."""
        import hashlib
        return self._read().get("image_ids", {}).get(tag) or \
            "sha256:" + hashlib.sha256(tag.encode()).hexdigest()

    def tag_image(self, source: str, tag: str) -> None:
        """``docker tag source tag``: a second name for the same image (same id
        and creation time), logged as an ``image tag`` event."""
        image_id = self.image_id_of(source)
        data = self._read()
        if tag not in data["images"]:
            data["images"].append(tag)
        data.setdefault("image_ids", {})[tag] = image_id
        made = data.get("image_created", {}).get(source)
        if made is not None:
            data.setdefault("image_created", {})[tag] = made
        self._image_event(data, "tag", image_id, tag)
        self._write(data)

    def untag_image(self, tag: str) -> None:
        """``docker rmi tag`` from outside the scaler (a sibling job's cleanup),
        logged as the daemon logs it: an ``untag`` naming only the image id."""
        image_id = self.image_id_of(tag)
        data = self._read()
        data["images"].remove(tag)
        self._image_event(data, "untag", image_id, image_id)
        self._write(data)

    def events(self) -> list[dict[str, Any]]:
        return self._read().get("events", [])

    def containers(self) -> dict[str, dict[str, Any]]:
        return self._read()["containers"]

    def images(self) -> list[str]:
        return self._read()["images"]

    def volumes(self) -> list[str]:
        return self._read()["volumes"]

    def cp_payload(self, container: str) -> bytes:
        """The tar a `docker cp - <container>:<dir>` received on stdin."""
        return (self.state_dir / f"cp-{container}.tar").read_bytes()

    def mounted_file(self, container: str, dest: str) -> dict[str, Any] | None:
        """What the container sees at `dest` once it started: the bind-mounted
        host file's content, mode, owner and its directory's mode as they were AT
        START (None if no file was mounted there then)."""
        return self._read().get("mounted", {}).get(container, {}).get(dest)

    def argv_log(self) -> list[list[str]]:
        text = (self.state_dir / "argv.jsonl").read_text()
        return [json.loads(line) for line in text.splitlines() if line.strip()]

    def commands(self, *verbs: str) -> list[list[str]]:
        """Logged argv whose first element is one of `verbs`."""
        return [argv for argv in self.argv_log() if argv and argv[0] in verbs]
