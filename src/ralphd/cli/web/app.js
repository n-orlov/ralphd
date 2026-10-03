/* ralphd hub — plain JS, no build step, no framework. Talks only to the
 * JSON endpoints served by ui_server.py (`/api/runs`, `/api/runs/<id>`,
 * `/api/runs/<id>/logs`, `POST /api/runs/<id>/steer`). */

const REFRESH_MS = 4000;
// Task 005 (A.3): the back-off target once the run detail poll decides it
// is quiescent (two identical `304`s in a row, or the run is no longer
// `running`) -- a fixed point inside the PRD's 15-30s band rather than a
// randomized one, so the browser-tier test pinning it has something exact
// to assert on.
const BACKOFF_POLL_MS = 20000;
let refreshTimer = null;

// -- where the hub is mounted --------------------------------------------
//
// Every request below is written as a root-absolute path (`/api/runs/...`),
// which is what `ui_server.py` routes on -- but the hub is often reached
// through a reverse proxy that mounts it under a path prefix and strips
// that prefix before proxying. `tailscale serve --set-path=/ralph` does
// exactly that, and forwards no header naming the prefix (only
// `X-Forwarded-{For,Host,Proto}`), so the *server* cannot know it is
// mounted at all. The *client* can: `document.currentScript.src` is the
// absolute URL the browser resolved `<script src="app.js">` to, so its
// directory is the mount root -- `/` when the hub is served at the root,
// `/ralph/` behind the mount above. `HUB_MOUNT` is that directory minus
// its trailing slash, i.e. `""` at the root, where `hubURL()` is then the
// identity and every request goes out byte-identical to before.
//
// One constraint this cannot lift: the document URL has to end in `/`.
// `https://host/ralph` resolves `app.js` and `style.css` against `/`, so
// neither loads and there is no client left to correct anything --
// `https://host/ralph/` is the canonical form (docs/cli.md, `ralphctl ui`).
const HUB_MOUNT = (function () {
  const src = document.currentScript && document.currentScript.src;
  if (!src) return "";
  try {
    return new URL(src, document.baseURI).pathname.replace(/\/[^/]*$/, "");
  } catch (e) {
    return "";
  }
})();

// Test-only instrumentation (never read by the app), like
// `window.__hubRefreshLoopCount` below: the prefix the bundle resolved.
window.__hubMount = () => HUB_MOUNT;

function hubURL(path) {
  return HUB_MOUNT + String(path);
}

// Task 016 (#27): `router()` is synchronous and does not await the async
// view renderer it kicks off (`renderRunList`/`renderRunDetail`), so a
// second navigation landing inside the first render's `await load()` used
// to (a) clear a `refreshTimer` that had not been *set* yet -- the first
// render only assigns it after its initial `load()` resolves -- orphaning
// that timer once it finally got installed, and (b) let the first render's
// now-superseded HTTP response still be applied to the DOM after the
// second render had already taken over. `renderGeneration` fixes both:
// every `router()` call mints a new generation and every render closure
// captures the generation it was born with, checking it before touching
// the DOM *and* before installing a timer. Only the current generation is
// ever allowed to do either, so exactly one navigation's refresh loop is
// ever live and a stale response is discarded rather than rendered.
let renderGeneration = 0;

// Test-only instrumentation (never read by the app itself): the number of
// `setInterval` handles currently outstanding for the auto-refresh loop.
// Wired through `startRefreshLoop`/`stopRefreshLoop` below rather than
// bumped next to the generation guard, so a test can prove the guard
// itself is what keeps this at 1 -- reverting the guard while leaving this
// counter in place makes the counter observably wrong, which is the point.
let liveRefreshLoops = 0;
window.__hubRefreshLoopCount = () => liveRefreshLoops;

function startRefreshLoop(fn, intervalMs) {
  liveRefreshLoops += 1;
  return setInterval(fn, intervalMs == null ? REFRESH_MS : intervalMs);
}

function stopRefreshLoop(id) {
  if (id) {
    clearInterval(id);
    liveRefreshLoops -= 1;
  }
}

// Task 005 (A.3): "stops polling while `document.hidden`" -- a single
// pause/resume point that sits below both `renderRunList` and
// `renderRunDetail`'s own refresh loops (only one is ever live at a time,
// per the `renderGeneration` guard above), rather than a duplicate
// `visibilitychange` listener in each view. `scheduleRefresh` is what
// every call site now uses in place of a bare `startRefreshLoop(load)`:
// it remembers the function/interval a hidden tab owes a resume, and
// either installs the timer right away (tab visible) or leaves it paused
// (tab hidden) for the listener below to pick up the moment the tab comes
// back. `router()` clears `pendingLoadFn` on every navigation so a view
// that got superseded while hidden never resumes into a page nobody is
// looking at any more.
let pendingLoadFn = null;
let pendingIntervalMs = REFRESH_MS;

function scheduleRefresh(fn, intervalMs) {
  pendingLoadFn = fn;
  pendingIntervalMs = intervalMs == null ? REFRESH_MS : intervalMs;
  if (document.hidden) return null;
  return startRefreshLoop(fn, pendingIntervalMs);
}

document.addEventListener("visibilitychange", () => {
  if (document.hidden) {
    if (refreshTimer) {
      stopRefreshLoop(refreshTimer);
      refreshTimer = null;
    }
  } else if (pendingLoadFn) {
    const fn = pendingLoadFn;
    fn();
    refreshTimer = startRefreshLoop(fn, pendingIntervalMs);
  }
});

function h(tag, attrs, children) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (k === "class") el.className = v;
    else if (k === "html") el.innerHTML = v;
    else if (k.startsWith("on") && typeof v === "function") el.addEventListener(k.slice(2), v);
    else el.setAttribute(k, v);
  }
  for (const c of children || []) {
    if (c == null) continue;
    el.appendChild(typeof c === "string" ? document.createTextNode(c) : c);
  }
  return el;
}

// Task 001 (#277): the hub's "apply now" steer form shows the SAME
// wording `ralphctl steer --now`/`ralphctl interrupt` prints
// (`_interrupt_report`, cli/main.py) -- fetched from the ONE place either
// phrase is spelled out (`web/interrupt-wording.json`, served as a plain
// static asset) rather than hardcoded here a second time, so the two
// surfaces cannot drift apart. Memoized: the file never changes at
// runtime, so one fetch per page load is enough.
let interruptWordingPromise = null;
function loadInterruptWording() {
  if (!interruptWordingPromise) {
    interruptWordingPromise = fetch(hubURL("/interrupt-wording.json"), { cache: "no-store" })
      .then((r) => r.json())
      .catch(() => ({}));
  }
  return interruptWordingPromise;
}

async function getJSON(path, headers) {
  const resp = await fetch(hubURL(path), { cache: "no-store", headers: headers || undefined });
  // Task 005 (A.3): a `304` (only ever returned for `GET /api/runs/<id>`,
  // task 003's `If-None-Match` hit path) carries no body to parse -- `body`
  // is `{}` rather than the result of a failed `.json()` on an empty
  // stream, so a caller can tell "nothing to parse" from "server sent
  // malformed JSON" by checking `status` first.
  const body = resp.status === 304 ? {} : await resp.json().catch(() => ({}));
  return { ok: resp.ok, status: resp.status, body, etag: resp.headers.get("ETag") };
}

function pill(text) {
  const cls = "pill pill-" + String(text || "unknown").toLowerCase().replace(/[^a-z0-9-]/g, "-");
  return h("span", { class: cls }, [String(text == null ? "unknown" : text)]);
}

// Task 005 (PRD requirement A.5, #269): the two markers that can ride
// alongside a state pill -- `pauseMarker` (task 017, the ONE `pauseReason`
// a plain unpause cannot answer) and the new `escalationUndeliveredMarker`
// (the run's latest escalation never reached the operator, after every
// delivery retry) -- shared between the runs-list row and the run-detail
// page's own state cell, so the two surfaces render the same two facts the
// same way rather than each inventing its own markup.
//
// Task 008 (v0.26, #269): when `pauseMarker` is exactly the literal
// `"\u23F8 parked for input"` text (mirrors `state.PARKED_FOR_INPUT_MARKER`
// -- inlined here, rather than a shared top-level const, so this function
// stays self-contained for the node-eval harnesses that grab it alone,
// e.g. tests/test_escalation_undelivered_marker.py) and a `runId` is
// available, the marker becomes a link to that run's own parked panel
// (`#/run/<id>`, the panel task 008 renders at the top of the page) --
// every OTHER marker text (the escalation-hold marker, the undelivered
// marker) stays plain text, since neither answers to a panel of its own.
function stateMarkerSpans(pauseMarker, escalationUndeliveredMarker, runId) {
  const spans = [];
  if (pauseMarker) {
    const linkable = runId != null && pauseMarker === "\u23F8 parked for input";
    const body = linkable
      ? h("a", { href: "#/run/" + encodeURIComponent(runId), class: "parked-panel-link" },
          [pauseMarker])
      : pauseMarker;
    spans.push(h("span", { class: "operator-input-marker" }, [" ", body]));
  }
  if (escalationUndeliveredMarker) {
    spans.push(h("span", { class: "escalation-undelivered-marker" },
      [" " + escalationUndeliveredMarker]));
  }
  return spans;
}

// The runs-list row's state cell. Task 024 (#8): a run recorded non-terminal
// whose API no longer answers (`r.containerGone`) carries the gone marker.
// Task 017 (PRD requirement A.7, #224): `r.pauseMarker` is the SAME marker
// `ralphctl runs` renders (`state.format_pause_marker`) for the one
// `pauseReason` a plain unpause cannot answer, `""` for every other run.
// Task 005 (PRD requirement A.5, #269): `r.escalationUndeliveredMarker`
// rides alongside it (both, either, or neither, via `stateMarkerSpans`) --
// on a gone row as well: the container being gone does not answer the
// pause, so a dead run still sitting on an undelivered escalation keeps
// both markers next to the gone marker.
// Task 008 (v0.26, #269): `r.runId` rides along too, so the parked-for-
// input marker on THIS row can link straight to that run's own panel.
function runRowStateChildren(r) {
  const children = [pill(r.state)];
  if (r.containerGone === true) {
    children.push(h("span", { class: "container-gone-marker" }, [" \u26A0 container gone"]));
  }
  children.push(...stateMarkerSpans(r.pauseMarker, r.escalationUndeliveredMarker, r.runId));
  return children;
}

// -- duration formatting (mirrors the compact style used by `ralphctl
// status`/`logs`: no millisecond noise, largest-two-units only) ----------

function fmtDuration(seconds) {
  if (seconds == null || !isFinite(seconds) || seconds < 0) return "";
  const s = Math.floor(seconds);
  const d = Math.floor(s / 86400);
  const h_ = Math.floor((s % 86400) / 3600);
  const m = Math.floor((s % 3600) / 60);
  const sec = s % 60;
  if (d > 0) return `${d}d ${h_}h`;
  if (h_ > 0) return `${h_}h ${m}m`;
  if (m > 0) return `${m}m ${sec}s`;
  return `${sec}s`;
}

function isoToEpoch(iso) {
  if (!iso) return null;
  const t = Date.parse(iso);
  return isNaN(t) ? null : t / 1000;
}

function durationBetween(startIso, endIso) {
  const a = isoToEpoch(startIso);
  const b = isoToEpoch(endIso);
  if (a == null || b == null) return null;
  return b - a;
}

// task 014: mirrors `main.py:_countdown_to()` -- a countdown to a future
// wall-clock instant, degrading to the raw value rather than throwing when
// the timestamp is not parseable (a status line must never be the thing
// that breaks the page).
function countdownTo(iso) {
  if (!iso) return "due now";
  const t = isoToEpoch(iso);
  if (t == null) return "at " + String(iso);
  const remaining = t - Date.now() / 1000;
  if (remaining <= 0) return "due now";
  return "in " + fmtDuration(remaining) + " (at " + String(iso) + ")";
}

// ------------------------------------------------- run list sorting (#9)

// Task 054 (#9): the run list is sortable on every column, defaulting to
// STARTED descending -- newest first, which is what an operator opening the
// hub wants, rather than the run-id alphabetical order the registry
// directory listing happens to yield.
//
// Two rules this implementation exists to honour:
//  * sort keys read RAW PAYLOAD VALUES (`startedAt`'s ISO instant,
//    `iterationsUsed`'s integer), never the rendered cell text -- sorting
//    "17/250" against "9/10" as strings, or ISO strings with different
//    offsets lexicographically, silently produces a wrong order that looks
//    plausible;
//  * the sort state lives HERE, outside the DOM, because `load()` rebuilds
//    the whole table every REFRESH_MS -- state kept in a class on a <th>
//    would be thrown away 4 seconds after the operator clicked it.
let runSort = { key: "startedAt", dir: -1 };

// Lifecycle order for the two enum columns: alphabetical would interleave
// live and finished runs (aborted, failed, running, starting, succeeded).
// Mirrors `main.py`'s `_STATUS_STATES` / the verdict progression in
// docs/api.md (no verdict yet -> unverified -> verified -> verified-with-
// residuals, requirement D.3).
const RUN_STATE_ORDER = ["starting", "running", "paused", "finishing", "timed_out", "succeeded", "failed", "aborted"];
const RUN_VERDICT_ORDER = ["", "unverified", "verified", "verified-with-residuals"];

function lifecycleRank(order, value) {
  const v = String(value == null ? "" : value).toLowerCase();
  const i = order.indexOf(v);
  // An unrecognised value sorts after every known one instead of throwing
  // the order away (a future state value must not scramble the table).
  return i >= 0 ? i : order.length;
}

function numOrNull(v) {
  const n = Number(v);
  return v == null || v === "" || isNaN(n) ? null : n;
}

const RUN_COLUMNS = [
  { label: "RUN", key: "runId", value: r => String(r.runId || "") },
  { label: "STATE", key: "state", value: r => lifecycleRank(RUN_STATE_ORDER, r.state) },
  { label: "VERDICT", key: "verdict", value: r => lifecycleRank(RUN_VERDICT_ORDER, r.verdict) },
  { label: "PHASE", key: "phase", value: r => String(r.phase || "") },
  // Task 008 (#16): the CELL text is the server-rendered `n/m`
  // (ui_server._with_approach_display -> engine.state.format_approach), while
  // the SORT value stays the raw numerator -- sorting on "10/12" as a string
  // would put approach 10 before approach 2.
  { label: "APPROACH", key: "approach", value: r => numOrNull(r.approach) },
  // Task 014 (#21): the CELL text is the server-rendered `5/7`
  // (ui_server._row_tasks -> engine.state.format_task_fraction) plus the
  // trouble flags; the SORT value is the completion RATIO, so `5/7` (0.71)
  // ranks above `100/250` (0.4) -- which neither a string sort of the cells
  // nor a sort on the bare numerator would get right. A run with no plan has
  // no ratio at all (null), so `cmpValues` puts it last ascending instead of
  // pretending it is 0% done.
  { label: "TASKS", key: "tasks", value: r => taskRatio(r) },
  // numeric on iterationsUsed -- NOT the rendered "17/250" cell text
  { label: "ITERATIONS", key: "iterationsUsed", value: r => numOrNull(r.iterationsUsed) },
  // epoch seconds from the raw ISO instant -- NOT the ISO string, which
  // only sorts correctly while every run happens to use the same offset
  { label: "STARTED", key: "startedAt", value: r => isoToEpoch(r.startedAt) },
];

function approachText(o) {
  // Task 008 (#16): one accessor for both the run-list cell and the run-detail
  // row. The server sends `approachDisplay` (formatted by the same
  // `engine.state.format_approach` the CLI prints); the raw-counter fallback
  // only matters for a payload that predates that field, and it deliberately
  // shows the bare numerator rather than inventing a denominator here.
  if (o && typeof o.approachDisplay === "string") return o.approachDisplay;
  return String(!o || o.approach == null ? "" : o.approach);
}

// ----------------------------------------------- task progress cell (#21)

function taskRatio(r) {
  // Sort value for the TASKS column: completed/total as a fraction of one,
  // or null when there is no plan to have progress through -- the same
  // "unknown is not zero" rule `engine.state.format_task_fraction` follows
  // when it renders an EMPTY string rather than `0/0`.
  const total = numOrNull(r && r.tasksTotal);
  const completed = numOrNull(r && r.tasksCompleted);
  if (total == null || total <= 0) return null;
  return (completed == null ? 0 : completed) / total;
}

function taskCell(r) {
  // The fraction is whatever the SERVER rendered (`tasksDisplay`, task 013):
  // one formatter behind the hub, `ralphctl runs` and `ralphctl status`,
  // never a second spelling in JS. Empty string for a plan-less run.
  const fraction = r && typeof r.tasksDisplay === "string" ? r.tasksDisplay : "";
  const trouble = r && Array.isArray(r.tasksTrouble) ? r.tasksTrouble.map(String) : [];
  const kids = [fraction];
  if (fraction && trouble.length > 0) {
    // Worded exactly as `format_task_counts` words it ("1 validation-failed",
    // "2 in-progress") -- the server did the wording, so a plan stuck on a
    // failed validation cannot look like ordinary progress here.
    kids.push(h("span", { class: "tasks-trouble" }, [" \u26A0 " + trouble.join(", ")]));
  }
  if (fraction && r && r.tasksStale === true) {
    // Task 005 (#15) labels this on run detail; in the list the marker says
    // the fraction is the last plan that PARSED (a poll landed mid-rewrite).
    kids.push(h("span", { class: "pill pill-stale" }, [" stale"]));
  }
  // Task 019 (#77/#78 operator-facing half): the vigilant lifecycle, off the
  // raw counts the server sends (`tasksAwaitingValidation`, `taskEffectiveTier`
  // -- `engine.state.TasksRead.row_fields`), never by re-parsing `tasksSummary`'s
  // rendered sentence back into numbers. This particular status is not
  // trouble (it is the vigilant worker's ordinary claim state), so it gets
  // its own class rather than joining `tasksTrouble`'s warning styling, and
  // its wording here is deliberately generic (no literal status-name
  // string), same discipline `taskStatusText` follows for the per-task rows.
  if (fraction && r && Number(r.tasksAwaitingValidation) > 0) {
    kids.push(h("span", { class: "tasks-vigilant" },
      [" ", pill(r.tasksAwaitingValidation + " awaiting review")]));
  }
  if (fraction && r && r.taskEffectiveTier) {
    kids.push(h("span", { class: "tasks-tier" }, [" ", pill(String(r.taskEffectiveTier))]));
  }
  const attrs = { class: "tasks-cell",
                  "data-tasks-source": String((r && r.tasksSource) || "") };
  // The whole sentence (`5/7 completed (1 in-progress, 1 pending)`) on hover;
  // the cell itself stays a column, not a paragraph.
  if (r && r.tasksSummary) attrs.title = String(r.tasksSummary);
  return h("td", attrs, kids);
}

function runColumn(key) {
  return RUN_COLUMNS.find(c => c.key === key) || RUN_COLUMNS[RUN_COLUMNS.length - 1];
}

function cmpValues(a, b) {
  // Missing values (no startedAt, no approach yet) sort last in ascending
  // order rather than pretending to be 0/"".
  const aMissing = a == null || a === "";
  const bMissing = b == null || b === "";
  if (aMissing || bMissing) return aMissing && bMissing ? 0 : (aMissing ? 1 : -1);
  if (typeof a === "number" && typeof b === "number") return a < b ? -1 : (a > b ? 1 : 0);
  const as = String(a), bs = String(b);
  return as < bs ? -1 : (as > bs ? 1 : 0);
}

function sortRuns(runs) {
  const col = runColumn(runSort.key);
  return [...runs].sort((a, b) => {
    const r = cmpValues(col.value(a), col.value(b));
    if (r !== 0) return r * runSort.dir;
    // Deterministic tie-break so the 4s rebuild never reshuffles equal rows.
    return cmpValues(String(a.runId || ""), String(b.runId || ""));
  });
}

