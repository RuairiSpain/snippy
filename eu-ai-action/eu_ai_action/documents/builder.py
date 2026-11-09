from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel

from ..models import AgentFacts, Document, DutyResult, FieldValue, stable_hash
from ..rules.schema import Rule
from .narrative import NarrativeGenerator


class FieldDef(BaseModel):
    name: str
    source: Literal["fact", "rule_result", "llm", "human", "incident", "deadline", "org", "static"]
    key: str = ""
    label: dict[str, str] = {}  # shown in the portal for human fields
    hours: int = 0  # deadline: hours after the incident timestamp named in key
    text: dict[str, str] = {}  # static text per language


class Row(BaseModel):
    label: dict[str, str]
    field: str


class Section(BaseModel):
    title: dict[str, str]
    text: dict[str, str] = {}
    rows: list[Row] = []
    bullets: dict[str, list[str]] = {}


class DocType(BaseModel):
    id: str
    rule: str
    template_reviewed_by: str = ""
    titles: dict[str, str]
    fields: list[FieldDef]
    layout: list[Section] = []  # when present, the document is generated from this instead of a Jinja template
    basis: dict[str, str] = {}

    @property
    def template_reviewed(self) -> bool:
        return bool(self.template_reviewed_by.strip())


def load_doc_types(path: str) -> dict[str, DocType]:
    """Accepts a directory (all types*.yaml in it) or a single file."""
    p = Path(path)
    files = sorted(p.glob("types*.yaml")) if p.is_dir() else [p]
    types: list[DocType] = []
    for f in files:
        types.extend(DocType.model_validate(r) for r in (yaml.safe_load(f.read_text(encoding="utf-8")) or []))
    ids = [t.id for t in types]
    if len(ids) != len(set(ids)):
        raise ValueError("duplicate document type ids")
    return {t.id: t for t in types}


# Lists where "empty" is a real finding (the source answered) rather than missing data.
EMPTY_MEANS_NONE = {"knowledgeSources", "connectors", "dataCategories", "sensitivityLabels", "literacyGaps", "overseers", "suggestedAnnexIII"}
NONE_FOUND = {"en": "None found", "es": "Ninguno encontrado"}


def deadline_text(ts: Any, hours: int) -> str | None:
    from datetime import datetime, timedelta

    if not ts:
        return None
    try:
        t = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except ValueError:
        return None
    return (t + timedelta(hours=hours)).strftime("%Y-%m-%d %H:%M UTC")


def fmt(value: Any) -> str | None:
    if value is None or value == "" or value == []:
        return None
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, dict):
        return "; ".join(f"{k}: {v}" for k, v in value.items()) or None
    if isinstance(value, list):
        parts = []
        for v in value:
            if isinstance(v, dict):
                parts.append(f"{v.get('name', '')} ({v.get('type', '')})".strip())
            else:
                parts.append(str(v))
        return ", ".join(parts) or None
    return str(value)


def build_document(
    *,
    doc_type: DocType,
    rule: Rule,
    rule_set_version: str,
    facts: AgentFacts,
    duty: DutyResult,
    human_inputs: dict[str, str],
    language: str,
    narrative: NarrativeGenerator,
    version: int,
    subject: str = "",
    incident: dict[str, Any] | None = None,
    org: dict[str, str] | None = None,
) -> Document:
    fields: dict[str, FieldValue] = {}
    llm_names = [f.name for f in doc_type.fields if f.source == "llm"]
    nres = narrative.generate(facts, llm_names, language) if llm_names else None
    for f in doc_type.fields:
        if f.source == "fact":
            fv = facts.facts.get(f.key)
            val = fmt(fv.value if fv else None)
            if val is None and fv and fv.source and f.key in EMPTY_MEANS_NONE and fv.value == []:
                val = NONE_FOUND[language]
            fields[f.name] = FieldValue(value=val, source="fact", ref=(fv.evidence_id or "") if fv else "", pending=val is None)
        elif f.source == "rule_result":
            fields[f.name] = FieldValue(value=duty.result, source="rule_result", ref=duty.id)
        elif f.source == "llm":
            assert nres is not None
            val = nres.values.get(f.name)
            fields[f.name] = FieldValue(
                value=val, source="llm" if nres.source == "llm" else "derived", ref=nres.model or "template", pending=val is None
            )
        elif f.source == "incident":
            val = fmt((incident or {}).get(f.key))
            fields[f.name] = FieldValue(value=val, source="fact", ref=subject, pending=val is None)
        elif f.source == "deadline":
            val = deadline_text((incident or {}).get(f.key), f.hours)
            fields[f.name] = FieldValue(value=val, source="derived", ref=f"{f.key}+{f.hours}h", pending=val is None)
        elif f.source == "org":
            val = fmt((org or {}).get(f.key))
            fields[f.name] = FieldValue(value=val, source="fact", ref="organisation profile", pending=val is None)
        elif f.source == "static":
            fields[f.name] = FieldValue(value=f.text.get(language), source="static")
        else:
            raw = human_inputs.get(f"{subject}:{f.name}") if subject else None
            val = ((raw if raw is not None else human_inputs.get(f.name)) or "").strip() or None
            fields[f.name] = FieldValue(value=val, source="human", pending=val is None)
    # Hash of the inputs that determine the content, so a change in facts or inputs creates a new version.
    input_hash = stable_hash(
        {
            "fields": {k: (v.value, v.source) for k, v in fields.items()},
            "rule_set": rule_set_version,
            "lang": language,
        }
    )
    return Document(
        id=f"{facts.agent_id}.{doc_type.id}." + (f"{subject}." if subject else "") + f"{language}.v{version}",
        subject=subject,
        tenant_id=facts.tenant_id,
        agent_id=facts.agent_id,
        doc_type=doc_type.id,
        language=language,
        version=version,
        fields=fields,
        input_hash=input_hash,
        rule_id=rule.id,
        rule_set_version=rule_set_version,
        counsel_reviewed=rule.reviewed and doc_type.template_reviewed,
        llm_model=nres.model if nres else "",
        llm_prompt_hash=nres.prompt_hash if nres else "",
    )
