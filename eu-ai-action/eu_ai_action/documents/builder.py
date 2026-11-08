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
    source: Literal["fact", "rule_result", "llm", "human"]
    key: str = ""


class DocType(BaseModel):
    id: str
    rule: str
    template_reviewed_by: str = ""
    titles: dict[str, str]
    fields: list[FieldDef]

    @property
    def template_reviewed(self) -> bool:
        return bool(self.template_reviewed_by.strip())


def load_doc_types(path: str) -> dict[str, DocType]:
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or []
    types = [DocType.model_validate(r) for r in raw]
    return {t.id: t for t in types}


# Lists where "empty" is a real finding (the source answered) rather than missing data.
EMPTY_MEANS_NONE = {"knowledgeSources", "connectors", "dataCategories", "sensitivityLabels"}
NONE_FOUND = {"en": "None found", "es": "Ninguno encontrado"}


def fmt(value: Any) -> str | None:
    if value is None or value == "" or value == []:
        return None
    if isinstance(value, bool):
        return "yes" if value else "no"
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
        else:
            val = (human_inputs.get(f.name) or "").strip() or None
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
        id=f"{facts.agent_id}.{doc_type.id}.{language}.v{version}",
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
