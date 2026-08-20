/* ralphd hub — plain JS, no build step, no framework. Talks only to the
 * JSON endpoints served by ui_server.py (`/api/runs`, `/api/runs/<id>`,
 * `/api/runs/<id>/logs`, `POST /api/runs/<id>/steer`). */

const REFRESH_MS = 4000;
let refreshTimer = null;

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

async function getJSON(path) {
  const resp = await fetch(path, { cache: "no-store" });
  const body = await resp.json().catch(() => ({}));
  return { ok: resp.ok, status: resp.status, body };
}

function pill(text) {
  const cls = "pill pill-" + String(text || "unknown").toLowerCase().replace(/[^a-z0-9-]/g, "-");
  return h("span", { class: cls }, [String(text == null ? "unknown" : text)]);
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
// docs/api.md (no verdict yet -> unverified -> verified).
const RUN_STATE_ORDER = ["starting", "running", "succeeded", "failed", "aborted"];
const RUN_VERDICT_ORDER = ["", "unverified", "verified"];

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
function openTextDialog(title, text, note) {
  const previous = document.getElementById("text-dialog");
  if (previous) previous.remove();
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
  dlg.addEventListener("close", () => dlg.remove());
  document.body.appendChild(dlg);
  if (typeof dlg.showModal === "function") dlg.showModal();
  else dlg.setAttribute("open", "open");
  return dlg;
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
function taskDialogText(t) {
  const lines = [];
  lines.push("status: " + String(t.status == null ? "unknown" : t.status));
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
  return lines.join("\n");
}

function openTaskDialog(t) {
  const id = t.id == null ? "?" : String(t.id);
  const title = "Task " + id + (t.title ? " — " + String(t.title) : "");
  return openTextDialog(title, taskDialogText(t), null);
}

// -------------------------------------------------------------- routing

function router() {
  const hash = location.hash || "#/";
  const m = hash.match(/^#\/run\/([^/]+)$/);
  if (refreshTimer) { clearInterval(refreshTimer); refreshTimer = null; }
  if (m) {
    renderRunDetail(decodeURIComponent(m[1]));
  } else {
    renderRunList();
  }
}

window.addEventListener("hashchange", router);
window.addEventListener("DOMContentLoaded", () => {
  setInterval(() => {
    document.getElementById("clock").textContent = new Date().toLocaleTimeString();
  }, 1000);
  router();
});

// ---------------------------------------------------------- run list view

async function renderRunList() {
  const app = document.getElementById("app");
  app.innerHTML = "";
  app.appendChild(h("h2", {}, ["Runs"]));
  const tableWrap = h("div", { id: "run-list-wrap" }, [h("p", { class: "muted" }, ["loading…"])]);
  app.appendChild(tableWrap);

  async function load() {
    const { ok, body } = await getJSON("/api/runs");
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
      h("thead", {}, [h("tr", {}, RUN_COLUMNS.map(col => h("th", {
        class: "sortable" + (runSort.key === col.key ? " sorted" : ""),
        "data-sort-key": col.key,
        "aria-sort": runSort.key !== col.key ? "none"
          : (runSort.dir < 0 ? "descending" : "ascending"),
        onclick: () => { toggleRunSort(col.key); load(); },
      }, [col.label + sortIndicator(col.key)])))]),
    ]);
    const tbody = h("tbody", {});
    for (const r of sortRuns(runs)) {
      // Task 024 (#8): a run recorded non-terminal whose API no longer
      // answers (`containerGone`, ui_server.py) must not look exactly like a
      // healthy running one -- it is over, it just never got to say so.
      const gone = r.containerGone === true;
      tbody.appendChild(h("tr", { class: gone ? "row-warning" : "" }, [
        h("td", {}, [h("a", { href: "#/run/" + encodeURIComponent(r.runId) }, [r.runId])]),
        h("td", {}, gone
          ? [pill(r.state), h("span", { class: "container-gone-marker" }, [" \u26A0 container gone"])]
          : [pill(r.state)]),
        h("td", {}, [pill(r.verdict)]),
        h("td", {}, [String(r.phase || "")]),
        h("td", {}, [approachText(r)]),
        h("td", {}, [`${r.iterationsUsed ?? 0}/${r.iterationsBudget ?? "?"}`]),
        h("td", { class: "muted" }, [String(r.startedAt || "")]),
      ]));
    }
    table.appendChild(tbody);
    tableWrap.appendChild(table);
  }

  await load();
  refreshTimer = setInterval(load, REFRESH_MS);
}

// -------------------------------------------------------- run detail view

async function renderRunDetail(runId) {
  const app = document.getElementById("app");
  app.innerHTML = "";
  app.appendChild(h("p", {}, [h("a", { href: "#/" }, ["← runs"])]));
  app.appendChild(h("h2", {}, ["Run " + runId]));
  // Task 056 (#1): the run's PRD, one click away from its detail page.
  app.appendChild(h("p", { class: "detail-actions" }, [
    h("button", {
      type: "button", class: "open-prd", id: "open-prd",
      onclick: () => { openPrdDialog(runId); },
    }, ["view PRD"]),
  ]));

  const summary = h("div", { class: "card" }, [h("p", { class: "muted" }, ["loading…"])]);
  const usageSec = h("section", {}, [h("h2", {}, ["Usage / cost"]), h("div", { id: "usage-box" })]);
  const taskSec = h("section", {}, [h("h2", {}, ["Tasks"]), h("div", { id: "task-box" })]);
  const timelineSec = h("section", {}, [h("h2", {}, ["Iteration timeline"]), h("div", { id: "timeline-box" })]);
  const logSec = h("section", {}, [
    h("h2", {}, ["Live log tail"]),
    h("div", { id: "logbox" }, ["(loading…)"]),
  ]);
  const steerSec = h("section", {}, [
    h("h2", {}, ["Steer"]),
    h("form", { class: "steer", id: "steer-form" }, [
      h("input", { type: "text", id: "steer-name", placeholder: "name (optional)", size: "10" }),
      h("input", { type: "text", id: "steer-message", placeholder: "steering message", required: "required" }),
      h("label", {}, [h("input", { type: "checkbox", id: "steer-now" }), "apply now"]),
      h("button", { type: "submit" }, ["send"]),
    ]),
    h("div", { id: "steer-status", class: "muted" }, []),
  ]);

  app.appendChild(summary);
  app.appendChild(usageSec);
  app.appendChild(taskSec);
  app.appendChild(timelineSec);
  app.appendChild(logSec);
  app.appendChild(steerSec);
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
      const resp = await fetch(`/api/runs/${encodeURIComponent(runId)}/steer`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      const j = await resp.json().catch(() => ({}));
      if (resp.ok) {
        statusEl.textContent = "sent (" + (j.file || "ok") + ")";
        document.getElementById("steer-message").value = "";
      } else {
        statusEl.textContent = "failed: " + (j.error || resp.status);
      }
    } catch (e) {
      statusEl.textContent = "failed: " + e;
    }
  });

  async function load() {
    const { ok, status, body } = await getJSON(`/api/runs/${encodeURIComponent(runId)}`);
    if (!ok) {
      summary.innerHTML = "";
      summary.appendChild(h("p", { class: "muted" }, [
        status === 404 ? "run not found" : "failed to load run detail",
      ]));
      return;
    }
    renderSummary(summary, body);
    renderUsage(document.getElementById("usage-box"), body.status && body.status.usage);
    renderTasks(document.getElementById("task-box"), body.tasks);
    renderTimeline(document.getElementById("timeline-box"), body.iterations || []);
    await loadLogs(runId);
  }

  await load();
  refreshTimer = setInterval(load, REFRESH_MS);
}

