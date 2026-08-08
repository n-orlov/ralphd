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

function esc(s) {
  return String(s == null ? "" : s)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
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
      body && body.text ? "" : "(run's API is not reachable — no live log tail)",
    ]));
    if (body && body.text) renderLogText(box, body.text);
    return;
  }
  renderLogText(box, body.text || "");
}

// -- NDJSON pretty renderer, mirroring ralphctl's `_render_logs` rules
// (docs/cli.md `logs` section / src/ralphd/cli/main.py) -------------------

function renderLogText(box, raw) {
  box.innerHTML = "";
  const lines = raw.split("\n");
  let textOpen = false, textBuf = "";
  const flushText = () => {
    if (textOpen && textBuf) {
      box.appendChild(h("span", { class: "lg-text", html: esc(textBuf) }));
      box.appendChild(document.createElement("br"));
    }
    textOpen = false;
    textBuf = "";
  };
  for (const raw_line of lines) {
    if (!raw_line.trim()) continue;
    let ev;
    try {
      ev = JSON.parse(raw_line);
    } catch {
      flushText();
      box.appendChild(h("div", { class: "lg-malformed" },
        [`! [malformed log line, ${raw_line.length} bytes]`]));
      continue;
    }
    if (typeof ev !== "object" || ev === null || Array.isArray(ev)) {
      flushText();
      box.appendChild(h("div", { class: "lg-malformed" }, ["! [malformed log line: not a JSON object]"]));
      continue;
    }
    const etype = ev.type;
    if (etype === "ralphd.iteration") {
      flushText();
      renderBoundary(box, ev);
    } else if (etype === "message_update") {
      const inner = ev.assistantMessageEvent || {};
      if (inner.type === "text_delta") {
        textOpen = true;
        textBuf += inner.delta || "";
      } else if (inner.type === "text_end") {
        flushText();
      } else if (inner.type === "thinking_start" || inner.type === "thinking_delta") {
        box.appendChild(h("div", { class: "lg-thinking" }, ["[thinking…]"]));
      }
    } else if (etype === "tool_execution_end") {
      flushText();
      renderToolResult(box, ev);
    } else if (etype === "message_end") {
      flushText();
      renderMessageEnd(box, ev.message || {});
    }
    // everything else silently skipped, matching the CLI renderer.
  }
  flushText();
  box.scrollTop = box.scrollHeight;
}

function fmtArgs(obj) {
  if (typeof obj !== "object" || obj === null) {
    const s = JSON.stringify(obj);
    return s.length > 60 ? s.slice(0, 57) + "..." : s;
  }
  const parts = [];
  for (const [k, v] of Object.entries(obj).slice(0, 3)) {
    let s = JSON.stringify(v);
    if (s && s.length > 40) s = s.slice(0, 37) + "...";
    parts.push(`${k}=${s}`);
  }
  return parts.join(", ");
}

function renderBoundary(box, ev) {
  const { number: n, phase, model, approach } = ev;
  if (ev.event === "start") {
    box.appendChild(h("div", { class: "lg-boundary-start" },
      [`── iteration ${n} · phase=${phase} · model=${model} · approach=${approach} ──`]));
    return;
  }
  const usage = ev.usage || {};
  const bits = [`iteration ${n} done`];
  if (ev.exitCode !== undefined && ev.exitCode !== null) bits.push(`exit=${ev.exitCode}`);
  if (usage && Object.keys(usage).length) {
    bits.push(`tokens=${usage.totalTokens ?? 0}`);
    if (usage.costUSD != null) bits.push(`cost=$${usage.costUSD}`);
  }
  box.appendChild(h("div", { class: "lg-boundary-end" }, ["  " + bits.join(", ")]));
  if (ev.error) {
    box.appendChild(h("div", { class: "lg-error" }, [`!! iteration ${n} error: ${ev.error}`]));
  }
}

function renderToolResult(box, ev) {
  const name = ev.toolName || "?";
  const fargs = fmtArgs(ev.args || ev.arguments || {});
  const isError = !!ev.isError;
  const outcome = isError ? "✗ error" : "✓ ok";
  let tail = "";
  if (typeof ev.result === "string" && ev.result && !isError) {
    tail = " (" + ev.result.slice(0, 60) + ")";
  }
  box.appendChild(h("div", { class: isError ? "lg-tool-err" : "lg-tool" },
    [`  → ${name}(${fargs}) ${outcome}${tail}`]));
}

function renderMessageEnd(box, message) {
  for (const item of message.content || []) {
    if (!item || typeof item !== "object") continue;
    if (item.type === "text") {
      box.appendChild(h("div", { class: "lg-text", html: esc(item.text || "") }));
    } else if (item.type === "thinking") {
      box.appendChild(h("div", { class: "lg-thinking" }, ["[thinking…]"]));
    } else if (item.type === "toolCall") {
      box.appendChild(h("div", { class: "lg-tool" },
        [`  → ${item.name || "?"}(${fmtArgs(item.arguments || {})})`]));
    }
  }
}
