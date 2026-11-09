"use strict";
// All dynamic values are inserted with textContent (never innerHTML), so agent names and
// greeting text from a customer tenant cannot inject markup.

const view = document.getElementById("view");
const statusEl = document.getElementById("status");
let tenant = new URLSearchParams(location.search).get("tenant") || "";

const YES_NO = {
  used_in_workplace: "Used at the workplace on employees?", decisions_about_persons: "Used in decisions about individuals?",
  dpia_required: "Does a DPIA apply?", substantial_modification: "Substantially modified, rebranded or given a new purpose?",
  publishes_public_interest_content: "Publishes AI-generated text on matters of public interest?", iso42001: "In scope of ISO/IEC 42001?",
};
const RISK_TIERS = ["", "minimal", "limited", "high", "prohibited-concern"];
const PROFILE = {
  country: ["Country (two-letter code)", "text"], is_public_body: ["Public body?", "yesno"], provides_public_services: ["Provides public services?", "yesno"],
  credit_or_insurance_pricing: ["Credit scoring or life/health insurance pricing?", "yesno"], is_financial_entity: ["Financial entity (DORA)?", "yesno"],
  is_nis2_entity: ["Essential or important entity (NIS2)?", "yesno"], is_employer: ["Employer of the people using agents?", "yesno"],
  iso42001_in_scope: ["Pursuing ISO/IEC 42001?", "yesno"],
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
  const [agents, prof] = await Promise.all([api("GET", "/agents"), api("GET", "/profile")]);
  const banner = prof.complete ? null : h("div", { class: "card" }, "Organisation profile incomplete: ",
    h("a", { href: "#/settings" }, "complete it"), " so sector rules (DORA, NIS2, FRIA scope, Spain) can apply. Missing: ", prof.missing.join(", "), ".");
  if (!agents.length) return view.replaceChildren(banner, h("p", {}, "No agents yet. Choose \"Scan tenant\"."));
  view.replaceChildren(
    banner,
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
  const a = d.agent, f = a.facts, labels = d.inputLabels || {};
  const val = (k) => (f[k] && f[k].value != null && f[k].value !== "" ? (Array.isArray(f[k].value) ? f[k].value.map((x) => (typeof x === "object" ? x.name : x)).join(", ") : String(f[k].value)) : "");

  // human inputs needed by the current documents, keyed by subject when the document is per incident or per request
  const needed = new Map();
  d.documents.forEach((doc) => Object.entries(doc.fields).forEach(([n, fv]) => {
    if (fv.source !== "human") return;
    const key = doc.subject ? `${doc.subject}:${n}` : n;
    if (fv.pending || a.inputs[key]) needed.set(key, { label: (labels[n] || n.replace(/_/g, " ")) + (doc.subject ? ` (${doc.subject})` : ""), doc: doc.docType });
  }));
  const textKeys = [...needed.keys()].filter((k) => k !== "risk_tier");

  const groupByDoc = (keys) => {
    const groups = new Map();
    keys.forEach((k) => { const t = needed.get(k).doc; groups.set(t, [...(groups.get(t) || []), k]); });
    return [...groups.entries()].sort();
  };
  const select = (name, options, current) => h("select", { name, id: `in-${name}` }, options.map(([v, t]) => h("option", { value: v, selected: v === current }, t)));
  const control = [
    h("div", {}, h("label", { for: "in-risk_tier" }, "Confirmed risk tier"), select("risk_tier", RISK_TIERS.map((v) => [v, v || "not confirmed"]), (a.inputs.risk_tier || "").toLowerCase())),
    ...Object.entries(YES_NO).map(([k, label]) => h("div", {}, h("label", { for: `in-${k}` }, label), select(k, [["", "not answered"], ["yes", "yes"], ["no", "no"]], (a.inputs[k] || "").toLowerCase()))),
    h("div", {}, h("label", { for: "in-explanation_requests" }, "Article 86 explanation requests (comma-separated ids)"),
      h("input", { type: "text", id: "in-explanation_requests", name: "explanation_requests", maxlength: 2000, value: a.inputs.explanation_requests || "" })),
  ];
  const form = h("form", { onsubmit: async (e) => {
    e.preventDefault();
    const body = {};
    ["risk_tier", ...Object.keys(YES_NO), "explanation_requests"].forEach((n) => { body[n] = form.elements[n].value; });
    textKeys.forEach((n) => { if (n !== "explanation_requests") body[n] = form.elements[n].value; });
    try { say("Saving..."); await api("PATCH", `/agents/${encodeURIComponent(id)}/inputs`, body); say("Saved."); await agentView(id); refreshTaskCount(); }
    catch (err) { say(err.message, true); }
  } },
    h("h4", {}, "Scope and classification"), control,
    h("h4", {}, "Document inputs"),
    textKeys.length ? groupByDoc(textKeys.filter((n) => n !== "explanation_requests")).map(([docType, keys]) => h("details", {},
      h("summary", {}, `${docType} (${keys.filter((n) => !a.inputs[n]).length} missing)`),
      keys.map((n) => h("div", {}, h("label", { for: `in-${n}` }, needed.get(n).label),
        h("textarea", { id: `in-${n}`, name: n, rows: 2, maxlength: 2000 }, a.inputs[n] || ""))))) : h("p", { class: "muted" }, "No document inputs needed."),
    h("p", {}, h("button", { class: "primary", type: "submit" }, "Save inputs")));

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
      h("div", {}, h("strong", {}, "Possible Annex III areas (keyword match): "), val("suggestedAnnexIII") || "none"),
      h("div", { class: "muted" }, `Public-facing is a heuristic (confidence ${(f.isPublicFacing && f.isPublicFacing.confidence) || "n/a"}). Confirm with the agent owner.`)),
    h("h3", {}, "Duties"),
    h("table", {}, h("thead", {}, h("tr", {}, ["Regulation", "Article", "Subject", "Applies", "Result", "Counsel review"].map((x) => h("th", {}, x)))),
      h("tbody", {}, d.dutyResults.filter((r) => r.applicability === "applies").map((r) => h("tr", {}, h("td", {}, r.regulation), h("td", {}, r.article), h("td", {}, r.subject || ""),
        h("td", {}, "yes"), h("td", {}, pill(r.result)), h("td", {}, r.counselReviewed ? "signed off" : h("span", { class: "unknown" }, "pending")))))),
    h("h3", {}, "Inputs"), form,
    h("h3", {}, "Documents"),
    h("table", {}, h("thead", {}, h("tr", {}, ["Document", "Subject", "Lang", "Version", "State", "Auto-filled", "Files", "Actions"].map((x) => h("th", {}, x)))),
      h("tbody", {}, d.documents.map((doc) => h("tr", {},
        h("td", {}, doc.docType), h("td", {}, doc.subject || ""), h("td", {}, doc.language), h("td", {}, `v${doc.version}${doc.stale ? " (stale)" : ""}`),
        h("td", {}, doc.state), h("td", {}, `${Math.round(doc.autofillRatio * 100)}%`),
        h("td", {}, ["docx", "pdf"].filter((fmt) => doc.filePaths[fmt]).map((fmt) => h("a", { href: `/api/tenants/${encodeURIComponent(tenant)}/documents/${encodeURIComponent(doc.id)}/file?format=${fmt}`, style: "margin-right:8px" }, fmt))),
        h("td", { class: "row" }, (actions[doc.state] || []).map((x) => transition(doc, x))))))));
}