function toggleRunSort(key) {
  if (runSort.key === key) {
    runSort = { key, dir: -runSort.dir };
    return;
  }
  // First click on a new column: time-like and numeric columns are most
  // useful biggest/newest first, text columns A->Z.
  // TASKS is deliberately NOT in this list: least-complete first is the
  // useful first click (those runs still owe work), and ascending also puts
  // the plan-less runs -- which have no progress to compare -- last.
  const desc = key === "startedAt" || key === "iterationsUsed" || key === "approach";
  runSort = { key, dir: desc ? -1 : 1 };
}

function sortIndicator(key) {
  if (runSort.key !== key) return "";
  return runSort.dir < 0 ? " \u25BC" : " \u25B2";
}

// ---------------------------------------------------- text dialogs (#1, #2)

// Task 056 (#1): agent/operator-authored text (a run's PRD; and, task 057
// (#2), a task's detail) opens in a modal <dialog> instead of forcing the
// operator back to `ralphctl` or the run dir on disk.
//
// Rendering discipline (task 014): the text is inserted as TEXT NODES only,
// via `h()`, never `innerHTML`. A PRD is arbitrary markdown written outside
// this page's trust boundary -- rendering it as HTML would let any run dir
// inject markup/script into the hub, and would also mangle the very thing
// the operator wants to read (a fenced code block full of `<`).
//
// Exactly one dialog exists at a time: it is removed on close, so the 4s
// `load()` rebuild of the page behind it can never accumulate stale copies.
//
// `showDialog` is that invariant, shared by every kind of dialog (the text
// dialog below and task 031's delete confirmation): it removes ANY dialog
// currently in the document before showing the new one, so two *different*
// kinds cannot stack either.
function showDialog(dlg) {
  for (const previous of document.querySelectorAll("dialog")) previous.remove();
  dlg.addEventListener("close", () => dlg.remove());
  document.body.appendChild(dlg);
  if (typeof dlg.showModal === "function") dlg.showModal();
  else dlg.setAttribute("open", "open");
  return dlg;
}

function openTextDialog(title, text, note) {
  const dlg = h("dialog", { id: "text-dialog", class: "text-dialog" }, [
    h("h3", { class: "dialog-title" }, [String(title)]),
    note ? h("p", { class: "muted dialog-note" }, [String(note)]) : null,
    // <pre> keeps the markdown's own line breaks/indentation readable
    // without interpreting any of it.
    h("pre", { class: "dialog-body" }, [String(text)]),
    h("form", { method: "dialog", class: "dialog-close" }, [
      h("button", { type: "submit" }, ["close"]),
    ]),
  ]);
  return showDialog(dlg);
}

// ------------------------------------------- mutating-proxy reason (#155, G.7)

// Task 069 (#155, requirement G.7): the hub wraps a proxy rejection as
// `{error, detail: <engine body>}` (`ui_server.py`'s `do_POST`), and the
// engine's OWN problem body can itself carry a `detail` key -- so a rejection
// forwarded through a second hop (e.g. the steer proxy relaying the engine's
// `409 {"detail": {"title": ..., "detail": "steering has no effect"}}` for a
// run that finished between the browser's submit and the proxy's forward)
// nests one `detail` object inside another. The steer form's error branch
// and `renderRetryNow`'s fallback used to each spell their own `||` chain
// (`j.detail && (j.detail.detail || j.detail.title)) || j.error` and
// `j.detail || j.title || j.error || status` respectively) that stops at the
// first TRUTHY value -- which, for a doubly-nested body, is an object, and
// `String()` on an object renders the literal text `[object Object]` instead
// of the reason underneath it. This helper instead walks down the `detail`
// chain while it keeps finding an OBJECT, then reads the innermost level's
// own `detail` (if it is a string) or `title`, falling back to the
// outermost `error` and finally to the numeric status -- one walk, shared by
// both control surfaces, so they render the SAME reason for the SAME cause.
function innermostReason(body, fallback) {
  let cur = body;
  while (cur && typeof cur === "object" && cur.detail && typeof cur.detail === "object") {
    cur = cur.detail;
  }
  if (cur && typeof cur === "object") {
    if (typeof cur.detail === "string") return cur.detail;
    if (typeof cur.title === "string") return cur.title;
  }
  if (body && typeof body === "object" && typeof body.error === "string") return body.error;
  return fallback;
}

// ------------------------------------------------------- delete a run (#19)

// Task 031 (#19): deleting a finished run used to mean leaving the hub for
// two `ralphctl` commands (`stop`, then `rm`). The run list and the run detail
// page now offer it directly -- behind a confirmation naming the run id, and
// for terminal runs ONLY.
//
// Discipline:
//  * whether the control is offered at all is the SERVER's answer
//    (`deletable`, from `ui_server.deletion_fields` -> the very gate
//    `DELETE /api/runs/<id>` applies), never a state comparison re-spelled
//    here -- a browser deciding for itself which states are "finished" is how
//    a UI ends up offering to destroy work in flight;
//  * when it is refused, the REASON shown is the server's sentence
//    (`deleteRefusal`, `ui_server.DELETE_REFUSED_ACTIVE`/`_UNKNOWN`), text
//    nodes only, so the hub cannot word a refusal differently from the
//    endpoint that issued it;
//  * the confirmation names the run id and says what will be removed, because
//    this is the one irreversible action in the hub.

const DELETE_LABEL = "delete";
const DELETE_BUSY = "deleting…";
const DELETE_FAILED = "delete failed: ";

function deleteConfirmTitle(runId) {
  return "Delete run — " + String(runId);
}

function deleteConfirmText(runId) {
  return [
    "Delete run " + String(runId) + " ?",
    "",
    "This removes that run's container, its sibling containers, its run",
    "directory (transcripts, tasks.json, artifacts) and its job config —",
    "the same removal ralphctl rm --force performs. It cannot be undone.",
  ].join("\n");
}

async function requestRunDeletion(runId) {
  const resp = await fetch(hubURL(`/api/runs/${encodeURIComponent(runId)}`),
                           { method: "DELETE" });
  const body = await resp.json().catch(() => ({}));
  return { ok: resp.ok, status: resp.status, body };
}

// `after` is what the surface does once the run is really gone (the run list
// reloads; the detail page of a run that no longer exists goes back to the
// list). A failure leaves the dialog open with the server's own error text --
// a refusal the button did not expect (the run started again between poll and
// click) must be readable, not silently swallowed.
function openDeleteDialog(runId, after) {
  const statusEl = h("p", { class: "muted dialog-note", id: "delete-status" }, []);
  const confirm = h("button", {
    type: "button", class: "delete-confirm", id: "delete-confirm",
  }, [DELETE_LABEL + " " + String(runId)]);
  const dlg = h("dialog", { id: "delete-dialog", class: "text-dialog delete-dialog" }, [
    h("h3", { class: "dialog-title" }, [deleteConfirmTitle(runId)]),
    h("pre", { class: "dialog-body" }, [deleteConfirmText(runId)]),
    statusEl,
    h("form", { method: "dialog", class: "dialog-close" }, [
      h("button", { type: "submit", class: "delete-cancel", id: "delete-cancel" },
        ["cancel"]),
      confirm,
    ]),
  ]);
  confirm.addEventListener("click", async () => {
    confirm.disabled = true;
    statusEl.textContent = DELETE_BUSY;
    let result;
    try {
      result = await requestRunDeletion(runId);
    } catch (e) {
      statusEl.textContent = DELETE_FAILED + e;
      confirm.disabled = false;
      return;
    }
    if (!result.ok) {
      const b = result.body || {};
      statusEl.textContent = DELETE_FAILED +
        String(b.error || b.detail || b.title || result.status);
      confirm.disabled = false;
      return;
    }
    dlg.close();
    if (after) after();
  });
  return showDialog(dlg);
}

// The affordance itself, shared by the run list's row and the run detail
// page's action bar -- one control, so the two surfaces cannot disagree about
// whether a run may be deleted or about why not.
function deleteControl(runId, o, after) {
  const deletable = o && o.deletable === true;
  const reason = (o && typeof o.deleteRefusal === "string") ? o.deleteRefusal : "";
  const attrs = {
    type: "button",
    class: "delete-run",
    "data-delete-run": String(runId),
  };
  if (deletable) {
    attrs.title = "delete this run (container, run dir and job config)";
    attrs.onclick = () => { openDeleteDialog(runId, after); };
  } else {
    // Disabled, with the reason -- not hidden: an operator who came here to
    // clean up must learn that the run is still active, not find a missing
    // button and wonder whether the hub can do this at all.
    attrs.disabled = "disabled";
    attrs["data-delete-refused"] = "1";
    if (reason) attrs.title = reason;
  }
  const cell = h("span", { class: "delete-cell" }, [
    h("button", attrs, [DELETE_LABEL]),
  ]);
  if (!deletable && reason) {
    cell.appendChild(h("span", { class: "muted delete-refusal" }, [reason]));
  }
  return cell;
}

// The PRD comes from the hub's `GET /api/runs/<id>/prd` (ui_server.py),
// which proxies the run's live `GET /prd` and falls back to the on-disk
// run dir for an unreachable run -- so this works for a dead run too, and
// says which of the two it got, in the same wording style as the log tail's
// on-disk-snapshot label.
async function openPrdDialog(runId) {
  const { ok, body } = await getJSON(`/api/runs/${encodeURIComponent(runId)}/prd`);
  const text = (ok && body && typeof body.text === "string" && body.text)
    ? body.text : "(failed to load the PRD)";
  const note = (ok && body && body.live !== true)
    ? "(on-disk snapshot — the run's API is not reachable)" : null;
  return openTextDialog("PRD — " + runId, text, note);
}

// Task 057 (#2): a task row in the run-detail view opens the plan entry the
// agent is actually working against -- its status, its successCriteria (the
// text that decides whether the task is done) and its scheduling fields --
// instead of making the operator open the run dir's tasks.json by hand.
//
// The task record is already in the page (the detail payload's `tasks`), so
// no new endpoint is needed; it is rendered through `openTextDialog`, i.e.
// as text nodes only, because criteria are agent/operator-authored prose
// that routinely contains `<`, backticks and fenced snippets.
// Task 025 (#33): which KIND of `failed` a task record is -- `failed` means
// both "a verifier judged the requirement unmet" and "the engine spent this
// task's validation rounds", and the two call for different operator action.
//
// The answer is DERIVED SERVER-SIDE (`engine.state.task_failure_kinds`, via
// `ui_server._with_task_failure_kinds`) and arrives as the payload's
// `taskFailureKinds` map, because deriving it here would mean a second copy of
// the migration rule for records written before the `failureKind` label
// existed. The record's own label is the fallback for a payload served by an
// older hub, and only ever for a task that IS failed.
function taskFailureKind(doc, t) {
  const status = t && t.status == null ? "" : String((t && t.status) || "");
  if (status !== "failed") return null;
  const map = doc && typeof doc.taskFailureKinds === "object" && doc.taskFailureKinds
    ? doc.taskFailureKinds : null;
  const id = t && t.id == null ? "" : String(t.id);
  const derived = map ? map[id] : null;
  if (typeof derived === "string" && derived) return derived;
  const own = t && t.failureKind;
  return typeof own === "string" && own ? own : null;
}

// Task 021 (#60 pillar 3): a task the dependency graph dooms -- a
// `dependsOn` entry that reached `failed`/`skipped`, directly or through
// another doomed task -- WITHOUT the harness ever changing this task's own
// `status`. "Dead dependencies are harness-derived": the answer is DERIVED
// SERVER-SIDE (`engine.state.task_blocked_by`, via
// `ui_server._with_task_blocked_by`) and arrives as the payload's
// `taskBlockedBy` map, the exact sibling of `taskFailureKinds` above -- never
// recomputed here from `dependsOn` in JS, which would be a second copy of
// the SATISFIES-a-dependency rule (`engine.state.TASK_DEPENDENCY_SATISFIED_
// STATUSES` -- a task's OWN terminal-success status only; `skipped`
// deliberately does not qualify). Returns the sorted list of ancestor ids
// that doom this task, or null when it is not blocked at all.
function taskBlockedBy(doc, t) {
  const map = doc && typeof doc.taskBlockedBy === "object" && doc.taskBlockedBy
    ? doc.taskBlockedBy : null;
  if (!map) return null;
  const id = t && t.id == null ? "" : String(t.id);
  const by = map[id];
  return Array.isArray(by) && by.length > 0 ? by.map(String) : null;
}

// Task 011 (PRD requirement I, #98): a task the steer hat inserted carries
// `origin: "steer"` (`engine.state.TASK_ORIGIN_STEER`) directly on the
// record -- unlike `taskFailureKind` above, there is no legacy label to
// migrate and nothing to derive server-side; the raw field IS the answer, so
// this is a plain predicate, not a lookup into a payload map. A
// planner-proposed task carries no `origin` key at all (backward compatible
// with every plan written before this existed).
function isSteeringOrigin(t) {
  return !!(t && t.origin === "steer");
}

// Task 021 (PRD requirement N, #102): the sibling predicate for a task the
// WORKER hat inserted through its own `proposed_tasks` split proposal
// (`engine.state.TASK_ORIGIN_WORKER`) -- same reasoning as `isSteeringOrigin`
// just above.
function isWorkerOrigin(t) {
  return !!(t && t.origin === "worker");
}

// The status as `engine.state.format_task_status` words it -- `failed
// (validation-exhausted)`, or `pending (blocked by 004, 005)` -- so the pill,
// the task dialog and `ralphctl tasks` say the same words about the same
// task.
function taskStatusText(doc, t) {
  const status = t && t.status ? String(t.status) : "unknown";
  const kind = taskFailureKind(doc, t);
  let text = kind ? status + " (" + kind + ")" : status;
  if (isSteeringOrigin(t)) text += " (steering-origin)";
  if (isWorkerOrigin(t)) text += " (worker-origin)";
  const by = taskBlockedBy(doc, t);
  if (by) text += " (blocked by " + by.join(", ") + ")";
  // Task 023 (#156, PRD requirement C.1): a task still carrying its own
  // `deferredUntil` (a worker's `verdict: "deferred"` report, task 021)
  // gets " (deferred until <t>)" appended verbatim from that field --
  // the same words `engine.state.format_task_status` renders server-side,
  // read straight off the raw record rather than re-derived.
  if (t && t.deferredUntil) text += " (deferred until " + String(t.deferredUntil) + ")";
  return text;
}

function taskDialogText(t, doc) {
  const lines = [];
  lines.push("status: " + taskStatusText(doc, t));
  if (isSteeringOrigin(t)) lines.push("origin: steer");
  if (isWorkerOrigin(t)) lines.push("origin: worker");
  if (t.priority != null) lines.push("priority: " + String(t.priority));
  if (Array.isArray(t.dependsOn) && t.dependsOn.length > 0) {
    lines.push("dependsOn: " + t.dependsOn.map(String).join(", "));
  }
  if (t.validationAttempts != null) {
    lines.push("validationAttempts: " + String(t.validationAttempts));
  }
  lines.push("");
  lines.push("successCriteria:");
  lines.push(String(t.successCriteria || "(none recorded)"));
  if (t.validationNotes) {
    lines.push("");
    lines.push("validationNotes:");
    lines.push(String(t.validationNotes));
  }
  // Task 037 (G.5): the iterations behind the row's own timeDisplay/
  // costDisplay total (task 036, G.4) -- `iterationsBreakdown`, the SAME
  // list `engine.state.task_totals` built the total from, server-side, one
  // line per iteration this task's own `assignedTaskId` named, in this
  // run's own chronological order. Never re-derived here: `tier`/
  // `costDisplay` per entry are read verbatim off the server payload.
  if (Array.isArray(t.iterationsBreakdown) && t.iterationsBreakdown.length > 0) {
    lines.push("");
    lines.push("iterations behind this total:");
    for (const it of t.iterationsBreakdown) {
      const num = it && it.number != null ? String(it.number) : "?";
      const tier = it && it.tier ? String(it.tier) : "\u2014";
      const cost = it && it.costDisplay ? String(it.costDisplay) : "\u2014";
      lines.push("  #" + num + " -- tier: " + tier + ", cost: " + cost);
    }
  }
  // Task 039 (PRD I.1): the states this task passed through, oldest first
  // -- `t.transitionHistory`, one entry per "task" event on this run's own
  // events.jsonl (`engine.state.task_transition_history`), server-shaped
  // and rendered verbatim, exactly the `iterationsBreakdown` discipline
  // just above. `cause` is present only when the event itself recorded
  // one (a worker's deferred/unsatisfiable report, or a forced split) --
  // absent for an ordinary dispatch, never invented here.
  if (Array.isArray(t.transitionHistory) && t.transitionHistory.length > 0) {
    lines.push("");
    lines.push("state history:");
    for (const tr of t.transitionHistory) {
      const when = tr && tr.tsLocal ? String(tr.tsLocal) : "\u2014";
      const from = tr && tr.oldStatus != null ? String(tr.oldStatus) : "(none)";
      const to = tr && tr.newStatus != null ? String(tr.newStatus) : "?";
      let line = "  " + when + ": " + from + " -> " + to;
      if (tr && tr.cause) line += " (" + String(tr.cause) + ")";
      lines.push(line);
    }
  }
  return lines.join("\n");
}

function openTaskDialog(t, doc) {
  const id = t.id == null ? "?" : String(t.id);
  const title = "Task " + id + (t.title ? " — " + String(t.title) : "");
  return openTextDialog(title, taskDialogText(t, doc), null);
}

// ------------------------------------------------- iteration detail (#18.1)

// Task 020 (#18.1, restructured into labelled regions by #81): a timeline
// row used to be a dead summary -- phase, model, duration -- and "why did
// iteration 47 end like that, and what did the agent actually do in it"
// meant leaving the hub for `ralphctl iteration`/the run dir. Clicking a row
// now opens that iteration's own story as three distinctly LABELLED
// regions, matching the on-disk layout: outcome (`meta.json`), input
// (`prompt.md`), logs (`output.jsonl`) -- with OUTCOME FIRST, so an
// iteration that ended `exitCode: -2, interrupted: true` is readable
// without scrolling past the transcript.
//
// Every string in each region was formatted in Python -- `outcomeText` is
// `state.iteration_summary_lines` joined, `promptText` is
// `state.read_iteration_prompt`, `logsText` is the shared `log_render`
// renderer's output behind `state.format_iteration_log_header` -- all three
// arriving in `ui_server.iteration_view`'s payload, the SAME shaping
// `ralphctl iteration` prints (`state.iteration_detail_text`). app.js only
// puts each field into its own text node: it cannot word an exit reason, a
// duration or a token count differently from the CLI, and the transcript is
// rendered by the same code as the log tail.
// The endpoint is purely on-disk, so this works for a dead run with no
// container -- hence no live/snapshot note here (unlike the PRD dialog).
function iterationTitle(runId, number) {
  return "Iteration #" + String(number) + " — " + String(runId);
}

// The three region labels, in outcome-first display order. A STRUCTURAL
// label (which file backs which region), not a derived fact like an exit
// reason or a cost -- unlike those, it can never drift between `ralphctl
// iteration` and the hub, so spelling it once here is safe; the words
// themselves (outcome/input/logs) are `state.ITERATION_REGION_OUTCOME/
// INPUT/LOGS`'s own vocabulary, task 020's (#81).
const ITERATION_REGIONS = [
  { key: "outcome", label: "Outcome (meta.json)", field: "outcomeText" },
  { key: "input", label: "Input (prompt.md)", field: "promptText" },
  { key: "logs", label: "Logs (output.jsonl)", field: "logsText" },
];

function iterationRegion(key, label, text) {
  return h("section", { class: "iteration-region",
                        id: "iteration-region-" + key,
                        "data-iteration-region": key }, [
    h("h4", { class: "iteration-region-label" }, [label]),
    h("pre", { class: "dialog-body" }, [String(text == null ? "" : text)]),
  ]);
}

