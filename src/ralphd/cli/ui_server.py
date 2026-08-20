"""`ralphctl ui` — local hub HTTP server (PRD reqs 21-22).

Deliberately stdlib-only (`http.server` + `urllib`), same spirit as
`main.py`'s doc string: this is a CLI-side feature and must not force
`fastapi`/`uvicorn` (already dependencies of the engine side, but not
needed here) onto the `ralphctl ui` path.

Serves two things:
  - JSON endpoints under `/api/...` reading `<registry>/runs/*` and
    proxying a run's *live* container API when it is reachable, degrading
    gracefully (never raising into a 500, never hanging past a short
    timeout) when it is not -- including the log tail (task 039), the
    PRD (task 056) and the steering history (task 016), which all fall
    back to reading the run dir on disk so a dead run stays readable.
    One read is on-disk ONLY by design: `GET
    /api/runs/<id>/iterations/<n>` (task 020), because the engine writes
    `iterations/NNNN/meta.json` and the transcript into the run dir
    itself -- see `iteration_view`.
    Control routes are proxies too: `POST
    /api/runs/<id>/steer` -> the run's `/steering`, and (task 017) `POST
    /api/runs/<id>/retry` -> the run's `/retry`, behind the hub's "retry
    now" button on a degraded run-detail card.
  - The static hub bundle (plain HTML/JS/CSS, no build step) from the
    `web/` directory next to this file (task 034: run list, run detail
    with task table/iteration timeline/live log tail/steering
    form/usage-cost, packaged in the wheel). Non-`/api` paths that don't
    match a real file fall back to `index.html` (SPA-style client-side
    routing).

See docs/cli.md's "ralphctl ui" section for the exact endpoint shapes.
"""

from __future__ import annotations

import json
import mimetypes
import socket
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from ..engine.state import (
    NONTERMINAL_STATES,
    STEERING_APPLIED,
    STEERING_PENDING,
    TASKS_STALE_LABEL,
    format_approach,
    format_cost,
    format_iteration_log_header,
    format_local_time,
    iteration_detail,
    iteration_summary_lines,
    prd_path,
    read_tasks_doc,
    steering_entries,
    tasks_read_notice,
)
from ..log_merge import NO_TRANSCRIPT, iteration_lines, merged_lines
from .log_render import new_render_state, render_to_lines

STATIC_DIR = Path(__file__).parent / "web"

DEFAULT_LOG_TAIL = 200

# Task 056 (#1): shown by the hub's PRD dialog when a run dir has no PRD at
# all (same discipline as `log_merge.NO_TRANSCRIPT`: the wording lives
# server-side, never spelled out again in app.js).
NO_PRD = "(no PRD recorded)"

# Task 016 (#17): shown by the hub's steering panel when an operator has
# never steered this run -- same discipline as `NO_PRD`/`NO_TRANSCRIPT`, the
# wording lives server-side and app.js only renders it.
NO_STEERING = "(no steering messages)"

# Task 024 (#8): how long the run-list liveness probe waits for a run's API
# port to accept a TCP connection. Deliberately tiny: the API is published on
# loopback, so a live run answers in microseconds and a dead one is refused
# instantly -- this timeout only bounds the pathological "port filtered,
# nothing answers at all" case.
API_PROBE_TIMEOUT_S = 0.3


def _read_json(path: Path, default=None):
    """Read one small JSON document off disk, defaulting when it is absent or
    mid-write.

    Deliberately NOT for `tasks.json` (task 004, #15): collapsing a
    `JSONDecodeError` into `{"tasks": []}` is exactly how "the plan vanished
    for one poll cycle" reached the hub table, and the hardened reader
    (`engine.state.read_tasks_doc`) exists to distinguish absent from
    mid-write. The guard is a hard error rather than a comment so a future
    caller cannot quietly reintroduce the bug -- the run dir's own
    `.tasks-last-good.json` is not read through here either, it belongs to
    the reader.
    """
    if path.name == "tasks.json":
        raise ValueError(
            "read tasks.json through engine.state.read_tasks_doc(persist=False), "
            "not _read_json: an unparseable plan must not become an empty one")
    try:
        return json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return default


def host_meta(reg: Path, run_id: str) -> dict:
    return _read_json(reg / "runs" / run_id / "host.json", {}) or {}


