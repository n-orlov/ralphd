"""Container HTTP API (see docs/api.md). Minimal v0.1 subset."""

from __future__ import annotations

import asyncio
import json
import os
import signal

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import JSONResponse, PlainTextResponse, StreamingResponse

from .. import API_VERSION, __version__
from .config import JobConfig
from .loop import LoopSupervisor
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