async function openIterationDialog(runId, number) {
  const path = `/api/runs/${encodeURIComponent(runId)}/iterations/` +
    encodeURIComponent(String(number));
  const { ok, body } = await getJSON(path);
  if (!ok || !body) {
    return openTextDialog(iterationTitle(runId, number),
      "(failed to load iteration " + String(number) + ")", null);
  }
  const dlg = h("dialog", { id: "text-dialog",
                           class: "text-dialog iteration-dialog" }, [
    h("h3", { class: "dialog-title" }, [iterationTitle(runId, number)]),
    ...ITERATION_REGIONS.map(r => iterationRegion(r.key, r.label, body[r.field])),
    h("form", { method: "dialog", class: "dialog-close" }, [
      h("button", { type: "submit" }, ["close"]),
    ]),
  ]);
  return showDialog(dlg);
}

// --------------------------------------------------- state documents (#18.2)

// Task 022 (#18.2): a run's prose -- the worker's handoff `notes.md`, the
// reviewer's `review-findings.md`, the `composite-prd.md` an approach restart
// wrote, and the effective `job.yaml` -- used to be reachable only by knowing
// the registry layout and `cat`-ing files on the host (which is also how
// `job.yaml`'s secrets got read out loud). The panel lists what
// `GET /api/runs/<id>/documents` reports and opens each existing document in
// THE single `openTextDialog`.
//
// Every string shown was formatted in Python: the button's size cell is
// `state.format_run_document_size` (`sizeDisplay`) and the dialog body is
// `state.run_document_text`, the very text `ralphctl docs <run> <name>` prints
// -- and `job.yaml` arrives already redacted by the server, so there is no raw
// body in this page to leak. Text nodes only: these documents are
// agent/operator-authored markdown from outside this page's trust boundary,
// exactly like a PRD. The endpoint is on-disk, so this works for a dead run
// with no container (hence no live/snapshot note, like the iteration dialog).

const DOCUMENT_LOAD_FAILED = "failed to load the run's state documents";

function documentTitle(runId, doc) {
  return String(doc.name || doc.key || "document") + " — " + String(runId);
}

async function openDocumentDialog(runId, doc) {
  const key = String(doc.key || doc.name || "");
  const path = `/api/runs/${encodeURIComponent(runId)}/documents/` +
    encodeURIComponent(key);
  const { ok, body } = await getJSON(path);
  const text = (ok && body && typeof body.text === "string" && body.text)
    ? body.text
    : "(failed to load " + key + ")";
  return openTextDialog(documentTitle(runId, doc), text, null);
}

function renderDocuments(box, ok, body, runId) {
  box.innerHTML = "";
  if (!ok || !body) {
    box.appendChild(h("p", { class: "muted" }, [DOCUMENT_LOAD_FAILED]));
    return;
  }
  const docs = Array.isArray(body.documents) ? body.documents : [];
  const list = h("div", { class: "document-list", id: "document-list" }, []);
  for (const d of docs) {
    const label = String(d.name || d.key || "");
    const size = String(d.sizeDisplay || "");
    if (d.exists === true) {
      // A <button> rather than a role=button div: keyboard reachability and
      // Enter/Space come for free from the platform here.
      list.appendChild(h("button", {
        type: "button",
        class: "document-item",
        "data-document": String(d.key || ""),
        title: String(d.title || ""),
        onclick: () => { openDocumentDialog(runId, d); },
      }, [
        label,
        h("span", { class: "muted document-size" }, [" " + size]),
      ]));
    } else {
      // A document this run never wrote (or one whose config dir is out of
      // reach) gets no dialog -- but its absence is itself an answer, stated
      // in the server's own wording rather than by omitting the row.
      list.appendChild(h("span", {
        class: "document-item document-absent",
        "data-document": String(d.key || ""),
        "data-document-absent": "1",
        title: String(d.title || ""),
      }, [label, h("span", { class: "muted document-size" }, [" " + size])]));
    }
  }
  box.appendChild(list);
  if (body.notice) {
    // Wording comes from the server (`ui_server.NO_DOCUMENTS`), like the log
    // tail's `NO_TRANSCRIPT` and the steering panel's `NO_STEERING`.
    box.appendChild(h("p", { class: "muted", id: "documents-notice" },
                      [String(body.notice)]));
  }
}

async function loadDocuments(runId) {
  const box = document.getElementById("documents-box");
  const { ok, body } = await getJSON(
    `/api/runs/${encodeURIComponent(runId)}/documents`);
  if (!box) return;
  renderDocuments(box, ok, body, runId);
}

// -------------------------------------------------------- artifacts (#18.3)

// Task 024 (#18.3): what the job left behind in `artifacts/` -- above all the
// reflect phase's post-mortem (`reflection/report.md`) and the prompt/skill
// diff it proposes (`reflection/suggestions.diff`) -- used to be reachable only
// by knowing the registry layout and `cat`-ing files on the host, so the whole
// point of the reflect phase was invisible from the hub. The panel lists what
// `GET /api/runs/<id>/artifacts` reports and opens each entry in THE single
// `openTextDialog`.
//
// Every string shown was formatted in Python: the row's size cell is
// `state.format_artifact_size` (`sizeDisplay`), the row's label is the
// artifact's own key/path, and the dialog body is `state.artifact_text`, the
// very text `ralphctl artifacts <run> show <name>` prints -- including its
// wording for an artifact that vanished, is empty, or is binary (a report is
// agent-authored markdown, a diff is full of `<`/`>`; both go in as TEXT NODES
// only, exactly like a PRD). The endpoint is on-disk, so this works for a dead
// run with no container (hence no live/snapshot note).

const ARTIFACT_LOAD_FAILED = "failed to load the run's artifacts";

function artifactTitle(runId, a) {
  return String(a.path || a.key || "artifact") + " — " + String(runId);
}

// Task 025 (#54): which of the two dialog shapes an artifact gets is decided
// from `kind`, the ONE server-decided field (`state._artifact_kind`, task 022)
// -- sniffed from magic bytes, never from the name or extension. There is no
// client-side list of "image extensions" here and there must never be one:
// app.js only ever asks "does the listing/view say kind === "image"?", the
// exact question the server already answered when it built the row.
const ARTIFACT_KIND_IMAGE = "image";

function artifactRawPath(runId, name) {
  // The raw byte route (task 023): the ONLY place a `<img src>` can point --
  // the JSON dialog route below never carries bytes, only text.
  return hubURL(`/api/runs/${encodeURIComponent(runId)}/artifacts-raw/` +
    encodeURIComponent(name));
}

function openImageArtifactDialog(runId, a, name) {
  const dlg = h("dialog", { id: "text-dialog", class: "text-dialog" }, [
    h("h3", { class: "dialog-title" }, [artifactTitle(runId, a)]),
    h("img", { class: "dialog-image", src: artifactRawPath(runId, name),
              alt: name }),
    h("form", { method: "dialog", class: "dialog-close" }, [
      h("button", { type: "submit" }, ["close"]),
    ]),
  ]);
  return showDialog(dlg);
}

async function openArtifactDialog(runId, a) {
  const name = String(a.path || a.key || "");
  // The listing row (`a`) already carries the server's `kind` -- no second
  // fetch is needed to decide the SHAPE of the dialog, only (for a
  // non-image) to get the body text. A tarball (kind === "binary") and plain
  // text both still go through the JSON route below and `openTextDialog`,
  // unchanged: the `pull` wording for a tarball is exactly today's binary
  // path, not a new one.
  if (a.kind === ARTIFACT_KIND_IMAGE) {
    return openImageArtifactDialog(runId, a, name);
  }
  const path = `/api/runs/${encodeURIComponent(runId)}/artifacts/` +
    encodeURIComponent(name);
  const { ok, body } = await getJSON(path);
  const text = (ok && body && typeof body.text === "string" && body.text)
    ? body.text
    : "(failed to load " + name + ")";
  return openTextDialog(artifactTitle(runId, a), text, null);
}

function renderArtifacts(box, ok, body, runId) {
  if (!ok || !body) {
    box.innerHTML = "";
    delete box.dataset.artifacts;
    box.appendChild(h("p", { class: "muted" }, [ARTIFACT_LOAD_FAILED]));
    return;
  }
  const items = Array.isArray(body.artifacts) ? body.artifacts : [];
  // Task 021 (#82) applies `renderApproachHistory`'s own lesson here: `load()`
  // rebuilds every sub-panel, this one included, every REFRESH_MS, so an
  // unconditional rebuild would silently re-collapse the `<details>` below
  // the moment the operator opened it. Skip the rebuild entirely when the
  // artifact set is unchanged from the last render -- the open/closed state
  // (and any focus inside it) then survives untouched across polls.
  const wanted = items.map((a) => String(a.path || a.key || "")).join("\u0000") +
    "|" + String(body.notice || "");
  if (box.dataset.artifacts === wanted) return;
  box.dataset.artifacts = wanted;
  box.innerHTML = "";
  const list = h("div", { class: "artifact-list", id: "artifact-list" }, []);
  for (const a of items) {
    const path = String(a.path || "");
    const key = String(a.key || "");
    const size = String(a.sizeDisplay || "");
    // A <button>, like a document row: keyboard reachability and Enter/Space
    // come from the platform. A binary artifact stays clickable -- the server
    // answers with its own "copy it out with pull" wording, which is a better
    // answer than an unexplained dead row.
    list.appendChild(h("button", {
      type: "button",
      class: "artifact-item",
      "data-artifact": path,
      "data-artifact-key": key,
      title: String(a.title || ""),
      onclick: () => { openArtifactDialog(runId, a); },
    }, [
      key ? h("span", { class: "artifact-key" }, [key]) : null,
      h("span", { class: "artifact-path" }, [path]),
      h("span", { class: "muted artifact-size" }, [" " + size]),
    ]));
  }
  // Collapsed by default, same native `<details>`/`<summary>` chrome as the
  // approach-history panel (no hand-rolled toggle) -- a run with a long
  // artifact tree used to push the whole page down every time. The summary
  // carries the COUNT so collapsing it never hides whether the run left
  // anything behind; each row stays a real `<button>`, unaffected by the
  // wrapper, once the panel is expanded.
  const details = h("details", { class: "artifact-list-details" }, [
    h("summary", {}, ["artifacts (" + String(items.length) + ")"]),
    list,
  ]);
  box.appendChild(details);
  if (body.notice) {
    // Wording comes from the server (`state.NO_ARTIFACTS`, the same line
    // `ralphctl artifacts ls` prints), like the document panel's notice.
    // Kept outside the collapsed `<details>` so an empty run's notice is
    // visible without expanding anything.
    box.appendChild(h("p", { class: "muted", id: "artifacts-notice" },
                      [String(body.notice)]));
  }
}

async function loadArtifacts(runId) {
  const box = document.getElementById("artifacts-box");
  const { ok, body } = await getJSON(
    `/api/runs/${encodeURIComponent(runId)}/artifacts`);
  if (!box) return;
  renderArtifacts(box, ok, body, runId);
}

// ------------------------------------------------------ router calls (#80)

// Task 022 (P, #80): `engine.loop.LoopSupervisor._ask_router` is the ONE
// model call this engine deliberately never charges as a work iteration --
// no `iterations_used` bump, no `iterations/NNNN/` directory -- which used
// to also mean it had NO surface at all: an operator could not tell "the
// router ran and picked X" from "the router never ran" without reading a
// raw NDJSON transcript by hand. `GET /api/runs/<id>/router` (ui_server.py's
// `router_calls_view`) reports `state.router_call_entries`, read from
// `artifacts/router/decisions.jsonl` -- the engine's own write, one line per
// COMPLETED decision. Every entry already carries `chargesIteration: false`
// and `note` (the "never a work iteration" sentence, worded once in
// Python) -- this panel shows both, so nobody has to trust a caption alone.

const ROUTER_CALLS_LOAD_FAILED = "failed to load the run's router calls";

function renderRouterCalls(box, ok, body) {
  if (!ok || !body) {
    box.innerHTML = "";
    delete box.dataset.routerCalls;
    box.appendChild(h("p", { class: "muted" }, [ROUTER_CALLS_LOAD_FAILED]));
    return;
  }
  const items = Array.isArray(body.routerCalls) ? body.routerCalls : [];
  // Same rebuild-skip discipline as `renderArtifacts`/`renderApproachHistory`
  // (task 021/020): `load()` reruns this every REFRESH_MS, so an
  // unconditional rebuild would re-collapse an operator's open `<details>`.
  const wanted = items.map((c) => String(c.index) + ":" + String(c.state) +
    ":" + String(c.chosenTarget)).join("\u0000") + "|" + String(body.notice || "");
  if (box.dataset.routerCalls === wanted) return;
  box.dataset.routerCalls = wanted;
  box.innerHTML = "";
  const list = h("div", { class: "router-call-list", id: "router-call-list" }, []);
  for (const c of items) {
    // #289: a task_selection entry lists task ids (`taskCandidateIds`) and has
    // no guard; an edge-based one lists `target(guard)` pairs.
    const isTask = Array.isArray(c.taskCandidateIds);
    const candidates = isTask
      ? c.taskCandidateIds.join(", ")
      : (Array.isArray(c.candidateEdges)
        ? c.candidateEdges.map((e) => `${e.target}(${e.guard})`).join(", ")
        : "");
    list.appendChild(h("div", { class: "router-call-item" }, [
      h("span", { class: "router-call-index" }, ["#" + String(c.index)]),
      h("span", { class: "router-call-state" }, [String(c.state || "")]),
      h("span", { class: "router-call-arrow" }, [" \u2192 "]),
      h("span", { class: "router-call-chosen" },
        [String(c.chosenTarget || "") +
          (isTask ? "" : " (guard=" + String(c.chosenGuard || "") + ")")]),
      c.usedFallback ? h("span", { class: "router-call-fallback" }, [" FELL BACK"]) : null,
      h("span", { class: "muted router-call-candidates" }, [" candidates: " + candidates]),
      isTask && c.deterministicPick != null
        ? h("span", { class: "muted router-call-deterministic" },
            [" deterministic: " + String(c.deterministicPick)])
        : null,
      isTask && c.reason
        ? h("span", { class: "muted router-call-reason" },
            [" reason: " + String(c.reason)])
        : null,
      isTask && c.usedFallback && c.fallbackCause
        ? h("span", { class: "muted router-call-fallback-cause" },
            [" cause: " + String(c.fallbackCause)])
        : null,
      h("span", { class: "muted router-call-model" },
        [" model: " + String(c.model || "(unset)")]),
      h("span", { class: "muted router-call-cost" },
        [" " + String(c.tokensDisplay || "") + " " + String(c.costDisplay || "")]),
    ]));
  }
  // Collapsed by default, same native `<details>`/`<summary>` chrome as the
  // artifacts panel (task 021) -- no hand-rolled toggle -- with the COUNT in
  // the summary and the explicit "never charged" sentence right below it, so
  // collapsing this panel never hides whether the router ran, or the fact
  // that running is free.
  const details = h("details", { class: "router-call-list-details" }, [
    h("summary", {}, ["router calls (" + String(items.length) + ")"]),
    h("p", { class: "muted" }, [
      "a router call consumes no iteration and no approach; it is never " +
      "presented as a work iteration and needs no iterations/NNNN/ directory",
    ]),
    list,
  ]);
  box.appendChild(details);
  if (body.notice) {
    // Wording from the server (`state.NO_ROUTER_CALLS`), like every other
    // on-disk panel's empty-state notice.
    box.appendChild(h("p", { class: "muted", id: "router-calls-notice" },
                      [String(body.notice)]));
  }
}

async function loadRouterCalls(runId) {
  const box = document.getElementById("router-calls-box");
  const { ok, body } = await getJSON(
    `/api/runs/${encodeURIComponent(runId)}/router`);
  if (!box) return;
  renderRouterCalls(box, ok, body);
}

// ------------------------------------------------- fault explanation (#18.4)

// Task 026 (#18.4): a degraded card said the run was sitting out an infra
// outage and a failed card said it failed, and neither said WHY: which row of
// `engine/faults.py`' signature table fired, how far up the retry ladder the
// run has climbed, how much of the outage budget is already spent. Reading
// that meant knowing the signature table by heart, grepping `events.jsonl`
// and doing the budget arithmetic by hand.
//
// The badge on the card is now the way in: it opens `GET
// /api/runs/<id>/fault` (ui_server.py, on-disk only -- so this works for a
// run whose container is long gone) in THE single `openTextDialog`, showing
// the server's own `text`, i.e. exactly the block `ralphctl fault <run>`
// prints (`state.fault_summary_lines`). Nothing here words a fact: even "no
// fault recorded" is the server's line, so a badge is never a lie about
// having something to say.

const FAULT_LOAD_FAILED = "failed to load the run's fault explanation";

// What the badge promises when hovered -- the four facts #18.4 asked for.
const FAULT_BADGE_TITLE = "explain this fault: classification, matched " +
  "signature, retry-ladder position, outage budget spent";

function faultTitle(runId) {
  return "Fault \u2014 " + String(runId);
}

async function openFaultDialog(runId) {
  const { ok, body } = await getJSON(
    `/api/runs/${encodeURIComponent(runId)}/fault`);
  const text = (ok && body && typeof body.text === "string" && body.text)
    ? body.text
    : FAULT_LOAD_FAILED;
  return openTextDialog(faultTitle(runId), text, null);
}

// A real <button> (keyboard reachability and Enter/Space come from the
// platform, like the document/artifact rows), styled as the badge it wraps.
// `kind` says which badge it is (`state` on a failed/aborted run's state
// pill, `infra-wait` on a degraded card) so a test -- and an operator reading
// the DOM -- can tell the two entry points apart.
//
// No run id (a payload without `runId`) means no endpoint to ask: the badge is
// then not rendered at all rather than being a button that can only fail --
// `h()` skips a null child, and `stateCell` falls back to the bare pill.
function faultBadge(runId, kind, child) {
  if (!runId) return null;
  return h("button", {
    type: "button",
    class: "fault-badge",
    "data-fault-badge": kind,
    title: FAULT_BADGE_TITLE,
    onclick: () => { openFaultDialog(runId); },
  }, [child]);
}

// ---------------------------------------------------- cost breakdown (#18.5)

// Task 028 (#18.5): the usage card showed ONE number -- what the run spent in
// total -- and the per-phase/per-approach tables under it showed raw token
// counts with no word on how much of that money is actually known. "Which
// phase burned the tokens, and is this figure quoted, derived or unavailable?"
// meant leaving the hub for `ralphctl cost` or reading status.json by hand.
//
// The cost cell is now the way in: it opens `GET /api/runs/<id>/cost`
// (ui_server.py, on-disk only -- so this works for a run whose container is
// long gone) in THE single `openTextDialog`, showing the server's own `text`,
// i.e. exactly the block `ralphctl cost <run>` prints
// (`state.cost_breakdown_lines`). Nothing here words a fact: the per-bucket
// labels, the legend, the zero-quote anomaly notice and even "no usage
// recorded" are the server's strings, so the dialog cannot label money
// differently from the CLI -- and the cell's own headline stays `costDisplay`,
// the string the card already showed, so opening the breakdown can never
// contradict the number that was clicked.

const COST_LOAD_FAILED = "failed to load the run's cost breakdown";

// What the cell promises when hovered -- what #18.5 asked the dialog for.
const COST_CELL_TITLE = "explain this cost: per-phase and per-approach usage, " +
  "and whether each figure was quoted, derived or is unavailable";

function costTitle(runId) {
  return "Cost \u2014 " + String(runId);
}

async function openCostDialog(runId) {
  const { ok, body } = await getJSON(
    `/api/runs/${encodeURIComponent(runId)}/cost`);
  const text = (ok && body && typeof body.text === "string" && body.text)
    ? body.text
    : COST_LOAD_FAILED;
  return openTextDialog(costTitle(runId), text, null);
}