def _api_reachable(reg: Path, run_id: str, timeout: float = API_PROBE_TIMEOUT_S) -> bool:
    """Task 024 (#8): does the run's container API accept a connection?

    A bare TCP connect (no HTTP request, no auth) to the port `host.json`
    records -- the cheapest possible "is the engine still there" question,
    which is all the run list needs in order to tell a healthy running run
    from one whose container died without recording a terminal state.

    Deliberately NOT `docker inspect`: the hub is a read-only viewer that
    must work without the docker CLI (`run_list`'s contract, asserted by
    tests/test_cli_ui.py), and "the API answers" is exactly the fact the
    run-detail proxy already reports as `live`.
    """
    api_url = host_meta(reg, run_id).get("apiUrl")
    if not api_url:
        return False
    parts = urlsplit(api_url)
    host = parts.hostname
    if not host:
        return False
    port = parts.port or (443 if parts.scheme == "https" else 80)
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def container_gone(status: dict, api_reachable: bool) -> bool:
    """Task 024 (#8): the hub's form of the zombie condition -- status.json
    still records a non-terminal state (`NONTERMINAL_STATES`, the same set
    `ralphctl status`/`doctor`/`repair` use) while the run's API does not
    answer. The engine never got to write a terminal state, so without this
    the hub renders such a run EXACTLY like a healthy running one.

    The CLI can go one better and ask docker whether the container still
    exists; the hub stops at "the API is unreachable" (no docker dependency),
    so the UI wording says "appears gone" and points at `ralphctl repair` for
    the authoritative diagnosis instead of claiming certainty it lacks.
    """
    return not api_reachable and status.get("state") in NONTERMINAL_STATES


def _row_tasks(run_dir: Path) -> dict:
    """Task 013 (#21): the run-list row's task-progress fields, from ONE local
    read of that run's `tasks.json`.

    The read goes through the engine's hardened reader
    (`engine.state.read_tasks_doc`, task 002) with `persist=False` -- so a poll
    landing inside the agent's rewrite of the plan shows the last plan that
    parsed, flagged `tasksStale`, instead of the column blinking to nothing,
    and the hub still writes nothing into somebody else's run dir.

    Deliberately NO live proxy call: the run list's contract is local reads
    only (see `run_list`), so listing N runs cannot cost N HTTP round trips --
    and the fraction must be there for a finished run whose container is long
    gone, which is exactly when a live call cannot help.

    Rendered server-side by the shared formatters, the discipline of
    `approachDisplay`/`costDisplay`: `tasksDisplay` (`5/7`, empty for a
    plan-less run -- never `0/0`), `tasksSummary` (`ralphctl status`' exact
    sentence) and `tasksTrouble` (`['1 validation-failed']`, worded exactly as
    that sentence words it). The raw counts travel alongside so the browser can
    sort numerically on progress rather than on the rendered string.

    Task 015 (#21): the field set itself is `TasksRead.row_fields`, shared with
    `ralphctl runs` -- the two surfaces that list runs cannot disagree about a
    run's progress, because there is only one place the row is built. The READ
    stays here (one per row, `persist=False`, no proxy call).
    """
    return read_tasks_doc(run_dir, persist=False).row_fields


def run_list(reg: Path) -> list[dict]:
    """Run list view (PRD req 21): state/verdict/phase/iterations per run,
    read from `<registry>/runs/*/status.json` only -- no live proxy calls
    (would make listing N runs take N round trips).

    Task 024 (#8) adds `containerGone` per row, which does need to know
    whether the API answers. That stays within the spirit of the rule above:
    only runs whose *recorded* state is non-terminal are probed (a finished
    run cannot be a zombie), the probe is a loopback TCP connect rather than
    an HTTP round trip, and the probes run concurrently -- so the sweep costs
    one short timeout in the worst case, not N.

    Task 013 (#21) adds the TASKS fields, which are also strictly local: one
    `read_tasks_doc` per row (see `_row_tasks`), never a `GET /tasks` proxy
    call -- so the fraction is just as available for a run whose container is
    gone as for a live one."""
    rows = []
    runs_dir = reg / "runs"
    if runs_dir.is_dir():
        for d in sorted(runs_dir.iterdir()):
            if not d.is_dir():
                continue
            status = _read_json(d / "status.json", {}) or {}
            rows.append({
                "runId": d.name,
                "state": status.get("state"),
                "verdict": status.get("verdict"),
                "phase": status.get("phase"),
                # Task 008 (#16): the raw counter stays exactly as it was (the
                # hub sorts the APPROACH column numerically on it) and the
                # denominator travels alongside it, rendered once here by the
                # shared formatter -- see `_with_approach_display`.
                "approach": status.get("approach"),
                "maxApproaches": status.get("maxApproaches"),
                "approachDisplay": format_approach(status.get("approach"),
                                                   status.get("maxApproaches")),
                "iterationsUsed": status.get("iterationsUsed"),
                "iterationsBudget": status.get("iterationsBudget"),
                "startedAt": status.get("startedAt"),
                "containerGone": False,
                # Task 013 (#21): task progress per row, one local hardened
                # read each -- see `_row_tasks`.
                **_row_tasks(d),
            })
    maybe_zombies = [r for r in rows if r["state"] in NONTERMINAL_STATES]
    if maybe_zombies:
        with ThreadPoolExecutor(max_workers=min(8, len(maybe_zombies))) as pool:
            for row, reachable in zip(
                    maybe_zombies,
                    pool.map(lambda r: _api_reachable(reg, r["runId"]), maybe_zombies)):
                row["containerGone"] = not reachable
    return rows