function renderSummary(el, detail) {
  const s = detail.status || {};
  el.innerHTML = "";
  el.className = "card" + (s.state === "failed" ? " error" : "");
  const rows = [
    ["state", pill(s.state)],
    ["verdict", pill(s.verdict)],
    ["phase", String(s.phase || "")],
    ["approach", approachText(s)],
    ["iterations", `${s.iterationsUsed ?? 0}/${s.iterationsBudget ?? "?"}`],
    ["live", detail.live ? "yes (proxied from container)" : "no (on-disk snapshot)"],
  ];
  const started = s.startedAt;
  const ended = s.endedAt;
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
  if (s.endedAtLocal) rows.push(["ended", String(s.endedAtLocal)]);
  const p = h("p", {}, []);
  for (const [k, v] of rows) {
    const line = h("div", {}, [h("b", {}, [k + ": "]), typeof v === "string" ? v : v]);
    p.appendChild(line);
  }
  el.appendChild(p);
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
  renderContainerGone(el, detail);
  renderReflect(el, s);
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
    el.appendChild(h("p", { class: "infra-wait" }, [
      "\u26A0 degraded: infra outage episode in progress " +
      "(a retry attempt is running, no backoff wait pending)",
    ]));
    return;
  }
  const box = h("div", { class: "infra-wait" }, [
    h("div", {}, [
      "\u26A0 degraded: infra outage — attempt " + String(wait.attempt ?? "?") +
      " (phase " + String(wait.phase || "?") + ")",
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
function renderRetryNow(box, runId, live, wait) {
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
      const resp = await fetch(`/api/runs/${encodeURIComponent(runId)}/retry`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
      });
      const j = await resp.json().catch(() => ({}));
      if (resp.ok) {
        statusEl.textContent = " retrying now (outage budget clock reset)";
      } else {
        statusEl.textContent = " failed: " +
          String(j.detail || j.title || j.error || resp.status);
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

function renderUsage(el, usage) {
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
    const card = statCard("cost", costText);
    if (usage.costStatus) card.classList.add("cost-" + usage.costStatus);
    grid.appendChild(card);
  }
  el.appendChild(grid);
  for (const [label, key] of [["by phase", "byPhase"], ["by approach", "byApproach"]]) {
    const bucket = usage[key];
    if (!bucket || Object.keys(bucket).length === 0) continue;
    el.appendChild(h("h3", { class: "muted" }, [label]));
    const table = h("table", {}, [h("thead", {}, [h("tr", {}, [
      h("th", {}, [key === "byPhase" ? "phase" : "approach"]),
      h("th", {}, ["tokens"]),
      h("th", {}, ["cost"]),
    ])])]);
    const tbody = h("tbody", {});
    for (const [k, v] of Object.entries(bucket)) {
      tbody.appendChild(h("tr", {}, [
        h("td", {}, [k]),
        h("td", {}, [String(v.totalTokens ?? "")]),
        h("td", {}, [v.costDisplay ?? (v.costUSD != null ? "$" + Number(v.costUSD).toFixed(4) : "")]),
      ]));
    }
    table.appendChild(tbody);
    el.appendChild(table);
  }
}

function statCard(label, value) {
  return h("div", { class: "stat" }, [
    h("div", { class: "k" }, [label]),
    h("div", { class: "v" }, [String(value)]),
  ]);
}

function renderTasks(el, tasks) {
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
  ])])]);
  const tbody = h("tbody", {});
  for (const t of list) {
    // Task 057 (#2): the whole row is the affordance -- clickable, and
    // reachable from the keyboard (Enter/Space), since a <tr> is not
    // natively focusable.
    const open = () => { openTaskDialog(t); };
    tbody.appendChild(h("tr", {
      class: "task-row",
      "data-task-id": String(t.id == null ? "" : t.id),
      role: "button",
      tabindex: "0",
      title: "show this task's success criteria",
      onclick: open,
      onkeydown: (ev) => {
        if (ev.key === "Enter" || ev.key === " ") { ev.preventDefault(); open(); }
      },
    }, [
      h("td", {}, [String(t.id || "")]),
      h("td", {}, [pill(t.status)]),
      h("td", {}, [String(t.title || "")]),
    ]));
  }
  table.appendChild(tbody);
  el.appendChild(table);
}

function renderTimeline(el, iterations) {
  el.innerHTML = "";
  if (iterations.length === 0) {
    el.appendChild(h("p", { class: "muted" }, ["(no iterations yet)"]));
    return;
  }
  const sorted = [...iterations].sort((a, b) => (a.number ?? 0) - (b.number ?? 0));
  for (const it of sorted) {
    const dur = it.endedAt
      ? fmtDuration(durationBetween(it.startedAt, it.endedAt))
      : (it.startedAt ? "running…" : "");
    const row = h("div", { class: "timeline-item" }, [
      h("span", { class: "num" }, ["#" + (it.number ?? "?")]),
      // Task 048 (#4): when the iteration ran, not just how long it took --
      // server-formatted absolute local time (`startedAtLocal`), with the
      // raw ISO `startedAt` still in the payload for sorting/consumers.
      h("span", { class: "at" }, [String(it.startedAtLocal || "")]),
      h("span", { class: "phase" }, [String(it.phase || "")]),
      h("span", {}, [String(it.model || "")]),
      it.error ? h("span", { class: "pill pill-failed" }, ["error"]) : null,
      h("span", { class: "dur" }, [dur]),
    ]);
    el.appendChild(row);
  }
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