// A real <button> around the card's cost value (keyboard reachability and
// Enter/Space come from the platform, like the fault badge and the document
// rows), styled to keep the stat card's own look.
//
// No run id means no endpoint to ask: the cell is then left as the plain value
// it always was rather than becoming a button that can only fail (the
// `faultBadge` rule).
function costCell(runId, child) {
  if (!runId) return null;
  return h("button", {
    type: "button",
    class: "cost-cell",
    "data-cost-cell": "total",
    title: COST_CELL_TITLE,
    onclick: () => { openCostDialog(runId); },
  }, [child]);
}

// --------------------------------------------------- steering history (#17)

// Task 017 (#17): steering used to be write-only in the hub -- an operator
// could post a message through the form below and then had no way to see what
// was queued, what the loop had already applied, or what the text said. The
// panel lists every entry `GET /api/runs/<id>/steering` reports (ui_server.py:
// live-first with an on-disk fallback, so a dead run's history is still
// readable) and opens each body in THE single `openTextDialog` -- text nodes
// only, because a steering message is operator prose from outside this page's
// trust boundary, exactly like the PRD.

const STEERING_NO_BODY = "(empty message)";
// A file the live engine named but the hub cannot see on disk (task 016 keeps
// no body for it rather than inventing one).
const STEERING_BODY_UNAVAILABLE =
  "(no body available — the run's steering file is not on this host)";

function steeringTitle(entry) {
  const file = String(entry.file || "");
  const name = String(entry.name || "");
  return "Steering " + file + (name && name !== file ? " — " + name : "");
}

function openSteeringDialog(entry, live) {
  const hasBody = typeof entry.body === "string";
  const text = hasBody
    ? (entry.body.trim() ? entry.body : STEERING_NO_BODY)
    : STEERING_BODY_UNAVAILABLE;
  const note = "state: " + String(entry.state || "unknown") +
    (entry.tsLocal ? "   arrived " + String(entry.tsLocal) : "") +
    (live ? "" : "   (on-disk snapshot — the run's API is not reachable)");
  return openTextDialog(steeringTitle(entry), text, note);
}

function renderSteering(box, ok, body) {
  box.innerHTML = "";
  if (!ok || !body) {
    box.appendChild(h("p", { class: "muted" }, ["failed to load steering history"]));
    return;
  }
  const entries = Array.isArray(body.entries) ? body.entries : [];
  const live = body.live === true;
  if (entries.length === 0) {
    // Wording comes from the server (`ui_server.NO_STEERING`), like the log
    // tail's `NO_TRANSCRIPT` and the PRD's `NO_PRD`.
    box.appendChild(h("p", { class: "muted", id: "steering-notice" },
                      [String(body.notice || "")]));
    return;
  }
  const list = h("div", { class: "steering-list", id: "steering-list" }, []);
  for (const e of entries) {
    const open = () => { openSteeringDialog(e, live); };
    list.appendChild(h("div", {
      class: "steering-item",
      "data-steering-file": String(e.file || ""),
      "data-steering-state": String(e.state || ""),
      role: "button",
      tabindex: "0",
      title: "show this steering message",
      onclick: open,
      onkeydown: (ev) => {
        if (ev.key === "Enter" || ev.key === " ") { ev.preventDefault(); open(); }
      },
    }, [
      // pending vs applied is the fact the panel exists to state: an entry
      // the loop has not read yet is still steerable, one it applied is
      // history.
      pill(e.state),
      h("span", { class: "steering-name" }, [String(e.name || e.file || "")]),
      // Absolute local time formatted server-side (`tsLocal`, by the same
      // `engine.state.format_local_time` `ralphctl status` uses); an entry
      // whose file the hub cannot see has no arrival time and claims none.
      h("span", { class: "steering-at muted" }, [String(e.tsLocal || "")]),
      h("span", { class: "steering-file muted" }, [String(e.file || "")]),
    ]));
  }
  box.appendChild(list);
  if (!live) {
    box.appendChild(h("p", { class: "muted steering-snapshot" }, [
      "(on-disk snapshot — the run's API is not reachable)",
    ]));
  }
}

async function loadSteering(runId) {
  const box = document.getElementById("steering-box");
  const { ok, body } = await getJSON(`/api/runs/${encodeURIComponent(runId)}/steering`);
  if (!box) return;
  renderSteering(box, ok, body);
}

// -------------------------------------------------------------- routing