def _proxy_json(reg: Path, run_id: str, method: str, path: str,
                 body: bytes | None = None, timeout: float = 5.0):
    """Forward a request to the run's live container API, expecting a JSON
    response. Returns (ok, status_code, obj). Never raises -- a dead/gone
    run degrades to (False, 0, None) so callers can fall back to on-disk
    state instead of the hub crashing."""
    meta = host_meta(reg, run_id)
    api_url = meta.get("apiUrl")
    if not api_url:
        return False, 0, None
    req = urllib.request.Request(api_url + path, method=method, data=body)
    token_file = reg / "runs" / run_id / ".api-token"
    if token_file.exists():
        req.add_header("Authorization", f"Bearer {token_file.read_text().strip()}")
    if body is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = resp.read()
            return True, resp.status, (json.loads(data) if data else {})
    except urllib.error.HTTPError as e:
        try:
            detail = json.loads(e.read())
        except (json.JSONDecodeError, ValueError):
            detail = {"detail": str(e)}
        return False, e.code, detail
    except (urllib.error.URLError, TimeoutError, OSError):
        return False, 0, None


def _proxy_text(reg: Path, run_id: str, path: str, timeout: float = 5.0):
    """Same as `_proxy_json` but for the (text/plain NDJSON) `/logs`
    endpoint. Returns (ok, text)."""
    meta = host_meta(reg, run_id)
    api_url = meta.get("apiUrl")
    if not api_url:
        return False, ""
    req = urllib.request.Request(api_url + path, method="GET")
    token_file = reg / "runs" / run_id / ".api-token"
    if token_file.exists():
        req.add_header("Authorization", f"Bearer {token_file.read_text().strip()}")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return True, resp.read().decode(errors="replace")
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError):
        return False, ""


def rendered_log_lines(reg: Path, run_id: str, tail: int | None) -> tuple[bool, list[str]]:
    """Server-side pretty-render of a run's log tail (task 014), through
    the EXACT SAME `log_render.render_to_lines` function `ralphctl logs`
    uses -- so the hub UI never reimplements event-to-text rendering and
    a many-delta thinking block collapses to exactly one '[thinking…]'
    line here too (the `thinking_seen` guard lives in `log_render`, not
    in this function or in JS).

    Mirrors the CLI's non-follow `cmd_logs` tail contract (task 057): `N`
    means N RENDERED lines, not N raw NDJSON events -- trimming raw events
    before rendering (as the engine's own `GET /logs?tail=` does) would
    yield a much-smaller, wildly variable visible line count once the
    renderer collapses/skips event types. So this always fetches the FULL
    raw backlog from the run's live API (no `tail` query param -- the
    engine's own default, `tail=0`, means unlimited), renders every line,
    THEN trims to the last `tail` rendered lines.

    `tty=False` is passed to `render_to_lines` unconditionally: the hub
    displays plain text via the DOM (each line becomes its own text node,
    task 014), so ANSI color codes would just be inserted as visible
    garbage -- and passing `tty=False` also guarantees the returned lines
    never contain `\r`/ANSI control bytes (task 004's piped-output
    contract extends naturally to this server-side non-TTY caller).

    Returns `(live, lines)`. Task 039 (#6): `live=False` no longer means
    "no lines" -- an unreachable run (its container died, or it finished
    long ago) falls back to the ON-DISK merge, `log_merge.merged_lines`,
    which is the very same merge the engine's `GET /logs` serves from the
    inside. So the hub can still show a dead run's transcript; only the
    *follow* part needs the container. Callers/UI must therefore label
    `live: false` output as an on-disk snapshot (app.js does, in the same
    wording style as the detail card's `live` row) rather than treating it
    as "nothing to show".

    Host-side reads pass no `scrub` -- see `log_merge`'s doc string and
    docs/architecture.md's redaction section for that decision (the bytes
    on disk were already scrubbed at write time by `runner.py`).

    Task 041 (#6): an empty render answers with the single explicit
    `log_merge.NO_TRANSCRIPT` line rather than `[]`, so the hub's log box
    says *why* it is empty (a run whose `iterations/` dir has nothing in it
    yet) instead of just looking broken -- and says it in the same words
    `ralphctl logs` uses, because the wording lives in `log_merge`, not
    here and not in app.js.
    """
    ok, raw_text = _proxy_text(reg, run_id, "/logs")
    if not ok:
        raw_text = "".join(merged_lines(reg / "runs" / run_id))
    lines = render_to_lines(raw_text, tty=False, state=new_render_state())
    if tail:
        lines = lines[-tail:]
    return ok, lines or [NO_TRANSCRIPT]