async function tasksView() {
  const tasks = await api("GET", "/tasks");
  view.replaceChildren(h("h2", {}, "Open tasks"),
    tasks.length ? h("table", {}, h("tbody", {}, tasks.map((t) => h("tr", {}, h("td", {}, t.title), h("td", {}, h("a", { href: `#/agent/${encodeURIComponent(t.agentId)}` }, "Open agent"))))))
      : h("p", {}, "No open tasks."));
}

async function settingsView() {
  const prof = await api("GET", "/profile");
  const defaults = h("form", { onsubmit: async (e) => {
    e.preventDefault();
    try { await api("PATCH", "/defaults", { controller_name: defaults.elements.controller_name.value, controller_contact: defaults.elements.controller_contact.value }); say("Saved."); }
    catch (err) { say(err.message, true); }
  } },
    h("label", { for: "cn" }, "Controller name"), h("input", { id: "cn", name: "controller_name", type: "text", maxlength: 2000 }),
    h("label", { for: "cc" }, "Controller contact (DPO)"), h("input", { id: "cc", name: "controller_contact", type: "text", maxlength: 2000 }),
    h("p", {}, h("button", { class: "primary", type: "submit" }, "Save")));
  const profile = h("form", { onsubmit: async (e) => {
    e.preventDefault();
    const body = {};
    Object.keys(PROFILE).forEach((k) => { body[k] = profile.elements[k].value; });
    try { await api("PATCH", "/profile", body); say("Saved."); await settingsView(); } catch (err) { say(err.message, true); }
  } },
    Object.entries(PROFILE).map(([k, [label, kind]]) => h("div", {}, h("label", { for: `p-${k}` }, label),
      kind === "yesno"
        ? h("select", { id: `p-${k}`, name: k }, [["", "not answered"], ["yes", "yes"], ["no", "no"]].map(([v, t]) => h("option", { value: v, selected: v === (prof.profile[k] || "").toLowerCase() }, t)))
        : h("input", { id: `p-${k}`, name: k, type: "text", maxlength: 2, value: prof.profile[k] || "" }))),
    h("p", {}, h("button", { class: "primary", type: "submit" }, "Save profile")));
  view.replaceChildren(h("h2", {}, "Organisation defaults"), h("p", { class: "muted" }, "Used in every record-of-processing entry."), defaults,
    h("h2", {}, "Organisation profile"), h("p", { class: "muted" }, "Decides which regulations apply to your agents."), profile);
}