function router() {
  const hash = location.hash || "#/";
  const m = hash.match(/^#\/run\/([^/]+)$/);
  // Mint a fresh generation for this navigation FIRST, synchronously, so
  // that even a render already in flight (awaiting its own initial fetch)
  // can tell -- via its captured `myGen` -- that it has been superseded the
  // moment this function returns, well before its `await` resolves.
  renderGeneration += 1;
  const myGen = renderGeneration;
  stopRefreshLoop(refreshTimer);
  refreshTimer = null;
  // A view superseded while the tab was hidden must not resume into a page
  // nobody navigated to any more (task 005, A.3).
  pendingLoadFn = null;
  if (m) {
    renderRunDetail(decodeURIComponent(m[1]), myGen);
  } else {
    renderRunList(myGen);
  }
  return myGen;
}

window.addEventListener("hashchange", router);
window.addEventListener("DOMContentLoaded", () => {
  setInterval(() => {
    document.getElementById("clock").textContent = new Date().toLocaleTimeString();
  }, 1000);
  loadHubVersion();
  router();
});

// Task 033 (G.1): the hub's own package version, fetched once from `GET
// /api/version` (ui_server.py -- `ralphd.__version__`, never a literal
// baked into this file) and shown in the page header, independent of
// which view the router renders -- the header sits outside `#app`, so
// this runs exactly once per page load, same as the clock above.
async function loadHubVersion() {
  const { ok, body } = await getJSON("/api/version");
  if (ok && body && body.version) {
    document.getElementById("hub-version").textContent = `v${body.version}`;
  }
}

// ---------------------------------------------------------- run list view

async function renderRunList(myGen) {
  const app = document.getElementById("app");
  app.innerHTML = "";
  app.appendChild(h("h2", {}, ["Runs"]));
  const tableWrap = h("div", { id: "run-list-wrap" }, [h("p", { class: "muted" }, ["loading…"])]);
  app.appendChild(tableWrap);

  // Task 016 (#27): a second tag, independent of `renderGeneration` --
  // covers a slow poll tick's response arriving after a *later* tick of
  // the SAME render (sort-click-triggered reloads included), which the
  // navigation-level generation alone would not catch.
  let loadSeq = 0;

  async function load() {
    // Task 016 (#27): a render superseded by a later navigation (`router()`
    // bumped `renderGeneration` already) must neither apply a response nor
    // -- second check -- one already in flight when it lost the race.
    if (myGen !== renderGeneration) return;
    const seq = ++loadSeq;
    const { ok, body } = await getJSON("/api/runs");
    if (myGen !== renderGeneration || seq !== loadSeq) return;
    tableWrap.innerHTML = "";
    if (!ok) {
      tableWrap.appendChild(h("p", { class: "muted" }, ["failed to load run list"]));
      return;
    }
    const runs = body.runs || [];
    if (runs.length === 0) {
      tableWrap.appendChild(h("p", { class: "muted" }, ["(no runs)"]));
      return;
    }
    const table = h("table", { class: "run-list" }, [
      h("thead", {}, [h("tr", {}, [...RUN_COLUMNS.map(col => h("th", {
        class: "sortable" + (runSort.key === col.key ? " sorted" : ""),
        "data-sort-key": col.key,
        "aria-sort": runSort.key !== col.key ? "none"
          : (runSort.dir < 0 ? "descending" : "ascending"),
        onclick: () => { toggleRunSort(col.key); load(); },
      }, [col.label + sortIndicator(col.key)])),
        // Task 031 (#19): the delete affordance's column. Deliberately NOT a
        // `RUN_COLUMNS` entry: it renders an action, not a value, so there is
        // nothing to sort on and nothing for `ralphctl runs --sort` to mirror.
        h("th", { class: "actions-col" }, [""]),
      ])]),
    ]);
    const tbody = h("tbody", {});
    for (const r of sortRuns(runs)) {
      // Task 024 (#8): a run recorded non-terminal whose API no longer
      // answers (`containerGone`, ui_server.py) must not look exactly like a
      // healthy running one -- it is over, it just never got to say so.
      const gone = r.containerGone === true;
      tbody.appendChild(h("tr", { class: gone ? "row-warning" : "" }, [
        h("td", {}, [h("a", { href: "#/run/" + encodeURIComponent(r.runId) }, [r.runId])]),
        h("td", {}, runRowStateChildren(r)),
        h("td", {}, [pill(r.verdict)]),
        h("td", {}, [String(r.phase || "")]),
        h("td", {}, [approachText(r)]),
        taskCell(r),
        h("td", {}, [`${r.iterationsUsed ?? 0}/${r.iterationsBudget ?? "?"}`]),
        h("td", { class: "muted" }, [String(r.startedAt || "")]),
        // Task 031 (#19): delete this run, for a terminal run only, behind
        // the confirmation. `load()` afterwards, so the row leaves the table
        // as soon as it is really gone rather than at the next 4s poll.
        h("td", { class: "actions-col" }, [deleteControl(r.runId, r, () => { load(); })]),
      ]));
    }
    table.appendChild(tbody);
    tableWrap.appendChild(table);
  }

  await load();
  // Task 016 (#27): only install the loop if this render is still the one
  // `router()` most recently asked for -- otherwise a render that lost the
  // race installs a timer nothing will ever clear (the `stopRefreshLoop`
  // call already happened, in the *next* `router()` call, before this one
  // got here).
  if (myGen === renderGeneration) refreshTimer = scheduleRefresh(load, REFRESH_MS);
}

// -------------------------------------------------------- run detail view

async function renderRunDetail(runId, myGen) {
  const app = document.getElementById("app");
  app.innerHTML = "";
  // Task 008 (v0.26, #269): the parked-question panel, installed FIRST so
  // it sits at the very top of the page -- content-managed entirely by
  // `renderParkedPanel` inside `load()` below (empty/hidden until a
  // `parkedQuestion` payload arrives), never rebuilt on every poll tick the
  // way the rest of this function's one-shot scaffolding below is.
  const parkedBox = h("div", { id: "parked-panel-box", style: "display:none" }, []);
  app.appendChild(parkedBox);
  app.appendChild(h("p", {}, [h("a", { href: "#/" }, ["← runs"])]));
  app.appendChild(h("h2", {}, ["Run " + runId]));
  // Task 056 (#1): the run's PRD, one click away from its detail page.
  app.appendChild(h("p", { class: "detail-actions" }, [
    h("button", {
      type: "button", class: "open-prd", id: "open-prd",
      onclick: () => { openPrdDialog(runId); },
    }, ["view PRD"]),
    // Task 031 (#19): ...and deleting the run, once it is over. Filled in by
    // `load()` below, because whether it may be offered is a fact about the
    // run's recorded state that only the payload knows (`deletable`).
    h("span", { id: "delete-box" }, []),
  ]));

  const summary = h("div", { class: "card" }, [h("p", { class: "muted" }, ["loading…"])]);
  // Task 022 (#18.2): the run's own prose -- notes, review findings, the
  // composite PRD and the redacted job.yaml -- one click away.
  const docSec = h("section", {}, [
    h("h2", {}, ["State documents"]),
    h("div", { id: "documents-box" }, [h("p", { class: "muted" }, ["(loading…)"])]),
  ]);
  // Task 024 (#18.3): what the job left behind -- the reflect report and the
  // diff it proposes above all -- one click away too.
  const artifactSec = h("section", {}, [
    h("h2", {}, ["Artifacts"]),
    h("div", { id: "artifacts-box" }, [h("p", { class: "muted" }, ["(loading…)"])]),
  ]);
  // Task 022 (P, #80): the router's own decisions -- normally none, since
  // no shipped state has ever reached a genuine multi-edge choice -- with
  // the explicit "never a work iteration" note carried on the panel itself.
  const routerSec = h("section", {}, [
    h("h2", {}, ["Router calls"]),
    h("div", { id: "router-calls-box" }, [h("p", { class: "muted" }, ["(loading…)"])]),
  ]);
  const usageSec = h("section", {}, [h("h2", {}, ["Usage / cost"]), h("div", { id: "usage-box" })]);
  const taskSec = h("section", {}, [h("h2", {}, ["Tasks"]), h("div", { id: "task-box" })]);
  // Task 021 (#24): every PREVIOUS approach `_archive_approach` froze into
  // `approaches/NN/`, most recent last, collapsed until clicked -- the
  // CURRENT approach's own task table/notes/findings are already visible in
  // the sections above, so this panel would only duplicate them; hidden
  // entirely (not even an empty placeholder) for a run still on its first
  // approach, per SPEC.md's "Approach history panel" bullet.
  const approachSec = h("section", { id: "approach-history-sec", style: "display:none" }, [
    h("h2", {}, ["Approach history"]),
    h("div", { id: "approach-box" }, []),
  ]);
  const timelineSec = h("section", {}, [h("h2", {}, ["Iteration timeline"]), h("div", { id: "timeline-box" })]);
  // #288: what the in-flight iteration is, side by side with its
  // log on a wide screen (`.live-row`), stacked on a narrow one.
  const currentSec = h("section", { class: "current-iteration-sec" }, [
    h("h2", {}, ["Current iteration"]),
    h("div", { id: "current-iteration-box" }, [h("p", { class: "muted" }, ["(loading…)"])]),
  ]);
  const logSec = h("section", { class: "log-sec" }, [
    h("h2", {}, ["Live log tail"]),
    h("div", { id: "logbox" }, ["(loading…)"]),
  ]);
  const steerSec = h("section", {}, [
    h("h2", {}, ["Steer"]),
    h("form", { class: "steer", id: "steer-form" }, [
      h("input", { type: "text", id: "steer-name", placeholder: "name (optional)", size: "10" }),
      // v0.26 task 010 (PRD requirement 4, #270): a resizable `textarea`
      // (about 8 rows), not the old one-line `<input type="text">` -- the
      // steer message is operator prose that can run to several
      // paragraphs, and `Enter` inserting a newline (a textarea's native
      // behaviour, unlike an `<input>` where it submits the form) matters
      // more than a single-line message ever did. The submit button is
      // still the only way to send.
      h("textarea", { id: "steer-message", placeholder: "steering message", required: "required", rows: "8" }),
      // #161: `apply now` is `ralphctl steer --now` -- labelled with what it
      // costs, so the checkbox is never ticked as if it were free. cure-01-05
      // (adopted #270 C.12): the label also discloses the OTHER cost #161
      // already carries, with its real conditions. A FIRST interrupt of a
      // running plan-review iteration is re-asked in place with no revision
      // spent (`test_steer_now_against_a_running_plan_review_iteration_
      // keeps_todays_161_cost`); only a SECOND interrupt in a row over a
      // fresh, not-yet-worked plan spends a revision and re-runs `planning`,
      // which re-ingests every task `pending` off the fresh proposal
      // (`test_steer_now_twice_against_a_running_plan_review_still_spends_a_
      // revision`). Disclosure only -- this changes no interrupt or
      // rejection semantics.
      h("label", { class: "steer-now-label", title: "steer --now: SIGINTs the running iteration; its uncommitted work is lost; a first interrupt of a running plan review is re-asked in place for free, but a second interrupt in a row over a fresh, unworked plan spends a revision and re-ingests every task pending" }, [
        h("input", { type: "checkbox", id: "steer-now" }),
        "apply now",
        h("span", { class: "muted steer-now-cost" }, [
          " (cost: interrupts the running iteration -- its uncommitted work is lost; an idle loop is only woken; " +
          "a running plan review is re-asked in place for free the first time, but a second interrupt in a row over a fresh, unworked plan spends a revision and re-ingests every task pending)",
        ]),
      ]),
      h("button", { type: "submit" }, ["send"]),
    ]),
    h("div", { id: "steer-status", class: "muted" }, []),
    // Task 017 (#17): the write surface above is no longer the whole story --
    // what was queued and what the loop already applied is listed here.
    h("h3", { class: "muted" }, ["Steering history"]),
    h("div", { id: "steering-box" }, [h("p", { class: "muted" }, ["(loading…)"])]),
  ]);
  // Task 013 (v0.23 requirement A.2, #224): the operator answer channel's
  // own section -- deliberately a SEPARATE form/route from Steer above (a
  // ruling answers a specific rejection and reaches both judging hats;
  // review discounts steering by design), landing through
  // `POST /api/runs/<id>/rule` into its own on-disk record.
  const ruleSec = h("section", {}, [
    h("h2", {}, ["Rule"]),
    h("p", { class: "muted" }, [
      "An operator ruling on a rejection the engine is about to spend an " +
      "approach on -- reaches both review and verify, keyed to the " +
      "finding signature (never a task id), so it survives a replan.",
    ]),
    h("form", { class: "rule", id: "rule-form" }, [
      h("input", { type: "text", id: "rule-name", placeholder: "name (optional)", size: "10" }),
      // v0.26 task 010 (PRD requirement 4, #270): 12 rows, up from 4 --
      // still resizable, so a longer ruling never has to fight a fixed box.
      h("textarea", { id: "rule-text", placeholder: "ruling text", required: "required", rows: "12" }),
      h("button", { type: "submit" }, ["submit ruling"]),
    ]),
    h("div", { id: "rule-status", class: "muted" }, []),
  ]);

  app.appendChild(summary);
  app.appendChild(docSec);
  app.appendChild(artifactSec);
  app.appendChild(routerSec);
  app.appendChild(usageSec);
  app.appendChild(taskSec);
  app.appendChild(approachSec);
  app.appendChild(timelineSec);
  app.appendChild(h("div", { class: "live-row" }, [currentSec, logSec]));
  app.appendChild(steerSec);
  app.appendChild(ruleSec);
  app.appendChild(h("footer", { class: "autorefresh" }, [`auto-refreshing every ${REFRESH_MS / 1000}s`]));

  document.getElementById("steer-form").addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const msg = document.getElementById("steer-message").value;
    const name = document.getElementById("steer-name").value;
    const now = document.getElementById("steer-now").checked;
    const statusEl = document.getElementById("steer-status");
    statusEl.textContent = "sending…";
    const body = { message: msg };
    if (name) body.name = name;
    if (now) body.now = true;
    try {
      const resp = await fetch(hubURL(`/api/runs/${encodeURIComponent(runId)}/steer`), {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      const j = await resp.json().catch(() => ({}));
      if (resp.ok) {
        if (now) {
          // Task 001 (#277): the response is the engine's own `POST
          // /interrupt` body, passed through unchanged by the hub proxy
          // -- `target` is whichever of the two shapes it returned.
          const wording = await loadInterruptWording();
          statusEl.textContent = wording[j.target] || wording.idle || "sent";
        } else {
          statusEl.textContent = "sent (" + (j.file || "ok") + ")";
        }
        document.getElementById("steer-message").value = "";
        // Task 017 (#17): show it in the history immediately rather than
        // making the operator wait out the 4s poll to see their own message.
        loadSteering(runId);
      } else {
        // #155/G.7 (task 069): `innermostReason` walks the nested `detail`
        // chain instead of stopping at the first truthy (possibly object)
        // value, so both control surfaces show the SAME reason for the SAME
        // cause and neither can render the literal `[object Object]`.
        const reason = innermostReason(j, resp.status);
        statusEl.textContent = "failed: " + String(reason);
      }
    } catch (e) {
      statusEl.textContent = "failed: " + e;
    }
  });

  // Task 013 (v0.23 requirement A.2, #224): the operator ruling textarea's
  // submit handler -- same shape as steer's above, deliberately posting to
  // its own route (`/rule`, never `/steer`) so a ruling is never discounted
  // the way steering is by review's design.
  document.getElementById("rule-form").addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const text = document.getElementById("rule-text").value;
    const name = document.getElementById("rule-name").value;
    const statusEl = document.getElementById("rule-status");
    statusEl.textContent = "sending…";
    const body = { text: text };
    if (name) body.name = name;
    try {
      const resp = await fetch(hubURL(`/api/runs/${encodeURIComponent(runId)}/rule`), {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      const j = await resp.json().catch(() => ({}));
      if (resp.ok) {
        statusEl.textContent = "recorded (" + (j.file || "ok") + ")";
        document.getElementById("rule-text").value = "";
      } else {
        const reason = innermostReason(j, resp.status);
        statusEl.textContent = "failed: " + String(reason);
      }
    } catch (e) {
      statusEl.textContent = "failed: " + e;
    }
  });

  // Task 016 (#27): see the matching comment in `renderRunList` -- a second
  // tag, independent of `renderGeneration`, catching a slow poll tick's
  // response arriving after a later tick of this same view.
  let loadSeq = 0;

  // Task 005 (A.3): the client half of the ETag/`If-None-Match` round trip
  // (server half: task 003, `run_detail_etag`/`_send_not_modified`) plus
  // the back-off it exists to drive. `lastEtag` is the tag from the most
  // recent `200`, sent back as `If-None-Match` on every later poll of THIS
  // view; `unchangedStreak` counts consecutive `304`s (the server's own
  // word that nothing in `status.json`/`tasks.json`/`events.jsonl`/the
  // newest iteration `meta.json` moved); `pollIntervalMs` is this view's
  // current scheduling interval, widened to `BACKOFF_POLL_MS` once either
  // back-off condition holds and snapped back to `REFRESH_MS` the moment
  // something genuinely changes again.
  let lastEtag = null;
  let unchangedStreak = 0;
  let lastKnownState;
  let pollIntervalMs = REFRESH_MS;
  // cure-01-01-5 (adopted #270 D.14): the LAST `parkedQuestion` this view
  // actually rendered, as JSON, so an unrelated refresh (a fresh, unrelated
  // event appended to `events.jsonl` moves the run-detail `ETag` even
  // while the SAME question stays parked -- SPEC.md §11.3) never rebuilds
  // `renderParkedPanel`'s box and wipes the operator's in-progress draft
  // name/ruling text. A genuinely DIFFERENT question (a new escalation, or
  // the pause getting answered/resumed to `null`) always differs here and
  // rebuilds -- fresh findings need a fresh, empty answer box.
  let lastParkedQuestionJSON;
  // #288: the last `currentIteration` panel payload, re-rendered on
  // EVERY tick (a `304` included) so its elapsed time keeps moving while
  // the back-off holds the payload itself still.
  let lastCurrentIteration = null;
  // Test-only instrumentation (never read by the app itself): lets a
  // browser test observe the back-off without waiting out the full
  // 15-30s band it targets.
  window.__hubDetailPollIntervalMs = () => pollIntervalMs;

  async function load() {
    // Task 016 (#27): same discard-if-superseded rule as `renderRunList`'s
    // `load()` -- see the comment there. The detail view's `load()` also
    // touches many more sub-panels (logs, steering, documents, artifacts),
    // all of which are skipped for a superseded render by never running at
    // all rather than trying to guard each one individually.
    if (myGen !== renderGeneration) return;
    const seq = ++loadSeq;
    const headers = lastEtag ? { "If-None-Match": lastEtag } : undefined;
    const { ok, status, body, etag } =
      await getJSON(`/api/runs/${encodeURIComponent(runId)}`, headers);
    if (myGen !== renderGeneration || seq !== loadSeq) return;
    if (status === 304) {
      // Confirmed identical to what is already on screen -- nothing to
      // reparse or repaint for the main card/tasks/timeline, but the sub
      // panels below (steering, logs, documents, artifacts, router calls,
      // approaches) each have their own change surface the ETag does not
      // cover, so they still poll every tick regardless of back-off state.
      unchangedStreak += 1;
    } else if (!ok) {
      summary.innerHTML = "";
      summary.appendChild(h("p", { class: "muted" }, [
        status === 404 ? "run not found" : "failed to load run detail",
      ]));
      return;
    } else {
      lastEtag = etag || lastEtag;
      unchangedStreak = 0;
      lastKnownState = body.status && body.status.state;
      const pqJSON = JSON.stringify(body.parkedQuestion || null);
      if (pqJSON !== lastParkedQuestionJSON) {
        lastParkedQuestionJSON = pqJSON;
        renderParkedPanel(document.getElementById("parked-panel-box"), runId,
                          body.parkedQuestion);
      }
      renderSummary(summary, body);
      renderDelete(document.getElementById("delete-box"), runId, body);
      renderUsage(document.getElementById("usage-box"),
                  body.status && body.status.usage, runId);
      renderTasks(document.getElementById("task-box"), body.tasks,
                  body.status && body.status.progressBudget);
      renderTimeline(document.getElementById("timeline-box"), body.iterations || [], runId);
      lastCurrentIteration = body.currentIteration || null;
    }
    renderCurrentIteration(document.getElementById("current-iteration-box"),
                           lastCurrentIteration, runId);
    // Task 049 (H.7): `loadLogs` (the transcript/log-tail poll,
    // `GET /api/runs/<id>/logs?tail=200`) already shares this SAME
    // `load()` call, which is itself scheduled through `scheduleRefresh`
    // below at `pollIntervalMs` -- so it already inherits both the
    // back-off widening this function computes at its own tail and the
    // `document.hidden` pause `scheduleRefresh`/the `visibilitychange`
    // listener enforce for every caller. No separate timer was ever wired
    // for it, so there was nothing to add here -- proven at the network
    // layer by `tests/test_browser_hub_backoff.py::
    // test_a_hidden_tab_issues_no_log_tail_request`, beside
    // `test_run_detail_backs_off_after_two_identical_polls_of_a_running_run`
    // for the back-off half.
    await loadLogs(runId);
    await loadSteering(runId);
    await loadDocuments(runId);
    await loadArtifacts(runId);
    await loadRouterCalls(runId);
    await loadApproaches(runId);

    // Task 005 (A.3): back off once the run looks quiescent -- two
    // identical payloads in a row (`304`, twice) or a run that is no
    // longer `running` -- into the fixed point this app picked inside the
    // PRD's 15-30s band; snap back to the normal 4s cadence the instant
    // something actually changes. Only REINSTALLS the timer when the
    // target interval actually differs AND a timer is already running:
    // the very first call `renderRunDetail` makes (`await load()`, before
    // its own install line further down has ever run) must only ever
    // update `pollIntervalMs` for that line to pick up -- installing a
    // timer here too would double the loop that line goes on to install
    // right after (the exact race `test_browser_hub_refresh_loop.py`
    // pins against for the pre-back-off code).
    const backOff = unchangedStreak >= 2 || lastKnownState !== "running";
    const nextIntervalMs = backOff ? BACKOFF_POLL_MS : REFRESH_MS;
    const intervalChanged = nextIntervalMs !== pollIntervalMs;
    pollIntervalMs = nextIntervalMs;
    if (myGen === renderGeneration && refreshTimer != null && intervalChanged) {
      stopRefreshLoop(refreshTimer);
      refreshTimer = scheduleRefresh(load, pollIntervalMs);
    }
  }

  await load();
  if (myGen === renderGeneration) refreshTimer = scheduleRefresh(load, pollIntervalMs);
}

// Task 008 (v0.26, #269): human labels for `parkedQuestion.reason` -- the
// two `pauseReason` values a ruling can actually answer (task 007's own
// `PARKED_QUESTION_REASONS`). An if/else, deliberately never an object
// literal keyed by the reason string -- an object key spelled from
// `"rejected_repeated_signature"` would land the word naming what
// repeated right against the object literal's own colon, and
// `test_hub_fault_dialog.py::test_app_js_words_no_fault_fact_of_its_own`
// forbids that exact adjacency ANYWHERE in the bundle (the fault dialog's
// own field label is worded server-side, never respelled here) -- an
// unrelated collision this function avoids by construction rather than by
// carving an exception into that guard.
function parkedReasonText(reason) {
  if (reason === "operator_input_needed") return "operator input needed";
  if (reason === "rejected_repeated_signature") return "rejected \u2014 repeated rejection signature";
  return String(reason || "unknown");
}

// Task 008 (v0.26, #269): the plain-language reading of task 007's
// `consequence` object -- what happens if this pause is resumed with no
// ruling at all.
// Task 009 (v0.26, #269): the plain-language reading of a single
// `nextStepIfUnanswered` value, factored out of `parkedConsequenceLines` so
// the "Resume without a ruling" button below can label itself with the
// EXACT same predicted outcome the consequence list already renders,
// never a second, independently-worded copy of the same fact.
// Cure task cure-01-05 (PRD v0.27 requirement C.4, DoD 3): a third value,
// `"finishing"` -- this approach is archived but the run ENDS instead of
// replanning, because no approach budget is left (`replanAvailable: false`
// on the SAME `parkedQuestion`).
function parkedNextStepOutcomeText(next) {
  return next === "cure_pass"
    ? "a cure pass over this approach's blocking findings"
    : next === "archive_and_replan"
      ? "this approach is archived and the run replans"
      : next === "finishing"
        ? "this approach is archived and the run ends (no approach left)"
        : "unknown (" + String(next) + ")";
}

function parkedConsequenceLines(c) {
  const nextText = parkedNextStepOutcomeText(c.nextStepIfUnanswered);
  const reopened = c.taskIdsValidatedToPending || [];
  const cost = c.costSpentThisApproach;
  const tokens = c.tokensSpentThisApproach;
  return [
    "if left unanswered: " + nextText,
    "tasks reopened to pending: " + (reopened.length ? reopened.join(", ") : "none"),
    "tasks inserted: " + String(c.insertCount ?? 0),
    "spent this approach: " + (cost != null ? String(cost) : "unknown") +
      (tokens != null ? " (" + String(tokens) + " tokens)" : ""),
  ];
}

// Task 008 (v0.26, #269): one finding row -- id/severity/clause up front (the
// escalation text's own vocabulary, `F<n>` blocking then `R<n>` residual),
// expandable to its full evidence/`cured_when`/`invalidates`. The evidence
// block scrolls (CSS `max-height`/`overflow`, see `.parked-finding-evidence`
// in style.css) rather than truncating -- SPEC.md 11.3/task 007 both promise
// the WHOLE evidence, never a clipped preview.
// Task 009 (v0.26, #269): the exact ruling-text line a finding's waive
// checkbox composes -- one line, `id` + `clause`, byte-stable so
// `applyWaiveLine` below can find and remove that SAME line again on
// uncheck without touching anything else the operator typed into the
// ruling textarea by hand, or any other finding's own waived line.
function parkedWaiveLineText(f) {
  return "waive " + f.id + " (" + (f.clause || "no clause") + ")";
}

// Task 009 (v0.26, #269): a pure text transform -- checking a waive box
// appends `line` to `text` (unless already present) as its own line, after
// a newline if `text` does not already end in one; unchecking removes only
// that ONE line (and the newline ending it). Every other byte the operator
// typed -- blank lines, paragraphs, leading/trailing whitespace -- is left
// exactly as it was. No DOM, no request: this is the whole of a waive
// checkbox's effect, and the piece `tests/test_hub_parked_actions.py`
// exercises directly so the compose-then-submit rule is pinned independent
// of any event wiring.
//
// cure-01-01-5: an earlier fix guessed whether a trailing single "\n"
// was check()'s OWN inserted separator or the operator's own real byte by
// inspecting only the resulting string AFTER the fact -- genuinely
// ambiguous once other composed/edited text sits nearby.
//
// cure-01-01-6: two bugs remained. (1) the last-line-only removal branch
// fell back to a bare `indexOf(line + "\n")` for a line that is NOT the
// last line -- a plain substring search with no requirement that it start
// at a line boundary, so a waive line's own text embedded inside an
// unrelated operator sentence ("Do not waive F1 (clause one (#111))\n")
// matched and got spliced out of THAT prose instead of the real composed
// line further down. (2) the separator-tracking bit (`state.inserted`)
// was recorded only at the instant the composed region grew from zero
// active lines to one, so a hand edit appended directly to the textarea
// between two checks (leaving it unterminated again) with one composed
// line already active never got its own leading separator recorded --
// checking a SECOND finding right after such an edit silently ran its
// composed line into the operator's own last word.
//
// The fix removes the ambiguity at its root instead of guessing around
// it: `state` (an object OWNED by the caller, mutated in place --
// `renderParkedPanel` below hands it one shared instance across every
// toggle of a single parked question) records, PER COMPOSED LINE, the
// one fact that matters -- whether THAT line's own insertion had to add
// a leading separator, decided at the moment it was inserted (text ended
// without "\n" at that instant) rather than inferred later from shape.
// Removal always locates the line by a boundary-anchored search ("\n" or
// start-of-string before it, "\n" after it), so a coincidental substring
// elsewhere in hand-typed prose is never touched, and only strips the
// synthetic leading separator when this line's own record says it added
// one -- never a byte the operator (or another composed line) put there.
//
// cure-01-01-7: checking TWO findings then unchecking the earlier one
// (F1 checked, then F2, then F1 unchecked) still lost a byte -- F1's own
// record correctly said ITS insertion added a leading separator, and the
// old code always stripped that separator whenever it removed F1, even
// though F2's own composed line still sits right after it and still
// needs that exact separator to stay off `before`. The fix never strips
// a still-needed separator: when the line being removed owned one AND
// another composed line remains immediately after it (no gap), that
// remaining line inherits the debt (`state[other] = true`) instead --
// so ITS OWN later removal, once nothing follows it at all, strips the
// byte this removal correctly left alone. Only a removal that leaves
// nothing following ever strips a separator, which is exactly what
// restores the original bytes once every composed waiver is gone.
function applyWaiveLine(text, line, checked, composedLines, sepState) {
  const src = String(text || "");
  const state = sepState || {};
  const boundaryIndex = (s, l) => {
    if (s === l || s.startsWith(l + "\n")) return 0;
    const at = s.indexOf("\n" + l + "\n");
    return at < 0 ? -1 : at + 1;
  };
  const already = boundaryIndex(src, line) >= 0;
  if (checked) {
    if (already) return src;
    const addSep = src.length > 0 && !src.endsWith("\n");
    state[line] = addSep;
    return src + (addSep ? "\n" : "") + line + "\n";
  }
  if (!already) return src;
  const idx = boundaryIndex(src, line);
  const before = src.slice(0, idx);
  const after = src.slice(idx + line.length + 1);
  const insertedSep = !!state[line];
  delete state[line];
  if (!insertedSep) return before + after;
  if (after.length === 0) {
    return before.endsWith("\n") ? before.slice(0, -1) + after : before + after;
  }
  for (const other of Object.keys(state)) {
    if (after === other || after.startsWith(other + "\n")) {
      state[other] = true;
      break;
    }
  }
  return before + after;
}

// Task 009 (v0.26, #269): `onWaive(f, checked)` is called on toggle, never
// invoked here -- the checkbox's ONLY effect is composing editable ruling
// text (via `onWaive`, wired by `renderParkedPanel` below to
// `applyWaiveLine`/`parkedWaiveLineText`); it sends no request of its own,
// so checking it never waives anything by itself -- only a later
// `Submit ruling & resume` click does that.
function parkedFindingItem(f, onWaive) {
  const invalidates = f.invalidates || [];
  const waiveId = "waive-" + f.id;
  return h("details", { class: "parked-finding" }, [
    h("summary", {}, [
      `${f.id} \u00b7 ${f.severity} \u00b7 ${f.clause || "(no clause)"}`,
    ]),
    h("div", { class: "parked-finding-body" }, [
      h("div", { class: "parked-finding-row" }, [
        h("label", { class: "parked-waive-label", for: waiveId }, [
          h("input", {
            type: "checkbox", id: waiveId, class: "waive-checkbox",
            onchange: (ev) => {
              if (onWaive) onWaive(f, !!(ev.target && ev.target.checked));
            },
          }, []),
          " waive " + f.id,
        ]),
      ]),
      h("div", { class: "parked-finding-row" }, [
        h("b", {}, ["evidence: "]),
        h("pre", { class: "parked-finding-evidence" }, [String(f.evidence ?? "")]),
      ]),
      h("div", { class: "parked-finding-row" }, [
        h("b", {}, ["cured_when: "]),
        String(f.cured_when ?? "(none)"),
      ]),
      h("div", { class: "parked-finding-row" }, [
        h("b", {}, ["invalidates: "]),
        invalidates.length ? invalidates.join(", ") : "(none)",
      ]),
    ]),
  ]);
}

// Task 008 (v0.26, #269): the parked-question panel -- a full-width card
// rendered from task 007's `parkedQuestion` payload into a persistent `box`
// `renderRunDetail` installs once at the TOP of the page (ahead of the
// Steer/Rule sections, which stay their own, separate sections below it --
// criterion 2). Absent/`null` `pq` (a running run, or a pause reason no
// ruling answers) empties the box and hides it -- the panel is simply not
// rendered, never a lingering stale one. Every finding is listed (blocking
// AND residual, task 007's own `F<n>`/`R<n>` order), never only the
// blocking subset.
//
// cure-01-05 (adopted #270 C.12/D.14): the panel now carries its OWN
// visible, editable answer box -- `#parked-rule-name` and
// `#parked-rule-text` -- right alongside its findings, so an operator
// answering THIS pause never has to scroll past it down to the separate
// Rule section to find a name field or a textarea at all (a live browser
// probe found zero of either inside `#parked-panel`, and a chosen
// `#rule-name` silently dropped on parked submit). The ordinary Steer and
// Rule sections further down the page are untouched and stay their own,
// independent sections -- this is a SECOND, panel-own box, not a move of
// the existing one.
function renderParkedPanel(box, runId, pq) {
  if (!box) return;
  box.innerHTML = "";
  if (!pq) {
    box.style.display = "none";
    return;
  }
  box.style.display = "";
  const c = pq.consequence || {};
  const approachOfMax = pq.approachOfMax ||
    (pq.approach != null ? "approach " + pq.approach : "unknown");
  const parkedFor = pq.parkedAt
    ? fmtDuration(durationBetween(pq.parkedAt, new Date().toISOString())) + " ago"
    : "unknown";
  const rows = [
    ["reason", parkedReasonText(pq.reason)],
    ["approach", approachOfMax],
    ["parked", parkedFor + (pq.parkedAt ? ` (${pq.parkedAt})` : "")],
    ["replan available", pq.replanAvailable == null ? "n/a" : (pq.replanAvailable ? "yes" : "no")],
    ["blocking findings", String(pq.blockingCount ?? 0)],
    ["residual findings", String(pq.residualCount ?? 0)],
  ];
  // cure-01-05 (adopted #270 D.14): the waive checkbox composes into the
  // PANEL'S OWN `#parked-rule-text` -- never `#rule-text`, the SEPARATE
  // Rule section's own control further down the page -- so the operator
  // never has to leave the panel to see, or edit, the ruling a waive
  // built.
  // One `sepState` instance shared across every waive toggle for THIS
  // rendering of the panel (cure-01-01-5, refined cure-01-01-6) --
  // `applyWaiveLine` mutates it in place, keyed by each composed line's
  // OWN text, to remember whether THAT line's own insertion had to add a
  // leading separator newline, decided once at insertion time rather
  // than guessed back out of the text's shape on removal.
  const sepState = {};
  const onWaive = (f, checked) => {
    const ta = document.getElementById("parked-rule-text");
    if (!ta) return;
    ta.value = applyWaiveLine(ta.value, parkedWaiveLineText(f), checked, null, sepState);
  };
  const outcomeText = parkedNextStepOutcomeText(c.nextStepIfUnanswered);
  const actionStatus = h("div", { id: "parked-action-status", class: "muted" }, []);
  // cure-01-05 (adopted #270 D.14): the panel's own answer box -- a name
  // field and a resizable 12-row ruling textarea, matching the Rule
  // section's own box in shape (task 010) but living INSIDE the panel,
  // right alongside the findings a waive checkbox reads from. `Enter`
  // inserts a newline (a `<textarea>`'s native behaviour) and never
  // submits; only the button below does.
  const nameInput = h("input", {
    type: "text", id: "parked-rule-name", placeholder: "name (optional)", size: "10",
  });
  const rulingTextarea = h("textarea", {
    id: "parked-rule-text", placeholder: "ruling text", rows: "12",
  });
  // Task 009 (v0.26, #269): the two stated-effect actions -- each shows
  // its own effect in visible text BEFORE it is clicked, never only in a
  // hover title (#161's own rule for `steer --now`). `Submit ruling &
  // resume` posts the textarea body UNCHANGED to `POST /rule` -- the SAME
  // route the Rule section's own form already posts to, and the SAME
  // route that resumes the run itself when the ruling answers the
  // current pause (`LoopSupervisor.resume_if_ruling_answers_pause`) --
  // never a second, separate `/unpause` call from here. `Resume without a
  // ruling` instead posts straight to `POST /unpause` (task 006) with no
  // ruling recorded at all, so it is labelled with the SAME predicted
  // outcome (`nextStepIfUnanswered`) the consequence list above already
  // shows -- an operator taking this action is deliberately choosing to
  // let that outcome happen now rather than review a waived finding first.
  const submitRulingBtn = h("button", {
    type: "button", id: "parked-submit-ruling",
    onclick: async () => {
      const ta = document.getElementById("parked-rule-text");
      const nameEl = document.getElementById("parked-rule-name");
      // The body is posted byte-for-byte as the textarea holds it (the
      // Rule form's own contract) -- never trimmed or reflowed. Only a
      // body with nothing but whitespace is refused, client-side, before
      // any request is sent. A name the operator typed into the panel's
      // own name field rides along exactly the way the Rule form's own
      // `#rule-name` does -- never silently dropped.
      const text = ta ? ta.value : "";
      if (!text.trim()) { actionStatus.textContent = "failed: ruling text required"; return; }
      const name = nameEl ? nameEl.value : "";
      const body = { text: text };
      if (name) body.name = name;
      actionStatus.textContent = "sending\u2026";
      try {
        const resp = await fetch(hubURL(`/api/runs/${encodeURIComponent(runId)}/rule`), {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body),
        });
        const j = await resp.json().catch(() => ({}));
        if (resp.ok) {
          actionStatus.textContent = "ruling recorded, run resumed if it answered this pause";
          if (ta) ta.value = "";
          if (nameEl) nameEl.value = "";
        } else {
          actionStatus.textContent = "failed: " + String(innermostReason(j, resp.status));
        }
      } catch (e) {
        actionStatus.textContent = "failed: " + e;
      }
    },
  }, ["Submit ruling & resume"]);
  const resumeNoRulingBtn = h("button", {
    type: "button", id: "parked-resume-no-ruling",
    onclick: async () => {
      actionStatus.textContent = "sending\u2026";
      try {
        const resp = await fetch(hubURL(`/api/runs/${encodeURIComponent(runId)}/unpause`), {
          method: "POST",
        });
        const j = await resp.json().catch(() => ({}));
        if (resp.ok) {
          actionStatus.textContent = "resumed with no ruling";
        } else {
          actionStatus.textContent = "failed: " + String(innermostReason(j, resp.status));
        }
      } catch (e) {
        actionStatus.textContent = "failed: " + e;
      }
    },
  }, ["Resume without a ruling"]);
  box.appendChild(h("div", { class: "card parked-panel-card", id: "parked-panel" }, [
    h("h2", {}, ["\u23F8 Parked for an operator answer"]),
    ...rows.map(([k, v]) => h("div", {}, [h("b", {}, [k + ": "]), v])),
    h("div", { class: "parked-consequence" }, [
      h("b", {}, ["consequence of not answering:"]),
      h("ul", {}, parkedConsequenceLines(c).map((line) => h("li", {}, [line]))),
    ]),
    // cure-01-01-5 (B.3-B.5, adopted #270 D.14): findings occupy the LEFT
    // column and the answer box plus its two actions occupy the RIGHT
    // column at desktop widths (`.parked-panel-columns`/style.css's own
    // `min-width: 900px` rule) -- a live browser probe at 1280px found
    // both stacked full-width instead, the answer below rather than
    // beside the findings. Narrower than that, `.parked-panel-columns`
    // stacks them exactly as before (findings, then the answer box, then
    // the actions), so nothing regresses on a narrow viewport. The
    // ordinary Steer/Rule sections further down the page are OUTSIDE this
    // card entirely and stay their own, separate full-width sections.
    h("div", { class: "parked-panel-columns" }, [
      h("div", { class: "parked-panel-left" }, [
        h("h3", {}, ["Findings"]),
        h("div", { class: "parked-findings" },
          (pq.findings || []).map((f) => parkedFindingItem(f, onWaive))),
      ]),
      h("div", { class: "parked-panel-right" }, [
        h("h3", {}, ["Answer"]),
        // cure-01-05 (adopted #270 D.14): the visible, editable answer box
        // lives HERE, inside the panel, alongside the findings to its left
        // and the two actions below it -- never only in the separate Rule
        // section further down the page.
        h("div", { class: "parked-answer" }, [nameInput, rulingTextarea]),
        h("div", { class: "parked-actions" }, [
          h("div", { class: "parked-action" }, [
            submitRulingBtn,
            h("p", { class: "muted parked-action-effect" }, [
              "effect: posts the ruling text above, unchanged, to this run's " +
              "own ruling route -- which resumes the run itself if the ruling " +
              "answers this pause.",
            ]),
          ]),
          h("div", { class: "parked-action" }, [
            resumeNoRulingBtn,
            h("p", { class: "muted parked-action-effect" }, [
              "effect: resumes with no ruling recorded -- predicted outcome: " +
              outcomeText,
            ]),
          ]),
          actionStatus,
        ]),
      ]),
    ]),
  ]));
}

