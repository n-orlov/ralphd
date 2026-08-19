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
from ..log_merge import apply_tail, boundary_line, iteration_dirs, merge_entries
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
from .state import RunDir, prd_path


def problem(status: int, title: str, detail: str = "") -> HTTPException:
    return HTTPException(status_code=status,
                         detail={"title": title, "status": status, "detail": detail})


class BudgetSpecError(ValueError):
    """An `iterations` spec that PATCH /config/budget must reject (422)."""


def resolve_iterations_spec(spec: object, current: int) -> int:
    """Resolve `PATCH /config/budget`'s `iterations` spec against the run's
    current budget (task 045, #3).

    Accepts the same two forms `ralphctl resume --iterations` already takes,
    so operators only have to learn one syntax:

    * `"+N"` (string) -- relative top-up, N >= 0;
    * `N` (int, or its string form) -- absolute new budget, N >= 1.

    To *lower* a budget, pass the absolute value: a bare `-5` is read as the
    absolute budget -5 and rejected as negative, never as a decrement.
    Raises BudgetSpecError with an operator-readable message.
    """
    if isinstance(spec, bool) or spec is None:
        raise BudgetSpecError("expected an integer or a \"+N\" string")
    if isinstance(spec, int):
        value, relative = spec, False
    elif isinstance(spec, str):
        text = spec.strip()
        relative = text.startswith("+")
        try:
            value = int(text[1:] if relative else text)
        except ValueError:
            raise BudgetSpecError(
                f"{spec!r} is not an integer or a \"+N\" top-up") from None
    else:
        raise BudgetSpecError(
            f"expected an integer or a \"+N\" string, got {type(spec).__name__}")
    if relative:
        if value < 0:
            raise BudgetSpecError(
                f"relative top-up must not be negative (got {spec!r}); pass an "
                "absolute value to lower the budget")
        return current + value
    if value < 1:
        raise BudgetSpecError(
            f"absolute iteration budget must be a positive integer (got {value})")
    return value


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
        # Task 019 (#5): same for the reflect phase's verdict -- null means
        # "no reflect iteration has finished" (reflect off, or not there yet),
        # never "an older engine wrote this run dir".
        s.setdefault("reflect", None)
        # Task 006 (#16): the approach denominator is part of the contract too
        # -- an explicit null for a run dir written by a pre-v0.6 engine, which
        # consumers render as a bare `approach` with no `/m` rather than
        # guessing a denominator (or crashing on a missing key).
        s.setdefault("maxApproaches", None)
        tasks_read = run.read_tasks_result()
        # Task 023 (#8): shared with the CLI's on-disk fallback (state.py).
        # Task 003 (#15): counts come from the hardened reader, so a request
        # that lands inside the agent's rewrite of tasks.json reports the
        # last-good counts flagged stale instead of collapsing to total 0 --
        # and `tasksStale`/`tasksSource` say which happened.
        s["tasks"] = tasks_read.counts
        s.update(tasks_read.contract)
        pending = len(run.pending_steering())
        consumed = len(list(run.steering_dir.glob("[0-9][0-9][0-9]-*.md"))) - pending
        s["steering"] = {"pending": pending, "consumed": consumed}
        return s

    @app.get("/tasks")
    async def tasks():
        # Task 003 (#15): the payload is tasks.json verbatim plus the read's
        # provenance (`tasksStale`/`tasksSource`, appended last so a plan key
        # of the same name can never claim freshness). A mid-write file yields
        # the last-good plan flagged stale, never an empty task list.
        res = run.read_tasks_result()
        return {**res.doc, **res.contract}

    @app.get("/prd")
    async def prd(original: bool = False):
        # Task 056 (#1): which of prd.md/composite-prd.md is "the PRD" is
        # decided by the one shared helper in state.py, which the hub's
        # on-disk fallback (ui_server.prd_text) uses too.
        f = prd_path(run.root, original=original)
        if f is None:
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
        return iteration_dirs(run.root)

    def _merge_logs():
        """The on-disk merge (shared module `ralphd.log_merge`, task 038) with
        serving-time scrubbing: lines are scrubbed again here
        (defense-in-depth, task 060) on top of the write-time scrub in
        runner.py/state.py -- catches a value that is only *recognized* as a
        secret after the transcript line was originally written (e.g. a cred
        added mid-run, using the same literal value an earlier iteration
        happened to echo)."""
        return merge_entries(run.root, scrub=scrub_text)

    @app.get("/logs")
    async def logs(tail: int = 0, follow: bool = False):
        entries = apply_tail(list(_merge_logs()), tail)

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
                    yield scrub_text(boundary_line(meta, "start"))
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
                    yield scrub_text(boundary_line(meta, "end"))
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

    # -- iteration budget top-up (PRD req 3 / issue #3) ---------------------
    @app.patch("/config/budget")
    async def patch_budget(body: dict | None = None):
        """Raise (or lower) the iteration budget of a running job without
        restarting the container: `{"iterations": "+10"}` tops up by 10,
        `{"iterations": 40}` sets it absolutely. The new value is live at the
        next iteration boundary (the loop reads cfg.iterations on every turn)
        and immediately visible in `GET /status` (`iterationsBudget`) and
        `GET /config` (`budgets.iterations`).

        It is a *live-engine* change: `/config/job.yaml` is a read-only mount,
        so the engine cannot persist it there -- the new budget lives in this
        engine process (and in status.json's iterationsBudget); a later
        `ralphctl resume <run-id> --iterations +N` is what carries a bigger
        budget into a fresh container.
        """
        if finished():
            raise problem(409, "job finished",
                         "the iteration budget no longer applies; bump it with "
                         "`ralphctl resume <run-id> --iterations +N`")
        spec = (body or {}).get("iterations")
        if spec is None:
            raise problem(422, "iterations required",
                         'expected {"iterations": "+10"} or {"iterations": 40}')
        try:
            value = resolve_iterations_spec(spec, cfg.iterations)
        except BudgetSpecError as exc:
            raise problem(422, "invalid iterations", str(exc)) from exc
        used = loop.iterations_used_charged
        if value < used:
            raise problem(409, "budget below iterations used",
                         f"{value} is below the {used} iteration(s) already "
                         "used; a budget can only be set to the current usage "
                         "or above")
        previous = cfg.iterations
        loop.set_iteration_budget(value)
        run.emit("budget_changed", field="iterations", previous=previous,
                 iterations=value, delta=value - previous, iterationsUsed=used,
                 source="api")
        return {"iterations": value, "previous": previous, "iterationsUsed": used}

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

    @app.post("/retry")
    async def retry_now():
        # Task 015 (#5): wake an infra backoff wait *now*. Distinct from
        # /resume, which releases an operator *pause*: a degraded run is not
        # paused, it is waiting out an outage, and the two states are
        # independent (this route never unpauses, /resume never shortens a
        # backoff).
        if finished():
            raise problem(409, "job finished", "retry has no effect")
        if not loop.retry_now():
            raise problem(
                409, "not waiting on an infra fault",
                "/retry only wakes a run whose /status shows health "
                "'degraded' with a populated infraWait; use /resume to "
                "release an operator pause")
        return {"retrying": True}

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
