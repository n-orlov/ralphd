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
    Some reads are on-disk ONLY by design: `GET
    /api/runs/<id>/iterations/<n>` (task 020), `GET
    /api/runs/<id>/documents[/<name>]` (task 022), `GET
    /api/runs/<id>/artifacts[/<path>]` (task 024) and `GET
    /api/runs/<id>/fault` (task 026) and `GET /api/runs/<id>/cost`
    (task 028), because the engine,
    the agent and `start` write `iterations/NNNN/meta.json`, the
    transcript, status.json, events.jsonl, the run's state documents and
    everything under `artifacts/` into the run dir / job config dir
    themselves -- see
    `iteration_view`/`document_list`/`artifact_list`/`fault_view`/`cost_view`.
    Control routes are proxies too: `POST
    /api/runs/<id>/steer` -> the run's `/steering`, (task 017) `POST
    /api/runs/<id>/retry` -> the run's `/retry`, behind the hub's "retry
    now" button on a degraded run-detail card, and (task 013, v0.23
    requirement A.2, #224) `POST /api/runs/<id>/rule` -> the run's
    `/rule`, behind the run page's operator-ruling textarea -- a distinct
    proxy from `steer`'s, landing in a distinct on-disk record
    (`engine.state.write_ruling`/`ruling_entries`), never a variant of
    steering, and (task 006) `POST /api/runs/<id>/unpause` -> the run's
    `/resume` -- the SAME engine route `ralphctl unpause`/`cmd_unpause`
    (cli/main.py) calls, so there is no second unpause mechanism.
    `DELETE /api/runs/<id>`
    (task 030) is the one route that is neither a read nor a proxy: it
    performs `ralphctl rm --force`'s removal through the CLI's own
    `remove_run_state`, for a run whose recorded state is terminal and for
    no other -- see `delete_run`/`deletion_refusal` (and
    `deletion_fields`, which tells the browser which of the two answers a
    run would get, so the hub's delete button is enabled exactly when it
    would work).
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
from urllib.parse import parse_qs, unquote, urlsplit

from .. import __version__
from ..engine.config import MUTATING_METHODS, JobConfig
from . import llm_profiles
from ..engine.state import (
    NO_ARTIFACTS,
    NONTERMINAL_STATES,
    STEERING_APPLIED,
    STEERING_PENDING,
    TASK_BLOCKED_FIELD,
    TASK_FAILURE_KINDS_FIELD,
    TASKS_STALE_LABEL,
    TERMINAL_STATES,
    archived_approach_view,
    archived_approaches_list,
    artifact,
    artifact_entries,
    artifact_raw,
    artifact_summary_lines,
    artifact_text,
    cost_breakdown,
    cost_breakdown_text,
    fault_explanation,
    fault_text,
    format_approach,
    format_artifact_size,
    format_cost,
    format_iteration_log_header,
    format_local_time,
    escalation_pause_markers,
    format_provisioning_lines,
    format_run_document_size,
    iteration_detail,
    iteration_detail_text,
    iteration_summary_lines,
    NO_ROUTER_CALLS,
    prd_path,
    read_iteration_prompt,
    read_tasks_doc,
    router_call_entries,
    run_document,
    run_document_summary_lines,
    run_document_text,
    run_documents,
    steering_entries,
    task_blocked_by,
    task_failure_kinds,
    task_totals,
    task_totals_records_attribution,
    task_totals_zero,
    task_transition_history,
    tasks_read_notice,
)
from ..log_merge import (
    NO_TRANSCRIPT,
    iteration_dir,
    iteration_lines,
    iteration_numbers,
    iteration_output_path,
    merged_lines,
)
from .log_render import new_render_state, render_tail_lines, render_to_lines

STATIC_DIR = Path(__file__).parent / "web"

# Task 024 (#54): sent on EVERY hub response (`_send_json`/`_send_bytes`, so
# the static bundle, every JSON API route and the raw artifact byte route
# all carry it -- see the two send helpers below). The hub serves files an
# agent inside the job container wrote (artifacts, notes, PRD text,
# reflection reports) back to a browser at the operator's own origin; this
# is the barrier that stops one of those files turning into script that can
# act against that origin (steal the page's view of OTHER runs, call the
# DELETE endpoint, etc).
#
# No `'unsafe-inline'` anywhere: `index.html`/`app.js` contain no inline
# `<script>`, no `onclick="..."` attributes and no `<style>` blocks or
# `style="..."` HTML attributes (`app.js` only ever sets `.style.display`
# as a DOM property, which CSP's `style-src` does not govern) -- so
# `script-src 'self'` and `style-src 'self'` cost the hub nothing. Every
# resource the page needs (`app.js`, `style.css`, the raw artifact bytes)
# is same-origin, so `default-src 'self'` covers images without a separate
# `img-src` needing to widen anything. `base-uri`/`form-action`/
# `frame-ancestors` are locked to `'none'`/`'self'` defensively even though
# the hub uses none of the three today.
HUB_CSP = (
    "default-src 'self'; "
    "script-src 'self'; "
    "style-src 'self'; "
    "img-src 'self'; "
    "connect-src 'self'; "
    "base-uri 'none'; "
    "form-action 'none'; "
    "frame-ancestors 'none'"
)

DEFAULT_LOG_TAIL = 200

# Task 056 (#1): shown by the hub's PRD dialog when a run dir has no PRD at
# all (same discipline as `log_merge.NO_TRANSCRIPT`: the wording lives
# server-side, never spelled out again in app.js).
NO_PRD = "(no PRD recorded)"

# Task 016 (#17): shown by the hub's steering panel when an operator has
# never steered this run -- same discipline as `NO_PRD`/`NO_TRANSCRIPT`, the
# wording lives server-side and app.js only renders it.
NO_STEERING = "(no steering messages)"

# Task 022 (#18.2): shown by the hub's state-document panel when a run wrote
# none of them (and its job config dir is out of reach) -- the `NO_PRD`/
# `NO_STEERING` discipline again, wording server-side.
NO_DOCUMENTS = "(no state documents on disk)"

# Task 024 (#18.3): the hub's artifacts panel has NO wording of its own -- the
# "this run left nothing behind" line is `state.NO_ARTIFACTS`, i.e. the very
# string `ralphctl artifacts <run> ls` prints, imported above rather than
# respelled here (the `NO_DOCUMENTS`/`NO_PRD` discipline, one step further:
# when the CLI already owns the wording, the hub reuses it).

# Task 024 (#8): how long the run-list liveness probe waits for a run's API
# port to accept a TCP connection. Deliberately tiny: the API is published on
# loopback, so a live run answers in microseconds and a dead one is refused
# instantly -- this timeout only bounds the pathological "port filtered,
# nothing answers at all" case.
API_PROBE_TIMEOUT_S = 0.3

# Task 030 (#19): the hub's delete refusals, worded ONCE here (the
# `NO_PRD`/`NO_STEERING`/`NO_DOCUMENTS` discipline) so the endpoint's response
# and -- task 031 -- the disabled button's explanation are literally the same
# sentence, and so app.js never invents a reason of its own.
DELETE_REFUSED_ACTIVE = ("run is still active (state: {state}) — abort or stop "
                         "it first")
# Task 013 (#56): the `finishing` window (engine/state.py's
# NONTERMINAL_STATES) is NOT "still working the plan" -- the verdict is
# already settled and on disk, only the post-terminal reflect iteration is
# still unwinding. `DELETE_REFUSED_ACTIVE`'s "abort or stop it first" is the
# wrong instruction here (there is nothing left to abort), so this state gets
# its own sentence, checked BEFORE the generic NONTERMINAL_STATES branch in
# `deletion_refusal` -- one constant, read by both the endpoint's 409 and
# `deletion_fields`' disabled button, so they can never say two different
# things about the same refusal.
DELETE_REFUSED_FINISHING = ("run is finishing (state: finishing) — wait for it "
                            "to reach a terminal state, then delete it")
DELETE_REFUSED_UNKNOWN = ("cannot establish that this run has finished (state: "
                          "{state}) — check ralphctl repair, then delete it "
                          "with ralphctl rm --force")
# What a status.json that is absent, unreadable or records something this build
# does not recognize is called in those sentences. It is NOT a state value: it
# is the admission that we have none.
UNKNOWN_STATE = "unknown"

# Recorded in the run's operator-termination marker when the hub deletes a run
# whose job container was still around (`_teardown_container`'s `reason`), so
# the audit trail says WHICH surface took the run down -- the CLI's own
# wording for the same act is "stopped by operator (`ralphctl rm --force`)".
DELETE_TERMINATION_REASON = "stopped by operator (hub delete)"


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


# Task 003 (requirement A, #201): the same host-only, never-mounted-into-
# any-container path `cli.main.control_creds_dir` builds -- kept as a small
# duplicate rather than an import from `cli.main` (which this module never
# imports from, mirroring `host_meta`'s own pre-existing duplication of the
# host-side path convention) so the hub's control sends (#155's flow) read
# `.api-token`/`.control-token` from the same place `ralphctl` itself does.
def _control_creds_dir(reg: Path, run_id: str) -> Path:
    return reg / "control" / run_id


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
                # Task 017 (PRD requirement A.7, #224): the SAME marker
                # `ralphctl runs` renders for a run parked with
                # `pauseReason: "operator_input_needed"` -- see
                # `state.format_pause_marker`'s own docstring for why
                # this ONE reason gets a distinct marker and no other
                # `paused` reason does.
                "pauseReason": status.get("pauseReason"),
                # Task 005 (PRD requirement A.5, #269): `pauseMarker` plus
                # `escalationUndelivered`/`escalationUndeliveredMarker`, the
                # same three facts `ralphctl status` renders (see
                # `state.escalation_pause_markers`'s own docstring, which also
                # covers the in-memory `rejected_incurable` hold) -- read
                # straight off this row's OWN `events.jsonl`, still local-
                # only (no proxy call), the same discipline `_row_tasks`
                # below already follows.
                **escalation_pause_markers(d, status),
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
                # Task 031 (#19): what this row's delete control may promise,
                # decided by the delete endpoint's OWN gate over the same
                # on-disk status doc -- see `deletion_fields`.
                **deletion_fields(status),
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
    token_file = _control_creds_dir(reg, run_id) / ".api-token"
    if token_file.exists():
        req.add_header("Authorization", f"Bearer {token_file.read_text().strip()}")
    # #155: the run's mutating routes (POST /steering, POST /retry, ...)
    # ask for a SECOND, independent secret -- `X-Ralphd-Control-Token`,
    # checked against `JobConfig.control_token` -- which `ralphctl start`/
    # `resume` provisions unconditionally into `.control-token` beside
    # `.api-token` (see main.py's `api()` helper, which forwards it the
    # same way for the CLI's own mutating commands). The hub's proxy is a
    # THIRD caller of that same contract; before this fix it forwarded only
    # `.api-token` and every hub-initiated steer/retry 401'd against a real
    # run.
    if method in MUTATING_METHODS:
        control_file = _control_creds_dir(reg, run_id) / ".control-token"
        if control_file.exists():
            req.add_header("X-Ralphd-Control-Token",
                          control_file.read_text().strip())
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
    token_file = _control_creds_dir(reg, run_id) / ".api-token"
    if token_file.exists():
        req.add_header("Authorization", f"Bearer {token_file.read_text().strip()}")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return True, resp.read().decode(errors="replace")
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError):
        return False, ""


# Task 046 (PRD H.4): the same textual pre-filter `log_render._BOUNDARY_TOKEN`
# uses to LOCATE a `ralphd.iteration` boundary line without fully parsing it
# -- used here only to estimate how many CONTENT (non-boundary) lines a
# live fetch came back with, which is `log_merge.apply_tail`'s own unit for
# "how much backlog was asked for" (its `tail` keeps the last `tail`
# non-boundary lines, plus whatever boundary lines fall inside that
# window). A false match inside some event's own text would only make the
# loop below re-ask once when it did not strictly need to -- harmless,
# since the container answers a saturated request with everything it has
# either way.
_ITERATION_BOUNDARY_TOKEN = '"ralphd.iteration"'

# Task cure-01-08 (PRD G.2/G.6/G.9): a run of consecutive RECOGNIZED-but-
# SILENT raw lines (PRD Q's own shape -- a `message_end` whose content is
# empty/unrecognized, or renders nothing new) between the retained tail and
# whatever collapse-guard setter still needs to be SEEN (not merely looked
# up by the O(1) index -- printing "[thinking\u2026]" happens at the setter
# line itself, never at a seeded boundary) makes `_fetch_live_tail`/
# `_fetch_dead_tail`'s own doubling pay for the SUM of every smaller
# doubling step on top of the one read that actually resolves it --
# measured at 38,087,918 bytes on PRD Q's own fixture, no prelude at all,
# where the theoretical floor (one single non-doubling read of the same
# iteration's whole transcript) is 10,720,887. Past this many doublings a
# further doubling is treated as proof the render needs most of this
# iteration's own backlog anyway, so the caller stops paying for the
# GEOMETRIC SUM of ever-larger partial reads and asks once more for
# effectively the whole remaining backlog instead -- collapsing however
# many further doublings would otherwise have run into ONE non-doubling
# read. `_TAIL_FULL_ASK_BOUND` is far larger than any real transcript's own
# raw line count; `merge_entries_tail`/the live handler still only ever
# reads as many actual bytes as the transcript holds (never more), so
# asking for it costs nothing extra when the backlog is small (a run whose
# real answer sits within the first few doublings, PRD P's own fixtures,
# never reaches this threshold at all -- see docs/reports for the measured
# comparison). This does not, and cannot, bring PRD Q's own fixture (or
# either silent-prelude depth) under the frozen 1,000,000-byte cap: the
# 10,000 padded RECOGNIZED-silent lines a real run of this shape floods
# between the deciding setter and the file's own end are themselves
# ~10,660,000 bytes, an order of magnitude over the whole cap, with no
# further reduction available under the frozen cap, the frozen transcript
# format, an unweakened equality assertion, or without a further side
# index (Ruling 1 section 2's own disclosed-residual disposition, not a
# raised ceiling). It DOES remove the doubling ladder's own SUM-of-smaller-
# reads waste for exactly that pathological shape (roughly halving the
# measured bytes -- see the cure-01-08 commit message for the corrected
# figures), and it changes nothing at all for a request that resolves
# within the first `_TAIL_GROWTH_ROUNDS_BEFORE_FULL_ASK` doublings.
_TAIL_GROWTH_ROUNDS_BEFORE_FULL_ASK = 5
_TAIL_FULL_ASK_BOUND = 10_000_000


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
    renderer collapses/skips event types.

    Task 043 (PRD H.1): a request that names a tail is served by
    `log_render.render_tail_lines`, which renders BACKWARDS from the newest
    events in growing chunks until `N` rendered lines exist, so the work is
    proportional to the tail instead of to the run's whole history (the old
    shape -- render every line, then trim -- is what made a repeated
    `GET /logs` on a long run cost more each time, PRD H's own
    measurement). `tail=0`/`None` still means "the whole transcript" and
    still goes through `render_to_lines`, since there is no tail to bound
    it by. Both paths render through the same `render_log_line`, so the
    lines are the same lines (pinned byte-for-byte by task 044/H.2).

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

    Task 046 (PRD H.4): when `tail` is given, the container is no longer
    asked for its ENTIRE backlog -- `_fetch_live_tail` (nested here, not a
    top-level function, so `_proxy_text` is still only called from this
    one live-answer view, for `test_docs_semantics.py`'s
    `_proxying_hub_views` count) asks `GET /logs?tail=N` for a bound
    derived from the requested rendered-line count, and RE-ASKS with a
    doubled bound whenever the render of what came back still falls short
    of that count and the container's answer suggests there is more
    history behind it (`log_merge.apply_tail`'s own signal: it returned as
    many content lines as were asked for). `tail=0`/`None` still means
    the whole transcript, with no tail to bound the fetch by, so that
    branch is unchanged.

    Task 048 (PRD H.6): the DEAD-run fallback used to call `merged_lines`
    with no tail at all -- reading every iteration transcript the run ever
    recorded, however small the request -- so H.4's live-side bound had a
    still-unbounded twin sitting right next to it. `_fetch_dead_tail` is the
    on-disk mirror of `_fetch_live_tail`: `merged_lines(..., tail=bound)` now
    reads only the newest iterations needed for `bound` raw content lines
    (`log_merge.merge_entries_tail`), and the same growing-bound re-ask loop
    keeps it correct across a many-delta thinking block that collapses raw
    lines into far fewer rendered ones.
    """
    def _fetch_live_tail(bound_tail: int) -> tuple[bool, str]:
        bound = bound_tail
        rounds = 0
        ok, raw_text = _proxy_text(reg, run_id, f"/logs?tail={bound}")
        while ok:
            content_lines = sum(1 for ln in raw_text.splitlines()
                                 if _ITERATION_BOUNDARY_TOKEN not in ln)
            if len(render_tail_lines(raw_text, tty=False, n=bound_tail)) >= bound_tail:
                break
            if content_lines < bound:
                break
            rounds += 1
            bound = (_TAIL_FULL_ASK_BOUND
                     if rounds >= _TAIL_GROWTH_ROUNDS_BEFORE_FULL_ASK
                     else bound * 2)
            ok, raw_text = _proxy_text(reg, run_id, f"/logs?tail={bound}")
        return ok, raw_text

    def _fetch_dead_tail(bound_tail: int) -> str:
        """The on-disk-merge twin of `_fetch_live_tail` (task 048, PRD H.6):
        a dead run's fallback used to call `merged_lines` with NO tail at
        all, reading every iteration transcript the run ever recorded to
        serve even a 10-line request. `merged_lines(..., tail=bound)` now
        reads only the newest iterations needed for `bound` raw content
        lines (`log_merge.merge_entries_tail`), so this grows `bound` the
        exact same way `_fetch_live_tail` grows its live fetch -- doubling
        whenever the render of what came back still falls short of
        `bound_tail` RENDERED lines and there is more history behind it."""
        run_root = reg / "runs" / run_id
        bound = bound_tail
        rounds = 0
        raw_text = "".join(merged_lines(run_root, tail=bound))
        while True:
            content_lines = sum(1 for ln in raw_text.splitlines()
                                 if _ITERATION_BOUNDARY_TOKEN not in ln)
            if len(render_tail_lines(raw_text, tty=False, n=bound_tail)) >= bound_tail:
                break
            if content_lines < bound:
                break
            rounds += 1
            bound = (_TAIL_FULL_ASK_BOUND
                     if rounds >= _TAIL_GROWTH_ROUNDS_BEFORE_FULL_ASK
                     else bound * 2)
            raw_text = "".join(merged_lines(run_root, tail=bound))
        return raw_text

    if tail:
        ok, raw_text = _fetch_live_tail(tail)
        if not ok:
            raw_text = _fetch_dead_tail(tail)
        lines = render_tail_lines(raw_text, tty=False, n=tail)
    else:
        ok, raw_text = _proxy_text(reg, run_id, "/logs")
        if not ok:
            raw_text = "".join(merged_lines(reg / "runs" / run_id))
        lines = render_to_lines(raw_text, tty=False, state=new_render_state())
    return ok, lines or [NO_TRANSCRIPT]


def iteration_view(reg: Path, run_id: str, number: int, *,
                   log: bool = True) -> dict | None:
    """One iteration's whole story for the hub's iteration dialog (task 020,
    #18.1, restructured into labelled regions by #81), or None when the run
    dir holds no such iteration (caller -> 404).

    The payload is `engine.state.iteration_detail` -- the ONE shaping of
    `iterations/NNNN/meta.json`, shared with `ralphctl iteration` (task 019) --
    plus:

      `promptText`   this iteration's own `prompt.md`, the "input" region
                     (`state.read_iteration_prompt`),
      `outcomeText`  the "outcome" region's body, unlabelled
                     (`"\n".join(summaryLines)`, `state.iteration_summary_lines`,
                     unchanged since #18.1),
      `log`/`logsText` that iteration's transcript, rendered by the SAME
                     server-side renderer the log tail uses
                     (`log_render.render_to_lines`) -- the "logs" region, as a
                     list of lines and as one labelled string respectively,
      `summaryLines` the same lines `outcomeText` joins, kept for callers
                     that already read it (task 019),
      `text`         the complete dialog body, all three regions LABELLED and
                     in outcome-first order (`state.iteration_detail_text`).

    So every string the browser shows was formatted in Python by the same
    functions `ralphctl iteration` prints (the `costDisplay`/`startedAtLocal`
    discipline, applied to a block of text): app.js only puts these fields
    into text nodes, never re-wording or re-ordering them.

    Deliberately NO live-API branch, unlike the log tail / PRD / steering
    endpoints: `meta.json`, `prompt.md` and the transcript are written by the
    engine itself into the run dir, so the on-disk copy is authoritative even
    while the container is alive (see `state.iteration_detail`) -- there is
    nothing to fall back FROM, hence no `live` flag and no snapshot notice to
    render. An empty render still answers with `log_merge.NO_TRANSCRIPT`
    rather than `[]`, so the dialog says *why* it shows no log.
    """
    run_dir = reg / "runs" / run_id
    detail = iteration_detail(run_dir, number)
    if detail is None:
        return None
    summary = iteration_summary_lines(detail)
    prompt_text = read_iteration_prompt(run_dir, number)
    lines: list[str] | None = None
    if log:
        raw = "".join(iteration_lines(run_dir, number))
        lines = render_to_lines(raw, tty=False, state=new_render_state()) \
            or [NO_TRANSCRIPT]
    body = iteration_detail_text(detail, prompt_text=prompt_text, log_lines=lines)
    out = {"runId": run_id, **detail, "summaryLines": summary,
           "outcomeText": "\n".join(summary),
           "promptText": prompt_text, "text": "\n".join(body)}
    # `log`/`logsText` are absent (not empty) when the caller asked for no
    # transcript -- an empty list/string would claim the iteration produced
    # none (`ralphctl iteration --no-log`'s own rule, applied to both).
    if lines is not None:
        out["log"] = lines
        out["logsText"] = "\n".join([format_iteration_log_header(len(lines)),
                                     *lines])
    return out


def fault_view(reg: Path, run_id: str) -> dict:
    """Why this run is (or last was) in trouble, for the hub's fault dialog
    (task 026, #18.4): the classification, WHICH row of
    `faults.INFRA_SIGNATURES` matched, how far up the retry ladder the run
    climbed and how much of the outage budget is spent.

    The payload is `engine.state.fault_explanation` -- the ONE join of
    status.json's degraded contract, the episode's `infra_retry` events and the
    last failing iteration's meta.json (task 025) -- plus `text`, i.e.
    `state.fault_text`. So it is byte-for-byte the shape `ralphctl fault <run>
    --json` prints, `summaryLines` and all (the shaping carries them), and the
    dialog body is the same block the CLI prints: app.js only puts `text` into a
    text node, and the two surfaces cannot explain the same fault differently.

    A run that never faulted is NOT an error: the explanation answers
    `hasFault: false` and its `text` is `state.NO_FAULT` -- "nothing went
    wrong" is an answer (the `RUN_DOCUMENT_ABSENT`/`NO_ARTIFACTS` discipline),
    so the badge that opened the dialog never has to lie about being clickable.

    Purely on-disk, like `iteration_view`/`document_list`/`artifact_list` and
    `ralphctl fault`: status.json, events.jsonl and the iteration metas are the
    engine's own writes, so a live run and one whose container is long gone read
    identically -- there is no live answer to prefer and hence no `live` flag
    and no snapshot notice.
    """
    exp = fault_explanation(reg / "runs" / run_id)
    return {"runId": run_id, **exp, "text": fault_text(exp)}


def cost_view(reg: Path, run_id: str) -> dict:
    """What this run spent, per phase and per approach, for the hub's cost
    dialog behind the usage card's cost cell (task 028, #18.5).

    The payload is `engine.state.cost_breakdown` -- the ONE shaping of
    status.json's `usage` buckets (task 027), `summaryLines` and all -- plus
    `text`, i.e. `state.cost_breakdown_text`. So it is byte-for-byte the
    document `ralphctl cost <run> --json` prints and the dialog body is the very
    block `ralphctl cost <run>` prints: app.js only puts `text` into a text
    node, and the two surfaces cannot label the same money differently
    (provider-priced, `~… derived`, a partial subtotal, `unavailable`).

    The headline the dialog hangs off stays the card's own `costDisplay` --
    `format_cost` applied to the same bucket (`cost_breakdown`'s `total`), so
    opening the dialog can never contradict the number that was clicked.

    A run with no usage at all is NOT an error: the breakdown answers
    `hasUsage: false` and its `text` is `state.COST_NO_USAGE` (the
    `NO_FAULT`/`NO_ARTIFACTS` discipline), so the cell is never a lie about
    having a breakdown to show.

    Purely on-disk, like `iteration_view`/`document_list`/`artifact_list`/
    `fault_view` and `ralphctl cost`: status.json is the engine's own atomic
    write, so a live run and one whose container is long gone read identically
    -- there is no live answer to prefer and hence no `live` flag and no
    snapshot notice.
    """
    bd = cost_breakdown(reg / "runs" / run_id)
    return {"runId": run_id, **bd, "text": cost_breakdown_text(bd)}


def _config_dir(reg: Path, run_id: str) -> Path:
    """A run's job config dir, where `job.yaml` (and this run's staged secret
    values) live -- the same `<registry>/configs/<run-id>` layout
    `cli.main.config_root` spells, so the hub and `ralphctl docs` read the very
    same file. Returned even when it is not there: `state.run_documents` then
    reports `job.yaml` as never written rather than out of reach, which is what
    a registry without a config dir for this run actually means."""
    return reg / "configs" / run_id


def _with_document_display(doc: dict) -> dict:
    """Add the one server-rendered display string the document panel needs:
    `sizeDisplay`, i.e. `state.format_run_document_size` -- the byte count, or
    the ONE wording for a document this run never wrote / that is out of reach.
    Same discipline as `usage.costDisplay`/`steering.tsLocal`: app.js renders
    strings, it does not word facts (and a forged `sizeDisplay` in a payload we
    were handed is always recomputed from the entry's own fields)."""
    return {**doc, "sizeDisplay": format_run_document_size(doc)}


def document_list(reg: Path, run_id: str) -> dict:
    """Which state documents this run has, for the hub's document panel (task
    022, #18.2): `notes.md`, `review-findings.md`, `composite-prd.md` and the
    redacted `job.yaml`.

    The listing `ralphctl docs <run>` prints, as a payload: one entry per KNOWN
    document (`state.run_documents`, the ONE shaping) whether or not it exists,
    because *which* documents a run wrote is itself part of the answer -- app.js
    turns the ones that exist into dialog buttons and states the absence of the
    rest in the server's own wording (`sizeDisplay`).

    Bodies are deliberately NOT included: the panel only needs labels, and a
    run's whole notes/PRD/job.yaml on every 4s poll would be pure waste. The
    body arrives when a dialog opens, from `document_view`.

    Purely on-disk, like `iteration_view` (task 020) and `ralphctl docs`: every
    one of these files is written by the agent, the engine or `start` itself
    into directories this host holds, so a live run and one whose container is
    long gone read identically -- there is no live answer to prefer and hence
    no `live` flag and no snapshot notice.
    """
    docs = [_with_document_display(d) for d in
            run_documents(reg / "runs" / run_id, _config_dir(reg, run_id),
                          bodies=False)]
    return {"runId": run_id, "documents": docs,
            "notice": "" if any(d.get("exists") for d in docs) else NO_DOCUMENTS}


def document_view(reg: Path, run_id: str, name: str) -> dict | None:
    """One state document for the hub's dialog, or None when `name` matches no
    known document (caller -> 404).

    `state.run_document` (key OR file name, task 021's aliases) plus the two
    renderings worded ONCE in `engine.state`: `summaryLines` (the header block)
    and `text` (header + separator + body), which is exactly what `ralphctl docs
    <run> <name>` prints. So app.js puts `text` into text nodes and the two
    surfaces cannot describe the same file differently.

    `job.yaml` arrives ALREADY redacted -- `state.run_documents` runs
    `redact.redact_job_yaml` over it at read time, so this endpoint has no raw
    back door and the dialog is as safe to screenshot as `ralphctl docs` output
    is to paste.

    A document this run never wrote is not an error: it answers with the entry
    and `state.RUN_DOCUMENT_ABSENT` as its body, the same as the CLI's wording,
    rather than an empty dialog.
    """
    doc = run_document(reg / "runs" / run_id, name, _config_dir(reg, run_id))
    if doc is None:
        return None
    return {"runId": run_id, **_with_document_display(doc),
            "summaryLines": run_document_summary_lines(doc),
            "text": run_document_text(doc)}


def _with_artifact_display(entry: dict) -> dict:
    """Add the one server-rendered display string the artifacts panel needs:
    `sizeDisplay`, i.e. `state.format_artifact_size` (the same file-size
    vocabulary the document panel uses). Same discipline as
    `_with_document_display`: app.js renders strings, and a `sizeDisplay` that
    arrived in a payload is always recomputed from the entry's own fields."""
    return {**entry, "sizeDisplay": format_artifact_size(entry)}


def artifact_list(reg: Path, run_id: str) -> dict:
    """What a run left behind in `artifacts/`, for the hub's artifacts panel
    (task 024, #18.3): above all the reflect phase's `reflection/report.md` and
    `reflection/suggestions.diff`, which until now could only be read by knowing
    the registry layout and `cat`-ing files on the host.

    The listing `ralphctl artifacts <run> ls` prints, as a payload:
    `state.artifact_entries` (the ONE shaping, task 023) with `sizeDisplay`
    added, so a file cannot be described as missing in one surface and empty in
    the other. Unlike the document panel there is no fixed set of rows -- an
    artifact tree is whatever the agent wrote -- so a run that produced nothing
    is an empty list plus `state.NO_ARTIFACTS`, the CLI's own wording.

    Bodies are deliberately NOT included (`artifact_entries(bodies=False)`): the
    panel needs labels, and shipping a whole reflection report on every 4s poll
    would be pure waste. The body arrives when a dialog opens, from
    `artifact_view`.

    Purely on-disk, like `iteration_view`/`document_list` and `ralphctl
    artifacts`: the agent writes these files into a directory this host holds,
    so a live run and one whose container is long gone read identically --
    there is no live answer to prefer, hence no `live` flag and no snapshot
    notice.
    """
    entries = [_with_artifact_display(e) for e in
               artifact_entries(reg / "runs" / run_id, bodies=False)]
    return {"runId": run_id, "artifacts": entries,
            "notice": "" if entries else NO_ARTIFACTS}


def router_calls_view(reg: Path, run_id: str) -> dict:
    """This run's router calls, for the hub's router panel (task 022, P,
    #80): `state.router_call_entries` read from `artifacts/router/
    decisions.jsonl`, the ONE surface a decision the router made is visible
    on at all (before this, it was a raw NDJSON transcript under
    `artifacts/router/NNNN.jsonl` plus a log line -- correct for the engine's
    own accounting, invisible to an operator).

    Every entry already carries `chargesIteration: False` and `note`
    (`state.ROUTER_CALL_NOTE`) -- the "never a work iteration, never an
    approach" fact is said on the DATA, not just once in this docstring, so
    `ralphctl router` and the hub panel cannot drift on it.

    Purely on-disk, like `artifact_list`/`document_list`: a live run and one
    whose container is long gone read identically, and a run that never
    reached a router-ambiguous state -- every shipped run, today -- answers
    with an empty list plus `state.NO_ROUTER_CALLS`.
    """
    entries = router_call_entries(reg / "runs" / run_id)
    return {"runId": run_id, "routerCalls": entries,
            "notice": "" if entries else NO_ROUTER_CALLS}


def artifact_view(reg: Path, run_id: str, name: str) -> dict | None:
    """One artifact for the hub's dialog, or None when `name` cannot address an
    artifact at all (caller -> 404).

    `state.artifact` resolves a well-known key (`report`, `suggestions`), a path
    under `artifacts/` or that path spelled with the directory -- and it is the
    ONE traversal guard, which matters here precisely because this name arrives
    from a URL: an absolute path or one containing `..` is not an artifact and
    gets None rather than a file from somewhere else on the host.

    The payload carries the two renderings worded ONCE in `engine.state`:
    `summaryLines` (the header block) and `text` (header + separator + body),
    which is exactly what `ralphctl artifacts <run> show <name>` prints -- so
    app.js puts `text` into text nodes and the two surfaces cannot describe the
    same file differently.

    An artifact this run never wrote is not an error (the listing may be a poll
    cycle behind the disk): it answers with the entry and
    `state.RUN_DOCUMENT_ABSENT` as its body. A binary one answers with
    `state.ARTIFACT_BINARY` instead of spraying bytes into a browser.
    """
    entry = artifact(reg / "runs" / run_id, name)
    if entry is None:
        return None
    return {"runId": run_id, **_with_artifact_display(entry),
            "summaryLines": artifact_summary_lines(entry),
            "text": artifact_text(entry)}


def approaches_list_view(reg: Path, run_id: str) -> dict:
    """Every archived approach's own task counts, for the hub's
    approach-history panel (task 020/021, #24): the collapsed list of
    previous approaches before any one of them is expanded.

    `engine.state.archived_approaches_list` -- the SAME reader `ralphctl
    tasks <run> --all` (task 019) uses -- so the hub and the CLI can never
    list a different set of approaches or disagree about one's counts. A
    run with none archived yet (still on its first approach, or one that
    never needed a second) answers with an empty list rather than an error;
    app.js renders that as no history section at all.

    Purely on-disk, like `document_list`/`artifact_list`: these files are
    copied by the engine's own `_archive_approach()` into the run dir, so a
    dead run's archived approaches read exactly like a live one's -- there
    is no live answer to prefer, hence no `live` flag.
    """
    payload = archived_approaches_list(reg / "runs" / run_id)
    return {"runId": run_id, **payload}


def approach_view(reg: Path, run_id: str, approach: int) -> dict | None:
    """One archived approach's full task table plus its `notes.md`/
    `review-findings.md` text, for the hub's approach-history dialog (task
    020/021, #24) -- None when this approach was never archived (caller ->
    404).

    `engine.state.archived_approach_view` does the ONE shaping: the task
    table is `read_tasks_doc(...).payload`, identical to `GET /tasks` and
    `ralphctl tasks --approach --json` (task 019); the notes/findings
    entries are the SAME dicts `document_view` returns for the live
    documents (task 022's `RUN_DOCUMENT_ABSENT` discipline), given
    `sizeDisplay`/`text` here too so app.js never has to word a missing
    file differently depending on which dialog it is in.
    """
    view = archived_approach_view(reg / "runs" / run_id, approach)
    if view is None:
        return None
    notes = _with_document_display(view["notes"])
    findings = _with_document_display(view["findings"])
    return {"runId": run_id, "approach": view["approach"],
            "tasks": view["tasks"], "notice": view["notice"],
            "notes": {**notes, "text": run_document_text(view["notes"])},
            "findings": {**findings,
                        "text": run_document_text(view["findings"])}}


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
    for key in ("byPhase", "byApproach", "byTier"):
        buckets = usage.get(key)
        if isinstance(buckets, dict):
            out_usage[key] = {k: rendered(v) for k, v in buckets.items()}
    return {**doc, "usage": out_usage}


def _with_provisioning(doc: dict, cfg_path: Path) -> dict:
    """Task 010 (#79, PRD requirement O): attach the resolved hat -> model
    mapping to a status doc server-side, the same discipline
    `_with_approach_display`/`_with_cost_display` use for their own fields --
    except the raw material is not on the status doc itself (a live engine's
    `/status` answer carries no `models:` table) but this run's own
    `job.yaml`, so it is loaded HERE, from the host's copy
    (`ui_server._config_dir`, the same path `cli.main.config_root` spells),
    never from anything a proxied payload could forge.

    `provisioning` is `JobConfig.provisioning_table()`'s structured rows
    (`ralphctl status --json`'s own `provisioning` key, so the two surfaces
    describe the identical mapping); `provisioningLines` is
    `state.format_provisioning_lines` over those SAME rows -- the exact
    strings `ralphctl status`'s plain-text output prints, rendered by the
    hub as-is (`renderSummary`, `app.js`), never re-worded in JS.

    A run with no job.yaml on disk yet (or an unreadable one) gets an empty
    table rather than raising -- the same "absent, not a crash" discipline
    `_with_local_times` gives a doc with no timestamps.
    """
    if not isinstance(doc, dict):
        return doc
    table: list[dict] = []
    if cfg_path.is_file():
        try:
            table = JobConfig.load(cfg_path).provisioning_table()
        except Exception:
            table = []
    return {**doc, "provisioning": table,
            "provisioningLines": format_provisioning_lines(table)}


def _with_llm_headers(doc: dict, cfg_path: Path) -> dict:
    """Task 047 (#177): attach this run's own `--llm-header` map to a status
    doc server-side, the exact same discipline `_with_provisioning` right
    above uses -- the raw material (`<cdir>/llm-wiring.json`) is not on the
    status doc itself either, live or on-disk, so it is read HERE, from
    `cfg_path.parent` (the same config dir `_with_provisioning` was already
    given `cfg_path` under), through the ONE shared reader
    (`llm_profiles.read_llm_wiring_headers`) `ralphctl status` and the
    redacted `job.yaml` view also call, so a header's redaction cannot read
    differently on the hub than it does on the CLI.

    `llmHeaders` is a plain `name -> value` dict with every ref-derived
    value already replaced by `llm_profiles.MASK` -- `app.js`'s
    `renderSummary` renders it verbatim under the model line, never
    re-deriving which values were secret.

    A run with no config dir/llm-wiring.json at all (or an unreadable one)
    gets an empty dict rather than raising -- the same "absent, not a
    crash" discipline `_with_provisioning` gives a job.yaml-less run.
    """
    if not isinstance(doc, dict):
        return doc
    try:
        headers = llm_profiles.read_llm_wiring_headers(cfg_path.parent)
    except Exception:
        headers = {}
    return {**doc, "llmHeaders": headers}


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


def _with_pause_markers(doc: dict, run_dir: Path) -> dict:
    """Task 005 (PRD requirement A.5, #269): attach `pauseMarker` (task
    017, previously only carried by `run_list`'s flat rows) and the new
    `escalationUndeliveredMarker`/`escalationUndelivered` to the run-detail
    page's own status doc, so `app.js`'s `stateCell` has the same two
    fields the runs-list row already reads, whether `doc` came from a live
    proxied `GET /status` or the on-disk snapshot.

    `run_dir` (never the live proxy) is the events.jsonl read -- local-only,
    same discipline `_with_task_transitions` already follows.
    """
    if not isinstance(doc, dict):
        return doc
    return {
        **doc,
        **escalation_pause_markers(run_dir, doc),
    }


def _with_task_failure_kinds(tasks: dict) -> dict:
    """Task 025 (#33): the `taskFailureKinds` map -- `{task id: kind}` for every
    failed task -- DERIVED from the task list in a LIVE `GET /tasks` payload.

    Derived rather than trusted for the same reason `_with_tasks_read_label`
    re-derives its label: the payload may come from a pre-v0.7 engine (no such
    field, and no `failureKind` on the records either), and it may carry a
    forged key of that name written by the agent into tasks.json. One call to
    `engine.state.task_failure_kinds` -- the function `GET /tasks` and `ralphctl
    tasks` use -- so the hub cannot read a failed task differently from the CLI.
    Absent entirely when nothing failed, exactly as `TasksRead.payload` omits it
    on the on-disk path (which needs no second derivation).
    """
    out = {k: v for k, v in tasks.items() if k != TASK_FAILURE_KINDS_FIELD}
    kinds = task_failure_kinds(tasks.get("tasks") or [])
    if kinds:
        out[TASK_FAILURE_KINDS_FIELD] = kinds
    return out


def _with_task_blocked_by(tasks: dict) -> dict:
    """Task 021 (#60 pillar 3): the `taskBlockedBy` map -- `{task id: [dead/
    doomed ancestor ids]}` -- DERIVED from the task list in a LIVE `GET
    /tasks` payload, the exact sibling of `_with_task_failure_kinds` above and
    for the same reason: the payload may come from an engine that predates
    this field, or may carry a forged key of that name written straight into
    tasks.json by an agent. One call to `engine.state.task_blocked_by` -- the
    function `GET /tasks` (via `TasksRead.payload`) and `ralphctl tasks` use
    -- so the hub cannot show a dead subtree differently from the CLI. Absent
    entirely when nothing is blocked, exactly as `TasksRead.payload` omits it
    on the on-disk path.
    """
    # Task 013 (PRD requirement C.1, #149): `include_petitioned=True` --
    # this is the hub's own re-derivation half of the same four display
    # surfaces; a dependent of a `petitioned` task shows up here exactly as
    # `TasksRead.blocked_by` (the on-disk/`GET /tasks` half) shows it, with
    # the petitioned task named as its cause.
    out = {k: v for k, v in tasks.items() if k != TASK_BLOCKED_FIELD}
    blocked = task_blocked_by(tasks.get("tasks") or [], include_petitioned=True)
    if blocked:
        out[TASK_BLOCKED_FIELD] = blocked
    return out


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


def _with_task_totals(tasks: dict, iterations: list, approach) -> dict:
    """Task 036 (G.4; re-scoped task 001/PRD I.1, #207/#29's defect
    recurring in this new place): attach each task's own aggregated
    `timeDisplay`/`costDisplay` -- summed from every entry in `iterations`
    (THIS run's own `run_detail` iteration list, `engine.state.
    iteration_detail` shaped) that named it via `assignedTaskId` AND whose
    OWN `approach` field matches `approach` (the caller's own current
    approach, from `status.json`) -- via `engine.state.task_totals`'s
    approach-namespaced `"<approach>:<taskId>"` keys -- so the hub tasks
    table's new time/cost columns render server-formatted strings, exactly
    the `usage.costDisplay`/`startedAtLocal` discipline every other hub
    number already follows, never a client-side re-sum, and never another
    approach's figures: every approach's planning pass renumbers
    `tasks.json` from "001", so without this scoping approach 2's task
    "001" would show approach 1's minutes and dollars too (#207).

    A task the CURRENT approach has never dispatched reads ZERO -- PRD I.1's
    own definition of done ("a `pending` task in the current approach shows
    zero"): both keys are attached from `engine.state.task_totals_zero()`, so
    the row renders `0s`/`$0.0000` and not an em dash, even when an EARLIER
    approach's same-numbered task has real spend on disk. That zero is the
    honest reading of a run that DOES record dispatch attribution: this
    approach dispatched no iteration against this task, so its minutes and
    dollars are zero, and a dash there would read as "unknown" for a figure
    that is in fact known. Any `iterationsBreakdown` the incoming task doc
    carried is dropped on that path, the same forged-key discipline
    `_with_tasks_read_label` above uses for `tasksNotice`/`tasksLabel` --
    there are no iterations behind a zero.

    The ONE case that still renders the em dash is a run that never records
    dispatch attribution at all: a pre-task-027 run dir, where NO iteration
    carries the `assignedTaskId` key, so nothing about any task's spend is
    measurable and neither key is attached (task 038, G.6 --
    `test_pre_dispatch_record_run_dir_still_renders` pins exactly that).
    Distinct from a brand-new v0.24+ run that DOES record attribution but
    has not yet dispatched any iteration against any task -- its own
    planning iteration already carries `"assignedTaskId": null`, a present
    key, so `task_totals_records_attribution` (below) reads it as
    attribution-capable and every pending task still gets the zero bucket,
    never the dash (cure-01-06, PRD I.1, DoD 10). "Never measured" and
    "measured zero" are different statements and the hub says them
    differently -- and `task_totals()` alone returning `{}` cannot tell
    those two apart, since neither a truly legacy run NOR a fresh run with
    zero dispatches so far builds any bucket; only the key's presence on at
    least one iteration answers it.

    Also attaches `iterationsBreakdown` (task 037, G.5): the SAME per-
    iteration list `task_totals` built the total from -- `{"number",
    "tier", "costDisplay"}` per entry -- so a task detail view can list
    exactly the iterations behind its total, bounded to this run's own
    iteration set AND this approach, without re-deriving anything
    client-side. Absent when that list is empty (including every zero-bucket
    row above).
    """
    totals = task_totals(iterations)
    if not totals and not task_totals_records_attribution(iterations):
        return tasks
    approach = approach if isinstance(approach, int) and not isinstance(approach, bool) else 1
    zero = task_totals_zero()
    out = dict(tasks)
    new_list = []
    for t in tasks.get("tasks") or []:
        tid = t.get("id")
        agg = totals.get(f"{approach}:{tid}") if tid is not None else None
        if agg:
            t = {**t, "timeDisplay": agg["durationDisplay"],
                 "costDisplay": agg["costDisplay"]}
            if agg.get("iterations"):
                t["iterationsBreakdown"] = agg["iterations"]
        else:
            t = {**t, "timeDisplay": zero["durationDisplay"],
                 "costDisplay": zero["costDisplay"]}
            t.pop("iterationsBreakdown", None)
        new_list.append(t)
    out["tasks"] = new_list
    return out


def _with_task_transitions(tasks: dict, run_dir: Path) -> dict:
    """Task 039 (PRD I.1): attach each task's own `transitionHistory` --
    `engine.state.task_transition_history`'s list of `{ts, tsLocal,
    oldStatus, newStatus, cause?}` entries, read straight off this run's own
    `events.jsonl` -- so a task detail dialog can show every state the task
    passed through, not only the single current status `tasks.json` itself
    carries. A task with no recorded transition (never seen by an
    `_emit_task_changes()` diff -- pre-v0.9 run dir, or a plan rewritten
    since the last poll) gets no key at all, the same absent-key discipline
    `_with_task_totals` just above uses for `iterationsBreakdown` (and, for a
    run recording no dispatch attribution at all, for `timeDisplay`/
    `costDisplay` too).

    Purely on-disk, like `task_transition_history` itself, so this runs the
    same for a live run and a dead one with no container.
    """
    out = dict(tasks)
    new_list = []
    for t in tasks.get("tasks") or []:
        tid = t.get("id")
        history = task_transition_history(run_dir, str(tid)) if tid is not None else []
        if history:
            t = {**t, "transitionHistory": history}
        new_list.append(t)
    out["tasks"] = new_list
    return out


# Task 004 (A.2): the hub's derivation caches, so a poller that re-fetches
# `GET /api/runs/<id>` on a fixed interval does not pay to re-derive iteration
# rows or re-shape the tasks/status blocks when nothing that feeds them has
# moved on disk since the last request for that SAME run. Keyed per run id
# (never globally) so one run's cache entries cannot answer for another's --
# a run id being a fresh directory each time (`ralphctl start` never reuses
# one while its old entry is still cached) makes a stale cross-run hit a
# non-concern; `delete_run` below also drops a deleted run's entries rather
# than leaving them to leak for the life of the hub process.
#
# `{run_id: {number: (key, detail)}}` for the iteration rows, `{run_id:
# (mtime, block)}` for the tasks/status blocks -- plain dicts, no lock: every
# write here replaces a whole tuple/dict value under one key, which CPython's
# GIL makes atomic against the `ThreadingHTTPServer`'s concurrent requests: a
# reader either sees the old value or the new one, never a torn half-write.
_ITERATION_ROW_CACHE: dict[str, dict[int, tuple[tuple, dict | None]]] = {}
_TASKS_BLOCK_CACHE: dict[str, tuple[float, dict]] = {}
_STATUS_BLOCK_CACHE: dict[str, tuple[float, dict]] = {}
# Task 045 (H.3): the rendered-tail cache for `GET /api/runs/<id>/logs`.
# `{run_id: (key, (live, lines))}` -- see `_cached_rendered_log_lines` below.
_LOG_TAIL_CACHE: dict[str, tuple[tuple, tuple[bool, list[str]]]] = {}

# Task 047 (H.5): the cap each of the three module-scope derivation caches
# above (`_ITERATION_ROW_CACHE`, `_TASKS_BLOCK_CACHE`, `_STATUS_BLOCK_CACHE`)
# is held to -- a long-lived `ralphctl ui` polling a great many distinct run
# ids over its life must not grow an entry per run id forever. 200 is well
# above any one operator's realistic number of concurrently-interesting runs
# while still bounding worst-case memory to a small, fixed multiple of a
# single run's own cache footprint.
_RUN_CACHE_CAP = 200


def _bound_run_cache(cache: dict, run_id: str) -> None:
    """Task 047 (H.5): enforce `_RUN_CACHE_CAP` as an LRU policy over one of
    the three per-run-id caches above, keyed by `run_id` at the cache's own
    top level (this also covers `_ITERATION_ROW_CACHE`, whose values are
    themselves per-iteration dicts -- the cap bounds the number of RUNS
    resident, not rows within a run).

    Call this after every access to the cache for `run_id` -- hit or miss,
    read or write -- so the run just touched is moved to the end (most
    -recently-used) and is never the entry evicted; plain `dict`s preserve
    insertion order (Python 3.7+), so `pop` + reinsert is enough to track
    recency without a separate `OrderedDict`. Once the cache holds more than
    `_RUN_CACHE_CAP` distinct run ids, the least-recently-used ones are
    popped until it fits again -- ordinarily just one, since this runs after
    every single insertion, but the `while` tolerates a cache pre-loaded
    past the cap by a test.
    """
    if run_id in cache:
        cache[run_id] = cache.pop(run_id)
    while len(cache) > _RUN_CACHE_CAP:
        oldest = next(iter(cache))
        cache.pop(oldest)


def _cached_iteration_row(run_dir: Path, run_id: str, number: int) -> dict | None:
    """Task 004 (A.2): the iteration-row cache, keyed per run by (iteration
    number, meta.json mtime, transcript size) -- not merely "have we built
    row N once", so a still-running iteration whose meta.json or transcript
    keeps growing is re-derived through `iteration_detail` on every request
    that observes a new mtime/size, while every OLDER, finished iteration in
    the same run is served from cache untouched.

    `iteration_detail` is looked up by its bound global name (the one
    imported at module scope above), never through a private alias, so a
    test that patches `ui_server.iteration_detail` to count calls observes
    every derivation this cache actually performs -- and none it skips.

    Only the two `stat()` calls below run on a cache hit; the shaping
    `iteration_detail` does (formatting times/cost/tokens, re-deriving the
    fault cause, counting archived attempts) is exactly the per-row cost a
    run with hundreds of iterations must not pay again on every poll.
    """
    try:
        meta_mtime = (iteration_dir(run_dir, number) / "meta.json").stat().st_mtime
    except OSError:
        meta_mtime = None
    try:
        transcript_size = iteration_output_path(run_dir, number).stat().st_size
    except OSError:
        transcript_size = 0
    key = (meta_mtime, transcript_size)
    per_run = _ITERATION_ROW_CACHE.setdefault(run_id, {})
    _bound_run_cache(_ITERATION_ROW_CACHE, run_id)
    cached = per_run.get(number)
    if cached is not None and cached[0] == key:
        return cached[1]
    detail = iteration_detail(run_dir, number)
    per_run[number] = (key, detail)
    return detail


def _cached_disk_tasks_block(run_dir: Path, run_id: str) -> dict:
    """Task 004 (A.2): the on-disk `tasks.json` shaping cache.

    Keyed per run by `tasks.json`'s own mtime -- re-derives
    `read_tasks_doc(run_dir, persist=False)` (the hardened reader, task 004
    #15, plus the failure-kind/blocked-by derivation it already carries for
    the local-read path) only when the file has actually moved since the
    last request for this run, rather than re-parsing and re-labelling an
    unchanged plan on every poll. Used only as the FALLBACK when the run has
    no live `/tasks` answer -- a live payload is somebody else's process
    answering right now and is shaped fresh every time, never cached here.
    """
    path = run_dir / "tasks.json"
    try:
        mtime = path.stat().st_mtime
    except OSError:
        mtime = 0.0
    cached = _TASKS_BLOCK_CACHE.get(run_id)
    if cached is not None and cached[0] == mtime:
        _bound_run_cache(_TASKS_BLOCK_CACHE, run_id)
        return cached[1]
    tasks_read = read_tasks_doc(run_dir, persist=False)
    tasks = tasks_read.payload
    tasks.setdefault("tasks", [])
    _TASKS_BLOCK_CACHE[run_id] = (mtime, tasks)
    _bound_run_cache(_TASKS_BLOCK_CACHE, run_id)
    return tasks


def _log_tail_cache_key(run_dir: Path, tail: int | None) -> tuple:
    """Task 045 (H.3): the key `_cached_rendered_log_lines` stores its result
    under -- the newest iteration's own number and its own on-disk transcript
    size, plus the `tail` this particular request asked for (a `tail=200`
    request and a `tail=0` full-render request over the identical transcript
    render different output and must never collide in the same slot).
    `(None, 0, tail)` for a run with no iterations yet, rather than raising."""
    numbers = iteration_numbers(run_dir)
    newest = max(numbers) if numbers else None
    try:
        size = (iteration_output_path(run_dir, newest).stat().st_size
                if newest is not None else 0)
    except OSError:
        size = 0
    return (newest, size, tail)


def _cached_rendered_log_lines(reg: Path, run_dir: Path, run_id: str,
                               tail: int | None) -> tuple[bool, list[str]]:
    """Task 045 (H.3): the route-level cache in front of `rendered_log_lines`
    -- a repeated `GET /api/runs/<id>/logs` for the SAME tail, while the
    newest iteration's number and transcript size have not moved, is
    answered from this cache instead of paying for another live-container
    fetch (or on-disk merge) and another render pass.

    Deliberately a wrapper around `rendered_log_lines`, not a change to it:
    task 043/044's parse-work probes call `rendered_log_lines` directly
    against a run dir that holds no on-disk iterations at all (the render
    input comes entirely from a monkeypatched `_proxy_text`), so caching
    inside that function would key every call to the same `(None, 0, tail)`
    slot and serve the FIRST blob's render back for every later, differently
    -- shaped blob those tests feed it. Calling `rendered_log_lines` by its
    bound module-level name (never a private alias) keeps it monkeypatchable
    by those same tests and by a caller that wants to count real renders.
    """
    key = _log_tail_cache_key(run_dir, tail)
    cached = _LOG_TAIL_CACHE.get(run_id)
    if cached is not None and cached[0] == key:
        return cached[1]
    result = rendered_log_lines(reg, run_id, tail)
    _LOG_TAIL_CACHE[run_id] = (key, result)
    return result


def _cached_disk_status_block(reg: Path, run_dir: Path, run_id: str,
                              recorded_status: dict) -> dict:
    """Task 004 (A.2): the on-disk `status.json` shaping cache.

    Keyed per run by `status.json`'s own mtime -- re-derives the display
    pipeline (`_with_local_times` -> `_with_cost_display` ->
    `_with_approach_display` -> `_with_provisioning`) only when the file has
    moved since the last request for this run. Used only as the FALLBACK
    when the run has no live `/status` answer -- a live payload is shaped
    fresh every time, never cached here, since it is somebody else's process
    answering right now rather than a file this cache can key on.

    `recorded_status` -- the caller's own disk read -- is passed in rather
    than re-read here, so there is exactly one read of `status.json` per
    request regardless of whether the shaping that follows it is a cache hit
    or a miss.
    """
    try:
        mtime = (run_dir / "status.json").stat().st_mtime
    except OSError:
        mtime = 0.0
    cached = _STATUS_BLOCK_CACHE.get(run_id)
    if cached is not None and cached[0] == mtime:
        _bound_run_cache(_STATUS_BLOCK_CACHE, run_id)
        return cached[1]
    _cfg_path = _config_dir(reg, run_id) / "job.yaml"
    shaped = _with_llm_headers(_with_provisioning(
        _with_approach_display(_with_cost_display(_with_local_times(recorded_status))),
        _cfg_path), _cfg_path)
    _STATUS_BLOCK_CACHE[run_id] = (mtime, shaped)
    _bound_run_cache(_STATUS_BLOCK_CACHE, run_id)
    return shaped


def _drop_run_detail_caches(run_id: str) -> None:
    """Task 004 (A.2): forget a deleted run's cache entries -- called from
    `delete_run` -- so the hub process does not grow an entry per run id for
    the life of a long-lived `ralphctl ui`."""
    _ITERATION_ROW_CACHE.pop(run_id, None)
    _TASKS_BLOCK_CACHE.pop(run_id, None)
    _STATUS_BLOCK_CACHE.pop(run_id, None)
    _LOG_TAIL_CACHE.pop(run_id, None)


def run_detail_etag(reg: Path, run_id: str) -> str:
    """Task 003 (A.1): the `ETag` for `GET /api/runs/<id>`.

    Derived from the max mtime of the four on-disk files whose changes can
    move what `run_detail` reports -- `status.json`, `tasks.json`,
    `events.jsonl` and the newest iteration's `meta.json` -- so a hub poller
    can send it back as `If-None-Match` and skip re-fetching (and
    re-rendering) a detail view that has not moved since its last poll.
    Purely on-disk, like the mtime it is built from: a live proxied
    status/tasks answer is never consulted here, so the tag only ever
    changes when something was actually WRITTEN to this run's directory,
    never merely because a container answered a proxy call differently this
    time.

    Missing files (a run dir before its first iteration writes anything, or
    an events.jsonl not yet created) contribute nothing rather than raising
    -- the tag is simply built from whichever of the four exist.
    """
    run_dir = reg / "runs" / run_id
    candidates = [run_dir / "status.json", run_dir / "tasks.json",
                  run_dir / "events.jsonl"]
    numbers = iteration_numbers(run_dir)
    if numbers:
        candidates.append(iteration_dir(run_dir, max(numbers)) / "meta.json")
    mtime = 0.0
    for path in candidates:
        try:
            mtime = max(mtime, path.stat().st_mtime)
        except OSError:
            continue
    return f'"{run_id}-{mtime:.6f}"'


def log_tail_etag(reg: Path, run_id: str) -> str:
    """Task 007 (G): the `ETag` for `GET /api/runs/<id>/logs`.

    Deliberately NOT the same value as `run_detail_etag`: the logs route
    renders the newest iteration's TRANSCRIPT, and `run_detail_etag`'s own
    four inputs (`status.json`, `tasks.json`, `events.jsonl`, the newest
    iteration's `meta.json`) do not include it. A running iteration writes
    its own `meta.json` once at start and again at end, so an in-flight
    iteration's own on-disk transcript can grow -- visible content genuinely
    appended -- while all four of those inputs sit untouched; a client that
    replayed `run_detail_etag`'s tag as `If-None-Match` would then wrongly
    get back a `304` for content that had actually changed. This tag adds
    the newest iteration's own on-disk transcript SIZE (the same figure
    `_log_tail_cache_key` already keys the render cache on, so the two never
    disagree about what counts as "moved") on top of `run_detail_etag`'s own
    mtime, so an append to that transcript alone still yields a new tag.

    Missing iterations contribute a size of 0 rather than raising, matching
    `run_detail_etag`'s own missing-file tolerance.
    """
    run_dir = reg / "runs" / run_id
    base = run_detail_etag(reg, run_id)
    numbers = iteration_numbers(run_dir)
    size = 0
    if numbers:
        try:
            size = iteration_output_path(run_dir, max(numbers)).stat().st_size
        except OSError:
            size = 0
    # `base` is `'"{run_id}-{mtime:.6f}"'` -- splice the size in before the
    # closing quote rather than re-deriving the mtime here, so this tag and
    # `run_detail_etag`'s own never drift apart on the shared half.
    return f'{base[:-1]}-{size}"'


def run_detail(reg: Path, run_id: str) -> dict | None:
    """Run detail view (PRD req 21): task table + iteration timeline data,
    live where possible, falling back to the on-disk snapshot for a dead
    run. Returns None if the run doesn't exist at all (caller -> 404)."""
    run_dir = reg / "runs" / run_id
    if not run_dir.is_dir():
        return None
    recorded_status = _read_json(run_dir / "status.json", {}) or {}
    # Task 031 (#19): the delete gate's own doc. The endpoint gates on what the
    # run dir RECORDS, so the affordance -- and `container_gone` below --
    # decide from this SAME on-disk read, never from a live proxied answer
    # (which task 004's cache below shapes separately, into `shaped_status`).
    ok_s, _, live_status = _proxy_json(reg, run_id, "GET", "/status")
    if ok_s and live_status:
        # Task 004 (A.2): a live answer is somebody else's process replying
        # right now -- shaped fresh every time, never cached, since there is
        # no on-disk mtime to key a cache entry on.
        _cfg_path = _config_dir(reg, run_id) / "job.yaml"
        shaped_status = _with_llm_headers(_with_provisioning(
            _with_approach_display(_with_cost_display(_with_local_times(live_status))),
            _cfg_path), _cfg_path)
    else:
        shaped_status = _cached_disk_status_block(reg, run_dir, run_id, recorded_status)

    # Task 005 (PRD requirement A.5, #269): both markers, on the SAME doc
    # whichever branch produced it -- see `_with_pause_markers`'s own
    # docstring for why the read is always local (`run_dir`) even on the
    # live-proxied branch.
    shaped_status = _with_pause_markers(shaped_status, run_dir)

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
    # Task 004 (A.2): cached per `tasks.json`'s own mtime -- only re-parsed
    # and re-labelled when the file has actually moved since the last
    # request for this run.
    tasks = _cached_disk_tasks_block(run_dir, run_id)
    ok_t, _, live_tasks = _proxy_json(reg, run_id, "GET", "/tasks")
    if ok_t and isinstance(live_tasks, dict):
        # Task 025 (#33): the local read already carries the derived failure
        # kinds (`TasksRead.payload`); a LIVE answer gets them derived here, so
        # a pre-v0.7 engine's plan -- and a `taskFailureKinds` key an agent
        # wrote into tasks.json itself -- cannot reach the browser unlabelled
        # or forged. Exactly ONE derivation on each path, never both.
        #
        # Task 021 (#60 pillar 3): same discipline, same reason, for the
        # derived `taskBlockedBy` dead-dependency map.
        tasks = _with_task_failure_kinds(live_tasks)
        tasks = _with_task_blocked_by(tasks)
    tasks = _with_tasks_read_label(tasks)

    # v0.13 task 015 (#89, requirement I, hub half): each row is built from
    # `engine.state.iteration_detail` -- the SAME shaping `iteration_view`
    # (the dialog) and `ralphctl iteration` already use -- rather than a raw
    # `meta.json` read through `_with_local_times`. Before this, the timeline
    # row and the dialog it opens were built from two different reads of the
    # same file: the dialog's `reason`/`faultClass` re-derivation (task 013)
    # never reached the row at all, so a faulted iteration's cause could only
    # be seen after opening it. Skipped exactly when `_read_json` used to
    # return None -- an unreadable/missing meta.json -- so a dir the engine
    # never finished writing still does not appear as a row of blanks.
    # Task 004 (A.2): cached per iteration by (meta.json mtime, transcript
    # size) -- a finished, unchanging iteration is served from cache rather
    # than re-derived through `iteration_detail` on every request.
    iterations = []
    for number in iteration_numbers(run_dir):
        detail = _cached_iteration_row(run_dir, run_id, number)
        if detail is not None and detail.get("hasMeta"):
            iterations.append(detail)

    # Task 036 (G.4; re-scoped task 001/PRD I.1, #207): the hub tasks
    # table's time/cost columns -- per-task sums over exactly the
    # iterations just built above, scoped to the CURRENT approach
    # (`shaped_status`'s own `approach`, the same field `_with_approach_
    # display` renders and `current_approach()` falls back to 1 for), so a
    # task's total can never disagree with what its own timeline rows
    # already show, and never picks up another approach's spend against a
    # same-numbered task.
    tasks = _with_task_totals(tasks, iterations, shaped_status.get("approach"))
    # Task 039 (PRD I.1): each task's own transition history, read straight
    # off this run's own events.jsonl -- never derived from the iterations
    # list just built above, so a transition recorded before this run's
    # iteration history begins (or with none at all) still shows.
    tasks = _with_task_transitions(tasks, run_dir)

    return {
        "runId": run_id,
        "live": ok_s,
        # Task 024 (#8): `live: false` alone is not the story -- a *finished*
        # run is unreachable by design. Paired with a non-terminal recorded
        # state it means the container died without recording a terminal
        # state, which the card renders with the warning treatment.
        "containerGone": container_gone(recorded_status, ok_s),
        # Task 031 (#19): `deletable` + `deleteRefusal` (the server's own
        # wording) for the detail page's delete control. Top level, not inside
        # `status`: it is a fact about this run's removability, not a rendering
        # of the status doc -- and a live payload must not be able to claim it.
        **deletion_fields(recorded_status),
        "status": shaped_status,
        "tasks": tasks,
        "iterations": iterations,
    }


def _cli():
    """The host-side CLI module, imported lazily.

    `cli/main.py` imports THIS module at import time (`ralphctl ui` boots the
    hub), so the dependency can only go the other way at call time. Nothing on
    a read path uses it -- only the delete endpoint, which deliberately reuses
    the CLI's removal sequence (`main.remove_run_state`) instead of growing a
    second one here.
    """
    from . import main as cli_main
    return cli_main


def deletion_fields(status: dict) -> dict:
    """Task 031 (#19): what a delete affordance may offer for this run --
    `deletable` (may be clicked) plus `deleteRefusal` (why not, or None).

    Decided by the ONE gate the endpoint itself applies (`deletion_refusal`)
    over the SAME on-disk status doc `delete_run` reads, so the button can
    neither offer a deletion the endpoint will refuse nor hide one it would
    perform. In particular the run-detail view keeps deciding this from the
    run dir's `status.json` even when the card above it is showing a *live*
    proxied status: the endpoint gates on what is recorded, and a control that
    disagreed with it would be lying about what clicking does.

    Both keys are always present and always recomputed, the discipline of
    `approachDisplay`/`costDisplay`: a forged `deletable` in somebody's
    status.json (or in a proxied payload) cannot talk the hub into offering a
    destructive action, and the refusal SENTENCE is the server's own wording
    (`DELETE_REFUSED_ACTIVE`/`_FINISHING`/`_UNKNOWN`), so app.js never invents
    a second explanation of why a run may not be deleted.
    """
    reason = deletion_refusal(status if isinstance(status, dict) else {})
    return {"deletable": reason is None, "deleteRefusal": reason}


def deletion_refusal(status: dict) -> str | None:
    """Why this run must not be deleted right now -- or None when it may be.

    The hub's gate (task 030, #19), deliberately STRICTER than
    `ralphctl rm --force`'s: the recorded `state` must be in
    `TERMINAL_STATES`, full stop. `rm --force` may also delete a run dir whose
    container is already gone while status.json still says `running` (an
    operator at a terminal, having read `repair`'s diagnosis, can decide that);
    a one-click button in a browser may not, because "the API does not answer"
    is not the same fact as "the job is over", and the hub cannot tell a
    zombie from a run whose port is merely filtered.

    An absent, unreadable or unrecognized state is refused with its own
    wording rather than being called active: the honest answer is that we do
    not know, and unknown is not permission for a destructive action (the same
    rule task 002 applies to an unreadable tasks.json, here with teeth).
    """
    state = status.get("state")
    if state in TERMINAL_STATES:
        return None
    # Task 013 (#56): checked before the generic NONTERMINAL_STATES branch --
    # `finishing` gets its own wording (settled verdict, unwinding reflect),
    # not the "abort or stop it first" instruction that applies to a run
    # still actively working the plan.
    if state == "finishing":
        return DELETE_REFUSED_FINISHING
    if state in NONTERMINAL_STATES:
        return DELETE_REFUSED_ACTIVE.format(state=state)
    return DELETE_REFUSED_UNKNOWN.format(
        state=state if isinstance(state, str) and state else UNKNOWN_STATE)


def delete_run(reg: Path, run_id: str) -> tuple[int, dict]:
    """Perform `rm --force`'s removal for a terminal run. Returns
    (http status, payload) -- never raises for an ordinary refusal.

    Success payload is `ralphctl rm --json`'s own shape
    (`{"removed": <run id>, "stoppedContainer": <bool>}`) because it is the
    same act: the container teardown, the sibling reaping and both
    directories come from `main.remove_run_state`, the ONE removal sequence.
    A refusal answers 409 with the reason `deletion_refusal` worded and the
    recorded state, and touches NOTHING -- not even a `docker inspect`.
    """
    runs_dir = reg / "runs"
    run_dir = runs_dir / run_id
    # A run id arrives as one URL segment and is about to be handed to
    # `shutil.rmtree`: refuse anything that is not the plain name of a direct
    # child of `<registry>/runs` (`..`, an absolute path, a nested path),
    # rather than trusting the caller's spelling.
    if (not run_id or run_id in (".", "..") or "/" in run_id or "\\" in run_id
            or run_dir.parent.resolve() != runs_dir.resolve()
            or not run_dir.is_dir()):
        return 404, {"error": f"run {run_id} not found"}
    status = _read_json(run_dir / "status.json", {}) or {}
    reason = deletion_refusal(status)
    if reason:
        return 409, {"error": reason, "runId": run_id,
                     "state": status.get("state")}
    cli = _cli()
    # The CLI resolves a run's directories from RALPHD_REGISTRY, while the hub
    # was handed its registry explicitly (`make_server`). `ralphctl ui` passes
    # exactly `registry()`, so these agree in every real deployment -- but a
    # mismatch would mean deleting some OTHER registry's run, so it stops here
    # instead of guessing which of the two is meant.
    if cli.registry().resolve() != reg.resolve():
        return 500, {"error": "hub registry does not match RALPHD_REGISTRY — "
                              "refusing to delete", "runId": run_id}
    container_exists = cli.container_record_exists(run_id)
    cli.remove_run_state(run_id, container_exists=container_exists,
                         reason=DELETE_TERMINATION_REASON)
    # Task 004 (A.2): a deleted run's directory is gone -- forget its
    # derivation-cache entries too, rather than leaking them for the life of
    # this hub process.
    _drop_run_detail_caches(run_id)
    return 200, {"removed": run_id, "stoppedContainer": container_exists}


class Handler(BaseHTTPRequestHandler):
    """Bound to a registry path via `make_handler_class()`."""

    registry: Path = Path.home() / ".ralphd"
    server_version = "ralphctl-ui/1"

    def log_message(self, fmt, *args):  # quiet by default -- no per-request spam
        pass

    # -- response helpers -------------------------------------------------

    def _send_json(self, obj, code=200, *, etag: str | None = None):
        data = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Content-Security-Policy", HUB_CSP)
        if etag is not None:
            # Task 003 (A.1): only the run-detail route passes this -- see
            # `run_detail_etag`.
            self.send_header("ETag", etag)
        self.end_headers()
        self.wfile.write(data)

    def _send_not_modified(self, etag: str) -> None:
        """Task 003 (A.1): the `If-None-Match` hit path for `GET
        /api/runs/<id>` -- `304` with the same `ETag` and an empty body, no
        `Content-Type` (there is no representation being sent)."""
        self.send_response(304)
        self.send_header("ETag", etag)
        self.send_header("Content-Security-Policy", HUB_CSP)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _send_bytes(self, data: bytes, code=200, content_type="application/octet-stream",
                    *, nosniff: bool = False):
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Content-Security-Policy", HUB_CSP)
        if nosniff:
            # Task 023 (#54): the raw artifact byte route's own header --
            # never sent by the static bundle or JSON routes, which have no
            # agent-authored bytes to worry a browser's own sniffer with.
            self.send_header("X-Content-Type-Options", "nosniff")
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
            if segs == ["api", "version"]:
                # Task 033 (G.1): the hub's OWN package version, served
                # from `ralphd.__version__` rather than hard-coded into
                # the static bundle -- `app.js` fetches this once on load
                # and renders it in the page header, beside (never
                # replacing) `server_version`'s wire-protocol tag below,
                # which this route does not touch.
                self._send_json({"version": __version__})
                return
            if segs == ["api", "runs"]:
                self._send_json({"runs": run_list(reg)})
                return
            if len(segs) == 4 and segs[:2] == ["api", "runs"] and segs[3] == "logs":
                run_id = segs[2]
                if not (reg / "runs" / run_id).is_dir():
                    self._send_json({"error": f"run {run_id} not found"}, 404)
                    return
                # Task 045 (H.3) / task 007 (G): the log-tail-specific `ETag`
                # -- `run_detail_etag`'s own four inputs plus the newest
                # iteration's transcript size, so an in-flight iteration's
                # the newest iteration's transcript growing (with `status.json`, `tasks.json`
                # and `meta.json` all untouched) still revalidates instead of
                # wrongly serving a stale `304` -- computed BEFORE the cache
                # lookup/render below, so a hit never pays for either.
                etag = log_tail_etag(reg, run_id)
                if self.headers.get("If-None-Match") == etag:
                    self._send_not_modified(etag)
                    return
                tail = qs.get("tail", [str(DEFAULT_LOG_TAIL)])[0]
                try:
                    tail_n = int(tail)
                except ValueError:
                    tail_n = DEFAULT_LOG_TAIL
                run_dir = reg / "runs" / run_id
                live, lines = _cached_rendered_log_lines(reg, run_dir, run_id, tail_n)
                self._send_json({"live": live, "lines": lines}, etag=etag)
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
            if (len(segs) in (4, 5) and segs[:2] == ["api", "runs"]
                    and segs[3] == "documents"):
                # Task 022 (#18.2): the run's state documents -- the listing
                # for the panel, one document for the dialog. Purely on-disk
                # (see `document_list`): these files are written into the run
                # dir and the job config dir by the agent/engine/`start`
                # itself, so there is no live answer to prefer.
                run_id = segs[2]
                if not (reg / "runs" / run_id).is_dir():
                    self._send_json({"error": f"run {run_id} not found"}, 404)
                    return
                if len(segs) == 4:
                    self._send_json(document_list(reg, run_id))
                    return
                view = document_view(reg, run_id, segs[4])
                if view is None:
                    self._send_json(
                        {"error": f"unknown document {segs[4]!r}"}, 404)
                    return
                self._send_json(view)
                return
            if (len(segs) >= 5 and segs[:2] == ["api", "runs"]
                    and segs[3] == "artifacts-raw"):
                # Task 023 (#54): the raw byte route for a previewable image's
                # dialog (task 025) -- `<img src>` cannot point at a JSON
                # endpoint. Same name shape as `artifacts` above (one
                # percent-encoded segment or several real ones), resolved
                # through the SAME single resolver, `state.artifact_relpath`
                # (via `artifact_raw`) -- an origin that also exposes `DELETE
                # /api/runs/<id>` does not get a second, looser path check.
                #
                # Never `mimetypes.guess_type` on the URL/filename: the
                # `Content-Type` is only ever one of `state.
                # ARTIFACT_IMAGE_MIME_TYPES`, decided from the file's own
                # sniffed magic bytes (`artifact_raw`), with
                # `X-Content-Type-Options: nosniff` on every answer. Anything
                # that is not a previewable image under the inline size cap --
                # text, opaque binary, absent, or an image over the cap --
                # answers 200 with today's `pull` wording as plain text
                # instead of bytes a browser cannot make sense of; only a name
                # `state.artifact_relpath` refuses outright (traversal, an
                # absolute path, an unknown run) answers 404.
                run_id = segs[2]
                if not (reg / "runs" / run_id).is_dir():
                    self._send_bytes(b"not found\n", 404, "text/plain",
                                     nosniff=True)
                    return
                name = "/".join(unquote(s) for s in segs[4:])
                raw = artifact_raw(reg / "runs" / run_id, name)
                if raw is None:
                    self._send_bytes(b"not found\n", 404, "text/plain",
                                     nosniff=True)
                    return
                self._send_bytes(
                    raw["body"], 200,
                    raw["contentType"] or "text/plain; charset=utf-8",
                    nosniff=True)
                return
            if (len(segs) >= 4 and segs[:2] == ["api", "runs"]
                    and segs[3] == "artifacts"):
                # Task 024 (#18.3): what the job left behind -- the listing for
                # the panel, one artifact for the dialog. Purely on-disk (see
                # `artifact_list`), like the documents above.
                #
                # An artifact is addressed by a PATH (`reflection/report.md`),
                # so it may span several URL segments -- and app.js may also
                # send it percent-encoded as one. Both spellings are unquoted
                # and rejoined here, then resolved (and guarded against
                # traversal) by the ONE resolver, `state.artifact_relpath`.
                run_id = segs[2]
                if not (reg / "runs" / run_id).is_dir():
                    self._send_json({"error": f"run {run_id} not found"}, 404)
                    return
                if len(segs) == 4:
                    self._send_json(artifact_list(reg, run_id))
                    return
                name = "/".join(unquote(s) for s in segs[4:])
                view = artifact_view(reg, run_id, name)
                if view is None:
                    self._send_json(
                        {"error": f"not an artifact name: {name!r}"}, 404)
                    return
                self._send_json(view)
                return
            if (len(segs) == 4 and segs[:2] == ["api", "runs"]
                    and segs[3] == "router"):
                # Task 022 (P, #80): this run's router-call listing (see
                # `router_calls_view` -- purely on-disk, like the artifacts
                # route just above). No per-entry dialog route yet: the
                # listing payload already carries every field task 022's
                # criteria asks for, so app.js's panel needs nothing more.
                run_id = segs[2]
                if not (reg / "runs" / run_id).is_dir():
                    self._send_json({"error": f"run {run_id} not found"}, 404)
                    return
                self._send_json(router_calls_view(reg, run_id))
                return
            if (len(segs) in (4, 5) and segs[:2] == ["api", "runs"]
                    and segs[3] == "approaches"):
                # Task 020 (#24): the run's archived approach history --
                # the listing for the panel (task 021), one approach's task
                # table + notes/findings for the dialog. Purely on-disk
                # (`archived_approach_view`/`archived_approaches_list`),
                # like the documents and artifacts above: `_archive_approach`
                # writes these files into the run dir itself, so a dead
                # run's history reads exactly like a live one's.
                run_id = segs[2]
                if not (reg / "runs" / run_id).is_dir():
                    self._send_json({"error": f"run {run_id} not found"}, 404)
                    return
                if len(segs) == 4:
                    self._send_json(approaches_list_view(reg, run_id))
                    return
                try:
                    approach = int(segs[4])
                except ValueError:
                    self._send_json(
                        {"error": f"bad approach number {segs[4]!r}"}, 404)
                    return
                view = approach_view(reg, run_id, approach)
                if view is None:
                    self._send_json(
                        {"error": f"run {run_id} has no archived approach "
                                  f"{approach}"}, 404)
                    return
                self._send_json(view)
                return
            if len(segs) == 4 and segs[:2] == ["api", "runs"] and segs[3] == "fault":
                # Task 026 (#18.4): why this run is (or last was) in trouble,
                # for the fault dialog behind the degraded/failure badge.
                # Purely on-disk (see `fault_view`), like the iteration,
                # document and artifact views above.
                run_id = segs[2]
                if not (reg / "runs" / run_id).is_dir():
                    self._send_json({"error": f"run {run_id} not found"}, 404)
                    return
                self._send_json(fault_view(reg, run_id))
                return
            if len(segs) == 4 and segs[:2] == ["api", "runs"] and segs[3] == "cost":
                # Task 028 (#18.5): what this run spent, per phase and per
                # approach, for the dialog behind the usage card's cost cell.
                # Purely on-disk (see `cost_view`), like the fault, iteration,
                # document and artifact views above.
                run_id = segs[2]
                if not (reg / "runs" / run_id).is_dir():
                    self._send_json({"error": f"run {run_id} not found"}, 404)
                    return
                self._send_json(cost_view(reg, run_id))
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
                if not (reg / "runs" / run_id).is_dir():
                    self._send_json({"error": f"run {run_id} not found"}, 404)
                    return
                # Task 003 (A.1): the ETag is cheap (four `stat()` calls) and
                # computed BEFORE the full detail build, so a hit never pays
                # for the tasks/iterations shaping it is about to skip.
                etag = run_detail_etag(reg, run_id)
                if self.headers.get("If-None-Match") == etag:
                    self._send_not_modified(etag)
                    return
                detail = run_detail(reg, run_id)
                if detail is None:
                    self._send_json({"error": f"run {run_id} not found"}, 404)
                    return
                self._send_json(detail, etag=etag)
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
            if len(segs) == 4 and segs[:2] == ["api", "runs"] and segs[3] == "unpause":
                # Task 006: the hub's "unpause" button -- the SAME engine
                # verb `ralphctl unpause`/`cmd_unpause` (cli/main.py) calls,
                # `POST /resume` (docs/api.md). No second unpause mechanism:
                # this is a pure proxy, same shape as the "retry" branch
                # above -- an unreachable run collapses to 503, and the
                # engine's own status code (its 200 `{"resumed": true}`,
                # or anything else it happens to answer with) is passed
                # straight through rather than flattened.
                run_id = segs[2]
                if not (reg / "runs" / run_id).is_dir():
                    self._send_json({"error": f"run {run_id} not found"}, 404)
                    return
                ok, code, resp = _proxy_json(reg, run_id, "POST", "/resume", body=b"")
                if not ok and not code:
                    self._send_json(
                        {"error": "run's API is unreachable — cannot unpause"}, 503)
                    return
                self._send_json(resp if isinstance(resp, dict) else {"detail": str(resp)},
                                code)
                return
            if len(segs) == 4 and segs[:2] == ["api", "runs"] and segs[3] == "rule":
                # Task 013 (v0.23 requirement A.2, #224): the hub run
                # page's ruling textarea. Pure proxy to the run's own
                # `POST /rule`, exactly the same shape as the "steer"
                # branch above -- deliberately its own endpoint, never a
                # variant of `/steer`, so a ruling is never discounted the
                # way review discounts steering by design.
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
                ok, code, resp = _proxy_json(reg, run_id, "POST", "/rule",
                                              body=json.dumps(body).encode())
                if not ok:
                    self._send_json(
                        {"error": "run's API unreachable or rejected the request",
                         "detail": resp}, code or 503)
                    return
                self._send_json(resp, code)
                return
            self._send_json({"error": "no such endpoint"}, 404)
        except Exception as e:
            self._send_json({"error": str(e)}, 500)

    def do_DELETE(self):
        # Task 030 (#19): DELETE /api/runs/<id> -- the hub's one-command
        # delete, i.e. `ralphctl rm --force`'s removal sequence behind the
        # run-id confirm dialog task 031 puts in front of it. Terminal runs
        # ONLY; everything else answers 409 with the reason and changes
        # nothing (see `delete_run`/`deletion_refusal`).
        parts = urlsplit(self.path)
        segs = [s for s in parts.path.split("/") if s]
        try:
            if len(segs) == 3 and segs[:2] == ["api", "runs"]:
                # Path segments are NOT unquoted for us; a percent-encoded
                # traversal must be seen for what it is before `delete_run`
                # guards against it.
                code, payload = delete_run(self.registry, unquote(segs[2]))
                self._send_json(payload, code)
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