def iteration_view(reg: Path, run_id: str, number: int, *,
                   log: bool = True) -> dict | None:
    """One iteration's whole story for the hub's iteration dialog (task 020,
    #18.1), or None when the run dir holds no such iteration (caller -> 404).

    The payload is `engine.state.iteration_detail` -- the ONE shaping of
    `iterations/NNNN/meta.json`, shared with `ralphctl iteration` (task 019) --
    plus that iteration's transcript rendered by the SAME server-side renderer
    the log tail uses (`rendered_log_lines`' `log_render.render_to_lines`), and
    `text`: the complete dialog body, i.e. `state.iteration_summary_lines`
    followed by `state.format_iteration_log_header` and the rendered lines.

    So every string the browser shows was formatted in Python by the same
    functions `ralphctl iteration` prints (the `costDisplay`/`startedAtLocal`
    discipline, applied to a block of text): app.js only puts `text` into a
    text node.

    Deliberately NO live-API branch, unlike the log tail / PRD / steering
    endpoints: `meta.json` and the transcript are written by the engine itself
    into the run dir, so the on-disk copy is authoritative even while the
    container is alive (see `state.iteration_detail`) -- there is nothing to
    fall back FROM, hence no `live` flag and no snapshot notice to render.
    An empty render still answers with `log_merge.NO_TRANSCRIPT` rather than
    `[]`, so the dialog says *why* it shows no log.
    """
    run_dir = reg / "runs" / run_id
    detail = iteration_detail(run_dir, number)
    if detail is None:
        return None
    summary = iteration_summary_lines(detail)
    body = list(summary)
    lines: list[str] | None = None
    if log:
        raw = "".join(iteration_lines(run_dir, number))
        lines = render_to_lines(raw, tty=False, state=new_render_state()) \
            or [NO_TRANSCRIPT]
        body += [format_iteration_log_header(len(lines)), *lines]
    out = {"runId": run_id, **detail, "summaryLines": summary,
           "text": "\n".join(body)}
    # `log` is absent (not empty) when the caller asked for no transcript --
    # an empty list would claim the iteration produced none (`ralphctl
    # iteration --no-log`'s own rule).
    if lines is not None:
        out["log"] = lines
    return out


def prd_text(reg: Path, run_id: str) -> tuple[bool, str]:
    """A run's PRD for the hub's PRD dialog (task 056, #1).

    Exactly the shape `rendered_log_lines` established for the log tail
    (tasks 038/039): ask the run's LIVE API first (`GET /prd`, which is
    where a still-running job's composite PRD is authoritative), and fall
    back to reading the run dir directly when that API does not answer --
    a finished or dead run's PRD is sitting right there on disk, so the
    dialog must not be live-only. Which file counts as "the PRD"
    (`composite-prd.md` when present, else `prd.md`) is decided by the ONE
    shared helper `engine.state.prd_path`, the same one the engine's route
    uses, so the live and on-disk answers cannot diverge.

    Returns `(live, text)`; `text` is `NO_PRD` when there is nothing to show,
    never an empty string, so the dialog says *why* it is empty (the same
    reason `log_merge.NO_TRANSCRIPT` exists). Host-side reads pass no scrub
    -- see docs/architecture.md's redaction section.
    """
    ok, text = _proxy_text(reg, run_id, "/prd")
    if not ok:
        f = prd_path(reg / "runs" / run_id)
        try:
            text = f.read_text(errors="replace") if f is not None else ""
        except OSError:
            text = ""
    return ok, text if text.strip() else NO_PRD


