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
    const table = h("table", {}, [
      h("thead", {}, [h("tr", {}, [
        "RUN", "STATE", "VERDICT", "PHASE", "APPROACH", "ITERATIONS", "STARTED",
      ].map(t => h("th", {}, [t])))]),
    ]);
    const tbody = h("tbody", {});
    for (const r of runs) {
      tbody.appendChild(h("tr", {}, [
        h("td", {}, [h("a", { href: "#/run/" + encodeURIComponent(r.runId) }, [r.runId])]),
        h("td", {}, [pill(r.state)]),
        h("td", {}, [pill(r.verdict)]),
        h("td", {}, [String(r.phase || "")]),
        h("td", {}, [String(r.approach == null ? "" : r.approach)]),
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
    ["approach", String(s.approach == null ? "" : s.approach)],
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
  const p = h("p", {}, []);
  for (const [k, v] of rows) {
    const line = h("div", {}, [h("b", {}, [k + ": "]), typeof v === "string" ? v : v]);
    p.appendChild(line);
  }
  el.appendChild(p);
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
  if (cost != null) grid.appendChild(statCard("cost", "$" + Number(cost).toFixed(4)));
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
        h("td", {}, [v.costUSD != null ? "$" + Number(v.costUSD).toFixed(4) : ""]),
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
  const list = (tasks && tasks.tasks) || [];
  if (list.length === 0) {
    el.appendChild(h("p", { class: "muted" }, ["(no tasks)"]));
    return;
  }
  const table = h("table", {}, [h("thead", {}, [h("tr", {}, [
    h("th", {}, ["id"]), h("th", {}, ["status"]), h("th", {}, ["title"]),
  ])])]);
  const tbody = h("tbody", {});
  for (const t of list) {
    tbody.appendChild(h("tr", {}, [
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
  if (!ok || !body.live) {
    box.innerHTML = "";
    box.appendChild(h("span", { class: "muted" }, [
      body && body.lines && body.lines.length ? "" : "(run's API is not reachable — no live log tail)",
    ]));
    if (body && body.lines) renderLogLines(box, body.lines);
    return;
  }
  renderLogLines(box, body.lines || []);
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
