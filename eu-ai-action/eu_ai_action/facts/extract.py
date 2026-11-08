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


@lru_cache
def _categories() -> dict[str, list[str]]:
    return yaml.safe_load(_CATS.read_text(encoding="utf-8"))


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

    return AgentFacts(
        id=agent.agent_id,
        tenant_id=tenant_id,
        agent_id=agent.agent_id,
        name=name,
        facts=facts,
        last_scan_id=scan_id,
    )
