"""Evidence -> AgentFacts. Every fact carries its source and evidence id (provenance)."""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

from ..models import AgentFacts, AgentRef, Evidence, FactValue
from .disclosure import detect_disclosure

INTERNAL_CHANNELS = {"MsTeams", "M365Copilot", "Outlook", "SharePoint"}
AUTH_MODES = {1: "none", 2: "microsoft", 3: "custom"}
LCID = {1033: "en", 2057: "en", 3082: "es", 1034: "es"}
INSTRUCTIONS_LIMIT = 2000
_CATS = Path(__file__).parent / "data" / "categories.yaml"
_ANNEX = Path(__file__).parent / "data" / "annex_iii.yaml"
MIN_RETENTION_DAYS = 183  # "at least six months" (Art. 26(6)); counsel to confirm the day count


@lru_cache
def _categories() -> dict[str, list[str]]:
    return yaml.safe_load(_CATS.read_text(encoding="utf-8"))


@lru_cache
def _annex_iii() -> dict[str, list[str]]:
    return yaml.safe_load(_ANNEX.read_text(encoding="utf-8"))


def _parse(content: Any) -> dict:
    if not content or not isinstance(content, str):
        return {}
    try:
        d = yaml.safe_load(content)
    except yaml.YAMLError:
        return {}
    return d if isinstance(d, dict) else {}


def _activity_texts(node: Any) -> list[str]:
    """Collect text of SendActivity actions anywhere in a dialog definition."""
    out: list[str] = []
    if isinstance(node, dict):
        if node.get("kind") == "SendActivity":
            act = node.get("activity")
            if isinstance(act, str):
                out.append(act)
            elif isinstance(act, dict):
                t = act.get("text")
                if isinstance(t, str):
                    out.append(t)
                elif isinstance(t, list):
                    out.extend(x for x in t if isinstance(x, str))
        for v in node.values():
            out.extend(_activity_texts(v))
    elif isinstance(node, list):
        for v in node:
            out.extend(_activity_texts(v))
    return out


def _is_greeting_dialog(d: dict) -> bool:
    begin = d.get("beginDialog")
    return isinstance(begin, dict) and begin.get("kind") == "OnConversationStart"