def steering_list(reg: Path, run_id: str, *, bodies: bool = True) -> tuple[bool, list[dict]]:
    """A run's steering messages for the hub (task 016, #17).

    The shape the log tail (tasks 038/039) and the PRD dialog (task 056)
    established: ask the run's LIVE API first (`GET /steering`, authoritative
    for a running job -- it is the process that decides when an entry becomes
    *applied*) and fall back to reading `<run>/steering/` directly when that
    API does not answer, so a finished or dead run's steering history stays
    readable. Both sides go through the ONE shared reader
    (`engine.state.steering_entries`), so the live and on-disk answers are the
    same entries for the same run rather than two vocabularies.

    A pre-v0.6 engine's `GET /steering` answers with only `file`/`consumed`.
    Those entries are COMPLETED from the run dir on disk (the hub is running
    on the host that holds it) instead of being served half-empty: the live
    side keeps deciding which files exist and which are applied, disk only
    supplies the fields it has always had -- the name, arrival timestamp and
    body of a file the live answer already named.

    Returns `(live, entries)`; `entries` is `[]` for a run nobody ever
    steered (the caller pairs that with `NO_STEERING`).

    Task 017 (#17): every entry also carries `tsLocal`, its arrival time
    rendered by the ONE shared absolute-timestamp formatter
    (`engine.state.format_local_time`) -- see `_with_steering_display`, the
    same discipline as `startedAtLocal` on the detail card.
    """
    run_dir = reg / "runs" / run_id
    disk = {e["file"]: e for e in steering_entries(run_dir, bodies=bodies)}
    ok, _, resp = _proxy_json(reg, run_id, "GET", "/steering")
    if not (ok and isinstance(resp, list)):
        return False, [_with_steering_display(e) for e in disk.values()]
    entries = []
    for item in resp:
        if not isinstance(item, dict) or not isinstance(item.get("file"), str):
            continue
        live_fields = {k: v for k, v in item.items() if v is not None}
        base = dict(disk.get(item["file"], {}))
        # The LIVE side owns applied-ness. A pre-v0.6 answer states it as
        # `consumed` only, so disk's `state` must not survive the merge and
        # contradict it (nor the other way round for a `state`-only answer);
        # whichever key the live answer omits is re-derived below from the
        # one it sent, in `_normalized_steering`.
        if "consumed" in live_fields and "state" not in live_fields:
            base.pop("state", None)
        if "state" in live_fields and "consumed" not in live_fields:
            base.pop("consumed", None)
        merged = {**base, **live_fields}
        if not bodies:
            merged.pop("body", None)
        entries.append(_with_steering_display(_normalized_steering(merged)))
    return True, entries


def _normalized_steering(entry: dict) -> dict:
    """Fill in what a pre-v0.6 live answer does not send, from what it does.

    `state` is derived from `consumed` (and vice versa) rather than left
    missing, because "pending or applied" is the one thing the panel must
    always be able to say; `name` falls back to the file name. Nothing is
    invented that the entry does not already imply -- an entry with no `ts`
    and no `body` on disk keeps having none (the run dir is gone, and
    claiming a timestamp would be a guess).
    """
    out = dict(entry)
    if "consumed" not in out and "state" in out:
        out["consumed"] = out["state"] != STEERING_PENDING
    if "state" not in out:
        out["state"] = STEERING_APPLIED if out.get("consumed") else STEERING_PENDING
    out.setdefault("name", out.get("file"))
    return out


def _with_steering_display(entry: dict) -> dict:
    """Task 017 (#17): attach `tsLocal`, the entry's arrival time as a string,
    formatted server-side by `engine.state.format_local_time`.

    The discipline of `_with_local_times`/`_with_cost_display`/
    `_with_approach_display`: the browser renders a string Python formatted,
    so "local" means the host running ralphd and the hub cannot grow a second
    timestamp vocabulary that drifts from `ralphctl`'s.

    Always recomputed from the entry's own `ts` (a forged `tsLocal` in a
    proxied payload cannot claim an arrival time the timestamp does not
    support), and *absent* when there is no `ts` at all -- a live entry naming
    a file the hub cannot see has no arrival time, and `format_local_time`'s
    `"n/a"` would read like one.
    """
    if not isinstance(entry, dict):
        return entry
    out = {k: v for k, v in entry.items() if k != "tsLocal"}
    if out.get("ts"):
        out["tsLocal"] = format_local_time(out["ts"])
    return out


# Task 048 (#4): absolute timestamps are formatted HERE, server-side, by the
# one shared formatter (`engine/state.format_local_time`) instead of being
# re-implemented in `web/app.js` -- the hub then renders the string as-is
# (textContent). The raw ISO fields are left completely untouched alongside
# the added `*Local` ones, so machine consumers and any client-side sorting
# (task 054) still have the exact wire values.
_LOCAL_TIME_FIELDS = ("startedAt", "endedAt", "updatedAt")


def _with_local_times(doc: dict) -> dict:
    if not isinstance(doc, dict):
        return doc
    out = dict(doc)
    for field in _LOCAL_TIME_FIELDS:
        if doc.get(field):
            out[field + "Local"] = format_local_time(doc[field])
    return out