// Task 031 (#19): the run-detail page's delete control -- the same affordance
// the run list's rows carry (`deleteControl`), reading the same server-decided
// `deletable`/`deleteRefusal` from the detail payload. On success there is no
// run left to show, so the hub returns to the list rather than leaving the
// operator on a page that is about to 404.
function renderDelete(box, runId, detail) {
  if (!box) return;
  box.innerHTML = "";
  box.appendChild(deleteControl(runId, detail, () => { location.hash = "#/"; }));
}

// Task 026 (#18.4): the states whose badge is a way IN to the fault
// explanation -- a run the engine stopped for a reason. A degraded *running*
// run gets its own badge on the infra-wait block instead (`renderInfraWait`),
// where the outage is already the subject.
const FAULT_BADGE_STATES = new Set(["failed", "aborted"]);

// The summary card's `state:` cell: the pill, wrapped in the fault badge when
// this run ended in a way that has an explanation to give (task 026). The
// pill's own look is untouched -- the badge only makes it clickable. Task
// 005 (PRD requirement A.5, #269): for every non-fault state, the SAME
// `pauseMarker`/`escalationUndeliveredMarker` spans the runs-list row
// already renders now ride alongside the pill here too (`s.pauseMarker`/
// `s.escalationUndeliveredMarker`, attached server-side by `ui_server.
// _with_pause_markers`) -- both empty strings for the ordinary case, so
// nothing changes for a run that is neither.
function stateCell(detail, s) {
  const pillEl = pill(s.state);
  if (FAULT_BADGE_STATES.has(String(s.state || ""))) {
    return faultBadge(detail.runId, "state", pillEl) || pillEl;
  }
  const spans = stateMarkerSpans(s.pauseMarker, s.escalationUndeliveredMarker, detail.runId);
  if (spans.length === 0) return pillEl;
  return h("span", {}, [pillEl, ...spans]);
}

function renderSummary(el, detail) {
  const s = detail.status || {};
  el.innerHTML = "";
  el.className = "card" + (s.state === "failed" ? " error" : "");
  // Task 015 (#56): the `finishing` window (task 012) settles `verdict`/
  // `endedAt` in status.json before the reflect iteration that still has
  // to run is over -- `currentIteration` staying a live dict is the tell.
  // While that is true, a colored verdict pill (green "verified", the same
  // look a genuinely terminal run gets) and an `ended` timestamp both read
  // as "this is over" beside a timeline row that is still showing
  // "running…" for that very iteration. Neither is hidden -- `verdict` is
  // deliberately readable the moment it settles (task 012) -- only their
  // TERMINAL-looking rendering is withheld until the window actually closes.
  const iterationInProgress = s.currentIteration != null &&
    typeof s.currentIteration === "object";
  const rows = [
    ["state", stateCell(detail, s)],
    ["verdict", iterationInProgress ? String(s.verdict ?? "unknown") : pill(s.verdict)],
    ["phase", String(s.phase || "")],
    ["approach", approachText(s)],
    ["iterations", `${s.iterationsUsed ?? 0}/${s.iterationsBudget ?? "?"}`],
    ["live", detail.live ? "yes (proxied from container)" : "no (on-disk snapshot)"],
  ];
  // Task 047 (#177): the per-iteration `model:` line CLI's `ralphctl
  // status` prints, worded identically here (rendered server-side too --
  // `s.model`/`s.modelRaw` travel unchanged from the SAME `GET /status`
  // payload `ralphctl status` reads), with this run's own `--llm-header`
  // map (`s.llmHeaders`, attached server-side by `ui_server._with_llm_
  // headers`, ref-derived values already masked) right under it -- one
  // row per header, never a second vocabulary from re-deriving anything
  // in JS. Gated on `s.model` exactly like the CLI's own line is (omitted
  // entirely, never "model: (unset)", before any iteration has observed
  // one); the header rows are NOT gated on it (known from
  // `llm-wiring.json` alone, same "known before iteration 1" property
  // `provisioning` below already has) -- each row's own `header: <name>`
  // label already says what it is, so an unmodelled run's headers (rare
  // -- `--llm-header` with `--llm none`, still a legal combination) still
  // render even with no `model` row above them.
  if (s.model) {
    let modelLine = String(s.model);
    if (s.modelRaw) modelLine += `  (gateway id: ${s.modelRaw})`;
    rows.push(["model", modelLine]);
  }
  const llmHeaders = s.llmHeaders || {};
  for (const [hname, hvalue] of Object.entries(llmHeaders)) {
    rows.push([`header: ${hname}`, String(hvalue)]);
  }
  // Task 035 (G.3): the engine version this run is executing (F.4's
  // `status.json` field, always present) -- and, whenever the workspace's
  // own version is knowable at all (`workspaceVersion`, persisted for every
  // run whose workspace names a version, drift or no drift, by
  // `loop._record_workspace_version`), both versions on the same line, so
  // the drift E.3/task 017 would otherwise only page the operator about is
  // readable here at a glance -- and so is a matching pair.
  if (s.engineVersion) {
    let engineLine = String(s.engineVersion);
    if (s.workspaceVersion) {
      engineLine += `  (workspace: ${s.workspaceVersion})`;
    }
    rows.push(["engine", engineLine]);
  }
  const started = s.startedAt;
  const ended = iterationInProgress ? null : s.endedAt;
  let durText = "";
  if (started && ended) {
    durText = "total " + fmtDuration(durationBetween(started, ended));
  } else if (started) {
    durText = "elapsed " + fmtDuration(durationBetween(started, new Date().toISOString()));
  }
  if (durText) rows.push(["duration", durText]);
  // Task 048 (#4): absolute local-time instants alongside the relative
  // duration. Formatted server-side by the ONE shared Python formatter
  // (`engine/state.format_local_time`, delivered as `startedAtLocal` /
  // `endedAtLocal` by `ui_server.run_detail`) rather than a second
  // JS implementation, so the hub and `ralphctl status` can never drift.
  if (s.startedAtLocal) rows.push(["started", String(s.startedAtLocal)]);
  if (!iterationInProgress && s.endedAtLocal) rows.push(["ended", String(s.endedAtLocal)]);
  const p = h("p", {}, []);
  for (const [k, v] of rows) {
    const line = h("div", {}, [h("b", {}, [k + ": "]), typeof v === "string" ? v : v]);
    p.appendChild(line);
  }
  el.appendChild(p);
  // Task 010 (#79, PRD requirement O): the resolved hat -> model mapping,
  // one row per hat, worded server-side by the ONE shared Python formatter
  // (`engine/state.format_provisioning_lines`, delivered as
  // `provisioningLines` by `ui_server.run_detail`'s `_with_provisioning` --
  // the exact strings `ralphctl status`'s own `provisioning:` block
  // prints, so the hub cannot describe the mapping differently). A native
  // `<details>` disclosure, never a hand-rolled one, and present the
  // instant a run is created -- it comes from job.yaml alone, not from
  // anything an iteration observed, so it is not gated on
  // `iterationInProgress`/`s.model` the way the per-iteration model line
  // elsewhere on this page is.
  const provLines = s.provisioningLines || [];
  if (provLines.length > 0) {
    // Task 040 (I.2): collapsed by default, like the iteration history
    // above. Task 041 (I.3): a stored "provisioning" preference (scoped
    // per section type, not per job, same mechanism as the timeline
    // above) overrides that default once the operator has toggled this
    // section on any run.
    const provPref = readCollapsePref("provisioning");
    const provOpen = provPref === "open";
    const provAttrs = { class: "provisioning" };
    if (provOpen) provAttrs.open = "";
    const provDetails = h("details", provAttrs, [
      h("summary", {}, ["provisioning (resolved hat \u2192 model)"]),
      h("div", { class: "provisioning-rows" },
        provLines.map((line) => h("div", {}, [String(line)]))),
    ]);
    wireCollapsePersistence(provDetails, "provisioning");
    el.appendChild(provDetails);
  }
  // Task 001a criterion 4: while an infra-fault retry is backing off,
  // currentIteration.note carries a human-readable "retrying after infra
  // fault (attempt N/max, next in Xs): <error>" message -- surface it on
  // the run-detail card so a hub operator sees the same thing plain
  // `ralphctl status` shows, not just the raw --json blob.
  const curIt = s.currentIteration;
  if (curIt && curIt.note) {
    el.appendChild(h("p", { class: "infra-retry-note" }, [
      "\u23F3 " + String(curIt.note),
    ]));
  }
  renderInfraWait(el, s, detail.runId, detail.live !== false);
  renderRateLimitWait(el, s, detail.runId, detail.live !== false);
  renderContainerGone(el, detail);
  renderReflect(el, s);
  renderProvisionalVerdict(el, s);
  // Task 004: the engine writes a high-quality `reason` into status.json
  // on terminal failed/aborted states (e.g. the no-progress fail-fast
  // explanation) -- surface it prominently on the run-detail card rather
  // than leaving the operator to fetch --json to learn why.
  const terminalFailed = s.state === "failed" || s.state === "aborted";
  if (terminalFailed && s.reason) {
    el.classList.add("error");
    el.appendChild(h("p", { class: "run-reason" }, [
      "reason: " + String(s.reason),
    ]));
  }
  // Task 006: a terminal run that still has unconsumed steering files is a
  // silent-drop hazard -- a terminal run never reads pending steering
  // again, so this is the only remaining place the operator can notice.
  const unconsumed = s.unconsumedSteering || [];
  if (unconsumed.length > 0) {
    el.classList.add("error");
    el.appendChild(h("p", { class: "steering-warning" }, [
      "⚠ UNCONSUMED STEERING (run ended without acting on): " + unconsumed.join(", "),
    ]));
  }
}

// Task 024 (#8): a run whose status.json still records a non-terminal state
// while its API no longer answers (`containerGone`, computed server-side in
// ui_server.py from the SAME `NONTERMINAL_STATES` set `ralphctl
// status`/`doctor`/`repair` use) is dead: the engine was killed before it
// could write a terminal state. Rendered with the card's existing warning
// treatment (`.card.warning`, sharing one CSS rule with `.card.degraded`) so
// it cannot be confused with a healthy `state: running` card -- previously
// the only hint was the `live: no (on-disk snapshot)` row, which reads the
// same for a finished run that is unreachable by design.
//
// Wording mirrors `ralphctl status`'s `container: … appears gone` lines
// (main.py's `_format_container_gone_lines`), including pointing at
// `ralphctl repair` for the authoritative docker-side diagnosis -- the hub
// only knows the API stopped answering. textContent only (via `h()`).
function renderContainerGone(el, detail) {
  if (detail.containerGone !== true) return;
  const s = detail.status || {};
  el.classList.add("warning");
  el.appendChild(h("div", { class: "container-gone" }, [
    h("div", {}, [
      "\u26A0 container appears gone: the run's API is unreachable while " +
      "status.json still records state " + JSON.stringify(String(s.state)),
    ]),
    h("div", {}, [
      "this run stopped without recording a terminal state \u2014 diagnose " +
      "with `ralphctl repair " + String(detail.runId || "") + "`",
    ]),
  ]));
}

// Task 020 (#5): a *failed* post-terminal reflection deliberately leaves the
// run's state/verdict/reason untouched (the job is already over when reflect
// runs, see docs/api.md's `reflect`), so without this line the hub renders a
// run that lost its post-mortem exactly like one that never asked for it.
// Same wording `ralphctl status` prints (main.py's `_format_reflect_lines`).
// textContent only (via `h()`'s text nodes) -- never innerHTML.
function renderReflect(el, s) {
  const reflect = (s.reflect && typeof s.reflect === "object") ? s.reflect : null;
  // Nothing for a successful reflection (its report.md is the signal) and
  // nothing for `reflect: null` (disabled, or the phase has not ended).
  if (reflect == null || reflect.ok !== false) return;
  const error = String(reflect.error == null ? "" : reflect.error).trim() ||
    "reason not recorded";
  el.appendChild(h("p", { class: "reflect-failed" }, [
    "reflection: failed (" + error + ")",
  ]));
}

// Task 026 (PRD requirement H.2/H.3, #264): a run that concluded on
// review's early-verdict checkpoint (task 025) instead of a fully refined
// pass carries an explicit `provisionalVerdict` object on status.json --
// read directly here, never derived by comparing `recordedAt` against
// `endedAt` by hand. Nothing at all unless `provisional` is true; the same
// wording `ralphctl status`'s own `provisional:` line prints (main.py's
// `_format_provisional_verdict_lines`). textContent only (via `h()`'s text
// nodes) -- never innerHTML.
function renderProvisionalVerdict(el, s) {
  const pv = (s.provisionalVerdict && typeof s.provisionalVerdict === "object")
    ? s.provisionalVerdict : null;
  if (pv == null || pv.provisional !== true) return;
  const scope = pv.refinementScope || "further refinement";
  el.appendChild(h("p", { class: "provisional-verdict" }, [
    "\u26A0 PROVISIONAL verdict (" + String(pv.verdict) + ", recorded " +
    String(pv.recordedAt) + "): review never got to " + String(scope) + ".",
  ]));
}

