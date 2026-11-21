/* Foundry FinOps dashboard – vanilla JS, no build step. Charts: Chart.js 4. */
"use strict";

// ============================================================ utilities
const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const h = (html) => { const t = document.createElement("template"); t.innerHTML = html.trim(); return t.content.firstElementChild; };
const state = { info: null, charts: new Map(), currency: "USD" };
const PALETTE = ["#0078d4", "#8764b8", "#00b294", "#ff8c00", "#e81123", "#00bcf2", "#b4009e", "#107c10", "#ffb900", "#5c2d91", "#038387", "#c239b3"];

const fmt = {
  money: (v, cur = state.currency) => v == null ? "–" : new Intl.NumberFormat(undefined, { style: "currency", currency: cur || "USD", maximumFractionDigits: v >= 1000 ? 0 : 2 }).format(v),
  num: (v) => v == null ? "–" : new Intl.NumberFormat(undefined, { maximumFractionDigits: 2 }).format(v),
  compact: (v) => v == null ? "–" : new Intl.NumberFormat(undefined, { notation: "compact", maximumFractionDigits: 1 }).format(v),
  date: (v) => { if (!v) return "–"; const d = new Date(v); return isNaN(d) ? v : d.toLocaleDateString(); },
  dt: (v) => { if (!v) return "–"; const d = new Date(v); return isNaN(d) ? v : d.toLocaleString(); },
  pct: (v) => v == null ? "–" : `${fmt.num(v)}%`,
};