def _with_cost_display(doc: dict) -> dict:
    """Task 051 (#10): attach the hub's *rendered* cost strings server-side,
    exactly like `_with_local_times` does for timestamps -- computed by the one
    shared formatter (`engine/state.format_cost`) so the hub, `ralphctl status`
    and the `ralphctl logs` footer word an unpriced/mixed total identically and
    none of them can render `$0.0000` for a cost nobody knows.

    `costDisplay` is *added* to the usage total and to every byPhase/byApproach
    bucket; the raw `costUSD`/`costStatus` fields are left untouched for
    machine consumers, and a bucket with no cost information at all gets no
    `costDisplay` (app.js then falls back to its own number rendering).
    """
    usage = doc.get("usage")
    if not isinstance(usage, dict):
        return doc

    def rendered(bucket):
        if not isinstance(bucket, dict):
            return bucket
        display = format_cost(bucket, decimals=4)
        return {**bucket, "costDisplay": display} if display is not None else dict(bucket)

    out_usage = rendered(usage)
    for key in ("byPhase", "byApproach"):
        buckets = usage.get(key)
        if isinstance(buckets, dict):
            out_usage[key] = {k: rendered(v) for k, v in buckets.items()}
    return {**doc, "usage": out_usage}


def _with_approach_display(doc: dict) -> dict:
    """Task 008 (#16): attach the rendered approach counter (`2/3`) to a status
    doc server-side, by the same one shared formatter `ralphctl status`/`runs`
    use (`engine.state.format_approach`) -- the discipline of
    `_with_local_times`/`_with_cost_display`/`_with_tasks_read_label`: the
    browser displays a string the server formatted, so the hub cannot grow a
    second denominator vocabulary that drifts from the CLI's.

    Always computed from the doc's OWN `approach`/`maxApproaches` and always
    written (empty string when there is no approach), so a forged
    `approachDisplay` in a proxied payload cannot claim a ladder position that
    the counter fields do not support. A live answer from a pre-v0.6 engine
    carries no `maxApproaches`, which `format_approach` renders as a bare `2`
    rather than guessing this host's configured limit.
    """
    if not isinstance(doc, dict):
        return doc
    return {**doc, "approachDisplay": format_approach(doc.get("approach"),
                                                     doc.get("maxApproaches"))}


def _with_tasks_read_label(tasks: dict) -> dict:
    """Task 005 (#15): render the read's provenance into the two display
    strings the browser shows -- `tasksLabel` (the short badge, e.g. `stale`)
    and `tasksNotice` (the full sentence) -- server-side, from the one home
    for that wording (`engine.state.tasks_read_notice` / `TASKS_STALE_LABEL`).

    Same discipline as `usage.costDisplay` and `startedAtLocal`: `app.js`
    displays strings the server formatted instead of re-spelling engine
    wording in JS, which is how a second, drifting vocabulary gets born.

    Derived strictly FROM the `tasksStale`/`tasksSource` flags already in the
    payload (whether it came off disk or verbatim from a live `GET /tasks`),
    so nothing is invented for a pre-v0.6 engine that sends no flags -- it
    gets no label, and the table renders exactly as it does today. Both keys
    are also *removed* when the read is not stale, so a task doc that carries
    forged `tasksNotice`/`tasksLabel` keys of its own cannot make a fresh
    plan look stale (the same reason task 003 appends the contract last).
    """
    notice = tasks_read_notice(tasks.get("tasksSource"), bool(tasks.get("tasksStale")))
    out = {k: v for k, v in tasks.items() if k not in ("tasksNotice", "tasksLabel")}
    if notice:
        out["tasksNotice"] = notice
        out["tasksLabel"] = TASKS_STALE_LABEL
    return out