// Task 014 (#5): a run sitting out an infra outage must NOT render
// identically to a healthy running run -- `state` deliberately stays
// "running" (see docs/api.md: there is no `degraded` state value), so the
// only signal is `health`/`infraWait` and the hub has to show it or the
// operator sees a run that merely looks stuck. Same information
// `ralphctl status`'s `degraded:` line prints (main.py's
// `_format_degraded_lines`): attempt, phase, countdown to the next
// attempt, episode wait against the outage budget, and the error.
// textContent only (via `h()`'s text nodes) -- never innerHTML.
function renderInfraWait(el, s, runId, live) {
  const wait = (s.infraWait && typeof s.infraWait === "object") ? s.infraWait : null;
  // `infraWait` is populated only while a backoff wait is actually pending;
  // between two attempts it is back to null while `health` stays
  // "degraded" (the episode is not over until an iteration reaches the
  // model). Both shapes get the degraded treatment.
  const degraded = wait != null || (s.health || "ok") === "degraded";
  if (!degraded) return;
  el.classList.add("degraded");
  if (wait == null) {
    // Task 050 (requirement J.1, #136/#134): a run that TERMINATED on the
    // repeated-ingestion-signature path (engine/loop.py's two-strike
    // ingestion-termination branch) also carries `health: degraded`, set
    // once at replan time and never cleared -- but it is not "between
    // retry attempts"; it is over. `state` in TERMINAL_STATES (mirrored
    // here from engine/state.py: "succeeded"/"failed"/"aborted" -- a
    // genuine infra-outage episode between waits is always
    // running/starting/finishing, never terminal) is the distinguishing
    // signal, and `s.reason` is the terminal note `_finish` already wrote
    // (the same field the terminal-reason `reason:` line below quotes for
    // a terminal failed/aborted run) -- quoted here instead of the
    // generic outage sentence that would otherwise misreport a finished
    // run as still stuck in an episode.
    const TERMINAL_STATES = ["succeeded", "failed", "aborted"];
    if (TERMINAL_STATES.indexOf(s.state) !== -1) {
      const reason = String(s.reason == null ? "" : s.reason).trim();
      el.appendChild(h("p", { class: "infra-wait" }, [
        "\u26A0 degraded: " + (reason || "run ended degraded (no reason recorded)"),
        // Task 026 (#18.4) again: a run the outage KILLED still has an
        // outage to explain, so this way in keeps its badge -- task 050's
        // first cut of this branch dropped it and turned the browser tier
        // red on tests/test_browser_hub.py::
        // test_run_detail_opens_the_fault_dialog_from_the_badge.
        faultBadge(runId, "infra-wait", " explain"),
      ]));
      return;
    }
    // Cure-01-05 (v0.24 requirement B.1, DoD 3, #169): `timed_out` is the
    // deadline/budget HOLD (task 008) -- resume-eligible, nothing in
    // flight, no retry attempt actually running and no backoff wait
    // pending. It is not in TERMINAL_STATES above (a hold is not an
    // outcome) but it must not fall into the generic "a retry attempt is
    // running" sentence below either -- that is exactly the
    // still-working misreading B.1 forbids. `s.reason` is the hold's own
    // note (`_finish_timed_out`, docs/api.md), quoted the same way the
    // terminal branch above quotes its own reason.
    if (s.state === "timed_out") {
      const reason = String(s.reason == null ? "" : s.reason).trim();
      el.appendChild(h("p", { class: "infra-wait" }, [
        "\u26A0 degraded: held at the deadline " +
        (reason ? "(" + reason + ")" : "(no reason recorded)"),
        faultBadge(runId, "infra-wait", " explain"),
      ]));
      return;
    }
    el.appendChild(h("p", { class: "infra-wait" }, [
      "\u26A0 degraded: infra outage episode in progress " +
      "(a retry attempt is running, no backoff wait pending)",
      // Task 026 (#18.4): ...and the badge that says WHICH outage.
      faultBadge(runId, "infra-wait", " explain"),
    ]));
    return;
  }
  const box = h("div", { class: "infra-wait" }, [
    h("div", {}, [
      "\u26A0 degraded: infra outage — attempt " + String(wait.attempt ?? "?") +
      " (phase " + String(wait.phase || "?") + ")",
      faultBadge(runId, "infra-wait", " explain"),
    ]),
    h("div", { class: "infra-wait-budget" }, [
      "waited " + fmtDuration(wait.waitedS) + " of " + fmtDuration(wait.budgetS) +
      " outage budget" +
      (wait.remainingS == null ? "" : " (" + fmtDuration(wait.remainingS) + " left)"),
    ]),
  ]);
  const error = String(wait.error == null ? "" : wait.error).trim();
  if (error) box.appendChild(h("div", { class: "infra-wait-error" }, ["error: " + error]));
  renderRetryNow(box, runId, live, wait);
  el.appendChild(box);
}

// v0.30 F.2: a run waiting for its rate-limit window to reset. Not an outage
// (`health` stays "ok"), so it is not the degraded card: one line, worded
// server-side (`rateLimitWaitLine`, from `state.format_rate_limit_wait_line`,
// the same words `ralphctl status` prints), plus the countdown to the reset
// and the same "retry now" button, which `POST /retry` honours for this wait
// too. textContent only.
function renderRateLimitWait(el, s, runId, live) {
  const wait = (s.rateLimitWait && typeof s.rateLimitWait === "object") ? s.rateLimitWait : null;
  if (wait == null) return;
  const line = String(s.rateLimitWaitLine ||
    ("waiting: rate limit (" + String(wait.type || "unknown type") + ") until " +
     String(wait.resetsAt || "?"))).replace(/\s+/g, " ");
  const box = h("div", { class: "rate-limit-wait" }, [h("div", {}, ["\u23F3 " + line])]);
  renderRetryNow(box, runId, live, { nextAttemptAt: wait.resetsAt },
                 "retrying now (rate-limit wait woken)");
  el.appendChild(box);
}

function nextAttemptText(iso) {
  return "next attempt " + countdownTo(iso);
}

// Task 017 (#5): a degraded card gets a live-ticking countdown to
// `nextAttemptAt` plus a "retry now" button that POSTs through the hub's
// own proxy route (`POST /api/runs/<id>/retry`, ui_server.py) to the run's
// `POST /retry` — the same thing `ralphctl retry <run-id>` does: wake the
// pending backoff wait immediately and reset the outage-budget episode
// clock (docs/api.md).
//
// The button is rendered ONLY while a backoff wait is actually pending
// (i.e. from inside `renderInfraWait`, never on a healthy card) AND only
// when the run's API is reachable: a dead run's card is a read-only
// on-disk snapshot, so a button whose proxy can only ever answer 503
// would be a lie. textContent only (via `h()`'s text nodes).
function renderRetryNow(box, runId, live, wait, okText) {
  const row = h("div", { class: "infra-retry-controls" }, []);
  const countdown = h("span", { class: "infra-countdown" }, [nextAttemptText(wait.nextAttemptAt)]);
  row.appendChild(countdown);
  // Tick once a second so the operator watches the wait drain instead of a
  // number that only moves on the 4s full-page rebuild. The interval stops
  // itself as soon as its element leaves the DOM -- every `load()` rebuilds
  // the card, so a per-refresh leaked interval would otherwise pile up.
  const timer = setInterval(() => {
    if (!countdown.isConnected) { clearInterval(timer); return; }
    countdown.textContent = nextAttemptText(wait.nextAttemptAt);
  }, 1000);
  if (!live || !runId) {
    row.appendChild(h("span", { class: "muted" }, [
      " — read-only on-disk snapshot: the run's API is unreachable, cannot retry now",
    ]));
    box.appendChild(row);
    return;
  }
  const statusEl = h("span", { class: "retry-now-status muted" }, []);
  const button = h("button", { class: "retry-now", type: "button" }, ["retry now"]);
  button.addEventListener("click", async () => {
    button.disabled = true;
    statusEl.textContent = " retrying…";
    try {
      const resp = await fetch(hubURL(`/api/runs/${encodeURIComponent(runId)}/retry`), {
        method: "POST",
        headers: { "Content-Type": "application/json" },
      });
      const j = await resp.json().catch(() => ({}));
      if (resp.ok) {
        statusEl.textContent = " " + (okText || "retrying now (outage budget clock reset)");
      } else {
        // #155/G.7 (task 069): shared with the steer form's error branch --
        // see `innermostReason`'s own comment above.
        statusEl.textContent = " failed: " + String(innermostReason(j, resp.status));
      }
    } catch (e) {
      statusEl.textContent = " failed: " + e;
    }
    button.disabled = false;
  });
  row.appendChild(button);
  row.appendChild(statusEl);
  box.appendChild(row);
}

function renderUsage(el, usage, runId) {
  el.innerHTML = "";
  if (!usage) {
    el.appendChild(h("p", { class: "muted" }, ["(no usage data)"]));
    return;
  }
  const grid = h("div", { class: "usage-grid" });
  const total = usage.totalTokens ?? usage.total_tokens;
  const cost = usage.costUSD ?? usage.cost_usd;
  if (total != null) grid.appendChild(statCard("total tokens", total));
  // Task 051 (#10): `costDisplay` is the string the ONE shared formatter
  // (`engine/state.format_cost`, applied server-side in ui_server, same
  // pattern as `startedAtLocal`) produced -- "unavailable" for tokens the
  // provider never priced, "$x+ (partial, rest unavailable)" for a mixed
  // bucket. Never re-derive it from costUSD here: that is how `$0.0000`
  // got shown for an unknown cost in the first place (issue #10).
  const costText = usage.costDisplay ?? (cost != null ? "$" + Number(cost).toFixed(4) : null);
  if (costText != null) {
    // Task 028 (#18.5): the headline stays exactly this string; the cell only
    // becomes clickable, opening the breakdown behind it.
    const card = statCard("cost", costText, (v) => costCell(runId, v));
    if (usage.costStatus) card.classList.add("cost-" + usage.costStatus);
    grid.appendChild(card);
  }
  // The subscription list price (`listEquivalentUSD`): shown apart from the
  // cost, never added to it. `listEquivalentDisplay` is rendered server-side.
  if (usage.listEquivalentDisplay && usage.listEquivalentUSD) {
    const listCard = statCard("list-equivalent", usage.listEquivalentDisplay);
    listCard.classList.add("list-equivalent");
    grid.appendChild(listCard);
  }
  el.appendChild(grid);
  for (const [label, key] of [["by phase", "byPhase"], ["by approach", "byApproach"], ["by tier", "byTier"]]) {
    const bucket = usage[key];
    if (!bucket || Object.keys(bucket).length === 0) continue;
    el.appendChild(h("h3", { class: "muted" }, [label]));
    // Task 017 (#68 half 2): the tier table gains an `iterations` column --
    // per-tier spend alone does not answer "how many iterations bought
    // that", and `iterations` is a plain counter `loop._accumulate_usage`
    // keeps only on `byTier` buckets (never on byPhase/byApproach).
    const cols = key === "byTier" ? ["tokens", "cost", "iterations"] : ["tokens", "cost"];
    const hasListEquiv = Object.values(bucket).some((v) => v && v.listEquivalentUSD);
    if (hasListEquiv) cols.push("list-equivalent");
    const table = h("table", {}, [h("thead", {}, [h("tr", {}, [
      h("th", {}, [key === "byPhase" ? "phase" : key === "byApproach" ? "approach" : "tier"]),
      ...cols.map((c) => h("th", {}, [c])),
    ])])]);
    const tbody = h("tbody", {});
    for (const [k, v] of Object.entries(bucket)) {
      const cells = [
        h("td", {}, [k]),
        h("td", {}, [String(v.totalTokens ?? "")]),
        h("td", {}, [v.costDisplay ?? (v.costUSD != null ? "$" + Number(v.costUSD).toFixed(4) : "")]),
      ];
      if (cols.includes("iterations")) cells.push(h("td", {}, [String(v.iterations ?? "")]));
      if (hasListEquiv) {
        cells.push(h("td", {}, [v.listEquivalentUSD ? String(v.listEquivalentDisplay ?? "") : ""]));
      }
      tbody.appendChild(h("tr", {}, cells));
    }
    table.appendChild(tbody);
    el.appendChild(table);
  }
}

// `wrap` (optional) may put the rendered value inside an affordance -- the
// cost cell's dialog button (task 028). It receives the value's text node and
// may return null, in which case the plain value is shown: an affordance that
// cannot work is simply not offered.
function statCard(label, value, wrap) {
  const text = document.createTextNode(String(value));
  const inner = wrap ? wrap(text) : null;
  return h("div", { class: "stat" }, [
    h("div", { class: "k" }, [label]),
    h("div", { class: "v" }, [inner || text]),
  ]);
}

// --------------------------------------------------- approach history (#24)

// Task 021 (#24): every PREVIOUS approach `_archive_approach` (`engine/loop.py`)
// froze into `approaches/NN/` before the loop restarted the plan, listed
// oldest first / most recent last (SPEC.md's "Approach history panel"
// bullet), each row collapsed until clicked. The CURRENT approach is not a
// row here at all -- its task table is already the "Tasks" section above and
// its notes.md/review-findings.md are already the "State documents" panel, so
// duplicating them here would only disagree with those sections the moment
// either one polled at a different tick. A run still on its first approach
// (nothing archived yet) hides the WHOLE section rather than showing an empty
// panel -- `approachSec.style.display` toggles, never a placeholder sentence.
//
// Expanding a row fetches `GET /api/runs/<id>/approaches/<n>` exactly once
// (cached on the row's own body element) and renders that approach's OWN
// task table with THE SAME `renderTasks` the live plan uses, plus its
// notes.md/review-findings.md as document buttons styled and behaved exactly
// like the state-documents panel's (`.document-item`/`.document-absent`) --
// the text is already inline in the fetched payload (task 020's
// `archived_approach_view`), so opening one costs no further request, unlike
// the live documents panel's per-click fetch.

const APPROACH_LOAD_FAILED = "failed to load the run's approach history";

// Same exists/absent affordance as `renderDocuments`' `document-item`s, but
// the click just opens the text already in hand (no per-click fetch) since
// an archived approach's notes/findings arrive inline with the row's data.
function approachDocItem(d, onOpen) {
  const label = String((d && (d.name || d.key)) || "");
  const size = String((d && d.sizeDisplay) || "");
  if (d && d.exists === true) {
    return h("button", {
      type: "button", class: "document-item",
      "data-document": String(d.key || ""),
      title: String(d.title || ""),
      onclick: onOpen,
    }, [label, h("span", { class: "muted document-size" }, [" " + size])]);
  }
  return h("span", {
    class: "document-item document-absent",
    "data-document": String((d && d.key) || ""),
    "data-document-absent": "1",
    title: String((d && d.title) || ""),
  }, [label, h("span", { class: "muted document-size" }, [" " + size])]);
}

async function expandArchivedApproach(bodyBox, runId, num) {
  // Loaded once per row; a second toggle-open just re-shows what is already
  // there rather than re-fetching a frozen file.
  if (bodyBox.dataset.loaded === "1") return;
  bodyBox.innerHTML = "";
  bodyBox.appendChild(h("p", { class: "muted" }, ["(loading…)"]));
  const { ok, body } = await getJSON(
    `/api/runs/${encodeURIComponent(runId)}/approaches/${encodeURIComponent(String(num))}`);
  bodyBox.innerHTML = "";
  if (!ok || !body) {
    bodyBox.appendChild(h("p", { class: "muted" }, [APPROACH_LOAD_FAILED]));
    return;
  }
  bodyBox.dataset.loaded = "1";
  const tableBox = h("div", { class: "approach-tasks" }, []);
  renderTasks(tableBox, body.tasks);
  bodyBox.appendChild(tableBox);
  const notesText = (body.notes && typeof body.notes.text === "string") ? body.notes.text : "";
  const findingsText = (body.findings && typeof body.findings.text === "string") ? body.findings.text : "";
  bodyBox.appendChild(h("div", { class: "document-list approach-doc-list" }, [
    approachDocItem(body.notes, () => {
      openTextDialog("notes.md — approach " + String(num) + " — " + runId, notesText, null);
    }),
    approachDocItem(body.findings, () => {
      openTextDialog("review-findings.md — approach " + String(num) + " — " + runId, findingsText, null);
    }),
  ]));
  if (body.notice) {
    bodyBox.appendChild(h("p", { class: "muted" }, [String(body.notice)]));
  }
}

function renderArchivedApproachEntry(runId, entry) {
  const num = entry.approach;
  const bodyBox = h("div", { class: "approach-body" }, []);
  const summaryText = "Approach " + String(num) +
    (entry.summary ? " — " + String(entry.summary) : "");
  const details = h("details", { class: "approach-entry", "data-approach": String(num) }, [
    h("summary", {}, [summaryText]),
    bodyBox,
  ]);
  // Lazily fetched on first expand, like `openDocumentDialog`'s per-click
  // fetch -- a collapsed row costs nothing beyond the one listing request.
  details.addEventListener("toggle", () => {
    if (details.open) expandArchivedApproach(bodyBox, runId, num);
  });
  return details;
}

function renderApproachHistory(box, sec, ok, listBody, runId) {
  if (!ok || !listBody) {
    // A failed load is shown, not silently swallowed -- same contract as every
    // other on-disk panel (documents/artifacts) on this page.
    sec.style.display = "";
    box.innerHTML = "";
    delete box.dataset.approaches;
    box.appendChild(h("p", { class: "muted" }, [APPROACH_LOAD_FAILED]));
    return;
  }
  const archived = Array.isArray(listBody.approaches) ? listBody.approaches : [];
  if (archived.length === 0) {
    // SPEC.md: "A run still on its first approach shows no history at all
    // rather than an empty panel."
    sec.style.display = "none";
    box.innerHTML = "";
    delete box.dataset.approaches;
    return;
  }
  sec.style.display = "";
  const sorted = [...archived].sort((a, b) => (a.approach ?? 0) - (b.approach ?? 0));
  // Task 016/017's own lesson applied here: an archived approach is FROZEN
  // the moment `_archive_approach` writes it, so the set of rows this poll
  // wants can only grow, never change -- once every row is already in the
  // DOM, leave the whole subtree alone rather than rebuilding it, or the
  // 4s refresh timer would silently re-collapse (and forget the fetched
  // body of) any row the operator has open.
  const wanted = sorted.map((e) => String(e.approach)).join(",");
  if (box.dataset.approaches === wanted) return;
  box.dataset.approaches = wanted;
  box.innerHTML = "";
  const list = h("div", { class: "approach-list", id: "approach-list" }, []);
  for (const entry of sorted) {
    list.appendChild(renderArchivedApproachEntry(runId, entry));
  }
  box.appendChild(list);
}

async function loadApproaches(runId) {
  const box = document.getElementById("approach-box");
  const sec = document.getElementById("approach-history-sec");
  if (!box || !sec) return;
  const { ok, body } = await getJSON(`/api/runs/${encodeURIComponent(runId)}/approaches`);
  if (!document.getElementById("approach-box") || !document.getElementById("approach-history-sec")) return;
  renderApproachHistory(box, sec, ok, body, runId);
}

// Task 014 (#156, requirement A.4): the in-progress task's own progress
// budget (status.json's `progressBudget`, task 013, `{taskId, used,
// budget}`) -- passed in separately from `doc` above because it lives on
// status.json, not the tasks payload (the hub's own
// `body.status.progressBudget`, unchanged from GET /status's own key, see
// `renderRunDetail`'s call site). Gated on the task's OWN status being
// `"in-progress"`, the same discipline `engine.state.format_task_status`
// uses server-side: the record persists after the task it named moves on,
// so showing it beside a finished (or a different) task would misdescribe
// both.
function taskProgressBudget(progressBudget, t) {
  if (!progressBudget || typeof progressBudget !== "object") return null;
  if (String(t && t.status) !== "in-progress") return null;
  const id = t && t.id == null ? "" : String(t.id);
  if (String(progressBudget.taskId) !== id) return null;
  return progressBudget;
}

