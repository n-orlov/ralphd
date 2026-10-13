"""Container HTTP API (see docs/api.md). Minimal v0.1 subset."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import signal
import time

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import JSONResponse, PlainTextResponse, StreamingResponse

from .. import API_VERSION, __version__
from ..log_merge import (apply_tail, boundary_line, iteration_dirs, merge_entries,
                         merge_entries_tail)
from .answer import AnswerRefused
from .config import (CONFIG_DIR, PROMPT_NAMES, JobConfig, classify_method,
                     list_prompts, overlay_write_path)
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
    ambient_home,
    api_skills_dir,
    clear_tombstone,
    deleted_dir,
    effective_source,
    extract_skill_tar,
    list_skills,
    place_skills,
    tar_dir,
    verify_placement_and_report,
)
from .state import (
    STEERING_GLOB,
    AWAITING_INPUT_STATE,
    EXHAUSTED_STATE,
    RunDir,
    image_record,
    prd_path,
    steering_entries,
    utc_from_epoch,
)


def problem(status: int, title: str, detail: str = "") -> HTTPException:
    return HTTPException(status_code=status,
                         detail={"title": title, "status": status, "detail": detail})


class BudgetSpecError(ValueError):
    """An `iterations` spec that PATCH /config/budget must reject (422)."""


def resolve_iterations_spec(spec: object, current: int,
                            label: str = "iteration budget") -> int:
    """Resolve `PATCH /config/budget`'s `iterations` spec against the run's
    current budget (task 045, #3).

    Accepts the same two forms `ralphctl resume --iterations` already takes,
    so operators only have to learn one syntax:

    * `"+N"` (string) -- relative top-up, N >= 0;
    * `N` (int, or its string form) -- absolute new budget, N >= 1.

    To *lower* a budget, pass the absolute value: a bare `-5` is read as the
    absolute budget -5 and rejected as negative, never as a decrement.
    Raises BudgetSpecError with an operator-readable message.

    Task 003 (#94 (a), operator-added scope): the SAME grammar is reused,
    unmodified, for `maxApproaches`, `iterationTimeoutS` and the job-deadline
    knob -- `label` only customises the absolute-value error message so it
    names the budget actually being set ("approaches budget", "iteration
    timeout", "job deadline") instead of always saying "iteration budget"
    for knobs that are not the iteration count. The parsing rules themselves
    (relative-must-not-be-negative, absolute-must-be->=1) are identical for
    every knob -- one syntax, four call sites.
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
            f"absolute {label} must be a positive integer (got {value})")
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
        # Task 004 (#62 candidate 1, requirement A): the boundary, distinct
        # from the `api_token` gate above. `api_token` is an operator
        # opt-in that (when set) happens to gate every route including
        # this one; it is unset by default and unset in most of this
        # module's own component tests, so it cannot be *the* thing that
        # keeps an iteration off `/abort` et al in a default deployment --
        # see artifacts/reports/requirement-a-boundary-design.md. This
        # second check is unconditional in shape (mutating routes ask for
        # it whenever `control_token` is configured) and `ralphctl start`/
        # `resume` (task 004) provision one on every real run regardless of
        # whether the operator ever passed `--api-token`, so the mutating
        # surface is closed by default rather than by operator choice.
        # A JobConfig built directly (this module's own unit/component
        # tests) never sets `control_token`, so those tests are unaffected.
        # Deliberately a different header than `Authorization`
        # (`X-Ralphd-Control-Token`) so an operator request can satisfy both
        # gates independently in one call.
        if cfg.control_token and classify_method(request.method) == "mutating":
            supplied = request.headers.get("x-ralphd-control-token", "")
            if supplied != cfg.control_token:
                return JSONResponse(
                    {"title": "unauthorized", "status": 401,
                     "detail": "mutating routes require the control token"},
                    status_code=401)
        return await call_next(request)

    def finished() -> bool:
        # Task 011 (v0.24 requirement C.1, #169): `exhausted` is deliberately
        # NOT one of `state.TERMINAL_STATES` -- a future `resume` can still
        # revive the run dir -- but *this* process's job is over the moment
        # it lands there (task 010, requirement B.3: the whole `run_job()`
        # coroutine returns and `main.py` heads straight into its shutdown
        # funnel). Every caller of `finished()` here is asking "is there any
        # more work coming from *this* engine process", not "is the run
        # permanently done" -- so a plain `state.TERMINAL_STATES` check
        # leaves `/logs?follow=true` (and `/pause`, `/steering`, `/ruling`,
        # `/retry`, `/abort`, `PATCH /config/budget`) all waiting forever on
        # a state that will never again change in this process, which is
        # exactly the live hang this task's own new coverage caught.
        return run.read_status().get("state") in ("succeeded", "failed",
                                                    "aborted", EXHAUSTED_STATE)

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
        # v0.30 F.2: same for the rate-limit wait.
        s.setdefault("rateLimitWait", None)
        # v0.30 G.2: and for the pause's sub-reason (provider_refused).
        s.setdefault("pauseDetail", None)
        # Task 019 (#5): same for the reflect phase's verdict -- null means
        # "no reflect iteration has finished" (reflect off, or not there yet),
        # never "an older engine wrote this run dir".
        s.setdefault("reflect", None)
        # Task 006 (#16): the approach denominator is part of the contract too
        # -- an explicit null for a run dir written by a pre-v0.6 engine, which
        # consumers render as a bare `approach` with no `/m` rather than
        # guessing a denominator (or crashing on a missing key).
        s.setdefault("maxApproaches", None)
        # Task 012 (#14): the model this run is actually talking to, as pi
        # resolved it (`modelRaw` = the provider-side id when it differs).
        # An explicit null means no iteration has observed a model yet (or a
        # pre-v0.6 run dir), never "the engine has no idea what it asked for".
        s.setdefault("model", None)
        s.setdefault("modelRaw", None)
        # Task 036 (#20 H4): which job image this run is running, and the
        # daemon's content id for it. The engine cannot see its own image, so
        # this is the host's record from the run dir's own host.json (read per
        # request, never cached: a `resume` rewrites it) -- explicit nulls when
        # nothing is recorded, so a consumer of the API alone can still answer
        # "which engine is this?" and tell that from "nobody wrote it down".
        s.update(image_record(run.root))
        tasks_read = run.read_tasks_result()
        # Task 023 (#8): shared with the CLI's on-disk fallback (state.py).
        # Task 003 (#15): counts come from the hardened reader, so a request
        # that lands inside the agent's rewrite of tasks.json reports the
        # last-good counts flagged stale instead of collapsing to total 0 --
        # and `tasksStale`/`tasksSource` say which happened.
        s["tasks"] = tasks_read.counts
        s.update(tasks_read.contract)
        pending = len(run.pending_steering())
        consumed = len(list(run.steering_dir.glob(STEERING_GLOB))) - pending
        s["steering"] = {"pending": pending, "consumed": consumed}
        return s

    @app.get("/tasks")
    async def tasks():
        # Task 003 (#15): the payload is tasks.json verbatim plus the read's
        # provenance (`tasksStale`/`tasksSource`, appended last so a plan key
        # of the same name can never claim freshness). A mid-write file yields
        # the last-good plan flagged stale, never an empty task list.
        #
        # Task 025 (#33): ... and, when something failed, the derived
        # `taskFailureKinds` map, so a consumer of the API alone can tell "a
        # verifier judged this requirement unmet" from "the engine spent this
        # task's validation rounds" even for a plan whose records predate the
        # `failureKind` label. Shaped by `TasksRead.payload`, shared with the
        # CLI's on-disk path and the hub.
        res = run.read_tasks_result()
        return res.payload

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

    def _merge_logs(tail: int = 0):
        """The on-disk merge (shared module `ralphd.log_merge`, task 038) with
        serving-time scrubbing: lines are scrubbed again here
        (defense-in-depth, task 060) on top of the write-time scrub in
        runner.py/state.py -- catches a value that is only *recognized* as a
        secret after the transcript line was originally written (e.g. a cred
        added mid-run, using the same literal value an earlier iteration
        happened to echo).

        Task 048 (PRD H.6): a request naming a `tail` reads only the newest
        iterations needed to satisfy it (`merge_entries_tail`), instead of
        every iteration the run has ever recorded (`merge_entries`) -- the
        work `GET /logs?tail=N` costs is bounded by the tail, not by how
        long the run has been going."""
        if tail:
            return merge_entries_tail(run.root, tail, scrub=scrub_text)
        return list(merge_entries(run.root, scrub=scrub_text))

    @app.get("/logs")
    async def logs(tail: int = 0, follow: bool = False):
        entries = apply_tail(_merge_logs(tail), tail)

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
        """Raise (or lower), in flight, any of five budgets of a running job
        without restarting the container: `iterations` (PRD req 3 / issue
        #3), and -- task 003, #94 (a), operator-added scope -- `maxApproaches`,
        `iterationTimeoutS` and `jobDeadlineEpochS`, plus `curePassCeiling`
        (task 010, v0.27 requirement C.3), and `rateLimitMaxWaitS` (v0.30
        F.3). Every knob is independent and optional; a request may carry
        any non-empty subset of the six in one call. Every knob uses the SAME `resolve_iterations_spec` grammar:
        `"+N"` tops up relative to the knob's own current value, a bare `N`
        (int or numeric string) sets it absolutely.

        * `{"iterations": "+10"}` / `{"iterations": 40}` -- unchanged from
          task 045: live at the next iteration boundary (the loop reads
          `cfg.iterations` on every turn), visible in `GET /status`
          (`iterationsBudget`) and `GET /config` (`budgets.iterations`), and
          it persists across a resume the way task 010/#51 already describes.
        * `{"maxApproaches": "+1"}` / `{"maxApproaches": 5}` -- applied via
          `loop.set_max_approaches`; live at the next replan decision
          (`loop.py`'s `if self._approach < self.cfg.max_approaches:` and its
          post-review-rejection twin). Refused with 409 if the new value is
          below the run's current approach.
        * `{"iterationTimeoutS": "+300"}` / `{"iterationTimeoutS": 900}` --
          applied via `loop.set_iteration_timeout`; the per-iteration ceiling
          is read once PER ITERATION (`run_iteration`'s own
          `timeout = min(self.cfg.iteration_timeout_s, ...)`), so a change
          here applies to the NEXT iteration, never to one already running.
        * `{"jobDeadlineEpochS": "+3600"}` / `{"jobDeadlineEpochS": 1234567890}`
          -- resolved against `loop.deadline_epoch` (the same wall-clock
          epoch `set_job_deadline` moves and republishes as `deadlineAt`), so
          `"+N"` extends the run's remaining wall-clock time by N seconds and
          an absolute value sets the deadline to that Unix epoch second.
          Refused with 409 if the resolved value is at or before "now".
        * `{"curePassCeiling": "+1"}` / `{"curePassCeiling": 5}` -- v0.36
          C.2 (was task 010, v0.27 C.3): sets the CURRENT approach's
          cure-round allowance, which review and the peer verdict gate draw
          on together (`loop.cure_pass_allowance` is the figure a `+N|N` spec
          resolves against); applied via `loop.set_cure_pass_ceiling`, which
          records the top-up under status.json's `cureRoundTopUps` for this
          approach only -- a later approach starts back at
          `cure_pass_ceiling`. Refused with 409 if the new value is below
          the rounds this approach already spent (`loop.cure_passes_used`).
          Unlike `maxApproaches`/`iterationTimeoutS`/`jobDeadlineEpochS`,
          this one PERSISTS across a resume, because it is disk-backed.
        * `{"rateLimitMaxWaitS": "+3600"}` / `{"rateLimitMaxWaitS": 172800}`
          -- v0.30 F.3: the ceiling, in seconds, on the run's total
          rate-limit waiting; applied via `loop.set_rate_limit_max_wait`,
          read before the next rate-limit wait starts. It persists across a
          resume like `curePassCeiling` (`main._seed_rate_limit_max_wait`).

        Task 003/#94 (d): none of the `maxApproaches`/`iterationTimeoutS`/
        `jobDeadlineEpochS` knobs is seeded from status.json on a fresh
        container's startup the way `iterations` is (task 010, #51) --
        they are process-only. A `resume` with no budget flags reverts
        them to whatever `job.yaml` (or `ralphctl resume`'s own flags,
        task 004) say; only `iterations` and, since task 010 (v0.27
        requirement C.3), `curePassCeiling` survive a resume with no
        flags. See docs/cli.md's budget table for the same statement in
        operator-facing form.
        """
        if finished():
            raise problem(409, "job finished",
                         "the iteration budget no longer applies; bump it with "
                         "`ralphctl resume <run-id> --iterations +N`")
        body = body or {}
        has_iterations = body.get("iterations") is not None
        has_max_approaches = body.get("maxApproaches") is not None
        has_iteration_timeout = body.get("iterationTimeoutS") is not None
        has_deadline = body.get("jobDeadlineEpochS") is not None
        has_cure_pass_ceiling = body.get("curePassCeiling") is not None
        has_rate_limit_max_wait = body.get("rateLimitMaxWaitS") is not None
        if not (has_iterations or has_max_approaches or has_iteration_timeout
                or has_deadline or has_cure_pass_ceiling
                or has_rate_limit_max_wait):
            raise problem(422, "iterations required",
                         'expected {"iterations": "+10"} or {"iterations": 40}')

        result: dict = {}

        if has_iterations:
            try:
                value = resolve_iterations_spec(body["iterations"], cfg.iterations)
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
                     iterations=value, delta=value - previous,
                     iterationsUsed=used, source="api")
            result.update(iterations=value, previous=previous, iterationsUsed=used)

        if has_max_approaches:
            try:
                value = resolve_iterations_spec(body["maxApproaches"],
                                                cfg.max_approaches,
                                                label="approaches budget")
            except BudgetSpecError as exc:
                raise problem(422, "invalid maxApproaches", str(exc)) from exc
            current_approach = run.read_status().get("approach") or 1
            if value < current_approach:
                raise problem(409, "max approaches below current approach",
                             f"{value} is below the current approach "
                             f"{current_approach}; a max-approaches budget can "
                             "only be set to the current approach or above")
            previous = cfg.max_approaches
            loop.set_max_approaches(value)
            run.emit("budget_changed", field="maxApproaches", previous=previous,
                     maxApproaches=value, delta=value - previous, source="api")
            result.update(maxApproaches=value, previousMaxApproaches=previous)

        if has_iteration_timeout:
            try:
                value = resolve_iterations_spec(body["iterationTimeoutS"],
                                                cfg.iteration_timeout_s,
                                                label="iteration timeout")
            except BudgetSpecError as exc:
                raise problem(422, "invalid iterationTimeoutS", str(exc)) from exc
            previous = cfg.iteration_timeout_s
            loop.set_iteration_timeout(value)
            run.emit("budget_changed", field="iterationTimeoutS", previous=previous,
                     iterationTimeoutS=value, delta=value - previous, source="api")
            result.update(iterationTimeoutS=value,
                          previousIterationTimeoutS=previous)

        if has_deadline:
            try:
                value = resolve_iterations_spec(body["jobDeadlineEpochS"],
                                                loop.deadline_epoch,
                                                label="job deadline")
            except BudgetSpecError as exc:
                raise problem(422, "invalid jobDeadlineEpochS", str(exc)) from exc
            now = time.time()
            if value <= now:
                raise problem(409, "deadline in the past",
                             f"{value} is at or before the current time "
                             f"({now}); a job deadline cannot be set in the past")
            previous = loop.deadline_epoch
            loop.set_job_deadline(value)
            run.emit("budget_changed", field="jobDeadlineEpochS", previous=previous,
                     jobDeadlineEpochS=value, delta=value - previous, source="api")
            result.update(jobDeadlineEpochS=value,
                          previousJobDeadlineEpochS=previous,
                          deadlineAt=utc_from_epoch(value))

        if has_cure_pass_ceiling:
            try:
                value = resolve_iterations_spec(body["curePassCeiling"],
                                                loop.cure_pass_allowance,
                                                label="cure pass ceiling")
            except BudgetSpecError as exc:
                raise problem(422, "invalid curePassCeiling", str(exc)) from exc
            used = loop.cure_passes_used
            if value < used:
                raise problem(409, "cure pass ceiling below cure passes used",
                             f"{value} is below the {used} cure pass(es) the "
                             "current approach has already taken; an allowance "
                             "can only be set to the current usage or above")
            previous = loop.cure_pass_allowance
            loop.set_cure_pass_ceiling(value)
            run.emit("budget_changed", field="curePassCeiling", previous=previous,
                     curePassCeiling=value, delta=value - previous, source="api")
            result.update(curePassCeiling=value, previousCurePassCeiling=previous)

        if has_rate_limit_max_wait:
            try:
                value = resolve_iterations_spec(body["rateLimitMaxWaitS"],
                                                cfg.rate_limit_max_wait_s,
                                                label="rate-limit wait ceiling")
            except BudgetSpecError as exc:
                raise problem(422, "invalid rateLimitMaxWaitS", str(exc)) from exc
            previous = cfg.rate_limit_max_wait_s
            loop.set_rate_limit_max_wait(value)
            run.emit("budget_changed", field="rateLimitMaxWaitS", previous=previous,
                     rateLimitMaxWaitS=value, delta=value - previous, source="api")
            result.update(rateLimitMaxWaitS=value,
                          previousRateLimitMaxWaitS=previous)

        return result

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
        _home = ambient_home()
        place_skills(CONFIG_DIR, home=_home)
        verify_placement_and_report(run, CONFIG_DIR, _home)
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
        _home = ambient_home()
        place_skills(CONFIG_DIR, home=_home)
        verify_placement_and_report(run, CONFIG_DIR, _home)
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
        place_creds(CONFIG_DIR, home=ambient_home())
        refresh_redaction_map(home=ambient_home())
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
        place_creds(CONFIG_DIR, home=ambient_home())
        refresh_redaction_map(home=ambient_home())
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
        run dir, events, or the job config."""
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
        apply_llm(env, pi_fragment, home=ambient_home())
        refresh_redaction_map(home=ambient_home())
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

    @app.post("/rule", status_code=202)
    async def rule(body: dict):
        # Task 013 (v0.23 PRD requirement A.2, #224): the operator answer
        # channel's landing route. Deliberately a DISTINCT route from
        # `/steering` (`steer` is discounted by review by design) and from
        # anything `amend`-shaped (an `amend` ruling never touches this
        # process at all -- see `engine.state`'s `rulings/` vocabulary
        # block). No control-token check written here: `classify_method`
        # already treats every POST as mutating, so `cfg.control_token`
        # (when the run has one, which every real `start`/`resume` does)
        # already gates this the moment it exists (operator ruling 4) --
        # see `tests/test_control_plane_auth.py`.
        if finished():
            raise problem(409, "job finished", "a ruling has no effect")
        text = (body or {}).get("text", "").strip()
        if not text:
            raise problem(422, "text required")
        fname = run.add_ruling(text, (body or {}).get("name"))
        # v0.36 D.1: a ruling is recorded at any time and reaches both judges
        # and planning, but it never releases an `awaiting_input` park: only
        # `POST /answer` (or the input timeout) leaves it.
        return {"file": fname}

    @app.get("/steering")
    async def steering_list():
        # Task 016 (#17): the full entries -- name, timestamp, pending/applied
        # state and body -- through the ONE shared reader
        # (`state.steering_entries`), which the hub's on-disk fallback uses
        # too, so a live answer and a container-gone answer describe the same
        # run identically. The pre-v0.6 `file`/`consumed` keys are still
        # there, unchanged: this is purely additive.
        return steering_entries(run.root)

    @app.post("/interrupt")
    async def interrupt(body: dict | None = None):
        # v0.25 task 028b (PRD requirement K.3/K.4, #267): `finished()`
        # gates BEFORE any steering is enqueued -- the measured defect on
        # `deck-phase4c` was `run.add_steering` landing here and THEN this
        # handler refusing with a 409 that told the caller the message
        # never landed. The only refusal left is a genuinely finished run
        # (`state` already `succeeded`/`failed`/`aborted`/`exhausted`),
        # which enqueues nothing -- "a refusal leaves no enqueued steering
        # message behind" is true by construction, not by a second check.
        # While the loop is alive but idle (between iterations, in a
        # deferred wait, in a backoff wait) this now answers 200 rather
        # than lying that no iteration is running: `loop.interrupt_or_wake`
        # tells a signalled running iteration apart from a woken idle one,
        # and a message that did land is never reported as a failure
        # either way.
        if finished():
            raise problem(409, "job finished", "no iteration to interrupt")
        if body and body.get("message"):
            run.add_steering(body["message"], body.get("name"))
        result = loop.interrupt_or_wake()
        return {"interrupted": True, **result}

    @app.post("/tasks/{task_id}/undefer")
    async def undefer(task_id: str):
        # #292: release a deferred task early. Every refusal comes BEFORE
        # anything is enqueued (the `/interrupt` model): a finished run first,
        # then an unknown task, then a task carrying no `deferredUntil`. This
        # handler never writes tasks.json -- `loop.request_undefer` queues the
        # release and rings the wake bell, and the loop thread applies it at
        # its next selection boundary.
        if finished():
            raise problem(409, "job finished", "no deferral to release")
        task = next((t for t in run.read_tasks().get("tasks", [])
                     if isinstance(t, dict) and str(t.get("id")) == task_id),
                    None)
        if task is None:
            raise problem(404, "no such task", f"unknown task {task_id!r}")
        if not task.get("deferredUntil"):
            raise problem(409, "task not deferred",
                          f"task {task_id!r} is not deferred")
        loop.request_undefer(task_id)
        return {"undeferring": True, "taskId": task.get("id")}

    @app.post("/pause")
    async def pause():
        if finished():
            raise problem(409, "job finished")
        loop.pause()
        return {"paused": True}

    @app.post("/answer")
    async def answer(body: dict | None = None):
        # v0.36 C.2: the ONE route that answers an `awaiting_input` findings
        # park; `ralphctl answer` and the hub panel both post here, and all
        # three reach `LoopSupervisor.answer`. Body (all optional):
        # `{"waive": ["F1"], "accept": ["F2"], "cureRounds": 1,
        #   "approaches": 1, "note": "...", "name": "slug"}`.
        if finished():
            raise problem(409, "job finished", "there is nothing to answer")
        body = body or {}
        try:
            return loop.answer(
                waive=body.get("waive"), accept=body.get("accept"),
                cure_rounds=body.get("cureRounds"),
                approaches=body.get("approaches"), note=body.get("note"),
                note_name=body.get("name"))
        except AnswerRefused as exc:
            raise problem(exc.status, "answer refused", exc.message)

    @app.post("/resume")
    async def resume():
        # v0.36 D.2: an `awaiting_input` run is left by an answer, never by a
        # bare unpause -- "carry on with what the engine would do" is the
        # input timeout's outcome, or `ralphctl answer <run> --cure-rounds 0`.
        if run.read_status().get("state") == AWAITING_INPUT_STATE:
            raise problem(
                409, "run is awaiting input",
                "an `awaiting_input` run is answered, not unpaused: run "
                f"`ralphctl answer {loop.cfg.run_id}` (add `--cure-rounds 0` to "
                "carry on with what the engine would do)")
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
                "'degraded' with a populated infraWait, or a populated "
                "rateLimitWait (a rate-limit wait); use /resume to release "
                "an operator pause")
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