async function api(path, { method = "GET", body } = {}) {
  const res = await fetch(path, {
    method,
    headers: { "Content-Type": "application/json", "X-Requested-With": "finops-dashboard" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
  return data;
}

function toast(msg, isError = false) {
  const t = $("#toast");
  t.textContent = msg;
  t.className = `toast show${isError ? " error" : ""}`;
  clearTimeout(toast._t);
  toast._t = setTimeout(() => (t.className = "toast"), isError ? 6000 : 2800);
}

function tableToRecords(table) {
  const names = table.columns.map((c) => c.name ?? c);
  return table.rows.map((r) => Object.fromEntries(names.map((n, i) => [n, r[i]])));
}

// ============================================================ downloads
function download(filename, content, type) {
  const blob = content instanceof Blob ? content : new Blob([content], { type });
  const a = Object.assign(document.createElement("a"), { href: URL.createObjectURL(blob), download: filename });
  document.body.appendChild(a); a.click(); a.remove();
  setTimeout(() => URL.revokeObjectURL(a.href), 1000);
}
function csvCell(v) {
  if (v == null) return "";
  if (typeof v === "object") v = JSON.stringify(v);
  v = String(v);
  if (/^[=+\-@\t\r]/.test(v)) v = "'" + v; // spreadsheet formula-injection guard
  return /[",\n\r]/.test(v) ? `"${v.replace(/"/g, '""')}"` : v;
}
function toCSV(columns, records) {
  return [columns.map(csvCell).join(","), ...records.map((r) => columns.map((c) => csvCell(r[c])).join(","))].join("\r\n");
}
const stamp = () => new Date().toISOString().slice(0, 16).replace(/[:T]/g, "-");
function downloadRecords(name, columns, records, kind) {
  if (kind === "json") download(`${name}-${stamp()}.json`, JSON.stringify(records, null, 2), "application/json");
  else download(`${name}-${stamp()}.csv`, "\ufeff" + toCSV(columns, records), "text/csv;charset=utf-8");
}
function downloadChart(name, canvas) {
  const out = document.createElement("canvas");
  out.width = canvas.width; out.height = canvas.height;
  const ctx = out.getContext("2d");
  ctx.fillStyle = "#fff"; ctx.fillRect(0, 0, out.width, out.height); ctx.drawImage(canvas, 0, 0);
  out.toBlob((b) => download(`${name}-${stamp()}.png`, b));
}

// ============================================================ charts
function pivot(records, x, y, split, agg = "sum") {
  const xs = [], keys = [], cells = new Map();
  for (const r of records) {
    const xv = r[x], k = split ? String(r[split] ?? "(blank)") : y;
    if (!xs.includes(xv)) xs.push(xv);
    if (!keys.includes(k)) keys.push(k);
    const id = `${xv}\u0000${k}`;
    const cur = cells.get(id) || { s: 0, n: 0 };
    cur.s += Number(r[y]) || 0; cur.n += 1; cells.set(id, cur);
  }
  const isTime = xs.length && xs.every((v) => typeof v === "string" && /^\d{4}-\d{2}-\d{2}/.test(v));
  if (isTime) xs.sort();
  const val = (c) => (!c ? 0 : agg === "avg" ? c.s / c.n : c.s);
  // keep the series readable: top 10 by total, rest grouped as "Other"
  let ordered = keys.map((k) => [k, xs.reduce((a, xv) => a + val(cells.get(`${xv}\u0000${k}`)), 0)]).sort((a, b) => b[1] - a[1]).map((e) => e[0]);
  const top = ordered.slice(0, 10), rest = ordered.slice(10);
  const datasets = top.map((k, i) => ({ label: k, data: xs.map((xv) => val(cells.get(`${xv}\u0000${k}`))), color: PALETTE[i % PALETTE.length] }));
  if (rest.length) datasets.push({ label: `Other (${rest.length})`, data: xs.map((xv) => rest.reduce((a, k) => a + val(cells.get(`${xv}\u0000${k}`)), 0)), color: "#a19f9d" });
  const labels = xs.map((v) => (isTime ? (String(v).length > 10 && !String(v).endsWith("T00:00:00Z") ? fmt.dt(v) : fmt.date(v)) : v));
  return { labels, datasets, isTime };
}

function drawChart(canvas, kind, { labels, datasets }, { yFormat = fmt.compact, stacked = true } = {}) {
  if (!window.Chart) { canvas.replaceWith(h(`<div class="empty">Charts need internet access to load Chart.js.</div>`)); return null; }
  state.charts.get(canvas)?.destroy();
  const type = { timechart: "line", linechart: "line", line: "line", areachart: "line", area: "line", columnchart: "bar", column: "bar", barchart: "bar", bar: "bar", piechart: "doughnut", pie: "doughnut", doughnut: "doughnut" }[kind] || "line";
  const isArea = kind === "areachart" || kind === "area";
  const circular = type === "doughnut";
  let cfgData;
  if (circular) {
    const totals = datasets.map((d) => d.data.reduce((a, b) => a + (+b || 0), 0));
    const single = datasets.length === 1;
    cfgData = single
      ? { labels, datasets: [{ data: datasets[0].data, backgroundColor: labels.map((_, i) => PALETTE[i % PALETTE.length]) }] }
      : { labels: datasets.map((d) => d.label), datasets: [{ data: totals, backgroundColor: datasets.map((d) => d.color) }] };
  } else {
    cfgData = { labels, datasets: datasets.map((d) => ({ label: d.label, data: d.data, borderColor: d.color, backgroundColor: isArea ? d.color + "55" : d.color, fill: isArea, tension: 0.25, pointRadius: labels.length > 60 ? 0 : 2, borderWidth: 2 })) };
  }
  const chart = new Chart(canvas, {
    type, data: cfgData,
    options: {
      responsive: true, maintainAspectRatio: false, animation: false,
      indexAxis: kind === "barchart" ? "y" : "x",
      interaction: { mode: circular ? "nearest" : "index", intersect: circular },
      plugins: {
        legend: { position: circular ? "right" : "bottom", labels: { boxWidth: 12, font: { size: 11 } } },
        tooltip: { callbacks: { label: (c) => `${c.dataset.label || c.label}: ${yFormat(circular ? c.parsed : (kind === "barchart" ? c.parsed.x : c.parsed.y))}` } },
      },
      scales: circular ? {} : {
        x: { stacked: type === "bar" && stacked, ticks: { maxRotation: 0, autoSkip: true, font: { size: 11 } }, grid: { display: false } },
        y: { stacked: (type === "bar" || isArea) && stacked, ticks: { callback: (v) => yFormat(v), font: { size: 11 } }, beginAtZero: true },
      },
    },
  });
  state.charts.set(canvas, chart);
  return chart;
}

function chartCard(title, id, { tall = false, name } = {}) {
  const el = h(`<div class="card"><h3><span>${esc(title)}</span><button class="btn sm" title="Download chart as PNG">⤓ PNG</button></h3><div class="chart-box${tall ? " tall" : ""}"><canvas id="${id}"></canvas></div></div>`);
  $("button", el).onclick = () => downloadChart(name || id, $("canvas", el));
  return el;
}

// ============================================================ grid
/**
 * Searchable, sortable, paged grid with CSV/JSON download.
 * cols: [{key, label, render?(row), num?, csv?(row)}]
 */
function Grid({ cols, rows, name, actions, toolbar, pageSize = 50, empty = "No items" }) {
  const el = h(`<div class="grid-wrap"><div class="grid-tools"><input type="search" placeholder="Search to filter items…" aria-label="Filter rows"><span class="extra"></span><button class="btn sm" data-dl="csv">⤓ CSV</button><button class="btn sm" data-dl="json">⤓ JSON</button><span class="count"></span></div><div class="grid-scroll"><table class="grid"><thead></thead><tbody></tbody></table></div><div class="pager"></div></div>`);
  if (toolbar) $(".extra", el).replaceWith(toolbar);
  let sortKey = null, sortDir = 1, page = 0, query = "";
  const textOf = (r) => cols.map((c) => { const v = c.csv ? c.csv(r) : r[c.key]; return typeof v === "object" ? JSON.stringify(v) : v; }).join(" ").toLowerCase();
  const filtered = () => {
    let out = query ? rows.filter((r) => textOf(r).includes(query)) : rows.slice();
    if (sortKey) out.sort((a, b) => { const x = a[sortKey], y = b[sortKey]; return (x == null) - (y == null) || (x > y ? 1 : x < y ? -1 : 0) * sortDir; });
    return out;
  };
  const render = () => {
    const data = filtered();
    const pages = Math.max(1, Math.ceil(data.length / pageSize));
    page = Math.min(page, pages - 1);
    $("thead", el).innerHTML = `<tr>${cols.map((c) => `<th data-k="${esc(c.key)}">${esc(c.label ?? c.key)}<span class="arrow">${sortKey === c.key ? (sortDir > 0 ? "▲" : "▼") : ""}</span></th>`).join("")}${actions ? "<th></th>" : ""}</tr>`;
    const body = $("tbody", el);
    body.innerHTML = "";
    if (!data.length) body.innerHTML = `<tr><td colspan="${cols.length + 1}" class="empty">${esc(query ? "No items match your search" : empty)}</td></tr>`;
    for (const r of data.slice(page * pageSize, (page + 1) * pageSize)) {
      const tr = document.createElement("tr");
      for (const c of cols) {
        const td = document.createElement("td");
        if (c.num) td.className = "num";
        const v = c.render ? c.render(r) : r[c.key];
        if (v instanceof Node) td.appendChild(v); else td.innerHTML = c.render ? v ?? "" : esc(typeof v === "object" && v !== null ? JSON.stringify(v) : v);
        td.title = td.textContent;
        tr.appendChild(td);
      }
      if (actions) {
        const td = document.createElement("td"); td.className = "actions";
        for (const a of actions) {
          if (a.show && !a.show(r)) continue;
          const b = h(`<button class="btn sm ${a.danger ? "danger" : ""}">${esc(a.label)}</button>`);
          b.onclick = () => a.run(r); td.appendChild(b); td.append(" ");
        }
        tr.appendChild(td);
      }
      body.appendChild(tr);
    }
    $(".count", el).textContent = `${data.length} of ${rows.length} item${rows.length === 1 ? "" : "s"}`;
    $(".pager", el).innerHTML = pages > 1 ? `<button class="btn sm" data-p="-1" ${page ? "" : "disabled"}>‹ Prev</button> Page ${page + 1} of ${pages} <button class="btn sm" data-p="1" ${page < pages - 1 ? "" : "disabled"}>Next ›</button>` : "";
  };
  el.addEventListener("click", (e) => {
    const th = e.target.closest("th[data-k]");
    if (th) { const k = th.dataset.k; sortDir = sortKey === k ? -sortDir : 1; sortKey = k; render(); }
    const p = e.target.closest("[data-p]"); if (p) { page += +p.dataset.p; render(); }
    const dl = e.target.closest("[data-dl]");
    if (dl) {
      const recs = filtered().map((r) => Object.fromEntries(cols.map((c) => [c.label ?? c.key, c.csv ? c.csv(r) : r[c.key]])));
      downloadRecords(name, cols.map((c) => c.label ?? c.key), recs, dl.dataset.dl);
    }
  });
  $("input", el).addEventListener("input", (e) => { query = e.target.value.trim().toLowerCase(); page = 0; render(); });
  render();
  el.update = (newRows) => { rows = newRows; render(); };
  return el;
}

// ============================================================ flyout forms
/**
 * fields: [{key, label, type: text|number|select|date|checkbox|textarea|kv|section, options, help, required, value, placeholder, readonly}]
 */
function openForm({ title, fields, submitLabel = "Save", onSubmit, extraNote }) {
  const fly = $("#flyout"), body = $("#flyoutBody"), foot = $("#flyoutFoot");
  $("#flyoutTitle").textContent = title;
  body.innerHTML = extraNote ? `<div class="note info">${extraNote}</div>` : "";
  const form = h(`<form novalidate></form>`);
  for (const f of fields) {
    if (f.type === "section") { form.appendChild(h(`<h4 style="margin:16px 0 8px">${esc(f.label)}</h4>`)); continue; }
    const id = `f_${f.key.replace(/\W/g, "_")}`;
    const lab = h(`<label class="f" for="${id}"><span>${esc(f.label)}${f.required ? " *" : ""}</span></label>`);
    let input;
    if (f.type === "select") {
      input = h(`<select id="${id}">${f.options.map((o) => { const [v, l] = Array.isArray(o) ? o : [o, o]; return `<option value="${esc(v)}">${esc(l)}</option>`; }).join("")}</select>`);
    } else if (f.type === "textarea") {
      input = h(`<textarea id="${id}" rows="${f.rows || 4}" style="font-family:var(--mono)"></textarea>`);
    } else if (f.type === "checkbox") {
      input = h(`<input id="${id}" type="checkbox" style="width:auto">`);
      input.checked = !!f.value;
    } else if (f.type === "kv") {
      input = kvEditor(f.value || {});
    } else {
      input = h(`<input id="${id}" type="${f.type || "text"}" ${f.step ? `step="${f.step}"` : ""} placeholder="${esc(f.placeholder || "")}">`);
    }
    input.dataset.key = f.key;
    if (f.type !== "checkbox" && f.type !== "kv" && f.value != null) input.value = f.value;
    if (f.readonly) input.disabled = true;
    lab.appendChild(input);
    if (f.help) lab.appendChild(h(`<small>${f.help}</small>`));
    form.appendChild(lab);
  }
  const err = h(`<div class="err" role="alert"></div>`);
  body.append(form, err);
  foot.innerHTML = "";
  const ok = h(`<button class="btn primary" type="button">${esc(submitLabel)}</button>`);
  const cancel = h(`<button class="btn" type="button">Cancel</button>`);
  foot.append(ok, cancel);
  const close = () => { fly.classList.remove("open"); fly.setAttribute("aria-hidden", "true"); };
  cancel.onclick = close; $("#flyoutClose").onclick = close;
  const collect = () => {
    const out = {};
    for (const el of $$("[data-key]", form)) {
      const k = el.dataset.key;
      let v = el.type === "checkbox" ? el.checked : el.kv ? el.kv() : el.value;
      const fdef = fields.find((x) => x.key === k);
      if (fdef?.type === "number" && v !== "") v = Number(v);
      if (fdef?.required && (v === "" || v == null)) throw new Error(`${fdef.label} is required`);
      setPath(out, k, v);
    }
    return out;
  };
  ok.onclick = async () => {
    err.textContent = ""; ok.disabled = true;
    try { await onSubmit(collect()); close(); } catch (e) { err.textContent = e.message; } finally { ok.disabled = false; }
  };
  form.onsubmit = (e) => { e.preventDefault(); ok.click(); };
  fly.classList.add("open"); fly.setAttribute("aria-hidden", "false");
  setTimeout(() => $("input:not([disabled]),select,textarea", form)?.focus(), 50);
}
function setPath(obj, path, v) { const ks = path.split("."); let o = obj; ks.slice(0, -1).forEach((k) => (o = o[k] ??= {})); o[ks.at(-1)] = v; }
function kvEditor(initial) {
  const box = h(`<div><div class="rows"></div><button type="button" class="btn sm">+ Add tag</button></div>`);
  const add = (k = "", v = "") => {
    const row = h(`<div class="kv-row"><input type="text" placeholder="Name" aria-label="Tag name"><input type="text" placeholder="Value" aria-label="Tag value"><button type="button" class="btn sm danger" aria-label="Remove">✕</button></div>`);
    const [ki, vi] = $$("input", row); ki.value = k; vi.value = v;
    $("button", row).onclick = () => row.remove();
    $(".rows", box).appendChild(row);
  };
  Object.entries(initial).forEach(([k, v]) => add(k, v));
  if (!Object.keys(initial).length) add();
  $("button.btn.sm:not(.danger)", box).onclick = () => add();
  box.kv = () => Object.fromEntries($$(".kv-row", box).map((r) => $$("input", r).map((i) => i.value.trim())).filter(([k]) => k));
  return box;
}
async function confirmDelete(what, fn) {
  if (!confirm(`Delete ${what}? This can't be undone.`)) return;
  try { await fn(); toast(`Deleted ${what}`); route(); } catch (e) { toast(e.message, true); }
}

// ============================================================ page helpers
function page(title, sub, ...children) {
  const el = h(`<div class="page"><div class="page-title"><h1>${esc(title)}</h1></div>${sub ? `<p class="page-sub">${sub}</p>` : ""}</div>`);
  children.filter(Boolean).forEach((c) => el.appendChild(c));
  return el;
}
function cmdbar(items) {
  const bar = h(`<div class="cmdbar"></div>`);
  for (const it of items) {
    if (it === "|") { bar.appendChild(h(`<span class="sep"></span>`)); continue; }
    if (it instanceof Node) { bar.appendChild(it); continue; }
    const b = h(`<button class="cmd"${it.disabled ? " disabled" : ""}><span class="g">${it.icon || ""}</span>${esc(it.label)}</button>`);
    b.onclick = it.run; bar.appendChild(b);
  }
  return bar;
}
const writable = () => !state.info?.read_only;
const badgeFor = (stateText) => {
  const cls = { "On track": "ok", "Near threshold": "warn", "Limit reached": "bad", "Over budget": "bad", Succeeded: "ok", active: "ok", published: "ok", suspended: "warn", notPublished: "warn" }[stateText] || "info";
  return `<span class="badge ${cls}">${esc(stateText ?? "–")}</span>`;
};
const progress = (pct) => {
  if (pct == null) return "–";
  const cls = pct >= 100 ? "bad" : pct >= 80 ? "warn" : "";
  return `${fmt.pct(pct)}<div class="bar ${cls}"><div style="width:${Math.min(100, pct)}%"></div></div>`;
};
const chips = (obj) => Object.entries(obj || {}).map(([k, v]) => `<span class="chip"><b>${esc(k)}</b>${Array.isArray(v) ? ": " + esc(v.join(", ")) : v !== "" && v != null ? " = " + esc(v) : ""}</span>`).join("") || '<span style="color:var(--muted)">–</span>';
function selectEl(options, value, label) {
  const s = h(`<select aria-label="${esc(label)}" style="width:auto">${options.map((o) => { const [v, l] = Array.isArray(o) ? o : [o, o]; return `<option value="${esc(v)}"${String(v) === String(value) ? " selected" : ""}>${esc(l)}</option>`; }).join("")}</select>`);
  return s;
}
function labeled(label, el) { const w = h(`<label class="inline" style="gap:6px;flex:none"><span style="color:var(--muted)">${esc(label)}</span></label>`); w.appendChild(el); return w; }

// ============================================================ Overview
async function pageOverview() {
  const [ov, cost, tokens, ccs, quota] = await Promise.all([
    api("/api/overview?days=30"), api("/api/cost?group_by=project&days=30"), api("/api/tokens?group_by=deployment&hours=168"),
    api("/api/cost-controls"), api("/api/quota"),
  ]);
  state.currency = ov.currency || "USD";
  const k = ov.kpis;
  const kpi = (l, v, s = "", cls = "") => `<div class="kpi ${cls}"><div class="l">${esc(l)}</div><div class="v">${v}</div><div class="s">${s}</div></div>`;
  const kpis = h(`<div class="kpis">
    ${kpi("Spend month to date", fmt.money(k.spend_mtd), "Billed cost · 8–24 h delay")}
    ${kpi("Forecast for month end", fmt.money(k.forecast_month_end), "Cost Management forecast")}
    ${kpi("Tokens, last 24 h", fmt.compact(k.tokens_24h), `${fmt.compact(k.requests_24h)} requests · near real time`)}
    ${kpi("Cost controls", k.cost_controls, `${k.cost_controls_attention} need attention`, k.cost_controls_attention ? "warn" : "")}
    ${kpi("Budgets", k.budgets, "Azure Cost Management")}
    ${kpi("Highest quota use", fmt.pct(k.quota_max_pct), "Regional TPM quota", k.quota_max_pct >= 90 ? "bad" : k.quota_max_pct >= 75 ? "warn" : "")}
  </div>`);
  const cards = h(`<div class="cards"></div>`);
  const c1 = chartCard("Daily spend by project (30 days)", "ovCost", { name: "spend-by-project" });
  const c2 = chartCard("Tokens by deployment (7 days)", "ovTokens", { name: "tokens-by-deployment" });
  const c3 = chartCard("Spend by agent (30 days)", "ovAgents", { name: "spend-by-agent" });
  const c4 = chartCard("Spend by model (30 days)", "ovModels", { name: "spend-by-model" });
  cards.append(c1, c2, c3, c4);
  const ccCard = h(`<div class="card"><h3><span>Cost controls closest to their limit</span><a href="#/cost-controls">Manage</a></h3></div>`);
  ccCard.appendChild(Grid({ name: "cost-controls-status", rows: [...ccs].sort((a, b) => (b.percent_used ?? -1) - (a.percent_used ?? -1)).slice(0, 6), cols: [
    { key: "name", label: "Cost control" }, { key: "category", label: "Type" },
    { key: "percent_used", label: "Used (estimated)", render: (r) => progress(r.percent_used) },
    { key: "state", label: "State", render: (r) => badgeFor(r.state) }] }));
  const qCard = h(`<div class="card"><h3><span>Quota use</span><a href="#/quota">Manage</a></h3></div>`);
  qCard.appendChild(Grid({ name: "quota", rows: quota, cols: [
    { key: "display_name", label: "Quota" }, { key: "current_value", label: "Used", num: true },
    { key: "limit", label: "Limit", num: true }, { key: "percent_used", label: "%", render: (r) => progress(r.percent_used) }] }));
  cards.append(ccCard, qCard);
  const view = page("Overview", `${esc(state.info.foundry_account || "")} · ${state.info.mode === "demo" ? "Demo data – all names and numbers are illustrative" : "Live data"}`, kpis, cards);
  mount(view);
  drawChart($("#ovCost"), "columnchart", pivot(tableToRecords(cost), "UsageDate", "Cost", "project"), { yFormat: (v) => fmt.money(v) });
  drawChart($("#ovTokens"), "areachart", pivot(tableToRecords(tokens), "TimeGenerated", "TotalTokens", "deployment"));
  drawChart($("#ovAgents"), "barchart", { labels: ov.top_agents.slice(0, 10).map((x) => x.name), datasets: [{ label: "Spend", data: ov.top_agents.slice(0, 10).map((x) => x.cost), color: PALETTE[1] }] }, { yFormat: (v) => fmt.money(v) });
  drawChart($("#ovModels"), "piechart", { labels: ov.top_models.map((x) => x.name), datasets: [{ label: "Spend", data: ov.top_models.map((x) => x.cost), color: PALETTE[0] }] }, { yFormat: (v) => fmt.money(v) });
}

// ============================================================ Cost analysis
async function pageCost() {
  const groupSel = selectEl([["project", "Project (Foundry tag)"], ["agent", "Agent (agent.name tag)"], ["deployment", "Deployment (tag)"], ["model", "Model / meter"], ["team", "Team (your tag)"]], "project", "Group by");
  const daysSel = selectEl([[7, "Last 7 days"], [30, "Last 30 days"], [60, "Last 60 days"]], 30, "Time range");
  const chartSel = selectEl([["columnchart", "Stacked column"], ["areachart", "Area"], ["timechart", "Line"], ["piechart", "Donut (totals)"]], "columnchart", "Chart type");
  const kpis = h(`<div class="kpis"></div>`);
  const chart = chartCard("Cost", "costChart", { tall: true, name: "cost-analysis" });
  const gridHost = h(`<div style="margin-top:16px"></div>`);
  const view = page("Cost analysis", "Billed cost from Azure Cost Management, attributed with the <code>project</code>, <code>deployment</code> and <code>agent.name</code> tags Foundry adds to model charges. Cost appears 8–24 hours after usage.",
    cmdbar([labeled("Group by", groupSel), labeled("Range", daysSel), labeled("Chart", chartSel), "|", { icon: "⟳", label: "Refresh", run: () => load() }]), kpis, chart, gridHost);
  mount(view);
  async function load() {
    const g = groupSel.value;
    const t = await api(`/api/cost?group_by=${g}&days=${daysSel.value}`);
    const recs = tableToRecords(t);
    const total = recs.reduce((a, r) => a + (+r.Cost || 0), 0);
    const byKey = {};
    recs.forEach((r) => (byKey[r[g]] = (byKey[r[g]] || 0) + (+r.Cost || 0)));
    const ranked = Object.entries(byKey).sort((a, b) => b[1] - a[1]);
    const days = new Set(recs.map((r) => r.UsageDate)).size || 1;
    kpis.innerHTML = `<div class="kpi"><div class="l">Total</div><div class="v">${fmt.money(total)}</div></div>
      <div class="kpi"><div class="l">Average per day</div><div class="v">${fmt.money(total / days)}</div></div>
      <div class="kpi"><div class="l">Top ${esc(g)}</div><div class="v" style="font-size:18px">${esc(ranked[0]?.[0] ?? "–")}</div><div class="s">${fmt.money(ranked[0]?.[1])}</div></div>
      <div class="kpi"><div class="l">Distinct ${esc(g)} values</div><div class="v">${ranked.length}</div></div>`;
    $("h3 span", chart).textContent = `Cost by ${g}`;
    drawChart($("#costChart"), chartSel.value, pivot(recs, "UsageDate", "Cost", g), { yFormat: (v) => fmt.money(v) });
    const summary = ranked.map(([k, v]) => ({ [g]: k, Cost: Math.round(v * 100) / 100, Share: Math.round((v / total) * 1000) / 10 }));
    gridHost.innerHTML = "";
    const tabs = h(`<div class="subtabs"><button class="active">Summary</button><button>Daily detail</button></div>`);
    const summaryGrid = Grid({ name: `cost-by-${g}`, rows: summary, cols: [{ key: g }, { key: "Cost", num: true, render: (r) => fmt.money(r.Cost) }, { key: "Share", label: "Share %", num: true, render: (r) => progress(r.Share) }] });
    const detailGrid = Grid({ name: `daily-cost-by-${g}`, rows: recs, cols: t.columns.map((c) => ({ key: c.name, num: c.name === "Cost" })) });
    detailGrid.style.display = "none";
    $$("button", tabs).forEach((b, i) => (b.onclick = () => { $$("button", tabs).forEach((x) => x.classList.remove("active")); b.classList.add("active"); summaryGrid.style.display = i ? "none" : ""; detailGrid.style.display = i ? "" : "none"; }));
    gridHost.append(tabs, summaryGrid, detailGrid);
  }
  [groupSel, daysSel, chartSel].forEach((s) => (s.onchange = load));
  await load();
}

// ============================================================ Usage (metrics)
async function pageUsage() {
  const live = state.info.mode === "live";
  const groupOpts = live ? [["deployment", "Deployment"], ["model", "Model"]] : [["deployment", "Deployment"], ["model", "Model"], ["project", "Project"], ["agent", "Agent"], ["team", "Team"]];
  const groupSel = selectEl(groupOpts, "deployment", "Split by");
  const metricSel = selectEl([["TotalTokens", "Total tokens"], ["InputTokens", "Input (prompt) tokens"], ["OutputTokens", "Output (generated) tokens"], ["Requests", "Requests"]], "TotalTokens", "Metric");
  const hoursSel = selectEl([[24, "Last 24 hours"], [48, "Last 48 hours"], [168, "Last 7 days"], [336, "Last 14 days"]], 168, "Time range");
  const chartSel = selectEl([["timechart", "Line"], ["areachart", "Stacked area"], ["columnchart", "Stacked column"], ["piechart", "Donut (totals)"]], "timechart", "Chart type");
  const chart = chartCard("Tokens", "useChart", { tall: true, name: "token-usage" });
  const gridHost = h(`<div style="margin-top:16px"></div>`);
  const view = page("Metrics – token usage", live ? "Azure Monitor platform metrics (<code>ProcessedPromptTokens</code>, <code>GeneratedTokens</code>, <code>AzureOpenAIRequests</code>) split by deployment or model. Near real time." : "Token usage per deployment, model, project, agent and team. In live mode, project/agent/team splits come from tracing (see Logs).",
    cmdbar([labeled("Metric", metricSel), labeled("Split by", groupSel), labeled("Range", hoursSel), labeled("Chart", chartSel), "|", { icon: "⟳", label: "Refresh", run: () => load() }]), chart, gridHost);
  mount(view);
  async function load() {
    const t = await api(`/api/tokens?group_by=${groupSel.value}&hours=${hoursSel.value}`);
    const recs = tableToRecords(t);
    $("h3 span", chart).textContent = `${metricSel.selectedOptions[0].text} by ${groupSel.value}`;
    drawChart($("#useChart"), chartSel.value, pivot(recs, "TimeGenerated", metricSel.value, groupSel.value));
    gridHost.innerHTML = "";
    gridHost.appendChild(Grid({ name: `tokens-by-${groupSel.value}`, rows: recs, cols: t.columns.map((c) => ({ key: c.name, num: c.type !== "string" && c.type !== "datetime", render: c.name === "TimeGenerated" ? (r) => esc(fmt.dt(r.TimeGenerated)) : c.type === "long" ? (r) => fmt.num(r[c.name]) : undefined })) }));
  }
  [groupSel, metricSel, hoursSel, chartSel].forEach((s) => (s.onchange = load));
  await load();
}

// ============================================================ Logs (Log Analytics look)
const KQL_KW = /\b(where|extend|project|project-away|project-reorder|summarize|by|order|sort|asc|desc|take|limit|top|distinct|count|render|and|or|not|in|has|contains|startswith|endswith|between|let|join|on|union|mv-expand|parse|make-series|timechart|columnchart|piechart|barchart|areachart|linechart|table)\b/g;
function highlightKql(src) {
  const out = [];
  const re = /(\/\/[^\n]*)|("(?:[^"\\\n]|\\.)*"|'(?:[^'\\\n]|\\.)*')|(\b\d+(?:\.\d+)?(?:d|h|m|s|ms)?\b)|([A-Za-z_][\w-]*)(?=\s*\()|([A-Za-z_][\w-]*)|(\||==|!=|=~|>=|<=)|([\s\S])/g;
  let m, first = true;
  while ((m = re.exec(src))) {
    if (m[1]) out.push(`<span class="tok-cm">${esc(m[1])}</span>`);
    else if (m[2]) out.push(`<span class="tok-str">${esc(m[2])}</span>`);
    else if (m[3]) out.push(`<span class="tok-num">${esc(m[3])}</span>`);
    else if (m[4]) out.push(`<span class="tok-fn">${esc(m[4])}</span>`);
    else if (m[5]) {
      const w = m[5];
      if (KQL_KW.test(w)) out.push(`<span class="tok-kw">${esc(w)}</span>`);
      else if (first) out.push(`<span class="tok-tbl">${esc(w)}</span>`);
      else out.push(esc(w));
      KQL_KW.lastIndex = 0; first = false;
    } else if (m[6]) out.push(`<span class="tok-op">${esc(m[6])}</span>`);
    else out.push(esc(m[7]));
  }
  return out.join("") + "\n";
}

const LS_TABS_BASE = "finops.logs.tabs", LS_MINE = "finops.logs.mine";
async function pageLogs() {
  const [schema, saved] = await Promise.all([api("/api/logs/schema"), api("/api/logs/saved")]);
  const firstQuery = Object.values(saved)[0] || Object.keys(schema)[0] || "";
  const LS_TABS = `${LS_TABS_BASE}.${state.info.mode}`;
  let tabs = JSON.parse(localStorage.getItem(LS_TABS) || "null") || [{ name: "New Query 1", text: firstQuery }];
  let active = 0, last = null;
  const persist = () => localStorage.setItem(LS_TABS, JSON.stringify(tabs));
  const mine = () => JSON.parse(localStorage.getItem(LS_MINE) || "{}");

  const root = h(`<div class="la">
    <div class="la-tabs" role="tablist"></div>
    <div class="la-toolbar">
      <button class="run-btn" title="Run (Shift+Enter)">▶ Run</button>
      <label class="inline" style="gap:6px;flex:none;margin-left:8px"><span style="color:var(--muted)">Time range</span><select class="timespan" style="width:auto">
        <option value="PT1H">Last hour</option><option value="PT24H">Last 24 hours</option><option value="P7D" selected>Last 7 days</option><option value="P30D">Last 30 days</option><option value="P60D">Last 60 days</option></select></label>
      <span class="sep"></span>
      <button class="cmd" data-act="save"><span class="g">💾</span>Save</button>
      <button class="cmd" data-act="csv"><span class="g">⤓</span>Export CSV</button>
      <button class="cmd" data-act="json"><span class="g">⤓</span>Export JSON</button>
      <button class="cmd" data-act="png"><span class="g">⤓</span>Chart PNG</button>
      <span class="sep"></span>
      <button class="cmd" data-act="format"><span class="g">≡</span>Format query</button>
      <span style="margin-left:auto;color:var(--muted);font-size:12px">${state.info.mode === "demo" ? "Demo tables · KQL subset" : esc(state.info.logs_target || "Set FINOPS_LOGS_TARGET")}</span>
    </div>
    <div class="la-body">
      <div class="la-side">
        <div class="subtabs"><button class="active" data-side="tables">Tables</button><button data-side="queries">Queries</button></div>
        <div style="padding:6px 8px"><input type="search" class="side-filter" placeholder="Filter…" aria-label="Filter tables and queries"></div>
        <div class="la-side-list"></div>
      </div>
      <div class="la-main">
        <div class="editor"><div class="gutter"></div><div class="code-wrap"><pre aria-hidden="true"></pre><textarea spellcheck="false" aria-label="KQL query editor"></textarea></div></div>
        <div class="results">
          <div class="results-head"><div class="subtabs"><button class="active" data-res="grid">Results</button><button data-res="chart">Chart</button></div><span class="status">Run a query to see results.</span></div>
          <div class="results-body"></div>
        </div>
      </div>
    </div></div>`);
  mount(root);
  const ta = $("textarea", root), pre = $("pre", root), gutter = $(".gutter", root), status = $(".status", root), resBody = $(".results-body", root);
  const sync = () => {
    pre.innerHTML = highlightKql(ta.value);
    gutter.innerHTML = ta.value.split("\n").map((_, i) => i + 1).join("<br>");
    pre.scrollTop = ta.scrollTop; pre.scrollLeft = ta.scrollLeft; gutter.scrollTop = ta.scrollTop;
    tabs[active].text = ta.value; persist();
  };
  ta.addEventListener("input", sync);
  ta.addEventListener("scroll", () => { pre.scrollTop = ta.scrollTop; pre.scrollLeft = ta.scrollLeft; gutter.scrollTop = ta.scrollTop; });
  ta.addEventListener("keydown", (e) => {
    if ((e.key === "Enter" && (e.shiftKey || e.ctrlKey || e.metaKey))) { e.preventDefault(); run(); }
    if (e.key === "Tab") { e.preventDefault(); const s = ta.selectionStart; ta.setRangeText("    ", s, ta.selectionEnd, "end"); sync(); }
  });
  const insert = (text) => { const s = ta.selectionStart; ta.setRangeText(text, s, ta.selectionEnd, "end"); ta.focus(); sync(); };

  function renderTabs() {
    const bar = $(".la-tabs", root);
    bar.innerHTML = "";
    tabs.forEach((t, i) => {
      const b = h(`<div class="la-tab${i === active ? " active" : ""}" role="tab" tabindex="0"><span>${esc(t.name)}</span>${tabs.length > 1 ? '<span class="x" title="Close">✕</span>' : ""}</div>`);
      b.onclick = (e) => {
        if (e.target.classList.contains("x")) { tabs.splice(i, 1); active = Math.max(0, Math.min(active, tabs.length - 1)); }
        else active = i;
        persist(); renderTabs(); ta.value = tabs[active].text; sync();
      };
      b.ondblclick = () => { const n = prompt("Rename query tab", t.name); if (n) { t.name = n; persist(); renderTabs(); } };
      bar.appendChild(b);
    });
    const plus = h(`<button class="la-tab" title="New query tab">＋</button>`);
    plus.onclick = () => { tabs.push({ name: `New Query ${tabs.length + 1}`, text: Object.keys(schema)[0] || "" }); active = tabs.length - 1; persist(); renderTabs(); ta.value = tabs[active].text; sync(); ta.focus(); };
    bar.appendChild(plus);
  }

  let side = "tables";
  function renderSide() {
    const list = $(".la-side-list", root), q = $(".side-filter", root).value.toLowerCase();
    list.innerHTML = "";
    if (side === "tables") {
      for (const [table, cols] of Object.entries(schema)) {
        if (q && !table.toLowerCase().includes(q) && !cols.some((c) => c.name.toLowerCase().includes(q))) continue;
        const item = h(`<div><div class="tree-item" title="Double-click to insert"><span class="tw">▸</span><span style="color:var(--blue)">▦</span>${esc(table)}</div><div class="tree-cols" hidden>${cols.map((c) => `<div data-col="${esc(c.name)}">${esc(c.name)}<em>${esc(c.type)}</em></div>`).join("")}</div></div>`);
        const head = $(".tree-item", item), body = $(".tree-cols", item);
        head.onclick = () => { body.hidden = !body.hidden; $(".tw", item).textContent = body.hidden ? "▸" : "▾"; };
        head.ondblclick = () => insert(table);
        body.onclick = (e) => { const c = e.target.closest("[data-col]"); if (c) insert(c.dataset.col); };
        list.appendChild(item);
      }
    } else {
      const all = [...Object.entries(saved).map(([n, t]) => [n, t, "Example"]), ...Object.entries(mine()).map(([n, t]) => [n, t, "My query"])];
      for (const [name, text, kind] of all) {
        if (q && !name.toLowerCase().includes(q) && !text.toLowerCase().includes(q)) continue;
        const it = h(`<div class="query-item"><b>${esc(name)} <span class="badge ${kind === "My query" ? "info" : ""}">${kind}</span></b><code>${esc(text.replace(/\s+/g, " "))}</code></div>`);
        it.onclick = () => { tabs[active] = { name, text }; persist(); renderTabs(); ta.value = text; sync(); run(); };
        list.appendChild(it);
      }
    }
  }
  $$("[data-side]", root).forEach((b) => (b.onclick = () => { $$("[data-side]", root).forEach((x) => x.classList.remove("active")); b.classList.add("active"); side = b.dataset.side; renderSide(); }));
  $(".side-filter", root).oninput = renderSide;

  let resTab = "grid", chartCfg = {};
  $$("[data-res]", root).forEach((b) => (b.onclick = () => { $$("[data-res]", root).forEach((x) => x.classList.remove("active")); b.classList.add("active"); resTab = b.dataset.res; renderResults(); }));

  async function run() {
    const text = ta.value.trim();
    if (!text) return;
    status.className = "status"; status.textContent = "Running…";
    const t0 = performance.now();
    try {
      last = await api("/api/logs/query", { method: "POST", body: { query: text, timespan: $(".timespan", root).value } });
      const secs = ((performance.now() - t0) / 1000).toFixed(2);
      status.className = "status status-ok";
      status.textContent = `Completed · ${last.rows.length} record${last.rows.length === 1 ? "" : "s"} · ${secs}s`;
      chartCfg = autoChart(last);
      if (last.render) { resTab = "chart"; $$("[data-res]", root).forEach((x) => x.classList.toggle("active", x.dataset.res === "chart")); }
      renderResults();
    } catch (e) {
      last = null; status.className = "status status-err"; status.textContent = "Query failed";
      resBody.innerHTML = `<div class="empty err" style="text-align:left">${esc(e.message)}</div>`;
    }
  }
  function autoChart(res) {
    const cols = res.columns;
    const time = cols.find((c) => c.type === "datetime");
    const nums = cols.filter((c) => ["long", "real", "int", "decimal", "double"].includes(c.type));
    const strs = cols.filter((c) => c.type === "string");
    const type = res.render || (time ? "timechart" : strs.length ? "columnchart" : "columnchart");
    const x = (time || strs[0] || cols[0])?.name;
    const y = nums.find((c) => c.name !== x)?.name || cols.at(-1)?.name;
    const split = time ? strs.find((c) => c.name !== x)?.name || "" : (type === "piechart" ? "" : strs.find((c) => c.name !== x)?.name || "");
    return { type, x, y, split, agg: "sum" };
  }
  function renderResults() {
    resBody.innerHTML = "";
    if (!last) { resBody.innerHTML = `<div class="empty">Run a query to see results.</div>`; return; }
    const recs = tableToRecords(last);
    if (resTab === "grid") {
      const g = Grid({ name: tabs[active].name.replace(/\W+/g, "-").toLowerCase(), rows: recs, pageSize: 100, cols: last.columns.map((c) => ({ key: c.name, num: ["long", "real", "int", "double", "decimal"].includes(c.type), render: c.type === "dynamic" ? (r) => `<code>${esc(JSON.stringify(r[c.name]))}</code>` : undefined })) });
      g.style.cssText = "flex:1;display:flex;flex-direction:column;border:0;min-height:0";
      resBody.appendChild(g);
      return;
    }
    const area = h(`<div class="chart-area"><div class="chart-box"><canvas id="laChart"></canvas></div><div class="chart-format"><h4>Chart formatting</h4></div></div>`);
    const fmtPane = $(".chart-format", area);
    const colNames = last.columns.map((c) => c.name);
    const ctl = (label, key, opts) => { const s = selectEl(opts, chartCfg[key], label); s.onchange = () => { chartCfg[key] = s.value; renderResults(); }; const l = h(`<label class="f"><span>${esc(label)}</span></label>`); l.appendChild(s); fmtPane.appendChild(l); s.style.width = "100%"; };
    ctl("Chart type", "type", [["timechart", "Line"], ["areachart", "Stacked area"], ["columnchart", "Stacked column"], ["barchart", "Bar"], ["piechart", "Pie / donut"]]);
    ctl("X-axis", "x", colNames);
    ctl("Y-axis", "y", colNames);
    ctl("Split by", "split", [["", "None"], ...colNames]);
    ctl("Aggregation", "agg", [["sum", "Sum"], ["avg", "Average"]]);
    resBody.appendChild(area);
    const data = pivot(recs, chartCfg.x, chartCfg.y, chartCfg.split || null, chartCfg.agg);
    if (chartCfg.type === "piechart" && !chartCfg.split) drawChart($("#laChart"), "piechart", data);
    else drawChart($("#laChart"), chartCfg.type, data, { stacked: chartCfg.type !== "timechart" });
  }
  $(".run-btn", root).onclick = run;
  root.addEventListener("click", (e) => {
    const act = e.target.closest("[data-act]")?.dataset.act;
    if (!act) return;
    if (act === "save") {
      const n = prompt("Save query as", tabs[active].name);
      if (n) { const m = mine(); m[n] = ta.value; localStorage.setItem(LS_MINE, JSON.stringify(m)); tabs[active].name = n; persist(); renderTabs(); renderSide(); toast("Query saved in this browser"); }
    }
    if ((act === "csv" || act === "json") && last) downloadRecords(tabs[active].name.replace(/\W+/g, "-").toLowerCase(), last.columns.map((c) => c.name), tableToRecords(last), act);
    if (act === "png") { const c = $("#laChart", root); if (c) downloadChart("log-chart", c); else toast("Open the Chart tab first", true); }
    if (act === "format") { ta.value = ta.value.replace(/\s*\|\s*/g, "\n| ").trim(); sync(); }
    if ((act === "csv" || act === "json") && !last) toast("Run a query first", true);
  });
  renderTabs(); renderSide();
  ta.value = tabs[active].text; sync();
  if (ta.value) run();
}

// ============================================================ Cost controls
function costControlForm(existing) {
  const e = existing || {};
  const f = e.filters || {};
  const today = new Date().toISOString().slice(0, 10);
  const nextYear = new Date(Date.now() + 365 * 864e5).toISOString().slice(0, 10);
  openForm({
    title: existing ? `Edit cost control – ${e.name}` : "Create cost control",
    extraNote: "Cost controls are in preview. They count GPT and o-series model tokens as requests run and act within about 15 minutes. Spending totals are estimates and aren't reconciled with your bill.",
    fields: [
      { type: "section", label: "What are you controlling?" },
      { key: "name", label: "Name", required: true, value: e.name, readonly: !!existing, help: "1–63 letters, numbers, hyphens or underscores. Can't be changed later." },
      { key: "project", label: "Owner", type: "select", options: [["", "Foundry resource (all projects)"], ...state.projects.map((p) => [p, `Project: ${p}`])], value: e.project || "", readonly: !!existing },
      { type: "section", label: "What does it apply to?" },
      { key: "filters.project", label: "Projects", value: (f.project || []).join(", "), placeholder: "e.g. fraud-risk", help: "Comma-separated project names. An agent filter needs exactly one project." },
      { key: "filters.agent", label: "Agents", value: (f.agent || []).join(", "), placeholder: "e.g. fraud-case-review" },
      { key: "filters.deployment", label: "Deployments", value: (f.deployment || []).join(", "), placeholder: "e.g. gpt-5.4-mini" },
      { key: "filters.sessionId", label: "Hosted agent session IDs", value: (f.sessionId || []).join(", "), help: "Up to 20." },
      { key: "sharing", label: "How is the budget shared?", type: "select", options: [["shared", "One budget for everything"], ["agent", "Budget per agent"], ["sessionId", "Budget per session"], ["project", "Budget per project (resource only)"]], value: e.sharing || "shared" },
      { type: "section", label: "Set the budget" },
      { key: "category", label: "Budget type", type: "select", options: [["Cost", "Spending (USD)"], ["Tokens", "Tokens"]], value: e.category || "Cost", readonly: !!existing, help: "Can't be changed after creation." },
      { key: "filters.tokenType", label: "Token types (Tokens budgets)", type: "select", options: [["", "All token types"], ["input", "Input only"], ["output", "Output only"], ["input,output", "Input and output"]], value: (f.tokenType || []).join(",") },
      { key: "amount", label: "Amount (USD) or token limit", type: "number", step: "any", required: true, value: e.amount },
      { key: "time_grain", label: "Resets", type: "select", options: ["Daily", "Weekly", "Monthly"], value: e.time_grain || "Monthly", help: "Changing the reset period or sharing starts a new usage count." },
      { key: "start_date", label: "Start date (UTC)", type: "date", value: (e.start_date || today).slice(0, 10) },
      { key: "end_date", label: "End date (UTC)", type: "date", value: (e.end_date || nextYear).slice(0, 10), help: "Every cost control has an end date – extend it before it passes." },
      { type: "section", label: "Threshold actions" },
      { key: "alert.threshold", label: "Send alert at (% of budget)", type: "number", value: e.alert?.threshold ?? "", placeholder: "e.g. 80 – leave empty for no alert" },
      { key: "alert.event_grid_connection_id", label: "Event Grid connection ID", value: e.alert?.event_grid_connection_id ?? state.connection ?? "", help: "Use the connection Foundry created for an alert in the portal; connections you create yourself are skipped." },
      { key: "block.threshold", label: "Block usage at (% of budget)", type: "number", value: e.block?.threshold ?? "", placeholder: "e.g. 100 – leave empty to only track" },
    ],
    submitLabel: existing ? "Save" : "Create",
    onSubmit: async (v) => {
      v.filters = Object.fromEntries(Object.entries(v.filters || {}).map(([k, s]) => [k, String(s).split(",").map((x) => x.trim()).filter(Boolean)]).filter(([, a]) => a.length));
      if (existing) { v.name = e.name; v.project = e.project || ""; v.category = e.category; }
      if (v.alert?.threshold === "" || v.alert?.threshold == null) v.alert = null;
      if (v.block?.threshold === "" || v.block?.threshold == null) v.block = null;
      await api("/api/cost-controls", { method: "POST", body: v });
      toast(`${existing ? "Updated" : "Created"} ${v.name} – takes effect within 15 minutes`);
      route();
    },
  });
}

async function pageCostControls() {
  const rows = await api("/api/cost-controls");
  state.connection = rows.find((r) => r.alert?.event_grid_connection_id)?.alert.event_grid_connection_id;
  const tags = await api("/api/tags").catch(() => []);
  state.projects = [...new Set(tags.filter((t) => /projects\//.test(t.scope)).map((t) => t.name))];
  const amount = (r) => r.category === "Tokens" ? `${fmt.compact(r.amount)} tokens` : fmt.money(r.amount);
  const grid = Grid({
    name: "cost-controls", rows, empty: "No cost controls yet. Create one to alert on or block spending.",
    cols: [
      { key: "name", label: "Policy name", render: (r) => `<b>${esc(r.name)}</b>` },
      { key: "project", label: "Owner", render: (r) => r.project ? `Project: ${esc(r.project)}` : "Foundry resource", csv: (r) => r.project || "resource" },
      { key: "filters", label: "Applies to", render: (r) => chips(r.filters), csv: (r) => JSON.stringify(r.filters) },
      { key: "amount", label: "Budget", render: (r) => `${amount(r)} / ${esc(r.time_grain.toLowerCase())}<br><small style="color:var(--muted)">${r.sharing === "shared" ? "One total" : "Per " + esc(r.sharing)}</small>`, csv: (r) => r.amount },
      { key: "alert", label: "Actions", render: (r) => [r.alert && `${r.alert.threshold}% · Alert`, r.block && `${r.block.threshold}% · Block`].filter(Boolean).map((x) => `<span class="chip">${esc(x)}</span>`).join("") || "Track only", csv: (r) => [r.alert && `alert ${r.alert.threshold}%`, r.block && `block ${r.block.threshold}%`].filter(Boolean).join("; ") },
      { key: "start_date", label: "Start", render: (r) => esc(fmt.date(r.start_date)) },
      { key: "end_date", label: "End", render: (r) => esc(fmt.date(r.end_date)) },
      { key: "percent_used", label: "Closest to limit (est.)", render: (r) => progress(r.percent_used) },
      { key: "state", label: "State", render: (r) => badgeFor(r.state) },
    ],
    actions: writable() ? [
      { label: "Edit", run: (r) => costControlForm(r) },
      { label: "Delete", danger: true, run: (r) => confirmDelete(`cost control ${r.name}`, () => api(`/api/cost-controls/${encodeURIComponent(r.name)}?project=${encodeURIComponent(r.project || "")}`, { method: "DELETE" })) },
    ] : null,
  });
  mount(page("Cost controls", "Real-time spending or token budgets on the Foundry resource, projects, deployments, agents or hosted agent sessions. Blocked requests return HTTP 429 with <code>x-ms-budget-cause: budget_exceeded</code>.",
    cmdbar([{ icon: "＋", label: "Create", run: () => costControlForm(), disabled: !writable() }, { icon: "⟳", label: "Refresh", run: route }]), grid));
}

// ============================================================ Budgets
function budgetForm(existing) {
  const e = existing || {};
  const n = e.notifications || [];
  const actual = n.find((x) => x.threshold_type === "Actual"), fc = n.find((x) => x.threshold_type === "Forecasted");
  const firstOfMonth = new Date(); firstOfMonth.setUTCDate(1);
  openForm({
    title: existing ? `Edit budget – ${e.name}` : "Create budget",
    extraNote: "Cost Management budgets alert on billed cost (8–24 h delay). They can't block requests – use cost controls for that.",
    fields: [
      { key: "name", label: "Name", required: true, value: e.name, readonly: !!existing },
      { key: "amount", label: "Amount", type: "number", step: "any", required: true, value: e.amount },
      { key: "time_grain", label: "Reset period", type: "select", options: ["Monthly", "Quarterly", "Annually"], value: e.time_grain || "Monthly" },
      { key: "start_date", label: "Start date", type: "date", value: (e.start_date || firstOfMonth.toISOString()).slice(0, 10), help: "Must be the first day of a month." },
      { key: "end_date", label: "End date", type: "date", value: (e.end_date || "").slice(0, 10) },
      { key: "filter_tag", label: "Filter by tag (name=value1,value2)", value: e.filter?.tags ? `${e.filter.tags.name}=${e.filter.tags.values.join(",")}` : "", placeholder: "team=Support", help: "Leave empty for the whole scope." },
      { type: "section", label: "Alert conditions" },
      { key: "actual", label: "Actual cost alert at (%)", type: "number", value: actual?.threshold ?? 80 },
      { key: "forecast", label: "Forecasted cost alert at (%)", type: "number", value: fc?.threshold ?? 100 },
      { key: "emails", label: "Alert recipients", value: (actual?.contact_emails || fc?.contact_emails || []).join(", "), placeholder: "finops@contoso.com", help: "Comma-separated email addresses." },
    ],
    onSubmit: async (v) => {
      const emails = String(v.emails || "").split(",").map((x) => x.trim()).filter(Boolean);
      const notifications = [];
      if (v.actual !== "" && v.actual != null) notifications.push({ threshold: v.actual, threshold_type: "Actual", contact_emails: emails });
      if (v.forecast !== "" && v.forecast != null) notifications.push({ threshold: v.forecast, threshold_type: "Forecasted", contact_emails: emails });
      let filter = null;
      if (v.filter_tag) { const [name, vals] = v.filter_tag.split("="); filter = { tags: { name: name.trim(), operator: "In", values: (vals || "").split(",").map((x) => x.trim()).filter(Boolean) } }; }
      await api("/api/budgets", { method: "POST", body: { name: existing ? e.name : v.name, amount: v.amount, time_grain: v.time_grain, start_date: v.start_date, end_date: v.end_date || null, filter, notifications } });
      toast(`Saved budget ${existing ? e.name : v.name}`); route();
    },
  });
}
async function pageBudgets() {
  const rows = await api("/api/budgets");
  const grid = Grid({
    name: "budgets", rows, empty: "No budgets in this scope.",
    cols: [
      { key: "name", label: "Name", render: (r) => `<b>${esc(r.name)}</b>` },
      { key: "amount", label: "Budget", render: (r) => `${fmt.money(r.amount, r.currency)} / ${esc((r.time_grain || "").toLowerCase())}`, csv: (r) => r.amount },
      { key: "current_spend", label: "Current spend", num: true, render: (r) => fmt.money(r.current_spend, r.currency) },
      { key: "forecast_spend", label: "Forecast", num: true, render: (r) => fmt.money(r.forecast_spend, r.currency) },
      { key: "percent_used", label: "Used", render: (r) => progress(r.percent_used) },
      { key: "filter", label: "Filter", render: (r) => r.filter ? `<code>${esc(JSON.stringify(r.filter))}</code>` : "Whole scope" },
      { key: "notifications", label: "Alerts", render: (r) => (r.notifications || []).map((n) => `<span class="chip">${esc(n.threshold_type)} ≥ ${n.threshold}%</span>`).join(""), csv: (r) => (r.notifications || []).map((n) => `${n.threshold_type} ${n.threshold}%`).join("; ") },
    ],
    actions: writable() ? [{ label: "Edit", run: (r) => budgetForm(r) }, { label: "Delete", danger: true, run: (r) => confirmDelete(`budget ${r.name}`, () => api(`/api/budgets/${encodeURIComponent(r.name)}`, { method: "DELETE" })) }] : null,
  });
  const chart = rows.length ? chartCard("Spend against budget", "budChart", { name: "budgets" }) : null;
  mount(page("Budgets", "Azure Cost Management budgets for the resource group or subscription. Alerts by email or action group; up to 5 actual and 5 forecasted alert conditions per budget.",
    cmdbar([{ icon: "＋", label: "Create", run: () => budgetForm(), disabled: !writable() }, { icon: "⟳", label: "Refresh", run: route }]), chart, h(`<div style="height:16px"></div>`), grid));
  if (chart) drawChart($("#budChart"), "barchart", { labels: rows.map((r) => r.name), datasets: [
    { label: "Current spend", data: rows.map((r) => r.current_spend || 0), color: PALETTE[0] },
    { label: "Forecast", data: rows.map((r) => r.forecast_spend || 0), color: PALETTE[3] },
    { label: "Budget", data: rows.map((r) => r.amount), color: "#a19f9d" }] }, { yFormat: (v) => fmt.money(v), stacked: false });
}

// ============================================================ Tags
async function pageTags() {
  const rows = await api("/api/tags");
  const editTags = (r) => openForm({
    title: `Tags – ${r.name}`,
    extraNote: "Tags on the Foundry resource, projects, resource group or subscription show up in Cost Management (turn on tag inheritance for resource-group and subscription tags). Foundry's own <code>project</code>, <code>deployment</code> and <code>agent.name</code> billing tags are added automatically and can't be edited here.",
    fields: [{ key: "scope", label: "Scope", value: r.scope, readonly: true }, { key: "tags", label: "Tags", type: "kv", value: r.tags }],
    onSubmit: async (v) => { await api("/api/tags", { method: "PUT", body: { scope: r.scope, tags: v.tags } }); toast(`Tags saved on ${r.name}`); route(); },
  });
  // tag summary: name -> value -> resources
  const summary = [];
  rows.forEach((r) => Object.entries(r.tags || {}).forEach(([k, v]) => {
    let s = summary.find((x) => x.tag === k && x.value === v);
    if (!s) summary.push((s = { tag: k, value: v, resources: [] }));
    s.resources.push(r.name);
  }));
  const tabs = h(`<div class="subtabs"><button class="active">Resources</button><button>Tag summary</button></div>`);
  const resGrid = Grid({
    name: "tag-inventory", rows,
    cols: [
      { key: "name", label: "Resource", render: (r) => `<b>${esc(r.name)}</b>` },
      { key: "type", label: "Type" },
      { key: "tags", label: "Tags", render: (r) => chips(r.tags), csv: (r) => Object.entries(r.tags || {}).map(([k, v]) => `${k}=${v}`).join("; ") },
      { key: "scope", label: "Resource ID", render: (r) => `<code style="font-size:11px">${esc(r.scope)}</code>` },
    ],
    actions: writable() ? [{ label: "Edit tags", run: editTags }, { label: "Clear", danger: true, show: (r) => Object.keys(r.tags || {}).length > 0, run: (r) => confirmDelete(`all tags on ${r.name}`, () => api("/api/tags", { method: "PUT", body: { scope: r.scope, tags: {} } })) }] : null,
  });
  const sumGrid = Grid({ name: "tag-summary", rows: summary, cols: [{ key: "tag", label: "Tag name" }, { key: "value", label: "Value" }, { key: "resources", label: "Resources", render: (r) => r.resources.map((n) => `<span class="chip">${esc(n)}</span>`).join(""), csv: (r) => r.resources.join("; ") }, { key: "count", label: "Count", num: true, render: (r) => r.resources.length, csv: (r) => r.resources.length }] });
  sumGrid.style.display = "none";
  $$("button", tabs).forEach((b, i) => (b.onclick = () => { $$("button", tabs).forEach((x) => x.classList.remove("active")); b.classList.add("active"); resGrid.style.display = i ? "none" : ""; sumGrid.style.display = i ? "" : "none"; }));
  mount(page("Tags", "Team and cost-centre tags on Foundry, project, gateway and resource-group scopes. Use them to group spend in Cost analysis and to filter budgets.",
    cmdbar([{ icon: "⟳", label: "Refresh", run: route }, "|", { icon: "$", label: "Cost by team tag", run: () => (location.hash = "#/cost") }]), tabs, resGrid, sumGrid));
}

// ============================================================ Quota & deployments
async function pageQuota() {
  const [quota, deps] = await Promise.all([api("/api/quota"), api("/api/deployments")]);
  const qChart = chartCard("Regional quota use (thousands of TPM)", "quotaChart", { name: "quota" });
  const setCap = (d) => openForm({
    title: `Change capacity – ${d.name}`,
    extraNote: `Capacity is this deployment's share of the regional quota, in thousands of tokens per minute for Standard deployments. Current: <b>${fmt.num(d.capacity)}</b> (${fmt.compact(d.tokens_per_minute)} TPM).`,
    fields: [{ key: "capacity", label: "New capacity", type: "number", required: true, value: d.capacity }],
    submitLabel: "Apply",
    onSubmit: async (v) => { await api(`/api/deployments/${encodeURIComponent(d.name)}/capacity`, { method: "PUT", body: v }); toast(`Capacity for ${d.name} set to ${v.capacity}`); route(); },
  });
  const depGrid = Grid({
    name: "deployments", rows: deps,
    cols: [{ key: "name", label: "Deployment", render: (r) => `<b>${esc(r.name)}</b>` }, { key: "model_name", label: "Model" }, { key: "model_version", label: "Version" },
      { key: "sku_name", label: "Deployment type" }, { key: "capacity", label: "Capacity", num: true }, { key: "tokens_per_minute", label: "TPM", num: true, render: (r) => fmt.compact(r.tokens_per_minute) },
      { key: "provisioning_state", label: "State", render: (r) => badgeFor(r.provisioning_state) }],
    actions: writable() ? [{ label: "Change capacity", run: setCap }] : null,
  });
  const qGrid = Grid({ name: "quota-usage", rows: quota, cols: [{ key: "display_name", label: "Quota" }, { key: "current_value", label: "Used", num: true }, { key: "limit", label: "Limit", num: true }, { key: "available", label: "Available", num: true }, { key: "percent_used", label: "Used %", render: (r) => progress(r.percent_used) }] });
  mount(page("Quota & deployments", "Subscription quota per region and model, and each deployment's allocation. To raise a regional limit, submit a quota increase request.",
    cmdbar([{ icon: "⟳", label: "Refresh", run: route }]), qChart, h(`<h3>Quota</h3>`), qGrid, h(`<h3>Deployments</h3>`), depGrid));
  drawChart($("#quotaChart"), "barchart", { labels: quota.map((q) => q.display_name.replace("Tokens Per Minute (thousands) - ", "")), datasets: [{ label: "Used", data: quota.map((q) => q.current_value), color: PALETTE[0] }, { label: "Available", data: quota.map((q) => q.available), color: "#c8c6c4" }] }, { yFormat: fmt.compact });
}

// ============================================================ Alerts
async function pageAlerts() {
  const data = await api("/api/alerts");
  let tab = state.alertTab || "metric";
  const tabs = h(`<div class="subtabs"><button data-t="metric">Metric alerts</button><button data-t="log">Log search alerts</button><button data-t="action_groups">Action groups</button></div>`);
  const host = h(`<div></div>`);
  const metricForm = (e = {}) => openForm({
    title: e.name ? `Edit alert – ${e.name}` : "Create metric alert",
    fields: [
      { key: "name", label: "Alert rule name", required: true, value: e.name, readonly: !!e.name },
      { key: "description", label: "Description", value: e.description },
      { key: "metric_name", label: "Signal", type: "select", options: [["TokenTransaction", "Total tokens (TokenTransaction)"], ["ProcessedPromptTokens", "Input tokens"], ["GeneratedTokens", "Output tokens"], ["AzureOpenAIRequests", "Requests"], ["AzureOpenAITTLTInMS", "Time to last byte (ms)"]], value: e.metric_name || "TokenTransaction" },
      { key: "dimensions", label: "Deployments (comma-separated, empty = all)", value: (e.dimensions?.[0]?.values || []).join(", ") },
      { key: "time_aggregation", label: "Aggregation", type: "select", options: ["Total", "Average", "Maximum"], value: e.time_aggregation || "Total" },
      { key: "operator", label: "Operator", type: "select", options: ["GreaterThan", "GreaterThanOrEqual", "LessThan"], value: e.operator || "GreaterThan" },
      { key: "threshold", label: "Threshold", type: "number", required: true, value: e.threshold },
      { key: "window_size", label: "Look-back window", type: "select", options: [["PT5M", "5 minutes"], ["PT15M", "15 minutes"], ["PT1H", "1 hour"], ["PT6H", "6 hours"], ["P1D", "1 day"]], value: e.window_size || "PT1H" },
      { key: "evaluation_frequency", label: "Check every", type: "select", options: [["PT1M", "1 minute"], ["PT5M", "5 minutes"], ["PT15M", "15 minutes"], ["PT1H", "1 hour"]], value: e.evaluation_frequency || "PT15M" },
      { key: "severity", label: "Severity", type: "select", options: [[0, "0 – Critical"], [1, "1 – Error"], [2, "2 – Warning"], [3, "3 – Informational"], [4, "4 – Verbose"]], value: e.severity ?? 2 },
      { key: "action_group_ids", label: "Action group", type: "select", options: [["", "None"], ...data.action_groups.map((a) => [a.id || a.name, a.name])], value: e.action_group_ids?.[0] || "" },
      { key: "enabled", label: "Enabled", type: "checkbox", value: e.enabled ?? true },
    ],
    onSubmit: async (v) => {
      v.name = e.name || v.name;
      v.action_group_ids = v.action_group_ids ? [v.action_group_ids] : [];
      v.scopes = e.scopes;
      await api("/api/alerts/metric", { method: "POST", body: v }); toast(`Saved alert ${v.name}`); state.alertTab = "metric"; route();
    },
  });
  const del = (kind) => (r) => confirmDelete(`${kind.replace("_", " ")} ${r.name}`, () => api(`/api/alerts/${kind}/${encodeURIComponent(r.name)}`, { method: "DELETE" }));
  const grids = {
    metric: () => Grid({ name: "metric-alerts", rows: data.metric, empty: "No metric alerts.", cols: [
      { key: "name", label: "Name", render: (r) => `<b>${esc(r.name)}</b><br><small style="color:var(--muted)">${esc(r.description || "")}</small>` },
      { key: "metric_name", label: "Signal" },
      { key: "threshold", label: "Condition", render: (r) => `${esc(r.time_aggregation)} ${esc(r.operator)} ${fmt.compact(r.threshold)} over ${esc(r.window_size)}`, csv: (r) => `${r.operator} ${r.threshold}` },
      { key: "dimensions", label: "Dimensions", render: (r) => (r.dimensions || []).map((d) => `<span class="chip">${esc(d.name)}: ${esc((d.values || []).join(", "))}</span>`).join("") || "All" },
      { key: "severity", label: "Sev", num: true }, { key: "enabled", label: "Enabled", render: (r) => badgeFor(r.enabled ? "On track" : "Disabled").replace("On track", "Enabled") }],
      actions: writable() ? [{ label: "Edit", run: (r) => metricForm(r) }, { label: "Delete", danger: true, run: del("metric") }] : null }),
    log: () => Grid({ name: "log-alerts", rows: data.log, empty: "No log search alerts.", cols: [
      { key: "name", label: "Name", render: (r) => `<b>${esc(r.name)}</b><br><small style="color:var(--muted)">${esc(r.description || "")}</small>` },
      { key: "query", label: "Query", render: (r) => `<code style="font-size:11px">${esc(r.query)}</code>` },
      { key: "threshold", label: "Condition", render: (r) => `${esc(r.time_aggregation)} ${esc(r.operator)} ${fmt.compact(r.threshold)}` },
      { key: "window_size", label: "Window" }, { key: "severity", label: "Sev", num: true }],
      actions: writable() ? [{ label: "Open in Logs", run: (r) => { localStorage.setItem(`finops.logs.tabs.${state.info.mode}`, JSON.stringify([{ name: r.name, text: r.query }])); location.hash = "#/logs"; } }, { label: "Delete", danger: true, run: del("log") }] : null }),
    action_groups: () => Grid({ name: "action-groups", rows: data.action_groups, cols: [
      { key: "name", label: "Name" }, { key: "short_name", label: "Short name" },
      { key: "emails", label: "Email receivers", render: (r) => chips(r.emails) }, { key: "webhooks", label: "Webhooks", render: (r) => chips(r.webhooks) }],
      actions: writable() ? [{ label: "Delete", danger: true, run: del("action_group") }] : null }),
  };
  const show = () => {
    $$("button", tabs).forEach((b) => b.classList.toggle("active", b.dataset.t === tab));
    host.innerHTML = ""; host.appendChild(grids[tab]());
  };
  $$("button", tabs).forEach((b) => (b.onclick = () => { tab = state.alertTab = b.dataset.t; show(); }));
  mount(page("Alerts", "Azure Monitor alert rules on token metrics and KQL, plus the action groups they notify. Real-time cost-control alerts go to Event Grid instead.",
    cmdbar([{ icon: "＋", label: "Create metric alert", run: () => metricForm(), disabled: !writable() }, { icon: "⟳", label: "Refresh", run: route }]), tabs, host));
  show();
}

// ============================================================ AI gateway (APIM)
async function pageGateway() {
  const data = await api("/api/apim");
  let tab = state.gwTab || "products";
  const tabs = h(`<div class="subtabs"><button data-t="products">Products (teams)</button><button data-t="subscriptions">Subscriptions</button><button data-t="limits">Token limits</button></div>`);
  const host = h(`<div></div>`);
  const productForm = (e = {}) => openForm({
    title: e.name ? `Edit product – ${e.name}` : "Create product",
    extraNote: "A product per team gives each team its own subscription keys, token limits and chargeback dimension.",
    fields: [
      { key: "name", label: "Product ID", required: true, value: e.name, readonly: !!e.name },
      { key: "display_name", label: "Display name", required: true, value: e.display_name },
      { key: "description", label: "Description", type: "textarea", rows: 2, value: e.description },
      { key: "state", label: "State", type: "select", options: ["published", "notPublished"], value: e.state || "published" },
      { key: "subscription_required", label: "Requires subscription", type: "checkbox", value: e.subscription_required ?? true },
      { key: "approval_required", label: "Requires approval", type: "checkbox", value: e.approval_required ?? false },
    ],
    onSubmit: async (v) => { v.name = e.name || v.name; await api("/api/apim/products", { method: "POST", body: v }); toast(`Saved product ${v.name}`); state.gwTab = "products"; route(); },
  });
  const subForm = (e = {}) => openForm({
    title: e.name ? `Edit subscription – ${e.name}` : "Create subscription",
    extraNote: "Keys aren't shown here. Retrieve them in the Azure portal or with the SDK's <code>list_subscription_secrets()</code> and store them in Key Vault.",
    fields: [
      { key: "name", label: "Subscription ID", required: true, value: e.name, readonly: !!e.name },
      { key: "display_name", label: "Display name", required: true, value: e.display_name },
      { key: "scope", label: "Scope", type: "select", options: data.products.map((p) => [`/products/${p.name}`, `Product: ${p.display_name || p.name}`]).concat([["/apis", "All APIs"]]), value: e.scope },
      { key: "state", label: "State", type: "select", options: ["active", "suspended", "cancelled"], value: e.state || "active" },
    ],
    onSubmit: async (v) => { v.name = e.name || v.name; await api("/api/apim/subscriptions", { method: "POST", body: v }); toast(`Saved subscription ${v.name}`); state.gwTab = "subscriptions"; route(); },
  });
  const limitForm = (e = {}) => openForm({
    title: e.scope ? `Token limit – ${e.scope}` : "Add token limit",
    extraNote: "Writes an <code>&lt;llm-token-limit&gt;</code> policy. Over the TPM limit callers get HTTP 429; over the quota they get HTTP 403.",
    fields: [
      { key: "scope", label: "Scope", type: "select", options: [["global", "All APIs (global)"], ...data.products.map((p) => [`product:${p.name}`, `Product: ${p.display_name || p.name}`])], value: e.scope || `product:${data.products[0]?.name || ""}`, readonly: !!e.scope },
      { key: "counter_key", label: "Counter key", value: e.counter_key || "@(context.Subscription.Id)", help: "Policy expression – one counter per value (e.g. subscription, team header, IP)." },
      { key: "tokens_per_minute", label: "Tokens per minute", type: "number", value: e.tokens_per_minute ?? "" },
      { key: "token_quota", label: "Token quota", type: "number", value: e.token_quota ?? "" },
      { key: "token_quota_period", label: "Quota period", type: "select", options: [["", "–"], "Hourly", "Daily", "Weekly", "Monthly", "Yearly"], value: e.token_quota_period || "" },
      { key: "estimate_prompt_tokens", label: "Estimate prompt tokens before sending", type: "checkbox", value: e.estimate_prompt_tokens ?? false },
    ],
    onSubmit: async (v) => { v.scope = e.scope || v.scope; await api("/api/apim/token-limits", { method: "PUT", body: v }); toast(`Token limit saved on ${v.scope}`); state.gwTab = "limits"; route(); },
  });
  const grids = {
    products: () => Grid({ name: "apim-products", rows: data.products, empty: "No products.", cols: [
      { key: "name", label: "ID", render: (r) => `<b>${esc(r.name)}</b>` }, { key: "display_name", label: "Name" }, { key: "description", label: "Description" },
      { key: "state", label: "State", render: (r) => badgeFor(r.state) }, { key: "approval_required", label: "Approval", render: (r) => (r.approval_required ? "Required" : "–") }],
      actions: writable() ? [{ label: "Edit", run: (r) => productForm(r) }, { label: "Delete", danger: true, run: (r) => confirmDelete(`product ${r.name} and its subscriptions`, () => api(`/api/apim/products/${encodeURIComponent(r.name)}`, { method: "DELETE" })) }] : null }),
    subscriptions: () => Grid({ name: "apim-subscriptions", rows: data.subscriptions, empty: "No subscriptions.", cols: [
      { key: "name", label: "ID", render: (r) => `<b>${esc(r.name)}</b>` }, { key: "display_name", label: "Name" }, { key: "scope", label: "Scope" }, { key: "state", label: "State", render: (r) => badgeFor(r.state) }],
      actions: writable() ? [{ label: "Edit", run: (r) => subForm(r) }, { label: "Delete", danger: true, run: (r) => confirmDelete(`subscription ${r.name}`, () => api(`/api/apim/subscriptions/${encodeURIComponent(r.name)}`, { method: "DELETE" })) }] : null }),
    limits: () => Grid({ name: "apim-token-limits", rows: data.token_limits, empty: "No token-limit policies found.", cols: [
      { key: "scope", label: "Scope", render: (r) => `<b>${esc(r.scope)}</b>` }, { key: "counter_key", label: "Counter key", render: (r) => `<code>${esc(r.counter_key)}</code>` },
      { key: "tokens_per_minute", label: "Tokens / minute", num: true, render: (r) => fmt.compact(r.tokens_per_minute) },
      { key: "token_quota", label: "Quota", num: true, render: (r) => (r.token_quota ? `${fmt.compact(r.token_quota)} / ${esc((r.token_quota_period || "").toLowerCase())}` : "–") },
      { key: "estimate_prompt_tokens", label: "Estimate prompt", render: (r) => (r.estimate_prompt_tokens ? "Yes" : "No") }],
      actions: writable() ? [{ label: "Edit", run: (r) => limitForm(r) }, { label: "Remove", danger: true, run: (r) => confirmDelete(`token limit on ${r.scope}`, () => api(`/api/apim/token-limits?scope=${encodeURIComponent(r.scope)}`, { method: "DELETE" })) }] : null }),
  };
  const create = { products: () => productForm(), subscriptions: () => subForm(), limits: () => limitForm() };
  const show = () => { $$("button", tabs).forEach((b) => b.classList.toggle("active", b.dataset.t === tab)); host.innerHTML = ""; host.appendChild(grids[tab]()); };
  $$("button", tabs).forEach((b) => (b.onclick = () => { tab = state.gwTab = b.dataset.t; show(); }));
  mount(page("AI gateway (API Management)", data.note ? esc(data.note) : "Per-team products and subscriptions, and the <code>llm-token-limit</code> policies that cap tokens per minute and per period. Pair with <code>llm-emit-token-metric</code> for per-team usage in Logs.",
    cmdbar([{ icon: "＋", label: "Create", run: () => create[tab](), disabled: !writable() }, { icon: "⟳", label: "Refresh", run: route }]), tabs, host));
  show();
}

// ============================================================ router & boot
const PAGES = { overview: pageOverview, logs: pageLogs, usage: pageUsage, cost: pageCost, "cost-controls": pageCostControls, budgets: pageBudgets, alerts: pageAlerts, tags: pageTags, quota: pageQuota, gateway: pageGateway };
function mount(node) { const v = $("#view"); v.innerHTML = ""; v.appendChild(node); }
async function route() {
  const key = (location.hash.replace(/^#\//, "") || "overview").split("?")[0];
  const fn = PAGES[key] || pageOverview;
  $$(".blade-menu a").forEach((a) => a.classList.toggle("active", a.dataset.page === key));
  $("#crumbPage").textContent = $(`.blade-menu a[data-page="${key}"]`)?.textContent.replace(/^.\s*/, "").trim() || "Overview";
  state.charts.forEach((c) => c.destroy()); state.charts.clear();
  $("#view").innerHTML = `<div class="empty">Loading…</div>`;
  try { await fn(); } catch (e) { $("#view").innerHTML = ""; mount(page("Something went wrong", "", h(`<div class="note err">${esc(e.message)}</div>`))); }
  $("#view").focus({ preventScroll: true });
}
function loadLocalChartJs() {
  // CDN blocked or offline? Fall back to static/vendor/chart.umd.min.js if you've placed a copy there.
  if (window.Chart) return Promise.resolve();
  return new Promise((resolve) => {
    const s = Object.assign(document.createElement("script"), { src: "vendor/chart.umd.min.js" });
    s.onload = s.onerror = () => resolve();
    document.head.appendChild(s);
  });
}
async function boot() {
  state.projects = [];
  await loadLocalChartJs();
  try { state.info = await api("/api/info"); } catch (e) { state.info = { mode: "error" }; }
  const badge = $("#modeBadge");
  badge.textContent = state.info.mode === "demo" ? "Demo data" : state.info.mode === "live" ? "Live" : "Offline";
  badge.className = `mode ${state.info.mode}`;
  $("#crumbResource").textContent = state.info.foundry_account || state.info.resource_group || "Foundry";
  $("#whoami").textContent = [state.info.resource_group, state.info.read_only ? "read-only" : ""].filter(Boolean).join(" · ");
  $("#navToggle").onclick = () => $("#menu").classList.toggle("hidden");
  const filterMenu = (q) => $$(".blade-menu a").forEach((a) => (a.style.display = a.textContent.toLowerCase().includes(q.toLowerCase()) ? "" : "none"));
  $("#menuFilter").oninput = (e) => filterMenu(e.target.value);
  $("#globalSearch").addEventListener("keydown", (e) => { if (e.key === "Enter") { const m = $$(".blade-menu a").find((a) => a.textContent.toLowerCase().includes(e.target.value.toLowerCase())); if (m) location.hash = m.getAttribute("href"); } });
  document.addEventListener("keydown", (e) => { if (e.ctrlKey && e.key === "/") { e.preventDefault(); $("#globalSearch").focus(); } if (e.key === "Escape") $("#flyout").classList.remove("open"); });
  window.addEventListener("hashchange", route);
  route();
}
document.addEventListener("DOMContentLoaded", boot);