async function auditView() {
  const pack = await api("GET", "/audit");
  const fileInput = h("input", { type: "file", id: "cm-file", accept: ".xlsx" });
  const download = async () => {
    try {
      const f = fileInput.files[0];
      const r = await fetch(`/api/tenants/${encodeURIComponent(tenant)}/export`, { method: "POST", body: f || undefined });
      if (!r.ok) throw new Error((await r.json().catch(() => ({}))).error || `HTTP ${r.status}`);
      const url = URL.createObjectURL(await r.blob());
      const link = h("a", { href: url, download: "eu-ai-action-evidence.zip" });
      document.body.append(link); link.click(); link.remove(); URL.revokeObjectURL(url);
      say("Bundle downloaded.");
    } catch (err) { say(err.message, true); }
  };
  const verify = (doc) => h("button", { type: "button", onclick: async (ev) => {
    try { const v = await api("GET", `/documents/${encodeURIComponent(doc.id)}/verify`); ev.target.replaceWith(h("span", { class: v.intact ? "pass" : "fail" }, v.intact ? "intact" : "CHANGED")); }
    catch (err) { say(err.message, true); }
  } }, "verify");
  view.replaceChildren(h("h2", {}, "Audit"),
    h("p", { class: "muted" }, `Rule set ${pack.ruleSetVersion}. Approved documents only; a changed file fails verification.`),
    h("div", { class: "card" }, h("strong", {}, "Evidence bundle"), h("p", { class: "muted" }, "Optionally choose the Excel file exported from Compliance Manager; its status and notes columns are filled in. Column names are unverified: check the result before uploading."),
      h("div", { class: "row" }, fileInput, h("button", { type: "button", class: "primary", onclick: download }, "Download bundle"))),
    pack.documents.length ? h("table", {}, h("thead", {}, h("tr", {}, ["Document", "Lang", "Version", "Approved by", "Approved at", "Counsel", "Hash", ""].map((x) => h("th", {}, x)))),
      h("tbody", {}, pack.documents.map((d) => h("tr", {}, h("td", {}, d.id), h("td", {}, d.language), h("td", {}, `v${d.version}`), h("td", {}, d.approvedBy), h("td", {}, d.approvedAt),
        h("td", {}, d.counselReviewed ? "signed off" : "pending"), h("td", {}, d.fileHash), h("td", {}, verify(d)))))) : h("p", {}, "No approved documents yet."),
    h("h3", {}, "Log"),
    h("table", {}, h("tbody", {}, pack.auditLog.map((a) => h("tr", {}, h("td", {}, a.at), h("td", {}, a.user), h("td", {}, a.action), h("td", {}, a.documentId))))));
}

async function route() {
  say("");
  const hash = location.hash || "#/";
  try {
    if (!tenant) return view.replaceChildren(h("p", {}, "Tenant unknown. Sign in, or add ?tenant=<id> to the address."));
    if (hash.startsWith("#/agent/")) await agentView(decodeURIComponent(hash.slice(8)));
    else if (hash === "#/tasks") await tasksView();
    else if (hash === "#/settings") await settingsView();
    else if (hash === "#/audit") await auditView();
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