def run_detail(reg: Path, run_id: str) -> dict | None:
    """Run detail view (PRD req 21): task table + iteration timeline data,
    live where possible, falling back to the on-disk snapshot for a dead
    run. Returns None if the run doesn't exist at all (caller -> 404)."""
    run_dir = reg / "runs" / run_id
    if not run_dir.is_dir():
        return None
    status = _read_json(run_dir / "status.json", {}) or {}
    ok_s, _, live_status = _proxy_json(reg, run_id, "GET", "/status")
    if ok_s and live_status:
        status = live_status

    # Task 004 (#15): the on-disk path goes through the engine's hardened
    # reader, so a poll that lands inside an agent's rewrite of tasks.json
    # serves the last plan that parsed (flagged `tasksStale`) instead of an
    # empty table. `persist=False`: the hub is a read-only viewer of somebody
    # else's run dir and must not write a last-good cache into it.
    #
    # The flags travel as the same `tasksStale`/`tasksSource` fields a live
    # `GET /tasks` already carries (task 003), so app.js has one contract to
    # render whichever side served the payload. A live answer is passed
    # through verbatim -- including a pre-v0.6 engine's answer, which carries
    # no flags at all; inventing `tasksStale: false` there would claim
    # freshness nobody vouched for.
    tasks_read = read_tasks_doc(run_dir, persist=False)
    tasks = {**tasks_read.doc, **tasks_read.contract}
    # The payload has always carried a `tasks` list for a plan-less run
    # (app.js renders `d.tasks.tasks`); with `tasksSource: "absent"` alongside
    # it, an empty list here is a stated fact rather than a swallowed error.
    tasks.setdefault("tasks", [])
    ok_t, _, live_tasks = _proxy_json(reg, run_id, "GET", "/tasks")
    if ok_t and isinstance(live_tasks, dict):
        tasks = live_tasks
    tasks = _with_tasks_read_label(tasks)

    iterations = []
    itroot = run_dir / "iterations"
    if itroot.is_dir():
        for d in sorted(itroot.iterdir()):
            meta = _read_json(d / "meta.json")
            if meta is not None:
                iterations.append(_with_local_times(meta))

    return {
        "runId": run_id,
        "live": ok_s,
        # Task 024 (#8): `live: false` alone is not the story -- a *finished*
        # run is unreachable by design. Paired with a non-terminal recorded
        # state it means the container died without recording a terminal
        # state, which the card renders with the warning treatment.
        "containerGone": container_gone(status, ok_s),
        "status": _with_approach_display(_with_cost_display(_with_local_times(status))),
        "tasks": tasks,
        "iterations": iterations,
    }