function renderTasks(el, tasks, progressBudget) {
  el.innerHTML = "";
  const doc = tasks || {};
  const list = doc.tasks || [];
  // Task 005 (#15): a task list served from the last-good cache says so.
  // `tasksStale` (with `tasksLabel`/`tasksNotice`, the strings `ui_server`
  // rendered from the engine's own wording -- see `_with_tasks_read_label`)
  // means "tasks.json would not parse on this read; this is the last plan
  // that did". Without the label the table below would silently claim to be
  // the current plan, which is the whole defect issue #15 is about; with it,
  // an operator watching a poll that landed inside an agent's rewrite sees
  // stale-but-true data instead of a table that blinks to empty.
  // Text nodes only (`h` + pill), like every other payload the hub renders.
  if (doc.tasksStale === true) {
    el.appendChild(h("p", {
      id: "tasks-stale",
      class: "muted tasks-stale",
      "data-tasks-source": String(doc.tasksSource || ""),
    }, [
      pill(doc.tasksLabel || "stale"),
      " ",
      String(doc.tasksNotice || ""),
    ]));
  }
  if (list.length === 0) {
    // An empty list under a stale read is ignorance, not an empty plan
    // (`tasksSource: "unreadable"`), so the notice above stands alone
    // rather than being contradicted by a confident "(no tasks)".
    if (doc.tasksStale !== true) {
      el.appendChild(h("p", { class: "muted" }, ["(no tasks)"]));
    }
    return;
  }
  const table = h("table", {}, [h("thead", {}, [h("tr", {}, [
    h("th", {}, ["id"]), h("th", {}, ["status"]), h("th", {}, ["title"]),
    h("th", {}, ["time"]), h("th", {}, ["cost"]),
  ])])]);
  const tbody = h("tbody", {});
  for (const t of list) {
    // Task 057 (#2): the whole row is the affordance -- clickable, and
    // reachable from the keyboard (Enter/Space), since a <tr> is not
    // natively focusable.
    const open = () => { openTaskDialog(t, doc); };
    const kind = taskFailureKind(doc, t);
    const blockedBy = taskBlockedBy(doc, t);
    const steering = isSteeringOrigin(t);
    const workerOrigin = isWorkerOrigin(t);
    const budget = taskProgressBudget(progressBudget, t);
    tbody.appendChild(h("tr", {
      class: "task-row" + (blockedBy ? " task-row-blocked" : ""),
      "data-task-id": String(t.id == null ? "" : t.id),
      "data-failure-kind": String(kind || ""),
      "data-blocked": blockedBy ? blockedBy.join(",") : "",
      "data-task-origin": steering ? "steer" : (workerOrigin ? "worker" : ""),
      role: "button",
      tabindex: "0",
      title: "show this task's success criteria",
      onclick: open,
      onkeydown: (ev) => {
        if (ev.key === "Enter" || ev.key === " ") { ev.preventDefault(); open(); }
      },
    }, [
      h("td", {}, [String(t.id || "")]),
      // Task 025 (#33): a failed task's row says WHICH failure it is, in the
      // server's wording -- otherwise the hub shows the one word `failed` for
      // both meanings and the operator has to open tasks.json to tell a
      // requirement nobody met from a task the engine stopped validating.
      //
      // Task 021 (#60 pillar 3): a task the dependency graph dooms gets a
      // `blocked` pill alongside its OWN real status -- so a dead subtree
      // is visible in the row itself, not only inside the dialog.
      //
      // Task 011 (PRD requirement I, #98): a steering-origin task gets its
      // own pill too -- planner-proposed and steer-injected are otherwise
      // visually identical rows, which is exactly what this task exists to
      // stop.
      //
      // Task 021 (PRD requirement N, #102): the sibling pill for a task the
      // worker split OUT of its own assignment mid-run.
      h("td", {}, [
        pill(t.status),
        ...(kind ? [" ", pill(kind)] : []),
        ...(steering ? [" ", pill("steering-origin")] : []),
        ...(workerOrigin ? [" ", pill("worker-origin")] : []),
        ...(blockedBy ? [" ", pill("blocked by " + blockedBy.join(", "))] : []),
        ...(t.deferredUntil ? [" ", pill("deferred until " + String(t.deferredUntil))] : []),
        ...(budget ? [" ", pill("budget " + budget.used + "/" + budget.budget)] : []),
      ]),
      h("td", {}, [String(t.title || "")]),
      // Task 036 (G.4; re-scoped task 001/PRD I.1, #207): each task's own
      // aggregated time and cost -- `timeDisplay`/`costDisplay`, summed
      // server-side (`engine.state.task_totals`) from every iteration this
      // task's own `assignedTaskId` named WITHIN THE CURRENT APPROACH ONLY
      // (`_with_task_totals` passes the approach it is rendering and reads
      // back only that approach's own `"<approach>:<taskId>"` bucket),
      // INCLUDING a validation-failed re-entry's second attempt -- never
      // recomputed here, and never another approach's spend against a
      // same-numbered task (every approach's planning pass renumbers
      // `tasks.json` from "001", #207/#29). A task THIS approach never
      // dispatched reads a real zero -- `0s`/`$0.0000`, from
      // `state.task_totals_zero()` -- not a dash, because that figure is
      // known, not missing (PRD I.1: "a `pending` task in the current
      // approach shows zero"), even when an earlier approach's same-numbered
      // task has real spend. The em dash below is left for the one case the
      // server says nothing at all about: a pre-dispatch-record run dir
      // whose iterations carry no `assignedTaskId`, where neither key is
      // attached (task 038, G.6).
      h("td", { class: "task-time" }, [String(t.timeDisplay || "\u2014")]),
      h("td", { class: "task-cost" }, [String(t.costDisplay || "\u2014")]),
    ]));
  }
  table.appendChild(tbody);
  el.appendChild(table);
}

// Task 040 (I.2), reworked by #288: the iteration history is newest
// first, and its `TIMELINE_RECENT_COUNT` newest rows are always visible; the
// rest sit behind a native `<details>`/`<summary>` ("show all N"), the same
// chrome the artifacts and router-call panels already use (task 021/022).
// Collapsed is the default at any iteration count -- "collapsed" means "the
// last 3", never "nothing", so the rows an operator actually wants are never
// hidden and a 300-iteration run no longer buries them at the bottom.
//
// Task 041 (I.3): an operator's own expand/collapse choice is persisted in
// `localStorage`, scoped per SECTION TYPE, not per job (PRD I.3 -- "expanding
// the iteration history on one run means it renders expanded on the next").
// `readCollapsePref`/`writeCollapsePref`/`wireCollapsePersistence` below are
// the one shared mechanism; this function and the provisioning block
// (`renderSummary`) each call it with their own section-type string
// ("timeline"/"provisioning") so the two never share a key. This also
// closes the last gap task 040 left open: `renderTimeline` still rebuilds
// its `<details>` fresh on every `load()` poll (~4s), but the stored
// preference -- not the 15-record threshold -- now wins on every rebuild
// once the operator has toggled it once, so an in-session expand survives
// a poll too.
const TIMELINE_RECENT_COUNT = 3;

// `localStorage` key shape: `ralphd.collapse.<sectionType>`, value the
// literal string "open" or "closed". Anything else (key unset, corrupted,
// `localStorage` unavailable e.g. private browsing) falls back to that
// section's own I.2 default -- callers treat `readCollapsePref`'s return
// value as advisory, never as the only source of the open/closed decision.
const COLLAPSE_STORAGE_PREFIX = "ralphd.collapse.";

function readCollapsePref(sectionType) {
  try {
    return window.localStorage.getItem(COLLAPSE_STORAGE_PREFIX + sectionType);
  } catch (e) {
    return null;
  }
}

function writeCollapsePref(sectionType, isOpen) {
  try {
    window.localStorage.setItem(
      COLLAPSE_STORAGE_PREFIX + sectionType, isOpen ? "open" : "closed");
  } catch (e) {
    // best-effort persistence only -- a write failure (quota, private mode)
    // never blocks the toggle itself, it just does not survive a reload.
  }
}

// Wires a `<details>` element's native `toggle` event so every operator
// expand/collapse click updates the section-type-scoped preference above,
// without a hand-rolled click handler duplicating what the browser already
// fires for a `<summary>` click or a keyboard toggle.
//
// Setting the `open` attribute at element construction (above, in `h()`)
// is ITSELF a native open-state change and queues its own `toggle` event,
// exactly like an operator's click would -- verified against this
// project's own browser tier: the construction-time event fires as a
// queued task, strictly before any task scheduled after construction runs.
// Listening from the moment this function runs, unguarded, would misfile
// that construction-time event as a real operator choice and write a
// preference to `localStorage` before anyone has touched anything --
// which would make a DIFFERENT run's own I.2 default (15+ records
// collapsed, provisioning always collapsed) leak into this section's
// stored preference the first time either page loads. `ready` flips true
// only once a task scheduled right here gets its turn, which the
// construction-time toggle always beats.
function wireCollapsePersistence(detailsEl, sectionType) {
  let ready = false;
  setTimeout(() => { ready = true; }, 0);
  detailsEl.addEventListener("toggle", () => {
    if (!ready) return;
    writeCollapsePref(sectionType, detailsEl.open);
  });
}

function renderTimeline(el, iterations, runId) {
  el.innerHTML = "";
  if (iterations.length === 0) {
    el.appendChild(h("p", { class: "muted" }, ["(no iterations yet)"]));
    return;
  }
  const sorted = [...iterations].sort((a, b) => (b.number ?? 0) - (a.number ?? 0));
  const recentBox = h("div", { class: "timeline-rows timeline-recent" }, []);
  el.appendChild(recentBox);
  let olderBox = null;
  if (sorted.length > TIMELINE_RECENT_COUNT) {
    // I.3: a stored preference wins once the operator has toggled this
    // section at least once, on ANY run -- the key is scoped by section
    // type ("timeline"), not by `runId`. Default: collapsed.
    const detailsAttrs = { class: "timeline-details" };
    if (readCollapsePref("timeline") === "open") detailsAttrs.open = "";
    olderBox = h("div", { class: "timeline-rows timeline-older" }, []);
    const details = h("details", detailsAttrs, [
      h("summary", {}, ["show all " + String(sorted.length) + " iterations (" +
        String(sorted.length - TIMELINE_RECENT_COUNT) + " older)"]),
      olderBox,
    ]);
    wireCollapsePersistence(details, "timeline");
    el.appendChild(details);
  }
  for (const it of sorted) {
    const dur = it.endedAt
      ? fmtDuration(durationBetween(it.startedAt, it.endedAt))
      : (it.startedAt ? "running…" : "");
    // Task 020 (#18.1): the whole row is the affordance -- clickable and
    // keyboard-reachable (Enter/Space), like a task row -- opening that
    // iteration's header block and transcript.
    const number = it.number ?? null;
    const open = number == null ? null : () => { openIterationDialog(runId, number); };
    // Attributes are only set when there is something to click: `h()` would
    // otherwise stringify a null into `role="null"`/`onclick="null"`.
    const attrs = {
      class: "timeline-item" + (open ? " timeline-clickable" : ""),
      "data-iteration": number == null ? "" : String(number),
    };
    if (open) {
      attrs.role = "button";
      attrs.tabindex = "0";
      attrs.title = "show this iteration's detail and log";
      attrs.onclick = open;
      attrs.onkeydown = (ev) => {
        if (ev.key === "Enter" || ev.key === " ") { ev.preventDefault(); open(); }
      };
    }
    // Task 034 (G.2): the task this iteration was dispatched against and
    // the effective tier it ran at -- `assignedTaskId`/`effectiveTier`,
    // `loop.py`'s dispatch write (task 027, F.1), passed through verbatim
    // by `iteration_detail` (never in `ITERATION_DERIVED_KEYS`, so nothing
    // here re-derives them) -- plus the cost, read from THIS iteration's
    // own `costDisplay` (`format_cost(usage, decimals=4)`, task 015/019),
    // never recomputed client-side. A non-task-scoped call (planning,
    // reflect, run-level review) carries `assignedTaskId: null`, rendered
    // as the em dash the task table already uses for "nothing assigned".
    const taskCell = it.assignedTaskId == null ? "\u2014" : String(it.assignedTaskId);
    const row = h("div", attrs, [
      h("span", { class: "num" }, ["#" + (it.number ?? "?")]),
      // Task 048 (#4): when the iteration ran, not just how long it took --
      // server-formatted absolute local time (`startedAtLocal`), with the
      // raw ISO `startedAt` still in the payload for sorting/consumers.
      h("span", { class: "at" }, [String(it.startedAtLocal || "")]),
      h("span", { class: "phase" }, [String(it.phase || "")]),
      // v0.31 (#285): the agent harness beside the model --
      // `anthropic/claude-opus-5-5 · claude-code`. `harness` is meta.json's
      // own field (loop.py); a pre-0.30 iteration has none and shows the bare
      // model exactly as before.
      h("span", { class: "model" }, [iterationModelLabel(it)]),
      h("span", { class: "task-id" }, ["task: " + taskCell]),
      h("span", { class: "tier" }, ["tier: " + String(it.effectiveTier || "\u2014")]),
      h("span", { class: "cost" }, [String(it.costDisplay || "")]),
      it.error ? h("span", { class: "pill pill-failed" }, ["error"]) : null,
      h("span", { class: "dur" }, [dur]),
      // Task 015 (#89, requirement I, hub half): the SAME cause sentence the
      // iteration dialog's `cause:` line shows -- `it.causeText`, formatted
      // once by `engine.state.iteration_detail` and carried verbatim in this
      // very row's own `body.iterations` entry (`ui_server.run_detail`), so
      // the timeline row and the dialog it opens cannot disagree about why a
      // faulted iteration ended the way it did. Wraps onto its own line
      // (`.timeline-item` is `flex-wrap: wrap`) rather than crowding the
      // fixed-width columns above; absent entirely for an iteration that
      // never recorded a fault.
      it.causeText ? h("span", { class: "cause" }, ["cause: " + String(it.causeText)]) : null,
    ]);
    (olderBox && recentBox.childNodes.length >= TIMELINE_RECENT_COUNT
      ? olderBox : recentBox).appendChild(row);
  }
}

// v0.31 (#285): "<model> · <harness>" for one iteration row. Tolerates a
// missing model, a missing harness (pre-0.30 meta.json) and junk types.
function iterationModelLabel(it) {
  const model = it && it.model ? String(it.model) : "";
  const harness = it && typeof it.harness === "string" ? it.harness : "";
  return harness ? (model ? model + " \u00B7 " + harness : harness) : model;
}

// #288: the "Current iteration" panel beside the live log. Every
// field comes from `ui_server.current_iteration_panel` (`body.
// currentIteration`); the only thing computed here is elapsed time against
// the browser's clock, so the bar keeps moving between payload refreshes.
function renderCurrentIteration(el, cur, runId) {
  if (!el) return;
  el.innerHTML = "";
  if (!cur) {
    el.appendChild(h("p", { class: "muted" }, ["(unknown)"]));
    return;
  }
  const deferredLines = (cur.deferred || []).map((d) => h("div", { class: "ci-deferred" }, [
    "deferred: task " + String(d.taskId) + " until " + String(d.untilLocal || d.until) +
      (d.reason ? " \u2014 " + String(d.reason) : ""),
  ]));
  const rateLine = cur.rateLimitWaitLine
    ? h("div", { class: "ci-wait" }, ["\u23F3 " + String(cur.rateLimitWaitLine)]) : null;
  if (!cur.active) {
    el.appendChild(h("p", { class: "ci-idle muted" }, [String(cur.idleText || "idle")]));
    if (rateLine) el.appendChild(rateLine);
    for (const line of deferredLines) el.appendChild(line);
    return;
  }
  const number = cur.number ?? null;
  const title = h("div", { class: "ci-title" }, [
    h("b", {}, ["#" + (number == null ? "?" : String(number))]),
    " " + String(cur.phase || ""),
  ]);
  if (number != null) {
    title.classList.add("timeline-clickable");
    title.setAttribute("role", "button");
    title.setAttribute("tabindex", "0");
    title.title = "show this iteration's detail and log";
    const open = () => { openIterationDialog(runId, number); };
    title.onclick = open;
    title.onkeydown = (ev) => {
      if (ev.key === "Enter" || ev.key === " ") { ev.preventDefault(); open(); }
    };
  }
  el.appendChild(title);
  const rows = [];
  const modelLabel = iterationModelLabel(cur);
  if (modelLabel) rows.push(["model", modelLabel]);
  rows.push(["tier", String(cur.effectiveTier || "\u2014")]);
  if (cur.assignedTaskId != null) {
    let task = String(cur.assignedTaskId);
    if (cur.taskTitle) task += " \u2014 " + String(cur.taskTitle);
    rows.push(["task", task]);
    let attempts = "iteration " + String(cur.taskIterations ?? "?") + " on this task";
    if (cur.progressBudget) {
      attempts += ", budget " + String(cur.progressBudget.used) + "/" +
        String(cur.progressBudget.budget);
    }
    rows.push(["attempt", attempts]);
  } else {
    rows.push(["task", "\u2014"]);
  }
  if (cur.startedAtLocal) rows.push(["started", String(cur.startedAtLocal)]);
  const elapsed = cur.startedAt
    ? durationBetween(cur.startedAt, new Date().toISOString()) : null;
  const timeout = typeof cur.timeoutSeconds === "number" ? cur.timeoutSeconds : null;
  if (elapsed != null) {
    let text = fmtDuration(elapsed);
    if (timeout) {
      const left = timeout - elapsed;
      text += " of " + fmtDuration(timeout) +
        (left > 0 ? " (" + fmtDuration(left) + " left)" : " (over the iteration timeout)");
    }
    rows.push(["elapsed", text]);
  }
  if (cur.tokensDisplay) rows.push(["tokens", String(cur.tokensDisplay)]);
  if (cur.costDisplay) rows.push(["cost", String(cur.costDisplay)]);
  const grid = h("div", { class: "ci-fields" }, []);
  for (const [k, v] of rows) {
    grid.appendChild(h("div", { class: "ci-row", "data-field": k }, [
      h("span", { class: "ci-key" }, [k]), h("span", { class: "ci-val" }, [v]),
    ]));
  }
  el.appendChild(grid);
  if (elapsed != null && timeout) {
    const pct = Math.max(0, Math.min(100, (elapsed / timeout) * 100));
    // Width through CSSOM, never a `style=` attribute: HUB_CSP's
    // `style-src 'self'` blocks inline style attributes (test_hub_csp.py).
    const fill = h("div", { class: "ci-bar-fill" }, []);
    fill.style.width = pct.toFixed(1) + "%";
    el.appendChild(h("div", {
      class: "ci-bar" + (pct >= 90 ? " ci-bar-late" : ""),
      role: "progressbar", "aria-valuemin": "0", "aria-valuemax": "100",
      "aria-valuenow": String(Math.round(pct)),
    }, [fill]));
  }
  if (cur.note) el.appendChild(h("div", { class: "ci-wait" }, ["\u23F3 " + String(cur.note)]));
  if (rateLine) el.appendChild(rateLine);
  for (const line of deferredLines) el.appendChild(line);
}

async function loadLogs(runId) {
  const box = document.getElementById("logbox");
  const { ok, body } = await getJSON(`/api/runs/${encodeURIComponent(runId)}/logs?tail=200`);
  if (!box) return;
  // Task 039 (#6): `live: false` is no longer "nothing to show" -- the
  // server falls back to the on-disk transcript merge (`log_merge`), the
  // same merge the container serves from the inside, so a dead or
  // finished run still has a readable tail. It just isn't following, so
  // say so in the same wording style the detail card's `live` row uses
  // ("no (on-disk snapshot)") instead of the old, now-wrong claim that
  // there is no log to read.
  const lines = (ok && body && body.lines) ? body.lines : [];
  renderLogLines(box, lines);
  if (!ok || !body || body.live !== true) {
    box.insertBefore(
      h("div", { class: "lg-line lg-snapshot muted" },
        ["(on-disk snapshot — the run's API is not reachable, not following)"]),
      box.firstChild);
    box.scrollTop = box.scrollHeight;
  }
}

// task 014: the server (`ui_server.py`'s `/api/runs/<id>/logs`) already
// rendered the NDJSON transcript into plain text lines through the exact
// same `log_render` module `ralphctl logs` uses -- this used to
// reimplement that event-to-HTML mapping client-side (and, notably,
// lacked the CLI's `thinking_seen` guard, so a many-delta thinking block
// flooded the tail with one `[thinking…]` element per delta). Now this
// just displays the lines the server already decided on, one per DOM
// element, via `textContent` (never `innerHTML`) so nothing here needs to
// HTML-escape anything itself -- the browser does that for free for text
// nodes, and there is no per-event-type branching left to keep in sync
// with the CLI's rendering rules.
function renderLogLines(box, lines) {
  box.innerHTML = "";
  for (const line of lines) {
    box.appendChild(h("div", { class: "lg-line" }, [line]));
  }
  box.scrollTop = box.scrollHeight;
}