def extract_facts(tenant_id: str, agent: AgentRef, evidence: list[Evidence], scan_id: str) -> AgentFacts:
    by_source = {e.source: e for e in evidence}
    ent, ret, fdy, dfn, lrn = (by_source.get(k) for k in ("entra", "retention", "foundry", "defender", "learning"))
    dv = by_source.get("dataverse")
    reg = by_source.get("registry")
    pv = by_source.get("purview")
    facts: dict[str, FactValue] = {}

    def put(key: str, value: Any, ev: Evidence | None, confidence: float = 1.0, note: str = "") -> None:
        facts[key] = FactValue(
            value=value, source=ev.source if ev else None, evidence_id=ev.id if ev else None, confidence=confidence, note=note
        )

    bot = dv.payload.get("bot", {}) if dv else {}
    components = dv.payload.get("components", []) if dv else []
    parsed = [(c, _parse(c.get("content"))) for c in components]

    name = bot.get("name") or agent.name or agent.agent_id
    put("name", name, dv)
    put("platform", "Microsoft Copilot Studio" if dv else None, dv)
    put("createdAt", bot.get("createdon"), dv)
    put("modifiedAt", bot.get("modifiedon"), dv)
    put("language", LCID.get(bot.get("language")), dv)

    # owner and sponsor: registry first, then Dataverse owner
    owner = (reg.payload.get("owner") or {}).get("displayName") if reg else None
    if owner:
        put("owner", owner, reg)
    elif bot.get("ownername"):
        put("owner", bot["ownername"], dv, confidence=0.8, note="Dataverse record owner, not an Entra sponsor")
    else:
        put("owner", None, None)
    put("sponsor", ((reg.payload.get("sponsor") or {}).get("displayName") if reg else None), reg if reg else None)
    last = (reg.payload.get("lastActivity") if reg else None) or (pv.payload.get("lastActivity") if pv else None)
    put("lastActivity", last, reg or pv)

    # access and channels
    auth = AUTH_MODES.get(bot.get("authenticationmode"))
    put("authMode", auth, dv)
    channels = list(dv.payload.get("channels", [])) if dv else []
    put("channels", channels, dv)
    public = auth == "none" or any(c not in INTERNAL_CHANNELS for c in channels)
    known = auth is not None or bool(channels)
    put(
        "isPublicFacing",
        public if known else None,
        dv,
        confidence=0.7,
        note="Heuristic: no authentication or a channel outside Teams/Microsoft 365. Confirm with the agent owner.",
    )

    # greeting and AI disclosure
    greeting: list[str] = []
    for _c, d in parsed:
        if _is_greeting_dialog(d):
            greeting.extend(_activity_texts(d))
    greeting_text = " ".join(t.strip() for t in greeting if t.strip()) or None
    put("greetingText", greeting_text, dv)
    strength, matched = detect_disclosure(greeting_text or "")
    put("aiDisclosureStrength", strength if greeting_text else None, dv)
    put("hasAiDisclosure", (strength == "strong") if greeting_text else None, dv)
    put("disclosureMatch", matched or None, dv)

    # instructions, knowledge, connectors
    instructions = None
    knowledge: list[dict[str, str]] = []
    connectors: list[str] = []
    for c, d in parsed:
        kind = d.get("kind")
        if kind == "GptComponentMetadata" and isinstance(d.get("instructions"), str):
            instructions = d["instructions"][:INSTRUCTIONS_LIMIT]
        elif kind == "KnowledgeSourceConfiguration":
            src = d.get("source") or {}
            knowledge.append(
                {"type": str(src.get("kind", "unknown")), "name": str(c.get("name") or src.get("site") or src.get("url") or "")}
            )
        elif kind in ("TaskDialog", "ConnectorAction"):
            connectors.append(str(c.get("name") or d.get("connector") or "connector"))
    put("instructions", instructions, dv)
    put("knowledgeSources", knowledge, dv)
    put("connectors", connectors, dv)

    # sensitivity and data categories
    labels = sorted({str(x.get("label")) for x in (pv.payload.get("labels", []) if pv else []) if x.get("label")})
    put("sensitivityLabels", labels, pv)
    haystack = " ".join([k["name"] for k in knowledge] + labels + [str(x.get("source", "")) for x in (pv.payload.get("labels", []) if pv else [])]).lower()
    cats = sorted(cat for cat, words in _categories().items() if any(w in haystack for w in words))
    put("dataCategories", cats, pv or dv, confidence=0.5, note="Keyword heuristic over source names and labels. DPO must confirm.")

    parts = [f"Authentication: {auth or 'unknown'}", f"Channels: {', '.join(channels) or 'unknown'}"]
    if labels:
        parts.append(f"Sensitivity labels on sources: {', '.join(labels)}")
    put("securityObserved", "; ".join(parts), dv)

    # ---- v1/v2 evidence (assumed response shapes, see tests/fixtures/agents/*.json) ----
    users = (ent.payload.get("users", []) if ent else [])
    put("accessUsers", [u.get("displayName", "") for u in users] if ent else None, ent)
    put("affectedGroups", [g.get("name", "") for g in ent.payload.get("accessGroups", [])] if ent else None, ent)
    put("overseers", [u.get("displayName", "") for u in users if u.get("role") == "overseer"] if ent else None, ent)
    completed = {c.get("user") for c in (lrn.payload.get("completions", []) if lrn else [])} | {
        u.get("displayName") for u in users if u.get("trainingCompleted")
    }
    if ent and (lrn or any("trainingCompleted" in u for u in users)):
        gaps = sorted(u.get("displayName", "") for u in users if u.get("displayName") not in completed)
        put("literacyGaps", gaps, lrn or ent)
        put("literacyComplete", not gaps, lrn or ent)
    else:
        put("literacyGaps", None, None)
        put("literacyComplete", None, None)
    if ret:
        days = {k: v for k, v in ret.payload.items() if isinstance(v, int)}
        put("retentionDays", days, ret)
        put("minRetentionDays", min(days.values()) if days else None, ret)
        put("retentionOk", (min(days.values()) >= MIN_RETENTION_DAYS) if days else None, ret)
    else:
        put("retentionDays", None, None)
        put("minRetentionDays", None, None)
        put("retentionOk", None, None)
    deployments = fdy.payload.get("deployments", []) if fdy else []
    put("modelDeployments", [f"{d.get('name', '')} ({d.get('provider', '')}, {d.get('region', '')})" for d in deployments] if fdy else None, fdy)
    put("modelProviders", sorted({d.get("provider", "") for d in deployments}) if fdy else None, fdy)
    put("fineTunedModel", any(d.get("fineTuned") for d in deployments) if fdy else None, fdy)
    put("generatesSyntheticContent", bool(fdy.payload.get("generatesContent")) if fdy else None, fdy)
    put("syntheticContentTypes", list(fdy.payload.get("generatesContent", [])) if fdy else None, fdy)
    put("contentMarking", fdy.payload.get("contentCredentials") if fdy and "contentCredentials" in fdy.payload else None, fdy)
    put("evaluations", [f"{e.get('name', '')} {e.get('date', '')}: {e.get('score', '')}" for e in fdy.payload.get("evaluations", [])] if fdy else None, fdy)
    put("incidents", list(dfn.payload.get("incidents", [])) if dfn else [], dfn)

    # ---- Annex III suggestion: a prompt for the human classification, never a conclusion ----
    hay = " ".join(
        [name, instructions or ""] + [k["name"] for k in knowledge] + cats
    ).lower()
    suggested = sorted(area for area, words in _annex_iii().items() if any(w in hay for w in words))
    put("suggestedAnnexIII", suggested, dv, confidence=0.4, note="Keyword heuristic. A person confirms the risk tier.")

    return AgentFacts(
        id=agent.agent_id,
        tenant_id=tenant_id,
        agent_id=agent.agent_id,
        name=name,
        facts=facts,
        last_scan_id=scan_id,
    )