class Handler(BaseHTTPRequestHandler):
    """Bound to a registry path via `make_handler_class()`."""

    registry: Path = Path.home() / ".ralphd"
    server_version = "ralphctl-ui/1"

    def log_message(self, fmt, *args):  # quiet by default -- no per-request spam
        pass

    # -- response helpers -------------------------------------------------

    def _send_json(self, obj, code=200):
        data = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _send_bytes(self, data: bytes, code=200, content_type="application/octet-stream"):
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _read_body(self) -> bytes:
        length = int(self.headers.get("Content-Length", "0") or "0")
        return self.rfile.read(length) if length else b""

    # -- routing ------------------------------------------------------------

    def do_GET(self):
        parts = urlsplit(self.path)
        segs = [s for s in parts.path.split("/") if s]
        qs = parse_qs(parts.query)
        reg = self.registry
        try:
            if segs == ["api", "runs"]:
                self._send_json({"runs": run_list(reg)})
                return
            if len(segs) == 4 and segs[:2] == ["api", "runs"] and segs[3] == "logs":
                run_id = segs[2]
                if not (reg / "runs" / run_id).is_dir():
                    self._send_json({"error": f"run {run_id} not found"}, 404)
                    return
                tail = qs.get("tail", [str(DEFAULT_LOG_TAIL)])[0]
                try:
                    tail_n = int(tail)
                except ValueError:
                    tail_n = DEFAULT_LOG_TAIL
                live, lines = rendered_log_lines(reg, run_id, tail_n)
                self._send_json({"live": live, "lines": lines})
                return
            if len(segs) == 4 and segs[:2] == ["api", "runs"] and segs[3] == "prd":
                # Task 056 (#1): the hub's PRD dialog. Live-first with an
                # on-disk fallback, exactly like the log tail above.
                run_id = segs[2]
                if not (reg / "runs" / run_id).is_dir():
                    self._send_json({"error": f"run {run_id} not found"}, 404)
                    return
                live, text = prd_text(reg, run_id)
                self._send_json({"live": live, "text": text})
                return
            if len(segs) == 4 and segs[:2] == ["api", "runs"] and segs[3] == "steering":
                # Task 016 (#17): the run's steering history for the hub's
                # steering panel (task 017 renders it). Live-first with an
                # on-disk fallback, exactly like the log tail and the PRD
                # above -- see `steering_list`.
                run_id = segs[2]
                if not (reg / "runs" / run_id).is_dir():
                    self._send_json({"error": f"run {run_id} not found"}, 404)
                    return
                live, entries = steering_list(reg, run_id)
                self._send_json({"live": live, "entries": entries,
                                 "notice": "" if entries else NO_STEERING})
                return
            if (len(segs) == 5 and segs[:2] == ["api", "runs"]
                    and segs[3] == "iterations"):
                # Task 020 (#18.1): one iteration's header + transcript for
                # the hub's iteration dialog. Purely on-disk (see
                # `iteration_view`): meta.json is the engine's own atomic
                # write, so there is no live answer to prefer.
                run_id = segs[2]
                if not (reg / "runs" / run_id).is_dir():
                    self._send_json({"error": f"run {run_id} not found"}, 404)
                    return
                try:
                    number = int(segs[4])
                except ValueError:
                    self._send_json({"error": f"bad iteration number {segs[4]!r}"}, 404)
                    return
                view = iteration_view(reg, run_id, number,
                                      log=qs.get("log", ["1"])[0] != "0")
                if view is None:
                    self._send_json(
                        {"error": f"run {run_id} has no iteration {number}"}, 404)
                    return
                self._send_json(view)
                return
            if len(segs) == 3 and segs[:2] == ["api", "runs"]:
                run_id = segs[2]
                detail = run_detail(reg, run_id)
                if detail is None:
                    self._send_json({"error": f"run {run_id} not found"}, 404)
                    return
                self._send_json(detail)
                return
            if segs and segs[0] == "api":
                self._send_json({"error": "no such endpoint"}, 404)
                return
            self._serve_static(parts.path)
        except Exception as e:  # defensive: never let one bad request kill the hub
            self._send_json({"error": str(e)}, 500)

    def do_POST(self):
        parts = urlsplit(self.path)
        segs = [s for s in parts.path.split("/") if s]
        reg = self.registry
        try:
            if len(segs) == 4 and segs[:2] == ["api", "runs"] and segs[3] == "steer":
                run_id = segs[2]
                if not (reg / "runs" / run_id).is_dir():
                    self._send_json({"error": f"run {run_id} not found"}, 404)
                    return
                raw = self._read_body()
                try:
                    body = json.loads(raw) if raw else {}
                except json.JSONDecodeError:
                    self._send_json({"error": "invalid JSON body"}, 400)
                    return
                ok, code, resp = _proxy_json(reg, run_id, "POST", "/steering",
                                              body=json.dumps(body).encode())
                if not ok:
                    self._send_json(
                        {"error": "run's API unreachable or rejected the request",
                         "detail": resp}, code or 503)
                    return
                self._send_json(resp, code)
                return
            if len(segs) == 4 and segs[:2] == ["api", "runs"] and segs[3] == "retry":
                # Task 017 (#5): the hub's "retry now" button on a degraded
                # run-detail card. Pure proxy to the run's own `POST /retry`
                # (docs/api.md), which wakes the pending infra backoff wait
                # and resets the outage-budget episode clock. The engine's
                # own status code is passed THROUGH (notably its 409 "not
                # waiting on an infra fault" refusal, so the UI can say so
                # rather than claiming a generic failure); only an
                # unreachable run collapses to 503, matching the read-only
                # treatment the card already gives a dead run.
                run_id = segs[2]
                if not (reg / "runs" / run_id).is_dir():
                    self._send_json({"error": f"run {run_id} not found"}, 404)
                    return
                ok, code, resp = _proxy_json(reg, run_id, "POST", "/retry", body=b"")
                if not ok and not code:
                    self._send_json(
                        {"error": "run's API is unreachable — cannot retry now"}, 503)
                    return
                self._send_json(resp if isinstance(resp, dict) else {"detail": str(resp)},
                                code)
                return
            self._send_json({"error": "no such endpoint"}, 404)
        except Exception as e:
            self._send_json({"error": str(e)}, 500)

    # -- static bundle (populated by task 034) ------------------------------

    def _serve_static(self, url_path: str) -> None:
        if not STATIC_DIR.is_dir():
            self._send_bytes(
                b"ralphctl ui: static hub bundle not installed in this build\n",
                404, "text/plain")
            return
        rel = url_path.lstrip("/") or "index.html"
        candidate = (STATIC_DIR / rel).resolve()
        if STATIC_DIR.resolve() not in candidate.parents and candidate != STATIC_DIR.resolve():
            self._send_bytes(b"forbidden\n", 403, "text/plain")
            return
        if candidate.is_dir():
            candidate = candidate / "index.html"
        if not candidate.is_file():
            candidate = STATIC_DIR / "index.html"
            if not candidate.is_file():
                self._send_bytes(b"not found\n", 404, "text/plain")
                return
        content_type = mimetypes.guess_type(str(candidate))[0] or "application/octet-stream"
        self._send_bytes(candidate.read_bytes(), 200, content_type)


def make_handler_class(reg: Path) -> type[Handler]:
    return type("BoundHandler", (Handler,), {"registry": reg})


def make_server(reg: Path, bind: str, port: int) -> ThreadingHTTPServer:
    return ThreadingHTTPServer((bind, port), make_handler_class(reg))
