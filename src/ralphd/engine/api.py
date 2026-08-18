"""Container HTTP API (see docs/api.md). Minimal v0.1 subset."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import signal

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import JSONResponse, PlainTextResponse, StreamingResponse

from .. import API_VERSION, __version__
from .config import CONFIG_DIR, PROMPT_NAMES, JobConfig, list_prompts, overlay_write_path
from .creds import (
    api_creds_dir,
    clear_creds_tombstone,
    creds_deleted_dir,
    effective_env_source,
    list_creds,
    place_creds,
)
from .llm import apply_llm, current_env
from .loop import LoopSupervisor
from .redact import refresh_redaction_map, scrub_text
from .skills import (
    InvalidSkillTar,
    api_skills_dir,
    clear_tombstone,
    deleted_dir,
    effective_source,
    extract_skill_tar,
    list_skills,
    place_skills,
    tar_dir,
)
from .state import RunDir


def problem(status: int, title: str, detail: str = "") -> HTTPException:
    return HTTPException(status_code=status,
                         detail={"title": title, "status": status, "detail": detail})


def create_app(cfg: JobConfig, run: RunDir, loop: LoopSupervisor) -> FastAPI:
    app = FastAPI(title="ralphd", version=__version__)

    @app.middleware("http")
    async def auth(request: Request, call_next):
        if cfg.api_token and request.url.path != "/healthz":
            supplied = request.headers.get("authorization", "")
            if supplied != f"Bearer {cfg.api_token}":
                return JSONResponse({"title": "unauthorized", "status": 401},
                                    status_code=401)
        return await call_next(request)

    def finished() -> bool:
        return run.read_status().get("state") in ("succeeded", "failed", "aborted")

    # -- observation -----------------------------------------------------
    @app.get("/healthz")
    async def healthz():
        return {"ok": True}

    @app.get("/version")
    async def version():
        return {"ralphd": __version__, "api": API_VERSION}

    @app.get("/status")
    async def status():
        s = run.read_status()
        # Task 012 (#5): health/infraWait are always present in the contract,
        # even for a run dir written by an older engine or one whose loop has
        # not started yet -- their absence is never a third case a consumer
        # has to handle.
        s.setdefault("health", "ok")
        s.setdefault("infraWait", None)
        tasks = run.read_tasks().get("tasks", [])
        counts = {"total": len(tasks)}
        for t in tasks:
            key = {"in-progress": "inProgress",
                   "validation-failed": "validationFailed"}.get(
                t.get("status"), t.get("status", "unknown"))
            counts[key] = counts.get(key, 0) + 1
        s["tasks"] = counts
        pending = len(run.pending_steering())
        consumed = len(list(run.steering_dir.glob("[0-9][0-9][0-9]-*.md"))) - pending
        s["steering"] = {"pending": pending, "consumed": consumed}
        return s

    @app.get("/tasks")
    async def tasks():
        return run.read_tasks()

    @app.get("/prd")
    async def prd(original: bool = False):
        f = run.prd_file if (original or not run.composite_prd_file.exists()) \
            else run.composite_prd_file
        if not f.exists():
            raise problem(404, "no PRD")
        return PlainTextResponse(f.read_text(), media_type="text/markdown")

    @app.get("/notes")
    async def notes():
        text = run.notes_file.read_text() if run.notes_file.exists() else ""
        return PlainTextResponse(text, media_type="text/markdown")

    @app.get("/iterations")
    async def iterations():
        metas = []
        for d in sorted((run.root / "iterations").iterdir()):
            meta = d / "meta.json"
            if meta.exists():
                metas.append(json.loads(meta.read_text()))
        return metas

    @app.get("/iterations/{n}")
    async def iteration(n: int):
        meta = run.root / "iterations" / f"{n:04d}" / "meta.json"
        if not meta.exists():
            raise problem(404, "no such iteration")
        return json.loads(meta.read_text())

    @app.get("/iterations/{n}/output")
    async def iteration_output(n: int, tail: int = 0, follow: bool = False):
        path = run.root / "iterations" / f"{n:04d}" / "output.jsonl"
        if not path.exists():
            raise problem(404, "no transcript")
        if not follow:
            lines = path.read_text().splitlines(keepends=True)
            if tail:
                lines = lines[-tail:]
            return PlainTextResponse("".join(lines),
                                     media_type="application/x-ndjson")

        async def follower():
            with open(path) as f:
                if tail:
                    for line in f.readlines()[-tail:]:
                        yield line
                else:
                    f.seek(0, os.SEEK_END)
                while True:
                    line = f.readline()
                    if line:
                        yield line
                        continue
                    cur = run.read_status().get("currentIteration") or {}
                    if cur.get("number") != n:
                        break
                    await asyncio.sleep(0.5)
        return StreamingResponse(follower(), media_type="application/x-ndjson")

    def _iteration_dirs():
        itroot = run.root / "iterations"
        if not itroot.exists():
            return []
        return sorted(itroot.iterdir())

    def _boundary(meta: dict, event: str) -> str:
        line: dict = {"type": "ralphd.iteration", "event": event,
                     "number": meta.get("number"), "phase": meta.get("phase"),
                     "model": meta.get("model"), "approach": meta.get("approach"),
                     "startedAt": meta.get("startedAt")}
        if event == "end":
            line["exitCode"] = meta.get("exitCode")
            line["error"] = meta.get("error")
            line["usage"] = meta.get("usage")
            line["endedAt"] = meta.get("endedAt")
        return json.dumps(line) + "\n"

    def _merge_logs():
        """Yield (is_boundary, line) for every iteration transcript in order.
        Lines are scrubbed again here (defense-in-depth, task 060) on top of
        the write-time scrub in runner.py/state.py -- catches a value that
        is only *recognized* as a secret after the transcript line was
        originally written (e.g. a cred added mid-run, using the same
        literal value an earlier iteration happened to echo)."""
        for d in _iteration_dirs():
            meta_path = d / "meta.json"
            if not meta_path.exists():
                continue
            try:
                meta = json.loads(meta_path.read_text())
            except json.JSONDecodeError:
                continue
            yield True, scrub_text(_boundary(meta, "start"))
            out = d / "output.jsonl"
            if out.exists():
                for line in out.read_text().splitlines(keepends=True):
                    line = line if line.endswith("\n") else line + "\n"
                    yield False, scrub_text(line)
            if meta.get("endedAt"):
                yield True, scrub_text(_boundary(meta, "end"))

    def _apply_tail(entries: list[tuple[bool, str]], tail: int) -> list[tuple[bool, str]]:
        """Keep only the last `tail` non-boundary (transcript) lines, plus any
        boundary lines that fall within that window."""
        if not tail:
            return entries
        selected: list[tuple[bool, str]] = []
        content_count = 0
        for is_boundary, line in reversed(entries):
            selected.append((is_boundary, line))
            if not is_boundary:
                content_count += 1
                if content_count >= tail:
                    break
        return list(reversed(selected))

    @app.get("/logs")
    async def logs(tail: int = 0, follow: bool = False):
        entries = _apply_tail(list(_merge_logs()), tail)

        if not follow:
            text = "".join(line for _, line in entries)
            return PlainTextResponse(text, media_type="application/x-ndjson")

        async def follower():
            # Replay the (possibly tail-limited) snapshot first.
            for _, line in entries:
                yield line

            # Then continue live from wherever that snapshot's true (untailed)
            # end was: the actual end of the last iteration's output.jsonl at
            # snapshot time, and whether that iteration had already ended.
            dirs = _iteration_dirs()
            if dirs:
                idx = len(dirs) - 1
                last_meta = json.loads((dirs[idx] / "meta.json").read_text())
                out = dirs[idx] / "output.jsonl"
                pos = out.stat().st_size if out.exists() else 0
                if last_meta.get("endedAt"):
                    idx += 1
                    pos = 0
                    start_emitted = False
                else:
                    start_emitted = True  # its start boundary was already replayed above
            else:
                idx = 0
                pos = 0
                start_emitted = False

            while True:
                dirs = _iteration_dirs()
                if idx >= len(dirs):
                    if finished():
                        break
                    await asyncio.sleep(0.3)
                    continue
                d = dirs[idx]
                meta_path = d / "meta.json"
                if not meta_path.exists():
                    await asyncio.sleep(0.1)
                    continue
                meta = json.loads(meta_path.read_text())
                if not start_emitted:
                    yield scrub_text(_boundary(meta, "start"))
                    start_emitted = True
                out = d / "output.jsonl"
                if out.exists():
                    with open(out) as f:
                        f.seek(pos)
                        while line := f.readline():
                            pos = f.tell()
                            line = line if line.endswith("\n") else line + "\n"
                            yield scrub_text(line)
                meta = json.loads(meta_path.read_text())
                if meta.get("endedAt"):
                    yield scrub_text(_boundary(meta, "end"))
                    idx += 1
                    pos = 0
                    start_emitted = False
                else:
                    await asyncio.sleep(0.3)

        return StreamingResponse(follower(), media_type="application/x-ndjson")

    @app.get("/events")
    async def events(since: int = -1):
        async def stream():
            path = run.root / "events.jsonl"
            pos = 0
            sent = since
            while True:
                if path.exists():
                    with open(path) as f:
                        f.seek(pos)
                        while line := f.readline():
                            pos = f.tell()
                            try:
                                ev = json.loads(line)
                            except json.JSONDecodeError:
                                continue
                            if since >= 0 and ev["id"] <= sent:
                                continue
                            sent = ev["id"]
                            yield f"id: {ev['id']}\nevent: {ev['type']}\ndata: {json.dumps(ev)}\n\n"
                yield ": keepalive\n\n"
                await asyncio.sleep(1)
        return StreamingResponse(stream(), media_type="text/event-stream")

    @app.get("/artifacts")
    async def artifacts():
        out = []
        for p in sorted(run.artifacts_dir.rglob("*")):
            if p.is_file():
                out.append({"path": str(p.relative_to(run.artifacts_dir)),
                            "size": p.stat().st_size})
        return out

    @app.get("/artifacts/{path:path}")
    async def artifact(path: str):
        f = (run.artifacts_dir / path).resolve()
        if not f.is_relative_to(run.artifacts_dir.resolve()) or not f.is_file():
            raise problem(404, "no such artifact")
        return Response(f.read_bytes(), media_type="application/octet-stream")

    # -- effective config, redacted (PRD req 10) ---------------------------
    @app.get("/config")
    async def config_effective():
        """Effective job config: budgets, flags, model strategy, prompt
        sources, and skills/creds *names* only -- never credential values,
        never LLM env values (only the configured key *names*, if any)."""
        doc = cfg.effective()
        doc["prompts"] = list_prompts()
        doc["skills"] = [{"name": s["name"], "origin": s["origin"]}
                         for s in list_skills(CONFIG_DIR)]
        doc["creds"] = [c["name"] for c in list_creds(CONFIG_DIR)]
        doc["llmEnvKeys"] = sorted(current_env().keys())
        return doc

    # -- prompts CRUD (PRD req 10) -----------------------------------------
    @app.get("/config/prompts")
    async def prompts_list():
        """Every phase prompt name with its effective source: builtin,
        mounted (/config/prompts/{name}.md), or api (runtime PUT override)."""
        return list_prompts()

    @app.put("/config/prompts/{name}", status_code=204)
    async def put_prompt_override(name: str, request: Request):
        """Runtime prompt override (PRD req 10). Body is the raw prompt
        markdown; it's written to the container-local writable overlay (never
        under the read-only-mounted /config, never under the run dir) and
        takes effect on the next iteration that builds this phase's prompt."""
        if name not in PROMPT_NAMES:
            raise problem(422, "invalid prompt name",
                         f"must be one of: {', '.join(PROMPT_NAMES)}")
        body = await request.body()
        if not body.strip():
            raise problem(422, "empty body")
        dest = overlay_write_path(f"prompts/{name}.md")
        dest.write_bytes(body)
        run.emit("log", message=f"prompt overridden via API: {name}")
        return Response(status_code=204)

    # -- skills CRUD (PRD req 10) ------------------------------------------
    @app.get("/config/skills")
    async def skills_list():
        return list_skills(CONFIG_DIR)

    @app.get("/config/skills/{name}")
    async def skills_get(name: str):
        found = effective_source(CONFIG_DIR, name)
        if not found:
            raise problem(404, "no such skill")
        src, _origin = found
        return Response(tar_dir(src), media_type="application/x-tar")

    @app.put("/config/skills/{name}", status_code=204)
    async def skills_put(name: str, request: Request):
        body = await request.body()
        if not body:
            raise problem(422, "empty body")
        dest = api_skills_dir() / name
        dest.parent.mkdir(parents=True, exist_ok=True)
        try:
            extract_skill_tar(body, dest)
        except InvalidSkillTar as exc:
            raise problem(422, "invalid skill archive", str(exc)) from exc
        clear_tombstone(name)
        place_skills(CONFIG_DIR)
        run.emit("log", message=f"skill added via API: {name}")
        return Response(status_code=204)

    @app.delete("/config/skills/{name}", status_code=204)
    async def skills_delete(name: str):
        if not effective_source(CONFIG_DIR, name):
            raise problem(404, "no such skill")
        api_dir = api_skills_dir() / name
        if api_dir.exists():
            shutil.rmtree(api_dir)
        ddir = deleted_dir()
        ddir.mkdir(parents=True, exist_ok=True)
        (ddir / name).touch()
        place_skills(CONFIG_DIR)
        run.emit("log", message=f"skill removed via API: {name}")
        return Response(status_code=204)

    # -- creds CRUD (PRD req 10) --------------------------------------------
    @app.get("/config/creds")
    async def creds_list():
        return list_creds(CONFIG_DIR)

    @app.get("/config/creds/{name}")
    async def creds_get(name: str):
        found = effective_env_source(CONFIG_DIR, name)
        if not found:
            raise problem(404, "no such credential")
        src, _origin = found
        return PlainTextResponse(src.read_text())

    @app.put("/config/creds/{name}", status_code=204)
    async def creds_put(name: str, request: Request):
        body = await request.body()
        if not body.strip():
            raise problem(422, "empty body")
        dest = api_creds_dir() / f"{name}.env"
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(body)
        clear_creds_tombstone(name)
        place_creds(CONFIG_DIR)
        refresh_redaction_map()
        run.emit("log", message=f"credential added via API: {name}")
        return Response(status_code=204)

    @app.delete("/config/creds/{name}", status_code=204)
    async def creds_delete(name: str):
        if not effective_env_source(CONFIG_DIR, name):
            raise problem(404, "no such credential")
        api_file = api_creds_dir() / f"{name}.env"
        if api_file.exists():
            api_file.unlink()
        ddir = creds_deleted_dir()
        ddir.mkdir(parents=True, exist_ok=True)
        (ddir / f"{name}.env").touch()
        place_creds(CONFIG_DIR)
        refresh_redaction_map()
        run.emit("log", message=f"credential removed via API: {name}")
        return Response(status_code=204)

    # -- llm env + pi fragment (PRD req 10) ---------------------------------
    @app.put("/config/llm", status_code=204)
    async def put_llm(body: dict):
        """Mid-run LLM endpoint/key rotation. Body:
        `{"env": {"KEY": "value"}, "pi": {...models.json fragment...}}`.
        `env` (when given) replaces the whole env-override set applied to
        subsequent `pi` invocations; `pi` (when given) is deep-merged into
        `~/.pi/agent/models.json` immediately. Neither ever lands in the
        run dir, events, or job.json."""
        body = body or {}
        env = body.get("env")
        pi_fragment = body.get("pi")
        if env is None and pi_fragment is None:
            raise problem(422, "empty body",
                         'expected {"env": {...}} and/or {"pi": {...}}')
        if env is not None and not isinstance(env, dict):
            raise problem(422, "invalid env", "env must be an object of KEY: value")
        if pi_fragment is not None and not isinstance(pi_fragment, dict):
            raise problem(422, "invalid pi fragment", "pi must be an object")
        apply_llm(env, pi_fragment)
        refresh_redaction_map()
        run.emit("log", message="LLM config replaced via API")
        return Response(status_code=204)

    # -- steering & control ------------------------------------------------
    @app.post("/steering", status_code=202)
    async def steer(body: dict):
        if finished():
            raise problem(409, "job finished", "steering has no effect")
        message = (body or {}).get("message", "").strip()
        if not message:
            raise problem(422, "message required")
        fname = run.add_steering(message, (body or {}).get("name"))
        return {"file": fname}

    @app.get("/steering")
    async def steering_list():
        consumed = {p.name for p in run.steering_dir.glob("[0-9][0-9][0-9]-*.md")} - \
                   {p.name for p in run.pending_steering()}
        return [{"file": p.name, "consumed": p.name in consumed}
                for p in sorted(run.steering_dir.glob("[0-9][0-9][0-9]-*.md"))]

    @app.post("/interrupt")
    async def interrupt(body: dict | None = None):
        if body and body.get("message"):
            run.add_steering(body["message"], body.get("name"))
        if not loop.interrupt():
            raise problem(409, "no iteration running")
        return {"interrupted": True}

    @app.post("/pause")
    async def pause():
        if finished():
            raise problem(409, "job finished")
        loop.pause()
        return {"paused": True}

    @app.post("/resume")
    async def resume():
        loop.resume()
        return {"resumed": True}

    @app.post("/abort")
    async def abort(body: dict | None = None):
        if finished():
            raise problem(409, "job already finished")
        loop.abort((body or {}).get("reason", ""))
        loop.resume()  # unblock a paused loop so it can wind down
        return {"aborting": True}

    @app.post("/shutdown")
    async def shutdown():
        if not finished():
            raise problem(409, "job still running", "abort first")
        run.emit("log", message="shutdown requested")
        asyncio.get_event_loop().call_later(0.2, os.kill, os.getpid(), signal.SIGTERM)
        return {"shuttingDown": True}

    return app
