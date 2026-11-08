"use strict";
// All dynamic values are inserted with textContent (never innerHTML), so agent names and
// greeting text from a customer tenant cannot inject markup.

const view = document.getElementById("view");
const statusEl = document.getElementById("status");
let tenant = new URLSearchParams(location.search).get("tenant") || "";

const INPUT_LABELS = {
  business_purpose: "Business purpose", intended_users: "Intended users", processing_purposes: "Purposes of the processing",
  data_subjects: "Categories of data subjects", recipients: "Recipients", third_country_transfers: "Transfers to third countries",
  retention_period: "Retention period",
};

function h(tag, attrs, ...kids) {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (k === "class") e.className = v;
    else if (k.startsWith("on")) e.addEventListener(k.slice(2), v);
    else if (v !== false && v != null) e.setAttribute(k, v === true ? "" : v);
  }
  for (const kid of kids.flat()) if (kid != null) e.append(kid.nodeType ? kid : document.createTextNode(String(kid)));
  return e;
}

function say(msg, error) { statusEl.textContent = msg || ""; statusEl.className = error ? "error" : ""; }

async function api(method, path, body) {
  const r = await fetch(`/api/tenants/${encodeURIComponent(tenant)}${path}`, {
    method, headers: body ? { "Content-Type": "application/json" } : {}, body: body ? JSON.stringify(body) : undefined,
  });
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.error || `HTTP ${r.status}`);
  return data;
}

async function resolveTenant() {
  if (tenant) return;
  try {
    const me = await (await fetch("/.auth/me")).json();
    const claims = (me.clientPrincipal && me.clientPrincipal.claims) || [];
    const t = claims.find((c) => c.typ === "tid" || c.typ.endsWith("/tenantid"));
    tenant = t ? t.val : "";
  } catch { /* not behind Static Web Apps */ }
}

const pill = (r) => h("span", { class: `pill ${r}` }, r === "none" ? "n/a" : r);

async function refreshTaskCount() {
  try {
    const t = await api("GET", "/tasks");
    const b = document.getElementById("task-count");
    b.textContent = t.length; b.hidden = t.length === 0;
  } catch { /* ignore */ }
}

async function agentsView() {
  const agents = await api("GET", "/agents");
  if (!agents.length) return view.replaceChildren(h("p", {}, "No agents yet. Choose \"Scan tenant\"."));
  view.replaceChildren(
    h("h2", {}, "Agents"),
    h("table", {},
      h("thead", {}, h("tr", {}, ["Agent", "Owner", "Public-facing", "Duties met", "Pass", "Fail", "Unknown"].map((x) => h("th", {}, x)))),
      h("tbody", {}, agents.map((a) => h("tr", {},
        h("td", {}, h("a", { href: `#/agent/${encodeURIComponent(a.agentId)}` }, a.name)),
        h("td", {}, a.owner || h("span", { class: "unknown" }, "missing")),
        h("td", {}, a.isPublicFacing === null ? "unknown" : a.isPublicFacing ? "yes" : "no"),
        h("td", {}, `${a.readiness.satisfied} of ${a.readiness.applicable}`),
        h("td", { class: "pass" }, a.readiness.pass), h("td", { class: "fail" }, a.readiness.fail), h("td", { class: "unknown" }, a.readiness.unknown),
      )))));
}

