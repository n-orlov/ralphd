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
  const resp = await fetch(`/api/runs/${encodeURIComponent(runId)}`,
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

// ------------------------------------------------- iteration detail (#18.1)

// Task 020 (#18.1): a timeline row used to be a dead summary -- phase, model,
// duration -- and "why did iteration 47 end like that, and what did the agent
// actually do in it" meant leaving the hub for `ralphctl iteration`/the run
// dir. Clicking a row now opens that iteration's own story in THE single
// `openTextDialog`.
//
// Every string in the dialog was formatted in Python by
// `state.iteration_summary_lines` + the shared `log_render` renderer and
// arrives as one `text` blob (ui_server.iteration_view), so the hub cannot
// word an exit reason, a duration or a token count differently from `ralphctl
// iteration`, and the transcript is rendered by the same code as the log tail.
// The endpoint is purely on-disk, so this works for a dead run with no
// container -- hence no live/snapshot note here (unlike the PRD dialog).
function iterationTitle(runId, number) {
  return "Iteration #" + String(number) + " — " + String(runId);
}

async function openIterationDialog(runId, number) {
  const path = `/api/runs/${encodeURIComponent(runId)}/iterations/` +
    encodeURIComponent(String(number));
  const { ok, body } = await getJSON(path);
  const text = (ok && body && typeof body.text === "string" && body.text)
    ? body.text
    : "(failed to load iteration " + String(number) + ")";
  return openTextDialog(iterationTitle(runId, number), text, null);
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

async function openArtifactDialog(runId, a) {
  const name = String(a.path || a.key || "");
  const path = `/api/runs/${encodeURIComponent(runId)}/artifacts/` +
    encodeURIComponent(name);
  const { ok, body } = await getJSON(path);
  const text = (ok && body && typeof body.text === "string" && body.text)
    ? body.text
    : "(failed to load " + name + ")";
  return openTextDialog(artifactTitle(runId, a), text, null);
}

function renderArtifacts(box, ok, body, runId) {
  box.innerHTML = "";
  if (!ok || !body) {
    box.appendChild(h("p", { class: "muted" }, [ARTIFACT_LOAD_FAILED]));
    return;
  }
  const items = Array.isArray(body.artifacts) ? body.artifacts : [];
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
  box.appendChild(list);
  if (body.notice) {
    // Wording comes from the server (`state.NO_ARTIFACTS`, the same line
    // `ralphctl artifacts ls` prints), like the document panel's notice.
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
        h("td", {}, gone
          ? [pill(r.state), h("span", { class: "container-gone-marker" }, [" \u26A0 container gone"])]
          : [pill(r.state)]),
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
    // Task 017 (#17): the write surface above is no longer the whole story --
    // what was queued and what the loop already applied is listed here.
    h("h3", { class: "muted" }, ["Steering history"]),
    h("div", { id: "steering-box" }, [h("p", { class: "muted" }, ["(loading…)"])]),
  ]);

  app.appendChild(summary);
  app.appendChild(docSec);
  app.appendChild(artifactSec);
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
        // Task 017 (#17): show it in the history immediately rather than
        // making the operator wait out the 4s poll to see their own message.
        loadSteering(runId);
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
    renderDelete(document.getElementById("delete-box"), runId, body);
    renderUsage(document.getElementById("usage-box"),
                body.status && body.status.usage, runId);
    renderTasks(document.getElementById("task-box"), body.tasks);
    renderTimeline(document.getElementById("timeline-box"), body.iterations || [], runId);
    await loadLogs(runId);
    await loadSteering(runId);
    await loadDocuments(runId);
    await loadArtifacts(runId);
  }

  await load();
  refreshTimer = setInterval(load, REFRESH_MS);
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
// pill's own look is untouched -- the badge only makes it clickable.
function stateCell(detail, s) {
  const pillEl = pill(s.state);
  if (!FAULT_BADGE_STATES.has(String(s.state || ""))) return pillEl;
  return faultBadge(detail.runId, "state", pillEl) || pillEl;
}

function renderSummary(el, detail) {
  const s = detail.status || {};
  el.innerHTML = "";
  el.className = "card" + (s.state === "failed" ? " error" : "");
  const rows = [
    ["state", stateCell(detail, s)],
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

function renderTimeline(el, iterations, runId) {
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
    const row = h("div", attrs, [
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