async function agentView(id) {
  const d = await api("GET", `/agents/${encodeURIComponent(id)}`);
  const a = d.agent, f = a.facts;
  const val = (k) => (f[k] && f[k].value != null && f[k].value !== "" ? (Array.isArray(f[k].value) ? f[k].value.map((x) => (typeof x === "object" ? x.name : x)).join(", ") : String(f[k].value)) : "");
  const needed = new Set();
  d.documents.forEach((doc) => Object.entries(doc.fields).forEach(([n, fv]) => { if (fv.source === "human" && fv.pending) needed.add(n); }));
  const allInputs = Object.keys(INPUT_LABELS).filter((n) => needed.has(n) || a.inputs[n]);

  const form = h("form", { onsubmit: async (e) => {
    e.preventDefault();
    const body = {};
    allInputs.forEach((n) => { body[n] = form.elements[n].value; });
    try { say("Saving..."); await api("PATCH", `/agents/${encodeURIComponent(id)}/inputs`, body); say("Saved."); await agentView(id); refreshTaskCount(); }
    catch (err) { say(err.message, true); }
  } },
    allInputs.map((n) => h("div", {}, h("label", { for: `in-${n}` }, INPUT_LABELS[n]),
      h("textarea", { id: `in-${n}`, name: n, rows: 2, maxlength: 2000 }, a.inputs[n] || ""))),
    allInputs.length ? h("p", {}, h("button", { class: "primary", type: "submit" }, "Save inputs")) : h("p", { class: "muted" }, "No inputs needed."));

  const transition = (doc, action) => h("button", { type: "button", onclick: async () => {
    try { await api("POST", `/documents/${encodeURIComponent(doc.id)}/transition`, { action }); say(`${action} done.`); await agentView(id); }
    catch (err) { say(err.message, true); }
  } }, action);
  const actions = { draft: ["submit"], in_review: ["approve", "reject"], rejected: ["reopen"], approved: [], superseded: [] };

  view.replaceChildren(
    h("p", {}, h("a", { href: "#/" }, "< All agents")),
    h("h2", {}, a.name),
    h("div", { class: "card" },
      h("div", {}, h("strong", {}, "Owner: "), val("owner") || "missing", "   ", h("strong", {}, "Sponsor: "), val("sponsor") || "missing"),
      h("div", {}, h("strong", {}, "Channels: "), val("channels") || "unknown", "   ", h("strong", {}, "Authentication: "), val("authMode") || "unknown"),
      h("div", {}, h("strong", {}, "Opening message: "), val("greetingText") || h("span", { class: "unknown" }, "not found")),
      h("div", { class: "muted" }, `Public-facing is a heuristic (confidence ${(f.isPublicFacing && f.isPublicFacing.confidence) || "n/a"}). Confirm with the agent owner.`)),
    h("h3", {}, "Duties"),
    h("table", {}, h("thead", {}, h("tr", {}, ["Regulation", "Article", "Applies", "Result", "Counsel review"].map((x) => h("th", {}, x)))),
      h("tbody", {}, d.dutyResults.map((r) => h("tr", {}, h("td", {}, r.regulation), h("td", {}, r.article),
        h("td", {}, r.applicability === "applies" ? "yes" : "no"), h("td", {}, pill(r.result)), h("td", {}, r.counselReviewed ? "signed off" : h("span", { class: "unknown" }, "pending")))))),
    h("h3", {}, "Inputs"), form,
    h("h3", {}, "Documents"),
    h("table", {}, h("thead", {}, h("tr", {}, ["Document", "Lang", "Version", "State", "Auto-filled", "Files", "Actions"].map((x) => h("th", {}, x)))),
      h("tbody", {}, d.documents.map((doc) => h("tr", {},
        h("td", {}, doc.docType), h("td", {}, doc.language), h("td", {}, `v${doc.version}${doc.stale ? " (stale)" : ""}`),
        h("td", {}, doc.state), h("td", {}, `${Math.round(doc.autofillRatio * 100)}%`),
        h("td", {}, ["docx", "pdf"].map((fmt) => h("a", { href: `/api/tenants/${encodeURIComponent(tenant)}/documents/${encodeURIComponent(doc.id)}/file?format=${fmt}`, style: "margin-right:8px" }, fmt))),
        h("td", { class: "row" }, (actions[doc.state] || []).map((x) => transition(doc, x))))))));
}

async function tasksView() {
  const tasks = await api("GET", "/tasks");
  view.replaceChildren(h("h2", {}, "Open tasks"),
    tasks.length ? h("table", {}, h("tbody", {}, tasks.map((t) => h("tr", {}, h("td", {}, t.title), h("td", {}, h("a", { href: `#/agent/${encodeURIComponent(t.agentId)}` }, "Open agent"))))))
      : h("p", {}, "No open tasks."));
}

async function settingsView() {
  const form = h("form", { onsubmit: async (e) => {
    e.preventDefault();
    try { await api("PATCH", "/defaults", { controller_name: form.elements.controller_name.value, controller_contact: form.elements.controller_contact.value }); say("Saved."); }
    catch (err) { say(err.message, true); }
  } },
    h("label", { for: "cn" }, "Controller name"), h("input", { id: "cn", name: "controller_name", type: "text", maxlength: 2000 }),
    h("label", { for: "cc" }, "Controller contact (DPO)"), h("input", { id: "cc", name: "controller_contact", type: "text", maxlength: 2000 }),
    h("p", {}, h("button", { class: "primary", type: "submit" }, "Save")));
  view.replaceChildren(h("h2", {}, "Organisation defaults"), h("p", { class: "muted" }, "Used in every record-of-processing entry."), form);
}

async function route() {
  say("");
  const hash = location.hash || "#/";
  try {
    if (!tenant) return view.replaceChildren(h("p", {}, "Tenant unknown. Sign in, or add ?tenant=<id> to the address."));
    if (hash.startsWith("#/agent/")) await agentView(decodeURIComponent(hash.slice(8)));
    else if (hash === "#/tasks") await tasksView();
    else if (hash === "#/settings") await settingsView();
    else await agentsView();
    refreshTaskCount();
  } catch (err) { say(err.message, true); }
}

document.getElementById("scan").addEventListener("click", async (ev) => {
  ev.target.disabled = true; say("Scanning...");
  try { const s = await api("POST", "/scans"); say(`Scan ${s.status || "started"}.`); await route(); }
  catch (err) { say(err.message, true); }
  finally { ev.target.disabled = false; }
});
window.addEventListener("hashchange", route);
resolveTenant().then(route);
